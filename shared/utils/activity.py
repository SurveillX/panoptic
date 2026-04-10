"""
Activity score computation — single authoritative implementation.

Formula (design_spec §6, build_spec §8):

    raw = 0.5 * N + 0.2 * U + 0.3 * V

where N, U, V are bounded z-scores of:
    N = object_count_total      (w1 = 0.5)
    U = unique_object_classes   (w2 = 0.2)
    V = temporal_variance       (w3 = 0.3)

Bounded z-score:
    z = (value - mean) / max(std, 1e-9)
    bounded = clamp((z + 3) / 6, 0.0, 1.0)

Mapping: mean → 0.5, mean+3σ → 1.0, mean-3σ → 0.0.
Values beyond ±3σ are saturated.

Empty-scene rule:
    If object_count_total == 0 AND stream_coverage_ok is True,
    return exactly 0.0.  The bucket is valid; no computation needed.

Do NOT import this from anywhere other than shared.utils.activity.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ActivityComponents:
    """Raw inputs extracted from a single detection bucket."""

    object_count_total: int
    unique_object_classes: int
    # Variance of per-interval object counts within the bucket window.
    # A bucket with uniform low activity has low variance; a bucket with
    # a sudden spike has high variance.
    temporal_variance: float


@dataclass
class CameraStats:
    """
    Rolling per-camera baseline statistics.

    These are maintained externally (e.g. in Postgres or a cache) and passed
    in at call time.  Zero std is safe: _bounded_zscore guards against it.
    """

    mean_object_count: float
    std_object_count: float
    mean_unique_classes: float
    std_unique_classes: float
    mean_temporal_variance: float
    std_temporal_variance: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _bounded_zscore(value: float, mean: float, std: float) -> float:
    """
    Convert a raw value to a [0, 1] bounded z-score.

    z = (value - mean) / max(std, 1e-9)
    result = clamp((z + 3) / 6, 0.0, 1.0)

    The ±3σ window is mapped linearly to [0, 1]:
      mean - 3σ  →  0.0
      mean       →  0.5
      mean + 3σ  →  1.0
    Outliers beyond ±3σ are saturated to [0, 1].
    """
    z = (value - mean) / max(std, 1e-9)
    return _clamp((z + 3.0) / 6.0, 0.0, 1.0)


def compute_activity_score(
    components: ActivityComponents,
    camera_stats: CameraStats,
    stream_coverage_ok: bool = True,
) -> float:
    """
    Compute the activity score for a single bucket.

    Returns a float in [0.0, 1.0].

    Empty-scene rule: if object_count_total == 0 and stream_coverage_ok,
    returns exactly 0.0 without invoking the normalization formula.
    This distinguishes "nothing happened" from "data missing".
    """
    if components.object_count_total == 0 and stream_coverage_ok:
        return 0.0

    n = _bounded_zscore(
        components.object_count_total,
        camera_stats.mean_object_count,
        camera_stats.std_object_count,
    )
    u = _bounded_zscore(
        components.unique_object_classes,
        camera_stats.mean_unique_classes,
        camera_stats.std_unique_classes,
    )
    v = _bounded_zscore(
        components.temporal_variance,
        camera_stats.mean_temporal_variance,
        camera_stats.std_temporal_variance,
    )

    raw = 0.5 * n + 0.2 * u + 0.3 * v
    return _clamp(raw, 0.0, 1.0)
