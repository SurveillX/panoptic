"""
Tests for shared.signals.derive — both fragment-based and history-based
marker derivation.

Fragment-based tests exercise `derive_markers` (spike, after_hours).
History-based tests exercise `derive_history_markers`. P12a ships with
the skeleton only; each subsequent phase (P12b → P12e) adds the
positive / negative / thin-history / noise-floor / edge cases for one
marker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.signals.derive import (
    MARKER_AFTER_HOURS,
    MARKER_DROP,
    MARKER_SPIKE,
    MARKER_START,
    derive_history_markers,
    derive_markers,
)
from shared.signals.history import BucketHistory


UTC = timezone.utc


def _empty_history() -> BucketHistory:
    return BucketHistory(
        rolling_mean_total_detections=0.0,
        rolling_std_total_detections=0.0,
        rolling_bucket_sample_size=0,
        first_active_bucket_start_today=None,
        recent_quiet_run_minutes=0,
        typical_first_active_hour_utc=None,
        day_baseline_days_considered=14,
        day_baseline_days_with_activity=0,
    )


def _active_history(
    *,
    mean: float = 100.0,
    std: float = 30.0,
    n: int = 96,
    first_today: datetime | None = None,
    quiet_minutes: int = 0,
    typical_hour: int | None = 7,
    days_with_activity: int = 10,
) -> BucketHistory:
    return BucketHistory(
        rolling_mean_total_detections=mean,
        rolling_std_total_detections=std,
        rolling_bucket_sample_size=n,
        first_active_bucket_start_today=first_today,
        recent_quiet_run_minutes=quiet_minutes,
        typical_first_active_hour_utc=typical_hour,
        day_baseline_days_considered=14,
        day_baseline_days_with_activity=days_with_activity,
    )


# ---------------------------------------------------------------------------
# derive_history_markers — contract tests
# ---------------------------------------------------------------------------


class TestDeriveHistoryMarkersContract:
    def test_empty_history_returns_empty(self):
        """Thin / empty history is never a signal — every marker's
        sample-size guard must short-circuit."""
        result = derive_history_markers(
            total_detections=0,
            bucket_start=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            bucket_minutes=15,
            history=_empty_history(),
        )
        assert result == []

    def test_returns_list_type(self):
        """Contract: returns a list (never None) so callers can iterate
        unconditionally."""
        result = derive_history_markers(
            total_detections=50,
            bucket_start=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            bucket_minutes=15,
            history=_active_history(),
        )
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# P12b — drop marker
# ---------------------------------------------------------------------------


class TestDeriveDrop:
    """drop fires when an active-baseline camera collapses to near-zero
    detections during daytime hours."""

    _MIDDAY = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)

    def _drop_of(self, markers: list[dict]) -> dict | None:
        matches = [m for m in markers if m["event_type"] == MARKER_DROP]
        return matches[0] if matches else None

    def test_fires_on_sharp_drop(self):
        # mean=100, std=30 → threshold = 100 - 2*30 = 40; current=1 < 40
        markers = derive_history_markers(
            total_detections=1,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=96),
        )
        drop = self._drop_of(markers)
        assert drop is not None
        assert drop["event_type"] == MARKER_DROP
        assert drop["label"] == "activity_drop"
        assert drop["ts"] == self._MIDDAY.isoformat()

    def test_confidence_in_unit_interval(self):
        # Severe drop: current=0, mean=100, std=30 → raw = 100/31 ≈ 3.2,
        # clamped to 1.0. The formula saturates on large drops — acceptable;
        # severity ordering between hard drops isn't meaningful.
        markers = derive_history_markers(
            total_detections=0,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=96),
        )
        drop = self._drop_of(markers)
        assert drop is not None
        assert 0.0 <= drop["confidence"] <= 1.0

    def test_confidence_reflects_sub_saturation_gap(self):
        # Keep the gap small enough that clamping isn't hit: with mean=50
        # and std=50, raw ≈ (50 - current) / 51; at current=20 raw ≈ 0.59.
        history = _active_history(mean=50.0, std=50.0, n=96)
        # threshold = max(1, 50 - 100) = 1 — drop fires for 0 only.
        # Pick parameters where the formula is in its linear regime by
        # hand-crafting: mean=30, std=50, threshold=max(1, -70)=1.
        history = _active_history(mean=30.0, std=50.0, n=96)
        markers = derive_history_markers(
            total_detections=0,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=history,
        )
        drop = self._drop_of(markers)
        assert drop is not None
        # raw = (30 - 0) / (50 + 1) ≈ 0.588 — in the linear regime.
        assert drop["confidence"] == pytest.approx(30.0 / 51.0, abs=1e-3)

    def test_suppressed_when_current_above_threshold(self):
        # mean=100, std=30, threshold=40; current=60 ≥ 40 → no fire.
        markers = derive_history_markers(
            total_detections=60,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=96),
        )
        assert self._drop_of(markers) is None

    def test_suppressed_thin_history(self):
        # < _DROP_MIN_ROLLING_SAMPLE (16) buckets of history — not enough
        # to reason about a drop.
        markers = derive_history_markers(
            total_detections=1,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=10),
        )
        assert self._drop_of(markers) is None

    def test_suppressed_always_dead_camera(self):
        # Rolling mean below the noise-floor — this camera never has
        # much activity; a bucket with 0 detections isn't a "drop".
        markers = derive_history_markers(
            total_detections=0,
            bucket_start=self._MIDDAY,
            bucket_minutes=15,
            history=_active_history(mean=2.0, std=1.0, n=96),
        )
        assert self._drop_of(markers) is None

    def test_suppressed_after_hours(self):
        # At night the after_hours marker is the meaningful signal;
        # drop is daytime-only so the two never overlap on the same bucket.
        night = datetime(2026, 4, 20, 23, 0, tzinfo=UTC)
        markers = derive_history_markers(
            total_detections=1,
            bucket_start=night,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=96),
        )
        assert self._drop_of(markers) is None

    def test_suppressed_pre_dawn(self):
        # Same daytime-gate applies to early morning.
        dawn = datetime(2026, 4, 20, 5, 0, tzinfo=UTC)
        markers = derive_history_markers(
            total_detections=1,
            bucket_start=dawn,
            bucket_minutes=15,
            history=_active_history(mean=100.0, std=30.0, n=96),
        )
        assert self._drop_of(markers) is None

    def test_floor_threshold_never_below_one(self):
        # When mean - 2σ ≤ 0, threshold floors at 1: drop still fires for
        # zero detections (but not for 1+, which meets the floor).
        history = _active_history(mean=5.0, std=3.0, n=96)
        # mean - 2σ = -1, floored to 1
        assert self._drop_of(derive_history_markers(
            total_detections=0, bucket_start=self._MIDDAY,
            bucket_minutes=15, history=history,
        )) is not None
        assert self._drop_of(derive_history_markers(
            total_detections=1, bucket_start=self._MIDDAY,
            bucket_minutes=15, history=history,
        )) is None


# ---------------------------------------------------------------------------
# P12c — start marker
# ---------------------------------------------------------------------------


class TestDeriveStart:
    """start fires on the first active bucket of the UTC day after a
    sustained-quiet tail — the "work has begun today" signal."""

    _MORNING = datetime(2026, 4, 20, 7, 0, tzinfo=UTC)

    def _start_of(self, markers: list[dict]) -> dict | None:
        matches = [m for m in markers if m["event_type"] == MARKER_START]
        return matches[0] if matches else None

    def test_fires_after_sustained_quiet(self):
        # 2h quiet (120 min) + no prior active bucket today + 10 detections.
        markers = derive_history_markers(
            total_detections=10,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=120, first_today=None),
        )
        start = self._start_of(markers)
        assert start is not None
        assert start["event_type"] == MARKER_START
        assert start["label"] == "start of activity"
        assert start["confidence"] == pytest.approx(0.9)
        assert start["ts"] == self._MORNING.isoformat()

    def test_suppressed_when_quiet_run_too_short(self):
        # 90 min quiet < 120 floor — natural lulls shouldn't trigger start.
        markers = derive_history_markers(
            total_detections=10,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=90, first_today=None),
        )
        assert self._start_of(markers) is None

    def test_suppressed_when_already_active_today(self):
        # If this camera already had an active bucket earlier today, the
        # first-of-day gate blocks a second start marker.
        earlier = datetime(2026, 4, 20, 5, 0, tzinfo=UTC)
        markers = derive_history_markers(
            total_detections=10,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=120, first_today=earlier),
        )
        assert self._start_of(markers) is None

    def test_suppressed_when_detections_below_noise_floor(self):
        # 4 detections → likely tree shadow / wildlife. Skip.
        markers = derive_history_markers(
            total_detections=4,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=120, first_today=None),
        )
        assert self._start_of(markers) is None

    def test_exactly_at_quiet_floor_fires(self):
        # Boundary: exactly 120 min quiet must fire.
        markers = derive_history_markers(
            total_detections=5,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=120, first_today=None),
        )
        assert self._start_of(markers) is not None

    def test_start_and_drop_do_not_co_fire(self):
        # A bucket that qualifies for start (lots of detections after a
        # quiet tail) cannot also be a drop — drop requires current <<
        # mean, but here current is the FIRST active bucket so current
        # meets or exceeds the baseline floor. Guards itself.
        markers = derive_history_markers(
            total_detections=50,
            bucket_start=self._MORNING,
            bucket_minutes=15,
            history=_active_history(quiet_minutes=120, first_today=None, mean=5.0, std=2.0),
        )
        drop = [m for m in markers if m["event_type"] == MARKER_DROP]
        start = [m for m in markers if m["event_type"] == MARKER_START]
        assert start and not drop


# ---------------------------------------------------------------------------
# Fragment-based markers — spike + after_hours. Pre-M12 behaviour preserved.
# ---------------------------------------------------------------------------


class _Fragment:
    """Minimal duck-typed stand-in for TrailerBucketData.

    derive_markers touches only a small subset of fields; a dataclass-ish
    object is lighter than constructing the full pydantic model in tests.
    """

    def __init__(
        self,
        *,
        total_detections: int = 0,
        anomaly_flag: int = 0,
        anomaly_score: float | None = None,
        max_count_at: datetime | None = None,
    ) -> None:
        self.total_detections = total_detections
        self.anomaly_flag = anomaly_flag
        self.anomaly_score = anomaly_score
        self.max_count_at = max_count_at


class TestDeriveMarkersSpike:
    def test_spike_fires_when_flag_and_score_above_threshold(self):
        bucket_start = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        peak = bucket_start + timedelta(minutes=3)
        frag = _Fragment(
            total_detections=150,
            anomaly_flag=1,
            anomaly_score=0.85,
            max_count_at=peak,
        )
        markers = derive_markers([frag], bucket_start)
        assert len(markers) == 1
        assert markers[0]["event_type"] == MARKER_SPIKE
        assert markers[0]["ts"] == peak.isoformat()
        assert markers[0]["confidence"] == pytest.approx(0.85)

    def test_spike_suppressed_below_threshold(self):
        frag = _Fragment(anomaly_flag=1, anomaly_score=0.5)
        assert derive_markers([frag], datetime(2026, 4, 20, 12, 0, tzinfo=UTC)) == []

    def test_spike_suppressed_when_flag_not_set(self):
        frag = _Fragment(anomaly_flag=0, anomaly_score=0.99)
        assert derive_markers([frag], datetime(2026, 4, 20, 12, 0, tzinfo=UTC)) == []

    def test_spike_ts_falls_back_to_bucket_start_when_no_peak(self):
        bucket_start = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        frag = _Fragment(anomaly_flag=1, anomaly_score=0.8, max_count_at=None)
        markers = derive_markers([frag], bucket_start)
        assert markers[0]["ts"] == bucket_start.isoformat()


class TestDeriveMarkersAfterHours:
    def test_fires_late_evening_with_detections(self):
        bucket_start = datetime(2026, 4, 20, 22, 0, tzinfo=UTC)
        frag = _Fragment(total_detections=5)
        markers = derive_markers([frag], bucket_start)
        assert any(m["event_type"] == MARKER_AFTER_HOURS for m in markers)

    def test_fires_pre_dawn_with_detections(self):
        bucket_start = datetime(2026, 4, 20, 3, 0, tzinfo=UTC)
        frag = _Fragment(total_detections=5)
        markers = derive_markers([frag], bucket_start)
        assert any(m["event_type"] == MARKER_AFTER_HOURS for m in markers)

    def test_daytime_does_not_fire(self):
        bucket_start = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        frag = _Fragment(total_detections=5)
        markers = derive_markers([frag], bucket_start)
        assert not any(m["event_type"] == MARKER_AFTER_HOURS for m in markers)

    def test_after_hours_suppressed_when_zero_detections(self):
        bucket_start = datetime(2026, 4, 20, 3, 0, tzinfo=UTC)
        frag = _Fragment(total_detections=0)
        markers = derive_markers([frag], bucket_start)
        assert not any(m["event_type"] == MARKER_AFTER_HOURS for m in markers)
