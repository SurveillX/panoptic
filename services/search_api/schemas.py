"""
Pydantic schemas for the Search API.

See /home/bryan/.claude/plans/quirky-honking-peacock.md for the full contract.

Filter semantics:
  serial_number / camera_id / scope_id / trigger use EXACT equality only.
  No substring, no prefix, no regex, no ILIKE — anywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


RecordType = Literal["summary", "image", "event"]
TriggerValue = Literal["alert", "anomaly", "baseline"]
SummaryLevel = Literal["camera", "hour", "day", "site"]

DEFAULT_RECORD_TYPES: list[RecordType] = ["summary", "image", "event"]
DEFAULT_TOP_K: int = 10
MAX_TOP_K: int = 50


class TimeRange(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _validate_order(self) -> "TimeRange":
        if self.start >= self.end:
            raise ValueError("time_range.start must be < time_range.end")
        return self


class SearchFilters(BaseModel):
    serial_number: str | None = None
    camera_id: str | None = None
    time_range: TimeRange | None = None
    trigger: list[TriggerValue] | None = None
    summary_level: list[SummaryLevel] | None = None
    # Event-layer filters (panoptic_events):
    #   event_type:   e.g. "alert_created", "anomaly_detected", "activity_spike",
    #                      "after_hours_activity"
    #   event_source: "image_trigger" | "bucket_marker"
    event_type: list[str] | None = None
    event_source: list[str] | None = None

    @model_validator(mode="after")
    def _normalize_lists(self) -> "SearchFilters":
        if self.trigger is not None and len(self.trigger) == 0:
            self.trigger = None
        if self.summary_level is not None and len(self.summary_level) == 0:
            self.summary_level = None
        if self.event_type is not None and len(self.event_type) == 0:
            self.event_type = None
        if self.event_source is not None and len(self.event_source) == 0:
            self.event_source = None
        return self


class SearchRequest(BaseModel):
    query: str | None = None
    record_types: list[RecordType] = Field(default_factory=lambda: list(DEFAULT_RECORD_TYPES))
    filters: SearchFilters = Field(default_factory=SearchFilters)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)

    @model_validator(mode="after")
    def _validate_query_requirements(self) -> "SearchRequest":
        if self.query is not None and not self.query.strip():
            self.query = None

        if self.query is None:
            semantic_types = {"summary", "image"} & set(self.record_types)
            if semantic_types:
                raise ValueError(
                    "query is required when record_types includes "
                    f"{sorted(semantic_types)}; only 'event' supports filter-only browse"
                )

        if not self.record_types:
            raise ValueError("record_types must be non-empty")
        return self


class SummaryHit(BaseModel):
    id: str
    score: float
    record_type: Literal["summary"] = "summary"
    level: str | None = None
    serial_number: str | None = None
    scope_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    summary: str | None = None
    key_events_labels: list[str] = Field(default_factory=list)
    confidence: float | None = None


class ImageHit(BaseModel):
    id: str
    score: float
    record_type: Literal["image"] = "image"
    serial_number: str | None = None
    camera_id: str | None = None
    scope_id: str | None = None
    trigger: str | None = None
    captured_at: str | None = None
    bucket_start: str | None = None
    caption_text: str | None = None
    storage_path: str | None = None


class EventHit(BaseModel):
    """
    Event record hit. Fields mirror panoptic_events — the legacy
    image-trigger-derived fields (trigger, captured_at, bucket_start,
    caption_text) are gone per P4 D-5 (clean cut — no back-compat layer).
    """

    id: str  # event_id (content-addressed SHA256)
    score: float
    record_type: Literal["event"] = "event"

    event_type: str | None = None
    event_source: str | None = None  # "image_trigger" | "bucket_marker"

    serial_number: str | None = None
    camera_id: str | None = None
    scope_id: str | None = None

    severity: float | None = None
    confidence: float | None = None

    start_time_utc: str | None = None
    end_time_utc: str | None = None
    event_time_utc: str | None = None

    bucket_id: str | None = None
    image_id: str | None = None

    title: str | None = None
    description: str | None = None


class SearchResults(BaseModel):
    summaries: list[SummaryHit] = Field(default_factory=list)
    images: list[ImageHit] = Field(default_factory=list)
    events: list[EventHit] = Field(default_factory=list)


class TimingMs(BaseModel):
    parse: int = 0
    qdrant: int = 0
    postgres: int = 0
    rerank: int = 0
    total: int = 0


class SearchResponse(BaseModel):
    query: str | None
    total: int
    results: SearchResults
    timing_ms: TimingMs


# ---------------------------------------------------------------------------
# Verification (POST /v1/search/verify)
# ---------------------------------------------------------------------------

Verdict = Literal[
    "supported",
    "partially_supported",
    "not_supported",
    "insufficient_evidence",
]

VERIFY_MAX_SUMMARIES_CAP: int = 5
VERIFY_MAX_IMAGES_CAP: int = 5
VERIFY_MAX_EVENTS_CAP: int = 10


class VerifyRequest(BaseModel):
    query: str
    record_types: list[RecordType] = Field(default_factory=lambda: list(DEFAULT_RECORD_TYPES))
    filters: SearchFilters = Field(default_factory=SearchFilters)
    search_top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    verify_max_summaries: int = Field(default=3, ge=0, le=VERIFY_MAX_SUMMARIES_CAP)
    verify_max_images: int = Field(default=4, ge=0, le=VERIFY_MAX_IMAGES_CAP)
    verify_max_events: int = Field(default=5, ge=0, le=VERIFY_MAX_EVENTS_CAP)

    @model_validator(mode="after")
    def _validate(self) -> "VerifyRequest":
        if not self.query or not self.query.strip():
            raise ValueError("query is required for verification")
        self.query = self.query.strip()
        if not self.record_types:
            raise ValueError("record_types must be non-empty")
        return self


class Verification(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_summary_ids: list[str] = Field(default_factory=list)
    supporting_image_ids: list[str] = Field(default_factory=list)
    supporting_event_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    uncertainties: list[str] = Field(default_factory=list)


class VerifyTimingMs(BaseModel):
    search: int = 0
    verification: int = 0
    total: int = 0


class VerifyResponse(BaseModel):
    query: str
    results: SearchResults
    verification: Verification
    timing_ms: VerifyTimingMs


# ---------------------------------------------------------------------------
# Period summarization (POST /v1/summarize/period)
# ---------------------------------------------------------------------------

SummaryType = Literal["operational", "progress", "mixed"]

PERIOD_MAX_SUMMARIES_CAP: int = 20
PERIOD_MAX_IMAGES_CAP: int = 12
PERIOD_MAX_EVENTS_CAP: int = 20


class PeriodScope(BaseModel):
    serial_number: str = Field(min_length=1)
    camera_ids: list[str] | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "PeriodScope":
        if self.camera_ids is not None:
            # drop empty strings + preserve order + dedup
            seen: set[str] = set()
            kept: list[str] = []
            for cam in self.camera_ids:
                c = cam.strip()
                if c and c not in seen:
                    seen.add(c)
                    kept.append(c)
            self.camera_ids = kept if kept else None
        return self


class PeriodSummarizeRequest(BaseModel):
    scope: PeriodScope
    time_range: TimeRange
    summary_type: SummaryType = "operational"
    max_input_summaries_per_camera: int = Field(default=12, ge=0, le=PERIOD_MAX_SUMMARIES_CAP)
    max_input_images_per_camera: int = Field(default=8, ge=0, le=PERIOD_MAX_IMAGES_CAP)
    max_input_events_per_camera: int = Field(default=12, ge=0, le=PERIOD_MAX_EVENTS_CAP)


class CameraSummary(BaseModel):
    camera_id: str
    headline: str
    summary: str
    supporting_summary_ids: list[str] = Field(default_factory=list)
    supporting_image_ids: list[str] = Field(default_factory=list)
    supporting_event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class OverallSummary(BaseModel):
    headline: str
    summary: str
    supporting_camera_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class PeriodTimingMs(BaseModel):
    retrieve: int = 0
    camera_synthesis: int = 0
    fusion: int = 0
    total: int = 0


class PeriodSummarizeResponse(BaseModel):
    scope: PeriodScope
    time_range: TimeRange
    camera_summaries: list[CameraSummary] = Field(default_factory=list)
    overall: OverallSummary
    timing_ms: PeriodTimingMs


# ---------------------------------------------------------------------------
# M10 — Operator UI read endpoints
# ---------------------------------------------------------------------------


class TrailerDayEvent(BaseModel):
    """One event row, shaped for the trailer-day rollup."""

    event_id: str
    event_type: str
    event_source: str
    camera_id: str | None = None
    severity: float | None = None
    confidence: float | None = None
    event_time_utc: str | None = None
    title: str | None = None
    description: str | None = None
    bucket_id: str | None = None
    image_id: str | None = None


class TrailerDayImage(BaseModel):
    """One image (after dedup), shaped for the trailer-day rollup."""

    image_id: str
    camera_id: str | None = None
    trigger: str | None = None
    captured_at: str | None = None
    bucket_start: str | None = None
    caption_text: str | None = None


class TrailerDaySummary(BaseModel):
    """One summary, shaped for the trailer-day rollup."""

    summary_id: str
    camera_id: str | None = None
    level: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    summary: str | None = None
    confidence: float | None = None


class TrailerDayPerCamera(BaseModel):
    """Per-camera mini-row for the trailer-day rollup table."""

    camera_id: str
    event_count: int = 0
    image_count: int = 0
    summary_count: int = 0


class TrailerDayResponse(BaseModel):
    """
    Full rollup for one (serial, date) UTC window.

    `latest_daily_report` — the existing daily report row for this
    window, if one exists. UI uses it to link into the report viewer.
    """

    serial_number: str
    date: str  # YYYY-MM-DD (UTC)
    window_start_utc: str
    window_end_utc: str

    events: list[TrailerDayEvent] = Field(default_factory=list)
    images: list[TrailerDayImage] = Field(default_factory=list)
    summaries: list[TrailerDaySummary] = Field(default_factory=list)
    per_camera: list[TrailerDayPerCamera] = Field(default_factory=list)

    event_count: int = 0
    image_count: int = 0
    summary_count: int = 0
    camera_count: int = 0

    latest_daily_report_id: str | None = None
    latest_daily_report_status: str | None = None


class ImageDetailResponse(BaseModel):
    """One panoptic_images row — used by /v1/images/{id}."""

    image_id: str
    serial_number: str
    camera_id: str
    scope_id: str
    trigger: str
    bucket_start_utc: str | None = None
    bucket_end_utc: str | None = None
    captured_at_utc: str | None = None
    caption_text: str | None = None
    caption_status: str | None = None
    storage_path: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    created_at: str | None = None


class EventDetailResponse(BaseModel):
    """One panoptic_events row — used by /v1/events/{id}."""

    event_id: str
    serial_number: str
    camera_id: str
    scope_id: str
    event_type: str
    event_source: str
    severity: float | None = None
    confidence: float | None = None
    start_time_utc: str | None = None
    end_time_utc: str | None = None
    event_time_utc: str | None = None
    bucket_id: str | None = None
    image_id: str | None = None
    title: str | None = None
    description: str | None = None
    metadata_json: dict = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class SummaryDetailResponse(BaseModel):
    """One panoptic_summaries row — used by /v1/summaries/{id}."""

    summary_id: str
    serial_number: str
    level: str
    scope_id: str
    start_time: str | None = None
    end_time: str | None = None
    summary: str
    key_events_labels: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    summary_mode: str | None = None
    frames_used: int | None = None
    confidence: float | None = None
    model_profile: str | None = None
    prompt_version: str | None = None
    is_latest: bool | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Fleet overview
# ---------------------------------------------------------------------------


class FleetTrailer(BaseModel):
    """One row of the fleet overview."""

    serial_number: str
    name: str | None = None
    is_active: bool
    # Most recent bucket / image timestamps (null if none)
    last_bucket_start_utc: str | None = None
    last_image_captured_at_utc: str | None = None
    # Event count in the last 24 hours (rolling, computed at request time)
    event_count_24h: int = 0
    # Most recent successful daily report
    latest_daily_report_id: str | None = None
    latest_daily_report_window_start_utc: str | None = None
    latest_daily_report_generated_at: str | None = None


class FleetOverviewResponse(BaseModel):
    trailers: list[FleetTrailer] = Field(default_factory=list)
    count: int = 0
    generated_at_utc: str
