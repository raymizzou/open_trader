#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
MODE="stack"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
PLUTIL_BIN="${PLUTIL_BIN:-/usr/bin/plutil}"
LSOF_BIN="${LSOF_BIN:-$(command -v lsof || true)}"
CURL_BIN="${CURL_BIN:-$(command -v curl || true)}"
WAIT_SECONDS="${DASHBOARD_LAUNCHD_WAIT_SECONDS:-30}"

usage() {
  echo "usage: $0 [--dry-run] [--mode stack|single] [--repo-root PATH] [--runtime-root PATH] [--python PATH] [--launch-agents-dir PATH] [--wait-seconds N]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --mode) [[ $# -ge 2 ]] || { usage; exit 2; }; MODE="$2"; shift 2 ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$MODE" == "stack" || "$MODE" == "single" ]] || { usage; exit 2; }

REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT}"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" && pwd)"
SINGLE_LABEL="com.open-trader.dashboard"
GATEWAY_LABEL="com.open-trader.frontend-gateway"
LEGACY_LABEL="com.open-trader.legacy-dashboard"
SINGLE_TEMPLATE="$REPO_ROOT/ops/launchd/$SINGLE_LABEL.plist.template"
GATEWAY_TEMPLATE="$REPO_ROOT/ops/launchd/$GATEWAY_LABEL.plist.template"
LEGACY_TEMPLATE="$REPO_ROOT/ops/launchd/$LEGACY_LABEL.plist.template"
SINGLE_PLIST="$LAUNCH_AGENTS_DIR/$SINGLE_LABEL.plist"
GATEWAY_PLIST="$LAUNCH_AGENTS_DIR/$GATEWAY_LABEL.plist"
LEGACY_PLIST="$LAUNCH_AGENTS_DIR/$LEGACY_LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
REPORTS_DIR="$RUNTIME_ROOT/reports"
PORTFOLIO="$DATA_DIR/latest/portfolio.csv"
DAILY_CONFIG="$RUNTIME_ROOT/config/daily_premarket.env"
PREDICTION_CONFIG="$RUNTIME_ROOT/config/prediction_arbitrage.json"
OUT_LOG="$REPO_ROOT/logs/dashboard/launchd.out.log"
ERR_LOG="$REPO_ROOT/logs/dashboard/launchd.err.log"
GATEWAY_OUT_LOG="$REPO_ROOT/logs/frontend_gateway/launchd.out.log"
GATEWAY_ERR_LOG="$REPO_ROOT/logs/frontend_gateway/launchd.err.log"
LEGACY_OUT_LOG="$REPO_ROOT/logs/legacy_dashboard/launchd.out.log"
LEGACY_ERR_LOG="$REPO_ROOT/logs/legacy_dashboard/launchd.err.log"

if [[ "$MODE" == "single" ]]; then
  [[ -f "$SINGLE_TEMPLATE" ]] || { echo "missing launchd template: $SINGLE_TEMPLATE" >&2; exit 1; }
else
  [[ -f "$GATEWAY_TEMPLATE" ]] || { echo "missing launchd template: $GATEWAY_TEMPLATE" >&2; exit 1; }
  [[ -f "$LEGACY_TEMPLATE" ]] || { echo "missing launchd template: $LEGACY_TEMPLATE" >&2; exit 1; }
fi

sed_escape() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_template() {
  local template="$1" repo python data reports portfolio daily_config prediction
  repo="$(sed_escape "$REPO_ROOT")"
  python="$(sed_escape "$PYTHON_BIN")"
  data="$(sed_escape "$DATA_DIR")"
  reports="$(sed_escape "$REPORTS_DIR")"
  portfolio="$(sed_escape "$PORTFOLIO")"
  daily_config="$(sed_escape "$DAILY_CONFIG")"
  prediction="$(sed_escape "$PREDICTION_CONFIG")"
  sed \
    -e "s|OPEN_TRADER_PYTHON|$python|g" \
    -e "s|OPEN_TRADER_PORTFOLIO|$portfolio|g" \
    -e "s|OPEN_TRADER_DATA_DIR|$data|g" \
    -e "s|OPEN_TRADER_REPORTS_DIR|$reports|g" \
    -e "s|OPEN_TRADER_DAILY_CONFIG|$daily_config|g" \
    -e "s|OPEN_TRADER_PREDICTION_CONFIG|$prediction|g" \
    -e "s|OPEN_TRADER_REPO|$repo|g" \
    "$template"
}

lint_plist() {
  local rendered="$1" temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-dashboard.XXXXXX.plist")"
  printf '%s\n' "$rendered" > "$temp"
  "$PLUTIL_BIN" -lint "$temp" >/dev/null
  rm -f "$temp"
}

job_pid() {
  "$LAUNCHCTL_BIN" print "gui/$UID/$1" 2>/dev/null |
    awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }' || true
}

ensure_port_owned() {
  local port="$1" listener label known
  shift
  [[ -x "$LSOF_BIN" ]] || { echo "lsof is unavailable: $LSOF_BIN" >&2; return 1; }
  for listener in $([[ -n "$LSOF_BIN" ]] && "$LSOF_BIN" -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    known=0
    for label in "$@"; do
      [[ "$listener" == "$(job_pid "$label")" ]] && known=1
    done
    [[ "$known" -eq 1 ]] || {
      echo "port $port is occupied by an unknown process (pid $listener); refusing to modify launchd jobs" >&2
      return 1
    }
  done
}

health_matches() {
  printf '%s' "$1" | "$PYTHON_BIN" -c '
import json
import sys

module = sys.argv[1]
payload = json.load(sys.stdin)
valid = payload.get("module") == module
if module == "frontend_gateway":
    valid = valid and payload.get("legacy_upstream_status") == "ok"
    valid = valid and payload.get("account_upstream_status") == "ok"
raise SystemExit(0 if valid else 1)
' "$2"
}

health_failure() {
  printf '%s' "$1" | "$PYTHON_BIN" -c '
import json
import sys

payload = json.load(sys.stdin)
for component in ("legacy", "account"):
    status = payload.get(f"{component}_upstream_status")
    if status != "ok":
        print(f"{component} upstream is {status or 'unavailable'}")
        break
'
}

wait_health() {
  local url="$1" module="$2" attempt payload
  [[ -x "$CURL_BIN" ]] || { echo "curl is unavailable: $CURL_BIN" >&2; return 1; }
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    payload="$("$CURL_BIN" --fail --silent --show-error --max-time 2 "$url" 2>/dev/null || true)"
    if [[ -n "$payload" ]] && health_matches "$payload" "$module"; then
      return 0
    fi
    sleep 1
  done
  if [[ "$module" == "frontend_gateway" && -n "$payload" ]]; then
    health_failure "$payload" >&2
  fi
  echo "$module did not become ready at $url" >&2
  return 1
}

wait_http() {
  local url="$1" attempt
  [[ -x "$CURL_BIN" ]] || { echo "curl is unavailable: $CURL_BIN" >&2; return 1; }
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    "$CURL_BIN" --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "HTTP readiness failed at $url" >&2
  return 1
}

bootout_agent() {
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$1" 2>/dev/null || true
  wait_agent_absent "$1"
}

wait_agent_absent() {
  local label="$1" attempt output status
  for attempt in 1 2 3 4 5; do
    if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$label" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then
      return 0
    fi
    if [[ "$status" -ne 0 ]]; then
      echo "failed to inspect launchd label: $label" >&2
      printf '%s\n' "$output" >&2
      return 1
    fi
    [[ "$attempt" -lt 5 ]] && sleep 1
  done
  echo "launchd job is still loaded: $label" >&2
  return 1
}

bootstrap_agent() {
  local plist="$1" attempt
  for attempt in 1 2 3 4 5; do
    "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$plist" && return 0
    [[ "$attempt" -lt 5 ]] || return 1
    sleep 1
  done
}

start_agent() {
  local label="$1" plist="$2"
  bootout_agent "$label" || return 1
  bootstrap_agent "$plist"
}

install_single() {
  single_rendered="$(render_template "$SINGLE_TEMPLATE")"
  lint_plist "$single_rendered"
  ensure_port_owned 8766 "$SINGLE_LABEL" "$GATEWAY_LABEL"
  ensure_port_owned 8767 "$LEGACY_LABEL"
  mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/dashboard" "$DATA_DIR" "$REPORTS_DIR"
  printf '%s\n' "$single_rendered" > "$SINGLE_PLIST"
  bootout_agent "$GATEWAY_LABEL"
  bootout_agent "$LEGACY_LABEL"
  : > "$OUT_LOG"
  : > "$ERR_LOG"
  start_agent "$SINGLE_LABEL" "$SINGLE_PLIST"
  wait_health "http://127.0.0.1:8766/healthz" "legacy_dashboard"
  wait_http "http://127.0.0.1:8766/"
  echo "installed launchd agent: $SINGLE_LABEL"
  echo "review URL: http://127.0.0.1:8766/"
}

install_stack() {
  local gateway_rendered legacy_rendered
  [[ -f "$SINGLE_PLIST" ]] || {
    echo "missing rollback plist: $SINGLE_PLIST; run --mode single first" >&2
    return 1
  }
  "$PLUTIL_BIN" -lint "$SINGLE_PLIST" >/dev/null
  ensure_port_owned 8766 "$SINGLE_LABEL" "$GATEWAY_LABEL"
  ensure_port_owned 8767 "$LEGACY_LABEL"
  wait_http "http://127.0.0.1:8766/"
  gateway_rendered="$(render_template "$GATEWAY_TEMPLATE")"
  legacy_rendered="$(render_template "$LEGACY_TEMPLATE")"
  lint_plist "$gateway_rendered"
  lint_plist "$legacy_rendered"
  mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/frontend_gateway" \
    "$REPO_ROOT/logs/legacy_dashboard" "$DATA_DIR" "$REPORTS_DIR"
  printf '%s\n' "$gateway_rendered" > "$GATEWAY_PLIST"
  printf '%s\n' "$legacy_rendered" > "$LEGACY_PLIST"
  : > "$GATEWAY_OUT_LOG"
  : > "$GATEWAY_ERR_LOG"
  : > "$LEGACY_OUT_LOG"
  : > "$LEGACY_ERR_LOG"

  if ! start_agent "$LEGACY_LABEL" "$LEGACY_PLIST" || \
    ! wait_health "http://127.0.0.1:8767/healthz" "legacy_dashboard"; then
    echo "legacy dashboard failed readiness" >&2
    return 1
  fi

  bootout_agent "$SINGLE_LABEL"
  bootout_agent "$GATEWAY_LABEL"
  if ! start_agent "$GATEWAY_LABEL" "$GATEWAY_PLIST" || \
    ! wait_health "http://127.0.0.1:8766/healthz" "frontend_gateway" || \
    ! wait_http "http://127.0.0.1:8766/"; then
    echo "frontend gateway failed readiness" >&2
    return 1
  fi
  echo "installed launchd stack: $GATEWAY_LABEL + $LEGACY_LABEL"
  echo "review URL: http://127.0.0.1:8766/"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$MODE" == "stack" ]]; then
    gateway_rendered="$(render_template "$GATEWAY_TEMPLATE")"
    legacy_rendered="$(render_template "$LEGACY_TEMPLATE")"
    lint_plist "$gateway_rendered"
    lint_plist "$legacy_rendered"
    printf '===== %s =====\n%s\n' "$GATEWAY_LABEL" "$gateway_rendered"
    printf '===== %s =====\n%s\n' "$LEGACY_LABEL" "$legacy_rendered"
  else
    single_rendered="$(render_template "$SINGLE_TEMPLATE")"
    lint_plist "$single_rendered"
    printf '%s\n' "$single_rendered"
  fi
  exit 0
fi

if [[ "$MODE" == "single" ]]; then
  install_single
else
  install_stack
fi
