"""
Warm up the retrieval-service code paths used by /v1/search so the first
real user query doesn't eat the ~100s torch.compile cost on the reranker.

Runs in a background daemon thread at search_api startup. Non-blocking:
if the retrieval service isn't up yet, we log and move on — the first
real request will pay the compile cost, which is the behavior without
this module. Adding the warmup is always an improvement, never a
regression.

Three probes, in order:
  1. POST /embed                     — warms the embedding path
  2. POST /rerank (small batch)      — forces rerank torch.compile
  3. POST /rerank (top_n)            — covers the rank-and-cut variant
"""

from __future__ import annotations

import logging
import os
import threading
import time

from shared.clients.embedding import get_embedding_client
from shared.clients.reranker import get_reranker_client

log = logging.getLogger(__name__)


def _warmup_sync() -> None:
    t0 = time.perf_counter()

    emb = get_embedding_client()
    rr = get_reranker_client()

    try:
        _ = emb.embed("warmup: person near parked vehicle")
        log.info("warmup: /embed ok (%.0fms)", (time.perf_counter() - t0) * 1000)
    except Exception as exc:
        log.warning("warmup: /embed failed, skipping: %s", exc)
        return  # if embed is down, rerank probably is too

    t1 = time.perf_counter()
    try:
        _ = rr.rerank(
            "warmup query",
            [
                "a red car in a parking lot",
                "a person walking on a sidewalk",
                "an empty warehouse aisle",
            ],
        )
        log.info("warmup: /rerank ok (%.0fms)", (time.perf_counter() - t1) * 1000)
    except Exception as exc:
        log.warning("warmup: /rerank failed (first-call compile may still happen): %s", exc)
        return

    t2 = time.perf_counter()
    try:
        _ = rr.rerank(
            "warmup query top_n",
            [
                "a red car in a parking lot",
                "a person walking on a sidewalk",
                "an empty warehouse aisle",
                "a forklift moving pallets",
            ],
            top_n=2,
        )
        log.info("warmup: /rerank top_n ok (%.0fms)", (time.perf_counter() - t2) * 1000)
    except Exception as exc:
        log.warning("warmup: /rerank top_n failed: %s", exc)

    log.info("warmup: done in %.0fms", (time.perf_counter() - t0) * 1000)


def start_warmup(delay_sec: float = 2.0) -> threading.Thread:
    """
    Spawn a daemon thread that waits `delay_sec` (so the HTTP server has
    finished binding) then pings the retrieval service. Never raises.
    Returns the thread so tests can join it.
    """
    if os.environ.get("SEARCH_API_WARMUP_DISABLED", "").lower() == "true":
        log.info("warmup: disabled via SEARCH_API_WARMUP_DISABLED=true")
        dummy = threading.Thread(target=lambda: None)
        dummy.start()
        return dummy

    def _run() -> None:
        try:
            time.sleep(delay_sec)
            _warmup_sync()
        except Exception:
            log.exception("warmup: unexpected error")

    t = threading.Thread(target=_run, name="search-api-warmup", daemon=True)
    t.start()
    return t
