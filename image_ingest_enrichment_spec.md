## VIL Image Ingest + Enrichment Implementation Spec

This is the concrete spec for the **first VIL-side capability to build next**.

It is based on the decisions already made in this chat:

* trailers push **selected low-res images directly to VIL**
* selected images are a **central searchable visual corpus**
* VIL stores the actual image, not just a reference
* VIL creates an image metadata record
* VIL enriches the image asynchronously with:

  * caption
  * caption embedding
* caption embeddings go to Qdrant
* visual image embeddings are **not required in this first build**
* unique camera identity is the composite:

  * `(serial_number, camera_id)` 
* VIL already uses Postgres + Redis/job queue + Qdrant + worker patterns, and current summary/search direction is based on summary text embeddings in Qdrant.  

---

## 1. Scope of this build

Build only this:

1. receive one pushed trailer image
2. store JPEG locally on VIL
3. create one `vil_images` Postgres row
4. enqueue `image_caption`
5. when caption completes, enqueue `caption_embed`
6. store caption text in Postgres
7. store caption embedding in Qdrant

Not in this build:

* visual image embeddings
* verification workflow
* report generation
* Azure/object storage
* broader search API redesign
* long-horizon summary work
* extra agent/orchestration layers

---

## 2. What this build enables

After this build, VIL will support:

* centralized storage of selected pushed low-res images
* metadata filtering over images in Postgres
* semantic text search over image content via caption embeddings in Qdrant
* linking images to `(serial_number, camera_id, bucket_start, bucket_end)`
* later verification/report/search work built on a real image corpus

It does **not** yet enable:

* visual similarity search
* full verification workflow
* automated image-grounded report generation

---

## 3. Ingest API

### Endpoint

`POST /v1/trailer/image`

### Content type

`multipart/form-data`

### Parts

#### `metadata`

JSON string.

Required fields:

```json
{
  "event_id": "evt_123",
  "schema_version": "1.0",
  "sent_at_utc": "2026-04-13T20:15:08.123Z",

  "serial_number": "1422725077375",
  "camera_id": "695e037f-c8bb-4aa6-a914-bd58bfb70ea7-default",

  "bucket_start": "2026-04-13T20:00:00Z",
  "bucket_end": "2026-04-13T20:15:00Z",

  "trigger": "alert",
  "timestamp_ms": 1776018379793,
  "captured_at_utc": "2026-04-13T20:05:12.450Z",

  "selection_policy_version": "1",

  "context": {
    "max_anomaly_score": 3.2,
    "max_count": 5,
    "object_types": ["person", "truck"],
    "row_count": 3
  }
}
```

Allowed `trigger` values:

* `alert`
* `anomaly`
* `baseline`

Rules:

* `timestamp_ms` may be null only for `baseline`
* `captured_at_utc` should be present when known
* `context` may be empty but must exist

#### `image`

Raw JPEG bytes.

### Validation

Reject with `400` if:

* missing required metadata field
* invalid JSON
* invalid timestamp format
* unsupported trigger
* missing image
* content type not JPEG
* image size exceeds configured limit

Suggested limit:

* `2 MB` max for pushed low-res image

### Response

Success:

```json
{
  "status": "accepted",
  "image_id": "img_..."
}
```

Duplicate:

```json
{
  "status": "duplicate",
  "image_id": "img_..."
}
```

---

## 4. Idempotency / dedupe

Use both:

### A. `event_id` dedupe

Maintain idempotency on trailer retry.

### B. deterministic `image_id`

Generate from:

* `serial_number`
* `camera_id`
* `bucket_start`
* `bucket_end`
* `trigger`
* `timestamp_ms` or literal `"baseline"`

This should make repeated delivery of the same selected image collapse to the same record.

### Behavior on duplicate

If `event_id` or deterministic `image_id` already exists:

* return `200`
* do not write duplicate file
* do not create duplicate `vil_images` row
* do not enqueue duplicate jobs

---

## 5. File storage

### Storage strategy

Store actual JPEG on local VIL storage.

### Path convention

```text
/data/vil/images/{serial_number}/{camera_id}/{YYYY}/{MM}/{DD}/{image_id}.jpg
```

Example:

```text
/data/vil/images/1422725077375/695e037f-c8bb-4aa6-a914-bd58bfb70ea7-default/2026/04/13/img_abc123.jpg
```

### Behavior

* create directories if missing
* if file already exists during duplicate retry, treat as duplicate success
* local path is the authoritative storage pointer in Postgres

No Azure/object storage in this build.

---

## 6. Postgres table

Create `vil_images`.

### Table definition

```sql
CREATE TABLE vil_images (
    image_id TEXT PRIMARY KEY,

    serial_number TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,                -- "{serial_number}:{camera_id}"

    bucket_start_utc TIMESTAMPTZ NOT NULL,
    bucket_end_utc   TIMESTAMPTZ NOT NULL,

    captured_at_utc TIMESTAMPTZ,
    timestamp_ms BIGINT,

    trigger TEXT NOT NULL,                 -- alert | anomaly | baseline
    selection_policy_version TEXT NOT NULL DEFAULT '1',

    context_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    storage_path TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'image/jpeg',
    width INTEGER,
    height INTEGER,
    size_bytes BIGINT,

    caption_status TEXT NOT NULL DEFAULT 'pending',           -- pending|success|failed
    caption_model TEXT,
    caption_text TEXT,

    caption_embedding_status TEXT NOT NULL DEFAULT 'pending', -- pending|success|failed
    caption_embedding_model TEXT,
    caption_embedding_vector_id TEXT,

    source TEXT NOT NULL DEFAULT 'trailer_push',
    is_searchable BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
```

### Indexes

```sql
CREATE INDEX ix_vil_images_scope_time
    ON vil_images (scope_id, bucket_start_utc DESC);

CREATE INDEX ix_vil_images_sn_camera_time
    ON vil_images (serial_number, camera_id, bucket_start_utc DESC);

CREATE INDEX ix_vil_images_trigger_time
    ON vil_images (trigger, bucket_start_utc DESC);

CREATE INDEX ix_vil_images_created_at
    ON vil_images (created_at DESC);
```

### Why these fields

This captures exactly what we decided:

* actual image retained centrally
* metadata wrapper for search/use
* link to camera-window
* caption and caption embedding as async enrichments
* no image embedding yet

---

## 7. Immediate ingest behavior

When `POST /v1/trailer/image` succeeds:

1. validate request
2. dedupe by `event_id` and/or deterministic `image_id`
3. store JPEG to local disk
4. inspect image for width/height/size
5. insert one `vil_images` row with:

   * `caption_status = 'pending'`
   * `caption_embedding_status = 'pending'`
6. create one `image_caption` job
7. return success

Do **not** do captioning inline in request path.

---

## 8. Jobs for this build

### Required jobs

* `image_caption`
* `caption_embed`

### Not required in this build

* `image_embed`

---

## 9. `image_caption` job

### Purpose

Generate a short caption / visual description for the stored image.

### Job payload

```json
{
  "job_type": "image_caption",
  "image_id": "img_...",
  "serial_number": "1422725077375"
}
```

### Worker behavior

1. load `vil_images` row by `image_id`
2. read JPEG from `storage_path`
3. call caption-capable VLM
4. generate short caption
5. update `vil_images`
6. enqueue `caption_embed`

### Caption output

Keep it practical:

* one short factual visual description
* not a long essay
* no speculative interpretation beyond what is visually supportable

Example:

* `"Two people and a pickup truck near the trailer entrance."`

### Postgres updates on success

Set:

* `caption_status = 'success'`
* `caption_model = <model_name>`
* `caption_text = <generated caption>`
* `updated_at = now()`

### On failure

Set:

* `caption_status = 'failed'`
* `updated_at = now()`

Do not enqueue `caption_embed` if no caption text exists.

---

## 10. `caption_embed` job

### Purpose

Turn `caption_text` into a semantic search vector in Qdrant.

### Job payload

```json
{
  "job_type": "caption_embed",
  "image_id": "img_...",
  "serial_number": "1422725077375"
}
```

### Worker behavior

1. load `vil_images` row
2. require `caption_text`
3. call text embedding model on `caption_text`
4. upsert vector into Qdrant
5. update `vil_images`

### Qdrant collection

Use:

* `image_caption_vectors`

### Qdrant payload shape

```json
{
  "record_type": "image_caption",
  "record_id": "img_...",
  "image_id": "img_...",

  "serial_number": "1422725077375",
  "camera_id": "cam-01",
  "scope_id": "1422725077375:cam-01",

  "bucket_start": "2026-04-13T20:00:00Z",
  "bucket_end": "2026-04-13T20:15:00Z",
  "captured_at": "2026-04-13T20:05:12.450Z",

  "trigger": "alert",
  "caption_text": "Two people and a pickup truck near the trailer."
}
```

### Postgres updates on success

Set:

* `caption_embedding_status = 'success'`
* `caption_embedding_model = <embedding_model_name>`
* `caption_embedding_vector_id = <qdrant_point_id>`
* `updated_at = now()`

### On failure

Set:

* `caption_embedding_status = 'failed'`
* `updated_at = now()`

---

## 11. Job chaining

This is the required order:

```text
image arrives
→ store JPEG
→ insert vil_images row
→ enqueue image_caption
image_caption succeeds
→ enqueue caption_embed
caption_embed succeeds
→ image is now semantically searchable
```

Important:

* `caption_embed` is not “later someday”
* it is an immediate chained async step after caption generation
* it cannot happen before caption text exists

---

## 12. Search implications of this build

After this build:

### Postgres can do

* image filtering by:

  * `serial_number`
  * `camera_id`
  * `scope_id`
  * time range
  * `trigger`

### Qdrant can do

* semantic search over image captions via `image_caption_vectors`

### This build does not yet do

* image visual similarity search
* image embedding search
* verification workflow
* cross-record fused search API

But it enables the image side of that later work.

---

## 13. New/modified files

This should follow existing repo patterns: workers, clients, schemas, DB models, queue helpers. Current bucket/summary side already uses worker/job orchestration and summary embeddings.  

### New files likely needed

* `infra/migrations/versions/00X_add_vil_images.py`
* `shared/schemas/image.py`
* `services/vil_image_ingest/` if you want a dedicated FastAPI service module, or add endpoint to existing webhook service if already created
* `services/vil_image_caption_worker/executor.py`
* `services/vil_image_caption_worker/worker.py`
* `services/vil_caption_embed_worker/executor.py`
* `services/vil_caption_embed_worker/worker.py`

### Modified files likely needed

* `shared/db/models.py`
* `shared/schemas/job.py` if job types are enumerated there
* `shared/utils/streams.py`
* `shared/utils/leases.py` if job claim/result typing needs update
* `pyproject.toml` if new image libs/model client deps are needed
* VIL webhook/router module to add `POST /v1/trailer/image`
* any central config module for:

  * image storage root
  * max image size
  * caption model
  * embedding model / Qdrant collection

If you already created `services/trailer_webhook/` in the earlier VIL plan, put the new image endpoint there rather than inventing another ingest surface. 

---

## 14. Implementation decisions already settled here

These are **not open questions** for Claude to rediscover:

* selected images are pushed directly to VIL, not Azure
* actual image bytes are stored on VIL
* image record stores metadata + storage pointer
* image caption is generated asynchronously
* caption embedding is generated asynchronously after caption exists
* image embeddings are deferred to a later build
* searchable record types are:

  * summary
  * image
  * event
* Qdrant should hold:

  * `summary_vectors`
  * `image_caption_vectors`
  * later `image_vectors`
* image captions should be embedded, not just stored as plain text

---

## 15. Manual verification checklist

### A. Basic ingest

1. POST one valid image + metadata to `POST /v1/trailer/image`
2. verify `200 accepted`
3. verify JPEG exists at expected local path
4. verify one `vil_images` row exists
5. verify:

   * `caption_status = 'pending'`
   * `caption_embedding_status = 'pending'`

### B. Duplicate retry

1. POST same image/metadata again
2. verify `200 duplicate`
3. verify no second file
4. verify no second `vil_images` row
5. verify no duplicate jobs created

### C. Caption job

1. run `image_caption` worker
2. verify `caption_text` populated
3. verify `caption_status = 'success'`
4. verify `caption_embed` job created

### D. Caption embedding job

1. run `caption_embed` worker
2. verify vector exists in `image_caption_vectors`
3. verify Qdrant payload metadata is correct
4. verify:

   * `caption_embedding_status = 'success'`
   * `caption_embedding_vector_id` populated

### E. Failure handling

1. force caption model failure
2. verify `caption_status = 'failed'`
3. verify no `caption_embed` job created

### F. Metadata filtering sanity

1. query `vil_images` by:

   * `serial_number`
   * `camera_id`
   * `trigger`
   * time range
2. verify expected image is returned

### G. Semantic search sanity

1. search Qdrant with a phrase close to generated caption
2. verify expected `image_id` is returned

---

## 16. Final one-line flow

```text
trailer pushes selected image → VIL stores JPEG + vil_images row → image_caption job → caption_embed job → image becomes semantically searchable
```

That is the full implementation spec for this slice.
