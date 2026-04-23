# Panoptic Agent (M11 / M11.1)

Task-oriented tool-using agent over the Panoptic Search API. Answers
grounded natural-language questions by calling the same HTTP endpoints
the operator UI (M10) uses. **Not a chatbot** — single-turn, evidence-first.

Runs on `http://localhost:8500/`. Inherits the Panoptic internal trust
boundary (same as the Search API at `:8600` and operator UI at `:8400`).
No per-user auth; no public ingress today.

**M11.1 adds multi-backend support.** One tool-use loop, one citation
verifier, one UI integration — but the LLM call is dispatched to one
of several interchangeable backends chosen by env or per-request:

| Request-facing key | Provider | Default model | Status |
|---|---|---|---|
| `gemma` | local vLLM | `gemma-4-26b-it` | always available |
| `claude` | Anthropic | `claude-sonnet-4-6` | available if `ANTHROPIC_API_KEY` set |
| `gpt5mini` | OpenAI | `gpt-5-mini` | available if `OPENAI_API_KEY` set |

The request-facing name (`gemma`, `claude`, `gpt5mini`) is deliberately
separate from the provider tag (`vllm`, `anthropic`, `openai`) so
future models from the same provider (e.g. `gpt5` alongside `gpt5mini`)
add as a registration line, no rename.

## API

### POST /v1/agent/ask

```
{
  "question": "What happened on this trailer today?",
  "scope":    { "serial_number": "YARD-A-001", "date": "2026-04-18",
                "camera_ids": null },
  "backend":  "gemma" | "claude" | "gpt5mini" | null    // M11.1; null = default
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
    "backend":            "gemma",            // request-facing key
    "provider":           "vllm",             // vendor tag (M11.1)
    "model":              "gemma-4-26b-it",
    "iterations":         2,
    "tool_call_count":    1,
    "llm_calls":          2,
    "parse_failures":     0,
    "total_prompt_tokens":      8470,
    "total_completion_tokens":  412,
    "total_latency_ms":         8029,
    "backend_latency_ms":       7940,         // LLM portion, excl. tool dispatch (M11.1)
    "stop_reason":              "end_turn",
    "estimated_cost_usd":       0.0,          // benchmarking telemetry, not billing truth (M11.1)
    "tool_calls":               [{...per-tool call...}],
    "unverified_citations":     [],
    "backend_error":            null          // {code, body_preview, trace_tag} on 4xx/5xx (M11.1)
  }
}
```

Backend selection:
- If `backend` omitted or null → use `AGENT_DEFAULT_BACKEND` (default: `gemma`).
- If `backend` specified but unknown → **400** `{error: "unknown backend",
  allowed: [...]}`.
- If `backend` known but unavailable (key missing / probe failed) →
  **400** `{error: "backend unavailable", unavailable_reason: "..."}`.
- If the chosen backend fails mid-run → degraded structured answer
  with `trace.stop_reason="backend_error"` and `trace.backend_error`
  populated. **No silent fallback.**

### GET /v1/agent/backends

```
{
  "default": "gemma",
  "backends": [
    {"name":"gemma",    "provider":"vllm",      "model":"gemma-4-26b-it",
     "is_available":true, "probe_latency_ms":2,
     "pricing":{"in_per_m":0.0,"out_per_m":0.0}},
    {"name":"claude",   "provider":"anthropic", "model":"claude-sonnet-4-6",
     "is_available":false, "probe_latency_ms":null,
     "unavailable_reason":"ANTHROPIC_API_KEY not set",
     "pricing":{"in_per_m":3.0,"out_per_m":15.0}},
    {"name":"gpt5mini", "provider":"openai",    "model":"gpt-5-mini",
     "is_available":false, "probe_latency_ms":null,
     "unavailable_reason":"OPENAI_API_KEY not set",
     "pricing":{"in_per_m":1.0,"out_per_m":2.0}}
  ]
}
```

`name` is the request-facing key. Unavailable backends still appear
with `is_available=false` + `unavailable_reason` so operators can see
what would light up if they added a key.

### Other endpoints
- `GET /healthz` — service status, Search API reachability, full
  backend list + default.
- `POST /v1/agent/ask` enforces a soft in-memory rate limit
  (`AGENT_ASK_RATE_PER_MIN=30` default). Over the cap returns 429.

## Tool surface (12 tools)

Most tools are 1:1 wrappers over existing Search API read endpoints.
M14 adds the first write-to-evidence tool (`pull_frame`).

Read tools (always available):
- `search` — hybrid search across summaries / images / events
- `verify` — VLM-grounded claim verification with citations
- `summarize_period` — multi-camera period narrative; accepts `camera_ids`
- `get_trailer_day` — single-call rollup for (trailer, date)
- `get_fleet_overview` — active trailers + last-seen + 24h events + latest report
- `get_event`, `get_summary`, `get_image` — single-row detail endpoints
- `list_reports` — recent report metadata
- `get_report` — one report's status + metadata

Evidence-fetching tool (always available; rate-limited 10/min
process-wide):
- `pull_frame` — M14. Pulls a JPEG from a trailer's Continuum endpoint
  at a specific timestamp and persists it as a `panoptic_images` row
  with `source='on_demand_pull'`. Used when the agent needs visual
  evidence for a moment not covered by cached baseline / novelty /
  alert / anomaly images. Caption enrichment is async; the image_id
  is returned immediately and becomes citable.

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

## Backend configuration (M11.1)

```
# Which request-facing keys to register at startup. Hosted backends
# silently skip when their API key is absent.
AGENT_BACKENDS_ENABLED=gemma,claude,gpt5mini

# Default backend when /v1/agent/ask omits the backend field.
AGENT_DEFAULT_BACKEND=gemma

# Per-backend model + base URL (all env-overridable).
AGENT_GEMMA_BASE_URL=http://localhost:8000
AGENT_GEMMA_MODEL=gemma-4-26b-it
AGENT_CLAUDE_MODEL=claude-sonnet-4-6
AGENT_GPT5MINI_MODEL=gpt-5-mini
AGENT_GPT5MINI_BASE_URL=https://api.openai.com/v1

# Secrets — set in local .env only; never commit.
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
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
  vLLM to the 32k context limit on later iterations. The agent catches
  4xx responses, emits a degraded answer, and sets
  `trace.stop_reason="backend_error"` with
  `trace.backend_error.trace_tag="gemma_error_<code>"` — no 503 to the
  caller.

Hosted models (Claude / GPT-5 mini) are expected to resolve both the
truncation and the context-pressure issues. Today's Gemma baseline is
7/9 on the seed harness; with a hosted model we expect 9/9.

### Protocol parity across backends

All three backends use the **same prompt-driven tool loop**. The agent
sends `system_prompt` + `messages`; the model emits one JSON action
per turn (`tool_call` or `answer`); the loop dispatches or finishes.
Native tool_use / function-calling (Claude's `tool_use` blocks,
OpenAI's `tool_calls`) is deliberately **not** used in M11.1 —
apples-to-apples benchmarking depends on running the same protocol.
M11.2 is the place to add per-backend native-tool paths if the
numbers justify the code split.

## Evaluation harness

```bash
# Single-run against the default backend (from AGENT_DEFAULT_BACKEND).
.venv/bin/python tests/agent/runner.py

# Force a specific backend for the whole run.
.venv/bin/python tests/agent/runner.py --backend claude

# Compare backends on the same seed set.
.venv/bin/python tests/agent/runner.py --compare gemma,claude,gpt5mini \
    --csv-out /tmp/bench.csv --json-out /tmp/bench.json
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

`--compare` runs the full seed set once per backend and emits a
side-by-side table: per-question verdict + latency; aggregates for
pass/warn/fail, avg latency, iterations, tool_call_count, citations,
tokens in/out, total estimated cost (USD). Backends listed in
`--compare` but not currently available in the registry show
"skipped" per question + zero aggregates so the table stays
comparable across runs. `--csv-out` writes one row per (backend,
question) for spreadsheet diffing; `--json-out` writes the full
response payload for each run.

Current baseline (Gemma-4-26B-it, 2026-04-20): **7 PASS · 2 FAIL** on
the 9-question seed set. Failures are context-window pressure on
multi-round conversations; the agent catches them as
`stop_reason=backend_error` with `trace_tag=gemma_error_400`. Hosted
backends (Claude, gpt5mini) are expected to reach 9/9 when keys are
present — the seed harness is pre-wired to compare them.

### Fairness of the comparison

The seed harness runs **one protocol** (prompt-driven, same
system prompt + tool schemas for every backend). That answers
"which backend is best under this one protocol" — NOT "what is the
absolute best each provider could do." Claude and OpenAI ship native
tool_use / function-calling that typically beats prompt-driven on
tool-heavy agents; using those would break the apples-to-apples
comparison. Deferred to M11.2 if the numbers here justify the
per-provider code split.

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

## Future: M11.2

If M11.1 benchmark numbers show a meaningful gap between
prompt-driven and native tool-use for Claude / OpenAI:

- per-backend "use_native_tools: true" flag that switches to native
  `tool_use` blocks (Claude) / `tool_calls` (OpenAI) instead of the
  prompt-driven JSON action protocol
- keeps shared citation verifier + response shape
- adds per-backend message-translation paths
- breaks the current apples-to-apples benchmark; we'd need separate
  "prompt-driven" and "native" scoreboards

Also on the M11.2 table:
- multi-turn conversation wrapper (session_id + history truncation)
- per-backend prompt overrides (Claude prefers terse; OpenAI benefits
  from structured scaffolding)
- streaming responses via SSE for UI responsiveness
- explicit `AGENT_FALLBACK_ORDER` for auto-fallback (opt-in only —
  the default stays "no hidden fallback")
