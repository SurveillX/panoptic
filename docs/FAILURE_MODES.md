# Panoptic Failure Modes

Observed and simulated failure modes, their detection, and recovery.
Living document — append as new modes surface.

Each entry answers:

1. **Symptom** — what you'd see on the dashboard or in logs
2. **Cause** — what's actually wrong
3. **Detection** — how the system surfaces it (if it does)
4. **Recovery** — how to get back to green

---

## 1. Worker crashes mid-job

**Symptom:** pane in tmux dies (`dead=1`); dashboard shows the worker's
`/healthz` as `UNREACH`. The job it was processing sits in Postgres with
`state='leased'`, `lease_owner=<dead worker_id>`, `lease_expires_at` in
the future.

**Cause:** SIGINT, OOM, uncaught exception, container kill — anything
that terminates the Python process while it holds an active lease.

**Detection:** the reclaimer (30s tick) queries for `state IN
('leased','running','retry_wait') AND lease_expires_at < now()`. Up to
LEASE_TTL_SECONDS (120s) passes before the lease is considered expired,
so worst-case detection lag is ~150s.

**Recovery — automatic:** reclaimer resets the job to `pending`, calls
`enqueue_job()` to XADD a fresh stream message, and the next alive
worker (or the same pane once respawned) picks it up via the retry path
(`attempt_count` increments).

**Verified:** 2026-04-17 during M2 smoke (manual SIGINT to caption
worker), 2026-04-18 during normal real-trailer operation (reclaimer
logged `reset=1` on a stale lease from before the fleet-secret change).

---

## 2. Job hits max attempts → DLQ

**Symptom:** warning log in the worker's pane:
```
DLQ job_type=<type> job_id=<uuid> reason=<msg>
```
and the job row in Postgres moves to `state='failed_terminal'`.

**Cause:** three classes:
- Permanent: missing dependencies (image row or file gone, vector
  service returns 400 permanently, etc.). The executor returns
  `'failed_terminal'` directly on first attempt.
- Transient-but-exhausting: attempt_count reached max_attempts (3 by
  default) after repeated exceptions.
- Reclaimer decision: when the reclaimer reclaims a job whose
  attempt_count already exceeded max_attempts, it writes
  `failed_terminal` itself and enqueues DLQ.

**Detection:** `scripts/dlq_inspect.py` lists every DLQ entry across
every stream with the associated job state and reason.

**Recovery — manual:**

```bash
# 1. See what's there
.venv/bin/python scripts/dlq_inspect.py

# 2. Understand the reason (often: missing dep, changed upstream)
#    Fix the underlying cause.

# 3. Replay the job
.venv/bin/python scripts/dlq_replay.py --job-id <uuid> --ack

# Bulk replay all entries in one DLQ stream:
.venv/bin/python scripts/dlq_replay.py --job-type caption_embed --all --ack
```

By default the replay script resets Postgres state to
`state='pending'`, `attempt_count=0`, `last_error=NULL`, then re-XADDs.
Use `--no-reset` to preserve the attempt history (rare).

**Verified:** 2026-04-18 during M4 kickoff. Injected a caption_embed
job with a non-existent image_id; watched it land in DLQ in <1s with
reason `unknown error`; inserted a stub image row; replayed with
`--ack`; watched it succeed and clear.

---

## 3. Redis briefly unavailable

**Status:** verified 2026-04-18 with a 10s docker stop and a follow-up 15s stop.

**First run (before fix):** every one of the six job-processing workers
(caption / cap_embed / summary / sum_embed / rollup / img_embed) died.
The `XREADGROUP` call in `consume_next()` raised
`redis.exceptions.ConnectionError` out of the worker's outer loop —
the only `try/except` was around `_process_message`, not
`consume_next`. Workers had to be manually respawned.

**Fix:** `shared/utils/streams.consume_next()` now catches
`ConnectionError` and `TimeoutError` from redis-py, logs a backoff
warning, sleeps 1 s, and returns None. Outer loop sees "no messages"
and retries naturally.

**Second run (after fix):** zero worker deaths. All panes logged
`consume_next: Redis unavailable — backing off 1s: Error 111
connecting to panoptic-store:6379. Connection refused.` repeatedly
during the outage, then resumed cleanly when Redis came back. The
dashboard transitioned `redis` OK → error → OK over the next probe
cycle (30 s). Webhook finalizer caught its own Redis errors as it
already did (pre-existing try/except).

**Recovery semantics:**
- Workers resume blocking reads; in-flight state in Postgres is
  intact.
- PEL entries held during the outage are picked up by XAUTOCLAIM on
  the next reclaimer tick.
- Trailer pushes during the outage return 5xx — trailer retries via
  its at-least-once semantics.

---

## 4. Postgres briefly unavailable

**Status:** verified 2026-04-18 with a 15s docker stop.

Observed: zero worker deaths. Reclaimer logged
`reclaimer: tick failed (continuing): (psycopg2.OperationalError)
connection to server at "panoptic-store" (127.0.0.1), port 5432
failed: Connection refused` once during the window, its outer
`try/except` caught it, next tick succeeded. No non-terminal jobs left
behind after recovery (387 jobs total, zero stuck).

Workers didn't emit Postgres errors simply because no jobs were
claimable during the brief window — but the code path is correct: the
outer `except` in each worker's `run_worker()` catches exceptions from
`_process_message`, logs, and continues.

Trailer pushes during the outage would get 500 (FastAPI endpoint
exception → HTTP 500). Trailer retries.

---

## 5. Qdrant briefly unavailable

**Status:** verified 2026-04-18 with a 15s docker stop.

Observed: zero worker deaths, `/v1/search` returned HTTP 500 with
`{"error":"search failed","detail":"[Errno 111] Connection refused"}`
during the outage. First search after restart returned 9 results in
335ms — full recovery.

Workers' outer `except` would push any in-flight embedding job to
`retry_wait` (verified by code path — no actual jobs triggered
during this brief window). Max 3 attempts before DLQ.

---

## 6. vLLM (Gemma) briefly unavailable

**Status:** verified 2026-04-18 with a 15s docker stop. Worth noting
vLLM takes 30s+ to reload Gemma (model weights + compile) on restart,
so its recovery is slower than the stateless services.

Observed: zero worker deaths. `/v1/search/verify` during outage
returned 500 with `"detail":"[Errno 104] Connection reset by peer"`.
No in-flight caption/summary/rollup jobs during the window, so no
retry_wait activity observed directly — but the code path is the
standard `try/except` → `retry_wait` → up to 3 attempts → DLQ.

Recovery: vLLM returns after ~30-60s (Gemma model reload is the long
part) → next retry succeeds. Deep backlog drains at worker throughput.

---

## 7. Retrieval service briefly unavailable

**Status:** verified 2026-04-18 with a 15s docker stop.

Observed: zero worker deaths. `/v1/search` returned 500 during
outage. Search API recovered immediately on restart — first post-
restart query completed in 335ms.

Workers' outer `except` handles in-flight embedding failures the same
way as Qdrant above (retry_wait → DLQ after max_attempts).

---

## 8. Trailer push during panoptic webhook downtime

**Symptom:** trailer sees 502/503 at `https://panoptic.surveillx.ai`.

Trailer behavior: at-least-once delivery semantics mean it retries. On
resume:
- Bucket notifications with the same `event_id` will be rejected at
  the webhook's Redis SETNX layer with `status='duplicate'` — no
  double-processing.
- Image pushes with the same deterministic `image_id` will be rejected
  at the Postgres `ON CONFLICT (image_id) DO NOTHING` layer, same
  status.

**Verified:** 2026-04-18 during M2 smoke (scripts/dev_idempotency_test.py).

---

## 9. Disk fills up on /data/panoptic-store

**Status:** not yet simulated.

Expected symptom: Postgres WAL writes fail → webhook returns 500 →
trailer retries. Qdrant writes fail → embed workers retry then DLQ.

Detection: `scripts/dashboard.sh` displays `df -h /` and `du -sh
/data/panoptic-store`. No alert threshold today — M4 gap.

Recovery: expand the mount or prune old images / snapshots. Qdrant
snapshots accumulate at `/data/panoptic-store/qdrant/snapshots/`.

---

## 10. Health-probe connection leak (fixed)

**Symptom:** after ~10 hours of real traffic, workers started reporting
`✗postgres` on `/healthz` even though Postgres itself was fine.
`pg_stat_activity` showed ~90/100 connections in use, and new
connections failed with `FATAL: sorry, too many clients already`.

**Root cause:** `shared/health/probes._probe_postgres()` was creating a
brand-new SQLAlchemy engine (and its connection pool) on every 30 s
probe without disposing it. With 8 workers probing every 30 s, we were
allocating pools faster than GC was reclaiming them. Same latent leak
existed in `_probe_redis()`, just less impactful because Redis tolerates
many more concurrent clients.

**Fix:** `_probe_postgres()` now uses `psycopg2.connect(...)` directly
and explicitly `conn.close()`s — one TCP connection opened and closed
per probe, no pool. `_probe_redis()` explicitly calls `r.close()` and
`r.connection_pool.disconnect()` after `ping`.

**Verified:** after the fix and a full tmux-session restart,
`pg_stat_activity` reports 3 connections total (1 active + 2 idle) —
down from ~90.

**Lesson:** any per-probe resource should be scoped to the probe call
and explicitly released. The original mental model was "SA will GC the
engine" — true eventually, but not reliably enough for a 30 s loop.

---

## Open gaps

- Alerting on disk usage / health degradation (today's only surface is
  the manual dashboard run).
- No DLQ size bounds enforcement — the per-stream `maxlen=10000` on
  XADD protects against unbounded growth of a single DLQ stream, but
  aggregate DLQ size across all 7 streams has no alarm.
- Retrieval service "slow but up" mode not characterized (would look
  like a normal worker hang to the reclaimer).
- Trailer's Continuum endpoint 404s on some cameras → summaries
  degrade to metadata_only. Per trailer team this is by design; worth
  measuring what fraction of buckets end up metadata_only over time
  and whether it correlates with recording gaps on specific cameras.
