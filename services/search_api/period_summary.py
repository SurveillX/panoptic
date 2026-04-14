"""
Period summarization orchestration for POST /v1/summarize/period.

Flow:
  1. Parse + validate (Pydantic)
  2. Enumerate cameras (if omitted, distinct camera_id from panoptic_images in window)
  3. For each camera: run three Postgres SELECTs (summaries / images / events)
  4. Dedup near-duplicate images (same trigger within a 5-min window)
  5. Per-camera synthesis — one VLM call per camera with selected JPEGs
  6. Fusion — one VLM call over the per-camera JSON outputs
  7. Return PeriodSummarizeResponse

No persistence. No worker queue. All retrieval is structured SQL;
no Qdrant involvement.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text as sa_text

from shared.clients.vlm import (
    VLMAuthError,
    VLMClient,
    VLMError,
    VLMNetworkError,
)

from .period_summary_prompt import (
    FUSION_SYSTEM_MESSAGE,
    PER_CAMERA_SYSTEM_MESSAGE,
    build_fusion_user_prompt,
    build_per_camera_user_prompt,
)
from .schemas import (
    CameraSummary,
    OverallSummary,
    PeriodScope,
    PeriodSummarizeRequest,
    PeriodSummarizeResponse,
    PeriodTimingMs,
    SummaryType,
    TimeRange,
)

log = logging.getLogger(__name__)

# Images with the same trigger captured within this window are treated
# as near-duplicates — keep only the first.
_IMAGE_DEDUP_WINDOW = timedelta(minutes=5)


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


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_period_summary(
    req: PeriodSummarizeRequest,
    engine,
    vlm: VLMClient,
) -> PeriodSummarizeResponse:
    t_total_start = time.perf_counter()
    timing = PeriodTimingMs()

    # ------------------------------------------------------------------
    # Camera enumeration (no DB trip if caller provided the list)
    # ------------------------------------------------------------------
    t_retrieve = time.perf_counter()
    if req.scope.camera_ids:
        camera_ids = list(req.scope.camera_ids)
    else:
        camera_ids = _list_cameras(engine, req.scope.serial_number, req.time_range)

    # ------------------------------------------------------------------
    # Per-camera retrieval
    # ------------------------------------------------------------------
    per_camera_evidence: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
    for cam in camera_ids:
        summaries = _fetch_summaries(
            engine, req.scope.serial_number, cam, req.time_range,
            req.max_input_summaries_per_camera,
        )
        images_raw = _fetch_images(
            engine, req.scope.serial_number, cam, req.time_range,
            req.max_input_images_per_camera * 3,
        )
        images = _dedup_images(images_raw)[: req.max_input_images_per_camera]
        events = _fetch_events(
            engine, req.scope.serial_number, cam, req.time_range,
            req.max_input_events_per_camera,
        )
        if summaries or images or events:
            per_camera_evidence[cam] = (summaries, images, events)
    timing.retrieve = int((time.perf_counter() - t_retrieve) * 1000)

    # ------------------------------------------------------------------
    # No-evidence early exit
    # ------------------------------------------------------------------
    if not per_camera_evidence:
        timing.total = int((time.perf_counter() - t_total_start) * 1000)
        return PeriodSummarizeResponse(
            scope=req.scope,
            time_range=req.time_range,
            camera_summaries=[],
            overall=OverallSummary(
                headline="No evidence found in the requested period.",
                summary=(
                    "No summaries, images, or alert/anomaly events were recorded "
                    "for the requested trailer and camera set within the given time range."
                ),
                supporting_camera_ids=[],
                confidence=0.0,
            ),
            timing_ms=timing,
        )

    # ------------------------------------------------------------------
    # Per-camera synthesis
    # ------------------------------------------------------------------
    t_camera = time.perf_counter()
    camera_summaries: list[CameraSummary] = []
    for cam, (summaries, images, events) in per_camera_evidence.items():
        cs = _synthesize_camera_summary(
            serial_number=req.scope.serial_number,
            camera_id=cam,
            time_range=req.time_range,
            summary_type=req.summary_type,
            summaries=summaries,
            images=images,
            events=events,
            vlm=vlm,
        )
        if cs is not None:
            camera_summaries.append(cs)
    timing.camera_synthesis = int((time.perf_counter() - t_camera) * 1000)

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------
    t_fuse = time.perf_counter()
    overall = _fuse(
        serial_number=req.scope.serial_number,
        time_range=req.time_range,
        summary_type=req.summary_type,
        camera_summaries=camera_summaries,
        vlm=vlm,
    )
    timing.fusion = int((time.perf_counter() - t_fuse) * 1000)

    timing.total = int((time.perf_counter() - t_total_start) * 1000)
    return PeriodSummarizeResponse(
        scope=PeriodScope(
            serial_number=req.scope.serial_number,
            camera_ids=[cs.camera_id for cs in camera_summaries] or req.scope.camera_ids,
        ),
        time_range=req.time_range,
        camera_summaries=camera_summaries,
        overall=overall,
        timing_ms=timing,
    )


# ---------------------------------------------------------------------------
# Retrieval (SQL)
# ---------------------------------------------------------------------------

def _list_cameras(engine, serial_number: str, tr: TimeRange) -> list[str]:
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


def _fetch_summaries(
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
        labels = _normalize_event_labels(row.get("key_events"))
        d["key_events_labels"] = labels
        d["start_time"] = _iso(row.get("start_time"))
        d["end_time"] = _iso(row.get("end_time"))
        out.append(d)
    return out


def _fetch_images(
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
        d["captured_at"] = _iso(row.get("captured_at_utc"))
        d["bucket_start"] = _iso(row.get("bucket_start_utc"))
        out.append(d)
    return out


def _fetch_events(
    engine, serial_number: str, camera_id: str, tr: TimeRange, limit: int,
) -> list[dict]:
    if limit <= 0:
        return []
    sql = sa_text("""
        SELECT image_id, serial_number, camera_id, scope_id, trigger,
               captured_at_utc, bucket_start_utc, caption_text
          FROM panoptic_images
         WHERE serial_number = :sn
           AND camera_id     = :cam
           AND trigger IN ('alert','anomaly')
           AND bucket_start_utc >= :tstart
           AND bucket_start_utc <  :tend
         ORDER BY bucket_start_utc DESC
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
        d["captured_at"] = _iso(row.get("captured_at_utc"))
        d["bucket_start"] = _iso(row.get("bucket_start_utc"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Image dedup
# ---------------------------------------------------------------------------

def _dedup_images(images: list[dict]) -> list[dict]:
    """Drop images that share trigger + 5-minute cluster with an earlier kept one."""
    if len(images) <= 1:
        return images
    # Order by time ASC so "first" in a cluster is the earliest.
    ordered = sorted(
        images,
        key=lambda d: _parse_iso(d.get("bucket_start")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    kept: list[dict] = []
    last_by_trigger: dict[str, datetime] = {}
    for img in ordered:
        trg = img.get("trigger") or ""
        ts = _parse_iso(img.get("bucket_start"))
        if ts is None:
            kept.append(img)
            continue
        prev = last_by_trigger.get(trg)
        if prev is not None and (ts - prev) < _IMAGE_DEDUP_WINDOW:
            continue
        kept.append(img)
        last_by_trigger[trg] = ts
    # Re-sort kept by trigger priority then time DESC to match the original intent.
    priority = {"alert": 3, "anomaly": 2, "baseline": 1}
    kept.sort(
        key=lambda d: (
            -priority.get(d.get("trigger") or "", 0),
            -_epoch(d.get("bucket_start")),
        )
    )
    return kept


# ---------------------------------------------------------------------------
# Per-camera synthesis
# ---------------------------------------------------------------------------

def _synthesize_camera_summary(
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
    label_to_event_id = {label: ev["image_id"] for label, ev in event_items}

    # Load JPEGs in the same order as image_items.
    frame_uris: list[str] = []
    for _, im in image_items:
        uri = _load_jpeg_data_uri(im.get("storage_path"))
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
        time_range_start=_iso(time_range.start) or "",
        time_range_end=_iso(time_range.end) or "",
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
        supporting_summary_ids=_translate_labels(parsed.supporting_summary_ids, label_to_summary_id),
        supporting_image_ids=_translate_labels(parsed.supporting_image_ids, label_to_image_id),
        supporting_event_ids=_translate_labels(parsed.supporting_event_ids, label_to_event_id),
        confidence=parsed.confidence,
    )


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def _fuse(
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
        time_range_start=_iso(time_range.start) or "",
        time_range_end=_iso(time_range.end) or "",
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
        return _degraded_overall(known_camera_ids)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("period: fusion non-JSON: %s (raw=%s)", exc, raw[:200])
        return _degraded_overall(known_camera_ids)

    try:
        parsed = _FusionVLMOutput.model_validate(payload)
    except ValidationError as exc:
        log.warning("period: fusion JSON failed schema: %s", exc.error_count())
        return _degraded_overall(known_camera_ids)

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


def _degraded_overall(known_camera_ids: set[str]) -> OverallSummary:
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
# Small utilities
# ---------------------------------------------------------------------------

def _translate_labels(labels: list[str], label_map: dict[str, str]) -> list[str]:
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


def _normalize_event_labels(key_events) -> list[str]:
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


def _load_jpeg_data_uri(storage_path: str | None) -> str | None:
    if not storage_path:
        return None
    try:
        with open(storage_path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, OSError) as exc:
        log.warning("period: failed to read %s: %s", storage_path, exc)
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _epoch(value: str | None) -> float:
    dt = _parse_iso(value)
    return dt.timestamp() if dt is not None else 0.0


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
