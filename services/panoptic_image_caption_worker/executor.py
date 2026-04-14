"""
image_caption job executor.

Steps:
  1. Fetch panoptic_images row by image_id (from payload).
  2. If caption_status == 'success': return succeeded (idempotent no-op).
  3. Read JPEG from storage_path, encode as base64 data URI.
  4. Call VLM with caption prompt + image.
  5. Parse JSON response, extract caption text.
  6. UPDATE panoptic_images SET caption_status='success', caption_text, caption_model.

Returns a tuple of (job_state, should_chain):
  ("succeeded", True)   — caption generated, caller should enqueue caption_embed
  ("succeeded", False)  — already complete, no chaining needed
  ("failed_terminal", False) — permanent failure
  ("retry_wait", False)      — transient failure, retry later

All Postgres writes are within the caller's open transaction.
No commit is issued here; the worker commits after release_job + lease check.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Literal

from sqlalchemy import text

from shared.clients.vlm import VLLM_MODEL, VLMClient

log = logging.getLogger(__name__)

_CAPTION_PROMPT = (
    "Describe what you see in this surveillance camera image in one concise sentence. "
    "Focus on people, vehicles, objects, and activities visible. "
    "Be factual — do not speculate beyond what is visually evident.\n\n"
    'Respond with JSON: {"caption": "<your one-sentence description>"}'
)

JobState = Literal["succeeded", "failed_terminal", "retry_wait"]


def run_image_caption_job(
    conn,
    payload: dict,
    worker_id: str,
    vlm_client: VLMClient,
) -> tuple[JobState, bool]:
    """
    Execute an image_caption job.

    Returns
    -------
    (job_state, should_chain)
        should_chain is True when a new caption was generated and the caller
        should create a caption_embed job.
    """
    image_id = payload["image_id"]

    # ------------------------------------------------------------------
    # Step 1: Fetch image row
    # ------------------------------------------------------------------
    row = conn.execute(
        text("""
            SELECT image_id, storage_path, caption_status
              FROM panoptic_images
             WHERE image_id = :image_id
        """),
        {"image_id": image_id},
    ).fetchone()

    if row is None:
        log.error("run_image_caption_job: image_id=%s not found", image_id)
        return ("failed_terminal", False)

    # ------------------------------------------------------------------
    # Step 2: Idempotency check
    # ------------------------------------------------------------------
    if row.caption_status == "success":
        log.info("run_image_caption_job: image_id=%s already captioned — no-op", image_id)
        return ("succeeded", False)

    # ------------------------------------------------------------------
    # Step 3: Read JPEG and encode as data URI
    # ------------------------------------------------------------------
    try:
        with open(row.storage_path, "rb") as f:
            jpeg_bytes = f.read()
    except FileNotFoundError:
        log.error("run_image_caption_job: file missing image_id=%s path=%s", image_id, row.storage_path)
        return ("failed_terminal", False)

    data_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg_bytes).decode()}"

    # ------------------------------------------------------------------
    # Steps 4-5: Call VLM and parse response
    # ------------------------------------------------------------------
    log.info("run_image_caption_job: captioning image_id=%s worker=%s", image_id, worker_id)

    raw_text = vlm_client.call(
        prompt_text=_CAPTION_PROMPT,
        frame_uris=[data_uri],
    )

    # Parse JSON response — expect {"caption": "..."}
    try:
        parsed = json.loads(raw_text)
        caption = parsed["caption"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning(
            "run_image_caption_job: JSON parse failed image_id=%s raw=%s: %s",
            image_id, raw_text[:200], exc,
        )
        # Try to use the raw text as a fallback caption
        caption = raw_text.strip()
        if not caption:
            log.error("run_image_caption_job: empty caption image_id=%s", image_id)
            conn.execute(
                text("""
                    UPDATE panoptic_images
                       SET caption_status = 'failed',
                           updated_at     = now()
                     WHERE image_id = :image_id
                """),
                {"image_id": image_id},
            )
            return ("failed_terminal", False)

    # ------------------------------------------------------------------
    # Step 6: Update Postgres
    # ------------------------------------------------------------------
    conn.execute(
        text("""
            UPDATE panoptic_images
               SET caption_status = 'success',
                   caption_model  = :caption_model,
                   caption_text   = :caption_text,
                   updated_at     = now()
             WHERE image_id = :image_id
        """),
        {
            "image_id": image_id,
            "caption_model": VLLM_MODEL,
            "caption_text": caption,
        },
    )
    log.info("run_image_caption_job: caption_status=success image_id=%s", image_id)

    return ("succeeded", True)
