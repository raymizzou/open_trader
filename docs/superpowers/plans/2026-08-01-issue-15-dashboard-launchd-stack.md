# Issue #15 Dashboard launchd Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Dashboard launchd installer safely cut over from the preserved single-process job to a two-process Frontend Gateway and Legacy Dashboard stack, with automatic rollback.

**Architecture:** Keep orchestration in the existing Bash installer. Add one plist template per new job, match `lsof` listener PIDs to fixed launchd labels, validate health with `curl` and stdlib JSON parsing, and recover through the preserved single-process plist. Keep rollback in `--mode single` and full removal in the existing uninstaller.

**Tech Stack:** Bash, macOS launchd/launchctl, plist/plutil, lsof, curl, Python stdlib `json`/`plistlib`, pytest.

## Global Constraints

- Default mode is `stack`; explicit rollback is `--mode single`.
- Public URL remains `http://127.0.0.1:8766/`; Legacy listens only on `127.0.0.1:8767`.
- Fixed labels are `com.open-trader.dashboard`, `com.open-trader.frontend-gateway`, and `com.open-trader.legacy-dashboard`.
- Unknown `8766` or `8767` listeners abort before launchd mutation, HTTP probing, or LaunchAgents writes; never kill a PID directly.
- Stack dry-run lints and prints both new plists without launchctl, lsof, curl, LaunchAgents, or log side effects.
- Automatic rollback and `--mode single` preserve all three plist files; uninstall never starts a service.
- Do not modify Gateway/Dashboard Python behavior, UI, API schema, worker cadence, or prediction execution behavior.
- Use focused tests during implementation. Run `make acceptance` exactly once as the final gate, then deploy the exact accepted SHA.

---

### Task 1: Define the two launchd jobs and dry-run contract

**Files:**
- Create: `ops/launchd/com.open-trader.frontend-gateway.plist.template`
- Create: `ops/launchd/com.open-trader.legacy-dashboard.plist.template`
- Create: `tests/test_dashboard_launchd_stack.py`
- Modify: `scripts/install_dashboard_launchd.sh:4-74`
- Modify: `tests/test_prediction_arbitrage_launchd.py:37-167`

**Interfaces:**
- Consumes: existing installer arguments and the preserved `com.open-trader.dashboard` template.
- Produces: `--mode stack|single`, `render_template TEMPLATE`, and stack dry-run sections delimited by `===== <label> =====`.

- [ ] **Step 1: Write failing template and dry-run tests**

Create `tests/test_dashboard_launchd_stack.py` with:

```python
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_dashboard_launchd.sh"
GATEWAY_LABEL = "com.open-trader.frontend-gateway"
LEGACY_LABEL = "com.open-trader.legacy-dashboard"
GATEWAY_TEMPLATE = ROOT / f"ops/launchd/{GATEWAY_LABEL}.plist.template"
LEGACY_TEMPLATE = ROOT / f"ops/launchd/{LEGACY_LABEL}.plist.template"


def _dry_run_sections(stdout: str) -> dict[str, dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    for section in stdout.split("===== ")[1:]:
        label, xml = section.split(" =====\n", 1)
        sections[label] = plistlib.loads(xml.encode("utf-8"))
    return sections


def test_stack_templates_define_separate_loopback_jobs() -> None:
    gateway = plistlib.loads(GATEWAY_TEMPLATE.read_bytes())
    legacy = plistlib.loads(LEGACY_TEMPLATE.read_bytes())
    gateway_args = gateway["ProgramArguments"]
    legacy_args = legacy["ProgramArguments"]

    assert gateway["Label"] == GATEWAY_LABEL
    assert legacy["Label"] == LEGACY_LABEL
    assert gateway_args[gateway_args.index("-m") : gateway_args.index("-m") + 3] == [
        "-m", "open_trader", "frontend-gateway"
    ]
    assert gateway_args[gateway_args.index("--port") + 1] == "8766"
    assert gateway_args[gateway_args.index("--upstream-port") + 1] == "8767"
    assert legacy_args[legacy_args.index("-m") : legacy_args.index("-m") + 3] == [
        "-m", "open_trader", "dashboard"
    ]
    assert legacy_args[legacy_args.index("--port") + 1] == "8767"
    assert legacy_args[legacy_args.index("--public-url") + 1] == "http://127.0.0.1:8766/"
    assert gateway["StandardOutPath"] == "OPEN_TRADER_REPO/logs/frontend_gateway/launchd.out.log"
    assert legacy["StandardOutPath"] == "OPEN_TRADER_REPO/logs/legacy_dashboard/launchd.out.log"


def test_stack_dry_run_prints_two_valid_plists_without_side_effects(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    forbidden = tmp_path / "forbidden-tool"
    forbidden.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    forbidden.chmod(0o755)
    result = subprocess.run(
        [
            str(INSTALLER), "--dry-run", "--repo-root", str(ROOT),
            "--runtime-root", str(runtime), "--launch-agents-dir", str(agents),
            "--python", str(ROOT / ".venv/bin/python"),
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
    assert str(runtime / "data") in sections[LEGACY_LABEL]["ProgramArguments"]
    assert not list(agents.iterdir())
```

In `tests/test_prediction_arbitrage_launchd.py`, add `"--mode", "single"` to the existing single-plist dry-run and bootstrap-retry commands.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_launchd_stack.py tests/test_prediction_arbitrage_launchd.py -q
```

Expected: FAIL because both new templates are missing and the installer rejects `--mode`.

- [ ] **Step 3: Add the exact plist templates**

Duplicate the existing plist envelope, keeping `caffeinate -s`, `WorkingDirectory=OPEN_TRADER_REPO`, `PYTHONPATH=OPEN_TRADER_REPO/src`, `RunAtLoad`, `KeepAlive`, `ProcessType=Interactive`, and `ThrottleInterval=5`.

Gateway's command and logs are exactly:

```text
OPEN_TRADER_PYTHON -m open_trader frontend-gateway
  --host 127.0.0.1 --port 8766
  --upstream-host 127.0.0.1 --upstream-port 8767
  --public-origin http://127.0.0.1:8766
  --static-dir OPEN_TRADER_REPO/src/open_trader/dashboard_static
OPEN_TRADER_REPO/logs/frontend_gateway/launchd.out.log
OPEN_TRADER_REPO/logs/frontend_gateway/launchd.err.log
```

Legacy's command and logs are exactly:

```text
OPEN_TRADER_PYTHON -m open_trader dashboard
  --host 127.0.0.1 --port 8767
  --portfolio OPEN_TRADER_PORTFOLIO
  --data-dir OPEN_TRADER_DATA_DIR
  --reports-dir OPEN_TRADER_REPORTS_DIR
  --config OPEN_TRADER_DAILY_CONFIG
  --public-url http://127.0.0.1:8766/
  --prediction-config OPEN_TRADER_PREDICTION_CONFIG
OPEN_TRADER_REPO/logs/legacy_dashboard/launchd.out.log
OPEN_TRADER_REPO/logs/legacy_dashboard/launchd.err.log
```

- [ ] **Step 4: Implement mode parsing and shared rendering**

Add:

```bash
MODE="stack"
SINGLE_LABEL="com.open-trader.dashboard"
GATEWAY_LABEL="com.open-trader.frontend-gateway"
LEGACY_LABEL="com.open-trader.legacy-dashboard"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-/bin/launchctl}"
LSOF_BIN="${LSOF_BIN:-/usr/sbin/lsof}"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
PLUTIL_BIN="${PLUTIL_BIN:-/usr/bin/plutil}"
```

Parse `--mode`, require `stack` or `single`, and make `render_template()` accept a template path. Route dry-run before every live helper:

```bash
if [[ "$DRY_RUN" -eq 1 && "$MODE" == "stack" ]]; then
  gateway_rendered="$(render_template "$GATEWAY_TEMPLATE")"
  legacy_rendered="$(render_template "$LEGACY_TEMPLATE")"
  lint_plist "$gateway_rendered"
  lint_plist "$legacy_rendered"
  printf '===== %s =====\n%s\n' "$GATEWAY_LABEL" "$gateway_rendered"
  printf '===== %s =====\n%s\n' "$LEGACY_LABEL" "$legacy_rendered"
  exit 0
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  single_rendered="$(render_template "$SINGLE_TEMPLATE")"
  lint_plist "$single_rendered"
  printf '%s\n' "$single_rendered"
  exit 0
fi
```

Keep the current single-process live path temporarily behind `MODE=single`.

- [ ] **Step 5: Run focused tests and commit**

Run the Step 2 command. Expected: all tests pass.

```bash
git add ops/launchd/com.open-trader.frontend-gateway.plist.template \
  ops/launchd/com.open-trader.legacy-dashboard.plist.template \
  scripts/install_dashboard_launchd.sh tests/test_dashboard_launchd_stack.py \
  tests/test_prediction_arbitrage_launchd.py
git commit -m "feat: render dashboard launchd stack"
```

---

### Task 2: Implement listener ownership, cutover, and automatic rollback

**Files:**
- Modify: `scripts/install_dashboard_launchd.sh`
- Modify: `tests/test_dashboard_launchd_stack.py`

**Interfaces:**
- Consumes: rendered stack plists and the preserved single plist from Task 1.
- Produces: `job_pid`, `ensure_port_owned`, `wait_health`, `wait_http`, `start_agent`, `restore_single`, and `install_stack`.

- [ ] **Step 1: Add stubs and failing call-order tests**

Create executable launchctl/lsof/curl stubs in each test's temporary directory. Every stub appends its tool name and arguments to `$FAKE_CALLS`.

The launchctl stub returns these `print` PIDs and succeeds for mutations:

```text
com.open-trader.dashboard          4101
com.open-trader.frontend-gateway   4102
com.open-trader.legacy-dashboard   4103
```

The lsof stub returns `$FAKE_8766_PID` or `$FAKE_8767_PID`. The curl stub returns:

```json
{"module":"legacy_dashboard"}
```

for `8767/healthz`, and:

```json
{"module":"frontend_gateway","upstream_status":"ok"}
```

for `8766/healthz`; `$FAKE_FAIL_GATEWAY=1` makes only the Gateway health request fail.

Add a `_run_installer(tmp_path, *, mode="stack", **env_overrides)` helper that returns `(result, calls, agents)`. It creates temporary runtime/LaunchAgents directories, writes the three stubs, renders the preserved single plist with `--mode single --dry-run`, then runs the requested live mode with `--wait-seconds 1` and the stub paths in `LAUNCHCTL_BIN`, `LSOF_BIN`, and `CURL_BIN`. When `mode="single"`, it also splits the stack dry-run output and prewrites both new plist files; this proves rollback preserves existing stack configuration rather than testing an empty directory.

Add these tests:

```python
def test_stack_cutover_verifies_legacy_before_stopping_single_and_starting_gateway(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(tmp_path)
    domain = f"gui/{os.getuid()}"
    legacy_ready = next(i for i, call in enumerate(calls) if call.endswith("http://127.0.0.1:8767/healthz"))
    single_stop = calls.index(f"launchctl bootout {domain}/com.open-trader.dashboard")
    gateway_start = calls.index(
        f"launchctl bootstrap {domain} {agents / 'com.open-trader.frontend-gateway.plist'}"
    )
    gateway_ready = next(i for i, call in enumerate(calls) if call.endswith("http://127.0.0.1:8766/healthz"))
    assert result.returncode == 0
    assert legacy_ready < single_stop < gateway_start < gateway_ready


def test_gateway_failure_stops_stack_restores_single_and_verifies_public_url(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(tmp_path, FAKE_FAIL_GATEWAY="1")
    domain = f"gui/{os.getuid()}"
    changes = [call for call in calls if any(word in call for word in (" bootout ", " bootstrap ", " kickstart "))]
    assert result.returncode == 1
    assert changes[-5:] == [
        f"launchctl bootout {domain}/com.open-trader.frontend-gateway",
        f"launchctl bootout {domain}/com.open-trader.legacy-dashboard",
        f"launchctl bootout {domain}/com.open-trader.dashboard",
        f"launchctl bootstrap {domain} {agents / 'com.open-trader.dashboard.plist'}",
        f"launchctl kickstart -k {domain}/com.open-trader.dashboard",
    ]
    assert calls[-1].endswith("http://127.0.0.1:8766/")
    assert "restored single-process dashboard" in result.stderr

@pytest.mark.parametrize(
    ("env_name", "port", "pid"),
    [("FAKE_8766_PID", 8766, "9999"), ("FAKE_8767_PID", 8767, "9998")],
)
def test_unknown_listener_aborts_before_mutation_or_http_probe(
    tmp_path: Path, env_name: str, port: int, pid: str
) -> None:
    result, calls, _ = _run_installer(tmp_path, **{env_name: pid})
    assert result.returncode == 1
    assert f"port {port} is occupied by an unknown process (pid {pid})" in result.stderr
    assert not any(any(word in call for word in (" bootout ", " bootstrap ", " kickstart ")) for call in calls)
    assert not any(call.startswith("curl ") for call in calls)
```

The success test asserts this relative order in `$FAKE_CALLS`:

```text
curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8767/healthz
launchctl bootout gui/$UID/com.open-trader.dashboard
launchctl bootstrap gui/$UID $LAUNCH_AGENTS_DIR/com.open-trader.frontend-gateway.plist
launchctl kickstart -k gui/$UID/com.open-trader.frontend-gateway
curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8766/healthz
curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8766/
```

The failure test asserts the final state-changing calls are:

```text
bootout frontend-gateway
bootout legacy-dashboard
bootout dashboard
bootstrap com.open-trader.dashboard.plist
kickstart dashboard
```

and that the final curl verifies `8766/`. The unknown-listener test asserts no `bootout`, `bootstrap`, `kickstart`, or curl call occurred.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_launchd_stack.py -q
```

Expected: the three live-stack behaviors fail because no cutover state machine exists.

- [ ] **Step 3: Implement PID ownership and readiness helpers**

Use fixed tools and real behavior:

```bash
job_pid() {
  "$LAUNCHCTL_BIN" print "gui/$UID/$1" 2>/dev/null |
    awk '$1 == "pid" && $2 == "=" && $3 ~ /^[0-9]+$/ { print $3; exit }' || true
}

ensure_port_owned() {
  local port="$1" listener label known
  shift
  [[ -x "$LSOF_BIN" ]] || { echo "lsof is unavailable: $LSOF_BIN" >&2; return 1; }
  for listener in $("$LSOF_BIN" -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    known=0
    for label in "$@"; do
      [[ "$listener" == "$(job_pid "$label")" ]] && known=1
    done
    [[ "$known" -eq 1 ]] || {
      echo "port $port is occupied by an unknown process (pid $listener); refusing to modify launchd jobs" >&2
      return 1
    }
  done
}
```

`wait_health URL MODULE UPSTREAM` loops `WAIT_SECONDS`, runs curl with `--fail --silent --show-error --max-time 2`, and pipes the response to `$PYTHON_BIN -c` using stdlib `json`. It requires `payload["module"] == MODULE`; when UPSTREAM is non-empty it also requires `payload["upstream_status"] == UPSTREAM`. `wait_http URL` loops the same way and only requires curl exit 0.

Generalize bootstrap and start:

```bash
bootstrap_agent() {
  local plist="$1" attempt
  for attempt in 1 2 3 4 5; do
    "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$plist" && return 0
    [[ "$attempt" -lt 5 ]] || return 1
    sleep 1
  done
}

bootout_agent() {
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$1" 2>/dev/null || true
}

start_agent() {
  local label="$1" plist="$2"
  bootout_agent "$label"
  bootstrap_agent "$plist"
  "$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$label"
}
```

- [ ] **Step 4: Implement cutover and recovery with explicit branches**

```bash
restore_single() {
  bootout_agent "$GATEWAY_LABEL"
  bootout_agent "$LEGACY_LABEL"
  bootout_agent "$SINGLE_LABEL"
  bootstrap_agent "$SINGLE_PLIST" || return 1
  "$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$SINGLE_LABEL" || return 1
  wait_http "http://127.0.0.1:8766/"
}

fail_stack() {
  local reason="$1"
  if restore_single; then
    echo "$reason; restored single-process dashboard" >&2
  else
    echo "$reason; FAILED TO RESTORE single-process dashboard" >&2
  fi
  return 1
}
```

`install_stack()` must execute exactly:

1. Require and lint `$SINGLE_PLIST`.
2. `ensure_port_owned 8766 "$SINGLE_LABEL" "$GATEWAY_LABEL"`.
3. `ensure_port_owned 8767 "$LEGACY_LABEL"`.
4. Verify current `http://127.0.0.1:8766/` before any write.
5. Create only stack log/runtime directories; write both new plists; truncate only new logs.
6. Start Legacy and require `wait_health "http://127.0.0.1:8767/healthz" "legacy_dashboard" ""`.
7. Bootout single and any old Gateway only after Legacy passes.
8. Start Gateway and require `wait_health "http://127.0.0.1:8766/healthz" "frontend_gateway" "ok"` plus public `/` HTTP 200.
9. On either new-job failure, call `fail_stack` and return 1.

Do not overwrite `$SINGLE_PLIST` in stack mode.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_launchd_stack.py tests/test_prediction_arbitrage_launchd.py -q
```

Expected: all tests pass, including the exact sequence checks.

```bash
git add scripts/install_dashboard_launchd.sh tests/test_dashboard_launchd_stack.py
git commit -m "feat: cut over dashboard launchd stack safely"
```

---

### Task 3: Separate rollback, uninstall, and operator documentation

**Files:**
- Modify: `scripts/install_dashboard_launchd.sh`
- Modify: `scripts/uninstall_dashboard_launchd.sh`
- Modify: `tests/test_dashboard_launchd_stack.py`
- Modify: `tests/test_prediction_arbitrage_launchd.py`
- Modify: `docs/operations/frontend-gateway-deployment-reference.md`
- Modify: `README.md:70-79`
- Modify: `CHANGELOG.md:6-12`

**Interfaces:**
- Consumes: Task 2 helpers and fixed labels.
- Produces: `install_single` and an idempotent three-label uninstaller.

- [ ] **Step 1: Write failing rollback and uninstall tests**

Using the same `_run_installer` helper, add:

```python
def test_single_mode_stops_stack_starts_single_and_keeps_all_plists(
    tmp_path: Path,
) -> None:
    result, calls, agents = _run_installer(
        tmp_path, mode="single", FAKE_8766_PID="4102", FAKE_8767_PID="4103"
    )
    domain = f"gui/{os.getuid()}"
    changes = [call for call in calls if any(word in call for word in (" bootout ", " bootstrap ", " kickstart "))]
    assert result.returncode == 0
    assert changes[-5:] == [
        f"launchctl bootout {domain}/com.open-trader.frontend-gateway",
        f"launchctl bootout {domain}/com.open-trader.legacy-dashboard",
        f"launchctl bootout {domain}/com.open-trader.dashboard",
        f"launchctl bootstrap {domain} {agents / 'com.open-trader.dashboard.plist'}",
        f"launchctl kickstart -k {domain}/com.open-trader.dashboard",
    ]
    assert {path.name for path in agents.glob("*.plist")} == {
        "com.open-trader.dashboard.plist",
        "com.open-trader.frontend-gateway.plist",
        "com.open-trader.legacy-dashboard.plist",
    }
    assert calls[-1].endswith("http://127.0.0.1:8766/")


def test_uninstaller_idempotently_removes_all_three_known_jobs(tmp_path: Path) -> None:
    first = _run_uninstaller(tmp_path, loaded_labels=set())
    second = _run_uninstaller(tmp_path, loaded_labels=set())
    assert first.returncode == second.returncode == 0
    assert not list((tmp_path / "LaunchAgents").glob("*.plist"))
    for label in ("com.open-trader.dashboard", GATEWAY_LABEL, LEGACY_LABEL):
        assert label in first.stdout
        assert label in second.stdout


def test_uninstaller_preserves_plist_when_job_remains_loaded(tmp_path: Path) -> None:
    result = _run_uninstaller(tmp_path, loaded_labels={GATEWAY_LABEL})
    plist = tmp_path / "LaunchAgents" / f"{GATEWAY_LABEL}.plist"
    assert result.returncode == 1
    assert plist.exists()
    assert f"still loaded: {GATEWAY_LABEL}" in result.stderr
```

The single-mode test sets known Gateway/Legacy listeners, runs `--mode single`, and asserts the final state-changing calls are:

```text
bootout frontend-gateway
bootout legacy-dashboard
bootout dashboard
bootstrap com.open-trader.dashboard.plist
kickstart dashboard
```

It also asserts all three plist files remain and public `8766/` was verified. Implement `_run_uninstaller(tmp_path, loaded_labels)` to prewrite three valid plist files, use a launchctl stub whose `print` exits 0 only for labels in `loaded_labels`, and run the real uninstaller with the temporary LaunchAgents directory. The tests require two consecutive unloaded runs to return 0 and a still-loaded Gateway plist to be preserved with exit 1.

Update the old source assertion to require all three Dashboard labels while continuing to forbid `rm -rf` and unrelated premarket labels.

- [ ] **Step 2: Run tests and verify RED**

Run the Task 2 Step 5 pytest command. Expected: rollback/uninstall tests fail because current single mode does not stop the stack and uninstaller only handles the old label.

- [ ] **Step 3: Implement explicit single-process rollback**

```bash
install_single() {
  ensure_port_owned 8766 "$SINGLE_LABEL" "$GATEWAY_LABEL"
  ensure_port_owned 8767 "$LEGACY_LABEL"
  mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_ROOT/logs/dashboard" "$DATA_DIR" "$REPORTS_DIR"
  printf '%s\n' "$single_rendered" > "$SINGLE_PLIST"
  bootout_agent "$GATEWAY_LABEL"
  bootout_agent "$LEGACY_LABEL"
  : > "$SINGLE_OUT_LOG"
  : > "$SINGLE_ERR_LOG"
  start_agent "$SINGLE_LABEL" "$SINGLE_PLIST"
  wait_health "http://127.0.0.1:8766/healthz" "legacy_dashboard" ""
  wait_http "http://127.0.0.1:8766/"
  echo "restored single-process launchd agent: $SINGLE_LABEL"
  echo "review URL: http://127.0.0.1:8766/"
}
```

Render/lint only the selected mode's plists, then call `install_single` or `install_stack`.

- [ ] **Step 4: Make uninstall cover all labels without rollback**

```bash
status=0
for label in \
  com.open-trader.frontend-gateway \
  com.open-trader.legacy-dashboard \
  com.open-trader.dashboard
do
  plist="$LAUNCH_AGENTS_DIR/$label.plist"
  "$LAUNCHCTL_BIN" bootout "gui/$UID/$label" 2>/dev/null || true
  if "$LAUNCHCTL_BIN" print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "launchd job is still loaded: $label; preserving $plist" >&2
    status=1
  elif [[ -f "$plist" ]]; then
    rm "$plist"
    echo "removed launchd agent: $plist"
  else
    echo "launchd agent not installed: $plist"
  fi
done
exit "$status"
```

- [ ] **Step 5: Update operations docs, README, and changelog**

Document these exact commands:

```bash
scripts/install_dashboard_launchd.sh --dry-run
scripts/install_dashboard_launchd.sh
scripts/install_dashboard_launchd.sh --mode single
scripts/uninstall_dashboard_launchd.sh
```

Document both health endpoints, all labels/log paths, unknown-listener refusal, and automatic recovery. Replace README's ambiguous install/uninstall snippet with the default/rollback distinction.

Add this `2026-08-01` CHANGELOG entry before merge:

```markdown
- Dashboard launchd 安装器现在默认把已验证的 Legacy Dashboard `8767` 与轻量 Frontend Gateway `8766` 作为一个双进程 stack 切换；切换失败会自动恢复保留的单进程 job，`--mode single` 可明确回滚，完整卸载会幂等移除三个已知 job。未知端口 listener 会在任何状态修改前阻止安装。
```

- [ ] **Step 6: Run focused tests and direct dry-runs**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_launchd_stack.py \
  tests/test_prediction_arbitrage_launchd.py \
  tests/test_frontend_gateway.py tests/test_frontend_gateway_cli.py -q

scripts/install_dashboard_launchd.sh --dry-run > /tmp/open-trader-stack-dry-run.txt
scripts/install_dashboard_launchd.sh --mode single --dry-run \
  > /tmp/open-trader-single-dry-run.plist
plutil -lint /tmp/open-trader-single-dry-run.plist
```

Expected: all focused tests pass; both dry-runs exit 0; single plist lint reports `OK`. Remove those two exact temporary files after inspection.

- [ ] **Step 7: Commit behavior, docs, and merge log**

```bash
git add scripts/install_dashboard_launchd.sh scripts/uninstall_dashboard_launchd.sh \
  tests/test_dashboard_launchd_stack.py tests/test_prediction_arbitrage_launchd.py \
  docs/operations/frontend-gateway-deployment-reference.md README.md CHANGELOG.md
git commit -m "feat: add dashboard stack rollback controls"
```

---

### Task 4: Review, final verification, acceptance, and exact-SHA cutover

**Files:**
- Verify only. If review exposes a defect, return to RED/GREEN, commit the fix, and restart this task.

**Interfaces:**
- Consumes: committed candidate and runtime-root `/Users/ray/projects/open_trader`.
- Produces: review result, full-suite evidence, `make acceptance: PASS`, and live exact-SHA Gateway/Legacy proof.

- [ ] **Step 1: Review the diff**

Use `code-review` with base `0f85fd36c9ed288933e107639514e8b5162dc8e3`. Check repository standards and every Issue #15 criterion. Fix blocking findings through a failing test and commit them.

- [ ] **Step 2: Run complete tests and Prediction preflight**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q

PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  prediction-arb wallet status --config config/prediction_arbitrage.json

PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  prediction-arb preflight --config config/prediction_arbitrage.json --no-submit
```

Expected: full suite, wallet status, and no-submit preflight pass without secret output.

- [ ] **Step 3: Deploy candidate SHA in single mode before acceptance**

Require clean status and record `ACCEPTED_SHA="$(git rev-parse HEAD)"`. Then:

```bash
scripts/install_dashboard_launchd.sh --mode single \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

If account-sync or CN/HK/US controller status has an older SHA, redeploy them from this worktree using the existing install scripts and shared runtime config. Verify single Dashboard PID, cwd, SHA, fresh `dashboard_runtime`, clean source, and HTTP 200.

- [ ] **Step 4: Run the final Dashboard gate once**

```bash
make acceptance
```

Expected: final `PASS`, Dashboard `errors: []`, no blocker. On `FAIL`, fix and repeat Tasks 4.1-4.4 with the new SHA. On `BLOCKED`, report the blocker and do not cut over.

- [ ] **Step 5: Cut over the exact accepted SHA**

Without changing source or data after PASS:

```bash
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --short)"
scripts/install_dashboard_launchd.sh \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

- [ ] **Step 6: Verify live identity, logs, and HTTP**

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

For both PIDs, verify cwd equals the clean #15 worktree, Git SHA equals `$ACCEPTED_SHA`, logs are fresh and contain the correct runtime record, Gateway `upstream_status` is `ok`, and public HTTP status is `200`.

- [ ] **Step 7: Post GitHub evidence**

Comment on Issue #15 with focused/full test counts, acceptance PASS, exact SHA, both live labels/PIDs/cwd/SHA/log checks, health results, and automatic rollback test evidence. Update parent Issue #13 to mark Phase 0.2 locally complete. Keep issues open until the user authorizes push/integration.

---

## Plan self-review

- Issue #15 criteria map to Tasks 1-3; runtime and acceptance obligations map to Task 4.
- Every behavior change begins with a named failing test and RED command.
- No dependency, wrapper, direct PID kill, or Phase 0.3 acceptance behavior is added.
- CHANGELOG is committed before any merge decision.
- `make acceptance` is the final gate; the post-PASS stack cutover uses the exact accepted SHA.
