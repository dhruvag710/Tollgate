"""The gateway opens its Redis connections at startup, and starts anyway if
Redis is down."""

import logging
from typing import Any

import httpx
import pytest

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger
from tollgate.config import Settings
from tollgate.main import create_app


def gateway(**settings: Any) -> Any:
    return create_app(
        Settings(providers_config=TEST_PROVIDERS, **settings),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )


def idle_connections(app: Any) -> int:
    """Open connections waiting in the pool (a failed connect leaves one unopened)."""
    pool = app.state.redis.connection_pool
    return sum(conn.is_connected for conn in pool._available_connections)


async def test_startup_opens_the_pool() -> None:
    app = gateway(redis_max_connections=40, redis_min_connections=25)
    async with app.router.lifespan_context(app):
        assert idle_connections(app) == 25


async def test_no_more_than_the_pool_holds() -> None:
    app = gateway(redis_max_connections=10, redis_min_connections=50)
    async with app.router.lifespan_context(app):
        assert idle_connections(app) == 10


async def test_starts_without_redis(caplog: pytest.LogCaptureFixture) -> None:
    app = gateway(redis_url="redis://127.0.0.1:1/0", redis_timeout_s=0.2, redis_min_connections=5)
    with caplog.at_level(logging.WARNING, logger="tollgate.redis_pool"):
        async with app.router.lifespan_context(app):
            assert idle_connections(app) == 0
    assert "opened 0 of 5 Redis connections at startup" in caplog.text
