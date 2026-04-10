"""
Bucket record schema — canonical finalized detection window from Cognia.

bucket_id is deterministic: sha256 of a sorted-key JSON payload that includes
tenant_id, so the same camera/window across different tenants always produces
different IDs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class KeyframeCandidates(BaseModel):
    model_config = ConfigDict(strict=True)

    baseline_ts: datetime | None = None
    peak_ts: datetime | None = None
    change_ts: datetime | None = None


class EventMarker(BaseModel):
    model_config = ConfigDict(strict=True)

    ts: datetime
    event_type: str
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


class BucketCompleteness(BaseModel):
    model_config = ConfigDict(strict=True)

    detection_coverage: float = Field(ge=0.0, le=1.0)
    stream_interrupted_seconds: int = Field(ge=0)
    aggregator_restart_seen: bool


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_bucket_id(
    tenant_id: str,
    camera_id: str,
    start_utc: datetime,
    end_utc: datetime,
    detection_hash: str,
    schema_version: int,
) -> str:
    """
    Deterministic bucket ID.

    Uses sorted-key JSON serialisation so the canonical encoding is stable
    across Python versions and call order.  tenant_id is the first logical
    discriminator — identical camera/window/hash across tenants produces
    different IDs.
    """
    payload = json.dumps(
        {
            "camera_id": camera_id,
            "detection_hash": detection_hash,
            "end_utc": end_utc.isoformat(),
            "schema_version": schema_version,
            "start_utc": start_utc.isoformat(),
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bucket record
# ---------------------------------------------------------------------------


class BucketRecord(BaseModel):
    """
    Authoritative finalized detection bucket emitted by Cognia Aggregator.

    bucket_id must be generated via generate_bucket_id() before construction;
    it is not derived automatically here so that callers can validate the ID
    they received matches what they would compute from the payload.
    """

    model_config = ConfigDict(strict=True)

    bucket_id: str
    tenant_id: str
    site_id: str
    trailer_id: str
    camera_id: str

    bucket_start_utc: datetime
    bucket_end_utc: datetime
    bucket_status: Literal["complete", "partial", "late_finalized"]

    schema_version: int = Field(ge=1)
    detection_hash: str

    activity_score: float = Field(ge=0.0, le=1.0)
    activity_components: dict[str, float]
    object_counts: dict[str, int]

    keyframe_candidates: KeyframeCandidates
    event_markers: list[EventMarker] = Field(default_factory=list)
    completeness: BucketCompleteness
