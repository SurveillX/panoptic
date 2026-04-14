"""
Hashing utilities shared across Panoptic components.

compute_child_set_hash: produces a stable hash from a set of child summary IDs
regardless of the order in which they were received.  Used by:
  - rollup job_key construction
  - summary_id generation for rollup summaries
"""

from __future__ import annotations

import hashlib
import json


def compute_child_set_hash(child_summary_ids: list[str]) -> str:
    """
    Return sha256 of the sorted list of child summary IDs serialised as JSON.

    Sorting ensures the hash is identical regardless of arrival order, which
    is critical for rollup stability — a late-arriving L1 that changes the
    child set will produce a different hash, correctly invalidating any
    previously computed rollup job_key.
    """
    sorted_ids = sorted(child_summary_ids)
    payload = json.dumps(sorted_ids, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
