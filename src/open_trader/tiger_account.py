from __future__ import annotations

import csv
import inspect
import os
from dataclasses import dataclass, field
import json
from decimal import Decimal, InvalidOperation
import uuid
from pathlib import Path
from typing import Callable, Iterable

from .csv_io import write_rows
from .fx import DEFAULT_RATES_TO_HKD, StaticMonthEndFxProvider
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows, pct
from .models import AssetClass, CashBalance, Market, Position


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

TIGER_UNMAPPED_ASSETS_SYMBOL = "TIGER_UNMAPPED_ASSETS"


class TigerAccountError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        sync_result: TigerPortfolioSyncResult | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.sync_result = sync_result


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


@dataclass(frozen=True)
class TigerPortfolioSyncResult:
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


def sync_tiger_portfolio(
    *,
    snapshot: TigerAccountSnapshot,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    update_latest: bool,
) -> TigerPortfolioSyncResult:
    existing_rows = _read_portfolio_rows(portfolio_path)
    preserved_positions, preserved_cash = _latest_non_tiger_detail_inputs(
        data_dir,
        run_date,
    )
    use_detail_rows = bool(preserved_positions or preserved_cash)
    fx_rows = (
        existing_rows
        if use_detail_rows
        else _fallback_fx_source_rows(existing_rows)
    )
    fx_provider = _fx_provider_from_existing_rows(run_date, fx_rows)
    if not use_detail_rows:
        preserved_rows = existing_rows
    else:
        preserved_rows = []
    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date=run_date,
    )
    positions = [
        *positions,
        *_unmapped_total_asset_positions(
            snapshot=snapshot,
            positions=positions,
            cash_balances=cash_balances,
            fx_provider=fx_provider,
            run_date=run_date,
        ),
    ]
    if use_detail_rows:
        _raise_for_unsupported_detail_tiger_collisions(
            preserved_positions,
            preserved_cash,
            positions,
            cash_balances,
        )
        merged_rows = build_portfolio_rows(
            run_date[:7],
            [*preserved_positions, *positions],
            [*preserved_cash, *cash_balances],
            fx_provider,
        )
    else:
        (
            preserved_portfolio_rows,
            preserved_portfolio_positions,
            preserved_portfolio_cash,
            preserved_has_invalid_market_value,
            preserved_safety_rows,
        ) = _portfolio_inputs_from_preserved_rows(
            preserved_rows,
            positions,
            cash_balances,
        )
        merged_rows = build_portfolio_rows(
            run_date[:7],
            [*preserved_portfolio_positions, *positions],
            [*preserved_portfolio_cash, *cash_balances],
            fx_provider,
        )
        _apply_preserved_safety_metadata(merged_rows, preserved_safety_rows)
        merged_rows = _recalculate_combined_portfolio_rows(
            [*preserved_portfolio_rows, *merged_rows]
        )
        if preserved_has_invalid_market_value:
            _mark_all_rows_data_check(merged_rows)

    run_dir = data_dir / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = reports_dir / "tiger_account"
    report_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = run_dir / "tiger_account_snapshot.json"
    merged_portfolio_path = run_dir / "portfolio.csv"
    extracted_positions_path = run_dir / "extracted_positions.csv"
    extracted_cash_path = run_dir / "extracted_cash.csv"
    report_path = report_dir / f"{run_date}.md"
    latest_path = data_dir / "latest" / "portfolio.csv"
    updated_latest = False

    _write_text_file_atomic(
        snapshot_path,
        json.dumps(_snapshot_to_json(snapshot), ensure_ascii=False, indent=2),
    )
    if use_detail_rows:
        write_rows(
            extracted_positions_path,
            POSITION_DETAIL_FIELDNAMES,
            (
                _position_to_detail_row(position)
                for position in [*preserved_positions, *positions]
            ),
        )
        write_rows(
            extracted_cash_path,
            CASH_DETAIL_FIELDNAMES,
            (_cash_to_detail_row(cash) for cash in [*preserved_cash, *cash_balances]),
        )
    _write_portfolio_rows_atomic(merged_portfolio_path, merged_rows)
    _write_text_file_atomic(
        report_path,
        _render_tiger_account_report(
            account_count=len(snapshot.accounts),
            position_count=len(positions),
            cash_count=len(cash_balances),
            blocking_errors=blocking_errors,
            updated_latest=False,
        ),
    )

    blocking_result = TigerPortfolioSyncResult(
        run_date=run_date,
        account_count=len(snapshot.accounts),
        position_count=len(positions),
        cash_count=len(cash_balances),
        merged_row_count=len(merged_rows),
        snapshot_path=snapshot_path,
        portfolio_path=merged_portfolio_path,
        report_path=report_path,
        latest_path=latest_path,
        updated_latest=False,
    )

    if blocking_errors:
        raise TigerAccountError(
            "; ".join(blocking_errors),
            error_type="blocking_data_error",
            sync_result=blocking_result,
        )

    if update_latest:
        latest_backup_path: Path | None = None
        latest_existed = latest_path.exists()
        if latest_existed:
            latest_backup_path = _atomic_temp_path(latest_path)
            latest_backup_path.write_bytes(latest_path.read_bytes())

        try:
            _write_latest_portfolio_atomic(latest_path, merged_rows)
            _write_text_file_atomic(
                report_path,
                _render_tiger_account_report(
                    account_count=len(snapshot.accounts),
                    position_count=len(positions),
                    cash_count=len(cash_balances),
                    blocking_errors=blocking_errors,
                    updated_latest=True,
                ),
            )
            updated_latest = True
        except Exception:
            if latest_existed:
                assert latest_backup_path is not None
                latest_backup_path.replace(latest_path)
            else:
                if latest_path.exists():
                    latest_path.unlink()
            raise
        finally:
            if latest_backup_path is not None and latest_backup_path.exists():
                latest_backup_path.unlink()

    return TigerPortfolioSyncResult(
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
            positions = list(self.trade_client.get_positions(account=account.account))
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
        segments = _get_attr(payload, "segments", {})
        if not isinstance(segments, dict):
            return records
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
            return records

        account_total = _first_present_value(
            segment,
            "equity_with_loan",
            "net_liquidation",
        )
        if account_total is not None:
            records.append(
                {
                    "record_type": "account_total",
                    "account": account.account,
                    "account_alias": account.account_alias,
                    "currency": _text(segment, "currency"),
                    "account_total": account_total,
                    "segment_category": _text(segment, "category"),
                    "net_liquidation": _text(segment, "net_liquidation"),
                    "equity_with_loan": _text(segment, "equity_with_loan"),
                    "locked_funds": _text(segment, "locked_funds"),
                    "uncollected": _text(segment, "uncollected"),
                    "source": account.asset_method,
                }
            )

        currency_assets = _get_attr(segment, "currency_assets", {})
        if not isinstance(currency_assets, dict):
            return records
        for currency_asset in currency_assets.values():
            if not self._has_non_zero_balance(currency_asset):
                continue
            records.append(
                {
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
            )
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

        for payload_account in payload_accounts:
            if _text(payload_account, "account") != account.account:
                continue
            market_values = _get_attr(payload_account, "market_values", {})
            if not isinstance(market_values, dict):
                continue
            for market_value in market_values.values():
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
        return records


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
    return Position(
        statement_id=statement_id,
        broker="tiger",
        account_alias=_text(record, "account_alias", "tiger_unknown"),
        market=_market_from_record(record),
        asset_class=_asset_class_from_record(record),
        symbol=symbol,
        name=_text(record, "name", symbol),
        currency=_text(record, "currency").upper(),
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
        total_assets_hkd = (
            account_total * fx_provider.get_rate_to_hkd(total_currency).rate
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


def _asset_class_from_record(record: dict[str, object]) -> AssetClass:
    raw_type = _text(record, "sec_type", "").upper()
    if raw_type in {"STK", "STOCK", "EQUITY", "COMMON_STOCK"}:
        return AssetClass.STOCK
    if raw_type in {"ETF", "EXCHANGE_TRADED_FUND"}:
        return AssetClass.ETF
    return AssetClass.UNKNOWN


def _read_portfolio_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _latest_non_tiger_detail_inputs(
    data_dir: Path,
    run_date: str,
) -> tuple[list[Position], list[CashBalance]]:
    detail_dir = _detail_dir_for_tiger_sync(data_dir, run_date)
    if detail_dir is None:
        return [], []
    positions = [
        _position_from_detail_row(row)
        for row in _read_csv_rows(detail_dir / "extracted_positions.csv")
        if row.get("broker", "").strip().lower() != "tiger"
    ]
    cash_balances = [
        _cash_from_detail_row(row)
        for row in _read_csv_rows(detail_dir / "extracted_cash.csv")
        if row.get("broker", "").strip().lower() != "tiger"
    ]
    return positions, cash_balances


def _detail_dir_for_tiger_sync(data_dir: Path, run_date: str) -> Path | None:
    exact_dir = data_dir / "runs" / run_date
    if (exact_dir / "extracted_positions.csv").is_file():
        return exact_dir
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    detail_dirs = [
        path
        for path in runs_dir.iterdir()
        if path.is_dir() and (path / "extracted_positions.csv").is_file()
    ]
    return max(detail_dirs, key=lambda path: path.name) if detail_dirs else None


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _position_from_detail_row(row: dict[str, str]) -> Position:
    quantity, quantity_ok, _ = _required_decimal(row, ("quantity",))
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
        confidence=_confidence(row.get("confidence", ""), quantity_ok),
        notes=row.get("notes", ""),
    )


def _cash_from_detail_row(row: dict[str, str]) -> CashBalance:
    cash_balance, cash_ok, _ = _required_decimal(row, ("cash_balance",))
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


def _raise_for_unsupported_detail_tiger_collisions(
    preserved_positions: list[Position],
    preserved_cash: list[CashBalance],
    tiger_positions: list[Position],
    tiger_cash_balances: list[CashBalance],
) -> None:
    for position in preserved_positions:
        _raise_for_unsupported_preserved_mixed_brokers(
            symbol=position.symbol,
            broker_parts=_broker_parts_from_text(position.broker),
            allow_futu_tiger_split=False,
        )
    for cash in preserved_cash:
        _raise_for_unsupported_preserved_mixed_brokers(
            symbol=cash.symbol,
            broker_parts=_broker_parts_from_text(cash.broker),
            allow_futu_tiger_split=False,
        )

    tiger_position_keys = {
        _position_portfolio_key(position) for position in tiger_positions
    }
    preserved_position_brokers: dict[tuple[Market, str, str], set[str]] = {}
    for position in preserved_positions:
        key = _position_portfolio_key(position)
        if key not in tiger_position_keys:
            continue
        preserved_position_brokers.setdefault(key, set()).update(
            _broker_parts_from_text(position.broker)
        )
    for key, broker_parts in preserved_position_brokers.items():
        if broker_parts != {"futu"}:
            _raise_mixed_tiger_broker_row(
                {
                    "symbol": key[1],
                    "brokers": ";".join(sorted({*broker_parts, "tiger"})),
                }
            )

    tiger_cash_keys = {_cash_portfolio_key(cash) for cash in tiger_cash_balances}
    preserved_cash_brokers: dict[tuple[str, str], set[str]] = {}
    for cash in preserved_cash:
        key = _cash_portfolio_key(cash)
        if key not in tiger_cash_keys:
            continue
        preserved_cash_brokers.setdefault(key, set()).update(
            _broker_parts_from_text(cash.broker)
        )
    for key, broker_parts in preserved_cash_brokers.items():
        if broker_parts != {"futu"}:
            _raise_mixed_tiger_broker_row(
                {
                    "symbol": key[0],
                    "brokers": ";".join(sorted({*broker_parts, "tiger"})),
                }
            )


def _portfolio_inputs_from_preserved_rows(
    rows: list[dict[str, str]],
    tiger_positions: list[Position],
    tiger_cash_balances: list[CashBalance],
) -> tuple[
    list[dict[str, str]],
    list[Position],
    list[CashBalance],
    bool,
    list[dict[str, str]],
]:
    preserved_rows: list[dict[str, str]] = []
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    preserved_safety_rows: list[dict[str, str]] = []
    has_invalid_market_value = False
    tiger_positions_by_key = _tiger_positions_by_portfolio_key(tiger_positions)
    tiger_cash_by_key = _tiger_cash_by_portfolio_key(tiger_cash_balances)
    for row in rows:
        broker_parts = _broker_parts(row)
        _raise_for_unsupported_preserved_mixed_brokers(
            symbol=row.get("symbol", ""),
            broker_parts=broker_parts,
            allow_futu_tiger_split=True,
        )
        has_tiger = "tiger" in broker_parts
        has_other_brokers = bool(broker_parts - {"tiger"})
        market = _market_from_text(row.get("market", ""))
        asset_class = _asset_class_from_text(row.get("asset_class", ""))
        is_cash_row = (
            market == Market.CASH
            and asset_class == AssetClass.CASH
            and _is_currency_cash_portfolio_row(row)
        )
        if not has_tiger:
            if is_cash_row:
                key = _cash_portfolio_key_from_row(row)
                if key not in tiger_cash_by_key:
                    has_invalid_market_value = (
                        has_invalid_market_value
                        or _portfolio_row_has_invalid_market_value(row)
                    )
                    cash_balances.append(_cash_from_portfolio_row(row))
                    preserved_safety_rows.append(row)
                    continue
                if broker_parts != {"futu"}:
                    _raise_mixed_tiger_broker_row(
                        _mixed_tiger_row_for_key(row, broker_parts)
                    )
                has_invalid_market_value = (
                    has_invalid_market_value
                    or _portfolio_row_has_invalid_market_value(row)
                )
                cash_balances.append(_cash_from_portfolio_row(row))
            else:
                if market == Market.CASH and asset_class == AssetClass.CASH:
                    preserved_rows.append(row)
                    continue
                key = _position_portfolio_key_from_row(row)
                if key not in tiger_positions_by_key:
                    has_invalid_market_value = (
                        has_invalid_market_value
                        or _portfolio_row_has_invalid_market_value(row)
                    )
                    positions.append(_position_from_portfolio_row(row))
                    preserved_safety_rows.append(row)
                    continue
                if broker_parts != {"futu"}:
                    _raise_mixed_tiger_broker_row(
                        _mixed_tiger_row_for_key(row, broker_parts)
                    )
                has_invalid_market_value = (
                    has_invalid_market_value
                    or _portfolio_row_has_invalid_market_value(row)
                )
                positions.append(_position_from_portfolio_row(row))
            continue
        if not has_other_brokers:
            continue
        if broker_parts != {"futu", "tiger"}:
            _raise_mixed_tiger_broker_row(row)

        if is_cash_row:
            key = _cash_portfolio_key_from_row(row)
            tiger_cash_balance = tiger_cash_by_key.get(key)
            if tiger_cash_balance is None:
                _raise_mixed_tiger_broker_row(row)
            has_invalid_market_value = (
                has_invalid_market_value
                or _portfolio_row_has_invalid_market_value(row)
            )
            cash_balances.append(
                _non_tiger_cash_residual_from_portfolio_row(
                    row,
                    tiger_cash_balance,
                )
            )
            continue

        key = _position_portfolio_key_from_row(row)
        tiger_position = tiger_positions_by_key.get(key)
        if tiger_position is None:
            _raise_mixed_tiger_broker_row(row)
        has_invalid_market_value = (
            has_invalid_market_value
            or _portfolio_row_has_invalid_market_value(row)
        )
        residual_position, residual_has_invalid_market_value = (
            _non_tiger_position_residual_from_portfolio_row(row, tiger_position)
        )
        positions.append(residual_position)
        has_invalid_market_value = (
            has_invalid_market_value or residual_has_invalid_market_value
        )
    return (
        preserved_rows,
        positions,
        cash_balances,
        has_invalid_market_value,
        preserved_safety_rows,
    )


def _apply_preserved_safety_metadata(
    rows: list[dict[str, str]],
    preserved_safety_rows: list[dict[str, str]],
) -> None:
    safety_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in preserved_safety_rows:
        safety_by_key.setdefault(_portfolio_row_output_key(row), []).append(row)

    for row in rows:
        source_rows = safety_by_key.get(_portfolio_row_output_key(row))
        if not source_rows:
            continue
        if any(
            source.get("risk_flag", "").strip() == "data_check"
            for source in source_rows
        ):
            row["risk_flag"] = "data_check"
        if any(
            source.get("ai_eligible", "").strip().lower() == "false"
            for source in source_rows
        ):
            row["ai_eligible"] = "false"
            row["analysis_symbol"] = ""


def _portfolio_row_output_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        row.get("market", "").strip().upper(),
        row.get("symbol", "").strip().upper(),
        row.get("currency", "").strip().upper(),
    )


def _portfolio_row_has_invalid_market_value(row: dict[str, str]) -> bool:
    return (
        _parse_finite_decimal(row.get("market_value_hkd", "").strip()) is None
        or _parse_finite_decimal(row.get("market_value", "").strip()) is None
    )


def _is_currency_cash_portfolio_row(row: dict[str, str]) -> bool:
    currency = row.get("currency", "").strip().upper()
    symbol = row.get("symbol", "").strip().upper()
    return bool(currency) and symbol == f"{currency}_CASH"


def _mixed_tiger_row_for_key(
    row: dict[str, str],
    broker_parts: set[str],
) -> dict[str, str]:
    return {
        "symbol": row.get("symbol", ""),
        "brokers": ";".join(sorted({*broker_parts, "tiger"})),
    }


def _raise_for_unsupported_preserved_mixed_brokers(
    *,
    symbol: str,
    broker_parts: set[str],
    allow_futu_tiger_split: bool,
) -> None:
    if len(broker_parts) <= 1:
        return
    if allow_futu_tiger_split and broker_parts == {"futu", "tiger"}:
        return
    _raise_mixed_tiger_broker_row(
        {
            "symbol": symbol,
            "brokers": ";".join(sorted(broker_parts)),
        }
    )


def _position_portfolio_key_from_row(row: dict[str, str]) -> tuple[Market, str, str]:
    return (
        _market_from_text(row.get("market", "")),
        row.get("symbol", "").strip().upper(),
        row.get("currency", "").strip().upper(),
    )


def _position_portfolio_key(position: Position) -> tuple[Market, str, str]:
    return (
        position.market,
        position.symbol.strip().upper(),
        position.currency.strip().upper(),
    )


def _cash_portfolio_key_from_row(row: dict[str, str]) -> tuple[str, str]:
    return (
        row.get("symbol", "").strip().upper(),
        row.get("currency", "").strip().upper(),
    )


def _cash_portfolio_key(cash_balance: CashBalance) -> tuple[str, str]:
    return (
        cash_balance.symbol.strip().upper(),
        cash_balance.currency.strip().upper(),
    )


def _tiger_positions_by_portfolio_key(
    positions: list[Position],
) -> dict[tuple[Market, str, str], Position]:
    grouped: dict[tuple[Market, str, str], list[Position]] = {}
    for position in positions:
        grouped.setdefault(_position_portfolio_key(position), []).append(position)
    return {
        key: _combined_tiger_position_for_key(key, group)
        for key, group in grouped.items()
    }


def _combined_tiger_position_for_key(
    key: tuple[Market, str, str],
    group: list[Position],
) -> Position:
    market, symbol, currency = key
    quantity = sum((position.quantity for position in group), Decimal("0"))
    market_value = _sum_optional_decimals(position.market_value for position in group)
    cost_value = _sum_optional_decimals(position.cost_value for position in group)
    unrealized_pnl = _sum_optional_decimals(
        position.unrealized_pnl for position in group
    )
    return Position(
        statement_id="tiger-live-aggregate",
        broker="tiger",
        account_alias=";".join(sorted({position.account_alias for position in group})),
        market=market,
        asset_class=max(
            (position.asset_class for position in group),
            key=lambda asset_class: 0 if asset_class == AssetClass.UNKNOWN else 1,
        ),
        symbol=symbol,
        name=max((position.name for position in group), key=len),
        currency=currency,
        quantity=quantity,
        cost_price=None,
        last_price=None,
        market_value=market_value,
        cost_value=cost_value,
        unrealized_pnl=unrealized_pnl,
        confidence=_merged_input_confidence(position.confidence for position in group),
        notes="",
    )


def _tiger_cash_by_portfolio_key(
    cash_balances: list[CashBalance],
) -> dict[tuple[str, str], CashBalance]:
    grouped: dict[tuple[str, str], list[CashBalance]] = {}
    for cash_balance in cash_balances:
        grouped.setdefault(_cash_portfolio_key(cash_balance), []).append(cash_balance)
    return {
        key: CashBalance(
            statement_id="tiger-live-aggregate",
            broker="tiger",
            account_alias=";".join(sorted({cash.account_alias for cash in group})),
            currency=key[1],
            cash_balance=sum((cash.cash_balance for cash in group), Decimal("0")),
            available_balance=_sum_optional_decimals(
                cash.available_balance for cash in group
            ),
            confidence=_merged_input_confidence(cash.confidence for cash in group),
            notes="",
        )
        for key, group in grouped.items()
    }


def _sum_optional_decimals(values: Iterable[Decimal | None]) -> Decimal | None:
    items = list(values)
    if any(value is None for value in items):
        return None
    return sum((value for value in items if value is not None), Decimal("0"))


def _merged_input_confidence(values: Iterable[str]) -> str:
    confidence_values = set(values)
    if "low" in confidence_values:
        return "low"
    if "medium" in confidence_values:
        return "medium"
    return "high"


def _position_from_portfolio_row(row: dict[str, str]) -> Position:
    quantity, quantity_ok, _ = _required_decimal(row, ("total_quantity",))
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


def _non_tiger_position_residual_from_portfolio_row(
    row: dict[str, str],
    tiger_position: Position,
) -> tuple[Position, bool]:
    position = _position_from_portfolio_row(row)
    quantity, quantity_ok, _ = _required_decimal(row, ("total_quantity",))
    market_value, market_value_ok = _market_value_from_portfolio_row(row)
    cost_value = _optional_decimal(row, ("cost_value",))
    unrealized_pnl = _optional_decimal(row, ("unrealized_pnl",))
    residual_market_value = _subtract_optional_decimal(
        market_value,
        tiger_position.market_value,
    )
    residual_cost_value = _subtract_optional_decimal(
        cost_value,
        tiger_position.cost_value,
    )
    residual_unrealized_pnl = _subtract_optional_decimal(
        unrealized_pnl,
        tiger_position.unrealized_pnl,
    )
    residuals_are_valid = (
        quantity_ok
        and market_value_ok
        and residual_market_value is not None
        and residual_cost_value is not None
        and residual_market_value >= 0
        and residual_cost_value >= 0
        and quantity - tiger_position.quantity >= 0
    )
    if not residuals_are_valid:
        _raise_mixed_tiger_broker_row(row)
    return (
        Position(
            statement_id=position.statement_id,
            broker=_non_tiger_brokers_text(row),
            account_alias=_non_tiger_accounts_text(row),
            market=position.market,
            asset_class=position.asset_class,
            symbol=position.symbol,
            name=position.name,
            currency=position.currency,
            quantity=quantity - tiger_position.quantity,
            cost_price=position.cost_price,
            last_price=position.last_price,
            market_value=residual_market_value,
            cost_value=residual_cost_value,
            unrealized_pnl=residual_unrealized_pnl,
            confidence=_confidence(row.get("confidence", ""), residuals_are_valid),
            notes=_non_tiger_notes_text(row),
        ),
        not market_value_ok,
    )


def _non_tiger_cash_residual_from_portfolio_row(
    row: dict[str, str],
    tiger_cash_balance: CashBalance,
) -> CashBalance:
    cash_balance = _cash_from_portfolio_row(row)
    residual_cash_balance = cash_balance.cash_balance - tiger_cash_balance.cash_balance
    if residual_cash_balance < 0:
        _raise_mixed_tiger_broker_row(row)
    return CashBalance(
        statement_id=cash_balance.statement_id,
        broker=_non_tiger_brokers_text(row),
        account_alias=_non_tiger_accounts_text(row),
        currency=cash_balance.currency,
        cash_balance=residual_cash_balance,
        available_balance=None,
        confidence=cash_balance.confidence,
        notes=_non_tiger_notes_text(row),
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


def _subtract_optional_decimal(
    total: Decimal | None,
    value: Decimal | None,
) -> Decimal | None:
    if total is None or value is None:
        return None
    return total - value


def _non_tiger_brokers_text(row: dict[str, str]) -> str:
    return ";".join(sorted(_broker_parts(row) - {"tiger"}))


def _non_tiger_accounts_text(row: dict[str, str]) -> str:
    accounts = [
        part.strip()
        for chunk in row.get("accounts", "").split(",")
        for part in chunk.split(";")
        if part.strip() and "tiger" not in part.strip().lower()
    ]
    return ";".join(sorted(accounts))


def _non_tiger_notes_text(row: dict[str, str]) -> str:
    notes = [
        part.strip()
        for part in row.get("notes", "").split(";")
        if part.strip() and "tiger" not in part.strip().lower()
    ]
    return "; ".join(notes)


def _mark_all_rows_data_check(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row["portfolio_weight_hkd"] = ""
        row["risk_flag"] = "data_check"


def _position_to_detail_row(position: Position) -> dict[str, str]:
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


def _cash_to_detail_row(cash: CashBalance) -> dict[str, str]:
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


def _market_from_text(value: str) -> Market:
    normalized = str(value or "").strip().upper()
    if normalized == "US":
        return Market.US
    if normalized == "HK":
        return Market.HK
    if normalized == "CASH":
        return Market.CASH
    return Market.OTHER


def _asset_class_from_text(value: str) -> AssetClass:
    normalized = str(value or "").strip().lower()
    for asset_class in AssetClass:
        if asset_class.value == normalized:
            return asset_class
    return AssetClass.UNKNOWN


def _confidence(value: str, required_fields_ok: bool) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"high", "medium", "low"} and required_fields_ok:
        return normalized
    return "high" if required_fields_ok else "low"


def _decimal_to_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _atomic_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _write_text_file_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    temp_path = _atomic_temp_path(path)
    try:
        temp_path.write_text(text, encoding=encoding)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_portfolio_rows_atomic(path: Path, rows: list[dict[str, str]]) -> None:
    temp_path = _atomic_temp_path(path)
    try:
        write_rows(temp_path, PORTFOLIO_FIELDNAMES, rows)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_bytes_to_path_atomic(source_path: Path, destination_path: Path) -> None:
    temp_path = _atomic_temp_path(destination_path)
    try:
        temp_path.write_bytes(source_path.read_bytes())
        temp_path.replace(destination_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_latest_portfolio_atomic(latest_path: Path, rows: list[dict[str, str]]) -> None:
    _write_portfolio_rows_atomic(latest_path, rows)


def _has_tiger_broker(row: dict[str, str]) -> bool:
    return "tiger" in _broker_parts(row)


def _broker_parts(row: dict[str, str]) -> set[str]:
    return _broker_parts_from_text(row.get("brokers", ""))


def _broker_parts_from_text(brokers: str) -> set[str]:
    return {
        part.strip().lower()
        for chunk in brokers.split(",")
        for part in chunk.split(";")
        if part.strip()
    }


def _raise_for_mixed_tiger_broker_rows(rows: list[dict[str, str]]) -> None:
    for row in rows:
        parts = _broker_parts(row)
        if "tiger" in parts and len(parts) > 1:
            _raise_mixed_tiger_broker_row(row)


def _raise_mixed_tiger_broker_row(row: dict[str, str]) -> None:
    symbol = row.get("symbol", "")
    brokers = row.get("brokers", "")
    raise TigerAccountError(
        f"portfolio row {symbol} mixes Tiger with other brokers: {brokers}",
        error_type="mixed_tiger_broker_row",
    )


def _fallback_fx_source_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if _broker_parts(row) != {"tiger"}]


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


def _recalculate_combined_portfolio_rows(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    normalized_rows = [
        {field: str(row.get(field, "")) for field in PORTFOLIO_FIELDNAMES}
        for row in rows
    ]
    parsed_market_values: list[Decimal | None] = []
    values: list[Decimal] = []
    has_missing_value = False
    for row in normalized_rows:
        value = _parse_finite_decimal(row.get("market_value_hkd", "").strip())
        parsed_market_values.append(value)
        if value is None:
            has_missing_value = True
            continue
        values.append(value)
    total = sum(values, Decimal("0"))
    for row, market_value_hkd in zip(normalized_rows, parsed_market_values):
        if has_missing_value:
            row["portfolio_weight_hkd"] = ""
            row["risk_flag"] = "data_check"
            continue
        market_value_hkd = market_value_hkd or Decimal("0")
        weight = market_value_hkd / total if total else Decimal("0")
        row["portfolio_weight_hkd"] = pct(weight)
        # Keep existing data_check markers as manual/data-review flags; recompute
        # only non-review risk states.
        if row["risk_flag"] == "data_check":
            continue
        if row["asset_class"] not in {"cash", "money_market_fund"} and weight > Decimal(
            "0.10"
        ):
            row["risk_flag"] = "overweight"
        else:
            row["risk_flag"] = "normal"
    return [
        row
        for row, _ in sorted(
            zip(normalized_rows, parsed_market_values),
            key=lambda item: (
                _safe_sort_group(item[0].get("sort_group", "")),
                -(item[1] or Decimal("0")),
            ),
        )
    ]


def _safe_sort_group(value: str, default_sort_group: int = 9) -> int:
    raw = value.strip()
    try:
        return int(raw) if raw else default_sort_group
    except ValueError:
        return default_sort_group


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


def _snapshot_to_json(snapshot: TigerAccountSnapshot) -> dict[str, object]:
    return {
        "accounts": [
            {
                "account": mask_account_id(account.account),
                "account_alias": account.account_alias,
                "account_type": account.account_type,
                "capability": account.capability,
                "status": account.status,
                "asset_method": account.asset_method,
            }
            for account in snapshot.accounts
        ],
        "cash_records": [
            _json_safe_record(_mask_snapshot_record(record)) for record in snapshot.cash_records
        ],
        "position_records": [
            _json_safe_record(_mask_snapshot_record(record))
            for record in snapshot.position_records
        ],
    }


def _mask_snapshot_record(record: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in record.items():
        if key == "account" and value is not None:
            output[key] = mask_account_id(value)
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


def _render_tiger_account_report(
    *,
    account_count: int,
    position_count: int,
    cash_count: int,
    blocking_errors: list[str],
    updated_latest: bool,
) -> str:
    latest_text = "已更新 latest" if updated_latest else "未更新 latest"
    lines = [
        "# 老虎账户同步",
        "",
        f"- 老虎账户：{account_count}",
        f"- 老虎持仓：{position_count}",
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
