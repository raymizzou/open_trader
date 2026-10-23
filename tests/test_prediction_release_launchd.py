from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import signal
import stat

from timing_support import supervised_process
import sys
import time
from pathlib import Path

import pytest

from open_trader.prediction_release import (
    load_prediction_release_manifest,
    load_prediction_runtime_record,
)


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.open-trader.prediction-service"


@dataclass(frozen=True)
class ReleaseCheckout:
    path: Path
    sha: str
    reader_generation: int = 1
    contract_generation: int = 1


class CommandCalls:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def named(self, command: str) -> list[str]:
        return [line for line in self.all() if line.split(" ", 1)[0] == command]

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


FAKE_COMMAND_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
calls_path = Path(os.environ["FAKE_CALLS"])
state = json.loads(state_path.read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(command + " " + " ".join(sys.argv[1:]) + "\n")

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def observation_state():
    if state.get("lock_barrier_phase") == "frozen":
        return {**state, **state["lock_barrier_snapshot"]}
    return state

if command == "gh":
    from urllib.parse import urlsplit, parse_qs
    path = sys.argv[-1]
    assert sys.argv[1:3] == ["api", "--hostname"] and sys.argv[3] == "github.com"
    assert "--method" in sys.argv and sys.argv[sys.argv.index("--method") + 1] == "GET"
    assert path.startswith("repos/raymizzou/open_trader/")
    fixture = Path(os.environ["FAKE_GITHUB_DIR"])
    data = json.loads((fixture / "api.json").read_text())
    if path.endswith("/zip"):
        artifact_id = path.split("/")[-2]
        sys.stdout.buffer.write((fixture / (artifact_id + ".zip")).read_bytes())
    else:
        route = path.split("?", 1)[0]
        if "/compare/" in route:
            result = {"status": "identical", "merge_base_commit": {"sha": data["tip"]}}
        elif route.endswith("/git/ref/heads/main"):
            result = {"object": {"sha": data["tip"]}}
        elif route.endswith("/actions/workflows/ci.yml"):
            result = {"id": 11, "path": ".github/workflows/ci.yml", "state": "active"}
        elif route.endswith("/actions/runs/22"):
            result = data["run"]
        else:
            key = ("workflow_runs" if route.endswith("/actions/workflows/11/runs") else
                   "jobs" if route.endswith("/jobs") else
                   "check_runs" if route.endswith("/check-runs") else
                   "artifacts" if route.endswith("/artifacts") else None)
            assert key, path
            page = int(parse_qs(urlsplit(path).query).get("page", ["1"])[0])
            result = {"total_count": len(data[key]), key: data[key][(page-1)*100:page*100]}
        print(json.dumps(result))
    raise SystemExit(0)

if command == "launchctl":
    action = sys.argv[1]
    if action == "print":
        barrier_count = state.get("print_barrier_count")
        if barrier_count is not None:
            state["print_count"] = int(state.get("print_count", 0)) + 1
            save()
            if state["print_count"] == int(barrier_count):
                marker = Path(state["print_barrier_marker"])
                release = Path(state["print_barrier_release"])
                marker.write_text("ready", encoding="utf-8")
                while not release.exists():
                    time.sleep(0.01)
                state = json.loads(state_path.read_text(encoding="utf-8"))
        grace_remaining = int(state.get("label_absence_grace_polls", 0))
        if grace_remaining > 0:
            state["label_absence_grace_polls"] = grace_remaining - 1
            save()
            view = {
                **state,
                "loaded": True,
                "pid": state.get("label_absence_grace_pid", state.get("pid", 0)),
            }
        else:
            view = observation_state()
        if view["loaded"]:
            print(f"path = {view['plist']}")
            print(f"working directory = {view.get('label_cwd', view['cwd'])}")
            print(f"stdout path = {view['stdout_log']}")
            print(f"stderr path = {view['stderr_log']}")
            print(f"pid = {view['pid']}")
            arguments = view.get("arguments", [])
            if arguments:
                print("arguments = {")
                for argument in arguments:
                    print(f"  {argument}")
                print("}")
            raise SystemExit(0)
        print("Could not find service", file=sys.stderr)
        raise SystemExit(113)
    if action == "bootout":
        case = state["case"]
        old_pid = state["pid"]
        if state.get("runtime_lock_directory_after_bootout"):
            lock_path = Path(os.environ["FAKE_DATA_DIR"]) / "prediction_arbitrage/runtime.lock"
            lock_path.unlink()
            lock_path.mkdir()
        state.setdefault("bootout_pids", []).append(old_pid)
        grace_polls = int(state.get("bootout_grace_polls", 0))
        state.update(loaded=False, pid=0, cwd="", listener=False,
                     owner_available=True, health={}, bootout_seen=True,
                     lock_owner_pid=0, lock_owner_pids=[], arguments=[],
                     process_cwd="", label_cwd="")
        if grace_polls > 0:
            state.update(
                label_absence_grace_polls=grace_polls,
                label_absence_grace_pid=old_pid,
            )
        if case in {"pid_still_present", "keepalive_restart_survivor"}:
            state["pid"] = old_pid
        if case == "listener_still_present":
            state.update(pid=old_pid, listener=True)
        if case == "owner_still_held":
            state["owner_available"] = False
        save()
        raise SystemExit(0)
    if action == "bootstrap":
        if state["case"] == "keepalive_exited":
            state.update(loaded=True, pid=0,
                         cwd=os.environ["FAKE_CANDIDATE_CWD"],
                         label_cwd=os.environ["FAKE_CANDIDATE_CWD"],
                         listener=False, owner_available=True, health={},
                         plist=sys.argv[-1],
                         stdout_log=state["stdout_log"] or os.environ["FAKE_STDOUT_LOG"],
                         stderr_log=state["stderr_log"] or os.environ["FAKE_STDERR_LOG"])
        elif state["case"] in {"bind_failure", "reconcile_failure", "incompatible_reader"}:
            state.update(loaded=True, pid=0,
                         cwd=os.environ["FAKE_CANDIDATE_CWD"],
                         label_cwd=os.environ["FAKE_CANDIDATE_CWD"],
                         listener=False, owner_available=True, health={},
                         plist=sys.argv[-1],
                         stdout_log=state["stdout_log"] or os.environ["FAKE_STDOUT_LOG"],
                         stderr_log=state["stderr_log"] or os.environ["FAKE_STDERR_LOG"])
            save()
            raise SystemExit(1)
        elif state["case"] == "bootstrap_absent":
            state.update(loaded=False, pid=0, listener=False, owner_available=True)
        else:
            pid = 4242
            health = {
                "schema_version": "open_trader.prediction_service.health.v1",
                "module": "prediction_service", "status": "running",
                "mode": "production", "production_owner": True,
                "mutations": "enabled", "pid": pid,
                "cwd": os.environ["FAKE_CANDIDATE_CWD"],
                "git_sha": os.environ["FAKE_CANDIDATE_SHA"],
                "started_at": "2026-08-11T10:00:00+08:00",
                "release_schema_version": "open_trader.prediction_service.release.v1",
                "reader_generation": int(os.environ["FAKE_READER_GENERATION"]),
                "contract_generation": int(os.environ["FAKE_CONTRACT_GENERATION"]),
            }
            if state["case"] in {
                "wrong_health_sha",
                "keepalive_restart",
                "keepalive_restart_survivor",
            }:
                health["git_sha"] = "wrong"
            if state["case"] == "wrong_health_generation":
                health["reader_generation"] += 1
            listener = state["case"] != "listener_after_health"
            state.update(loaded=True, pid=pid, cwd=health["cwd"], listener=listener,
                         owner_available=False, health=health,
                         plist=sys.argv[-1],
                         process_cwd=health["cwd"], label_cwd=health["cwd"],
                         lock_owner_pid=pid, lock_owner_pids=[pid],
                         arguments=[
                             os.environ["FAKE_PYTHON"], "-m", "open_trader",
                             "prediction-service", "--mode", "production",
                             "--data-dir", os.environ["FAKE_DATA_DIR"], "--config",
                             os.environ["FAKE_CONFIG"], "--host", "127.0.0.1",
                             "--port", "8769", "--notifier-config",
                             os.environ["FAKE_NOTIFIER_CONFIG"], "--release-manifest",
                             os.environ["FAKE_RELEASE_MANIFEST"],
                         ],
                         stdout_log=state["stdout_log"] or os.environ["FAKE_STDOUT_LOG"],
                         stderr_log=state["stderr_log"] or os.environ["FAKE_STDERR_LOG"],
                         max_pids=max(int(state["max_pids"]), 1))
            if state["case"] == "listener_after_health":
                state["listener_after_health"] = True
        save()
        if state["case"] == "record_write_failure":
            record_path = Path(os.environ["FAKE_RECORD_PATH"])
            if record_path.exists():
                record_path.rename(record_path.with_name(record_path.name + ".prior"))
            record_path.mkdir(parents=True)
        raise SystemExit(0)

if command == "lsof":
    if any("runtime.lock" in item for item in sys.argv):
        barrier_count = state.get("lock_barrier_count")
        if barrier_count is not None:
            probe_count = int(state.get("lock_probe_count", 0)) + 1
            state["lock_probe_count"] = probe_count
            if probe_count == int(barrier_count):
                state["lock_barrier_snapshot"] = {
                    key: state.get(key) for key in (
                        "pid", "health", "lock_owner_pid", "lock_owner_pids",
                        "loaded", "cwd", "process_cwd", "label_cwd", "listener",
                    )
                }
                state["lock_barrier_phase"] = "waiting"
                save()
                marker = Path(state["lock_barrier_marker"])
                release = Path(state["lock_barrier_release"])
                marker.write_text("ready", encoding="utf-8")
                while not release.exists():
                    time.sleep(0.01)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["lock_barrier_phase"] = "frozen"
                save()
            elif state.get("lock_barrier_phase") == "frozen":
                state["lock_barrier_phase"] = "done"
                save()
            if state.get("lock_barrier_phase") == "frozen":
                state = {**state, **state["lock_barrier_snapshot"]}
        lock_owner_pids = state.get("lock_owner_pids")
        if lock_owner_pids is None:
            lock_owner_pids = [state.get("lock_owner_pid", 0)]
        lock_owner_pids = [pid for pid in lock_owner_pids if pid]
        if lock_owner_pids:
            print("\n".join(
                f"p{pid}\nf3\nk1\n"
                f"n{os.environ['FAKE_DATA_DIR']}/prediction_arbitrage/runtime.lock"
                for pid in lock_owner_pids
            ))
            raise SystemExit(0)
        raise SystemExit(1)
    view = observation_state()
    if "-d" in sys.argv and "cwd" in sys.argv:
        if view["pid"]:
            print(f"p{view['pid']}\nfcwd\nn{view.get('process_cwd', view['cwd'])}")
            raise SystemExit(0)
        raise SystemExit(1)
    if (view["case"] == "listener_inspection_error"
            or (view.get("listener_inspection_error_after_bootout")
                and view.get("bootout_seen"))) and not view["loaded"]:
        print("lsof: inspection failed", file=sys.stderr)
        raise SystemExit(1)
    if view["listener"]:
        print(
            f"p{view.get('listener_pid_override', view['pid'])}\n"
            f"n{view.get('listener_addr_override', '127.0.0.1:8769')}"
        )
        raise SystemExit(0)
    raise SystemExit(1)

if command == "curl":
    view = observation_state()
    if view["health"]:
        health = view["health"]
        if state.get("listener_after_health"):
            state.update(
                listener=True,
                listener_after_health=False,
                lock_owner_pid=health["pid"],
                lock_owner_pids=[health["pid"]],
            )
            save()
        if view["case"] in {"keepalive_restart", "keepalive_restart_survivor"}:
            restarted_pid = 4243
            state.update(
                pid=restarted_pid,
                health={**health, "pid": restarted_pid},
                restart_pids=[health["pid"], restarted_pid],
            )
            save()
        print(json.dumps(health))
        raise SystemExit(0)
    raise SystemExit(22)

if command == "ps":
    view = observation_state()
    if view["case"] == "ps_inspection_error" or view.get("ps_inspection_error"):
        print("ps: inspection failed", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if view["pid"] and str(view["pid"]) in sys.argv else 1)

if command == "owner-probe":
    view = observation_state()
    raise SystemExit(0 if view["owner_available"] else 1)
'''


MANAGER_EXTERNAL_BOUNDARIES = r'''"""Only the fixture OS/GitHub boundary is modeled; guard and verifier stay real."""
import errno, json, os, signal, socket, subprocess
from pathlib import Path
from open_trader import prediction_cloud
prediction_cloud.UNIT_PATH = Path(__file__).parent / "fixture-absent-systemd.service"
state_file = os.environ.get("FAKE_STATE")
if state_file:
    def state(): return json.loads(Path(state_file).read_text())
    native_connect = socket.socket.connect_ex
    def connect(sock, address):
        if address == ("127.0.0.1", 8769):
            observed = state()
            if observed.get("listener_inspection_error_after_bootout") and observed.get("bootout_seen"):
                return errno.EACCES
            return 0 if observed["listener"] else errno.ECONNREFUSED
        raise AssertionError("uncontrolled native socket probe: " + repr(address))
    socket.socket.connect_ex = connect
    native_kill = os.kill
    def kill(pid, signum):
        if signum != 0:
            raise AssertionError("fixture manager cannot signal any actual process")
        if state()["pid"] == pid: return None
        raise ProcessLookupError(errno.ESRCH, "modeled fixture PID is absent")
    os.kill = kill
    native_run = subprocess.run
    def run(command, *args, **kwargs):
        if isinstance(command, list) and command and command[0] in ("/usr/sbin/lsof", "/bin/ps", "/bin/launchctl"):
            command = [str(Path(os.environ["LAUNCHCTL_BIN"]).parent / Path(command[0]).name), *command[1:]]
        return native_run(command, *args, **kwargs)
    subprocess.run = run
'''


class ReleaseHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.runtime_root = tmp_path / "runtime"
        self.agents = tmp_path / "LaunchAgents"
        self.agents.mkdir()
        self.state_path = tmp_path / "fake-state.json"
        self.calls = CommandCalls(tmp_path / "fake-calls.log")
        self.fake_bin = tmp_path / "fake-bin"
        self.fake_bin.mkdir()
        self._write_dispatcher()
        self.config = self.root / "prediction.json"
        self.config.write_text(json.dumps(dict(signer_address="0x" + "a"*40, wallet_address="0x" + "a"*40)))
        self.config.chmod(0o600)
        self.candidate = self.make_checkout("candidate")
        # A separate unchanged manager checkout preserves the poisoned candidate
        # PYTHONPATH oracle and isolates every newly native OS boundary too.
        self.manager = self.make_checkout("manager")
        self.configure("absent")

    def make_checkout(self, name: str) -> ReleaseCheckout:
        path = self.root / name
        for directory in ("src", "ops", "scripts"):
            shutil.copytree(ROOT / directory, path / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        for name in ("pyproject.toml", "uv.lock", "Dockerfile.dev"):
            shutil.copyfile(ROOT / name, path / name)
        tests = path / "tests"; tests.mkdir()
        (tests / "__init__.py").write_text("")
        for name in ("lp_d2_service_harness.py", "test_lp_order_registration_contract.py",
                     "test_deployment_preflight.py", "test_lp_d2_service_lifecycle.py",
                     "test_lp_d2_startup_integration.py"):
            shutil.copyfile(ROOT / "tests" / name, tests / name)
        from tests.test_deployment_preflight import evidence
        # Use the existing external artifact fixture's minimal source partition.
        scratch = self.root / (path.name + "-partition")
        scratch.mkdir()
        evidence.__wrapped__(scratch)
        for extra in (scratch / "tests").glob("*.py"):
            if not (tests / extra.name).exists(): shutil.copyfile(extra, tests / extra.name)
        (path / "src" / "sitecustomize.py").write_text(MANAGER_EXTERNAL_BOUNDARIES)
        release = load_prediction_release_manifest(
            path / "ops" / "prediction-service-release.json"
        )
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=test", "-c",
             "user.email=test@example.com", "commit", "-qm", path.name],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(path), "checkout", "--detach", "-q"], check=True)
        return ReleaseCheckout(
            path=path,
            sha=sha,
            reader_generation=release.reader_generation,
            contract_generation=release.contract_generation,
        )

    def _write_dispatcher(self) -> None:
        dispatcher = self.fake_bin / "fake-command"
        dispatcher.write_text(FAKE_COMMAND_SOURCE, encoding="utf-8")
        dispatcher.chmod(0o755)
        for name in ("launchctl", "lsof", "curl", "ps", "owner-probe", "sleep", "gh"):
            (self.fake_bin / name).symlink_to(dispatcher)

    def configure(self, case: str) -> None:
        state = {
            "case": case,
            "loaded": False,
            "pid": 0,
            "cwd": "",
            "listener": False,
            "owner_available": True,
            "health": {},
            "max_pids": 0,
            "plist": "",
            "stdout_log": "",
            "stderr_log": "",
            "restart_pids": [],
            "bootout_pids": [],
            "lock_owner_pid": 0,
            "lock_owner_pids": [],
            "arguments": [],
            "process_cwd": "",
            "label_cwd": "",
            "print_count": 0,
        }
        if case == "unknown_listener":
            state.update(pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_label_identity":
            state.update(loaded=True, pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_owner":
            state["owner_available"] = False
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def _env(
        self, checkout: ReleaseCheckout, *, release_manifest: Path | None = None,
    ) -> dict[str, str]:
        from tests.lp_d2_service_harness import offline_github_fixture
        actual_sha = subprocess.check_output(["git", "-C", str(checkout.path), "rev-parse", "HEAD"], text=True).strip()
        fixture = self.root / ("github-" + actual_sha)
        if not fixture.exists():
            api = offline_github_fixture(checkout.path, fixture)
            payload = dict(tip=api.tip, run=api.run, workflow_runs=api.runs,
                jobs=api.jobs, check_runs=api.checks, artifacts=api.artifacts)
            (fixture / "api.json").write_text(json.dumps(payload))
            for artifact_id, data in api.downloads.items():
                (fixture / (str(artifact_id) + ".zip")).write_bytes(data)
        return {
            **os.environ,
            "FAKE_GITHUB_DIR": str(fixture),
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(checkout.path / "src"),
            "LAUNCHCTL_BIN": str(self.fake_bin / "launchctl"),
            "LSOF_BIN": str(self.fake_bin / "lsof"),
            "CURL_BIN": str(self.fake_bin / "curl"),
            "PS_BIN": str(self.fake_bin / "ps"),
            "OWNER_PROBE_BIN": str(self.fake_bin / "owner-probe"),
            "FAKE_STATE": str(self.state_path),
            "FAKE_CALLS": str(self.calls.path),
            "FAKE_CANDIDATE_CWD": str(checkout.path),
            "FAKE_CANDIDATE_SHA": checkout.sha,
            "FAKE_READER_GENERATION": str(checkout.reader_generation),
            "FAKE_CONTRACT_GENERATION": str(checkout.contract_generation),
            "FAKE_STDOUT_LOG": str(self.stdout_log),
            "FAKE_STDERR_LOG": str(self.stderr_log),
            "FAKE_PYTHON": sys.executable,
            "FAKE_DATA_DIR": str(self.runtime_root / "data"),
            "FAKE_CONFIG": str(self.root / "prediction.json"),
            "FAKE_NOTIFIER_CONFIG": str(self.runtime_root / "config" / "daily_premarket.env"),
            "FAKE_RELEASE_MANIFEST": str(
                checkout.path / "ops" / "prediction-service-release.json"
                if release_manifest is None else release_manifest
            ),
            "FAKE_RECORD_PATH": str(self.runtime_root / "prediction-service-runtime.json"),
        }

    def install(
        self, checkout: ReleaseCheckout | None = None, *, mode: str = "production",
        dry_run: bool = False, expected_sha: str | None = None,
        release_manifest: Path | None = None, check: bool = False,
        preflight: bool = False, owner_probe: bool = True,
        runtime_root: Path | None = None, wait_seconds: int = 1,
    ) -> subprocess.CompletedProcess[str]:
        checkout = self.candidate if checkout is None else checkout
        runtime_root = self.runtime_root if runtime_root is None else runtime_root
        release_manifest = (
            checkout.path / "ops" / "prediction-service-release.json"
            if release_manifest is None else release_manifest
        )
        command = [
            str(checkout.path / "scripts" / "install_prediction_service_launchd.sh"),
            "--mode", mode, "--repo-root", str(checkout.path),
            "--runtime-root", str(runtime_root), "--python", sys.executable,
            "--config", str(self.root / "prediction.json"),
            "--launch-agents-dir", str(self.agents), "--wait-seconds", str(wait_seconds),
            "--release-manifest", str(release_manifest),
        ]
        if dry_run:
            command.append("--dry-run")
        if preflight:
            command.append("--preflight")
        if expected_sha is not None:
            command.extend(("--expected-sha", expected_sha))
        environment = self._env(checkout, release_manifest=release_manifest)
        if not owner_probe:
            environment.pop("OWNER_PROBE_BIN", None)
        return supervised_process(
            command, check=check, timeout=30, combine_stderr=False,
            env=environment,
        )

    def install_process(
        self, checkout: ReleaseCheckout,
    ) -> subprocess.Popen[str]:
        command = [
            str(checkout.path / "scripts" / "install_prediction_service_launchd.sh"),
            "--mode", "production", "--repo-root", str(checkout.path),
            "--runtime-root", str(self.runtime_root), "--python", sys.executable,
            "--config", str(self.root / "prediction.json"),
            "--launch-agents-dir", str(self.agents), "--wait-seconds", "1",
            "--release-manifest", str(checkout.path / "ops" / "prediction-service-release.json"),
        ]
        return subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self._env(checkout), start_new_session=True,
        )

    def uninstall(self, *, mode: str = "production") -> subprocess.CompletedProcess[str]:
        return supervised_process(
            [
                str(self.candidate.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", mode, "--runtime-root", str(self.runtime_root),
                "--launch-agents-dir", str(self.agents), "--python", sys.executable,
            ],
            timeout=30, combine_stderr=False, env=self._env(self.candidate),
        )

    def manager_install(
        self, checkout: ReleaseCheckout, *, inherited_pythonpath: Path,
    ) -> subprocess.CompletedProcess[str]:
        return supervised_process(
            [
                str(self.manager.path / "scripts" / "install_prediction_service_launchd.sh"),
                "--mode", "production", "--repo-root", str(checkout.path),
                "--runtime-root", str(self.runtime_root), "--python", sys.executable,
                "--config", str(self.root / "prediction.json"),
                "--launch-agents-dir", str(self.agents), "--wait-seconds", "1",
                "--release-manifest", str(checkout.path / "ops" / "prediction-service-release.json"),
            ],
            timeout=30, combine_stderr=False,
            env={**self._env(checkout), "PYTHONPATH": str(inherited_pythonpath)},
        )

    def manager_uninstall(
        self, *, inherited_pythonpath: Path,
    ) -> subprocess.CompletedProcess[str]:
        return supervised_process(
            [
                str(self.manager.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", "production", "--runtime-root", str(self.runtime_root),
                "--launch-agents-dir", str(self.agents), "--python", sys.executable,
            ],
            timeout=30, combine_stderr=False,
            env={**self._env(self.candidate), "PYTHONPATH": str(inherited_pythonpath)},
        )

    def mutate_live_restart(
        self, *, pid: int = 5252,
        started_at: str = "2026-09-09T11:00:00+08:00",
    ) -> None:
        state = self.state
        health = dict(state["health"])
        health.update(pid=pid, started_at=started_at)
        state.update(pid=pid, health=health, lock_owner_pid=pid, lock_owner_pids=[pid])
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    @property
    def runtime_record(self) -> dict[str, object] | None:
        return load_prediction_runtime_record(
            self.runtime_root / "prediction-service-runtime.json"
        )

    @property
    def plist(self) -> Path:
        return self.agents / f"{LABEL}.plist"

    @property
    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @property
    def listener_pids(self) -> list[int]:
        state = self.state
        return [int(state["pid"])] if state["listener"] else []

    def owner_is_available(self) -> bool:
        return self.state["owner_available"] is True

    @property
    def manifest(self) -> Path:
        return self.candidate.path / "ops" / "prediction-service-release.json"

    @property
    def sha(self) -> str:
        return self.candidate.sha

    @property
    def pid(self) -> int:
        return int(self.state["pid"])

    @property
    def started_at(self) -> str:
        return str(self.state["health"]["started_at"])

    @property
    def database(self) -> Path:
        return self.runtime_root / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"

    @property
    def stdout_log(self) -> Path:
        return self.runtime_root / "logs" / "prediction_service" / "launchd.out.log"

    @property
    def stderr_log(self) -> Path:
        return self.runtime_root / "logs" / "prediction_service" / "launchd.err.log"


@pytest.fixture
def release_harness(tmp_path: Path) -> ReleaseHarness:
    return ReleaseHarness(tmp_path)


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("dirty_checkout", "release root is dirty"),
        ("wrong_sha", "requested SHA does not match checkout"),
        ("invalid_manifest", "prediction release manifest"),
        ("unknown_listener", "unknown listener on 8769"),
        ("listener_inspection_error", "failed to inspect listener on 8769"),
        ("unknown_label_identity", "managed launchd identity is not verified"),
        ("unknown_owner", "prediction runtime owner is held by an unknown process"),
    ],
)
def test_production_preflight_refuses_before_shutdown(
    release_harness: ReleaseHarness, case: str, expected_error: str
) -> None:
    checkout = release_harness.candidate
    install_kwargs: dict[str, object] = {"mode": "production"}
    if case == "dirty_checkout":
        (checkout.path / "dirty.txt").write_text("dirty", encoding="utf-8")
    elif case == "wrong_sha":
        install_kwargs["expected_sha"] = "0" * 40
    elif case == "invalid_manifest":
        manifest = checkout.path / "ops" / "prediction-service-release.json"
        manifest.write_text('{"reader_generation":0}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout.path), "add", str(manifest)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout.path), "-c", "user.name=test", "-c",
             "user.email=test@example.com", "commit", "-qm", "invalid manifest"],
            check=True,
        )
        checkout = ReleaseCheckout(
            path=checkout.path,
            sha=subprocess.run(
                ["git", "-C", str(checkout.path), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
        )
    else:
        release_harness.configure(case)
    result = release_harness.install(checkout, **install_kwargs)
    assert result.returncode == 1
    assert expected_error in result.stderr
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.runtime_record is None


@pytest.mark.parametrize("location", ["external", "ignored"])
def test_production_requires_manifest_tracked_by_selected_checkout(
    release_harness: ReleaseHarness, location: str
) -> None:
    checkout = release_harness.candidate
    if location == "external":
        manifest = release_harness.root / "external-release.json"
    else:
        manifest = checkout.path / "ignored-release.json"
        (checkout.path / ".git" / "info" / "exclude").write_text(
            "ignored-release.json\n", encoding="utf-8"
        )
    shutil.copy2(release_harness.manifest, manifest)

    result = release_harness.install(release_manifest=manifest)

    assert result.returncode == 1
    assert "release manifest must be tracked by checkout" in result.stderr
    assert release_harness.calls.all() == []
    assert release_harness.runtime_record is None


def test_production_dry_run_is_side_effect_free(release_harness: ReleaseHarness) -> None:
    result = release_harness.install(mode="production", dry_run=True)
    assert result.returncode == 0
    payload = plistlib.loads(result.stdout.encode())
    assert payload["WorkingDirectory"] == str(release_harness.candidate.path)
    assert "--mode" in payload["ProgramArguments"]
    assert "production" in payload["ProgramArguments"]
    assert str(release_harness.manifest) in payload["ProgramArguments"]
    assert not release_harness.runtime_root.exists()
    assert release_harness.calls.all() == []


def test_production_first_install_records_only_observed_ready_evidence(
    release_harness: ReleaseHarness,
) -> None:
    assert not release_harness.runtime_root.exists()
    result = release_harness.install(mode="production")
    assert result.returncode == 0, result.stdout + result.stderr
    info = release_harness.runtime_root.lstat()
    assert info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"
    binding = record["candidate"]["lp_validation"]
    assert record["candidate"] == {
        "lp_validation": binding,
        "manifest": str(release_harness.manifest.resolve()),
        "checkout": str(release_harness.candidate.path),
        "git_sha": release_harness.sha,
        "source_state": "clean",
        "reader_generation": release_harness.candidate.reader_generation,
        "contract_generation": release_harness.candidate.contract_generation,
    }
    import hashlib
    assert set(binding) == {"schema_version", "source_sha", "release_root", "runtime_root",
        "python", "capability_digest", "config_selection", "runtime", "ci", "operation_nonce"}
    assert binding["schema_version"] == "open_trader.lp.validation.v1"
    assert binding["source_sha"] == release_harness.sha
    assert binding["release_root"] == str(release_harness.candidate.path)
    assert binding["runtime_root"] == str(release_harness.runtime_root)
    assert binding["python"] == sys.executable
    assert binding["capability_digest"] == hashlib.sha256((release_harness.candidate.path / "ops/lp-runtime-contract.json").read_bytes()).hexdigest()
    assert binding["config_selection"] == dict(path=str(release_harness.config),
        sha256=hashlib.sha256(release_harness.config.read_bytes()).hexdigest())
    runtime = binding["runtime"]
    assert runtime["source_root"] == str(release_harness.candidate.path)
    assert runtime["python"] == sys.executable and runtime["dependencies"]
    assert runtime["lock_sha256"] == hashlib.sha256((release_harness.candidate.path / "uv.lock").read_bytes()).hexdigest()
    fixture = release_harness.root / ("github-" + release_harness.sha)
    expected_artifacts = [dict(scope=scope, id=index,
        digest="sha256:" + hashlib.sha256((fixture / (str(index) + ".zip")).read_bytes()).hexdigest())
        for index, scope in enumerate(("gateway", "legacy", "account", "prediction", "portable"), 1)]
    assert binding["ci"] == dict(run_id=22, run_attempt=2, artifact_role="test-only",
        run_url="https://github.com/raymizzou/open_trader/actions/runs/22", artifacts=expected_artifacts)
    permit = json.loads((release_harness.runtime_root / "lp-start-guard/permit.json").read_text())
    assert permit["state"] == "ARMED"
    assert binding["operation_nonce"] == permit["operation_nonce"]
    assert record["previous_release"] is None
    assert record["ready"]["pid"] == release_harness.pid
    assert record["ready"]["cwd"] == str(release_harness.candidate.path)
    assert record["ready"]["listener"] == "127.0.0.1:8769"
    assert record["ready"]["health_schema"] == "open_trader.prediction_service.health.v1"
    assert record["ready"]["health_module"] == "prediction_service"
    assert record["ready"]["process_started_at"] == release_harness.started_at
    assert record["ready"]["logs"]["stdout"].endswith("launchd.out.log")


def test_ready_logs_come_from_observed_launchctl_state(
    release_harness: ReleaseHarness,
) -> None:
    observed_stdout = release_harness.root / "observed" / "manager.out.log"
    observed_stderr = release_harness.root / "observed" / "manager.err.log"
    state = release_harness.state
    state["stdout_log"] = str(observed_stdout)
    state["stderr_log"] = str(observed_stderr)
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")

    result = release_harness.install(mode="production")

    assert result.returncode == 0
    record = release_harness.runtime_record
    assert record is not None
    assert record["ready"]["logs"] == {
        "stdout": str(observed_stdout),
        "stderr": str(observed_stderr),
    }


def test_production_refuses_when_post_start_source_cannot_be_rechecked(
    release_harness: ReleaseHarness,
) -> None:
    curl = release_harness.fake_bin / "curl"
    curl.unlink()
    curl.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

calls_path = Path(os.environ["FAKE_CALLS"])
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write("curl " + " ".join(sys.argv[1:]) + "\\n")
git_dir = Path(os.environ["FAKE_CANDIDATE_CWD"]) / ".git"
if git_dir.exists():
    git_dir.rename(git_dir.with_name(".git-unavailable"))
state = json.loads(Path(os.environ["FAKE_STATE"]).read_text(encoding="utf-8"))
print(json.dumps(state["health"]))
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "candidate_source_became_dirty" in result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert not release_harness.plist.exists()
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()
    assert sum(
        " bootout " in f" {call} " for call in release_harness.calls.all()
    ) == 1


def test_keepalive_loaded_exited_candidate_is_booted_out(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("keepalive_exited")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "candidate_timeout" in result.stderr
    assert release_harness.state["loaded"] is False
    assert not release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_timeout"
    assert sum(
        " bootstrap " in f" {call} " for call in release_harness.calls.all()
    ) == 1
    assert sum(
        " bootout " in f" {call} " for call in release_harness.calls.all()
    ) == 1


def test_keepalive_restarted_candidate_is_booted_out(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("keepalive_restart")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "wrong_health_identity" in result.stderr
    assert release_harness.state["restart_pids"] == [4242, 4243]
    assert release_harness.state["loaded"] is False
    assert not release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "wrong_health_identity"
    assert sum(
        " bootout " in f" {call} " for call in release_harness.calls.all()
    ) == 1


def test_keepalive_restarted_survivor_fails_pid_absence_proof(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("keepalive_restart_survivor")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "candidate_cleanup_not_proven" in result.stderr
    assert release_harness.state["restart_pids"] == [4242, 4243]
    assert release_harness.state["loaded"] is False
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()
    assert "ps -p 4243" in release_harness.calls.all()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_cleanup_not_proven"


@pytest.mark.parametrize("operation", ["upgrade", "uninstall"])
def test_installer_waits_past_launchd_exit_grace(
    release_harness: ReleaseHarness, operation: str,
) -> None:
    old = release_harness.candidate
    release_harness.install(old, check=True)
    state = release_harness.state
    state["bootout_grace_polls"] = 5
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")

    if operation == "upgrade":
        target = release_harness.make_checkout("grace-upgrade")
        result = release_harness.install(target, wait_seconds=7)
        assert result.returncode == 0, result.stderr
        record = release_harness.runtime_record
        assert record is not None
        assert record["state"] == "ready"
        assert record["candidate"]["git_sha"] == target.sha
    else:
        result = release_harness.uninstall()
        assert result.returncode == 0, result.stderr
        record = release_harness.runtime_record
        assert record is not None
        assert record["state"] == "stopped"

    assert release_harness.state["label_absence_grace_polls"] == 0


def test_candidate_ready_reobserves_listener_after_health(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("listener_after_health")

    result = release_harness.install(mode="production")

    assert result.returncode == 0, result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"
    assert record["ready"]["pid"] == release_harness.pid
    assert record["ready"]["listener"] == "127.0.0.1:8769"


def test_candidate_primary_failure_survives_cleanup_failure(
    release_harness: ReleaseHarness,
) -> None:
    state = release_harness.state
    state.update(case="wrong_health_sha", ps_inspection_error=True)
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "candidate primary failure: wrong_health_identity" in result.stderr
    assert "candidate readiness diagnostic:" in result.stderr
    assert '"health_pid":4242' in result.stderr
    assert '"health_cwd":' in result.stderr
    assert "candidate cleanup failure: candidate_cleanup_not_proven" in result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_cleanup_not_proven"
    assert "ready" not in record


def test_upgrade_refuses_old_pid_inspection_error_before_bootstrap(
    release_harness: ReleaseHarness,
) -> None:
    old = release_harness.candidate
    new = release_harness.make_checkout("new-candidate")
    release_harness.install(old, check=True)
    state = release_harness.state
    state["ps_inspection_error"] = True
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    release_harness.calls.clear()

    result = release_harness.install(new)

    assert result.returncode == 1
    assert "candidate_cleanup_not_proven" in result.stderr
    assert release_harness.calls.named("launchctl") == [
        f"launchctl print gui/{os.getuid()}/{LABEL}",
        f"launchctl print gui/{os.getuid()}/{LABEL}",
        f"launchctl bootout gui/{os.getuid()}/{LABEL}",
        f"launchctl print gui/{os.getuid()}/{LABEL}",
    ]


@pytest.mark.parametrize("inspection", ["ps", "listener"])
def test_failed_candidate_refuses_cleanup_inspection_error(
    release_harness: ReleaseHarness, inspection: str
) -> None:
    state = release_harness.state
    state["case"] = "wrong_health_sha"
    state[
        "ps_inspection_error"
        if inspection == "ps"
        else "listener_inspection_error_after_bootout"
    ] = True
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "candidate_cleanup_not_proven" in result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_cleanup_not_proven"


def test_post_maintenance_log_setup_failure_removes_autoload_plist(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.stdout_log.mkdir(parents=True)

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert not release_harness.plist.exists()
    assert release_harness.state["loaded"] is False
    assert release_harness.calls.named("launchctl") == [
        f"launchctl print gui/{os.getuid()}/{LABEL}",
        f"launchctl print gui/{os.getuid()}/{LABEL}",
    ]
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_exited"


def test_absent_candidate_cleanup_never_boots_out_unobserved_label(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("bootstrap_absent")

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert all(
        " bootout " not in f" {call} " for call in release_harness.calls.all()
    )
    assert release_harness.state["loaded"] is False
    assert not release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_exited"


def test_same_sha_ready_install_is_a_noop(release_harness: ReleaseHarness) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.calls.clear()
    result = release_harness.install(mode="production")
    assert result.returncode == 0
    assert "already ready" in result.stdout
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())


def test_managed_owner_lock_contention_is_a_quiet_probe(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    lock_path = release_harness.runtime_root / "data/prediction_arbitrage/runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = release_harness.install(mode="production", owner_probe=False)

    assert result.returncode == 0
    assert "already ready" in result.stdout
    assert result.stderr == ""


def test_unmanaged_owner_lock_contention_blocks_install(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.configure("unknown_owner")
    lock_path = release_harness.runtime_root / "data/prediction_arbitrage/runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = release_harness.install(mode="production", owner_probe=False)

    assert result.returncode == 1
    assert "prediction runtime owner is held by an unknown process" in result.stderr
    assert "Traceback" not in result.stderr
    assert all(
        "bootout" not in call and "bootstrap" not in call
        for call in release_harness.calls.named("launchctl")
    )


def test_owner_lock_probe_error_blocks_install(
    release_harness: ReleaseHarness,
) -> None:
    lock_path = release_harness.runtime_root / "data/prediction_arbitrage/runtime.lock"
    lock_path.mkdir(parents=True)

    result = release_harness.install(mode="production", owner_probe=False)

    assert result.returncode == 1
    assert "prediction runtime ownership probe failed: IsADirectoryError" in result.stderr
    assert all(
        "bootout" not in call and "bootstrap" not in call
        for call in release_harness.calls.named("launchctl")
    )


def test_owner_lock_probe_io_cause_is_reported(
    release_harness: ReleaseHarness,
) -> None:
    checkout = release_harness.candidate
    runtime_source = checkout.path / "src/open_trader/prediction_runtime.py"
    source = runtime_source.read_text(encoding="utf-8")
    source = source.replace(
        "fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
        "raise OSError(5, 'synthetic flock I/O failure')", 1,
    )
    runtime_source.write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout.path), "add", str(runtime_source)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout.path), "-c", "user.name=test", "-c",
         "user.email=test@example.com", "commit", "-qm", "synthetic lock I/O error"],
        check=True,
    )

    result = release_harness.install(owner_probe=False)

    assert result.returncode == 1
    assert "prediction runtime ownership probe failed: OSError: [Errno 5] synthetic flock I/O failure" in result.stderr
    assert all("bootstrap" not in call for call in release_harness.calls.named("launchctl"))


def test_owner_lock_probe_error_after_bootout_records_failure(
    release_harness: ReleaseHarness,
) -> None:
    old = release_harness.candidate
    new = release_harness.make_checkout("new-candidate")
    release_harness.install(old, check=True)
    release_harness.calls.clear()
    state = release_harness.state
    state["runtime_lock_directory_after_bootout"] = True
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    lock_path = release_harness.runtime_root / "data/prediction_arbitrage/runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = release_harness.install(new, owner_probe=False)

    assert result.returncode == 1
    assert "prediction runtime ownership probe failed: IsADirectoryError" in result.stderr
    assert "candidate_cleanup_not_proven" in result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["failure_reason"] == "candidate_cleanup_not_proven"
    assert all("bootstrap" not in call for call in release_harness.calls.named("launchctl"))


@pytest.mark.parametrize("field", ["reader_generation", "contract_generation"])
def test_install_rejects_boolean_ready_generation_without_bootout(
    release_harness: ReleaseHarness, field: str
) -> None:
    release_harness.install(mode="production", check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    record = release_harness.runtime_record
    assert record is not None
    record["ready"][field] = True
    record_path.write_text(json.dumps(record), encoding="utf-8")
    release_harness.calls.clear()

    result = release_harness.install(mode="production")

    assert result.returncode == 1
    assert "managed launchd identity is not verified" in result.stderr
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())


@pytest.mark.parametrize("direction", ["upgrade", "rollback"])
def test_compatible_transition_uses_one_downtime_handoff(
    release_harness: ReleaseHarness, direction: str
) -> None:
    old = release_harness.candidate
    new = release_harness.make_checkout("new-candidate")
    first, candidate = (old, new) if direction == "upgrade" else (new, old)
    release_harness.install(first, check=True)
    release_harness.calls.clear()
    result = release_harness.install(candidate)
    assert result.returncode == 0
    calls = release_harness.calls.all()
    assert next(i for i, call in enumerate(calls) if call.startswith("launchctl bootout")) \
        < next(i for i, call in enumerate(calls) if call.startswith("launchctl bootstrap"))
    assert release_harness.state["max_pids"] == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["candidate"]["git_sha"] == candidate.sha
    assert record["previous_release"]["git_sha"] == first.sha


def test_incompatible_rollback_stays_single_owner_and_non_ready(
    release_harness: ReleaseHarness,
) -> None:
    older = release_harness.candidate
    newer = release_harness.make_checkout("newer-candidate")
    release_harness.install(newer, check=True)
    state = release_harness.state
    state["case"] = "incompatible_reader"
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.install(older)
    assert result.returncode == 1
    assert "candidate_exited" in result.stderr
    assert release_harness.state["max_pids"] == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert record["previous_release"]["git_sha"] == newer.sha
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()


@pytest.mark.parametrize(
    "failure",
    ["reconcile_failure", "wrong_health_sha", "wrong_health_generation", "bind_failure"],
)
def test_failed_candidate_is_removed_and_previous_is_not_auto_restarted(
    release_harness: ReleaseHarness, failure: str
) -> None:
    state = release_harness.state
    state["case"] = failure
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.install(mode="production")
    assert result.returncode == 1
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "failed"
    assert len(release_harness.calls.named("launchctl")) >= 2
    assert sum(" bootstrap " in f" {call} " for call in release_harness.calls.all()) == 1
    assert sum(" bootout " in f" {call} " for call in release_harness.calls.all()) == 1
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()


def test_production_uninstall_preserves_data_logs_and_marks_stopped(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.database.parent.mkdir(parents=True, exist_ok=True)
    release_harness.database.write_bytes(b"test-database")
    release_harness.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    release_harness.stdout_log.write_text("keep-log\n", encoding="utf-8")
    config = release_harness.root / "prediction.json"
    config.write_text('{"mode":"test"}\n', encoding="utf-8")
    evidence = release_harness.runtime_root / "data" / "operator-evidence.json"
    evidence.write_text('{"keep":true}\n', encoding="utf-8")
    database_before = release_harness.database.read_bytes()
    log_before = release_harness.stdout_log.read_text(encoding="utf-8")
    record_before = release_harness.runtime_record
    assert record_before is not None
    ready_before = record_before["ready"]
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 0
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "stopped"
    assert record["candidate"]["git_sha"] == release_harness.sha
    assert record["ready"] == ready_before
    assert release_harness.database.read_bytes() == database_before
    assert release_harness.stdout_log.read_text(encoding="utf-8") == log_before
    assert config.read_text(encoding="utf-8") == '{"mode":"test"}\n'
    assert evidence.read_text(encoding="utf-8") == '{"keep":true}\n'
    assert not release_harness.plist.exists()
    assert release_harness.listener_pids == []
    assert release_harness.owner_is_available()

    repeated = release_harness.uninstall(mode="production")
    assert repeated.returncode == 0
    assert sum(" bootout " in f" {call} " for call in release_harness.calls.all()) == 1


def test_uninstall_refuses_unknown_identity_without_bootout_or_plist_removal(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    release_harness.configure("unknown_label_identity")
    release_harness.calls.clear()
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 1
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.plist.exists()


@pytest.mark.parametrize("case", ["pid_still_present", "listener_still_present", "owner_still_held"])
def test_uninstall_requires_every_absence_proof(
    release_harness: ReleaseHarness, case: str
) -> None:
    release_harness.install(mode="production", check=True)
    state = release_harness.state
    state["case"] = case
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 1
    assert release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] != "stopped"


@pytest.mark.parametrize(
    "case", ["listener_inspection_error", "ps_inspection_error"]
)
def test_uninstall_refuses_absence_inspection_error(
    release_harness: ReleaseHarness, case: str
) -> None:
    release_harness.install(mode="production", check=True)
    state = release_harness.state
    state["case"] = case
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = release_harness.uninstall(mode="production")
    assert result.returncode == 1
    assert release_harness.plist.exists()
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"


def test_uninstall_rejects_maintenance_record_without_side_effects(
    release_harness: ReleaseHarness,
) -> None:
    release_harness.install(mode="production", check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    record = release_harness.runtime_record
    assert record is not None
    record["state"] = "maintenance"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    record_before = record_path.read_bytes()
    release_harness.configure("absent")
    release_harness.calls.clear()

    result = release_harness.uninstall(mode="production")

    assert result.returncode == 1
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.plist.exists()
    assert record_path.read_bytes() == record_before


@pytest.mark.parametrize("field", ["reader_generation", "contract_generation"])
def test_uninstall_rejects_boolean_ready_generation_without_bootout(
    release_harness: ReleaseHarness, field: str
) -> None:
    release_harness.install(mode="production", check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    record = release_harness.runtime_record
    assert record is not None
    record["ready"][field] = True
    record_path.write_text(json.dumps(record), encoding="utf-8")
    record_before = record_path.read_bytes()
    release_harness.calls.clear()

    result = release_harness.uninstall(mode="production")

    assert result.returncode == 1
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert release_harness.plist.exists()
    assert record_path.read_bytes() == record_before


def test_checkout_release_direct_workflow(release_harness: ReleaseHarness) -> None:
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    old = release_harness.candidate
    new = release_harness.make_checkout("direct-new")
    observed: list[dict[str, object]] = []
    PredictionArbitrageStore(release_harness.runtime_root / "data")

    for checkout in (old, new, old):
        result = release_harness.install(checkout)
        assert result.returncode == 0, result.stderr
        record = release_harness.runtime_record
        assert record is not None
        observed.append({
            "state": record["state"],
            "git_sha": record["candidate"]["git_sha"],
            "reader_generation": record["candidate"]["reader_generation"],
            "contract_generation": record["candidate"]["contract_generation"],
        })

    stopped = release_harness.uninstall()
    assert stopped.returncode == 0, stopped.stderr
    final_record = release_harness.runtime_record
    assert final_record is not None
    assert final_record["previous_release"]["git_sha"] == new.sha
    evidence = {
        "states": [item["state"] for item in observed] + [final_record["state"]],
        "candidate_shas": [item["git_sha"] for item in observed],
        "reader_generations": [item["reader_generation"] for item in observed],
        "contract_generations": [item["contract_generation"] for item in observed],
        "max_simultaneous_managed_pids": release_harness.state["max_pids"],
        "final_listener": release_harness.listener_pids or None,
        "final_owner_available": release_harness.owner_is_available(),
    }
    print(json.dumps(evidence, sort_keys=True))

    assert evidence == {
        "states": ["ready", "ready", "ready", "stopped"],
        "candidate_shas": [old.sha, new.sha, old.sha],
        "reader_generations": [old.reader_generation, new.reader_generation, old.reader_generation],
        "contract_generations": [old.contract_generation, new.contract_generation, old.contract_generation],
        "max_simultaneous_managed_pids": 1,
        "final_listener": None,
        "final_owner_available": True,
    }
    calls = release_harness.calls.all()
    assert all("/bin/launchctl" not in call for call in calls)
    assert all("/usr/sbin/lsof" not in call for call in calls)
    assert all("/Users/ray/projects/open_trader" not in call for call in calls)


# Approved TDD case A: the service manager PID/start observation may change
# while the verified release identity remains the same.
@pytest.mark.parametrize("operation", ["same_release", "upgrade", "uninstall"])
def test_managed_restart_supports_release_operations(
    release_harness: ReleaseHarness, operation: str
) -> None:
    old = release_harness.candidate
    release_harness.install(old, check=True)
    release_harness.mutate_live_restart()
    release_harness.calls.clear()

    if operation == "same_release":
        result = release_harness.install(old)
        assert result.returncode == 0, result.stderr
        assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
        assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
        record = release_harness.runtime_record
        assert record is not None
        assert record["ready"]["pid"] == 5252
        assert record["ready"]["process_started_at"] == "2026-09-09T11:00:00+08:00"
    elif operation == "upgrade":
        new = release_harness.make_checkout("restart-upgrade")
        result = release_harness.install(new)
        assert result.returncode == 0, result.stderr
        assert release_harness.state["bootout_pids"] == [5252]
        assert release_harness.state["max_pids"] == 1
        record = release_harness.runtime_record
        assert record is not None
        assert record["candidate"]["git_sha"] == new.sha
        assert record["previous_release"]["git_sha"] == old.sha
    else:
        result = release_harness.uninstall()
        assert result.returncode == 0, result.stderr
        assert release_harness.state["bootout_pids"] == [5252]
        record = release_harness.runtime_record
        assert record is not None
        assert record["state"] == "stopped"
        assert record["candidate"]["git_sha"] == old.sha
        assert record["ready"]["pid"] == 5252


def test_manager_helpers_do_not_depend_on_candidate_pythonpath(
    release_harness: ReleaseHarness,
) -> None:
    old = release_harness.candidate
    release_harness.install(old, check=True)
    release_harness.mutate_live_restart()
    poison = release_harness.root / "poison"
    (poison / "open_trader").mkdir(parents=True)
    (poison / "open_trader" / "__init__.py").write_text("", encoding="utf-8")
    (poison / "open_trader" / "prediction_release.py").write_text(
        "raise RuntimeError('candidate helper imported')\n", encoding="utf-8"
    )
    release_harness.calls.clear()

    refreshed = release_harness.manager_install(old, inherited_pythonpath=poison)

    assert refreshed.returncode == 0, refreshed.stderr
    assert release_harness.state["pid"] == 5252
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
    record = release_harness.runtime_record
    assert record is not None
    assert record["ready"]["pid"] == 5252

    stopped = release_harness.manager_uninstall(inherited_pythonpath=poison)

    assert stopped.returncode == 0, stopped.stderr
    final_record = release_harness.runtime_record
    assert final_record is not None
    assert final_record["state"] == "stopped"
    assert final_record["ready"]["pid"] == 5252


@pytest.mark.parametrize("change", ["record", "live_identity"])
def test_recovery_refuses_evidence_changed_before_mutation(
    release_harness: ReleaseHarness, change: str,
) -> None:
    old = release_harness.candidate
    live = release_harness.make_checkout("barrier-live")
    target = release_harness.make_checkout("barrier-target")
    release_harness.install(old, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    stale_record_bytes = record_path.read_bytes()
    release_harness.install(live, check=True)
    record_path.write_bytes(stale_record_bytes)
    plist_bytes = release_harness.plist.read_bytes()
    state = release_harness.state
    state.update(
        print_barrier_count=2,
        print_barrier_marker=str(release_harness.root / "barrier.ready"),
        print_barrier_release=str(release_harness.root / "barrier.release"),
        print_count=0,
        bootout_pids=[],
    )
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    marker = Path(state["print_barrier_marker"])
    release = Path(state["print_barrier_release"])
    process = release_harness.install_process(target)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "installer did not reach the mutation-boundary barrier"
        maintenance_bytes = record_path.read_bytes()
        if change == "record":
            record_path.write_bytes(stale_record_bytes)
            expected_record_bytes = stale_record_bytes
        else:
            changed_state = release_harness.state
            changed_state["listener_pid_override"] = int(changed_state["pid"]) + 1
            release_harness.state_path.write_text(
                json.dumps(changed_state), encoding="utf-8"
            )
            expected_record_bytes = maintenance_bytes
        release.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode != 0, stdout + stderr
    assert "before handoff" in stderr
    assert release_harness.state["bootout_pids"] == []
    assert release_harness.state["pid"] == 4242
    assert record_path.read_bytes() == expected_record_bytes
    assert release_harness.plist.read_bytes() == plist_bytes


@pytest.mark.parametrize(
    ("operation", "change"),
    [
        ("same_release", "record"),
        ("same_release", "live_identity"),
        ("uninstall", "record"),
        ("uninstall", "live_identity"),
        ("interrupted_candidate", "live_identity"),
    ],
)
def test_refresh_and_uninstall_refuse_evidence_changed_before_mutation(
    release_harness: ReleaseHarness, operation: str, change: str,
) -> None:
    release = release_harness.candidate
    release_harness.install(release, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    saved_record_bytes = record_path.read_bytes()
    if operation == "interrupted_candidate":
        _write_interrupted_record(
            release_harness, candidate=release, previous=release,
        )
        saved_record_bytes = record_path.read_bytes()
    release_harness.mutate_live_restart()
    plist_bytes = release_harness.plist.read_bytes()

    state = release_harness.state
    state.update(
        lock_barrier_count=1,
        lock_barrier_marker=str(release_harness.root / "lock.ready"),
        lock_barrier_release=str(release_harness.root / "lock.release"),
        lock_probe_count=0,
        bootout_pids=[],
    )
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    marker = Path(state["lock_barrier_marker"])
    release = Path(state["lock_barrier_release"])
    release_harness.calls.clear()
    process = (
        release_harness.install_process(release_harness.candidate)
        if operation != "uninstall"
        else subprocess.Popen(
            [
                str(release_harness.candidate.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", "production", "--runtime-root", str(release_harness.runtime_root),
                "--launch-agents-dir", str(release_harness.agents), "--python", sys.executable,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=release_harness._env(release_harness.candidate),
        )
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "operation did not reach the evidence barrier"
        expected_record_bytes = saved_record_bytes
        if change == "record":
            changed_record = json.loads(saved_record_bytes)
            changed_record["updated_at"] = "2026-09-09T11:30:00+08:00"
            expected_record_bytes = json.dumps(changed_record).encode("utf-8")
            record_path.write_bytes(expected_record_bytes)
        else:
            release_harness.mutate_live_restart(
                pid=6363, started_at="2026-09-09T12:00:00+08:00",
            )
        release.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode != 0, stdout + stderr
    assert "recovered managed prediction release" not in stdout
    assert "stopped managed prediction release" not in stdout
    assert release_harness.state["bootout_pids"] == []
    assert all(
        " bootout " not in f" {call} "
        and " bootstrap " not in f" {call} "
        for call in release_harness.calls.all()
    )
    assert release_harness.state["pid"] == (6363 if change == "live_identity" else 5252)
    assert record_path.read_bytes() == expected_record_bytes
    assert release_harness.plist.read_bytes() == plist_bytes


@pytest.mark.parametrize("operation", ["upgrade", "uninstall"])
def test_changed_health_blocks_destructive_release_operation(
    release_harness: ReleaseHarness, operation: str,
) -> None:
    old = release_harness.candidate
    target = release_harness.make_checkout("health-change-target")
    release_harness.install(old, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    original_record_bytes = record_path.read_bytes()
    plist_bytes = release_harness.plist.read_bytes()
    state = release_harness.state
    state.update(
        print_barrier_count=2,
        print_barrier_marker=str(release_harness.root / "health.ready"),
        print_barrier_release=str(release_harness.root / "health.release"),
        print_count=0,
        bootout_pids=[],
    )
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    marker = Path(state["print_barrier_marker"])
    release = Path(state["print_barrier_release"])
    release_harness.calls.clear()
    if operation == "upgrade":
        process = release_harness.install_process(target)
    else:
        process = subprocess.Popen(
            [
                str(old.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", "production", "--runtime-root", str(release_harness.runtime_root),
                "--launch-agents-dir", str(release_harness.agents), "--python", sys.executable,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=release_harness._env(old),
        )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "operation did not reach the handoff barrier"
        expected_record_bytes = (
            record_path.read_bytes() if operation == "upgrade" else original_record_bytes
        )
        changed_state = release_harness.state
        changed_state["health"]["git_sha"] = "0" * 40
        release_harness.state_path.write_text(
            json.dumps(changed_state), encoding="utf-8"
        )
        release.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode != 0, stdout + stderr
    assert "managed release evidence changed before" in stderr
    assert release_harness.state["bootout_pids"] == []
    assert release_harness.state["pid"] == 4242
    assert release_harness.state["lock_owner_pids"] == [4242]
    assert record_path.read_bytes() == expected_record_bytes
    assert release_harness.plist.read_bytes() == plist_bytes
    assert "installed managed prediction release" not in stdout
    assert "stopped managed prediction release" not in stdout
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())


@pytest.mark.parametrize("operation", ["upgrade", "uninstall"])
def test_release_recheck_ignores_http_load_changes(
    release_harness: ReleaseHarness, operation: str,
) -> None:
    old = release_harness.candidate
    target = release_harness.make_checkout("http-load-target")
    release_harness.install(old, check=True)
    state = release_harness.state
    state["health"]["http_load"] = {
        "limit": 100,
        "active": 3,
        "overload_rejections": 2,
        "history_cache_hits": 11,
        "history_cache_misses": 5,
    }
    state.update(
        print_barrier_count=2,
        print_barrier_marker=str(release_harness.root / "http-load.ready"),
        print_barrier_release=str(release_harness.root / "http-load.release"),
        print_count=0,
        bootout_pids=[],
    )
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    marker = Path(state["print_barrier_marker"])
    release = Path(state["print_barrier_release"])
    release_harness.calls.clear()
    if operation == "upgrade":
        process = release_harness.install_process(target)
    else:
        process = subprocess.Popen(
            [
                str(old.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", "production", "--runtime-root", str(release_harness.runtime_root),
                "--launch-agents-dir", str(release_harness.agents), "--python", sys.executable,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=release_harness._env(old),
        )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "operation did not reach the telemetry barrier"
        changed_state = release_harness.state
        changed_state["health"]["http_load"]["active"] = 7
        changed_state["health"]["http_load"]["history_cache_hits"] = 19
        release_harness.state_path.write_text(
            json.dumps(changed_state), encoding="utf-8"
        )
        release.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == 0, stdout + stderr
    assert "managed release evidence changed before" not in stderr
    record = release_harness.runtime_record
    assert record is not None
    if operation == "upgrade":
        assert release_harness.state["bootout_pids"] == [4242]
        assert release_harness.state["lock_owner_pids"] == [4242]
        assert record["state"] == "ready"
        assert record["candidate"]["git_sha"] == target.sha
        assert record["previous_release"]["git_sha"] == old.sha
        assert record["ready"]["pid"] == 4242
    else:
        assert release_harness.state["bootout_pids"] == [4242]
        assert record["state"] == "stopped"
        assert record["candidate"]["git_sha"] == old.sha
        assert record["ready"]["pid"] == 4242


@pytest.mark.parametrize("contender", ["upgrade", "uninstall"])
def test_release_operations_are_exclusive(
    release_harness: ReleaseHarness, contender: str, monkeypatch, request,
) -> None:
    old = release_harness.candidate
    # This node defers real cloud loading and retains opt-in observation.
    # Select its fixture bootstrap before disposable source commit/artifact assembly.
    bootstrap_recording = r'''import os as _phase_os
_phase_path = _phase_os.environ.get('LP_EXCLUSIVE_BOOTSTRAP_PHASES')
if _phase_path:
    import atexit as _phase_atexit, json as _phase_json, sys as _phase_sys, time as _phase_time
    _phase_fd = _phase_os.open(_phase_path, _phase_os.O_WRONLY | _phase_os.O_APPEND | _phase_os.O_CREAT, 0o600)
    def _phase_record(phase, **facts):
        raw = _phase_json.dumps(dict(phase=phase, pid=_phase_os.getpid(),
            ppid=_phase_os.getppid(), pgid=_phase_os.getpgid(0),
            monotonic_ns=_phase_time.monotonic_ns(), bootstrap=__file__,
            argv0=_phase_sys.argv[0], **facts), separators=(',', ':')).encode() + b'\n'
        _phase_os.write(_phase_fd, raw)
    def _phase_finish():
        try: _phase_record('atexit')
        finally: _phase_os.close(_phase_fd)
    _phase_record('entry')
    _phase_atexit.register(_phase_finish)
'''
    cloud_import = 'from open_trader import prediction_cloud'
    assert MANAGER_EXTERNAL_BOUNDARIES.count(cloud_import) == 1
    cloud_setup = cloud_import + '\nprediction_cloud.UNIT_PATH = Path(__file__).parent / "fixture-absent-systemd.service"'
    assert MANAGER_EXTERNAL_BOUNDARIES.count(cloud_setup) == 1
    lazy_cloud_setup = r'''import sys as _cloud_sys
_cloud_name = 'open_trader.prediction_cloud'
_cloud_finders = tuple(_cloud_sys.meta_path)
def _cloud_isolate(module, loader, requester):
    # The real module code has run. Preserve its actual file/spec/loader and
    # set the original external fixture boundary before import returns.
    module.UNIT_PATH = Path(__file__).parent / 'fixture-absent-systemd.service'
    if _phase_path:
        _phase_record('cloud_isolated', module_file=module.__file__,
            spec_origin=module.__spec__.origin, spec_name=module.__spec__.name,
            unit_path=str(module.UNIT_PATH), native_loader=type(loader).__name__,
            loader_identity_preserved=(module.__loader__ is loader and module.__spec__.loader is loader),
            requester_module=requester[0], requester_function=requester[1],
            requester_file=requester[2])
class _CloudPostLoadFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _cloud_name:
            return None
        requester = ('', '', '')
        frame = _cloud_sys._getframe(1)
        while frame is not None:
            if frame.f_globals.get('__name__') == 'open_trader.prediction_start_guard':
                requester = (frame.f_globals['__name__'], frame.f_code.co_name, frame.f_code.co_filename)
                break
            frame = frame.f_back
        for finder in _cloud_finders:
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            loader = spec.loader
            native_exec = loader.exec_module
            def execute(module):
                if _phase_path: _phase_record('before_cloud')
                try:
                    result = native_exec(module)
                finally:
                    # Keep the original loader object/interface and metadata.
                    loader.exec_module = native_exec
                if _phase_path: _phase_record('after_cloud', module_file=module.__file__,
                    spec_origin=module.__spec__.origin, native_exec_restored=(loader.exec_module is native_exec))
                _cloud_isolate(module, loader, requester)
                return result
            loader.exec_module = execute
            return spec
        return None
if _cloud_name in _cloud_sys.modules:
    _cloud_existing = _cloud_sys.modules[_cloud_name]
    if _cloud_existing is None:
        raise ModuleNotFoundError('real cloud import blocked in sys.modules', name=_cloud_name)
    if _phase_path: _phase_record('cloud_already_loaded', module_file=_cloud_existing.__file__)
    _cloud_isolate(_cloud_existing, _cloud_existing.__loader__, ('already_loaded', '', ''))
_cloud_sys.meta_path.insert(0, _CloudPostLoadFinder())
'''
    observed_bootstrap = bootstrap_recording + MANAGER_EXTERNAL_BOUNDARIES.replace(
        cloud_setup, lazy_cloud_setup)
    observed_bootstrap += "\nif _phase_path: _phase_record('installed')\n"
    with monkeypatch.context() as bootstrap_selection:
        bootstrap_selection.setitem(globals(), 'MANAGER_EXTERNAL_BOUNDARIES', observed_bootstrap)
        next_release = release_harness.make_checkout("exclusive-next")
        final_release = release_harness.make_checkout("exclusive-final")
    bootstrap_phase_path = release_harness.root / 'exclusive-bootstrap-phases.jsonl'
    release_harness.install(old, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    data_sentinel = release_harness.runtime_root / "data" / "sentinel"
    data_sentinel.parent.mkdir(parents=True, exist_ok=True)
    data_sentinel.write_bytes(b"keep")
    # Prepare through the actual public installer before installing contention.
    # Only its pending selection may change; the old owner stays untouched.
    import hashlib
    from open_trader.prediction_release import validate_lp_validation
    before_prepare=release_harness.runtime_record
    prior_pending=before_prepare.get('pending_lp_validation')
    state_before_prepare=release_harness.state
    calls_before_prepare=release_harness.calls.all()
    plist_before_prepare=release_harness.plist.read_bytes()
    permit_path=release_harness.runtime_root/'lp-start-guard/permit.json'
    permit_before_prepare=permit_path.read_bytes()
    native_popen=subprocess.Popen
    prepare_commands=[]
    def prepare_popen(command,*args,**kwargs):
        if isinstance(command,list) and command and command[0]==str(
                next_release.path/'scripts/install_prediction_service_launchd.sh'):
            command=[*command,'--prepare-target']
            prepare_commands.append(command)
        return native_popen(command,*args,**kwargs)
    with monkeypatch.context() as preparation:
        preparation.setattr(subprocess,'Popen',prepare_popen)
        prepared_result=release_harness.install(next_release)
    proof_path=release_harness.root/'exclusive-preparation.json'
    prepared_record=release_harness.runtime_record
    proof=dict(commands=prepare_commands,returncode=prepared_result.returncode,
        stdout=prepared_result.stdout,stderr=prepared_result.stderr,
        record_before=before_prepare,record_after=prepared_record,
        prior_pending=prior_pending,state_before=state_before_prepare,
        state_after=release_harness.state,
        calls=release_harness.calls.all()[len(calls_before_prepare):])
    proof_path.write_text(json.dumps(proof))
    assert prepared_result.returncode==0,prepared_result.stderr
    prepared=json.loads(prepared_result.stdout)
    assert prepared['status']=='PREPARED'
    operation_nonce=prepared['operation_nonce']
    assert isinstance(operation_nonce,str) and operation_nonce
    assert len(prepare_commands)==1 and prepare_commands[0][-1]=='--prepare-target'
    assert {k:v for k,v in prepared_record.items() if k!='pending_lp_validation'}=={
        k:v for k,v in before_prepare.items() if k!='pending_lp_validation'}
    pending=prepared_record['pending_lp_validation']
    assert pending['operation_nonce']==operation_nonce
    assert pending['source_sha']==next_release.sha
    assert pending['release_root']==str(next_release.path.resolve())
    assert pending['runtime_root']==str(release_harness.runtime_root.resolve())
    assert pending['python']==sys.executable
    assert pending['config_selection']==dict(path=str(release_harness.config.resolve()),
        sha256=hashlib.sha256(release_harness.config.read_bytes()).hexdigest())
    assert validate_lp_validation(pending,release_root=next_release.path,
        runtime_root=release_harness.runtime_root,python=sys.executable,
        config_path=release_harness.config)==pending
    assert state_before_prepare['loaded'] is True
    assert state_before_prepare['owner_available'] is False
    assert state_before_prepare['cwd']==str(old.path)
    assert release_harness.state==state_before_prepare
    assert [call for call in release_harness.calls.all() if ' bootout ' in f' {call} '
        or ' bootstrap ' in f' {call} ']==[call for call in calls_before_prepare
        if ' bootout ' in f' {call} ' or ' bootstrap ' in f' {call} ']
    assert release_harness.plist.read_bytes()==plist_before_prepare
    assert permit_path.read_bytes()==permit_before_prepare
    assert data_sentinel.read_bytes()==b'keep'
    prepared_remote_calls=release_harness.calls.named('gh')
    assert len(prepared_remote_calls)>len([call for call in calls_before_prepare if call.startswith('gh ')])
    state = release_harness.state
    state.update(
        print_barrier_count=1,
        print_barrier_marker=str(release_harness.root / "exclusive.ready"),
        print_barrier_release=str(release_harness.root / "exclusive.release"),
        print_count=0,
    )
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    marker = Path(state["print_barrier_marker"])
    release = Path(state["print_barrier_release"])
    record_bytes = record_path.read_bytes()
    plist_bytes = release_harness.plist.read_bytes()
    # Opt-in observation for this node only. Keep the actual shebang/installer
    # and native verifier untouched; Bash builtins write to a separate file FD.
    trace_path=release_harness.root/'exclusive-native-trace.log'
    trace_handle=trace_path.open('ab',buffering=0)
    request.addfinalizer(trace_handle.close)
    trace_fd=trace_handle.fileno()
    trace_setup=release_harness.root/'exclusive-native-trace-env.sh'
    debug=("printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
        '"$SECONDS" "${BASHPID:-$$}" "${BASH_SOURCE[0]}" "$LINENO" "$?" "$BASH_COMMAND" '
        f'>&{trace_fd}')
    import shlex
    trace_setup.write_text(f"if {{ : >&{trace_fd}; }} 2>/dev/null; then\n"
        f"  printf 'BASH_START %s %s\\n' \"$BASH_VERSION\" \"$$\" >&{trace_fd}\n"
        "  set -o functrace\n"
        f"  trap {shlex.quote(debug)} DEBUG\n"
        "fi\n")
    native_popen=subprocess.Popen
    def observed_popen(command,*args,**kwargs):
        if isinstance(command,list) and command and command[0]==str(
                next_release.path/'scripts/install_prediction_service_launchd.sh'):
            command=[*command,'--operation-nonce',operation_nonce]
            kwargs['env']={**kwargs['env'],'BASH_ENV':str(trace_setup),
                'LP_EXCLUSIVE_BOOTSTRAP_PHASES':str(bootstrap_phase_path)}
            kwargs['pass_fds']=(*kwargs.get('pass_fds',()),trace_fd)
        return native_popen(command,*args,**kwargs)
    process_started=time.monotonic()
    with monkeypatch.context() as observation:
        observation.setattr(subprocess,'Popen',observed_popen)
        process = release_harness.install_process(next_release)
    identity_path=release_harness.root/'exclusive-native-process.json'
    process_identity=dict(pid=process.pid,pgid=process.pid,started_at=process_started,
        trace_path=str(trace_path),selected_target=str(next_release.path),
        selected_sha=next_release.sha,operation_nonce=operation_nonce,
        bootstrap_phase_path=str(bootstrap_phase_path))
    identity_path.write_text(json.dumps(process_identity))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "installer did not enter the release operation"
        if contender == "upgrade":
            busy_result = release_harness.install(final_release)
        else:
            busy_result = release_harness.uninstall()
        assert busy_result.returncode != 0
        assert "release operation already in progress" in busy_result.stderr
        preflight = release_harness.install(
            old, mode="production", preflight=True,
        )
        assert preflight.returncode == 1
        assert json.loads(preflight.stdout)["status"] == "BLOCKED"
        assert "release operation already in progress" in json.loads(preflight.stdout)["reason"]
        shadow_root = release_harness.root / "shadow-runtime"
        shadow = release_harness.install(
            old, mode="shadow", runtime_root=shadow_root,
        )
        assert shadow.returncode != 0
        assert "release operation already in progress" in shadow.stderr
        assert record_path.read_bytes() == record_bytes
        assert release_harness.plist.read_bytes() == plist_bytes
        assert data_sentinel.read_bytes() == b"keep"
    finally:
        process_identity['released_at']=time.monotonic()
        release.touch()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # Preserve actual native descendant identity before existing kill.
            # This is an observation, never a completion/readiness substitute.
            try:
                native_ps=subprocess.run(['/bin/ps','-axo','pid=,ppid=,pgid=,stat=,etime=,args='],
                    capture_output=True,text=True,timeout=1)
                members=[]
                for line in native_ps.stdout.splitlines():
                    fields=line.split(None,5)
                    if len(fields)==6 and int(fields[2])==process.pid:
                        members.append(dict(zip(('pid','ppid','pgid','state','elapsed','command'),fields)))
                process_identity['timeout_observation']=dict(observed_at=time.monotonic(),
                    ps_returncode=native_ps.returncode,ps_stderr=native_ps.stderr,members=members)
            except Exception as exc:
                process_identity['timeout_observation']=dict(error_type=type(exc).__name__,error=str(exc))
            identity_path.write_text(json.dumps(process_identity))
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
            process_identity.update(returncode=process.returncode,killed_after_timeout=True)
            identity_path.write_text(json.dumps(process_identity))
            # CI retains test.log/JUnit, not pytest's temporary directory.
            # Emit only after native termination; never fill child pipes.
            print('F_NATIVE_TIMEOUT '+json.dumps(process_identity),flush=True)
            try:print('F_NATIVE_TRACE_TAIL\n'+trace_path.read_text()[-32768:],flush=True)
            except OSError as exc:print('F_NATIVE_TRACE_UNKNOWN '+repr(exc),flush=True)
            raise AssertionError("installer did not stop after release:\n" + stdout + stderr)
        if process.returncode != 0:
            raise AssertionError(stdout + stderr)
        process_identity.update(returncode=process.returncode,ended_at=time.monotonic())
        identity_path.write_text(json.dumps(process_identity))

    phase_records=[json.loads(line) for line in bootstrap_phase_path.read_text().splitlines()]
    assert phase_records and all(row['pgid']==process.pid for row in phase_records)
    assert all(Path(row['bootstrap']).resolve()==(next_release.path/'src/sitecustomize.py').resolve()
        for row in phase_records)
    known_os={'launchctl','lsof','curl','owner-probe','ps'}
    os_pids={row['pid'] for row in phase_records if Path(row['argv0']).name in known_os}
    assert os_pids
    cloud_phases={'before_cloud','after_cloud','cloud_isolated','cloud_already_loaded'}
    assert not any(row['pid'] in os_pids and row['phase'] in cloud_phases for row in phase_records)
    actual_cloud=[row for row in phase_records if row['phase']=='cloud_isolated']
    assert actual_cloud and any(row['requester_module']=='open_trader.prediction_start_guard'
        and row['requester_function']=='_installed_systemd_selects' for row in actual_cloud)
    for row in actual_cloud:
        assert Path(row['module_file']).resolve()==(next_release.path/'src/open_trader/prediction_cloud.py').resolve()
        assert row['spec_origin']==row['module_file'] and row['spec_name']=='open_trader.prediction_cloud'
        assert row['loader_identity_preserved'] is True
        assert Path(row['unit_path'])==next_release.path/'src/fixture-absent-systemd.service'
        same_pid={event['phase']:event for event in phase_records if event['pid']==row['pid']}
        assert ('cloud_already_loaded' in same_pid or
            {'before_cloud','after_cloud'}<=set(same_pid))
        if 'after_cloud' in same_pid:
            assert same_pid['after_cloud']['native_exec_restored'] is True
            assert same_pid['after_cloud']['monotonic_ns']<=row['monotonic_ns']
    assert release_harness.calls.named('gh')==prepared_remote_calls
    assert release_harness.runtime_record['candidate']['lp_validation']['operation_nonce']==operation_nonce
    assert release_harness.state["max_pids"] == 1
    lock_path = release_harness.agents / f".{LABEL}.release.lock"
    assert lock_path.exists()
    shadow_retry = release_harness.install(
        old, mode="shadow", runtime_root=release_harness.root / "shadow-runtime",
    )
    assert shadow_retry.returncode != 0
    assert "production" in shadow_retry.stderr
    if contender == "upgrade":
        retry = release_harness.install(final_release)
        assert retry.returncode == 0, retry.stderr
        record = release_harness.runtime_record
        assert record is not None
        assert record["candidate"]["git_sha"] == final_release.sha
        assert record["previous_release"]["git_sha"] == next_release.sha
    else:
        retry = release_harness.uninstall()
        assert retry.returncode == 0, retry.stderr
        record = release_harness.runtime_record
        assert record is not None
        assert record["state"] == "stopped"
        assert record["candidate"]["git_sha"] == next_release.sha


# Approved TDD case B: a verified live release can replace a stale saved record.
@pytest.mark.parametrize("operation", ["same_release", "upgrade", "uninstall"])
def test_verified_stale_release_record_is_recovered(
    release_harness: ReleaseHarness, operation: str
) -> None:
    old = release_harness.candidate
    live = release_harness.make_checkout("stale-live")
    release_harness.install(old, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    stale_record_bytes = record_path.read_bytes()
    release_harness.install(live, check=True)
    live_entry = release_harness.runtime_record["candidate"]
    permit_path = release_harness.runtime_root / "lp-start-guard/permit.json"
    live_permit_bytes = permit_path.read_bytes()
    # A stale return-to-old selection can retain its actually running previous
    # release. Both entries and their validation receipts come from real installs.
    stale_record = json.loads(stale_record_bytes)
    stale_record["previous_release"] = live_entry
    stale_record_bytes = json.dumps(stale_record).encode("utf-8")
    record_path.write_bytes(stale_record_bytes)
    state_after_live = release_harness.state
    state_after_live["bootout_pids"] = []
    release_harness.state_path.write_text(json.dumps(state_after_live), encoding="utf-8")
    release_harness.calls.clear()

    if operation == "same_release":
        result = release_harness.install(live)
    elif operation == "upgrade":
        target = release_harness.make_checkout("stale-upgrade")
        result = release_harness.install(target)
    else:
        result = release_harness.uninstall()

    assert result.returncode == 0, result.stderr
    archives = list((release_harness.runtime_root / "audit").glob(
        "prediction-service-runtime.json.recovery-*.bak"
    ))
    assert len(archives) == 1
    assert archives[0].read_bytes() == stale_record_bytes
    record = release_harness.runtime_record
    assert record is not None
    assert record.get("recovery_reason")
    if operation == "same_release":
        assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
        assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
        assert record["state"] == "ready"
        assert record["candidate"]["git_sha"] == live.sha
        assert record["candidate"]["lp_validation"] == live_entry["lp_validation"]
        assert "pending_lp_validation" not in record
        assert permit_path.read_bytes() == live_permit_bytes
    elif operation == "upgrade":
        assert release_harness.state["bootout_pids"] == [4242]
        assert record["candidate"]["git_sha"] == target.sha
        assert record["previous_release"]["git_sha"] == live.sha
    else:
        assert release_harness.state["bootout_pids"] == [4242]
        assert record["state"] == "stopped"
        assert record["candidate"]["git_sha"] == live.sha


def _write_interrupted_record(
    release_harness: ReleaseHarness,
    *,
    candidate: ReleaseCheckout,
    previous: ReleaseCheckout,
    state: str = "maintenance",
) -> None:
    release_harness.runtime_root.mkdir(parents=True, exist_ok=True)
    existing = release_harness.runtime_record or {}
    record = {
        "schema_version": "open_trader.prediction_service.runtime.v1",
        "state": state,
        "candidate": {
            "checkout": str(candidate.path), "git_sha": candidate.sha,
            "source_state": "clean",
            "reader_generation": candidate.reader_generation,
            "contract_generation": candidate.contract_generation,
        },
        "previous_release": {
            "checkout": str(previous.path), "git_sha": previous.sha,
            "source_state": "clean",
            "reader_generation": previous.reader_generation,
            "contract_generation": previous.contract_generation,
        },
        "transition_started_at": "2026-09-09T10:55:00+08:00",
        "updated_at": "2026-09-09T10:55:00+08:00",
        "failure_reason": "" if state == "maintenance" else "candidate_timeout",
    }
    for name in ("manager", "manager_identity"):
        if name in existing:
            record[name] = existing[name]
    for destination in ("candidate", "previous_release"):
        identity = record[destination]
        source_identity = dict(identity)
        for source in (existing.get("candidate"), existing.get("previous_release")):
            if (isinstance(source, dict)
                    and {key: value for key, value in source.items()
                         if key not in ("lp_validation", "manifest")} == source_identity
                    and "lp_validation" in source):
                if "lp_validation" in identity:
                    assert identity["lp_validation"] == source["lp_validation"]
                identity["lp_validation"] = source["lp_validation"]
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    with open(record_path, "w", encoding="utf-8",
              opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(json.dumps(record))


# Approved TDD case C: retry follows the observed interrupted phase.
@pytest.mark.parametrize(
    ("phase", "record_state"),
    [
        ("old_running", "maintenance"),
        ("candidate_ready", "maintenance"),
        ("owner_absent", "maintenance"),
        ("failed_owner_absent", "failed"),
    ],
)
def test_interrupted_release_retry_uses_observed_phase(
    release_harness: ReleaseHarness, phase: str, record_state: str
) -> None:
    old = release_harness.candidate
    candidate = release_harness.make_checkout("interrupted-candidate")
    if phase == "old_running":
        release_harness.install(old, check=True)
    elif phase == "candidate_ready":
        release_harness.install(candidate, check=True)
    else:
        release_harness.configure("absent")
        assert not (release_harness.runtime_root / "data").exists()
    _write_interrupted_record(
        release_harness, candidate=candidate, previous=old, state=record_state
    )
    permit_path = release_harness.runtime_root / "lp-start-guard/permit.json"
    prior_permit = permit_path.read_bytes() if phase == "candidate_ready" else None
    release_harness.calls.clear()

    result = release_harness.install(candidate)

    assert result.returncode == 0, result.stderr
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"
    assert record["candidate"]["git_sha"] == candidate.sha
    assert record["previous_release"]["git_sha"] == old.sha
    if phase == "candidate_ready":
        assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
        assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
        assert permit_path.read_bytes() == prior_permit
        assert "pending_lp_validation" not in record
    elif phase == "old_running":
        assert release_harness.state["bootout_pids"] == [4242]
    else:
        assert sum(" bootstrap " in f" {call} " for call in release_harness.calls.all()) == 1
        assert release_harness.state["max_pids"] == 1
        for directory in (release_harness.runtime_root / "data",
                          release_harness.runtime_root / "data/prediction_arbitrage"):
            assert directory.is_dir()
            assert directory.stat().st_uid == os.getuid()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700


# Approved TDD case D: shadow commands cannot manage a live production label.
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_shadow_commands_refuse_managed_production(
    release_harness: ReleaseHarness, operation: str
) -> None:
    release_harness.install(mode="production", check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    record_bytes = record_path.read_bytes()
    plist_bytes = release_harness.plist.read_bytes()
    sentinel = release_harness.runtime_root / "data" / "sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"keep")
    state_before = release_harness.state
    result = (
        release_harness.install(mode="shadow")
        if operation == "install"
        else release_harness.uninstall(mode="shadow")
    )
    assert result.returncode != 0
    assert "production" in result.stderr
    assert release_harness.state["pid"] == state_before["pid"]
    assert release_harness.state["loaded"] is True
    assert record_path.read_bytes() == record_bytes
    assert release_harness.plist.read_bytes() == plist_bytes
    assert sentinel.read_bytes() == b"keep"


# Approved TDD case E: read-only production preflight classifies without mutation.
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("ready", "READY", 0),
        ("restart", "RECOVERABLE", 0),
        ("stale", "RECOVERABLE", 0),
        ("absent", "READY", 0),
        ("unknown_listener", "BLOCKED", 1),
        ("dirty_requested", "BLOCKED", 1),
        ("missing_pid", "BLOCKED", 1),
        ("missing_record", "BLOCKED", 1),
        ("real_lock_unheld", "READY", 0),
        ("real_lock_held", "BLOCKED", 1),
    ],
)
def test_release_preflight_reports_without_mutation(
    release_harness: ReleaseHarness,
    case: str,
    expected_status: str,
    expected_code: int,
) -> None:
    if case == "ready":
        release_harness.install(mode="production", check=True)
        checkout = release_harness.candidate
    elif case == "restart":
        release_harness.install(mode="production", check=True)
        release_harness.mutate_live_restart()
        checkout = release_harness.candidate
    elif case == "stale":
        old = release_harness.candidate
        checkout = release_harness.make_checkout("preflight-live")
        release_harness.install(old, check=True)
        record_path = release_harness.runtime_root / "prediction-service-runtime.json"
        stale_bytes = record_path.read_bytes()
        release_harness.install(checkout, check=True)
        record_path.write_bytes(stale_bytes)
    elif case == "dirty_requested":
        checkout = release_harness.candidate
        (checkout.path / "dirty-requested.txt").write_text("dirty", encoding="utf-8")
    elif case == "missing_pid":
        checkout = release_harness.candidate
        release_harness.configure("absent")
        state = release_harness.state
        state.update(loaded=True, pid=0, cwd=str(checkout.path), plist=str(release_harness.plist))
        release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    elif case == "missing_record":
        checkout = release_harness.candidate
        release_harness.install(checkout, mode="production", check=True)
        (release_harness.runtime_root / "prediction-service-runtime.json").unlink()
    elif case in {"real_lock_unheld", "real_lock_held"}:
        checkout = release_harness.candidate
        release_harness.configure("absent")
        lock_path = release_harness.runtime_root / "data" / "prediction_arbitrage" / "runtime.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.touch()
    else:
        checkout = release_harness.candidate
        release_harness.configure("absent" if case == "absent" else "unknown_listener")
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    before = {
        "record": record_path.read_bytes() if record_path.exists() else None,
        "plist": release_harness.plist.read_bytes() if release_harness.plist.exists() else None,
        "state": release_harness.state_path.read_bytes(),
        "runtime_exists": release_harness.runtime_root.exists(),
    }
    release_harness.calls.clear()
    owner_probe = case not in {
        "absent", "real_lock_unheld", "real_lock_held",
    }
    lock_handle = None
    if case == "real_lock_held":
        lock_path = release_harness.runtime_root / "data" / "prediction_arbitrage" / "runtime.lock"
        lock_handle = lock_path.open("rb")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    try:
        result = release_harness.install(
            checkout, mode="production", preflight=True,
            owner_probe=owner_probe,
        )
    finally:
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
    payload = json.loads(result.stdout)
    assert result.returncode == expected_code
    assert payload["schema_version"] == "open_trader.prediction_service.preflight.v1"
    assert payload["status"] == expected_status
    assert payload["managed_identity_verified"] is (case in {"ready", "restart", "stale"})
    assert payload["reason"]
    if case in {"restart", "stale"}:
        assert set(payload["recorded_release"]) == {
            "checkout", "git_sha", "source_state",
            "reader_generation", "contract_generation",
        }
        assert payload["recorded_release"]["checkout"] == str(
            release_harness.candidate.path
            if case == "restart" else release_harness.candidate.path
        )
        assert payload["recorded_release"]["git_sha"] == release_harness.candidate.sha
    if case == "restart":
        assert payload["recorded_ready"] == {
            "pid": 4242,
            "process_started_at": "2026-08-11T10:00:00+08:00",
        }
        assert payload["observed_ready"] == {
            "pid": 5252,
            "process_started_at": "2026-09-09T11:00:00+08:00",
        }
    if case == "stale":
        assert payload["observed_release"]["checkout"] == str(checkout.path)
        assert payload["observed_release"]["git_sha"] == checkout.sha
    assert (record_path.read_bytes() if record_path.exists() else None) == before["record"]
    assert (release_harness.plist.read_bytes() if release_harness.plist.exists() else None) == before["plist"]
    assert release_harness.state_path.read_bytes() == before["state"]
    assert release_harness.runtime_root.exists() == before["runtime_exists"]
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())


# Approved TDD case F: conflicting independent observations remain fail-closed.
@pytest.mark.parametrize(
    "corruption",
    [
        "manager_listener_pid",
        "manager_plist_path",
        "manager_cwd",
        "wrong_health_sha",
        "dirty_source",
        "wrong_data_root",
        "wrong_lock_owner",
        "multiple_lock_openers",
        "health_unavailable",
        "missing_loaded_manifest",
    ],
)
def test_recovery_refuses_conflicting_live_evidence(
    release_harness: ReleaseHarness, corruption: str
) -> None:
    old = release_harness.candidate
    new = release_harness.make_checkout("recovery-current")
    release_harness.install(old, check=True)
    record_path = release_harness.runtime_root / "prediction-service-runtime.json"
    stale_record_bytes = record_path.read_bytes()
    release_harness.install(new, check=True)
    plist_bytes = release_harness.plist.read_bytes()
    record_path.write_bytes(stale_record_bytes)
    state = release_harness.state
    if corruption == "manager_listener_pid":
        state["listener_pid_override"] = int(state["pid"]) + 1
    elif corruption == "manager_plist_path":
        state["plist"] = str(release_harness.root / "other.plist")
    elif corruption == "manager_cwd":
        state["label_cwd"] = str(release_harness.root / "wrong-cwd")
    elif corruption == "wrong_health_sha":
        state["health"]["git_sha"] = "0" * 40
    elif corruption == "dirty_source":
        (new.path / "untracked.txt").write_text("dirty", encoding="utf-8")
    elif corruption == "wrong_data_root":
        args = state["arguments"]
        args[args.index("--data-dir") + 1] = str(release_harness.root / "other-data")
    elif corruption == "wrong_lock_owner":
        state["lock_owner_pid"] = int(state["pid"]) + 1
        state["lock_owner_pids"] = [int(state["pid"]) + 1]
    elif corruption == "multiple_lock_openers":
        state["lock_owner_pids"] = [int(state["pid"]), int(state["pid"]) + 1]
    elif corruption == "missing_loaded_manifest":
        arguments = state["arguments"]
        manifest_index = arguments.index("--release-manifest")
        del arguments[manifest_index:manifest_index + 2]
    else:
        state["health"] = {}
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")
    release_harness.calls.clear()

    result = release_harness.install(new)

    assert result.returncode != 0
    assert "managed" in result.stderr or "release root is dirty" in result.stderr
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
    assert record_path.read_bytes() == stale_record_bytes
    assert release_harness.plist.read_bytes() == plist_bytes


def test_custom_tracked_manifest_supports_release_recovery(
    release_harness: ReleaseHarness,
) -> None:
    custom = release_harness.make_checkout("custom-manifest")
    default_manifest = custom.path / "ops" / "prediction-service-release.json"
    nested_manifest = custom.path / "nested" / "custom" / "release.json"
    nested_manifest.parent.mkdir(parents=True)
    shutil.copy2(default_manifest, nested_manifest)
    subprocess.run(
        ["git", "-C", str(custom.path), "add", str(nested_manifest)],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(custom.path), "-c", "user.name=test",
            "-c", "user.email=test@example.com", "commit", "-qm",
            "nested manifest",
        ],
        check=True,
    )
    custom = ReleaseCheckout(
        path=custom.path,
        sha=subprocess.run(
            ["git", "-C", str(custom.path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip(),
        reader_generation=custom.reader_generation,
        contract_generation=custom.contract_generation,
    )

    installed = release_harness.install(
        custom, release_manifest=nested_manifest, check=True,
    )
    assert installed.returncode == 0
    assert release_harness.state["arguments"][
        release_harness.state["arguments"].index("--release-manifest") + 1
    ] == str(nested_manifest)
    release_harness.mutate_live_restart()
    release_harness.calls.clear()

    refreshed = release_harness.install(
        custom, release_manifest=nested_manifest,
    )
    assert refreshed.returncode == 0, refreshed.stderr
    assert release_harness.state["pid"] == 5252
    assert all(" bootout " not in f" {call} " for call in release_harness.calls.all())
    assert all(" bootstrap " not in f" {call} " for call in release_harness.calls.all())
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"
    assert record["candidate"]["checkout"] == str(custom.path)
    assert record["candidate"]["git_sha"] == custom.sha
    assert record["ready"]["pid"] == 5252

    stopped = release_harness.uninstall()
    assert stopped.returncode == 0, stopped.stderr
    final_record = release_harness.runtime_record
    assert final_record is not None
    assert final_record["state"] == "stopped"
    assert final_record["candidate"]["checkout"] == str(custom.path)
    assert final_record["candidate"]["git_sha"] == custom.sha
    assert final_record["ready"]["pid"] == 5252
    assert not release_harness.plist.exists()


# Approved TDD case G: a final record publication failure cannot claim success.
def test_release_record_write_failure_is_not_success(
    release_harness: ReleaseHarness,
) -> None:
    state = release_harness.state
    state["case"] = "record_write_failure"
    release_harness.state_path.write_text(json.dumps(state), encoding="utf-8")

    result = release_harness.install()

    assert result.returncode != 0
    assert "installed managed prediction release" not in result.stdout
    assert not release_harness.plist.exists()
    assert release_harness.state["loaded"] is False
    assert release_harness.owner_is_available()
    assert (release_harness.runtime_root / "prediction-service-runtime.json").is_dir()


# Approved TDD case H: each upgrade persists the release actually observed.
def test_consecutive_upgrades_keep_runtime_record_current(
    release_harness: ReleaseHarness,
) -> None:
    releases = [
        release_harness.candidate,
        release_harness.make_checkout("consecutive-b"),
        release_harness.make_checkout("consecutive-c"),
    ]
    for index, release in enumerate(releases):
        result = release_harness.install(release)
        assert result.returncode == 0, result.stderr
        record = release_harness.runtime_record
        assert record is not None
        assert record["candidate"]["git_sha"] == release.sha
        if index:
            assert record["previous_release"]["git_sha"] == releases[index - 1].sha
    assert release_harness.state["max_pids"] == 1
