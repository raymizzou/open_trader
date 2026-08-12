from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_prediction_service.sh"
SHA = "a" * 40


FAKE_COMMAND = r'''#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import sys
import time
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
    route = json.loads(route_path.read_text(encoding="utf-8"))
    mode = route["mode"]
    if state["states"][-1] != mode:
        state["states"].append(mode)
    return route

if command == "python":
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
    save()
    completed = subprocess.run(
        [os.environ["FAKE_REAL_PYTHON"], *sys.argv[1:]],
        input=source,
        text=True,
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
        print("services = {")
        for pid, label in labels:
            print(f"\t{pid} = {label}")
        print("}")
        save()
        raise SystemExit(0)
    label = sys.argv[-1].rsplit("/", 1)[-1]
    item = state["labels"].get(label)
    if sys.argv[1] == "print" and item and item["loaded"]:
        print(f"path = {item['plist']}")
        print(f"working directory = {item['cwd']}")
        print(f"pid = {item['pid']}")
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
    if "command=" in sys.argv:
        for item in state["labels"].values():
            if item["loaded"] and str(item["pid"]) == pid:
                print(shlex.join(item["argv"]))
                save()
                raise SystemExit(0)
        save()
        raise SystemExit(1)
    present = any(
        item["loaded"] and str(item["pid"]) == pid
        for item in state["labels"].values()
    )
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
            "prediction_upstream_status": (
                "ok" if route["mode"] == "service" and state["prediction_service_ready"]
                else "not_selected"
            ),
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
        }))
    save()
    raise SystemExit(0)

if command == "install_dashboard_launchd.sh":
    expected = [
        "--mode", "legacy", "--prediction-owner", option("--prediction-owner"),
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
    save()
    raise SystemExit(0)

save()
raise SystemExit(2)
'''


class CutoverHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.runtime = root / "runtime"
        self.bin = root / "bin"
        self.launch_agents = root / "LaunchAgents"
        for path in (self.repo / "scripts", self.runtime / "config", self.bin, self.launch_agents):
            path.mkdir(parents=True)
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
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def run(
        self, target: str, *extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
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
            "FAKE_SHA": SHA,
            "FAKE_REAL_PYTHON": sys.executable,
            "GIT_BIN": str(self.bin / "git"),
            "LAUNCHCTL_BIN": str(self.bin / "launchctl"),
            "LSOF_BIN": str(self.bin / "lsof"),
            "CURL_BIN": str(self.bin / "curl"),
            "PS_BIN": str(self.bin / "ps"),
            "OWNER_PROBE_BIN": str(self.bin / "owner-probe"),
        }
        return subprocess.run(
            [
                "bash", str(SCRIPT), "--target", target,
                "--repo-root", str(self.repo),
                "--runtime-root", str(self.runtime),
                "--python", str(self.bin / "python"),
                "--expected-sha", SHA,
                "--prediction-config", str(self.config),
                "--launch-agents-dir", str(self.launch_agents),
                "--wait-seconds", "2",
                *extra,
            ],
            text=True,
            capture_output=True,
            env=env,
        )


@pytest.fixture
def harness(tmp_path: Path) -> CutoverHarness:
    return CutoverHarness(tmp_path)


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
    assert any(call[0] == "ps" and "command=" in call for call in harness.state["calls"])
    assert any(
        call[0] == "lsof" and call[-1].endswith("runtime.lock")
        for call in harness.state["calls"]
    )


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


@pytest.mark.parametrize("evidence_state", ["missing", "malformed"])
def test_repeating_completed_target_fails_closed_without_valid_evidence(
    harness: CutoverHarness,
    evidence_state: str,
) -> None:
    harness.run("service").check_returncode()
    evidence = harness.runtime / "prediction-cutover-evidence.json"
    if evidence_state == "missing":
        evidence.unlink()
    else:
        evidence.write_text("{}", encoding="utf-8")
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
