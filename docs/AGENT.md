# Panoptic Agent (M11)

Task-oriented tool-using agent over the Panoptic Search API. Answers
grounded natural-language questions by calling the same HTTP endpoints
the operator UI (M10) uses. **Not a chatbot** — single-turn, evidence-first.

Runs on `http://localhost:8500/`. Inherits the Panoptic internal trust
boundary (same as the Search API at `:8600` and operator UI at `:8400`).
No per-user auth; no public ingress today.

## API

```
POST /v1/agent/ask
{
  "question": "What happened on this trailer today?",
  "scope":    { "serial_number": "YARD-A-001", "date": "2026-04-18",
                "camera_ids": null }
}

→ 200 OK
{
  "answer": {
    "narrative":        "2-4 sentence answer with inline [64-hex] markers.",
    "evidence_bullets": ["claim with [64-hex]", ...],
    "next_artifact":    { "kind": "report", "id": "...", "url": "/reports/.../view",
                          "label": "..." } | null
  },
  "citations": [{ "type": "event"|"image"|"summary"|"report",
                  "id": "<64-hex>", "marker": "<64-hex>" }, ...],
  "scope_used": { ... },
  "trace": {
    "backend":            "vllm",
    "model":              "gemma-4-26b-it",
    "iterations":         2,
    "tool_call_count":    1,
    "llm_calls":          2,
    "parse_failures":     0,
    "total_prompt_tokens":      8470,
    "total_completion_tokens":  412,
    "total_latency_ms":         8029,
    "stop_reason":              "stop",
    "tool_calls":               [{...per-tool call...}],
    "unverified_citations":     []
  }
}
```

Other endpoints:
- `GET /healthz` — service status + upstream reachability (vLLM + Search API).
- `POST /v1/agent/ask` enforces a soft in-memory rate limit
  (`AGENT_ASK_RATE_PER_MIN=30` default). Over the cap returns 429.

## Tool surface (11 tools)

All tools are 1:1 wrappers over existing Search API endpoints. **No new
backend endpoints land with M11.**

Read tools (always available):
- `search` — hybrid search across summaries / images / events
- `verify` — VLM-grounded claim verification with citations
- `summarize_period` — multi-camera period narrative; accepts `camera_ids`
- `get_trailer_day` — single-call rollup for (trailer, date)
- `get_fleet_overview` — active trailers + last-seen + 24h events + latest report
- `get_event`, `get_summary`, `get_image` — single-row detail endpoints
- `list_reports` — recent report metadata
- `get_report` — one report's status + metadata

Gated write tool (only visible to the model when the question's text
matches a report-intent regex — prevents the agent from choosing
"generate a report" as a lazy resolution for any uncertainty):
- `generate_daily_report` — enqueues a daily report; idempotent

## Architecture

```
┌─────────────────┐   HTTP   ┌──────────────────┐   HTTP   ┌─────────────┐
│ operator_ui     │ ────────▶│ panoptic_agent   │ ────────▶│ search_api  │
│ :8400           │◀──────── │  :8500           │◀──────── │ :8600       │
│ (M10)           │          │  tool-use loop   │          │ (unchanged) │
└─────────────────┘          │  over local vLLM │          └─────────────┘
                             └──────────────────┘
                                     │
                                     ▼
                             panoptic-vllm :8000
                             (Gemma-4-26B-it by default)
```

- The agent **never** touches Postgres, Redis, or filesystem directly.
  Every read goes through the Search API.
- Operator UI has a single consumer integration today: the
  "Ask about this day" panel on the trailer-day page. Any client
  (CLI, Slack bot, remote trigger) can call `/v1/agent/ask` directly.
- `AGENT_BACKEND` env var reserves a path for a hosted-model swap
  (Claude / GPT-4) with no code changes to tools, dispatch, or UI.

## Interaction model (v1)

Single-shot ask → single-shot answer. No chat history; no session
memory; no persistent conversation state across requests. Every ask
is independent. Multi-turn refinement is deferred to a future milestone.

Why: sharper to debug, sharper to evaluate, and avoids turning M11 into
a UX project. The same endpoint will naturally accept a conversation
wrapper later (one field on the request) without breaking v1 clients.

## Guardrails (enforced in v1)

- **Evidence-first citations.** System prompt: every inline `[<id>]`
  marker must reference a real ID that appeared in a tool output in
  this turn. A post-hoc verifier scans the final answer, resolves
  truncated hex prefixes to full 64-hex IDs via the trace, and
  populates `trace.unverified_citations` for anything it cannot
  resolve. The operator UI renders a warning banner when that list is
  non-empty.
- **Conservative wording.** System prompt requires hedged language
  ("strongest evidence suggests", "no direct evidence found in this
  window") when an answer is based on partial retrieval; firm language
  ("verified", "confirmed") only when the `verify` tool returned a
  supportive verdict in this turn.
- **Prefer scoped tools first.** If the request carries a scope
  (serial + date), the system prompt nudges the agent toward
  `get_trailer_day` and filtered `search` before widening.
- **Max iterations = 8.** Hard cap; if hit without a final answer,
  return a degraded answer with whatever evidence is in the trace.
- **Max tokens per LLM call = 1024.** Keeps completions tight.
- **Gated write tool.** `generate_daily_report` is included in the
  per-request tool list **only** when the question matches a
  report-intent regex (`generate/create/make/produce ... report`).
  Dispatch layer re-checks `allow_write` even if the model somehow
  invokes it anyway.
- **Rate limit.** Soft sliding-window, 30 asks/min. Returns 429 over.
- **No DB access.** Agent service has no psycopg2, no SQLAlchemy,
  no Redis client.

## Model selection

```
AGENT_BACKEND  = vllm        # today; "claude" / "openai" reserved for later swap
AGENT_MODEL    = gemma-4-26b-it
AGENT_VLLM_BASE_URL = http://localhost:8000
```

Observed behavior on Gemma-4-26B-it (local vLLM):
- **JSON compliance** — ~75% clean on first turn; occasional parse
  failures self-recover via a parse-error nudge message.
- **Citation hygiene** — truncates long hex IDs in ~1-in-3 answers
  (missing 1-3 chars). The verifier's prefix-resolver catches most
  tail-truncations; middle-truncations get flagged as
  `unverified_citations` and rendered with a warning state in the UI.
- **Tool planning** — efficient; usually 1-2 tool calls per ask. When
  scope is provided, almost always starts with `get_trailer_day`.
- **Latency** — 7-25 seconds per ask on the Spark. Tolerable for the
  operator-typing cadence, not fast enough for chat.
- **Context window pressure** — for high-volume trailer-days (the real
  trailer with ~30 events + ~100 summaries), tool outputs can push
  vLLM to the 32k context limit on later iterations. The agent now
  catches vLLM 400s, emits a degraded answer explaining the scope
  issue, and logs `stop_reason=vllm_error_<code>` — no 503 to the
  caller.

A hosted-model swap (Claude Sonnet 4.6 or GPT-4) is expected to fix
both the truncation and the context pressure. Today's gate is 7/9 on
the seed harness; with a hosted model we expect 9/9.

## Evaluation harness

```
cd ~/panoptic
.venv/bin/python tests/agent/runner.py
```

Fires each seed question in `tests/agent/seed_questions.yaml` and
scores against assertions:

- `expected_tools` — at least one of these tool names must appear in `trace.tool_calls`
- `min_citations` — `citations[]` must have at least this many entries
- `max_unverified` — `trace.unverified_citations` length must be ≤ this
- `max_iterations` — `trace.iterations` must be ≤ this
- `max_latency_ms` — `trace.total_latency_ms` must be ≤ this (WARN, not FAIL)
- `hedge_required` — narrative must contain hedging language
  ("no evidence", "strongest evidence suggests", etc.) — enforces
  evidence discipline on questions where the honest answer is "I don't know"
- `disallow_next_artifact` — `next_artifact` must be null

Current baseline (Gemma-4-26B-it, 2026-04-19): **7 PASS · 2 FAIL**
Failures are `real_trailer_whats_important` and `yard_after_hours_activity`,
both context-window pressure on multi-round conversations. Tracked as
M11.1 items.

## Audit log

Every `/v1/agent/ask` emits one structured JSONL line (logger name
`panoptic_agent.audit`) with question + scope + trace summary +
narrative preview. Routed to Docker's json-file driver by default;
parse with standard JSON tooling for replay or regression investigation.

## Access

**Internal-only.** `:8500` must not be tunneled publicly. Per-user
authentication, trailer-scoped ACLs, and session/SSO are all explicit
non-goals for v1. The agent's /ask endpoint surfaces the same data as
the Search API's `/v1/search` + detail endpoints — same trust boundary
applies.

If `:8500` ever gets a public tunnel, an auth layer at the edge (Caddy
API key, session cookie, or equivalent) must land first. Don't expose
this endpoint directly without that.

## Known limitations (v1)

- **No multi-turn / chat.** M11.x.
- **Context-window pressure on large trailer-days.** Local Gemma can
  hit its limit when the rollup + tool_result accumulates across
  rounds. Hosted-model swap resolves; meanwhile the agent degrades to
  a JSON response with `stop_reason=vllm_error_400`.
- **Hex citation truncation.** Gemma sometimes truncates 64-hex IDs by
  1-3 chars. Verifier resolves tail-truncations; middle-truncations
  get flagged as unverified.
- **No background / cron-triggered agent runs.** M11 is request-driven.
- **No per-camera-subset report scoping via the agent.** Operators can
  already ask `summarize_period` with `camera_ids` for live narratives;
  persisted camera-subset reports are tracked in
  [project_camera_roles.md](../../.claude/projects/-home-surveillx-panoptic/memory/project_camera_roles.md).

## Future: M11.1 hosted-model swap

Implementation: add `AGENT_BACKEND=claude` (or `openai`) path alongside
the existing vLLM path. Switch is roughly 50 LOC in `agent.py`
(SDK, prompt_system → `system=[...]` shape, cache_control on system +
tools). Tools, dispatch, citations, rate limiter, UI integration all
stay unchanged. When real customer demo needs quality + sub-5s
latency, this is where we spend a few dollars a day on Sonnet or
GPT-4 and leave vLLM as the fallback.
