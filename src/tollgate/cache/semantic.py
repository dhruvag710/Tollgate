"""The semantic layer of the response cache (SPEC Phase 2.4), in Postgres.

A cacheable request that misses the exact layer is looked up by meaning: its
last user message is embedded, and the nearest entry of the same tenant and
scope answers it if it is close enough (cosine similarity at or above the
threshold B4 chose). The scope is everything else about the request: the
model, the system prompt and earlier turns, every parameter (the exact key of
the request without its last message), so a question asked in a different
conversation never matches. The search is exact: every unexpired entry of
the tenant in the scope is compared, so no other tenant's entries can hide or
change the answer (SPEC Phase 2.4 says why there is no HNSW index).

The layer fails open: if the embedder or Postgres fails, the request is a
miss, and goes to a provider as it would without a cache. Storing an answer,
and counting a hit, happen off the response path.
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.cache.embedder import Embedder
from tollgate.cache.exact import storable
from tollgate.cache.policy import exact_key
from tollgate.db.models import CacheEntryRow

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Found:
    """The semantic layer's answer to a question."""

    embedding: list[float] | None  # None: the layer failed; nothing to store either
    completion: dict[str, Any] | None = None  # a hit
    entry_id: int | None = None
    similarity: float | None = None  # the nearest entry's, hit or not


class SemanticCache:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        threshold: float,
        ttl_s: int,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self.threshold = threshold
        self._ttl = timedelta(seconds=ttl_s)
        self._tasks: set[asyncio.Task[None]] = set()

    async def find(self, tenant_id: str, question: Question) -> Found:
        """Embed the question and look for its answer. Never raises: a failure
        is a miss (logged by type only; the question is a prompt)."""
        try:
            embedding = await anyio.to_thread.run_sync(self._embedder.embed, question.text)
        except Exception as exc:
            log.warning("semantic cache: could not embed a query: %s", type(exc).__name__)
            return Found(None)
        try:
            nearest = await self._nearest(tenant_id, question.scope, embedding)
        except _DB_ERRORS as exc:
            log.warning("semantic cache: lookup failed: %s", type(exc).__name__)
            return Found(None)
        if nearest is None:
            return Found(embedding)
        entry_id, completion, similarity = nearest
        if similarity >= self.threshold and storable(completion):
            return Found(embedding, completion, entry_id, similarity)
        return Found(embedding, similarity=similarity)

    @staticmethod
    def nearest_query(tenant_id: str, scope: str, embedding: list[float]) -> Select[Any]:
        """The tenant's unexpired entry in scope nearest the embedding, found
        exactly: the (tenant_id, scope_hash) index picks the candidates, and
        each one's distance is computed."""
        distance = CacheEntryRow.embedding.cosine_distance(embedding)
        return (
            select(CacheEntryRow.id, CacheEntryRow.response_json, distance)
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
    ) -> tuple[int, dict[str, Any], float] | None:
        query = self.nearest_query(tenant_id, scope, embedding)
        async with self._sessions() as session, session.begin():
            row = (await session.execute(query)).first()
        if row is None:
            return None
        entry_id, completion, cosine_distance = row
        return entry_id, completion, 1.0 - float(cosine_distance)

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
