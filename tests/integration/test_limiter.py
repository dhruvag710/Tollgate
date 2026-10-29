"""The rate-limit scripts, run directly against the suite's Redis."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis

from tollgate.ratelimit import limiter as limiter_module
from tollgate.ratelimit.limiter import LIBRARY, Decision, RateLimiter, Reservation
from tollgate.usage.budget import spend_key


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    yield client
    await client.aclose()


def a_tenant() -> str:
    return str(uuid.uuid4())


async def test_both_limits_or_neither(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    first = await limiter.check(tenant, budget_usd=None, rpm_limit=2, tpm_limit=100, reservation=60)
    assert first.allowed
    assert (first.remaining_requests, first.remaining_tokens) == (1, 40)

    denied = await limiter.check(
        tenant, budget_usd=None, rpm_limit=2, tpm_limit=100, reservation=60
    )
    assert denied.outcome == "tokens"
    # Its request token wasn't taken: the second of the two is still there.
    third = await limiter.check(tenant, budget_usd=None, rpm_limit=2, tpm_limit=100, reservation=30)
    assert third.allowed
    assert (third.remaining_requests, third.remaining_tokens) == (0, 10)


async def test_retry_after_comes_from_bucket_state(redis: Redis) -> None:
    limiter = RateLimiter(redis)
    rpm_tenant = a_tenant()
    for _ in range(2):
        assert (
            await limiter.check(
                rpm_tenant, budget_usd=None, rpm_limit=2, tpm_limit=10**6, reservation=1
            )
        ).allowed
    requests = await limiter.check(
        rpm_tenant, budget_usd=None, rpm_limit=2, tpm_limit=10**6, reservation=1
    )
    # 2 per minute: the next request token is ~30 s away.
    assert requests.outcome == "requests"
    assert 29 < requests.retry_after_s <= 30
    assert requests.headers()["Retry-After"] == "30"
    assert requests.error().code == "rpm_exceeded"

    tpm_tenant = a_tenant()
    assert (
        await limiter.check(
            tpm_tenant, budget_usd=None, rpm_limit=100, tpm_limit=60, reservation=50
        )
    ).allowed
    tokens = await limiter.check(
        tpm_tenant, budget_usd=None, rpm_limit=100, tpm_limit=60, reservation=30
    )
    # 10 left, 30 needed, at 1 token/s: ~20 s.
    assert tokens.outcome == "tokens"
    assert 19 < tokens.retry_after_s <= 20
    assert tokens.error().code == "tpm_exceeded"


async def test_a_reservation_beyond_the_limit_is_too_large(redis: Redis) -> None:
    decision = await RateLimiter(redis).check(
        a_tenant(), budget_usd=None, rpm_limit=10, tpm_limit=100, reservation=101
    )
    assert decision.outcome == "too_large"
    assert "Retry-After" not in decision.headers()  # waiting won't help
    error = decision.error()
    assert (error.status_code, error.code) == (429, "request_too_large")


async def test_refill_runs_on_redis_time(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    seconds, micros = await redis.time()
    # An empty RPM bucket as of Redis's clock: 600 rpm refills 10 tokens a second.
    await redis.hset(f"tg:rl:rpm:{tenant}", mapping={"tokens": 0, "ts": f"{seconds}.{micros:06d}"})
    assert not (
        await limiter.check(tenant, budget_usd=None, rpm_limit=600, tpm_limit=10**6, reservation=1)
    ).allowed
    await asyncio.sleep(0.5)
    later = await limiter.check(
        tenant, budget_usd=None, rpm_limit=600, tpm_limit=10**6, reservation=1
    )
    assert later.allowed
    assert later.remaining_requests >= 3  # ~5 refilled, one taken


def test_no_script_takes_the_time_from_the_app() -> None:
    assert "redis.call('TIME')" in LIBRARY
    assert "os.time" not in LIBRARY  # Lua's clock is the app host's, not Redis's
    # The only arguments are the tenant, its budget and limits, and the tokens
    # (and cost) involved.
    assert "ARGV[6]" not in limiter_module._CHECK
    assert "ARGV[5]" not in limiter_module._SETTLE


async def test_bucket_state_keeps_microseconds_and_expires(redis: Redis) -> None:
    tenant = a_tenant()
    await RateLimiter(redis).check(
        tenant, budget_usd=None, rpm_limit=10, tpm_limit=100, reservation=5
    )
    for kind in ("rpm", "tpm"):
        key = f"tg:rl:{kind}:{tenant}"
        assert len((await redis.hget(key, "ts")).split(".")[1]) == 6
        assert 119_000 < await redis.pttl(key) <= 120_000  # twice the refill window


async def test_reconcile_refunds_charges_and_caps(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    held = Reservation(tenant, tpm_limit=600, tokens=500)
    assert (
        await limiter.check(tenant, budget_usd=None, rpm_limit=10, tpm_limit=600, reservation=500)
    ).allowed
    # 100 left; 200 of the 500 were used: 300 come back.
    assert 400 <= await limiter.settle(held, used=200, cost_micro_usd=0) <= 401
    # Charging past what's left goes below zero; refill pays it back later.
    assert await limiter.settle(Reservation(tenant, 600, tokens=0), used=1000, cost_micro_usd=0) < 0
    # A refund never takes the bucket past its capacity.
    assert (
        await limiter.settle(Reservation(tenant, 600, tokens=5000), used=0, cost_micro_usd=0) == 600
    )


# --- budgets --------------------------------------------------------------------------


async def run_library(redis: Redis, call: str, *args: object) -> Any:
    """Call a library function directly, with arguments the real scripts never
    take (a timestamp, say), to check it in isolation."""
    return await redis.eval(LIBRARY + f"\nreturn {call}", 0, *args)


@pytest.mark.parametrize(
    ("moment", "month"),
    [
        ("1970-01-01T00:00:00", "197001"),
        ("2024-02-29T23:59:59", "202402"),  # leap day
        ("2024-03-01T00:00:00", "202403"),
        ("2026-12-31T23:59:59", "202612"),
        ("2027-01-01T00:00:00", "202701"),
        ("2100-02-28T12:00:00", "210002"),  # not a leap year
        ("2100-03-01T00:00:00", "210003"),
    ],
)
async def test_the_month_is_utc(redis: Redis, moment: str, month: str) -> None:
    seconds = datetime.fromisoformat(moment).replace(tzinfo=UTC).timestamp()
    assert await run_library(redis, "utc_month(tonumber(ARGV[1]))", seconds) == month


async def test_the_scripts_month_is_redis_time(redis: Redis) -> None:
    seconds, _ = await redis.time()
    decision = await RateLimiter(redis).check(
        a_tenant(), budget_usd=None, rpm_limit=10, tpm_limit=100, reservation=1
    )
    assert decision.month == datetime.fromtimestamp(seconds, UTC).strftime("%Y%m")


@pytest.mark.parametrize(
    ("budget", "micro"),
    [("12.3456", 12_345_600), ("10", 10_000_000), ("0.0001", 100), ("0.0000", 0)],
)
async def test_budgets_are_read_as_exact_micro_dollars(
    redis: Redis, budget: str, micro: int
) -> None:
    assert await run_library(redis, "micro_usd(ARGV[1])", budget) == micro


async def test_admission_checks_the_budget_first(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    budget = Decimal("1.0000")

    async def check() -> Decision:
        return await limiter.check(
            tenant, budget_usd=budget, rpm_limit=10, tpm_limit=1000, reservation=10
        )

    unknown = await check()
    assert unknown.outcome == "spend_unknown"  # the caller refills the counter
    key = spend_key(tenant, unknown.month)
    await redis.set(key, 999_999)  # a micro-dollar short
    assert (await check()).allowed
    await redis.set(key, 1_000_000)
    over = await check()
    assert over.outcome == "budget"
    error = over.error()
    assert (error.status_code, error.type, error.code) == (
        402,
        "insufficient_quota",
        "budget_exceeded",
    )
    assert "$1 " in error.message
    # Neither refusal took anything from the rate limits: one request so far.
    assert (await redis.hget(f"tg:rl:rpm:{tenant}", "tokens")) == "9.000000"


async def test_unlimited_tenants_have_no_spend_counter(redis: Redis) -> None:
    tenant = a_tenant()
    decision = await RateLimiter(redis).check(
        tenant, budget_usd=None, rpm_limit=10, tpm_limit=1000, reservation=10
    )
    assert decision.allowed
    assert not await redis.exists(spend_key(tenant, decision.month))


async def test_settle_adds_spend_only_to_a_counter_that_exists(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    held = Reservation(tenant, tpm_limit=1000, tokens=10)
    month = (
        await limiter.check(tenant, budget_usd=None, rpm_limit=10, tpm_limit=1000, reservation=10)
    ).month
    key = spend_key(tenant, month)
    await limiter.settle(held, used=10, cost_micro_usd=500)
    # Left for a refill from the ledger, which SET NX would otherwise skip.
    assert not await redis.exists(key)
    await redis.set(key, 1_000)
    await limiter.settle(held, used=10, cost_micro_usd=500)
    await limiter.settle(held, used=10, cost_micro_usd=0)
    assert await redis.get(key) == "1500"
