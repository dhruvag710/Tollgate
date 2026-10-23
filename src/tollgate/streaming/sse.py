"""SSE passthrough: relays upstream events as they arrive, captures usage from
the stream, and cancels the upstream request when the client disconnects."""

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from typing import Any

import anyio
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from tollgate.errors import (
    SHUTDOWN_MESSAGE,
    TollgateError,
    error_body,
    error_response,
    internal_error_message,
    upstream_error_response,
)
from tollgate.observability.timing import Timings
from tollgate.providers.base import Provider, UpstreamResponse, UpstreamStream, UpstreamStreamError
from tollgate.ratelimit.estimate import generated_text
from tollgate.usage.ledger import UsageMeter, parse_usage

SSE_HEADERS = [
    (b"content-type", b"text/event-stream; charset=utf-8"),
    (b"cache-control", b"no-cache"),
    (b"x-accel-buffering", b"no"),  # stop nginx (multi-replica setup) buffering the stream
]


class SSEDecoder:
    """Splits a byte stream into complete SSE events.

    Holds back only an incomplete trailing event. Events come back without
    their blank-line terminator and with CRLF normalised to LF. (Lone-CR line
    endings are not supported; OpenAI-compatible providers don't use them.)
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        *events, self._buffer = (self._buffer + data).replace(b"\r\n", b"\n").split(b"\n\n")
        return [e.lstrip(b"\n") for e in events if e.strip(b"\n")]


def event_data(event: bytes) -> str | None:
    """The event's data field (data lines joined by LF), or None if it has none."""
    lines = [line[5:] for line in event.split(b"\n") if line.startswith(b"data:")]
    if not lines:
        return None
    return b"\n".join(v[1:] if v.startswith(b" ") else v for v in lines).decode(errors="replace")


def error_event(message: str, code: str) -> bytes:
    """An event that makes OpenAI SDKs raise APIError mid-stream."""
    return b"data: " + json.dumps(error_body(message, "api_error", code)).encode() + b"\n\n"


class StreamTally:
    """What the relay has learned from the events seen so far."""

    def __init__(self, *, forward_usage_chunk: bool) -> None:
        self.forward_usage_chunk = forward_usage_chunk
        self.usage: tuple[int, int] | None = None
        self.done = False  # saw [DONE]
        self.finished = False  # saw a finish_reason
        self.upstream_error = False  # upstream sent an error event
        self.first_token_at: float | None = None
        self._generated: list[str] = []

    @property
    def generated(self) -> str:
        return "".join(self._generated)

    def observe(self, data: str | None) -> bool:
        """Update from one event's data. Returns False if the event must not be forwarded."""
        if data is None:  # comment or keep-alive
            return True
        if data == "[DONE]":
            self.done = True
            return True
        try:
            chunk = json.loads(data)
        except ValueError:
            return True
        if not isinstance(chunk, dict):
            return True
        if isinstance(chunk.get("error"), dict):
            self.upstream_error = True
            return True
        usage = parse_usage(chunk.get("usage"))
        if usage is not None:
            self.usage = usage
        choices = chunk.get("choices")
        if not choices:
            # The usage-only final chunk Tollgate asked for: forward it only if the client did too.
            return usage is None or self.forward_usage_chunk
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finished = True
            if text := generated_text(choice.get("delta")):
                self._generated.append(text)
                if self.first_token_at is None:
                    self.first_token_at = time.perf_counter()
        return True


class SSEProxyResponse(Response):
    """Streams a chat completion from `provider` to the client.

    Nothing is sent until the first upstream event arrives, so a failure before
    that is still an ordinary JSON error response. After it, a failure ends the
    stream with an error event and is never retried. A watcher on `receive`
    notices the client disconnecting at any point and cancels the relay, which
    closes the upstream response and so cancels the upstream request. Being cut
    off by a server shutdown does the same.
    """

    def __init__(
        self,
        *,
        provider: Provider,
        body: dict[str, Any],
        client_headers: Mapping[str, str],
        forward_usage_chunk: bool,
        meter: UsageMeter,
        timings: Timings,
        server_timing: bool,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._body = body
        self._client_headers = client_headers
        self._meter = meter
        self._timings = timings
        self._server_timing = server_timing
        self._tally = StreamTally(forward_usage_chunk=forward_usage_chunk)
        self._upstream: UpstreamStream | None = None
        self._started = False  # http.response.start sent
        self._responded = False  # final message sent, or about to be

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # measure() records exactly one usage event, settling a server shutdown
        # (cancellation from outside; relay_scope absorbs the watcher's) as
        # gateway_shutdown and any other exception as gateway_error.
        async with self._meter.measure():
            relay_scope = anyio.CancelScope()
            watcher = asyncio.create_task(self._cancel_on_disconnect(receive, relay_scope))
            try:
                with relay_scope:
                    await self._relay(scope, receive, send)
            except asyncio.CancelledError:
                # relay_scope absorbs the watcher's cancellation: this is a shutdown.
                await self._end_started_stream(send, SHUTDOWN_MESSAGE, "gateway_shutdown")
                raise
            except Exception:
                message = internal_error_message(self._meter.request_id)
                await self._end_started_stream(send, message, "internal_error")
                raise
            finally:
                watcher.cancel()
                self._report_usage()
                if self._upstream is not None:
                    with anyio.CancelScope(shield=True):
                        await self._upstream.aclose()

    async def _cancel_on_disconnect(self, receive: Receive, relay_scope: anyio.CancelScope) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass
        # Servers also report a disconnect once the response is complete, and
        # the relay may still be reading the upstream to its end by then.
        if not self._responded:
            self._meter.settle("client_disconnect")
            relay_scope.cancel()

    def _report_usage(self) -> None:
        tally, meter = self._tally, self._meter
        if tally.usage is not None:
            meter.reported = tally.usage
        meter.generated = tally.generated
        meter.first_token_at = tally.first_token_at

    async def _end_started_stream(self, send: Send, message: str, code: str) -> None:
        """End a stream the client is already reading with an error event, as an
        upstream failure would. A stream not yet started is answered instead by
        UnhandledErrorMiddleware, with a 500 or a 503."""
        if not self._started or self._responded:
            return
        with contextlib.suppress(Exception):  # sending may be what failed
            await self._send(send, error_event(message, code), more_body=False)

    async def _relay(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            with self._timings.stage("upstream-open"):
                opened = await self._provider.open_stream(
                    self._body, client_headers=self._client_headers, on_sent=self._meter.mark_sent
                )
        except TollgateError as exc:
            self._meter.settle("provider_error")
            await self._respond(error_response(exc), scope, receive, send)
            return
        if isinstance(opened, UpstreamResponse):
            self._meter.reported = (0, 0)  # an error response: nothing billed
            self._meter.settle("provider_error")
            await self._respond(
                upstream_error_response(opened, self._provider.name), scope, receive, send
            )
            return
        self._upstream = opened
        opened_at: float | None = time.perf_counter()

        decoder = SSEDecoder()
        tally = self._tally
        failure: UpstreamStreamError | None = None
        try:
            async for chunk in opened.aiter_bytes():
                if opened_at is not None:
                    self._timings.add("upstream-first-chunk", time.perf_counter() - opened_at)
                    opened_at = None
                if tally.done:
                    # The client already has [DONE]. Read on to the end of the
                    # body so the upstream connection can be reused.
                    continue
                events = [e for e in decoder.feed(chunk) if tally.observe(event_data(e))]
                if not events:
                    continue
                if tally.done:
                    # [DONE] ends the client's response, so the outcome is settled
                    # now, even though the upstream is still read to its end.
                    self._meter.settle("provider_error" if tally.upstream_error else "success")
                await self._send(
                    send, b"".join(e + b"\n\n" for e in events), more_body=not tally.done
                )
        except UpstreamStreamError as exc:
            failure = exc

        if tally.done:
            return
        if failure is None and (tally.finished or tally.upstream_error):
            # Any upstream error event has already been forwarded to the client.
            self._meter.settle("provider_error" if tally.upstream_error else "success")
            await self._send(send, b"", more_body=False)
            return
        if failure is None:
            failure = UpstreamStreamError(
                f"Upstream provider {self._provider.name!r} ended the stream early", timeout=False
            )
        self._meter.settle("provider_error")
        code = "upstream_timeout" if failure.timeout else "upstream_stream_error"
        if self._started:
            await self._send(send, error_event(str(failure), code), more_body=False)
            return
        # Nothing has reached the client yet, so this can still be an HTTP error.
        error = TollgateError(
            504 if failure.timeout else 502,
            str(failure),
            "api_error",
            code,
            headers={"X-Tollgate-Provider": self._provider.name},
        )
        await self._respond(error_response(error), scope, receive, send)

    async def _send(self, send: Send, body: bytes, *, more_body: bool = True) -> None:
        if not more_body:
            self._responded = True
        if not self._started:
            self._started = True
            headers = [*SSE_HEADERS, (b"x-tollgate-provider", self._provider.name.encode())]
            if self._server_timing:
                headers.append((b"server-timing", self._timings.server_timing().encode()))
            await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": more_body})

    async def _respond(
        self, response: Response, scope: Scope, receive: Receive, send: Send
    ) -> None:
        self._responded = True
        await response(scope, receive, send)
