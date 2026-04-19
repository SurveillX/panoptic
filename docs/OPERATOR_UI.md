# Panoptic Operator UI (M10)

Thin HTML surface over the Panoptic Search API. One operator answering
"what happened on trailer X on day Y, and can I see the supporting
evidence?"

Runs on `http://localhost:8400/`. No per-user auth — inherits the Search
API's internal trust boundary (see §Access below).

## Pages

| URL | Purpose |
|---|---|
| `/` | Fleet overview: active trailers, last-seen, 24h event count, latest daily report |
| `/trailer/{serial}/{yyyy-mm-dd}` | One-trailer-one-day rollup: events, images, summaries, per-camera counts, report-history panel, "generate daily report" button |
| `/search?q=...&type=event&serial=...` | URL-driven search. Query + filters + `type` checkboxes are all in the URL; results group into events / images / summaries |
| `/events/{event_id}` | Event detail: type, source, severity, timestamps, linked evidence (image preview + bucket ref), metadata JSON |
| `/summaries/{summary_id}` | Summary detail: level, scope, narrative, signal labels, metrics, coverage |
| `/images/{image_id}` | Image detail: full-size image, caption, metadata |
| `/reports/{report_id}/view` | Report viewer: iframe over the stored HTML from M9 |
| `/healthz` | JSON health check (includes Search API reachability) |

All detail pages fail to a clean "not found" template when their underlying ID
doesn't exist. Unknown URLs 404.

## Architecture

```
┌──────────────────────┐         loopback HTTP
│ panoptic_operator_ui │ ─────────────────────▶  ┌──────────────────┐
│  FastAPI + Jinja2    │                          │ search_api       │
│  :8400               │ ◀─────── JSON ─────────  │ :8600            │
└──────────────────────┘                          └──────────────────┘
```

The UI holds **no** domain logic. Every data read goes through the Search API
via `services/panoptic_operator_ui/client.py`. No Postgres, no Redis, no DB
access. Frontend is HTMX + Jinja2 + functional CSS — no build step, no SPA.

## Backing API endpoints

The UI consumes these Search API endpoints (all read-only JSON unless noted):

| Endpoint | Used by |
|---|---|
| `GET /v1/fleet/overview` | fleet page |
| `GET /v1/trailer/{serial}/day/{yyyy-mm-dd}` | trailer-day page |
| `GET /v1/reports?serial_number=&kind=&limit=` | report-history panel |
| `GET /v1/reports/{id}` | report viewer header (status + metadata) |
| `GET /v1/reports/{id}/view` | iframe src — streams stored HTML |
| `GET /v1/reports/{id}/assets/{image_id}.jpg` | cited-image preview inside a report |
| `POST /v1/reports/daily` | "Generate daily report" button |
| `POST /v1/search` | search page |
| `GET /v1/events/{id}` | event detail page |
| `GET /v1/summaries/{id}` | summary detail page |
| `GET /v1/images/{id}` | image detail page (metadata) |
| `GET /v1/images/{id}.jpg` | image JPEG streaming (detail page + thumbnails) |

## Access

**The Search API (`:8600`) and the operator UI (`:8400`) are internal-only.**

- **No per-user auth on any page or endpoint.** Anyone who can reach `:8400` can
  browse any trailer's data. This is intentional for v1 and matches the existing
  M9 trust boundary.
- `GET /v1/images/{id}.jpg` has **no** cited-id check (unlike the M9 report-asset
  endpoint `/v1/reports/{id}/assets/{image_id}.jpg`, which only serves images
  cited by that specific report). This is the broadest new data-access vector
  in M10.
- Today only the webhook (`:8100`) is publicly tunneled through Caddy + FRP, and
  that path enforces HMAC auth. **Neither `:8400` nor `:8600` is tunneled.**
- **Do NOT add a public tunnel** for `:8400` or `:8600` without first landing a
  stronger auth layer (per-user session, API key at the Caddy edge, or
  trailer-scoped ACLs). Tunneling these without auth would leak every
  trailer's images + narrative to the public internet.
- Per-user auth, trailer-scoped ACLs, and session/SSO are explicit non-goals for
  M10. They land with (or before) M11 when the surface expands.

The `docker-compose.yml` entry for `search_api` carries a multi-line comment
reinforcing the boundary. Keep it there.

## Running locally

The UI is shipped as a container in the main `panoptic` compose stack.

```bash
cd ~/panoptic
docker compose up -d operator_ui
open http://localhost:8400/
```

Dev alternative (tmux, no container):

```bash
cd ~/panoptic
.venv/bin/python -m services.panoptic_operator_ui.server
# → listens on :8400 (configurable via OPERATOR_UI_PORT)
```

The UI calls `SEARCH_API_URL` (default `http://localhost:8600`), which must be
reachable.

Environment variables (see `.env.example`):
- `OPERATOR_UI_PORT=8400`
- `SEARCH_API_URL=http://localhost:8600`
- `OPERATOR_UI_API_TIMEOUT_SEC=15`

## UX / data-model principles (locked)

- **Evidence-first.** Every claim links to its source — event, image,
  summary, report.
- **Every page has a URL.** No modal-only content; no SPA routing.
- **Provenance visible.** Severity, confidence, cited-id counts shown, not
  hidden.
- **UTC everywhere.** All timestamps rendered in UTC with explicit `Z`. No
  silent timezone conversion.
- **URL-driven search.** `GET /search?q=...&type=event&serial=...` — the
  URL is the source of truth, shareable via copy-paste.
- **Hit the API, not the DB.** The UI service never SELECTs. If data is
  missing, add it to the Search API first.

## Known limitations (v1 → M10.1+)

- **No browser-local time rendering.** All timestamps are UTC. Inline JS to
  display local-time tooltips is a P10.5.1 candidate.
- **No pagination on search results.** Capped at `top_k=50`. For deeper
  browsing, refine filters.
- **Report viewer is an iframe.** Cross-origin to `:8600`. Works because
  both origins are loopback. A cleaner direct-HTML proxy is post-M10.
- **No agent / chat.** M11.
- **No cross-trailer rollups or time-series.** M12+ if ever.
- **`/v1/images/{id}.jpg` bypasses M9's cited-id check.** See §Access.
- **Reports are trailer-scoped — no camera-subset reports yet.**
  On construction sites, a typical trailer has 1–2 cameras focused on the
  primary construction area and several more on perimeter/security. Operators
  will want "construction-focused" reports that narrate *just* the
  primary-camera subset. Today:
  - Search and verify already accept `filters.camera_id` (exact-match).
  - `POST /v1/summarize/period` already takes `PeriodScope.camera_ids` as a
    list, so a live camera-scoped narrative is available without any
    backend change — just not persisted as an HTML report.
  - Persisted daily/weekly reports are trailer-scoped only
    (`DailyReportRequest` has no `camera_ids` field; the `report_id` hash
    doesn't include camera_ids).

  **Planned evolution** (sequenced by cost, deferred until real operator
  feedback):
  1. Operator UI action on the trailer-day page that runs a live
     camera-scoped period summary via the existing endpoint (zero backend
     change).
  2. Extend `DailyReportRequest` / `WeeklyReportRequest` with optional
     `camera_ids: list[str]`. Persist the subset in
     `panoptic_reports.metadata_json.scope_cameras` and include its sorted
     tuple in the `report_id` hash so whole-trailer and camera-subset
     reports coexist without collision.
  3. Named camera groups (new `panoptic_camera_groups(serial, name,
     camera_ids)` table + UI). Reports can reference a group name —
     e.g., "daily report for YARD-A-001's construction group."

  Revisit after real customer usage informs which of the three is worth
  the schema.

## Service wiring

- Container: `panoptic-operator-ui` (docker-compose.yml)
- Tmux window: `operator_ui` (scripts/tmux-dev.sh)
- Health port: same as HTTP port (`:8400/healthz`)
- Logs: Docker json-file (rotated 50MB × 5) or `logs/operator_ui.log` in tmux

## M11 notes

The agent layer (M11) will call the same Search API endpoints. The
operator UI's `client.py` is a reasonable reference for which endpoints
the agent will want as tools. No UI-specific fields on the API responses —
deliberate, to avoid agent-side translation.
