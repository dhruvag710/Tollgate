"""Admin API: API keys."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Body, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.api.admin.common import admin_router, invalidate_cached_keys, not_found
from tollgate.auth.cache import KeyCache
from tollgate.config import Settings
from tollgate.db import tenants
from tollgate.deps import get_key_cache, get_sessions, get_settings

router = admin_router()


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)


class IssuedKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str | None
    key_prefix: str
    created_at: datetime
    key: str  # shown this once; only its hash is stored


@router.post("/tenants/{tenant_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(
    tenant_id: uuid.UUID,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[KeyIn | None, Body()] = None,
) -> IssuedKeyOut:
    async with sessions() as session, session.begin():
        issued = await tenants.create_api_key(
            session,
            tenant_id,
            pepper=settings.key_pepper.get_secret_value(),
            name=body.name if body else None,
        )
    if issued is None:
        raise not_found("tenant", "tenant_not_found")
    key = issued.key
    return IssuedKeyOut(
        id=key.id,
        tenant_id=key.tenant_id,
        name=key.name,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
        key=issued.raw_key,
    )


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    cache: Annotated[KeyCache, Depends(get_key_cache)],
) -> Response:
    async with sessions() as session, session.begin():
        key = await tenants.revoke_api_key(session, key_id)
    if key is None:
        raise not_found("API key", "key_not_found")
    # Also for a key already revoked: a retry after a failed invalidation.
    await invalidate_cached_keys(cache, str(key.tenant_id), key_hash=key.key_hash)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
