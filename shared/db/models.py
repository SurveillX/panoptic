"""
SQLAlchemy 2.x ORM models for all VIL Postgres tables.

All TIMESTAMP columns use TIMESTAMP WITH TIME ZONE (timezone=True).
All JSONB columns default to '{}' or '[]' as appropriate.

Tables:
  vil_buckets          — canonical bucket records from Cognia
  vil_jobs             — authoritative job state (leasing source of truth)
  vil_job_history      — append-only transition log
  vil_summaries        — summary records with versioning
  vil_rollup_state     — rollup readiness tracking per parent window
  vil_embedding_backlog — reconciliation helper for failed embeddings
  vil_images           — trailer-pushed images with caption enrichment
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# vil_buckets
# ---------------------------------------------------------------------------


class VilBucket(Base):
    __tablename__ = "vil_buckets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    camera_id: Mapped[str] = mapped_column(Text, nullable=False)

    bucket_start_utc: Mapped[datetime] = mapped_column(
        "bucket_start_utc", Text, nullable=False  # stored as TIMESTAMPTZ via migration
    )
    bucket_end_utc: Mapped[datetime] = mapped_column(
        "bucket_end_utc", Text, nullable=False
    )
    bucket_status: Mapped[str] = mapped_column(Text, nullable=False)

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_hash: Mapped[str] = mapped_column(Text, nullable=False)

    activity_score: Mapped[float] = mapped_column(Float, nullable=False)
    activity_components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    object_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)

    keyframe_candidates: Mapped[dict] = mapped_column(JSONB, nullable=False)
    event_markers: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    completeness: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_buckets_sn_camera_start", "serial_number", "camera_id", "bucket_start_utc"),
        Index("ix_vil_buckets_sn_start_desc", "serial_number", "bucket_start_utc"),
    )


# ---------------------------------------------------------------------------
# vil_jobs
# ---------------------------------------------------------------------------


class VilJob(Base):
    __tablename__ = "vil_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    # Uniqueness on job_key enforces at-most-one-active-job-per-logical-operation.
    job_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))

    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))

    # lease_expires_at: NULL when state NOT IN ('leased', 'running').
    # Workers must verify current_utc < lease_expires_at before every write.
    # Reclaimer: WHERE state IN ('leased','running') AND lease_expires_at < now()
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(Text, nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_jobs_state_lease", "state", "lease_expires_at"),
        Index("ix_vil_jobs_sn_state", "serial_number", "state"),
        Index("ix_vil_jobs_job_id", "job_id"),
    )


# ---------------------------------------------------------------------------
# vil_job_history  (append-only)
# ---------------------------------------------------------------------------


class VilJobHistory(Base):
    __tablename__ = "vil_job_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Not a FK — history must survive job deletion / archival.
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL for initial insert
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_job_history_job_id", "job_id"),
        Index("ix_vil_job_history_sn_created", "serial_number", "created_at"),
    )


# ---------------------------------------------------------------------------
# vil_summaries
# ---------------------------------------------------------------------------


class VilSummary(Base):
    __tablename__ = "vil_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    summary_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)

    start_time: Mapped[datetime] = mapped_column(Text, nullable=False)
    end_time: Mapped[datetime] = mapped_column(Text, nullable=False)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_events: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)

    summary_mode: Mapped[str] = mapped_column(Text, nullable=False)
    frames_used: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_timestamps: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    embedding_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # summary_id of the record that supersedes this one; NULL if this is current.
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_profile: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    source_refs: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "ix_vil_summaries_sn_level_scope_latest",
            "serial_number", "level", "scope_id", "is_latest",
        ),
        Index("ix_vil_summaries_embedding_status", "embedding_status"),
        Index("ix_vil_summaries_sn_start_desc", "serial_number", "start_time"),
    )


# ---------------------------------------------------------------------------
# vil_rollup_state
# ---------------------------------------------------------------------------


class VilRollupState(Base):
    __tablename__ = "vil_rollup_state"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(Text, nullable=False)
    window_end: Mapped[datetime] = mapped_column(Text, nullable=False)

    expected_children: Mapped[int] = mapped_column(Integer, nullable=False)
    present_children: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    coverage_ratio: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))

    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_rollup_summary_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_recompute_at: Mapped[datetime | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_rollup_state_sn_level_start", "serial_number", "level", "window_start"),
        Index("ix_vil_rollup_state_sn_stale", "serial_number", "stale"),
    )


# ---------------------------------------------------------------------------
# vil_embedding_backlog
# ---------------------------------------------------------------------------


class VilEmbeddingBacklog(Base):
    __tablename__ = "vil_embedding_backlog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    summary_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_embedding_backlog_next_attempt", "next_attempt_at"),
        Index("ix_vil_embedding_backlog_sn_next", "serial_number", "next_attempt_at"),
    )


# ---------------------------------------------------------------------------
# vil_images
# ---------------------------------------------------------------------------


class VilImage(Base):
    __tablename__ = "vil_images"

    # Deterministic SHA256 — natural primary key.
    image_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Stored for traceability, not used for dedup.
    event_id: Mapped[str] = mapped_column(Text, nullable=False)

    serial_number: Mapped[str] = mapped_column(Text, nullable=False)
    camera_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)

    bucket_start_utc: Mapped[datetime] = mapped_column(Text, nullable=False)
    bucket_end_utc: Mapped[datetime] = mapped_column(Text, nullable=False)

    captured_at_utc: Mapped[datetime | None] = mapped_column(Text, nullable=True)
    timestamp_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    selection_policy_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'1'")
    )

    context_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'image/jpeg'")
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    caption_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    caption_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    caption_embedding_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    caption_embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_embedding_vector_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'trailer_push'")
    )
    is_searchable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Text, nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_vil_images_scope_time", "scope_id", "bucket_start_utc"),
        Index("ix_vil_images_sn_camera_time", "serial_number", "camera_id", "bucket_start_utc"),
        Index("ix_vil_images_trigger_time", "trigger", "bucket_start_utc"),
        Index("ix_vil_images_created_at", "created_at"),
    )
