"""
Request-signing helper.

Library form — used by dev scripts and (referenced by) trailer-side code:

    from scripts.sign_request import sign_panoptic_headers
    headers = sign_panoptic_headers(
        secret=..., serial="...", method="POST", path="/v1/trailer/image", body=body_bytes,
    )

CLI form — for curl testing:

    echo -n '<body>' | .venv/bin/python scripts/sign_request.py \\
        --secret "$PANOPTIC_SHARED_SECRET_ACTIVE" \\
        --serial YARD-A-001 --method POST --path /v1/trailer/bucket-notification

Prints the three X-Panoptic-* headers in `Header: Value` form.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time


def sign_panoptic_headers(
    secret: str,
    serial: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    body_sha256 = hashlib.sha256(body).hexdigest()
    signing_string = f"{serial}|{ts}|{method.upper()}|{path}|{body_sha256}"
    sig = hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Panoptic-Serial": serial,
        "X-Panoptic-Timestamp": ts,
        "X-Panoptic-Signature": sig,
    }


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", required=True)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--method", default="POST")
    ap.add_argument("--path", required=True)
    ap.add_argument("--timestamp", type=int, default=None)
    args = ap.parse_args()

    body = sys.stdin.buffer.read()
    headers = sign_panoptic_headers(
        secret=args.secret,
        serial=args.serial,
        method=args.method,
        path=args.path,
        body=body,
        timestamp=args.timestamp,
    )
    for k, v in headers.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
