"""
Seed ~20 varied synthetic trailer payloads for M1 relevance-harness work.

Each scene has a unique (serial_number, camera_id) so each gets its own
bucket and its own summary. The drawn scenes are visually distinctive
enough that Gemma-4-26b will caption them differently — giving the
relevance harness a meaningful surface to rank against.

The labels dict at the top is the ground-truth tagging used by the
relevance harness (tests/relevance/queries.yaml).
"""

from __future__ import annotations

import io
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont

from scripts._signed_post import signed_json_post, signed_multipart_post

WEBHOOK = "http://localhost:8100"

# ---------------------------------------------------------------------------
# Scene catalog — 20 distinct scenes, each bound to a (trailer, camera, trigger)
# ---------------------------------------------------------------------------

# Ground-truth labels. The relevance harness uses these to score queries.
SCENES = [
    # idx, trailer,      camera,   trigger,    label,                    color_primary, shape_hint
    ( 1, "YARD-A-001",   "cam-01", "anomaly",  "person standing",         (100,  60,  60), "person"),
    ( 2, "YARD-A-001",   "cam-02", "alert",    "two people walking",      (110,  70,  70), "two_people"),
    ( 3, "YARD-A-001",   "cam-03", "baseline", "empty parking area",      (120, 120, 120), "empty"),
    ( 4, "YARD-A-001",   "cam-04", "anomaly",  "red vehicle in lot",      (200,  40,  40), "red_car"),
    ( 5, "YARD-A-001",   "cam-05", "alert",    "blue truck parked",       ( 40,  80, 200), "blue_truck"),

    ( 6, "YARD-B-002",   "cam-01", "alert",    "forklift with load",      (230, 200,  30), "forklift"),
    ( 7, "YARD-B-002",   "cam-02", "anomaly",  "fire or flames visible",  (220,  80,  30), "fire"),
    ( 8, "YARD-B-002",   "cam-03", "alert",    "smoke rising",            (160, 160, 160), "smoke"),
    ( 9, "YARD-B-002",   "cam-04", "anomaly",  "water puddle on ground",  ( 60, 130, 220), "water"),
    (10, "YARD-B-002",   "cam-05", "baseline", "stacked boxes",           (160, 110,  60), "boxes"),

    (11, "YARD-C-003",   "cam-01", "alert",    "orange traffic cone",     (240, 130,  30), "cone"),
    (12, "YARD-C-003",   "cam-02", "anomaly",  "fallen ladder",           (180, 160, 100), "ladder"),
    (13, "YARD-C-003",   "cam-03", "alert",    "red stop sign",           (220,  30,  30), "stop_sign"),
    (14, "YARD-C-003",   "cam-04", "baseline", "closed gate barrier",     (130, 130, 140), "gate"),
    (15, "YARD-C-003",   "cam-05", "anomaly",  "person carrying box",     (105,  65,  65), "carrying"),

    (16, "YARD-D-004",   "cam-01", "anomaly",  "bicycle leaning",         (200, 200,  40), "bicycle"),
    (17, "YARD-D-004",   "cam-02", "alert",    "dog or small animal",     (150, 100,  60), "animal"),
    (18, "YARD-D-004",   "cam-03", "baseline", "hard hat on ground",      (250, 200,  30), "hardhat"),
    (19, "YARD-D-004",   "cam-04", "alert",    "spilled liquid",          ( 50, 110, 170), "spill"),
    (20, "YARD-D-004",   "cam-05", "anomaly",  "open package on ground",  (170, 120,  70), "package"),
]


# ---------------------------------------------------------------------------
# Tiny drawing routines — shape_hint drives what gets drawn.
# We keep these deliberately simple so the pipeline is exercised, not art.
# ---------------------------------------------------------------------------


def _draw_scene(draw: ImageDraw.ImageDraw, w: int, h: int, shape: str, color) -> None:
    ground_y = int(h * 0.85)
    draw.rectangle([0, ground_y, w, h], fill=(80, 80, 80))  # ground
    cx, cy = w // 2, int(h * 0.55)

    if shape == "person":
        draw.rectangle([cx - 40, cy - 20, cx + 40, cy + 120], fill=color)
        draw.ellipse([cx - 35, cy - 90, cx + 35, cy - 20], fill=(220, 190, 170))
    elif shape == "two_people":
        for off in (-80, 80):
            draw.rectangle([cx + off - 30, cy - 20, cx + off + 30, cy + 120], fill=color)
            draw.ellipse([cx + off - 28, cy - 80, cx + off + 28, cy - 20], fill=(220, 190, 170))
    elif shape == "empty":
        for i in range(-3, 4):
            x = cx + i * 90
            draw.line([x, ground_y - 120, x, ground_y], fill=(255, 255, 255), width=3)
    elif shape == "red_car":
        draw.rectangle([cx - 140, cy + 20, cx + 140, cy + 120], fill=color)
        draw.polygon([(cx - 100, cy + 20), (cx - 60, cy - 40), (cx + 60, cy - 40), (cx + 100, cy + 20)], fill=color)
        draw.ellipse([cx - 130, cy + 100, cx - 60, cy + 170], fill=(20, 20, 20))
        draw.ellipse([cx + 60, cy + 100, cx + 130, cy + 170], fill=(20, 20, 20))
    elif shape == "blue_truck":
        draw.rectangle([cx - 200, cy - 30, cx + 50, cy + 140], fill=color)
        draw.rectangle([cx + 50, cy + 30, cx + 200, cy + 140], fill=(color[0] + 30, color[1] + 30, color[2]))
        for wx in (cx - 170, cx - 90, cx + 120):
            draw.ellipse([wx - 30, cy + 110, wx + 30, cy + 170], fill=(20, 20, 20))
    elif shape == "forklift":
        draw.rectangle([cx - 100, cy - 10, cx + 80, cy + 130], fill=color)
        draw.rectangle([cx + 80, cy - 80, cx + 100, cy + 130], fill=(80, 80, 80))  # mast
        draw.rectangle([cx + 100, cy + 60, cx + 220, cy + 75], fill=(80, 80, 80))  # fork
        draw.rectangle([cx + 100, cy + 100, cx + 220, cy + 115], fill=(80, 80, 80))
        draw.ellipse([cx - 80, cy + 100, cx - 20, cy + 160], fill=(20, 20, 20))
        draw.ellipse([cx + 20, cy + 100, cx + 80, cy + 160], fill=(20, 20, 20))
    elif shape == "fire":
        for i, a in enumerate([0, 60, -60, 30, -30]):
            tip_y = cy - 40 - i * 15
            draw.polygon(
                [(cx + a, ground_y), (cx + a - 40, cy + 20), (cx + a, tip_y), (cx + a + 40, cy + 20)],
                fill=(color[0], color[1] - i * 20, color[2]),
            )
    elif shape == "smoke":
        for r, dy in [(90, 0), (70, -80), (50, -140), (40, -200)]:
            draw.ellipse([cx - r, cy + dy - r, cx + r, cy + dy + r], fill=color)
    elif shape == "water":
        draw.ellipse([cx - 180, ground_y - 60, cx + 180, ground_y + 40], fill=color)
        draw.ellipse([cx - 120, ground_y - 40, cx + 140, ground_y + 20], fill=(color[0] + 30, color[1] + 30, 255))
    elif shape == "boxes":
        for row in range(3):
            for col in range(2):
                x = cx - 80 + col * 70
                y = ground_y - (row + 1) * 70
                draw.rectangle([x, y, x + 65, y + 65], fill=color)
                draw.rectangle([x, y, x + 65, y + 65], outline=(80, 50, 20), width=3)
    elif shape == "cone":
        draw.polygon([(cx, cy - 100), (cx - 60, ground_y), (cx + 60, ground_y)], fill=color)
        draw.rectangle([cx - 30, cy - 30, cx + 30, cy - 10], fill=(255, 255, 255))
    elif shape == "ladder":
        draw.rectangle([cx - 150, ground_y - 15, cx + 100, ground_y], fill=color)  # fallen flat
        for i in range(5):
            x = cx - 130 + i * 40
            draw.rectangle([x, ground_y - 15, x + 10, ground_y], fill=(100, 80, 40))
    elif shape == "stop_sign":
        pts = []
        import math as m
        for i in range(8):
            a = m.pi / 8 + i * m.pi / 4
            pts.append((cx + 80 * m.cos(a), cy + 80 * m.sin(a)))
        draw.polygon(pts, fill=color)
        draw.text((cx - 30, cy - 10), "STOP", fill="white")
    elif shape == "gate":
        draw.rectangle([cx - 20, cy - 80, cx + 20, ground_y], fill=color)
        draw.rectangle([cx + 20, cy - 20, cx + 280, cy + 5], fill=color)
        for i in range(5):
            draw.line([cx + 40 + i * 50, cy - 20, cx + 40 + i * 50, cy + 5], fill=(200, 200, 50), width=3)
    elif shape == "carrying":
        draw.rectangle([cx - 40, cy - 20, cx + 40, cy + 120], fill=color)
        draw.ellipse([cx - 30, cy - 80, cx + 30, cy - 20], fill=(220, 190, 170))
        draw.rectangle([cx + 40, cy + 10, cx + 100, cy + 70], fill=(160, 110, 60))  # box
    elif shape == "bicycle":
        draw.ellipse([cx - 140, cy + 40, cx - 40, cy + 140], outline=color, width=6)
        draw.ellipse([cx + 40, cy + 40, cx + 140, cy + 140], outline=color, width=6)
        draw.line([cx - 90, cy + 90, cx + 90, cy + 90], fill=color, width=5)
        draw.line([cx + 20, cy + 90, cx + 60, cy + 30], fill=color, width=5)
        draw.line([cx - 40, cy + 90, cx, cy + 30], fill=color, width=5)
    elif shape == "animal":
        draw.ellipse([cx - 80, cy + 30, cx + 80, cy + 110], fill=color)   # body
        draw.ellipse([cx + 50, cy + 10, cx + 120, cy + 70], fill=color)   # head
        for lx in (cx - 60, cx - 20, cx + 30, cx + 70):
            draw.rectangle([lx, cy + 100, lx + 10, cy + 140], fill=color)
    elif shape == "hardhat":
        draw.pieslice([cx - 60, cy + 30, cx + 60, cy + 130], 180, 360, fill=color)
        draw.rectangle([cx - 70, cy + 80, cx + 70, cy + 95], fill=(color[0] - 30, color[1] - 30, 30))
    elif shape == "spill":
        draw.polygon(
            [(cx - 150, ground_y), (cx - 100, ground_y - 50), (cx, ground_y - 60),
             (cx + 120, ground_y - 30), (cx + 180, ground_y)],
            fill=color,
        )
    elif shape == "package":
        draw.rectangle([cx - 80, cy + 40, cx + 80, cy + 140], fill=color)
        draw.line([cx - 80, cy + 90, cx + 80, cy + 90], fill=(80, 50, 20), width=3)
        draw.line([cx, cy + 40, cx, cy + 140], fill=(80, 50, 20), width=3)
        # flap peeled back
        draw.polygon([(cx - 80, cy + 40), (cx, cy + 40), (cx - 40, cy + 10)], fill=(color[0] + 30, color[1] + 30, 60))


def make_image(shape_hint: str, color) -> bytes:
    img = Image.new("RGB", (640, 360), "white")
    d = ImageDraw.Draw(img)
    _draw_scene(d, 640, 360, shape_hint, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------


def _push_bucket(now, bucket_start, bucket_end, serial, camera, trigger):
    payload = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "sent_at_utc": now.isoformat(),
        "serial_number": serial,
        "camera_id": camera,
        "bucket": {
            "bucket_start": bucket_start.isoformat(),
            "bucket_end": bucket_end.isoformat(),
            "bucket_minutes": 15,
            "camera_id": camera,
            "object_type": "person" if trigger != "baseline" else "vehicle",
            "unique_tracker_ids": 2 if trigger != "baseline" else 0,
            "total_detections": 45 if trigger != "baseline" else 2,
            "frame_count": 450,
            "min_count": 0,
            "max_count": 3 if trigger != "baseline" else 1,
            "mode_count": 1 if trigger != "baseline" else 0,
            "mean_count": 1.2 if trigger != "baseline" else 0.1,
            "std_dev_count": 0.8,
            "max_count_at": (bucket_start + timedelta(minutes=3)).isoformat(),
            "min_confidence": 0.55,
            "max_confidence": 0.95 if trigger == "alert" else 0.82,
            "avg_confidence": 0.75,
            "first_detection_at": bucket_start.isoformat(),
            "last_detection_at": bucket_end.isoformat(),
            "active_seconds": 150.0 if trigger != "baseline" else 8.0,
            "duty_cycle": 0.17 if trigger != "baseline" else 0.01,
            "anomaly_score": 0.72 if trigger == "anomaly" else 0.18,
            "anomaly_flag": 1 if trigger == "anomaly" else 0,
        },
    }
    path = "/v1/trailer/bucket-notification"
    r = signed_json_post(f"{WEBHOOK}{path}", path, serial, payload, timeout=10)
    return r.status_code, r.text


def _push_image(now, bucket_start, bucket_end, serial, camera, trigger, shape_hint, color):
    image_bytes = make_image(shape_hint, color)
    meta = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "sent_at_utc": now.isoformat(),
        "serial_number": serial,
        "camera_id": camera,
        "bucket_start": bucket_start.isoformat(),
        "bucket_end": bucket_end.isoformat(),
        "trigger": trigger,
        "timestamp_ms": int(now.timestamp() * 1000) if trigger != "baseline" else None,
        "captured_at_utc": now.isoformat() if trigger != "baseline" else None,
        "selection_policy_version": "1",
        "context": {
            "max_anomaly_score": 0.87 if trigger == "anomaly" else 0.15,
            "max_count": 3 if trigger != "baseline" else 1,
            "object_types": ["person"] if trigger == "anomaly" else ["vehicle"],
            "row_count": 1,
        },
    }
    files = {"image": ("frame.jpg", image_bytes, "image/jpeg")}
    data = {"metadata": json.dumps(meta)}
    path = "/v1/trailer/image"
    r = signed_multipart_post(
        f"{WEBHOOK}{path}", path, serial, data=data, files=files, timeout=30,
    )
    return r.status_code, r.text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _register_trailers_if_needed() -> None:
    """Upsert all seed serials into panoptic_trailers so auth succeeds."""
    import os
    import sqlalchemy as sa
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return
    engine = sa.create_engine(db_url)
    serials = sorted({s[1] for s in SCENES})
    with engine.connect() as c:
        for sn in serials:
            c.execute(
                sa.text(
                    "INSERT INTO panoptic_trailers (serial_number, name, is_active) "
                    "VALUES (:sn, :nm, true) "
                    "ON CONFLICT (serial_number) DO UPDATE SET is_active = true, updated_at = now()"
                ),
                {"sn": sn, "nm": f"synthetic:{sn}"},
            )
        c.commit()
    print(f"registered {len(serials)} synthetic trailer serials in panoptic_trailers\n")


def main() -> int:
    _register_trailers_if_needed()
    now = datetime.now(timezone.utc)
    # Use the PREVIOUS 15-min bucket (so bucket_end < now)
    this_bucket_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    bucket_start = this_bucket_start - timedelta(minutes=15)
    bucket_end = this_bucket_start

    print(f"seeding {len(SCENES)} scenes into bucket {bucket_start.isoformat()} .. {bucket_end.isoformat()}\n")

    ok_buckets = 0
    ok_images = 0
    for idx, serial, camera, trigger, label, color, shape_hint in SCENES:
        b_code, _ = _push_bucket(now, bucket_start, bucket_end, serial, camera, trigger)
        i_code, i_body = _push_image(now, bucket_start, bucket_end, serial, camera, trigger, shape_hint, color)
        ok_buckets += (b_code == 200)
        ok_images += (i_code == 200)
        print(f"  [{idx:2d}] {serial}/{camera} trig={trigger:8s} shape={shape_hint:10s} label={label!r:30s} bucket={b_code} image={i_code}")
        # tiny stagger so the webhook isn't rate-hammered
        time.sleep(0.05)

    print(f"\nbuckets ok: {ok_buckets}/{len(SCENES)}  images ok: {ok_images}/{len(SCENES)}")
    return 0 if (ok_buckets == len(SCENES) and ok_images == len(SCENES)) else 1


if __name__ == "__main__":
    sys.exit(main())
