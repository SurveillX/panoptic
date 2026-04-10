"""
LLM output schema — validated response from the language model.

All LLM output must be validated against this schema before storage.
Validation failure policy (build_spec §10.3):
  - Retry once with repair prompt
  - If still fails: mark job degraded, store partial summary with confidence=0.0
  - Never store raw unvalidated output
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LLMSummaryOutput(BaseModel):
    model_config = ConfigDict(strict=False)  # coercion from JSON deserialization

    summary: str
    key_events: list[dict] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
