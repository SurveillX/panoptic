"""
image_embed job executor.

VL-native image embedding step. Runs after caption_embed completes on
every captioned image — independent of captions in terms of semantics
(pixel similarity vs caption-text similarity), so both embeddings live
side by side and a future Search API branch can blend them.

Steps:
  1. Fetch panoptic_images row by image_id.
  2. If image_embedding_status == 'success': no-op idempotently.
  3. Read the JPEG bytes from storage_path (must exist on disk).
  4. POST /embed_visual via VLEmbeddingClient → 4096-dim vector.
  5. Upsert into Qdrant panoptic_image_vectors (mirror of caption flow).
  6. UPDATE panoptic_images SET image_embedding_status='success'.

Write ordering matches caption_embed: Qdrant upsert before Postgres
UPDATE, so on commit failure the retry re-does both idempotently.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from sqlalchemy import text

from shared.clients.qdrant import upsert_image_vector_point
from shared.clients.vl_embedding import VL_EMBEDDING_MODEL_ID, VLEmbeddingClient

log = logging.getLogger(__name__)


def run_image_embed_job(
    conn,
    payload: dict,
    worker_id: str,
    vl_client: VLEmbeddingClient,
) -> Literal["succeeded", "failed_terminal"]:
    image_id = payload["image_id"]

    row = conn.execute(
        text("""
            SELECT image_id, serial_number, camera_id, scope_id,
                   bucket_start_utc, bucket_end_utc, captured_at_utc,
                   trigger, storage_path, caption_text,
                   image_embedding_status
              FROM panoptic_images
             WHERE image_id = :image_id
        """),
        {"image_id": image_id},
    ).fetchone()

    if row is None:
        log.error("run_image_embed_job: image_id=%s not found", image_id)
        return "failed_terminal"

    if row.image_embedding_status == "success":
        log.info(
            "run_image_embed_job: image_id=%s already embedded — no-op", image_id
        )
        return "succeeded"

    if not row.storage_path or not os.path.exists(row.storage_path):
        log.error(
            "run_image_embed_job: image_id=%s storage_path missing or inaccessible: %s",
            image_id, row.storage_path,
        )
        return "failed_terminal"

    log.info(
        "run_image_embed_job: embedding image_id=%s worker=%s path=%s",
        image_id, worker_id, row.storage_path,
    )
    with open(row.storage_path, "rb") as f:
        image_bytes = f.read()

    # Pass the caption text (when available) as co-item text for richer
    # grounding. Empty string is fine.
    vector = vl_client.embed_image_bytes(
        image_bytes, text=(row.caption_text or "")
    )
    log.info(
        "run_image_embed_job: embedding created image_id=%s dim=%d",
        image_id, len(vector),
    )

    def _ts_str(val) -> str | None:
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    qdrant_payload = {
        "record_type":   "image_vector",
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
        "storage_path":  row.storage_path,
    }
    qdrant_id = upsert_image_vector_point(image_id, vector, qdrant_payload)
    log.info("run_image_embed_job: Qdrant upsert success image_id=%s", image_id)

    conn.execute(
        text("""
            UPDATE panoptic_images
               SET image_embedding_status    = 'success',
                   image_embedding_model     = :embedding_model,
                   image_embedding_vector_id = :vector_id,
                   updated_at                = now()
             WHERE image_id = :image_id
        """),
        {
            "image_id": image_id,
            "embedding_model": VL_EMBEDDING_MODEL_ID,
            "vector_id": qdrant_id,
        },
    )
    log.info(
        "run_image_embed_job: image_embedding_status=success image_id=%s", image_id
    )

    return "succeeded"
