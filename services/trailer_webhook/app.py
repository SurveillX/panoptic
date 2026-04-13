"""
Trailer webhook receiver — FastAPI application.

Endpoints:
  POST /v1/trailer/bucket-notification — receive bucket data from trailers
  GET  /health                         — health check

Dedup: Redis SETNX on vil:webhook:seen:{event_id} with 24h TTL.
Aggregation: fragments stored in Redis hashes; background finalizer
runs every 10s and calls ingest_bucket() when a hash goes quiet.
"""

from __future__ import annotations

import asyncio
import logging

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.clients.trailer_intake import (
    FINALIZE_QUIET_SECONDS,
    scan_and_finalize,
    store_fragment,
)
from shared.schemas.trailer_webhook import TrailerBucketPayload

log = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS = 86400  # 24 hours
_FINALIZER_INTERVAL_SECONDS = 10


def create_app(engine, r: redis.Redis, model_profile: str = "default", prompt_version: str = "v1") -> FastAPI:
    """Create and return the FastAPI app with background finalizer."""

    app = FastAPI(title="VIL Trailer Webhook", version="1.0")
    _finalizer_task: asyncio.Task | None = None

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
            log.warning("webhook: validation error: %s", exc.error_count())
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": str(exc)[:500]},
            )

        # Dedup by event_id
        dedup_key = f"vil:webhook:seen:{payload.event_id}"
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

    return app
