#!/usr/bin/env bash
set -euo pipefail

LABEL="com.open-trader.prediction-arbitrage-health"
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"

if /bin/launchctl bootout "gui/$UID/$LABEL" 2>/dev/null; then
  echo "booted out: $LABEL"
fi
if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "removed: $PLIST_PATH"
else
  echo "no plist at $PLIST_PATH"
fi
