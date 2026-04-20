"""
Backend protocol + shared types for the multi-backend agent.

The only surface the agent loop cares about is `Backend.generate()`.
Each adapter handles SDK quirks (request shape, usage mapping,
stop-reason mapping, error → None) and leaves everything else —
tools, prompts, citation verification, UI, audit log — in the shared
layer.

Naming layers (per M11.1 AI-team review):
  Backend.name      — request-facing key ("gemma" | "claude" | "gpt5mini")
  Backend.provider  — vendor tag ("vllm" | "anthropic" | "openai")
  Backend.model     — model identifier string
Three fields land in the trace as separate keys so analytics can
group by any of them without inference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..agent import AgentTrace  # avoid a runtime cycle


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized stop reasons
# ---------------------------------------------------------------------------

StopReason = Literal[
    "end_turn",       # model finished naturally
    "max_tokens",     # hit the output cap
    "stop_sequence",  # stop token matched
    "tool_pending",   # native function-calling; not used in v1 (prompt-driven)
    "backend_error",  # 4xx/5xx/network/quota — inspect trace.backend_error
    "unknown",
]


# ---------------------------------------------------------------------------
# Pricing table — benchmarking telemetry, NOT billing truth.
# ---------------------------------------------------------------------------
# As-of 2026-04-19; expected to drift. Keyed by request-facing backend
# name so adding a new size (e.g. "gpt5" alongside "gpt5mini") is
# trivial.

@dataclass(frozen=True)
class Pricing:
    in_per_m: float   # USD per 1M input tokens
    out_per_m: float  # USD per 1M output tokens


PRICING: dict[str, Pricing] = {
    "gemma":    Pricing(in_per_m=0.0,  out_per_m=0.0),
    "claude":   Pricing(in_per_m=3.0,  out_per_m=15.0),   # Sonnet 4.6
    "gpt5mini": Pricing(in_per_m=1.0,  out_per_m=2.0),    # GPT-5 mini placeholder
}


def estimate_cost_usd(backend_name: str, tokens_in: int, tokens_out: int) -> float:
    pricing = PRICING.get(backend_name)
    if pricing is None:
        return 0.0
    return round(
        (tokens_in * pricing.in_per_m + tokens_out * pricing.out_per_m) / 1_000_000.0,
        6,
    )


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class Backend(Protocol):
    """The only surface the agent loop cares about.

    Implementations must expose these four attributes as instance data
    (not just class attributes) so the registry can log availability
    and the trace can round-trip provenance.
    """

    name: str                        # request-facing key
    provider: str                    # vendor tag
    model: str                       # model identifier
    is_available: bool               # did startup probe succeed?
    unavailable_reason: str | None   # populated when is_available is False
    probe_latency_ms: int | None     # last probe latency, if any

    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        trace: "AgentTrace",
    ) -> str | None:
        """Call the underlying model and return the assistant's text.

        Must:
          - update `trace.backend` / `trace.provider` / `trace.model`
            on first call (subsequent calls may overwrite identically)
          - accumulate `trace.total_prompt_tokens`,
            `trace.total_completion_tokens`, `trace.raw_usage`
          - set `trace.stop_reason` using the StopReason enum
          - on any 4xx/5xx/network error, set trace.stop_reason="backend_error"
            and trace.backend_error={"code": ..., "body_preview": ...}
            and return None. Do NOT raise through the adapter boundary.

        Must NOT:
          - add tool dispatch, citation logic, or retries
          - log the API key
          - mutate any message content
        """
        ...

    def probe(self) -> tuple[bool, str | None, int | None]:
        """Cheap reachability check run once at startup.

        Returns (ok, reason_when_not_ok, latency_ms).
        """
        ...


# ---------------------------------------------------------------------------
# Registry — owns the set of backends and the default pointer
# ---------------------------------------------------------------------------


class BackendRegistry:
    """Startup-time collection of backends keyed by request-facing name.

    Handles:
      - registration
      - availability probing
      - default resolution + safe fallback when the configured default is down
      - dispatch lookup by request-facing key
    """

    def __init__(self) -> None:
        self.backends: dict[str, Backend] = {}
        self.default: str = "gemma"

    # ---- registration ----

    def add(self, backend: Backend) -> None:
        if backend.name in self.backends:
            log.warning("backend %s already registered — replacing", backend.name)
        self.backends[backend.name] = backend

    def probe_all(self) -> None:
        for backend in self.backends.values():
            try:
                ok, reason, latency_ms = backend.probe()
            except Exception as exc:  # defensive — any probe bug must not kill startup
                log.exception("probe for backend %s raised", backend.name)
                ok, reason, latency_ms = False, f"probe_error: {exc!s}"[:200], None
            backend.is_available = ok
            backend.unavailable_reason = None if ok else (reason or "probe_failed")
            backend.probe_latency_ms = latency_ms

    def resolve_default(self, requested: str) -> str:
        """Pick the effective default backend for this process.

        Prefers the configured default if it's available. Falls back
        to `gemma` if the configured default is unavailable. Logs a
        warning when the fallback fires so operators see it.
        """
        if requested in self.backends and self.backends[requested].is_available:
            return requested
        if requested not in self.backends:
            log.warning(
                "AGENT_DEFAULT_BACKEND=%r not in enabled backends %s; "
                "falling back to 'gemma'",
                requested, list(self.backends.keys()),
            )
        else:
            log.warning(
                "AGENT_DEFAULT_BACKEND=%r is registered but unavailable (%s); "
                "falling back to 'gemma'",
                requested, self.backends[requested].unavailable_reason,
            )
        if "gemma" in self.backends and self.backends["gemma"].is_available:
            return "gemma"
        # Fallback of last resort — first available backend, or
        # whatever was configured if nothing is available.
        for name, b in self.backends.items():
            if b.is_available:
                return name
        return requested

    # ---- dispatch ----

    def get(self, name: str | None) -> Backend | None:
        """Return the backend instance for this key (or default if None)."""
        key = name or self.default
        return self.backends.get(key)

    def list_public(self) -> list[dict]:
        """Shape for `GET /v1/agent/backends`."""
        out: list[dict] = []
        for name, b in self.backends.items():
            entry: dict = {
                "name":             b.name,
                "provider":         b.provider,
                "model":            b.model,
                "is_available":     b.is_available,
                "probe_latency_ms": b.probe_latency_ms,
            }
            if not b.is_available:
                entry["unavailable_reason"] = b.unavailable_reason
            pricing = PRICING.get(b.name)
            if pricing is not None:
                entry["pricing"] = {
                    "in_per_m":  pricing.in_per_m,
                    "out_per_m": pricing.out_per_m,
                }
            out.append(entry)
        return out
