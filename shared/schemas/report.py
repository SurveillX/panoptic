"""
Report schemas — M9.

Shapes:
  ReportRecord            — Pydantic mirror of panoptic_reports rows
  DailyReportRequest      — POST /v1/reports/daily body
  WeeklyReportRequest     — POST /v1/reports/weekly body
  ReportEnqueueResponse   — immediate response: {report_id, status}
  ReportStatusResponse    — GET /v1/reports/{report_id} response
  ReportMetadata          — typed view of panoptic_reports.metadata_json

report_id identity hash: sha256(serial_number, kind, window_start_utc,
window_end_utc). NOT parametrized by template_version — a template change
must overwrite the same row in place rather than mint a new report_id.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReportKind = Literal["daily", "weekly"]
ReportStatus = Literal["pending", "running", "success", "failed"]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def generate_report_id(
    *,
    serial_number: str,
    kind: ReportKind,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> str:
    """
    Deterministic report_id. Same (serial, kind, window) → same id.
    template_version is NOT part of this hash — see shared/schemas/report
    module docstring.
    """
    payload = json.dumps(
        {
            "kind": kind,
            "serial_number": serial_number,
            "window_end_utc": window_end_utc.isoformat(),
            "window_start_utc": window_start_utc.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class DailyReportRequest(BaseModel):
    """POST /v1/reports/daily — generate one daily HTML for (serial, date UTC)."""

    model_config = ConfigDict(strict=False)

    serial_number: str = Field(min_length=1)
    # ISO date string (YYYY-MM-DD) or full ISO datetime. Normalized to a
    # 24h [00:00:00, 00:00:00+1d) window in UTC.
    date: str = Field(min_length=10)

    @model_validator(mode="after")
    def _normalize(self) -> "DailyReportRequest":
        self.serial_number = self.serial_number.strip()
        self.date = self.date.strip()
        if not self.serial_number:
            raise ValueError("serial_number must be non-empty")
        return self


class WeeklyReportRequest(BaseModel):
    """POST /v1/reports/weekly — generate one weekly HTML for (serial, ISO week).

    iso_week is 'YYYYWnn' (ISO 8601 week date, Mon-anchored). The window is
    [Mon 00:00:00 UTC, next-Mon 00:00:00 UTC).
    """

    model_config = ConfigDict(strict=False)

    serial_number: str = Field(min_length=1)
    iso_week: str = Field(pattern=r"^\d{4}W\d{2}$")

    @model_validator(mode="after")
    def _normalize(self) -> "WeeklyReportRequest":
        self.serial_number = self.serial_number.strip()
        if not self.serial_number:
            raise ValueError("serial_number must be non-empty")
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class NarrativeBlock(BaseModel):
    """A single per-camera (daily) or per-day (weekly) narrative piece."""

    model_config = ConfigDict(strict=False)

    # For daily reports this is the camera_id; for weekly it's a day key
    # like "2026-04-13".
    key: str
    headline: str
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)


class OverallNarrative(BaseModel):
    """Overall headline/summary pair (fusion output)."""

    model_config = ConfigDict(strict=False)

    headline: str
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting: list[str] = Field(default_factory=list)


class ReportMetadata(BaseModel):
    """
    Structured view of panoptic_reports.metadata_json.

    Populated on status='success'. Fields may be absent/empty otherwise.

    Narratives are persisted so weekly reports can use daily outputs as
    their narrative base without re-running VLM synthesis per day.
    """

    model_config = ConfigDict(strict=False, extra="allow")

    cited_image_ids: list[str] = Field(default_factory=list)
    cited_event_ids: list[str] = Field(default_factory=list)
    cited_summary_ids: list[str] = Field(default_factory=list)
    cited_camera_ids: list[str] = Field(default_factory=list)
    input_counts: dict[str, int] = Field(default_factory=dict)
    coverage: dict[str, int] = Field(default_factory=dict)
    vlm_timings_ms: dict[str, int] = Field(default_factory=dict)
    template_version: str | None = None

    # Narrative persistence (added P9.4). `narratives` holds per-camera
    # blocks (daily) or per-day blocks (weekly). `overall` holds the
    # fusion output for the whole report.
    narratives: list[NarrativeBlock] = Field(default_factory=list)
    overall: OverallNarrative | None = None


class ReportRecord(BaseModel):
    """Full Pydantic mirror of a panoptic_reports row."""

    model_config = ConfigDict(strict=False)

    report_id: str
    serial_number: str
    kind: ReportKind

    window_start_utc: datetime
    window_end_utc: datetime

    storage_path: str | None = None
    status: ReportStatus
    last_error: str | None = None
    generated_at: datetime | None = None

    metadata: ReportMetadata = Field(default_factory=ReportMetadata)

    created_at: datetime
    updated_at: datetime


class ReportEnqueueResponse(BaseModel):
    """Immediate response from POST /v1/reports/{daily,weekly}."""

    report_id: str
    status: ReportStatus


class ReportStatusResponse(ReportRecord):
    """GET /v1/reports/{report_id} — identical shape to ReportRecord."""


__all__ = [
    "ReportKind",
    "ReportStatus",
    "ReportMetadata",
    "NarrativeBlock",
    "OverallNarrative",
    "ReportRecord",
    "ReportEnqueueResponse",
    "ReportStatusResponse",
    "DailyReportRequest",
    "WeeklyReportRequest",
    "generate_report_id",
]
