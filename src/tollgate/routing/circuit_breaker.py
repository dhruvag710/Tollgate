"""Per-provider circuit breakers, in Redis so every replica shares them (SPEC
Phase 2.1). The state machine is Lua (lua/breaker.lua), run on Redis's clock.

A request's admission call (ratelimit/limiter.py, auth/cache.py) already reads
the breakers of its model's providers, so routing costs no round trip. The
breakers are written:

- when an attempt fails: a report off the response path;
- when an attempt succeeds: counted by the settle call every request already
  makes (ratelimit/limiter.py), so success costs nothing extra;
- around a probe: claiming it is the one call a request waits for, made only
  when a provider's cooldown is over; its verdict is a report.

Reports are best effort. If Redis is unavailable a failure goes uncounted, which
at worst delays opening a breaker; the request itself still fails over.
"""

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from tollgate.config import BreakerConfig, ProviderConfig

log = logging.getLogger(__name__)

LIBRARY = (Path(__file__).parent / "lua" / "breaker.lua").read_text(encoding="utf-8")
_CLAIM = LIBRARY + "return breaker_claim(ARGV[1], ARGV[2], tonumber(ARGV[3]), breaker_now_ms())"
_FAILURE = (
    LIBRARY
    + """
return breaker_failure(ARGV[1], ARGV[2], breaker_now_ms(), tonumber(ARGV[3]),
  tonumber(ARGV[4]), tonumber(ARGV[5]), tonumber(ARGV[6]))
"""
)
_PROBE_SUCCEEDED = LIBRARY + "return breaker_probe_succeeded(ARGV[1], ARGV[2])"
_RELEASE = LIBRARY + "return breaker_release(ARGV[1], ARGV[2], breaker_now_ms())"
_STATES = (
    LIBRARY
    + """
-- ARGV: provider, window_ms, provider, window_ms, ...
local out, now = {}, breaker_now_ms()
for i = 1, #ARGV, 2 do out[#out + 1] = breaker_status(ARGV[i], now, tonumber(ARGV[i + 1])) end
return out
"""
)

ViewState = Literal["closed", "probe", "open"]
BreakerState = Literal["closed", "open", "half_open"]
Transition = Callable[[str, BreakerState], None]


@dataclass(frozen=True)
class BreakerView:
    """What a request may do with a provider, as its admission call read it:
    use it (closed), claim its probe first (probe), or skip it (open)."""

    state: ViewState
    wait_s: float = 0.0  # open: until the provider may be probed


CLOSED = BreakerView("closed")


def parse_views(flat: Sequence[Any]) -> tuple[BreakerView, ...]:
    """The admission reply's views: {state, wait_ms, state, wait_ms, ...}."""
    return tuple(
        BreakerView(cast(ViewState, flat[i]), int(flat[i + 1]) / 1000)
        for i in range(0, len(flat), 2)
    )


@dataclass(frozen=True)
class BreakerSettings:
    """One provider's breaker, in the units the scripts take."""

    window_ms: int
    min_requests: int
    failure_ratio_ppm: int
    cooldown_ms: int
    # How long a probe may take to answer before another request may claim
    # it: as long as the attempt may take to its first byte, plus a second.
    probe_lease_ms: int

    @classmethod
    def of(cls, breaker: BreakerConfig, provider: ProviderConfig) -> "BreakerSettings":
        first_byte_s = provider.first_byte_timeout_s or provider.timeout_s
        return cls(
            window_ms=math.ceil(breaker.window_s * 1000),
            min_requests=breaker.min_requests,
            failure_ratio_ppm=round(breaker.failure_ratio * 1_000_000),
            cooldown_ms=math.ceil(breaker.cooldown_s * 1000),
            probe_lease_ms=math.ceil((provider.connect_timeout_s + first_byte_s + 1) * 1000),
        )


@dataclass(frozen=True)
class BreakerSuccess:
    """A success for the settle call to count against a provider's breaker."""

    provider: str
    window_ms: int


@dataclass(frozen=True)
class BreakerStatus:
    """A provider's breaker as /admin/providers reports it."""

    provider: str
    state: BreakerState
    requests: int  # attempts over the last window (closed only)
    failures: int
    retry_in_s: float | None  # open: until it may be probed; half_open: the probe's lease


class Breakers:
    def __init__(
        self,
        redis: Redis,
        settings: Mapping[str, BreakerSettings],
        *,
        on_transition: Transition | None = None,
    ) -> None:
        self.settings = dict(settings)
        self._on_transition = on_transition
        self._claim = redis.register_script(_CLAIM)
        self._failure = redis.register_script(_FAILURE)
        self._probe_succeeded = redis.register_script(_PROBE_SUCCEEDED)
        self._release = redis.register_script(_RELEASE)
        self._states = redis.register_script(_STATES)
        self._tasks: set[asyncio.Task[None]] = set()

    async def claim(self, provider: str, request_id: str) -> bool:
        """Claim `provider`'s probe for this request. False if another request
        has it, or if Redis can't say (the provider is skipped)."""
        try:
            claimed = await self._claim(
                args=[provider, request_id, self.settings[provider].probe_lease_ms]
            )
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("could not claim a probe of %s: %s", provider, type(exc).__name__)
            return False
        if claimed:
            self._transitioned(provider, "half_open")
        return bool(claimed)

    def failed(self, provider: str, request_id: str) -> None:
        """Count a failed attempt (the probe's: a verdict), off the response path."""
        s = self.settings[provider]
        args: list[str | int] = [
            provider,
            request_id,
            s.window_ms,
            s.min_requests,
            s.failure_ratio_ppm,
            s.cooldown_ms,
        ]
        self._report(provider, "count a failure", self._failure(args=args))

    def probe_succeeded(self, provider: str, request_id: str) -> None:
        """The probe answered: close the breaker, off the response path."""
        self._report(provider, "close", self._probe_succeeded(args=[provider, request_id]))

    def release(self, provider: str, request_id: str) -> None:
        """Give up a probe that ended with no verdict, off the response path.
        The breaker goes back to open only to be probed again at once: not an
        opening, so neither logged nor counted as one."""
        self._report(
            provider,
            "release a probe",
            self._release(args=[provider, request_id]),
            transition=False,
        )

    def success(self, provider: str) -> BreakerSuccess:
        """A success for the request's settle call to count."""
        return BreakerSuccess(provider, self.settings[provider].window_ms)

    async def wait_idle(self) -> None:
        """Wait for the reports still running (shutdown)."""
        # Only the pending ones: awaiting tasks that are all done doesn't yield,
        # so their done callbacks, which take them out of the set, would never
        # run, and a loop over the set would spin forever.
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.wait(pending)

    async def states(self, providers: Iterable[str]) -> list[BreakerStatus]:
        names = list(providers)
        args = [part for name in names for part in (name, self.settings[name].window_ms)]
        replies = cast(list[list[Any]], await self._states(args=args))
        return [
            BreakerStatus(
                name,
                cast(BreakerState, state),
                int(requests),
                int(failures),
                None if state == "closed" else int(wait_ms) / 1000,
            )
            for name, (state, requests, failures, wait_ms) in zip(names, replies, strict=True)
        ]

    def _report(
        self,
        provider: str,
        what: str,
        call: Coroutine[Any, Any, Any],
        *,
        transition: bool = True,
    ) -> None:
        async def report() -> None:
            try:
                changed = await call
            except (RedisError, OSError, TimeoutError) as exc:
                log.warning("could not %s for %s: %s", what, provider, type(exc).__name__)
                return
            if changed and transition:
                self._transitioned(provider, cast(BreakerState, changed))
            elif changed:
                log.info("released the probe of provider %s", provider)

        task = asyncio.create_task(report())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _transitioned(self, provider: str, state: BreakerState) -> None:
        if state == "open":
            log.warning("circuit breaker for provider %s is open", provider)
        else:
            log.info("circuit breaker for provider %s is %s", provider, state.replace("_", "-"))
        if self._on_transition is not None:
            self._on_transition(provider, state)
