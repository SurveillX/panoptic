"""
Thread-safe health state for a single worker/service process.

Updated by:
  - the main worker loop (job claim/success/failure)
  - the background dep-probe loop
  - the HTTP /healthz handler (read only)

Serialized via a single Lock; contention is trivial since updates are rare.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DepStatus:
    ok: bool = False
    latency_ms: int | None = None
    reason: str | None = None
    checked_at: str | None = None


@dataclass
class ConsumerStats:
    stream: str | None = None
    group: str | None = None
    pending_pel: int = 0
    xlen: int = 0
    lag_sec: int | None = None
    updated_at: str | None = None


@dataclass
class JobStats:
    last_claim_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    attempts: int = 0


@dataclass
class ReclaimStatsSnapshot:
    last_run_at: str | None = None
    last_run_reset: int = 0
    last_run_dlq: int = 0
    last_run_pel_acked: int = 0
    totals_reset: int = 0
    totals_dlq: int = 0
    totals_pel_acked: int = 0
    last_error: str | None = None
    last_error_at: str | None = None


class HealthState:
    """
    Aggregates per-process health state. Thread-safe.

    Usage:
        h = HealthState(service_name="panoptic_image_caption_worker",
                        worker_id=worker_id,
                        consumer_stream="panoptic:jobs:image_caption",
                        consumer_group="panoptic-image-caption-workers")
        h.record_job_claim()
        h.record_job_success()
        h.set_dep("postgres", DepStatus(ok=True, latency_ms=2, checked_at=_utcnow_iso()))
        snap = h.snapshot()  # dict for JSON encoding
    """

    def __init__(
        self,
        service_name: str,
        worker_id: str,
        consumer_stream: str | None = None,
        consumer_group: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._start_ts = time.monotonic()
        self._service = service_name
        self._worker_id = worker_id
        self._deps: dict[str, DepStatus] = {}
        self._consumer = ConsumerStats(stream=consumer_stream, group=consumer_group)
        self._jobs = JobStats()
        self._reclaim = ReclaimStatsSnapshot()
        self._reclaim_has_data = False
        self._degraded_deps: set[str] = set()
        self._critical_deps: set[str] = set()

    # ------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------

    def mark_critical(self, *deps: str) -> None:
        """Mark these dep names as critical (error if any down)."""
        with self._lock:
            self._critical_deps.update(deps)

    def mark_non_critical(self, *deps: str) -> None:
        """Mark these dep names as non-critical (status=degraded if down)."""
        with self._lock:
            self._critical_deps.difference_update(deps)

    # ------------------------------------------------------------
    # Worker loop updates
    # ------------------------------------------------------------

    def record_job_claim(self) -> None:
        with self._lock:
            self._jobs.last_claim_at = _utcnow_iso()
            self._jobs.attempts += 1

    def record_job_success(self) -> None:
        with self._lock:
            self._jobs.last_success_at = _utcnow_iso()

    def record_job_failure(self) -> None:
        with self._lock:
            self._jobs.last_failure_at = _utcnow_iso()

    # ------------------------------------------------------------
    # Consumer stats (updated by probe loop when available)
    # ------------------------------------------------------------

    def set_consumer_stats(self, *, pending_pel: int, xlen: int, lag_sec: int | None) -> None:
        with self._lock:
            self._consumer.pending_pel = pending_pel
            self._consumer.xlen = xlen
            self._consumer.lag_sec = lag_sec
            self._consumer.updated_at = _utcnow_iso()

    # ------------------------------------------------------------
    # Dependency status (set by probe loop)
    # ------------------------------------------------------------

    def set_dep(self, name: str, status: DepStatus) -> None:
        with self._lock:
            self._deps[name] = status

    # ------------------------------------------------------------
    # Reclaimer-specific
    # ------------------------------------------------------------

    def record_reclaim(self, stats: Any) -> None:
        """Accepts a ReclaimStats-shaped object (from leases.py)."""
        with self._lock:
            self._reclaim_has_data = True
            self._reclaim.last_run_at = _utcnow_iso()
            self._reclaim.last_run_reset = getattr(stats, "reset_to_pending", 0)
            self._reclaim.last_run_dlq = getattr(stats, "sent_to_dlq", 0)
            self._reclaim.last_run_pel_acked = getattr(stats, "stale_pel_acked", 0)
            self._reclaim.totals_reset += self._reclaim.last_run_reset
            self._reclaim.totals_dlq += self._reclaim.last_run_dlq
            self._reclaim.totals_pel_acked += self._reclaim.last_run_pel_acked

    def record_failure(self, err: str) -> None:
        with self._lock:
            self._reclaim.last_error = err[:200]
            self._reclaim.last_error_at = _utcnow_iso()

    # ------------------------------------------------------------
    # Readout
    # ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            uptime_sec = int(now - self._start_ts)

            # Determine overall status.
            status = "ok"
            for name, dep in self._deps.items():
                if dep.ok:
                    continue
                if name in self._critical_deps:
                    status = "error"
                    break
                if status == "ok":
                    status = "degraded"

            payload: dict[str, Any] = {
                "status": status,
                "service": self._service,
                "worker_id": self._worker_id,
                "uptime_sec": uptime_sec,
                "dependencies": {
                    name: {
                        "ok": d.ok,
                        "latency_ms": d.latency_ms,
                        **({"reason": d.reason} if d.reason else {}),
                        **({"checked_at": d.checked_at} if d.checked_at else {}),
                    }
                    for name, d in self._deps.items()
                },
            }

            if self._consumer.stream is not None:
                payload["consumer"] = {
                    "stream": self._consumer.stream,
                    "group": self._consumer.group,
                    "pending_pel": self._consumer.pending_pel,
                    "xlen": self._consumer.xlen,
                    "lag_sec": self._consumer.lag_sec,
                    "updated_at": self._consumer.updated_at,
                }

            if self._jobs.last_claim_at or self._jobs.last_success_at or self._jobs.attempts:
                payload["jobs"] = {
                    "last_claim_at": self._jobs.last_claim_at,
                    "last_success_at": self._jobs.last_success_at,
                    "last_failure_at": self._jobs.last_failure_at,
                    "attempts": self._jobs.attempts,
                }

            if self._reclaim_has_data:
                payload["reclaim"] = {
                    "last_run_at": self._reclaim.last_run_at,
                    "last_run_reset": self._reclaim.last_run_reset,
                    "last_run_dlq": self._reclaim.last_run_dlq,
                    "last_run_pel_acked": self._reclaim.last_run_pel_acked,
                    "totals": {
                        "reset_to_pending": self._reclaim.totals_reset,
                        "sent_to_dlq": self._reclaim.totals_dlq,
                        "stale_pel_acked": self._reclaim.totals_pel_acked,
                    },
                    "last_error": self._reclaim.last_error,
                    "last_error_at": self._reclaim.last_error_at,
                }

            return payload
