"""Prometheus metrics at /metrics (SPEC Phase 1.7): each chat request counted
once by outcome, with bounded labels, and the histograms and gauges beside."""

from decimal import Decimal
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger, issue_key
from tollgate.config import Settings
from tollgate.main import create_app

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "measure me"}]}


def bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def samples(text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    return {
        (s.name, frozenset(s.labels.items())): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def requests(status: str, model: str = "gpt-4o-mini", provider: str = "mock") -> Any:
    labels = {"status": status, "model": model, "provider": provider, "cache": "MISS"}
    return ("tollgate_requests_total", frozenset(labels.items()))


def plain(name: str, **labels: str) -> Any:
    return (name, frozenset(labels.items()))


async def test_each_chat_request_is_counted_once_by_outcome() -> None:
    ok = await issue_key()
    limited = await issue_key(rpm_limit=1)
    broke = await issue_key(monthly_budget_usd=Decimal("0"))
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):

        async def chat(key: str | None, body: Any = CHAT, **headers: str) -> int:
            auth = bearer(key) if key else {}
            r = await client.post("/v1/chat/completions", json=body, headers={**auth, **headers})
            return r.status_code

        assert await chat(ok.raw_key) == 200
        assert await chat(ok.raw_key, {**CHAT, "stream": True}) == 200
        fail = {"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"}
        assert await chat(ok.raw_key, **fail) == 500
        assert await chat(limited.raw_key) == 200
        assert await chat(limited.raw_key) == 429
        assert await chat(broke.raw_key) == 402
        assert await chat(None) == 401
        assert await chat("tg_live_" + "x" * 43) == 401
        assert await chat(ok.raw_key, {"model": "gpt-9-made-up", "messages": CHAT["messages"]}) in (
            400,
            404,
        )
        await app.state.metering.wait_idle()
        r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    got = samples(r.text)

    assert got[requests("success")] == 3
    assert got[requests("provider_error")] == 1
    # Turned away before any provider: no provider label.
    assert got[requests("rate_limited", provider="none")] == 1
    assert got[requests("budget_exceeded", provider="none")] == 1
    # A missing key is refused before the body is read: no model either. A
    # well-formed unknown key only once the body is read and the key looked up.
    assert got[requests("missing_api_key", model="none", provider="none")] == 1
    assert got[requests("invalid_api_key", provider="none")] == 1
    # A model that isn't configured is "other", whatever the client called it.
    made_up = [k for k in got if k[0] == "tollgate_requests_total" and ("model", "other") in k[1]]
    assert len(made_up) == 1 and got[made_up[0]] == 1
    assert "gpt-9-made-up" not in r.text
    assert sum(v for k, v in got.items() if k[0] == "tollgate_requests_total") == 9

    assert got[plain("tollgate_request_duration_seconds_count")] == 9
    # Reached a provider: the two successes, the provider error, and limited's first.
    assert got[plain("tollgate_gateway_overhead_seconds_count")] == 4
    assert got[plain("tollgate_ttft_seconds_count")] == 1  # the stream
    assert got[plain("tollgate_ratelimit_rejections_total", limit="rpm")] == 1
    assert got[plain("tollgate_ledger_queue_depth")] == 0  # no Postgres ledger here


async def test_ledger_queue_depth_counts_unwritten_events() -> None:
    issued = await issue_key()
    app = create_app(
        # An interval no request outlives: events wait in the queue.
        Settings(providers_config=TEST_PROVIDERS, ledger_flush_interval_s=3600),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers=bearer(issued.raw_key),
        ) as client,
    ):
        for _ in range(3):
            assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 200
        await app.state.metering.wait_idle()
        got = samples((await client.get("/metrics")).text)
    assert got[plain("tollgate_ledger_queue_depth")] == 3
