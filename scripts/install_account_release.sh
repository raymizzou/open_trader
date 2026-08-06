#!/usr/bin/env bash
set -euo pipefail

# Install Account Sync Worker + Account API as one matched Account release.
# Worker first (stop old writer, wait for lock release, start new writer,
# wait for a fresh publication), then API (wait for same-SHA healthy route),
# then a final same-SHA cross-check with recorded evidence.

DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=""
PYTHON_BIN="${OPEN_TRADER_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
WAIT_SECONDS="${ACCOUNT_RELEASE_LAUNCHD_WAIT_SECONDS:-120}"
EVIDENCE_OUT=""

usage() {
  echo "usage: $0 [--dry-run] [--repo-root PATH] [--runtime-root PATH] [--python PATH] [--launch-agents-dir PATH] [--wait-seconds N] [--evidence-out PATH]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --repo-root) [[ $# -ge 2 ]] || { usage; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --runtime-root) [[ $# -ge 2 ]] || { usage; exit 2; }; RUNTIME_ROOT="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || { usage; exit 2; }; PYTHON_BIN="$2"; shift 2 ;;
    --launch-agents-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; LAUNCH_AGENTS_DIR="$2"; shift 2 ;;
    --wait-seconds) [[ $# -ge 2 ]] || { usage; exit 2; }; WAIT_SECONDS="$2"; shift 2 ;;
    --evidence-out) [[ $# -ge 2 ]] || { usage; exit 2; }; EVIDENCE_OUT="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { usage; exit 2; }
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT}"
RUNTIME_ROOT="$(cd "$RUNTIME_ROOT" 2>/dev/null && pwd || printf '%s' "$RUNTIME_ROOT")"

[[ -f "$REPO_ROOT/scripts/install_account_sync_launchd.sh" ]] || { echo "missing release scripts in $REPO_ROOT" >&2; exit 1; }
EXPECTED_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  "$REPO_ROOT/scripts/install_account_sync_launchd.sh" --dry-run \
    --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" \
    --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
    --wait-seconds "$WAIT_SECONDS"
  "$REPO_ROOT/scripts/install_account_api_launchd.sh" --dry-run --mode production \
    --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" \
    --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
    --wait-seconds "$WAIT_SECONDS"
  echo "account release $EXPECTED_SHA: worker first, then API, then same-SHA cross-check"
  exit 0
fi

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "release root is dirty: $REPO_ROOT" >&2
  exit 1
fi

# 1. Writer: stop old writer, wait lock released, start new writer,
#    wait for a fresh account publication at this release SHA.
"$REPO_ROOT/scripts/install_account_sync_launchd.sh" \
  --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --wait-seconds "$WAIT_SECONDS"

# 2. API: start new API and wait until it publishes same-SHA healthy state.
"$REPO_ROOT/scripts/install_account_api_launchd.sh" --mode production \
  --repo-root "$REPO_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$PYTHON_BIN" --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --wait-seconds "$WAIT_SECONDS"

WORKER_PID="$("$LAUNCHCTL_BIN" print "gui/$UID/com.open-trader.account-sync-controller" 2>/dev/null |
  awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }' || true)"
API_PID="$("$LAUNCHCTL_BIN" print "gui/$UID/com.open-trader.account-api" 2>/dev/null |
  awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }' || true)"

EVIDENCE_JSON="$("$PYTHON_BIN" - "$EXPECTED_SHA" "$REPO_ROOT" "$RUNTIME_ROOT" \
  "http://127.0.0.1:8768" "$RUNTIME_ROOT/data/account_sync/controller_status.json" \
  "$WORKER_PID" "$API_PID" "$LSOF_BIN" "$EVIDENCE_OUT" <<'PY'
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

expected_sha, repo_root, runtime_root, api_url, status_path = sys.argv[1:6]
worker_pid, api_pid, lsof_bin, evidence_out = sys.argv[6:10]

def fetch(url):
    req = urllib.request.Request(
        url, headers={"X-Open-Trader-Account-Route": "production"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())

health_status, health = fetch(api_url + "/healthz")
snapshot_status, snapshot = fetch(api_url + "/api/v1/account/snapshot")
worker = json.loads(Path(status_path).read_text(encoding="utf-8"))

listener = ""
if api_pid and lsof_bin:
    try:
        out = subprocess.run(
            [lsof_bin, "-nP", "-a", "-p", api_pid, "-iTCP:8768", "-sTCP:LISTEN", "-Fn"],
            check=True, capture_output=True, text=True,
        ).stdout
        listener = next(
            (line[1:] for line in out.splitlines() if line.startswith("n")),
            "",
        )
    except (subprocess.CalledProcessError, OSError):
        listener = ""

heartbeat = worker.get("heartbeat_at")
heartbeat_fresh = False
if heartbeat:
    try:
        heartbeat_dt = datetime.fromisoformat(heartbeat)
        heartbeat_fresh = (
            heartbeat_dt.tzinfo is not None
            and abs((datetime.now().astimezone() - heartbeat_dt).total_seconds()) <= 120
        )
    except ValueError:
        heartbeat_fresh = False

checks = {
    "worker_pid_matches_launchd": worker.get("pid") == int(worker_pid or 0),
    "worker_sha_matches": worker.get("git_sha") == expected_sha,
    "worker_cwd_matches": worker.get("working_directory") == repo_root,
    "worker_heartbeat_fresh": heartbeat_fresh,
    "api_pid_matches_launchd": health.get("pid") == int(api_pid or 0),
    "api_status_ok": health.get("status") == "ok" and health_status == 200,
    "api_mode_production": health.get("mode") == "production",
    "api_sha_matches": health.get("api_git_sha") == expected_sha,
    "worker_sha_in_api_matches": health.get("worker_git_sha") == expected_sha,
    "release_match": health.get("release_match") is True,
    "api_listens_8768": listener == "127.0.0.1:8768",
    "snapshot_healthy": snapshot_status == 200
        and snapshot.get("schema_version") == 1
        and snapshot.get("status") == "healthy"
        and bool(snapshot.get("snapshot_generation")),
    "snapshot_release_matches": snapshot.get("release", {}).get("api_git_sha") == expected_sha
        and snapshot.get("release", {}).get("worker_git_sha") == expected_sha,
}

evidence = {
    "schema_version": "open_trader.account_release.install.v1",
    "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "repo_root": repo_root,
    "runtime_root": runtime_root,
    "git_sha": expected_sha,
    "api": {
        "pid": health.get("pid"),
        "started_at": health.get("started_at"),
        "listener": listener,
        "mode": health.get("mode"),
    },
    "worker": {
        "pid": worker.get("pid"),
        "started_at": worker.get("started_at"),
        "heartbeat_at": heartbeat,
        "phase": worker.get("phase"),
        "cwd": worker.get("working_directory"),
    },
    "snapshot": {
        "http_status": snapshot_status,
        "snapshot_generation": snapshot.get("snapshot_generation"),
        "account_generation": snapshot.get("account_generation"),
        "status": snapshot.get("status"),
    },
    "checks": checks,
    "logs": {
        "worker_out": str(Path(repo_root) / "logs" / "account_sync" / "launchd.out.log"),
        "api_out": str(Path(repo_root) / "logs" / "account_api" / "launchd.out.log"),
    },
}

if evidence_out:
    Path(evidence_out).parent.mkdir(parents=True, exist_ok=True)
    Path(evidence_out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(evidence, ensure_ascii=False))
if not all(checks.values()):
    raise SystemExit(1)
PY
)"

echo "account release installed: $EXPECTED_SHA (api pid $API_PID, worker pid $WORKER_PID)"
if [[ -n "$EVIDENCE_OUT" ]]; then
  echo "evidence written: $EVIDENCE_OUT"
fi
