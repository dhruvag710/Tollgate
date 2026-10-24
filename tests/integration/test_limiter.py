"""The rate-limit scripts, run directly against the suite's Redis."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from tollgate.ratelimit import limiter as limiter_module
from tollgate.ratelimit.limiter import BUCKET_LIBRARY, RateLimiter, Reservation


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    yield client
    await client.aclose()


def a_tenant() -> str:
    return str(uuid.uuid4())


async def test_both_limits_or_neither(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    first = await limiter.check(tenant, rpm_limit=2, tpm_limit=100, reservation=60)
    assert first.allowed
    assert (first.remaining_requests, first.remaining_tokens) == (1, 40)

    denied = await limiter.check(tenant, rpm_limit=2, tpm_limit=100, reservation=60)
    assert denied.outcome == "tokens"
    # Its request token wasn't taken: the second of the two is still there.
    third = await limiter.check(tenant, rpm_limit=2, tpm_limit=100, reservation=30)
    assert third.allowed
    assert (third.remaining_requests, third.remaining_tokens) == (0, 10)


async def test_retry_after_comes_from_bucket_state(redis: Redis) -> None:
    limiter = RateLimiter(redis)
    rpm_tenant = a_tenant()
    for _ in range(2):
        assert (
            await limiter.check(rpm_tenant, rpm_limit=2, tpm_limit=10**6, reservation=1)
        ).allowed
    requests = await limiter.check(rpm_tenant, rpm_limit=2, tpm_limit=10**6, reservation=1)
    # 2 per minute: the next request token is ~30 s away.
    assert requests.outcome == "requests"
    assert 29 < requests.retry_after_s <= 30
    assert requests.headers()["Retry-After"] == "30"
    assert requests.error().code == "rpm_exceeded"

    tpm_tenant = a_tenant()
    assert (await limiter.check(tpm_tenant, rpm_limit=100, tpm_limit=60, reservation=50)).allowed
    tokens = await limiter.check(tpm_tenant, rpm_limit=100, tpm_limit=60, reservation=30)
    # 10 left, 30 needed, at 1 token/s: ~20 s.
    assert tokens.outcome == "tokens"
    assert 19 < tokens.retry_after_s <= 20
    assert tokens.error().code == "tpm_exceeded"


async def test_a_reservation_beyond_the_limit_is_too_large(redis: Redis) -> None:
    decision = await RateLimiter(redis).check(
        a_tenant(), rpm_limit=10, tpm_limit=100, reservation=101
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
    assert not (await limiter.check(tenant, rpm_limit=600, tpm_limit=10**6, reservation=1)).allowed
    await asyncio.sleep(0.5)
    later = await limiter.check(tenant, rpm_limit=600, tpm_limit=10**6, reservation=1)
    assert later.allowed
    assert later.remaining_requests >= 3  # ~5 refilled, one taken


def test_no_script_takes_the_time_from_the_app() -> None:
    assert "redis.call('TIME')" in BUCKET_LIBRARY
    assert "os.time" not in BUCKET_LIBRARY  # Lua's clock is the app host's, not Redis's
    # The only arguments are the tenant, its limits and the tokens involved.
    assert "ARGV[5]" not in limiter_module._CHECK
    assert "ARGV[4]" not in limiter_module._RECONCILE


async def test_bucket_state_keeps_microseconds_and_expires(redis: Redis) -> None:
    tenant = a_tenant()
    await RateLimiter(redis).check(tenant, rpm_limit=10, tpm_limit=100, reservation=5)
    for kind in ("rpm", "tpm"):
        key = f"tg:rl:{kind}:{tenant}"
        assert len((await redis.hget(key, "ts")).split(".")[1]) == 6
        assert 119_000 < await redis.pttl(key) <= 120_000  # twice the refill window


async def test_reconcile_refunds_charges_and_caps(redis: Redis) -> None:
    limiter, tenant = RateLimiter(redis), a_tenant()
    held = Reservation(tenant, tpm_limit=600, tokens=500)
    assert (await limiter.check(tenant, rpm_limit=10, tpm_limit=600, reservation=500)).allowed
    # 100 left; 200 of the 500 were used: 300 come back.
    assert 400 <= await limiter.reconcile(held, used=200) <= 401
    # Charging past what's left goes below zero; refill pays it back later.
    assert await limiter.reconcile(Reservation(tenant, 600, tokens=0), used=1000) < 0
    # A refund never takes the bucket past its capacity.
    assert await limiter.reconcile(Reservation(tenant, 600, tokens=5000), used=0) == 600
