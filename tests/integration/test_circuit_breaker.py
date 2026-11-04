"""Circuit breaker state transitions with time mocked (SPEC §8, Phase 2.1).

The state machine is the Lua library in routing/lua/breaker.lua, which takes
the time as an argument; Tollgate's scripts always pass Redis server time.
These tests compose the same library with entry points that take the time
from the test instead, so every transition is checked at exact instants.
"""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

from tollgate.auth import cache
from tollgate.ratelimit import limiter
from tollgate.routing import circuit_breaker
from tollgate.routing.circuit_breaker import LIBRARY

WINDOW, MIN_REQUESTS, RATIO_PPM, COOLDOWN, LEASE = 10_000, 20, 500_000, 30_000, 5_000
T0 = 1_800_000_000_000  # ms

_CLOCKED = (
    LIBRARY
    + """
local op, provider, now, id = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4]
local window, min, ratio, cooldown, lease = tonumber(ARGV[5]), tonumber(ARGV[6]),
  tonumber(ARGV[7]), tonumber(ARGV[8]), tonumber(ARGV[9])
if op == 'view' then return breaker_view(provider, now) end
if op == 'claim' then return breaker_claim(provider, id, lease, now) end
if op == 'success' then return breaker_success(provider, now, window) end
if op == 'failure' then return breaker_failure(provider, id, now, window, min, ratio, cooldown) end
if op == 'probe_succeeded' then return breaker_probe_succeeded(provider, id) end
if op == 'release' then return breaker_release(provider, id, now) end
if op == 'status' then return breaker_status(provider, now, window) end
"""
)


class Breaker:
    """One provider's breaker, driven at times the test chooses."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.provider = f"test-{uuid.uuid4()}"
        self._script = redis.register_script(_CLOCKED)

    async def _call(self, op: str, now: int, request: str = "") -> Any:
        return await self._script(
            args=[op, self.provider, now, request, WINDOW, MIN_REQUESTS, RATIO_PPM, COOLDOWN, LEASE]
        )

    async def view(self, now: int) -> tuple[str, int]:
        state, wait = await self._call("view", now)
        return state, int(wait)

    async def failure(self, now: int, request: str = "r") -> str:
        result: str = await self._call("failure", now, request)
        return result

    async def failures(self, n: int, now: int) -> list[str]:
        return [await self.failure(now) for _ in range(n)]

    async def successes(self, n: int, now: int) -> None:
        for _ in range(n):
            await self._call("success", now)

    async def claim(self, now: int, request: str) -> bool:
        return bool(await self._call("claim", now, request))

    async def probe_succeeded(self, request: str) -> str:
        result: str = await self._call("probe_succeeded", 0, request)
        return result

    async def release(self, now: int, request: str) -> str:
        result: str = await self._call("release", now, request)
        return result

    async def status(self, now: int) -> list[Any]:
        result: list[Any] = await self._call("status", now)
        return result

    async def fields(self) -> dict[str, str]:
        return await self.redis.hgetall(f"tg:cb:{self.provider}")


@pytest.fixture
async def breaker() -> AsyncIterator[Breaker]:
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        yield Breaker(redis)
    finally:
        await redis.aclose()


def test_tollgates_breaker_scripts_run_on_redis_time() -> None:
    """The transitions take `now` as an argument only so these tests can pick
    it: every script Tollgate runs hands them Redis's own clock."""
    assert LIBRARY.count("redis.call('TIME')") == 1 and "os.time" not in LIBRARY
    entries = {
        name: script.removeprefix(prefix)
        for name, script, prefix in [
            ("claim", circuit_breaker._CLAIM, LIBRARY),
            ("failure", circuit_breaker._FAILURE, LIBRARY),
            ("release", circuit_breaker._RELEASE, LIBRARY),
            ("states", circuit_breaker._STATES, LIBRARY),
            ("admission", limiter._CHECK, limiter.LIBRARY),
            ("lookup", cache._LOOKUP, limiter.LIBRARY),
        ]
    }
    # The states script reads the clock once, into `now`, for every provider.
    states = entries.pop("states")
    assert "local out, now = {}, breaker_now_ms()" in states
    assert states.count("now") == states.count("breaker_now_ms()") + 2  # assigned, then used
    for name, entry in entries.items():
        assert "breaker_now_ms()" in entry, name
        assert "now" not in entry.replace("breaker_now_ms()", ""), name  # no other time
    assert "now" not in circuit_breaker._PROBE_SUCCEEDED.removeprefix(LIBRARY)


async def test_a_new_breaker_is_closed(breaker: Breaker) -> None:
    assert await breaker.view(T0) == ("closed", 0)
    assert await breaker.status(T0) == ["closed", 0, 0, 0]


async def test_it_opens_once_the_ratio_is_reached_over_the_minimum_volume(
    breaker: Breaker,
) -> None:
    # 19 failures out of 19 is 100%, but below the minimum volume.
    assert await breaker.failures(MIN_REQUESTS - 1, T0) == [""] * (MIN_REQUESTS - 1)
    assert await breaker.view(T0) == ("closed", 0)
    assert await breaker.status(T0) == ["closed", 19, 19, 0]
    assert await breaker.failure(T0) == "open"
    assert await breaker.view(T0 + 1) == ("open", COOLDOWN - 1)


async def test_successes_hold_the_ratio_down(breaker: Breaker) -> None:
    await breaker.successes(30, T0)
    # 29 failures of 59 attempts is under 50%; the 30th makes exactly 50%.
    assert await breaker.failures(29, T0) == [""] * 29
    assert await breaker.view(T0) == ("closed", 0)
    assert await breaker.failure(T0) == "open"


async def test_a_steady_failure_rate_under_the_ratio_never_opens(breaker: Breaker) -> None:
    for i in range(300):  # one in three fails
        if i % 3 == 2:
            assert await breaker.failure(T0 + i * 100) == ""
        else:
            await breaker.successes(1, T0 + i * 100)
    assert (await breaker.view(T0 + 30_000))[0] == "closed"


async def test_a_new_window_does_not_decide_on_its_first_attempts(breaker: Breaker) -> None:
    await breaker.successes(100, T0)
    # Just into the next window: the 100 successes still weigh almost fully,
    # so 20 failures, 100% of what the new window has seen, don't open it.
    assert await breaker.failures(20, T0 + WINDOW + 1) == [""] * 20
    assert await breaker.status(T0 + WINDOW + 1) == ["closed", 120, 20, 0]
    # Halfway through it, half the old window still counts.
    assert await breaker.status(T0 + WINDOW + WINDOW // 2) == ["closed", 70, 20, 0]
    # A window later the successes are gone, and the failures, now the window
    # before, still weigh almost fully: one more opens it.
    assert await breaker.failure(T0 + 2 * WINDOW + 1) == "open"


async def test_attempts_older_than_two_windows_are_forgotten(breaker: Breaker) -> None:
    await breaker.successes(100, T0)
    later = T0 + 2 * WINDOW
    assert await breaker.status(later) == ["closed", 0, 0, 0]
    assert await breaker.failures(MIN_REQUESTS - 1, later) == [""] * (MIN_REQUESTS - 1)
    assert await breaker.failure(later) == "open"


async def opened(breaker: Breaker) -> None:
    assert (await breaker.failures(MIN_REQUESTS, T0))[-1] == "open"


async def test_after_the_cooldown_one_request_may_probe(breaker: Breaker) -> None:
    await opened(breaker)
    assert await breaker.view(T0 + COOLDOWN - 1) == ("open", 1)
    assert not await breaker.claim(T0 + COOLDOWN - 1, "early")
    assert await breaker.view(T0 + COOLDOWN) == ("probe", 0)
    assert await breaker.claim(T0 + COOLDOWN, "first")
    assert not await breaker.claim(T0 + COOLDOWN, "second")  # one probe at a time
    # Everyone else skips it meanwhile, told to come back soon, not after the lease.
    assert await breaker.view(T0 + COOLDOWN + 1) == ("open", 1000)
    assert (await breaker.fields())["state"] == "half_open"
    assert await breaker.status(T0 + COOLDOWN + 1) == ["half_open", 0, 0, LEASE - 1]


async def test_the_probes_success_closes_it_with_a_fresh_window(breaker: Breaker) -> None:
    await opened(breaker)
    assert await breaker.claim(T0 + COOLDOWN, "probe")
    assert await breaker.probe_succeeded("someone-else") == ""
    assert await breaker.probe_succeeded("probe") == "closed"
    assert await breaker.fields() == {}
    assert await breaker.view(T0 + COOLDOWN) == ("closed", 0)
    # A fresh window: it takes the minimum volume again to open.
    assert await breaker.failures(MIN_REQUESTS - 1, T0 + COOLDOWN) == [""] * (MIN_REQUESTS - 1)


async def test_the_probes_failure_opens_it_again(breaker: Breaker) -> None:
    await opened(breaker)
    probe_at = T0 + COOLDOWN + 500
    assert await breaker.claim(probe_at, "probe")
    assert await breaker.failure(probe_at + 200, "probe") == "open"
    # A whole new cooldown, from the probe's failure.
    assert await breaker.view(probe_at + 200) == ("open", COOLDOWN)


async def test_a_probe_that_never_reports_can_be_claimed_again(breaker: Breaker) -> None:
    await opened(breaker)
    assert await breaker.claim(T0 + COOLDOWN, "lost")
    assert not await breaker.claim(T0 + COOLDOWN + LEASE - 1, "too-soon")
    assert await breaker.claim(T0 + COOLDOWN + LEASE, "next")
    # The first probe's verdict, if it ever comes, no longer counts.
    assert await breaker.probe_succeeded("lost") == ""
    assert await breaker.failure(T0 + COOLDOWN + LEASE, "lost") == ""
    assert (await breaker.fields())["probe"] == "next"


async def test_a_released_probe_may_be_claimed_at_once(breaker: Breaker) -> None:
    await opened(breaker)
    assert await breaker.claim(T0 + COOLDOWN, "left")
    assert await breaker.release(T0 + COOLDOWN + 10, "someone-else") == ""
    assert await breaker.release(T0 + COOLDOWN + 10, "left") == "open"
    assert await breaker.view(T0 + COOLDOWN + 10) == ("probe", 0)
    assert await breaker.claim(T0 + COOLDOWN + 10, "next")


async def test_reports_from_before_it_opened_are_ignored(breaker: Breaker) -> None:
    await opened(breaker)
    before = await breaker.fields()
    await breaker.successes(50, T0 + 1)  # requests already in flight when it opened
    assert await breaker.failure(T0 + 1, "straggler") == ""
    assert await breaker.fields() == before
    assert await breaker.claim(T0 + COOLDOWN, "probe")
    half_open = await breaker.fields()
    await breaker.successes(5, T0 + COOLDOWN + 1)
    assert await breaker.failure(T0 + COOLDOWN + 1, "straggler") == ""
    assert await breaker.fields() == half_open
