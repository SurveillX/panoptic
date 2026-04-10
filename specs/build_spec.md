# SurveillX VIL Build Spec v1.0

## Purpose

This document translates the VIL Master Design Specification v1.0 into a concrete implementation plan.

It is intentionally biased toward:
- fast first implementation
- low rework
- explicit service boundaries
- testability
- compatibility with the current SurveillX stack

---

# 1. Delivery Strategy

Build in this order:

1. **Contracts first**
2. **Queue + orchestration**
3. **L1 bucket summaries**
4. **Embedding + retrieval**
5. **L2 hourly rollups**
6. **Recompute / stale handling**
7. **Operational hardening**

Do **not** start with:
- site-wide summaries
- advanced search UX
- cross-camera reasoning
- full HA infrastructure

---

# 2. New / Modified Components

## 2.1 New Services

### A. `vil-orchestrator`
Owns:
- intake of finalized bucket events
- job creation
- rollup readiness bookkeeping
- recompute scheduling
- embedding reconciliation scheduling

### B. `vil-summary-agent`
Owns:
- lease + execute summary jobs
- fetch frames / thumbnails
- apply frame quality filter
- build prompts
- call vLLM
- write summary records
- enqueue follow-on jobs

### C. `vil-embedding-worker`
Owns:
- embedding generation
- upsert into semantic store
- embedding retry + reconciliation

---

## 2.2 Modified Existing Components

### D. `cognia-aggregator`
Must emit canonical finalized bucket records with:
- stable identifiers
- completeness metadata
- keyframe candidate timestamps
- activity_score and its supporting metrics
- event markers

### E. Jetson Keyframe API
Must support:
- timestamp-tolerant frame fetch
- timestamp-tolerant thumbnail fetch
- quality metadata in response
- token-based authentication

### F. Metadata DB
Must store:
- bucket records
- job records
- summary records
- rollup readiness state
- embedding status
- stale / superseded relationships

### G. Semantic Store
Qdrant namespace or collection strategy scoped by tenant/site as needed.

---

# 3. Recommended Repo Layout

Use a new top-level workspace or service directory, for example:

```text
surveillx-vil/
  services/
    vil-orchestrator/
    vil-summary-agent/
    vil-embedding-worker/
  shared/
    schemas/
    clients/
    prompts/
    utils/
  infra/
    docker/
    migrations/
    compose/
  docs/
    surveillx_vil_master_design_v1_0.md
    surveillx_vil_build_spec_v1_0.md
```

If you prefer to keep this in an existing repo, preserve the same logical split.

---

# 4. Technology Choices (v1)

## Required
- Python 3.11+
- FastAPI
- Redis
- Redis Streams
- Postgres
- Qdrant
- Pydantic v2
- SQLAlchemy or sqlmodel
- httpx
- tenacity
- OpenAI-compatible client for vLLM

## Avoid for v1
- Kafka
- Celery
- heavyweight workflow engines
- multiple vector stores
- multiple queues

---

# 5. Normative Data Contracts

## 5.1 Bucket Record Schema

Implement as a strict Pydantic model in:

```text
shared/schemas/bucket.py
```

Fields:

```python
bucket_id: str
tenant_id: str
site_id: str
trailer_id: str
camera_id: str

bucket_start_utc: datetime
bucket_end_utc: datetime
bucket_status: Literal["complete", "partial", "late_finalized"]

schema_version: int
detection_hash: str

activity_score: float
activity_components: dict[str, float]
object_counts: dict[str, int]

keyframe_candidates: {
    "baseline_ts": datetime | None,
    "peak_ts": datetime | None,
    "change_ts": datetime | None,
}

event_markers: list[...]
completeness: {
    "detection_coverage": float,
    "stream_interrupted_seconds": int,
    "aggregator_restart_seen": bool,
}
```

### Rule
`bucket_id` must be generated from a structured payload, not freeform string concatenation.

---

## 5.2 Summary Record Schema

Implement in:

```text
shared/schemas/summary.py
```

Fields:

```python
summary_id: str
tenant_id: str
level: Literal["camera", "hour", "day", "site"]
scope_id: str

start_time: datetime
end_time: datetime

summary: str
key_events: list[...]
metrics: dict[str, Any]

coverage: {
    "expected": int,
    "present": int,
    "ratio": float,
    "missing": list[str],
}

summary_mode: Literal["full", "partial", "metadata_only"]
frames_used: int
confidence: float

embedding_status: Literal["pending", "success", "failed"]

version: int
is_latest: bool
superseded_by: str | None

model_profile: str
prompt_version: str
schema_version: int

source_refs: list[str]
created_at: datetime
updated_at: datetime
```

---

## 5.3 Job Record Schema

Implement in:

```text
shared/schemas/job.py
```

Fields:

```python
job_id: str
job_key: str
job_type: Literal["bucket_summary", "rollup_summary", "embedding_upsert", "recompute_summary"]
priority: Literal["high", "normal", "low"]

state: Literal["pending", "leased", "running", "succeeded", "degraded", "retry_wait", "failed_terminal", "cancelled"]

lease_owner: str | None
lease_expires_at: datetime | None
attempt_count: int
max_attempts: int

payload: dict[str, Any]
last_error: str | None

created_at: datetime
updated_at: datetime
```

---

# 6. Database Tables

Create migrations for these tables first.

## 6.1 `vil_buckets`
Stores canonical bucket records from Cognia.

## 6.2 `vil_jobs`
Stores current authoritative job state.

## 6.3 `vil_job_history`
Optional but strongly recommended.
Append-only record of transitions and failures.

## 6.4 `vil_summaries`
Stores summary records.

## 6.5 `vil_rollup_state`
Tracks readiness of parent windows.

Suggested fields:
- parent_key
- tenant_id
- level
- window_start
- window_end
- expected_children
- present_children
- coverage_ratio
- stale
- last_rollup_summary_id

## 6.6 `vil_embedding_backlog`
Optional helper table if you do not want to derive from `vil_summaries`.

---

# 7. Redis Streams Contract

## 7.1 Streams

```text
vil:jobs:bucket_summary
vil:jobs:rollup_summary
vil:jobs:embedding_upsert
vil:jobs:recompute
```

## 7.2 DLQ Streams

```text
vil:dlq:bucket_summary
vil:dlq:rollup_summary
vil:dlq:embedding_upsert
vil:dlq:recompute
```

## 7.3 Consumer Groups

Create one consumer group per worker type, for example:

```text
group: vil-summary-workers
group: vil-embedding-workers
group: vil-recompute-workers
```

## 7.4 Lease / Reclaim Protocol

Implement in shared utility code:

```text
shared/utils/leases.py
```

Rules:
- claim message from stream
- write authoritative lease state to Postgres `vil_jobs`
- lease TTL = 120 seconds
- renew every 30 seconds while running
- reclaimer runs every 30 seconds:
  - finds jobs with expired lease
  - resets state to `pending`
  - republishes if needed
  - preserves attempt count

Do **not** rely on Redis Streams pending list alone as your source of truth.

---

# 8. Activity Score Spec (v1)

Implement centrally in:

```text
shared/utils/activity.py
```

## 8.1 Inputs
- total object count over bucket
- number of unique classes observed
- temporal variance of count series

## 8.2 Formula

```python
raw = (
    0.5 * normalized_object_count
    + 0.2 * normalized_unique_classes
    + 0.3 * normalized_temporal_variance
)
activity_score = clamp(raw, 0.0, 1.0)
```

## 8.3 Normalization
Per camera:
- maintain rolling mean/std over recent buckets
- convert components to bounded z-score
- clamp to `[0, 1]`

## 8.4 Empty-scene rule
If all detections are zero and stream coverage is good:
- `activity_score = 0.0`
- bucket remains valid
- summarization may still occur, but likely metadata-only or simple “no notable activity” summary

This must be implemented in one place only.

---

# 9. Jetson Keyframe API Build Requirements

## 9.1 Endpoints

```http
GET /thumbnail?camera_id=...&target_ts=...&tolerance_sec=...
GET /frame?camera_id=...&target_ts=...&tolerance_sec=...
```

## 9.2 Response contract

```json
{
  "uri": "local or signed fetch path",
  "requested_ts": "2026-04-07T10:00:00Z",
  "actual_ts": "2026-04-07T09:59:57Z",
  "exact_match": false,
  "quality": {
    "blur": 0.18,
    "brightness": 0.62,
    "occluded": false
  }
}
```

## 9.3 Behavior rules
- return 404 if no frame within tolerance
- return actual nearest frame otherwise
- include quality values if available
- require bearer token auth
- apply per-client rate limiting
- log request latency and miss rate

## 9.4 Frame quality scoring
Implement lightweight scoring first:
- blur: Laplacian variance based heuristic
- brightness: grayscale mean / normalized brightness
- occluded: coarse heuristic or placeholder flag if unavailable

---

# 10. Summary Agent Implementation

## 10.1 Process flow for `bucket_summary`

1. Lease job
2. Load bucket record from Postgres
3. Determine summary policy
4. Resolve frame candidate timestamps
5. Fetch frames from Keyframe API
6. Apply quality filter
7. Decide summary mode:
   - full
   - partial
   - metadata_only
8. Build prompt
9. Call vLLM
10. Validate model output
11. Upsert summary record
12. Mark embedding_status = pending
13. Enqueue embedding job
14. Update rollup readiness state
15. Mark job succeeded/degraded

## 10.2 Prompt files

Store prompts in:

```text
shared/prompts/
  bucket_summary_v1.txt
  hourly_rollup_v1.txt
  daily_rollup_v1.txt
```

Prompts must be versioned by filename and explicit `prompt_version`.

## 10.3 Output validation

Model output must be parsed into a structured schema.
Use Pydantic validation after LLM call.

If validation fails:
- retry once with stricter repair prompt
- otherwise mark retryable or degraded based on policy

Do **not** store raw unvalidated prose as authoritative output.

---

# 11. Rollup Trigger Design

## 11.1 Use event-driven triggers only

Do not mix cron and event-driven logic for v1.

### Mechanism
On L1 or L2 completion:
- update `vil_rollup_state`
- compute readiness
- if readiness threshold reached and no active rollup job exists:
  - enqueue `rollup_summary`

## 11.2 Thresholds

Defaults:
- hourly (L2): 0.50
- daily (L3): 0.70
- site (L4): deferred or manual in v1 unless explicitly needed

## 11.3 Recompute governance

```text
max_recompute_depth = 2
min_recompute_interval_per_parent = 10 minutes
```

Meaning:
- L1 change can auto-trigger L2 and L3 recompute
- L4 automatic recompute is disabled in v1

## 11.4 Stale marking
If a child summary changes and parent already exists:
- mark parent stale
- enqueue recompute if debounce permits

---

# 12. Embedding Worker Implementation

## 12.1 Flow

1. Lease embedding job
2. Load summary record
3. Generate embedding
4. Upsert into Qdrant
5. Mark embedding_status = success

If failure:
- mark embedding_status = failed
- set job to retry_wait or failed_terminal

## 12.2 Reconciliation loop

Run periodic reconciliation every 10 minutes:
- query summaries where `embedding_status != success`
- enqueue missing embedding jobs
- skip if active job already exists

This is required.

---

# 13. Degraded Mode Contract

This must be treated as a first-class feature, not a soft fallback.

## 13.1 Modes

### `full`
- target frame count met
- acceptable quality

### `partial`
- at least 1 good frame, but less than target or some quality failure

### `metadata_only`
- zero usable frames
- summary built from bucket metadata only

## 13.2 Schema requirements
Every summary must include:
- `summary_mode`
- `frames_used`
- `confidence`
- `coverage`

## 13.3 UI/API behavior
UI must show degraded state.
Do not display degraded output as equivalent to full visual analysis.

---

# 14. Worker Crash Recovery

This is mandatory before production.

## 14.1 Cases to test
- crash before vLLM call
- crash after vLLM response, before DB write
- crash after DB write, before enqueue embedding
- crash after embedding generation, before Qdrant upsert

## 14.2 Recovery rule
Authoritative state lives in Postgres.
Workers may crash freely.
Reclaimer + reconciliation jobs must restore progress without manual intervention.

---

# 15. Security / Tenancy

## 15.1 Tenant model
Tenant = customer account boundary.

All major records must include:
- tenant_id
- site_id
- trailer_id
- camera_id where applicable

## 15.2 Isolation rules
- every query to Postgres filtered by tenant_id
- every Qdrant record tagged with tenant_id
- search queries must filter by tenant_id
- job payloads must include tenant_id
- API auth must map request → tenant scope

If v1 is effectively single tenant internally, state that explicitly in code and schemas anyway.

---

# 16. SLO / Metrics Implementation

## 16.1 Required metrics

### Queue / jobs
- queue depth by stream
- jobs by state
- reclaim count
- DLQ count
- retry count

### Media
- frame fetch success rate
- 404/miss rate
- frame latency
- frame quality rejection count

### LLM
- inference latency
- validation failure count
- timeout count

### Summaries
- summaries by mode
- coverage ratios
- stale parent count
- embedding backlog

## 16.2 End-to-end latency metric
You must measure:

```text
summary_completed_at - bucket_end_utc
```

Without this, your L1 SLO is meaningless.

---

# 17. Phase-by-Phase Build Plan

## Phase 0 — Contracts & persistence
Deliver:
- Pydantic schemas
- Postgres migrations
- Redis stream contract
- activity_score utility
- tenant fields everywhere

Exit criteria:
- bucket can be inserted
- job can be created
- job can be leased/reclaimed in tests

---

## Phase 1 — L1 bucket summaries
Deliver:
- Keyframe API contract implementation
- summary agent
- prompt v1
- summary record persistence
- degraded mode

Exit criteria:
- end-to-end from bucket → summary works locally
- retries and reclaim work
- metadata_only fallback works

---

## Phase 2 — Embeddings & search foundation
Deliver:
- embedding worker
- Qdrant upsert
- reconciliation loop

Exit criteria:
- summaries become searchable
- failed embedding eventually reconciles

---

## Phase 3 — Hourly rollups
Deliver:
- rollup state tracking
- event-driven trigger
- L2 summary generation
- stale marking + bounded recompute

Exit criteria:
- late L1 can update L2 correctly
- no recompute storm in load tests

---

## Phase 4 — Operational hardening
Deliver:
- dashboards
- DLQ inspection tools
- backpressure controls
- rate limits

Exit criteria:
- system survives worker crashes, Redis restart, frame misses, and delayed buckets

---

# 18. Required Test Matrix

## Unit tests
- activity_score calculation
- bucket_id generation
- summary_id generation
- readiness threshold logic
- degraded mode selection
- lease expiration/reclaim

## Integration tests
- bucket → summary
- late bucket → recompute
- frame 404 → metadata_only
- embedding failure → reconciliation recovery
- worker crash during each critical stage

## Load tests
- burst of 500 bucket events
- Jetson frame misses
- Redis restart during active leases
- vLLM latency spike

---

# 19. Suggested Immediate Tasks for Claude Code

1. Create shared schemas and migrations
2. Implement Redis Streams producer/consumer library with lease TTL + reclaim
3. Add canonical bucket emission contract to Cognia Aggregator
4. Build minimal Keyframe API auth + timestamp-tolerant lookup
5. Build `vil-summary-agent` with metadata-only path first
6. Add prompt/version plumbing
7. Build embedding worker + reconciliation
8. Add rollup readiness table and L2 trigger path

---

# 20. Non-Goals for v1

Do not build these now:
- cross-camera event stitching
- full site-level automatic recompute
- semantic frame clustering
- multi-model routing
- push-every-frame storage
- perfect HA stack

---

# Final Implementation Guidance

Keep the first working system narrow:
- bucket summaries first
- correctness before sophistication
- recovery before speed
- explicit contracts before optimization

A smaller system with hard guarantees beats a richer system with silent corruption.
