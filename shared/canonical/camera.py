"""
Canonical camera-ID resolution (plan D-2, Option B — inert deploy).

The trailer fleet currently emits different raw camera_id strings across
bucket vs image payloads for the same physical camera on at least one
trailer (1422725077375). Without a collapsing step, image-trigger events
and bucket-marker events for that camera land with different scope_ids
and camera-scoped queries look broken.

This module provides a tiny lookup: given (serial_number, raw_camera_id,
payload_type), return the canonical camera_id. If no alias row exists,
the raw value is returned unchanged — the deploy is inert until someone
inserts an alias.

Table: panoptic_camera_aliases (migration 009)
Write pattern: operators insert rows manually when a mismatch is found.
No automated population in v1.

Thread-safety: the cache is a plain dict; readers are workers that each
own their own process, so no lock is needed. A manual cache bust
(reload_aliases) is provided for tests.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


PayloadType = Literal["bucket", "image"]

# In-process cache: (serial_number, raw_camera_id, payload_type) -> canonical
_CACHE: dict[tuple[str, str, str], str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED_AT: float = 0.0
_CACHE_TTL_SEC: float = 300.0  # 5 min refresh window; cheap table


def _load_cache(engine: Engine) -> None:
    """Load the full alias table into the process cache."""
    global _CACHE, _CACHE_LOADED_AT
    new_cache: dict[tuple[str, str, str], str] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT serial_number, raw_camera_id, payload_type, "
                "       canonical_camera_id "
                "  FROM panoptic_camera_aliases"
            )
        ).fetchall()
    for row in rows:
        new_cache[(row.serial_number, row.raw_camera_id, row.payload_type)] = (
            row.canonical_camera_id
        )
    with _CACHE_LOCK:
        _CACHE = new_cache
        _CACHE_LOADED_AT = time.time()
    log.info("canonical camera aliases loaded: %d rows", len(new_cache))


def resolve_canonical_camera_id(
    engine: Engine,
    *,
    serial_number: str,
    raw_camera_id: str,
    payload_type: PayloadType,
) -> str:
    """
    Return the canonical camera_id for a (serial_number, raw_camera_id,
    payload_type) tuple, falling back to raw_camera_id when no alias
    exists.

    Cache is refreshed lazily on TTL expiry. This function never fails open
    on DB errors — if the cache load raises, the caller still gets the raw
    value back (safe default: behave as today).
    """
    now = time.time()
    if now - _CACHE_LOADED_AT > _CACHE_TTL_SEC:
        try:
            _load_cache(engine)
        except Exception as exc:
            log.warning(
                "canonical camera cache reload failed — using raw values: %s",
                exc,
            )

    return _CACHE.get((serial_number, raw_camera_id, payload_type), raw_camera_id)


def reload_aliases(engine: Engine) -> None:
    """Force a cache refresh. Primarily for tests and operator tools."""
    _load_cache(engine)
