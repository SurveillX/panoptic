# Observability — design (v1)

**Status:** draft, pending review.
**Scope:** M2 prerequisite per `NEXT_STEPS.md` v2. "Minimum useful
observability" — the floor needed to run unattended with one real
trailer, not a full monitoring stack.

---

## 1. Purpose

Cheap visibility into the running stack so we're not flying blind.

Design goals:

* know if every worker is alive and progressing
* see queue depths / consumer lag at a glance
* see dependency health (Redis / Postgres / Qdrant / vLLM / retrieval)
* catch disk pressure before it breaks ingest
* keep logs bounded

Non-goals (explicitly deferred):

* Prometheus / Grafana / Datadog / full metrics stack
* distributed tracing (OpenTelemetry)
* per-request histograms / percentiles
* paging / alerting infrastructure
* custom dashboards

Re-evaluate those once M3 real-trailer traffic shows the simpler
mechanism is inadequate.

---

## 2. Shape

Three pieces, all on the Spark, nothing external:

1. **`/healthz` on every worker and service.** JSON status endpoint on
   each process. Existing `/health` on webhook + search_api + retrieval
   + vLLM stay as-is.
2. **`scripts/dashboard.sh`.** One-command terminal dashboard that
   curls every endpoint and prints a one-page status summary.
3. **`logrotate` config.** Daily rotation + 14-copy retention for
   `~/panoptic/logs/*.log`.

---

## 3. `/healthz` endpoints

### 3.1 Port assignments

| Service | HTTP port | Health path |
|---|---|---|
| trailer_webhook | 8100 | `/health` *(existing, extend contents)* |
| search_api | 8600 | `/health` *(existing, extend contents)* |
| panoptic_image_caption_worker | **8201** | `/healthz` *(new)* |
| panoptic_caption_embed_worker | **8202** | `/healthz` *(new)* |
| panoptic_summary_agent | **8203** | `/healthz` *(new)* |
| panoptic_embedding_worker | **8204** | `/healthz` *(new)* |
| panoptic_rollup_worker | **8205** | `/healthz` *(new)* |
| panoptic_reclaimer *(M2 finding 1, separate doc)* | **8210** | `/healthz` |

Ports 8200–8299 reserved for worker health endpoints. Avoids collision
with webhook (8100), search_api (8600), retrieval (8700), vLLM (8000).

Env var per worker: `<NAME>_HEALTH_PORT=<default>`. Override if a port
collides.

### 3.2 Response shape

```json
{
  "status": "ok",
  "worker_id": "spark-6262:355682:ee93e0e0",
  "service": "panoptic_image_caption_worker",
  "uptime_sec": 3421,
  "consumer": {
    "stream": "panoptic:jobs:image_caption",
    "group": "panoptic-image-caption-workers",
    "pending_pel": 0,
    "xlen": 12,
    "lag_sec": 4
  },
  "jobs": {
    "last_claim_at": "2026-04-17T22:49:42+00:00",
    "last_success_at": "2026-04-17T22:49:43+00:00",
    "attempts_today": 24
  },
  "dependencies": {
    "postgres": {"ok": true,  "latency_ms": 2},
    "redis":    {"ok": true,  "latency_ms": 1},
    "vllm":     {"ok": true,  "latency_ms": 180}
  }
}
```

Status levels:

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | All deps reachable, worker progressing |
| `degraded` | 200 | Worker running, one non-critical dep down (e.g. continuum 404s) |
| `error` | 503 | A critical dep down (Postgres, Redis, own stream) |
| *(503 without JSON)* | 503 | Worker crashed; nothing answering |

### 3.3 Which deps each worker reports

| Worker | Postgres | Redis | Qdrant | vLLM | Retrieval |
|---|---|---|---|---|---|
| trailer_webhook | ✅ | ✅ | | | |
| image_caption_worker | ✅ | ✅ | | ✅ | |
| caption_embed_worker | ✅ | ✅ | ✅ | | ✅ |
| summary_agent | ✅ | ✅ | | ✅ | |
| embedding_worker (sum_embed) | ✅ | ✅ | ✅ | | ✅ |
| rollup_worker | ✅ | ✅ | | ✅ | |
| search_api | ✅ | | ✅ | | ✅ |
| reclaimer | ✅ | ✅ | | | |

### 3.4 Dep-check caching

Don't pound dependencies on every `/healthz` hit.

* Each worker runs a **background probe loop**, once every **30 s**,
  updating an in-memory snapshot.
* `/healthz` reads the snapshot instantly.
* Snapshot includes timestamp of last probe so staleness is visible.

If a probe exceeds its per-call timeout (Postgres: 2 s, Redis: 1 s,
Qdrant: 2 s, vLLM: 5 s, retrieval: 5 s), mark that dep `ok=false,
latency_ms=null, reason="timeout"`.

### 3.5 Implementation shape

Each worker spawns a tiny HTTP server on a **daemon thread** at
startup. Server uses `http.server` + `threading.Thread` (stdlib, no
new deps). Reads from a thread-safe `HealthState` object updated by
the main XREADGROUP loop and the background probe loop.

```python
# worker main()
health = HealthState(service_name="...", worker_id=worker_id,
                     consumer_stream=STREAM_FOR_JOB_TYPE["image_caption"],
                     consumer_group="...")
start_health_server(port=HEALTH_PORT, state=health)
start_dep_probe_loop(state=health, deps=("postgres", "redis", "vllm"))
run_worker(engine, r, worker_id, vlm_client, health_state=health)
```

Shared module: `shared/health/server.py`. Each worker wires it into
its `main()` in ~5 lines.

For webhook and search_api (already FastAPI), the existing `/health`
route is extended to return the same JSON shape — no port change.

---

## 4. `scripts/dashboard.sh`

Plain-text one-page status. Runnable from any terminal, no daemon, no
credentials needed.

Output shape:

```
panoptic stack — 2026-04-17 23:10:00

SERVICES
  webhook          :8100  ok      lag=  0    last_claim=-           deps=pg,redis
  caption          :8201  ok      lag=  0    last_claim=12s ago     deps=pg,redis,vllm
  cap_embed        :8202  ok      lag=  0    last_claim=10s ago     deps=pg,redis,qdrant,retrieval
  summary          :8203  degraded lag= 3    last_claim=45s ago     deps=pg,redis,vllm | continuum:down
  sum_embed        :8204  ok      lag=  0    last_claim=42s ago     deps=pg,redis,qdrant,retrieval
  rollup           :8205  ok      lag=  0    last_claim=-           deps=pg,redis,vllm
  reclaimer        :8210  ok      reset=2 dlq=0 last_run=27s ago    deps=pg,redis
  search_api       :8600  ok      last_query=3s ago                  deps=pg,qdrant,retrieval

DEPS (latency ms)
  postgres         ok   2ms
  redis            ok   1ms
  qdrant           ok   3ms
  vllm             ok   180ms
  retrieval        ok   152ms

CONTAINERS (docker stats)
  NAME                         CPU%   MEM
  panoptic-postgres            0.4%   128MB/121GB
  panoptic-qdrant              0.1%   3.1GB/121GB
  panoptic-redis               0.0%   8MB/121GB
  panoptic-retrieval           9.2%   30GB/121GB  (GPU)
  panoptic-vllm                4.1%   22GB/121GB  (GPU)

DISK
  /              3.3TB free of 3.7TB  (8% used)
  /data/panoptic-store   1.2GB used

LOGS
  /home/surveillx/panoptic/logs — 6 files, 14MB total

uptime: 2h 53m
```

### Implementation

Bash script, ~80 lines. No external deps beyond `curl`, `jq`, `docker`,
`df`, `du`. Uses jq to pick fields out of `/healthz` JSON.

Refresh pattern: one-shot. User re-runs or pipes through `watch -n 5
scripts/dashboard.sh`. No daemon.

Exit code: 0 if every service is `ok`, 1 if any `degraded`, 2 if any
`error` or unreachable. Makes it usable in cron / CI for a quick
heartbeat if we ever want one.

---

## 5. `logrotate` config

Install at `/etc/logrotate.d/panoptic` (sudo).

```
/home/surveillx/panoptic/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Explanation of choices:

* **`daily`** — cheap cadence, rotations are tiny.
* **`rotate 14`** — two weeks of history, gzipped.
* **`compress` + `delaycompress`** — gzip rotations but keep the most
  recent rotation uncompressed for fast tailing when debugging.
* **`copytruncate`** — tmux-dev.sh pipes stdout through `tee -a`,
  which opens the log file once at startup. Without `copytruncate`,
  rotation would rename the file and `tee` would keep writing to the
  renamed inode. `copytruncate` preserves the original file handle.
* **`missingok` + `notifempty`** — don't error on missing/empty files;
  workers may not all have written yet on fresh boot.

For the Docker containers (`panoptic-vllm`, `panoptic-retrieval`,
`panoptic-store/*`), log rotation is handled by Docker's
`--log-opt max-size=...` instead. Not critical today; add if and when
their logs grow meaningfully. Out of scope for M2.

---

## 6. Latency visibility

No new work. The existing worker log format already emits per-job
timings:

```
2026-04-17 22:49:43,602 INFO httpx: HTTP Request: POST http://localhost:8000/v1/chat/completions "HTTP/1.1 200 OK"
2026-04-17 22:49:43,604 INFO ...executor: run_image_caption_job: caption_status=success
```

Wall-clock between claim and success is visible in `caption.log` /
`sum_embed.log` / `cap_embed.log`. If we want aggregate latency
histograms, that's M3+ observability work.

Search API already emits a `timing_ms` breakdown in every response
(parse / qdrant / postgres / rerank / total). That stays as-is.

---

## 7. Implementation surface

| File | Change |
|---|---|
| `shared/health/__init__.py` | NEW |
| `shared/health/state.py` | NEW — `HealthState` dataclass, thread-safe setters |
| `shared/health/server.py` | NEW — background HTTP server serving `/healthz` |
| `shared/health/probes.py` | NEW — dep probe helpers (pg, redis, qdrant, vllm, retrieval, continuum) + background loop |
| `services/panoptic_image_caption_worker/worker.py` | Wire health state updates at claim/success/failure; start health server in main() |
| `services/panoptic_caption_embed_worker/worker.py` | same |
| `services/panoptic_summary_agent/worker.py` | same |
| `services/panoptic_embedding_worker/worker.py` | same |
| `services/panoptic_rollup_worker/worker.py` | same |
| `services/trailer_webhook/app.py` | Extend existing `/health` to the common JSON shape |
| `services/search_api/app.py` | Extend existing `/health` to the common JSON shape |
| `scripts/dashboard.sh` | NEW — one-page status dashboard |
| `/etc/logrotate.d/panoptic` | NEW — logrotate config |
| `scripts/install_logrotate.sh` | NEW — sudo helper to install logrotate config |
| `.env.example` | Add per-worker `<NAME>_HEALTH_PORT` defaults |

---

## 8. Configuration

New env vars:

| Var | Default | Purpose |
|---|---|---|
| `CAPTION_HEALTH_PORT` | `8201` | image_caption_worker health endpoint |
| `CAP_EMBED_HEALTH_PORT` | `8202` | caption_embed_worker health endpoint |
| `SUMMARY_HEALTH_PORT` | `8203` | summary_agent health endpoint |
| `SUM_EMBED_HEALTH_PORT` | `8204` | embedding_worker health endpoint |
| `ROLLUP_HEALTH_PORT` | `8205` | rollup_worker health endpoint |
| `RECLAIMER_HEALTH_PORT` | `8210` | reclaimer health endpoint |
| `HEALTH_PROBE_INTERVAL_SEC` | `30` | dep probe cadence (shared) |
| `HEALTH_PROBE_TIMEOUT_SEC` | `5` | per-dep probe timeout |

No changes to existing ports (webhook 8100, search_api 8600, retrieval
8700, vLLM 8000, store 5432/6333/6379).

---

## 9. What dashboard.sh will NOT do

Keep scope clean:

* no writing to a database
* no push to an external service
* no historical tracking (each run is a snapshot)
* no auth — runs locally, no credentials
* no emojis, no ANSI colors (log-scrape-friendly plain text)

If someone wants historical tracking, that's when we graduate to
Prometheus (deferred).

---

## 10. Rollout

Order of work once this doc is approved:

1. `shared/health/` scaffold (~30 min)
2. Webhook + search_api `/health` extended shape (~15 min)
3. One worker retrofitted end-to-end as proof (caption worker, ~30 min)
4. Remaining 4 workers follow the pattern (~30 min total)
5. `dashboard.sh` script (~30 min)
6. `logrotate` config + install script (~15 min)
7. Smoke: start the stack, run `dashboard.sh`, kill one dep, confirm it shows up as degraded/error (~15 min)

Total: ~2.5 hours.

---

## 11. Open questions

Two calls I'd want confirmation on before implementing:

1. **Should `/healthz` on workers enforce auth?** My lean: **no**. It's
   localhost-only (0.0.0.0 on the Spark, inside the LAN), and the
   dashboard script + the operator are the only consumers. Adding auth
   here duplicates the trailer-auth design and pays no benefit. If
   someone is on your LAN, you have bigger problems. But flagging it.

2. **Is `/etc/logrotate.d/panoptic` the right path, or would you
   prefer the logrotate config to live inside the repo as a
   documented artifact that gets symlinked / copied by a provision
   script?** My lean: **keep it in the repo at
   `~/panoptic/deploy/logrotate/panoptic.conf`**, with
   `scripts/install_logrotate.sh` that sudos to symlink it into
   `/etc/logrotate.d/`. That makes the config versioned and
   reproducible on a new Spark.

Everything else I'm recommending as written.
