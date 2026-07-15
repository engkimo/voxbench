"""add phase 4 sip rtp ingest tables

Revision ID: 0005_phase4_sip_rtp_ingest
Revises: 0004_phase4_run_environment_readiness
Create Date: 2026-07-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_phase4_sip_rtp_ingest"
down_revision = "0004_phase4_run_environment_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sip_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("summary_alias", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rtp_stats",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("jitter_ms", sa.Float(), nullable=True),
        sa.Column("loss_pct", sa.Float(), nullable=True),
        sa.Column("mos", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("rtp_stats")
    op.drop_table("sip_events")
