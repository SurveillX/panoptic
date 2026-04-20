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
    MARKER_SPIKE,
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
# P12a — skeleton: derive_history_markers always returns [] until P12b lands.
# ---------------------------------------------------------------------------


class TestDeriveHistoryMarkersSkeleton:
    """P12a groundwork — derive_history_markers accepts the full input
    surface and returns an empty list. Per-marker tests replace these
    cases as each phase lands."""

    def test_empty_history_returns_empty(self):
        result = derive_history_markers(
            total_detections=0,
            bucket_start=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            bucket_minutes=15,
            history=_empty_history(),
        )
        assert result == []

    def test_active_history_returns_empty(self):
        # Even with plausible history + a dramatic drop, no marker is
        # emitted because P12a has no per-marker logic yet.
        result = derive_history_markers(
            total_detections=1,
            bucket_start=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            bucket_minutes=15,
            history=_active_history(),
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
