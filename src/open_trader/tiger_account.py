from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable

from .account_sync_state import BrokerAccountCandidate
from .fx import DEFAULT_RATES_TO_HKD, StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position, TradeFill
from .parsers.base import detect_asset_class

TIGER_UNMAPPED_ASSETS_SYMBOL = "TIGER_UNMAPPED_ASSETS"


class TigerAccountError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _market_from_text(value: str) -> Market:
    try:
        return Market(str(value or "").strip().upper())
    except ValueError:
        return Market.OTHER


def _decimal_to_str(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _parse_finite_decimal(value_text: str) -> Decimal | None:
    try:
        value = Decimal(value_text.strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


@dataclass(frozen=True)
class TigerAccountConfig:
    tiger_id: str
    account: str
    private_key_path: Path | None
    private_key: str | None = field(repr=False)
    secret_key: str | None = field(repr=False)
    token: str | None = field(repr=False)
    sandbox: bool
    config_dir: Path


@dataclass(frozen=True)
class TigerAccount:
    account: str
    account_alias: str
    account_type: str
    capability: str
    status: str
    asset_method: str


@dataclass(frozen=True)
class TigerAccountSnapshot:
    accounts: list[TigerAccount]
    cash_records: list[dict[str, object]]
    position_records: list[dict[str, object]]


def mask_account_id(account_id: object) -> str:
    text = str(account_id).strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 8:
        return f"{'*' * 3}{text[-4:]}"
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def _read_properties(config_dir: Path) -> dict[str, str]:
    path = config_dir.expanduser() / "tiger_openapi_config.properties"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().replace("\\n", "\n")
    return values


def load_tiger_account_config(
    *,
    config_dir: Path,
    account: str | None,
    sandbox: bool,
) -> TigerAccountConfig:
    expanded_config_dir = config_dir.expanduser()
    properties = _read_properties(expanded_config_dir)
    tiger_id = (
        os.environ.get("TIGEROPEN_TIGER_ID")
        or properties.get("tiger_id")
        or properties.get("tigerId")
        or ""
    ).strip()
    selected_account = (
        account
        or os.environ.get("TIGEROPEN_ACCOUNT")
        or properties.get("account")
        or ""
    ).strip()
    private_key_path_text = (
        os.environ.get("TIGEROPEN_PRIVATE_KEY_PATH")
        or properties.get("private_key_path")
        or ""
    ).strip()
    private_key = (
        os.environ.get("TIGEROPEN_PRIVATE_KEY")
        or properties.get("private_key_pk1")
        or properties.get("private_key")
        or None
    )
    private_key_path = Path(private_key_path_text).expanduser() if private_key_path_text else None
    secret_key = os.environ.get("TIGEROPEN_SECRET_KEY") or properties.get("secret_key")
    token = os.environ.get("TIGEROPEN_TOKEN") or properties.get("token")

    if private_key_path is not None:
        private_key = None
        if not private_key_path.exists() or not private_key_path.is_file():
            raise TigerAccountError(
                (
                    f"Tiger OpenAPI private key path is invalid: {private_key_path}. "
                    "Set TIGEROPEN_PRIVATE_KEY_PATH or private_key_path to an existing file."
                ),
                error_type="config_invalid",
            )

    if not tiger_id or not selected_account or (private_key_path is None and not private_key):
        raise TigerAccountError(
            (
                "Tiger OpenAPI configuration is incomplete. Provide tiger_id, "
                "account, and a PKCS#1 private key via ~/.tigeropen/"
                "tiger_openapi_config.properties or TIGEROPEN_* environment variables."
            ),
            error_type="config_missing",
        )
    return TigerAccountConfig(
        tiger_id=tiger_id,
        account=selected_account,
        private_key_path=private_key_path,
        private_key=private_key,
        secret_key=secret_key,
        token=token,
        sandbox=sandbox,
        config_dir=expanded_config_dir,
    )


def _get_attr(record: object, key: str, default: object = None) -> object:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _text(record: object, key: str, default: str = "") -> str:
    value = _get_attr(record, key)
    if value is None:
        return default
    value_text = str(value).strip()
    return value_text if value_text else default


def _attr_with_presence(record: object, key: str) -> tuple[object | None, bool]:
    if isinstance(record, dict):
        if key in record:
            return record[key], True
        return None, False
    if hasattr(record, key):
        return getattr(record, key), True
    return None, False


def _first_present_value(record: object, *keys: str) -> str | None:
    for key in keys:
        value, found = _attr_with_presence(record, key)
        if not found:
            continue
        normalized = _text({key: value}, key, None)
        if normalized is not None:
            return normalized
    return None


def _account_alias(account: str) -> str:
    text = str(account).strip()
    if not text:
        return "tiger_"
    if len(text) <= 4:
        return f"tiger_{text}"
    return f"tiger_{text[-4:]}"


def _compact_iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid ISO date: {value}") from None
    if parsed.isoformat() != value:
        raise ValueError(f"invalid ISO date: {value}")
    return value.replace("-", "")


def _tiger_trade_fill(
    transaction: object,
    *,
    account_alias: str,
    fees: Decimal | None,
) -> TradeFill:
    source_id = _text(transaction, "id")
    source_order_id = _text(transaction, "order_id")
    contract = _get_attr(transaction, "contract", None)
    symbol = _text(contract, "symbol").upper()
    currency = _text(contract, "currency").upper()
    side = _text(transaction, "action").upper()
    quantity = _parse_finite_decimal(_text(transaction, "filled_quantity"))
    price = _parse_finite_decimal(_text(transaction, "filled_price"))
    executed_at = _tiger_executed_at(_get_attr(transaction, "transacted_at", None))
    if (
        not source_id
        or not source_order_id
        or not symbol
        or side not in {"BUY", "SELL"}
        or quantity is None
        or quantity <= 0
        or price is None
        or price <= 0
        or executed_at is None
    ):
        raise ValueError("invalid Tiger fill")
    market = _tiger_transaction_market(transaction)
    return TradeFill(
        source_id=source_id,
        source_order_id=source_order_id,
        broker="tiger",
        account_alias=account_alias,
        market=market,
        symbol=symbol,
        currency=currency,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        executed_at=executed_at,
    )


def _tiger_transaction_market(transaction: object) -> Market:
    contract = _get_attr(transaction, "contract", None)
    market = _market_from_text(_text(contract, "market"))
    if market is not Market.OTHER:
        return market
    return {"USD": Market.US, "HKD": Market.HK, "CNY": Market.CN}.get(
        _text(contract, "currency").upper(), Market.OTHER
    )


def _tiger_executed_at(value: object) -> str | None:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return current.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        timestamp = Decimal(text)
    except InvalidOperation:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return None
    if not timestamp.is_finite():
        return None
    seconds = timestamp / 1000 if abs(timestamp) >= 100_000_000_000 else timestamp
    try:
        return datetime.fromtimestamp(float(seconds), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _tiger_order_fees(order: object | None) -> Decimal | None:
    if order is None:
        return None
    commission = _parse_finite_decimal(_text(order, "commission"))
    if commission is not None:
        return commission
    totals = [
        total
        for charge in (_get_attr(order, "charges", None) or [])
        if (total := _parse_finite_decimal(_text(charge, "total"))) is not None
    ]
    return sum(totals, Decimal("0")) if totals else None


def _is_active_account(account: object) -> bool:
    return _text(account, "status").upper() in {"FUNDED", "OPEN"}


def _asset_method_for_account_type(account_type: str) -> str:
    return "get_assets" if str(account_type).strip().upper() == "GLOBAL" else "get_prime_assets"


def _default_trade_client_factory(client_config: TigerAccountConfig) -> object:
    try:
        from tigeropen.trade.trade_client import TradeClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.common.consts import Language
    except ImportError:
        raise TigerAccountError(
            "Tiger OpenAPI SDK (tigeropen) is not installed. Install it before running Tiger sync.",
            error_type="tigeropen_missing",
        )

    private_key = client_config.private_key
    if private_key is None:
        if client_config.private_key_path is None:
            raise TigerAccountError(
                "Tiger OpenAPI private key is required",
                error_type="config_invalid",
            )
        try:
            private_key = client_config.private_key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TigerAccountError(
                f"Cannot read Tiger OpenAPI private key file: {client_config.private_key_path}",
                error_type="config_invalid",
            ) from exc

    if not private_key:
        raise TigerAccountError(
            "Tiger OpenAPI private key is required",
            error_type="config_invalid",
        )

    open_config = TigerOpenClientConfig(sandbox_debug=client_config.sandbox)
    open_config.tiger_id = client_config.tiger_id
    open_config.account = client_config.account
    open_config.private_key = private_key
    open_config.language = Language.zh_CN
    if client_config.secret_key:
        open_config.secret_key = client_config.secret_key
    if client_config.token:
        open_config.token = client_config.token

    try:
        return TradeClient(open_config)
    except Exception as exc:
        raise TigerAccountError(
            "failed to initialize Tiger TradeClient",
            error_type="config_invalid",
        ) from exc


class TigerAccountClient:
    def __init__(
        self,
        *,
        config: TigerAccountConfig,
        trade_client_factory: Callable[[TigerAccountConfig], object] = _default_trade_client_factory,
    ) -> None:
        self.config = config
        self.trade_client = self._make_trade_client(trade_client_factory)

    def _make_trade_client(
        self,
        trade_client_factory: Callable[[TigerAccountConfig], object],
    ) -> object:
        try:
            return self._coerce_trade_client_factory_call(trade_client_factory)
        except TigerAccountError:
            raise
        except Exception as exc:  # pragma: no cover - safety net for SDK init issues
            raise TigerAccountError(
                "failed to initialize Tiger TradeClient",
                error_type="config_invalid",
            ) from exc

    def _coerce_trade_client_factory_call(
        self,
        trade_client_factory: Callable[[TigerAccountConfig], object],
    ) -> object:
        try:
            signature = inspect.signature(trade_client_factory)
        except (TypeError, ValueError):
            return trade_client_factory(self.config)

        if self._factory_accepts_client_config_keyword(signature):
            return trade_client_factory(client_config=self.config)

        if self._factory_accepts_single_positional_arg(signature):
            return trade_client_factory(self.config)

        return trade_client_factory(self.config)

    @staticmethod
    def _factory_accepts_client_config_keyword(signature: inspect.Signature) -> bool:
        kwargs: dict[str, object] = {"client_config": None}
        try:
            signature.bind_partial(**kwargs)
            return True
        except TypeError:
            return False

    @staticmethod
    def _factory_accepts_single_positional_arg(signature: inspect.Signature) -> bool:
        try:
            signature.bind(None)
            return True
        except TypeError:
            return False

    def fetch_snapshot(self) -> TigerAccountSnapshot:
        if not hasattr(self.trade_client, "get_managed_accounts"):
            raise TigerAccountError(
                "Tiger OpenAPI TradeClient is unavailable. Install tigeropen and retry.",
                error_type="tigeropen_missing",
            )

        try:
            profiles = list(self.trade_client.get_managed_accounts(account=self.config.account))
        except Exception as exc:
            raise TigerAccountError(
                "failed to query Tiger managed accounts",
                error_type="account_query_failed",
            ) from exc

        matching_accounts = []
        for profile in profiles:
            account = self._parse_account(profile)
            if (
                account is not None
                and account.account == self.config.account
                and _is_active_account(profile)
            ):
                matching_accounts.append(account)

        if not matching_accounts:
            raise TigerAccountError(
                f"no active Tiger accounts matched account {mask_account_id(self.config.account)}",
                error_type="no_matching_accounts",
            )

        position_records: list[dict[str, object]] = []
        cash_records: list[dict[str, object]] = []

        for account in matching_accounts:
            position_records.extend(self._fetch_position_records(account))
            cash_records.extend(self._fetch_cash_records(account))

        return TigerAccountSnapshot(
            accounts=matching_accounts,
            cash_records=cash_records,
            position_records=position_records,
        )

    def fetch_actual_fills(self, start_date: str, end_date: str) -> list[TradeFill]:
        since_date = _compact_iso_date(start_date)
        to_date = _compact_iso_date(end_date)
        transactions: list[object] = []
        page_token: str | None = None
        while True:
            try:
                response = self.trade_client.get_transactions(
                    account=self.config.account,
                    since_date=since_date,
                    to_date=to_date,
                    limit=100,
                    page_token=page_token,
                )
            except Exception as exc:
                message = str(exc).lower()
                if "symbol" not in message or "empty" not in message:
                    raise TigerAccountError(
                        "failed to query Tiger transactions",
                        error_type="transaction_query_failed",
                    ) from exc
                try:
                    current_day = date.fromisoformat(start_date)
                    final_day = date.fromisoformat(end_date)
                    while current_day <= final_day:
                        next_day = current_day + timedelta(days=1)
                        response = self.trade_client.get_filled_orders(
                            account=self.config.account,
                            start_time=f"{current_day.isoformat()} 00:00:00",
                            end_time=f"{next_day.isoformat()} 00:00:00",
                            limit=100,
                        )
                        if response is None:
                            raise ValueError("Tiger filled orders are unavailable")
                        orders = (
                            list(response)
                            if isinstance(response, list)
                            else list(_get_attr(response, "result", []))
                        )
                        if len(orders) >= 100:
                            raise ValueError("Tiger filled orders may be truncated")
                        for order in orders:
                            order_id = _get_attr(
                                order, "id", _get_attr(order, "order_id")
                            )
                            if order_id is None:
                                raise ValueError("Tiger filled order is missing id")
                            scoped_transactions: list[object] = []
                            scoped_page_token: str | None = None
                            while True:
                                scoped = self.trade_client.get_transactions(
                                    account=self.config.account,
                                    order_id=order_id,
                                    limit=100,
                                    page_token=scoped_page_token,
                                )
                                if scoped is None:
                                    raise ValueError(
                                        "Tiger order transactions are unavailable"
                                    )
                                if isinstance(scoped, list):
                                    page = list(scoped)
                                    if not page:
                                        raise ValueError(
                                            "Tiger order transactions are empty"
                                        )
                                    scoped_transactions.extend(page)
                                    if len(page) < 100:
                                        break
                                    if scoped_page_token is not None:
                                        raise ValueError(
                                            "Tiger order transaction pagination is incomplete"
                                        )
                                    scoped_page_token = ""
                                    continue
                                page = list(_get_attr(scoped, "result", []))
                                if not page:
                                    raise ValueError(
                                        "Tiger order transactions are empty"
                                    )
                                scoped_transactions.extend(page)
                                next_token = _get_attr(
                                    scoped,
                                    "next_page_token",
                                    _get_attr(scoped, "page_token", None),
                                )
                                if next_token:
                                    scoped_page_token = str(next_token)
                                    continue
                                if len(page) >= 100:
                                    raise ValueError(
                                        "Tiger order transaction pagination is incomplete"
                                    )
                                break
                            transactions.extend(scoped_transactions)
                        current_day = next_day
                except Exception as fallback_exc:
                    raise TigerAccountError(
                        "failed to query Tiger transactions",
                        error_type="transaction_query_failed",
                    ) from fallback_exc
                break
            if response is None:
                break
            if isinstance(response, list):
                transactions.extend(response)
                if page_token is None and len(response) == 100:
                    page_token = ""
                    continue
                break
            transactions.extend(list(_get_attr(response, "result", [])))
            next_token = _get_attr(
                response,
                "next_page_token",
                _get_attr(response, "page_token", None),
            )
            if not next_token:
                break
            page_token = str(next_token)

        transactions = list(
            {_text(transaction, "id"): transaction for transaction in transactions}.values()
        )
        transactions = [
            transaction
            for transaction in transactions
            if _tiger_transaction_market(transaction) is Market.US
        ]
        counts_by_order: dict[str, int] = {}
        for transaction in transactions:
            order_id = _text(transaction, "order_id")
            counts_by_order[order_id] = counts_by_order.get(order_id, 0) + 1
        orders: dict[str, object | None] = {}
        for transaction in transactions:
            order_id = _text(transaction, "order_id")
            if order_id in orders:
                continue
            try:
                orders[order_id] = self.trade_client.get_order(
                    order_id=_get_attr(transaction, "order_id"),
                    show_charges=True,
                )
            except Exception as exc:
                raise TigerAccountError(
                    "failed to query Tiger order charges",
                    error_type="order_query_failed",
                ) from exc

        try:
            return [
                _tiger_trade_fill(
                    transaction,
                    account_alias=_account_alias(self.config.account),
                    fees=(
                        _tiger_order_fees(orders[_text(transaction, "order_id")])
                        if counts_by_order[_text(transaction, "order_id")] == 1
                        else None
                    ),
                )
                for transaction in transactions
            ]
        except (InvalidOperation, ValueError) as exc:
            raise TigerAccountError(
                "Tiger transaction response contains invalid fill data",
                error_type="transaction_invalid",
            ) from exc

    def close(self) -> None:
        close = getattr(self.trade_client, "close", None)
        if callable(close):
            close()

    def _parse_account(self, profile: object) -> TigerAccount | None:
        account_id = _text(profile, "account")
        if not account_id:
            return None
        account_type = _text(profile, "accountType", "STANDARD").upper() or "STANDARD"
        capability = _text(profile, "capability").upper() or ""
        status = _text(profile, "status").upper() or ""
        return TigerAccount(
            account=account_id,
            account_alias=_account_alias(account_id),
            account_type=account_type,
            capability=capability,
            status=status,
            asset_method=_asset_method_for_account_type(account_type),
        )

    def _fetch_position_records(self, account: TigerAccount) -> list[dict[str, object]]:
        try:
            positions = [
                position
                for sec_type in ("STK", "FUND")
                for position in self.trade_client.get_positions(
                    account=account.account,
                    sec_type=sec_type,
                )
            ]
        except Exception as exc:
            raise TigerAccountError(
                "failed to query Tiger account positions",
                error_type="position_query_failed",
            ) from exc
        return [self._position_record(account, position) for position in positions]

    def _fetch_cash_records(self, account: TigerAccount) -> list[dict[str, object]]:
        if account.asset_method == "get_assets":
            try:
                payload = self.trade_client.get_assets(
                    account=account.account,
                    market_value=True,
                )
            except Exception as exc:
                raise TigerAccountError(
                    "failed to query Tiger assets",
                    error_type="asset_query_failed",
                ) from exc
            return self._records_from_assets(account, payload)

        try:
            payload = self.trade_client.get_prime_assets(account=account.account)
        except Exception as exc:
            raise TigerAccountError(
                "failed to query Tiger assets",
                error_type="asset_query_failed",
            ) from exc
        return self._records_from_prime_assets(account, payload)

    def _position_record(self, account: TigerAccount, position: object) -> dict[str, object]:
        contract = _get_attr(position, "contract", None)
        return {
            "account": account.account,
            "account_alias": account.account_alias,
            "symbol": _text(contract, "symbol"),
            "name": _text(contract, "name"),
            "sec_type": _text(contract, "sec_type"),
            "currency": _text(contract, "currency"),
            "market": _text(contract, "market"),
            "position_qty": _text(position, "position_qty"),
            "average_cost": _text(position, "average_cost"),
            "market_price": _text(position, "market_price"),
            "market_value": _text(position, "market_value"),
            "unrealized_pnl": _text(position, "unrealized_pnl"),
            "source": "get_positions",
        }

    def _records_from_prime_assets(
        self,
        account: TigerAccount,
        payload: object,
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        segments = _get_attr(payload, "segments", None)
        if not isinstance(segments, dict):
            raise TigerAccountError(
                "Tiger assets response is incomplete",
                error_type="asset_query_failed",
            )
        segment = segments.get("S")
        if segment is None:
            segment = next(
                (
                    candidate
                    for candidate in segments.values()
                    if _text(candidate, "category").upper() == "S"
                ),
                None,
            )
        if segment is None:
            raise TigerAccountError(
                "Tiger assets response is incomplete",
                error_type="asset_query_failed",
            )

        currency_assets = _get_attr(segment, "currency_assets", None)
        if not isinstance(currency_assets, dict):
            raise TigerAccountError(
                "Tiger assets response is incomplete",
                error_type="asset_query_failed",
            )
        account_total = _first_present_value(
            segment,
            "equity_with_loan",
            "net_liquidation",
        )
        if account_total is not None:
            if not _text(segment, "currency"):
                raise TigerAccountError(
                    "Tiger assets response is incomplete",
                    error_type="asset_query_failed",
                )
            record = {
                "record_type": "account_total",
                "account": account.account,
                "account_alias": account.account_alias,
                "currency": _text(segment, "currency"),
                "account_total": account_total,
                "segment_category": _text(segment, "category"),
                "net_liquidation": _text(segment, "net_liquidation"),
                "equity_with_loan": _text(segment, "equity_with_loan"),
                "cash_balance": _text(segment, "cash_balance"),
                "cash_available_for_trade": _text(
                    segment, "cash_available_for_trade"
                ),
                "locked_funds": _text(segment, "locked_funds"),
                "uncollected": _text(segment, "uncollected"),
                "source": account.asset_method,
            }
            fx_to_hkd = _prime_fx_to_hkd(
                currency_assets,
                _text(segment, "currency"),
            )
            if fx_to_hkd:
                record["fx_to_hkd"] = fx_to_hkd
            records.append(record)

        for currency_asset in currency_assets.values():
            if not _text(currency_asset, "currency"):
                raise TigerAccountError(
                    "Tiger assets response is incomplete",
                    error_type="asset_query_failed",
                )
            if not self._has_non_zero_balance(currency_asset):
                continue
            record = {
                "account": account.account,
                "account_alias": account.account_alias,
                "currency": _text(currency_asset, "currency"),
                "cash_balance": _text(currency_asset, "cash_balance"),
                "available_balance": _first_present_value(
                    currency_asset,
                    "cash_available_for_trade",
                    "cash_available_for_withdrawal",
                ),
                "gross_position_value": _text(
                    currency_asset, "gross_position_value"
                ),
                "source": account.asset_method,
            }
            fx_to_hkd = _prime_fx_to_hkd(
                currency_assets,
                _text(currency_asset, "currency"),
            )
            if fx_to_hkd:
                record["fx_to_hkd"] = fx_to_hkd
            records.append(record)
        return records

    @staticmethod
    def _has_non_zero_balance(currency_asset: object) -> bool:
        balance_fields = (
            _get_attr(currency_asset, "cash_balance", ""),
            _get_attr(currency_asset, "cash_available_for_withdrawal", ""),
            _get_attr(currency_asset, "cash_available_for_trade", ""),
            _get_attr(currency_asset, "gross_position_value", ""),
        )
        for raw_value in balance_fields:
            if raw_value:
                try:
                    value = Decimal(raw_value)
                except Exception:
                    return True
                if value.is_finite() and value != 0:
                    return True
        return False

    def _records_from_assets(self, account: TigerAccount, payload: object) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        payload_accounts: list[object]
        if isinstance(payload, list):
            payload_accounts = list(payload)
        else:
            payload_accounts = [payload]

        matched_account = False
        for payload_account in payload_accounts:
            if _text(payload_account, "account") != account.account:
                continue
            matched_account = True
            market_values = _get_attr(payload_account, "market_values", None)
            if not isinstance(market_values, dict):
                raise TigerAccountError(
                    "Tiger assets response is incomplete",
                    error_type="asset_query_failed",
                )
            for market_value in market_values.values():
                if not _text(market_value, "currency"):
                    raise TigerAccountError(
                        "Tiger assets response is incomplete",
                        error_type="asset_query_failed",
                    )
                records.append(
                    {
                        "account": account.account,
                        "account_alias": account.account_alias,
                        "currency": _text(market_value, "currency"),
                        "cash_balance": _text(market_value, "cash_balance"),
                        "available_balance": _first_present_value(
                            market_value,
                            "cash_available_for_trade",
                            "cash_available_for_withdrawal",
                            "available_balance",
                        ),
                        "gross_position_value": _first_present_value(
                            market_value,
                            "gross_position_value",
                            "net_liquidation",
                        ),
                        "source": account.asset_method,
                    }
                )
        if not matched_account:
            raise TigerAccountError(
                "Tiger assets response is incomplete",
                error_type="asset_query_failed",
            )
        return records


def _prime_fx_to_hkd(currency_assets: dict[object, object], currency: str) -> str:
    normalized_currency = currency.strip().upper()
    if normalized_currency == "HKD":
        return "1"
    assets = {
        _text(asset, "currency").upper(): asset for asset in currency_assets.values()
    }
    currency_rate = _positive_decimal_attr(assets.get(normalized_currency), "forex_rate")
    hkd_rate = _positive_decimal_attr(assets.get("HKD"), "forex_rate")
    if currency_rate is None or hkd_rate is None:
        return ""
    return _decimal_to_str(currency_rate / hkd_rate)


def _positive_decimal_attr(record: object, key: str) -> Decimal | None:
    raw_value = _get_attr(record, key, None)
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def map_snapshot_to_portfolio_inputs(
    snapshot: TigerAccountSnapshot,
    *,
    run_date: str,
) -> tuple[list[Position], list[CashBalance], list[str]]:
    statement_id = f"{run_date}-tiger-live"
    blocking_errors: list[str] = []
    # Malformed position rows are intentionally excluded from downstream inputs after
    # recording blocking errors, so we never emit fake zero/NaN quantities.
    positions = [
        position
        for position in (
            _position_from_record(record, statement_id, blocking_errors)
            for record in snapshot.position_records
        )
        if position is not None
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
) -> Position | None:
    raw_symbol: object | None = None
    for key in ("symbol", "code", "security_code", "ticker"):
        value, found = _attr_with_presence(record, key)
        if found:
            if _is_blank_scalar(value):
                continue
            raw_symbol = value
            break
    if raw_symbol is None or str(raw_symbol).strip() == "":
        blocking_errors.append("position has invalid required field symbol=None")
        return None

    symbol = str(raw_symbol).strip().upper()
    identity_ok = bool(symbol)

    quantity, quantity_ok, quantity_raw = _required_decimal(
        record,
        ("position_qty", "quantity"),
    )
    market_value, market_value_ok, market_value_raw = _required_decimal(
        record,
        ("market_value",),
    )
    if not quantity_ok:
        blocking_errors.append(
            f"position {symbol} has invalid required field position_qty={quantity_raw!r}"
        )
    if not market_value_ok:
        blocking_errors.append(
            f"position {symbol} has invalid required field market_value={market_value_raw!r}"
        )
    if not quantity_ok or not market_value_ok:
        return None

    cost_price = _optional_decimal(record, ("average_cost",))
    cost_value = (
        cost_price * quantity if cost_price is not None else None
    )
    market = _market_from_record(record)
    return Position(
        statement_id=statement_id,
        broker="tiger",
        account_alias=_text(record, "account_alias", "tiger_unknown"),
        market=market,
        asset_class=_asset_class_from_record(record),
        symbol=symbol,
        name=_text(record, "name", symbol),
        currency=_currency_from_record(record, market),
        quantity=quantity,
        cost_price=cost_price,
        last_price=_optional_decimal(record, ("market_price", "last_price")),
        market_value=market_value,
        cost_value=cost_value,
        unrealized_pnl=_optional_decimal(record, ("unrealized_pnl",)),
        confidence=(
            "high"
            if identity_ok and quantity_ok and market_value_ok
            else "low"
        ),
        notes="Tiger live account position",
    )


def _cash_balances_from_record(
    record: dict[str, object],
    statement_id: str,
    blocking_errors: list[str],
) -> list[CashBalance]:
    if _text(record, "record_type") == "account_total":
        return []

    currency = _text(record, "currency").upper()
    if currency in {"", "N/A"}:
        return []

    cash_balance, cash_ok, cash_raw = _required_decimal(record, ("cash_balance", "cash"))
    available_balance = _optional_decimal(record, ("available_balance",))
    gross_position_value = _optional_decimal(record, ("gross_position_value",))
    if cash_ok and cash_balance == 0 and (
        available_balance is None or available_balance == 0
    ) and not (
        gross_position_value is not None
        and gross_position_value.is_finite()
        and gross_position_value != 0
    ):
        return []

    if not cash_ok:
        blocking_errors.append(
            f"cash {currency} has invalid required field cash_balance={cash_raw!r}"
        )
        return []

    return [
        CashBalance(
            statement_id=statement_id,
            broker="tiger",
            account_alias=_text(record, "account_alias", "tiger_unknown"),
            currency=currency,
            cash_balance=cash_balance,
            available_balance=available_balance,
            confidence="high" if cash_ok else "low",
            notes="Tiger live account cash",
        )
    ]


def _unmapped_total_asset_positions(
    *,
    snapshot: TigerAccountSnapshot,
    positions: list[Position],
    cash_balances: list[CashBalance],
    fx_to_hkd: dict[tuple[str, str], Decimal],
    run_date: str,
) -> list[Position]:
    mapped_hkd_by_account: dict[str, Decimal] = {}
    for position in positions:
        if position.market_value is None:
            continue
        rate = fx_to_hkd.get((position.account_alias, position.currency.upper()))
        if rate is None:
            return []
        mapped_hkd_by_account[position.account_alias] = mapped_hkd_by_account.get(
            position.account_alias,
            Decimal("0"),
        ) + position.market_value * rate
    for cash in cash_balances:
        rate = fx_to_hkd.get((cash.account_alias, cash.currency.upper()))
        if rate is None:
            return []
        mapped_hkd_by_account[cash.account_alias] = mapped_hkd_by_account.get(
            cash.account_alias,
            Decimal("0"),
        ) + cash.cash_balance * rate

    adjustments: list[Position] = []
    statement_id = f"{run_date}-tiger-live"
    for record in snapshot.cash_records:
        if _text(record, "record_type") != "account_total":
            continue
        account_total = _optional_decimal(record, ("account_total",))
        if account_total is None:
            continue
        account_alias = _text(record, "account_alias", "tiger_unknown")
        total_currency = _text(record, "currency", "USD").upper()
        if total_currency in {"", "N/A"}:
            total_currency = "USD"
        total_rate = fx_to_hkd.get(
            (account_alias, total_currency),
        )
        if total_rate is None:
            continue
        total_assets_hkd = account_total * total_rate
        residual_hkd = total_assets_hkd - mapped_hkd_by_account.get(
            account_alias,
            Decimal("0"),
        )
        if abs(residual_hkd) < Decimal("0.01"):
            continue
        adjustments.append(
            Position(
                statement_id=statement_id,
                broker="tiger",
                account_alias=account_alias,
                market=Market.CASH,
                asset_class=AssetClass.CASH,
                symbol=TIGER_UNMAPPED_ASSETS_SYMBOL,
                name="老虎未明细账户资产",
                currency="HKD",
                quantity=Decimal("1"),
                cost_price=residual_hkd,
                last_price=residual_hkd,
                market_value=residual_hkd,
                cost_value=residual_hkd,
                unrealized_pnl=Decimal("0"),
                confidence="high",
                notes=(
                    "Tiger account_total reconciliation for locked funds "
                    "or fund assets not returned as positions"
                ),
            )
        )
    return adjustments


def build_tiger_account_candidate(
    snapshot: TigerAccountSnapshot,
    *,
    run_date: str,
    data_as_of: str,
) -> BrokerAccountCandidate:
    if not snapshot.accounts:
        raise TigerAccountError(
            "no active Tiger accounts matched snapshot",
            error_type="no_matching_accounts",
        )
    aliases_by_identity: dict[str, str] = {}
    for account in snapshot.accounts:
        account_id = str(account.account)
        safe_alias = _account_alias(account_id)
        for identity in (account_id, safe_alias):
            if identity in aliases_by_identity:
                raise TigerAccountError(
                    "Tiger snapshot has ambiguous account aliases",
                    error_type="account_query_failed",
                )
            aliases_by_identity[identity] = safe_alias

    def normalize_record(record: dict[str, object]) -> dict[str, object]:
        account_id = _text(record, "account")
        account_alias = _text(record, "account_alias")
        safe_alias = aliases_by_identity.get(account_id or account_alias)
        if safe_alias is None or (
            account_id
            and account_alias
            and aliases_by_identity.get(account_alias) != safe_alias
        ):
            raise TigerAccountError(
                "Tiger snapshot has an unrecognized account alias",
                error_type="account_query_failed",
            )
        return {**record, "account_alias": safe_alias}

    normalized_snapshot = TigerAccountSnapshot(
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
        raise TigerAccountError(
            "; ".join(blocking_errors),
            error_type="blocking_data_error",
        )
    if any(
        _text(record, "record_type") == "account_total"
        and (account_total := _get_attr(record, "account_total", None)) not in (None, "")
        and _optional_decimal(record, ("account_total",)) is None
        for record in normalized_snapshot.cash_records
    ):
        raise TigerAccountError(
            "Tiger account_total is invalid",
            error_type="blocking_data_error",
        )

    fx_to_hkd = _snapshot_fx_to_hkd(normalized_snapshot)
    required_fx = {
        (item.account_alias, item.currency.upper())
        for item in [*positions, *cash_balances]
    }
    required_fx.update(
        (
            _text(record, "account_alias", "tiger_unknown"),
            _text(record, "currency", "USD").upper() or "USD",
        )
        for record in normalized_snapshot.cash_records
        if _text(record, "record_type") == "account_total"
        and _optional_decimal(record, ("account_total",)) is not None
    )
    missing_fx = required_fx - fx_to_hkd.keys()
    if missing_fx:
        account_alias, currency = sorted(missing_fx)[0]
        raise TigerAccountError(
            f"missing live FX rate for {account_alias} {currency}",
            error_type="fx_rate_missing",
        )

    positions = [
        *positions,
        *_unmapped_total_asset_positions(
            snapshot=normalized_snapshot,
            positions=positions,
            cash_balances=cash_balances,
            fx_to_hkd=fx_to_hkd,
            run_date=run_date,
        ),
    ]
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
        raise TigerAccountError(
            "duplicate broker snapshot identity",
            error_type="duplicate_identity",
        )

    return BrokerAccountCandidate(
        broker="tiger",
        source_kind="live",
        data_as_of=data_as_of,
        period=run_date[:7],
        positions=tuple(positions),
        cash=tuple(cash_balances),
        fx_rates=tuple(
            {
                "account_alias": account_alias,
                "currency": currency,
                "rate_to_hkd": format(rate, "f"),
            }
            for (account_alias, currency), rate in sorted(fx_to_hkd.items())
        ),
        summary={
            "account_count": len(snapshot.accounts),
            "position_count": len(positions),
            "cash_count": len(cash_balances),
            "account_aliases": sorted(
                {item.account_alias for item in [*positions, *cash_balances]}
            ),
        },
    )


def _snapshot_fx_to_hkd(
    snapshot: TigerAccountSnapshot,
) -> dict[tuple[str, str], Decimal]:
    rates: dict[tuple[str, str], Decimal] = {}
    for record in snapshot.cash_records:
        rate = _optional_decimal(record, ("fx_to_hkd",))
        currency = _text(record, "currency").upper()
        account_alias = _text(record, "account_alias")
        if rate is not None and rate > 0 and currency and account_alias:
            rates[(account_alias, currency)] = rate
    return rates


def _required_decimal(
    record: dict[str, object],
    keys: tuple[str, ...],
) -> tuple[Decimal, bool, object | None]:
    raw_value: object | None = None
    for key in keys:
        value = record.get(key)
        if _is_blank_scalar(value):
            continue
        raw_value = value
        break
    if raw_value is None:
        return Decimal("0"), False, None
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0"), False, raw_value
    if not value.is_finite():
        return Decimal("0"), False, raw_value
    return value, True, raw_value


def _optional_decimal(
    record: dict[str, object],
    keys: tuple[str, ...],
) -> Decimal | None:
    for key in keys:
        raw_value = record.get(key)
        if _is_blank_scalar(raw_value):
            continue
        try:
            value = Decimal(str(raw_value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None
        return value if value.is_finite() else None
    return None


def _is_blank_scalar(value: object) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _market_from_record(record: dict[str, object]) -> Market:
    raw_market = _text(record, "market", "").upper()
    if raw_market == "US":
        return Market.US
    if raw_market == "HK":
        return Market.HK
    if raw_market:
        return Market.OTHER

    currency = _text(record, "currency", "").upper()
    if currency == "USD":
        return Market.US
    if currency == "HKD":
        return Market.HK

    symbol = _text(record, "symbol", "").upper()
    if symbol.endswith(".HK") or symbol.startswith("HK."):
        return Market.HK
    if symbol.isdigit() and 4 <= len(symbol) <= 5:
        return Market.HK
    return Market.OTHER


def _currency_from_record(record: dict[str, object], market: Market) -> str:
    currency = _text(record, "currency").upper()
    if currency and currency != "N/A":
        return currency
    if market == Market.HK:
        return "HKD"
    if market == Market.US:
        return "USD"
    return currency


def _asset_class_from_record(record: dict[str, object]) -> AssetClass:
    raw_type = _text(record, "sec_type", "").upper()
    if raw_type in {"STK", "STOCK", "EQUITY", "COMMON_STOCK"}:
        return AssetClass.STOCK
    if raw_type == "FUND":
        detected = detect_asset_class(_text(record, "symbol"), _text(record, "name"))
        return (
            detected
            if detected == AssetClass.MONEY_MARKET_FUND
            else AssetClass.FUND
        )
    if raw_type in {"ETF", "EXCHANGE_TRADED_FUND"}:
        return AssetClass.ETF
    return AssetClass.UNKNOWN
