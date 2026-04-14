"""Add vil_images table for trailer-pushed image storage and enrichment.

Revision ID: 004
Revises: 003
Create Date: 2026-04-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vil_images",
        # image_id is a deterministic SHA256 — natural primary key.
        sa.Column("image_id", sa.Text(), nullable=False),
        # Stored for traceability / debugging, not used for dedup.
        sa.Column("event_id", sa.Text(), nullable=False),

        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("camera_id", sa.Text(), nullable=False),
        # "{serial_number}:{camera_id}"
        sa.Column("scope_id", sa.Text(), nullable=False),

        sa.Column("bucket_start_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("bucket_end_utc", sa.TIMESTAMP(timezone=True), nullable=False),

        sa.Column("captured_at_utc", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=True),

        # 'alert' | 'anomaly' | 'baseline'
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column(
            "selection_policy_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'1'"),
        ),

        sa.Column(
            "context_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'image/jpeg'"),
        ),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),

        # Caption enrichment (async)
        sa.Column(
            "caption_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("caption_model", sa.Text(), nullable=True),
        sa.Column("caption_text", sa.Text(), nullable=True),

        # Caption embedding enrichment (async, after caption)
        sa.Column(
            "caption_embedding_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("caption_embedding_model", sa.Text(), nullable=True),
        sa.Column("caption_embedding_vector_id", sa.Text(), nullable=True),

        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'trailer_push'"),
        ),
        sa.Column(
            "is_searchable",
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

        sa.PrimaryKeyConstraint("image_id"),
    )

    op.create_index(
        "ix_vil_images_scope_time",
        "vil_images",
        ["scope_id", sa.text("bucket_start_utc DESC")],
    )
    op.create_index(
        "ix_vil_images_sn_camera_time",
        "vil_images",
        ["serial_number", "camera_id", sa.text("bucket_start_utc DESC")],
    )
    op.create_index(
        "ix_vil_images_trigger_time",
        "vil_images",
        ["trigger", sa.text("bucket_start_utc DESC")],
    )
    op.create_index(
        "ix_vil_images_created_at",
        "vil_images",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("vil_images")
