"""
rollup_summary job executor — L2 (hour-level) summaries.

Steps:
  1. Load payload (serial_number, camera_id, window_start/end, child_ids, ...).
  2. Fetch child camera-level summaries ordered by start_time.
  3. Coverage check:
       == 0             → failed_terminal
       < 0.5 + recent   → retry_wait (children still arriving)
       < 0.5 + old      → proceed degraded
  4. Build prompt from child summaries (hourly_rollup_v1.txt).
  5. Call vLLM (or stub) via _call_and_validate (no frames).
  6. Compute deterministic summary_id (level=hour, child_set_hash from sorted child_ids).
  7. Build SummaryRecord and upsert_summary (ON CONFLICT handles duplicates).
  8. insert_embedding_job.
  9. Return (job_state, embedding_job_id).

No day-level cascade in v1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import text

from services.panoptic_summary_agent.executor import _call_and_validate
from services.panoptic_summary_agent.summary_db import insert_embedding_job, upsert_summary
from shared.clients.vlm import VLMClient
from shared.schemas.llm import LLMSummaryOutput
from shared.schemas.summary import SummaryCoverage, SummaryRecord, generate_summary_id
from shared.utils.hashing import compute_child_set_hash

log = logging.getLogger(__name__)

_ROLLUP_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "shared" / "prompts" / "hourly_rollup_v1.txt"
)
_RECENT_WINDOW_HOURS = 2  # retry_wait if window_end > now - 2h


@dataclass(frozen=True)
class RollupResult:
    job_state: Literal["succeeded", "degraded", "failed_terminal", "retry_wait"]
    embedding_job_id: str | None  # None on non-success paths


def run_rollup_job(
    conn,
    payload: dict,
    worker_id: str,
    attempt_count: int,
    vlm_client: VLMClient | None = None,
) -> RollupResult:
    """
    Execute a rollup_summary job (L2 — hour level).

    All DB writes occur within the caller's open connection/transaction.
    No commit is issued here; the worker commits after release_job + lease check.
    """
    serial_number      = payload["serial_number"]
    camera_id      = payload["camera_id"]
    child_ids      = payload["child_ids"]
    model_profile  = payload["model_profile"]
    prompt_version = payload["prompt_version"]
    window_start   = _parse_ts(payload["window_start"])
    window_end     = _parse_ts(payload["window_end"])

    # ------------------------------------------------------------------
    # Step 2: Fetch child summaries ordered by start_time
    # ------------------------------------------------------------------
    placeholders = ", ".join(f":id_{i}" for i in range(len(child_ids)))
    id_params = {f"id_{i}": cid for i, cid in enumerate(child_ids)}

    rows = conn.execute(
        text(f"""
            SELECT summary_id, summary, confidence, start_time, end_time
              FROM panoptic_summaries
             WHERE summary_id IN ({placeholders})
               AND is_latest = true
             ORDER BY start_time
        """),
        id_params,
    ).fetchall()

    found = len(rows)
    expected = len(child_ids)

    # ------------------------------------------------------------------
    # Step 3: Coverage check
    # ------------------------------------------------------------------
    if found == 0:
        log.error(
            "run_rollup_job: no child summaries found camera=%s window=%s",
            camera_id, window_start.isoformat(),
        )
        return RollupResult(job_state="failed_terminal", embedding_job_id=None)

    coverage_ratio = found / expected

    if coverage_ratio < 0.5:
        now = datetime.now(timezone.utc)
        if window_end > now - timedelta(hours=_RECENT_WINDOW_HOURS):
            log.info(
                "run_rollup_job: coverage=%.2f < 0.5, window recent — retry_wait "
                "camera=%s window=%s",
                coverage_ratio, camera_id, window_start.isoformat(),
            )
            return RollupResult(job_state="retry_wait", embedding_job_id=None)
        log.warning(
            "run_rollup_job: coverage=%.2f < 0.5, window old — proceeding degraded "
            "camera=%s window=%s",
            coverage_ratio, camera_id, window_start.isoformat(),
        )

    # ------------------------------------------------------------------
    # Step 4: Build prompt
    # ------------------------------------------------------------------
    child_lines = "\n".join(
        f"{i + 1}. [{_fmt_ts(r.start_time)}–{_fmt_ts(r.end_time)}] "
        f"confidence={r.confidence:.2f}: {r.summary}"
        for i, r in enumerate(rows)
    )
    prompt = _ROLLUP_PROMPT_PATH.read_text().format(
        camera_id=camera_id,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        child_count=found,
        child_summaries=child_lines,
    )

    # ------------------------------------------------------------------
    # Step 5: Call vLLM (or stub) — no frames at rollup level
    # _call_and_validate's stub path uses bucket_row.object_counts; pass a
    # minimal shim so call_llm_stub produces a valid response.
    # ------------------------------------------------------------------
    class _BucketShim:
        object_counts = {}

    llm_output: LLMSummaryOutput = _call_and_validate(
        prompt=prompt,
        bucket_row=_BucketShim(),
        frames_used=0,
        vlm_client=vlm_client,
        frame_uris=[],
    )

    # ------------------------------------------------------------------
    # Step 6: Compute deterministic summary_id
    # child_set_hash derived from sorted(child_ids) — independent of fetch order.
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    child_set_hash = compute_child_set_hash(sorted(child_ids))
    summary_id = generate_summary_id(
        serial_number=serial_number,
        level="hour",
        scope_id=f"{serial_number}:{camera_id}",
        window_start=window_start,
        window_end=window_end,
        child_set_hash=child_set_hash,
        model_profile=model_profile,
        prompt_version=prompt_version,
        summary_schema_version=1,
    )

    # ------------------------------------------------------------------
    # Step 7: Build SummaryRecord
    # ------------------------------------------------------------------
    summary_mode = "full" if found == expected else "partial"
    source_refs = [r.summary_id for r in rows]  # ordered by start_time

    record = SummaryRecord(
        summary_id=summary_id,
        serial_number=serial_number,
        level="hour",
        scope_id=f"{serial_number}:{camera_id}",
        start_time=window_start,
        end_time=window_end,
        summary=llm_output.summary,
        key_events=llm_output.key_events,
        metrics={"child_set_hash": child_set_hash},
        coverage=SummaryCoverage(
            expected=expected,
            present=found,
            ratio=min(coverage_ratio, 1.0),
            missing=[],
        ),
        summary_mode=summary_mode,
        frames_used=0,
        confidence=llm_output.confidence,
        embedding_status="pending",
        version=1,
        is_latest=True,
        superseded_by=None,
        model_profile=model_profile,
        prompt_version=prompt_version,
        schema_version=1,
        source_refs=source_refs,
        created_at=now,
        updated_at=now,
    )

    # ------------------------------------------------------------------
    # Step 8: Upsert + embedding job
    # upsert_summary uses ON CONFLICT — no pre-check needed.
    # ------------------------------------------------------------------
    upsert_summary(conn, record)
    embedding_job_id = insert_embedding_job(conn, summary_id=summary_id, serial_number=serial_number)

    # ------------------------------------------------------------------
    # Step 9: Determine job state
    # ------------------------------------------------------------------
    job_state: Literal["succeeded", "degraded"] = (
        "degraded"
        if summary_mode == "partial" or llm_output.confidence == 0.0
        else "succeeded"
    )

    log.info(
        "run_rollup_job: camera=%s window=%s summary_id=%s mode=%s state=%s "
        "confidence=%.2f children=%d/%d",
        camera_id, window_start.isoformat(), summary_id,
        summary_mode, job_state, llm_output.confidence, found, expected,
    )

    return RollupResult(job_state=job_state, embedding_job_id=embedding_job_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_ts(value) -> str:
    return _parse_ts(value).strftime("%H:%M")
