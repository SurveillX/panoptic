"""
M10 — evidence detail endpoints.

Endpoints:
  GET /v1/events/{event_id}    — full panoptic_events row (JSON)
  GET /v1/summaries/{summary_id} — full panoptic_summaries row (JSON)
  GET /v1/images/{image_id}    — full panoptic_images row (JSON)
  GET /v1/images/{image_id}.jpg — stream the image bytes (FileResponse)

Access model — internal-only:
  The /v1/images/{id}.jpg endpoint has NO cited-id check (unlike the M9
  report-asset endpoint). Anyone who can reach :8600 can fetch any
  image. This is the same trust boundary as /v1/search et al. The
  endpoint must not be publicly tunneled without landing an auth
  layer first (see docs/OPERATOR_UI.md §Access).

All detail endpoints return 404 (not 403) for unknown IDs to avoid
enumeration side-channels.
"""

from __future__ import annotations

import logging
import os

from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text as sa_text

from .schemas import (
    EventDetailResponse,
    ImageDetailResponse,
    SummaryDetailResponse,
)

log = logging.getLogger(__name__)


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


# ---------------------------------------------------------------------------
# GET /v1/events/{event_id}
# ---------------------------------------------------------------------------


def get_event_detail(engine, event_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT event_id, serial_number, camera_id, scope_id,
                       event_type, event_source,
                       severity, confidence,
                       start_time_utc, end_time_utc, event_time_utc,
                       bucket_id, image_id,
                       title, description, metadata_json,
                       created_at, updated_at
                  FROM panoptic_events
                 WHERE event_id = :eid
            """),
            {"eid": event_id},
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "event not found"},
        )

    resp = EventDetailResponse(
        event_id=row.event_id,
        serial_number=row.serial_number,
        camera_id=row.camera_id,
        scope_id=row.scope_id,
        event_type=row.event_type,
        event_source=row.event_source,
        severity=row.severity,
        confidence=row.confidence,
        start_time_utc=_iso(row.start_time_utc),
        end_time_utc=_iso(row.end_time_utc),
        event_time_utc=_iso(row.event_time_utc),
        bucket_id=row.bucket_id,
        image_id=row.image_id,
        title=row.title,
        description=row.description,
        metadata_json=row.metadata_json or {},
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )
    return resp.model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /v1/summaries/{summary_id}
# ---------------------------------------------------------------------------


def _normalize_event_labels(key_events) -> list[str]:
    """Extract label strings from panoptic_summaries.key_events JSONB array."""
    if not key_events:
        return []
    out: list[str] = []
    for e in key_events:
        if isinstance(e, dict):
            lbl = e.get("label")
            if lbl:
                out.append(str(lbl))
        elif isinstance(e, str):
            out.append(e)
    return out


def get_summary_detail(engine, summary_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT summary_id, serial_number, level, scope_id,
                       start_time, end_time, summary, key_events,
                       metrics, coverage, summary_mode, frames_used,
                       confidence, model_profile, prompt_version,
                       is_latest, created_at
                  FROM panoptic_summaries
                 WHERE summary_id = :sid
            """),
            {"sid": summary_id},
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "summary not found"},
        )

    resp = SummaryDetailResponse(
        summary_id=row.summary_id,
        serial_number=row.serial_number,
        level=row.level,
        scope_id=row.scope_id,
        start_time=_iso(row.start_time),
        end_time=_iso(row.end_time),
        summary=row.summary or "",
        key_events_labels=_normalize_event_labels(row.key_events),
        metrics=row.metrics or {},
        coverage=row.coverage or {},
        summary_mode=row.summary_mode,
        frames_used=row.frames_used,
        confidence=row.confidence,
        model_profile=row.model_profile,
        prompt_version=row.prompt_version,
        is_latest=row.is_latest,
        created_at=_iso(row.created_at),
    )
    return resp.model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /v1/images/{image_id}  — metadata only
# ---------------------------------------------------------------------------


def get_image_detail(engine, image_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT image_id, serial_number, camera_id, scope_id, trigger,
                       bucket_start_utc, bucket_end_utc, captured_at_utc,
                       caption_text, caption_status, storage_path,
                       width, height, size_bytes, created_at
                  FROM panoptic_images
                 WHERE image_id = :iid
            """),
            {"iid": image_id},
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "image not found"},
        )

    resp = ImageDetailResponse(
        image_id=row.image_id,
        serial_number=row.serial_number,
        camera_id=row.camera_id,
        scope_id=row.scope_id,
        trigger=row.trigger,
        bucket_start_utc=_iso(row.bucket_start_utc),
        bucket_end_utc=_iso(row.bucket_end_utc),
        captured_at_utc=_iso(row.captured_at_utc),
        caption_text=row.caption_text,
        caption_status=row.caption_status,
        storage_path=row.storage_path,
        width=row.width,
        height=row.height,
        size_bytes=row.size_bytes,
        created_at=_iso(row.created_at),
    )
    return resp.model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /v1/images/{image_id}.jpg — internal JPEG stream
# ---------------------------------------------------------------------------


def get_image_asset(engine, image_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("""
                SELECT storage_path
                  FROM panoptic_images
                 WHERE image_id = :iid
            """),
            {"iid": image_id},
        ).fetchone()

    if row is None or not row.storage_path:
        return JSONResponse(
            status_code=404,
            content={"error": "image not found"},
        )

    storage_path = row.storage_path
    if not os.path.exists(storage_path):
        log.warning(
            "details: asset missing on disk image_id=%s path=%s",
            image_id, storage_path,
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
