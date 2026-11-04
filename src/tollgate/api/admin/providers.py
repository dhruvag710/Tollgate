"""Admin API: the providers' circuit breakers (SPEC §6)."""

from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel
from redis.exceptions import RedisError

from tollgate.api.admin.common import admin_router
from tollgate.deps import get_router
from tollgate.errors import TollgateError
from tollgate.routing.router import Router

router = admin_router()


class ProviderOut(BaseModel):
    name: str
    state: Literal["closed", "open", "half_open"]
    # Attempts counted in the breaker's current window, while closed.
    requests: int
    failures: int
    # Open: seconds until a request may probe the provider. Half-open: until
    # the probe's lease runs out. Closed: null.
    retry_in_s: float | None


class ProvidersOut(BaseModel):
    providers: list[ProviderOut]


@router.get("/providers")
async def list_providers(router_: Annotated[Router, Depends(get_router)]) -> ProvidersOut:
    """Every configured provider's breaker, as Redis holds it now."""
    try:
        states = await router_.breakers.states(router_.registry.providers)
    except (RedisError, OSError, TimeoutError) as exc:
        raise TollgateError(
            503,
            "Breaker states are unavailable; retry the request.",
            "api_error",
            "breakers_unavailable",
        ) from exc
    return ProvidersOut(
        providers=[
            ProviderOut(
                name=s.provider,
                state=s.state,
                requests=s.requests,
                failures=s.failures,
                retry_in_s=s.retry_in_s,
            )
            for s in states
        ]
    )
