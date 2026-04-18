# Trailer webhook auth — design (v2)

**Status:** locked, ready to implement.
**Scope:** M2 prerequisite per `NEXT_STEPS.md` v2.
**Supersedes:** earlier per-trailer-tokens draft (rejected).

---

## 1. Purpose

Add lightweight authentication to trailer → Panoptic push requests
without introducing per-trailer secret management.

Design goals:

* keep the current trailer → Panoptic payload contract unchanged
* avoid unique credentials per trailer
* bind each request to a claimed trailer serial number
* reject random/junk traffic and casual spoofing
* tamper-evident for body, method, and path
* simple enough to implement on both trailer and Panoptic quickly

---

## 2. Summary

**Shared fleet secret + signed request headers.**

Each trailer sends three headers:

* `X-Panoptic-Serial` — trailer serial number
* `X-Panoptic-Timestamp` — unix epoch seconds
* `X-Panoptic-Signature` — HMAC-SHA256 hex over a canonical signing string

Panoptic verifies, in order:

1. headers present
2. serial in known-trailer registry
3. timestamp within allowed skew
4. not a replay
5. signature matches (active or previous secret)

---

## 3. Non-goals

This design does **not** attempt to provide:

* per-trailer unique secrets
* mTLS / PKI
* JWT infrastructure
* payload encryption
* perfect replay prevention
* strong protection if one trailer fully leaks the fleet secret

Intentionally minimal and pragmatic.

---

## 4. Auth model

### 4.1 Shared secret with dual-active rotation

Two environment variables on the Panoptic side:

* `PANOPTIC_SHARED_SECRET_ACTIVE` — current secret, always set
* `PANOPTIC_SHARED_SECRET_PREVIOUS` — optional, set only during rotation

Verification accepts a signature that validates against **either** secret.

Rotation flow:

1. Mint new secret; set it as `PANOPTIC_SHARED_SECRET_ACTIVE` and move the
   old secret to `PANOPTIC_SHARED_SECRET_PREVIOUS`. Reload webhook.
2. Push the new secret to every trailer as part of routine config update.
3. Once all trailers confirm they're signing with the new secret, unset
   `PANOPTIC_SHARED_SECRET_PREVIOUS` and reload. Old secret is now dead.

During the overlap window both old and new signatures validate. Trailers
can roll forward at their own pace.

### 4.2 Request identity

Each request claims a `serial_number` via the `X-Panoptic-Serial` header.
Panoptic validates that serial against the `panoptic_trailers` registry
(§10). A leaked fleet secret doesn't let an attacker inject data for
non-registered serials.

---

## 5. Request headers

All trailer pushes must include:

| Header | Meaning | Example |
|---|---|---|
| `X-Panoptic-Serial` | Trailer serial number | `1422725077375` |
| `X-Panoptic-Timestamp` | Unix epoch seconds at request build time | `1776201234` |
| `X-Panoptic-Signature` | Hex-encoded HMAC-SHA256 over the signing string | `8c2d4f…` |

No payload schema changes. Existing multipart/JSON body formats are
preserved as-is.

---

## 6. Signing algorithm

### 6.1 Canonical signing string

```
<serial>|<timestamp>|<method>|<path>|<body_sha256>
```

Where:

| Field | Source | Notes |
|---|---|---|
| `serial` | `X-Panoptic-Serial` | as sent |
| `timestamp` | `X-Panoptic-Timestamp` | as sent |
| `method` | HTTP method | **uppercased** (`POST`, `GET`) |
| `path` | request path only | no query string, no scheme/host, e.g. `/v1/trailer/image` |
| `body_sha256` | SHA256 of raw request body bytes | lowercase hex |

### 6.2 Signature

```
signature = hex(HMAC_SHA256(secret, signing_string))
```

### 6.3 Path caveat

Sign the path exactly as the client sends it. Panoptic must verify
against the path as received on the wire, without normalization (no
lowercasing, no slash collapsing, no URL-decoding). The Caddy → FRP
→ Spark chain is a pure L7 proxy and does not rewrite paths.

### 6.4 Body hash caveat — multipart requests

For `/v1/trailer/image` (multipart/form-data), `body_sha256` is the hash
of the **entire raw multipart payload** (boundary + part headers + file
bytes), not of any one part. Both client and server must agree on the
raw bytes.

Panoptic-side implication: the existing FastAPI handlers parse
multipart form fields before the handler runs, consuming the body.
Auth verification must run **before** that parse and must buffer the
raw bytes for signature verification. See §13 for the ASGI-middleware
approach.

---

## 7. Panoptic verification rules

On every authenticated push request:

1. **Require headers.** Reject 401 if any of `X-Panoptic-Serial`,
   `X-Panoptic-Timestamp`, `X-Panoptic-Signature` are missing.
2. **Validate formats.** Serial is non-empty string. Timestamp parses
   to int. Signature is hex of length 64.
3. **Validate time window.** `|now - timestamp| > PANOPTIC_AUTH_MAX_SKEW_SEC`
   → reject 401. Default window: **±300 s (5 min)**.
4. **Check replay cache.** Key = `panoptic:replay:{serial}:{timestamp}:{sig_first16}`.
   `SETNX` with TTL = `PANOPTIC_AUTH_REPLAY_TTL_SEC` (default **600 s**).
   If key already existed → reject 401.
5. **Validate serial is registered.** Look up `serial` in
   `panoptic_trailers` where `is_active = true`. Miss → reject **403**.
6. **Recompute body SHA256.** From the raw body bytes buffered by the
   middleware.
7. **Recompute expected signature.** Use `PANOPTIC_SHARED_SECRET_ACTIVE`.
   Compare constant-time.
8. **Fallback to previous secret.** If step 7 fails and
   `PANOPTIC_SHARED_SECRET_PREVIOUS` is set, recompute with it and
   constant-time compare. Fail both → reject 401.

Order matters: step 3 rejects stale replays cheaply before we touch
Redis; step 4 blocks same-timestamp replays within the skew window;
step 5 ensures a leaked secret can't inject data for phantom serials.

---

## 8. Replay cache (day 1)

Implementation: Redis `SET key "1" NX EX 600`.

* Key: `panoptic:replay:{serial}:{timestamp}:{sig[:16]}`
  * `sig[:16]` (first 16 hex chars) keeps keys short; collisions at this
    prefix length for a single (serial, ts) window are negligible.
* TTL: 600 s matches the skew window (300 s) + a safety margin.
* Failure mode: Redis unavailable → fail closed (reject 503). Webhook is
  already dead without Redis anyway (streams can't enqueue).

Not day-1-optional: the cost is one Redis round-trip, well under 1 ms on
the co-located deployment.

---

## 9. Dev-mode disable flag

Auth is **on by default.** Disabling requires **both**:

* `PANOPTIC_DEV_MODE=true`
* `PANOPTIC_AUTH_ENABLED=false`

While auth is disabled, log a loud warning:

```
WARNING: panoptic trailer auth is DISABLED (dev mode). Any client can push.
```

Emission rules:

* Emit once on webhook startup.
* Emit once every **60 seconds** while disabled, regardless of traffic.
* Log level WARNING, prefixed clearly, ideally in a distinguishable color
  if TTY.

In CI / prod neither variable should ever be set. Any code path that
reads `PANOPTIC_AUTH_ENABLED` must also read `PANOPTIC_DEV_MODE` and
refuse to disable auth unless both are true.

---

## 10. Known-trailer registry

New table `panoptic_trailers`:

```sql
CREATE TABLE panoptic_trailers (
    serial_number   TEXT PRIMARY KEY,
    name            TEXT,                -- human label, optional
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes           TEXT
);
```

CLI for operators: `scripts/add_trailer.py --serial <sn> --name "..."`.
Upserts with `is_active=true`. Revocation: `scripts/revoke_trailer.py --serial <sn>`
sets `is_active=false` (serial becomes 403).

Webhook caches the active-serial set in memory with a short refresh
interval (e.g. 30 s) to avoid a DB lookup per request.

---

## 11. Response behavior

| Outcome | HTTP | Body |
|---|---|---|
| Valid auth | pass through | (endpoint's normal response) |
| Missing / malformed headers | 401 | `{"error":"invalid_auth"}` |
| Stale timestamp or replay | 401 | `{"error":"invalid_auth"}` |
| Signature mismatch | 401 | `{"error":"invalid_auth"}` |
| Unknown / revoked serial | 403 | `{"error":"invalid_trailer"}` |

Responses never leak which step failed. Detailed reasons go to logs only.

---

## 12. Logging

On auth failure, log at WARNING level:

* category (`missing_header`, `bad_format`, `stale_timestamp`, `replayed`, `bad_signature`, `unknown_serial`)
* serial if present (else `<unset>`)
* request path and method
* `X-Real-IP` or remote IP
* timestamp header value
* first 8 hex chars of provided signature (if any)

Do **not** log:

* the shared secret
* the full signature
* the request body

On auth success, no per-request log (traffic volume is too high). Rely
on existing endpoint INFO logs.

---

## 13. Panoptic implementation surface

ASGI middleware is the right shape — multipart body hashing cannot
work from a FastAPI `Depends` since `Depends` runs after body parsing.

Files to touch or create:

| File | Change |
|---|---|
| `infra/migrations/versions/006_add_trailer_registry.py` | NEW — Alembic migration for `panoptic_trailers` |
| `shared/db/models.py` | NEW model `Trailer` |
| `shared/auth/hmac_auth.py` | NEW — signing-string construction, HMAC verify, replay cache integration, registry lookup |
| `services/trailer_webhook/middleware.py` | NEW — ASGI middleware that buffers raw body, performs all 8 verification steps, then replays body to app |
| `services/trailer_webhook/app.py` | Mount the middleware on the FastAPI app |
| `services/trailer_webhook/server.py` | Read new env vars, warn-loop on disabled auth |
| `scripts/add_trailer.py` | NEW — CLI to add trailer to registry |
| `scripts/revoke_trailer.py` | NEW — CLI to revoke trailer |
| `scripts/sign_request.py` | NEW — helper library trailer-side and a CLI wrapper for curl testing |
| `scripts/dev_fake_trailer.py` | Sign outgoing requests via `sign_request.py`; call `add_trailer.py` for test serial if not present |
| `scripts/seed_synthetic.py` | Same; add its 4 trailer serials to registry before seeding |
| `scripts/dev_idempotency_test.py` | Same |
| `scripts/dev_reclaim_test.py` | Same |
| `.env.example` | Add the new auth env vars |
| `docs/M1_RESULTS.md` | Quick note that M1 tests now use signed requests |

### 13.1 Middleware sketch

```python
class TrailerAuthMiddleware:
    def __init__(self, app, verifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_trailer_endpoint(scope["path"]):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)         # buffer once
        decision = await self.verifier.check(scope, body)
        if not decision.ok:
            await _respond(send, decision.status, decision.body)
            return

        # Replay the body to the downstream app.
        await self.app(scope, _wrap_receive(body), send)
```

Only `/v1/trailer/*` paths go through verification. `/health` and admin
endpoints bypass. No change to FastAPI handler signatures.

---

## 14. Panoptic-side configuration

New env vars (added to `.env.example` and panoptic-store-side if needed):

| Var | Default | Required | Purpose |
|---|---|---|---|
| `PANOPTIC_SHARED_SECRET_ACTIVE` | *(none)* | yes (when auth on) | current signing secret |
| `PANOPTIC_SHARED_SECRET_PREVIOUS` | *(none)* | no | previous signing secret, for rotation overlap |
| `PANOPTIC_AUTH_ENABLED` | `true` | no | must be `false` AND `PANOPTIC_DEV_MODE=true` to disable |
| `PANOPTIC_DEV_MODE` | `false` | no | paired with above |
| `PANOPTIC_AUTH_MAX_SKEW_SEC` | `300` | no | clock-skew window |
| `PANOPTIC_AUTH_REPLAY_TTL_SEC` | `600` | no | Redis replay cache TTL |
| `PANOPTIC_AUTH_REGISTRY_REFRESH_SEC` | `30` | no | in-memory serial cache TTL |

---

## 15. Trailer-side implementation

Pseudocode:

```python
import hmac, hashlib, time

def sign_request(secret, serial, method, path, body_bytes):
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    signing_string = f"{serial}|{ts}|{method.upper()}|{path}|{body_hash}"
    sig = hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Panoptic-Serial":    serial,
        "X-Panoptic-Timestamp": ts,
        "X-Panoptic-Signature": sig,
    }
```

A shared library (`scripts/sign_request.py`) exposes this for both the
trailer-side client and dev scripts, so both sides sign identically.

---

## 16. Rollout

### Phase 1 — this milestone (M2)

* migration + `panoptic_trailers` table
* ASGI middleware + `shared/auth/hmac_auth.py`
* env wiring + dev-mode flag with loud logging
* replay cache in Redis
* dual-secret support (day 1)
* update all dev scripts to sign
* add M1's test trailer serials to registry
* smoke: unauth request rejected, signed request passes end-to-end
* rotation dry-run: set PREVIOUS + ACTIVE, verify both work, drop PREVIOUS

### Phase 2 — later, only if signal demands

* per-trailer secrets (if fleet-secret compromise risk becomes real)
* stronger replay protection (hashed-body-based cache instead of `sig[:16]`)
* rate limiting / abuse_prevention at Caddy layer

### Phase 3 — very unlikely

* mTLS / JWT / PKI

Not adding any of these proactively.

---

## 17. Security properties

### Protects against

* random internet junk traffic
* accidental unauthenticated pushes
* casual spoofing without the secret
* request-body tampering
* endpoint-path/method tampering
* stale replay outside the skew window
* exact-replay inside the skew window (Redis cache)
* injection of data for serials not in the registry

### Does **not** protect against

* a fully compromised trailer leaking the fleet secret
* a compromised trailer impersonating another registered serial (it knows the secret)
* high-volume replay attacks targeting very new timestamps faster than Redis SETNX can respond (acceptable; no real threat vector)

The spec accepts these tradeoffs.

---

## 18. Example

### Signing (trailer side)

```
serial         = "YARD-A-001"
timestamp      = "1776201234"
method         = "POST"
path           = "/v1/trailer/image"
body_sha256    = "6d8f1d…a1f0"

signing_string = "YARD-A-001|1776201234|POST|/v1/trailer/image|6d8f1d…a1f0"
signature      = hmac_sha256(PANOPTIC_SHARED_SECRET_ACTIVE, signing_string)
               = "8c2d4f…0b9e"
```

### Request on the wire

```
POST /v1/trailer/image HTTP/1.1
Host: panoptic.surveillx.ai
X-Panoptic-Serial:    YARD-A-001
X-Panoptic-Timestamp: 1776201234
X-Panoptic-Signature: 8c2d4f…0b9e
Content-Type:         multipart/form-data; boundary=----abc
Content-Length:       <N>

------abc
Content-Disposition: form-data; name="metadata"

{"event_id":…}
------abc
Content-Disposition: form-data; name="image"; filename="frame.jpg"
Content-Type: image/jpeg

<jpeg bytes>
------abc--
```

### Verification (Panoptic side)

Middleware reads raw body, confirms timestamp within skew, checks Redis
replay cache, looks up serial in registry, recomputes body hash +
signing string + HMAC (active then previous), constant-time compares.
On pass, replays buffered body to the FastAPI handler unchanged.
