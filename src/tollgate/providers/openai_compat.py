"""Provider for any OpenAI-compatible API: OpenAI, OpenRouter, Ollama, mock."""

import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import anyio
import httpx

from tollgate.config import ProviderConfig
from tollgate.providers.base import (
    UpstreamError,
    UpstreamFailure,
    UpstreamResponse,
    UpstreamStream,
    UpstreamStreamError,
)


class OpenAICompatProvider:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient, api_key: str | None):
        self.name = config.name
        self._config = config
        self._client = client
        self._api_key = api_key
        self._url = config.base_url.rstrip("/") + "/chat/completions"
        self._timeout = httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s)
        # From sending to the first byte; None leaves it to the read timeout.
        self._first_byte_s = config.first_byte_timeout_s

    def _headers(self, client_headers: Mapping[str, str]) -> dict[str, str]:
        # The client's own Authorization header is a Tollgate key; never forward it.
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._config.forward_mock_headers:
            headers.update(
                (k, v) for k, v in client_headers.items() if k.lower().startswith("x-mock-")
            )
        return headers

    async def chat_completion(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> UpstreamResponse:
        # A non-streamed response's headers come once it is ready: its first byte.
        with anyio.move_on_after(self._first_byte_s) as deadline:
            resp = await self._send(body, self._headers(client_headers), on_sent)
        if deadline.cancelled_caught:
            raise UpstreamError(self.name, "timeout", first_byte=False)
        try:
            if resp.status_code >= 400:
                return await self._error_response(resp)
            try:
                await resp.aread()
            except httpx.HTTPError as exc:
                raise self._transport_error(exc, first_byte=True) from exc
            data = _json_object(resp)
            if data is None:
                raise UpstreamError(self.name, "bad_response", first_byte=True)
            return UpstreamResponse(resp.status_code, data)
        finally:
            await resp.aclose()

    async def open_stream(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> UpstreamResponse | UpstreamStream:
        # identity: a compressing upstream may hold SSE events in its compressor buffer.
        headers = {**self._headers(client_headers), "Accept-Encoding": "identity"}
        resp: httpx.Response | None = None
        try:
            # Everything up to the first chunk is before the first byte: a
            # failure here may still be retried on another provider.
            with anyio.move_on_after(self._first_byte_s):
                resp = await self._send(body, headers, on_sent)
                if resp.status_code >= 400:
                    return await self._error_response(resp)
                chunks = resp.aiter_bytes()
                headers_at = time.perf_counter()
                try:
                    first = await anext(chunks)
                except StopAsyncIteration:
                    # Success, then nothing at all: as good as a dropped connection.
                    raise UpstreamError(
                        self.name, "dropped", first_byte=False, what="ended the stream early"
                    ) from None
                except httpx.TimeoutException as exc:
                    raise UpstreamError(self.name, "timeout", first_byte=False) from exc
                except httpx.HTTPError as exc:
                    raise UpstreamError(self.name, "dropped", first_byte=False) from exc
                stream = _HttpxStream(resp, self.name, first, chunks)
                stream.first_chunk_s, resp = time.perf_counter() - headers_at, None
                return stream
            raise UpstreamError(self.name, "timeout", first_byte=False)  # the deadline passed
        finally:
            if resp is not None:  # not handed over to a stream
                await resp.aclose()

    async def _send(
        self, body: dict[str, Any], headers: dict[str, str], on_sent: Callable[[], None]
    ) -> httpx.Response:
        async def trace(event: str, info: dict[str, Any]) -> None:
            # httpcore reports each step; this one means the whole request is out.
            if event.endswith(".send_request_body.complete"):
                on_sent()

        request = self._client.build_request(
            "POST",
            self._url,
            json=body,
            headers=headers,
            timeout=self._timeout,
            extensions={"trace": trace},
        )
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, first_byte=False) from exc
        on_sent()  # a response proves it, whatever the transport
        return resp

    async def _error_response(self, resp: httpx.Response) -> UpstreamResponse:
        """The provider answered with an error status. Its body is relayed if it
        is a JSON object; one that can't be read doesn't change the answer."""
        try:
            await resp.aread()
        except httpx.HTTPError:
            data = None
        else:
            data = _json_object(resp)
        return UpstreamResponse(resp.status_code, data or {}, resp.headers.get("retry-after"))

    def _transport_error(self, exc: httpx.HTTPError, *, first_byte: bool) -> UpstreamError:
        kind: UpstreamFailure = (
            "timeout" if isinstance(exc, httpx.TimeoutException) else "unreachable"
        )
        return UpstreamError(self.name, kind, first_byte=first_byte)


def _json_object(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class _HttpxStream:
    def __init__(
        self,
        response: httpx.Response,
        provider: str,
        first: bytes,
        rest: AsyncIterator[bytes],
    ) -> None:
        self._response = response
        self._provider = provider
        self._first: bytes | None = first
        self._rest = rest
        self.first_chunk_s = 0.0

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._first is not None:
            first, self._first = self._first, None
            yield first
        try:
            async for chunk in self._rest:
                yield chunk
        except httpx.TimeoutException as exc:
            raise UpstreamStreamError(
                f"Upstream provider {self._provider!r} timed out", timeout=True
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamStreamError(
                f"Upstream provider {self._provider!r} dropped the connection", timeout=False
            ) from exc

    async def aclose(self) -> None:
        await self._response.aclose()
