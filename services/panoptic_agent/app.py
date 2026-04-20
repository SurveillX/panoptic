"""
Panoptic Agent — FastAPI app factory.

Endpoints:
  POST /v1/agent/ask       — run one tool-use loop turn
  GET  /v1/agent/backends  — list registered backends + availability
  GET  /healthz            — service + dependency status

The multi-backend wiring (M11.1) lives here:
  - `build_registry()` from services.panoptic_agent.backends constructs
    a `BackendRegistry` at startup, honoring AGENT_BACKENDS_ENABLED and
    probing each backend's availability.
  - Request may specify a `backend` field; unknown / unavailable →
    400 with a readable reason. No silent fallback.

Rate limiting: in-memory sliding window, soft cap per minute. Internal
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

from .agent import run_agent
from .backends import build_registry
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
        "ts":                 datetime.now(timezone.utc).isoformat(),
        "question":           question,
        "scope":              scope or {},
        "backend":            trace.get("backend"),
        "provider":           trace.get("provider"),
        "model":              trace.get("model"),
        "iterations":         trace.get("iterations"),
        "tool_call_count":    trace.get("tool_call_count"),
        "tool_names":         [tc.get("name") for tc in (trace.get("tool_calls") or [])],
        "citations_count":    len(citations),
        "unverified":         len(trace.get("unverified_citations") or []),
        "tokens_in":          trace.get("total_prompt_tokens"),
        "tokens_out":         trace.get("total_completion_tokens"),
        "latency_ms":         trace.get("total_latency_ms"),
        "backend_latency_ms": trace.get("backend_latency_ms"),
        "parse_failures":     trace.get("parse_failures"),
        "stop_reason":        trace.get("stop_reason"),
        "estimated_cost_usd": trace.get("estimated_cost_usd"),
        "narrative":          (answer.get("narrative") or "")[:400],
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
    # New in M11.1: optional request-facing backend key. Null → use
    # the registry's resolved default.
    backend: str | None = None


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
    search_api_client = SearchAPIClient()
    limiter = _SlidingWindowLimiter(AGENT_ASK_RATE_PER_MIN)
    registry = build_registry()

    @app.get("/healthz")
    def healthz():
        # Search API + at-least-one-backend-available.
        search_reachable = False
        detail: dict = {
            "service":        "panoptic_agent",
            "search_api_url": SEARCH_API_URL,
            "default_backend": registry.default,
            "backends":        registry.list_public(),
        }
        try:
            up = search_api_client.health()
            search_reachable = up.get("status") == "ok"
            detail["search_api_status"] = up.get("status")
        except Exception as exc:
            detail["search_api_error"] = str(exc)[:200]

        any_backend_available = any(b.is_available for b in registry.backends.values())
        status = "ok" if (search_reachable and any_backend_available) else "error"
        detail["status"] = status
        detail["search_api_reachable"] = search_reachable
        detail["any_backend_available"] = any_backend_available
        http_code = 200 if status == "ok" else 503
        return JSONResponse(detail, status_code=http_code)

    @app.get("/v1/agent/backends")
    def list_backends():
        return {
            "default":  registry.default,
            "backends": registry.list_public(),
        }

    @app.post("/v1/agent/ask")
    def agent_ask(request: Request, body: dict):
        try:
            req = AskRequest.model_validate(body)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "detail": exc.errors()},
            )

        # Backend selection.
        requested = req.backend
        backend = None
        if requested is None:
            backend = registry.get(registry.default)
            if backend is None or not backend.is_available:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "default backend unavailable",
                        "detail": f"default='{registry.default}'; no backend is currently available",
                        "available": [b.name for b in registry.backends.values() if b.is_available],
                    },
                )
        else:
            backend = registry.backends.get(requested)
            if backend is None:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error":   "unknown backend",
                        "detail":  f"requested '{requested}' is not registered",
                        "allowed": list(registry.backends.keys()),
                    },
                )
            if not backend.is_available:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error":              "backend unavailable",
                        "detail":             f"backend '{requested}' is registered but not available",
                        "unavailable_reason": backend.unavailable_reason,
                    },
                )

        if not limiter.allow():
            return JSONResponse(
                status_code=429,
                content={
                    "error":  "rate limited",
                    "detail": f"max {AGENT_ASK_RATE_PER_MIN} /v1/agent/ask per minute",
                },
            )

        try:
            scope_dict = req.scope.model_dump() if req.scope else None
            response = run_agent(
                backend=backend,
                search_api_client=search_api_client,
                question=req.question,
                scope=scope_dict,
            )
        except httpx.HTTPError as exc:
            log.exception("agent: upstream network error")
            return JSONResponse(
                status_code=503,
                content={
                    "error":  "upstream unreachable",
                    "detail": str(exc)[:300],
                },
            )
        except Exception as exc:
            log.exception("agent: run_agent failed")
            return JSONResponse(
                status_code=500,
                content={"error": "agent failed", "detail": str(exc)[:500]},
            )

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
