from __future__ import annotations

import csv
import json
import socket
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .csv_io import write_rows
from .fx import DEFAULT_RATES_TO_HKD, StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position
from .parsers.base import detect_asset_class
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows


TRD_ENV_REAL = "REAL"
POSITION_DETAIL_FIELDNAMES = [
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
CASH_DETAIL_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]
FUTU_CASH_CURRENCY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("HKD", "hk_cash", "hk_avl_withdrawal_cash"),
    ("USD", "us_cash", "us_avl_withdrawal_cash"),
    ("CNH", "cn_cash", "cn_avl_withdrawal_cash"),
    ("JPY", "jp_cash", "jp_avl_withdrawal_cash"),
    ("SGD", "sg_cash", "sg_avl_withdrawal_cash"),
    ("AUD", "au_cash", "au_avl_withdrawal_cash"),
    ("CAD", "ca_cash", "ca_avl_withdrawal_cash"),
    ("MYR", "my_cash", "my_avl_withdrawal_cash"),
)
FUTU_UNMAPPED_ASSETS_SYMBOL = "FUTU_UNMAPPED_ASSETS"


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
    acc_status: str = "ACTIVE"


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
    acc_status = _first_text(record, ("acc_status", "status"), "ACTIVE").upper()
    return FutuAccount(
        acc_id=acc_id,
        acc_index=acc_index,
        trd_env=trd_env,
        acc_type=acc_type,
        account_alias=f"futu_{acc_id}",
        acc_status=acc_status,
    )


def _is_real_security_account(account: FutuAccount) -> bool:
    return account.trd_env == TRD_ENV_REAL and account.acc_status == "ACTIVE"


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
        cash_balance
        for record in snapshot.cash_records
        for cash_balance in _cash_balances_from_record(
            record,
            statement_id,
            blocking_errors,
        )
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


def _cash_balances_from_record(
    record: dict[str, object],
    statement_id: str,
    blocking_errors: list[str],
) -> list[CashBalance]:
    cash_balances = _per_currency_cash_balances_from_record(record, statement_id)
    if cash_balances:
        return cash_balances
    fallback = _cash_from_record(record, statement_id, blocking_errors)
    return [] if fallback is None else [fallback]


def _per_currency_cash_balances_from_record(
    record: dict[str, object],
    statement_id: str,
) -> list[CashBalance]:
    cash_balances: list[CashBalance] = []
    for currency, cash_key, available_key in FUTU_CASH_CURRENCY_FIELDS:
        cash_value = _optional_decimal(record, (cash_key,))
        available_balance = _optional_decimal(record, (available_key,))
        if cash_value is None and available_balance is None:
            continue
        if (cash_value or Decimal("0")) == 0 and (
            available_balance or Decimal("0")
        ) == 0:
            continue
        cash_balances.append(
            CashBalance(
                statement_id=statement_id,
                broker="futu",
                account_alias=_first_text(record, ("_account_alias",), "futu_unknown"),
                currency=currency,
                cash_balance=(
                    cash_value if cash_value is not None else available_balance
                )
                or Decimal("0"),
                available_balance=available_balance,
                confidence="high" if cash_value is not None else "low",
                notes="Futu live account cash",
            )
        )
    return cash_balances


def _cash_from_record(
    record: dict[str, object],
    statement_id: str,
    blocking_errors: list[str],
) -> CashBalance | None:
    currency = _first_text(record, ("currency", "currency_type"), "HKD").upper()
    if currency in {"", "N/A"}:
        return None
    cash_value, cash_ok = _required_decimal(record, ("cash", "cash_balance", "total_cash"))
    available_balance = _optional_decimal(
        record, ("available_cash", "available_balance", "available_funds")
    )
    if cash_ok and cash_value == 0 and (available_balance is None or available_balance == 0):
        return None
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


def _unmapped_total_asset_positions(
    *,
    snapshot: FutuAccountSnapshot,
    positions: list[Position],
    cash_balances: list[CashBalance],
    fx_provider: StaticMonthEndFxProvider,
    run_date: str,
) -> list[Position]:
    mapped_hkd_by_account: dict[str, Decimal] = {}
    for position in positions:
        if position.market_value is None:
            continue
        mapped_hkd_by_account[position.account_alias] = mapped_hkd_by_account.get(
            position.account_alias,
            Decimal("0"),
        ) + (
            position.market_value
            * fx_provider.get_rate_to_hkd(position.currency.upper()).rate
        )
    for cash in cash_balances:
        mapped_hkd_by_account[cash.account_alias] = mapped_hkd_by_account.get(
            cash.account_alias,
            Decimal("0"),
        ) + (
            cash.cash_balance * fx_provider.get_rate_to_hkd(cash.currency.upper()).rate
        )

    adjustments: list[Position] = []
    statement_id = f"{run_date}-futu-live"
    for record in snapshot.cash_records:
        total_assets = _optional_decimal(record, ("total_assets",))
        if total_assets is None:
            continue
        account_alias = _first_text(record, ("_account_alias",), "futu_unknown")
        total_currency = _first_text(record, ("currency",), "HKD").upper()
        if total_currency in {"", "N/A"}:
            total_currency = "HKD"
        total_assets_hkd = (
            total_assets * fx_provider.get_rate_to_hkd(total_currency).rate
        )
        residual_hkd = total_assets_hkd - mapped_hkd_by_account.get(
            account_alias,
            Decimal("0"),
        )
        if abs(residual_hkd) < Decimal("0.01"):
            continue
        adjustments.append(
            Position(
                statement_id=statement_id,
                broker="futu",
                account_alias=account_alias,
                market=Market.CASH,
                asset_class=AssetClass.CASH,
                symbol=FUTU_UNMAPPED_ASSETS_SYMBOL,
                name="富途未明细账户资产",
                currency="HKD",
                quantity=Decimal("1"),
                cost_price=residual_hkd,
                last_price=residual_hkd,
                market_value=residual_hkd,
                cost_value=residual_hkd,
                unrealized_pnl=Decimal("0"),
                confidence="high",
                notes="Futu total_assets reconciliation for fund_assets or pending_asset not returned as positions",
            )
        )
    return adjustments


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
    code = _first_text(record, ("code", "stock_code", "symbol")).upper()
    symbol = _symbol_from_code(code)
    name = _first_text(record, ("stock_name", "name", "security_name"), symbol)
    if symbol or name:
        return detect_asset_class(symbol, name)
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
    fx_provider = _fx_provider_from_existing_rows(run_date, existing_rows)
    preserved_positions, preserved_cash = _latest_non_futu_statement_inputs(data_dir)
    use_statement_details = bool(preserved_positions or preserved_cash)
    if not use_statement_details:
        _raise_for_mixed_futu_broker_rows(existing_rows)
        preserved_rows = [row for row in existing_rows if not _has_futu_broker(row)]
    else:
        preserved_rows = []
    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date=run_date,
    )
    if use_statement_details:
        positions = _apply_asset_class_hints(
            positions,
            _asset_class_hints(preserved_positions),
        )
    asset_adjustments = _unmapped_total_asset_positions(
        snapshot=snapshot,
        positions=positions,
        cash_balances=cash_balances,
        fx_provider=fx_provider,
        run_date=run_date,
    )
    futu_positions = [*positions, *asset_adjustments]
    if use_statement_details:
        merged_rows = build_portfolio_rows(
            run_date[:7],
            [*preserved_positions, *futu_positions],
            [*preserved_cash, *cash_balances],
            fx_provider,
        )
    else:
        (
            preserved_portfolio_positions,
            preserved_portfolio_cash,
            preserved_has_invalid_market_value,
        ) = _portfolio_inputs_from_preserved_rows(preserved_rows)
        merged_rows = build_portfolio_rows(
            run_date[:7],
            [*preserved_portfolio_positions, *futu_positions],
            [*preserved_portfolio_cash, *cash_balances],
            fx_provider,
        )
        if preserved_has_invalid_market_value:
            _mark_all_rows_data_check(merged_rows)
    run_dir = data_dir / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = reports_dir / "futu_account"
    report_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = run_dir / "futu_account_snapshot.json"
    merged_portfolio_path = run_dir / "portfolio.csv"
    extracted_positions_path = run_dir / "extracted_positions.csv"
    extracted_cash_path = run_dir / "extracted_cash.csv"
    report_path = report_dir / f"{run_date}.md"
    snapshot_path.write_text(
        json.dumps(_snapshot_to_json(snapshot), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_rows(
        extracted_positions_path,
        POSITION_DETAIL_FIELDNAMES,
        (
            _position_to_statement_row(position)
            for position in [*preserved_positions, *futu_positions]
        ),
    )
    write_rows(
        extracted_cash_path,
        CASH_DETAIL_FIELDNAMES,
        (_cash_to_statement_row(cash) for cash in [*preserved_cash, *cash_balances]),
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
        _write_latest_portfolio_atomic(latest_path, merged_rows)
        updated_latest = True
    if update_latest and blocking_errors:
        raise FutuAccountError(
            "; ".join(blocking_errors),
            error_type="blocking_data_error",
        )
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
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _latest_non_futu_statement_inputs(
    data_dir: Path,
) -> tuple[list[Position], list[CashBalance]]:
    detail_dir = _latest_statement_detail_dir(data_dir)
    if detail_dir is None:
        return [], []
    positions = [
        _position_from_statement_row(row)
        for row in _read_csv_rows(detail_dir / "extracted_positions.csv")
        if row.get("broker", "").strip().lower() != "futu"
    ]
    cash_balances = [
        _cash_from_statement_row(row)
        for row in _read_csv_rows(detail_dir / "extracted_cash.csv")
        if row.get("broker", "").strip().lower() != "futu"
    ]
    return positions, cash_balances


def _latest_statement_detail_dir(data_dir: Path) -> Path | None:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    detail_dirs = [
        path
        for path in runs_dir.iterdir()
        if path.is_dir()
        and len(path.name) == 7
        and (path / "extracted_positions.csv").is_file()
    ]
    return max(detail_dirs, key=lambda path: path.name) if detail_dirs else None


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _position_from_statement_row(row: dict[str, str]) -> Position:
    quantity, quantity_ok = _required_decimal(row, ("quantity",))
    confidence = _confidence(row.get("confidence", ""), quantity_ok)
    return Position(
        statement_id=row.get("statement_id", ""),
        broker=row.get("broker", ""),
        account_alias=row.get("account_alias", ""),
        market=_market_from_text(row.get("market", "")),
        asset_class=_asset_class_from_text(row.get("asset_class", "")),
        symbol=row.get("symbol", ""),
        name=row.get("name", ""),
        currency=row.get("currency", "").upper(),
        quantity=quantity,
        cost_price=_optional_decimal(row, ("cost_price",)),
        last_price=_optional_decimal(row, ("last_price",)),
        market_value=_optional_decimal(row, ("market_value",)),
        cost_value=_optional_decimal(row, ("cost_value",)),
        unrealized_pnl=_optional_decimal(row, ("unrealized_pnl",)),
        confidence=confidence,
        notes=row.get("notes", ""),
    )


def _cash_from_statement_row(row: dict[str, str]) -> CashBalance:
    cash_balance, cash_ok = _required_decimal(row, ("cash_balance",))
    return CashBalance(
        statement_id=row.get("statement_id", ""),
        broker=row.get("broker", ""),
        account_alias=row.get("account_alias", ""),
        currency=row.get("currency", "").upper(),
        cash_balance=cash_balance,
        available_balance=_optional_decimal(row, ("available_balance",)),
        confidence=_confidence(row.get("confidence", ""), cash_ok),
        notes=row.get("notes", ""),
    )


def _portfolio_inputs_from_preserved_rows(
    rows: list[dict[str, str]],
) -> tuple[list[Position], list[CashBalance], bool]:
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    has_invalid_market_value = False
    for row in rows:
        if _parse_finite_decimal(row.get("market_value_hkd", "").strip()) is None:
            has_invalid_market_value = True
        if _parse_finite_decimal(row.get("market_value", "").strip()) is None:
            has_invalid_market_value = True
        market = _market_from_text(row.get("market", ""))
        asset_class = _asset_class_from_text(row.get("asset_class", ""))
        if (
            market == Market.CASH
            and asset_class == AssetClass.CASH
            and _is_currency_cash_portfolio_row(row)
        ):
            cash_balances.append(_cash_from_portfolio_row(row))
            continue
        positions.append(_position_from_portfolio_row(row))
    return positions, cash_balances, has_invalid_market_value


def _is_currency_cash_portfolio_row(row: dict[str, str]) -> bool:
    currency = row.get("currency", "").strip().upper()
    symbol = row.get("symbol", "").strip().upper()
    return bool(currency) and symbol == f"{currency}_CASH"


def _position_from_portfolio_row(row: dict[str, str]) -> Position:
    quantity, quantity_ok = _required_decimal(row, ("total_quantity",))
    market_value, market_value_ok = _market_value_from_portfolio_row(row)
    cost_value = _optional_decimal(row, ("cost_value",))
    required_fields_ok = (
        quantity_ok
        and market_value_ok
        and market_value is not None
        and cost_value is not None
    )
    return Position(
        statement_id="preserved-portfolio",
        broker=row.get("brokers", ""),
        account_alias=row.get("accounts", ""),
        market=_market_from_text(row.get("market", "")),
        asset_class=_asset_class_from_text(row.get("asset_class", "")),
        symbol=row.get("symbol", ""),
        name=row.get("name", ""),
        currency=row.get("currency", "").upper(),
        quantity=quantity,
        cost_price=_optional_decimal(row, ("avg_cost_price",)),
        last_price=_optional_decimal(row, ("last_price",)),
        market_value=market_value,
        cost_value=cost_value,
        unrealized_pnl=_optional_decimal(row, ("unrealized_pnl",)),
        confidence=_confidence(row.get("confidence", ""), required_fields_ok),
        notes=row.get("notes", ""),
    )


def _cash_from_portfolio_row(row: dict[str, str]) -> CashBalance:
    cash_balance, cash_ok = _market_value_from_portfolio_row(row)
    return CashBalance(
        statement_id="preserved-portfolio",
        broker=row.get("brokers", ""),
        account_alias=row.get("accounts", ""),
        currency=row.get("currency", "").upper(),
        cash_balance=cash_balance or Decimal("0"),
        available_balance=None,
        confidence=_confidence(row.get("confidence", ""), cash_ok),
        notes=row.get("notes", ""),
    )


def _market_value_from_portfolio_row(row: dict[str, str]) -> tuple[Decimal | None, bool]:
    market_value = _parse_finite_decimal(row.get("market_value", "").strip())
    if market_value is not None:
        return market_value, True
    market_value_hkd = _parse_finite_decimal(row.get("market_value_hkd", "").strip())
    fx_to_hkd = _parse_finite_decimal(row.get("fx_to_hkd", "").strip())
    if market_value_hkd is not None and fx_to_hkd is not None and fx_to_hkd > 0:
        return market_value_hkd / fx_to_hkd, False
    return None, False


def _position_to_statement_row(position: Position) -> dict[str, str]:
    return {
        "statement_id": position.statement_id,
        "broker": position.broker,
        "account_alias": position.account_alias,
        "market": position.market.value,
        "asset_class": position.asset_class.value,
        "symbol": position.symbol,
        "name": position.name,
        "currency": position.currency,
        "quantity": _decimal_to_str(position.quantity),
        "cost_price": _decimal_to_str(position.cost_price),
        "last_price": _decimal_to_str(position.last_price),
        "market_value": _decimal_to_str(position.market_value),
        "cost_value": _decimal_to_str(position.cost_value),
        "unrealized_pnl": _decimal_to_str(position.unrealized_pnl),
        "confidence": position.confidence,
        "notes": position.notes,
    }


def _cash_to_statement_row(cash: CashBalance) -> dict[str, str]:
    return {
        "statement_id": cash.statement_id,
        "broker": cash.broker,
        "account_alias": cash.account_alias,
        "currency": cash.currency,
        "cash_balance": _decimal_to_str(cash.cash_balance),
        "available_balance": _decimal_to_str(cash.available_balance),
        "confidence": cash.confidence,
        "notes": cash.notes,
    }


def _decimal_to_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _market_from_text(value: str) -> Market:
    try:
        return Market(value.strip().upper())
    except ValueError:
        return Market.OTHER


def _asset_class_from_text(value: str) -> AssetClass:
    try:
        return AssetClass(value.strip().lower())
    except ValueError:
        return AssetClass.UNKNOWN


def _confidence(value: str, required_fields_ok: bool) -> str:
    normalized = value.strip().lower()
    if required_fields_ok and normalized in {"high", "medium", "low"}:
        return normalized
    return "low"


def _asset_class_hints(
    positions: list[Position],
) -> dict[tuple[Market, str, str], AssetClass]:
    return {
        (
            position.market,
            position.symbol.upper(),
            position.currency.upper(),
        ): position.asset_class
        for position in positions
        if position.asset_class != AssetClass.UNKNOWN
    }


def _apply_asset_class_hints(
    positions: list[Position],
    hints: dict[tuple[Market, str, str], AssetClass],
) -> list[Position]:
    output: list[Position] = []
    for position in positions:
        hint = hints.get(
            (
                position.market,
                position.symbol.upper(),
                position.currency.upper(),
            )
        )
        if hint is not None:
            output.append(replace(position, asset_class=hint))
            continue
        output.append(position)
    return output


def _write_latest_portfolio_atomic(
    latest_path: Path,
    rows: list[dict[str, str]],
) -> None:
    temp_path = latest_path.with_name(f".{latest_path.name}.tmp")
    try:
        write_rows(temp_path, PORTFOLIO_FIELDNAMES, rows)
        temp_path.replace(latest_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


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
    return StaticMonthEndFxProvider(run_date[:7], {**DEFAULT_RATES_TO_HKD, **rates})


def _parse_finite_decimal(value_text: str) -> Decimal | None:
    if not value_text:
        return None
    try:
        value = Decimal(value_text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _mark_all_rows_data_check(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row["portfolio_weight_hkd"] = ""
        row["risk_flag"] = "data_check"


def _snapshot_to_json(snapshot: FutuAccountSnapshot) -> dict[str, object]:
    return {
        "accounts": [
            {
                "acc_id": _mask_account_id(account.acc_id),
                "acc_index": account.acc_index,
                "trd_env": account.trd_env,
                "acc_type": account.acc_type,
                "acc_status": account.acc_status,
                "account_alias": _mask_futu_account_alias(account.account_alias),
            }
            for account in snapshot.accounts
        ],
        "cash_records": [
            _json_safe_record(_mask_snapshot_record(record))
            for record in snapshot.cash_records
        ],
        "position_records": [
            _json_safe_record(_mask_snapshot_record(record))
            for record in snapshot.position_records
        ],
    }


def _mask_account_id(account_id: object) -> str:
    text = str(account_id).strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 8:
        return f"{'*' * 3}{text[-4:]}"
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def _mask_futu_account_alias(account_alias: object) -> str:
    text = str(account_alias).strip()
    prefix = "futu_"
    if not text.lower().startswith(prefix):
        return text
    return f"{text[:len(prefix)]}{_mask_account_id(text[len(prefix):])}"


def _mask_snapshot_record(record: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in record.items():
        if key == "_acc_id" and value is not None:
            output[key] = _mask_account_id(value)
        elif key == "_account_alias" and value is not None:
            output[key] = _mask_futu_account_alias(value)
        else:
            output[key] = value
    return output


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
