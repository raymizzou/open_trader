# Futu Account Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only Futu real-account sync that diagnoses account access, pulls live Futu holdings and cash, and merges those Futu rows into the standard `portfolio.csv` while preserving non-Futu statement rows.

**Architecture:** Add a focused `open_trader.futu_account` module for Futu trade-context access, record mapping, portfolio row replacement, artifact writing, and report rendering. Keep `open_trader.cli` as a thin wrapper that parses arguments, delegates to the module, prints counts and paths, and closes the Futu context. The sync derives FX rates from the existing portfolio before removing old Futu rows, so no new manual exchange-rate option is needed.

**Tech Stack:** Python 3.12, `futu-api` `OpenSecTradeContext`, stdlib `csv`/`json`/`decimal`/`socket`/`dataclasses`, existing `Position`/`CashBalance`/`build_portfolio_rows()`/`write_rows()`, pytest.

---

## File Structure

- Create `src/open_trader/futu_account.py`
  - Futu account dataclasses and `FutuAccountError`.
  - `FutuAccountClient` read-only wrapper around `OpenSecTradeContext`.
  - Mapping functions from Futu records to `Position` and `CashBalance`.
  - Portfolio merge, FX extraction, artifact writing, and Chinese report rendering.
- Create `tests/test_futu_account.py`
  - Unit tests for client diagnostics, mapping, merge behavior, artifact writing, and errors.
- Create `tests/test_futu_account_cli.py`
  - CLI help and wiring tests for `check-futu-account` and `sync-futu-portfolio`.
- Modify `src/open_trader/cli.py`
  - Import new Futu account helpers.
  - Register two subcommands.
  - Delegate command handling to `futu_account.py`.
- Modify `docs/monthly_portfolio_import.md`
  - Document the mixed-source workflow and manual verification commands.
- Modify `README.md` and `README.zh-CN.md`
  - Add concise references to Futu account sync.

---

### Task 1: Add Read-Only Futu Account Client

**Files:**
- Create: `src/open_trader/futu_account.py`
- Create: `tests/test_futu_account.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/test_futu_account.py` with this initial content:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_account import (
    FutuAccountClient,
    FutuAccountError,
)


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeSecTradeContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.accinfo_calls: list[dict[str, object]] = []
        self.position_calls: list[dict[str, object]] = []

    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 111,
                        "acc_index": 0,
                        "trd_env": "REAL",
                        "acc_type": "SECURITY",
                        "card_num": "12345678",
                    },
                    {
                        "acc_id": 222,
                        "acc_index": 1,
                        "trd_env": "SIMULATE",
                        "acc_type": "SECURITY",
                        "card_num": "SIM",
                    },
                ]
            ),
        )

    def accinfo_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        currency: str,
        asset_category: str,
    ) -> tuple[int, FakeDataFrame]:
        self.accinfo_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "currency": currency,
                "asset_category": asset_category,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "currency": "USD",
                        "cash": "100.25",
                        "available_cash": "88.50",
                        "total_assets": "1500",
                    }
                ]
            ),
        )

    def position_list_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        position_market: str,
        asset_category: str,
        currency: str,
    ) -> tuple[int, FakeDataFrame]:
        self.position_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "position_market": position_market,
                "asset_category": asset_category,
                "currency": currency,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "code": "US.MSFT",
                        "stock_name": "Microsoft",
                        "qty": "2",
                        "cost_price": "300",
                        "nominal_price": "410",
                        "market_val": "820",
                        "pl_val": "220",
                        "currency": "USD",
                        "stock_type": "STOCK",
                    }
                ]
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeNoRealAccountContext(FakeSecTradeContext):
    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 222,
                        "acc_index": 1,
                        "trd_env": "SIMULATE",
                        "acc_type": "SECURITY",
                    }
                ]
            ),
        )


class FakeFailingAccountContext(FakeSecTradeContext):
    def get_acc_list(self) -> tuple[int, str]:
        return -1, "account query failed"


def test_futu_account_client_fetches_only_real_accounts() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )

    snapshot = client.fetch_snapshot()

    assert len(snapshot.accounts) == 1
    assert snapshot.accounts[0].acc_id == 111
    assert snapshot.accounts[0].acc_index == 0
    assert snapshot.accounts[0].trd_env == "REAL"
    assert snapshot.accounts[0].account_alias == "futu_111"
    assert snapshot.cash_records[0]["cash"] == "100.25"
    assert snapshot.position_records[0]["code"] == "US.MSFT"
    assert client.context.accinfo_calls == [
        {
            "trd_env": "REAL",
            "acc_id": 111,
            "acc_index": 0,
            "refresh_cache": True,
            "currency": "HKD",
            "asset_category": "N/A",
        }
    ]
    assert client.context.position_calls == [
        {
            "trd_env": "REAL",
            "acc_id": 111,
            "acc_index": 0,
            "refresh_cache": True,
            "position_market": "N/A",
            "asset_category": "N/A",
            "currency": "USD",
        }
    ]


def test_futu_account_client_fails_fast_when_opend_unreachable() -> None:
    with pytest.raises(FutuAccountError) as exc_info:
        FutuAccountClient(
            host="127.0.0.1",
            port=11111,
            context_factory=FakeSecTradeContext,
            connectivity_checker=lambda host, port: False,
        )

    assert exc_info.value.error_type == "opend_unreachable"
    assert "Futu OpenD is not reachable" in str(exc_info.value)


def test_futu_account_client_reports_no_real_accounts() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeNoRealAccountContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "no_real_accounts"
    assert "no REAL Futu securities accounts found" in str(exc_info.value)


def test_futu_account_client_classifies_account_query_failure() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeFailingAccountContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "account_query_failed"
    assert "account query failed" in str(exc_info.value)


def test_futu_account_client_close_closes_context() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )

    client.close()

    assert client.context.closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'open_trader.futu_account'`.

- [ ] **Step 3: Implement the read-only client**

Create `src/open_trader/futu_account.py` with this content:

```python
from __future__ import annotations

import socket
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable


TRD_ENV_REAL = "REAL"


class FutuAccountError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class FutuAccount:
    acc_id: int
    acc_index: int
    trd_env: str
    acc_type: str
    account_alias: str


@dataclass(frozen=True)
class FutuAccountSnapshot:
    accounts: list[FutuAccount]
    cash_records: list[dict[str, object]]
    position_records: list[dict[str, object]]


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _default_trade_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenSecTradeContext
    except ImportError as exc:
        raise FutuAccountError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    return OpenSecTradeContext(host=host, port=port)


def _records(data: object) -> list[dict[str, object]]:
    if hasattr(data, "to_dict"):
        rows = data.to_dict("records")
        return [dict(row) for row in rows]
    raise FutuAccountError(
        f"Futu returned an unsupported table payload: {type(data).__name__}",
        error_type="trade_context_failed",
    )


def _as_int(value: object, *, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise FutuAccountError(
            f"Futu account field {field_name} is not an integer: {value!r}",
            error_type="account_query_failed",
        ) from exc


def _first_text(record: dict[str, object], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _account_from_record(record: dict[str, object]) -> FutuAccount:
    acc_id = _as_int(record.get("acc_id"), field_name="acc_id")
    acc_index = _as_int(record.get("acc_index", 0), field_name="acc_index")
    trd_env = _first_text(record, ("trd_env", "env", "trd_env_name")).upper()
    acc_type = _first_text(record, ("acc_type", "account_type"), "SECURITY").upper()
    return FutuAccount(
        acc_id=acc_id,
        acc_index=acc_index,
        trd_env=trd_env,
        acc_type=acc_type,
        account_alias=f"futu_{acc_id}",
    )


def _is_real_security_account(account: FutuAccount) -> bool:
    return account.trd_env == TRD_ENV_REAL and account.acc_type in {
        "SECURITY",
        "SEC",
        "STOCK",
        "UNIVERSAL",
    }


class FutuAccountClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_trade_context_factory,
        connectivity_checker: Callable[[str, int], bool] = _can_connect_to_opend,
    ) -> None:
        if not connectivity_checker(host, port):
            raise FutuAccountError(
                f"Futu OpenD is not reachable at {host}:{port}. Start OpenD, log in, and check host/port.",
                error_type="opend_unreachable",
            )
        try:
            self.context = context_factory(host=host, port=port)
        except FutuAccountError:
            raise
        except Exception as exc:
            raise FutuAccountError(
                f"failed to create Futu trade context at {host}:{port}: {exc}",
                error_type="trade_context_failed",
            ) from exc
        self.host = host
        self.port = port

    def fetch_snapshot(self) -> FutuAccountSnapshot:
        ret_code, data = self.context.get_acc_list()
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="account_query_failed")
        accounts = [
            account
            for account in (_account_from_record(record) for record in _records(data))
            if _is_real_security_account(account)
        ]
        if not accounts:
            raise FutuAccountError(
                "no REAL Futu securities accounts found",
                error_type="no_real_accounts",
            )

        cash_records: list[dict[str, object]] = []
        position_records: list[dict[str, object]] = []
        for account in accounts:
            cash_records.extend(self._fetch_cash_records(account))
            position_records.extend(self._fetch_position_records(account))
        return FutuAccountSnapshot(
            accounts=accounts,
            cash_records=cash_records,
            position_records=position_records,
        )

    def _fetch_cash_records(self, account: FutuAccount) -> list[dict[str, object]]:
        ret_code, data = self.context.accinfo_query(
            trd_env=TRD_ENV_REAL,
            acc_id=account.acc_id,
            acc_index=account.acc_index,
            refresh_cache=True,
            currency="HKD",
            asset_category="N/A",
        )
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="asset_query_failed")
        return [
            {**record, "_account_alias": account.account_alias, "_acc_id": account.acc_id}
            for record in _records(data)
        ]

    def _fetch_position_records(self, account: FutuAccount) -> list[dict[str, object]]:
        ret_code, data = self.context.position_list_query(
            trd_env=TRD_ENV_REAL,
            acc_id=account.acc_id,
            acc_index=account.acc_index,
            refresh_cache=True,
            position_market="N/A",
            asset_category="N/A",
            currency="USD",
        )
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="position_query_failed")
        return [
            {**record, "_account_alias": account.account_alias, "_acc_id": account.acc_id}
            for record in _records(data)
        ]

    def close(self) -> None:
        self.context.close()
```

- [ ] **Step 4: Run client tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit client foundation**

Run:

```bash
git add src/open_trader/futu_account.py tests/test_futu_account.py
git commit -m "feat: add futu account client"
```

---

### Task 2: Map Futu Records To Portfolio Inputs

**Files:**
- Modify: `src/open_trader/futu_account.py`
- Modify: `tests/test_futu_account.py`

- [ ] **Step 1: Add failing mapping tests**

Append these tests to `tests/test_futu_account.py`:

```python
from open_trader.models import AssetClass, Market
from open_trader.futu_account import map_snapshot_to_portfolio_inputs


def test_map_snapshot_to_portfolio_inputs_maps_positions_and_cash() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )
    snapshot = client.fetch_snapshot()

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    position = positions[0]
    assert position.statement_id == "2026-06-18-futu-live"
    assert position.broker == "futu"
    assert position.account_alias == "futu_111"
    assert position.market == Market.US
    assert position.asset_class == AssetClass.STOCK
    assert position.symbol == "MSFT"
    assert position.name == "Microsoft"
    assert position.currency == "USD"
    assert position.quantity == Decimal("2")
    assert position.cost_price == Decimal("300")
    assert position.last_price == Decimal("410")
    assert position.market_value == Decimal("820")
    assert position.cost_value == Decimal("600")
    assert position.unrealized_pnl == Decimal("220")
    assert position.confidence == "high"
    assert "Futu live account" in position.notes

    assert len(cash_balances) == 1
    cash = cash_balances[0]
    assert cash.statement_id == "2026-06-18-futu-live"
    assert cash.broker == "futu"
    assert cash.account_alias == "futu_111"
    assert cash.currency == "USD"
    assert cash.cash_balance == Decimal("100.25")
    assert cash.available_balance == Decimal("88.50")
    assert cash.confidence == "high"


def test_map_snapshot_marks_malformed_required_position_fields_low_confidence() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "100",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.BROKEN",
                "stock_name": "Broken",
                "qty": "not-a-number",
                "market_val": "100",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert cash_balances[0].cash_balance == Decimal("100")
    assert len(positions) == 1
    assert positions[0].symbol == "BROKEN"
    assert positions[0].quantity == Decimal("0")
    assert positions[0].market_value is None
    assert positions[0].confidence == "low"
    assert blocking_errors == [
        "position US.BROKEN has invalid required field qty='not-a-number'"
    ]


def test_map_snapshot_accepts_empty_positions() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "5000",
                "available_cash": "4500",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert positions == []
    assert cash_balances[0].symbol == "HKD_CASH"
    assert blocking_errors == []


def client_snapshot_from_records(
    *,
    cash_records: list[dict[str, object]],
    position_records: list[dict[str, object]],
) -> object:
    from open_trader.futu_account import FutuAccount, FutuAccountSnapshot

    return FutuAccountSnapshot(
        accounts=[
            FutuAccount(
                acc_id=111,
                acc_index=0,
                trd_env="REAL",
                acc_type="SECURITY",
                account_alias="futu_111",
            )
        ],
        cash_records=cash_records,
        position_records=position_records,
    )
```

- [ ] **Step 2: Run mapping tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: FAIL with `ImportError` for `map_snapshot_to_portfolio_inputs`.

- [ ] **Step 3: Add mapping implementation**

Append these imports near the top of `src/open_trader/futu_account.py`:

```python
from decimal import InvalidOperation

from .models import AssetClass, CashBalance, Market, Position
```

Append this code below `FutuAccountClient`:

```python
def map_snapshot_to_portfolio_inputs(
    snapshot: FutuAccountSnapshot,
    *,
    run_date: str,
) -> tuple[list[Position], list[CashBalance], list[str]]:
    statement_id = f"{run_date}-futu-live"
    blocking_errors: list[str] = []
    positions = [
        _position_from_record(record, statement_id, blocking_errors)
        for record in snapshot.position_records
    ]
    cash_balances = [
        _cash_from_record(record, statement_id, blocking_errors)
        for record in snapshot.cash_records
    ]
    return positions, cash_balances, blocking_errors


def _position_from_record(
    record: dict[str, object],
    statement_id: str,
    blocking_errors: list[str],
) -> Position:
    code = _first_text(record, ("code", "stock_code", "symbol")).upper()
    market = _market_from_code(code)
    symbol = _symbol_from_code(code)
    quantity, quantity_ok = _required_decimal(record, ("qty", "quantity", "position_qty"), "qty", code)
    last_price = _optional_decimal(record, ("nominal_price", "last_price", "price"))
    market_value = _optional_decimal(record, ("market_val", "market_value", "market_vale"))
    cost_price = _optional_decimal(record, ("cost_price", "average_cost"))
    raw_cost_value = _optional_decimal(record, ("cost_value", "cost_val"))
    cost_value = raw_cost_value
    if cost_value is None and cost_price is not None and quantity_ok:
        cost_value = cost_price * quantity
    unrealized_pnl = _optional_decimal(record, ("pl_val", "unrealized_pnl", "pl_value"))
    currency = _first_text(record, ("currency", "currency_type"), _default_currency_for_market(market)).upper()
    name = _first_text(record, ("stock_name", "name", "security_name"), symbol)
    if not quantity_ok:
        value = record.get("qty", record.get("quantity", record.get("position_qty")))
        blocking_errors.append(
            f"position {code or symbol} has invalid required field qty={value!r}"
        )
        market_value = None
        cost_value = None
        unrealized_pnl = None
    confidence = "high" if quantity_ok and market_value is not None else "low"
    return Position(
        statement_id=statement_id,
        broker="futu",
        account_alias=_first_text(record, ("_account_alias",), "futu_unknown"),
        market=market,
        asset_class=_asset_class_from_record(record),
        symbol=symbol,
        name=name,
        currency=currency,
        quantity=quantity,
        cost_price=cost_price,
        last_price=last_price,
        market_value=market_value,
        cost_value=cost_value,
        unrealized_pnl=unrealized_pnl,
        confidence=confidence,
        notes="Futu live account position",
    )


def _cash_from_record(
    record: dict[str, object],
    statement_id: str,
    blocking_errors: list[str],
) -> CashBalance:
    currency = _first_text(record, ("currency", "currency_type"), "HKD").upper()
    cash_value, cash_ok = _required_decimal(record, ("cash", "cash_balance", "total_cash"), "cash", currency)
    available_balance = _optional_decimal(record, ("available_cash", "available_balance", "available_funds"))
    if not cash_ok:
        value = record.get("cash", record.get("cash_balance", record.get("total_cash")))
        blocking_errors.append(
            f"cash {currency} has invalid required field cash={value!r}"
        )
    return CashBalance(
        statement_id=statement_id,
        broker="futu",
        account_alias=_first_text(record, ("_account_alias",), "futu_unknown"),
        currency=currency,
        cash_balance=cash_value,
        available_balance=available_balance,
        confidence="high" if cash_ok else "low",
        notes="Futu live account cash",
    )


def _required_decimal(
    record: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
    label: str,
) -> tuple[Decimal, bool]:
    raw_value = None
    for key in keys:
        if record.get(key) not in {None, ""}:
            raw_value = record.get(key)
            break
    if raw_value is None:
        return Decimal("0"), False
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0"), False
    if not value.is_finite():
        return Decimal("0"), False
    return value, True


def _optional_decimal(
    record: dict[str, object],
    keys: tuple[str, ...],
) -> Decimal | None:
    for key in keys:
        raw_value = record.get(key)
        if raw_value in {None, ""}:
            continue
        try:
            value = Decimal(str(raw_value).strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() else None
    return None


def _market_from_code(code: str) -> Market:
    if code.startswith("US."):
        return Market.US
    if code.startswith("HK."):
        return Market.HK
    return Market.OTHER


def _symbol_from_code(code: str) -> str:
    if "." in code:
        return code.split(".", 1)[1]
    return code


def _default_currency_for_market(market: Market) -> str:
    if market == Market.US:
        return "USD"
    if market == Market.HK:
        return "HKD"
    return "HKD"


def _asset_class_from_record(record: dict[str, object]) -> AssetClass:
    raw_type = _first_text(
        record,
        ("stock_type", "security_type", "asset_class", "sec_type"),
    ).upper()
    if raw_type in {"STOCK", "EQUITY", "COMMON_STOCK"}:
        return AssetClass.STOCK
    if raw_type in {"ETF", "EXCHANGE_TRADED_FUND"}:
        return AssetClass.ETF
    if raw_type in {"FUND", "MUTUAL_FUND"}:
        return AssetClass.FUND
    if raw_type in {"OPTION", "WARRANT"}:
        return AssetClass.OPTION
    return AssetClass.UNKNOWN
```

- [ ] **Step 4: Run mapping tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit mapping**

Run:

```bash
git add src/open_trader/futu_account.py tests/test_futu_account.py
git commit -m "feat: map futu account records"
```

---

### Task 3: Merge Futu Rows Into Standard Portfolio Artifacts

**Files:**
- Modify: `src/open_trader/futu_account.py`
- Modify: `tests/test_futu_account.py`

- [ ] **Step 1: Add failing merge and artifact tests**

Append these tests to `tests/test_futu_account.py`:

```python
import csv
import json
from pathlib import Path

from open_trader.futu_account import sync_futu_portfolio
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def read_portfolio(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def old_futu_row() -> dict[str, str]:
    return {
        "sort_group": "2",
        "market": "US",
        "asset_class": "stock",
        "symbol": "OLD",
        "name": "Old Futu",
        "currency": "USD",
        "total_quantity": "1",
        "avg_cost_price": "1.00",
        "last_price": "1.00",
        "market_value": "1",
        "cost_value": "1",
        "unrealized_pnl": "0.00",
        "unrealized_pnl_pct": "0.00%",
        "fx_source": "external_month_end_static",
        "fx_date": "2026-06-30",
        "fx_to_hkd": "7.8",
        "market_value_hkd": "7.80",
        "cost_value_hkd": "7.80",
        "portfolio_weight_hkd": "0.01%",
        "brokers": "futu",
        "accounts": "old",
        "ai_eligible": "true",
        "analysis_symbol": "OLD",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }


def tiger_row() -> dict[str, str]:
    return {
        "sort_group": "2",
        "market": "US",
        "asset_class": "stock",
        "symbol": "AAPL",
        "name": "Apple",
        "currency": "USD",
        "total_quantity": "1",
        "avg_cost_price": "100.00",
        "last_price": "200.00",
        "market_value": "200",
        "cost_value": "100",
        "unrealized_pnl": "100.00",
        "unrealized_pnl_pct": "100.00%",
        "fx_source": "external_month_end_static",
        "fx_date": "2026-06-30",
        "fx_to_hkd": "7.8",
        "market_value_hkd": "1560.00",
        "cost_value_hkd": "780.00",
        "portfolio_weight_hkd": "100.00%",
        "brokers": "tiger",
        "accounts": "tiger_main",
        "ai_eligible": "true",
        "analysis_symbol": "AAPL",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }


def test_sync_futu_portfolio_replaces_old_futu_rows_and_preserves_other_brokers(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.MSFT",
                "stock_name": "Microsoft",
                "qty": "2",
                "cost_price": "300",
                "nominal_price": "410",
                "market_val": "820",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    symbols = {row["symbol"] for row in rows}
    assert "OLD" not in symbols
    assert {"AAPL", "MSFT", "USD_CASH"} <= symbols
    msft = next(row for row in rows if row["symbol"] == "MSFT")
    assert msft["brokers"] == "futu"
    assert msft["market_value_hkd"] == "6396.00"
    assert msft["portfolio_weight_hkd"] == "75.32%"
    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert aapl["brokers"] == "tiger"
    assert aapl["portfolio_weight_hkd"] == "18.37%"
    assert result.latest_path == tmp_path / "data/latest/portfolio.csv"
    assert read_portfolio(result.latest_path)[0]["symbol"] == "OLD"
    assert result.updated_latest is False

    snapshot_payload = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot_payload["accounts"][0]["acc_id"] == 111
    report = result.report_path.read_text(encoding="utf-8")
    assert "富途账户同步" in report
    assert "真实账户：1" in report
    assert "未更新 latest" in report


def test_sync_futu_portfolio_updates_latest_only_when_requested(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=True,
    )

    rows = read_portfolio(result.latest_path)
    assert {row["symbol"] for row in rows} == {"AAPL", "USD_CASH"}
    assert result.updated_latest is True


def test_sync_futu_portfolio_blocks_latest_when_required_fields_are_malformed(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.BROKEN",
                "stock_name": "Broken",
                "qty": "bad",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    with pytest.raises(FutuAccountError) as exc_info:
        sync_futu_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-18",
            update_latest=True,
        )

    assert exc_info.value.error_type == "blocking_data_error"
    assert read_portfolio(portfolio_path)[0]["symbol"] == "OLD"
```

- [ ] **Step 2: Run merge tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: FAIL with `ImportError` for `sync_futu_portfolio`.

- [ ] **Step 3: Add merge and artifact implementation**

Append these imports to `src/open_trader/futu_account.py`:

```python
import csv
import json
from pathlib import Path

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows, pct
```

Append this implementation to `src/open_trader/futu_account.py`:

```python
@dataclass(frozen=True)
class FutuPortfolioSyncResult:
    run_date: str
    account_count: int
    position_count: int
    cash_count: int
    merged_row_count: int
    snapshot_path: Path
    portfolio_path: Path
    report_path: Path
    latest_path: Path
    updated_latest: bool


def sync_futu_portfolio(
    *,
    snapshot: FutuAccountSnapshot,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    update_latest: bool,
) -> FutuPortfolioSyncResult:
    existing_rows = _read_portfolio_rows(portfolio_path)
    fx_provider = _fx_provider_from_existing_rows(run_date, existing_rows)
    preserved_rows = [row for row in existing_rows if not _has_futu_broker(row)]
    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date=run_date,
    )
    if update_latest and blocking_errors:
        raise FutuAccountError(
            "; ".join(blocking_errors),
            error_type="blocking_data_error",
        )
    futu_rows = build_portfolio_rows(
        run_date[:7],
        positions,
        cash_balances,
        fx_provider,
    )
    merged_rows = _recalculate_combined_portfolio_rows([*preserved_rows, *futu_rows])
    run_dir = data_dir / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = run_dir / "futu_account_snapshot.json"
    merged_portfolio_path = run_dir / "portfolio.csv"
    report_path = run_dir / "futu_account_report.md"
    snapshot_path.write_text(
        json.dumps(_snapshot_to_json(snapshot), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_rows(merged_portfolio_path, PORTFOLIO_FIELDNAMES, merged_rows)
    report_path.write_text(
        _render_futu_account_report(
            account_count=len(snapshot.accounts),
            position_count=len(positions),
            cash_count=len(cash_balances),
            blocking_errors=blocking_errors,
            updated_latest=update_latest and not blocking_errors,
        ),
        encoding="utf-8",
    )
    latest_path = data_dir / "latest" / "portfolio.csv"
    updated_latest = False
    if update_latest and not blocking_errors:
        write_rows(latest_path, PORTFOLIO_FIELDNAMES, merged_rows)
        updated_latest = True
    return FutuPortfolioSyncResult(
        run_date=run_date,
        account_count=len(snapshot.accounts),
        position_count=len(positions),
        cash_count=len(cash_balances),
        merged_row_count=len(merged_rows),
        snapshot_path=snapshot_path,
        portfolio_path=merged_portfolio_path,
        report_path=report_path,
        latest_path=latest_path,
        updated_latest=updated_latest,
    )


def _read_portfolio_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _has_futu_broker(row: dict[str, str]) -> bool:
    brokers = row.get("brokers", "")
    parts = [
        part.strip().lower()
        for chunk in brokers.split(",")
        for part in chunk.split(";")
    ]
    return "futu" in parts


def _fx_provider_from_existing_rows(
    run_date: str,
    rows: list[dict[str, str]],
) -> StaticMonthEndFxProvider:
    rates: dict[str, Decimal] = {}
    for row in rows:
        currency = row.get("currency", "").strip().upper()
        rate_text = row.get("fx_to_hkd", "").strip()
        if not currency or currency == "HKD" or not rate_text:
            continue
        try:
            rate = Decimal(rate_text)
        except (InvalidOperation, ValueError):
            continue
        if rate.is_finite() and rate > 0:
            rates[currency] = rate
    return StaticMonthEndFxProvider(run_date[:7], rates)


def _recalculate_combined_portfolio_rows(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    normalized_rows = [
        {field: str(row.get(field, "")) for field in PORTFOLIO_FIELDNAMES}
        for row in rows
    ]
    values: list[Decimal] = []
    has_missing_value = False
    for row in normalized_rows:
        value_text = row.get("market_value_hkd", "").strip()
        if not value_text:
            has_missing_value = True
            continue
        try:
            value = Decimal(value_text)
        except (InvalidOperation, ValueError):
            has_missing_value = True
            continue
        if not value.is_finite():
            has_missing_value = True
            continue
        values.append(value)
    total = sum(values, Decimal("0"))
    for row in normalized_rows:
        if has_missing_value:
            row["portfolio_weight_hkd"] = ""
            row["risk_flag"] = "data_check"
            continue
        market_value_hkd = Decimal(row["market_value_hkd"] or "0")
        weight = market_value_hkd / total if total else Decimal("0")
        row["portfolio_weight_hkd"] = pct(weight)
        if (
            row["risk_flag"] != "data_check"
            and row["asset_class"] not in {"cash", "money_market_fund"}
            and weight > Decimal("0.10")
        ):
            row["risk_flag"] = "overweight"
    return sorted(
        normalized_rows,
        key=lambda row: (
            int(row.get("sort_group") or "9"),
            -Decimal(row.get("market_value_hkd") or "0"),
        ),
    )


def _snapshot_to_json(snapshot: FutuAccountSnapshot) -> dict[str, object]:
    return {
        "accounts": [
            {
                "acc_id": account.acc_id,
                "acc_index": account.acc_index,
                "trd_env": account.trd_env,
                "acc_type": account.acc_type,
                "account_alias": account.account_alias,
            }
            for account in snapshot.accounts
        ],
        "cash_records": [_json_safe_record(record) for record in snapshot.cash_records],
        "position_records": [
            _json_safe_record(record) for record in snapshot.position_records
        ],
    }


def _json_safe_record(record: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in record.items():
        if isinstance(value, Decimal):
            output[key] = str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            output[key] = value
        else:
            output[key] = str(value)
    return output


def _render_futu_account_report(
    *,
    account_count: int,
    position_count: int,
    cash_count: int,
    blocking_errors: list[str],
    updated_latest: bool,
) -> str:
    latest_text = "已更新 latest" if updated_latest else "未更新 latest"
    lines = [
        "# 富途账户同步",
        "",
        f"- 真实账户：{account_count}",
        f"- 富途持仓：{position_count}",
        f"- 现金币种：{cash_count}",
        f"- latest 状态：{latest_text}",
    ]
    if blocking_errors:
        lines.append("- 数据检查：需要复核")
        for error in blocking_errors:
            lines.append(f"- 问题：{error}")
    else:
        lines.append("- 数据检查：通过")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run merge tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py -q
```

Expected: PASS.

- [ ] **Step 5: Run focused existing portfolio tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio.py tests/test_futu_account.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit merge behavior**

Run:

```bash
git add src/open_trader/futu_account.py tests/test_futu_account.py
git commit -m "feat: sync futu portfolio artifacts"
```

---

### Task 4: Wire CLI Commands

**Files:**
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_futu_account_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_futu_account_cli.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_account import FutuAccount, FutuAccountSnapshot, FutuPortfolioSyncResult


def test_check_futu_account_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["check-futu-account", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output


def test_sync_futu_portfolio_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["sync-futu-portfolio", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--date" in output
    assert "--host" in output
    assert "--port" in output
    assert "--update-latest" in output


def test_check_futu_account_main_prints_diagnostic_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeFutuAccountClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port
            self.closed = False

        def fetch_snapshot(self) -> FutuAccountSnapshot:
            return FutuAccountSnapshot(
                accounts=[
                    FutuAccount(
                        acc_id=111,
                        acc_index=0,
                        trd_env="REAL",
                        acc_type="SECURITY",
                        account_alias="futu_111",
                    )
                ],
                cash_records=[{"currency": "USD", "cash": "100"}],
                position_records=[{"code": "US.MSFT"}],
            )

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli, "FutuAccountClient", FakeFutuAccountClient)

    result = cli.main(["check-futu-account", "--host", "127.0.0.1", "--port", "11111"])

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "connected to Futu OpenD at 127.0.0.1:11111" in output
    assert "real_accounts: 1" in output
    assert "positions: 1" in output
    assert "cash_records: 1" in output


def test_sync_futu_portfolio_main_wires_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    snapshot = FutuAccountSnapshot(
        accounts=[
            FutuAccount(
                acc_id=111,
                acc_index=0,
                trd_env="REAL",
                acc_type="SECURITY",
                account_alias="futu_111",
            )
        ],
        cash_records=[],
        position_records=[],
    )

    class FakeFutuAccountClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def fetch_snapshot(self) -> FutuAccountSnapshot:
            return snapshot

        def close(self) -> None:
            captured["closed"] = True

    def fake_sync_futu_portfolio(**kwargs: object) -> FutuPortfolioSyncResult:
        captured.update(kwargs)
        return FutuPortfolioSyncResult(
            run_date="2026-06-18",
            account_count=1,
            position_count=2,
            cash_count=1,
            merged_row_count=3,
            snapshot_path=tmp_path / "data/runs/2026-06-18/futu_account_snapshot.json",
            portfolio_path=tmp_path / "data/runs/2026-06-18/portfolio.csv",
            report_path=tmp_path / "data/runs/2026-06-18/futu_account_report.md",
            latest_path=tmp_path / "data/latest/portfolio.csv",
            updated_latest=True,
        )

    monkeypatch.setattr(cli, "FutuAccountClient", FakeFutuAccountClient)
    monkeypatch.setattr(cli, "sync_futu_portfolio", fake_sync_futu_portfolio)

    result = cli.main(
        [
            "sync-futu-portfolio",
            "--portfolio",
            str(tmp_path / "data/latest/portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-18",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["snapshot"] is snapshot
    assert captured["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["reports_dir"] == tmp_path / "reports"
    assert captured["run_date"] == "2026-06-18"
    assert captured["update_latest"] is True
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "run_date: 2026-06-18" in output
    assert "real_accounts: 1" in output
    assert "positions: 2" in output
    assert "cash: 1" in output
    assert "merged_rows: 3" in output
    assert "updated_latest: true" in output
```

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account_cli.py -q
```

Expected: FAIL because the parser has no `check-futu-account` or `sync-futu-portfolio` commands.

- [ ] **Step 3: Add CLI imports**

In `src/open_trader/cli.py`, add this import block near the existing Futu imports:

```python
from .futu_account import FutuAccountClient, FutuAccountError, sync_futu_portfolio
```

- [ ] **Step 4: Register CLI arguments**

In `build_parser()` after `check-futu-quotes`, add:

```python
    check_futu_account_parser = subparsers.add_parser(
        "check-futu-account",
        help="Diagnose read-only Futu real-account access",
    )
    check_futu_account_parser.add_argument("--host", default="127.0.0.1")
    check_futu_account_parser.add_argument("--port", type=positive_int, default=11111)

    sync_futu_portfolio_parser = subparsers.add_parser(
        "sync-futu-portfolio",
        help="Merge live Futu real-account data into portfolio.csv",
    )
    sync_futu_portfolio_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    sync_futu_portfolio_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sync_futu_portfolio_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    sync_futu_portfolio_parser.add_argument("--date", type=canonical_date, required=True)
    sync_futu_portfolio_parser.add_argument("--host", default="127.0.0.1")
    sync_futu_portfolio_parser.add_argument("--port", type=positive_int, default=11111)
    sync_futu_portfolio_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest/portfolio.csv after writing dated artifacts",
    )
```

- [ ] **Step 5: Add CLI command handlers**

In `main()` after the `check-futu-quotes` branch, add:

```python
    if args.command == "check-futu-account":
        account_client = None
        try:
            account_client = FutuAccountClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            snapshot = account_client.fetch_snapshot()
        except (RuntimeError, FutuAccountError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        print(f"real_accounts: {len(snapshot.accounts)}")
        print(f"positions: {len(snapshot.position_records)}")
        print(f"cash_records: {len(snapshot.cash_records)}")
        return 0

    if args.command == "sync-futu-portfolio":
        account_client = None
        try:
            account_client = FutuAccountClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            snapshot = account_client.fetch_snapshot()
            result = sync_futu_portfolio(
                snapshot=snapshot,
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                run_date=args.date,
                update_latest=args.update_latest,
            )
        except (FileNotFoundError, ValueError, RuntimeError, FutuAccountError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        print(f"run_date: {result.run_date}")
        print(f"real_accounts: {result.account_count}")
        print(f"positions: {result.position_count}")
        print(f"cash: {result.cash_count}")
        print(f"merged_rows: {result.merged_row_count}")
        print(f"snapshot: {result.snapshot_path}")
        print(f"portfolio: {result.portfolio_path}")
        print(f"report: {result.report_path}")
        print(f"latest: {result.latest_path}")
        print(f"updated_latest: {'true' if result.updated_latest else 'false'}")
        return 0
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account_cli.py tests/test_futu_account.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit CLI wiring**

Run:

```bash
git add src/open_trader/cli.py tests/test_futu_account_cli.py
git commit -m "feat: add futu account sync cli"
```

---

### Task 5: Document Workflow And Verify

**Files:**
- Modify: `docs/monthly_portfolio_import.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Add workflow docs**

Append this section to `docs/monthly_portfolio_import.md` near the existing Futu sections:

```markdown
## Futu Live Account Sync

Futu holdings can be pulled from Futu OpenD instead of re-importing a Futu
monthly statement. Other brokers still use the statement import workflow. The
sync replaces existing `brokers=futu` rows and keeps non-Futu broker rows from
the current portfolio.

First verify read-only account access:

```bash
.venv/bin/python -m open_trader check-futu-account
```

Then generate a dated merged portfolio without changing `data/latest`:

```bash
.venv/bin/python -m open_trader sync-futu-portfolio \
  --date 2026-06-18
```

Review:

- `data/runs/2026-06-18/futu_account_snapshot.json`
- `data/runs/2026-06-18/portfolio.csv`
- `data/runs/2026-06-18/futu_account_report.md`

After confirming the merged portfolio, promote it:

```bash
.venv/bin/python -m open_trader sync-futu-portfolio \
  --date 2026-06-18 \
  --update-latest
```

The command is read-only against Futu. It does not unlock trading, does not
store a trading password, and does not place orders.
```

- [ ] **Step 2: Add concise README references**

In `README.md`, add this bullet to the feature list:

```markdown
- Pull live Futu real-account holdings and cash into the standard portfolio CSV while keeping other brokers on statement imports.
```

In `README.zh-CN.md`, add this bullet to the feature list:

```markdown
- 从富途真实账户只读拉取实时持仓和现金，并与其他券商月结单数据合并为统一 portfolio CSV。
```

- [ ] **Step 3: Run focused docs and CLI verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py tests/test_futu_account_cli.py -q
.venv/bin/python -m open_trader check-futu-account --help
.venv/bin/python -m open_trader sync-futu-portfolio --help
```

Expected:

- pytest PASS.
- First help output includes `check-futu-account`.
- Second help output includes `--update-latest`.

- [ ] **Step 4: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 5: Manual Futu verification when OpenD is available**

Run:

```bash
.venv/bin/python -m open_trader check-futu-account
.venv/bin/python -m open_trader sync-futu-portfolio --date 2026-06-18
```

Expected when OpenD is logged in and trade account access is available:

- `check-futu-account` prints `connected to Futu OpenD at 127.0.0.1:11111`.
- It prints nonzero `real_accounts`.
- `sync-futu-portfolio` writes snapshot, portfolio, and report paths.
- It prints `updated_latest: false`.

Expected when OpenD or Futu trade-account access is unavailable:

- The command exits cleanly with a parser-style error.
- The error text identifies the Futu access failure such as `opend_unreachable`,
  `trade_context_failed`, `account_query_failed`, or `no_real_accounts`.

- [ ] **Step 6: Commit docs**

Run:

```bash
git add docs/monthly_portfolio_import.md README.md README.zh-CN.md
git commit -m "docs: document futu account sync"
```

---

## Final Verification

- [ ] Run:

```bash
git status --short
.venv/bin/python -m pytest -q
.venv/bin/python -m open_trader check-futu-account --help
.venv/bin/python -m open_trader sync-futu-portfolio --help
```

Expected:

- `git status --short` shows no tracked implementation changes left unstaged or uncommitted.
- pytest passes.
- Both help commands exit 0.

- [ ] If local Futu OpenD is running and logged in, run:

```bash
.venv/bin/python -m open_trader check-futu-account
.venv/bin/python -m open_trader sync-futu-portfolio --date 2026-06-18
```

Expected:

- Real-account diagnostics complete.
- Dated artifacts are written.
- `data/latest/portfolio.csv` is not changed unless `--update-latest` is used.

---

## Self-Review Notes

- Spec coverage: the plan covers read-only diagnostics, REAL account filtering, live Futu position and cash mapping, replacement of old Futu rows, preservation of non-Futu rows, dated artifacts, gated latest promotion, Chinese reporting, CLI commands, fake-client tests, and manual OpenD verification.
- Scope check: this is one cohesive subsystem. It does not add trading unlock, order placement, scheduler changes, or notification changes.
- Type consistency: `FutuAccountSnapshot`, `FutuPortfolioSyncResult`, `FutuAccountClient.fetch_snapshot()`, and `sync_futu_portfolio()` are defined before CLI tests use them.
