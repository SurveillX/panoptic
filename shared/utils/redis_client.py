"""
Central VIL Redis client.

Connects ONLY to the central cloud Redis instance (REDIS_URL).
This is NOT the Jetson edge Redis — never use this client for edge operations.

All VIL job queue keys are namespaced under vil:*

Thread-safe: uses a connection pool shared across the process.
For multi-machine deployments, each machine gets its own pool pointed
at the same central Redis URL.
"""

from __future__ import annotations

import os

import redis

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Module-level pool — one pool per process, shared across threads.
_pool: redis.ConnectionPool | None = None


def get_redis_client() -> redis.Redis:
    """
    Return a Redis client backed by a shared connection pool.

    The pool is created once per process on first call.  All workers
    within the same process share it; each machine maintains its own pool
    pointing at the same central Redis URL.

    decode_responses=True: all keys and values are str, not bytes.
    """
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=50,
        )
    return redis.Redis(connection_pool=_pool)
