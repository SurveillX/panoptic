#!/usr/bin/env bash
# Live view of a single trailer's activity on the Panoptic stack.
#
# Shows three streams in parallel:
#   - webhook log lines mentioning the serial
#   - worker log lines mentioning the serial
#   - periodic DB snapshot (image + summary counts, latest rows)
#
# Usage:
#   ./scripts/watch_trailer.sh 1422725077375
#   ./scripts/watch_trailer.sh 1422725077375 --poll 3     # faster DB poll

set -uo pipefail

SERIAL="${1:-}"
if [[ -z "$SERIAL" ]]; then
  echo "usage: $0 <serial> [--poll <sec>]" >&2
  exit 1
fi

POLL_SEC=10
if [[ "${2:-}" == "--poll" && -n "${3:-}" ]]; then
  POLL_SEC="$3"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
cd "$REPO_ROOT"

# Load env (DATABASE_URL, etc.)
if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi

# ANSI-free plain text so it pipes/greps nicely.

cleanup() {
  kill "${TAIL_PID:-0}" 2>/dev/null || true
  kill "${POLL_PID:-0}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "=========================================="
echo "watching trailer: $SERIAL"
echo "webhook  :        logs/webhook.log"
echo "workers  :        logs/{caption,cap_embed,summary,sum_embed,rollup,reclaimer}.log"
echo "db poll every     ${POLL_SEC}s"
echo "Ctrl-C to stop"
echo "=========================================="
echo

# Tail webhook + worker logs, filtered to this serial + any image_id we've seen
# (image_ids pre-image-push aren't known, so we ALSO emit lines for any caption
# or embed job to reveal when jobs for this serial move).
(
  tail -F \
    "$LOG_DIR"/webhook.log \
    "$LOG_DIR"/caption.log \
    "$LOG_DIR"/cap_embed.log \
    "$LOG_DIR"/summary.log \
    "$LOG_DIR"/sum_embed.log \
    "$LOG_DIR"/rollup.log \
    "$LOG_DIR"/reclaimer.log \
    2>/dev/null \
  | grep --line-buffered -E "${SERIAL}|panoptic_trailers.*${SERIAL}"
) &
TAIL_PID=$!

# Periodic DB snapshot
(
  while :; do
    "$REPO_ROOT/.venv/bin/python" - <<PY
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["DATABASE_URL"])
sn = "${SERIAL}"
with e.connect() as c:
    img_all = c.execute(sa.text(
        "SELECT COUNT(*) FROM panoptic_images WHERE serial_number = :sn"
    ), {"sn": sn}).scalar()
    img_done = c.execute(sa.text(
        "SELECT COUNT(*) FROM panoptic_images WHERE serial_number = :sn "
        "AND caption_status='success' AND caption_embedding_status='success'"
    ), {"sn": sn}).scalar()
    img_latest = c.execute(sa.text(
        "SELECT image_id, trigger, caption_status, caption_embedding_status, "
        "       LEFT(caption_text, 70) AS cap, captured_at_utc "
        "  FROM panoptic_images WHERE serial_number = :sn "
        " ORDER BY created_at DESC LIMIT 1"
    ), {"sn": sn}).mappings().first()

    buck_all = c.execute(sa.text(
        "SELECT COUNT(*) FROM panoptic_buckets WHERE serial_number = :sn"
    ), {"sn": sn}).scalar()
    sum_all = c.execute(sa.text(
        "SELECT COUNT(*) FROM panoptic_summaries WHERE serial_number = :sn"
    ), {"sn": sn}).scalar()
    sum_done = c.execute(sa.text(
        "SELECT COUNT(*) FROM panoptic_summaries WHERE serial_number = :sn "
        "AND embedding_status='complete'"
    ), {"sn": sn}).scalar()
    sum_latest = c.execute(sa.text(
        "SELECT LEFT(summary, 100) AS summ, confidence, summary_mode, "
        "       embedding_status, start_time AS bs "
        "  FROM panoptic_summaries WHERE serial_number = :sn "
        " ORDER BY created_at DESC LIMIT 1"
    ), {"sn": sn}).mappings().first()

    jobs = c.execute(sa.text(
        "SELECT state, COUNT(*) FROM panoptic_jobs WHERE serial_number = :sn "
        "GROUP BY state ORDER BY state"
    ), {"sn": sn}).all()

print(f"\n----- DB snapshot @ \$(date +%H:%M:%S) -----")
print(f"buckets: {buck_all}  images: {img_done}/{img_all}  summaries: {sum_done}/{sum_all}")
if jobs: print("jobs:", ", ".join(f"{st}={n}" for st, n in jobs))
if img_latest:
    print(f"latest image: trigger={img_latest['trigger']} cap={img_latest['caption_status']}/{img_latest['caption_embedding_status']}")
    if img_latest['cap']:
        print(f"  caption: {img_latest['cap']!r}")
if sum_latest:
    print(f"latest summary: mode={sum_latest['summary_mode']} embedding={sum_latest['embedding_status']} conf={sum_latest['confidence']}")
    print(f"  summary: {sum_latest['summ']!r}")
print()
PY
    sleep "$POLL_SEC"
  done
) &
POLL_PID=$!

wait
