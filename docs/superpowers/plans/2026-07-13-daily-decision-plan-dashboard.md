# Daily Decision Plan Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Dashboard's unsupported final conclusion with one immutable daily plan per symbol, backed by deterministic strategy formulas, an explicit backtest gate, a 10% position cap, edge-trigger notifications, and a factual non-executable fallback.

**Architecture:** Extend the existing standard-strategy/backtest path instead of creating another strategy engine. A new `decision_plan.py` module owns the v1 artifact, validation, selection, and atomic publication; the existing premarket workflow supplies portfolio, K-line, TradingAgents, and backtest inputs. A small watcher consumes only validated plans and appends replayable JSONL edge events, while the Dashboard remains a read-only projection of plan and event artifacts.

**Tech Stack:** Python 3.12, `Decimal`, dataclasses, JSON/JSONL, existing Backtrader adapter, existing Futu/AKShare providers, vanilla JavaScript/CSS, pytest, existing `make acceptance` browser gate.

## Global Constraints

- Each symbol has at most one plan per trading day; ordinary intraday movement never rewrites it.
- US/HK daily K-lines and completed closes come from Futu; CN daily K-lines come from AKShare. Never silently switch or mix providers within a plan or backtest.
- Use only completed daily bars available before the plan's `effective_at`; current intraday quotes are watcher inputs, never historical closes.
- A validated plan requires a versioned deterministic strategy, formula/source provenance, completed portfolio risk, current market data, and an explicit passing backtest gate.
- A single-instrument position may not be increased beyond 10% of portfolio NAV. Existing overweight positions may be held or reduced, never increased.
- A failed gate, insufficient eligible history, or listing history below one year produces `fallback_advice`, not executable conditions. Missing or corrupt required input produces a visible generation failure.
- Fallback advice is non-executable and contains facts, a concise TradingAgents interpretation, a recommendation, and the 10% constraint.
- The watcher notifies only on `false -> true`; `true -> false` resets the edge so the same condition can trigger again later that day.
- Orders, user compliance, broker attribution, auto-ordering, intraday regeneration, material-event intervention, new-listing trend strategies, and database persistence remain out of scope.
- All numeric JSON fields are decimal strings. Daily publication is atomic and a failed run never replaces `data/latest/<MARKET>/decision_plans.json`.
- Every behavior slice is test-first. After every modification run `make acceptance`; only `PASS` counts as completion.
- After the final `PASS`, redeploy the exact accepted Git SHA and verify PID, working directory, SHA, fresh logs, HTTP 200, desktop, and mobile review flows.

## File Map

- Modify `src/open_trader/strategy_backtest.py`: add Sharpe and the single authoritative gate result to existing standard-backtest output.
- Modify `src/open_trader/standard_strategies.py`: expose current deterministic indicator values and next-condition formulas from the same rules used in backtests.
- Create `src/open_trader/decision_plan.py`: build, validate, load, and atomically publish v1 daily plan artifacts.
- Modify `src/open_trader/plan_events.py`: use the approved v1 event vocabulary and strict JSONL replay.
- Create `src/open_trader/decision_plan_watch.py`: evaluate false/true edges, append events, and call the existing notifier.
- Modify `src/open_trader/daily_premarket.py`: generate backtest evidence and daily plans before promoting latest artifacts.
- Modify `src/open_trader/cli.py`: expose a plan watcher command using the existing Futu quote and notification configuration.
- Modify `src/open_trader/dashboard.py`: attach the latest plan, today's events, and previous-trading-day review to each holding.
- Modify `src/open_trader/dashboard_static/dashboard.js`: replace only the Final Decision tab with validated/fallback/failed renderers and deep-link selection.
- Modify `src/open_trader/dashboard_static/dashboard.css`: add the approved responsive plan layout using existing tokens.
- Test in the corresponding existing test modules plus new focused `test_decision_plan.py` and `test_decision_plan_watch.py`.

---

### Task 1: Make the existing backtest output a decision gate

**Files:**
- Modify: `src/open_trader/strategy_backtest.py`
- Test: `tests/test_strategy_backtest.py`

**Interfaces:**
- Produces: `ExecutionResult.sharpe_ratio: Decimal | None`.
- Produces: serialized top-level `gate: {"passed": bool, "policy_id": "benchmark_outperformance/v1", "reasons": list[str]}`.
- Gate policy: the strategy return must be strictly greater than the contemporaneous market benchmark return. Missing benchmark data fails the gate. Maximum drawdown and Sharpe are displayed evidence, not additional hidden thresholds.

- [ ] **Step 1: Write failing tests for Sharpe and the explicit gate**

Add focused assertions to the existing service tests:

```python
def test_standard_backtest_serializes_sharpe_and_passing_gate(tmp_path: Path) -> None:
    result = run_service_with_prices(
        tmp_path,
        strategy_closes=["100", "102", "104", "106"],
        benchmark_closes=["100", "100", "101", "101"],
    ).to_dict()

    assert result["strategy"]["sharpe_ratio"] is not None
    assert result["gate"] == {
        "passed": True,
        "policy_id": "benchmark_outperformance/v1",
        "reasons": [],
    }


def test_standard_backtest_gate_fails_without_benchmark(tmp_path: Path) -> None:
    result = run_service_with_missing_benchmark(tmp_path).to_dict()

    assert result["gate"]["passed"] is False
    assert result["gate"]["reasons"] == ["benchmark_data_missing"]
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_strategy_backtest.py -q`

Expected: FAIL because `sharpe_ratio` and `gate` are absent.

- [ ] **Step 3: Implement daily-return Sharpe and the one gate policy**

Add `sharpe_ratio` to `ExecutionResult`, calculate it from adjacent equity values with a 252-day annualization, and return `None` for fewer than two returns or zero volatility:

```python
def _sharpe_ratio(curve: Sequence[dict[str, str]]) -> Decimal | None:
    equities = [Decimal(row["equity"]) for row in curve]
    returns = [current / previous - Decimal("1") for previous, current in zip(equities, equities[1:]) if previous]
    if len(returns) < 2:
        return None
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal("0")) / Decimal(len(returns))
    if variance == 0:
        return None
    return mean / variance.sqrt() * Decimal(252).sqrt()


def _backtest_gate(result: StandardBacktestResult) -> dict[str, object]:
    if result.market_benchmark is None:
        reasons = ["benchmark_data_missing"]
    elif result.strategy.total_return_pct <= result.market_benchmark.total_return_pct:
        reasons = ["did_not_outperform_benchmark"]
    else:
        reasons = []
    return {
        "passed": not reasons,
        "policy_id": "benchmark_outperformance/v1",
        "reasons": reasons,
    }
```

Serialize both fields as decimal strings, preserving `None` as JSON `null`.

- [ ] **Step 4: Run the focused and full backtest tests**

Run: `.venv/bin/python -m pytest tests/test_strategy_backtest.py tests/test_backtest_prices.py -q`

Expected: PASS.

- [ ] **Step 5: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 6: Commit the slice**

```bash
git add src/open_trader/strategy_backtest.py tests/test_strategy_backtest.py
git commit -m "feat: add explicit standard backtest gate"
```

---

### Task 2: Derive live conditions from the same deterministic strategy formulas

**Files:**
- Modify: `src/open_trader/standard_strategies.py`
- Test: `tests/test_standard_strategies.py`

**Interfaces:**
- Produces: `build_current_strategy_snapshot(strategy_id: str, bars: Sequence[StrategyBar], max_strategy_weight: Decimal) -> dict[str, object]`.
- Snapshot keys: `strategy`, `facts`, and ordered `conditions`; every numeric value is a decimal string and every condition includes `formula`, `inputs`, `source_date`, and `calculated_value`.
- Conditions use aggregate `target_weight`, not order quantities. Risk/exit conditions sort before reduce/add/buy conditions.

- [ ] **Step 1: Write a failing provenance/priority test**

```python
def test_current_snapshot_uses_versioned_formulas_and_risk_first() -> None:
    snapshot = build_current_strategy_snapshot(
        "trend_pullback/v1",
        rising_bars(60),
        Decimal("0.10"),
    )

    assert snapshot["strategy"]["id"] == "trend_pullback/v1"
    assert snapshot["conditions"][0]["priority"] == "risk"
    assert snapshot["conditions"][0]["formula"] == "min(sma50, active_stop)"
    assert snapshot["conditions"][0]["source_date"] == rising_bars(60)[-1].date.isoformat()
    assert snapshot["conditions"][0]["target_weight"] == "0"
    assert snapshot["facts"]["rsi14"]["formula"] == "Wilder RSI(close, 14)"
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_standard_strategies.py -q`

Expected: FAIL because `build_current_strategy_snapshot` does not exist.

- [ ] **Step 3: Implement the snapshot beside the existing formulas**

Reuse `_sma`, `_atr`, `_rsi`, `_bollinger`, `_prior_high`, `_CATALOG`, and `ACTION_PRECEDENCE`; do not create a second indicator library. Return only the formulas required by each selected strategy. The common fact helper is:

```python
def _fact(value: Decimal | None, formula: str, inputs: Mapping[str, object], source_date: date) -> dict[str, object]:
    return {
        "formula": formula,
        "inputs": {key: str(item) for key, item in inputs.items()},
        "source_date": source_date.isoformat(),
        "calculated_value": None if value is None else str(value),
    }
```

For `trend_pullback/v1`, emit exit at `min(sma50, active_stop)`, reduce at `sma20 + 2*atr14`, add at prior five-day high, and buy/reclaim at SMA20 when those values are computable. Apply the analogous existing formulas for breakout and mean-reversion; omit conditions whose inputs cannot be calculated rather than inventing values.

- [ ] **Step 4: Prove snapshot thresholds match signal formulas**

Add one fixture per catalog strategy and assert the snapshot's calculated thresholds equal values independently visible in the fixture, while action labels and target weights follow `ACTION_TARGET_FRACTIONS`.

Run: `.venv/bin/python -m pytest tests/test_standard_strategies.py -q`

Expected: PASS.

- [ ] **Step 5: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 6: Commit the slice**

```bash
git add src/open_trader/standard_strategies.py tests/test_standard_strategies.py
git commit -m "feat: expose strategy condition provenance"
```

---

### Task 3: Build and atomically publish one daily decision plan per symbol

**Files:**
- Create: `src/open_trader/decision_plan.py`
- Create: `tests/test_decision_plan.py`

**Interfaces:**
- Produces: `build_decision_plan(*, run_date: str, market: str, symbol: str, position: Mapping[str, str], strategy_snapshots: Sequence[Mapping[str, object]], backtests: Sequence[Mapping[str, object]], technical_facts: Mapping[str, object], tradingagents_summary: Mapping[str, object], effective_at: str, expires_at: str) -> dict[str, object]`.
- Produces: `validate_decision_plan(record: Mapping[str, object]) -> None`.
- Produces: `publish_decision_plans(*, data_dir: Path, run_date: str, market: str, records: Sequence[Mapping[str, object]], update_latest: bool) -> tuple[Path, Path]`.
- Produces: `load_decision_plans(path: Path) -> list[dict[str, object]]` with strict validation.

- [ ] **Step 1: Write failing public-seam tests**

```python
def test_builds_validated_plan_only_from_passing_strategy() -> None:
    plan = build_decision_plan(
        run_date="2026-07-13", market="US", symbol="DRAM",
        position={"quantity": "400", "weight": "0.078", "nav": "100000"},
        strategy_snapshots=[passing_strategy_snapshot()],
        backtests=[passing_backtest("6M"), passing_backtest("1Y"), passing_backtest("5Y")],
        technical_facts=present_facts(), tradingagents_summary=present_summary(),
        effective_at="2026-07-13T09:30:00-04:00",
        expires_at="2026-07-13T16:00:00-04:00",
    )

    assert plan["mode"] == "validated_plan"
    assert plan["max_weight"] == "0.10"
    assert plan["conditions"][0]["priority"] == "risk"
    assert all(Decimal(item["target_weight"]) <= Decimal("0.10") for item in plan["conditions"])


def test_failed_gate_produces_non_executable_fallback() -> None:
    plan = build_decision_plan(
        run_date="2026-07-13", market="US", symbol="DRAM",
        position={"quantity": "400", "weight": "0.078", "nav": "100000"},
        strategy_snapshots=[passing_strategy_snapshot()],
        backtests=[passing_backtest("6M"), failed_backtest("1Y")],
        technical_facts=present_facts(), tradingagents_summary=present_summary(),
        effective_at="2026-07-13T09:30:00-04:00",
        expires_at="2026-07-13T16:00:00-04:00",
    )

    assert plan["mode"] == "fallback_advice"
    assert plan["conditions"] == []
    assert plan["fallback"]["label"] == "非执行型建议"
    assert plan["fallback"]["recommendation"] in {"观察", "禁止加仓", "考虑降低风险"}


def test_publication_rejects_duplicate_symbol_without_replacing_latest(tmp_path: Path) -> None:
    latest = tmp_path / "latest/US/decision_plans.json"
    latest.parent.mkdir(parents=True)
    latest.write_text('{"old": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        publish_decision_plans(
            data_dir=tmp_path, run_date="2026-07-13", market="US",
            records=[validated_plan(), validated_plan()], update_latest=True,
        )

    assert latest.read_text(encoding="utf-8") == '{"old": true}'
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_decision_plan.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement selection and fallback without a new framework**

Use plain dicts plus strict boundary validation. For symbols with at least one year of data, a strategy is eligible only when every required available range has `gate.passed=true`; require 6M and 1Y, and include 5Y when the price history reaches five years. Select the eligible strategy with the highest 1Y excess return, breaking ties by catalog order. For less than one year, return fallback immediately.

Clamp only buy/add targets to `0.10`; if current weight already exceeds `0.10`, remove buy/add conditions and retain hold/reduce/exit conditions with `risk_status="overweight_no_add"`. Never change a sell target upward.

Build fallback facts from already-calculated MA distance, RSI14, Bollinger position, and relative volume. Missing required facts raises `ValueError` and creates no plan record; it is not converted to neutral advice.

- [ ] **Step 4: Implement strict decimal/provenance validation and atomic writes**

Use the standard library only:

```python
def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
```

Validate schema versions, date/market/symbol identity, unique plan and condition IDs, finite decimal strings, ordered risk priority, required provenance, backtest gate consistency, and `conditions == []` for fallback mode before either run or latest publication.

- [ ] **Step 5: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_decision_plan.py tests/test_systematic_plan.py -q`

Expected: PASS.

- [ ] **Step 6: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 7: Commit the slice**

```bash
git add src/open_trader/decision_plan.py tests/test_decision_plan.py
git commit -m "feat: publish daily decision plan artifacts"
```

---

### Task 4: Add replayable false-to-true plan notifications

**Files:**
- Modify: `src/open_trader/plan_events.py`
- Create: `src/open_trader/decision_plan_watch.py`
- Modify: `tests/test_plan_events.py`
- Create: `tests/test_decision_plan_watch.py`

**Interfaces:**
- Event types: `condition_triggered`, `condition_reset`, `notification_sent`, `notification_failed`, `plan_expired`.
- Produces: `evaluate_plan_snapshot(*, plan: Mapping[str, object], previous_truth: Mapping[str, bool], last_price: Decimal, as_of: datetime) -> tuple[list[PlanEvent], dict[str, bool]]`.
- Produces: `run_decision_plan_watch(*, plans_path: Path, events_path: Path, quote_client: QuoteClientProtocol, notifier: Notifier, poll_seconds: float, once: bool, sleep_fn: Callable[[float], None], now_fn: Callable[[], datetime]) -> DecisionPlanWatchResult` using the existing quote-client protocol and notifier interface.

- [ ] **Step 1: Write the transition-table test first**

```python
def test_same_condition_can_trigger_reset_and_trigger_again() -> None:
    truth: dict[str, bool] = {}
    first, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("66"), as_of=at("10:00"))
    held, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("67"), as_of=at("10:01"))
    reset, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("64"), as_of=at("10:02"))
    second, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("66"), as_of=at("10:03"))

    assert [event.event_type for event in first] == ["condition_triggered"]
    assert held == []
    assert [event.event_type for event in reset] == ["condition_reset"]
    assert [event.event_type for event in second] == ["condition_triggered"]
```

Also test that fallback records never enter the watcher, expiry emits once, malformed JSONL raises a visible load error, and notifier failure appends `notification_failed` without altering truth state.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_plan_events.py tests/test_decision_plan_watch.py -q`

Expected: FAIL on the old event vocabulary and missing watcher.

- [ ] **Step 3: Implement strict events and edge evaluation**

Keep `PlanEvent` as the one persisted representation. Include `condition_id` directly on the dataclass, and make `load_plan_events` report the offending line number:

```python
def load_plan_events(path: Path) -> list[PlanEvent]:
    events: list[PlanEvent] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(PlanEvent(**json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid plan event line {line_number}") from exc
    return events
```

Evaluate conditions in stored priority order. A price condition is true only for its declared operator; a deadline condition is true at/after its timestamp. Append the trigger/reset event before attempting notification, then append `notification_sent` or `notification_failed` with a stable UUID event ID.

- [ ] **Step 4: Render the approved notification payload**

The notifier message must include market/symbol, actual trigger fact, suggested action, current aggregate quantity, target aggregate quantity/weight, formula source, and `/?market=US&symbol=DRAM&decision_tab=final`. Do not infer an order quantity or execution status.

- [ ] **Step 5: Run watcher tests**

Run: `.venv/bin/python -m pytest tests/test_plan_events.py tests/test_decision_plan_watch.py -q`

Expected: PASS.

- [ ] **Step 6: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 7: Commit the slice**

```bash
git add src/open_trader/plan_events.py src/open_trader/decision_plan_watch.py tests/test_plan_events.py tests/test_decision_plan_watch.py
git commit -m "feat: notify on decision plan condition edges"
```

---

### Task 5: Wire plans into the existing daily workflow and CLI

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_daily_premarket.py`
- Test: `tests/test_futu_watch_cli.py`

**Interfaces:**
- `DailyPremarketRunner` generates decision plans after portfolio/fact inputs and standard backtests are available, but before latest promotion.
- Provider rule: `FutuQuoteClient` for US/HK and `AkShareDailyKlineProvider` for CN, with provider name recorded in every plan/backtest input.
- CLI command: `watch-decision-plans --plans data/latest/<MARKET>/decision_plans.json --date YYYY-MM-DD`.

- [ ] **Step 1: Write a failing workflow test for provider and promotion behavior**

```python
def test_daily_runner_promotes_decision_plan_only_after_complete_success(tmp_path: Path) -> None:
    runner = configured_runner(tmp_path, decision_plan_builder=fake_plan_builder)

    result = runner.run(run_date="2026-07-13", market="US")

    assert result.status == "success"
    assert (tmp_path / "data/runs/2026-07-13/US/decision_plans.json").exists()
    assert (tmp_path / "data/latest/US/decision_plans.json").exists()
    assert fake_plan_builder.calls[0]["provider_source"] == "futu"


def test_daily_runner_does_not_promote_partial_plan_on_generation_error(tmp_path: Path) -> None:
    old = tmp_path / "data/latest/US/decision_plans.json"
    write_old_plan(old)
    runner = configured_runner(tmp_path, decision_plan_builder=raising_plan_builder)

    result = runner.run(run_date="2026-07-13", market="US")

    assert result.status == "failed"
    assert load_json(old)["run_date"] == "2026-07-12"
```

- [ ] **Step 2: Run the workflow tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_daily_premarket.py -q`

Expected: FAIL because the workflow has no decision-plan stage.

- [ ] **Step 3: Add the minimum orchestration**

For each eligible symbol:

1. Obtain completed daily bars through `ensure_resolved_backtest_price_range`, which already caches OHLCV under `data/prices/<MARKET>/<SYMBOL>.csv`.
2. Run catalog strategies for 6M, 1Y, and 5Y when enough history is available, reusing `StandardBacktestService` and its immutable outputs.
3. Build current strategy snapshots from the same bars.
4. Call `build_decision_plan` with aggregate portfolio quantity/weight, technical facts, TradingAgents summary, effective market open, and market close expiry.
5. Publish the dated artifact with `update_latest=False` and add its latest path to the existing all-or-nothing promotion list.

Instantiate one provider per market run and close it in `finally`. Do not catch a Futu failure and retry through AKShare.

- [ ] **Step 4: Add the watcher command using existing configuration**

Parse the command alongside `watch-futu`, construct `FutuQuoteClient` and `build_notifier(config)`, and call `run_decision_plan_watch`. Reject CN plans because the watcher uses Futu live quotes in v1.

- [ ] **Step 5: Run the workflow and CLI tests**

Run: `.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_futu_watch_cli.py -q`

Expected: PASS.

- [ ] **Step 6: Exercise the real dry-run command**

Run the project's configured daily premarket command with `--dry-run` for one enabled market, then inspect the dated `decision_plans.json`. Verify its `run_date`, source provider, completed-bar cutoff, gate, and absence of a latest-file mutation.

Expected: command exits 0 and the dated artifact validates.

- [ ] **Step 7: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 8: Commit the slice**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/cli.py tests/test_daily_premarket.py tests/test_futu_watch_cli.py
git commit -m "feat: generate plans in daily market workflow"
```

---

### Task 6: Project plan and event artifacts into the Dashboard API

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Each holding gets one display-ready `decision_plan` object with `available`, `mode`, `status`, plan fields, today's trigger counts, and `previous_review`.
- Invalid/missing current artifact returns `available=false` plus a visible error; it never falls back to `agent_report` for the Final Decision tab.

- [ ] **Step 1: Write failing API projection tests**

```python
def test_dashboard_attaches_plan_events_and_previous_review(tmp_path: Path) -> None:
    write_current_plan(tmp_path, validated_plan())
    write_events(tmp_path, current_trigger_events(2))
    write_previous_plan(tmp_path, fallback_plan(run_date="2026-07-10"))

    holding = load_dashboard_state(config(tmp_path)).to_dict()["holdings"][0]

    assert holding["decision_plan"]["available"] is True
    assert holding["decision_plan"]["conditions"][0]["trigger_count"] == 2
    assert holding["decision_plan"]["previous_review"]["run_date"] == "2026-07-10"
    assert "compliance" not in holding["decision_plan"]["previous_review"]


def test_dashboard_exposes_invalid_plan_as_failed_state(tmp_path: Path) -> None:
    write_invalid_current_plan(tmp_path)

    plan = load_dashboard_state(config(tmp_path)).to_dict()["holdings"][0]["decision_plan"]

    assert plan["available"] is False
    assert plan["error"] == "decision_plans.json 无效"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -q`

Expected: FAIL because holdings have no `decision_plan`.

- [ ] **Step 3: Add strict loading and display normalization**

Index current plans by `(market, symbol)`, strictly load today's event file, and count `condition_triggered` events per condition. Find the most recent earlier dated plan directory for the same market/symbol and include objective prior occurrences, prior closing quantity, current starting quantity, and raw order context if already available. Do not calculate strategy eligibility, risk, trigger truth, or compliance in Dashboard code.

- [ ] **Step 4: Run Dashboard state tests**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -q`

Expected: PASS.

- [ ] **Step 5: Run the required acceptance gate**

Run: `make acceptance`

Expected: `PASS`.

- [ ] **Step 6: Commit the slice**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose daily plans in dashboard state"
```

---

### Task 7: Render the approved validated, fallback, and failed layouts

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- `renderDecisionPlan(holding)` dispatches to `renderValidatedDecisionPlan`, `renderFallbackDecisionPlan`, or a failed state.
- URL query keys: `market`, `symbol`, `decision_tab=final`.
- Browser performs formatting only; no gate, formula, risk, or edge calculations.

- [ ] **Step 1: Write failing browser-render tests for both modes**

Use the existing Node-based JavaScript harness:

```javascript
const validated = renderDecisionPlan({ decision_plan: validatedPlanFixture });
for (const text of ["今日交易计划", "下一条件", "目标仓位", "回测闸门", "最大回撤", "夏普比率", "参数来源"]) {
  if (!validated.includes(text)) throw new Error("missing " + text);
}

const fallback = renderDecisionPlan({ decision_plan: fallbackPlanFixture });
for (const text of ["非执行型建议", "禁止加仓", "RSI", "布林带", "为什么没有可执行计划"]) {
  if (!fallback.includes(text)) throw new Error("missing " + text);
}
if (fallback.includes("data-plan-condition")) throw new Error("fallback rendered executable condition");
```

Add a 375px Playwright assertion to the existing acceptance fixture: no horizontal overflow, semantic `details/summary` for previous review, visible keyboard focus, and all interactive targets at least 44px.

- [ ] **Step 2: Run the web tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Expected: FAIL because the Final Decision tab still renders the LLM template.

- [ ] **Step 3: Replace only the Final Decision tab data source**

Change the `final` entry in `decisionTabViews` to:

```javascript
final: {
  available: Boolean(holding && holding.decision_plan && holding.decision_plan.available === true),
  error: holding && holding.decision_plan && holding.decision_plan.error,
  html: renderDecisionPlan(holding),
},
```

Render the approved hierarchy: status banner, ordered conditions, evidence rail/backtest table, parameter provenance, and collapsed previous review. For fallback, render the non-executable label, fact cards, TradingAgents interpretation, reason, recommendation, and 10% constraint. Use existing `escapeHtml`, formatting helpers, status tokens, and tabular-number classes.

- [ ] **Step 4: Add responsive CSS with current tokens only**

Use a two-column CSS grid above 900px, one column below it, and condition cards on phones. Add `font-variant-numeric: tabular-nums`, explicit text status labels, `:focus-visible`, 44px controls, a 150ms transition, and a `prefers-reduced-motion` override. Add no dependency and no new design system.

- [ ] **Step 5: Implement deep-link restoration**

At initial state load, read `market`, `symbol`, and `decision_tab`; select the matching holding and Final Decision tab. When the user changes holdings or tabs, update the URL with `history.replaceState` so refresh and browser back preserve the view.

- [ ] **Step 6: Run web and Dashboard tests**

Run: `.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q`

Expected: PASS.

- [ ] **Step 7: Run the required final acceptance gate**

Run: `make acceptance`

Expected: final line reports `PASS`, including real API/data, two refresh cycles, process version/log checks, and desktop/mobile browser flows.

- [ ] **Step 8: Commit the accepted UI slice**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render daily decision plans"
```

---

### Task 8: Redeploy the exact accepted revision for review

**Files:**
- No source changes.

**Interfaces:**
- Review URL opens the deployed Final Decision tab for a real holding.

- [ ] **Step 1: Record the accepted revision**

Run: `git rev-parse HEAD`

Expected: one Git SHA with a clean tracked worktree.

- [ ] **Step 2: Restart the Dashboard from that exact SHA**

Use the repository's existing Dashboard start/restart command. Do not edit source, generated plan data, or configuration after acceptance.

- [ ] **Step 3: Verify the live process**

Check the new PID, process working directory, deployed Git SHA, and a fresh log timestamp. Confirm no older Dashboard process remains on the review port.

- [ ] **Step 4: Verify the review endpoint**

Run: `curl -I 'http://127.0.0.1:8766/?market=US&symbol=DRAM&decision_tab=final'`

Expected: HTTP 200 from the new process.

- [ ] **Step 5: Hand off the direct URL**

Provide `http://127.0.0.1:8766/?market=<MARKET>&symbol=<SYMBOL>&decision_tab=final` with the verified real holding identity and ask the user to review the validated or fallback layout.
