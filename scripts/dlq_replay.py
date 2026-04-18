"""
DLQ replay CLI.

Pushes a failed job back onto its normal processing stream. Workflow:

  1. Look up the job in Postgres by job_id.
  2. Reset its state to 'pending' and zero attempt_count (or keep — see --no-reset).
  3. XADD to the normal stream so a worker picks it up.
  4. (Optionally) delete the entry from the DLQ stream with --ack.

    # replay one job by id (keeps the DLQ entry)
    .venv/bin/python scripts/dlq_replay.py --job-id <uuid>

    # replay + remove from DLQ
    .venv/bin/python scripts/dlq_replay.py --job-id <uuid> --ack

    # replay all jobs in a specific DLQ stream
    .venv/bin/python scripts/dlq_replay.py --job-type caption_embed --all

    # dry-run (shows what would be replayed)
    .venv/bin/python scripts/dlq_replay.py --job-type caption_embed --all --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

import redis
import sqlalchemy as sa

from shared.utils.streams import DLQ_FOR_JOB_TYPE, enqueue_job


def _fmt(v) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode()
        except Exception:
            return repr(v)
    return str(v) if v is not None else ""


def _reset_job(engine, job_id: str, no_reset: bool) -> tuple[bool, str | None]:
    """Return (reset_ok, job_type). If no_reset, just return (True, job_type)."""
    with engine.connect() as c:
        row = c.execute(sa.text(
            "SELECT job_type, state, attempt_count FROM panoptic_jobs WHERE job_id = :jid"
        ), {"jid": job_id}).mappings().first()
        if not row:
            return False, None
        job_type = row["job_type"]

        if no_reset:
            return True, job_type

        c.execute(sa.text("""
            UPDATE panoptic_jobs
               SET state            = 'pending',
                   lease_owner      = NULL,
                   lease_expires_at = NULL,
                   attempt_count    = 0,
                   last_error       = NULL,
                   updated_at       = now()
             WHERE job_id = :jid
        """), {"jid": job_id})
        c.commit()
    return True, job_type


def _replay_one(r, engine, *, job_id: str, serial: str, job_type: str, no_reset: bool) -> bool:
    ok, actual_job_type = _reset_job(engine, job_id, no_reset)
    if not ok:
        print(f"  {job_id}: missing from panoptic_jobs — skipped")
        return False
    if job_type != actual_job_type:
        print(f"  {job_id}: db job_type={actual_job_type} differs from DLQ job_type={job_type}, using db")
        job_type = actual_job_type

    try:
        enqueue_job(r, job_type=job_type, job_id=job_id, serial_number=serial)
    except Exception as exc:
        print(f"  {job_id}: XADD failed — {exc}")
        return False
    print(f"  {job_id}: replayed onto panoptic:jobs:{job_type} (db reset={'no' if no_reset else 'yes'})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default=None)
    ap.add_argument("--job-type", default=None, help="replay-all scope (with --all)")
    ap.add_argument("--all", action="store_true", help="replay every entry for --job-type")
    ap.add_argument("--ack", action="store_true", help="delete DLQ entry after successful replay")
    ap.add_argument("--no-reset", action="store_true",
                    help="don't zero the job's attempt_count / state; only re-enqueue")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not (args.job_id or (args.job_type and args.all)):
        ap.error("specify --job-id, or --job-type + --all")

    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    engine = sa.create_engine(os.environ["DATABASE_URL"])

    # Collect candidates
    candidates: list[tuple[str, str, str, str]] = []  # (stream, entry_id, job_id, serial)
    if args.job_id:
        # Find which DLQ holds this job_id by scanning.
        with engine.connect() as c:
            row = c.execute(
                sa.text("SELECT job_type, serial_number FROM panoptic_jobs WHERE job_id = :jid"),
                {"jid": args.job_id},
            ).mappings().first()
        if not row:
            print(f"job_id {args.job_id} not in panoptic_jobs")
            return 1
        jt = row["job_type"]
        stream = DLQ_FOR_JOB_TYPE[jt]
        # Find the entry_id in that stream
        for entry_id, fields in r.xrevrange(stream, count=5000):
            f = {_fmt(k): _fmt(v) for k, v in fields.items()}
            if f.get("job_id") == args.job_id:
                candidates.append((stream, _fmt(entry_id), args.job_id, f.get("serial_number", row["serial_number"])))
                break
        if not candidates:
            # Job_id given but no DLQ entry — still allow a forced re-enqueue.
            print(f"note: no DLQ entry for {args.job_id} in {stream}; re-enqueueing by job_id alone")
            candidates.append(("", "", args.job_id, row["serial_number"]))
    else:
        stream = DLQ_FOR_JOB_TYPE[args.job_type]
        for entry_id, fields in r.xrevrange(stream, count=5000):
            f = {_fmt(k): _fmt(v) for k, v in fields.items()}
            candidates.append((stream, _fmt(entry_id), f.get("job_id"), f.get("serial_number", "")))

    if not candidates:
        print("nothing to replay.")
        return 0

    print(f"found {len(candidates)} candidate(s)")
    if args.dry_run:
        for stream, eid, jid, sn in candidates:
            print(f"  would replay job_id={jid} serial={sn} from {stream} entry={eid}")
        return 0

    replayed = 0
    for stream, entry_id, job_id, serial in candidates:
        # determine job_type from DLQ stream name
        jt = stream.split(":")[-1] if stream else (args.job_type or "")
        ok = _replay_one(
            r, engine,
            job_id=job_id,
            serial=serial,
            job_type=jt,
            no_reset=args.no_reset,
        )
        if ok and args.ack and stream and entry_id:
            try:
                r.xdel(stream, entry_id)
                print(f"    deleted DLQ entry {entry_id}")
            except Exception as exc:
                print(f"    DLQ xdel failed: {exc}")
        if ok:
            replayed += 1

    print(f"done: {replayed}/{len(candidates)} replayed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
