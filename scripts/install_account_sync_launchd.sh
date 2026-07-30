#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
WAIT_SECONDS="${ACCOUNT_SYNC_LAUNCHD_WAIT_SECONDS:-30}"

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

REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT}"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" 2>/dev/null && pwd || printf '%s' "$RUNTIME_ROOT")"
TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.account-sync-controller.plist.template"
LABEL="com.open-trader.account-sync-controller"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
REPORTS_DIR="$RUNTIME_ROOT/reports"
PORTFOLIO="$DATA_DIR/latest/portfolio.csv"
DAILY_CONFIG="$RUNTIME_ROOT/config/daily_premarket.env"
TIGER_CONFIG_DIR="${OPEN_TRADER_TIGER_CONFIG_DIR:-$HOME/.tigeropen}"
OUT_LOG="$REPO_ROOT/logs/account_sync/launchd.out.log"
ERR_LOG="$REPO_ROOT/logs/account_sync/launchd.err.log"
STATUS_PATH="$DATA_DIR/account_sync/controller_status.json"

[[ -f "$TEMPLATE" ]] || { echo "missing launchd template: $TEMPLATE" >&2; exit 1; }

sed_escape() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_plist() {
  sed \
    -e "s|OPEN_TRADER_PYTHON|$(sed_escape "$PYTHON_BIN")|g" \
    -e "s|OPEN_TRADER_PORTFOLIO|$(sed_escape "$PORTFOLIO")|g" \
    -e "s|OPEN_TRADER_DATA_DIR|$(sed_escape "$DATA_DIR")|g" \
    -e "s|OPEN_TRADER_REPORTS_DIR|$(sed_escape "$REPORTS_DIR")|g" \
    -e "s|OPEN_TRADER_DAILY_CONFIG|$(sed_escape "$DAILY_CONFIG")|g" \
    -e "s|OPEN_TRADER_TIGER_CONFIG_DIR|$(sed_escape "$TIGER_CONFIG_DIR")|g" \
    -e "s|OPEN_TRADER_REPO|$(sed_escape "$REPO_ROOT")|g" \
    "$TEMPLATE"
}

lint_plist() {
  local temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-account-sync.XXXXXX.plist")"
  printf '%s\n' "$1" > "$temp"
  plutil -lint "$temp" >/dev/null
  rm -f "$temp"
}

bootstrap_agent() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$PLIST_PATH"; then
      return 0
    fi
    [[ "$attempt" -lt 5 ]] || return 1
    sleep 1
  done
}

wait_ready() {
  local expected_sha attempt
  expected_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    if "$PYTHON_BIN" - "$STATUS_PATH" "$REPO_ROOT" "$expected_sha" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    heartbeat = datetime.fromisoformat(status["heartbeat_at"])
    assert heartbeat.tzinfo is not None
    assert (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds() <= 120
    assert status["schema_version"] == "open_trader.account_sync.controller.v1"
    assert isinstance(status["pid"], int) and status["pid"] > 0
    assert status["working_directory"] == sys.argv[2]
    assert status["git_sha"] == sys.argv[3]
except (AssertionError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 1
  done
  echo "account sync controller did not publish a matching fresh status" >&2
  return 1
}

rendered="$(render_plist)"
lint_plist "$rendered"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/account_sync" "$DATA_DIR" "$REPORTS_DIR"
printf '%s\n' "$rendered" > "$PLIST_PATH"
"$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>/dev/null || true
: > "$OUT_LOG"
: > "$ERR_LOG"
bootstrap_agent
"$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$LABEL"
wait_ready
echo "installed launchd agent: $LABEL"
