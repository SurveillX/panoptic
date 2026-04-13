"""
Summary record persistence — upsert, versioning, supersession, rollup state,
and downstream job insertion.

All functions accept an open SQLAlchemy Connection and operate within the
caller's transaction.  No commits are issued here.

Caller's responsibility (in strict order):
  1. Call upsert_summary / insert_embedding_job / upsert_rollup_state_and_maybe_enqueue
  2. Call release_job
  3. Verify hb.is_valid()
  4. conn.commit()
  5. enqueue_job for embedding and/or rollup (only after commit)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from shared.schemas.job import make_embedding_upsert_key, make_rollup_summary_key
from shared.schemas.summary import SummaryRecord
from shared.utils.hashing import compute_child_set_hash

log = logging.getLogger(__name__)

# Expected number of 15-min buckets per hour window.
_BUCKETS_PER_HOUR = 4


# ---------------------------------------------------------------------------
# Summary upsert + versioning
# ---------------------------------------------------------------------------

def upsert_summary(conn, record: SummaryRecord) -> tuple[str, bool]:
    """
    Upsert a summary record into vil_summaries.

    Returns (summary_id, was_new: bool).

    Idempotency:
      - Same summary_id already exists → ON CONFLICT DO NOTHING, return False.
      - Different is_latest row for same scope/window → insert new row (version
        incremented), mark old row superseded.

    Version assignment:
      - First row for a scope/window: version=1.
      - Each new summary_id for the same scope/window: previous version + 1.
    """
    existing = conn.execute(
        text("""
            SELECT summary_id, version
              FROM vil_summaries
             WHERE serial_number  = :serial_number
               AND level      = :level
               AND scope_id   = :scope_id
               AND start_time = :start_time
               AND end_time   = :end_time
               AND is_latest  = true
        """),
        {
            "serial_number":  record.serial_number,
            "level":      record.level,
            "scope_id":   record.scope_id,
            "start_time": record.start_time.isoformat(),
            "end_time":   record.end_time.isoformat(),
        },
    ).fetchone()

    if existing and existing.summary_id == record.summary_id:
        log.debug("upsert_summary: duplicate summary_id=%s — no-op", record.summary_id)
        return record.summary_id, False

    next_version = (existing.version + 1) if existing else 1

    conn.execute(
        text("""
            INSERT INTO vil_summaries (
                summary_id, serial_number, level, scope_id,
                start_time, end_time,
                summary, key_events, metrics, coverage,
                summary_mode, frames_used, frame_timestamps, confidence,
                embedding_status, version, is_latest, superseded_by,
                model_profile, prompt_version, schema_version,
                source_refs, created_at, updated_at
            ) VALUES (
                :summary_id, :serial_number, :level, :scope_id,
                :start_time, :end_time,
                :summary, CAST(:key_events AS jsonb), CAST(:metrics AS jsonb), CAST(:coverage AS jsonb),
                :summary_mode, :frames_used, CAST(:frame_timestamps AS jsonb), :confidence,
                :embedding_status, :version, true, NULL,
                :model_profile, :prompt_version, :schema_version,
                CAST(:source_refs AS jsonb), now(), now()
            )
            ON CONFLICT (summary_id) DO NOTHING
        """),
        {
            "summary_id":       record.summary_id,
            "serial_number":        record.serial_number,
            "level":            record.level,
            "scope_id":         record.scope_id,
            "start_time":       record.start_time.isoformat(),
            "end_time":         record.end_time.isoformat(),
            "summary":          record.summary,
            "key_events":       json.dumps(record.key_events),
            "metrics":          json.dumps(record.metrics),
            "coverage":         json.dumps({
                "expected": record.coverage.expected,
                "present":  record.coverage.present,
                "ratio":    record.coverage.ratio,
                "missing":  record.coverage.missing,
            }),
            "summary_mode":     record.summary_mode,
            "frames_used":      record.frames_used,
            "frame_timestamps": json.dumps(record.frame_timestamps),
            "confidence":       record.confidence,
            "embedding_status": record.embedding_status,
            "version":          next_version,
            "model_profile":    record.model_profile,
            "prompt_version":   record.prompt_version,
            "schema_version":   record.schema_version,
            "source_refs":      json.dumps(record.source_refs),
        },
    )

    if existing:
        conn.execute(
            text("""
                UPDATE vil_summaries
                   SET superseded_by = :new_id,
                       is_latest     = false,
                       updated_at    = now()
                 WHERE summary_id = :old_id
            """),
            {"new_id": record.summary_id, "old_id": existing.summary_id},
        )
        log.info(
            "upsert_summary: superseded old=%s new=%s version=%d",
            existing.summary_id, record.summary_id, next_version,
        )

    log.debug(
        "upsert_summary: inserted summary_id=%s version=%d",
        record.summary_id, next_version,
    )
    return record.summary_id, True


# ---------------------------------------------------------------------------
# Embedding job insertion
# ---------------------------------------------------------------------------

def insert_embedding_job(
    conn,
    *,
    summary_id: str,
    serial_number: str,
    max_attempts: int = 3,
) -> str | None:
    """
    Insert an embedding_upsert job into vil_jobs.

    Returns the new job_id UUID string if inserted.
    Returns None if a job with the same job_key already exists (idempotent).

    Caller must enqueue_job('embedding_upsert', returned_job_id) AFTER commit.
    """
    job_key = make_embedding_upsert_key(summary_id)
    new_job_id = str(uuid.uuid4())

    row = conn.execute(
        text("""
            INSERT INTO vil_jobs (
                job_id, job_key, serial_number, job_type,
                priority, state, attempt_count, max_attempts, payload
            ) VALUES (
                :job_id, :job_key, :serial_number, 'embedding_upsert',
                'normal', 'pending', 0, :max_attempts, CAST(:payload AS jsonb)
            )
            ON CONFLICT (job_key) DO NOTHING
            RETURNING job_id
        """),
        {
            "job_id":       new_job_id,
            "job_key":      job_key,
            "serial_number":    serial_number,
            "max_attempts": max_attempts,
            "payload":      json.dumps({"summary_id": summary_id, "serial_number": serial_number}),
        },
    ).fetchone()

    if row is None:
        log.debug("insert_embedding_job: duplicate job_key=%s", job_key)
        return None

    log.debug(
        "insert_embedding_job: inserted job_id=%s summary_id=%s",
        new_job_id, summary_id,
    )
    return new_job_id


# ---------------------------------------------------------------------------
# Rollup state update + conditional rollup job insertion
# ---------------------------------------------------------------------------

def upsert_rollup_state_and_maybe_enqueue(
    conn,
    *,
    serial_number: str,
    camera_id: str,
    bucket_start_utc: datetime,
    bucket_end_utc: datetime,
    model_profile: str,
    prompt_version: str,
    max_rollup_attempts: int = 3,
) -> tuple[str | None, str | None]:
    """
    Update vil_rollup_state for the parent hour window.

    If coverage_ratio >= 0.5 after the update, attempt to insert a rollup_summary
    job via ON CONFLICT (job_key) DO NOTHING.  Idempotency is enforced by the
    UNIQUE(job_key) constraint — never use a SELECT check.

    Returns (rollup_job_id, serial_number):
      - rollup_job_id: new job UUID if a new row was inserted; None otherwise.
      - serial_number:     same as input, or None if no new job.

    Caller must enqueue_job('rollup_summary', rollup_job_id) AFTER commit,
    only when rollup_job_id is not None.
    """
    # Derive hour window from bucket_start_utc (UTC-normalised, truncated to hour)
    utc = bucket_start_utc.astimezone(timezone.utc)
    hour_start = utc.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)

    parent_key = f"hour:{serial_number}:{camera_id}:{hour_start.isoformat()}"
    scope_id = f"{serial_number}:{camera_id}"

    # Derive expected children from bucket duration; fall back to 4 (15-min buckets)
    duration_s = (bucket_end_utc - bucket_start_utc).total_seconds()
    if duration_s > 0:
        expected = max(1, round(3600.0 / duration_s))
    else:
        expected = _BUCKETS_PER_HOUR

    state_row = conn.execute(
        text("""
            INSERT INTO vil_rollup_state (
                parent_key, serial_number, level, window_start, window_end,
                expected_children, present_children, coverage_ratio
            ) VALUES (
                :parent_key, :serial_number, 'hour', :window_start, :window_end,
                :expected, 1, 1.0 / :expected
            )
            ON CONFLICT (parent_key) DO UPDATE SET
                present_children = vil_rollup_state.present_children + 1,
                coverage_ratio   = least(
                    (vil_rollup_state.present_children + 1.0)
                    / vil_rollup_state.expected_children,
                    1.0
                ),
                updated_at       = now()
            RETURNING present_children, coverage_ratio
        """),
        {
            "parent_key":   parent_key,
            "serial_number":    serial_number,
            "window_start": hour_start.isoformat(),
            "window_end":   hour_end.isoformat(),
            "expected":     expected,
        },
    ).fetchone()

    if state_row.coverage_ratio < 0.5:
        log.debug(
            "rollup_state: coverage=%.2f < 0.5 — no trigger parent=%s",
            state_row.coverage_ratio, parent_key,
        )
        return None, None

    # coverage >= 0.5: gather all latest L1 summaries for this scope/window
    child_rows = conn.execute(
        text("""
            SELECT summary_id
              FROM vil_summaries
             WHERE serial_number  = :serial_number
               AND level      = 'camera'
               AND scope_id   = :camera_id
               AND start_time >= :window_start
               AND start_time <  :window_end
               AND is_latest  = true
             ORDER BY summary_id
        """),
        {
            "serial_number":    serial_number,
            "camera_id":    camera_id,
            "window_start": hour_start.isoformat(),
            "window_end":   hour_end.isoformat(),
        },
    ).fetchall()

    child_ids = [r.summary_id for r in child_rows]
    if not child_ids:
        log.warning(
            "rollup_state: coverage >= 0.5 but no child summaries found parent=%s",
            parent_key,
        )
        return None, None

    child_set_hash = compute_child_set_hash(child_ids)
    job_key = make_rollup_summary_key(
        scope_id=scope_id,
        window_start=hour_start,
        model_profile=model_profile,
        prompt_version=prompt_version,
        child_set_hash=child_set_hash,
    )
    new_job_id = str(uuid.uuid4())

    job_row = conn.execute(
        text("""
            INSERT INTO vil_jobs (
                job_id, job_key, serial_number, job_type,
                priority, state, attempt_count, max_attempts, payload
            ) VALUES (
                :job_id, :job_key, :serial_number, 'rollup_summary',
                'normal', 'pending', 0, :max_attempts, CAST(:payload AS jsonb)
            )
            ON CONFLICT (job_key) DO NOTHING
            RETURNING job_id
        """),
        {
            "job_id":       new_job_id,
            "job_key":      job_key,
            "serial_number":    serial_number,
            "max_attempts": max_rollup_attempts,
            "payload":      json.dumps({
                "serial_number":      serial_number,
                "scope_id":       scope_id,
                "camera_id":      camera_id,
                "window_start":   hour_start.isoformat(),
                "window_end":     hour_end.isoformat(),
                "child_ids":      child_ids,
                "child_set_hash": child_set_hash,
                "model_profile":  model_profile,
                "prompt_version": prompt_version,
            }),
        },
    ).fetchone()

    if job_row is None:
        log.debug("rollup_state: rollup job already exists job_key=%s", job_key)
        return None, None

    log.info(
        "rollup_state: triggered rollup job_id=%s parent=%s coverage=%.2f children=%d",
        new_job_id, parent_key, state_row.coverage_ratio, len(child_ids),
    )
    return new_job_id, serial_number
