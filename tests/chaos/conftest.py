"""Chaos tests (SPEC §8: X1-X4) against the docker compose stack.

    docker compose up -d --build      # this code, with both mock providers
    pytest tests/chaos

They take the mock providers down and restart Redis, so nothing else should be
using the stack meanwhile; a plain `pytest` never collects them. The stack's
breakers run with the defaults (20 attempts, 50%, 10 s, a 30 s cooldown), so
X2 takes about half a minute. Skipped if the stack isn't up.
"""

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tollgate.db.models import UsageEventRow

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = os.environ.get("CHAOS_GATEWAY_URL", "http://localhost:8000")
PRIMARY, SECONDARY = "http://localhost:8001", "http://localhost:8002"
REDIS_URL = os.environ.get("CHAOS_REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.environ.get(
    "CHAOS_DATABASE_URL", "postgresql+asyncpg://tollgate:tollgate@localhost:5432/tollgate"
)
# The compose stack's development token, unless .env set another.
ADMIN = {"Authorization": f"Bearer {os.environ.get('ADMIN_TOKEN', 'dev-only-admin-token')}"}
TIMEOUT = httpx.Timeout(60.0)
# The stack's breakers: config/providers.test.yaml leaves the defaults.
MIN_REQUESTS, COOLDOWN_S = 20, 30


def compose(*args: str) -> str:
    done = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout


def _up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session", autouse=True)
def stack() -> None:
    down = [url for url in (GATEWAY, PRIMARY, SECONDARY) if not _up(url)]
    if down:
        pytest.skip(f"the compose stack isn't running ({down}): docker compose up -d --build")


def behave(mock: str, **knobs: Any) -> None:
    """Every request to `mock` behaves so from now on; none restores its defaults."""
    httpx.put(f"{mock}/mock/behaviour", json=knobs, timeout=TIMEOUT).raise_for_status()


def stats(mock: str) -> dict[str, Any]:
    result: dict[str, Any] = httpx.get(f"{mock}/mock/stats", timeout=TIMEOUT).json()
    return result


def close_breakers() -> None:
    client = redis.Redis.from_url(REDIS_URL)
    try:
        keys = list(client.scan_iter("tg:cb:*"))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@pytest.fixture(autouse=True)
def healthy_providers(stack: None) -> Iterator[None]:
    """Each test starts, and leaves the stack, with both mocks behaving
    normally, their counters at zero and every breaker closed."""

    def reset() -> None:
        for mock in (PRIMARY, SECONDARY):
            behave(mock)
            httpx.post(f"{mock}/mock/stats/reset", timeout=TIMEOUT).raise_for_status()
        close_breakers()

    reset()
    yield
    reset()


@dataclass
class Tenant:
    id: str
    key: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


@pytest.fixture
def tenant() -> Tenant:
    """A tenant of its own, with limits no chaos test reaches."""
    with httpx.Client(base_url=GATEWAY, headers=ADMIN, timeout=TIMEOUT) as admin:
        body = {"name": f"chaos-{uuid.uuid4()}", "rpm_limit": 1_000_000, "tpm_limit": 1_000_000_000}
        created = admin.post("/admin/tenants", json=body)
        assert created.status_code == 201, created.text
        tenant_id = created.json()["id"]
        issued = admin.post(f"/admin/tenants/{tenant_id}/keys", json={})
        assert issued.status_code == 201, issued.text
    return Tenant(tenant_id, issued.json()["key"])


def breaker(provider: str = "mock") -> dict[str, Any]:
    r = httpx.get(f"{GATEWAY}/admin/providers", headers=ADMIN, timeout=TIMEOUT)
    r.raise_for_status()
    (state,) = [p for p in r.json()["providers"] if p["name"] == provider]
    return dict(state)


@pytest.fixture
async def ledger() -> AsyncIterator[Any]:
    """`await ledger(tenant_id)`: the tenant's rows in the stack's ledger, oldest first."""
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine)

    async def rows(tenant_id: str) -> list[UsageEventRow]:
        async with sessions() as session:
            found = await session.scalars(
                select(UsageEventRow)
                .where(UsageEventRow.tenant_id == uuid.UUID(tenant_id))
                .order_by(UsageEventRow.id)
            )
            return list(found)

    try:
        yield rows
    finally:
        await engine.dispose()
