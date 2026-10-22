"""Provider for any OpenAI-compatible API: OpenAI, OpenRouter, Ollama, mock."""

from collections.abc import Mapping
from typing import Any

import httpx

from tollgate.config import ProviderConfig
from tollgate.errors import TollgateError
from tollgate.providers.base import UpstreamResponse


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
        self, body: dict[str, Any], *, client_headers: Mapping[str, str]
    ) -> UpstreamResponse:
        try:
            resp = await self._client.post(
                self._url, json=body, headers=self._headers(client_headers), timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise TollgateError(
                504, f"Upstream provider {self.name!r} timed out", "api_error", "upstream_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise TollgateError(
                502,
                f"Upstream provider {self.name!r} is unreachable",
                "api_error",
                "upstream_unreachable",
            ) from exc

        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise TollgateError(
                502,
                f"Upstream provider {self.name!r} returned an invalid response",
                "api_error",
                "upstream_bad_response",
            )
        return UpstreamResponse(resp.status_code, data, resp.headers.get("retry-after"))
