"""
Lease management for Panoptic workers.

Design guarantees:
  - Postgres (panoptic_jobs) is the authoritative source of truth for lease state.
  - Redis Streams is the delivery mechanism only.
  - At-least-once execution: a crashed worker's job is reclaimed and retried.
  - No duplicate execution: claim_job uses a conditional Postgres UPDATE
    (WHERE state='pending') so only one worker wins the race per job.
  - No silent loss: reclaimer resets Postgres state; orchestrator re-enqueues.

Lease lifecycle:
  pending     → leased        (claim_job)
  leased      → running       (mark_running — optional, for observability)
  leased/running → succeeded | degraded     (release_job, attempt OK)
  leased/running → retry_wait              (release_job, transient failure)
  leased/running → failed_terminal         (release_job, attempts exhausted)
  retry_wait  → pending       (reclaimer, after lease_expires_at backoff passes)
  leased/running (expired) → pending | failed_terminal  (reclaimer)

ACK contract (enforced here):
  ACK happens ONLY after release_job + Postgres commit.
  If claim_job returns None, the worker must NOT ACK — leave the message in
  PEL so the reclaimer can clean it up via XAUTOCLAIM.
  If the worker crashes before ACK, the PEL entry is the recovery breadcrumb.

Double-execution prevention:
  All terminal state writes (succeeded, degraded, retry_wait, failed_terminal,
  cancelled) include WHERE lease_owner = worker_id.  If 0 rows are affected,
  the lease was stolen by the reclaimer; the worker MUST abort and discard
  any results it was about to write.

Multi-machine safety:
  worker_id includes hostname + PID + random suffix, unique per process instance.
  FOR UPDATE SKIP LOCKED in the reclaimer prevents concurrent reclaimers from
  double-processing the same expired job row.

Enqueueing jobs:
  Only the orchestrator enqueues jobs (XADD to Redis Streams).
  The reclaimer NEVER calls enqueue_job or XADD.
  Reclaimer responsibility: reset Postgres state + clean up stale PEL entries.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import redis
from sqlalchemy import Connection, text

from shared.utils.streams import (
    CONSUMER_GROUP_FOR_STREAM,
    DLQ_FOR_JOB_TYPE,
    autoclaim_and_ack_stale,
    enqueue_dlq,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEASE_TTL_SECONDS: int = 120
HEARTBEAT_INTERVAL_SECONDS: int = 30

# Retry backoff: 30s * 2^(attempt-1), capped at 10 minutes.
_MAX_RETRY_DELAY_SECONDS: int = 600


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------

def generate_worker_id() -> str:
    """
    Generate a unique, stable worker ID for this process instance.

    Format: {hostname}:{pid}:{random8}
    Stable for the lifetime of the process; unique across restarts and machines.
    """
    hostname = socket.gethostname()
    pid = os.getpid()
    rnd = uuid.uuid4().hex[:8]
    return f"{hostname}:{pid}:{rnd}"


# ---------------------------------------------------------------------------
# Claim result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimResult:
    """Returned by claim_job on success."""

    job_id: str
    serial_number: str
    job_type: str
    attempt_count: int   # current attempt number (already incremented)
    max_attempts: int


# ---------------------------------------------------------------------------
# Lease operations (Postgres)
# ---------------------------------------------------------------------------

def claim_job(
    conn: Connection,
    *,
    job_id: str,
    worker_id: str,
) -> ClaimResult | None:
    """
    Atomically claim a pending job via a conditional Postgres UPDATE.

    Returns ClaimResult on success, None if the job is no longer pending
    (already claimed by another worker, completed, or cancelled).

    attempt_count is incremented here — every claim counts as an attempt,
    including those by workers that subsequently crash.

    On None return: the caller MUST NOT ACK the stream message.
    Leave it in the PEL; the reclaimer will clean it up via XAUTOCLAIM
    once it has been idle for LEASE_TTL seconds.

    On ClaimResult return: ACK only after release_job() + conn.commit()
    have both completed (in a finally block).
    """
    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=LEASE_TTL_SECONDS)

    row = conn.execute(
        text("""
            UPDATE panoptic_jobs
               SET state            = 'leased',
                   lease_owner      = :worker_id,
                   lease_expires_at = :expires_at,
                   attempt_count    = attempt_count + 1,
                   updated_at       = now()
             WHERE job_id = :job_id
               AND state  = 'pending'
         RETURNING job_id, serial_number, job_type, attempt_count, max_attempts
        """),
        {"worker_id": worker_id, "expires_at": lease_expires_at, "job_id": job_id},
    ).fetchone()

    if row is None:
        log.debug("claim_job: job_id=%s not pending (already claimed or terminal)", job_id)
        return None

    _append_job_history(
        conn,
        job_id=row.job_id,
        serial_number=row.serial_number,
        from_state="pending",
        to_state="leased",
        worker_id=worker_id,
        note=f"attempt {row.attempt_count}/{row.max_attempts}",
    )

    log.info(
        "claimed job_id=%s worker=%s attempt=%d/%d",
        row.job_id, worker_id, row.attempt_count, row.max_attempts,
    )
    return ClaimResult(
        job_id=row.job_id,
        serial_number=row.serial_number,
        job_type=row.job_type,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
    )


def renew_lease(
    conn: Connection,
    *,
    job_id: str,
    worker_id: str,
) -> bool:
    """
    Extend the lease by LEASE_TTL_SECONDS from now.

    Returns True if the renewal succeeded (we still own the lease).
    Returns False if the lease was stolen by the reclaimer.

    Workers MUST abort immediately when this returns False.  The lease is
    gone; any subsequent release_job call will affect 0 rows and return False,
    confirming the abort.  Do not write results.
    """
    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=LEASE_TTL_SECONDS)

    result = conn.execute(
        text("""
            UPDATE panoptic_jobs
               SET lease_expires_at = :expires_at,
                   updated_at       = now()
             WHERE job_id      = :job_id
               AND lease_owner = :worker_id
               AND state IN ('leased', 'running')
        """),
        {"expires_at": lease_expires_at, "job_id": job_id, "worker_id": worker_id},
    )

    if result.rowcount == 0:
        log.warning("renew_lease: lease lost job_id=%s worker=%s — abort", job_id, worker_id)
        return False

    log.debug("renewed lease job_id=%s worker=%s", job_id, worker_id)
    return True


def mark_running(
    conn: Connection,
    *,
    job_id: str,
    worker_id: str,
) -> None:
    """
    Transition a leased job to 'running' after initial setup is complete.

    Optional but recommended for observability — distinguishes jobs that are
    actively processing from those that were just claimed.
    """
    row = conn.execute(
        text("""
            UPDATE panoptic_jobs
               SET state      = 'running',
                   updated_at = now()
             WHERE job_id      = :job_id
               AND lease_owner = :worker_id
               AND state       = 'leased'
         RETURNING job_id, serial_number
        """),
        {"job_id": job_id, "worker_id": worker_id},
    ).fetchone()

    if row is not None:
        _append_job_history(
            conn,
            job_id=row.job_id,
            serial_number=row.serial_number,
            from_state="leased",
            to_state="running",
            worker_id=worker_id,
            note=None,
        )


def release_job(
    conn: Connection,
    *,
    job_id: str,
    worker_id: str,
    new_state: Literal["succeeded", "degraded", "retry_wait", "failed_terminal", "cancelled"],
    last_error: str | None = None,
    retry_after_seconds: int | None = None,
) -> bool:
    """
    Release the lease and set the job's terminal or waiting state.

    All writes include WHERE lease_owner = worker_id.

    Returns True  → update succeeded.  Caller MUST follow this exact sequence:
                      1. conn.commit()          — make Postgres state durable
                      2. enqueue_dlq(...)       — ONLY if new_state == 'failed_terminal'
                      3. ack_message(...)       — ACK the stream entry last
                    Reversing steps 2 or 3 risks a DLQ message with no committed
                    Postgres record, or a consumed stream entry with no durable state.

    Returns False → 0 rows affected; the lease was stolen by the reclaimer before
                    this worker finished.  Caller MUST abort immediately:
                      - do NOT commit any results
                      - do NOT call enqueue_dlq
                      - do NOT ACK the stream message (reclaimer will clean PEL)
                      - do NOT write to any downstream systems

    retry_wait:
      Pass retry_after_seconds to set the backoff window.
      lease_expires_at is repurposed as the "retry not before" timestamp.
      The reclaimer resets retry_wait → pending once lease_expires_at passes.
      lease_owner is cleared so the reclaimer can reclaim without ownership check.

    All other states:
      lease_owner and lease_expires_at are set to NULL.
    """
    if new_state == "retry_wait":
        if retry_after_seconds is None:
            raise ValueError("retry_after_seconds required for retry_wait")
        retry_until = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        row = conn.execute(
            text("""
                UPDATE panoptic_jobs
                   SET state            = 'retry_wait',
                       lease_owner      = NULL,
                       lease_expires_at = :retry_until,
                       last_error       = :last_error,
                       updated_at       = now()
                 WHERE job_id      = :job_id
                   AND lease_owner = :worker_id
             RETURNING job_id, serial_number
            """),
            {
                "retry_until": retry_until,
                "last_error":  last_error,
                "job_id":      job_id,
                "worker_id":   worker_id,
            },
        ).fetchone()
    else:
        row = conn.execute(
            text("""
                UPDATE panoptic_jobs
                   SET state            = :new_state,
                       lease_owner      = NULL,
                       lease_expires_at = NULL,
                       last_error       = :last_error,
                       updated_at       = now()
                 WHERE job_id      = :job_id
                   AND lease_owner = :worker_id
             RETURNING job_id, serial_number
            """),
            {
                "new_state":  new_state,
                "last_error": last_error,
                "job_id":     job_id,
                "worker_id":  worker_id,
            },
        ).fetchone()

    if row is None:
        # Lease was stolen — reclaimer already reset this job.
        # Worker must abort: do not commit, do not ACK stream message.
        log.error(
            "release_job: lease stolen before release job_id=%s worker=%s new_state=%s "
            "— aborting, no results will be written",
            job_id, worker_id, new_state,
        )
        return False

    _append_job_history(
        conn,
        job_id=row.job_id,
        serial_number=row.serial_number,
        from_state=None,
        to_state=new_state,
        worker_id=worker_id,
        note=last_error[:500] if last_error else None,
    )
    log.info("released job_id=%s worker=%s state=%s", job_id, worker_id, new_state)
    return True


def compute_retry_delay(attempt_count: int) -> int:
    """
    Exponential backoff for retry_wait:
      attempt 1 → 30s
      attempt 2 → 60s
      attempt 3 → 120s
      ...capped at 600s (10 minutes)
    """
    return min(30 * (2 ** (attempt_count - 1)), _MAX_RETRY_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Heartbeat (background thread)
# ---------------------------------------------------------------------------

class LeaseHeartbeat:
    """
    Context manager that renews a job lease every HEARTBEAT_INTERVAL_SECONDS
    in a background daemon thread.

    Usage:
        with LeaseHeartbeat(engine, job_id, worker_id) as hb:
            do_work()
            if not hb.is_valid():
                # lease was stolen — abort without writing results
                raise LeaseExpiredError(job_id)

    The heartbeat thread uses its own Postgres connection so it does not
    share a transaction with the caller.

    On context exit the thread stops.  Check is_valid() before committing
    any results — a False here means release_job will return False and the
    caller should skip the commit and ACK.
    """

    def __init__(self, engine, job_id: str, worker_id: str) -> None:
        self._engine = engine
        self._job_id = job_id
        self._worker_id = worker_id
        self._valid = True
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LeaseHeartbeat:
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self._job_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_valid(self) -> bool:
        """True while we hold the lease. False if the reclaimer stole it."""
        return self._valid

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS):
            try:
                with self._engine.connect() as conn:
                    ok = renew_lease(conn, job_id=self._job_id, worker_id=self._worker_id)
                    conn.commit()
                if not ok:
                    self._valid = False
                    return
            except Exception as exc:
                # Transient DB/network error — keep trying.
                # If this persists past LEASE_TTL the reclaimer will reclaim.
                log.warning(
                    "heartbeat: renewal error job_id=%s: %s", self._job_id, exc
                )


class LeaseExpiredError(Exception):
    """Raised when a worker detects its lease was stolen mid-execution."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Lease expired during execution of job_id={job_id}")
        self.job_id = job_id


# ---------------------------------------------------------------------------
# Reclaimer
# ---------------------------------------------------------------------------

@dataclass
class ReclaimedJob:
    """A job that was reset to pending by the reclaimer, ready for re-enqueue."""
    job_id: str
    job_type: str
    serial_number: str


@dataclass
class ReclaimStats:
    reset_to_pending: int = 0
    sent_to_dlq: int = 0
    stale_pel_acked: int = 0
    # List of jobs whose state was reset to pending during this tick.
    # The reclaim function does NOT re-enqueue; callers (the reclaimer
    # process) are expected to XADD each of these to the appropriate stream.
    reset_jobs: list[ReclaimedJob] = None

    def __post_init__(self) -> None:
        if self.reset_jobs is None:
            self.reset_jobs = []


def reclaim_expired_leases(engine, r: redis.Redis) -> ReclaimStats:
    """
    Reclaim jobs from crashed workers and expired retry waits.

    Takes a SQLAlchemy Engine (not a Connection) so it can own its transaction
    boundary and guarantee the required ordering:

      For failed_terminal:
        1. UPDATE Postgres → state = 'failed_terminal'   (inside transaction)
        2. conn.commit()                                  (durable before any Redis)
        3. enqueue_dlq(...)                               (only after commit)
        [No stream ACK — reclaimer has no PEL entry to ACK]

      For pending reset:
        1. UPDATE Postgres → state = 'pending'
        2. conn.commit()
        [No Redis enqueue — orchestrator re-enqueues pending jobs]

    Responsibility boundary:
      - Resets Postgres state only (no enqueue_job / XADD).
      - Re-enqueueing of pending jobs is the orchestrator's responsibility.
      - DLQ enqueue happens after commit; if DLQ enqueue fails the job is
        already marked failed_terminal in Postgres, so it will not be retried.
        The DLQ entry is informational; Postgres state is authoritative.

    Phase 1 — Expired leases + backoff timeouts:
      Finds jobs in ('leased', 'running', 'retry_wait') with
      lease_expires_at < now().  Uses FOR UPDATE SKIP LOCKED so concurrent
      reclaimers don't double-process the same row.

      - attempt_count >= max_attempts → failed_terminal, then enqueue_dlq
      - attempt_count <  max_attempts → pending (orchestrator will re-enqueue)

    Phase 2 — Stale PEL cleanup:
      XAUTOCLAIMs PEL entries idle for > LEASE_TTL seconds and XACKs them.
      Runs after Phase 1 commit, so Postgres state is durable before PEL
      entries are discarded.
    """
    stats = ReclaimStats()

    # ------------------------------------------------------------------
    # Phase 1: expired leases and retry backlogs
    # Each job is processed in its own transaction so a commit-per-job
    # failure does not block recovery of other jobs.
    # ------------------------------------------------------------------
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT job_id, serial_number, job_type, attempt_count, max_attempts
                  FROM panoptic_jobs
                 WHERE state IN ('leased', 'running', 'retry_wait')
                   AND lease_expires_at < now()
                 ORDER BY lease_expires_at
                 LIMIT 100
                 FOR UPDATE SKIP LOCKED
            """)
        ).fetchall()
        # Commit the SELECT FOR UPDATE SKIP LOCKED immediately to release locks
        # while we process; actual updates below use separate connections.
        conn.commit()

    dlq_pending: list[dict] = []

    for row in rows:
        with engine.connect() as conn:
            if row.attempt_count >= row.max_attempts:
                # Step 1: UPDATE Postgres
                conn.execute(
                    text("""
                        UPDATE panoptic_jobs
                           SET state            = 'failed_terminal',
                               lease_owner      = NULL,
                               lease_expires_at = NULL,
                               updated_at       = now()
                         WHERE job_id = :job_id
                    """),
                    {"job_id": row.job_id},
                )
                _append_job_history(
                    conn,
                    job_id=row.job_id,
                    serial_number=row.serial_number,
                    from_state=None,
                    to_state="failed_terminal",
                    worker_id="reclaimer",
                    note=f"attempts exhausted ({row.attempt_count}/{row.max_attempts})",
                )
                # Step 2: COMMIT — durable before any Redis write
                conn.commit()

                # Step 3: enqueue_dlq — only after commit
                try:
                    enqueue_dlq(
                        r,
                        job_type=row.job_type,
                        job_id=row.job_id,
                        serial_number=row.serial_number,
                        reason=(
                            f"attempts exhausted "
                            f"({row.attempt_count}/{row.max_attempts})"
                        ),
                    )
                except Exception as exc:
                    # DLQ is informational; failed_terminal in Postgres is
                    # authoritative.  Log and continue.
                    log.error(
                        "reclaimer: DLQ enqueue failed job_id=%s: %s",
                        row.job_id, exc,
                    )
                stats.sent_to_dlq += 1

            else:
                # Step 1: UPDATE Postgres → pending
                conn.execute(
                    text("""
                        UPDATE panoptic_jobs
                           SET state            = 'pending',
                               lease_owner      = NULL,
                               lease_expires_at = NULL,
                               updated_at       = now()
                         WHERE job_id = :job_id
                    """),
                    {"job_id": row.job_id},
                )
                _append_job_history(
                    conn,
                    job_id=row.job_id,
                    serial_number=row.serial_number,
                    from_state=None,
                    to_state="pending",
                    worker_id="reclaimer",
                    note="lease expired — reset for retry; orchestrator will re-enqueue",
                )
                # Step 2: COMMIT
                conn.commit()
                stats.reset_to_pending += 1
                stats.reset_jobs.append(
                    ReclaimedJob(
                        job_id=str(row.job_id),
                        job_type=row.job_type,
                        serial_number=row.serial_number,
                    )
                )

    # ------------------------------------------------------------------
    # Phase 2: PEL cleanup via XAUTOCLAIM
    # Runs after all Phase 1 commits are durable.
    # ------------------------------------------------------------------
    min_idle_ms = LEASE_TTL_SECONDS * 1000
    reclaimer_id = f"reclaimer:{generate_worker_id()}"

    for stream, group in CONSUMER_GROUP_FOR_STREAM.items():
        try:
            acked = autoclaim_and_ack_stale(
                r,
                stream=stream,
                group=group,
                reclaimer_id=reclaimer_id,
                min_idle_ms=min_idle_ms,
            )
            stats.stale_pel_acked += acked
        except Exception as exc:
            log.error("reclaimer: PEL cleanup error stream=%s: %s", stream, exc)

    if rows:
        log.info(
            "reclaim complete: reset=%d dlq=%d pel_acked=%d",
            stats.reset_to_pending, stats.sent_to_dlq, stats.stale_pel_acked,
        )

    return stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _append_job_history(
    conn: Connection,
    *,
    job_id: str,
    serial_number: str | None,
    from_state: str | None,
    to_state: str,
    worker_id: str | None,
    note: str | None,
) -> None:
    """
    Append a row to panoptic_job_history.

    panoptic_job_history is append-only — never UPDATE or DELETE from it.
    """
    conn.execute(
        text("""
            INSERT INTO panoptic_job_history
                (job_id, serial_number, from_state, to_state, worker_id, note)
            VALUES
                (:job_id, :serial_number, :from_state, :to_state, :worker_id, :note)
        """),
        {
            "job_id":        job_id,
            "serial_number": serial_number or "",
            "from_state":    from_state,
            "to_state":      to_state,
            "worker_id":     worker_id,
            "note":          note,
        },
    )
