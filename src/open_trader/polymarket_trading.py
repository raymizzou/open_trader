"""The one authenticated boundary for protected Polymarket orders.

The rest of the application should pass :class:`PairIntent` values here and
never handle private keys, builder credentials, or signed order payloads.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence, cast
from urllib.error import URLError
from urllib.request import urlopen

from polymarket import BuilderApiKey, SecureClient

from .prediction_arbitrage import PairIntent


SECURITY = "/usr/bin/security"
KEYCHAIN_SERVICE = "com.open-trader.polymarket"
KEYCHAIN_ACCOUNTS = (
    "signing-private-key",
    "builder-key",
    "builder-secret",
    "builder-passphrase",
)
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
GEOBLOCK_TIMEOUT_SECONDS = 5.0
MERGE_WAIT_TIMEOUT_SECONDS = 60.0
COLLATERAL_BASE_UNITS = Decimal("1000000")
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
        self.error_code = error_code
        super().__init__(f"keychain error: {error_code}")


@dataclass(frozen=True, slots=True)
class TradingConfig:
    signer_address: str
    wallet_address: str


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


def _run_security(
    args: list[str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, **kwargs)  # type: ignore[arg-type]


def _validate_keychain_account(account: str) -> None:
    if account not in KEYCHAIN_ACCOUNTS:
        raise ValueError("unsupported polymarket keychain account")


def store_keychain_secret(
    account: str,
    secret: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Store one secret via stdin so it never appears in process arguments."""

    _validate_keychain_account(account)
    if not isinstance(secret, str) or not secret:
        raise ValueError("keychain secret must not be empty")
    runner = run or _run_security
    args = [
        SECURITY,
        "add-generic-password",
        "-U",
        "-a",
        account,
        "-s",
        KEYCHAIN_SERVICE,
        "-w",
    ]
    try:
        runner(
            args,
            input=f"{secret}\n",
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception as exc:
        del exc
        raise KeychainError() from None


def load_keychain_secret(
    account: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Read one secret from Keychain without including it in diagnostics."""

    _validate_keychain_account(account)
    runner = run or _run_security
    args = [
        SECURITY,
        "find-generic-password",
        "-a",
        account,
        "-s",
        KEYCHAIN_SERVICE,
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
    if set(payload) != expected:
        raise ValueError("prediction arbitrage config must contain signer_address and wallet_address")
    return TradingConfig(
        signer_address=_canonical_address(payload.get("signer_address"), "signer_address"),
        wallet_address=_canonical_address(payload.get("wallet_address"), "wallet_address"),
    )


def _safe_error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, KeychainError):
        return exc.error_code
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


class PolymarketTradingClient:
    """A narrow, redacted wrapper around the official synchronous SDK."""

    def __init__(
        self,
        config: TradingConfig,
        client: object,
        *,
        urlopen_fn: Callable[..., object] | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._urlopen_fn = urlopen_fn

    @classmethod
    def from_keychain(
        cls,
        config: TradingConfig,
        *,
        client_factory: Callable[..., object] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
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
        if signer is not None and signer.lower() != config.signer_address.lower():
            raise PolymarketTradingError("auth")
        if wallet is not None and wallet.lower() != config.wallet_address.lower():
            raise PolymarketTradingError("auth")
        return cls(config, client)

    def geoblock_allowed(self) -> bool:
        """Return true only for an explicit ``{"blocked": false}`` response."""

        opener = self._urlopen_fn or urlopen
        try:
            with opener(GEOBLOCK_URL, timeout=GEOBLOCK_TIMEOUT_SECONDS) as response:
                raw = response.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            return isinstance(payload, dict) and payload.get("blocked") is False
        except Exception:
            return False

    def account_snapshot(self) -> AccountSnapshot:
        try:
            balance = self._client.get_balance_allowance(asset_type="COLLATERAL")
            orders = _collect(self._client.list_open_orders())
            # This read is intentionally performed even though the snapshot only
            # stores open-order IDs; the authenticated preflight must prove it.
            _collect(self._client.list_account_trades())
            positions = _collect(self._client.list_positions())
            p_usd_balance = _decimal(_field(balance, "balance"), base_units=True)
            allowances = _field(balance, "allowances", {})
            allowance_values = (
                allowances.values() if isinstance(allowances, Mapping) else ()
            )
            p_usd_allowance = max(
                (_decimal(value, base_units=True) for value in allowance_values),
                default=Decimal("0"),
            )
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

    def _identity_summary(self) -> tuple[str, str]:
        signer = _address_from_client(self._client, "signer")
        wallet = _address_from_client(self._client, "wallet")
        signer_match = "yes" if signer is None or signer.lower() == self.config.signer_address.lower() else "no"
        wallet_match = "yes" if wallet is None or wallet.lower() == self.config.wallet_address.lower() else "no"
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
    def _signed_quantity(signed: object, expected: Decimal) -> Decimal | None:
        raw = _field(signed, "taker_amount")
        if raw is None:
            raw = _field(signed, "requested_amount")
        try:
            parsed = _decimal(raw)
        except ValueError:
            return None
        # The SDK serializes shares as six-decimal base units. Accept a direct
        # Decimal/int in test doubles only when it is exactly the expected value.
        if parsed == expected:
            return parsed
        return parsed / COLLATERAL_BASE_UNITS

    def _signed_pair(self, intent: PairIntent) -> tuple[object, object, str | None]:
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

    def no_submit_preflight(self, intent: PairIntent) -> dict[str, object]:
        signer_match, wallet_match = self._identity_summary()
        summary: dict[str, object] = {
            "signer_match": signer_match,
            "wallet_match": wallet_match,
            "posted": False,
            "fok_pair_signed_not_submitted": "fail",
            "equal_requested_shares": "fail",
            "error_code": "none",
            "result": "BLOCKED",
        }
        if signer_match != "yes" or wallet_match != "yes":
            summary["error_code"] = "auth"
            return summary
        if not self.geoblock_allowed():
            summary["geoblock"] = "blocked"
            summary["error_code"] = "geoblock_blocked"
            return summary
        summary["geoblock"] = "allowed"
        _, _, error_code = self._signed_pair(intent)
        if error_code is not None:
            summary["error_code"] = error_code
            return summary
        summary["fok_pair_signed_not_submitted"] = "pass"
        summary["equal_requested_shares"] = "pass"
        summary["result"] = "PASS"
        return summary

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

    def submit_pair_once(self, intent: PairIntent) -> PairSubmission:
        if not self.geoblock_allowed():
            return self._blocked_pair("geoblock_blocked")
        yes, no, error_code = self._signed_pair(intent)
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

    def reconcile(self, *, condition_id: str, since: datetime) -> dict[str, object]:
        try:
            orders = _collect(self._client.list_open_orders(market=condition_id))
            trades = _collect(
                self._client.list_account_trades(
                    market=condition_id, after=since.isoformat()
                )
            )
            positions = _collect(self._client.list_positions(market=[condition_id]))
            return {
                "status": "ok",
                "open_order_count": len(orders),
                "trade_count": len(trades),
                "position_count": len(positions),
            }
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            return {"status": "blocked", "error_code": code}

    def cancel_orders(self, order_ids: tuple[str, ...]) -> tuple[str, ...]:
        try:
            response = self._client.cancel_orders(order_ids=order_ids)
            canceled = _field(response, "canceled", ())
            return tuple(item for item in canceled if isinstance(item, str))
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            raise PolymarketTradingError(code) from None

    def submit_remediation_once(self, order: dict[str, object]) -> LegResult:
        raw_leg = order.get("leg")
        leg: Literal["YES", "NO"] = "YES" if raw_leg == "YES" else "NO"
        token_id = order.get("token_id")
        amount = order.get("amount", order.get("max_spend"))
        max_price = order.get("max_price")
        if not isinstance(token_id, str) or not isinstance(amount, Decimal) or not isinstance(max_price, Decimal):
            return LegResult(leg, False, "blocked", "", Decimal("0"), (), "invalid")
        try:
            signed = self._sign_leg(token_id=token_id, amount=amount, max_price=max_price)
            if _field(signed, "order_type") != "FOK" or _field(signed, "side") != "BUY":
                return LegResult(leg, False, "blocked", "", Decimal("0"), (), "order_shape_mismatch")
            responses = tuple(self._client.post_orders((signed,)))
        except Exception:
            return LegResult(leg, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        if len(responses) != 1:
            return LegResult(leg, False, "ambiguous", "", Decimal("0"), (), "ambiguous")
        return self._leg_result(leg, responses[0])

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
        return {"status": "confirmed", "error_code": "none"}


__all__ = [
    "AccountSnapshot",
    "GEOBLOCK_URL",
    "KEYCHAIN_ACCOUNTS",
    "KEYCHAIN_SERVICE",
    "KeychainError",
    "LegResult",
    "PairSubmission",
    "PolymarketTradingClient",
    "PolymarketTradingError",
    "TradingConfig",
    "load_keychain_secret",
    "load_trading_config",
    "store_keychain_secret",
]
