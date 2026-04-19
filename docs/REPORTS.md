# Panoptic Reports (M9)

Async HTML report generation for trailers. Daily and weekly windows,
stored on disk and indexed in Postgres. HTTP and cron go through the same
enqueue path.

## Surface

| HTTP | Method | Purpose |
|---|---|---|
| `/v1/reports/daily` | POST | enqueue a daily report for (`serial_number`, `date`) |
| `/v1/reports/weekly` | POST | enqueue a weekly report for (`serial_number`, `iso_week`) |
| `/v1/reports/{report_id}` | GET | status + metadata + storage_path |
| `/v1/reports/{report_id}/assets/{image_id}.jpg` | GET | authorized image asset (only image_ids cited by this report) |

POST returns `{report_id, status}` immediately. Status values: `pending
→ running → success` on happy path, or `pending → running → failed`
with `last_error` populated on terminal failure. Reports are
content-addressed on `(serial, kind, window_start, window_end)` so
re-POSTing for the same window reuses the existing row.

## Storage

```
/data/panoptic-store/reports/<serial>/<YYYY>/<MM>/<serial>-<YYYYMMDD>-daily.html
/data/panoptic-store/reports/<serial>/<YYYY>/<MM>/<serial>-<YYYYWww>-weekly.html
```

Root configurable via `REPORT_STORAGE_ROOT` (defaults to the path above).

## Cron

User crontab:

```cron
# 03:45 UTC — prune HTML files older than 90 days (keeps Postgres rows).
45 3 * * * cd /home/surveillx/panoptic && .venv/bin/python scripts/prune_reports.py --apply >> logs/prune.log 2>&1

# 03:50 UTC — nightly daily report for every active trailer (yesterday's window).
50 3 * * * cd /home/surveillx/panoptic && .venv/bin/python scripts/generate_reports.py --daily --all-active --date $(date -u -d yesterday +\%Y-\%m-\%d) --apply >> logs/reports.log 2>&1

# 04:00 UTC Mondays — weekly report for every active trailer (prior ISO week).
0 4 * * 1 cd /home/surveillx/panoptic && .venv/bin/python scripts/generate_reports.py --weekly --all-active --iso-week $(date -u -d 'last monday' +\%GW\%V) --apply >> logs/reports.log 2>&1
```

## Operator commands

```bash
# On-demand (same as cron, for one trailer)
curl -sSf -X POST http://localhost:8600/v1/reports/daily \
  -H 'Content-Type: application/json' \
  -d '{"serial_number":"1422725077375","date":"2026-04-18"}'

# Poll for completion
curl -sSf http://localhost:8600/v1/reports/<report_id> | jq .

# Dry-run a full-fleet cron run
.venv/bin/python scripts/generate_reports.py --daily --all-active --date 2026-04-18

# Dry-run retention pruning
.venv/bin/python scripts/prune_reports.py --dry-run
```

## Data contract

### `panoptic_reports` (migration 010)

| Column | Type | Notes |
|---|---|---|
| `report_id` | TEXT PK | `sha256(serial, kind, window_start, window_end)`; `template_version` NOT in hash |
| `serial_number` | TEXT | |
| `kind` | TEXT | `daily` \| `weekly` |
| `window_start_utc` | TIMESTAMPTZ | `window` unique with `(serial, kind)` |
| `window_end_utc` | TIMESTAMPTZ | |
| `storage_path` | TEXT \| NULL | absolute path when status=success; nulled on prune |
| `status` | TEXT | `pending` \| `running` \| `success` \| `failed` |
| `last_error` | TEXT \| NULL | populated when status=failed |
| `generated_at` | TIMESTAMPTZ \| NULL | set on status=success |
| `metadata_json` | JSONB | provenance + narrative cache (see below) |

### `metadata_json` on a `success` report

```json
{
  "cited_image_ids":   ["..."],
  "cited_event_ids":   ["..."],
  "cited_summary_ids": ["..."],
  "cited_camera_ids":  ["..."],
  "input_counts": {"summaries": N, "images": N, "events": N, "cameras": N},
  "coverage":     {"cameras_with_data": N, "cameras_total": N},
  "vlm_timings_ms": {},
  "template_version": "v1",
  "narratives": [
    {"key": "cam-01" or "2026-04-13", "headline": "...", "summary": "...", "confidence": 0.85}
  ],
  "overall": {"headline": "...", "summary": "...", "confidence": 0.85, "supporting": [...]}
}
```

Weekly reports consume `metadata.narratives` + `metadata.overall` from
the 7 daily reports that cover the ISO week. Missing days render as
"No daily report was produced for this date" placeholders.

## Scope (v1)

**In:** daily + weekly HTML, async generation, stored artifacts, cron
generation, static-served cited images, 90-day on-disk retention.

**Out:** PDF, email/Slack/PagerDuty delivery, cross-trailer fleet
reports, verify-integration (`/v1/search/verify`), custom/branded
styling, alternate output formats. Each is a potential M9.1+ or later.

## Known limitations

- Weekly reports DO NOT on-the-fly synthesize a missing day from raw
  data in v1. If a daily is missing (worker outage, trailer offline),
  the weekly renders that slot as a placeholder. Workaround: manually
  re-POST `/v1/reports/daily` for the missing date, then re-POST the
  weekly.
- `max_attempts=1` on report_generate jobs: VLM failures go straight to
  DLQ. Use `scripts/dlq_replay.py --job-type report_generate` to retry.
- `_dedup_images` uses a 5-min cluster tuned for short periods. For
  24-hour daily windows it may over-collapse cross-hour clusters.
  Flag for M9.1 tuning.
- Asset endpoint serves only cited image_ids. Weekly's cited list is
  the union of its member dailies' cited lists.
- `REPORT_STORAGE_ROOT` directory is written by the worker container
  as root (bind-mount). Files are readable from the host; delete
  requires either running prune in-container or host-level root.
