"""The Server-Timing header (SERVER_TIMING=true)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from mock_provider.app import create_app as create_mock_app
from tests.helpers import AUTH, TEST_PROVIDERS, MemoryLedger
from tollgate.config import Settings
from tollgate.main import create_app

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@asynccontextmanager
async def gateway(server_timing: bool) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, server_timing=server_timing),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH
        ) as client,
    ):
        yield client


def _stages(header: str) -> list[str]:
    return [item.strip().partition(";")[0] for item in header.split(",")]


@pytest.mark.parametrize(
    ("stream", "upstream"),
    [(False, ["upstream-response"]), (True, ["upstream-open", "upstream-first-chunk"])],
)
async def test_stages_reported(stream: bool, upstream: list[str]) -> None:
    async with gateway(server_timing=True) as client:
        r = await client.post("/v1/chat/completions", json={**CHAT, "stream": stream})
    assert r.status_code == 200
    assert _stages(r.headers["server-timing"]) == [
        "auth",
        "body",
        *upstream,
        "other",
        "gateway",
        "total",
    ]


async def test_off_by_default() -> None:
    async with gateway(server_timing=False) as client:
        plain = await client.post("/v1/chat/completions", json=CHAT)
        streamed = await client.post("/v1/chat/completions", json={**CHAT, "stream": True})
    assert "server-timing" not in plain.headers
    assert "server-timing" not in streamed.headers
