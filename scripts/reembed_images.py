"""
Enqueue image_embed jobs for every existing panoptic_images row that
hasn't been VL-embedded yet.

Use when:
  - M5 rollout — seed VL vectors for pre-existing images (one-time).
  - VL model upgrade — reset image_embedding_status and re-run.

By default skips rows already marked image_embedding_status='success'.
Pass --force to re-embed every row regardless.

    .venv/bin/python scripts/reembed_images.py
    .venv/bin/python scripts/reembed_images.py --force
    .venv/bin/python scripts/reembed_images.py --serial 1422725077375
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import redis
import sqlalchemy as sa

from shared.schemas.job import make_image_embed_key
from shared.utils.streams import enqueue_job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None, help="only enqueue for this serial")
    ap.add_argument(
        "--force",
        action="store_true",
        help="reset image_embedding_status to 'pending' before enqueueing",
    )
    args = ap.parse_args()

    db_url = os.environ["DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]

    engine = sa.create_engine(db_url)
    r = redis.Redis.from_url(redis_url)

    # Pick candidates
    where_clauses = ["caption_status = 'success'"]
    params: dict = {}
    if not args.force:
        where_clauses.append("image_embedding_status != 'success'")
    if args.serial:
        where_clauses.append("serial_number = :sn")
        params["sn"] = args.serial
    where_sql = " AND ".join(where_clauses)

    with engine.connect() as c:
        rows = c.execute(
            sa.text(f"""
                SELECT image_id, serial_number
                  FROM panoptic_images
                 WHERE {where_sql}
                 ORDER BY created_at
            """),
            params,
        ).mappings().all()

    if not rows:
        print("no images match — nothing to enqueue.")
        return 0

    print(f"found {len(rows)} image(s) to enqueue for image_embed")

    enqueued = 0
    skipped = 0
    with engine.connect() as c:
        if args.force:
            c.execute(
                sa.text(
                    "UPDATE panoptic_images SET image_embedding_status = 'pending' "
                    "WHERE image_id = ANY(:ids)"
                ),
                {"ids": [row["image_id"] for row in rows]},
            )
            c.commit()

        for row in rows:
            image_id = row["image_id"]
            serial = row["serial_number"]

            # Upsert the job row (idempotent — same job_key returns existing row)
            res = c.execute(
                sa.text("""
                    INSERT INTO panoptic_jobs (
                        job_key, serial_number, job_type, payload
                    ) VALUES (
                        :job_key, :sn, 'image_embed', CAST(:payload AS jsonb)
                    )
                    ON CONFLICT (job_key) DO NOTHING
                    RETURNING job_id
                """),
                {
                    "job_key": make_image_embed_key(image_id),
                    "sn": serial,
                    "payload": json.dumps({
                        "image_id": image_id,
                        "serial_number": serial,
                    }),
                },
            )
            new_row = res.fetchone()
            c.commit()

            if new_row is None:
                # Job already exists in Postgres; re-enqueue it on the stream
                # anyway so a worker picks it up. The worker's idempotency check
                # (image_embedding_status == 'success' → no-op) makes this safe.
                existing = c.execute(
                    sa.text("SELECT job_id FROM panoptic_jobs WHERE job_key = :k"),
                    {"k": make_image_embed_key(image_id)},
                ).scalar()
                enqueue_job(
                    r,
                    job_type="image_embed",
                    job_id=str(existing),
                    serial_number=serial,
                )
                skipped += 1
            else:
                enqueue_job(
                    r,
                    job_type="image_embed",
                    job_id=str(new_row.job_id),
                    serial_number=serial,
                )
                enqueued += 1

    print(f"done: new={enqueued}  replayed={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
