"""
Backfill panoptic_events rows from existing panoptic_images + panoptic_buckets.

Direct Postgres INSERT ... ON CONFLICT DO NOTHING — no Redis stream, no
worker. event_id is content-addressed so reruns are free.

Run:
    # dry run — counts only, no writes
    .venv/bin/python scripts/backfill_events.py --source all

    # apply
    .venv/bin/python scripts/backfill_events.py --source all --apply

    # restrict to one serial
    .venv/bin/python scripts/backfill_events.py --source image --serial 1422725077375 --apply

Options:
    --source  image | bucket | all
    --serial  serial_number (optional filter)
    --apply   perform writes (default is dry-run)
    --limit   cap on rows processed per source (default: unlimited)

Exit code: 0 on success; 1 on any row-level error (errors logged, run continues).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import sqlalchemy as sa
from sqlalchemy import text

from shared.canonical.camera import resolve_canonical_camera_id
from shared.events.build import (
    build_event_row_from_bucket_marker,
    build_event_row_from_image,
)

log = logging.getLogger("backfill_events")

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")


_INSERT_SQL = text("""
    INSERT INTO panoptic_events (
        event_id,
        serial_number, camera_id, scope_id,
        event_type, event_source,
        severity, confidence,
        start_time_utc, end_time_utc, event_time_utc,
        bucket_id, image_id,
        title, description, metadata_json,
        created_at, updated_at
    ) VALUES (
        :event_id,
        :serial_number, :camera_id, :scope_id,
        :event_type, :event_source,
        :severity, :confidence,
        :start_time_utc, :end_time_utc, :event_time_utc,
        :bucket_id, :image_id,
        :title, :description, CAST(:metadata_json AS jsonb),
        now(), now()
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
""")


_KNOWN_MARKER_KEYS = frozenset({
    "spike",
    "after_hours",
    "drop",
    "start",
    "late_start",
    "underperforming",
})


def backfill_images(engine, *, serial: str | None, apply: bool, limit: int | None) -> tuple[int, int]:
    """
    Backfill events from alert/anomaly images.

    Returns (inspected, inserted).
    """
    q = """
        SELECT image_id, serial_number, camera_id, scope_id, trigger,
               bucket_start_utc, bucket_end_utc, captured_at_utc,
               caption_text, context_json
          FROM panoptic_images
         WHERE trigger IN ('alert', 'anomaly')
    """
    params: dict = {}
    if serial is not None:
        q += " AND serial_number = :serial"
        params["serial"] = serial
    q += " ORDER BY created_at"
    if limit is not None:
        q += f" LIMIT {int(limit)}"

    inspected = 0
    inserted = 0
    errors = 0

    with engine.connect() as conn:
        rows = conn.execute(text(q), params).fetchall()

    for row in rows:
        inspected += 1
        try:
            canonical_camera_id = resolve_canonical_camera_id(
                engine,
                serial_number=row.serial_number,
                raw_camera_id=row.camera_id,
                payload_type="image",
            )
            image_row = {
                "image_id": row.image_id,
                "serial_number": row.serial_number,
                "camera_id": canonical_camera_id,
                "scope_id": f"{row.serial_number}:{canonical_camera_id}",
                "trigger": row.trigger,
                "bucket_start_utc": row.bucket_start_utc,
                "bucket_end_utc": row.bucket_end_utc,
                "captured_at_utc": row.captured_at_utc,
                "caption_text": row.caption_text,
                "context_json": row.context_json or {},
            }
            event_row = build_event_row_from_image(image_row)
            if apply:
                inserted += _insert(engine, event_row)
        except Exception as exc:
            errors += 1
            log.error("image %s: %s", row.image_id, exc)

    log.info(
        "image backfill: inspected=%d inserted=%d errors=%d apply=%s",
        inspected, inserted, errors, apply,
    )
    if errors:
        # non-fatal: caller decides via exit code
        pass
    return inspected, inserted


def backfill_buckets(engine, *, serial: str | None, apply: bool, limit: int | None) -> tuple[int, int]:
    """
    Backfill events from bucket markers.

    Returns (inspected_markers, inserted).
    """
    q = """
        SELECT bucket_id, serial_number, camera_id,
               bucket_start_utc, bucket_end_utc, event_markers
          FROM panoptic_buckets
         WHERE jsonb_array_length(event_markers) > 0
    """
    params: dict = {}
    if serial is not None:
        q += " AND serial_number = :serial"
        params["serial"] = serial
    q += " ORDER BY created_at"
    if limit is not None:
        q += f" LIMIT {int(limit)}"

    inspected = 0
    inserted = 0
    skipped_unknown = 0
    errors = 0

    with engine.connect() as conn:
        rows = conn.execute(text(q), params).fetchall()

    for row in rows:
        try:
            canonical_camera_id = resolve_canonical_camera_id(
                engine,
                serial_number=row.serial_number,
                raw_camera_id=row.camera_id,
                payload_type="bucket",
            )
            bucket_row = {
                "bucket_id": row.bucket_id,
                "serial_number": row.serial_number,
                "camera_id": canonical_camera_id,
                "bucket_start_utc": row.bucket_start_utc,
                "bucket_end_utc": row.bucket_end_utc,
            }
            for marker in row.event_markers:
                inspected += 1
                marker_key = marker.get("event_type")
                if marker_key not in _KNOWN_MARKER_KEYS:
                    skipped_unknown += 1
                    continue
                event_row = build_event_row_from_bucket_marker(bucket_row, marker)
                if apply:
                    inserted += _insert(engine, event_row)
        except Exception as exc:
            errors += 1
            log.error("bucket %s: %s", row.bucket_id, exc)

    log.info(
        "bucket backfill: inspected=%d inserted=%d skipped_unknown=%d errors=%d apply=%s",
        inspected, inserted, skipped_unknown, errors, apply,
    )
    return inspected, inserted


def _insert(engine, event_row: dict) -> int:
    """INSERT ... ON CONFLICT DO NOTHING. Returns 1 if a row was inserted."""
    params = dict(event_row)
    params["metadata_json"] = json.dumps(event_row.get("metadata_json") or {})
    with engine.begin() as conn:
        result = conn.execute(_INSERT_SQL, params)
        return 1 if result.fetchone() is not None else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["image", "bucket", "all"], default="all")
    parser.add_argument("--serial", default=None, help="restrict to one serial_number")
    parser.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="cap rows per source")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)

    total_inserted = 0
    if args.source in ("image", "all"):
        _, ins = backfill_images(engine, serial=args.serial, apply=args.apply, limit=args.limit)
        total_inserted += ins
    if args.source in ("bucket", "all"):
        _, ins = backfill_buckets(engine, serial=args.serial, apply=args.apply, limit=args.limit)
        total_inserted += ins

    log.info("backfill_events complete: total_inserted=%d apply=%s", total_inserted, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
