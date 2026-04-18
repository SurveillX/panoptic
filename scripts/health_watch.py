"""
Periodic health+signal capture for Panoptic.

Intended to run from cron (or `watch`) — one-shot, not a daemon. Each
invocation:

  1. Probes every worker /healthz, webhook /health, search_api /health.
  2. Reads derived signals from Postgres (non-terminal job count, age,
     connection count) and Redis (DLQ depth per stream, memory).
  3. Writes a single-line status record to ~/panoptic/logs/health_watch.log
     (csv-ish, greppable).
  4. Prints a human-readable summary.
  5. Emits "ALERT: reason" lines to stdout + returns exit code 1 if any
     threshold is crossed. Cron mails on non-zero exit by default.

Thresholds are conservative and meant for a single-trailer dev setup.
Revisit in docs/SCALING.md when approaching 10+ trailers.

    # one-shot
    .venv/bin/python scripts/health_watch.py

    # cron (every 5 min, mail on ALERT)
    */5 * * * * cd $HOME/panoptic && . ./.env && .venv/bin/python scripts/health_watch.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import sqlalchemy as sa
from redis import Redis

LOG_FILE = Path(os.environ.get("HEALTH_WATCH_LOG", str(Path.home() / "panoptic/logs/health_watch.log")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


# --- thresholds (conservative for single-trailer dev) ---
MAX_NONTERMINAL_JOBS = 50        # fine steady-state; alerts if backlog grows
MAX_NONTERMINAL_AGE_MIN = 10     # a job pending/leased for >10min = stuck
MAX_DLQ_DEPTH = 0                # any DLQ entries demand attention
MAX_DISK_USED_PCT = 80           # /data fill
MAX_PG_CONNS_PCT = 80            # of configured max_connections
MAX_REDIS_MEMORY_MB = 1024       # soft
MAX_WORKER_HEALTH_AGE_SEC = 120  # /healthz staleness


WORKER_PROBES = [
    ("webhook",    "http://localhost:8100/health"),
    ("caption",    "http://localhost:8201/healthz"),
    ("cap_embed",  "http://localhost:8202/healthz"),
    ("summary",    "http://localhost:8203/healthz"),
    ("sum_embed",  "http://localhost:8204/healthz"),
    ("rollup",     "http://localhost:8205/healthz"),
    ("img_embed",  "http://localhost:8206/healthz"),
    ("reclaimer",  "http://localhost:8210/healthz"),
    ("search_api", "http://localhost:8600/health"),
]


def main() -> int:
    now = datetime.now(timezone.utc)
    alerts: list[str] = []
    summary: dict = {"ts": now.isoformat()}

    # ---------------- Worker /healthz ----------------
    worker_status: dict[str, str] = {}
    for name, url in WORKER_PROBES:
        try:
            r = httpx.get(url, timeout=5)
            if r.status_code == 200:
                body = r.json()
                worker_status[name] = body.get("status", "ok")
            else:
                worker_status[name] = f"http{r.status_code}"
                alerts.append(f"{name} /healthz → HTTP {r.status_code}")
        except Exception as exc:
            worker_status[name] = "unreach"
            alerts.append(f"{name} unreachable: {type(exc).__name__}")
    summary["workers"] = worker_status
    for name, st in worker_status.items():
        if st == "error":
            alerts.append(f"{name} reports status=error")
        elif st == "degraded":
            # degraded is a warning, not an alert — don't escalate
            pass

    # ---------------- Postgres signals ----------------
    try:
        engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=False)
        with engine.connect() as c:
            # Connection load
            pg_conns = c.execute(sa.text(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE datname='panoptic'"
            )).scalar() or 0
            pg_max = int(c.execute(sa.text(
                "SELECT setting FROM pg_settings WHERE name='max_connections'"
            )).scalar() or 100)
            pct = 100.0 * pg_conns / pg_max
            summary["pg_conns"] = f"{pg_conns}/{pg_max}"
            if pct > MAX_PG_CONNS_PCT:
                alerts.append(f"pg_conns {pg_conns}/{pg_max} ({pct:.0f}%)")

            # Non-terminal jobs + oldest age
            non_term = c.execute(sa.text(
                "SELECT COUNT(*), MIN(updated_at) FROM panoptic_jobs "
                "WHERE state NOT IN ('succeeded','failed_terminal','degraded')"
            )).first()
            n = non_term[0] or 0
            oldest = non_term[1]
            summary["non_terminal_jobs"] = n
            if n > MAX_NONTERMINAL_JOBS:
                alerts.append(f"non_terminal_jobs={n} > {MAX_NONTERMINAL_JOBS}")
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                age = (now - oldest).total_seconds() / 60.0
                summary["oldest_nonterm_age_min"] = round(age, 1)
                if age > MAX_NONTERMINAL_AGE_MIN and n > 0:
                    alerts.append(f"oldest non-terminal job {age:.1f}min old")
        engine.dispose()
    except Exception as exc:
        alerts.append(f"pg probe failed: {exc}")
        summary["pg_conns"] = "?"

    # ---------------- Redis signals ----------------
    try:
        r = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        try:
            info = r.info("memory")
            used_mb = int(info.get("used_memory", 0)) / (1024 * 1024)
            summary["redis_mem_mb"] = round(used_mb, 1)
            if used_mb > MAX_REDIS_MEMORY_MB:
                alerts.append(f"redis_mem={used_mb:.0f}MB > {MAX_REDIS_MEMORY_MB}")

            # DLQ depth across every stream
            from shared.utils.streams import DLQ_FOR_JOB_TYPE
            dlq_total = 0
            for stream in DLQ_FOR_JOB_TYPE.values():
                dlq_total += r.xlen(stream)
            summary["dlq_depth"] = dlq_total
            if dlq_total > MAX_DLQ_DEPTH:
                alerts.append(f"DLQ depth={dlq_total}")
        finally:
            try:
                r.close()
                r.connection_pool.disconnect()
            except Exception:
                pass
    except Exception as exc:
        alerts.append(f"redis probe failed: {exc}")
        summary["redis_mem_mb"] = "?"
        summary["dlq_depth"] = "?"

    # ---------------- Disk ----------------
    for mount_label, mount_path in [("root", "/"), ("data", "/data/panoptic-store")]:
        if not os.path.exists(mount_path):
            continue
        try:
            stat = shutil.disk_usage(mount_path)
            pct = 100.0 * stat.used / stat.total
            summary[f"disk_{mount_label}_pct"] = round(pct, 1)
            if pct > MAX_DISK_USED_PCT:
                alerts.append(
                    f"disk {mount_path} {pct:.0f}% used "
                    f"({stat.used/1e9:.0f}GB / {stat.total/1e9:.0f}GB)"
                )
        except Exception as exc:
            summary[f"disk_{mount_label}_pct"] = "?"

    # ---------------- Append to log ----------------
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({**summary, "alerts": alerts}) + "\n")

    # ---------------- Print summary ----------------
    ok_workers = sum(1 for s in worker_status.values() if s == "ok")
    print(
        f"[{now.strftime('%Y-%m-%d %H:%M:%S')}UTC] "
        f"workers={ok_workers}/{len(worker_status)}ok  "
        f"pg={summary.get('pg_conns')}  "
        f"jobs_nt={summary.get('non_terminal_jobs')}  "
        f"dlq={summary.get('dlq_depth')}  "
        f"redis={summary.get('redis_mem_mb')}MB  "
        f"disk_data={summary.get('disk_data_pct')}%"
    )

    if alerts:
        print()
        for a in alerts:
            print(f"ALERT: {a}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
