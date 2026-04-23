"""
Thin httpx wrapper around the Panoptic Search API — used by the agent's
tool dispatch layer.

The agent service is read-mostly. The one write it performs
(`generate_daily_report`) goes through the exact same HTTP surface the
operator UI uses. No DB or Redis access from this service.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

SEARCH_API_URL: str = os.environ.get(
    "AGENT_SEARCH_API_URL",
    os.environ.get("SEARCH_API_URL", "http://localhost:8600"),
)
_TIMEOUT_SEC = float(os.environ.get("AGENT_SEARCH_API_TIMEOUT_SEC", "20"))


class SearchAPIClient:
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

    def _get_json(self, path: str, params: dict | None = None) -> Any:
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _post_json(self, path: str, body: dict) -> Any:
        r = self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def health(self) -> dict:
        return self._get_json("/health")

    # ------------------------------------------------------------------
    # Tool targets
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        query: str | None,
        record_types: list[str],
        filters: dict | None = None,
        top_k: int = 10,
    ) -> dict:
        body: dict = {"record_types": record_types, "top_k": top_k}
        if query:
            body["query"] = query
        if filters:
            body["filters"] = filters
        return self._post_json("/v1/search", body)

    def verify(
        self,
        *,
        query: str,
        record_types: list[str] | None = None,
        filters: dict | None = None,
        search_top_k: int = 10,
    ) -> dict:
        body: dict = {"query": query, "search_top_k": search_top_k}
        if record_types:
            body["record_types"] = record_types
        if filters:
            body["filters"] = filters
        return self._post_json("/v1/search/verify", body)

    def pull_frame(
        self,
        *,
        serial_number: str,
        camera_id: str,
        timestamp_utc: str,
        reason: str | None = None,
    ) -> dict:
        """
        M14 on-demand continuum pull. Fetches a JPEG from the trailer and
        persists it as a panoptic_images row. Returns {image_id, status,
        storage_path, caption_status, bucket_start_utc, bucket_end_utc}.
        """
        body: dict = {
            "serial_number": serial_number,
            "camera_id":     camera_id,
            "timestamp_utc": timestamp_utc,
        }
        if reason:
            body["reason"] = reason
        return self._post_json("/v1/search/pull_frame", body)

    def summarize_period(
        self,
        *,
        serial_number: str,
        camera_ids: list[str] | None,
        time_range_start: str,
        time_range_end: str,
        summary_type: str = "operational",
    ) -> dict:
        scope: dict = {"serial_number": serial_number}
        if camera_ids:
            scope["camera_ids"] = camera_ids
        body = {
            "scope": scope,
            "time_range": {"start": time_range_start, "end": time_range_end},
            "summary_type": summary_type,
        }
        return self._post_json("/v1/summarize/period", body)

    def get_trailer_day(self, *, serial_number: str, date: str) -> dict:
        return self._get_json(f"/v1/trailer/{serial_number}/day/{date}")

    def get_fleet_overview(self) -> dict:
        return self._get_json("/v1/fleet/overview")

    def get_event(self, *, event_id: str) -> dict:
        return self._get_json(f"/v1/events/{event_id}")

    def get_summary(self, *, summary_id: str) -> dict:
        return self._get_json(f"/v1/summaries/{summary_id}")

    def get_image(self, *, image_id: str) -> dict:
        return self._get_json(f"/v1/images/{image_id}")

    def list_reports(
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

    def get_report(self, *, report_id: str) -> dict:
        return self._get_json(f"/v1/reports/{report_id}")

    def generate_daily_report(self, *, serial_number: str, date: str) -> dict:
        return self._post_json(
            "/v1/reports/daily",
            {"serial_number": serial_number, "date": date},
        )
