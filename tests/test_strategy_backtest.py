from __future__ import annotations

import csv
import json
import hashlib
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.kline_technical_facts import DailyKlineBar
import backtrader as bt

from open_trader.standard_strategies import StrategyBar, StrategySignal
from open_trader.strategy_backtest import (
    BacktraderTargetWeightAdapter, NormalizedTrade, StandardBacktestRequest,
    _execution_result, _run_buy_hold, run_standard_backtest,
)


class FixtureProvider:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        benchmark = futu_symbol.endswith(".SPY") or futu_symbol.endswith(".02800")
        if self.scenario == "missing_benchmark" and benchmark:
            return []
        if self.scenario == "broken_benchmark" and benchmark:
            raise ValueError("基准服务认证失败")
        first = date(2024, 9, 23)
        rows: list[DailyKlineBar] = []
        for offset in range(170):
            day = first + timedelta(days=offset)
            if self.scenario == "unequal_calendar" and benchmark and day == date(2025, 2, 3):
                continue
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
                volume=float(volume),
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


def test_breakout_rejects_zero_only_volume_in_chinese(tmp_path: Path) -> None:
    class ZeroVolumeProvider(FixtureProvider):
        def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
            return [replace(bar, volume=0.0) for bar in super().get_daily_kline(futu_symbol, start=start, end=end)]

    with pytest.raises(ValueError, match="突破动量策略需要有效的非零成交量数据"):
        run_standard_backtest(
            standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
            price_provider=ZeroVolumeProvider("breakout_next_open"),
        )


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


def test_cn_standard_backtest_uses_csi_300_benchmark(tmp_path: Path) -> None:
    request = standard_request(tmp_path, market="CN", symbol="600025")
    result = run_standard_backtest(request, price_provider=fixture_provider("basic"))
    assert result.benchmark_symbol == "000300"


def test_result_payload_exposes_manifest_backed_assumptions_definition_and_signals(tmp_path: Path) -> None:
    payload = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("never_triggers")).to_dict()
    assert payload["assumptions"] == {
        "initial_cash": "100000", "max_strategy_weight": "0.10",
        "commission_bps": "2", "slippage_bps": "5",
    }
    assert payload["strategy_definition"]["id"] == "trend_pullback/v1"
    assert payload["strategy_definition"]["name_zh"] == "趋势回调"
    assert payload["strategy_definition"]["parameters"]["sma_short"] == "20"
    assert payload["signals"]
    assert {"market", "symbol", "strategy_id", "strategy_version", "parameters", "decision_date", "earliest_execution_date", "action", "target_weight", "rule", "explanation", "data_cutoff"} <= payload["signals"][0].keys()
    assert payload["signals"][0]["market"] == "US"
    assert payload["signals"][0]["symbol"] == "MSFT"
    assert payload["signals"][0]["strategy_id"] == "trend_pullback/v1"
    assert payload["signals"][0]["strategy_version"] == "v1"
    assert payload["signals"][0]["parameters"]["sma_short"] == "20"
    assert any(signal["action"] == "HOLD" for signal in payload["signals"])


def test_missing_market_benchmark_degrades_only_market_comparison(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path), price_provider=fixture_provider("missing_benchmark"),
    )
    payload = result.to_dict()
    assert result.strategy.equity_curve and result.buy_hold.equity_curve
    assert payload["market_benchmark"] is None
    assert payload["market_excess_return_pct"] is None
    assert payload["market_benchmark_error"] == "基准行情缺失，无法比较"
    assert payload["market_benchmark_equity_path"] is None
    assert payload["gate"] == {
        "passed": False,
        "policy_id": "benchmark_outperformance/v1",
        "reasons": ["benchmark_data_missing"],
    }


def test_standard_backtest_serializes_sharpe_and_passing_gate(tmp_path: Path) -> None:
    class FlatBenchmarkProvider(FixtureProvider):
        def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
            bars = super().get_daily_kline(futu_symbol, start=start, end=end)
            if not futu_symbol.endswith(".SPY"):
                return bars
            return [
                replace(bar, open=200.0, high=201.0, low=199.0, close=200.0)
                for bar in bars
            ]

    payload = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=FlatBenchmarkProvider("breakout_next_open"),
    ).to_dict()

    assert payload["strategy"]["sharpe_ratio"] is not None
    assert "calmar_ratio" in payload["strategy"]
    assert payload["gate"] == {
        "passed": True,
        "policy_id": "benchmark_outperformance/v1",
        "reasons": [],
    }


def test_market_benchmark_degradation_does_not_swallow_unrelated_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="基准服务认证失败"):
        run_standard_backtest(
            standard_request(tmp_path), price_provider=fixture_provider("broken_benchmark"),
        )


def test_run_writes_reproducible_manifest_and_normalized_artifacts(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("basic"))
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["strategy"]["id"] == "trend_pullback/v1"
    assert manifest["adapter"] == {"name": "backtrader", "version": result.adapter_version}
    assert manifest["sources"]["symbol"]["sha256"]
    created_at = datetime.fromisoformat(manifest["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset().total_seconds() == 0
    assert manifest["requested_range"] == {"start": "2025-01-01", "end": "2026-01-01"}
    expected_data_paths = {
        "signals.csv": f"backtests/{result.run_id}/signals.csv",
        "trades.csv": f"backtests/{result.run_id}/trades.csv",
        "equity_curve.csv": f"backtests/{result.run_id}/equity_curve.csv",
        "buy_hold_equity.csv": f"backtests/{result.run_id}/buy_hold_equity.csv",
        "market_benchmark_equity.csv": f"backtests/{result.run_id}/market_benchmark_equity.csv",
        "metrics.json": f"backtests/{result.run_id}/metrics.json",
        "report.md": f"backtests/{result.run_id}/report.md",
    }
    assert {name: item["path"] for name, item in manifest["artifacts"].items()} == expected_data_paths
    assert manifest["report"] == {
        "path": f"backtests/{result.run_id}.md",
        "sha256": hashlib.sha256(result.report_path.read_bytes()).hexdigest(),
    }
    assert "manifest.json" not in manifest["artifacts"]
    assert result.signals_path.exists()
    assert result.trades_path.exists()
    assert result.equity_curve_path.exists()


def test_detached_signals_csv_self_describes_strategy_and_parameters(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("never_triggers"))
    with result.signals_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert {row["market"] for row in rows} == {"US"}
    assert {row["symbol"] for row in rows} == {"MSFT"}
    assert {row["strategy_id"] for row in rows} == {"trend_pullback/v1"}
    assert {row["strategy_version"] for row in rows} == {"v1"}
    expected = json.dumps(result.strategy_definition["parameters"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert {row["parameters"] for row in rows} == {expected}


def test_zero_trade_run_is_successful(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("never_triggers"))
    assert result.status == "ok"
    assert result.trade_count == 0
    assert result.message_zh == "所选区间内没有触发交易"


def test_zero_quantity_attempt_uses_zero_trade_success_message(tmp_path: Path) -> None:
    result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1", initial_cash=Decimal("1")),
        price_provider=fixture_provider("breakout_next_open"),
    )
    assert result.trades
    assert all(trade.quantity == 0 for trade in result.trades)
    assert result.trade_count == 0
    assert result.message_zh == "所选区间内没有触发交易"


def _bar(day: date, price: str) -> StrategyBar:
    value = Decimal(price)
    return StrategyBar(day, value, value, value, value, Decimal("100"))


def _signal(decision: date, execution: date, action: str, weight: str) -> StrategySignal:
    return StrategySignal(decision, execution, action, Decimal(weight), "test", "测试信号", decision)  # type: ignore[arg-type]


def test_adapter_runs_real_cerebro_and_notify_order_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = bt.Cerebro.run

    def tracked(self: bt.Cerebro, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(bt.Cerebro, "run", tracked)
    days = [date(2025, 1, day) for day in range(2, 6)]
    result = BacktraderTargetWeightAdapter().run(
        bars=[_bar(day, price) for day, price in zip(days, ("100", "101", "102", "103"))],
        signals=[_signal(days[0], days[1], "BUY", "0.1")], initial_cash=Decimal("100000"),
        commission_bps=Decimal("2"), slippage_bps=Decimal("5"),
    )
    assert calls == 1
    assert any(trade.quantity > 0 for trade in result.trades)
    assert any(trade.reason == "回测期末平仓" for trade in result.trades)


def test_unequal_calendars_are_intersected_by_exact_date_set(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("unequal_calendar"))
    curves = (result.strategy.equity_curve, result.buy_hold.equity_curve, result.market_benchmark.equity_curve)
    date_sets = [[row["date"] for row in curve] for curve in curves]
    assert date_sets[0] == date_sets[1] == date_sets[2]
    assert "2025-02-03" not in date_sets[0]


def test_terminal_strategy_position_is_liquidated_with_cost_reconciliation() -> None:
    days = [date(2025, 1, day) for day in range(2, 6)]
    result = BacktraderTargetWeightAdapter().run(
        bars=[_bar(day, price) for day, price in zip(days, ("100", "100", "110", "120"))],
        signals=[_signal(days[0], days[1], "BUY", "0.1")], initial_cash=Decimal("100000"),
        commission_bps=Decimal("10"), slippage_bps=Decimal("10"),
    )
    buy, sell = [trade for trade in result.trades if trade.quantity]
    assert buy.execution_price == Decimal("100.1")
    assert sell.execution_price == Decimal("119.88")
    assert sell.action == "EXIT" and sell.reason == "回测期末平仓"
    expected = Decimal("100000") - buy.quantity * buy.execution_price - buy.fees
    expected += abs(sell.quantity) * sell.execution_price - sell.fees
    assert result.final_equity == expected
    assert Decimal(result.equity_curve[-1]["position_quantity"]) == 0


def test_win_rate_uses_completed_round_trip_realized_pnl() -> None:
    days = [date(2025, 1, day) for day in range(2, 9)]
    bars = [_bar(day, price) for day, price in zip(days, ("100", "100", "120", "120", "100", "100", "80"))]
    signals = [
        _signal(days[0], days[1], "BUY", "0.1"), _signal(days[1], days[2], "EXIT", "0"),
        _signal(days[3], days[4], "BUY", "0.1"), _signal(days[4], days[5], "EXIT", "0"),
    ]
    result = BacktraderTargetWeightAdapter().run(
        bars=bars, signals=signals, initial_cash=Decimal("100000"),
        commission_bps=Decimal("0"), slippage_bps=Decimal("0"),
    )
    assert result.win_rate_pct == Decimal("50")


def test_conflicting_same_execution_date_signals_are_rejected_in_chinese() -> None:
    days = [date(2025, 1, day) for day in range(2, 6)]
    with pytest.raises(ValueError, match="同一执行日存在冲突策略信号"):
        BacktraderTargetWeightAdapter().run(
            bars=[_bar(day, "100") for day in days],
            signals=[_signal(days[0], days[1], "BUY", "0.1"), _signal(days[0] - timedelta(days=1), days[1], "EXIT", "0")],
            initial_cash=Decimal("100000"), commission_bps=Decimal("0"), slippage_bps=Decimal("0"),
        )


def test_manifest_has_schema_and_verified_artifact_hashes(tmp_path: Path) -> None:
    result = run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("basic"))
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["schema_version"] == "open_trader.standard_backtest.manifest.v1"
    for filename, digest in manifest["artifacts"].items():
        assert hashlib.sha256((result.manifest_path.parent / filename).read_bytes()).hexdigest() == digest["sha256"]
    payload = result.to_dict()
    assert payload["strategy"]["initial_allocated_notional"] == "10000.00"
    assert payload["actual_start"] == result.actual_start.isoformat()
    assert result.strategy.annualized_return_pct.is_finite()
    assert result.strategy.max_drawdown_pct >= 0


def test_artifact_failure_leaves_no_partial_final_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.strategy_backtest as module
    original = module._write_run_artifact
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        original(*args, **kwargs)

    monkeypatch.setattr(module, "_write_run_artifact", fail_second)
    with pytest.raises(ValueError, match="回测产物写入失败"):
        run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("basic"))
    assert not list((tmp_path / "data" / "backtests").iterdir())


def test_run_collision_refuses_to_overwrite_in_chinese(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.strategy_backtest as module
    monkeypatch.setattr(module, "_build_run_id", lambda *args, **kwargs: "fixed-run")
    request = standard_request(tmp_path)
    run_standard_backtest(request, price_provider=fixture_provider("basic"))
    with pytest.raises(ValueError, match="回测运行编号已存在，拒绝覆盖"):
        run_standard_backtest(request, price_provider=fixture_provider("basic"))


def test_reduce_uses_sell_slippage_for_target_quantity() -> None:
    days = [date(2025, 1, day) for day in range(2, 7)]
    result = BacktraderTargetWeightAdapter().run(
        bars=[_bar(day, "100") for day in days],
        signals=[_signal(days[0], days[1], "BUY", "0.1"), _signal(days[2], days[3], "REDUCE", "0.05")],
        initial_cash=Decimal("100000"), commission_bps=Decimal("0"), slippage_bps=Decimal("10"),
    )
    reduce = next(trade for trade in result.trades if trade.action == "REDUCE")
    assert reduce.execution_price == Decimal("99.9")
    assert abs(reduce.quantity) == Decimal("49")  # 99 shares -> target floor(5000 / 99.9) = 50


def test_strategy_entry_sizing_includes_commission_and_matches_benchmark() -> None:
    days = [date(2025, 1, day) for day in range(2, 6)]
    bars = [_bar(day, "10") for day in days]
    strategy = BacktraderTargetWeightAdapter().run(
        bars=bars, signals=[_signal(days[0], days[1], "BUY", "0.1")],
        initial_cash=Decimal("1000"), commission_bps=Decimal("100"), slippage_bps=Decimal("0"),
    )
    benchmark = _run_buy_hold(
        bars, Decimal("1000"), Decimal("100"), Decimal("100"), Decimal("0"),
    )
    strategy_buy = next(trade for trade in strategy.trades if trade.action == "BUY")
    benchmark_buy = next(trade for trade in benchmark.trades if trade.action == "BUY")
    assert strategy_buy.quantity == benchmark_buy.quantity == Decimal("9")
    assert strategy_buy.quantity * strategy_buy.execution_price + strategy_buy.fees <= Decimal("100")


def test_terminal_reason_is_chinese_in_trade_csv_and_report_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_trader.strategy_backtest as module
    monkeypatch.setattr(
        module, "generate_strategy_signals",
        lambda *args, **kwargs: [_signal(date(2025, 1, 1), date(2025, 1, 2), "BUY", "0.1")],
    )
    result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    )
    assert any(trade.reason == "回测期末平仓" for trade in result.trades)
    assert "end_of_backtest" not in result.trades_path.read_text(encoding="utf-8")
    assert "end_of_backtest" not in result.report_path.read_text(encoding="utf-8")
    assert "回测期末平仓" in result.trades_path.read_text(encoding="utf-8")


def test_annualized_return_and_drawdown_match_hand_calculation() -> None:
    rows = [
        {"date": "2025-01-01", "cash": "100", "position_quantity": "0", "close": "1", "equity": "100", "drawdown_pct": "0"},
        {"date": "2025-07-01", "cash": "80", "position_quantity": "0", "close": "1", "equity": "80", "drawdown_pct": "-20"},
        {"date": "2026-01-01", "cash": "121", "position_quantity": "0", "close": "1", "equity": "121", "drawdown_pct": "0"},
    ]
    result = _execution_result([], rows, Decimal("100"), Decimal("10"), date(2025, 1, 1), date(2026, 1, 1), Decimal("-20"))
    assert result.total_return_pct == Decimal("21.00")
    assert result.annualized_return_pct == Decimal("21.00")
    assert result.max_drawdown_pct == Decimal("20")
    assert result.sharpe_ratio is not None
    assert result.calmar_ratio == Decimal("1.05")


def test_existing_publication_lock_refuses_run_without_touching_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_trader.strategy_backtest as module
    monkeypatch.setattr(module, "_build_run_id", lambda *args, **kwargs: "fixed-run")
    parent = tmp_path / "data" / "backtests"
    parent.mkdir(parents=True)
    lock = parent / ".fixed-run.lock"
    lock.write_text("first publisher", encoding="utf-8")
    with pytest.raises(ValueError, match="回测运行编号已存在，拒绝覆盖"):
        run_standard_backtest(standard_request(tmp_path), price_provider=fixture_provider("basic"))
    assert lock.read_text(encoding="utf-8") == "first publisher"
    assert not (parent / "fixed-run").exists()
