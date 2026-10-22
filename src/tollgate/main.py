"""App factory. Run with: uvicorn --factory tollgate.main:create_app"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate.api import health
from tollgate.api.v1 import chat, models
from tollgate.config import Settings, load_providers_file
from tollgate.errors import install_error_handlers
from tollgate.providers.registry import ProviderRegistry
from tollgate.routing.router import Router


class RequestIDMiddleware:
    """Assigns a request id and echoes it as X-Request-ID.

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
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_with_id)


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway. `upstream_transport` lets tests run providers in-process."""
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)
    providers_file = load_providers_file(settings.providers_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        limits = httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive,
        )
        async with httpx.AsyncClient(transport=upstream_transport, limits=limits) as client:
            app.state.router = Router(ProviderRegistry.from_config(providers_file, client))
            yield

    app = FastAPI(title="Tollgate", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    return app
