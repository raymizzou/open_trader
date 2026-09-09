#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
PREFLIGHT=0
MODE="shadow"
MANAGER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$MANAGER_ROOT"
MANAGER_SRC="$MANAGER_ROOT/src"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
CONFIG=""
NOTIFIER_CONFIG=""
RELEASE_MANIFEST=""
EXPECTED_SHA=""
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
PS_BIN="${PS_BIN:-/bin/ps}"
WAIT_SECONDS="${PREDICTION_SERVICE_LAUNCHD_WAIT_SECONDS:-90}"
LABEL="com.open-trader.prediction-service"

usage() {
  echo "usage: $0 --runtime-root PATH [--dry-run] [--preflight] [--mode shadow|production] [--repo-root PATH] [--python PATH] [--config PATH] [--notifier-config PATH] [--launch-agents-dir PATH] [--wait-seconds N] [--release-manifest PATH] [--expected-sha SHA]" >&2
}

fail() {
  echo "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --preflight) PREFLIGHT=1; shift ;;
    --mode) [[ $# -ge 2 ]] || { usage; exit 2; }; MODE="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --config) [[ $# -ge 2 ]] || { usage; exit 2; }; CONFIG="$2"; shift 2 ;;
    --notifier-config) [[ $# -ge 2 ]] || { usage; exit 2; }; NOTIFIER_CONFIG="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    --release-manifest) [[ $# -ge 2 ]] || { usage; exit 2; }; RELEASE_MANIFEST="$2"; shift 2 ;;
    --expected-sha) [[ $# -ge 2 ]] || { usage; exit 2; }; EXPECTED_SHA="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "$RUNTIME_ROOT" && "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
[[ "$MODE" == "shadow" || "$MODE" == "production" ]] || { usage; exit 2; }
[[ "$PREFLIGHT" -eq 0 || "$MODE" == "production" ]] || { usage; exit 2; }

resolve_path() {
  "$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1"
}

REPO_ROOT="$(resolve_path "$REPO_ROOT")"
RUNTIME_ROOT="$(resolve_path "$RUNTIME_ROOT")"
RELEASE_MANIFEST="${RELEASE_MANIFEST:-$REPO_ROOT/ops/prediction-service-release.json}"
RELEASE_MANIFEST="$(resolve_path "$RELEASE_MANIFEST")"
CONFIG="${CONFIG:-$RUNTIME_ROOT/config/prediction_arbitrage.json}"
NOTIFIER_CONFIG="${NOTIFIER_CONFIG:-$RUNTIME_ROOT/config/daily_premarket.env}"
NOTIFIER_CONFIG="$(resolve_path "$NOTIFIER_CONFIG")"
TEMPLATE="$REPO_ROOT/ops/launchd/$LABEL.plist.template"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
OPERATION_LOCK_PATH="$LAUNCH_AGENTS_DIR/.$LABEL.release.lock"
DATA_DIR="$RUNTIME_ROOT/data"
LOCK_PATH="$DATA_DIR/prediction_arbitrage/runtime.lock"
LOG_DIR="$RUNTIME_ROOT/logs/prediction_service"
OUT_LOG="$LOG_DIR/launchd.out.log"
ERR_LOG="$LOG_DIR/launchd.err.log"
RUNTIME_RECORD="$RUNTIME_ROOT/prediction-service-runtime.json"
[[ -f "$TEMPLATE" ]] || fail "missing launchd template: $TEMPLATE"

sed_escape() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_plist() {
  sed \
    -e "s|OPEN_TRADER_PYTHON|$(sed_escape "$PYTHON_BIN")|g" \
    -e "s|OPEN_TRADER_DATA_DIR|$(sed_escape "$DATA_DIR")|g" \
    -e "s|OPEN_TRADER_PREDICTION_CONFIG|$(sed_escape "$CONFIG")|g" \
    -e "s|OPEN_TRADER_NOTIFIER_CONFIG|$(sed_escape "$NOTIFIER_CONFIG")|g" \
    -e "s|OPEN_TRADER_PREDICTION_MODE|$(sed_escape "$MODE")|g" \
    -e "s|OPEN_TRADER_RELEASE_MANIFEST|$(sed_escape "$RELEASE_MANIFEST")|g" \
    -e "s|OPEN_TRADER_RUNTIME_ROOT|$(sed_escape "$RUNTIME_ROOT")|g" \
    -e "s|OPEN_TRADER_REPO|$(sed_escape "$REPO_ROOT")|g" \
    "$TEMPLATE"
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
  "$LSOF_BIN" -nP -Fpkfn "$LOCK_PATH" 2>/dev/null \
    | awk -v expected="$LOCK_PATH" '
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
    print(json.dumps(inspect_prediction_release_checkout(Path(sys.argv[1]), Path(sys.argv[2]) if sys.argv[2] else None), separators=(",", ":")))
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

preflight_json() {
  local status="$1" reason="$2" differences_json="${3:-[]}" observed_json="${4:-null}"
  local recorded_json="${5:-null}" observed_ready_json="${6:-null}"
  "$PYTHON_BIN" - "$status" "$reason" "$differences_json" "$observed_json" \
    "$recorded_json" "$observed_ready_json" <<'PY'
import json
import sys
status, reason, differences, observed, recorded, observed_ready = sys.argv[1:]
payload = {
    "schema_version": "open_trader.prediction_service.preflight.v1",
    "status": status,
    "reason": reason,
    "differences": json.loads(differences),
}
if observed != "null":
    payload["observed_release"] = json.loads(observed)
if recorded != "null":
    record = json.loads(recorded)
    if isinstance(record, dict):
        candidate = record.get("candidate")
        if isinstance(candidate, dict):
            payload["recorded_release"] = {
                key: candidate[key] for key in (
                    "checkout", "git_sha", "source_state",
                    "reader_generation", "contract_generation",
                ) if key in candidate
            }
        ready = record.get("ready")
        if isinstance(ready, dict):
            payload["recorded_ready"] = {
                key: ready[key] for key in ("pid", "process_started_at")
                if key in ready
            }
if observed_ready != "null":
    ready = json.loads(observed_ready)
    if isinstance(ready, dict):
        payload["observed_ready"] = {
            key: ready[key] for key in ("pid", "process_started_at")
            if key in ready
        }
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
}

preflight_fail() {
  local reason="$1"
  if [[ "$PREFLIGHT" -eq 1 ]]; then
    preflight_json "BLOCKED" "$reason"
    exit 1
  fi
  fail "$reason"
}

operation_lock_busy_readonly() {
  "$PYTHON_BIN" - "$OPERATION_LOCK_PATH" <<'PY'
import fcntl
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
try:
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
except OSError:
    raise SystemExit(2)
raise SystemExit(1)
PY
}

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

lint_plist() {
  local temp
  temp="$(mktemp "${TMPDIR:-/tmp}/open-trader-prediction-service.XXXXXX")"
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

shadow_health_matches() {
  "$PYTHON_BIN" -c '
import json, sys
expected_pid, expected_cwd, expected_sha, payload = sys.argv[1:]
try:
    health = json.loads(payload)
    valid = (
        health.get("schema_version") == "open_trader.prediction_service.health.v1"
        and health.get("module") == "prediction_service"
        and health.get("status") == "running"
        and health.get("mode") == "shadow"
        and health.get("production_owner") is False
        and health.get("mutations") == "prohibited"
        and health.get("pid") == int(expected_pid)
        and health.get("cwd") == expected_cwd
        and health.get("git_sha") == expected_sha
    )
except (TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
' "$1" "$2" "$3" "$4"
}

process_cwd_matches() {
  "$LSOF_BIN" -a -p "$1" -d cwd -Fn 2>/dev/null | awk -v expected="$REPO_ROOT" '
    $1 ~ /^n/ { found = 1; if (substr($1, 2) == expected) matched = 1 }
    END { exit !(found && matched) }
  '
}

loopback_listener_matches() {
  "$LSOF_BIN" -nP -a -p "$1" -iTCP:8769 -sTCP:LISTEN -Fn 2>/dev/null | awk '
    $1 ~ /^n/ { count += 1; if ($1 != "n127.0.0.1:8769") invalid = 1 }
    END { exit !(count == 1 && !invalid) }
  '
}

listener_absent() {
  local output status
  if output="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
    [[ -z "$output" ]]
    return
  else
    status=$?
  fi
  [[ "$status" -eq 1 && -z "$output" ]]
}

pid_absent() {
  local output status
  if output="$("$PS_BIN" -p "$1" 2>&1)"; then
    return 1
  else
    status=$?
  fi
  [[ "$status" -eq 1 ]] && return 0
  printf '%s\n' "$output" >&2
  return "$status"
}

owner_available() {
  if [[ "$PREFLIGHT" -eq 1 ]]; then
    if [[ -n "${OWNER_PROBE_BIN:-}" ]]; then
      "$OWNER_PROBE_BIN" "$DATA_DIR"
      return
    fi
    # The normal runtime lock acquisition creates missing parent paths. A
    # preflight must remain read-only, so inspect only the existing lock path.
    if [[ ! -e "$LOCK_PATH" ]]; then
      return 0
    fi
    "$PYTHON_BIN" - "$LOCK_PATH" <<'PY'
import fcntl
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)
try:
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(1)
except OSError:
    raise SystemExit(2)
raise SystemExit(0)
PY
  fi
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

remove_managed_plist() {
  if [[ -e "$PLIST_PATH" || -L "$PLIST_PATH" ]]; then
    rm "$PLIST_PATH"
  fi
}

wait_shadow_ready() {
  local expected_sha attempt output pid health alive=0
  expected_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1 || true)"
    pid="$(printf '%s\n' "$output" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }')"
    [[ -n "$pid" ]] && alive=1
    if [[ -n "$pid" ]] && process_cwd_matches "$pid" \
      && loopback_listener_matches "$pid" \
      && health="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null)" \
      && shadow_health_matches "$pid" "$REPO_ROOT" "$expected_sha" "$health"; then
      return 0
    fi
    sleep 1
  done
  if [[ "$alive" -eq 1 ]]; then
    echo "Prediction Service shadow health not confirmed within ${WAIT_SECONDS}s; job left running" >&2
    return 1
  fi
  bootout_if_loaded || true
  wait_agent_absent || return 1
  echo "Prediction Service did not start (no process bound to 8769)" >&2
  return 1
}

shadow_production_conflict() {
  local output args mode health
  if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
    args="$(label_field "$output" arguments)"
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
except (OSError, ValueError, TypeError):
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

MANIFEST_JSON=""
ACTUAL_SHA=""
CANDIDATE_JSON=""
if [[ "$MODE" == "production" ]]; then
  ACTUAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  SOURCE_STATUS=""
  if ! SOURCE_STATUS="$(git -C "$REPO_ROOT" status --porcelain)"; then
    preflight_fail "failed to inspect release root: $REPO_ROOT"
  fi
  [[ -z "$SOURCE_STATUS" ]] || preflight_fail "release root is dirty: $REPO_ROOT"
  [[ -z "$EXPECTED_SHA" || "$EXPECTED_SHA" == "$ACTUAL_SHA" ]] \
    || preflight_fail "requested SHA does not match checkout"
  MANIFEST_RELATIVE=""
  if ! MANIFEST_RELATIVE="$("$PYTHON_BIN" - "$REPO_ROOT" "$RELEASE_MANIFEST" <<'PY'
from pathlib import Path
import sys
root, manifest = map(Path, sys.argv[1:])
try:
    print(manifest.relative_to(root).as_posix())
except ValueError:
    raise SystemExit(1)
PY
)"; then
    preflight_fail "release manifest must be tracked by checkout"
  fi
  git -C "$REPO_ROOT" ls-files --error-unmatch -- "$MANIFEST_RELATIVE" >/dev/null 2>&1 \
    || preflight_fail "release manifest must be tracked by checkout"
  git -C "$REPO_ROOT" cat-file -e "$ACTUAL_SHA:$MANIFEST_RELATIVE" 2>/dev/null \
    || preflight_fail "release manifest must be tracked by checkout"
  if ! MANIFEST_JSON="$(PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RELEASE_MANIFEST" <<'PY'
import json, sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_release_manifest
release = load_prediction_release_manifest(Path(sys.argv[1]))
print(json.dumps({
    "schema_version": release.schema_version,
    "reader_generation": release.reader_generation,
    "contract_generation": release.contract_generation,
}, separators=(",", ":")))
PY
)"; then
    preflight_fail "prediction release manifest is invalid or unreadable"
  fi
  CANDIDATE_JSON="$("$PYTHON_BIN" - "$REPO_ROOT" "$ACTUAL_SHA" "$MANIFEST_JSON" <<'PY'
import json, sys
checkout, git_sha, manifest = sys.argv[1:]
release = json.loads(manifest)
print(json.dumps({
    "checkout": checkout,
    "git_sha": git_sha,
    "source_state": "clean",
    "reader_generation": release["reader_generation"],
    "contract_generation": release["contract_generation"],
}, separators=(",", ":")))
PY
)"
fi

rendered="$(render_plist)"
lint_plist "$rendered"
if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

if [[ "$PREFLIGHT" -eq 1 ]]; then
  if operation_lock_busy_readonly; then
    preflight_json "BLOCKED" "release operation already in progress"
    exit 1
  else
    lock_status=$?
    if [[ "$lock_status" -ne 1 ]]; then
      preflight_json "BLOCKED" "could not inspect release operation lock"
      exit 1
    fi
  fi
elif [[ "$MODE" == "shadow" || "$MODE" == "production" ]]; then
  start_operation_lock
fi

if [[ "$MODE" == "shadow" ]]; then
  if shadow_reason="$(shadow_production_conflict)"; then
    fail "$shadow_reason"
  fi
  mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$DATA_DIR"
  printf '%s\n' "$rendered" > "$PLIST_PATH"
  bootout_if_loaded
  wait_agent_absent
  : > "$OUT_LOG"
  : > "$ERR_LOG"
  "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$PLIST_PATH"
  wait_shadow_ready
  echo "installed launchd agent: $LABEL"
  exit 0
fi

record_ready_valid() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json
import sys
try:
    record = json.loads(sys.argv[1])
    ready = record.get("ready") if isinstance(record, dict) else None
    valid = record is None or (
        isinstance(record, dict)
        and (
            ready is None
            or (
                isinstance(ready, dict)
                and type(ready.get("reader_generation")) is int
                and type(ready.get("contract_generation")) is int
            )
        )
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

if ! CURRENT_RECORD_JSON="$(PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY'
import json, sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record
record = load_prediction_runtime_record(Path(sys.argv[1]))
print("null" if record is None else json.dumps(record, separators=(",", ":")))
PY
)"; then
  if [[ "$PREFLIGHT" -eq 1 ]]; then
    preflight_json "BLOCKED" "prediction runtime record is malformed or unreadable"
    exit 1
  fi
  fail "prediction runtime record is malformed or unreadable"
fi
if ! record_ready_valid; then
  if [[ "$PREFLIGHT" -eq 1 ]]; then
    preflight_fail "prediction runtime record ready evidence is malformed or unreadable"
  fi
  fail "managed launchd identity is not verified"
fi

LABEL_LOADED=0
LABEL_OUTPUT=""
OLD_PID=""
if LABEL_OUTPUT="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
  LABEL_LOADED=1
  OLD_PID="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ { print $3; exit }')"
  [[ -n "$OLD_PID" ]] || preflight_fail "managed launchd identity is not verified: managed launchd service has no PID"
else
  LABEL_STATUS=$?
  if [[ "$LABEL_STATUS" -eq 0 || "$LABEL_OUTPUT" != *"Could not find service"* ]]; then
    preflight_fail "failed to inspect launchd label: $LABEL"
  fi
fi

LABEL_PATH=""
LABEL_CWD=""
LABEL_ARGUMENTS_JSON="[]"
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  LABEL_PATH="$(label_field "$LABEL_OUTPUT" path)"
  LABEL_CWD="$(label_field "$LABEL_OUTPUT" working_directory)"
  LABEL_ARGUMENTS_JSON="$(label_field "$LABEL_OUTPUT" arguments)"
fi
LIVE_RELEASE_MANIFEST="$(release_manifest_argument "$LABEL_ARGUMENTS_JSON")"

CURRENT_CWD=""
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  CURRENT_CWD="$("$LSOF_BIN" -a -p "$OLD_PID" -d cwd -Fn 2>/dev/null \
    | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
fi

LISTENER_OUTPUT=""
LISTENER_STATUS=0
if LISTENER_OUTPUT="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
  LISTENER_STATUS=0
else
  LISTENER_STATUS=$?
  if [[ "$LISTENER_STATUS" -ne 1 || -n "$LISTENER_OUTPUT" ]]; then
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      preflight_json "BLOCKED" "listener inspection is unavailable"
      exit 1
    fi
    fail "failed to inspect listener on 8769"
  fi
fi
LISTENER_PID="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '
  /^p[0-9]+$/ { pid = substr($1, 2) }
  /^n/ { print pid; exit }
')"
LISTENER_ADDR="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { print substr($1, 2); exit }')"
LISTENER_COUNT="$(printf '%s\n' "$LISTENER_OUTPUT" | awk '/^n/ { count += 1 } END { print count + 0 }')"

CURRENT_HEALTH=""
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  CURRENT_HEALTH="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
fi

OWNER_AVAILABLE=0
if owner_available; then OWNER_AVAILABLE=1; fi
LOCK_OWNER_PIDS=""
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  LOCK_OWNER_PIDS="$(lock_owner_pids || true)"
fi
LABEL_FACTS_JSON="$("$PYTHON_BIN" \
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

managed_identity_matches() {
  [[ -z "$(managed_identity_error \
    "$LABEL_FACTS_JSON" "$CURRENT_HEALTH" "$LIVE_RELEASE_MANIFEST" "$LOCK_OWNER_PIDS")" ]]
}

MANAGED_OLD=0
OBSERVED_RELEASE_JSON="null"
OBSERVED_READY_JSON="null"
OBSERVED_RECOVERY_REASON=""
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  [[ "$CURRENT_RECORD_JSON" != "null" ]] \
    || preflight_fail "managed launchd identity is not verified: prediction runtime record is required to recover a managed service"
  IDENTITY_REASON="$(managed_identity_error \
    "$LABEL_FACTS_JSON" "$CURRENT_HEALTH" "$LIVE_RELEASE_MANIFEST" "$LOCK_OWNER_PIDS")"
  if [[ "$LISTENER_STATUS" -eq 0 && "$OWNER_AVAILABLE" -eq 0 \
    && -z "$IDENTITY_REASON" ]]; then
    if [[ -z "$LIVE_RELEASE_MANIFEST" ]]; then
      if [[ "$PREFLIGHT" -eq 1 ]]; then
        preflight_json "BLOCKED" "managed launchd release manifest is not loaded"
        exit 1
      fi
      fail "managed launchd identity is not verified: release manifest is not loaded"
    fi
    if ! OBSERVED_RELEASE_JSON="$(observed_release_json "$CURRENT_CWD" "$LIVE_RELEASE_MANIFEST")"; then
      if [[ "$PREFLIGHT" -eq 1 ]]; then
        preflight_json "BLOCKED" "live release checkout is not independently verified"
        exit 1
      fi
      fail "managed launchd identity is not verified: live release checkout is not independently verified"
    fi
    HEALTH_RELEASE_REASON="$("$PYTHON_BIN" - "$CURRENT_HEALTH" "$OBSERVED_RELEASE_JSON" <<'PY'
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
    if [[ -n "$HEALTH_RELEASE_REASON" ]]; then
      if [[ "$PREFLIGHT" -eq 1 ]]; then
        preflight_json "BLOCKED" "$HEALTH_RELEASE_REASON"
        exit 1
      fi
      fail "managed launchd identity is not verified: $HEALTH_RELEASE_REASON"
    fi
    OBSERVED_READY_JSON="$(PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$CURRENT_HEALTH" "$OLD_PID" \
      "$CURRENT_CWD" "$LISTENER_ADDR" \
      "$(label_field "$LABEL_OUTPUT" stdout)" "$(label_field "$LABEL_OUTPUT" stderr)" <<'PY'
import json
import sys
from open_trader.prediction_release import ready_evidence_from_health
health = json.loads(sys.argv[1])
print(json.dumps(ready_evidence_from_health(
    health, pid=int(sys.argv[2]), cwd=sys.argv[3], listener=sys.argv[4],
    stdout=sys.argv[5], stderr=sys.argv[6],
), separators=(",", ":")))
PY
)"
    MANAGED_OLD=1
  else
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      preflight_json "BLOCKED" "${IDENTITY_REASON:-live listener or runtime ownership is not verified}"
      exit 1
    fi
    fail "managed launchd identity is not verified: ${IDENTITY_REASON:-live listener or runtime ownership is not verified}"
  fi
else
  if [[ "$LISTENER_STATUS" -eq 0 ]]; then
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      preflight_json "BLOCKED" "unknown listener on 8769"
      exit 1
    fi
    fail "unknown listener on 8769"
  fi
  if [[ "$OWNER_AVAILABLE" -ne 1 ]]; then
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      preflight_json "BLOCKED" "prediction runtime owner is held by an unknown process"
      exit 1
    fi
    fail "prediction runtime owner is held by an unknown process"
  fi
fi

OBSERVED_RELEASE_FOR_RECORD="$("$PYTHON_BIN" - "$OBSERVED_RELEASE_JSON" <<'PY'
import json
import sys
release = json.loads(sys.argv[1])
if isinstance(release, dict):
    release.pop("manifest", None)
else:
    release = {}
print(json.dumps(release, separators=(",", ":")))
PY
)"

record_matches_observed() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$OBSERVED_RELEASE_FOR_RECORD" <<'PY'
import json
import sys
try:
    record, observed = map(json.loads, sys.argv[1:])
    matches = record.get("state") == "ready" and record.get("candidate") == observed
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)
PY
}

record_ready_matches_observed() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$OBSERVED_READY_JSON" <<'PY'
import json
import sys
try:
    record, observed = map(json.loads, sys.argv[1:])
    ready = record.get("ready")
    matches = (
        isinstance(ready, dict)
        and type(ready.get("reader_generation")) is int
        and type(ready.get("contract_generation")) is int
        and ready == observed
    )
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)
PY
}

PREFLIGHT_STATUS="READY"
PREFLIGHT_REASON="no managed production owner requires recovery"
PREFLIGHT_DIFFERENCES="[]"
if [[ "$MANAGED_OLD" -eq 1 ]]; then
  if record_matches_observed; then
    if record_ready_matches_observed; then
      PREFLIGHT_REASON="managed release identity and current observation match"
    else
      PREFLIGHT_STATUS="RECOVERABLE"
      PREFLIGHT_REASON="managed release identity is unchanged but PID/start observation changed"
      PREFLIGHT_DIFFERENCES='["pid","process_started_at"]'
    fi
  else
    PREFLIGHT_STATUS="RECOVERABLE"
    PREFLIGHT_REASON="verified live release differs from the saved runtime record"
    PREFLIGHT_DIFFERENCES='["saved_release","observed_release"]'
  fi
fi

if [[ "$PREFLIGHT" -eq 1 ]]; then
  preflight_json "$PREFLIGHT_STATUS" "$PREFLIGHT_REASON" \
    "$PREFLIGHT_DIFFERENCES" "$OBSERVED_RELEASE_JSON" \
    "$CURRENT_RECORD_JSON" "$OBSERVED_READY_JSON"
  [[ "$PREFLIGHT_STATUS" != "BLOCKED" ]]
  exit 0
fi

if [[ "$MANAGED_OLD" -eq 1 \
  && "$CANDIDATE_JSON" == "$OBSERVED_RELEASE_FOR_RECORD" ]] && record_matches_observed \
  && record_ready_matches_observed; then
  echo "prediction release already ready: $ACTUAL_SHA"
  exit 0
fi

if [[ "$MANAGED_OLD" -eq 1 ]]; then
  PREVIOUS_JSON="$OBSERVED_RELEASE_FOR_RECORD"
else
  PREVIOUS_JSON="$("$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json, sys
record = json.loads(sys.argv[1])
previous = None
if isinstance(record, dict):
    if record.get("state") == "ready" and isinstance(record.get("candidate"), dict):
        previous = record["candidate"]
    elif isinstance(record.get("previous_release"), dict):
        previous = record["previous_release"]
print("null" if previous is None else json.dumps(previous, separators=(",", ":")))
PY
)"
fi
PREVIOUS_READY_JSON="$("$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json, sys
record = json.loads(sys.argv[1])
ready = record.get("ready") if isinstance(record, dict) and record.get("state") == "ready" else None
print("null" if not isinstance(ready, dict) else json.dumps(ready, separators=(",", ":")))
PY
)"
if [[ "$MANAGED_OLD" -eq 1 ]]; then
  PREVIOUS_READY_JSON="$OBSERVED_READY_JSON"
fi
TRANSITION_STARTED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))')"

write_record() {
  local state="$1" failure_reason="$2" ready_json="$3"
  if ! PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - \
    "$RUNTIME_RECORD" "$CURRENT_RECORD_JSON" "$state" "$CANDIDATE_JSON" "$PREVIOUS_JSON" \
    "$TRANSITION_STARTED_AT" "$failure_reason" "$ready_json" \
    "$OBSERVED_RECOVERY_REASON" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record, write_prediction_runtime_record
path, expected_raw, state, candidate, previous, started, failure, ready, recovery_reason = sys.argv[1:]
current = load_prediction_runtime_record(Path(path))
expected = None if expected_raw == "null" else json.loads(expected_raw)
if current != expected:
    raise ValueError("prediction runtime record changed during install")
payload = {
    "state": state,
    "candidate": json.loads(candidate),
    "previous_release": None if previous == "null" else json.loads(previous),
    "transition_started_at": started,
    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "failure_reason": failure,
}
if ready != "null":
    payload["ready"] = json.loads(ready)
if recovery_reason:
    payload["recovery_reason"] = recovery_reason
write_prediction_runtime_record(Path(path), payload)
PY
  then
    return 1
  fi
  CURRENT_RECORD_JSON="$(PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY'
import json
import sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record
record = load_prediction_runtime_record(Path(sys.argv[1]))
print("null" if record is None else json.dumps(record, separators=(",", ":")))
PY
)"
}

record_candidate_equals() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$1" <<'PY'
import json
import sys
try:
    record, expected = map(json.loads, sys.argv[1:])
    matches = isinstance(record, dict) and record.get("candidate") == expected
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)
PY
}

record_previous_json() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json
import sys
record = json.loads(sys.argv[1])
previous = record.get("previous_release") if isinstance(record, dict) else None
print("null" if not isinstance(previous, dict) else json.dumps(previous, separators=(",", ":")))
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

recheck_managed_identity_before_handoff() {
  local output status fresh_pid fresh_path fresh_cwd fresh_args_json
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

RECORD_CANDIDATE_OBSERVED=0
if [[ "$MANAGED_OLD" -eq 1 ]] && record_candidate_equals "$OBSERVED_RELEASE_FOR_RECORD"; then
  RECORD_CANDIDATE_OBSERVED=1
fi
TARGET_CANDIDATE_OBSERVED=0
if [[ "$MANAGED_OLD" -eq 1 ]] && [[ "$CANDIDATE_JSON" == "$OBSERVED_RELEASE_FOR_RECORD" ]]; then
  TARGET_CANDIDATE_OBSERVED=1
fi
if [[ "$MANAGED_OLD" -eq 1 && "$RECORD_CANDIDATE_OBSERVED" -eq 0 ]]; then
  OBSERVED_RECOVERY_REASON="stale runtime record recovered from verified live release"
  archive_record "$OBSERVED_RECOVERY_REASON" \
    || fail "prediction runtime record recovery archive could not be written"
fi
if [[ "$TARGET_CANDIDATE_OBSERVED" -eq 1 ]]; then
  RECORD_STATE="$("$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json
import sys
record = json.loads(sys.argv[1])
print(record.get("state", "") if isinstance(record, dict) else "")
PY
)"
  if [[ "$RECORD_CANDIDATE_OBSERVED" -eq 0 ]]; then
    PREVIOUS_JSON="null"
  elif [[ "$RECORD_STATE" != "ready" ]]; then
    PREVIOUS_JSON="$(record_previous_json)"
    OBSERVED_RECOVERY_REASON="interrupted install finalized from observed candidate"
  else
    PREVIOUS_JSON="$(record_previous_json)"
    OBSERVED_RECOVERY_REASON="managed restart observation refreshed"
  fi
  if ! recheck_managed_identity_before_handoff; then
    fail "managed release evidence changed before recovery publication"
  fi
  if ! write_record ready "" "$OBSERVED_READY_JSON"; then
    fail "prediction release recovery failed: runtime_record_write_failed"
  fi
  echo "recovered managed prediction release: $ACTUAL_SHA"
  exit 0
fi

record_failed_and_exit() {
  local reason="$1"
  if ! write_record failed "$reason" null; then
    echo "prediction release failed and the failed runtime record could not be written" >&2
  fi
  echo "prediction release failed: $reason; see $ERR_LOG" >&2
  exit 1
}

setup_failed_and_exit() {
  local reason="$1"
  remove_managed_plist || reason="candidate_cleanup_not_proven"
  record_failed_and_exit "$reason"
}

write_record maintenance "" "$PREVIOUS_READY_JSON" \
  || record_failed_and_exit "runtime_record_write_failed"

if [[ "$MANAGED_OLD" -eq 1 ]]; then
  recheck_managed_identity_before_handoff \
    || fail "managed release evidence changed before handoff"
  bootout_if_loaded \
    || record_failed_and_exit "candidate_cleanup_not_proven"
fi
wait_agent_absent \
  || record_failed_and_exit "candidate_cleanup_not_proven"
if [[ -n "$OLD_PID" ]] && ! pid_absent "$OLD_PID"; then
  record_failed_and_exit "candidate_cleanup_not_proven"
fi
listener_absent \
  || record_failed_and_exit "candidate_cleanup_not_proven"
owner_available \
  || record_failed_and_exit "candidate_cleanup_not_proven"
remove_managed_plist \
  || record_failed_and_exit "candidate_cleanup_not_proven"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$DATA_DIR" \
  || setup_failed_and_exit "candidate_exited"
printf '%s\n' "$rendered" > "$PLIST_PATH" \
  || setup_failed_and_exit "candidate_exited"
: > "$OUT_LOG" \
  || setup_failed_and_exit "candidate_exited"
: > "$ERR_LOG" \
  || setup_failed_and_exit "candidate_exited"

FAILURE_REASON="candidate_timeout"
CANDIDATE_PID=""
CLEANUP_PID=""
READY_JSON=""

ready_evidence() {
  PYTHONPATH="$MANAGER_SRC" "$PYTHON_BIN" - "$1" "$2" "$3" "$4" "$ACTUAL_SHA" "$MANIFEST_JSON" \
    "$5" "$6" <<'PY'
import json, sys
pid, cwd, listener, health_raw, expected_sha, manifest_raw, stdout, stderr = sys.argv[1:]
try:
    expected_pid = int(pid)
    health = json.loads(health_raw)
    manifest = json.loads(manifest_raw)
    valid = (
        health.get("schema_version") == "open_trader.prediction_service.health.v1"
        and health.get("module") == "prediction_service"
        and health.get("status") == "running"
        and health.get("mode") == "production"
        and health.get("production_owner") is True
        and health.get("mutations") == "enabled"
        and health.get("pid") == expected_pid
        and health.get("cwd") == cwd
        and health.get("git_sha") == expected_sha
        and health.get("release_schema_version") == manifest["schema_version"]
        and type(health.get("reader_generation")) is int
        and health.get("reader_generation") == manifest["reader_generation"]
        and type(health.get("contract_generation")) is int
        and health.get("contract_generation") == manifest["contract_generation"]
        and listener == "127.0.0.1:8769"
        and isinstance(health.get("started_at"), str)
        and bool(health["started_at"])
        and bool(stdout)
        and bool(stderr)
    )
    if valid:
        print(json.dumps({
            "pid": health["pid"],
            "cwd": health["cwd"],
            "listener": listener,
            "health_schema": health["schema_version"],
            "health_module": health["module"],
            "health_status": health["status"],
            "mode": health["mode"],
            "production_owner": health["production_owner"],
            "mutations": health["mutations"],
            "git_sha": health["git_sha"],
            "release_schema_version": health["release_schema_version"],
            "reader_generation": health["reader_generation"],
            "contract_generation": health["contract_generation"],
            "process_started_at": health["started_at"],
            "logs": {"stdout": stdout, "stderr": stderr},
        }, separators=(",", ":")))
        raise SystemExit(0)
except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    pass
raise SystemExit(1)
PY
}

bootstrap_and_wait_for_exact_ready() {
  local attempt output status cwd listener health source_status
  local observed_stdout observed_stderr
  if ! "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$PLIST_PATH"; then
    FAILURE_REASON="candidate_exited"
    return 1
  fi
  for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
    if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
      CANDIDATE_PID="$(printf '%s\n' "$output" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ { print $3; exit }')"
      observed_stdout="$(printf '%s\n' "$output" | awk '$1 == "stdout" && $2 == "path" && $3 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
      observed_stderr="$(printf '%s\n' "$output" | awk '$1 == "stderr" && $2 == "path" && $3 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
    else
      status=$?
      if [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]]; then
        FAILURE_REASON="candidate_exited"
        return 1
      fi
      FAILURE_REASON="candidate_timeout"
      return 1
    fi
    if [[ -n "$CANDIDATE_PID" ]]; then
      cwd="$("$LSOF_BIN" -a -p "$CANDIDATE_PID" -d cwd -Fn 2>/dev/null \
        | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
      listener="$("$LSOF_BIN" -nP -a -p "$CANDIDATE_PID" -iTCP:8769 -sTCP:LISTEN -Fn 2>/dev/null \
        | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
      health="$("$CURL_BIN" -fsS http://127.0.0.1:8769/healthz 2>/dev/null || true)"
      if [[ -n "$health" ]]; then
        if [[ "$cwd" != "$REPO_ROOT" || "$listener" != "127.0.0.1:8769" ]]; then
          FAILURE_REASON="wrong_health_identity"
          return 1
        fi
        if READY_JSON="$(ready_evidence "$CANDIDATE_PID" "$cwd" "$listener" "$health" \
          "$observed_stdout" "$observed_stderr")"; then
          if ! source_status="$(git -C "$REPO_ROOT" status --porcelain)"; then
            FAILURE_REASON="candidate_source_became_dirty"
            return 1
          fi
          if [[ -n "$source_status" ]]; then
            FAILURE_REASON="candidate_source_became_dirty"
            return 1
          fi
          return 0
        fi
        FAILURE_REASON="wrong_health_identity"
        return 1
      fi
    fi
    [[ "$attempt" -lt "$WAIT_SECONDS" ]] && sleep 1
  done
  FAILURE_REASON="candidate_timeout"
  return 1
}

cleanup_verified_candidate() {
  local output status pid cwd label_path label_cwd
  local listener_output listener_status listener_pid listener_addr listener_count
  if output="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
    label_path="$(printf '%s\n' "$output" | awk '$1 == "path" && $2 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
    label_cwd="$(printf '%s\n' "$output" | awk '$1 == "working" && $2 == "directory" && $3 == "=" { sub(/^[^=]*= /, ""); print; exit }')"
    [[ "$label_path" == "$PLIST_PATH" && "$label_cwd" == "$REPO_ROOT" ]] || return 1
    pid="$(printf '%s\n' "$output" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ { print $3; exit }')"
    CLEANUP_PID="$pid"
    if [[ -n "$pid" ]]; then
      cwd="$("$LSOF_BIN" -a -p "$pid" -d cwd -Fn 2>/dev/null \
        | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
      [[ "$cwd" == "$REPO_ROOT" ]] || return 1
      if listener_output="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
        listener_status=0
      else
        listener_status=$?
        [[ "$listener_status" -eq 1 && -z "$listener_output" ]] || return 1
      fi
      if [[ "$listener_status" -eq 0 ]]; then
        listener_pid="$(printf '%s\n' "$listener_output" | awk '
          /^p[0-9]+$/ { current = substr($1, 2) }
          /^n/ { print current; exit }
        ')"
        listener_addr="$(printf '%s\n' "$listener_output" | awk '/^n/ { print substr($1, 2); exit }')"
        listener_count="$(printf '%s\n' "$listener_output" | awk '/^n/ { count += 1 } END { print count + 0 }')"
        [[ "$listener_count" -eq 1 && "$listener_pid" == "$pid" \
          && "$listener_addr" == "127.0.0.1:8769" ]] || return 1
      fi
    fi
    bootout_if_loaded || return 1
    wait_agent_absent
    return
  else
    status=$?
    [[ "$status" -ne 0 && "$output" == *"Could not find service"* ]] || return 1
  fi
  return 0
}

candidate_absent() {
  wait_agent_absent || return 1
  if [[ -n "$CANDIDATE_PID" ]] && ! pid_absent "$CANDIDATE_PID"; then
    return 1
  fi
  if [[ -n "$CLEANUP_PID" && "$CLEANUP_PID" != "$CANDIDATE_PID" ]] \
    && ! pid_absent "$CLEANUP_PID"; then
    return 1
  fi
  listener_absent || return 1
  owner_available
}

finish_candidate_failure() {
  local reason="$1" cleanup_ok=1
  FAILURE_REASON="$reason"
  cleanup_verified_candidate || cleanup_ok=0
  candidate_absent || cleanup_ok=0
  remove_managed_plist || cleanup_ok=0
  if [[ "$cleanup_ok" -ne 1 ]]; then
    FAILURE_REASON="candidate_cleanup_not_proven"
  fi
  record_failed_and_exit "$FAILURE_REASON"
}

if ! bootstrap_and_wait_for_exact_ready; then
  finish_candidate_failure "$FAILURE_REASON"
fi

if ! write_record ready "" "$READY_JSON"; then
  finish_candidate_failure "runtime_record_write_failed"
fi
echo "installed managed prediction release: $ACTUAL_SHA"
