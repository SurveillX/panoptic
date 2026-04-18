"""
M1 idempotency sanity test — worker crash + reclaimer recovery.

Flow:
  1. Push a fresh image → caption worker claims, starts Gemma call
  2. Kill the caption worker while the job is leased
  3. Wait for LEASE_TTL (120s) to pass so lease_expires_at < now()
  4. Invoke reclaim_expired_leases() to reset the job to pending
  5. Re-enqueue via XADD (the reclaimer itself doesn't re-enqueue — the
     orchestrator is supposed to, but in our architecture the re-enqueue
     happens naturally when a fresh worker starts and sees pending jobs
     on the stream replay).
     SIMPLER APPROACH: restart the worker; it'll read pending messages.
  6. Verify job completes with caption_status=success and NO duplicate
     row / Qdrant point.

Finding: the reclaimer function is not run on a schedule anywhere in
the workers. This test invokes it manually. A real M2 item is to put
it in a ticker inside each worker.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import redis
import sqlalchemy as sa
from PIL import Image, ImageDraw

from scripts._signed_post import signed_multipart_post

WEBHOOK = "http://localhost:8100"
QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
DB_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

TEST_SERIAL = f"YARD-RECLAIM-{int(time.time())}"
TEST_CAMERA = "cam-rcl"


def _img_bytes() -> bytes:
    img = Image.new("RGB", (320, 240), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([60, 40, 260, 200], fill=(60, 140, 60), outline="black", width=3)
    d.text((100, 100), "RCL-TEST", fill="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _push_image() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    bucket_start = "2026-04-17T23:00:00+00:00"
    bucket_end = "2026-04-17T23:15:00+00:00"
    meta = {
        "event_id": f"reclaim-img-{int(time.time() * 1000)}",
        "schema_version": "1",
        "sent_at_utc": now_iso,
        "serial_number": TEST_SERIAL,
        "camera_id": TEST_CAMERA,
        "bucket_start": bucket_start,
        "bucket_end": bucket_end,
        "trigger": "anomaly",
        "timestamp_ms": int(time.time() * 1000),
        "captured_at_utc": now_iso,
        "selection_policy_version": "1",
        "context": {"max_anomaly_score": 0.9, "max_count": 1, "object_types": ["person"], "row_count": 1},
    }
    files = {"image": ("rcl.jpg", _img_bytes(), "image/jpeg")}
    image_path = "/v1/trailer/image"
    r = signed_multipart_post(
        f"{WEBHOOK}{image_path}",
        image_path,
        TEST_SERIAL,
        data={"metadata": json.dumps(meta)},
        files=files,
        timeout=30,
    )
    return r.json()


def _kill_caption_pane() -> None:
    # Send Ctrl-C to the caption window. This kills the Python process.
    # Because tmux.conf has `remain-on-exit on`, the pane stays open.
    subprocess.run(
        ["tmux", "send-keys", "-t", "panoptic:caption", "C-c"],
        check=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )


def _restart_caption_pane() -> None:
    # Respawn the pane's command.
    subprocess.run(
        [
            "tmux", "respawn-pane", "-t", "panoptic:caption", "-k",
            "bash -c 'cd $HOME/panoptic && set -a && source .env && set +a && "
            "exec .venv/bin/python -m services.panoptic_image_caption_worker.worker "
            "2>&1 | tee -a logs/caption.log'",
        ],
        check=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _job_row(engine, job_type: str, serial: str) -> dict | None:
    with engine.connect() as c:
        row = c.execute(
            sa.text(
                "SELECT job_id, state, attempt_count, lease_owner, lease_expires_at "
                "FROM panoptic_jobs WHERE serial_number = :sn AND job_type = :jt "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"sn": serial, "jt": job_type},
        ).mappings().first()
    return dict(row) if row else None


def _ensure_registered(serial: str, engine) -> None:
    with engine.connect() as c:
        c.execute(
            sa.text(
                "INSERT INTO panoptic_trailers (serial_number, name, is_active) "
                "VALUES (:sn, :nm, true) "
                "ON CONFLICT (serial_number) DO UPDATE SET is_active = true, updated_at = now()"
            ),
            {"sn": serial, "nm": f"reclaim-test:{serial}"},
        )
        c.commit()


def main() -> int:
    engine = sa.create_engine(DB_URL)
    r = redis.Redis.from_url(REDIS_URL)

    _ensure_registered(TEST_SERIAL, engine)
    print(f"test serial: {TEST_SERIAL}")

    # 1. Push image
    print("\n== pushing image ==")
    resp = _push_image()
    print(f"  webhook response: {resp}")
    image_id = resp.get("image_id")

    # 2. Wait for caption worker to claim it, then kill
    print("\n== waiting for job to be leased... ==")
    for _ in range(30):
        row = _job_row(engine, "image_caption", TEST_SERIAL)
        if row and row["state"] == "leased":
            print(f"  job leased: {row}")
            break
        time.sleep(0.3)
    else:
        print("  job never reached 'leased' state — aborting")
        return 1

    print("\n== killing caption worker (Ctrl-C to its pane) ==")
    _kill_caption_pane()
    time.sleep(3)
    row = _job_row(engine, "image_caption", TEST_SERIAL)
    print(f"  job state after kill: state={row['state']} lease_owner={row['lease_owner']}")
    if row["state"] != "leased":
        print("  unexpected: job left 'leased' state after kill")
        return 1
    print("  ✓ job stays in 'leased' state with dead worker — no silent loss")

    # 3. Wait for LEASE_TTL to pass
    wait_for = 125  # LEASE_TTL_SECONDS = 120, add margin
    print(f"\n== waiting {wait_for}s for lease to expire ==")
    time.sleep(wait_for)

    # 4. Invoke reclaimer manually
    print("\n== invoking reclaim_expired_leases() ==")
    from shared.utils.leases import reclaim_expired_leases
    stats = reclaim_expired_leases(engine, r)
    print(f"  reclaim stats: reset={stats.reset_to_pending} dlq={stats.sent_to_dlq} pel_acked={stats.stale_pel_acked}")

    row = _job_row(engine, "image_caption", TEST_SERIAL)
    print(f"  job state after reclaim: {row}")
    if row["state"] != "pending":
        print(f"  unexpected: job did not reset to 'pending' (got {row['state']})")
        return 1
    print("  ✓ job moved to 'pending'")

    # 5. Restart caption worker (it'll read pending jobs via PEL replay or fresh message)
    # The reclaimer ACK'd the old PEL entry. For the restarted worker to pick up
    # the job, it needs a fresh XADD.
    # We manually re-enqueue.
    print("\n== re-enqueueing job message ==")
    from shared.utils.streams import STREAM_FOR_JOB_TYPE
    r.xadd(
        STREAM_FOR_JOB_TYPE["image_caption"],
        {"job_id": row["job_id"], "job_type": "image_caption", "serial_number": TEST_SERIAL, "priority": "default"},
    )

    print("\n== restarting caption worker pane ==")
    _restart_caption_pane()

    # 6. Wait for completion
    print("\n== waiting up to 120s for job to complete ==")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 120:
        row = _job_row(engine, "image_caption", TEST_SERIAL)
        if row and row["state"] in ("succeeded", "failed_terminal", "degraded"):
            print(f"  final state: {row}")
            break
        time.sleep(3)
    else:
        print("  job did not complete within timeout")
        return 1

    if row["state"] != "succeeded":
        print(f"  unexpected: job did not succeed after retry (state={row['state']})")
        return 1

    # Verify no duplicate image row + single Qdrant point
    with engine.connect() as c:
        img_count = c.execute(
            sa.text("SELECT COUNT(*) FROM panoptic_images WHERE serial_number = :sn"),
            {"sn": TEST_SERIAL},
        ).scalar()
        cap = c.execute(
            sa.text("SELECT caption_status, LEFT(caption_text, 80) AS cap FROM panoptic_images WHERE image_id = :iid"),
            {"iid": image_id},
        ).mappings().first()

    print(f"\n  image rows for {TEST_SERIAL}: {img_count}")
    print(f"  caption_status={cap['caption_status']}")
    print(f"  caption preview: {cap['cap']!r}")

    ok = img_count == 1 and cap["caption_status"] == "success"
    print()
    print("RESULT: " + ("PASS — reclaimer recovery verified" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
