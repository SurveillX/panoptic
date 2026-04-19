"""
Job record schema — authoritative execution unit tracked in Postgres.

job_key enforces idempotency: only one active job per key is allowed.

job_key format by type:
  bucket_summary:   bucket_summary:{bucket_id}:{model_profile}:{prompt_version}
  rollup_summary:   rollup_summary:{scope_id}:{window_start_iso}:{model_profile}:{prompt_version}:{child_set_hash}
  embedding_upsert: embedding_upsert:{summary_id}
  recompute_summary: recompute_summary:{summary_id}:{model_profile}:{prompt_version}

lease_expires_at semantics:
  - UTC timestamp at which the current lease expires.
  - NULL when state is not 'leased' or 'running'.
  - Workers MUST verify current_utc < lease_expires_at before every DB write.
  - Reclaimer targets rows where state IN ('leased','running')
    AND lease_expires_at < now().
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal[
    "bucket_summary", "rollup_summary", "embedding_upsert", "recompute_summary",
    "image_caption", "caption_embed", "image_embed", "event_produce",
    "report_generate",
]
JobState = Literal[
    "pending",
    "leased",
    "running",
    "succeeded",
    "degraded",
    "retry_wait",
    "failed_terminal",
    "cancelled",
]
JobPriority = Literal["high", "normal", "low"]


# ---------------------------------------------------------------------------
# Job key helpers
# ---------------------------------------------------------------------------


def make_bucket_summary_key(bucket_id: str, model_profile: str, prompt_version: str) -> str:
    return f"bucket_summary:{bucket_id}:{model_profile}:{prompt_version}"


def make_rollup_summary_key(
    scope_id: str,
    window_start: datetime,
    model_profile: str,
    prompt_version: str,
    child_set_hash: str,
) -> str:
    return (
        f"rollup_summary:{scope_id}:{window_start.isoformat()}"
        f":{model_profile}:{prompt_version}:{child_set_hash}"
    )


def make_embedding_upsert_key(summary_id: str) -> str:
    return f"embedding_upsert:{summary_id}"


def make_recompute_summary_key(
    summary_id: str, model_profile: str, prompt_version: str
) -> str:
    return f"recompute_summary:{summary_id}:{model_profile}:{prompt_version}"


def make_image_caption_key(image_id: str) -> str:
    return f"image_caption:{image_id}"


def make_caption_embed_key(image_id: str) -> str:
    return f"caption_embed:{image_id}"


def make_image_embed_key(image_id: str) -> str:
    return f"image_embed:{image_id}"


def make_event_produce_image_key(image_id: str) -> str:
    return f"event_produce:image:{image_id}"


def make_event_produce_bucket_key(bucket_id: str) -> str:
    return f"event_produce:bucket:{bucket_id}"


def make_report_generate_key(
    serial_number: str, kind: str, window_start_utc: datetime,
) -> str:
    """
    Deterministic job_key for M9 report_generate.

    Matches the (serial, kind, window_start) uniqueness contract of
    panoptic_reports — rerunning a report for the same window reuses
    the same job_key and gets ON CONFLICT DO NOTHING idempotency.
    """
    return f"report_generate:{serial_number}:{kind}:{window_start_utc.isoformat()}"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class JobRecord(BaseModel):
    """
    Full authoritative job record as stored in panoptic_jobs.
    """

    model_config = ConfigDict(strict=True)

    job_id: str
    job_key: str
    serial_number: str
    job_type: JobType
    priority: JobPriority = "normal"

    state: JobState = "pending"

    # lease_expires_at: NULL when state not in ('leased', 'running').
    # Workers must check current_utc < lease_expires_at before any write.
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None

    attempt_count: int = Field(ge=0, default=0)
    max_attempts: int = Field(ge=1, default=3)

    payload: dict[str, Any]
    last_error: str | None = None

    created_at: datetime
    updated_at: datetime


class JobCreate(BaseModel):
    """
    Input model for creating a new job — orchestrator-facing.
    job_id, state, lease fields, and timestamps are set by the persistence layer.
    """

    model_config = ConfigDict(strict=True)

    job_key: str
    serial_number: str
    job_type: JobType
    priority: JobPriority = "normal"
    max_attempts: int = Field(ge=1, default=3)
    payload: dict[str, Any]
