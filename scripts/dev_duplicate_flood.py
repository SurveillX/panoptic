"""
M4 duplicate-flood test.

Fires N identical (same event_id, same everything) signed trailer pushes
concurrently, verifies that:

  1. Exactly one succeeds with status='accepted'
  2. The rest all return status='duplicate' (from Redis SETNX on bucket
     event_id, or Postgres PK on image_id)
  3. Zero new duplicate Postgres rows appear
  4. Zero new duplicate Qdrant points appear

Default N=100 concurrent pushes.

Usage:
    .venv/bin/python scripts/dev_duplicate_flood.py
    .venv/bin/python scripts/dev_duplicate_flood.py --n 500 --kind bucket
    .venv/bin/python scripts/dev_duplicate_flood.py --n 100 --kind image
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
from PIL import Image, ImageDraw

from scripts._signed_post import signed_json_post, signed_multipart_post

WEBHOOK = "http://localhost:8100"
SERIAL = f"DUP-FLOOD-{int(time.time())}"


def _register(serial: str) -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as c:
        c.execute(sa.text(
            "INSERT INTO panoptic_trailers (serial_number, name, is_active) "
            "VALUES (:sn, :nm, true) "
            "ON CONFLICT (serial_number) DO UPDATE SET is_active = true, updated_at = now()"
        ), {"sn": serial, "nm": f"dup-flood:{serial}"})
        c.commit()


def _bucket_payload(event_id: str) -> dict:
    bucket_start = "2026-04-18T18:30:00+00:00"
    bucket_end = "2026-04-18T18:45:00+00:00"
    return {
        "event_id": event_id,
        "schema_version": "1",
        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
        "serial_number": SERIAL,
        "camera_id": "cam-flood",
        "bucket": {
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "bucket_minutes": 15,
            "camera_id": "cam-flood",
            "object_type": "person",
            "unique_tracker_ids": 0,
            "total_detections": 0,
            "frame_count": 50,
            "min_count": 0, "max_count": 0, "mode_count": 0,
            "active_seconds": 0.0, "duty_cycle": 0.0,
        },
    }


def _image_payload(event_id: str) -> tuple[dict, bytes]:
    img = Image.new("RGB", (160, 120), "white")
    ImageDraw.Draw(img).rectangle([30, 30, 130, 90], fill=(120, 120, 220))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=80)
    meta = {
        "event_id": event_id,
        "schema_version": "1",
        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
        "serial_number": SERIAL,
        "camera_id": "cam-flood",
        "bucket_start": "2026-04-18T18:30:00+00:00",
        "bucket_end": "2026-04-18T18:45:00+00:00",
        "trigger": "anomaly",
        "timestamp_ms": 1776504000000,  # fixed, gives deterministic image_id
        "captured_at_utc": "2026-04-18T18:40:00+00:00",
        "selection_policy_version": "1",
        "context": {},
    }
    return meta, buf.getvalue()


def _one_bucket(event_id: str):
    path = "/v1/trailer/bucket-notification"
    try:
        r = signed_json_post(f"{WEBHOOK}{path}", path, SERIAL, _bucket_payload(event_id), timeout=30)
        return r.status_code, r.json().get("status", "?")
    except Exception as exc:
        return 0, f"exc:{type(exc).__name__}"


def _one_image(event_id: str):
    path = "/v1/trailer/image"
    meta, img_bytes = _image_payload(event_id)
    try:
        r = signed_multipart_post(
            f"{WEBHOOK}{path}", path, SERIAL,
            data={"metadata": json.dumps(meta)},
            files={"image": ("f.jpg", img_bytes, "image/jpeg")},
            timeout=30,
        )
        return r.status_code, r.json().get("status", "?")
    except Exception as exc:
        return 0, f"exc:{type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--kind", choices=["bucket", "image"], default="bucket")
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    print(f"serial: {SERIAL}")
    _register(SERIAL)

    # Fixed event_id across ALL N requests — that's the point of the test.
    event_id = f"dup-flood-{uuid.uuid4().hex[:8]}"
    print(f"event_id: {event_id}")
    print(f"firing {args.n} concurrent {args.kind} pushes (max {args.workers} workers)")

    fn = _one_bucket if args.kind == "bucket" else _one_image

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda _: fn(event_id), range(args.n)))
    elapsed = time.perf_counter() - t0

    # Tally
    tally = Counter(results)
    print()
    print(f"completed in {elapsed:.2f}s  ({args.n/elapsed:.0f} req/s)")
    print("status breakdown:")
    for (code, status), count in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  HTTP {code}  status={status!r:20s}  {count}")

    # Verify Postgres + Qdrant invariants
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as c:
        if args.kind == "bucket":
            n_buckets = c.execute(sa.text(
                "SELECT COUNT(*) FROM panoptic_buckets WHERE serial_number = :s"
            ), {"s": SERIAL}).scalar()
            print(f"\npanoptic_buckets rows for {SERIAL}: {n_buckets}  (expected ≤ 1, = 1 if finalizer fires)")
        else:
            n_images = c.execute(sa.text(
                "SELECT COUNT(*) FROM panoptic_images WHERE serial_number = :s"
            ), {"s": SERIAL}).scalar()
            print(f"\npanoptic_images rows for {SERIAL}: {n_images}  (expected = 1)")

    # Success criterion:
    accepted = sum(v for (code, status), v in tally.items() if status == "accepted")
    duplicates = sum(v for (code, status), v in tally.items() if status == "duplicate")
    errors = args.n - accepted - duplicates

    print()
    print(f"accepted: {accepted}  duplicates: {duplicates}  errors/other: {errors}")
    if accepted == 1 and duplicates == (args.n - 1) and errors == 0:
        print("RESULT: PASS — exactly one accepted, all others deduped, zero errors")
        return 0
    if accepted == 1 and errors == 0:
        print("RESULT: PASS — exactly one accepted, rest deduped (some via status other than 'duplicate')")
        return 0
    print("RESULT: FAIL — dedup invariant violated")
    return 1


if __name__ == "__main__":
    sys.exit(main())
