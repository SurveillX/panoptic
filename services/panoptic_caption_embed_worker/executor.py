"""
caption_embed job executor.

Steps:
  1. Fetch panoptic_images row by image_id (from payload).
  2. If caption_embedding_status == 'success': return succeeded (idempotent no-op).
  3. Require caption_text exists and caption_status == 'success'.
  4. Call embedding_client.embed(caption_text) → dense vector.
  5. Upsert point into Qdrant image_caption_vectors collection.
  6. UPDATE panoptic_images SET caption_embedding_status = 'success'.

All Postgres writes are within the caller's open transaction.
No commit is issued here; the worker commits after release_job + lease check.

Qdrant upsert (step 5) happens before the Postgres UPDATE (step 6) so that
on commit failure the job retries from scratch: re-embed + re-upsert, both safe.
"""

from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import text

from shared.clients.embedding import EMBEDDING_MODEL, EmbeddingClient
from shared.clients.qdrant import upsert_image_caption_point

log = logging.getLogger(__name__)


def run_caption_embed_job(
    conn,
    payload: dict,
    worker_id: str,
    embedding_client: EmbeddingClient,
) -> Literal["succeeded", "failed_terminal", "retry_wait"]:
    """
    Execute a caption_embed job.

    Returns
    -------
    'succeeded'       — embedding created and stored, status updated
    'failed_terminal' — image/caption not found (permanent failure)
    """
    image_id = payload["image_id"]

    # ------------------------------------------------------------------
    # Step 1: Fetch image row
    # ------------------------------------------------------------------
    row = conn.execute(
        text("""
            SELECT image_id, serial_number, camera_id, scope_id,
                   bucket_start_utc, bucket_end_utc, captured_at_utc,
                   trigger, caption_status, caption_text,
                   caption_embedding_status
              FROM panoptic_images
             WHERE image_id = :image_id
        """),
        {"image_id": image_id},
    ).fetchone()

    if row is None:
        log.error("run_caption_embed_job: image_id=%s not found", image_id)
        return "failed_terminal"

    # ------------------------------------------------------------------
    # Step 2: Idempotency check
    # ------------------------------------------------------------------
    if row.caption_embedding_status == "success":
        log.info(
            "run_caption_embed_job: image_id=%s already embedded — no-op", image_id
        )
        return "succeeded"

    # ------------------------------------------------------------------
    # Step 3: Require caption
    # ------------------------------------------------------------------
    if row.caption_status != "success" or not row.caption_text:
        log.error(
            "run_caption_embed_job: image_id=%s caption not ready "
            "(status=%s, text=%s)",
            image_id, row.caption_status,
            "present" if row.caption_text else "None",
        )
        return "failed_terminal"

    # ------------------------------------------------------------------
    # Step 4: Generate embedding
    # ------------------------------------------------------------------
    log.info(
        "run_caption_embed_job: embedding image_id=%s worker=%s", image_id, worker_id
    )
    vector = embedding_client.embed(row.caption_text)
    log.info(
        "run_caption_embed_job: embedding created image_id=%s dim=%d",
        image_id, len(vector),
    )

    # ------------------------------------------------------------------
    # Step 5: Upsert into Qdrant (external, before Postgres UPDATE)
    # ------------------------------------------------------------------
    def _ts_str(val) -> str | None:
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    qdrant_payload = {
        "record_type":   "image_caption",
        "record_id":     image_id,
        "image_id":      image_id,
        "serial_number": row.serial_number,
        "camera_id":     row.camera_id,
        "scope_id":      row.scope_id,
        "bucket_start":  _ts_str(row.bucket_start_utc),
        "bucket_end":    _ts_str(row.bucket_end_utc),
        "captured_at":   _ts_str(row.captured_at_utc),
        "trigger":       row.trigger,
        "caption_text":  row.caption_text,
    }
    qdrant_id = upsert_image_caption_point(image_id, vector, qdrant_payload)
    log.info("run_caption_embed_job: Qdrant upsert success image_id=%s", image_id)

    # ------------------------------------------------------------------
    # Step 6: Mark embedding complete in Postgres
    # ------------------------------------------------------------------
    conn.execute(
        text("""
            UPDATE panoptic_images
               SET caption_embedding_status    = 'success',
                   caption_embedding_model     = :embedding_model,
                   caption_embedding_vector_id = :vector_id,
                   updated_at                  = now()
             WHERE image_id = :image_id
        """),
        {
            "image_id": image_id,
            "embedding_model": EMBEDDING_MODEL,
            "vector_id": qdrant_id,
        },
    )
    log.info(
        "run_caption_embed_job: caption_embedding_status=success image_id=%s",
        image_id,
    )

    return "succeeded"
