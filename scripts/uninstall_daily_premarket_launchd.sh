#!/usr/bin/env bash
set -euo pipefail

TARGET="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"

if [[ -f "$TARGET" ]]; then
  launchctl unload "$TARGET" 2>/dev/null || true
  rm "$TARGET"
  echo "removed launchd agent: $TARGET"
else
  echo "launchd agent not installed: $TARGET"
fi
