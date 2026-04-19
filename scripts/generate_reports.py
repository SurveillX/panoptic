"""
Panoptic report generation CLI / cron driver.

Enqueues report_generate jobs via the SAME path as POST /v1/reports/*.
Does NOT invoke the executor directly — this guarantees cron and HTTP
share identical semantics (status transitions, metadata, idempotency).

Run:
    # daily for one trailer
    .venv/bin/python scripts/generate_reports.py \\
        --daily --serial 1422725077375 --date 2026-04-18 --apply

    # daily for every active trailer
    .venv/bin/python scripts/generate_reports.py \\
        --daily --all-active --date 2026-04-18 --apply

    # weekly for one trailer (Mon-anchored ISO week)
    .venv/bin/python scripts/generate_reports.py \\
        --weekly --serial 1422725077375 --iso-week 2026W16 --apply

    # dry-run: print what WOULD be enqueued, skip DB + Redis writes
    .venv/bin/python scripts/generate_reports.py \\
        --daily --all-active --date 2026-04-18

Defaults: dry-run. Pass --apply to actually enqueue.

Exit codes:
    0  — all targets enqueued (or dry-run complete)
    1  — at least one target failed to enqueue (others still tried)
    2  — invalid invocation
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import sqlalchemy as sa
from sqlalchemy import text as sa_text

from services.search_api.reports import (
    _daily_window,
    _enqueue_report,
    _weekly_window,
)
from shared.utils.redis_client import get_redis_client

log = logging.getLogger("generate_reports")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")


def _list_active_trailers(engine) -> list[str]:
    """Return sorted serial_numbers for is_active=true trailers."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text("""
                SELECT serial_number
                  FROM panoptic_trailers
                 WHERE is_active = true
                 ORDER BY serial_number
            """)
        ).fetchall()
    return [r.serial_number for r in rows]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--daily", action="store_true")
    kind.add_argument("--weekly", action="store_true")

    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--serial", help="single trailer serial_number")
    scope.add_argument("--all-active", action="store_true",
                        help="iterate every is_active=true trailer")

    p.add_argument("--date", help="YYYY-MM-DD (required for --daily)")
    p.add_argument("--iso-week", help="YYYYWnn (required for --weekly)")

    p.add_argument("--apply", action="store_true",
                    help="actually enqueue (default: dry-run)")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = _parse_args()
    kind: str = "daily" if args.daily else "weekly"

    if kind == "daily" and not args.date:
        log.error("--daily requires --date YYYY-MM-DD")
        return 2
    if kind == "weekly" and not args.iso_week:
        log.error("--weekly requires --iso-week YYYYWnn")
        return 2

    try:
        if kind == "daily":
            window_start, window_end = _daily_window(args.date)
        else:
            window_start, window_end = _weekly_window(args.iso_week)
    except Exception as exc:
        log.error("invalid window: %s", exc)
        return 2

    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()

    # Resolve target serials.
    if args.serial:
        serials = [args.serial]
    else:
        serials = _list_active_trailers(engine)
        if not serials:
            log.warning("no active trailers found")
            return 0

    label = args.date if kind == "daily" else args.iso_week
    log.info(
        "%s report generation | kind=%s | window=%s | targets=%d | apply=%s",
        "ENQUEUE" if args.apply else "DRY-RUN",
        kind, label, len(serials), args.apply,
    )

    enqueued = 0
    failed = 0
    skipped = 0
    for sn in serials:
        if not args.apply:
            log.info("  [dry-run] would enqueue serial=%s kind=%s window=%s",
                     sn, kind, label)
            continue
        try:
            report_id, status = _enqueue_report(
                engine=engine, r=r, serial_number=sn, kind=kind,
                window_start=window_start, window_end=window_end,
            )
            if status == "success":
                skipped += 1
                log.info("  skipped serial=%s (already success) report_id=%s",
                         sn, report_id[:16])
            else:
                enqueued += 1
                log.info("  enqueued serial=%s status=%s report_id=%s",
                         sn, status, report_id[:16])
        except Exception as exc:
            failed += 1
            log.error("  failed serial=%s: %s", sn, exc)

    log.info(
        "complete: enqueued=%d skipped=%d failed=%d total=%d",
        enqueued, skipped, failed, len(serials),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
