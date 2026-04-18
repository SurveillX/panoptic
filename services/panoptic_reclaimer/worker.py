"""
Panoptic reclaimer — dedicated process that runs reclaim_expired_leases()
on a fixed interval to provide the at-least-once guarantee for job
delivery. See docs/RECLAIMER_DESIGN.md.

Usage:
    DATABASE_URL=... REDIS_URL=... \\
    PYTHONPATH=. python -m services.panoptic_reclaimer.worker

Environment variables:
    DATABASE_URL               — Postgres connection string (required)
    REDIS_URL                  — Redis connection string (required)
    RECLAIMER_INTERVAL_SEC     — tick cadence (default 30)
    RECLAIMER_HEALTH_PORT      — /healthz bind port (default 8210)
"""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy import create_engine

from shared.health.probes import start_probe_loop
from shared.health.server import start_health_server
from shared.health.state import HealthState
from shared.utils.leases import generate_worker_id, reclaim_expired_leases
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import enqueue_job

log = logging.getLogger(__name__)

INTERVAL_SEC = int(os.environ.get("RECLAIMER_INTERVAL_SEC", "30"))
HEALTH_PORT = int(os.environ.get("RECLAIMER_HEALTH_PORT", "8210"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()
    worker_id = generate_worker_id()

    health = HealthState(service_name="panoptic_reclaimer", worker_id=worker_id)
    health.mark_critical("postgres", "redis")
    start_health_server(port=HEALTH_PORT, state=health)
    start_probe_loop(
        health,
        targets={
            "postgres": {"database_url": DATABASE_URL},
            "redis": {"redis_url": REDIS_URL},
        },
    )

    log.info(
        "reclaimer starting worker_id=%s interval=%ds health_port=%d",
        worker_id, INTERVAL_SEC, HEALTH_PORT,
    )

    while True:
        try:
            stats = reclaim_expired_leases(engine, r)

            # The reclaim function deliberately doesn't XADD. It's our job
            # here to re-enqueue any job it reset to pending so a worker
            # picks it up.
            re_enqueued = 0
            for job in stats.reset_jobs:
                try:
                    enqueue_job(
                        r,
                        job_id=job.job_id,
                        job_type=job.job_type,
                        serial_number=job.serial_number,
                    )
                    re_enqueued += 1
                except Exception as exc:
                    log.error(
                        "reclaimer: re-enqueue failed job_id=%s job_type=%s: %s",
                        job.job_id, job.job_type, exc,
                    )

            health.record_reclaim(stats)
            if stats.reset_to_pending or stats.sent_to_dlq or stats.stale_pel_acked or re_enqueued:
                log.info(
                    "reclaimer tick: reset=%d re_enqueued=%d dlq=%d pel_acked=%d",
                    stats.reset_to_pending, re_enqueued, stats.sent_to_dlq, stats.stale_pel_acked,
                )
            else:
                log.debug("reclaimer tick: quiet")
        except Exception as exc:
            log.exception("reclaimer: tick failed (continuing): %s", exc)
            health.record_failure(str(exc))

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
