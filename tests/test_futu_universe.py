from __future__ import annotations

import csv
from pathlib import Path

from open_trader.futu_universe import (
    FutuUniverseItem,
    SkippedFutuUniverseRow,
    load_futu_quote_universe,
)


PORTFOLIO_FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "name",
    "total_quantity",
]


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_load_futu_quote_universe_excludes_cash_and_money_market_funds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "market": "US",
                "asset_class": "stock",
                "symbol": "MSFT",
                "name": "Microsoft",
                "total_quantity": "4",
            },
            {
                "market": "HK",
                "asset_class": "stock",
                "symbol": "2476",
                "name": "HK stock",
                "total_quantity": "500",
            },
            {
                "market": "US",
                "asset_class": "option",
                "symbol": "VIXY260717C27000",
                "name": "VIXY call",
                "total_quantity": "-1",
            },
            {
                "market": "HK",
                "asset_class": "money_market_fund",
                "symbol": "HK0000951506.HKD",
                "name": "HKD money market fund",
                "total_quantity": "543253.5521",
            },
            {
                "market": "CASH",
                "asset_class": "cash",
                "symbol": "HKD_CASH",
                "name": "HKD Cash",
                "total_quantity": "10",
            },
            {
                "market": "US",
                "asset_class": "etf",
                "symbol": "ZERO",
                "name": "Zero quantity",
                "total_quantity": "0",
            },
            {
                "market": "US",
                "asset_class": "stock",
                "symbol": "BADQTY",
                "name": "Bad quantity",
                "total_quantity": "not-a-number",
            },
        ],
    )

    universe = load_futu_quote_universe(path)

    assert universe.items == [
        FutuUniverseItem(
            row_number=2,
            market="US",
            asset_class="stock",
            symbol="MSFT",
            futu_symbol="US.MSFT",
            name="Microsoft",
        ),
        FutuUniverseItem(
            row_number=3,
            market="HK",
            asset_class="stock",
            symbol="2476",
            futu_symbol="HK.02476",
            name="HK stock",
        ),
        FutuUniverseItem(
            row_number=4,
            market="US",
            asset_class="option",
            symbol="VIXY260717C27000",
            futu_symbol="US.VIXY260717C27000",
            name="VIXY call",
        ),
    ]
    assert universe.skipped == [
        SkippedFutuUniverseRow(
            row_number=5,
            market="HK",
            asset_class="money_market_fund",
            symbol="HK0000951506.HKD",
            reason="excluded_asset_class",
        ),
        SkippedFutuUniverseRow(
            row_number=6,
            market="CASH",
            asset_class="cash",
            symbol="HKD_CASH",
            reason="excluded_asset_class",
        ),
        SkippedFutuUniverseRow(
            row_number=7,
            market="US",
            asset_class="etf",
            symbol="ZERO",
            reason="zero_quantity",
        ),
        SkippedFutuUniverseRow(
            row_number=8,
            market="US",
            asset_class="stock",
            symbol="BADQTY",
            reason="invalid_quantity",
        ),
    ]


def test_load_futu_quote_universe_skips_blank_symbols_and_unsupported_markets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "market": "OTHER",
                "asset_class": "stock",
                "symbol": "XYZ",
                "name": "Unsupported",
                "total_quantity": "1",
            },
            {
                "market": "US",
                "asset_class": "stock",
                "symbol": "",
                "name": "Blank",
                "total_quantity": "1",
            },
            {
                "market": "HK",
                "asset_class": "fund",
                "symbol": "2824",
                "name": "HK fund",
                "total_quantity": "10",
            },
        ],
    )

    universe = load_futu_quote_universe(path)

    assert [item.futu_symbol for item in universe.items] == ["HK.02824"]
    assert universe.skipped == [
        SkippedFutuUniverseRow(
            row_number=2,
            market="OTHER",
            asset_class="stock",
            symbol="XYZ",
            reason="unsupported_market",
        ),
        SkippedFutuUniverseRow(
            row_number=3,
            market="US",
            asset_class="stock",
            symbol="",
            reason="blank_symbol",
        ),
    ]


def test_load_futu_quote_universe_includes_unknown_supported_market_holdings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "market": "US",
                "asset_class": "unknown",
                "symbol": "BOTZ",
                "name": "Global X Robotics ETF",
                "total_quantity": "50",
            },
            {
                "market": "HK",
                "asset_class": "unknown",
                "symbol": "1989",
                "name": "HK stock",
                "total_quantity": "100",
            },
            {
                "market": "CASH",
                "asset_class": "unknown",
                "symbol": "HKD_CASH",
                "name": "HKD Cash",
                "total_quantity": "1",
            },
        ],
    )

    universe = load_futu_quote_universe(path)

    assert [item.futu_symbol for item in universe.items] == [
        "US.BOTZ",
        "HK.01989",
    ]
    assert universe.skipped == [
        SkippedFutuUniverseRow(
            row_number=4,
            market="CASH",
            asset_class="unknown",
            symbol="HKD_CASH",
            reason="unsupported_market",
        ),
    ]


def test_load_futu_quote_universe_maps_cn_exchange_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            {
                "market": "CN",
                "asset_class": "stock",
                "symbol": "600025",
                "name": "华能水电",
                "total_quantity": "6000",
            },
            {
                "market": "CN",
                "asset_class": "etf",
                "symbol": "159915",
                "name": "创业板 ETF",
                "total_quantity": "100",
            },
            {
                "market": "CN",
                "asset_class": "stock",
                "symbol": "800001",
                "name": "Unsupported",
                "total_quantity": "1",
            },
        ],
    )

    universe = load_futu_quote_universe(path)

    assert [item.futu_symbol for item in universe.items] == [
        "SH.600025",
        "SZ.159915",
    ]
    assert universe.skipped == [
        SkippedFutuUniverseRow(
            row_number=4,
            market="CN",
            asset_class="stock",
            symbol="800001",
            reason="invalid_symbol",
        )
    ]
