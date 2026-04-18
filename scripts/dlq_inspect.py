"""
DLQ inspection CLI.

Lists entries across every panoptic:dlq:* stream, with the most recent
first. Correlates with panoptic_jobs via job_id so you can see the
job's current state and which stream the original payload belongs to.

    .venv/bin/python scripts/dlq_inspect.py                # all streams
    .venv/bin/python scripts/dlq_inspect.py --job-type caption_embed
    .venv/bin/python scripts/dlq_inspect.py --serial 1422725077375
    .venv/bin/python scripts/dlq_inspect.py --limit 20
"""

from __future__ import annotations

import argparse
import os
import sys

import redis
import sqlalchemy as sa

from shared.utils.streams import DLQ_FOR_JOB_TYPE


def _fmt(v: bytes | str | None) -> str:
    if v is None:
        return "-"
    if isinstance(v, bytes):
        try:
            return v.decode()
        except Exception:
            return repr(v)
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-type", default=None,
                    help="only show this job_type (e.g. caption_embed)")
    ap.add_argument("--serial", default=None,
                    help="only show entries for this serial_number")
    ap.add_argument("--limit", type=int, default=50,
                    help="max entries per stream (default 50)")
    args = ap.parse_args()

    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    engine = sa.create_engine(os.environ["DATABASE_URL"])

    job_types = [args.job_type] if args.job_type else list(DLQ_FOR_JOB_TYPE.keys())

    total = 0
    for jt in job_types:
        stream = DLQ_FOR_JOB_TYPE[jt]
        entries = r.xrevrange(stream, count=args.limit)
        if not entries:
            continue

        print(f"\n=== {stream} ({len(entries)} entries) ===")
        for entry_id, fields in entries:
            f = {_fmt(k): _fmt(v) for k, v in fields.items()}
            if args.serial and f.get("serial_number") != args.serial:
                continue

            job_id = f.get("job_id")
            db_state = None
            db_attempt = None
            if job_id:
                with engine.connect() as c:
                    row = c.execute(sa.text(
                        "SELECT state, attempt_count FROM panoptic_jobs WHERE job_id = :jid"
                    ), {"jid": job_id}).first()
                    if row:
                        db_state = row.state
                        db_attempt = row.attempt_count

            eid = _fmt(entry_id)
            reason = (f.get("reason") or "")[:140]
            print(f"  [{eid}]")
            print(f"    job_id:   {f.get('job_id')}")
            print(f"    serial:   {f.get('serial_number')}")
            print(f"    db state: {db_state or '<missing>'}  attempts={db_attempt}")
            print(f"    reason:   {reason}")
            total += 1

    if total == 0:
        print("DLQ is empty across all streams.")
    else:
        print(f"\ntotal DLQ entries shown: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
