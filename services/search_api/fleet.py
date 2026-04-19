"""
M10 — fleet overview endpoint.

GET /v1/fleet/overview

Returns one row per registered trailer with a composite rollup so the
operator UI can render the fleet list in a single request:

  - serial_number, name, is_active
  - last_bucket_start_utc   (max across panoptic_buckets)
  - last_image_captured_at_utc (max across panoptic_images)
  - event_count_24h         (rolling 24h count from panoptic_events)
  - latest_daily_report_id + window_start + generated_at

Output is capped at 50 trailers. Indexed scans: the existing
ix_panoptic_buckets_sn_start_desc covers last_bucket; the event count
uses ix_panoptic_events_sn_camera_time. Fine at fleet sizes up to
several hundred trailers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from .schemas import FleetOverviewResponse, FleetTrailer

log = logging.getLogger(__name__)

_MAX_TRAILERS = 50


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


_FLEET_SQL = sa_text("""
    WITH tr AS (
      SELECT serial_number, name, is_active
        FROM panoptic_trailers
       WHERE is_active = true
       ORDER BY serial_number
       LIMIT :lim
    ),
    last_bucket AS (
      SELECT serial_number, MAX(bucket_start_utc) AS last_bucket
        FROM panoptic_buckets
       WHERE serial_number IN (SELECT serial_number FROM tr)
       GROUP BY 1
    ),
    last_image AS (
      SELECT serial_number, MAX(captured_at_utc) AS last_image
        FROM panoptic_images
       WHERE serial_number IN (SELECT serial_number FROM tr)
       GROUP BY 1
    ),
    events24 AS (
      SELECT serial_number, COUNT(*) AS n_events
        FROM panoptic_events
       WHERE serial_number IN (SELECT serial_number FROM tr)
         AND event_time_utc > (now() - interval '24 hours')
       GROUP BY 1
    ),
    latest_report AS (
      SELECT DISTINCT ON (serial_number)
             serial_number, report_id, window_start_utc, generated_at
        FROM panoptic_reports
       WHERE serial_number IN (SELECT serial_number FROM tr)
         AND kind = 'daily'
         AND status = 'success'
       ORDER BY serial_number, window_start_utc DESC, generated_at DESC
    )
    SELECT tr.serial_number,
           tr.name,
           tr.is_active,
           last_bucket.last_bucket        AS last_bucket,
           last_image.last_image          AS last_image,
           COALESCE(events24.n_events, 0) AS n_events,
           latest_report.report_id        AS latest_daily_report_id,
           latest_report.window_start_utc AS latest_daily_report_window,
           latest_report.generated_at     AS latest_daily_report_generated_at
      FROM tr
 LEFT JOIN last_bucket   USING (serial_number)
 LEFT JOIN last_image    USING (serial_number)
 LEFT JOIN events24      USING (serial_number)
 LEFT JOIN latest_report USING (serial_number)
     ORDER BY tr.serial_number
""")


def get_fleet_overview(engine):
    try:
        with engine.connect() as conn:
            rows = conn.execute(_FLEET_SQL, {"lim": _MAX_TRAILERS}).mappings().all()
    except Exception as exc:
        log.exception("fleet: composite rollup query failed")
        return JSONResponse(
            status_code=500,
            content={"error": "fleet rollup failed", "detail": str(exc)[:500]},
        )

    trailers = [
        FleetTrailer(
            serial_number=r["serial_number"],
            name=r["name"],
            is_active=bool(r["is_active"]),
            last_bucket_start_utc=_iso(r["last_bucket"]),
            last_image_captured_at_utc=_iso(r["last_image"]),
            event_count_24h=int(r["n_events"] or 0),
            latest_daily_report_id=r["latest_daily_report_id"],
            latest_daily_report_window_start_utc=_iso(r["latest_daily_report_window"]),
            latest_daily_report_generated_at=_iso(r["latest_daily_report_generated_at"]),
        )
        for r in rows
    ]

    response = FleetOverviewResponse(
        trailers=trailers,
        count=len(trailers),
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    return response.model_dump(mode="json")
