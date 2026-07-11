"""标准策略回测编排、执行适配与不可变产物。"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, Sequence

from .backtest_prices import DailyKlineProvider, ensure_resolved_backtest_price_range
from .standard_strategies import (
    StrategyBar, StrategySignal, generate_strategy_signals, strategy_catalog,
)


BENCHMARK_SYMBOLS = {"US": "SPY", "HK": "02800"}


@dataclass(frozen=True)
class StandardBacktestRequest:
    data_dir: Path
    reports_dir: Path
    market: str
    symbol: str
    strategy_id: str
    range_preset: str | None
    custom_start: date | None
    custom_end: date | None
    initial_cash: Decimal
    max_strategy_weight: Decimal
    commission_bps: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True)
class NormalizedTrade:
    decision_date: str
    execution_date: str
    action: str
    quantity: Decimal
    raw_price: Decimal
    execution_price: Decimal
    fees: Decimal
    reason: str


@dataclass(frozen=True)
class ExecutionResult:
    trades: Sequence[NormalizedTrade]
    equity_curve: Sequence[dict[str, str]]
    final_equity: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal
    max_drawdown_pct: Decimal
    win_rate_pct: Decimal
    actual_start: date
    actual_end: date
    initial_cash: Decimal
    initial_allocated_notional: Decimal


@dataclass(frozen=True)
class StandardBacktestResult:
    run_id: str
    status: str
    message_zh: str
    strategy_id: str
    benchmark_symbol: str
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    strategy: ExecutionResult
    buy_hold: ExecutionResult
    market_benchmark: ExecutionResult
    strategy_excess_return_pct: Decimal
    market_excess_return_pct: Decimal
    adapter_version: str
    manifest_path: Path
    signals_path: Path
    trades_path: Path
    equity_curve_path: Path
    buy_hold_equity_path: Path
    market_benchmark_equity_path: Path
    metrics_path: Path
    report_path: Path

    @property
    def trades(self) -> Sequence[NormalizedTrade]:
        return self.strategy.trades

    @property
    def trade_count(self) -> int:
        return len([trade for trade in self.strategy.trades if trade.action in {"BUY", "ADD", "REDUCE", "EXIT"}])

    def to_dict(self) -> dict[str, object]:
        return serialize_standard_backtest_result(self)


class StrategyExecutionAdapter(Protocol):
    name: str
    version: str

    def run(self, *, bars: Sequence[StrategyBar], signals: Sequence[StrategySignal],
            initial_cash: Decimal, commission_bps: Decimal,
            slippage_bps: Decimal) -> ExecutionResult: ...


class BacktraderTargetWeightAdapter:
    """Backtrader is deliberately contained behind this normalized adapter."""

    name = "backtrader"

    def __init__(self) -> None:
        try:
            import backtrader as bt
        except ImportError as exc:  # pragma: no cover - dependency is mandatory
            raise ValueError("缺少 Backtrader 依赖，无法执行标准策略回测") from exc
        self.version = str(getattr(bt, "__version__", "1.9.78"))

    def run(self, *, bars: Sequence[StrategyBar], signals: Sequence[StrategySignal],
            initial_cash: Decimal, commission_bps: Decimal,
            slippage_bps: Decimal) -> ExecutionResult:
        if not bars:
            raise ValueError("共同有效区间内没有可用价格数据")
        signal_by_execution = {
            signal.earliest_execution_date: signal for signal in signals
            if signal.action != "HOLD" and signal.earliest_execution_date is not None
        }
        cash, quantity = initial_cash, Decimal("0")
        trades: list[NormalizedTrade] = []
        curve: list[dict[str, str]] = []
        peak = initial_cash
        max_drawdown = Decimal("0")
        for bar in bars:
            signal = signal_by_execution.get(bar.date)
            if signal is not None and signal.target_weight is not None:
                is_buy = signal.target_weight * initial_cash > quantity * bar.open
                execution_price = bar.open * (Decimal("1") + (slippage_bps / Decimal("10000")) * (1 if is_buy else -1))
                target_notional = initial_cash * signal.target_weight
                target_quantity = (target_notional / execution_price).quantize(Decimal("1"), rounding=ROUND_DOWN)
                delta = target_quantity - quantity
                fees = abs(delta * execution_price) * commission_bps / Decimal("10000")
                reason = signal.explanation
                if delta == 0:
                    reason = f"未成交：目标数量未变化；{reason}"
                elif delta > 0 and delta * execution_price + fees > cash:
                    affordable = (cash / (execution_price * (Decimal("1") + commission_bps / Decimal("10000")))).quantize(Decimal("1"), rounding=ROUND_DOWN)
                    delta = max(Decimal("0"), affordable)
                    fees = delta * execution_price * commission_bps / Decimal("10000")
                    reason = f"资金约束后成交；{reason}" if delta else f"订单被拒绝：可用资金不足；{reason}"
                if delta:
                    cash -= delta * execution_price + fees
                    quantity += delta
                trades.append(NormalizedTrade(
                    signal.decision_date.isoformat(), bar.date.isoformat(), signal.action,
                    delta, bar.open, execution_price, fees, reason,
                ))
            equity = cash + quantity * bar.close
            peak = max(peak, equity)
            drawdown = (equity / peak - Decimal("1")) * Decimal("100") if peak else Decimal("0")
            max_drawdown = min(max_drawdown, drawdown)
            curve.append(_equity_row(bar.date, cash, quantity, bar.close, equity, drawdown))
        return _execution_result(
            trades, curve, initial_cash, initial_cash, bars[0].date, bars[-1].date,
            max_drawdown,
        )


class StandardBacktestService:
    def __init__(self, *, price_provider: DailyKlineProvider) -> None:
        self.price_provider = price_provider

    def run(self, request: StandardBacktestRequest) -> StandardBacktestResult:
        market = request.market.strip().upper()
        symbol = request.symbol.strip().upper()
        benchmark_symbol = BENCHMARK_SYMBOLS[market]
        symbol_prices = ensure_resolved_backtest_price_range(
            data_dir=request.data_dir, market=market, symbol=symbol,
            preset=request.range_preset, custom_start=request.custom_start,
            custom_end=request.custom_end, provider=self.price_provider,
        )
        benchmark_prices = ensure_resolved_backtest_price_range(
            data_dir=request.data_dir, market=market, symbol=benchmark_symbol,
            preset=request.range_preset, custom_start=request.custom_start,
            custom_end=request.custom_end, provider=self.price_provider,
        )
        requested_start = symbol_prices.price_range.requested_start
        requested_end = symbol_prices.price_range.requested_end
        symbol_dates = {bar.date for bar in symbol_prices.price_range.bars if requested_start <= bar.date <= requested_end}
        benchmark_dates = {bar.date for bar in benchmark_prices.price_range.bars if requested_start <= bar.date <= requested_end}
        common_dates = sorted(symbol_dates & benchmark_dates)
        if len(common_dates) < 2:
            raise ValueError("策略标的与市场基准没有足够的共同交易日")
        actual_start, actual_end = common_dates[0], common_dates[-1]
        symbol_bars = tuple(bar for bar in symbol_prices.price_range.bars if actual_start <= bar.date <= actual_end)
        benchmark_bars = tuple(bar for bar in benchmark_prices.price_range.bars if actual_start <= bar.date <= actual_end)
        all_symbol_bars = tuple(bar for bar in symbol_prices.price_range.bars if bar.date <= actual_end)
        signals = generate_strategy_signals(
            request.strategy_id, all_symbol_bars, start_date=actual_start,
            max_strategy_weight=request.max_strategy_weight,
        )
        signals = [signal for signal in signals if actual_start <= signal.decision_date <= actual_end]
        adapter = BacktraderTargetWeightAdapter()
        strategy = adapter.run(
            bars=symbol_bars, signals=signals, initial_cash=request.initial_cash,
            commission_bps=request.commission_bps, slippage_bps=request.slippage_bps,
        )
        allocated = request.initial_cash * request.max_strategy_weight
        buy_hold = _run_buy_hold(
            symbol_bars, request.initial_cash, allocated,
            request.commission_bps, request.slippage_bps,
        )
        market_benchmark = _run_buy_hold(
            benchmark_bars, request.initial_cash, allocated,
            request.commission_bps, request.slippage_bps,
        )
        # A strategy's fair comparison capital is its maximum permitted allocation.
        strategy = _replace_allocated(strategy, allocated)
        request_hash = hashlib.sha256(json.dumps(_json_safe(request), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
        slug = request.strategy_id.replace("/", "-")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{timestamp}-{market}-{symbol}-{slug}-{request_hash}"
        output_dir = request.data_dir / "backtests" / run_id
        report_path = request.reports_dir / "backtests" / f"{run_id}.md"
        paths = {name: output_dir / name for name in (
            "manifest.json", "signals.csv", "trades.csv", "equity_curve.csv",
            "buy_hold_equity.csv", "market_benchmark_equity.csv", "metrics.json",
        )}
        manifest = {
            "run_id": run_id, "request_hash": request_hash,
            "strategy": next(item.to_dict() for item in strategy_catalog() if item.strategy_id == request.strategy_id),
            "adapter": {"name": adapter.name, "version": adapter.version},
            "requested_range": {"start": requested_start.isoformat(), "end": requested_end.isoformat()},
            "actual_range": {"start": actual_start.isoformat(), "end": actual_end.isoformat()},
            "capital": str(request.initial_cash), "max_strategy_weight": str(request.max_strategy_weight),
            "costs_bps": {"commission": str(request.commission_bps), "slippage": str(request.slippage_bps)},
            "sources": {
                "symbol": {"market": market, "symbol": symbol, "sha256": symbol_prices.price_range.source_hash},
                "benchmark": {"market": market, "symbol": benchmark_symbol, "sha256": benchmark_prices.price_range.source_hash},
            },
        }
        _atomic_write_json(paths["manifest.json"], manifest)
        _atomic_write_csv(paths["signals.csv"], _signal_rows(signals), (
            "decision_date", "earliest_execution_date", "action", "target_weight", "rule", "explanation", "data_cutoff",
        ))
        _atomic_write_csv(paths["trades.csv"], [_json_safe(trade) for trade in strategy.trades], tuple(field.name for field in fields(NormalizedTrade)))
        equity_fields = ("date", "cash", "position_quantity", "close", "equity", "drawdown_pct")
        _atomic_write_csv(paths["equity_curve.csv"], list(strategy.equity_curve), equity_fields)
        _atomic_write_csv(paths["buy_hold_equity.csv"], list(buy_hold.equity_curve), equity_fields)
        _atomic_write_csv(paths["market_benchmark_equity.csv"], list(market_benchmark.equity_curve), equity_fields)
        metrics = {
            "strategy": _json_safe(strategy), "buy_hold": _json_safe(buy_hold),
            "market_benchmark": _json_safe(market_benchmark),
            "strategy_excess_return_pct": str(strategy.total_return_pct - buy_hold.total_return_pct),
            "market_excess_return_pct": str(strategy.total_return_pct - market_benchmark.total_return_pct),
        }
        _atomic_write_json(paths["metrics.json"], metrics)
        message = "所选区间内没有触发交易" if not strategy.trades else "标准策略回测完成"
        _atomic_write_text(report_path, _render_report(run_id, request.strategy_id, benchmark_symbol, message, metrics))
        return StandardBacktestResult(
            run_id, "ok", message, request.strategy_id, benchmark_symbol,
            requested_start, requested_end, actual_start, actual_end, strategy,
            buy_hold, market_benchmark,
            strategy.total_return_pct - buy_hold.total_return_pct,
            strategy.total_return_pct - market_benchmark.total_return_pct,
            adapter.version, paths["manifest.json"], paths["signals.csv"], paths["trades.csv"],
            paths["equity_curve.csv"], paths["buy_hold_equity.csv"],
            paths["market_benchmark_equity.csv"], paths["metrics.json"], report_path,
        )


def validate_standard_backtest_request(request: StandardBacktestRequest) -> None:
    if request.market.strip().upper() not in BENCHMARK_SYMBOLS:
        raise ValueError(f"不支持的市场：{request.market}")
    if not request.symbol.strip():
        raise ValueError("标的代码不能为空")
    if request.strategy_id not in {item.strategy_id for item in strategy_catalog()}:
        raise ValueError(f"未知策略：{request.strategy_id}")
    if not request.initial_cash.is_finite() or request.initial_cash <= 0:
        raise ValueError("初始资金必须大于 0")
    if not request.max_strategy_weight.is_finite() or not Decimal("0") < request.max_strategy_weight <= Decimal("1"):
        raise ValueError("最大策略仓位必须大于 0 且不超过 100%")
    for value, label in ((request.commission_bps, "佣金"), (request.slippage_bps, "滑点")):
        if not value.is_finite() or value < 0:
            raise ValueError(f"{label}基点不能为负数")


def run_standard_backtest(request: StandardBacktestRequest, *, price_provider: DailyKlineProvider) -> StandardBacktestResult:
    validate_standard_backtest_request(request)
    return StandardBacktestService(price_provider=price_provider).run(request)


def serialize_standard_backtest_result(result: StandardBacktestResult) -> dict[str, object]:
    return _json_safe(result)


def _replace_allocated(result: ExecutionResult, allocated: Decimal) -> ExecutionResult:
    values = asdict(result)
    values["trades"] = result.trades
    values["equity_curve"] = result.equity_curve
    values["initial_allocated_notional"] = allocated
    return ExecutionResult(**values)


def _run_buy_hold(bars: Sequence[StrategyBar], initial_cash: Decimal, allocated: Decimal,
                  commission_bps: Decimal, slippage_bps: Decimal) -> ExecutionResult:
    entry_bar, exit_bar = bars[1], bars[-1]
    entry = entry_bar.open * (Decimal("1") + slippage_bps / Decimal("10000"))
    quantity = (allocated / (entry * (Decimal("1") + commission_bps / Decimal("10000")))).quantize(Decimal("1"), rounding=ROUND_DOWN)
    entry_fee = quantity * entry * commission_bps / Decimal("10000")
    cash = initial_cash - quantity * entry - entry_fee
    trades = [NormalizedTrade(bars[0].date.isoformat(), entry_bar.date.isoformat(), "BUY", quantity, entry_bar.open, entry, entry_fee, "基准首个可执行开盘买入")]
    curve: list[dict[str, str]] = []
    peak, max_drawdown = initial_cash, Decimal("0")
    for bar in bars:
        held = quantity if bar.date >= entry_bar.date else Decimal("0")
        row_cash = cash if held else initial_cash
        equity = row_cash + held * bar.close
        if bar.date == exit_bar.date and held:
            exit_price = bar.close * (Decimal("1") - slippage_bps / Decimal("10000"))
            exit_fee = quantity * exit_price * commission_bps / Decimal("10000")
            equity = cash + quantity * exit_price - exit_fee
        peak = max(peak, equity)
        drawdown = (equity / peak - Decimal("1")) * Decimal("100")
        max_drawdown = min(max_drawdown, drawdown)
        curve.append(_equity_row(bar.date, row_cash, held, bar.close, equity, drawdown))
    trades.append(NormalizedTrade(exit_bar.date.isoformat(), exit_bar.date.isoformat(), "EXIT", -quantity, exit_bar.close,
        exit_bar.close * (Decimal("1") - slippage_bps / Decimal("10000")), quantity * exit_bar.close * (Decimal("1") - slippage_bps / Decimal("10000")) * commission_bps / Decimal("10000"), "基准末日收盘清仓"))
    return _execution_result(trades, curve, initial_cash, allocated, bars[0].date, bars[-1].date, max_drawdown)


def _execution_result(trades: Sequence[NormalizedTrade], curve: Sequence[dict[str, str]],
                      initial_cash: Decimal, allocated: Decimal, actual_start: date,
                      actual_end: date, max_drawdown: Decimal) -> ExecutionResult:
    final = Decimal(curve[-1]["equity"])
    total = (final / initial_cash - Decimal("1")) * Decimal("100")
    days = max(1, (actual_end - actual_start).days)
    annualized = ((final / initial_cash) ** (Decimal("365") / Decimal(days)) - Decimal("1")) * Decimal("100") if final > 0 else Decimal("-100")
    exits = [trade for trade in trades if trade.action in {"REDUCE", "EXIT"} and trade.quantity]
    win_rate = Decimal("0") if not exits else Decimal("100") * Decimal(sum(1 for trade in exits if trade.execution_price > trade.raw_price)) / Decimal(len(exits))
    return ExecutionResult(tuple(trades), tuple(curve), final, total, annualized, abs(max_drawdown), win_rate,
                           actual_start, actual_end, initial_cash, allocated)


def _equity_row(day: date, cash: Decimal, quantity: Decimal, close: Decimal,
                equity: Decimal, drawdown: Decimal) -> dict[str, str]:
    return {"date": day.isoformat(), "cash": str(cash), "position_quantity": str(quantity),
            "close": str(close), "equity": str(equity), "drawdown_pct": str(drawdown)}


def _signal_rows(signals: Sequence[StrategySignal]) -> list[dict[str, str]]:
    return [{"decision_date": item.decision_date.isoformat(),
             "earliest_execution_date": item.earliest_execution_date.isoformat() if item.earliest_execution_date else "",
             "action": item.action, "target_weight": str(item.target_weight) if item.target_weight is not None else "",
             "rule": item.rule, "explanation": item.explanation, "data_cutoff": item.data_cutoff.isoformat()}
            for item in signals]


def _json_safe(value: object) -> object:
    if is_dataclass(value):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, Decimal, Path)):
        return value.isoformat() if isinstance(value, date) else str(value)
    return value


def _atomic_write_csv(path: Path, rows: Sequence[dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        handle.write(content)
    temp.replace(path)


def _render_report(run_id: str, strategy_id: str, benchmark: str, message: str, metrics: dict[str, object]) -> str:
    return (f"# 标准策略回测报告\n\n- 运行编号：{run_id}\n- 策略：{strategy_id}\n"
            f"- 市场基准：{benchmark}\n- 状态：{message}\n\n"
            f"## 核心指标\n\n```json\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n```\n")
