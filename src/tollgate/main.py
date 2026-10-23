"""App factory. Run with: uvicorn --factory tollgate.main:create_app"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate.api import health
from tollgate.api.v1 import chat, models
from tollgate.config import Settings, load_providers_file
from tollgate.errors import InternalErrorMiddleware, install_error_handlers
from tollgate.providers.registry import ProviderRegistry
from tollgate.ratelimit import estimate
from tollgate.routing.router import Router
from tollgate.usage.ledger import Ledger, LogLedger, Metering

# How long shutdown waits for requests it cut off to record their usage.
USAGE_RECORD_TIMEOUT_S = 2.0


class RequestIDMiddleware:
    """Assigns a request id (echoed as X-Request-ID) and starts the request timer.

    Plain ASGI (not BaseHTTPMiddleware) so it never buffers streamed bodies or
    hides client disconnects from the endpoint.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {}).update(request_id=request_id, started_at=time.perf_counter())

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_with_id)


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    ledger: Ledger | None = None,
) -> FastAPI:
    """Build the gateway. `upstream_transport` lets tests run providers in-process."""
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)
    providers_file = load_providers_file(settings.providers_config)
    metering = Metering(ledger or LogLedger())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fail at startup, not mid-request, if a tokenizer can't be loaded.
        await anyio.to_thread.run_sync(estimate.warm_up, list(providers_file.models))
        limits = httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive,
        )
        async with httpx.AsyncClient(transport=upstream_transport, limits=limits) as client:
            app.state.router = Router(ProviderRegistry.from_config(providers_file, client))
            yield
            with anyio.move_on_after(USAGE_RECORD_TIMEOUT_S):
                await metering.wait_idle()

    app = FastAPI(title="Tollgate", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.metering = metering
    install_error_handlers(app)
    # The last added runs outermost: InternalErrorMiddleware needs the request id.
    app.add_middleware(InternalErrorMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    return app
