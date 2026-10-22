"""Provider protocol: what the router needs from an upstream LLM API."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    body: dict[str, Any]
    retry_after: str | None = None


class UpstreamStreamError(Exception):
    """The upstream connection failed while a streamed response was being read."""

    def __init__(self, message: str, *, timeout: bool) -> None:
        super().__init__(message)
        self.timeout = timeout


class UpstreamStream(Protocol):
    """An open streaming response. The caller must aclose() it."""

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Body bytes as they arrive. Raises UpstreamStreamError on failure."""
        ...

    async def aclose(self) -> None:
        """Close the response; if it is unfinished, this cancels the upstream request."""
        ...


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

    async def open_stream(
        self, body: dict[str, Any], *, client_headers: Mapping[str, str]
    ) -> UpstreamResponse | UpstreamStream:
        """Start a streaming chat completion.

        Returns an UpstreamResponse (already read and closed) if the upstream
        answered with an error status, otherwise the open stream. Raises
        TollgateError like chat_completion.
        """
        ...
