import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import pytest
from starlette.types import Message

from tests.helpers import MemoryLedger
from tollgate.providers.base import UpstreamResponse
from tollgate.streaming.sse import (
    SSEDecoder,
    SSEProxyResponse,
    StreamTally,
    error_event,
    event_data,
)
from tollgate.usage.ledger import Metering


def test_decoder_reassembles_events_across_reads() -> None:
    d = SSEDecoder()
    assert d.feed(b"data: a\n\nda") == [b"data: a"]
    assert d.feed(b"ta: b\n") == []
    assert d.feed(b"\ndata: c\n\n") == [b"data: b", b"data: c"]


def test_decoder_normalises_crlf_split_across_reads() -> None:
    d = SSEDecoder()
    assert d.feed(b"data: a\r\n\r") == []
    assert d.feed(b"\n: keep-alive\r\n\r\n") == [b"data: a", b": keep-alive"]


def test_decoder_skips_extra_blank_lines() -> None:
    assert SSEDecoder().feed(b"\n\n\n\ndata: a\n\n\n") == [b"data: a"]


def test_event_data() -> None:
    assert event_data(b'data: {"a": 1}') == '{"a": 1}'
    assert event_data(b"data:x") == "x"
    assert event_data(b"event: e\ndata: a\ndata: b") == "a\nb"
    assert event_data(b": comment") is None


def test_error_event_shape() -> None:
    raw = error_event("boom", "upstream_timeout")
    assert raw.endswith(b"\n\n")
    body = json.loads(event_data(raw.strip()) or "")
    assert body["error"] == {
        "message": "boom",
        "type": "api_error",
        "param": None,
        "code": "upstream_timeout",
    }


def _chunk(delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any) -> str:
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    return json.dumps({"choices": choices, **extra})


def test_tally_drops_unrequested_usage_chunk_but_keeps_usage() -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    for asked in (False, True):
        tally = StreamTally(forward_usage_chunk=asked)
        assert tally.observe(_chunk(usage=usage)) is asked
        assert tally.usage == (3, 2)


def test_tally_completion_signals() -> None:
    tally = StreamTally(forward_usage_chunk=False)
    tally.observe(_chunk({"content": "x"}, finish="stop"))
    assert tally.finished and not tally.done
    tally.observe("[DONE]")
    assert tally.done


def test_tally_first_token_ignores_role_chunk() -> None:
    tally = StreamTally(forward_usage_chunk=False)
    tally.observe(_chunk({"role": "assistant", "content": ""}))
    assert tally.first_token_at is None
    tally.observe(_chunk({"content": "hi"}))
    assert tally.first_token_at is not None


def test_tally_collects_generated_text_including_tool_calls() -> None:
    tally = StreamTally(forward_usage_chunk=False)
    tally.observe(_chunk({"content": "a"}))
    call = {"index": 0, "function": {"name": "f", "arguments": '{"x"'}}
    tally.observe(_chunk({"tool_calls": [call]}))
    assert tally.generated == 'af{"x"'
    assert tally.first_token_at is not None


def test_tally_flags_error_events_and_forwards_odd_payloads() -> None:
    tally = StreamTally(forward_usage_chunk=False)
    assert tally.observe('{"error": {"message": "x"}}')
    assert tally.upstream_error
    for odd in (None, "not json", "[1, 2]", '{"choices": "nope"}', '{"choices": [1]}'):
        assert tally.observe(odd)
    assert not (tally.done or tally.finished)


# --- SSEProxyResponse driven directly, with a fake upstream ---------------------


def _event(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return f"data: {_chunk(delta, finish)}\n\n".encode()


class LingeringStream:
    """Upstream that sends `data`, then raises `error` or keeps the connection
    open until released."""

    def __init__(self, data: bytes, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.release = asyncio.Event()
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self.data
        if self.error is not None:
            raise self.error
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed = True


class FakeProvider:
    name = "fake"

    def __init__(self, stream: LingeringStream, open_error: Exception | None = None) -> None:
        self.stream = stream
        self.open_error = open_error

    async def chat_completion(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> UpstreamResponse:
        raise NotImplementedError

    async def open_stream(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> LingeringStream:
        if self.open_error is not None:
            raise self.open_error  # before anything was sent
        on_sent()
        return self.stream


class Sent:
    """Records the ASGI messages a response sends."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self._changed = asyncio.Event()

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)
        self._changed.set()

    async def wait_for(self, predicate: Callable[[list[Message]], bool]) -> None:
        async with asyncio.timeout(2):
            while True:
                self._changed.clear()
                if predicate(self.messages):
                    return
                await self._changed.wait()


async def _run(
    stream: LingeringStream, open_error: Exception | None = None
) -> tuple["asyncio.Task[None]", Sent, asyncio.Event, MemoryLedger, Metering]:
    ledger = MemoryLedger()
    metering = Metering(ledger)
    meter = metering.meter(
        request_id="r1",
        tenant_id="t1",
        api_key_id="k1",
        started_at=time.perf_counter(),
        model="gpt-4o-mini",
        provider="fake",
        messages=[{"role": "user", "content": "hi"}],
    )
    response = SSEProxyResponse(
        provider=FakeProvider(stream, open_error),
        body={},
        client_headers={},
        forward_usage_chunk=False,
        meter=meter,
    )
    sent = Sent()
    disconnected = asyncio.Event()

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(response({"type": "http"}, receive, sent))
    return task, sent, disconnected, ledger, metering


async def test_response_ends_at_done_while_upstream_lingers() -> None:
    stream = LingeringStream(_event({"content": "hi"}, "stop") + b"data: [DONE]\n\n")
    task, sent, disconnected, ledger, _ = await _run(stream)
    await sent.wait_for(lambda m: bool(m) and m[-1].get("more_body") is False)
    assert sent.messages[-1]["body"].endswith(b"data: [DONE]\n\n")
    # Servers report a disconnect once the response is complete. That must
    # not cancel the relay, which is still reading the upstream to its end.
    disconnected.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    stream.release.set()
    await task
    assert stream.closed
    (event,) = ledger.events
    assert event.status == "success"


async def test_disconnect_mid_stream_cancels_relay_and_closes_upstream() -> None:
    stream = LingeringStream(_event({"content": "hello"}))
    task, sent, disconnected, ledger, _ = await _run(stream)
    await sent.wait_for(lambda m: len(m) == 2)  # response start + first event
    disconnected.set()
    async with asyncio.timeout(2):
        await task
    assert stream.closed
    assert not stream.release.is_set()  # cancelled mid-read, not finished
    (event,) = ledger.events
    assert event.status == "client_disconnect"
    assert event.usage_estimated
    assert event.completion_tokens == 1


async def test_shutdown_mid_stream_records_gateway_shutdown() -> None:
    """uvicorn cancels the request task once its graceful shutdown timeout
    expires. That is the gateway cutting the stream off, not the client."""
    stream = LingeringStream(_event({"content": "hello"}))
    task, sent, _, ledger, metering = await _run(stream)
    await sent.wait_for(lambda m: len(m) == 2)
    task.cancel()
    # What the app's lifespan shutdown waits on before tearing anything down.
    async with asyncio.timeout(2):
        await metering.wait_idle()
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"
    assert event.usage_estimated
    assert event.completion_tokens == 1
    assert stream.closed
    with pytest.raises(asyncio.CancelledError):
        await task
    # The client's stream ends with an event saying why, rather than just stopping.
    last = sent.messages[-1]
    assert last["more_body"] is False
    body = json.loads(event_data(last["body"].strip()) or "")
    assert body["error"]["code"] == "gateway_shutdown"


async def test_internal_error_mid_stream_records_gateway_error() -> None:
    stream = LingeringStream(_event({"content": "hello"}), error=RuntimeError("bug"))
    task, sent, _, ledger, _ = await _run(stream)
    with pytest.raises(RuntimeError, match="bug"):
        await task
    assert stream.closed
    # The client's stream ends with an error event that gives nothing away.
    last = sent.messages[-1]
    assert last["more_body"] is False
    body = json.loads(event_data(last["body"].strip()) or "")
    assert body["error"]["code"] == "internal_error"
    assert "r1" in body["error"]["message"]
    assert b"bug" not in last["body"]
    (event,) = ledger.events
    assert event.status == "gateway_error"
    # Whatever is known: the prompt and the one token relayed, estimated.
    assert event.usage_estimated
    assert event.completion_tokens == 1


async def test_internal_error_before_upstream_opens_records_zero_tokens() -> None:
    stream = LingeringStream(b"")
    task, _, _, ledger, _ = await _run(stream, open_error=RuntimeError("bug"))
    with pytest.raises(RuntimeError, match="bug"):
        await task
    (event,) = ledger.events
    assert event.status == "gateway_error"
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_shutdown_after_done_keeps_success() -> None:
    """A shutdown that only interrupts reading the upstream to its end, after
    the client already has the whole response, doesn't relabel the request."""
    stream = LingeringStream(_event({"content": "hi"}, "stop") + b"data: [DONE]\n\n")
    task, sent, _, ledger, _ = await _run(stream)
    await sent.wait_for(lambda m: bool(m) and m[-1].get("more_body") is False)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (event,) = ledger.events
    assert event.status == "success"
