#!/usr/bin/env bash
set -euo pipefail

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
LABEL="com.open-trader.prediction-service"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --launch-agents-dir) [[ $# -ge 2 ]] || { echo "missing value for --launch-agents-dir" >&2; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    *) echo "usage: $0 [--launch-agents-dir PATH]" >&2; exit 2 ;;
  esac
done
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
# Keep command/shell overhead inside the validator's existing 30-second cleanup reserve.
CLEANUP_POLL_BUDGET=20
wait_agent_absent() {
  local output status
  while [[ "$CLEANUP_POLL_BUDGET" -gt 0 ]]; do
    CLEANUP_POLL_BUDGET=$((CLEANUP_POLL_BUDGET - 1))
    if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then status=0; else status=$?; fi
    if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then return 0; fi
    if [[ "$status" -ne 0 ]]; then echo "failed to inspect launchd label: $LABEL" >&2; printf '%s\n' "$output" >&2; return 1; fi
    [[ "$CLEANUP_POLL_BUDGET" -gt 0 ]] && sleep 1
  done
  echo "launchd job is still loaded after cleanup polling: $LABEL; preserving $PLIST_PATH" >&2
  return 1
}
listener_absent() {
  local output status
  if output="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN 2>&1)"; then
    [[ -z "$output" ]] && return 0
    echo "prediction service listener is still present on 8769" >&2
    printf '%s\n' "$output" >&2
    return 1
  else status=$?; fi
  [[ "$status" -eq 1 ]] && return 0
  printf '%s\n' "$output" >&2
  return "$status"
}
wait_listener_absent() {
  local initial_check=1
  while [[ "$initial_check" -eq 1 || "$CLEANUP_POLL_BUDGET" -gt 0 ]]; do
    if [[ "$CLEANUP_POLL_BUDGET" -gt 0 ]]; then
      CLEANUP_POLL_BUDGET=$((CLEANUP_POLL_BUDGET - 1))
    fi
    initial_check=0
    if listener_absent; then return 0; fi
    [[ "$CLEANUP_POLL_BUDGET" -gt 0 ]] && sleep 1
  done
  return 1
}
"$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>/dev/null || true
wait_agent_absent
wait_listener_absent
if [[ -f "$PLIST_PATH" ]]; then rm "$PLIST_PATH"; echo "removed launchd agent: $PLIST_PATH"; else echo "launchd agent not installed: $PLIST_PATH"; fi
