#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
WAIT_SECONDS="${ACCOUNT_API_LAUNCHD_WAIT_SECONDS:-30}"
LABEL="com.open-trader.account-api"

usage() {
  echo "usage: $0 [--dry-run] [--repo-root PATH] [--runtime-root PATH] [--python PATH] [--launch-agents-dir PATH] [--wait-seconds N]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT}"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" 2>/dev/null && pwd || printf '%s' "$RUNTIME_ROOT")"
TEMPLATE="$REPO_ROOT/ops/launchd/$LABEL.plist.template"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
OUT_LOG="$REPO_ROOT/logs/account_api/launchd.out.log"
ERR_LOG="$REPO_ROOT/logs/account_api/launchd.err.log"

[[ -f "$TEMPLATE" ]] || { echo "missing launchd template: $TEMPLATE" >&2; exit 1; }

sed_escape() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_plist() {
  sed \
    -e "s|OPEN_TRADER_PYTHON|$(sed_escape "$PYTHON_BIN")|g" \
    -e "s|OPEN_TRADER_DATA_DIR|$(sed_escape "$DATA_DIR")|g" \
    -e "s|OPEN_TRADER_REPO|$(sed_escape "$REPO_ROOT")|g" \
    "$TEMPLATE"
}

lint_plist() {
  local temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-account-api.XXXXXX.plist")"
  printf '%s\n' "$1" > "$temp"
  plutil -lint "$temp" >/dev/null
  rm -f "$temp"
}

wait_agent_absent() {
  local attempt output status
  for attempt in 1 2 3 4 5; do
    if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then
      return 0
    fi
    if [[ "$status" -ne 0 ]]; then
      echo "failed to inspect launchd label: $LABEL" >&2
      printf '%s\n' "$output" >&2
      return 1
    fi
    [[ "$attempt" -lt 5 ]] && sleep 1
  done
  echo "launchd job is still loaded: $LABEL" >&2
  return 1
}

bootout_if_loaded() {
  local output status
  if output="$("$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>&1)"; then
    return 0
  else
    status=$?
  fi
  if [[ "$output" == *"Could not find service"* || "$output" == *"No such process"* ]]; then
    return 0
  fi
  printf '%s\n' "$output" >&2
  return "$status"
}

health_matches() {
  "$PYTHON_BIN" -c '
import json
import sys

expected_pid, expected_sha, payload = sys.argv[1:]
try:
    health = json.loads(payload)
    valid = (
        health.get("schema_version") == "open_trader.account_api.health.v1"
        and health.get("module") == "account_api"
        and health.get("status") == "ok"
        and health.get("mode") == "shadow"
        and health.get("pid") == int(expected_pid)
        and health.get("api_git_sha") == expected_sha
        and health.get("worker_git_sha") == expected_sha
        and health.get("release_match") is True
    )
except (TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
' "$1" "$2" "$3"
}

wait_ready() {
  local expected_sha attempt output pid health
  expected_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1 || true)"
    pid="$(printf '%s\n' "$output" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }')"
    if [[ -n "$pid" ]] && health="$("$CURL_BIN" -fsS http://127.0.0.1:8768/healthz 2>/dev/null)" \
      && health_matches "$pid" "$expected_sha" "$health"; then
      return 0
    fi
    sleep 1
  done
  bootout_if_loaded || true
  wait_agent_absent || return 1
  echo "Account API did not publish matching shadow health" >&2
  return 1
}

rendered="$(render_plist)"
lint_plist "$rendered"
if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/account_api" "$DATA_DIR"
printf '%s\n' "$rendered" > "$PLIST_PATH"
bootout_if_loaded
wait_agent_absent
: > "$OUT_LOG"
: > "$ERR_LOG"
"$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$PLIST_PATH"
wait_ready
echo "installed launchd agent: $LABEL"
