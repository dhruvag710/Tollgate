"""The semantic layer of the response cache (SPEC Phase 2.4), in Postgres.

A cacheable request that misses the exact layer is looked up by meaning, in two
stages (B4, BENCHMARKS.md):

1. Its last user message is embedded, and the nearest entry of the same tenant
   and scope is the candidate if it is close enough: cosine similarity at or
   above the threshold, a loose one that keeps most real paraphrases.
2. A cross-encoder (cache.verifier) reads the candidate's question and this one
   together, and the candidate answers the request only if its score is at or
   above the verifier's threshold.

The scope is everything else about the request: the model, the system prompt
and earlier turns, every parameter (the exact key of the request without its
last message), so a question asked in a different conversation never matches.
The search is exact: every unexpired entry of the tenant in the scope is
compared, so no other tenant's entries can hide or change the answer (SPEC
Phase 2.4 says why there is no HNSW index).

Each model runs in a bounded pool of its own (cache.pool), off the event loop:
the check, about ten times slower than the embedding, never holds an embedding
up. The layer fails open: if a pool is full, a model fails or takes too long,
or Postgres fails, the request is a miss and goes to a provider as it would
without a cache. A lookup that didn't complete changes nothing in the layer
either: only one that did (no candidate, or a candidate the check turned down)
stores the request's answer afterwards, so nothing is served or stored on the
strength of a check that errored, timed out or never ran. Storing an answer,
and counting a hit, happen off the response path.
"""

import asyncio
import logging
import math
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal

from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.cache.embedder import Embedder
from tollgate.cache.exact import storable
from tollgate.cache.policy import exact_key
from tollgate.cache.pool import Done, ModelPool, PoolFull
from tollgate.cache.verifier import Verifier
from tollgate.db.models import CacheEntryRow
from tollgate.observability.timing import Timings

log = logging.getLogger(__name__)

# What the models are given to answer once at startup (SemanticCache.warm_up).
WARM_UP_TEXT = "warm-up"
WARM_UP_PAIR = ("warm up", "warm-up")

# Failures the layer survives as misses (the embedder's could be anything).
_DB_ERRORS = (SQLAlchemyError, OSError, TimeoutError)


@dataclass(frozen=True)
class Question:
    """What a request asks the semantic layer: its scope and last user message."""

    scope: str
    text: str


def question_of(body: Mapping[str, Any]) -> Question | None:
    """The request's question, or None if it has none the layer can embed: its
    last message must be a user message with text, and only text."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return None
    last = messages[-1]
    if last.get("role") != "user":
        return None
    content = last.get("content")
    if isinstance(content, list):  # content parts: text only
        if not all(isinstance(p, dict) and p.get("type") == "text" for p in content):
            return None
        content = "".join(str(p.get("text", "")) for p in content)
    if not isinstance(content, str) or not content.strip():
        return None
    return Question(exact_key({**body, "messages": messages[:-1]}), content)


# How a lookup ended. "hit": the candidate passed the check. "miss": no
# candidate (no entry in scope, or none close enough). "rejected": the check
# turned the candidate down. The rest are failures, each a miss that stores
# nothing: "full" (the stage's pool had no room), "timeout", "error".
Outcome = Literal["hit", "miss", "rejected", "full", "timeout", "error"]
Stage = Literal["embed", "lookup", "verify"]


@dataclass(frozen=True)
class Found:
    """The semantic layer's answer to a question."""

    outcome: Outcome
    stage: Stage | None = None  # where a failure happened
    # The question's embedding, only when the lookup completed without a hit:
    # the request's answer may then be stored by it.
    embedding: list[float] | None = None
    completion: dict[str, Any] | None = None  # a hit
    entry_id: int | None = None
    similarity: float | None = None  # the nearest entry's, if one was found
    score: float | None = None  # the check's, if it ran


@dataclass(frozen=True)
class Limits:
    """Room in each model's pool, and how long a call to it may take, the
    wait for the pool included."""

    queue_size: int
    embed_timeout_s: float
    verify_timeout_s: float


class SemanticCache:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        verifier: Verifier,
        *,
        threshold: float,
        verifier_threshold: float,
        ttl_s: int,
        limits: Limits,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._verifier = verifier
        self.threshold = threshold  # the candidate's similarity, at least
        self.verifier_threshold = verifier_threshold  # the check's score, at least
        self._ttl = timedelta(seconds=ttl_s)
        self._limits = limits
        # One thread per model: a replica runs on one CPU, and a model's
        # tokenizer can't be used by two threads at once.
        self.embed_pool = ModelPool("embed", queue_size=limits.queue_size)
        self.verify_pool = ModelPool("verify", queue_size=limits.queue_size)
        self._tasks: set[asyncio.Task[None]] = set()

    async def warm_up(self) -> None:
        """One call to each model, in its pool's thread: a broken model stops the
        gateway starting, and the first request doesn't pay for setting it up."""
        await self.embed_pool.run(partial(self._embedder.embed, WARM_UP_TEXT), 60.0)
        await self.verify_pool.run(partial(self._verifier.score, *WARM_UP_PAIR), 60.0)

    def close(self) -> None:
        self.embed_pool.close()
        self.verify_pool.close()

    async def find(
        self, tenant_id: str, question: Question, timings: Timings | None = None
    ) -> Found:
        """Look for the question's answer, in two stages. Never raises: a failure
        is a miss (logged by type only; the question is a prompt). `timings`
        gets each stage's time, the wait for a model's pool apart from its
        call, and how the lookup ended."""
        timings = timings or Timings(0.0)
        embedded = await self._call(
            self.embed_pool,
            partial(self._embedder.embed, question.text),
            self._limits.embed_timeout_s,
            "embed",
            timings,
        )
        if isinstance(embedded, Found):
            return embedded
        embedding = embedded.value
        try:
            with timings.stage("semantic-lookup"):
                nearest = await self._nearest(tenant_id, question.scope, embedding)
        except _DB_ERRORS as exc:
            log.warning("semantic cache: lookup failed: %s", type(exc).__name__)
            return self._ended(Found("error", "lookup"), timings)
        if nearest is None:
            return self._ended(Found("miss", embedding=embedding), timings)
        entry_id, completion, similarity, cached_text = nearest
        if similarity < self.threshold or not storable(completion):
            return self._ended(Found("miss", embedding=embedding, similarity=similarity), timings)
        checked = await self._call(
            self.verify_pool,
            partial(self._verifier.score, cached_text, question.text),
            self._limits.verify_timeout_s,
            "verify",
            timings,
            similarity,
        )
        if isinstance(checked, Found):
            return checked
        score = checked.value
        usable = isinstance(score, float | int) and not isinstance(score, bool)
        if not usable or not math.isfinite(score):
            log.warning("semantic cache: the check returned no usable score")
            return self._ended(Found("error", "verify", similarity=similarity), timings)
        if score < self.verifier_threshold:
            found = Found("rejected", embedding=embedding, similarity=similarity, score=score)
            return self._ended(found, timings)
        hit = Found("hit", completion=completion, entry_id=entry_id, similarity=similarity,
                    score=score)  # fmt: skip
        return self._ended(hit, timings)

    async def _call[T](
        self,
        pool: ModelPool,
        call: Callable[[], T],
        timeout_s: float,
        stage: Stage,
        timings: Timings,
        similarity: float | None = None,
    ) -> Done[T] | Found:
        """One model's call in its pool: what it returned, or the Found that
        ends the lookup if there was no room, or it failed or took too long."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            done = await pool.run(call, timeout_s)
        except PoolFull:
            return self._ended(Found("full", stage, similarity=similarity), timings)
        except TimeoutError:
            timings.add(f"semantic-{stage}-wait", loop.time() - started)
            log.warning("semantic cache: %s took longer than %.0f ms", stage, timeout_s * 1000)
            return self._ended(Found("timeout", stage, similarity=similarity), timings)
        except Exception as exc:
            timings.add(f"semantic-{stage}", loop.time() - started)
            log.warning("semantic cache: %s failed: %s", stage, type(exc).__name__)
            return self._ended(Found("error", stage, similarity=similarity), timings)
        timings.add(f"semantic-{stage}-wait", done.waited_s)
        timings.add(f"semantic-{stage}", done.ran_s)
        return done

    @staticmethod
    def _ended(found: Found, timings: Timings) -> Found:
        """Note how the lookup ended: its outcome, and where it failed."""
        timings.note("semantic", found.outcome if found.stage is None else
                     f"{found.stage}-{found.outcome}")  # fmt: skip
        return found

    @staticmethod
    def nearest_query(tenant_id: str, scope: str, embedding: list[float]) -> Select[Any]:
        """The tenant's unexpired entry in scope nearest the embedding, found
        exactly: the (tenant_id, scope_hash) index picks the candidates, and
        each one's distance is computed."""
        distance = CacheEntryRow.embedding.cosine_distance(embedding)
        return (
            select(
                CacheEntryRow.id, CacheEntryRow.response_json, distance, CacheEntryRow.query_text
            )
            .where(
                CacheEntryRow.tenant_id == uuid.UUID(tenant_id),
                CacheEntryRow.scope_hash == scope,
                CacheEntryRow.expires_at > func.now(),
            )
            .order_by(distance)
            .limit(1)
        )

    async def _nearest(
        self, tenant_id: str, scope: str, embedding: list[float]
    ) -> tuple[int, dict[str, Any], float, str] | None:
        """The nearest entry: its id, answer, similarity and question."""
        query = self.nearest_query(tenant_id, scope, embedding)
        async with self._sessions() as session, session.begin():
            row = (await session.execute(query)).first()
        if row is None:
            return None
        entry_id, completion, cosine_distance, cached_text = row
        return entry_id, completion, 1.0 - float(cosine_distance), cached_text

    def store(
        self, tenant_id: str, question: Question, embedding: list[float], completion: dict[str, Any]
    ) -> None:
        """Keep a complete answer, off the response path."""
        usage = completion.get("usage") or {}
        values = {
            "tenant_id": uuid.UUID(tenant_id),
            "scope_hash": question.scope,
            "query_text": question.text,
            "embedding": embedding,
            "response_json": completion,
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "expires_at": datetime.now(UTC) + self._ttl,
        }
        self._background("store an entry", self._execute(insert(CacheEntryRow).values(values)))

    def touch(self, entry_id: int) -> None:
        """Count a hit, off the response path."""
        statement = (
            update(CacheEntryRow)
            .where(CacheEntryRow.id == entry_id)
            .values(hit_count=CacheEntryRow.hit_count + 1)
        )
        self._background("count a hit", self._execute(statement))

    async def sweep(self) -> int:
        """Delete expired entries; how many."""
        statement = delete(CacheEntryRow).where(CacheEntryRow.expires_at <= func.now())
        async with self._sessions() as session, session.begin():
            result = await session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    async def wait_idle(self) -> None:
        """Wait for the stores and counts still running (shutdown)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.wait(pending)

    async def _execute(self, statement: Any) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(statement)

    def _background(self, what: str, call: Coroutine[Any, Any, None]) -> None:
        async def run() -> None:
            try:
                await call
            except _DB_ERRORS as exc:
                log.warning("semantic cache: could not %s: %s", what, type(exc).__name__)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
