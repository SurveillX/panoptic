"""Add panoptic_camera_aliases — inert mapping table for Option B (plan D-2).

Deployed empty. When a trailer emits mismatched camera_ids across bucket
vs image payloads for the same physical camera, insert an alias row to
collapse them into a single canonical id. Zero behavior change until a
row is inserted.

Revision ID: 009
Revises: 008
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panoptic_camera_aliases",
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("raw_camera_id", sa.Text(), nullable=False),
        # payload_type in ('bucket', 'image') — same raw_camera_id can map
        # differently per payload source.
        sa.Column("payload_type", sa.Text(), nullable=False),
        sa.Column("canonical_camera_id", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "serial_number",
            "raw_camera_id",
            "payload_type",
            name="pk_panoptic_camera_aliases",
        ),
    )


def downgrade() -> None:
    op.drop_table("panoptic_camera_aliases")
