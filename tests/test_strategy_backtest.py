from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.kline_technical_facts import DailyKlineBar
from open_trader.strategy_backtest import StandardBacktestRequest, run_standard_backtest


class FixtureProvider:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        benchmark = futu_symbol.endswith(".SPY") or futu_symbol.endswith(".02800")
        first = date(2024, 9, 23)
        rows: list[DailyKlineBar] = []
        for offset in range(170):
            day = first + timedelta(days=offset)
            close = Decimal("100") + Decimal(offset) / Decimal("10")
            volume = Decimal("100")
            if self.scenario == "never_triggers":
                close = Decimal("100")
            if self.scenario == "breakout_next_open" and day == date(2025, 2, 10):
                close, volume = Decimal("120"), Decimal("1000")
            open_price = Decimal("105") if day == date(2025, 2, 11) else close
            if benchmark:
                close = Decimal("200") + Decimal(offset) / Decimal("5")
                open_price = close
            rows.append(DailyKlineBar(
                date=day.isoformat(), open=float(open_price), high=float(max(open_price, close) + 1),
                low=float(min(open_price, close) - 1), close=float(close),
            ))
        return rows


def fixture_provider(scenario: str) -> FixtureProvider:
    return FixtureProvider(scenario)


def standard_request(tmp_path: Path, **overrides: object) -> StandardBacktestRequest:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data", "reports_dir": tmp_path / "reports",
        "market": "US", "symbol": "MSFT", "strategy_id": "trend_pullback/v1",
        "range_preset": None, "custom_start": date(2025, 1, 1),
        "custom_end": date(2026, 1, 1), "initial_cash": Decimal("100000"),
        "max_strategy_weight": Decimal("0.10"), "commission_bps": Decimal("2"),
        "slippage_bps": Decimal("5"),
    }
    for key, value in overrides.items():
        if key == "max_weight":
            values["max_strategy_weight"] = Decimal(str(value))
        else:
            values[key] = value
    return StandardBacktestRequest(**values)  # type: ignore[arg-type]


def test_standard_backtest_executes_signal_at_next_session_open(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    )
    buy = next(trade for trade in result.trades if trade.action == "BUY")
    assert buy.decision_date == "2025-02-10"
    assert buy.execution_date == "2025-02-11"
    assert buy.raw_price == Decimal("105")
    assert buy.execution_price == Decimal("105.0525")


def test_invalid_max_weight_is_rejected_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="最大策略仓位必须大于 0 且不超过 100%"):
        run_standard_backtest(
            replace(standard_request(tmp_path), max_strategy_weight=Decimal("1.1")),
            price_provider=fixture_provider("basic"),
        )


def test_strategy_and_benchmarks_share_capital_range_and_notional(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path, market="US", symbol="MSFT", max_weight="0.10"),
        price_provider=fixture_provider("strategy_and_spy"),
    )
    assert result.benchmark_symbol == "SPY"
    assert result.actual_start == result.buy_hold.actual_start == result.market_benchmark.actual_start
    assert result.actual_end == result.buy_hold.actual_end == result.market_benchmark.actual_end
    assert result.strategy.initial_allocated_notional == Decimal("10000")
    assert result.buy_hold.initial_allocated_notional == Decimal("10000")
    assert result.market_benchmark.initial_allocated_notional == Decimal("10000")
    assert result.strategy_excess_return_pct == result.strategy.total_return_pct - result.buy_hold.total_return_pct


def test_run_writes_reproducible_manifest_and_normalized_artifacts(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("basic"))
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["strategy"]["id"] == "trend_pullback/v1"
    assert manifest["adapter"] == {"name": "backtrader", "version": result.adapter_version}
    assert manifest["sources"]["symbol"]["sha256"]
    assert manifest["requested_range"] == {"start": "2025-01-01", "end": "2026-01-01"}
    assert result.signals_path.exists()
    assert result.trades_path.exists()
    assert result.equity_curve_path.exists()


def test_zero_trade_run_is_successful(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("never_triggers"))
    assert result.status == "ok"
    assert result.trade_count == 0
    assert result.message_zh == "所选区间内没有触发交易"
