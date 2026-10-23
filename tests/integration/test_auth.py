"""Phase 1.4: authenticating API keys.

Acceptance:
- a revoked key is rejected within 1 s: test_revoked_key_rejected_within_1s_across_replicas
- a cached auth runs no Postgres query: test_cached_auth_runs_no_postgres_query
"""

import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import event, select

from mock_provider.app import create_app as create_mock_app
from tests.helpers import ADMIN, TEST_PEPPER, TEST_PROVIDERS, MemoryLedger, issue_key
from tollgate.auth import keys
from tollgate.auth.cache import KeyCache
from tollgate.auth.context import TenantContext
from tollgate.config import Settings
from tollgate.db.models import ApiKey
from tollgate.main import create_app

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
DEAD_REDIS = "redis://127.0.0.1:1/0"
DEAD_POSTGRES = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"


@asynccontextmanager
async def replica(**settings: Any) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """A gateway replica: its own engine and Redis client, on the suite's
    Postgres and Redis unless overridden."""
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, **settings),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield app, client


def bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


class QueryCounter:
    """Counts the SQL statements an app's engine sends to Postgres."""

    def __init__(self, app: FastAPI) -> None:
        self.count = 0
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", self._seen)

    def _seen(self, *args: Any) -> None:
        self.count += 1


def assert_error(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


# --- acceptance -------------------------------------------------------------


async def test_cached_auth_runs_no_postgres_query() -> None:
    issued = await issue_key()
    headers = bearer(issued.raw_key)
    async with replica() as (app, client):
        queries = QueryCounter(app)
        # A key's first two requests read Postgres (see auth/cache.py).
        for _ in range(2):
            assert (await client.get("/v1/models", headers=headers)).status_code == 200
        assert queries.count == 2
        for _ in range(20):
            assert (await client.get("/v1/models", headers=headers)).status_code == 200
        r = await client.post("/v1/chat/completions", json=CHAT, headers=headers)
        assert r.status_code == 200
        assert queries.count == 2  # 21 more authentications, all from the cache


async def test_revoked_key_rejected_within_1s_across_replicas() -> None:
    issued = await issue_key()
    headers = bearer(issued.raw_key)
    async with replica() as (app_a, a), replica() as (_, b):
        for _ in range(2):
            assert (await a.get("/v1/models", headers=headers)).status_code == 200
        queries = QueryCounter(app_a)
        assert (await a.get("/v1/models", headers=headers)).status_code == 200
        assert queries.count == 0  # replica A now authenticates this key from its cache

        r = await b.delete(f"/admin/keys/{issued.key.id}", headers=ADMIN)
        assert r.status_code == 204
        revoked_at = time.perf_counter()
        r = await a.get("/v1/models", headers=headers)
        rejected_after = time.perf_counter() - revoked_at

        assert_error(r, 401, "invalid_api_key")
        assert rejected_after < 1
        for _ in range(3):  # and it stays rejected
            assert_error(await a.get("/v1/models", headers=headers), 401, "invalid_api_key")


async def test_a_lookup_racing_a_revoke_cannot_refill_the_cache() -> None:
    """A lookup reads Postgres, a revoke commits and invalidates, then the
    lookup tries to cache what it read: refused, or the revoked key would keep
    working until the entry expired."""
    raw_key = keys.generate_key()
    key_hash = keys.hash_key(raw_key, TEST_PEPPER)
    context = TenantContext(
        tenant_id=str(uuid.uuid4()),
        tenant_name="t",
        api_key_id=str(uuid.uuid4()),
        key_hash=key_hash,
        rpm_limit=60,
        tpm_limit=100,
        monthly_budget_usd=None,
        allowed_models=None,
        cache_enabled=True,
    )
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        cache = KeyCache(redis)
        assert not await cache.store(context, await cache.lookup(key_hash))  # first sight
        seen = await cache.lookup(key_hash)  # a lookup misses, and goes to Postgres...
        assert (seen.context, seen.tenant_id) == (None, context.tenant_id)
        await cache.invalidate(context.tenant_id, key_hash=key_hash)  # ...a revoke lands...
        assert not await cache.store(context, seen)  # ...so what it read is not cached
        assert (await cache.lookup(key_hash)).context is None

        fresh = await cache.lookup(key_hash)  # a lookup begun after the change is cached
        assert await cache.store(context, fresh)
        assert (await cache.lookup(key_hash)).context == context
    finally:
        await redis.aclose()


# --- rejected keys -----------------------------------------------------------


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer   ", "Basic dGc6eA=="])
async def test_missing_key(header: str | None) -> None:
    async with replica() as (_, client):
        headers = {} if header is None else {"Authorization": header}
        assert_error(await client.get("/v1/models", headers=headers), 401, "missing_api_key")


@pytest.mark.parametrize(
    "raw_key",
    ["sk-1234", "tg_live_short", "tg_test_" + "a" * 43, "tg_live_" + "!" * 43],
)
async def test_malformed_key_is_rejected_without_any_lookup(raw_key: str) -> None:
    # Against a dead Redis and Postgres: a lookup would be a 503, not a 401.
    async with replica(redis_url=DEAD_REDIS, database_url=DEAD_POSTGRES) as (_, client):
        assert_error(
            await client.get("/v1/models", headers=bearer(raw_key)), 401, "invalid_api_key"
        )


async def test_unknown_key() -> None:
    async with replica() as (_, client):
        r = await client.get("/v1/models", headers=bearer(keys.generate_key()))
    assert_error(r, 401, "invalid_api_key")
    assert r.json()["error"]["message"] == "Incorrect API key provided."


async def test_chat_needs_a_key_before_its_body_is_read() -> None:
    async with replica(max_request_body_bytes=10) as (_, client):
        r = await client.post("/v1/chat/completions", json=CHAT)  # too big, and no key
    assert_error(r, 401, "missing_api_key")


# --- what a key grants ---------------------------------------------------------


async def test_allowed_models_limit_listing_and_use() -> None:
    issued = await issue_key(allowed_models=["llama3.1:8b"])
    headers = bearer(issued.raw_key)
    async with replica() as (_, client):
        listed = (await client.get("/v1/models", headers=headers)).json()
        denied = await client.post("/v1/chat/completions", json=CHAT, headers=headers)
        allowed = await client.post(
            "/v1/chat/completions", json={**CHAT, "model": "llama3.1:8b"}, headers=headers
        )
    assert [m["id"] for m in listed["data"]] == ["llama3.1:8b"]
    # The same answer as for a model that doesn't exist.
    assert_error(denied, 404, "model_not_found")
    assert allowed.status_code == 200


async def test_limit_change_reaches_a_warm_cache_at_once() -> None:
    issued = await issue_key()
    headers = bearer(issued.raw_key)
    async with replica() as (_, a), replica() as (_, b):
        for _ in range(3):
            assert (await a.post("/v1/chat/completions", json=CHAT, headers=headers)).is_success
        r = await b.patch(
            f"/admin/tenants/{issued.key.tenant_id}",
            json={"allowed_models": ["llama3.1:8b"]},
            headers=ADMIN,
        )
        assert r.status_code == 200
        assert_error(
            await a.post("/v1/chat/completions", json=CHAT, headers=headers),
            404,
            "model_not_found",
        )


async def test_usage_events_name_the_tenant_and_key() -> None:
    issued = await issue_key()
    async with replica() as (app, client):
        r = await client.post("/v1/chat/completions", json=CHAT, headers=bearer(issued.raw_key))
        assert r.status_code == 200
        (event_,) = app.state.metering.ledger.events
    assert event_.tenant_id == str(issued.key.tenant_id)
    assert event_.api_key_id == str(issued.key.id)


async def test_last_used_at_is_stamped() -> None:
    issued = await issue_key()
    assert issued.key.last_used_at is None
    async with replica() as (app, client):
        await client.get("/v1/models", headers=bearer(issued.raw_key))
        async with app.state.sessions() as session:
            key = await session.scalar(select(ApiKey).where(ApiKey.id == issued.key.id))
    assert key is not None and key.last_used_at is not None


# --- failing closed --------------------------------------------------------------


async def test_fails_closed_without_redis() -> None:
    issued = await issue_key()
    async with replica(redis_url=DEAD_REDIS, redis_timeout_s=0.2) as (_, client):
        r = await client.get("/v1/models", headers=bearer(issued.raw_key))
    assert_error(r, 503, "auth_unavailable")


async def test_fails_closed_without_postgres_on_a_cache_miss() -> None:
    issued = await issue_key()  # never seen, so not cached
    async with replica(database_url=DEAD_POSTGRES, db_command_timeout_s=0.5) as (_, client):
        r = await client.get("/v1/models", headers=bearer(issued.raw_key))
    assert_error(r, 503, "auth_unavailable")


async def test_readyz() -> None:
    async with replica() as (_, client):
        ok = await client.get("/readyz")
    async with replica(redis_url=DEAD_REDIS, redis_timeout_s=0.2) as (_, client):
        degraded = await client.get("/readyz")
    assert (ok.status_code, ok.json()) == (
        200,
        {"status": "ready", "postgres": "ok", "redis": "ok"},
    )
    assert (degraded.status_code, degraded.json()) == (
        503,
        {"status": "not_ready", "postgres": "ok", "redis": "unavailable"},
    )
