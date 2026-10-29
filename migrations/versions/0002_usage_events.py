"""The usage ledger (SPEC 5.1, phase 1.6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, not imported: a migration must keep meaning what it meant.
STATUSES = (
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


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("saved_cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_status", sa.Text(), server_default=sa.text("'MISS'"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
        sa.UniqueConstraint("request_id", name="uq_usage_events_request_id"),
        sa.CheckConstraint(_one_of("status", STATUSES), name="ck_usage_events_status"),
        sa.CheckConstraint(
            _one_of("cache_status", CACHE_STATUSES), name="ck_usage_events_cache_status"
        ),
    )
    op.create_index(
        "ix_usage_events_tenant_id_created_at", "usage_events", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_tenant_id_created_at", table_name="usage_events")
    op.drop_table("usage_events")
