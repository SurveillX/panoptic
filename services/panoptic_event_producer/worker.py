"""
panoptic-event-producer — event_produce consumer.

Worker loop:
  consume_next → claim_job → LeaseHeartbeat → run_event_produce_job
  → release_job → verify lease → commit → enqueue_dlq (if terminal) → ACK

No external writes (Postgres INSERT only). No chaining. Idempotent by
content-addressed event_id; re-running is safe.

ACK contract (strictly enforced):
  claim fails           → no ACK
  lease stolen          → no ACK, no commit
  lease lost pre-commit → no ACK, no commit
  succeeded / retry_wait → release_job → verify lease → commit → ACK
  failed_terminal       → release_job → verify lease → commit
                          → enqueue_dlq → ACK
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from services.panoptic_event_producer.executor import run_event_produce_job
from shared.health.probes import start_probe_loop
from shared.health.server import start_health_server
from shared.health.state import HealthState
from shared.utils.leases import (
    LeaseHeartbeat,
    claim_job,
    compute_retry_delay,
    generate_worker_id,
    release_job,
)
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import (
    CONSUMER_GROUP_FOR_STREAM,
    GROUP_FOR_JOB_TYPE,
    STREAM_FOR_JOB_TYPE,
    ack_message,
    bootstrap_streams,
    consume_next,
    enqueue_dlq,
)

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")


def _process_message(engine, r, msg, worker_id: str) -> bool:
    """Process one stream message end-to-end."""
    job_id = msg.job_id

    with engine.connect() as conn:
        claim = claim_job(conn, job_id=job_id, worker_id=worker_id)
        conn.commit()

    if claim is None:
        log.debug("_process_message: job_id=%s not claimable — no ACK", job_id)
        return False

    job_state = "failed_terminal"
    last_error: str | None = None

    with engine.connect() as conn:
        with LeaseHeartbeat(engine, job_id, worker_id) as hb:
            job_row = conn.execute(
                text("""
                    SELECT payload, attempt_count, max_attempts
                      FROM panoptic_jobs
                     WHERE job_id = :job_id
                """),
                {"job_id": job_id},
            ).fetchone()

            if job_row is None:
                log.error("_process_message: job_id=%s missing after claim", job_id)
                return False

            try:
                job_state = run_event_produce_job(
                    conn,
                    payload=job_row.payload,
                    worker_id=worker_id,
                    engine=engine,
                )
            except Exception as exc:
                log.exception(
                    "_process_message: unexpected error job_id=%s: %s", job_id, exc
                )
                job_state = (
                    "failed_terminal"
                    if job_row.attempt_count >= job_row.max_attempts
                    else "retry_wait"
                )
                last_error = str(exc)[:1000]

            retry_delay = (
                compute_retry_delay(job_row.attempt_count)
                if job_state == "retry_wait"
                else None
            )
            released = release_job(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                new_state=job_state,
                last_error=last_error,
                retry_after_seconds=retry_delay,
            )

            if not released:
                log.warning(
                    "_process_message: lease stolen before release job_id=%s — abort",
                    job_id,
                )
                conn.rollback()
                return False

            if not hb.is_valid():
                log.warning(
                    "_process_message: lease lost pre-commit job_id=%s — abort", job_id
                )
                conn.rollback()
                return False

            conn.commit()

    if job_state == "failed_terminal":
        try:
            enqueue_dlq(
                r,
                job_type="event_produce",
                job_id=job_id,
                serial_number=claim.serial_number,
                reason=last_error or "unknown error",
            )
        except Exception as exc:
            log.error(
                "_process_message: DLQ enqueue failed job_id=%s: %s", job_id, exc
            )

    ack_message(r, stream=msg.stream, group=msg.group, entry_id=msg.entry_id)
    log.info("_process_message: completed job_id=%s state=%s", job_id, job_state)
    return True


def run_worker(engine, r, worker_id: str) -> None:
    stream = STREAM_FOR_JOB_TYPE["event_produce"]
    group = GROUP_FOR_JOB_TYPE["event_produce"]

    log.info(
        "worker starting worker_id=%s stream=%s group=%s",
        worker_id, stream, group,
    )

    while True:
        msg = consume_next(r, stream=stream, group=group, consumer_id=worker_id)
        if msg is None:
            continue

        log.debug("worker: received job_id=%s", msg.job_id)
        try:
            _process_message(engine, r, msg, worker_id)
        except Exception as exc:
            log.exception(
                "worker: unhandled error for job_id=%s — message stays in PEL: %s",
                msg.job_id, exc,
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()

    bootstrap_streams(r)
    worker_id = generate_worker_id()

    stream = STREAM_FOR_JOB_TYPE["event_produce"]
    health = HealthState(
        service_name="panoptic_event_producer",
        worker_id=worker_id,
        consumer_stream=stream,
        consumer_group=CONSUMER_GROUP_FOR_STREAM[stream],
    )
    health.mark_critical("postgres", "redis")
    start_health_server(
        port=int(os.environ.get("EVENT_PRODUCER_HEALTH_PORT", "8207")),
        state=health,
    )
    start_probe_loop(
        health,
        targets={
            "postgres": {"database_url": DATABASE_URL},
            "redis": {"redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379")},
        },
        consumer_probe=(stream, CONSUMER_GROUP_FOR_STREAM[stream]),
    )

    run_worker(engine, r, worker_id)


if __name__ == "__main__":
    main()
