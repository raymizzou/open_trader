from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .futu_symbols import to_futu_symbol


QUOTEABLE_ASSET_CLASSES = {"stock", "etf", "fund", "option", "unknown"}
SUPPORTED_MARKETS = {"US", "HK", "CN"}
STATEMENT_BROKERS = {"eastmoney", "phillips"}


@dataclass(frozen=True)
class FutuUniverseItem:
    row_number: int
    market: str
    asset_class: str
    symbol: str
    futu_symbol: str
    name: str


@dataclass(frozen=True)
class SkippedFutuUniverseRow:
    row_number: int
    market: str
    asset_class: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class FutuQuoteUniverse:
    items: list[FutuUniverseItem]
    skipped: list[SkippedFutuUniverseRow]


def load_futu_quote_universe(portfolio_path: Path) -> FutuQuoteUniverse:
    items: list[FutuUniverseItem] = []
    skipped: list[SkippedFutuUniverseRow] = []
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            market = row.get("market", "").strip().upper()
            asset_class = row.get("asset_class", "").strip().lower()
            symbol = row.get("symbol", "").strip().upper()
            name = row.get("name", "").strip()
            quantity_text = row.get("total_quantity", "").strip()
            brokers = {
                broker.strip().lower()
                for broker in row.get("brokers", "").replace(",", ";").split(";")
                if broker.strip()
            }
            reason = _skip_reason(
                market=market,
                asset_class=asset_class,
                symbol=symbol,
                quantity_text=quantity_text,
                brokers=brokers,
            )
            if reason is not None:
                skipped.append(
                    SkippedFutuUniverseRow(
                        row_number=row_number,
                        market=market,
                        asset_class=asset_class,
                        symbol=symbol,
                        reason=reason,
                    )
                )
                continue
            items.append(
                FutuUniverseItem(
                    row_number=row_number,
                    market=market,
                    asset_class=asset_class,
                    symbol=symbol,
                    futu_symbol=to_futu_symbol(market, symbol),
                    name=name,
                )
            )
    return FutuQuoteUniverse(items=items, skipped=skipped)


def build_futu_quote_universe(
    positions: Sequence[Mapping[str, object]],
) -> FutuQuoteUniverse:
    """Build the production quote universe from accepted Account positions."""
    items: list[FutuUniverseItem] = []
    skipped: list[SkippedFutuUniverseRow] = []
    for row_number, row in enumerate(positions, start=1):
        market = str(row.get("market") or "").strip().upper()
        asset_class = str(row.get("asset_class") or "").strip().lower()
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        quantity_text = str(
            row.get("quantity") if row.get("quantity") is not None else row.get("total_quantity") or ""
        ).strip()
        reason = _skip_reason(
            market=market,
            asset_class=asset_class,
            symbol=symbol,
            quantity_text=quantity_text,
            brokers=set(),
        )
        if reason is not None:
            skipped.append(
                SkippedFutuUniverseRow(
                    row_number=row_number,
                    market=market,
                    asset_class=asset_class,
                    symbol=symbol,
                    reason=reason,
                )
            )
            continue
        items.append(
            FutuUniverseItem(
                row_number=row_number,
                market=market,
                asset_class=asset_class,
                symbol=symbol,
                futu_symbol=to_futu_symbol(market, symbol),
                name=name,
            )
        )
    return FutuQuoteUniverse(items=items, skipped=skipped)


def build_account_quote_universe(state: Mapping[str, object]) -> FutuQuoteUniverse:
    positions: list[Mapping[str, object]] = []
    brokers = state.get("brokers")
    if isinstance(brokers, Mapping):
        for broker in sorted(brokers):
            source = brokers[broker]
            rows = source.get("positions") if isinstance(source, Mapping) else None
            if isinstance(rows, list):
                positions.extend(row for row in rows if isinstance(row, Mapping))
    return build_futu_quote_universe(positions)


def _skip_reason(
    *,
    market: str,
    asset_class: str,
    symbol: str,
    quantity_text: str,
    brokers: set[str],
) -> str | None:
    if not symbol:
        return "blank_symbol"
    try:
        quantity = Decimal(quantity_text)
    except (InvalidOperation, ValueError):
        return "invalid_quantity"
    if not quantity.is_finite():
        return "invalid_quantity"
    if quantity == 0:
        return "zero_quantity"
    if asset_class not in QUOTEABLE_ASSET_CLASSES:
        return "excluded_asset_class"
    if market not in SUPPORTED_MARKETS:
        return "unsupported_market"
    if brokers and brokers <= STATEMENT_BROKERS:
        return "statement_only_source"
    try:
        to_futu_symbol(market, symbol)
    except ValueError:
        return "invalid_symbol"
    return None
