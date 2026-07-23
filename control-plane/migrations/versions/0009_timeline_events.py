"""add persisted typed timeline events

Revision ID: 0009_timeline_events
Revises: 0008_run_job_leases
Create Date: 2026-07-23 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_timeline_events"
down_revision = "0008_run_job_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "timeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clock_domain", sa.String(length=64), nullable=False),
        sa.Column("alignment_uncertainty_ms", sa.Float(), nullable=True),
        sa.Column("direction", sa.String(length=32), nullable=True),
        sa.Column("stage", sa.String(length=255), nullable=True),
        sa.Column("stream_alias", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("correlation_alias", sa.String(length=128), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "event_id",
            name="uq_timeline_events_run_event",
        ),
    )


def downgrade() -> None:
    op.drop_table("timeline_events")
