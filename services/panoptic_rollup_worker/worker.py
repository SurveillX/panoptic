"""
panoptic-rollup-worker — rollup_summary consumer (L2 hour-level).

Worker loop:
  consume_next → claim_job → LeaseHeartbeat → run_rollup_job
  → release_job → verify lease → commit → enqueue embedding → ACK

ACK contract (strictly enforced):
  claim fails           → no ACK
  lease stolen          → no ACK, no commit
  lease lost pre-commit → no ACK, no commit
  succeeded/degraded    → release_job → verify lease → commit
                          → enqueue embedding → ACK
  retry_wait            → release_job → verify lease → commit → ACK
  failed_terminal       → release_job → verify lease → commit
                          → enqueue_dlq → ACK

Redis enqueues happen only AFTER the Postgres commit is durable.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from services.panoptic_rollup_worker.executor import RollupResult, run_rollup_job
from shared.clients.vlm import VLLM_BASE_URL, get_vlm_client
from shared.utils.leases import (
    LeaseHeartbeat,
    claim_job,
    compute_retry_delay,
    generate_worker_id,
    release_job,
)
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import (
    GROUP_FOR_JOB_TYPE,
    STREAM_FOR_JOB_TYPE,
    ack_message,
    bootstrap_streams,
    consume_next,
    enqueue_dlq,
    enqueue_job,
)

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")


# ---------------------------------------------------------------------------
# Message processor
# ---------------------------------------------------------------------------

def _process_message(engine, r, msg, worker_id: str, vlm_client) -> bool:
    """
    Process one rollup_summary stream message end-to-end.

    Returns True if ACK'd, False if left in PEL for reclaimer.
    """
    job_id = msg.job_id

    # ------------------------------------------------------------------
    # Step 1: Claim job
    # ------------------------------------------------------------------
    with engine.connect() as conn:
        claim = claim_job(conn, job_id=job_id, worker_id=worker_id)
        conn.commit()

    if claim is None:
        log.debug("_process_message: job_id=%s not claimable — no ACK", job_id)
        return False

    # ------------------------------------------------------------------
    # Steps 2–N: execute within single connection + heartbeat
    # ------------------------------------------------------------------
    result: RollupResult = RollupResult(job_state="failed_terminal", embedding_job_id=None)

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
                log.error(
                    "_process_message: job_id=%s missing from panoptic_jobs after claim",
                    job_id,
                )
                return False

            last_error: str | None = None

            try:
                result = run_rollup_job(
                    conn,
                    payload=job_row.payload,
                    worker_id=worker_id,
                    attempt_count=job_row.attempt_count,
                    vlm_client=vlm_client,
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
                result = RollupResult(job_state=job_state, embedding_job_id=None)
                last_error = str(exc)[:1000]

            retry_delay = (
                compute_retry_delay(job_row.attempt_count)
                if result.job_state == "retry_wait"
                else None
            )
            released = release_job(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                new_state=result.job_state,
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

    # HeartbeatLease context exited before post-commit work.

    # ------------------------------------------------------------------
    # Post-commit enqueues (only after commit is durable)
    # ------------------------------------------------------------------
    if result.job_state in ("succeeded", "degraded"):
        if result.embedding_job_id:
            try:
                enqueue_job(
                    r,
                    job_type="embedding_upsert",
                    job_id=result.embedding_job_id,
                    serial_number=claim.serial_number,
                    priority="normal",
                )
            except Exception as exc:
                log.error(
                    "_process_message: embedding enqueue failed "
                    "embedding_job_id=%s rollup_job_id=%s: %s",
                    result.embedding_job_id, job_id, exc,
                )
                # job is 'pending' in Postgres; orchestrator scan will re-enqueue

    elif result.job_state == "failed_terminal":
        try:
            enqueue_dlq(
                r,
                job_type="rollup_summary",
                job_id=job_id,
                serial_number=claim.serial_number,
                reason=last_error or "unknown error",
            )
        except Exception as exc:
            log.error(
                "_process_message: DLQ enqueue failed job_id=%s: %s", job_id, exc
            )

    ack_message(r, stream=msg.stream, group=msg.group, entry_id=msg.entry_id)
    log.info(
        "_process_message: completed job_id=%s state=%s",
        job_id, result.job_state,
    )
    return True


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def run_worker(engine, r, worker_id: str, vlm_client) -> None:
    """
    Main worker loop: block-reads and processes rollup_summary jobs indefinitely.
    """
    stream = STREAM_FOR_JOB_TYPE["rollup_summary"]
    group = GROUP_FOR_JOB_TYPE["rollup_summary"]

    log.info(
        "worker starting worker_id=%s stream=%s group=%s vlm=%s",
        worker_id, stream, group,
        "enabled" if vlm_client is not None else "stub",
    )

    while True:
        msg = consume_next(r, stream=stream, group=group, consumer_id=worker_id)
        if msg is None:
            continue

        log.debug("worker: received job_id=%s", msg.job_id)
        try:
            _process_message(engine, r, msg, worker_id, vlm_client)
        except Exception as exc:
            log.exception(
                "worker: unhandled error for job_id=%s — message stays in PEL: %s",
                msg.job_id, exc,
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Bootstrap and start the rollup worker process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()

    if VLLM_BASE_URL:
        vlm_client = get_vlm_client()
        log.info("vLLM configured: %s model=%s", VLLM_BASE_URL, vlm_client._model)
    else:
        vlm_client = None
        log.warning(
            "VLLM_BASE_URL not set — vLLM disabled; "
            "all rollup summaries will use call_llm_stub"
        )

    bootstrap_streams(r)
    worker_id = generate_worker_id()

    run_worker(engine, r, worker_id, vlm_client)


if __name__ == "__main__":
    main()
