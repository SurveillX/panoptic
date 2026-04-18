"""
VL embedding client — thin HTTP wrapper around panoptic-retrieval's
/embed_visual endpoint (Qwen3-VL-Embedding-8B, dim 4096).

Contract (from docs/TRAILER_AUTH_HANDOFF.md section "Service summary"):

    POST /embed_visual
    {
      "items": [{"text": "...", "image": {"url": "..."} | {"data": "<base64>"}}],
      "normalize": true
    }
    → { "model": "qwen3-vl-embedding-8b", "dim": 4096,
        "embeddings": [[...]], "truncated_count": 0 }

Batch cap: 4 items per call. We always send a single item today;
batch support is a later optimization.

Configuration:
  RETRIEVAL_BASE_URL — default http://localhost:8700
  RETRIEVAL_TIMEOUT_SEC — default 180 (first call can take ~30s for compile)
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

RETRIEVAL_BASE_URL: str = os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")
RETRIEVAL_TIMEOUT_SEC: float = float(os.environ.get("RETRIEVAL_TIMEOUT_SEC", "180"))

VL_EMBEDDING_MODEL_ID: str = "qwen3-vl-embedding-8b"


class VLEmbeddingClient:
    """HTTP client for the panoptic-retrieval /embed_visual endpoint."""

    def __init__(
        self,
        base_url: str = RETRIEVAL_BASE_URL,
        timeout_sec: float = RETRIEVAL_TIMEOUT_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    def embed_image_bytes(
        self,
        image_bytes: bytes,
        *,
        text: str = "",
        normalize: bool = True,
    ) -> list[float]:
        """
        Embed a JPEG/PNG from raw bytes. Returns a single dense vector.

        `text` is an optional co-item — Qwen3-VL-Embedding-8B accepts a
        text+image pair for richer grounding. Leave empty for pure image
        embedding.
        """
        b64 = base64.b64encode(image_bytes).decode("ascii")
        item = {"text": text, "image": {"data": b64}}

        resp = httpx.post(
            f"{self._base_url}/embed_visual",
            json={"items": [item], "normalize": normalize},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"][0]

    def embed_image_path(
        self,
        path: str | Path,
        *,
        text: str = "",
        normalize: bool = True,
    ) -> list[float]:
        """Convenience — read bytes from disk and embed."""
        with open(path, "rb") as f:
            image_bytes = f.read()
        return self.embed_image_bytes(image_bytes, text=text, normalize=normalize)

    def embed_text(self, text: str, *, normalize: bool = True) -> list[float]:
        """
        Embed text only, no image, in the VL shared space. Used by the
        Search API to query `panoptic_image_vectors` with a text prompt
        (cross-modal retrieval — same 4096-dim space as the image-side
        vectors).
        """
        resp = httpx.post(
            f"{self._base_url}/embed_visual",
            json={"items": [{"text": text}], "normalize": normalize},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"][0]


def get_vl_embedding_client() -> VLEmbeddingClient:
    return VLEmbeddingClient()
