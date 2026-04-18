"""Add panoptic_events table — unified event layer (image-trigger + bucket-marker).

See panoptic_events_design_spec_v2.md §4 and panoptic_events_implementation_plan.md P1.

event_id is content-addressed; (source_type, originating evidence, marker_name)
must be in the hash payload, enrichment fields (bucket_id resolution, image
correlation) must not.

Revision ID: 008
Revises: 007
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panoptic_events",
        sa.Column("event_id", sa.Text(), primary_key=True),

        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("camera_id", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),

        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_source", sa.Text(), nullable=False),

        sa.Column("severity", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),

        sa.Column(
            "start_time_utc",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "end_time_utc",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "event_time_utc",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),

        sa.Column("bucket_id", sa.Text(), nullable=True),
        sa.Column("image_id", sa.Text(), nullable=True),

        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
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
    )

    op.create_index(
        "ix_panoptic_events_scope_time",
        "panoptic_events",
        ["scope_id", sa.text("event_time_utc DESC")],
    )
    op.create_index(
        "ix_panoptic_events_sn_camera_time",
        "panoptic_events",
        ["serial_number", "camera_id", sa.text("event_time_utc DESC")],
    )
    op.create_index(
        "ix_panoptic_events_type_time",
        "panoptic_events",
        ["event_type", sa.text("event_time_utc DESC")],
    )
    op.create_index(
        "ix_panoptic_events_source_time",
        "panoptic_events",
        ["event_source", sa.text("event_time_utc DESC")],
    )
    op.create_index(
        "ix_panoptic_events_created_at",
        "panoptic_events",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_panoptic_events_created_at", table_name="panoptic_events")
    op.drop_index("ix_panoptic_events_source_time", table_name="panoptic_events")
    op.drop_index("ix_panoptic_events_type_time", table_name="panoptic_events")
    op.drop_index("ix_panoptic_events_sn_camera_time", table_name="panoptic_events")
    op.drop_index("ix_panoptic_events_scope_time", table_name="panoptic_events")
    op.drop_table("panoptic_events")
