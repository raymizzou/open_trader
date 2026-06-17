from __future__ import annotations

import csv
from pathlib import Path

import pytest

from open_trader.advice.portfolio_loader import load_eligible_portfolio_rows


FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "name",
    "market_value_hkd",
    "portfolio_weight_hkd",
    "ai_eligible",
    "analysis_symbol",
    "risk_flag",
]


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_load_eligible_portfolio_rows_filters_ai_eligible_rows(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "Volatility ETF",
                "market_value_hkd": "38015.98",
                "portfolio_weight_hkd": "3.05%",
                "ai_eligible": "true",
                "analysis_symbol": "VIXY",
                "risk_flag": "normal",
            },
            {
                "market": "HK",
                "asset_class": "stock",
                "symbol": "02476",
                "name": "VGT",
                "market_value_hkd": "189400.00",
                "portfolio_weight_hkd": "15.20%",
                "ai_eligible": "false",
                "analysis_symbol": "",
                "risk_flag": "overweight",
            },
        ],
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert [row.symbol for row in rows] == ["VIXY"]
    assert rows[0].analysis_symbol == "VIXY"
    assert rows[0].market_value_hkd == "38015.98"
    assert rows[0].portfolio_weight_hkd == "3.05%"


def test_load_eligible_portfolio_rows_uses_analysis_symbol_when_present(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": "US",
                "asset_class": "stock",
                "symbol": "BRK.B",
                "name": "Berkshire Hathaway",
                "market_value_hkd": "25000.00",
                "portfolio_weight_hkd": "2.00%",
                "ai_eligible": "true",
                "analysis_symbol": "BRK-B",
                "risk_flag": "normal",
            },
        ],
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert rows[0].symbol == "BRK.B"
    assert rows[0].analysis_symbol == "BRK-B"


def test_load_eligible_portfolio_rows_handles_case_and_whitespace(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": " US ",
                "asset_class": " etf ",
                "symbol": " VIXY ",
                "name": " Volatility ETF ",
                "market_value_hkd": " 38,015.98 ",
                "portfolio_weight_hkd": " 3.05% ",
                "ai_eligible": " TRUE ",
                "analysis_symbol": " ",
                "risk_flag": " normal ",
            },
        ],
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert rows[0].symbol == "VIXY"
    assert rows[0].market == "US"
    assert rows[0].asset_class == "etf"
    assert rows[0].name == "Volatility ETF"
    assert rows[0].market_value_hkd == "38,015.98"
    assert rows[0].portfolio_weight_hkd == "3.05%"
    assert rows[0].risk_flag == "normal"
    assert rows[0].analysis_symbol == "VIXY"


def test_load_eligible_portfolio_rows_allows_missing_market_value_column(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    portfolio_path.write_text(
        "\n".join(
            [
                "market,asset_class,symbol,name,portfolio_weight_hkd,ai_eligible,analysis_symbol,risk_flag",
                "US,etf,VIXY,Volatility ETF,3.05%,true,VIXY,normal",
            ]
        ),
        encoding="utf-8",
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert rows[0].symbol == "VIXY"
    assert rows[0].market_value_hkd == ""


def test_load_eligible_portfolio_rows_rejects_eligible_row_missing_symbol(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    portfolio_path.write_text(
        "\n".join(
            [
                ",".join(FIELDNAMES),
                "US,etf,VIXY,Volatility ETF,38015.98,3.05%,false,VIXY,normal",
                "US,etf,,Volatility ETF,38015.98,3.05%,true,VIXY,normal",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="row 3.*symbol"):
        load_eligible_portfolio_rows(portfolio_path)


def test_load_eligible_portfolio_rows_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_eligible_portfolio_rows(tmp_path / "missing.csv")
