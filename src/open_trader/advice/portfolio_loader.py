from __future__ import annotations

import csv
from pathlib import Path

from .models import PortfolioInputRow


def load_eligible_portfolio_rows(portfolio_path: Path) -> list[PortfolioInputRow]:
    with portfolio_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    eligible: list[PortfolioInputRow] = []
    for row in rows:
        if row.get("ai_eligible", "").lower() != "true":
            continue
        symbol = row.get("symbol", "").strip()
        analysis_symbol = row.get("analysis_symbol", "").strip() or symbol
        eligible.append(
            PortfolioInputRow(
                symbol=symbol,
                market=row.get("market", "").strip(),
                asset_class=row.get("asset_class", "").strip(),
                name=row.get("name", "").strip(),
                portfolio_weight_hkd=row.get("portfolio_weight_hkd", "").strip(),
                risk_flag=row.get("risk_flag", "").strip(),
                analysis_symbol=analysis_symbol,
            )
        )
    return eligible
