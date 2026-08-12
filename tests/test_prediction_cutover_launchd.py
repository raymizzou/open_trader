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
import subprocess
import sys
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
    result = sys.argv[7] if tag == "evidence-write" else ""
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
    save()
    raise SystemExit(0 if present else 1)

if command == "owner-probe":
    if state["fail_at"] in {"owner_lock", "legacy_owner_lock"}:
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
        print(json.dumps({
            "status": "ready",
            "health": {"status": "ok"},
            "readiness": {"status": "ready"},
            "events": [],
            "opportunities": [],
        }))
    save()
    raise SystemExit(0)

if command == "install_dashboard_launchd.sh":
    owner = option("--prediction-owner")
    if state["fail_at"] == "legacy_restart":
        save()
        raise SystemExit(1)
    legacy = state["labels"]["com.open-trader.legacy-dashboard"]
    if state["fail_at"] != "old_legacy_pid":
        legacy["pid"] += 1
    legacy["cwd"] = option("--repo-root")
    state["legacy_prediction_owner"] = owner
    state["listeners"]["8767"] = {"pid": legacy["pid"]}
    save()
    raise SystemExit(0)

if command == "install_prediction_service_launchd.sh":
    if state["fail_at"] in {"service_installer", "service_generation", "service_reconcile"}:
        save()
        raise SystemExit(1)
    service = state["labels"]["com.open-trader.prediction-service"]
    service.update(loaded=True, pid=3001, cwd=option("--repo-root"), sha=os.environ["FAKE_SHA"])
    state["prediction_service_ready"] = True
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
    if state["fail_at"] == "service_uninstall":
        save()
        raise SystemExit(1)
    service = state["labels"]["com.open-trader.prediction-service"]
    service.update(loaded=False, pid=0, cwd="")
    state["prediction_service_ready"] = False
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
            "labels": {
                "com.open-trader.frontend-gateway": {
                    "loaded": True, "pid": 1001, "cwd": str(self.repo), "sha": SHA,
                    "plist": str(self.launch_agents / "com.open-trader.frontend-gateway.plist"),
                },
                "com.open-trader.legacy-dashboard": {
                    "loaded": True, "pid": 2001, "cwd": str(self.repo), "sha": SHA,
                    "plist": str(self.launch_agents / "com.open-trader.legacy-dashboard.plist"),
                },
                "com.open-trader.prediction-service": {
                    "loaded": False, "pid": 0, "cwd": "", "sha": "",
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
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def run(
        self, target: str, *extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_ROUTE": str(self.route),
            "FAKE_RUNTIME_ROOT": str(self.runtime),
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
            call[:3] == ["python", "-", "evidence-write"] and call[7] == "ready"
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


def test_stale_operation_cannot_overwrite_active_maintenance(
    harness: CutoverHarness,
) -> None:
    harness.configure("stale_operation")

    result = harness.run("service")

    route = json.loads(harness.route.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert route["mode"] == "maintenance"
    assert route["operation_id"] == "newer-operation"
    assert harness.state["max_prediction_owners"] == 1
    assert not any(
        call[0] == "install_dashboard_launchd.sh" for call in harness.state["calls"]
    )


@pytest.mark.parametrize(
    "failure",
    [
        "dirty_checkout",
        "wrong_sha",
        "wrong_runtime_sha",
        "unknown_listener",
        "loaded_unknown_label",
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
            call[:3] == ["python", "-", "evidence-write"] and call[7] == "ready"
            for call in calls
        )
