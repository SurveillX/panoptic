"""
Panoptic Search API — FastAPI application.

Endpoints:
  POST /v1/search — search summaries, images, and events
  GET  /health    — health check

See /home/bryan/.claude/plans/quirky-honking-peacock.md for the design.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shared.clients.embedding import EmbeddingClient
from shared.clients.vlm import VLMClient, get_vlm_client

from .executor import execute_search
from .period_summary import run_period_summary
from .schemas import PeriodSummarizeRequest, SearchRequest, VerifyRequest
from .verify import run_verification

log = logging.getLogger(__name__)


def create_app(
    engine,
    embedder: EmbeddingClient | None = None,
    vlm: VLMClient | None = None,
) -> FastAPI:
    app = FastAPI(title="Panoptic Search API", version="1.0")
    _embedder = embedder or EmbeddingClient()
    _vlm = vlm or get_vlm_client()

    @app.get("/health")
    def health():
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
            response = execute_search(req, engine, _embedder)
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

    return app
