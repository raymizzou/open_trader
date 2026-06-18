from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .dashboard import DashboardConfig
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_universe import FutuUniverseItem, load_futu_quote_universe
from .futu_watch import QuoteSnapshot


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class DashboardQuoteClient(Protocol):
    def get_snapshots(
        self,
        futu_symbols: Sequence[str],
    ) -> dict[str, QuoteSnapshot]:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested_count": self.requested_count,
            "quote_count": self.quote_count,
            "missing_count": self.missing_count,
            "fetched_at": self.fetched_at,
            "last_success_at": self.last_success_at,
            "stale": self.stale,
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
        client: DashboardQuoteClient | None = None

        try:
            snapshots: dict[str, QuoteSnapshot]
            if requested_symbols:
                client = self._new_client()
                snapshots = client.get_snapshots(requested_symbols)
            else:
                snapshots = {}
        except FutuQuoteError as exc:
            return QuoteRefreshResult(
                status="failed",
                requested_count=len(requested_symbols),
                quote_count=0,
                missing_count=0,
                fetched_at=fetched_at,
                last_success_at=self.last_success_at,
                stale=bool(self.last_quotes),
                quotes=_mark_stale(self.last_quotes),
                diagnostic=_error_diagnostic(exc),
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

        quotes = {
            futu_symbol: _quote_row(
                item=items_by_symbol[futu_symbol],
                snapshot=snapshots.get(futu_symbol),
                fetched_at=fetched_at,
                stale=False,
            )
            for futu_symbol in requested_symbols
        }
        missing_count = sum(
            1 for quote in quotes.values() if quote["status"] == "missing_quote"
        )
        quote_count = len(requested_symbols) - missing_count
        status = "partial" if missing_count else "ok"
        diagnostic = (
            _missing_quotes_diagnostic(missing_count) if missing_count else {}
        )
        if status == "ok":
            self.last_success_at = fetched_at
            self.last_quotes = {
                futu_symbol: dict(quote)
                for futu_symbol, quote in quotes.items()
            }

        return QuoteRefreshResult(
            status=status,
            requested_count=len(requested_symbols),
            quote_count=quote_count,
            missing_count=missing_count,
            fetched_at=fetched_at,
            last_success_at=self.last_success_at,
            stale=False,
            quotes=quotes,
            diagnostic=diagnostic,
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
    snapshot: QuoteSnapshot | None,
    fetched_at: str,
    stale: bool,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "market": item.market,
            "symbol": item.symbol,
            "name": item.name,
            "futu_symbol": item.futu_symbol,
            "status": "missing_quote",
            "last_price": "",
            "fetched_at": fetched_at,
            "stale": stale,
        }
    return {
        "market": item.market,
        "symbol": item.symbol,
        "name": item.name,
        "futu_symbol": item.futu_symbol,
        "status": "ok",
        "last_price": _decimal_text(snapshot.last_price),
        "fetched_at": fetched_at,
        "stale": stale,
    }


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


def _missing_quotes_diagnostic(missing_count: int) -> dict[str, Any]:
    return {
        "error_type": "missing_quotes",
        "message": f"缺失 {missing_count} 个标的行情。",
        "next_step": f"请人工复核缺失 {missing_count} 个标的行情，再决定是否执行相关交易动作。",
        "opend_reachable": True,
        "context_ok": True,
        "snapshot_ok": True,
    }


def _error_diagnostic(error: FutuQuoteError) -> dict[str, Any]:
    return {
        "error_type": error.error_type,
        "message": str(error),
        "next_step": error.next_step,
        "opend_reachable": error.opend_reachable,
        "context_ok": error.context_ok,
        "snapshot_ok": error.snapshot_ok,
    }
