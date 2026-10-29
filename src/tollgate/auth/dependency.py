"""Resolves a request's API key to its TenantContext (request lifecycle step 2),
admitting the request by its tenant's budget and rate limits in the same Redis
call when asked (steps 3 and 4), and guards the admin API."""

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
from tollgate.usage.budget import Budgets

log = logging.getLogger(__name__)

# Refills of a missing spend counter per request: one, plus one more if the
# month rolled over in between.
MAX_SPEND_REFILLS = 2


class SpendUnknown(Exception):
    """The month's spend counter vanished again after being refilled."""


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
    budgets: Budgets | None = None,
    reservation: int | None = None,
) -> tuple[TenantContext, Decision | None]:
    """The key's tenant, and with a TPM `reservation` (and `budgets`) the
    admission decision: from the same Redis call when the key is cached, one
    more call after a miss, and a refill when the month's spend counter is
    missing."""
    with request.state.timings.stage("auth"):
        return await _authenticate(
            raw_key, settings, sessions, cache, limiter, budgets, reservation
        )


async def _authenticate(
    raw_key: str,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    cache: KeyCache,
    limiter: RateLimiter,
    budgets: Budgets | None,
    reservation: int | None,
) -> tuple[TenantContext, Decision | None]:
    key_hash = keys.hash_key(raw_key, settings.key_pepper.get_secret_value())
    try:
        context, limits = await _resolve(key_hash, sessions, cache, limiter, reservation)
        if context is not None and reservation is not None:
            assert budgets is not None, "admitting a request needs budgets"
            limits = await _admit(context, limits, limiter, budgets, reservation)
    except (RedisError, SQLAlchemyError, OSError, TimeoutError, SpendUnknown) as exc:
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
    return context, None


async def _admit(
    context: TenantContext,
    limits: Decision | None,
    limiter: RateLimiter,
    budgets: Budgets,
    reservation: int,
) -> Decision:
    """`limits` from the cached lookup, or the limiter's own check after a miss;
    a missing spend counter is refilled from the ledger and the check repeated."""
    refills = 0
    while limits is None or limits.outcome == "spend_unknown":
        if limits is not None:
            if refills == MAX_SPEND_REFILLS:
                raise SpendUnknown
            await budgets.refill(context.tenant_id, limits.month)
            refills += 1
        limits = await limiter.check(
            context.tenant_id,
            budget_usd=context.monthly_budget_usd,
            rpm_limit=context.rpm_limit,
            tpm_limit=context.tpm_limit,
            reservation=reservation,
        )
    return limits


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
