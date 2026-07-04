from __future__ import annotations

import csv
import re
from pathlib import Path

from open_trader.market_scope import parse_market_scope

from .models import PortfolioInputRow


REQUIRED_FIELDS = ["symbol", "market", "asset_class", "risk_flag"]
REPORTABLE_ASSET_CLASSES = {"stock", "etf", "fund", "unknown"}
OPTION_SYMBOL_PATTERN = re.compile(r"^[A-Z]+[0-9]{6}[CP][0-9]+$")


def _csv_value(value: str | None) -> str:
    return (value or "").strip()


def _is_reportable_asset(*, symbol: str, asset_class: str) -> bool:
    normalized_class = asset_class.strip().lower()
    if normalized_class not in REPORTABLE_ASSET_CLASSES:
        return False
    normalized_symbol = symbol.strip().upper()
    if normalized_class == "unknown" and OPTION_SYMBOL_PATTERN.match(normalized_symbol):
        return False
    return True


def load_eligible_portfolio_rows(
    portfolio_path: Path,
    *,
    market: str | None = None,
) -> list[PortfolioInputRow]:
    market_filter = parse_market_scope(market).value if market is not None else None
    with portfolio_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        eligible: list[PortfolioInputRow] = []
        for row_number, row in enumerate(reader, start=2):
            normalized_row = {
                field: _csv_value(row.get(field))
                for field in (
                    "symbol",
                    "market",
                    "asset_class",
                    "name",
                    "portfolio_weight_hkd",
                    "ai_eligible",
                    "analysis_symbol",
                    "risk_flag",
                )
            }
            normalized_row["market"] = normalized_row["market"].upper()
            normalized_row["asset_class"] = normalized_row["asset_class"].lower()
            if market_filter is not None and normalized_row["market"] != market_filter:
                continue
            if not _is_reportable_asset(
                symbol=normalized_row["symbol"],
                asset_class=normalized_row["asset_class"],
            ):
                continue

            missing_fields = [
                field for field in REQUIRED_FIELDS if not normalized_row[field]
            ]
            if missing_fields:
                raise ValueError(
                    "Eligible portfolio row "
                    f"{row_number} missing required fields: "
                    f"{', '.join(missing_fields)}"
                )

            symbol = normalized_row["symbol"]
            analysis_symbol = normalized_row["analysis_symbol"] or symbol
            eligible.append(
                PortfolioInputRow(
                    symbol=symbol,
                    market=normalized_row["market"],
                    asset_class=normalized_row["asset_class"],
                    name=normalized_row["name"],
                    portfolio_weight_hkd=normalized_row["portfolio_weight_hkd"],
                    risk_flag=normalized_row["risk_flag"],
                    analysis_symbol=analysis_symbol,
                )
            )
    return eligible
