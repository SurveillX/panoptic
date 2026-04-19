"""
SQL aggregations for weekly reports.

Direct COUNT/GROUP BY queries over panoptic_events / panoptic_images /
panoptic_buckets for a 7-day window. These produce the factual tables
that render directly into the weekly HTML without going through the VLM.

Separating these from the VLM narrative avoids the known failure modes
where a text roll-up (a) gets counts wrong, or (b) omits cameras that
happened not to headline any daily summary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.engine import Engine


# Top-K events shown in the notable-events table.
_NOTABLE_EVENTS_LIMIT = 12


def compute_weekly_aggregates(
    engine: Engine,
    *,
    serial_number: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """
    Return a dict with:
      event_type_totals   — {event_type: count}
      image_trigger_totals — {trigger: count}
      per_camera_rank     — [{camera_id, event_count, image_count, bucket_count}, ...] sorted desc
      coverage_per_camera — [{camera_id, buckets, hours_with_data}, ...] sorted by camera
      notable_events      — [event_row, ...] top-K by severity (nulls last)
      total_events        — int
      total_images        — int
      total_buckets       — int
      cameras_seen        — int
    """
    out: dict[str, Any] = {}
    params = {"sn": serial_number, "ws": window_start, "we": window_end}

    with engine.connect() as conn:
        # ---- event_type_totals ----
        rows = conn.execute(
            sa_text("""
                SELECT event_type, COUNT(*) AS n
                  FROM panoptic_events
                 WHERE serial_number = :sn
                   AND event_time_utc >= :ws
                   AND event_time_utc <  :we
                 GROUP BY 1 ORDER BY n DESC
            """),
            params,
        ).mappings().all()
        out["event_type_totals"] = {r["event_type"]: r["n"] for r in rows}
        out["total_events"] = sum(r["n"] for r in rows)

        # ---- image_trigger_totals ----
        rows = conn.execute(
            sa_text("""
                SELECT trigger, COUNT(*) AS n
                  FROM panoptic_images
                 WHERE serial_number = :sn
                   AND bucket_start_utc >= :ws
                   AND bucket_start_utc <  :we
                 GROUP BY 1 ORDER BY n DESC
            """),
            params,
        ).mappings().all()
        out["image_trigger_totals"] = {r["trigger"]: r["n"] for r in rows}
        out["total_images"] = sum(r["n"] for r in rows)

        # ---- per_camera_rank (events + images + bucket count) ----
        rows = conn.execute(
            sa_text("""
                WITH ev AS (
                  SELECT camera_id, COUNT(*) AS event_count
                    FROM panoptic_events
                   WHERE serial_number = :sn
                     AND event_time_utc >= :ws
                     AND event_time_utc <  :we
                   GROUP BY 1
                ),
                im AS (
                  SELECT camera_id, COUNT(*) AS image_count
                    FROM panoptic_images
                   WHERE serial_number = :sn
                     AND bucket_start_utc >= :ws
                     AND bucket_start_utc <  :we
                   GROUP BY 1
                ),
                bk AS (
                  SELECT camera_id, COUNT(*) AS bucket_count
                    FROM panoptic_buckets
                   WHERE serial_number = :sn
                     AND bucket_start_utc >= :ws
                     AND bucket_start_utc <  :we
                   GROUP BY 1
                )
                SELECT COALESCE(ev.camera_id, im.camera_id, bk.camera_id) AS camera_id,
                       COALESCE(ev.event_count,  0) AS event_count,
                       COALESCE(im.image_count,  0) AS image_count,
                       COALESCE(bk.bucket_count, 0) AS bucket_count
                  FROM ev
             FULL JOIN im USING (camera_id)
             FULL JOIN bk USING (camera_id)
                 ORDER BY event_count DESC, image_count DESC, camera_id
            """),
            params,
        ).mappings().all()
        per_camera = [dict(r) for r in rows]
        out["per_camera_rank"] = per_camera
        out["cameras_seen"] = len(per_camera)
        out["total_buckets"] = sum(r["bucket_count"] for r in per_camera)

        # ---- coverage_per_camera ----
        # Buckets are 15-minute windows; 4 per hour. Hours_with_data is
        # bucket_count / 4 rounded.
        coverage = [
            {
                "camera_id": r["camera_id"],
                "buckets": r["bucket_count"],
                "hours_with_data": round(r["bucket_count"] / 4.0, 1),
            }
            for r in per_camera
        ]
        out["coverage_per_camera"] = coverage

        # ---- notable events ----
        rows = conn.execute(
            sa_text("""
                SELECT event_id, event_type, event_source, camera_id,
                       severity, confidence, event_time_utc, title, description
                  FROM panoptic_events
                 WHERE serial_number = :sn
                   AND event_time_utc >= :ws
                   AND event_time_utc <  :we
                 ORDER BY severity DESC NULLS LAST,
                          confidence DESC NULLS LAST,
                          event_time_utc DESC
                 LIMIT :lim
            """),
            {**params, "lim": _NOTABLE_EVENTS_LIMIT},
        ).mappings().all()
        out["notable_events"] = [dict(r) for r in rows]

    return out
