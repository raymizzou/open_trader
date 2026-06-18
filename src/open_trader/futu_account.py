from __future__ import annotations

import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .models import AssetClass, CashBalance, Market, Position


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
    return account.trd_env == TRD_ENV_REAL


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
    quantity, quantity_ok = _required_decimal(
        record, ("qty", "quantity", "position_qty"), "qty", code
    )
    last_price = _optional_decimal(record, ("nominal_price", "last_price", "price"))
    market_value = _optional_decimal(record, ("market_val", "market_value", "market_vale"))
    cost_price = _optional_decimal(record, ("cost_price", "average_cost"))
    raw_cost_value = _optional_decimal(record, ("cost_value", "cost_val"))
    cost_value = raw_cost_value
    if cost_value is None and cost_price is not None and quantity_ok:
        cost_value = cost_price * quantity
    unrealized_pnl = _optional_decimal(record, ("pl_val", "unrealized_pnl", "pl_value"))
    currency = _first_text(
        record, ("currency", "currency_type"), _default_currency_for_market(market)
    ).upper()
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
    cash_value, cash_ok = _required_decimal(
        record, ("cash", "cash_balance", "total_cash"), "cash", currency
    )
    available_balance = _optional_decimal(
        record, ("available_cash", "available_balance", "available_funds")
    )
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
