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


# Production-shipped marker keys. `underperforming` is omitted on purpose
# (plan §3 — conditional on Mode-1 FP gate against real yard data); flip
# this set to PRODUCED_HISTORY_MARKERS | {"underperforming"} once the
# rederive evaluation clears the gate.
PRODUCED_HISTORY_MARKERS: frozenset[str] = frozenset({
    MARKER_DROP,
    MARKER_START,
    MARKER_LATE_START,
})

# All markers implemented in this module, gated or not. Used by the
# rederive script to evaluate candidates (including `underperforming`)
# before they reach production.
IMPLEMENTED_HISTORY_MARKERS: frozenset[str] = frozenset({
    MARKER_DROP,
    MARKER_START,
    MARKER_LATE_START,
    MARKER_UNDERPERFORMING,
})


def derive_history_markers(
    *,
    total_detections: int,
    bucket_start: datetime,
    bucket_minutes: int,
    history: BucketHistory,
    produce: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """
    Derive history-dependent markers from aggregate bucket inputs + a
    BucketHistory snapshot.

    Returns a list of marker dicts shaped like `derive_markers`' output:
        {"ts": iso8601, "event_type": str, "label": str, "confidence": float}

    `produce` filters which marker families are attempted. When None
    (default), only PRODUCED_HISTORY_MARKERS fire — the safe set that's
    been validated for production. The rederive script passes a larger
    set (including `underperforming`) for Mode-1 evaluation against
    real historical data before flipping the production gate.

    `bucket_minutes` is carried through so per-marker logic can convert
    between minute-valued thresholds and bucket counts without hard-
    coding 15-minute cadence.
    """

    allowed = produce if produce is not None else PRODUCED_HISTORY_MARKERS
    markers: list[dict] = []

    if MARKER_DROP in allowed:
        drop = _derive_drop(
            total_detections=total_detections,
            bucket_start=bucket_start,
            history=history,
        )
        if drop is not None:
            markers.append(drop)

    if MARKER_START in allowed:
        start = _derive_start(
            total_detections=total_detections,
            bucket_start=bucket_start,
            history=history,
        )
        if start is not None:
            markers.append(start)

    if MARKER_LATE_START in allowed:
        late_start = _derive_late_start(
            total_detections=total_detections,
            bucket_start=bucket_start,
            history=history,
        )
        if late_start is not None:
            markers.append(late_start)

    if MARKER_UNDERPERFORMING in allowed:
        underperforming = _derive_underperforming(
            total_detections=total_detections,
            bucket_start=bucket_start,
            history=history,
        )
        if underperforming is not None:
            markers.append(underperforming)

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

    # Meaningful-drop threshold: mean - 2σ must itself clear the
    # noise floor. When std dwarfs mean (CoV ≥ ~0.5), a camera's
    # distribution is too bursty for a below-mean rule to be useful
    # — every zero-detection daytime bucket would fire drop on the
    # yard's high-variance cameras. Suppress instead.
    threshold = (
        history.rolling_mean_total_detections
        - _DROP_SIGMA * history.rolling_std_total_detections
    )
    if threshold < 1.0:
        return None
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


def _derive_start(
    *,
    total_detections: int,
    bucket_start: datetime,
    history: BucketHistory,
) -> dict | None:
    """
    Fire `start` on the first meaningful-activity bucket of the UTC day
    for this camera, provided the preceding stretch was sustained-quiet.

    Guards:
      - recent_quiet_run_minutes >= _START_MIN_QUIET_MINUTES  (≥ 2h trailing quiet)
      - total_detections >= _START_MIN_DETECTIONS             (not tree-shadow noise)
      - first_active_bucket_start_today is None               (first active bucket today)

    Label matches the summary agent's dormant branch ("start of
    activity"). Confidence is fixed at 0.9 — low-entropy event; there
    isn't a useful gradient to convey.
    """
    if history.recent_quiet_run_minutes < _START_MIN_QUIET_MINUTES:
        return None
    if total_detections < _START_MIN_DETECTIONS:
        return None
    if history.first_active_bucket_start_today is not None:
        return None

    return {
        "ts":         bucket_start.isoformat(),
        "event_type": MARKER_START,
        "label":      "start of activity",
        "confidence": 0.9,
    }


def _derive_late_start(
    *,
    total_detections: int,
    bucket_start: datetime,
    history: BucketHistory,
) -> dict | None:
    """
    Fire `late_start` when today's first active bucket is at least
    _LATE_START_HOUR_DELAY_THRESHOLD hours past this camera's typical
    first-active hour, based on the 14-day day-level baseline.

    Intentionally co-fires with `start` on the same bucket when both
    guards pass — the summary agent's dormant branches handle both
    independently (plan §6.8).

    UTC-day compromise (plan §2e): "today" and `typical_first_active_hour_utc`
    are both UTC-based. Sites in distant time zones may see edge-case
    markers near local midnight; documented in docs/M12.md.
    """
    if history.typical_first_active_hour_utc is None:
        return None
    if history.day_baseline_days_with_activity < _LATE_START_MIN_DAYS_WITH_ACTIVITY:
        return None
    if history.first_active_bucket_start_today is not None:
        return None
    if total_detections < _LATE_START_MIN_DETECTIONS:
        return None

    hour_delay = bucket_start.hour - history.typical_first_active_hour_utc
    if hour_delay < _LATE_START_HOUR_DELAY_THRESHOLD:
        return None

    # Plan §2c: clamp((hour_delay - 2) / 6, 0.5, 1.0). Floor at 0.5 so
    # every late_start carries enough severity to matter in UI sorts;
    # linear ramp past 5h so deeply late starts saturate at 1.0 by 8h.
    raw = (hour_delay - _LATE_START_HOUR_DELAY_THRESHOLD) / 6.0
    confidence = max(0.5, min(1.0, raw))

    return {
        "ts":         bucket_start.isoformat(),
        "event_type": MARKER_LATE_START,
        "label":      "late start",
        "confidence": confidence,
    }


def _derive_underperforming(
    *,
    total_detections: int,
    bucket_start: datetime,
    history: BucketHistory,
) -> dict | None:
    """
    Fire `underperforming` when a camera is active but running at a
    fraction of its usual intensity during its typical work window.

    Fuzziest of the M12 markers — plan §2d flags this as the highest
    false-positive risk. Ships to production only after Mode-1 rederive
    dry-run on real yard data clears the FP gate (§5a).

    Guards:
      - rolling_bucket_sample_size     >= 40     (≥ 10h of history)
      - rolling_mean_total_detections  >= 10     (real baseline)
      - 0 < current < mean - 1.5σ                (active but low)
      - hour in [typical_first_active_hour, typical_first_active_hour + 10)
      - minutes_since_first_active_today >= 30   (skip warm-up)
    """
    if history.rolling_bucket_sample_size < _UNDERPERFORMING_MIN_ROLLING_SAMPLE:
        return None
    if history.rolling_mean_total_detections < _UNDERPERFORMING_MIN_ROLLING_MEAN:
        return None

    # Active-but-low: strictly > 0 so this doesn't overlap drop, AND
    # strictly below 1.5σ from the baseline.
    if total_detections <= 0:
        return None
    threshold = (
        history.rolling_mean_total_detections
        - _UNDERPERFORMING_SIGMA * history.rolling_std_total_detections
    )
    if total_detections >= threshold:
        return None

    # Hour-of-day gate — must be inside the camera's typical work window.
    # Requires a day-level baseline; underperforming is not meaningful
    # without "normal work hours" context.
    if history.typical_first_active_hour_utc is None:
        return None
    typical = history.typical_first_active_hour_utc
    hour = bucket_start.hour
    if hour < typical or hour >= typical + _UNDERPERFORMING_ACTIVE_WINDOW_HOURS:
        return None

    # Warm-up skip — if today's first active bucket happened < 30 min ago,
    # the ramp-up hasn't finished; suppress.
    first_today = history.first_active_bucket_start_today
    if first_today is None:
        # No prior active bucket today AND current is active → this bucket
        # IS today's first active. Definitionally inside warm-up (0 min
        # elapsed since first-of-day == this one).
        return None
    minutes_since_first = int(
        (bucket_start - first_today).total_seconds() / 60
    )
    if minutes_since_first < _UNDERPERFORMING_WARMUP_MINUTES:
        return None

    # Confidence: clamp((mean - current) / (std*3 + 1), 0.3, 1.0).
    # 0.3 floor keeps underperforming below spike/drop in severity sorts.
    raw = (
        history.rolling_mean_total_detections - float(total_detections)
    ) / (history.rolling_std_total_detections * 3.0 + 1.0)
    confidence = max(0.3, min(1.0, raw))

    return {
        "ts":         bucket_start.isoformat(),
        "event_type": MARKER_UNDERPERFORMING,
        "label":      "site underperforming",
        "confidence": confidence,
    }
