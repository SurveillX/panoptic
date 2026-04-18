"""
Trailer webhook intake — fragment aggregation and transformation.

Trailers send one POST per object_type per 15-minute bucket window.
This module:
  1. Stores each fragment in a Redis aggregation hash.
  2. A background finalizer detects quiet hashes (>30s since last update).
  3. Finalizer reads all fragments, transforms to a BucketRecord, and calls
     ingest_bucket() to enter the existing pipeline.

Late-arriving fragments (after finalization):
  A done marker (panoptic:agg:done:{sn}:{cam}:{start}) is set at finalization
  with 1h TTL.  store_fragment() checks for this marker BEFORE creating any
  Redis state.  Late fragments are discarded with a warning log.

Identity rule:
  The unique camera identity is (serial_number, camera_id).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import redis

from shared.clients.cognia import ingest_bucket
from shared.schemas.bucket import generate_bucket_id
from shared.schemas.trailer_webhook import TrailerBucketData, TrailerBucketPayload

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Redis key prefixes
_AGG_PREFIX = "panoptic:agg"
_AGG_ACTIVE_SET = "panoptic:agg:active"
_AGG_DONE_PREFIX = "panoptic:agg:done"

# Finalization timing
FINALIZE_QUIET_SECONDS = 30
_AGG_HASH_TTL = 3600       # 1 hour safety net
_DONE_MARKER_TTL = 3600    # 1 hour


# ---------------------------------------------------------------------------
# Fragment storage
# ---------------------------------------------------------------------------

def _agg_key(serial_number: str, camera_id: str, bucket_start_iso: str) -> str:
    return f"{_AGG_PREFIX}:{serial_number}:{camera_id}:{bucket_start_iso}"


def _done_key(serial_number: str, camera_id: str, bucket_start_iso: str) -> str:
    return f"{_AGG_DONE_PREFIX}:{serial_number}:{camera_id}:{bucket_start_iso}"


def store_fragment(r: redis.Redis, payload: TrailerBucketPayload) -> bool:
    """
    Store a per-object-type fragment in the Redis aggregation hash.

    Returns True if stored, False if discarded (duplicate event_id or
    late arrival after finalization).

    Checks the done marker BEFORE creating any Redis state.
    """
    sn = payload.serial_number
    cam = payload.camera_id
    bucket_start_iso = payload.bucket.bucket_start.isoformat()
    obj_type = payload.bucket.object_type

    # Check done marker — discard late fragments
    done = _done_key(sn, cam, bucket_start_iso)
    if r.exists(done):
        log.warning(
            "store_fragment: late fragment discarded sn=%s cam=%s start=%s type=%s",
            sn, cam, bucket_start_iso, obj_type,
        )
        return False

    key = _agg_key(sn, cam, bucket_start_iso)

    # Store fragment data
    fragment_json = payload.bucket.model_dump_json()
    r.hset(key, obj_type, fragment_json)
    r.hset(key, "_updated_at", str(time.time()))
    r.expire(key, _AGG_HASH_TTL)

    # Track in active set
    r.sadd(_AGG_ACTIVE_SET, key)

    log.debug(
        "store_fragment: stored sn=%s cam=%s start=%s type=%s",
        sn, cam, bucket_start_iso, obj_type,
    )
    return True


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------

def scan_and_finalize(
    r: redis.Redis,
    engine,
    *,
    model_profile: str = "default",
    prompt_version: str = "v1",
) -> int:
    """
    Scan active aggregation hashes and finalize any that have been quiet
    for FINALIZE_QUIET_SECONDS.

    Returns the number of buckets finalized.
    """
    active_keys = r.smembers(_AGG_ACTIVE_SET)
    if not active_keys:
        return 0

    now = time.time()
    finalized = 0

    for key_bytes in active_keys:
        key = key_bytes if isinstance(key_bytes, str) else key_bytes.decode()

        # Check if hash still exists (may have been cleaned up)
        updated_at_raw = r.hget(key, "_updated_at")
        if updated_at_raw is None:
            r.srem(_AGG_ACTIVE_SET, key)
            continue

        updated_at = float(updated_at_raw)
        if now - updated_at < FINALIZE_QUIET_SECONDS:
            continue

        # Ready to finalize
        try:
            _finalize_one(r, engine, key, model_profile=model_profile, prompt_version=prompt_version)
            finalized += 1
        except Exception as exc:
            log.error("scan_and_finalize: error finalizing %s: %s", key, exc)

    return finalized


def _finalize_one(
    r: redis.Redis,
    engine,
    agg_key: str,
    *,
    model_profile: str,
    prompt_version: str,
) -> None:
    """
    Finalize a single aggregation hash: transform + ingest + cleanup.
    """
    # Parse key: panoptic:agg:{sn}:{cam}:{bucket_start_iso}
    parts = agg_key.split(":", 4)
    # parts = ["vil", "agg", sn, cam, bucket_start_iso]
    sn = parts[2]
    cam = parts[3]
    bucket_start_iso = parts[4]

    # Read all fragments (skip internal fields starting with _)
    all_fields = r.hgetall(agg_key)
    fragments: list[TrailerBucketData] = []
    for field, value in all_fields.items():
        field_str = field if isinstance(field, str) else field.decode()
        if field_str.startswith("_"):
            continue
        value_str = value if isinstance(value, str) else value.decode()
        fragments.append(TrailerBucketData.model_validate_json(value_str))

    if not fragments:
        log.warning("_finalize_one: no fragments in %s — cleaning up", agg_key)
        r.delete(agg_key)
        r.srem(_AGG_ACTIVE_SET, agg_key)
        return

    # Transform to BucketRecord dict
    bucket_dict = transform_to_bucket_record(fragments, sn, cam)

    # Ingest through existing pipeline
    result = ingest_bucket(
        engine,
        r,
        bucket_dict,
        model_profile=model_profile,
        prompt_version=prompt_version,
    )

    log.info(
        "_finalize_one: finalized sn=%s cam=%s start=%s fragments=%d "
        "bucket_id=%s action=%s",
        sn, cam, bucket_start_iso, len(fragments),
        result.bucket_id, result.bucket_action,
    )

    # Set done marker BEFORE deleting hash
    done = _done_key(sn, cam, bucket_start_iso)
    r.set(done, "1", ex=_DONE_MARKER_TTL)

    # Cleanup
    r.delete(agg_key)
    r.srem(_AGG_ACTIVE_SET, agg_key)


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------

def transform_to_bucket_record(
    fragments: list[TrailerBucketData],
    serial_number: str,
    camera_id: str,
) -> dict:
    """
    Transform aggregated per-object-type fragments into a dict that
    validates against BucketRecord.

    The resulting dict is passed to ingest_bucket() which recomputes
    activity_score from activity_components + camera history.
    """
    first = fragments[0]
    bucket_start = first.bucket_start
    bucket_end = first.bucket_end
    bucket_minutes = first.bucket_minutes

    # object_counts: {object_type: unique_tracker_ids}
    object_counts = {f.object_type: f.unique_tracker_ids for f in fragments}

    # activity_components
    # Treat None stats as 0 — trailer aggregator omits them when the bucket
    # had too few samples for a meaningful statistic.
    total_detections = sum(f.total_detections for f in fragments)
    unique_classes = len(set(f.object_type for f in fragments))
    max_std_dev = max((f.std_dev_count for f in fragments if f.std_dev_count is not None), default=0.0)
    sum_mean_count = sum((f.mean_count or 0.0) for f in fragments)
    max_duty_cycle = max(f.duty_cycle for f in fragments)

    activity_components = {
        "object_count_total": total_detections,
        "unique_object_classes": unique_classes,
        "temporal_variance": max_std_dev,
        "c1": round(sum_mean_count / 30.0, 4),
        "c2": round(max_duty_cycle, 4),
        "c3": round(max_std_dev / 15.0, 4),
    }

    # keyframe_candidates
    # peak_ts: max_count_at from fragment with highest max_count.
    #          Fall back to bucket_start if the peak fragment has no timestamp
    #          (e.g. zero-detection bucket where max_count_at is null).
    peak_frag = max(fragments, key=lambda f: f.max_count)
    peak_ts = (
        peak_frag.max_count_at.isoformat()
        if peak_frag.max_count_at is not None
        else bucket_start.isoformat()
    )

    # change_ts: earliest first_detection_at across fragments that have one.
    #            If no fragment saw a detection, use bucket_start.
    detected = [f for f in fragments if f.first_detection_at is not None]
    if detected:
        earliest_detection = min(detected, key=lambda f: f.first_detection_at)
        change_ts = earliest_detection.first_detection_at.isoformat()
    else:
        change_ts = bucket_start.isoformat()

    keyframe_candidates = {
        "baseline_ts": bucket_start.isoformat(),
        "peak_ts": peak_ts,
        "change_ts": change_ts,
    }

    # event_markers
    event_markers = _derive_event_markers(fragments, bucket_start)

    # completeness
    # Clamp duty_cycle into [0, 1] before deriving completeness values.
    # Real trailer's aggregator reports duty_cycle > 1.0 (tracker-seconds
    # over bucket-seconds with concurrent trackers — not the fraction-of-
    # time-active our schema expects). Our BucketRecord.completeness has
    # explicit ge=0 / le=1 bounds on these derived fields.
    coverage = min(1.0, max(0.0, max_duty_cycle))
    completeness = {
        "detection_coverage": coverage,
        "stream_interrupted_seconds": max(0, int(bucket_minutes * 60 * (1 - coverage))),
        "aggregator_restart_seen": False,
    }

    # detection_hash
    detection_hash = _compute_detection_hash(fragments)

    # activity_score placeholder — ingest_bucket recomputes
    activity_score = 0.0

    # bucket_id
    bucket_id = generate_bucket_id(
        serial_number=serial_number,
        camera_id=camera_id,
        start_utc=bucket_start,
        end_utc=bucket_end,
        detection_hash=detection_hash,
        schema_version=SCHEMA_VERSION,
    )

    return {
        "bucket_id": bucket_id,
        "serial_number": serial_number,
        "camera_id": camera_id,
        "bucket_start_utc": bucket_start.isoformat(),
        "bucket_end_utc": bucket_end.isoformat(),
        "bucket_status": "complete",
        "schema_version": SCHEMA_VERSION,
        "detection_hash": detection_hash,
        "activity_score": activity_score,
        "activity_components": activity_components,
        "object_counts": object_counts,
        "keyframe_candidates": keyframe_candidates,
        "event_markers": event_markers,
        "completeness": completeness,
    }


def _derive_event_markers(
    fragments: list[TrailerBucketData],
    bucket_start: datetime,
) -> list[dict]:
    """Derive event markers from fragment data."""
    markers: list[dict] = []

    # Spike: anomaly_flag == 1 and anomaly_score >= 0.7.
    # Null anomaly_score means the scorer hasn't run yet — treat as not-a-spike.
    # Null max_count_at means no peak timestamp — fall back to bucket_start.
    for frag in fragments:
        score = frag.anomaly_score
        if frag.anomaly_flag == 1 and score is not None and score >= 0.7:
            ts = (
                frag.max_count_at.isoformat()
                if frag.max_count_at is not None
                else bucket_start.isoformat()
            )
            markers.append({
                "ts": ts,
                "event_type": "spike",
                "label": "activity_spike",
                "confidence": min(score, 1.0),
            })

    # After hours: bucket outside 06:00-21:00 UTC
    hour = bucket_start.hour
    if hour < 6 or hour >= 21:
        # Only add if there were actual detections
        if any(f.total_detections > 0 for f in fragments):
            markers.append({
                "ts": bucket_start.isoformat(),
                "event_type": "after_hours",
                "label": "after hours activity",
                "confidence": 0.9,
            })

    return markers


def _compute_detection_hash(fragments: list[TrailerBucketData]) -> str:
    """Deterministic hash of fragment detection data."""
    payload = json.dumps(
        sorted(
            [
                {
                    "object_type": f.object_type,
                    "total_detections": f.total_detections,
                    "unique_tracker_ids": f.unique_tracker_ids,
                    "bucket_start": f.bucket_start.isoformat(),
                    "bucket_end": f.bucket_end.isoformat(),
                }
                for f in fragments
            ],
            key=lambda x: x["object_type"],
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()
