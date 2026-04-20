"""
VLLMBackend — the local Gemma baseline (request-facing key "gemma").

Exact behavior carried over from the original `_call_vllm` path. No
quality or timing change. Consumers migrating from the M11 direct
`_call_vllm` call now go through `Backend.generate()`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

from .base import Backend, StopReason

if TYPE_CHECKING:
    from ..agent import AgentTrace


log = logging.getLogger(__name__)


# Environment — new M11.1 names with M11 fall-through for compatibility.
GEMMA_BASE_URL: str = (
    os.environ.get("AGENT_GEMMA_BASE_URL")
    or os.environ.get("AGENT_VLLM_BASE_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://localhost:8000"
).rstrip("/")

GEMMA_MODEL: str = (
    os.environ.get("AGENT_GEMMA_MODEL")
    or os.environ.get("AGENT_MODEL")
    or "gemma-4-26b-it"
)


class VLLMBackend(Backend):
    name = "gemma"
    provider = "vllm"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self._base_url = (base_url or GEMMA_BASE_URL).rstrip("/")
        self.model = model or GEMMA_MODEL
        self.is_available = False
        self.unavailable_reason: str | None = "unprobed"
        self.probe_latency_ms: int | None = None
        self._client = httpx.Client(timeout=120.0)

    # ------------------------------------------------------------------
    # Backend.probe
    # ------------------------------------------------------------------

    def probe(self) -> tuple[bool, str | None, int | None]:
        t0 = time.perf_counter()
        try:
            r = self._client.get(f"{self._base_url}/v1/models", timeout=5.0)
            r.raise_for_status()
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return True, None, latency_ms
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return False, f"probe_failed: {type(exc).__name__}: {exc!s}"[:200], latency_ms

    # ------------------------------------------------------------------
    # Backend.generate
    # ------------------------------------------------------------------

    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        trace: "AgentTrace",
    ) -> str | None:
        # Identify ourselves in the trace on every call so mid-run
        # backend swaps (future; not v1) still round-trip provenance.
        trace.backend = self.name
        trace.provider = self.provider
        trace.model = self.model

        # OpenAI-compatible request: system message goes inline.
        oai_messages = [{"role": "system", "content": system_prompt}, *messages]

        body = {
            "model":       self.model,
            "messages":    oai_messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }

        t0 = time.perf_counter()
        try:
            resp = self._client.post(
                f"{self._base_url}/v1/chat/completions",
                json=body,
                timeout=float(os.environ.get("AGENT_LLM_TIMEOUT_SEC", "90")),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            body_preview = (exc.response.text or "")[:400] if exc.response is not None else ""
            log.warning("gemma/vllm rejected request (%s): %s", code, body_preview)
            trace.stop_reason = "backend_error"
            trace.backend_error = {
                "code":          code,
                "body_preview":  body_preview,
                "trace_tag":     f"gemma_error_{code}",
            }
            trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)
            return None
        except httpx.HTTPError as exc:
            log.warning("gemma/vllm network error: %s", exc)
            trace.stop_reason = "backend_error"
            trace.backend_error = {
                "code":          0,
                "body_preview":  f"{type(exc).__name__}: {exc!s}"[:400],
                "trace_tag":     "gemma_error_network",
            }
            trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)
            return None

        trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)
        payload = resp.json()

        # Usage.
        usage = payload.get("usage") or {}
        trace.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        trace.total_completion_tokens += int(usage.get("completion_tokens") or 0)
        trace.raw_usage.append({"backend": self.name, "provider": self.provider, **usage})

        # Stop reason.
        finish = (payload.get("choices") or [{}])[0].get("finish_reason")
        trace.stop_reason = _normalize_finish_reason(finish)

        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return content


def _normalize_finish_reason(raw: str | None) -> StopReason:
    if raw == "stop":
        return "end_turn"
    if raw == "length":
        return "max_tokens"
    if raw in ("tool_calls", "function_call"):
        return "tool_pending"
    return "unknown"
