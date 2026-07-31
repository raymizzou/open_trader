# Frontend Gateway and Legacy Dashboard Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep `http://127.0.0.1:8766` stable while inserting a lightweight Frontend Gateway in front of the current Dashboard, which becomes a separately supervised Legacy Dashboard Module on `127.0.0.1:8767`.

**Architecture:** The Frontend Gateway owns only static Dashboard assets, a health interface, and same-origin forwarding of `/api/*`; it contains no trading rules, report assembly, file reads, external adapters, or workers. The current Dashboard server keeps all existing behavior behind an explicit legacy interface. A fenced launchd cutover starts and verifies the legacy module before moving port `8766`, and can restore the previous single-process job if the gateway fails. Acceptance proves both processes, both runtime records, the stable browser URL, and the direct legacy interface.

**Tech Stack:** Python 3 standard library (`http.server`, `http.client`, `urllib.parse`), pytest, zsh launchd scripts, plist templates, existing Dashboard acceptance harness.

## Global Constraints

- Preserve `http://127.0.0.1:8766` as the only browser/operator URL.
- Do not change Dashboard layout, copy, interactions, report schemas, strategy logic, trade execution, notification behavior, or worker cadence.
- Do not move Account, Trend, Research, or Prediction workers in this phase.
- Do not optimize the current large `/api/dashboard` payload or quote polling in this phase.
- The gateway may serve checked-in static files and forward HTTP. It must not import `dashboard_data`, statement importers, backtests, research chat, prediction execution, OpenD adapters, Trend Animals, or Codex adapters.
- Bind both processes to loopback only. The gateway must preserve the current cookie and CSRF contract without weakening origin validation.
- Use only the Python standard library for forwarding; add no reverse-proxy dependency.
- Keep the old `com.open-trader.dashboard` plist installed but unloaded after a successful cutover so `--single-process` can restore it.
- Do not run `make acceptance` during intermediate development. It is the final Dashboard gate after all source, tests, docs, and changelog changes are committed.
- Do not merge this branch as part of implementation. Before a later merge, the dated operator-facing `CHANGELOG.md` entry must already be committed.

## Public Interfaces Introduced in Phase 0

```text
Browser / operator
  http://127.0.0.1:8766/
      GET /, /static/dashboard.css, /static/dashboard.js
      GET /healthz
      GET|POST /api/*
               |
               v
Frontend Gateway (com.open-trader.frontend-gateway)
  loopback :8766
               |
               v
Legacy Dashboard Module (com.open-trader.legacy-dashboard)
  loopback :8767
      GET /healthz
      existing GET|POST /api/* behavior
```

Python interface:

```python
@dataclass(frozen=True)
class FrontendGatewayConfig:
    static_dir: Path
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8767
    public_origin: str = "http://127.0.0.1:8766"
    upstream_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 20 * 1024 * 1024


def create_frontend_gateway(
    *,
    config: FrontendGatewayConfig,
    host: str,
    port: int,
) -> ThreadingHTTPServer: ...


def serve_frontend_gateway(
    *,
    config: FrontendGatewayConfig,
    host: str,
    port: int,
) -> None: ...
```

CLI interfaces:

```text
python -m open_trader frontend-gateway \
  --host 127.0.0.1 --port 8766 \
  --upstream-host 127.0.0.1 --upstream-port 8767 \
  --public-origin http://127.0.0.1:8766

python -m open_trader dashboard \
  --host 127.0.0.1 --port 8767 \
  --public-url http://127.0.0.1:8766/
```

Runtime log prefixes:

```text
frontend_gateway_runtime: {JSON}
dashboard_runtime: {JSON}
```

---

## Task 1: Build the Frontend Gateway as a Deep Module

**Files:**

- Create: `src/open_trader/frontend_gateway.py`
- Create: `tests/test_frontend_gateway.py`
- Reference only: `src/open_trader/dashboard_static/dashboard.html`
- Reference only: `src/open_trader/dashboard_static/dashboard.css`
- Reference only: `src/open_trader/dashboard_static/dashboard.js`

- [ ] **Step 1: Write a fake legacy module fixture and static-asset tests**

Create a local `ThreadingHTTPServer` fixture in `tests/test_frontend_gateway.py`. It must record method, path, headers, and body, return configurable status/body/headers, and set `daemon_threads = True` so test teardown cannot hang.

Write tests proving:

```python
def test_gateway_serves_dashboard_assets_without_contacting_upstream(tmp_path): ...
def test_gateway_rejects_unknown_non_api_path(tmp_path): ...
def test_gateway_health_reports_runtime_and_upstream_status(tmp_path): ...
```

The asset test must request `/`, `/static/dashboard.css`, and `/static/dashboard.js`, compare each response with the configured files, then assert the fake upstream received no request. The unknown-path test must expect HTTP 404.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_frontend_gateway.py -q
```

Expected: collection fails because `open_trader.frontend_gateway` does not exist.

- [ ] **Step 3: Add forwarding-contract tests before implementation**

Add tests for the exact seam:

```python
def test_gateway_forwards_api_get_path_query_status_and_body(gateway, upstream): ...
def test_gateway_forwards_api_post_body_cookie_and_csrf_header(gateway, upstream): ...
def test_gateway_rewrites_only_the_configured_public_origin(gateway, upstream): ...
def test_gateway_does_not_launder_an_untrusted_origin(gateway, upstream): ...
def test_gateway_passes_set_cookie_to_the_browser(gateway, upstream): ...
def test_gateway_strips_hop_by_hop_headers_in_both_directions(gateway, upstream): ...
def test_gateway_rejects_oversized_or_chunked_request_bodies(gateway, upstream): ...
def test_gateway_returns_structured_503_when_upstream_is_unavailable(gateway): ...
```

The origin assertions are security-critical:

```python
assert recorded.headers["Host"] == f"127.0.0.1:{upstream.port}"
assert recorded.headers["Origin"] == f"http://127.0.0.1:{upstream.port}"

# A hostile origin must not become a trusted upstream origin.
assert hostile_response.status == 403
assert upstream.requests == []
```

The gateway must reject an untrusted non-empty `Origin` on POST before forwarding. A request without `Origin` remains compatible with current loopback CLI/test clients and is forwarded unchanged.

- [ ] **Step 4: Implement the minimal gateway module**

Implement `FrontendGatewayConfig`, `create_frontend_gateway`, and `serve_frontend_gateway`. Keep all implementation private except those three names.

Required behavior:

```python
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_STATIC_ROUTES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}
```

- Resolve every static file from `config.static_dir`; never accept a filesystem path from the URL.
- Forward only paths whose parsed path is `/api` or begins with `/api/`.
- Preserve the original path and query string.
- Buffer at most `max_request_body_bytes`; reject `Transfer-Encoding` and invalid/missing POST `Content-Length` with HTTP 400, and oversized bodies with HTTP 413.
- Set upstream `Host` to the legacy listener.
- Rewrite `Origin` and same-origin `Referer` only when they start with `config.public_origin`; reject a different non-empty POST origin with HTTP 403.
- Forward cookies, `X-CSRF-Token`, and ordinary end-to-end headers.
- Strip hop-by-hop headers in both directions and recalculate `Content-Length`.
- Preserve upstream status, response body, content type, and every `Set-Cookie` header.
- On connection failure or timeout, return HTTP 503 with JSON schema `open_trader.frontend_gateway.error.v1`, code `legacy_dashboard_unavailable`, and no stack trace.
- `GET /healthz` returns schema `open_trader.frontend_gateway.health.v1`, module `frontend_gateway`, process metadata, and `upstream_status` (`ok` or `unavailable`). It may probe legacy `/healthz`, but its own HTTP status stays 200 so launchd can distinguish a live gateway from a dead listener.
- `serve_frontend_gateway` prints one flushed `frontend_gateway_runtime: {JSON}` record before `serve_forever()`.

Do not import the Dashboard server to reuse its handler. Duplication of a few HTTP response helpers is preferable to coupling the two modules.

- [ ] **Step 5: Run the focused tests and inspect module dependencies**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_frontend_gateway.py -q

rg -n "dashboard_data|statement|backtest|research|prediction|opend|futu|codex" \
  src/open_trader/frontend_gateway.py
```

Expected: all gateway tests pass. The dependency scan has no matches except wording inside the explicit `legacy_dashboard_unavailable` error code.

- [ ] **Step 6: Commit the deep module**

```bash
git add src/open_trader/frontend_gateway.py tests/test_frontend_gateway.py
git commit -m "feat: add lightweight frontend gateway"
```

---

## Task 2: Add Explicit CLI and Legacy Public-URL Contracts

**Files:**

- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `tests/test_dashboard_cli.py`
- Modify: `tests/test_dashboard_web.py`
- Create: `tests/test_frontend_gateway_cli.py`

- [ ] **Step 1: Write failing parser and dispatch tests**

In `tests/test_frontend_gateway_cli.py`, patch `open_trader.cli.serve_frontend_gateway`, invoke `main()` with the new command, and assert the exact `FrontendGatewayConfig` values and listener arguments.

```python
def test_frontend_gateway_cli_uses_loopback_defaults(monkeypatch): ...
def test_frontend_gateway_cli_dispatches_explicit_upstream_and_origin(monkeypatch): ...
```

In `tests/test_dashboard_cli.py`, extend the existing Dashboard dispatch test:

```python
def test_dashboard_cli_passes_public_url_to_server(monkeypatch):
    # ... invoke dashboard --host 127.0.0.1 --port 8767
    #     --public-url http://127.0.0.1:8766/
    assert captured["public_url"] == "http://127.0.0.1:8766/"
```

- [ ] **Step 2: Write failing Dashboard interface tests**

Add focused tests in `tests/test_dashboard_web.py`:

```python
def test_dashboard_healthz_reports_legacy_module_runtime(running_dashboard): ...
def test_prediction_execution_link_uses_public_url(monkeypatch, dashboard_config): ...
```

The health response must use schema `open_trader.legacy_dashboard.health.v1` and module `legacy_dashboard`. The prediction test must prove generated notification/action links remain `http://127.0.0.1:8766/` even while the listener is `8767`.

- [ ] **Step 3: Run the focused tests and confirm they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
    tests/test_frontend_gateway_cli.py \
    tests/test_dashboard_cli.py \
    tests/test_dashboard_web.py -q
```

Expected: new command, `--public-url`, and `/healthz` assertions fail.

- [ ] **Step 4: Implement parser and dispatch changes**

Add a `frontend-gateway` parser with:

```text
--host                default 127.0.0.1
--port                default 8766
--upstream-host       default 127.0.0.1
--upstream-port       default 8767
--public-origin       default http://127.0.0.1:8766
--upstream-timeout    default 30.0
--static-dir          default package dashboard_static directory
```

Import and call `serve_frontend_gateway` only from CLI dispatch. Construct `FrontendGatewayConfig` there so the gateway module stays independent of `argparse`.

Add `--public-url` to the existing `dashboard` parser. Its empty default means `serve_dashboard` derives `http://{host}:{port}/`, preserving direct single-process behavior. Pass the resolved URL to `PredictionExecutionService(dashboard_url=...)`.

Add `/healthz` to the legacy handler. Return process metadata from the same helper used by the `dashboard_runtime:` startup record; do not initialize a second prediction monitor or data projection for health checks.

- [ ] **Step 5: Run focused tests and direct smoke commands**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
    tests/test_frontend_gateway_cli.py \
    tests/test_dashboard_cli.py \
    tests/test_dashboard_web.py -q

PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader frontend-gateway --help

PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader dashboard --help
```

Expected: tests pass and both new flags appear in help output.

- [ ] **Step 6: Commit CLI and legacy adapter contracts**

```bash
git add src/open_trader/cli.py src/open_trader/dashboard_web.py \
  tests/test_frontend_gateway_cli.py tests/test_dashboard_cli.py \
  tests/test_dashboard_web.py
git commit -m "feat: expose legacy dashboard behind gateway"
```

---

## Task 3: Supervise the Two Modules as One Reversible Stack

**Files:**

- Create: `ops/launchd/com.open-trader.frontend-gateway.plist.template`
- Create: `ops/launchd/com.open-trader.legacy-dashboard.plist.template`
- Preserve: `ops/launchd/com.open-trader.dashboard.plist.template`
- Modify: `scripts/install_dashboard_launchd.sh`
- Modify: `scripts/uninstall_dashboard_launchd.sh`
- Modify: `tests/test_prediction_arbitrage_launchd.py`

- [ ] **Step 1: Replace single-job expectations with stack expectations**

Extend `tests/test_prediction_arbitrage_launchd.py` with constants for all three templates and labels:

```python
FRONTEND_LABEL = "com.open-trader.frontend-gateway"
LEGACY_LABEL = "com.open-trader.legacy-dashboard"
SINGLE_PROCESS_LABEL = "com.open-trader.dashboard"
```

Write tests proving:

```python
def test_frontend_gateway_template_runs_gateway_on_8766(): ...
def test_legacy_dashboard_template_runs_dashboard_on_8767_with_public_url(): ...
def test_installer_dry_run_renders_both_stack_jobs(): ...
def test_single_process_dry_run_preserves_old_job_contract(): ...
def test_installer_verifies_legacy_before_stopping_single_process_job(): ...
def test_installer_restores_single_process_job_when_gateway_readiness_fails(): ...
def test_uninstaller_removes_all_dashboard_job_labels(): ...
```

Use stub executables for `launchctl`, `curl`, `lsof`, and `ps`, following the existing launchd test style. Assert call order from the stub log; do not rely only on script text matching.

- [ ] **Step 2: Run launchd tests and confirm they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_prediction_arbitrage_launchd.py -q
```

Expected: failures for missing templates and missing two-job orchestration.

- [ ] **Step 3: Add the two launchd templates**

The frontend job must run:

```text
caffeinate -s <venv-python> -m open_trader frontend-gateway
  --host 127.0.0.1 --port 8766
  --upstream-host 127.0.0.1 --upstream-port 8767
  --public-origin http://127.0.0.1:8766
```

The legacy job must retain every current Dashboard portfolio/data/report/config option and change only:

```text
--host 127.0.0.1
--port 8767
--public-url http://127.0.0.1:8766/
```

Use separate logs:

```text
logs/dashboard/frontend-gateway.out.log
logs/dashboard/frontend-gateway.err.log
logs/dashboard/legacy-dashboard.out.log
logs/dashboard/legacy-dashboard.err.log
```

Both jobs use `RunAtLoad=true`, `KeepAlive=true`, the same explicit worktree root, virtualenv, `PYTHONSAFEPATH=1`, and `PYTHONPATH=<root>:<root>/src` during Phase 0.

- [ ] **Step 4: Implement the fenced installer**

Keep the existing script path as the one operator entry. Default mode installs the two-job stack; `--single-process` restores the old layout.

Default cutover order must be encoded and tested:

1. Resolve and validate the exact repo root and virtualenv.
2. Render both new plists to temporary files and run `plutil -lint` on both.
3. Refuse any unknown listener on `8767`.
4. Bootstrap `com.open-trader.legacy-dashboard` on `8767` while the old `8766` process is still serving.
5. Require HTTP 200 from direct legacy `/healthz` and one representative `/api/prediction-arbitrage/state` response.
6. Boot out `com.open-trader.dashboard` only after step 5 succeeds.
7. Refuse any remaining unknown listener on `8766`, then bootstrap `com.open-trader.frontend-gateway`.
8. Require HTTP 200 from gateway `/healthz`, `/`, and forwarded `/api/prediction-arbitrage/state`.
9. Leave the old single-process plist file present but unloaded.

If any action after step 6 fails, execute this recovery path before returning nonzero:

```text
bootout frontend-gateway if loaded
bootout legacy-dashboard if loaded
bootstrap the preserved com.open-trader.dashboard plist
require HTTP 200 from http://127.0.0.1:8766/
print a clear rollback result
```

`--single-process` performs that same recovery path deliberately. It is the operator rollback command.

`--dry-run` must not call `launchctl`, `curl`, or modify `~/Library/LaunchAgents`. In default mode it prints two labeled plist documents; with `--single-process` it prints the existing single-process plist.

Do not kill listeners by PID. A listener is acceptable only when `lsof`/`ps` proves it belongs to one of the three known labels and the selected repo root; otherwise stop and report it.

- [ ] **Step 5: Update uninstall semantics**

`scripts/uninstall_dashboard_launchd.sh` must boot out and remove the plist files for all three known labels. Missing jobs remain idempotent success. This command is full removal, not rollback; document `install_dashboard_launchd.sh --single-process` as rollback.

- [ ] **Step 6: Run launchd tests and lint every rendered plist**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_prediction_arbitrage_launchd.py -q

scripts/install_dashboard_launchd.sh --dry-run > /tmp/open-trader-dashboard-stack.plists
rg -n "com.open-trader.(frontend-gateway|legacy-dashboard)" \
  /tmp/open-trader-dashboard-stack.plists

scripts/install_dashboard_launchd.sh --dry-run --single-process \
  > /tmp/open-trader-dashboard-single.plist
plutil -lint /tmp/open-trader-dashboard-single.plist
```

Expected: tests pass, both stack labels are present, and the single-process rollback plist is valid.

- [ ] **Step 7: Commit stack supervision**

```bash
git add ops/launchd/com.open-trader.frontend-gateway.plist.template \
  ops/launchd/com.open-trader.legacy-dashboard.plist.template \
  scripts/install_dashboard_launchd.sh \
  scripts/uninstall_dashboard_launchd.sh \
  tests/test_prediction_arbitrage_launchd.py
git commit -m "ops: supervise dashboard gateway stack"
```

---

## Task 4: Make Acceptance Prove Both Runtime Modules

**Files:**

- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing dual-runtime acceptance tests**

Add tests around the existing listener and runtime-log helpers:

```python
def test_acceptance_requires_distinct_gateway_and_legacy_listeners(monkeypatch): ...
def test_acceptance_rejects_gateway_with_wrong_sha_or_cwd(monkeypatch): ...
def test_acceptance_rejects_legacy_dashboard_with_wrong_sha_or_cwd(monkeypatch): ...
def test_acceptance_requires_fresh_frontend_gateway_runtime_record(tmp_path): ...
def test_acceptance_requires_fresh_legacy_dashboard_runtime_record(tmp_path): ...
def test_acceptance_checks_direct_legacy_health_and_forwarded_api(monkeypatch): ...
```

The tests must prove a healthy gateway cannot hide a dead or stale legacy process, and a healthy legacy process cannot substitute for the `8766` gateway.

- [ ] **Step 2: Run focused acceptance tests and confirm they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: new dual-runtime assertions fail against the single-listener implementation.

- [ ] **Step 3: Generalize runtime proof by role**

Refactor the current Dashboard-specific runtime-log parser into a role-aware helper without weakening any existing checks:

```python
def _runtime_log_errors(
    *,
    log_path: Path,
    prefix: str,
    role: str,
    expected_pid: int,
    expected_root: Path,
    expected_sha: str,
    acceptance_started_at: datetime,
) -> list[str]: ...
```

Call it twice:

```text
role=frontend_gateway prefix="frontend_gateway_runtime: "
role=legacy_dashboard prefix="dashboard_runtime: "
```

For each role, continue to require PID, process start time, cwd, Git SHA, clean source state, and a record fresh enough for the current candidate deployment.

- [ ] **Step 4: Add dual listener and HTTP checks**

Add acceptance arguments:

```text
--legacy-url                 default http://127.0.0.1:8767
--frontend-gateway-log       default logs/dashboard/frontend-gateway.out.log
--legacy-dashboard-log       default logs/dashboard/legacy-dashboard.out.log
--expected-frontend-root     defaults to --expected-root
--expected-frontend-sha      defaults to --expected-sha
--expected-legacy-root       defaults to --expected-root
--expected-legacy-sha        defaults to --expected-sha
```

Resolve the listener PID/cwd for both URLs. Reject the run if the PIDs are equal, either listener is absent, or either process does not match its expected root/SHA.

Require:

- Gateway `/healthz` reports `module=frontend_gateway` and HTTP 200.
- Direct legacy `/healthz` reports `module=legacy_dashboard` and HTTP 200.
- Existing browser/API acceptance remains on the stable `8766` URL.
- A direct legacy representative API request succeeds with the same schema as the forwarded request. Compare schema and required keys, not volatile quote values or timestamps.

Do not duplicate the expensive full `/api/dashboard` request solely for equality. Existing acceptance already validates the forwarded Dashboard payload and live browser state.

- [ ] **Step 5: Wire Makefile variables**

Add:

```make
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
FRONTEND_GATEWAY_LOG ?= logs/dashboard/frontend-gateway.out.log
LEGACY_DASHBOARD_LOG ?= logs/dashboard/legacy-dashboard.out.log
```

Pass them into the acceptance command. Keep `DASHBOARD_URL` at `http://127.0.0.1:8766` so every existing browser flow continues through the gateway.

- [ ] **Step 6: Run focused acceptance tests**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: all acceptance unit tests pass. Do not run `make acceptance` yet.

- [ ] **Step 7: Commit dual-runtime acceptance**

```bash
git add src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_acceptance.py Makefile
git commit -m "test: verify gateway and legacy dashboard runtimes"
```

---

## Task 5: Document, Prove, and Deploy the Phase 0 Cutover

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Verify: all files changed by Tasks 1-4

- [ ] **Step 1: Update the operator runbook**

Update the README Dashboard deployment section to state:

- The stable review URL remains `http://127.0.0.1:8766/`.
- `scripts/install_dashboard_launchd.sh` deploys the two-module stack.
- `scripts/install_dashboard_launchd.sh --single-process` is the rollback command.
- `8766` is the Frontend Gateway and `8767` is an internal Legacy Dashboard interface.
- Operators start one stack; they do not start its internal processes individually.
- Diagnostic commands show both launchd labels, both listener PIDs, and both log files.
- This phase changes process topology only; Account, Trend, Research, and Prediction worker migration happens in later module-specific phases.

Do not document direct `8767` access as a normal user workflow.

- [ ] **Step 2: Add the dated operator-facing changelog entry**

Under `2026-07-31`, record:

- Stable `8766` Frontend Gateway introduced.
- Existing Dashboard moved behind it to internal `8767` as the Legacy Dashboard Module.
- One installer supervises both and supports automatic/manual rollback to the preserved single-process job.
- No Dashboard presentation, strategy, report, execution, or worker behavior changed.

- [ ] **Step 3: Commit docs and changelog before final gate**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: add dashboard gateway cutover runbook"
```

This commit satisfies the changelog-before-merge gate. Do not modify source or runtime data after the final accepted SHA is chosen.

- [ ] **Step 4: Run the focused regression suite**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
    tests/test_frontend_gateway.py \
    tests/test_frontend_gateway_cli.py \
    tests/test_dashboard_cli.py \
    tests/test_dashboard_web.py \
    tests/test_prediction_arbitrage_launchd.py \
    tests/test_dashboard_acceptance.py -q
```

Record the exact pass/fail count. Fix any failure and recommit before continuing.

- [ ] **Step 5: Run the full automated test suite**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest -q
```

Record the exact pass/fail count. If an unrelated pre-existing failure remains, prove it on unchanged local `main`; otherwise fix it before continuing. A known failure does not turn the Dashboard gate into a pass.

- [ ] **Step 6: Deploy the candidate stack for final acceptance**

Capture the candidate SHA and confirm the worktree is clean:

```bash
git status --short
git rev-parse HEAD
scripts/install_dashboard_launchd.sh
```

Before the final gate, directly verify:

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8767/healthz
curl --fail --silent http://127.0.0.1:8766/api/prediction-arbitrage/state
```

Inspect fresh `frontend_gateway_runtime:` and `dashboard_runtime:` records and match their PID/cwd/SHA to the listeners.

- [ ] **Step 7: Run `make acceptance` once as the final Dashboard gate**

Run:

```bash
make acceptance
```

Interpret the result exactly:

- `PASS`: continue to exact-SHA redeployment.
- `FAIL`: diagnose, fix, recommit, redeploy the new candidate, then rerun the final gate.
- `BLOCKED`: report the environmental blocker; do not substitute curl, mocks, fixtures, focused tests, or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA and prove the live review stack**

With no source or data changes after acceptance:

```bash
ACCEPTED_SHA=$(git rev-parse HEAD)
scripts/install_dashboard_launchd.sh
```

Verify and record:

- New frontend gateway PID, cwd, start timestamp, and `ACCEPTED_SHA`.
- New legacy Dashboard PID, cwd, start timestamp, and `ACCEPTED_SHA`.
- Fresh runtime log records for both new PIDs.
- HTTP 200 from `http://127.0.0.1:8766/`.
- HTTP 200 from gateway and direct legacy health interfaces.
- HTTP 200 from one forwarded API route.

Because Phase 0 intentionally changes no visible content or interaction, it does not require an additional UI screenshot handoff. The browser flows inside `make acceptance` remain mandatory.

- [ ] **Step 9: Hand off the stable review URL and rollback command**

Report the exact test counts, acceptance result, accepted SHA, both live PIDs/cwds/SHAs, fresh log evidence, and review URL:

```text
http://127.0.0.1:8766/
```

Also state the rollback command:

```bash
scripts/install_dashboard_launchd.sh --single-process
```

Do not merge to `main` until the user explicitly asks.

---

## Deferred Module Plans

After Phase 0 is accepted, create separate specs and implementation plans in this order:

1. Account Module: controller-owned account/quote interfaces plus internal 60-second account and 5-second quote loops.
2. Trend Module: report/query interface plus CN/HK/US controller ownership.
3. Research Module: research chat/premarket/model interfaces and workers.
4. Prediction Module: WebSocket watcher, Codex validation queue, notifications, execution, and SQLite ownership.
5. Legacy Dashboard retirement: delete each legacy route only after its new owning module passes contract, runtime, and rollback acceptance.

Each later plan may choose a different Git SHA and runtime environment per module while retaining one declarative Open Trader stack and the stable `8766` browser interface.
