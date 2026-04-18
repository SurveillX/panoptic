# Panoptic — Next Steps (v2)

## Context

Panoptic is running on the Spark with the core architecture proven:

* trailer bucket ingest
* trailer image ingest
* image captioning
* bucket summarization
* summary and caption embeddings
* retrieval service and vector search infrastructure
* separate repos for `panoptic`, `panoptic-vllm`, `panoptic-retrieval`,
  and `panoptic-store`

The trailer push contract is stable and well-defined: 15-minute bucket
pushes, selected image pushes on alert/anomaly/baseline triggers,
Redis-backed retry and DLQ, at-least-once delivery semantics.

The strategic goal is no longer "prove the architecture exists." It is:

**prove the product loop works end-to-end and is safe enough to onboard
one real trailer.**

## Primary objective

Show that pushed trailer data becomes useful, searchable intelligence
end-to-end — ingested, processed, vectorized, queryable, observable,
and secure enough to accept one real trailer.

---

# Milestone 1 — Search API live + ingest→query proof (strengthened, bounded)

## Goal

Prove the product loop with enough data to judge quality, and close
the cheapest idempotency gaps before any real trailer traffic.

## Scope

* Search API running in the active runtime (tmux window alongside the
  existing 6 workers)
* Synthetic-data seeder producing ~20 varied trailer payloads
* Ingest → process → store → search proof for both the image path and
  the summary path
* Tiny relevance harness (10–20 named queries, expected hits,
  pass/fail scoreboard)
* Two idempotency sanity checks
* `docs/M1_RESULTS.md` capturing all outputs

## Explicitly out of scope

* Retrieval quality tuning (can be its own optional milestone later)
* Full failure-mode testing — stays in M4
* Auth, HTTPS, observability — stays in M2

## Required deliverables

1. **Synthetic-data seeder** — `scripts/seed_synthetic.py` (or flags
   extending `scripts/dev_fake_trailer.py`) that pushes ~20 varied
   payloads across multiple cameras, triggers, and visual scenes.
2. **Search API live** — added as a 7th window in
   `scripts/tmux-dev.sh`, reachable at `localhost:8600`.
3. **Relevance harness** — `tests/relevance/queries.yaml` +
   `tests/relevance/runner.py` that runs named queries against the
   Search API and prints a scoreboard against expected hits.
4. **Idempotency sanity tests** (floor, not ceiling):
   * Replay the same seeded webhook payload (same `event_id`) →
     verify single Postgres row, single Qdrant point, no double
     caption.
   * Manually SIGINT one worker mid-job → verify the lease reclaimer
     completes the job on retry with no duplicates.
5. **`docs/M1_RESULTS.md`** with:
   * Dataset description (how seeded, how many, what variety)
   * Query list + expected hits
   * Harness scoreboard
   * Idempotency sanity-check results
   * Any retrieval-quality surprises worth flagging for a later
     tuning milestone

## Success criteria

* Search API live and reachable
* Both image-backed and summary-backed searches return relevant results
  against seeded data
* Relevance harness runs cleanly and produces a scoreboard
* Both idempotency sanity checks pass
* `docs/M1_RESULTS.md` exists and captures the above

---

# Milestone 2 — Webhook auth + minimum observability

## Goal

Make the ingest path safe enough and visible enough to accept a real
trailer.

## Required design decisions BEFORE implementation begins

### Auth design

M2 implementation may not start until these are written down and
approved:

* **Mechanism** — bearer token, API key, mTLS, or HMAC signature
* **Credential scoping** — per-trailer (keyed by `serial_number`) vs
  shared credential
* **Rotation strategy** — how a compromised credential is replaced
  without fleet-wide downtime
* **Transport** — HTTPS termination at uvicorn, reverse proxy (caddy
  /nginx), or infrastructure tunnel

**Default recommendation for review:** per-trailer bearer token tied
to `serial_number`, hashed-at-rest in Postgres, rotatable via admin
endpoint. HTTPS via reverse proxy (caddy) rather than in-app, so
auth logic stays transport-agnostic.

Output: `docs/AUTH_DESIGN.md` before coding starts.

### Observability mechanism

Cheapest acceptable first-pass (explicitly chosen, not left open):

* `/healthz` endpoint on every worker (extending the webhook pattern
  that already exists) — JSON response with: last-job timestamp,
  consumer-group lag, dependency status (Redis/Postgres/Qdrant/vLLM
  /retrieval reachable)
* `scripts/dashboard.sh` — curls every `/healthz`, runs
  `docker stats --no-stream`, runs `df -h /data`, prints a one-page
  status
* `logrotate` config for `~/panoptic/logs/*.log`
* Basic timing lives in existing log lines (already present for the
  caption/summary/embed paths)

**Do NOT** graduate to Prometheus/Grafana/full monitoring stack
until M3 real-trailer traffic reveals the simpler mechanism is
inadequate.

Output: `docs/OBSERVABILITY_DESIGN.md` before coding starts.

## Scope

* Implement the chosen auth on both webhook endpoints
  (`/v1/trailer/bucket-notification`, `/v1/trailer/image`)
* Implement `/healthz` on all workers and webhook
* Implement `scripts/dashboard.sh`
* Add `logrotate` config
* Update `scripts/dev_fake_trailer.py` to pass the new credentials
* Smoke-test: unauthorized request rejected, authorized request
  succeeds

## Success criteria

* Unauthorized webhook requests rejected (401/403)
* Authorized pushes still succeed end-to-end through M1's pipeline
* `/healthz` endpoints respond accurately for every worker
* Dashboard script gives a useful one-shot status
* Logs rotate under `logrotate` control
* `docs/AUTH_DESIGN.md` and `docs/OBSERVABILITY_DESIGN.md` both exist

---

# Milestone 3 — Onboard one real trailer

## Goal

Validate the full product loop against real trailer data.

## Conditional dependency

Requires: a real trailer ready for onboarding when M2 completes.

If hardware is not ready, do not stall the roadmap — extend M1's
synthetic data campaign (more varied scenes, longer run times,
relevance harness iteration) and defer M3. Do not skip M3 to reach
M4/M5 early.

## Scope

* Onboard exactly one trailer
* Validate: bucket push, image push, retries, DLQ behavior, search
  usefulness, image and summary quality
* Intentionally narrow — no multi-trailer parallelization

## Success criteria

* One real trailer pushes unattended for at least ~1 week
* Both bucket and image paths work in practice
* At least one real operator-style query returns useful results over
  real trailer data
* No major stability surprises in queues, workers, storage, or
  captioning quality

---

# Milestone 4 — Full idempotency + crash-recovery validation

## Goal

Harden duplicate handling and failure recovery for multi-trailer scale.

Builds on M1's sanity-check floor. M4 is the full campaign.

## Scope

* DLQ replay testing
* Worker restart storms under load
* Duplicate webhook floods
* Dependency outage simulations:
  * Redis down
  * Postgres connection loss
  * Qdrant down
  * vLLM / retrieval service outage
* Long-quiet-then-burst patterns against the bucket finalizer
* Lease reclaimer behavior under contention

## Success criteria

* Repeated delivery of the same payload produces no logical
  duplicates in Postgres or Qdrant
* Worker crashes do not poison streams or leave orphaned leases
* Dependency outages degrade gracefully with documented behavior per
  dependency
* Recovery paths documented and rehearsed in `docs/FAILURE_MODES.md`

---

# Milestone 5 — VL image retrieval

## Goal

Upgrade Panoptic from caption-only to image-native retrieval.

The retrieval service already hosts the Qwen3-VL models and was
verified (2026-04-17) to produce 4096-dim vectors with correct
cross-modal ranking. Panoptic-side integration is the missing piece.

## Scope

* `shared/clients/vl_embedding.py` (mirrors text embedding client, POSTs
  `/embed_visual`)
* `shared/clients/vl_reranker.py` (mirrors text reranker, POSTs
  `/rerank_visual`)
* New Qdrant collection `panoptic_image_vectors` (4096-dim cosine)
* New worker `services/panoptic_image_embed_worker/` — reads JPEG from
  `panoptic_images.storage_path`, POSTs `/embed_visual`, upserts Qdrant
* New job type `image_embed` in `shared/utils/streams.py`
* Alembic migration adding `image_embedding_{status,model,vector_id}`
  columns to `panoptic_images`
* Search API image branch — combines caption-based and image-native
  retrieval, merges ranked results
* `scripts/reembed_images.py` — rebuild visual embeddings when the
  model or schema changes

## Success criteria

* Stored pushed images embedded natively into Qdrant
* At least one named query in M1's relevance harness demonstrably
  benefits from VL retrieval vs caption-only (scoreboard delta)
* Combined ranking (caption + VL) works cleanly in the Search API

---

# Milestone 6 — Move `panoptic-store` to dedicated machine

## Goal

Separate the data tier from Spark compute.

## Required prework

* Service-discovery decision written down: Tailscale MagicDNS vs DHCP
  reservation + router DNS vs manual `/etc/hosts` sync across Sparks
* Backup/restore drill documented and executed against current single-
  box setup before migrating

## Scope

* Identify target machine (possibly the pre-Spark Ubuntu box, if specs
  are adequate — see the `STATUS.md` sizing table)
* Run `~/panoptic-store/provision.sh` on target
* `rsync -a /data/panoptic-store/ <target>:/data/panoptic-store/`
* Update `/etc/hosts` on the Spark: `panoptic-store → <target IP>`
* Update `~/panoptic-store/.env.example` and `docs/STORE_MIGRATION.md`
* Validate full ingest + search stack against new host

## Success criteria

* Panoptic code unchanged after migration (hostname-only diff)
* Ingest, search, and worker flows still work against new store
* Backup/recovery rehearsal on the new host passes
* Real trailer traffic (from M3) uninterrupted through the cutover, or
  has a documented planned-downtime window

---

# Milestone 7 — Containerize Panoptic workers

## Goal

Bring worker/runtime management in line with `panoptic-vllm`,
`panoptic-retrieval`, and `panoptic-store`. One-command bring-up across
all four repos.

## Scope

* `Dockerfile` at `~/panoptic/` root (base image, venv, deps)
* `docker-compose.yml` at `~/panoptic/` root — one service per worker
  plus `trailer_webhook` and `search_api`
* Decision during M7: bind-mount source vs bake into image (affects
  iteration speed vs reproducibility)
* Documented dependency ordering (workers wait on `panoptic-store`
  health)
* Env-driven config (already in place — verify coverage)
* `scripts/tmux-dev.sh` marked dev-only; compose becomes default

## Success criteria

* `cd ~/panoptic && docker compose up -d` brings up the full app stack
* `docker compose ps` shows all green
* No observable regression vs the native-tmux baseline
* Local dev path (tmux) remains usable for active code editing

---

# Recommended order

## Do now

1. M1 — Search API live + ingest→query proof (strengthened, bounded)
2. M2 — Webhook auth + minimum observability (design decisions first)
3. M3 — One real trailer onboarding (conditional on hardware)
4. M4 — Full idempotency + crash-recovery validation

## Do next

5. M5 — VL image retrieval
6. M6 — Dedicated `panoptic-store` machine
7. M7 — Containerize workers

---

# Deferred items

Explicitly deferred, not forgotten:

1. **Multi-Spark rollout** — wait until one trailer is stable, product
   loop is proven, and duplicate/recovery handling is explicit.
2. **Object storage migration** — NFS-style shared-path semantics
   first; revisit object storage later only if NFS proves inadequate.
3. **Broader report generation** — wait until search is proven on real
   trailer data and VL retrieval direction is validated.
4. **UI / agent-layer expansion** — the backend intelligence loop
   needs to be trusted first.
5. **Full monitoring stack (Prometheus / Grafana)** — do not overbuild
   before M3. Start with the minimum observability set in M2 and
   graduate only if signal demands it.

---

# Minimum observability required before one real trailer

Restated for clarity — the M2 deliverables cover all of these:

* queue depth visibility (`/healthz` consumer-group lag)
* worker alive/heartbeat visibility (`/healthz` last-job timestamp)
* dependency health visibility (`/healthz` reachability checks)
* disk usage visibility (`scripts/dashboard.sh` `df -h /data`)
* log rotation (`logrotate` config)
* basic inference/search latency visibility (existing log lines)

That is the floor. Anything more is deferred.

---

# Short version

1. **Search API live + ingest→query proof + relevance harness +
   synthetic seeder + idempotency sanity + `docs/M1_RESULTS.md`**
2. **Webhook auth + minimum observability** (with `docs/AUTH_DESIGN.md`
   and `docs/OBSERVABILITY_DESIGN.md` locked before coding)
3. **One real trailer onboarding** (conditional on hardware)
4. **Full idempotency + crash-recovery**
5. **VL image retrieval**
6. **Dedicated `panoptic-store` machine**
7. **Worker containerization**

The cleanest path from "the architecture works" to "the product loop
is proven and safe to scale."
