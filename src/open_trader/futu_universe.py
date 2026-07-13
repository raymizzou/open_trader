from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .futu_symbols import to_futu_symbol


QUOTEABLE_ASSET_CLASSES = {"stock", "etf", "fund", "option", "unknown"}
SUPPORTED_MARKETS = {"US", "HK", "CN"}


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
            reason = _skip_reason(
                market=market,
                asset_class=asset_class,
                symbol=symbol,
                quantity_text=quantity_text,
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


def _skip_reason(
    *,
    market: str,
    asset_class: str,
    symbol: str,
    quantity_text: str,
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
    try:
        to_futu_symbol(market, symbol)
    except ValueError:
        return "invalid_symbol"
    return None
