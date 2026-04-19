"""
Agent orchestration — prompt-driven tool-use loop over vLLM.

One call per /v1/agent/ask. Enforces:
  - max-iteration cap
  - max-tokens cap
  - scope injection
  - gated write tool (generate_daily_report)
  - post-hoc citation verification

Backend: local vLLM (Gemma-4-26B-it by default) via the OpenAI-compatible
`/v1/chat/completions` endpoint. No native function-calling — the loop
is prompt-driven: the model emits one JSON action per turn, the
orchestrator parses/dispatches/appends the tool result as a new user
message, and repeats.

A future backend swap (Claude, GPT-4, etc.) is a ~50 LOC change in this
module gated on AGENT_BACKEND; the rest of the service (tools, citations,
rate limit, UI wiring) is backend-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .citations import verify_citations
from .client import SearchAPIClient
from .prompts import build_system_prompt, scope_preamble
from .tools import (
    dispatch_tool,
    is_report_related_question,
    tools_for_question,
)

log = logging.getLogger(__name__)


AGENT_BACKEND: str = os.environ.get("AGENT_BACKEND", "vllm").lower()
AGENT_MODEL: str = os.environ.get("AGENT_MODEL", "gemma-4-26b-it")
AGENT_VLLM_BASE_URL: str = os.environ.get(
    "AGENT_VLLM_BASE_URL",
    os.environ.get("VLLM_BASE_URL", "http://localhost:8000"),
).rstrip("/")
AGENT_MAX_ITERATIONS: int = int(os.environ.get("AGENT_MAX_ITERATIONS", "8"))
AGENT_MAX_TOKENS: int = int(os.environ.get("AGENT_MAX_TOKENS", "1024"))
AGENT_TEMPERATURE: float = float(os.environ.get("AGENT_TEMPERATURE", "0.2"))
AGENT_MAX_TOOL_OUTPUT_CHARS: int = int(
    os.environ.get("AGENT_MAX_TOOL_OUTPUT_CHARS", "8000")
)
AGENT_LLM_TIMEOUT_SEC: float = float(os.environ.get("AGENT_LLM_TIMEOUT_SEC", "90"))


# ---------------------------------------------------------------------------
# Trace data
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    name: str
    input: dict
    latency_ms: int
    output_digest: str | None = None
    output_json: Any = None
    error: str | None = None


@dataclass
class AgentTrace:
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    backend: str = ""
    model: str = ""
    iterations: int = 0
    tool_call_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_latency_ms: int = 0
    stop_reason: str | None = None
    llm_calls: int = 0
    parse_failures: int = 0

    def to_dict(self, *, include_output_json: bool = False) -> dict:
        calls = []
        for c in self.tool_calls:
            call_dict = {
                "name": c.name,
                "input": c.input,
                "latency_ms": c.latency_ms,
                "output_digest": c.output_digest,
            }
            if c.error:
                call_dict["error"] = c.error
            if include_output_json:
                call_dict["output_json"] = c.output_json
            calls.append(call_dict)
        return {
            "tool_calls": calls,
            "backend": self.backend,
            "model": self.model,
            "iterations": self.iterations,
            "tool_call_count": self.tool_call_count,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "llm_calls": self.llm_calls,
            "parse_failures": self.parse_failures,
            "total_latency_ms": self.total_latency_ms,
            "stop_reason": self.stop_reason,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent(
    *,
    http_client: httpx.Client,
    search_api_client: SearchAPIClient,
    question: str,
    scope: dict | None,
) -> dict:
    """
    Run a single /v1/agent/ask turn. Returns the structured response dict.
    """
    t0 = time.perf_counter()
    trace = AgentTrace(backend=AGENT_BACKEND, model=AGENT_MODEL)

    allow_write = is_report_related_question(question)
    tool_schemas = tools_for_question(question)

    system_prompt = build_system_prompt(tool_schemas)
    user_text = scope_preamble(scope) + f"Question: {question}"

    # Conversation log. We drive the model by appending tool_result or
    # parse-error messages as new user turns.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_text},
    ]

    parsed_answer: dict | None = None

    for _ in range(AGENT_MAX_ITERATIONS):
        trace.iterations += 1
        trace.llm_calls += 1

        llm_response = _call_vllm(http_client, messages, trace)
        if llm_response is None:
            # vLLM rejected the request (context length, bad input,
            # model not loaded, etc.) — break out and emit a degraded
            # answer. stop_reason is already set inside _call_vllm.
            parsed_answer = _degraded_answer(
                "vLLM request was rejected "
                f"(stop_reason={trace.stop_reason}); "
                "often caused by the trailer-day tool output exceeding "
                "the model's context window. Try narrowing the scope "
                "or asking a more specific question."
            )
            break

        content = (llm_response.get("choices") or [{}])[0].get("message", {}).get("content", "")
        trace.stop_reason = (llm_response.get("choices") or [{}])[0].get("finish_reason")

        # Record the assistant turn verbatim so the next LLM call sees
        # its own prior output.
        messages.append({"role": "assistant", "content": content})

        action = _parse_action(content)
        if action is None:
            trace.parse_failures += 1
            # Nudge the model toward the protocol and retry.
            messages.append({
                "role": "user",
                "content": (
                    '{"parse_error": "Your previous message was not valid JSON '
                    'matching the required protocol. Respond with a single JSON '
                    'object of shape {\\"action\\": \\"tool_call\\", ...} or '
                    '{\\"action\\": \\"answer\\", ...} and nothing else."}'
                ),
            })
            continue

        if action.get("action") == "tool_call":
            tool_name = action.get("tool", "")
            tool_input = action.get("input") or {}

            record = _dispatch_one(
                search_api_client,
                tool_name=tool_name,
                tool_input=tool_input,
                allow_write=allow_write,
            )
            trace.tool_calls.append(record)
            trace.tool_call_count += 1

            if record.error is not None:
                tool_result_payload = {
                    "tool_result": {"tool": tool_name, "error": record.error}
                }
            else:
                truncated = json.dumps(record.output_json)
                if len(truncated) > AGENT_MAX_TOOL_OUTPUT_CHARS:
                    truncated = truncated[:AGENT_MAX_TOOL_OUTPUT_CHARS] + "…(truncated)"
                tool_result_payload = {
                    "tool_result": {
                        "tool": tool_name,
                        "output": json.loads(truncated) if not truncated.endswith("…(truncated)") else truncated,
                    }
                }

            messages.append({
                "role": "user",
                "content": json.dumps(tool_result_payload),
            })
            continue

        if action.get("action") == "answer":
            parsed_answer = _normalize_answer(action.get("answer"))
            break

        # Unknown action key — treat as parse failure and nudge.
        trace.parse_failures += 1
        messages.append({
            "role": "user",
            "content": (
                '{"parse_error": "Unknown action value. Use '
                '\\"tool_call\\" or \\"answer\\"."}'
            ),
        })

    if parsed_answer is None:
        parsed_answer = _degraded_answer(
            "agent did not emit a final answer within the iteration cap"
        )

    trace.total_latency_ms = int((time.perf_counter() - t0) * 1000)

    # Post-hoc citation verification: resolves short-prefix markers to
    # full IDs where the trace has a unique match, flags anything it
    # can't resolve as unverified.
    citation_info = verify_citations(
        parsed_answer,
        trace.to_dict(include_output_json=True),
    )

    citations = _build_citations_list(
        cited=citation_info["cited"],
        trace=trace,
    )

    return {
        "answer": citation_info["rewritten_answer"],
        "citations": citations,
        "scope_used": scope or {},
        "trace": {
            **trace.to_dict(include_output_json=False),
            "unverified_citations": citation_info["unverified_citations"],
        },
    }


# ---------------------------------------------------------------------------
# vLLM call
# ---------------------------------------------------------------------------


def _call_vllm(
    http_client: httpx.Client,
    messages: list[dict[str, str]],
    trace: AgentTrace,
) -> dict | None:
    """POST /v1/chat/completions on the local vLLM. Returns the JSON
    payload, or None on an unrecoverable vLLM error (context length,
    bad request, etc.). The outer loop uses None to break out cleanly
    with a degraded answer rather than raising a 503 to the caller.

    We deliberately do NOT pass response_format={"type":"json_object"}:
    it isn't universally supported across vLLM model backends, and the
    system prompt already enforces JSON-only output with a parse-failure
    retry/nudge path.
    """
    url = f"{AGENT_VLLM_BASE_URL}/v1/chat/completions"
    body = {
        "model": AGENT_MODEL,
        "messages": messages,
        "max_tokens": AGENT_MAX_TOKENS,
        "temperature": AGENT_TEMPERATURE,
    }
    try:
        resp = http_client.post(url, json=body, timeout=AGENT_LLM_TIMEOUT_SEC)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body_preview = (exc.response.text or "")[:400] if exc.response is not None else ""
        log.warning(
            "vLLM rejected request (%s): %s",
            exc.response.status_code if exc.response is not None else "?",
            body_preview,
        )
        # 4xx from vLLM means the request was malformed OR the context
        # is too long. Either way, no point retrying at this layer.
        # Convey the failure as None so the loop can emit a degraded
        # answer rather than bubbling a 503 to the user.
        trace.stop_reason = f"vllm_error_{exc.response.status_code if exc.response is not None else 'unknown'}"
        return None
    payload = resp.json()

    usage = payload.get("usage") or {}
    trace.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
    trace.total_completion_tokens += int(usage.get("completion_tokens") or 0)
    return payload


# ---------------------------------------------------------------------------
# Parsing the model's per-turn JSON
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_action(text: str) -> dict | None:
    """Parse the model's turn as JSON. Tolerant of markdown fences and
    prefix/suffix prose (find the outermost {..}). Returns None on
    failure."""
    raw = (text or "").strip()
    if not raw:
        return None

    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    try:
        return _parse_json_loose(raw)
    except (ValueError, json.JSONDecodeError):
        return None


def _parse_json_loose(raw: str) -> dict:
    """Try strict JSON first; fall back to extracting the first
    balanced {...} block."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        first = raw.find("{")
        last = raw.rfind("}")
        if first < 0 or last <= first:
            raise
        parsed = json.loads(raw[first : last + 1])
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _normalize_answer(answer: Any) -> dict:
    """Shape-normalize the model's `answer` block."""
    if not isinstance(answer, dict):
        return _degraded_answer("final answer was not a JSON object")
    narrative = str(answer.get("narrative", "")).strip()
    bullets = answer.get("evidence_bullets") or []
    if not isinstance(bullets, list):
        bullets = []
    bullets = [str(b) for b in bullets][:6]
    next_artifact = answer.get("next_artifact")
    if not isinstance(next_artifact, dict):
        next_artifact = None
    return {
        "narrative": narrative,
        "evidence_bullets": bullets,
        "next_artifact": next_artifact,
    }


def _degraded_answer(reason: str) -> dict:
    return {
        "narrative": f"(no answer — {reason})",
        "evidence_bullets": [],
        "next_artifact": None,
    }


# ---------------------------------------------------------------------------
# Tool dispatch helper
# ---------------------------------------------------------------------------


def _dispatch_one(
    client: SearchAPIClient,
    *,
    tool_name: str,
    tool_input: dict,
    allow_write: bool,
) -> ToolCallRecord:
    t0 = time.perf_counter()
    try:
        out = dispatch_tool(
            client,
            tool_name=tool_name,
            tool_input=tool_input,
            allow_write=allow_write,
        )
    except PermissionError as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        log.warning("tool %s blocked: %s", tool_name, exc)
        return ToolCallRecord(
            name=tool_name,
            input=tool_input,
            latency_ms=latency_ms,
            output_json={"error": str(exc)},
            error=str(exc),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        log.exception("tool %s errored", tool_name)
        return ToolCallRecord(
            name=tool_name,
            input=tool_input,
            latency_ms=latency_ms,
            output_json={"error": f"{type(exc).__name__}: {exc}"},
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
        )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    digest = json.dumps(out)[:500]
    return ToolCallRecord(
        name=tool_name,
        input=tool_input,
        latency_ms=latency_ms,
        output_digest=digest,
        output_json=out,
    )


# ---------------------------------------------------------------------------
# Citations list — resolve cited IDs to their {type, id, marker}
# ---------------------------------------------------------------------------


def _build_citations_list(*, cited: list[str], trace: AgentTrace) -> list[dict]:
    id_type: dict[str, str] = {}
    for call in trace.tool_calls:
        if call.output_json is None:
            continue
        _index_ids(call.output_json, id_type)

    out: list[dict] = []
    for cid in cited:
        ctype = id_type.get(cid, "unknown")
        out.append({"type": ctype, "id": cid, "marker": cid})
    return out


def _index_ids(node: Any, id_type: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if isinstance(val, str) and re.fullmatch(r"[0-9a-fA-F]{64}", val):
                label = _key_to_type(key)
                if label:
                    id_type.setdefault(val.lower(), label)
            elif isinstance(val, (dict, list)):
                _index_ids(val, id_type)
    elif isinstance(node, list):
        for v in node:
            _index_ids(v, id_type)


def _key_to_type(key: str) -> str | None:
    k = key.lower()
    if "event_id" in k or k == "event_ids":
        return "event"
    if "image_id" in k or k == "image_ids":
        return "image"
    if "summary_id" in k or k == "summary_ids":
        return "summary"
    if "report_id" in k or k == "report_ids":
        return "report"
    if "bucket_id" in k:
        return "bucket"
    return None
