"""
Agent backend adapters (M11.1).

Each adapter is a concrete class implementing the `Backend` protocol
from base.py. The registry in this module maps the **request-facing
backend key** (e.g. "gemma", "claude", "gpt5mini") to a running
instance at app startup. Request-facing keys are stable across
provider/model rearrangement — see AI-team review refinement in the
M11.1 plan.

Layers:
  name     — request-facing key (what `/v1/agent/ask {backend: ...}` says)
  provider — vendor tag ("vllm"/"anthropic"/"openai")
  model    — concrete model identifier passed to the provider SDK

Only the vLLM backend is registered in P11.1a. Claude and gpt5mini
adapters arrive in P11.1b / P11.1c.
"""

from __future__ import annotations

import logging
import os

from .base import Backend, BackendRegistry
from .vllm import VLLMBackend

log = logging.getLogger(__name__)


_DEFAULT_ENABLED = ["gemma"]


def _parse_enabled() -> list[str]:
    raw = os.environ.get("AGENT_BACKENDS_ENABLED", "gemma")
    out = [x.strip() for x in raw.split(",") if x.strip()]
    return out or list(_DEFAULT_ENABLED)


def build_registry() -> BackendRegistry:
    """Construct the `BackendRegistry` for this process.

    - Iterates AGENT_BACKENDS_ENABLED (request-facing keys).
    - Instantiates and probes each supported backend.
    - Missing keys / failed probes → backend marked unavailable,
      appears in the registry with a reason, but won't be dispatched to.
    - AGENT_DEFAULT_BACKEND defaults to the first available backend;
      if that's unavailable at startup, a warning is logged and the
      default falls back to `gemma` (which is always configured).
    """
    registry = BackendRegistry()
    enabled = _parse_enabled()

    for key in enabled:
        if key == "gemma":
            registry.add(VLLMBackend())
        elif key == "claude":
            # P11.1b will drop the ClaudeBackend import + register it
            # here. For now, flag it as not-yet-implemented so the
            # config isn't silent.
            log.info("backend 'claude' configured but adapter not yet shipped (P11.1b)")
        elif key == "gpt5mini":
            log.info("backend 'gpt5mini' configured but adapter not yet shipped (P11.1c)")
        else:
            log.warning("backend %r in AGENT_BACKENDS_ENABLED is unknown — ignoring", key)

    # Probe each available backend with a cheap health check.
    registry.probe_all()

    # Resolve the default.
    requested_default = os.environ.get("AGENT_DEFAULT_BACKEND", "gemma")
    resolved_default = registry.resolve_default(requested_default)
    registry.default = resolved_default

    log.info(
        "agent backends registered: %s (default=%s)",
        [(b.name, b.is_available, b.unavailable_reason) for b in registry.backends.values()],
        registry.default,
    )
    return registry


__all__ = ["Backend", "BackendRegistry", "VLLMBackend", "build_registry"]
