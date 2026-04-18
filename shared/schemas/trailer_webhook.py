"""
Pydantic models for the trailer webhook payload.

The trailer pushes one POST per object_type per 15-minute bucket window.
Panoptic aggregates these fragments into a single BucketRecord before ingestion.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TrailerBucketData(BaseModel):
    """Per-object-type bucket data from the trailer's cognia-aggregator."""

    model_config = ConfigDict(strict=False)

    bucket_start: datetime
    bucket_end: datetime
    # Bucket window length in minutes. Defaults to 15 — the trailer
    # aggregator's standard cadence — when omitted by the client.
    bucket_minutes: int = 15
    camera_id: str
    object_type: str
    unique_tracker_ids: int
    total_detections: int
    frame_count: int
    min_count: int
    max_count: int
    mode_count: int
    # Statistical aggregates — None when the trailer has too few samples
    # (e.g. empty bucket) or when the aggregator hasn't populated them yet.
    mean_count: float | None = None
    std_dev_count: float | None = None
    # The following fields can be null when the trailer's anomaly scorer
    # hasn't accumulated a baseline yet, or when the bucket had no detections.
    # All downstream consumers must handle None gracefully.
    max_count_at: datetime | None = None
    min_confidence: float | None = None
    max_confidence: float | None = None
    avg_confidence: float | None = None
    first_detection_at: datetime | None = None
    last_detection_at: datetime | None = None
    active_seconds: float
    duty_cycle: float
    anomaly_score: float | None = None
    # Defaults to 0 when the trailer's anomaly scorer isn't running. Pairs
    # with the nullable anomaly_score above — both absent => not-a-spike.
    anomaly_flag: int = 0


class TrailerBucketPayload(BaseModel):
    """Top-level webhook payload pushed by the trailer drain task."""

    model_config = ConfigDict(strict=False)

    event_id: str
    schema_version: str
    sent_at_utc: datetime
    serial_number: str
    camera_id: str
    bucket: TrailerBucketData
