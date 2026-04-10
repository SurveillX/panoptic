# SurveillX Visual Intelligence Layer (VIL)

## Master Design Specification v1.0

---

# 0. Purpose

Defines a production-ready system to convert detections + sparse imagery into hierarchical, searchable intelligence with **explicit execution, failure handling, and data contracts**.

---

# 1. Core Goal

> Explain what happened, when, and why it matters—reliably, under partial data and unreliable networks.

---

# 2. Edge Constraint

* Jetson NVR = **rolling (~2 weeks)**
* Video is **ephemeral**
* System must persist:

  * summaries (authoritative record)
  * embeddings
  * **selected frames for important events (optional, policy-driven)**

---

# 3. Architecture

```text
Jetson (Continuum + Cognia)
   ↓
Cloud Control Plane (API + Orchestrator)
   ↓
Summary Agent (stateless compute)
   ↓
Semantic Store (Qdrant + metadata DB)
```

---

# 4. Execution Layer (Finalized)

## 4.1 Job Types

* `bucket_summary` (L1)
* `rollup_summary` (L2–L4)
* `embedding_upsert`
* `recompute_summary`

---

## 4.2 Job States

`pending → leased → running → (succeeded | degraded | retry_wait | failed_terminal | cancelled)`

---

## 4.3 Queue

* **Redis Streams (v1)**
* Streams:

  * `vil:jobs:bucket_summary`
  * `vil:jobs:rollup_summary`
  * `vil:jobs:embedding_upsert`
  * `vil:jobs:recompute`
* Enable **AOF persistence** + snapshots

---

## 4.4 Leasing & Recovery (FIXED)

**Required protocol:**

```text
lease_ttl = 120s (configurable)
heartbeat_interval = 30s

Worker:
  - XREADGROUP to claim
  - set lease with TTL
  - renew every heartbeat

Recovery:
  - if lease expired → job is reclaimable
  - background reclaimer:
      XAUTOCLAIM pending entries older than lease_ttl
```

**Guarantee:** at-least-once execution + no silent loss

---

## 4.5 Idempotency

```text
job_key = {type}:{bucket_id}:{model}:{prompt_version}
```

* Only one active job per `job_key`
* Writes must be **upsert-by-summary_id**

---

# 5. Identity & Versioning (FIXED)

## 5.1 Bucket ID (corrected)

```text
bucket_id = sha256(
  camera_id +
  start_utc +
  end_utc +
  detection_hash +
  schema_version
)
```

`detection_hash` = hash of aggregated counts/events → prevents silent corruption on replay

---

## 5.2 Summary ID (corrected)

```text
summary_id = sha256(
  level +
  scope_id +
  window_start +
  window_end +
  child_set_hash +
  model_profile +
  prompt_version +
  summary_schema_version
)
```

---

## 5.3 Parent Versioning (NEW)

Each summary includes:

```json
{
  "version": 3,
  "superseded_by": "summary_id|null",
  "is_latest": true
}
```

Search MUST filter `is_latest=true`.

---

# 6. Activity Score (DEFINED)

```text
activity_score =
  normalize(
    w1 * object_count_total +
    w2 * unique_object_classes +
    w3 * temporal_variance(object_count)
  )
```

Defaults:

* `w1=0.5`, `w2=0.2`, `w3=0.3`
* normalized per camera using rolling mean/std (z-score clamp to [0,1])

Used for:

* frame selection
* prioritization
* summary hints

---

# 7. Frame Retrieval

## 7.1 Modes

* **A (default):** pull from Jetson
* **B (optional):** staged (object storage) for flagged events
* **C:** metadata fallback

---

## 7.2 API Contract

```http
GET /thumbnail?camera_id&target_ts&tolerance_sec
GET /frame?camera_id&target_ts&tolerance_sec
```

Response:

```json
{
  "uri": "...",
  "requested_ts": "...",
  "actual_ts": "...",
  "exact_match": false,
  "quality": {
    "blur": 0-1,
    "brightness": 0-1,
    "occluded": false
  }
}
```

---

## 7.3 Retry (FIXED)

Per frame:

* retry on **network errors only**
* no retry on 404/miss
* backoff: 0s, +2s

Per bucket:

* if ≥1 frame → proceed (degraded if < target)
* if 0 frames → follow `summary_policy`

---

## 7.4 Frame Quality Filter (NEW)

Reject frames if:

* blur > 0.7
* brightness < 0.2
* occluded = true

---

# 8. Degraded Mode (DEFINED)

Every summary MUST include:

```json
{
  "summary_mode": "full | partial | metadata_only",
  "frames_used": 0-8,
  "confidence": 0.0-1.0
}
```

Rules:

* `full` → ≥ target frames
* `partial` → some frames missing
* `metadata_only` → 0 frames

UI must display degraded state.

---

# 9. Hierarchy & Rollups

## 9.1 Levels

* L1: 15-min (camera)
* L2: hourly
* L3: daily
* L4: site

---

## 9.2 Trigger Model (FINAL)

**Event-driven**

```text
On child summary completion:
  update parent readiness state
  if coverage ≥ threshold → enqueue rollup
```

---

## 9.3 Coverage

```json
{
  "expected": 4,
  "present": 3,
  "ratio": 0.75,
  "missing": [...]
}
```

Defaults:

* L2 threshold = 0.5
* L3 threshold = 0.7

---

## 9.4 Recompute (BOUNDED)

```text
max_recompute_depth = 2
```

* L1 change → recompute L2 → L3
* DO NOT auto-recompute L4

Debounce:

* min 10 min between recomputes per parent

---

# 10. Embedding Consistency (FIXED)

## 10.1 Status Field

```json
{
  "embedding_status": "pending | success | failed"
}
```

---

## 10.2 Reconciliation Job (REQUIRED)

Periodic job:

```text
scan summaries where embedding_status != success
enqueue embedding_upsert
exponential backoff
```

Guarantee: no orphan summaries

---

# 11. Data Model (Core)

```json
{
  "summary_id": "...",
  "level": "camera|hour|day|site",
  "scope_id": "...",

  "start_time": "...",
  "end_time": "...",

  "summary": "...",
  "key_events": [],

  "metrics": {
    "activity_score": 0.0,
    "object_counts": {}
  },

  "coverage": {...},
  "summary_mode": "...",
  "confidence": 0.0,

  "embedding_status": "...",

  "version": 1,
  "is_latest": true,
  "superseded_by": null,

  "model_profile": "...",
  "prompt_version": "...",
  "schema_version": 1
}
```

---

# 12. Time Canonicalization (NEW)

* All times = UTC
* Cloud normalizes bucket windows
* Allow ±5s tolerance when grouping buckets into rollups
* Log drift per device

---

# 13. Backpressure

Limits:

* max jobs per camera
* max concurrent frame fetches per Jetson
* max global workers

On overload:

* defer rollups
* allow metadata-only summaries

---

# 14. Observability

Required:

* queue depth
* job success/degraded/failure rates
* lease reclaim count
* frame fetch success %
* embedding backlog
* recompute count

---

# 15. Security (v1)

* authenticated Keyframe API (token-based)
* service-to-service auth
* tenant_id on all jobs
* strict filtering in Semantic Store

---

# 16. v1 Scope

## Include

* Redis Streams + leasing
* L1 summaries
* L2 rollups
* embeddings + reconciliation
* degraded mode
* recompute (bounded)

## Defer

* cross-camera reasoning
* full HA infra
* advanced VLM
* full frame archiving

---

# 17. Final Statement

This system:

* tolerates partial data
* recovers from failure
* avoids silent corruption
* produces consistent, versioned intelligence

It is now **safe to implement**.
