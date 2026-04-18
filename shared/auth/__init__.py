"""HMAC-signed trailer auth (shared fleet secret). See docs/AUTH_DESIGN.md."""

from shared.auth.hmac_auth import (
    AUTH_ENABLED,
    AuthFailure,
    canonical_signing_string,
    compute_signature,
    sign_headers,
    verify_request,
)

__all__ = [
    "AUTH_ENABLED",
    "AuthFailure",
    "canonical_signing_string",
    "compute_signature",
    "sign_headers",
    "verify_request",
]
