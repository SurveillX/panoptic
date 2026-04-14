"""
Verification orchestration for POST /v1/search/verify.

Flow:
  1. Run existing execute_search() with the verify request's search params.
  2. Select top-N per record type, dedup image/event so JPEGs aren't sent twice.
  3. Mint synthetic labels (sum_N, img_N, evt_N) for the VLM.
  4. Build multimodal prompt; attach selected JPEGs as data URIs.
  5. Call VLM with strict-JSON prompt.
  6. Parse response, translate labels back to real record IDs, drop unknowns.
  7. Return VerifyResponse. On any verification failure, return search results
     with verdict=insufficient_evidence and do not fail the HTTP request.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import cast

from pydantic import BaseModel, Field, ValidationError

from shared.clients.embedding import EmbeddingClient
from shared.clients.vlm import (
    VLMAuthError,
    VLMClient,
    VLMError,
    VLMNetworkError,
)

from .executor import execute_search
from .schemas import (
    EventHit,
    ImageHit,
    SearchRequest,
    SearchResults,
    SummaryHit,
    Verdict,
    Verification,
    VerifyRequest,
    VerifyResponse,
    VerifyTimingMs,
)
from .verify_prompt import SYSTEM_MESSAGE, build_user_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal model for VLM response validation
# ---------------------------------------------------------------------------

class _VerifyVLMOutput(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_summary_ids: list[str] = Field(default_factory=list)
    supporting_image_ids: list[str] = Field(default_factory=list)
    supporting_event_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    uncertainties: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_verification(
    req: VerifyRequest,
    engine,
    embedder: EmbeddingClient,
    vlm: VLMClient,
) -> VerifyResponse:
    t_total_start = time.perf_counter()
    timing = VerifyTimingMs()

    # ------------------------------------------------------------------
    # 1. Search
    # ------------------------------------------------------------------
    search_req = SearchRequest(
        query=req.query,
        record_types=list(req.record_types),
        filters=req.filters,
        top_k=req.search_top_k,
    )
    t_search = time.perf_counter()
    search_resp = execute_search(search_req, engine, embedder)
    timing.search = int((time.perf_counter() - t_search) * 1000)
    results = search_resp.results

    # ------------------------------------------------------------------
    # 2. Early exit — nothing to verify
    # ------------------------------------------------------------------
    if search_resp.total == 0:
        timing.total = int((time.perf_counter() - t_total_start) * 1000)
        return VerifyResponse(
            query=req.query,
            results=results,
            verification=Verification(
                verdict="insufficient_evidence",
                confidence=0.0,
                reason="No matching records found.",
            ),
            timing_ms=timing,
        )

    # ------------------------------------------------------------------
    # 3. Evidence selection + dedup + label minting
    # ------------------------------------------------------------------
    summary_items, image_items, event_items, image_uris = _select_evidence(
        results,
        req.verify_max_summaries,
        req.verify_max_images,
        req.verify_max_events,
    )

    label_to_summary_id = {label: s.id for label, s in summary_items}
    label_to_image_id = {label: i.id for label, i in image_items}
    label_to_event_id = {label: e.id for label, e in event_items}

    # ------------------------------------------------------------------
    # 4. Call VLM and parse
    # ------------------------------------------------------------------
    t_verify = time.perf_counter()
    verification = _call_vlm_and_parse(
        req.query,
        summary_items, image_items, event_items, image_uris,
        label_to_summary_id, label_to_image_id, label_to_event_id,
        vlm,
    )
    timing.verification = int((time.perf_counter() - t_verify) * 1000)
    timing.total = int((time.perf_counter() - t_total_start) * 1000)

    return VerifyResponse(
        query=req.query,
        results=results,
        verification=verification,
        timing_ms=timing,
    )


# ---------------------------------------------------------------------------
# Evidence selection
# ---------------------------------------------------------------------------

def _select_evidence(
    results: SearchResults,
    max_summaries: int,
    max_images: int,
    max_events: int,
) -> tuple[
    list[tuple[str, SummaryHit]],
    list[tuple[str, ImageHit]],
    list[tuple[str, EventHit]],
    list[str],
]:
    """
    Slice top-N per type and attach synthetic labels.

    Dedup rule: the JPEG for an image is attached only once. If an event hit
    shares its id (underlying image_id) with a selected image, the event is
    still kept as text context (labeled evt_N), but its JPEG is NOT re-sent.
    """
    summary_items: list[tuple[str, SummaryHit]] = [
        (f"sum_{i}", s) for i, s in enumerate(results.summaries[:max_summaries])
    ]

    selected_images = results.images[:max_images]
    selected_image_ids = {i.id for i in selected_images}

    image_items: list[tuple[str, ImageHit]] = [
        (f"img_{i}", img) for i, img in enumerate(selected_images)
    ]

    image_uris: list[str] = []
    for _, img in image_items:
        uri = _load_jpeg_data_uri(img.storage_path)
        if uri is not None:
            image_uris.append(uri)
        else:
            log.warning(
                "verify: image %s missing on disk (path=%s) — sending text-only",
                img.id, img.storage_path,
            )

    # Events: keep top-N. Event id IS the underlying image_id, so we can
    # test overlap directly. Events whose image already appears in image_items
    # are still included as text context (no extra JPEG).
    event_items: list[tuple[str, EventHit]] = [
        (f"evt_{i}", e) for i, e in enumerate(results.events[:max_events])
    ]

    # Note overlap in logs for visibility.
    overlap = [label for label, e in event_items if e.id in selected_image_ids]
    if overlap:
        log.debug("verify: event labels sharing image_id with selected images: %s", overlap)

    return summary_items, image_items, event_items, image_uris


def _load_jpeg_data_uri(storage_path: str | None) -> str | None:
    if not storage_path:
        return None
    try:
        with open(storage_path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, OSError) as exc:
        log.warning("verify: failed to read %s: %s", storage_path, exc)
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


# ---------------------------------------------------------------------------
# VLM invocation + response translation
# ---------------------------------------------------------------------------

def _call_vlm_and_parse(
    query: str,
    summary_items: list[tuple[str, SummaryHit]],
    image_items: list[tuple[str, ImageHit]],
    event_items: list[tuple[str, EventHit]],
    image_uris: list[str],
    label_to_summary_id: dict[str, str],
    label_to_image_id: dict[str, str],
    label_to_event_id: dict[str, str],
    vlm: VLMClient,
) -> Verification:
    prompt_text = build_user_prompt(query, summary_items, image_items, event_items)

    try:
        raw = vlm.call(
            prompt_text=prompt_text,
            frame_uris=image_uris,
            system_message=SYSTEM_MESSAGE,
        )
    except (VLMNetworkError, VLMAuthError, VLMError) as exc:
        log.warning("verify: VLM call failed: %s", exc)
        return Verification(
            verdict="insufficient_evidence",
            confidence=0.0,
            reason="Verification model unavailable.",
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("verify: VLM returned non-JSON: %s (raw=%s)", exc, raw[:200])
        return Verification(
            verdict="insufficient_evidence",
            confidence=0.0,
            reason="Verification response invalid.",
        )

    try:
        parsed = _VerifyVLMOutput.model_validate(payload)
    except ValidationError as exc:
        log.warning("verify: VLM JSON failed schema: %s", exc.error_count())
        return Verification(
            verdict="insufficient_evidence",
            confidence=0.0,
            reason="Verification response invalid.",
        )

    # Translate synthetic labels → real record IDs; drop unknowns; dedupe.
    return Verification(
        verdict=cast(Verdict, parsed.verdict),
        confidence=parsed.confidence,
        supporting_summary_ids=_translate_labels(parsed.supporting_summary_ids, label_to_summary_id),
        supporting_image_ids=_translate_labels(parsed.supporting_image_ids, label_to_image_id),
        supporting_event_ids=_translate_labels(parsed.supporting_event_ids, label_to_event_id),
        reason=parsed.reason[:280],
        uncertainties=[u[:200] for u in parsed.uncertainties[:5]],
    )


def _translate_labels(labels: list[str], label_map: dict[str, str]) -> list[str]:
    """Map synthetic labels → real record IDs. Drop unknown labels. Dedup preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        real = label_map.get(label)
        if real is None:
            log.debug("verify: dropping unknown label %s (map=%s)", label, list(label_map))
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out
