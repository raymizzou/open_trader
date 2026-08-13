from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_prediction_service.sh"
SHA = "a" * 40


FAKE_COMMAND = r'''#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
route_path = Path(os.environ["FAKE_ROUTE"])
runtime_root = Path(os.environ["FAKE_RUNTIME_ROOT"])
state = json.loads(state_path.read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
state["calls"].append([command, *sys.argv[1:]])
state["command_paths"].append(sys.argv[0])

def owners():
    return int(state["legacy_prediction_owner"] == "enabled") + int(
        state["prediction_service_ready"]
    )

def save():
    state["max_prediction_owners"] = max(state["max_prediction_owners"], owners())
    state["owner_available"] = owners() == 0
    state_path.write_text(json.dumps(state), encoding="utf-8")

def option(name, default=""):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default

def observe_route():
    if route_path.exists():
        route = json.loads(route_path.read_text(encoding="utf-8"))
    else:
        route = {
            "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
            "mode": "maintenance",
            "operation_id": "__absent__",
            "updated_at": "2026-08-12T09:00:00+08:00",
        }
    mode = route["mode"]
    if state["states"][-1] != mode:
        state["states"].append(mode)
    return route

if command == "python":
    if len(sys.argv) > 2 and sys.argv[1] == "-c" \
            and "start_new_session=True" in sys.argv[2]:
        save()
        os.execv(os.environ["FAKE_REAL_PYTHON"], [
            os.environ["FAKE_REAL_PYTHON"], *sys.argv[1:]
        ])
    source = sys.stdin.read() if len(sys.argv) > 1 and sys.argv[1] == "-" else None
    tag = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-" else ""
    mode = sys.argv[4] if tag == "route-write" else ""
    result = sys.argv[9] if tag == "evidence-write" else ""
    fail = (
        state["fail_at"] == "route_maintenance_write"
        and tag == "route-write" and mode == "maintenance"
        and not state.get("injected_failure_seen")
    ) or (
        state["fail_at"] == "service_route_write"
        and tag == "route-write" and mode == "service"
        and not state.get("injected_failure_seen")
    ) or (
        state["fail_at"] == "legacy_route_write"
        and tag == "route-write" and mode == "legacy"
        and not state.get("injected_failure_seen")
    ) or (
        state["fail_at"] == "evidence_write"
        and tag == "evidence-write" and result == "ready"
        and not state.get("injected_failure_seen")
    )
    if (
        state["fail_at"] == "initial_stale_operation"
        and tag == "route-write" and mode == "maintenance"
        and not state.get("injected_failure_seen")
    ):
        newer_route = json.loads(route_path.read_text(encoding="utf-8"))
        newer_route.update(mode="maintenance", operation_id="newer-operation")
        route_path.write_text(json.dumps(newer_route), encoding="utf-8")
        Path(os.environ["FAKE_EVIDENCE"]).write_text(
            json.dumps(state["newer_evidence"]), encoding="utf-8"
        )
        state["injected_failure_seen"] = True
    if (
        state["fail_at"] == "locked_state_race"
        and tag == "route-write" and mode == "maintenance"
        and not state.get("injected_failure_seen")
    ):
        state["injected_failure_seen"] = True
    save()
    if tag == "route-write" and mode in {"service", "legacy"}:
        state["post_route_ready"] = True
        save()
    child_env = os.environ.copy()
    if state["fail_at"] == "locked_state_race" and tag == "route-write":
        child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [
            os.environ["FAKE_INSTRUMENTATION"], child_env.get("PYTHONPATH", "")
        ]))
    completed = subprocess.run(
        [os.environ["FAKE_REAL_PYTHON"], *sys.argv[1:]],
        input=source,
        text=True,
        env=child_env,
    )
    if fail and completed.returncode == 0:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["injected_failure_seen"] = True
        save()
        raise SystemExit(1)
    raise SystemExit(completed.returncode)

if command == "git":
    if "rev-parse" in sys.argv and "HEAD" in sys.argv:
        print(state.get("git_sha", os.environ["FAKE_SHA"]))
    elif "status" in sys.argv:
        print(state.get("git_status", ""))
    else:
        raise SystemExit(2)
    save()
    raise SystemExit(0)

if command == "launchctl":
    if state["fail_at"] == "label_inspection_error":
        print("launchctl diagnostic", file=sys.stderr)
        save()
        raise SystemExit(2)
    if len(sys.argv) == 3 and sys.argv[1] == "print" and sys.argv[2].count("/") == 1:
        labels = [
            (item["pid"], name)
            for name, item in state["labels"].items() if item["loaded"]
        ] + [(9998, label) for label in state["extra_loaded_labels"]]
        print("PID\tStatus\tLabel")
        for pid, label in labels:
            print(f"{pid}\t-\t{label}")
        save()
        raise SystemExit(0)
    if sys.argv[1] == "bootout":
        label = sys.argv[-1].rsplit("/", 1)[-1]
        if label == "com.open-trader.frontend-gateway" and state["fail_at"] == "gateway_bootout":
            print("bootout diagnostic", file=sys.stderr)
            save()
            raise SystemExit(1)
        item = state["labels"].get(label)
        if item is None or not item["loaded"]:
            save()
            raise SystemExit(0)
        item["loaded"] = False
        if label == "com.open-trader.frontend-gateway":
            state["listeners"]["8766"] = None
            state["gateway_stopped"] = True
        elif label == "com.open-trader.legacy-dashboard":
            state["listeners"]["8767"] = None
        elif label == "com.open-trader.prediction-service":
            state["listeners"]["8769"] = None
            state["prediction_service_ready"] = False
            state["lock_holders"] = []
        save()
        raise SystemExit(0)
    label = sys.argv[-1].rsplit("/", 1)[-1]
    item = state["labels"].get(label)
    if sys.argv[1] == "print" and item and item["loaded"]:
        print(f"\tpath = {item['plist']}")
        print("\targuments = {")
        for argument in item["argv"]:
            print(f"\t\t{argument}")
        print("\t}")
        print(f"\tworking directory = {item['cwd']}")
        print(f"\tpid = {item['pid']}")
        save()
        raise SystemExit(0)
    print("Could not find service", file=sys.stderr)
    save()
    raise SystemExit(113)

if command == "lsof":
    if state["fail_at"] == "listener_inspection_error":
        print("lsof diagnostic", file=sys.stderr)
        save()
        raise SystemExit(2)
    if (
        state["fail_at"] == "bootstrap_listener_inspection_error"
        and state.get("gateway_stopped")
        and "TCP:8766" in " ".join(sys.argv[1:])
    ):
        print("bootstrap lsof diagnostic", file=sys.stderr)
        save()
        raise SystemExit(2)
    args = " ".join(sys.argv[1:])
    if args.endswith("runtime.lock"):
        for pid in state["lock_holders"]:
            print(f"p{pid}")
        save()
        raise SystemExit(0 if state["lock_holders"] else 1)
    if "-d cwd" in args:
        pid = option("-p")
        for item in state["labels"].values():
            if str(item["pid"]) == pid and item["loaded"]:
                print(f"n{item['cwd']}")
                save()
                raise SystemExit(0)
        save()
        raise SystemExit(1)
    for port, listener in state["listeners"].items():
        if f"TCP:{port}" in args and listener:
            print(f"p{listener['pid']}")
            print(f"n127.0.0.1:{port}")
            save()
            raise SystemExit(0)
    save()
    raise SystemExit(1)

if command == "ps":
    if state["fail_at"] == "ps_inspection_error":
        print("ps diagnostic", file=sys.stderr)
        save()
        raise SystemExit(2)
    pid = option("-p")
    present = any(
        item["loaded"] and str(item["pid"]) == pid
        for item in state["labels"].values()
    )
    if present:
        print(pid)
    save()
    raise SystemExit(0 if present else 1)

if command == "owner-probe":
    if state["fail_at"] == "owner_lock" or (
        state["fail_at"] == "legacy_owner_lock" and owners() > 0
    ):
        save()
        raise SystemExit(2)
    save()
    raise SystemExit(0 if owners() == 0 else 1)

if command == "curl":
    url = sys.argv[-1]
    output_path = option("--output")
    cookie_jar = option("--cookie-jar")
    route = observe_route()
    if state["fail_at"] == "stale_operation" and route["mode"] == "maintenance" \
            and not state.get("injected_failure_seen"):
        route["operation_id"] = "newer-operation"
        route_path.write_text(json.dumps(route), encoding="utf-8")
        Path(os.environ["FAKE_EVIDENCE"]).write_text(
            json.dumps(state["newer_evidence"]), encoding="utf-8"
        )
        state["injected_failure_seen"] = True
    if state["fail_at"] == "gateway_maintenance" and route["mode"] == "maintenance":
        route = {**route, "mode": "legacy"}
    if state["fail_at"] == "gateway_legacy" and route["mode"] == "legacy":
        route = {**route, "mode": "maintenance"}
    if state["fail_at"] == "gateway_service" and route["mode"] == "service":
        route = {**route, "mode": "maintenance"}
    if state["fail_at"] == "post_route_signal" \
            and state.get("post_route_ready") \
            and route["mode"] in {"service", "legacy"} \
            and not state.get("post_route_signal_seen"):
        Path(os.environ["FAKE_POST_ROUTE_SIGNAL"]).write_text("ready\n", encoding="utf-8")
        state["post_route_signal_seen"] = True
        save()
        time.sleep(30)
    if route["mode"] == "maintenance" and ":8766/api/prediction-arbitrage/" in url:
        payload = {
            "schema_version": "open_trader.frontend_gateway.error.v1",
            "code": "prediction_maintenance",
            "message": "Prediction service is in maintenance",
            "route_mode": "maintenance",
        }
        if output_path:
            Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
        if cookie_jar:
            Path(cookie_jar).write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        if any(arg in {"--write-out", "-w"} for arg in sys.argv):
            print("503", end="")
        save()
        raise SystemExit(0)
    if url.endswith("/healthz") and ":8766" in url:
        gateway = state["labels"]["com.open-trader.frontend-gateway"]
        gateway_health = {
            "schema_version": "open_trader.frontend_gateway.health.v1",
            "module": "frontend_gateway",
            "pid": gateway["pid"],
            "cwd": gateway["cwd"],
            "git_sha": gateway["sha"],
            "source_state": "clean",
            "legacy_upstream_status": "ok",
            "account_upstream_status": "ok",
            "prediction_route_mode": route["mode"],
            "prediction_inflight_requests": (
                1 if state["fail_at"] == "inflight_drain" else state["inflight"]
            ),
            "prediction_upstream_status": {
                "service": "ok" if state["prediction_service_ready"] else "unavailable",
                "legacy": "legacy",
                "maintenance": "maintenance",
            }[route["mode"]],
        }
        if state["fail_at"] == "gateway_service_health" and route["mode"] == "service":
            gateway_health["prediction_upstream_status"] = "unavailable"
        print(json.dumps(gateway_health))
    elif url.endswith("/healthz") and ":8769" in url:
        service = state["labels"]["com.open-trader.prediction-service"]
        health = {
            "schema_version": "open_trader.prediction_service.health.v1",
            "module": "prediction_service",
            "status": "running",
            "mode": "production",
            "production_owner": True,
            "mutations": "enabled",
            "pid": service["pid"],
            "cwd": service["cwd"],
            "git_sha": service["sha"],
            "release_schema_version": "open_trader.prediction_service.release.v1",
            "reader_generation": 1,
            "contract_generation": 1,
            "started_at": "2026-08-12T10:00:00+08:00",
        }
        if state["fail_at"] == "health_evidence":
            health["git_sha"] = "wrong"
        print(json.dumps(health))
    elif url.endswith("/healthz") and ":8768" in url:
        print(json.dumps({
            "schema_version": "open_trader.account_api.health.v1",
            "module": "account_api",
            "status": "healthy",
            "pid": state["labels"].get("com.open-trader.account-sync-controller", {}).get("pid", 4001),
            "api_git_sha": os.environ["FAKE_SHA"],
            "worker_git_sha": os.environ["FAKE_SHA"],
        }))
    elif url.endswith("/healthz") and ":8767" in url:
        legacy = state["labels"]["com.open-trader.legacy-dashboard"]
        payload = {
            "schema_version": "open_trader.legacy_dashboard.health.v1",
            "module": "legacy_dashboard",
            "pid": legacy["pid"],
            "cwd": legacy["cwd"],
            "git_sha": legacy["sha"],
            "source_state": "clean",
            "prediction_owner": state["legacy_prediction_owner"],
        }
        if (
            state["fail_at"] == "legacy_contract"
            and state["legacy_prediction_owner"] == "enabled"
        ):
            payload["git_sha"] = "wrong"
        print(json.dumps(payload))
    elif "/api/prediction-arbitrage/history" in url:
        kind = parse_qs(urlsplit(url).query).get("kind", [""])[0]
        print(json.dumps({"kind": kind, "items": [], "total": 0, "limit": 100, "offset": 0, "has_more": False}))
    elif url.endswith("/api/prediction-arbitrage/preview"):
        print(json.dumps({"state": "rejected", "reason": "opportunity_unavailable"}))
    else:
        if state["fail_at"] == "public_contract":
            print("[]")
            save()
            raise SystemExit(0)
        public_status = state.get("public_status", "healthy")
        print(json.dumps({
            "status": public_status,
            "health": {"status": state.get("public_health_status", "healthy")},
            "readiness": {
                "status": state.get("public_readiness_status", "ready")
            },
            "stale": False,
            "events": [],
            "opportunities": [],
            "csrf_token": "fake-csrf",
        }))
    body = sys.stdout.getvalue() if hasattr(sys.stdout, "getvalue") else None
    if output_path:
        # Reconstruct the just-emitted JSON from the command branch without relying
        # on the test runner's stdout implementation.
        if "/api/prediction-arbitrage/history" in url:
            kind = parse_qs(urlsplit(url).query).get("kind", [""])[0]
            payload = {"kind": kind, "items": [], "total": 0, "limit": 100, "offset": 0, "has_more": False}
        elif url.endswith("/api/prediction-arbitrage/preview"):
            payload = {"state": "rejected", "reason": "opportunity_unavailable"}
        elif url.endswith("/api/prediction-arbitrage/state"):
            payload = {"status": state.get("public_status", "healthy"), "health": {"status": state.get("public_health_status", "healthy")}, "readiness": {"status": state.get("public_readiness_status", "ready")}, "stale": False, "events": [], "opportunities": [], "csrf_token": "fake-csrf"}
        else:
            payload = {}
        Path(output_path).write_text(json.dumps(payload), encoding="utf-8")
    if cookie_jar:
        Path(cookie_jar).write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tot_prediction_session\tfake-session\n", encoding="utf-8")
    if any(arg in {"--write-out", "-w"} for arg in sys.argv):
        print("200", end="")
    save()
    raise SystemExit(0)

if command == "install_dashboard_launchd.sh":
    mode = option("--mode", "legacy")
    expected = [
        "--mode", mode, "--prediction-owner", option("--prediction-owner"),
        "--repo-root", os.environ["FAKE_REPO_ROOT"],
        "--runtime-root", os.environ["FAKE_RUNTIME_ROOT"],
        "--python", os.environ["FAKE_PYTHON"],
        "--launch-agents-dir", os.environ["FAKE_LAUNCH_AGENTS"],
        "--wait-seconds", os.environ["FAKE_WAIT_SECONDS"],
    ]
    if sys.argv[1:] != expected:
        save()
        raise SystemExit(97)
    owner = option("--prediction-owner")
    if state["fail_at"] == "legacy_restart":
        save()
        raise SystemExit(1)
    legacy = state["labels"]["com.open-trader.legacy-dashboard"]
    if state["fail_at"] != "old_legacy_pid":
        legacy["pid"] += 1
    legacy["cwd"] = option("--repo-root")
    legacy["argv"] = [
        os.environ["FAKE_PYTHON"], "-m", "open_trader", "dashboard",
        "--prediction-owner", owner,
    ]
    state["legacy_prediction_owner"] = owner
    state["lock_holders"] = [legacy["pid"]] if owner == "enabled" else []
    state["listeners"]["8767"] = {"pid": legacy["pid"]}
    if mode == "stack":
        gateway = state["labels"]["com.open-trader.frontend-gateway"]
        gateway.update(loaded=True, pid=1002, cwd=option("--repo-root"), sha=os.environ["FAKE_SHA"])
        state["listeners"]["8766"] = {"pid": gateway["pid"]}
    save()
    raise SystemExit(0)

if command == "install_prediction_service_launchd.sh":
    expected = [
        "--mode", "production", "--repo-root", os.environ["FAKE_REPO_ROOT"],
        "--runtime-root", os.environ["FAKE_RUNTIME_ROOT"],
        "--python", os.environ["FAKE_PYTHON"],
        "--config", os.environ["FAKE_CONFIG"],
        "--launch-agents-dir", os.environ["FAKE_LAUNCH_AGENTS"],
        "--wait-seconds", os.environ["FAKE_WAIT_SECONDS"],
        "--expected-sha", os.environ["FAKE_SHA"],
    ]
    if sys.argv[1:] != expected:
        save()
        raise SystemExit(97)
    if state["fail_at"] == "service_installer_timeout":
        time.sleep(10)
    if state["fail_at"] == "service_installer_interrupt":
        Path(os.environ["FAKE_SLEEPING_CHILD_PID"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        def finish_signal(signum, _frame):
            Path(os.environ["FAKE_CHILD_CLEANUP_STARTED"]).write_text(
                f"{signum}\n", encoding="utf-8"
            )
            time.sleep(0.5)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGINT, finish_signal)
        signal.signal(signal.SIGTERM, finish_signal)
        save()
        time.sleep(30)
    if state["fail_at"] in {"service_installer", "service_generation", "service_reconcile"}:
        save()
        raise SystemExit(1)
    service = state["labels"]["com.open-trader.prediction-service"]
    service.update(
        loaded=True, pid=3001, cwd=option("--repo-root"), sha=os.environ["FAKE_SHA"],
        argv=[os.environ["FAKE_PYTHON"], "-m", "open_trader", "prediction-service"],
    )
    state["prediction_service_ready"] = True
    state["lock_holders"] = [service["pid"]]
    state["listeners"]["8769"] = {"pid": service["pid"]}
    record = {
        "schema_version": "open_trader.prediction_service.runtime.v1",
        "state": "ready",
        "candidate": {
            "source_state": "clean",
            "checkout": service["cwd"],
            "git_sha": os.environ["FAKE_SHA"],
            "reader_generation": 1,
            "contract_generation": 1,
        },
        "ready": {
            "pid": service["pid"],
            "cwd": service["cwd"],
            "listener": "127.0.0.1:8769",
            "health_schema": "open_trader.prediction_service.health.v1",
            "health_module": "prediction_service",
            "health_status": "running",
            "mode": "production",
            "git_sha": os.environ["FAKE_SHA"],
            "production_owner": True,
            "mutations": "enabled",
            "release_schema_version": "open_trader.prediction_service.release.v1",
            "reader_generation": 1,
            "contract_generation": 1,
            "process_started_at": "2026-08-12T10:00:00+08:00",
        },
    }
    if state["fail_at"] == "runtime_evidence":
        record["ready"]["pid"] = True
    if state["fail_at"] == "generation_evidence":
        record["ready"]["reader_generation"] = True
    if state["fail_at"] == "runtime_schema":
        record["schema_version"] = "wrong"
    (runtime_root / "prediction-service-runtime.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    save()
    raise SystemExit(0)

if command == "uninstall_prediction_service_launchd.sh":
    expected = [
        "--mode", "production", "--runtime-root", os.environ["FAKE_RUNTIME_ROOT"],
        "--python", os.environ["FAKE_PYTHON"],
        "--launch-agents-dir", os.environ["FAKE_LAUNCH_AGENTS"],
    ]
    if sys.argv[1:] != expected:
        save()
        raise SystemExit(97)
    if state["fail_at"] == "service_uninstall":
        save()
        raise SystemExit(1)
    service = state["labels"]["com.open-trader.prediction-service"]
    service.update(loaded=False, pid=0, cwd="")
    state["prediction_service_ready"] = False
    state["lock_holders"] = []
    state["listeners"]["8769"] = None
    (runtime_root / "prediction-service-runtime.json").unlink(missing_ok=True)
    save()
    raise SystemExit(0)

save()
raise SystemExit(2)
'''


RACE_INSTRUMENTATION = r'''import json
import os
import sys
import time
from pathlib import Path

_loads = json.loads

def loads(value, *args, **kwargs):
    payload = _loads(value, *args, **kwargs)
    marker = Path(os.environ["FAKE_RACE_READ"])
    if (
        len(sys.argv) > 1
        and sys.argv[1] == "route-write"
        and isinstance(payload, dict)
        and payload.get("schema_version") \
            == "open_trader.frontend_gateway.prediction_route.v1"
        and payload.get("operation_id") == "bootstrap"
        and not marker.exists()
    ):
        marker.write_text("read\n", encoding="utf-8")
        deadline = time.monotonic() + 5
        release = Path(os.environ["FAKE_RACE_RELEASE"])
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("race release was not observed")
            time.sleep(0.01)
    return payload

json.loads = loads
'''


class CutoverHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.runtime = root / "runtime"
        self.bin = root / "bin"
        self.launch_agents = root / "LaunchAgents"
        self.race_read = root / "race-read"
        self.race_release = root / "race-release"
        self.sleeping_child_pid = root / "sleeping-child-pid"
        self.child_cleanup_started = root / "child-cleanup-started"
        self.post_route_signal = root / "post-route-signal"
        self.instrumentation = root / "instrumentation"
        for path in (self.repo / "scripts", self.runtime / "config", self.bin, self.launch_agents):
            path.mkdir(parents=True)
        self.instrumentation.mkdir()
        (self.instrumentation / "sitecustomize.py").write_text(
            RACE_INSTRUMENTATION, encoding="utf-8"
        )
        self.config = self.runtime / "config/prediction.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.route = self.runtime / "config/prediction-route.json"
        self.route.write_text(json.dumps({
            "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
            "mode": "legacy",
            "operation_id": "bootstrap",
            "updated_at": "2026-08-12T09:00:00+08:00",
        }), encoding="utf-8")
        self.state_path = root / "state.json"
        self.state_path.write_text(json.dumps({
            "calls": [],
            "command_paths": [],
            "fail_at": "",
            "states": ["legacy"],
            "max_prediction_owners": 1,
            "owner_available": False,
            "legacy_prediction_owner": "enabled",
            "prediction_service_ready": False,
            "inflight": 0,
            "extra_loaded_labels": [],
            "lock_holders": [2001],
            "public_status": "healthy",
            "public_health_status": "healthy",
            "public_readiness_status": "ready",
            "newer_evidence": {
                "schema_version": "open_trader.prediction_cutover.evidence.v1",
                "operation_id": "newer-operation",
                "target": "service",
                "expected_sha": SHA,
                "result": "failed",
                "failure_reason": "newer-operation-active",
                "downtime_started_at": "2026-08-12T11:00:00+08:00",
                "downtime_ended_at": "",
                "before": {
                    "gateway": {"pid": 1001, "cwd": str(self.repo), "git_sha": SHA, "listener": "127.0.0.1:8766"},
                    "legacy": {"pid": 2001, "cwd": str(self.repo), "git_sha": SHA, "listener": "127.0.0.1:8767"},
                    "service": {"pid": None, "cwd": str(self.repo), "git_sha": SHA, "listener": None},
                },
                "after": {
                    "gateway": {"pid": 1001, "cwd": str(self.repo), "git_sha": SHA, "listener": "127.0.0.1:8766"},
                    "legacy": {"pid": 2001, "cwd": str(self.repo), "git_sha": SHA, "listener": "127.0.0.1:8767"},
                    "service": {"pid": None, "cwd": str(self.repo), "git_sha": SHA, "listener": None},
                },
                "route": {"before_mode": "legacy", "after_mode": "maintenance", "inflight_before": 0, "inflight_after": 0},
                "owner": {"pid": 2001, "lock_holders": [2001], "available": False},
                "service_runtime": {"state": "unknown", "reader_generation": None, "contract_generation": None},
                "verification": {
                    "direct_backend": "failed",
                    "direct_state": False,
                    "direct_history": False,
                    "direct_preview_no_submit": False,
                    "public_state": False,
                    "public_history": False,
                    "public_preview_no_submit": False,
                },
            },
            "labels": {
                "com.open-trader.frontend-gateway": {
                    "loaded": True, "pid": 1001, "cwd": str(self.repo), "sha": SHA,
                    "argv": [sys.executable, "-m", "open_trader", "frontend-gateway"],
                    "plist": str(self.launch_agents / "com.open-trader.frontend-gateway.plist"),
                },
                "com.open-trader.legacy-dashboard": {
                    "loaded": True, "pid": 2001, "cwd": str(self.repo), "sha": SHA,
                    "argv": [
                        sys.executable, "-m", "open_trader", "dashboard",
                        "--prediction-owner", "enabled",
                    ],
                    "plist": str(self.launch_agents / "com.open-trader.legacy-dashboard.plist"),
                },
                "com.open-trader.prediction-service": {
                    "loaded": False, "pid": 0, "cwd": "", "sha": "",
                    "argv": [],
                    "plist": str(self.launch_agents / "com.open-trader.prediction-service.plist"),
                },
                "com.open-trader.account-sync-controller": {
                    "loaded": True, "pid": 4001, "cwd": str(self.repo), "sha": SHA,
                    "argv": [sys.executable, "-m", "open_trader", "account-sync-controller"],
                    "plist": str(self.launch_agents / "com.open-trader.account-sync-controller.plist"),
                },
            },
            "listeners": {
                "8766": {"pid": 1001}, "8767": {"pid": 2001},
                "8768": None, "8769": None,
            },
        }), encoding="utf-8")
        fake = self.bin / "fake-command"
        fake.write_text(FAKE_COMMAND, encoding="utf-8")
        fake.chmod(0o755)
        for name in ("git", "launchctl", "lsof", "curl", "ps", "owner-probe", "python"):
            shutil.copy2(fake, self.bin / name)
        for name in (
            "install_dashboard_launchd.sh",
            "install_prediction_service_launchd.sh",
            "uninstall_prediction_service_launchd.sh",
        ):
            shutil.copy2(fake, self.repo / "scripts" / name)

    @property
    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @property
    def evidence(self) -> dict[str, object]:
        return json.loads(
            (self.runtime / "prediction-cutover-evidence.json").read_text(
                encoding="utf-8"
            )
        )

    def configure(self, fail_at: str) -> None:
        state = self.state
        state["fail_at"] = fail_at
        if fail_at == "dirty_checkout":
            state["git_status"] = " M src/open_trader/example.py"
        elif fail_at == "wrong_sha":
            state["git_sha"] = "b" * 40
        elif fail_at == "wrong_runtime_sha":
            state["labels"]["com.open-trader.frontend-gateway"]["sha"] = "b" * 40
        elif fail_at == "unknown_listener":
            state["listeners"]["8769"] = {"pid": 9999}
        elif fail_at == "loaded_unknown_label":
            state["labels"]["com.open-trader.legacy-dashboard"]["cwd"] = "/unknown"
        elif fail_at == "unknown_relevant_label":
            state["extra_loaded_labels"] = ["com.open-trader.prediction-service-copy"]
        elif fail_at == "duplicate_relevant_label":
            state["extra_loaded_labels"] = ["com.open-trader.legacy-dashboard"]
        elif fail_at == "public_unavailable":
            state["public_status"] = "unavailable"
        elif fail_at == "public_degraded":
            state["public_health_status"] = "degraded"
        elif fail_at == "public_error":
            state["public_readiness_status"] = "error"
        elif fail_at == "legacy_argv_wrong":
            state["labels"]["com.open-trader.legacy-dashboard"]["argv"][-1] = "disabled"
        elif fail_at == "legacy_lock_holder_wrong":
            state["lock_holders"] = [9999]
        elif fail_at == "post_route_signal":
            state["post_route_ready"] = False
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_ROUTE": str(self.route),
            "FAKE_RUNTIME_ROOT": str(self.runtime),
            "FAKE_REPO_ROOT": str(self.repo),
            "FAKE_EVIDENCE": str(self.runtime / "prediction-cutover-evidence.json"),
            "FAKE_CONFIG": str(self.config),
            "FAKE_PYTHON": str(self.bin / "python"),
            "FAKE_LAUNCH_AGENTS": str(self.launch_agents),
            "FAKE_WAIT_SECONDS": "2",
            "FAKE_RACE_READ": str(self.race_read),
            "FAKE_RACE_RELEASE": str(self.race_release),
            "FAKE_SLEEPING_CHILD_PID": str(self.sleeping_child_pid),
            "FAKE_CHILD_CLEANUP_STARTED": str(self.child_cleanup_started),
            "FAKE_POST_ROUTE_SIGNAL": str(self.post_route_signal),
            "FAKE_INSTRUMENTATION": str(self.instrumentation),
            "FAKE_SHA": SHA,
            "FAKE_REAL_PYTHON": sys.executable,
            "GIT_BIN": str(self.bin / "git"),
            "LAUNCHCTL_BIN": str(self.bin / "launchctl"),
            "LSOF_BIN": str(self.bin / "lsof"),
            "CURL_BIN": str(self.bin / "curl"),
            "PS_BIN": str(self.bin / "ps"),
            "OWNER_PROBE_BIN": str(self.bin / "owner-probe"),
        }

    def command(self, target: str, *extra: str) -> list[str]:
        return [
                "bash", str(SCRIPT), "--target", target,
                "--repo-root", str(self.repo),
                "--runtime-root", str(self.runtime),
                "--python", str(self.bin / "python"),
                "--expected-sha", SHA,
                "--prediction-config", str(self.config),
                "--launch-agents-dir", str(self.launch_agents),
                "--wait-seconds", "2",
                *extra,
            ]

    def run(
        self, target: str, *extra: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(target, *extra),
            text=True,
            capture_output=True,
            env=self.environment(),
        )


@pytest.fixture
def harness(tmp_path: Path) -> CutoverHarness:
    return CutoverHarness(tmp_path)


def wait_for_path(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


def test_service_happy_path(harness: CutoverHarness) -> None:
    result = harness.run("service")

    assert result.returncode == 0, result.stderr
    assert harness.state["states"] == ["legacy", "maintenance", "service"]
    assert harness.state["max_prediction_owners"] == 1
    assert harness.state["legacy_prediction_owner"] == "disabled"
    assert harness.state["prediction_service_ready"] is True
    assert harness.evidence["result"] == "ready"
    assert harness.evidence["downtime_started_at"]
    assert harness.evidence["downtime_ended_at"]
    assert str(
        harness.repo / "scripts/install_dashboard_launchd.sh"
    ) in harness.state["command_paths"]
    assert str(
        harness.repo / "scripts/install_prediction_service_launchd.sh"
    ) in harness.state["command_paths"]
    assert [
        call for call in harness.state["calls"]
        if call[0] == "install_dashboard_launchd.sh"
    ][0][1:] == [
        "--mode", "legacy", "--prediction-owner", "disabled",
        "--repo-root", str(harness.repo),
        "--runtime-root", str(harness.runtime),
        "--python", str(harness.bin / "python"),
        "--launch-agents-dir", str(harness.launch_agents),
        "--wait-seconds", "2",
    ]


def test_service_absent_route_bootstraps_stack_inside_cutover(
    harness: CutoverHarness,
) -> None:
    harness.route.unlink()

    result = harness.run("service")

    assert result.returncode == 0, result.stderr
    assert harness.state["states"] == ["legacy", "maintenance", "service"]
    stack_calls = [
        call for call in harness.state["calls"]
        if call[0] == "install_dashboard_launchd.sh"
    ]
    assert stack_calls[0][1:5] == ["--mode", "stack", "--prediction-owner", "enabled"]
    assert stack_calls[1][1:5] == ["--mode", "legacy", "--prediction-owner", "disabled"]
    assert harness.state["max_prediction_owners"] == 1
    assert harness.evidence["route"]["before_mode"] == "absent"
    assert [
        call for call in harness.state["calls"]
        if call[0] == "install_prediction_service_launchd.sh"
    ][0][1:] == [
        "--mode", "production", "--repo-root", str(harness.repo),
        "--runtime-root", str(harness.runtime),
        "--python", str(harness.bin / "python"),
        "--config", str(harness.config),
        "--launch-agents-dir", str(harness.launch_agents),
        "--wait-seconds", "2", "--expected-sha", SHA,
    ]


def test_absent_bootstrap_proves_old_gateway_absent_before_route_write(
    harness: CutoverHarness,
) -> None:
    harness.route.unlink()

    result = harness.run("service")

    assert result.returncode == 0, result.stderr
    calls = harness.state["calls"]
    bootout = next(
        index for index, call in enumerate(calls)
        if call[:2] == ["launchctl", "bootout"]
        and call[-1].endswith("com.open-trader.frontend-gateway")
    )
    route_bootstrap = next(
        index for index, call in enumerate(calls)
        if call[:3] == ["python", "-", "route-bootstrap"]
    )
    assert bootout < route_bootstrap
    assert not any(
        call[0] == "curl" and ":8766/api/prediction-arbitrage" in " ".join(call[1:])
        for call in calls[:route_bootstrap]
    )


@pytest.mark.parametrize("failure", ["gateway_bootout", "bootstrap_listener_inspection_error"])
def test_absent_bootstrap_failure_keeps_route_absent(
    harness: CutoverHarness, failure: str
) -> None:
    harness.route.unlink()
    harness.configure(failure)

    result = harness.run("service")

    assert result.returncode == 1
    assert not harness.route.exists()
    assert not any(call[:3] == ["python", "-", "route-bootstrap"] for call in harness.state["calls"])
    assert not any(call[0] == "install_dashboard_launchd.sh" for call in harness.state["calls"])


@pytest.mark.parametrize(
    ("failure", "forbidden_later_command"),
    [
        ("route_maintenance_write", "curl"),
        ("gateway_maintenance", "install_dashboard_launchd.sh"),
        ("inflight_drain", "install_dashboard_launchd.sh"),
        ("legacy_restart", "install_prediction_service_launchd.sh"),
        ("old_legacy_pid", "install_prediction_service_launchd.sh"),
        ("ps_inspection_error", "install_prediction_service_launchd.sh"),
        ("owner_lock", "install_prediction_service_launchd.sh"),
        ("service_installer", "route:service"),
        ("service_generation", "route:service"),
        ("service_reconcile", "route:service"),
        ("health_evidence", "route:service"),
        ("runtime_evidence", "route:service"),
        ("generation_evidence", "route:service"),
        ("runtime_schema", "route:service"),
        ("service_route_write", "public_contract"),
        ("gateway_service", "public_contract"),
        ("gateway_service_health", "public_contract"),
        ("public_contract", "evidence:ready"),
        ("evidence_write", "none"),
    ],
)
def test_service_failure_stays_in_maintenance_without_running_later_commands(
    harness: CutoverHarness,
    failure: str,
    forbidden_later_command: str,
) -> None:
    harness.configure(failure)

    result = harness.run("service")

    assert result.returncode == 1
    assert json.loads(harness.route.read_text(encoding="utf-8"))["mode"] == "maintenance"
    assert harness.state["max_prediction_owners"] <= 1
    assert harness.evidence["result"] == "failed"
    calls = harness.state["calls"]
    if forbidden_later_command == "curl":
        route_write = next(
            index for index, call in enumerate(calls)
            if call[:3] == ["python", "-", "route-write"]
        )
        assert not any(call[0] == "curl" for call in calls[route_write + 1:])
    elif forbidden_later_command == "route:service":
        assert not any(
            call[:3] == ["python", "-", "route-write"] and call[4] == "service"
            for call in calls
        )
    elif forbidden_later_command == "public_contract":
        assert not any(
            call[0] == "curl"
            and ("--cookie-jar" in call or "--cookie" in call)
            and any("/api/prediction-arbitrage/state" in arg for arg in call[1:])
            for call in calls
        )
    elif forbidden_later_command == "evidence:ready":
        assert not any(
            call[:3] == ["python", "-", "evidence-write"] and call[9] == "ready"
            for call in calls
        )
    elif forbidden_later_command != "none":
        assert not any(call[0] == forbidden_later_command for call in calls)
    assert not any(
        call[0] == "install_dashboard_launchd.sh"
        and "enabled" in call
        for call in calls
    )


def test_service_to_legacy_rollback_uses_one_owner(harness: CutoverHarness) -> None:
    harness.run("service").check_returncode()

    result = harness.run("legacy")

    assert result.returncode == 0, result.stderr
    assert harness.state["states"] == [
        "legacy", "maintenance", "service", "maintenance", "legacy"
    ]
    assert harness.state["max_prediction_owners"] == 1
    assert harness.state["legacy_prediction_owner"] == "enabled"
    assert harness.state["prediction_service_ready"] is False
    assert harness.evidence["result"] == "ready"
    assert harness.evidence["target"] == "legacy"
    uninstall_calls = [
        call for call in harness.state["calls"]
        if call[0] == "uninstall_prediction_service_launchd.sh"
    ]
    assert len(uninstall_calls) == 1
    assert uninstall_calls[0][1:] == [
        "--mode", "production",
        "--runtime-root", str(harness.runtime),
        "--python", str(harness.bin / "python"),
        "--launch-agents-dir", str(harness.launch_agents),
    ]
    legacy_enable = [
        call for call in harness.state["calls"]
        if call[0] == "install_dashboard_launchd.sh" and "enabled" in call
    ]
    assert legacy_enable[-1][1:] == [
        "--mode", "legacy", "--prediction-owner", "enabled",
        "--repo-root", str(harness.repo),
        "--runtime-root", str(harness.runtime),
        "--python", str(harness.bin / "python"),
        "--launch-agents-dir", str(harness.launch_agents),
        "--wait-seconds", "2",
    ]
    assert not any(
        call[0] == "ps" and "command=" in call
        for call in harness.state["calls"]
    )
    assert any(
        call[0] == "lsof" and call[-1].endswith("runtime.lock")
        for call in harness.state["calls"]
    )


def test_failed_service_then_separate_rollback_recovers_maintenance(
    harness: CutoverHarness,
) -> None:
    harness.configure("legacy_restart")
    failed = harness.run("service")
    assert failed.returncode == 1
    assert json.loads(harness.route.read_text(encoding="utf-8"))["mode"] == "maintenance"

    harness.configure("")
    recovered = harness.run("legacy")

    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(harness.route.read_text(encoding="utf-8"))["mode"] == "legacy"
    assert harness.evidence["result"] == "ready"
    assert harness.evidence["target"] == "legacy"
    assert not any(
        call[0] == "uninstall_prediction_service_launchd.sh"
        for call in harness.state["calls"]
    )


def test_evidence_snapshots_are_observed_before_and_after_cutover(
    harness: CutoverHarness,
) -> None:
    harness.run("service").check_returncode()

    evidence = harness.evidence

    assert evidence["before"] != evidence["after"]
    assert evidence["before"]["service"]["pid"] is None
    assert evidence["after"]["service"]["pid"] == 3001
    assert evidence["before"]["legacy"]["pid"] == 2001
    assert evidence["after"]["legacy"]["pid"] == 2002
    assert evidence["route"]["inflight_before"] == 0
    assert evidence["route"]["inflight_after"] == 0


def test_repeating_completed_target_preserves_evidence(harness: CutoverHarness) -> None:
    harness.run("service").check_returncode()
    evidence_before = (
        harness.runtime / "prediction-cutover-evidence.json"
    ).read_bytes()
    calls_before = len(harness.state["calls"])

    result = harness.run("service")

    assert result.returncode == 0, result.stderr
    assert "already ready" in result.stdout
    assert (harness.runtime / "prediction-cutover-evidence.json").read_bytes() == evidence_before
    later_calls = harness.state["calls"][calls_before:]
    assert not any(call[0] in {
        "install_dashboard_launchd.sh",
        "install_prediction_service_launchd.sh",
        "uninstall_prediction_service_launchd.sh",
    } for call in later_calls)
    assert not any(call[:3] == ["python", "-", "route-write"] for call in later_calls)


@pytest.mark.parametrize("evidence_state", ["missing", "malformed", "boolean", "stale"])
def test_repeating_completed_target_fails_closed_without_valid_evidence(
    harness: CutoverHarness,
    evidence_state: str,
) -> None:
    harness.run("service").check_returncode()
    evidence = harness.runtime / "prediction-cutover-evidence.json"
    if evidence_state == "missing":
        evidence.unlink()
    elif evidence_state == "malformed":
        evidence.write_text("{}", encoding="utf-8")
    else:
        payload = harness.evidence
        if evidence_state == "boolean":
            payload["route"]["inflight_before"] = True
        else:
            payload["operation_id"] = "stale-operation"
        evidence.write_text(json.dumps(payload), encoding="utf-8")
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert "already ready" not in result.stdout
    assert harness.route.read_bytes() == route_before


def test_repeating_completed_target_requires_operation_lock(
    harness: CutoverHarness,
) -> None:
    harness.run("service").check_returncode()
    lock = harness.runtime / "config/.prediction-cutover.lock"
    lock.mkdir()
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert "another prediction cutover is active" in result.stderr
    assert "already ready" not in result.stdout
    assert harness.route.read_bytes() == route_before


@pytest.mark.parametrize(
    "failure",
    ["health_evidence", "public_unavailable", "public_degraded", "public_error"],
)
def test_repeating_service_reproves_runtime_and_public_contract(
    harness: CutoverHarness,
    failure: str,
) -> None:
    harness.run("service").check_returncode()
    harness.configure(failure)
    route_before = harness.route.read_bytes()
    evidence_before = (
        harness.runtime / "prediction-cutover-evidence.json"
    ).read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert "already ready" not in result.stdout
    assert harness.route.read_bytes() == route_before
    assert (
        harness.runtime / "prediction-cutover-evidence.json"
    ).read_bytes() == evidence_before


@pytest.mark.parametrize("failure", ["legacy_argv_wrong", "legacy_lock_holder_wrong"])
def test_repeating_legacy_reproves_pid_argv_and_lock_owner(
    harness: CutoverHarness,
    failure: str,
) -> None:
    harness.run("service").check_returncode()
    harness.run("legacy").check_returncode()
    harness.configure(failure)

    result = harness.run("legacy")

    assert result.returncode == 1
    assert "already ready" not in result.stdout


def test_stale_operation_cannot_overwrite_active_maintenance(
    harness: CutoverHarness,
) -> None:
    harness.configure("stale_operation")

    original_evidence = harness.state["newer_evidence"]
    result = harness.run("service")

    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert route["mode"] == "maintenance"
    assert route["operation_id"] == "newer-operation"
    assert harness.evidence == original_evidence
    assert harness.state["max_prediction_owners"] == 1
    assert not any(
        call[0] == "install_dashboard_launchd.sh" for call in harness.state["calls"]
    )


def test_initial_maintenance_cas_cannot_overwrite_newer_route_or_evidence(
    harness: CutoverHarness,
) -> None:
    harness.configure("initial_stale_operation")
    expected_evidence = harness.state["newer_evidence"]

    result = harness.run("service")

    assert result.returncode == 1
    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert route["mode"] == "maintenance"
    assert route["operation_id"] == "newer-operation"
    assert harness.evidence == expected_evidence


def test_state_transition_lock_serializes_route_and_evidence_cas(
    harness: CutoverHarness,
) -> None:
    harness.configure("locked_state_race")
    cutover = subprocess.Popen(
        harness.command("service"),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=harness.environment(),
    )
    competitor: subprocess.Popen[str] | None = None
    try:
        assert wait_for_path(harness.race_read), "stale writer did not pause after read"
        competitor = subprocess.Popen(
            [
                sys.executable,
                "-c",
                r'''
import fcntl, json, os, sys
from pathlib import Path
from tempfile import NamedTemporaryFile

lock_path, route_raw, evidence_raw, route_json, evidence_json = sys.argv[1:]

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
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

with open(lock_path, "a+", encoding="utf-8") as state_lock:
    fcntl.flock(state_lock, fcntl.LOCK_EX)
    atomic_write(Path(route_raw), json.loads(route_json))
    atomic_write(Path(evidence_raw), json.loads(evidence_json))
''',
                str(harness.runtime / "config/.prediction-cutover-state.lock"),
                str(harness.route),
                str(harness.runtime / "prediction-cutover-evidence.json"),
                json.dumps({
                    "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                    "mode": "maintenance",
                    "operation_id": "newer-operation",
                    "updated_at": "2026-08-12T11:00:00+08:00",
                }),
                json.dumps(harness.state["newer_evidence"]),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        competitor_was_serialized = competitor.poll() is None
        harness.race_release.write_text("continue\n", encoding="utf-8")
        stdout, stderr = cutover.communicate(timeout=20)
        competitor_stdout, competitor_stderr = competitor.communicate(timeout=5)
    finally:
        if cutover.poll() is None:
            cutover.kill()
            cutover.wait()
        if competitor is not None and competitor.poll() is None:
            competitor.kill()
            competitor.wait()

    assert competitor_was_serialized, competitor_stderr
    assert cutover.returncode == 1, (stdout, stderr)
    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert route["operation_id"] == "newer-operation"
    assert harness.evidence == harness.state["newer_evidence"]


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_signal_waits_for_mutating_child_cleanup_before_releasing_lock(
    harness: CutoverHarness,
    interrupt_signal: signal.Signals,
) -> None:
    harness.configure("service_installer_interrupt")
    cutover = subprocess.Popen(
        harness.command("service"),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=harness.environment(),
    )
    child_pid = 0
    try:
        assert wait_for_path(harness.sleeping_child_pid, timeout=20)
        child_pid = int(harness.sleeping_child_pid.read_text(encoding="utf-8"))
        operation_lock = harness.runtime / "config/.prediction-cutover.lock"
        assert operation_lock.is_dir()
        cutover.send_signal(interrupt_signal)
        cleanup_started = wait_for_path(harness.child_cleanup_started, timeout=3)
        lock_during_cleanup = operation_lock.is_dir()
        stdout, stderr = cutover.communicate(timeout=20)
    finally:
        if cutover.poll() is None:
            cutover.kill()
            cutover.wait()
        if child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert cleanup_started, (stdout, stderr)
    assert lock_during_cleanup
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert route["mode"] == "maintenance"
    assert not (harness.runtime / "config/.prediction-cutover.lock").exists()
    assert not any(
        call[:3] == ["python", "-", "route-write"] and call[4] == "service"
        for call in harness.state["calls"]
    )
    assert not any(
        call[:3] == ["python", "-", "evidence-write"] and call[9] == "ready"
        for call in harness.state["calls"]
    )


@pytest.mark.parametrize("target", ["service", "legacy"])
def test_signal_after_route_before_evidence_fails_closed(
    harness: CutoverHarness, target: str
) -> None:
    if target == "legacy":
        harness.run("service").check_returncode()
    harness.configure("post_route_signal")
    cutover = subprocess.Popen(
        harness.command(target),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=harness.environment(),
    )
    try:
        assert wait_for_path(harness.post_route_signal, timeout=20)
        cutover.send_signal(signal.SIGTERM)
        stdout, stderr = cutover.communicate(timeout=20)
    finally:
        if cutover.poll() is None:
            cutover.kill()
            cutover.wait()

    assert cutover.returncode == 143, (stdout, stderr)
    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert route["mode"] == "maintenance"
    assert harness.evidence["result"] == "failed"
    assert harness.evidence["failure_reason"] == "interrupted"


@pytest.mark.parametrize(
    "failure",
    [
        "dirty_checkout",
        "wrong_sha",
        "wrong_runtime_sha",
        "unknown_listener",
        "loaded_unknown_label",
        "unknown_relevant_label",
        "duplicate_relevant_label",
        "listener_inspection_error",
        "label_inspection_error",
    ],
)
def test_preflight_rejects_unverified_runtime_before_maintenance(
    harness: CutoverHarness,
    failure: str,
) -> None:
    harness.configure(failure)
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_bytes() == route_before
    assert not (harness.runtime / "prediction-cutover-evidence.json").exists()
    assert not any(
        call[0] in {
            "install_dashboard_launchd.sh",
            "install_prediction_service_launchd.sh",
            "uninstall_prediction_service_launchd.sh",
        }
        for call in harness.state["calls"]
    )
    assert not any(
        call[:3] == ["python", "-", "route-write"]
        for call in harness.state["calls"]
    )


def test_preflight_rejects_missing_tool_before_maintenance(
    harness: CutoverHarness,
) -> None:
    (harness.bin / "lsof").unlink()
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_bytes() == route_before
    assert not (harness.runtime / "prediction-cutover-evidence.json").exists()


def test_preflight_rejects_missing_prediction_config(
    harness: CutoverHarness,
) -> None:
    harness.config.unlink()
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_bytes() == route_before


def test_preflight_rejects_malformed_route_record(
    harness: CutoverHarness,
) -> None:
    harness.route.write_text("{}", encoding="utf-8")

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_text(encoding="utf-8") == "{}"


def test_preflight_rejects_malformed_evidence_record(
    harness: CutoverHarness,
) -> None:
    evidence = harness.runtime / "prediction-cutover-evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_bytes() == route_before
    assert evidence.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("path_name", ["route", "evidence"])
def test_preflight_rejects_symlinked_state_paths(
    harness: CutoverHarness,
    path_name: str,
) -> None:
    if path_name == "route":
        path = harness.route
        target = harness.root / "outside-route.json"
        target.write_bytes(path.read_bytes())
    else:
        path = harness.runtime / "prediction-cutover-evidence.json"
        target = harness.root / "outside-evidence.json"
        target.write_text("{}", encoding="utf-8")
    path.unlink(missing_ok=True)
    path.symlink_to(target)

    result = harness.run("service")

    assert result.returncode == 1
    assert path.is_symlink()
    assert target.read_bytes() != b""


def test_concurrent_cutover_lock_rejects_before_route_write(
    harness: CutoverHarness,
) -> None:
    lock = harness.runtime / "config/.prediction-cutover.lock"
    lock.mkdir()
    route_before = harness.route.read_bytes()

    result = harness.run("service")

    assert result.returncode == 1
    assert harness.route.read_bytes() == route_before
    assert lock.is_dir()


def test_unknown_argument_is_rejected_before_runtime_inspection(
    harness: CutoverHarness,
) -> None:
    result = harness.run("service", "--unexpected", "value")

    assert result.returncode == 2
    assert harness.state["calls"] == []


def test_issue45_runtime_artifacts_are_ignored_by_git() -> None:
    paths = [
        "config/prediction-route.json",
        "config/.prediction-cutover.lock",
        "config/.prediction-cutover-state.lock",
        "prediction-cutover-evidence.json",
        "prediction-service-runtime.json",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input="\n".join(paths) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == paths


def test_evidence_excludes_config_and_request_secrets(harness: CutoverHarness) -> None:
    secret = "token-cookie-wallet-request-body"
    harness.config.write_text(json.dumps({"token": secret}), encoding="utf-8")

    harness.run("service").check_returncode()

    evidence_text = (
        harness.runtime / "prediction-cutover-evidence.json"
    ).read_text(encoding="utf-8")
    assert secret not in evidence_text
    assert set(json.loads(evidence_text)) == {
        "schema_version", "operation_id", "target", "expected_sha", "result",
        "failure_reason", "downtime_started_at", "downtime_ended_at",
        "before", "after", "route", "owner", "service_runtime", "verification",
    }


def test_service_child_timeout_fails_closed_with_no_later_mutation(
    harness: CutoverHarness,
) -> None:
    harness.configure("service_installer_timeout")

    result = harness.run("service")

    assert result.returncode == 1
    assert json.loads(harness.route.read_text(encoding="utf-8"))["mode"] == "maintenance"
    assert harness.state["prediction_service_ready"] is False
    assert harness.evidence["result"] == "failed"


@pytest.mark.parametrize(
    ("failure", "forbidden_later_command"),
    [
        ("service_uninstall", "legacy_enable"),
        ("owner_lock", "legacy_enable"),
        ("legacy_restart", "route:legacy"),
        ("legacy_contract", "route:legacy"),
        ("legacy_owner_lock", "route:legacy"),
        ("legacy_route_write", "gateway_legacy"),
        ("gateway_legacy", "evidence:ready"),
        ("public_contract", "evidence:ready"),
        ("evidence_write", "none"),
    ],
)
def test_rollback_failure_stays_in_maintenance_without_auto_service_restart(
    harness: CutoverHarness,
    failure: str,
    forbidden_later_command: str,
) -> None:
    harness.run("service").check_returncode()
    harness.configure(failure)
    calls_before = len(harness.state["calls"])

    result = harness.run("legacy")

    assert result.returncode == 1
    assert json.loads(harness.route.read_text(encoding="utf-8"))["mode"] == "maintenance"
    assert harness.state["max_prediction_owners"] <= 1
    assert harness.evidence["result"] == "failed"
    calls = harness.state["calls"][calls_before:]
    assert not any(call[0] == "install_prediction_service_launchd.sh" for call in calls)
    if forbidden_later_command == "legacy_enable":
        assert not any(
            call[0] == "install_dashboard_launchd.sh" and "enabled" in call
            for call in calls
        )
    elif failure == "legacy_owner_lock":
        legacy_enable_index = next(
            index for index, call in enumerate(calls)
            if call[0] == "install_dashboard_launchd.sh" and "enabled" in call
        )
        owner_probe_index = next(
            index for index, call in enumerate(calls)
            if call[0] == "owner-probe" and index > legacy_enable_index
        )
        assert owner_probe_index > legacy_enable_index
        assert not any(
            call[:3] == ["python", "-", "route-write"] and call[4] == "legacy"
            for call in calls[owner_probe_index + 1:]
        )
    elif forbidden_later_command == "route:legacy":
        assert not any(
            call[:3] == ["python", "-", "route-write"] and call[4] == "legacy"
            for call in calls
        )
    elif forbidden_later_command == "gateway_legacy":
        route_write = next(
            index for index, call in enumerate(calls)
            if call[:3] == ["python", "-", "route-write"] and call[4] == "legacy"
        )
        assert not any(
            call[0] == "curl"
            and any(":8766/healthz" in arg for arg in call[1:])
            for call in calls[route_write + 1:]
        )
    elif forbidden_later_command == "evidence:ready":
        assert not any(
            call[:3] == ["python", "-", "evidence-write"] and call[9] == "ready"
            for call in calls
        )
