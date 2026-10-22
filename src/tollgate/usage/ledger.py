"""Usage events and the ledger they go to (request lifecycle step 9).

Phase 1.6 adds the batched Postgres writer behind the same Ledger protocol.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

import anyio

from tollgate.ratelimit import estimate

log = logging.getLogger(__name__)

UsageStatus = Literal["success", "provider_error", "client_disconnect", "gateway_shutdown"]


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


@dataclass(frozen=True)
class UsageMeter:
    """Builds and records the usage event for one request."""

    ledger: Ledger
    request_id: str
    started_at: float  # time.perf_counter() when the request arrived
    model: str
    provider: str
    messages: list[dict[str, Any]]

    async def record(
        self,
        status: UsageStatus,
        *,
        reported: tuple[int, int] | None = None,
        generated: str | None = None,
        first_token_at: float | None = None,
    ) -> None:
        """Record usage the provider `reported`; without it, estimate from the
        prompt and the `generated` text. Pass neither when no response came
        back from the provider, so nothing was billed."""
        estimated = reported is None and generated is not None
        if reported is not None:
            prompt, completion = reported
        elif generated is not None:
            # Off the event loop: a 1 MB prompt takes tiktoken a noticeable while.
            prompt, completion = await anyio.to_thread.run_sync(self._estimate, generated)
        else:
            prompt = completion = 0
        await self.ledger.record(
            UsageEvent(
                request_id=self.request_id,
                model=self.model,
                provider=self.provider,
                status=status,
                prompt_tokens=prompt,
                completion_tokens=completion,
                usage_estimated=estimated,
                latency_ms=_ms_since(self.started_at, time.perf_counter()),
                ttft_ms=None
                if first_token_at is None
                else _ms_since(self.started_at, first_token_at),
            )
        )

    def _estimate(self, generated: str) -> tuple[int, int]:
        return (
            estimate.estimate_prompt_tokens(self.messages, self.model),
            estimate.count_tokens(generated, self.model),
        )


def _ms_since(start: float, end: float) -> int:
    return round((end - start) * 1000)
