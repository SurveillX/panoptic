#!/usr/bin/env bash
# Install the Panoptic logrotate config via symlink.
# Requires sudo.
#
#   ./scripts/install_logrotate.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/deploy/logrotate/panoptic.conf"
DEST="/etc/logrotate.d/panoptic"

if [[ ! -f "$SRC" ]]; then
  echo "source missing: $SRC" >&2
  exit 1
fi

# /etc/logrotate.d/ rejects symlinks on some distros — copy instead.
echo "installing logrotate config: $SRC -> $DEST"
sudo cp "$SRC" "$DEST"
sudo chmod 0644 "$DEST"

echo "verifying..."
sudo logrotate --debug /etc/logrotate.d/panoptic 2>&1 | tail -20

echo "done. dry-run:  sudo logrotate -d /etc/logrotate.d/panoptic"
echo "       force:   sudo logrotate -f /etc/logrotate.d/panoptic"
