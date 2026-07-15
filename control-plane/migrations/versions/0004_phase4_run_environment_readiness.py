"""add phase 4 run environment readiness metadata

Revision ID: 0004_phase4_run_environment_readiness
Revises: 0003_phase2_metrics
Create Date: 2026-06-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_phase4_run_environment_readiness"
down_revision = "0003_phase2_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "environment_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "readiness_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "readiness_checklist")
    op.drop_column("runs", "environment_metadata")
