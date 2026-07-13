# Dashboard Source Completeness Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the existing HK/US daily premarket jobs synchronously produce every Dashboard decision source, expose TradingAgents as its own tab, and make `make acceptance` fail unless every current advice symbol has all eight real sources available.

**Architecture:** Keep the two existing launchd jobs and the existing artifact formats. Remove the daily runner's explicit skipped generators, add Futu skill-fact generation to the same run, validate generated run artifacts before latest promotion, and reuse the Dashboard payload as the public acceptance contract. Preserve the last valid latest files when a run fails, while recording exact market/symbol/source failures in daily status, reports, logs, and blocker notifications.

**Tech Stack:** Python 3.12, pytest, vanilla JavaScript/CSS, Playwright, launchd, Make.

---

## Global constraints

- The strict symbol scope is exactly the rows in `data/latest/<MARKET>/trading_advice.csv`, not every portfolio holding.
- A source passes only when it belongs to the current advice input/hash and reports `available: true`; fallback text and old latest files do not pass.
- The eight required sources per in-scope symbol are:
  - `tradingagents_summary`
  - `technical_facts`
  - `decision_facts.kline`
  - `decision_facts.news_sentiment`
  - `futu_skill_facts.news_sentiment`
  - `futu_skill_facts.technical_anomaly`
  - `futu_skill_facts.capital_anomaly`
  - `futu_skill_facts.derivatives_anomaly`
- Missing sources stay visible in the UI as red failed tabs/panels with the real error.
- Do not overwrite a valid old latest fact file with an incomplete new run artifact.
- `make acceptance` is the final and only completion gate. `FAIL` must be fixed and rerun; `BLOCKED` must be reported as blocked. Only `PASS` permits asking the user to review.

### Task 1: Replace skipped daily generators with real synchronous generation

**Files:**
- Modify: `src/open_trader/daily_premarket.py:368-720`
- Modify: `src/open_trader/advice/premarket.py:51-270`
- Test: `tests/test_daily_premarket.py:390-430`
- Test: `tests/test_daily_premarket.py:1248-1290`
- Test: `tests/test_premarket_pipeline.py`

**Step 1: Write the failing tests**

Replace the test that expects skipped facts with one that proves the daily runner leaves the premarket generators unset, allowing `run_premarket` to use its real defaults:

```python
def test_daily_runner_uses_real_premarket_fact_generators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("portfolio\n", encoding="utf-8")
    premarket = FakePremarket(market="US", symbol="MSFT")

    _daily_runner(
        config=config,
        premarket_runner=premarket,
        plan_builder=FakePlanBuilder(market="US", symbol="MSFT"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("390"))}, **kwargs
        ),
        trade_action_generator=FakeTradeActionGenerator(market="US", symbol="MSFT"),
    ).run(run_date="2026-06-19", market="US")

    call = premarket.calls[0]
    assert "technical_facts_generator" not in call
    assert "decision_facts_generator" not in call
```

Update the defaults test:

```python
assert runner.summary_generator is daily_premarket.generate_tradingagents_summary
```

Add a premarket result assertion so the real technical artifact is returned explicitly instead of being inferred by filename:

```python
assert result.technical_facts_path == (
    tmp_path / "data/runs/2026-06-19/US/technical_facts.json"
)
```

**Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_daily_premarket.py -k 'real_premarket_fact_generators or defaults_to_generate or technical_facts_path'
```

Expected: failures showing skipped callbacks are still passed, the summary default is skipped, or `PremarketResult` has no `technical_facts_path`.

**Step 3: Implement the minimal production changes**

In `PremarketResult`, add the missing field:

```python
technical_facts_path: Path | None = None
decision_facts_path: Path | None = None
```

Return both real result paths from `run_premarket`:

```python
technical_facts_path=technical_facts_result.run_path,
decision_facts_path=decision_facts_result.run_path,
```

In `DailyPremarketRunner.__init__`, use the real summary generator:

```python
self.summary_generator = summary_generator or generate_tradingagents_summary
```

In `_run_locked`, remove these two keyword arguments entirely:

```python
technical_facts_generator=_write_skipped_technical_facts,
decision_facts_generator=_write_skipped_decision_facts,
```

Retain dependency injection through the existing `premarket_runner` and `summary_generator` arguments; do not add another orchestration layer.

**Step 4: Run focused tests and confirm GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_daily_premarket.py tests/test_premarket_pipeline.py
```

Expected: all selected tests pass.

**Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/advice/premarket.py tests/test_daily_premarket.py tests/test_premarket_pipeline.py
git commit -m "fix: generate daily decision facts"
```

### Task 2: Add Futu facts and strict per-symbol daily failure reporting

**Files:**
- Modify: `src/open_trader/daily_premarket.py:1-80`
- Modify: `src/open_trader/daily_premarket.py:368-820`
- Modify: `src/open_trader/daily_premarket.py:1120-1700`
- Create: `src/open_trader/decision_source_availability.py`
- Create: `tests/test_decision_source_availability.py`
- Test: `tests/test_daily_premarket.py`

**Step 1: Write failing daily orchestration tests**

Add a fake Futu generator that writes a run artifact containing all four required modules for `US.MSFT`, and inject it into `_daily_runner`. Assert it receives the refreshed portfolio, market, run date, `update_latest=False`, and a constructed extractor.

```python
assert futu_facts.calls == [{
    "portfolio_path": refreshed_portfolio,
    "data_dir": config.data_dir,
    "run_date": "2026-06-19",
    "market": "US",
    "update_latest": False,
}]
```

Add one parameterized failure case for each required source. Each case should write one unavailable/missing module for `US.MSFT`, then assert:

```python
assert result.status == "failed"
status = json.loads(result.status_path.read_text(encoding="utf-8"))
assert status["readiness"] == "blocked"
assert status["source_failures"] == [{
    "market": "US",
    "symbol": "MSFT",
    "source": source,
    "error": expected_error,
}]
assert "source_incomplete" in status["status_reasons"]
```

Add promotion protection to each failure test:

```python
old_latest = latest_path.read_text(encoding="utf-8")
runner.run(run_date="2026-06-19", market="US")
assert latest_path.read_text(encoding="utf-8") == old_latest
```

Add a notification assertion that the blocker body contains `US.MSFT`, the source name, the error, and the existing retry command:

```text
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.us
```

**Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_daily_premarket.py -k 'futu_facts or source_incomplete or preserves_latest'
```

Expected: constructor does not accept a Futu generator, no Futu artifact exists, and incomplete sources do not fail the run.

**Step 3: Add the Futu generator to the existing runner**

Import the existing implementation:

```python
from .futu_skill_facts import (
    FutuSkillFactResult,
    FutuSkillFactsExtractor,
    generate_futu_skill_facts,
)
```

Add injectable defaults:

```python
futu_facts_generator: Callable[..., FutuSkillFactResult] = generate_futu_skill_facts,
futu_facts_extractor_factory: Callable[[], object] = FutuSkillFactsExtractor,
```

After technical and decision facts exist, and before plan/action publication, invoke:

```python
futu_facts_result = self.futu_facts_generator(
    portfolio_path=portfolio_path,
    data_dir=config.data_dir,
    run_date=run_date,
    market=market,
    extractor=self.futu_facts_extractor_factory(),
    update_latest=False,
)
```

Keep this synchronous in the existing HK/US daily process.

**Step 4: Validate run artifacts against advice symbols using the Dashboard's real rules**

Create a pure evaluator in `decision_source_availability.py` and call it from a small adapter in `daily_premarket.py`:

```python
@dataclass(frozen=True)
class SourceFailure:
    market: str
    symbol: str
    source: str
    error: str


def evaluate_required_sources(
    *,
    advice_rows: list[dict[str, str]],
    technical_records: dict[tuple[str, str], dict[str, object]],
    decision_records: dict[tuple[str, str], dict[str, object]],
    tradingagents_records: dict[tuple[str, str], dict[str, object]],
    futu_records: dict[tuple[str, str], dict[str, object]],
) -> list[SourceFailure]:
    ...
```

The implementation must:

1. Read only `(market, symbol)` pairs from the current advice CSV.
2. Index each run JSON's `records` by `(market, symbol)`.
3. Apply the same availability semantics already used by `dashboard.py`, not a raw `available` field that does not exist in run artifacts:
   - TradingAgents: matching current run date, no record error, and a valid normalized summary record.
   - Technical facts: matching current advice run date and source hash, `extraction_status == "ok"`, and no missing timeframe.
   - Decision K-line/news: matching hashes from `extract_decision_sources(raw_decision)`, `status == "ok"`, and the exact required field set.
   - Futu modules: record run date matching the current advice plus module `status in {"ok", "partial"}`.
4. Treat a missing record or failed predicate as unavailable.
5. Check all eight canonical source names from the global constraints.
6. Prefer the record/module's `error`, then `blocking_reason`, then `status`, then `"数据未生成"` as the failure detail.
7. Return deterministic market/symbol/source order for stable tests and reports.

Move or wrap the relevant pure predicates from `dashboard.py` so both the Dashboard transformation and daily evaluator call the same predicates. Do not maintain two independent definitions of source availability.

Pass the failures into `_derive_daily_state`; any non-empty list yields `status="failed"`, `readiness="blocked"`, and reason `source_incomplete`.

Add `source_failures` to the status JSON and Markdown report. Extend `_blocker_notification_message` to render each failure and the market-specific retry command.

**Step 5: Promote only a complete artifact set**

Add `futu_skill_facts_path` to `_promote_latest_set`. Call promotion only when `source_failures` is empty. This preserves all prior latest files as one coherent set if any new source is incomplete.

Add these artifact keys everywhere status payloads are constructed:

```python
"futu_skill_facts": str(futu_skill_facts_path) if futu_skill_facts_path else "",
"latest_futu_skill_facts": str(latest_futu_skill_facts_path),
```

Delete `_write_skipped_technical_facts`, `_write_skipped_decision_facts`, and `_write_skipped_tradingagents_summary` after all references and tests are gone.

**Step 6: Run daily tests and confirm GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_daily_premarket.py tests/test_futu_skill_facts.py tests/test_tradingagents_summary.py tests/test_technical_facts.py tests/test_decision_facts.py
```

Expected: all tests pass, including every unavailable-source parameter.

**Step 7: Commit**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/decision_source_availability.py tests/test_daily_premarket.py tests/test_decision_source_availability.py
git commit -m "fix: fail daily run on incomplete sources"
```

### Task 3: Give TradingAgents its own fifth tab and expose real failures

**Files:**
- Modify: `src/open_trader/dashboard.py:1033-1075`
- Modify: `src/open_trader/dashboard_static/dashboard.js:47-53`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2493-2575`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard.py:1415-1645`
- Test: `tests/test_dashboard_web.py:1020-1060`
- Test: `tests/test_dashboard_web.py:3040-3170`

**Step 1: Write failing backend and browser-rendering unit tests**

Update the expected summary detail contract:

```python
assert holding["tradingagents_summary"] == {
    "available": False,
    "status": "missing_current_summary",
    "error": "TradingAgents summary is unavailable for current advice",
    "ta_view": "低配",
    "current_action": "持有",
    "core_reason": "缺失",
    "ta_report_date": "2026-07-10",
    "latest_run_date": "2026-07-10",
}
```

Update JavaScript tests to require this exact order:

```javascript
["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]
```

Assert the final tab HTML contains the decision template but not the TradingAgents card, and the TradingAgents tab contains only its card.

Assert an unavailable summary produces both `decision-tab-failed` and a red panel containing its real `error`, rather than hiding the tab or treating fallback display text as available.

Add backend cases proving an otherwise healthy technical or Futu record from an older run date is unavailable for the current advice row. This closes the stale-cache case even when a module itself says `status: "ok"`.

**Step 2: Run focused tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_dashboard.py tests/test_dashboard_web.py -k 'tradingagents or decision_tab'
```

Expected: four-tab order, TradingAgents nested in final, and missing summary error fields are reported.

**Step 3: Add backend failure metadata**

In `_tradingagents_summary_detail`, return `status` and `error` in both branches. The available branch should use `status: "available"` and an empty error. The unavailable branch should retain the existing fallback display fields but set:

```python
"status": "missing_current_summary",
"error": "TradingAgents summary is unavailable for current advice",
```

Pass the current advice row into the shared technical and Futu availability predicates. Require their run date to equal `agent_report.run_date`; expose `stale_run_date` and a concrete error when it does not. The UI continues to render the red tab/panel from that error.

**Step 4: Split the tab definitions**

Change the tab list to:

```javascript
const DECISION_TABS = [
  { key: "final", label: "最终决策" },
  { key: "tradingagents", label: "TradingAgents" },
  { key: "kline", label: "趋势 / K 线" },
  { key: "news", label: "新闻 / 舆论" },
  { key: "futu", label: "富途异动" },
];
```

Change the view definitions:

```javascript
final: {
  available: Boolean(holding && holding.agent_report && holding.agent_report.available === true),
  error: holding && holding.agent_report && holding.agent_report.error,
  html: renderLLMDecisionTemplate(holding),
},
tradingagents: {
  available: summary.available === true,
  error: summary.error,
  html: renderTradingAgentsSummaryCard(holding),
},
```

Do not use fallback text to infer source availability. Keep existing full-width layout, horizontal mobile tab scrolling, and reset-to-final behavior.

**Step 5: Run Dashboard tests and confirm GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_dashboard.py tests/test_dashboard_web.py
```

Expected: all tests pass.

**Step 6: Commit**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: split TradingAgents into decision tab"
```

### Task 4: Make source completeness a hard API and browser acceptance gate

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:13-85`
- Modify: `src/open_trader/dashboard_acceptance.py:138-190`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2050-2080`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `Makefile` only if the current acceptance target does not already invoke this module

**Step 1: Write failing payload contract tests**

Expand `valid_payload()` so its in-scope `US.MSFT` row has `agent_report.available == true` and all eight nested sources available. Leave the five CN rows out of scope with `agent_report.available == false`.

Add one parameterized case per required source:

```python
@pytest.mark.parametrize("path", REQUIRED_SOURCE_PATHS)
def test_validate_dashboard_payload_rejects_each_missing_current_source(path):
    payload = valid_payload()
    source = nested_get(payload["holdings"][-1], path)
    source["available"] = False
    source["status"] = "stale_source_hash"

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert any("US.MSFT" in error and path[-1] in error for error in errors)
```

Add coverage that an out-of-scope holding may have unavailable sources without failing acceptance.

**Step 2: Write failing browser-gate tests around exact selectors**

Add `_first_in_scope_holding(payload)` and test that it returns `("US", "MSFT")` for `valid_payload()` and raises a clear error when no advice-backed holding exists.

Add `data-detail-market` and `data-detail-symbol` to the existing trading-decision row button and cover their rendered values in `tests/test_dashboard_web.py`:

```javascript
data-detail-market="${escapeHtml(holding.market)}"
data-detail-symbol="${escapeHtml(holding.symbol)}"
```

Extract `_check_decision_tabs(page, market, symbol)` in the acceptance module. It must locate the exact button with:

```python
button = page.locator(
    'button[data-detail-mode="decision"]'
    f'[data-detail-market="{market}"]'
    f'[data-detail-symbol="{symbol}"]'
)
```

The live check must, for both desktop and mobile:

1. Open one real holding whose `agent_report.available` is true.
2. Confirm exactly five tabs in the fixed order.
3. Confirm no tab has `decision-tab-failed`.
4. Click every tab.
5. Confirm its panel is visible and does not contain `数据未生成`.

**Step 3: Run focused acceptance tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_dashboard_acceptance.py
```

Expected: missing sources do not yet affect validation and browser code checks only the old portfolio filters.

**Step 4: Implement strict payload validation**

Define the canonical paths once:

```python
REQUIRED_SOURCE_PATHS = (
    ("tradingagents_summary",),
    ("technical_facts",),
    ("decision_facts", "kline"),
    ("decision_facts", "news_sentiment"),
    ("futu_skill_facts", "news_sentiment"),
    ("futu_skill_facts", "technical_anomaly"),
    ("futu_skill_facts", "capital_anomaly"),
    ("futu_skill_facts", "derivatives_anomaly"),
)
```

Inside `validate_dashboard_payload`, select only holdings with `agent_report.available is True`. For each path, require a mapping with `available is True`; otherwise append a deterministic error containing market, symbol, dotted source path, and the best available detail from `error`, `blocking_reason`, or `status`.

Because `validate_dashboard_payload` already runs on both API refresh cycles, this makes the source gate apply twice without duplicating logic.

**Step 5: Implement the live five-tab browser check**

Use the fetched payload to choose the first in-scope `(market, symbol)`. In each viewport, locate that exact button, open its trading-decision detail, and require:

```python
expected_labels = ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]
tabs = page.locator('.decision-tab-list [data-decision-tab]')
assert tabs.all_inner_texts() == expected_labels
assert page.locator('.decision-tab-list .decision-tab-failed').count() == 0
for index in range(tabs.count()):
    tabs.nth(index).click()
    panel = page.locator('.decision-tab-panel:visible')
    assert panel.count() == 1
    assert "数据未生成" not in panel.inner_text()
```

Convert assertion failures into viewport-prefixed acceptance errors, then retain the existing Phillips and Eastmoney browser checks.

Do not replace browser failures with curl, fixtures, screenshots, or unit tests. Browser unavailability must remain `BLOCKED`.

**Step 6: Run acceptance unit tests and full automated tests**

Run:

```bash
.venv/bin/pytest -q tests/test_dashboard_acceptance.py tests/test_dashboard.py tests/test_dashboard_web.py
make test
```

Expected: all tests pass.

**Step 7: Commit**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile
git commit -m "test: require all dashboard data sources"
```

### Task 5: Rebuild real data, restart live code, and pass the final gate

**Files:**
- Inspect: `config/launchd/*.plist`
- Inspect: `data/runs/<DATE>/<MARKET>/daily_run_status.json`
- Inspect: `data/latest/<MARKET>/*.json`
- Inspect: `logs/daily_premarket/<DATE>-<MARKET>.log`
- Inspect: `/tmp/open_trader_dashboard_8766.log`

**Step 1: Run the complete automated suite before touching live processes**

Run:

```bash
make test
```

Expected: exact output ends with all tests passing and exit code 0.

**Step 2: Inspect and restart stale scheduled processes**

Run:

```bash
launchctl print gui/$(id -u)/com.open-trader.premarket.hk
launchctl print gui/$(id -u)/com.open-trader.premarket.us
screen -ls
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Stop/restart any process that can still hold pre-change Python or Dashboard code in memory. Preserve the existing schedules: HK weekdays 08:00 Asia/Shanghai and US weekdays 18:30 Asia/Shanghai.

**Step 3: Run both real daily workflows**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.hk
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.us
```

Wait by polling `launchctl print` and fresh log timestamps; do not use a blind long sleep. Confirm both jobs exit successfully.

Inspect each new status file and require an empty source-failure list. Existing unrelated review states may still make the overall run `partial`, but a source failure must always make it `failed`:

```json
{
  "source_failures": []
}
```

Also inspect each current advice symbol in the generated and latest technical, decision, TradingAgents, and Futu JSON artifacts. If either market fails, diagnose, fix, rerun tests, restart affected code, and rerun both workflows as needed. Do not proceed with stale or partial data.

**Step 4: Restart the Dashboard and verify its new process**

Use the existing detached-screen restart command documented in `README.md`:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766'
```

Then verify:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -a -p <PID> -d cwd -Fn
git rev-parse HEAD
tail -n 100 /tmp/open_trader_dashboard_8766.log
```

Require one listener, repository cwd, current Git SHA, a fresh PID/timestamp, and no traceback or load-failure marker.

**Step 5: Run the mandatory final acceptance gate**

Run as the final verification command:

```bash
make acceptance
```

Expected: JSON result with `"status": "PASS"`, no errors, no blocker, a current Dashboard PID, two successful API refresh cycles, and successful desktop/mobile five-tab flows.

If the result is `FAIL`, continue diagnosing and fixing, then rerun the affected tests, live workflows/process restart, and `make acceptance`. If it is `BLOCKED`, report only the blocker. Do not ask the user to review in either case.

**Step 6: Commit any verification-driven fixes and rerun the gate**

If live verification required code changes, commit them in a focused commit and repeat Steps 1-5. The last command before handoff must still be `make acceptance` returning `PASS`.

**Step 7: Hand off for user review only after PASS**

Report the live URL, current PID/SHA, the exact `make acceptance` PASS result, and the two retained manual retry commands:

```bash
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.hk
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.us
```
