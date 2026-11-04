"""The exact-match cache end to end (SPEC Phase 2.3): identical requests hit,
changing any param misses, and a hit is free beyond RPM, answered from the
same Redis call that admits it."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from mock_provider.app import Stats
from mock_provider.app import create_app as create_mock_app
from tests.helpers import ADMIN, TEST_PROVIDERS, MemoryLedger, issue_key, redis_month
from tests.integration.test_rate_limit import RoundTrips, remaining
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.usage.budget import spend_key
from tollgate.usage.ledger import UsageEvent

MESSAGES = [{"role": "user", "content": "what is the capital of Austria?"}]
CHAT: dict[str, Any] = {"model": "gpt-4o-mini", "messages": MESSAGES, "temperature": 0}


class Gateway:
    def __init__(
        self, app: FastAPI, client: httpx.AsyncClient, ledger: MemoryLedger, mock: FastAPI, key: Any
    ) -> None:
        self.app, self.client, self.ledger, self.mock, self.key = app, client, ledger, mock, key

    @property
    def stats(self) -> Stats:
        stats: Stats = self.mock.state.stats
        return stats

    async def chat(self, body: dict[str, Any] | None = None, **headers: str) -> httpx.Response:
        r = await self.client.post("/v1/chat/completions", json=body or CHAT, headers=headers)
        await self.app.state.metering.wait_idle()  # settled, and so stored
        return r

    def last(self) -> UsageEvent:
        return self.ledger.events[-1]


@asynccontextmanager
async def gateway(settings: dict[str, Any] | None = None, **tenant: Any) -> AsyncIterator[Gateway]:
    tenant = {"rpm_limit": 1000, "tpm_limit": 1_000_000, **tenant}
    issued = await issue_key(**tenant)
    ledger, mock = MemoryLedger(), create_mock_app(env={"MOCK_SEED": "7"})
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, **(settings or {})),
        upstream_transport=httpx.ASGITransport(app=mock),
        ledger=ledger,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://tollgate",
            headers={"Authorization": f"Bearer {issued.raw_key}"},
        ) as client,
    ):
        yield Gateway(app, client, ledger, mock, issued)


@pytest.fixture
async def gw() -> AsyncIterator[Gateway]:
    async with gateway() as g:
        yield g


def content(r: httpx.Response) -> str:
    text: str = r.json()["choices"][0]["message"]["content"]
    return text


def streamed(r: httpx.Response) -> tuple[str, list[Any]]:
    """The content of an SSE response, and its chunks."""
    chunks = [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert r.text.rstrip().endswith("data: [DONE]")
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    return text, chunks


# --- hits and misses ---------------------------------------------------------------------


async def test_an_identical_request_is_answered_from_the_cache(gw: Gateway) -> None:
    first = await gw.chat()
    assert first.status_code == 200, first.text
    assert (first.headers["x-tollgate-cache"], first.headers["x-tollgate-provider"]) == (
        "MISS",
        "mock",
    )
    second = await gw.chat()
    assert second.status_code == 200, second.text
    assert second.headers["x-tollgate-cache"] == "HIT"
    assert "x-tollgate-provider" not in second.headers  # no provider was asked
    assert gw.stats.requests == 1
    assert content(second) == content(first)
    assert second.json()["usage"] == first.json()["usage"]
    assert second.json()["id"] != first.json()["id"]  # a response of its own


@pytest.mark.parametrize(
    "change",
    [
        {"model": "llama3.1:8b"},
        {"messages": [{"role": "user", "content": "what is the capital of Australia?"}]},
        {"messages": [{"role": "system", "content": "Answer in French."}, *MESSAGES]},
        {"temperature": 0.2},
        {"max_tokens": 5},
        {"max_completion_tokens": 5},
        {"top_p": 0.5},
        {"stop": ["\n"]},
        {"seed": 11},
        {"response_format": {"type": "json_object"}},
        {"presence_penalty": 0.5},
        {"frequency_penalty": 0.5},
    ],
    ids=lambda change: next(iter(change)),
)
async def test_changing_any_param_misses(gw: Gateway, change: dict[str, Any]) -> None:
    assert (await gw.chat()).headers["x-tollgate-cache"] == "MISS"
    assert (await gw.chat()).headers["x-tollgate-cache"] == "HIT"
    changed = {**CHAT, **change}
    assert (await gw.chat(changed)).headers["x-tollgate-cache"] == "MISS"
    assert gw.stats.requests == 2  # it went to the provider
    assert (await gw.chat(changed)).headers["x-tollgate-cache"] == "HIT"  # and is cached in turn
    assert gw.stats.requests == 2


async def test_a_large_request_is_cached_too(gw: Gateway) -> None:
    """Past 32 KB the key is hashed off the event loop: the same key."""
    big = {**CHAT, "messages": [{"role": "user", "content": "the quick brown fox " * 2_500}]}
    assert len(json.dumps(big)) > 32_768
    assert (await gw.chat(big)).headers["x-tollgate-cache"] == "MISS"
    assert (await gw.chat(big)).headers["x-tollgate-cache"] == "HIT"


async def test_each_tenant_has_its_own_cache() -> None:
    async with gateway() as a, gateway() as b:
        await a.chat()
        assert (await a.chat()).headers["x-tollgate-cache"] == "HIT"
        assert (await b.chat()).headers["x-tollgate-cache"] == "MISS"


async def test_entries_expire() -> None:
    async with gateway({"exact_cache_ttl_s": 1}) as g:
        await g.chat()
        assert (await g.chat()).headers["x-tollgate-cache"] == "HIT"
        await asyncio.sleep(1.2)
        assert (await g.chat()).headers["x-tollgate-cache"] == "MISS"


async def test_an_answer_too_large_to_keep_is_not_stored() -> None:
    async with gateway({"exact_cache_max_entry_bytes": 200}) as g:
        for _ in range(2):
            assert (await g.chat()).headers["x-tollgate-cache"] == "MISS"
        assert g.stats.requests == 2


# --- streams -----------------------------------------------------------------------------


async def test_a_plain_answer_is_replayed_to_a_streamed_request(gw: Gateway) -> None:
    plain = await gw.chat()
    r = await gw.chat({**CHAT, "stream": True})
    assert r.status_code == 200
    assert r.headers["x-tollgate-cache"] == "HIT"
    assert r.headers["content-type"].startswith("text/event-stream")
    text, chunks = streamed(r)
    assert text == content(plain)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all("usage" not in c for c in chunks)  # the client didn't ask for it
    assert gw.stats.requests == 1


async def test_a_streamed_answer_is_stored_whole_and_serves_both(gw: Gateway) -> None:
    first = await gw.chat({**CHAT, "stream": True})
    assert first.headers["x-tollgate-cache"] == "MISS"
    text, _ = streamed(first)
    plain = await gw.chat()  # not streamed, the same question
    assert plain.headers["x-tollgate-cache"] == "HIT"
    assert content(plain) == text
    # The provider's own usage, from the final chunk Tollgate asked it for.
    assert plain.json()["usage"]["completion_tokens"] == gw.stats.completion_tokens
    again = await gw.chat({**CHAT, "stream": True, "stream_options": {"include_usage": True}})
    assert again.headers["x-tollgate-cache"] == "HIT"
    replayed, chunks = streamed(again)
    assert replayed == text
    assert chunks[-1]["usage"] == plain.json()["usage"]  # asked for this time
    assert gw.stats.requests == 1


async def test_the_openai_sdk_reads_a_cached_stream(gw: Gateway) -> None:
    sdk = openai.AsyncOpenAI(
        base_url="http://tollgate/v1",
        api_key=gw.key.raw_key,
        http_client=gw.client,
        max_retries=0,
    )
    first = await sdk.chat.completions.create(**CHAT)
    await gw.app.state.metering.wait_idle()
    stream = await sdk.chat.completions.create(
        **CHAT, stream=True, stream_options={"include_usage": True}
    )
    parts, usage = [], None
    async for chunk in stream:
        parts += [c.delta.content or "" for c in chunk.choices]
        usage = chunk.usage or usage
    assert "".join(parts) == first.choices[0].message.content
    assert usage is not None and usage.total_tokens == first.usage.total_tokens  # type: ignore[union-attr]


# --- what isn't cached -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        ({k: v for k, v in CHAT.items() if k != "temperature"}, {}),  # the provider's default
        ({**CHAT, "temperature": 0.9}, {}),
        ({**CHAT, "tools": [{"type": "function", "function": {"name": "f"}}]}, {}),
        (CHAT, {"X-Tollgate-Cache": "bypass"}),
    ],
    ids=["no-temperature", "sampled", "tools", "client-bypass"],
)
async def test_requests_the_policy_excludes_bypass_the_cache(
    gw: Gateway, body: dict[str, Any], headers: dict[str, str]
) -> None:
    for _ in range(2):
        r = await gw.chat(body, **headers)
        assert r.status_code == 200, r.text
        assert r.headers["x-tollgate-cache"] == "BYPASS"
    assert gw.stats.requests == 2
    assert gw.last().cache_status == "BYPASS"


async def test_a_client_bypass_skips_even_a_cached_answer(gw: Gateway) -> None:
    await gw.chat()
    r = await gw.chat(**{"X-Tollgate-Cache": "bypass"})
    assert r.headers["x-tollgate-cache"] == "BYPASS"
    assert gw.stats.requests == 2


async def test_a_tenant_with_the_cache_disabled_bypasses_it() -> None:
    async with gateway(cache_enabled=False) as g:
        # Its first two requests resolve the key from Postgres, the rest from Redis.
        for _ in range(4):
            assert (await g.chat()).headers["x-tollgate-cache"] == "BYPASS"
        assert g.stats.requests == 4


@pytest.mark.parametrize(
    ("stream", "mode"),
    [(False, "500"), (True, "429"), (True, "drop_mid_stream"), (False, "drop_mid_stream")],
)
async def test_failures_are_never_stored(gw: Gateway, stream: bool, mode: str) -> None:
    fail = {"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": mode}
    await gw.chat({**CHAT, "stream": stream}, **fail)
    r = await gw.chat()
    assert (r.status_code, r.headers["x-tollgate-cache"]) == (200, "MISS")


# --- what a hit costs --------------------------------------------------------------------


async def test_a_hit_is_free_beyond_rpm() -> None:
    async with gateway(rpm_limit=10, tpm_limit=10_000, monthly_budget_usd=Decimal("10")) as g:
        miss = await g.chat()
        redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        try:
            key = spend_key(str(g.key.key.tenant_id), await redis_month(redis))
            spent = await redis.get(key)
            hit = await g.chat()
            assert await redis.get(key) == spent  # nothing spent
        finally:
            await redis.aclose()
        after = await g.chat()  # the next request sees what the hit left
    assert hit.headers["x-tollgate-cache"] == after.headers["x-tollgate-cache"] == "HIT"
    requests_left = [remaining(r)[0] for r in (miss, hit, after)]
    assert requests_left == [9, 8, 7]  # each counts against RPM
    # The hit's TPM reservation came back in full: the tokens left before the
    # next request are what the miss used, refilling, never less.
    assert remaining(after)[1] >= remaining(hit)[1]
    event = g.ledger.events[1]
    usage = miss.json()["usage"]
    assert (event.status, event.cache_status, event.provider, event.attempts) == (
        "success",
        "HIT",
        None,
        0,
    )
    assert (event.prompt_tokens, event.completion_tokens, event.cost_usd) == (0, 0, 0)
    assert event.saved_cost_usd == g.app.state.metering.cost_usd(
        "gpt-4o-mini", usage["prompt_tokens"], usage["completion_tokens"]
    )
    assert event.saved_cost_usd > 0
    assert g.ledger.events[0].cache_status == "MISS"


async def test_limits_still_apply_to_a_cached_answer() -> None:
    async with gateway(rpm_limit=2) as g:
        await g.chat()
        assert (await g.chat()).headers["x-tollgate-cache"] == "HIT"
        r = await g.chat()
        assert r.status_code == 429  # admitted before the cache is looked at
        assert g.last().cache_status == "BYPASS"
    async with gateway(monthly_budget_usd=Decimal("0")) as broke:
        assert (await broke.chat()).status_code == 402


async def test_a_model_the_tenant_may_no_longer_use_gets_no_hit(gw: Gateway) -> None:
    await gw.chat()
    r = await gw.client.patch(
        f"/admin/tenants/{gw.key.key.tenant_id}",
        json={"allowed_models": ["llama3.1:8b"]},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert (await gw.chat()).status_code == 404


async def test_a_hit_costs_no_round_trip_of_its_own(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(2):  # the key's first two sightings read Postgres
        await gw.client.get("/v1/models")
    trips = RoundTrips(monkeypatch)
    assert (await gw.chat()).headers["x-tollgate-cache"] == "MISS"
    assert (await gw.chat()).headers["x-tollgate-cache"] == "HIT"
    monkeypatch.undo()
    lookup, settle = gw.app.state.key_cache._lookup.sha, gw.app.state.limiter._settle.sha
    # The miss: admitted (and looked up) in one call, settled (and stored) in
    # another. The hit: answered from the call that admitted it, then settled.
    assert trips.sent == [[("EVALSHA", lookup)], [("EVALSHA", settle)]] * 2


async def test_the_ledger_still_matches_the_provider_with_hits_mixed_in(gw: Gateway) -> None:
    questions = [
        {**CHAT, "messages": [{"role": "user", "content": f"question {i}"}]} for i in range(3)
    ]
    for body in [*questions, *questions, questions[0], {**questions[1], "stream": True}]:
        assert (await gw.chat(body)).status_code == 200
    events = gw.ledger.events
    assert [e.cache_status for e in events] == ["MISS"] * 3 + ["HIT"] * 5
    # Hits bill nothing, so the ledger sums exactly what the provider served.
    assert sum(e.prompt_tokens for e in events) == gw.stats.prompt_tokens
    assert sum(e.completion_tokens for e in events) == gw.stats.completion_tokens
    # Each hit saved what its question's miss cost: questions 0, 1, 2, 0, 1.
    saved = [e.saved_cost_usd for e in events[3:]]
    assert saved == [events[i].cost_usd for i in (0, 1, 2, 0, 1)]
