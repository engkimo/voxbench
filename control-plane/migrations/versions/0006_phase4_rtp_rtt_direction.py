"""add safe direction and rtt fields to rtp stats

Revision ID: 0006_phase4_rtp_rtt_direction
Revises: 0005_phase4_sip_rtp_ingest
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_phase4_rtp_rtt_direction"
down_revision = "0005_phase4_sip_rtp_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rtp_stats", sa.Column("direction", sa.String(length=16), nullable=True))
    op.add_column("rtp_stats", sa.Column("rtt_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("rtp_stats", "rtt_ms")
    op.drop_column("rtp_stats", "direction")
