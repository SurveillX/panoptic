# Reclaimer — design (v1)

**Status:** draft, pending review.
**Scope:** M2 precondition. Surfaced as the highest-priority finding
in `docs/M1_RESULTS.md` (§5, Finding 1). At-least-once delivery is
currently only on paper; this doc makes it real.

---

## 1. Purpose

`shared/utils/leases.py:457` already implements
`reclaim_expired_leases(engine, r)` with correct semantics: finds
jobs in `(leased, running, retry_wait)` past their `lease_expires_at`,
resets them to `pending` (or to `failed_terminal` + DLQ if
`attempt_count >= max_attempts`), and cleans up stale PEL entries via
XAUTOCLAIM.

The function is well-built and was verified in M1's test 2 (manual
invocation recovered a crashed worker's job with no duplicates).

**What's missing: nothing runs it on a schedule.** A crashed worker's
lease sits forever until somebody invokes the function by hand. This
doc decides how to close that gap.

---

## 2. Placement decision

Two shapes were considered:

### Option A — in-worker ticker

Each worker starts a background thread that calls
`reclaim_expired_leases()` every N seconds.

**Pros:** no extra process to supervise.
**Cons:**
* N workers × N tickers = N concurrent reclaimers doing redundant
  work (safe under `FOR UPDATE SKIP LOCKED` but still waste).
* **If every worker is dead, no reclaimer runs.** Precisely the
  scenario at-least-once most needs to cover.
* Couples a cross-cutting concern into every worker's main loop.

### Option B — dedicated `panoptic_reclaimer` process *(chosen)*

A separate worker-shaped process whose sole job is running
`reclaim_expired_leases()` in a loop.

**Pros:**
* One responsibility, easy to reason about.
* Survives the scenario where every other worker has crashed — the
  reclaimer itself is the recovery mechanism.
* Fits the existing `services/panoptic_<name>/` directory pattern.
* Gets a slot in the tmux launcher (8th window) and a `/healthz`
  endpoint just like every other worker.
* Single instance on the Spark today; multi-Spark later adds more
  instances and `FOR UPDATE SKIP LOCKED` already handles contention.

**Cons:**
* One more process to watch. Acceptable — it's ~30 lines of Python
  and has no complex startup.

**Decision: Option B.**

---

## 3. Implementation surface

New service directory `services/panoptic_reclaimer/`:

```
services/panoptic_reclaimer/
├── __init__.py
└── worker.py          # entry point
```

Plus two touch-ups elsewhere:

| File | Change |
|---|---|
| `shared/utils/leases.py` | No code change. (The existing `reclaim_expired_leases()` is the implementation.) |
| `scripts/tmux-dev.sh` | Add 8th window: `mkwindow reclaimer services.panoptic_reclaimer.worker` |
| `.env.example` | Add `RECLAIMER_INTERVAL_SEC=30`, `RECLAIMER_HEALTH_PORT=8210` |
| `docs/OBSERVABILITY_DESIGN.md` | Already reserves port 8210 for the reclaimer |

### 3.1 Worker entry point sketch

```python
# services/panoptic_reclaimer/worker.py
from __future__ import annotations
import logging, os, time
from sqlalchemy import create_engine

from shared.health.server import start_health_server
from shared.health.state import HealthState
from shared.utils.leases import generate_worker_id, reclaim_expired_leases
from shared.utils.redis_client import get_redis_client

log = logging.getLogger(__name__)

INTERVAL_SEC = int(os.environ.get("RECLAIMER_INTERVAL_SEC", "30"))
HEALTH_PORT  = int(os.environ.get("RECLAIMER_HEALTH_PORT", "8210"))
DATABASE_URL = os.environ["DATABASE_URL"]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()
    worker_id = generate_worker_id()

    health = HealthState(service_name="panoptic_reclaimer", worker_id=worker_id)
    start_health_server(port=HEALTH_PORT, state=health)

    log.info("reclaimer starting worker_id=%s interval=%ds", worker_id, INTERVAL_SEC)

    while True:
        try:
            stats = reclaim_expired_leases(engine, r)
            health.record_reclaim(stats)
            if stats.reset_to_pending or stats.sent_to_dlq or stats.stale_pel_acked:
                log.info(
                    "reclaimer tick: reset=%d dlq=%d pel_acked=%d",
                    stats.reset_to_pending, stats.sent_to_dlq, stats.stale_pel_acked,
                )
            else:
                log.debug("reclaimer tick: quiet")
        except Exception as exc:
            log.exception("reclaimer: tick failed (continuing): %s", exc)
            health.record_failure(str(exc))

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
```

A bug in one tick must not kill the process — hence the broad
`except` with log. Matches how existing workers handle their own
XREADGROUP errors.

---

## 4. Interval

**30 seconds** matches the observability probe cadence and is well
below LEASE_TTL (120 s). A dead worker's job becomes reclaimable 120
s after the last lease extension; the next reclaimer tick then fires
within 30 s, giving a **worst-case recovery lag of ~150 s**.

Tunable via `RECLAIMER_INTERVAL_SEC`. Don't lower below ~10 s without
reason — tick cost is one SELECT + optional UPDATE, cheap but still
non-zero.

---

## 5. Observability integration

The reclaimer's `/healthz` shape follows the workers' shape (from
`OBSERVABILITY_DESIGN.md §3.2`) with two additional fields:

```json
{
  "status": "ok",
  "service": "panoptic_reclaimer",
  "worker_id": "spark-6262:12345:a1b2c3d4",
  "uptime_sec": 3421,
  "reclaim": {
    "last_run_at": "2026-04-17T22:49:42+00:00",
    "last_run_reset": 0,
    "last_run_dlq": 0,
    "last_run_pel_acked": 0,
    "totals": {
      "reset_to_pending": 14,
      "sent_to_dlq": 1,
      "stale_pel_acked": 3
    },
    "last_error": null,
    "last_error_at": null
  },
  "dependencies": {
    "postgres": {"ok": true, "latency_ms": 2},
    "redis":    {"ok": true, "latency_ms": 1}
  }
}
```

Dashboard line for reclaimer (already sketched in
`OBSERVABILITY_DESIGN.md §4`):

```
reclaimer        :8210  ok    reset=2  dlq=0  last_run=27s ago    deps=pg,redis
```

---

## 6. Failure modes

| Scenario | Behavior |
|---|---|
| Postgres down | `reclaim_expired_leases()` raises. Caught, logged, `/healthz` → degraded. Next tick retries. |
| Redis down | Phase-1 updates succeed (Postgres state resets to pending). Phase-2 XAUTOCLAIM fails. Logged, degraded status. |
| Multiple reclaimers (future multi-Spark) | `FOR UPDATE SKIP LOCKED` ensures each row processed by exactly one reclaimer per tick. Safe. |
| Reclaimer itself crashes | `tmux-dev.sh` has `remain-on-exit on`; operator sees dead pane. For prod, systemd / container restart handles this. |
| A worker crashes mid-job but its **lease hasn't expired yet** | Nothing for the reclaimer to do until lease expiry. Correct behavior — lease hasn't timed out, so the worker is nominally still holding it. |

---

## 7. What this does NOT cover

Deliberately out of scope:

* **Worker process supervision.** The reclaimer recovers *jobs* from
  crashed workers. It does not restart the workers themselves. That's
  systemd / docker-compose / tmux's job.
* **DLQ processing.** Reclaimer only sends items to the DLQ; it does
  not replay from it. DLQ replay is M4 scope.
* **Proactive health-check-driven job redistribution.** We rely on
  lease expiry, not on worker health signals. Simpler and sufficient.

---

## 8. Rollout

Order of work once this doc is approved:

1. Create `services/panoptic_reclaimer/{__init__.py,worker.py}` (~20 min)
2. Add health-state wiring once `shared/health/` exists from the
   observability work (~10 min — dep-ordered after obs scaffold)
3. Add `reclaimer` window to `scripts/tmux-dev.sh` (~2 min)
4. Add env vars to `.env.example` (~2 min)
5. Smoke: start stack, SIGINT the caption worker mid-job, observe
   the reclaimer pane log `reset=1` within ~150 s, verify the job
   retries and succeeds (the exact test `dev_reclaim_test.py`
   already runs — it'll now pass without manual reclaim invocation)
   (~10 min)

Total: ~45 min after the observability scaffold lands.

---

## 9. Dependency on observability

This doc shares the `shared/health/` module with the observability
design. Correct build order: **observability scaffold first**, then
reclaimer consumes it. If schedule pressure arises, the reclaimer can
temporarily skip the health endpoint (start with a stub) and add it
once observability lands — but the cleaner order is obs → reclaimer.

---

## 10. Open questions

None. This is a thin wrapper around an already-correct function; the
only real decision was Option A vs B (§2), which this doc locks as B.

Ready to implement once approved.
