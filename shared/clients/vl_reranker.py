"""
VL reranker client — wraps panoptic-retrieval's /rerank_visual endpoint
(Qwen3-VL-Reranker-2B).

Unlike the text reranker at /rerank which scores `(query_text,
document_text)` pairs, /rerank_visual scores `(query_text,
{text, image})` pairs — i.e. the reranker actually looks at image
pixels. Useful when hits came through the VL retrieval branch.

Service contract (from panoptic-retrieval's own handoff doc):
    POST /rerank_visual
    {
      "query": "...",
      "documents": [{"text": "...", "image": {"url": ... | "data": ...}}],
      "top_n"?: int,
      "return_documents"?: bool
    }
    → { "model": "qwen3-vl-reranker-2b",
        "results": [ {"index": int, "score": float, "document"?: ...}, ...] }

Batch cap: 8 documents per call.

Cost: each call sends base64 JPEG payloads over HTTP. Keep batches
small; don't pass every image in the index per query.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

log = logging.getLogger(__name__)

RETRIEVAL_BASE_URL: str = os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")
RETRIEVAL_TIMEOUT_SEC: float = float(os.environ.get("RETRIEVAL_TIMEOUT_SEC", "180"))

VL_RERANKER_MODEL_ID: str = "qwen3-vl-reranker-2b"
MAX_BATCH_RERANK_VL: int = 8   # panoptic-retrieval hard cap


class VLRerankerClient:
    """HTTP client for the panoptic-retrieval /rerank_visual endpoint."""

    def __init__(
        self,
        base_url: str = RETRIEVAL_BASE_URL,
        timeout_sec: float = RETRIEVAL_TIMEOUT_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    def rerank_paths(
        self,
        query: str,
        items: list[dict],
        top_n: int | None = None,
        instruction: str | None = None,
    ) -> list[tuple[int, float]]:
        """
        Rerank a batch of items. Each item is a dict containing at least
        one of:
          - "storage_path": filesystem path to a JPEG (most common)
          - "image_bytes": raw JPEG bytes (in-memory)
          - "image_data":  pre-computed base64-encoded JPEG
        Optional: "text" — co-item text used as document text
        alongside the image.

        Missing image data on an item is fatal — the caller's job is to
        filter to items that have retrievable pixels. VL rerank without
        pixels has nothing to score against.

        Returns [(original_index, score), ...] sorted desc by score.
        Length of result equals top_n if given, else len(items).
        """
        if not items:
            return []
        if len(items) > MAX_BATCH_RERANK_VL:
            # Enforce the server cap client-side for a clearer error than
            # a 400 from the retrieval service.
            raise ValueError(
                f"rerank_paths: too many items ({len(items)}); "
                f"VL reranker caps at {MAX_BATCH_RERANK_VL}"
            )

        documents: list[dict] = []
        for it in items:
            text = it.get("text", "") or ""
            img_b64: str | None = it.get("image_data")
            if img_b64 is None:
                raw: bytes | None = it.get("image_bytes")
                if raw is None:
                    path = it.get("storage_path")
                    if not path or not os.path.exists(path):
                        raise ValueError(
                            "rerank_paths: item has no retrievable image "
                            f"(no image_data/image_bytes; storage_path={path!r} "
                            "missing or absent)"
                        )
                    with open(path, "rb") as f:
                        raw = f.read()
                img_b64 = base64.b64encode(raw).decode("ascii")
            documents.append({"text": text, "image": {"data": img_b64}})

        body: dict = {"query": query, "documents": documents}
        if top_n is not None:
            body["top_n"] = top_n
        if instruction is not None:
            body["instruction"] = instruction

        resp = httpx.post(
            f"{self._base_url}/rerank_visual",
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return [(int(r["index"]), float(r["score"])) for r in data.get("results", [])]


def get_vl_reranker_client() -> VLRerankerClient:
    return VLRerankerClient()
