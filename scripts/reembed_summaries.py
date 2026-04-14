"""
Re-embed all summaries into the panoptic_summaries Qdrant collection.

Why this exists:
    After the vil → panoptic rename the code points at collection
    `panoptic_summaries` but the historical vectors live in the orphaned
    `vil_summaries` collection. This script resets summary embedding state
    and re-enqueues embedding_upsert jobs so the worker rebuilds the
    collection from Postgres source of truth.

Idempotent — safe to re-run. If a summary is already 'pending', it stays
pending; if already 'complete', it's reset to pending.

Usage:
    PYTHONPATH=. python3 scripts/reembed_summaries.py

Requires the panoptic_embedding_worker to be running (or started afterwards)
to actually consume the enqueued jobs.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from shared.utils.redis_client import get_redis_client
from shared.utils.streams import bootstrap_streams, enqueue_job

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_url = os.environ["DATABASE_URL"]
    engine = create_engine(db_url, pool_pre_ping=True)
    r = get_redis_client()
    bootstrap_streams(r)

    with engine.connect() as conn:
        # 1. Reset summary status.
        result = conn.execute(text("""
            UPDATE panoptic_summaries
               SET embedding_status = 'pending',
                   updated_at       = now()
             WHERE embedding_status = 'complete'
        """))
        log.info("reset %d summaries from complete -> pending", result.rowcount)

        # 2. Reset any 'succeeded' embedding_upsert jobs back to 'pending' so
        #    the worker will re-process them. Leave attempt tracking clean.
        result = conn.execute(text("""
            UPDATE panoptic_jobs
               SET state             = 'pending',
                   lease_owner       = NULL,
                   lease_expires_at  = NULL,
                   attempt_count     = 0,
                   last_error        = NULL,
                   updated_at        = now()
             WHERE job_type = 'embedding_upsert'
               AND state   <> 'pending'
        """))
        log.info("reset %d embedding_upsert jobs to pending", result.rowcount)

        # 3. Fetch all pending embedding_upsert jobs for Redis enqueue.
        jobs = conn.execute(text("""
            SELECT job_id, serial_number
              FROM panoptic_jobs
             WHERE job_type = 'embedding_upsert'
               AND state    = 'pending'
             ORDER BY created_at
        """)).fetchall()

        conn.commit()

    log.info("enqueueing %d embedding_upsert jobs to Redis", len(jobs))
    for job in jobs:
        enqueue_job(
            r,
            job_type="embedding_upsert",
            job_id=str(job.job_id),
            serial_number=job.serial_number,
        )

    log.info("done. start panoptic_embedding_worker to drain the queue.")


if __name__ == "__main__":
    main()
