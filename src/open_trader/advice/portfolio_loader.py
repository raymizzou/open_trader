from __future__ import annotations

import csv
from pathlib import Path

from .models import PortfolioInputRow


REQUIRED_FIELDS = ["symbol", "market", "asset_class", "risk_flag"]


def _csv_value(value: str | None) -> str:
    return (value or "").strip()


def load_eligible_portfolio_rows(portfolio_path: Path) -> list[PortfolioInputRow]:
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
            if normalized_row["ai_eligible"].lower() != "true":
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
