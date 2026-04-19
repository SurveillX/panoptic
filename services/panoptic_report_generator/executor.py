"""
report_generate job executor.

Handles daily (and eventually weekly, P9.4) report generation. Each job
consumes one row from `panoptic_reports` that was inserted by the HTTP
enqueue handler or cron driver in 'pending' state.

Lifecycle (panoptic_reports.status):
  pending   ← set by enqueue path
  running   ← set via a short external commit before long-running work
              starts, so HTTP/UI callers can observe mid-job state
  success   ← set in the job conn with storage_path + metadata_json
  failed    ← set via a short external commit with last_error populated

The job conn (inside LeaseHeartbeat) commits only the success transition.
running/failed are written via the engine in small separate transactions
so that mid-job visibility survives even if the worker crashes before
reaching its main commit.

No retries in v1: VLM gibberish + prompt-structure failures aren't
transiently recoverable; failures are DLQ'd on the first attempt
(enqueue path sets max_attempts=1).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from shared.clients.vlm import VLMClient
from shared.report.aggregate import compute_weekly_aggregates
from shared.report.render import TEMPLATE_VERSION, render_daily, render_weekly
from shared.report.synthesis import (
    dedup_images,
    fetch_events,
    fetch_images,
    fetch_summaries,
    fuse,
    list_cameras_in_window,
    synthesize_camera_summary,
    synthesize_weekly,
)
from shared.schemas.report import ReportKind

# Reverse import: reuse search-API schemas until they're moved into shared.
from services.search_api.schemas import TimeRange

log = logging.getLogger(__name__)


JobState = Literal["succeeded", "failed_terminal", "retry_wait"]


# Default per-window fetch budgets for daily reports.
_DAILY_MAX_SUMMARIES_PER_CAMERA = 12
_DAILY_MAX_IMAGES_PER_CAMERA = 6
_DAILY_MAX_EVENTS_PER_CAMERA = 20
_DAILY_SUMMARY_TYPE = "operational"


REPORT_STORAGE_ROOT = os.environ.get(
    "REPORT_STORAGE_ROOT", "/data/panoptic-store/reports"
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_report_generate_job(
    conn,
    payload: dict,
    worker_id: str,
    engine: Engine,
    vlm: VLMClient,
) -> JobState:
    report_id = payload.get("report_id")
    if not report_id:
        log.error("run_report_generate_job: missing report_id in payload=%r", payload)
        return "failed_terminal"

    row = conn.execute(
        text("""
            SELECT report_id, serial_number, kind,
                   window_start_utc, window_end_utc,
                   status, metadata_json
              FROM panoptic_reports
             WHERE report_id = :report_id
        """),
        {"report_id": report_id},
    ).fetchone()

    if row is None:
        log.error("run_report_generate_job: report_id=%s missing", report_id)
        return "failed_terminal"

    if row.status == "success":
        log.info(
            "run_report_generate_job: report_id=%s already success — no-op",
            report_id,
        )
        return "succeeded"

    # Mark running externally so callers see mid-job state.
    _mark_status(engine, report_id=report_id, status="running")

    kind = row.kind
    window_start = row.window_start_utc
    window_end = row.window_end_utc

    try:
        if kind == "daily":
            result = _generate_daily(
                engine=engine,
                vlm=vlm,
                report_id=report_id,
                serial_number=row.serial_number,
                window_start=window_start,
                window_end=window_end,
            )
        elif kind == "weekly":
            result = _generate_weekly(
                engine=engine,
                vlm=vlm,
                report_id=report_id,
                serial_number=row.serial_number,
                window_start=window_start,
                window_end=window_end,
            )
        else:
            log.error("run_report_generate_job: unknown kind=%r", kind)
            _mark_status(
                engine, report_id=report_id, status="failed",
                last_error=f"unknown report kind {kind!r}",
            )
            return "failed_terminal"
    except Exception as exc:
        log.exception(
            "run_report_generate_job: unexpected error report_id=%s: %s",
            report_id, exc,
        )
        _mark_status(
            engine, report_id=report_id, status="failed",
            last_error=str(exc)[:1000],
        )
        return "failed_terminal"

    # Success path: commit storage_path + metadata inside the job conn so
    # that the lease release + this update land in the same transaction.
    conn.execute(
        text("""
            UPDATE panoptic_reports
               SET status        = 'success',
                   storage_path  = :storage_path,
                   last_error    = NULL,
                   generated_at  = now(),
                   metadata_json = CAST(:metadata_json AS jsonb),
                   updated_at    = now()
             WHERE report_id = :report_id
        """),
        {
            "report_id":     report_id,
            "storage_path":  result["storage_path"],
            "metadata_json": json.dumps(result["metadata"]),
        },
    )
    log.info(
        "run_report_generate_job: success report_id=%s path=%s",
        report_id, result["storage_path"],
    )
    return "succeeded"


# ---------------------------------------------------------------------------
# Daily generation
# ---------------------------------------------------------------------------


def _generate_daily(
    *,
    engine: Engine,
    vlm: VLMClient,
    report_id: str,
    serial_number: str,
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """Run synthesis + render + write for a daily report. Returns
    {storage_path, metadata}. Raises on any fatal error (caller logs +
    marks failed)."""

    tr = TimeRange(start=window_start, end=window_end)

    # Enumerate cameras that had any image push in the window.
    camera_ids = list_cameras_in_window(engine, serial_number, tr)

    per_camera_evidence: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
    total_summary_count = 0
    total_image_count = 0
    total_event_count = 0

    for cam in camera_ids:
        summaries = fetch_summaries(
            engine, serial_number, cam, tr, _DAILY_MAX_SUMMARIES_PER_CAMERA,
        )
        images_raw = fetch_images(
            engine, serial_number, cam, tr, _DAILY_MAX_IMAGES_PER_CAMERA * 3,
        )
        images = dedup_images(images_raw)[:_DAILY_MAX_IMAGES_PER_CAMERA]
        events = fetch_events(
            engine, serial_number, cam, tr, _DAILY_MAX_EVENTS_PER_CAMERA,
        )
        if summaries or images or events:
            per_camera_evidence[cam] = (summaries, images, events)
            total_summary_count += len(summaries)
            total_image_count += len(images)
            total_event_count += len(events)

    # Per-camera synthesis (VLM, one call per camera)
    camera_summaries = []
    for cam, (summaries, images, events) in per_camera_evidence.items():
        cs = synthesize_camera_summary(
            serial_number=serial_number,
            camera_id=cam,
            time_range=tr,
            summary_type=_DAILY_SUMMARY_TYPE,
            summaries=summaries,
            images=images,
            events=events,
            vlm=vlm,
        )
        if cs is not None:
            camera_summaries.append(cs)

    # Fusion
    overall = fuse(
        serial_number=serial_number,
        time_range=tr,
        summary_type=_DAILY_SUMMARY_TYPE,
        camera_summaries=camera_summaries,
        vlm=vlm,
    )

    # Build per-camera render context + collect provenance.
    per_camera_ctx: list[dict] = []
    cited_image_ids: list[str] = []
    cited_event_ids: list[str] = []
    cited_summary_ids: list[str] = []
    cited_camera_ids: list[str] = []

    cs_by_cam = {cs.camera_id: cs for cs in camera_summaries}
    for cam, (summaries, images, events) in per_camera_evidence.items():
        cs = cs_by_cam.get(cam)
        if cs is None:
            # VLM failed for this camera — still render a placeholder block.
            per_camera_ctx.append({
                "camera_id": cam,
                "headline": "Per-camera synthesis unavailable.",
                "summary": "The VLM failed to produce a summary for this camera.",
                "confidence": 0.0,
                "images": images,
            })
        else:
            per_camera_ctx.append({
                "camera_id": cam,
                "headline": cs.headline,
                "summary": cs.summary,
                "confidence": cs.confidence,
                "images": images,
            })

        cited_camera_ids.append(cam)
        for img in images:
            iid = img.get("image_id")
            if iid:
                cited_image_ids.append(iid)
        for ev in events:
            eid = ev.get("event_id")
            if eid:
                cited_event_ids.append(eid)
        for s in summaries:
            sid = s.get("summary_id")
            if sid:
                cited_summary_ids.append(sid)

    # All events flat — used for the bottom "Events" table in the HTML.
    all_events: list[dict] = []
    for _, (_, _, events) in per_camera_evidence.items():
        all_events.extend(events)
    all_events.sort(key=lambda e: e.get("event_time_utc") or "", reverse=True)

    # Render
    storage_path = _compute_storage_path(
        serial_number=serial_number,
        kind="daily",
        window_start=window_start,
    )

    asset_url_prefix = f"/v1/reports/{report_id}/assets"

    html = render_daily({
        "report_id": report_id,
        "serial_number": serial_number,
        "window_start_utc": _iso(window_start),
        "window_end_utc": _iso(window_end),
        "generated_at_utc": _iso(datetime.now(timezone.utc)),
        "per_camera": per_camera_ctx,
        "overall": {
            "headline": overall.headline,
            "summary": overall.summary,
            "confidence": overall.confidence,
            "supporting_camera_ids": overall.supporting_camera_ids,
        },
        "events": all_events,
        "camera_count": len(per_camera_ctx),
        "image_count": total_image_count,
        "summary_count": total_summary_count,
        "event_count": total_event_count,
        "asset_url_prefix": asset_url_prefix,
        "template_version": TEMPLATE_VERSION,
    })

    # Write file (overwrite if exists — regen is idempotent by report_id).
    storage_dir = os.path.dirname(storage_path)
    os.makedirs(storage_dir, exist_ok=True)
    with open(storage_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Persist per-camera narratives so weekly reports can reuse them.
    narratives = [
        {
            "key":        cs.camera_id,
            "headline":   cs.headline,
            "summary":    cs.summary,
            "confidence": cs.confidence,
        }
        for cs in camera_summaries
    ]

    metadata = {
        "cited_image_ids":   _dedup_preserve_order(cited_image_ids),
        "cited_event_ids":   _dedup_preserve_order(cited_event_ids),
        "cited_summary_ids": _dedup_preserve_order(cited_summary_ids),
        "cited_camera_ids":  _dedup_preserve_order(cited_camera_ids),
        "input_counts": {
            "summaries": total_summary_count,
            "images":    total_image_count,
            "events":    total_event_count,
            "cameras":   len(per_camera_ctx),
        },
        "coverage": {
            "cameras_with_data": len(per_camera_ctx),
            "cameras_total":     len(camera_ids),
        },
        "vlm_timings_ms": {},
        "template_version": TEMPLATE_VERSION,
        "narratives": narratives,
        "overall": {
            "headline":   overall.headline,
            "summary":    overall.summary,
            "confidence": overall.confidence,
            "supporting": overall.supporting_camera_ids,
        },
    }

    return {
        "storage_path": storage_path,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Weekly generation (P9.4)
# ---------------------------------------------------------------------------


def _generate_weekly(
    *,
    engine: Engine,
    vlm: VLMClient,
    report_id: str,
    serial_number: str,
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """Run the weekly report: aggregate via SQL + fuse per-day narratives
    via VLM. Reads the week's daily reports from panoptic_reports; missing
    days are rendered as placeholders in the HTML (no on-the-fly daily
    synthesis in v1 — backfill dailies first if needed)."""

    # 1. SQL aggregation over the 7-day window.
    aggregates = compute_weekly_aggregates(
        engine,
        serial_number=serial_number,
        window_start=window_start,
        window_end=window_end,
    )

    # 2. Fetch available daily reports for this trailer in the window.
    with engine.connect() as conn:
        daily_rows = conn.execute(
            text("""
                SELECT report_id, window_start_utc, status, metadata_json
                  FROM panoptic_reports
                 WHERE serial_number   = :sn
                   AND kind            = 'daily'
                   AND window_start_utc >= :ws
                   AND window_start_utc <  :we
                 ORDER BY window_start_utc
            """),
            {"sn": serial_number, "ws": window_start, "we": window_end},
        ).mappings().all()

    daily_by_day: dict[str, dict] = {}
    for row in daily_rows:
        day_key = row["window_start_utc"].strftime("%Y-%m-%d")
        daily_by_day[day_key] = dict(row)

    # Build one per-day entry for each of the 7 days (even missing ones).
    per_day_ctx: list[dict] = []
    day_entries_for_vlm: list[dict] = []
    union_cited_image_ids: list[str] = []
    union_cited_event_ids: list[str] = []
    union_cited_summary_ids: list[str] = []
    union_cited_camera_ids: list[str] = []

    for i in range(7):
        day = window_start + timedelta(days=i)
        day_key = day.strftime("%Y-%m-%d")
        daily = daily_by_day.get(day_key)

        if daily is None or daily["status"] != "success":
            per_day_ctx.append({
                "day_key": day_key,
                "missing": True,
                "headline": "",
                "summary": "",
                "confidence": 0.0,
                "daily_report_id": None,
            })
            continue

        metadata = daily["metadata_json"] or {}
        overall = metadata.get("overall") or {}
        headline = overall.get("headline", "")
        summary = overall.get("summary", "")
        confidence = overall.get("confidence", 0.0)

        per_day_ctx.append({
            "day_key": day_key,
            "missing": False,
            "headline": headline,
            "summary": summary,
            "confidence": confidence,
            "daily_report_id": daily["report_id"],
        })
        if headline or summary:
            day_entries_for_vlm.append({
                "day_key": day_key,
                "headline": headline,
                "summary": summary,
                "confidence": confidence,
            })

        # Union provenance from each daily.
        union_cited_image_ids.extend(metadata.get("cited_image_ids") or [])
        union_cited_event_ids.extend(metadata.get("cited_event_ids") or [])
        union_cited_summary_ids.extend(metadata.get("cited_summary_ids") or [])
        union_cited_camera_ids.extend(metadata.get("cited_camera_ids") or [])

    # 3. Weekly VLM synthesis (one call rolling up per-day narratives).
    weekly_overall = synthesize_weekly(
        serial_number=serial_number,
        window_start_iso=window_start.isoformat(),
        window_end_iso=window_end.isoformat(),
        day_entries=day_entries_for_vlm,
        aggregates=aggregates,
        vlm=vlm,
    )

    # 4. Render.
    storage_path = _compute_storage_path(
        serial_number=serial_number, kind="weekly", window_start=window_start,
    )
    iso_week = window_start.strftime("%GW%V")

    html = render_weekly({
        "report_id": report_id,
        "serial_number": serial_number,
        "iso_week": iso_week,
        "window_start_utc": _iso(window_start),
        "window_end_utc": _iso(window_end),
        "generated_at_utc": _iso(datetime.now(timezone.utc)),
        "dailies_available": sum(1 for d in per_day_ctx if not d["missing"]),
        "weekly_overall": weekly_overall,
        "per_day": per_day_ctx,
        "aggregates": aggregates,
        "template_version": TEMPLATE_VERSION,
    })

    storage_dir = os.path.dirname(storage_path)
    os.makedirs(storage_dir, exist_ok=True)
    with open(storage_path, "w", encoding="utf-8") as f:
        f.write(html)

    metadata = {
        "cited_image_ids":   _dedup_preserve_order(union_cited_image_ids),
        "cited_event_ids":   _dedup_preserve_order(union_cited_event_ids),
        "cited_summary_ids": _dedup_preserve_order(union_cited_summary_ids),
        "cited_camera_ids":  _dedup_preserve_order(union_cited_camera_ids),
        "input_counts": {
            "summaries": sum(
                (daily["metadata_json"] or {}).get("input_counts", {}).get("summaries", 0)
                for daily in daily_by_day.values()
            ),
            "images":    aggregates.get("total_images", 0),
            "events":    aggregates.get("total_events", 0),
            "cameras":   aggregates.get("cameras_seen", 0),
        },
        "coverage": {
            "dailies_available": sum(1 for d in per_day_ctx if not d["missing"]),
            "dailies_total":     7,
        },
        "vlm_timings_ms": {},
        "template_version": TEMPLATE_VERSION,
        "narratives": [
            {
                "key":        d["day_key"],
                "headline":   d["headline"],
                "summary":    d["summary"],
                "confidence": d["confidence"],
            }
            for d in per_day_ctx if not d["missing"]
        ],
        "overall": {
            "headline":   weekly_overall["headline"],
            "summary":    weekly_overall["summary"],
            "confidence": weekly_overall["confidence"],
            "supporting": weekly_overall.get("supporting_day_ids", []),
        },
    }

    return {
        "storage_path": storage_path,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mark_status(
    engine: Engine, *, report_id: str, status: str, last_error: str | None = None,
) -> None:
    """Short external commit for status transitions (running, failed).

    Used so that mid-job state is observable even if the main worker conn
    hasn't committed yet. Kept in its own connection to avoid interfering
    with LeaseHeartbeat.
    """
    with engine.begin() as ext_conn:
        ext_conn.execute(
            text("""
                UPDATE panoptic_reports
                   SET status       = :status,
                       last_error   = :last_error,
                       updated_at   = now()
                 WHERE report_id = :report_id
            """),
            {
                "report_id":  report_id,
                "status":     status,
                "last_error": last_error,
            },
        )


def _compute_storage_path(
    *, serial_number: str, kind: str, window_start: datetime,
) -> str:
    """/data/panoptic-store/reports/<sn>/<yyyy>/<mm>/<sn>-<yyyymmdd>-<kind>.html"""
    yyyy = window_start.strftime("%Y")
    mm = window_start.strftime("%m")
    if kind == "daily":
        stamp = window_start.strftime("%Y%m%d")
    elif kind == "weekly":
        stamp = window_start.strftime("%GW%V")  # ISO-week
    else:
        stamp = window_start.strftime("%Y%m%d")
    filename = f"{serial_number}-{stamp}-{kind}.html"
    return os.path.join(REPORT_STORAGE_ROOT, serial_number, yyyy, mm, filename)


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
