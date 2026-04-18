"""
Event row construction — shared by the event_producer worker and backfill.

Two entry points:

  build_event_row_from_image(image_row) -> dict
      One panoptic_events row per alert/anomaly image. Called after the
      image has been committed to panoptic_images.

  build_event_row_from_bucket_marker(bucket_row, marker) -> dict
      One panoptic_events row per marker in a bucket's event_markers list.
      Called after the bucket has been committed to panoptic_buckets.

Both builders produce a dict shaped exactly for INSERT INTO panoptic_events
— no ORM involvement, no surprises. event_id is content-addressed by
generate_event_id so repeated calls with identical inputs emit identical
rows (ON CONFLICT DO NOTHING handles the idempotency at the DB edge).

Identity hash rules (plan D-4, spec §7):
  - Image-trigger event_id hashes only (source, image_id). image_id is
    already content-addressed, so this transitively covers the identifying
    fields.
  - Bucket-marker event_id hashes (source, bucket_id, marker_key, marker_ts).
    Same bucket can have multiple markers of different types or at different
    timestamps — marker_key + ts disambiguate.
  - Enrichment fields (bucket_id on image events, image_id on bucket events,
    captions, severities) MUST NOT enter the hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from shared.schemas.event import (
    EVENT_TYPE_ACTIVITY_SPIKE,
    EVENT_TYPE_AFTER_HOURS,
    EVENT_TYPE_ALERT_CREATED,
    EVENT_TYPE_ANOMALY_DETECTED,
)


# Marker-key (from shared/signals/derive) → canonical event_type on the event row.
_MARKER_TO_EVENT_TYPE: dict[str, str] = {
    "spike": EVENT_TYPE_ACTIVITY_SPIKE,
    "after_hours": EVENT_TYPE_AFTER_HOURS,
}

# Image trigger → canonical event_type. 'baseline' never produces an event.
_TRIGGER_TO_EVENT_TYPE: dict[str, str] = {
    "alert": EVENT_TYPE_ALERT_CREATED,
    "anomaly": EVENT_TYPE_ANOMALY_DETECTED,
}


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_event_id(
    *,
    event_source: str,
    image_id: str | None = None,
    bucket_id: str | None = None,
    marker_key: str | None = None,
    marker_ts: str | None = None,
) -> str:
    """
    Deterministic event ID.

    For image-trigger events, pass event_source="image_trigger" and image_id.
    For bucket-marker events, pass event_source="bucket_marker", bucket_id,
    marker_key (lowercase, e.g. "spike"), and marker_ts (iso8601 string as
    it appears in bucket.event_markers[*].ts).

    Enrichment fields must not be passed — see module docstring.
    """
    if event_source == "image_trigger":
        if not image_id:
            raise ValueError("image_id is required for image_trigger events")
        payload: dict[str, Any] = {
            "event_source": "image_trigger",
            "image_id": image_id,
        }
    elif event_source == "bucket_marker":
        if not bucket_id or not marker_key or not marker_ts:
            raise ValueError(
                "bucket_id, marker_key, and marker_ts are all required for "
                "bucket_marker events"
            )
        payload = {
            "bucket_id": bucket_id,
            "event_source": "bucket_marker",
            "marker_key": marker_key,
            "marker_ts": marker_ts,
        }
    else:
        raise ValueError(f"unknown event_source: {event_source!r}")

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def build_event_row_from_image(image_row: dict) -> dict:
    """
    Build a panoptic_events row dict from a panoptic_images row dict.

    Callers: event_producer worker (when source_type='image') and
    backfill_events.py --source image. The image_row dict must expose at
    minimum: image_id, serial_number, camera_id, scope_id, trigger,
    bucket_start_utc, bucket_end_utc, captured_at_utc, caption_text,
    context_json.

    Only alert/anomaly triggers produce events. 'baseline' raises.
    """
    trigger = image_row["trigger"]
    if trigger not in _TRIGGER_TO_EVENT_TYPE:
        raise ValueError(
            f"image trigger {trigger!r} does not produce events "
            "(baseline images are not events)"
        )

    event_type = _TRIGGER_TO_EVENT_TYPE[trigger]
    image_id = image_row["image_id"]

    event_id = generate_event_id(event_source="image_trigger", image_id=image_id)

    # event_time prefers captured_at_utc; falls back to bucket_start_utc when
    # the trailer didn't send a timestamp (shouldn't happen for alert/anomaly
    # per TrailerImageMetadata validator, but stays defensive at the boundary).
    event_time = image_row.get("captured_at_utc") or image_row["bucket_start_utc"]

    context = image_row.get("context_json") or {}
    # severity and confidence drawn from the trailer-provided anomaly score
    # when available; otherwise null. Keep the raw context in metadata_json
    # for audit.
    severity = context.get("max_anomaly_score") if isinstance(context, dict) else None
    confidence = severity  # single-value proxy for v1

    return {
        "event_id": event_id,
        "serial_number": image_row["serial_number"],
        "camera_id": image_row["camera_id"],
        "scope_id": image_row["scope_id"],
        "event_type": event_type,
        "event_source": "image_trigger",
        "severity": _clamp_unit(severity),
        "confidence": _clamp_unit(confidence),
        "start_time_utc": image_row["bucket_start_utc"],
        "end_time_utc": image_row["bucket_end_utc"],
        "event_time_utc": event_time,
        "bucket_id": None,  # enrichment — filled in later if bucket is resolvable
        "image_id": image_id,
        "title": _image_title(event_type),
        "description": image_row.get("caption_text"),
        "metadata_json": {
            "trigger": trigger,
            "context": context if isinstance(context, dict) else {},
        },
    }


def build_event_row_from_bucket_marker(bucket_row: dict, marker: dict) -> dict:
    """
    Build a panoptic_events row dict from a bucket row + one marker dict.

    Callers: event_producer worker (when source_type='bucket', iterating
    bucket.event_markers) and backfill_events.py --source bucket.

    Marker dict shape (from shared/signals/derive.derive_markers):
        {"ts": iso8601, "event_type": str, "label": str, "confidence": float}

    The marker's "event_type" field is the internal marker key (e.g. "spike",
    "after_hours"); the canonical public event_type is resolved via the
    _MARKER_TO_EVENT_TYPE map. Markers not in the map are skipped by the
    caller, not silently mislabeled — raise here if one slips through.
    """
    marker_key = marker["event_type"]
    if marker_key not in _MARKER_TO_EVENT_TYPE:
        raise ValueError(
            f"marker key {marker_key!r} has no canonical event_type mapping"
        )

    marker_ts = marker["ts"]
    bucket_id = bucket_row["bucket_id"]

    event_id = generate_event_id(
        event_source="bucket_marker",
        bucket_id=bucket_id,
        marker_key=marker_key,
        marker_ts=marker_ts,
    )

    marker_confidence = marker.get("confidence")

    return {
        "event_id": event_id,
        "serial_number": bucket_row["serial_number"],
        "camera_id": bucket_row["camera_id"],
        "scope_id": f"{bucket_row['serial_number']}:{bucket_row['camera_id']}",
        "event_type": _MARKER_TO_EVENT_TYPE[marker_key],
        "event_source": "bucket_marker",
        "severity": _clamp_unit(marker_confidence),
        "confidence": _clamp_unit(marker_confidence),
        "start_time_utc": bucket_row["bucket_start_utc"],
        "end_time_utc": bucket_row["bucket_end_utc"],
        "event_time_utc": marker_ts,
        "bucket_id": bucket_id,
        "image_id": None,  # enrichment — set later when a correlating image is known
        "title": marker.get("label"),
        "description": None,
        "metadata_json": {"marker": marker},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_unit(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _image_title(event_type: str) -> str:
    if event_type == EVENT_TYPE_ALERT_CREATED:
        return "Alert created"
    if event_type == EVENT_TYPE_ANOMALY_DETECTED:
        return "Anomaly detected"
    return event_type


# Explicit re-exports for convenient import paths.
__all__ = [
    "generate_event_id",
    "build_event_row_from_image",
    "build_event_row_from_bucket_marker",
]
