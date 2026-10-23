"""Shared by the admin routes. Every admin route needs the admin token."""

from fastapi import APIRouter, Depends
from redis.exceptions import RedisError

from tollgate.auth.cache import KeyCache
from tollgate.auth.dependency import require_admin
from tollgate.errors import TollgateError


def admin_router() -> APIRouter:
    return APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


async def invalidate_cached_keys(
    cache: KeyCache, tenant_id: str, *, key_hash: str | None = None
) -> None:
    """Call after the change is committed (see KeyCache.invalidate). If Redis is
    unreachable the change stands but cached keys may not see it for up to the
    cache TTL, so the admin is told to retry, which is safe: every change is
    idempotent."""
    try:
        await cache.invalidate(tenant_id, key_hash=key_hash)
    except (RedisError, OSError, TimeoutError) as exc:
        raise TollgateError(
            503,
            "The change is saved, but cached keys could not be invalidated; retry the request.",
            "api_error",
            "cache_invalidation_failed",
        ) from exc


def not_found(what: str, code: str) -> TollgateError:
    return TollgateError(404, f"No such {what}.", "invalid_request_error", code)
