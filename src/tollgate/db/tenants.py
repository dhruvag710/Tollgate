"""Tenant and API key storage. Callers own the transaction."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from tollgate.auth import keys
from tollgate.auth.context import TenantContext
from tollgate.db.models import ApiKey, Tenant


@dataclass(frozen=True)
class IssuedKey:
    """A key just created. Its raw form exists only here and is never stored."""

    key: ApiKey
    raw_key: str


async def create_tenant(session: AsyncSession, **fields: Any) -> Tenant:
    """Raises IntegrityError if the name is taken."""
    tenant = Tenant(**fields)
    session.add(tenant)
    await session.flush()
    await session.refresh(tenant)  # load the server-side defaults
    return tenant


async def update_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, changes: dict[str, Any]
) -> Tenant | None:
    """Apply `changes` to the tenant; None if it doesn't exist. Raises
    IntegrityError if a new name is taken."""
    tenant = await session.get(Tenant, tenant_id, with_for_update=True)
    if tenant is None:
        return None
    for name, value in changes.items():
        setattr(tenant, name, value)
    await session.flush()
    return tenant


async def create_api_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    pepper: str,
    name: str | None = None,
    raw_key: str | None = None,  # for seeding known keys (tests, dev); generated otherwise
) -> IssuedKey | None:
    """None if the tenant doesn't exist."""
    if await session.get(Tenant, tenant_id) is None:
        return None
    raw = raw_key or keys.generate_key()
    key = ApiKey(
        tenant_id=tenant_id,
        key_prefix=keys.display_prefix(raw),
        key_hash=keys.hash_key(raw, pepper),
        name=name,
    )
    session.add(key)
    await session.flush()
    await session.refresh(key)
    return IssuedKey(key, raw)


async def revoke_api_key(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    """Revoke the key; revoking it again changes nothing. None if it doesn't exist."""
    key = await session.get(ApiKey, key_id, with_for_update=True)
    if key is None:
        return None
    if key.revoked_at is None:
        key.revoked_at = func.now()  # set by the database clock
        await session.flush()
        await session.refresh(key)
    return key


async def lookup_active_key(session: AsyncSession, key_hash: str) -> TenantContext | None:
    """The unrevoked key with this hash, with its tenant; stamps last_used_at.

    One statement, run only on a key-cache miss, so last_used_at is accurate to
    about the cache TTL.
    """
    stmt = (
        update(ApiKey)
        .where(
            ApiKey.key_hash == key_hash,
            ApiKey.revoked_at.is_(None),
            ApiKey.tenant_id == Tenant.id,
        )
        .values(last_used_at=func.now())
        .returning(
            ApiKey.id,
            ApiKey.key_hash,
            Tenant.id,
            Tenant.name,
            Tenant.rpm_limit,
            Tenant.tpm_limit,
            Tenant.monthly_budget_usd,
            Tenant.allowed_models,
            Tenant.cache_enabled,
        )
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    key_id, stored_hash, tenant_id, name, rpm, tpm, budget, models, cache_enabled = row
    return TenantContext(
        tenant_id=str(tenant_id),
        tenant_name=name,
        api_key_id=str(key_id),
        key_hash=stored_hash,
        rpm_limit=rpm,
        tpm_limit=tpm,
        monthly_budget_usd=budget,
        allowed_models=None if models is None else tuple(models),
        cache_enabled=cache_enabled,
    )
