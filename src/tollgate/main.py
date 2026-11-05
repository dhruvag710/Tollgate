"""App factory. Run with: uvicorn --factory tollgate.main:create_app"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import anyio
import httpx
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate.api import health
from tollgate.api.admin import keys as admin_keys
from tollgate.api.admin import providers as admin_providers
from tollgate.api.admin import tenants as admin_tenants
from tollgate.api.v1 import chat, models
from tollgate.auth.cache import KeyCache
from tollgate.cache.embedder import Embedder, SentenceTransformerEmbedder
from tollgate.cache.semantic import Limits, SemanticCache
from tollgate.cache.verifier import NLIVerifier, Verifier
from tollgate.config import Settings, load_providers_file
from tollgate.db.models import EMBEDDING_DIMENSIONS
from tollgate.db.session import create_engine, create_sessionmaker
from tollgate.errors import UnhandledErrorMiddleware, install_error_handlers
from tollgate.observability.logging import configure_logging, request_id_var
from tollgate.observability.metrics import Metrics, Observation
from tollgate.observability.timing import Timings
from tollgate.providers.registry import ProviderRegistry
from tollgate.ratelimit import estimate
from tollgate.ratelimit.limiter import RateLimiter
from tollgate.redis_pool import create_redis, open_connections
from tollgate.routing.circuit_breaker import Breakers, BreakerSettings
from tollgate.routing.router import Router
from tollgate.usage.budget import Budgets
from tollgate.usage.ledger import Ledger, Metering, UsageWriter, postgres_writer
from tollgate.usage.pricing import Pricing

log = logging.getLogger(__name__)

# How long shutdown waits for requests it cut off to record their usage (and
# for the breaker reports still running).
USAGE_RECORD_TIMEOUT_S = 2.0
CHAT_PATH = "/v1/chat/completions"


class RequestIDMiddleware:
    """Assigns a request id (echoed as X-Request-ID, and on every log line logged
    during the request), starts the request timer, and records a chat request's
    metrics once it ends.

    Plain ASGI (not BaseHTTPMiddleware) so it never buffers streamed bodies or
    hides client disconnects from the endpoint.
    """

    def __init__(self, app: ASGIApp, metrics: Metrics):
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        started_at = time.perf_counter()
        timings, observation = Timings(started_at), Observation()
        scope.setdefault("state", {}).update(
            request_id=request_id, started_at=started_at, timings=timings, observation=observation
        )

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
                if observation.response_started_at is None:
                    observation.response_started_at = time.perf_counter()
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
            if scope["path"] == CHAT_PATH and scope["method"] == "POST":
                self._metrics.observe_request(
                    observation, started_at, time.perf_counter(), timings.upstream_seconds()
                )


SEMANTIC_SETTINGS = (
    "SEMANTIC_CACHE_MODEL",
    "SEMANTIC_CACHE_THRESHOLD",
    "SEMANTIC_CACHE_VERIFIER",
    "SEMANTIC_CACHE_VERIFIER_THRESHOLD",
)


async def _semantic_cache(
    settings: Settings,
    sessions: Any,
    embedder: Embedder | None,
    verifier: Verifier | None,
) -> SemanticCache | None:
    """The semantic cache layer, if configured: both models (or a test's
    stand-ins) and both thresholds B4 chose. The models load now and answer
    once each, so a broken one stops the gateway starting rather than failing
    requests, and the first request doesn't pay for setting them up."""
    s = settings
    given = {
        "SEMANTIC_CACHE_MODEL": s.semantic_cache_model is not None or embedder is not None,
        "SEMANTIC_CACHE_THRESHOLD": s.semantic_cache_threshold is not None,
        "SEMANTIC_CACHE_VERIFIER": s.semantic_cache_verifier is not None or verifier is not None,
        "SEMANTIC_CACHE_VERIFIER_THRESHOLD": s.semantic_cache_verifier_threshold is not None,
    }
    if not all(given.values()):
        if any(given.values()):
            log.warning(
                "the semantic cache is off: it needs all of %s; missing: %s. B4 found that no "
                "embedding model alone keeps false hits within target (BENCHMARKS.md)",
                ", ".join(SEMANTIC_SETTINGS),
                ", ".join(name for name, ok in given.items() if not ok),
            )
        return None
    assert s.semantic_cache_threshold is not None
    assert s.semantic_cache_verifier_threshold is not None
    if embedder is None:
        assert s.semantic_cache_model is not None
        embedder = await anyio.to_thread.run_sync(
            partial(
                SentenceTransformerEmbedder,
                s.semantic_cache_model,
                revision=s.semantic_cache_model_revision,
                threads=s.semantic_cache_threads,
            )
        )
    if embedder.dimensions != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"the semantic cache stores {EMBEDDING_DIMENSIONS}-dimension embeddings; "
            f"the model makes {embedder.dimensions}"
        )
    if verifier is None:
        assert s.semantic_cache_verifier is not None
        verifier = await anyio.to_thread.run_sync(
            partial(
                NLIVerifier,
                s.semantic_cache_verifier,
                revision=s.semantic_cache_verifier_revision,
                threads=s.semantic_cache_threads,
            )
        )
    semantic = SemanticCache(
        sessions,
        embedder,
        verifier,
        threshold=s.semantic_cache_threshold,
        verifier_threshold=s.semantic_cache_verifier_threshold,
        ttl_s=s.semantic_cache_ttl_s,
        limits=Limits(
            queue_size=s.semantic_cache_queue_size,
            embed_timeout_s=s.semantic_cache_embed_timeout_ms / 1000,
            verify_timeout_s=s.semantic_cache_verify_timeout_ms / 1000,
        ),
    )
    try:
        await semantic.warm_up()
    except BaseException:
        semantic.close()
        raise
    return semantic


async def _sweep(semantic: SemanticCache, settings: Settings) -> None:
    """Delete the semantic cache's expired entries now and then. Lookups skip
    them anyway: this only keeps the table from growing."""
    while True:
        await asyncio.sleep(settings.semantic_cache_sweep_s)
        try:
            deleted = await semantic.sweep()
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            log.warning("semantic cache: could not sweep: %s", type(exc).__name__)
        else:
            if deleted:
                log.info("semantic cache: deleted %d expired entries", deleted)


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    ledger: Ledger | None = None,
    embedder: Embedder | None = None,
    verifier: Verifier | None = None,
) -> FastAPI:
    """Build the gateway. `upstream_transport` lets tests run providers in-process;
    `ledger` replaces the Postgres ledger; `embedder` and `verifier` replace the
    semantic cache's models (the layer still needs both thresholds to be on)."""
    settings = settings or Settings()
    configure_logging(settings.log_level)
    if settings.web_concurrency > 1:
        log.warning(
            "WEB_CONCURRENCY=%d: each worker keeps its own metrics, and a scrape sees only "
            "one of them. Run one worker per replica and scale with replicas.",
            settings.web_concurrency,
        )
    if settings.uses_development_secrets:
        log.warning(
            "DEV_MODE: running with the development KEY_PEPPER/ADMIN_TOKEN. "
            "Anyone can read these; never use them outside local development."
        )
    providers_file = load_providers_file(settings.providers_config)
    # The Postgres ledger's writer starts with the lifespan; events wait in its queue.
    writer: UsageWriter | None = None
    if ledger is None:
        ledger = writer = UsageWriter(
            batch_size=settings.ledger_batch_size, interval_s=settings.ledger_flush_interval_s
        )
    metering = Metering(
        ledger,
        Pricing.from_config(providers_file),
        cache_ttl_s=settings.exact_cache_ttl_s,
        cache_max_entry_bytes=settings.exact_cache_max_entry_bytes,
    )
    metrics = Metrics(providers_file.models, (p.name for p in providers_file.providers))
    if writer is not None:
        metrics.watch_ledger(lambda: writer.pending)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fail at startup, not mid-request, if a tokenizer can't be loaded.
        await anyio.to_thread.run_sync(estimate.warm_up, list(providers_file.models))
        limits = httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive,
        )
        # Both connect lazily: the gateway starts while either is briefly down,
        # and requests fail closed meanwhile (/readyz reports it).
        engine = create_engine(settings)
        redis = create_redis(settings)
        app.state.engine = engine
        app.state.sessions = create_sessionmaker(engine)
        app.state.redis = redis
        app.state.key_cache = KeyCache(redis)
        app.state.limiter = metering.limiter = RateLimiter(redis)
        app.state.budgets = Budgets(redis, app.state.sessions)
        breakers = Breakers(
            redis,
            {
                p.name: BreakerSettings.of(providers_file.breaker_for(p), p)
                for p in providers_file.providers
            },
            on_transition=metrics.breaker_transition,
        )
        semantic: SemanticCache | None = None
        sweeper: asyncio.Task[None] | None = None
        if writer is not None:
            writer.start(postgres_writer(app.state.sessions))
        try:
            semantic = app.state.semantic = await _semantic_cache(
                settings, app.state.sessions, embedder, verifier
            )
            if semantic is not None:
                metrics.watch_semantic(semantic.embed_pool, semantic.verify_pool)
                sweeper = asyncio.create_task(_sweep(semantic, settings))
            # Before serving, so a cold gateway can take a burst (best effort).
            await open_connections(
                redis, min(settings.redis_min_connections, settings.redis_max_connections)
            )
            async with httpx.AsyncClient(transport=upstream_transport, limits=limits) as client:
                registry = ProviderRegistry.from_config(providers_file, client)
                app.state.router = Router(registry, breakers, metrics)
                yield
                with anyio.move_on_after(USAGE_RECORD_TIMEOUT_S):
                    await metering.wait_idle()
                    await breakers.wait_idle()
                    if semantic is not None:
                        await semantic.wait_idle()
        finally:
            if sweeper is not None:
                sweeper.cancel()
            if semantic is not None:
                semantic.close()
            # After every request has recorded its event: shutdown flushes them all.
            if writer is not None:
                await writer.close(settings.ledger_shutdown_timeout_s)
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Tollgate", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.metering = metering
    app.state.metrics = metrics
    install_error_handlers(app)
    # The last added runs outermost: UnhandledErrorMiddleware needs the request id.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestIDMiddleware, metrics=metrics)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(admin_tenants.router)
    app.include_router(admin_keys.router)
    app.include_router(admin_providers.router)
    return app
