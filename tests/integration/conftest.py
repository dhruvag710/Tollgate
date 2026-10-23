"""Integration tests run against real Postgres and Redis (SPEC 8), one pair of
containers per session, migrated once. Their URLs, the key pepper and the admin
token are exported as environment variables, so every `Settings()`, and every
gateway process a test starts, finds them."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import openai
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    AUTH,
    ROOT,
    TEST_ADMIN_TOKEN,
    TEST_KEY,
    TEST_PEPPER,
    TEST_PROVIDERS,
    MemoryLedger,
    issue_key,
)
from tollgate.config import Settings
from tollgate.main import create_app


@pytest.fixture(scope="session", autouse=True)
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
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
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
