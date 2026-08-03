from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .account_sync_state import QUOTE_STALE_SECONDS
from .dashboard import DashboardConfig
from .futu_quote import DashboardQuoteSnapshot, FutuQuoteClient, FutuQuoteError
from .futu_universe import FutuUniverseItem, load_futu_quote_universe


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ACTIVE_US_SESSION_ORDERS = {
    "OVERNIGHT": ("overnight", "after_hours", "regular", "pre_market"),
    "PRE_MARKET_BEGIN": ("pre_market", "overnight", "after_hours", "regular"),
    "MORNING": ("regular", "pre_market", "overnight", "after_hours"),
    "AFTERNOON": ("regular", "pre_market", "overnight", "after_hours"),
    "AFTER_HOURS_BEGIN": ("after_hours", "regular", "pre_market", "overnight"),
}
INACTIVE_US_SESSION_ORDERS = {
    "PRE_MARKET_END": ("pre_market", "overnight", "after_hours", "regular"),
    "WAITING_OPEN": ("pre_market", "overnight", "after_hours", "regular"),
    "AFTER_HOURS_END": ("after_hours", "regular", "pre_market", "overnight"),
}
CLOSED_US_SESSION_ORDER = ("after_hours", "regular", "pre_market", "overnight")


def load_published_quotes(path: Path, *, now: datetime) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unknown_published_quotes()
    if not isinstance(payload, dict):
        return _unknown_published_quotes()
    status = payload.get("status")
    last_success_at = payload.get("last_success_at")
    stale = payload.get("stale")
    quotes = payload.get("quotes")
    if (
        status not in {"ok", "partial", "failed"}
        or not isinstance(last_success_at, str)
        or not isinstance(stale, bool)
        or not isinstance(quotes, dict)
        or any(
            not isinstance(symbol, str) or not isinstance(quote, dict)
            for symbol, quote in quotes.items()
        )
    ):
        return _unknown_published_quotes()
    last_success = _parse_aware_datetime(last_success_at)
    if last_success_at and last_success is None:
        return _unknown_published_quotes()
    effective_stale = stale or (
        last_success is not None
        and (now - last_success).total_seconds() > QUOTE_STALE_SECONDS
    )
    return {
        **payload,
        "stale": effective_stale,
        "quotes": {
            symbol: {**quote, "stale": True} if effective_stale else dict(quote)
            for symbol, quote in quotes.items()
        },
    }


def _unknown_published_quotes() -> dict[str, object]:
    return {
        "status": "unknown",
        "last_success_at": "",
        "stale": False,
        "quotes": {},
    }


def _parse_aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class DashboardQuoteClient(Protocol):
    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        raise NotImplementedError

    def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class QuoteRefreshResult:
    status: str
    requested_count: int
    quote_count: int
    missing_count: int
    fetched_at: str
    last_success_at: str
    stale: bool
    quotes: dict[str, dict[str, Any]]
    diagnostic: dict[str, Any]
    fallback_count: int = 0
    us_session_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested_count": self.requested_count,
            "quote_count": self.quote_count,
            "missing_count": self.missing_count,
            "fetched_at": self.fetched_at,
            "last_success_at": self.last_success_at,
            "stale": self.stale,
            "fallback_count": self.fallback_count,
            "us_session_status": self.us_session_status,
            "quotes": {
                futu_symbol: dict(quote)
                for futu_symbol, quote in self.quotes.items()
            },
            "diagnostic": dict(self.diagnostic),
        }


@dataclass
class DashboardQuoteService:
    config: DashboardConfig
    client_factory: Callable[[], DashboardQuoteClient] | None = None
    last_success_at: str = ""
    last_quotes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def refresh(self) -> QuoteRefreshResult:
        fetched_at = _now_text()
        universe = load_futu_quote_universe(self.config.portfolio_path)
        items_by_symbol = _items_by_sorted_symbol(universe.items)
        requested_symbols = list(items_by_symbol)
        us_symbols = [symbol for symbol in requested_symbols if symbol.startswith("US.")]
        symbols_by_prefix: dict[str, list[str]] = {}
        for symbol in items_by_symbol:
            symbols_by_prefix.setdefault(symbol.split(".", 1)[0], []).append(symbol)
        client: DashboardQuoteClient | None = None
        market_states: dict[str, str] = {}
        snapshot_errors: list[tuple[str, FutuQuoteError]] = []
        state_error: FutuQuoteError | None = None

        try:
            snapshots: dict[str, DashboardQuoteSnapshot] = {}
            if requested_symbols:
                client = self._new_client()
                for prefix, symbols in symbols_by_prefix.items():
                    try:
                        snapshots.update(client.get_dashboard_snapshots(symbols))
                    except FutuQuoteError as exc:
                        snapshot_errors.append((prefix, exc))
                if len(snapshot_errors) == len(symbols_by_prefix):
                    raise snapshot_errors[0][1]
                if us_symbols and not any(
                    prefix == "US" for prefix, _ in snapshot_errors
                ):
                    try:
                        market_states = client.get_market_states(us_symbols)
                    except FutuQuoteError as exc:
                        state_error = exc
        except FutuQuoteError as exc:
            retained_quotes = _mark_stale(_accepted_quotes(self.last_quotes))
            quotes = {
                symbol: _quote_row(
                    item=items_by_symbol[symbol],
                    snapshot=None,
                    market_state="",
                    use_us_session=False,
                    fetched_at=fetched_at,
                    stale=False,
                )
                for symbol in requested_symbols
                if symbol not in retained_quotes
            }
            quotes.update(retained_quotes)
            missing_count = sum(
                1 for quote in quotes.values() if quote["status"] == "missing_quote"
            )
            return QuoteRefreshResult(
                status="failed",
                requested_count=len(requested_symbols),
                quote_count=len(quotes) - missing_count,
                missing_count=missing_count,
                fetched_at=fetched_at,
                last_success_at=self.last_success_at,
                stale=bool(retained_quotes),
                quotes=quotes,
                diagnostic=_error_diagnostic(exc),
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

        us_snapshots_succeeded = not any(
            prefix == "US" for prefix, _ in snapshot_errors
        )
        if us_snapshots_succeeded and state_error is None and any(
            not market_states.get(symbol) for symbol in us_symbols
        ):
            state_error = FutuQuoteError(
                "incomplete US market states",
                error_type="market_state_failed",
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=True,
            )

        quotes = {
            futu_symbol: _quote_row(
                item=items_by_symbol[futu_symbol],
                snapshot=snapshots.get(futu_symbol),
                market_state=market_states.get(futu_symbol, ""),
                use_us_session=state_error is None,
                fetched_at=fetched_at,
                stale=False,
            )
            for futu_symbol in requested_symbols
        }
        reused_us_quotes = (
            _last_good_us_quotes(self.last_quotes, us_symbols)
            if us_snapshots_succeeded and state_error is not None
            else {}
        )
        failed_prefixes = {prefix for prefix, _ in snapshot_errors}
        retained_quotes = {
            symbol: quote
            for symbol, quote in _mark_stale(_accepted_quotes(self.last_quotes)).items()
            if symbol in requested_symbols
            and symbol.split(".", 1)[0] in failed_prefixes
        }
        if reused_us_quotes:
            quotes.update(reused_us_quotes)
            market_states.update(
                {
                    symbol: str(quote["market_state"])
                    for symbol, quote in reused_us_quotes.items()
                }
            )
        quotes.update(retained_quotes)
        missing_count = sum(
            1 for quote in quotes.values() if quote["status"] == "missing_quote"
        )
        quote_count = len(requested_symbols) - missing_count
        fallback_count = sum(
            1
            for futu_symbol, quote in quotes.items()
            if market_states.get(futu_symbol) in ACTIVE_US_SESSION_ORDERS
            and quote["status"] == "ok"
            and not quote["current_session_quote"]
        )
        status = (
            "partial"
            if missing_count or fallback_count or snapshot_errors or state_error
            else "ok"
        )
        diagnostic = _partial_diagnostic(
            missing_count,
            fallback_count,
            snapshot_errors,
            state_error,
            bool(reused_us_quotes),
        )
        cacheable = not snapshot_errors and missing_count == 0 and state_error is None
        if cacheable:
            self.last_success_at = fetched_at
            self.last_quotes = {
                futu_symbol: dict(quote)
                for futu_symbol, quote in quotes.items()
            }
        elif snapshot_errors and state_error is None:
            self.last_quotes = {
                **self.last_quotes,
                **{
                    symbol: dict(quote)
                    for symbol, quote in quotes.items()
                    if symbol.split(".", 1)[0] not in failed_prefixes
                    and quote["status"] == "ok"
                },
            }

        return QuoteRefreshResult(
            status=status,
            requested_count=len(requested_symbols),
            quote_count=quote_count,
            missing_count=missing_count,
            fetched_at=fetched_at,
            last_success_at=self.last_success_at,
            stale=bool(reused_us_quotes or retained_quotes),
            quotes=quotes,
            diagnostic=diagnostic,
            fallback_count=fallback_count,
            us_session_status=_us_session_status(
                us_symbols,
                market_states,
                None if reused_us_quotes else state_error,
            ),
        )

    def _new_client(self) -> DashboardQuoteClient:
        if self.client_factory is not None:
            return self.client_factory()
        return FutuQuoteClient(host=self.config.futu_host, port=self.config.futu_port)


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def _items_by_sorted_symbol(
    items: list[FutuUniverseItem],
) -> dict[str, FutuUniverseItem]:
    unique: dict[str, FutuUniverseItem] = {}
    for item in sorted(items, key=lambda row: row.futu_symbol):
        unique.setdefault(item.futu_symbol, item)
    return unique


def _quote_row(
    *,
    item: FutuUniverseItem,
    snapshot: DashboardQuoteSnapshot | None,
    market_state: str,
    use_us_session: bool,
    fetched_at: str,
    stale: bool,
) -> dict[str, Any]:
    price: Decimal | None = None
    price_session = ""
    current_session_quote = False
    price_time = ""
    if snapshot is not None:
        price = snapshot.last_price
        if item.futu_symbol.startswith("US.") and use_us_session:
            price, price_session, current_session_quote, price_time = (
                _select_us_price(snapshot, market_state)
            )
    if price is None:
        return {
            "market": item.market,
            "symbol": item.symbol,
            "name": item.name,
            "futu_symbol": item.futu_symbol,
            "status": "missing_quote",
            "last_price": "",
            "price_session": "",
            "price_time": "",
            "current_session_quote": False,
            "market_state": market_state,
            "fetched_at": fetched_at,
            "stale": stale,
        }
    return {
        "market": item.market,
        "symbol": item.symbol,
        "name": item.name,
        "futu_symbol": item.futu_symbol,
        "status": "ok",
        "last_price": _decimal_text(price),
        "price_session": price_session,
        "price_time": price_time,
        "current_session_quote": current_session_quote,
        "market_state": market_state,
        "fetched_at": fetched_at,
        "stale": stale,
    }


def _session_prices(
    snapshot: DashboardQuoteSnapshot,
) -> dict[str, Decimal | None]:
    return {
        "regular": snapshot.last_price,
        "pre_market": snapshot.pre_price,
        "after_hours": snapshot.after_price,
        "overnight": snapshot.overnight_price,
    }


def _select_us_price(
    snapshot: DashboardQuoteSnapshot, market_state: str
) -> tuple[Decimal | None, str, bool, str]:
    active_order = ACTIVE_US_SESSION_ORDERS.get(market_state)
    order = active_order or INACTIVE_US_SESSION_ORDERS.get(
        market_state, CLOSED_US_SESSION_ORDER
    )
    prices = _session_prices(snapshot)
    for index, session in enumerate(order):
        if price := prices[session]:
            current = active_order is not None and index == 0
            return (
                price,
                session,
                current,
                _normalize_futu_us_price_time(snapshot.update_time) if current else "",
            )
    return None, "", False, ""


def _normalize_futu_us_price_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.isoformat(sep=" ", timespec="milliseconds")


def _us_session_status(
    us_symbols: list[str],
    market_states: dict[str, str],
    state_error: FutuQuoteError | None,
) -> str:
    if state_error is not None or not us_symbols or not market_states:
        return "unknown"
    active_count = sum(
        market_states[symbol] in ACTIVE_US_SESSION_ORDERS for symbol in us_symbols
    )
    if active_count == len(us_symbols):
        return "active"
    return "mixed" if active_count else "closed"


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _mark_stale(
    quotes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    stale_quotes: dict[str, dict[str, Any]] = {}
    for futu_symbol, quote in quotes.items():
        row = dict(quote)
        row["stale"] = True
        stale_quotes[futu_symbol] = row
    return stale_quotes


def _accepted_quotes(
    quotes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        symbol: quote
        for symbol, quote in quotes.items()
        if quote.get("status") != "missing_quote"
    }


def _last_good_us_quotes(
    last_quotes: dict[str, dict[str, Any]], us_symbols: list[str]
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for symbol in us_symbols:
        quote = last_quotes.get(symbol)
        if (
            not quote
            or quote.get("status") != "ok"
            or not quote.get("price_session")
            or not quote.get("market_state")
        ):
            return {}
        rows[symbol] = {**quote, "stale": True}
    return rows


def _partial_diagnostic(
    missing_count: int,
    fallback_count: int,
    snapshot_errors: list[tuple[str, FutuQuoteError]],
    state_error: FutuQuoteError | None,
    reused_us_quotes: bool = False,
) -> dict[str, Any]:
    messages: list[str] = []
    if snapshot_errors:
        prefix, error = snapshot_errors[0]
        messages.append(f"{prefix} 行情获取失败：{error}")
    if missing_count:
        messages.append(f"缺失 {missing_count} 个标的行情。")
    if fallback_count:
        messages.append(f"{fallback_count} 个标的当前时段无报价，已使用上一有效价。")
    if state_error is not None:
        messages.append(
            "美股市场状态不可用，已使用上一笔有效分时段行情。"
            if reused_us_quotes
            else "美股市场状态不可用，已使用盘中价。"
        )
    if not messages:
        return {}
    primary_error = snapshot_errors[0][1] if snapshot_errors else state_error
    error_type = (
        primary_error.error_type
        if primary_error is not None
        else "missing_quotes" if missing_count else "session_quote_fallback"
    )
    next_step = (
        primary_error.next_step
        if primary_error is not None
        else f"请人工复核缺失 {missing_count} 个标的行情，再决定是否执行相关交易动作。"
        if missing_count
        else "请人工复核当前时段报价，再决定是否执行相关交易动作。"
    )
    diagnostic = {
        "error_type": error_type,
        "message": " ".join(messages),
        "next_step": next_step,
        "opend_reachable": (
            primary_error.opend_reachable if primary_error is not None else True
        ),
        "context_ok": primary_error.context_ok if primary_error is not None else True,
        "snapshot_ok": primary_error.snapshot_ok if primary_error is not None else True,
    }
    if snapshot_errors:
        diagnostic["market"] = snapshot_errors[0][0]
    return diagnostic


def _error_diagnostic(error: FutuQuoteError) -> dict[str, Any]:
    return {
        "error_type": error.error_type,
        "message": str(error),
        "next_step": error.next_step,
        "opend_reachable": error.opend_reachable,
        "context_ok": error.context_ok,
        "snapshot_ok": error.snapshot_ok,
    }
