"""Rename all vil_* tables, indexes, and constraints to panoptic_*.

Revision ID: 005
Revises: 004
Create Date: 2026-04-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old_table, new_table)
_TABLES = [
    ("vil_buckets", "panoptic_buckets"),
    ("vil_jobs", "panoptic_jobs"),
    ("vil_job_history", "panoptic_job_history"),
    ("vil_summaries", "panoptic_summaries"),
    ("vil_rollup_state", "panoptic_rollup_state"),
    ("vil_embedding_backlog", "panoptic_embedding_backlog"),
    ("vil_images", "panoptic_images"),
]

# (old_index, new_index)
_INDEXES = [
    # vil_buckets
    ("ix_vil_buckets_sn_camera_start", "ix_panoptic_buckets_sn_camera_start"),
    ("ix_vil_buckets_sn_start_desc", "ix_panoptic_buckets_sn_start_desc"),
    # vil_jobs
    ("ix_vil_jobs_state_lease", "ix_panoptic_jobs_state_lease"),
    ("ix_vil_jobs_sn_state", "ix_panoptic_jobs_sn_state"),
    ("ix_vil_jobs_job_id", "ix_panoptic_jobs_job_id"),
    # vil_job_history
    ("ix_vil_job_history_job_id", "ix_panoptic_job_history_job_id"),
    ("ix_vil_job_history_sn_created", "ix_panoptic_job_history_sn_created"),
    # vil_summaries
    ("ix_vil_summaries_sn_level_scope_latest", "ix_panoptic_summaries_sn_level_scope_latest"),
    ("ix_vil_summaries_embedding_status", "ix_panoptic_summaries_embedding_status"),
    ("ix_vil_summaries_sn_start_desc", "ix_panoptic_summaries_sn_start_desc"),
    # vil_rollup_state
    ("ix_vil_rollup_state_sn_level_start", "ix_panoptic_rollup_state_sn_level_start"),
    ("ix_vil_rollup_state_sn_stale", "ix_panoptic_rollup_state_sn_stale"),
    # vil_embedding_backlog
    ("ix_vil_embedding_backlog_next_attempt", "ix_panoptic_embedding_backlog_next_attempt"),
    ("ix_vil_embedding_backlog_sn_next", "ix_panoptic_embedding_backlog_sn_next"),
    # vil_images
    ("ix_vil_images_scope_time", "ix_panoptic_images_scope_time"),
    ("ix_vil_images_sn_camera_time", "ix_panoptic_images_sn_camera_time"),
    ("ix_vil_images_trigger_time", "ix_panoptic_images_trigger_time"),
    ("ix_vil_images_created_at", "ix_panoptic_images_created_at"),
]

# (old_constraint, new_constraint) — unique constraints are also indexes in Postgres
_UNIQUE_CONSTRAINTS = [
    ("uq_vil_buckets_bucket_id", "uq_panoptic_buckets_bucket_id"),
    ("uq_vil_jobs_job_id", "uq_panoptic_jobs_job_id"),
    ("uq_vil_jobs_job_key", "uq_panoptic_jobs_job_key"),
    ("uq_vil_summaries_summary_id", "uq_panoptic_summaries_summary_id"),
    ("uq_vil_rollup_state_parent_key", "uq_panoptic_rollup_state_parent_key"),
    ("uq_vil_embedding_backlog_summary_id", "uq_panoptic_embedding_backlog_summary_id"),
]


def upgrade() -> None:
    # Rename tables first — PKs and NOT NULL constraints auto-rename.
    for old, new in _TABLES:
        op.rename_table(old, new)

    # Rename indexes.
    for old_idx, new_idx in _INDEXES:
        op.execute(f'ALTER INDEX "{old_idx}" RENAME TO "{new_idx}"')

    # Rename unique constraints (they are also indexes in Postgres).
    for old_con, new_con in _UNIQUE_CONSTRAINTS:
        op.execute(f'ALTER INDEX "{old_con}" RENAME TO "{new_con}"')


def downgrade() -> None:
    # Reverse unique constraint renames.
    for old_con, new_con in _UNIQUE_CONSTRAINTS:
        op.execute(f'ALTER INDEX "{new_con}" RENAME TO "{old_con}"')

    # Reverse index renames.
    for old_idx, new_idx in _INDEXES:
        op.execute(f'ALTER INDEX "{new_idx}" RENAME TO "{old_idx}"')

    # Reverse table renames.
    for old, new in _TABLES:
        op.rename_table(new, old)
