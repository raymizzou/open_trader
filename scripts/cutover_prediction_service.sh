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
[[ -n "$REPO_ROOT" && -n "$RUNTIME_ROOT" && -n "$PYTHON_BIN" \
  && -n "$EXPECTED_SHA" && -n "$PREDICTION_CONFIG" \
  && -n "$LAUNCH_AGENTS_DIR" && "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ \
  && "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{40}$ ]] || { usage; exit 2; }

fail() {
  echo "$*" >&2
  exit 1
}

require_executable() {
  [[ -n "$1" && -x "$1" ]] || fail "required executable is unavailable: $1"
}

COMMAND_TIMEOUT_SECONDS=$((WAIT_SECONDS + 5))
ACTIVE_RUNNER_PID=""
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
[[ -f "$ROUTE_PATH" && ! -L "$ROUTE_PATH" ]] || fail "prediction route path is invalid"
[[ ! -e "$EVIDENCE_PATH" || ( -f "$EVIDENCE_PATH" && ! -L "$EVIDENCE_PATH" ) ]] \
  || fail "prediction evidence path is invalid"

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

INITIAL_ROUTE="$(read_route)" || fail "prediction route record is invalid"
INITIAL_MODE="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["mode"])' "$INITIAL_ROUTE")"
INITIAL_OPERATION_ID="$(run_bounded "$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["operation_id"])' "$INITIAL_ROUTE")"

validate_evidence() {
  run_bounded "$PYTHON_BIN" - "$EVIDENCE_PATH" <<'PY'
import json, re, sys
from pathlib import Path
try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    valid = (
        isinstance(payload, dict)
        and set(payload) == {
            "schema_version", "operation_id", "target", "expected_sha", "result",
            "failure_reason", "downtime_started_at", "downtime_ended_at",
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
        and (
            payload["result"] != "ready"
            or (
                not payload["failure_reason"]
                and bool(payload["downtime_started_at"])
                and bool(payload["downtime_ended_at"])
            )
        )
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

[[ ! -f "$EVIDENCE_PATH" ]] || validate_evidence \
  || fail "prediction cutover evidence record is invalid"
INITIAL_EVIDENCE_OPERATION_ID="__absent__"
if [[ -f "$EVIDENCE_PATH" ]]; then
  INITIAL_EVIDENCE_OPERATION_ID="$(run_bounded "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["operation_id"])' \
    "$EVIDENCE_PATH")"
fi

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
  if output="$(run_bounded "$LSOF_BIN" -nP -iTCP:"$port" -sTCP:LISTEN -Fn 2>&1)"; then
    status=0
  else
    status=$?
  fi
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

preflight_health() {
  local gateway legacy
  gateway="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz)" || return 1
  legacy="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz)" || return 1
  run_bounded "$PYTHON_BIN" - "$gateway" "$legacy" "$GATEWAY_PID" "$LEGACY_PID" \
    "$REPO_ROOT" "$EXPECTED_SHA" "$INITIAL_ROUTE" <<'PY'
import json, sys
try:
    gateway, legacy, route = json.loads(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[7])
    expected_gateway_pid, expected_legacy_pid = int(sys.argv[3]), int(sys.argv[4])
    expected_prediction_status = "ok" if route["mode"] == "service" else "not_selected"
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

verify_relevant_label_set() {
  local output expected_service_count
  output="$(run_bounded "$LAUNCHCTL_BIN" print "gui/$UID")" || return 1
  expected_service_count=0
  [[ "$INITIAL_MODE" == "service" ]] && expected_service_count=1
  run_bounded "$PYTHON_BIN" - "$output" "$expected_service_count" <<'PY'
import re, sys
labels = re.findall(
    r'(?m)^\s*(?:[1-9][0-9]*|-)\s+(?:[0-9]+|-)\s+'
    r'(com\.open-trader\.[^\s]+)\s*$', sys.argv[1]
)
expected = {
    "com.open-trader.frontend-gateway": 1,
    "com.open-trader.legacy-dashboard": 1,
    "com.open-trader.prediction-service": int(sys.argv[2]),
}
relevant = [
    label for label in labels
    if any(term in label for term in ("prediction", "frontend-gateway", "legacy-dashboard"))
]
valid = all(relevant.count(label) == count for label, count in expected.items())
valid = valid and all(label in expected for label in relevant)
raise SystemExit(0 if valid else 1)
PY
}

[[ "$INITIAL_MODE" != "maintenance" ]] || fail "another prediction cutover owns maintenance"
verify_relevant_label_set || fail "relevant launchd label set is not verified"
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
else
  [[ -z "$SERVICE_PID" ]] || fail "non-service route has a loaded Prediction Service"
fi
preflight_health || fail "Gateway and Legacy runtime health is not verified"

evidence_is_ready_for_target() {
  [[ -f "$EVIDENCE_PATH" ]] || return 1
  run_bounded "$PYTHON_BIN" - "$EVIDENCE_PATH" "$TARGET" "$EXPECTED_SHA" "$INITIAL_ROUTE" <<'PY'
import json, sys
from pathlib import Path
try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    route = json.loads(sys.argv[4])
    valid = (
        isinstance(payload, dict)
        and set(payload) == {
            "schema_version", "operation_id", "target", "expected_sha", "result",
            "failure_reason", "downtime_started_at", "downtime_ended_at",
        }
        and payload.get("schema_version") == "open_trader.prediction_cutover.evidence.v1"
        and payload.get("operation_id") == route.get("operation_id")
        and payload.get("result") == "ready"
        and payload.get("target") == sys.argv[2]
        and payload.get("expected_sha") == sys.argv[3]
        and payload.get("failure_reason") == ""
        and isinstance(payload.get("downtime_started_at"), str)
        and bool(payload["downtime_started_at"])
        and isinstance(payload.get("downtime_ended_at"), str)
        and bool(payload["downtime_ended_at"])
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

LOCK_DIR="$RUNTIME_ROOT/config/.prediction-cutover.lock"
run_bounded "$PYTHON_BIN" -c \
  'from pathlib import Path; import sys; Path(sys.argv[1]).mkdir()' "$LOCK_DIR" 2>/dev/null \
  || fail "another prediction cutover is active"
OPERATION_LOCK_HELD=1
cleanup_lock() {
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

def valid_evidence(payload):
    return (
        isinstance(payload, dict)
        and set(payload) == {
            "schema_version", "operation_id", "target", "expected_sha", "result",
            "failure_reason", "downtime_started_at", "downtime_ended_at",
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
        and (
            payload["result"] != "ready"
            or (
                not payload["failure_reason"]
                and bool(payload["downtime_started_at"])
                and bool(payload["downtime_ended_at"])
            )
        )
    )

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
if operation == "route-write":
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
        if (
            mode == "maintenance"
            and current.get("mode") == "maintenance"
            and current.get("operation_id") != operation_id
        ):
            raise ValueError("another prediction cutover owns maintenance")
        payload = {
            "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
            "mode": mode,
            "operation_id": operation_id,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        atomic_write(route_path, payload)
elif operation == "evidence-write":
    (
        _, path_raw, route_raw, initial_evidence_operation_id, operation_id,
        target, expected_sha, result, reason, started, ended, lock_raw,
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
        if not valid_evidence(payload):
            raise ValueError("invalid prediction evidence payload")
        atomic_write(path, payload)
else:
    raise ValueError("unknown prediction state transition")
PY
}

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
  run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/healthz
}

wait_gateway() {
  local mode="$1" require_zero="$2" attempt payload
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    if payload="$(gateway_health)" && run_bounded "$PYTHON_BIN" - "$payload" "$mode" "$require_zero" <<'PY'
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
        and (
            payload.get("prediction_upstream_status") == "ok"
            if sys.argv[2] == "service"
            else payload.get("prediction_upstream_status") == "not_selected"
        )
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
  if output="$(run_bounded "$PS_BIN" -p "$1" 2>&1)"; then
    return 1
  else
    status=$?
  fi
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
  health="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8769/healthz)" \
    || return 1
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

prove_public_contract() {
  local payload gateway
  gateway="$(gateway_health)" || return 1
  payload="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8766/api/prediction-arbitrage/state)" || return 1
  validate_public_payload "$payload" || return 1
  run_bounded "$PYTHON_BIN" - "$gateway" "$TARGET" <<'PY'
import json, sys
try:
    gateway = json.loads(sys.argv[1])
    expected_upstream = "ok" if sys.argv[2] == "service" else "not_selected"
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
  health="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/healthz)" || return 1
  payload="$(run_bounded "$CURL_BIN" --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8767/api/prediction-arbitrage/state)" || return 1
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
  local result="$1" reason="$2" ended_at="$3"
  state_transition evidence-write "$EVIDENCE_PATH" "$ROUTE_PATH" \
    "$INITIAL_EVIDENCE_OPERATION_ID" "$OPERATION_ID" "$TARGET" "$EXPECTED_SHA" \
    "$result" "$reason" "$DOWNTIME_STARTED_AT" "$ended_at" "$STATE_LOCK_PATH"
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

if [[ "$INITIAL_MODE" == "$TARGET" ]]; then
  evidence_is_ready_for_target || fail "completed target evidence is unavailable"
  if [[ "$TARGET" == "service" ]]; then
    prove_service_ready || fail "completed Service runtime is not verified"
  else
    prove_legacy_ready || fail "completed Legacy runtime is not verified"
    prove_legacy_owner || fail "completed Legacy owner is not verified"
  fi
  wait_gateway "$TARGET" 0 || fail "completed target Gateway route is not verified"
  prove_public_contract || fail "completed target public contract is not verified"
  echo "prediction route already ready: $TARGET"
  exit 0
fi

if [[ "$TARGET" == "service" ]]; then
  OLD_LEGACY_PID="$(label_pid com.open-trader.legacy-dashboard)" \
    || fail "failed to inspect Legacy Dashboard PID"
  [[ -n "$OLD_LEGACY_PID" ]] || fail "Legacy Dashboard PID is invalid"
  DOWNTIME_STARTED_AT="$(now)"
  write_route maintenance "$OPERATION_ID" "$INITIAL_OPERATION_ID" \
    || fail_in_maintenance route_write_failed
  wait_gateway maintenance 0 || fail_in_maintenance gateway_maintenance_unobserved
  route_owned_maintenance || fail_in_maintenance stale_operation
  wait_gateway maintenance 1 || fail_in_maintenance prediction_inflight_timeout
  route_owned_maintenance || fail_in_maintenance stale_operation
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
  route_owned_maintenance || fail_in_maintenance stale_operation
  write_route service "$OPERATION_ID" "$OPERATION_ID" \
    || fail_in_maintenance service_route_write_failed
  wait_gateway service 0 || fail_in_maintenance gateway_service_route_unobserved
  prove_public_contract || fail_in_maintenance public_contract_unproven
  DOWNTIME_ENDED_AT="$(now)" || fail_in_maintenance timestamp_failed
  write_evidence ready "" "$DOWNTIME_ENDED_AT" \
    || fail_in_maintenance evidence_write_failed
  echo "prediction cutover ready: service"
  exit 0
fi

write_route maintenance "$OPERATION_ID" "$INITIAL_OPERATION_ID" \
  || fail_in_maintenance route_write_failed
DOWNTIME_STARTED_AT="$(now)" || fail_in_maintenance timestamp_failed
wait_gateway maintenance 0 || fail_in_maintenance gateway_maintenance_unobserved
route_owned_maintenance || fail_in_maintenance stale_operation
wait_gateway maintenance 1 || fail_in_maintenance prediction_inflight_timeout
route_owned_maintenance || fail_in_maintenance stale_operation
run_bounded "$UNINSTALL_SERVICE" --mode production --runtime-root "$RUNTIME_ROOT" \
  --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  || fail_in_maintenance service_uninstall_failed
route_owned_maintenance || fail_in_maintenance stale_operation
owner_available || fail_in_maintenance owner_unavailable
route_owned_maintenance || fail_in_maintenance stale_operation
run_bounded "$INSTALL_DASHBOARD" --mode legacy --prediction-owner enabled \
  --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" --wait-seconds "$WAIT_SECONDS" \
  || fail_in_maintenance legacy_restart_failed
route_owned_maintenance || fail_in_maintenance stale_operation
prove_legacy_ready || fail_in_maintenance legacy_readiness_unproven
prove_legacy_owner || fail_in_maintenance legacy_owner_lock_unproven
route_owned_maintenance || fail_in_maintenance stale_operation
write_route legacy "$OPERATION_ID" "$OPERATION_ID" \
  || fail_in_maintenance legacy_route_write_failed
wait_gateway legacy 0 || fail_in_maintenance gateway_legacy_route_unobserved
prove_public_contract || fail_in_maintenance public_contract_unproven
DOWNTIME_ENDED_AT="$(now)" || fail_in_maintenance timestamp_failed
write_evidence ready "" "$DOWNTIME_ENDED_AT" \
  || fail_in_maintenance evidence_write_failed
echo "prediction cutover ready: legacy"
