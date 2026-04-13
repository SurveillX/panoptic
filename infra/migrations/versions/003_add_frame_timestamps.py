"""Add frame_timestamps column to vil_summaries.

Stores ISO-8601 timestamps of frames that were actually fetched and sent
to the LLM.  Enables retrieval of the same frames from the trailer later
for debugging, auditing, or re-processing.

Revision ID: 003
Revises: 002
Create Date: 2026-04-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vil_summaries",
        sa.Column(
            "frame_timestamps",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("vil_summaries", "frame_timestamps")
