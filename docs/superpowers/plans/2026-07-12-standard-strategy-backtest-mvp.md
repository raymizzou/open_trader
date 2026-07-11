# Standard Strategy Backtest MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-holding Trading Plan backtests with one global, on-demand, single-symbol backtest workflow for three versioned daily-swing strategies, with buy-and-hold and market-index comparisons.

**Architecture:** Keep the existing Trading Plan CLI compatible, but add a separate standard-strategy domain and orchestration service. Strategy rules consume point-in-time OHLCV bars and emit target-weight signals; a hidden Backtrader adapter executes those signals; the dashboard API coordinates prices, benchmarks, immutable artifacts, and a dedicated workspace opened from one homepage action.

**Tech Stack:** Python 3.11+, dataclasses, `Decimal`, Backtrader, existing Futu daily-K provider, `http.server` dashboard, vanilla JavaScript/CSS, pytest, Playwright/browser verification.

## Global Constraints

- Each run covers exactly one symbol and one preset strategy.
- Preset strategy identifiers are `trend_pullback/v1`, `breakout_momentum/v1`, and `range_mean_reversion/v1`.
- Historical actions are `BUY`, `ADD`, `HOLD`, `REDUCE`, and `EXIT`; action precedence is `EXIT`, `REDUCE`, `ADD`, `BUY`, then `HOLD`.
- `BUY` targets 50% of `max_strategy_weight`, `ADD` targets 100%, `REDUCE` targets 50%, and `EXIT` targets 0%.
- Decisions based on day `T` data execute no earlier than the next available session.
- Quick ranges are 6 months, 1 year, 3 years, and 5 years; the default end is the latest available trading day.
- Benchmarks are same-symbol buy-and-hold plus SPY for US or `HK.02800` for HK, using the same effective range, capital, costs, and allocated notional.
- Backtrader is the only MVP adapter and is not exposed as a user-selectable option.
- Do not add market-regime detection, strategy recommendations, parameter search, custom strategy editing, scheduling, notifications, or live orders.
- The dashboard has one global `策略回测` entry; holding rows have no backtest action and the homepage has no backtest-readiness filters.
- All user-facing labels, explanations, and errors are Chinese.
- Preserve the existing `run-backtest` Trading Plan CLI unless a focused compatibility test proves a deliberate change is required.

---

## File Structure

- Create `src/open_trader/standard_strategies.py`: strategy catalog, OHLCV input model, indicators, fixed v1 rules, and normalized target-weight signals.
- Create `src/open_trader/strategy_backtest.py`: date-range resolution, hidden Backtrader adapter, benchmark execution, metrics, manifests, and normalized artifacts.
- Modify `src/open_trader/backtest_prices.py`: range coverage checks and reusable price loading without changing existing fetch behavior.
- Modify `src/open_trader/dashboard_web.py`: options and run endpoints; remove automatic per-holding price backfill from normal dashboard loading.
- Modify `src/open_trader/dashboard.py`: expose backtest symbol choices from holdings and `data/latest/watchlist.csv`; stop attaching Trading Plan readiness/results to every holding.
- Modify `src/open_trader/dashboard_static/index.html`: one header entry and dedicated backtest workspace; remove homepage readiness controls.
- Modify `src/open_trader/dashboard_static/dashboard.js`: global workspace state, form submission, result rendering, and removal of row-level backtest behavior.
- Modify `src/open_trader/dashboard_static/dashboard.css`: strategy cards, form, comparison metrics, charts, trades, responsive layout; remove obsolete row-level backtest styling when unused.
- Create `tests/test_standard_strategies.py`: deterministic rule and no-lookahead tests.
- Create `tests/test_strategy_backtest.py`: adapter, range, benchmark, manifest, and error tests.
- Modify `tests/test_backtest_prices.py`, `tests/test_dashboard.py`, and `tests/test_dashboard_web.py`: price coverage, universe/API behavior, and browser-facing static contracts.
- Modify `README.md`, `README.zh-CN.md`, and `CHANGELOG.md`: operator workflow, fixed strategies, scope, and verification.

---

### Task 1: Versioned Standard Strategy Catalog and Signals

**Files:**
- Create: `src/open_trader/standard_strategies.py`
- Create: `tests/test_standard_strategies.py`

**Interfaces:**
- Consumes: daily `StrategyBar` rows ordered by date and `max_strategy_weight: Decimal`.
- Produces: `strategy_catalog() -> tuple[StrategyDefinition, StrategyDefinition, StrategyDefinition]` and `generate_strategy_signals(strategy_id: str, bars: Sequence[StrategyBar], *, start_date: date, max_strategy_weight: Decimal) -> list[StrategySignal]`.
- `StrategySignal` fields are `decision_date`, `earliest_execution_date`, `action`, `target_weight`, `rule`, `explanation`, and `data_cutoff`.

- [ ] **Step 1: Write catalog and action-contract tests**

```python
from datetime import date
from decimal import Decimal

from open_trader.standard_strategies import ACTION_TARGET_FRACTIONS, strategy_catalog


def test_strategy_catalog_has_three_fixed_v1_entries() -> None:
    assert [item.strategy_id for item in strategy_catalog()] == [
        "trend_pullback/v1",
        "breakout_momentum/v1",
        "range_mean_reversion/v1",
    ]
    assert [item.name_zh for item in strategy_catalog()] == [
        "趋势回调",
        "突破动量",
        "区间均值回归",
    ]


def test_action_target_fractions_are_stable() -> None:
    assert ACTION_TARGET_FRACTIONS == {
        "BUY": Decimal("0.5"),
        "ADD": Decimal("1"),
        "HOLD": None,
        "REDUCE": Decimal("0.5"),
        "EXIT": Decimal("0"),
    }
```

- [ ] **Step 2: Run catalog tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_standard_strategies.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'open_trader.standard_strategies'`.

- [ ] **Step 3: Add the public domain models and catalog**

```python
@dataclass(frozen=True)
class StrategyBar:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    name_zh: str
    description_zh: str
    parameters: Mapping[str, Decimal | int]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.strategy_id,
            "name_zh": self.name_zh,
            "description_zh": self.description_zh,
            "parameters": {key: str(value) for key, value in self.parameters.items()},
        }


@dataclass(frozen=True)
class StrategySignal:
    decision_date: date
    earliest_execution_date: date | None
    action: Literal["BUY", "ADD", "HOLD", "REDUCE", "EXIT"]
    target_weight: Decimal | None
    rule: str
    explanation: str
    data_cutoff: date


ACTION_TARGET_FRACTIONS = {
    "BUY": Decimal("0.5"),
    "ADD": Decimal("1"),
    "HOLD": None,
    "REDUCE": Decimal("0.5"),
    "EXIT": Decimal("0"),
}
```

Define the three catalog entries with the exact v1 values from the spec: SMA20,
SMA50, SMA10, ATR14, RSI14, 20-session high/volume, volume multiplier `1.5`,
Bollinger period `20`, standard-deviation multiplier `2`, and stop multiplier
`2`.

- [ ] **Step 4: Add deterministic fixtures for every non-HOLD action**

Use helper bars that make the intended condition unambiguous and assert action
order and target weights. Each fixture must first have enough warm-up bars.

```python
@pytest.mark.parametrize(
    ("strategy_id", "expected_actions"),
    [
        ("trend_pullback/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
        ("breakout_momentum/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
        ("range_mean_reversion/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
    ],
)
def test_strategy_fixture_covers_position_lifecycle(
    strategy_id: str,
    expected_actions: list[str],
) -> None:
    signals = generate_strategy_signals(
        strategy_id,
        lifecycle_fixture(strategy_id),
        start_date=date(2025, 4, 1),
        max_strategy_weight=Decimal("0.10"),
    )
    actions = [signal.action for signal in signals if signal.action != "HOLD"]
    assert actions == expected_actions
    assert [signal.target_weight for signal in signals if signal.action != "HOLD"] == [
        Decimal("0.05"), Decimal("0.10"), Decimal("0.05"), Decimal("0"),
    ]
```

- [ ] **Step 5: Implement indicators and the three stateful v1 evaluators**

Implement small private helpers `_sma`, `_atr`, `_rsi`, `_bollinger`, and
`_prior_high`; calculate each row only from `bars[: index + 1]`. Maintain only
the state required by the spec: current target weight, entry price, active stop,
and breakout level. Evaluate all candidate actions and select by:

```python
ACTION_PRECEDENCE = {"EXIT": 0, "REDUCE": 1, "ADD": 2, "BUY": 3, "HOLD": 4}
action = min(candidates, key=lambda item: ACTION_PRECEDENCE[item.action])
```

Set `earliest_execution_date` to the next bar's date and `None` for the final
bar. Suppress trades before `start_date`, while still using earlier bars for
indicator warm-up.

- [ ] **Step 6: Add explicit no-lookahead and warm-up tests**

```python
def test_appending_future_bar_does_not_change_prior_decisions() -> None:
    bars = lifecycle_fixture("trend_pullback/v1")
    original = generate_strategy_signals(
        "trend_pullback/v1", bars, start_date=date(2025, 4, 1),
        max_strategy_weight=Decimal("0.10"),
    )
    extended = generate_strategy_signals(
        "trend_pullback/v1", [*bars, future_shock_bar()],
        start_date=date(2025, 4, 1), max_strategy_weight=Decimal("0.10"),
    )
    assert extended[: len(original)] == original


def test_warmup_bars_never_emit_trade_actions() -> None:
    start = date(2025, 4, 1)
    signals = generate_strategy_signals(
        "breakout_momentum/v1", lifecycle_fixture("breakout_momentum/v1"),
        start_date=start, max_strategy_weight=Decimal("0.10"),
    )
    assert all(signal.action == "HOLD" for signal in signals if signal.decision_date < start)
```

- [ ] **Step 7: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_standard_strategies.py -q`

Expected: all tests PASS.

```bash
git add src/open_trader/standard_strategies.py tests/test_standard_strategies.py
git commit -m "feat: add standard backtest strategies"
```

---

### Task 2: Historical Range and Price Coverage

**Files:**
- Modify: `src/open_trader/backtest_prices.py`
- Modify: `tests/test_backtest_prices.py`

**Interfaces:**
- Consumes: market, symbol, quick range or custom dates, and a daily-K provider.
- Produces: `resolve_backtest_range(*, preset: str | None, custom_start: date | None, custom_end: date | None, latest_available: date) -> BacktestDateRange`, `load_price_rows(path: Path) -> list[StrategyBar]`, and `ensure_backtest_price_range(*, data_dir: Path, market: str, symbol: str, date_range: BacktestDateRange, provider: DailyKlineProvider) -> BacktestPriceRangeResult`.
- Later tasks rely on `requested_start`, `requested_end`, `actual_start`, `actual_end`, `warmup_start`, `prices_path`, and `source_hash`.

- [ ] **Step 1: Write quick-range and actual-range tests**

```python
def test_resolve_three_year_range_ends_on_latest_available_date() -> None:
    result = resolve_backtest_range(
        preset="3Y", custom_start=None, custom_end=None,
        latest_available=date(2026, 7, 10),
    )
    assert result.requested_start == date(2023, 7, 10)
    assert result.requested_end == date(2026, 7, 10)
    assert result.warmup_start == date(2023, 4, 1)


def test_load_price_rows_reports_actual_available_range(tmp_path: Path) -> None:
    path = write_prices(tmp_path, first="2024-01-02", last="2026-07-10")
    rows = load_price_rows(path)
    assert rows[0].date == date(2024, 1, 2)
    assert rows[-1].date == date(2026, 7, 10)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_backtest_prices.py -q`

Expected: FAIL because `resolve_backtest_range` and `load_price_rows` do not exist.

- [ ] **Step 3: Implement exact range parsing and validation**

```python
@dataclass(frozen=True)
class BacktestDateRange:
    requested_start: date
    requested_end: date
    warmup_start: date


@dataclass(frozen=True)
class BacktestPriceRangeResult:
    market: str
    symbol: str
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    warmup_start: date
    prices_path: Path
    source_hash: str
    bars: Sequence[StrategyBar]


PRESET_MONTHS = {"6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}


def resolve_backtest_range(*, preset: str | None, custom_start: date | None,
                           custom_end: date | None, latest_available: date) -> BacktestDateRange:
    end = custom_end or latest_available
    if end > latest_available:
        end = latest_available
    start = custom_start if custom_start else subtract_months(end, PRESET_MONTHS[preset or "1Y"])
    if start >= end:
        raise ValueError("回测开始日期必须早于结束日期")
    return BacktestDateRange(start, end, start - timedelta(days=100))
```

Use calendar-safe month subtraction rather than `days = months * 30`. Parse CSV
columns `date,open,high,low,close,volume`; reject missing, duplicate, unsorted,
or invalid rows with Chinese errors.

- [ ] **Step 4: Add coverage/fetch tests**

```python
def test_ensure_price_range_fetches_when_file_does_not_cover_warmup(
    tmp_path: Path, fake_provider: FakeDailyKlineProvider,
) -> None:
    result = ensure_backtest_price_range(
        data_dir=tmp_path, market="US", symbol="MSFT",
        date_range=BacktestDateRange(date(2025, 1, 1), date(2026, 1, 1), date(2024, 9, 23)),
        provider=fake_provider,
    )
    assert fake_provider.requests == [("US.MSFT", "2024-09-23", "2026-01-01")]
    assert result.actual_start <= date(2025, 1, 1)
    assert result.actual_end == date(2026, 1, 1)
    assert len(result.source_hash) == 64
```

- [ ] **Step 5: Implement coverage reuse and atomic refresh**

Reuse an existing CSV only when its first date is on or before `warmup_start` and
its final date is on or after `requested_end`. Otherwise call the existing
`fetch_backtest_prices`, reload the atomic CSV, calculate SHA-256 from bytes, and
return the actual in-range dates. Do not add automatic fetches to normal
dashboard page loads.

For a default/latest end, use `date.today()` only as the provisional fetch end.
After loading the returned bars, set `latest_available` to the final bar date,
recompute the preset start from that date, and perform one additional fetch only
if the recomputed warm-up start is earlier than the fetched coverage. For a
custom end, clamp it to the final available bar and expose both the requested and
actual end dates rather than silently presenting them as equal.

- [ ] **Step 6: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_backtest_prices.py -q`

Expected: all tests PASS.

```bash
git add src/open_trader/backtest_prices.py tests/test_backtest_prices.py
git commit -m "feat: resolve standard backtest price ranges"
```

---

### Task 3: Backtrader Execution, Benchmarks, and Immutable Artifacts

**Files:**
- Create: `src/open_trader/strategy_backtest.py`
- Create: `tests/test_strategy_backtest.py`
- Modify: `pyproject.toml` only if the existing Backtrader dependency declaration is incomplete.

**Interfaces:**
- Consumes: `StandardBacktestRequest`, three covered OHLCV series, and
  `generate_strategy_signals`.
- Produces: `run_standard_backtest(request, *, price_provider) -> StandardBacktestResult`.
- The result includes strategy metrics, buy-and-hold metrics, market-benchmark metrics, excess returns, normalized signals/trades/equity curves, requested/actual ranges, and artifact paths.

- [ ] **Step 1: Write request validation and next-session execution tests**

```python
def test_standard_backtest_executes_signal_at_next_session_open(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    )
    buy = next(trade for trade in result.trades if trade.action == "BUY")
    assert buy.decision_date == "2025-02-10"
    assert buy.execution_date == "2025-02-11"
    assert buy.raw_price == Decimal("105")
    assert buy.execution_price == Decimal("105.0525")  # 5 bps slippage


def test_invalid_max_weight_is_rejected_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="最大策略仓位必须大于 0 且不超过 100%"):
        run_standard_backtest(
            replace(standard_request(tmp_path), max_strategy_weight=Decimal("1.1")),
            price_provider=fixture_provider("basic"),
        )
```

- [ ] **Step 2: Run the new test file and verify failure**

Run: `.venv/bin/python -m pytest tests/test_strategy_backtest.py -q`

Expected: FAIL during collection because `open_trader.strategy_backtest` does not exist.

- [ ] **Step 3: Define stable request/result and adapter protocols**

```python
@dataclass(frozen=True)
class StandardBacktestRequest:
    data_dir: Path
    reports_dir: Path
    market: str
    symbol: str
    strategy_id: str
    range_preset: str | None
    custom_start: date | None
    custom_end: date | None
    initial_cash: Decimal
    max_strategy_weight: Decimal
    commission_bps: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True)
class ExecutionResult:
    trades: Sequence[NormalizedTrade]
    equity_curve: Sequence[dict[str, str]]
    final_equity: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal
    max_drawdown_pct: Decimal
    win_rate_pct: Decimal


@dataclass(frozen=True)
class StandardBacktestResult:
    run_id: str
    status: str
    message_zh: str
    strategy_id: str
    benchmark_symbol: str
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    strategy: ExecutionResult
    buy_hold: ExecutionResult
    market_benchmark: ExecutionResult
    strategy_excess_return_pct: Decimal
    market_excess_return_pct: Decimal
    adapter_version: str
    manifest_path: Path
    signals_path: Path
    trades_path: Path
    equity_curve_path: Path

    def to_dict(self) -> dict[str, object]:
        return serialize_standard_backtest_result(self)


class StrategyExecutionAdapter(Protocol):
    name: str
    version: str

    def run(self, *, bars: Sequence[StrategyBar], signals: Sequence[StrategySignal],
            initial_cash: Decimal, commission_bps: Decimal,
            slippage_bps: Decimal) -> ExecutionResult:
        raise NotImplementedError
```

Define `NormalizedTrade` immediately before `ExecutionResult`, with string-safe
fields for `decision_date`, `execution_date`, `action`, `quantity`, `raw_price`,
`execution_price`, `fees`, and `reason`. Implement
`serialize_standard_backtest_result` in the same module; convert `date`,
`Decimal`, tuple, and `Path` values to JSON-safe strings/lists without changing
field names. The orchestration entry point has the exact signature:

```python
def run_standard_backtest(
    request: StandardBacktestRequest,
    *,
    price_provider: DailyKlineProvider,
) -> StandardBacktestResult:
    validate_standard_backtest_request(request)
    return StandardBacktestService(price_provider=price_provider).run(request)
```

Implement `BacktraderTargetWeightAdapter` as the only selected adapter. Convert
each signal target to a next-open order. Apply commission and slippage once,
record rejected/zero-quantity orders, and keep strategy code free of Backtrader
imports.

- [ ] **Step 4: Write benchmark fairness tests**

```python
def test_strategy_and_benchmarks_share_capital_range_and_notional(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path, market="US", symbol="MSFT", max_weight="0.10"),
        price_provider=fixture_provider("strategy_and_spy"),
    )
    assert result.benchmark_symbol == "SPY"
    assert result.actual_start == result.buy_hold.actual_start == result.market_benchmark.actual_start
    assert result.actual_end == result.buy_hold.actual_end == result.market_benchmark.actual_end
    assert result.strategy.initial_allocated_notional == Decimal("10000")
    assert result.buy_hold.initial_allocated_notional == Decimal("10000")
    assert result.market_benchmark.initial_allocated_notional == Decimal("10000")
    assert result.strategy_excess_return_pct == (
        result.strategy.total_return_pct - result.buy_hold.total_return_pct
    )
```

- [ ] **Step 5: Implement buy-and-hold and market benchmarks**

Map markets with `BENCHMARK_SYMBOLS = {"US": "SPY", "HK": "02800"}`. Execute
both benchmarks at the first in-range next-session open using
`initial_cash * max_strategy_weight`, apply the same costs, preserve unallocated
cash, and liquidate at the last in-range close with exit costs. Intersect all
three datasets to a common effective start/end before calculating metrics.

- [ ] **Step 6: Write immutable manifest and zero-trade tests**

```python
def test_run_writes_reproducible_manifest_and_normalized_artifacts(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path), price_provider=fixture_provider("basic"),
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["strategy"]["id"] == "trend_pullback/v1"
    assert manifest["adapter"] == {"name": "backtrader", "version": result.adapter_version}
    assert manifest["sources"]["symbol"]["sha256"]
    assert manifest["requested_range"] == {"start": "2025-01-01", "end": "2026-01-01"}
    assert result.signals_path.exists()
    assert result.trades_path.exists()
    assert result.equity_curve_path.exists()


def test_zero_trade_run_is_successful(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path), price_provider=fixture_provider("never_triggers"),
    )
    assert result.status == "ok"
    assert result.trade_count == 0
    assert result.message_zh == "所选区间内没有触发交易"
```

- [ ] **Step 7: Implement atomic result storage**

Use run IDs containing UTC timestamp, market, normalized symbol, strategy slug,
and an eight-character request hash. Write under
`data/backtests/<run_id>/manifest.json`, `signals.csv`, `trades.csv`,
`equity_curve.csv`, `buy_hold_equity.csv`, `market_benchmark_equity.csv`, and
`metrics.json`; write the Chinese Markdown report under
`reports/backtests/<run_id>.md`. Use temporary sibling files and `Path.replace`.

- [ ] **Step 8: Run focused and compatibility tests, then commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_standard_strategies.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_backtest.py tests/test_backtest_cli.py -q
```

Expected: all tests PASS, including the pre-existing Trading Plan tests.

```bash
git add src/open_trader/strategy_backtest.py tests/test_strategy_backtest.py pyproject.toml
git commit -m "feat: run standard strategy backtests"
```

---

### Task 4: Dashboard Symbol Universe and Backtest APIs

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Produces `backtest_universe` with concrete `holdings: list[dict[str, str]]` and `watchlist: list[dict[str, str]]` values in dashboard/options data.
- Adds `GET /api/backtests/options` and `POST /api/backtests/standard/run`.
- Removes `backtest_readiness` and `backtest` from each holding payload and stops `auto_fetch_backtest_prices=True` on `GET /api/dashboard`.

- [ ] **Step 1: Write universe tests for holdings and watchlist deduplication**

```python
def test_dashboard_backtest_universe_combines_holdings_and_watchlist(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path, [("US", "MSFT"), ("HK", "00700")])
    write_watchlist(config.data_dir / "latest/watchlist.csv", [
        ("US", "MSFT"), ("US", "NVDA"), ("HK", "00700"),
    ])
    payload = load_dashboard_state(config).to_dict()
    assert [(row["market"], row["symbol"]) for row in payload["backtest_universe"]["holdings"]] == [
        ("US", "MSFT"), ("HK", "00700"),
    ]
    assert [(row["market"], row["symbol"]) for row in payload["backtest_universe"]["watchlist"]] == [
        ("US", "NVDA"),
    ]
```

- [ ] **Step 2: Run the focused dashboard test and verify failure**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_backtest_universe_combines_holdings_and_watchlist -q`

Expected: FAIL with missing `backtest_universe`.

- [ ] **Step 3: Implement the normalized symbol universe**

Read existing holding rows plus `data/latest/watchlist.csv`; accept the existing
watchlist `market` and `symbol` fields; normalize HK symbols to five digits for
Futu requests but display the repository's existing symbol label. Remove
watchlist duplicates already present in holdings. Return only valid HK/US equity
symbols; exclude cash and options in this MVP because the v1 strategies require
daily equity OHLCV and benchmark comparison.

- [ ] **Step 4: Write API option and run tests**

```python
def test_backtest_options_api_exposes_fixed_catalog_and_defaults(live_dashboard) -> None:
    payload = get_json(live_dashboard.url + "/api/backtests/options")
    assert [item["id"] for item in payload["strategies"]] == [
        "trend_pullback/v1", "breakout_momentum/v1", "range_mean_reversion/v1",
    ]
    assert payload["ranges"] == ["6M", "1Y", "3Y", "5Y", "CUSTOM"]
    assert payload["defaults"] == {
        "range": "1Y", "initial_cash": "100000", "max_strategy_weight": "0.10",
        "commission_bps": "10", "slippage_bps": "5",
    }


def test_standard_backtest_run_api_does_not_accept_adapter_choice(live_dashboard) -> None:
    response = post_json(live_dashboard.url + "/api/backtests/standard/run", {
        "market": "US", "symbol": "MSFT", "strategy_id": "trend_pullback/v1",
        "range_preset": "1Y", "adapter": "simple",
    })
    assert response.status == 400
    assert response.json()["message"] == "不支持从界面选择回测执行工具"
```

- [ ] **Step 5: Implement API builders and routes**

Add:

```python
def build_standard_backtest_options_payload(config: DashboardConfig) -> dict[str, Any]:
    state = load_dashboard_state(config).to_dict()
    return {
        "strategies": [definition.to_dict() for definition in strategy_catalog()],
        "ranges": ["6M", "1Y", "3Y", "5Y", "CUSTOM"],
        "defaults": {
            "range": "1Y",
            "initial_cash": "100000",
            "max_strategy_weight": "0.10",
            "commission_bps": "10",
            "slippage_bps": "5",
        },
        "universe": state["backtest_universe"],
        "benchmarks": {"US": "SPY", "HK": "HK.02800"},
    }

def build_standard_backtest_run_payload(
    config: DashboardConfig,
    request: dict[str, Any],
    *,
    provider: DailyKlineProvider | None = None,
) -> dict[str, Any]:
    if "adapter" in request:
        raise ValueError("不支持从界面选择回测执行工具")
    parsed = parse_standard_backtest_request(config, request)
    owned_provider = provider is None
    price_provider = provider or FutuQuoteClient(host=config.futu_host, port=config.futu_port)
    try:
        return run_standard_backtest(parsed, price_provider=price_provider).to_dict()
    finally:
        if owned_provider:
            price_provider.close()
```

Implement `parse_standard_backtest_request(config, request)` beside these
builders. It must return `StandardBacktestRequest`, parse percent input `10%` as
`Decimal("0.10")`, accept ISO dates only, verify the selected market/symbol is in
`build_standard_backtest_options_payload(config)["universe"]`, and reject keys
outside the documented request schema.

Validate market/symbol membership in the returned universe, exact strategy ID,
range mode, dates, decimals, and unsupported `adapter`. Map validation errors to
HTTP 400 and provider/adapter failures to HTTP 502 with Chinese messages. Return
the complete normalized result payload required by the result UI.

- [ ] **Step 6: Remove holding-level readiness and automatic page-load fetch**

Delete calls that attach `backtest_readiness`/`backtest` to holding rows. Change
normal `GET /api/dashboard` construction to `auto_fetch_backtest_prices=False`.
Retain the legacy `/api/backtests/run` implementation only if compatibility
tests or external callers require it; do not expose it in the UI. Delete the
legacy `/api/backtests/prices` route and auto-fetch helpers once `rg` confirms
they have no remaining non-test caller.

- [ ] **Step 7: Run dashboard tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: all tests PASS after replacing obsolete per-row readiness assertions
with universe and API assertions.

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: expose standard backtest dashboard APIs"
```

---

### Task 5: Single Homepage Entry and Backtest Run Workspace

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `/api/backtests/options` and `/api/backtests/standard/run`.
- Produces: one `#open-standard-backtest` header action, one `#standard-backtest-workspace`, and no `[data-detail-mode="backtest"]` or `#header-backtest-filters` elements.

- [ ] **Step 1: Write static contract tests**

```python
def test_dashboard_has_one_global_backtest_entry_and_no_row_entry() -> None:
    html = DASHBOARD_INDEX.read_text(encoding="utf-8")
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    assert html.count('id="open-standard-backtest"') == 1
    assert 'id="standard-backtest-workspace"' in html
    assert 'id="header-backtest-filters"' not in html
    assert 'data-detail-mode="backtest"' not in js
    assert "查看回测" not in js
```

- [ ] **Step 2: Run the static test and verify failure**

Run: `.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_has_one_global_backtest_entry_and_no_row_entry -q`

Expected: FAIL because the current homepage has readiness filters and holding-row backtest actions.

- [ ] **Step 3: Add the dedicated workspace markup**

Add a single header button beside the Open Trader brand/title. The workspace
contains semantic elements with these stable IDs:

```html
<button id="open-standard-backtest" class="primary-button" type="button">策略回测</button>
<section id="standard-backtest-workspace" class="backtest-workspace hidden" hidden>
  <header class="backtest-workspace-header">
    <div><h1>策略回测</h1><p>一次选择一个标的和一套策略。</p></div>
    <button id="close-standard-backtest" class="raw-toggle" type="button">返回持仓看板</button>
  </header>
  <form id="standard-backtest-form">
    <div id="backtest-symbol-source"></div>
    <select id="backtest-symbol" required></select>
    <div id="backtest-strategy-cards"></div>
    <div id="backtest-range-controls"></div>
    <input id="backtest-max-weight" inputmode="decimal" value="10%">
    <button id="run-standard-backtest" type="submit">运行回测</button>
  </form>
  <div id="standard-backtest-status" aria-live="polite"></div>
  <section id="standard-backtest-results" hidden></section>
</section>
```

Remove the homepage backtest filter block and price-sync status. Do not add any
backtest control to a holding row.

- [ ] **Step 4: Add JavaScript form-state and request tests**

Extend the existing Node-based static test harness in `tests/test_dashboard_web.py`:

```javascript
document.getElementById("open-standard-backtest").click();
if (document.getElementById("standard-backtest-workspace").hidden) throw new Error("workspace hidden");
selectValue("backtest-symbol", "US:MSFT");
clickChoice("trend_pullback/v1");
clickChoice("3Y");
document.getElementById("standard-backtest-form").dispatchEvent(new Event("submit"));
if (posted.url !== "/api/backtests/standard/run") throw new Error(posted.url);
if (posted.body.adapter !== undefined) throw new Error("adapter leaked to UI");
if (posted.body.range_preset !== "3Y") throw new Error(JSON.stringify(posted.body));
```

- [ ] **Step 5: Implement workspace state and submission**

Add one nested state object:

```javascript
standardBacktest: {
  options: null,
  source: "holdings",
  symbolKey: "",
  strategyId: "trend_pullback/v1",
  rangePreset: "1Y",
  customStart: "",
  customEnd: "",
  busy: false,
  error: "",
  result: null,
}
```

Load options only when the global entry is first opened. Render current holdings
and watchlist as separate source tabs. Show the three selectable strategy cards
and a disabled `自定义策略 / 后续版本` card. Reveal custom date inputs only for
`CUSTOM`. Post no adapter field. Preserve the last form selection while the user
returns to the homepage during the same browser session.

- [ ] **Step 6: Implement responsive styling matching the confirmed mock**

Use the existing dashboard colors, radii, and typography. On desktop, show the
three strategy cards in one row and the form in a two-column grid; below 850px,
stack all controls and keep the run button full width. The workspace replaces
the holdings workspace while open rather than appearing inside a holding detail.

- [ ] **Step 7: Run static/browser-harness tests and commit**

Run: `.venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Expected: all tests PASS.

```bash
git add src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: add global standard backtest workspace"
```

---

### Task 6: Benchmark-Aware Result UI

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: the normalized result payload from `build_standard_backtest_run_payload`.
- Produces: summary comparison, three equity curves, price/action chart, trade table, assumptions, ranges, and reproducibility details.

- [ ] **Step 1: Write result rendering tests**

```javascript
renderStandardBacktestResult(fixtureResult);
const text = document.getElementById("standard-backtest-results").textContent;
for (const expected of [
  "策略收益", "买入持有", "SPY", "相对买入持有", "相对市场指数",
  "最大回撤", "交易次数", "胜率", "BUY", "ADD", "REDUCE", "EXIT",
  "请求范围", "实际数据", "trend_pullback/v1",
]) {
  if (!text.includes(expected)) throw new Error(`missing ${expected}`);
}
```

Add a zero-trade fixture and assert the exact Chinese message
`所选区间内没有触发交易` appears without an error class. Add a missing benchmark
fixture and assert the strategy result remains visible while the benchmark card
shows `基准行情缺失，无法比较`.

- [ ] **Step 2: Run rendering tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'standard_backtest_result' -q`

Expected: FAIL because result rendering functions do not exist.

- [ ] **Step 3: Implement comparison cards and charts**

Build pure rendering helpers:

```javascript
function renderStandardBacktestResult(result) {
  const target = document.getElementById("standard-backtest-results");
  target.innerHTML = [
    renderBacktestComparisonMetrics(result),
    renderBacktestEquityComparison(result),
    renderBacktestPriceActions(result),
    renderBacktestTradeTable(result),
    renderBacktestRunAssumptions(result),
  ].join("");
  target.hidden = false;
}

function renderBacktestComparisonMetrics(result) {
  const rows = [
    ["策略收益", result.strategy.total_return_pct],
    ["买入持有", result.buy_hold.total_return_pct],
    [result.market_benchmark.symbol, result.market_benchmark.total_return_pct],
    ["相对买入持有", result.strategy_excess_return_pct],
    ["相对市场指数", result.market_excess_return_pct],
    ["最大回撤", result.strategy.max_drawdown_pct],
  ];
  return `<section class="backtest-comparison-grid">${rows.map(([label, value]) =>
    `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(percentValue(value))}</strong></article>`
  ).join("")}</section>`;
}

function renderBacktestEquityComparison(result) {
  return renderThreeSeriesBacktestChart(
    result.strategy.equity_curve,
    result.buy_hold.equity_curve,
    result.market_benchmark.equity_curve,
  );
}

function renderBacktestPriceActions(result) {
  return renderPriceActionChart(result.strategy.price_rows, result.strategy.signals);
}

function renderBacktestTradeTable(result) {
  if (!result.strategy.trades.length) {
    return `<section class="detail-section"><p>所选区间内没有触发交易</p></section>`;
  }
  return `<section class="detail-section"><table class="backtest-trades-table"><tbody>${
    result.strategy.trades.map((trade) => `<tr><td>${escapeHtml(trade.execution_date)}</td><td>${escapeHtml(trade.action)}</td><td>${escapeHtml(trade.quantity)}</td><td>${escapeHtml(trade.execution_price)}</td></tr>`).join("")
  }</tbody></table></section>`;
}

function renderBacktestRunAssumptions(result) {
  return `<section class="detail-section"><dl><dt>请求范围</dt><dd>${escapeHtml(result.requested_start)} 至 ${escapeHtml(result.requested_end)}</dd><dt>实际数据</dt><dd>${escapeHtml(result.actual_start)} 至 ${escapeHtml(result.actual_end)}</dd><dt>策略版本</dt><dd>${escapeHtml(result.strategy_id)}</dd></dl></section>`;
}
```

Implement `renderThreeSeriesBacktestChart` and `renderPriceActionChart` in this
task as pure SVG renderers. `renderThreeSeriesBacktestChart` aligns rows by date,
uses the existing numeric/path helpers, and always emits three labeled paths.
`renderPriceActionChart` aligns signals to close prices and emits a marker only
for `BUY`, `ADD`, `REDUCE`, or `EXIT`; `HOLD` remains available in accessible
summary text but is not drawn as a marker.

Reuse the existing SVG path/axis helpers where their inputs match; do not retain
the holding-detail coupling. Use distinct legend labels and colors for strategy,
buy-and-hold, and market benchmark. Action markers use Chinese explanations in
accessible text even if the visual marker contains the stable English action.

- [ ] **Step 4: Render errors without losing form state**

Map API messages into `#standard-backtest-status`; keep the selected symbol,
strategy, and range. Treat zero trades as success. For missing benchmark data,
render strategy and buy-and-hold results and mark only the unavailable comparison.

- [ ] **Step 5: Run result and full dashboard tests, then commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_strategy_backtest.py -q
```

Expected: all tests PASS.

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: visualize standard backtest comparisons"
```

---

### Task 7: Documentation, Full Verification, and Live Dashboard Proof

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`
- Test: all affected tests and the real local dashboard at `http://127.0.0.1:8766`

**Interfaces:**
- Documents the single global entry, three strategies, ranges, benchmark defaults, cost assumptions, artifact locations, and explicit non-goals.

- [ ] **Step 1: Update operator documentation**

Document the exact UI sequence:

```text
持仓实时看板 → 策略回测 → 当前持仓/自选股 → 单一标的 →
趋势回调/突破动量/区间均值回归 → 时间范围 → 运行回测
```

State that Backtrader is hidden, results are research-only, US uses SPY, HK uses
`HK.02800`, custom strategy editing and automatic execution are excluded, and
actual data dates may be shorter than the requested range but are always shown.

- [ ] **Step 2: Add a dated changelog entry**

Add a newest-first `2026-07-12` entry summarizing the global standard-strategy
workflow, benchmark comparisons, and exact verification performed. Do not claim
live verification until Steps 5-7 have succeeded; finalize the verification
line only after recording those results.

- [ ] **Step 3: Run formatting and focused tests**

Run:

```bash
git diff --check
.venv/bin/python -m pytest tests/test_standard_strategies.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_backtest.py tests/test_backtest_cli.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: `git diff --check` exits 0 and all focused tests PASS.

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests PASS. Record the exact count and duration for the handoff and changelog.

- [ ] **Step 5: Inspect and restart the real dashboard process**

Run:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
ps aux | rg 'open_trader dashboard|python -m open_trader|8766'
screen -ls
```

Stop the exact stale listener if it is running pre-change code. Start the current
worktree using the repository's documented command:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --poll-seconds 5 \
  --host 127.0.0.1 \
  --port 8766
```

Record the new PID and startup timestamp. Do not call the deployment verified
from `curl` alone.

- [ ] **Step 6: Verify real API data and run one real read-only backtest**

Use `GET /api/backtests/options` to confirm current holdings and watchlist symbols.
Choose one current holding with adequate Futu data, run the 1Y
`trend_pullback/v1` request through `POST /api/backtests/standard/run`, and inspect:

- requested and actual date ranges;
- strategy and benchmark symbols;
- nonempty manifest/source hashes;
- artifact existence;
- zero-trade handling or normalized trades;
- no modification to portfolio, trading plan, or order state.

- [ ] **Step 7: Verify the live UI in a browser**

Against `http://127.0.0.1:8766`, prove:

- exactly one visible `策略回测` homepage entry;
- no homepage backtest-status filter;
- no `查看回测` button in any current holding row;
- the global entry opens the dedicated workspace;
- holdings and watchlist source tabs use real local symbols;
- a strategy and range can be selected and submitted;
- the result shows both benchmarks, actual dates, assumptions, charts, trades or
  the valid zero-trade message;
- desktop and mobile widths remain usable.

Capture screenshots and console/network errors. Any old process or stale static
asset invalidates the check until restarted and reverified.

- [ ] **Step 8: Finalize changelog, rerun checks, and commit**

Run:

```bash
git diff --check
.venv/bin/python -m pytest tests/test_standard_strategies.py tests/test_strategy_backtest.py tests/test_dashboard.py tests/test_dashboard_web.py -q
git status --short
```

Expected: checks PASS; only intended documentation/changelog files remain unstaged.

```bash
git add README.md README.zh-CN.md CHANGELOG.md
git commit -m "docs: document standard strategy backtests"
```

Do not merge, push, install launchd jobs, or change live order state without a
separate explicit user request.
