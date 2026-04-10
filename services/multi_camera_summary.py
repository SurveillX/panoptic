"""
Multi-camera summary — aggregates summaries across multiple cameras.

Queries vil_summaries for a set of cameras within a time window,
aggregates signals and summaries, then calls the LLM to produce
a single combined summary.

Usage:
    VLLM_MODEL=gemma-4-e4b-it DATABASE_URL=postgresql://user:pass@localhost/dbname \
        PYTHONPATH=. python -m services.multi_camera_summary \
        --cameras cam-01 cam-02 \
        --start 2026-04-08T00:00:00+00:00 \
        --end 2026-04-08T23:59:59+00:00
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import create_engine, text

from shared.clients.vlm import get_vlm_client

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/vil")

# Normalize labels — same mapping as embedding worker
_LABEL_CONTAINS = [
    ("underperform", "underperforming"),
    ("late",         "late start"),
    ("after",        "after hours activity"),
    ("start",        "start of activity"),
    ("spike",        "spike in activity"),
    ("drop",         "drop in activity"),
]


def _normalize_label(label: str) -> str | None:
    lower = label.lower()
    for keyword, canonical in _LABEL_CONTAINS:
        if keyword in lower:
            return canonical
    return None


@dataclass(frozen=True)
class MultiCameraSummaryResult:
    summary: str
    signals: list[str]
    camera_count: int
    summary_count: int


def multi_camera_summary(
    engine,
    camera_ids: list[str],
    start_time: str,
    end_time: str,
    vlm_client=None,
) -> MultiCameraSummaryResult:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT scope_id, summary, key_events, start_time, end_time
                  FROM vil_summaries
                 WHERE level = 'camera'
                   AND scope_id = ANY(:camera_ids)
                   AND start_time >= :start_time
                   AND end_time <= :end_time
                   AND is_latest = true
                 ORDER BY scope_id, start_time
            """),
            {
                "camera_ids": camera_ids,
                "start_time": start_time,
                "end_time": end_time,
            },
        ).fetchall()

    if not rows:
        return MultiCameraSummaryResult(
            summary="No summaries found for the specified cameras and time window.",
            signals=[],
            camera_count=0,
            summary_count=0,
        )

    # Aggregate
    cameras_seen = set()
    all_labels = set()
    per_camera_summaries = []

    for row in rows:
        cameras_seen.add(row.scope_id)
        per_camera_summaries.append(f"[{row.scope_id} {row.start_time}–{row.end_time}] {row.summary}")
        for event in (row.key_events or []):
            label = event.get("label", str(event)) if isinstance(event, dict) else str(event)
            normalized = _normalize_label(label)
            if normalized:
                all_labels.add(normalized)

    signals = sorted(all_labels)
    camera_count = len(cameras_seen)

    # Build prompt
    signals_text = ", ".join(signals) if signals else "none"
    summaries_block = "\n".join(per_camera_summaries)

    prompt = (
        f"Summarize activity across {camera_count} cameras.\n"
        f"Signals observed: {signals_text}\n"
        f"Number of cameras: {camera_count}\n"
        f"Time window: {start_time} to {end_time}\n\n"
        f"Individual camera summaries:\n{summaries_block}\n\n"
        f"Write a 2-3 sentence combined summary. "
        f"Include all observed signals using their exact phrases. "
        f"Return ONLY valid JSON: {{\"summary\": \"<text>\"}}"
    )

    # Call LLM
    if vlm_client:
        system_msg = "You are a strict intelligence analysis system. You MUST follow all rules exactly."
        raw = vlm_client.call(prompt, [], system_message=system_msg)
        try:
            result = json.loads(raw)
            summary = result.get("summary", raw)
        except json.JSONDecodeError:
            summary = raw
    else:
        summary = (
            f"Combined summary across {camera_count} cameras. "
            f"Signals: {signals_text}. "
            f"{len(rows)} individual summaries aggregated."
        )

    return MultiCameraSummaryResult(
        summary=summary,
        signals=signals,
        camera_count=camera_count,
        summary_count=len(rows),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Multi-camera summary")
    parser.add_argument("--cameras", nargs="+", required=True, help="Camera IDs")
    parser.add_argument("--start", required=True, help="Start time (ISO)")
    parser.add_argument("--end", required=True, help="End time (ISO)")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM call, use stub")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    vlm_client = None
    if not args.no_llm:
        try:
            vlm_client = get_vlm_client()
        except Exception as exc:
            log.warning("VLM client unavailable, using stub: %s", exc)

    result = multi_camera_summary(
        engine,
        camera_ids=args.cameras,
        start_time=args.start,
        end_time=args.end,
        vlm_client=vlm_client,
    )

    print(f"\nCameras: {result.camera_count}")
    print(f"Summaries aggregated: {result.summary_count}")
    print(f"Signals: {result.signals}")
    print(f"\nSummary:\n{result.summary}")


if __name__ == "__main__":
    main()
