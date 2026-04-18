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
import os

from shared.clients.reranker import RerankerClient, get_reranker_client
from shared.clients.vl_reranker import MAX_BATCH_RERANK_VL, VLRerankerClient, get_vl_reranker_client

log = logging.getLogger(__name__)

_client: RerankerClient | None = None
_vl_client: VLRerankerClient | None = None

# Rerank strategy for image/event branches:
#   "text" (default) — pass caption_text through the text reranker (current)
#   "vl"   — pass (query, storage_path) through the VL reranker
# Only "text" and "vl" are supported; summaries always use text.
SEARCH_RERANK_MODE = os.environ.get("SEARCH_RERANK_MODE", "text").lower()


def _get_client() -> RerankerClient:
    global _client
    if _client is None:
        _client = get_reranker_client()
    return _client


def _get_vl_client() -> VLRerankerClient:
    global _vl_client
    if _vl_client is None:
        _vl_client = get_vl_reranker_client()
    return _vl_client


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


def rerank_images(
    query: str | None,
    hits: list[dict],
    top_n: int | None = None,
) -> list[dict]:
    """
    Image/event-specific rerank. Dispatches on SEARCH_RERANK_MODE:

      text: same as rerank(..., text_field='caption_text')
      vl:   pass (query, image on disk) through Qwen3-VL-Reranker-2B;
            the reranker actually looks at pixels, not just caption text.

    VL mode caps at MAX_BATCH_RERANK_VL (8 items per retrieval-service
    call). Hits beyond that are kept in their pre-rerank order after the
    reranked batch. Hits missing a usable `storage_path` are rescored via
    the text reranker fallback to avoid dropping them.

    Always returns the same number of hits as it received. Never raises.
    """
    if not query or not hits:
        return hits

    if SEARCH_RERANK_MODE != "vl":
        return rerank(query, hits, text_field="caption_text", top_n=top_n)

    # VL path: split hits by whether they have a readable storage_path
    with_img: list[tuple[int, dict]] = []
    fallback_text: list[tuple[int, dict]] = []
    for i, h in enumerate(hits):
        payload = h.get("payload") or {}
        path = payload.get("storage_path")
        if path and os.path.exists(path):
            with_img.append((i, h))
        else:
            fallback_text.append((i, h))

    vl_head = with_img[:MAX_BATCH_RERANK_VL]
    vl_tail = with_img[MAX_BATCH_RERANK_VL:]

    reranked: list[dict] = []

    if vl_head:
        items = [
            {
                "storage_path": (h[1].get("payload") or {}).get("storage_path"),
                "text": (h[1].get("payload") or {}).get("caption_text") or "",
            }
            for h in vl_head
        ]
        try:
            pairs = _get_vl_client().rerank_paths(query, items, top_n=len(items))
            for idx, score in pairs:
                if 0 <= idx < len(vl_head):
                    h = dict(vl_head[idx][1])
                    h["rerank_score"] = float(score)
                    h["rerank_model"] = "vl"
                    reranked.append(h)
        except Exception as exc:
            log.warning(
                "rerank_images: VL rerank failed (%d items), falling back to text for this batch: %s",
                len(vl_head), exc,
            )
            # fall through to text-rerank for this batch
            docs = [(h[1].get("payload") or {}).get("caption_text") or "" for h in vl_head]
            try:
                pairs = _get_client().rerank(query, docs, top_n=len(docs))
                for idx, score in pairs:
                    if 0 <= idx < len(vl_head):
                        h = dict(vl_head[idx][1])
                        h["rerank_score"] = float(score)
                        h["rerank_model"] = "text-fallback"
                        reranked.append(h)
            except Exception:
                # last-resort — preserve original order
                for _, h in vl_head:
                    reranked.append(dict(h))

    # Overflow VL-eligible items that didn't fit in batch: append unchanged
    for _, h in vl_tail:
        reranked.append(dict(h))

    # Items without image: rescore via text reranker so they still participate
    if fallback_text:
        docs = [(h[1].get("payload") or {}).get("caption_text") or "" for h in fallback_text]
        try:
            pairs = _get_client().rerank(query, docs, top_n=len(docs))
            ordered = [fallback_text[idx][1] for idx, _ in pairs if 0 <= idx < len(fallback_text)]
            reranked.extend(dict(h) for h in ordered)
        except Exception:
            reranked.extend(dict(h) for _, h in fallback_text)

    # Apply top_n cap at the end (preserves the VL-first ordering)
    if top_n is not None:
        reranked = reranked[:top_n]
    return reranked
