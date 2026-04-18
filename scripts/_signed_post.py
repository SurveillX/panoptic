"""
Tiny helper shared by dev scripts to POST JSON / multipart with Panoptic
HMAC auth headers applied.

Built on top of the pure signing helper in scripts/sign_request.py.
"""

from __future__ import annotations

import json
import os

import httpx

from scripts.sign_request import sign_panoptic_headers


def _secret() -> str:
    s = os.environ.get("PANOPTIC_SHARED_SECRET_ACTIVE")
    if not s:
        raise RuntimeError(
            "PANOPTIC_SHARED_SECRET_ACTIVE is unset. Set it in .env or export before running this script."
        )
    return s


def signed_json_post(url: str, path: str, serial: str, payload: dict, *, timeout: int = 10) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = sign_panoptic_headers(_secret(), serial, "POST", path, body)
    headers["Content-Type"] = "application/json"
    return httpx.post(url, content=body, headers=headers, timeout=timeout)


def signed_multipart_post(
    url: str,
    path: str,
    serial: str,
    *,
    data: dict,
    files: dict,
    timeout: int = 30,
) -> httpx.Response:
    # Materialize the multipart body via httpx's internal encoder, then sign the
    # exact bytes we'll send. This mirrors the trailer-side pattern documented
    # in docs/TRAILER_AUTH_HANDOFF.md §3.2.
    req = httpx.Request("POST", url, data=data, files=files)
    body = req.read()  # materializes the streaming multipart body
    content_type = req.headers["content-type"]
    headers = sign_panoptic_headers(_secret(), serial, "POST", path, body)
    headers["Content-Type"] = content_type
    return httpx.post(url, content=body, headers=headers, timeout=timeout)
