"""
Redis Streams contract for Panoptic job queues.

Streams (job delivery):
  panoptic:jobs:bucket_summary
  panoptic:jobs:rollup_summary
  panoptic:jobs:embedding_upsert
  panoptic:jobs:recompute

DLQ streams (terminal failures):
  panoptic:dlq:bucket_summary
  panoptic:dlq:rollup_summary
  panoptic:dlq:embedding_upsert
  panoptic:dlq:recompute

Consumer groups:
  panoptic-summary-workers   → bucket_summary, rollup_summary, recompute
  panoptic-embedding-workers → embedding_upsert

Stream messages carry only routing fields; full job payload lives in Postgres.
Workers look up the job by job_id from Postgres after claiming.

Multi-worker safety:
  XREADGROUP delivers each message to exactly one consumer in the group.
  Leasing in Postgres (leases.py) is the authoritative duplicate-execution guard.
  Redis Streams is the delivery mechanism only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import redis

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stream / DLQ / group names
# ---------------------------------------------------------------------------

STREAM_FOR_JOB_TYPE: dict[str, str] = {
    "bucket_summary":    "panoptic:jobs:bucket_summary",
    "rollup_summary":    "panoptic:jobs:rollup_summary",
    "embedding_upsert":  "panoptic:jobs:embedding_upsert",
    "recompute_summary": "panoptic:jobs:recompute",
    "image_caption":     "panoptic:jobs:image_caption",
    "caption_embed":     "panoptic:jobs:caption_embed",
}

DLQ_FOR_JOB_TYPE: dict[str, str] = {
    "bucket_summary":    "panoptic:dlq:bucket_summary",
    "rollup_summary":    "panoptic:dlq:rollup_summary",
    "embedding_upsert":  "panoptic:dlq:embedding_upsert",
    "recompute_summary": "panoptic:dlq:recompute",
    "image_caption":     "panoptic:dlq:image_caption",
    "caption_embed":     "panoptic:dlq:caption_embed",
}

# Each stream has exactly one consumer group.
CONSUMER_GROUP_FOR_STREAM: dict[str, str] = {
    "panoptic:jobs:bucket_summary":   "panoptic-summary-workers",
    "panoptic:jobs:rollup_summary":   "panoptic-summary-workers",
    "panoptic:jobs:embedding_upsert": "panoptic-embedding-workers",
    "panoptic:jobs:recompute":        "panoptic-recompute-workers",
    "panoptic:jobs:image_caption":    "panoptic-image-caption-workers",
    "panoptic:jobs:caption_embed":    "panoptic-caption-embed-workers",
}

# Convenience: group name by job type.
GROUP_FOR_JOB_TYPE: dict[str, str] = {
    job_type: CONSUMER_GROUP_FOR_STREAM[stream]
    for job_type, stream in STREAM_FOR_JOB_TYPE.items()
}

# Cap stream length to avoid unbounded growth.
# Approximate trimming (~= is fine, exact is not needed).
_STREAM_MAXLEN = 50_000


# ---------------------------------------------------------------------------
# StreamMessage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamMessage:
    """A message received from a Redis Stream consumer group."""

    entry_id: str   # Redis stream entry ID, e.g. "1680000000000-0"
    stream: str     # Stream the message was read from
    group: str      # Consumer group that delivered it

    job_id: str
    job_type: str
    serial_number: str
    priority: str


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def bootstrap_streams(r: redis.Redis) -> None:
    """
    Ensure all job streams and consumer groups exist.

    Safe to call multiple times (idempotent).  Call once on service startup
    before spawning worker threads.

    Uses MKSTREAM so the stream is created if it does not exist.
    Uses '$' as the start ID so the group only receives new messages —
    historical messages are not redelivered on restart.  The reclaimer
    handles any jobs that were in-flight when the group was last created.
    """
    for stream, group in CONSUMER_GROUP_FOR_STREAM.items():
        try:
            r.xgroup_create(stream, group, id="$", mkstream=True)
            log.info("created consumer group %s on %s", group, stream)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                # Group already exists — normal on subsequent startups.
                pass
            else:
                raise


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------

def enqueue_job(
    r: redis.Redis,
    *,
    job_type: str,
    job_id: str,
    serial_number: str,
    priority: str = "normal",
) -> str:
    """
    Add a job message to the appropriate stream.

    Returns the Redis stream entry ID.

    The full job payload is stored in Postgres (panoptic_jobs); only routing
    fields are put into the stream message.

    Raises KeyError if job_type is unknown.
    """
    stream = STREAM_FOR_JOB_TYPE[job_type]
    entry_id: str = r.xadd(
        stream,
        {
            "job_id":   job_id,
            "job_type": job_type,
            "serial_number": serial_number,
            "priority": priority,
        },
        maxlen=_STREAM_MAXLEN,
        approximate=True,
    )
    log.debug("enqueued %s job_id=%s entry=%s", job_type, job_id, entry_id)
    return entry_id


def enqueue_dlq(
    r: redis.Redis,
    *,
    job_type: str,
    job_id: str,
    serial_number: str,
    reason: str,
) -> str:
    """
    Route a terminally failed job to its DLQ stream.

    DLQ messages are retained for inspection; they are NOT consumed by workers.
    """
    dlq = DLQ_FOR_JOB_TYPE[job_type]
    entry_id: str = r.xadd(
        dlq,
        {
            "job_id":   job_id,
            "job_type": job_type,
            "serial_number": serial_number,
            "reason":   reason[:1000],  # cap to avoid bloating Redis
        },
        maxlen=10_000,
        approximate=True,
    )
    log.warning("DLQ job_type=%s job_id=%s reason=%s", job_type, job_id, reason[:200])
    return entry_id


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

def consume_next(
    r: redis.Redis,
    *,
    stream: str,
    group: str,
    consumer_id: str,
    block_ms: int = 5_000,
) -> StreamMessage | None:
    """
    Blocking read of the next undelivered message from a consumer group.

    Returns None on timeout (no messages available).

    XREADGROUP with '>' delivers only new messages not yet delivered to any
    consumer in the group.  The returned message is added to the PEL (pending
    entries list) until ack_message() is called.

    ACK contract (strict):
      - ACK only after the job is fully processed AND committed to Postgres.
      - If Postgres claim fails (another worker won the race), do NOT ACK.
        Leave the message in the PEL; the reclaimer will XAUTOCLAIM and
        clean it up after it goes idle for LEASE_TTL seconds.
      - If the worker crashes before ACK, the message stays in the PEL,
        preserving the recovery path via XAUTOCLAIM.
    """
    result = r.xreadgroup(
        groupname=group,
        consumername=consumer_id,
        streams={stream: ">"},
        count=1,
        block=block_ms,
        noack=False,
    )

    if not result:
        return None

    stream_name, entries = result[0]
    if not entries:
        return None

    entry_id, fields = entries[0]
    return StreamMessage(
        entry_id=entry_id,
        stream=stream_name,
        group=group,
        job_id=fields["job_id"],
        job_type=fields["job_type"],
        serial_number=fields.get("serial_number", fields.get("tenant_id", "")),
        priority=fields.get("priority", "normal"),
    )


def ack_message(r: redis.Redis, *, stream: str, group: str, entry_id: str) -> None:
    """
    Acknowledge a stream message, removing it from the consumer group's PEL.

    Call this ONLY after the job terminal state has been committed to Postgres.
    Never call this on claim failure or before the Postgres commit.

    If the reclaimer has already ACK'd this entry (race between worker finish
    and XAUTOCLAIM cleanup), xack returns 0 — this is safe and ignored.
    """
    r.xack(stream, group, entry_id)
    log.debug("acked stream=%s group=%s entry=%s", stream, group, entry_id)


# ---------------------------------------------------------------------------
# PEL cleanup (called by reclaimer)
# ---------------------------------------------------------------------------

def autoclaim_and_ack_stale(
    r: redis.Redis,
    *,
    stream: str,
    group: str,
    reclaimer_id: str,
    min_idle_ms: int,
    batch_size: int = 200,
) -> int:
    """
    Transfer stale PEL entries (idle > min_idle_ms) to the reclaimer consumer,
    then immediately ACK them.

    This is hygiene only: by the time this runs, the reclaimer has already
    reset the Postgres job state and re-enqueued a fresh stream message.
    ACKing the stale PEL entries prevents them from accumulating.

    Returns the number of stale entries cleaned up.
    """
    total_acked = 0
    start_id = "0-0"

    while True:
        next_id, entries, _deleted = r.xautoclaim(
            name=stream,
            groupname=group,
            consumername=reclaimer_id,
            min_idle_time=min_idle_ms,
            start_id=start_id,
            count=batch_size,
        )

        if entries:
            entry_ids = [eid for eid, _ in entries]
            r.xack(stream, group, *entry_ids)
            total_acked += len(entry_ids)
            log.debug(
                "autoclaim_ack stream=%s acked=%d", stream, len(entry_ids)
            )

        # next_id == "0-0" means we've scanned the full PEL.
        if next_id == "0-0":
            break
        start_id = next_id

    return total_acked
