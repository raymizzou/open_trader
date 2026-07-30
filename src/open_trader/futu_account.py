from __future__ import annotations

import socket
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from .account_sync_state import BrokerAccountCandidate
from .fx import DEFAULT_RATES_TO_HKD, StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position
from .parsers.base import detect_asset_class


TRD_ENV_REAL = "REAL"
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
FUTU_NET_CASH_POWER_FIELDS = {
    currency: f"{currency.lower()}_net_cash_power"
    for currency, _, _ in FUTU_CASH_CURRENCY_FIELDS
}
FUTU_UNMAPPED_ASSETS_SYMBOL = "FUTU_UNMAPPED_ASSETS"


class FutuAccountError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


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
        available_balance = _optional_decimal(
            record, (FUTU_NET_CASH_POWER_FIELDS[currency], available_key)
        )
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


def build_futu_account_candidate(
    snapshot: FutuAccountSnapshot,
    *,
    run_date: str,
    data_as_of: str,
    fallback_fx_to_hkd: Mapping[str, Decimal],
) -> BrokerAccountCandidate:
    if not snapshot.accounts or any(
        not _is_real_security_account(account) for account in snapshot.accounts
    ):
        raise FutuAccountError(
            "no REAL Futu securities accounts found",
            error_type="no_real_accounts",
        )
    aliases_by_identity: dict[str, str] = {}
    for account in snapshot.accounts:
        account_id = str(account.acc_id)
        safe_alias = _mask_futu_account_alias(f"futu_{account_id}")
        for identity in (account_id, f"futu_{account_id}", safe_alias):
            if identity in aliases_by_identity:
                raise FutuAccountError(
                    "Futu snapshot has ambiguous account aliases",
                    error_type="account_query_failed",
                )
            aliases_by_identity[identity] = safe_alias

    def normalize_record(record: dict[str, object]) -> dict[str, object]:
        account_id = _first_text(record, ("_acc_id",))
        account_alias = _first_text(record, ("_account_alias",))
        safe_alias = aliases_by_identity.get(account_id or account_alias)
        if safe_alias is None or (
            account_id
            and account_alias
            and aliases_by_identity.get(account_alias) != safe_alias
        ):
            raise FutuAccountError(
                "Futu snapshot has an unrecognized account alias",
                error_type="account_query_failed",
            )
        return {**record, "_account_alias": safe_alias}

    normalized_snapshot = FutuAccountSnapshot(
        accounts=snapshot.accounts,
        cash_records=[normalize_record(record) for record in snapshot.cash_records],
        position_records=[
            normalize_record(record) for record in snapshot.position_records
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        normalized_snapshot,
        run_date=run_date,
    )
    if blocking_errors:
        raise FutuAccountError(
            "; ".join(blocking_errors),
            error_type="blocking_data_error",
        )
    if any(
        (total_assets := _first_raw_value(record, ("total_assets",))) not in (None, "")
        and _optional_decimal(record, ("total_assets",)) is None
        for record in normalized_snapshot.cash_records
    ):
        raise FutuAccountError(
            "Futu account total_assets is invalid",
            error_type="blocking_data_error",
        )

    fx_provider = StaticMonthEndFxProvider(
        run_date[:7], {**DEFAULT_RATES_TO_HKD, **fallback_fx_to_hkd}
    )
    try:
        positions = [
            *positions,
            *_unmapped_total_asset_positions(
                snapshot=normalized_snapshot,
                positions=positions,
                cash_balances=cash_balances,
                fx_provider=fx_provider,
                run_date=run_date,
            ),
        ]
        fx_rates = tuple(
            {
                "account_alias": account_alias,
                "currency": currency,
                "rate_to_hkd": format(
                    fx_provider.get_rate_to_hkd(currency).rate,
                    "f",
                ),
            }
            for account_alias, currency in sorted(
                {
                    (item.account_alias, item.currency.upper())
                    for item in [*positions, *cash_balances]
                }
            )
        )
    except KeyError as exc:
        raise FutuAccountError(str(exc), error_type="fx_rate_missing") from exc

    position_keys = {
        (
            item.broker,
            item.account_alias,
            item.market,
            item.asset_class,
            item.symbol.upper(),
            item.currency.upper(),
        )
        for item in positions
    }
    cash_keys = {
        (item.broker, item.account_alias, item.currency.upper())
        for item in cash_balances
    }
    if len(position_keys) != len(positions) or len(cash_keys) != len(cash_balances):
        raise FutuAccountError(
            "duplicate broker snapshot identity",
            error_type="duplicate_identity",
        )

    return BrokerAccountCandidate(
        broker="futu",
        source_kind="live",
        data_as_of=data_as_of,
        period=run_date[:7],
        positions=tuple(positions),
        cash=tuple(cash_balances),
        fx_rates=fx_rates,
        summary={
            "account_count": len(snapshot.accounts),
            "position_count": len(positions),
            "cash_count": len(cash_balances),
            "account_aliases": sorted(
                {item.account_alias for item in [*positions, *cash_balances]}
            ),
        },
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
    code = _first_text(record, ("code", "stock_code", "symbol")).upper()
    symbol = _symbol_from_code(code)
    name = _first_text(record, ("stock_name", "name", "security_name"), symbol)
    if symbol or name:
        return detect_asset_class(symbol, name)
    return AssetClass.UNKNOWN
