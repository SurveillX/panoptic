"""
Bucket history — per-camera rolling / day-level baselines for marker
derivation.

Fetched once per bucket finalization (see shared.clients.cognia.ingest_bucket)
and passed into shared.signals.derive.derive_history_markers. Keeping the
fetch separate from derivation keeps derivation a pure function over
aggregate inputs, testable with synthetic BucketHistory objects.

Baseline shape (M12 revision 2):
  - Rolling-bucket baseline: last N buckets for (sn, camera), regardless
    of day boundary. Drives drop / underperforming.
  - Same-day context: first active bucket of today + consecutive-quiet
    run ending at the target bucket's boundary. Drives start and the
    first-of-day gate used by late_start.
  - Day-level baseline: median UTC hour at which this camera's first
    active bucket occurs across the last D calendar days. Drives
    late_start. Kept as *_days_considered + *_days_with_activity so
    guards can check the right sample size.

All queries use strictly-earlier buckets than the target `bucket_start`
so re-derivation over historical data cannot leak future information
into the baseline.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text


# ---------------------------------------------------------------------------
# Tunables — live here beside the fetcher so history-window widths are in
# one place. Per-marker thresholds (sample-size floors, 2σ / 1.5σ cutoffs,
# quiet-run minutes, warm-up minutes) live in shared/signals/derive.py.
# ---------------------------------------------------------------------------

# Rolling-bucket window: ~24 hours at 15-minute cadence.
_ROLLING_WINDOW_BUCKETS: int = 96

# Day-level window: how many calendar days we reach back for the typical
# first-active-hour baseline.
_DAY_BASELINE_WINDOW_DAYS: int = 14

# Per-bucket "quiet" floor used when counting the consecutive-quiet tail.
# Kept here (not in derive.py) because it's intrinsic to HOW we measure
# history, not to marker thresholds.
_QUIET_FLOOR_DETECTIONS: int = 1

# Ceiling on recent_quiet_run_minutes — a camera that's been silent for
# days shouldn't report 40_000+ minutes. 24h is "more than enough" for
# every downstream guard (start only needs 120).
_QUIET_RUN_CAP_MINUTES: int = 24 * 60


@dataclass
class BucketHistory:
    """
    Snapshot of per-camera history needed by derive_history_markers.

    Fields grouped by which marker they serve (see each derivation in
    shared/signals/derive.py).
    """

    # --- Rolling per-camera stats (last _ROLLING_WINDOW_BUCKETS buckets) ---
    # Used by: drop, underperforming.
    rolling_mean_total_detections: float
    rolling_std_total_detections: float
    rolling_bucket_sample_size: int

    # --- Same-day context for this camera ---
    # Used by: start (quiet-run), late_start (first-of-day gate),
    # underperforming (warm-up skip).
    first_active_bucket_start_today: datetime | None
    recent_quiet_run_minutes: int

    # --- Day-level baseline ---
    # Used by: late_start only.
    typical_first_active_hour_utc: int | None
    day_baseline_days_considered: int
    day_baseline_days_with_activity: int


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


def fetch_bucket_history(
    conn,
    *,
    serial_number: str,
    camera_id: str,
    bucket_start: datetime,
) -> BucketHistory:
    """
    Build a BucketHistory snapshot for (serial_number, camera_id) as of
    `bucket_start`.

    `conn` is an open SQLAlchemy Connection (not a session). `bucket_start`
    is an aware UTC datetime. Every query uses strict inequality against
    `bucket_start` so this function is safe to call during re-derivation
    against historical buckets — baselines never include or peek past the
    target bucket.
    """

    rolling_mean, rolling_std, rolling_n = _fetch_rolling_stats(
        conn,
        serial_number=serial_number,
        camera_id=camera_id,
        bucket_start=bucket_start,
    )

    first_active_today, quiet_run_minutes = _fetch_same_day_context(
        conn,
        serial_number=serial_number,
        camera_id=camera_id,
        bucket_start=bucket_start,
    )

    typical_hour, days_considered, days_with_activity = _fetch_day_baseline(
        conn,
        serial_number=serial_number,
        camera_id=camera_id,
        bucket_start=bucket_start,
    )

    return BucketHistory(
        rolling_mean_total_detections=rolling_mean,
        rolling_std_total_detections=rolling_std,
        rolling_bucket_sample_size=rolling_n,
        first_active_bucket_start_today=first_active_today,
        recent_quiet_run_minutes=quiet_run_minutes,
        typical_first_active_hour_utc=typical_hour,
        day_baseline_days_considered=days_considered,
        day_baseline_days_with_activity=days_with_activity,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fetch_rolling_stats(
    conn,
    *,
    serial_number: str,
    camera_id: str,
    bucket_start: datetime,
) -> tuple[float, float, int]:
    """Return (mean, std, sample_size) of total_detections across the last
    N buckets before `bucket_start` for this camera. total_detections is
    reconstructed from activity_components.object_count_total, which is
    the canonical aggregate field persisted at bucket write time."""

    rows = conn.execute(
        text(
            """
            SELECT activity_components
              FROM panoptic_buckets
             WHERE serial_number = :sn
               AND camera_id     = :cam
               AND bucket_start_utc < :ts
             ORDER BY bucket_start_utc DESC
             LIMIT :lim
            """
        ),
        {
            "sn":  serial_number,
            "cam": camera_id,
            "ts":  bucket_start,
            "lim": _ROLLING_WINDOW_BUCKETS,
        },
    ).fetchall()

    counts = [float((r.activity_components or {}).get("object_count_total", 0)) for r in rows]
    if not counts:
        return 0.0, 0.0, 0

    mean = statistics.mean(counts)
    std = statistics.stdev(counts) if len(counts) > 1 else 0.0
    return mean, std, len(counts)


def _fetch_same_day_context(
    conn,
    *,
    serial_number: str,
    camera_id: str,
    bucket_start: datetime,
) -> tuple[datetime | None, int]:
    """Return (first_active_bucket_start_today, recent_quiet_run_minutes).

    "Today" is the UTC calendar day containing `bucket_start`. "Active"
    means activity_components.object_count_total >= _QUIET_FLOOR_DETECTIONS.

    recent_quiet_run_minutes is the elapsed minutes since the last active
    bucket ended. This is time-based (not bucket-count-based) so gaps in
    bucket emission (trailer offline, sparse test fixtures) count as
    quiet time rather than breaking the walk. Capped at _QUIET_RUN_CAP_MINUTES
    so a long-dormant camera doesn't produce silly-large numbers.
    """

    day_floor = _utc_day_floor(bucket_start)

    # Earliest active bucket of today (strictly before `bucket_start`).
    row = conn.execute(
        text(
            """
            SELECT bucket_start_utc
              FROM panoptic_buckets
             WHERE serial_number = :sn
               AND camera_id     = :cam
               AND bucket_start_utc >= :day_floor
               AND bucket_start_utc <  :ts
               AND (activity_components->>'object_count_total')::float >= :floor
             ORDER BY bucket_start_utc ASC
             LIMIT 1
            """
        ),
        {
            "sn":        serial_number,
            "cam":       camera_id,
            "day_floor": day_floor,
            "ts":        bucket_start,
            "floor":     _QUIET_FLOOR_DETECTIONS,
        },
    ).fetchone()
    first_active_today = row.bucket_start_utc if row is not None else None

    # Minutes since the last active bucket ended. When no prior active
    # bucket exists (new camera / long-dormant), cap at 24h so the number
    # stays sane — any guard that cares about "long enough" is satisfied.
    row = conn.execute(
        text(
            """
            SELECT EXTRACT(EPOCH FROM (:ts - MAX(bucket_end_utc))) / 60 AS quiet_minutes
              FROM panoptic_buckets
             WHERE serial_number = :sn
               AND camera_id     = :cam
               AND bucket_start_utc < :ts
               AND (activity_components->>'object_count_total')::float >= :floor
            """
        ),
        {
            "sn":    serial_number,
            "cam":   camera_id,
            "ts":    bucket_start,
            "floor": _QUIET_FLOOR_DETECTIONS,
        },
    ).fetchone()
    if row is None or row.quiet_minutes is None:
        quiet_run_minutes = _QUIET_RUN_CAP_MINUTES
    else:
        quiet_run_minutes = min(
            _QUIET_RUN_CAP_MINUTES,
            max(0, int(row.quiet_minutes)),
        )

    return first_active_today, quiet_run_minutes


def _fetch_day_baseline(
    conn,
    *,
    serial_number: str,
    camera_id: str,
    bucket_start: datetime,
) -> tuple[int | None, int, int]:
    """Return (typical_first_active_hour_utc, days_considered, days_with_activity)
    across the last _DAY_BASELINE_WINDOW_DAYS calendar days before today."""

    day_floor = _utc_day_floor(bucket_start)
    window_start = day_floor - timedelta(days=_DAY_BASELINE_WINDOW_DAYS)

    rows = conn.execute(
        text(
            """
            SELECT date_trunc('day', bucket_start_utc)::date AS day,
                   MIN(bucket_start_utc) AS first_active_at
              FROM panoptic_buckets
             WHERE serial_number = :sn
               AND camera_id     = :cam
               AND bucket_start_utc >= :window_start
               AND bucket_start_utc <  :day_floor
               AND (activity_components->>'object_count_total')::float >= :floor
             GROUP BY date_trunc('day', bucket_start_utc)
            """
        ),
        {
            "sn":           serial_number,
            "cam":          camera_id,
            "window_start": window_start,
            "day_floor":    day_floor,
            "floor":        _QUIET_FLOOR_DETECTIONS,
        },
    ).fetchall()

    days_with_activity = len(rows)
    if days_with_activity == 0:
        return None, _DAY_BASELINE_WINDOW_DAYS, 0

    first_hours = [r.first_active_at.hour for r in rows]
    typical_hour = int(statistics.median(first_hours))

    return typical_hour, _DAY_BASELINE_WINDOW_DAYS, days_with_activity


def _utc_day_floor(ts: datetime) -> datetime:
    """Midnight UTC of the day containing `ts`."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = ["BucketHistory", "fetch_bucket_history"]
