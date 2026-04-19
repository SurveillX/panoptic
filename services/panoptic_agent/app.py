"""
Panoptic Agent — FastAPI app factory.

One endpoint (`POST /v1/agent/ask`) + health. Every request runs a
prompt-driven tool-use loop against the local vLLM and returns a
structured answer with citations + trace.

Rate limiting: in-memory token-bucket, soft cap per minute. Internal
service — no per-user auth.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .agent import (
    AGENT_BACKEND,
    AGENT_MODEL,
    AGENT_VLLM_BASE_URL,
    run_agent,
)
from .client import SEARCH_API_URL, SearchAPIClient

log = logging.getLogger(__name__)


AGENT_ASK_RATE_PER_MIN: int = int(os.environ.get("AGENT_ASK_RATE_PER_MIN", "30"))

# One structured JSONL line per /v1/agent/ask so any run is replayable.
_AGENT_AUDIT_LOG = logging.getLogger("panoptic_agent.audit")


def _log_ask_audit(*, question: str, scope: dict | None, response: dict) -> None:
    trace = response.get("trace") or {}
    answer = response.get("answer") or {}
    citations = response.get("citations") or []
    record = {
        "ts":              datetime.now(timezone.utc).isoformat(),
        "question":        question,
        "scope":           scope or {},
        "backend":         trace.get("backend"),
        "model":           trace.get("model"),
        "iterations":      trace.get("iterations"),
        "tool_call_count": trace.get("tool_call_count"),
        "tool_names":      [tc.get("name") for tc in (trace.get("tool_calls") or [])],
        "citations_count": len(citations),
        "unverified":      len(trace.get("unverified_citations") or []),
        "tokens_in":       trace.get("total_prompt_tokens"),
        "tokens_out":      trace.get("total_completion_tokens"),
        "latency_ms":      trace.get("total_latency_ms"),
        "parse_failures":  trace.get("parse_failures"),
        "stop_reason":     trace.get("stop_reason"),
        "narrative":       (answer.get("narrative") or "")[:400],
    }
    _AGENT_AUDIT_LOG.info(json.dumps(record, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AskScope(BaseModel):
    serial_number: str | None = None
    date: str | None = None
    camera_ids: list[str] | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    scope: AskScope | None = None


# ---------------------------------------------------------------------------
# Soft rate limiter (in-memory, per-process)
# ---------------------------------------------------------------------------


class _SlidingWindowLimiter:
    def __init__(self, max_per_min: int) -> None:
        self._max = max_per_min
        self._window: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            while self._window and self._window[0] < cutoff:
                self._window.popleft()
            if len(self._window) >= self._max:
                return False
            self._window.append(now)
            return True


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="Panoptic Agent", version="1.0")
    http_client = httpx.Client(timeout=120.0)
    search_api_client = SearchAPIClient()
    limiter = _SlidingWindowLimiter(AGENT_ASK_RATE_PER_MIN)

    @app.get("/healthz")
    def healthz():
        status = "ok"
        search_reachable = False
        vllm_reachable = False
        detail: dict = {
            "service": "panoptic_agent",
            "backend": AGENT_BACKEND,
            "model": AGENT_MODEL,
            "vllm_url": AGENT_VLLM_BASE_URL,
            "search_api_url": SEARCH_API_URL,
        }
        try:
            up = search_api_client.health()
            search_reachable = up.get("status") == "ok"
            detail["search_api_status"] = up.get("status")
        except Exception as exc:
            detail["search_api_error"] = str(exc)[:200]

        try:
            r = http_client.get(f"{AGENT_VLLM_BASE_URL}/v1/models", timeout=5.0)
            r.raise_for_status()
            vllm_reachable = True
            # Include the served model list for operator visibility.
            data = (r.json() or {}).get("data") or []
            detail["vllm_served_models"] = [m.get("id") for m in data][:5]
        except Exception as exc:
            detail["vllm_error"] = str(exc)[:200]

        if not (search_reachable and vllm_reachable):
            status = "error"
        detail["status"] = status
        detail["search_api_reachable"] = search_reachable
        detail["vllm_reachable"] = vllm_reachable
        http_code = 200 if status == "ok" else 503
        return JSONResponse(detail, status_code=http_code)

    @app.post("/v1/agent/ask")
    def agent_ask(request: Request, body: dict):
        try:
            req = AskRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": exc.errors()},
            )

        if not limiter.allow():
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate limited",
                    "detail": f"max {AGENT_ASK_RATE_PER_MIN} /v1/agent/ask per minute",
                },
            )

        try:
            scope_dict = req.scope.model_dump() if req.scope else None
            response = run_agent(
                http_client=http_client,
                search_api_client=search_api_client,
                question=req.question,
                scope=scope_dict,
            )
        except httpx.HTTPError as exc:
            log.exception("agent: upstream network error")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "upstream unreachable",
                    "detail": str(exc)[:300],
                },
            )
        except Exception as exc:
            log.exception("agent: run_agent failed")
            return JSONResponse(
                status_code=500,
                content={"error": "agent failed", "detail": str(exc)[:500]},
            )

        # Structured audit log for replay/eval — one JSONL line per ask.
        try:
            _log_ask_audit(
                question=req.question,
                scope=scope_dict,
                response=response,
            )
        except Exception:
            log.exception("agent: audit log write failed")

        return response

    return app
