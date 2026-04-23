"""
Tests for shared.events.build — image-trigger event row construction.

M13 adds `novelty → scene_change` to the trigger→event_type mapping
and introduces a similarity-based severity for novelty.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.events.build import build_event_row_from_image, generate_event_id
from shared.schemas.event import (
    EVENT_TYPE_ALERT_CREATED,
    EVENT_TYPE_ANOMALY_DETECTED,
    EVENT_TYPE_SCENE_CHANGE,
)


UTC = timezone.utc


def _image_row(**overrides) -> dict:
    base = {
        "image_id":          "img-test-1",
        "serial_number":     "TEST",
        "camera_id":         "test-cam",
        "scope_id":          "TEST:test-cam",
        "trigger":           "alert",
        "bucket_start_utc":  datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        "bucket_end_utc":    datetime(2026, 4, 23, 12, 15, tzinfo=UTC),
        "captured_at_utc":   datetime(2026, 4, 23, 12, 5, tzinfo=UTC),
        "caption_text":      None,
        "context_json":      {},
    }
    base.update(overrides)
    return base


class TestNoveltyEventBuild:
    def test_novelty_produces_scene_change_event(self):
        row = build_event_row_from_image(_image_row(
            trigger="novelty",
            context_json={"similarity": 0.25, "sample_id": 3, "reason": "novel_scene"},
        ))
        assert row["event_type"] == EVENT_TYPE_SCENE_CHANGE
        assert row["event_source"] == "image_trigger"
        assert row["title"] == "Scene change"

    def test_novelty_severity_from_similarity(self):
        # 1 - 0.25 = 0.75
        row = build_event_row_from_image(_image_row(
            trigger="novelty",
            context_json={"similarity": 0.25},
        ))
        assert row["severity"] == pytest.approx(0.75)
        assert row["confidence"] == pytest.approx(0.75)

    def test_novelty_severity_saturation(self):
        # Very high similarity → near-zero severity.
        row = build_event_row_from_image(_image_row(
            trigger="novelty",
            context_json={"similarity": 0.99},
        ))
        assert row["severity"] == pytest.approx(0.01, abs=1e-6)

        # Very low similarity → saturated at 1.0.
        row = build_event_row_from_image(_image_row(
            trigger="novelty",
            context_json={"similarity": 0.0},
        ))
        assert row["severity"] == pytest.approx(1.0)

    def test_novelty_without_similarity_gets_none_severity(self):
        row = build_event_row_from_image(_image_row(
            trigger="novelty",
            context_json={"sample_id": 1, "reason": "novel_scene"},
        ))
        assert row["severity"] is None
        assert row["confidence"] is None

    def test_novelty_event_id_deterministic(self):
        row_a = build_event_row_from_image(_image_row(trigger="novelty"))
        row_b = build_event_row_from_image(_image_row(trigger="novelty"))
        assert row_a["event_id"] == row_b["event_id"]
        # Same event_id shape as alert/anomaly — content-addressed on
        # (event_source, image_id). trigger doesn't enter the hash.
        assert row_a["event_id"] == generate_event_id(
            event_source="image_trigger", image_id="img-test-1"
        )


class TestExistingTriggersUnchanged:
    def test_alert_still_maps_to_alert_created(self):
        row = build_event_row_from_image(_image_row(
            trigger="alert",
            context_json={"max_anomaly_score": 0.88, "rule_id": "rule-42"},
        ))
        assert row["event_type"] == EVENT_TYPE_ALERT_CREATED
        assert row["severity"] == pytest.approx(0.88)

    def test_anomaly_still_maps_to_anomaly_detected(self):
        row = build_event_row_from_image(_image_row(
            trigger="anomaly",
            context_json={"max_anomaly_score": 3.2},
        ))
        # 3.2 clamps to 1.0 via _clamp_unit.
        assert row["event_type"] == EVENT_TYPE_ANOMALY_DETECTED
        assert row["severity"] == pytest.approx(1.0)

    def test_baseline_still_raises(self):
        # baseline is still explicitly excluded — the selector sends
        # baseline images for reference, not as event-worthy evidence.
        with pytest.raises(ValueError, match="baseline"):
            build_event_row_from_image(_image_row(trigger="baseline"))

    def test_unknown_trigger_raises(self):
        with pytest.raises(ValueError):
            build_event_row_from_image(_image_row(trigger="foo"))
