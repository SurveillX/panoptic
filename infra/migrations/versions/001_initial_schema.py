"""Initial VIL schema — all six tables.

Revision ID: 001
Revises: (none)
Create Date: 2026-04-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # vil_buckets
    # ------------------------------------------------------------------
    op.create_table(
        "vil_buckets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bucket_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("trailer_id", sa.Text(), nullable=False),
        sa.Column("camera_id", sa.Text(), nullable=False),
        sa.Column("bucket_start_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("bucket_end_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        # 'complete' | 'partial' | 'late_finalized'
        sa.Column("bucket_status", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("detection_hash", sa.Text(), nullable=False),
        sa.Column("activity_score", sa.Float(), nullable=False),
        sa.Column("activity_components", postgresql.JSONB(), nullable=False),
        sa.Column("object_counts", postgresql.JSONB(), nullable=False),
        sa.Column("keyframe_candidates", postgresql.JSONB(), nullable=False),
        sa.Column(
            "event_markers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("completeness", postgresql.JSONB(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_id", name="uq_vil_buckets_bucket_id"),
    )
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

    # ------------------------------------------------------------------
    # vil_jobs
    # ------------------------------------------------------------------
    op.create_table(
        "vil_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Uniqueness enforces at-most-one-active-job per logical operation.
        sa.Column("job_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # 'bucket_summary' | 'rollup_summary' | 'embedding_upsert' | 'recompute_summary'
        sa.Column("job_type", sa.Text(), nullable=False),
        # 'high' | 'normal' | 'low'
        sa.Column("priority", sa.Text(), nullable=False, server_default=sa.text("'normal'")),
        # 'pending' | 'leased' | 'running' | 'succeeded' | 'degraded' |
        # 'retry_wait' | 'failed_terminal' | 'cancelled'
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        # NULL when state NOT IN ('leased', 'running').
        # Workers MUST check: current_utc < lease_expires_at before any write.
        # Reclaimer targets: state IN ('leased','running') AND lease_expires_at < now()
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_vil_jobs_job_id"),
        sa.UniqueConstraint("job_key", name="uq_vil_jobs_job_key"),
    )
    op.create_index(
        "ix_vil_jobs_state_lease",
        "vil_jobs",
        ["state", "lease_expires_at"],
    )
    op.create_index(
        "ix_vil_jobs_tenant_state",
        "vil_jobs",
        ["tenant_id", "state"],
    )
    op.create_index("ix_vil_jobs_job_id", "vil_jobs", ["job_id"])

    # ------------------------------------------------------------------
    # vil_job_history  (append-only — no FK so history survives job deletion)
    # ------------------------------------------------------------------
    op.create_table(
        "vil_job_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # NULL for the initial insert (no previous state).
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vil_job_history_job_id", "vil_job_history", ["job_id"])
    op.create_index(
        "ix_vil_job_history_tenant_created",
        "vil_job_history",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ------------------------------------------------------------------
    # vil_summaries
    # ------------------------------------------------------------------
    op.create_table(
        "vil_summaries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("summary_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # 'camera' | 'hour' | 'day' | 'site'
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "key_events",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        # 'full' | 'partial' | 'metadata_only'
        sa.Column("summary_mode", sa.Text(), nullable=False),
        sa.Column("frames_used", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        # 'pending' | 'success' | 'failed'
        sa.Column(
            "embedding_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # summary_id of the superseding record; NULL if this is the current version.
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("model_profile", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "source_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id", name="uq_vil_summaries_summary_id"),
    )
    op.create_index(
        "ix_vil_summaries_tenant_level_scope_latest",
        "vil_summaries",
        ["tenant_id", "level", "scope_id", "is_latest"],
    )
    op.create_index(
        "ix_vil_summaries_embedding_status",
        "vil_summaries",
        ["embedding_status"],
    )
    op.create_index(
        "ix_vil_summaries_tenant_start_desc",
        "vil_summaries",
        ["tenant_id", sa.text("start_time DESC")],
    )

    # ------------------------------------------------------------------
    # vil_rollup_state
    # ------------------------------------------------------------------
    op.create_table(
        "vil_rollup_state",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # 'hour' | 'day' | 'site'
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expected_children", sa.Integer(), nullable=False),
        sa.Column(
            "present_children", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "coverage_ratio", sa.Float(), nullable=False, server_default=sa.text("0.0")
        ),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_rollup_summary_id", sa.Text(), nullable=True),
        sa.Column("last_recompute_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_key", name="uq_vil_rollup_state_parent_key"),
    )
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

    # ------------------------------------------------------------------
    # vil_embedding_backlog
    # ------------------------------------------------------------------
    op.create_table(
        "vil_embedding_backlog",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("summary_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id", name="uq_vil_embedding_backlog_summary_id"),
    )
    op.create_index(
        "ix_vil_embedding_backlog_next_attempt",
        "vil_embedding_backlog",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_vil_embedding_backlog_tenant_next",
        "vil_embedding_backlog",
        ["tenant_id", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("vil_embedding_backlog")
    op.drop_table("vil_rollup_state")
    op.drop_table("vil_summaries")
    op.drop_table("vil_job_history")
    op.drop_table("vil_jobs")
    op.drop_table("vil_buckets")
