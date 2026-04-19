"""
Shared synthesis helpers for period summaries and M9 reports.

Lifted verbatim from services/search_api/period_summary.py in M9 P9.1 so
that the same logic can be called from both the synchronous
/v1/summarize/period HTTP path and the async report_generate worker
without duplication.

Zero-behavior-change contract: moving these functions MUST NOT change
/v1/summarize/period responses on a fixed fixture. Validated by the P9.1
gate before merging the lift commit.

Architectural note: this module imports a handful of Pydantic types
(TimeRange, CameraSummary, OverallSummary, SummaryType) from
`services.search_api.schemas` — a reverse import from `services → shared`.
It's deliberate for v1: moving those types into `shared/schemas/search.py`
is a larger restructure that doesn't belong in the M9 lift. Flag for
later consolidation if the reverse import becomes a constraint.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text as sa_text

from shared.clients.vlm import VLMAuthError, VLMClient, VLMError, VLMNetworkError
from shared.report.prompts import (
    FUSION_SYSTEM_MESSAGE,
    PER_CAMERA_SYSTEM_MESSAGE,
    WEEKLY_SYSTEM_MESSAGE,
    build_fusion_user_prompt,
    build_per_camera_user_prompt,
    build_weekly_user_prompt,
)

# Reverse import, see module docstring for context.
from services.search_api.schemas import (
    CameraSummary,
    OverallSummary,
    SummaryType,
    TimeRange,
)

log = logging.getLogger(__name__)

# Images with the same trigger captured within this window are treated
# as near-duplicates — keep only the first.
IMAGE_DEDUP_WINDOW = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Internal VLM response models
# ---------------------------------------------------------------------------


class _PerCameraVLMOutput(BaseModel):
    headline: str
    summary: str
    supporting_summary_ids: list[str] = Field(default_factory=list)
    supporting_image_ids: list[str] = Field(default_factory=list)
    supporting_event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class _FusionVLMOutput(BaseModel):
    headline: str
    summary: str
    supporting_camera_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class _WeeklyVLMOutput(BaseModel):
    headline: str
    summary: str
    supporting_day_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Retrieval (SQL)
# ---------------------------------------------------------------------------


def list_cameras_in_window(engine, serial_number: str, tr: TimeRange) -> list[str]:
    sql = sa_text("""
        SELECT DISTINCT camera_id
          FROM panoptic_images
         WHERE serial_number = :sn
           AND bucket_start_utc >= :tstart
           AND bucket_start_utc <  :tend
         ORDER BY camera_id
    """)
    with engine.connect() as conn:
        return [row.camera_id for row in conn.execute(
            sql, {"sn": serial_number, "tstart": tr.start, "tend": tr.end}
        )]


def fetch_summaries(
    engine, serial_number: str, camera_id: str, tr: TimeRange, limit: int,
) -> list[dict]:
    if limit <= 0:
        return []
    scope_id = f"{serial_number}:{camera_id}"
    sql = sa_text("""
        SELECT summary_id, serial_number, level, scope_id,
               start_time, end_time, summary, key_events, confidence
          FROM panoptic_summaries
         WHERE serial_number = :sn
           AND level IN ('camera','hour')
           AND scope_id = :scope_id
           AND is_latest = true
           AND start_time >= :tstart
           AND start_time <  :tend
         ORDER BY confidence DESC, start_time DESC
         LIMIT :limit
    """)
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "sn": serial_number, "scope_id": scope_id,
                "tstart": tr.start, "tend": tr.end, "limit": limit,
            },
        ).mappings().all()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        labels = normalize_event_labels(row.get("key_events"))
        d["key_events_labels"] = labels
        d["start_time"] = iso(row.get("start_time"))
        d["end_time"] = iso(row.get("end_time"))
        out.append(d)
    return out


def fetch_images(
    engine, serial_number: str, camera_id: str, tr: TimeRange, limit: int,
) -> list[dict]:
    if limit <= 0:
        return []
    # Trigger priority: alert=3, anomaly=2, baseline=1
    sql = sa_text("""
        SELECT image_id, serial_number, camera_id, scope_id, trigger,
               captured_at_utc, bucket_start_utc, caption_text, storage_path
          FROM panoptic_images
         WHERE serial_number = :sn
           AND camera_id     = :cam
           AND bucket_start_utc >= :tstart
           AND bucket_start_utc <  :tend
         ORDER BY
           CASE trigger WHEN 'alert' THEN 3
                        WHEN 'anomaly' THEN 2
                        WHEN 'baseline' THEN 1
                        ELSE 0 END DESC,
           bucket_start_utc DESC
         LIMIT :limit
    """)
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "sn": serial_number, "cam": camera_id,
                "tstart": tr.start, "tend": tr.end, "limit": limit,
            },
        ).mappings().all()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["captured_at"] = iso(row.get("captured_at_utc"))
        d["bucket_start"] = iso(row.get("bucket_start_utc"))
        out.append(d)
    return out


def fetch_events(
    engine, serial_number: str, camera_id: str, tr: TimeRange, limit: int,
) -> list[dict]:
    """Fetch panoptic_events rows (both image_trigger and bucket_marker) for
    the camera in window. Post-M8 — the event layer is the authoritative
    source for 'what happened here.'"""
    if limit <= 0:
        return []
    sql = sa_text("""
        SELECT event_id, event_type, event_source,
               serial_number, camera_id, scope_id,
               severity, confidence,
               start_time_utc, end_time_utc, event_time_utc,
               bucket_id, image_id,
               title, description
          FROM panoptic_events
         WHERE serial_number = :sn
           AND camera_id     = :cam
           AND event_time_utc >= :tstart
           AND event_time_utc <  :tend
         ORDER BY event_time_utc DESC
         LIMIT :limit
    """)
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "sn": serial_number, "cam": camera_id,
                "tstart": tr.start, "tend": tr.end, "limit": limit,
            },
        ).mappings().all()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["event_time_utc"] = iso(row.get("event_time_utc"))
        d["start_time_utc"] = iso(row.get("start_time_utc"))
        d["end_time_utc"] = iso(row.get("end_time_utc"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Image dedup
# ---------------------------------------------------------------------------


def dedup_images(images: list[dict]) -> list[dict]:
    """Drop images that share trigger + 5-minute cluster with an earlier kept one."""
    if len(images) <= 1:
        return images
    # Order by time ASC so "first" in a cluster is the earliest.
    ordered = sorted(
        images,
        key=lambda d: parse_iso(d.get("bucket_start")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    kept: list[dict] = []
    last_by_trigger: dict[str, datetime] = {}
    for img in ordered:
        trg = img.get("trigger") or ""
        ts = parse_iso(img.get("bucket_start"))
        if ts is None:
            kept.append(img)
            continue
        prev = last_by_trigger.get(trg)
        if prev is not None and (ts - prev) < IMAGE_DEDUP_WINDOW:
            continue
        kept.append(img)
        last_by_trigger[trg] = ts
    # Re-sort kept by trigger priority then time DESC to match the original intent.
    priority = {"alert": 3, "anomaly": 2, "baseline": 1}
    kept.sort(
        key=lambda d: (
            -priority.get(d.get("trigger") or "", 0),
            -epoch(d.get("bucket_start")),
        )
    )
    return kept


# ---------------------------------------------------------------------------
# Per-camera synthesis
# ---------------------------------------------------------------------------


def synthesize_camera_summary(
    *,
    serial_number: str,
    camera_id: str,
    time_range: TimeRange,
    summary_type: SummaryType,
    summaries: list[dict],
    images: list[dict],
    events: list[dict],
    vlm: VLMClient,
) -> CameraSummary | None:
    summary_items = [(f"sum_{i}", s) for i, s in enumerate(summaries)]
    image_items = [(f"img_{i}", im) for i, im in enumerate(images)]
    event_items = [(f"evt_{i}", ev) for i, ev in enumerate(events)]

    label_to_summary_id = {label: s["summary_id"] for label, s in summary_items}
    label_to_image_id = {label: im["image_id"] for label, im in image_items}
    label_to_event_id = {label: ev["event_id"] for label, ev in event_items}

    # Load JPEGs in the same order as image_items.
    frame_uris: list[str] = []
    for _, im in image_items:
        uri = load_jpeg_data_uri(im.get("storage_path"))
        if uri is not None:
            frame_uris.append(uri)
        else:
            log.warning(
                "period: image %s missing on disk (path=%s) — text-only",
                im.get("image_id"), im.get("storage_path"),
            )

    prompt_text = build_per_camera_user_prompt(
        serial_number=serial_number,
        camera_id=camera_id,
        time_range_start=iso(time_range.start) or "",
        time_range_end=iso(time_range.end) or "",
        summary_type=summary_type,
        summary_items=summary_items,
        image_items=image_items,
        event_items=event_items,
    )

    try:
        raw = vlm.call(
            prompt_text=prompt_text,
            frame_uris=frame_uris,
            system_message=PER_CAMERA_SYSTEM_MESSAGE,
        )
    except (VLMNetworkError, VLMAuthError, VLMError) as exc:
        log.warning("period: VLM call failed for camera=%s: %s", camera_id, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "period: VLM non-JSON for camera=%s: %s (raw=%s)",
            camera_id, exc, raw[:200],
        )
        return None

    try:
        parsed = _PerCameraVLMOutput.model_validate(payload)
    except ValidationError as exc:
        log.warning("period: VLM JSON failed schema for camera=%s: %s", camera_id, exc.error_count())
        return None

    return CameraSummary(
        camera_id=camera_id,
        headline=parsed.headline[:140],
        summary=parsed.summary[:600],
        supporting_summary_ids=translate_labels(parsed.supporting_summary_ids, label_to_summary_id),
        supporting_image_ids=translate_labels(parsed.supporting_image_ids, label_to_image_id),
        supporting_event_ids=translate_labels(parsed.supporting_event_ids, label_to_event_id),
        confidence=parsed.confidence,
    )


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def fuse(
    *,
    serial_number: str,
    time_range: TimeRange,
    summary_type: SummaryType,
    camera_summaries: list[CameraSummary],
    vlm: VLMClient,
) -> OverallSummary:
    known_camera_ids = {cs.camera_id for cs in camera_summaries}

    if not camera_summaries:
        return OverallSummary(
            headline="Unable to generate overall summary.",
            summary="No per-camera summaries were produced, so no fusion was performed.",
            supporting_camera_ids=[],
            confidence=0.0,
        )

    prompt_text = build_fusion_user_prompt(
        serial_number=serial_number,
        time_range_start=iso(time_range.start) or "",
        time_range_end=iso(time_range.end) or "",
        summary_type=summary_type,
        camera_summaries=camera_summaries,
    )

    try:
        raw = vlm.call(
            prompt_text=prompt_text,
            frame_uris=[],
            system_message=FUSION_SYSTEM_MESSAGE,
        )
    except (VLMNetworkError, VLMAuthError, VLMError) as exc:
        log.warning("period: fusion VLM call failed: %s", exc)
        return degraded_overall(known_camera_ids)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("period: fusion non-JSON: %s (raw=%s)", exc, raw[:200])
        return degraded_overall(known_camera_ids)

    try:
        parsed = _FusionVLMOutput.model_validate(payload)
    except ValidationError as exc:
        log.warning("period: fusion JSON failed schema: %s", exc.error_count())
        return degraded_overall(known_camera_ids)

    # Keep only camera IDs that were actually in the provided set.
    supporting = [c for c in parsed.supporting_camera_ids if c in known_camera_ids]
    # Dedupe preserving order.
    seen: set[str] = set()
    supporting_unique: list[str] = []
    for c in supporting:
        if c not in seen:
            seen.add(c)
            supporting_unique.append(c)

    return OverallSummary(
        headline=parsed.headline[:160],
        summary=parsed.summary[:900],
        supporting_camera_ids=supporting_unique,
        confidence=parsed.confidence,
    )


def degraded_overall(known_camera_ids: set[str]) -> OverallSummary:
    return OverallSummary(
        headline="Unable to generate overall summary.",
        summary=(
            "Per-camera summaries were produced, but fusion failed. "
            "See individual camera_summaries for details."
        ),
        supporting_camera_ids=sorted(known_camera_ids),
        confidence=0.0,
    )


# ---------------------------------------------------------------------------
# Weekly synthesis (M9 P9.4)
# ---------------------------------------------------------------------------


def synthesize_weekly(
    *,
    serial_number: str,
    window_start_iso: str,
    window_end_iso: str,
    day_entries: list[dict],
    aggregates: dict,
    vlm: VLMClient,
) -> dict:
    """
    Run one VLM call rolling up the 7 per-day summaries into a weekly
    overall. Returns a dict with headline/summary/confidence/supporting_day_ids.

    Degrades gracefully: returns a placeholder with confidence=0 when the
    VLM fails OR when no per-day input is available.
    """
    known_day_keys = {d["day_key"] for d in day_entries}

    if not day_entries:
        return {
            "headline": "No per-day summaries available for this week.",
            "summary": (
                "The weekly report could not be assembled because no daily "
                "reports had successfully generated for this trailer in the "
                "requested window. Aggregation counts above still reflect "
                "raw data."
            ),
            "supporting_day_ids": [],
            "confidence": 0.0,
        }

    prompt_text = build_weekly_user_prompt(
        serial_number=serial_number,
        window_start=window_start_iso,
        window_end=window_end_iso,
        day_entries=day_entries,
        aggregates=aggregates,
    )

    try:
        raw = vlm.call(
            prompt_text=prompt_text,
            frame_uris=[],
            system_message=WEEKLY_SYSTEM_MESSAGE,
        )
    except (VLMNetworkError, VLMAuthError, VLMError) as exc:
        log.warning("weekly: VLM call failed: %s", exc)
        return _degraded_weekly(known_day_keys)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("weekly: non-JSON VLM response: %s (raw=%s)", exc, raw[:200])
        return _degraded_weekly(known_day_keys)

    try:
        parsed = _WeeklyVLMOutput.model_validate(payload)
    except ValidationError as exc:
        log.warning("weekly: VLM JSON failed schema: %s", exc.error_count())
        return _degraded_weekly(known_day_keys)

    supporting = [d for d in parsed.supporting_day_ids if d in known_day_keys]
    seen: set[str] = set()
    supporting_unique: list[str] = []
    for d in supporting:
        if d in seen:
            continue
        seen.add(d)
        supporting_unique.append(d)

    return {
        "headline": parsed.headline[:180],
        "summary":  parsed.summary[:1200],
        "supporting_day_ids": supporting_unique,
        "confidence": parsed.confidence,
    }


def _degraded_weekly(known_day_keys: set[str]) -> dict:
    return {
        "headline": "Unable to generate weekly summary.",
        "summary": (
            "Per-day summaries were available, but the weekly fusion VLM call "
            "failed. Aggregation counts are still shown above."
        ),
        "supporting_day_ids": sorted(known_day_keys),
        "confidence": 0.0,
    }


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def translate_labels(labels: list[str], label_map: dict[str, str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        real = label_map.get(label)
        if real is None:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def normalize_event_labels(key_events) -> list[str]:
    """Extract label strings from panoptic_summaries.key_events JSONB array."""
    if not key_events:
        return []
    out: list[str] = []
    for e in key_events:
        if isinstance(e, dict):
            lbl = e.get("label")
            if lbl:
                out.append(str(lbl))
        elif isinstance(e, str):
            out.append(e)
    return out


def load_jpeg_data_uri(storage_path: str | None) -> str | None:
    if not storage_path:
        return None
    try:
        with open(storage_path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, OSError) as exc:
        log.warning("period: failed to read %s: %s", storage_path, exc)
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def epoch(value: str | None) -> float:
    dt = parse_iso(value)
    return dt.timestamp() if dt is not None else 0.0


def iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
