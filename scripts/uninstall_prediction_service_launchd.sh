#!/usr/bin/env bash
set -euo pipefail

MODE="shadow"
RUNTIME_ROOT=""
MANAGER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$MANAGER_ROOT"
MANAGER_SRC="$MANAGER_ROOT/src"
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
PYTHON_OVERRIDE=0
[[ -n "${OPEN_TRADER_PYTHON:-}" ]] && PYTHON_OVERRIDE=1
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
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; PYTHON_OVERRIDE=1; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { echo "missing value for --launch-agents-dir" >&2; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$MODE" == "shadow" || "$MODE" == "production" ]] || { usage; exit 2; }
if [[ "$PYTHON_OVERRIDE" -eq 0 && ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] \
  || fail "python interpreter is unavailable: $PYTHON_BIN"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
OPERATION_LOCK_PATH="$LAUNCH_AGENTS_DIR/.$LABEL.release.lock"
# Keep command/shell overhead inside the validator's existing 30-second cleanup reserve.
CLEANUP_POLL_BUDGET=20

OPERATION_LOCK_HELD=0
cleanup_operation_lock() {
  if [[ "$OPERATION_LOCK_HELD" -eq 1 ]]; then
    exec 9>&-
    OPERATION_LOCK_HELD=0
  fi
}

start_operation_lock() {
  mkdir -p "$LAUNCH_AGENTS_DIR" \
    || fail "could not prepare release operation lock directory"
  if ! exec 9<>"$OPERATION_LOCK_PATH"; then
    fail "could not prepare release operation lock"
  fi
  local status
  if "$PYTHON_BIN" -c '
import fcntl
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(73)
except OSError:
    raise SystemExit(74)
' 9>&9; then
    :
  else
    status=$?
    exec 9>&-
    if [[ "$status" -eq 73 ]]; then
      fail "release operation already in progress"
    fi
    fail "could not establish release operation exclusion"
  fi
  OPERATION_LOCK_HELD=1
  trap cleanup_operation_lock EXIT
}

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

shadow_production_conflict() {
  local output args mode health
  if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
    args="$("$PYTHON_BIN" - "$output" <<'PY'
import json
import sys
raw = sys.argv[1]
values = []
in_arguments = False
for line in raw.splitlines():
    item = line.strip()
    if item == "arguments = {":
        in_arguments = True
    elif in_arguments and item == "}":
        in_arguments = False
    elif in_arguments and item:
        values.append(item.strip('"'))
print(json.dumps(values))
PY
)"
    mode="$("$PYTHON_BIN" - "$args" <<'PY'
import json
import sys
args = json.loads(sys.argv[1])
values = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "--mode"]
print(values[0] if len(values) == 1 else "")
PY
)"
    if [[ "$mode" == "production" ]]; then
      echo "shadow command refused: managed production service is active"
      return 0
    fi
  fi
  if [[ -e "$PLIST_PATH" || -L "$PLIST_PATH" ]]; then
    mode="$("$PYTHON_BIN" - "$PLIST_PATH" <<'PY'
import plistlib
import sys
try:
    args = plistlib.loads(open(sys.argv[1], "rb").read()).get("ProgramArguments", [])
    values = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "--mode"]
    print(values[0] if len(values) == 1 else "")
except (OSError, TypeError, ValueError):
    print("")
PY
)"
    if [[ "$mode" == "production" ]]; then
      echo "shadow command refused: managed production plist is active"
      return 0
    fi
  fi
  health="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
  if "$PYTHON_BIN" - "$health" <<'PY'
import json
import sys
try:
    health = json.loads(sys.argv[1])
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if health.get("mode") == "production" or health.get("production_owner") is True else 1)
PY
  then
    echo "shadow command refused: production health is active"
    return 0
  fi
  return 1
}

start_operation_lock

if [[ "$MODE" == "shadow" ]]; then
  if shadow_reason="$(shadow_production_conflict)"; then
    fail "$shadow_reason"
  fi
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
CURRENT_RECORD_JSON="$(PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY'
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
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$DATA_DIR" <<'PY'
from pathlib import Path
import sys
from open_trader.prediction_runtime import _RuntimeOwnershipLock
lock = _RuntimeOwnershipLock(Path(sys.argv[1]) / "prediction_arbitrage" / "runtime.lock")
lock.acquire()
lock.release()
PY
}

label_field() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json
import sys

raw, field = sys.argv[1:]
facts = {"path": "", "working_directory": "", "stdout": "", "stderr": "", "pid": "", "arguments": []}
in_arguments = False
for line in raw.splitlines():
    stripped = line.strip()
    if stripped == "arguments = {":
        in_arguments = True
        continue
    if in_arguments:
        if stripped == "}":
            in_arguments = False
        elif stripped:
            if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
                stripped = stripped[1:-1]
            facts["arguments"].append(stripped)
        continue
    if " = " not in stripped:
        continue
    key, value = stripped.split(" = ", 1)
    value = value.strip().strip('"')
    field_name = {
        "path": "path", "working directory": "working_directory",
        "stdout path": "stdout", "stderr path": "stderr", "pid": "pid",
    }.get(key)
    if field_name:
        facts[field_name] = value
value = facts.get(field, "")
print(json.dumps(value) if field == "arguments" else value)
PY
}

release_manifest_argument() {
  local arguments="$1"
  "$PYTHON_BIN" - "$arguments" <<'PY'
import json
import sys
args = json.loads(sys.argv[1])
values = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "--release-manifest"]
print(values[0] if len(values) == 1 else "")
PY
}

lock_owner_pids() {
  local lock_path="$DATA_DIR/prediction_arbitrage/runtime.lock"
  "$LSOF_BIN" -nP -Fpkfn "$lock_path" 2>/dev/null \
    | awk -v expected="$lock_path" '
      /^p[1-9][0-9]*$/ { pid = substr($0, 2); next }
      /^n/ {
        if (pid != "" && substr($0, 2) == expected) print pid
        pid = ""
      }
    ' | sort -n -u | paste -sd, -
}

managed_identity_error() {
  local facts_json="$1" health_raw="$2" release_manifest="$3" lock_pids="$4"
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$facts_json" "$health_raw" \
    "$release_manifest" "$lock_pids" "$PLIST_PATH" "$DATA_DIR" <<'PY'
import json
import sys
from open_trader.prediction_release import managed_release_identity_error

facts_raw, health_raw, release_manifest, lock_pids, expected_plist, data_dir = sys.argv[1:]
facts = json.loads(facts_raw)
try:
    health = json.loads(health_raw)
except (TypeError, ValueError, json.JSONDecodeError):
    health = None
facts.update({
    "expected_plist_path": expected_plist,
    "health": health,
    "release_manifest": release_manifest,
    "expected_data_dir": data_dir,
    "lock_owner_pids": [int(item) for item in lock_pids.split(",") if item],
})
print(managed_release_identity_error(facts) or "")
PY
}

observed_release_json() {
  local cwd="$1" manifest="$2"
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$cwd" "$manifest" <<'PY'
import json
import sys
from pathlib import Path
from open_trader.prediction_release import inspect_prediction_release_checkout
try:
    print(json.dumps(inspect_prediction_release_checkout(
        Path(sys.argv[1]), Path(sys.argv[2]) if sys.argv[2] else None,
    ), separators=(",", ":")))
except ValueError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
PY
}

archive_record() {
  local reason="$1"
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RUNTIME_RECORD" "$RUNTIME_ROOT/audit" "$reason" <<'PY'
import sys
from pathlib import Path
from open_trader.prediction_release import archive_prediction_runtime_record
archive_prediction_runtime_record(Path(sys.argv[1]), Path(sys.argv[2]), reason=sys.argv[3])
PY
}

ready_evidence() {
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$1" "$2" "$3" "$4" "$5" "$6" <<'PY'
import json
import sys
from open_trader.prediction_release import ready_evidence_from_health
health = json.loads(sys.argv[1])
print(json.dumps(ready_evidence_from_health(
    health, pid=int(sys.argv[2]), cwd=sys.argv[3], listener=sys.argv[4],
    stdout=sys.argv[5], stderr=sys.argv[6],
), separators=(",", ":")))
PY
}

record_ready_valid() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json
import sys
try:
    record = json.loads(sys.argv[1])
    ready = record.get("ready")
    valid = (
        record.get("state") in {"ready", "stopped"}
        and isinstance(record.get("candidate"), dict)
        and isinstance(ready, dict)
        and type(ready.get("reader_generation")) is int
        and type(ready.get("contract_generation")) is int
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

record_snapshot_matches() {
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RUNTIME_RECORD" "$CURRENT_RECORD_JSON" <<'PY'
import json
import sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record
current = load_prediction_runtime_record(Path(sys.argv[1]))
expected = json.loads(sys.argv[2])
raise SystemExit(0 if current == expected else 1)
PY
}

recheck_managed_identity_before_uninstall() {
  local output fresh_path fresh_cwd fresh_pid fresh_args_json
  local fresh_listener_output fresh_listener_status fresh_listener_pid fresh_listener_addr fresh_listener_count
  local fresh_health fresh_process_cwd fresh_lock_pids fresh_owner_available fresh_facts_json
  if ! output="$($LAUNCHCTL_BIN print "gui/$UID/$LABEL" 2>&1)"; then
    return 1
  fi
  fresh_path="$(label_field "$output" path)"
  fresh_cwd="$(label_field "$output" working_directory)"
  fresh_pid="$(label_field "$output" pid)"
  fresh_args_json="$(label_field "$output" arguments)"
  [[ "$fresh_path" == "$PLIST_PATH" && "$fresh_pid" == "$OLD_PID" ]] || return 1
  fresh_process_cwd="$($LSOF_BIN -a -p "$fresh_pid" -d cwd -Fn 2>/dev/null \
    | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
  [[ "$fresh_cwd" == "$CURRENT_CWD" && "$fresh_process_cwd" == "$CURRENT_CWD" ]] || return 1
  if fresh_listener_output="$($LSOF_BIN -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
    fresh_listener_status=0
  else
    fresh_listener_status=$?
  fi
  [[ "$fresh_listener_status" -eq 0 ]] || return 1
  fresh_listener_pid="$(printf '%s\n' "$fresh_listener_output" | awk '/^p[0-9]+$/ { pid = substr($1, 2) } /^n/ { print pid; exit }')"
  fresh_listener_addr="$(printf '%s\n' "$fresh_listener_output" | awk '/^n/ { print substr($1, 2); exit }')"
  fresh_listener_count="$(printf '%s\n' "$fresh_listener_output" | awk '/^n/ { count += 1 } END { print count + 0 }')"
  [[ "$fresh_listener_pid" == "$OLD_PID" && "$fresh_listener_addr" == "$LISTENER_ADDR" \
    && "$fresh_listener_count" == "$LISTENER_COUNT" ]] || return 1
  fresh_health="$($CURL_BIN -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
  [[ -n "$fresh_health" ]] || return 1
  if ! "$PYTHON_BIN" - "$CURRENT_HEALTH" "$fresh_health" <<'PY'
import json
import sys
try:
    first, second = map(json.loads, sys.argv[1:])
    first.pop("http_load", None)
    second.pop("http_load", None)
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if first == second else 1)
PY
  then
    return 1
  fi
  fresh_owner_available=0
  if owner_available; then fresh_owner_available=1; fi
  [[ "$fresh_owner_available" -eq 0 ]] || return 1
  fresh_lock_pids="$(lock_owner_pids || true)"
  [[ "$fresh_lock_pids" == "$LOCK_OWNER_PIDS" ]] || return 1
  fresh_facts_json="$($PYTHON_BIN \
    - "$fresh_path" "$fresh_cwd" "$fresh_pid" "$fresh_process_cwd" \
    "$fresh_listener_pid" "$fresh_listener_addr" "$fresh_listener_count" "$fresh_args_json" <<'PY'
import json
import sys
label_path, label_cwd, pid, process_cwd, listener_pid, listener_addr, listener_count, arguments = sys.argv[1:]
print(json.dumps({
    "label_path": label_path,
    "launchd_cwd": label_cwd,
    "pid": pid,
    "process_cwd": process_cwd,
    "listener_pid": listener_pid,
    "listener_addr": listener_addr,
    "listener_count": int(listener_count),
    "arguments": json.loads(arguments),
}, separators=(",", ":")))
PY
  )"
  [[ -z "$(managed_identity_error \
    "$fresh_facts_json" "$fresh_health" "$LIVE_RELEASE_MANIFEST" "$fresh_lock_pids")" ]] || return 1
  record_snapshot_matches
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

LABEL_OUTPUT=""
OLD_PID=""
OBSERVED_RELEASE_FOR_RECORD="null"
OBSERVED_READY_JSON="null"
RECOVERY_REASON=""
if LABEL_OUTPUT="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
  LABEL_PATH="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "path" && $2 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
  OLD_PID="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ { print $3; exit }')"
  [[ "$LABEL_PATH" == "$PLIST_PATH" && -n "$OLD_PID" ]] \
    || fail "managed launchd identity is not verified"
  LABEL_CWD="$(label_field "$LABEL_OUTPUT" working_directory)"
  LABEL_ARGUMENTS_JSON="$(label_field "$LABEL_OUTPUT" arguments)"
  LIVE_RELEASE_MANIFEST="$(release_manifest_argument "$LABEL_ARGUMENTS_JSON")"
  CURRENT_CWD="$("$LSOF_BIN" -a -p "$OLD_PID" -d cwd -Fn 2>/dev/null \
    | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
  if LISTENER_OUTPUT="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
    LISTENER_STATUS=0
  else
    LISTENER_STATUS=$?
  fi
  [[ "$LISTENER_STATUS" -eq 0 || ("$LISTENER_STATUS" -eq 1 && -z "$LISTENER_OUTPUT") ]] \
    || fail "failed to inspect listener on 8769"
  LISTENER_PID="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^p[0-9]+$/ { pid = substr($1, 2) } /^n/ { print pid; exit }')"
  LISTENER_ADDR="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { print substr($1, 2); exit }')"
  LISTENER_COUNT="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { count += 1 } END { print count + 0 }')"
  CURRENT_HEALTH="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
  OWNER_AVAILABLE=0
  if owner_available; then OWNER_AVAILABLE=1; fi
  LOCK_OWNER_PIDS="$(lock_owner_pids || true)"
  LABEL_FACTS_JSON="$($PYTHON_BIN \
    - "$LABEL_PATH" "$LABEL_CWD" "$OLD_PID" "$CURRENT_CWD" \
    "$LISTENER_PID" "$LISTENER_ADDR" "$LISTENER_COUNT" "$LABEL_ARGUMENTS_JSON" <<'PY'
import json
import sys
label_path, label_cwd, pid, process_cwd, listener_pid, listener_addr, listener_count, arguments = sys.argv[1:]
print(json.dumps({
    "label_path": label_path,
    "launchd_cwd": label_cwd,
    "pid": pid,
    "process_cwd": process_cwd,
    "listener_pid": listener_pid,
    "listener_addr": listener_addr,
    "listener_count": int(listener_count),
    "arguments": json.loads(arguments),
}, separators=(",", ":")))
PY
  )"
  IDENTITY_REASON="$(managed_identity_error \
    "$LABEL_FACTS_JSON" "$CURRENT_HEALTH" "$LIVE_RELEASE_MANIFEST" "$LOCK_OWNER_PIDS")"
  [[ "$LISTENER_STATUS" -eq 0 && "$OWNER_AVAILABLE" -eq 0 && -z "$IDENTITY_REASON" ]] \
    || fail "managed launchd identity is not verified${IDENTITY_REASON:+: $IDENTITY_REASON}"
  record_ready_valid || fail "managed launchd identity is not verified"
  if ! OBSERVED_RELEASE_JSON="$(observed_release_json "$CURRENT_CWD" "$LIVE_RELEASE_MANIFEST")"; then
    fail "managed launchd identity is not verified: live release checkout is not independently verified"
  fi
  HEALTH_RELEASE_REASON="$($PYTHON_BIN - "$CURRENT_HEALTH" "$OBSERVED_RELEASE_JSON" <<'PY'
import json
import sys
health = json.loads(sys.argv[1])
release = json.loads(sys.argv[2])
if health.get("git_sha") != release.get("git_sha"):
    print("managed health SHA differs from checked release")
elif health.get("reader_generation") != release.get("reader_generation"):
    print("managed health reader generation differs from checked release")
elif health.get("contract_generation") != release.get("contract_generation"):
    print("managed health contract generation differs from checked release")
PY
  )"
  [[ -z "$HEALTH_RELEASE_REASON" ]] \
    || fail "managed launchd identity is not verified: $HEALTH_RELEASE_REASON"
  OBSERVED_RELEASE_FOR_RECORD="$($PYTHON_BIN - "$OBSERVED_RELEASE_JSON" <<'PY'
import json
import sys
release = json.loads(sys.argv[1])
release.pop("manifest", None)
print(json.dumps(release, separators=(",", ":")))
PY
  )"
  OBSERVED_READY_JSON="$(ready_evidence "$CURRENT_HEALTH" "$OLD_PID" "$CURRENT_CWD" \
    "$LISTENER_ADDR" "$(label_field "$LABEL_OUTPUT" stdout)" \
    "$(label_field "$LABEL_OUTPUT" stderr)")"
  RECORD_CANDIDATE_MATCHES=0
  RECORD_READY_MATCHES=0
  if "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$OBSERVED_RELEASE_FOR_RECORD" <<'PY'
import json, sys
record, observed = map(json.loads, sys.argv[1:])
raise SystemExit(0 if record.get("candidate") == observed else 1)
PY
  then RECORD_CANDIDATE_MATCHES=1; fi
  if "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$OBSERVED_READY_JSON" <<'PY'
import json, sys
record, observed = map(json.loads, sys.argv[1:])
raise SystemExit(0 if record.get("ready") == observed else 1)
PY
  then RECORD_READY_MATCHES=1; fi
  if [[ "$RECORD_CANDIDATE_MATCHES" -eq 0 ]]; then
    RECOVERY_REASON="stale runtime record recovered from verified live release"
    archive_record "$RECOVERY_REASON" \
      || fail "prediction runtime record recovery archive could not be written"
  elif [[ "$RECORD_READY_MATCHES" -eq 0 ]]; then
    RECOVERY_REASON="managed restart observation refreshed"
  fi
  recheck_managed_identity_before_uninstall \
    || fail "managed release evidence changed before uninstall"
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

PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - \
  "$RUNTIME_RECORD" "$CURRENT_RECORD_JSON" "$OBSERVED_RELEASE_FOR_RECORD" \
  "$OBSERVED_READY_JSON" "$RECOVERY_REASON" <<'PY'
from datetime import datetime
from pathlib import Path
import json
import sys
from open_trader.prediction_release import load_prediction_runtime_record, write_prediction_runtime_record
path, current_raw, observed_raw, ready_raw, recovery_reason = sys.argv[1:]
record = load_prediction_runtime_record(path)
if record != json.loads(current_raw):
    raise ValueError("prediction runtime record changed during uninstall")
if observed_raw != "null":
    record["candidate"] = json.loads(observed_raw)
if ready_raw != "null":
    record["ready"] = json.loads(ready_raw)
if recovery_reason:
    record["recovery_reason"] = recovery_reason
write_prediction_runtime_record(path, {
    **record,
    "state": "stopped",
    "failure_reason": "",
    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
})
PY
echo "stopped managed prediction release: $LABEL"
