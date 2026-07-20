"""persist the complete run reconstruction state

Revision ID: 0007_run_runtime_state
Revises: 0006_phase4_rtp_rtt_direction
Create Date: 2026-07-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_run_runtime_state"
down_revision = "0006_phase4_rtp_rtt_direction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("failure_alias", sa.String(length=128), nullable=True))
    op.add_column(
        "runs",
        sa.Column(
            "resolved_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    for table_name in (
        "recordings",
        "spans",
        "metrics",
        "verifications",
        "sip_events",
        "rtp_stats",
    ):
        op.add_column(
            table_name,
            sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    for table_name in (
        "rtp_stats",
        "sip_events",
        "verifications",
        "metrics",
        "spans",
        "recordings",
    ):
        op.drop_column(table_name, "ordinal")
    op.drop_column("runs", "resolved_config")
    op.drop_column("runs", "failure_alias")
