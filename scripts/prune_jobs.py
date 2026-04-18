"""
Panoptic job-history retention / pruning.

Deletes rows in `panoptic_jobs` and `panoptic_job_history` older than
N days that are in terminal state. Important because:

  - `panoptic_jobs` grows once per job (roughly 5 jobs per image pushed
    + 2 per bucket). At 500 trailers steady-state ~3M rows/month. Not
    a death sentence for Postgres but indexes on big tables get slow.
  - `panoptic_job_history` is append-only and grows every state
    transition (~4 per job). Unbounded without pruning.

Jobs in non-terminal state (pending, leased, running, retry_wait) are
NEVER pruned regardless of age. Only `succeeded`, `failed_terminal`,
and `degraded` rows are candidates.

Default retention: 30 days.

Run:
    .venv/bin/python scripts/prune_jobs.py --dry-run
    .venv/bin/python scripts/prune_jobs.py --apply
    .venv/bin/python scripts/prune_jobs.py --apply --keep-days 60
    .venv/bin/python scripts/prune_jobs.py --apply --limit 10000

Rows are deleted in batches of --limit (default 5000) per run to avoid
long-held locks. Safe to run under live traffic.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

TERMINAL_STATES = ("succeeded", "failed_terminal", "degraded")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="redundant (default is dry-run); accepted for clarity")
    ap.add_argument("--limit", type=int, default=5000,
                    help="max rows per run per table (batches keep locks short)")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        print("--apply and --dry-run are mutually exclusive")
        return 2
    dry_run = not args.apply
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)

    print(f"retention: terminal jobs older than {args.keep_days} days")
    print(f"cutoff:    {cutoff.isoformat()}")
    print(f"mode:      {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"limit:     {args.limit} rows per table")
    print()

    db_url = os.environ["DATABASE_URL"]
    engine = sa.create_engine(db_url)

    # Count candidates
    with engine.connect() as c:
        job_n = c.execute(sa.text(
            "SELECT COUNT(*) FROM panoptic_jobs "
            "WHERE state = ANY(:st) AND updated_at < :co"
        ), {"st": list(TERMINAL_STATES), "co": cutoff}).scalar() or 0

        hist_n = c.execute(sa.text(
            "SELECT COUNT(*) FROM panoptic_job_history WHERE created_at < :co"
        ), {"co": cutoff}).scalar() or 0

    print(f"panoptic_jobs candidates:         {job_n}")
    print(f"panoptic_job_history candidates:  {hist_n}")

    if job_n == 0 and hist_n == 0:
        print("\nnothing to prune.")
        return 0

    if dry_run:
        print("\ndry-run. re-run with --apply to actually delete.")
        return 0

    # Apply — batch delete, capped by --limit to keep locks short
    t0 = time.perf_counter()
    with engine.connect() as c:
        # panoptic_jobs — delete the first N terminal-and-old rows
        res = c.execute(sa.text("""
            DELETE FROM panoptic_jobs
             WHERE job_id IN (
                 SELECT job_id FROM panoptic_jobs
                  WHERE state = ANY(:st) AND updated_at < :co
                  ORDER BY updated_at
                  LIMIT :lim
             )
        """), {"st": list(TERMINAL_STATES), "co": cutoff, "lim": args.limit})
        c.commit()
        jobs_deleted = res.rowcount

        # panoptic_job_history — no state filter; just age
        res = c.execute(sa.text("""
            DELETE FROM panoptic_job_history
             WHERE id IN (
                 SELECT id FROM panoptic_job_history
                  WHERE created_at < :co
                  ORDER BY created_at
                  LIMIT :lim
             )
        """), {"co": cutoff, "lim": args.limit})
        c.commit()
        hist_deleted = res.rowcount

    elapsed = time.perf_counter() - t0
    print(f"\ndeleted in {elapsed:.2f}s:")
    print(f"  panoptic_jobs:         {jobs_deleted}")
    print(f"  panoptic_job_history:  {hist_deleted}")

    remain_jobs = job_n - jobs_deleted
    remain_hist = hist_n - hist_deleted
    if remain_jobs > 0 or remain_hist > 0:
        print(
            f"\n{remain_jobs} jobs + {remain_hist} history rows still eligible. "
            f"Re-run to drain further, or increase --limit."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
