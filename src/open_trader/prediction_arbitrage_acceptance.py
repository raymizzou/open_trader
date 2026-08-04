"""Prediction-market acceptance registry with strict no-submit live evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import subprocess
import time
from types import MethodType
from typing import Callable, Iterable, Iterator, Mapping
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .polymarket_trading import (
    KeychainError,
    PolymarketTradingError,
    PolymarketTradingClient,
    PredictConfig,
    TradingConfig,
    load_trading_config,
)
from .predict_source import PredictSource
from .predict_trading import PREDICT_BASE_UNITS, PredictTradingClient


SCENARIO_IDS = (
    "MON-01", "MON-02", "MON-03", "MON-04", "MON-05",
    "MON-06", "MON-07", "MON-08", "MON-09", "MON-10",
    "PRE-01", "PRE-02", "PRE-03", "PRE-04", "PRE-05",
    "PRE-06", "PRE-07", "PRE-08", "PRE-09",
    "SEC-01", "SEC-02", "SEC-03", "SEC-04",
    "EXE-01", "EXE-02", "EXE-03", "EXE-04", "EXE-05",
    "EXE-06", "EXE-07", "EXE-08", "EXE-09", "EXE-10",
    "REC-01", "REC-02", "REC-03", "REC-04", "REC-05",
    "RST-01", "RST-02",
    "HIS-01", "HIS-02", "HIS-03",
    "UI-01", "UI-02", "UI-03", "UI-04", "UI-05",
    "UI-06", "UI-07", "UI-08", "UI-09", "UI-10",
    "UI-11", "UI-12", "UI-13", "UI-14",
    "LIVE-01", "LIVE-02", "LIVE-03",
    "OPS-01", "OPS-02", "OPS-03",
)

LIVE_SCENARIO_IDS = frozenset({"LIVE-01", "LIVE-02", "LIVE-03"})
_READY = "PASS"
_BLOCKED = "BLOCKED"
_FAILED = "FAIL"
_NOT_APPLICABLE = "NOT_APPLICABLE"
_NO_PREDICT_MARKET = object()


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    status: str
    detail: str
    facts: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LiveReadinessReport:
    predict_market: ReadinessCheck
    predict_account: ReadinessCheck
    predict_preflight: ReadinessCheck
    polymarket: ReadinessCheck
    browser: ReadinessCheck
    safety: ReadinessCheck
    mutation_calls: int
    live_notifications: int

    @property
    def status(self) -> str:
        statuses = tuple(
            check.status
            for check in (
                self.predict_market,
                self.predict_account,
                self.predict_preflight,
                self.polymarket,
                self.browser,
                self.safety,
            )
            if check.status != _NOT_APPLICABLE
        )
        if _FAILED in statuses:
            return _FAILED
        return _BLOCKED if _BLOCKED in statuses else _READY

    def as_dict(self) -> dict[str, object]:
        return {
            "predict": {
                "market_book_rest_ws": _check_dict(self.predict_market),
                "account_jwt_balance_allowance": _check_dict(self.predict_account),
                "signed_not_submitted_preflight": _check_dict(self.predict_preflight),
            },
            "polymarket": {
                "source_account_preflight": _check_dict(self.polymarket),
            },
            "browser": _check_dict(self.browser),
            "safety": {
                "zero_mutation_calls": self.mutation_calls,
                "zero_live_notifications": self.live_notifications,
                **_check_dict(self.safety),
            },
            "status": self.status,
        }


class ReadOnlyTransport:
    """Permit source reads and Predict authentication, never an order request."""

    def __init__(self, opener: Callable[..., object] = urlopen) -> None:
        self._opener = opener
        self.mutation_calls = 0
        self.live_notifications = 0

    def __call__(self, request: Request, **kwargs: object) -> object:
        path = urlparse(request.full_url).path
        method = request.get_method().upper()
        if method != "GET" and not (method == "POST" and path == "/v1/auth"):
            self.mutation_calls += 1
            raise RuntimeError("mutation prohibited")
        return self._opener(request, **kwargs)


class _ExternalUnavailable(RuntimeError):
    """Internal marker for an unavailable external dependency."""


class _PolymarketMutationViolation(RuntimeError):
    """Internal marker for a guarded Polymarket mutation or notification."""


class _PredictMutationViolation(RuntimeError):
    """Internal marker for a guarded Predict mutation."""


class _GuardedCallable:
    """Call an SDK method with a guarded proxy as its self object."""

    __slots__ = ("_guard", "_target", "_function", "_proxy_self")

    def __init__(
        self,
        guard: "_PolymarketReadOnlyGuard",
        target: Callable[..., object] | None = None,
        *,
        function: Callable[..., object] | None = None,
        proxy_self: object | None = None,
    ) -> None:
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_function", function)
        object.__setattr__(self, "_proxy_self", proxy_self)

    def __getattribute__(self, name: str) -> object:
        if name in {"_guard", "_target", "_function", "_proxy_self"}:
            guard = object.__getattribute__(self, "_guard")
            guard.violation("raw_internal")
        return object.__getattribute__(self, name)

    def __call__(self, *args: object, **kwargs: object) -> object:
        guard = object.__getattribute__(self, "_guard")
        function = object.__getattribute__(self, "_function")
        if function is not None:
            result = function(
                object.__getattribute__(self, "_proxy_self"), *args, **kwargs
            )
        else:
            target = object.__getattribute__(self, "_target")
            name = getattr(target, "__name__", "")
            result = (
                guard.call(name, target, *args, **kwargs)
                if name.lower() in {"request", "send"}
                else target(*args, **kwargs)  # type: ignore[misc]
            )
        return guard.protect(result)


class _PolymarketReadOnlyGuard:
    """Hard-fail any mutation-capable SDK or transport call during preflight."""

    _NOTIFICATION_NAMES = frozenset(
        {
            "notify",
            "notify_live",
            "send_notification",
            "drop_notifications",
        }
    )
    _MUTATION_NAMES = frozenset(
        {
            "post",
            "post_json",
            "post_order",
            "post_orders",
            "redeem",
            "place_limit_order",
            "place_market_order",
            "cancel_order",
            "cancel_orders",
            "cancel_all",
            "cancel_market_orders",
            "merge_positions",
            "merge_multiple_positions",
            "redeem_positions",
            "split_position",
            "execute_transaction",
            "approve",
            "set_approval",
            "clear_approval",
            "cleanup",
            "cleanup_allowance",
            "transfer",
            "transfer_usdt",
            "withdraw",
            "delete",
            "delete_json",
            "put",
            "put_json",
            "patch",
            "patch_json",
        }
    )
    def __init__(self) -> None:
        self.mutation_calls = 0
        self.live_notifications = 0

    def _kind(self, name: str) -> str | None:
        normalized = name.lower()
        if normalized in self._NOTIFICATION_NAMES or normalized.startswith("notify"):
            return "notification"
        if normalized in self._MUTATION_NAMES or normalized.startswith(
            (
                "post_",
                "place_",
                "cancel_",
                "merge_",
                "redeem_",
                "split_",
                "execute_",
                "approve_",
                "set_",
                "clear_",
                "cleanup_",
                "transfer_",
                "withdraw_",
                "delete_",
                "put_",
                "patch_",
                "submit_",
            )
        ):
            return "mutation"
        return None

    def violation(self, name: str, *, kind: str | None = None) -> None:
        kind = kind or self._kind(name)
        if kind == "notification":
            self.live_notifications += 1
        else:
            self.mutation_calls += 1
        raise _PolymarketMutationViolation("Polymarket mutation or notification prohibited")

    def protect(self, value: object, *, notification_scope: bool = False) -> object:
        if value is None or isinstance(value, (str, bytes, bytearray, bool, int, float, Decimal)):
            return value
        if isinstance(value, (_GuardedCallable, _GuardedPolymarketValue)):
            return value
        if isinstance(value, MethodType):
            if value.__name__.lower() in {"request", "send", "create_market_order"}:
                return _GuardedCallable(self, target=value)
            return _GuardedCallable(
                self,
                function=value.__func__,
                proxy_self=self.wrap(value.__self__, notification_scope=notification_scope),
            )
        if callable(value):
            call = getattr(value, "__call__", None)
            if isinstance(call, MethodType):
                return _GuardedCallable(
                    self,
                    function=call.__func__,
                    proxy_self=self.wrap(value, notification_scope=notification_scope),
                )
            return _GuardedCallable(self, target=value)
        return self.wrap(value)

    def call(self, name: str, target: Callable[..., object], *args: object, **kwargs: object) -> object:
        if name.lower() in {"request", "send"}:
            method = kwargs.get("method")
            if method is None and args and isinstance(args[0], str):
                method = args[0]
            if method is None and args and hasattr(args[0], "get_method"):
                method = args[0].get_method()
            if method is None and args:
                method = getattr(args[0], "method", None)
            if name.lower() == "send" and method is None:
                return target(*args, **kwargs)
            allowed = {"GET", "HEAD", "OPTIONS"} if name.lower() == "send" else {"GET"}
            if str(method).upper() not in allowed:
                self.violation(name)
            return target(*args, **kwargs)
        self.violation(name)
        return None

    def wrap(self, value: object, *, notification_scope: bool = False) -> "_GuardedPolymarketValue":
        return _GuardedPolymarketValue(value, self, notification_scope=notification_scope)


class _GuardedPolymarketValue:
    __slots__ = ("_value", "_guard", "_notification_scope")

    def __init__(
        self,
        value: object,
        guard: _PolymarketReadOnlyGuard,
        *,
        notification_scope: bool = False,
    ) -> None:
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_notification_scope", notification_scope)

    def __getattribute__(self, name: str) -> object:
        if name in {"_value", "_guard"}:
            guard = object.__getattribute__(self, "_guard")
            guard.violation("raw_internal")
        return object.__getattribute__(self, name)

    @property
    def __class__(self) -> type[object]:
        # Preserve isinstance checks made by the real Polymarket adapter.
        return type(object.__getattribute__(self, "_value"))

    def __getattr__(self, name: str) -> object:
        guard = object.__getattribute__(self, "_guard")
        notification_scope = object.__getattribute__(self, "_notification_scope")
        kind = guard._kind(name)
        if kind is not None:
            return lambda *args, **kwargs: guard.violation(name)
        if name.lower() == "send" and notification_scope:
            return lambda *args, **kwargs: guard.violation(name, kind="notification")
        value = getattr(object.__getattribute__(self, "_value"), name)
        if name.lower() == "request" and callable(value):
            guarded = guard.protect(value, notification_scope=notification_scope)
            return lambda *args, **kwargs: guard.call(name, guarded, *args, **kwargs)
        child_scope = notification_scope or name.lower() in {
            "notifier",
            "_notifier",
            "notification",
            "_notification",
        }
        return guard.protect(value, notification_scope=child_scope)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_value", "_guard"}:
            guard = object.__getattribute__(self, "_guard")
            guard.violation("raw_internal")
        setattr(object.__getattribute__(self, "_value"), name, value)

    def __call__(self, *args: object, **kwargs: object) -> object:
        guard = object.__getattribute__(self, "_guard")
        value = object.__getattribute__(self, "_value")
        return guard.call("__call__", value, *args, **kwargs)

    def __getitem__(self, key: object) -> object:
        guard = object.__getattribute__(self, "_guard")
        value = object.__getattribute__(self, "_value")[key]  # type: ignore[index]
        return guard.protect(
            value,
            notification_scope=object.__getattribute__(self, "_notification_scope"),
        )

    def __iter__(self) -> Iterator[object]:
        guard = object.__getattribute__(self, "_guard")
        scope = object.__getattribute__(self, "_notification_scope")
        return (guard.protect(value, notification_scope=scope) for value in object.__getattribute__(self, "_value"))  # type: ignore[union-attr]

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_value"))  # type: ignore[arg-type]


class _PredictReadOnlyGuard(_PolymarketReadOnlyGuard):
    """Use the SDK proxy machinery while reporting Predict mutations separately."""

    _MUTATION_NAMES = _PolymarketReadOnlyGuard._MUTATION_NAMES | frozenset(
        {
            "approval",
            "redemption",
            "send_raw_transaction",
            "submit_order",
            "submit_orders",
            "transact",
        }
    )

    def _kind(self, name: str) -> str | None:
        normalized = name.lower()
        if normalized.startswith(("convert_positions", "run_approval")):
            return "mutation"
        return super()._kind(name)

    def violation(self, name: str, *, kind: str | None = None) -> None:
        del name, kind
        self.mutation_calls += 1
        raise _PredictMutationViolation("Predict mutation prohibited")


@contextmanager
def _guard_predict_client(
    client: object, guard: _PredictReadOnlyGuard
) -> Iterator[None]:
    """Install a reversible guard around the Predict client and nested builder."""

    try:
        builder = getattr(client, "_builder")
        setattr(client, "_builder", guard.wrap(builder))
    except Exception:
        raise RuntimeError("Predict read-only guard unavailable") from None

    direct_replacements: list[tuple[str, bool, object]] = []
    direct_names = set(guard._MUTATION_NAMES)
    direct_names.update(name for name in dir(client) if guard._kind(name) == "mutation")
    try:
        for name in direct_names:
            try:
                previous = getattr(client, name)
            except AttributeError:
                had_previous = False
                previous = None
            else:
                had_previous = True
            setattr(
                client,
                name,
                lambda *args, _name=name, **kwargs: guard.violation(_name),
            )
            direct_replacements.append((name, had_previous, previous))
    except Exception:
        for name, had_previous, previous in reversed(direct_replacements):
            if had_previous:
                setattr(client, name, previous)
            else:
                delattr(client, name)
        setattr(client, "_builder", builder)
        raise RuntimeError("Predict read-only guard unavailable") from None
    try:
        yield
    finally:
        for name, had_previous, previous in reversed(direct_replacements):
            if had_previous:
                setattr(client, name, previous)
            else:
                delattr(client, name)
        setattr(client, "_builder", builder)


@contextmanager
def _guard_polymarket_client(
    client: object, guard: _PolymarketReadOnlyGuard
) -> Iterator[None]:
    """Install a reversible guard around the SDK and notifier boundaries."""

    replacements: list[tuple[str, object]] = []
    direct_replacements: list[tuple[str, bool, object]] = []
    for name in (
        "_client",
        "_ctx",
        "_ctx_inner",
        "notifier",
        "_notifier",
        "notification",
        "_notification",
    ):
        try:
            value = getattr(client, name)
        except AttributeError:
            continue
        try:
            setattr(
                client,
                name,
                guard.wrap(
                    value,
                    notification_scope=name.lower()
                    in {"notifier", "_notifier", "notification", "_notification"},
                ),
            )
        except Exception:
            for restored_name, restored_value in reversed(replacements):
                setattr(client, restored_name, restored_value)
            raise RuntimeError("Polymarket read-only guard unavailable") from None
        replacements.append((name, value))

    direct_names = set(guard._MUTATION_NAMES) | set(guard._NOTIFICATION_NAMES) | {"request", "send"}
    direct_names.update(name for name in dir(client) if guard._kind(name) is not None)
    try:
        for name in direct_names:
            try:
                previous = getattr(client, name)
            except AttributeError:
                had_previous = False
                previous = None
            else:
                had_previous = True
            if name == "request" and had_previous and callable(previous):
                replacement = lambda *args, _target=previous, **kwargs: guard.call(
                    name, _target, *args, **kwargs
                )
            else:
                replacement = lambda *args, _name=name, **kwargs: guard.violation(_name)
            setattr(client, name, replacement)
            direct_replacements.append((name, had_previous, previous))
    except Exception:
        for name, had_previous, previous in reversed(direct_replacements):
            if had_previous:
                setattr(client, name, previous)
            else:
                delattr(client, name)
        for restored_name, restored_value in reversed(replacements):
            setattr(client, restored_name, restored_value)
        raise RuntimeError("Polymarket read-only guard unavailable") from None
    if not any(name == "_client" for name, _ in replacements):
        for name, had_previous, previous in reversed(direct_replacements):
            if had_previous:
                setattr(client, name, previous)
            else:
                delattr(client, name)
        for restored_name, restored_value in reversed(replacements):
            setattr(client, restored_name, restored_value)
        raise RuntimeError("Polymarket read-only guard unavailable")
    try:
        yield
    finally:
        for name, had_previous, previous in reversed(direct_replacements):
            if had_previous:
                setattr(client, name, previous)
            else:
                delattr(client, name)
        for name, value in reversed(replacements):
            setattr(client, name, value)


def _check_dict(check: ReadinessCheck) -> dict[str, str]:
    return {
        "status": check.status,
        "detail": check.detail,
        **(dict(check.facts) if check.facts is not None else {}),
    }


def _blocked(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_BLOCKED, f"BLOCKED: {detail}")


def _failed(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_FAILED, f"FAIL: {detail}")


def _ready(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_READY, detail)


def _ready_with_facts(detail: str, facts: Mapping[str, str]) -> ReadinessCheck:
    return ReadinessCheck(_READY, detail, facts)


def _not_applicable(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_NOT_APPLICABLE, detail)


def _http_status(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_external_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, _ExternalUnavailable):
        return True
    if isinstance(exc, PolymarketTradingError):
        return exc.error_code in {"network", "timeout", "unavailable"}
    status = _http_status(exc)
    if status is not None:
        return status == 429 or status >= 500
    return isinstance(exc, (ConnectionError, OSError, TimeoutError, URLError))


def _blocked_report(
    detail: str, *, browser: ReadinessCheck | None = None
) -> LiveReadinessReport:
    check = _blocked(detail)
    return LiveReadinessReport(
        check,
        check,
        check,
        check,
        browser or check,
        _ready("zero mutation calls; zero live notifications"),
        0,
        0,
    )


def _failed_report(
    detail: str, *, browser: ReadinessCheck | None = None
) -> LiveReadinessReport:
    check = _failed(detail)
    return LiveReadinessReport(
        check,
        check,
        check,
        check,
        browser or check,
        _ready("zero mutation calls; zero live notifications"),
        0,
        0,
    )


async def _predict_market_book(source: object, *, timeout: float) -> object:
    markets = await asyncio.wait_for(source.list_open_markets(limit=1), timeout=timeout)  # type: ignore[attr-defined]
    if not markets:
        _raise_source_status(source, "rest")
        return _NO_PREDICT_MARKET
    market = markets[0]
    book = await asyncio.wait_for(source.get_order_book(market.market_id), timeout=timeout)  # type: ignore[attr-defined]
    if book is None:
        _raise_source_status(source, "rest")
        raise RuntimeError
    stream = source.stream_books([market.market_id])  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(anext(stream), timeout=timeout)
    except StopAsyncIteration:
        _raise_source_status(source, "ws")
        raise RuntimeError from None
    finally:
        await stream.aclose()
    return market


def _raise_source_status(source: object, channel: str) -> None:
    snapshot = getattr(source, "snapshot", lambda: {})()
    if not isinstance(snapshot, Mapping):
        return
    status = snapshot.get(channel)
    reason = snapshot.get(f"{channel}_reason")
    if status == "pending" or reason == "api_key_pending":
        raise KeychainError("keychain_unavailable")
    if status == "unavailable" or reason == "network_unavailable":
        raise _ExternalUnavailable("external source unavailable")
    if status == "stale" or reason in {"rest_stale", "ws_stale"}:
        raise RuntimeError("invalid source data")


def _predict_quantity(market: object) -> int:
    try:
        minimum = Decimal(str(market.minimum_order_size))  # type: ignore[attr-defined]
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise ValueError("invalid market minimum") from exc
    quantity = minimum * Decimal(10**18)
    if minimum <= 0 or not minimum.is_finite() or quantity != quantity.to_integral_value():
        raise ValueError("invalid market minimum")
    return int(quantity)


def _decimal_fact(facts: Mapping[str, object], key: str) -> Decimal:
    value = Decimal(str(facts[key]))
    if not value.is_finite() or value < 0:
        raise ValueError(key)
    return value


def _predict_account_check(client: object, market: object | None) -> ReadinessCheck:
    try:
        jwt = client._authenticate()  # type: ignore[attr-defined]
        if market is None:
            return _blocked("Predict market/account readiness unavailable")
        facts = (
            client.account_snapshot()  # type: ignore[attr-defined]
            if market is _NO_PREDICT_MARKET
            else client.approval_facts(market.market_id, exact_debit_wei=0)  # type: ignore[attr-defined]
        )
    except KeychainError:
        return _blocked("Predict Keychain environment unavailable")
    except Exception as exc:
        if _is_external_unavailable(exc):
            return _blocked("Predict account environment unavailable")
        return _failed("predict account read failed")
    try:
        predict_account = facts["predict_account"]
        gas_signer = facts["gas_signer"]
        balance = _decimal_fact(facts, "available_usdt")
        balance_raw = _decimal_fact(facts, "available_usdt_raw")
        allowance = _decimal_fact(facts, "allowance")
        allowance_raw = _decimal_fact(facts, "allowance_raw")
        bnb_balance = _decimal_fact(facts, "bnb_balance")
        required_bnb = _decimal_fact(facts, "required_bnb")
        minimum_top_up_bnb = _decimal_fact(facts, "minimum_top_up_bnb")
    except (KeyError, InvalidOperation, ValueError):
        return _failed("predict account read failed")
    if (
        not isinstance(jwt, str)
        or not jwt
        or not isinstance(predict_account, str)
        or not predict_account
        or not isinstance(gas_signer, str)
        or not gas_signer
        or facts.get("scope_ready") is not True
        or facts.get("allowance_breaker") is not False
        or balance * Decimal(PREDICT_BASE_UNITS) != balance_raw
        or allowance != 0
        or allowance_raw != 0
        or allowance * Decimal(PREDICT_BASE_UNITS) != allowance_raw
    ):
        return _failed("predict account read failed")
    wallet_address = facts.get("wallet_address")
    if wallet_address is not None and (
        not isinstance(wallet_address, str)
        or not wallet_address
        or wallet_address.casefold() != predict_account.casefold()
    ):
        return _failed("predict account read failed")
    if minimum_top_up_bnb > 0 or bnb_balance < required_bnb:
        return _failed("Predict signer BNB unavailable")
    return _ready_with_facts(
        "Predict JWT, balance, allowance, and signer gas ready",
        {
            "gas_signer": gas_signer,
            "bnb_balance": str(facts["bnb_balance"]),
            "required_bnb": str(facts["required_bnb"]),
            "minimum_top_up_bnb": str(facts["minimum_top_up_bnb"]),
        },
    )


def _predict_preflight_check(client: object, market: object) -> ReadinessCheck:
    try:
        result = client.no_submit_buy_preflight(  # type: ignore[attr-defined]
            market.market_id, market.yes_token_id, _predict_quantity(market)
        )
    except KeychainError:
        return _blocked("Predict Keychain environment unavailable")
    except Exception as exc:
        if _is_external_unavailable(exc):
            return _blocked("Predict preflight environment unavailable")
        return _failed("predict no-submit preflight failed")
    error_code = getattr(result, "error_code", "none")
    if error_code in {"network", "timeout", "unavailable"}:
        return _blocked("Predict preflight environment unavailable")
    if (
        getattr(result, "accepted", False) is not True
        or getattr(result, "status", "") != "preflight"
        or error_code != "none"
    ):
        return _failed("predict no-submit preflight failed")
    return _ready("Predict order signed but not submitted")


def _polymarket_check(client: object) -> ReadinessCheck:
    try:
        report = client.preflight_report()  # type: ignore[attr-defined]
    except KeychainError:
        return _blocked("Polymarket Keychain environment unavailable")
    except PolymarketTradingError as exc:
        if exc.error_code in {"network", "timeout", "unavailable", "geoblock_blocked"}:
            return _blocked("Polymarket external environment unavailable")
        return _failed("Polymarket source/account/preflight read failed")
    except Exception as exc:
        if isinstance(exc, _PolymarketMutationViolation):
            return _failed("Polymarket mutation or live notification attempted")
        if _is_external_unavailable(exc):
            return _blocked("Polymarket external environment unavailable")
        return _failed("Polymarket source/account/preflight read failed")
    if not isinstance(report, Mapping):
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("result") != _READY:
        if report.get("error_code") in {
            "network",
            "timeout",
            "unavailable",
            "geoblock_blocked",
        }:
            return _blocked("Polymarket external environment unavailable")
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("error_code", "none") != "none":
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("posted") is not False:
        return _failed("Polymarket preflight did not attest no mutation")
    if report.get("account_reads") != "pass" or report.get("fok_pair_signed_not_submitted") != "pass":
        return _failed("Polymarket source/account/preflight read failed")
    return _ready("Polymarket source, account, and no-submit preflight ready")


def run_live_readiness(
    config_path: Path,
    *,
    load_config: Callable[[Path], TradingConfig] = load_trading_config,
    predict_source_factory: Callable[..., object] = PredictSource,
    predict_client_factory: Callable[..., object] = PredictTradingClient.from_keychain,
    polymarket_client_factory: Callable[[TradingConfig], object] = PolymarketTradingClient.from_keychain,
    timeout: float = 10.0,
    browser_handoff_path: Path | None = None,
    browser_nonce_path: Path | None = None,
    browser_url: str = "http://127.0.0.1:8766",
    browser_fixture_url: str = "http://127.0.0.1:18766",
    browser_root: Path | None = None,
    browser_now_fn: Callable[[], float] = time.time,
    browser_commit_fn: Callable[[Path], str | None] | None = None,
    browser_health_fn: Callable[[str], bool] | None = None,
) -> LiveReadinessReport:
    """Run only real read/auth/sign checks; no order or notification is allowed."""

    browser = _browser_readiness(
        browser_handoff_path,
        browser_nonce_path,
        browser_url,
        browser_fixture_url,
        browser_root or Path.cwd(),
        health_fn=browser_health_fn or _browser_health,
        now_fn=browser_now_fn,
        commit_fn=browser_commit_fn or _git_commit,
    )
    if browser.status != _READY:
        return _blocked_report("browser readiness unavailable", browser=browser)
    if not config_path.is_file():
        return _blocked_report("prediction configuration unavailable", browser=browser)
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        return _blocked_report("prediction configuration unavailable", browser=browser)
    except Exception:
        return _failed_report("prediction configuration invalid", browser=browser)
    if config.predict is None:
        return _blocked_report("Predict configuration unavailable", browser=browser)

    transport = ReadOnlyTransport()
    market: object | None = None
    try:
        source = predict_source_factory(config.predict, urlopen_fn=transport)
        market = asyncio.run(_predict_market_book(source, timeout=timeout))
        predict_market = (
            _ready("Predict market scan complete; no open V1 market")
            if market is _NO_PREDICT_MARKET
            else _ready("Predict REST and WebSocket market/book ready")
        )
    except KeychainError:
        predict_market = _blocked("Predict Keychain environment unavailable")
    except Exception as exc:
        if _is_external_unavailable(exc):
            predict_market = _blocked("Predict external market environment unavailable")
        else:
            predict_market = _failed("Predict REST/WebSocket market/book read failed")

    predict_guard = _PredictReadOnlyGuard()
    try:
        predict_client = predict_client_factory(config, urlopen_fn=transport)
    except KeychainError:
        predict_client = None
        predict_account = _blocked("Predict Keychain environment unavailable")
    except Exception as exc:
        predict_client = None
        predict_account = (
            _blocked("Predict account environment unavailable")
            if _is_external_unavailable(exc)
            else _failed("predict account read failed")
        )
    else:
        try:
            with _guard_predict_client(predict_client, predict_guard):
                predict_account = _predict_account_check(predict_client, market)
                if market is _NO_PREDICT_MARKET:
                    predict_preflight = _not_applicable(
                        "Predict signed-order construction not applicable; no open V1 market"
                    )
                elif market is None:
                    predict_preflight = _blocked("Predict market/account readiness unavailable")
                elif predict_account.status != _READY:
                    predict_preflight = _blocked("Predict account readiness unavailable")
                else:
                    predict_preflight = _predict_preflight_check(predict_client, market)
        except Exception as exc:
            predict_account = (
                _blocked("Predict account environment unavailable")
                if _is_external_unavailable(exc)
                else _failed("predict account read failed")
            )
            predict_preflight = _blocked("Predict account readiness unavailable")
    if predict_client is None:
        predict_preflight = _blocked("Predict market/account readiness unavailable")

    polymarket_guard = _PolymarketReadOnlyGuard()
    try:
        polymarket_client = polymarket_client_factory(config)
        with _guard_polymarket_client(polymarket_client, polymarket_guard):
            polymarket = _polymarket_check(polymarket_client)
    except KeychainError:
        polymarket = _blocked("Polymarket Keychain environment unavailable")
    except Exception as exc:
        polymarket = (
            _blocked("Polymarket external environment unavailable")
            if _is_external_unavailable(exc)
            else _failed("Polymarket source/account/preflight read failed")
        )

    mutation_calls = (
        transport.mutation_calls
        + predict_guard.mutation_calls
        + polymarket_guard.mutation_calls
    )
    live_notifications = (
        transport.live_notifications
        + predict_guard.live_notifications
        + polymarket_guard.live_notifications
    )
    safety = (
        _ready("zero mutation calls; zero live notifications")
        if mutation_calls == 0 and live_notifications == 0
        else _failed("mutation or live notification attempted")
    )
    return LiveReadinessReport(
        predict_market,
        predict_account,
        predict_preflight,
        polymarket,
        browser,
        safety,
        mutation_calls,
        live_notifications,
    )


def _joined_check(*checks: ReadinessCheck) -> ReadinessCheck:
    if any(check.status == _FAILED for check in checks):
        status = _FAILED
    elif any(check.status == _BLOCKED for check in checks):
        status = _BLOCKED
    else:
        status = _READY
    return ReadinessCheck(status, "; ".join(check.detail for check in checks))


def scenario_results(
    *,
    live_report: LiveReadinessReport | None = None,
) -> tuple[ScenarioResult, ...]:
    """Return fixed acceptance rows; live rows require an explicit real report."""

    if live_report is None:
        live_rows = {
            scenario_id: _blocked("required external/Keychain environment unavailable")
            for scenario_id in LIVE_SCENARIO_IDS
        }
    else:
        live_rows = {
            "LIVE-01": _joined_check(
                live_report.predict_market, live_report.browser, live_report.safety
            ),
            "LIVE-02": _joined_check(
                live_report.predict_account,
                live_report.predict_preflight,
                live_report.browser,
                live_report.safety,
            ),
            "LIVE-03": _joined_check(
                live_report.polymarket, live_report.browser, live_report.safety
            ),
        }
    return tuple(
        ScenarioResult(
            scenario_id,
            live_rows[scenario_id].status if scenario_id in LIVE_SCENARIO_IDS else _READY,
            live_rows[scenario_id].detail if scenario_id in LIVE_SCENARIO_IDS else "deterministic contract",
        )
        for scenario_id in SCENARIO_IDS
    )


def validate_registry(results: Iterable[ScenarioResult]) -> list[str]:
    rows = tuple(results)
    errors: list[str] = []
    ids = tuple(row.scenario_id for row in rows)
    if ids != SCENARIO_IDS:
        errors.append("scenario IDs are missing, duplicated, or out of order")
    if len(set(ids)) != len(SCENARIO_IDS):
        errors.append("scenario IDs are not unique")
    if any(row.status not in {_READY, _FAILED, _BLOCKED} for row in rows):
        errors.append("scenario status is not PASS/FAIL/BLOCKED")
    return errors


def _dashboard_is_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def _browser_health(url: str, timeout: float = 2.0) -> bool:
    try:
        target = url.rstrip("/")
        if not urlparse(target).path.endswith("/healthz"):
            target += "/healthz"
        with urlopen(target, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def _consume_browser_nonce(path: Path | None) -> str | None:
    """Atomically move the pipeline nonce out of its server-owned path once."""

    if path is None:
        return None
    consumed_path = path.with_name(path.name + ".consumed")
    try:
        os.replace(path, consumed_path)
    except OSError:
        return None
    try:
        nonce = consumed_path.read_text(encoding="utf-8").strip()
    except OSError:
        nonce = ""
    finally:
        try:
            os.unlink(consumed_path)
        except OSError:
            pass
    return nonce or None


def _browser_readiness(
    handoff_path: Path | None,
    nonce_path: Path | None,
    url: str,
    fixture_url: str,
    root: Path,
    *,
    health_fn: Callable[[str], bool],
    now_fn: Callable[[], float],
    commit_fn: Callable[[Path], str | None],
) -> ReadinessCheck:
    if handoff_path is None or not handoff_path.is_file():
        return _blocked("Playwright browser handoff unavailable")
    try:
        payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blocked("Playwright browser handoff unavailable")
    if not isinstance(payload, Mapping):
        return _blocked("Playwright browser handoff unavailable")
    if not (
        payload.get("schema_version") == 2
        and payload.get("source") == "playwright"
        and payload.get("playwright_status") == "passed"
        and payload.get("browser_project") == "chromium"
    ):
        return _blocked("Playwright browser handoff unavailable")
    run_nonce = payload.get("run_nonce")
    expected_fixture_url = fixture_url.rstrip("/")
    expected_fixture_health_url = expected_fixture_url + "/healthz"
    if (
        not isinstance(run_nonce, str)
        or not run_nonce
        or payload.get("fixture_url") != expected_fixture_url
        or payload.get("fixture_health_url") != expected_fixture_health_url
    ):
        return _blocked("Playwright browser fixture binding unavailable")
    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    if (
        isinstance(created_at, bool)
        or isinstance(expires_at, bool)
        or not isinstance(created_at, (int, float))
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(float(created_at))
        or not math.isfinite(float(expires_at))
    ):
        return _blocked("Playwright browser handoff unavailable")
    try:
        now = float(now_fn())
        current_commit = commit_fn(root)
    except Exception:
        return _blocked("Playwright browser handoff unavailable")
    if not math.isfinite(now):
        return _blocked("Playwright browser handoff unavailable")
    if (
        float(created_at) > now + 5
        or float(expires_at) <= now
        or now - float(created_at) > 120
        or float(expires_at) - float(created_at) > 120
    ):
        return _blocked("Playwright browser handoff expired")
    expected_url = url.rstrip("/")
    expected_health_url = expected_url + "/healthz"
    if payload.get("review_url") != expected_url or payload.get("health_url") != expected_health_url:
        return _blocked("Playwright browser handoff review URL mismatch")
    if payload.get("candidate_commit") != current_commit:
        return _blocked("Playwright browser handoff candidate mismatch")
    expected_nonce = _consume_browser_nonce(nonce_path)
    if expected_nonce is None:
        return _blocked("Playwright browser nonce unavailable")
    if run_nonce != expected_nonce:
        return _blocked("Playwright browser nonce mismatch")
    try:
        healthy = health_fn(str(payload["health_url"]))
    except Exception:
        healthy = False
    if not healthy:
        return _blocked("browser/dashboard health unavailable")
    return _ready("Playwright browser handoff and dashboard health ready")


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = completed.stdout.strip()
    return commit or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run prediction-market acceptance registry")
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--browser-handoff", type=Path)
    parser.add_argument("--json", action="store_true", help="print the redacted live-readiness report")
    args = parser.parse_args(argv)

    config_path = args.config or args.expected_root / "config" / "prediction_arbitrage.json"
    browser_handoff_path = args.browser_handoff or (
        args.expected_root / "logs/acceptance/prediction-market-browser-handoff.json"
    )
    browser_nonce_path = Path(
        os.environ.get(
            "PREDICTION_ACCEPTANCE_BROWSER_NONCE_FILE",
            str(args.expected_root / "logs/acceptance/prediction-market-browser-nonce"),
        )
    )
    report = run_live_readiness(
        config_path,
        browser_handoff_path=browser_handoff_path,
        browser_nonce_path=browser_nonce_path,
        browser_url=args.url,
        browser_fixture_url="http://127.0.0.1:18766",
        browser_root=args.expected_root,
    )
    if args.json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    results = list(scenario_results(live_report=report))
    errors = validate_registry(results)
    if errors:
        for error in errors:
            print(f"ACCEPTANCE FAIL {error}")
        return 1
    output_results: list[ScenarioResult] = []
    for result in results:
        if result.scenario_id.startswith("OPS-") and not _dashboard_is_reachable(args.url):
            result = ScenarioResult(result.scenario_id, _BLOCKED, "BLOCKED: Dashboard review URL unavailable")
        output_results.append(result)
        print(f"SCENARIO {result.scenario_id} {result.status} {result.detail}")
    statuses = {result.status for result in output_results}
    if _FAILED in statuses:
        return 1
    if _BLOCKED in statuses:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
