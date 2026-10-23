"""Usage events and the ledger they go to (request lifecycle step 9).

Phase 1.6 adds the batched Postgres writer behind the same Ledger protocol.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

import anyio

from tollgate.ratelimit import estimate

log = logging.getLogger(__name__)

UsageStatus = Literal[
    "success", "provider_error", "client_disconnect", "gateway_shutdown", "gateway_error"
]


@dataclass(frozen=True)
class UsageEvent:
    request_id: str
    model: str
    provider: str
    status: UsageStatus
    prompt_tokens: int
    completion_tokens: int
    # True when the provider never reported usage and the counts are tiktoken estimates.
    usage_estimated: bool
    latency_ms: int
    ttft_ms: int | None


class Ledger(Protocol):
    async def record(self, event: UsageEvent) -> None: ...


class LogLedger:
    """Writes each event as one log line. Events never contain prompt or completion text."""

    async def record(self, event: UsageEvent) -> None:
        log.info("usage_event %s", json.dumps(asdict(event)))


def parse_usage(usage: Any) -> tuple[int, int] | None:
    """(prompt_tokens, completion_tokens) from a provider's usage object, if well-formed."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int:
        return None
    return prompt, completion


class Metering:
    """Hands out one UsageMeter per request and counts those still measuring.

    At shutdown uvicorn cancels requests that outlive its graceful timeout,
    then runs the app's lifespan shutdown without waiting for them, and asyncio
    then cancels whatever is left (no shield stops that). The lifespan waits
    for wait_idle() first, so cut-off requests still record their usage.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._open = 0  # meters measuring, or still recording their event
        self._idle = asyncio.Event()
        self._idle.set()

    def meter(
        self,
        *,
        request_id: str,
        started_at: float,
        model: str,
        provider: str,
        messages: list[dict[str, Any]],
    ) -> "UsageMeter":
        return UsageMeter(self, request_id, started_at, model, provider, messages)

    def _opened(self) -> None:
        self._open += 1
        self._idle.clear()

    def _closed(self, _: object = None) -> None:
        self._open -= 1
        if self._open == 0:
            self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()


class UsageMeter:
    """What is known about one request's usage. Wrap everything from the
    provider call on in measure(): whatever happens inside, exactly one usage
    event is recorded on the way out."""

    def __init__(
        self,
        metering: Metering,
        request_id: str,
        started_at: float,  # time.perf_counter() when the request arrived
        model: str,
        provider: str,
        messages: list[dict[str, Any]],
    ) -> None:
        self._metering = metering
        self.request_id = request_id
        self.started_at = started_at
        self.model = model
        self.provider = provider
        self.messages = messages
        self.status: UsageStatus | None = None
        # (prompt, completion) tokens the provider reported, if it did; an
        # error response reports (0, 0): nothing is billed for it.
        self.reported: tuple[int, int] | None = None
        # The request reached the provider, which may bill its prompt even if
        # no response comes back. Until then there is nothing to estimate.
        self.sent = False
        # Completion text received so far, for estimating unreported usage.
        self.generated = ""
        self.first_token_at: float | None = None
        self._measured = False

    def mark_sent(self) -> None:
        self.sent = True

    def settle(self, status: UsageStatus) -> None:
        """Decide the outcome, unless something already has: the first wins."""
        if self.status is None:
            self.status = status

    @asynccontextmanager
    async def measure(self) -> AsyncIterator[None]:
        if self._measured:
            raise RuntimeError(f"request {self.request_id} is already measured")
        self._measured = True
        self._metering._opened()
        try:
            yield
        except asyncio.CancelledError:
            # Nothing but a server shutdown cancels a request task.
            self.settle("gateway_shutdown")
            raise
        except Exception:
            self.settle("gateway_error")
            raise
        finally:
            # Written by its own task: a cancellation landing mid-write (as
            # uvicorn's shutdown can) abandons only this wait, not the write.
            recording = asyncio.create_task(self._record())
            recording.add_done_callback(self._metering._closed)
            await asyncio.shield(recording)

    async def _record(self) -> None:
        estimated = self.reported is None and self.sent
        if self.reported is not None:
            prompt, completion = self.reported
        elif estimated:
            # Off the event loop: a 1 MB prompt takes tiktoken a noticeable while.
            prompt, completion = await anyio.to_thread.run_sync(self._estimate)
        else:
            prompt = completion = 0  # never sent: nothing billed
        await self._metering.ledger.record(
            UsageEvent(
                request_id=self.request_id,
                model=self.model,
                provider=self.provider,
                # Leaving measure() without settling anything is itself a bug.
                status=self.status or "gateway_error",
                prompt_tokens=prompt,
                completion_tokens=completion,
                usage_estimated=estimated,
                latency_ms=_ms_since(self.started_at, time.perf_counter()),
                ttft_ms=None
                if self.first_token_at is None
                else _ms_since(self.started_at, self.first_token_at),
            )
        )

    def _estimate(self) -> tuple[int, int]:
        return (
            estimate.estimate_prompt_tokens(self.messages, self.model),
            estimate.count_tokens(self.generated, self.model),
        )


def _ms_since(start: float, end: float) -> int:
    return round((end - start) * 1000)
