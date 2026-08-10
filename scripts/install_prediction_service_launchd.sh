#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
CONFIG=""
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
WAIT_SECONDS="${PREDICTION_SERVICE_LAUNCHD_WAIT_SECONDS:-90}"
LABEL="com.open-trader.prediction-service"

usage() { echo "usage: $0 --runtime-root PATH [--dry-run] [--repo-root PATH] [--python PATH] [--config PATH] [--launch-agents-dir PATH] [--wait-seconds N]" >&2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --config) [[ $# -ge 2 ]] || { usage; exit 2; }; CONFIG="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -n "$RUNTIME_ROOT" && "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" 2>/dev/null && pwd || printf '%s' "$RUNTIME_ROOT")"
CONFIG="${CONFIG:-$RUNTIME_ROOT/config/prediction_arbitrage.json}"
TEMPLATE="$REPO_ROOT/ops/launchd/$LABEL.plist.template"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
LOG_DIR="$RUNTIME_ROOT/logs/prediction_service"
OUT_LOG="$LOG_DIR/launchd.out.log"
ERR_LOG="$LOG_DIR/launchd.err.log"
[[ -f "$TEMPLATE" ]] || { echo "missing launchd template: $TEMPLATE" >&2; exit 1; }

sed_escape() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
render_plist() {
  sed -e "s|OPEN_TRADER_PYTHON|$(sed_escape "$PYTHON_BIN")|g" -e "s|OPEN_TRADER_DATA_DIR|$(sed_escape "$DATA_DIR")|g" -e "s|OPEN_TRADER_PREDICTION_CONFIG|$(sed_escape "$CONFIG")|g" -e "s|OPEN_TRADER_RUNTIME_ROOT|$(sed_escape "$RUNTIME_ROOT")|g" -e "s|OPEN_TRADER_REPO|$(sed_escape "$REPO_ROOT")|g" "$TEMPLATE"
}
lint_plist() {
  local temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-prediction-service.XXXXXX.plist")"
  printf '%s\n' "$1" > "$temp"
  plutil -lint "$temp" >/dev/null
  rm -f "$temp"
}
wait_agent_absent() {
  local attempt output status
  for attempt in 1 2 3 4 5; do
    if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then status=0; else status=$?; fi
    if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then return 0; fi
    if [[ "$status" -ne 0 ]]; then echo "failed to inspect launchd label: $LABEL" >&2; printf '%s\n' "$output" >&2; return 1; fi
    [[ "$attempt" -lt 5 ]] && sleep 1
  done
  echo "launchd job is still loaded: $LABEL" >&2
  return 1
}
bootout_if_loaded() {
  local output status
  if output="$("$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>&1)"; then return 0; else status=$?; fi
  if [[ "$output" == *"Could not find service"* || "$output" == *"No such process"* ]]; then return 0; fi
  printf '%s\n' "$output" >&2
  return "$status"
}
health_matches() {
  "$PYTHON_BIN" -c '
import json, sys
expected_pid, expected_cwd, expected_sha, payload = sys.argv[1:]
try:
    health = json.loads(payload)
    valid = (health.get("schema_version") == "open_trader.prediction_service.health.v1" and health.get("module") == "prediction_service" and health.get("status") == "running" and health.get("mode") == "shadow" and health.get("production_owner") is False and health.get("mutations") == "prohibited" and health.get("pid") == int(expected_pid) and health.get("cwd") == expected_cwd and health.get("git_sha") == expected_sha)
except (TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
' "$1" "$2" "$3" "$4"
}
process_cwd_matches() {
  "$LSOF_BIN" -a -p "$1" -d cwd -Fn 2>/dev/null | awk -v expected="$REPO_ROOT" '$1 ~ /^n/ { found = 1; if (substr($1, 2) == expected) matched = 1 } END { exit !(found && matched) }'
}
loopback_listener_matches() {
  "$LSOF_BIN" -nP -a -p "$1" -iTCP:8769 -sTCP:LISTEN -Fn 2>/dev/null | awk '$1 ~ /^n/ { count += 1; if ($1 != "n127.0.0.1:8769") invalid = 1 } END { exit !(count == 1 && !invalid) }'
}
wait_ready() {
  local expected_sha attempt output pid health alive=0
  expected_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1 || true)"
    pid="$(printf '%s\n' "$output" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }')"
    [[ -n "$pid" ]] && alive=1
    if [[ -n "$pid" ]] && process_cwd_matches "$pid" && loopback_listener_matches "$pid" && health="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null)" && health_matches "$pid" "$REPO_ROOT" "$expected_sha" "$health"; then return 0; fi
    sleep 1
  done
  if [[ "$alive" -eq 1 ]]; then echo "Prediction Service installed; shadow health not confirmed within ${WAIT_SECONDS}s, job left running" >&2; return 0; fi
  bootout_if_loaded || true
  wait_agent_absent || return 1
  echo "Prediction Service did not start (no process bound to 8769)" >&2
  return 1
}

rendered="$(render_plist)"
lint_plist "$rendered"
if [[ "$DRY_RUN" -eq 1 ]]; then printf '%s\n' "$rendered"; exit 0; fi
mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$DATA_DIR"
printf '%s\n' "$rendered" > "$PLIST_PATH"
bootout_if_loaded
wait_agent_absent
: > "$OUT_LOG"
: > "$ERR_LOG"
"$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$PLIST_PATH"
wait_ready
echo "installed launchd agent: $LABEL"
