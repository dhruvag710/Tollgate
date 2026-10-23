"""Admin API: tenants."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from fastapi import Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.api.admin.common import admin_router, invalidate_cached_keys, not_found
from tollgate.auth.cache import KeyCache
from tollgate.db import tenants
from tollgate.deps import get_key_cache, get_router, get_sessions
from tollgate.errors import TollgateError
from tollgate.routing.router import Router

router = admin_router()

Name = Annotated[str, Field(min_length=1, max_length=200)]
Limit = Annotated[int, Field(ge=1, le=2**31 - 1)]  # an INT column
Budget = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=4)]


class TenantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    rpm_limit: Limit = 60
    tpm_limit: Limit = 100_000
    monthly_budget_usd: Budget | None = None  # None: unlimited
    allowed_models: list[str] | None = None  # None: every model
    cache_enabled: bool = True


class TenantPatch(BaseModel):
    """Only the fields sent change. null clears monthly_budget_usd (unlimited)
    and allowed_models (every model); the other fields can't be null."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    rpm_limit: Limit | None = None
    tpm_limit: Limit | None = None
    monthly_budget_usd: Budget | None = None
    allowed_models: list[str] | None = None
    cache_enabled: bool | None = None

    @model_validator(mode="after")
    def _required_fields_stay_set(self) -> Self:
        for field in ("name", "rpm_limit", "tpm_limit", "cache_enabled"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} can't be null")
        return self


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    rpm_limit: int
    tpm_limit: int
    monthly_budget_usd: Decimal | None
    allowed_models: list[str] | None
    cache_enabled: bool
    created_at: datetime


def _check_models(models: list[str] | None, router_: Router) -> None:
    unknown = sorted(set(models or ()) - router_.registry.models.keys())
    if unknown:
        raise TollgateError(
            400,
            f"Unknown model(s): {', '.join(unknown)}",
            "invalid_request_error",
            "unknown_model",
        )


def _name_taken(name: str) -> TollgateError:
    return TollgateError(
        409, f"A tenant named {name!r} already exists.", "invalid_request_error", "tenant_exists"
    )


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantIn,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    router_: Annotated[Router, Depends(get_router)],
) -> TenantOut:
    _check_models(body.allowed_models, router_)
    try:
        async with sessions() as session, session.begin():
            tenant = await tenants.create_tenant(session, **body.model_dump())
    except IntegrityError as exc:
        raise _name_taken(body.name) from exc
    return TenantOut.model_validate(tenant)


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantPatch,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    router_: Annotated[Router, Depends(get_router)],
    cache: Annotated[KeyCache, Depends(get_key_cache)],
) -> TenantOut:
    changes = body.model_dump(exclude_unset=True)
    _check_models(changes.get("allowed_models"), router_)
    try:
        async with sessions() as session, session.begin():
            tenant = await tenants.update_tenant(session, tenant_id, changes)
    except IntegrityError as exc:
        raise _name_taken(changes["name"]) from exc
    if tenant is None:
        raise not_found("tenant", "tenant_not_found")
    await invalidate_cached_keys(cache, str(tenant.id))
    return TenantOut.model_validate(tenant)
