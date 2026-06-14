# Monthly Portfolio Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the monthly PDF-statement import pipeline that produces the single user-facing `portfolio.csv`.

**Architecture:** Implement a small Python package with focused modules for broker PDF parsing, normalized records, month-end FX conversion, portfolio merging, and CSV writing. The CLI reads Futu, Tiger, and Phillip PDF statements for one month, writes traceable intermediate files under `data/runs/YYYY-MM/`, and updates `data/latest/portfolio.csv`.

**Tech Stack:** Python 3.12, `pdfplumber`, standard-library `csv`, `argparse`, `dataclasses`, `pytest`, local `.venv`.

---

## Scope

This plan implements the first independent subsystem from the spec:

```text
PDF statements -> extracted_positions.csv / extracted_cash.csv -> portfolio.csv
```

TradingAgents analysis, `analysis_results.csv`, and `watchlist.csv` are intentionally deferred to a second implementation plan. They depend on a reliable `portfolio.csv`.

## File Structure

Create:

- `pyproject.toml`: package metadata, dependencies, pytest config.
- `src/open_trader/__init__.py`: package marker and version.
- `src/open_trader/__main__.py`: `python -m open_trader` entrypoint.
- `src/open_trader/cli.py`: command parsing and import command orchestration.
- `src/open_trader/models.py`: normalized dataclasses and enums.
- `src/open_trader/csv_io.py`: CSV writing helpers.
- `src/open_trader/fx.py`: month-end FX provider and conversion helpers.
- `src/open_trader/portfolio.py`: merge, sort, HKD weights, risk flags.
- `src/open_trader/parsers/__init__.py`: parser exports.
- `src/open_trader/parsers/base.py`: parser protocol and shared utilities.
- `src/open_trader/parsers/futu.py`: Futu PDF parser.
- `src/open_trader/parsers/tiger.py`: Tiger PDF parser.
- `src/open_trader/parsers/phillips.py`: Phillip PDF parser.
- `src/open_trader/pipeline.py`: end-to-end import pipeline.
- `tests/fixtures/pdf_text/futu.txt`: sanitized text fixture from Futu statement.
- `tests/fixtures/pdf_text/tiger.txt`: sanitized text fixture from Tiger statement.
- `tests/fixtures/pdf_text/phillips.txt`: sanitized text fixture from Phillip statement.
- `tests/test_fx.py`: FX conversion tests.
- `tests/test_portfolio.py`: merge, sort, risk tests.
- `tests/test_parsers_text.py`: parser behavior against sanitized text fixtures.
- `tests/test_pipeline.py`: end-to-end pipeline tests using fake parsers.

Modify:

- `.gitignore`: ignore generated `data/` outputs and test caches while keeping source code and docs tracked.

## Task 1: Package Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/open_trader/__init__.py`
- Create: `src/open_trader/__main__.py`
- Create: `src/open_trader/cli.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the package metadata**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=80"]
build-backend = "setuptools.build_meta"

[project]
name = "open-trader"
version = "0.1.0"
description = "Monthly portfolio aggregation and trading analysis tools"
requires-python = ">=3.12"
dependencies = [
    "pdfplumber>=0.11.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]

[project.scripts]
open-trader = "open_trader.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

- [ ] **Step 2: Create the package marker**

Create `src/open_trader/__init__.py`:

```python
"""Open Trader portfolio tooling."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create the module entrypoint**

Create `src/open_trader/__main__.py`:

```python
from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create the initial CLI command surface**

Create `src/open_trader/cli.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-statements",
        help="Import monthly broker statements and generate portfolio.csv",
    )
    import_parser.add_argument("--month", required=True, help="Statement month, YYYY-MM")
    import_parser.add_argument("--futu", type=Path, required=True)
    import_parser.add_argument("--tiger", type=Path, required=True)
    import_parser.add_argument("--phillips", type=Path, required=True)
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-statements":
        parser.error("import-statements is not implemented yet")

    parser.error(f"unknown command: {args.command}")
    return 2
```

- [ ] **Step 5: Update `.gitignore`**

Modify `.gitignore` so it contains:

```gitignore
.venv/
.tradingagents/
data/
.pytest_cache/
__pycache__/
*.py[cod]
```

- [ ] **Step 6: Install package in editable mode**

Run:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

Expected: command exits `0`, and output includes `Successfully installed open-trader`.

- [ ] **Step 7: Verify CLI help works**

Run:

```bash
.venv/bin/python -m open_trader --help
```

Expected: command exits `0`, and output includes `import-statements`.

- [ ] **Step 8: Commit scaffold**

```bash
git add .gitignore pyproject.toml src/open_trader
git commit -m "feat: scaffold open trader package"
```

## Task 2: Normalized Models

**Files:**
- Create: `src/open_trader/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_models.py`:

```python
from decimal import Decimal

from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord


def test_position_identity_key_merges_by_market_asset_symbol_currency():
    position = Position(
        statement_id="2026-05-futu",
        broker="futu",
        account_alias="futu_main",
        market=Market.US,
        asset_class=AssetClass.STOCK,
        symbol="NVDA",
        name="NVIDIA",
        currency="USD",
        quantity=Decimal("10"),
        cost_price=Decimal("120"),
        last_price=Decimal("130"),
        market_value=Decimal("1300"),
        cost_value=Decimal("1200"),
        unrealized_pnl=Decimal("100"),
        confidence="high",
        notes="",
    )

    assert position.identity_key() == (Market.US, AssetClass.STOCK, "NVDA", "USD")


def test_cash_balance_uses_synthetic_symbol():
    cash = CashBalance(
        statement_id="2026-05-tiger",
        broker="tiger",
        account_alias="tiger_main",
        currency="USD",
        cash_balance=Decimal("1000"),
        available_balance=Decimal("900"),
        confidence="high",
        notes="",
    )

    assert cash.symbol == "USD_CASH"
    assert cash.market == Market.CASH
    assert cash.asset_class == AssetClass.CASH


def test_warning_record_has_stable_csv_fields():
    warning = WarningRecord(
        statement_id="2026-05-phillips",
        broker="phillips",
        page=2,
        severity="warning",
        code="missing_cost",
        message="Missing cost value for 00001",
    )

    assert warning.to_row()["code"] == "missing_cost"
    assert warning.to_row()["page"] == "2"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.models'`.

- [ ] **Step 3: Implement models**

Create `src/open_trader/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal


Confidence = Literal["high", "medium", "low"]
RiskFlag = Literal["normal", "overweight", "data_check"]


class Market(StrEnum):
    US = "US"
    HK = "HK"
    OTHER = "OTHER"
    CASH = "CASH"


class AssetClass(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    FUND = "fund"
    MONEY_MARKET_FUND = "money_market_fund"
    OPTION = "option"
    CASH = "cash"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Position:
    statement_id: str
    broker: str
    account_alias: str
    market: Market
    asset_class: AssetClass
    symbol: str
    name: str
    currency: str
    quantity: Decimal
    cost_price: Decimal | None
    last_price: Decimal | None
    market_value: Decimal | None
    cost_value: Decimal | None
    unrealized_pnl: Decimal | None
    confidence: Confidence
    notes: str

    def identity_key(self) -> tuple[Market, AssetClass, str, str]:
        return (self.market, self.asset_class, self.symbol.upper(), self.currency.upper())


@dataclass(frozen=True)
class CashBalance:
    statement_id: str
    broker: str
    account_alias: str
    currency: str
    cash_balance: Decimal
    available_balance: Decimal | None
    confidence: Confidence
    notes: str

    @property
    def market(self) -> Market:
        return Market.CASH

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CASH

    @property
    def symbol(self) -> str:
        return f"{self.currency.upper()}_CASH"


@dataclass(frozen=True)
class WarningRecord:
    statement_id: str
    broker: str
    page: int | None
    severity: str
    code: str
    message: str

    def to_row(self) -> dict[str, str]:
        return {
            "statement_id": self.statement_id,
            "broker": self.broker,
            "page": "" if self.page is None else str(self.page),
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ManifestRecord:
    month: str
    broker: str
    source_file: str
    source_sha256: str
    parsed_at: str
    page_count: int
    parser_version: str
    status: str
```

- [ ] **Step 4: Run model tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit models**

```bash
git add src/open_trader/models.py tests/test_models.py
git commit -m "feat: add normalized portfolio models"
```

## Task 3: CSV Writers

**Files:**
- Create: `src/open_trader/csv_io.py`
- Test: `tests/test_csv_io.py`

- [ ] **Step 1: Write failing CSV writer tests**

Create `tests/test_csv_io.py`:

```python
from pathlib import Path

from open_trader.csv_io import write_rows


def test_write_rows_creates_parent_and_writes_header(tmp_path: Path):
    output = tmp_path / "nested" / "rows.csv"

    write_rows(output, ["symbol", "quantity"], [{"symbol": "NVDA", "quantity": "10"}])

    assert output.read_text(encoding="utf-8") == "symbol,quantity\nNVDA,10\n"


def test_write_rows_writes_header_for_empty_rows(tmp_path: Path):
    output = tmp_path / "empty.csv"

    write_rows(output, ["symbol", "quantity"], [])

    assert output.read_text(encoding="utf-8") == "symbol,quantity\n"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_csv_io.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.csv_io'`.

- [ ] **Step 3: Implement CSV writer**

Create `src/open_trader/csv_io.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping


def write_rows(path: Path, fieldnames: list[str], rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
```

- [ ] **Step 4: Run CSV tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_csv_io.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Commit CSV writer**

```bash
git add src/open_trader/csv_io.py tests/test_csv_io.py
git commit -m "feat: add csv writing helper"
```

## Task 4: FX Conversion

**Files:**
- Create: `src/open_trader/fx.py`
- Test: `tests/test_fx.py`

- [ ] **Step 1: Write failing FX tests**

Create `tests/test_fx.py`:

```python
from decimal import Decimal

import pytest

from open_trader.fx import StaticMonthEndFxProvider


def test_static_fx_provider_returns_hkd_for_hkd():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    quote = provider.get_rate_to_hkd("HKD")

    assert quote.fx_date == "2026-05-31"
    assert quote.rate == Decimal("1")
    assert quote.source == "external_month_end_static"


def test_static_fx_provider_returns_configured_rate():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    quote = provider.get_rate_to_hkd("usd")

    assert quote.currency == "USD"
    assert quote.rate == Decimal("7.84")


def test_static_fx_provider_rejects_missing_currency():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    with pytest.raises(KeyError, match="Missing HKD FX rate for EUR"):
        provider.get_rate_to_hkd("EUR")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_fx.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.fx'`.

- [ ] **Step 3: Implement FX provider**

Create `src/open_trader/fx.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import calendar


@dataclass(frozen=True)
class FxQuote:
    currency: str
    fx_date: str
    rate: Decimal
    source: str


def month_end_date(month: str) -> str:
    year_s, month_s = month.split("-", 1)
    year = int(year_s)
    month_i = int(month_s)
    last_day = calendar.monthrange(year, month_i)[1]
    return f"{year:04d}-{month_i:02d}-{last_day:02d}"


class StaticMonthEndFxProvider:
    """Deterministic month-end FX provider.

    The first implementation uses caller-provided external rates so the
    portfolio pipeline is reproducible. A later task can add live rate fetching.
    """

    source = "external_month_end_static"

    def __init__(self, month: str, rates_to_hkd: dict[str, Decimal]):
        self.month = month
        self.fx_date = month_end_date(month)
        self.rates_to_hkd = {currency.upper(): rate for currency, rate in rates_to_hkd.items()}

    def get_rate_to_hkd(self, currency: str) -> FxQuote:
        normalized = currency.upper()
        if normalized == "HKD":
            return FxQuote(currency="HKD", fx_date=self.fx_date, rate=Decimal("1"), source=self.source)
        if normalized not in self.rates_to_hkd:
            raise KeyError(f"Missing HKD FX rate for {normalized}")
        return FxQuote(
            currency=normalized,
            fx_date=self.fx_date,
            rate=self.rates_to_hkd[normalized],
            source=self.source,
        )
```

- [ ] **Step 4: Run FX tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_fx.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit FX provider**

```bash
git add src/open_trader/fx.py tests/test_fx.py
git commit -m "feat: add month end fx provider"
```

## Task 5: Portfolio Merge And Risk Flags

**Files:**
- Create: `src/open_trader/portfolio.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Write failing portfolio tests**

Create `tests/test_portfolio.py`:

```python
from decimal import Decimal

from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.portfolio import build_portfolio_rows


def position(
    broker: str,
    symbol: str,
    quantity: str,
    cost_value: str,
    market_value: str,
    *,
    market: Market = Market.US,
    asset_class: AssetClass = AssetClass.STOCK,
    currency: str = "USD",
) -> Position:
    return Position(
        statement_id=f"2026-05-{broker}",
        broker=broker,
        account_alias=f"{broker}_main",
        market=market,
        asset_class=asset_class,
        symbol=symbol,
        name=symbol,
        currency=currency,
        quantity=Decimal(quantity),
        cost_price=None,
        last_price=None,
        market_value=Decimal(market_value),
        cost_value=Decimal(cost_value),
        unrealized_pnl=Decimal(market_value) - Decimal(cost_value),
        confidence="high",
        notes="",
    )


def test_build_portfolio_rows_merges_same_us_symbol_across_brokers():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position("futu", "NVDA", "10", "1000", "1300"),
        position("tiger", "NVDA", "5", "600", "700"),
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    nvda = rows[0]
    assert nvda["symbol"] == "NVDA"
    assert nvda["total_quantity"] == "15"
    assert nvda["market_value"] == "2000"
    assert nvda["cost_value"] == "1600"
    assert nvda["market_value_hkd"] == "15600.00"
    assert nvda["brokers"] == "futu;tiger"
    assert nvda["ai_eligible"] == "true"
    assert nvda["analysis_symbol"] == "NVDA"


def test_cash_is_included_in_weight_denominator_but_not_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [position("futu", "NVDA", "10", "1000", "2000")]
    cash = [
        CashBalance(
            statement_id="2026-05-futu",
            broker="futu",
            account_alias="futu_main",
            currency="USD",
            cash_balance=Decimal("18000"),
            available_balance=Decimal("18000"),
            confidence="high",
            notes="",
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, cash, fx)

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    usd_cash = next(row for row in rows if row["symbol"] == "USD_CASH")
    assert nvda["portfolio_weight_hkd"] == "10.00%"
    assert nvda["risk_flag"] == "normal"
    assert usd_cash["risk_flag"] == "normal"


def test_non_cash_position_over_ten_percent_is_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [position("futu", "NVDA", "10", "1000", "2000")]
    cash = [
        CashBalance(
            statement_id="2026-05-futu",
            broker="futu",
            account_alias="futu_main",
            currency="USD",
            cash_balance=Decimal("1000"),
            available_balance=Decimal("1000"),
            confidence="high",
            notes="",
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, cash, fx)

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["portfolio_weight_hkd"] == "66.67%"
    assert nvda["risk_flag"] == "overweight"


def test_money_market_fund_is_not_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position(
            "futu",
            "HK0000584752",
            "1000",
            "1000",
            "2000",
            market=Market.OTHER,
            asset_class=AssetClass.MONEY_MARKET_FUND,
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    fund = rows[0]
    assert fund["portfolio_weight_hkd"] == "100.00%"
    assert fund["risk_flag"] == "normal"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.portfolio'`.

- [ ] **Step 3: Implement portfolio builder**

Create `src/open_trader/portfolio.py`:

```python
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .fx import StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position


PORTFOLIO_FIELDNAMES = [
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


def money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def number(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    return format(normalized, "f")


def pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def _sort_group(market: Market, asset_class: AssetClass, ai_eligible: bool) -> int:
    if market == Market.US and ai_eligible:
        return 1
    if market == Market.US:
        return 2
    if market == Market.HK:
        return 3
    if market == Market.CASH:
        return 5
    return 4


def _ai_eligible(position: Position) -> bool:
    return position.market == Market.US and position.asset_class in {AssetClass.STOCK, AssetClass.ETF}


def build_portfolio_rows(
    month: str,
    positions: Iterable[Position],
    cash_balances: Iterable[CashBalance],
    fx_provider: StaticMonthEndFxProvider,
) -> list[dict[str, str]]:
    grouped: dict[tuple[Market, AssetClass, str, str], list[Position]] = defaultdict(list)
    for position in positions:
        grouped[position.identity_key()].append(position)

    raw_rows: list[dict[str, object]] = []
    for (market, asset_class, symbol, currency), group in grouped.items():
        total_quantity = sum((p.quantity for p in group), Decimal("0"))
        market_value = sum((p.market_value or Decimal("0") for p in group), Decimal("0"))
        cost_value = sum((p.cost_value or Decimal("0") for p in group), Decimal("0"))
        unrealized_pnl = market_value - cost_value if cost_value else None
        avg_cost_price = cost_value / total_quantity if total_quantity and cost_value else None
        last_price = market_value / total_quantity if total_quantity and market_value else None
        quote = fx_provider.get_rate_to_hkd(currency)
        market_value_hkd = market_value * quote.rate
        cost_value_hkd = cost_value * quote.rate if cost_value else None
        ai_eligible = any(_ai_eligible(p) for p in group)
        confidence = "low" if any(p.confidence == "low" for p in group) else "medium" if any(p.confidence == "medium" for p in group) else "high"
        brokers = sorted({p.broker for p in group})
        accounts = sorted({p.account_alias for p in group})
        name = max((p.name for p in group), key=len)
        notes = "; ".join(p.notes for p in group if p.notes)

        raw_rows.append(
            {
                "sort_group": _sort_group(market, asset_class, ai_eligible),
                "market": market.value,
                "asset_class": asset_class.value,
                "symbol": symbol,
                "name": name,
                "currency": currency,
                "total_quantity": total_quantity,
                "avg_cost_price": avg_cost_price,
                "last_price": last_price,
                "market_value": market_value,
                "cost_value": cost_value if cost_value else None,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": (unrealized_pnl / cost_value) if unrealized_pnl is not None and cost_value else None,
                "fx_source": quote.source,
                "fx_date": quote.fx_date,
                "fx_to_hkd": quote.rate,
                "market_value_hkd": market_value_hkd,
                "cost_value_hkd": cost_value_hkd,
                "brokers": ";".join(brokers),
                "accounts": ";".join(accounts),
                "ai_eligible": ai_eligible,
                "analysis_symbol": symbol if ai_eligible else "",
                "risk_flag": "data_check" if confidence == "low" or not market_value else "normal",
                "confidence": confidence,
                "notes": notes,
            }
        )

    for cash in cash_balances:
        quote = fx_provider.get_rate_to_hkd(cash.currency)
        market_value = cash.cash_balance
        raw_rows.append(
            {
                "sort_group": 5,
                "market": Market.CASH.value,
                "asset_class": AssetClass.CASH.value,
                "symbol": cash.symbol,
                "name": f"{cash.currency.upper()} Cash",
                "currency": cash.currency.upper(),
                "total_quantity": Decimal("1"),
                "avg_cost_price": None,
                "last_price": None,
                "market_value": market_value,
                "cost_value": None,
                "unrealized_pnl": None,
                "unrealized_pnl_pct": None,
                "fx_source": quote.source,
                "fx_date": quote.fx_date,
                "fx_to_hkd": quote.rate,
                "market_value_hkd": market_value * quote.rate,
                "cost_value_hkd": None,
                "brokers": cash.broker,
                "accounts": cash.account_alias,
                "ai_eligible": False,
                "analysis_symbol": "",
                "risk_flag": "data_check" if cash.confidence == "low" else "normal",
                "confidence": cash.confidence,
                "notes": cash.notes,
            }
        )

    total_hkd = sum((row["market_value_hkd"] for row in raw_rows), Decimal("0"))
    output: list[dict[str, str]] = []
    for row in raw_rows:
        weight = row["market_value_hkd"] / total_hkd if total_hkd else Decimal("0")
        if (
            row["risk_flag"] != "data_check"
            and row["asset_class"] not in {AssetClass.CASH.value, AssetClass.MONEY_MARKET_FUND.value}
            and weight > Decimal("0.10")
        ):
            row["risk_flag"] = "overweight"

        output.append(
            {
                "sort_group": str(row["sort_group"]),
                "market": str(row["market"]),
                "asset_class": str(row["asset_class"]),
                "symbol": str(row["symbol"]),
                "name": str(row["name"]),
                "currency": str(row["currency"]),
                "total_quantity": number(row["total_quantity"]),
                "avg_cost_price": money(row["avg_cost_price"]),
                "last_price": money(row["last_price"]),
                "market_value": money(row["market_value"]),
                "cost_value": money(row["cost_value"]),
                "unrealized_pnl": money(row["unrealized_pnl"]),
                "unrealized_pnl_pct": pct(row["unrealized_pnl_pct"]),
                "fx_source": str(row["fx_source"]),
                "fx_date": str(row["fx_date"]),
                "fx_to_hkd": number(row["fx_to_hkd"]),
                "market_value_hkd": money(row["market_value_hkd"]),
                "cost_value_hkd": money(row["cost_value_hkd"]),
                "portfolio_weight_hkd": pct(weight),
                "brokers": str(row["brokers"]),
                "accounts": str(row["accounts"]),
                "ai_eligible": "true" if row["ai_eligible"] else "false",
                "analysis_symbol": str(row["analysis_symbol"]),
                "risk_flag": str(row["risk_flag"]),
                "confidence": str(row["confidence"]),
                "notes": str(row["notes"]),
            }
        )

    return sorted(output, key=lambda item: (int(item["sort_group"]), -Decimal(item["market_value_hkd"])))
```

- [ ] **Step 4: Run portfolio tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio.py -v
```

Expected: `4 passed`.

- [ ] **Step 5: Commit portfolio builder**

```bash
git add src/open_trader/portfolio.py tests/test_portfolio.py
git commit -m "feat: build merged portfolio rows"
```

## Task 6: Parser Base Utilities

**Files:**
- Create: `src/open_trader/parsers/__init__.py`
- Create: `src/open_trader/parsers/base.py`
- Test: `tests/test_parser_base.py`

- [ ] **Step 1: Write failing parser utility tests**

Create `tests/test_parser_base.py`:

```python
from decimal import Decimal

from open_trader.models import AssetClass, Market
from open_trader.parsers.base import detect_asset_class, detect_market, parse_decimal, split_symbol_name


def test_parse_decimal_handles_commas_parentheses_and_dashes():
    assert parse_decimal("1,234.50") == Decimal("1234.50")
    assert parse_decimal("(12.30)") == Decimal("-12.30")
    assert parse_decimal("-") is None
    assert parse_decimal("") is None


def test_split_symbol_name_handles_broker_display_format():
    assert split_symbol_name("NVIDIA (NVDA)") == ("NVDA", "NVIDIA")
    assert split_symbol_name("BOTZ(Global X Robotics & Artificial Intelligence Thematic ETF)") == (
        "BOTZ",
        "Global X Robotics & Artificial Intelligence Thematic ETF",
    )
    assert split_symbol_name("00700(腾讯控股)") == ("00700", "腾讯控股")


def test_detect_market_normalizes_us_hk_and_other():
    assert detect_market("US") == Market.US
    assert detect_market("NASDAQ") == Market.US
    assert detect_market("SEHK") == Market.HK
    assert detect_market("HK") == Market.HK
    assert detect_market("SG") == Market.OTHER


def test_detect_asset_class_uses_name_and_symbol_clues():
    assert detect_asset_class("BOTZ", "Global X Robotics ETF") == AssetClass.ETF
    assert detect_asset_class("HK0000584752", "高腾微金美元货币基金") == AssetClass.MONEY_MARKET_FUND
    assert detect_asset_class("NVDA 2026 CALL 100", "NVIDIA CALL") == AssetClass.OPTION
    assert detect_asset_class("NVDA", "NVIDIA") == AssetClass.STOCK
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_parser_base.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.parsers'`.

- [ ] **Step 3: Implement parser utilities**

Create `src/open_trader/parsers/__init__.py`:

```python
"""Broker statement parsers."""
```

Create `src/open_trader/parsers/base.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
from pathlib import Path
import re

from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord


@dataclass
class ParseResult:
    statement_id: str
    broker: str
    positions: list[Position] = field(default_factory=list)
    cash_balances: list[CashBalance] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    page_count: int = 0


class StatementParser:
    broker: str
    parser_version = "0.1.0"

    def parse(self, path: Path, month: str) -> ParseResult:
        raise NotImplementedError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = value.strip().replace(",", "").replace("HKD", "").replace("USD", "").strip()
    if cleaned in {"", "-", "--"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        number = Decimal(cleaned)
    except Exception:
        return None
    return -number if negative else number


def split_symbol_name(value: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", value).strip()
    match = re.match(r"^(?P<name>.+?)\s*\((?P<symbol>[A-Z0-9.\-]+)\)$", text)
    if match:
        return match.group("symbol").upper(), match.group("name").strip()
    match = re.match(r"^(?P<symbol>[A-Z0-9.\-]+)\((?P<name>.+)\)$", text)
    if match:
        return match.group("symbol").upper(), match.group("name").strip()
    parts = text.split()
    return parts[0].upper(), text


def detect_market(value: str) -> Market:
    normalized = value.upper().strip()
    if normalized in {"US", "NYSE", "NASDAQ", "AMEX", "CBOE"}:
        return Market.US
    if normalized in {"HK", "SEHK", "HKEX"}:
        return Market.HK
    return Market.OTHER


def detect_asset_class(symbol: str, name: str) -> AssetClass:
    text = f"{symbol} {name}".upper()
    if " MONEY MARKET" in text or "货币基金" in name or "貨幣基金" in name:
        return AssetClass.MONEY_MARKET_FUND
    if " ETF" in text or "ETF" in symbol.upper():
        return AssetClass.ETF
    if any(token in text for token in (" CALL", " PUT", " OPTION", "期权", "期權")):
        return AssetClass.OPTION
    if symbol.upper().startswith("HK") and "基金" in name:
        return AssetClass.FUND
    return AssetClass.STOCK
```

- [ ] **Step 4: Run parser utility tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_parser_base.py -v
```

Expected: `4 passed`.

- [ ] **Step 5: Commit parser base**

```bash
git add src/open_trader/parsers tests/test_parser_base.py
git commit -m "feat: add statement parser utilities"
```

## Task 7: Text-Fixture Parsers For Broker Tables

**Files:**
- Create: `tests/fixtures/pdf_text/futu.txt`
- Create: `tests/fixtures/pdf_text/tiger.txt`
- Create: `tests/fixtures/pdf_text/phillips.txt`
- Create: `src/open_trader/parsers/futu.py`
- Create: `src/open_trader/parsers/tiger.py`
- Create: `src/open_trader/parsers/phillips.py`
- Test: `tests/test_parsers_text.py`

- [ ] **Step 1: Create sanitized text fixtures**

Create `tests/fixtures/pdf_text/futu.txt`:

```text
期末概覽-股票和股票期權
代碼名稱 交易所/市場 貨幣種類 數量 價格 乘數 市值 初始保證金要求 維持保證金要求 維持保證金率
NVDA(NVIDIA) US USD 10 130.00 - 1300.00 650.00 520.00 0.40
BOTZ(Global X Robotics & Artificial Intelligence Thematic ETF) US USD 50 37.20 - 1860.00 930.00 744.00 0.40
00700(腾讯控股) SEHK HKD 100 380.00 - 38000.00 19000.00 15200.00 0.40
現金結餘
USD 1000.00
HKD 5000.00
```

Create `tests/fixtures/pdf_text/tiger.txt`:

```text
期末持仓
股票
代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
ARM Holdings (ARM) 4 1.0 281.00 353.00 1412.00 288.00 706.00 564.80 USD
COHERENT (COHR) 4 1.0 318.00 320.00 1280.00 8.00 640.00 512.00 USD
现金
USD 2000.00
```

Create `tests/fixtures/pdf_text/phillips.txt`:

```text
Securities Portfolio
產品 市場 產品代號 代號名稱 上日存貨 最後買貨日期 是日存貨 收市價 市值 按貨比率 按倉值
股票 HK 0300476 勝宏科技 0 2026/05/20 200 378.40 75680.00 0.50 37840.00
股票 US NVDA NVIDIA 0 2026/05/20 5 130.00 650.00 0.50 325.00
Cash Balance
HKD 8000.00
```

- [ ] **Step 2: Write failing parser tests**

Create `tests/test_parsers_text.py`:

```python
from pathlib import Path

from open_trader.models import AssetClass, Market
from open_trader.parsers.futu import parse_futu_text
from open_trader.parsers.phillips import parse_phillips_text
from open_trader.parsers.tiger import parse_tiger_text


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdf_text"


def test_parse_futu_text_extracts_positions_and_cash():
    result = parse_futu_text(FIXTURE_DIR.joinpath("futu.txt").read_text(encoding="utf-8"), "2026-05")

    assert len(result.positions) == 3
    assert len(result.cash_balances) == 2
    nvda = next(position for position in result.positions if position.symbol == "NVDA")
    assert nvda.market == Market.US
    assert nvda.asset_class == AssetClass.STOCK
    assert str(nvda.quantity) == "10"
    botz = next(position for position in result.positions if position.symbol == "BOTZ")
    assert botz.asset_class == AssetClass.ETF


def test_parse_tiger_text_extracts_us_positions():
    result = parse_tiger_text(FIXTURE_DIR.joinpath("tiger.txt").read_text(encoding="utf-8"), "2026-05")

    assert {position.symbol for position in result.positions} == {"ARM", "COHR"}
    assert result.positions[0].currency == "USD"
    assert len(result.cash_balances) == 1


def test_parse_phillips_text_extracts_hk_and_us_positions():
    result = parse_phillips_text(FIXTURE_DIR.joinpath("phillips.txt").read_text(encoding="utf-8"), "2026-05")

    assert {position.symbol for position in result.positions} == {"0300476", "NVDA"}
    hk = next(position for position in result.positions if position.symbol == "0300476")
    assert hk.market == Market.HK
    assert len(result.cash_balances) == 1
```

- [ ] **Step 3: Run parser tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_parsers_text.py -v
```

Expected: FAIL with missing parser modules.

- [ ] **Step 4: Implement Futu text parser**

Create `src/open_trader/parsers/futu.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import CashBalance, Position
from open_trader.parsers.base import ParseResult, StatementParser, detect_asset_class, detect_market, parse_decimal, split_symbol_name


def parse_futu_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-futu"
    result = ParseResult(statement_id=statement_id, broker="futu")
    in_positions = False
    in_cash = False
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if "期末概覽-股票" in line:
            in_positions = True
            in_cash = False
            continue
        if "現金結餘" in line or "现金结余" in line:
            in_positions = False
            in_cash = True
            continue
        if line.startswith("代碼名稱") or line.startswith("代码名称"):
            continue
        if in_positions:
            match = re.match(
                r"(?P<display>.+?)\s+(?P<market>US|SEHK|HK|HKEX|NASDAQ|NYSE)\s+(?P<currency>[A-Z]{3})\s+(?P<qty>-?[\d,.]+)\s+(?P<price>-?[\d,.]+)\s+(?P<multiplier>-|[\d,.]+)\s+(?P<value>-?[\d,.]+)\s+",
                line,
            )
            if not match:
                continue
            symbol, name = split_symbol_name(match.group("display"))
            market = detect_market(match.group("market"))
            result.positions.append(
                Position(
                    statement_id=statement_id,
                    broker="futu",
                    account_alias="futu_main",
                    market=market,
                    asset_class=detect_asset_class(symbol, name),
                    symbol=symbol,
                    name=name,
                    currency=match.group("currency"),
                    quantity=parse_decimal(match.group("qty")) or Decimal("0"),
                    cost_price=None,
                    last_price=parse_decimal(match.group("price")),
                    market_value=parse_decimal(match.group("value")),
                    cost_value=None,
                    unrealized_pnl=None,
                    confidence="high",
                    notes="",
                )
            )
        elif in_cash:
            match = re.match(r"(?P<currency>[A-Z]{3})\s+(?P<balance>-?[\d,.]+)$", line)
            if match:
                balance = parse_decimal(match.group("balance")) or Decimal("0")
                result.cash_balances.append(
                    CashBalance(statement_id, "futu", "futu_main", match.group("currency"), balance, balance, "high", "")
                )
    return result


class FutuStatementParser(StatementParser):
    broker = "futu"

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text(x_tolerance=1, y_tolerance=3) or "" for page in pdf.pages)
            result = parse_futu_text(text, month)
            result.page_count = len(pdf.pages)
            return result
```

- [ ] **Step 5: Implement Tiger text parser**

Create `src/open_trader/parsers/tiger.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import CashBalance, Market, Position
from open_trader.parsers.base import ParseResult, StatementParser, detect_asset_class, parse_decimal, split_symbol_name


def parse_tiger_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-tiger"
    result = ParseResult(statement_id=statement_id, broker="tiger")
    in_positions = False
    in_cash = False
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if "期末持仓" in line or "期末持倉" in line:
            in_positions = True
            in_cash = False
            continue
        if line in {"股票", "代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种"}:
            continue
        if line.startswith("现金") or line.startswith("現金"):
            in_positions = False
            in_cash = True
            continue
        if in_positions:
            match = re.match(
                r"(?P<display>.+?)\s+(?P<qty>-?[\d,.]+)\s+(?P<multiplier>[\d,.]+)\s+(?P<cost_price>-?[\d,.]+)\s+(?P<last_price>-?[\d,.]+)\s+(?P<market_value>-?[\d,.]+)\s+(?P<pnl>-?[\d,.]+)\s+(?P<initial>-?[\d,.]+)\s+(?P<maintenance>-?[\d,.]+)\s+(?P<currency>[A-Z]{3})$",
                line,
            )
            if not match:
                continue
            symbol, name = split_symbol_name(match.group("display"))
            quantity = parse_decimal(match.group("qty")) or Decimal("0")
            cost_price = parse_decimal(match.group("cost_price"))
            cost_value = cost_price * quantity if cost_price is not None else None
            result.positions.append(
                Position(
                    statement_id=statement_id,
                    broker="tiger",
                    account_alias="tiger_main",
                    market=Market.US,
                    asset_class=detect_asset_class(symbol, name),
                    symbol=symbol,
                    name=name,
                    currency=match.group("currency"),
                    quantity=quantity,
                    cost_price=cost_price,
                    last_price=parse_decimal(match.group("last_price")),
                    market_value=parse_decimal(match.group("market_value")),
                    cost_value=cost_value,
                    unrealized_pnl=parse_decimal(match.group("pnl")),
                    confidence="high",
                    notes="",
                )
            )
        elif in_cash:
            match = re.match(r"(?P<currency>[A-Z]{3})\s+(?P<balance>-?[\d,.]+)$", line)
            if match:
                balance = parse_decimal(match.group("balance")) or Decimal("0")
                result.cash_balances.append(
                    CashBalance(statement_id, "tiger", "tiger_main", match.group("currency"), balance, balance, "high", "")
                )
    return result


class TigerStatementParser(StatementParser):
    broker = "tiger"

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text(x_tolerance=1, y_tolerance=3) or "" for page in pdf.pages)
            result = parse_tiger_text(text, month)
            result.page_count = len(pdf.pages)
            return result
```

- [ ] **Step 6: Implement Phillip text parser**

Create `src/open_trader/parsers/phillips.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import CashBalance, Position
from open_trader.parsers.base import ParseResult, StatementParser, detect_asset_class, detect_market, parse_decimal


def parse_phillips_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-phillips"
    result = ParseResult(statement_id=statement_id, broker="phillips")
    in_positions = False
    in_cash = False
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if "Securities Portfolio" in line or "證券投資組合" in line or "证券投资组合" in line:
            in_positions = True
            in_cash = False
            continue
        if line.startswith("產品 市場") or line.startswith("Product Market"):
            continue
        if "Cash Balance" in line:
            in_positions = False
            in_cash = True
            continue
        if in_positions:
            match = re.match(
                r"股票\s+(?P<market>HK|US|SEHK|NASDAQ|NYSE)\s+(?P<symbol>[A-Z0-9.]+)\s+(?P<name>.+?)\s+(?P<bf>-?[\d,.]+)\s+(?P<date>\d{4}/\d{2}/\d{2})\s+(?P<qty>-?[\d,.]+)\s+(?P<price>-?[\d,.]+)\s+(?P<value>-?[\d,.]+)\s+(?P<ratio>-?[\d,.]+)\s+(?P<margin>-?[\d,.]+)$",
                line,
            )
            if not match:
                continue
            symbol = match.group("symbol").upper()
            name = match.group("name").strip()
            result.positions.append(
                Position(
                    statement_id=statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    market=detect_market(match.group("market")),
                    asset_class=detect_asset_class(symbol, name),
                    symbol=symbol,
                    name=name,
                    currency="HKD" if detect_market(match.group("market")).value == "HK" else "USD",
                    quantity=parse_decimal(match.group("qty")) or Decimal("0"),
                    cost_price=None,
                    last_price=parse_decimal(match.group("price")),
                    market_value=parse_decimal(match.group("value")),
                    cost_value=None,
                    unrealized_pnl=None,
                    confidence="medium",
                    notes="phillips fixture parser infers currency from market",
                )
            )
        elif in_cash:
            match = re.match(r"(?P<currency>[A-Z]{3})\s+(?P<balance>-?[\d,.]+)$", line)
            if match:
                balance = parse_decimal(match.group("balance")) or Decimal("0")
                result.cash_balances.append(
                    CashBalance(statement_id, "phillips", "phillips_main", match.group("currency"), balance, balance, "high", "")
                )
    return result


class PhillipsStatementParser(StatementParser):
    broker = "phillips"

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text(x_tolerance=1, y_tolerance=3) or "" for page in pdf.pages)
            result = parse_phillips_text(text, month)
            result.page_count = len(pdf.pages)
            return result
```

- [ ] **Step 7: Run parser tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_parsers_text.py -v
```

Expected: `3 passed`.

- [ ] **Step 8: Commit text parsers**

```bash
git add src/open_trader/parsers tests/fixtures/pdf_text tests/test_parsers_text.py
git commit -m "feat: parse broker statement text fixtures"
```

## Task 8: End-To-End Pipeline

**Files:**
- Create: `src/open_trader/pipeline.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing pipeline test**

Create `tests/test_pipeline.py`:

```python
from decimal import Decimal
from pathlib import Path

from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.parsers.base import ParseResult
from open_trader.pipeline import run_import


class FakeParser:
    parser_version = "test"

    def __init__(self, broker: str, positions: list[Position], cash: list[CashBalance]):
        self.broker = broker
        self.positions = positions
        self.cash = cash

    def parse(self, path: Path, month: str) -> ParseResult:
        return ParseResult(
            statement_id=f"{month}-{self.broker}",
            broker=self.broker,
            positions=self.positions,
            cash_balances=self.cash,
            page_count=1,
        )


def test_run_import_writes_portfolio_and_latest(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fake")
    position = Position(
        statement_id="2026-05-futu",
        broker="futu",
        account_alias="futu_main",
        market=Market.US,
        asset_class=AssetClass.STOCK,
        symbol="NVDA",
        name="NVIDIA",
        currency="USD",
        quantity=Decimal("10"),
        cost_price=Decimal("100"),
        last_price=Decimal("130"),
        market_value=Decimal("1300"),
        cost_value=Decimal("1000"),
        unrealized_pnl=Decimal("300"),
        confidence="high",
        notes="",
    )
    cash = CashBalance("2026-05-futu", "futu", "futu_main", "USD", Decimal("1000"), Decimal("1000"), "high", "")
    parser = FakeParser("futu", [position], [cash])
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})

    result = run_import(
        month="2026-05",
        statement_paths={"futu": source},
        parsers={"futu": parser},
        data_dir=tmp_path / "data",
        fx_provider=fx,
    )

    assert result.portfolio_path == tmp_path / "data" / "runs" / "2026-05" / "portfolio.csv"
    assert result.latest_path == tmp_path / "data" / "latest" / "portfolio.csv"
    assert "NVDA" in result.portfolio_path.read_text(encoding="utf-8")
    assert result.latest_path.read_text(encoding="utf-8") == result.portfolio_path.read_text(encoding="utf-8")
    assert "source_sha256" in (tmp_path / "data" / "runs" / "2026-05" / "manifest.csv").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run pipeline test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.pipeline'`.

- [ ] **Step 3: Implement pipeline**

Create `src/open_trader/pipeline.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .models import CashBalance, ManifestRecord, Position
from .parsers.base import StatementParser, sha256_file
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows


@dataclass(frozen=True)
class ImportResult:
    run_dir: Path
    portfolio_path: Path
    latest_path: Path
    positions_count: int
    cash_count: int
    warnings_count: int


POSITION_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "confidence",
    "notes",
]

CASH_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]

MANIFEST_FIELDNAMES = [
    "month",
    "broker",
    "source_file",
    "source_sha256",
    "parsed_at",
    "page_count",
    "parser_version",
    "status",
]

WARNING_FIELDNAMES = ["statement_id", "broker", "page", "severity", "code", "message"]


def position_to_row(position: Position) -> dict[str, object]:
    return {
        "statement_id": position.statement_id,
        "broker": position.broker,
        "account_alias": position.account_alias,
        "market": position.market.value,
        "asset_class": position.asset_class.value,
        "symbol": position.symbol,
        "name": position.name,
        "currency": position.currency,
        "quantity": position.quantity,
        "cost_price": position.cost_price,
        "last_price": position.last_price,
        "market_value": position.market_value,
        "cost_value": position.cost_value,
        "unrealized_pnl": position.unrealized_pnl,
        "confidence": position.confidence,
        "notes": position.notes,
    }


def cash_to_row(cash: CashBalance) -> dict[str, object]:
    return {
        "statement_id": cash.statement_id,
        "broker": cash.broker,
        "account_alias": cash.account_alias,
        "currency": cash.currency,
        "cash_balance": cash.cash_balance,
        "available_balance": cash.available_balance,
        "confidence": cash.confidence,
        "notes": cash.notes,
    }


def manifest_to_row(record: ManifestRecord) -> dict[str, object]:
    return {
        "month": record.month,
        "broker": record.broker,
        "source_file": record.source_file,
        "source_sha256": record.source_sha256,
        "parsed_at": record.parsed_at,
        "page_count": record.page_count,
        "parser_version": record.parser_version,
        "status": record.status,
    }


def run_import(
    *,
    month: str,
    statement_paths: dict[str, Path],
    parsers: dict[str, StatementParser],
    data_dir: Path,
    fx_provider: StaticMonthEndFxProvider,
) -> ImportResult:
    run_dir = data_dir / "runs" / month
    latest_dir = data_dir / "latest"
    parsed_at = datetime.now(timezone.utc).isoformat()

    all_positions: list[Position] = []
    all_cash: list[CashBalance] = []
    warnings = []
    manifest: list[ManifestRecord] = []

    for broker, path in statement_paths.items():
        parser = parsers[broker]
        result = parser.parse(path, month)
        all_positions.extend(result.positions)
        all_cash.extend(result.cash_balances)
        warnings.extend(result.warnings)
        manifest.append(
            ManifestRecord(
                month=month,
                broker=broker,
                source_file=str(path),
                source_sha256=sha256_file(path),
                parsed_at=parsed_at,
                page_count=result.page_count,
                parser_version=parser.parser_version,
                status="success",
            )
        )

    portfolio_rows = build_portfolio_rows(month, all_positions, all_cash, fx_provider)

    portfolio_path = run_dir / "portfolio.csv"
    write_rows(run_dir / "manifest.csv", MANIFEST_FIELDNAMES, [manifest_to_row(row) for row in manifest])
    write_rows(run_dir / "extracted_positions.csv", POSITION_FIELDNAMES, [position_to_row(row) for row in all_positions])
    write_rows(run_dir / "extracted_cash.csv", CASH_FIELDNAMES, [cash_to_row(row) for row in all_cash])
    write_rows(run_dir / "parse_warnings.csv", WARNING_FIELDNAMES, [warning.to_row() for warning in warnings])
    write_rows(portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows)

    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_path = latest_dir / "portfolio.csv"
    shutil.copyfile(portfolio_path, latest_path)

    return ImportResult(
        run_dir=run_dir,
        portfolio_path=portfolio_path,
        latest_path=latest_path,
        positions_count=len(all_positions),
        cash_count=len(all_cash),
        warnings_count=len(warnings),
    )
```

- [ ] **Step 4: Modify CLI to call pipeline**

Replace `src/open_trader/cli.py` with:

```python
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from .fx import StaticMonthEndFxProvider
from .parsers.futu import FutuStatementParser
from .parsers.phillips import PhillipsStatementParser
from .parsers.tiger import TigerStatementParser
from .pipeline import run_import


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-statements",
        help="Import monthly broker statements and generate portfolio.csv",
    )
    import_parser.add_argument("--month", required=True, help="Statement month, YYYY-MM")
    import_parser.add_argument("--futu", type=Path, required=True)
    import_parser.add_argument("--tiger", type=Path, required=True)
    import_parser.add_argument("--phillips", type=Path, required=True)
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    import_parser.add_argument("--usd-hkd", type=Decimal, required=True, help="External month-end USD/HKD rate")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-statements":
        result = run_import(
            month=args.month,
            statement_paths={
                "futu": args.futu,
                "tiger": args.tiger,
                "phillips": args.phillips,
            },
            parsers={
                "futu": FutuStatementParser(),
                "tiger": TigerStatementParser(),
                "phillips": PhillipsStatementParser(),
            },
            data_dir=args.data_dir,
            fx_provider=StaticMonthEndFxProvider(args.month, {"USD": args.usd_hkd}),
        )
        print(f"portfolio: {result.portfolio_path}")
        print(f"latest: {result.latest_path}")
        print(f"positions: {result.positions_count}")
        print(f"cash balances: {result.cash_count}")
        print(f"warnings: {result.warnings_count}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
```

- [ ] **Step 5: Run pipeline tests and verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -v
```

Expected: `1 passed`.

- [ ] **Step 6: Run full test suite and verify pass**

Run:

```bash
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit pipeline**

```bash
git add src/open_trader/cli.py src/open_trader/pipeline.py tests/test_pipeline.py
git commit -m "feat: generate monthly portfolio outputs"
```

## Task 9: Real PDF Smoke Command

**Files:**
- No source files unless a parser bug is found.

- [ ] **Step 1: Run CLI against the three current user PDFs**

Run:

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --futu /Users/ray/Downloads/futu.pdf \
  --tiger /Users/ray/Downloads/tiger.pdf \
  --phillips /Users/ray/Downloads/phillips.pdf \
  --usd-hkd 7.85
```

Expected: command exits `0`, writes `data/runs/2026-05/portfolio.csv`, and prints counts.

- [ ] **Step 2: Inspect final portfolio**

Run:

```bash
sed -n '1,40p' data/latest/portfolio.csv
```

Expected: header includes `portfolio_weight_hkd`, first rows are US eligible holdings when present, and cash rows appear at the bottom.

- [ ] **Step 3: Inspect parser warnings**

Run:

```bash
sed -n '1,80p' data/runs/2026-05/parse_warnings.csv
```

Expected: header is present. If warning rows exist, each warning identifies broker, page, code, and message.

- [ ] **Step 4: Fix parser bugs one at a time if smoke fails**

For each failure, add a small sanitized fixture line to `tests/fixtures/pdf_text/<broker>.txt`, add or update one assertion in `tests/test_parsers_text.py`, run the focused failing test, implement the minimal parser fix, and rerun:

```bash
.venv/bin/python -m pytest tests/test_parsers_text.py -v
.venv/bin/python -m pytest -v
```

Expected after each fix: all tests pass.

- [ ] **Step 5: Commit real-PDF parser adjustments**

If source files changed:

```bash
git add src/open_trader tests
git commit -m "fix: handle real broker statement layout"
```

If no source files changed, do not create an empty commit.

## Task 10: Final Verification

**Files:**
- No source files.

- [ ] **Step 1: Verify package entrypoint**

Run:

```bash
.venv/bin/python -m open_trader --help
```

Expected: output includes `import-statements`.

- [ ] **Step 2: Verify tests**

Run:

```bash
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 3: Verify git status**

Run:

```bash
git status --short
```

Expected: no uncommitted source changes. `data/` may exist locally but should be ignored.

## Self-Review

Spec coverage:

- Monthly directories and persistent outputs: Task 8.
- Final one-table `portfolio.csv`: Tasks 5 and 8.
- Merge same holdings across brokers: Task 5.
- HKD FX risk basis: Task 4 and Task 5.
- 10% overweight rule with cash and money market fund exemptions: Task 5.
- Futu, Tiger, Phillip parser adapters: Task 7.
- Manifest and parser warnings: Task 8.
- Latest output file: Task 8.
- Real PDF smoke run: Task 9.

Known deferral:

- TradingAgents analysis and watchlist generation are covered by the design spec but deferred to a second plan because they are an independent subsystem that depends on `portfolio.csv`.
