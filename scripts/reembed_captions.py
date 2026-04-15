"""
Re-embed all image captions into the image_caption_vectors Qdrant collection.

Why this exists:
    When the underlying embedding model changes (dimension, architecture, or
    quality target), image-caption vectors must be rebuilt. This script resets
    caption-embedding state and re-enqueues caption_embed jobs so the worker
    rebuilds the collection from Postgres source of truth.

Only rows whose caption_status = 'success' are re-enqueued — if a caption
itself never succeeded, there is no text to embed.

Idempotent — safe to re-run.

Usage:
    PYTHONPATH=. python3 scripts/reembed_captions.py

Requires the panoptic_caption_embed_worker to be running (or started afterwards)
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
        # 1. Reset caption-embedding status for rows whose caption exists.
        result = conn.execute(text("""
            UPDATE panoptic_images
               SET caption_embedding_status = 'pending',
                   updated_at               = now()
             WHERE caption_status            = 'success'
               AND caption_embedding_status <> 'pending'
        """))
        log.info("reset %d images caption_embedding_status -> pending", result.rowcount)

        # 2. Reset caption_embed jobs back to pending.
        result = conn.execute(text("""
            UPDATE panoptic_jobs
               SET state             = 'pending',
                   lease_owner       = NULL,
                   lease_expires_at  = NULL,
                   attempt_count     = 0,
                   last_error        = NULL,
                   updated_at        = now()
             WHERE job_type = 'caption_embed'
               AND state   <> 'pending'
        """))
        log.info("reset %d caption_embed jobs to pending", result.rowcount)

        # 3. Fetch all pending caption_embed jobs for Redis enqueue.
        jobs = conn.execute(text("""
            SELECT job_id, serial_number
              FROM panoptic_jobs
             WHERE job_type = 'caption_embed'
               AND state    = 'pending'
             ORDER BY created_at
        """)).fetchall()

        conn.commit()

    log.info("enqueueing %d caption_embed jobs to Redis", len(jobs))
    for job in jobs:
        enqueue_job(
            r,
            job_type="caption_embed",
            job_id=str(job.job_id),
            serial_number=job.serial_number,
        )

    log.info("done. start panoptic_caption_embed_worker to drain the queue.")


if __name__ == "__main__":
    main()
