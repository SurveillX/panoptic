"""
Continuum frame client — fetches JPEG frames from trailer edge devices.

Trailers expose:
  GET https://{serial_number}.trailers.surveillx.ai/continuum/v1/recordings/{head_id}/frame?t={epoch_ms}

Returns raw JPEG bytes.  Panoptic must download locally and pass to vLLM as
base64 data URIs since vLLM cannot reach the trailer directly (edge NAT).

Retry policy: 2 attempts with 2s delay (mirrors KeyframeClient).
Quality filtering: skipped for v1 (Continuum provides no quality metadata).

Configuration:
  CONTINUUM_BASE_URL_TEMPLATE — URL template with {serial_number} placeholder
                                default: https://{serial_number}.trailers.surveillx.ai
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

CONTINUUM_BASE_URL_TEMPLATE: str = os.environ.get(
    "CONTINUUM_BASE_URL_TEMPLATE",
    "https://{serial_number}.trailers.surveillx.ai",
)
CONTINUUM_TIMEOUT_SEC: float = float(os.environ.get("CONTINUUM_TIMEOUT_SEC", "15"))

_RETRY_DELAY_SECONDS: float = 2.0
_MAX_ATTEMPTS: int = 2
_MAX_FRAME_BYTES: int = 5 * 1024 * 1024  # 5MB


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContinuumFrameResponse:
    """A frame fetched from the Continuum endpoint."""
    jpeg_bytes: bytes
    data_uri: str          # "data:image/jpeg;base64,{b64}"
    requested_ts: datetime


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ContinuumNetworkError(IOError):
    """Network error persisted after all retry attempts."""


class ContinuumAuthError(PermissionError):
    """401 or 403 response from the Continuum endpoint."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ContinuumClient:
    """
    HTTP client for the trailer Continuum frame endpoint.

    Fetches raw JPEG bytes and returns them with a precomputed base64 data URI
    suitable for passing to vLLM's OpenAI-compatible multimodal API.
    """

    def __init__(
        self,
        base_url_template: str = CONTINUUM_BASE_URL_TEMPLATE,
        timeout_sec: float = CONTINUUM_TIMEOUT_SEC,
    ) -> None:
        self._base_url_template = base_url_template
        self._timeout = timeout_sec

    def fetch_frame(
        self,
        serial_number: str,
        head_id: str,
        target_ts: datetime,
        *,
        width: int = 640,
        quality: int = 85,
        accurate: bool = True,
    ) -> ContinuumFrameResponse | None:
        """
        Fetch a JPEG frame from the trailer's Continuum endpoint.

        Returns ContinuumFrameResponse with JPEG bytes and base64 data URI.
        Returns None on 404 (no recording at that timestamp).
        Raises ContinuumNetworkError after exhausted retries.
        Raises ContinuumAuthError on 401/403.
        """
        base_url = self._base_url_template.format(serial_number=serial_number)
        epoch_ms = int(target_ts.timestamp() * 1000)

        url = f"{base_url}/continuum/v1/recordings/{head_id}/frame"
        params = {
            "t": str(epoch_ms),
            "width": str(width),
            "quality": str(quality),
        }
        if accurate:
            params["accurate"] = "true"

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = httpx.get(
                    url,
                    params=params,
                    timeout=self._timeout,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS:
                    log.warning(
                        "continuum fetch network error attempt=%d/%d: %s",
                        attempt, _MAX_ATTEMPTS, exc,
                    )
                    time.sleep(_RETRY_DELAY_SECONDS)
                continue

            if response.status_code == 404:
                log.debug(
                    "continuum miss head_id=%s t=%d", head_id, epoch_ms,
                )
                return None

            if response.status_code in (401, 403):
                raise ContinuumAuthError(
                    f"Continuum auth failure: {response.status_code} {url}"
                )

            response.raise_for_status()

            jpeg_bytes = response.content
            if len(jpeg_bytes) > _MAX_FRAME_BYTES:
                log.warning(
                    "continuum frame too large (%d bytes) — skipping",
                    len(jpeg_bytes),
                )
                return None

            b64 = base64.b64encode(jpeg_bytes).decode()
            data_uri = f"data:image/jpeg;base64,{b64}"

            log.debug(
                "continuum hit head_id=%s t=%d size=%d",
                head_id, epoch_ms, len(jpeg_bytes),
            )
            return ContinuumFrameResponse(
                jpeg_bytes=jpeg_bytes,
                data_uri=data_uri,
                requested_ts=target_ts,
            )

        raise ContinuumNetworkError(
            f"Continuum unreachable after {_MAX_ATTEMPTS} attempts: "
            f"head_id={head_id} last_error={last_exc}"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_continuum_client() -> ContinuumClient:
    """Return a ContinuumClient configured from environment variables."""
    return ContinuumClient(
        base_url_template=CONTINUUM_BASE_URL_TEMPLATE,
        timeout_sec=CONTINUUM_TIMEOUT_SEC,
    )
