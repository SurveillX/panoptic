"""
embedding_upsert job executor.

Steps:
  1. Fetch summary row from panoptic_summaries by summary_id (from payload).
  2. If embedding_status == 'complete': return succeeded (idempotent no-op).
  3. Build embedding input: text = summary field.
  4. Call embedding_client.embed(text) → dense vector.
  5. Upsert point into Qdrant (external write, idempotent on retry).
  6. UPDATE panoptic_summaries SET embedding_status = 'complete'.

All Postgres writes are within the caller's open transaction.
No commit is issued here; the worker commits after release_job + lease check.

Qdrant upsert (step 5) happens before the Postgres UPDATE (step 6) so that
on commit failure the job retries from scratch: re-embed + re-upsert, both safe.
"""

from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import text

from shared.clients.embedding import EmbeddingClient
from shared.clients.vector_store import VectorStore

log = logging.getLogger(__name__)


def run_embedding_job(
    conn,
    payload: dict,
    worker_id: str,
    embedding_client: EmbeddingClient,
    vector_store: VectorStore,
) -> Literal["succeeded", "failed_terminal", "retry_wait"]:
    """
    Execute an embedding_upsert job.

    Parameters
    ----------
    conn             : open SQLAlchemy connection (transaction in progress)
    payload          : panoptic_jobs.payload dict — must contain 'summary_id'
    worker_id        : worker identity (for logging)
    embedding_client : EmbeddingClient instance

    Returns
    -------
    'succeeded'       — embedding created and stored, status updated
    'failed_terminal' — summary not found (permanent failure, do not retry)
    """
    summary_id = payload["summary_id"]

    # ------------------------------------------------------------------
    # Step 1: Fetch summary
    # ------------------------------------------------------------------
    row = conn.execute(
        text("""
            SELECT summary_id, serial_number, level, scope_id,
                   start_time, end_time, summary, key_events, confidence,
                   embedding_status
              FROM panoptic_summaries
             WHERE summary_id = :summary_id
        """),
        {"summary_id": summary_id},
    ).fetchone()

    if row is None:
        log.error(
            "run_embedding_job: summary_id=%s not found in panoptic_summaries", summary_id
        )
        return "failed_terminal"

    # ------------------------------------------------------------------
    # Step 2: Idempotency check
    # ------------------------------------------------------------------
    if row.embedding_status == "complete":
        log.info(
            "run_embedding_job: summary_id=%s already complete — no-op", summary_id
        )
        return "succeeded"

    # ------------------------------------------------------------------
    # Steps 3–4: Build input and generate embedding
    # ------------------------------------------------------------------
    log.info(
        "run_embedding_job: embedding summary_id=%s worker=%s", summary_id, worker_id
    )
    # Strict canonical mapping — order matters (most specific first)
    _LABEL_CONTAINS = [
        ("underperform",      "underperforming"),
        ("late",              "late start"),
        ("after",             "after hours activity"),
        ("start",             "start of activity"),
        ("spike",             "spike in activity"),
        ("drop",              "drop in activity"),
    ]

    def _normalize_label(label: str) -> str | None:
        lower = label.lower()
        for keyword, canonical in _LABEL_CONTAINS:
            if keyword in lower:
                return canonical
        return None  # discard unrecognized labels

    key_events = row.key_events or []
    event_labels = [e.get("label", str(e)) if isinstance(e, dict) else str(e) for e in key_events]
    normalized_labels = list({c for l in event_labels if (c := _normalize_label(l)) is not None})
    embed_text = " ".join(normalized_labels) + " " + row.summary
    vector = embedding_client.embed(embed_text)
    log.info(
        "run_embedding_job: embedding created summary_id=%s dim=%d",
        summary_id, len(vector),
    )

    # ------------------------------------------------------------------
    # Step 5: Upsert into Qdrant (external, before Postgres UPDATE)
    # Idempotent: safe to retry if commit fails.
    # ------------------------------------------------------------------
    qdrant_payload = {
        "serial_number":  row.serial_number,
        "level":      row.level,
        "scope_id":   row.scope_id,
        "start_time": row.start_time.isoformat() if hasattr(row.start_time, "isoformat") else str(row.start_time),
        "end_time":   row.end_time.isoformat() if hasattr(row.end_time, "isoformat") else str(row.end_time),
        "summary":          row.summary,
        "key_events":       event_labels,
        "key_events_labels": normalized_labels,
        "confidence":       row.confidence,
    }
    vector_store.upsert(summary_id, vector, qdrant_payload)
    log.info("run_embedding_job: Qdrant upsert success summary_id=%s", summary_id)

    # ------------------------------------------------------------------
    # Step 6: Mark embedding complete in Postgres
    # ------------------------------------------------------------------
    conn.execute(
        text("""
            UPDATE panoptic_summaries
               SET embedding_status = 'complete',
                   updated_at       = now()
             WHERE summary_id = :summary_id
        """),
        {"summary_id": summary_id},
    )
    log.info(
        "run_embedding_job: embedding_status=complete summary_id=%s", summary_id
    )

    return "succeeded"
