"""FastAPI dependencies resolving objects created in the app factory and lifespan.

All async: FastAPI runs a plain `def` dependency in its thread pool, a thread
round trip per request for what is only an attribute read."""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth.cache import KeyCache
from tollgate.config import Settings
from tollgate.ratelimit.limiter import RateLimiter
from tollgate.routing.router import Router
from tollgate.usage.budget import Budgets
from tollgate.usage.ledger import Metering


async def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_router(request: Request) -> Router:
    router: Router = request.app.state.router
    return router


async def get_metering(request: Request) -> Metering:
    metering: Metering = request.app.state.metering
    return metering


async def get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    return sessions


async def get_key_cache(request: Request) -> KeyCache:
    cache: KeyCache = request.app.state.key_cache
    return cache


async def get_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.limiter
    return limiter


async def get_budgets(request: Request) -> Budgets:
    budgets: Budgets = request.app.state.budgets
    return budgets
