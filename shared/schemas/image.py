"""
Image metadata schema — trailer-pushed image ingest.

image_id is deterministic: sha256 of a sorted-key JSON payload that includes
serial_number, camera_id, bucket window, trigger, and timestamp_ms.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class ImageMetadataContext(BaseModel):
    # extra="allow" lets per-trigger Cognia fields (similarity, sample_id,
    # reason, rule_id, incident_id, etc.) flow into context_json verbatim.
    # Panoptic-specific typed fields stay declared; anything else is
    # preserved by model_dump().
    model_config = ConfigDict(strict=False, extra="allow")

    max_anomaly_score: float | None = None
    max_count: int | None = None
    object_types: list[str] = Field(default_factory=list)
    row_count: int | None = None


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_image_id(
    serial_number: str,
    camera_id: str,
    bucket_start: str,
    bucket_end: str,
    trigger: str,
    timestamp_ms: int | None,
) -> str:
    """
    Deterministic image ID.

    Uses sorted-key JSON serialisation so the canonical encoding is stable
    across Python versions and call order.  When timestamp_ms is None
    (baseline trigger), the literal string "baseline" is used instead.
    """
    ts_value = str(timestamp_ms) if timestamp_ms is not None else "baseline"
    payload = json.dumps(
        {
            "bucket_end": bucket_end,
            "bucket_start": bucket_start,
            "camera_id": camera_id,
            "serial_number": serial_number,
            "timestamp_ms": ts_value,
            "trigger": trigger,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Ingest metadata
# ---------------------------------------------------------------------------


class TrailerImageMetadata(BaseModel):
    """
    Pydantic model for the ``metadata`` JSON part of POST /v1/trailer/image.
    """

    model_config = ConfigDict(strict=False)

    event_id: str
    schema_version: str
    sent_at_utc: datetime

    serial_number: str
    camera_id: str

    bucket_start: datetime
    bucket_end: datetime

    trigger: Literal["alert", "anomaly", "baseline", "novelty"]
    timestamp_ms: int | None = None
    captured_at_utc: datetime | None = None

    selection_policy_version: str = "1"
    context: ImageMetadataContext = Field(default_factory=ImageMetadataContext)

    @model_validator(mode="after")
    def _require_timestamp_ms_for_non_baseline(self) -> TrailerImageMetadata:
        if self.trigger != "baseline" and self.timestamp_ms is None:
            raise ValueError("timestamp_ms is required when trigger is not 'baseline'")
        return self
