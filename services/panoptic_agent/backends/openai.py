"""
OpenAIBackend — OpenAI Chat Completions adapter (M11.1c).

Request-facing backend key: "gpt5mini" (configurable; the adapter
class is name-agnostic so a future "gpt5" variant is one registration
line). Default model: gpt-5-mini (env-overridable via
AGENT_GPT5MINI_MODEL).

Provider tag: "openai".

Silent-skip if OPENAI_API_KEY is absent at construction.

Protocol: prompt-driven, like gemma + claude. System prompt becomes a
system-role message — the Chat Completions shape. Native
function-calling / tools NOT used in M11.1 to keep benchmarks fair;
that's M11.2 if the numbers justify it.
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


GPT5MINI_MODEL: str = os.environ.get("AGENT_GPT5MINI_MODEL", "gpt-5-mini")
GPT5MINI_BASE_URL: str = os.environ.get(
    "AGENT_GPT5MINI_BASE_URL", "https://api.openai.com/v1"
)
GPT5MINI_TIMEOUT_SEC: float = float(os.environ.get("AGENT_GPT5MINI_TIMEOUT_SEC", "60"))


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


class OpenAIBackend(Backend):
    """OpenAI Chat Completions adapter. Default registration uses
    name='gpt5mini'; constructor accepts a custom name so a second
    instance could register as 'gpt5' with a different model."""

    provider = "openai"

    def __init__(
        self,
        *,
        name: str = "gpt5mini",
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.name = name
        self.model = model or GPT5MINI_MODEL
        self.is_available = False
        self.unavailable_reason: str | None = "unprobed"
        self.probe_latency_ms: int | None = None
        self._client = None

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self.unavailable_reason = "OPENAI_API_KEY not set"
            return

        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            self.unavailable_reason = f"openai SDK not importable: {exc!s}"
            return

        try:
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url or GPT5MINI_BASE_URL,
                timeout=GPT5MINI_TIMEOUT_SEC,
            )
        except Exception as exc:
            self.unavailable_reason = f"client init failed: {exc!s}"
            return

    # ------------------------------------------------------------------
    # Backend.probe
    # ------------------------------------------------------------------

    def probe(self) -> tuple[bool, str | None, int | None]:
        if self._client is None:
            return False, self.unavailable_reason or "client not initialized", None

        t0 = time.perf_counter()
        try:
            # models.retrieve is a light read; 404 here would indicate
            # the configured model isn't accessible with this key.
            self._client.models.retrieve(self.model)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return True, None, latency_ms
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            # Fall back to a broader list probe in case retrieve is
            # restricted by policy on this key.
            try:
                self._client.models.list()
                latency_ms = int((time.perf_counter() - t0) * 1000)
                return (
                    True,
                    f"model {self.model!r} retrieve failed ({type(exc).__name__}) but list() succeeded",
                    latency_ms,
                )
            except Exception as exc2:
                return (
                    False,
                    f"probe_failed: {type(exc2).__name__}: {exc2!s}"[:200],
                    latency_ms,
                )

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
                "trace_tag":    f"{self.name}_error_unavailable",
            }
            return None

        oai_messages = [
            {"role": "system", "content": system_prompt},
            *messages,
        ]

        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            code = getattr(exc, "status_code", 0) or 0
            body_preview = f"{type(exc).__name__}: {exc!s}"[:400]
            log.warning("%s rejected request (%s): %s", self.name, code, body_preview)
            trace.stop_reason = "backend_error"
            trace.backend_error = {
                "code":         code,
                "body_preview": body_preview,
                "trace_tag":    f"{self.name}_error_{code or 'unknown'}",
            }
            trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)
            return None

        trace.backend_latency_ms += int((time.perf_counter() - t0) * 1000)

        # Usage.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            prompt_t = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_t = int(getattr(usage, "completion_tokens", 0) or 0)
            trace.total_prompt_tokens += prompt_t
            trace.total_completion_tokens += completion_t
            trace.raw_usage.append({
                "backend":           self.name,
                "provider":          self.provider,
                "prompt_tokens":     prompt_t,
                "completion_tokens": completion_t,
            })

        choices = getattr(resp, "choices", None) or []
        if not choices:
            trace.stop_reason = "unknown"
            return ""

        first = choices[0]
        trace.stop_reason = _normalize_finish_reason(getattr(first, "finish_reason", None))
        msg = getattr(first, "message", None)
        content = getattr(msg, "content", "") if msg is not None else ""
        return content or ""


def _normalize_finish_reason(raw: str | None) -> StopReason:
    if raw == "stop":
        return "end_turn"
    if raw == "length":
        return "max_tokens"
    if raw in ("tool_calls", "function_call"):
        return "tool_pending"
    return "unknown"
