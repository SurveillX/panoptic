"""
M15 — operator-facing dashboard aggregate.

GET /v1/fleet/dashboard

Single response assembling the four things an operator wants to see at
a glance:

  1. Service health — parallel /healthz probes across all Panoptic
     workers + stateful services. Surfaces "event-producer has been off
     for 4 days" class of bug immediately.

  2. Ingest activity per trailer (last 1h + last 24h) — bucket counts,
     image counts split by Cognia trigger (alert / anomaly / baseline /
     novelty / pulled), event counts, last-received timestamps.

  3. Queue state — panoptic_jobs grouped by (job_type, state). Surfaces
     pending backlogs and degraded accumulations.

  4. Rate-limit watchlist — per-camera trigger counts in the last hour
     with a flag when a camera is at or near Cognia's per-camera cap
     (default 4/hour; env-overridable via PUSH_MAX_PER_CAMERA_PER_HOUR).

All SQL is indexed-scan over a bounded window — fine at current fleet
sizes. No schema changes.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from sqlalchemy import text as sa_text


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service-health probe
# ---------------------------------------------------------------------------

# (name, URL). Host-networked compose → localhost reaches every worker.
_SERVICE_ENDPOINTS: list[tuple[str, str]] = [
    ("webhook",          "http://localhost:8100/health"),
    ("caption",          f"http://localhost:{os.environ.get('CAPTION_HEALTH_PORT', '8201')}/healthz"),
    ("cap_embed",        f"http://localhost:{os.environ.get('CAP_EMBED_HEALTH_PORT', '8202')}/healthz"),
    ("summary",          f"http://localhost:{os.environ.get('SUMMARY_HEALTH_PORT', '8203')}/healthz"),
    ("sum_embed",        f"http://localhost:{os.environ.get('SUM_EMBED_HEALTH_PORT', '8204')}/healthz"),
    ("rollup",           f"http://localhost:{os.environ.get('ROLLUP_HEALTH_PORT', '8205')}/healthz"),
    ("img_embed",        f"http://localhost:{os.environ.get('IMAGE_EMBED_HEALTH_PORT', '8206')}/healthz"),
    ("event_producer",   f"http://localhost:{os.environ.get('EVENT_PRODUCER_HEALTH_PORT', '8207')}/healthz"),
    ("report_generator", f"http://localhost:{os.environ.get('REPORT_GENERATOR_HEALTH_PORT', '8208')}/healthz"),
    ("reclaimer",        f"http://localhost:{os.environ.get('RECLAIMER_HEALTH_PORT', '8210')}/healthz"),
    ("operator_ui",      "http://localhost:8400/healthz"),
    ("search_api",       "http://localhost:8600/health"),
    ("agent",            "http://localhost:8500/healthz"),
]

_HEALTH_PROBE_TIMEOUT_SEC: float = 3.0


def _probe_service(name: str, url: str) -> dict:
    t0 = time.perf_counter()
    try:
        r = httpx.get(url, timeout=_HEALTH_PROBE_TIMEOUT_SEC)
        probe_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            body = _safe_json(r)
            return {
                "name":       name,
                "url":        url,
                "healthy":    _is_healthy(body),
                "status":     body.get("status", "ok") if isinstance(body, dict) else "ok",
                "uptime_sec": body.get("uptime_sec") if isinstance(body, dict) else None,
                "probe_ms":   probe_ms,
                "error":      None,
            }
        return {
            "name":       name,
            "url":        url,
            "healthy":    False,
            "status":     f"http_{r.status_code}",
            "uptime_sec": None,
            "probe_ms":   probe_ms,
            "error":      f"{r.status_code}: {r.text[:120]}",
        }
    except httpx.TimeoutException:
        return {
            "name":       name,
            "url":        url,
            "healthy":    False,
            "status":     "timeout",
            "uptime_sec": None,
            "probe_ms":   int(_HEALTH_PROBE_TIMEOUT_SEC * 1000),
            "error":      f"timeout after {_HEALTH_PROBE_TIMEOUT_SEC}s",
        }
    except httpx.TransportError as exc:
        return {
            "name":       name,
            "url":        url,
            "healthy":    False,
            "status":     "unreachable",
            "uptime_sec": None,
            "probe_ms":   int((time.perf_counter() - t0) * 1000),
            "error":      str(exc)[:120],
        }


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {}


def _is_healthy(body) -> bool:
    """A service is healthy if status is "ok" or missing (200 alone is
    enough). "degraded" / "error" count as not-healthy for dashboard
    purposes — operator needs to see the nuance."""
    if not isinstance(body, dict):
        return True
    status = body.get("status")
    if status is None:
        return True
    return status == "ok"


def _probe_all_services(endpoints: list[tuple[str, str]] | None = None) -> list[dict]:
    """Serial probe — simpler than threading; each probe has a 3s cap and
    there are ~13 of them. Worst case ~40s if everything is down, but
    typical case is well under a second."""
    out = []
    for name, url in (endpoints or _SERVICE_ENDPOINTS):
        out.append(_probe_service(name, url))
    return out


# ---------------------------------------------------------------------------
# Ingest activity per trailer
# ---------------------------------------------------------------------------


_BUCKETS_SQL = sa_text(
    """
    SELECT serial_number,
           MAX(bucket_start_utc) AS last_bucket,
           COUNT(*) FILTER (WHERE bucket_start_utc >= NOW() - INTERVAL '1 hour')  AS count_1h,
           COUNT(*) FILTER (WHERE bucket_start_utc >= NOW() - INTERVAL '24 hours') AS count_24h
      FROM panoptic_buckets
     WHERE bucket_start_utc >= NOW() - INTERVAL '24 hours'
     GROUP BY serial_number
    """
)

_IMAGES_SQL = sa_text(
    """
    SELECT serial_number, trigger,
           COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 hour')  AS count_1h,
           COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS count_24h,
           MAX(created_at) AS last_image
      FROM panoptic_images
     WHERE created_at >= NOW() - INTERVAL '24 hours'
     GROUP BY serial_number, trigger
    """
)

_EVENTS_SQL = sa_text(
    """
    SELECT serial_number,
           COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 hour')  AS count_1h,
           COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS count_24h
      FROM panoptic_events
     WHERE created_at >= NOW() - INTERVAL '24 hours'
     GROUP BY serial_number
    """
)


def _fetch_ingest(engine) -> list[dict]:
    """Merge the three per-trailer queries into a single list."""
    per_trailer: dict[str, dict] = defaultdict(lambda: {
        "serial_number":   None,
        "last_bucket_utc": None,
        "last_image_utc":  None,
        "counts_1h":  {"buckets": 0, "images_total": 0, "events": 0,
                       "alert": 0, "anomaly": 0, "baseline": 0, "novelty": 0, "pulled": 0, "other": 0},
        "counts_24h": {"buckets": 0, "images_total": 0, "events": 0,
                       "alert": 0, "anomaly": 0, "baseline": 0, "novelty": 0, "pulled": 0, "other": 0},
    })

    with engine.connect() as conn:
        for row in conn.execute(_BUCKETS_SQL).fetchall():
            sn = row.serial_number
            per_trailer[sn]["serial_number"] = sn
            per_trailer[sn]["last_bucket_utc"] = _iso(row.last_bucket)
            per_trailer[sn]["counts_1h"]["buckets"] = int(row.count_1h)
            per_trailer[sn]["counts_24h"]["buckets"] = int(row.count_24h)

        for row in conn.execute(_IMAGES_SQL).fetchall():
            sn = row.serial_number
            per_trailer[sn]["serial_number"] = sn
            trig = row.trigger
            key = trig if trig in ("alert", "anomaly", "baseline", "novelty", "pulled") else "other"
            per_trailer[sn]["counts_1h"][key] += int(row.count_1h)
            per_trailer[sn]["counts_24h"][key] += int(row.count_24h)
            per_trailer[sn]["counts_1h"]["images_total"] += int(row.count_1h)
            per_trailer[sn]["counts_24h"]["images_total"] += int(row.count_24h)
            prev_last = per_trailer[sn]["last_image_utc"]
            last = _iso(row.last_image)
            if last and (prev_last is None or last > prev_last):
                per_trailer[sn]["last_image_utc"] = last

        for row in conn.execute(_EVENTS_SQL).fetchall():
            sn = row.serial_number
            per_trailer[sn]["serial_number"] = sn
            per_trailer[sn]["counts_1h"]["events"] = int(row.count_1h)
            per_trailer[sn]["counts_24h"]["events"] = int(row.count_24h)

    # Sort by most-recent-bucket descending (active trailers on top).
    return sorted(
        per_trailer.values(),
        key=lambda t: t.get("last_bucket_utc") or "",
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Queue state
# ---------------------------------------------------------------------------


_JOBS_SQL = sa_text(
    """
    SELECT job_type, state, COUNT(*) AS n
      FROM panoptic_jobs
     GROUP BY job_type, state
     ORDER BY job_type, state
    """
)


def _fetch_jobs(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(_JOBS_SQL).fetchall()
    return [{"job_type": r.job_type, "state": r.state, "count": int(r.n)} for r in rows]


# ---------------------------------------------------------------------------
# Rate-limit watch
# ---------------------------------------------------------------------------


_RATE_LIMIT_CAP: int = int(
    os.environ.get("PUSH_MAX_PER_CAMERA_PER_HOUR", "4")
)

# Flag cameras at this fraction of cap or above.
_RATE_WATCH_WARN_FRACTION: float = 0.75


_RATE_SQL = sa_text(
    """
    SELECT serial_number, camera_id, trigger, COUNT(*) AS n
      FROM panoptic_images
     WHERE created_at >= NOW() - INTERVAL '1 hour'
     GROUP BY serial_number, camera_id, trigger
    """
)


def _fetch_rate_limits(engine) -> dict:
    per_cam: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "serial_number":     None,
        "camera_id":         None,
        "trigger_counts_1h": {"alert": 0, "anomaly": 0, "baseline": 0, "novelty": 0, "pulled": 0, "other": 0},
        "total":             0,
    })

    with engine.connect() as conn:
        for row in conn.execute(_RATE_SQL).fetchall():
            key = (row.serial_number, row.camera_id)
            trig = row.trigger
            bucket = trig if trig in ("alert", "anomaly", "baseline", "novelty", "pulled") else "other"
            per_cam[key]["serial_number"] = row.serial_number
            per_cam[key]["camera_id"] = row.camera_id
            per_cam[key]["trigger_counts_1h"][bucket] += int(row.n)
            per_cam[key]["total"] += int(row.n)

    warn_floor = max(1, int(_RATE_LIMIT_CAP * _RATE_WATCH_WARN_FRACTION))
    watch = [
        {**cam, "cap": _RATE_LIMIT_CAP}
        for cam in per_cam.values()
        if cam["total"] >= warn_floor
    ]
    watch.sort(key=lambda c: c["total"], reverse=True)

    return {
        "cap_per_camera_per_hour": _RATE_LIMIT_CAP,
        "warn_floor":              warn_floor,
        "cameras_watch":           watch,
    }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_dashboard(engine) -> dict:
    """Assemble the dashboard JSON. Each section degrades independently;
    a failure in one query yields an empty section + an error marker."""

    out: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "errors":           [],
    }

    try:
        out["services"] = _probe_all_services()
    except Exception as exc:
        log.exception("dashboard: service probe failed")
        out["services"] = []
        out["errors"].append(f"services: {exc!s}")

    try:
        out["ingest"] = {"trailers": _fetch_ingest(engine)}
    except Exception as exc:
        log.exception("dashboard: ingest fetch failed")
        out["ingest"] = {"trailers": []}
        out["errors"].append(f"ingest: {exc!s}")

    try:
        out["jobs"] = _fetch_jobs(engine)
    except Exception as exc:
        log.exception("dashboard: jobs fetch failed")
        out["jobs"] = []
        out["errors"].append(f"jobs: {exc!s}")

    try:
        out["rate_limits"] = _fetch_rate_limits(engine)
    except Exception as exc:
        log.exception("dashboard: rate-limit fetch failed")
        out["rate_limits"] = {"cap_per_camera_per_hour": _RATE_LIMIT_CAP, "warn_floor": 0, "cameras_watch": []}
        out["errors"].append(f"rate_limits: {exc!s}")

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)
