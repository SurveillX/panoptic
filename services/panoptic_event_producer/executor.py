"""
event_produce job executor.

Handles two payload shapes:

  {"source_type": "image",  "image_id": "..."}
      One panoptic_events row from the referenced panoptic_images row.

  {"source_type": "bucket", "bucket_id": "..."}
      One panoptic_events row per marker in the bucket's event_markers.

All writes are INSERT ... ON CONFLICT (event_id) DO NOTHING — the event_id
is content-addressed so reruns and duplicate deliveries are safe.

No external writes in this worker — only Postgres. The caller commits.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from shared.canonical.camera import resolve_canonical_camera_id
from shared.events.build import (
    build_event_row_from_bucket_marker,
    build_event_row_from_image,
)

log = logging.getLogger(__name__)


JobState = Literal["succeeded", "failed_terminal", "retry_wait"]


_INSERT_SQL = text("""
    INSERT INTO panoptic_events (
        event_id,
        serial_number, camera_id, scope_id,
        event_type, event_source,
        severity, confidence,
        start_time_utc, end_time_utc, event_time_utc,
        bucket_id, image_id,
        title, description, metadata_json,
        created_at, updated_at
    ) VALUES (
        :event_id,
        :serial_number, :camera_id, :scope_id,
        :event_type, :event_source,
        :severity, :confidence,
        :start_time_utc, :end_time_utc, :event_time_utc,
        :bucket_id, :image_id,
        :title, :description, CAST(:metadata_json AS jsonb),
        now(), now()
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
""")


def run_event_produce_job(
    conn,
    payload: dict,
    worker_id: str,
    engine: Engine,
) -> JobState:
    """
    Execute one event_produce job. Commits nothing — the worker commits
    after the lease check.
    """
    source_type = payload.get("source_type")

    if source_type == "image":
        return _produce_from_image(conn, payload, engine)

    if source_type == "bucket":
        return _produce_from_bucket(conn, payload, engine)

    log.error(
        "run_event_produce_job: unknown source_type=%r payload=%r",
        source_type, payload,
    )
    return "failed_terminal"


# ---------------------------------------------------------------------------
# source_type = "image"
# ---------------------------------------------------------------------------


def _produce_from_image(conn, payload: dict, engine: Engine) -> JobState:
    image_id = payload.get("image_id")
    if not image_id:
        log.error("_produce_from_image: missing image_id in payload=%r", payload)
        return "failed_terminal"

    row = conn.execute(
        text("""
            SELECT image_id, serial_number, camera_id, scope_id, trigger,
                   bucket_start_utc, bucket_end_utc, captured_at_utc,
                   caption_text, context_json
              FROM panoptic_images
             WHERE image_id = :image_id
        """),
        {"image_id": image_id},
    ).fetchone()

    if row is None:
        log.error("_produce_from_image: image_id=%s missing", image_id)
        return "failed_terminal"

    if row.trigger not in ("alert", "anomaly"):
        # baseline images do not become events; the webhook shouldn't have
        # enqueued one, but defend the boundary.
        log.info(
            "_produce_from_image: image_id=%s trigger=%s — skipping (baseline)",
            image_id, row.trigger,
        )
        return "succeeded"

    canonical_camera_id = resolve_canonical_camera_id(
        engine,
        serial_number=row.serial_number,
        raw_camera_id=row.camera_id,
        payload_type="image",
    )
    canonical_scope_id = f"{row.serial_number}:{canonical_camera_id}"

    image_row = {
        "image_id": row.image_id,
        "serial_number": row.serial_number,
        "camera_id": canonical_camera_id,
        "scope_id": canonical_scope_id,
        "trigger": row.trigger,
        "bucket_start_utc": row.bucket_start_utc,
        "bucket_end_utc": row.bucket_end_utc,
        "captured_at_utc": row.captured_at_utc,
        "caption_text": row.caption_text,
        "context_json": row.context_json or {},
    }

    event_row = build_event_row_from_image(image_row)
    _insert_event(conn, event_row)

    log.info(
        "_produce_from_image: image_id=%s → event_id=%s event_type=%s",
        image_id, event_row["event_id"], event_row["event_type"],
    )
    return "succeeded"


# ---------------------------------------------------------------------------
# source_type = "bucket"
# ---------------------------------------------------------------------------


def _produce_from_bucket(conn, payload: dict, engine: Engine) -> JobState:
    bucket_id = payload.get("bucket_id")
    if not bucket_id:
        log.error("_produce_from_bucket: missing bucket_id in payload=%r", payload)
        return "failed_terminal"

    row = conn.execute(
        text("""
            SELECT bucket_id, serial_number, camera_id,
                   bucket_start_utc, bucket_end_utc, event_markers
              FROM panoptic_buckets
             WHERE bucket_id = :bucket_id
        """),
        {"bucket_id": bucket_id},
    ).fetchone()

    if row is None:
        log.error("_produce_from_bucket: bucket_id=%s missing", bucket_id)
        return "failed_terminal"

    markers = row.event_markers or []
    if not markers:
        log.info(
            "_produce_from_bucket: bucket_id=%s has no markers — no-op",
            bucket_id,
        )
        return "succeeded"

    canonical_camera_id = resolve_canonical_camera_id(
        engine,
        serial_number=row.serial_number,
        raw_camera_id=row.camera_id,
        payload_type="bucket",
    )

    bucket_row = {
        "bucket_id": row.bucket_id,
        "serial_number": row.serial_number,
        "camera_id": canonical_camera_id,
        "bucket_start_utc": row.bucket_start_utc,
        "bucket_end_utc": row.bucket_end_utc,
    }

    produced = 0
    skipped = 0
    for marker in markers:
        marker_key = marker.get("event_type")
        if marker_key not in _KNOWN_MARKER_KEYS:
            # D-1c: summary agent has consumer branches for markers we don't
            # yet derive. Skip the unknowns instead of erroring the job.
            skipped += 1
            continue
        event_row = build_event_row_from_bucket_marker(bucket_row, marker)
        _insert_event(conn, event_row)
        produced += 1

    log.info(
        "_produce_from_bucket: bucket_id=%s produced=%d skipped=%d",
        bucket_id, produced, skipped,
    )
    return "succeeded"


# Known marker keys — matches shared/signals/derive.py. Extend together.
_KNOWN_MARKER_KEYS = frozenset({"spike", "after_hours"})


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------


def _insert_event(conn, event_row: dict) -> None:
    """INSERT ... ON CONFLICT DO NOTHING. Returns nothing; idempotent."""
    params = dict(event_row)
    params["metadata_json"] = json.dumps(event_row.get("metadata_json") or {})
    conn.execute(_INSERT_SQL, params)
