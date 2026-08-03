#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LABEL="com.open-trader.account-api"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) [[ $# -ge 2 ]] || { echo "missing value for --repo-root" >&2; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { echo "missing value for --launch-agents-dir" >&2; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    *) echo "usage: $0 [--repo-root PATH] [--launch-agents-dir PATH]" >&2; exit 2 ;;
  esac
done

PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
"$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>/dev/null || true
if "$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  echo "launchd job is still loaded: $LABEL; preserving $PLIST_PATH" >&2
  exit 1
fi
if [[ -f "$PLIST_PATH" ]]; then
  rm "$PLIST_PATH"
  echo "removed launchd agent: $PLIST_PATH"
else
  echo "launchd agent not installed: $PLIST_PATH"
fi
