"""Add image_embedding_* columns to panoptic_images for VL image-native retrieval.

M5 scope. Mirrors the caption_embedding_* trio on the same table, this
time for Qwen3-VL-Embedding-8B vectors produced by `embed_visual`.

Revision ID: 007
Revises: 006
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "panoptic_images",
        sa.Column(
            "image_embedding_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "panoptic_images",
        sa.Column("image_embedding_model", sa.Text(), nullable=True),
    )
    op.add_column(
        "panoptic_images",
        sa.Column("image_embedding_vector_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("panoptic_images", "image_embedding_vector_id")
    op.drop_column("panoptic_images", "image_embedding_model")
    op.drop_column("panoptic_images", "image_embedding_status")
