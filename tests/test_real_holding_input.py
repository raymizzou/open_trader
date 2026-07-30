from __future__ import annotations

import csv
from pathlib import Path

import pytest

from open_trader.a_share_trend import load_real_holding_input


def write_rows(data_dir: Path, broker: str, market: str, statement_id: str) -> None:
    run_dir = data_dir / "runs" / statement_id[:10]
    run_dir.mkdir(parents=True)
    rows = [
        {
            "statement_id": statement_id,
            "broker": broker,
            "market": market,
            "asset_class": "stock",
            "symbol": "SH.600001" if market == "CN" else "700" if market == "HK" else "AAPL",
            "name": "股票",
            "currency": "CNY" if market == "CN" else "HKD" if market == "HK" else "USD",
            "quantity": "100",
            "cost_price": "9.5",
            "market_value": "1000",
        },
        {
            "statement_id": statement_id,
            "broker": broker,
            "market": market,
            "asset_class": "etf",
            "symbol": "SH.510300" if market == "CN" else "2800" if market == "HK" else "SPY",
            "name": "ETF",
            "currency": "CNY" if market == "CN" else "HKD" if market == "HK" else "USD",
            "quantity": "20",
            "cost_price": "4",
            "market_value": "1000",
        },
        {
            "statement_id": statement_id,
            "broker": broker,
            "market": market,
            "asset_class": "option",
            "symbol": "OPTION",
            "name": "期权",
            "currency": "USD",
            "quantity": "1",
            "cost_price": "1",
            "market_value": "1",
        },
        {
            "statement_id": statement_id,
            "broker": broker,
            "market": market,
            "asset_class": "money_market_fund",
            "symbol": "CASHLIKE",
            "name": "货币基金",
            "currency": "USD",
            "quantity": "10",
            "cost_price": "1",
            "market_value": "10",
        },
        {
            "statement_id": statement_id,
            "broker": broker,
            "market": market,
            "asset_class": "stock",
            "symbol": "ZERO",
            "name": "已清仓",
            "currency": "USD",
            "quantity": "0",
            "cost_price": "1",
            "market_value": "0",
        },
    ]
    with (run_dir / "extracted_positions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (run_dir / "extracted_cash.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["statement_id", "broker", "currency", "cash_balance"])
        writer.writeheader()
        writer.writerow({"statement_id": statement_id, "broker": broker, "currency": "USD", "cash_balance": "10"})


@pytest.mark.parametrize(
    ("market", "broker", "statement_id", "symbols"),
    [
        ("CN", "eastmoney", "2026-07-29-eastmoney", ["600001", "510300"]),
        ("HK", "phillips", "2026-07-29-phillips", ["00700", "02800"]),
        ("US", "tiger", "2026-07-29-tiger-live", ["AAPL", "SPY"]),
    ],
)
def test_real_input_keeps_only_positive_stock_and_etf(
    tmp_path: Path,
    market: str,
    broker: str,
    statement_id: str,
    symbols: list[str],
) -> None:
    data_dir = tmp_path / "data"
    write_rows(data_dir, broker, market, statement_id)

    loaded = load_real_holding_input(
        data_dir,
        market,
        state_path=data_dir / f"trend_{market.lower()}" / "real_protection_state.json",
    )

    assert loaded.status == "available"
    assert [position.symbol for position in loaded.positions] == sorted(symbols)
    assert all(position.quantity > 0 for position in loaded.positions)
    assert loaded.source["broker"] == broker
    assert loaded.source["snapshot_period"] == "2026-07-29"
    assert loaded.source["read_only_text"] == "只读，不自动下单"


def test_real_input_marks_malformed_retained_row_unavailable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_rows(data_dir, "phillips", "HK", "2026-07-29-phillips")
    path = data_dir / "runs" / "2026-07-29" / "extracted_positions.csv"
    text = path.read_text(encoding="utf-8").replace(",100,9.5,1000", ",bad,9.5,1000", 1)
    path.write_text(text, encoding="utf-8")

    loaded = load_real_holding_input(
        data_dir,
        "HK",
        state_path=data_dir / "trend_hk_phillips" / "real_protection_state.json",
    )

    assert loaded.status == "unavailable"
    assert loaded.positions == ()
    assert "数量" in loaded.reason
