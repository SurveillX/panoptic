"""
Shared marker derivation.

Single source of truth for converting finalized bucket data into event
markers. Two entry points:

  derive_markers(fragments, bucket_start)
      Fragment-dependent markers that can be computed without DB access
      (spike, after_hours). Called from trailer_intake during transform.

  derive_history_markers(*, total_detections, bucket_start, bucket_minutes,
                          history)
      History-dependent markers (drop, start, late_start, underperforming).
      Called from shared.clients.cognia.ingest_bucket after BucketHistory
      is fetched. Derivation operates on aggregate inputs — fragments are
      no longer available at this point.

Markers from both paths merge into panoptic_buckets.event_markers and
flow through shared/events/build.build_event_row_from_bucket_marker into
panoptic_events.

All thresholds live in this module. Consumers see labels, not numbers.
"""

from __future__ import annotations

from datetime import datetime

from shared.schemas.trailer_webhook import TrailerBucketData
from shared.signals.history import BucketHistory


# ---------------------------------------------------------------------------
# Marker keys — must match shared/events/build._MARKER_TO_EVENT_TYPE.
# ---------------------------------------------------------------------------

MARKER_SPIKE = "spike"
MARKER_AFTER_HOURS = "after_hours"
MARKER_DROP = "drop"
MARKER_START = "start"
MARKER_LATE_START = "late_start"
MARKER_UNDERPERFORMING = "underperforming"


# ---------------------------------------------------------------------------
# Thresholds — fragment-based markers (spike, after_hours). Unchanged.
# ---------------------------------------------------------------------------

_SPIKE_ANOMALY_THRESHOLD = 0.7
_AFTER_HOURS_START_HOUR = 21  # UTC — inclusive
_AFTER_HOURS_END_HOUR = 6     # UTC — exclusive


# ---------------------------------------------------------------------------
# Thresholds — history-based markers. Every constant named; docs/M12.md
# "tuning knobs" section references these by name.
# ---------------------------------------------------------------------------

# drop — activity collapse
_DROP_MIN_ROLLING_SAMPLE = 16          # ≥ 4h of history at 15-min cadence
_DROP_MIN_ROLLING_MEAN = 5.0           # camera must have a real baseline
_DROP_SIGMA = 2.0                      # current < mean - 2σ

# start — first meaningful activity after sustained quiet
_START_MIN_QUIET_MINUTES = 120         # ≥ 2h sustained-quiet tail
_START_MIN_DETECTIONS = 5              # meaningful, not tree-shadow noise

# late_start — delayed day-start vs. camera's norm
_LATE_START_MIN_DAYS_WITH_ACTIVITY = 5 # stable baseline floor
_LATE_START_HOUR_DELAY_THRESHOLD = 2   # ≥ 2h later than typical first hour
_LATE_START_MIN_DETECTIONS = 5

# underperforming — active but well below norm
_UNDERPERFORMING_MIN_ROLLING_SAMPLE = 40    # ≥ 10h of history
_UNDERPERFORMING_MIN_ROLLING_MEAN = 10.0    # tighter than drop
_UNDERPERFORMING_SIGMA = 1.5                # looser cutoff — sustained-low signal
_UNDERPERFORMING_WARMUP_MINUTES = 30        # skip natural ramp-up
_UNDERPERFORMING_ACTIVE_WINDOW_HOURS = 10   # hours after typical_first_active_hour


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


# ---------------------------------------------------------------------------
# History-based derivation
# ---------------------------------------------------------------------------


def derive_history_markers(
    *,
    total_detections: int,
    bucket_start: datetime,
    bucket_minutes: int,
    history: BucketHistory,
) -> list[dict]:
    """
    Derive history-dependent markers from aggregate bucket inputs + a
    BucketHistory snapshot.

    Returns a list of marker dicts shaped like `derive_markers`' output:
        {"ts": iso8601, "event_type": str, "label": str, "confidence": float}

    The caller merges the returned list with fragment-based markers into
    `panoptic_buckets.event_markers`. Each per-marker helper is enabled
    phase-by-phase (M12 plan P12b → P12e); this function returns an empty
    list until the first phase lands.

    `bucket_minutes` is carried through so per-marker logic can convert
    between minute-valued thresholds and bucket counts without hard-
    coding 15-minute cadence.
    """

    markers: list[dict] = []

    drop = _derive_drop(
        total_detections=total_detections,
        bucket_start=bucket_start,
        history=history,
    )
    if drop is not None:
        markers.append(drop)

    # P12c: start
    # P12d: late_start
    # P12e: underperforming (conditional — Mode-1 FP gate)

    return markers


def _derive_drop(
    *,
    total_detections: int,
    bucket_start: datetime,
    history: BucketHistory,
) -> dict | None:
    """
    Fire `drop` when the current bucket's total_detections collapses below
    the camera's rolling baseline during daytime hours.

    Guards:
      - rolling_bucket_sample_size  >= _DROP_MIN_ROLLING_SAMPLE     (thin history → no fire)
      - rolling_mean_total_detections >= _DROP_MIN_ROLLING_MEAN     (always-dead cam → no fire)
      - bucket_start.hour in daytime window                         (after_hours covers night)
      - total_detections < max(1, mean - _DROP_SIGMA * std)         (sharp drop required)

    Confidence scales with magnitude of the drop relative to σ:
        clamp((mean - current) / (std + 1), 0, 1)
    """
    if history.rolling_bucket_sample_size < _DROP_MIN_ROLLING_SAMPLE:
        return None
    if history.rolling_mean_total_detections < _DROP_MIN_ROLLING_MEAN:
        return None

    # Daytime-only — the inverse of the after_hours window so a bucket is
    # covered by exactly one "quiet-shaped" marker type at any given hour.
    hour = bucket_start.hour
    if hour < _AFTER_HOURS_END_HOUR or hour >= _AFTER_HOURS_START_HOUR:
        return None

    threshold = max(
        1.0,
        history.rolling_mean_total_detections
        - _DROP_SIGMA * history.rolling_std_total_detections,
    )
    if total_detections >= threshold:
        return None

    raw = (
        history.rolling_mean_total_detections - float(total_detections)
    ) / (history.rolling_std_total_detections + 1.0)
    confidence = max(0.0, min(1.0, raw))

    return {
        "ts":         bucket_start.isoformat(),
        "event_type": MARKER_DROP,
        "label":      "activity_drop",
        "confidence": confidence,
    }
