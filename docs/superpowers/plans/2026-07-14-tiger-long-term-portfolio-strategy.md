# Tiger Long-Term Portfolio Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shadow-mode SMA200 long/cash portfolio strategy for the full Tiger account, with equal-weight allocation, symbol and risk-group caps, realistic US costs and cash yield, five-year validation evidence, and Dashboard visibility.

**Architecture:** Add one Tiger-specific domain module and one portfolio-backtest module instead of generalizing the existing single-symbol engine. Reuse the existing Tiger account snapshot, Futu daily K-line client, immutable run/latest artifact pattern, Dashboard state loader, and acceptance workflow. The first artifact is always shadow/calibration-required; no broker order path is added.

**Tech Stack:** Python 3.12, standard library (`dataclasses`, `decimal`, `csv`, `json`, `urllib`), existing Futu OpenAPI client, existing pytest and vanilla Dashboard JavaScript/CSS.

## Global Constraints

- Strategy id is exactly `tiger_sma200_equal_weight/v1`.
- Strategy capital is 100% of account alias `tiger_5683` net liquidation value; Futu and Phillips capital is excluded.
- Pool membership is manual and conditional on the current universe; no selection, ranking, options, leverage, shorting, Bollinger Bands, candlestick patterns, or automatic orders.
- Signal is completed close `> SMA200`; state changes execute at the next session open.
- Symbol cap is 10%, risk-group cap is 30%, and non-risk rebalance tolerance is two percentage points.
- Validation uses one warm-up year and five evaluation years, QFQ-adjusted prices, DGS3MO cash, official Tiger fees, 5 bps slippage, and the same-pool always-long primary benchmark.
- Gate floors are Sharpe `0.8` and Calmar `0.8`; anti-degeneracy thresholds remain deliberately uncalibrated, so version 1 must report `calibration_required` and cannot become active.
- Every source/data/cost failure is explicit; no fixture, zero, or stale-artifact substitution in the live workflow.
- Run `make acceptance` after every source modification. Only `PASS` is completion.

---

### Task 1: Manual Pool, SMA200 Signal, and Allocation Rules

**Files:**
- Create: `config/tiger_long_term_strategy.json`
- Create: `src/open_trader/tiger_long_term.py`
- Test: `tests/test_tiger_long_term.py`

**Interfaces:**
- Produces: `TigerLongTermConfig`, `load_tiger_long_term_config(path)`, `sma200_state(bars)`, `allocate_target_weights(states, risk_groups)`, and `rebalance_reasons(actual, target, previous_states, states)`.
- Consumes: existing `StrategyBar` from `open_trader.standard_strategies`.

- [ ] **Step 1: Write the failing configuration and signal tests**

```python
from datetime import date, timedelta
from decimal import Decimal

from open_trader.standard_strategies import StrategyBar
from open_trader.tiger_long_term import (
    allocate_target_weights,
    load_tiger_long_term_config,
    rebalance_reasons,
    sma200_state,
)


def bars(closes: list[str]) -> list[StrategyBar]:
    start = date(2020, 1, 1)
    return [
        StrategyBar(start + timedelta(days=index), Decimal(close), Decimal(close),
                    Decimal(close), Decimal(close), Decimal("100"))
        for index, close in enumerate(closes)
    ]


def test_loads_fixed_tiger_pool(tmp_path):
    path = tmp_path / "strategy.json"
    path.write_text('{"strategy_id":"tiger_sma200_equal_weight/v1","account_alias":"tiger_5683","members":{"QQQ":"broad_us_growth"}}')
    config = load_tiger_long_term_config(path)
    assert config.members == {"QQQ": "broad_us_growth"}


def test_sma200_uses_only_completed_closes():
    assert sma200_state(bars(["100"] * 199)) == "INELIGIBLE"
    assert sma200_state(bars(["100"] * 200 + ["101"])) == "LONG"
    assert sma200_state(bars(["100"] * 200 + ["100"])) == "CASH"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term.py -q`

Expected: collection fails with `ModuleNotFoundError: open_trader.tiger_long_term`.

- [ ] **Step 3: Add the fixed manual configuration**

```json
{
  "strategy_id": "tiger_sma200_equal_weight/v1",
  "account_alias": "tiger_5683",
  "members": {
    "DRAM": "semiconductor",
    "SOXX": "semiconductor",
    "EUV": "semiconductor",
    "TSM": "semiconductor",
    "SMH": "semiconductor",
    "MSFT": "software",
    "QQQ": "broad_us_growth",
    "AGRZ": "agriculture"
  }
}
```

- [ ] **Step 4: Implement the minimal domain module**

```python
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping, Sequence

from .standard_strategies import StrategyBar

STRATEGY_ID = "tiger_sma200_equal_weight/v1"
SYMBOL_CAP = Decimal("0.10")
RISK_GROUP_CAP = Decimal("0.30")
DRIFT_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True)
class TigerLongTermConfig:
    strategy_id: str
    account_alias: str
    members: Mapping[str, str]


def load_tiger_long_term_config(path: Path) -> TigerLongTermConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    strategy_id = str(payload.get("strategy_id") or "")
    account_alias = str(payload.get("account_alias") or "")
    members = payload.get("members")
    if strategy_id != STRATEGY_ID or not account_alias or not isinstance(members, dict) or not members:
        raise ValueError("Tiger 长线策略配置无效")
    normalized = {str(symbol).strip().upper(): str(group).strip() for symbol, group in members.items()}
    if any(not symbol or not group for symbol, group in normalized.items()):
        raise ValueError("Tiger 长线策略池成员无效")
    return TigerLongTermConfig(strategy_id, account_alias, normalized)


def sma200_state(bars: Sequence[StrategyBar]) -> str:
    if len(bars) < 201:
        return "INELIGIBLE"
    sma200 = sum((bar.close for bar in bars[-201:-1]), Decimal("0")) / Decimal(200)
    return "LONG" if bars[-1].close > sma200 else "CASH"
```

Implement `allocate_target_weights` by assigning `min(0.10, 1 / long_count)` to `LONG` members, then proportionally scaling each risk group above `0.30`. Implement `rebalance_reasons` with precedence `state_change`, `symbol_cap`, `risk_group_cap`, then `drift` when absolute drift is strictly greater than `0.02`.

- [ ] **Step 5: Add allocation and tolerance tests**

```python
def test_allocation_scales_concentrated_risk_group():
    states = {symbol: "LONG" for symbol in ["DRAM", "SOXX", "EUV", "TSM", "QQQ"]}
    groups = {symbol: "semiconductor" for symbol in ["DRAM", "SOXX", "EUV", "TSM"]} | {"QQQ": "broad"}
    weights = allocate_target_weights(states, groups)
    assert sum(weights[symbol] for symbol in ["DRAM", "SOXX", "EUV", "TSM"]) == Decimal("0.30")
    assert weights["QQQ"] == Decimal("0.10")


def test_rebalance_ignores_two_point_drift_but_reports_larger_drift():
    assert rebalance_reasons({"QQQ": Decimal("0.08")}, {"QQQ": Decimal("0.10")}, {"QQQ": "LONG"}, {"QQQ": "LONG"}) == {}
    assert rebalance_reasons({"QQQ": Decimal("0.079")}, {"QQQ": Decimal("0.10")}, {"QQQ": "LONG"}, {"QQQ": "LONG"}) == {"QQQ": "drift"}
```

- [ ] **Step 6: Run focused tests, commit, and run acceptance**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term.py -q`

Expected: all tests pass.

Run: `git add config/tiger_long_term_strategy.json src/open_trader/tiger_long_term.py tests/test_tiger_long_term.py && git commit -m "feat: add Tiger long-term allocation rules"`

Run: `make acceptance`

Expected: `PASS` before starting Task 2.

---

### Task 2: QFQ Provenance, DGS3MO Cash, and Tiger Fees

**Files:**
- Modify: `src/open_trader/futu_quote.py`
- Create: `src/open_trader/tiger_long_term_backtest.py`
- Modify: `tests/test_futu_quote.py`
- Create: `tests/test_tiger_long_term_backtest.py`

**Interfaces:**
- Produces: `FutuQuoteClient.get_rehab_rows(symbol)`, `TigerUsFeeModel.fee(side, quantity, price)`, `load_dgs3mo_csv(path)`, `ensure_dgs3mo_rates(data_dir, end_date, *, opener)`, and `cash_growth(rate, calendar_days)`.
- Consumes: Futu `AuType.QFQ`, official fee constants, and FRED CSV rows.

- [ ] **Step 1: Write the failing explicit-QFQ provenance test**

Add a fake quote context that captures `request_history_kline` arguments and returns rehab rows. Assert `autype == AuType.QFQ` (or the string fallback in tests), and assert `get_rehab_rows("US.QQQ")` returns JSON-safe sorted mappings including ex-dividend data.

- [ ] **Step 2: Run the Futu test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_futu_quote.py -q`

Expected: failure because `autype` is absent and `get_rehab_rows` does not exist.

- [ ] **Step 3: Make QFQ explicit and expose rehab provenance**

In `get_daily_kline`, import `AuType` with `KLType`, set `autype = AuType.QFQ`, and pass it to `request_history_kline`. Add:

```python
def get_rehab_rows(self, futu_symbol: str) -> list[dict[str, str]]:
    ret_code, data = self.context.get_rehab(futu_symbol)
    if ret_code != 0:
        raise FutuQuoteError(str(data), error_type="snapshot_failed", next_step=SNAPSHOT_FAILED_NEXT_STEP,
                             opend_reachable=True, context_ok=True, snapshot_ok=False)
    return [
        {str(key): "" if value is None else str(value) for key, value in row.items()}
        for row in data.to_dict("records")
    ]
```

- [ ] **Step 4: Write RED tests for exact costs and cash accrual**

```python
from decimal import Decimal
from open_trader.tiger_long_term_backtest import TigerUsFeeModel, cash_growth, load_dgs3mo_csv


def test_tiger_fee_model_applies_minimums_and_sell_fees():
    model = TigerUsFeeModel()
    assert model.fee("BUY", Decimal("1"), Decimal("100")) == Decimal("1.99")
    assert model.fee("SELL", Decimal("1"), Decimal("100")) > model.fee("BUY", Decimal("1"), Decimal("100"))
    assert model.fee("BUY", Decimal("0.5"), Decimal("100")) == Decimal("0.50")


def test_dgs3mo_loader_does_not_backfill_from_the_future(tmp_path):
    path = tmp_path / "rates.csv"
    path.write_text("DATE,DGS3MO\n2026-01-02,4.00\n2026-01-05,.\n2026-01-06,3.90\n")
    rates = load_dgs3mo_csv(path)
    assert str(rates[next(iter(rates))]) == "4.00"
    assert cash_growth(Decimal("4"), 365) == Decimal("0.04")
```

- [ ] **Step 5: Run the new test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term_backtest.py -q`

Expected: collection fails because the backtest module does not exist.

- [ ] **Step 6: Implement fee and rate helpers**

Create immutable fee constants and calculate per-order commission, platform,
settlement, SEC, and FINRA fees with the caps/minimums in the design. Quantize
published rounded-to-cent fees with `ROUND_HALF_UP`. For quantity below one,
return `min(trade_value * Decimal("0.01"), Decimal("1"))`.

Parse FRED `DATE,DGS3MO` with `csv.DictReader`, skip `.` observations, reject
duplicates or non-finite/negative rates, and implement:

```python
def cash_growth(rate: Decimal, calendar_days: int) -> Decimal:
    return (Decimal("1") + rate / Decimal("100")) ** (
        Decimal(calendar_days) / Decimal("365")
    ) - Decimal("1")
```

`ensure_dgs3mo_rates` downloads the official FRED graph CSV into
`data/rates/DGS3MO.csv` with `urllib.request.urlopen`, writes atomically, and
returns parsed rates plus the file SHA-256. Reuse the cache only when it reaches
the requested end date; reject an HTTP error, empty response, or series that
cannot provide a rate on or before the evaluation start. Unit tests pass a fake
`opener` and never use network fixtures as live evidence.

- [ ] **Step 7: Run focused tests, commit, and run acceptance**

Run: `.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_tiger_long_term_backtest.py -q`

Expected: all tests pass.

Run: `git add src/open_trader/futu_quote.py src/open_trader/tiger_long_term_backtest.py tests/test_futu_quote.py tests/test_tiger_long_term_backtest.py && git commit -m "feat: add Tiger strategy market assumptions"`

Run: `make acceptance`

Expected: `PASS` before starting Task 3.

---

### Task 3: Multi-Asset Simulation, Metrics, Segments, and Gate

**Files:**
- Modify: `src/open_trader/tiger_long_term_backtest.py`
- Modify: `tests/test_tiger_long_term_backtest.py`

**Interfaces:**
- Produces: `run_tiger_long_term_backtest(bars_by_symbol, risk_groups, rates, *, initial_cash) -> dict[str, object]` and `build_validation_gate(strategy, benchmark, *, cash_annualized_return_pct, provenance_ok) -> dict[str, object]`.
- Consumes: Task 1 allocation rules, Task 2 costs/rates, and `StrategyBar` sequences with one warm-up plus five evaluation years.

- [ ] **Step 1: Write a deterministic RED lifecycle test**

Construct two six-year daily fixtures: `QQQ` stays above its SMA200, while
`SOXX` crosses below and later above. Assert orders occur only at the following
open, cash accrues between valuation dates, weights never exceed symbol/group
caps, and the always-long benchmark never emits an SMA exit.

```python
def test_portfolio_backtest_uses_next_open_and_respects_caps():
    result = run_tiger_long_term_backtest(
        bars_by_symbol={"QQQ": qqq_fixture(), "SOXX": soxx_fixture()},
        risk_groups={"QQQ": "broad", "SOXX": "semiconductor"},
        rates=constant_rates("4.0"),
        initial_cash=Decimal("100000"),
    )
    assert result["strategy"]["orders"][0]["decision_date"] < result["strategy"]["orders"][0]["execution_date"]
    assert max(Decimal(row["weight"]) for row in result["strategy"]["member_weights"]) <= Decimal("0.10")
    assert all(order["reason"] != "sma200_exit" for order in result["benchmark"]["orders"])
```

- [ ] **Step 2: Run the lifecycle test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term_backtest.py::test_portfolio_backtest_uses_next_open_and_respects_caps -q`

Expected: failure because `run_tiger_long_term_backtest` is missing.

- [ ] **Step 3: Implement the smallest chronological simulator**

Use the union of evaluation sessions, maintain `cash` and decimal quantities,
value positions using the latest close on or before a session, queue targets at
close, and execute queued deltas at the next available open for that member.
Apply fees/slippage per order, risk-free growth across calendar gaps, and no
future price/rate lookup. Run the same loop with signal states forced to
`LONG` for the primary benchmark. Store orders, daily equity, member weights,
cash, turnover, time in market, round trips, and profit contributions.

- [ ] **Step 4: Write RED tests for metrics, ten segments, and gate reasons**

```python
def test_gate_requires_risk_adjusted_floors_and_calibration():
    gate = build_validation_gate(
        strategy={"sharpe_ratio": "1.1", "calmar_ratio": "1.0", "annualized_return_pct": "7", "max_drawdown_pct": "8"},
        benchmark={"sharpe_ratio": "0.9", "calmar_ratio": "0.8", "annualized_return_pct": "8", "max_drawdown_pct": "10"},
        cash_annualized_return_pct=Decimal("4"),
        provenance_ok=True,
    )
    assert gate == {"passed": False, "policy_id": "tiger_risk_adjusted/v1", "reasons": ["calibration_required"]}


def test_gate_reports_every_fixed_failure():
    gate = build_validation_gate(
        strategy={"sharpe_ratio": "0.7", "calmar_ratio": "0.6", "annualized_return_pct": "3", "max_drawdown_pct": "12"},
        benchmark={"sharpe_ratio": "0.9", "calmar_ratio": "0.8", "annualized_return_pct": "8", "max_drawdown_pct": "10"},
        cash_annualized_return_pct=Decimal("4"),
        provenance_ok=False,
    )
    assert gate["reasons"] == ["sharpe_below_floor", "sharpe_below_benchmark", "calmar_below_floor", "calmar_below_benchmark", "return_below_cash", "drawdown_above_benchmark", "provenance_incomplete", "calibration_required"]
```

- [ ] **Step 5: Implement metrics and gate**

Reuse the existing annualized return, drawdown, and daily-return formulas where
their behavior matches, but calculate Sharpe from daily portfolio return minus
the matching daily cash return. Calculate Calmar as annualized return divided
by positive maximum drawdown. Partition the five-year evaluation range into ten
chronological six-month diagnostic segments without requiring each to pass.
Return `None` ratios only when mathematically undefined and emit the matching
structured gate reason.

- [ ] **Step 6: Run focused tests, commit, and run acceptance**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term.py tests/test_tiger_long_term_backtest.py -q`

Expected: all tests pass.

Run: `git add src/open_trader/tiger_long_term_backtest.py tests/test_tiger_long_term_backtest.py && git commit -m "feat: simulate Tiger long-term portfolio"`

Run: `make acceptance`

Expected: `PASS` before starting Task 4.

---

### Task 4: Real Tiger Snapshot, Immutable Artifact, CLI, and Daily Shadow Run

**Files:**
- Modify: `src/open_trader/tiger_long_term.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_tiger_long_term.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_daily_premarket.py`

**Interfaces:**
- Produces: `generate_tiger_long_term_strategy(run_date, data_dir, config_path, price_provider, *, update_latest) -> TigerLongTermResult` and CLI command `run-tiger-long-term-strategy`.
- Consumes: `data/runs/<date>/tiger_account_snapshot.json`, Tasks 1–3, FutuQuoteClient, and FRED cache/fetch path.

- [ ] **Step 1: Write RED tests for account isolation and atomic publication**

Create a Tiger snapshot containing `tiger_5683` account-total and position rows,
plus unrelated fake Futu/Phillips data in `portfolio.csv`. Assert NAV and actual
weights come only from the Tiger snapshot, the run artifact is always written,
latest updates only after complete validation, and missing account-total fails
without replacing the old latest artifact.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term.py -q`

Expected: failure because `generate_tiger_long_term_strategy` is missing.

- [ ] **Step 3: Implement generation and strict artifact validation**

Add schema `open_trader.tiger_long_term_strategy.v1`, atomic JSON publication,
strict loading/validation, source hashes, member eligibility, daily target and
actual weights, rebalance reasons, backtest/benchmark/SPY metrics, ten segments,
diagnostics, and gate. Publish status `shadow` with
`gate.reasons=[..., "calibration_required"]`; never emit an order request.

Define the result type used by the CLI and daily workflow:

```python
@dataclass(frozen=True)
class TigerLongTermResult:
    status: str
    member_count: int
    eligible_count: int
    run_path: Path
    latest_path: Path | None
```

Store a validation hash over strategy/config versions, all price and rehab
hashes, DGS3MO hash, and cost-model version. Reuse a validation only when it is
from the same calendar month and the full hash matches; always recalculate live
actual/target weights. Add tests proving a matching hash reuses metrics and a
changed price/config/rate/cost hash forces a new backtest.

- [ ] **Step 4: Add and test the CLI command**

Parser contract:

```text
open-trader run-tiger-long-term-strategy \
  --date YYYY-MM-DD \
  --config config/tiger_long_term_strategy.json \
  --data-dir data \
  [--host 127.0.0.1] \
  [--port 11111] \
  [--dry-run]
```

The command constructs `FutuQuoteClient`, runs generation, closes the client in
`finally`, prints status/run/latest paths and member counts, and returns nonzero
on a generation failure. Add parser and dispatch tests in `test_pipeline.py`.

- [ ] **Step 5: Integrate only the US daily workflow**

Inject `tiger_long_term_generator` into `DailyPremarketRunner`. After live
portfolio refresh and before decision-plan generation, call it only for
`market == "US"`; add its run/latest paths and status to the daily artifact and
report dictionaries. A strategy failure blocks only the Tiger strategy status,
not unrelated TradingAgents artifacts, but must be visible in daily readiness
reasons. Dry-run writes the run artifact and does not promote latest.

- [ ] **Step 6: Run focused workflow tests, commit, and run acceptance**

Run: `.venv/bin/python -m pytest tests/test_tiger_long_term.py tests/test_pipeline.py tests/test_daily_premarket.py -q`

Expected: all tests pass.

Run: `git add src/open_trader/tiger_long_term.py src/open_trader/cli.py src/open_trader/daily_premarket.py tests/test_tiger_long_term.py tests/test_pipeline.py tests/test_daily_premarket.py && git commit -m "feat: publish Tiger long-term shadow strategy"`

Run: `make acceptance`

Expected: `PASS` before Task 5 modifies Dashboard behavior further.

---

### Task 5: Dashboard State, Panel, and Acceptance Coverage

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Produces: top-level Dashboard payload key `tiger_long_term_strategy` and one Tiger long-term panel.
- Consumes: strict latest artifact loader from Task 4.

- [ ] **Step 1: Write RED Dashboard-state tests**

Write a valid latest strategy artifact and assert `load_dashboard_state(...).to_dict()` exposes it unchanged under `tiger_long_term_strategy`. Assert missing and invalid artifacts return `{available: false, error: ...}` without breaking other Dashboard data.

- [ ] **Step 2: Run state tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -q`

Expected: failure because the top-level key is absent.

- [ ] **Step 3: Load the artifact into Dashboard state**

Add `tiger_long_term_strategy: dict[str, Any]` to `DashboardState`, load
`data/latest/US/tiger_long_term_strategy.json` through its strict loader, and
serialize it in `to_dict()`.

- [ ] **Step 4: Write RED browser-rendering tests**

Use the existing Node-backed HTML tests to require these visible strings:

```text
老虎长线组合
影子验证
SMA200
夏普比率
卡玛比率
同池永久持有
条件验证，不含选股
风险组上限 30%
仅供人工复核
```

Also assert member rows include symbol, risk group, trend, actual/target weight,
drift, eligibility reason, and rebalance reason on desktop and mobile markup.

- [ ] **Step 5: Add the smallest panel**

Add one section in the existing decision area, render top-line gate status and
strategy/benchmark/SPY metrics, then a responsive member table/cards. Reuse
existing status pills, metric cards, percent formatting, and mobile breakpoints;
add only selectors unique to the panel. Do not add charts, tabs, editing, or
order buttons.

- [ ] **Step 6: Extend acceptance assertions**

Require the live payload to have a valid Tiger strategy object and browser text
to include `老虎长线组合`, `夏普比率`, `卡玛比率`, and
`calibration_required`. Treat a missing browser environment as `BLOCKED` under
the existing acceptance semantics.

- [ ] **Step 7: Run Dashboard tests, commit, and run acceptance**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`

Expected: all tests pass.

Run: `git add src/open_trader/dashboard.py src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css src/open_trader/dashboard_acceptance.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py && git commit -m "feat: show Tiger long-term strategy dashboard"`

Run: `make acceptance`

Expected: `PASS`; Task 6 repeats the gate only after the real shadow workflow
has produced current external-data evidence.

---

### Task 6: Full Verification, Real Shadow Run, Acceptance, and Exact-SHA Deployment

**Files:**
- Modify only if verification exposes a defect; every fix begins with a failing regression test.

**Interfaces:**
- Consumes: all prior tasks and the project acceptance contract.
- Produces: accepted and deployed exact Git SHA plus review URL.

- [ ] **Step 1: Run the focused strategy and Dashboard suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_tiger_long_term.py \
  tests/test_tiger_long_term_backtest.py \
  tests/test_futu_quote.py \
  tests/test_pipeline.py \
  tests/test_daily_premarket.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all tests pass with zero warnings/errors.

- [ ] **Step 2: Run the full automated suite**

Run: `make test`

Expected: pytest exits 0 with zero failures.

- [ ] **Step 3: Run the real Tiger shadow workflow**

Run:

```bash
.venv/bin/open-trader run-tiger-long-term-strategy \
  --date 2026-07-14 \
  --config config/tiger_long_term_strategy.json \
  --data-dir data \
  --host 127.0.0.1 \
  --port 11111
```

Verify the real output uses `tiger_5683`, contains all eight manual members,
never includes Futu/Phillips NAV, records QFQ/rehab/DGS3MO hashes, reports
ten segment diagnostics when data permits, remains shadow with
`calibration_required`, and writes no broker order.

- [ ] **Step 4: Commit any generated source/config corrections, then record SHA**

Run: `git status --short && git rev-parse HEAD`

Do not commit runtime `data/`, reports, logs, user files, or the pre-existing
untracked `CONTEXT.md` and 2026-07-13 documents.

- [ ] **Step 5: Inspect and stop old Dashboard processes**

Run:

```bash
ps aux | rg 'open_trader.dashboard_web|open-trader dashboard|8766'
screen -ls
launchctl list | rg 'open-trader|dashboard'
```

Restart any process using pre-change code through its existing service command.

- [ ] **Step 6: Run the only completion gate**

Run: `make acceptance`

Expected: explicit `PASS`. On `FAIL`, add a failing regression test, fix, and
rerun. On `BLOCKED`, report the blocker and do not substitute another check.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Restart the Dashboard without changing source/data, then verify the new PID,
working directory `/Users/ray/projects/open_trader`, exact accepted Git SHA,
fresh log timestamp/content, and HTTP 200 from `http://127.0.0.1:8766`.

- [ ] **Step 8: Report the review URL**

Only after Steps 6–7 succeed, provide `http://127.0.0.1:8766` for review and
state the focused/full test counts, acceptance `PASS`, deployed SHA, and PID.
