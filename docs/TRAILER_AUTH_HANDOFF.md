# Trailer-side handoff — Panoptic push auth + related changes

**Audience:** whoever (likely a Claude) is implementing push-side
changes in the trailer codebase.

**Status:** server-side spec is locked. Server implementation is in
flight and does not block this work — both sides can proceed in
parallel and integrate at the end.

This document is **self-contained**. You should not need to read
anything else on the Panoptic side to implement the trailer changes.

---

## 1. What's changing

Today the trailer POSTs unauthenticated HTTP to Panoptic:

- `POST https://panoptic.surveillx.ai/v1/trailer/bucket-notification`
- `POST https://panoptic.surveillx.ai/v1/trailer/image`

Starting with this change, every push must be **HMAC-signed** with a
shared fleet secret and carry three new headers. The payload bodies
(JSON for bucket, multipart for image) are unchanged.

Auth is enforced by an ASGI middleware on the Panoptic webhook. No
round-trip, no token exchange, no per-trailer secrets.

---

## 2. Required changes — summary

| # | Change | Priority |
|---|---|---|
| 1 | Add three signing headers to every push | **must** |
| 2 | Read `PANOPTIC_SHARED_SECRET` from trailer config/env | **must** |
| 3 | Ensure the trailer serial number is correctly claimed in headers (matches payload) | **must** |
| 4 | Handle new HTTP error codes (401 / 403 / 503) distinctly from existing retry logic | **must** |
| 5 | Ensure the trailer has accurate wall-clock time (NTP / chrony) — signing includes a ±5 min timestamp window | **must** |
| 6 | No payload body schema changes needed | — |

Nothing else on the trailer needs to change for this rollout.

---

## 3. The signing protocol

### 3.1 Canonical signing string

Build this string, byte-for-byte:

```
<serial>|<timestamp>|<method>|<path>|<body_sha256>
```

- `serial` — trailer serial number (same value as `serial_number` in payload)
- `timestamp` — current Unix epoch seconds, as a decimal string (e.g. `"1776201234"`)
- `method` — uppercase HTTP method (`POST` for both endpoints)
- `path` — request path only, no scheme/host/query. `/v1/trailer/image` or `/v1/trailer/bucket-notification`
- `body_sha256` — lowercase hex SHA-256 of the **raw HTTP request body bytes**, exactly as sent on the wire

Separator is the literal ASCII `|` (pipe). No spaces. No trailing newline.

### 3.2 Body hash — multipart caveat

For `/v1/trailer/image`, the body is `multipart/form-data`. Hash the
**entire multipart-encoded payload** (boundary + part headers + file
bytes), not any individual part.

The easiest way to get this right in Python:

```python
# build the multipart body first (buffered), then hash, then POST
from requests_toolbelt import MultipartEncoder
import hashlib

enc = MultipartEncoder(fields={
    "metadata": (None, json.dumps(metadata), "application/json"),
    "image": (filename, jpeg_bytes, "image/jpeg"),
})
body_bytes = enc.to_string()             # materialize once
content_type = enc.content_type           # must be this exact string when POSTing
body_sha256 = hashlib.sha256(body_bytes).hexdigest()

# then use `body_bytes` as the POST body, and `content_type` as Content-Type
```

The standard `requests` library does multipart in-memory by default
and gives you no hook to hash the wire bytes, which is why we
recommend `requests-toolbelt`. You can also hand-build the multipart
body if you prefer zero extra deps — just do not use a streaming
encoder.

For `/v1/trailer/bucket-notification` the body is plain JSON bytes,
so `body_sha256 = hashlib.sha256(json_bytes).hexdigest()`.

### 3.3 Signature

```
signature = hmac_sha256(PANOPTIC_SHARED_SECRET, signing_string).hex()
```

Lowercase hex, 64 chars.

### 3.4 Headers

Add these three headers to every request:

```
X-Panoptic-Serial:    <serial>
X-Panoptic-Timestamp: <timestamp>
X-Panoptic-Signature: <signature>
```

### 3.5 Reference implementation (drop-in)

```python
import hashlib
import hmac
import json
import time


def sign_panoptic_headers(
    secret: str,
    serial: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Return the three X-Panoptic-* headers for a Panoptic push."""
    ts = str(int(timestamp if timestamp is not None else time.time()))
    body_sha256 = hashlib.sha256(body).hexdigest()
    signing_string = f"{serial}|{ts}|{method.upper()}|{path}|{body_sha256}"
    sig = hmac.new(
        secret.encode(),
        signing_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Panoptic-Serial":    serial,
        "X-Panoptic-Timestamp": ts,
        "X-Panoptic-Signature": sig,
    }
```

Use identically for both endpoints. Just pass the correct `path` and
`body` for each.

---

## 4. End-to-end example — image push

### URLs

One base URL + two paths:

```
PANOPTIC_WEBHOOK_URL = "https://panoptic.surveillx.ai"
BUCKET_PATH          = "/v1/trailer/bucket-notification"
IMAGE_PATH           = "/v1/trailer/image"
```

Send requests to `PANOPTIC_WEBHOOK_URL + path`. Use **only the path**
(`/v1/trailer/image`) in the canonical signing string — never the
full URL.

### Code

```python
import httpx
import json
from requests_toolbelt import MultipartEncoder
# ... sign_panoptic_headers from section 3.5

SECRET = os.environ["PANOPTIC_SHARED_SECRET"]
BASE   = os.environ["PANOPTIC_WEBHOOK_URL"]    # e.g. "https://panoptic.surveillx.ai"
SERIAL = TRAILER_SERIAL                         # e.g. "YARD-A-001"

PATH = "/v1/trailer/image"
URL  = BASE + PATH

# Send your existing image metadata unchanged. Do NOT add fields you
# don't already populate — optional fields are optional (see §4.1).
meta = your_existing_image_metadata_dict()

enc = MultipartEncoder(fields={
    "metadata": (None, json.dumps(meta), "application/json"),
    "image":    ("frame.jpg", jpeg_bytes, "image/jpeg"),
})
body_bytes   = enc.to_string()
content_type = enc.content_type

headers = sign_panoptic_headers(SECRET, SERIAL, "POST", PATH, body_bytes)
headers["Content-Type"] = content_type

resp = httpx.post(URL, content=body_bytes, headers=headers, timeout=30)
```

### 4.1 Payload field reference — which are required, which are optional

**Image metadata** (the `metadata` part of the multipart payload):

| Field | Required? | Notes |
|---|---|---|
| `event_id` | yes | unique per push, enables server-side idempotency |
| `schema_version` | yes | `"1"` for current schema |
| `sent_at_utc` | yes | ISO-8601 UTC timestamp of push build time |
| `serial_number` | yes | must match `X-Panoptic-Serial` header |
| `camera_id` | yes | |
| `bucket_start` / `bucket_end` | yes | ISO-8601, bounds of the 15-min bucket |
| `trigger` | yes | one of `alert` / `anomaly` / `baseline` |
| `timestamp_ms` | yes (when `trigger != baseline`) | epoch ms when the frame was captured |
| `context` | yes | can be `{}` if no additional context |
| `captured_at_utc` | **optional** | skip if you already send `timestamp_ms` — same info |
| `selection_policy_version` | **optional** | server defaults to `"1"` |

Do not add optional fields unless you already have clean values for
them. The server accepts the minimal shape your pusher sends today.

**Bucket notification payload** is unchanged from your current shape.
Every field in the existing payload is preserved.

---

## 5. End-to-end example — bucket notification

```python
PATH = "/v1/trailer/bucket-notification"
URL  = BASE + PATH

# your existing payload shape, unchanged
payload = your_existing_bucket_payload_dict()

body_bytes = json.dumps(payload).encode()  # must be the exact bytes you POST
headers    = sign_panoptic_headers(SECRET, SERIAL, "POST", PATH, body_bytes)
headers["Content-Type"] = "application/json"

resp = httpx.post(URL, content=body_bytes, headers=headers, timeout=10)
```

**Do not build the body with `httpx.post(json=payload)` and then hash
`json.dumps(payload)` separately** — they may differ in whitespace or
key ordering. Build the body once, hash it, POST the same bytes.

---

## 6. Response handling

| Response | Meaning | Trailer behavior |
|---|---|---|
| `200` / `202` | success | normal — mark event done |
| `401 invalid_auth` | bad/missing/expired signature, stale timestamp, replayed, or serial mismatch | **do not retry**. Log loudly and surface to operator (this is a config / clock / secret issue, not a transient error) |
| `403 invalid_trailer` | serial not in Panoptic registry | **do not retry**. Operator needs to register this trailer on the Panoptic side (see §8) |
| `503` | Panoptic replay-cache backend (Redis) is unreachable | retry with backoff — transient |
| `5xx` (other) | server failure | retry with existing backoff policy |
| network error | unchanged | retry with existing backoff policy |

Keep the existing at-least-once push semantics. Panoptic has
idempotency guards on `event_id` / `image_id`, so harmless re-pushes
will be dedup'd server-side.

---

## 7. Configuration

The trailer needs one new config value:

```
PANOPTIC_SHARED_SECRET=<value>
```

Provided out-of-band by the operator. Same value for every trailer in
the fleet — **do not generate per-trailer secrets.**

### Rotation

During a fleet rotation the trailer may be given a new secret. The
server accepts both old and new signatures for an overlap window
(default 24 h). Cutover on the trailer side is: update the env /
config, restart the pusher. No special "both-secrets" logic needed
on the trailer — it always signs with exactly one current secret.

---

## 8. Trailer registration (operator step — outside trailer code)

For a new trailer's pushes to be accepted, the Panoptic operator must
register the trailer's serial. This is a one-time operator action on
the Panoptic side:

```
# on the Panoptic Spark, for each new trailer:
python scripts/add_trailer.py --serial YARD-A-001 --name "Yard A trailer #1"
```

Before that runs, the trailer's signed pushes will return 403
`invalid_trailer`. After registration, pushes succeed.

Just flagging so the trailer-side Claude doesn't spend time
debugging a legitimate 403 as if it were a signing bug.

---

## 9. Clock requirements

The server rejects requests where `|server_now - timestamp| > 300 s`
(5 minutes).

Trailers must have an accurate wall-clock. If not already enabled:

- systemd hosts: `systemctl enable --now systemd-timesyncd`
- or install `chrony`: `apt install chrony && systemctl enable --now chronyd`

Verify with `timedatectl`; the `System clock synchronized: yes` line
and a small `Time zone` offset should be present.

A silently-wrong clock → every push fails with 401 `invalid_auth`
`stale_timestamp`. Guard against this with a startup check if you
want, or just rely on ops discipline.

---

## 10. Testing

Once the trailer-side signing is implemented, you can smoke-test
**against Panoptic's dev stack** (co-located on the Spark, not the
production DO droplet) by hitting the webhook port directly. The
operator will provide:

1. `PANOPTIC_SHARED_SECRET` — the dev secret
2. `WEBHOOK_URL` — the dev webhook URL (e.g. `http://panoptic-spark.internal:8100`)
3. Confirmation that your test trailer serial is registered

A green smoke test is:

- POST a bucket notification → `200` with `{"status":"accepted"}`
- POST an image → `200` with `{"status":"accepted", "image_id":"..."}`
- Flip one character in the signature → `401 invalid_auth`
- Use a registered serial in headers but a different serial in payload → `403` on payload validation *(this is a different, unrelated rejection; both should reject)*

---

## 11. Does any part of the **existing payload** change?

**No.** Bucket notification JSON and image multipart metadata stay
exactly as they are today. The contract is purely additive at the
header layer.

---

## 12. What does Panoptic NOT care about

Deliberately not part of the protocol:

- The order of fields in your JSON (sorted-keys not required)
- Whether the multipart fields are in any particular order
- Whether you include a `User-Agent` (ignored)
- TLS client certs (not used)
- mTLS, JWT, OAuth, cookies — none of these are involved

Keep it simple.

---

## 13. Open items on the trailer side

Things for the trailer Claude to think about that aren't specified
by the protocol:

- Where to put the shared secret (env var vs encrypted config file vs
  systemd credential). Decision belongs to the trailer team.
- Whether to pre-compute the signing string in the same goroutine /
  coroutine that sends the request, or precompute async. Irrelevant
  to correctness; performance tradeoff only.
- Whether to add a self-test on startup that signs a dummy payload
  and verifies it matches a known-good value (recommended — catches
  clock-skew and secret-wiring bugs immediately).

---

## 14. Quick-reference test vectors

So both sides can sanity-check the same canonical string produces the
same signature:

### Vector 1 — bucket notification

```
secret       = "test-fleet-secret"
serial       = "YARD-A-001"
timestamp    = "1776201234"
method       = "POST"
path         = "/v1/trailer/bucket-notification"
body         = b'{"event_id":"fixed"}'
body_sha256  = "e3178a6b6a8e0e44d2c97ff13f2c8cfadee5d9f6fc9e8e32c8d4b5c4f9a6b2e0"
signing_str  = "YARD-A-001|1776201234|POST|/v1/trailer/bucket-notification|e3178a6b6a8e0e44d2c97ff13f2c8cfadee5d9f6fc9e8e32c8d4b5c4f9a6b2e0"
signature    = (compute locally, compare)
```

(Actual signature will be provided alongside the dev secret when the
trailer team is ready to integrate — plug in the given secret and
verify the signature matches. Exact byte agreement on `body_sha256`
is the usual place integrations disagree.)

---

## 15. Handoff summary

1. Add `PANOPTIC_SHARED_SECRET` to trailer config.
2. Import / implement the `sign_panoptic_headers` helper (§3.5).
3. Add the three `X-Panoptic-*` headers to every push.
4. Handle 401 / 403 as "do not retry, log", keep retrying on 5xx / network.
5. Ensure NTP is enabled on the trailer.
6. Ask the Panoptic operator to register the trailer's serial via `add_trailer.py`.
7. Run the smoke test from §10.

Nothing else is required. No payload schema changes, no new endpoints,
no SDK dependency.
