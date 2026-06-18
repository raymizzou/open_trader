# HK Market Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HK market support while running HK and US daily premarket workflows as separate market-scoped jobs with separate artifacts, latest files, readiness, and notifications.

**Architecture:** Add small market/path helpers, then thread an explicit `market` through the portfolio eligibility, premarket, daily runner, plan/action, watcher, notification, and launchd surfaces. Keep lower-level CSV contracts stable and use market-scoped directories only for the daily runner first, so existing manual commands keep working during migration.

**Tech Stack:** Python 3.12, argparse CLI, csv/json files, Futu OpenD via `futu-api`, launchd plist templates, pytest.

---

### Task 1: Add Market Helpers and HK Portfolio Eligibility

**Files:**
- Create: `src/open_trader/market_scope.py`
- Modify: `src/open_trader/portfolio.py`
- Test: `tests/test_market_scope.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Write failing tests for market helper paths**

Add `tests/test_market_scope.py`:

```python
from pathlib import Path

import pytest

from open_trader.market_scope import (
    MarketScope,
    market_report_path,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


def test_parse_market_scope_accepts_us_and_hk_case_insensitively() -> None:
    assert parse_market_scope("us") is MarketScope.US
    assert parse_market_scope("HK") is MarketScope.HK


def test_parse_market_scope_rejects_blank_or_unknown_values() -> None:
    with pytest.raises(ValueError, match="market must be one of: HK, US"):
        parse_market_scope("")
    with pytest.raises(ValueError, match="market must be one of: HK, US"):
        parse_market_scope("CN")


def test_market_scoped_paths_are_separate_from_legacy_latest() -> None:
    data_dir = Path("data")
    reports_dir = Path("reports")

    assert market_run_dir(data_dir, "2026-06-19", MarketScope.HK) == Path(
        "data/runs/2026-06-19/HK"
    )
    assert market_scoped_latest_path(data_dir, MarketScope.HK, "trading_plan.csv") == Path(
        "data/latest/HK/trading_plan.csv"
    )
    assert market_report_path(
        reports_dir,
        "daily_runs",
        "2026-06-19",
        MarketScope.US,
    ) == Path("reports/daily_runs/2026-06-19-US.md")
```

- [ ] **Step 2: Run market helper tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_scope.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.market_scope'`.

- [ ] **Step 3: Implement `src/open_trader/market_scope.py`**

Create `src/open_trader/market_scope.py`:

```python
from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class MarketScope(StrEnum):
    HK = "HK"
    US = "US"


def parse_market_scope(value: str) -> MarketScope:
    normalized = value.strip().upper()
    try:
        return MarketScope(normalized)
    except ValueError as exc:
        raise ValueError("market must be one of: HK, US") from exc


def market_run_dir(data_dir: Path, run_date: str, market: MarketScope) -> Path:
    return data_dir / "runs" / run_date / market.value


def market_scoped_latest_dir(data_dir: Path, market: MarketScope) -> Path:
    return data_dir / "latest" / market.value


def market_scoped_latest_path(data_dir: Path, market: MarketScope, name: str) -> Path:
    return market_scoped_latest_dir(data_dir, market) / name


def market_report_path(
    reports_dir: Path,
    section: str,
    run_date: str,
    market: MarketScope,
) -> Path:
    return reports_dir / section / f"{run_date}-{market.value}.md"
```

- [ ] **Step 4: Run market helper tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_scope.py -v
```

Expected: PASS.

- [ ] **Step 5: Write failing portfolio eligibility tests**

Append to `tests/test_portfolio.py`:

```python
def test_hk_stock_and_etf_are_ai_eligible() -> None:
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position(
            "futu",
            "00700",
            "100",
            "35000",
            "38000",
            market=Market.HK,
            currency="HKD",
        ),
        position(
            "futu",
            "02800",
            "200",
            "40000",
            "42000",
            market=Market.HK,
            asset_class=AssetClass.ETF,
            currency="HKD",
        ),
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    tencent = next(row for row in rows if row["symbol"] == "00700")
    tracker = next(row for row in rows if row["symbol"] == "02800")
    assert tencent["market"] == "HK"
    assert tencent["currency"] == "HKD"
    assert tencent["ai_eligible"] == "true"
    assert tencent["analysis_symbol"] == "00700"
    assert tracker["ai_eligible"] == "true"
    assert tracker["analysis_symbol"] == "02800"


def test_hk_money_market_fund_stays_ai_ineligible() -> None:
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position(
                "futu",
                "HK0000951506.HKD",
                "100",
                "100",
                "100",
                market=Market.HK,
                asset_class=AssetClass.MONEY_MARKET_FUND,
                currency="HKD",
            )
        ],
        [],
        fx,
    )

    fund = rows[0]
    assert fund["market"] == "HK"
    assert fund["ai_eligible"] == "false"
    assert fund["analysis_symbol"] == ""
```

- [ ] **Step 6: Run portfolio tests and verify HK eligibility fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio.py::test_hk_stock_and_etf_are_ai_eligible tests/test_portfolio.py::test_hk_money_market_fund_stays_ai_ineligible -v
```

Expected: first test FAILS because HK stock/ETF rows currently have `ai_eligible=false`; second may pass.

- [ ] **Step 7: Update portfolio eligibility**

In `src/open_trader/portfolio.py`, replace `_sort_group` and `_ai_eligible` with:

```python
def _sort_group(market: Market, asset_class: AssetClass, ai_eligible: bool) -> int:
    if market == Market.HK and ai_eligible:
        return 1
    if market == Market.US and ai_eligible:
        return 2
    if market == Market.HK:
        return 3
    if market == Market.US:
        return 4
    if market == Market.CASH:
        return 6
    return 5


def _ai_eligible(position: Position) -> bool:
    return position.market in {Market.US, Market.HK} and position.asset_class in {
        AssetClass.STOCK,
        AssetClass.ETF,
    }
```

This keeps AI-eligible HK and US rows first while preserving cash and unsupported assets later.

- [ ] **Step 8: Run portfolio and market tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_scope.py tests/test_portfolio.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/market_scope.py src/open_trader/portfolio.py tests/test_market_scope.py tests/test_portfolio.py
git commit -m "feat: add market scope helpers and hk eligibility"
```

### Task 2: Add Market Filtering to Portfolio Loading and Premarket Outputs

**Files:**
- Modify: `src/open_trader/advice/portfolio_loader.py`
- Modify: `src/open_trader/advice/premarket.py`
- Modify: `src/open_trader/advice/report.py`
- Test: `tests/test_advice_portfolio_loader.py`
- Test: `tests/test_premarket_pipeline.py`
- Test: `tests/test_premarket_report.py`

- [ ] **Step 1: Write failing tests for market-scoped eligible loading**

Append to `tests/test_advice_portfolio_loader.py`:

```python
def test_load_eligible_portfolio_rows_filters_by_market(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "name": "Microsoft",
                "portfolio_weight_hkd": "1.13%",
                "ai_eligible": "true",
                "analysis_symbol": "MSFT",
                "risk_flag": "normal",
            },
            {
                "symbol": "00700",
                "market": "HK",
                "asset_class": "stock",
                "name": "Tencent",
                "portfolio_weight_hkd": "2.00%",
                "ai_eligible": "true",
                "analysis_symbol": "00700",
                "risk_flag": "normal",
            },
        ],
    )

    hk_rows = load_eligible_portfolio_rows(path, market="HK")
    us_rows = load_eligible_portfolio_rows(path, market="US")

    assert [row.symbol for row in hk_rows] == ["00700"]
    assert hk_rows[0].market == "HK"
    assert [row.symbol for row in us_rows] == ["MSFT"]
    assert us_rows[0].market == "US"
```

- [ ] **Step 2: Run loader test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_portfolio_loader.py::test_load_eligible_portfolio_rows_filters_by_market -v
```

Expected: FAIL with `TypeError` because `load_eligible_portfolio_rows()` does not accept `market`.

- [ ] **Step 3: Implement market filtering in portfolio loader**

In `src/open_trader/advice/portfolio_loader.py`, change the function signature and add the filter:

```python
def load_eligible_portfolio_rows(
    portfolio_path: Path,
    *,
    market: str | None = None,
) -> list[PortfolioInputRow]:
    market_filter = market.strip().upper() if market else None
    with portfolio_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        eligible: list[PortfolioInputRow] = []
        for row_number, row in enumerate(reader, start=2):
            normalized_row = {
                field: _csv_value(row.get(field))
                for field in (
                    "symbol",
                    "market",
                    "asset_class",
                    "name",
                    "portfolio_weight_hkd",
                    "ai_eligible",
                    "analysis_symbol",
                    "risk_flag",
                )
            }
            normalized_row["market"] = normalized_row["market"].upper()
            if market_filter is not None and normalized_row["market"] != market_filter:
                continue
            if normalized_row["ai_eligible"].lower() != "true":
                continue
            missing_fields = [
                field for field in REQUIRED_FIELDS if not normalized_row[field]
            ]
            if missing_fields:
                raise ValueError(
                    "Eligible portfolio row "
                    f"{row_number} missing required fields: "
                    f"{', '.join(missing_fields)}"
                )

            symbol = normalized_row["symbol"]
            analysis_symbol = normalized_row["analysis_symbol"] or symbol
            eligible.append(
                PortfolioInputRow(
                    symbol=symbol,
                    market=normalized_row["market"],
                    asset_class=normalized_row["asset_class"],
                    name=normalized_row["name"],
                    portfolio_weight_hkd=normalized_row["portfolio_weight_hkd"],
                    risk_flag=normalized_row["risk_flag"],
                    analysis_symbol=analysis_symbol,
                )
            )
    return eligible
```

- [ ] **Step 4: Run loader tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_portfolio_loader.py -v
```

Expected: PASS.

- [ ] **Step 5: Write failing premarket market-output tests**

In `tests/test_premarket_pipeline.py`, add a focused test near existing `run_premarket` tests:

```python
def test_run_premarket_filters_hk_market_and_writes_market_scoped_outputs(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio,
        [
            portfolio_row(symbol="MSFT", market="US", ai_eligible="true"),
            portfolio_row(symbol="00700", market="HK", ai_eligible="true"),
        ],
    )

    result = run_premarket(
        run_date="2026-06-19",
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        market="HK",
        update_latest=True,
        max_workers=1,
    )

    assert result.eligible_count == 1
    assert result.advice_path == tmp_path / "data/runs/2026-06-19/HK/trading_advice.csv"
    assert result.actions_path == tmp_path / "data/runs/2026-06-19/HK/premarket_actions.csv"
    assert result.report_path == tmp_path / "reports/premarket/2026-06-19-HK.md"
    assert (tmp_path / "data/latest/HK/trading_advice.csv").exists()
```

If `tests/test_premarket_pipeline.py` does not already expose `portfolio_row`,
add this helper near the other test helpers:

```python
def portfolio_row(
    *,
    symbol: str,
    market: str,
    ai_eligible: str,
    asset_class: str = "stock",
    name: str = "Test Holding",
    portfolio_weight_hkd: str = "1.00%",
    analysis_symbol: str | None = None,
    risk_flag: str = "normal",
) -> dict[str, str]:
    return {
        "sort_group": "1",
        "market": market,
        "asset_class": asset_class,
        "symbol": symbol,
        "name": name,
        "currency": "HKD" if market == "HK" else "USD",
        "total_quantity": "100",
        "avg_cost_price": "100",
        "last_price": "110",
        "market_value": "11000",
        "cost_value": "10000",
        "unrealized_pnl": "1000",
        "unrealized_pnl_pct": "10.00%",
        "fx_source": "static",
        "fx_date": "2026-05",
        "fx_to_hkd": "1" if market == "HK" else "7.8",
        "market_value_hkd": "11000",
        "cost_value_hkd": "10000",
        "portfolio_weight_hkd": portfolio_weight_hkd,
        "brokers": "futu",
        "accounts": "main",
        "ai_eligible": ai_eligible,
        "analysis_symbol": analysis_symbol or symbol,
        "risk_flag": risk_flag,
        "confidence": "high",
        "notes": "",
    }
```

- [ ] **Step 6: Run the new premarket test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_filters_hk_market_and_writes_market_scoped_outputs -v
```

Expected: FAIL because `run_premarket()` does not accept `market` and outputs are not market-scoped.

- [ ] **Step 7: Add `market` argument to `run_premarket` and output writer**

In `src/open_trader/advice/premarket.py`, update the `run_premarket` signature:

```python
def run_premarket(
    *,
    run_date: str,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    advice_runner: AdviceRunner | None = None,
    advice_runner_factory: Callable[[], AdviceRunner] | None = None,
    classifier: ChangeClassifier | None = None,
    symbols: set[str] | None = None,
    excluded_symbols: set[str] | None = None,
    update_latest: bool = True,
    max_workers: int = 1,
    use_fallback: bool = False,
    deadline_reached: Callable[[], bool] | None = None,
    market: str | None = None,
) -> PremarketRunResult:
```

When loading eligible rows, call:

```python
portfolio_rows = load_eligible_portfolio_rows(portfolio_path, market=market)
```

When writing outputs, pass `market=market` to `write_premarket_outputs`.

In `src/open_trader/advice/report.py`, update `write_premarket_outputs`:

```python
def write_premarket_outputs(
    *,
    run_date: str,
    actions: Iterable[PremarketAction],
    data_dir: Path,
    reports_dir: Path,
    update_latest: bool = True,
    no_eligible: bool = False,
    market: str | None = None,
) -> tuple[Path, Path, Path]:
    market_value = market.strip().upper() if market else ""
    if market_value:
        run_actions_path = data_dir / "runs" / run_date / market_value / "premarket_actions.csv"
        latest_actions_path = data_dir / "latest" / market_value / "premarket_actions.csv"
        report_path = reports_dir / "premarket" / f"{run_date}-{market_value}.md"
    else:
        run_actions_path = data_dir / "runs" / run_date / "premarket_actions.csv"
        latest_actions_path = data_dir / "latest" / "premarket_actions.csv"
        report_path = reports_dir / "premarket" / f"{run_date}.md"
```

Apply the same scoped-path decision in `run_premarket` for `trading_advice.csv` and `change_classifications.csv`.

- [ ] **Step 8: Make no-eligible report wording market-aware**

In `src/open_trader/advice/report.py`, add:

```python
def _no_eligible_message(market: str | None) -> str:
    if market and market.strip().upper() == "HK":
        return "No eligible HK stocks or ETFs were found."
    if market and market.strip().upper() == "US":
        return "No eligible US stocks or ETFs were found."
    return "No eligible stocks or ETFs were found."
```

Then in `_render_markdown`, use `_no_eligible_message(market)` and pass `market` through from `write_premarket_outputs`.

- [ ] **Step 9: Run premarket tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_portfolio_loader.py tests/test_premarket_pipeline.py tests/test_premarket_report.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/open_trader/advice/portfolio_loader.py src/open_trader/advice/premarket.py src/open_trader/advice/report.py tests/test_advice_portfolio_loader.py tests/test_premarket_pipeline.py tests/test_premarket_report.py
git commit -m "feat: scope premarket analysis by market"
```

### Task 2A: Add HK TradingAgents Market Context

**Files:**
- Modify: `src/open_trader/advice/tradingagents_adapter.py`
- Test: `tests/test_tradingagents_adapter.py`

- [ ] **Step 1: Write failing HK analysis-context test**

Append to `tests/test_tradingagents_adapter.py`:

```python
def test_adapter_uses_hk_futu_symbol_and_records_hk_context() -> None:
    graph = FakeGraph()
    adapter = TradingAgentsAdapter.from_graph(graph)
    row = PortfolioInputRow(
        symbol="00700",
        market="HK",
        asset_class="stock",
        name="Tencent",
        portfolio_weight_hkd="2.00%",
        risk_flag="normal",
        analysis_symbol="00700",
    )

    advice = adapter.analyze(row, "2026-06-19")
    raw = json.loads(advice.raw_decision)

    assert graph.calls == [("HK.00700", "2026-06-19")]
    assert raw["market_context"] == {
        "market": "HK",
        "market_name": "Hong Kong / HKEX",
        "currency": "HKD",
        "portfolio_symbol": "00700",
        "tradingagents_symbol": "HK.00700",
        "futu_symbol": "HK.00700",
    }
```

- [ ] **Step 2: Run HK adapter context test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_adapter.py::test_adapter_uses_hk_futu_symbol_and_records_hk_context -v
```

Expected: FAIL because the adapter currently calls the graph with `00700` and
does not write `market_context` into `raw_decision`.

- [ ] **Step 3: Add market context helpers**

In `src/open_trader/advice/tradingagents_adapter.py`, add:

```python
def _market_context(row: PortfolioInputRow) -> dict[str, str]:
    market = row.market.strip().upper()
    tradingagents_symbol = _tradingagents_symbol(row)
    if market == "HK":
        return {
            "market": "HK",
            "market_name": "Hong Kong / HKEX",
            "currency": "HKD",
            "portfolio_symbol": row.symbol,
            "tradingagents_symbol": tradingagents_symbol,
            "futu_symbol": tradingagents_symbol,
        }
    return {
        "market": market or "US",
        "market_name": "United States",
        "currency": "USD",
        "portfolio_symbol": row.symbol,
        "tradingagents_symbol": tradingagents_symbol,
        "futu_symbol": tradingagents_symbol if "." in tradingagents_symbol else f"US.{tradingagents_symbol}",
    }


def _tradingagents_symbol(row: PortfolioInputRow) -> str:
    market = row.market.strip().upper()
    symbol = row.analysis_symbol.strip().upper() or row.symbol.strip().upper()
    if market == "HK" and symbol.isdigit():
        return f"HK.{symbol.zfill(5)}"
    return symbol
```

- [ ] **Step 4: Use market context in adapter output**

In `TradingAgentsAdapter.analyze`, replace:

```python
state, decision = self._graph.propagate(row.analysis_symbol, run_date)
```

with:

```python
market_context = _market_context(row)
state, decision = self._graph.propagate(
    market_context["tradingagents_symbol"],
    run_date,
)
```

Replace the successful `raw_decision` payload with:

```python
raw_decision=json.dumps(
    {
        "market_context": market_context,
        "state": state,
        "decision": decision,
    },
    ensure_ascii=False,
    default=str,
),
```

- [ ] **Step 5: Run adapter tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_adapter.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/advice/tradingagents_adapter.py tests/test_tradingagents_adapter.py
git commit -m "feat: add hk tradingagents market context"
```

### Task 3: Market-Scope Trading Plan and Trade Actions Paths

**Files:**
- Modify: `src/open_trader/trading_plan.py`
- Modify: `src/open_trader/trade_actions.py`
- Test: `tests/test_trading_plan.py`
- Test: `tests/test_trade_actions.py`

- [ ] **Step 1: Write failing HK trading plan path and symbol tests**

Append to `tests/test_trading_plan.py`:

```python
def test_build_trading_plan_writes_market_scoped_hk_paths(tmp_path: Path) -> None:
    advice = tmp_path / "advice.csv"
    write_advice(
        advice,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "00700",
                "market": "HK",
                "advice_action": "Overweight",
                "advice_summary": TEMPLATE_SUMMARY,
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(
        advice,
        tmp_path / "data",
        run_date="2026-06-19",
        update_latest=True,
        market="HK",
    )
    rows = load_trading_plan_rows(result.plan_path)

    assert result.plan_path == tmp_path / "data/runs/2026-06-19/HK/trading_plan.csv"
    assert result.latest_path == tmp_path / "data/latest/HK/trading_plan.csv"
    assert rows[0].futu_symbol == "HK.00700"
```

Use the existing advice-writing helper names in `tests/test_trading_plan.py`. If `TEMPLATE_SUMMARY` does not exist, use the same structured Chinese template already used by existing active-plan tests.

- [ ] **Step 2: Run HK trading plan test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py::test_build_trading_plan_writes_market_scoped_hk_paths -v
```

Expected: FAIL because `build_trading_plan()` does not accept `market`.

- [ ] **Step 3: Add market-scoped paths to trading plan builder**

In `src/open_trader/trading_plan.py`, change the function signature:

```python
def build_trading_plan(
    advice_path: Path,
    data_dir: Path,
    run_date: str | None = None,
    update_latest: bool = True,
    market: str | None = None,
) -> TradingPlanBuildResult:
```

After filtering by date, add market filtering:

```python
market_filter = market.strip().upper() if market else None
if market_filter is not None:
    filtered_rows = [
        row
        for row in filtered_rows
        if row.get("market", "").strip().upper() == market_filter
    ]
```

Choose paths:

```python
if market_filter:
    plan_path = data_dir / "runs" / effective_run_date / market_filter / "trading_plan.csv"
    latest_path = data_dir / "latest" / market_filter / "trading_plan.csv"
else:
    plan_path = data_dir / "runs" / effective_run_date / "trading_plan.csv"
    latest_path = data_dir / "latest" / "trading_plan.csv"
```

- [ ] **Step 4: Run trading plan tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py -v
```

Expected: PASS.

- [ ] **Step 5: Write failing HK trade action tests**

Append to `tests/test_trade_actions.py`:

```python
def test_generate_trade_actions_writes_market_scoped_hk_paths_and_uses_hkd_cash(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "data/runs/2026-06-19/HK/trading_plan.csv"
    write_plan(
        plan_path,
        [
            plan_row(
                run_date="2026-06-19",
                symbol="00700",
                market="HK",
                entry_zone_low="370",
                entry_zone_high="390",
                max_weight="5%",
            )
        ],
    )
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            portfolio_row(
                market="HK",
                symbol="00700",
                currency="HKD",
                total_quantity="100",
                avg_cost_price="350",
                market_value="38000",
                fx_to_hkd="1",
                market_value_hkd="38000",
                portfolio_weight_hkd="2.00%",
            ),
            portfolio_row(
                market="CASH",
                asset_class="cash",
                symbol="HKD_CASH",
                currency="HKD",
                total_quantity="1",
                market_value="10000",
                fx_to_hkd="1",
                market_value_hkd="10000",
                portfolio_weight_hkd="0.50%",
            ),
        ],
    )

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        snapshots={"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))},
        run_date="2026-06-19",
        update_latest=True,
        market="HK",
    )
    rows = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))

    assert result.actions_path == tmp_path / "data/runs/2026-06-19/HK/trade_actions.csv"
    assert result.latest_path == tmp_path / "data/latest/HK/trade_actions.csv"
    assert result.report_path == tmp_path / "reports/trade_actions/2026-06-19-HK.md"
    assert rows[0]["futu_symbol"] == "HK.00700"
    assert rows[0]["notional_currency"] == "HKD"
    assert rows[0]["cash_available"] == "10000"
```

Adapt helper argument names to match existing helpers in `tests/test_trade_actions.py`.

- [ ] **Step 6: Run HK trade action test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions.py::test_generate_trade_actions_writes_market_scoped_hk_paths_and_uses_hkd_cash -v
```

Expected: FAIL because `generate_trade_actions()` does not accept `market`.

- [ ] **Step 7: Add market-scoped paths and filtering to trade actions**

In `src/open_trader/trade_actions.py`, update signature:

```python
def generate_trade_actions(
    *,
    plan_path: Path,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    snapshots: dict[str, QuoteSnapshot],
    run_date: str | None,
    update_latest: bool,
    market: str | None = None,
) -> TradeActionsResult:
```

After selecting `plans`, filter by market:

```python
market_filter = market.strip().upper() if market else None
if market_filter is not None:
    plans = [plan for plan in plans if plan.market.upper() == market_filter]
```

Choose output paths:

```python
if market_filter:
    actions_path = data_dir / "runs" / effective_run_date / market_filter / "trade_actions.csv"
    latest_path = data_dir / "latest" / market_filter / "trade_actions.csv"
    report_path = reports_dir / "trade_actions" / f"{effective_run_date}-{market_filter}.md"
else:
    actions_path = data_dir / "runs" / effective_run_date / "trade_actions.csv"
    latest_path = data_dir / "latest" / "trade_actions.csv"
    report_path = reports_dir / "trade_actions" / f"{effective_run_date}.md"
```

The existing `_notional_currency()` already returns `HKD` for non-US markets and position currency should win. Keep that behavior.

- [ ] **Step 8: Run trading plan and trade action tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py tests/test_trade_actions.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/trading_plan.py src/open_trader/trade_actions.py tests/test_trading_plan.py tests/test_trade_actions.py
git commit -m "feat: write market scoped plan and trade actions"
```

### Task 4: Market-Scope the Daily Runner and CLI

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_daily_premarket.py`
- Test: `tests/test_premarket_cli.py` or `tests/test_daily_premarket.py` for CLI parser behavior

- [ ] **Step 1: Write failing config/default deadline tests**

Append to `tests/test_daily_premarket.py`:

```python
def test_daily_config_deadline_for_market_uses_hk_and_us_defaults(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
    )

    assert daily_premarket._deadline_for_market(config, "HK") == "09:00"
    assert daily_premarket._deadline_for_market(config, "US") == "21:10"
```

- [ ] **Step 2: Run deadline test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_config_deadline_for_market_uses_hk_and_us_defaults -v
```

Expected: FAIL because `_deadline_for_market` is missing.

- [ ] **Step 3: Implement market deadline helpers**

In `src/open_trader/daily_premarket.py`, import `MarketScope` and `parse_market_scope`:

```python
from .market_scope import MarketScope, parse_market_scope
```

Add helpers:

```python
def _deadline_for_market(config: DailyPremarketConfig, market: str) -> str:
    scope = parse_market_scope(market)
    if scope is MarketScope.HK:
        return "09:00"
    return config.deadline


def _config_for_market(config: DailyPremarketConfig, market: str) -> DailyPremarketConfig:
    scope = parse_market_scope(market)
    return replace(config, deadline=_deadline_for_market(config, scope.value))
```

Also add `replace` to the dataclass imports:

```python
from dataclasses import dataclass, replace
```

- [ ] **Step 4: Write failing daily runner market-path test**

Append to `tests/test_daily_premarket.py`:

```python
def test_daily_runner_hk_uses_market_scoped_paths_and_calls_market_filter(
    tmp_path: Path,
) -> None:
    (tmp_path / "data/latest").mkdir(parents=True)
    (tmp_path / "data/latest/portfolio.csv").write_text("portfolio\n", encoding="utf-8")
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
    )
    premarket = FakePremarket(market="HK", symbol="00700")
    plan_builder = FakePlanBuilder(market="HK", symbol="00700")
    trade_actions = FakeTradeActionGenerator(market="HK")

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))}
        ),
        trade_action_generator=trade_actions,
    ).run(run_date="2026-06-19", market="HK")

    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert result.status_path == tmp_path / "data/runs/2026-06-19/HK/daily_run_status.json"
    assert result.report_path == tmp_path / "reports/daily_runs/2026-06-19-HK.md"
    assert status["market"] == "HK"
    assert status["deadline_at"].endswith("09:00:00+08:00")
    assert premarket.calls[0]["market"] == "HK"
    assert plan_builder.calls[0]["market"] == "HK"
    assert trade_actions.calls[0]["market"] == "HK"
    assert status["artifacts"]["latest_trading_plan"].endswith("data/latest/HK/trading_plan.csv")
```

Update the fake classes in the same test file so constructors accept optional `market="US"` and `symbol="MSFT"` and write rows using those values:

```python
class FakePremarket:
    def __init__(self, *, market: str = "US", symbol: str = "MSFT") -> None:
        self.market = market
        self.symbol = symbol
        self.calls: list[dict[str, object]] = []
```

Apply the same pattern to `FakePlanBuilder` and `FakeTradeActionGenerator`, preserving existing defaults so current tests keep passing.

- [ ] **Step 5: Run daily runner market-path test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_runner_hk_uses_market_scoped_paths_and_calls_market_filter -v
```

Expected: FAIL because `DailyPremarketRunner.run()` does not accept `market`.

- [ ] **Step 6: Thread market through daily runner**

In `src/open_trader/daily_premarket.py`, update:

```python
def run(
    self,
    run_date: str,
    *,
    market: str,
    dry_run: bool | None = None,
) -> DailyRunResult:
    market_scope = parse_market_scope(market)
    market_config = _config_for_market(self.config, market_scope.value)
```

Use `market_config` for deadline calculations in this run. Compute paths:

```python
status_path = (
    self.config.data_dir
    / "runs"
    / run_date
    / market_scope.value
    / "daily_run_status.json"
)
report_path = self.config.reports_dir / "daily_runs" / f"{run_date}-{market_scope.value}.md"
log_path = self.config.logs_dir / "daily_premarket" / f"{run_date}-{market_scope.value}.log"
lock_path = self.config.data_dir / "runs" / f".daily_premarket.{market_scope.value}.lock"
```

Update `_run_locked` to accept these arguments:

```python
def _run_locked(
    self,
    *,
    run_date: str,
    market: str,
    config: DailyPremarketConfig,
    started_at: datetime,
    status_path: Path,
    report_path: Path,
    log_path: Path,
    dry_run: bool,
) -> DailyRunResult:
```

Update `_write_failure` and `_write_already_running` with the same required
`market: str` and `config: DailyPremarketConfig` keyword arguments. Use `config`
instead of `self.config` for deadline calculations in those methods.

Inside `_run_locked`, pass market into lower layers:

```python
premarket_result = self.premarket_runner(
    run_date=run_date,
    portfolio_path=config.portfolio,
    data_dir=config.data_dir,
    reports_dir=config.reports_dir,
    advice_runner=None,
    advice_runner_factory=self._advice_runner_factory(config, run_date),
    classifier=ChangeClassifier(
        client=OpenAIClassifierClient(model=config.classifier_model)
    ),
    symbols=None,
    excluded_symbols=None,
    update_latest=False,
    max_workers=config.max_workers,
    use_fallback=True,
    deadline_reached=_deadline_reached(config, run_date),
    market=market,
)
plan_result = self.plan_builder(
    advice_path=advice_path,
    data_dir=config.data_dir,
    run_date=run_date,
    update_latest=False,
    market=market,
)
trade_actions_result = self.trade_action_generator(
    plan_path=plan_result.plan_path,
    portfolio_path=config.portfolio,
    data_dir=config.data_dir,
    reports_dir=config.reports_dir,
    snapshots=_snapshots_from_futu_status(futu_status),
    run_date=run_date,
    update_latest=False,
    market=market,
)
```

Use market-scoped latest artifact paths:

```python
latest_advice_path = self.config.data_dir / "latest" / market / "trading_advice.csv"
latest_actions_path = self.config.data_dir / "latest" / market / "premarket_actions.csv"
latest_plan_path = self.config.data_dir / "latest" / market / "trading_plan.csv"
```

Update `_promote_latest_set` to:

```python
def _promote_latest_set(
    *,
    advice_path: Path,
    actions_path: Path,
    plan_path: Path,
    trade_actions_path: Path,
    data_dir: Path,
    market: str | None = None,
) -> None:
    latest_dir = data_dir / "latest" / market if market else data_dir / "latest"
    promotions = [
        _LatestPromotion(advice_path, latest_dir / "trading_advice.csv"),
        _LatestPromotion(actions_path, latest_dir / "premarket_actions.csv"),
        _LatestPromotion(plan_path, latest_dir / "trading_plan.csv"),
        _LatestPromotion(trade_actions_path, latest_dir / "trade_actions.csv"),
    ]
```

Keep the existing backup, replace, rollback, and cleanup code after the
`promotions` list.

- [ ] **Step 7: Add market to status JSON and daily reports**

In `_write_status_and_report`, add `market` and include it in the payload:

```python
"market": market,
```

In `_render_daily_report`, add a market line after the title summary:

```python
if payload.get("market"):
    lines.append(f"- Market: {payload['market']}")
```

- [ ] **Step 8: Make CLI require `--market` for daily runs**

In `src/open_trader/cli.py`, add a market parser helper:

```python
def canonical_market(value: str) -> str:
    try:
        return parse_market_scope(value).value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
```

Import:

```python
from .market_scope import parse_market_scope
```

On `daily_parser`, add:

```python
daily_parser.add_argument(
    "--market",
    type=canonical_market,
    required=True,
    choices=["HK", "US"],
    help="Market workflow to run: HK or US",
)
```

When running:

```python
result = DailyPremarketRunner(
    config=config,
    notifier=build_notifier(config),
).run(
    run_date=run_date,
    market=args.market,
    dry_run=args.dry_run,
)
```

- [ ] **Step 9: Run daily and CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_premarket_cli.py -v
```

Expected: PASS. If existing CLI tests directly call `run-daily-premarket` without `--market`, update those tests to pass `--market US` because the approved spec requires market to be explicit.

- [ ] **Step 10: Commit**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/cli.py tests/test_daily_premarket.py tests/test_premarket_cli.py
git commit -m "feat: run daily premarket by market"
```

### Task 5: Support HK Triggers in Futu Watch

**Files:**
- Modify: `src/open_trader/futu_watch.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_futu_watch.py`

- [ ] **Step 1: Write failing HK watcher trigger test**

Append to `tests/test_futu_watch.py`:

```python
def test_load_monitor_triggers_keeps_hk_active_price_rows(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(
                symbol="00700",
                market="HK",
                operator=">=",
                trigger_price="390",
                trigger_text="升穿 390",
            ),
            base_row(symbol="BADHK", market="HK"),
            base_row(symbol="MSFT", market="US"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert loaded.triggers == [
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="00700",
            market="HK",
            futu_symbol="HK.00700",
            trigger_type="price",
            operator=">=",
            trigger_price=Decimal("390"),
            suggested_action="reduce",
            severity="high",
            trigger_text="升穿 390",
        ),
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="MSFT",
            market="US",
            futu_symbol="US.MSFT",
            trigger_type="price",
            operator="<=",
            trigger_price=Decimal("95"),
            suggested_action="reduce",
            severity="high",
            trigger_text="below 95",
        ),
    ]
```

- [ ] **Step 2: Run watcher test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py::test_load_monitor_triggers_keeps_hk_active_price_rows -v
```

Expected: FAIL because HK rows are currently skipped.

- [ ] **Step 3: Add market-to-Futu conversion in watcher**

In `src/open_trader/futu_watch.py`, add:

```python
def _to_futu_symbol(market: str, symbol: str) -> str | None:
    if market == "US" and symbol:
        return f"US.{symbol}"
    if market == "HK" and symbol.isdigit():
        return f"HK.{symbol.zfill(5)}"
    return None
```

In `_trigger_from_row`, replace the `market != "US"` condition with:

```python
futu_symbol = _to_futu_symbol(market, symbol)
if (
    futu_symbol is None
    or row.get("status", "").strip() != "active"
    or trigger_type not in {"price", "open_price"}
    or operator not in {"<=", ">="}
):
    return None
```

Return `futu_symbol=futu_symbol`.

Change output text in `run_futu_watch` from:

```python
output_fn(f"loaded {len(loaded.triggers)} active US trigger(s)")
```

to:

```python
output_fn(f"loaded {len(loaded.triggers)} active trigger(s)")
```

- [ ] **Step 4: Update CLI help text**

In `src/open_trader/cli.py`, change watch-futu help from:

```python
help="Watch active US price triggers with Futu OpenD quotes",
```

to:

```python
help="Watch active US/HK price triggers with Futu OpenD quotes",
```

- [ ] **Step 5: Run watcher tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/futu_watch.py src/open_trader/cli.py tests/test_futu_watch.py
git commit -m "feat: support hk futu watch triggers"
```

### Task 6: Market-Aware Notifications and Notification Logs

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing notification title/log tests**

Append to `tests/test_daily_premarket.py`:

```python
def test_hk_daily_runner_uses_market_notification_titles(tmp_path: Path) -> None:
    (tmp_path / "data/latest").mkdir(parents=True)
    (tmp_path / "data/latest/portfolio.csv").write_text("portfolio\n", encoding="utf-8")
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        notify_daily_report=True,
    )
    notifier = RecordingNotifier()

    DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(market="HK", symbol="00700"),
        plan_builder=FakePlanBuilder(market="HK", symbol="00700"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))}
        ),
        trade_action_generator=FakeTradeActionGenerator(market="HK"),
        notifier=notifier,
    ).run(run_date="2026-06-19", market="HK")

    titles = [call[0] for call in notifier.calls]
    assert "Open Trader 港股行动通知" in titles
    log_rows = list(
        csv.DictReader(
            (tmp_path / "logs/notifications/2026-06-19-HK.csv").open(encoding="utf-8")
        )
    )
    assert log_rows[0]["market"] == "HK"
```

If no `RecordingNotifier` exists, add this helper in the test file:

```python
class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))
```

- [ ] **Step 2: Run notification test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_hk_daily_runner_uses_market_notification_titles -v
```

Expected: FAIL because notification titles and logs are not market-aware.

- [ ] **Step 3: Add market notification title helper**

In `src/open_trader/daily_premarket.py`, add:

```python
def _market_label(market: str) -> str:
    if market == "HK":
        return "港股"
    if market == "US":
        return "美股"
    return market


def _notification_title(kind: str, market: str) -> str:
    return f"Open Trader {_market_label(market)}{kind}"
```

Use:

```python
self._notify(_notification_title("阻塞通知", market), blocker_message, market=market, run_date=run_date)
self._notify(_notification_title("行动通知", market), message, market=market, run_date=run_date)
```

Update `_notify` signature:

```python
def _notify(self, title: str, message: str, *, market: str, run_date: str) -> None:
```

- [ ] **Step 4: Write market into notification logs**

Update `_write_notification_log` signature:

```python
def _write_notification_log(
    self,
    *,
    title: str,
    attempt: NotificationAttempt,
    market: str,
    run_date: str,
) -> None:
```

Use market-scoped path:

```python
log_dir = self.config.logs_dir / "notifications"
path = log_dir / f"{run_date}-{market}.csv"
```

Add `"market"` to the CSV fieldnames and row:

```python
fieldnames = [
    "sent_at",
    "market",
    "title",
    "channel",
    "success",
    "error_type",
    "error",
]
row = {
    "sent_at": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
    "market": market,
    "title": title,
    "channel": attempt.channel,
    "success": "true" if attempt.success else "false",
    "error_type": attempt.error_type,
    "error": attempt.error,
}
```

Update all `_notify` callers to pass `market` and `run_date`.

- [ ] **Step 5: Run daily notification tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'notification or market' -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: label notifications by market"
```

### Task 7: Add HK and US Launchd Jobs

**Files:**
- Modify: `ops/launchd/com.open-trader.premarket.plist.template`
- Modify: `scripts/install_daily_premarket_launchd.sh`
- Modify: `scripts/uninstall_daily_premarket_launchd.sh`
- Modify: `config/daily_premarket.env.example`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing launchd template test**

Update the existing launchd template test in `tests/test_daily_premarket.py` or add:

```python
def test_launchd_template_accepts_market_placeholder() -> None:
    template = Path("ops/launchd/com.open-trader.premarket.plist.template").read_text(
        encoding="utf-8"
    )

    assert "OPEN_TRADER_MARKET" in template
    assert "--market" in template
    assert "OPEN_TRADER_LABEL" in template
    assert "OPEN_TRADER_HOUR" in template
    assert "OPEN_TRADER_MINUTE" in template
```

- [ ] **Step 2: Run launchd template test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_launchd_template_accepts_market_placeholder -v
```

Expected: FAIL because the template has no market placeholders.

- [ ] **Step 3: Update plist template**

In `ops/launchd/com.open-trader.premarket.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
<key>Label</key>
<string>OPEN_TRADER_LABEL</string>

<key>WorkingDirectory</key>
<string>OPEN_TRADER_REPO</string>

<key>ProgramArguments</key>
<array>
<string>OPEN_TRADER_PYTHON</string>
<string>-m</string>
<string>open_trader</string>
<string>run-daily-premarket</string>
<string>--market</string>
<string>OPEN_TRADER_MARKET</string>
<string>--date</string>
<string>today</string>
<string>--config</string>
<string>OPEN_TRADER_REPO/config/daily_premarket.env</string>
</array>

<key>StartCalendarInterval</key>
<array>
<dict>
<key>Weekday</key>
<integer>1</integer>
<key>Hour</key>
<integer>OPEN_TRADER_HOUR</integer>
<key>Minute</key>
<integer>OPEN_TRADER_MINUTE</integer>
</dict>
<dict>
<key>Weekday</key>
<integer>2</integer>
<key>Hour</key>
<integer>OPEN_TRADER_HOUR</integer>
<key>Minute</key>
<integer>OPEN_TRADER_MINUTE</integer>
</dict>
<dict>
<key>Weekday</key>
<integer>3</integer>
<key>Hour</key>
<integer>OPEN_TRADER_HOUR</integer>
<key>Minute</key>
<integer>OPEN_TRADER_MINUTE</integer>
</dict>
<dict>
<key>Weekday</key>
<integer>4</integer>
<key>Hour</key>
<integer>OPEN_TRADER_HOUR</integer>
<key>Minute</key>
<integer>OPEN_TRADER_MINUTE</integer>
</dict>
<dict>
<key>Weekday</key>
<integer>5</integer>
<key>Hour</key>
<integer>OPEN_TRADER_HOUR</integer>
<key>Minute</key>
<integer>OPEN_TRADER_MINUTE</integer>
</dict>
</array>

<key>StandardOutPath</key>
<string>OPEN_TRADER_REPO/logs/daily_premarket/launchd-OPEN_TRADER_MARKET.out.log</string>

<key>StandardErrorPath</key>
<string>OPEN_TRADER_REPO/logs/daily_premarket/launchd-OPEN_TRADER_MARKET.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Update installer to install both jobs by default**

In `scripts/install_daily_premarket_launchd.sh`, support:

```bash
usage() {
  echo "usage: $0 [--dry-run] [--market HK|US|all]" >&2
}
```

Default `MARKET=all`. Render:

```bash
render_market() {
  local market="$1"
  local label hour minute target
  if [[ "$market" == "HK" ]]; then
    label="com.open-trader.premarket.hk"
    hour="8"
    minute="0"
  elif [[ "$market" == "US" ]]; then
    label="com.open-trader.premarket.us"
    hour="18"
    minute="30"
  else
    echo "unsupported market: $market" >&2
    exit 2
  fi
  target="$HOME/Library/LaunchAgents/$label.plist"
  sed \
    -e "s#OPEN_TRADER_LABEL#$(sed_replacement_escape "$(xml_escape "$label")")#g" \
    -e "s#OPEN_TRADER_MARKET#$market#g" \
    -e "s#OPEN_TRADER_HOUR#$hour#g" \
    -e "s#OPEN_TRADER_MINUTE#$minute#g" \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_REPO")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$TEMPLATE" > "$target"
  plutil -lint "$target"
  launchctl unload "$target" 2>/dev/null || true
  launchctl load "$target"
  echo "installed launchd agent: $target"
}
```

For `--dry-run`, print each rendered plist to stdout and do not write/load files.

- [ ] **Step 5: Update uninstaller**

In `scripts/uninstall_daily_premarket_launchd.sh`, remove:

```bash
com.open-trader.premarket.hk.plist
com.open-trader.premarket.us.plist
```

Also accept `--market HK|US|all` with default `all`.

- [ ] **Step 6: Update env example**

In `config/daily_premarket.env.example`, add comments:

```env
# HK daily workflow deadline is fixed by code at 09:00 Asia/Shanghai.
# US daily workflow uses OPEN_TRADER_DEADLINE.
OPEN_TRADER_DEADLINE=21:10
```

- [ ] **Step 7: Run launchd dry-run and lint manually**

Run:

```bash
scripts/install_daily_premarket_launchd.sh --dry-run --market HK > /tmp/open-trader-hk.plist
scripts/install_daily_premarket_launchd.sh --dry-run --market US > /tmp/open-trader-us.plist
plutil -lint /tmp/open-trader-hk.plist /tmp/open-trader-us.plist
```

Expected: both plist files are valid.

- [ ] **Step 8: Run launchd-related tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'launchd or market' -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add ops/launchd/com.open-trader.premarket.plist.template scripts/install_daily_premarket_launchd.sh scripts/uninstall_daily_premarket_launchd.sh config/daily_premarket.env.example tests/test_daily_premarket.py
git commit -m "feat: install separate hk and us launchd jobs"
```

### Task 8: Documentation, Backward Compatibility Checks, and End-to-End Verification

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `README.md`
- Modify: `docs/monthly_portfolio_import.md`
- Test/Verify: no new test file required unless previous tasks expose a documentation-tested command.

- [ ] **Step 1: Update Chinese README commands**

In `README.zh-CN.md`, update daily commands to include separate market runs:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env \
  --dry-run

.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

Document outputs:

```text
data/runs/<YYYY-MM-DD>/HK/
data/runs/<YYYY-MM-DD>/US/
data/latest/HK/
data/latest/US/
reports/daily_runs/<YYYY-MM-DD>-HK.md
reports/daily_runs/<YYYY-MM-DD>-US.md
```

Add a note that HK runs before 09:00 Asia/Shanghai, US uses `OPEN_TRADER_DEADLINE`.

- [ ] **Step 2: Update English README**

Mirror the same command and output changes in `README.md`.

- [ ] **Step 3: Update monthly import docs**

In `docs/monthly_portfolio_import.md`, add that HK stock and ETF positions are now AI-eligible and feed the separate HK daily workflow, while HK money market funds and cash stay excluded from AI analysis.

- [ ] **Step 4: Run focused test suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_market_scope.py \
  tests/test_portfolio.py \
  tests/test_advice_portfolio_loader.py \
  tests/test_premarket_pipeline.py \
  tests/test_trading_plan.py \
  tests/test_trade_actions.py \
  tests/test_futu_watch.py \
  tests/test_daily_premarket.py \
  tests/test_premarket_cli.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 6: Run market-scoped dry runs**

Run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env \
  --dry-run

.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

Expected:

```text
status: success
status_json: /Users/ray/projects/open_trader/data/runs/<date>/HK/daily_run_status.json
report: /Users/ray/projects/open_trader/reports/daily_runs/<date>-HK.md
```

and equivalent `US` paths for the US run. If run after the HK 09:00 deadline, fallback or partial status may be valid; inspect `daily_run_status.json` and verify the HK paths and `market` field are correct.

- [ ] **Step 7: Verify Futu HK quote support directly**

Run:

```bash
.venv/bin/python - <<'PY'
from futu import OpenQuoteContext
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
ret, data = ctx.get_market_snapshot(["HK.00700"])
print(ret, data)
ctx.close()
PY
```

Expected: `ret` is `0` and output includes a row for `HK.00700`. If `ret` is not `0` and message includes `网络中断`, inspect `get_global_state()` and recover OpenD quote login before claiming live verification.

- [ ] **Step 8: Run one real HK market run before deadline when possible**

Before 09:00 Asia/Shanghai, run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env
```

Expected:

```text
status: success
status_json: /Users/ray/projects/open_trader/data/runs/<date>/HK/daily_run_status.json
report: /Users/ray/projects/open_trader/reports/daily_runs/<date>-HK.md
```

Then inspect:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
today = Path("data/runs").glob("*/HK/daily_run_status.json")
latest = sorted(today)[-1]
payload = json.loads(latest.read_text(encoding="utf-8"))
print(latest)
print(payload["market"], payload["status"], payload["readiness"])
print(payload["deadline_at"])
print(payload["artifacts"]["latest_trading_plan"])
PY
```

Expected: `market` is `HK`, deadline ends with `09:00:00+08:00`, latest paths include `/latest/HK/`.

- [ ] **Step 9: Commit docs and verification notes**

```bash
git add README.md README.zh-CN.md docs/monthly_portfolio_import.md
git commit -m "docs: document hk and us market workflows"
```

### Final Verification

- [ ] Run full tests:

```bash
.venv/bin/python -m pytest
```

- [ ] Run dry-run commands for both markets:

```bash
.venv/bin/python -m open_trader run-daily-premarket --market HK --date today --config config/daily_premarket.env --dry-run
.venv/bin/python -m open_trader run-daily-premarket --market US --date today --config config/daily_premarket.env --dry-run
```

- [ ] Render and lint both launchd plists:

```bash
scripts/install_daily_premarket_launchd.sh --dry-run --market HK > /tmp/open-trader-hk.plist
scripts/install_daily_premarket_launchd.sh --dry-run --market US > /tmp/open-trader-us.plist
plutil -lint /tmp/open-trader-hk.plist /tmp/open-trader-us.plist
```

- [ ] If Futu OpenD is available, verify HK quote snapshots:

```bash
.venv/bin/python -m open_trader check-futu-quotes --portfolio data/latest/portfolio.csv
```

Expected: HK symbols such as `HK.00700` are either quoted or explicitly reported as missing; no HK stock/ETF is silently skipped as an unsupported market.
