"""Resolves a request's API key to its TenantContext (request lifecycle step 2),
applying the tenant's rate limits in the same Redis call when asked (step 4),
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
from tollgate.deps import get_key_cache, get_limiter, get_sessions, get_settings
from tollgate.errors import TollgateError
from tollgate.ratelimit.limiter import Decision, RateLimiter

log = logging.getLogger(__name__)


def _bearer_token(header: str | None) -> str | None:
    scheme, _, token = (header or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def _invalid_key() -> TollgateError:
    return TollgateError(
        401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key"
    )


def presented_key(request: Request) -> str:
    """The request's API key, if it is one at all. No I/O: a missing or malformed
    key costs nothing, not even reading the body."""
    raw_key = _bearer_token(request.headers.get("authorization"))
    if raw_key is None:
        raise TollgateError(
            401,
            "No API key provided. Send it as 'Authorization: Bearer <key>'.",
            "invalid_request_error",
            "missing_api_key",
        )
    if not keys.is_well_formed(raw_key):
        raise _invalid_key()
    return raw_key


async def require_tenant(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    cache: Annotated[KeyCache, Depends(get_key_cache)],
    limiter: Annotated[RateLimiter, Depends(get_limiter)],
) -> TenantContext:
    """For routes that authenticate but aren't rate limited."""
    context, _ = await authenticate(
        request, presented_key(request), settings, sessions, cache, limiter
    )
    return context


async def authenticate(
    request: Request,
    raw_key: str,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    cache: KeyCache,
    limiter: RateLimiter,
    *,
    reservation: int | None = None,
) -> tuple[TenantContext, Decision | None]:
    """The key's tenant, and with a TPM `reservation` the rate-limit decision: from
    the same Redis call when the key is cached, one more call after a miss."""
    with request.state.timings.stage("auth"):
        return await _authenticate(raw_key, settings, sessions, cache, limiter, reservation)


async def _authenticate(
    raw_key: str,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    cache: KeyCache,
    limiter: RateLimiter,
    reservation: int | None,
) -> tuple[TenantContext, Decision | None]:
    key_hash = keys.hash_key(raw_key, settings.key_pepper.get_secret_value())
    try:
        context, limits = await _resolve(key_hash, sessions, cache, limiter, reservation)
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
    return context, limits


async def _resolve(
    key_hash: str,
    sessions: async_sessionmaker[AsyncSession],
    cache: KeyCache,
    limiter: RateLimiter,
    reservation: int | None,
) -> tuple[TenantContext | None, Decision | None]:
    seen = await cache.lookup(key_hash, reservation=reservation)
    if seen.context is not None:
        return seen.context, seen.decision
    async with sessions() as session, session.begin():
        context = await tenants.lookup_active_key(session, key_hash)
    if context is None:
        return None, None
    await cache.store(context, seen)
    if reservation is None:
        return context, None
    limits = await limiter.check(
        context.tenant_id,
        rpm_limit=context.rpm_limit,
        tpm_limit=context.tpm_limit,
        reservation=reservation,
    )
    return context, limits


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
