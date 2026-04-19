"""
Panoptic Search API — FastAPI application.

Endpoints:
  POST /v1/search                             — search summaries, images, and events
  POST /v1/search/verify                      — verify hits with VLM citations
  POST /v1/summarize/period                   — multi-camera period summary
  POST /v1/reports/daily                      — enqueue daily report (M9)
  POST /v1/reports/weekly                     — enqueue weekly report (M9)
  GET  /v1/reports/{report_id}                — report status + metadata (M9)
  GET  /v1/reports/{report_id}/assets/{id}.jpg — authorized image asset (M9)
  GET  /health                                — health check

See /home/bryan/.claude/plans/quirky-honking-peacock.md for the original design.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.clients.embedding import EmbeddingClient
from shared.clients.vlm import VLMClient, get_vlm_client
from shared.utils.redis_client import get_redis_client

from .executor import execute_search
from .period_summary import run_period_summary
from .reports import (
    generate_daily_report,
    generate_weekly_report,
    get_report_asset,
    get_report_status,
)
from .schemas import PeriodSummarizeRequest, SearchRequest, VerifyRequest
from .verify import run_verification

log = logging.getLogger(__name__)


def create_app(
    engine,
    embedder: EmbeddingClient | None = None,
    vlm: VLMClient | None = None,
    vl_embedder=None,  # VLEmbeddingClient | None
    health_state=None,
) -> FastAPI:
    app = FastAPI(title="Panoptic Search API", version="1.0")
    _embedder = embedder or EmbeddingClient()
    _vlm = vlm or get_vlm_client()
    if vl_embedder is None:
        from shared.clients.vl_embedding import get_vl_embedding_client
        vl_embedder = get_vl_embedding_client()
    _vl_embedder = vl_embedder
    _redis = get_redis_client()

    @app.get("/health")
    def health():
        if health_state is not None:
            snap = health_state.snapshot()
            http_code = 503 if snap.get("status") == "error" else 200
            return JSONResponse(snap, status_code=http_code)
        return {"status": "ok"}

    @app.post("/v1/search")
    def search(request: Request, body: dict):
        try:
            req = SearchRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": json.loads(exc.json())},
            )

        try:
            response = execute_search(req, engine, _embedder, _vl_embedder)
        except Exception as exc:
            log.exception("search failed")
            return JSONResponse(
                status_code=500,
                content={"error": "search failed", "detail": str(exc)[:500]},
            )

        return response.model_dump()

    @app.post("/v1/summarize/period")
    def summarize_period(request: Request, body: dict):
        try:
            req = PeriodSummarizeRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": json.loads(exc.json())},
            )

        try:
            response = run_period_summary(req, engine, _vlm)
        except Exception as exc:
            log.exception("period summarization failed")
            return JSONResponse(
                status_code=500,
                content={"error": "period summarization failed", "detail": str(exc)[:500]},
            )

        return response.model_dump()

    @app.post("/v1/search/verify")
    def search_verify(request: Request, body: dict):
        try:
            req = VerifyRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": json.loads(exc.json())},
            )

        try:
            response = run_verification(req, engine, _embedder, _vlm)
        except Exception as exc:
            log.exception("verify failed")
            return JSONResponse(
                status_code=500,
                content={"error": "verify failed", "detail": str(exc)[:500]},
            )

        return response.model_dump()

    # ------------------------------------------------------------------
    # M9 — report endpoints
    # ------------------------------------------------------------------

    @app.post("/v1/reports/daily")
    def reports_daily(request: Request, body: dict):
        return generate_daily_report(engine, _redis, body)

    @app.post("/v1/reports/weekly")
    def reports_weekly(request: Request, body: dict):
        return generate_weekly_report(engine, _redis, body)

    @app.get("/v1/reports/{report_id}")
    def reports_status(report_id: str):
        return get_report_status(engine, report_id)

    @app.get("/v1/reports/{report_id}/assets/{image_id}.jpg")
    def reports_asset(report_id: str, image_id: str):
        return get_report_asset(engine, report_id, image_id)

    return app
