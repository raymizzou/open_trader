"""The one authenticated boundary for protected Polymarket orders.

The rest of the application should pass :class:`PairIntent` values here and
never handle private keys, builder credentials, or signed order payloads.
"""

from __future__ import annotations

import json
import importlib.metadata
import logging
import os
import pty
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import UTC, date as Date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable, Literal, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from polymarket import BuilderApiKey, PRODUCTION, PublicClient, SecureClient
from polymarket._internal.wallet import signature_type_for

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
logger = logging.getLogger(__name__)
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
LP_PRICE_HISTORY_ENDPOINT = "https://clob.polymarket.com/batch-prices-history"
LP_PRICE_HISTORY_BATCH_SIZE = 20
LP_PRICE_HISTORY_MAX_CONCURRENCY = 4
LP_PRICE_HISTORY_TIMEOUT_SECONDS = 20.0
LP_REWARD_SELECTED_MAX_CONCURRENCY = 4
# Issue #137: the ~17k-market reward catalog must not be re-read and fully
# re-validated from gamma on every lp_market_metadata call.  Results live in
# an in-process TTL cache (positive and confirmed-missing entries) with a
# per-call refresh budget, optionally warm-started from a SQLite backing
# store.
LP_METADATA_CACHE_TTL_SECONDS = 43200.0
LP_METADATA_CACHE_JITTER_SECONDS = 0.0
LP_METADATA_NEGATIVE_TTL_SECONDS = 3600.0
LP_METADATA_MAX_REFRESH_IDS_PER_CALL = 1500
LP_REWARD_ASSET_USD_ADDRESSES = frozenset(
    {
        # Both contracts are identified by the official Polymarket contracts
        # and pUSD migration docs; arbitrary reward assets remain UNKNOWN.
        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    }
)
LP_REWARD_ASSET_LABELS = {
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
}
COLLATERAL_BASE_UNITS = Decimal("1000000")
DEFAULT_TICK_SIZE = Decimal("0.01")
CENT = Decimal("0.01")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_LP_DIAGNOSTIC_SUMMARY_MAX_CHARS = 512
_LP_DIAGNOSTIC_LOG_MAX_CHARS = 2048
_LP_DIAGNOSTIC_FIELD_MAX_CHARS = 96
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


class _RewardReadCancelled(RuntimeError):
    """Cooperative stop requested between bounded reward reads."""


#: Issue #64: the N-leg unit scale (units per $1.00) shared with the #117
#: economics pipeline; FOK bounds and prices convert through this divisor.
_NLEG_UNITS_PER_DOLLAR = 1_000_000


def _interpret_fok_response(response: object) -> dict[str, object]:
    """Map one FOK BUY response to a conservative terminal outcome.

    Only an explicit positive acknowledgement (``success``/``status``
    indicating the FOK matched) books a FILLED leg, at the conservative
    ``max_cost_units`` bound the caller supplied. An explicit failure books a
    clean REJECTED (FOK either fills fully or not at all, nothing rests).
    Everything else — model shapes this adapter does not recognize — is
    UNKNOWN so the driver opens an incident instead of guessing.
    """
    success = _field(response, "success", None)
    status = _field(response, "status", None)
    if success is None and isinstance(response, Mapping):
        success = response.get("success")
    if status is None and isinstance(response, Mapping):
        status = response.get("status")
    status_text = str(status or "").strip().upper()
    if success is True or status_text in {"MATCHED", "FILLED", "MATCHED_CONFIRMED"}:
        return {"state": "FILLED", "error_code": None}
    if success is False or status_text in {
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "UNFILLED",
        "NOT_PLACED",
        "FAILED",
    }:
        return {"state": "REJECTED", "error_code": None}
    return {"state": "UNKNOWN", "error_code": "unrecognized_receipt"}


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


def _safe_read_failure(stage: str, exc: BaseException) -> str:
    """Identify a failed read without retaining exception details."""

    error_types: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    status: int | None = None
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        error_types.append(type(current).__name__)
        if status is None and type(getattr(current, "status", None)) is int:
            status = cast(int, getattr(current, "status"))
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause if isinstance(cause, BaseException) else None
    status_detail = f" status={status}" if status is not None else ""
    logger.warning(
        "lp_metadata_read_failed stage=%s error_types=%s%s",
        stage,
        ">".join(error_types) or "unknown",
        status_detail,
    )
    return f"{stage}_read_{type(exc).__name__}"


def _redact_lp_diagnostic(value: object, *, limit: int) -> str:
    text = str(value)
    text = re.sub(
        r"(?im)\b(?:authorization|proxy-authorization)\s*[:=]\s*[^\r\n]*",
        "[redacted-header]",
        text,
    )
    text = re.sub(
        r"(?im)\b(?:cookie|set-cookie)\s*[:=]\s*[^\r\n]*",
        "[redacted-header]",
        text,
    )
    text = " ".join(text.split())
    text = re.sub(r"(?i)https?://[^\s<>\"']+", "[redacted-url]", text)
    text = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\s,;]+",
        "[redacted-header]",
        text,
    )
    text = re.sub(
        r"(?i)(?:[?&](?:token|access_token|api[_-]?key|secret|auth|cookie)=[^&\s]+)",
        "[redacted-query]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:wallet|token|condition|market|asset|address)(?:[_-]?id)?\s*[:=]\s*[^\s,;]+",
        "[redacted-id]",
        text,
    )
    text = re.sub(r"\b0x[0-9a-fA-F]{40,64}\b", "[redacted-id]", text)
    text = re.sub(r"\b\d{40,}\b", "[redacted-id]", text)
    text = re.sub(
        r"(?i)\b(?:[a-z0-9]+[-_])*(?:token|secret|cookie|password|credential)[-_][a-z0-9._~-]+",
        "[redacted-secret]",
        text,
    )
    return text[:limit]


def _lp_response_error_summary(response: object) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, Mapping):
        field_names = ("error", "message", "detail", "code")

        def public_values(value: object, depth: int = 0) -> tuple[str, ...]:
            if depth > 3:
                return ()
            if isinstance(value, Mapping):
                return tuple(
                    fragment
                    for name in field_names
                    if name in value
                    for fragment in public_values(value[name], depth + 1)
                )
            if isinstance(value, (str, int, float, bool)):
                return (str(value),)
            return ()

        raw = " ".join(public_values(payload))
    elif isinstance(payload, str):
        raw = payload
    else:
        try:
            raw = response.text
        except Exception:
            return "[unavailable]"
    return _redact_lp_diagnostic(raw, limit=_LP_DIAGNOSTIC_SUMMARY_MAX_CHARS)


def _install_lp_metadata_response_hook(
    public: object, requested_count: int
) -> Callable[[], None] | None:
    try:
        gamma = getattr(getattr(public, "_ctx"), "gamma")
        http_client = getattr(gamma, "_client")
        response_hooks = getattr(http_client, "event_hooks").get("response")
    except Exception:
        return None
    if not isinstance(response_hooks, list):
        return None

    def on_response(response: object) -> None:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or 200 <= status < 300:
            return
        request = getattr(response, "request", None)
        url = getattr(request, "url", None)
        if getattr(url, "path", None) != "/markets/keyset":
            return
        # httpx runs response hooks before Client.send reads the body.  Keep
        # this read outside the best-effort diagnostic block so a real stream
        # error remains the original request error.
        response.read()
        try:
            params = getattr(url, "params", None)
            condition_ids = (
                params.get_list("condition_ids")
                if callable(getattr(params, "get_list", None))
                else ()
            )
            page = (
                "continuation"
                if params is not None and params.get("after_cursor") is not None
                else "first"
            )
            url_bytes = len(str(url).encode("utf-8"))
            headers = getattr(response, "headers")
            fields = {
                "content_type": headers.get("content-type", "-"),
                "server": headers.get("server", "-"),
                "cf_ray": headers.get("cf-ray", "-"),
                "retry_after": headers.get("retry-after", "-"),
            }
            safe_fields = {
                name: _redact_lp_diagnostic(
                    value, limit=_LP_DIAGNOSTIC_FIELD_MAX_CHARS
                )
                for name, value in fields.items()
            }
            requested = len(condition_ids) or requested_count
            message = (
                "lp_metadata_http_failure "
                f"status={status} page={page} requested_ids={requested} "
                f"url_bytes={url_bytes} "
                f"content_type={safe_fields['content_type']} "
                f"server={safe_fields['server']} "
                f"cf_ray={safe_fields['cf_ray']} "
                f"retry_after={safe_fields['retry_after']} "
                f"summary={_lp_response_error_summary(response)}"
            )
            logger.warning("%s", message[:_LP_DIAGNOSTIC_LOG_MAX_CHARS])
        except Exception:
            return

    response_hooks.append(on_response)

    def remove() -> None:
        try:
            for index, hook in enumerate(response_hooks):
                if hook is on_response:
                    del response_hooks[index]
                    break
        except Exception:
            return

    return remove


def _submit_error_detail(exc: BaseException) -> dict[str, str]:
    """Redacted observable facts about a submit exception; never credentials."""

    message = " ".join(str(exc).split())
    if len(message) > 500:
        message = message[:500] + "…"
    return {
        "error_code": _safe_error_code(exc),
        "error_type": type(exc).__name__,
        "message": message,
    }


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


def _collect_lp_market_pages(
    value: object, requested: set[str]
) -> tuple[object, ...]:
    first_page = getattr(value, "first_page", None)
    from_cursor = getattr(value, "from_cursor", None)
    if not callable(first_page) or not callable(from_cursor):
        page_items = _field(value, "items", None)
        if page_items is not None:
            return _collect(page_items)
        return _collect(value)

    rows: list[object] = []
    observed: set[str] = set()
    page = first_page()
    while True:
        page_rows = _collect(_field(page, "items", ()))
        rows.extend(page_rows)
        observed.update(
            condition_id
            for item in page_rows
            if (row := _model_dict(item)) is not None
            for condition_id in (row.get("condition_id", row.get("conditionId")),)
            if isinstance(condition_id, str) and condition_id in requested
        )
        if observed >= requested or not _field(page, "has_more", False):
            return tuple(rows)
        cursor = _field(page, "next_cursor")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError("market pagination cursor missing")
        page = from_cursor(cursor).first_page()


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


def _lp_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _reward_date(value: object) -> Date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return Date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_lp_reward_config(
    value: object,
    *,
    sponsored: bool,
    source: str | None = None,
    require_id: bool = True,
) -> tuple[dict[str, object], tuple[str | None, str, Date, Date]] | None:
    """Normalize one SDK reward config for both catalog read paths."""

    config = _model_dict(value)
    if config is None:
        return None
    config_id = config.get("id")
    asset_address = config.get("asset_address")
    start_date = _reward_date(config.get("start_date"))
    end_date = _reward_date(config.get("end_date"))
    rate = _lp_decimal(config.get("rate_per_day"))
    if (
        (require_id and config_id is None)
        or not isinstance(asset_address, str)
        or start_date is None
        or end_date is None
        or rate is None
        or rate < 0
    ):
        return None
    normalized = dict(config)
    normalized["rate_per_day"] = rate
    normalized["sponsored"] = sponsored
    if source is not None:
        normalized["source"] = source
    return normalized, (
        None if config_id is None else str(config_id),
        asset_address.casefold(),
        start_date,
        end_date,
    )


def _reward_usd_value(row: Mapping[str, object]) -> Decimal | None:
    asset = row.get("asset_address")
    if not isinstance(asset, str) or asset.strip().lower() not in LP_REWARD_ASSET_USD_ADDRESSES:
        return None
    earnings = _lp_decimal(row.get("earnings"))
    asset_rate = _lp_decimal(row.get("asset_rate"))
    # The official reward docs identify the supported pUSD/USDC.e assets, but
    # do not define asset_rate as a general USD FX rate.  Only a unit rate is
    # safe to treat as nominal USD; every other valuation remains UNKNOWN.
    if (
        earnings is None
        or asset_rate is None
        or earnings < 0
        or asset_rate != Decimal("1")
    ):
        return None
    return earnings


def _reward_raw_amounts(
    rows: Sequence[Mapping[str, object]],
    *,
    parsed_date: Date,
    maker: str,
    condition_id: str | None = None,
) -> dict[str, Decimal] | None:
    amounts: dict[str, Decimal] = {}
    maker_folded = maker.casefold()
    for row in rows:
        if _reward_date(row.get("date")) != parsed_date:
            return None
        row_maker = row.get("maker_address")
        if not isinstance(row_maker, str) or row_maker.casefold() != maker_folded:
            return None
        if condition_id is not None:
            row_condition = row.get("condition_id")
            if not isinstance(row_condition, str):
                return None
            if row_condition != condition_id:
                continue
        asset = row.get("asset_address")
        amount = _lp_decimal(row.get("earnings"))
        if not isinstance(asset, str) or not asset.strip() or amount is None or amount < 0:
            return None
        identity = asset.strip().casefold()
        amounts[identity] = amounts.get(identity, Decimal("0")) + amount
    return amounts


def _reward_accruals(amounts: Mapping[str, Decimal]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "amount": amount,
            "asset": LP_REWARD_ASSET_LABELS.get(address, address),
            "asset_address": address,
        }
        for address, amount in sorted(amounts.items())
    )


def _lp_level(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    price = _lp_decimal(row.get("price"))
    size = _lp_decimal(row.get("size"))
    if price is None or size is None:
        return None
    return {"price": price, "size": size}


def _lp_book(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    for field, target in (("bids", bids), ("asks", asks)):
        raw_levels = row.get(field)
        if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
            return None
        for raw_level in raw_levels:
            level = _lp_level(raw_level)
            if level is None:
                continue
            price = cast(Decimal, level["price"])
            size = cast(Decimal, level["size"])
            if price <= 0 or size <= 0:
                continue
            target[price] = target.get(price, Decimal("0")) + size
    timestamp = _venue_timestamp(row.get("timestamp"))
    return {
        "market": row.get("condition_id", row.get("market")),
        "condition_id": row.get("condition_id", row.get("market")),
        "token_id": row.get("token_id", row.get("asset_id")),
        "timestamp": timestamp,
        "source_timestamp": row.get("timestamp"),
        "bids": [
            {"price": price, "size": size}
            for price, size in sorted(bids.items())
        ],
        "asks": [
            {"price": price, "size": size}
            for price, size in sorted(asks.items())
        ],
        "min_order_size": _lp_decimal(row.get("min_order_size")),
        "tick_size": _lp_decimal(row.get("tick_size")),
        "neg_risk": row.get("neg_risk"),
        "last_trade_price": _lp_decimal(row.get("last_trade_price")),
        "hash": row.get("hash"),
    }


def _lp_order(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    order_id = row.get("id", row.get("order_id"))
    token_id = row.get("token_id", row.get("asset_id"))
    if order_id in (None, "") or token_id in (None, ""):
        return None
    original_size = _lp_decimal(row.get("original_size", row.get("size")))
    matched = _lp_decimal(row.get("size_matched", row.get("matched_amount")))
    if original_size is None:
        original_size = Decimal("0")
    if matched is None:
        matched = Decimal("0")
    return {
        "id": str(order_id),
        "order_id": str(order_id),
        "market": row.get("condition_id", row.get("market")),
        "condition_id": row.get("condition_id", row.get("market")),
        "market_id": row.get("market_id"),
        "market_title": row.get("market_title", row.get("title")),
        "market_url": row.get("market_url"),
        "token_id": str(token_id),
        "asset_id": str(token_id),
        "side": str(row.get("side", "")).upper(),
        "price": _lp_decimal(row.get("price")),
        "original_size": original_size,
        "size_matched": matched,
        "remaining_size": max(Decimal("0"), original_size - matched),
        "size": max(Decimal("0"), original_size - matched),
        "outcome": row.get("outcome"),
        "order_type": row.get("order_type"),
        "status": str(row.get("status", "")).upper(),
        "expiration": row.get("expiration", row.get("expires_at")),
        "created_at": _venue_timestamp(row.get("created_at")),
    }


def _lp_position(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    token_id = row.get("token_id", row.get("asset_id", row.get("asset")))
    if token_id in (None, ""):
        return None
    market_url = row.get("market_url")
    slug = row.get("slug")
    if not market_url and isinstance(slug, str) and slug.strip():
        market_url = f"https://polymarket.com/event/{slug.strip()}"
    return {
        "condition_id": row.get("condition_id", row.get("conditionId", row.get("market"))),
        "market_id": row.get("market_id"),
        "market_title": row.get("market_title", row.get("title")),
        "market_url": market_url,
        "token_id": str(token_id),
        "outcome": row.get("outcome"),
        "size": _lp_decimal(row.get("size", row.get("quantity"))),
        "average_price": _lp_decimal(row.get("average_price", row.get("avg_price"))),
        "current_value": _lp_decimal(row.get("current_value")),
    }


def _lp_maker_order(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    order_id = row.get("order_id", row.get("id"))
    token_id = row.get("token_id", row.get("asset_id"))
    if order_id in (None, "") or token_id in (None, ""):
        return None
    return {
        "order_id": str(order_id),
        "token_id": str(token_id),
        "asset_id": str(token_id),
        "maker_address": row.get("maker_address"),
        "owner": row.get("owner"),
        "side": str(row.get("side", "")).upper(),
        "price": _lp_decimal(row.get("price")),
        "matched_amount": _lp_decimal(row.get("matched_amount", row.get("size"))),
        "fee_rate_bps": _lp_decimal(row.get("fee_rate_bps")),
    }


def _lp_maker_order_is_self(row: object, wallet_address: object) -> bool:
    """True only when a maker row provably belongs to our wallet.

    Rows without a usable maker address/owner cannot be attributed and are
    treated as foreign: missing volume is safer than misattributed volume.
    """

    target = str(wallet_address or "").strip().lower()
    if not target:
        return False
    if not isinstance(row, Mapping):
        return False
    for key in ("maker_address", "owner"):
        value = str(row.get(key) or "").strip().lower()
        if value and value == target:
            return True
    return False


def _lp_trade(value: object) -> dict[str, object] | None:
    row = _model_dict(value)
    if row is None:
        return None
    trade_id = row.get("id", row.get("trade_id"))
    token_id = row.get("token_id", row.get("asset_id"))
    if trade_id in (None, "") or token_id in (None, ""):
        return None
    makers: list[dict[str, object]] = []
    raw_makers = row.get("maker_orders", ())
    if isinstance(raw_makers, Sequence) and not isinstance(raw_makers, (str, bytes)):
        for maker in raw_makers:
            normalized = _lp_maker_order(maker)
            if normalized is not None:
                makers.append(normalized)
    status = str(row.get("status", "")).upper()
    if status.startswith("TRADE_STATUS_"):
        status = status[len("TRADE_STATUS_") :]
    return {
        "id": str(trade_id),
        "trade_id": str(trade_id),
        "market": row.get("condition_id", row.get("market")),
        "condition_id": row.get("condition_id", row.get("market")),
        "token_id": str(token_id),
        "asset_id": str(token_id),
        "taker_order_id": str(row.get("taker_order_id", "")),
        "side": str(row.get("side", "")).upper(),
        "trader_side": str(row.get("trader_side", "")).upper(),
        "price": _lp_decimal(row.get("price")),
        "size": _lp_decimal(row.get("size")),
        "status": status,
        "fee_rate_bps": _lp_decimal(row.get("fee_rate_bps")),
        "maker_orders": makers,
        "matched_at": _trade_timestamp(row),
        "updated_at": _venue_timestamp(row.get("updated_at", row.get("last_update"))),
    }


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


def _reward_total_rows(payload: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, Mapping)):
        raise ValueError("reward_total_shape_unknown")
    rows: list[Mapping[str, object]] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise ValueError("reward_row_unknown")
        rows.append(row)
    return tuple(rows)


def _reward_amount(
    rows: Sequence[Mapping[str, object]],
    *,
    parsed_date: Date,
    maker: str,
    condition_id: str | None = None,
) -> Decimal | None:
    total = Decimal("0")
    maker_folded = maker.casefold()
    for row in rows:
        if _reward_date(row.get("date")) != parsed_date:
            return None
        row_maker = row.get("maker_address")
        if not isinstance(row_maker, str) or row_maker.casefold() != maker_folded:
            return None
        if condition_id is not None:
            row_condition = row.get("condition_id")
            if not isinstance(row_condition, str):
                return None
            if row_condition != condition_id:
                continue
        value = _reward_usd_value(row)
        if value is None:
            return None
        total += value
    return total


class PolymarketTradingClient:
    """A narrow, redacted wrapper around the official synchronous SDK."""

    def __init__(
        self,
        config: TradingConfig,
        client: object,
        *,
        urlopen_fn: Callable[..., object] | None = None,
        public_client_factory: Callable[[], object] | None = None,
        metadata_cache: object | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._urlopen_fn = urlopen_fn
        self._public_client_factory = public_client_factory or PublicClient
        self._metadata_cache = metadata_cache
        self._metadata_entries: dict[
            str, tuple[float, dict[str, object] | None]
        ] = {}
        self._metadata_lock = threading.Lock()
        self._metadata_warm_loaded = False
        self._metadata_last_prune_epoch = float("-inf")
        self._readiness_key: tuple[PairIntent, Decimal] | None = None
        self._threshold_readiness_key: ThresholdHedgeIntent | None = None
        self._cross_leg_readiness_key: object | None = None
        self._last_submit_error: dict[str, str] | None = None

    def attach_metadata_cache(self, cache: object | None) -> None:
        """Attach a duck-typed persistent backing store before first use."""

        with self._metadata_lock:
            self._metadata_cache = cache

    def expire_lp_metadata_cache(self) -> None:
        """Drop all cached LP market metadata so the next read re-fetches."""

        with self._metadata_lock:
            self._metadata_entries.clear()

    def last_submit_error(self) -> dict[str, str] | None:
        """Redacted detail of the most recent submit exception, if any."""

        return self._last_submit_error

    @classmethod
    def from_keychain(
        cls,
        config: TradingConfig,
        *,
        client_factory: Callable[..., object] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        public_client_factory: Callable[[], object] | None = None,
        metadata_cache: object | None = None,
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
        return cls(
            config,
            client,
            public_client_factory=public_client_factory,
            metadata_cache=metadata_cache,
        )

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

    def _account_read_facts(
        self,
    ) -> tuple[Decimal, Decimal, tuple[object, ...], tuple[object, ...], datetime]:
        try:
            p_usd_balance, p_usd_allowance = self._collateral_balance_allowance()
            orders = tuple(_collect(self._client.list_open_orders()))
            # This read is intentionally performed even though the snapshot only
            # stores open-order data; the authenticated preflight must prove it.
            _collect(self._client.list_account_trades())
            positions = tuple(_collect(self._client.list_positions()))
            return p_usd_balance, p_usd_allowance, orders, positions, datetime.now(UTC)
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            raise PolymarketTradingError(code) from None

    def account_snapshot(self) -> AccountSnapshot:
        p_usd_balance, p_usd_allowance, orders, positions, checked_at = (
            self._account_read_facts()
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
            checked_at=checked_at,
        )

    def lp_account_snapshot(self) -> dict[str, object]:
        """Return current account orders and holdings for the read-only LP panel."""

        lp_checked_at = datetime.now(UTC)
        balance, allowance, orders, positions, _account_checked_at = self._account_read_facts()
        checked_at = lp_checked_at
        order_rows: list[dict[str, object]] = []
        open_orders_complete = True
        for order in orders:
            raw = _model_dict(order)
            row = _lp_order(order)
            if raw is None or row is None:
                open_orders_complete = False
                continue
            original_size = _lp_decimal(raw.get("original_size", raw.get("size")))
            matched_size = _lp_decimal(raw.get("size_matched", raw.get("matched_amount")))
            if (
                not row.get("condition_id")
                or not row.get("token_id")
                or row.get("side") not in {"BUY", "SELL"}
                or not row.get("status")
                or _lp_decimal(raw.get("price")) is None
                or original_size is None
                or matched_size is None
                or original_size < 0
                or matched_size < 0
            ):
                open_orders_complete = False
            order_rows.append(row)

        position_rows: list[dict[str, object]] = []
        positions_complete = True
        for position in positions:
            raw = _model_dict(position)
            row = _lp_position(position)
            if raw is None or row is None:
                positions_complete = False
                continue
            size = _lp_decimal(raw.get("size", raw.get("quantity")))
            if (
                not row.get("condition_id")
                or not row.get("token_id")
                or size is None
                or size < 0
            ):
                positions_complete = False
            position_rows.append(row)
        condition_ids = tuple(
            dict.fromkeys(
                str(row.get("condition_id") or "")
                for row in (*order_rows, *position_rows)
                if row.get("condition_id")
            )
        )
        metadata = self.lp_market_metadata(condition_ids)
        for row in (*order_rows, *position_rows):
            market = metadata.get(str(row.get("condition_id") or ""))
            if market is not None:
                row.update(market)
                row["condition_id"] = market.get("condition_id")
        return {
            "authenticated": True,
            "balance": balance,
            "allowance": allowance,
            "open_orders": tuple(order_rows),
            "positions": tuple(position_rows),
            "checked_at": checked_at,
            "open_orders_complete": open_orders_complete,
            "positions_complete": positions_complete,
        }

    def lp_open_orders_snapshot(self) -> dict[str, object]:
        """Read only authenticated open-order facts for the LP share watcher."""

        checked_at = datetime.now(UTC)
        try:
            orders = tuple(_collect(self._client.list_open_orders()))
        except Exception:
            return {
                "authenticated": False,
                "open_orders": (),
                "open_orders_complete": False,
                "checked_at": checked_at,
            }
        order_rows: list[dict[str, object]] = []
        complete = True
        for order in orders:
            raw = _model_dict(order)
            row = _lp_order(order)
            if raw is None or row is None:
                complete = False
                continue
            original_size = _lp_decimal(raw.get("original_size", raw.get("size")))
            matched_size = _lp_decimal(raw.get("size_matched", raw.get("matched_amount")))
            if (
                not row.get("condition_id")
                or not row.get("token_id")
                or row.get("side") not in {"BUY", "SELL"}
                or not row.get("status")
                or _lp_decimal(raw.get("price")) is None
                or original_size is None
                or matched_size is None
                or original_size < 0
                or matched_size < 0
            ):
                complete = False
            order_rows.append(row)
        return {
            "authenticated": True,
            "wallet_address": self.config.wallet_address,
            "open_orders": tuple(order_rows),
            "open_orders_complete": complete,
            "checked_at": checked_at,
        }

    def lp_account_trades(
        self,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read per-market account trades with conservative completeness.

        One authenticated call per requested market; markets whose read fails
        are absent from ``trades`` and flip ``complete`` to False so callers
        can fail open instead of trusting a partial day.
        """

        requested = tuple(
            dict.fromkeys(
                value.strip()
                for value in condition_ids
                if isinstance(value, str) and value.strip()
            )
        )
        checked_at = datetime.now(UTC)
        if not requested or (stop_event is not None and stop_event.is_set()):
            return {
                "state": "unknown",
                "complete": False,
                "checked_at": checked_at,
                "trades": {},
            }
        trades: dict[str, tuple[dict[str, object], ...]] = {}
        complete = True
        wallet = str(getattr(self.config, "wallet_address", "") or "")

        def read_market(condition_id: str) -> tuple[object, ...]:
            if stop_event is not None and stop_event.is_set():
                return ()
            return _collect(self._client.list_account_trades(market=condition_id))

        with ThreadPoolExecutor(max_workers=min(8, len(requested))) as pool:
            futures = [
                (condition_id, pool.submit(read_market, condition_id))
                for condition_id in requested
            ]
            for condition_id, future in futures:
                try:
                    rows = future.result()
                except Exception:
                    complete = False
                    continue
                normalized: list[dict[str, object]] = []
                for row in rows:
                    trade = _lp_trade(row)
                    if trade is None:
                        complete = False
                        continue
                    # One trade row lists every maker it filled; keep only
                    # maker orders provably ours so aggregation cannot count
                    # foreign or unattributable fills as our volume.
                    makers = trade.get("maker_orders")
                    if isinstance(makers, list):
                        trade["maker_orders"] = [
                            maker
                            for maker in makers
                            if _lp_maker_order_is_self(maker, wallet)
                        ]
                    normalized.append(trade)
                trades[condition_id] = tuple(normalized)
        if stop_event is not None and stop_event.is_set():
            return {
                "state": "unknown",
                "complete": False,
                "checked_at": checked_at,
                "trades": {},
            }
        return {
            "state": "known" if complete else "unknown",
            "complete": complete,
            "checked_at": checked_at,
            "trades": trades,
        }

    def lp_market_metadata(
        self,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, dict[str, object]]:
        """Read LP market facts while retaining the mapping-only API."""

        requested = self._normalise_condition_ids(condition_ids)
        if not requested or (stop_event is not None and stop_event.is_set()):
            return {}
        result = self.lp_market_metadata_batch(requested, stop_event=stop_event)
        markets = cast(dict[str, dict[str, object]], result["markets"])
        failed_ids = cast(dict[str, str], result["failed_ids"])
        market_failures = {
            condition_id
            for condition_id, reason in failed_ids.items()
            if reason.startswith("market_read_")
        }
        if market_failures:
            confirmed_absent = set(result["confirmed_absent_ids"])
            if not markets and not confirmed_absent:
                raise RuntimeError("market read failed")
        return {
            condition_id: dict(markets[condition_id])
            for condition_id in requested
            if condition_id in markets
        }

    def lp_market_metadata_batch(
        self,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read metadata with explicit success, absence, failure, and deferral."""

        requested = self._normalise_condition_ids(condition_ids)
        checked_at = datetime.now(UTC)
        if not requested:
            return {
                "markets": {},
                "confirmed_absent_ids": (),
                "failed_ids": {},
                "deferred_ids": (),
                "state": "known",
                "checked_at": checked_at,
            }
        if stop_event is not None and stop_event.is_set():
            return {
                "markets": {},
                "confirmed_absent_ids": (),
                "failed_ids": {},
                "deferred_ids": requested,
                "state": "cancelled",
                "checked_at": checked_at,
            }
        return self._lp_market_metadata_batch_result(
            requested, checked_at=checked_at, force_refresh=False, stop_event=stop_event
        )

    def lp_market_metadata_fresh(
        self,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, dict[str, object]]:
        """Read metadata directly, without serving a stale cached payload."""

        requested = self._normalise_condition_ids(condition_ids)
        if not requested or (stop_event is not None and stop_event.is_set()):
            return {}
        checked_at = datetime.now(UTC)
        result = self._lp_market_metadata_batch_result(
            requested, checked_at=checked_at, force_refresh=True, stop_event=stop_event
        )
        markets = cast(dict[str, dict[str, object]], result["markets"])
        failed_ids = cast(dict[str, str], result["failed_ids"])
        return {
            condition_id: dict(value)
            for condition_id, value in markets.items()
            if condition_id not in failed_ids
        }

    @staticmethod
    def _normalise_condition_ids(condition_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value.strip()
                for value in condition_ids
                if isinstance(value, str) and value.strip()
            )
        )

    def _lp_market_metadata_batch_result(
        self,
        requested: tuple[str, ...],
        *,
        checked_at: datetime,
        force_refresh: bool,
        stop_event: threading.Event | None,
    ) -> dict[str, object]:
        if force_refresh:
            fresh: dict[str, dict[str, object] | None] = {}
            stale: dict[str, dict[str, object] | None] = {}
            refresh_ids = list(requested[:LP_METADATA_MAX_REFRESH_IDS_PER_CALL])
            deferred_ids = list(requested[LP_METADATA_MAX_REFRESH_IDS_PER_CALL:])
            self._prune_metadata_cache(checked_at)
        else:
            fresh, stale, refresh_ids = self._partition_metadata_cache(
                requested, checked_at
            )
            deferred_ids = [
                condition_id
                for condition_id in requested
                if condition_id not in fresh
                and condition_id not in refresh_ids
            ]
            self._prune_metadata_cache(checked_at)

        markets = {
            condition_id: dict(value)
            for condition_id, value in fresh.items()
            if value is not None
        }
        markets.update(
            {
                condition_id: dict(value)
                for condition_id, value in stale.items()
                if value is not None
            }
        )
        confirmed_absent = {
            condition_id
            for condition_id, value in fresh.items()
            if value is None
        }
        failed_ids: dict[str, str] = {}
        fetched: dict[str, dict[str, object]] = {}
        fetched_absent: frozenset[str] = frozenset()
        if refresh_ids:
            public: object | None = None
            try:
                public = self._public_client_factory()
                fetched, failed_ids, fetched_absent = self._fetch_lp_market_metadata(
                    tuple(refresh_ids), public=public, stop_event=stop_event
                )
            except Exception as exc:
                reason = _safe_read_failure("market", exc)
                failed_ids.update(
                    (condition_id, reason) for condition_id in refresh_ids
                )
            finally:
                close = getattr(public, "close", None)
                if callable(close):
                    close()
            markets.update(fetched)
            confirmed_absent.update(fetched_absent)
            self._record_metadata_entries(
                tuple(refresh_ids),
                fetched,
                metadata_checked_at=checked_at,
                confirmed_absent_ids=fetched_absent,
                failed_ids=failed_ids,
            )

        accounted = set(markets) | confirmed_absent | set(failed_ids) | set(deferred_ids)
        unclassified = set(requested).difference(accounted)
        deferred_ids.extend(
            condition_id
            for condition_id in requested
            if condition_id in unclassified
        )
        deferred = tuple(dict.fromkeys(deferred_ids))
        confirmed = tuple(
            condition_id for condition_id in requested if condition_id in confirmed_absent
        )
        ordered_failed = {
            condition_id: failed_ids[condition_id]
            for condition_id in requested
            if condition_id in failed_ids
        }
        known_ids = (
            set(fresh).difference({condition_id for condition_id, value in fresh.items() if value is None})
            | set(fetched).difference(ordered_failed)
            | set(confirmed)
        )
        cancelled = stop_event is not None and stop_event.is_set()
        if cancelled:
            state = "cancelled"
        elif not ordered_failed and not deferred and len(known_ids) == len(requested):
            state = "known"
        elif known_ids:
            state = "partial"
        else:
            state = "unknown"
        return {
            "markets": {
                condition_id: markets[condition_id]
                for condition_id in requested
                if condition_id in markets
            },
            "confirmed_absent_ids": confirmed,
            "failed_ids": ordered_failed,
            "deferred_ids": deferred,
            "state": state,
            "checked_at": checked_at,
        }

    def _partition_metadata_cache(
        self,
        requested: tuple[str, ...],
        now: datetime,
    ) -> tuple[
        dict[str, dict[str, object] | None],
        dict[str, dict[str, object] | None],
        list[str],
    ]:
        """Split requested ids into fresh, stale, and refresh candidates.

        Refresh candidates are ordered oldest-expiry first (never-read ids
        first) and capped at ``LP_METADATA_MAX_REFRESH_IDS_PER_CALL``.  Ids
        over the refresh budget are served from their (expired) cached
        value when one exists; never-read ids and expired confirmed-missing
        entries stay absent.
        """

        now_epoch = now.timestamp()
        fresh: dict[str, dict[str, object] | None] = {}
        stale: dict[str, dict[str, object] | None] = {}
        refresh: list[tuple[float, int, str]] = []
        with self._metadata_lock:
            self._warm_load_metadata_cache(now)
            for index, condition_id in enumerate(requested):
                entry = self._metadata_entries.get(condition_id)
                if entry is None:
                    refresh.append((float("-inf"), index, condition_id))
                elif entry[0] > now_epoch:
                    fresh[condition_id] = entry[1]
                else:
                    refresh.append((entry[0], index, condition_id))
        refresh.sort(key=lambda item: (item[0], item[1]))
        refresh_ids = [
            condition_id for _expires_at, _index, condition_id in refresh
        ]
        for condition_id in refresh_ids:
            entry = self._metadata_entries.get(condition_id)
            if entry is not None and entry[1] is not None:
                stale[condition_id] = entry[1]
        capped = refresh_ids[:LP_METADATA_MAX_REFRESH_IDS_PER_CALL]
        return fresh, stale, capped

    def _warm_load_metadata_cache(self, now: datetime) -> None:
        """Bulk-load non-expired persisted entries once per process.

        Warm-started entries keep the persisted `expires_at` stamp as their
        in-memory expiry, so a restart never extends freshness past the
        originally persisted window (which is bounded by the risk-gate
        horizon by construction).  Backing-store failures degrade to a
        memory-only cache.
        """

        cache_store = self._metadata_cache
        if cache_store is None or self._metadata_warm_loaded:
            return
        self._metadata_warm_loaded = True
        try:
            warm = cache_store.lp_metadata_cache_entries(now=now)
        except Exception:
            logger.warning("lp_metadata_cache_warm_load_failed", exc_info=True)
            return
        if not isinstance(warm, Mapping):
            return
        for key, value in warm.items():
            if not (isinstance(key, str) and key):
                continue
            if not isinstance(value, tuple) or len(value) != 2:
                continue
            raw_expires_at, payload = value
            if isinstance(raw_expires_at, bool) or not isinstance(
                raw_expires_at, (int, float)
            ):
                continue
            if payload is not None and not isinstance(payload, dict):
                continue
            if key not in self._metadata_entries:
                self._metadata_entries[key] = (float(raw_expires_at), payload)

    def _prune_metadata_cache(self, now: datetime) -> None:
        """Prune the backing store at most once per process per hour."""

        cache_store = self._metadata_cache
        if cache_store is None:
            return
        now_epoch = now.timestamp()
        with self._metadata_lock:
            due = (
                now_epoch - self._metadata_last_prune_epoch
                >= LP_METADATA_CACHE_TTL_SECONDS
            )
            if due:
                self._metadata_last_prune_epoch = now_epoch
        if not due:
            return
        try:
            cache_store.lp_metadata_cache_prune(now=now)
        except Exception:
            logger.warning("lp_metadata_cache_prune_failed", exc_info=True)

    def _record_metadata_entries(
        self,
        refresh_ids: tuple[str, ...],
        fetched: Mapping[str, dict[str, object]],
        *,
        metadata_checked_at: datetime,
        confirmed_absent_ids: frozenset[str] = frozenset(),
        failed_ids: Mapping[str, str] | None = None,
    ) -> None:
        """Write a completed refresh into the in-memory TTL cache.

        Only successful reads reach this point: confirmed-missing ids record
        negative entries, while failed or deferred ids leave any prior cache
        entry untouched.
        """

        read_epoch = metadata_checked_at.timestamp()
        expires_at = read_epoch + LP_METADATA_CACHE_TTL_SECONDS
        negative_expires_at = read_epoch + LP_METADATA_NEGATIVE_TTL_SECONDS
        updated: dict[str, tuple[float, dict[str, object] | None]] = {}
        for condition_id in refresh_ids:
            if failed_ids is not None and condition_id in failed_ids:
                continue
            value = fetched.get(condition_id)
            if value is not None:
                updated[condition_id] = (expires_at, value)
            elif condition_id in confirmed_absent_ids:
                updated[condition_id] = (negative_expires_at, None)
        if not updated:
            return
        with self._metadata_lock:
            self._metadata_entries.update(updated)
        cache_store = self._metadata_cache
        if cache_store is not None:
            try:
                cache_store.lp_metadata_cache_store_entries(updated)
            except Exception:
                logger.warning(
                    "lp_metadata_cache_store_entries_failed", exc_info=True
                )

    def _fetch_lp_market_metadata(
        self,
        requested: tuple[str, ...],
        *,
        public: object,
        stop_event: threading.Event | None = None,
    ) -> tuple[dict[str, dict[str, object]], dict[str, str], frozenset[str]]:
        """Fetch LP market facts while preserving each completed sub-read."""

        metadata_checked_at = datetime.now(UTC)
        event_facts: dict[str, Mapping[str, object] | None] = {}
        event_failures: dict[str, str] = {}
        event_failures_lock = threading.Lock()
        rows: list[object] = []
        completed_market_ids: set[str] = set()
        failed_ids: dict[str, str] = {}

        def read_market_batch(
            batch: tuple[str, ...],
        ) -> tuple[tuple[object, ...], bool]:
            if stop_event is not None and stop_event.is_set():
                return (), False
            return (
                _collect_lp_market_pages(
                    public.list_markets(condition_ids=batch, page_size=100),
                    set(batch),
                ),
                True,
            )

        market_batches = tuple(
            requested[offset : offset + 100]
            for offset in range(0, len(requested), 100)
        )
        remove_response_hook = _install_lp_metadata_response_hook(
            public, len(requested)
        )
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(market_batches))) as pool:
                futures = [
                    (batch, pool.submit(read_market_batch, batch))
                    for batch in market_batches
                ]
                for batch, future in futures:
                    try:
                        batch_rows, completed = future.result()
                    except Exception as exc:
                        reason = _safe_read_failure("market", exc)
                        failed_ids.update((condition_id, reason) for condition_id in batch)
                        continue
                    if completed:
                        completed_market_ids.update(batch)
                        rows.extend(batch_rows)
        finally:
            if remove_response_hook is not None:
                remove_response_hook()

        numeric_event_keys: dict[int, list[str]] = {}
        direct_event_ids: list[str] = []
        for value in rows:
            row = _model_dict(value)
            if row is None:
                continue
            condition_id = row.get("condition_id", row.get("conditionId"))
            if not isinstance(condition_id, str) or condition_id not in requested:
                continue
            references = tuple(
                reference
                for raw_reference in _collect(row.get("events"))
                if (reference := _model_dict(raw_reference)) is not None
            )
            if len(references) != 1:
                continue
            raw_event_id = references[0].get("id")
            event_id = str(raw_event_id).strip() if raw_event_id is not None else ""
            if not event_id or event_id in event_facts:
                continue
            event_facts[event_id] = None
            if (
                event_id.isascii()
                and event_id.isdecimal()
                and str(int(event_id)) == event_id
            ):
                numeric_event_keys.setdefault(int(event_id), []).append(event_id)
            else:
                direct_event_ids.append(event_id)

        def mark_event_failure(event_ids: Sequence[object], reason: str) -> None:
            with event_failures_lock:
                event_failures.update((str(value).strip(), reason) for value in event_ids)

        unresolved_numeric_ids = tuple(numeric_event_keys)
        for closed in (False, True):
            if not unresolved_numeric_ids:
                break
            event_batches = tuple(
                (unresolved_numeric_ids[offset : offset + 100], closed)
                for offset in range(0, len(unresolved_numeric_ids), 100)
            )

            def read_event_batch(
                query: tuple[tuple[int, ...], bool],
            ) -> tuple[tuple[object, ...], str | None]:
                batch, is_closed = query
                if stop_event is not None and stop_event.is_set():
                    reason = "event_read_cancelled"
                    mark_event_failure(batch, reason)
                    return (), reason
                list_events = getattr(public, "list_events", None)
                if not callable(list_events):
                    reason = "event_read_unavailable"
                    mark_event_failure(batch, reason)
                    return (), reason
                try:
                    return (
                        _collect(list_events(ids=batch, closed=is_closed, page_size=100)),
                        None,
                    )
                except Exception as exc:
                    reason = _safe_read_failure("event", exc)
                    mark_event_failure(batch, reason)
                    return (), reason

            resolved: set[int] = set()
            with ThreadPoolExecutor(max_workers=min(8, len(event_batches))) as pool:
                futures = [
                    (batch, pool.submit(read_event_batch, query))
                    for query in event_batches
                    for batch in (query[0],)
                ]
                for batch, future in futures:
                    event_rows, _error = future.result()
                    for value in event_rows:
                        event = _model_dict(value)
                        if event is None:
                            continue
                        raw_id = event.get("id")
                        event_id = str(raw_id) if raw_id is not None else ""
                        if not event_id.isascii() or not event_id.isdecimal():
                            continue
                        numeric_id = int(event_id)
                        if (
                            event_id != str(numeric_id)
                            or numeric_id not in batch
                            or numeric_id not in numeric_event_keys
                        ):
                            continue
                        for key in numeric_event_keys[numeric_id]:
                            event_facts[key] = event
                            with event_failures_lock:
                                event_failures.pop(key, None)
                        resolved.add(numeric_id)
            unresolved_numeric_ids = tuple(
                event_id
                for event_id in unresolved_numeric_ids
                if event_id not in resolved
            )
        if stop_event is not None and stop_event.is_set():
            mark_event_failure(unresolved_numeric_ids, "event_read_cancelled")

        def read_direct_event(
            event_id: str,
        ) -> tuple[str, Mapping[str, object] | None, str | None]:
            if stop_event is not None and stop_event.is_set():
                return event_id, None, "event_read_cancelled"
            get_event = getattr(public, "get_event", None)
            if not callable(get_event):
                return event_id, None, "event_read_unavailable"
            try:
                event = _model_dict(get_event(id=event_id))
            except Exception as exc:
                return event_id, None, _safe_read_failure("event", exc)
            if event is None or str(event.get("id") or "") != event_id:
                return event_id, None, None
            return event_id, event, None

        if direct_event_ids:
            with ThreadPoolExecutor(max_workers=min(8, len(direct_event_ids))) as pool:
                for event_id, event, error in pool.map(read_direct_event, direct_event_ids):
                    if event is not None:
                        event_facts[event_id] = event
                        with event_failures_lock:
                            event_failures.pop(event_id, None)
                    elif error is not None:
                        mark_event_failure((event_id,), error)
        result: dict[str, dict[str, object]] = {}
        for value in rows:
            row = _model_dict(value)
            if row is None:
                continue
            condition_id = row.get("condition_id", row.get("conditionId"))
            if not isinstance(condition_id, str) or condition_id not in requested:
                continue
            slug = row.get("slug")
            market_url = row.get("market_url", row.get("url"))
            state = _model_dict(row.get("state")) or {}
            trading = _model_dict(row.get("trading")) or {}
            rewards = _model_dict(row.get("rewards")) or {}
            fee_schedule = _model_dict(trading.get("fee_schedule")) or {}
            raw_spread = _lp_decimal(
                rewards.get("rewards_max_spread", row.get("rewards_max_spread"))
            )
            fees_enabled = trading.get("fees_enabled")
            taker_rate = _lp_decimal(
                fee_schedule.get("rate", row.get("taker_fee_rate"))
            )
            raw_outcomes = _model_dict(row.get("outcomes")) or {}
            outcomes: dict[str, dict[str, object]] = {}
            for key, raw_outcome in raw_outcomes.items():
                outcome = _model_dict(raw_outcome)
                if outcome is None:
                    continue
                token_id = outcome.get("token_id", outcome.get("tokenId"))
                if token_id is None:
                    continue
                outcome_key = str(key).strip().lower()
                outcomes[outcome_key] = {
                    "label": outcome.get("label", key),
                    "token_id": str(token_id),
                }
            events = tuple(
                event
                for raw_event in _collect(row.get("events"))
                if (event := _model_dict(raw_event)) is not None
            )
            event_reference = events[0] if len(events) == 1 else None
            event_id = event_reference.get("id") if event_reference else None
            event_key = str(event_id).strip() if event_id is not None else ""
            event = event_facts.get(event_key) if event_key else None
            if event_key in event_failures:
                failed_ids.setdefault(condition_id, event_failures[event_key])
            sports = _model_dict(row.get("sports")) or {}
            event_state = _model_dict(event.get("state")) or {} if event else {}
            event_schedule = _model_dict(event.get("schedule")) or {} if event else {}
            prices = _model_dict(row.get("prices")) or {}
            one_day_price_change = _lp_decimal(prices.get("one_day_price_change"))
            event_slug = (
                event.get("slug")
                if event is not None
                else event_reference.get("slug") if event_reference else None
            )
            if not market_url and isinstance(slug, str) and slug.strip():
                market_slug = slug.strip()
                if (
                    isinstance(event_slug, str)
                    and event_slug.strip()
                    and event_slug.strip() != market_slug
                ):
                    market_url = (
                        f"https://polymarket.com/event/{event_slug.strip()}/{market_slug}"
                    )
                else:
                    market_url = f"https://polymarket.com/event/{market_slug}"
            result[condition_id] = {
                "market_id": row.get("id", row.get("market_id")),
                "condition_id": condition_id,
                "metadata_checked_at": metadata_checked_at,
                "market_title": row.get("question", row.get("title")),
                "market_url": market_url,
                "event_id": event_id,
                "game_id": sports.get("game_id"),
                "game_start_time": sports.get("game_start_time"),
                "event_start_time": event_schedule.get("start_time"),
                "event_ended": event_state.get("ended"),
                "event_finished_at": event_schedule.get("finished_at"),
                "price_change_24h": one_day_price_change,
                "price_change_24h_source": (
                    "polymarket.prices.one_day_price_change"
                    if one_day_price_change is not None
                    else None
                ),
                "accepting_orders": state.get("accepting_orders"),
                "exchange_type": "CLOB",
                "tick_size": _lp_decimal(trading.get("minimum_tick_size")),
                "minimum_order_size": _lp_decimal(
                    trading.get("minimum_order_size")
                ),
                "fee": (
                    Decimal("0")
                    if fees_enabled is False or fee_schedule.get("taker_only") is True
                    else None
                ),
                "fees_enabled": fees_enabled,
                "fee_exponent": _lp_decimal(fee_schedule.get("exponent", 1)),
                "taker_fee_rate": taker_rate,
                "reward_min_size": _lp_decimal(
                    rewards.get("rewards_min_size", row.get("rewards_min_size"))
                ),
                "reward_max_spread": (
                    None if raw_spread is None else raw_spread / Decimal("100")
                ),
                "outcomes": outcomes,
            }
        confirmed_absent_ids = frozenset(
            condition_id
            for condition_id in completed_market_ids
            if condition_id not in result and condition_id not in failed_ids
        )
        return result, failed_ids, confirmed_absent_ids

    def lp_order_books(
        self,
        token_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, dict[str, object]]:
        """Read current books in bounded SDK batches for the LP candidate catalog."""

        requested = tuple(
            dict.fromkeys(
                value.strip()
                for value in token_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not requested or (stop_event is not None and stop_event.is_set()):
            return {}
        result: dict[str, dict[str, object]] = {}
        public = self._public_client_factory()
        try:
            for offset in range(0, len(requested), 100):
                if stop_event is not None and stop_event.is_set():
                    return {}
                batch = requested[offset : offset + 100]
                rows = _collect(public.get_order_books(token_ids=batch))
                received_at = datetime.now(UTC)
                if stop_event is not None and stop_event.is_set():
                    return {}
                for value in rows:
                    row = _model_dict(value)
                    if row is None:
                        continue
                    token_id = row.get("token_id", row.get("asset_id"))
                    if not isinstance(token_id, str) or token_id not in batch:
                        continue
                    book = _lp_book(row)
                    if book is None:
                        continue
                    book["received_at"] = received_at
                    result[token_id] = book
        finally:
            close = getattr(public, "close", None)
            if callable(close):
                close()
        return result

    def lp_price_history(
        self,
        token_ids: Sequence[str],
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int = 1,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read minute price histories in bounded public batches.

        The response is intentionally lossless about missing directions: a
        token with no valid rows is reported in ``unknown_token_ids`` rather
        than being given an empty or zero-priced range.
        """

        requested = tuple(
            dict.fromkeys(
                value.strip()
                for value in token_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not requested or type(start_ts) is not int or type(end_ts) is not int:
            return {
                "state": "unknown",
                "history": {},
                "unknown_token_ids": list(requested),
                "errors": {token: "request_invalid" for token in requested},
                "request_count": 0,
            }
        if end_ts < start_ts or fidelity != 1:
            return {
                "state": "unknown",
                "history": {},
                "unknown_token_ids": list(requested),
                "errors": {token: "request_invalid" for token in requested},
                "request_count": 0,
            }
        batches = tuple(
            requested[offset : offset + LP_PRICE_HISTORY_BATCH_SIZE]
            for offset in range(0, len(requested), LP_PRICE_HISTORY_BATCH_SIZE)
        )
        opener = self._urlopen_fn or urlopen

        def read_batch(batch: tuple[str, ...]) -> tuple[tuple[str, ...], object, str | None]:
            if stop_event is not None and stop_event.is_set():
                return batch, None, "cancelled"
            body = json.dumps(
                {
                    "markets": list(batch),
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            request = Request(
                LP_PRICE_HISTORY_ENDPOINT,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "OpenTrader/1.0",
                },
                method="POST",
            )
            try:
                with opener(request, timeout=LP_PRICE_HISTORY_TIMEOUT_SECONDS) as response:
                    raw = response.read()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return batch, json.loads(raw), None
            except Exception as exc:
                return batch, None, type(exc).__name__

        histories: dict[str, list[dict[str, object]]] = {}
        unknown: set[str] = set()
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=min(LP_PRICE_HISTORY_MAX_CONCURRENCY, len(batches))
        ) as pool:
            for batch, payload, error in pool.map(read_batch, batches):
                if error is not None:
                    unknown.update(batch)
                    for token in batch:
                        errors[token] = error
                    continue
                raw_history = payload.get("history") if isinstance(payload, Mapping) else None
                if not isinstance(raw_history, Mapping):
                    unknown.update(batch)
                    for token in batch:
                        errors[token] = "history_shape_unknown"
                    continue
                for token in batch:
                    raw_rows = raw_history.get(token)
                    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
                        unknown.add(token)
                        errors[token] = "history_missing"
                        continue
                    by_timestamp: dict[int, dict[str, object]] = {}
                    invalid = False
                    for raw_row in raw_rows:
                        if not isinstance(raw_row, Mapping):
                            invalid = True
                            continue
                        stamp = raw_row.get("t", raw_row.get("timestamp"))
                        price = _lp_decimal(raw_row.get("p", raw_row.get("price")))
                        if type(stamp) is not int:
                            invalid = True
                            continue
                        if price is None or price < 0 or price > 1:
                            invalid = True
                            continue
                        if stamp < start_ts or stamp > end_ts:
                            continue
                        by_timestamp[stamp] = {"t": stamp, "p": price}
                    rows = [by_timestamp[stamp] for stamp in sorted(by_timestamp)]
                    if rows and not invalid:
                        histories[token] = rows
                    elif invalid:
                        unknown.add(token)
                        errors[token] = "history_values_invalid"
                    else:
                        unknown.add(token)
                        errors[token] = "history_values_unknown"
        if stop_event is not None and stop_event.is_set():
            for token in requested:
                unknown.add(token)
                errors.setdefault(token, "cancelled")
        unknown.difference_update(histories)
        return {
            "state": "known" if not unknown else "partial" if histories else "unknown",
            "history": histories,
            "unknown_token_ids": [token for token in requested if token in unknown],
            "errors": errors,
            "request_count": len(batches),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "fidelity": fidelity,
        }

    def _lp_selected_reward_catalog(
        self,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read reward facts for a fixed market set without global calls."""

        checked_at = datetime.now(UTC)
        requested = tuple(
            dict.fromkeys(
                value.strip()
                for value in condition_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not requested:
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "daily_pool_usd": Decimal("0"),
                "markets": (),
            }

        requests = tuple(
            (condition_id, sponsored)
            for condition_id in requested
            for sponsored in (False, True)
        )

        def close_public(public: object) -> None:
            close = getattr(public, "close", None)
            if callable(close):
                close()

        def read_rewards(
            request: tuple[str, bool],
        ) -> tuple[str, bool, tuple[object, ...], str | None]:
            condition_id, sponsored = request
            if stop_event is not None and stop_event.is_set():
                return condition_id, sponsored, (), "reward_read_cancelled"
            public = self._public_client_factory()
            try:
                reader = getattr(public, "list_market_rewards", None)
                if not callable(reader):
                    raise ValueError("selected_reward_reader_unavailable")
                return condition_id, sponsored, _collect(
                    reader(condition_id=condition_id, sponsored=sponsored)
                ), None
            except _RewardReadCancelled:
                return condition_id, sponsored, (), "reward_read_cancelled"
            except Exception:
                return condition_id, sponsored, (), "reward_read_failed"
            finally:
                close_public(public)

        results: dict[tuple[str, bool], tuple[tuple[object, ...], str | None]] = {}
        with ThreadPoolExecutor(
            max_workers=min(LP_REWARD_SELECTED_MAX_CONCURRENCY, len(requests)),
            thread_name_prefix="polymarket-selected-rewards",
        ) as executor:
            for condition_id, sponsored, rows, error in executor.map(
                read_rewards, requests
            ):
                results[(condition_id, sponsored)] = (rows, error)

        markets: list[dict[str, object]] = []
        known_count = 0
        total = Decimal("0")
        total_known = True
        as_of = checked_at.date()
        for condition_id in requested:
            market: dict[str, object] = {
                "condition_id": condition_id,
                "checked_at": checked_at,
                "reward_checked_at": checked_at,
                "rewards_max_spread": None,
                "rewards_min_size": None,
                "native_reward_configs": [],
                "combined_reward_configs": [],
                "sponsored_reward_configs": [],
                "native_daily_pool_usd": Decimal("0"),
                "sponsored_daily_pool_usd": Decimal("0"),
            }
            reason_codes: list[str] = []
            row_fingerprints: dict[bool, str] = {}
            config_signatures: dict[bool, dict[str, tuple[object, ...]]] = {
                False: {},
                True: {},
            }
            active_configs = 0
            unknown_asset = False
            native_total: Decimal | None = Decimal("0")
            combined_total: Decimal | None = Decimal("0")

            for sponsored in (False, True):
                rows, read_error = results[(condition_id, sponsored)]
                if read_error is not None:
                    reason_codes.append(read_error)
                    continue
                for reward in rows:
                    row = _model_dict(reward)
                    if row is None or row.get("condition_id") != condition_id:
                        reason_codes.append("reward_identity_unknown")
                        continue
                    try:
                        fingerprint = json.dumps(
                            row,
                            sort_keys=True,
                            default=str,
                            separators=(",", ":"),
                        )
                    except (TypeError, ValueError):
                        reason_codes.append("reward_identity_unknown")
                        continue
                    previous_fingerprint = row_fingerprints.get(sponsored)
                    if previous_fingerprint is not None:
                        if previous_fingerprint == fingerprint:
                            continue
                        reason_codes.append("reward_identity_unknown")
                        continue
                    row_fingerprints[sponsored] = fingerprint

                    if sponsored:
                        combined_spread = _lp_decimal(
                            row.get("rewards_max_spread")
                        )
                        combined_min_size = _lp_decimal(
                            row.get("rewards_min_size")
                        )
                        if combined_spread is not None:
                            market["rewards_max_spread"] = combined_spread
                        if combined_min_size is not None:
                            market["rewards_min_size"] = combined_min_size
                    raw_configs = row.get("rewards_config")
                    if not isinstance(raw_configs, Sequence) or isinstance(
                        raw_configs, (str, bytes)
                    ):
                        reason_codes.append("reward_config_unknown")
                        continue
                    for raw_config in raw_configs:
                        normalized_parts = _normalize_lp_reward_config(
                            raw_config,
                            sponsored=sponsored,
                            require_id=False,
                        )
                        if normalized_parts is None:
                            reason_codes.append("reward_config_unknown")
                            continue
                        normalized, config_identity = normalized_parts
                        config_id, asset_key, start_date, end_date = config_identity
                        rate = cast(Decimal, normalized["rate_per_day"])
                        if config_id is not None:
                            config_key = str(config_id)
                            signature = (asset_key, start_date, end_date, rate)
                            previous_signature = config_signatures[sponsored].get(
                                config_key
                            )
                            if previous_signature is not None:
                                if previous_signature == signature:
                                    continue
                                reason_codes.append("reward_identity_unknown")
                                continue
                            config_signatures[sponsored][config_key] = signature
                        if not start_date <= as_of <= end_date:
                            continue
                        configs_key = (
                            "combined_reward_configs"
                            if sponsored
                            else "native_reward_configs"
                        )
                        cast(list[dict[str, object]], market[configs_key]).append(
                            normalized
                        )
                        active_configs += 1
                        if asset_key not in LP_REWARD_ASSET_USD_ADDRESSES:
                            unknown_asset = True
                            if sponsored:
                                combined_total = None
                            else:
                                native_total = None
                            continue
                        if sponsored:
                            if combined_total is not None:
                                combined_total += rate
                        elif native_total is not None:
                            native_total += rate

            if unknown_asset:
                reason_codes.append("reward_asset_unknown")
            reason_codes = list(dict.fromkeys(reason_codes))
            if reason_codes:
                market.update(
                    {
                        "state": "unknown",
                        "complete": False,
                        "reward_active": None,
                        "daily_pool_usd": None,
                        "reason_codes": reason_codes,
                    }
                )
                total_known = False
            else:
                native = native_total
                combined = combined_total
                if native is None or combined is None:
                    market.update(
                        {
                            "state": "unknown",
                            "complete": False,
                            "reward_active": None,
                            "daily_pool_usd": None,
                            "reason_codes": ["reward_total_unknown"],
                        }
                    )
                    total_known = False
                    markets.append(market)
                    continue
                if combined < native:
                    market.update(
                        {
                            "state": "unknown",
                            "complete": False,
                            "reward_active": None,
                            "daily_pool_usd": None,
                            "sponsored_daily_pool_usd": None,
                            "reason_codes": ["reward_totals_inconsistent"],
                        }
                    )
                    total_known = False
                    markets.append(market)
                    continue
                sponsored = combined - native
                daily_pool = combined
                market["native_daily_pool_usd"] = native
                market["sponsored_daily_pool_usd"] = sponsored
                market.update(
                    {
                        "state": "known",
                        "complete": True,
                        "reward_active": active_configs > 0,
                        "daily_pool_usd": daily_pool,
                        "reason_codes": [],
                    }
                )
                known_count += 1
                total += daily_pool
            markets.append(market)

        complete = known_count == len(requested)
        return {
            "state": "known"
            if complete
            else "partial"
            if known_count
            else "unknown",
            "complete": complete,
            "checked_at": checked_at,
            "daily_pool_usd": total if total_known else None,
            "markets": tuple(markets),
        }

    def lp_reward_catalog(
        self,
        *,
        condition_ids: Sequence[str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read complete active native and sponsored LP reward configurations."""

        if condition_ids is not None:
            return self._lp_selected_reward_catalog(
                condition_ids, stop_event=stop_event
            )

        checked_at = datetime.now(UTC)
        unknown = {
            "state": "unknown",
            "complete": False,
            "checked_at": checked_at,
            "daily_pool_usd": None,
            "markets": (),
        }
        try:
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            public = self._public_client_factory()
            try:
                reward_rows = (
                    (False, _collect(public.list_current_rewards(sponsored=False))),
                    (True, _collect(public.list_current_rewards(sponsored=True))),
                )
            finally:
                close = getattr(public, "close", None)
                if callable(close):
                    close()
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            markets: dict[str, dict[str, object]] = {}
            seen: set[tuple[object, ...]] = set()
            as_of = checked_at.date()
            for sponsored, rewards in reward_rows:
                for reward in rewards:
                    row = _model_dict(reward)
                    if row is None:
                        raise ValueError("reward_market_unknown")
                    condition_id = row.get("condition_id")
                    if not isinstance(condition_id, str) or not condition_id:
                        raise ValueError("reward_market_unknown")
                    raw_configs = row.get("rewards_config")
                    if not isinstance(raw_configs, Sequence) or isinstance(
                        raw_configs, (str, bytes)
                    ):
                        raise ValueError("reward_config_unknown")

                    market = markets.setdefault(
                        condition_id,
                        {
                            "condition_id": condition_id,
                            "rewards_max_spread": _lp_decimal(
                                row.get("rewards_max_spread")
                            ),
                            "rewards_min_size": _lp_decimal(
                                row.get("rewards_min_size")
                            ),
                            "native_reward_configs": [],
                            "sponsored_reward_configs": [],
                            "native_daily_pool_usd": Decimal("0"),
                            "sponsored_daily_pool_usd": Decimal("0"),
                        },
                    )
                    configs_key = (
                        "sponsored_reward_configs"
                        if sponsored
                        else "native_reward_configs"
                    )
                    amount_key = (
                        "sponsored_daily_pool_usd"
                        if sponsored
                        else "native_daily_pool_usd"
                    )
                    for raw_config in raw_configs:
                        normalized_parts = _normalize_lp_reward_config(
                            raw_config,
                            sponsored=sponsored,
                        )
                        if normalized_parts is None:
                            raise ValueError("reward_config_unknown")
                        normalized, config_identity = normalized_parts
                        _, asset_key, start_date, end_date = config_identity
                        rate = cast(Decimal, normalized["rate_per_day"])
                        identity = (
                            condition_id,
                            *config_identity,
                            sponsored,
                        )
                        if identity in seen:
                            continue
                        seen.add(identity)
                        if not start_date <= as_of <= end_date:
                            continue

                        cast(list[dict[str, object]], market[configs_key]).append(
                            normalized
                        )
                        if asset_key not in LP_REWARD_ASSET_USD_ADDRESSES:
                            market[amount_key] = None
                            continue
                        current_amount = cast(Decimal | None, market[amount_key])
                        if current_amount is not None:
                            market[amount_key] = current_amount + rate

            result_markets: list[dict[str, object]] = []
            total = Decimal("0")
            total_known = True
            for market in markets.values():
                native = cast(Decimal | None, market["native_daily_pool_usd"])
                sponsored = cast(Decimal | None, market["sponsored_daily_pool_usd"])
                market["daily_pool_usd"] = (
                    None if native is None or sponsored is None else native + sponsored
                )
                pool = cast(Decimal | None, market["daily_pool_usd"])
                if pool is None:
                    total_known = False
                else:
                    total += pool
                result_markets.append(market)
                market["reward_active"] = True
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "daily_pool_usd": total if total_known else None,
                "markets": tuple(result_markets),
            }
        except _RewardReadCancelled:
            unknown["reason"] = "cancelled"
            return unknown
        except Exception as exc:
            unknown["reason"] = "reward_catalog_read_failed"
            unknown["error_type"] = type(exc).__name__
            return unknown

    def lp_reward_rates(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Read current per-market native and sponsored reward shares."""

        checked_at = datetime.now(UTC)
        unknown = {
            "state": "unknown",
            "complete": False,
            "checked_at": checked_at,
            "markets": {},
        }
        try:
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            context = getattr(self._client, "_ctx", None)
            transport = getattr(context, "secure_clob", None)
            get_json = getattr(transport, "get_json", None)
            if not callable(get_json):
                raise ValueError("reward_transport_unknown")
            wallet_type = getattr(context, "wallet_type", None)
            signature_type = signature_type_for(wallet_type)
            as_of = checked_at.date()
            collected: dict[str, dict[str, object]] = {}
            identities: set[tuple[object, ...]] = set()

            for sponsored in (False, True):
                source = "sponsored" if sponsored else "native"
                for only_open_orders, only_open_positions in (
                    (True, False),
                    (False, True),
                ):
                    cursor: str | None = None
                    seen_cursors: set[str] = set()
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            raise _RewardReadCancelled
                        params: dict[str, object] = {
                            "signature_type": signature_type,
                            "maker_address": self.config.wallet_address,
                            "sponsored": sponsored,
                            "only_open_orders": only_open_orders,
                            "only_open_positions": only_open_positions,
                            "page_size": 500,
                        }
                        if cursor is not None:
                            params["next_cursor"] = cursor
                        payload = get_json("/rewards/user/markets", params=params)
                        if stop_event is not None and stop_event.is_set():
                            raise _RewardReadCancelled
                        if not isinstance(payload, Mapping):
                            raise ValueError("reward_page_unknown")
                        rows = payload.get("data")
                        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                            raise ValueError("reward_page_unknown")
                        for raw_row in rows:
                            row = _model_dict(raw_row)
                            if row is None:
                                raise ValueError("reward_row_unknown")
                            condition_id = row.get("condition_id")
                            if not isinstance(condition_id, str) or not condition_id:
                                raise ValueError("reward_market_unknown")
                            percentage = _lp_decimal(row.get("earning_percentage"))
                            if percentage is not None and not Decimal("0") <= percentage <= Decimal("100"):
                                percentage = None
                            raw_configs = row.get("rewards_config")
                            if not isinstance(raw_configs, Sequence) or isinstance(
                                raw_configs, (str, bytes)
                            ):
                                raise ValueError("reward_config_unknown")
                            active_configs: list[dict[str, object]] = []
                            for raw_config in raw_configs:
                                config = _model_dict(raw_config)
                                if config is None:
                                    raise ValueError("reward_config_unknown")
                                config_id = config.get("id")
                                asset_address = config.get("asset_address")
                                start_date = _reward_date(config.get("start_date"))
                                end_date = _reward_date(config.get("end_date"))
                                rate = _lp_decimal(config.get("rate_per_day"))
                                if (
                                    config_id is None
                                    or not isinstance(asset_address, str)
                                    or start_date is None
                                    or end_date is None
                                    or rate is None
                                    or rate < 0
                                ):
                                    raise ValueError("reward_config_unknown")
                                if not start_date <= as_of <= end_date:
                                    continue
                                normalized = dict(config)
                                normalized["rate_per_day"] = rate
                                normalized["sponsored"] = sponsored
                                normalized["source"] = source
                                active_configs.append(normalized)
                            if not active_configs:
                                continue
                            market = collected.setdefault(
                                condition_id,
                                {"condition_id": condition_id, "sources": {}},
                            )
                            market_sources = cast(
                                dict[str, dict[str, object]], market["sources"]
                            )
                            source_result = market_sources.setdefault(
                                source,
                                {
                                    "source": source,
                                    "percentages": [],
                                    "reward_configs": [],
                                },
                            )
                            cast(list[Decimal | None], source_result["percentages"]).append(
                                percentage
                            )
                            for normalized in active_configs:
                                asset_address = str(normalized["asset_address"])
                                start_date = _reward_date(normalized["start_date"])
                                end_date = _reward_date(normalized["end_date"])
                                assert start_date is not None and end_date is not None
                                identity = (
                                    condition_id,
                                    str(normalized["id"]),
                                    asset_address.casefold(),
                                    start_date,
                                    end_date,
                                    sponsored,
                                )
                                if identity in identities:
                                    continue
                                identities.add(identity)
                                cast(
                                    list[dict[str, object]],
                                    source_result["reward_configs"],
                                ).append(normalized)
                        next_cursor = payload.get("next_cursor")
                        if not isinstance(next_cursor, str) or not next_cursor:
                            raise ValueError("reward_pagination_unknown")
                        if next_cursor == "LTE=":
                            break
                        if next_cursor in seen_cursors:
                            raise ValueError("reward_pagination_loop")
                        seen_cursors.add(next_cursor)
                        cursor = next_cursor

            for market in collected.values():
                hourly_total = Decimal("0")
                market_known = True
                source_rows = cast(dict[str, dict[str, object]], market["sources"])
                for source in ("native", "sponsored"):
                    source_result = source_rows.get(source)
                    if source_result is None:
                        continue
                    percentages = cast(list[Decimal | None], source_result["percentages"])
                    configs = cast(
                        list[dict[str, object]], source_result["reward_configs"]
                    )
                    percentage = (
                        percentages[0]
                        if percentages
                        and percentages[0] is not None
                        and all(item == percentages[0] for item in percentages)
                        else None
                    )
                    daily_pool = Decimal("0")
                    source_known = percentage is not None
                    for config in configs:
                        asset_address = str(config["asset_address"]).casefold()
                        if asset_address not in LP_REWARD_ASSET_USD_ADDRESSES:
                            source_known = False
                            continue
                        daily_pool += cast(Decimal, config["rate_per_day"])
                    rate = (
                        daily_pool * percentage / Decimal("100") / Decimal("24")
                        if source_known and percentage is not None
                        else None
                    )
                    if not source_known or rate is None:
                        market_known = False
                        source_result.update(
                            {
                                "state": "unknown",
                                "earning_percentage": percentage,
                                "daily_pool_usd": None,
                                "hourly_reward_usd": None,
                                "currency": None,
                                "checked_at": checked_at,
                            }
                        )
                    else:
                        source_result.update(
                            {
                                "state": "known",
                                "earning_percentage": percentage,
                                "daily_pool_usd": daily_pool,
                                "hourly_reward_usd": rate,
                                "currency": "USD",
                                "checked_at": checked_at,
                            }
                        )
                        hourly_total += rate
                    market[source] = source_result
                market["state"] = "known" if market_known else "unknown"
                market["hourly_reward_usd"] = hourly_total if market_known else None
                market["currency"] = "USD" if market_known else None
                market["checked_at"] = checked_at
                market["sources"] = tuple(
                    source
                    for source in ("native", "sponsored")
                    if source in source_rows
                )
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "markets": collected,
            }
        except _RewardReadCancelled:
            unknown["reason"] = "cancelled"
            return unknown
        except Exception:
            return unknown

    def lp_reward_snapshots(
        self,
        reward_date: str,
        condition_ids: Sequence[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, dict[str, object]]:
        """Read one day's rewards for several conditions with one account read."""

        requested = tuple(dict.fromkeys(str(condition_id) for condition_id in condition_ids))
        if not requested:
            return {}
        unknown = {
            condition_id: {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "maker_address": self.config.wallet_address,
            }
            for condition_id in requested
        }
        if not any(requested):
            return unknown
        try:
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            parsed_date = Date.fromisoformat(str(reward_date))
            context = getattr(self._client, "_ctx", None)
            transport = getattr(context, "secure_clob", None)
            get_json = getattr(transport, "get_json", None)
            if not callable(get_json):
                raise ValueError("reward_transport_unknown")
            wallet_type = getattr(context, "wallet_type", None)
            signature_type = signature_type_for(wallet_type)
            maker = self.config.wallet_address
            total_payload = get_json(
                "/rewards/user/total",
                params={
                    "date": parsed_date.isoformat(),
                    "signature_type": signature_type,
                    "maker_address": maker,
                    "sponsored": True,
                },
            )
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            total_rows = _reward_total_rows(total_payload)
            account_amount = _reward_amount(
                total_rows, parsed_date=parsed_date, maker=maker
            )
            account_raw_amounts = _reward_raw_amounts(
                total_rows, parsed_date=parsed_date, maker=maker
            )
            if account_raw_amounts is None or any(
                asset not in LP_REWARD_ASSET_USD_ADDRESSES
                for asset in account_raw_amounts
            ):
                raise ValueError("reward_total_unknown")
            account_accruals = _reward_accruals(account_raw_amounts)
            account_raw = account_accruals[0] if len(account_accruals) == 1 else None
            results: dict[str, dict[str, object]] = {}
            for condition_id in requested:
                if not condition_id:
                    continue
                try:
                    market_rows: list[Mapping[str, object]] = []
                    for sponsored in (False, True):
                        market_rows.extend(
                            self._lp_reward_market_rows(
                                parsed_date=parsed_date,
                                maker=maker,
                                condition_id=condition_id,
                                signature_type=signature_type,
                                sponsored=sponsored,
                                get_json=get_json,
                                stop_event=stop_event,
                            )
                        )
                    market_amount = _reward_amount(
                        market_rows,
                        parsed_date=parsed_date,
                        maker=maker,
                        condition_id=condition_id,
                    )
                    market_raw_amounts = _reward_raw_amounts(
                        market_rows,
                        parsed_date=parsed_date,
                        maker=maker,
                        condition_id=condition_id,
                    )
                    if market_raw_amounts is None or any(
                        asset not in LP_REWARD_ASSET_USD_ADDRESSES
                        for asset in market_raw_amounts
                    ):
                        raise ValueError("reward_market_unknown")
                    market_accruals = _reward_accruals(market_raw_amounts)
                    market_raw = market_accruals[0] if len(market_accruals) == 1 else None
                    usd_state = (
                        "known"
                        if account_amount is not None and market_amount is not None
                        else "unknown"
                    )
                    results[condition_id] = {
                        "state": usd_state,
                        "reward_date": parsed_date.isoformat(),
                        "condition_id": condition_id,
                        "maker_address": maker,
                        "account_amount": account_amount,
                        "market_amount": market_amount,
                        "account_amount_raw": account_raw.get("amount") if account_raw else None,
                        "account_asset": account_raw.get("asset") if account_raw else None,
                        "account_accruals_raw": account_accruals,
                        "market_amount_raw": market_raw.get("amount") if market_raw else None,
                        "market_asset": market_raw.get("asset") if market_raw else None,
                        "market_accruals_raw": market_accruals,
                        "usd_state": usd_state,
                        "account_reward": account_amount,
                        "market_reward": market_amount,
                        "currency": "USD",
                        "paid": False,
                        "reason": "usd_value_unknown" if usd_state == "unknown" else None,
                        "conversion_basis": (
                            "earnings at unit asset_rate for verified pUSD/USDC.e assets; "
                            "non-unit valuations UNKNOWN"
                        ),
                    }
                except _RewardReadCancelled:
                    raise
                except Exception:
                    results[condition_id] = dict(unknown[condition_id])
            return {condition_id: results.get(condition_id, unknown[condition_id]) for condition_id in requested}
        except _RewardReadCancelled:
            return {
                condition_id: {**unknown[condition_id], "reason": "cancelled"}
                for condition_id in requested
            }
        except Exception:
            return unknown


    def lp_reward_snapshot(
        self,
        reward_date: str,
        condition_id: str,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Read one day's current platform reward record without trading."""

        return self.lp_reward_snapshots(
            reward_date, (condition_id,), stop_event=stop_event
        ).get(
            condition_id,
            {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "maker_address": self.config.wallet_address,
            },
        )

    def lp_reward_percentages(self) -> dict[str, object]:
        """Read the account's official per-market reward percentages."""

        checked_at = datetime.now(UTC)
        unknown = {
            "state": "unknown",
            "scope": "account",
            "maker_address": self.config.wallet_address,
            "percentages": {},
            "checked_at": checked_at,
        }
        try:
            context = getattr(self._client, "_ctx", None)
            transport = getattr(context, "secure_clob", None)
            get_json = getattr(transport, "get_json", None)
            if not callable(get_json):
                raise ValueError("reward_percentage_transport_unknown")
            signature_type = signature_type_for(getattr(context, "wallet_type", None))
            payload = get_json(
                "/rewards/user/percentages",
                params={
                    "signature_type": signature_type,
                    "maker_address": self.config.wallet_address,
                },
            )
            if not isinstance(payload, Mapping):
                raise ValueError("reward_percentage_shape_unknown")
            percentages: dict[str, Decimal] = {}
            for raw_condition_id, raw_percentage in payload.items():
                if (
                    not isinstance(raw_condition_id, str)
                    or not raw_condition_id.strip()
                ):
                    raise ValueError("reward_percentage_identity_unknown")
                percentage = _lp_decimal(raw_percentage)
                if (
                    percentage is None
                    or percentage < Decimal("0")
                    or percentage > Decimal("100")
                ):
                    raise ValueError("reward_percentage_value_unknown")
                percentages[raw_condition_id] = percentage
            return {
                "state": "known",
                "scope": "account",
                "maker_address": self.config.wallet_address,
                "percentages": percentages,
                "checked_at": checked_at,
            }
        except Exception as exc:
            del exc
            return unknown

    @staticmethod
    def _lp_reward_market_rows(
        *,
        parsed_date: Date,
        maker: str,
        condition_id: str,
        signature_type: int,
        sponsored: bool,
        get_json: Callable[..., object],
        stop_event: threading.Event | None = None,
    ) -> list[Mapping[str, object]]:
        rows: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            params: dict[str, object] = {
                "date": parsed_date.isoformat(),
                "signature_type": signature_type,
                "maker_address": maker,
                "sponsored": sponsored,
            }
            if cursor is not None:
                params["next_cursor"] = cursor
            payload = get_json("/rewards/user", params=params)
            if stop_event is not None and stop_event.is_set():
                raise _RewardReadCancelled
            if not isinstance(payload, Mapping):
                raise ValueError("reward_page_unknown")
            page_rows = payload.get("data")
            if not isinstance(page_rows, Sequence) or isinstance(page_rows, (str, bytes)):
                raise ValueError("reward_page_unknown")
            for row in page_rows:
                if not isinstance(row, Mapping):
                    raise ValueError("reward_row_unknown")
                rows.append(row)
            next_cursor = payload.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("reward_pagination_unknown")
            if next_cursor == "LTE=":
                return rows
            if next_cursor in seen_cursors:
                raise ValueError("reward_pagination_loop")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def lp_snapshot(self, request: Mapping[str, object]) -> dict[str, object]:
        """Read the authenticated and public facts used by one LP session.

        This adapter deliberately returns order, trade, position, market and
        book facts rather than a precomputed LP result.  The LP service owns
        attribution and risk arithmetic after these facts have been read.
        """

        market_id = str(request.get("market_id") or "")
        condition_id = str(request.get("condition_id") or "")
        token_id = str(request.get("token_id") or "")
        if not market_id or not condition_id or not token_id:
            raise ValueError("external_snapshot_unknown")
        try:
            account = self.account_snapshot()
            open_orders = tuple(_collect(self._client.list_open_orders()))
            trades = tuple(
                _collect(
                    self._client.list_account_trades(
                        token_id=token_id,
                        market=condition_id,
                    )
                )
            )
            order_facts: list[object] = list(open_orders)
            known_ids = {
                str(_field(order, "id", _field(order, "order_id", "")))
                for order in order_facts
            }
            for key in ("entry_order_id", "passive_exit_order_id", "protected_exit_order_id"):
                order_id = str(request.get(key) or "")
                if not order_id or order_id in known_ids:
                    continue
                get_order = getattr(self._client, "get_order", None)
                if not callable(get_order):
                    continue
                try:
                    order = get_order(order_id=order_id)
                except Exception:
                    continue
                order_facts.append(order)
                known_ids.add(order_id)

            public = self._public_client_factory()
            try:
                market_model = public.get_market(id=market_id)
                book_model = public.get_order_book(token_id=token_id)
                # Capture local receipt time at the successful REST boundary;
                # the venue timestamp remains source metadata on the book.
                book_received_at = datetime.now(UTC)
            finally:
                close = getattr(public, "close", None)
                if callable(close):
                    close()
            market = _model_dict(market_model)
            book = _lp_book(book_model)
            if market is None or book is None:
                raise ValueError("external_snapshot_unknown")
            book["received_at"] = book_received_at
            state = _model_dict(market.get("state")) or {}
            outcomes = _model_dict(market.get("outcomes")) or {}
            selected = str(request.get("outcome") or "").lower()
            selected_outcome = _model_dict(outcomes.get(selected)) or {}
            trading = _model_dict(market.get("trading")) or {}
            rewards = _model_dict(market.get("rewards")) or {}
            fee_schedule = _model_dict(trading.get("fee_schedule")) or {}
            fees_enabled = trading.get("fees_enabled")
            reward_min = rewards.get("rewards_min_size")
            reward_spread = rewards.get("rewards_max_spread")
            if reward_min is None or reward_spread is None:
                reward_reader = getattr(self._client, "list_market_rewards", None)
                if callable(reward_reader):
                    reward_rows = tuple(_collect(reward_reader(condition_id=condition_id)))
                    if reward_rows:
                        reward = _model_dict(reward_rows[0]) or {}
                        reward_min = reward.get("rewards_min_size")
                        reward_spread = reward.get("rewards_max_spread")
            tick_size = book.get("tick_size", trading.get("minimum_tick_size"))
            minimum_order_size = book.get("min_order_size", trading.get("minimum_order_size"))
            taker_rate = fee_schedule.get("rate")
            if taker_rate is None:
                taker_rate = market.get("taker_fee_rate")
            if taker_rate is None and fees_enabled is not False:
                raise ValueError("fee_rate_unknown")
            raw_reward_spread = _lp_decimal(reward_spread)
            normalized_reward_spread = (
                None
                if raw_reward_spread is None
                else raw_reward_spread / Decimal("100")
            )
            fee_value: Decimal | None
            if fees_enabled is False or fee_schedule.get("taker_only") is True:
                fee_value = Decimal("0")
            else:
                fee_value = None
            market_facts = {
                "market_id": market.get("id"),
                "condition_id": market.get("condition_id"),
                "token_id": selected_outcome.get("token_id"),
                "outcome": str(
                    selected_outcome.get("label") or request.get("outcome") or ""
                ).upper(),
                "accepting_orders": state.get("accepting_orders"),
                "exchange_type": "CLOB",
                "tick_size": _lp_decimal(tick_size),
                "minimum_order_size": _lp_decimal(minimum_order_size),
                "fee": fee_value,
                "fees_enabled": fees_enabled,
                "fee_exponent": _lp_decimal(fee_schedule.get("exponent", 1)),
                "taker_fee_rate": _lp_decimal(taker_rate),
                "reward_min_size": _lp_decimal(reward_min),
                "reward_max_spread": normalized_reward_spread,
            }
            account_facts = {
                "authenticated": True,
                "balance": account.p_usd_balance,
                "allowance": account.p_usd_allowance,
                "positions": list(account.positions),
                "open_orders": [
                    normalized
                    for order in order_facts
                    if (normalized := _lp_order(order)) is not None
                ],
                "checked_at": account.checked_at,
            }
            order_rows = [
                normalized
                for order in order_facts
                if (normalized := _lp_order(order)) is not None
            ]
            order_ids = {
                str(request.get(key) or "")
                for key in ("entry_order_id", "passive_exit_order_id", "protected_exit_order_id")
            } - {""}
            order_statuses = {
                str(_field(order, "id", _field(order, "order_id", ""))): str(
                    _field(order, "status", "")
                ).upper()
                for order in order_rows
            }
            orders_terminal = not order_ids or all(
                order_statuses.get(order_id) in {
                    "FILLED",
                    "MATCHED",
                    "CANCELED",
                    "CANCELLED",
                    "REJECTED",
                    "EXPIRED",
                    "FAILED",
                }
                for order_id in order_ids
            )
            trade_rows = [
                normalized
                for trade in trades
                if (normalized := _lp_trade(trade)) is not None
            ]
            result: dict[str, object] = {
                "account": account_facts,
                "market": market_facts,
                "book": book,
                "orders": order_rows,
                "trades": trade_rows,
                "orders_terminal": orders_terminal,
                "position_flat": not any(
                    str(_field(position, "token_id", _field(position, "asset_id", "")))
                    == token_id
                    and (_decimal(_field(position, "size", 0)) > 0)
                    for position in account.positions
                ),
                "account_checked_at": account.checked_at,
                "book_checked_at": book.get("received_at"),
            }
            return result
        except PolymarketTradingError:
            raise ValueError("external_snapshot_unknown") from None
        except ValueError:
            raise
        except Exception:
            raise ValueError("external_snapshot_unknown") from None

    def lp_create_limit_order(self, **kwargs: object) -> object:
        """Create one explicit post-only GTD signed LP order."""

        expiration = kwargs.get("expiration")
        post_only = kwargs.get("post_only")
        signed = self._client.create_limit_order(
            token_id=str(kwargs["token_id"]),
            price=cast(Decimal, kwargs["price"]),
            size=cast(Decimal, kwargs["quantity"]),
            side=cast(str, kwargs["side"]),
            post_only=post_only is True,
            expiration=cast(int | None, expiration),
        )
        if post_only is not True or expiration is None:
            raise PolymarketTradingError("order_shape_mismatch")
        if _field(signed, "post_only") is not True or str(_field(signed, "order_type", "")).upper() != "GTD":
            raise PolymarketTradingError("order_shape_mismatch")
        return signed

    def lp_post_order(self, signed_order: object) -> object:
        return self._client.post_order(signed_order)

    def get_order_scoring(self, order_id: str) -> bool:
        return self._client.get_order_scoring(order_id=order_id) is True

    def submit_protected_sell(
        self, *, token_id: str, quantity: Decimal, min_price: Decimal
    ) -> object:
        if min_price <= 0 or quantity <= 0:
            raise ValueError("protected_exit_floor_invalid")
        signed = self._client.create_market_order(
            token_id=token_id,
            side="SELL",
            shares=quantity,
            min_price=min_price,
            order_type="FOK",
        )
        return self._client.post_order(signed)

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
        max_spend: Decimal | None = None,
    ) -> object:
        return self._client.create_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            max_spend=max_spend if max_spend is not None else amount,
            max_price=max_price,
            order_type="FOK",
        )

    def submit_n_leg_leg_once(
        self,
        *,
        client_order_id: str,
        token_id: str,
        quantity_lots: int,
        max_cost_units: int,
        timeout_seconds: int = 15,
    ) -> dict[str, object]:
        """Issue #64: ONE FOK BUY attempt for one N-leg batch leg.

        Exactly one attempt is made — a timeout, exception or unrecognized
        receipt is reported as ``UNKNOWN`` and the caller must open an
        incident; this adapter never retries. Units follow the N-leg
        convention (1,000,000 units per $); the whole FOK fill is bounded by
        ``max_cost_units``, so a successful fill books that conservative
        upper bound as the cumulative cost.
        """
        if type(quantity_lots) is not int or quantity_lots <= 0:
            return {"state": "UNKNOWN", "error_code": "invalid_quantity"}
        if type(max_cost_units) is not int or max_cost_units <= 0:
            return {"state": "UNKNOWN", "error_code": "invalid_cost_bound"}
        max_price = Decimal(max_cost_units) / (
            Decimal(quantity_lots) * _NLEG_UNITS_PER_DOLLAR
        )
        amount = Decimal(quantity_lots)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                self._sign_leg,
                token_id=str(token_id),
                amount=amount,
                max_price=max_price,
                max_spend=Decimal(max_cost_units) / _NLEG_UNITS_PER_DOLLAR,
            )
            try:
                response = future.result(timeout=max(1, int(timeout_seconds)))
            except FuturesTimeoutError:
                future.cancel()
                return {"state": "UNKNOWN", "error_code": "timeout"}
        except Exception as exc:
            code = _safe_error_code(exc)
            del exc
            return {"state": "UNKNOWN", "error_code": code or "submit_error"}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return _interpret_fok_response(response)

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
        self._last_submit_error = None
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
        except Exception as exc:
            # A POST may have reached the venue; never retry or claim rejection.
            self._last_submit_error = _submit_error_detail(exc)
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
            # The venue fee is budgeted in intent.maximum_fee. If max_spend only
            # covered leg.max_cost, the SDK would shrink the buy to fit the fee
            # inside it and sign fewer shares than intent.quantity. One cent is
            # added because the SDK shrinks on <=; it only widens the SDK's
            # internal prep cap (not the signed order) and keeps the boundary
            # valid when one leg consumes the entire fee budget.
            signed_a = self._sign_leg(
                token_id=intent.leg_a.token_id,
                amount=intent.leg_a.max_cost,
                max_price=intent.leg_a.max_price,
                max_spend=intent.leg_a.max_cost + intent.maximum_fee + CENT,
            )
            signed_b = self._sign_leg(
                token_id=intent.leg_b.token_id,
                amount=intent.leg_b.max_cost,
                max_price=intent.leg_b.max_price,
                max_spend=intent.leg_b.max_cost + intent.maximum_fee + CENT,
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
        self._last_submit_error = None
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
        except Exception as exc:
            self._last_submit_error = _submit_error_detail(exc)
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
