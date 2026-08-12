from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dashboard_launchd.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_dashboard_launchd.sh"
SINGLE_LABEL = "com.open-trader.dashboard"
GATEWAY_LABEL = "com.open-trader.frontend-gateway"
LEGACY_LABEL = "com.open-trader.legacy-dashboard"
SINGLE_TEMPLATE = ROOT / f"ops/launchd/{SINGLE_LABEL}.plist.template"
GATEWAY_TEMPLATE = ROOT / f"ops/launchd/{GATEWAY_LABEL}.plist.template"
LEGACY_TEMPLATE = ROOT / f"ops/launchd/{LEGACY_LABEL}.plist.template"


def _dry_run_sections(stdout: str) -> dict[str, dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    for section in stdout.split("===== ")[1:]:
        label, xml = section.split(" =====\n", 1)
        sections[label] = plistlib.loads(xml.encode("utf-8"))
    return sections


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *,
    mode: str = "stack",
    prediction_owner: str | None = None,
    **env_overrides: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    repo = tmp_path / "repo"
    (repo / "ops/launchd").mkdir(parents=True)
    (repo / "config").mkdir()
    for template in (SINGLE_TEMPLATE, GATEWAY_TEMPLATE, LEGACY_TEMPLATE):
        shutil.copy2(template, repo / "ops/launchd" / template.name)
    (repo / "config/prediction_arbitrage.json").write_text(
        "{}\n", encoding="utf-8"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls_path = tmp_path / "fake-calls"
    state_dir = tmp_path / "launchd-state"
    state_dir.mkdir()
    for label in (SINGLE_LABEL, GATEWAY_LABEL, LEGACY_LABEL):
        (state_dir / label).touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    launchctl = bin_dir / "launchctl"
    _write_executable(
        launchctl,
        """#!/bin/bash
echo "launchctl $*" >> "$FAKE_CALLS"
label="${2##*/}"
if [[ "$1" == "bootout" ]]; then
  if [[ "$label" != "${FAKE_STUCK_LABEL:-}" ]]; then
    rm -f "$FAKE_LAUNCHD_STATE_DIR/$label"
  fi
  exit 0
fi
if [[ "$1" == "bootstrap" ]]; then
  label="$(basename "$3" .plist)"
  if [[ "$label" == "com.open-trader.frontend-gateway" && "${FAKE_FAIL_GATEWAY_BOOTSTRAP:-0}" == "1" ]]; then
    exit 5
  fi
  : > "$FAKE_LAUNCHD_STATE_DIR/$label"
  exit 0
fi
if [[ "$1" == "print" ]]; then
  if [[ ! -f "$FAKE_LAUNCHD_STATE_DIR/$label" ]]; then
    echo "Could not find service" >&2
    exit 113
  fi
  case "$2" in
    *com.open-trader.dashboard) echo "pid = 4101" ;;
    *com.open-trader.frontend-gateway) echo "pid = 4102" ;;
    *com.open-trader.legacy-dashboard) echo "pid = 4103" ;;
  esac
fi
exit 0
""",
    )
    lsof = bin_dir / "lsof"
    _write_executable(
        lsof,
        """#!/bin/bash
echo "lsof $*" >> "$FAKE_CALLS"
case "$*" in
  *tiTCP:8766*) [[ -n "${FAKE_8766_PID:-4101}" ]] && echo "${FAKE_8766_PID:-4101}" ;;
  *tiTCP:8767*) [[ -n "${FAKE_8767_PID:-}" ]] && echo "$FAKE_8767_PID" ;;
esac
""",
    )
    curl = bin_dir / "curl"
    _write_executable(
        curl,
        """#!/bin/bash
echo "curl $*" >> "$FAKE_CALLS"
url="${@: -1}"
if [[ "${FAKE_FAIL_GATEWAY:-0}" == "1" && "$url" == "http://127.0.0.1:8766/healthz" ]]; then
  exit 22
fi
case "$url" in
  http://127.0.0.1:8767/healthz)
    printf '%s\\n' '{"module":"legacy_dashboard"}' ;;
  http://127.0.0.1:8766/healthz)
    if [[ "${FAKE_SINGLE_HEALTH:-0}" == "1" ]]; then
      printf '%s\\n' '{"module":"legacy_dashboard"}'
    else
      account_status="ok"
      [[ "${FAKE_ACCOUNT_HEALTH_MODE:-production}" == "production" ]] || account_status="unavailable"
      printf '%s\\n' '{"module":"frontend_gateway","upstream_status":"ok","legacy_upstream_status":"'"${FAKE_GATEWAY_LEGACY_STATUS:-ok}"'","account_upstream_status":"'"$account_status"'"}'
    fi ;;
esac
exit 0
""",
    )

    common_args = [
        str(INSTALLER),
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--launch-agents-dir",
        str(agents),
        "--python",
        sys.executable,
        "--wait-seconds",
        "1",
    ]
    single_dry_run = subprocess.run(
        [str(INSTALLER), "--mode", "single", "--dry-run", *common_args[1:]],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    (agents / f"{SINGLE_LABEL}.plist").write_text(
        single_dry_run.stdout, encoding="utf-8"
    )
    if mode == "single":
        stack_dry_run = subprocess.run(
            [str(INSTALLER), "--dry-run", *common_args[1:]],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        for label, payload in _dry_run_sections(stack_dry_run.stdout).items():
            (agents / f"{label}.plist").write_bytes(plistlib.dumps(payload))

    env = {
        **os.environ,
        "FAKE_CALLS": str(calls_path),
        "FAKE_LAUNCHD_STATE_DIR": str(state_dir),
        "LAUNCHCTL_BIN": str(launchctl),
        "LSOF_BIN": str(lsof),
        "CURL_BIN": str(curl),
        **env_overrides,
    }
    owner_args = (
        ["--prediction-owner", prediction_owner]
        if prediction_owner is not None
        else []
    )
    result = subprocess.run(
        [str(INSTALLER), "--mode", mode, *owner_args, *common_args[1:]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = (
        calls_path.read_text(encoding="utf-8").splitlines()
        if calls_path.exists()
        else []
    )
    return result, calls, agents


def test_stack_templates_define_separate_loopback_jobs() -> None:
    gateway = plistlib.loads(GATEWAY_TEMPLATE.read_bytes())
    legacy = plistlib.loads(LEGACY_TEMPLATE.read_bytes())
    gateway_args = gateway["ProgramArguments"]
    legacy_args = legacy["ProgramArguments"]

    assert gateway["Label"] == GATEWAY_LABEL
    assert legacy["Label"] == LEGACY_LABEL
    assert gateway_args[gateway_args.index("-m") : gateway_args.index("-m") + 3] == [
        "-m",
        "open_trader",
        "frontend-gateway",
    ]
    assert gateway_args[gateway_args.index("--port") + 1] == "8766"
    assert gateway_args[gateway_args.index("--upstream-port") + 1] == "8767"
    assert gateway_args[gateway_args.index("--prediction-route-state") + 1] == (
        "OPEN_TRADER_PREDICTION_ROUTE_STATE"
    )
    assert gateway_args[gateway_args.index("--prediction-upstream-host") + 1] == (
        "127.0.0.1"
    )
    assert gateway_args[gateway_args.index("--prediction-upstream-port") + 1] == "8769"
    assert legacy_args[legacy_args.index("-m") : legacy_args.index("-m") + 3] == [
        "-m",
        "open_trader",
        "dashboard",
    ]
    assert legacy_args[legacy_args.index("--port") + 1] == "8767"
    assert legacy_args[legacy_args.index("--public-url") + 1] == (
        "http://127.0.0.1:8766/"
    )
    assert gateway["StandardOutPath"] == (
        "OPEN_TRADER_REPO/logs/frontend_gateway/launchd.out.log"
    )
    assert legacy["StandardOutPath"] == (
        "OPEN_TRADER_REPO/logs/legacy_dashboard/launchd.out.log"
    )


def test_stack_dry_run_prints_two_valid_plists_without_side_effects(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    forbidden = tmp_path / "forbidden-tool"
    forbidden.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    forbidden.chmod(0o755)
    result = subprocess.run(
        [
            str(INSTALLER),
            "--dry-run",
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(runtime),
            "--launch-agents-dir",
            str(agents),
            "--python",
            str(ROOT / ".venv/bin/python"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "LAUNCHCTL_BIN": str(forbidden),
            "LSOF_BIN": str(forbidden),
            "CURL_BIN": str(forbidden),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    sections = _dry_run_sections(result.stdout)
    assert set(sections) == {GATEWAY_LABEL, LEGACY_LABEL}
    assert sections[GATEWAY_LABEL]["WorkingDirectory"] == str(ROOT)
    gateway_args = sections[GATEWAY_LABEL]["ProgramArguments"]
    assert str(runtime / "config/prediction-route.json") in gateway_args
    legacy_args = sections[LEGACY_LABEL]["ProgramArguments"]
    assert str(runtime / "data") in legacy_args
    assert str(runtime / "config/prediction_arbitrage.json") in legacy_args
    assert not list(agents.iterdir())


def test_legacy_only_disables_prediction_owner_without_touching_gateway_or_single(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(
        tmp_path, mode="legacy", prediction_owner="disabled"
    )
    domain = f"gui/{os.getuid()}"
    legacy = plistlib.loads((agents / f"{LEGACY_LABEL}.plist").read_bytes())
    legacy_args = legacy["ProgramArguments"]

    assert result.returncode == 0
    assert legacy_args[legacy_args.index("--prediction-config") + 2 :][:2] == [
        "--prediction-owner",
        "disabled",
    ]
    assert [
        call
        for call in calls
        if any(word in call for word in (" bootout ", " bootstrap ", " kickstart"))
    ] == [
        f"launchctl bootout {domain}/{LEGACY_LABEL}",
        f"launchctl bootstrap {domain} {agents / f'{LEGACY_LABEL}.plist'}",
    ]
    assert not any(
        label in call
        for call in calls
        for label in (GATEWAY_LABEL, SINGLE_LABEL, "com.open-trader.account-")
    )
    assert not any("8766" in call for call in calls)


def test_legacy_prediction_owner_defaults_enabled(tmp_path: Path) -> None:
    result, _, agents = _run_installer(tmp_path, mode="legacy")
    legacy = plistlib.loads((agents / f"{LEGACY_LABEL}.plist").read_bytes())
    legacy_args = legacy["ProgramArguments"]

    assert result.returncode == 0
    assert legacy_args[legacy_args.index("--prediction-owner") + 1] == "enabled"


@pytest.mark.parametrize(
    ("mode", "owner"),
    (("unknown", "disabled"), ("legacy", "unknown")),
)
def test_unknown_mode_or_prediction_owner_fails_without_side_effects(
    tmp_path: Path, mode: str, owner: str
) -> None:
    repo = tmp_path / "repo"
    (repo / "ops/launchd").mkdir(parents=True)
    (repo / "config").mkdir()
    for template in (SINGLE_TEMPLATE, GATEWAY_TEMPLATE, LEGACY_TEMPLATE):
        shutil.copy2(template, repo / "ops/launchd" / template.name)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls = tmp_path / "calls"
    forbidden = tmp_path / "forbidden"
    _write_executable(forbidden, '#!/bin/sh\nprintf x >> "$FAKE_CALLS"\n')

    result = subprocess.run(
        [
            str(INSTALLER),
            "--mode",
            mode,
            "--prediction-owner",
            owner,
            "--repo-root",
            str(repo),
            "--runtime-root",
            str(runtime),
            "--launch-agents-dir",
            str(agents),
            "--python",
            sys.executable,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "FAKE_CALLS": str(calls),
            "LAUNCHCTL_BIN": str(forbidden),
            "LSOF_BIN": str(forbidden),
            "CURL_BIN": str(forbidden),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert not calls.exists()
    assert not list(agents.iterdir())
    assert not list(runtime.iterdir())
    assert not (repo / "logs").exists()


@pytest.mark.parametrize(
    "retired_option", ("--cross-execution-mode", "--cross-execution-mode=auto_submit")
)
def test_retired_installer_mode_option_fails_without_side_effects(
    tmp_path: Path, retired_option: str
) -> None:
    repo = tmp_path / "repo"
    (repo / "ops/launchd").mkdir(parents=True)
    (repo / "config").mkdir()
    for template in (SINGLE_TEMPLATE, GATEWAY_TEMPLATE, LEGACY_TEMPLATE):
        shutil.copy2(template, repo / "ops/launchd" / template.name)
    (repo / "config/prediction_arbitrage.json").write_text(
        "{}\n", encoding="utf-8"
    )
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls = tmp_path / "launchctl-calls"
    launchctl = tmp_path / "launchctl"
    _write_executable(launchctl, '#!/bin/sh\nprintf x >> "$FAKE_CALLS"\n')
    lsof = tmp_path / "lsof"
    _write_executable(lsof, '#!/bin/sh\nprintf x >> "$FAKE_CALLS"\n')
    curl = tmp_path / "curl"
    _write_executable(curl, '#!/bin/sh\nprintf x >> "$FAKE_CALLS"\n')
    common = [
        str(INSTALLER),
        "--mode",
        "single",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--launch-agents-dir",
        str(agents),
        "--python",
        sys.executable,
        "--wait-seconds",
        "1",
    ]
    args = [*common, retired_option]
    if retired_option == "--cross-execution-mode":
        args.append("auto_submit")
    result = subprocess.run(
        args,
        cwd=ROOT,
        env={
            **os.environ,
            "FAKE_CALLS": str(calls),
            "LAUNCHCTL_BIN": str(launchctl),
            "LSOF_BIN": str(lsof),
            "CURL_BIN": str(curl),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "cross-auto mode" in result.stderr
    assert result.stdout == ""
    assert not calls.exists()
    assert not list(agents.iterdir())
    assert not list(runtime.rglob("*"))
    assert not (repo / "logs").exists()


def test_dashboard_plists_have_no_cross_execution_environment_variable() -> None:
    for template in (SINGLE_TEMPLATE, LEGACY_TEMPLATE):
        assert "OPEN_TRADER_CROSS_EXECUTION_MODE" not in template.read_text(
            encoding="utf-8"
        )


def test_stack_cutover_verifies_legacy_before_stopping_single_and_starting_gateway(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(tmp_path)
    domain = f"gui/{os.getuid()}"
    legacy_ready = next(
        i
        for i, call in enumerate(calls)
        if call.endswith("http://127.0.0.1:8767/healthz")
    )
    single_stop = calls.index(f"launchctl bootout {domain}/{SINGLE_LABEL}")
    gateway_start = calls.index(
        f"launchctl bootstrap {domain} "
        f"{agents / f'{GATEWAY_LABEL}.plist'}"
    )
    gateway_ready = next(
        i
        for i, call in enumerate(calls)
        if call.endswith("http://127.0.0.1:8766/healthz")
    )
    assert result.returncode == 0
    assert legacy_ready < single_stop < gateway_start < gateway_ready
    assert not any(" kickstart " in call for call in calls)


def test_stack_does_not_bootstrap_while_bootout_remains_loaded(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(
        tmp_path,
        FAKE_STUCK_LABEL=LEGACY_LABEL,
    )
    domain = f"gui/{os.getuid()}"

    assert result.returncode == 1
    assert f"launchd job is still loaded: {LEGACY_LABEL}" in result.stderr
    assert (
        f"launchctl bootstrap {domain} {agents / f'{LEGACY_LABEL}.plist'}"
        not in calls
    )


@pytest.mark.parametrize(
    ("failure", "env_overrides", "expected_error"),
    [
        (
            "legacy unavailable",
            {"FAKE_GATEWAY_LEGACY_STATUS": "unavailable"},
            "legacy upstream is unavailable",
        ),
        (
            "account shadow",
            {"FAKE_ACCOUNT_HEALTH_MODE": "shadow"},
            "account upstream is unavailable",
        ),
        (
            "account unavailable",
            {"FAKE_ACCOUNT_HEALTH_MODE": "unavailable"},
            "account upstream is unavailable",
        ),
        (
            "gateway bootstrap",
            {"FAKE_FAIL_GATEWAY_BOOTSTRAP": "1"},
            "frontend gateway failed readiness",
        ),
    ],
)
def test_stack_failures_do_not_restore_or_bootstrap_single(
    tmp_path: Path,
    failure: str,
    env_overrides: dict[str, str],
    expected_error: str,
) -> None:
    result, calls, agents = _run_installer(tmp_path, **env_overrides)
    domain = f"gui/{os.getuid()}"

    assert result.returncode == 1
    assert expected_error in result.stderr, failure
    assert (
        f"launchctl bootstrap {domain} {agents / f'{SINGLE_LABEL}.plist'}"
        not in calls
    )


@pytest.mark.parametrize(
    ("env_name", "port", "pid"),
    [("FAKE_8766_PID", 8766, "9999"), ("FAKE_8767_PID", 8767, "9998")],
)
def test_unknown_listener_aborts_before_mutation_or_http_probe(
    tmp_path: Path,
    env_name: str,
    port: int,
    pid: str,
) -> None:
    result, calls, _ = _run_installer(tmp_path, **{env_name: pid})
    assert result.returncode == 1
    assert f"port {port} is occupied by an unknown process (pid {pid})" in result.stderr
    assert not any(
        any(word in call for word in (" bootout ", " bootstrap ", " kickstart"))
        for call in calls
    )
    assert not any(call.startswith("curl ") for call in calls)


def test_single_mode_stops_stack_starts_single_and_keeps_all_plists(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(
        tmp_path,
        mode="single",
        FAKE_8766_PID="4102",
        FAKE_8767_PID="4103",
        FAKE_SINGLE_HEALTH="1",
    )
    domain = f"gui/{os.getuid()}"
    changes = [
        call
        for call in calls
        if any(word in call for word in (" bootout ", " bootstrap ", " kickstart"))
    ]
    single_health = next(
        i
        for i, call in enumerate(calls)
        if call.endswith("http://127.0.0.1:8766/healthz")
    )
    public_ready = next(
        i
        for i, call in enumerate(calls)
        if call.endswith("http://127.0.0.1:8766/")
    )
    assert result.returncode == 0
    assert changes[-4:] == [
        f"launchctl bootout {domain}/{GATEWAY_LABEL}",
        f"launchctl bootout {domain}/{LEGACY_LABEL}",
        f"launchctl bootout {domain}/{SINGLE_LABEL}",
        f"launchctl bootstrap {domain} {agents / f'{SINGLE_LABEL}.plist'}",
    ]
    assert single_health < public_ready
    assert {path.name for path in agents.glob("*.plist")} == {
        f"{SINGLE_LABEL}.plist",
        f"{GATEWAY_LABEL}.plist",
        f"{LEGACY_LABEL}.plist",
    }


def _run_uninstaller(
    tmp_path: Path,
    loaded_labels: set[str],
    *,
    seed: bool = True,
) -> subprocess.CompletedProcess[str]:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    if seed:
        for label in (SINGLE_LABEL, GATEWAY_LABEL, LEGACY_LABEL):
            (agents / f"{label}.plist").write_bytes(
                plistlib.dumps({"Label": label})
            )
    calls = tmp_path / "uninstall-calls"
    launchctl = tmp_path / "uninstall-launchctl"
    loaded_case = "|".join(f"*{label}*" for label in sorted(loaded_labels))
    _write_executable(
        launchctl,
        f"""#!/bin/bash
echo "launchctl $*" >> "{calls}"
if [[ "$1" == "print" ]]; then
  case "$2" in
    {loaded_case}) exit 0 ;;
    *) exit 1 ;;
  esac
fi
exit 0
""",
    )
    return subprocess.run(
        [
            str(UNINSTALLER),
            "--repo-root",
            str(ROOT),
            "--launch-agents-dir",
            str(agents),
        ],
        cwd=ROOT,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl)},
        capture_output=True,
        text=True,
    )


def test_uninstaller_idempotently_removes_all_three_known_jobs(tmp_path: Path) -> None:
    first = _run_uninstaller(tmp_path, loaded_labels=set())
    second = _run_uninstaller(tmp_path, loaded_labels=set(), seed=False)
    assert first.returncode == second.returncode == 0
    assert not list((tmp_path / "LaunchAgents").glob("*.plist"))
    for label in (SINGLE_LABEL, GATEWAY_LABEL, LEGACY_LABEL):
        assert label in first.stdout
        assert label in second.stdout


def test_uninstaller_preserves_plist_when_job_remains_loaded(tmp_path: Path) -> None:
    result = _run_uninstaller(tmp_path, loaded_labels={GATEWAY_LABEL})
    plist = tmp_path / "LaunchAgents" / f"{GATEWAY_LABEL}.plist"
    assert result.returncode == 1
    assert plist.exists()
    assert f"still loaded: {GATEWAY_LABEL}" in result.stderr
