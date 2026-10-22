from collections.abc import AsyncIterator

import httpx
import openai
import pytest
from fastapi import FastAPI

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger
from tollgate.config import Settings
from tollgate.main import create_app


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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://tollgate"
    ) as c:
        yield c


@pytest.fixture
async def sdk(client: httpx.AsyncClient) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        base_url="http://tollgate/v1", api_key="tg_test", http_client=client, max_retries=0
    )
