# Simulated Holdings Tabs and Report History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four account-level views—real holdings, simulated holdings, trend report, and market review—while linking live Futu simulated positions to immutable report versions and keeping every historical report available.

**Architecture:** Merge the already accepted trend-execution closure first, then reuse its Futu adapter and append-only action ledger. Query one configured simulated account lazily through a dedicated Dashboard endpoint only when its account Tab opens; derive report attribution from filled action events and frozen report hashes. Keep report summaries and exact historical artifacts behind read-only on-demand endpoints so Dashboard payload size does not grow with history.

**Tech Stack:** Python 3.12, stdlib `http.server`, Futu OpenAPI, immutable JSON files, vanilla JavaScript/CSS, pytest, Playwright-backed Dashboard acceptance.

## Global Constraints

- Start from local `main` in the isolated `feat/simulated-holdings-tabs-history` worktree.
- Integrate exact accepted dependency SHA `37f5a73e358533779efe97a8e0f9fa05b246d9a9`; do not reimplement its order history or action ledger.
- Trend accounts are exactly Tiger/US, Phillips/HK, and Eastmoney/CN. Keep Futu's option-attention UI unchanged.
- Account Tab order and copy are exact: `真实持仓｜模拟盘持仓｜趋势报告｜对应市场复盘`.
- Real holdings are the default view and their current data/interaction contract must not change.
- Futu simulate-account API is the only current simulated-position authority. Never substitute real holdings, report plans, fixtures, or stale examples.
- Historical reports use payload `execution_date` and immutable report hash, never filename date, as authority.
- Use existing JSON report artifacts and immutable action events. Do not add a database, daily position snapshots, or a second ledger.
- Do not add account cards, pill controls, bordered report buttons, a fifth account Tab, manual linking, backtest controls, or parameter export.
- Use semantic buttons with `role="tab"`, visible focus, and text-tab styling; no horizontal page overflow at 375px.
- Do not run `make acceptance` during intermediate work. Run it only as the final Dashboard gate.

---

### Task 1: Integrate the Accepted Execution-Ledger Dependency

**Files:**
- Merge only; no hand-authored source file in this task.

**Interfaces:**
- Consumes: accepted commit `37f5a73e358533779efe97a8e0f9fa05b246d9a9`.
- Produces: `FutuSimulateOrderExecutionClient.list_orders(start=..., end=...)`, stable action-event paths under `data/trend_review/ledgers/<market>/actions/`, and Dashboard action execution projection.

- [ ] **Step 1: Verify the worktree and dependency ancestry**

Run:

```bash
git branch --show-current
git merge-base --is-ancestor main HEAD
git show -s --format='%H %s' 37f5a73e358533779efe97a8e0f9fa05b246d9a9
```

Expected: branch is `feat/simulated-holdings-tabs-history`; the ancestry command exits 0; the dependency SHA resolves to `fix: read strict trend review v2 projections`.

- [ ] **Step 2: Merge the exact dependency SHA**

Run:

```bash
git merge --no-ff 37f5a73e358533779efe97a8e0f9fa05b246d9a9 -m "merge: integrate trend execution closure"
```

Expected: merge completes without overwriting `docs/superpowers/specs/2026-07-18-simulated-holdings-tabs-history-design.md`.

- [ ] **Step 3: Verify the integrated baseline**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_execution.py tests/test_trend_review.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS with no failed tests.

- [ ] **Step 4: Record the merge evidence**

Run:

```bash
git log -1 --oneline
git status --short
```

Expected: merge commit is the branch tip and the worktree is clean.

### Task 2: Project Live Simulated Positions and Report Attribution

**Files:**
- Create: `src/open_trader/trend_simulate_positions.py`
- Create: `tests/test_trend_simulate_positions.py`

**Interfaces:**
- Consumes: `FutuSimulateOrderExecutionClient.account_snapshot()`, report JSON files, and immutable action events.
- Produces: `TrendSimulatePositionService.load(broker: str) -> dict[str, Any]` with stable keys `available`, `broker`, `market`, `synced_at`, `positions`, and `error`.

- [ ] **Step 1: Write failing route-independent service tests**

Create fakes returning one Futu row and tests with these assertions:

```python
def test_simulated_positions_route_account_and_link_exact_filled_report(tmp_path: Path):
    report = frozen_report(execution_date="2026-07-20", symbol="TRV", version="v1")
    write_report(tmp_path, broker="tiger", artifact="2026-07-17.json", payload=report)
    write_action_event(
        tmp_path,
        market="US",
        symbol="TRV",
        side="buy",
        status="filled",
        report_sha256=report_hash(report),
        strategy_version="v1",
    )
    clients = FakeClientFactory(positions=[{
        "code": "US.TRV", "stock_name": "旅行者保险", "qty": "9",
        "cost_price": "368.98", "nominal_price": "371.20",
        "market_val": "3340.80", "pl_ratio": "0.60",
    }])

    payload = service(tmp_path, clients).load("tiger")

    assert clients.calls == [{"market": "US", "simulate_acc_id": 102}]
    assert payload["positions"][0]["symbol"] == "TRV"
    assert payload["positions"][0]["report"] == {
        "artifact": "2026-07-17.json",
        "execution_date": "2026-07-20",
        "strategy_version": "v1",
        "report_sha256": report_hash(report),
    }


def test_simulated_positions_keep_unlinked_position_visible(tmp_path: Path):
    payload = service(tmp_path, FakeClientFactory(positions=[position("US.OLD")])).load("tiger")
    assert payload["positions"][0]["attribution_status"] == "unlinked"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_fail_closed_on_conflicting_reports(tmp_path: Path):
    write_two_distinct_filled_buy_reports_without_intervening_sell(tmp_path, "US", "TRV")
    payload = service(tmp_path, FakeClientFactory(positions=[position("US.TRV")])).load("tiger")
    assert payload["positions"][0]["attribution_status"] == "conflict"


def test_simulated_positions_return_unavailable_instead_of_fallback(tmp_path: Path):
    payload = service(tmp_path, FailingClientFactory("OpenD unavailable")).load("tiger")
    assert payload == {
        "available": False,
        "broker": "tiger",
        "market": "US",
        "synced_at": "",
        "positions": [],
        "error": "OpenD unavailable",
    }
```

Also parameterize broker routing as `tiger -> US/102`, `phillips -> HK/103`, and `eastmoney -> CN/101`; assert `futu` raises `ValueError("unsupported trend simulate broker: futu")`.

- [ ] **Step 2: Run the service tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_simulate_positions.py -v
```

Expected: FAIL because `trend_simulate_positions` does not exist.

- [ ] **Step 3: Implement the minimal service**

Create these public shapes and keep all helpers private:

```python
TREND_SIMULATE_BROKERS = {
    "tiger": ("US", "USD"),
    "phillips": ("HK", "HKD"),
    "eastmoney": ("CN", "CNY"),
}


class TrendSimulatePositionService:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        account_ids: Mapping[str, int],
        fx_to_hkd: Mapping[str, Decimal],
        data_dir: Path,
        reports_dir: Path,
        client_factory: Callable[..., Any] = FutuSimulateOrderExecutionClient,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.host = host
        self.port = port
        self.account_ids = dict(account_ids)
        self.fx_to_hkd = dict(fx_to_hkd)
        self.data_dir = data_dir
        self.reports_dir = reports_dir
        self.client_factory = client_factory
        self.now = now

    def load(self, broker: str) -> dict[str, Any]:
        if broker not in TREND_SIMULATE_BROKERS:
            raise ValueError(f"unsupported trend simulate broker: {broker}")
        market, currency = TREND_SIMULATE_BROKERS[broker]
        account_id = self.account_ids.get(broker, 0)
        if account_id <= 0:
            return _unavailable(broker, market, "模拟账户未登记")
        client = None
        try:
            client = self.client_factory(
                host=self.host, port=self.port,
                simulate_acc_id=account_id, trd_market=market,
            )
            snapshot = client.account_snapshot()
            positions = _project_positions(
                snapshot, broker=broker, market=market, currency=currency,
                fx_to_hkd=self.fx_to_hkd,
                attributions=_position_attributions(
                    self.data_dir, self.reports_dir, broker=broker, market=market
                ),
            )
            return {
                "available": True,
                "broker": broker,
                "market": market,
                "synced_at": self.now().isoformat(timespec="seconds"),
                "positions": positions,
                "error": "",
            }
        except Exception as exc:
            return _unavailable(broker, market, str(exc))
        finally:
            if client is not None:
                client.close()
```

Normalize only positive-quantity positions. Preserve decimal values as strings. Compute `cost_value`, `market_value_hkd`, `account_weight`, and `portfolio_weight` from the returned simulated account. Inject the existing `DETAIL_FX_TO_HKD` mapping at composition time; `trend_simulate_positions.py` must not import `dashboard.py`. For this account-local view, account weight and portfolio weight are identical.

Replay action events in `(date, recorded_at, path)` order. A positive `partially_filled` or `filled` buy establishes attribution; a `filled` sell clears it. Repeated events with the same report hash are idempotent; two different buy hashes before a clearing sell produce `attribution_status="conflict"`. Resolve the selected hash only against a report whose canonical `_report_hash(payload)` matches and whose payload market/broker identity is valid.

- [ ] **Step 4: Run service tests GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_simulate_positions.py tests/test_kelly_order_execution.py tests/test_trend_review.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the simulated-position projection**

```bash
git add src/open_trader/trend_simulate_positions.py tests/test_trend_simulate_positions.py
git commit -m "feat: project attributed trend simulate positions"
```

### Task 3: Add Lazy Simulated-Position HTTP Loading

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_premarket_cli.py`

**Interfaces:**
- Consumes: `TrendSimulatePositionService.load(broker)` from Task 2 and optional account IDs in `config/daily_premarket.env`.
- Produces: `GET /api/trend-simulate-positions/<broker>` and three new `DashboardConfig` integer fields.

- [ ] **Step 1: Write failing configuration and HTTP tests**

Add defaults to the existing `dashboard_config` test helper, then assert:

```python
def test_dashboard_http_loads_only_requested_simulated_account(tmp_path):
    calls = []
    service = FakeTrendSimulatePositionService(calls=calls)
    server = create_dashboard_server(
        dashboard_config(tmp_path), "127.0.0.1", 0,
        quote_service=FakeQuoteService(quote_result()),
        trend_simulate_position_service=service,
    )
    payload = read_json(server_url(server, "/api/trend-simulate-positions/tiger"))
    assert calls == ["tiger"]
    assert payload["broker"] == "tiger"


def test_dashboard_http_rejects_unknown_simulated_broker(tmp_path):
    status, payload = read_error(server, "/api/trend-simulate-positions/futu")
    assert status == 400
    assert payload["message"] == "unsupported trend simulate broker: futu"


def test_dashboard_cli_reads_three_distinct_simulate_account_ids(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "serve_dashboard", lambda config, **_: captured.setdefault("config", config))
    monkeypatch.setattr(cli, "_load_optional_env_values", lambda _: {
        "OPEN_TRADER_TREND_REVIEW_CN_SIMULATE_ACC_ID": "101",
        "OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID": "102",
        "OPEN_TRADER_TREND_REVIEW_HK_SIMULATE_ACC_ID": "103",
    })
    assert cli.main(["dashboard"]) == 0
    assert captured["config"].trend_review_us_simulate_acc_id == 102
```

Add rejection cases for a non-integer, negative value, and duplicate positive IDs.

- [ ] **Step 2: Run the new tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_premarket_cli.py -k 'simulate_account or simulated_account' -v
```

Expected: FAIL because the config fields, injected service, and route do not exist.

- [ ] **Step 3: Wire config, service, and route**

Extend `DashboardConfig` without breaking existing call sites:

```python
@dataclass(frozen=True)
class DashboardConfig:
    # existing required fields remain unchanged
    trend_review_cn_simulate_acc_id: int = 0
    trend_review_us_simulate_acc_id: int = 0
    trend_review_hk_simulate_acc_id: int = 0
```

Parse the three optional env values in the Dashboard CLI with one private integer parser. Reject duplicate positive IDs before starting the server. In `serve_dashboard`, construct exactly one `TrendSimulatePositionService` with:

```python
account_ids={
    "eastmoney": config.trend_review_cn_simulate_acc_id,
    "tiger": config.trend_review_us_simulate_acc_id,
    "phillips": config.trend_review_hk_simulate_acc_id,
},
fx_to_hkd=DETAIL_FX_TO_HKD,
```

Add an optional `trend_simulate_position_service` argument to `create_dashboard_server`. Route only exact paths matching `/api/trend-simulate-positions/<broker>`; call `service.load(broker)` on that request, not during `/api/dashboard` and not during quote polling.

- [ ] **Step 4: Run HTTP and CLI tests GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_premarket_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit lazy HTTP loading**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py src/open_trader/cli.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_premarket_cli.py
git commit -m "feat: expose live trend simulate positions"
```

### Task 4: Expose Immutable Report History and Exact Artifacts

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: existing `_valid_trend_report_payload`, `_trend_action_executions`, report directories, and payload `execution_date`.
- Produces: `GET /api/trend-reports/<broker>/history` and `GET /api/trend-reports/<broker>/history/<artifact>`.

- [ ] **Step 1: Write failing history projection tests**

Create reports where filename dates intentionally differ from payload dates and assert:

```python
def test_trend_report_history_uses_payload_date_and_keeps_revisions(tmp_path):
    write_report("2026-07-17.json", execution_date="2026-07-20", generated_at="2026-07-18T09:00:00+08:00")
    write_report("2026-07-17-r1.json", execution_date="2026-07-20", generated_at="2026-07-18T09:30:00+08:00")
    write_report("2026-07-16.json", execution_date="2026-07-17", generated_at="2026-07-17T09:00:00+08:00")

    history = _load_trend_report_history(tmp_path, broker="tiger")

    assert [row["execution_date"] for row in history[:2]] == ["2026-07-20", "2026-07-20"]
    assert {row["artifact"] for row in history[:2]} == {"2026-07-17.json", "2026-07-17-r1.json"}


def test_exact_historical_report_includes_its_immutable_execution(tmp_path):
    report = _load_historical_trend_report(..., artifact="2026-07-16.json")
    assert report["report_date"] == "2026-07-17"
    assert report["buy_actions"][0]["execution"]["status"] == "missed"


def test_history_marks_corrupt_artifact_without_hiding_valid_siblings(tmp_path):
    assert _load_trend_report_history(...)[-1] == {
        "available": False,
        "artifact": "broken.json",
        "status_text": "报告不可读取",
    }
```

Add traversal tests for `../secret.json`, absolute paths, unknown broker, wrong report market, and artifact names outside the broker directory.

- [ ] **Step 2: Run history tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -k 'report_history or historical_report' -v
```

Expected: FAIL because the loaders and endpoints do not exist.

- [ ] **Step 3: Implement summary and exact-report loaders**

Add these interfaces to `dashboard.py`:

```python
def load_trend_report_history(
    reports_dir: Path, *, broker: str
) -> list[dict[str, Any]]:
    """Return strict, newest-first summaries for one trend broker."""


def load_historical_trend_report(
    data_dir: Path, reports_dir: Path, *, broker: str, artifact: str
) -> dict[str, Any]:
    """Return the same report projection used by the current-report UI."""
```

Use the existing `TREND_REPORT_SOURCES` mapping to select the directory and identity. Require `Path(artifact).name == artifact` and `.suffix == ".json"`. Refactor the current `_load_broker_trend_report` projection body into one private `_project_broker_trend_report(selected, ...)` function so current and historical reports cannot drift.

Valid history summary keys are exact: `available`, `artifact`, `execution_date`, `data_date`, `generated_at`, `strategy_version`, `revision`, and `execution_counts`. Sort valid rows by `(execution_date, generated_at, revision, artifact)` descending. An invalid JSON row has only `available`, `artifact`, and `status_text`; it remains visible and never prevents valid siblings.

- [ ] **Step 4: Add strict read-only HTTP routes**

In `dashboard_web.py`, match only:

```text
/api/trend-reports/tiger/history
/api/trend-reports/tiger/history/2026-07-16.json
```

Return `400` for unsupported broker or unsafe artifact, `404` for a safe nonexistent artifact, and `200` for valid or explicitly unreadable history summaries. Do not accept POST/PUT/DELETE for these routes.

- [ ] **Step 5: Run history and neighboring tests GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit immutable report history**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: expose immutable trend report history"
```

### Task 5: Render Four Account-Level Views

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: current Dashboard payload plus the two on-demand endpoint families from Tasks 3–4.
- Produces: exact account Tab order, lazy simulated holdings, inline current/history report views, and inline market review.

- [ ] **Step 1: Write failing DOM and behavior tests**

Add browser-runtime tests asserting:

```javascript
const labels = [...document.querySelectorAll('#account-tiger [role="tab"][data-account-view]')]
  .map(node => node.textContent.trim());
if (JSON.stringify(labels) !== JSON.stringify(['真实持仓','模拟盘持仓','趋势报告','美股复盘'])) throw new Error(labels);
if (document.querySelector('#account-tiger [aria-selected="true"]').textContent.trim() !== '真实持仓') throw new Error('default');
```

Click `模拟盘持仓`; assert exactly one fetch to `/api/trend-simulate-positions/tiger`, the same holdings column labels as the real table, and a linked `报告 2026-07-20 · v1`. Repeat with an empty response and assert only `当前无模拟盘持仓`; repeat with unavailable and assert the broker error without real-position fallback.

Click `趋势报告`; assert report content renders inside `#account-tiger`, `.workspace-grid` stays visible, and a low-emphasis `历史报告` control exists inside the report panel. Open history, choose `2026-07-16.json`, and assert the exact artifact fetch and its `错过` execution status. Click `美股复盘`; assert the existing Calmar/Sharpe content is inline. Assert Futu still has its existing option-attention entry and no four-view trend Tab row.

Add keyboard assertions for ArrowLeft/ArrowRight/Home/End and a 375px DOM assertion that `document.documentElement.scrollWidth <= window.innerWidth`.

- [ ] **Step 2: Run UI tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'account_view or simulate_positions or report_history' -v
```

Expected: FAIL because the current account header still renders bordered report buttons and opens a separate workspace.

- [ ] **Step 3: Implement account view state and lazy loading**

Add only these new state containers:

```javascript
accountViews: {tiger:'real', phillips:'real', eastmoney:'real'},
trendSimulatePositions: {},
trendReportHistories: {},
trendHistoricalReports: {},
```

Render the exact Tab set only for `tiger`, `phillips`, and `eastmoney`. Use semantic `<button role="tab">` controls and a matching `role="tabpanel"`. `setAccountView(broker, view)` updates only that broker, clears symbol detail, renders immediately, and invokes one loader only when the selected view lacks data.

Keep Futu on the existing `renderTrendReportEntry("futu")` path. Remove `renderTrendReportEntry` from the three trend-account header grids.

- [ ] **Step 4: Reuse existing holdings/report/review renderers**

Split `renderAccountTable(rows)` into a shared header/cell renderer that accepts `{simulated: true}`. Simulated rows omit the `交易决策` and `做T` buttons but keep the same market, symbol, quantity, cost, price, currency value, HKD value, weights, and P&L columns. Under the symbol name render exactly one of:

```html
<button class="report-attribution-link" data-history-artifact="2026-07-17.json">报告 2026-07-20 · v1</button>
<span class="meta-text">未关联历史报告</span>
<span class="missing-text">报告关联冲突</span>
```

Add an `embedded` boolean to the existing trend report and trend review renderers. Embedded mode omits “返回持仓看板” and renders inside the account `tabpanel`; Futu's existing standalone workspace keeps the old return behavior.

Inside the embedded trend report, add one text-style `历史报告` control. History summaries replace only the report panel; clicking an artifact loads and renders the exact historical report read-only. Back restores current report and preserves account, Tab, and scroll position.

- [ ] **Step 5: Implement the approved A styling**

In CSS, make the account header two rows: identity/meta/total in the first grid row, `.account-view-tabs` spanning the full second row. Style inner Tabs with transparent background, no border/radius, existing font/color tokens, 44px minimum hit area, and a 2px bottom indicator for `[aria-selected="true"]`.

At the existing mobile breakpoint, keep the summary single-column and make only the Tab strip horizontally scrollable; the page itself must not overflow. Do not add shadows, cards, pills, gradients, or new color tokens.

- [ ] **Step 6: Run UI tests GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the four account views**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: add simulated holdings account tab"
```

### Task 6: Enforce Acceptance and Deploy the Exact SHA

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile` only if the existing target cannot pass the new real-data expectations.

**Interfaces:**
- Consumes: real Dashboard API, Futu simulated accounts, immutable report history endpoints, browser DOM, process state, and logs.
- Produces: one final `PASS`, `FAIL`, or `BLOCKED` and an exact-SHA review deployment.

- [ ] **Step 1: Write failing acceptance-contract tests**

Add fake-page/API cases proving acceptance fails when:

- the four Tab labels/order differ;
- real holdings is not the default;
- simulated API code/quantity/cost differs from direct Futu facts;
- a linked position opens a different report hash or version;
- an unlinked position is hidden;
- any historical action referenced by the immutable ledger disappears after a newer report exists;
- Futu unavailable is replaced with fixture/real/report-plan rows;
- any viewport overflows or uses bordered account-view buttons.

Add passing cases for zero simulated positions and for an explicitly unlinked legacy position.

- [ ] **Step 2: Run acceptance tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -k 'simulate or account_view or history' -v
```

Expected: FAIL because the final gate does not yet inspect these contracts.

- [ ] **Step 3: Implement strict final-gate checks**

Query each configured simulated account directly through the existing Futu adapter and compare normalized `(market, symbol, qty, cost_price)` tuples with the Dashboard endpoint. Derive historical report/action expectations from the immutable ledger rather than hard-coding a permanent symbol or date. Validate all four Tab labels/order and browser flows at 1920, 1440, 760, and 375 widths. Open one current report, the historical report list, one exact old artifact, one simulated-position attribution, and the review; assert focus return and no horizontal page overflow.

Return `FAIL` for mismatched/missing data, wrong report hash, stale exact-SHA process, browser overflow, or error logs. Return `BLOCKED` only when the required browser or external service is unavailable and cannot be replaced under the project gate.

- [ ] **Step 4: Run focused and full automated tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_simulate_positions.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the acceptance contract**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile
git commit -m "test: enforce simulated holdings report traceability"
```

- [ ] **Step 6: Run real workflows before the final gate**

From the feature worktree, query CN/HK/US configured simulated accounts directly and then query the new Dashboard endpoints. Confirm exact tuple equality and confirm the history endpoint still contains the 2026-07-17 NDAQ `missed` action after the 2026-07-20 report. Start the candidate Dashboard from the feature worktree with `PYTHONPATH=<worktree>/src`; restart affected stale processes and verify PID, cwd, Git SHA, fresh logs, and HTTP 200.

- [ ] **Step 7: Run the final Dashboard gate once**

Run:

```bash
make acceptance
```

Expected: `PASS`. On `FAIL`, diagnose, fix, rerun focused checks, then rerun the gate. On `BLOCKED`, report the external blocker without substituting mocks or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA**

After `PASS`, record `git rev-parse HEAD`, restart the Dashboard using that exact worktree and SHA without source/data changes, and verify the new PID, cwd, SHA, fresh error-free log, and HTTP 200 for `/`, `/api/dashboard`, `/api/trend-simulate-positions/tiger`, and `/api/trend-reports/tiger/history`. Provide `http://127.0.0.1:8766/` for user review.
