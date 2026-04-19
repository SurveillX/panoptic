"""Add panoptic_reports table — M9 async report generation.

See plans/please-put-a-plan-jazzy-sunrise.md (M9 kickoff plan rev 2).

One row per (serial_number, kind, window_start_utc) tuple. report_id is
content-addressed as sha256(serial, kind, window_start, window_end);
regenerating against the same window updates the existing row rather
than creating a duplicate. template_version lives in metadata_json and
is NOT part of the identity hash.

Revision ID: 010
Revises: 009
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panoptic_reports",
        sa.Column("report_id", sa.Text(), primary_key=True),
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),  # 'daily' | 'weekly'
        sa.Column("window_start_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        # NULL until status='success'
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),  # pending | running | success | failed
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "serial_number",
            "kind",
            "window_start_utc",
            name="uq_panoptic_reports_sn_kind_window",
        ),
    )

    op.create_index(
        "ix_panoptic_reports_sn_kind_window",
        "panoptic_reports",
        ["serial_number", "kind", sa.text("window_start_utc DESC")],
    )
    op.create_index(
        "ix_panoptic_reports_status",
        "panoptic_reports",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_panoptic_reports_status", table_name="panoptic_reports")
    op.drop_index("ix_panoptic_reports_sn_kind_window", table_name="panoptic_reports")
    op.drop_table("panoptic_reports")
