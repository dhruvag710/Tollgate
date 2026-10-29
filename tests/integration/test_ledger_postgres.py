"""The usage ledger in Postgres (SPEC Phase 1.6): what each request's row
holds, batched writes flushed at shutdown, idempotent inserts, the schema's
statuses, and the spend counter agreeing with the ledger."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, issue_key, ledger_rows, redis_month
from tollgate.config import Settings, load_providers_file
from tollgate.db.session import create_engine, create_sessionmaker
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.budget import spend_key
from tollgate.usage.ledger import UsageEvent, postgres_writer
from tollgate.usage.pricing import Pricing, micro_usd

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "what is a ledger"}]
PROMPT = estimate_prompt_tokens(MESSAGES, MODEL)
PRICING = Pricing.from_config(load_providers_file(TEST_PROVIDERS))
CHAT = {"model": MODEL, "messages": MESSAGES}
TEN = {"X-Mock-Output-Tokens": "10"}


@asynccontextmanager
async def postgres_gateway(
    settings: dict[str, Any] | None = None, **tenant: Any
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, uuid.UUID]]:
    """A gateway writing to the Postgres ledger, and a client for a new tenant.
    Leaving it shuts the gateway down, which flushes the ledger."""
    issued = await issue_key(**tenant)
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, **(settings or {})),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {issued.raw_key}"},
        ) as client,
    ):
        yield app, client, issued.key.tenant_id


async def test_each_request_is_one_row_with_its_cost() -> None:
    async with postgres_gateway() as (_, client, tenant_id):
        plain = await client.post("/v1/chat/completions", json=CHAT, headers=TEN)
        streamed = await client.post(
            "/v1/chat/completions", json={**CHAT, "stream": True}, headers=TEN
        )
        failed = await client.post(
            "/v1/chat/completions",
            json=CHAT,
            headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"},
        )
    assert (plain.status_code, streamed.status_code, failed.status_code) == (200, 200, 500)
    rows = await ledger_rows(tenant_id)
    assert [str(row.request_id) for row in rows] == [
        r.headers["x-request-id"] for r in (plain, streamed, failed)
    ]
    success, stream, error = rows
    assert (success.provider, success.model, success.status) == ("mock", MODEL, "success")
    assert (success.prompt_tokens, success.completion_tokens) == (PROMPT, 10)
    assert success.cost_usd == PRICING.cost_usd(MODEL, PROMPT, 10)
    assert not success.usage_estimated
    assert (success.cache_status, success.attempts, success.saved_cost_usd) == ("MISS", 1, 0)
    assert success.latency_ms is not None and success.ttft_ms is None
    assert stream.status == "success" and stream.ttft_ms is not None
    assert (stream.prompt_tokens, stream.completion_tokens) == (PROMPT, 10)
    # An error response bills nothing.
    assert (error.status, error.prompt_tokens, error.completion_tokens) == ("provider_error", 0, 0)
    assert error.cost_usd == 0


async def test_rejected_requests_are_rows_with_nothing_billed() -> None:
    budget = {"monthly_budget_usd": Decimal("0.0000"), "rpm_limit": 1}
    async with postgres_gateway(**budget) as (_, client, tenant_id):
        over_budget = await client.post("/v1/chat/completions", json=CHAT)
    assert over_budget.status_code == 402
    async with postgres_gateway(rpm_limit=1) as (_, client, limited_id):
        await client.post("/v1/chat/completions", json=CHAT)
        limited = await client.post("/v1/chat/completions", json=CHAT)
    assert limited.status_code == 429
    for rows, status in (
        (await ledger_rows(tenant_id), "budget_exceeded"),
        ((await ledger_rows(limited_id))[1:], "rate_limited"),
    ):
        (row,) = rows
        assert (row.status, row.provider, row.prompt_tokens, row.completion_tokens) == (
            status,
            None,
            0,
            0,
        )
        assert row.cost_usd == 0


async def test_shutdown_writes_the_queued_events() -> None:
    # An interval no request outlives: only the shutdown flush writes these.
    settings = {"ledger_flush_interval_s": 3600}
    async with postgres_gateway(settings) as (app, client, tenant_id):
        for _ in range(5):
            assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 200
        await app.state.metering.wait_idle()
        assert await ledger_rows(tenant_id) == []
        assert app.state.metering.ledger.pending == 5
    assert len(await ledger_rows(tenant_id)) == 5


def an_event(tenant_id: uuid.UUID, request_id: str, status: Any = "success") -> UsageEvent:
    return UsageEvent(
        request_id=request_id,
        tenant_id=str(tenant_id),
        api_key_id=str(uuid.uuid4()),
        model=MODEL,
        provider="mock",
        status=status,
        prompt_tokens=3,
        completion_tokens=4,
        usage_estimated=False,
        latency_ms=12,
        ttft_ms=None,
        cost_usd=Decimal("0.000003"),
    )


@pytest.fixture
async def write() -> AsyncIterator[Any]:
    engine = create_engine(Settings(providers_config=TEST_PROVIDERS))
    yield postgres_writer(create_sessionmaker(engine))
    await engine.dispose()


async def test_an_event_written_twice_is_one_row(write: Any) -> None:
    tenant_id, request_id = uuid.uuid4(), str(uuid.uuid4())
    await write([an_event(tenant_id, request_id)])
    # A retried batch: the event already in the ledger is skipped, the new one written.
    await write([an_event(tenant_id, request_id), an_event(tenant_id, str(uuid.uuid4()))])
    rows = await ledger_rows(tenant_id)
    assert len(rows) == 2
    assert str(rows[0].request_id) == request_id


async def test_the_schema_refuses_an_unknown_status(write: Any) -> None:
    with pytest.raises(IntegrityError, match="ck_usage_events_status"):
        await write([an_event(uuid.uuid4(), str(uuid.uuid4()), status="lost")])


async def test_spend_counter_matches_the_ledger() -> None:
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        async with postgres_gateway(monthly_budget_usd=Decimal("100")) as (_, client, tenant_id):
            for tokens in (5, 50, 500, 1000):
                r = await client.post(
                    "/v1/chat/completions",
                    json=CHAT,
                    headers={"X-Mock-Output-Tokens": str(tokens)},
                )
                assert r.status_code == 200
        spent = await redis.get(spend_key(str(tenant_id), await redis_month(redis)))
    finally:
        await redis.aclose()
    rows = await ledger_rows(tenant_id)
    assert len(rows) == 4
    assert int(spent) == micro_usd(sum((row.cost_usd for row in rows), Decimal(0))) > 0
