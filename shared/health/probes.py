"""
Background dependency probes.

Each probe is a callable that returns a DepStatus. PROBE_REGISTRY maps
a name (e.g. "postgres", "redis", "qdrant", "vllm", "retrieval") to the
probe function, so workers can enable the set they care about.

start_probe_loop() runs every HEALTH_PROBE_INTERVAL_SEC (default 30s).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import httpx
import redis as redis_module
import sqlalchemy as sa

from shared.health.state import DepStatus, HealthState

log = logging.getLogger(__name__)

PROBE_INTERVAL_SEC = int(os.environ.get("HEALTH_PROBE_INTERVAL_SEC", "30"))
PROBE_TIMEOUT_SEC = float(os.environ.get("HEALTH_PROBE_TIMEOUT_SEC", "5"))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timed(fn: Callable[[], None]) -> tuple[bool, int | None, str | None]:
    t0 = time.perf_counter()
    try:
        fn()
        ms = int((time.perf_counter() - t0) * 1000)
        return True, ms, None
    except Exception as exc:
        return False, None, str(exc)[:120]


# ---------------------------------------------------------------------------
# Individual probes — each takes kwargs from `targets` dict
# ---------------------------------------------------------------------------


def _probe_postgres(*, database_url: str) -> DepStatus:
    """
    One-shot Postgres liveness probe. Uses psycopg2 directly (no SQLAlchemy
    engine / pool) so each probe call opens and closes exactly one TCP
    connection. Prior implementation created a new SQLAlchemy engine per
    probe — the pools leaked and exhausted Postgres's max_connections
    after ~10 hours of 30s probing across 8 workers.
    """
    import psycopg2

    def _do():
        conn = psycopg2.connect(database_url, connect_timeout=int(PROBE_TIMEOUT_SEC))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    ok, ms, reason = _timed(_do)
    return DepStatus(ok=ok, latency_ms=ms, reason=reason, checked_at=_utcnow_iso())


def _probe_redis(*, redis_url: str) -> DepStatus:
    """
    One-shot Redis liveness probe. Explicitly closes the client + its
    connection pool after every probe so we don't leak pools the way the
    old Postgres probe did.
    """
    def _do():
        r = redis_module.Redis.from_url(redis_url, socket_connect_timeout=PROBE_TIMEOUT_SEC, socket_timeout=PROBE_TIMEOUT_SEC)
        try:
            r.ping()
        finally:
            try:
                r.close()
                r.connection_pool.disconnect()
            except Exception:
                pass
    ok, ms, reason = _timed(_do)
    return DepStatus(ok=ok, latency_ms=ms, reason=reason, checked_at=_utcnow_iso())


def _probe_qdrant(*, qdrant_url: str) -> DepStatus:
    def _do():
        resp = httpx.get(f"{qdrant_url}/readyz", timeout=PROBE_TIMEOUT_SEC)
        if resp.status_code != 200:
            raise RuntimeError(f"qdrant /readyz {resp.status_code}")
    ok, ms, reason = _timed(_do)
    return DepStatus(ok=ok, latency_ms=ms, reason=reason, checked_at=_utcnow_iso())


def _probe_vllm(*, vllm_url: str) -> DepStatus:
    def _do():
        resp = httpx.get(f"{vllm_url}/v1/models", timeout=PROBE_TIMEOUT_SEC)
        if resp.status_code != 200:
            raise RuntimeError(f"vllm /v1/models {resp.status_code}")
    ok, ms, reason = _timed(_do)
    return DepStatus(ok=ok, latency_ms=ms, reason=reason, checked_at=_utcnow_iso())


def _probe_retrieval(*, retrieval_url: str) -> DepStatus:
    def _do():
        resp = httpx.get(f"{retrieval_url}/health", timeout=PROBE_TIMEOUT_SEC)
        if resp.status_code != 200:
            raise RuntimeError(f"retrieval /health {resp.status_code}")
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"retrieval not ready: {data}")
    ok, ms, reason = _timed(_do)
    return DepStatus(ok=ok, latency_ms=ms, reason=reason, checked_at=_utcnow_iso())


PROBE_REGISTRY: dict[str, Callable[..., DepStatus]] = {
    "postgres": _probe_postgres,
    "redis": _probe_redis,
    "qdrant": _probe_qdrant,
    "vllm": _probe_vllm,
    "retrieval": _probe_retrieval,
}


# ---------------------------------------------------------------------------
# Consumer-lag probe (Redis streams specific)
# ---------------------------------------------------------------------------


def _probe_consumer_stats(*, redis_url: str, stream: str, group: str) -> tuple[int, int]:
    """
    Return (pending_pel, xlen) for the given stream/group.

    Per-call Redis client with explicit close() + pool disconnect in a
    finally — same rationale as _probe_redis. Earlier version leaked a
    pool per probe.
    """
    r = redis_module.Redis.from_url(redis_url, decode_responses=True)
    try:
        xlen = r.xlen(stream)
        try:
            pending = r.xpending(stream, group)
            # redis-py returns dict or a summary tuple depending on version
            if isinstance(pending, dict):
                pel = int(pending.get("pending", 0))
            else:
                pel = int(pending[0]) if pending else 0
        except Exception:
            pel = 0
        return pel, xlen
    finally:
        try:
            r.close()
            r.connection_pool.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Probe loop
# ---------------------------------------------------------------------------


def start_probe_loop(
    state: HealthState,
    *,
    targets: dict[str, dict],
    consumer_probe: tuple[str, str] | None = None,
    interval_sec: int = PROBE_INTERVAL_SEC,
) -> threading.Thread:
    """
    Spawn a daemon thread that updates `state` every `interval_sec`.

    targets: {"postgres": {"database_url": "..."}, "redis": {"redis_url": "..."}, ...}
    consumer_probe: optional (stream, group) to report xlen/PEL for.

    Runs one pass immediately, then sleeps.
    """
    def _loop() -> None:
        redis_url = targets.get("redis", {}).get("redis_url")
        while True:
            # Dep probes
            for name, kwargs in targets.items():
                probe = PROBE_REGISTRY.get(name)
                if probe is None:
                    continue
                try:
                    status = probe(**kwargs)
                except Exception as exc:
                    status = DepStatus(ok=False, reason=str(exc)[:120], checked_at=_utcnow_iso())
                state.set_dep(name, status)

            # Consumer stats (if configured + Redis is up)
            if consumer_probe is not None and redis_url:
                stream, group = consumer_probe
                try:
                    pel, xlen = _probe_consumer_stats(redis_url=redis_url, stream=stream, group=group)
                    state.set_consumer_stats(pending_pel=pel, xlen=xlen, lag_sec=None)
                except Exception as exc:
                    log.debug("consumer stats probe failed: %s", exc)

            time.sleep(interval_sec)

    thread = threading.Thread(target=_loop, name="health-probe", daemon=True)
    thread.start()
    log.info("health probe loop started interval=%ds deps=%s", interval_sec, list(targets.keys()))
    return thread
