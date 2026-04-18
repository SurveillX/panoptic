# Panoptic — System Status (2026-04-18, end of day, refresh #2)

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
each image natively with Qwen3-VL-Embedding-8B** (M5), and serves
hybrid semantic search over the history via the Search API.

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

### 4.2 Application processes (tmux session `panoptic`, **9 windows**)

| Window | Port | Role |
|---|---|---|
| `webhook` | 8100 | Trailer ingest (FastAPI, HMAC middleware) |
| `caption` | 8201 | Image captions (Gemma vision) |
| `cap_embed` | 8202 | Caption → Qdrant `image_caption_vectors` |
| `img_embed` | 8206 | **VL pixels → Qdrant `panoptic_image_vectors` (M5)** |
| `summary` | 8203 | Bucket summaries (Gemma text, optional keyframes) |
| `sum_embed` | 8204 | Summary → Qdrant `panoptic_summaries` |
| `rollup` | 8205 | Multi-level rollups |
| `reclaimer` | 8210 | Lease expiry recovery + stream re-enqueue (30 s tick) |
| `search` | 8600 | Search API (hybrid retrieval: caption + VL) |

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
| `panoptic_jobs` | ~885 | all terminal (succeeded / failed_terminal / degraded) |
| `panoptic_trailers` | 6 active | registry (real trailer + 4 synthetic + SMOKE-TEST) |
| `image_caption_vectors` (Qdrant, 4096-dim cosine) | 141 pts | caption-text embeddings |
| `panoptic_image_vectors` (Qdrant, 4096-dim cosine) | 141 pts | VL-native image embeddings (M5) |
| `panoptic_summaries` (Qdrant, 4096-dim cosine) | 233 pts | summary-text embeddings |

Alembic at migration **007**.

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
| M7 | Containerize workers | ✅ done — `docker compose up -d` brings up the 9-service stack, all healthy, end-to-end push + search verified through containers |

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

---

## 7. Known Gaps (Current)

| Gap | Severity | Notes |
|---|---|---|
| Off-box backup target not wired | medium | Backups live on the same disk as primary data. Needs SSH setup from Bryan to the DO gateway (or to a second box post-M6). See `docs/RESTORE.md` §Off-box backup. |
| No active paging on health_watch alerts | medium | Alerts go to stdout → cron `logs/cron.log`. No push (email/slack/pagerduty). Will mail via MAILTO env if user configures it. |
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
# Application (9 containers — M7)
cd ~/panoptic && docker compose up -d
# Ingress tunnel
sudo systemctl start frpc
```

Dev-only alternative: `cd ~/panoptic && ./scripts/tmux-dev.sh` runs the
9 workers directly in a tmux session using the host venv. Don't run
both — they collide on ports.

### Status

```bash
~/panoptic/scripts/dashboard.sh              # all 9 workers + containers + disk
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

### Live smoke

```bash
curl https://panoptic.surveillx.ai/health    # proves ingress end-to-end
.venv/bin/python scripts/dev_fake_trailer.py # signed push through full pipeline
.venv/bin/python tests/relevance/runner.py   # relevance harness
```

---

## 9. Git History (session)

14 commits on `main` today, starting from yesterday's head
`203fd24`. Highlights:

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

M1–M5 are all done. The remaining roadmap items plus the known
operational gaps:

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
Dockerfile + docker-compose.yml landed. 9 services (webhook, 7 workers,
search_api) on host network mode, source bind-mounted for dev iteration,
json-file log rotation (50MB × 5 files). `tmux-dev.sh` kept as an
alternative for deep code-edit loops. Full stack healthy end-to-end on
first boot.

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
