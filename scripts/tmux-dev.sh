#!/usr/bin/env bash
# DEV-ONLY alternative to docker-compose.yml (M7).
#
# The canonical bring-up is `docker compose up -d` at the repo root.
# This script is retained because it lets you edit code and see
# process output inline without rebuilding or restarting containers.
#
# Don't run this AND compose at the same time — they fight for the
# same ports (webhook 8100, search 8600, healthz 8201-8206/8210).
#
#   start:   ./scripts/tmux-dev.sh
#   attach:  tmux a -t panoptic
#   stop:    tmux kill-session -t panoptic

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION=panoptic
VENV="$REPO_ROOT/.venv/bin/python"

mkdir -p "$REPO_ROOT/logs"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already running."
  echo "attach:  tmux a -t $SESSION"
  echo "stop:    tmux kill-session -t $SESSION"
  exit 0
fi

mkwindow() {
  local name="$1"; local module="$2"
  local cmd="cd $REPO_ROOT && set -a && source .env && set +a && $VENV -m $module 2>&1 | tee -a logs/${name}.log"
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -n "$name" "bash -c '$cmd'"
  else
    tmux new-window -t "$SESSION:" -n "$name" "bash -c '$cmd'"
  fi
}

mkwindow webhook         services.trailer_webhook.server
mkwindow caption         services.panoptic_image_caption_worker.worker
mkwindow cap_embed       services.panoptic_caption_embed_worker.worker
mkwindow img_embed       services.panoptic_image_embed_worker.worker
mkwindow summary         services.panoptic_summary_agent.worker
mkwindow sum_embed       services.panoptic_embedding_worker.worker
mkwindow rollup          services.panoptic_rollup_worker.worker
mkwindow event_producer  services.panoptic_event_producer.worker
mkwindow report_gen      services.panoptic_report_generator.worker
mkwindow reclaimer       services.panoptic_reclaimer.worker
mkwindow search          services.search_api.server

tmux select-window -t "$SESSION:webhook"

echo "started panoptic tmux session with 11 windows."
echo "attach:  tmux a -t $SESSION"
