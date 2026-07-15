from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.decision_plan import load_decision_plans
from open_trader.decision_plan_generation import (
    _RangeCachingProvider,
    generate_daily_decision_plans,
)
from open_trader.kline_technical_facts import DailyKlineBar


class PriceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        self.calls += 1
        first = date(2025, 4, 20)
        return [
            DailyKlineBar(
                date=(first + timedelta(days=index)).isoformat(),
                open=100 + index / 100,
                high=101 + index / 100,
                low=99 + index / 100,
                close=100 + index / 100,
                volume=1000 + index,
            )
            for index in range(450)
        ]


class ShortHistoryPriceProvider:
    def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        first = date(2026, 6, 25)
        return [
            DailyKlineBar(
                date=(first + timedelta(days=index)).isoformat(),
                open=100 + index,
                high=101 + index,
                low=99 + index,
                close=100 + index,
                volume=1000 + index,
            )
            for index in range(13)
        ]


def test_range_cache_reuses_requested_end_after_latest_returned_bar() -> None:
    raw_provider = PriceProvider()
    provider = _RangeCachingProvider(raw_provider)

    provider.get_daily_kline(
        "US.MSFT",
        start="2025-04-20",
        end="2026-07-15",
    )
    bars = provider.get_daily_kline(
        "US.MSFT",
        start="2026-07-01",
        end="2026-07-15",
    )

    assert raw_provider.calls == 1
    assert bars
    assert all("2026-07-01" <= bar.date <= "2026-07-15" for bar in bars)


def test_market_generator_runs_available_ranges_and_publishes_futu_source(
    tmp_path: Path, monkeypatch,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,total_quantity,portfolio_weight_hkd,market_value_hkd,fx_to_hkd,ai_eligible\n"
        "US,stock,MSFT,MSFT,10,5.00%,10000,7.8,true\n",
        encoding="utf-8",
    )
    technical = tmp_path / "technical.json"
    technical.write_text("{}\n", encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "records": [{"symbol": "MSFT", "current_action": "观察", "core_reason": "等待"}],
    }), encoding="utf-8")
    requests = []

    def fake_backtest(request, *, price_provider):
        requests.append(request)
        price_provider.get_daily_kline("US.MSFT", start="2025-07-13", end="2026-07-13")
        price_provider.get_daily_kline("US.MSFT", start="2025-07-13", end="2026-07-13")
        return SimpleNamespace(to_dict=lambda: {
            "strategy_id": request.strategy_id,
            "gate": {"passed": True, "policy_id": "benchmark_outperformance/v1", "reasons": []},
            "strategy": {"total_return_pct": "8", "max_drawdown_pct": "5", "sharpe_ratio": "1.2"},
            "market_benchmark": {"symbol": "SPY", "total_return_pct": "4"},
            "market_excess_return_pct": "4",
            "actual_start": "2025-07-13", "actual_end": "2026-07-13",
        })

    monkeypatch.setattr(
        "open_trader.decision_plan_generation.run_standard_backtest",
        fake_backtest,
    )

    provider = PriceProvider()
    result = generate_daily_decision_plans(
        portfolio_path=portfolio, technical_facts_path=technical,
        tradingagents_summary_path=summary, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", run_date="2026-07-13", market="US",
        futu_host="127.0.0.1", futu_port=11111, update_latest=True,
        price_provider=provider,
    )

    assert result.records == 1
    assert {(request.range_preset, request.strategy_id) for request in requests} == {
        (period, strategy)
        for period in ("6M", "1Y")
        for strategy in ("trend_pullback/v1", "breakout_momentum/v1", "range_mean_reversion/v1")
    }
    plan = load_decision_plans(result.latest_path)[0]
    assert plan["mode"] == "validated_plan"
    assert plan["market_data_source"] == "futu"
    assert provider.calls == 1


def test_new_listing_with_short_history_publishes_explainable_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,total_quantity,portfolio_weight_hkd,market_value_hkd,fx_to_hkd,ai_eligible\n"
        "US,etf,RAM,RAM,100,2.00%,10000,7.8,true\n",
        encoding="utf-8",
    )
    technical = tmp_path / "technical.json"
    technical.write_text("{}\n", encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text('{"records":[{"symbol":"RAM"}]}', encoding="utf-8")
    monkeypatch.setattr(
        "open_trader.decision_plan_generation.run_standard_backtest",
        lambda *args, **kwargs: pytest.fail("short history must not run a backtest"),
    )

    result = generate_daily_decision_plans(
        portfolio_path=portfolio, technical_facts_path=technical,
        tradingagents_summary_path=summary, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", run_date="2026-07-13", market="US",
        futu_host="127.0.0.1", futu_port=11111, update_latest=True,
        price_provider=ShortHistoryPriceProvider(),
    )

    plan = load_decision_plans(result.latest_path)[0]
    assert plan["mode"] == "fallback_advice"
    facts = {item["key"]: item for item in plan["fallback"]["facts"]}
    assert facts["ma20_distance_pct"]["calculated_value"] == "insufficient_history"
    assert facts["bollinger_position"]["calculated_value"] == "insufficient_history"


def test_cn_generator_defaults_to_futu_provider(tmp_path: Path, monkeypatch) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,total_quantity,portfolio_weight_hkd,market_value_hkd,fx_to_hkd,ai_eligible\n"
        "CN,stock,600025,600025,100,2.00%,10000,1.08,true\n",
        encoding="utf-8",
    )
    technical = tmp_path / "technical.json"
    technical.write_text("{}\n", encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text('{"records":[{"symbol":"600025"}]}', encoding="utf-8")
    provider = ShortHistoryPriceProvider()
    monkeypatch.setattr(
        "open_trader.decision_plan_generation.FutuQuoteClient",
        lambda **kwargs: provider,
    )

    result = generate_daily_decision_plans(
        portfolio_path=portfolio,
        technical_facts_path=technical,
        tradingagents_summary_path=summary,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-07-13",
        market="CN",
        futu_host="127.0.0.1",
        futu_port=11111,
        update_latest=True,
    )

    plan = load_decision_plans(result.latest_path)[0]
    assert plan["market_data_source"] == "futu"
