# Panoptic Push — Payload Reference

**Audience:** the team implementing the receiving side at `panoptic.surveillx.ai`.

This document describes exactly what each trailer sends to Panoptic over HTTP. For the HMAC auth protocol (signing string, header format, server-side verification spec), see [TRAILER_AUTH_HANDOFF.md](TRAILER_AUTH_HANDOFF.md) — this document only references it, does not duplicate it.

---

## Endpoints

Every trailer POSTs to one of two endpoints on a single base URL (`PANOPTIC_WEBHOOK_URL`, e.g. `https://panoptic.surveillx.ai`):

| Method | Path | Content-Type | Carries |
|---|---|---|---|
| POST | `/v1/trailer/bucket-notification` | `application/json` | 15-minute aggregation bucket |
| POST | `/v1/trailer/image` | `multipart/form-data; boundary=...` | Image event (alert / anomaly / baseline / novelty) + JPEG |

Every request carries the three HMAC auth headers:

- `X-Panoptic-Serial` — trailer's Jetson serial number
- `X-Panoptic-Timestamp` — unix epoch seconds at signing time
- `X-Panoptic-Signature` — `HMAC-SHA256(shared_secret, signing_string).hexdigest()`

Where `signing_string = "{serial}|{timestamp}|{METHOD}|{path}|{sha256(body)}"`. The `body` hashed is the **raw request body exactly as sent**, including the multipart boundaries for image requests. See TRAILER_AUTH_HANDOFF.md for the complete spec.

---

## 1. Bucket payload — `POST /v1/trailer/bucket-notification`

**Content-Type:** `application/json`

**When sent:** once per (camera, 15-minute window, object_type) tuple, shortly after the 15-minute boundary plus a 120-second lateness settle. Roughly 2,000–3,000 pushes/day on a 10-camera trailer.

**Body:**

```json
{
  "event_id": "bucket:cam-abc-default-2026-04-22T10:45:00+00:00-15m-person",
  "schema_version": "1",
  "sent_at_utc": "2026-04-22T11:01:23.456789+00:00",
  "serial_number": "1422725077375",
  "camera_id": "cam-abc-default",
  "bucket": {
    "bucket_start": "2026-04-22T10:45:00+00:00",
    "bucket_end":   "2026-04-22T11:00:00+00:00",
    "camera_id":    "cam-abc-default",
    "object_type":  "person",

    "unique_tracker_ids": 5,
    "total_detections":   135,
    "frame_count":        2073,

    "min_count": 0,
    "max_count": 1,
    "mode_count": 0,
    "mean_count": 0.065,
    "std_dev_count": 0.240,
    "max_count_at": "2026-04-22T10:57:34.643000+00:00",

    "min_confidence": 0.502,
    "max_confidence": 0.601,
    "avg_confidence": 0.541,

    "first_detection_at": "2026-04-22T10:57:34.643000+00:00",
    "last_detection_at":  "2026-04-22T10:59:59.861000+00:00",

    "active_seconds": 135.0,
    "duty_cycle": 0.15,

    "anomaly_score": 0.42,
    "anomaly_flag": 0
  }
}
```

### Envelope fields (every request)

| Field | Type | Notes |
|---|---|---|
| `event_id` | string | Deterministic, idempotency key. See "Idempotency" below. |
| `schema_version` | string | Currently `"1"` (string, not number). Bumped if the bucket schema ever changes. |
| `sent_at_utc` | ISO8601 | When cognia-push signed the request. Not the same as `bucket_start`. |
| `serial_number` | string | Trailer's Jetson serial (also in `X-Panoptic-Serial` header). |
| `camera_id` | string | Canonical Continuum head_id, typically `{uuid}-default`. Same as `bucket.camera_id`. |

### Bucket fields (what you probably care about)

| Field | Type | Notes |
|---|---|---|
| `bucket_start` / `bucket_end` | ISO8601 | 15-minute window boundaries. |
| `object_type` | string | One of `person`, `car`, `truck`, `bicycle`, `motorcycle`, `bus`, etc. (COCO classes filtered at the edge). |
| `unique_tracker_ids` | int | Approximate count of distinct tracked objects in the window. |
| `total_detections` | int | Sum of per-frame detection counts. |
| `frame_count` | int | Number of frames that contributed to this bucket. Typically `900 × fps`. |
| `max_count` / `max_count_at` | int / ISO8601 | Peak simultaneous object count, and the wall-clock time of the peak frame. |
| `mean_count` / `std_dev_count` | float | Per-frame count distribution. |
| `duty_cycle` | float | `active_seconds / 900` — fraction of the 15-minute window with ≥1 detection. |
| `anomaly_score` | float or null | Z-score-like value from the edge's hour-of-week baseline. Null if baseline isn't established yet (first ~7 days of a camera's life). |
| `anomaly_flag` | int | `0` or `1`. `1` if the score crossed the edge's anomaly threshold. |

**Note on anomaly:** the edge computes anomaly against its own hour-of-week baseline. Panoptic is free to run its own scoring over the bucket stream and ignore `anomaly_score`/`anomaly_flag` — they're informational, not authoritative.

---

## 2. Image payload — `POST /v1/trailer/image`

**Content-Type:** `multipart/form-data; boundary=<generated>`

**Two parts, in this order:**

1. `metadata` — `application/json`, no filename. Metadata envelope describing the event.
2. `image` — `image/jpeg`, filename `frame.jpg`. Raw JPEG bytes.

### Example (formatted for readability; real wire form has multipart boundaries)

```
--<boundary>
Content-Disposition: form-data; name="metadata"
Content-Type: application/json

{
  "event_id": "anomaly:cam-abc-default:2026-04-22T10:45:00+00:00:person",
  "schema_version": "1",
  "sent_at_utc": "2026-04-22T11:01:23.456789+00:00",
  "serial_number": "1422725077375",
  "camera_id": "cam-abc-default",
  "bucket_start": "2026-04-22T10:45:00+00:00",
  "bucket_end":   "2026-04-22T11:00:00+00:00",
  "trigger": "anomaly",
  "timestamp_ms": 1776843600000,
  "context": {
    "anomaly_score": 3.2,
    "max_count": 5,
    "object_type": "person",
    "bucket_start": "2026-04-22T10:45:00+00:00",
    "bucket_end": "2026-04-22T11:00:00+00:00"
  }
}
--<boundary>
Content-Disposition: form-data; name="image"; filename="frame.jpg"
Content-Type: image/jpeg

<raw JPEG bytes>
--<boundary>--
```

### Top-level metadata fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `event_id` | string | yes | Deterministic idempotency key. Format depends on trigger — see "Idempotency" below. |
| `schema_version` | string | yes | `"1"`. |
| `sent_at_utc` | ISO8601 | yes | When signed. |
| `serial_number` | string | yes | Trailer serial. |
| `camera_id` | string | yes | Continuum head_id. |
| `bucket_start` | ISO8601 | yes | Aligned to the 15-minute bucket this image is contextually part of. For per-moment triggers (alert/baseline/novelty) the value is `timestamp_ms` rendered as ISO8601 — there is no real window. Panoptic validators require this field to be present. |
| `bucket_end` | ISO8601 | yes | Same notes as `bucket_start`. |
| `trigger` | string | yes | Exactly one of `alert`, `anomaly`, `baseline`, `novelty`. |
| `timestamp_ms` | int or null | yes | Epoch ms of the frame. Never null for any currently-emitting producer. `null` is reserved for future "give me whatever is fresh now" producers. |
| `context` | object | yes | Opaque pass-through from the producer. Panoptic should tolerate unknown fields. Per-trigger schemas below. |

### Trigger semantics

Each trigger means something different to the downstream consumer of the image:

| Trigger | Owner | Meaning |
|---|---|---|
| `alert` | cognia-analytics (trailer) | **Evidence of a rule-triggered alert.** Image is the frame at the moment a FOV / ROI / tripwire rule fired. Visual proof of the rule hit. |
| `anomaly` | cognia-aggregator (trailer) | **Associated visual context around a statistical anomaly.** The anomaly is a property of aggregated 15-minute bucket counts/distributions, not of this specific frame. The image is the best near-in-time visual accompaniment — not "the anomaly itself." |
| `baseline` | cognia-selector (trailer) | **Periodic reference.** A "this is what the camera looks like right now" image emitted on first-sample per camera or on 1-hour liveness, regardless of scene change. Gives Panoptic a recent reference even on quiet cameras. |
| `novelty` | cognia-selector (trailer) | **Visually meaningful change.** Current frame's MobileCLIP2 embedding is far from the camera's reference embedding; the scene has changed in a way worth surfacing. |

### Per-trigger `context` fields

All fields are advisory; Panoptic should tolerate missing or additional fields.

**`trigger: "alert"`** — from cognia-analytics rule hits:
```json
{
  "rule_id": "rule-42",
  "rule_name": "phase4 smoke test",
  "rule_type": "fov",                    // "fov" | "roi" | "tripwire"
  "severity": "warning",                 // "info" | "warning" | "critical"
  "object_type": "person",
  "count": 7,                            // value that crossed the threshold
  "description": "Too many people",      // from rule.alert_message_template
  "direction": null                      // set only for tripwire rules
}
```

**`trigger: "anomaly"`** — from cognia-aggregator:
```json
{
  "anomaly_score": 3.2,
  "max_count": 5,
  "object_type": "person",               // single object_type (one event per row)
  "bucket_start": "2026-04-22T10:45:00+00:00",
  "bucket_end":   "2026-04-22T11:00:00+00:00",
  "incident_id": 42                      // optional, present when bucket row triggered a new incident
}
```

**`trigger: "baseline"`** — from cognia-selector:
```json
{
  "sample_id": 1,
  "similarity": null,                    // null on first sample (no reference yet); float on timer-driven baselines
  "reason": "no_reference",              // "no_reference" | "interval_timer"
  "frame_source": "deepstream"
}
```

**`trigger: "novelty"`** — from cognia-selector:
```json
{
  "sample_id": 3,
  "similarity": 0.12,                    // cosine similarity to camera's reference embedding
  "reason": "novel_scene",
  "frame_source": "deepstream"
}
```

### Image-fallback annotation

If cognia-push couldn't retrieve the producer's intended JPEG (e.g. selector's Redis key expired) and had to fall back to Continuum for a near-time frame, the context gets an extra field:

```json
"context": {
  ...,
  "image_fallback": "continuum"
}
```

This is a signal that the image you received may not be the exact frame the producer referenced — it's a close-in-time substitute. Happens rarely; should be logged cloud-side but is not an error.

---

## Idempotency — `event_id` formats

All `event_id`s are deterministic and stable across retries. Panoptic **must** dedup on `event_id` — the trailer uses at-least-once delivery (XAUTOCLAIM-driven retries, bounded LRU dedup edge-side). A successful 2xx from Panoptic on a duplicate should be idempotent (treat as success, no double-side-effect).

| Trigger | `event_id` format |
|---|---|
| `kind=bucket` | `bucket:{camera_id}-{bucket_start_iso}-15m-{object_type}` |
| `trigger=alert` | `alert:{analytics_alert_id}` — analytics mints this alert_id from `{camera}::rule_type::rule_type::{timestamp_iso}` |
| `trigger=anomaly` | `anomaly:{camera_id}:{bucket_start_iso}:{object_type}` |
| `trigger=baseline` | `baseline:{camera_id}:{sample_id}` |
| `trigger=novelty` | `novelty:{camera_id}:{sample_id}` |

Real examples from live traffic:

```
bucket:00240999-bf78-49cc-ae0f-28ffd330d4b9-default-2026-04-22T10:45:00+00:00-15m-person
alert:test-camera-phase4::fov::occupancy::2026-04-22T16:34:00.000
anomaly:phase3-smoke-cam:2026-04-22T16:00:00+00:00:person
baseline:phase5-smoke-cam:1
novelty:phase5-smoke-cam:3
```

---

## Response semantics (what cognia-push does with your HTTP response)

| Status | Edge behavior |
|---|---|
| 2xx | Success. Event is ack'd, removed from the trailer-side queue. |
| 429 | Retry with the standard retry policy (see below). |
| 5xx | Retry. |
| 401 / 403 | **Terminal, dead-letter.** Trailer logs it as a critical auth failure. Most likely cause: HMAC secret mismatch, clock skew, or trailer not registered. Dead-lettered entries sit in Redis for manual inspection. |
| Other 4xx | Terminal, dead-letter. Schema violation or malformed payload. |
| Network / timeout | Retry. |

**Retry policy:**
- Max 10 attempts per event.
- Retries are driven by Redis `XAUTOCLAIM` after `pending_idle_ms = 30s` idle, so the minimum retry interval is ~30 seconds.
- Total retry wall-clock window ≈ 400 seconds. After that, the event is dead-lettered.
- `X-Panoptic-Timestamp` is regenerated on each retry (so don't reject replays based on old timestamps — the trailer's signature will be fresh).

**Response body:** the edge logs the first 500 chars of your 4xx/5xx response bodies. Returning a human-readable error helps triage.

---

## Rate limits (trailer-side — informational)

cognia-push self-throttles outbound volume with per-camera and per-trailer token buckets. This is defense-in-depth; it doesn't replace Panoptic-side rate limiting.

| Limit | Default |
|---|---|
| Max images per camera per hour | 4 |
| Max images per trailer (all cameras) per hour | 20 |

These apply to **all image triggers combined** — alert, anomaly, baseline, and novelty events share the same bucket. Bucket events are not rate-capped and always flow.

A 10-camera trailer with ~3 active object types per camera produces ~2,880 bucket pushes/day.

---

## Tunables Panoptic can request changed

These are environment variables on the trailer side. Changing them is an operations handshake: Panoptic tells us what behavior they want, the trailer operator updates the env and restarts the affected container (~10 seconds, no rebuild).

| Tunable | Default | Owner service | Effect |
|---|---|---|---|
| `SELECTOR_BASELINE_INTERVAL_SECONDS` | `3600` | cognia-selector | How often the selector emits a liveness `trigger=baseline` image per camera. Lower = more frequent baselines. |
| `SELECTOR_SIMILARITY_THRESHOLD` | `0.85` | cognia-selector | Cosine similarity cutoff for `trigger=novelty`. Raise to emit more novelty events (more sensitive to scene change); lower to emit fewer. |
| `PUSH_MAX_PER_CAMERA_PER_HOUR` | `4` | cognia-push | Hourly cap on **all** image events per camera. |
| `PUSH_MAX_PER_TRAILER_PER_HOUR` | `20` | cognia-push | Hourly cap across all cameras on the trailer. |

**Interaction that matters:** the rate cap doesn't distinguish between triggers. If baseline interval is lowered without raising the cap, baseline events will hit the cap and be dead-lettered rather than delivered, AND they will crowd out any novelty/anomaly/alert events firing in the same hour.

### Sizing the cap when changing baseline cadence

```
PUSH_MAX_PER_CAMERA_PER_HOUR
  >= (baselines per hour)
   + headroom for expected novelty events
   + headroom for anomaly events
   + headroom for alert events
```

For common targets:

| Desired baseline cadence | `SELECTOR_BASELINE_INTERVAL_SECONDS` | Min suggested `PUSH_MAX_PER_CAMERA_PER_HOUR` |
|---|---|---|
| Every hour (default) | `3600` | 4 |
| Every 30 min | `1800` | 6 |
| Every 15 min | `900` | 10 |
| Every 10 min | `600` | 14 |
| Every 5 min | `300` | 20 |

Bandwidth scales linearly: a baseline image is ~25–100 KB, so 12 baselines/hour/camera × 10 cameras × 50 KB ≈ 6 MB/hour. Not a concern for current trailer links.

### Recommendation for picking the right knob

"I want more images" usually means one of three things. Pick the matching knob rather than over-tuning:

- **"I want a recent reference even on quiet cameras, more often than hourly."** → lower `SELECTOR_BASELINE_INTERVAL_SECONDS`, raise the cap to match.
- **"I want to see scene changes faster when they happen."** → raise `SELECTOR_SIMILARITY_THRESHOLD` (more events when scene shifts). Baseline cadence unchanged.
- **"I want pure liveness — proof the camera is alive — every N minutes, not necessarily a JPEG."** → this isn't a tunable; a lightweight `kind=heartbeat` event type would be a small feature. Ask us if that's what's needed.

---

## Volume + bandwidth estimate (per trailer, 10 cameras, typical)

| Stream | Rate | Size each | Daily total |
|---|---|---|---|
| `bucket-notification` JSON | ~2,880/day | ~1 KB | ~3 MB |
| `image` multipart (low-res JPEG) | 50–150/day | ~25–100 KB | ~5–15 MB |
| **Total outbound** | | | **~8–18 MB/day** |

---

## Related docs

- [`TRAILER_AUTH_HANDOFF.md`](TRAILER_AUTH_HANDOFF.md) — full HMAC auth protocol (signing string, header format, server-side verification spec, edge cases).
- [`push-events.md`](push-events.md) — trailer-internal contract: the Redis stream producers write to and cognia-push consumes from. Panoptic doesn't interact with this layer; included for context if you're debugging a trailer-side issue.
