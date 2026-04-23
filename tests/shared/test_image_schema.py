"""
Tests for shared.schemas.image — trigger enum + context passthrough.

M13 introduces `novelty` as a fourth trigger value. These tests guard
both the acceptance (novelty validates end-to-end) and the shape rules
that other triggers depend on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.schemas.image import TrailerImageMetadata


UTC = timezone.utc


def _base_meta(**overrides) -> dict:
    """Minimal valid metadata dict; overrides merged on top."""
    out = {
        "event_id":       "test:1",
        "schema_version": "1",
        "sent_at_utc":    "2026-04-23T12:00:00+00:00",
        "serial_number":  "TEST",
        "camera_id":      "test-cam",
        "bucket_start":   "2026-04-23T12:00:00+00:00",
        "bucket_end":     "2026-04-23T12:15:00+00:00",
        "trigger":        "baseline",
        "timestamp_ms":   None,
        "context":        {},
    }
    out.update(overrides)
    return out


class TestTriggerAcceptance:
    def test_alert_validates(self):
        m = TrailerImageMetadata.model_validate(
            _base_meta(trigger="alert", timestamp_ms=1776894000000)
        )
        assert m.trigger == "alert"

    def test_anomaly_validates(self):
        m = TrailerImageMetadata.model_validate(
            _base_meta(trigger="anomaly", timestamp_ms=1776894000000)
        )
        assert m.trigger == "anomaly"

    def test_baseline_validates_without_timestamp(self):
        # baseline is the only trigger allowed to omit timestamp_ms.
        m = TrailerImageMetadata.model_validate(
            _base_meta(trigger="baseline", timestamp_ms=None)
        )
        assert m.trigger == "baseline"
        assert m.timestamp_ms is None

    def test_novelty_validates_with_similarity_context(self):
        m = TrailerImageMetadata.model_validate(
            _base_meta(
                trigger="novelty",
                timestamp_ms=1776894000000,
                event_id="novelty:test-cam:3",
                context={
                    "sample_id":    3,
                    "similarity":   0.12,
                    "reason":       "novel_scene",
                    "frame_source": "deepstream",
                },
            )
        )
        assert m.trigger == "novelty"
        # ImageMetadataContext uses extra="allow" (M13) so all per-trigger
        # fields round-trip through model_dump.
        ctx = m.context.model_dump()
        assert ctx["similarity"] == 0.12
        assert ctx["sample_id"] == 3
        assert ctx["reason"] == "novel_scene"

    def test_unknown_trigger_rejected(self):
        with pytest.raises(ValidationError):
            TrailerImageMetadata.model_validate(
                _base_meta(trigger="foo", timestamp_ms=1776894000000)
            )

    def test_novelty_without_timestamp_rejected(self):
        # Novelty must carry timestamp_ms (only baseline may omit).
        with pytest.raises(ValidationError):
            TrailerImageMetadata.model_validate(
                _base_meta(trigger="novelty", timestamp_ms=None)
            )

    def test_alert_without_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            TrailerImageMetadata.model_validate(
                _base_meta(trigger="alert", timestamp_ms=None)
            )


class TestContextPassthrough:
    def test_alert_rule_fields_preserved(self):
        m = TrailerImageMetadata.model_validate(
            _base_meta(
                trigger="alert",
                timestamp_ms=1776894000000,
                context={
                    "rule_id":     "rule-42",
                    "rule_name":   "phase4 smoke test",
                    "rule_type":   "fov",
                    "severity":    "warning",
                    "object_type": "person",
                    "count":       7,
                },
            )
        )
        ctx = m.context.model_dump()
        assert ctx["rule_id"] == "rule-42"
        assert ctx["rule_type"] == "fov"

    def test_declared_typed_fields_still_work(self):
        m = TrailerImageMetadata.model_validate(
            _base_meta(
                trigger="anomaly",
                timestamp_ms=1776894000000,
                context={
                    "max_anomaly_score": 3.2,
                    "max_count":         5,
                    "object_types":      ["person"],
                },
            )
        )
        # Declared fields stay typed on the instance.
        assert m.context.max_anomaly_score == 3.2
        assert m.context.max_count == 5
        assert m.context.object_types == ["person"]
