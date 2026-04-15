"""
Embedding client — thin HTTP wrapper around the standalone retrieval service.

Public interface is preserved from the previous sentence-transformers version:

    client = get_embedding_client()
    vector = client.embed(text)             # list[float]
    vectors = client.embed_batch(texts)     # list[list[float]]  (new)

Configuration (env vars):
  RETRIEVAL_BASE_URL     — default http://localhost:8700
  RETRIEVAL_TIMEOUT_SEC  — default 60
  EMBEDDING_MODEL        — informational (logged); the model that actually
                           runs is whichever the retrieval service loaded.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

RETRIEVAL_BASE_URL: str = os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")
RETRIEVAL_TIMEOUT_SEC: float = float(os.environ.get("RETRIEVAL_TIMEOUT_SEC", "60"))

# Kept for log lines and DB audit fields. Not used for model loading.
EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding-8b")


class EmbeddingClient:
    """HTTP client for the retrieval service /embed endpoint."""

    def __init__(
        self,
        base_url: str = RETRIEVAL_BASE_URL,
        timeout_sec: float = RETRIEVAL_TIMEOUT_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    def embed(self, text: str) -> list[float]:
        """Embed a single text. Returns the dense vector."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str], normalize: bool = True) -> list[list[float]]:
        """
        Embed a batch of texts. Returns vectors in the same order.

        Raises httpx.HTTPStatusError on non-2xx responses. Callers with worker
        lease/retry semantics (embedding_worker, caption_embed_worker) rely
        on exceptions to trigger retry_wait — do not swallow them here.
        """
        if not texts:
            return []
        url = f"{self._base_url}/embed"
        resp = httpx.post(
            url,
            json={"texts": texts, "normalize": normalize},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"]


def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient(
        base_url=RETRIEVAL_BASE_URL,
        timeout_sec=RETRIEVAL_TIMEOUT_SEC,
    )
