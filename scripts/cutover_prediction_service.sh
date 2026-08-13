#!/usr/bin/env bash
set -euo pipefail

TARGET=""
REPO_ROOT=""
RUNTIME_ROOT=""
PYTHON_BIN=""
EXPECTED_SHA=""
PREDICTION_CONFIG=""
LAUNCH_AGENTS_DIR=""
WAIT_SECONDS=""
GIT_BIN="${GIT_BIN:-$(command -v git || true)}"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
PS_BIN="${PS_BIN:-/bin/ps}"
OWNER_PROBE_BIN="${OWNER_PROBE_BIN:-}"
GATEWAY_HEALTH=""
BEFORE_GATEWAY_HEALTH=""
BEFORE_LEGACY_HEALTH=""
BEFORE_SERVICE_HEALTH=""
AFTER_GATEWAY_HEALTH=""
AFTER_LEGACY_HEALTH=""
AFTER_SERVICE_HEALTH=""
BEFORE_OWNER_HOLDERS=""
AFTER_OWNER_HOLDERS=""
SERVICE_RUNTIME_PRESENT=0
SERVICE_RUNTIME_STATE=""
BEFORE_GATEWAY_PID=""
BEFORE_LEGACY_PID=""
BEFORE_SERVICE_PID=""
BEFORE_GATEWAY_LISTENER_PID=""
BEFORE_LEGACY_LISTENER_PID=""
BEFORE_SERVICE_LISTENER_PID=""
BEFORE_ROUTE_MODE=""
BEFORE_ACCOUNT_PID=""
BEFORE_ACCOUNT_LISTENER_PID=""
BEFORE_ACCOUNT_HEALTH=""
ACCOUNT_PID=""
ACCOUNT_LISTENER_PID=""
ACCOUNT_HEALTH_BEFORE=""
ACCOUNT_HEALTH_AFTER=""
ACCOUNT_CONTROLLER_PID=""
ACCOUNT_API_PID=""
ACCOUNT_CONTROLLER_SHA=""
ACCOUNT_API_SHA=""
BEFORE_ACCOUNT_CONTROLLER_PID=""
BEFORE_ACCOUNT_API_PID=""
BEFORE_ACCOUNT_CONTROLLER_SHA=""
BEFORE_ACCOUNT_API_SHA=""
BEFORE_ACCOUNT_API_LISTENER_PID=""
BEFORE_ACCOUNT_API_HEALTH=""
AFTER_ACCOUNT_CONTROLLER_PID=""
AFTER_ACCOUNT_API_PID=""
AFTER_ACCOUNT_CONTROLLER_SHA=""
AFTER_ACCOUNT_API_SHA=""
AFTER_ACCOUNT_API_LISTENER_PID=""
AFTER_ACCOUNT_API_HEALTH=""
DIRECT_STATE_VERIFIED=0
DIRECT_HISTORY_VERIFIED=0
DIRECT_PREVIEW_VERIFIED=0
PUBLIC_STATE_VERIFIED=0
PUBLIC_HISTORY_VERIFIED=0
PUBLIC_PREVIEW_VERIFIED=0

usage() {
  echo "usage: $0 --target service|legacy --repo-root PATH --runtime-root PATH --python PATH --expected-sha 40_HEX --prediction-config PATH --launch-agents-dir PATH --wait-seconds POSITIVE_INT" \
    >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) [[ $# -ge 2 ]] || { usage; exit 2; }; TARGET="$2"; shift 2 ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --expected-sha) [[ $# -ge 2 ]] || { usage; exit 2; }; EXPECTED_SHA="$2"; shift 2 ;;
    --prediction-config) [[ $# -ge 2 ]] || { usage; exit 2; }; PREDICTION_CONFIG="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$TARGET" == "service" || "$TARGET" == "legacy" ]] || { usage; exit 2; }
[[ -n "$REPO_ROOT" && -n "$PYTHON_BIN" \
  && -n "$EXPECTED_SHA" && -n "$PREDICTION_CONFIG" \
  && -n "$LAUNCH_AGENTS_DIR" && "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ \
  && "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{40}$ ]] || { usage; exit 2; }
RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT}"

fail() {
  echo "$*" >&2
  exit 1
}

require_executable() {
  [[ -n "$1" && -x "$1" ]] || fail "required executable is unavailable: $1"
}

COMMAND_TIMEOUT_SECONDS=$((WAIT_SECONDS + 5))
ACTIVE_RUNNER_PID=""
CAPTURED_OUTPUT=""
CAPTURE_TEMP_PATH=""
AUTH_TEMP_DIR=""
MAINTENANCE_TEMP_PATH=""
cleanup_capture_temp() {
  if [[ -n "$CAPTURE_TEMP_PATH" ]]; then
    rm -f -- "$CAPTURE_TEMP_PATH"
    CAPTURE_TEMP_PATH=""
  fi
}
cleanup_auth_temp() {
  if [[ -n "$AUTH_TEMP_DIR" ]]; then
    rm -rf -- "$AUTH_TEMP_DIR"
    AUTH_TEMP_DIR=""
  fi
}
cleanup_maintenance_temp() {
  if [[ -n "$MAINTENANCE_TEMP_PATH" ]]; then
    rm -f -- "$MAINTENANCE_TEMP_PATH"
    MAINTENANCE_TEMP_PATH=""
  fi
}
cleanup_runtime_temps() {
  cleanup_capture_temp
  cleanup_auth_temp
  cleanup_maintenance_temp
}
trap cleanup_runtime_temps EXIT
run_bounded() {
  "$PYTHON_BIN" -c '
import os, signal, subprocess, sys
timeout = int(sys.argv[1])
process = subprocess.Popen(sys.argv[2:], start_new_session=True)

def stop_child():
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()

def interrupted(signum, _frame):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    stop_child()
    raise SystemExit(128 + signum)

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
try:
    raise SystemExit(process.wait(timeout=timeout))
except subprocess.TimeoutExpired:
    stop_child()
    print(f"command timed out after {timeout}s: {sys.argv[2]}", file=sys.stderr)
    raise SystemExit(124)
' "$COMMAND_TIMEOUT_SECONDS" "$@" <&0 &
  ACTIVE_RUNNER_PID=$!
  local status
  if wait "$ACTIVE_RUNNER_PID"; then
    status=0
  else
    status=$?
  fi
  ACTIVE_RUNNER_PID=""
  return "$status"
}

capture_bounded() {
  local status
  cleanup_capture_temp
  CAPTURE_TEMP_PATH="$(mktemp "${TMPDIR:-/tmp}/open-trader-cutover-output.XXXXXX")" || return 1
  CAPTURED_OUTPUT=""
  if run_bounded "$@" >"$CAPTURE_TEMP_PATH"; then
    status=0
  else
    status=$?
  fi
  if [[ -f "$CAPTURE_TEMP_PATH" ]]; then
    CAPTURED_OUTPUT="$(<"$CAPTURE_TEMP_PATH")"
  fi
  cleanup_capture_temp
  return "$status"
}

REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)" || fail "repo root is unavailable"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" && pwd -P)" || fail "runtime root is unavailable"
PREDICTION_CONFIG_DIR="${PREDICTION_CONFIG%/*}"
[[ "$PREDICTION_CONFIG_DIR" != "$PREDICTION_CONFIG" ]] || PREDICTION_CONFIG_DIR="."
PREDICTION_CONFIG="$(cd "$PREDICTION_CONFIG_DIR" && pwd -P)/${PREDICTION_CONFIG##*/}"
LAUNCH_AGENTS_DIR="$(cd "$LAUNCH_AGENTS_DIR" && pwd -P)" || fail "launch agents directory is unavailable"

ROUTE_PATH="$RUNTIME_ROOT/config/prediction-route.json"
EVIDENCE_PATH="$RUNTIME_ROOT/prediction-cutover-evidence.json"
RUNTIME_RECORD="$RUNTIME_ROOT/prediction-service-runtime.json"
INSTALL_DASHBOARD="$REPO_ROOT/scripts/install_dashboard_launchd.sh"
INSTALL_SERVICE="$REPO_ROOT/scripts/install_prediction_service_launchd.sh"
UNINSTALL_SERVICE="$REPO_ROOT/scripts/uninstall_prediction_service_launchd.sh"

for executable in "$PYTHON_BIN" "$GIT_BIN" "$LAUNCHCTL_BIN" "$LSOF_BIN" \
  "$CURL_BIN" "$PS_BIN" "$INSTALL_DASHBOARD" "$INSTALL_SERVICE" "$UNINSTALL_SERVICE"; do
  require_executable "$executable"
done
[[ -z "$OWNER_PROBE_BIN" ]] || require_executable "$OWNER_PROBE_BIN"
[[ -f "$PREDICTION_CONFIG" ]] || fail "prediction config is unavailable"
[[ ! -L "$ROUTE_PATH" ]] || fail "prediction route path is invalid"
[[ ! -e "$EVIDENCE_PATH" || ( -f "$EVIDENCE_PATH" && ! -L "$EVIDENCE_PATH" ) ]] \
  || fail "prediction evidence path is invalid"
if [[ -e "$RUNTIME_RECORD" ]]; then
  # runtime record validation
  [[ -f "$RUNTIME_RECORD" && ! -L "$RUNTIME_RECORD" ]] \
    || fail "prediction runtime record path is invalid"
  capture_bounded "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY' \
    || fail "prediction runtime record inspection failed"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict) or payload.get("state") not in {"ready", "failed", "stopped"}:
    raise ValueError("prediction runtime record is not an object")
print(payload["state"])
PY
  SERVICE_RUNTIME_STATE="$CAPTURED_OUTPUT"
  SERVICE_RUNTIME_PRESENT=1
fi

ACTUAL_SHA="$(run_bounded "$GIT_BIN" -C "$REPO_ROOT" rev-parse HEAD)" \
  || fail "failed to inspect selected checkout SHA"
[[ "$ACTUAL_SHA" == "$EXPECTED_SHA" ]] || fail "selected checkout SHA does not match expected SHA"
SOURCE_STATUS="$(run_bounded "$GIT_BIN" -C "$REPO_ROOT" status --porcelain)" \
  || fail "failed to inspect selected checkout status"
[[ -z "$SOURCE_STATUS" ]] || fail "selected checkout is dirty"

read_route() {
  run_bounded "$PYTHON_BIN" - "$ROUTE_PATH" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
valid = (
    isinstance(payload, dict)
    and set(payload) == {"schema_version", "mode", "operation_id", "updated_at"}
    and payload.get("schema_version") == "open_trader.frontend_gateway.prediction_route.v1"
    and payload.get("mode") in {"legacy", "maintenance", "service"}
    and isinstance(payload.get("operation_id"), str) and bool(payload["operation_id"])
    and isinstance(payload.get("updated_at"), str) and bool(payload["updated_at"])
)
if not valid:
    raise ValueError("invalid prediction route record")
print(json.dumps(payload, separators=(",", ":")))
PY
}

INITIAL_ROUTE=""
INITIAL_MODE="absent"
INITIAL_OPERATION_ID="__absent__"
if [[ -e "$ROUTE_PATH" ]]; then
  [[ -f "$ROUTE_PATH" ]] || fail "prediction route path is invalid"
  INITIAL_ROUTE="$(read_route)" || fail "prediction route record is invalid"
  INITIAL_MODE="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["mode"])' "$INITIAL_ROUTE")"
  INITIAL_OPERATION_ID="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["operation_id"])' "$INITIAL_ROUTE")"
fi

INITIAL_EVIDENCE_OPERATION_ID="__absent__"

inspect_label() {
  local label="$1" required="$2" output status expected_plist
  if output="$(run_bounded "$LAUNCHCTL_BIN" print "gui/$UID/$label" 2>&1)"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then
    [[ "$required" == "0" ]] || return 1
    return 0
  fi
  [[ "$status" -eq 0 ]] || { printf '%s\n' "$output" >&2; return 1; }
  expected_plist="$LAUNCH_AGENTS_DIR/$label.plist"
  run_bounded "$PYTHON_BIN" - "$output" "$expected_plist" "$REPO_ROOT" <<'PY'
import re, sys
text, expected_plist, expected_cwd = sys.argv[1:]
path = re.search(r"(?m)^\s*path = (.+?)\s*$", text)
cwd = re.search(r"(?m)^\s*working directory = (.+?)\s*$", text)
pid = re.search(r"(?m)^\s*pid = ([1-9][0-9]*)\s*$", text)
if not path or not cwd or not pid or path.group(1) != expected_plist or cwd.group(1) != expected_cwd:
    raise SystemExit(1)
print(pid.group(1))
PY
}

listener_pid() {
  local port="$1" output status
  if capture_bounded "$LSOF_BIN" -nP -iTCP:"$port" -sTCP:LISTEN -Fn; then
    status=0
  else
    status=$?
  fi
  output="$CAPTURED_OUTPUT"
  if [[ "$status" -eq 1 && -z "$output" ]]; then
    return 0
  fi
  [[ "$status" -eq 0 ]] || { printf '%s\n' "$output" >&2; return 1; }
  run_bounded "$PYTHON_BIN" - "$output" "$port" <<'PY'
import re, sys
lines = sys.argv[1].splitlines()
pids = [line[1:] for line in lines if re.fullmatch(r"p[1-9][0-9]*", line)]
addresses = [line[1:] for line in lines if line.startswith("n")]
valid = len(pids) == 1 and addresses == [f"127.0.0.1:{sys.argv[2]}"]
if not valid:
    raise SystemExit(1)
print(pids[0])
PY
}

listener_absent() {
  local port="$1" status
  if capture_bounded "$LSOF_BIN" -nP -iTCP:"$port" -sTCP:LISTEN -Fn; then
    return 1
  else
    status=$?
  fi
  [[ "$status" -eq 1 && -z "$CAPTURED_OUTPUT" ]]
}

inspect_account_identity() {
  inspect_label com.open-trader.account-sync-controller 1
}

inspect_account_api_identity() {
  inspect_label com.open-trader.account-api 1
}

account_repo_sha() {
  run_bounded "$GIT_BIN" -C "$REPO_ROOT" rev-parse HEAD
}

account_health_snapshot() {
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8768/healthz || return 1
  printf '%s\n' "$CAPTURED_OUTPUT"
}

validate_account_health() {
  run_bounded "$PYTHON_BIN" - "$1" "$2" "$EXPECTED_SHA" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = (
        isinstance(payload, dict)
        and payload.get("schema_version") == "open_trader.account_api.health.v1"
        and payload.get("module") == "account_api"
        and payload.get("status") == "ok"
        and type(payload.get("pid")) is int
        and payload["pid"] == int(sys.argv[2])
        and payload.get("api_git_sha") == sys.argv[3]
        and payload.get("worker_git_sha") == sys.argv[3]
        and payload.get("release_match") is True
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

account_state_unchanged() {
  local controller_pid api_pid api_listener api_health controller_sha api_sha
  controller_pid="$(inspect_account_identity)" || return 1
  api_pid="$(inspect_account_api_identity)" || return 1
  api_listener="$(listener_pid 8768)" || return 1
  [[ "$controller_pid" == "$ACCOUNT_CONTROLLER_PID" \
    && "$api_pid" == "$ACCOUNT_API_PID" \
    && "$api_listener" == "$ACCOUNT_API_PID" ]] || return 1
  controller_sha="$(account_repo_sha)" || return 1
  [[ "$controller_sha" == "$ACCOUNT_CONTROLLER_SHA" ]] || return 1
  api_health="$(account_health_snapshot)" || return 1
  validate_account_health "$api_health" "$api_pid" || return 1
  api_sha="$(run_bounded "$PYTHON_BIN" -c \
    'import json,sys; print(json.loads(sys.argv[1])["api_git_sha"])' "$api_health")" || return 1
  [[ "$api_sha" == "$ACCOUNT_API_SHA" ]] || return 1
  account_health_unchanged "$ACCOUNT_HEALTH_BEFORE" "$api_health"
}

preflight_health() {
  local gateway legacy
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz || return 1
  gateway="$CAPTURED_OUTPUT"
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz || return 1
  legacy="$CAPTURED_OUTPUT"
  run_bounded "$PYTHON_BIN" - "$gateway" "$legacy" "$GATEWAY_PID" "$LEGACY_PID" \
    "$REPO_ROOT" "$EXPECTED_SHA" "$INITIAL_ROUTE" <<'PY'
import json, sys
try:
    gateway, legacy, route = json.loads(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[7])
    expected_gateway_pid, expected_legacy_pid = int(sys.argv[3]), int(sys.argv[4])
    expected_prediction_status = {
        "service": "ok", "legacy": "legacy", "maintenance": "maintenance",
    }[route["mode"]]
    valid = (
        isinstance(gateway, dict)
        and gateway.get("schema_version") == "open_trader.frontend_gateway.health.v1"
        and gateway.get("module") == "frontend_gateway"
        and type(gateway.get("pid")) is int and gateway["pid"] == expected_gateway_pid
        and gateway.get("cwd") == sys.argv[5]
        and gateway.get("git_sha") == sys.argv[6]
        and gateway.get("source_state") == "clean"
        and gateway.get("legacy_upstream_status") == "ok"
        and gateway.get("account_upstream_status") == "ok"
        and gateway.get("prediction_route_mode") == route["mode"]
        and type(gateway.get("prediction_inflight_requests")) is int
        and gateway["prediction_inflight_requests"] >= 0
        and gateway.get("prediction_upstream_status") == expected_prediction_status
        and isinstance(legacy, dict)
        and legacy.get("schema_version") == "open_trader.legacy_dashboard.health.v1"
        and legacy.get("module") == "legacy_dashboard"
        and type(legacy.get("pid")) is int and legacy["pid"] == expected_legacy_pid
        and legacy.get("cwd") == sys.argv[5]
        and legacy.get("git_sha") == sys.argv[6]
        and legacy.get("source_state") == "clean"
    )
except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_observed_health() {
  run_bounded "$PYTHON_BIN" - "$1" "$2" "$3" "$REPO_ROOT" "$EXPECTED_SHA" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    expected_module, expected_pid = sys.argv[2], int(sys.argv[3])
    valid = (
        isinstance(payload, dict)
        and payload.get("module") == expected_module
        and type(payload.get("pid")) is int
        and payload["pid"] == expected_pid
        and payload.get("cwd") == sys.argv[4]
        and payload.get("git_sha") == sys.argv[5]
    )
    if expected_module == "frontend_gateway":
        valid = valid and payload.get("schema_version") == "open_trader.frontend_gateway.health.v1" \
            and payload.get("source_state") == "clean"
    elif expected_module == "legacy_dashboard":
        valid = valid and payload.get("schema_version") == "open_trader.legacy_dashboard.health.v1" \
            and payload.get("source_state") == "clean"
    else:
        valid = valid and payload.get("schema_version") == "open_trader.prediction_service.health.v1" \
            and payload.get("status") == "running" \
            and payload.get("mode") == "production"
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_pre_cutover_health() {
  run_bounded "$PYTHON_BIN" - "$1" "$2" "$3" "$REPO_ROOT" <<'PY'
import json, re, sys
try:
    payload = json.loads(sys.argv[1])
    expected_module, expected_pid = sys.argv[2], int(sys.argv[3])
    valid = (
        isinstance(payload, dict)
        and payload.get("module") == expected_module
        and type(payload.get("pid")) is int
        and payload["pid"] == expected_pid
        and payload.get("cwd") == sys.argv[4]
        and isinstance(payload.get("git_sha"), str)
        and re.fullmatch(r"[0-9a-fA-F]{40}", payload["git_sha"]) is not None
        and payload.get("source_state") == "clean"
    )
    if expected_module == "frontend_gateway":
        valid = valid and payload.get("schema_version") == "open_trader.frontend_gateway.health.v1"
    elif expected_module == "legacy_dashboard":
        valid = valid and payload.get("schema_version") == "open_trader.legacy_dashboard.health.v1"
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

capture_owner_holders() {
  local status
  if capture_bounded "$LSOF_BIN" -nP -F p -- \
      "$RUNTIME_ROOT/data/prediction_arbitrage/runtime.lock"; then
    return 0
  else
    status=$?
  fi
  [[ "$status" -eq 1 && -z "$CAPTURED_OUTPUT" ]]
}

capture_before_snapshot() {
  BEFORE_ROUTE_MODE="$INITIAL_MODE"
  BEFORE_GATEWAY_PID="$GATEWAY_PID"
  BEFORE_LEGACY_PID="$LEGACY_PID"
  BEFORE_SERVICE_PID="$SERVICE_PID"
  BEFORE_GATEWAY_LISTENER_PID="$GATEWAY_LISTENER_PID"
  BEFORE_LEGACY_LISTENER_PID="$LEGACY_LISTENER_PID"
  BEFORE_SERVICE_LISTENER_PID="$SERVICE_LISTENER_PID"
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz || return 1
  BEFORE_GATEWAY_HEALTH="$CAPTURED_OUTPUT"
  if [[ "$INITIAL_MODE" == "absent" ]]; then
    validate_pre_cutover_health "$BEFORE_GATEWAY_HEALTH" frontend_gateway "$GATEWAY_PID" \
      || return 1
  else
    validate_observed_health "$BEFORE_GATEWAY_HEALTH" frontend_gateway "$GATEWAY_PID" \
      || return 1
  fi
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz || return 1
  BEFORE_LEGACY_HEALTH="$CAPTURED_OUTPUT"
  if [[ "$INITIAL_MODE" == "absent" ]]; then
    validate_pre_cutover_health "$BEFORE_LEGACY_HEALTH" legacy_dashboard "$LEGACY_PID" \
      || return 1
  else
    validate_observed_health "$BEFORE_LEGACY_HEALTH" legacy_dashboard "$LEGACY_PID" \
      || return 1
  fi
  if [[ -n "$SERVICE_PID" ]]; then
    capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
      http://127.0.0.1:8769/healthz || return 1
    BEFORE_SERVICE_HEALTH="$CAPTURED_OUTPUT"
    validate_observed_health "$BEFORE_SERVICE_HEALTH" prediction_service "$SERVICE_PID" \
      || return 1
  else
    BEFORE_SERVICE_HEALTH=""
  fi
  capture_owner_holders || return 1
  BEFORE_OWNER_HOLDERS="$CAPTURED_OUTPUT"
  BEFORE_ACCOUNT_PID="$ACCOUNT_PID"
  BEFORE_ACCOUNT_LISTENER_PID="$ACCOUNT_LISTENER_PID"
  BEFORE_ACCOUNT_HEALTH="$ACCOUNT_HEALTH_BEFORE"
  BEFORE_ACCOUNT_CONTROLLER_PID="$ACCOUNT_CONTROLLER_PID"
  BEFORE_ACCOUNT_API_PID="$ACCOUNT_API_PID"
  BEFORE_ACCOUNT_CONTROLLER_SHA="$ACCOUNT_CONTROLLER_SHA"
  BEFORE_ACCOUNT_API_SHA="$ACCOUNT_API_SHA"
  BEFORE_ACCOUNT_API_LISTENER_PID="$ACCOUNT_LISTENER_PID"
  BEFORE_ACCOUNT_API_HEALTH="$ACCOUNT_HEALTH_BEFORE"
}

capture_after_account_snapshot() {
  AFTER_ACCOUNT_CONTROLLER_PID="$(inspect_account_identity)" || return 1
  AFTER_ACCOUNT_API_PID="$(inspect_account_api_identity)" || return 1
  AFTER_ACCOUNT_API_LISTENER_PID="$(listener_pid 8768)" || return 1
  [[ "$AFTER_ACCOUNT_API_LISTENER_PID" == "$AFTER_ACCOUNT_API_PID" ]] || return 1
  AFTER_ACCOUNT_CONTROLLER_SHA="$(account_repo_sha)" || return 1
  [[ "$AFTER_ACCOUNT_CONTROLLER_SHA" == "$EXPECTED_SHA" ]] || return 1
  AFTER_ACCOUNT_API_HEALTH="$(account_health_snapshot)" || return 1
  validate_account_health "$AFTER_ACCOUNT_API_HEALTH" "$AFTER_ACCOUNT_API_PID" || return 1
  AFTER_ACCOUNT_API_SHA="$(run_bounded "$PYTHON_BIN" -c \
    'import json,sys; print(json.loads(sys.argv[1])["api_git_sha"])' \
    "$AFTER_ACCOUNT_API_HEALTH")" || return 1
  [[ "$AFTER_ACCOUNT_API_SHA" == "$EXPECTED_SHA" ]] || return 1
}

capture_after_snapshot() {
  GATEWAY_PID="$(inspect_label com.open-trader.frontend-gateway 1)" || return 1
  GATEWAY_LISTENER_PID="$(listener_pid 8766)" || return 1
  [[ "$GATEWAY_LISTENER_PID" == "$GATEWAY_PID" ]] || return 1
  LEGACY_PID="$(inspect_label com.open-trader.legacy-dashboard 1)" || return 1
  LEGACY_LISTENER_PID="$(listener_pid 8767)" || return 1
  [[ "$LEGACY_LISTENER_PID" == "$LEGACY_PID" ]] || return 1
  SERVICE_PID="$(inspect_label com.open-trader.prediction-service 0)" || return 1
  if [[ -n "$SERVICE_PID" ]]; then
    SERVICE_LISTENER_PID="$(listener_pid 8769)" || return 1
    [[ "$SERVICE_LISTENER_PID" == "$SERVICE_PID" ]] || return 1
  else
    listener_absent 8769 || return 1
    SERVICE_LISTENER_PID=""
  fi
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz || return 1
  AFTER_GATEWAY_HEALTH="$CAPTURED_OUTPUT"
  validate_observed_health "$AFTER_GATEWAY_HEALTH" frontend_gateway "$GATEWAY_PID" \
    || return 1
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz || return 1
  AFTER_LEGACY_HEALTH="$CAPTURED_OUTPUT"
  validate_observed_health "$AFTER_LEGACY_HEALTH" legacy_dashboard "$LEGACY_PID" \
    || return 1
  if [[ -n "$SERVICE_PID" ]]; then
    capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
      http://127.0.0.1:8769/healthz || return 1
    AFTER_SERVICE_HEALTH="$CAPTURED_OUTPUT"
    validate_observed_health "$AFTER_SERVICE_HEALTH" prediction_service "$SERVICE_PID" \
      || return 1
  else
    AFTER_SERVICE_HEALTH=""
  fi
  capture_owner_holders || return 1
  AFTER_OWNER_HOLDERS="$CAPTURED_OUTPUT"
  capture_after_account_snapshot || return 1
}

verify_relevant_label_set() {
  local output expected_service_count
  output="$(run_bounded "$LAUNCHCTL_BIN" print "gui/$UID")" || return 1
  expected_service_count=0
  [[ "$INITIAL_MODE" == "service" ]] && expected_service_count=1
  [[ "${1:-}" == "rollback-maintenance" ]] && expected_service_count="rollback-maintenance"
  run_bounded "$PYTHON_BIN" - "$output" "$expected_service_count" <<'PY'
import re, sys
labels = re.findall(
    r'(?m)^\s*(?:[1-9][0-9]*|-)\s+(?:[0-9]+|-)\s+'
    r'(com\.open-trader\.[^\s]+)\s*$', sys.argv[1]
)
expected = {
    "com.open-trader.frontend-gateway": 1,
    "com.open-trader.legacy-dashboard": 1,
    "com.open-trader.prediction-service": 0 if sys.argv[2] == "rollback-maintenance" else int(sys.argv[2]),
}
relevant = [
    label for label in labels
    if any(term in label for term in ("prediction", "frontend-gateway", "legacy-dashboard"))
]
valid = all(relevant.count(label) == count for label, count in expected.items())
if sys.argv[2] == "rollback-maintenance":
    valid = (
        relevant.count("com.open-trader.frontend-gateway") == 1
        and relevant.count("com.open-trader.legacy-dashboard") == 1
        and relevant.count("com.open-trader.prediction-service") in {0, 1}
    )
valid = valid and all(label in expected for label in relevant)
raise SystemExit(0 if valid else 1)
PY
}

if [[ "$INITIAL_MODE" == "maintenance" && "$TARGET" != "legacy" ]]; then
  fail "another prediction cutover owns maintenance"
fi
[[ "$INITIAL_MODE" != "absent" || "$TARGET" == "service" ]] \
  || fail "prediction route record is required for legacy rollback"
if [[ "$INITIAL_MODE" == "maintenance" && "$TARGET" == "legacy" ]]; then
  verify_relevant_label_set rollback-maintenance || fail "relevant launchd label set is not verified"
else
  verify_relevant_label_set || fail "relevant launchd label set is not verified"
fi
GATEWAY_PID="$(inspect_label com.open-trader.frontend-gateway 1)" \
  || fail "Frontend Gateway launchd identity is not verified"
LEGACY_PID="$(inspect_label com.open-trader.legacy-dashboard 1)" \
  || fail "Legacy Dashboard launchd identity is not verified"
SERVICE_PID="$(inspect_label com.open-trader.prediction-service 0)" \
  || fail "Prediction Service launchd identity is not verified"
GATEWAY_LISTENER_PID="$(listener_pid 8766)" \
  || fail "Frontend Gateway listener inspection failed"
LEGACY_LISTENER_PID="$(listener_pid 8767)" \
  || fail "Legacy Dashboard listener inspection failed"
SERVICE_LISTENER_PID="$(listener_pid 8769)" \
  || fail "Prediction Service listener inspection failed"
ACCOUNT_CONTROLLER_PID="$(inspect_account_identity)" \
  || fail "Account sync controller launchd identity is not verified"
ACCOUNT_API_PID="$(inspect_account_api_identity)" \
  || fail "Account API launchd identity is not verified"
ACCOUNT_LISTENER_PID="$(listener_pid 8768)" \
  || fail "Account API listener inspection failed"
[[ "$ACCOUNT_LISTENER_PID" == "$ACCOUNT_API_PID" ]] \
  || fail "unknown listener owner on 8768"
ACCOUNT_CONTROLLER_SHA="$(account_repo_sha)" \
  || fail "Account sync controller SHA is not observable"
[[ "$ACCOUNT_CONTROLLER_SHA" == "$EXPECTED_SHA" ]] \
  || fail "Account sync controller SHA is not selected checkout"
ACCOUNT_HEALTH_BEFORE="$(account_health_snapshot)" \
  || fail "Account API health is not verified"
validate_account_health "$ACCOUNT_HEALTH_BEFORE" "$ACCOUNT_API_PID" \
  || fail "Account API health identity is not verified"
ACCOUNT_API_SHA="$(run_bounded "$PYTHON_BIN" -c \
  'import json,sys; print(json.loads(sys.argv[1])["api_git_sha"])' \
  "$ACCOUNT_HEALTH_BEFORE")" \
  || fail "Account API SHA is not observable"
[[ "$ACCOUNT_API_SHA" == "$EXPECTED_SHA" ]] \
  || fail "Account API SHA is not selected checkout"
ACCOUNT_PID="$ACCOUNT_API_PID"
[[ "$GATEWAY_LISTENER_PID" == "$GATEWAY_PID" ]] \
  || fail "unknown listener on 8766"
[[ "$LEGACY_LISTENER_PID" == "$LEGACY_PID" ]] \
  || fail "unknown listener on 8767"
if [[ -n "$SERVICE_PID" ]]; then
  [[ "$SERVICE_LISTENER_PID" == "$SERVICE_PID" ]] || fail "unknown listener on 8769"
else
  [[ -z "$SERVICE_LISTENER_PID" ]] || fail "unknown listener on 8769"
fi
if [[ "$INITIAL_MODE" == "service" ]]; then
  [[ -n "$SERVICE_PID" ]] || fail "service route has no verified Prediction Service"
elif [[ "$INITIAL_MODE" == "maintenance" && "$TARGET" == "legacy" ]]; then
  : # An interrupted Service cutover is the explicit rollback input.
else
  [[ -z "$SERVICE_PID" ]] || fail "non-service route has a loaded Prediction Service"
fi
if [[ "$INITIAL_MODE" != "absent" ]]; then
  preflight_health || fail "Gateway and Legacy runtime health is not verified"
fi
capture_before_snapshot || fail "runtime evidence before-snapshot is not verified"

evidence_is_ready_for_target() {
  [[ -f "$EVIDENCE_PATH" ]] || return 1
  state_transition evidence-validate "$EVIDENCE_PATH" "$TARGET" "$EXPECTED_SHA" \
    "$REPO_ROOT" "$INITIAL_ROUTE" "1"
}

LOCK_DIR="$RUNTIME_ROOT/config/.prediction-cutover.lock"
run_bounded "$PYTHON_BIN" -c \
  'from pathlib import Path; import sys; Path(sys.argv[1]).mkdir()' "$LOCK_DIR" 2>/dev/null \
  || fail "another prediction cutover is active"
OPERATION_LOCK_HELD=1
cleanup_lock() {
  cleanup_runtime_temps
  [[ "$OPERATION_LOCK_HELD" -eq 1 ]] || return 0
  run_bounded "$PYTHON_BIN" -c \
    'from pathlib import Path; import sys; Path(sys.argv[1]).rmdir()' "$LOCK_DIR" \
    2>/dev/null || true
  OPERATION_LOCK_HELD=0
}
stop_active_runner() {
  local runner_pid="$ACTIVE_RUNNER_PID"
  [[ "$runner_pid" =~ ^[1-9][0-9]*$ ]] || return 0
  kill -TERM "$runner_pid" 2>/dev/null || true
  wait "$runner_pid" 2>/dev/null || true
  ACTIVE_RUNNER_PID=""
}
handle_signal() {
  local status="$1"
  trap - INT TERM
  stop_active_runner
  if [[ -n "${OPERATION_ID:-}" && -n "${DOWNTIME_STARTED_AT:-}" ]] \
      && declare -F read_route >/dev/null 2>&1 \
      && declare -F state_transition >/dev/null 2>&1; then
    local current="" mode="" operation_id="" ended_at=""
    current="$(read_route 2>/dev/null || true)"
    if [[ -n "$current" ]]; then
      mode="$(run_bounded "$PYTHON_BIN" -c \
        'import json,sys; print(json.loads(sys.argv[1]).get("mode", ""))' \
        "$current" 2>/dev/null || true)"
      operation_id="$(run_bounded "$PYTHON_BIN" -c \
        'import json,sys; print(json.loads(sys.argv[1]).get("operation_id", ""))' \
        "$current" 2>/dev/null || true)"
      if [[ "$operation_id" == "$OPERATION_ID" && ( "$mode" == "service" || "$mode" == "legacy" || "$mode" == "maintenance" ) ]]; then
        if [[ "$mode" != "maintenance" ]]; then
          write_route maintenance "$OPERATION_ID" "$OPERATION_ID" 2>/dev/null || true
        fi
        if route_owned_maintenance 2>/dev/null; then
          ended_at="$(now 2>/dev/null || true)"
          write_evidence failed "interrupted" "$ended_at" 2>/dev/null || true
        fi
      fi
    fi
  fi
  cleanup_lock
  exit "$status"
}
trap cleanup_lock EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

OPERATION_ID="$(run_bounded "$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4().hex)')"
DOWNTIME_STARTED_AT=""
STATE_LOCK_PATH="$RUNTIME_ROOT/config/.prediction-cutover-state.lock"

state_transition() {
  run_bounded "$PYTHON_BIN" - "$@" <<'PY'
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def valid_route(payload):
    return (
        isinstance(payload, dict)
        and set(payload) == {"schema_version", "mode", "operation_id", "updated_at"}
        and payload.get("schema_version") == "open_trader.frontend_gateway.prediction_route.v1"
        and payload.get("mode") in {"legacy", "maintenance", "service"}
        and isinstance(payload.get("operation_id"), str) and bool(payload["operation_id"])
        and isinstance(payload.get("updated_at"), str) and bool(payload["updated_at"])
    )

def valid_evidence(payload, *, expected_target=None, expected_sha=None,
                   expected_cwd=None, route=None, require_ready=False):
    def valid_component(value):
        return (
            isinstance(value, dict)
            and set(value) == {"pid", "cwd", "git_sha", "listener"}
            and (value["pid"] is None or (type(value["pid"]) is int and value["pid"] > 0))
            and isinstance(value["cwd"], str)
            and isinstance(value["git_sha"], str)
            and (value["listener"] is None or isinstance(value["listener"], str))
        )
    def valid_snapshot(value):
        return (
            isinstance(value, dict)
            and set(value) == {"gateway", "legacy", "service"}
            and all(valid_component(value[name]) for name in ("gateway", "legacy", "service"))
        )
    def valid_account_component(value, api=False):
        keys = (
            {"pid", "cwd", "git_sha"}
            if not api else
            {"pid", "cwd", "git_sha", "listener", "health_status", "health_pid",
             "api_git_sha", "worker_git_sha", "release_match"}
        )
        if not isinstance(value, dict) or set(value) != keys:
            return False
        if value["pid"] is not None and (type(value["pid"]) is not int or value["pid"] <= 0):
            return False
        if not isinstance(value["cwd"], str) or not isinstance(value["git_sha"], str):
            return False
        if not api:
            return True
        return (
            value["listener"] is None or isinstance(value["listener"], str)
        ) and (
            value["health_pid"] is None
            or (type(value["health_pid"]) is int and value["health_pid"] > 0)
        ) and isinstance(value["health_status"], str) \
        and isinstance(value["api_git_sha"], str) \
        and isinstance(value["worker_git_sha"], str) \
        and type(value["release_match"]) is bool

    def valid_account_snapshot(value):
        return (
            isinstance(value, dict)
            and set(value) == {"controller", "api"}
            and valid_account_component(value["controller"])
            and valid_account_component(value["api"], api=True)
        )
    if not (
        isinstance(payload, dict)
        and set(payload) == {
            "schema_version", "operation_id", "target", "expected_sha", "result",
            "failure_reason", "downtime_started_at", "downtime_ended_at",
            "before", "after", "route", "owner", "service_runtime", "verification",
            "account",
        }
        and payload.get("schema_version") == "open_trader.prediction_cutover.evidence.v1"
        and isinstance(payload.get("operation_id"), str) and bool(payload["operation_id"])
        and payload.get("target") in {"service", "legacy"}
        and isinstance(payload.get("expected_sha"), str)
        and re.fullmatch(r"[0-9a-fA-F]{40}", payload["expected_sha"])
        and payload.get("result") in {"ready", "failed"}
        and isinstance(payload.get("failure_reason"), str)
        and isinstance(payload.get("downtime_started_at"), str)
        and isinstance(payload.get("downtime_ended_at"), str)
        and valid_snapshot(payload.get("before"))
        and valid_snapshot(payload.get("after"))
        and isinstance(payload.get("route"), dict)
        and set(payload["route"]) == {"before_mode", "after_mode", "inflight_before", "inflight_after"}
        and all(payload["route"].get(key) is None or (type(payload["route"].get(key)) is int and payload["route"][key] >= 0)
                for key in ("inflight_before", "inflight_after"))
        and all(payload["route"].get(key) in {"absent", "legacy", "maintenance", "service", "unknown"}
                for key in ("before_mode", "after_mode"))
        and isinstance(payload.get("owner"), dict)
        and set(payload["owner"]) == {"pid", "lock_holders", "before_lock_holders", "available"}
        and (payload["owner"]["pid"] is None or (type(payload["owner"]["pid"]) is int and payload["owner"]["pid"] > 0))
        and isinstance(payload["owner"]["lock_holders"], list)
        and isinstance(payload["owner"]["before_lock_holders"], list)
        and all(type(value) is int and value > 0 for value in payload["owner"]["lock_holders"] + payload["owner"]["before_lock_holders"])
        and type(payload["owner"]["available"]) is bool
        and isinstance(payload.get("service_runtime"), dict)
        and set(payload["service_runtime"]) == {"state", "reader_generation", "contract_generation"}
        and isinstance(payload["service_runtime"]["state"], str)
        and (payload["service_runtime"]["reader_generation"] is None or (type(payload["service_runtime"]["reader_generation"]) is int and payload["service_runtime"]["reader_generation"] > 0))
        and (payload["service_runtime"]["contract_generation"] is None or (type(payload["service_runtime"]["contract_generation"]) is int and payload["service_runtime"]["contract_generation"] > 0))
        and isinstance(payload.get("account"), dict)
        and isinstance(payload["account"].get("before"), dict)
        and isinstance(payload["account"].get("after"), dict)
        and valid_account_snapshot(payload["account"]["before"])
        and valid_account_snapshot(payload["account"]["after"])
        and isinstance(payload.get("verification"), dict)
        and set(payload["verification"]) == {
            "direct_backend", "direct_state", "direct_history", "direct_preview_no_submit",
            "public_state", "public_history", "public_preview_no_submit",
        }
        and payload["verification"]["direct_backend"] in {"verified", "failed"}
        and all(type(payload["verification"][key]) is bool for key in (
            "direct_state", "direct_history", "direct_preview_no_submit",
            "public_state", "public_history", "public_preview_no_submit",
        ))
    ):
        return False
    if payload["result"] == "ready":
        if not (not payload["failure_reason"] and payload["downtime_started_at"] and payload["downtime_ended_at"]):
            return False
        if payload["verification"]["direct_backend"] != "verified" or not all(
            payload["verification"][key] for key in (
                "direct_state", "direct_history", "direct_preview_no_submit",
                "public_state", "public_history", "public_preview_no_submit",
            )
        ):
            return False
    else:
        if not payload["failure_reason"] or payload["verification"]["direct_backend"] == "verified":
            return False
    if not require_ready:
        return True
    if payload["result"] != "ready" or expected_target not in {"service", "legacy"} \
            or not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha) \
            or not isinstance(expected_cwd, str) or not isinstance(route, dict):
        return False
    if payload["target"] != expected_target or payload["expected_sha"] != expected_sha \
            or payload["operation_id"] != route.get("operation_id") \
            or route.get("mode") != expected_target \
            or payload["route"]["after_mode"] != expected_target \
            or payload["route"]["inflight_after"] != 0:
        return False
    for name, port in (("gateway", 8766), ("legacy", 8767)):
        component = payload["after"][name]
        if component["pid"] is None or component["cwd"] != expected_cwd \
                or component["git_sha"] != expected_sha \
                or component["listener"] != f"127.0.0.1:{port}":
            return False
    selected = payload["after"][expected_target]
    if selected["pid"] is None or selected["cwd"] != expected_cwd \
            or selected["git_sha"] != expected_sha \
            or selected["listener"] != f"127.0.0.1:{8769 if expected_target == 'service' else 8767}":
        return False
    if expected_target == "service":
        runtime = payload["service_runtime"]
        if payload["after"]["service"]["pid"] is None or runtime["state"] != "ready" \
                or runtime["reader_generation"] is None or runtime["contract_generation"] is None:
            return False
    else:
        if payload["after"]["service"]["pid"] is not None \
            or payload["after"]["service"]["listener"] is not None:
            return False
    owner = payload["owner"]
    account_before = payload["account"]["before"]
    account_after = payload["account"]["after"]
    account_api = account_after["api"]
    account_controller = account_after["controller"]
    if account_before != account_after \
            or account_controller["pid"] is None \
            or account_controller["cwd"] != expected_cwd \
            or account_controller["git_sha"] != expected_sha \
            or account_api["pid"] is None \
            or account_api["cwd"] != expected_cwd \
            or account_api["git_sha"] != expected_sha \
            or account_api["listener"] != "127.0.0.1:8768" \
            or account_api["health_status"] != "ok" \
            or account_api["health_pid"] != account_api["pid"] \
            or account_api["api_git_sha"] != expected_sha \
            or account_api["worker_git_sha"] != expected_sha \
            or account_api["release_match"] is not True:
        return False
    return owner["pid"] == selected["pid"] and owner["lock_holders"] == [selected["pid"]] \
        and owner["available"] is False

def atomic_write(path, payload):
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        if read_json(path) != payload:
            raise ValueError("prediction state readback mismatch")
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

operation = sys.argv[1]
if operation == "route-bootstrap":
    _, path_raw, operation_id, lock_raw = sys.argv[1:]
    route_path = Path(path_raw)
    with open(lock_raw, "a+", encoding="utf-8") as state_lock:
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        if route_path.exists():
            raise ValueError("prediction route already exists")
        atomic_write(route_path, {
            "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
            "mode": "maintenance",
            "operation_id": operation_id,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
elif operation == "route-write":
    _, path_raw, mode, operation_id, expected_operation_id, lock_raw = sys.argv[1:]
    route_path = Path(path_raw)
    with open(lock_raw, "a+", encoding="utf-8") as state_lock:
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        current = read_json(route_path)
        if not valid_route(current):
            raise ValueError("invalid prediction route record")
        if expected_operation_id and current.get("operation_id") != expected_operation_id:
            raise ValueError("stale prediction cutover operation")
        if mode != "maintenance" and current.get("mode") != "maintenance":
            raise ValueError("prediction route is not in maintenance")
        if mode == "maintenance" and not expected_operation_id \
                and current.get("operation_id") != operation_id:
            raise ValueError("another prediction cutover owns maintenance")
        payload = {
            "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
            "mode": mode,
            "operation_id": operation_id,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        atomic_write(route_path, payload)
elif operation == "evidence-validate":
    _, path_raw, target_raw, sha_raw, cwd_raw, route_raw, ready_raw = sys.argv[1:]
    payload = read_json(Path(path_raw))
    route = json.loads(route_raw) if route_raw else None
    if not valid_evidence(
        payload,
        expected_target=target_raw or None,
        expected_sha=sha_raw or None,
        expected_cwd=cwd_raw or None,
        route=route,
        require_ready=ready_raw == "1",
    ):
        raise ValueError("invalid prediction evidence record")
elif operation == "evidence-write":
    (
        _, path_raw, route_raw, initial_evidence_operation_id, operation_id,
        target, expected_sha, result, reason, started, ended, lock_raw, cwd_raw, details_raw,
    ) = sys.argv[1:]
    path = Path(path_raw)
    route_path = Path(route_raw)
    with open(lock_raw, "a+", encoding="utf-8") as state_lock:
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        route = read_json(route_path)
        if not valid_route(route):
            raise ValueError("invalid prediction route record")
        expected_mode = target if result == "ready" else "maintenance"
        if route.get("operation_id") != operation_id or route.get("mode") != expected_mode:
            raise ValueError("prediction route is not owned by evidence writer")
        if path.exists():
            current_evidence = read_json(path)
            if not valid_evidence(current_evidence):
                raise ValueError("invalid prediction evidence record")
            if current_evidence.get("operation_id") not in {
                initial_evidence_operation_id, operation_id,
            }:
                raise ValueError("prediction evidence is owned by another operation")
        elif initial_evidence_operation_id != "__absent__":
            raise ValueError("prediction evidence disappeared during cutover")
        payload = {
            "schema_version": "open_trader.prediction_cutover.evidence.v1",
            "operation_id": operation_id,
            "target": target,
            "expected_sha": expected_sha,
            "result": result,
            "failure_reason": reason,
            "downtime_started_at": started,
            "downtime_ended_at": ended,
        }
        details = json.loads(details_raw)
        if not isinstance(details, dict):
            raise ValueError("invalid prediction evidence details")
        payload.update(details)
        if result != "ready":
            # A terminal failure must never preserve successful verification
            # claims from a pre-failure snapshot.  Keep the observations for
            # diagnosis, but make the verification result explicitly failed.
            payload["verification"] = {
                "direct_backend": "failed",
                "direct_state": False,
                "direct_history": False,
                "direct_preview_no_submit": False,
                "public_state": False,
                "public_history": False,
                "public_preview_no_submit": False,
            }
        if not valid_evidence(payload):
            raise ValueError("invalid prediction evidence payload")
        if result == "ready" and not valid_evidence(
            payload,
            expected_target=target,
            expected_sha=expected_sha,
            expected_cwd=cwd_raw,
            route=route,
            require_ready=True,
        ):
            raise ValueError("prediction ready evidence is not semantically verified")
        atomic_write(path, payload)
else:
    raise ValueError("unknown prediction state transition")
PY
}

validate_evidence() {
  state_transition evidence-validate "$1" "" "" "$REPO_ROOT" "" "0"
}

if [[ -f "$EVIDENCE_PATH" ]]; then
  validate_evidence "$EVIDENCE_PATH" \
    || fail "prediction cutover evidence record is invalid"
  INITIAL_EVIDENCE_OPERATION_ID="$(run_bounded "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["operation_id"])' \
    "$EVIDENCE_PATH")" || fail "prediction evidence operation is unavailable"
fi

write_route() {
  local mode="$1" operation_id="$2" expected_operation_id="${3:-}"
  state_transition route-write "$ROUTE_PATH" "$mode" "$operation_id" \
    "$expected_operation_id" "$STATE_LOCK_PATH"
}

now() {
  run_bounded "$PYTHON_BIN" -c \
    'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))'
}

gateway_health() {
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz || return 1
  GATEWAY_HEALTH="$CAPTURED_OUTPUT"
}

wait_gateway() {
  local mode="$1" require_zero="$2" attempt payload
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    if gateway_health && payload="$GATEWAY_HEALTH" \
      && run_bounded "$PYTHON_BIN" - "$payload" "$mode" "$require_zero" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = (
        isinstance(payload, dict)
        and payload.get("schema_version") == "open_trader.frontend_gateway.health.v1"
        and payload.get("module") == "frontend_gateway"
        and payload.get("prediction_route_mode") == sys.argv[2]
        and type(payload.get("prediction_inflight_requests")) is int
        and payload["prediction_inflight_requests"] >= 0
        and (sys.argv[3] != "1" or payload["prediction_inflight_requests"] == 0)
        and payload.get("prediction_upstream_status") == {
            "service": "ok", "legacy": "legacy", "maintenance": "maintenance",
        }[sys.argv[2]]
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
    then
      return 0
    fi
    [[ "$attempt" -lt "$WAIT_SECONDS" ]] \
      && run_bounded "$PYTHON_BIN" -c 'import time; time.sleep(1)'
  done
  return 1
}

label_pid() {
  local label="$1" output
  output="$(run_bounded "$LAUNCHCTL_BIN" print "gui/$UID/$label")" || return
  run_bounded "$PYTHON_BIN" - "$output" <<'PY'
import re, sys
match = re.search(r"(?m)^\s*pid = ([1-9][0-9]*)\s*$", sys.argv[1])
if match is None:
    raise SystemExit(1)
print(match.group(1))
PY
}

pid_absent() {
  local output status
  if capture_bounded "$PS_BIN" -p "$1" -o pid=; then
    return 1
  else
    status=$?
  fi
  output="$CAPTURED_OUTPUT"
  [[ "$status" -eq 1 && -z "$output" ]]
}

owner_available() {
  if [[ -n "$OWNER_PROBE_BIN" ]]; then
    run_bounded "$OWNER_PROBE_BIN" "$RUNTIME_ROOT/data"
    return
  fi
  PYTHONPATH="$REPO_ROOT/src" run_bounded "$PYTHON_BIN" - "$RUNTIME_ROOT/data" <<'PY'
from pathlib import Path
import sys
from open_trader.prediction_runtime import _RuntimeOwnershipLock
lock = _RuntimeOwnershipLock(Path(sys.argv[1]) / "prediction_arbitrage" / "runtime.lock")
lock.acquire()
lock.release()
PY
}

owner_held() {
  local output status
  if [[ -n "$OWNER_PROBE_BIN" ]]; then
    if output="$(run_bounded "$OWNER_PROBE_BIN" "$RUNTIME_ROOT/data" 2>&1)"; then
      return 1
    else
      status=$?
    fi
    [[ "$status" -eq 1 ]]
    return
  fi
  PYTHONPATH="$REPO_ROOT/src" run_bounded "$PYTHON_BIN" - "$RUNTIME_ROOT/data" <<'PY'
from pathlib import Path
import sys
from open_trader.prediction_runtime import (
    PredictionRuntimeOwnershipError,
    _RuntimeOwnershipLock,
)
lock = _RuntimeOwnershipLock(Path(sys.argv[1]) / "prediction_arbitrage" / "runtime.lock")
try:
    lock.acquire()
except PredictionRuntimeOwnershipError:
    raise SystemExit(0)
else:
    lock.release()
    raise SystemExit(1)
PY
}

prove_service_ready() {
  local health
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8769/healthz || return 1
  health="$CAPTURED_OUTPUT"
  run_bounded "$PYTHON_BIN" - "$RUNTIME_RECORD" "$health" "$REPO_ROOT" "$EXPECTED_SHA" <<'PY'
import json, sys
from pathlib import Path
try:
    record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    health = json.loads(sys.argv[2])
    candidate = record["candidate"]
    ready = record["ready"]
    valid = (
        record.get("schema_version") == "open_trader.prediction_service.runtime.v1"
        and record.get("state") == "ready"
        and isinstance(candidate, dict)
        and isinstance(ready, dict)
        and candidate.get("source_state") == "clean"
        and candidate.get("checkout") == sys.argv[3]
        and candidate.get("git_sha") == sys.argv[4]
        and type(candidate.get("reader_generation")) is int and candidate["reader_generation"] > 0
        and type(candidate.get("contract_generation")) is int and candidate["contract_generation"] > 0
        and type(ready.get("pid")) is int and ready["pid"] > 0
        and ready.get("cwd") == sys.argv[3]
        and ready.get("listener") == "127.0.0.1:8769"
        and ready.get("health_schema") == "open_trader.prediction_service.health.v1"
        and ready.get("health_module") == "prediction_service"
        and ready.get("health_status") == "running"
        and ready.get("mode") == "production"
        and ready.get("production_owner") is True
        and ready.get("mutations") == "enabled"
        and ready.get("git_sha") == sys.argv[4]
        and ready.get("release_schema_version") == "open_trader.prediction_service.release.v1"
        and type(ready.get("reader_generation")) is int and ready["reader_generation"] > 0
        and ready["reader_generation"] == candidate["reader_generation"]
        and type(ready.get("contract_generation")) is int and ready["contract_generation"] > 0
        and ready["contract_generation"] == candidate["contract_generation"]
        and isinstance(ready.get("process_started_at"), str) and bool(ready["process_started_at"])
        and health.get("schema_version") == "open_trader.prediction_service.health.v1"
        and health.get("module") == "prediction_service"
        and health.get("status") == "running"
        and health.get("mode") == "production"
        and health.get("production_owner") is True
        and health.get("mutations") == "enabled"
        and type(health.get("pid")) is int and health["pid"] == ready["pid"]
        and health.get("cwd") == sys.argv[3]
        and health.get("git_sha") == sys.argv[4]
        and health.get("release_schema_version") == ready["release_schema_version"]
        and type(health.get("reader_generation")) is int
        and health.get("reader_generation") == ready["reader_generation"]
        and type(health.get("contract_generation")) is int
        and health.get("contract_generation") == ready["contract_generation"]
        and health.get("started_at") == ready["process_started_at"]
    )
except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_public_payload() {
  run_bounded "$PYTHON_BIN" - "$1" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = (
        isinstance(payload, dict)
        and payload.get("status") == "healthy"
        and isinstance(payload.get("health"), dict)
        and payload["health"].get("status") == "healthy"
        and isinstance(payload.get("readiness"), dict)
        and payload["readiness"].get("status") == "ready"
        and payload.get("stale") is False
        and isinstance(payload.get("events"), list)
        and isinstance(payload.get("opportunities"), list)
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_history_payload() {
  run_bounded "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = (
        isinstance(payload, dict)
        and payload.get("kind") == sys.argv[2]
        and isinstance(payload.get("items"), list)
        and type(payload.get("total")) is int and payload["total"] >= 0
        and type(payload.get("limit")) is int and payload["limit"] > 0
        and type(payload.get("offset")) is int and payload["offset"] >= 0
        and type(payload.get("has_more")) is bool
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_direct_health() {
  run_bounded "$PYTHON_BIN" - "$1" "$TARGET" "$REPO_ROOT" "$EXPECTED_SHA" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    target = sys.argv[2]
    valid = isinstance(payload, dict)
    if target == "service":
        valid = valid and payload.get("schema_version") == "open_trader.prediction_service.health.v1"
        valid = valid and payload.get("status") == "running"
        valid = valid and payload.get("mode") == "production"
        valid = valid and payload.get("production_owner") is True
        valid = valid and payload.get("mutations") == "enabled"
        valid = valid and payload.get("module") == "prediction_service"
        valid = valid and payload.get("cwd") == sys.argv[3]
        valid = valid and payload.get("git_sha") == sys.argv[4]
    else:
        valid = valid and payload.get("schema_version") == "open_trader.legacy_dashboard.health.v1"
        valid = valid and payload.get("module") == "legacy_dashboard"
        valid = valid and payload.get("cwd") == sys.argv[3]
        valid = valid and payload.get("git_sha") == sys.argv[4]
        valid = valid and payload.get("source_state") == "clean"
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

validate_maintenance_response() {
  run_bounded "$PYTHON_BIN" - "$1" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = payload == {
        "schema_version": "open_trader.frontend_gateway.error.v1",
        "code": "prediction_maintenance",
        "message": "Prediction service is in maintenance",
        "route_mode": "maintenance",
    }
except (TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

prove_maintenance_public_contract() {
  local response_path status response
  response_path="$(mktemp "${TMPDIR:-/tmp}/open-trader-cutover-maintenance.XXXXXX")" \
    || return 1
  MAINTENANCE_TEMP_PATH="$response_path"
  if capture_bounded "$CURL_BIN" --silent --show-error --max-time 2 \
      --output "$response_path" --write-out '%{http_code}' \
      http://127.0.0.1:8766/api/prediction-arbitrage/state; then
    status=0
  else
    status=$?
  fi
  response="$(<"$response_path")"
  cleanup_maintenance_temp
  [[ "$status" -eq 0 && "$CAPTURED_OUTPUT" == "503" ]] || return 1
  validate_maintenance_response "$response"
}

ENDPOINT_STATE_CANONICAL=""
ENDPOINT_HISTORY_CANONICAL=""
ENDPOINT_PREVIEW_CANONICAL=""

validate_rejected_preview() {
  run_bounded "$PYTHON_BIN" - "$1" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = (
        isinstance(payload, dict)
        and payload.get("state") in {"rejected", "locked", "unavailable"}
        and "preview_id" not in payload
        and "execution_id" not in payload
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

normalize_prediction_payload() {
  run_bounded "$PYTHON_BIN" - "$1" <<'PY'
import json, sys

def normalize(value):
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in value.items()
            if key not in {
                "csrf_token", "timestamp", "created_at", "updated_at", "sampled_at",
                "process_id", "pid", "sampling", "heartbeat", "heartbeat_at",
            }
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value

try:
    print(json.dumps(normalize(json.loads(sys.argv[1])), sort_keys=True, separators=(",", ":")))
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
}

verify_prediction_endpoint() {
  local label="$1" base="$2" port="$3" auth_dir="$4"
  local state_before="$auth_dir/$label-state-before.json"
  local state_after="$auth_dir/$label-state-after.json"
  local csrf preview_status preview_payload payload kind history_before history_after
  local origin="http://127.0.0.1:$port"
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    --cookie-jar "$auth_dir/$label-cookies" --output "$state_before" \
    "$base/api/prediction-arbitrage/state" || return 1
  payload="$(<"$state_before")"
  validate_public_payload "$payload" || return 1
  [[ -s "$auth_dir/$label-cookies" ]] || return 1
  for kind in signals executions incidents; do
    capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
      --cookie "$auth_dir/$label-cookies" --output "$auth_dir/$label-$kind-before.json" \
      "$base/api/prediction-arbitrage/history?kind=$kind&limit=100&offset=0" || return 1
    validate_history_payload "$(<"$auth_dir/$label-$kind-before.json")" "$kind" || return 1
  done
  capture_bounded "$PYTHON_BIN" - "$payload" <<'PY' || return 1
import json, sys
try:
    token = json.loads(sys.argv[1]).get("csrf_token")
    if not isinstance(token, str) or not token:
        raise ValueError
    print(token)
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
  csrf="$CAPTURED_OUTPUT"
  capture_bounded "$CURL_BIN" --silent --show-error --max-time 2 \
    --cookie "$auth_dir/$label-cookies" \
    --header "Origin: $origin" --header "Referer: $origin/" \
    --header "X-CSRF-Token: $csrf" --header "Content-Type: application/json" \
    --data '{"opportunity_id":"__cutover_nonexistent_opportunity__"}' \
    --output "$auth_dir/$label-preview.json" --write-out '%{http_code}' \
    "$base/api/prediction-arbitrage/preview" || return 1
  preview_status="$CAPTURED_OUTPUT"
  preview_status="${preview_status##*$'\n'}"
  [[ "$preview_status" == "200" ]] || return 1
  preview_payload="$(<"$auth_dir/$label-preview.json")"
  validate_rejected_preview "$preview_payload" || return 1
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    --cookie "$auth_dir/$label-cookies" --output "$state_after" \
    "$base/api/prediction-arbitrage/state" || return 1
  payload="$(<"$state_after")"
  validate_public_payload "$payload" || return 1
  normalize_prediction_payload "$(<"$state_before")" >"$auth_dir/$label-state-before.normalized" \
    || return 1
  normalize_prediction_payload "$payload" >"$auth_dir/$label-state-after.normalized" \
    || return 1
  cmp -s "$auth_dir/$label-state-before.normalized" "$auth_dir/$label-state-after.normalized" \
    || return 1
  history_before=""
  history_after=""
  for kind in signals executions incidents; do
    capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
      --cookie "$auth_dir/$label-cookies" --output "$auth_dir/$label-$kind-after.json" \
      "$base/api/prediction-arbitrage/history?kind=$kind&limit=100&offset=0" || return 1
    validate_history_payload "$(<"$auth_dir/$label-$kind-after.json")" "$kind" || return 1
    normalize_prediction_payload "$(<"$auth_dir/$label-$kind-before.json")" \
      >"$auth_dir/$label-$kind-before.normalized" || return 1
    normalize_prediction_payload "$(<"$auth_dir/$label-$kind-after.json")" \
      >"$auth_dir/$label-$kind-after.normalized" || return 1
    cmp -s "$auth_dir/$label-$kind-before.normalized" \
      "$auth_dir/$label-$kind-after.normalized" || return 1
    history_before+="$(<"$auth_dir/$label-$kind-before.normalized")"
    history_after+="$(<"$auth_dir/$label-$kind-after.normalized")"
  done
  ENDPOINT_STATE_CANONICAL="$(<"$auth_dir/$label-state-before.normalized")"
  ENDPOINT_HISTORY_CANONICAL="$history_before"
  normalize_prediction_payload "$preview_payload" >"$auth_dir/$label-preview.normalized" \
    || return 1
  ENDPOINT_PREVIEW_CANONICAL="$(<"$auth_dir/$label-preview.normalized")"
}

prove_public_contract() {
  local gateway direct direct_port auth_dir
  DIRECT_STATE_VERIFIED=0
  DIRECT_HISTORY_VERIFIED=0
  DIRECT_PREVIEW_VERIFIED=0
  PUBLIC_STATE_VERIFIED=0
  PUBLIC_HISTORY_VERIFIED=0
  PUBLIC_PREVIEW_VERIFIED=0
  gateway_health || return 1
  gateway="$GATEWAY_HEALTH"
  direct_port=8767
  [[ "$TARGET" == "service" ]] && direct_port=8769
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    "http://127.0.0.1:${direct_port}/healthz" || return 1
  direct="$CAPTURED_OUTPUT"
  validate_direct_health "$direct" || return 1
  cleanup_auth_temp
  auth_dir="$(mktemp -d "${TMPDIR:-/tmp}/open-trader-cutover-auth.XXXXXX")" || return 1
  AUTH_TEMP_DIR="$auth_dir"
  verify_prediction_endpoint direct "http://127.0.0.1:$direct_port" "$direct_port" "$auth_dir" \
    || { cleanup_auth_temp; return 1; }
  DIRECT_STATE_VERIFIED=1
  DIRECT_HISTORY_VERIFIED=1
  DIRECT_PREVIEW_VERIFIED=1
  local direct_state="$ENDPOINT_STATE_CANONICAL"
  local direct_history="$ENDPOINT_HISTORY_CANONICAL"
  local direct_preview="$ENDPOINT_PREVIEW_CANONICAL"
  verify_prediction_endpoint public http://127.0.0.1:8766 8766 "$auth_dir" \
    || { cleanup_auth_temp; return 1; }
  PUBLIC_STATE_VERIFIED=1
  PUBLIC_HISTORY_VERIFIED=1
  PUBLIC_PREVIEW_VERIFIED=1
  public_state="$ENDPOINT_STATE_CANONICAL"
  public_history="$ENDPOINT_HISTORY_CANONICAL"
  local public_preview="$ENDPOINT_PREVIEW_CANONICAL"
  [[ "$direct_state" == "$public_state" \
    && "$direct_history" == "$public_history" \
    && "$direct_preview" == "$public_preview" ]] \
    || { cleanup_auth_temp; return 1; }
  cleanup_auth_temp
  PUBLIC_VERIFICATION_RESULT=1
  run_bounded "$PYTHON_BIN" - "$gateway" "$TARGET" <<'PY'
import json, sys
try:
    gateway = json.loads(sys.argv[1])
    expected_upstream = {
        "service": "ok", "legacy": "legacy", "maintenance": "maintenance",
    }[sys.argv[2]]
    valid = (
        isinstance(gateway, dict)
        and gateway.get("prediction_route_mode") == sys.argv[2]
        and gateway.get("prediction_upstream_status") == expected_upstream
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

prove_legacy_ready() {
  local pid health payload
  pid="$(label_pid com.open-trader.legacy-dashboard)" || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz || return 1
  health="$CAPTURED_OUTPUT"
  capture_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/api/prediction-arbitrage/state || return 1
  payload="$CAPTURED_OUTPUT"
  validate_public_payload "$payload" || return 1
  run_bounded "$PYTHON_BIN" - "$health" "$pid" "$REPO_ROOT" "$EXPECTED_SHA" <<'PY'
import json, sys
try:
    health = json.loads(sys.argv[1])
    valid = (
        isinstance(health, dict)
        and health.get("schema_version") == "open_trader.legacy_dashboard.health.v1"
        and health.get("module") == "legacy_dashboard"
        and type(health.get("pid")) is int
        and health["pid"] == int(sys.argv[2])
        and health.get("cwd") == sys.argv[3]
        and health.get("git_sha") == sys.argv[4]
        and health.get("source_state") == "clean"
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

prove_legacy_owner() {
  local output holders lock_path expected_plist
  output="$(run_bounded "$LAUNCHCTL_BIN" print \
    "gui/$UID/com.open-trader.legacy-dashboard")" || return 1
  expected_plist="$LAUNCH_AGENTS_DIR/com.open-trader.legacy-dashboard.plist"
  lock_path="$RUNTIME_ROOT/data/prediction_arbitrage/runtime.lock"
  holders="$(run_bounded "$LSOF_BIN" -nP -F p -- "$lock_path")" || return 1
  run_bounded "$PYTHON_BIN" - "$output" "$holders" "$expected_plist" \
    "$REPO_ROOT" <<'PY' || return 1
import re, sys
text, holder_text, expected_plist, expected_cwd = sys.argv[1:]
path = re.search(r"(?m)^\s*path = (.+?)\s*$", text)
cwd = re.search(r"(?m)^\s*working directory = (.+?)\s*$", text)
pid = re.search(r"(?m)^\s*pid = ([1-9][0-9]*)\s*$", text)
lines = text.splitlines()
starts = [index for index, line in enumerate(lines) if line.strip() == "arguments = {"]
argument_block_valid = len(starts) == 1
argv = []
if argument_block_valid:
    end = next(
        (index for index in range(starts[0] + 1, len(lines))
         if lines[index].strip() == "}"),
        None,
    )
    argument_block_valid = end is not None
    if end is not None:
        argv = [line.strip() for line in lines[starts[0] + 1:end] if line.strip()]
owner_enabled = any(
    argv[index:index + 2] == ["--prediction-owner", "enabled"]
    for index in range(len(argv) - 1)
)
holders = {
    line[1:] for line in holder_text.splitlines()
    if re.fullmatch(r"p[1-9][0-9]*", line)
}
valid = (
    path is not None and path.group(1) == expected_plist
    and cwd is not None and cwd.group(1) == expected_cwd
    and pid is not None
    and argument_block_valid
    and owner_enabled
    and holders == {pid.group(1)}
)
raise SystemExit(0 if valid else 1)
PY
  owner_held
}

write_evidence() {
  local result="$1" reason="$2" ended_at="$3" details
  if ! details="$(evidence_details)"; then
    [[ "$result" == "ready" ]] && return 1
    details='{"before":{"gateway":{"pid":null,"cwd":"","git_sha":"","listener":null},"legacy":{"pid":null,"cwd":"","git_sha":"","listener":null},"service":{"pid":null,"cwd":"","git_sha":"","listener":null}},"after":{"gateway":{"pid":null,"cwd":"","git_sha":"","listener":null},"legacy":{"pid":null,"cwd":"","git_sha":"","listener":null},"service":{"pid":null,"cwd":"","git_sha":"","listener":null}},"route":{"before_mode":"unknown","after_mode":"maintenance","inflight_before":null,"inflight_after":null},"owner":{"pid":null,"lock_holders":[],"before_lock_holders":[],"available":false},"service_runtime":{"state":"unknown","reader_generation":null,"contract_generation":null},"account":{"before":{"controller":{"pid":null,"cwd":"","git_sha":""},"api":{"pid":null,"cwd":"","git_sha":"","listener":null,"health_status":"","health_pid":null,"api_git_sha":"","worker_git_sha":"","release_match":false}},"after":{"controller":{"pid":null,"cwd":"","git_sha":""},"api":{"pid":null,"cwd":"","git_sha":"","listener":null,"health_status":"","health_pid":null,"api_git_sha":"","worker_git_sha":"","release_match":false}}},"verification":{"direct_backend":"failed","direct_state":false,"direct_history":false,"direct_preview_no_submit":false,"public_state":false,"public_history":false,"public_preview_no_submit":false}}'
  fi
  state_transition evidence-write "$EVIDENCE_PATH" "$ROUTE_PATH" \
    "$INITIAL_EVIDENCE_OPERATION_ID" "$OPERATION_ID" "$TARGET" "$EXPECTED_SHA" \
    "$result" "$reason" "$DOWNTIME_STARTED_AT" "$ended_at" "$STATE_LOCK_PATH" \
    "$REPO_ROOT" "$details"
}

evidence_details() {
  local route mode
  route="$(read_route 2>/dev/null || printf '%s' '{}')"
  mode="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1]).get("mode", "unknown"))' "$route" 2>/dev/null || printf '%s' 'unknown')"
  run_bounded "$PYTHON_BIN" - \
    "$BEFORE_GATEWAY_HEALTH" "$BEFORE_LEGACY_HEALTH" "$BEFORE_SERVICE_HEALTH" \
    "$AFTER_GATEWAY_HEALTH" "$AFTER_LEGACY_HEALTH" "$AFTER_SERVICE_HEALTH" \
    "$BEFORE_GATEWAY_PID" "$BEFORE_LEGACY_PID" "$BEFORE_SERVICE_PID" \
    "$GATEWAY_PID" "$LEGACY_PID" "$SERVICE_PID" \
    "$BEFORE_GATEWAY_LISTENER_PID" "$BEFORE_LEGACY_LISTENER_PID" "$BEFORE_SERVICE_LISTENER_PID" \
    "$GATEWAY_LISTENER_PID" "$LEGACY_LISTENER_PID" "$SERVICE_LISTENER_PID" \
    "$REPO_ROOT" "$BEFORE_ROUTE_MODE" "$mode" "$BEFORE_OWNER_HOLDERS" "$AFTER_OWNER_HOLDERS" "$RUNTIME_RECORD" \
    "$BEFORE_ACCOUNT_CONTROLLER_PID" "$BEFORE_ACCOUNT_API_PID" \
    "$BEFORE_ACCOUNT_CONTROLLER_SHA" "$BEFORE_ACCOUNT_API_SHA" \
    "$BEFORE_ACCOUNT_API_LISTENER_PID" "$BEFORE_ACCOUNT_API_HEALTH" \
    "$AFTER_ACCOUNT_CONTROLLER_PID" "$AFTER_ACCOUNT_API_PID" \
    "$AFTER_ACCOUNT_CONTROLLER_SHA" "$AFTER_ACCOUNT_API_SHA" \
    "$AFTER_ACCOUNT_API_LISTENER_PID" "$AFTER_ACCOUNT_API_HEALTH" \
    "$DIRECT_STATE_VERIFIED" "$DIRECT_HISTORY_VERIFIED" "$DIRECT_PREVIEW_VERIFIED" \
    "$PUBLIC_STATE_VERIFIED" "$PUBLIC_HISTORY_VERIFIED" "$PUBLIC_PREVIEW_VERIFIED" <<'PY'
import json, re, sys
from pathlib import Path

(
    before_gateway_raw, before_legacy_raw, before_service_raw,
    after_gateway_raw, after_legacy_raw, after_service_raw,
    before_gateway_pid_raw, before_legacy_pid_raw, before_service_pid_raw,
    gateway_pid_raw, legacy_pid_raw, service_pid_raw,
    before_gateway_listener_raw, before_legacy_listener_raw, before_service_listener_raw,
    gateway_listener_raw, legacy_listener_raw, service_listener_raw,
    repo, before_mode, after_mode, before_holders_raw, after_holders_raw, runtime_raw,
    before_account_controller_pid_raw, before_account_api_pid_raw,
    before_account_controller_sha, before_account_api_sha,
    before_account_api_listener_raw, before_account_api_health_raw,
    after_account_controller_pid_raw, after_account_api_pid_raw,
    after_account_controller_sha, after_account_api_sha,
    after_account_api_listener_raw, after_account_api_health_raw,
    direct_state, direct_history, direct_preview,
    public_state, public_history, public_preview,
) = sys.argv[1:]

def pid(raw):
    return int(raw) if re.fullmatch(r"[1-9][0-9]*", raw or "") else None

def health(raw):
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("health observation is not an object")
    return value

def component(raw, pid_raw, listener_raw, port):
    process_pid = pid(pid_raw)
    observed = health(raw)
    if process_pid is None:
        if observed is not None:
            raise ValueError("health exists for an absent process")
        return {"pid": None, "cwd": "", "git_sha": "", "listener": None}
    if observed is None or observed.get("pid") != process_pid:
        raise ValueError("process observation does not match health")
    cwd = observed.get("cwd")
    git_sha = observed.get("git_sha")
    if not isinstance(cwd, str) or not cwd or not isinstance(git_sha, str) or not git_sha:
        raise ValueError("process identity is incomplete")
    if pid(listener_raw) is None:
        raise ValueError("listener observation is missing")
    return {
        "pid": process_pid,
        "cwd": cwd,
        "git_sha": git_sha,
        "listener": f"127.0.0.1:{port}",
    }

def holders(raw):
    values = []
    for line in raw.splitlines():
        if not line:
            continue
        if not re.fullmatch(r"p[1-9][0-9]*", line):
            raise ValueError("invalid lock-holder observation")
        values.append(int(line[1:]))
    return sorted(set(values))

def account_snapshot(controller_pid_raw, api_pid_raw, controller_sha,
                     api_sha, api_listener_raw, api_health_raw):
    controller_pid = pid(controller_pid_raw)
    api_pid = pid(api_pid_raw)
    listener_pid = pid(api_listener_raw)
    try:
        health = json.loads(api_health_raw) if api_health_raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        health = {}
    return {
        "controller": {
            "pid": controller_pid,
            "cwd": repo if controller_pid is not None else "",
            "git_sha": controller_sha if controller_pid is not None else "",
        },
        "api": {
            "pid": api_pid,
            "cwd": repo if api_pid is not None else "",
            "git_sha": api_sha if api_pid is not None else "",
            "listener": "127.0.0.1:8768" if listener_pid is not None else None,
            "health_status": health.get("status", "") if isinstance(health, dict) else "",
            "health_pid": health.get("pid") if isinstance(health, dict) and type(health.get("pid")) is int else None,
            "api_git_sha": health.get("api_git_sha", "") if isinstance(health, dict) else "",
            "worker_git_sha": health.get("worker_git_sha", "") if isinstance(health, dict) else "",
            "release_match": health.get("release_match") is True if isinstance(health, dict) else False,
        },
    }

before_gateway = health(before_gateway_raw)
after_gateway = health(after_gateway_raw)
if after_gateway is None:
    raise ValueError("Gateway after-observation is required")
before = {
    "gateway": component(before_gateway_raw, before_gateway_pid_raw, before_gateway_listener_raw, 8766),
    "legacy": component(before_legacy_raw, before_legacy_pid_raw, before_legacy_listener_raw, 8767),
    "service": component(before_service_raw, before_service_pid_raw, before_service_listener_raw, 8769),
}
after = {
    "gateway": component(after_gateway_raw, gateway_pid_raw, gateway_listener_raw, 8766),
    "legacy": component(after_legacy_raw, legacy_pid_raw, legacy_listener_raw, 8767),
    "service": component(after_service_raw, service_pid_raw, service_listener_raw, 8769),
}
account = {
    "before": account_snapshot(
        before_account_controller_pid_raw, before_account_api_pid_raw,
        before_account_controller_sha, before_account_api_sha,
        before_account_api_listener_raw, before_account_api_health_raw,
    ),
    "after": account_snapshot(
        after_account_controller_pid_raw, after_account_api_pid_raw,
        after_account_controller_sha, after_account_api_sha,
        after_account_api_listener_raw, after_account_api_health_raw,
    ),
}
runtime_path = Path(runtime_raw)
runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.is_file() else {}
if not isinstance(runtime, dict):
    raise ValueError("runtime record is not an object")
ready = runtime.get("ready") if isinstance(runtime.get("ready"), dict) else {}
def generation(value):
    return value if type(value) is int and value > 0 else None
before_lock_holders = holders(before_holders_raw)
lock_holders = holders(after_holders_raw)
owner_pid = after["legacy"]["pid"] if after_mode == "legacy" else after["service"]["pid"]
if owner_pid is not None and lock_holders != [owner_pid]:
    raise ValueError("owner lock holder does not match selected owner")
payload = {
    "before": before,
    "after": after,
    "route": {
        "before_mode": before_mode,
        "after_mode": after_mode,
        "inflight_before": before_gateway.get("prediction_inflight_requests"),
        "inflight_after": after_gateway.get("prediction_inflight_requests"),
    },
    "owner": {
        "pid": owner_pid,
        "lock_holders": lock_holders,
        "before_lock_holders": before_lock_holders,
        "available": not bool(lock_holders),
    },
    "service_runtime": {
        "state": str(runtime.get("state", "unknown")),
        "reader_generation": generation(ready.get("reader_generation")),
        "contract_generation": generation(ready.get("contract_generation")),
    },
    "account": account,
    "verification": {
        "direct_backend": "verified" if all(value == "1" for value in (direct_state, direct_history, direct_preview)) else "failed",
        "direct_state": direct_state == "1",
        "direct_history": direct_history == "1",
        "direct_preview_no_submit": direct_preview == "1",
        "public_state": public_state == "1",
        "public_history": public_history == "1",
        "public_preview_no_submit": public_preview == "1",
    },
}
if payload["route"]["inflight_before"] is not None and type(payload["route"]["inflight_before"]) is not int:
    raise ValueError("invalid before Gateway inflight observation")
if payload["route"]["inflight_after"] is not None and type(payload["route"]["inflight_after"]) is not int:
    raise ValueError("invalid Gateway inflight observation")
print(json.dumps(payload, separators=(",", ":")))
PY
}

retain_maintenance() {
  local current mode operation_id
  current="$(read_route)" || return 1
  mode="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["mode"])' "$current")" \
    || return 1
  operation_id="$(run_bounded "$PYTHON_BIN" -c \
    'import json,sys; print(json.loads(sys.argv[1])["operation_id"])' "$current")" \
    || return 1
  if [[ "$mode" == "maintenance" && "$operation_id" != "$OPERATION_ID" ]]; then
    return 0
  fi
  if [[ "$operation_id" == "$OPERATION_ID" ]]; then
    write_route maintenance "$OPERATION_ID" "$OPERATION_ID"
  else
    write_route maintenance "$OPERATION_ID" "$INITIAL_OPERATION_ID"
  fi
}

route_owned_maintenance() {
  local current
  current="$(read_route)" || return 1
  run_bounded "$PYTHON_BIN" - "$current" "$OPERATION_ID" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    valid = payload.get("mode") == "maintenance" and payload.get("operation_id") == sys.argv[2]
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

fail_in_maintenance() {
  local reason="$1" ended_at
  if ! retain_maintenance; then
    echo "failed to retain prediction maintenance route" >&2
  fi
  if ! route_owned_maintenance; then
    echo "prediction cutover was displaced; active evidence preserved" >&2
    echo "prediction cutover failed in maintenance: $reason" >&2
    exit 1
  fi
  ended_at="$(now 2>/dev/null || true)"
  if ! write_evidence failed "$reason" "$ended_at"; then
    echo "failed to write prediction cutover failure evidence" >&2
  fi
  echo "prediction cutover failed in maintenance: $reason" >&2
  exit 1
}

account_health_unchanged() {
  run_bounded "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json, sys
try:
    before = json.loads(sys.argv[1])
    after = json.loads(sys.argv[2])
    valid = (
        isinstance(before, dict)
        and isinstance(after, dict)
        and before.get("module") == "account_api"
        and after.get("module") == "account_api"
        and before.get("status") == after.get("status") == "ok"
        and before.get("pid") == after.get("pid")
        and before.get("api_git_sha") == after.get("api_git_sha")
        and before.get("worker_git_sha") == after.get("worker_git_sha")
    )
except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

if [[ "$INITIAL_MODE" == "$TARGET" ]]; then
  evidence_is_ready_for_target || fail "completed target evidence is unavailable"
  if [[ "$TARGET" == "service" ]]; then
    prove_service_ready || fail "completed Service runtime is not verified"
  else
    prove_legacy_ready || fail "completed Legacy runtime is not verified"
    prove_legacy_owner || fail "completed Legacy owner is not verified"
  fi
  wait_gateway "$TARGET" 1 || fail "completed target Gateway route is not verified"
  prove_public_contract || fail "completed target public contract is not verified"
  account_state_unchanged || fail "Account changed while repeating completed target"
  echo "prediction route already ready: $TARGET"
  exit 0
fi

if [[ "$TARGET" == "service" ]]; then
  if [[ "$INITIAL_MODE" == "absent" ]]; then
    OLD_GATEWAY_PID="$GATEWAY_PID"
    [[ -n "$DOWNTIME_STARTED_AT" ]] || DOWNTIME_STARTED_AT="$(now)" \
      || fail "timestamp_failed"
    run_bounded "$LAUNCHCTL_BIN" bootout \
      "gui/$UID/com.open-trader.frontend-gateway" \
      || fail "gateway_bootout_failed"
    for _ in 1 2 3 4 5; do
      if gateway_probe="$(run_bounded "$LAUNCHCTL_BIN" print \
          "gui/$UID/com.open-trader.frontend-gateway" 2>&1)"; then
        fail "gateway_pid_still_loaded"
      elif [[ "$gateway_probe" != *"Could not find service"* ]]; then
        fail "gateway_label_absence_unproven"
      fi
      [[ "$_" -lt 5 ]] && run_bounded "$PYTHON_BIN" -c 'import time; time.sleep(1)'
    done
    pid_absent "$OLD_GATEWAY_PID" || fail "gateway_pid_absence_unproven"
    listener_absent 8766 || fail "gateway_listener_absence_unproven"
    state_transition route-bootstrap "$ROUTE_PATH" "$OPERATION_ID" "$STATE_LOCK_PATH" \
      || fail "failed to seed maintenance route"
    route_owned_maintenance || fail_in_maintenance bootstrap_route_ownership_unverified
    INITIAL_MODE="maintenance"
    INITIAL_OPERATION_ID="$OPERATION_ID"
    INITIAL_ROUTE="$(read_route)" || fail_in_maintenance bootstrap_route_read_failed
    run_bounded "$INSTALL_DASHBOARD" --mode stack --prediction-owner enabled \
      --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" \
      --launch-agents-dir "$LAUNCH_AGENTS_DIR" --wait-seconds "$WAIT_SECONDS" \
      || fail_in_maintenance compatibility_bootstrap_failed
    route_owned_maintenance || fail_in_maintenance stale_operation
    GATEWAY_PID="$(inspect_label com.open-trader.frontend-gateway 1)" \
      || fail_in_maintenance gateway_identity_unproven
    [[ "$GATEWAY_PID" != "$OLD_GATEWAY_PID" ]] \
      || fail_in_maintenance gateway_pid_not_replaced
    GATEWAY_LISTENER_PID="$(listener_pid 8766)" \
      || fail_in_maintenance gateway_listener_unproven
    [[ "$GATEWAY_LISTENER_PID" == "$GATEWAY_PID" ]] \
      || fail_in_maintenance gateway_listener_owner_unproven
    LEGACY_PID="$(inspect_label com.open-trader.legacy-dashboard 1)" \
      || fail_in_maintenance legacy_identity_unproven
    LEGACY_LISTENER_PID="$(listener_pid 8767)" \
      || fail_in_maintenance legacy_listener_unproven
    [[ "$LEGACY_LISTENER_PID" == "$LEGACY_PID" ]] \
      || fail_in_maintenance legacy_listener_owner_unproven
    preflight_health || fail_in_maintenance compatibility_health_unproven
    prove_legacy_ready || fail_in_maintenance compatibility_legacy_health_unproven
    prove_legacy_owner || fail_in_maintenance compatibility_legacy_owner_unproven
    account_state_unchanged \
      || fail_in_maintenance account_changed_during_bootstrap
    SERVICE_PID="$(inspect_label com.open-trader.prediction-service 0)" \
      || fail_in_maintenance service_identity_unproven
    [[ -z "$SERVICE_PID" ]] || fail_in_maintenance unexpected_prediction_service
    listener_absent 8769 || fail_in_maintenance unexpected_prediction_listener
    INITIAL_EVIDENCE_OPERATION_ID="__absent__"
  fi
  if [[ -n "$DOWNTIME_STARTED_AT" ]]; then
    OLD_LEGACY_PID="$(label_pid com.open-trader.legacy-dashboard)" \
      || fail_in_maintenance legacy_pid_inspection_failed
  else
    OLD_LEGACY_PID="$(label_pid com.open-trader.legacy-dashboard)" \
      || fail "failed to inspect Legacy Dashboard PID"
  fi
  [[ -n "$OLD_LEGACY_PID" ]] || fail_in_maintenance legacy_pid_invalid
  [[ -n "$DOWNTIME_STARTED_AT" ]] || DOWNTIME_STARTED_AT="$(now)" \
    || fail_in_maintenance timestamp_failed
  write_route maintenance "$OPERATION_ID" "$INITIAL_OPERATION_ID" \
    || fail_in_maintenance route_write_failed
  wait_gateway maintenance 0 || fail_in_maintenance gateway_maintenance_unobserved
  route_owned_maintenance || fail_in_maintenance stale_operation
  wait_gateway maintenance 1 || fail_in_maintenance prediction_inflight_timeout
  route_owned_maintenance || fail_in_maintenance stale_operation
  prove_maintenance_public_contract \
    || fail_in_maintenance maintenance_public_contract_unproven
  run_bounded "$INSTALL_DASHBOARD" --mode legacy --prediction-owner disabled \
    --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" \
    --launch-agents-dir "$LAUNCH_AGENTS_DIR" --wait-seconds "$WAIT_SECONDS" \
    || fail_in_maintenance legacy_restart_failed
  route_owned_maintenance || fail_in_maintenance stale_operation
  pid_absent "$OLD_LEGACY_PID" || fail_in_maintenance legacy_pid_absence_unproven
  route_owned_maintenance || fail_in_maintenance stale_operation
  owner_available || fail_in_maintenance owner_unavailable
  route_owned_maintenance || fail_in_maintenance stale_operation
  run_bounded "$INSTALL_SERVICE" --mode production --repo-root "$REPO_ROOT" \
    --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" --config "$PREDICTION_CONFIG" \
    --launch-agents-dir "$LAUNCH_AGENTS_DIR" --wait-seconds "$WAIT_SECONDS" \
    --expected-sha "$EXPECTED_SHA" || fail_in_maintenance service_install_failed
  route_owned_maintenance || fail_in_maintenance stale_operation
  prove_service_ready || fail_in_maintenance service_readiness_unproven
  SERVICE_PID="$(label_pid com.open-trader.prediction-service)" \
    || fail_in_maintenance service_pid_unproven
  SERVICE_LISTENER_PID="$(listener_pid 8769)" \
    || fail_in_maintenance service_listener_unproven
  [[ "$SERVICE_LISTENER_PID" == "$SERVICE_PID" ]] \
    || fail_in_maintenance service_listener_owner_unproven
  route_owned_maintenance || fail_in_maintenance stale_operation
  write_route service "$OPERATION_ID" "$OPERATION_ID" \
    || fail_in_maintenance service_route_write_failed
  wait_gateway service 1 || fail_in_maintenance gateway_service_route_unobserved
  prove_public_contract || fail_in_maintenance public_contract_unproven
  account_state_unchanged || fail_in_maintenance account_changed_during_cutover
  capture_after_snapshot || fail_in_maintenance runtime_evidence_after_snapshot_unverified
  DOWNTIME_ENDED_AT="$(now)" || fail_in_maintenance timestamp_failed
  write_evidence ready "" "$DOWNTIME_ENDED_AT" \
    || fail_in_maintenance evidence_write_failed
  echo "prediction cutover ready: service"
  exit 0
fi

[[ -n "$DOWNTIME_STARTED_AT" ]] || DOWNTIME_STARTED_AT="$(now)" \
  || fail_in_maintenance timestamp_failed
write_route maintenance "$OPERATION_ID" "$INITIAL_OPERATION_ID" \
  || fail_in_maintenance route_write_failed
wait_gateway maintenance 0 || fail_in_maintenance gateway_maintenance_unobserved
route_owned_maintenance || fail_in_maintenance stale_operation
wait_gateway maintenance 1 || fail_in_maintenance prediction_inflight_timeout
route_owned_maintenance || fail_in_maintenance stale_operation
prove_maintenance_public_contract \
  || fail_in_maintenance maintenance_public_contract_unproven
if [[ -n "$SERVICE_PID" || -n "$SERVICE_LISTENER_PID" ]]; then
  [[ -n "$SERVICE_PID" && -n "$SERVICE_LISTENER_PID" ]] \
    || fail_in_maintenance service_absence_unproven
  run_bounded "$UNINSTALL_SERVICE" --mode production --runtime-root "$RUNTIME_ROOT" \
    --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
    || fail_in_maintenance service_uninstall_failed
  SERVICE_RUNTIME_PRESENT=0
elif [[ "$SERVICE_RUNTIME_PRESENT" -eq 1 ]]; then
  [[ "$SERVICE_RUNTIME_STATE" == "failed" || "$SERVICE_RUNTIME_STATE" == "stopped" ]] \
    || fail_in_maintenance service_absence_unproven
  run_bounded "$UNINSTALL_SERVICE" --mode production --runtime-root "$RUNTIME_ROOT" \
    --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
    || fail_in_maintenance service_uninstall_failed
  SERVICE_RUNTIME_PRESENT=0
fi
route_owned_maintenance || fail_in_maintenance stale_operation
if [[ -z "$SERVICE_PID" && -z "$SERVICE_LISTENER_PID" && "$SERVICE_RUNTIME_PRESENT" -eq 0 ]]; then
  if ! owner_available; then
    prove_legacy_owner || fail_in_maintenance owner_unavailable
  fi
else
  owner_available || fail_in_maintenance owner_unavailable
fi
route_owned_maintenance || fail_in_maintenance stale_operation
run_bounded "$INSTALL_DASHBOARD" --mode legacy --prediction-owner enabled \
  --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" --wait-seconds "$WAIT_SECONDS" \
  || fail_in_maintenance legacy_restart_failed
route_owned_maintenance || fail_in_maintenance stale_operation
prove_legacy_ready || fail_in_maintenance legacy_readiness_unproven
prove_legacy_owner || fail_in_maintenance legacy_owner_lock_unproven
LEGACY_PID="$(label_pid com.open-trader.legacy-dashboard)" \
  || fail_in_maintenance legacy_pid_unproven
LEGACY_LISTENER_PID="$(listener_pid 8767)" \
  || fail_in_maintenance legacy_listener_unproven
[[ "$LEGACY_LISTENER_PID" == "$LEGACY_PID" ]] \
  || fail_in_maintenance legacy_listener_owner_unproven
route_owned_maintenance || fail_in_maintenance stale_operation
write_route legacy "$OPERATION_ID" "$OPERATION_ID" \
  || fail_in_maintenance legacy_route_write_failed
  wait_gateway legacy 1 || fail_in_maintenance gateway_legacy_route_unobserved
  prove_public_contract || fail_in_maintenance public_contract_unproven
  account_state_unchanged || fail_in_maintenance account_changed_during_cutover
capture_after_snapshot || fail_in_maintenance runtime_evidence_after_snapshot_unverified
DOWNTIME_ENDED_AT="$(now)" || fail_in_maintenance timestamp_failed
write_evidence ready "" "$DOWNTIME_ENDED_AT" \
  || fail_in_maintenance evidence_write_failed
echo "prediction cutover ready: legacy"
