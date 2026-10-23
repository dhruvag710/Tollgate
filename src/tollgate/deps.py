"""FastAPI dependencies resolving objects created in the app factory and lifespan."""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth.cache import KeyCache
from tollgate.config import Settings
from tollgate.routing.router import Router
from tollgate.usage.ledger import Metering


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_router(request: Request) -> Router:
    router: Router = request.app.state.router
    return router


def get_metering(request: Request) -> Metering:
    metering: Metering = request.app.state.metering
    return metering


def get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    return sessions


def get_key_cache(request: Request) -> KeyCache:
    cache: KeyCache = request.app.state.key_cache
    return cache
