# Panoptic Data Retention

What we keep, what we prune, when.

---

## TL;DR recommended policy

| Data class | Retention | Tool |
|---|---|---|
| **Images — `baseline` trigger** | **7 days** | `scripts/prune_images.py` |
| **Images — `alert` trigger** | **180 days** | `scripts/prune_images.py` |
| **Images — `anomaly` trigger** | **365 days** | `scripts/prune_images.py` |
| **Summaries** (`panoptic_summaries`) | **indefinite** | — |
| **Buckets** (`panoptic_buckets`) | **indefinite** | — |
| **Terminal jobs** (`panoptic_jobs`) | **30 days** | `scripts/prune_jobs.py` |
| **Job history** (`panoptic_job_history`) | **30 days** | `scripts/prune_jobs.py` |
| **Trailer registry** (`panoptic_trailers`) | indefinite | — |
| **DLQ entries** (Redis) | capped at 10k per stream (maxlen) | automatic |
| **Webhook dedup markers** (Redis SETNX) | 24 h TTL | automatic |
| **Replay cache** (Redis SETNX) | 10 min TTL | automatic |
| **Logs** (`~/panoptic/logs/*.log`) | 14 days (compressed) | `logrotate` (installed) |

These defaults are what the scripts run with if you pass `--apply` and
nothing else. Override per-run flags for different policies.

---

## Why these numbers

### Images

**Baseline images are 80–95% of all push volume.** The trailer pushes
~240 images/day at full cadence; the vast majority are routine
"nothing happening" frames whose retrieval value drops rapidly with
age. Meanwhile each image is ~500 KB and each also has two Qdrant
vectors + caption text hanging off it — storage isn't free even if
it's cheap.

At 500 trailers × 7-day baseline retention:
`500 × 240/day × 80% baseline × 7 days × 500KB ≈ **336 GB**`.

That's the steady state, not the unbounded growth we'd have without
pruning.

**Alert images are the interesting ones** — the operator actually
wants to look back months for those. 180 days gives decent recall for
quarterly reviews or incident retrospectives. 1 year for anomaly
triggers since those are the rarest and highest-signal.

Tune per trigger type is deliberate — a policy "keep everything 30
days" wastes disk on baselines while losing the alerts too fast.

### Summaries + buckets

These are small structured records (~1 KB each). 500 trailers × 365
days × 96 buckets/day × 1 KB = ~17 GB. Trivial. Keep forever for
long-horizon queries like "how did Yard A's activity change over Q1".

### Jobs

Terminal job rows are operational metadata. We rarely query them
after the job completed. 30 days is a reasonable forensic window
(enough to investigate "did this image get processed last week?")
before pruning.

Non-terminal jobs are **never** pruned by the tool, regardless of
age. If a job has been stuck in `pending` for 60 days, deleting it
silently is worse than letting it accumulate — investigate it.

### Job history

`panoptic_job_history` is append-only and grows with every state
transition (~4-5 per job). At 500 trailers × 90k jobs/month × 4
transitions = 1.44M rows/month. 30-day retention keeps it at ~1.5M.

If you need longer audit retention (compliance, debugging long-tail
stability), bump `--keep-days 180` and accept the larger table.
Postgres handles tens of millions of rows fine; the cost is disk + an
eventual `VACUUM FULL`.

---

## Running the pruners

Both scripts default to **dry-run** — print what they would delete,
don't touch anything. Use `--apply` to actually delete.

```bash
cd ~/panoptic && set -a && . ./.env && set +a

# dry-run reports
.venv/bin/python scripts/prune_images.py
.venv/bin/python scripts/prune_jobs.py

# apply with defaults
.venv/bin/python scripts/prune_images.py --apply
.venv/bin/python scripts/prune_jobs.py --apply

# custom policy example
.venv/bin/python scripts/prune_images.py \
    --keep-baseline-days 3 --keep-alert-days 365 --apply

# batch size (useful when catching up after a long gap)
.venv/bin/python scripts/prune_jobs.py --apply --limit 10000
```

Both are idempotent and safe to re-run. Both continue on per-row errors.

---

## What the tools delete

### `scripts/prune_images.py` — per matching image:

1. **Qdrant points** in both `image_caption_vectors` and
   `panoptic_image_vectors` (matched by deterministic `image_id →
   UUID` derivation).
2. **Postgres row** in `panoptic_images`.
3. **JPEG file** at `storage_path` on disk.

Delete order is Qdrant → Postgres → disk. A per-item failure is
logged and the script continues with the next row (no global rollback).

### `scripts/prune_jobs.py` — batched:

1. `DELETE FROM panoptic_jobs WHERE state IN
   ('succeeded','failed_terminal','degraded') AND updated_at < cutoff`
   — batch-limited per run to keep locks short.
2. `DELETE FROM panoptic_job_history WHERE created_at < cutoff` —
   same batching.

**Non-terminal jobs are never touched.**

---

## Cron schedule (recommended)

Add to user crontab alongside the existing `health_watch.py` entry:

```cron
# Nightly prune at 03:10 UTC (low-traffic window)
10 3 * * * cd /home/surveillx/panoptic && set -a && . ./.env && set +a && .venv/bin/python scripts/prune_images.py --apply >> logs/prune.log 2>&1
20 3 * * * cd /home/surveillx/panoptic && set -a && . ./.env && set +a && .venv/bin/python scripts/prune_jobs.py --apply >> logs/prune.log 2>&1
```

Both tools are idempotent — running them on an empty-candidate day
is a no-op.

---

## What's NOT pruned automatically

These require operator action; the tools don't touch them:

- **DLQ entries.** Capped at 10k per stream (maxlen), but operator
  judgement decides whether to replay or drop each. Use
  `scripts/dlq_inspect.py` and `scripts/dlq_replay.py`.
- **Inactive trailers** (`panoptic_trailers WHERE is_active=false`).
  Left for audit; delete manually if needed.
- **Orphan Qdrant points** (where the matching `panoptic_images` row
  was deleted but Qdrant delete failed). Tiny fraction at our error
  rate. A future "reconcile" script could sweep these if volume
  matters.

---

## Escalation triggers

`scripts/health_watch.py` already alerts when `disk_data_pct > 80`.
When that fires, the sequence is:

1. Check `du -sh /data/panoptic-store/{images,postgres,qdrant,redis}`
   to see which is growing.
2. If images dominate: lower `--keep-baseline-days` and re-run
   `prune_images.py --apply`.
3. If Postgres/Qdrant dominate: consider `--keep-days` reduction on
   `prune_jobs.py` + a `VACUUM FULL panoptic_job_history` after.
4. If none of the above recovers enough room: we're at the M6
   threshold — time to move `panoptic-store` to a dedicated box with
   more disk.
