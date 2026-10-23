"""Phase 1.3: streaming passthrough, usage capture, client disconnects.

Acceptance:
- TTFT through the gateway stays within the overhead budget of TTFT direct to
  the mock (SPEC B1b: p99 < 20 ms added): test_ttft_overhead_within_budget
- A client disconnect cancels the upstream request:
  test_client_disconnect_cancels_upstream_request (mid-stream) and
  test_disconnect_before_first_token_cancels_upstream_request
"""

import itertools
import json
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from mock_provider.app import count_prompt_tokens
from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    MemoryLedger,
    assert_internal_error_response,
    in_flight_bound,
    run_server,
    run_uvicorn_process,
    serve,
    wait_until,
    write_providers,
)
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.ratelimit.estimate import count_tokens, estimate_prompt_tokens
from tollgate.streaming import sse

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "hello"}]
STREAM_BODY = {"model": MODEL, "messages": MESSAGES, "stream": True}
SHUTDOWN_ERROR = {
    "error": {
        "message": "Tollgate is shutting down. Retry the request.",
        "type": "api_error",
        "param": None,
        "code": "gateway_shutdown",
    }
}


def _is_token_line(line: str) -> bool:
    return line.startswith("data: ") and '"content":"' in line and '"content":""' not in line


# --- through the mock, in-process ----------------------------------------------


async def test_sdk_stream_matches_non_streamed_text(sdk: openai.AsyncOpenAI) -> None:
    plain = await sdk.chat.completions.create(model=MODEL, messages=MESSAGES, seed=4)
    stream = await sdk.chat.completions.create(model=MODEL, messages=MESSAGES, seed=4, stream=True)
    chunks = [c async for c in stream]
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == (
        plain.choices[0].message.content
    )
    assert chunks[-1].choices[0].finish_reason == "stop"


async def test_stream_response_headers(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-tollgate-provider"] == "mock"
    assert r.headers["x-accel-buffering"] == "no"
    uuid.UUID(r.headers["x-request-id"])
    assert r.text.endswith("data: [DONE]\n\n")


async def test_usage_captured_without_forwarding_unrequested_chunk(
    sdk: openai.AsyncOpenAI, ledger: MemoryLedger
) -> None:
    stream = await sdk.chat.completions.create(
        model=MODEL, messages=MESSAGES, stream=True, extra_headers={"X-Mock-Output-Tokens": "7"}
    )
    chunks = [c async for c in stream]
    # The client never asked for usage, so code doing chunk.choices[0] must not break.
    assert all(c.choices for c in chunks)
    (event,) = ledger.events
    assert event.status == "success"
    assert not event.usage_estimated
    assert (event.prompt_tokens, event.completion_tokens) == (
        count_prompt_tokens(MESSAGES, MODEL),
        7,
    )
    assert event.ttft_ms is not None
    assert event.latency_ms >= event.ttft_ms


async def test_usage_chunk_forwarded_when_client_asks(
    sdk: openai.AsyncOpenAI, ledger: MemoryLedger
) -> None:
    stream = await sdk.chat.completions.create(
        model=MODEL, messages=MESSAGES, stream=True, stream_options={"include_usage": True}
    )
    chunks = [c async for c in stream]
    final = chunks[-1]
    assert final.choices == []
    assert final.usage is not None
    (event,) = ledger.events
    assert (event.prompt_tokens, event.completion_tokens) == (
        final.usage.prompt_tokens,
        final.usage.completion_tokens,
    )


async def test_upstream_error_status_is_plain_http_error(
    sdk: openai.AsyncOpenAI, ledger: MemoryLedger
) -> None:
    with pytest.raises(openai.RateLimitError) as exc:
        await sdk.chat.completions.create(
            model=MODEL,
            messages=MESSAGES,
            stream=True,
            extra_headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "429"},
        )
    assert exc.value.response.headers["retry-after"] == "1"
    (event,) = ledger.events
    assert event.status == "provider_error"
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_stream_options_must_be_object(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/chat/completions", json={**STREAM_BODY, "stream_options": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# --- scripted upstream, in-process ----------------------------------------------

SSE = {"content-type": "text/event-stream"}


def ev(data: Any) -> bytes:
    return b"data: " + (data if isinstance(data, str) else json.dumps(data)).encode() + b"\n\n"


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


ROLE = ev(chunk({"role": "assistant", "content": ""}))
DONE = ev("[DONE]")


def token(text: str, finish: str | None = None) -> bytes:
    return ev(chunk({"content": text}, finish))


class Scripted(httpx.AsyncByteStream):
    """Upstream body that yields the given bytes and raises the given errors, in order."""

    def __init__(self, *parts: bytes | Exception) -> None:
        self.parts = parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            yield part


def upstream(*parts: bytes | Exception) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, headers=SSE, stream=Scripted(*parts))


@asynccontextmanager
async def scripted_gateway(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> AsyncIterator[tuple[httpx.AsyncClient, MemoryLedger]]:
    ledger = MemoryLedger()
    app = create_app(
        Settings(providers_config=write_providers(tmp_path, "http://upstream/v1")),
        upstream_transport=httpx.MockTransport(handler),
        ledger=ledger,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client, ledger


def _sdk(client: httpx.AsyncClient) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        base_url="http://t/v1", api_key="tg_test", http_client=client, max_retries=0
    )


async def test_include_usage_injected_upstream(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers=SSE, stream=Scripted(token("hi", "stop"), DONE))

    async with scripted_gateway(tmp_path, handler) as (client, _):
        await client.post(
            "/v1/chat/completions",
            json={**STREAM_BODY, "stream_options": {"include_usage": False, "other": 1}},
        )
    assert json.loads(seen[0].content)["stream_options"] == {"include_usage": True, "other": 1}
    assert seen[0].headers["accept-encoding"] == "identity"


async def test_events_split_across_reads_pass_through_unchanged(tmp_path: Path) -> None:
    body = b": keep-alive\n\n" + ROLE + token("hello") + token(" world", "stop") + DONE
    one_byte_reads = [body[i : i + 1] for i in range(len(body))]
    async with scripted_gateway(tmp_path, upstream(*one_byte_reads)) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.content == body
    (event,) = ledger.events
    assert event.status == "success"


async def test_crlf_events_are_normalised(tmp_path: Path) -> None:
    body = ROLE + token("hi", "stop") + DONE
    async with scripted_gateway(tmp_path, upstream(body.replace(b"\n", b"\r\n"))) as (client, _):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.content == body


async def test_missing_usage_is_estimated(tmp_path: Path) -> None:
    async with scripted_gateway(tmp_path, upstream(ROLE, token("hello world", "stop"), DONE)) as (
        client,
        ledger,
    ):
        await client.post("/v1/chat/completions", json=STREAM_BODY)
    (event,) = ledger.events
    assert event.status == "success"
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, MODEL)
    assert event.completion_tokens == 2


async def test_failure_after_first_event_ends_with_error_event(tmp_path: Path) -> None:
    script = upstream(ROLE, token("hello"), httpx.RemoteProtocolError("peer closed connection"))
    async with scripted_gateway(tmp_path, script) as (client, ledger):
        stream = await _sdk(client).chat.completions.create(
            model=MODEL, messages=MESSAGES, stream=True
        )
        received = []
        with pytest.raises(openai.APIError, match="dropped the connection"):
            async for c in stream:
                received.append(c.choices[0].delta.content)
    assert received == ["", "hello"]
    (event,) = ledger.events
    assert event.status == "provider_error"
    assert event.usage_estimated
    assert event.completion_tokens == 1


async def test_stream_ending_early_ends_with_error_event(tmp_path: Path) -> None:
    async with scripted_gateway(tmp_path, upstream(ROLE, token("hel"))) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    last = json.loads(r.text.strip().split("\n\n")[-1].removeprefix("data: "))
    assert last["error"]["code"] == "upstream_stream_error"
    assert "[DONE]" not in r.text
    assert ledger.events[0].status == "provider_error"


async def test_nothing_after_done_reaches_client_or_counts_as_failure(tmp_path: Path) -> None:
    body = ROLE + token("hi", "stop") + DONE
    script = upstream(body, b": trailing\n\n", httpx.RemoteProtocolError("reset after [DONE]"))
    async with scripted_gateway(tmp_path, script) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.content == body
    assert ledger.events[0].status == "success"


async def test_upstream_error_event_is_forwarded_once(tmp_path: Path) -> None:
    error = {"error": {"message": "overloaded", "type": "server_error", "code": None}}
    async with scripted_gateway(tmp_path, upstream(ROLE, ev(error))) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.text.count('"error"') == 1
    assert "overloaded" in r.text
    assert ledger.events[0].status == "provider_error"


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ReadTimeout("slow"), 504, "upstream_timeout"),
        (httpx.RemoteProtocolError("closed"), 502, "upstream_stream_error"),
    ],
)
async def test_failure_before_first_event_is_plain_http_error(
    tmp_path: Path, exc: Exception, status: int, code: str
) -> None:
    async with scripted_gateway(tmp_path, upstream(exc)) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    assert r.headers["x-tollgate-provider"] == "mock"
    (event,) = ledger.events
    assert event.status == "provider_error"
    # The upstream accepted the request, so its prompt may be billed.
    assert event.usage_estimated
    assert event.completion_tokens == 0


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ConnectError("refused"), 502, "upstream_unreachable"),
        (httpx.ConnectTimeout("slow"), 504, "upstream_timeout"),
    ],
)
async def test_upstream_unreachable(tmp_path: Path, exc: Exception, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with scripted_gateway(tmp_path, handler) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    (event,) = ledger.events
    assert (event.status, event.prompt_tokens, event.usage_estimated) == (
        "provider_error",
        0,
        False,
    )


@pytest.mark.parametrize(
    "error_body",
    [Scripted(b"<html>down</html>"), Scripted(b'{"err', httpx.ReadError("reset"))],
)
async def test_error_status_with_unusable_body_keeps_its_status(
    tmp_path: Path, error_body: Scripted
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=error_body)

    async with scripted_gateway(tmp_path, handler) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_error"
    # The provider answered, with an error: nothing billed, nothing estimated.
    (event,) = ledger.events
    assert (event.status, event.prompt_tokens, event.usage_estimated) == (
        "provider_error",
        0,
        False,
    )


def fail_on_event(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the relay hit an internal error while handling upstream event `n`."""
    real = sse.event_data
    calls = 0

    def event_data(event: bytes) -> str | None:
        nonlocal calls
        calls += 1
        if calls == n:
            raise RuntimeError("bug")
        return real(event)

    monkeypatch.setattr(sse, "event_data", event_data)


async def test_internal_error_before_first_event_is_an_openai_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fail_on_event(1, monkeypatch)
    async with scripted_gateway(tmp_path, upstream(ROLE, token("hi", "stop"), DONE)) as (
        client,
        ledger,
    ):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    assert_internal_error_response(r, caplog)
    (event,) = ledger.events
    assert event.status == "gateway_error"
    assert event.usage_estimated  # the request was sent; its prompt may be billed
    assert event.completion_tokens == 0


async def test_internal_error_mid_stream_ends_with_an_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fail_on_event(3, monkeypatch)  # role chunk, "hello", then the failure
    script = upstream(ROLE, token("hello"), token(" world", "stop"), DONE)
    async with scripted_gateway(tmp_path, script) as (client, ledger):
        r = await client.post("/v1/chat/completions", json=STREAM_BODY)
    request_id = r.headers["x-request-id"]
    assert r.status_code == 200  # already sent with the first event
    last = json.loads(r.text.strip().split("\n\n")[-1].removeprefix("data: "))
    assert last == {
        "error": {
            "message": f"Tollgate hit an internal error. Request ID: {request_id}",
            "type": "api_error",
            "param": None,
            "code": "internal_error",
        }
    }
    assert "bug" not in r.text
    assert "[DONE]" not in r.text
    (record,) = [rec for rec in caplog.records if rec.name == "tollgate.errors"]
    assert request_id in record.getMessage()
    assert record.exc_info is not None
    (event,) = ledger.events
    assert event.status == "gateway_error"
    assert event.completion_tokens == 1  # "hello" reached the client


# --- real sockets ----------------------------------------------------------------


@contextmanager
def gateway_server(tmp_path: Path, mock_url: str) -> Iterator[tuple[str, MemoryLedger]]:
    ledger = MemoryLedger()
    config = write_providers(tmp_path, f"{mock_url}/v1")
    with run_server(create_app(Settings(providers_config=config), ledger=ledger)) as url:
        yield url, ledger


def test_sync_sdk_streams_over_sockets(tmp_path: Path) -> None:
    with (
        run_server(create_mock_app(env={})) as mock_url,
        gateway_server(tmp_path, mock_url) as (url, ledger),
    ):
        client = openai.OpenAI(base_url=f"{url}/v1", api_key="anything")
        stream = client.chat.completions.create(
            model=MODEL, messages=MESSAGES, stream=True, extra_headers={"X-Mock-Output-Tokens": "6"}
        )
        text = "".join(c.choices[0].delta.content or "" for c in stream)
        assert wait_until(lambda: len(ledger.events) == 1)
    assert len(text.split()) == 6
    assert ledger.events[0].completion_tokens == 6


def test_chunks_are_forwarded_as_they_arrive(tmp_path: Path) -> None:
    """No buffering: tokens the mock emits 100 ms apart reach the client 100 ms apart."""
    with (
        run_server(create_mock_app(env={})) as mock_url,
        gateway_server(tmp_path, mock_url) as (url, _),
        httpx.Client() as client,  # created first: building one takes ~200 ms on Windows
    ):
        start = time.perf_counter()
        arrivals = []
        with client.stream(
            "POST",
            f"{url}/v1/chat/completions",
            json=STREAM_BODY,
            headers={"X-Mock-Tokens-Per-Sec": "10", "X-Mock-Output-Tokens": "5"},
        ) as r:
            for line in r.iter_lines():
                if _is_token_line(line):
                    arrivals.append(time.perf_counter() - start)
    gaps = [b - a for a, b in itertools.pairwise(arrivals)]
    assert len(arrivals) == 5
    assert arrivals[0] < 0.1  # not held back until the next token
    assert min(gaps) > 0.05


def test_client_disconnect_cancels_upstream_request(tmp_path: Path) -> None:
    """Acceptance 1.3: the mock itself observes its request being cancelled."""
    body = {**STREAM_BODY, "seed": 7}
    long_reply = {"X-Mock-Output-Tokens": "1000"}
    with (
        run_server(create_mock_app(env={})) as mock_url,
        gateway_server(tmp_path, mock_url) as (url, ledger),
    ):

        def mock_stats() -> dict[str, Any]:
            stats: dict[str, Any] = httpx.get(f"{mock_url}/mock/stats").json()
            return stats

        # Seeded, so this is exactly the text the stream below will carry.
        words = (
            httpx.post(
                f"{mock_url}/v1/chat/completions",
                json={**body, "stream": False},
                headers=long_reply,
            )
            .json()["choices"][0]["message"]["content"]
            .split()
        )
        httpx.post(f"{mock_url}/mock/stats/reset")

        received = []
        # 1000 tokens at 20/s: the upstream would run for 50 s if nobody cancelled it.
        with httpx.stream(
            "POST",
            f"{url}/v1/chat/completions",
            json=body,
            headers={**long_reply, "X-Mock-Tokens-Per-Sec": "20"},
        ) as r:
            for line in r.iter_lines():
                if _is_token_line(line):
                    received.append(json.loads(line[6:])["choices"][0]["delta"]["content"])
                if len(received) == 5:
                    break
        # Leaving the block closed the connection: the client is gone.
        disconnected_at = time.perf_counter()
        assert wait_until(lambda: mock_stats()["streams_cancelled"] == 1, timeout=5)
        cancel_seconds = time.perf_counter() - disconnected_at
        assert wait_until(lambda: len(ledger.events) == 1)
        stats = mock_stats()

    assert cancel_seconds < 2
    assert stats["completed"] == 0
    assert 5 <= stats["partial_completion_tokens"] < 50
    (event,) = ledger.events
    assert event.status == "client_disconnect"
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, MODEL)
    # Partial usage covers what the gateway relayed: at least what this client
    # read, at most what the mock sent before it was cancelled.
    sent = " ".join(words[: stats["partial_completion_tokens"]])
    assert count_tokens("".join(received), MODEL) <= event.completion_tokens
    assert event.completion_tokens <= count_tokens(sent, MODEL)
    assert event.ttft_ms is not None


def test_disconnect_before_first_token_cancels_upstream_request(tmp_path: Path) -> None:
    with (
        run_server(create_mock_app(env={})) as mock_url,
        gateway_server(tmp_path, mock_url) as (url, ledger),
    ):
        # The client gives up while the upstream is still "thinking".
        with pytest.raises(httpx.ReadTimeout):
            httpx.post(
                f"{url}/v1/chat/completions",
                json=STREAM_BODY,
                headers={"X-Mock-TTFT-Ms": "30000"},
                timeout=httpx.Timeout(10, read=0.5),
            )
        assert wait_until(
            lambda: httpx.get(f"{mock_url}/mock/stats").json()["streams_cancelled"] == 1,
            timeout=5,
        )
        assert wait_until(lambda: len(ledger.events) == 1)
    (event,) = ledger.events
    assert event.status == "client_disconnect"
    assert event.completion_tokens == 0
    assert event.ttft_ms is None


def test_upstream_drop_mid_stream_ends_with_error_event(tmp_path: Path) -> None:
    with (
        run_server(create_mock_app(env={})) as mock_url,
        gateway_server(tmp_path, mock_url) as (url, ledger),
    ):
        client = openai.OpenAI(base_url=f"{url}/v1", api_key="anything", max_retries=0)
        stream = client.chat.completions.create(
            model=MODEL,
            messages=MESSAGES,
            stream=True,
            extra_headers={
                "X-Mock-Fail-Rate": "1",
                "X-Mock-Fail-Mode": "drop_mid_stream",
                "X-Mock-Output-Tokens": "10",
            },
        )
        received = []
        with pytest.raises(openai.APIError, match="dropped the connection"):
            for c in stream:
                received.append(c.choices[0].delta.content or "")
        assert wait_until(lambda: len(ledger.events) == 1)
    assert len("".join(received).split()) == 5  # the mock drops after half its tokens
    (event,) = ledger.events
    assert event.status == "provider_error"
    assert event.usage_estimated
    assert event.completion_tokens == count_tokens("".join(received), MODEL)


def test_gateway_shutdown_mid_stream_is_recorded(tmp_path: Path) -> None:
    """A stream still running when uvicorn's shutdown grace period ends is cut
    off, its upstream request cancelled, and its usage recorded."""
    ledger = MemoryLedger()
    with run_server(create_mock_app(env={})) as mock_url:
        config = write_providers(tmp_path, f"{mock_url}/v1")
        app = create_app(Settings(providers_config=config), ledger=ledger)
        with (
            serve(app, timeout_graceful_shutdown=0.5) as (url, server),
            httpx.stream(
                "POST",
                f"{url}/v1/chat/completions",
                json=STREAM_BODY,
                headers={"X-Mock-Tokens-Per-Sec": "20", "X-Mock-Output-Tokens": "1000"},
            ) as r,
        ):
            lines = r.iter_lines()
            received = 0
            for line in lines:
                received += _is_token_line(line)
                if received == 5:
                    break
            server.should_exit = True  # as on SIGTERM; the client stays connected
            assert wait_until(lambda: len(ledger.events) == 1, timeout=10)
            # The stream ends cleanly, with an error event telling the client why.
            rest = [line for line in lines if line.startswith("data: ")]
        assert "data: [DONE]" not in rest
        assert json.loads(rest[-1].removeprefix("data: ")) == SHUTDOWN_ERROR
        assert wait_until(
            lambda: httpx.get(f"{mock_url}/mock/stats").json()["streams_cancelled"] == 1
        )
        stats = httpx.get(f"{mock_url}/mock/stats").json()
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"
    assert event.usage_estimated
    assert event.completion_tokens >= 5
    # An abandoned stream (SPEC §4): the prompt reconciles exactly, the
    # completion within the tokens in flight.
    assert event.prompt_tokens == stats["partial_prompt_tokens"]
    assert 0 <= stats["partial_completion_tokens"] - event.completion_tokens <= in_flight_bound(20)


def test_gateway_shutdown_before_first_event_is_a_503(tmp_path: Path) -> None:
    ledger = MemoryLedger()
    with run_server(create_mock_app(env={})) as mock_url:
        config = write_providers(tmp_path, f"{mock_url}/v1")
        app = create_app(Settings(providers_config=config), ledger=ledger)
        with (
            serve(app, timeout_graceful_shutdown=0.5) as (url, server),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            # The upstream is still "thinking" when the shutdown cuts the stream off.
            reply = pool.submit(
                httpx.post,
                f"{url}/v1/chat/completions",
                json=STREAM_BODY,
                headers={"X-Mock-TTFT-Ms": "30000"},
                timeout=30,
            )
            assert wait_until(lambda: httpx.get(f"{mock_url}/mock/stats").json()["requests"] == 1)
            server.should_exit = True
            r = reply.result(timeout=10)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json() == SHUTDOWN_ERROR
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"
    assert event.usage_estimated  # sent: the prompt may be billed
    assert event.completion_tokens == 0


def _percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]


@pytest.mark.perf
def test_ttft_overhead_within_budget(tmp_path: Path) -> None:
    """Acceptance 1.3: time to first chunk via the gateway vs direct to the mock.

    Mock and gateway run as separate uvicorn processes, as deployed. This is a
    pass/fail check against the SPEC B1b budget (p99 < 20 ms added), not a
    benchmark: reported numbers come from bench/ (B1b).
    """
    with run_uvicorn_process("mock_provider.app:app") as mock_url:
        config = write_providers(tmp_path, f"{mock_url}/v1")
        env = {"PROVIDERS_CONFIG": str(config), "LOG_LEVEL": "WARNING"}
        with (
            run_uvicorn_process("--factory", "tollgate.main:create_app", env=env) as gw_url,
            httpx.Client(timeout=10) as client,
        ):

            def ttft(base_url: str) -> float:
                start = time.perf_counter()
                first = None
                with client.stream(
                    "POST",
                    f"{base_url}/v1/chat/completions",
                    json=STREAM_BODY,
                    headers={"X-Mock-Output-Tokens": "8"},
                ) as r:
                    for line in r.iter_lines():
                        if first is None and line.startswith("data: "):
                            first = time.perf_counter() - start
                assert first is not None
                return first

            for _ in range(20):  # warm up connections on both paths
                ttft(mock_url), ttft(gw_url)
            direct, via_gateway = [], []
            for _ in range(200):  # interleaved, so drift hits both paths alike
                direct.append(ttft(mock_url))
                via_gateway.append(ttft(gw_url))

    added_p50 = _percentile(via_gateway, 50) - _percentile(direct, 50)
    added_p99 = _percentile(via_gateway, 99) - _percentile(direct, 99)
    print(f"\nTTFT added by gateway: p50 {added_p50 * 1000:.2f} ms, p99 {added_p99 * 1000:.2f} ms")
    assert added_p99 < 0.020
