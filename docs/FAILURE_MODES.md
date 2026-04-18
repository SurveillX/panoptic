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

**Status:** not yet simulated on live traffic. Planned M4 smoke.

Expected behavior (by design):
- Webhook: XADD fails → returns 503 to trailer. Trailer retries on its
  own cadence.
- Workers: `XREADGROUP` with BLOCK returns None after timeout → inner
  loop continues, retries on next tick. No state change in Postgres.
- Reclaimer: Phase-1 Postgres reset completes; Phase-2 XAUTOCLAIM
  raises, is caught, logged, tick continues. No data loss.
- Dashboard: `/healthz` `deps.redis.ok = false` → status=`error` →
  HTTP 503.

Recovery on Redis restart: workers resume blocking reads; in-flight
state in Postgres is intact; any PEL entries held during the outage
will be picked up by XAUTOCLAIM on the next reclaimer tick.

---

## 4. Postgres briefly unavailable

**Status:** not yet simulated on live traffic.

Expected behavior:
- Webhook: SQLAlchemy raises `OperationalError`, bucket dedup write to
  Redis happens first but the image INSERT fails — trailer sees 500.
  Trailer retries; Redis SETNX on bucket event_id will mark it duplicate
  since the Redis write already happened, so subsequent bucket pushes for
  the same event_id return `duplicate` (that's correct behavior — we
  already processed the fragment).
- Workers: claim_job fails → next loop iteration. No state mutation
  observable externally.
- Reclaimer: whole tick fails, caught in the outer `except`, next tick
  in 30s. No data loss.

Recovery: workers resume on next tick once DB is back.

---

## 5. Qdrant briefly unavailable

**Status:** not yet simulated.

Expected behavior:
- `caption_embed_worker` / `embedding_worker` / `image_embed_worker`
  raise on upsert → caught by worker's outer `except`, job moves to
  `retry_wait`. Up to `max_attempts` retries with backoff. If the
  outage exceeds all retries → DLQ.
- `search_api`: `/v1/search` returns 500 (or degraded results if
  `_search()` 404 fallback kicks in for a missing collection).

Recovery: Qdrant comes back → next retry succeeds. DLQ replay for
anything that exhausted attempts.

---

## 6. vLLM (Gemma) briefly unavailable

**Status:** not yet simulated.

Expected behavior:
- `image_caption_worker` raises `VLMNetworkError` →
  `job_state = 'retry_wait'` with backoff. Jobs accumulate on the
  stream but don't progress.
- `summary_agent` has partial degradation — it already falls back to
  `metadata_only` mode when Continuum returns 404. If vLLM itself is
  down, the summary text-generation call fails and the job retries.

Recovery: vLLM returns → next retry succeeds. Deep backlog drains at
worker throughput.

---

## 7. Retrieval service briefly unavailable

**Status:** not yet simulated.

Expected behavior:
- Text and VL embedding calls fail → `caption_embed_worker` /
  `embedding_worker` / `image_embed_worker` retry via `retry_wait`.
- `search_api`: `/v1/search` can't embed the query — returns 500.
  Warmup path handles this gracefully (already coded).

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
