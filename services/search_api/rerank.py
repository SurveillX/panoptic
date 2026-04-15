"""
Rerank seam for the Search API.

Calls the standalone retrieval service's /rerank endpoint to reorder Qdrant
hits by cross-encoder score. Any failure (service down, bad response) is
logged and the hits are returned in their original order — rerank is an
enrichment, not a hard dependency for search.

Signature stays compatible with the no-op stub that preceded this:
    rerank(query, hits) -> hits

An optional `text_field` parameter tells the seam which key in each hit's
Qdrant payload carries the text to rerank against (summary → "summary",
image/event → "caption_text").
"""

from __future__ import annotations

import logging

from shared.clients.reranker import RerankerClient, get_reranker_client

log = logging.getLogger(__name__)

_client: RerankerClient | None = None


def _get_client() -> RerankerClient:
    global _client
    if _client is None:
        _client = get_reranker_client()
    return _client


def rerank(
    query: str | None,
    hits: list[dict],
    text_field: str = "summary",
    top_n: int | None = None,
) -> list[dict]:
    """
    Reorder `hits` by cross-encoder relevance to `query`.

    Fast path: returns hits unchanged when the query is empty or no hits.
    Error path: logs a warning and returns hits unchanged. Never raises.
    """
    if not query or not hits:
        return hits

    documents: list[str] = []
    for h in hits:
        payload = h.get("payload") or {}
        documents.append(payload.get(text_field) or "")

    try:
        pairs = _get_client().rerank(query, documents, top_n=top_n)
    except Exception as exc:
        log.warning("rerank: falling back to original order — %s", exc)
        return hits

    reordered: list[dict] = []
    for idx, score in pairs:
        if idx < 0 or idx >= len(hits):
            continue
        hit = dict(hits[idx])
        hit["rerank_score"] = float(score)
        reordered.append(hit)
    return reordered
