"""Provider for any OpenAI-compatible API: OpenAI, OpenRouter, Ollama, mock."""

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from tollgate.config import ProviderConfig
from tollgate.errors import TollgateError
from tollgate.providers.base import UpstreamResponse, UpstreamStream, UpstreamStreamError


class OpenAICompatProvider:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient, api_key: str | None):
        self.name = config.name
        self._config = config
        self._client = client
        self._api_key = api_key
        self._url = config.base_url.rstrip("/") + "/chat/completions"
        self._timeout = httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s)

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
        resp = await self._send(body, self._headers(client_headers), on_sent)
        try:
            if resp.status_code >= 400:
                return await self._error_response(resp)
            try:
                await resp.aread()
            except httpx.HTTPError as exc:
                raise self._transport_error(exc) from exc
            data = _json_object(resp)
            if data is None:
                raise TollgateError(
                    502,
                    f"Upstream provider {self.name!r} returned an invalid response",
                    "api_error",
                    "upstream_bad_response",
                    headers={"X-Tollgate-Provider": self.name},
                )
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
        resp = await self._send(body, headers, on_sent)
        if resp.status_code < 400:
            return _HttpxStream(resp, self.name)
        try:
            return await self._error_response(resp)
        finally:
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
            raise self._transport_error(exc) from exc
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

    def _transport_error(self, exc: httpx.HTTPError) -> TollgateError:
        headers = {"X-Tollgate-Provider": self.name}
        if isinstance(exc, httpx.TimeoutException):
            return TollgateError(
                504,
                f"Upstream provider {self.name!r} timed out",
                "api_error",
                "upstream_timeout",
                headers=headers,
            )
        return TollgateError(
            502,
            f"Upstream provider {self.name!r} is unreachable",
            "api_error",
            "upstream_unreachable",
            headers=headers,
        )


def _json_object(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class _HttpxStream:
    def __init__(self, response: httpx.Response, provider: str) -> None:
        self._response = response
        self._provider = provider

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_bytes():
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
