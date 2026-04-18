"""
Panoptic image retention / pruning.

Deletes `panoptic_images` rows (plus their JPEG on disk plus their
Qdrant points in both `image_caption_vectors` and `panoptic_image_vectors`)
that are older than the configured retention per trigger.

Rationale: baseline images are the bulk of the volume (10-100×
alerts/anomalies) and have low retrieval value — "yet another empty
yard" frames from 2 months ago rarely matter. Alerts/anomalies are the
interesting ones; keep them long or indefinitely.

Run:
    # show what would be deleted (safe, default)
    .venv/bin/python scripts/prune_images.py --dry-run

    # actually delete with default policy
    .venv/bin/python scripts/prune_images.py --apply

    # custom policy
    .venv/bin/python scripts/prune_images.py \\
        --keep-baseline-days 3 --keep-alert-days 365 \\
        --keep-anomaly-days 365 --apply

    # limit per run (operator safety)
    .venv/bin/python scripts/prune_images.py --apply --limit 1000

Defaults: baseline 7d, alert 180d, anomaly 365d.

Delete order per image: Qdrant points, then Postgres row, then disk file.
Any per-item failure is logged and skipped; the script continues.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
CAPTION_COLL = "image_caption_vectors"
IMAGE_VEC_COLL = "panoptic_image_vectors"


def _image_id_to_qdrant_id(image_id: str) -> str | None:
    """Mirror the production derivation. Returns None if image_id isn't
    a 64-char hex SHA256 (guarded against test rows)."""
    if len(image_id) < 32:
        return None
    try:
        return str(uuid.UUID(image_id[:32]))
    except Exception:
        return None


def _delete_qdrant_points(image_id: str) -> tuple[bool, bool]:
    """Returns (caption_ok, image_vec_ok)."""
    qid = _image_id_to_qdrant_id(image_id)
    if qid is None:
        return True, True  # nothing to delete; not a failure

    def _rm(collection: str) -> bool:
        try:
            r = httpx.post(
                f"{QDRANT_URL}/collections/{collection}/points/delete",
                json={"points": [qid]},
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    return _rm(CAPTION_COLL), _rm(IMAGE_VEC_COLL)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-baseline-days", type=int, default=7)
    ap.add_argument("--keep-alert-days", type=int, default=180)
    ap.add_argument("--keep-anomaly-days", type=int, default=365)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="redundant with default; kept for clarity")
    ap.add_argument("--limit", type=int, default=0,
                    help="max rows per run (0 = no limit)")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        print("--apply and --dry-run are mutually exclusive")
        return 2
    dry_run = not args.apply

    retention_days = {
        "baseline": args.keep_baseline_days,
        "alert": args.keep_alert_days,
        "anomaly": args.keep_anomaly_days,
    }

    now = datetime.now(timezone.utc)
    cutoffs = {
        trig: now - timedelta(days=days)
        for trig, days in retention_days.items()
    }

    print(f"retention policy (days):  "
          f"baseline={args.keep_baseline_days}  "
          f"alert={args.keep_alert_days}  "
          f"anomaly={args.keep_anomaly_days}")
    print(f"mode: {'DRY-RUN (no deletes)' if dry_run else 'APPLY'}")
    print(f"limit: {args.limit or 'none'}")
    print()

    db_url = os.environ["DATABASE_URL"]
    engine = sa.create_engine(db_url)

    # Pull candidates
    clauses = []
    params = {}
    for i, (trig, cutoff) in enumerate(cutoffs.items()):
        clauses.append(f"(trigger = :t{i} AND created_at < :c{i})")
        params[f"t{i}"] = trig
        params[f"c{i}"] = cutoff
    where = " OR ".join(clauses)
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""

    sql = f"""
        SELECT image_id, trigger, storage_path, created_at
          FROM panoptic_images
         WHERE {where}
         ORDER BY created_at
         {limit_sql}
    """
    with engine.connect() as c:
        candidates = c.execute(sa.text(sql), params).mappings().all()

    if not candidates:
        print("nothing to prune — all images within retention.")
        return 0

    # Categorize + totals
    by_trig = {"baseline": 0, "alert": 0, "anomaly": 0}
    total_bytes = 0
    for row in candidates:
        by_trig[row["trigger"]] = by_trig.get(row["trigger"], 0) + 1
        if row["storage_path"] and os.path.exists(row["storage_path"]):
            try:
                total_bytes += os.path.getsize(row["storage_path"])
            except Exception:
                pass

    print(f"candidates: {len(candidates)} images")
    for t, n in by_trig.items():
        print(f"  {t:8s} {n}")
    print(f"  disk to reclaim: {total_bytes/1e6:.1f} MB\n")

    if dry_run:
        print("dry-run — showing first 5 candidates:")
        for row in candidates[:5]:
            print(f"  {dict(row)}")
        print()
        print("re-run with --apply to actually delete.")
        return 0

    # Apply
    t0 = time.perf_counter()
    deleted_db = 0
    deleted_disk = 0
    deleted_qdrant_cap = 0
    deleted_qdrant_vec = 0
    errs = 0
    for row in candidates:
        image_id = row["image_id"]
        storage_path = row["storage_path"]

        # 1. Qdrant (both collections)
        cap_ok, vec_ok = _delete_qdrant_points(image_id)
        if cap_ok:
            deleted_qdrant_cap += 1
        if vec_ok:
            deleted_qdrant_vec += 1

        # 2. Postgres row
        try:
            with engine.connect() as c:
                c.execute(
                    sa.text("DELETE FROM panoptic_images WHERE image_id = :i"),
                    {"i": image_id},
                )
                c.commit()
            deleted_db += 1
        except Exception as exc:
            print(f"  DB delete failed {image_id[:12]}...: {exc}")
            errs += 1
            continue

        # 3. Disk file
        if storage_path and os.path.exists(storage_path):
            try:
                os.remove(storage_path)
                deleted_disk += 1
            except Exception as exc:
                print(f"  disk delete failed {storage_path}: {exc}")
                errs += 1

    elapsed = time.perf_counter() - t0
    print(f"done in {elapsed:.1f}s")
    print(f"  DB rows deleted:             {deleted_db}")
    print(f"  disk files deleted:          {deleted_disk}")
    print(f"  Qdrant caption points removed: {deleted_qdrant_cap}")
    print(f"  Qdrant image vec points removed: {deleted_qdrant_vec}")
    if errs:
        print(f"  errors: {errs}")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
