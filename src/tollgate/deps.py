"""FastAPI dependencies resolving objects created in the app factory and lifespan."""

from fastapi import Request

from tollgate.config import Settings
from tollgate.routing.router import Router
from tollgate.streaming.sse import OpenStreams
from tollgate.usage.ledger import Ledger


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_router(request: Request) -> Router:
    router: Router = request.app.state.router
    return router


def get_ledger(request: Request) -> Ledger:
    ledger: Ledger = request.app.state.ledger
    return ledger


def get_streams(request: Request) -> OpenStreams:
    streams: OpenStreams = request.app.state.streams
    return streams
