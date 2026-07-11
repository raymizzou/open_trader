"""标准策略回测编排、Backtrader 执行适配与不可变产物。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, Sequence
from uuid import uuid4

from .backtest_prices import DailyKlineProvider, ensure_resolved_backtest_price_range
from .standard_strategies import StrategyBar, StrategySignal, generate_strategy_signals, strategy_catalog


BENCHMARK_SYMBOLS = {"US": "SPY", "HK": "02800"}
MANIFEST_SCHEMA_VERSION = "open_trader.standard_backtest.manifest.v1"
EQUITY_FIELDS = ("date", "cash", "position_quantity", "close", "equity", "drawdown_pct")


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
    market_benchmark: ExecutionResult | None
    strategy_excess_return_pct: Decimal
    market_excess_return_pct: Decimal | None
    market_benchmark_error: str | None
    assumptions: dict[str, str]
    strategy_definition: dict[str, object]
    signals: Sequence[dict[str, str]]
    adapter_version: str
    manifest_path: Path
    signals_path: Path
    trades_path: Path
    equity_curve_path: Path
    buy_hold_equity_path: Path
    market_benchmark_equity_path: Path | None
    metrics_path: Path
    report_path: Path

    @property
    def trades(self) -> Sequence[NormalizedTrade]:
        return self.strategy.trades

    @property
    def trade_count(self) -> int:
        return sum(1 for trade in self.strategy.trades if trade.quantity != 0)

    def to_dict(self) -> dict[str, object]:
        return serialize_standard_backtest_result(self)


class StrategyExecutionAdapter(Protocol):
    name: str
    version: str

    def run(self, *, bars: Sequence[StrategyBar], signals: Sequence[StrategySignal],
            initial_cash: Decimal, commission_bps: Decimal,
            slippage_bps: Decimal) -> ExecutionResult: ...


class BacktraderTargetWeightAdapter:
    """唯一执行适配器；Backtrader 依赖不会泄漏到策略定义层。"""

    name = "backtrader"

    def __init__(self) -> None:
        try:
            import backtrader as bt
        except ImportError as exc:  # pragma: no cover
            raise ValueError("缺少 Backtrader 依赖，无法执行标准策略回测") from exc
        self._bt = bt
        self.version = str(getattr(bt, "__version__", "1.9.78"))

    def run(self, *, bars: Sequence[StrategyBar], signals: Sequence[StrategySignal],
            initial_cash: Decimal, commission_bps: Decimal,
            slippage_bps: Decimal) -> ExecutionResult:
        if not bars:
            raise ValueError("共同有效区间内没有可用价格数据")
        bt = self._bt
        ordered_bars = tuple(bars)
        signal_groups = _group_signals(signals)
        bar_index = {bar.date: index for index, bar in enumerate(ordered_bars)}
        next_open_by_decision = {
            signal.decision_date: ordered_bars[bar_index[signal.earliest_execution_date]].open
            for signal in signals
            if signal.action != "HOLD" and signal.earliest_execution_date in bar_index
        }
        last_bar = ordered_bars[-1]

        class BarFeed(bt.feeds.DataBase):  # type: ignore[misc]
            lines = ("volume",)

            def __init__(self) -> None:
                super().__init__()
                self._rows = list(ordered_bars) + [StrategyBar(
                    last_bar.date + timedelta(days=1), last_bar.close, last_bar.close,
                    last_bar.close, last_bar.close, Decimal("0"),
                )]
                self._index = 0

            def _load(self) -> bool:
                if self._index >= len(self._rows):
                    return False
                row = self._rows[self._index]
                self.lines.datetime[0] = bt.date2num(row.date)
                self.lines.open[0] = float(row.open)
                self.lines.high[0] = float(row.high)
                self.lines.low[0] = float(row.low)
                self.lines.close[0] = float(row.close)
                self.lines.volume[0] = float(row.volume)
                self._index += 1
                return True

        class TargetWeightStrategy(bt.Strategy):  # type: ignore[misc]
            def __init__(self) -> None:
                self.trades_out: list[NormalizedTrade] = []
                self.curve: list[dict[str, str]] = []
                self.order_meta: dict[int, dict[str, object]] = {}

            def _submit(self, signal: StrategySignal) -> None:
                next_open = next_open_by_decision.get(signal.decision_date)
                if next_open is None or signal.target_weight is None:
                    return
                target_notional = initial_cash * signal.target_weight
                current_notional = Decimal(str(self.position.size)) * next_open
                is_buy = target_notional > current_notional
                slip_direction = Decimal("1") if is_buy else Decimal("-1")
                slipped = next_open * (Decimal("1") + slip_direction * slippage_bps / Decimal("10000"))
                entry_cost_factor = Decimal("1") + commission_bps / Decimal("10000") if is_buy else Decimal("1")
                target = (target_notional / (slipped * entry_cost_factor)).quantize(Decimal("1"), rounding=ROUND_DOWN)
                delta = target - Decimal(str(self.position.size))
                if delta == 0:
                    self.trades_out.append(NormalizedTrade(
                        signal.decision_date.isoformat(), signal.earliest_execution_date.isoformat(),
                        signal.action, Decimal("0"), next_open, slipped, Decimal("0"),
                        f"未成交：目标数量未变化；{signal.explanation}",
                    ))
                    return
                order = self.order_target_size(target=float(target))
                if order is None:
                    self.trades_out.append(NormalizedTrade(
                        signal.decision_date.isoformat(), signal.earliest_execution_date.isoformat(),
                        signal.action, Decimal("0"), next_open, slipped, Decimal("0"),
                        f"订单被拒绝：无法创建订单；{signal.explanation}",
                    ))
                    return
                self.order_meta[order.ref] = {
                    "decision": signal.decision_date, "execution": signal.earliest_execution_date,
                    "action": signal.action, "raw": next_open, "reason": signal.explanation,
                }

            def next(self) -> None:
                current = bt.num2date(self.data.datetime[0]).date()
                if current > last_bar.date:
                    if self.curve:
                        self.curve[-1] = _equity_row(
                            last_bar.date, _broker_decimal(self.broker.getcash()),
                            Decimal(str(self.position.size)), last_bar.close,
                            _broker_decimal(self.broker.getvalue()), Decimal("0"),
                        )
                    return
                for signal in signal_groups.get(current, ()):
                    self._submit(signal)
                self.curve.append(_equity_row(
                    current, _broker_decimal(self.broker.getcash()),
                    Decimal(str(self.position.size)), Decimal(str(self.data.close[0])),
                    _broker_decimal(self.broker.getvalue()), Decimal("0"),
                ))
                if current == last_bar.date and self.position.size:
                    order = self.order_target_size(target=0)
                    if order is not None:
                        self.order_meta[order.ref] = {
                            "decision": last_bar.date, "execution": last_bar.date,
                            "action": "EXIT", "raw": last_bar.close, "reason": "回测期末平仓",
                        }

            def notify_order(self, order: object) -> None:
                meta = self.order_meta.get(order.ref)  # type: ignore[attr-defined]
                if meta is None or order.status in (order.Submitted, order.Accepted):  # type: ignore[attr-defined]
                    return
                if order.status == order.Completed:  # type: ignore[attr-defined]
                    quantity = _broker_decimal(order.executed.size)  # type: ignore[attr-defined]
                    price = _broker_decimal(order.executed.price)  # type: ignore[attr-defined]
                    fees = abs(_broker_decimal(order.executed.comm))  # type: ignore[attr-defined]
                    reason = str(meta["reason"])
                else:
                    quantity, fees = Decimal("0"), Decimal("0")
                    price = Decimal(str(meta["raw"]))
                    status_zh = {
                        "Canceled": "已取消", "Margin": "保证金不足",
                        "Rejected": "被拒绝", "Expired": "已过期",
                    }.get(order.getstatusname(), "未完成")  # type: ignore[attr-defined]
                    reason = f"订单被拒绝：{status_zh}；{meta['reason']}"
                self.trades_out.append(NormalizedTrade(
                    str(meta["decision"]), str(meta["execution"]), str(meta["action"]),
                    quantity, Decimal(str(meta["raw"])), price, fees, reason,
                ))
                self.order_meta.pop(order.ref, None)  # type: ignore[attr-defined]

        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(float(initial_cash))
        cerebro.broker.setcommission(commission=float(commission_bps / Decimal("10000")))
        cerebro.broker.set_slippage_perc(
            float(slippage_bps / Decimal("10000")), slip_open=True,
            slip_match=True, slip_out=True,
        )
        cerebro.adddata(BarFeed())
        cerebro.addstrategy(TargetWeightStrategy)
        strategies = cerebro.run()
        if not strategies:
            raise ValueError("Backtrader 未返回策略执行结果")
        strategy = strategies[0]
        curve, max_drawdown = _recalculate_drawdowns(strategy.curve)
        return _execution_result(
            strategy.trades_out, curve, initial_cash, initial_cash,
            ordered_bars[0].date, ordered_bars[-1].date, max_drawdown,
        )


class StandardBacktestService:
    def __init__(self, *, price_provider: DailyKlineProvider) -> None:
        self.price_provider = price_provider

    def run(self, request: StandardBacktestRequest) -> StandardBacktestResult:
        market, symbol = request.market.strip().upper(), request.symbol.strip().upper()
        benchmark_symbol = BENCHMARK_SYMBOLS[market]
        symbol_prices = ensure_resolved_backtest_price_range(
            data_dir=request.data_dir, market=market, symbol=symbol,
            preset=request.range_preset, custom_start=request.custom_start,
            custom_end=request.custom_end, provider=self.price_provider,
        )
        benchmark_prices = None
        benchmark_error = None
        try:
            benchmark_prices = ensure_resolved_backtest_price_range(
                data_dir=request.data_dir, market=market, symbol=benchmark_symbol,
                preset=request.range_preset, custom_start=request.custom_start,
                custom_end=request.custom_end, provider=self.price_provider,
            )
        except ValueError as exc:
            if not any(fragment in str(exc) for fragment in (
                "没有返回日线数据", "请求区间内没有可用价格数据", "价格文件没有数据行",
            )):
                raise
            benchmark_error = "基准行情缺失，无法比较"
        requested_start = symbol_prices.price_range.requested_start
        requested_end = symbol_prices.price_range.requested_end
        symbol_map = {bar.date: bar for bar in symbol_prices.price_range.bars if requested_start <= bar.date <= requested_end}
        benchmark_map = ({bar.date: bar for bar in benchmark_prices.price_range.bars if requested_start <= bar.date <= requested_end}
                         if benchmark_prices is not None else {})
        common_dates = sorted(symbol_map.keys() & benchmark_map.keys()) if benchmark_map else sorted(symbol_map)
        if benchmark_map and len(common_dates) < 2:
            benchmark_error = "基准行情缺失，无法比较"
            benchmark_map = {}
            common_dates = sorted(symbol_map)
        if len(common_dates) < 2:
            raise ValueError("策略标的没有足够的交易日")
        actual_start, actual_end = common_dates[0], common_dates[-1]
        symbol_bars = tuple(symbol_map[day] for day in common_dates)
        benchmark_bars = tuple(benchmark_map[day] for day in common_dates) if benchmark_map else ()
        all_symbol_bars = tuple(bar for bar in symbol_prices.price_range.bars if bar.date <= actual_end)
        if request.strategy_id == "breakout_momentum/v1" and not any(
            bar.volume > 0 for bar in all_symbol_bars
        ):
            raise ValueError("突破动量策略需要有效的非零成交量数据")
        generated = generate_strategy_signals(
            request.strategy_id, all_symbol_bars, start_date=actual_start,
            max_strategy_weight=request.max_strategy_weight,
        )
        signals = [signal for signal in generated if signal.decision_date in common_dates and (
            signal.action == "HOLD" or signal.earliest_execution_date in symbol_map
        )]
        # Rebind execution to the next common trading date so all comparisons use the same calendar.
        next_common = {day: common_dates[index + 1] for index, day in enumerate(common_dates[:-1])}
        signals = [signal if signal.action == "HOLD" else replace(
            signal, earliest_execution_date=next_common.get(signal.decision_date)
        ) for signal in signals if signal.action == "HOLD" or signal.decision_date in next_common]
        adapter = BacktraderTargetWeightAdapter()
        allocated = request.initial_cash * request.max_strategy_weight
        strategy = replace(adapter.run(
            bars=symbol_bars, signals=signals, initial_cash=request.initial_cash,
            commission_bps=request.commission_bps, slippage_bps=request.slippage_bps,
        ), initial_allocated_notional=allocated)
        buy_hold = _run_buy_hold(symbol_bars, request.initial_cash, allocated, request.commission_bps, request.slippage_bps)
        market_benchmark = (_run_buy_hold(benchmark_bars, request.initial_cash, allocated, request.commission_bps, request.slippage_bps)
                            if benchmark_bars else None)
        market_excess = strategy.total_return_pct - market_benchmark.total_return_pct if market_benchmark else None
        request_hash = hashlib.sha256(json.dumps(_json_safe(request), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
        run_id = _build_run_id(market, symbol, request.strategy_id, request_hash)
        message = "所选区间内没有触发交易" if not any(trade.quantity for trade in strategy.trades) else "标准策略回测完成"
        metrics = {
            "strategy": _json_safe(strategy), "buy_hold": _json_safe(buy_hold),
            "market_benchmark": _json_safe(market_benchmark),
            "strategy_excess_return_pct": str(strategy.total_return_pct - buy_hold.total_return_pct),
            "market_excess_return_pct": str(market_excess) if market_excess is not None else None,
            "market_benchmark_error": benchmark_error,
        }
        manifest_base = {
            "schema_version": MANIFEST_SCHEMA_VERSION, "run_id": run_id, "request_hash": request_hash,
            "strategy": next(item.to_dict() for item in strategy_catalog() if item.strategy_id == request.strategy_id),
            "adapter": {"name": adapter.name, "version": adapter.version},
            "requested_range": {"start": requested_start.isoformat(), "end": requested_end.isoformat()},
            "actual_range": {"start": actual_start.isoformat(), "end": actual_end.isoformat()},
            "capital": str(request.initial_cash), "initial_allocated_notional": str(allocated),
            "max_strategy_weight": str(request.max_strategy_weight),
            "costs_bps": {"commission": str(request.commission_bps), "slippage": str(request.slippage_bps)},
            "sources": {
                "symbol": {"market": market, "symbol": symbol, "sha256": symbol_prices.price_range.source_hash},
                "benchmark": ({"market": market, "symbol": benchmark_symbol, "sha256": benchmark_prices.price_range.source_hash}
                              if benchmark_prices is not None and benchmark_map else
                              {"market": market, "symbol": benchmark_symbol, "error": benchmark_error}),
            },
        }
        final_dir, report_path = _publish_run(
            request, run_id, manifest_base, signals, strategy, buy_hold, market_benchmark,
            _render_report(run_id, request.strategy_id, benchmark_symbol, message, metrics), metrics,
        )
        definition = next(item.to_dict() for item in strategy_catalog() if item.strategy_id == request.strategy_id)
        return StandardBacktestResult(
            run_id=run_id, status="ok", message_zh=message, strategy_id=request.strategy_id,
            benchmark_symbol=benchmark_symbol, requested_start=requested_start, requested_end=requested_end,
            actual_start=actual_start, actual_end=actual_end, strategy=strategy, buy_hold=buy_hold,
            market_benchmark=market_benchmark,
            strategy_excess_return_pct=strategy.total_return_pct - buy_hold.total_return_pct,
            market_excess_return_pct=market_excess, market_benchmark_error=benchmark_error,
            assumptions={"initial_cash": str(request.initial_cash), "max_strategy_weight": str(request.max_strategy_weight),
                         "commission_bps": str(request.commission_bps), "slippage_bps": str(request.slippage_bps)},
            strategy_definition=definition, signals=_signal_rows(signals), adapter_version=adapter.version,
            manifest_path=final_dir / "manifest.json", signals_path=final_dir / "signals.csv",
            trades_path=final_dir / "trades.csv", equity_curve_path=final_dir / "equity_curve.csv",
            buy_hold_equity_path=final_dir / "buy_hold_equity.csv",
            market_benchmark_equity_path=final_dir / "market_benchmark_equity.csv" if market_benchmark else None,
            metrics_path=final_dir / "metrics.json", report_path=report_path,
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
    return _json_safe(result)  # type: ignore[return-value]


def _group_signals(signals: Sequence[StrategySignal]) -> dict[date, tuple[StrategySignal, ...]]:
    grouped: dict[date, list[StrategySignal]] = {}
    by_execution: dict[date, list[StrategySignal]] = {}
    for signal in signals:
        if signal.action == "HOLD" or signal.earliest_execution_date is None:
            continue
        grouped.setdefault(signal.decision_date, []).append(signal)
        by_execution.setdefault(signal.earliest_execution_date, []).append(signal)
    for items in by_execution.values():
        actions = {(item.action, item.target_weight) for item in items}
        if len(items) > 1 and len(actions) > 1:
            raise ValueError("同一执行日存在冲突策略信号")
    return {key: tuple(items) for key, items in grouped.items()}


def _run_buy_hold(bars: Sequence[StrategyBar], initial_cash: Decimal, allocated: Decimal,
                  commission_bps: Decimal, slippage_bps: Decimal) -> ExecutionResult:
    entry_bar, exit_bar = bars[1], bars[-1]
    entry = entry_bar.open * (Decimal("1") + slippage_bps / Decimal("10000"))
    quantity = (allocated / (entry * (Decimal("1") + commission_bps / Decimal("10000")))).quantize(Decimal("1"), rounding=ROUND_DOWN)
    entry_fee = quantity * entry * commission_bps / Decimal("10000")
    cash = initial_cash - quantity * entry - entry_fee
    exit_price = exit_bar.close * (Decimal("1") - slippage_bps / Decimal("10000"))
    exit_fee = quantity * exit_price * commission_bps / Decimal("10000")
    trades = [
        NormalizedTrade(bars[0].date.isoformat(), entry_bar.date.isoformat(), "BUY", quantity, entry_bar.open, entry, entry_fee, "基准首个可执行开盘买入"),
        NormalizedTrade(exit_bar.date.isoformat(), exit_bar.date.isoformat(), "EXIT", -quantity, exit_bar.close, exit_price, exit_fee, "回测期末平仓"),
    ]
    curve: list[dict[str, str]] = []
    for bar in bars:
        held = quantity if entry_bar.date <= bar.date < exit_bar.date else Decimal("0")
        row_cash = cash if held else initial_cash if bar.date < entry_bar.date else cash + quantity * exit_price - exit_fee
        equity = row_cash + held * bar.close
        curve.append(_equity_row(bar.date, row_cash, held, bar.close, equity, Decimal("0")))
    curve, max_drawdown = _recalculate_drawdowns(curve)
    return _execution_result(trades, curve, initial_cash, allocated, bars[0].date, bars[-1].date, max_drawdown)


def _execution_result(trades: Sequence[NormalizedTrade], curve: Sequence[dict[str, str]],
                      initial_cash: Decimal, allocated: Decimal, actual_start: date,
                      actual_end: date, max_drawdown: Decimal) -> ExecutionResult:
    final = Decimal(curve[-1]["equity"])
    total = (final / initial_cash - Decimal("1")) * Decimal("100")
    days = max(1, (actual_end - actual_start).days)
    annualized = ((final / initial_cash) ** (Decimal("365") / Decimal(days)) - Decimal("1")) * Decimal("100") if final > 0 else Decimal("-100")
    return ExecutionResult(tuple(trades), tuple(curve), final, total, annualized, abs(max_drawdown),
                           _realized_win_rate(trades), actual_start, actual_end, initial_cash, allocated)


def _realized_win_rate(trades: Sequence[NormalizedTrade]) -> Decimal:
    quantity = Decimal("0")
    cost = Decimal("0")
    outcomes: list[Decimal] = []
    for trade in trades:
        if trade.quantity > 0:
            quantity += trade.quantity
            cost += trade.quantity * trade.execution_price + trade.fees
        elif trade.quantity < 0 and quantity > 0:
            sold = min(-trade.quantity, quantity)
            allocated_cost = cost * sold / quantity
            outcomes.append(sold * trade.execution_price - trade.fees - allocated_cost)
            cost -= allocated_cost
            quantity -= sold
    if not outcomes:
        return Decimal("0")
    return Decimal("100") * Decimal(sum(value > 0 for value in outcomes)) / Decimal(len(outcomes))


def _recalculate_drawdowns(rows: Sequence[dict[str, str]]) -> tuple[list[dict[str, str]], Decimal]:
    peak = Decimal("0")
    maximum = Decimal("0")
    output: list[dict[str, str]] = []
    for row in rows:
        equity = Decimal(row["equity"])
        peak = max(peak, equity)
        drawdown = (equity / peak - Decimal("1")) * Decimal("100") if peak else Decimal("0")
        maximum = min(maximum, drawdown)
        output.append({**row, "drawdown_pct": str(drawdown)})
    return output, maximum


def _broker_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _equity_row(day: date, cash: Decimal, quantity: Decimal, close: Decimal,
                equity: Decimal, drawdown: Decimal) -> dict[str, str]:
    return {"date": day.isoformat(), "cash": str(cash), "position_quantity": str(quantity),
            "close": str(close), "equity": str(equity), "drawdown_pct": str(drawdown)}


def _build_run_id(market: str, symbol: str, strategy_id: str, request_hash: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{market}-{symbol}-{strategy_id.replace('/', '-')}-{request_hash}"


def _publish_run(request: StandardBacktestRequest, run_id: str, manifest_base: dict[str, object],
                 signals: Sequence[StrategySignal], strategy: ExecutionResult,
                 buy_hold: ExecutionResult, market_benchmark: ExecutionResult | None,
                 report: str, metrics: dict[str, object]) -> tuple[Path, Path]:
    parent = request.data_dir / "backtests"
    final_dir = parent / run_id
    report_path = request.reports_dir / "backtests" / f"{run_id}.md"
    if final_dir.exists() or report_path.exists():
        raise ValueError("回测运行编号已存在，拒绝覆盖")
    parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{run_id}.lock"
    staging = parent / f".{run_id}.tmp-{uuid4().hex}"
    report_temp: Path | None = None
    published = False
    lock_fd: int | None = None
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError("回测运行编号已存在，拒绝覆盖") from exc
        staging.mkdir()
        artifacts: dict[str, tuple[str, object, Sequence[str] | None]] = {
            "signals.csv": ("csv", _signal_rows(signals), ("decision_date", "earliest_execution_date", "action", "target_weight", "rule", "explanation", "data_cutoff")),
            "trades.csv": ("csv", [_json_safe(trade) for trade in strategy.trades], tuple(field.name for field in fields(NormalizedTrade))),
            "equity_curve.csv": ("csv", list(strategy.equity_curve), EQUITY_FIELDS),
            "buy_hold_equity.csv": ("csv", list(buy_hold.equity_curve), EQUITY_FIELDS),
            "metrics.json": ("json", metrics, None),
            "report.md": ("text", report, None),
        }
        if market_benchmark is not None:
            artifacts["market_benchmark_equity.csv"] = ("csv", list(market_benchmark.equity_curve), EQUITY_FIELDS)
        for filename, (kind, payload, fieldnames) in artifacts.items():
            _write_run_artifact(staging / filename, kind, payload, fieldnames)
        hashes = {filename: {"sha256": hashlib.sha256((staging / filename).read_bytes()).hexdigest()}
                  for filename in artifacts}
        _write_run_artifact(staging / "manifest.json", "json", {**manifest_base, "artifacts": hashes}, None)
        with NamedTemporaryFile("w", encoding="utf-8", dir=report_path.parent, delete=False) as handle:
            report_temp = Path(handle.name)
            handle.write(report)
        if final_dir.exists() or report_path.exists():
            raise ValueError("回测运行编号已存在，拒绝覆盖")
        staging.replace(final_dir)
        published = True
        report_temp.replace(report_path)
        return final_dir, report_path
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"回测产物写入失败：{exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if report_temp is not None and report_temp.exists():
            report_temp.unlink()
        if published and not report_path.exists() and final_dir.exists():
            shutil.rmtree(final_dir)
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)


def _write_run_artifact(path: Path, kind: str, payload: object, fieldnames: Sequence[str] | None) -> None:
    if kind == "json":
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    if kind == "text":
        path.write_text(str(payload), encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ())
        writer.writeheader()
        writer.writerows(payload)  # type: ignore[arg-type]


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


def _render_report(run_id: str, strategy_id: str, benchmark: str, message: str, metrics: dict[str, object]) -> str:
    return (f"# 标准策略回测报告\n\n- 运行编号：{run_id}\n- 策略：{strategy_id}\n"
            f"- 市场基准：{benchmark}\n- 状态：{message}\n\n## 核心指标\n\n"
            f"```json\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n```\n")
