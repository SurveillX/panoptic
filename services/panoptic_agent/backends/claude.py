"""
ClaudeBackend — Anthropic Messages API adapter (M11.1b).

Request-facing backend key: "claude".
Provider tag: "anthropic".
Default model: claude-sonnet-4-6 (env-overridable via AGENT_CLAUDE_MODEL).

Silent-skip behavior: if ANTHROPIC_API_KEY is absent at import time,
this module still loads cleanly — the class instantiates fine but
`is_available = False` + `unavailable_reason` describes the gap. The
registry only surfaces it to /ask dispatch when the key is present AND
the startup probe succeeds.

Protocol: prompt-driven, same as gemma and gpt5mini. Claude's system
prompt goes into the `system=` parameter of messages.create() (not a
system-role message); tool_use blocks are NOT used in M11.1 — the
shared agent loop drives tool selection through text JSON so the
benchmark stays apples-to-apples across backends.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from .base import Backend, StopReason

if TYPE_CHECKING:
    from ..agent import AgentTrace


log = logging.getLogger(__name__)


CLAUDE_MODEL: str = os.environ.get("AGENT_CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_TIMEOUT_SEC: float = float(os.environ.get("AGENT_CLAUDE_TIMEOUT_SEC", "60"))


def is_configured() -> bool:
    """Cheap check callers use to decide whether to register this
    backend in the registry at all. We look at env, not a probe."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class ClaudeBackend(Backend):
    name = "claude"
    provider = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or CLAUDE_MODEL
        self.is_available = False
        self.unavailable_reason: str | None = "unprobed"
        self.probe_latency_ms: int | None = None
        self._client = None

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self.unavailable_reason = "ANTHROPIC_API_KEY not set"
            return

        # Import is deferred so the service starts cleanly in test envs
        # where anthropic may not be installed yet.
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as exc:
            self.unavailable_reason = f"anthropic SDK not importable: {exc!s}"
            return

        try:
            self._client = Anthropic(api_key=api_key, timeout=CLAUDE_TIMEOUT_SEC)
        except Exception as exc:
            self.unavailable_reason = f"client init failed: {exc!s}"
            return

    # ------------------------------------------------------------------
    # Backend.probe — cheapest real API call the SDK offers
    # ------------------------------------------------------------------

    def probe(self) -> tuple[bool, str | None, int | None]:
        if self._client is None:
            return False, self.unavailable_reason or "client not initialized", None

        t0 = time.perf_counter()
        try:
            # models.list is a read-only endpoint; counts as ~0 tokens.
            self._client.models.list(limit=1)
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
        trace.backend = self.name
        trace.provider = self.provider
        trace.model = self.model

        if self._client is None:
            trace.stop_reason = "backend_error"
            trace.backend_error = {
                "code":         0,
                "body_preview": self.unavailable_reason or "client not initialized",
                "trace_tag":    "claude_error_unavailable",
            }
            return None

        # Anthropic rejects a leading `system`-role message; strip any
        # that may have slipped in from older callers.
        anthropic_messages = [m for m in messages if m.get("role") != "system"]

        t0 = time.perf_counter()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=anthropic_messages,
            )
        except Exception as exc:
            # Anthropic SDK raises APIStatusError subclasses; catch
            # broadly so future SDK bumps don't break this layer.
            code = getattr(exc, "status_code", 0) or 0
            body_preview = f"{type(exc).__name__}: {exc!s}"[:400]
            log.warning("claude rejected request (%s): %s", code, body_preview)
            trace.stop_reason = "backend_error"
            trace.backend_error = {
                "code":         code,
                "body_preview": body_preview,
                "trace_tag":    f"claude_error_{code or 'unknown'}",
            }
            trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)
            return None

        trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)

        # Usage.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            input_t = int(getattr(usage, "input_tokens", 0) or 0)
            output_t = int(getattr(usage, "output_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            trace.total_prompt_tokens += input_t
            trace.total_completion_tokens += output_t
            trace.raw_usage.append({
                "backend":                     self.name,
                "provider":                    self.provider,
                "input_tokens":                input_t,
                "output_tokens":               output_t,
                "cache_read_input_tokens":     cache_read,
                "cache_creation_input_tokens": cache_create,
            })

        trace.stop_reason = _normalize_stop_reason(getattr(resp, "stop_reason", None))

        # Extract text from the first text content block.
        text_parts: list[str] = []
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
        return "".join(text_parts)


def _normalize_stop_reason(raw: str | None) -> StopReason:
    if raw == "end_turn":
        return "end_turn"
    if raw == "max_tokens":
        return "max_tokens"
    if raw == "stop_sequence":
        return "stop_sequence"
    if raw == "tool_use":
        return "tool_pending"
    return "unknown"
