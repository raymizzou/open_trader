#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
WAIT_SECONDS="${DASHBOARD_LAUNCHD_WAIT_SECONDS:-30}"

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
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" && pwd)"
TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.dashboard.plist.template"
LABEL="com.open-trader.dashboard"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
REPORTS_DIR="$RUNTIME_ROOT/reports"
PORTFOLIO="$DATA_DIR/latest/portfolio.csv"
DAILY_CONFIG="$RUNTIME_ROOT/config/daily_premarket.env"
PREDICTION_CONFIG="$REPO_ROOT/config/prediction_arbitrage.json"

[[ -f "$TEMPLATE" ]] || { echo "missing launchd template: $TEMPLATE" >&2; exit 1; }

sed_escape() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_plist() {
  local repo python data reports portfolio daily_config prediction
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
    "$TEMPLATE"
}

lint_plist() {
  local rendered="$1" temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-dashboard.XXXXXX.plist")"
  printf '%s\n' "$rendered" > "$temp"
  plutil -lint "$temp" >/dev/null
  rm -f "$temp"
}

listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  lsof -nP -tiTCP:8766 -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

ensure_port_safe() {
  local pid cwd command
  pid="$(listener_pid)"
  [[ -z "$pid" ]] && return 0
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true)"
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$cwd" != "$REPO_ROOT" || "$command" != *"open_trader"*"dashboard"* ]]; then
    echo "port 8766 is occupied by an unknown process (pid $pid); refusing to stop it" >&2
    return 1
  fi
}

wait_ready() {
  local attempt
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8766/ >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "dashboard did not become ready at http://127.0.0.1:8766/" >&2
  return 1
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

rendered="$(render_plist)"
lint_plist "$rendered"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

ensure_port_safe
mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/dashboard" "$DATA_DIR" "$REPORTS_DIR"
printf '%s\n' "$rendered" > "$PLIST_PATH"

"$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>/dev/null || true
bootstrap_agent
"$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$LABEL"
wait_ready
echo "installed launchd agent: $LABEL"
echo "review URL: http://127.0.0.1:8766/"
