"""Rate limiting at the endpoint (SPEC Phase 1.5): headers, 429s, TPM
reservations and their reconciliation, and the single Redis round trip that
authenticates and rate limits a request whose key is cached."""

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.asyncio.connection import AbstractConnection

from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger, issue_key
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "count to three"}]
PROMPT = estimate_prompt_tokens(MESSAGES, MODEL)
DEAD_REDIS = "redis://127.0.0.1:1/0"


def chat(max_tokens: int | None = None, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": MODEL, "messages": MESSAGES, **fields}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


@asynccontextmanager
async def tenant_gateway(
    settings: dict[str, Any] | None = None, **tenant: Any
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, MemoryLedger]]:
    """A gateway, and a client holding the key of a new tenant with these fields."""
    issued = await issue_key(**tenant)
    ledger = MemoryLedger()
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, **(settings or {})),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=ledger,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {issued.raw_key}"},
        ) as client,
    ):
        yield app, client, ledger


def remaining(r: httpx.Response) -> tuple[int, int]:
    return (
        int(r.headers["x-ratelimit-remaining-requests"]),
        int(r.headers["x-ratelimit-remaining-tokens"]),
    )


def assert_rate_limited(r: httpx.Response, code: str) -> None:
    assert r.status_code == 429, r.text
    assert r.json()["error"]["type"] == "rate_limit_exceeded"
    assert r.json()["error"]["code"] == code


# --- headers and reservations -------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_carry_rate_limit_headers(stream: bool) -> None:
    async with tenant_gateway(rpm_limit=10, tpm_limit=10_000) as (_, client, _):
        r = await client.post("/v1/chat/completions", json=chat(50, stream=stream))
    assert r.status_code == 200
    assert r.headers["x-ratelimit-limit-requests"] == "10"
    assert remaining(r) == (9, 10_000 - (PROMPT + 50))


async def test_reservation_is_prompt_plus_completion_cap() -> None:
    async with tenant_gateway(rpm_limit=10, tpm_limit=10_000) as (app, client, _):
        # Without max_tokens: the configured default cap.
        r = await client.post("/v1/chat/completions", json=chat())
        assert remaining(r)[1] == 10_000 - (PROMPT + app.state.settings.tpm_default_max_tokens)
    async with tenant_gateway(rpm_limit=10, tpm_limit=10_000) as (_, client, _):
        # max_completion_tokens wins over the legacy max_tokens.
        r = await client.post("/v1/chat/completions", json=chat(500, max_completion_tokens=20))
        assert remaining(r)[1] == 10_000 - (PROMPT + 20)


# --- 429s ------------------------------------------------------------------------------


async def test_rpm_limit_turns_requests_away_with_retry_after() -> None:
    async with tenant_gateway(rpm_limit=2, tpm_limit=10_000) as (_, client, ledger):
        for _ in range(2):
            assert (await client.post("/v1/chat/completions", json=chat(10))).status_code == 200
        r = await client.post("/v1/chat/completions", json=chat(10))
    assert_rate_limited(r, "rpm_exceeded")
    # 2 per minute: the next request is ~30 s away.
    assert r.headers["retry-after"] == "30"
    assert r.headers["x-ratelimit-remaining-requests"] == "0"
    # Turned away before any provider: recorded, but with nothing billed.
    assert [e.status for e in ledger.events] == ["success", "success", "rate_limited"]
    rejected = ledger.events[-1]
    assert (rejected.provider, rejected.prompt_tokens, rejected.completion_tokens) == (None, 0, 0)


async def test_tpm_limit_turns_requests_away_without_spending_rpm() -> None:
    async with tenant_gateway(rpm_limit=10, tpm_limit=1_000) as (app, client, _):
        # Uses everything it reserved, so reconciliation gives nothing back.
        used_all = {"X-Mock-Output-Tokens": "600"}
        r = await client.post("/v1/chat/completions", json=chat(600), headers=used_all)
        assert r.status_code == 200
        await app.state.metering.wait_idle()

        r = await client.post("/v1/chat/completions", json=chat(600))
        assert_rate_limited(r, "tpm_exceeded")
        assert 1 <= int(r.headers["retry-after"]) <= 60
        # The turned-away request kept its request token.
        r = await client.post("/v1/chat/completions", json=chat(10))
        assert r.status_code == 200
        assert remaining(r)[0] == 8


async def test_a_request_beyond_the_tpm_limit_is_too_large() -> None:
    async with tenant_gateway(rpm_limit=10, tpm_limit=100) as (_, client, _):
        r = await client.post("/v1/chat/completions", json=chat(200))
    assert_rate_limited(r, "request_too_large")
    assert "retry-after" not in r.headers  # waiting won't help


# --- reconciliation -----------------------------------------------------------------


async def test_unused_reservation_is_refunded() -> None:
    tpm = 6_000  # refills 100 tokens a second
    async with tenant_gateway(rpm_limit=10, tpm_limit=tpm) as (app, client, _):
        ten = {"X-Mock-Output-Tokens": "10"}
        started = time.monotonic()
        first = await client.post("/v1/chat/completions", json=chat(500), headers=ten)
        await app.state.metering.wait_idle()
        second = await client.post("/v1/chat/completions", json=chat(500), headers=ten)
        elapsed = time.monotonic() - started
    # The first request used PROMPT + 10 of its PROMPT + 500: 490 came back.
    expected = remaining(first)[1] + 490 - (PROMPT + 500)
    assert expected - 1 <= remaining(second)[1] <= expected + tpm / 60 * elapsed + 1


async def test_a_request_that_never_reaches_a_provider_gives_its_tokens_back() -> None:
    async with tenant_gateway(rpm_limit=10, tpm_limit=10_000, allowed_models=["llama3.1:8b"]) as (
        app,
        client,
        _,
    ):
        r = await client.post("/v1/chat/completions", json=chat(500))
        assert r.status_code == 404
        await app.state.metering.wait_idle()
        r = await client.post("/v1/chat/completions", json=chat(500, model="llama3.1:8b"))
    assert r.status_code == 200
    # The 404's tokens all came back (refill is capped at the limit).
    prompt = estimate_prompt_tokens(MESSAGES, "llama3.1:8b")
    assert remaining(r) == (8, 10_000 - (prompt + 500))


# --- one round trip ----------------------------------------------------------------------


class RoundTrips:
    """The (command, first argument) of every write to a Redis socket from this
    process: one per round trip, a pipeline's commands counted together."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[list[tuple[str, str]]] = []
        original = AbstractConnection.send_packed_command

        async def send(conn: AbstractConnection, command: Any, *args: Any, **kwargs: Any) -> None:
            chunks = [command] if isinstance(command, bytes | str) else list(command)
            packed = b"".join(c.encode() if isinstance(c, str) else c for c in chunks)
            self.sent.append(_commands(packed))
            await original(conn, command, *args, **kwargs)

        monkeypatch.setattr(AbstractConnection, "send_packed_command", send)


def _commands(packed: bytes) -> list[tuple[str, str]]:
    """(name, first argument) of each RESP command in `packed`."""
    parts = packed.split(b"\r\n")
    found, i = [], 0
    while i < len(parts) and parts[i].startswith(b"*"):
        argc = int(parts[i][1:])
        args = [parts[i + 2 + 2 * n].decode() for n in range(argc)]
        found.append((args[0].upper(), args[1] if argc > 1 else ""))
        i += 1 + 2 * argc
    return found


async def evalsha_calls(redis: Redis) -> int:
    stats = await redis.info("commandstats")
    return int(stats.get("cmdstat_evalsha", {}).get("calls", 0))


async def test_a_cached_request_costs_one_redis_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    observer = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        async with tenant_gateway(rpm_limit=100, tpm_limit=100_000) as (app, client, _):
            # A key's first two sightings read Postgres and fill the cache.
            for _ in range(2):
                assert (await client.get("/v1/models")).status_code == 200
            await app.state.metering.wait_idle()

            def no_second_call(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("limiter.check called on a cached key")

            monkeypatch.setattr(app.state.limiter, "check", no_second_call)
            evalsha_before = await evalsha_calls(observer)
            trips = RoundTrips(monkeypatch)
            r = await client.post("/v1/chat/completions", json=chat(10))
            assert r.status_code == 200
            await app.state.metering.wait_idle()
            monkeypatch.undo()
            evalsha_after = await evalsha_calls(observer)
    finally:
        await observer.aclose()
    # One round trip authenticated and rate limited the request; the other,
    # off the response path, reconciled its reservation. Nothing else.
    assert trips.sent == [
        [("EVALSHA", app.state.key_cache._lookup.sha)],
        [("EVALSHA", app.state.limiter._reconcile.sha)],
    ]
    assert evalsha_after - evalsha_before == 2  # and Redis saw just those two


# --- failing closed ---------------------------------------------------------------------


async def test_fails_closed_without_redis() -> None:
    settings = {"redis_url": DEAD_REDIS, "redis_timeout_s": 0.2}
    async with tenant_gateway(settings, rpm_limit=10, tpm_limit=10_000) as (_, client, ledger):
        r = await client.post("/v1/chat/completions", json=chat(10))
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "auth_unavailable"
    assert ledger.events == []
