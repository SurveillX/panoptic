"""
Reranker client — thin HTTP wrapper around the standalone retrieval service.

    client = get_reranker_client()
    ranked = client.rerank("query", ["doc a", "doc b", "doc c"], top_n=2)
    # -> [(1, 0.82), (2, 0.41)]    (original_index, score) sorted desc

Configuration (env vars):
  RETRIEVAL_BASE_URL     — default http://localhost:8700
  RETRIEVAL_TIMEOUT_SEC  — default 60
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

RETRIEVAL_BASE_URL: str = os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")
RETRIEVAL_TIMEOUT_SEC: float = float(os.environ.get("RETRIEVAL_TIMEOUT_SEC", "60"))


class RerankerClient:
    """HTTP client for the retrieval service /rerank endpoint."""

    def __init__(
        self,
        base_url: str = RETRIEVAL_BASE_URL,
        timeout_sec: float = RETRIEVAL_TIMEOUT_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        instruction: str | None = None,
    ) -> list[tuple[int, float]]:
        """
        Return (original_index, score) pairs ordered by score descending.

        Length of the returned list equals `top_n` when supplied, else
        `len(documents)`. Scores are probabilities in [0, 1].

        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        if not documents:
            return []
        body: dict = {"query": query, "documents": documents}
        if top_n is not None:
            body["top_n"] = top_n
        if instruction is not None:
            body["instruction"] = instruction
        resp = httpx.post(
            f"{self._base_url}/rerank",
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return [(int(r["index"]), float(r["score"])) for r in data.get("results", [])]


def get_reranker_client() -> RerankerClient:
    return RerankerClient(
        base_url=RETRIEVAL_BASE_URL,
        timeout_sec=RETRIEVAL_TIMEOUT_SEC,
    )
