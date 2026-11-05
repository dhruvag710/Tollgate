"""ORM models. The schema itself is owned by the Alembic migrations in
migrations/; a test checks that the two agree."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
        }
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, unique=True)
    rpm_limit: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    tpm_limit: Mapped[int] = mapped_column(Integer, server_default=text("100000"))
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))  # None: unlimited
    allowed_models: Mapped[list[str] | None] = mapped_column(ARRAY(Text))  # None: every model
    cache_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key_prefix: Mapped[str] = mapped_column(Text)  # e.g. "tg_live_ab12cd", for display
    key_hash: Mapped[str] = mapped_column(Text, unique=True)  # HMAC-SHA256(pepper, raw key)
    name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Every outcome a usage event can record (usage.ledger.UsageStatus).
USAGE_STATUSES = (
    "success",
    "provider_error",
    "client_disconnect",
    "gateway_shutdown",
    "gateway_error",
    "rate_limited",
    "budget_exceeded",
)
CACHE_STATUSES = ("HIT", "MISS", "BYPASS")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class UsageEventRow(Base):
    """One request's usage: the ledger (SPEC 5.1). No foreign keys: rows outlive
    the tenants and keys they name, and inserts never wait on them."""

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(_one_of("status", USAGE_STATUSES), name="status"),
        CheckConstraint(_one_of("cache_status", CACHE_STATUSES), name="cache_status"),
        Index("ix_usage_events_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)  # idempotency
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # True: the provider reported no usage; the tokens are tiktoken estimates.
    usage_estimated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    saved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))  # v2
    cache_status: Mapped[str] = mapped_column(Text, server_default=text("'MISS'"))
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)  # time to first token
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# The embedding models the semantic cache may use all have this many dimensions.
EMBEDDING_DIMENSIONS = 384


class CacheEntryRow(Base):
    """A semantic cache entry (SPEC 5.1, Phase 2.4): an answer, found by the
    embedding of the question it answered, within its tenant and scope."""

    __tablename__ = "cache_entries"
    __table_args__ = (
        # Lookups search one tenant's scope exactly; no index on the embedding
        # (see migrations/versions/0003_cache_entries.py).
        Index("ix_cache_entries_tenant_id_scope_hash", "tenant_id", "scope_hash"),
        Index("ix_cache_entries_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    scope_hash: Mapped[str] = mapped_column(Text)
    query_text: Mapped[str] = mapped_column(Text)  # the last user message
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the cached completion
    completion_tokens: Mapped[int] = mapped_column(Integer)
    hit_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
