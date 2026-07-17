from __future__ import annotations

import csv
import json
from pathlib import Path

from open_trader.kline_technical_facts import DailyKlineBar, generate_kline_technical_facts
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


class FakeDailyKlineProvider:
    def __init__(self, bars_by_code: dict[str, list[DailyKlineBar]]) -> None:
        self.bars_by_code = bars_by_code
        self.requests: list[tuple[str, str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[DailyKlineBar]:
        self.requests.append((futu_symbol, start, end))
        return self.bars_by_code.get(futu_symbol, [])


def write_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "sort_group": "美股正股",
                    "market": "US",
                    "asset_class": "etf",
                    "symbol": "RAM",
                    "name": "2倍做多DRAM ETF-T-REX",
                    "currency": "USD",
                    "total_quantity": "470",
                    "avg_cost_price": "19.69",
                    "last_price": "16.96",
                    "market_value": "7971.20",
                    "cost_value": "9254.30",
                    "unrealized_pnl": "-1283.10",
                    "unrealized_pnl_pct": "-13.86%",
                    "fx_source": "static_month_end",
                    "fx_date": "2026-07",
                    "fx_to_hkd": "7.80",
                    "market_value_hkd": "62175.36",
                    "cost_value_hkd": "72183.54",
                    "portfolio_weight_hkd": "0.86%",
                    "brokers": "futu",
                    "accounts": "futu_111",
                    "ai_eligible": "true",
                    "analysis_symbol": "RAM",
                    "risk_flag": "normal",
                    "confidence": "high",
                    "notes": "",
                },
                {
                    "sort_group": "美股正股",
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "AMAT",
                    "name": "应用材料",
                    "currency": "USD",
                    "total_quantity": "3",
                    "avg_cost_price": "180",
                    "last_price": "200",
                    "market_value": "600",
                    "cost_value": "540",
                    "unrealized_pnl": "60",
                    "unrealized_pnl_pct": "11.11%",
                    "fx_source": "static_month_end",
                    "fx_date": "2026-07",
                    "fx_to_hkd": "7.80",
                    "market_value_hkd": "4680",
                    "cost_value_hkd": "4212",
                    "portfolio_weight_hkd": "0.06%",
                    "brokers": "futu",
                    "accounts": "futu_111",
                    "ai_eligible": "true",
                    "analysis_symbol": "AMAT",
                    "risk_flag": "normal",
                    "confidence": "high",
                    "notes": "",
                },
            ]
        )


def test_generate_kline_technical_facts_writes_bollinger_records(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    provider = FakeDailyKlineProvider(
        {
            "US.RAM": [
                DailyKlineBar(date=f"2026-06-{day:02d}", close=float(100 + day), volume=1000)
                for day in range(1, 21)
            ],
            "US.AMAT": [
                DailyKlineBar(date=f"2026-06-{day:02d}", close=200.0, volume=1000)
                for day in range(1, 11)
            ],
        }
    )

    result = generate_kline_technical_facts(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-04",
        market="US",
        provider=provider,
        update_latest=True,
    )

    payload = json.loads(result.latest_path.read_text(encoding="utf-8"))
    by_symbol = {record["symbol"]: record for record in payload["records"]}
    assert result.records == 2
    assert result.extracted == 1
    assert result.failed == 1
    assert by_symbol["RAM"]["source_type"] == "futu_kline"
    assert by_symbol["RAM"]["extraction_status"] == "ok"
    ram_facts = by_symbol["RAM"]["facts"]
    assert ram_facts["market_data_as_of"] == "2026-06-20"
    assert ram_facts["timeframes"][0]["bollinger"]["summary_zh"]
    assert by_symbol["AMAT"]["extraction_status"] == "extraction_failed"
    assert by_symbol["AMAT"]["error"] == "日线不足 20 根，无法计算布林带"


def test_generate_kline_technical_facts_includes_held_fund_without_ai_report(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    with portfolio_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "market": "HK",
                    "asset_class": "fund",
                    "symbol": "02824",
                    "ai_eligible": "false",
                },
                {
                    "market": "HK",
                    "asset_class": "money_market_fund",
                    "symbol": "UT.480010",
                    "ai_eligible": "false",
                },
            ]
        )
    provider = FakeDailyKlineProvider(
        {
            "HK.02824": [
                DailyKlineBar(
                    date=f"2026-06-{day:02d}", close=float(100 + day), volume=1000
                )
                for day in range(1, 21)
            ]
        }
    )

    result = generate_kline_technical_facts(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-17",
        market="HK",
        provider=provider,
        update_latest=True,
    )

    payload = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert [record["symbol"] for record in payload["records"]] == ["02824"]
    assert provider.requests == [("HK.02824", "2025-12-09", "2026-07-17")]
    assert payload["records"][0]["facts"]["timeframes"][0]["timeframe"] == "daily"
