"""
Tiny HTTP server serving GET /healthz for worker processes.

Runs on a daemon thread; exits when the main process exits.
No auth — LAN-local, dashboard + operator are the only consumers.

    start_health_server(port=8201, state=health_state)
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shared.health.state import HealthState

log = logging.getLogger(__name__)


def _handler_factory(state: HealthState):
    class _HealthHandler(BaseHTTPRequestHandler):
        # Silence default per-request stderr noise.
        def log_message(self, fmt, *args) -> None:  # noqa: N802
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/healthz", "/health"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                snap = state.snapshot()
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "error", "reason": str(exc)[:200]}).encode()
                )
                return

            body = json.dumps(snap).encode()
            http_code = 503 if snap.get("status") == "error" else 200
            self.send_response(http_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _HealthHandler


def start_health_server(port: int, state: HealthState, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """
    Start the health HTTP server on a daemon thread and return the server
    instance. Caller doesn't need to hold onto it — it dies with the process.
    """
    handler = _handler_factory(state)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"health-server-{port}",
        daemon=True,
    )
    thread.start()
    log.info("health server listening on %s:%d", host, port)
    return server
