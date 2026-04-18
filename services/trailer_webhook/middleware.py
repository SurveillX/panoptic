"""
ASGI middleware for HMAC-signed trailer push auth.

Runs before FastAPI's handlers; buffers the raw request body once,
verifies the signature against the canonical signing string, and on
success replays the body to downstream routes.

Only /v1/trailer/* paths are verified. /health, /v1/admin/*, /docs, and
anything else pass through untouched.

See docs/AUTH_DESIGN.md.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from shared.auth.hmac_auth import (
    AUTH_ENABLED,
    AuthFailure,
    ReplayCache,
    TrailerRegistry,
    verify_request,
)

log = logging.getLogger(__name__)

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


_PROTECTED_PREFIXES = ("/v1/trailer/",)


def _is_protected(path: str) -> bool:
    return any(path.startswith(p) for p in _PROTECTED_PREFIXES)


class TrailerAuthMiddleware:
    """
    ASGI middleware. Wire it at app creation:

        app = FastAPI()
        app.add_middleware(
            TrailerAuthMiddleware,
            registry=registry,
            replay=replay_cache,
        )

    (Actually: FastAPI's add_middleware uses class-level construction,
    so use `app = TrailerAuthMiddleware(inner_app, registry=..., replay=...)`
    or FastAPI's middleware= parameter.)
    """

    def __init__(self, app: ASGIApp, *, registry: TrailerRegistry, replay: ReplayCache) -> None:
        self.app = app
        self.registry = registry
        self.replay = replay

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _is_protected(path):
            await self.app(scope, receive, send)
            return

        if not AUTH_ENABLED:
            # Dev-mode hatch already warns loudly in the server startup logs.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        body = await _read_body(receive)

        failure = verify_request(
            method=method,
            path=path,
            headers=headers,
            body=body,
            registry=self.registry,
            replay=self.replay,
        )

        if failure is not None:
            await _log_and_reject(send, failure, scope)
            return

        # Replay the buffered body to the downstream app.
        replayed_receive = _replay_body(body)
        await self.app(scope, replayed_receive, send)


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------


async def _read_body(receive) -> bytes:
    """Read the full ASGI http.request body, concatenating all chunks."""
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return b"".join(chunks)
        if msg["type"] == "http.request":
            chunks.append(msg.get("body", b"") or b"")
            if not msg.get("more_body", False):
                return b"".join(chunks)


def _replay_body(body: bytes):
    """Build a fresh async generator that re-emits the buffered body once."""
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _log_and_reject(send, failure: AuthFailure, scope: dict) -> None:
    log.warning(
        "trailer_auth: reject category=%s status=%d serial=%s path=%s method=%s fields=%s",
        failure.category,
        failure.http_status,
        failure.serial,
        scope.get("path"),
        scope.get("method"),
        failure.log_fields,
    )
    body = json.dumps(failure.body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": failure.http_status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
