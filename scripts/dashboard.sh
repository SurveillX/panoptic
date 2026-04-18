#!/usr/bin/env bash
# One-page Panoptic status dashboard.
#   ./scripts/dashboard.sh              — one-shot
#   watch -n 5 ./scripts/dashboard.sh   — refresh every 5s
#
# Exit code:
#   0 — every service ok
#   1 — at least one degraded
#   2 — at least one error / unreachable

set -uo pipefail

# --- config ---
WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:8100}"
SEARCH_URL="${SEARCH_URL:-http://localhost:8600}"
CAPTION_URL="${CAPTION_URL:-http://localhost:8201}"
CAP_EMBED_URL="${CAP_EMBED_URL:-http://localhost:8202}"
SUMMARY_URL="${SUMMARY_URL:-http://localhost:8203}"
SUM_EMBED_URL="${SUM_EMBED_URL:-http://localhost:8204}"
ROLLUP_URL="${ROLLUP_URL:-http://localhost:8205}"
IMAGE_EMBED_URL="${IMAGE_EMBED_URL:-http://localhost:8206}"
RECLAIMER_URL="${RECLAIMER_URL:-http://localhost:8210}"

STORE_CONTAINERS=(panoptic-postgres panoptic-qdrant panoptic-redis)
GPU_CONTAINERS=(panoptic-vllm panoptic-retrieval-retrieval-1)

DATA_DIR="${DATA_DIR:-/data/panoptic-store}"
LOG_DIR="${LOG_DIR:-$HOME/panoptic/logs}"

OVERALL=0

# --- helpers ---
_curl_health() {
  curl -sS -m 3 "$1/health" 2>/dev/null \
    || curl -sS -m 3 "$1/healthz" 2>/dev/null \
    || echo ''
}

_jqval() {
  # _jqval <json> <jq-filter> <default>
  local out
  out=$(printf '%s' "$1" | jq -r "$2" 2>/dev/null)
  if [[ -z "$out" || "$out" == "null" ]]; then
    printf '%s' "$3"
  else
    printf '%s' "$out"
  fi
}

_age_sec() {
  # diff between now and an ISO-8601 UTC string (GNU date)
  local iso="$1"
  [[ -z "$iso" || "$iso" == "null" ]] && { echo '-'; return; }
  local then now
  then=$(date -u -d "$iso" +%s 2>/dev/null) || { echo '-'; return; }
  now=$(date -u +%s)
  echo "$((now - then))"
}

_service_row() {
  # _service_row <label> <url>
  local label="$1" url="$2"
  local body status lag last_claim deps_str consumer_stream pel xlen reclaim_reset reclaim_dlq reclaim_last_run reclaim_age
  body=$(_curl_health "$url")

  if [[ -z "$body" ]]; then
    printf '  %-14s %-5s %s\n' "$label" "UNREACH" ""
    OVERALL=2
    return
  fi

  status=$(_jqval "$body" '.status' "unknown")
  last_claim=$(_jqval "$body" '.jobs.last_claim_at' "")
  pel=$(_jqval "$body" '.consumer.pending_pel' "-")
  xlen=$(_jqval "$body" '.consumer.xlen' "-")
  reclaim_last_run=$(_jqval "$body" '.reclaim.last_run_at' "")
  reclaim_reset=$(_jqval "$body" '.reclaim.totals.reset_to_pending' "-")
  reclaim_dlq=$(_jqval "$body" '.reclaim.totals.sent_to_dlq' "-")
  deps_str=$(printf '%s' "$body" | jq -r '
    .dependencies // {} | to_entries
    | map((if .value.ok then "" else "✗" end) + .key)
    | join(",")
  ' 2>/dev/null)
  [[ -z "$deps_str" ]] && deps_str="-"

  local extra=""
  if [[ "$reclaim_last_run" != "" && "$reclaim_last_run" != "null" ]]; then
    reclaim_age=$(_age_sec "$reclaim_last_run")
    extra="reset=$reclaim_reset dlq=$reclaim_dlq last_run=${reclaim_age}s ago"
  else
    local age
    age=$(_age_sec "$last_claim")
    if [[ "$pel" == "-" ]]; then
      extra="(no consumer)"
    elif [[ "$age" == "-" ]]; then
      extra="lag=$pel/$xlen last_claim=-"
    else
      extra="lag=$pel/$xlen last_claim=${age}s ago"
    fi
  fi

  case "$status" in
    ok)       status_str="OK" ;;
    degraded) status_str="DEGR"; [[ "$OVERALL" -lt 1 ]] && OVERALL=1 ;;
    error)    status_str="ERR"; OVERALL=2 ;;
    *)        status_str="?" ;;
  esac

  printf '  %-14s %-5s %-40s deps=%s\n' "$label" "$status_str" "$extra" "$deps_str"
}

# --- output ---

printf '\npanoptic stack — %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S')"

echo 'SERVICES'
_service_row "webhook"    "$WEBHOOK_URL"
_service_row "caption"    "$CAPTION_URL"
_service_row "cap_embed"  "$CAP_EMBED_URL"
_service_row "summary"    "$SUMMARY_URL"
_service_row "sum_embed"  "$SUM_EMBED_URL"
_service_row "rollup"     "$ROLLUP_URL"
_service_row "img_embed"  "$IMAGE_EMBED_URL"
_service_row "reclaimer"  "$RECLAIMER_URL"
_service_row "search_api" "$SEARCH_URL"

echo
echo 'CONTAINERS'
if command -v docker >/dev/null 2>&1; then
  docker stats --no-stream \
    --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
    "${STORE_CONTAINERS[@]}" "${GPU_CONTAINERS[@]}" 2>/dev/null \
    | sed 's/^/  /'
else
  echo '  docker not on PATH'
fi

echo
echo 'DISK'
df -h / 2>/dev/null | awk 'NR>1 { printf "  %-35s %4s free of %4s  (%s used)\n", "/", $4, $2, $5 }'
if [[ -d "$DATA_DIR" ]]; then
  printf '  %-35s ' "$DATA_DIR"
  du -sh "$DATA_DIR" 2>/dev/null | awk '{ print $1 " used" }'
fi

echo
echo 'LOGS'
if [[ -d "$LOG_DIR" ]]; then
  local_total=$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)
  local_count=$(find "$LOG_DIR" -maxdepth 1 -name '*.log' | wc -l)
  printf '  %s — %s files, %s total\n' "$LOG_DIR" "$local_count" "$local_total"
else
  echo "  $LOG_DIR missing"
fi

echo
exit "$OVERALL"
