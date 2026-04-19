"""
M10 — trailer-day rollup endpoint.

GET /v1/trailer/{serial_number}/day/{yyyy-mm-dd}

One read giving the operator UI everything needed to render a
single-page "what happened on trailer X on day Y" view:

- events in the window (all cameras, sorted desc by event_time_utc)
- images in the window (deduped by trigger within a 5-min cluster,
  trigger-priority ordered)
- summaries in the window
- per-camera counts (event/image/summary)
- the latest daily-report row's id + status if one exists

Reuses `shared/report/synthesis` helpers so the SQL shape matches what
the M9 report generator consumes — any fix in one place benefits both.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from shared.report.synthesis import (
    dedup_images,
    fetch_events,
    fetch_images,
    fetch_summaries,
    list_cameras_in_window,
)

from .schemas import (
    TimeRange,
    TrailerDayEvent,
    TrailerDayImage,
    TrailerDayPerCamera,
    TrailerDayResponse,
    TrailerDaySummary,
)

log = logging.getLogger(__name__)


_MAX_EVENTS = 200
_MAX_IMAGES_PER_CAMERA = 12
_MAX_SUMMARIES_PER_CAMERA = 12


def _parse_day(day: str) -> tuple[datetime, datetime]:
    """YYYY-MM-DD → [00:00:00 UTC, next-day 00:00:00 UTC)."""
    dt = datetime.fromisoformat(day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def get_trailer_day(engine, serial_number: str, day: str):
    try:
        window_start, window_end = _parse_day(day)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid date", "detail": str(exc)},
        )

    tr = TimeRange(start=window_start, end=window_end)

    try:
        # Enumerate cameras that pushed anything in the window. This only
        # counts cameras with at least one image — cameras that only pushed
        # buckets also matter, so union with bucket cameras.
        image_cameras = list_cameras_in_window(engine, serial_number, tr)
        with engine.connect() as conn:
            bucket_rows = conn.execute(
                sa_text("""
                    SELECT DISTINCT camera_id
                      FROM panoptic_buckets
                     WHERE serial_number = :sn
                       AND bucket_start_utc >= :tstart
                       AND bucket_start_utc <  :tend
                """),
                {"sn": serial_number, "tstart": window_start, "tend": window_end},
            ).fetchall()
        bucket_cameras = [r.camera_id for r in bucket_rows]
        cameras = sorted(set(image_cameras) | set(bucket_cameras))

        per_camera: list[TrailerDayPerCamera] = []
        all_events: list[TrailerDayEvent] = []
        all_images: list[TrailerDayImage] = []
        all_summaries: list[TrailerDaySummary] = []

        for cam in cameras:
            cam_summaries = fetch_summaries(
                engine, serial_number, cam, tr, _MAX_SUMMARIES_PER_CAMERA,
            )
            cam_images_raw = fetch_images(
                engine, serial_number, cam, tr, _MAX_IMAGES_PER_CAMERA * 3,
            )
            cam_images = dedup_images(cam_images_raw)[:_MAX_IMAGES_PER_CAMERA]
            cam_events = fetch_events(
                engine, serial_number, cam, tr, _MAX_EVENTS,
            )

            for e in cam_events:
                all_events.append(TrailerDayEvent(
                    event_id=e["event_id"],
                    event_type=e["event_type"],
                    event_source=e["event_source"],
                    camera_id=e.get("camera_id"),
                    severity=e.get("severity"),
                    confidence=e.get("confidence"),
                    event_time_utc=e.get("event_time_utc"),
                    title=e.get("title"),
                    description=e.get("description"),
                    bucket_id=e.get("bucket_id"),
                    image_id=e.get("image_id"),
                ))

            for img in cam_images:
                all_images.append(TrailerDayImage(
                    image_id=img["image_id"],
                    camera_id=img.get("camera_id"),
                    trigger=img.get("trigger"),
                    captured_at=img.get("captured_at"),
                    bucket_start=img.get("bucket_start"),
                    caption_text=img.get("caption_text"),
                ))

            for s in cam_summaries:
                all_summaries.append(TrailerDaySummary(
                    summary_id=s["summary_id"],
                    camera_id=cam,
                    level=s.get("level"),
                    start_time=s.get("start_time"),
                    end_time=s.get("end_time"),
                    summary=s.get("summary"),
                    confidence=s.get("confidence"),
                ))

            if cam_events or cam_images or cam_summaries:
                per_camera.append(TrailerDayPerCamera(
                    camera_id=cam,
                    event_count=len(cam_events),
                    image_count=len(cam_images),
                    summary_count=len(cam_summaries),
                ))

        # Events sorted desc by event_time_utc across all cameras.
        all_events.sort(key=lambda e: e.event_time_utc or "", reverse=True)

        # Latest daily report for this window.
        with engine.connect() as conn:
            report_row = conn.execute(
                sa_text("""
                    SELECT report_id, status
                      FROM panoptic_reports
                     WHERE serial_number = :sn
                       AND kind = 'daily'
                       AND window_start_utc = :ws
                     LIMIT 1
                """),
                {"sn": serial_number, "ws": window_start},
            ).fetchone()

        response = TrailerDayResponse(
            serial_number=serial_number,
            date=window_start.strftime("%Y-%m-%d"),
            window_start_utc=window_start.isoformat(),
            window_end_utc=window_end.isoformat(),
            events=all_events,
            images=all_images,
            summaries=all_summaries,
            per_camera=per_camera,
            event_count=len(all_events),
            image_count=len(all_images),
            summary_count=len(all_summaries),
            camera_count=len(per_camera),
            latest_daily_report_id=report_row.report_id if report_row else None,
            latest_daily_report_status=report_row.status if report_row else None,
        )
        return response.model_dump(mode="json")

    except Exception as exc:
        log.exception("trailer_day: failed serial=%s day=%s", serial_number, day)
        return JSONResponse(
            status_code=500,
            content={"error": "trailer_day failed", "detail": str(exc)[:500]},
        )
