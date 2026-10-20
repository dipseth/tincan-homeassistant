#!/usr/bin/env bash
# Copy custom_components/tincan to a Home Assistant box over SSH and restart Core.
#   ./install.sh                 # uses the "homeassistant" entry in ~/.ssh/config
#   ./install.sh user@10.0.0.93  # any ssh target
#   HA_CONFIG=/homeassistant ./install.sh   # override the remote config dir
set -euo pipefail
HOST="${1:-homeassistant}"
HA_CONFIG="${HA_CONFIG:-/config}"
SRC="$(cd "$(dirname "$0")" && pwd)/custom_components/tincan"
DEST="$HA_CONFIG/custom_components/tincan"

echo "→ $HOST:$DEST"
ssh "$HOST" "mkdir -p '$DEST'"
COPYFILE_DISABLE=1 tar -C "$SRC" --exclude='__pycache__' -cf - . | ssh "$HOST" "tar -C '$DEST' -xf -"
ssh "$HOST" "find '$DEST' -name '*.py' | sort | sed 's|^|  |'"

if ssh "$HOST" 'ha core restart 2>/dev/null || curl -sf -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/core/restart >/dev/null' 2>/dev/null; then
  echo "✓ Home Assistant Core restarting"
else
  echo "! Could not restart via the 'ha' CLI; restart Home Assistant from Settings → System → Restart"
fi
echo "Then: Settings → Devices & services → Add integration → \"Tin Can\""
