"""The semantic cache end to end (SPEC Phase 2.4), against real Postgres and
pgvector, with a stand-in embedder (tests/helpers.py) and a threshold of 0.9
for it: a question asked with one word more hits, one with a word changed
("Austria", "Australia") misses.

The real models and the real threshold are B4's business (bench/cache_eval).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select, text
from sqlalchemy.exc import OperationalError

from mock_provider.app import Stats
from mock_provider.app import create_app as create_mock_app
from tests.helpers import TEST_PROVIDERS, MemoryLedger, WordEmbedder, issue_key
from tests.integration.test_rate_limit import RoundTrips
from tollgate.cache.semantic import SemanticCache, question_of
from tollgate.config import Settings
from tollgate.db.models import CacheEntryRow
from tollgate.main import create_app
from tollgate.usage.ledger import UsageEvent

THRESHOLD = 0.9


def ask(question: str, **fields: Any) -> dict[str, Any]:
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": question}],
        "temperature": 0,
        **fields,
    }


AUSTRIA = ask("what is the capital of austria")
AUSTRIA_AGAIN = ask("so what is the capital of austria")  # one word more: 0.93
AUSTRALIA = ask("what is the capital of australia")  # one word changed: 0.83


class Gateway:
    def __init__(
        self, app: FastAPI, client: httpx.AsyncClient, ledger: MemoryLedger, mock: FastAPI, key: Any
    ) -> None:
        self.app, self.client, self.ledger, self.mock, self.key = app, client, ledger, mock, key

    @property
    def stats(self) -> Stats:
        stats: Stats = self.mock.state.stats
        return stats

    @property
    def semantic(self) -> SemanticCache:
        semantic: SemanticCache = self.app.state.semantic
        return semantic

    @property
    def tenant_id(self) -> str:
        return str(self.key.key.tenant_id)

    async def chat(self, body: dict[str, Any], **headers: str) -> httpx.Response:
        r = await self.client.post("/v1/chat/completions", json=body, headers=headers)
        await self.app.state.metering.wait_idle()
        if self.app.state.semantic is not None:
            await self.semantic.wait_idle()  # stored, and counted
        return r

    def last(self) -> UsageEvent:
        return self.ledger.events[-1]

    async def entries(self) -> list[CacheEntryRow]:
        async with self.app.state.sessions() as session:
            rows = await session.scalars(
                select(CacheEntryRow).where(CacheEntryRow.tenant_id == uuid.UUID(self.tenant_id))
            )
            return list(rows)


@asynccontextmanager
async def gateway(
    settings: dict[str, Any] | None = None, embedder: WordEmbedder | None = None
) -> AsyncIterator[Gateway]:
    issued = await issue_key(rpm_limit=1000, tpm_limit=1_000_000)
    ledger, mock = MemoryLedger(), create_mock_app(env={"MOCK_SEED": "7"})
    app = create_app(
        Settings(
            providers_config=TEST_PROVIDERS,
            **{"semantic_cache_threshold": THRESHOLD, **(settings or {})},
        ),
        upstream_transport=httpx.ASGITransport(app=mock),
        ledger=ledger,
        embedder=embedder or WordEmbedder(),
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


# --- hits and misses ---------------------------------------------------------------------


async def test_a_question_asked_differently_is_answered_from_the_cache(gw: Gateway) -> None:
    first = await gw.chat(AUSTRIA)
    assert first.headers["x-tollgate-cache"] == "MISS"
    (entry,) = await gw.entries()
    assert (entry.query_text, entry.hit_count) == ("what is the capital of austria", 0)
    again = await gw.chat(AUSTRIA_AGAIN)
    assert again.status_code == 200, again.text
    assert again.headers["x-tollgate-cache"] == "HIT"
    assert content(again) == content(first)
    assert gw.stats.requests == 1  # the provider answered once
    event = gw.last()
    assert (event.status, event.cache_status, event.provider, event.attempts) == (
        "success",
        "HIT",
        None,
        0,
    )
    assert (event.prompt_tokens, event.completion_tokens) == (0, 0)
    assert event.saved_cost_usd == gw.ledger.events[0].cost_usd > 0
    assert (await gw.entries())[0].hit_count == 1


async def test_a_near_miss_below_the_threshold_misses(gw: Gateway) -> None:
    await gw.chat(AUSTRIA)
    r = await gw.chat(AUSTRALIA)
    assert r.headers["x-tollgate-cache"] == "MISS"
    assert gw.stats.requests == 2
    assert len(await gw.entries()) == 2  # and is kept in its turn


@pytest.mark.parametrize(
    "other",
    [
        ask("so what is the capital of austria", model="llama3.1:8b"),
        ask("so what is the capital of austria", max_tokens=5),
        ask("so what is the capital of austria", top_p=0.5),
        {**AUSTRIA_AGAIN, "messages": [
            {"role": "system", "content": "Answer in French."}, *AUSTRIA_AGAIN["messages"]
        ]},
        {**AUSTRIA_AGAIN, "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi! Ask away."},
            *AUSTRIA_AGAIN["messages"],
        ]},
    ],
    ids=["model", "max_tokens", "top_p", "system-prompt", "earlier-turns"],
)  # fmt: skip
async def test_only_the_same_scope_matches(gw: Gateway, other: dict[str, Any]) -> None:
    """Same question, different conversation or parameters: a miss."""
    await gw.chat(AUSTRIA)
    assert (await gw.chat(other)).headers["x-tollgate-cache"] == "MISS"
    assert gw.stats.requests == 2


async def test_each_tenant_has_its_own_semantic_cache() -> None:
    async with gateway() as a, gateway() as b:
        await a.chat(AUSTRIA)
        assert (await a.chat(AUSTRIA_AGAIN)).headers["x-tollgate-cache"] == "HIT"
        assert (await b.chat(AUSTRIA_AGAIN)).headers["x-tollgate-cache"] == "MISS"


async def test_only_the_last_user_message_is_embedded() -> None:
    embedder = WordEmbedder()
    async with gateway(embedder=embedder) as g:
        body = {**AUSTRIA, "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "what is the capital of austria"},
        ]}  # fmt: skip
        await g.chat(body)
    assert embedder.texts == ["what is the capital of austria"]


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in AUSTRIA.items() if k != "temperature"},  # not cacheable at all
        {**AUSTRIA, "messages": [*AUSTRIA["messages"], {"role": "assistant", "content": "It"}]},
        {**AUSTRIA, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:,"}}
        ]}]},
    ],
    ids=["not-cacheable", "last-message-not-the-user's", "no-text"],
)  # fmt: skip
async def test_requests_without_a_question_to_embed_skip_the_layer(body: dict[str, Any]) -> None:
    embedder = WordEmbedder()
    async with gateway(embedder=embedder) as g:
        assert (await g.chat(body)).status_code == 200
        assert await g.entries() == []
    assert embedder.texts == []


async def test_a_streamed_answer_is_kept_and_a_paraphrase_is_replayed(gw: Gateway) -> None:
    first = await gw.chat({**AUSTRIA, "stream": True})
    assert first.headers["x-tollgate-cache"] == "MISS"
    assert len(await gw.entries()) == 1  # assembled from the stream
    plain = await gw.chat(AUSTRIA_AGAIN)
    assert plain.headers["x-tollgate-cache"] == "HIT"
    streamed = await gw.chat({**AUSTRIA_AGAIN, "stream": True})
    assert streamed.headers["x-tollgate-cache"] == "HIT"
    assert streamed.text.rstrip().endswith("data: [DONE]")
    chunks = [
        json.loads(line[6:])
        for line in streamed.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    replayed = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert replayed == content(plain)
    assert gw.stats.requests == 1


async def test_entries_expire() -> None:
    async with gateway({"semantic_cache_ttl_s": 1}) as g:
        await g.chat(AUSTRIA)
        assert (await g.chat(AUSTRIA_AGAIN)).headers["x-tollgate-cache"] == "HIT"
        await asyncio.sleep(1.2)
        assert (await g.chat(ask("now what is the capital of austria"))).headers[
            "x-tollgate-cache"
        ] == "MISS"
        assert await g.semantic.sweep() >= 1  # and the sweep deletes what has expired


async def test_failures_and_cut_off_answers_are_not_kept(gw: Gateway) -> None:
    fail = {"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"}
    assert (await gw.chat(AUSTRIA, **fail)).status_code == 500
    drop = {"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "drop_mid_stream"}
    await gw.chat({**AUSTRIA, "stream": True}, **drop)
    assert await gw.entries() == []


# --- failing open ------------------------------------------------------------------------


async def test_a_failing_embedder_is_a_miss_not_an_error() -> None:
    embedder = WordEmbedder()
    embedder.fail = True
    async with gateway(embedder=embedder) as g:
        r = await g.chat(AUSTRIA)
        assert (r.status_code, r.headers["x-tollgate-cache"]) == (200, "MISS")
        assert await g.entries() == []  # nothing to store it by
        metrics = (await g.client.get("/metrics")).text
    assert 'tollgate_cache_lookups_total{layer="semantic",result="error"} 1.0' in metrics


async def test_a_failing_database_is_a_miss_not_an_error(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any) -> None:
        raise OperationalError("SELECT", {}, ConnectionRefusedError("postgres is down"))

    monkeypatch.setattr(gw.semantic, "_nearest", down)
    r = await gw.chat(AUSTRIA)
    assert (r.status_code, r.headers["x-tollgate-cache"]) == (200, "MISS")


# --- the lookup --------------------------------------------------------------------------


async def test_the_nearest_entry_is_found_among_many_closer_ones_of_other_tenants(
    gw: Gateway,
) -> None:
    """Another tenant has the very question cached 3,000 times (similarity 1),
    and ours are 3,000 entries on other questions and one close enough to hit.
    The lookup searches our tenant's scope exactly, so it finds that one. An
    HNSW index on the embedding, filtered by tenant afterwards, missed it in
    most builds tried, iterative scan or not (SPEC Phase 2.4): the table has
    no approximate index."""
    question = question_of(AUSTRIA_AGAIN)
    assert question is not None
    embedder = WordEmbedder()

    def completion(answer: str) -> dict[str, Any]:
        return {
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 2, "total_tokens": 16},
        }  # fmt: skip

    def row(tenant: uuid.UUID, embedding: list[float], answer: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant, "scope_hash": question.scope, "query_text": "x",
            "embedding": embedding, "response_json": completion(answer),
            "completion_tokens": 2, "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }  # fmt: skip

    ours, other = uuid.UUID(gw.tenant_id), uuid.uuid4()
    rows = [
        # Another tenant's: the very question asked, similarity 1.
        *(row(other, embedder.embed(question.text), "Theirs.") for _ in range(3000)),
        # Ours, on other questions entirely.
        *(row(ours, embedder.embed(f"unrelated question {i}"), "Elsewhere.") for i in range(3000)),
        # And ours on this question, close enough to hit (0.93).
        row(ours, embedder.embed("what is the capital of austria"), "Vienna."),
    ]
    async with gw.app.state.sessions() as session, session.begin():
        await session.execute(insert(CacheEntryRow), rows)
        definitions = await session.scalars(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = 'cache_entries'")
        )
        indexes = list(definitions)
    approximate = [d for d in indexes if " USING hnsw " in d or " USING ivfflat " in d]
    assert approximate == [], indexes

    found = await gw.semantic.find(gw.tenant_id, question)
    assert found.completion is not None, found
    assert found.completion["choices"][0]["message"]["content"] == "Vienna."
    assert found.similarity is not None and THRESHOLD <= found.similarity < 1


async def test_a_semantic_hit_costs_no_redis_round_trip(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    await gw.chat(AUSTRIA)  # also fills the key cache
    await gw.chat(ask("and what is the capital of austria"))
    trips = RoundTrips(monkeypatch)
    r = await gw.chat(AUSTRIA_AGAIN)
    monkeypatch.undo()
    assert r.headers["x-tollgate-cache"] == "HIT"
    # The admission call (the exact layer's lookup in it), then the settle
    # call that refunds the hit's reservation: the semantic layer is Postgres.
    assert trips.sent == [
        [("EVALSHA", gw.app.state.key_cache._lookup.sha)],
        [("EVALSHA", gw.app.state.limiter._settle.sha)],
    ]


async def test_the_layer_is_off_until_a_threshold_is_set() -> None:
    embedder = WordEmbedder()
    async with gateway({"semantic_cache_threshold": None}, embedder=embedder) as g:
        await g.chat(AUSTRIA)
        assert (await g.chat(AUSTRIA_AGAIN)).headers["x-tollgate-cache"] == "MISS"
        assert g.app.state.semantic is None
    assert embedder.texts == []


async def test_a_model_of_the_wrong_size_stops_the_gateway_starting() -> None:
    embedder = WordEmbedder()
    embedder.dimensions = 768
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, semantic_cache_threshold=THRESHOLD),
        ledger=MemoryLedger(),
        embedder=embedder,
    )
    with pytest.raises(RuntimeError, match="384-dimension"):
        async with app.router.lifespan_context(app):
            pass
