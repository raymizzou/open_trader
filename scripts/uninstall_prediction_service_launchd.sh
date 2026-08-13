#!/usr/bin/env bash
set -euo pipefail

MODE="shadow"
RUNTIME_ROOT=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
PS_BIN="${PS_BIN:-/bin/ps}"
LABEL="com.open-trader.prediction-service"

usage() {
  echo "usage: $0 [--mode shadow|production] [--runtime-root PATH] [--python PATH] [--launch-agents-dir PATH]" >&2
}

fail() {
  echo "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) [[ $# -ge 2 ]] || { usage; exit 2; }; MODE="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { echo "missing value for --launch-agents-dir" >&2; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$MODE" == "shadow" || "$MODE" == "production" ]] || { usage; exit 2; }
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
  [[ "$status" -eq 1 && -z "$output" ]] && return 0
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

if [[ "$MODE" == "shadow" ]]; then
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL" 2>/dev/null || true
  wait_agent_absent
  wait_listener_absent
  if [[ -f "$PLIST_PATH" ]]; then rm "$PLIST_PATH"; echo "removed launchd agent: $PLIST_PATH"; else echo "launchd agent not installed: $PLIST_PATH"; fi
  exit 0
fi

[[ -n "$RUNTIME_ROOT" ]] || { usage; exit 2; }
RUNTIME_ROOT="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$RUNTIME_ROOT")"
DATA_DIR="$RUNTIME_ROOT/data"
RUNTIME_RECORD="$RUNTIME_ROOT/prediction-service-runtime.json"
CURRENT_RECORD_JSON="$(PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY'
import json, sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record
record = load_prediction_runtime_record(Path(sys.argv[1]))
if record is not None and record["state"] not in {"ready", "failed", "stopped"}:
    raise ValueError(f"prediction runtime state cannot be uninstalled: {record['state']}")
print("null" if record is None else json.dumps(record, separators=(",", ":")))
PY
)"
[[ "$CURRENT_RECORD_JSON" != "null" ]] || fail "prediction runtime record is required for production uninstall"

owner_available() {
  if [[ -n "${OWNER_PROBE_BIN:-}" ]]; then
    "$OWNER_PROBE_BIN" "$DATA_DIR"
    return
  fi
  PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$DATA_DIR" <<'PY'
from pathlib import Path
import sys
from open_trader.prediction_runtime import _RuntimeOwnershipLock
lock = _RuntimeOwnershipLock(Path(sys.argv[1]) / "prediction_arbitrage" / "runtime.lock")
lock.acquire()
lock.release()
PY
}

pid_absent() {
  local output status
  if output="$("$PS_BIN" -p "$1" -o pid= 2>&1)"; then
    echo "prediction service PID is still present: $1" >&2
    return 1
  else
    status=$?
  fi
  [[ "$status" -eq 1 && -z "$output" ]] && return 0
  printf '%s\n' "$output" >&2
  return "$status"
}

managed_identity_matches() {
  "$PYTHON_BIN" - "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, sys
pid, cwd, listener, health_raw, record_raw = sys.argv[1:]
try:
    expected_pid = int(pid)
    health = json.loads(health_raw)
    record = json.loads(record_raw)
    candidate = record["candidate"]
    ready = record["ready"]
    valid = (
        record.get("state") == "ready"
        and isinstance(candidate, dict)
        and isinstance(ready, dict)
        and candidate.get("source_state") == "clean"
        and candidate.get("checkout") == cwd
        and candidate.get("git_sha") == health.get("git_sha")
        and type(candidate.get("reader_generation")) is int
        and candidate.get("reader_generation") == health.get("reader_generation")
        and type(candidate.get("contract_generation")) is int
        and candidate.get("contract_generation") == health.get("contract_generation")
        and health.get("schema_version") == "open_trader.prediction_service.health.v1"
        and health.get("module") == "prediction_service"
        and health.get("status") == "running"
        and health.get("mode") == "production"
        and health.get("production_owner") is True
        and health.get("mutations") == "enabled"
        and health.get("pid") == expected_pid
        and health.get("cwd") == cwd
        and health.get("release_schema_version") == "open_trader.prediction_service.release.v1"
        and type(health.get("reader_generation")) is int
        and type(health.get("contract_generation")) is int
        and ready.get("pid") == expected_pid
        and ready.get("cwd") == cwd
        and ready.get("listener") == listener
        and ready.get("health_schema") == health.get("schema_version")
        and ready.get("health_module") == health.get("module")
        and ready.get("health_status") == health.get("status")
        and ready.get("mode") == health.get("mode")
        and ready.get("production_owner") is health.get("production_owner")
        and ready.get("mutations") == health.get("mutations")
        and ready.get("git_sha") == health.get("git_sha")
        and ready.get("release_schema_version") == health.get("release_schema_version")
        and type(ready.get("reader_generation")) is int
        and ready.get("reader_generation") == health.get("reader_generation")
        and type(ready.get("contract_generation")) is int
        and ready.get("contract_generation") == health.get("contract_generation")
        and ready.get("process_started_at") == health.get("started_at")
    )
except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

LABEL_OUTPUT=""
OLD_PID=""
if LABEL_OUTPUT="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
  LABEL_PATH="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "path" && $2 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
  OLD_PID="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ { print $3; exit }')"
  [[ "$LABEL_PATH" == "$PLIST_PATH" && -n "$OLD_PID" ]] \
    || fail "managed launchd identity is not verified"
  CURRENT_CWD="$("$LSOF_BIN" -a -p "$OLD_PID" -d cwd -Fn 2>/dev/null \
    | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
  LISTENER_OUTPUT="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>/dev/null || true)"
  LISTENER_PID="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^p[0-9]+$/ { pid = substr($1, 2) } /^n/ { print pid; exit }')"
  LISTENER_ADDR="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { print substr($1, 2); exit }')"
  LISTENER_COUNT="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { count += 1 } END { print count + 0 }')"
  CURRENT_HEALTH="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
  OWNER_AVAILABLE=0
  if owner_available; then OWNER_AVAILABLE=1; fi
  [[ "$LISTENER_COUNT" -eq 1 && "$LISTENER_PID" == "$OLD_PID" \
    && "$LISTENER_ADDR" == "127.0.0.1:8769" && "$OWNER_AVAILABLE" -eq 0 ]] \
    && managed_identity_matches "$OLD_PID" "$CURRENT_CWD" "$LISTENER_ADDR" \
      "$CURRENT_HEALTH" "$CURRENT_RECORD_JSON" \
    || fail "managed launchd identity is not verified"
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$LABEL"
else
  LABEL_STATUS=$?
  [[ "$LABEL_STATUS" -ne 0 && "$LABEL_OUTPUT" == *"Could not find service"* ]] \
    || fail "failed to inspect launchd label: $LABEL"
  listener_absent || fail "unknown listener on 8769"
  owner_available || fail "prediction runtime owner is held by an unknown process"
fi

wait_agent_absent
wait_listener_absent
[[ -z "$OLD_PID" ]] || pid_absent "$OLD_PID" \
  || fail "failed to prove prediction service PID absence: $OLD_PID"
owner_available || fail "prediction runtime owner is still held"
if [[ -e "$PLIST_PATH" || -L "$PLIST_PATH" ]]; then rm "$PLIST_PATH"; fi

PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - \
  "$RUNTIME_RECORD" "$CURRENT_RECORD_JSON" <<'PY'
from datetime import datetime
from pathlib import Path
import json
import sys
from open_trader.prediction_release import load_prediction_runtime_record, write_prediction_runtime_record
path = Path(sys.argv[1])
record = load_prediction_runtime_record(path)
if record != json.loads(sys.argv[2]):
    raise ValueError("prediction runtime record changed during uninstall")
write_prediction_runtime_record(path, {
    **record,
    "state": "stopped",
    "failure_reason": "",
    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
})
PY
echo "stopped managed prediction release: $LABEL"
