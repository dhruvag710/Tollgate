"""Provider protocol: what the router needs from an upstream LLM API."""

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tollgate.errors import TollgateError


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    body: dict[str, Any]
    retry_after: str | None = None


UpstreamFailure = Literal["unreachable", "timeout", "dropped", "bad_response"]

_FAILURES: dict[UpstreamFailure, tuple[int, str, str]] = {
    "unreachable": (502, "upstream_unreachable", "is unreachable"),
    "timeout": (504, "upstream_timeout", "timed out"),
    "dropped": (502, "upstream_stream_error", "dropped the connection"),
    "bad_response": (502, "upstream_bad_response", "returned an invalid response"),
}


class UpstreamError(TollgateError):
    """No usable response came from a provider.

    `kind` says what failed: the provider couldn't be reached or the connection
    was lost before it answered (unreachable), it was too slow (timeout), a
    stream's connection was lost after its headers but before any of its body
    (dropped), or it answered success with a body that isn't one
    (bad_response). `first_byte` says whether the provider had already started
    its response: a failure after that is never retried on another provider,
    since the provider may bill a completion it has started (SPEC Phase 2.1).
    """

    def __init__(
        self,
        provider: str,
        kind: UpstreamFailure,
        *,
        first_byte: bool,
        what: str | None = None,  # instead of the kind's own wording
    ) -> None:
        status, code, default = _FAILURES[kind]
        super().__init__(
            status,
            f"Upstream provider {provider!r} {what or default}",
            "api_error",
            code,
            headers={"X-Tollgate-Provider": provider},
        )
        self.provider = provider
        self.kind = kind
        self.first_byte = first_byte


class UpstreamStreamError(Exception):
    """The upstream connection failed while a streamed response was being read."""

    def __init__(self, message: str, *, timeout: bool) -> None:
        super().__init__(message)
        self.timeout = timeout


class UpstreamStream(Protocol):
    """An open streaming response, its first chunk already received. The
    caller must aclose() it."""

    # Of the time open_stream took, how much was spent waiting for the first
    # chunk once the response's headers had arrived (Server-Timing reports it
    # as its own stage).
    first_chunk_s: float

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Body bytes as they arrive, the first chunk first. Raises
        UpstreamStreamError on failure."""
        ...

    async def aclose(self) -> None:
        """Close the response; if it is unfinished, this cancels the upstream request."""
        ...


class Provider(Protocol):
    """Both calls take `on_sent`, called once the request has been sent to the
    provider, which may then bill its prompt even if no response comes back.
    Both raise UpstreamError, never anything else, when no usable response
    comes back."""

    name: str

    async def chat_completion(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> UpstreamResponse:
        """Send a non-streaming chat completion.

        Returns the upstream response for any HTTP status; an error status
        comes back even if its body is unreadable. Raises UpstreamError when no
        usable response was received (connect error, timeout, a success status
        with a non-JSON body).
        """
        ...

    async def open_stream(
        self,
        body: dict[str, Any],
        *,
        client_headers: Mapping[str, str],
        on_sent: Callable[[], None],
    ) -> UpstreamResponse | UpstreamStream:
        """Start a streaming chat completion, up to its first chunk.

        Returns an UpstreamResponse (already read and closed) if the upstream
        answered with an error status, otherwise the open stream. Raises
        UpstreamError, with first_byte False, if no chunk arrives.
        """
        ...
