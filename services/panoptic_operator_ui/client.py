"""
Thin httpx wrapper around the Panoptic Search API.

The operator UI is a rendering-only service: every data read goes
through this module. No Postgres, no Redis, no direct file access
beyond serving its own static assets.

Errors propagate via the httpx exception hierarchy so template handlers
can turn them into friendly error pages.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

SEARCH_API_URL: str = os.environ.get("SEARCH_API_URL", "http://localhost:8600")
_TIMEOUT_SEC = float(os.environ.get("OPERATOR_UI_API_TIMEOUT_SEC", "15"))


class SearchAPIClient:
    """One httpx.Client shared across the app's lifetime."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base = (base_url or SEARCH_API_URL).rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            timeout=_TIMEOUT_SEC,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _post_json(self, path: str, body: dict) -> dict:
        r = self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Endpoints the UI consumes
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return self._get_json("/health")

    def trailer_day(self, serial_number: str, day: str) -> dict:
        return self._get_json(f"/v1/trailer/{serial_number}/day/{day}")

    def reports_list(
        self,
        *,
        serial_number: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> dict:
        params: dict = {"limit": limit}
        if serial_number:
            params["serial_number"] = serial_number
        if kind:
            params["kind"] = kind
        return self._get_json("/v1/reports", params=params)

    def report_status(self, report_id: str) -> dict:
        return self._get_json(f"/v1/reports/{report_id}")

    def fleet_overview(self) -> dict:
        return self._get_json("/v1/fleet/overview")

    def fleet_dashboard(self) -> dict:
        return self._get_json("/v1/fleet/dashboard")

    def event_detail(self, event_id: str) -> dict:
        return self._get_json(f"/v1/events/{event_id}")

    def summary_detail(self, summary_id: str) -> dict:
        return self._get_json(f"/v1/summaries/{summary_id}")

    def image_detail(self, image_id: str) -> dict:
        return self._get_json(f"/v1/images/{image_id}")

    def enqueue_daily_report(self, serial_number: str, date: str) -> dict:
        """POST to /v1/reports/daily — returns {report_id, status}. Idempotent."""
        return self._post_json(
            "/v1/reports/daily",
            {"serial_number": serial_number, "date": date},
        )


class AgentClient:
    """Thin httpx wrapper around the M11 panoptic_agent service."""

    def __init__(self, base_url: str | None = None, timeout_sec: float | None = None) -> None:
        import os
        self._base = (
            base_url
            or os.environ.get("AGENT_URL", "http://localhost:8500")
        ).rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            timeout=float(timeout_sec or os.environ.get("OPERATOR_UI_AGENT_TIMEOUT_SEC", "180")),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def ask(self, *, question: str, scope: dict | None) -> dict:
        body: dict = {"question": question}
        if scope:
            body["scope"] = scope
        r = self._client.post("/v1/agent/ask", json=body)
        r.raise_for_status()
        return r.json()

    def healthz(self) -> dict:
        r = self._client.get("/healthz")
        r.raise_for_status()
        return r.json()

    def search(
        self,
        *,
        query: str | None,
        record_types: list[str],
        serial_number: str | None = None,
        camera_id: str | None = None,
        top_k: int = 10,
    ) -> dict:
        body: dict = {
            "record_types": record_types,
            "top_k": top_k,
        }
        if query:
            body["query"] = query
        filters: dict = {}
        if serial_number:
            filters["serial_number"] = serial_number
        if camera_id:
            filters["camera_id"] = camera_id
        if filters:
            body["filters"] = filters
        return self._post_json("/v1/search", body)
