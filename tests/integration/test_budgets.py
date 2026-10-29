"""Monthly budgets at the endpoint (SPEC Phase 1.6): requests are admitted while
spend is below the budget and refused with a 402 once it isn't; the spend
counter is refilled from the ledger when missing, and fails closed without it."""

import math
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert

from mock_provider.app import create_app as create_mock_app
from tests.helpers import ADMIN, TEST_PROVIDERS, MemoryLedger, issue_key, redis_month
from tollgate.config import Settings, load_providers_file
from tollgate.db.models import UsageEventRow
from tollgate.db.session import create_engine, create_sessionmaker
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.budget import month_bounds, spend_key
from tollgate.usage.pricing import Pricing, micro_usd

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "spend some money"}]
CHAT = {"model": MODEL, "messages": MESSAGES}
OUTPUT = {"X-Mock-Output-Tokens": "1000"}
# Each request costs the same: its prompt plus 1000 completion tokens.
COST = Pricing.from_config(load_providers_file(TEST_PROVIDERS)).cost_usd(
    MODEL, estimate_prompt_tokens(MESSAGES, MODEL), 1000
)
DEAD_POSTGRES = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    yield client
    await client.aclose()


@asynccontextmanager
async def gateway(
    raw_key: str, **settings: Any
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, MemoryLedger]]:
    ledger = MemoryLedger()
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, **settings),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=ledger,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {raw_key}"},
        ) as client,
    ):
        yield app, client, ledger


async def spend_until_refused(app: FastAPI, client: httpx.AsyncClient) -> int:
    """Send requests one at a time, each settled before the next, until one is
    refused; returns how many were admitted."""
    for admitted in range(100):
        r = await client.post("/v1/chat/completions", json=CHAT, headers=OUTPUT)
        await app.state.metering.wait_idle()
        if r.status_code == 402:
            return admitted
        assert r.status_code == 200, r.text
    raise AssertionError("never refused")


async def test_requests_are_refused_once_spend_reaches_the_budget(redis: Redis) -> None:
    budget = Decimal("0.0030")
    issued = await issue_key(monthly_budget_usd=budget)
    async with gateway(issued.raw_key) as (app, client, ledger):
        admitted = await spend_until_refused(app, client)
        refused = await client.post("/v1/chat/completions", json=CHAT, headers=OUTPUT)
    # Admitted while spend < budget: the last one admitted took spend past it.
    assert admitted == math.ceil(budget / COST)
    spent = await redis.get(spend_key(str(issued.key.tenant_id), await redis_month(redis)))
    assert int(spent) == admitted * micro_usd(COST)
    assert refused.status_code == 402
    assert refused.json() == {
        "error": {
            "message": "Monthly budget of $0.003 reached. Requests are refused until the "
            "budget is raised or the month ends.",
            "type": "insufficient_quota",
            "param": None,
            "code": "budget_exceeded",
        }
    }
    assert "retry-after" not in refused.headers
    # Refused before any provider: recorded, with nothing billed.
    statuses = [e.status for e in ledger.events]
    assert statuses == ["success"] * admitted + ["budget_exceeded"] * 2
    assert (ledger.events[-1].provider, ledger.events[-1].prompt_tokens) == (None, 0)


async def test_a_budget_change_takes_effect_at_once() -> None:
    issued = await issue_key(monthly_budget_usd=Decimal("0.0010"))
    tenant = f"/admin/tenants/{issued.key.tenant_id}"
    async with gateway(issued.raw_key) as (app, client, _):
        await spend_until_refused(app, client)
        for budget in ("1.0000", None):  # raised, then unlimited
            r = await client.patch(tenant, json={"monthly_budget_usd": budget}, headers=ADMIN)
            assert r.status_code == 200, r.text
            assert (
                await client.post("/v1/chat/completions", json=CHAT, headers=OUTPUT)
            ).status_code == 200
        await client.patch(tenant, json={"monthly_budget_usd": "0.0010"}, headers=ADMIN)
        assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 402


async def test_unlimited_tenants_have_no_spend_counter(redis: Redis) -> None:
    issued = await issue_key()
    async with gateway(issued.raw_key) as (_, client, _):
        assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 200
    assert not await redis.exists(spend_key(str(issued.key.tenant_id), await redis_month(redis)))


async def test_a_missing_counter_is_refilled_from_the_ledger(redis: Redis) -> None:
    issued = await issue_key(monthly_budget_usd=Decimal("0.0030"))
    tenant_id, month = issued.key.tenant_id, await redis_month(redis)
    start, _ = month_bounds(month)
    # This month's spend, and spend that doesn't count: last month's, another tenant's.
    await insert_rows(
        (tenant_id, Decimal("0.001200"), start),
        (tenant_id, Decimal("0.001300"), start + timedelta(days=1)),
        (tenant_id, Decimal("5"), start - timedelta(seconds=1)),
        (uuid.uuid4(), Decimal("9"), start + timedelta(days=1)),
    )
    async with gateway(issued.raw_key) as (app, client, _):
        # 0.0025 spent: admitted, and its cost takes spend past 0.0030.
        first = await client.post("/v1/chat/completions", json=CHAT, headers=OUTPUT)
        await app.state.metering.wait_idle()
        second = await client.post("/v1/chat/completions", json=CHAT, headers=OUTPUT)
    assert (first.status_code, second.status_code) == (200, 402)
    spent = await redis.get(spend_key(str(tenant_id), month))
    assert int(spent) == 2_500 + micro_usd(COST)


async def test_a_missing_counter_fails_closed_without_postgres() -> None:
    issued = await issue_key(monthly_budget_usd=Decimal("1"))
    async with gateway(issued.raw_key) as (_, client, _):
        for _ in range(2):  # cache the key, so auth itself needs no Postgres
            assert (await client.get("/v1/models")).status_code == 200
    settings = {"database_url": DEAD_POSTGRES, "db_command_timeout_s": 0.5}
    async with gateway(issued.raw_key, **settings) as (_, client, ledger):
        r = await client.post("/v1/chat/completions", json=CHAT)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "auth_unavailable"
    assert ledger.events == []  # never reached a provider


async def test_the_openai_sdk_does_not_retry_a_402() -> None:
    issued = await issue_key(monthly_budget_usd=Decimal("0"))
    async with gateway(issued.raw_key) as (_, client, ledger):
        sdk = openai.AsyncOpenAI(
            base_url="http://t/v1", api_key=issued.raw_key, http_client=client, max_retries=2
        )
        with pytest.raises(openai.APIStatusError) as caught:
            await sdk.chat.completions.create(model=MODEL, messages=MESSAGES)  # type: ignore[arg-type]
    assert caught.value.status_code == 402
    assert [e.status for e in ledger.events] == ["budget_exceeded"]  # one attempt only


async def insert_rows(*rows: tuple[uuid.UUID, Decimal, Any]) -> None:
    engine = create_engine(Settings(providers_config=TEST_PROVIDERS))
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            await session.execute(
                insert(UsageEventRow).values(
                    [
                        {
                            "request_id": uuid.uuid4(),
                            "tenant_id": tenant_id,
                            "api_key_id": uuid.uuid4(),
                            "model": MODEL,
                            "status": "success",
                            "cost_usd": cost,
                            "created_at": created_at,
                        }
                        for tenant_id, cost, created_at in rows
                    ]
                )
            )
    finally:
        await engine.dispose()
