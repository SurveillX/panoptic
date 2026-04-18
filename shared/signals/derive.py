"""
Shared marker derivation.

Single source of truth for converting a finalized bucket's fragments into
event markers. Called from:

  - shared/clients/trailer_intake.py (during bucket finalization) to populate
    panoptic_buckets.event_markers
  - the event_producer worker / backfill (via shared/events/build.py) to
    convert bucket rows into panoptic_events rows

Only two marker types are produced today: `spike` and `after_hours`. The
summary agent has consumer branches for `drop`, `start`, `late_start`, and
`underperforming` that remain dormant until derivation logic is added in a
follow-on phase (see plan D-1c).
"""

from __future__ import annotations

from datetime import datetime

from shared.schemas.trailer_webhook import TrailerBucketData


# Canonical event_type labels emitted into bucket.event_markers and, after
# mapping in shared/events/build.py, into panoptic_events.event_type.
MARKER_SPIKE = "spike"
MARKER_AFTER_HOURS = "after_hours"

_SPIKE_ANOMALY_THRESHOLD = 0.7
_AFTER_HOURS_START_HOUR = 21  # UTC — inclusive
_AFTER_HOURS_END_HOUR = 6     # UTC — exclusive


def derive_markers(
    fragments: list[TrailerBucketData],
    bucket_start: datetime,
) -> list[dict]:
    """
    Derive event markers from a bucket's fragments.

    Returned marker dicts match the shape persisted in
    panoptic_buckets.event_markers and consumed by the summary agent:

        {"ts": iso8601, "event_type": str, "label": str, "confidence": float}
    """
    markers: list[dict] = []

    # Spike: anomaly_flag == 1 and anomaly_score >= 0.7.
    # Null anomaly_score means the scorer hasn't run yet — treat as not-a-spike.
    # Null max_count_at means no peak timestamp — fall back to bucket_start.
    for frag in fragments:
        score = frag.anomaly_score
        if frag.anomaly_flag == 1 and score is not None and score >= _SPIKE_ANOMALY_THRESHOLD:
            ts = (
                frag.max_count_at.isoformat()
                if frag.max_count_at is not None
                else bucket_start.isoformat()
            )
            markers.append({
                "ts": ts,
                "event_type": MARKER_SPIKE,
                "label": "activity_spike",
                "confidence": min(score, 1.0),
            })

    # After hours: bucket outside 06:00-21:00 UTC with actual detections.
    hour = bucket_start.hour
    if (hour < _AFTER_HOURS_END_HOUR or hour >= _AFTER_HOURS_START_HOUR) and any(
        f.total_detections > 0 for f in fragments
    ):
        markers.append({
            "ts": bucket_start.isoformat(),
            "event_type": MARKER_AFTER_HOURS,
            "label": "after hours activity",
            "confidence": 0.9,
        })

    return markers
