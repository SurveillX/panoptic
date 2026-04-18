"""
HMAC-SHA256 signed request auth for Panoptic trailer ingest.

See docs/AUTH_DESIGN.md for the full spec. Highlights:

  * Canonical signing string:
      <serial>|<timestamp>|<method>|<path>|<body_sha256>
  * Dual-secret rotation via PANOPTIC_SHARED_SECRET_{ACTIVE,PREVIOUS}.
  * Replay cache in Redis keyed by (serial, ts, sig[:16]) TTL 600s.
  * Registry lookup in panoptic_trailers (is_active=true).

Dev-mode disable hatch requires BOTH env vars:
    PANOPTIC_DEV_MODE=true
    PANOPTIC_AUTH_ENABLED=false

Any other combo → auth ON.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable

import redis as redis_module
import sqlalchemy as sa

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEV_MODE = os.environ.get("PANOPTIC_DEV_MODE", "false").lower() == "true"
_AUTH_OFF = os.environ.get("PANOPTIC_AUTH_ENABLED", "true").lower() == "false"
AUTH_ENABLED: bool = not (_DEV_MODE and _AUTH_OFF)

MAX_SKEW_SEC = int(os.environ.get("PANOPTIC_AUTH_MAX_SKEW_SEC", "300"))
REPLAY_TTL_SEC = int(os.environ.get("PANOPTIC_AUTH_REPLAY_TTL_SEC", "600"))
REGISTRY_REFRESH_SEC = int(os.environ.get("PANOPTIC_AUTH_REGISTRY_REFRESH_SEC", "30"))

SECRET_ACTIVE = os.environ.get("PANOPTIC_SHARED_SECRET_ACTIVE", "")
SECRET_PREVIOUS = os.environ.get("PANOPTIC_SHARED_SECRET_PREVIOUS", "")


HEADER_SERIAL = "x-panoptic-serial"
HEADER_TIMESTAMP = "x-panoptic-timestamp"
HEADER_SIGNATURE = "x-panoptic-signature"


# ---------------------------------------------------------------------------
# Failure categories (for structured logging + HTTP mapping)
# ---------------------------------------------------------------------------


@dataclass
class AuthFailure:
    category: str
    serial: str | None
    http_status: int
    log_fields: dict

    @property
    def body(self) -> dict:
        if self.http_status == 403:
            return {"error": "invalid_trailer"}
        return {"error": "invalid_auth"}


def _fail(category: str, status: int, serial: str | None = None, **fields) -> AuthFailure:
    return AuthFailure(category=category, serial=serial, http_status=status, log_fields=fields)


# ---------------------------------------------------------------------------
# Signing (shared by client + server)
# ---------------------------------------------------------------------------


def canonical_signing_string(
    serial: str, timestamp: str, method: str, path: str, body: bytes
) -> str:
    body_sha256 = hashlib.sha256(body).hexdigest()
    return f"{serial}|{timestamp}|{method.upper()}|{path}|{body_sha256}"


def compute_signature(secret: str, signing_string: str) -> str:
    return hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()


def sign_headers(
    secret: str, serial: str, method: str, path: str, body: bytes, timestamp: int | None = None
) -> dict[str, str]:
    """Build auth headers for a client push."""
    ts = str(int(timestamp if timestamp is not None else time.time()))
    ss = canonical_signing_string(serial, ts, method, path, body)
    sig = compute_signature(secret, ss)
    return {
        "X-Panoptic-Serial": serial,
        "X-Panoptic-Timestamp": ts,
        "X-Panoptic-Signature": sig,
    }


# ---------------------------------------------------------------------------
# Server-side registry cache
# ---------------------------------------------------------------------------


class TrailerRegistry:
    """In-memory cache of active trailer serials, refreshed periodically."""

    def __init__(self, database_url: str, refresh_sec: int = REGISTRY_REFRESH_SEC) -> None:
        self._engine = sa.create_engine(database_url, pool_pre_ping=True)
        self._refresh_sec = refresh_sec
        self._last_refresh = 0.0
        self._active: set[str] = set()

    def is_active(self, serial: str) -> bool:
        self._maybe_refresh()
        if serial in self._active:
            return True
        # Miss: a recently-registered serial may not be in the cache yet.
        # Force a refresh and retry once. Cheap (one SELECT) and avoids
        # the usual 30s window after registering a new trailer.
        try:
            self.force_refresh()
        except Exception as exc:
            log.warning("trailer registry force-refresh on miss failed: %s", exc)
            return False
        return serial in self._active

    def force_refresh(self) -> None:
        with self._engine.connect() as c:
            rows = c.execute(
                sa.text("SELECT serial_number FROM panoptic_trailers WHERE is_active = true")
            ).all()
        self._active = {r[0] for r in rows}
        self._last_refresh = time.monotonic()
        log.debug("trailer registry refreshed: %d active", len(self._active))

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_sec:
            try:
                self.force_refresh()
            except Exception as exc:
                log.warning("trailer registry refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Replay cache
# ---------------------------------------------------------------------------


class ReplayCache:
    """Redis SETNX-based seen-tuple cache."""

    def __init__(self, redis_url: str, ttl_sec: int = REPLAY_TTL_SEC) -> None:
        self._r = redis_module.Redis.from_url(redis_url)
        self._ttl = ttl_sec

    def observe(self, serial: str, ts: str, sig: str) -> bool:
        """Mark (serial, ts, sig[:16]) as seen. Returns True if new, False if replayed."""
        key = f"panoptic:replay:{serial}:{ts}:{sig[:16]}"
        return bool(self._r.set(key, "1", nx=True, ex=self._ttl))


# ---------------------------------------------------------------------------
# Full verification
# ---------------------------------------------------------------------------


def verify_request(
    *,
    method: str,
    path: str,
    headers: dict,  # keys lowercased
    body: bytes,
    registry: TrailerRegistry,
    replay: ReplayCache,
    now_epoch: int | None = None,
) -> AuthFailure | None:
    """
    Run the 8 verification steps. Returns None on success, AuthFailure on rejection.
    """
    if not AUTH_ENABLED:
        return None  # dev-mode hatch

    # 1. Headers present
    serial = headers.get(HEADER_SERIAL)
    ts_header = headers.get(HEADER_TIMESTAMP)
    sig_header = headers.get(HEADER_SIGNATURE)
    if not serial or not ts_header or not sig_header:
        return _fail(
            "missing_header",
            401,
            serial=serial,
            have_serial=bool(serial),
            have_ts=bool(ts_header),
            have_sig=bool(sig_header),
        )

    # 2. Formats
    if not isinstance(serial, str) or not serial.strip():
        return _fail("bad_format", 401, serial=serial, field="serial")
    try:
        ts_int = int(ts_header)
    except (TypeError, ValueError):
        return _fail("bad_format", 401, serial=serial, field="timestamp", value=ts_header)
    if len(sig_header) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sig_header):
        return _fail("bad_format", 401, serial=serial, field="signature")

    # 3. Time window
    now = now_epoch if now_epoch is not None else int(time.time())
    if abs(now - ts_int) > MAX_SKEW_SEC:
        return _fail(
            "stale_timestamp",
            401,
            serial=serial,
            ts=ts_int,
            now=now,
            skew=now - ts_int,
        )

    # 4. Replay cache
    try:
        is_new = replay.observe(serial, ts_header, sig_header)
    except Exception as exc:
        # fail closed
        return _fail("replay_cache_unavailable", 503, serial=serial, reason=str(exc)[:80])
    if not is_new:
        return _fail("replayed", 401, serial=serial, ts=ts_int, sig_prefix=sig_header[:16])

    # 5. Known-trailer registry
    if not registry.is_active(serial):
        return _fail("unknown_serial", 403, serial=serial)

    # 6-7. Recompute signature(s) with constant-time compare
    signing_string = canonical_signing_string(serial, ts_header, method, path, body)

    if SECRET_ACTIVE:
        expected = compute_signature(SECRET_ACTIVE, signing_string)
        if hmac.compare_digest(sig_header, expected):
            return None

    # 8. Fallback to previous secret
    if SECRET_PREVIOUS:
        expected_prev = compute_signature(SECRET_PREVIOUS, signing_string)
        if hmac.compare_digest(sig_header, expected_prev):
            return None

    return _fail("bad_signature", 401, serial=serial, sig_prefix=sig_header[:16])


# ---------------------------------------------------------------------------
# Dev-mode loud warning loop (imported + started by webhook server if needed)
# ---------------------------------------------------------------------------


def start_dev_warning_loop(interval_sec: int = 60) -> None:
    """Spawn a daemon thread that logs a WARNING every `interval_sec` while auth is disabled."""
    import threading

    if AUTH_ENABLED:
        return

    def _loop() -> None:
        while True:
            log.warning(
                "panoptic trailer auth is DISABLED (dev mode). Any client can push."
            )
            time.sleep(interval_sec)

    log.warning(
        "panoptic trailer auth is DISABLED (dev mode). Any client can push."
    )
    t = threading.Thread(target=_loop, name="auth-dev-warn", daemon=True)
    t.start()


def assert_config_sane() -> None:
    """Fail fast on bad env config (except dev-disable, which is loud-warned)."""
    if not AUTH_ENABLED:
        return  # dev mode — intentional, no secrets required
    if not SECRET_ACTIVE:
        raise RuntimeError(
            "PANOPTIC_SHARED_SECRET_ACTIVE is unset and auth is enabled. "
            "Set the env var or disable auth via PANOPTIC_DEV_MODE=true + PANOPTIC_AUTH_ENABLED=false."
        )
