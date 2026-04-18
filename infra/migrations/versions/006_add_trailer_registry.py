"""Add panoptic_trailers table — known-trailer registry for HMAC auth.

See docs/AUTH_DESIGN.md §10.

Revision ID: 006
Revises: 005
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panoptic_trailers",
        sa.Column("serial_number", sa.Text(), nullable=False, primary_key=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_panoptic_trailers_is_active",
        "panoptic_trailers",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_panoptic_trailers_is_active", table_name="panoptic_trailers")
    op.drop_table("panoptic_trailers")
