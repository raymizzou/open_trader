#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
MODE="shadow"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
CONFIG=""
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
  echo "usage: $0 --runtime-root PATH [--dry-run] [--mode shadow|production] [--repo-root PATH] [--python PATH] [--config PATH] [--launch-agents-dir PATH] [--wait-seconds N] [--release-manifest PATH] [--expected-sha SHA]" >&2
}

fail() {
  echo "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --mode) [[ $# -ge 2 ]] || { usage; exit 2; }; MODE="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --config) [[ $# -ge 2 ]] || { usage; exit 2; }; CONFIG="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    --release-manifest) [[ $# -ge 2 ]] || { usage; exit 2; }; RELEASE_MANIFEST="$2"; shift 2 ;;
    --expected-sha) [[ $# -ge 2 ]] || { usage; exit 2; }; EXPECTED_SHA="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "$RUNTIME_ROOT" && "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
[[ "$MODE" == "shadow" || "$MODE" == "production" ]] || { usage; exit 2; }

resolve_path() {
  "$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1"
}

REPO_ROOT="$(resolve_path "$REPO_ROOT")"
RUNTIME_ROOT="$(resolve_path "$RUNTIME_ROOT")"
RELEASE_MANIFEST="${RELEASE_MANIFEST:-$REPO_ROOT/ops/prediction-service-release.json}"
RELEASE_MANIFEST="$(resolve_path "$RELEASE_MANIFEST")"
CONFIG="${CONFIG:-$RUNTIME_ROOT/config/prediction_arbitrage.json}"
TEMPLATE="$REPO_ROOT/ops/launchd/$LABEL.plist.template"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$RUNTIME_ROOT/data"
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
    -e "s|OPEN_TRADER_PREDICTION_MODE|$(sed_escape "$MODE")|g" \
    -e "s|OPEN_TRADER_RELEASE_MANIFEST|$(sed_escape "$RELEASE_MANIFEST")|g" \
    -e "s|OPEN_TRADER_RUNTIME_ROOT|$(sed_escape "$RUNTIME_ROOT")|g" \
    -e "s|OPEN_TRADER_REPO|$(sed_escape "$REPO_ROOT")|g" \
    "$TEMPLATE"
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
  [[ "$status" -eq 1 ]]
}

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

MANIFEST_JSON=""
ACTUAL_SHA=""
CANDIDATE_JSON=""
if [[ "$MODE" == "production" ]]; then
  ACTUAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  SOURCE_STATUS=""
  if ! SOURCE_STATUS="$(git -C "$REPO_ROOT" status --porcelain)"; then
    fail "failed to inspect release root: $REPO_ROOT"
  fi
  [[ -z "$SOURCE_STATUS" ]] || fail "release root is dirty: $REPO_ROOT"
  [[ -z "$EXPECTED_SHA" || "$EXPECTED_SHA" == "$ACTUAL_SHA" ]] || fail "requested SHA does not match checkout"
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
    fail "release manifest must be tracked by checkout"
  fi
  git -C "$REPO_ROOT" ls-files --error-unmatch -- "$MANIFEST_RELATIVE" >/dev/null 2>&1 \
    || fail "release manifest must be tracked by checkout"
  git -C "$REPO_ROOT" cat-file -e "$ACTUAL_SHA:$MANIFEST_RELATIVE" 2>/dev/null \
    || fail "release manifest must be tracked by checkout"
  MANIFEST_JSON="$(PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$RELEASE_MANIFEST" <<'PY'
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
)"
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

if [[ "$MODE" == "shadow" ]]; then
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

CURRENT_RECORD_JSON="$(PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$RUNTIME_RECORD" <<'PY'
import json, sys
from pathlib import Path
from open_trader.prediction_release import load_prediction_runtime_record
record = load_prediction_runtime_record(Path(sys.argv[1]))
print("null" if record is None else json.dumps(record, separators=(",", ":")))
PY
)"

LABEL_LOADED=0
LABEL_OUTPUT=""
OLD_PID=""
if LABEL_OUTPUT="$("$LAUNCHCTL_BIN" print "gui/$UID/$LABEL" 2>&1)"; then
  LABEL_LOADED=1
  OLD_PID="$(printf '%s\n' "$LABEL_OUTPUT" | awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }')"
  [[ -n "$OLD_PID" ]] || fail "managed launchd identity is not verified"
else
  LABEL_STATUS=$?
  if [[ "$LABEL_STATUS" -eq 0 || "$LABEL_OUTPUT" != *"Could not find service"* ]]; then
    fail "failed to inspect launchd label: $LABEL"
  fi
fi

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
  [[ "$LISTENER_STATUS" -eq 1 ]] || fail "failed to inspect listener on 8769"
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
        and ready.get("reader_generation") == health.get("reader_generation")
        and ready.get("contract_generation") == health.get("contract_generation")
        and ready.get("process_started_at") == health.get("started_at")
    )
except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

MANAGED_OLD=0
if [[ "$LABEL_LOADED" -eq 1 ]]; then
  if [[ "$LISTENER_STATUS" -eq 0 && "$LISTENER_COUNT" -eq 1 \
    && "$LISTENER_PID" == "$OLD_PID" && "$LISTENER_ADDR" == "127.0.0.1:8769" \
    && "$OWNER_AVAILABLE" -eq 0 ]] \
    && managed_identity_matches "$OLD_PID" "$CURRENT_CWD" "$LISTENER_ADDR" \
      "$CURRENT_HEALTH" "$CURRENT_RECORD_JSON"; then
    MANAGED_OLD=1
  else
    fail "managed launchd identity is not verified"
  fi
else
  [[ "$LISTENER_STATUS" -ne 0 ]] || fail "unknown listener on 8769"
  [[ "$OWNER_AVAILABLE" -eq 1 ]] \
    || fail "prediction runtime owner is held by an unknown process"
fi

record_candidate_matches() {
  "$PYTHON_BIN" - "$CURRENT_RECORD_JSON" "$CANDIDATE_JSON" <<'PY'
import json, sys
try:
    record, candidate = map(json.loads, sys.argv[1:])
    matches = record.get("state") == "ready" and record.get("candidate") == candidate
except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)
PY
}

if [[ "$MANAGED_OLD" -eq 1 ]] && record_candidate_matches; then
  echo "prediction release already ready: $ACTUAL_SHA"
  exit 0
fi

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
PREVIOUS_READY_JSON="$("$PYTHON_BIN" - "$CURRENT_RECORD_JSON" <<'PY'
import json, sys
record = json.loads(sys.argv[1])
ready = record.get("ready") if isinstance(record, dict) and record.get("state") == "ready" else None
print("null" if not isinstance(ready, dict) else json.dumps(ready, separators=(",", ":")))
PY
)"
TRANSITION_STARTED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))')"

write_record() {
  local state="$1" failure_reason="$2" ready_json="$3"
  PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - \
    "$RUNTIME_RECORD" "$state" "$CANDIDATE_JSON" "$PREVIOUS_JSON" \
    "$TRANSITION_STARTED_AT" "$failure_reason" "$ready_json" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
from open_trader.prediction_release import write_prediction_runtime_record
path, state, candidate, previous, started, failure, ready = sys.argv[1:]
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
write_prediction_runtime_record(Path(path), payload)
PY
}

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

write_record maintenance "" "$PREVIOUS_READY_JSON"

if [[ "$MANAGED_OLD" -eq 1 ]]; then
  bootout_if_loaded \
    || record_failed_and_exit "candidate_cleanup_not_proven"
fi
wait_agent_absent \
  || record_failed_and_exit "candidate_cleanup_not_proven"
if [[ -n "$OLD_PID" ]] && "$PS_BIN" -p "$OLD_PID" >/dev/null 2>&1; then
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
READY_JSON=""

ready_evidence() {
  "$PYTHON_BIN" - "$1" "$2" "$3" "$4" "$ACTUAL_SHA" "$MANIFEST_JSON" \
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
    if [[ -n "$pid" ]]; then
      [[ -z "$CANDIDATE_PID" || "$pid" == "$CANDIDATE_PID" ]] || return 1
      cwd="$("$LSOF_BIN" -a -p "$pid" -d cwd -Fn 2>/dev/null \
        | awk '$1 ~ /^n/ { print substr($1, 2); exit }' || true)"
      [[ "$cwd" == "$REPO_ROOT" ]] || return 1
      if listener_output="$("$LSOF_BIN" -nP -iTCP:8769 -sTCP:LISTEN -Fn 2>&1)"; then
        listener_status=0
      else
        listener_status=$?
        [[ "$listener_status" -eq 1 ]] || return 1
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
  if [[ -n "$CANDIDATE_PID" ]] && "$PS_BIN" -p "$CANDIDATE_PID" >/dev/null 2>&1; then
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
