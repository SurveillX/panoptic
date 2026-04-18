# Panoptic — System Status (2026-04-18)

Briefing document for external AI collaborators and future-self sessions.
Self-contained: a cold reader should be able to reason about next steps
without prior context.

---

## 1. What Panoptic Is

Edge-to-cloud surveillance analytics. Fleet of trailers (Jetson-based
units with 8 cameras) run local perception (`cognia`) and push two kinds
of signed HTTP payloads to a central processing stack:

1. **15-minute detection buckets** — aggregated per-object-type stats
   (counts, confidence, duty cycle, anomaly score, etc.) per camera.
2. **Images** — JPEGs on alert / anomaly / baseline triggers with
   metadata binding each image to a bucket.

The central stack authenticates + dedup-checks the push, captions
images via Gemma-4-26b (vision), summarizes buckets via Gemma text (with
optional keyframe context from the trailer's Continuum), embeds captions
and summaries with Qwen3-Embedding-8B, stores vectors in Qdrant, and
serves semantic search over the history via the Search API.

---

## 2. Hardware

- **DGX Spark** (GB10, Blackwell-class, 128 GB unified CPU/GPU memory).
  Single application + data host today. `python3.12.3`, Ubuntu Noble,
  aarch64.
- **DigitalOcean droplet `surveillx-gateway`** (public gateway): runs
  Caddy (TLS termination, on-demand Let's Encrypt) + FRP server
  (`frps`). Reserved IP `134.199.244.90`. Public hostnames:
  `panoptic.surveillx.ai`, `agent.surveillx.ai`, `*.trailers.surveillx.ai`.
- **Planned**: separate the data tier from the Spark to a dedicated
  LAN-local box once the first trailer is stable (M6).

Current unified-memory usage: ~73 GB of 121 GB; ~48 GB free.

---

## 3. Repo Layout

Four repos, all deployed locally on the Spark, all on `main`. HTTP
between tiers, no shared Python packages.

| Repo | Role | Deployment |
|---|---|---|
| `panoptic` | Application: workers, webhook, Search API, DB schema, reclaimer, HMAC auth, health/dashboard | Python venv (tmux dev session) |
| `panoptic-vllm` | LLM serving (Gemma-4-26b-it via vLLM) | Docker compose |
| `panoptic-retrieval` | Text + VL embed/rerank service (Qwen3 models, fp8) | Docker compose |
| `panoptic-store` | Postgres + Qdrant + Redis | Docker compose |

Image files live at `/data/panoptic-store/images/<serial>/<camera>/<yyyy>/<mm>/<dd>/<image_id>.jpg`.

**Key design property**: all stateful and service URLs come from env
vars (`DATABASE_URL`, `REDIS_URL`, `QDRANT_URL`, `VLLM_BASE_URL`,
`RETRIEVAL_BASE_URL`, `IMAGE_STORAGE_ROOT`). `/etc/hosts` maps
`panoptic-store → 127.0.0.1` today. Moving the data tier later is a
hostname change, not code.

---

## 4. Runtime State

### 4.1 Containerized services

| Container | Port(s) | Role |
|---|---|---|
| `panoptic-vllm` | 8000 | `gemma-4-26b-it` (multimodal) |
| `panoptic-retrieval-retrieval-1` | 8700 | Qwen3 embed (dim 4096) + rerank, VL embed + rerank |
| `panoptic-postgres` | 5432 | Postgres 16 |
| `panoptic-qdrant` | 6333 / 6334 | Qdrant v1.13.6 |
| `panoptic-redis` | 6379 | Redis 7 |

### 4.2 Application processes (tmux session `panoptic`, 8 windows)

| Window | Port | Role |
|---|---|---|
| `webhook` | 8100 | Trailer ingest (FastAPI, HMAC middleware) |
| `caption` | 8201 (healthz) | Image captions (Gemma vision) |
| `cap_embed` | 8202 | Caption → Qdrant `image_caption_vectors` |
| `summary` | 8203 | Bucket summaries (Gemma text, optional keyframes) |
| `sum_embed` | 8204 | Summary → Qdrant `panoptic_summaries` |
| `rollup` | 8205 | Multi-level rollups |
| `reclaimer` | 8210 | Lease expiry recovery + stream re-enqueue (30s tick) |
| `search` | 8600 | Search API |

Supervised by `scripts/tmux-dev.sh`; logs tee'd to `~/panoptic/logs/*.log`.
`logrotate` installed (daily, 14 copies, copytruncate) at
`/etc/logrotate.d/panoptic`.

### 4.3 Edge infrastructure

- **Caddyfile** (on DO droplet, `~/surveillx-gateway/caddy/Caddyfile`):
  on-demand TLS for `panoptic.surveillx.ai`, `agent.surveillx.ai`,
  `*.trailers.surveillx.ai`. Proxies to `frps:8080`.
- **frps** (on DO droplet, `~/surveillx-gateway/frp/frps.toml`): port
  7000 for client dial-in, port 8080 for vhost HTTP, token auth.
- **frpc** (systemd service on this Spark, config
  `/etc/frp/frpc.toml`, repo template
  `~/panoptic/deploy/frpc/frpc.toml.template` + gitignored
  `auth_token.txt`): dials the DO droplet, registers vhost
  `panoptic.surveillx.ai → 127.0.0.1:8100`.

End-to-end proven: `curl https://panoptic.surveillx.ai/health` from any
network returns the webhook's live health snapshot.

### 4.4 Persistent state

| Table / Collection | Count (approx) | Purpose |
|---|---|---|
| `panoptic_buckets` | ~45 | Ingested buckets |
| `panoptic_images` | ~52 | Captioned images |
| `panoptic_summaries` | ~45 | Bucket summaries |
| `panoptic_jobs` | ~200 | Job state (leases) |
| `panoptic_trailers` | 6 active | Known-trailer registry |
| `panoptic_job_history` | ~N | State transition log |
| `image_caption_vectors` (Qdrant) | ~52 points, dim 4096 | Image caption embeddings |
| `panoptic_summaries` (Qdrant) | ~45 points, dim 4096 | Summary embeddings |

Alembic at migration `006`.

---

## 5. What's Proven End-to-End (2026-04-18)

- **Authenticated signed trailer push over the public WAN**:
  `POST https://panoptic.surveillx.ai/v1/trailer/bucket-notification`
  with HMAC-SHA256 signature → 200 accepted (Caddy → FRP → Spark
  webhook → auth verified → Redis fragment stored → finalizer → job
  queued → summary → embedding → Qdrant).
- **Unauthenticated / bad-signature / unregistered-serial** all
  rejected correctly (401 or 403) with no body leakage.
- **Relevance harness**: 17 named queries against the seeded synthetic
  dataset. 16 PASS / 1 WARN / 0 FAIL, p50 latency ~300 ms.
- **At-least-once delivery**: crashed-worker scenarios recovered
  automatically by the reclaimer process (resets Postgres state +
  re-XADDs to the stream). Verified by intentional SIGINT mid-job.
- **Idempotency**: replayed identical `event_id` is rejected at Redis
  SETNX (bucket) or Postgres PK (image) — 0 duplicates, 0 duplicate
  Qdrant points.
- **Observability dashboard**: all 8 services report healthy, deps
  reachable, queue lag visible. `scripts/dashboard.sh` returns exit 0.

---

## 6. Where We Are in the Roadmap

Source of truth: `NEXT_STEPS.md` (v2). Milestones:

| # | Milestone | Status |
|---|---|---|
| M1 | Search API live + ingest→query proof + relevance harness + idempotency sanity + `docs/M1_RESULTS.md` | **done** |
| M2 | Webhook auth + minimum observability | **done** — HMAC middleware, `panoptic_trailers` registry, `/healthz` + dashboard, reclaimer scheduled, frpc tunnel live |
| M3 | Onboard one real trailer | **in flight** — trailer serial `1422725077375` registered; trailer team implementing signing client; waiting on first real bucket |
| M4 | Full idempotency + crash-recovery validation | pending |
| M5 | VL image retrieval | pending |
| M6 | Move panoptic-store to dedicated machine | pending |
| M7 | Containerize workers | pending |

---

## 7. Key Design Decisions Locked In

1. **Content-addressed IDs.** `image_id`, `summary_id`, `bucket_id` all
   SHA256 of canonical JSON including `serial_number`. Multi-Spark
   collision-safe by construction.
2. **Redis streams + Postgres leases.** Consumer groups for delivery
   pooling; Postgres leases are the authoritative double-execution
   guard. Reclaimer handles both layers (XAUTOCLAIM for stream PEL,
   `FOR UPDATE SKIP LOCKED` for Postgres). Re-enqueue is the
   reclaimer's job (not the "orchestrator" the docstring references).
3. **Shared fleet secret auth** (per `docs/AUTH_DESIGN.md` v2). Not
   per-trailer. HMAC-SHA256 over `serial|timestamp|method|path|body_sha256`,
   dual-secret rotation from day one, 5-minute skew window, 10-minute
   replay cache, `panoptic_trailers.is_active` for revocation.
4. **HTTP between tiers, no shared packages.** Each repo independent.
5. **Hostname-based addressing.** `panoptic-store` resolves to 127.0.0.1
   today; one `/etc/hosts` change for M6.
6. **Workers native in dev** (tmux + venv). Containerization is M7.
7. **fp8 retrieval models**, all 4 resident in ~30 GB on the Spark.
8. **Public ingress via Caddy on DO droplet → FRP → frpc on Spark.**
   Auth enforced inside the app (webhook middleware), not at the edge.

---

## 8. Known Gaps (Ordered)

These surfaced during M1/M2 and are explicitly still open.

| Gap | Severity | Notes |
|---|---|---|
| Search API first-query compile stall (~102 s) | medium | Known torch.compile cost on reranker. Planned fix: warm-up ping at Search API startup. ~15 min. |
| Continuum fetch SSL timeouts in dev | low | Summary worker tries to fetch keyframes from fake trailer hostnames, takes 30-60s per bucket to fall back to metadata-only. `CONTINUUM_DISABLED=1` flag would skip. |
| Multi-Spark storage story | deferred to M6 | `/data/panoptic-store/images/` is local FS. NFS mount = zero code change when the data tier moves. Object-storage (MinIO/S3) is strictly later. |
| `dev_reclaim_test.py` respawn step sometimes hangs | low | Recovery mechanism itself verified fine. Test script's `tmux respawn-pane` occasionally stalls under subprocess inheritance. Added timeouts; not a blocker. |
| Search API `/v1/search/verify` + `/v1/summarize/period` untested | low | Endpoints implemented, not yet smoke-tested against seeded data. |
| `fire` vs `smoke` cross-retrieval | deferred | Caption text overlaps semantically. Expected to improve with real imagery + VL retrieval in M5. |
| Docker image logs | deferred | `panoptic-vllm` / `panoptic-retrieval` / store containers rely on Docker's default logging. Add `max-size` options if they grow meaningfully. |

---

## 9. Operator Commands

Bring everything up on a fresh boot:

```bash
# store (Postgres, Qdrant, Redis)
cd ~/panoptic-store && docker compose up -d

# GPU services (each in their own repo)
cd ~/panoptic-retrieval && docker compose up -d
cd ~/panoptic-vllm && docker compose up -d

# application (8 workers in tmux)
cd ~/panoptic && ./scripts/tmux-dev.sh
```

Check status:

```bash
~/panoptic/scripts/dashboard.sh
```

Register a new trailer:

```bash
.venv/bin/python scripts/add_trailer.py --serial <SN> --name "<label>"
```

Watch a specific trailer (ad-hoc):

```bash
tail -f ~/panoptic/logs/webhook.log | grep <SN>
```

Install / reinstall frpc:

```bash
echo "<token>" > ~/panoptic/deploy/frpc/auth_token.txt
chmod 600 ~/panoptic/deploy/frpc/auth_token.txt
~/panoptic/deploy/frpc/install.sh
```

---

## 10. Git State

All four repos on `main`. Latest head:

```
panoptic            55169cd feat(ingress): frpc tunnel to DO gateway + bucket schema nullables
panoptic-store      3ad87a4 Initial commit — docker-compose for Postgres + Qdrant + Redis
panoptic-retrieval  (pre-Spark head, see that repo)
panoptic-vllm       (pre-Spark head, see that repo)
```

All three app-facing repos pushed to `github.com/SurveillX/*`.
`panoptic-store`'s `.env` (with Postgres password) is gitignored.
`panoptic`'s `.env` (DB URL + HMAC secret) is gitignored.
`deploy/frpc/auth_token.txt` (FRP server token) is gitignored.

---

## 11. Live Smoke Command

Single command that proves the whole stack is up + authenticated
pushes work:

```bash
cd ~/panoptic && set -a && . ./.env && set +a && .venv/bin/python scripts/dev_fake_trailer.py
```

Expected: `bucket: 200 {"status":"accepted",...}` and
`image: 200 {"status":"accepted","image_id":"..."}`.
