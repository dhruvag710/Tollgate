"""Provider protocol: what the router needs from an upstream LLM API."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    body: dict[str, Any]
    retry_after: str | None = None


class Provider(Protocol):
    name: str

    async def chat_completion(
        self, body: dict[str, Any], *, client_headers: Mapping[str, str]
    ) -> UpstreamResponse:
        """Send a non-streaming chat completion.

        Returns the upstream response for any HTTP status. Raises TollgateError
        only when no usable response was received (connect error, timeout,
        non-JSON body).
        """
        ...
