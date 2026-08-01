# Dashboard Dual-Runtime Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the final Dashboard acceptance prove that Frontend Gateway `8766` and Legacy Dashboard `8767` are separate, current, clean, healthy processes while keeping all business and browser checks on `8766`.

**Architecture:** Extend the existing `dashboard_acceptance` process and log checks with one small reusable health validator and one reusable runtime-evidence function. Invoke them for the two fixed runtime targets, then run the existing account, quote, API, and browser workflow unchanged through the Gateway. Keep the existing result classifier and `--url`/`--log` compatibility surface.

**Tech Stack:** Python 3.12 stdlib, pytest, GNU Make, macOS `lsof`/`ps`/`launchctl`, existing launchd installers.

## Global Constraints

- Public and browser URL remains `http://127.0.0.1:8766/`; Legacy remains loopback-only on `127.0.0.1:8767`.
- A passing run requires two unique listeners with different PIDs.
- Gateway health schema/module are `open_trader.frontend_gateway.health.v1` / `frontend_gateway`; Legacy health schema/module are `open_trader.legacy_dashboard.health.v1` / `legacy_dashboard`.
- Both runtimes must match the expected worktree, Git SHA, clean source state, process start, and fresh runtime log.
- Direct Legacy verification is limited to `/healthz`; do not add another `/api/dashboard` request or compare volatile quote/timestamp snapshots.
- Existing account, quote, API, desktop, and mobile checks continue through `8766` only.
- `PASS`, `FAIL`, and `BLOCKED` meanings do not change; any runtime mismatch is `FAIL`, and single-process rollback mode cannot pass.
- Do not change Dashboard UI, domain rules, worker behavior, API schemas, or Phase 0.4 runbooks.
- Do not run `make acceptance` during development; run it once as the final gate after all source and documentation commits.
- Commit the dated `CHANGELOG.md` entry before any merge into `main`.

---

## File Map

- `src/open_trader/dashboard_acceptance.py`: validate health/runtime identity, probe both processes, and preserve the existing public functional workflow.
- `tests/test_dashboard_acceptance.py`: focused RED/GREEN coverage for module identity, PID separation, cwd/SHA/source/start/log failures, CLI wiring, and public-only business requests.
- `Makefile`: pass the Legacy URL/log and point the existing Gateway log argument at the Gateway launchd log.
- `CHANGELOG.md`: dated operator-facing note for the stricter acceptance gate.

---

### Task 1: Validate Runtime Health And Target-Specific Logs

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:4327-4387`
- Test: `tests/test_dashboard_acceptance.py:6368-6520`

**Interfaces:**
- Consumes: health JSON already emitted by `frontend_gateway.py` and `dashboard_web.py`; existing `_log_errors` inputs.
- Produces: `_runtime_health_errors(payload: object, *, name: str, expected_schema: str, expected_module: str, pid: int, expected_sha: str, expected_cwd: Path, process_started_at: datetime, expected_upstream_status: str | None = None) -> list[str]` and generalized `_log_errors(..., name: str = "Dashboard", prefix: str = "dashboard_runtime: ") -> list[str]`.

- [ ] **Step 1: Link ignored local runtime prerequisites into the worktree**

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
test -e config/prediction_arbitrage.json || \
  ln -s /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
    config/prediction_arbitrage.json
```

Expected: both paths resolve, and `git status --short` remains clean because they are ignored.

- [ ] **Step 2: Write failing health and Gateway-log tests**

Add these focused tests near the existing `_log_errors` tests:

```python
def _runtime_health(
    tmp_path: Path, *, module: str, schema: str, pid: int,
) -> dict[str, object]:
    return {
        "schema_version": schema,
        "module": module,
        "pid": pid,
        "started_at": "2026-08-01T12:00:01+08:00",
        "cwd": str(tmp_path),
        "git_sha": "accepted-sha",
        "source_state": "clean",
    }


def test_acceptance_accepts_matching_gateway_health(tmp_path: Path) -> None:
    payload = {
        **_runtime_health(
            tmp_path,
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=123,
        ),
        "upstream_status": "ok",
    }

    assert dashboard_acceptance._runtime_health_errors(
        payload,
        name="Frontend Gateway",
        expected_schema="open_trader.frontend_gateway.health.v1",
        expected_module="frontend_gateway",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
        expected_upstream_status="ok",
    ) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "wrong.v1", "schema"),
        ("module", "legacy_dashboard", "模块"),
        ("pid", 999, "PID"),
        ("cwd", "/wrong/worktree", "工作目录"),
        ("git_sha", "old-sha", "Git SHA"),
        ("source_state", "dirty", "源码状态"),
        ("started_at", "2026-08-01T11:59:59+08:00", "启动时间"),
        ("upstream_status", "unavailable", "upstream"),
    ],
)
def test_acceptance_rejects_gateway_health_mismatch(
    tmp_path: Path, field: str, value: object, message: str,
) -> None:
    payload = {
        **_runtime_health(
            tmp_path,
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=123,
        ),
        "upstream_status": "ok",
        field: value,
    }

    errors = dashboard_acceptance._runtime_health_errors(
        payload,
        name="Frontend Gateway",
        expected_schema="open_trader.frontend_gateway.health.v1",
        expected_module="frontend_gateway",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
        expected_upstream_status="ok",
    )

    assert any(message in error for error in errors)


def test_acceptance_reads_gateway_runtime_prefix(tmp_path: Path) -> None:
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    }
    log = tmp_path / "gateway.log"
    log.write_text(
        f"frontend_gateway_runtime: {json.dumps(runtime)}\n",
        encoding="utf-8",
    )

    assert dashboard_acceptance._log_errors(
        log,
        name="Frontend Gateway",
        prefix="frontend_gateway_runtime: ",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
    ) == []
```

- [ ] **Step 3: Run the focused tests to verify RED**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py \
  -k 'runtime_health or gateway_runtime_prefix' -q
```

Expected: FAIL because `_runtime_health_errors` does not exist and `_log_errors` does not accept `name` or `prefix`.

- [ ] **Step 4: Add the minimal health validator**

Add this function immediately before `_log_errors`:

```python
def _runtime_health_errors(
    payload: object,
    *,
    name: str,
    expected_schema: str,
    expected_module: str,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
    expected_upstream_status: str | None = None,
) -> list[str]:
    if not isinstance(payload, Mapping):
        return [f"{name} health 不是对象"]
    errors: list[str] = []
    if payload.get("schema_version") != expected_schema:
        errors.append(f"{name} health schema 不匹配")
    if payload.get("module") != expected_module:
        errors.append(f"{name} health 模块身份不匹配")
    if payload.get("pid") != pid:
        errors.append(f"{name} health PID 不匹配")
    cwd = payload.get("cwd")
    if (
        not isinstance(cwd, str)
        or not cwd.strip()
        or Path(cwd).resolve() != expected_cwd.resolve()
    ):
        errors.append(f"{name} health 工作目录不匹配")
    if payload.get("git_sha") != expected_sha:
        errors.append(f"{name} health Git SHA 不匹配")
    if payload.get("source_state") != "clean":
        errors.append(f"{name} health 源码状态不是 clean")
    if (
        expected_upstream_status is not None
        and payload.get("upstream_status") != expected_upstream_status
    ):
        errors.append(f"{name} health upstream 状态不匹配")
    try:
        started_at = datetime.fromisoformat(str(payload.get("started_at") or ""))
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        if started_at < process_started_at:
            errors.append(f"{name} health 启动时间早于候选进程")
    except (TypeError, ValueError):
        errors.append(f"{name} health 启动时间无效")
    return errors
```

- [ ] **Step 5: Generalize the existing log validator without duplicating it**

Change its signature and fixed prefix:

```python
def _log_errors(
    path: Path,
    *,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
    name: str = "Dashboard",
    prefix: str = "dashboard_runtime: ",
) -> list[str]:
```

Delete the local `prefix = "dashboard_runtime: "` assignment. Replace every fixed
`Dashboard` noun inside `_log_errors` with `{name}` while preserving the existing
checks. The resulting messages must be exactly shaped like these examples:

```python
errors.append(f"日志没有候选 {name} PID：{pid}")
errors.append(f"{name} 日志不是候选进程的新日志文件")
errors.append(f"{name} 日志修改时间早于候选进程")
errors.append(f"日志中的 {name} Git SHA 不匹配")
errors.append(f"日志中的 {name} 工作目录不匹配")
errors.append(f"日志中的 {name} 源码状态不是 clean")
errors.append(f"日志中的 {name} 启动时间早于候选进程")
errors.append(f"日志中的 {name} 启动时间无效")
```

Keep the existing defaults so all current Dashboard log tests remain valid.

- [ ] **Step 6: Run the complete Dashboard acceptance unit file**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: all tests pass, including existing stale-log and timezone tests.

- [ ] **Step 7: Commit the helper behavior**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: validate dashboard runtime identities"
```

---

### Task 2: Require The Dual Runtime In The Acceptance Entry Point

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:1899-1938`
- Modify: `src/open_trader/dashboard_acceptance.py:4545-4698`
- Modify: `Makefile:6-8`
- Modify: `Makefile:35-39`
- Test: `tests/test_dashboard_acceptance.py:343-470`
- Test: `tests/test_dashboard_acceptance.py:6810-6826`

**Interfaces:**
- Consumes: Task 1 `_runtime_health_errors` and generalized `_log_errors`; existing `_listener`, `_process_started_at`, `_source_changes`, and `_fetch_json_path`.
- Produces: `_runtime_evidence(name: str, *, url: str, expected_schema: str, expected_module: str, expected_root: Path, expected_sha: str, expected_upstream_status: str | None = None) -> tuple[int | None, Path, datetime | None, list[str]]`; CLI options `--legacy-url` and `--legacy-log`; result fields `gateway_pid` and `legacy_pid` while preserving `pid` as the Gateway PID.

- [ ] **Step 1: Extend the main-test fixture to model two real runtimes**

In `_run_acceptance_main_with_reports`, create two log files and listeners:

```python
gateway_pid = 123
started_at = datetime.fromisoformat("2026-08-01T12:00:00+08:00")
gateway_log = tmp_path / "gateway.log"
legacy_log = tmp_path / "legacy.log"
gateway_log.write_text(
    "frontend_gateway_runtime: "
    + json.dumps({
        "pid": gateway_pid,
        "git_sha": "accepted-sha",
        "cwd": str(worktree.resolve()),
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    })
    + "\n",
    encoding="utf-8",
)
legacy_log.write_text(
    "dashboard_runtime: "
    + json.dumps({
        "pid": legacy_pid,
        "git_sha": "accepted-sha",
        "cwd": str(worktree.resolve()),
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    })
    + "\n",
    encoding="utf-8",
)
listeners = {
    "http://127.0.0.1:8766": (gateway_pid, worktree.resolve()),
    "http://127.0.0.1:8767": (legacy_pid, worktree.resolve()),
}
health = {
    "http://127.0.0.1:8766": {
        **_runtime_health(
            worktree.resolve(),
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=gateway_pid,
        ),
        "upstream_status": "ok",
    },
    "http://127.0.0.1:8767": _runtime_health(
        worktree.resolve(),
        module="legacy_dashboard",
        schema="open_trader.legacy_dashboard.health.v1",
        pid=legacy_pid,
    ),
}
monkeypatch.setattr(
    dashboard_acceptance, "_listener", lambda url: listeners[url]
)
monkeypatch.setattr(
    dashboard_acceptance,
    "_process_started_at",
    lambda _pid: started_at,
)
def health_payload(url: str, path: str) -> dict[str, object]:
    assert path == "/healthz"
    return health[url]

monkeypatch.setattr(
    dashboard_acceptance,
    "_fetch_json_path",
    health_payload,
)
```

Add `legacy_pid: int = 456` to the fixture's keyword-only parameters before using
the snippets above.

Pass both logs to `main`:

```python
status = dashboard_acceptance.main([
    "--expected-root", str(worktree),
    "--log", str(gateway_log),
    "--legacy-log", str(legacy_log),
])
```

Keep the existing browser-log mutation pointed at `legacy_log`, because browser/API
errors originate in the Legacy process. Update `log_is_directory` and
`log_read_error` setup to target `legacy_log` so the existing error-path tests keep
their meaning.

- [ ] **Step 2: Write failing topology and Makefile tests**

Add these tests:

```python
def test_acceptance_main_reports_distinct_dual_runtime_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch, capsys, tmp_path, [reports, reports]
    )

    assert status == 0
    assert result["pid"] == 123
    assert result["gateway_pid"] == 123
    assert result["legacy_pid"] == 456


def test_acceptance_rejects_missing_legacy_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance,
        "_listener",
        lambda _url: (_ for _ in ()).throw(
            RuntimeError("端口 8767 没有唯一监听进程")
        ),
    )

    pid, cwd, started_at, errors = dashboard_acceptance._runtime_evidence(
        "Legacy Dashboard",
        url="http://127.0.0.1:8767",
        expected_schema="open_trader.legacy_dashboard.health.v1",
        expected_module="legacy_dashboard",
        expected_root=tmp_path,
        expected_sha="accepted-sha",
    )

    assert pid is None
    assert cwd == tmp_path.resolve()
    assert started_at is None
    assert any("Legacy Dashboard" in error and "唯一监听" in error for error in errors)


def test_acceptance_rejects_listener_cwd_and_running_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    monkeypatch.setattr(
        dashboard_acceptance, "_listener", lambda _url: (456, wrong.resolve())
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda _pid: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    monkeypatch.setattr(dashboard_acceptance, "_source_changes", lambda _cwd: [])
    monkeypatch.setattr(
        dashboard_acceptance.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "old-sha\n",
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_fetch_json_path",
        lambda *_args: _runtime_health(
            tmp_path,
            module="legacy_dashboard",
            schema="open_trader.legacy_dashboard.health.v1",
            pid=456,
        ),
    )

    _, _, _, errors = dashboard_acceptance._runtime_evidence(
        "Legacy Dashboard",
        url="http://127.0.0.1:8767",
        expected_schema="open_trader.legacy_dashboard.health.v1",
        expected_module="legacy_dashboard",
        expected_root=tmp_path,
        expected_sha="accepted-sha",
    )

    assert any("工作目录" in error for error in errors)
    assert any("运行 Git SHA" in error for error in errors)


def test_make_acceptance_wires_gateway_and_legacy_runtime_logs() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "logs/frontend_gateway/launchd.out.log" in makefile
    assert "LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767" in makefile
    assert "logs/legacy_dashboard/launchd.out.log" in makefile
    assert '--legacy-url "$(LEGACY_DASHBOARD_URL)"' in makefile
    assert '--legacy-log "$(LEGACY_DASHBOARD_LOG)"' in makefile
```

Use the fixture's `legacy_pid` keyword in its listener/health/log fixtures, and add
this strict-topology test:

```python
def test_acceptance_main_rejects_same_gateway_and_legacy_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        legacy_pid=123,
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert any("不同 PID" in error for error in result["errors"])
```

- [ ] **Step 3: Run the focused tests to verify RED**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py \
  -k 'dual_runtime or missing_legacy or listener_cwd or gateway_and_legacy or same_gateway' -q
```

Expected: FAIL because `_runtime_evidence`, the Legacy CLI options, dual result
fields, Makefile wiring, and PID-separation check do not exist.

- [ ] **Step 4: Implement one reusable runtime-evidence function**

Add this function after `_source_changes`:

```python
def _runtime_evidence(
    name: str,
    *,
    url: str,
    expected_schema: str,
    expected_module: str,
    expected_root: Path,
    expected_sha: str,
    expected_upstream_status: str | None = None,
) -> tuple[int | None, Path, datetime | None, list[str]]:
    expected_cwd = expected_root.resolve()
    try:
        pid, cwd = _listener(url)
        process_started_at = _process_started_at(pid)
        errors: list[str] = []
        if cwd != expected_cwd:
            errors.append(f"{name} 运行目录不匹配：{cwd}")
        running_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        if running_sha != expected_sha:
            errors.append(
                f"{name} 运行 Git SHA 不匹配："
                f"{running_sha[:7]} != {expected_sha[:7]}"
            )
        source_changes = _source_changes(cwd)
        if source_changes:
            errors.append(f"{name} 源码未提交：{'；'.join(source_changes)}")
        health = _fetch_json_path(url, "/healthz")
        errors.extend(_runtime_health_errors(
            health,
            name=name,
            expected_schema=expected_schema,
            expected_module=expected_module,
            pid=pid,
            expected_sha=expected_sha,
            expected_cwd=expected_cwd,
            process_started_at=process_started_at,
            expected_upstream_status=expected_upstream_status,
        ))
        return pid, cwd, process_started_at, errors
    except Exception as exc:
        return (
            None,
            expected_cwd,
            None,
            [f"{name} 运行检查失败：{type(exc).__name__}: {exc}"],
        )
```

- [ ] **Step 5: Add the Legacy CLI contract and Makefile wiring**

Update `build_parser`:

```python
parser.add_argument("--url", default="http://127.0.0.1:8766")
parser.add_argument("--legacy-url", default="http://127.0.0.1:8767")
parser.add_argument("--expected-rows", type=int)
parser.add_argument("--expected-eastmoney-cny", type=Decimal)
parser.add_argument("--expected-root", type=Path, default=Path.cwd())
parser.add_argument("--expected-sha")
parser.add_argument(
    "--log",
    type=Path,
    default=Path("logs/frontend_gateway/launchd.out.log"),
)
parser.add_argument(
    "--legacy-log",
    type=Path,
    default=Path("logs/legacy_dashboard/launchd.out.log"),
)
```

Update Makefile variables and invocation:

```make
DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
```

```make
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--legacy-url "$(LEGACY_DASHBOARD_URL)" \
		--legacy-log "$(LEGACY_DASHBOARD_LOG)" \
		--expected-root "$(CURDIR)"
```

- [ ] **Step 6: Wire both targets into `main` before business checks**

Replace the single `pid`, `cwd`, and `process_started_at` setup with:

```python
gateway_pid: int | None = None
gateway_cwd = args.expected_root.resolve()
gateway_started_at: datetime | None = None
legacy_pid: int | None = None
legacy_cwd = args.expected_root.resolve()
legacy_started_at: datetime | None = None
```

Resolve `expected_sha` from `--expected-sha` or `expected_root/HEAD` before probing
the listeners, then call:

```python
(
    gateway_pid,
    gateway_cwd,
    gateway_started_at,
    gateway_errors,
) = _runtime_evidence(
    "Frontend Gateway",
    url=args.url,
    expected_schema="open_trader.frontend_gateway.health.v1",
    expected_module="frontend_gateway",
    expected_root=args.expected_root,
    expected_sha=expected_sha,
    expected_upstream_status="ok",
)
(
    legacy_pid,
    legacy_cwd,
    legacy_started_at,
    legacy_errors,
) = _runtime_evidence(
    "Legacy Dashboard",
    url=args.legacy_url,
    expected_schema="open_trader.legacy_dashboard.health.v1",
    expected_module="legacy_dashboard",
    expected_root=args.expected_root,
    expected_sha=expected_sha,
)
errors.extend(gateway_errors)
errors.extend(legacy_errors)
if (
    gateway_pid is not None
    and legacy_pid is not None
    and gateway_pid == legacy_pid
):
    errors.append("Frontend Gateway 与 Legacy Dashboard 必须使用不同 PID")
```

Delete the old single-listener cwd/SHA/source/start block. Pass `legacy_cwd` to both
`_effective_reports_dir` calls because the reports path originates from Legacy.
Leave every `_fetch_payload`, `_fetch_quotes_payload`, `_check_simulated_accounts`,
`_check_history_endpoints`, and `_browser_check` call on `args.url`.
In the outer `except`, keep the labeled error append but remove the obsolete
`pid = None` assignment; the target helpers already return optional PID evidence.

After `_browser_check`, validate both logs so errors emitted during the live workflow
are included:

```python
if gateway_pid is not None and gateway_started_at is not None:
    errors.extend(_log_errors(
        args.log,
        name="Frontend Gateway",
        prefix="frontend_gateway_runtime: ",
        pid=gateway_pid,
        expected_sha=expected_sha,
        expected_cwd=gateway_cwd,
        process_started_at=gateway_started_at,
    ))
if legacy_pid is not None and legacy_started_at is not None:
    errors.extend(_log_errors(
        args.legacy_log,
        name="Legacy Dashboard",
        prefix="dashboard_runtime: ",
        pid=legacy_pid,
        expected_sha=expected_sha,
        expected_cwd=legacy_cwd,
        process_started_at=legacy_started_at,
    ))
```

Preserve `pid` for compatibility and expose both identities:

```python
result = {
    "status": status,
    "pid": gateway_pid,
    "gateway_pid": gateway_pid,
    "legacy_pid": legacy_pid,
    "errors": errors,
    "blocker": "；".join(blockers) or None,
}
```

- [ ] **Step 7: Prove business requests still use only the public URL**

In the main fixture, record URLs passed to `_fetch_payload`,
`_fetch_quotes_payload`, and `_browser_check`. Add:

```python
def test_acceptance_business_and_browser_checks_stay_on_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    calls: list[str] = []

    status, _, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        public_calls=calls,
    )

    assert status == 0
    assert calls
    assert set(calls) == {"http://127.0.0.1:8766"}
```

Add `public_calls: list[str] | None = None` to the fixture and append the received
URL inside its payload, quotes, and browser fakes with these replacements:

```python
def record_public(url: str) -> None:
    if public_calls is not None:
        public_calls.append(url)

def fetch_payload(url: str) -> dict[str, object]:
    record_public(url)
    return next(payloads)

def fetch_quotes(url: str) -> dict[str, object]:
    record_public(url)
    return next(quote_payloads)

monkeypatch.setattr(dashboard_acceptance, "_fetch_payload", fetch_payload)
monkeypatch.setattr(dashboard_acceptance, "_fetch_quotes_payload", fetch_quotes)
```

Call `record_public(url)` at the start of the existing `browser_check` fake. Do not
count `_fetch_json_path`; its fake asserts that only `/healthz` is requested and is
intentionally called once for each target.

- [ ] **Step 8: Run focused and complete Dashboard acceptance tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py \
  -k 'runtime or listener or business_and_browser or make_acceptance' -q

PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: both commands pass; the complete file retains all previous acceptance
contracts.

- [ ] **Step 9: Commit the dual-runtime gate**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile
git commit -m "feat: require dashboard dual-runtime acceptance"
```

---

### Task 3: Freeze, Deploy, Accept, And Hand Off The Candidate

**Files:**
- Modify: `CHANGELOG.md`
- Verify: `src/open_trader/dashboard_acceptance.py`
- Verify: `tests/test_dashboard_acceptance.py`
- Verify: `Makefile`

**Interfaces:**
- Consumes: committed Tasks 1-2, shared runtime root `/Users/ray/projects/open_trader`, and the existing launchd installers.
- Produces: committed operator log, focused test evidence, direct dual-runtime evidence, final `make acceptance: PASS`, exact-SHA post-acceptance deployment, and GitHub follow-up for #16/#13.

- [ ] **Step 1: Add and commit the dated operator changelog entry**

Under the existing `2026-08-01` section, add exactly:

```markdown
- Dashboard acceptance 现在分别验证 Frontend Gateway `8766` 与 Legacy Dashboard `8767` 的独立 PID、模块身份、工作目录、Git SHA、源码状态、启动时间和新鲜 runtime 日志；账户、报价、API 与浏览器流程仍只通过稳定的 `8766` 入口执行，单进程 rollback 模式不再满足最终 PASS 条件。
```

Then:

```bash
git add CHANGELOG.md
git commit -m "docs: log dashboard dual-runtime acceptance"
```

- [ ] **Step 2: Review the complete candidate before live mutation**

Use the `code-review` skill with base
`c513840f431d9fe2cd1b1b99b701f768a36bb80f`. Check both repository standards and
every Issue #16 criterion. For each blocking finding, add a focused failing test,
make the smallest fix, rerun the focused file, and commit the fix before continuing.

- [ ] **Step 3: Run focused automated verification**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py \
  tests/test_frontend_gateway.py \
  tests/test_dashboard_launchd_stack.py -q

git diff --check
test -z "$(git status --short)"
```

Expected: all focused tests pass, no whitespace errors, and a clean worktree.

- [ ] **Step 4: Record and deploy the committed candidate to every acceptance-owned process**

```bash
git rev-parse HEAD > /tmp/open_trader_issue16_candidate_sha
CANDIDATE_SHA="$(sed -n '1p' /tmp/open_trader_issue16_candidate_sha)"

scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_account_sync_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
```

Expected: Gateway, Legacy, account-sync, and CN/HK/US controllers all start from
this worktree at `$CANDIDATE_SHA`. If an installer fails, use its existing rollback
behavior, diagnose the real process/listener, and do not run the final gate.

- [ ] **Step 5: Run direct dual-runtime checks before the final gate**

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8767/healthz | .venv/bin/python -m json.tool
tail -n 20 logs/frontend_gateway/launchd.out.log
tail -n 20 logs/legacy_dashboard/launchd.out.log
```

Expected: two different PIDs; both health payloads show `$CANDIDATE_SHA`, this
worktree, `source_state: clean`, and the correct module; Gateway reports
`upstream_status: ok`; both logs start with the matching current runtime record.

- [ ] **Step 6: Run the final acceptance gate once**

```bash
make acceptance
```

Expected: the full pytest suite passes, Prediction and drawdown gates pass, and the
Dashboard JSON is `PASS` with `errors: []`, no blocker, and distinct `gateway_pid`
and `legacy_pid`.

On `FAIL`, diagnose and fix the failure, commit the change, redeploy the new candidate
as in Steps 4-5, then rerun the final gate. On `BLOCKED`, report the environmental
blocker and do not claim review readiness.

- [ ] **Step 7: Redeploy the exact accepted SHA without changing source or data**

```bash
CANDIDATE_SHA="$(sed -n '1p' /tmp/open_trader_issue16_candidate_sha)"
ACCEPTED_SHA="$(git rev-parse HEAD)"
test "$ACCEPTED_SHA" = "$CANDIDATE_SHA"
test -z "$(git status --short)"

scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

This is the required post-acceptance restart of the exact accepted code; do not run
`make acceptance` again when no source or data changed.

- [ ] **Step 8: Verify post-acceptance runtime identity and review URL**

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8767/healthz | .venv/bin/python -m json.tool
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
tail -n 20 logs/frontend_gateway/launchd.out.log
tail -n 20 logs/legacy_dashboard/launchd.out.log
```

For each listener PID, run `lsof -a -p <PID> -d cwd -Fn` and
`ps -p <PID> -o lstart=`. Verify cwd is this clean worktree, Git SHA is
`$ACCEPTED_SHA`, the log mtime/runtime record is newer than the process start, and
the public page returns HTTP `200`. Also verify account-sync and all three trend
controller status JSON files still report this worktree, `$ACCEPTED_SHA`, live PIDs,
and fresh heartbeats.

- [ ] **Step 9: Post issue evidence and stop before integration**

Comment on Issue #16 with focused/full test counts, final acceptance status, accepted
SHA, both labels/PIDs/cwd/SHA/start/log evidence, health module identities, Gateway
upstream status, and public HTTP 200. Comment on parent Issue #13 that Phase 0.3 is
locally complete. Keep both issues open and do not push or merge until the user
chooses the integration action.

---

## Plan Self-Review

- Issue #16 listener, identity, SHA/cwd/source/start, and fresh-log criteria map to Tasks 1-2.
- Public-only API/account/quote/browser behavior and no duplicate `/api/dashboard` map to Task 2 Step 7.
- Missing listener, same PID, wrong cwd/SHA, dirty health, invalid start, and stale log each have a named focused failure test.
- `PASS/FAIL/BLOCKED` remains owned by the existing `classify_result`; no second classifier or shell gate is introduced.
- Only four existing files change; no dependency, service, UI, schema, or Phase 0.4 runbook is added.
- Every behavior task starts RED, turns GREEN, and commits independently.
- CHANGELOG is committed before any merge decision.
- `make acceptance` is the final source gate; post-PASS deployment uses the exact accepted SHA and requires PID/cwd/SHA/log/HTTP proof.
