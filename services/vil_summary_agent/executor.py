"""
bucket_summary job executor — Step 7 (real frame fetching + real vLLM).

Frame fetching via KeyframeClient and LLM calls via VLMClient are both live.
call_llm_stub is retained as a fallback when vlm_client=None (CI / no vLLM).

_call_and_validate two-attempt loop:
  Attempt 1: real prompt + frame URIs → parse JSON → validate schema.
  Attempt 2 (validation failure only): repair prompt, no images → validate.
  Second failure → degraded output with confidence=0.0.
  Infrastructure errors (VLMNetworkError, VLMAuthError, VLMError) propagate
  to the worker unchanged; they are not caught here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import text

from shared.clients.continuum import ContinuumClient, ContinuumFrameResponse, ContinuumNetworkError
from shared.clients.vlm import VLMClient, VLMAuthError, VLMError, VLMNetworkError

from services.vil_summary_agent.summary_db import (
    insert_embedding_job,
    upsert_rollup_state_and_maybe_enqueue,
    upsert_summary,
)
from shared.clients.keyframe import (
    FrameResponse,
    KeyframeAuthError,
    KeyframeNetworkError,
    is_usable_frame,
)
from shared.schemas.llm import LLMSummaryOutput
from shared.schemas.summary import SummaryCoverage, SummaryRecord, generate_summary_id
from shared.utils.hashing import compute_child_set_hash

log = logging.getLogger(__name__)

_PROMPT_PATH  = Path(__file__).parent.parent.parent / "shared" / "prompts" / "bucket_summary_v1.txt"
_REPAIR_PATH  = Path(__file__).parent.parent.parent / "shared" / "prompts" / "repair_v1.txt"

_TARGET_FRAMES = 3        # one per keyframe_candidates slot (baseline, peak, change)
_FRAME_TOLERANCE_SEC = 5  # passed to KeyframeClient tolerance_sec


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionResult:
    job_state: Literal["succeeded", "degraded", "failed_terminal", "retry_wait"]
    last_error: str | None
    summary_id: str              # empty string on failed_terminal / retry_wait
    embedding_job_id: str | None  # new UUID; None if duplicate or not applicable
    rollup_job_id: str | None     # new UUID; None if not triggered or duplicate
    rollup_serial_number: str | None  # mirrors serial_number when rollup_job_id is set


# ---------------------------------------------------------------------------
# LLM stub — REPLACE in Step 7
# ---------------------------------------------------------------------------

def call_llm_stub(prompt: str, bucket_row, frames_used: int) -> dict:
    """
    Fallback used when vlm_client=None (CI / no vLLM server configured).

    Returns a raw dict in the same shape as a real vLLM JSON response so that
    _call_and_validate can validate it identically to a real response.
    """
    total = sum((bucket_row.object_counts or {}).values())
    mode = f"{frames_used} frame(s)" if frames_used > 0 else "metadata only"
    return {
        "summary": (
            f"Stub summary ({mode}). "
            f"{total} total detections recorded during this period."
        ),
        "key_events": [],
        "confidence": 0.6 if frames_used > 0 else 0.3,
    }


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

def run_bucket_summary(
    conn,
    payload: dict,
    worker_id: str,
    attempt_count: int,
    keyframe_client=None,
    vlm_client: VLMClient | None = None,
    continuum_client: ContinuumClient | None = None,
) -> ExecutionResult:
    """
    Execute a bucket_summary job.

    All DB writes occur within the caller's open connection/transaction.
    No commit is issued here; the worker commits after release_job + lease check.

    Frame source routing (decided at worker startup, not per-job):
      continuum_client is not None → Continuum path (JPEG bytes → base64 data URI)
      keyframe_client is not None  → KeyframeClient path (URI references)
      both None                    → no frames, metadata_only

    Parameters
    ----------
    conn             : open SQLAlchemy connection (transaction in progress)
    payload          : vil_jobs.payload dict
    worker_id        : worker identity (for logging)
    attempt_count    : current attempt number from ClaimResult
    keyframe_client  : KeyframeClient | None
    vlm_client       : VLMClient | None — None falls back to call_llm_stub
    continuum_client : ContinuumClient | None — takes precedence over keyframe_client

    Returns an ExecutionResult describing what happened and which post-commit
    enqueues the worker should perform.
    """
    bucket_id     = payload["bucket_id"]
    serial_number     = payload["serial_number"]
    camera_id     = payload["camera_id"]
    model_profile = payload["model_profile"]
    prompt_version = payload["prompt_version"]

    # ------------------------------------------------------------------
    # Step 2: Load bucket
    # ------------------------------------------------------------------
    bucket_row = conn.execute(
        text("SELECT * FROM vil_buckets WHERE bucket_id = :bucket_id"),
        {"bucket_id": bucket_id},
    ).fetchone()

    if bucket_row is None:
        log.error(
            "run_bucket_summary: bucket_id=%s not found in vil_buckets", bucket_id
        )
        return ExecutionResult(
            job_state="failed_terminal",
            last_error=f"bucket not found: {bucket_id}",
            summary_id="",
            embedding_job_id=None,
            rollup_job_id=None,
            rollup_serial_number=None,
        )

    # ------------------------------------------------------------------
    # Steps 3–7: Resolve candidates, fetch frames, apply quality filter
    #
    # Frame source routing:
    #   continuum_client set → Continuum path (base64 data URIs)
    #   keyframe_client set  → KeyframeClient path (URI references)
    #   both None            → no frames
    # ------------------------------------------------------------------
    candidates = _resolve_candidates(bucket_row)
    usable_frames: list[FrameResponse] = []
    continuum_frames: list[ContinuumFrameResponse] = []

    if continuum_client is not None and candidates:
        continuum_frames = _fetch_continuum_frames(
            continuum_client, serial_number, camera_id, candidates
        )
    elif keyframe_client is not None and candidates:
        usable_frames = _fetch_usable_frames(keyframe_client, camera_id, candidates)

    frames_used = len(continuum_frames) or len(usable_frames)
    summary_mode = _decide_summary_mode(frames_used)

    # Collect timestamps of frames actually fetched (for later retrieval)
    if continuum_frames:
        frame_timestamps = [f.requested_ts.isoformat() for f in continuum_frames]
    elif usable_frames:
        frame_timestamps = [f.actual_ts.isoformat() for f in usable_frames]
    else:
        frame_timestamps = []

    # ------------------------------------------------------------------
    # Step 8: Build prompt
    # ------------------------------------------------------------------
    if continuum_frames:
        prompt = _build_prompt(bucket_row, [])  # no FrameResponse objects
        frame_uris = [f.data_uri for f in continuum_frames]
    else:
        prompt = _build_prompt(bucket_row, usable_frames)
        frame_uris = [f.uri for f in usable_frames]

    # ------------------------------------------------------------------
    # Steps 9–10: Call vLLM (or stub) + validate; repair on first failure
    # ------------------------------------------------------------------
    llm_output = _call_and_validate(prompt, bucket_row, frames_used, vlm_client, frame_uris)

    # Deterministically inject confirmed event signals into key_events and summary
    markers = bucket_row.event_markers or []
    injected_events = list(llm_output.key_events)
    summary = llm_output.summary
    existing_labels = {e.get("label") for e in injected_events if isinstance(e, dict)}
    if any(m.get("event_type") == "spike" for m in markers) and "spike in activity" not in existing_labels:
        injected_events.insert(0, {"label": "spike in activity", "event_type": "spike"})
    if any(m.get("event_type") == "drop" for m in markers):
        if "drop in activity" not in existing_labels:
            injected_events.insert(0, {"label": "drop in activity", "event_type": "drop"})
        if "drop in activity" not in summary.lower():
            summary = "Drop in activity observed. " + summary
    if any(m.get("event_type") == "after_hours" for m in markers):
        if "after hours activity" not in existing_labels:
            injected_events.insert(0, {"label": "after hours activity", "event_type": "after_hours"})
        if "after hours activity" not in summary.lower():
            summary = "After hours activity detected. " + summary
    if any(m.get("event_type") == "start" for m in markers):
        if "start of activity" not in existing_labels:
            injected_events.insert(0, {"label": "start of activity", "event_type": "start"})
        if "start of activity" not in summary.lower():
            summary = "Start of activity detected. " + summary
    if any(m.get("event_type") == "late_start" for m in markers):
        if "late start" not in existing_labels:
            injected_events.insert(0, {"label": "late start", "event_type": "late_start"})
        if "late start" not in summary.lower():
            summary = "Late start detected. " + summary
    if any(m.get("event_type") == "underperforming" for m in markers):
        if "site underperforming" not in existing_labels:
            injected_events.insert(0, {"label": "site underperforming", "event_type": "underperforming"})
        if "site underperforming" not in summary.lower():
            summary = "Site underperforming. " + summary
    llm_output = LLMSummaryOutput(
        summary=summary,
        key_events=injected_events,
        confidence=llm_output.confidence,
    )

    # ------------------------------------------------------------------
    # Steps 11–12: Build SummaryRecord and upsert
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    bucket_start = _parse_ts(bucket_row.bucket_start_utc)
    bucket_end   = _parse_ts(bucket_row.bucket_end_utc)

    child_set_hash = compute_child_set_hash([bucket_id])
    summary_id = generate_summary_id(
        serial_number=serial_number,
        level="camera",
        scope_id=f"{serial_number}:{camera_id}",
        window_start=bucket_start,
        window_end=bucket_end,
        child_set_hash=child_set_hash,
        model_profile=model_profile,
        prompt_version=prompt_version,
        summary_schema_version=1,
    )

    record = SummaryRecord(
        summary_id=summary_id,
        serial_number=serial_number,
        level="camera",
        scope_id=f"{serial_number}:{camera_id}",
        start_time=bucket_start,
        end_time=bucket_end,
        summary=llm_output.summary,
        key_events=llm_output.key_events,
        metrics={},
        coverage=SummaryCoverage(
            expected=1,
            present=1,
            ratio=1.0,
            missing=[],
        ),
        summary_mode=summary_mode,
        frames_used=frames_used,
        frame_timestamps=frame_timestamps,
        confidence=llm_output.confidence,
        embedding_status="pending",
        version=1,          # overridden by upsert_summary's version logic
        is_latest=True,
        superseded_by=None,
        model_profile=model_profile,
        prompt_version=prompt_version,
        schema_version=1,
        source_refs=[bucket_id],
        created_at=now,
        updated_at=now,
    )

    upsert_summary(conn, record)

    # ------------------------------------------------------------------
    # Step 12 (continued): Insert embedding_upsert job
    # embedding_status='pending' is already set on the summary record above.
    # ------------------------------------------------------------------
    embedding_job_id = insert_embedding_job(
        conn,
        summary_id=summary_id,
        serial_number=serial_number,
    )

    # ------------------------------------------------------------------
    # Step 13: Upsert rollup state; insert rollup job if coverage >= 0.5
    # ------------------------------------------------------------------
    rollup_job_id, rollup_serial_number = upsert_rollup_state_and_maybe_enqueue(
        conn,
        serial_number=serial_number,
        camera_id=camera_id,
        bucket_start_utc=bucket_start,
        bucket_end_utc=bucket_end,
        model_profile=model_profile,
        prompt_version=prompt_version,
    )

    # ------------------------------------------------------------------
    # Step 14: Determine job state
    # metadata_only always → degraded (design_spec §8)
    # ------------------------------------------------------------------
    job_state: Literal["succeeded", "degraded"] = (
        "degraded" if summary_mode == "metadata_only" else "succeeded"
    )

    log.info(
        "run_bucket_summary: bucket_id=%s summary_id=%s mode=%s state=%s confidence=%.2f",
        bucket_id, summary_id, summary_mode, job_state, llm_output.confidence,
    )

    return ExecutionResult(
        job_state=job_state,
        last_error=None,
        summary_id=summary_id,
        embedding_job_id=embedding_job_id,
        rollup_job_id=rollup_job_id,
        rollup_serial_number=rollup_serial_number,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_candidates(bucket_row) -> list[tuple]:
    """
    Extract (datetime, label) pairs from keyframe_candidates.

    Returns only non-None slots in order: baseline_ts, peak_ts, change_ts.
    """
    kf = bucket_row.keyframe_candidates or {}
    result = []
    for label in ("baseline_ts", "peak_ts", "change_ts"):
        val = kf.get(label)
        if val is not None:
            result.append((_parse_ts(val), label))
    return result


def _fetch_usable_frames(client, camera_id: str, candidates: list) -> list[FrameResponse]:
    """
    Fetch a thumbnail for each candidate timestamp; return only usable frames.

    - 404 (None return): silent skip.
    - KeyframeNetworkError: logged as warning, treated as miss.
    - KeyframeAuthError: re-raised; propagates to worker as retry_wait / failed_terminal.
    - Quality filter: is_usable_frame() rejects blur > 0.7, brightness < 0.2, occluded.
    """
    usable = []
    for ts, label in candidates:
        try:
            frame = client.fetch_thumbnail(
                camera_id, ts, tolerance_sec=_FRAME_TOLERANCE_SEC
            )
        except KeyframeNetworkError as exc:
            log.warning("frame fetch network error label=%s camera=%s: %s", label, camera_id, exc)
            continue
        except KeyframeAuthError:
            raise
        if frame is None:
            log.debug("frame miss label=%s camera=%s ts=%s", label, camera_id, ts.isoformat())
            continue
        if not is_usable_frame(frame.quality):
            log.debug(
                "frame rejected label=%s blur=%.2f brightness=%.2f occluded=%s",
                label, frame.quality.blur, frame.quality.brightness, frame.quality.occluded,
            )
            continue
        usable.append(frame)
    return usable


def _fetch_continuum_frames(
    client: ContinuumClient,
    serial_number: str,
    camera_id: str,
    candidates: list,
) -> list[ContinuumFrameResponse]:
    """
    Fetch frames from the trailer's Continuum endpoint for each candidate timestamp.

    No quality filtering for v1 — Continuum provides no quality metadata.
    All successfully fetched frames are treated as usable.

    - 404 (None return): silent skip.
    - ContinuumNetworkError: logged as warning, treated as miss.
    """
    frames = []
    for ts, label in candidates:
        try:
            frame = client.fetch_frame(serial_number, camera_id, ts)
        except ContinuumNetworkError as exc:
            log.warning(
                "continuum fetch error label=%s sn=%s cam=%s: %s",
                label, serial_number, camera_id, exc,
            )
            continue
        if frame is None:
            log.debug(
                "continuum miss label=%s sn=%s cam=%s ts=%s",
                label, serial_number, camera_id, ts.isoformat(),
            )
            continue
        frames.append(frame)
    return frames


def _decide_summary_mode(usable_count: int, target: int = _TARGET_FRAMES) -> str:
    """full >= target, partial >= 1, metadata_only == 0."""
    if usable_count >= target:
        return "full"
    if usable_count > 0:
        return "partial"
    return "metadata_only"


def _build_prompt(bucket_row, frames: list[FrameResponse]) -> str:
    """Render bucket_summary_v1.txt with bucket metadata and optional frame URIs."""
    template = _PROMPT_PATH.read_text()
    if frames:
        lines = [
            f"  {f.uri}  (actual_ts={f.actual_ts.isoformat()}, exact={f.exact_match})"
            for f in frames
        ]
        frames_section = "Visual frames:\n" + "\n".join(lines)
    else:
        frames_section = (
            "Note: No visual frames were available. "
            "Base your analysis on metadata only."
        )
    markers = bucket_row.event_markers or []
    if markers:
        facts = []
        spike_count = sum(1 for m in markers if m.get("event_type") == "spike")
        drop_count = sum(1 for m in markers if m.get("event_type") == "drop")
        if spike_count:
            facts.append(f"FACT: A spike in activity occurred during this window. This is confirmed by {spike_count} spike event(s) and must be described.")
        if drop_count:
            facts.append(f"FACT: A drop in activity occurred during this window. This is confirmed by {drop_count} drop event(s) and must be described.")
        after_hours_count = sum(1 for m in markers if m.get("event_type") == "after_hours")
        if after_hours_count:
            facts.append("FACT: After hours activity detected during this window. This must be described.")
        start_count = sum(1 for m in markers if m.get("event_type") == "start")
        if start_count:
            facts.append("FACT: Start of activity detected during this window. Activity began after a period of inactivity.")
        late_start_count = sum(1 for m in markers if m.get("event_type") == "late_start")
        if late_start_count:
            facts.append("FACT: Late start detected. Activity began later than the expected start time.")
        underperforming_count = sum(1 for m in markers if m.get("event_type") == "underperforming")
        if underperforming_count:
            facts.append("FACT: Site underperforming. Late start with no significant activity spike for the day.")
        if facts:
            event_text = " ".join(facts)
        else:
            event_text = "Detected events: " + "; ".join(
                m.get("label", m.get("event_type", "unknown")) for m in markers
            ) + "."
    else:
        event_text = "Detected events: none."

    return template.format(
        camera_id=bucket_row.camera_id,
        start_time=bucket_row.bucket_start_utc,
        end_time=bucket_row.bucket_end_utc,
        object_counts=json.dumps(bucket_row.object_counts or {}),
        activity_score=f"{bucket_row.activity_score:.3f}",
        event_markers=event_text,
        completeness=json.dumps(bucket_row.completeness or {}),
        frames_section=frames_section,
    )


def _call_and_validate(
    prompt: str,
    bucket_row,
    frames_used: int,
    vlm_client: VLMClient | None,
    frame_uris: list[str],
) -> LLMSummaryOutput:
    """
    Call the vLLM (or stub) and validate the output against LLMSummaryOutput.

    Two-attempt loop per build_spec §10.3:

    Attempt 1 — real prompt + frame URIs:
      If vlm_client is set: call vlm_client.call(prompt, frame_uris) → raw string.
      If vlm_client is None: call call_llm_stub() → dict (always valid; no repair needed).
      Parse raw string as JSON, then validate with LLMSummaryOutput.model_validate.
      On json.JSONDecodeError or ValidationError → attempt 2.

    Attempt 2 — repair prompt, no images:
      Build repair prompt from repair_v1.txt.
      Call vlm_client.call(repair_prompt, []).
      Parse and validate.
      On second failure → return degraded LLMSummaryOutput(confidence=0.0).

    Infrastructure errors (VLMNetworkError, VLMAuthError, VLMError) are NOT
    caught here.  They propagate to the worker's except block → retry_wait or
    failed_terminal.  The repair path is only for JSON / schema failures.
    """
    # Stub path: always produces valid output, no repair needed.
    if vlm_client is None:
        raw_dict = call_llm_stub(prompt, bucket_row, frames_used)
        return LLMSummaryOutput.model_validate(raw_dict)

    # Attempt 1: real vLLM call.
    _SYSTEM_MSG = "You are a strict intelligence analysis system. You MUST follow all rules exactly."
    raw_text: str = vlm_client.call(prompt, frame_uris, system_message=_SYSTEM_MSG)  # raises on infra error
    parse_error: Exception | None = None

    try:
        return LLMSummaryOutput.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError, Exception) as exc:
        parse_error = exc
        log.warning(
            "_call_and_validate: attempt 1 failed frames=%d: %s — trying repair",
            frames_used, exc,
        )

    # Attempt 2: repair prompt, no images.
    repair_prompt = _REPAIR_PATH.read_text().format(
        error=str(parse_error),
        raw_response=raw_text[:2000],
    )
    raw_text2: str = vlm_client.call(repair_prompt, [])  # raises on infra error

    try:
        return LLMSummaryOutput.model_validate(json.loads(raw_text2))
    except (json.JSONDecodeError, ValidationError, Exception) as exc:
        log.error(
            "_call_and_validate: repair attempt failed: %s — using degraded output", exc
        )
        return LLMSummaryOutput(
            summary="Unable to parse LLM output after repair attempt. Degraded summary.",
            key_events=[],
            confidence=0.0,
        )


def _parse_ts(value) -> datetime:
    """Parse a timestamp value from a Postgres row (datetime or ISO string)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
