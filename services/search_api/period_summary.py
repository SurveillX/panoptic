"""
Period summarization orchestration for POST /v1/summarize/period.

Thin HTTP adapter over shared/report/synthesis — all retrieval, dedup,
VLM synthesis, and fusion logic lives there and is shared with the M9
report_generate worker. This module handles only request unpacking,
camera enumeration, and response shaping.

Flow:
  1. Parse + validate (Pydantic)
  2. Enumerate cameras (if omitted, distinct camera_id from panoptic_images in window)
  3. For each camera: fetch summaries/images/events, dedup, synthesize (via shared)
  4. Fuse (via shared)
  5. Return PeriodSummarizeResponse

No persistence. No worker queue. All retrieval is structured SQL;
no Qdrant involvement.
"""

from __future__ import annotations

import logging
import time

from shared.clients.vlm import VLMClient
from shared.report.synthesis import (
    dedup_images,
    fetch_events,
    fetch_images,
    fetch_summaries,
    fuse,
    list_cameras_in_window,
    synthesize_camera_summary,
)

from .schemas import (
    CameraSummary,
    OverallSummary,
    PeriodScope,
    PeriodSummarizeRequest,
    PeriodSummarizeResponse,
    PeriodTimingMs,
)

log = logging.getLogger(__name__)


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
        camera_ids = list_cameras_in_window(engine, req.scope.serial_number, req.time_range)

    # ------------------------------------------------------------------
    # Per-camera retrieval
    # ------------------------------------------------------------------
    per_camera_evidence: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
    for cam in camera_ids:
        summaries = fetch_summaries(
            engine, req.scope.serial_number, cam, req.time_range,
            req.max_input_summaries_per_camera,
        )
        images_raw = fetch_images(
            engine, req.scope.serial_number, cam, req.time_range,
            req.max_input_images_per_camera * 3,
        )
        images = dedup_images(images_raw)[: req.max_input_images_per_camera]
        events = fetch_events(
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
                    "No summaries, images, or events were recorded "
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
        cs = synthesize_camera_summary(
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
    overall = fuse(
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
