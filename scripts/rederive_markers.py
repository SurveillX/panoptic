"""
scripts/rederive_markers.py — historical marker re-derivation and
dry-run evaluation for M12.

Three modes (default: Mode-1 dry-run):

    Mode 1 (default, no writes):
        python -m scripts.rederive_markers --serial <SN> --days 14

      Reconstructs BucketHistory for every bucket in the window and
      calls derive_history_markers() in evaluation mode (produces all
      IMPLEMENTED_HISTORY_MARKERS including underperforming). Writes
      a diff JSONL + human summary to logs/m12-rederive/<ts>/.

    Mode 2 (append new-family markers to event_markers):
        python -m scripts.rederive_markers --serial <SN> --days 14 --apply-new

      Same derivation, but UPSERTs the new-family marker dicts into
      each bucket's event_markers JSONB list. Deduplicates on
      (event_type, ts) — idempotent. Never deletes or overwrites
      existing markers.

    Mode 3 (not used for M12 ship-out):
        --overwrite-all  — full overwrite. Flag exists; off by default.

Safety:
  - Every query against panoptic_buckets uses strict-past-only
    conditions (bucket_start_utc < target). No future data leaks into
    any re-derivation baseline.
  - Mode 1 writes ZERO rows to Postgres.
  - Mode 2 append only; pre-existing markers are preserved byte-for-byte.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from shared.signals.derive import (
    IMPLEMENTED_HISTORY_MARKERS,
    PRODUCED_HISTORY_MARKERS,
    derive_history_markers,
)
from shared.signals.history import fetch_bucket_history


log = logging.getLogger("rederive")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--serial",
        required=True,
        help="Serial number to rederive. Use 'ALL' for every distinct serial.",
    )
    p.add_argument("--days", type=int, default=14, help="Lookback window (default 14)")
    p.add_argument(
        "--camera",
        default=None,
        help="Optional: restrict to one camera_id for this serial.",
    )
    p.add_argument(
        "--apply-new",
        action="store_true",
        help="Mode 2: append new-family markers to each bucket's event_markers.",
    )
    p.add_argument(
        "--overwrite-all",
        action="store_true",
        help="Mode 3 (unsafe): full overwrite. Off by default; M12 never uses this.",
    )
    p.add_argument(
        "--produce",
        default=",".join(sorted(IMPLEMENTED_HISTORY_MARKERS)),
        help=(
            "Comma-separated marker keys to evaluate. "
            "Defaults to every implemented history marker (includes "
            "underperforming for Mode-1 evaluation). "
            "Pass e.g. 'drop,start,late_start' to exclude underperforming."
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory (default logs/m12-rederive/<ts>/).",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Core rederive
# ---------------------------------------------------------------------------


def _load_buckets(
    conn,
    *,
    serial_number: str | None,
    camera_id: str | None,
    window_start: datetime,
) -> list:
    """Fetch bucket rows within the window, chronologically."""

    clauses = ["bucket_start_utc >= :window_start"]
    params: dict = {"window_start": window_start}
    if serial_number is not None:
        clauses.append("serial_number = :sn")
        params["sn"] = serial_number
    if camera_id is not None:
        clauses.append("camera_id = :cam")
        params["cam"] = camera_id

    sql = f"""
        SELECT bucket_id, serial_number, camera_id,
               bucket_start_utc, bucket_end_utc,
               activity_components, event_markers
          FROM panoptic_buckets
         WHERE {' AND '.join(clauses)}
         ORDER BY bucket_start_utc ASC
    """
    return list(conn.execute(text(sql), params).fetchall())


def _existing_marker_keyset(event_markers) -> set[tuple[str, str]]:
    """(event_type, ts-as-iso) pairs already present on the bucket."""
    out: set[tuple[str, str]] = set()
    if not event_markers:
        return out
    for m in event_markers:
        et = m.get("event_type")
        ts = m.get("ts")
        if isinstance(et, str) and isinstance(ts, str):
            out.add((et, ts))
    return out


def rederive(
    engine,
    *,
    serial_number: str | None,
    camera_id: str | None,
    days: int,
    produce: frozenset[str],
    out_dir: Path,
    apply_new: bool,
    overwrite_all: bool,
) -> dict:
    """Returns summary dict of the run."""

    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = out_dir / "diff.jsonl"
    summary_path = out_dir / "summary.txt"

    now = datetime.now(timezone.utc).replace(microsecond=0)
    window_start = now - timedelta(days=days)

    bucket_count = 0
    markers_total: Counter = Counter()
    markers_new: Counter = Counter()                            # only new-family types
    per_camera: dict[str, Counter] = defaultdict(Counter)
    applied = 0
    skipped_existing = 0

    t0 = time.perf_counter()

    with engine.connect() as conn:
        buckets = _load_buckets(
            conn,
            serial_number=None if serial_number == "ALL" else serial_number,
            camera_id=camera_id,
            window_start=window_start,
        )

    log.info("loaded %d buckets (serial=%s days=%d)", len(buckets), serial_number, days)

    with open(diff_path, "w") as diff_f:
        for row in buckets:
            bucket_count += 1

            # Use a fresh connection per bucket — fetch_bucket_history
            # queries strictly earlier data so it's always safe to read
            # the "current" snapshot of panoptic_buckets.
            with engine.connect() as conn:
                history = fetch_bucket_history(
                    conn,
                    serial_number=row.serial_number,
                    camera_id=row.camera_id,
                    bucket_start=row.bucket_start_utc,
                )

            total_detections = int(
                (row.activity_components or {}).get("object_count_total", 0)
            )
            bucket_minutes = int(
                (row.bucket_end_utc - row.bucket_start_utc).total_seconds() // 60
            ) or 15

            proposed = derive_history_markers(
                total_detections=total_detections,
                bucket_start=row.bucket_start_utc,
                bucket_minutes=bucket_minutes,
                history=history,
                produce=produce,
            )

            existing_keys = _existing_marker_keyset(row.event_markers)
            genuinely_new = [
                m for m in proposed
                if (m["event_type"], m["ts"]) not in existing_keys
            ]

            for m in proposed:
                markers_total[m["event_type"]] += 1
                per_camera[f"{row.serial_number}::{row.camera_id}"][m["event_type"]] += 1
            for m in genuinely_new:
                markers_new[m["event_type"]] += 1

            diff_f.write(json.dumps({
                "bucket_id":     row.bucket_id,
                "serial":        row.serial_number,
                "camera":        row.camera_id,
                "ts":            row.bucket_start_utc.isoformat(),
                "total":         total_detections,
                "history": {
                    "rolling_n":          history.rolling_bucket_sample_size,
                    "rolling_mean":       round(history.rolling_mean_total_detections, 2),
                    "rolling_std":        round(history.rolling_std_total_detections, 2),
                    "quiet_run_min":      history.recent_quiet_run_minutes,
                    "first_today":        history.first_active_bucket_start_today.isoformat()
                                          if history.first_active_bucket_start_today else None,
                    "typical_first_hour": history.typical_first_active_hour_utc,
                    "days_with_activity": history.day_baseline_days_with_activity,
                },
                "existing":      sorted(list(existing_keys)),
                "proposed":      proposed,
                "genuinely_new": genuinely_new,
            }) + "\n")

            if apply_new and genuinely_new:
                applied += _apply_new_markers(
                    engine,
                    bucket_id=row.bucket_id,
                    existing=row.event_markers,
                    new_markers=genuinely_new,
                )
            elif not apply_new and genuinely_new:
                skipped_existing += 0  # placeholder for symmetry

            if bucket_count % 1000 == 0:
                log.info("  ... %d / %d", bucket_count, len(buckets))

    elapsed = time.perf_counter() - t0
    _write_summary(
        summary_path,
        bucket_count=bucket_count,
        produce=produce,
        markers_total=markers_total,
        markers_new=markers_new,
        per_camera=per_camera,
        elapsed=elapsed,
        apply_new=apply_new,
        applied=applied,
    )

    log.info("diff:    %s", diff_path)
    log.info("summary: %s", summary_path)
    return {
        "bucket_count":  bucket_count,
        "markers_total": dict(markers_total),
        "markers_new":   dict(markers_new),
        "applied":       applied,
        "elapsed_sec":   round(elapsed, 2),
        "diff_path":     str(diff_path),
        "summary_path":  str(summary_path),
    }


def _apply_new_markers(
    engine, *, bucket_id: str, existing: list, new_markers: list[dict]
) -> int:
    """Mode 2: append new markers to event_markers; preserve existing. Returns 1 if updated."""
    merged = list(existing or [])
    # Re-dedup defensively in case `existing` contains dicts with the
    # same (event_type, ts) as one of the proposed new ones.
    existing_keys = _existing_marker_keyset(existing)
    for m in new_markers:
        if (m["event_type"], m["ts"]) not in existing_keys:
            merged.append(m)
            existing_keys.add((m["event_type"], m["ts"]))

    if len(merged) == len(existing or []):
        return 0

    # Store datetimes as ISO strings to match the JSONB shape used at
    # ingest time (see cognia._bucket_params).
    normalized = []
    for m in merged:
        norm = dict(m)
        ts_val = norm.get("ts")
        if isinstance(ts_val, datetime):
            norm["ts"] = ts_val.isoformat()
        normalized.append(norm)

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE panoptic_buckets SET event_markers = CAST(:em AS jsonb), "
                "updated_at = now() WHERE bucket_id = :bid"
            ),
            {"em": json.dumps(normalized), "bid": bucket_id},
        )
    return 1


# ---------------------------------------------------------------------------
# Summary writer
# ---------------------------------------------------------------------------


def _write_summary(
    path: Path,
    *,
    bucket_count: int,
    produce: frozenset[str],
    markers_total: Counter,
    markers_new: Counter,
    per_camera: dict,
    elapsed: float,
    apply_new: bool,
    applied: int,
) -> None:
    lines: list[str] = []
    lines.append(f"# M12 rederive summary")
    lines.append(f"generated_at: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"buckets_scanned: {bucket_count}")
    lines.append(f"elapsed_sec: {elapsed:.2f}")
    lines.append(f"mode: {'apply-new' if apply_new else 'dry-run'}")
    lines.append(f"produce: {sorted(produce)}")
    lines.append(f"production_set: {sorted(PRODUCED_HISTORY_MARKERS)}")
    lines.append("")
    lines.append("## Proposed marker totals")
    if not markers_total:
        lines.append("  (none)")
    else:
        for k in sorted(markers_total):
            lines.append(f"  {k:<16} {markers_total[k]:>6}")
    lines.append("")
    lines.append("## Genuinely new (not already on existing event_markers)")
    if not markers_new:
        lines.append("  (none)")
    else:
        for k in sorted(markers_new):
            lines.append(f"  {k:<16} {markers_new[k]:>6}")
    lines.append("")

    # FP-gate hints per plan §5a
    drop_n = markers_total.get("drop", 0)
    under_n = markers_total.get("underperforming", 0)
    late_n = markers_total.get("late_start", 0)
    start_n = markers_total.get("start", 0)
    spike_n = _count_existing_event_type(per_camera, "spike")

    lines.append("## Gate checks (plan §5a)")
    if "underperforming" in produce and drop_n > 0:
        ratio = under_n / max(drop_n, 1)
        verdict = "OK" if ratio <= 4 else "FAIL"
        lines.append(
            f"  underperforming ≤ 4× drop:    {under_n} vs {drop_n} "
            f"(ratio={ratio:.2f}) [{verdict}]"
        )
    elif "underperforming" in produce:
        lines.append("  underperforming gate:         drop==0, ratio undefined")
    if "late_start" in produce:
        lines.append(
            f"  late_start ≤ start:           {late_n} vs {start_n} "
            f"[{'OK' if late_n <= start_n else 'FAIL'}]"
        )
    if "drop" in produce and spike_n > 0:
        ratio = drop_n / max(spike_n, 1)
        lines.append(
            f"  drop ≤ 1.5× spike (heuristic): {drop_n} vs {spike_n} "
            f"(ratio={ratio:.2f}) "
            f"[{'OK' if ratio <= 1.5 else 'NOTE'}]"
        )
    lines.append("")

    # Per-camera histogram (top N by proposed markers)
    ranked = sorted(
        per_camera.items(),
        key=lambda kv: sum(kv[1].values()),
        reverse=True,
    )[:10]
    lines.append("## Top 10 cameras by proposed-marker count")
    for key, counter in ranked:
        parts = " ".join(f"{k}={v}" for k, v in sorted(counter.items()))
        lines.append(f"  {key}   total={sum(counter.values())}   {parts}")
    lines.append("")

    if apply_new:
        lines.append(f"applied_rows: {applied}")

    path.write_text("\n".join(lines) + "\n")


def _count_existing_event_type(per_camera: dict, event_type: str) -> int:
    """Note: per_camera only tracks PROPOSED markers, not existing.
    For the drop/spike heuristic we query spikes directly at report time;
    this helper is a placeholder to keep the gate block readable."""
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.overwrite_all:
        log.error(
            "refusing to use --overwrite-all in M12 rollout — this flag is "
            "reserved for a later release. Exiting."
        )
        return 2

    produce = frozenset(
        k.strip() for k in args.produce.split(",") if k.strip()
    )
    unknown = produce - IMPLEMENTED_HISTORY_MARKERS
    if unknown:
        log.error("unknown marker keys in --produce: %s", sorted(unknown))
        return 2

    out_root = Path(args.out) if args.out else Path(
        "logs/m12-rederive"
    ) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set")
        return 2
    engine = create_engine(db_url)

    result = rederive(
        engine,
        serial_number=args.serial,
        camera_id=args.camera,
        days=args.days,
        produce=produce,
        out_dir=out_root,
        apply_new=args.apply_new,
        overwrite_all=False,
    )
    log.info("done: %s", json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
