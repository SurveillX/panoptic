"""
Cognia bucket intake — library layer.

Called by the Panoptic orchestrator HTTP endpoint when Cognia POSTs a finalized
bucket record.  This module contains no HTTP handling; it is a pure intake
function that the endpoint calls after parsing the request body.

Intake sequence (strict ordering):
  1. Validate payload against BucketRecord schema
  2. Recompute bucket_id from payload fields; reject if mismatch
  3. Recompute activity_score from activity_components + camera history
  4. Upsert into panoptic_buckets            (idempotent — ON CONFLICT DO NOTHING)
  5. Insert into panoptic_jobs               (idempotent — ON CONFLICT (job_key) DO NOTHING)
  6. Commit transaction
  7. enqueue_job to Redis Stream        (only after commit; only if job was new)

Upsert behaviour:
  - bucket_status = 'late_finalized': overwrites the existing row (all fields).
  - All other statuses: ON CONFLICT DO NOTHING — first write wins.
  - Duplicate job: panoptic_jobs INSERT conflicts on UNIQUE(job_key) — no duplicate.
  - enqueue_job is called only when a new job row was inserted.
  - If commit succeeds but enqueue_job fails: job stays 'pending' in Postgres;
    the orchestrator's pending-job scan will re-enqueue it.

Activity score:
  - When camera history exists: z-score normalization via compute_activity_score.
  - When no history (new camera): raw weighted formula applied directly to the
    activity_components values, clamped to [0, 1].  Deterministic regardless
    of system state.
"""

from __future__ import annotations

import json
import logging
import statistics
import uuid
from dataclasses import dataclass

import redis
from pydantic import ValidationError
from sqlalchemy import text

from shared.schemas.bucket import BucketRecord, EventMarker, generate_bucket_id
from shared.schemas.job import make_bucket_summary_key, make_event_produce_bucket_key
from shared.signals.derive import derive_history_markers
from shared.signals.history import fetch_bucket_history
from shared.utils.activity import (
    ActivityComponents,
    CameraStats,
    compute_activity_score,
)
from shared.utils.streams import enqueue_job

log = logging.getLogger(__name__)

# Number of recent buckets used to compute rolling camera stats.
_CAMERA_STATS_LOOKBACK = 100


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BucketValidationError(ValueError):
    """Payload failed Pydantic schema validation."""


class BucketIdMismatchError(ValueError):
    """
    Submitted bucket_id does not match the value recomputed from payload fields.

    Indicates corruption in transit, incorrect ID generation by Cognia, or
    field mutation after signing.  The bucket must be rejected entirely.
    """

    def __init__(self, submitted: str, recomputed: str) -> None:
        super().__init__(
            f"bucket_id mismatch: submitted={submitted!r} recomputed={recomputed!r}"
        )
        self.submitted = submitted
        self.recomputed = recomputed


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestResult:
    bucket_id: str
    job_id: str | None       # UUID string; None if job already existed
    activity_score: float    # Panoptic-recomputed score stored in panoptic_buckets
    # 'inserted'             — new bucket row created
    # 'late_finalized_update' — existing row overwritten (bucket_status=late_finalized)
    # 'duplicate'            — row already existed, no change made
    bucket_action: str
    was_duplicate_job: bool


# ---------------------------------------------------------------------------
# Public intake function
# ---------------------------------------------------------------------------

def ingest_bucket(
    engine,
    r: redis.Redis,
    raw_payload: dict,
    *,
    model_profile: str,
    prompt_version: str,
    max_attempts: int = 3,
) -> IngestResult:
    """
    Ingest a finalized bucket record pushed by Cognia.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine (sync).  This function owns its connection and
        transaction; callers do not manage the transaction.
    r:
        Central Panoptic Redis client (NOT an edge Redis).
    raw_payload:
        Pre-parsed JSON dict from the HTTP request body.
    model_profile:
        LLM model identifier included in the job_key.
    prompt_version:
        Prompt version string included in the job_key.
    max_attempts:
        Max execution attempts for the created bucket_summary job.

    Raises
    ------
    BucketValidationError   — payload fails BucketRecord validation
    BucketIdMismatchError   — submitted bucket_id != recomputed value
    """

    # ------------------------------------------------------------------
    # Step 1: Validate payload against BucketRecord schema
    # ------------------------------------------------------------------
    try:
        # strict=False allows ISO 8601 strings → datetime coercion from JSON
        # payloads.  Field-level type errors (wrong type, missing required field)
        # are still caught and raised as BucketValidationError.
        bucket = BucketRecord.model_validate(raw_payload, strict=False)
    except ValidationError as exc:
        raise BucketValidationError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Step 2: Recompute bucket_id and verify match
    # ------------------------------------------------------------------
    recomputed_id = generate_bucket_id(
        bucket.serial_number,
        bucket.camera_id,
        bucket.bucket_start_utc,
        bucket.bucket_end_utc,
        bucket.detection_hash,
        bucket.schema_version,
    )
    if recomputed_id != bucket.bucket_id:
        raise BucketIdMismatchError(
            submitted=bucket.bucket_id,
            recomputed=recomputed_id,
        )

    # ------------------------------------------------------------------
    # Steps 3–6: single transaction
    # ------------------------------------------------------------------
    with engine.connect() as conn:

        # Step 3: Recompute activity_score from activity_components.
        # With camera history: z-score normalization via compute_activity_score.
        # Without history: raw weighted formula clamped to [0, 1] — deterministic.
        camera_stats = _get_camera_stats(conn, bucket.serial_number, bucket.camera_id)
        stream_ok = (
            bucket.completeness.detection_coverage >= 0.5
            and not bucket.completeness.aggregator_restart_seen
        )
        if camera_stats is not None:
            components = _extract_activity_components(bucket.activity_components)
            activity_score = compute_activity_score(
                components, camera_stats, stream_coverage_ok=stream_ok
            )
        else:
            activity_score = _raw_activity_score(
                bucket.activity_components, stream_coverage_ok=stream_ok
            )

        # Step 3b (M12): history-based markers.
        #
        # Fragment-based markers (spike, after_hours) are already present on
        # bucket.event_markers from transform_to_bucket_record. History-
        # based markers (drop, start, late_start, underperforming) need DB
        # access, so they're derived here and merged. Re-derivation is
        # intentionally cheap — two small queries per ingest — and keeps
        # the derivation pure over (aggregate, history) inputs.
        history = fetch_bucket_history(
            conn,
            serial_number=bucket.serial_number,
            camera_id=bucket.camera_id,
            bucket_start=bucket.bucket_start_utc,
        )
        bucket_minutes = int(
            (bucket.bucket_end_utc - bucket.bucket_start_utc).total_seconds() // 60
        ) or 15
        history_markers = derive_history_markers(
            total_detections=int(
                bucket.activity_components.get("object_count_total", 0)
            ),
            bucket_start=bucket.bucket_start_utc,
            bucket_minutes=bucket_minutes,
            history=history,
        )
        if history_markers:
            # Dedup on (event_type, ts) to stay idempotent across replays.
            # Existing markers are EventMarker instances; history_markers are
            # plain dicts shaped for pydantic validation.
            existing = {
                (m.event_type, m.ts.isoformat())
                for m in (bucket.event_markers or [])
            }
            for m in history_markers:
                key = (m.get("event_type"), m.get("ts"))
                if key in existing:
                    continue
                # derive_history_markers emits ts as an ISO string (same shape
                # as derive_markers). EventMarker's model is strict=True, so
                # explicitly allow string→datetime coercion here — matches the
                # strict=False used for BucketRecord validation at ingress.
                bucket.event_markers.append(EventMarker.model_validate(m, strict=False))
                existing.add(key)

        # Step 4: Upsert panoptic_buckets.
        # late_finalized overwrites all fields — the bucket arrived late but
        # carries authoritative data.  All other statuses: first write wins.
        # RETURNING (xmax = 0) distinguishes fresh insert from update.
        print("INGEST INPUT markers:", bucket.event_markers)
        params = _bucket_params(bucket, activity_score)
        print("INGEST PARAM markers:", params.get("event_markers"))
        bucket_row = conn.execute(
            text("""
                INSERT INTO panoptic_buckets (
                    bucket_id, serial_number, camera_id,
                    bucket_start_utc, bucket_end_utc, bucket_status,
                    schema_version, detection_hash,
                    activity_score, activity_components, object_counts,
                    keyframe_candidates, event_markers, completeness
                ) VALUES (
                    :bucket_id, :serial_number, :camera_id,
                    :bucket_start_utc, :bucket_end_utc, :bucket_status,
                    :schema_version, :detection_hash,
                    :activity_score, CAST(:activity_components AS jsonb),
                    CAST(:object_counts AS jsonb), CAST(:keyframe_candidates AS jsonb),
                    CAST(:event_markers AS jsonb), CAST(:completeness AS jsonb)
                )
                ON CONFLICT (bucket_id) DO UPDATE SET
                    bucket_status       = EXCLUDED.bucket_status,
                    activity_score      = EXCLUDED.activity_score,
                    activity_components = EXCLUDED.activity_components,
                    object_counts       = EXCLUDED.object_counts,
                    keyframe_candidates = EXCLUDED.keyframe_candidates,
                    event_markers       = EXCLUDED.event_markers,
                    completeness        = EXCLUDED.completeness,
                    updated_at          = now()
                WHERE EXCLUDED.bucket_status = 'late_finalized'
                RETURNING id, (xmax = 0) AS was_inserted
            """),
            params,
        ).fetchone()

        if bucket_row is None:
            bucket_action = "duplicate"
        elif bucket_row.was_inserted:
            bucket_action = "inserted"
        else:
            bucket_action = "late_finalized_update"

        # Step 5: Insert panoptic_jobs — idempotent via UNIQUE(job_key)
        job_key = make_bucket_summary_key(
            bucket.bucket_id, model_profile, prompt_version
        )
        new_job_id = str(uuid.uuid4())

        job_row = conn.execute(
            text("""
                INSERT INTO panoptic_jobs (
                    job_id, job_key, serial_number, job_type,
                    priority, state, attempt_count, max_attempts, payload
                ) VALUES (
                    :job_id, :job_key, :serial_number, 'bucket_summary',
                    'normal', 'pending', 0, :max_attempts, CAST(:payload AS jsonb)
                )
                ON CONFLICT (job_key) DO NOTHING
                RETURNING job_id
            """),
            {
                "job_id":       new_job_id,
                "job_key":      job_key,
                "serial_number":    bucket.serial_number,
                "max_attempts": max_attempts,
                "payload":      _job_payload_json(bucket, model_profile, prompt_version),
            },
        ).fetchone()

        was_duplicate_job = job_row is None
        returned_job_id = None if was_duplicate_job else new_job_id

        # Step 5b: Insert event_produce job for buckets with markers.
        # job_key is bucket-scoped (one event_produce per bucket ever); the
        # executor iterates the bucket's current event_markers, so reruns
        # against a modified bucket still pick up changes. ON CONFLICT
        # (job_key) DO NOTHING protects against duplicate deliveries.
        event_produce_job_id: str | None = None
        if bucket.event_markers and bucket_action != "duplicate":
            new_event_job_id = str(uuid.uuid4())
            ev_row = conn.execute(
                text("""
                    INSERT INTO panoptic_jobs (
                        job_id, job_key, serial_number, job_type,
                        priority, state, attempt_count, max_attempts, payload
                    ) VALUES (
                        :job_id, :job_key, :serial_number, 'event_produce',
                        'normal', 'pending', 0, :max_attempts, CAST(:payload AS jsonb)
                    )
                    ON CONFLICT (job_key) DO NOTHING
                    RETURNING job_id
                """),
                {
                    "job_id":       new_event_job_id,
                    "job_key":      make_event_produce_bucket_key(bucket.bucket_id),
                    "serial_number": bucket.serial_number,
                    "max_attempts": max_attempts,
                    "payload":      json.dumps({
                        "source_type": "bucket",
                        "bucket_id":   bucket.bucket_id,
                    }),
                },
            ).fetchone()
            if ev_row is not None:
                event_produce_job_id = new_event_job_id

        # Step 6: Commit — both rows durable before any Redis write
        conn.commit()

    # ------------------------------------------------------------------
    # Step 7: enqueue_job — only after commit, only for a new job row
    # ------------------------------------------------------------------
    print("INGEST reached enqueue block")
    print("enqueue condition values: was_duplicate_job=", was_duplicate_job, "job_row=", job_row, "returned_job_id=", returned_job_id, "bucket_action=", bucket_action)
    if not was_duplicate_job:
        try:
            enqueue_job(
                r,
                job_type="bucket_summary",
                job_id=returned_job_id,
                serial_number=bucket.serial_number,
                priority="normal",
            )
            print("ENQUEUED SUMMARY JOB:", returned_job_id)
        except Exception as exc:
            # Redis unavailable: job is 'pending' in Postgres.
            # Orchestrator pending-job scan will re-enqueue.
            log.error(
                "ingest_bucket: enqueue failed job_id=%s bucket_id=%s: %s",
                returned_job_id, bucket.bucket_id, exc,
            )

    if event_produce_job_id is not None:
        try:
            enqueue_job(
                r,
                job_type="event_produce",
                job_id=event_produce_job_id,
                serial_number=bucket.serial_number,
                priority="normal",
            )
        except Exception as exc:
            log.error(
                "ingest_bucket: event_produce enqueue failed job_id=%s bucket_id=%s: %s — "
                "job exists in Postgres, reclaimer will pick it up",
                event_produce_job_id, bucket.bucket_id, exc,
            )

    log.info(
        "ingest_bucket bucket_id=%s job_id=%s bucket_action=%s dup_job=%s score=%.3f",
        bucket.bucket_id, returned_job_id, bucket_action, was_duplicate_job, activity_score,
    )

    return IngestResult(
        bucket_id=bucket.bucket_id,
        job_id=returned_job_id,
        activity_score=activity_score,
        bucket_action=bucket_action,
        was_duplicate_job=was_duplicate_job,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_camera_stats(conn, serial_number: str, camera_id: str) -> CameraStats | None:
    """
    Derive rolling CameraStats from the most recent buckets for this camera.

    Returns None when no prior history exists; the caller falls back to
    _raw_activity_score in that case.
    """
    rows = conn.execute(
        text("""
            SELECT activity_components
              FROM panoptic_buckets
             WHERE serial_number = :serial_number
               AND camera_id = :camera_id
             ORDER BY bucket_start_utc DESC
             LIMIT :lim
        """),
        {"serial_number": serial_number, "camera_id": camera_id, "lim": _CAMERA_STATS_LOOKBACK},
    ).fetchall()

    if not rows:
        return None

    counts    = [float(r.activity_components.get("object_count_total", 0)) for r in rows]
    classes   = [float(r.activity_components.get("unique_object_classes", 0)) for r in rows]
    variances = [float(r.activity_components.get("temporal_variance", 0.0)) for r in rows]

    def _ms(vals: list[float]) -> tuple[float, float]:
        m = statistics.mean(vals)
        s = statistics.stdev(vals) if len(vals) > 1 else 1.0
        return m, max(s, 1e-9)

    mc, sc = _ms(counts)
    mu, su = _ms(classes)
    mv, sv = _ms(variances)

    return CameraStats(
        mean_object_count=mc,    std_object_count=sc,
        mean_unique_classes=mu,  std_unique_classes=su,
        mean_temporal_variance=mv, std_temporal_variance=sv,
    )


def _raw_activity_score(
    components: dict[str, float],
    stream_coverage_ok: bool = True,
) -> float:
    """
    Compute activity_score using the raw weighted formula when no camera
    history is available.

    Applies weights directly to the component values and clamps to [0, 1].
    Deterministic: same input always produces the same output regardless of
    any other system state.

      raw = 0.5 * object_count_total
          + 0.2 * unique_object_classes
          + 0.3 * temporal_variance
      score = clamp(raw, 0.0, 1.0)

    Empty-scene rule: if object_count_total == 0 and stream_coverage_ok,
    returns 0.0 exactly (consistent with compute_activity_score).
    """
    c1 = float(components.get("object_count_total", 0.0))
    c2 = float(components.get("unique_object_classes", 0.0))
    c3 = float(components.get("temporal_variance", 0.0))

    if c1 == 0.0 and stream_coverage_ok:
        return 0.0

    raw = 0.5 * c1 + 0.2 * c2 + 0.3 * c3
    return max(0.0, min(1.0, raw))


def _extract_activity_components(components: dict[str, float]) -> ActivityComponents:
    """
    Extract ActivityComponents from the activity_components dict.

    Missing keys default to 0; buckets with incomplete component data degrade
    to low activity rather than raising.
    """
    return ActivityComponents(
        object_count_total=int(components.get("object_count_total", 0)),
        unique_object_classes=int(components.get("unique_object_classes", 0)),
        temporal_variance=float(components.get("temporal_variance", 0.0)),
    )


def _bucket_params(bucket: BucketRecord, activity_score: float) -> dict:
    """Parameter dict for the panoptic_buckets INSERT."""
    return {
        "bucket_id":          bucket.bucket_id,
        "serial_number":          bucket.serial_number,
        "camera_id":          bucket.camera_id,
        "bucket_start_utc":   bucket.bucket_start_utc.isoformat(),
        "bucket_end_utc":     bucket.bucket_end_utc.isoformat(),
        "bucket_status":      bucket.bucket_status,
        "schema_version":     bucket.schema_version,
        "detection_hash":     bucket.detection_hash,
        "activity_score":     activity_score,
        "activity_components": json.dumps(bucket.activity_components),
        "object_counts":      json.dumps(bucket.object_counts),
        "keyframe_candidates": json.dumps({
            "baseline_ts": bucket.keyframe_candidates.baseline_ts.isoformat()
                           if bucket.keyframe_candidates.baseline_ts else None,
            "peak_ts":     bucket.keyframe_candidates.peak_ts.isoformat()
                           if bucket.keyframe_candidates.peak_ts else None,
            "change_ts":   bucket.keyframe_candidates.change_ts.isoformat()
                           if bucket.keyframe_candidates.change_ts else None,
        }),
        "event_markers": json.dumps([
            {
                "ts":         m.ts.isoformat(),
                "event_type": m.event_type,
                "label":      m.label,
                "confidence": m.confidence,
            }
            for m in bucket.event_markers
        ]),
        "completeness": json.dumps({
            "detection_coverage":         bucket.completeness.detection_coverage,
            "stream_interrupted_seconds": bucket.completeness.stream_interrupted_seconds,
            "aggregator_restart_seen":    bucket.completeness.aggregator_restart_seen,
        }),
    }


def _job_payload_json(
    bucket: BucketRecord,
    model_profile: str,
    prompt_version: str,
) -> str:
    """
    JSON string for panoptic_jobs.payload.

    Contains everything the Summary Agent needs to identify the work unit.
    Full bucket data is fetched from panoptic_buckets by the agent at execution time.
    """
    return json.dumps({
        "bucket_id":      bucket.bucket_id,
        "serial_number":      bucket.serial_number,
        "camera_id":      bucket.camera_id,
        "model_profile":  model_profile,
        "prompt_version": prompt_version,
    })
