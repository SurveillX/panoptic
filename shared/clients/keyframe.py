"""
Keyframe API client — shared library.

Fetches frames and thumbnails from the Jetson Keyframe API on behalf of the
Summary Agent.  Contains no HTTP server code; the Jetson-side implementation
is out of scope for this module.

Retry rules (design_spec §7.3):
  - Retry on network errors (httpx.TransportError, timeouts) only.
  - Two attempts total: attempt 1 immediately, attempt 2 after +2s.
  - No retry on 404 (no frame within tolerance) — return None.
  - No retry on 4xx/5xx other than network-level failures.

Quality filter (design_spec §7.4):
  A frame is usable if ALL of:
    blur       <= 0.7
    brightness >= 0.2
    occluded   == False

Timestamp serialization:
  target_ts is always converted to ISO-8601 UTC with 'Z' suffix before
  being placed in query params.  Implicit datetime serialization is not used.
  Example: 2026-04-07T10:00:00Z
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEYFRAME_BASE_URL: str = os.environ.get("KEYFRAME_BASE_URL", "http://localhost:8765")
KEYFRAME_TOKEN:    str = os.environ.get("KEYFRAME_TOKEN", "")
KEYFRAME_TIMEOUT:  float = float(os.environ.get("KEYFRAME_TIMEOUT_SEC", "10"))

_RETRY_DELAY_SECONDS: float = 2.0
_MAX_ATTEMPTS: int = 2


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

class FrameQuality(BaseModel):
    model_config = ConfigDict(strict=False)

    blur:       float   # 0–1; higher = blurrier
    brightness: float   # 0–1; lower = darker
    occluded:   bool


class FrameResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    uri:          str
    requested_ts: datetime
    actual_ts:    datetime
    exact_match:  bool
    quality:      FrameQuality


# ---------------------------------------------------------------------------
# Quality filter
# ---------------------------------------------------------------------------

def is_usable_frame(quality: FrameQuality) -> bool:
    """
    Return True if the frame meets minimum quality requirements.

    Reject if ANY of:
      blur > 0.7         (too blurry)
      brightness < 0.2   (too dark)
      occluded == True   (obstructed)

    Boundary values (0.7 blur, 0.2 brightness) are acceptable.
    """
    return (
        quality.blur <= 0.7
        and quality.brightness >= 0.2
        and not quality.occluded
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class KeyframeNetworkError(IOError):
    """Network error persisted after all retry attempts."""


class KeyframeAuthError(PermissionError):
    """401 or 403 response from the Keyframe API."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KeyframeClient:
    """
    HTTP client for the Jetson Keyframe API.

    Thread-safe: httpx.Client uses a connection pool internally.
    Create one instance per service process via get_keyframe_client().
    """

    def __init__(self, base_url: str, token: str, timeout_sec: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._headers = {"Authorization": f"Bearer {token}"}

    def fetch_frame(
        self,
        camera_id: str,
        target_ts: datetime,
        tolerance_sec: int = 5,
    ) -> FrameResponse | None:
        """
        Fetch the nearest full frame to target_ts within tolerance_sec.

        Returns None if no frame exists within the tolerance window (404).
        Raises KeyframeNetworkError if all retry attempts fail.
        Raises KeyframeAuthError on 401/403.
        """
        return self._fetch("/frame", camera_id, target_ts, tolerance_sec)

    def fetch_thumbnail(
        self,
        camera_id: str,
        target_ts: datetime,
        tolerance_sec: int = 5,
    ) -> FrameResponse | None:
        """
        Fetch the nearest thumbnail to target_ts within tolerance_sec.

        Same semantics as fetch_frame.
        """
        return self._fetch("/thumbnail", camera_id, target_ts, tolerance_sec)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch(
        self,
        path: str,
        camera_id: str,
        target_ts: datetime,
        tolerance_sec: int,
    ) -> FrameResponse | None:
        params = {
            "camera_id":    camera_id,
            "target_ts":    _to_utc_z(target_ts),
            "tolerance_sec": str(tolerance_sec),
        }
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = httpx.get(
                    url,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS:
                    log.warning(
                        "keyframe fetch network error attempt=%d/%d path=%s: %s",
                        attempt, _MAX_ATTEMPTS, path, exc,
                    )
                    time.sleep(_RETRY_DELAY_SECONDS)
                continue

            if response.status_code == 404:
                log.debug(
                    "keyframe miss path=%s camera_id=%s target_ts=%s",
                    path, camera_id, params["target_ts"],
                )
                return None

            if response.status_code in (401, 403):
                raise KeyframeAuthError(
                    f"Keyframe API auth failure: {response.status_code} {path}"
                )

            response.raise_for_status()

            frame = FrameResponse.model_validate(response.json(), strict=False)
            log.debug(
                "keyframe hit path=%s camera_id=%s actual_ts=%s exact=%s quality=%s",
                path, camera_id, frame.actual_ts.isoformat(),
                frame.exact_match, frame.quality,
            )
            return frame

        raise KeyframeNetworkError(
            f"Keyframe API unreachable after {_MAX_ATTEMPTS} attempts: "
            f"path={path} camera_id={camera_id} last_error={last_exc}"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_keyframe_client() -> KeyframeClient:
    """
    Return a KeyframeClient configured from environment variables.

      KEYFRAME_BASE_URL    — base URL of the Jetson Keyframe API
                             default: http://localhost:8765
      KEYFRAME_TOKEN       — bearer token for authentication
      KEYFRAME_TIMEOUT_SEC — per-request timeout in seconds
                             default: 10
    """
    return KeyframeClient(
        base_url=KEYFRAME_BASE_URL,
        token=KEYFRAME_TOKEN,
        timeout_sec=KEYFRAME_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_utc_z(dt: datetime) -> str:
    """
    Serialize a datetime to ISO-8601 UTC with 'Z' suffix.

    Always converts to UTC first so timezone-aware datetimes from any zone
    are normalized.  Never relies on implicit datetime serialization.

    Example: 2026-04-07T10:00:00Z
    """
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
