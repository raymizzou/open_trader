from __future__ import annotations

import csv
import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows, pct


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
    identity_ok = bool(code)
    if not identity_ok:
        value = _first_raw_value(record, ("code", "stock_code", "symbol"))
        blocking_errors.append(f"position has invalid required field code={value!r}")
    market = _market_from_code(code)
    symbol = _symbol_from_code(code)
    quantity, quantity_ok = _required_decimal(record, ("qty", "quantity", "position_qty"))
    last_price = _optional_decimal(record, ("nominal_price", "last_price", "price"))
    parsed_market_value, market_value_ok = _required_decimal(
        record, ("market_val", "market_value")
    )
    market_value = parsed_market_value if market_value_ok else None
    cost_price = _optional_decimal(record, ("cost_price", "average_cost"))
    raw_cost_value = _optional_decimal(record, ("cost_value", "cost_val"))
    cost_value = raw_cost_value
    if cost_value is None and cost_price is not None and quantity_ok:
        cost_value = cost_price * quantity
    cost_value_ok = cost_value is not None
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
    if not market_value_ok:
        value = _first_raw_value(record, ("market_val", "market_value"))
        blocking_errors.append(
            f"position {code or symbol} has invalid required field market_val={value!r}"
        )
    if quantity_ok and not cost_value_ok:
        value = _first_raw_value(record, ("cost_value", "cost_val"))
        blocking_errors.append(
            f"position {code or symbol} has invalid required field cost_value={value!r}"
        )
    confidence = (
        "high"
        if identity_ok and quantity_ok and market_value_ok and cost_value_ok
        else "low"
    )
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
    cash_value, cash_ok = _required_decimal(record, ("cash", "cash_balance", "total_cash"))
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


def _first_raw_value(record: dict[str, object], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in record:
            return record.get(key)
    return None


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
    _raise_for_mixed_futu_broker_rows(existing_rows)
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
    return "futu" in _broker_parts(row)


def _broker_parts(row: dict[str, str]) -> set[str]:
    brokers = row.get("brokers", "")
    return {
        part.strip().lower()
        for chunk in brokers.split(",")
        for part in chunk.split(";")
        if part.strip()
    }


def _raise_for_mixed_futu_broker_rows(rows: list[dict[str, str]]) -> None:
    for row in rows:
        parts = _broker_parts(row)
        if "futu" in parts and len(parts) > 1:
            symbol = row.get("symbol", "")
            brokers = row.get("brokers", "")
            raise FutuAccountError(
                f"portfolio row {symbol} mixes Futu with other brokers: {brokers}",
                error_type="mixed_futu_broker_row",
            )


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
