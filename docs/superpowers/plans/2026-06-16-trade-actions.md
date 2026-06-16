# Trade Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `generate-trade-actions`, a command that combines structured trading plans, portfolio state, and live Futu quotes into explicit machine-readable trading action instructions.

**Architecture:** Add a focused `open_trader.trade_actions` module for CSV contracts, portfolio loading, action mapping, sizing, report rendering, and atomic output writes. Keep `open_trader.cli` thin: parse command arguments, call the action generator, fetch Futu snapshots through the existing `FutuQuoteClient`, and print a summary. Reuse `TradingPlanRow`, `PlanQuoteStatus`, `evaluate_plan_quote`, and `QuoteSnapshot` instead of duplicating quote trigger semantics.

**Tech Stack:** Python standard library `csv`, `dataclasses`, `decimal`, `pathlib`, `tempfile`; existing Futu quote client; pytest and monkeypatch-based CLI tests.

---

## File Structure

- Create `src/open_trader/trade_actions.py`
  - Owns `TRADE_ACTION_FIELDNAMES`, action/result dataclasses, portfolio CSV loading, action mapping, sizing, report rendering, atomic CSV/report writes, and the top-level `generate_trade_actions()` function.
- Modify `src/open_trader/cli.py`
  - Adds the `generate-trade-actions` subcommand and delegates all business logic to `trade_actions.py`.
- Create `tests/test_trade_actions.py`
  - Covers action mapping, sizing, row-level review states, CSV/report output, and dry-run latest behavior.
- Create `tests/test_trade_actions_cli.py`
  - Covers parser options, Futu quote client wiring, summary output, clean CLI errors, and preservation of `check-futu-plan` behavior.

---

### Task 1: Trade Action Contracts and Portfolio Loader

**Files:**
- Create: `src/open_trader/trade_actions.py`
- Create: `tests/test_trade_actions.py`

- [ ] **Step 1: Write failing tests for field order and portfolio loading**

Add this file:

```python
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from open_trader.trade_actions import (
    TRADE_ACTION_FIELDNAMES,
    PortfolioActionContext,
    load_portfolio_action_context,
)


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "sort_group",
        "market",
        "asset_class",
        "symbol",
        "name",
        "currency",
        "total_quantity",
        "avg_cost_price",
        "last_price",
        "market_value",
        "cost_value",
        "unrealized_pnl",
        "unrealized_pnl_pct",
        "fx_source",
        "fx_date",
        "fx_to_hkd",
        "market_value_hkd",
        "cost_value_hkd",
        "portfolio_weight_hkd",
        "brokers",
        "accounts",
        "ai_eligible",
        "analysis_symbol",
        "risk_flag",
        "confidence",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_trade_action_fieldnames_are_stable() -> None:
    assert TRADE_ACTION_FIELDNAMES == [
        "run_date",
        "symbol",
        "market",
        "futu_symbol",
        "action",
        "priority",
        "last_price",
        "trigger_status",
        "suggested_quantity",
        "suggested_notional",
        "notional_currency",
        "current_quantity",
        "current_weight",
        "target_max_weight",
        "cash_available",
        "limit_price",
        "stop_price",
        "reason",
        "source_plan",
        "status",
        "error",
    ]


def test_load_portfolio_action_context_indexes_positions_cash_and_total_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "sort_group": "1",
                "market": "US",
                "asset_class": "stock",
                "symbol": "MSFT",
                "name": "Microsoft",
                "currency": "USD",
                "total_quantity": "10",
                "avg_cost_price": "300",
                "last_price": "390",
                "market_value": "3900",
                "cost_value": "3000",
                "unrealized_pnl": "900",
                "unrealized_pnl_pct": "30.00%",
                "fx_source": "fixture",
                "fx_date": "2026-05-31",
                "fx_to_hkd": "7.8",
                "market_value_hkd": "30420",
                "cost_value_hkd": "23400",
                "portfolio_weight_hkd": "39.00%",
                "brokers": "futu",
                "accounts": "futu_main",
                "ai_eligible": "true",
                "analysis_symbol": "MSFT",
                "risk_flag": "normal",
                "confidence": "high",
                "notes": "",
            },
            {
                "sort_group": "5",
                "market": "CASH",
                "asset_class": "cash",
                "symbol": "USD_CASH",
                "name": "USD Cash",
                "currency": "USD",
                "total_quantity": "1",
                "avg_cost_price": "",
                "last_price": "",
                "market_value": "1000",
                "cost_value": "",
                "unrealized_pnl": "",
                "unrealized_pnl_pct": "",
                "fx_source": "fixture",
                "fx_date": "2026-05-31",
                "fx_to_hkd": "7.8",
                "market_value_hkd": "7800",
                "cost_value_hkd": "",
                "portfolio_weight_hkd": "10.00%",
                "brokers": "futu",
                "accounts": "futu_main",
                "ai_eligible": "false",
                "analysis_symbol": "",
                "risk_flag": "normal",
                "confidence": "high",
                "notes": "",
            },
        ],
    )

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={
            ("US", "MSFT"): {
                "currency": "USD",
                "quantity": Decimal("10"),
                "market_value": Decimal("3900"),
                "market_value_hkd": Decimal("30420"),
                "weight": Decimal("0.39"),
                "fx_to_hkd": Decimal("7.8"),
            }
        },
        cash_by_currency={"USD": Decimal("1000")},
        total_market_value_hkd=Decimal("38220"),
    )
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_trade_action_fieldnames_are_stable tests/test_trade_actions.py::test_load_portfolio_action_context_indexes_positions_cash_and_total_value -q
```

Expected: fail with `ModuleNotFoundError: No module named 'open_trader.trade_actions'`.

- [ ] **Step 3: Implement contracts and portfolio loader**

Create `src/open_trader/trade_actions.py` with:

```python
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


TRADE_ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "priority",
    "last_price",
    "trigger_status",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "current_quantity",
    "current_weight",
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
    "reason",
    "source_plan",
    "status",
    "error",
]

PORTFOLIO_REQUIRED_FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "currency",
    "total_quantity",
    "market_value",
    "fx_to_hkd",
    "market_value_hkd",
    "portfolio_weight_hkd",
]


@dataclass(frozen=True)
class PortfolioActionContext:
    positions: dict[tuple[str, str], dict[str, Decimal | str]]
    cash_by_currency: dict[str, Decimal]
    total_market_value_hkd: Decimal


def load_portfolio_action_context(portfolio_path: Path) -> PortfolioActionContext:
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = sorted(set(PORTFOLIO_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing portfolio column(s): {', '.join(missing)}")
        rows = [
            {
                column: "" if value is None else str(value)
                for column, value in row.items()
                if column
            }
            for row in reader
        ]

    positions: dict[tuple[str, str], dict[str, Decimal | str]] = {}
    cash_by_currency: dict[str, Decimal] = {}
    total_hkd = Decimal("0")
    for row in rows:
        market_value_hkd = _optional_decimal(row.get("market_value_hkd", ""))
        if market_value_hkd is not None:
            total_hkd += market_value_hkd

        market = row.get("market", "").strip().upper()
        asset_class = row.get("asset_class", "").strip().lower()
        symbol = row.get("symbol", "").strip().upper()
        currency = row.get("currency", "").strip().upper()

        if market == "CASH" or asset_class == "cash":
            cash_value = _optional_decimal(row.get("market_value", ""))
            if currency and cash_value is not None:
                cash_by_currency[currency] = cash_by_currency.get(currency, Decimal("0")) + cash_value
            continue

        quantity = _optional_decimal(row.get("total_quantity", ""))
        market_value = _optional_decimal(row.get("market_value", ""))
        weight = _optional_percent(row.get("portfolio_weight_hkd", ""))
        fx_to_hkd = _optional_decimal(row.get("fx_to_hkd", ""))
        if market and symbol:
            positions[(market, symbol)] = {
                "currency": currency,
                "quantity": quantity if quantity is not None else Decimal("0"),
                "market_value": market_value if market_value is not None else Decimal("0"),
                "market_value_hkd": market_value_hkd if market_value_hkd is not None else Decimal("0"),
                "weight": weight if weight is not None else Decimal("0"),
                "fx_to_hkd": fx_to_hkd if fx_to_hkd is not None else Decimal("0"),
            }
    return PortfolioActionContext(
        positions=positions,
        cash_by_currency=cash_by_currency,
        total_market_value_hkd=total_hkd,
    )


def _optional_decimal(value: str) -> Decimal | None:
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _optional_percent(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    if value.endswith("%"):
        parsed = _optional_decimal(value[:-1])
        return None if parsed is None else parsed / Decimal("100")
    parsed = _optional_decimal(value)
    return parsed
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_trade_action_fieldnames_are_stable tests/test_trade_actions.py::test_load_portfolio_action_context_indexes_positions_cash_and_total_value -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py
git commit -m "feat: add trade action portfolio context"
```

Expected: commit succeeds with only the new module and test file.

---

### Task 2: Action Mapping Without Sizing

**Files:**
- Modify: `src/open_trader/trade_actions.py`
- Modify: `tests/test_trade_actions.py`

- [ ] **Step 1: Add failing tests for quote-status to action mapping**

Append to `tests/test_trade_actions.py`:

```python
from open_trader.trade_actions import map_quote_status_to_action


def test_map_quote_status_to_trade_action() -> None:
    assert map_quote_status_to_action("stop_loss_hit") == ("SELL_STOP", "critical")
    assert map_quote_status_to_action("target_2_hit") == ("TAKE_PROFIT", "high")
    assert map_quote_status_to_action("target_1_hit") == ("TRIM", "medium")
    assert map_quote_status_to_action("entry_zone") == ("BUY", "high")
    assert map_quote_status_to_action("add_zone") == ("ADD", "medium")
    assert map_quote_status_to_action("watch") == ("HOLD", "low")
    assert map_quote_status_to_action("missing_quote") == ("REVIEW", "medium")
    assert map_quote_status_to_action("unexpected") == ("REVIEW", "medium")
```

- [ ] **Step 2: Run the mapping test and verify it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_map_quote_status_to_trade_action -q
```

Expected: fail with `ImportError` for `map_quote_status_to_action`.

- [ ] **Step 3: Implement action mapping**

Add this function to `src/open_trader/trade_actions.py`:

```python
def map_quote_status_to_action(trigger_status: str) -> tuple[str, str]:
    mapping = {
        "stop_loss_hit": ("SELL_STOP", "critical"),
        "target_2_hit": ("TAKE_PROFIT", "high"),
        "target_1_hit": ("TRIM", "medium"),
        "entry_zone": ("BUY", "high"),
        "add_zone": ("ADD", "medium"),
        "watch": ("HOLD", "low"),
        "missing_quote": ("REVIEW", "medium"),
    }
    return mapping.get(trigger_status, ("REVIEW", "medium"))
```

- [ ] **Step 4: Run the mapping test and verify it passes**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_map_quote_status_to_trade_action -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py
git commit -m "feat: map quote triggers to trade actions"
```

Expected: commit succeeds.

---

### Task 3: Trade Action Row Builder and Sizing

**Files:**
- Modify: `src/open_trader/trade_actions.py`
- Modify: `tests/test_trade_actions.py`

- [ ] **Step 1: Add failing tests for buy and sell sizing**

Append to `tests/test_trade_actions.py`:

```python
from open_trader.trading_plan import PlanQuoteStatus, TradingPlanRow
from open_trader.trade_actions import build_trade_action_row


def active_plan(
    *,
    symbol: str = "MSFT",
    max_weight: str = "12%",
    plan_text: str = "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
) -> TradingPlanRow:
    return TradingPlanRow(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        rating="Overweight",
        entry_zone_low=Decimal("380"),
        entry_zone_high=Decimal("400"),
        add_price=Decimal("350"),
        stop_loss=Decimal("340"),
        target_1=Decimal("450"),
        target_2=Decimal("500"),
        max_weight=max_weight,
        catalyst="10月底财报",
        time_horizon="3-6个月",
        plan_text=plan_text,
        status="active",
        error="",
    )


def portfolio_context(*, quantity: str = "10", cash: str = "1000") -> PortfolioActionContext:
    return PortfolioActionContext(
        positions={
            ("US", "MSFT"): {
                "currency": "USD",
                "quantity": Decimal(quantity),
                "market_value": Decimal("3900"),
                "market_value_hkd": Decimal("30420"),
                "weight": Decimal("0.039"),
                "fx_to_hkd": Decimal("7.8"),
            }
        },
        cash_by_currency={"USD": Decimal(cash)},
        total_market_value_hkd=Decimal("780000"),
    )


def quote_status(trigger_status: str, price: str = "390") -> PlanQuoteStatus:
    return PlanQuoteStatus(
        symbol="MSFT",
        futu_symbol="US.MSFT",
        last_price=Decimal(price),
        status=trigger_status,
        message="fixture message",
    )


def test_buy_action_uses_plan_ratio_target_cap_and_cash_cap() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", "390"),
        portfolio=portfolio_context(cash="1000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_notional"] == "1000"
    assert row["suggested_quantity"] == "2"
    assert row["cash_available"] == "1000"
    assert row["limit_price"] == "390"
    assert row["stop_price"] == "340"


def test_buy_action_is_review_when_budget_is_below_one_share() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", "390"),
        portfolio=portfolio_context(cash="100"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert row["suggested_quantity"] == ""
    assert "below one share" in row["error"]


def test_add_action_defaults_to_40_percent_when_plan_ratio_is_missing() -> None:
    row = build_trade_action_row(
        plan=active_plan(plan_text="操作计划：350美元附近加仓。"),
        quote_status=quote_status("add_zone", "350"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "ADD"
    assert row["status"] == "ready"
    assert row["suggested_notional"] == "4550"
    assert row["suggested_quantity"] == "13"


def test_stop_loss_sells_full_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("stop_loss_hit", "339"),
        portfolio=portfolio_context(quantity="10"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "SELL_STOP"
    assert row["priority"] == "critical"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "10"
    assert row["suggested_notional"] == "3390"
    assert row["stop_price"] == "340"


def test_target_one_trims_half_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_1_hit", "451"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "4"
    assert row["suggested_notional"] == "1804"


def test_target_two_takes_profit_on_full_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_2_hit", "501"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TAKE_PROFIT"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "9"
    assert row["suggested_notional"] == "4509"
```

- [ ] **Step 2: Run the sizing tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_buy_action_uses_plan_ratio_target_cap_and_cash_cap tests/test_trade_actions.py::test_buy_action_is_review_when_budget_is_below_one_share tests/test_trade_actions.py::test_add_action_defaults_to_40_percent_when_plan_ratio_is_missing tests/test_trade_actions.py::test_stop_loss_sells_full_position tests/test_trade_actions.py::test_target_one_trims_half_position tests/test_trade_actions.py::test_target_two_takes_profit_on_full_position -q
```

Expected: fail with `ImportError` for `build_trade_action_row`.

- [ ] **Step 3: Implement row builder and sizing helpers**

Update imports in `src/open_trader/trade_actions.py`:

```python
import re
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .trading_plan import PlanQuoteStatus, TradingPlanRow
```

Add these functions:

```python
def build_trade_action_row(
    *,
    plan: TradingPlanRow,
    quote_status: PlanQuoteStatus,
    portfolio: PortfolioActionContext,
    source_plan: str,
) -> dict[str, str]:
    action, priority = map_quote_status_to_action(quote_status.status)
    position = portfolio.positions.get((plan.market.upper(), plan.symbol.upper()))
    currency = str(position.get("currency")) if position else _default_currency(plan.market)
    current_quantity = _decimal_from_position(position, "quantity")
    current_market_value = _decimal_from_position(position, "market_value")
    current_weight = _decimal_from_position(position, "weight")
    cash_available = portfolio.cash_by_currency.get(currency, Decimal("0"))
    target_max_weight = _optional_percent(plan.max_weight)

    base = {
        "run_date": plan.run_date,
        "symbol": plan.symbol,
        "market": plan.market,
        "futu_symbol": quote_status.futu_symbol,
        "action": action,
        "priority": priority,
        "last_price": _decimal_to_text(quote_status.last_price),
        "trigger_status": quote_status.status,
        "suggested_quantity": "",
        "suggested_notional": "",
        "notional_currency": currency,
        "current_quantity": _decimal_to_text(current_quantity),
        "current_weight": _percent_to_text(current_weight),
        "target_max_weight": plan.max_weight,
        "cash_available": _decimal_to_text(cash_available),
        "limit_price": "",
        "stop_price": _decimal_to_text(plan.stop_loss),
        "reason": quote_status.message,
        "source_plan": source_plan,
        "status": "watch" if action == "HOLD" else "review" if action == "REVIEW" else "ready",
        "error": "",
    }

    if action == "HOLD":
        return base
    if action == "REVIEW":
        base["status"] = "review"
        base["error"] = quote_status.message
        return base
    if action in {"SELL_STOP", "TAKE_PROFIT", "TRIM"}:
        return _size_sell_action(base, action, current_quantity, quote_status.last_price)
    return _size_buy_action(
        base=base,
        action=action,
        plan=plan,
        position=position,
        portfolio=portfolio,
        current_market_value=current_market_value,
        target_max_weight=target_max_weight,
        cash_available=cash_available,
        last_price=quote_status.last_price,
    )


def _size_sell_action(
    row: dict[str, str],
    action: str,
    current_quantity: Decimal,
    last_price: Decimal,
) -> dict[str, str]:
    if current_quantity <= 0:
        row["action"] = "REVIEW"
        row["status"] = "review"
        row["error"] = "missing current position for sell action"
        return row
    quantity = current_quantity
    if action == "TRIM":
        quantity = (current_quantity * Decimal("0.5")).to_integral_value(rounding=ROUND_DOWN)
    if quantity < 1:
        row["action"] = "REVIEW"
        row["status"] = "review"
        row["error"] = "sell quantity below one share"
        return row
    row["suggested_quantity"] = _decimal_to_text(quantity)
    row["suggested_notional"] = _decimal_to_text(quantity * last_price)
    row["limit_price"] = _decimal_to_text(last_price)
    row["status"] = "ready"
    return row


def _size_buy_action(
    *,
    base: dict[str, str],
    action: str,
    plan: TradingPlanRow,
    position: dict[str, Decimal | str] | None,
    portfolio: PortfolioActionContext,
    current_market_value: Decimal,
    target_max_weight: Decimal | None,
    cash_available: Decimal,
    last_price: Decimal,
) -> dict[str, str]:
    if target_max_weight is None:
        base["action"] = "REVIEW"
        base["status"] = "review"
        base["error"] = "unparseable target_max_weight"
        return base
    if position is None:
        base["action"] = "REVIEW"
        base["status"] = "review"
        base["error"] = "missing portfolio position row for target currency and fx"
        return base
    fx_to_hkd = _decimal_from_position(position, "fx_to_hkd")
    if fx_to_hkd <= 0:
        base["action"] = "REVIEW"
        base["status"] = "review"
        base["error"] = "missing fx_to_hkd for target symbol"
        return base
    portfolio_value = portfolio.total_market_value_hkd / fx_to_hkd
    target_budget = portfolio_value * target_max_weight
    remaining_target_budget = target_budget - current_market_value
    plan_ratio = _plan_ratio(plan.plan_text, action)
    plan_budget = target_budget * plan_ratio
    suggested_notional = min(plan_budget, remaining_target_budget, cash_available)
    if suggested_notional <= 0:
        base["action"] = "REVIEW"
        base["status"] = "review"
        base["error"] = "no remaining target budget or cash"
        return base
    quantity = (suggested_notional / last_price).to_integral_value(rounding=ROUND_DOWN)
    if quantity < 1:
        base["action"] = "REVIEW"
        base["status"] = "review"
        base["error"] = "suggested quantity below one share"
        return base
    notional = quantity * last_price
    base["suggested_quantity"] = _decimal_to_text(quantity)
    base["suggested_notional"] = _decimal_to_text(notional)
    base["limit_price"] = _decimal_to_text(last_price)
    base["status"] = "ready"
    return base


def _plan_ratio(plan_text: str, action: str) -> Decimal:
    if action == "ADD":
        match = re.search(r"(?:加仓|加碼|加碼).*?(\d+(?:\.\d+)?)\s*%", plan_text)
        if match:
            return Decimal(match.group(1)) / Decimal("100")
        return Decimal("0.4")
    match = re.search(r"(?:买入|買入|建仓|建倉).*?(\d+(?:\.\d+)?)\s*%", plan_text)
    if match:
        return Decimal(match.group(1)) / Decimal("100")
    return Decimal("0.6")


def _decimal_from_position(
    position: dict[str, Decimal | str] | None,
    key: str,
) -> Decimal:
    if position is None:
        return Decimal("0")
    value = position.get(key)
    return value if isinstance(value, Decimal) else Decimal("0")


def _default_currency(market: str) -> str:
    market = market.upper()
    if market == "US":
        return "USD"
    if market == "HK":
        return "HKD"
    return ""


def _decimal_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _percent_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{(value * Decimal('100')).normalize():f}%"
```

- [ ] **Step 4: Run the sizing tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py -q
```

Expected: all current `test_trade_actions.py` tests pass.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py
git commit -m "feat: size trade action rows"
```

Expected: commit succeeds.

---

### Task 4: Batch Generator, CSV Writes, and Markdown Report

**Files:**
- Modify: `src/open_trader/trade_actions.py`
- Modify: `tests/test_trade_actions.py`

- [ ] **Step 1: Add failing tests for batch generation and dry-run latest behavior**

Append to `tests/test_trade_actions.py`:

```python
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trade_actions import TradeActionsResult, generate_trade_actions


def write_trading_plan(path: Path, rows: list[dict[str, str]]) -> None:
    from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def msft_plan_row() -> dict[str, str]:
    return {
        "run_date": "2026-06-16",
        "symbol": "MSFT",
        "market": "US",
        "rating": "Overweight",
        "entry_zone_low": "380",
        "entry_zone_high": "400",
        "add_price": "350",
        "stop_loss": "340",
        "target_1": "450",
        "target_2": "500",
        "max_weight": "12%",
        "catalyst": "10月底财报",
        "time_horizon": "3-6个月",
        "plan_text": "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
        "status": "active",
        "error": "",
    }


def test_generate_trade_actions_writes_csv_report_and_latest(tmp_path: Path) -> None:
    plan_path = tmp_path / "data/latest/trading_plan.csv"
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_trading_plan(plan_path, [msft_plan_row()])
    write_portfolio(
        portfolio_path,
        [
            {
                "sort_group": "1",
                "market": "US",
                "asset_class": "stock",
                "symbol": "MSFT",
                "name": "Microsoft",
                "currency": "USD",
                "total_quantity": "10",
                "avg_cost_price": "300",
                "last_price": "390",
                "market_value": "3900",
                "cost_value": "3000",
                "unrealized_pnl": "900",
                "unrealized_pnl_pct": "30.00%",
                "fx_source": "fixture",
                "fx_date": "2026-05-31",
                "fx_to_hkd": "7.8",
                "market_value_hkd": "30420",
                "cost_value_hkd": "23400",
                "portfolio_weight_hkd": "3.90%",
                "brokers": "futu",
                "accounts": "futu_main",
                "ai_eligible": "true",
                "analysis_symbol": "MSFT",
                "risk_flag": "normal",
                "confidence": "high",
                "notes": "",
            },
            {
                "sort_group": "5",
                "market": "CASH",
                "asset_class": "cash",
                "symbol": "USD_CASH",
                "name": "USD Cash",
                "currency": "USD",
                "total_quantity": "1",
                "avg_cost_price": "",
                "last_price": "",
                "market_value": "1000",
                "cost_value": "",
                "unrealized_pnl": "",
                "unrealized_pnl_pct": "",
                "fx_source": "fixture",
                "fx_date": "2026-05-31",
                "fx_to_hkd": "7.8",
                "market_value_hkd": "7800",
                "cost_value_hkd": "",
                "portfolio_weight_hkd": "1.00%",
                "brokers": "futu",
                "accounts": "futu_main",
                "ai_eligible": "false",
                "analysis_symbol": "",
                "risk_flag": "normal",
                "confidence": "high",
                "notes": "",
            },
        ],
    )

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        snapshots={"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("390"))},
        run_date=None,
        update_latest=True,
    )

    assert result == TradeActionsResult(
        run_date="2026-06-16",
        action_count=1,
        ready_count=1,
        review_count=0,
        watch_count=0,
        actions_path=tmp_path / "data/runs/2026-06-16/trade_actions.csv",
        latest_path=tmp_path / "data/latest/trade_actions.csv",
        report_path=tmp_path / "reports/trade_actions/2026-06-16.md",
    )
    rows = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))
    assert rows[0]["action"] == "BUY"
    assert rows[0]["status"] == "ready"
    assert result.latest_path.read_text(encoding="utf-8") == result.actions_path.read_text(encoding="utf-8")
    report = result.report_path.read_text(encoding="utf-8")
    assert "行动：BUY" in report
    assert "标的：US.MSFT" in report
    assert "建议：买入" in report


def test_generate_trade_actions_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    latest_path = tmp_path / "data/latest/trade_actions.csv"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text("old latest", encoding="utf-8")
    plan_path = tmp_path / "data/latest/trading_plan.csv"
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_trading_plan(plan_path, [msft_plan_row()])
    write_portfolio(portfolio_path, [])

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        snapshots={},
        run_date="2026-06-16",
        update_latest=False,
    )

    assert result.actions_path.exists()
    assert result.report_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "old latest"
```

- [ ] **Step 2: Run the batch tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py::test_generate_trade_actions_writes_csv_report_and_latest tests/test_trade_actions.py::test_generate_trade_actions_dry_run_does_not_update_latest -q
```

Expected: fail with `ImportError` for `TradeActionsResult` or `generate_trade_actions`.

- [ ] **Step 3: Implement batch generation and output writers**

Add imports to `src/open_trader/trade_actions.py`:

```python
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping

from .futu_watch import QuoteSnapshot
from .trading_plan import evaluate_plan_quote, load_trading_plan_rows
```

Add result dataclass and generator:

```python
@dataclass(frozen=True)
class TradeActionsResult:
    run_date: str
    action_count: int
    ready_count: int
    review_count: int
    watch_count: int
    actions_path: Path
    latest_path: Path
    report_path: Path


def generate_trade_actions(
    *,
    plan_path: Path,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    snapshots: dict[str, QuoteSnapshot],
    run_date: str | None,
    update_latest: bool,
) -> TradeActionsResult:
    plans = [plan for plan in load_trading_plan_rows(plan_path) if plan.status == "active"]
    effective_run_date = run_date or _latest_run_date(plans)
    plans = [
        plan
        for plan in plans
        if not plan.run_date or plan.run_date == effective_run_date
    ]
    portfolio = load_portfolio_action_context(portfolio_path)
    rows: list[dict[str, str]] = []
    for plan in plans:
        quote = snapshots.get(plan.futu_symbol)
        if quote is None:
            quote_status = PlanQuoteStatus(
                symbol=plan.symbol,
                futu_symbol=plan.futu_symbol,
                last_price=Decimal("0"),
                status="missing_quote",
                message="Futu did not return a quote.",
            )
        else:
            quote_status = evaluate_plan_quote(plan, quote.last_price)
        rows.append(
            build_trade_action_row(
                plan=plan,
                quote_status=quote_status,
                portfolio=portfolio,
                source_plan=str(plan_path),
            )
        )
    actions_path = data_dir / "runs" / effective_run_date / "trade_actions.csv"
    latest_path = data_dir / "latest" / "trade_actions.csv"
    report_path = reports_dir / "trade_actions" / f"{effective_run_date}.md"
    _atomic_write_csv(actions_path, TRADE_ACTION_FIELDNAMES, rows)
    _atomic_write_text(report_path, render_trade_actions_report(effective_run_date, rows))
    if update_latest:
        _atomic_write_csv(latest_path, TRADE_ACTION_FIELDNAMES, rows)
    return TradeActionsResult(
        run_date=effective_run_date,
        action_count=len(rows),
        ready_count=sum(1 for row in rows if row["status"] == "ready"),
        review_count=sum(1 for row in rows if row["status"] == "review"),
        watch_count=sum(1 for row in rows if row["status"] == "watch"),
        actions_path=actions_path,
        latest_path=latest_path,
        report_path=report_path,
    )
```

Add report and writer helpers:

```python
def render_trade_actions_report(run_date: str, rows: list[dict[str, str]]) -> str:
    lines = [f"# Trade Actions - {run_date}", ""]
    if not rows:
        lines.append("No active trading actions were generated.")
        return "\n".join(lines) + "\n"
    for row in sorted(rows, key=_priority_sort_key):
        lines.extend(
            [
                f"## {row['priority']} {row['action']} {row['futu_symbol']}",
                "",
                f"行动：{row['action']}",
                f"标的：{row['futu_symbol']}",
                f"优先级：{row['priority']}",
                f"价格：{row['last_price']}",
                f"建议：{_suggestion_text(row)}",
                f"条件：{row['reason']}",
                f"风控：止损 {row['stop_price']}",
                f"原因：来自 {row['source_plan']}，计划仓位上限 {row['target_max_weight']}",
                f"状态：{row['status']}",
                "",
            ]
        )
    return "\n".join(lines)


def _suggestion_text(row: dict[str, str]) -> str:
    if row["status"] == "watch":
        return "继续观察，不建议交易"
    if row["status"] == "review":
        return f"需要人工复核：{row['error']}"
    verb = {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "SELL_STOP": "止损卖出",
        "TAKE_PROFIT": "止盈卖出",
    }.get(row["action"], row["action"])
    return (
        f"{verb} {row['suggested_quantity']} 股，"
        f"预算约 {row['notional_currency']} {row['suggested_notional']}"
    )


def _latest_run_date(plans: list[TradingPlanRow]) -> str:
    dates = sorted({plan.run_date for plan in plans if plan.run_date})
    if not dates:
        raise ValueError("--date is required when trading plan has no active run_date rows")
    return dates[-1]


def _priority_sort_key(row: dict[str, str]) -> tuple[int, str]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return (order.get(row["priority"], 9), row["futu_symbol"])


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
```

- [ ] **Step 4: Run batch tests and all core trade action tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py -q
```

Expected: all `test_trade_actions.py` tests pass.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py
git commit -m "feat: generate trade action outputs"
```

Expected: commit succeeds.

---

### Task 5: CLI Command Wiring

**Files:**
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_trade_actions_cli.py`

- [ ] **Step 1: Add failing CLI tests**

Create `tests/test_trade_actions_cli.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trade_actions import TradeActionsResult


def test_generate_trade_actions_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["generate-trade-actions", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--plan" in output
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--date" in output
    assert "--dry-run" in output
    assert "--host" in output
    assert "--port" in output


def test_generate_trade_actions_main_fetches_quotes_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakePlan:
        futu_symbol = "US.MSFT"
        status = "active"

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            captured["symbols"] = symbols
            return {
                "US.MSFT": QuoteSnapshot(
                    futu_symbol="US.MSFT",
                    last_price=Decimal("390"),
                )
            }

        def close(self) -> None:
            captured["closed"] = True

    def fake_generate_trade_actions(**kwargs: object) -> TradeActionsResult:
        captured["generate_kwargs"] = kwargs
        return TradeActionsResult(
            run_date="2026-06-16",
            action_count=1,
            ready_count=1,
            review_count=0,
            watch_count=0,
            actions_path=tmp_path / "data/runs/2026-06-16/trade_actions.csv",
            latest_path=tmp_path / "data/latest/trade_actions.csv",
            report_path=tmp_path / "reports/trade_actions/2026-06-16.md",
        )

    monkeypatch.setattr(cli, "load_trading_plan_rows", lambda path: [FakePlan()])
    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "generate_trade_actions", fake_generate_trade_actions)

    result = cli.main(
        [
            "generate-trade-actions",
            "--plan",
            str(tmp_path / "trading_plan.csv"),
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-16",
            "--dry-run",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
        ]
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    assert captured["symbols"] == ["US.MSFT"]
    assert captured["closed"] is True
    generate_kwargs = captured["generate_kwargs"]
    assert isinstance(generate_kwargs, dict)
    assert generate_kwargs["plan_path"] == tmp_path / "trading_plan.csv"
    assert generate_kwargs["portfolio_path"] == tmp_path / "portfolio.csv"
    assert generate_kwargs["run_date"] == "2026-06-16"
    assert generate_kwargs["update_latest"] is False
    output = capsys.readouterr().out
    assert "connected to Futu OpenD at 127.0.0.1:11111" in output
    assert "loaded 1 active trading plan(s)" in output
    assert "actions: 1" in output
    assert "ready: 1" in output
    assert "trade_actions_csv:" in output
    assert "report:" in output


def test_generate_trade_actions_main_reports_clean_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "load_trading_plan_rows",
        lambda path: (_ for _ in ()).throw(ValueError("missing trading plan column(s): symbol")),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["generate-trade-actions"])

    assert exc_info.value.code == 2
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions_cli.py -q
```

Expected: fail because the `generate-trade-actions` parser and CLI branch do not exist.

- [ ] **Step 3: Wire parser imports**

Modify imports in `src/open_trader/cli.py`:

```python
from .trade_actions import generate_trade_actions
```

Keep the existing `load_trading_plan_rows` import from `.trading_plan` because the CLI uses it to discover Futu symbols before fetching snapshots.

- [ ] **Step 4: Add parser command**

Add this block in `build_parser()` after `check-futu-plan`:

```python
    trade_actions_parser = subparsers.add_parser(
        "generate-trade-actions",
        help="Generate explicit trade action instructions from plans and Futu quotes",
    )
    trade_actions_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("data/latest/trading_plan.csv"),
    )
    trade_actions_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    trade_actions_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    trade_actions_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    trade_actions_parser.add_argument("--date", type=canonical_date)
    trade_actions_parser.add_argument("--host", default="127.0.0.1")
    trade_actions_parser.add_argument("--port", type=positive_int, default=11111)
    trade_actions_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run output and report but do not update latest trade actions",
    )
```

- [ ] **Step 5: Add CLI branch**

Add this branch in `main()` before the final unknown-command error:

```python
    if args.command == "generate-trade-actions":
        quote_client = None
        try:
            plans = [plan for plan in load_trading_plan_rows(args.plan) if plan.status == "active"]
            quote_client = FutuQuoteClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            print(f"loaded {len(plans)} active trading plan(s)")
            symbols = sorted({plan.futu_symbol for plan in plans})
            snapshots = quote_client.get_snapshots(symbols) if symbols else {}
            result = generate_trade_actions(
                plan_path=args.plan,
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                snapshots=snapshots,
                run_date=args.date,
                update_latest=not args.dry_run,
            )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))
        finally:
            if quote_client is not None:
                quote_client.close()
        print(f"actions: {result.action_count}")
        print(f"ready: {result.ready_count}")
        print(f"review: {result.review_count}")
        print(f"watch: {result.watch_count}")
        print(f"trade_actions_csv: {result.actions_path}")
        print(f"report: {result.report_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 6: Run CLI tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions_cli.py -q
```

Expected: all CLI tests pass.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add src/open_trader/cli.py tests/test_trade_actions_cli.py
git commit -m "feat: add trade actions cli"
```

Expected: commit succeeds.

---

### Task 6: Regression, Integration Check, and Documentation Update

**Files:**
- Modify: `docs/monthly_portfolio_import.md`
- Modify: `src/open_trader/trade_actions.py` only when verification exposes an implementation defect.
- Modify: `tests/test_trade_actions.py` only when verification exposes an uncovered contract or stale assertion.
- Modify: `tests/test_trade_actions_cli.py` only when verification exposes an uncovered CLI contract or stale assertion.

- [ ] **Step 1: Add usage docs**

Append this section to `docs/monthly_portfolio_import.md`:

````markdown
## Generate trade actions from live quotes

After `trading_plan.csv` exists and Futu OpenD is running, generate explicit
action instructions:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader generate-trade-actions \
  --plan data/latest/trading_plan.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-16
```

The command writes:

- `data/runs/2026-06-16/trade_actions.csv`
- `data/latest/trade_actions.csv`
- `reports/trade_actions/2026-06-16.md`

The CSV is the machine-readable contract for future automation. The Markdown
report is only a human-readable rendering. No orders are placed by this command.

Use `--dry-run` to write the dated CSV and report without updating
`data/latest/trade_actions.csv`.
````

- [ ] **Step 2: Run full focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trade_actions.py tests/test_trade_actions_cli.py tests/test_trading_plan.py tests/test_trading_plan_cli.py -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the full suite**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Run a dry command if Futu OpenD is available**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader generate-trade-actions --dry-run
```

Expected when Futu OpenD is running: command prints connection, counts, dated CSV path, report path, and latest path without replacing `data/latest/trade_actions.csv`.

Expected when Futu OpenD is not running: command fails cleanly with a message like `Futu OpenD is not reachable at 127.0.0.1:11111`. This is acceptable for local verification if the test suite passes.

- [ ] **Step 5: Commit Task 6**

Run:

```bash
git add docs/monthly_portfolio_import.md src/open_trader/trade_actions.py tests/test_trade_actions.py tests/test_trade_actions_cli.py
git commit -m "docs: document trade action generation"
```

Expected: commit succeeds if docs or verification fixes changed files. If only tests were run and no files changed, skip this commit.

---

## Final Verification Checklist

- [ ] `PYTHONPATH=src .venv/bin/python -m pytest -q` passes.
- [ ] `generate-trade-actions --help` shows `--plan`, `--portfolio`, `--data-dir`, `--reports-dir`, `--date`, `--dry-run`, `--host`, and `--port`.
- [ ] `check-futu-plan` behavior remains diagnostic and unchanged.
- [ ] `trade_actions.csv` field order matches `TRADE_ACTION_FIELDNAMES`.
- [ ] `data/latest/trade_actions.csv` is not updated by `--dry-run`.
- [ ] Missing quote or incomplete sizing data produces row-level `REVIEW`, not a batch failure.
- [ ] Futu connection failure remains a clean batch-level CLI error.
