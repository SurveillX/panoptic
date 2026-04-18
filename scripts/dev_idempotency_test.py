"""
M1 idempotency sanity test — duplicate webhook replay.

Pushes one fixed bucket + image payload TWICE with the same event_ids and
deterministic image metadata, then verifies:

  1. the second bucket POST returns 'duplicate' status (Redis SETNX dedup)
  2. the second image POST returns 'duplicate' status (Postgres PK on image_id)
  3. no new panoptic_images or panoptic_summaries rows appear
  4. no new Qdrant points appear in either collection

Reads counts before and after; any delta == 0 => pass.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
from PIL import Image, ImageDraw

from scripts._signed_post import signed_json_post, signed_multipart_post

WEBHOOK = "http://localhost:8100"
QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
DB_URL = os.environ["DATABASE_URL"]

# Fixed payload for deterministic replay.
import uuid as _uuid
# Per-run unique event ids so rerunning the test against a non-empty DB works.
# Within a single run, push #1 and push #2 share these — that's the whole point.
_run_tag = _uuid.uuid4().hex[:8]
FIXED_BUCKET_EVENT_ID = f"idem-bucket-{_run_tag}"
FIXED_IMAGE_EVENT_ID = f"idem-image-{_run_tag}"
FIXED_SERIAL = f"YARD-IDEM-{_run_tag[:6].upper()}"
FIXED_CAMERA = "cam-idem"
FIXED_TRIGGER = "anomaly"
# anchored timestamp so image_id is deterministic across the two pushes
FIXED_TIMESTAMP_MS = 1776470000000
FIXED_CAPTURED_AT = "2026-04-17T22:33:20+00:00"
FIXED_BUCKET_START = "2026-04-17T22:30:00+00:00"
FIXED_BUCKET_END = "2026-04-17T22:45:00+00:00"


def _img_bytes() -> bytes:
    img = Image.new("RGB", (320, 240), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 280, 200], fill=(200, 40, 40), outline=(0, 0, 0), width=3)
    d.text((80, 100), "IDEM-TEST", fill="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _push_once() -> tuple[dict, dict]:
    now = datetime.now(timezone.utc).isoformat()

    bucket_payload = {
        "event_id": FIXED_BUCKET_EVENT_ID,
        "schema_version": "1",
        "sent_at_utc": now,
        "serial_number": FIXED_SERIAL,
        "camera_id": FIXED_CAMERA,
        "bucket": {
            "bucket_start": FIXED_BUCKET_START,
            "bucket_end": FIXED_BUCKET_END,
            "bucket_minutes": 15,
            "camera_id": FIXED_CAMERA,
            "object_type": "person",
            "unique_tracker_ids": 1, "total_detections": 10, "frame_count": 100,
            "min_count": 0, "max_count": 1, "mode_count": 0,
            "mean_count": 0.3, "std_dev_count": 0.4,
            "max_count_at": FIXED_BUCKET_START,
            "min_confidence": 0.6, "max_confidence": 0.9, "avg_confidence": 0.75,
            "first_detection_at": FIXED_BUCKET_START,
            "last_detection_at": FIXED_BUCKET_END,
            "active_seconds": 30.0, "duty_cycle": 0.03,
            "anomaly_score": 0.8, "anomaly_flag": 1,
        },
    }
    bucket_path = "/v1/trailer/bucket-notification"
    br = signed_json_post(f"{WEBHOOK}{bucket_path}", bucket_path, FIXED_SERIAL, bucket_payload, timeout=10)

    meta = {
        "event_id": FIXED_IMAGE_EVENT_ID,
        "schema_version": "1",
        "sent_at_utc": now,
        "serial_number": FIXED_SERIAL,
        "camera_id": FIXED_CAMERA,
        "bucket_start": FIXED_BUCKET_START,
        "bucket_end": FIXED_BUCKET_END,
        "trigger": FIXED_TRIGGER,
        "timestamp_ms": FIXED_TIMESTAMP_MS,
        "captured_at_utc": FIXED_CAPTURED_AT,
        "selection_policy_version": "1",
        "context": {"max_anomaly_score": 0.9, "max_count": 1, "object_types": ["person"], "row_count": 1},
    }
    files = {"image": ("idem.jpg", _img_bytes(), "image/jpeg")}
    image_path = "/v1/trailer/image"
    ir = signed_multipart_post(
        f"{WEBHOOK}{image_path}",
        image_path,
        FIXED_SERIAL,
        data={"metadata": json.dumps(meta)},
        files=files,
        timeout=30,
    )

    return br.json(), ir.json()


def _wait_drain(max_seconds: int = 240) -> None:
    """
    Wait for the full pipeline to drain for FIXED_SERIAL:
      - at least 1 image row exists with caption_embedding_status='success'
      - at least 1 summary row exists with embedding_status='complete'
        (this implies the bucket finalizer has fired and the summary path
        completed — finalizer has a 30s quiet period so this takes ~45s+)
      - no pending/leased jobs for FIXED_SERIAL
    """
    e = sa.create_engine(DB_URL)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < max_seconds:
        with e.connect() as c:
            img_done = c.execute(
                sa.text("SELECT COUNT(*) FROM panoptic_images WHERE serial_number = :sn "
                        "AND caption_status='success' AND caption_embedding_status='success'"),
                {"sn": FIXED_SERIAL},
            ).scalar()
            sum_done = c.execute(
                sa.text("SELECT COUNT(*) FROM panoptic_summaries WHERE serial_number = :sn "
                        "AND embedding_status='complete'"),
                {"sn": FIXED_SERIAL},
            ).scalar()
            pending = c.execute(
                sa.text(
                    "SELECT COUNT(*) FROM panoptic_jobs "
                    "WHERE serial_number = :sn AND state IN ('pending','leased')"
                ),
                {"sn": FIXED_SERIAL},
            ).scalar()
        if img_done >= 1 and sum_done >= 1 and pending == 0:
            return
        time.sleep(3)
    raise RuntimeError(f"pipeline did not fully drain for {FIXED_SERIAL} in {max_seconds}s")


def _counts() -> dict:
    e = sa.create_engine(DB_URL)
    with e.connect() as c:
        img = c.execute(sa.text("SELECT COUNT(*) FROM panoptic_images")).scalar()
        sm = c.execute(sa.text("SELECT COUNT(*) FROM panoptic_summaries")).scalar()
        buck = c.execute(sa.text("SELECT COUNT(*) FROM panoptic_buckets")).scalar()
        jobs = c.execute(sa.text("SELECT COUNT(*) FROM panoptic_jobs")).scalar()
    q_imgvec = httpx.get(f"{QDRANT}/collections/image_caption_vectors", timeout=10).json()["result"]["points_count"]
    q_sumvec = httpx.get(f"{QDRANT}/collections/panoptic_summaries", timeout=10).json()["result"]["points_count"]
    return {"images": img, "summaries": sm, "buckets": buck, "jobs": jobs,
            "q_image_vectors": q_imgvec, "q_summary_vectors": q_sumvec}


def _ensure_registered(serial: str) -> None:
    engine = sa.create_engine(DB_URL)
    with engine.connect() as c:
        c.execute(
            sa.text(
                "INSERT INTO panoptic_trailers (serial_number, name, is_active) "
                "VALUES (:sn, :nm, true) "
                "ON CONFLICT (serial_number) DO UPDATE SET is_active = true, updated_at = now()"
            ),
            {"sn": serial, "nm": f"idem-test:{serial}"},
        )
        c.commit()


def main() -> int:
    _ensure_registered(FIXED_SERIAL)
    print("== pre-replay counts ==")
    pre = _counts()
    for k, v in pre.items():
        print(f"  {k:20s} {v}")

    print("\n== push #1 (fresh) ==")
    b1, i1 = _push_once()
    print(f"  bucket: {b1}")
    print(f"  image:  {i1}")

    # Wait for pipeline to fully drain: no pending/leased jobs for this scope.
    # Bucket finalizer has 30s quiet period, so we poll until all jobs for this
    # serial are terminal (succeeded/degraded) before taking the mid snapshot.
    print("\nwaiting for pipeline to fully drain after push #1...")
    _wait_drain()

    print("\n== counts after push #1 (pipeline drained) ==")
    mid = _counts()
    for k, v in mid.items():
        delta = v - pre[k]
        print(f"  {k:20s} {v:>4d}  (+{delta})")

    print("\n== push #2 (duplicate replay) ==")
    b2, i2 = _push_once()
    print(f"  bucket: {b2}")
    print(f"  image:  {i2}")

    # Ensure nothing from #2 ever reaches processing (if it leaked, it would within 45s)
    print("\nwaiting 50s to confirm no new work is processed from push #2...")
    time.sleep(50)

    print("\n== counts after push #2 (should be identical to after #1) ==")
    post = _counts()
    for k, v in post.items():
        delta = v - mid[k]
        flag = "  ✓" if delta == 0 else "  ✗ LEAKED"
        print(f"  {k:20s} {v:>4d}  (Δ={delta}){flag}")

    # Hard gate: bucket status should be 'duplicate' on push 2; image status 'duplicate' too
    b_ok = b2.get("status") == "duplicate"
    i_ok = i2.get("status") == "duplicate"
    counts_ok = all(post[k] == mid[k] for k in post)

    print()
    print(f"bucket replay returned 'duplicate':  {'✓' if b_ok else '✗'} ({b2.get('status')})")
    print(f"image replay returned 'duplicate':   {'✓' if i_ok else '✗'} ({i2.get('status')})")
    print(f"no new rows or Qdrant points:        {'✓' if counts_ok else '✗'}")

    all_ok = b_ok and i_ok and counts_ok
    print()
    print("RESULT: " + ("PASS — idempotency verified" if all_ok else "FAIL — see deltas"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
