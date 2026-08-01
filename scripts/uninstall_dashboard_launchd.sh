#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) [[ $# -ge 2 ]] || { echo "missing value for --repo-root" >&2; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { echo "missing value for --launch-agents-dir" >&2; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    *) echo "usage: $0 [--repo-root PATH] [--launch-agents-dir PATH]" >&2; exit 2 ;;
  esac
done

status=0
for label in \
  com.open-trader.frontend-gateway \
  com.open-trader.legacy-dashboard \
  com.open-trader.dashboard
do
  plist="$LAUNCH_AGENTS_DIR/$label.plist"
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$label" 2>/dev/null || true
  if "$LAUNCHCTL_BIN" print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "launchd job is still loaded: $label; preserving $plist" >&2
    status=1
  elif [[ -f "$plist" ]]; then
    rm "$plist"
    echo "removed launchd agent: $plist"
  else
    echo "launchd agent not installed: $plist"
  fi
done
exit "$status"
