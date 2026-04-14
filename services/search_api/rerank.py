"""
Rerank seam for the Search API.

v1 implementation is a no-op. Structure exists so the reranker (Qwen3-Reranker-0.6B
via TEI) can be wired in post-Spark migration as a single-file change.

Expected post-Spark signature stays the same: rerank(query, hits) -> hits. The
stub returns hits unchanged and preserves ordering.
"""

from __future__ import annotations


def rerank(query: str | None, hits: list[dict]) -> list[dict]:
    """No-op rerank. Returns hits in the order received."""
    return hits
