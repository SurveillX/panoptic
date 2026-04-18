"""
Event record schema — unified event layer.

panoptic_events rows originate from two sources:

  event_source="image_trigger"
      One row per alert/anomaly image. Built from panoptic_images via
      shared.events.build.build_event_row_from_image.

  event_source="bucket_marker"
      One row per marker in a bucket's event_markers. Built from a bucket +
      marker dict via shared.events.build.build_event_row_from_bucket_marker.

event_id is content-addressed — see shared.events.build.generate_event_id.
Enrichment fields (bucket_id on image events, image_id on bucket events)
must not participate in the identity hash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EventSource = Literal["image_trigger", "bucket_marker"]


# Canonical event_type values persisted in panoptic_events.event_type.
EVENT_TYPE_ALERT_CREATED = "alert_created"
EVENT_TYPE_ANOMALY_DETECTED = "anomaly_detected"
EVENT_TYPE_ACTIVITY_SPIKE = "activity_spike"
EVENT_TYPE_AFTER_HOURS = "after_hours_activity"

# Future markers (spec §5), kept here for type-checking consumers even though
# derivation logic doesn't produce them yet. See plan D-1c.
EVENT_TYPE_ACTIVITY_DROP = "activity_drop"
EVENT_TYPE_ACTIVITY_START = "activity_start"
EVENT_TYPE_LATE_START = "late_start"
EVENT_TYPE_UNDERPERFORMING = "underperforming"


class EventRecord(BaseModel):
    """
    Pydantic mirror of panoptic_events row. Used by the event producer and
    backfill for typed construction + by the Search API for response hydration.
    """

    model_config = ConfigDict(strict=False)

    event_id: str

    serial_number: str
    camera_id: str
    scope_id: str

    event_type: str
    event_source: EventSource

    severity: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    start_time_utc: datetime
    end_time_utc: datetime | None = None
    event_time_utc: datetime

    bucket_id: str | None = None
    image_id: str | None = None

    title: str | None = None
    description: str | None = None
    metadata_json: dict = Field(default_factory=dict)
