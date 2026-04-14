#!/usr/bin/env bash
#
# Start all Panoptic services in the background with log files.
#
# Usage:
#   ./scripts/run_all.sh              (reads .env from project root)
#   DATABASE_URL=... ./scripts/run_all.sh   (override .env values)
#
# Logs:  /tmp/panoptic-logs/<service>.log
# Stop:  ./scripts/run_all.sh stop
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="/tmp/panoptic-logs"
PID_DIR="/tmp/panoptic-pids"

# Load .env if it exists
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Activate virtualenv if it exists
VENV="${VENV:-$HOME/.virtualenvs/panoptic}"
if [ -f "$VENV/bin/activate" ]; then
    source "$VENV/bin/activate"
fi

# All services and their module paths
declare -A SERVICES=(
    [trailer_webhook]="services.trailer_webhook.server"
    [search_api]="services.search_api.server"
    [panoptic_summary_agent]="services.panoptic_summary_agent.worker"
    [panoptic_rollup_worker]="services.panoptic_rollup_worker.worker"
    [panoptic_embedding_worker]="services.panoptic_embedding_worker.worker"
    [panoptic_image_caption_worker]="services.panoptic_image_caption_worker.worker"
    [panoptic_caption_embed_worker]="services.panoptic_caption_embed_worker.worker"
)

# Ordered startup (webhook first, then workers)
STARTUP_ORDER=(
    trailer_webhook
    search_api
    panoptic_summary_agent
    panoptic_rollup_worker
    panoptic_embedding_worker
    panoptic_image_caption_worker
    panoptic_caption_embed_worker
)

_start() {
    mkdir -p "$LOG_DIR" "$PID_DIR"

    # Check DATABASE_URL
    if [ -z "${DATABASE_URL:-}" ]; then
        echo "ERROR: DATABASE_URL is not set"
        exit 1
    fi

    echo "Starting Panoptic services..."
    echo "  Logs: $LOG_DIR/"
    echo ""

    cd "$PROJECT_DIR"

    for name in "${STARTUP_ORDER[@]}"; do
        module="${SERVICES[$name]}"
        pid_file="$PID_DIR/$name.pid"
        log_file="$LOG_DIR/$name.log"

        # Skip if already running
        if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "  $name — already running (pid $(cat "$pid_file"))"
            continue
        fi

        PYTHONPATH="$PROJECT_DIR" python3 -m "$module" \
            >> "$log_file" 2>&1 &
        echo $! > "$pid_file"
        echo "  $name — started (pid $!, log: $log_file)"
    done

    echo ""
    echo "All services started. Use '$0 status' to check, '$0 stop' to stop."
}

_stop() {
    echo "Stopping Panoptic services..."
    for name in "${STARTUP_ORDER[@]}"; do
        pid_file="$PID_DIR/$name.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                echo "  $name — stopped (pid $pid)"
            else
                echo "  $name — not running"
            fi
            rm -f "$pid_file"
        else
            echo "  $name — no pid file"
        fi
    done
}

_status() {
    echo "Panoptic service status:"
    for name in "${STARTUP_ORDER[@]}"; do
        pid_file="$PID_DIR/$name.pid"
        if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "  $name — running (pid $(cat "$pid_file"))"
        else
            echo "  $name — stopped"
        fi
    done
}

_logs() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo "Usage: $0 logs <service_name>"
        echo "Services: ${STARTUP_ORDER[*]}"
        exit 1
    fi
    local log_file="$LOG_DIR/$name.log"
    if [ -f "$log_file" ]; then
        tail -f "$log_file"
    else
        echo "No log file found: $log_file"
        exit 1
    fi
}

case "${1:-start}" in
    start)   _start ;;
    stop)    _stop ;;
    restart) _stop; sleep 1; _start ;;
    status)  _status ;;
    logs)    _logs "${2:-}" ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs <service>}"
        exit 1
        ;;
esac
