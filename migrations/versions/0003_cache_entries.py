"""The semantic cache's entries (SPEC 5.1, phase 2.4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, not imported: a migration must keep meaning what it meant.
DIMENSIONS = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "cache_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_hash", sa.Text(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cache_entries")),
    )
    # A lookup searches one tenant's entries in one scope, exactly: no
    # approximate (HNSW) index on the embedding, which, filtered by tenant,
    # missed the tenant's nearest entry when many stored vectors were
    # identical to the query (SPEC Phase 2.4).
    op.create_index(
        "ix_cache_entries_tenant_id_scope_hash", "cache_entries", ["tenant_id", "scope_hash"]
    )
    # For sweeping out expired entries.
    op.create_index("ix_cache_entries_expires_at", "cache_entries", ["expires_at"])


def downgrade() -> None:
    op.drop_table("cache_entries")
    # The extension stays: dropping it could break anything else that uses it.
