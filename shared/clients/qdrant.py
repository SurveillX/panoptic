"""
Qdrant HTTP client — shared library.

Uses raw HTTP (httpx) instead of the Qdrant SDK to minimize dependencies.

Configuration:
  QDRANT_URL — base URL of the Qdrant instance (default: http://localhost:6333)

Collection:
  name: panoptic_summaries
  distance: Cosine
  vector size: inferred from the first embedding at startup

Point IDs:
  Qdrant requires point IDs to be unsigned integers or UUIDs.
  summary_id is a 64-char sha256 hex string; we take the first 32 chars
  and format as a UUID (deterministic, negligible collision probability).
"""

from __future__ import annotations

import logging
import os
import uuid as _uuid

import httpx

log = logging.getLogger(__name__)

QDRANT_URL: str = os.environ.get("QDRANT_URL", "http://localhost:6333")
_COLLECTION = "panoptic_summaries"
_TIMEOUT = 30.0


def _summary_id_to_qdrant_id(summary_id: str) -> str:
    """
    Convert a sha256 hex summary_id to a UUID string for Qdrant.

    Takes the first 32 hex chars of the 64-char sha256 and formats as UUID.
    Deterministic: same summary_id always maps to the same Qdrant point ID.
    """
    return str(_uuid.UUID(summary_id[:32]))


def ensure_collection(vector_size: int) -> None:
    """
    Create the panoptic_summaries collection if it does not exist.

    Safe to call on every startup — idempotent.
    Raises httpx.HTTPStatusError on unexpected Qdrant errors.
    """
    url = f"{QDRANT_URL}/collections/{_COLLECTION}"
    resp = httpx.get(url, timeout=_TIMEOUT)
    if resp.status_code == 200:
        log.debug("Qdrant collection %s already exists", _COLLECTION)
        return
    create_resp = httpx.put(
        url,
        json={"vectors": {"size": vector_size, "distance": "Cosine"}},
        timeout=_TIMEOUT,
    )
    create_resp.raise_for_status()
    log.info("created Qdrant collection %s size=%d", _COLLECTION, vector_size)


_IMAGE_CAPTION_COLLECTION = "image_caption_vectors"


def ensure_image_caption_collection(vector_size: int) -> None:
    """
    Create the image_caption_vectors collection if it does not exist.

    Safe to call on every startup — idempotent.
    """
    url = f"{QDRANT_URL}/collections/{_IMAGE_CAPTION_COLLECTION}"
    resp = httpx.get(url, timeout=_TIMEOUT)
    if resp.status_code == 200:
        log.debug("Qdrant collection %s already exists", _IMAGE_CAPTION_COLLECTION)
        return
    create_resp = httpx.put(
        url,
        json={"vectors": {"size": vector_size, "distance": "Cosine"}},
        timeout=_TIMEOUT,
    )
    create_resp.raise_for_status()
    log.info("created Qdrant collection %s size=%d", _IMAGE_CAPTION_COLLECTION, vector_size)


def upsert_image_caption_point(image_id: str, vector: list[float], payload: dict) -> str:
    """
    Upsert a single point into the image_caption_vectors collection.

    Returns the Qdrant point ID (UUID string).
    Idempotent — upserting the same image_id overwrites the previous point.
    """
    qdrant_id = str(_uuid.UUID(image_id[:32]))
    url = f"{QDRANT_URL}/collections/{_IMAGE_CAPTION_COLLECTION}/points"
    resp = httpx.put(
        url,
        json={"points": [{"id": qdrant_id, "vector": vector, "payload": payload}]},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    log.debug("upserted Qdrant image caption point image_id=%s qdrant_id=%s", image_id, qdrant_id)
    return qdrant_id


def upsert_point(summary_id: str, vector: list[float], payload: dict) -> None:
    """
    Upsert a single point into the panoptic_summaries collection.

    Idempotent — upserting the same summary_id overwrites the previous point.
    Raises httpx.HTTPStatusError on Qdrant errors.
    """
    qdrant_id = _summary_id_to_qdrant_id(summary_id)
    url = f"{QDRANT_URL}/collections/{_COLLECTION}/points"
    resp = httpx.put(
        url,
        json={"points": [{"id": qdrant_id, "vector": vector, "payload": payload}]},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    log.debug("upserted Qdrant point summary_id=%s qdrant_id=%s", summary_id, qdrant_id)


def _search(collection: str, vector: list[float], payload_filter: dict | None, top_k: int) -> list[dict]:
    """
    Run a points/search query against the given Qdrant collection.

    Returns the list of hits (each: {id, score, payload, ...}).
    If the collection does not exist (404), returns [] and logs a warning
    rather than raising — lets callers degrade gracefully during cold start
    or before workers have populated a new collection.
    """
    url = f"{QDRANT_URL}/collections/{collection}/points/search"
    body: dict = {"vector": vector, "limit": top_k, "with_payload": True}
    if payload_filter:
        body["filter"] = payload_filter
    resp = httpx.post(url, json=body, timeout=_TIMEOUT)
    if resp.status_code == 404:
        log.warning("Qdrant collection %s missing — returning empty result", collection)
        return []
    resp.raise_for_status()
    return resp.json().get("result", []) or []


def search_summaries(
    vector: list[float],
    payload_filter: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """Semantic search over the panoptic_summaries collection."""
    return _search(_COLLECTION, vector, payload_filter, top_k)


def search_image_captions(
    vector: list[float],
    payload_filter: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """Semantic search over the image_caption_vectors collection."""
    return _search(_IMAGE_CAPTION_COLLECTION, vector, payload_filter, top_k)
