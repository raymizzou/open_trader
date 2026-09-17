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
    assert payload["EnvironmentVariables"] == {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": "OPEN_TRADER_REPO/src",
        "OPEN_TRADER_NLEG_PAUSED": "OPEN_TRADER_NLEG_PAUSED_VALUE",
    }
    assert payload["ProgramArguments"] == [
        "OPEN_TRADER_PYTHON", "-m", "open_trader", "prediction-service",
        "--mode", "OPEN_TRADER_PREDICTION_MODE",
        "--data-dir", "OPEN_TRADER_DATA_DIR",
        "--config", "OPEN_TRADER_PREDICTION_CONFIG", "--host", "127.0.0.1",
        "--port", "8769", "--notifier-config", "OPEN_TRADER_NOTIFIER_CONFIG",
        "--release-manifest", "OPEN_TRADER_RELEASE_MANIFEST",
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
        "--port", "8769", "--notifier-config", str(runtime / "config" / "daily_premarket.env"),
        "--release-manifest", str(ROOT / "ops/prediction-service-release.json"),
    ]
    assert payload["StandardOutPath"] == str(runtime / "logs/prediction_service/launchd.out.log")
    assert payload["StandardErrorPath"] == str(runtime / "logs/prediction_service/launchd.err.log")
    assert "prediction_arbitrage.sqlite3" not in result.stdout
    assert "frontend-gateway" not in result.stdout
    assert "legacy-dashboard" not in result.stdout


def test_installer_preserves_explicit_n_leg_pause(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    common = [
        str(INSTALLER), "--dry-run", "--runtime-root", str(runtime), "--repo-root", str(ROOT),
        "--python", sys.executable, "--config", str(tmp_path / "prediction.json"),
        "--launch-agents-dir", str(agents),
    ]

    fresh = subprocess.run(common, check=True, capture_output=True, text=True)
    fresh_payload = plistlib.loads(fresh.stdout.encode())
    assert fresh_payload["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "0"

    paused = subprocess.run(common + ["--n-leg-paused", "1"], check=True, capture_output=True, text=True)
    paused_payload = plistlib.loads(paused.stdout.encode())
    (agents / f"{LABEL}.plist").write_bytes(plistlib.dumps(paused_payload))
    assert paused_payload["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "1"

    preserved = subprocess.run(common, check=True, capture_output=True, text=True)
    preserved_payload = plistlib.loads(preserved.stdout.encode())
    assert preserved_payload["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "1"

    resumed = subprocess.run(common + ["--n-leg-paused", "0"], check=True, capture_output=True, text=True)
    resumed_payload = plistlib.loads(resumed.stdout.encode())
    assert resumed_payload["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "0"

    malformed = dict(resumed_payload)
    malformed["EnvironmentVariables"] = {**malformed["EnvironmentVariables"], "OPEN_TRADER_NLEG_PAUSED": "yes"}
    (agents / f"{LABEL}.plist").write_bytes(plistlib.dumps(malformed))
    rejected = subprocess.run(common, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "OPEN_TRADER_NLEG_PAUSED must be 0 or 1" in rejected.stderr


def test_production_installer_applies_n_leg_pause_on_same_release(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    (repo / "src/open_trader").mkdir(parents=True)
    shutil.copy2(ROOT / "src/open_trader/prediction_release.py", repo / "src/open_trader/prediction_release.py")
    shutil.copy2(ROOT / "ops/prediction-service-release.json", repo / "ops/prediction-service-release.json")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com",
         "commit", "-qm", "release fixtures"],
        check=True,
    )

    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    config = tmp_path / "prediction.json"
    calls = tmp_path / "calls"
    state = tmp_path / "launchd-state.json"
    health_log = tmp_path / "health.log"
    launchctl = tmp_path / "launchctl"
    lsof = tmp_path / "lsof"
    curl = tmp_path / "curl"
    ps = tmp_path / "ps"
    owner_probe = tmp_path / "owner-probe"
    expected_sha = _git_sha(repo)
    manifest = repo / "ops/prediction-service-release.json"
    plist = agents / f"{LABEL}.plist"
    data_dir = runtime / "data"
    stdout_path = runtime / "logs/prediction_service/launchd.out.log"
    stderr_path = runtime / "logs/prediction_service/launchd.err.log"
    lock_path = data_dir / "prediction_arbitrage/runtime.lock"

    production_health = {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "cwd": str(repo),
        "git_sha": expected_sha,
        "release_schema_version": "open_trader.prediction_service.release.v1",
        "reader_generation": 2,
        "contract_generation": 2,
        "started_at": "initial-start",
        "n_leg": {"status": "running", "code": "N_LEG_RUNNING"},
    }
    state.write_text(
        json.dumps({"loaded": True, "pid": 4242, "pause": "0", "started_at": "initial-start"}),
        encoding="utf-8",
    )

    launchctl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, plistlib, sys\n"
        "from pathlib import Path\n"
        f"calls = Path({str(calls)!r})\n"
        f"state_path = Path({str(state)!r})\n"
        f"plist_path = Path({str(plist)!r})\n"
        f"repo = {str(repo)!r}\n"
        f"stdout_path = {str(stdout_path)!r}\n"
        f"stderr_path = {str(stderr_path)!r}\n"
        "args = sys.argv[1:]\n"
        "with calls.open('a', encoding='utf-8') as stream: stream.write('launchctl ' + ' '.join(args) + '\\n')\n"
        "current = json.loads(state_path.read_text(encoding='utf-8'))\n"
        "if args and args[0] == 'print':\n"
        "    if not current.get('loaded'):\n"
        "        print('Could not find service', file=sys.stderr); raise SystemExit(113)\n"
        "    print('path = ' + str(plist_path))\n"
        "    print('working directory = ' + repo)\n"
        "    print('stdout path = ' + stdout_path)\n"
        "    print('stderr path = ' + stderr_path)\n"
        "    print('pid = ' + str(current['pid']))\n"
        "    payload = plistlib.loads(plist_path.read_bytes())\n"
        "    print('arguments = {')\n"
        "    for value in payload['ProgramArguments']: print(f'\\\"{value}\\\"')\n"
        "    print('}')\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'bootout':\n"
        "    current['loaded'] = False\n"
        "    state_path.write_text(json.dumps(current), encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'bootstrap':\n"
        "    payload = plistlib.loads(Path(args[-1]).read_bytes())\n"
        "    env = payload.get('EnvironmentVariables', {})\n"
        "    current['loaded'] = True\n"
        "    current['pid'] = int(current.get('pid', 4241)) + 1\n"
        "    current['pause'] = env.get('OPEN_TRADER_NLEG_PAUSED', '0')\n"
        "    current['started_at'] = 'start-' + str(current['pid'])\n"
        "    state_path.write_text(json.dumps(current), encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state = json.loads(Path({str(state)!r}).read_text(encoding='utf-8'))\n"
        f"lock_path = {str(lock_path)!r}\n"
        "args = sys.argv[1:]\n"
        "loaded = bool(state.get('loaded'))\n"
        "pid = str(state.get('pid'))\n"
        "if '-Fpkfn' in args and lock_path in args:\n"
        "    if loaded: print('p' + pid); print('n' + lock_path); raise SystemExit(0)\n"
        "    raise SystemExit(1)\n"
        "if '-d' in args and 'cwd' in args:\n"
        "    if loaded: print('p' + pid); print('fcwd'); print('n' + " + repr(str(repo)) + "); raise SystemExit(0)\n"
        "    raise SystemExit(1)\n"
        "if '-iTCP:8769' in args:\n"
        "    if loaded: print('p' + pid); print('n127.0.0.1:8769'); raise SystemExit(0)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state_path = Path({str(state)!r})\n"
        f"health_log = Path({str(health_log)!r})\n"
        "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
        "if not state.get('loaded'): raise SystemExit(1)\n"
        f"payload = {json.dumps(production_health)!r}\n"
        "health = json.loads(payload)\n"
        "health['pid'] = state['pid']\n"
        "health['started_at'] = state['started_at']\n"
        "paused = state.get('pause') == '1'\n"
        "health['n_leg'] = {'status': 'paused' if paused else 'running', 'code': 'N_LEG_PAUSED' if paused else 'N_LEG_RUNNING'}\n"
        "with health_log.open('a', encoding='utf-8') as stream: stream.write(str(state.get('pause')) + '\\n')\n"
        "print(json.dumps(health, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    ps.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state = json.loads(Path({str(state)!r}).read_text(encoding='utf-8'))\n"
        "pid = sys.argv[sys.argv.index('-p') + 1]\n"
        "raise SystemExit(0 if state.get('loaded') and str(state.get('pid')) == pid else 1)\n",
        encoding="utf-8",
    )
    owner_probe.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        f"state = json.loads(Path({str(state)!r}).read_text(encoding='utf-8'))\n"
        "raise SystemExit(1 if state.get('loaded') else 0)\n",
        encoding="utf-8",
    )
    for command in (launchctl, lsof, curl, ps, owner_probe):
        command.chmod(0o755)

    candidate = {
        "checkout": str(repo),
        "git_sha": expected_sha,
        "source_state": "clean",
        "reader_generation": 2,
        "contract_generation": 2,
    }
    ready = {
        "pid": 4242,
        "cwd": str(repo),
        "listener": "127.0.0.1:8769",
        "health_schema": production_health["schema_version"],
        "health_module": production_health["module"],
        "health_status": production_health["status"],
        "mode": production_health["mode"],
        "production_owner": production_health["production_owner"],
        "mutations": production_health["mutations"],
        "git_sha": expected_sha,
        "release_schema_version": production_health["release_schema_version"],
        "reader_generation": 2,
        "contract_generation": 2,
        "process_started_at": "initial-start",
        "logs": {"stdout": str(stdout_path), "stderr": str(stderr_path)},
    }
    runtime_record = runtime / "prediction-service-runtime.json"
    runtime_record.parent.mkdir(parents=True)
    runtime_record.write_text(
        json.dumps({
            "schema_version": "open_trader.prediction_service.runtime.v1",
            "state": "ready",
            "candidate": candidate,
            "previous_release": None,
            "transition_started_at": "initial-start",
            "updated_at": "initial-start",
            "failure_reason": "",
            "ready": ready,
        }),
        encoding="utf-8",
    )
    plist_payload = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "open_trader", "prediction-service", "--mode", "production", "--data-dir", str(data_dir), "--config", str(config), "--host", "127.0.0.1", "--port", "8769", "--notifier-config", str(runtime / "config/daily_premarket.env"), "--release-manifest", str(manifest)],
        "EnvironmentVariables": {"OPEN_TRADER_NLEG_PAUSED": "0"},
        "WorkingDirectory": str(repo),
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    plist.write_bytes(plistlib.dumps(plist_payload))

    command = [
        str(repo / INSTALLER.relative_to(ROOT)), "--runtime-root", str(runtime), "--repo-root", str(repo),
        "--python", sys.executable, "--config", str(config), "--launch-agents-dir", str(agents),
        "--release-manifest", str(manifest), "--wait-seconds", "1", "--mode", "production",
    ]
    environment = {
        **os.environ,
        "LAUNCHCTL_BIN": str(launchctl),
        "LSOF_BIN": str(lsof),
        "CURL_BIN": str(curl),
        "PS_BIN": str(ps),
        "OWNER_PROBE_BIN": str(owner_probe),
    }

    changed = subprocess.run(command + ["--n-leg-paused", "1"], capture_output=True, text=True, env=environment)
    assert changed.returncode == 0, changed.stderr
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "1"
    first_calls = calls.read_text(encoding="utf-8")
    assert "launchctl bootout" in first_calls
    assert "launchctl bootstrap" in first_calls
    assert health_log.read_text(encoding="utf-8").splitlines()[-1] == "1"

    call_count = len(first_calls.splitlines())
    preserved = subprocess.run(command, capture_output=True, text=True, env=environment)
    assert preserved.returncode == 0, preserved.stderr
    assert "prediction release already ready" in preserved.stdout
    preserved_calls = calls.read_text(encoding="utf-8").splitlines()[call_count:]
    assert not any(" bootout " in f" {line} " or " bootstrap " in f" {line} " for line in preserved_calls)
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "1"

    resumed = subprocess.run(command + ["--n-leg-paused", "0"], capture_output=True, text=True, env=environment)
    assert resumed.returncode == 0, resumed.stderr
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "0"
    assert health_log.read_text(encoding="utf-8").splitlines()[-1] == "0"

    stale = json.loads(runtime_record.read_text(encoding="utf-8"))
    stale["ready"]["pid"] = 9999
    stale["ready"]["process_started_at"] = "stale-start"
    runtime_record.write_text(json.dumps(stale), encoding="utf-8")
    recovery = subprocess.run(command + ["--n-leg-paused", "1"], capture_output=True, text=True, env=environment)
    assert recovery.returncode == 0, recovery.stderr
    assert "recovered managed prediction release" not in recovery.stdout
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["OPEN_TRADER_NLEG_PAUSED"] == "1"
    assert health_log.read_text(encoding="utf-8").splitlines()[-1] == "1"



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
        f"print {domain}/{LABEL}", "-fsS http://127.0.0.1:8769/healthz",
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


def test_uninstaller_waits_past_five_polls_for_delayed_shutdown(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{LABEL}.plist"
    plist.write_text("keep", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl, lsof, sleep = fake_bin / "launchctl", fake_bin / "lsof", fake_bin / "sleep"
    polls = tmp_path / "polls"
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = print ]; then\n"
        "  n=$(cat \"$FAKE_POLLS\" 2>/dev/null || echo 0); n=$((n + 1)); echo \"$n\" > \"$FAKE_POLLS\"\n"
        "  if [ \"$n\" -le 6 ]; then echo 'pid = 123'; exit 0; fi\n"
        "  echo 'Could not find service' >&2; exit 113\n"
        "fi\nexit 0\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\n"
        "n=$(cat \"$FAKE_POLLS\" 2>/dev/null || echo 0)\n"
        "if [ \"$n\" -le 6 ]; then echo 'p123'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in (launchctl, lsof, sleep):
        command.chmod(0o755)

    result = subprocess.run(
        [str(UNINSTALLER), "--launch-agents-dir", str(agents)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "LAUNCHCTL_BIN": str(launchctl),
            "LSOF_BIN": str(lsof),
            "FAKE_POLLS": str(polls),
        },
    )

    assert result.returncode == 0
    assert int(polls.read_text(encoding="utf-8")) >= 6
    assert not plist.exists()


def test_uninstaller_checks_listener_when_label_absent_on_budget_boundary(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{LABEL}.plist"
    plist.write_text("keep", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl, lsof, sleep = fake_bin / "launchctl", fake_bin / "lsof", fake_bin / "sleep"
    polls, listener_check = tmp_path / "polls", tmp_path / "listener-check"
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = print ]; then\n"
        "  n=$(cat \"$FAKE_POLLS\" 2>/dev/null || echo 0); n=$((n + 1)); echo \"$n\" > \"$FAKE_POLLS\"\n"
        "  if [ \"$n\" -lt 20 ]; then echo 'pid = 123'; exit 0; fi\n"
        "  echo 'Could not find service' >&2; exit 113\n"
        "fi\nexit 0\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\n"
        "echo checked > \"$FAKE_LISTENER_CHECK\"\n"
        "exit 1\n",
        encoding="utf-8",
    )
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in (launchctl, lsof, sleep):
        command.chmod(0o755)

    result = subprocess.run(
        [str(UNINSTALLER), "--launch-agents-dir", str(agents)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "LAUNCHCTL_BIN": str(launchctl),
            "LSOF_BIN": str(lsof),
            "FAKE_POLLS": str(polls),
            "FAKE_LISTENER_CHECK": str(listener_check),
        },
    )

    assert result.returncode == 0
    assert polls.read_text(encoding="utf-8") == "20\n"
    assert listener_check.read_text(encoding="utf-8") == "checked\n"
    assert not plist.exists()


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
