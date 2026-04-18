"""
Trailer webhook receiver — FastAPI application.

Endpoints:
  POST /v1/trailer/bucket-notification — receive bucket data from trailers
  POST /v1/trailer/image               — receive pushed images from trailers
  GET  /health                         — health check

Bucket dedup: Redis SETNX on panoptic:webhook:seen:{event_id} with 24h TTL.
Image dedup:  Postgres panoptic_images.image_id PK (deterministic SHA256).
Aggregation: fragments stored in Redis hashes; background finalizer
runs every 10s and calls ingest_bucket() when a hash goes quiet.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import redis
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image as PILImage
from pydantic import ValidationError
from sqlalchemy import text as sa_text

from shared.auth.hmac_auth import AUTH_ENABLED, ReplayCache, TrailerRegistry
from shared.clients.trailer_intake import (
    FINALIZE_QUIET_SECONDS,
    scan_and_finalize,
    store_fragment,
)
from shared.health.state import HealthState
from shared.schemas.image import TrailerImageMetadata, generate_image_id
from shared.schemas.job import make_event_produce_image_key, make_image_caption_key
from shared.schemas.trailer_webhook import TrailerBucketPayload
from shared.utils.streams import enqueue_job

from services.trailer_webhook.middleware import TrailerAuthMiddleware

log = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS = 86400  # 24 hours
_FINALIZER_INTERVAL_SECONDS = 10

# Image ingest configuration
IMAGE_STORAGE_ROOT: str = os.environ.get("IMAGE_STORAGE_ROOT", "/data/panoptic/images")
IMAGE_MAX_SIZE_BYTES: int = int(os.environ.get("IMAGE_MAX_SIZE_BYTES", "2097152"))


def create_app(
    engine,
    r: redis.Redis,
    model_profile: str = "default",
    prompt_version: str = "v1",
    health_state: HealthState | None = None,
    database_url: str | None = None,
    redis_url: str | None = None,
) -> FastAPI:
    """Create and return the FastAPI app with background finalizer + HMAC auth."""

    app = FastAPI(title="Panoptic Trailer Webhook", version="1.0")
    _finalizer_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # HMAC auth middleware — only attached if auth is enabled.
    # In dev-mode-disabled mode, the middleware is skipped entirely so the
    # startup warn-loop is the only signal.
    # ------------------------------------------------------------------
    if AUTH_ENABLED:
        if database_url is None or redis_url is None:
            raise RuntimeError(
                "create_app requires database_url and redis_url when auth is enabled"
            )
        registry = TrailerRegistry(database_url=database_url)
        try:
            registry.force_refresh()
        except Exception as exc:
            log.warning("initial trailer registry load failed (will retry): %s", exc)
        replay = ReplayCache(redis_url=redis_url)
        app.add_middleware(
            TrailerAuthMiddleware,
            registry=registry,
            replay=replay,
        )

    # ------------------------------------------------------------------
    # Background finalizer
    # ------------------------------------------------------------------

    async def _finalizer_loop():
        """Periodically scan and finalize quiet aggregation hashes."""
        while True:
            try:
                count = scan_and_finalize(
                    r, engine,
                    model_profile=model_profile,
                    prompt_version=prompt_version,
                )
                if count > 0:
                    log.info("finalizer: finalized %d bucket(s)", count)
            except Exception as exc:
                log.error("finalizer: error: %s", exc)
            await asyncio.sleep(_FINALIZER_INTERVAL_SECONDS)

    @app.on_event("startup")
    async def _start_finalizer():
        nonlocal _finalizer_task
        _finalizer_task = asyncio.create_task(_finalizer_loop())
        log.info(
            "finalizer started: interval=%ds quiet=%ds",
            _FINALIZER_INTERVAL_SECONDS, FINALIZE_QUIET_SECONDS,
        )

    @app.on_event("shutdown")
    async def _stop_finalizer():
        if _finalizer_task:
            _finalizer_task.cancel()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health():
        if health_state is not None:
            snap = health_state.snapshot()
            http_code = 503 if snap.get("status") == "error" else 200
            return JSONResponse(snap, status_code=http_code)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Webhook endpoint
    # ------------------------------------------------------------------

    @app.post("/v1/trailer/bucket-notification")
    async def receive_bucket(request: Request):
        # Parse body
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid JSON"},
            )

        # Validate
        try:
            payload = TrailerBucketPayload.model_validate(body)
        except ValidationError as exc:
            # Log field-by-field so we can see which keys are failing in prod.
            field_errs = [
                {"loc": ".".join(str(p) for p in e.get("loc", ())),
                 "type": e.get("type"), "msg": e.get("msg"),
                 "input_repr": repr(e.get("input"))[:60]}
                for e in exc.errors()
            ]
            log.warning("webhook: bucket validation error n=%d fields=%s",
                        exc.error_count(), field_errs)
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": str(exc)[:500]},
            )

        # Dedup by event_id
        dedup_key = f"panoptic:webhook:seen:{payload.event_id}"
        is_new = r.set(dedup_key, "1", nx=True, ex=_DEDUP_TTL_SECONDS)
        if not is_new:
            log.debug("webhook: duplicate event_id=%s", payload.event_id)
            return {"status": "duplicate", "event_id": payload.event_id}

        # Store fragment
        stored = store_fragment(r, payload)
        if not stored:
            # Late fragment after finalization — already logged by store_fragment
            return {
                "status": "late_fragment_discarded",
                "event_id": payload.event_id,
            }

        log.info(
            "webhook: stored fragment event_id=%s sn=%s cam=%s type=%s",
            payload.event_id, payload.serial_number,
            payload.camera_id, payload.bucket.object_type,
        )

        return {
            "status": "accepted",
            "event_id": payload.event_id,
        }

    # ------------------------------------------------------------------
    # Image ingest endpoint
    # ------------------------------------------------------------------

    @app.post("/v1/trailer/image")
    async def receive_image(
        request: Request,
        metadata: str = Form(...),
        image: UploadFile = File(...),
    ):
        # -- Parse metadata --
        try:
            meta_dict = json.loads(metadata)
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid JSON in metadata field"},
            )

        try:
            meta = TrailerImageMetadata.model_validate(meta_dict)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "metadata validation failed", "detail": str(exc)[:500]},
            )

        # -- Validate image --
        if image.content_type not in ("image/jpeg", "image/jpg"):
            return JSONResponse(
                status_code=400,
                content={"error": "image must be JPEG"},
            )

        image_bytes = await image.read()

        if not image_bytes:
            return JSONResponse(
                status_code=400,
                content={"error": "empty image"},
            )

        if len(image_bytes) > IMAGE_MAX_SIZE_BYTES:
            return JSONResponse(
                status_code=400,
                content={"error": f"image exceeds {IMAGE_MAX_SIZE_BYTES} byte limit"},
            )

        # -- Compute deterministic image_id --
        bucket_start_iso = meta.bucket_start.isoformat()
        bucket_end_iso = meta.bucket_end.isoformat()
        image_id = generate_image_id(
            serial_number=meta.serial_number,
            camera_id=meta.camera_id,
            bucket_start=bucket_start_iso,
            bucket_end=bucket_end_iso,
            trigger=meta.trigger,
            timestamp_ms=meta.timestamp_ms,
        )

        # -- Inspect dimensions (best-effort) --
        width = None
        height = None
        try:
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
        except Exception:
            pass

        # -- Compute storage path --
        # Use captured_at_utc if present, else bucket_start for date partitioning
        date_ref = meta.captured_at_utc if meta.captured_at_utc is not None else meta.bucket_start
        storage_dir = os.path.join(
            IMAGE_STORAGE_ROOT,
            meta.serial_number,
            meta.camera_id,
            date_ref.strftime("%Y"),
            date_ref.strftime("%m"),
            date_ref.strftime("%d"),
        )
        storage_path = os.path.join(storage_dir, f"{image_id}.jpg")

        scope_id = f"{meta.serial_number}:{meta.camera_id}"
        size_bytes = len(image_bytes)

        # -- DB-first: attempt INSERT (authoritative dedup) --
        with engine.connect() as conn:
            result = conn.execute(
                sa_text("""
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
                        :trigger, :selection_policy_version,
                        :context_json,
                        :storage_path, 'image/jpeg',
                        :width, :height, :size_bytes,
                        'pending', 'pending',
                        'trailer_push', true,
                        now(), now()
                    )
                    ON CONFLICT (image_id) DO NOTHING
                    RETURNING image_id
                """),
                {
                    "image_id": image_id,
                    "event_id": meta.event_id,
                    "serial_number": meta.serial_number,
                    "camera_id": meta.camera_id,
                    "scope_id": scope_id,
                    "bucket_start_utc": meta.bucket_start,
                    "bucket_end_utc": meta.bucket_end,
                    "captured_at_utc": meta.captured_at_utc,
                    "timestamp_ms": meta.timestamp_ms,
                    "trigger": meta.trigger,
                    "selection_policy_version": meta.selection_policy_version,
                    "context_json": json.dumps(meta.context.model_dump()),
                    "storage_path": storage_path,
                    "width": width,
                    "height": height,
                    "size_bytes": size_bytes,
                },
            )
            inserted_row = result.fetchone()

            if inserted_row is None:
                # Duplicate — do not mutate anything.
                conn.rollback()
                log.debug("image: duplicate image_id=%s", image_id)
                return {"status": "duplicate", "image_id": image_id}

            # -- Write JPEG to disk (after successful INSERT) --
            try:
                os.makedirs(storage_dir, exist_ok=True)
                with open(storage_path, "wb") as f:
                    f.write(image_bytes)
            except Exception as exc:
                log.error(
                    "image: file write failed image_id=%s path=%s: %s",
                    image_id, storage_path, exc,
                )
                # Cleanup: delete the just-inserted row.
                conn.execute(
                    sa_text("DELETE FROM panoptic_images WHERE image_id = :image_id"),
                    {"image_id": image_id},
                )
                conn.commit()
                return JSONResponse(
                    status_code=500,
                    content={"error": "failed to store image file"},
                )

            # -- Create image_caption job --
            job_key = make_image_caption_key(image_id)
            job_id_result = conn.execute(
                sa_text("""
                    INSERT INTO panoptic_jobs (
                        job_key, serial_number, job_type, payload
                    ) VALUES (
                        :job_key, :serial_number, 'image_caption',
                        CAST(:payload AS jsonb)
                    )
                    ON CONFLICT (job_key) DO NOTHING
                    RETURNING job_id
                """),
                {
                    "job_key": job_key,
                    "serial_number": meta.serial_number,
                    "payload": json.dumps({
                        "image_id": image_id,
                        "serial_number": meta.serial_number,
                    }),
                },
            )
            job_row = job_id_result.fetchone()

            # -- Create event_produce job for alert/anomaly images --
            # (baseline images do not produce events)
            event_job_row = None
            if meta.trigger in ("alert", "anomaly"):
                event_job_result = conn.execute(
                    sa_text("""
                        INSERT INTO panoptic_jobs (
                            job_key, serial_number, job_type, payload
                        ) VALUES (
                            :job_key, :serial_number, 'event_produce',
                            CAST(:payload AS jsonb)
                        )
                        ON CONFLICT (job_key) DO NOTHING
                        RETURNING job_id
                    """),
                    {
                        "job_key": make_event_produce_image_key(image_id),
                        "serial_number": meta.serial_number,
                        "payload": json.dumps({
                            "source_type": "image",
                            "image_id": image_id,
                        }),
                    },
                )
                event_job_row = event_job_result.fetchone()

            conn.commit()

        # -- Post-commit: enqueue to Redis stream --
        if job_row is not None:
            try:
                enqueue_job(
                    r,
                    job_type="image_caption",
                    job_id=str(job_row.job_id),
                    serial_number=meta.serial_number,
                )
            except Exception as exc:
                log.error(
                    "image: Redis enqueue failed image_id=%s: %s — "
                    "job exists in Postgres, reclaimer will pick it up",
                    image_id, exc,
                )

        if event_job_row is not None:
            try:
                enqueue_job(
                    r,
                    job_type="event_produce",
                    job_id=str(event_job_row.job_id),
                    serial_number=meta.serial_number,
                )
            except Exception as exc:
                log.error(
                    "image: event_produce enqueue failed image_id=%s: %s — "
                    "job exists in Postgres, reclaimer will pick it up",
                    image_id, exc,
                )

        log.info(
            "image: accepted image_id=%s sn=%s cam=%s trigger=%s",
            image_id, meta.serial_number, meta.camera_id, meta.trigger,
        )

        return {"status": "accepted", "image_id": image_id}

    return app
