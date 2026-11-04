"""Integration and concurrency tests run against real Postgres and Redis (SPEC 8):
one pair of containers per session, migrated once, started only when a test
directory opts in (see tests/integration/conftest.py). Their URLs, the key
pepper and the admin token are exported as environment variables, so every
`Settings()`, and every gateway process a test starts, finds them."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import openai
import pytest
import redis
from alembic import command
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    AUTH,
    TEST_ADMIN_TOKEN,
    TEST_KEY,
    TEST_PEPPER,
    TEST_PROVIDERS,
    MemoryLedger,
    alembic_config,
    issue_key,
)
from tollgate.config import Settings
from tollgate.main import create_app

CHAOS = Path(__file__).parent / "chaos"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """The chaos tests run only when asked for by path (`pytest tests/chaos`):
    they take the compose stack's providers down and restart its Redis, which
    no plain `pytest` run should do to a stack someone is using."""
    if collection_path != CHAOS:
        return None
    root = config.invocation_params.dir
    asked = any((root / arg.split("::")[0]).resolve().is_relative_to(CHAOS) for arg in config.args)
    return None if asked else True


# Gateways the tests start (in-process or not) log at WARNING unless LOG_LEVEL
# says otherwise. At INFO every request adds access-log and HTTP client lines,
# and a failing test's captured output buries its failure: one C4 failure
# printed ~60,000 lines.
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def backing_services() -> Iterator[None]:
    with (
        PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as postgres,
        RedisContainer("redis:7-alpine") as redis,
    ):
        env = {
            "DATABASE_URL": postgres.get_connection_url(),
            "REDIS_URL": f"redis://{redis.get_container_host_ip()}:"
            f"{redis.get_exposed_port(6379)}/0",
            "KEY_PEPPER": TEST_PEPPER,
            "ADMIN_TOKEN": TEST_ADMIN_TOKEN,
        }
        saved = {name: os.environ.get(name) for name in env}
        os.environ.update(env)
        try:
            command.upgrade(alembic_config(), "head")
            # The tenant behind AUTH, with limits no test reaches by accident.
            asyncio.run(
                issue_key(
                    raw_key=TEST_KEY,
                    name="integration-suite",
                    rpm_limit=1_000_000,
                    tpm_limit=1_000_000_000,
                )
            )
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@pytest.fixture
def closed_breakers(backing_services: None) -> None:
    """Start with every provider's circuit breaker closed. They live in the
    session's one Redis, so one test's failures could open them for the next."""
    client = redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        keys = list(client.scan_iter("tg:cb:*"))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@pytest.fixture
def mock_app() -> FastAPI:
    return create_mock_app(env={})


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
async def gateway(mock_app: FastAPI, ledger: MemoryLedger) -> AsyncIterator[FastAPI]:
    """Gateway whose upstream calls go in-process to the mock provider."""
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS),
        upstream_transport=httpx.ASGITransport(app=mock_app),
        ledger=ledger,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(gateway: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated as the suite tenant."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://tollgate", headers=AUTH
    ) as c:
        yield c


@pytest.fixture
async def sdk(client: httpx.AsyncClient) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        base_url="http://tollgate/v1", api_key=TEST_KEY, http_client=client, max_retries=0
    )
