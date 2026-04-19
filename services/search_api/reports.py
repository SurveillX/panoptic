"""
M9 report HTTP handlers.

Endpoints (wired in services/search_api/app.py):
  POST /v1/reports/daily     — enqueue daily report generation
  POST /v1/reports/weekly    — enqueue weekly report generation
  GET  /v1/reports/{id}      — fetch status + metadata
  GET  /v1/reports/{id}/assets/{image_id}.jpg  — authorized JPEG asset

Enqueue path (shared between daily + weekly):
  1. Compute report_id = sha256(serial, kind, window_start, window_end).
  2. Upsert panoptic_reports (status='pending') if no existing row OR
     existing row is in a non-terminal or failed state; leave 'success'
     rows untouched.
  3. Upsert panoptic_jobs (job_key=report_generate:<sn>:<kind>:<start>)
     with max_attempts=1. ON CONFLICT — if row already exists and isn't
     leased/running, reset to pending for re-dispatch.
  4. COMMIT.
  5. XADD to panoptic:jobs:report_generate (post-commit).
  6. Return {report_id, status}.

Idempotency:
  - Calling daily twice for the same window is a no-op when the report
    is already success/running/pending. Failed reports are retried.
  - The same cron run across a fleet tolerant — if a prior run is still
    in flight when the cron fires, no duplicate work.

Asset authorization:
  The /assets/ endpoint authorizes the requested image_id against
  metadata_json.cited_image_ids for that report. Images not cited by
  the report (i.e., not rendered in the HTML) 404.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import redis
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text as sa_text

from shared.schemas.job import make_report_generate_key
from shared.schemas.report import (
    DailyReportRequest,
    ReportEnqueueResponse,
    ReportMetadata,
    ReportStatusResponse,
    WeeklyReportRequest,
    generate_report_id,
)
from shared.utils.streams import enqueue_job

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window normalization
# ---------------------------------------------------------------------------


def _daily_window(date_str: str) -> tuple[datetime, datetime]:
    """Normalize a YYYY-MM-DD or ISO datetime string to a 24h UTC window."""
    s = date_str.strip()
    if "T" in s:
        # Full ISO datetime — floor to midnight UTC.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _weekly_window(iso_week: str) -> tuple[datetime, datetime]:
    """Normalize 'YYYYWnn' to a Mon-anchored 7-day UTC window."""
    # DailyReportRequest validates the regex; assume shape here.
    year = int(iso_week[:4])
    week = int(iso_week[5:])
    monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7)


# ---------------------------------------------------------------------------
# Shared enqueue
# ---------------------------------------------------------------------------


# Report statuses that DON'T warrant a re-enqueue on an incoming request.
# 'success' reuses the existing HTML. 'running'/'pending' means a worker is
# already on it or about to be; re-enqueueing would duplicate work (ON
# CONFLICT protects us, but this short-circuits cleanly).
_TERMINAL_OR_IN_FLIGHT = frozenset({"success", "running", "pending"})


def _enqueue_report(
    *,
    engine,
    r: redis.Redis,
    serial_number: str,
    kind: Literal["daily", "weekly"],
    window_start: datetime,
    window_end: datetime,
) -> tuple[str, str]:
    """Upsert panoptic_reports + panoptic_jobs, push to stream, return
    (report_id, status)."""
    report_id = generate_report_id(
        serial_number=serial_number,
        kind=kind,
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    job_key = make_report_generate_key(serial_number, kind, window_start)
    new_job_id = str(uuid.uuid4())
    payload = json.dumps({
        "report_id": report_id,
        "kind": kind,
        "serial_number": serial_number,
    })

    should_enqueue = True
    current_status = "pending"
    job_id: str | None = None

    with engine.connect() as conn:
        # Does the report row already exist in a non-failed state?
        existing = conn.execute(
            sa_text("SELECT status FROM panoptic_reports WHERE report_id = :rid"),
            {"rid": report_id},
        ).fetchone()

        if existing is not None and existing.status in _TERMINAL_OR_IN_FLIGHT:
            # Reuse; do NOT re-enqueue.
            conn.rollback()
            return report_id, existing.status

        # Upsert the report row into 'pending' (either fresh insert or reset
        # from 'failed').
        conn.execute(
            sa_text("""
                INSERT INTO panoptic_reports (report_id, serial_number, kind,
                    window_start_utc, window_end_utc, status, metadata_json)
                VALUES (:rid, :sn, :kind, :ws, :we, 'pending', '{}'::jsonb)
                ON CONFLICT (report_id) DO UPDATE SET
                    status = 'pending',
                    storage_path = NULL,
                    last_error = NULL,
                    generated_at = NULL,
                    metadata_json = '{}'::jsonb,
                    updated_at = now()
            """),
            {
                "rid": report_id, "sn": serial_number, "kind": kind,
                "ws": window_start, "we": window_end,
            },
        )

        # Create / reset the job row. max_attempts=1 → one shot, then DLQ.
        job_row = conn.execute(
            sa_text("""
                INSERT INTO panoptic_jobs (job_id, job_key, serial_number, job_type,
                    priority, state, attempt_count, max_attempts, payload)
                VALUES (:jid, :jkey, :sn, 'report_generate', 'normal',
                    'pending', 0, 1, CAST(:payload AS jsonb))
                ON CONFLICT (job_key) DO UPDATE SET
                    state = 'pending',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    attempt_count = 0,
                    last_error = NULL,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                RETURNING job_id
            """),
            {
                "jid": new_job_id, "jkey": job_key, "sn": serial_number,
                "payload": payload,
            },
        ).fetchone()
        job_id = str(job_row.job_id)
        conn.commit()

    # Post-commit: push to Redis stream. If this fails, the job row is
    # still pending in Postgres and the reclaimer will pick it up.
    if should_enqueue and job_id is not None:
        try:
            enqueue_job(
                r,
                job_type="report_generate",
                job_id=job_id,
                serial_number=serial_number,
                priority="normal",
            )
        except Exception as exc:
            log.error(
                "reports: Redis enqueue failed report_id=%s job_id=%s: %s — "
                "job exists in Postgres, reclaimer will pick it up",
                report_id, job_id, exc,
            )

    return report_id, current_status


# ---------------------------------------------------------------------------
# POST /v1/reports/daily
# ---------------------------------------------------------------------------


def generate_daily_report(engine, r: redis.Redis, body: dict):
    try:
        req = DailyReportRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "validation failed", "detail": json.loads(exc.json())},
        )

    try:
        window_start, window_end = _daily_window(req.date)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid date", "detail": str(exc)},
        )

    try:
        report_id, status = _enqueue_report(
            engine=engine, r=r, serial_number=req.serial_number,
            kind="daily",
            window_start=window_start, window_end=window_end,
        )
    except Exception as exc:
        log.exception("reports: daily enqueue failed")
        return JSONResponse(
            status_code=500,
            content={"error": "enqueue failed", "detail": str(exc)[:500]},
        )

    return ReportEnqueueResponse(report_id=report_id, status=status).model_dump()


# ---------------------------------------------------------------------------
# POST /v1/reports/weekly
# ---------------------------------------------------------------------------


def generate_weekly_report(engine, r: redis.Redis, body: dict):
    try:
        req = WeeklyReportRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "validation failed", "detail": json.loads(exc.json())},
        )

    try:
        window_start, window_end = _weekly_window(req.iso_week)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid iso_week", "detail": str(exc)},
        )

    try:
        report_id, status = _enqueue_report(
            engine=engine, r=r, serial_number=req.serial_number,
            kind="weekly",
            window_start=window_start, window_end=window_end,
        )
    except Exception as exc:
        log.exception("reports: weekly enqueue failed")
        return JSONResponse(
            status_code=500,
            content={"error": "enqueue failed", "detail": str(exc)[:500]},
        )

    return ReportEnqueueResponse(report_id=report_id, status=status).model_dump()


# ---------------------------------------------------------------------------
# GET /v1/reports/{report_id}
# ---------------------------------------------------------------------------


def get_report_status(engine, report_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT report_id, serial_number, kind,
                       window_start_utc, window_end_utc,
                       storage_path, status, last_error, generated_at,
                       metadata_json, created_at, updated_at
                  FROM panoptic_reports
                 WHERE report_id = :rid
            """),
            {"rid": report_id},
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "report not found", "report_id": report_id},
        )

    metadata = ReportMetadata.model_validate(row.metadata_json or {})
    response = ReportStatusResponse(
        report_id=row.report_id,
        serial_number=row.serial_number,
        kind=row.kind,
        window_start_utc=row.window_start_utc,
        window_end_utc=row.window_end_utc,
        storage_path=row.storage_path,
        status=row.status,
        last_error=row.last_error,
        generated_at=row.generated_at,
        metadata=metadata,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /v1/reports/{report_id}/assets/{image_id}.jpg
# ---------------------------------------------------------------------------


def get_report_asset(engine, report_id: str, image_id: str):
    """
    Serve an image JPEG only if (a) the report exists and is in 'success',
    (b) image_id is in metadata_json.cited_image_ids, and (c) the file
    exists on disk.

    Returns 404 for every other case. Never expose internal storage paths.
    """
    with engine.connect() as conn:
        report_row = conn.execute(
            sa_text("""
                SELECT status, metadata_json
                  FROM panoptic_reports
                 WHERE report_id = :rid
            """),
            {"rid": report_id},
        ).fetchone()

        if report_row is None or report_row.status != "success":
            return JSONResponse(
                status_code=404,
                content={"error": "report not found or not ready"},
            )

        metadata = report_row.metadata_json or {}
        cited = metadata.get("cited_image_ids") or []
        if image_id not in cited:
            return JSONResponse(
                status_code=404,
                content={"error": "image not cited by this report"},
            )

        image_row = conn.execute(
            sa_text("""
                SELECT storage_path
                  FROM panoptic_images
                 WHERE image_id = :iid
            """),
            {"iid": image_id},
        ).fetchone()

    if image_row is None or not image_row.storage_path:
        return JSONResponse(
            status_code=404,
            content={"error": "image not found"},
        )

    storage_path = image_row.storage_path
    if not os.path.exists(storage_path):
        log.warning(
            "reports: asset missing on disk report=%s image=%s path=%s",
            report_id, image_id, storage_path,
        )
        return JSONResponse(
            status_code=404,
            content={"error": "image file missing"},
        )

    return FileResponse(
        storage_path,
        media_type="image/jpeg",
        filename=f"{image_id}.jpg",
    )


# ---------------------------------------------------------------------------
# GET /v1/reports — list recent reports for a trailer (M10 P10.1c slide-in)
# ---------------------------------------------------------------------------


def list_reports(
    engine,
    *,
    serial_number: str | None,
    kind: str | None,
    limit: int,
):
    """
    Return recent panoptic_reports rows in reverse-chronological order
    by window_start_utc. Used by the operator UI's report-history panel
    and (later) the full report browse page.

    All filters are optional. limit is capped at 50.
    """
    if limit <= 0:
        limit = 10
    if limit > 50:
        limit = 50

    clauses: list[str] = []
    params: dict = {"lim": limit}
    if serial_number:
        clauses.append("serial_number = :sn")
        params["sn"] = serial_number
    if kind:
        if kind not in ("daily", "weekly"):
            return JSONResponse(
                status_code=400,
                content={"error": "kind must be 'daily' or 'weekly'"},
            )
        clauses.append("kind = :k")
        params["k"] = kind

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = sa_text(f"""
        SELECT report_id, serial_number, kind,
               window_start_utc, window_end_utc,
               status, generated_at, created_at
          FROM panoptic_reports
          {where}
         ORDER BY window_start_utc DESC, created_at DESC
         LIMIT :lim
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    out = []
    for r in rows:
        out.append({
            "report_id":        r["report_id"],
            "serial_number":    r["serial_number"],
            "kind":             r["kind"],
            "window_start_utc": r["window_start_utc"].isoformat() if r["window_start_utc"] else None,
            "window_end_utc":   r["window_end_utc"].isoformat() if r["window_end_utc"] else None,
            "status":           r["status"],
            "generated_at":     r["generated_at"].isoformat() if r["generated_at"] else None,
            "created_at":       r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {"reports": out, "count": len(out)}


# ---------------------------------------------------------------------------
# GET /v1/reports/{report_id}/view — stream the stored HTML (M10)
# ---------------------------------------------------------------------------


def get_report_view(engine, report_id: str):
    """
    Stream the stored HTML report for iframe embedding by the M10
    operator UI. Only serves reports in `success` state; returns 404
    for anything else (not-found, pending, running, failed).

    Requires `/data/panoptic-store/reports` bind-mounted on the
    search_api container (added to docker-compose.yml for M10).
    """
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT status, storage_path
                  FROM panoptic_reports
                 WHERE report_id = :rid
            """),
            {"rid": report_id},
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "report not found"},
        )
    if row.status != "success" or not row.storage_path:
        return JSONResponse(
            status_code=404,
            content={"error": "report not ready"},
        )

    storage_path = row.storage_path
    if not os.path.exists(storage_path):
        log.warning(
            "reports: view path missing on disk report=%s path=%s",
            report_id, storage_path,
        )
        return JSONResponse(
            status_code=404,
            content={"error": "report file missing"},
        )

    # Don't pass filename= here — FileResponse converts it to a
    # `content-disposition: attachment` header, which makes browsers
    # trigger a download instead of rendering in an iframe.
    return FileResponse(
        storage_path,
        media_type="text/html; charset=utf-8",
    )
