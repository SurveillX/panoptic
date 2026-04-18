"""
panoptic-image-embed-worker — image_embed consumer.

VL-native image embedding. Runs the same worker skeleton as
caption_embed_worker — consume → claim → heartbeat → execute → release
→ commit → (DLQ on terminal) → ACK — with VLEmbeddingClient instead of
the text EmbeddingClient.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text

from services.panoptic_image_embed_worker.executor import run_image_embed_job
from shared.clients.qdrant import QDRANT_URL, ensure_image_vector_collection
from shared.clients.vl_embedding import VL_EMBEDDING_MODEL_ID, get_vl_embedding_client
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


def _process_message(engine, r, msg, worker_id: str, vl_client) -> bool:
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
                job_state = run_image_embed_job(
                    conn,
                    payload=job_row.payload,
                    worker_id=worker_id,
                    vl_client=vl_client,
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
                job_type="image_embed",
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


def run_worker(engine, r, worker_id: str, vl_client) -> None:
    stream = STREAM_FOR_JOB_TYPE["image_embed"]
    group = GROUP_FOR_JOB_TYPE["image_embed"]

    log.info(
        "worker starting worker_id=%s stream=%s group=%s model=%s",
        worker_id, stream, group, VL_EMBEDDING_MODEL_ID,
    )

    while True:
        msg = consume_next(r, stream=stream, group=group, consumer_id=worker_id)
        if msg is None:
            continue

        log.debug("worker: received job_id=%s", msg.job_id)
        try:
            _process_message(engine, r, msg, worker_id, vl_client)
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

    vl_client = get_vl_embedding_client()
    log.info("VL embedding model: %s", VL_EMBEDDING_MODEL_ID)

    # Probe the VL embedding dimension by pushing a tiny all-white JPEG.
    # This also warms the VL model if it's cold and confirms the collection
    # dimension. Cheaper than maintaining a hardcoded constant.
    import io
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), "white").save(buf, format="JPEG", quality=80)
    log.info("probing VL embedding dim...")
    probe_vec = vl_client.embed_image_bytes(buf.getvalue())
    ensure_image_vector_collection(vector_size=len(probe_vec))
    log.info(
        "Qdrant ready: %s collection=panoptic_image_vectors dim=%d",
        QDRANT_URL, len(probe_vec),
    )

    bootstrap_streams(r)
    worker_id = generate_worker_id()

    # Health endpoint + background dep probes
    stream = STREAM_FOR_JOB_TYPE["image_embed"]
    health = HealthState(
        service_name="panoptic_image_embed_worker",
        worker_id=worker_id,
        consumer_stream=stream,
        consumer_group=CONSUMER_GROUP_FOR_STREAM[stream],
    )
    health.mark_critical("postgres", "redis", "qdrant", "retrieval")
    start_health_server(
        port=int(os.environ.get("IMAGE_EMBED_HEALTH_PORT", "8206")),
        state=health,
    )
    start_probe_loop(
        health,
        targets={
            "postgres": {"database_url": DATABASE_URL},
            "redis": {"redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379")},
            "qdrant": {"qdrant_url": QDRANT_URL},
            "retrieval": {"retrieval_url": os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")},
        },
        consumer_probe=(stream, CONSUMER_GROUP_FOR_STREAM[stream]),
    )

    run_worker(engine, r, worker_id, vl_client)


if __name__ == "__main__":
    main()
