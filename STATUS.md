# Panoptic — System Status (2026-04-19, refresh #5)

Briefing document for external AI collaborators and future-self sessions.
Self-contained.

---

## 1. What Panoptic Is

Edge-to-cloud surveillance analytics. Fleet of trailers (Jetson-based
units with up to 8 cameras) run local perception (`cognia`) and push two
kinds of HMAC-signed HTTP payloads to a central processing stack:

1. **15-minute detection buckets** — aggregated per-object-type stats
   per camera.
2. **Images** — JPEGs on alert / anomaly / baseline triggers with
   metadata binding each image to a bucket.

The central stack authenticates + dedup-checks pushes, captions images
via Gemma-4-26b (vision), summarizes buckets via Gemma text (optionally
with keyframes from the trailer's Continuum), embeds captions and
summaries with Qwen3-Embedding-8B into Qdrant, additionally **embeds
each image natively with Qwen3-VL-Embedding-8B** (M5), **produces
first-class `panoptic_events` rows from image triggers and bucket
markers** (M8), and serves hybrid semantic search over the history via
the Search API.

---

## 2. Hardware

- **DGX Spark** (GB10, Blackwell-class, 128 GB unified CPU/GPU memory).
  Application + data host today. `python3.12.3`, Ubuntu Noble, aarch64.
- **DigitalOcean droplet `surveillx-gateway`** (public gateway): runs
  Caddy (TLS termination, on-demand Let's Encrypt) + FRP server
  (`frps`). Reserved IP `134.199.244.90`. Public hostnames:
  `panoptic.surveillx.ai`, `agent.surveillx.ai`, `*.trailers.surveillx.ai`.
- **Planned**: separate data tier to a dedicated LAN-local box once
  the first trailer is stable and multi-Spark scaling is needed (M6).

Unified-memory usage: ~73 GB of 121 GB; ~48 GB free.

---

## 3. Repo Layout

Four repos, all deployed on the Spark, all on `main`. HTTP between tiers,
no shared Python packages.

| Repo | Role | Deployment |
|---|---|---|
| `panoptic` | Application: workers, webhook, Search API, DB schema, reclaimer, HMAC auth, health/dashboard, VL image embedding | Python venv (tmux dev session) |
| `panoptic-vllm` | LLM serving (Gemma-4-26b-it via vLLM, multimodal) | Docker compose |
| `panoptic-retrieval` | Text + VL embed/rerank service (Qwen3 models, fp8) | Docker compose |
| `panoptic-store` | Postgres + Qdrant + Redis | Docker compose |

Image files: `/data/panoptic-store/images/<serial>/<camera>/<yyyy>/<mm>/<dd>/<image_id>.jpg`.

---

## 4. Runtime State

### 4.1 Docker services

| Container | Port(s) | Role |
|---|---|---|
| `panoptic-vllm` | 8000 | `gemma-4-26b-it` (multimodal) |
| `panoptic-retrieval-retrieval-1` | 8700 | Qwen3 text embed (dim 4096), rerank, VL embed, VL rerank |
| `panoptic-postgres` | 5432 | Postgres 16 |
| `panoptic-qdrant` | 6333 / 6334 | Qdrant v1.13.6 |
| `panoptic-redis` | 6379 | Redis 7 |

### 4.2 Application processes (tmux session `panoptic`, **13 windows**)

| Window | Port | Role |
|---|---|---|
| `webhook` | 8100 | Trailer ingest (FastAPI, HMAC middleware) |
| `caption` | 8201 | Image captions (Gemma vision) |
| `cap_embed` | 8202 | Caption → Qdrant `image_caption_vectors` |
| `img_embed` | 8206 | **VL pixels → Qdrant `panoptic_image_vectors` (M5)** |
| `summary` | 8203 | Bucket summaries (Gemma text, optional keyframes) |
| `sum_embed` | 8204 | Summary → Qdrant `panoptic_summaries` |
| `rollup` | 8205 | Multi-level rollups |
| `event_producer` | 8207 | **image/bucket → `panoptic_events` rows (M8)** |
| `report_gen` | 8208 | **daily/weekly HTML reports (M9)** |
| `reclaimer` | 8210 | Lease expiry recovery + stream re-enqueue (30 s tick) |
| `search` | 8600 | Search API (hybrid retrieval + M9 reports + M10 trailer-day/fleet/detail endpoints) |
| `operator_ui` | 8400 | **Operator evidence browser — HTML surface over Search API (M10)** |
| `agent` | 8500 | **Task-oriented agent over vLLM + Search API tools (M11)** |

Start/observe: `cd ~/panoptic && ./scripts/tmux-dev.sh` then
`tmux a -t panoptic`.
Logs tee'd to `~/panoptic/logs/*.log`, rotated daily × 14 copies.

### 4.3 Edge infrastructure

- Caddy on DO droplet terminates TLS for `panoptic.surveillx.ai` →
  `frps:8080` vhost → FRP tunnel over WAN → frpc systemd unit on the
  Spark (`/etc/frp/frpc.toml`) → `127.0.0.1:8100`.
- Auth is enforced **inside** the webhook (HMAC middleware), not at the
  edge. Caddyfile is unchanged vanilla reverse-proxy config.

### 4.4 Persistent state

| Table / Collection | Approx count | Notes |
|---|---|---|
| `panoptic_buckets` | ~220 | mostly real trailer `1422725077375` (running ~15h) |
| `panoptic_images` | ~141 | mostly real trailer, rest synthetic |
| `panoptic_summaries` | ~233 | real trailer rollups + period summaries |
| `panoptic_events` (M8) | 163 | 58 image_trigger + 105 bucket_marker; content-addressed event_ids |
| `panoptic_camera_aliases` (M8) | 0 | inert — insert rows only when a trailer emits mismatched camera_ids across payloads |
| `panoptic_reports` (M9) | growing | async HTML reports: daily + weekly, 90-day disk retention |
| `panoptic_jobs` | ~885 | all terminal (succeeded / failed_terminal / degraded) |
| `panoptic_trailers` | 6 active | registry (real trailer + 4 synthetic + SMOKE-TEST) |
| `image_caption_vectors` (Qdrant, 4096-dim cosine) | 141 pts | caption-text embeddings |
| `panoptic_image_vectors` (Qdrant, 4096-dim cosine) | 141 pts | VL-native image embeddings (M5) |
| `panoptic_summaries` (Qdrant, 4096-dim cosine) | 233 pts | summary-text embeddings |

Alembic at migration **010**.

---

## 5. Milestone Status

| # | Milestone | Status |
|---|---|---|
| M1 | Search API live + ingest→query proof + relevance harness + idempotency sanity + `docs/M1_RESULTS.md` | ✅ done |
| M2 | Webhook auth + minimum observability | ✅ done (HMAC middleware, panoptic_trailers, /healthz, dashboard, reclaimer, frpc) |
| M3 | Onboard one real trailer | ✅ effectively done — `1422725077375` pushing unattended for ~7 hours; 25 buckets, 7 images, 2 summaries, 0 failed jobs |
| M5 | VL image retrieval (+ opt-in VL final rerank) | ✅ done |
| M4 | Full idempotency + crash-recovery validation | ✅ done — 6/6 dep-outage tests, worker-restart-storm + duplicate-flood both verified |
| M6 | Move panoptic-store to dedicated machine | pending (blocked on hardware) |
| M7 | Containerize workers | ✅ done — `docker compose up -d` brings up the 11-service stack (M9 added report_generator), all healthy, end-to-end push + search verified through containers |
| M8 | Unified event layer (panoptic_events + producer worker) | ✅ done — 58/58 image triggers + 105/105 bucket markers converted; Search API + period summary both cite real event_ids; backfill is zero-insert on rerun |
| M9 | Async report generation (daily + weekly HTML) | ✅ done — `POST /v1/reports/{daily,weekly}` enqueue via new `panoptic_report_generator` worker; content-addressed report_ids; authorized asset endpoint; weekly aggregation SQL tables + per-day narrative roll-up; 90-day on-disk retention; cron entries documented in `docs/REPORTS.md` |
| M10 | Operator evidence browser UI | ✅ done — new `panoptic_operator_ui` service (12th, `:8400`) over 9 new read-only Search API endpoints; HTMX + Jinja2; pages: fleet, trailer-day, search (URL-driven), report viewer (iframe), event/summary/image detail; explicit empty states; "Generate daily report" button wires through to the M9 worker; access model documented in `docs/OPERATOR_UI.md` |
| M11 | Task-oriented agent layer | ✅ done — new `panoptic_agent` service (13th, `:8500`) runs a prompt-driven tool-use loop over the local vLLM (Gemma-4-26B-it). 11 tools wrap existing Search API endpoints; grounded structured answers with evidence-first citations; `"Ask about this day"` panel on the trailer-day page; seed-question harness baseline 7/9 PASS on local Gemma; hosted-model swap (Claude / GPT-4) is an `AGENT_BACKEND` env flip for M11.1. Access model + guardrails in `docs/AGENT.md` |

---

## 6. Today's New Capabilities & Key Findings

### 6.1 VL image retrieval (M5)

Second semantic space over the same images. Pixel-similarity cluster
queries work:

- `"patio with chairs"` → 0.85 on exact match
- `"nighttime surveillance view"` → 0.70+ on dark outdoor real imagery
- `"orange diamonds"` → top-2 at 1.00 (duplicates), then other
  orange-colored visual clusters

Chain: `image_caption` → caption_embed AND image_embed fan out in
parallel. `SEARCH_RETRIEVAL_MODE=hybrid` (default) merges both
retrieval spaces before rerank.

### 6.2 DLQ tooling + replay

- `scripts/dlq_inspect.py` — list all DLQ entries with Postgres state
  correlation
- `scripts/dlq_replay.py` — reset + re-enqueue (single or bulk),
  `--ack` to clear DLQ on success, `--dry-run` previews

### 6.3 Failure mode documentation

`docs/FAILURE_MODES.md` — 9 failure modes documented with empirical
evidence from today's outage tests (Redis, Postgres, Qdrant, retrieval,
vLLM).

### 6.4 Bug caught and fixed during Redis outage test

**Before:** during any Redis restart, the 6 job-processing workers
died because `consume_next()`'s `XREADGROUP` raised `ConnectionError`
outside the try/except that protected message processing. Required
manual respawn of every worker.

**After:** `shared/utils/streams.consume_next()` catches
`ConnectionError`/`TimeoutError`, logs a backoff warning, sleeps 1 s,
returns None. Outer loop retries naturally. Re-verified: second Redis
outage with fix in place → zero worker deaths.

### 6.5 Real-trailer schema nits absorbed

Trailer payloads hit 3 validation cascades we patched mid-flight:

1. `anomaly_score` + confidence fields + timestamp fields as null
   when scorer hasn't warmed up — made optional.
2. `bucket_minutes` + `anomaly_flag` omitted entirely — defaults 15 / 0.
3. `mean_count` + `std_dev_count` as null, `duty_cycle > 1.0` (observed
   3.46). Nullable + clamp into `completeness.detection_coverage` [0,1].

All documented in trailer handoff doc at `~/Downloads/PANOPTIC_TRAILER_STATUS_UPDATE.md`.

### 6.6 M4 crash-recovery — remaining tests complete

- **Worker-restart-storm** (kill all 6 workers mid-flight, respawn):
  clean drain, zero duplicates. See `docs/FAILURE_MODES.md` §11.
- **Duplicate flood** (100 concurrent identical signed pushes): 1
  accepted, 99 duplicate-409, 0 errors, no races on bucket or image
  dedup paths. See §12.

### 6.7 Operational hardening (retention, backup, freshness)

- **Qdrant nofile bumped** to 65536 after real-traffic FD climb hit
  the 1024 default. Capacity math through 500 trailers captured in
  `docs/SCALING.md`.
- **Postgres `max_connections=500`** (up from 100) — new probe
  traffic + several crons made 100 too tight.
- **Health probe connection leaks fixed** — both the Postgres probe
  and the Redis consumer-stats probe were creating fresh clients per
  tick. Reviewable in commits `0a50c59` and `5e58192`.
- **Retention** — `scripts/prune_images.py` (7d baseline / 180d alert
  / 365d anomaly) and `scripts/prune_jobs.py` (30d terminal) with
  nightly cron. Policy in `docs/RETENTION.md`.
- **Backup** — `~/panoptic-store/backup/pg_dump.sh` (14 dumps kept)
  and `qdrant_snapshot.sh` (7 snapshots/collection, pruned via Qdrant
  API). Both nightly from user cron. Qdrant snapshot volume mount
  fixed — was previously writing into the container's ephemeral
  layer, would have vanished on recreate.
- **Restore drill rehearsed** — full pg_dump restore into temp DB
  (141/233/885 rows exact match) and Qdrant snapshot recover into
  temp collection (233 points) both proven. Procedure in
  `docs/RESTORE.md`.
- **Backup freshness** in `health_watch.py` — alerts if either backup
  type is more than 36h old (silent-cron detector).

### 6.8 M5 polish — opt-in VL rerank

`SEARCH_RERANK_MODE=vl` makes the final-pass image rerank score
`(query, actual pixels)` via Qwen3-VL-Reranker-2B instead of scoring
caption text. Caps at 8 items/call (retrieval-service limit); larger
hit sets use VL on the top-8 and preserve original order for the
tail. Default stays `text` — flip when real surveillance imagery
dominates the workload.

### 6.9 M8 — unified event layer

New `panoptic_events` table (migration 008) + `panoptic_event_producer`
worker (new 10th service) unify image-trigger events and bucket-level
markers under a single content-addressed `event_id` space.

**Schema:** `event_id` (SHA256 PK), `event_source` IN
('image_trigger','bucket_marker'), `event_type` (canonical: e.g.
`alert_created`, `anomaly_detected`, `activity_spike`,
`after_hours_activity`), `severity`/`confidence`, full time triple
(`start_time_utc`/`end_time_utc`/`event_time_utc`), optional
enrichment refs (`bucket_id`/`image_id`), `title`/`description`,
`metadata_json`. Identity hash excludes enrichment — verified
idempotent under manual enrichment attachment.

**Wiring:**
- `trailer_webhook` enqueues `event_produce` after every alert/anomaly
  image commit.
- `cognia.ingest_bucket` enqueues after every bucket commit with
  non-empty `event_markers`.
- `services/panoptic_event_producer/executor.py` handles both
  `source_type='image'` and `source_type='bucket'`, INSERT ... ON
  CONFLICT DO NOTHING.
- `scripts/backfill_events.py --source image|bucket|all --apply`
  covers historic data (58 images + 105 markers backfilled).

**Downstream:**
- Search API `EventHit` cleanly cut to event-native fields (no legacy
  trigger/captured_at/caption_text). New filters: `event_type`,
  `event_source`. Filter-only browse queries `panoptic_events`
  directly; semantic browse hydrates via `image_id` FK.
- `/v1/search/verify` and `/v1/summarize/period` both now cite real
  64-char `event_id`s. Period summary prompt now surfaces
  bucket_marker events (spikes, after-hours) to the VLM.

**Camera-ID canonicalization (migration 009):**
Inert `panoptic_camera_aliases` table + `shared/canonical/camera.py`
resolver. Deploys no-op until an operator inserts a row; intended to
collapse trailer-emitted bucket vs image camera_id mismatches. Unused
today because no alias has been inserted — the first real trailer's
mismatch is the canonical use case (see §7 gaps).

**Bucket markers v1:** `spike` + `after_hours` only. `drop`, `start`,
`late_start`, `underperforming` remain as consumer-branch stubs in
the summary agent; deferred to a follow-on phase.

### 6.10 M9 — async report generation

HTML daily + weekly reports per trailer, generated by a new
`panoptic_report_generator` worker (11th service, health port 8208)
consuming the `panoptic:jobs:report_generate` stream.

**Enqueue paths (share one helper):**
- `POST /v1/reports/daily` / `POST /v1/reports/weekly` → immediate
  `{report_id, status}` response, job runs async.
- `scripts/generate_reports.py --daily|--weekly --all-active` — cron
  driver calls the same `_enqueue_report` function in-process (no HTTP
  round-trip), identical semantics.

**Lifecycle:** `pending → running → success` on happy path; `pending →
running → failed` (with `last_error`) on terminal failure. The `running`
transition uses a short external commit so it's observable while the
main job transaction is still in flight. `max_attempts=1` — VLM failures
go straight to DLQ; retry via `scripts/dlq_replay.py`.

**Daily output:** one VLM pass per camera (5 on a typical trailer), one
fusion pass, renders HTML with per-camera narratives + images + events
table. Typical generation time ~10-15s for 5 cameras.

**Weekly output:** aggregates 7-day counts via direct SQL
(`shared/report/aggregate.py`) for event-type totals, per-camera rank,
image-trigger totals, notable events. Fuses the 7 daily report
narratives (cached in `panoptic_reports.metadata_json.overall`) via one
weekly VLM call. Missing days render as placeholders.

**Storage:**
`/data/panoptic-store/reports/<serial>/<yyyy>/<mm>/<serial>-<stamp>-<kind>.html`
(stamp is `YYYYMMDD` for daily, `GW%V` for weekly). Configurable via
`REPORT_STORAGE_ROOT`.

**Provenance + asset auth:** `metadata_json` carries four cited-id
lists (`image`, `event`, `summary`, `camera`). The
`GET /v1/reports/<id>/assets/<image_id>.jpg` endpoint authorizes by
checking `image_id ∈ cited_image_ids` for that report_id, preventing
the endpoint from becoming a generic image proxy.

**Retention:** `scripts/prune_reports.py --apply --keep-days 90` runs
in nightly cron at 03:45 UTC. Deletes HTML files but retains Postgres
rows (small; preserve history). Policy + cron entries in
`docs/REPORTS.md`.

**template_version** ("v1") is metadata-only — NOT part of the
content-addressed `report_id`. Bumping the template regenerates in
place, no new row.

### 6.11 M10 — operator evidence browser UI

New `panoptic_operator_ui` service (12th, health port `:8400`,
`/healthz` on the same port). Thin HTML surface consuming the Search
API over loopback HTTP. No Postgres, no Redis, no DB access from the
UI layer — every read goes through `services/panoptic_operator_ui/client.py`.

**Stack:** FastAPI + Jinja2 + HTMX (vendored 1.9.12) + minimal CSS.
No build step, no SPA, no `node_modules`. Each page is a real URL.

**Pages shipped (all `http://localhost:8400/...`):**
- `/` — fleet overview: 10 active trailers with last-seen + 24h event
  count + latest daily report link
- `/trailer/{serial}/{yyyy-mm-dd}` — single-page rollup for one trailer
  on one UTC day: daily report status/link, report-history panel
  (5 daily + 2 weekly), event list with per-type color classes,
  deduped image thumbnails, per-camera mini-table, summary list,
  "Generate daily report" button (POST → 303 back to the page)
- `/search?q=...&type=event&serial=...` — URL-driven search; query +
  filters in the URL so results are shareable; grouped event/image/
  summary results; graceful-degrade when a semantic type is selected
  without a query
- `/reports/{id}/view` — iframe wrapping the stored M9 HTML (served
  by `search_api` directly so the report's internal asset URLs
  resolve against `:8600`)
- `/events/{id}`, `/summaries/{id}`, `/images/{id}` — evidence detail
  pages; all fall through to a clean 404 template on unknown IDs

**New Search API endpoints added for M10 (read-only JSON unless noted):**
- `GET /v1/trailer/{serial}/day/{yyyy-mm-dd}` — composite rollup; reuses
  `shared/report/synthesis` helpers for byte-equivalent fetch semantics
- `GET /v1/fleet/overview` — single CTE query over trailers + buckets +
  images + events + reports
- `GET /v1/events/{id}`, `/v1/summaries/{id}`, `/v1/images/{id}` —
  single-row detail endpoints; 404 on unknown IDs
- `GET /v1/images/{id}.jpg` — streams image bytes (no cited-id check,
  unlike the M9 report-asset endpoint)
- `GET /v1/reports/{id}/view` — streams stored HTML for iframe
  embedding (required adding a `/data/panoptic-store/reports:ro`
  mount to `search_api`)
- `GET /v1/reports?serial_number=&kind=&limit=` — report-history
  listing for the UI panel

**Bugfix during M10:** `get_report_view` originally passed `filename=`
to `FileResponse`, which set `content-disposition: attachment` — this
would cause browsers to download the report instead of rendering it in
the iframe. Dropped the `filename=` arg so `content-disposition` is
absent and the HTML renders inline.

**Second bugfix:** the `/search` handler's `type: list[str]` parameter
silently failed to bind because `type` shadows a Python builtin.
Switched to `Query(alias="type")` binding to `rt` — URL stays
`?type=...` for shareability, Python variable is clean.

**Access model (internal-only, documented in `docs/OPERATOR_UI.md`):**
- `:8400` (UI) and `:8600` (Search API) are both internal-only; **no
  public tunnel**, no per-user auth.
- `GET /v1/images/{id}.jpg` has no cited-id check — it's the broadest
  new data-access vector in M10. Must not be tunneled without
  landing auth first.
- Only `:8100` (webhook) is publicly tunneled today, and that enforces
  HMAC. Docker-compose carries a multi-line security comment on the
  `search_api` service reinforcing this at config-time.

**Explicit out-of-scope for v1:** per-user auth, chat/agent, PDF,
email/Slack, cross-trailer charts, mobile layout, branded styling.
These are M11+ concerns.

### 6.12 M11 — task-oriented agent layer

New `panoptic_agent` service (13th, health + HTTP on `:8500`). Runs a
prompt-driven tool-use loop against the local vLLM (Gemma-4-26B-it)
and answers natural-language questions with evidence-first, structured
JSON responses. Not a chatbot — single-turn.

**Architecture:** operator_ui (M10) → agent (M11) → search_api (M10) →
Postgres/Qdrant. The agent never touches the DB; every read goes
through Search API endpoints. M10 is the first UI consumer; the
endpoint is callable by any client (CLI, Slack bot, automation).

**Stack:** FastAPI + `httpx` + vLLM's OpenAI-compatible endpoint.
**No** Anthropic/OpenAI SDK in this service today; `AGENT_BACKEND` env
switch reserves a path for a hosted-model swap (Claude / GPT-4) in
M11.1 without touching tools, dispatch, or UI.

**Tool surface (11 tools):** `search`, `verify`, `summarize_period`,
`get_trailer_day`, `get_fleet_overview`, `get_event`, `get_summary`,
`get_image`, `list_reports`, `get_report` (always available) +
`generate_daily_report` (gated — only exposed when the question
matches a report-intent regex; prevents "generate a report" as a
lazy resolution for uncertainty).

**Response contract:** `{answer: {narrative, evidence_bullets,
next_artifact?}, citations: [{type, id, marker}], scope_used, trace}`.
The `trace` has iterations, tool_call_count, per-tool latencies,
token counts, unverified_citations, and stop_reason. Every inline
`[<64-hex>]` marker in the narrative resolves to a clickable chip in
the UI.

**Guardrails:**
- post-hoc citation verifier: scans the answer for hex markers,
  resolves short prefixes to full 64-hex IDs via the trace, flags
  anything ambiguous or hallucinated as `unverified_citations`
- UI warning state: when `unverified_citations` is non-empty, the
  "Ask" panel renders with an orange border + banner; unresolved
  markers show as `.cite-unknown` spans (no link, amber background)
- system prompt enforces: hedged wording when evidence is partial,
  firm wording only when `verify` returned a supportive verdict,
  prefer scoped tools first, minimize tool calls
- dispatch layer re-checks `allow_write` even if the model invokes
  the write tool out of context
- max_iterations=8, max_tokens=1024, max_tool_output_chars=8000
- rate limit: 30 asks/min (soft in-memory)
- every `/v1/agent/ask` emits a structured JSONL audit record

**UI integration (M10):** "Ask about this day" panel on the
trailer-day page. Single-shot form; answer renders inline with
clickable citations, optional next_artifact button, and collapsible
trace `<details>`.

**Seed-question harness** (`tests/agent/seed_questions.yaml` +
`tests/agent/runner.py`): 9 questions across 7 categories (including
evidence-discipline / "no-evidence" probes that require hedged
language). Baseline **7 PASS · 2 FAIL** on local Gemma. Both failures
are vLLM context-window pressure on large trailer-day rollups; the
agent catches the 400 and emits a degraded JSON answer (no 503 to
the caller). Hosted-model swap is expected to fix both.

**Known limits:** see `docs/AGENT.md` §Known limitations. Core item:
Gemma occasionally truncates long hex IDs; the verifier resolves
tail-truncations but middle-truncations get flagged.

### 6.13 Bugs caught during M11

- **vLLM `response_format: json_object`** was initially passed to
  `/v1/chat/completions`; isn't universally supported across model
  backends and caused 400s on larger contexts. Dropped — the system
  prompt already constrains JSON-only output, and the loop has a
  parse-error nudge path for recovery.
- **Context-length 400 from vLLM** was surfacing as a 503 to the
  caller. Now caught inside the tool-use loop and returned as a
  structured degraded answer with `stop_reason=vllm_error_<code>`.

---

## 7. Known Gaps (Current)

| Gap | Severity | Notes |
|---|---|---|
| Off-box backup target not wired | medium | Backups live on the same disk as primary data. Needs SSH setup from Bryan to the DO gateway (or to a second box post-M6). See `docs/RESTORE.md` §Off-box backup. |
| No active paging on health_watch alerts | medium | Alerts go to stdout → cron `logs/cron.log`. No push (email/slack/pagerduty). Will mail via MAILTO env if user configures it. |
| Four bucket-marker types not derived | low | `drop`, `start`, `late_start`, `underperforming` are referenced by the summary agent's consumer branches but no derivation logic produces them (M8 D-1c). Branches fire only when derivation lands. |
| Real trailer has no alert/anomaly images yet | low | Trailer `1422725077375` has only baseline images so far (107, zero alerts/anomalies). We can't end-to-end test cross-source event cohesion until the anomaly detector fires. Raw camera_ids already match between payloads, so when the first alert does fire it should land on the existing bucket_marker scope_id automatically. |
| Postgres/Qdrant "slow but up" not characterized | low | Would look like a hang to the reclaimer; LEASE_TTL=120s eventually recovers but surfacing is poor. |
| Multi-Spark DB + image storage | deferred to M6 | Image files at `/data/panoptic-store/` are local-FS. NFS mount = zero code change. |
| Synthetic harness regressed 2 queries with hybrid retrieval | low | VL amplifies real over synthetic. Not worth tuning — real data is what matters. Worth re-scoring once we hand-label a batch of real-trailer images. |
| VL vs text rerank on real data not A/B'd | low | We've built VL rerank but haven't proven it wins over text on real surveillance imagery. Needs hand-labeled ground truth. |

---

## 8. Operator Cheatsheet

### Bring everything up on a fresh boot

```bash
# Store (Postgres, Qdrant, Redis)
cd ~/panoptic-store && docker compose up -d
# GPU services
cd ~/panoptic-retrieval && docker compose up -d
cd ~/panoptic-vllm && docker compose up -d
# Application (13 containers — M7 base + M8 event_producer + M9 report_generator + M10 operator_ui + M11 agent)
cd ~/panoptic && docker compose up -d
# Ingress tunnel
sudo systemctl start frpc
```

Dev-only alternative: `cd ~/panoptic && ./scripts/tmux-dev.sh` runs the
13 workers directly in a tmux session using the host venv. Don't run
both — they collide on ports.

### Status

```bash
~/panoptic/scripts/dashboard.sh              # all 13 workers + containers + disk
~/panoptic/scripts/watch_trailer.sh <serial> # live per-trailer view
```

### Trailer onboarding

```bash
cd ~/panoptic
.venv/bin/python scripts/add_trailer.py --serial <SN> --name "<label>"
# share PANOPTIC_SHARED_SECRET + https://panoptic.surveillx.ai + <SN> with the trailer team
```

### DLQ recovery

```bash
.venv/bin/python scripts/dlq_inspect.py                         # what's in DLQ
.venv/bin/python scripts/dlq_replay.py --job-id <uuid> --ack    # replay one
.venv/bin/python scripts/dlq_replay.py --job-type image_embed --all --ack  # drain a stream
```

### Retention + backup

```bash
# Dry-run (default) — prints what would be pruned
.venv/bin/python scripts/prune_images.py
.venv/bin/python scripts/prune_jobs.py
# Apply
.venv/bin/python scripts/prune_images.py --apply
.venv/bin/python scripts/prune_jobs.py --apply

# Manual backups (scheduled nightly from cron)
cd ~/panoptic-store && bash backup/pg_dump.sh        # DB dump
cd ~/panoptic-store && bash backup/qdrant_snapshot.sh # Qdrant snapshots

# Restore drill — see docs/RESTORE.md for full procedure
```

### Re-embed images (after model swap)

```bash
.venv/bin/python scripts/reembed_images.py         # only not-yet-embedded
.venv/bin/python scripts/reembed_images.py --force # every image
```

### Backfill events (after adding a camera alias or derivation change)

```bash
# Dry-run (default) — counts only, no writes
.venv/bin/python scripts/backfill_events.py --source all
# Apply — idempotent via content-addressed event_id
.venv/bin/python scripts/backfill_events.py --source all --apply
# Restrict to one serial
.venv/bin/python scripts/backfill_events.py --source image --serial <SN> --apply
```

### Generate reports (on-demand and cron)

```bash
# Daily on-demand via HTTP
curl -sSf -X POST http://localhost:8600/v1/reports/daily \
    -H 'Content-Type: application/json' \
    -d '{"serial_number":"<SN>","date":"2026-04-18"}'

# Poll status
curl -sSf http://localhost:8600/v1/reports/<report_id> | jq .

# Full-fleet dry-run (same shape as cron)
.venv/bin/python scripts/generate_reports.py --daily --all-active --date 2026-04-18
.venv/bin/python scripts/generate_reports.py --daily --all-active --date 2026-04-18 --apply

# Weekly
.venv/bin/python scripts/generate_reports.py --weekly --all-active --iso-week 2026W16 --apply

# Prune expired HTML (90-day default)
.venv/bin/python scripts/prune_reports.py --dry-run
.venv/bin/python scripts/prune_reports.py --apply
```

Cron entries in `docs/REPORTS.md`.

### Operator UI (M10)

```bash
# Start the UI — same as any other service
docker compose up -d operator_ui
# Open a trailer day view
open http://localhost:8400/trailer/YARD-A-001/2026-04-18
```

Fleet / search / detail pages documented in `docs/OPERATOR_UI.md`.

### Agent (M11)

```bash
# Start the agent service
docker compose up -d agent
curl -sS http://localhost:8500/healthz | jq .

# Single-shot /ask via curl
curl -sS -X POST http://localhost:8500/v1/agent/ask \
    -H 'Content-Type: application/json' \
    -d '{"question":"What happened today?",
         "scope":{"serial_number":"YARD-A-001","date":"2026-04-18"}}'

# Baseline seed-question harness
.venv/bin/python tests/agent/runner.py
```

Tools, guardrails, access model in `docs/AGENT.md`.

### Live smoke

```bash
curl https://panoptic.surveillx.ai/health    # proves ingress end-to-end
.venv/bin/python scripts/dev_fake_trailer.py # signed push through full pipeline
.venv/bin/python tests/relevance/runner.py   # relevance harness
```

---

## 9. Git History (session)

Through 2026-04-19 — M9 report generation landed on top of the M8
event layer. Uncommitted at time of this refresh; will be committed as
a single `feat(M9)` commit at end-of-phase.

Highlights (prior commits):

```
9ead12c feat(M8): unified event layer — panoptic_events table + producer
8c79847 feat(M7): containerize the full worker stack
74edd77 feat(ops): backup + restore hardening — retention, freshness check, docs
```

Earlier in the day:

```
74dbda9 docs(M4): failure_modes — empirical results from 4 dep-outage tests
7bb8144 feat(M4): DLQ tooling + Redis outage resilience in workers
ef29bc6 feat(M5): hybrid retrieval — Search API queries caption + VL spaces
936214a feat(M5): VL-native image embedding path
d9f9b42 fix(intake): clamp duty_cycle into [0,1]
910849c fix(schema): mean_count + std_dev_count nullable too
a4ca81e fix(schema): bucket_minutes + anomaly_flag default on missing
58ed4cc feat(dev): search_api warmup + PANOPTIC_CONTINUUM_DISABLED
0d98429 docs: refresh STATUS.md + add watch_trailer.sh
55169cd feat(ingress): frpc tunnel to DO gateway + bucket schema nullables
593f523 feat(M2): /healthz everywhere + dashboard + lease reclaimer process
a5f0efb feat(M2): HMAC-signed trailer push auth
18a08df feat(M1): relevance harness + synthetic seeder + idempotency tests
fe3e9b5 chore: .gitignore + .env.example + stale vlm model refs
```

~7400 lines added across 69 files.

---

## 10. What the Next Session Should Pick Up

M1–M5 + M7 + M8 + M9 + M10 + M11 are all done. The remaining roadmap
items plus the known operational gaps:

**Off-box backup (top of list):**
- Set up SSH key from Spark → DO gateway droplet (user action).
- Add nightly rsync of `/data/panoptic-store/backups/` (DB dumps, ~200KB
  each, trivial over WAN).
- Qdrant snapshots: defer WAN shipment until they're small enough to
  justify it, or wait for the post-M6 second box with LAN access.

**M6 prep (blocked on hardware inventory):**
- Inventory the pre-Spark Ubuntu box Bryan mentioned — check specs
  against the sizing table in the store-migration design.
- Decide Tailscale MagicDNS vs DHCP + router DNS for service discovery.
- Rehearse backup/restore on the target host before migrating live data.

**M7 — containerize workers (✅ done):**
Dockerfile + docker-compose.yml landed. 10 services (webhook, 8 workers,
search_api) on host network mode, source bind-mounted for dev iteration,
json-file log rotation (50MB × 5 files). `tmux-dev.sh` kept as an
alternative for deep code-edit loops. Full stack healthy end-to-end on
first boot.

**M8 — unified event layer (✅ done):**
`panoptic_events` table + producer worker landed. See §6.9. Known
follow-ons: (a) insert the camera alias row for trailer `1422725077375`
once Bryan decides the canonical id (gap in §7); (b) write derivation
logic for `drop`/`start`/`late_start`/`underperforming` if we want
those bucket markers in v2.

**M9 — async report generation (✅ done):**
New `panoptic_report_generator` worker + `panoptic_reports` table +
four HTTP endpoints. See §6.10 and `docs/REPORTS.md`. Pick up next with:
install the cron entries, generate a first real batch for the active
fleet, iterate on the HTML template based on what operators find
useful. M9.1 candidates (all explicitly deferred): verify-integration
on headlines, PDF export, email/Slack delivery, branded styling,
on-the-fly missing-day synthesis in weekly.

**M10 — operator evidence browser (✅ done):**
New `panoptic_operator_ui` service + 9 new Search API endpoints.
See §6.11 and `docs/OPERATOR_UI.md`.

**M11 — agent layer (✅ done):**
New `panoptic_agent` service over the local vLLM. Same tool surface a
hosted-model agent would want. Seed baseline 7/9 on Gemma. See §6.12
and `docs/AGENT.md`. Pick up next with M11.1 hosted-model swap
(AGENT_BACKEND=claude) whenever real customer demo needs the quality
step-up, or with M12 expanded bucket-marker derivation.

**M12 — expanded bucket-marker derivation (next milestone):**
Add derivation logic for `drop`, `start`, `late_start`,
`underperforming` markers. Consumer branches already exist in the
summary agent; today only `spike` + `after_hours` are produced. Kickoff
plan needed before implementation.

**Optional — VL rerank A/B:**
- Hand-label ~20 real-trailer queries with ground-truth relevant images.
- Run both `SEARCH_RERANK_MODE=text` and `=vl` against that set.
- If VL wins materially, flip the default. If it's a wash on our
  specific imagery, save the compute.

**Operational follow-ups (not blockers):**
- Wire a push target for health_watch alerts (email via MAILTO is
  a 1-line change; slack/pagerduty is incremental).
- Quarterly restore drill rehearsal — put on the calendar so the
  backups stay trust-worthy.
