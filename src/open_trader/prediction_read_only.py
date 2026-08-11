"""Reusable fail-closed guards for prediction-market read-only work."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import traceback
from types import MethodType
from typing import Callable, Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ReadOnlyViolation(RuntimeError):
    """A blocked SDK, transport, or notification mutation."""

    def __init__(self, attempt: dict[str, object], message: str | None = None) -> None:
        self.attempt = attempt
        super().__init__(message or f"{attempt['venue']} {attempt['kind']} prohibited")


def _attempt(venue: str, kind: str, method: str) -> dict[str, object]:
    return {
        "venue": venue,
        "kind": kind,
        "method": method,
        "call_chain": [
            f"{frame.filename}:{frame.lineno} in {frame.name}"
            for frame in traceback.extract_stack(limit=12)
        ],
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
            raise ReadOnlyViolation(
                _attempt("predict", "mutation", method), "mutation prohibited"
            )
        return self._opener(request, **kwargs)


class _GuardedCallable:
    """Call an SDK method with a guarded proxy as its self object."""

    __slots__ = ("_guard", "_target", "_function", "_proxy_self")

    def __init__(
        self,
        guard: "PolymarketReadOnlyGuard",
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


class PolymarketReadOnlyGuard:
    """Hard-fail any mutation-capable SDK or transport call during preflight."""

    _venue = "polymarket"
    _NOTIFICATION_NAMES = frozenset(
        {"notify", "notify_live", "send_notification", "drop_notifications"}
    )
    _MUTATION_NAMES = frozenset(
        {
            "post", "post_json", "post_order", "post_orders", "redeem",
            "place_limit_order", "place_market_order", "cancel_order", "cancel_orders",
            "cancel_all", "cancel_market_orders", "merge_positions",
            "merge_multiple_positions", "redeem_positions", "split_position",
            "execute_transaction", "approve", "set_approval", "clear_approval",
            "cleanup", "cleanup_allowance", "transfer", "transfer_usdt", "withdraw",
            "delete", "delete_json", "put", "put_json", "patch", "patch_json",
        }
    )

    def __init__(
        self, on_violation: Callable[[dict[str, object]], object] | None = None
    ) -> None:
        self.attempts: list[dict[str, object]] = []
        self.mutation_calls = 0
        self.live_notifications = 0
        self._on_violation = on_violation

    def _kind(self, name: str) -> str | None:
        normalized = name.lower()
        if normalized in self._NOTIFICATION_NAMES or normalized.startswith("notify"):
            return "notification"
        if normalized in self._MUTATION_NAMES or normalized.startswith(
            (
                "post_", "place_", "cancel_", "merge_", "redeem_", "split_",
                "execute_", "approve_", "set_", "clear_", "cleanup_", "transfer_",
                "withdraw_", "delete_", "put_", "patch_", "submit_",
            )
        ):
            return "mutation"
        return None

    def violation(self, name: str, *, kind: str | None = None) -> None:
        kind = kind or self._kind(name) or "mutation"
        if kind == "notification":
            self.live_notifications += 1
        else:
            self.mutation_calls += 1
        attempt = _attempt(self._venue, kind, name)
        self.attempts.append(attempt)
        if self._on_violation is not None:
            try:
                self._on_violation(attempt)
            finally:
                raise ReadOnlyViolation(attempt)
        raise ReadOnlyViolation(attempt)

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

    def call(
        self, name: str, target: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
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

    def wrap(
        self, value: object, *, notification_scope: bool = False
    ) -> "_GuardedPolymarketValue":
        return _GuardedPolymarketValue(value, self, notification_scope=notification_scope)


class _GuardedPolymarketValue:
    __slots__ = ("_value", "_guard", "_notification_scope")

    def __init__(
        self,
        value: object,
        guard: PolymarketReadOnlyGuard,
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
            "notifier", "_notifier", "notification", "_notification"
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
        return (
            guard.protect(value, notification_scope=scope)
            for value in object.__getattribute__(self, "_value")  # type: ignore[union-attr]
        )

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_value"))  # type: ignore[arg-type]


class PredictReadOnlyGuard(PolymarketReadOnlyGuard):
    """Use the SDK proxy machinery while reporting Predict mutations separately."""

    _venue = "predict"
    _MUTATION_NAMES = PolymarketReadOnlyGuard._MUTATION_NAMES | frozenset(
        {
            "approval", "redemption", "send_raw_transaction", "submit_order",
            "submit_orders", "transact",
        }
    )

    def _kind(self, name: str) -> str | None:
        if name.lower().startswith(("convert_positions", "run_approval")):
            return "mutation"
        return super()._kind(name)


@contextmanager
def guard_predict_client(
    client: object, guard: PredictReadOnlyGuard
) -> Iterator[None]:
    """Install reversible mutation guards without proxying stateful SDK reads/signing."""

    try:
        builder = getattr(client, "_builder")
    except Exception:
        raise RuntimeError("Predict read-only guard unavailable") from None

    replacements: list[tuple[object, str, bool, object]] = []

    def protect(target: object, *, include_missing: bool = False) -> None:
        names = set(guard._MUTATION_NAMES)
        names.update(name for name in dir(target) if guard._kind(name) == "mutation")
        values = getattr(target, "__dict__", {})
        for name in names:
            try:
                getattr(target, name)
            except AttributeError:
                if not include_missing:
                    continue
            had_own = isinstance(values, dict) and name in values
            previous = values.get(name) if had_own else None
            setattr(target, name, lambda *args, _name=name, **kwargs: guard.violation(_name))
            replacements.append((target, name, had_own, previous))

    def restore() -> None:
        for target, name, had_own, previous in reversed(replacements):
            if had_own:
                setattr(target, name, previous)
            else:
                delattr(target, name)

    try:
        protect(client, include_missing=True)
        protect(builder)
        web3 = getattr(builder, "_web3", None)
        if web3 is not None:
            protect(web3)
            eth = getattr(web3, "eth", None)
            if eth is not None:
                protect(eth)
    except Exception:
        restore()
        raise RuntimeError("Predict read-only guard unavailable") from None
    try:
        yield
    finally:
        restore()


@contextmanager
def guard_polymarket_client(
    client: object, guard: PolymarketReadOnlyGuard
) -> Iterator[None]:
    """Install a reversible guard around the SDK and notifier boundaries."""

    replacements: list[tuple[str, object]] = []
    direct_replacements: list[tuple[str, bool, object]] = []
    for name in (
        "_client", "_ctx", "_ctx_inner", "notifier", "_notifier", "notification", "_notification"
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
                replacement = lambda *args, _name=name, _target=previous, **kwargs: guard.call(
                    _name, _target, *args, **kwargs
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
