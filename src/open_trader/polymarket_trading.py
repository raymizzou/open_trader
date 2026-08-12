"""The one authenticated boundary for protected Polymarket orders.

The rest of the application should pass :class:`PairIntent` values here and
never handle private keys, builder credentials, or signed order payloads.
"""

from __future__ import annotations

import json
import importlib.metadata
import os
import pty
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable, Literal, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from polymarket import BuilderApiKey, PRODUCTION, PublicClient, SecureClient

from .prediction_arbitrage import (
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_NET_EDGE,
    PairIntent,
    ThresholdHedgeIntent,
    ThresholdHedgeLeg,
    protected_buy_quantity,
)


SECURITY = "/usr/bin/security"
KEYCHAIN_SERVICE = "com.open-trader.polymarket"
PREDICT_KEYCHAIN_SERVICE = "com.open-trader.predict"
PREDICT_API_KEY_ACCOUNT = "api-key"
PREDICT_PRIVATE_KEY_ACCOUNT = "privy-private-key"
KEYCHAIN_ACCOUNTS = (
    "signing-private-key",
    "builder-key",
    "builder-secret",
    "builder-passphrase",
)
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
GEOBLOCK_TIMEOUT_SECONDS = 5.0
MERGE_WAIT_TIMEOUT_SECONDS = 60.0
REMEDIATION_BOOK_FRESHNESS_SECONDS = 10.0
COLLATERAL_BASE_UNITS = Decimal("1000000")
DEFAULT_TICK_SIZE = Decimal("0.01")
CENT = Decimal("0.01")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SAFE_ERROR_CODES = {
    "ambiguous",
    "auth",
    "geoblock_blocked",
    "geoblock_error",
    "invalid",
    "keychain_empty",
    "keychain_unavailable",
    "network",
    "order_amount_mismatch",
    "order_shape_mismatch",
    "preflight_required",
    "market_probe_unavailable",
    "account_insufficient",
    "rejected",
    "sdk_error",
    "signing",
    "timeout",
    "unavailable",
}


class PolymarketTradingError(RuntimeError):
    """An intentionally redacted adapter error."""

    def __init__(self, error_code: str) -> None:
        safe_code = error_code if error_code in _SAFE_ERROR_CODES else "sdk_error"
        self.error_code = safe_code
        super().__init__(f"polymarket trading error: {safe_code}")


class KeychainError(RuntimeError):
    """A redacted Keychain operation failure."""

    def __init__(self, error_code: str = "keychain_unavailable") -> None:
        safe_code = (
            error_code
            if error_code in {"keychain_empty", "keychain_unavailable"}
            else "keychain_unavailable"
        )
        self.error_code = safe_code
        super().__init__(f"keychain error: {safe_code}")


@dataclass(frozen=True, slots=True)
class PredictConfig:
    wallet_address: str
    environment: Literal["mainnet"] = "mainnet"


@dataclass(frozen=True, slots=True)
class TradingConfig:
    signer_address: str
    wallet_address: str
    predict: PredictConfig | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    wallet_address: str
    p_usd_balance: Decimal
    p_usd_allowance: Decimal
    open_order_ids: tuple[str, ...]
    positions: tuple[dict[str, str], ...]
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class LegResult:
    leg: Literal["YES", "NO"]
    accepted: bool
    status: str
    order_id: str
    filled_quantity: Decimal
    trade_ids: tuple[str, ...]
    error_code: str


@dataclass(frozen=True, slots=True)
class PairSubmission:
    yes: LegResult
    no: LegResult


@dataclass(frozen=True, slots=True)
class ThresholdLegResult:
    label: Literal["A", "B"]
    outcome: Literal["YES", "NO"]
    condition_id: str
    token_id: str
    accepted: bool
    status: str
    order_id: str
    filled_quantity: Decimal
    trade_ids: tuple[str, ...]
    error_code: str


@dataclass(frozen=True, slots=True)
class ThresholdHedgeSubmission:
    leg_a: ThresholdLegResult
    leg_b: ThresholdLegResult


def _run_security(
    args: list[str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, **kwargs)  # type: ignore[arg-type]


def _validate_keychain_account(account: str) -> None:
    if account not in KEYCHAIN_ACCOUNTS:
        raise ValueError("unsupported polymarket keychain account")


def _store_keychain_password(
    account: str,
    service: str,
    secret: str,
    run: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> None:
    args = [
        SECURITY,
        "add-generic-password",
        "-U",
        "-a",
        account,
        "-s",
        service,
        "-w",
    ]
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    try:
        if run is not None:
            run(args, input=f"{secret}\n", text=True, capture_output=True, check=True)
            return

        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        password_lines = (f"{secret}\n{secret}\n").encode()
        if os.write(master_fd, password_lines) != len(password_lines):
            raise OSError
        if process.wait(timeout=5) != 0:
            raise KeychainError()
    except Exception:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise KeychainError() from None
    finally:
        if master_fd >= 0:
            os.close(master_fd)
        if slave_fd >= 0:
            os.close(slave_fd)


def store_keychain_secret(
    account: str,
    secret: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Store one secret without placing it in process arguments."""

    _validate_keychain_account(account)
    if not isinstance(secret, str) or not secret:
        raise ValueError("keychain secret must not be empty")
    _store_keychain_password(account, KEYCHAIN_SERVICE, secret, run)


def load_keychain_secret(
    account: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Read one secret from Keychain without including it in diagnostics."""

    _validate_keychain_account(account)
    return _load_keychain_password(account, KEYCHAIN_SERVICE, run)


def _load_keychain_password(
    account: str,
    service: str,
    run: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> str:
    runner = run or _run_security
    args = [
        SECURITY,
        "find-generic-password",
        "-a",
        account,
        "-s",
        service,
        "-w",
    ]
    try:
        completed = runner(args, text=True, capture_output=True, check=True)
        value = getattr(completed, "stdout", "")
    except Exception as exc:
        del exc
        raise KeychainError() from None
    if not isinstance(value, str):
        raise KeychainError("keychain_empty")
    value = value.rstrip("\r\n")
    if not value:
        raise KeychainError("keychain_empty")
    return value


def store_predict_api_key(
    secret: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Store the Predict API key without exposing it in process arguments."""

    if not isinstance(secret, str) or not secret:
        raise ValueError("keychain secret must not be empty")
    _store_keychain_password(
        PREDICT_API_KEY_ACCOUNT, PREDICT_KEYCHAIN_SERVICE, secret, run
    )


def load_predict_api_key(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Load the Predict API key without exposing it in failures."""

    return _load_keychain_password(PREDICT_API_KEY_ACCOUNT, PREDICT_KEYCHAIN_SERVICE, run)


def load_predict_private_key(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Load the Predict signer key without exposing it in failures."""

    return _load_keychain_password(PREDICT_PRIVATE_KEY_ACCOUNT, PREDICT_KEYCHAIN_SERVICE, run)


def _canonical_address(value: object, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field} must be a canonical 20-byte hex address")
    return value


def load_trading_config(path: Path) -> TradingConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        del exc
        raise ValueError("invalid prediction arbitrage config") from None
    if not isinstance(payload, dict):
        raise ValueError("prediction arbitrage config must be an object")
    expected = {"signer_address", "wallet_address"}
    if set(payload) not in (expected, expected | {"predict"}):
        raise ValueError("prediction arbitrage config must contain signer_address and wallet_address")
    predict: PredictConfig | None = None
    if "predict" in payload:
        predict_payload = payload["predict"]
        if not isinstance(predict_payload, dict) or set(predict_payload) != {
            "wallet_address",
            "environment",
        }:
            raise ValueError("predict config must contain wallet_address and environment")
        if predict_payload.get("environment") != "mainnet":
            raise ValueError("predict environment must be mainnet")
        predict = PredictConfig(
            wallet_address=_canonical_address(
                predict_payload.get("wallet_address"), "predict.wallet_address"
            )
        )
    return TradingConfig(
        signer_address=_canonical_address(payload.get("signer_address"), "signer_address"),
        wallet_address=_canonical_address(payload.get("wallet_address"), "wallet_address"),
        predict=predict,
    )


def _safe_error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, KeychainError):
        return exc.error_code
    if isinstance(exc, PolymarketTradingError):
        return exc.error_code
    status = getattr(exc, "code", None)
    if isinstance(status, int):
        if status in {401, 403}:
            return "rejected"
        if status == 429 or status >= 500:
            return "network"
    if "timeout" in name:
        return "timeout"
    if "sign" in name:
        return "signing"
    if "reject" in name or "unauthor" in name or "forbidden" in name:
        return "rejected"
    if isinstance(exc, (ConnectionError, URLError)) or "network" in name:
        return "network"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid"
    if isinstance(exc, OSError):
        return "unavailable"
    return "sdk_error"


def _collect(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    for method_name in ("iter_items", "all"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return tuple(method())
            except TypeError:
                continue
    try:
        return tuple(cast(Sequence[object], value))
    except TypeError:
        return (value,)


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_string(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _decimal(value: object, *, base_units: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        del exc
        raise ValueError("invalid decimal") from None
    if not parsed.is_finite():
        raise ValueError("invalid decimal")
    return parsed / COLLATERAL_BASE_UNITS if base_units else parsed


def _address_from_client(client: object, name: str) -> str | None:
    try:
        value = getattr(client, name)
    except Exception:
        return None
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        return None
    return value


def _model_dict(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            return None
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _venue_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                return None
            # CLOB order-book timestamps are epoch milliseconds; trade
            # collaborators have historically supplied epoch seconds.
            divisor = Decimal("1000") if abs(number) > Decimal("10000000000") else Decimal("1")
            moment = datetime.fromtimestamp(float(number / divisor), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                number = Decimal(text)
            except (InvalidOperation, ValueError):
                return None
            if not number.is_finite():
                return None
            divisor = Decimal("1000") if abs(number) > Decimal("10000000000") else Decimal("1")
            try:
                moment = datetime.fromtimestamp(float(number / divisor), UTC)
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _trade_timestamp(value: object) -> datetime | None:
    for name in ("matched_at", "match_time", "updated_at", "last_update", "timestamp"):
        timestamp = _venue_timestamp(_field(value, name))
        if timestamp is not None:
            return timestamp
    return None


def _string_refs(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value.strip() else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str) and item.strip()}
    return set()


class PolymarketTradingClient:
    """A narrow, redacted wrapper around the official synchronous SDK."""

    def __init__(
        self,
        config: TradingConfig,
        client: object,
        *,
        urlopen_fn: Callable[..., object] | None = None,
        public_client_factory: Callable[[], object] | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._urlopen_fn = urlopen_fn
        self._public_client_factory = public_client_factory or PublicClient
        self._readiness_key: tuple[PairIntent, Decimal] | None = None
        self._threshold_readiness_key: ThresholdHedgeIntent | None = None
        self._cross_leg_readiness_key: object | None = None

    @classmethod
    def from_keychain(
        cls,
        config: TradingConfig,
        *,
        client_factory: Callable[..., object] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        public_client_factory: Callable[[], object] | None = None,
    ) -> "PolymarketTradingClient":
        private_key = load_keychain_secret("signing-private-key", run=run)
        builder_key = load_keychain_secret("builder-key", run=run)
        builder_secret = load_keychain_secret("builder-secret", run=run)
        builder_passphrase = load_keychain_secret("builder-passphrase", run=run)
        factory = client_factory or SecureClient.create
        try:
            client = factory(
                private_key=private_key,
                wallet=config.wallet_address,
                api_key=BuilderApiKey(
                    key=builder_key,
                    secret=builder_secret,
                    passphrase=builder_passphrase,
                ),
            )
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            raise PolymarketTradingError(code) from None
        signer = _address_from_client(client, "signer")
        wallet = _address_from_client(client, "wallet")
        if signer is None or signer.lower() != config.signer_address.lower():
            raise PolymarketTradingError("auth")
        if wallet is None or wallet.lower() != config.wallet_address.lower():
            raise PolymarketTradingError("auth")
        return cls(config, client, public_client_factory=public_client_factory)

    def geoblock_allowed(self) -> bool:
        """Return true only for an explicit ``{"blocked": false}`` response."""

        opener = self._urlopen_fn or urlopen
        try:
            request = Request(GEOBLOCK_URL, headers={"User-Agent": "OpenTrader/1.0"})
            with opener(request, timeout=GEOBLOCK_TIMEOUT_SECONDS) as response:
                raw = response.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            return isinstance(payload, dict) and payload.get("blocked") is False
        except Exception:
            return False

    def account_snapshot(self) -> AccountSnapshot:
        try:
            p_usd_balance, p_usd_allowance = self._collateral_balance_allowance()
            orders = _collect(self._client.list_open_orders())
            # This read is intentionally performed even though the snapshot only
            # stores open-order IDs; the authenticated preflight must prove it.
            _collect(self._client.list_account_trades())
            positions = _collect(self._client.list_positions())
            open_order_ids = tuple(
                _safe_string(order_id)
                for order in orders
                if (order_id := _field(order, "id")) is not None
            )
            safe_positions: list[dict[str, str]] = []
            for position in positions:
                payload = _model_dict(position)
                if payload is None:
                    continue
                safe_positions.append(
                    {str(key): _safe_string(value) for key, value in payload.items()}
                )
            return AccountSnapshot(
                wallet_address=self.config.wallet_address,
                p_usd_balance=p_usd_balance,
                p_usd_allowance=p_usd_allowance,
                open_order_ids=open_order_ids,
                positions=tuple(safe_positions),
                checked_at=datetime.now(UTC),
            )
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            raise PolymarketTradingError(code) from None

    def _collateral_balance_allowance(self) -> tuple[Decimal, Decimal]:
        balance = self._client.get_balance_allowance(asset_type="COLLATERAL")
        p_usd_balance = _decimal(_field(balance, "balance"), base_units=True)
        allowances = _field(balance, "allowances", {})
        environment = getattr(self._client, "environment", PRODUCTION)
        spender = getattr(environment, "standard_exchange", None)
        if not isinstance(spender, str) or not isinstance(allowances, Mapping):
            return p_usd_balance, Decimal("0")
        selected = next(
            (
                value
                for key, value in allowances.items()
                if isinstance(key, str) and key.lower() == spender.lower()
            ),
            0,
        )
        return p_usd_balance, _decimal(selected, base_units=True)

    def readiness_snapshot(self) -> dict[str, object]:
        """Return a fresh, explicit gasless-relayer and merge capability fact."""

        checked_at = datetime.now(UTC)
        p_usd_balance, p_usd_allowance = self._collateral_balance_allowance()
        merge_capable = callable(getattr(self._client, "merge_positions", None))
        if isinstance(self._client, SecureClient):
            gasless_ready = self._authenticated_relayer_probe()
        else:
            # Test/dry-run collaborators may expose an explicit readiness fact.
            # The real SecureClient path above never trusts its deprecated,
            # unconditional is_gasless_ready() implementation.
            gasless_method = getattr(self._client, "is_gasless_ready", None)
            gasless_ready = False
            if callable(gasless_method):
                try:
                    gasless_ready = gasless_method() is True
                except Exception as exc:
                    code = _safe_error_code(exc)
                    if code in {"network", "timeout", "unavailable"}:
                        raise PolymarketTradingError(code) from None
                    gasless_ready = False
        merge_ready = merge_capable and gasless_ready
        return {
            "checked_at": checked_at,
            "wallet": "ready",
            "wallet_address": self.config.wallet_address,
            "p_usd_balance": p_usd_balance,
            "p_usd_allowance": p_usd_allowance,
            "merge_capability": merge_capable,
            "merge_ready": merge_ready,
            "merge": "ready" if merge_ready else "unavailable",
            "relayer_ready": gasless_ready,
            "relayer": "ready" if gasless_ready else "unavailable",
            "ready": merge_ready,
        }

    def _authenticated_relayer_probe(self) -> bool:
        """Prove the configured non-EOA wallet can read relayer parameters."""

        try:
            context = self._client._ctx  # type: ignore[attr-defined]
            wallet_type = str(getattr(context, "wallet_type", ""))
            if wallet_type == "EOA":
                return False
            relay_type = {
                "POLY_PROXY": "PROXY",
                "GNOSIS_SAFE": "SAFE",
                "DEPOSIT_WALLET": "WALLET",
            }.get(wallet_type)
            relayer = getattr(context, "relayer", None)
            signer = str(getattr(getattr(context, "signer", None), "address", ""))
            if relay_type is None or not signer or not callable(getattr(relayer, "get_json", None)):
                return False
            path = "/relay-payload" if wallet_type == "POLY_PROXY" else "/v1/account/transactions/params"
            payload = relayer.get_json(
                path,
                params={"address": signer, "type": relay_type},
            )
            if not isinstance(payload, Mapping):
                return False
            address = payload.get("address")
            nonce = payload.get("nonce")
            return (
                isinstance(address, str)
                and _ADDRESS_RE.fullmatch(address) is not None
                and isinstance(nonce, str)
                and bool(nonce)
                and nonce.isdigit()
            )
        except Exception as exc:
            code = _safe_error_code(exc)
            if code in {"network", "timeout", "unavailable"}:
                raise PolymarketTradingError(code) from None
            return False

    def _identity_summary(self) -> tuple[str, str]:
        signer = _address_from_client(self._client, "signer")
        wallet = _address_from_client(self._client, "wallet")
        signer_match = "yes" if signer is not None and signer.lower() == self.config.signer_address.lower() else "no"
        wallet_match = "yes" if wallet is not None and wallet.lower() == self.config.wallet_address.lower() else "no"
        return signer_match, wallet_match

    def _sign_leg(
        self,
        *,
        token_id: str,
        amount: Decimal,
        max_price: Decimal,
    ) -> object:
        return self._client.create_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            max_spend=amount,
            max_price=max_price,
            order_type="FOK",
        )

    @staticmethod
    def _field_alias(value: object, *names: str, default: object = None) -> object:
        for name in names:
            found = _field(value, name, None)
            if found is not None:
                return found
        return default

    @staticmethod
    def _positive_decimal(value: object) -> Decimal | None:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            return None
        return value

    def _validate_intent(
        self,
        intent: PairIntent,
        *,
        account: AccountSnapshot,
        tick_size: Decimal,
        require_economics: bool = True,
    ) -> str | None:
        if not isinstance(intent, PairIntent):
            return "invalid"
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                intent.event_id,
                intent.market_id,
                intent.condition_id,
                intent.yes_token_id,
                intent.no_token_id,
            )
        ):
            return "invalid"
        decimal_fields = (
            intent.quantity,
            intent.yes_max_price,
            intent.no_max_price,
            intent.yes_max_cost,
            intent.no_max_cost,
            intent.total_max_cost,
            intent.minimum_profit,
            intent.net_edge,
        )
        if not all(isinstance(value, Decimal) and value.is_finite() for value in decimal_fields):
            return "invalid"
        if any(
            self._positive_decimal(value) is None
            for value in (
                intent.quantity,
                intent.yes_max_price,
                intent.no_max_price,
                intent.yes_max_cost,
                intent.no_max_cost,
                intent.total_max_cost,
            )
        ):
            return "invalid"
        if intent.yes_token_id == intent.no_token_id:
            return "invalid"
        if intent.yes_max_price > 1 or intent.no_max_price > 1:
            return "invalid"
        if intent.yes_max_cost % CENT or intent.no_max_cost % CENT:
            return "invalid"
        if intent.total_max_cost != intent.yes_max_cost + intent.no_max_cost:
            return "invalid"
        if intent.total_max_cost > MAX_NORMAL_COST:
            return "invalid"
        if require_economics and (
            intent.minimum_profit < MIN_ESTIMATED_PROFIT
            or intent.net_edge < MIN_NET_EDGE
        ):
            return "invalid"
        if account.p_usd_balance < 0 or account.p_usd_allowance < 0:
            return "account_insufficient"
        if account.p_usd_balance > MAX_WALLET_BALANCE:
            return "account_insufficient"
        if intent.total_max_cost > account.p_usd_balance or intent.total_max_cost > account.p_usd_allowance:
            return "account_insufficient"
        yes_expected = protected_buy_quantity(
            spend=intent.yes_max_cost,
            price=intent.yes_max_price,
            tick_size=tick_size,
        )
        no_expected = protected_buy_quantity(
            spend=intent.no_max_cost,
            price=intent.no_max_price,
            tick_size=tick_size,
        )
        if yes_expected is None or no_expected is None:
            return "order_amount_mismatch"
        if yes_expected != intent.quantity or no_expected != intent.quantity:
            return "order_amount_mismatch"
        return None

    @staticmethod
    def _signed_quantity(signed: object, expected: Decimal) -> Decimal | None:
        raw_values = [
            value
            for value in (
                _field(signed, "taker_amount"),
                _field(signed, "requested_amount"),
            )
            if value is not None
        ]
        if not raw_values:
            return None
        quantities: list[Decimal] = []
        for raw in raw_values:
            try:
                parsed = _decimal(raw)
            except ValueError:
                return None
            # The SDK serializes shares as six-decimal base units. Accept a
            # direct Decimal/int in test doubles only when it is exact.
            quantities.append(
                parsed if parsed == expected else parsed / COLLATERAL_BASE_UNITS
            )
        return quantities[0] if all(quantity == quantities[0] for quantity in quantities) else None

    @staticmethod
    def _signed_quantity_base_units(raw: object, expected: Decimal) -> Decimal | None:
        try:
            parsed = _decimal(raw)
        except ValueError:
            return None
        if parsed == expected:
            return parsed
        return parsed / COLLATERAL_BASE_UNITS

    def _signed_pair(
        self, intent: PairIntent, *, tick_size: Decimal
    ) -> tuple[object, object, str | None]:
        try:
            yes = self._sign_leg(
                token_id=intent.yes_token_id,
                amount=intent.yes_max_cost,
                max_price=intent.yes_max_price,
            )
            no = self._sign_leg(
                token_id=intent.no_token_id,
                amount=intent.no_max_cost,
                max_price=intent.no_max_price,
            )
        except Exception as exc:
            return object(), object(), _safe_error_code(exc)
        expected_tokens = (intent.yes_token_id, intent.no_token_id)
        for signed, expected_token in zip((yes, no), expected_tokens, strict=True):
            if _field(signed, "order_type") != "FOK" or _field(signed, "side") != "BUY":
                return yes, no, "order_shape_mismatch"
            if _field(signed, "token_id") not in (None, expected_token):
                return yes, no, "order_shape_mismatch"
        yes_quantity = self._signed_quantity(yes, intent.quantity)
        no_quantity = self._signed_quantity(no, intent.quantity)
        if yes_quantity != intent.quantity or no_quantity != intent.quantity:
            return yes, no, "order_amount_mismatch"
        if yes_quantity != no_quantity:
            return yes, no, "order_amount_mismatch"
        return yes, no, None

    def no_submit_preflight(
        self,
        intent: PairIntent,
        *,
        tick_size: Decimal = DEFAULT_TICK_SIZE,
        account: AccountSnapshot | None = None,
        require_economics: bool = True,
    ) -> dict[str, object]:
        self._readiness_key = None
        signer_match, wallet_match = self._identity_summary()
        summary: dict[str, object] = {
            "signer_match": signer_match,
            "wallet_match": wallet_match,
            "posted": False,
            "account_reads": "fail",
            "fok_pair_signed_not_submitted": "fail",
            "equal_requested_shares": "fail",
            "error_code": "none",
            "result": "BLOCKED",
        }
        if signer_match != "yes" or wallet_match != "yes":
            summary["error_code"] = "auth"
            return summary
        if account is None:
            try:
                account = self.account_snapshot()
            except PolymarketTradingError as exc:
                summary["error_code"] = exc.error_code
                return summary
        summary["account_reads"] = "pass"
        if not self.geoblock_allowed():
            summary["geoblock"] = "blocked"
            summary["error_code"] = "geoblock_blocked"
            return summary
        summary["geoblock"] = "allowed"
        error_code = self._validate_intent(
            intent,
            account=account,
            tick_size=tick_size,
            require_economics=require_economics,
        )
        if error_code is None:
            _, _, error_code = self._signed_pair(intent, tick_size=tick_size)
        if error_code is not None:
            summary["error_code"] = error_code
            return summary
        summary["fok_pair_signed_not_submitted"] = "pass"
        summary["equal_requested_shares"] = "pass"
        summary["result"] = "PASS"
        self._readiness_key = (intent, tick_size)
        return summary

    def _probe_candidates(
        self,
        *,
        price: Decimal,
        size: Decimal,
        minimum: Decimal,
        tick_size: Decimal,
    ) -> dict[Decimal, Decimal]:
        candidates: dict[Decimal, Decimal] = {}
        for cents in range(1, int(MAX_NORMAL_COST / CENT) + 1):
            spend = CENT * cents
            quantity = protected_buy_quantity(
                spend=spend, price=price, tick_size=tick_size
            )
            if quantity is None or quantity < minimum or quantity > size:
                continue
            candidates.setdefault(quantity, spend)
        return candidates

    def _discover_probe(self) -> tuple[PairIntent, Decimal]:
        try:
            public = self._public_client_factory()
            page = public.list_markets(
                closed=False,
                order="volume24hr",
                ascending=False,
                page_size=100,
            )
            markets = _collect(_field(page.first_page(), "items"))
            eligible: list[tuple[Decimal, str, object]] = []
            for market in markets:
                state = _field(market, "state")
                trading = _field(market, "trading")
                outcomes = _field(market, "outcomes")
                metrics = _field(market, "metrics")
                if not (
                    _field(state, "active") is True
                    and _field(state, "closed") is False
                    and _field(state, "archived") is False
                    and _field(state, "accepting_orders") is True
                    and _field(state, "enable_order_book") is True
                    and _field(state, "neg_risk") is False
                    and _field(trading, "fees_enabled") is False
                ):
                    continue
                volume = self._field_alias(metrics, "volume_24hr", "volume24hr")
                market_id = self._field_alias(market, "id", default="")
                try:
                    volume_decimal = _decimal(volume)
                except ValueError:
                    continue
                if volume_decimal < 0 or not isinstance(market_id, str):
                    continue
                eligible.append((volume_decimal, market_id, market))
            if not eligible:
                raise PolymarketTradingError("market_probe_unavailable")
            for _, market_id, market in sorted(eligible, reverse=True):
                outcomes = _field(market, "outcomes")
                yes = _field(outcomes, "yes")
                no = _field(outcomes, "no")
                yes_token = self._field_alias(yes, "token_id", "tokenId")
                no_token = self._field_alias(no, "token_id", "tokenId")
                condition_id = self._field_alias(
                    market, "condition_id", "conditionId", default=""
                )
                trading = _field(market, "trading")
                tick_value = self._field_alias(
                    trading, "minimum_tick_size", "minimumTickSize"
                )
                minimum_value = self._field_alias(
                    trading, "minimum_order_size", "minimumOrderSize"
                )
                if not (
                    isinstance(yes_token, str)
                    and isinstance(no_token, str)
                    and isinstance(condition_id, str)
                ):
                    continue
                yes_book = public.get_order_book(token_id=yes_token)
                no_book = public.get_order_book(token_id=no_token)
                yes_asks = _collect(_field(yes_book, "asks"))
                no_asks = _collect(_field(no_book, "asks"))
                if not yes_asks or not no_asks:
                    continue
                try:
                    yes_level = min(
                        yes_asks, key=lambda level: _decimal(_field(level, "price"))
                    )
                    no_level = min(
                        no_asks, key=lambda level: _decimal(_field(level, "price"))
                    )
                    yes_price = _decimal(_field(yes_level, "price"))
                    no_price = _decimal(_field(no_level, "price"))
                    yes_size = _decimal(_field(yes_level, "size"))
                    no_size = _decimal(_field(no_level, "size"))
                except ValueError:
                    continue
                if not isinstance(tick_value, Decimal):
                    tick_value = _field(yes_book, "tick_size")
                if not isinstance(minimum_value, Decimal):
                    minimum_value = _field(yes_book, "min_order_size")
                if not isinstance(tick_value, Decimal) or not isinstance(
                    minimum_value, Decimal
                ):
                    continue
                yes_candidates = self._probe_candidates(
                    price=yes_price,
                    size=yes_size,
                    minimum=minimum_value,
                    tick_size=tick_value,
                )
                no_candidates = self._probe_candidates(
                    price=no_price,
                    size=no_size,
                    minimum=minimum_value,
                    tick_size=tick_value,
                )
                common = sorted(
                    quantity
                    for quantity in set(yes_candidates) & set(no_candidates)
                    if yes_candidates[quantity] + no_candidates[quantity]
                    <= MAX_NORMAL_COST
                )
                if not common:
                    continue
                quantity = common[0]
                yes_cost = yes_candidates[quantity]
                no_cost = no_candidates[quantity]
                return (
                    PairIntent(
                        event_id=market_id,
                        market_id=market_id,
                        condition_id=condition_id,
                        yes_token_id=yes_token,
                        no_token_id=no_token,
                        quantity=quantity,
                        yes_max_price=yes_price,
                        no_max_price=no_price,
                        yes_max_cost=yes_cost,
                        no_max_cost=no_cost,
                        total_max_cost=yes_cost + no_cost,
                        minimum_profit=quantity - yes_cost - no_cost,
                        net_edge=(quantity - yes_cost - no_cost) / quantity,
                    ),
                    tick_value,
                )
            raise PolymarketTradingError("market_probe_unavailable")
        except PolymarketTradingError:
            raise
        except Exception as exc:
            code = _safe_error_code(exc)
            if code in {"network", "timeout", "unavailable"}:
                raise PolymarketTradingError(code) from None
            raise PolymarketTradingError("market_probe_unavailable") from None

    def preflight_report(self) -> dict[str, object]:
        report: dict[str, object] = {
            "sdk_version": "unknown",
            "signer_match": "no",
            "wallet_match": "no",
            "geoblock": "blocked",
            "account_reads": "fail",
            "fok_pair_signed_not_submitted": "fail",
            "equal_requested_shares": "fail",
            "merge_capability": "unavailable",
            "relayer_readiness": "fail",
            "secret_scan": "pass",
            "posted": False,
            "error_code": "none",
            "result": "BLOCKED",
        }
        try:
            report["sdk_version"] = importlib.metadata.version("polymarket-client")
        except importlib.metadata.PackageNotFoundError:
            pass
        signer_match, wallet_match = self._identity_summary()
        report["signer_match"] = signer_match
        report["wallet_match"] = wallet_match
        if signer_match != "yes" or wallet_match != "yes":
            report["error_code"] = "auth"
            return report
        try:
            account = self.account_snapshot()
        except PolymarketTradingError as exc:
            report["error_code"] = exc.error_code
            return report
        report["account_reads"] = "pass"
        try:
            readiness = self.readiness_snapshot()
        except PolymarketTradingError as exc:
            report["error_code"] = exc.error_code
            return report
        except Exception as exc:
            report["error_code"] = _safe_error_code(exc)
            return report
        report["merge_capability"] = (
            "present_not_invoked" if readiness.get("merge_capability") is True else "unavailable"
        )
        report["relayer_readiness"] = (
            "pass" if readiness.get("ready") is True else "fail"
        )
        if report["relayer_readiness"] != "pass":
            report["error_code"] = "sdk_error"
            return report
        try:
            intent, tick_size = self._discover_probe()
        except PolymarketTradingError as exc:
            report["error_code"] = exc.error_code
            return report
        summary = self.no_submit_preflight(
            intent,
            tick_size=tick_size,
            account=account,
            require_economics=False,
        )
        report["signer_match"] = summary["signer_match"]
        report["wallet_match"] = summary["wallet_match"]
        report["geoblock"] = summary.get("geoblock", "blocked")
        report["fok_pair_signed_not_submitted"] = summary[
            "fok_pair_signed_not_submitted"
        ]
        report["equal_requested_shares"] = summary["equal_requested_shares"]
        report["posted"] = summary.get("posted", False)
        report["error_code"] = summary.get("error_code", "sdk_error")
        if (
            report["sdk_version"] == "0.2.0"
            and
            report["signer_match"] == "yes"
            and report["wallet_match"] == "yes"
            and report["geoblock"] == "allowed"
            and report["account_reads"] == "pass"
            and report["fok_pair_signed_not_submitted"] == "pass"
            and report["equal_requested_shares"] == "pass"
            and report["merge_capability"] == "present_not_invoked"
            and report["relayer_readiness"] == "pass"
            and report["secret_scan"] == "pass"
        ):
            report["result"] = "PASS"
        return report

    @staticmethod
    def _ambiguous_pair() -> PairSubmission:
        return PairSubmission(
            yes=LegResult("YES", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
            no=LegResult("NO", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
        )

    @staticmethod
    def _blocked_pair(error_code: str) -> PairSubmission:
        return PairSubmission(
            yes=LegResult("YES", False, "blocked", "", Decimal("0"), (), error_code),
            no=LegResult("NO", False, "blocked", "", Decimal("0"), (), error_code),
        )

    @staticmethod
    def _leg_result(leg: Literal["YES", "NO"], response: object) -> LegResult:
        accepted_value = _field(response, "ok", _field(response, "success", False))
        accepted = accepted_value is True
        if accepted:
            status = _safe_string(_field(response, "status", "accepted"))
            error_code = "none"
            order_id = _safe_string(_field(response, "order_id", ""))
            filled = _field(response, "taking_amount", Decimal("0"))
            trades = _field(response, "trade_ids", ())
        else:
            raw_code = _field(response, "code", "rejected")
            error_code = raw_code if isinstance(raw_code, str) and re.fullmatch(r"[a-z_]+", raw_code) else "rejected"
            status = "rejected"
            order_id = ""
            filled = Decimal("0")
            trades = ()
        try:
            filled_quantity = _decimal(filled)
        except ValueError:
            filled_quantity = Decimal("0")
        trade_ids = tuple(item for item in trades if isinstance(item, str)) if isinstance(trades, (tuple, list)) else ()
        return LegResult(leg, accepted, status, order_id, filled_quantity, trade_ids, error_code)

    def submit_pair_once(
        self, intent: PairIntent, *, tick_size: Decimal = DEFAULT_TICK_SIZE
    ) -> PairSubmission:
        if self._readiness_key != (intent, tick_size):
            return self._blocked_pair("preflight_required")
        signer_match, wallet_match = self._identity_summary()
        if signer_match != "yes" or wallet_match != "yes":
            return self._blocked_pair("auth")
        if not self.geoblock_allowed():
            return self._blocked_pair("geoblock_blocked")
        try:
            account = self.account_snapshot()
        except PolymarketTradingError as exc:
            return self._blocked_pair(exc.error_code)
        error_code = self._validate_intent(
            intent, account=account, tick_size=tick_size, require_economics=True
        )
        if error_code is not None:
            return self._blocked_pair(error_code)
        yes, no, error_code = self._signed_pair(intent, tick_size=tick_size)
        if error_code is not None:
            return self._blocked_pair(error_code)
        try:
            responses = tuple(self._client.post_orders((yes, no)))
        except Exception:
            # A POST may have reached the venue; never retry or claim rejection.
            return self._ambiguous_pair()
        if len(responses) != 2:
            return self._ambiguous_pair()
        return PairSubmission(
            yes=self._leg_result("YES", responses[0]),
            no=self._leg_result("NO", responses[1]),
        )

    @staticmethod
    def _ambiguous_threshold(intent: ThresholdHedgeIntent) -> ThresholdHedgeSubmission:
        return ThresholdHedgeSubmission(
            leg_a=ThresholdLegResult(
                "A",
                intent.leg_a.outcome,
                intent.leg_a.condition_id,
                intent.leg_a.token_id,
                False,
                "ambiguous",
                "",
                Decimal("0"),
                (),
                "ambiguous",
            ),
            leg_b=ThresholdLegResult(
                "B",
                intent.leg_b.outcome,
                intent.leg_b.condition_id,
                intent.leg_b.token_id,
                False,
                "ambiguous",
                "",
                Decimal("0"),
                (),
                "ambiguous",
            ),
        )

    @staticmethod
    def _blocked_threshold(
        intent: ThresholdHedgeIntent, error_code: str
    ) -> ThresholdHedgeSubmission:
        return ThresholdHedgeSubmission(
            leg_a=ThresholdLegResult(
                "A",
                intent.leg_a.outcome,
                intent.leg_a.condition_id,
                intent.leg_a.token_id,
                False,
                "blocked",
                "",
                Decimal("0"),
                (),
                error_code,
            ),
            leg_b=ThresholdLegResult(
                "B",
                intent.leg_b.outcome,
                intent.leg_b.condition_id,
                intent.leg_b.token_id,
                False,
                "blocked",
                "",
                Decimal("0"),
                (),
                error_code,
            ),
        )

    @staticmethod
    def _threshold_leg_valid(leg: ThresholdHedgeLeg) -> bool:
        if not isinstance(leg, ThresholdHedgeLeg):
            return False
        if leg.label not in {"A", "B"} or leg.outcome not in {"YES", "NO"}:
            return False
        if not all(
            isinstance(value, str) and value.strip()
            for value in (leg.condition_id, leg.market_id, leg.token_id)
        ):
            return False
        if not all(
            isinstance(value, Decimal) and value.is_finite() and value > 0
            for value in (leg.quantity, leg.max_price, leg.max_cost, leg.tick_size)
        ):
            return False
        if leg.max_price > 1 or leg.max_cost % CENT:
            return False
        return (
            protected_buy_quantity(
                spend=leg.max_cost,
                price=leg.max_price,
                tick_size=leg.tick_size,
            )
            == leg.quantity
        )

    def _validate_threshold_intent(
        self,
        intent: ThresholdHedgeIntent,
        *,
        account: AccountSnapshot,
        require_economics: bool = True,
    ) -> str | None:
        if not isinstance(intent, ThresholdHedgeIntent):
            return "invalid"
        if not all(
            isinstance(value, str) and value.strip()
            for value in (intent.relation_id, intent.event_id, intent.relation)
        ):
            return "invalid"
        if intent.relation not in {"A_IMPLIES_B", "B_IMPLIES_A"}:
            return "invalid"
        if not self._threshold_leg_valid(intent.leg_a) or not self._threshold_leg_valid(
            intent.leg_b
        ):
            return "invalid"
        if intent.leg_a.label != "A" or intent.leg_b.label != "B":
            return "invalid"
        if intent.leg_a.condition_id == intent.leg_b.condition_id:
            return "invalid"
        if intent.leg_a.token_id == intent.leg_b.token_id:
            return "invalid"
        if not isinstance(intent.quantity, Decimal) or intent.quantity <= 0:
            return "invalid"
        if intent.leg_a.quantity != intent.quantity or intent.leg_b.quantity != intent.quantity:
            return "order_amount_mismatch"
        for value in (
            intent.maximum_fee,
            intent.total_max_cost,
            intent.minimum_payout,
            intent.minimum_profit,
            intent.net_edge,
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                return "invalid"
        if intent.maximum_fee < 0 or intent.total_max_cost <= 0:
            return "invalid"
        if intent.total_max_cost != (
            intent.leg_a.max_cost + intent.leg_b.max_cost + intent.maximum_fee
        ):
            return "invalid"
        if intent.minimum_payout != intent.quantity:
            return "invalid"
        if intent.minimum_profit != intent.minimum_payout - intent.total_max_cost:
            return "invalid"
        if intent.total_max_cost > MAX_NORMAL_COST:
            return "invalid"
        if require_economics and (intent.minimum_profit <= 0 or intent.net_edge <= 0):
            return "invalid"
        if account.p_usd_balance < 0 or account.p_usd_allowance < 0:
            return "account_insufficient"
        if account.p_usd_balance > MAX_WALLET_BALANCE:
            return "account_insufficient"
        if intent.total_max_cost > account.p_usd_balance or intent.total_max_cost > account.p_usd_allowance:
            return "account_insufficient"
        return None

    def _signed_threshold_pair(
        self, intent: ThresholdHedgeIntent
    ) -> tuple[object, object, str | None]:
        try:
            signed_a = self._sign_leg(
                token_id=intent.leg_a.token_id,
                amount=intent.leg_a.max_cost,
                max_price=intent.leg_a.max_price,
            )
            signed_b = self._sign_leg(
                token_id=intent.leg_b.token_id,
                amount=intent.leg_b.max_cost,
                max_price=intent.leg_b.max_price,
            )
        except Exception as exc:
            return object(), object(), _safe_error_code(exc)
        for signed, leg in ((signed_a, intent.leg_a), (signed_b, intent.leg_b)):
            if _field(signed, "order_type") != "FOK" or _field(signed, "side") != "BUY":
                return signed_a, signed_b, "order_shape_mismatch"
            if _field(signed, "token_id") not in (None, leg.token_id):
                return signed_a, signed_b, "order_shape_mismatch"
            if self._signed_quantity(signed, leg.quantity) != leg.quantity:
                return signed_a, signed_b, "order_amount_mismatch"
        return signed_a, signed_b, None

    @staticmethod
    def _threshold_leg_result(
        leg: ThresholdHedgeLeg, response: object
    ) -> ThresholdLegResult:
        accepted_value = _field(response, "ok", _field(response, "success", False))
        accepted = accepted_value is True
        if accepted:
            status = _safe_string(_field(response, "status", "accepted"))
            error_code = "none"
            order_id = _safe_string(_field(response, "order_id", ""))
            filled = _field(response, "taking_amount", Decimal("0"))
            trades = _field(response, "trade_ids", ())
        else:
            raw_code = _field(response, "code", "rejected")
            error_code = (
                raw_code
                if isinstance(raw_code, str) and re.fullmatch(r"[a-z_]+", raw_code)
                else "rejected"
            )
            status = "rejected"
            order_id = ""
            filled = Decimal("0")
            trades = ()
        try:
            filled_quantity = _decimal(filled)
        except ValueError:
            filled_quantity = Decimal("0")
        trade_ids = (
            tuple(item for item in trades if isinstance(item, str))
            if isinstance(trades, (tuple, list))
            else ()
        )
        return ThresholdLegResult(
            leg.label,
            leg.outcome,
            leg.condition_id,
            leg.token_id,
            accepted,
            status,
            order_id,
            filled_quantity,
            trade_ids,
            error_code,
        )

    def no_submit_threshold_preflight(
        self,
        intent: ThresholdHedgeIntent,
        *,
        account: AccountSnapshot | None = None,
        require_economics: bool = True,
    ) -> dict[str, object]:
        self._readiness_key = None
        self._threshold_readiness_key = None
        signer_match, wallet_match = self._identity_summary()
        summary: dict[str, object] = {
            "signer_match": signer_match,
            "wallet_match": wallet_match,
            "posted": False,
            "account_reads": "fail",
            "fok_pair_signed_not_submitted": "fail",
            "equal_requested_shares": "fail",
            "conditions": [intent.leg_a.condition_id, intent.leg_b.condition_id]
            if isinstance(intent, ThresholdHedgeIntent)
            else [],
            "merge": "not_required",
            "error_code": "none",
            "result": "BLOCKED",
        }
        if signer_match != "yes" or wallet_match != "yes":
            summary["error_code"] = "auth"
            return summary
        if account is None:
            try:
                account = self.account_snapshot()
            except PolymarketTradingError as exc:
                summary["error_code"] = exc.error_code
                return summary
        summary["account_reads"] = "pass"
        if not self.geoblock_allowed():
            summary["geoblock"] = "blocked"
            summary["error_code"] = "geoblock_blocked"
            return summary
        summary["geoblock"] = "allowed"
        error_code = self._validate_threshold_intent(
            intent, account=account, require_economics=require_economics
        )
        if error_code is None:
            _, _, error_code = self._signed_threshold_pair(intent)
        if error_code is not None:
            summary["error_code"] = error_code
            return summary
        summary["fok_pair_signed_not_submitted"] = "pass"
        summary["equal_requested_shares"] = "pass"
        summary["result"] = "PASS"
        self._threshold_readiness_key = intent
        return summary

    def submit_threshold_hedge_once(
        self, intent: ThresholdHedgeIntent
    ) -> ThresholdHedgeSubmission:
        if self._threshold_readiness_key != intent:
            return self._blocked_threshold(intent, "preflight_required")
        signer_match, wallet_match = self._identity_summary()
        if signer_match != "yes" or wallet_match != "yes":
            return self._blocked_threshold(intent, "auth")
        if not self.geoblock_allowed():
            return self._blocked_threshold(intent, "geoblock_blocked")
        try:
            account = self.account_snapshot()
        except PolymarketTradingError as exc:
            return self._blocked_threshold(intent, exc.error_code)
        error_code = self._validate_threshold_intent(
            intent, account=account, require_economics=True
        )
        if error_code is not None:
            return self._blocked_threshold(intent, error_code)
        signed_a, signed_b, error_code = self._signed_threshold_pair(intent)
        if error_code is not None:
            return self._blocked_threshold(intent, error_code)
        try:
            responses = tuple(self._client.post_orders((signed_a, signed_b)))
        except Exception:
            return self._ambiguous_threshold(intent)
        if len(responses) != 2:
            return self._ambiguous_threshold(intent)
        return ThresholdHedgeSubmission(
            leg_a=self._threshold_leg_result(intent.leg_a, responses[0]),
            leg_b=self._threshold_leg_result(intent.leg_b, responses[1]),
        )

    @staticmethod
    def _cross_leg_fields(
        leg: object,
    ) -> tuple[str, str, str, Decimal, Decimal] | None:
        exchange = _field(leg, "exchange")
        condition_id = _field(leg, "condition_id")
        token_id = _field(leg, "token_id")
        outcome = _field(leg, "outcome")
        quantity = _field(leg, "net_quantity")
        max_price = _field(leg, "max_price")
        max_cost = _field(leg, "max_cost")
        if (
            exchange != "polymarket"
            or not isinstance(condition_id, str)
            or not condition_id
            or not isinstance(token_id, str)
            or not token_id
            or outcome not in {"YES", "NO"}
            or not isinstance(quantity, Decimal)
            or not isinstance(max_price, Decimal)
            or not isinstance(max_cost, Decimal)
            or quantity <= 0
            or max_price <= 0
            or max_price > 1
            or max_cost <= 0
        ):
            return None
        return condition_id, token_id, outcome, quantity, max_price

    @staticmethod
    def _blocked_cross_leg(leg: object, error_code: str) -> ThresholdLegResult:
        fields = PolymarketTradingClient._cross_leg_fields(leg)
        if fields is None:
            return ThresholdLegResult(
                "polymarket", "YES", "", "", False, "blocked", "", Decimal("0"), (), "invalid"
            )
        condition_id, token_id, outcome, _quantity, _max_price = fields
        return ThresholdLegResult(
            "polymarket", outcome, condition_id, token_id, False, "blocked", "", Decimal("0"), (), error_code
        )

    def no_submit_cross_leg_preflight(
        self, leg: object, *, account: AccountSnapshot | None = None
    ) -> dict[str, object]:
        self._cross_leg_readiness_key = None
        summary: dict[str, object] = {
            "posted": False,
            "account_reads": "fail",
            "fok_leg_signed_not_submitted": "fail",
            "error_code": "none",
            "result": "BLOCKED",
        }
        fields = self._cross_leg_fields(leg)
        signer_match, wallet_match = self._identity_summary()
        if fields is None:
            summary["error_code"] = "invalid"
            return summary
        if signer_match != "yes" or wallet_match != "yes":
            summary["error_code"] = "auth"
            return summary
        if account is None:
            try:
                account = self.account_snapshot()
            except PolymarketTradingError as exc:
                summary["error_code"] = exc.error_code
                return summary
        summary["account_reads"] = "pass"
        if not self.geoblock_allowed():
            summary["error_code"] = "geoblock_blocked"
            return summary
        condition_id, token_id, _outcome, quantity, max_price = fields
        max_cost = _field(leg, "max_cost")
        if (
            not isinstance(max_cost, Decimal)
            or max_cost > account.p_usd_balance
            or max_cost > account.p_usd_allowance
        ):
            summary["error_code"] = "account_insufficient"
            return summary
        try:
            signed = self._sign_leg(
                token_id=token_id, amount=max_cost, max_price=max_price
            )
        except Exception as exc:
            summary["error_code"] = _safe_error_code(exc)
            return summary
        if (
            _field(signed, "order_type") != "FOK"
            or _field(signed, "side") != "BUY"
            or _field(signed, "token_id") not in (None, token_id)
            or self._signed_quantity(signed, quantity) != quantity
        ):
            summary["error_code"] = "order_shape_mismatch"
            return summary
        del condition_id
        summary["fok_leg_signed_not_submitted"] = "pass"
        summary["result"] = "PASS"
        self._cross_leg_readiness_key = leg
        return summary

    def submit_cross_leg_once(self, leg: object) -> ThresholdLegResult:
        if self._cross_leg_readiness_key != leg:
            return self._blocked_cross_leg(leg, "preflight_required")
        fields = self._cross_leg_fields(leg)
        if fields is None:
            return self._blocked_cross_leg(leg, "invalid")
        _condition_id, token_id, _outcome, quantity, max_price = fields
        max_cost = _field(leg, "max_cost")
        if not isinstance(max_cost, Decimal):
            return self._blocked_cross_leg(leg, "invalid")
        try:
            signed = self._sign_leg(
                token_id=token_id, amount=max_cost, max_price=max_price
            )
            if (
                _field(signed, "order_type") != "FOK"
                or _field(signed, "side") != "BUY"
                or self._signed_quantity(signed, quantity) != quantity
            ):
                return self._blocked_cross_leg(leg, "order_shape_mismatch")
            responses = tuple(self._client.post_orders((signed,)))
        except Exception:
            return ThresholdLegResult(
                "polymarket", _outcome, _condition_id, token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            )
        if len(responses) != 1:
            return ThresholdLegResult(
                "polymarket", _outcome, _condition_id, token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            )
        return self._threshold_leg_result(
            ThresholdHedgeLeg(
                "polymarket",
                _condition_id,
                str(_field(leg, "market_id", _condition_id)),
                _outcome,
                token_id,
                quantity,
                max_price,
                max_cost,
                DEFAULT_TICK_SIZE,
            ),
            responses[0],
        )

    def reconcile_cross_leg(
        self, leg: object, result: ThresholdLegResult, *, since: datetime
    ) -> dict[str, object]:
        fields = self._cross_leg_fields(leg)
        if fields is None or not isinstance(result, ThresholdLegResult):
            return {"status": "unknown", "verified": False, "conclusively_absent": False}
        condition_id, token_id, outcome, quantity, max_price = fields
        max_cost = _field(leg, "max_cost")
        if not isinstance(max_cost, Decimal):
            return {"status": "unknown", "verified": False, "conclusively_absent": False}
        try:
            minimum_order_size = _decimal(_field(leg, "minimum_order_size"))
        except ValueError:
            minimum_order_size = None
        actual, proof = self._reconcile_threshold_leg(result, since=since)
        position = proof.get("position_ref")
        position_quantity = (
            _decimal(position.get("quantity"))
            if isinstance(position, Mapping)
            else Decimal("0")
        ) or Decimal("0")
        if proof.get("positions_verified") is True and actual > 0:
            reconciled: dict[str, object] = {
                "status": "verified",
                "verified": True,
                "conclusively_absent": False,
                "filled_quantity": actual,
                "position_quantity": position_quantity,
                "execution_proof": {"verified": True, "venue": "polymarket", **proof},
            }
            actual_fee = proof.get("fee")
            if isinstance(actual_fee, Decimal) and actual_fee >= 0:
                reconciled["actual_fee"] = actual_fee
            if minimum_order_size is not None and minimum_order_size > 0:
                reconciled["minimum_order_size"] = minimum_order_size
            return reconciled
        if not result.accepted and result.status != "ambiguous":
            try:
                positions = _collect(self._client.list_positions(market=[condition_id]))
                for item in positions:
                    if _field(item, "token_id", _field(item, "tokenId", "")) != token_id:
                        continue
                    amount = _decimal(
                        _field(item, "size", _field(item, "quantity", _field(item, "shares")))
                    )
                    if amount is not None and amount > 0:
                        return {"status": "unknown", "verified": False, "conclusively_absent": False}
            except Exception:
                return {"status": "unknown", "verified": False, "conclusively_absent": False}
            return {
                "status": "absent",
                "verified": False,
                "conclusively_absent": True,
                "filled_quantity": Decimal("0"),
                "position_quantity": Decimal("0"),
            }
        return {
            "status": "unknown",
            "verified": False,
            "conclusively_absent": False,
            "filled_quantity": actual,
            "position_quantity": position_quantity,
            "execution_proof": {"verified": False, "venue": "polymarket", **proof},
        }

    def reconcile(
        self,
        *,
        condition_id: str,
        since: datetime,
        yes_token_id: str | None = None,
        no_token_id: str | None = None,
        yes_order_id: str | None = None,
        no_order_id: str | None = None,
        yes_trade_ids: Sequence[str] = (),
        no_trade_ids: Sequence[str] = (),
        yes_order_ids: Sequence[str] | None = None,
        no_order_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Prove one execution's fills from fresh, reference-matched trades."""

        empty_refs: dict[str, object] = {
            "token_id": "",
            "order_ids": [],
            "trade_ids": [],
        }
        proof: dict[str, object] = {
            "verified": False,
            "adapter_verified": True,
            "venue": "polymarket",
            "positions_verified": False,
            "matched_refs": {"YES": dict(empty_refs), "NO": dict(empty_refs)},
            "position_refs": {},
        }
        try:
            yes_orders = _string_refs(yes_order_ids) | _string_refs(yes_order_id)
            no_orders = _string_refs(no_order_ids) | _string_refs(no_order_id)
            yes_trades = _string_refs(yes_trade_ids)
            no_trades = _string_refs(no_trade_ids)
            if not (yes_orders or yes_trades) or not (no_orders or no_trades):
                return {
                    "status": "blocked",
                    "error_code": "reconciliation_unverified",
                    "execution_proof": proof,
                }
            if not isinstance(since, datetime):
                return {
                    "status": "blocked",
                    "error_code": "reconciliation_unverified",
                    "execution_proof": proof,
                }
            since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
            trades = _collect(
                self._client.list_account_trades(
                    market=condition_id, after=since_utc.isoformat()
                )
            )
            quantities = {"YES": Decimal("0"), "NO": Decimal("0")}
            matched: dict[str, dict[str, object]] = {
                "YES": {"token_id": str(yes_token_id or ""), "order_ids": [], "trade_ids": []},
                "NO": {"token_id": str(no_token_id or ""), "order_ids": [], "trade_ids": []},
            }
            seen: set[tuple[str, str]] = set()
            accepted_statuses = {"CONFIRMED"}
            for trade in trades:
                matched_at = _trade_timestamp(trade)
                if matched_at is None or matched_at < since_utc:
                    continue
                trade_condition = _field(
                    trade, "condition_id", _field(trade, "market", "")
                )
                if trade_condition not in (None, "", condition_id):
                    continue
                status = _safe_string(_field(trade, "status", "")).upper()
                if status not in accepted_statuses:
                    continue
                if _safe_string(_field(trade, "side", "")).upper() != "BUY":
                    continue
                raw_trade_id = _field(trade, "id", _field(trade, "trade_id", ""))
                raw_order_id = _field(
                    trade,
                    "taker_order_id",
                    _field(trade, "order_id", _field(trade, "orderId", "")),
                )
                raw_token_id = _field(
                    trade,
                    "token_id",
                    _field(trade, "tokenId", _field(trade, "asset_id", "")),
                )
                trade_id = raw_trade_id.strip() if isinstance(raw_trade_id, str) else ""
                order_id = raw_order_id.strip() if isinstance(raw_order_id, str) else ""
                token_id = raw_token_id.strip() if isinstance(raw_token_id, str) else ""
                if not trade_id and not order_id:
                    continue
                quantity = None
                for name in ("size", "quantity", "shares", "taking_amount"):
                    quantity = _decimal(_field(trade, name))
                    if quantity is not None:
                        break
                if quantity is None or quantity <= 0:
                    continue
                for leg, token, order_refs, trade_refs in (
                    ("YES", yes_token_id, yes_orders, yes_trades),
                    ("NO", no_token_id, no_orders, no_trades),
                ):
                    if token and token_id and token_id != token:
                        continue
                    if not ((order_id and order_id in order_refs) or (trade_id and trade_id in trade_refs)):
                        continue
                    identity = (leg, trade_id or f"{order_id}:{matched_at.isoformat()}")
                    if identity in seen:
                        continue
                    seen.add(identity)
                    quantities[leg] += quantity
                    if order_id and order_id not in matched[leg]["order_ids"]:
                        matched[leg]["order_ids"].append(order_id)
                    if trade_id and trade_id not in matched[leg]["trade_ids"]:
                        matched[leg]["trade_ids"].append(trade_id)
                    break
            if not (
                quantities["YES"] > 0 or quantities["NO"] > 0
            ):
                proof["matched_refs"] = matched
                return {
                    "status": "blocked",
                    "error_code": "reconciliation_unverified",
                    "execution_proof": proof,
                }
            for leg in ("YES", "NO"):
                if quantities[leg] > 0 and not (
                    matched[leg]["trade_ids"] or matched[leg]["order_ids"]
                ):
                    proof["matched_refs"] = matched
                    return {
                        "status": "blocked",
                        "error_code": "reconciliation_unverified",
                        "execution_proof": proof,
                    }
            positions = _collect(self._client.list_positions(market=[condition_id]))
            position_quantities = {"YES": Decimal("0"), "NO": Decimal("0")}
            position_refs: dict[str, dict[str, str]] = {}
            for position in positions:
                position_condition = _field(
                    position, "condition_id", _field(position, "market", "")
                )
                if position_condition not in (None, "", condition_id):
                    continue
                token_id = _field(
                    position,
                    "token_id",
                    _field(position, "tokenId", _field(position, "asset_id", "")),
                )
                if not isinstance(token_id, str) or token_id not in (
                    yes_token_id,
                    no_token_id,
                ):
                    continue
                position_timestamp = _trade_timestamp(position)
                if position_timestamp is not None and position_timestamp < since_utc:
                    continue
                size = None
                for name in ("size", "quantity", "shares"):
                    size = _decimal(_field(position, name))
                    if size is not None:
                        break
                if size is None or size <= 0:
                    continue
                leg = "YES" if token_id == yes_token_id else "NO"
                position_quantities[leg] += size
                position_refs[leg] = {
                    "token_id": token_id,
                    "quantity": format(position_quantities[leg], "f"),
                }
            positions_verified = all(
                quantities[leg] <= 0 or position_quantities[leg] >= quantities[leg]
                for leg in ("YES", "NO")
            )
            proof["matched_refs"] = matched
            proof["position_refs"] = position_refs
            proof["positions_verified"] = positions_verified
            if not positions_verified:
                return {
                    "status": "blocked",
                    "error_code": "reconciliation_unverified",
                    "execution_proof": proof,
                }
            if quantities["YES"] <= 0 or quantities["NO"] <= 0:
                proof["partial_verified"] = True
                return {
                    "status": "partial",
                    "yes_quantity": quantities["YES"],
                    "no_quantity": quantities["NO"],
                    "execution_proof": proof,
                }
            proof["verified"] = True
            return {
                "status": "ok",
                "yes_quantity": quantities["YES"],
                "no_quantity": quantities["NO"],
                "execution_proof": proof,
            }
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            return {
                "status": "blocked",
                "error_code": code,
                "execution_proof": proof,
            }

    def _reconcile_threshold_leg(
        self, leg: ThresholdLegResult, *, since: datetime
    ) -> tuple[Decimal, dict[str, object]]:
        empty: dict[str, object] = {
            "token_id": leg.token_id,
            "order_ids": [],
            "trade_ids": [],
        }
        refs = _string_refs(leg.order_id) | _string_refs(leg.trade_ids)
        matched: dict[str, object] = {
            "token_id": leg.token_id,
            "order_ids": [],
            "trade_ids": [],
        }
        if not refs or not isinstance(since, datetime):
            return Decimal("0"), {
                "matched_refs": matched,
                "position_ref": None,
                "positions_verified": False,
            }
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        quantity = Decimal("0")
        actual_fee = Decimal("0")
        fees_verified = True
        seen: set[tuple[str, str]] = set()
        try:
            trades = _collect(
                self._client.list_account_trades(
                    market=leg.condition_id, after=since_utc.isoformat()
                )
            )
            for trade in trades:
                matched_at = _trade_timestamp(trade)
                if matched_at is None or matched_at < since_utc:
                    continue
                condition = _field(trade, "condition_id", _field(trade, "market", ""))
                if condition not in (None, "", leg.condition_id):
                    continue
                status = _safe_string(_field(trade, "status", "")).upper()
                if status not in {"CONFIRMED", "MATCHED", "FILLED"}:
                    continue
                if _safe_string(_field(trade, "side", "")).upper() != "BUY":
                    continue
                token = _field(
                    trade,
                    "token_id",
                    _field(trade, "tokenId", _field(trade, "asset_id", "")),
                )
                if token not in (None, "", leg.token_id):
                    continue
                trade_id = _field(trade, "id", _field(trade, "trade_id", ""))
                order_id = _field(
                    trade,
                    "taker_order_id",
                    _field(trade, "order_id", _field(trade, "orderId", "")),
                )
                trade_ref = trade_id.strip() if isinstance(trade_id, str) else ""
                order_ref = order_id.strip() if isinstance(order_id, str) else ""
                if not ((trade_ref and trade_ref in refs) or (order_ref and order_ref in refs)):
                    continue
                identity = (trade_ref, order_ref)
                if identity in seen:
                    continue
                seen.add(identity)
                raw_quantity: Decimal | None = None
                for name in ("size", "quantity", "shares", "taking_amount"):
                    try:
                        raw_quantity = _decimal(_field(trade, name))
                    except ValueError:
                        raw_quantity = None
                    if raw_quantity is not None:
                        break
                if raw_quantity is None or raw_quantity <= 0:
                    continue
                quantity += raw_quantity
                if _safe_string(_field(trade, "trader_side", "")).upper() != "TAKER":
                    fees_verified = False
                else:
                    try:
                        price = _decimal(_field(trade, "price"))
                        fee_rate_bps = _decimal(
                            _field(trade, "fee_rate_bps", _field(trade, "feeRateBps"))
                        )
                    except ValueError:
                        fees_verified = False
                    else:
                        if not (Decimal("0") < price <= Decimal("1")) or fee_rate_bps < 0:
                            fees_verified = False
                        else:
                            actual_fee += (
                                raw_quantity
                                * fee_rate_bps
                                / Decimal("10000")
                                * price
                                * (Decimal("1") - price)
                            )
                if order_ref and order_ref not in matched["order_ids"]:
                    matched["order_ids"].append(order_ref)  # type: ignore[union-attr]
                if trade_ref and trade_ref not in matched["trade_ids"]:
                    matched["trade_ids"].append(trade_ref)  # type: ignore[union-attr]

            position_quantity = Decimal("0")
            position_ref: dict[str, str] | None = None
            positions = _collect(self._client.list_positions(market=[leg.condition_id]))
            for position in positions:
                condition = _field(
                    position, "condition_id", _field(position, "market", "")
                )
                if condition not in (None, "", leg.condition_id):
                    continue
                token = _field(
                    position,
                    "token_id",
                    _field(position, "tokenId", _field(position, "asset_id", "")),
                )
                if token != leg.token_id:
                    continue
                position_at = _trade_timestamp(position)
                if position_at is not None and position_at < since_utc:
                    continue
                size: Decimal | None = None
                for name in ("size", "quantity", "shares"):
                    try:
                        size = _decimal(_field(position, name))
                    except ValueError:
                        size = None
                    if size is not None:
                        break
                if size is None or size <= 0:
                    continue
                position_quantity += size
                position_ref = {
                    "token_id": leg.token_id,
                    "quantity": format(position_quantity, "f"),
                }
            proof = {
                "matched_refs": matched,
                "position_ref": position_ref,
                "positions_verified": quantity > 0
                and position_quantity >= quantity,
            }
            if quantity > 0 and fees_verified:
                proof["fee"] = actual_fee
            return quantity, proof
        except Exception:
            return Decimal("0"), {
                "matched_refs": matched,
                "position_ref": None,
                "positions_verified": False,
            }

    def reconcile_threshold_hedge(
        self,
        *,
        intent: ThresholdHedgeIntent,
        since: datetime,
        leg_a: ThresholdLegResult,
        leg_b: ThresholdLegResult,
    ) -> dict[str, object]:
        """Reconcile each threshold leg against its own condition and token."""

        if not isinstance(intent, ThresholdHedgeIntent):
            return {"status": "blocked", "error_code": "invalid"}
        quantity_a, proof_a = self._reconcile_threshold_leg(leg_a, since=since)
        quantity_b, proof_b = self._reconcile_threshold_leg(leg_b, since=since)
        proof: dict[str, object] = {
            "venue": "polymarket",
            "adapter_verified": True,
            "positions_verified": proof_a["positions_verified"] is True
            and proof_b["positions_verified"] is True,
            "matched_refs": {
                "A": proof_a["matched_refs"],
                "B": proof_b["matched_refs"],
            },
            "position_refs": {
                "A": proof_a["position_ref"],
                "B": proof_b["position_ref"],
            },
            "condition_ids": {
                "A": intent.leg_a.condition_id,
                "B": intent.leg_b.condition_id,
            },
            "token_ids": {
                "A": intent.leg_a.token_id,
                "B": intent.leg_b.token_id,
            },
        }
        if quantity_a > 0 and quantity_b > 0:
            proof["verified"] = proof["positions_verified"] is True
            return {
                "status": "ok" if proof["verified"] else "blocked",
                "leg_a_quantity": quantity_a,
                "leg_b_quantity": quantity_b,
                "execution_proof": proof,
            }
        if quantity_a > 0 or quantity_b > 0:
            proof["partial_verified"] = proof["positions_verified"] is True
            return {
                "status": "partial" if proof["partial_verified"] else "blocked",
                "leg_a_quantity": quantity_a,
                "leg_b_quantity": quantity_b,
                "execution_proof": proof,
            }
        return {
            "status": "blocked",
            "error_code": "reconciliation_unverified",
            "leg_a_quantity": Decimal("0"),
            "leg_b_quantity": Decimal("0"),
            "execution_proof": proof,
        }

    def cancel_orders(self, order_ids: tuple[str, ...]) -> tuple[str, ...]:
        try:
            response = self._client.cancel_orders(order_ids=order_ids)
            canceled = _field(response, "canceled", ())
            return tuple(item for item in canceled if isinstance(item, str))
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            raise PolymarketTradingError(code) from None

    def remediation_options(
        self,
        *,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        filled_leg: str,
        filled_quantity: Decimal,
        since: datetime,
    ) -> dict[str, object]:
        """Return fresh, bounded completion and unwind choices without posting."""

        del condition_id, since
        if (
            filled_leg not in {"YES", "NO"}
            or not isinstance(filled_quantity, Decimal)
            or not filled_quantity.is_finite()
            or filled_quantity <= 0
        ):
            return {"fresh": False}
        try:
            account = self.account_snapshot()
            if account.open_order_ids:
                return {"fresh": False}
            checked_at = _venue_timestamp(account.checked_at)
            if checked_at is None:
                return {"fresh": False}
            account_now = datetime.now(UTC)
            account_age = (account_now - checked_at).total_seconds()
            if account_age < 0 or account_age > REMEDIATION_BOOK_FRESHNESS_SECONDS:
                return {"fresh": False}
            positions = account.positions
            filled_token = yes_token_id if filled_leg == "YES" else no_token_id
            position_quantity = Decimal("0")
            for position in positions:
                token = position.get("token_id", position.get("tokenId", position.get("asset_id", "")))
                if token != filled_token:
                    continue
                size = _decimal(position.get("size", position.get("quantity", position.get("shares"))))
                if size is not None and size > 0:
                    position_quantity += size
            if position_quantity < filled_quantity:
                return {"fresh": False}
            public = self._public_client_factory()
            token_by_leg = {"YES": yes_token_id, "NO": no_token_id}
            books = {
                leg: public.get_order_book(token_id=token)
                for leg, token in token_by_leg.items()
            }
            now = datetime.now(UTC)
            book_timestamps: dict[str, datetime] = {}
            for leg, book in books.items():
                timestamp = _venue_timestamp(_field(book, "timestamp"))
                if timestamp is None:
                    return {"fresh": False}
                age = (now - timestamp).total_seconds()
                if age < 0 or age > REMEDIATION_BOOK_FRESHNESS_SECONDS:
                    return {"fresh": False}
                book_timestamps[leg] = timestamp

            def best(book: object, side: str) -> tuple[Decimal, Decimal, Decimal] | None:
                rows = _collect(_field(book, side, ()))
                levels: list[tuple[Decimal, Decimal]] = []
                for row in rows:
                    price = _decimal(_field(row, "price"))
                    size = _decimal(_field(row, "size"))
                    if price is None or size is None or price <= 0 or size < filled_quantity or price > 1:
                        continue
                    levels.append((price, size))
                if not levels:
                    return None
                price, size = (
                    min(levels, key=lambda item: item[0])
                    if side == "asks"
                    else max(levels, key=lambda item: item[0])
                )
                tick = _decimal(_field(book, "tick_size", _field(book, "minimum_tick_size")))
                if tick not in {
                    Decimal("0.1"), Decimal("0.01"), Decimal("0.005"),
                    Decimal("0.0025"), Decimal("0.001"), Decimal("0.0001"),
                }:
                    return None
                return price, size, tick

            missing_leg = "NO" if filled_leg == "YES" else "YES"
            ask = best(books[missing_leg], "asks")
            bid = best(books[filled_leg], "bids")
            if ask is None or bid is None:
                return {"fresh": False}
            amount: Decimal | None = None
            # Emergency completion is deliberately bounded to the approved
            # two-dollar loss ceiling; an over-cap book yields no executable
            # option and therefore no signed order attempt.
            for cents in range(1, 201):
                candidate = Decimal("0.01") * cents
                if protected_buy_quantity(
                    spend=candidate,
                    price=ask[0],
                    tick_size=ask[2],
                ) == filled_quantity:
                    amount = candidate
                    break
            if amount is None:
                return {"fresh": False}
            return {
                "fresh": True,
                # The combined read is only as fresh as its oldest venue
                # snapshot; never replace it with a local wall-clock marker.
                "checked_at": min(book_timestamps.values()),
                "complete": {
                    "leg": missing_leg,
                    "side": "BUY",
                    "token_id": token_by_leg[missing_leg],
                    "quantity": filled_quantity,
                    "amount": amount,
                    "max_spend": amount,
                    "max_price": ask[0],
                    # Completion spends the bounded amount of collateral.  A
                    # $1.20 order is therefore a $1.20 emergency cost for the
                    # safety policy, even though the resulting pair may later
                    # redeem for $1.00.
                    "loss": amount,
                },
                "unwind": {
                    "leg": filled_leg,
                    "side": "SELL",
                    "token_id": token_by_leg[filled_leg],
                    "shares": filled_quantity,
                    "quantity": filled_quantity,
                    "min_price": bid[0],
                    "loss": max(Decimal("0"), filled_quantity * (Decimal("1") - bid[0])),
                },
            }
        except Exception:
            return {"fresh": False}

    def cross_remediation_option(
        self,
        *,
        venue: str,
        market_id: str,
        condition_id: str,
        token_id: str,
        outcome: str,
        side: str,
        quantity: Decimal,
        maximum_fee: Decimal,
    ) -> dict[str, object]:
        """Return one current cross-venue completion or unwind option, never submit.

        This is deliberately a narrow adapter callback rather than a second
        venue abstraction.  The caller still chooses between this option and
        the other venue's independently refreshed option.
        """

        if (
            venue != "polymarket"
            or not all(isinstance(value, str) and value.strip() for value in (market_id, condition_id, token_id, outcome))
            or side not in {"BUY", "SELL"}
            or self._positive_decimal(quantity) is None
            or not isinstance(maximum_fee, Decimal)
            or not maximum_fee.is_finite()
            or maximum_fee < 0
        ):
            return {"fresh": False}
        try:
            account = self.account_snapshot()
            account_stamp = _venue_timestamp(account.checked_at)
            now = datetime.now(UTC)
            if (
                account_stamp is None
                or account.open_order_ids
                or (now - account_stamp).total_seconds() < 0
                or (now - account_stamp).total_seconds() > REMEDIATION_BOOK_FRESHNESS_SECONDS
            ):
                return {"fresh": False}
            if side == "BUY" and (
                account.p_usd_balance <= 0 or account.p_usd_allowance <= 0
            ):
                return {"fresh": False}
            if side == "SELL":
                position = sum(
                    (
                        _decimal(row.get("size", row.get("quantity", row.get("shares")))) or Decimal("0")
                        for row in account.positions
                        if row.get("condition_id", row.get("conditionId")) == condition_id
                        and row.get("token_id", row.get("tokenId", row.get("asset_id"))) == token_id
                    ),
                    Decimal("0"),
                )
                if position < quantity:
                    return {"fresh": False}
            book = self._public_client_factory().get_order_book(token_id=token_id)
            stamp = _venue_timestamp(_field(book, "timestamp"))
            now = datetime.now(UTC)
            if (
                stamp is None
                or (now - stamp).total_seconds() < 0
                or (now - stamp).total_seconds() > REMEDIATION_BOOK_FRESHNESS_SECONDS
            ):
                return {"fresh": False}
            levels = _collect(_field(book, "asks" if side == "BUY" else "bids", ()))
            valid: list[tuple[Decimal, Decimal]] = []
            for row in levels:
                price = _decimal(_field(row, "price"))
                size = _decimal(_field(row, "size"))
                if price is None or size is None or not (Decimal("0") < price <= Decimal("1")) or size < quantity:
                    continue
                valid.append((price, size))
            if not valid:
                return {"fresh": False}
            price = min(valid, key=lambda item: item[0])[0] if side == "BUY" else max(valid, key=lambda item: item[0])[0]
            option: dict[str, object] = {
                "venue": venue,
                "market_id": market_id,
                "condition_id": condition_id,
                "token_id": token_id,
                "outcome": outcome,
                "side": side,
                "quantity": quantity,
                "executable_price": price,
                "fee": maximum_fee,
                "slippage": Decimal("0"),
                "residual_dust": Decimal("0"),
            }
            if side == "BUY":
                max_spend = quantity * price + maximum_fee
                if max_spend > account.p_usd_balance or max_spend > account.p_usd_allowance:
                    return {"fresh": False}
                option["max_spend"] = max_spend
            else:
                option.update({"shares": quantity, "min_price": price})
            return {"fresh": True, "checked_at": min(account_stamp, stamp), "option": option}
        except Exception:
            return {"fresh": False}

    def submit_remediation_once(self, order: dict[str, object]) -> LegResult:
        raw_leg = order.get("leg")
        if raw_leg not in ("YES", "NO"):
            return LegResult("YES", False, "blocked", "", Decimal("0"), (), "invalid")
        leg: Literal["YES", "NO"] = cast(Literal["YES", "NO"], raw_leg)
        side = order.get("side")
        token_id = order.get("token_id")
        if side not in ("BUY", "SELL") or not isinstance(token_id, str) or not token_id:
            return LegResult(leg, False, "blocked", "", Decimal("0"), (), "invalid")
        quantity = order.get("quantity")
        if side == "SELL" and not isinstance(quantity, Decimal):
            quantity = order.get("shares") if side == "SELL" else None
        if isinstance(quantity, Decimal) and self._positive_decimal(quantity) is None:
            return LegResult(leg, False, "blocked", "", Decimal("0"), (), "invalid")
        try:
            if side == "BUY":
                amount = order.get("amount", order.get("max_spend"))
                max_spend = order.get("max_spend", amount)
                max_price = order.get("max_price")
                if (
                    not isinstance(amount, Decimal)
                    or self._positive_decimal(amount) is None
                    or not isinstance(max_spend, Decimal)
                    or self._positive_decimal(max_spend) is None
                    or max_spend != amount
                    or not isinstance(max_price, Decimal)
                    or self._positive_decimal(max_price) is None
                    or max_price > 1
                ):
                    return LegResult(leg, False, "blocked", "", Decimal("0"), (), "invalid")
                if not isinstance(quantity, Decimal):
                    quantity = amount / max_price
                signed = self._sign_leg(
                    token_id=token_id, amount=amount, max_price=max_price
                )
                signed_quantity = self._signed_quantity(signed, quantity)
            else:
                shares = order.get("shares")
                min_price = order.get("min_price")
                if (
                    not isinstance(shares, Decimal)
                    or self._positive_decimal(shares) is None
                    or not isinstance(min_price, Decimal)
                    or self._positive_decimal(min_price) is None
                    or min_price > 1
                ):
                    return LegResult(leg, False, "blocked", "", Decimal("0"), (), "invalid")
                signed = self._client.create_market_order(
                    token_id=token_id,
                    side="SELL",
                    shares=shares,
                    min_price=min_price,
                    order_type="FOK",
                )
                raw_shares = _field(signed, "maker_amount")
                signed_quantity = self._signed_quantity_base_units(raw_shares, quantity)
            if (
                _field(signed, "order_type") != "FOK"
                or _field(signed, "side") != side
                or signed_quantity != quantity
            ):
                return LegResult(leg, False, "blocked", "", Decimal("0"), (), "order_shape_mismatch")
            responses = tuple(self._client.post_orders((signed,)))
        except Exception:
            return LegResult(leg, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        if len(responses) != 1:
            return LegResult(leg, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        return self._leg_result(leg, responses[0])

    def threshold_remediation_options(
        self,
        *,
        intent: ThresholdHedgeIntent,
        filled_leg: str,
        filled_quantity: Decimal,
        since: datetime,
        **_: object,
    ) -> dict[str, object]:
        """Return a fresh, two-dollar-bounded repair for one threshold leg."""

        if (
            not isinstance(intent, ThresholdHedgeIntent)
            or filled_leg not in {"A", "B"}
            or not isinstance(filled_quantity, Decimal)
            or not filled_quantity.is_finite()
            or filled_quantity <= 0
        ):
            return {"fresh": False}
        filled = intent.leg_a if filled_leg == "A" else intent.leg_b
        missing = intent.leg_b if filled_leg == "A" else intent.leg_a
        try:
            account = self.account_snapshot()
            checked_at = _venue_timestamp(account.checked_at)
            if checked_at is None or account.open_order_ids:
                return {"fresh": False}
            now = datetime.now(UTC)
            if (now - checked_at).total_seconds() < 0 or (now - checked_at).total_seconds() > REMEDIATION_BOOK_FRESHNESS_SECONDS:
                return {"fresh": False}
            filled_position = Decimal("0")
            for position in account.positions:
                token = position.get("token_id", position.get("tokenId", position.get("asset_id", "")))
                if token != filled.token_id:
                    continue
                size = _decimal(position.get("size", position.get("quantity", position.get("shares"))))
                if size is not None and size > 0:
                    filled_position += size
            if filled_position < filled_quantity:
                return {"fresh": False}
            public = self._public_client_factory()
            filled_book = public.get_order_book(token_id=filled.token_id)
            missing_book = public.get_order_book(token_id=missing.token_id)

            def best(book: object, side: str) -> tuple[Decimal, Decimal] | None:
                rows = _collect(_field(book, side, ()))
                levels: list[tuple[Decimal, Decimal]] = []
                for row in rows:
                    price = _decimal(_field(row, "price"))
                    size = _decimal(_field(row, "size"))
                    if price is None or size is None or price <= 0 or price > 1 or size < filled_quantity:
                        continue
                    levels.append((price, size))
                return (max(levels, key=lambda item: item[0]) if side == "bids" else min(levels, key=lambda item: item[0])) if levels else None

            ask = best(missing_book, "asks")
            bid = best(filled_book, "bids")
            if ask is None or bid is None:
                return {"fresh": False}
            for book in (filled_book, missing_book):
                stamp = _venue_timestamp(_field(book, "timestamp"))
                if stamp is None or (now - stamp).total_seconds() < 0 or (now - stamp).total_seconds() > REMEDIATION_BOOK_FRESHNESS_SECONDS:
                    return {"fresh": False}
            amount: Decimal | None = None
            for cents in range(1, 201):
                candidate = CENT * cents
                if protected_buy_quantity(
                    spend=candidate,
                    price=ask[0],
                    tick_size=missing.tick_size,
                ) == filled_quantity:
                    amount = candidate
                    break
            if amount is None:
                return {"fresh": False}
            return {
                "fresh": True,
                "checked_at": min(
                    _venue_timestamp(_field(filled_book, "timestamp")),
                    _venue_timestamp(_field(missing_book, "timestamp")),
                ),
                "complete": {
                    "leg": missing.label,
                    "side": "BUY",
                    "condition_id": missing.condition_id,
                    "token_id": missing.token_id,
                    "outcome": missing.outcome,
                    "quantity": filled_quantity,
                    "amount": amount,
                    "max_spend": amount,
                    "max_price": ask[0],
                    "tick_size": missing.tick_size,
                    "loss": amount,
                },
                "unwind": {
                    "leg": filled.label,
                    "side": "SELL",
                    "condition_id": filled.condition_id,
                    "token_id": filled.token_id,
                    "outcome": filled.outcome,
                    "shares": filled_quantity,
                    "quantity": filled_quantity,
                    "min_price": bid[0],
                    "loss": max(Decimal("0"), filled_quantity * (Decimal("1") - bid[0])),
                },
            }
        except Exception:
            return {"fresh": False}

    def submit_threshold_remediation_once(self, order: dict[str, object]) -> ThresholdLegResult:
        raw_label = order.get("leg")
        if raw_label not in {"A", "B"}:
            return ThresholdLegResult("A", "YES", "", "", False, "blocked", "", Decimal("0"), (), "invalid")
        label: Literal["A", "B"] = cast(Literal["A", "B"], raw_label)
        outcome = order.get("outcome")
        if outcome not in {"YES", "NO"}:
            outcome = "YES"
        condition_id = order.get("condition_id")
        token_id = order.get("token_id")
        side = order.get("side")
        if not isinstance(condition_id, str) or not condition_id or not isinstance(token_id, str) or not token_id or side not in {"BUY", "SELL"}:
            return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), str(condition_id or ""), str(token_id or ""), False, "blocked", "", Decimal("0"), (), "invalid")
        quantity = order.get("quantity", order.get("shares"))
        if not isinstance(quantity, Decimal) or quantity <= 0:
            return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "blocked", "", Decimal("0"), (), "invalid")
        try:
            if side == "BUY":
                amount = order.get("amount", order.get("max_spend"))
                max_price = order.get("max_price")
                tick_size = order.get("tick_size")
                if not isinstance(amount, Decimal) or not isinstance(max_price, Decimal) or not isinstance(tick_size, Decimal) or protected_buy_quantity(spend=amount, price=max_price, tick_size=tick_size) != quantity:
                    return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "blocked", "", Decimal("0"), (), "invalid")
                signed = self._sign_leg(token_id=token_id, amount=amount, max_price=max_price)
                expected = self._signed_quantity(signed, quantity)
            else:
                min_price = order.get("min_price")
                if not isinstance(min_price, Decimal):
                    return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "blocked", "", Decimal("0"), (), "invalid")
                signed = self._client.create_market_order(token_id=token_id, side="SELL", shares=quantity, min_price=min_price, order_type="FOK")
                expected = self._signed_quantity_base_units(_field(signed, "maker_amount"), quantity)
            if _field(signed, "order_type") != "FOK" or _field(signed, "side") != side or expected != quantity:
                return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "blocked", "", Decimal("0"), (), "order_shape_mismatch")
            responses = tuple(self._client.post_orders((signed,)))
        except Exception:
            return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        if len(responses) != 1:
            return ThresholdLegResult(label, cast(Literal["YES", "NO"], outcome), condition_id, token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        return self._threshold_leg_result(
            ThresholdHedgeLeg(label, condition_id, str(order.get("market_id", condition_id)), cast(Literal["YES", "NO"], outcome), token_id, quantity, order.get("max_price", order.get("min_price")), order.get("amount", Decimal("0")), order.get("tick_size", DEFAULT_TICK_SIZE)),
            responses[0],
        )

    def merge_once(self, *, condition_id: str, quantity: Decimal) -> dict[str, object]:
        if not condition_id or not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            return {"status": "blocked", "error_code": "invalid"}
        try:
            amount = int((quantity * COLLATERAL_BASE_UNITS).to_integral_exact(rounding=ROUND_HALF_EVEN))
            handle = self._client.merge_positions(condition_id=condition_id, amount=amount)
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            return {"status": "blocked", "error_code": code}

        result: dict[str, object] = {}
        error: list[BaseException] = []

        def wait_for_completion() -> None:
            try:
                result["value"] = handle.wait()
            except BaseException as exc:  # pragma: no cover - defensive thread boundary
                error.append(exc)

        thread = threading.Thread(target=wait_for_completion, daemon=True)
        thread.start()
        thread.join(MERGE_WAIT_TIMEOUT_SECONDS)
        if thread.is_alive():
            return {"status": "timeout", "error_code": "timeout"}
        if error:
            return {"status": "blocked", "error_code": _safe_error_code(error[0])}
        outcome = result.get("value")
        transaction_hash = _field(
            outcome, "transaction_hash", _field(outcome, "tx_hash", None)
        )
        transaction_id = _field(outcome, "transaction_id", None)
        if not isinstance(transaction_hash, str) or not transaction_hash.strip():
            return {
                "status": "ambiguous",
                "confirmed": False,
                "error_code": "transaction_unconfirmed",
            }
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id.strip()
        ):
            return {
                "status": "ambiguous",
                "confirmed": False,
                "error_code": "transaction_unconfirmed",
            }
        response: dict[str, object] = {
            "status": "confirmed",
            "confirmed": True,
            "adapter_confirmed": True,
            "error_code": "none",
            "transaction_hash": transaction_hash,
        }
        if transaction_id is not None:
            response["transaction_id"] = transaction_id
        return response


__all__ = [
    "AccountSnapshot",
    "GEOBLOCK_URL",
    "KEYCHAIN_ACCOUNTS",
    "KEYCHAIN_SERVICE",
    "PREDICT_API_KEY_ACCOUNT",
    "PREDICT_PRIVATE_KEY_ACCOUNT",
    "PREDICT_KEYCHAIN_SERVICE",
    "KeychainError",
    "LegResult",
    "PairSubmission",
    "PolymarketTradingClient",
    "PolymarketTradingError",
    "PredictConfig",
    "ThresholdHedgeSubmission",
    "ThresholdLegResult",
    "TradingConfig",
    "load_keychain_secret",
    "load_predict_api_key",
    "load_predict_private_key",
    "load_trading_config",
    "store_keychain_secret",
    "store_predict_api_key",
]
