"""Resolves a request's API key to its TenantContext (request lifecycle step 2),
and guards the admin API."""

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth import keys
from tollgate.auth.cache import KeyCache
from tollgate.auth.context import TenantContext
from tollgate.config import Settings
from tollgate.db import tenants
from tollgate.deps import get_key_cache, get_sessions, get_settings
from tollgate.errors import TollgateError

log = logging.getLogger(__name__)


def _bearer_token(header: str | None) -> str | None:
    scheme, _, token = (header or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def _invalid_key() -> TollgateError:
    return TollgateError(
        401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key"
    )


async def require_tenant(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    cache: Annotated[KeyCache, Depends(get_key_cache)],
) -> TenantContext:
    with request.state.timings.stage("auth"):
        return await _authenticate(request, settings, sessions, cache)


async def _authenticate(
    request: Request,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    cache: KeyCache,
) -> TenantContext:
    raw_key = _bearer_token(request.headers.get("authorization"))
    if raw_key is None:
        raise TollgateError(
            401,
            "No API key provided. Send it as 'Authorization: Bearer <key>'.",
            "invalid_request_error",
            "missing_api_key",
        )
    if not keys.is_well_formed(raw_key):
        raise _invalid_key()  # without touching Redis or Postgres
    key_hash = keys.hash_key(raw_key, settings.key_pepper.get_secret_value())
    try:
        context = await _resolve(key_hash, sessions, cache)
    except (RedisError, SQLAlchemyError, OSError, TimeoutError) as exc:
        # Fail closed: without Redis or Postgres no key can be checked.
        log.warning("auth backend unavailable: %s", type(exc).__name__)
        raise TollgateError(
            503,
            "Authentication is temporarily unavailable; retry the request.",
            "api_error",
            "auth_unavailable",
        ) from exc
    if context is None or not keys.hashes_match(context.key_hash, key_hash):
        raise _invalid_key()
    return context


async def _resolve(
    key_hash: str, sessions: async_sessionmaker[AsyncSession], cache: KeyCache
) -> TenantContext | None:
    seen = await cache.lookup(key_hash)
    if seen.context is not None:
        return seen.context
    async with sessions() as session, session.begin():
        context = await tenants.lookup_active_key(session, key_hash)
    if context is not None:
        await cache.store(context, seen)
    return context


async def require_admin(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    if settings.admin_token is None:
        raise TollgateError(
            503,
            "The admin API is disabled; set ADMIN_TOKEN to enable it.",
            "api_error",
            "admin_disabled",
        )
    token = _bearer_token(request.headers.get("authorization"))
    expected = settings.admin_token.get_secret_value()
    if token is None or not hmac.compare_digest(token.encode(), expected.encode()):
        raise TollgateError(
            401, "Invalid admin token.", "invalid_request_error", "invalid_admin_token"
        )
