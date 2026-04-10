"""
Embedding client — shared library.

Generates dense vector embeddings from text using a sentence-transformers model.
The underlying model is loaded lazily on first use (singleton per process).

Configuration:
  EMBEDDING_MODEL — model name (default: all-MiniLM-L6-v2)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("loading embedding model: %s", EMBEDDING_MODEL)
        _model = SentenceTransformer(EMBEDDING_MODEL)
        log.info("embedding model loaded")
    return _model


class EmbeddingClient:
    """Generates text embeddings using a sentence-transformers model."""

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model_name = model_name

    def embed(self, text: str) -> list[float]:
        """
        Encode text as a normalized dense vector.

        The model is loaded on first call (lazy singleton).
        Returns a list of floats.
        """
        model = _get_model()
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()


def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient(model_name=EMBEDDING_MODEL)
