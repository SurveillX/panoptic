"""
replay_buckets.py — Synthetic bucket replay for pipeline testing.

Generates BucketRecords for various scenarios and feeds them through
ingest_bucket() to exercise: bucket → summary → embedding → rollup.

Usage:
  python scripts/replay_buckets.py --scenario steady --hours 2 \
      --tenant-id demo --camera-id cam-01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

from shared.clients.cognia import ingest_bucket
from shared.schemas.bucket import generate_bucket_id
from shared.utils.redis_client import get_redis_client

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/vil")
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_for_hash(obj):
    """Normalize floats to fixed precision before hashing."""
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round_for_hash(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_for_hash(v) for v in obj]
    return obj


def _build_bucket(
    serial_number: str,
    camera_id: str,
    start_utc: datetime,
    end_utc: datetime,
    object_counts: dict[str, int],
    cognia_stats: dict,
    event_markers: list[dict],
    completeness: dict,
    keyframe_candidates: dict | None = None,
) -> dict:
    """Build a raw bucket payload matching BucketRecord schema."""
    # activity_components with explicit c1/c2/c3
    activity_components = {
        "object_count_total":    cognia_stats["total_detections"],
        "unique_object_classes": len(object_counts),
        "temporal_variance":     cognia_stats["std_dev_count"],
        "c1": round(cognia_stats["mean_count"] / 30.0, 4),
        "c2": round(cognia_stats["duty_cycle"], 4),
        "c3": round(cognia_stats["std_dev_count"] / 15.0, 4),
    }

    # detection_hash: normalize floats before hashing
    hash_payload = _round_for_hash({**cognia_stats, "object_counts": object_counts})
    detection_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    bucket_id = generate_bucket_id(
        serial_number=serial_number,
        camera_id=camera_id,
        start_utc=start_utc,
        end_utc=end_utc,
        detection_hash=detection_hash,
        schema_version=SCHEMA_VERSION,
    )

    # activity_score placeholder — ingest_bucket recomputes it
    activity_score = round(0.5 * activity_components["c1"]
                          + 0.2 * activity_components["c2"]
                          + 0.3 * activity_components["c3"], 4)
    activity_score = max(0.0, min(1.0, activity_score))

    kf = keyframe_candidates or {"baseline_ts": None, "peak_ts": None, "change_ts": None}

    return {
        "bucket_id": bucket_id,
        "serial_number": serial_number,
        "camera_id": camera_id,
        "bucket_start_utc": start_utc.isoformat(),
        "bucket_end_utc": end_utc.isoformat(),
        "bucket_status": "complete",
        "schema_version": SCHEMA_VERSION,
        "detection_hash": detection_hash,
        "activity_score": activity_score,
        "activity_components": activity_components,
        "object_counts": object_counts,
        "keyframe_candidates": kf,
        "event_markers": event_markers,
        "completeness": completeness,
    }


def _default_completeness(coverage: float = 1.0) -> dict:
    return {
        "detection_coverage": coverage,
        "stream_interrupted_seconds": 0,
        "aggregator_restart_seen": False,
    }


def _cognia_stats(
    mean_count: float,
    std_dev_count: float,
    duty_cycle: float,
    total_detections: int,
    unique_tracker_ids: int | None = None,
    anomaly_score: float = 0.0,
    anomaly_flag: int = 0,
) -> dict:
    return {
        "mode_count": max(0, int(mean_count)),
        "mean_count": mean_count,
        "std_dev_count": std_dev_count,
        "duty_cycle": duty_cycle,
        "unique_tracker_ids": unique_tracker_ids or max(1, total_detections // 3),
        "total_detections": total_detections,
        "anomaly_score": anomaly_score,
        "anomaly_flag": anomaly_flag,
    }


# ---------------------------------------------------------------------------
# Scenario generators
# ---------------------------------------------------------------------------

def scenario_steady(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """Stable activity: moderate mean_count, low variance, no anomaly."""
    buckets = []
    for h in range(hours):
        hour_start = base_utc + timedelta(hours=h)
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            mc = round(random.uniform(3.0, 8.0), 2)
            sd = round(random.uniform(1.0, 2.0), 2)
            dc = round(random.uniform(0.7, 0.9), 2)
            td = int(mc * 15)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts={"person": random.randint(3, 8), "vehicle": random.randint(0, 2)},
                cognia_stats=_cognia_stats(mc, sd, dc, td),
                event_markers=[],
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_idle(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """Idle site: near-zero detections, low duty cycle."""
    buckets = []
    for h in range(hours):
        hour_start = base_utc + timedelta(hours=h)
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            mc = round(random.uniform(0.0, 0.5), 2)
            sd = round(random.uniform(0.0, 0.1), 2)
            dc = round(random.uniform(0.0, 0.1), 2)
            td = random.randint(0, 2)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts={},
                cognia_stats=_cognia_stats(mc, sd, dc, td),
                event_markers=[],
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_spike(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """Normal activity with one spike + one drop bucket per hour."""
    buckets = []
    for h in range(hours):
        hour_start = base_utc + timedelta(hours=h)
        # Spike at a random quarter; drop always follows spike (wraps around)
        # so there is a clear high → zero transition.
        spike_quarter = random.randint(0, 3)
        drop_quarter = (spike_quarter + 1) % 4
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            if q == spike_quarter:
                mc = round(random.uniform(15.0, 30.0), 2)
                sd = round(random.uniform(8.0, 15.0), 2)
                dc = round(random.uniform(0.9, 1.0), 2)
                td = int(mc * 15)
                oc = {"person": random.randint(15, 30), "vehicle": random.randint(3, 8)}
                markers = [{
                    "ts": (start + timedelta(minutes=7)).isoformat(),
                    "event_type": "spike",
                    "label": "activity_spike",
                    "confidence": 0.9,
                }]
                stats = _cognia_stats(mc, sd, dc, td, anomaly_score=0.75, anomaly_flag=1)
            elif q == drop_quarter:
                mc = 0.0
                sd = 0.0
                dc = 0.0
                td = 0
                oc = {"person": 0, "vehicle": 0}
                markers = [{
                    "ts": (start + timedelta(minutes=7)).isoformat(),
                    "event_type": "drop",
                    "label": "activity_drop",
                    "confidence": 0.9,
                }]
                stats = _cognia_stats(mc, sd, dc, td, anomaly_score=0.5, anomaly_flag=1)
            else:
                mc = round(random.uniform(3.0, 8.0), 2)
                sd = round(random.uniform(1.0, 2.0), 2)
                dc = round(random.uniform(0.7, 0.9), 2)
                td = int(mc * 15)
                oc = {"person": random.randint(3, 8), "vehicle": random.randint(0, 2)}
                markers = []
                stats = _cognia_stats(mc, sd, dc, td)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts=oc,
                cognia_stats=stats,
                event_markers=markers,
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_anomaly(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """Moderate counts but persistent anomaly flag — unusual pattern detected."""
    buckets = []
    for h in range(hours):
        hour_start = base_utc + timedelta(hours=h)
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            mc = round(random.uniform(5.0, 10.0), 2)
            sd = round(random.uniform(3.0, 6.0), 2)
            dc = round(random.uniform(0.5, 0.8), 2)
            td = int(mc * 15)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts={"person": random.randint(5, 10)},
                cognia_stats=_cognia_stats(
                    mc, sd, dc, td,
                    anomaly_score=round(random.uniform(0.8, 1.0), 2),
                    anomaly_flag=1,
                ),
                event_markers=[],
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_after_hours(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """Activity during night hours (22:00–05:00) with after_hours markers."""
    buckets = []
    # Start at 22:00 UTC to simulate night activity
    night_start = base_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    for h in range(hours):
        hour_start = night_start + timedelta(hours=h)
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            mc = round(random.uniform(2.0, 6.0), 2)
            sd = round(random.uniform(1.0, 3.0), 2)
            dc = round(random.uniform(0.5, 0.8), 2)
            td = int(mc * 15)
            oc = {"person": random.randint(1, 4), "vehicle": random.randint(0, 1)}
            markers = [{
                "ts": (start + timedelta(minutes=7)).isoformat(),
                "event_type": "after_hours",
                "label": "after hours activity",
                "confidence": 0.9,
            }]
            stats = _cognia_stats(mc, sd, dc, td)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts=oc,
                cognia_stats=stats,
                event_markers=markers,
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_after_hours_drop(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """After-hours spike followed by immediate drop — both signals on drop bucket."""
    buckets = []
    night_start = base_utc.replace(hour=23, minute=0, second=0, microsecond=0)
    for h in range(hours):
        hour_start = night_start + timedelta(hours=h)
        spike_quarter = random.randint(0, 2)  # 0-2 so drop can follow
        drop_quarter = spike_quarter + 1
        for q in range(4):
            start = hour_start + timedelta(minutes=q * 15)
            end = start + timedelta(minutes=15)
            ts_mid = (start + timedelta(minutes=7)).isoformat()
            if q == spike_quarter:
                # High activity spike at night
                mc = round(random.uniform(15.0, 25.0), 2)
                sd = round(random.uniform(6.0, 12.0), 2)
                dc = round(random.uniform(0.9, 1.0), 2)
                td = int(mc * 15)
                oc = {"person": random.randint(10, 20), "vehicle": random.randint(2, 5)}
                markers = [
                    {"ts": ts_mid, "event_type": "after_hours", "label": "after hours activity", "confidence": 0.9},
                    {"ts": ts_mid, "event_type": "spike", "label": "activity_spike", "confidence": 0.9},
                ]
                stats = _cognia_stats(mc, sd, dc, td, anomaly_score=0.8, anomaly_flag=1)
            elif q == drop_quarter:
                # Immediate drop to zero after the spike
                mc = 0.0
                sd = 0.0
                dc = 0.0
                td = 0
                oc = {"person": 0, "vehicle": 0}
                markers = [
                    {"ts": ts_mid, "event_type": "after_hours", "label": "after hours activity", "confidence": 0.9},
                    {"ts": ts_mid, "event_type": "drop", "label": "activity_drop", "confidence": 0.9},
                ]
                stats = _cognia_stats(mc, sd, dc, td, anomaly_score=0.6, anomaly_flag=1)
            else:
                # Quiet night baseline
                mc = round(random.uniform(0.5, 2.0), 2)
                sd = round(random.uniform(0.0, 1.0), 2)
                dc = round(random.uniform(0.2, 0.4), 2)
                td = int(mc * 15)
                oc = {"person": random.randint(0, 1), "vehicle": 0}
                markers = [
                    {"ts": ts_mid, "event_type": "after_hours", "label": "after hours activity", "confidence": 0.9},
                ]
                stats = _cognia_stats(mc, sd, dc, td)
            buckets.append(_build_bucket(
                serial_number, camera_id, start, end,
                object_counts=oc,
                cognia_stats=stats,
                event_markers=markers,
                completeness=_default_completeness(),
            ))
    return buckets


def scenario_workday(
    serial_number: str, camera_id: str,
    base_utc: datetime, hours: int,
) -> list[dict]:
    """
    Realistic 24-hour construction site pattern.

    Generates full 24-hour days. The 'hours' parameter controls the number
    of days (hours // 24, minimum 1).

    Pattern:
      00–05  Night idle — near-zero, after_hours marker on any detection
      05–06  Pre-dawn — occasional security patrol, after_hours
      06–07  Morning arrival — spike (crew + vehicles arrive)
      07–12  Morning work — steady moderate-high activity
      12–13  Lunch — brief drop then recovery
      13–17  Afternoon work — steady moderate activity, tapering
      17–18  Evening departure — drop (crew leaves)
      18–21  Evening wind-down — low activity
      21–24  Night idle — near-zero, after_hours on any detection
    """
    buckets = []
    num_days = max(1, hours // 24)

    prev_idle = [True]  # mutable container for closure; start of day is idle
    first_start_detected = [False]  # tracks if first start of day already occurred
    expected_start_hour = 6  # 06:00 UTC
    late_start_threshold_min = 30

    def _bucket(start, end, oc, mc, sd, dc, markers):
        td = int(mc * 15)
        total_objects = sum(oc.values())
        is_active = total_objects >= 2 and mc >= 2.0

        # Detect idle → active transition
        if is_active and prev_idle[0]:
            ts_mid = (start + timedelta(minutes=7)).isoformat()
            if not any(m["event_type"] == "start" for m in markers):
                markers.append({"ts": ts_mid, "event_type": "start",
                                "label": "start of activity", "confidence": 0.9})
            # Detect late start: only on first start of the day
            if not first_start_detected[0]:
                expected_start = start.replace(hour=expected_start_hour, minute=0, second=0, microsecond=0)
                if start > expected_start + timedelta(minutes=late_start_threshold_min):
                    if not any(m["event_type"] == "late_start" for m in markers):
                        markers.append({"ts": ts_mid, "event_type": "late_start",
                                        "label": "late start", "confidence": 0.9})
                first_start_detected[0] = True

        prev_idle[0] = not is_active

        anomaly_score = 0.0
        anomaly_flag = 0
        if any(m["event_type"] in ("spike", "drop", "start", "late_start") for m in markers):
            anomaly_score = 0.7
            anomaly_flag = 1
        stats = _cognia_stats(mc, sd, dc, td,
                              anomaly_score=anomaly_score, anomaly_flag=anomaly_flag)
        buckets.append(_build_bucket(
            serial_number, camera_id, start, end,
            object_counts=oc, cognia_stats=stats,
            event_markers=markers, completeness=_default_completeness(),
        ))

    for day in range(num_days):
        day_start = base_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day)
        prev_idle[0] = True
        first_start_detected[0] = False
        day_bucket_start = len(buckets)
        for h in range(24):
            hour_start = day_start + timedelta(hours=h)
            for q in range(4):
                start = hour_start + timedelta(minutes=q * 15)
                end = start + timedelta(minutes=15)
                ts_mid = (start + timedelta(minutes=7)).isoformat()

                # Late start day: odd-numbered days stay idle through 06–08
                is_late_day = day % 2 == 1

                # --- Night idle (00–05, 21–24) ---
                if h < 5 or h >= 21:
                    persons = random.choices([0, 0, 0, 0, 1], k=1)[0]
                    oc = {"person": persons, "vehicle": 0}
                    mc = round(random.uniform(0.0, 0.3), 2)
                    sd = round(random.uniform(0.0, 0.1), 2)
                    dc = round(random.uniform(0.0, 0.05), 2)
                    markers = []
                    if persons > 0:
                        markers.append({"ts": ts_mid, "event_type": "after_hours",
                                        "label": "after hours activity", "confidence": 0.9})
                    _bucket(start, end, oc, mc, sd, dc, markers)

                # --- Pre-dawn patrol (05–06) ---
                elif h == 5:
                    persons = random.choice([0, 0, 1, 1, 2])
                    oc = {"person": persons, "vehicle": random.choice([0, 0, 1])}
                    mc = round(random.uniform(0.5, 2.0), 2)
                    sd = round(random.uniform(0.0, 0.5), 2)
                    dc = round(random.uniform(0.1, 0.3), 2)
                    markers = []
                    if persons > 0:
                        markers.append({"ts": ts_mid, "event_type": "after_hours",
                                        "label": "after hours activity", "confidence": 0.9})
                    _bucket(start, end, oc, mc, sd, dc, markers)

                # --- Morning arrival (06–07) — idle on late days ---
                elif h == 6 and is_late_day:
                    oc = {"person": 0, "vehicle": 0}
                    mc = 0.0
                    sd = 0.0
                    dc = 0.0
                    _bucket(start, end, oc, mc, sd, dc, [])

                # --- Morning arrival (06–07) — normal days ---
                elif h == 6:
                    if q == 0:
                        oc = {"person": random.randint(2, 4), "vehicle": random.randint(1, 2)}
                        mc = round(random.uniform(3.0, 6.0), 2)
                        sd = round(random.uniform(2.0, 4.0), 2)
                        dc = round(random.uniform(0.5, 0.7), 2)
                        _bucket(start, end, oc, mc, sd, dc, [])
                    elif q == 1:
                        oc = {"person": random.randint(12, 25), "vehicle": random.randint(3, 7)}
                        mc = round(random.uniform(15.0, 25.0), 2)
                        sd = round(random.uniform(6.0, 12.0), 2)
                        dc = round(random.uniform(0.9, 1.0), 2)
                        markers = [{"ts": ts_mid, "event_type": "spike",
                                    "label": "activity_spike", "confidence": 0.9}]
                        _bucket(start, end, oc, mc, sd, dc, markers)
                    else:
                        oc = {"person": random.randint(8, 15), "vehicle": random.randint(2, 5)}
                        mc = round(random.uniform(8.0, 15.0), 2)
                        sd = round(random.uniform(2.0, 4.0), 2)
                        dc = round(random.uniform(0.7, 0.9), 2)
                        _bucket(start, end, oc, mc, sd, dc, [])

                # --- Late day: idle through 07, arrival at 08 ---
                elif h == 7 and is_late_day:
                    oc = {"person": 0, "vehicle": 0}
                    mc = 0.0
                    sd = 0.0
                    dc = 0.0
                    _bucket(start, end, oc, mc, sd, dc, [])

                # --- Morning work (07–12) ---
                elif 7 <= h < 12:
                    oc = {"person": random.randint(8, 18), "vehicle": random.randint(2, 5)}
                    mc = round(random.uniform(8.0, 18.0), 2)
                    sd = round(random.uniform(2.0, 5.0), 2)
                    dc = round(random.uniform(0.7, 0.95), 2)
                    _bucket(start, end, oc, mc, sd, dc, [])

                # --- Lunch break (12–13) ---
                elif h == 12:
                    if q <= 1:
                        oc = {"person": random.randint(1, 3), "vehicle": random.randint(0, 1)}
                        mc = round(random.uniform(1.0, 3.0), 2)
                        sd = round(random.uniform(0.5, 1.5), 2)
                        dc = round(random.uniform(0.2, 0.4), 2)
                        markers = [{"ts": ts_mid, "event_type": "drop",
                                    "label": "activity_drop", "confidence": 0.85}] if q == 0 else []
                        _bucket(start, end, oc, mc, sd, dc, markers)
                    else:
                        oc = {"person": random.randint(6, 12), "vehicle": random.randint(1, 3)}
                        mc = round(random.uniform(6.0, 12.0), 2)
                        sd = round(random.uniform(2.0, 4.0), 2)
                        dc = round(random.uniform(0.6, 0.8), 2)
                        _bucket(start, end, oc, mc, sd, dc, [])

                # --- Afternoon work (13–17) ---
                elif 13 <= h < 17:
                    base_persons = max(4, 15 - (h - 13) * 3)
                    oc = {"person": random.randint(base_persons - 2, base_persons + 3),
                          "vehicle": random.randint(1, 4)}
                    mc = round(random.uniform(float(base_persons - 2), float(base_persons + 3)), 2)
                    sd = round(random.uniform(1.5, 4.0), 2)
                    dc = round(random.uniform(0.6, 0.85), 2)
                    _bucket(start, end, oc, mc, sd, dc, [])

                # --- Evening departure (17–18) ---
                elif h == 17:
                    if q <= 1:
                        oc = {"person": random.randint(5, 10), "vehicle": random.randint(2, 5)}
                        mc = round(random.uniform(5.0, 10.0), 2)
                        sd = round(random.uniform(3.0, 6.0), 2)
                        dc = round(random.uniform(0.5, 0.7), 2)
                        _bucket(start, end, oc, mc, sd, dc, [])
                    elif q == 2:
                        oc = {"person": random.randint(0, 2), "vehicle": random.randint(0, 1)}
                        mc = round(random.uniform(0.5, 2.0), 2)
                        sd = round(random.uniform(0.5, 2.0), 2)
                        dc = round(random.uniform(0.1, 0.3), 2)
                        markers = [{"ts": ts_mid, "event_type": "drop",
                                    "label": "activity_drop", "confidence": 0.9}]
                        _bucket(start, end, oc, mc, sd, dc, markers)
                    else:
                        oc = {"person": 0, "vehicle": 0}
                        mc = 0.0
                        sd = 0.0
                        dc = 0.0
                        _bucket(start, end, oc, mc, sd, dc, [])

                # --- Evening wind-down (18–21) ---
                elif 18 <= h < 21:
                    persons = random.choice([0, 0, 0, 1, 1])
                    oc = {"person": persons, "vehicle": 0}
                    mc = round(random.uniform(0.0, 1.0), 2)
                    sd = round(random.uniform(0.0, 0.5), 2)
                    dc = round(random.uniform(0.0, 0.15), 2)
                    _bucket(start, end, oc, mc, sd, dc, [])

        # --- Day-level insights: site underperforming ---
        day_buckets = buckets[day_bucket_start:]
        day_markers = [m for b in day_buckets for m in b.get("event_markers", [])]
        has_late_start = any(m["event_type"] == "late_start" for m in day_markers)
        has_spike = any(m["event_type"] == "spike" for m in day_markers)
        if has_late_start and not has_spike:
            # Add underperforming marker to the late_start bucket
            for b in day_buckets:
                if any(m["event_type"] == "late_start" for m in b["event_markers"]):
                    b["event_markers"].append({
                        "ts": b["bucket_start_utc"],
                        "event_type": "underperforming",
                        "label": "site underperforming",
                        "confidence": 0.9,
                    })
                    break

    return buckets


SCENARIOS = {
    "steady":            scenario_steady,
    "idle":              scenario_idle,
    "spike":             scenario_spike,
    "anomaly":           scenario_anomaly,
    "after_hours":       scenario_after_hours,
    "after_hours_drop":  scenario_after_hours_drop,
    "workday":           scenario_workday,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Replay synthetic buckets")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS.keys())
    parser.add_argument("--hours", type=int, default=2)
    parser.add_argument("--serial-number", default="1422725077375")
    parser.add_argument("--camera-id", default="cam-01")
    parser.add_argument("--model-profile", default="default")
    parser.add_argument("--prompt-version", default="v1")
    parser.add_argument("--base-time", default=None,
                        help="ISO UTC start time (default: now truncated to hour)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.base_time:
        base_utc = datetime.fromisoformat(args.base_time)
        if base_utc.tzinfo is None:
            base_utc = base_utc.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        base_utc = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=args.hours)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()

    gen = SCENARIOS[args.scenario]
    buckets = gen(
        args.serial_number, args.camera_id,
        base_utc, args.hours,
    )

    log.info(
        "replaying %d buckets: scenario=%s hours=%d tenant=%s camera=%s base=%s",
        len(buckets), args.scenario, args.hours, args.serial_number,
        args.camera_id, base_utc.isoformat(),
    )

    for i, raw in enumerate(buckets):
        try:
            result = ingest_bucket(
                engine, r, raw,
                model_profile=args.model_profile,
                prompt_version=args.prompt_version,
            )
            log.info(
                "bucket %d/%d: bucket_id=%s action=%s job=%s",
                i + 1, len(buckets),
                raw["bucket_id"][:12],
                result.bucket_action,
                "dup_job" if result.was_duplicate_job else "new_job",
            )
        except Exception as exc:
            log.error("bucket %d/%d failed: %s", i + 1, len(buckets), exc)

    log.info("replay complete: %d buckets", len(buckets))


if __name__ == "__main__":
    main()
