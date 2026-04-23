"""
M14 — on-demand continuum pull.

POST /v1/search/pull_frame wraps `shared.clients.continuum.ContinuumClient`
and persists the fetched JPEG as a first-class `panoptic_images` row.
Pulled frames use `source='on_demand_pull'`, `trigger='pulled'`, and
`is_searchable=false` so they don't pollute general retrieval but are
still citable by event_id-like semantics (image_id). Caption + embedding
workers pick them up automatically via the existing pipeline.

No event row is produced — 'pulled' isn't in
`shared.events.build._TRIGGER_TO_EVENT_TYPE`, so the event producer
skips these images naturally. This is the right semantics: a pulled
frame is evidence, not a trigger.
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock

from PIL import Image as PILImage
from sqlalchemy import text

from shared.clients.continuum import (
    ContinuumAuthError,
    ContinuumClient,
    ContinuumNetworkError,
    get_continuum_client,
)
from shared.schemas.image import generate_image_id
from shared.schemas.job import make_image_caption_key
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import enqueue_job

from .schemas import PullFrameRequest, PullFrameResponse


log = logging.getLogger(__name__)


IMAGE_STORAGE_ROOT: str = os.environ.get(
    "IMAGE_STORAGE_ROOT", "/data/panoptic-store/images"
)
BUCKET_MINUTES: int = 15

# Server-side rate limit — 10 pulls/minute across the process. Protects
# trailer bandwidth + caption worker backlog from a runaway agent.
_RATE_LIMIT_WINDOW_SEC: float = 60.0
_RATE_LIMIT_MAX: int = 10

_rate_lock = Lock()
_rate_history: deque[float] = deque()


class PullFrameError(Exception):
    """Wrapper raised to the FastAPI handler with an HTTP status hint."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def run_pull_frame(
    req: PullFrameRequest,
    engine,
    *,
    continuum_client: ContinuumClient | None = None,
    redis_client=None,
    now_fn=None,
) -> PullFrameResponse:
    """
    Main entry. Fetches a frame, persists (or detects duplicate), enqueues
    caption/embedding jobs, returns a PullFrameResponse.

    Raises PullFrameError with status_code mapped to an HTTP response.
    """

    _rate_limit_check(now_fn=now_fn)

    client = continuum_client or get_continuum_client()
    target_ts = _to_utc(req.timestamp_utc)
    bucket_start, bucket_end = _bucket_bounds(target_ts)
    timestamp_ms = int(target_ts.timestamp() * 1000)

    image_id = generate_image_id(
        serial_number=req.serial_number,
        camera_id=req.camera_id,
        bucket_start=bucket_start.isoformat(),
        bucket_end=bucket_end.isoformat(),
        trigger="pulled",
        timestamp_ms=timestamp_ms,
    )

    # Short-circuit if this (serial, camera, bucket, ts) pair already
    # landed as a pulled frame. Saves a trailer round-trip.
    existing = _load_existing(engine, image_id)
    if existing is not None:
        log.debug("pull_frame: already_exists image_id=%s", image_id)
        return PullFrameResponse(
            image_id=image_id,
            status="already_exists",
            storage_path=existing["storage_path"],
            caption_status=existing["caption_status"],
            bucket_start_utc=bucket_start,
            bucket_end_utc=bucket_end,
        )

    # Fetch from the trailer.
    try:
        frame = client.fetch_frame(
            req.serial_number,
            req.camera_id,
            target_ts,
            width=req.width,
            quality=req.quality,
            accurate=True,
        )
    except ContinuumAuthError as exc:
        raise PullFrameError(403, f"trailer auth error: {exc}") from exc
    except ContinuumNetworkError as exc:
        raise PullFrameError(502, f"trailer unreachable: {exc}") from exc

    if frame is None:
        raise PullFrameError(
            404,
            f"no recording at {target_ts.isoformat()} for "
            f"{req.serial_number}/{req.camera_id}",
        )

    width, height = _inspect_dimensions(frame.jpeg_bytes)
    storage_dir = os.path.join(
        IMAGE_STORAGE_ROOT,
        req.serial_number,
        req.camera_id,
        target_ts.strftime("%Y"),
        target_ts.strftime("%m"),
        target_ts.strftime("%d"),
    )
    storage_path = os.path.join(storage_dir, f"{image_id}.jpg")

    scope_id = f"{req.serial_number}:{req.camera_id}"
    event_id_value = f"pulled:{req.serial_number}:{req.camera_id}:{timestamp_ms}"
    context_json = {
        "reason":               req.reason,
        "continuum_target_ts":  target_ts.isoformat(),
        "requested_width":      req.width,
        "requested_quality":    req.quality,
    }

    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO panoptic_images (
                    image_id, event_id,
                    serial_number, camera_id, scope_id,
                    bucket_start_utc, bucket_end_utc,
                    captured_at_utc, timestamp_ms,
                    trigger, selection_policy_version,
                    context_json,
                    storage_path, content_type,
                    width, height, size_bytes,
                    caption_status, caption_embedding_status,
                    source, is_searchable,
                    created_at, updated_at
                ) VALUES (
                    :image_id, :event_id,
                    :serial_number, :camera_id, :scope_id,
                    :bucket_start_utc, :bucket_end_utc,
                    :captured_at_utc, :timestamp_ms,
                    'pulled', '1',
                    CAST(:context_json AS jsonb),
                    :storage_path, 'image/jpeg',
                    :width, :height, :size_bytes,
                    'pending', 'pending',
                    'on_demand_pull', false,
                    now(), now()
                )
                ON CONFLICT (image_id) DO NOTHING
                RETURNING image_id
                """
            ),
            {
                "image_id":         image_id,
                "event_id":         event_id_value,
                "serial_number":    req.serial_number,
                "camera_id":        req.camera_id,
                "scope_id":         scope_id,
                "bucket_start_utc": bucket_start,
                "bucket_end_utc":   bucket_end,
                "captured_at_utc":  target_ts,
                "timestamp_ms":     timestamp_ms,
                "context_json":     json.dumps(context_json),
                "storage_path":     storage_path,
                "width":            width,
                "height":           height,
                "size_bytes":       len(frame.jpeg_bytes),
            },
        )
        inserted = result.fetchone()

        if inserted is None:
            # Lost a race (another request inserted between our _load_existing
            # and this INSERT). Treat as already_exists — the other writer
            # has the canonical file on disk.
            conn.rollback()
            existing = _load_existing(engine, image_id)
            if existing is None:
                # Extremely unlikely; surface as a 500 so it gets noticed.
                raise PullFrameError(
                    500,
                    f"concurrent insert collision but no row visible: {image_id}",
                )
            return PullFrameResponse(
                image_id=image_id,
                status="already_exists",
                storage_path=existing["storage_path"],
                caption_status=existing["caption_status"],
                bucket_start_utc=bucket_start,
                bucket_end_utc=bucket_end,
            )

        # File-write post-INSERT; cleanup row on failure so we don't leave
        # an orphan DB record pointing to a missing file.
        try:
            os.makedirs(storage_dir, exist_ok=True)
            with open(storage_path, "wb") as f:
                f.write(frame.jpeg_bytes)
        except Exception as exc:
            log.error(
                "pull_frame: file write failed image_id=%s path=%s: %s",
                image_id, storage_path, exc,
            )
            conn.execute(
                text("DELETE FROM panoptic_images WHERE image_id = :image_id"),
                {"image_id": image_id},
            )
            conn.commit()
            raise PullFrameError(500, "failed to store image file") from exc

        # Enqueue caption job — same shape as trailer_webhook. Embedding
        # is downstream of caption (caption worker emits caption_embed).
        job_key = make_image_caption_key(image_id)
        caption_job_id = conn.execute(
            text(
                """
                INSERT INTO panoptic_jobs (
                    job_key, serial_number, job_type, payload
                ) VALUES (
                    :job_key, :serial_number, 'image_caption',
                    CAST(:payload AS jsonb)
                )
                ON CONFLICT (job_key) DO NOTHING
                RETURNING job_id
                """
            ),
            {
                "job_key":       job_key,
                "serial_number": req.serial_number,
                "payload":       json.dumps({
                    "image_id":      image_id,
                    "serial_number": req.serial_number,
                }),
            },
        ).fetchone()

        conn.commit()

    # Post-commit: enqueue to Redis stream. If Redis is down, reclaimer
    # picks the job up from postgres eventually.
    if caption_job_id is not None:
        r = redis_client or get_redis_client()
        try:
            enqueue_job(
                r,
                job_type="image_caption",
                job_id=str(caption_job_id.job_id),
                serial_number=req.serial_number,
            )
        except Exception as exc:
            log.error(
                "pull_frame: redis enqueue failed image_id=%s: %s — "
                "job exists in postgres, reclaimer will pick it up",
                image_id, exc,
            )

    return PullFrameResponse(
        image_id=image_id,
        status="created",
        storage_path=storage_path,
        caption_status="pending",
        bucket_start_utc=bucket_start,
        bucket_end_utc=bucket_end,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _bucket_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """Snap to the surrounding 15-min window boundaries."""
    minute = (ts.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    start = ts.replace(minute=minute, second=0, microsecond=0)
    return start, start + timedelta(minutes=BUCKET_MINUTES)


def _inspect_dimensions(jpeg_bytes: bytes) -> tuple[int | None, int | None]:
    try:
        with PILImage.open(io.BytesIO(jpeg_bytes)) as img:
            return img.size
    except Exception:
        return None, None


def _load_existing(engine, image_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT storage_path, caption_status FROM panoptic_images "
                "WHERE image_id = :image_id"
            ),
            {"image_id": image_id},
        ).fetchone()
    if row is None:
        return None
    return {
        "storage_path":   row.storage_path,
        "caption_status": row.caption_status,
    }


def _rate_limit_check(*, now_fn=None) -> None:
    now = (now_fn or _now_monotonic)()
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    with _rate_lock:
        while _rate_history and _rate_history[0] < cutoff:
            _rate_history.popleft()
        if len(_rate_history) >= _RATE_LIMIT_MAX:
            raise PullFrameError(
                429,
                f"pull_frame rate limit exceeded "
                f"({_RATE_LIMIT_MAX}/{int(_RATE_LIMIT_WINDOW_SEC)}s)",
            )
        _rate_history.append(now)


def _now_monotonic() -> float:
    import time
    return time.monotonic()


def reset_rate_limit_for_tests() -> None:
    """Exported test helper — clears the in-memory rate window."""
    with _rate_lock:
        _rate_history.clear()
