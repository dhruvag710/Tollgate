"""Concurrent requests overshoot a budget by at most the cost of those in flight
when it is crossed (SPEC 4): a request is admitted while spend is below the
budget, and its cost is only known once it finishes."""

import asyncio
import os
from decimal import Decimal

import httpx
from redis.asyncio import Redis

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger, issue_key, redis_month, run_server
from tollgate.config import Settings, load_providers_file
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.budget import spend_key
from tollgate.usage.pricing import Pricing, micro_usd

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "spend it all"}]
COST = micro_usd(
    Pricing.from_config(load_providers_file(TEST_PROVIDERS)).cost_usd(
        MODEL, estimate_prompt_tokens(MESSAGES, MODEL), 1000
    )
)
WORKERS = 8


async def test_concurrent_spend_overshoots_by_at_most_what_is_in_flight() -> None:
    budget = Decimal("0.0100")
    issued = await issue_key(monthly_budget_usd=budget)
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )
    # Slow enough that every worker's request is in flight when one finishes.
    headers = {
        "Authorization": f"Bearer {issued.raw_key}",
        "X-Mock-Output-Tokens": "1000",
        "X-Mock-Latency-Ms": "50",
    }
    admitted = 0
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    key = spend_key(str(issued.key.tenant_id), await redis_month(redis))
    try:
        with run_server(app) as url:
            async with httpx.AsyncClient(base_url=url, headers=headers, timeout=10) as client:

                async def worker() -> None:
                    nonlocal admitted
                    body = {"model": MODEL, "messages": MESSAGES}
                    while (r := await client.post("/v1/chat/completions", json=body)).is_success:
                        admitted += 1
                    assert r.status_code == 402, r.text

                await asyncio.gather(*(worker() for _ in range(WORKERS)))
            # Every admitted request settles its cost just after its response.
            for _ in range(100):
                if int(await redis.get(key) or 0) == admitted * COST:
                    break
                await asyncio.sleep(0.05)
        spent = int(await redis.get(key) or 0)
    finally:
        await redis.aclose()

    budget_micro = micro_usd(budget)
    assert spent == admitted * COST  # every admitted request counted, exactly once
    assert spent >= budget_micro  # nobody stopped before the budget was reached
    # Requests in flight when spend crossed the budget were admitted before it:
    # at most one per worker.
    overshoot = spent - budget_micro
    assert overshoot < WORKERS * COST, (overshoot, WORKERS * COST)
