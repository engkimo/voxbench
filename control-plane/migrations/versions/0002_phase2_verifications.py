"""add phase 2 verification results

Revision ID: 0002_phase2_verifications
Revises: 0001_phase0_registry
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_phase2_verifications"
down_revision = "0001_phase0_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=255), nullable=False),
        sa.Column("invariant", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("verifications")
