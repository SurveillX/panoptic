"""Identity refactor: tenant_id → serial_number, drop site_id/trailer_id.

The unique camera identity is (serial_number, camera_id).
serial_number identifies the trailer; camera_id identifies a camera within it.

Revision ID: 002
Revises: 001
Create Date: 2026-04-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # vil_buckets: tenant_id → serial_number, drop site_id + trailer_id
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_buckets_tenant_camera_start", table_name="vil_buckets")
    op.drop_index("ix_vil_buckets_tenant_start_desc", table_name="vil_buckets")

    op.alter_column("vil_buckets", "tenant_id", new_column_name="serial_number")
    op.drop_column("vil_buckets", "site_id")
    op.drop_column("vil_buckets", "trailer_id")

    op.create_index(
        "ix_vil_buckets_sn_camera_start",
        "vil_buckets",
        ["serial_number", "camera_id", "bucket_start_utc"],
    )
    op.create_index(
        "ix_vil_buckets_sn_start_desc",
        "vil_buckets",
        ["serial_number", sa.text("bucket_start_utc DESC")],
    )

    # ------------------------------------------------------------------
    # vil_jobs: tenant_id → serial_number
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_jobs_tenant_state", table_name="vil_jobs")
    op.alter_column("vil_jobs", "tenant_id", new_column_name="serial_number")
    op.create_index(
        "ix_vil_jobs_sn_state",
        "vil_jobs",
        ["serial_number", "state"],
    )

    # ------------------------------------------------------------------
    # vil_job_history: tenant_id → serial_number
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_job_history_tenant_created", table_name="vil_job_history")
    op.alter_column("vil_job_history", "tenant_id", new_column_name="serial_number")
    op.create_index(
        "ix_vil_job_history_sn_created",
        "vil_job_history",
        ["serial_number", sa.text("created_at DESC")],
    )

    # ------------------------------------------------------------------
    # vil_summaries: tenant_id → serial_number
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_summaries_tenant_level_scope_latest", table_name="vil_summaries")
    op.drop_index("ix_vil_summaries_tenant_start_desc", table_name="vil_summaries")
    op.alter_column("vil_summaries", "tenant_id", new_column_name="serial_number")
    op.create_index(
        "ix_vil_summaries_sn_level_scope_latest",
        "vil_summaries",
        ["serial_number", "level", "scope_id", "is_latest"],
    )
    op.create_index(
        "ix_vil_summaries_sn_start_desc",
        "vil_summaries",
        ["serial_number", sa.text("start_time DESC")],
    )

    # ------------------------------------------------------------------
    # vil_rollup_state: tenant_id → serial_number
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_rollup_state_tenant_level_start", table_name="vil_rollup_state")
    op.drop_index("ix_vil_rollup_state_tenant_stale", table_name="vil_rollup_state")
    op.alter_column("vil_rollup_state", "tenant_id", new_column_name="serial_number")
    op.create_index(
        "ix_vil_rollup_state_sn_level_start",
        "vil_rollup_state",
        ["serial_number", "level", "window_start"],
    )
    op.create_index(
        "ix_vil_rollup_state_sn_stale",
        "vil_rollup_state",
        ["serial_number", "stale"],
    )

    # ------------------------------------------------------------------
    # vil_embedding_backlog: tenant_id → serial_number
    # ------------------------------------------------------------------
    op.drop_index("ix_vil_embedding_backlog_tenant_next", table_name="vil_embedding_backlog")
    op.alter_column("vil_embedding_backlog", "tenant_id", new_column_name="serial_number")
    op.create_index(
        "ix_vil_embedding_backlog_sn_next",
        "vil_embedding_backlog",
        ["serial_number", "next_attempt_at"],
    )


def downgrade() -> None:
    # vil_embedding_backlog
    op.drop_index("ix_vil_embedding_backlog_sn_next", table_name="vil_embedding_backlog")
    op.alter_column("vil_embedding_backlog", "serial_number", new_column_name="tenant_id")
    op.create_index(
        "ix_vil_embedding_backlog_tenant_next",
        "vil_embedding_backlog",
        ["tenant_id", "next_attempt_at"],
    )

    # vil_rollup_state
    op.drop_index("ix_vil_rollup_state_sn_stale", table_name="vil_rollup_state")
    op.drop_index("ix_vil_rollup_state_sn_level_start", table_name="vil_rollup_state")
    op.alter_column("vil_rollup_state", "serial_number", new_column_name="tenant_id")
    op.create_index(
        "ix_vil_rollup_state_tenant_level_start",
        "vil_rollup_state",
        ["tenant_id", "level", "window_start"],
    )
    op.create_index(
        "ix_vil_rollup_state_tenant_stale",
        "vil_rollup_state",
        ["tenant_id", "stale"],
    )

    # vil_summaries
    op.drop_index("ix_vil_summaries_sn_start_desc", table_name="vil_summaries")
    op.drop_index("ix_vil_summaries_sn_level_scope_latest", table_name="vil_summaries")
    op.alter_column("vil_summaries", "serial_number", new_column_name="tenant_id")
    op.create_index(
        "ix_vil_summaries_tenant_level_scope_latest",
        "vil_summaries",
        ["tenant_id", "level", "scope_id", "is_latest"],
    )
    op.create_index(
        "ix_vil_summaries_tenant_start_desc",
        "vil_summaries",
        ["tenant_id", sa.text("start_time DESC")],
    )

    # vil_job_history
    op.drop_index("ix_vil_job_history_sn_created", table_name="vil_job_history")
    op.alter_column("vil_job_history", "serial_number", new_column_name="tenant_id")
    op.create_index(
        "ix_vil_job_history_tenant_created",
        "vil_job_history",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # vil_jobs
    op.drop_index("ix_vil_jobs_sn_state", table_name="vil_jobs")
    op.alter_column("vil_jobs", "serial_number", new_column_name="tenant_id")
    op.create_index(
        "ix_vil_jobs_tenant_state",
        "vil_jobs",
        ["tenant_id", "state"],
    )

    # vil_buckets
    op.drop_index("ix_vil_buckets_sn_start_desc", table_name="vil_buckets")
    op.drop_index("ix_vil_buckets_sn_camera_start", table_name="vil_buckets")
    op.alter_column("vil_buckets", "serial_number", new_column_name="tenant_id")
    op.add_column("vil_buckets", sa.Column("trailer_id", sa.Text(), nullable=False, server_default=""))
    op.add_column("vil_buckets", sa.Column("site_id", sa.Text(), nullable=False, server_default=""))
    op.create_index(
        "ix_vil_buckets_tenant_camera_start",
        "vil_buckets",
        ["tenant_id", "camera_id", "bucket_start_utc"],
    )
    op.create_index(
        "ix_vil_buckets_tenant_start_desc",
        "vil_buckets",
        ["tenant_id", sa.text("bucket_start_utc DESC")],
    )
