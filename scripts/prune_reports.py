"""
Panoptic report retention / pruning.

Deletes generated HTML files on disk for reports whose window closed
more than --keep-days ago. The `panoptic_reports` row stays (small,
searchable) — we only reclaim disk and null out storage_path.

Run:
    # preview what would be deleted
    .venv/bin/python scripts/prune_reports.py --dry-run

    # apply (default: keep 90 days)
    .venv/bin/python scripts/prune_reports.py --apply

    # custom policy
    .venv/bin/python scripts/prune_reports.py --apply --keep-days 180

Per-row failures are logged and skipped; the script continues.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import text as sa_text

log = logging.getLogger("prune_reports")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keep-days", type=int, default=90,
                    help="retain HTML files from reports whose window_end_utc "
                         "is within this many days of now (default: 90)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="perform deletions")
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    p.add_argument("--limit", type=int, default=None,
                    help="max rows to process this run (operator safety)")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    apply = args.apply and not args.dry_run
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)

    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)

    sql = """
        SELECT report_id, serial_number, kind, window_end_utc, storage_path
          FROM panoptic_reports
         WHERE status = 'success'
           AND storage_path IS NOT NULL
           AND window_end_utc < :cutoff
         ORDER BY window_end_utc
    """
    if args.limit is not None:
        sql += f" LIMIT {int(args.limit)}"

    with engine.connect() as conn:
        rows = conn.execute(sa_text(sql), {"cutoff": cutoff}).mappings().all()

    log.info(
        "%s prune | keep_days=%d cutoff=%s | candidates=%d",
        "APPLY" if apply else "DRY-RUN", args.keep_days, cutoff.isoformat(), len(rows),
    )

    deleted_files = 0
    missing_files = 0
    failed = 0
    nulled = 0

    for row in rows:
        path = row["storage_path"]
        exists = os.path.exists(path)
        if not exists:
            missing_files += 1

        if not apply:
            log.info(
                "  [dry-run] would remove report_id=%s kind=%s sn=%s window_end=%s "
                "path=%s (exists=%s)",
                row["report_id"][:16], row["kind"], row["serial_number"],
                row["window_end_utc"].isoformat(), path, exists,
            )
            continue

        # Delete file first, then null storage_path in DB. If file delete
        # fails we don't null; that way a retry can pick up the residue.
        if exists:
            try:
                os.unlink(path)
                deleted_files += 1
            except OSError as exc:
                failed += 1
                log.error("  failed to unlink %s: %s", path, exc)
                continue

        with engine.begin() as tx:
            tx.execute(
                sa_text("""
                    UPDATE panoptic_reports
                       SET storage_path = NULL,
                           updated_at   = now()
                     WHERE report_id = :rid
                """),
                {"rid": row["report_id"]},
            )
        nulled += 1
        log.info(
            "  pruned report_id=%s kind=%s sn=%s path=%s",
            row["report_id"][:16], row["kind"], row["serial_number"], path,
        )

    log.info(
        "complete: candidates=%d deleted_files=%d missing_files=%d nulled=%d failed=%d apply=%s",
        len(rows), deleted_files, missing_files, nulled, failed, apply,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
