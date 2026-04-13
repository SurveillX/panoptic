"""
vil-summary-agent worker — bucket_summary consumer.

Worker loop:
  consume_next → claim_job → LeaseHeartbeat → run_bucket_summary
  → release_job → verify lease → commit → enqueue embedding/rollup → ACK

ACK contract (strictly enforced):
  claim fails           → no ACK
  lease stolen          → no ACK, no commit
  lease lost pre-commit → no ACK, no commit, no enqueue
  succeeded/degraded    → release_job → verify lease → commit
                          → enqueue embedding → enqueue rollup → ACK
  retry_wait            → release_job → verify lease → commit → ACK
  failed_terminal       → release_job → verify lease → commit
                          → enqueue_dlq → ACK

Redis enqueues happen only AFTER the Postgres commit is durable.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from services.vil_summary_agent.executor import ExecutionResult, run_bucket_summary
from shared.utils.leases import (
    LeaseHeartbeat,
    claim_job,
    compute_retry_delay,
    generate_worker_id,
    release_job,
)
from shared.clients.continuum import CONTINUUM_BASE_URL_TEMPLATE, get_continuum_client
from shared.clients.keyframe import KEYFRAME_BASE_URL, KEYFRAME_TOKEN, get_keyframe_client
from shared.clients.vlm import VLLM_BASE_URL, get_vlm_client
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

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/vil")


# ---------------------------------------------------------------------------
# Message processor
# ---------------------------------------------------------------------------

def _process_message(engine, r, msg, worker_id: str, keyframe_client, vlm_client, continuum_client=None) -> bool:
    """
    Process one stream message end-to-end.

    Returns True if the message was ACK'd.
    Returns False if not ACK'd (message must stay in PEL for reclaimer).
    """
    job_id = msg.job_id

    # ------------------------------------------------------------------
    # Step 1: Claim job (committed immediately so heartbeat can see it)
    # ------------------------------------------------------------------
    with engine.connect() as conn:
        claim = claim_job(conn, job_id=job_id, worker_id=worker_id)
        conn.commit()

    if claim is None:
        log.debug("_process_message: job_id=%s not claimable — no ACK", job_id)
        return False  # leave in PEL

    # ------------------------------------------------------------------
    # Steps 2–19: execute within a single connection + heartbeat
    # ------------------------------------------------------------------
    exec_result: ExecutionResult | None = None

    with engine.connect() as conn:
        with LeaseHeartbeat(engine, job_id, worker_id) as hb:

            # Load the job payload now that the claim is committed
            job_row = conn.execute(
                text("""
                    SELECT payload, attempt_count, max_attempts
                      FROM vil_jobs
                     WHERE job_id = :job_id
                """),
                {"job_id": job_id},
            ).fetchone()

            if job_row is None:
                log.error(
                    "_process_message: job_id=%s missing from vil_jobs after claim",
                    job_id,
                )
                return False  # should never happen; leave in PEL

            # Steps 2–14: execute (all DB writes, no commit yet)
            try:
                exec_result = run_bucket_summary(
                    conn,
                    payload=job_row.payload,
                    worker_id=worker_id,
                    attempt_count=claim.attempt_count,
                    keyframe_client=keyframe_client,
                    vlm_client=vlm_client,
                    continuum_client=continuum_client,
                )
            except Exception as exc:
                log.exception(
                    "_process_message: unexpected error job_id=%s: %s", job_id, exc
                )
                new_state = (
                    "failed_terminal"
                    if claim.attempt_count >= claim.max_attempts
                    else "retry_wait"
                )
                exec_result = ExecutionResult(
                    job_state=new_state,
                    last_error=str(exc)[:1000],
                    summary_id="",
                    embedding_job_id=None,
                    rollup_job_id=None,
                    rollup_serial_number=None,
                )

            # Step 14 (continued): release_job — all within same transaction
            retry_delay = (
                compute_retry_delay(claim.attempt_count)
                if exec_result.job_state == "retry_wait"
                else None
            )
            released = release_job(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                new_state=exec_result.job_state,
                last_error=exec_result.last_error,
                retry_after_seconds=retry_delay,
            )

            if not released:
                log.warning(
                    "_process_message: lease stolen before release job_id=%s — abort",
                    job_id,
                )
                # Explicit rollback: discard all partial writes (summary,
                # rollup state, job rows) accumulated since the last commit.
                # SQLAlchemy 2.x also rolls back implicitly on connection
                # close, but we make it explicit here so the intent is clear
                # and the rollback is not deferred to context-manager teardown.
                conn.rollback()
                return False  # no commit, no ACK

            # Step 15: verify lease still valid immediately before commit
            if not hb.is_valid():
                log.warning(
                    "_process_message: lease lost pre-commit job_id=%s — abort",
                    job_id,
                )
                # Explicit rollback: discard upsert_summary, upsert_rollup_state,
                # insert_embedding_job, insert_rollup_job, and release_job writes.
                # None of these must reach Postgres.  No enqueue_job, no ACK.
                conn.rollback()
                return False

            # Step 16: commit — all DB state durable before any Redis write
            conn.commit()

    # HeartbeatLease context exited (thread stopped) before post-commit work.

    # ------------------------------------------------------------------
    # Steps 17–18: Post-commit enqueues (only after commit is durable)
    # ------------------------------------------------------------------
    if exec_result.job_state in ("succeeded", "degraded"):
        if exec_result.embedding_job_id:
            try:
                enqueue_job(
                    r,
                    job_type="embedding_upsert",
                    job_id=exec_result.embedding_job_id,
                    serial_number=claim.serial_number,
                    priority="normal",
                )
            except Exception as exc:
                log.error(
                    "_process_message: embedding enqueue failed "
                    "embedding_job_id=%s bucket_job_id=%s: %s",
                    exec_result.embedding_job_id, job_id, exc,
                )
                # job is 'pending' in Postgres; orchestrator scan will re-enqueue

        if exec_result.rollup_job_id:
            try:
                enqueue_job(
                    r,
                    job_type="rollup_summary",
                    job_id=exec_result.rollup_job_id,
                    serial_number=exec_result.rollup_serial_number,
                    priority="normal",
                )
            except Exception as exc:
                log.error(
                    "_process_message: rollup enqueue failed "
                    "rollup_job_id=%s bucket_job_id=%s: %s",
                    exec_result.rollup_job_id, job_id, exc,
                )

    elif exec_result.job_state == "failed_terminal":
        try:
            enqueue_dlq(
                r,
                job_type="bucket_summary",
                job_id=job_id,
                serial_number=claim.serial_number,
                reason=exec_result.last_error or "unknown error",
            )
        except Exception as exc:
            log.error(
                "_process_message: DLQ enqueue failed job_id=%s: %s", job_id, exc
            )
            # failed_terminal is already committed in Postgres; DLQ is informational

    # ------------------------------------------------------------------
    # Step 19: ACK — always last, only after commit
    # ------------------------------------------------------------------
    ack_message(r, stream=msg.stream, group=msg.group, entry_id=msg.entry_id)
    log.info(
        "_process_message: completed job_id=%s state=%s",
        job_id, exec_result.job_state,
    )
    return True


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def run_worker(engine, r, worker_id: str, keyframe_client, vlm_client, continuum_client=None) -> None:
    """
    Main worker loop: block-reads and processes bucket_summary jobs indefinitely.

    Catches all per-message exceptions so a single bad message cannot crash
    the loop.  Unhandled errors leave the message in the PEL for the reclaimer.
    """
    stream = STREAM_FOR_JOB_TYPE["bucket_summary"]
    group = GROUP_FOR_JOB_TYPE["bucket_summary"]

    log.info(
        "worker starting worker_id=%s stream=%s group=%s frames=%s vlm=%s",
        worker_id, stream, group,
        "enabled" if keyframe_client is not None else "disabled",
        "enabled" if vlm_client is not None else "stub",
    )

    while True:
        print("WORKER polling for jobs...")
        msg = consume_next(r, stream=stream, group=group, consumer_id=worker_id)
        if msg is None:
            continue  # block timeout — loop and wait again

        print("WORKER received job:", msg.job_id)
        log.debug("worker: received job_id=%s", msg.job_id)
        try:
            _process_message(engine, r, msg, worker_id, keyframe_client, vlm_client, continuum_client)
        except Exception as exc:
            log.exception(
                "worker: unhandled error for job_id=%s — message stays in PEL: %s",
                msg.job_id, exc,
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Bootstrap and start the worker process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()

    if KEYFRAME_BASE_URL and KEYFRAME_TOKEN:
        keyframe_client = get_keyframe_client()
        log.info("Keyframe API configured: %s", KEYFRAME_BASE_URL)
    else:
        keyframe_client = None
        log.warning(
            "KEYFRAME_BASE_URL or KEYFRAME_TOKEN not set — "
            "frame fetching disabled; all summaries will be metadata_only"
        )

    if VLLM_BASE_URL:
        vlm_client = get_vlm_client()
        log.info("vLLM configured: %s model=%s", VLLM_BASE_URL, vlm_client._model)
    else:
        vlm_client = None
        log.warning(
            "VLLM_BASE_URL not set — vLLM disabled; "
            "all summaries will use call_llm_stub"
        )

    # Continuum client takes precedence over KeyframeClient when set
    if CONTINUUM_BASE_URL_TEMPLATE and "{serial_number}" in CONTINUUM_BASE_URL_TEMPLATE:
        continuum_client = get_continuum_client()
        log.info("Continuum configured: %s", CONTINUUM_BASE_URL_TEMPLATE)
    else:
        continuum_client = None

    bootstrap_streams(r)
    worker_id = generate_worker_id()

    run_worker(engine, r, worker_id, keyframe_client, vlm_client, continuum_client)


if __name__ == "__main__":
    main()
