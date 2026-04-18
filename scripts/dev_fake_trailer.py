"""
Dev-only: simulate a trailer pushing a bucket notification + an image.
Run from the panoptic repo root with the venv active (.env already loaded).

    .venv/bin/python scripts/dev_fake_trailer.py
"""

from __future__ import annotations

import io
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image, ImageDraw

from scripts._signed_post import signed_json_post, signed_multipart_post

WEBHOOK = "http://localhost:8100"
SERIAL = "TEST-SPARK-001"
CAMERA = "cam-01"


def _ensure_registered(serial: str) -> None:
    import os
    import sqlalchemy as sa
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return
    engine = sa.create_engine(db_url)
    with engine.connect() as c:
        c.execute(
            sa.text(
                "INSERT INTO panoptic_trailers (serial_number, name, is_active) "
                "VALUES (:sn, :nm, true) "
                "ON CONFLICT (serial_number) DO UPDATE SET is_active = true, updated_at = now()"
            ),
            {"sn": serial, "nm": f"dev:{serial}"},
        )
        c.commit()


def main() -> int:
    _ensure_registered(SERIAL)
    now = datetime.now(timezone.utc)
    bucket_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    bucket_end = bucket_start + timedelta(minutes=15)

    # ------------------------------------------------------------------
    # 1. Bucket notification
    # ------------------------------------------------------------------
    bucket_payload = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "sent_at_utc": now.isoformat(),
        "serial_number": SERIAL,
        "camera_id": CAMERA,
        "bucket": {
            "bucket_start": bucket_start.isoformat(),
            "bucket_end": bucket_end.isoformat(),
            "bucket_minutes": 15,
            "camera_id": CAMERA,
            "object_type": "person",
            "unique_tracker_ids": 3,
            "total_detections": 120,
            "frame_count": 450,
            "min_count": 0,
            "max_count": 4,
            "mode_count": 2,
            "mean_count": 1.8,
            "std_dev_count": 1.1,
            "max_count_at": (bucket_start + timedelta(minutes=3)).isoformat(),
            "min_confidence": 0.62,
            "max_confidence": 0.98,
            "avg_confidence": 0.84,
            "first_detection_at": bucket_start.isoformat(),
            "last_detection_at": bucket_end.isoformat(),
            "active_seconds": 240.0,
            "duty_cycle": 0.27,
            "anomaly_score": 0.12,
            "anomaly_flag": 0,
        },
    }

    path = "/v1/trailer/bucket-notification"
    r = signed_json_post(f"{WEBHOOK}{path}", path, SERIAL, bucket_payload, timeout=10)
    print(f"bucket:  {r.status_code}  {r.text}")

    # ------------------------------------------------------------------
    # 2. Image push
    # ------------------------------------------------------------------
    img = Image.new("RGB", (640, 360), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([100, 140, 180, 300], fill=(100, 60, 60))   # body
    d.ellipse([105, 90, 175, 150], fill=(220, 190, 170))    # head
    d.rectangle([0, 300, 640, 360], fill=(80, 80, 80))      # ground
    d.text((220, 20), "SIMULATED FRAME", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)

    meta = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "sent_at_utc": now.isoformat(),
        "serial_number": SERIAL,
        "camera_id": CAMERA,
        "bucket_start": bucket_start.isoformat(),
        "bucket_end": bucket_end.isoformat(),
        "trigger": "anomaly",
        "timestamp_ms": int(now.timestamp() * 1000),
        "captured_at_utc": now.isoformat(),
        "selection_policy_version": "1",
        "context": {
            "max_anomaly_score": 0.87,
            "max_count": 4,
            "object_types": ["person"],
            "row_count": 1,
        },
    }

    files = {"image": ("frame.jpg", buf.getvalue(), "image/jpeg")}
    data = {"metadata": json.dumps(meta)}

    path = "/v1/trailer/image"
    r = signed_multipart_post(
        f"{WEBHOOK}{path}", path, SERIAL, data=data, files=files, timeout=30,
    )
    print(f"image:   {r.status_code}  {r.text}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
