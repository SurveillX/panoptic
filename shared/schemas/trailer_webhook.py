"""
Pydantic models for the trailer webhook payload.

The trailer pushes one POST per object_type per 15-minute bucket window.
VIL aggregates these fragments into a single BucketRecord before ingestion.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TrailerBucketData(BaseModel):
    """Per-object-type bucket data from the trailer's cognia-aggregator."""

    model_config = ConfigDict(strict=False)

    bucket_start: datetime
    bucket_end: datetime
    bucket_minutes: int
    camera_id: str
    object_type: str
    unique_tracker_ids: int
    total_detections: int
    frame_count: int
    min_count: int
    max_count: int
    mode_count: int
    mean_count: float
    std_dev_count: float
    max_count_at: datetime
    min_confidence: float
    max_confidence: float
    avg_confidence: float
    first_detection_at: datetime
    last_detection_at: datetime
    active_seconds: float
    duty_cycle: float
    anomaly_score: float
    anomaly_flag: int


class TrailerBucketPayload(BaseModel):
    """Top-level webhook payload pushed by the trailer drain task."""

    model_config = ConfigDict(strict=False)

    event_id: str
    schema_version: str
    sent_at_utc: datetime
    serial_number: str
    camera_id: str
    bucket: TrailerBucketData
