from __future__ import annotations

from dataclasses import dataclass
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from open_trader.prediction_release import load_prediction_runtime_record


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
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
calls_path = Path(os.environ["FAKE_CALLS"])
state = json.loads(state_path.read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(command + " " + " ".join(sys.argv[1:]) + "\n")

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

if command == "launchctl":
    action = sys.argv[1]
    if action == "print":
        if state["loaded"]:
            print(f"path = {state['plist']}")
            print(f"working directory = {state['cwd']}")
            print(f"stdout path = {state['stdout_log']}")
            print(f"stderr path = {state['stderr_log']}")
            print(f"pid = {state['pid']}")
            raise SystemExit(0)
        print("Could not find service", file=sys.stderr)
        raise SystemExit(113)
    if action == "bootout":
        state.update(loaded=False, pid=0, cwd="", listener=False,
                     owner_available=True, health={}, plist="",
                     stdout_log="", stderr_log="")
        save()
        raise SystemExit(0)
    if action == "bootstrap":
        if state["case"] == "keepalive_exited":
            state.update(loaded=True, pid=0,
                         cwd=os.environ["FAKE_CANDIDATE_CWD"],
                         listener=False, owner_available=True, health={},
                         plist=sys.argv[-1],
                         stdout_log=state["stdout_log"] or os.environ["FAKE_STDOUT_LOG"],
                         stderr_log=state["stderr_log"] or os.environ["FAKE_STDERR_LOG"])
        elif state["case"] in {"bind_failure", "reconcile_failure", "incompatible_reader"}:
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
            if state["case"] in {"wrong_health_sha", "keepalive_restart"}:
                health["git_sha"] = "wrong"
            if state["case"] == "wrong_health_generation":
                health["reader_generation"] += 1
            state.update(loaded=True, pid=pid, cwd=health["cwd"], listener=True,
                         owner_available=False, health=health,
                         plist=sys.argv[-1],
                         stdout_log=state["stdout_log"] or os.environ["FAKE_STDOUT_LOG"],
                         stderr_log=state["stderr_log"] or os.environ["FAKE_STDERR_LOG"],
                         max_pids=max(int(state["max_pids"]), 1))
        save()
        raise SystemExit(0)

if command == "lsof":
    if "-d" in sys.argv and "cwd" in sys.argv:
        if state["pid"]:
            print(f"p{state['pid']}\nfcwd\nn{state['cwd']}")
            raise SystemExit(0)
        raise SystemExit(1)
    if state["listener"]:
        print(f"p{state['pid']}\nn127.0.0.1:8769")
        raise SystemExit(0)
    raise SystemExit(1)

if command == "curl":
    if state["health"]:
        health = state["health"]
        if state["case"] == "keepalive_restart":
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
    raise SystemExit(0 if state["pid"] and str(state["pid"]) in sys.argv else 1)

if command == "owner-probe":
    raise SystemExit(0 if state["owner_available"] else 1)
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
        self.candidate = self.make_checkout("candidate")
        self.configure("absent")

    def make_checkout(self, name: str) -> ReleaseCheckout:
        path = self.root / name
        (path / "scripts").mkdir(parents=True)
        (path / "ops" / "launchd").mkdir(parents=True)
        (path / "src").mkdir(parents=True)
        shutil.copytree(ROOT / "src" / "open_trader", path / "src" / "open_trader")
        for script in (
            "install_prediction_service_launchd.sh",
            "uninstall_prediction_service_launchd.sh",
        ):
            shutil.copy2(ROOT / "scripts" / script, path / "scripts" / script)
        shutil.copy2(
            ROOT / "ops" / "launchd" / f"{LABEL}.plist.template",
            path / "ops" / "launchd" / f"{LABEL}.plist.template",
        )
        shutil.copy2(
            ROOT / "ops" / "prediction-service-release.json",
            path / "ops" / "prediction-service-release.json",
        )
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=test", "-c",
             "user.email=test@example.com", "commit", "-qm", name],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return ReleaseCheckout(path=path, sha=sha)

    def _write_dispatcher(self) -> None:
        dispatcher = self.fake_bin / "fake-command"
        dispatcher.write_text(FAKE_COMMAND_SOURCE, encoding="utf-8")
        dispatcher.chmod(0o755)
        for name in ("launchctl", "lsof", "curl", "ps", "owner-probe"):
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
        }
        if case == "unknown_listener":
            state.update(pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_label_identity":
            state.update(loaded=True, pid=9999, cwd="/tmp/unknown", listener=True)
        if case == "unknown_owner":
            state["owner_available"] = False
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def _env(self, checkout: ReleaseCheckout) -> dict[str, str]:
        return {
            **os.environ,
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
        }

    def install(
        self, checkout: ReleaseCheckout | None = None, *, mode: str = "production",
        dry_run: bool = False, expected_sha: str | None = None,
        release_manifest: Path | None = None, check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        checkout = self.candidate if checkout is None else checkout
        release_manifest = (
            checkout.path / "ops" / "prediction-service-release.json"
            if release_manifest is None else release_manifest
        )
        command = [
            str(checkout.path / "scripts" / "install_prediction_service_launchd.sh"),
            "--mode", mode, "--repo-root", str(checkout.path),
            "--runtime-root", str(self.runtime_root), "--python", sys.executable,
            "--config", str(self.root / "prediction.json"),
            "--launch-agents-dir", str(self.agents), "--wait-seconds", "1",
            "--release-manifest", str(release_manifest),
        ]
        if dry_run:
            command.append("--dry-run")
        if expected_sha is not None:
            command.extend(("--expected-sha", expected_sha))
        return subprocess.run(
            command, check=check, capture_output=True, text=True,
            env=self._env(checkout),
        )

    def uninstall(self, *, mode: str = "production") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.candidate.path / "scripts" / "uninstall_prediction_service_launchd.sh"),
                "--mode", mode, "--runtime-root", str(self.runtime_root),
                "--launch-agents-dir", str(self.agents), "--python", sys.executable,
            ],
            capture_output=True, text=True, env=self._env(self.candidate),
        )

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
    result = release_harness.install(mode="production")
    assert result.returncode == 0
    record = release_harness.runtime_record
    assert record is not None
    assert record["state"] == "ready"
    assert record["candidate"] == {
        "checkout": str(release_harness.candidate.path),
        "git_sha": release_harness.sha,
        "source_state": "clean",
        "reader_generation": 1,
        "contract_generation": 1,
    }
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
    release_harness.configure("bind_failure")

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
