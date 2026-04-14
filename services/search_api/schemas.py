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

    @model_validator(mode="after")
    def _normalize_lists(self) -> "SearchFilters":
        if self.trigger is not None and len(self.trigger) == 0:
            self.trigger = None
        if self.summary_level is not None and len(self.summary_level) == 0:
            self.summary_level = None
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
    id: str
    score: float
    record_type: Literal["event"] = "event"
    trigger: str | None = None
    serial_number: str | None = None
    camera_id: str | None = None
    scope_id: str | None = None
    captured_at: str | None = None
    bucket_start: str | None = None
    caption_text: str | None = None


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
