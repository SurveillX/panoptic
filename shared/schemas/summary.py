"""
Summary record schema — authoritative output of the Summary Agent.

summary_id is deterministic: sha256 of a sorted-key JSON payload that includes
tenant_id, so the same scope/window/children across different tenants always
produces different IDs.

Search MUST filter is_latest=True to avoid returning superseded versions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class SummaryCoverage(BaseModel):
    model_config = ConfigDict(strict=True)

    expected: int = Field(ge=0)
    present: int = Field(ge=0)
    ratio: float = Field(ge=0.0, le=1.0)
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_summary_id(
    tenant_id: str,
    level: str,
    scope_id: str,
    window_start: datetime,
    window_end: datetime,
    child_set_hash: str,
    model_profile: str,
    prompt_version: str,
    summary_schema_version: int,
) -> str:
    """
    Deterministic summary ID.

    tenant_id is the first logical discriminator — identical scope/window/
    children across tenants produces different IDs.  sort_keys=True ensures
    canonical encoding regardless of insertion order.
    """
    payload = json.dumps(
        {
            "child_set_hash": child_set_hash,
            "level": level,
            "model_profile": model_profile,
            "prompt_version": prompt_version,
            "scope_id": scope_id,
            "summary_schema_version": summary_schema_version,
            "tenant_id": tenant_id,
            "window_end": window_end.isoformat(),
            "window_start": window_start.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Summary record
# ---------------------------------------------------------------------------


class SummaryRecord(BaseModel):
    """
    Authoritative summary record written by the Summary Agent.

    Versioning:
      - version increments on each recompute of the same logical scope/window.
      - When a new version is written, the previous record's superseded_by is
        set to the new summary_id and is_latest is set to False.
      - Search must always filter is_latest=True.
    """

    model_config = ConfigDict(strict=True)

    summary_id: str
    tenant_id: str
    level: Literal["camera", "hour", "day", "site"]
    scope_id: str

    start_time: datetime
    end_time: datetime

    summary: str
    key_events: list[Any] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    coverage: SummaryCoverage

    summary_mode: Literal["full", "partial", "metadata_only"]
    frames_used: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)

    embedding_status: Literal["pending", "success", "failed"] = "pending"

    version: int = Field(ge=1, default=1)
    is_latest: bool = True
    superseded_by: str | None = None

    model_profile: str
    prompt_version: str
    schema_version: int = Field(ge=1)

    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
