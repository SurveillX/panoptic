#!/usr/bin/env bash
# Install frpc config + systemd unit on this Spark.
#
# Usage (one-time):
#   echo 'LsOAsYBy/...real-token...' > ~/panoptic/deploy/frpc/auth_token.txt
#   chmod 600 ~/panoptic/deploy/frpc/auth_token.txt
#   sudo ./scripts/install_frpc.sh   # or  deploy/frpc/install.sh
#
# The token file (auth_token.txt) is gitignored — never committed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$REPO_ROOT/deploy/frpc/frpc.toml.template"
TOKEN_FILE="$REPO_ROOT/deploy/frpc/auth_token.txt"
UNIT_SRC="$REPO_ROOT/deploy/frpc/frpc.service"

CONFIG_DEST="/etc/frp/frpc.toml"
UNIT_DEST="/etc/systemd/system/frpc.service"
LOG_DIR="/var/log/frpc"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "missing token file: $TOKEN_FILE" >&2
  echo "create it with the frps auth.token value (mode 600)." >&2
  exit 1
fi

TOKEN="$(< "$TOKEN_FILE")"
if [[ -z "$TOKEN" ]]; then
  echo "token file is empty: $TOKEN_FILE" >&2
  exit 1
fi

echo "==> ensuring dirs"
sudo install -d -m 0755 /etc/frp
sudo install -d -m 0755 "$LOG_DIR"

echo "==> writing $CONFIG_DEST (root:root 0600)"
tmp="$(mktemp)"
sed "s|__FRP_AUTH_TOKEN__|${TOKEN}|" "$TEMPLATE" > "$tmp"
sudo install -o root -g root -m 0600 "$tmp" "$CONFIG_DEST"
rm -f "$tmp"

echo "==> installing $UNIT_DEST"
sudo install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DEST"

echo "==> enabling + starting frpc"
sudo systemctl daemon-reload
sudo systemctl enable frpc
sudo systemctl restart frpc

echo
echo "status:"
sudo systemctl status frpc --no-pager | head -12 || true
echo
echo "logs:"
sudo tail -n 20 "$LOG_DIR/frpc.log" 2>/dev/null || echo "  (log not yet created)"
echo
echo "done. verify from your laptop:"
echo "  curl -v https://panoptic.surveillx.ai/health"
