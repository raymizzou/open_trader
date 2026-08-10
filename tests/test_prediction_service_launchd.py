from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_prediction_service_launchd.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_prediction_service_launchd.sh"
TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.prediction-service.plist.template"
LABEL = "com.open-trader.prediction-service"


def test_template_runs_only_the_loopback_shadow_service() -> None:
    payload = plistlib.loads(TEMPLATE.read_bytes())

    assert payload["Label"] == LABEL
    assert payload["WorkingDirectory"] == "OPEN_TRADER_REPO"
    assert payload["EnvironmentVariables"] == {"PYTHONPATH": "OPEN_TRADER_REPO/src"}
    assert payload["ProgramArguments"] == [
        "OPEN_TRADER_PYTHON", "-m", "open_trader", "prediction-service",
        "--mode", "shadow", "--data-dir", "OPEN_TRADER_DATA_DIR",
        "--config", "OPEN_TRADER_PREDICTION_CONFIG", "--host", "127.0.0.1",
        "--port", "8769",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == "OPEN_TRADER_RUNTIME_ROOT/logs/prediction_service/launchd.out.log"
    assert payload["StandardErrorPath"] == "OPEN_TRADER_RUNTIME_ROOT/logs/prediction_service/launchd.err.log"


def test_installer_dry_run_renders_only_explicit_isolated_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "isolated runtime"
    config = tmp_path / "config" / "prediction.json"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

    result = subprocess.run(
        [str(INSTALLER), "--dry-run", "--runtime-root", str(runtime), "--repo-root", str(ROOT),
         "--python", sys.executable, "--config", str(config), "--launch-agents-dir", str(agents)],
        capture_output=True, text=True,
    )

    payload = plistlib.loads(result.stdout.encode())
    assert payload["WorkingDirectory"] == str(ROOT)
    assert payload["ProgramArguments"] == [
        sys.executable, "-m", "open_trader", "prediction-service", "--mode", "shadow",
        "--data-dir", str(runtime / "data"), "--config", str(config), "--host", "127.0.0.1",
        "--port", "8769",
    ]
    assert payload["StandardOutPath"] == str(runtime / "logs/prediction_service/launchd.out.log")
    assert payload["StandardErrorPath"] == str(runtime / "logs/prediction_service/launchd.err.log")
    assert "prediction_arbitrage.sqlite3" not in result.stdout
    assert "frontend-gateway" not in result.stdout
    assert "legacy-dashboard" not in result.stdout


def test_installer_dry_run_canonicalizes_a_new_relative_runtime_root(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

    result = subprocess.run(
        [str(INSTALLER), "--dry-run", "--runtime-root", "shadow-run", "--repo-root", str(ROOT),
         "--python", sys.executable, "--config", str(tmp_path / "prediction.json"),
         "--launch-agents-dir", str(agents)],
        check=True, capture_output=True, text=True,
    )

    payload = plistlib.loads(result.stdout.encode())
    runtime = tmp_path / "shadow-run"
    assert payload["ProgramArguments"][7] == str(runtime / "data")
    assert payload["StandardOutPath"] == str(runtime / "logs/prediction_service/launchd.out.log")


def test_installer_restarts_only_its_label_and_checks_exact_shadow_health(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    calls, state, pending = tmp_path / "calls", tmp_path / "state", tmp_path / "pending"
    launchctl, lsof, curl = tmp_path / "launchctl", tmp_path / "lsof", tmp_path / "curl"
    expected_sha = _git_sha(repo)
    launchctl.write_text(
        "#!/bin/sh\necho \"$*\" >> \"$FAKE_CALLS\"\ncase \"$1\" in\n"
        "bootout) rm -f \"$FAKE_STATE\" ;; bootstrap) : > \"$FAKE_STATE\" ;;\n"
        "print) [ -f \"$FAKE_STATE\" ] && { echo 'pid = 4242'; exit 0; }; echo 'Could not find service' >&2; exit 113 ;;\nesac\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\necho \"$*\" >> \"$FAKE_CALLS\"\ncase \"$*\" in\n"
        "*'-d cwd -Fn'*) printf 'p4242\\nfcwd\\nn%s\\n' \"$FAKE_REPO\" ;;\n"
        "*'-iTCP:8769 -sTCP:LISTEN -Fn'*) printf 'p4242\\nn127.0.0.1:8769\\n' ;;\nesac\n",
        encoding="utf-8",
    )
    health = json.dumps({"schema_version": "open_trader.prediction_service.health.v1", "module": "prediction_service", "status": "running", "mode": "shadow", "production_owner": False, "mutations": "prohibited", "pid": 4242, "cwd": str(repo), "git_sha": expected_sha})
    curl.write_text("#!/bin/sh\necho \"$*\" >> \"$FAKE_CALLS\"\nprintf '%s\\n' \"$FAKE_HEALTH\"\n", encoding="utf-8")
    for command in (launchctl, lsof, curl): command.chmod(0o755)

    result = subprocess.run(
        [str(repo / INSTALLER.relative_to(ROOT)), "--runtime-root", str(runtime), "--repo-root", str(repo),
         "--python", sys.executable, "--config", str(tmp_path / "config.json"),
         "--launch-agents-dir", str(agents), "--wait-seconds", "1"],
        check=True, capture_output=True, text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof), "CURL_BIN": str(curl), "FAKE_CALLS": str(calls), "FAKE_STATE": str(state), "FAKE_REPO": str(repo), "FAKE_HEALTH": health},
    )

    assert f"installed launchd agent: {LABEL}" in result.stdout
    domain = f"gui/{os.getuid()}"
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"bootout {domain}/{LABEL}", f"print {domain}/{LABEL}", f"bootstrap {domain} {agents / f'{LABEL}.plist'}", f"print {domain}/{LABEL}",
        "-a -p 4242 -d cwd -Fn", "-nP -a -p 4242 -iTCP:8769 -sTCP:LISTEN -Fn", "-fsS http://127.0.0.1:8769/healthz",
    ]


def test_installer_timeout_keeps_live_job_without_another_bootout(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    calls, state, pending = tmp_path / "calls", tmp_path / "state", tmp_path / "pending"
    launchctl, lsof, curl = tmp_path / "launchctl", tmp_path / "lsof", tmp_path / "curl"
    launchctl.write_text(
        "#!/bin/sh\necho \"$*\" >> \"$FAKE_CALLS\"\ncase \"$1\" in\n"
        "bootout) : > \"$FAKE_PENDING\" ;; bootstrap) : ;;\n"
        "print) [ -f \"$FAKE_PENDING\" ] && { rm \"$FAKE_PENDING\"; echo 'Could not find service' >&2; exit 113; }; echo 'pid = 4242' ;;\nesac\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        "*'-d cwd -Fn'*) printf 'p4242\\nfcwd\\nn%s\\n' \"$FAKE_REPO\" ;;\n"
        "*'-iTCP:8769 -sTCP:LISTEN -Fn'*) printf 'p4242\\nn127.0.0.1:8769\\n' ;;\nesac\n",
        encoding="utf-8",
    )
    curl.write_text("#!/bin/sh\nprintf '%s\\n' '{}'\n", encoding="utf-8")
    for command in (launchctl, lsof, curl): command.chmod(0o755)

    result = subprocess.run(
        [str(repo / INSTALLER.relative_to(ROOT)), "--runtime-root", str(tmp_path / "runtime"), "--repo-root", str(repo),
         "--python", sys.executable, "--config", str(tmp_path / "config.json"), "--launch-agents-dir", str(agents), "--wait-seconds", "1"],
        capture_output=True, text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof), "CURL_BIN": str(curl), "FAKE_CALLS": str(calls), "FAKE_STATE": str(state), "FAKE_PENDING": str(pending), "FAKE_REPO": str(repo)},
    )

    assert result.returncode == 1
    assert "shadow health not confirmed within 1s; job left running" in result.stderr
    assert "installed launchd agent" not in result.stdout
    assert (agents / f"{LABEL}.plist").exists()
    assert calls.read_text(encoding="utf-8").splitlines().count(f"bootout gui/{os.getuid()}/{LABEL}") == 1


def test_uninstaller_requires_label_and_listener_absence_before_deleting_plist(tmp_path: Path) -> None:
    agents, runtime = tmp_path / "LaunchAgents", tmp_path / "runtime"
    agents.mkdir()
    evidence = runtime / "data/evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("keep", encoding="utf-8")
    plist = agents / f"{LABEL}.plist"
    plist.write_text("keep", encoding="utf-8")
    launchctl, lsof = tmp_path / "launchctl", tmp_path / "lsof"
    launchctl.write_text("#!/bin/sh\n[ \"$1\" = print ] && exit 0\n", encoding="utf-8")
    lsof.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755); lsof.chmod(0o755)

    loaded = subprocess.run([str(UNINSTALLER), "--launch-agents-dir", str(agents)], capture_output=True, text=True, env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof)})
    assert loaded.returncode == 1
    assert plist.exists()
    assert evidence.read_text(encoding="utf-8") == "keep"

    launchctl.write_text("#!/bin/sh\necho 'Could not find service' >&2\nexit 113\n", encoding="utf-8")
    launchctl.chmod(0o755)
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    removed = subprocess.run([str(UNINSTALLER), "--launch-agents-dir", str(agents)], check=True, capture_output=True, text=True, env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof)})
    assert not plist.exists()
    assert evidence.read_text(encoding="utf-8") == "keep"
    assert "removed launchd agent" in removed.stdout
    repeated = subprocess.run([str(UNINSTALLER), "--launch-agents-dir", str(agents)], check=True, capture_output=True, text=True, env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof)})
    assert "launchd agent not installed" in repeated.stdout


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    for path in (INSTALLER, UNINSTALLER): shutil.copy2(path, repo / "scripts" / path.name)
    shutil.copy2(TEMPLATE, repo / "ops/launchd" / TEMPLATE.name)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "test"], check=True)
    return repo


def _git_sha(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
