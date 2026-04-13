"""
vil-embedding-worker — embedding_upsert consumer.

Worker loop:
  consume_next → claim_job → LeaseHeartbeat → run_embedding_job
  → release_job → verify lease → commit → enqueue_dlq (if terminal) → ACK

ACK contract (strictly enforced):
  claim fails           → no ACK
  lease stolen          → no ACK, no commit
  lease lost pre-commit → no ACK, no commit
  succeeded / retry_wait → release_job → verify lease → commit → ACK
  failed_terminal       → release_job → verify lease → commit
                          → enqueue_dlq → ACK

Postgres commit is always durable before any Redis write.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from services.vil_embedding_worker.executor import run_embedding_job
from shared.clients.embedding import EMBEDDING_MODEL, get_embedding_client
from shared.clients.qdrant import QDRANT_URL, ensure_collection
from shared.clients.vector_store_qdrant import QdrantVectorStore
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
)

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/vil")


# ---------------------------------------------------------------------------
# Message processor
# ---------------------------------------------------------------------------

def _process_message(engine, r, msg, worker_id: str, embedding_client, vector_store) -> bool:
    """
    Process one stream message end-to-end.

    Returns True if the message was ACK'd.
    Returns False if not ACK'd (message stays in PEL for reclaimer).
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
    # Steps 2–N: execute within a single connection + heartbeat
    # ------------------------------------------------------------------
    job_state = "failed_terminal"
    last_error: str | None = None

    with engine.connect() as conn:
        with LeaseHeartbeat(engine, job_id, worker_id) as hb:

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
                return False

            try:
                job_state = run_embedding_job(
                    conn,
                    payload=job_row.payload,
                    worker_id=worker_id,
                    embedding_client=embedding_client,
                    vector_store=vector_store,
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

    # ------------------------------------------------------------------
    # Post-commit: DLQ if terminal
    # ------------------------------------------------------------------
    if job_state == "failed_terminal":
        try:
            enqueue_dlq(
                r,
                job_type="embedding_upsert",
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
        "_process_message: completed job_id=%s state=%s", job_id, job_state
    )
    return True


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def run_worker(engine, r, worker_id: str, embedding_client, vector_store) -> None:
    """
    Main worker loop: block-reads and processes embedding_upsert jobs indefinitely.
    """
    stream = STREAM_FOR_JOB_TYPE["embedding_upsert"]
    group = GROUP_FOR_JOB_TYPE["embedding_upsert"]

    log.info(
        "worker starting worker_id=%s stream=%s group=%s model=%s",
        worker_id, stream, group, EMBEDDING_MODEL,
    )

    while True:
        msg = consume_next(r, stream=stream, group=group, consumer_id=worker_id)
        if msg is None:
            continue

        log.debug("worker: received job_id=%s", msg.job_id)
        try:
            _process_message(engine, r, msg, worker_id, embedding_client, vector_store)
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

    embedding_client = get_embedding_client()
    log.info("embedding model: %s", EMBEDDING_MODEL)

    # Warm up model and ensure Qdrant collection exists before consuming jobs.
    log.info("warming up embedding model...")
    probe_vector = embedding_client.embed("warmup")
    ensure_collection(vector_size=len(probe_vector))
    log.info(
        "Qdrant ready: %s collection=vil_summaries dim=%d",
        QDRANT_URL, len(probe_vector),
    )

    vector_store = QdrantVectorStore()

    bootstrap_streams(r)
    worker_id = generate_worker_id()

    run_worker(engine, r, worker_id, embedding_client, vector_store)


if __name__ == "__main__":
    main()
