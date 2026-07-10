from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .market_scope import parse_market_scope
from .trading_plan import TradingPlanRow, load_trading_plan_rows


BACKTEST_METRICS_SCHEMA_VERSION = "open_trader.backtest.metrics.v1"

PRICE_FIELDNAMES = ("date", "open", "high", "low", "close")
TRADE_FIELDNAMES = (
    "run_id",
    "run_date",
    "date",
    "market",
    "symbol",
    "side",
    "price",
    "quantity",
    "notional",
    "fees",
    "cash_after",
    "reason",
)
EQUITY_FIELDNAMES = (
    "run_id",
    "date",
    "cash",
    "position_quantity",
    "close",
    "equity",
    "drawdown_pct",
)


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    run_date: str
    market: str
    symbol: str
    adapter: str
    trade_count: int
    final_equity: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    metrics_path: Path
    trades_path: Path
    equity_curve_path: Path
    report_path: Path


@dataclass(frozen=True)
class PriceBar:
    date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class BacktestExecution:
    run_id: str
    run_date: str
    date: str
    market: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    fees: Decimal
    cash_after: Decimal
    reason: str


class TradingPlanBacktestAdapter(Protocol):
    name: str

    def run(
        self,
        *,
        run_id: str,
        plan: TradingPlanRow,
        effective_run_date: str,
        prices_path: Path,
        initial_cash: Decimal,
        commission_bps: Decimal,
        slippage_bps: Decimal,
    ) -> tuple[list[BacktestExecution], list[dict[str, str]]]:
        ...


class SimpleTradingPlanAdapter:
    name = "simple"

    def run(
        self,
        *,
        run_id: str,
        plan: TradingPlanRow,
        effective_run_date: str,
        prices_path: Path,
        initial_cash: Decimal,
        commission_bps: Decimal,
        slippage_bps: Decimal,
    ) -> tuple[list[BacktestExecution], list[dict[str, str]]]:
        bars = _read_price_bars(prices_path)
        if not bars:
            raise ValueError("price CSV has no rows")
        return _simulate_trading_plan(
            run_id=run_id,
            plan=plan,
            effective_run_date=effective_run_date,
            bars=bars,
            initial_cash=initial_cash,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )


class BacktraderTradingPlanAdapter:
    name = "backtrader"

    def run(
        self,
        *,
        run_id: str,
        plan: TradingPlanRow,
        effective_run_date: str,
        prices_path: Path,
        initial_cash: Decimal,
        commission_bps: Decimal,
        slippage_bps: Decimal,
    ) -> tuple[list[BacktestExecution], list[dict[str, str]]]:
        try:
            import backtrader as bt
        except ImportError as exc:
            raise ValueError("backtrader adapter requires the backtrader package") from exc

        class TradingPlanStrategy(bt.Strategy):  # type: ignore[misc]
            params = (
                ("run_id", run_id),
                ("plan", plan),
                ("effective_run_date", effective_run_date),
                ("initial_cash", initial_cash),
                ("commission_bps", commission_bps),
                ("slippage_bps", slippage_bps),
            )

            def __init__(self) -> None:
                self.bars: list[PriceBar] = []
                self.trades: list[BacktestExecution] = []
                self.equity_rows: list[dict[str, str]] = []

            def next(self) -> None:
                self.bars.append(
                    PriceBar(
                        date=bt.num2date(self.data.datetime[0]).date().isoformat(),
                        open=Decimal(str(self.data.open[0])),
                        high=Decimal(str(self.data.high[0])),
                        low=Decimal(str(self.data.low[0])),
                        close=Decimal(str(self.data.close[0])),
                    )
                )

            def stop(self) -> None:
                self.trades, self.equity_rows = _simulate_trading_plan(
                    run_id=self.p.run_id,
                    plan=self.p.plan,
                    effective_run_date=self.p.effective_run_date,
                    bars=self.bars,
                    initial_cash=self.p.initial_cash,
                    commission_bps=self.p.commission_bps,
                    slippage_bps=self.p.slippage_bps,
                )

        cerebro = bt.Cerebro(stdstats=False)
        data = bt.feeds.GenericCSVData(
            dataname=str(prices_path),
            dtformat="%Y-%m-%d",
            datetime=0,
            open=1,
            high=2,
            low=3,
            close=4,
            volume=-1,
            openinterest=-1,
        )
        cerebro.adddata(data)
        cerebro.addstrategy(TradingPlanStrategy)
        strategies = cerebro.run()
        if not strategies:
            raise ValueError("backtrader adapter produced no strategy result")
        strategy = strategies[0]
        if not strategy.bars:
            raise ValueError("price CSV has no rows")
        return strategy.trades, strategy.equity_rows


def run_backtest(
    *,
    plan_path: Path,
    prices_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    symbol: str,
    market: str,
    initial_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
    adapter: str = "backtrader",
) -> BacktestResult:
    market_scope = parse_market_scope(market)
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    _validate_positive_decimal("initial_cash", initial_cash)
    _validate_non_negative_decimal("commission_bps", commission_bps)
    _validate_non_negative_decimal("slippage_bps", slippage_bps)

    plan = _select_plan(
        load_trading_plan_rows(plan_path),
        run_date=run_date,
        market=market_scope.value,
        symbol=normalized_symbol,
    )
    adapter_impl = _backtest_adapter(adapter)
    adapter_name = adapter_impl.name

    run_id = f"{run_date}-{market_scope.value}-{normalized_symbol}-trading-plan"
    trades, equity_rows = adapter_impl.run(
        run_id=run_id,
        plan=plan,
        effective_run_date=run_date,
        prices_path=prices_path,
        initial_cash=initial_cash,
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
    )
    metrics = _build_metrics(
        run_id=run_id,
        run_date=run_date,
        market=market_scope.value,
        symbol=normalized_symbol,
        adapter=adapter_name,
        initial_cash=initial_cash,
        trades=trades,
        equity_rows=equity_rows,
    )

    output_dir = data_dir / "backtests" / run_id
    trades_path = output_dir / "trades.csv"
    equity_curve_path = output_dir / "equity_curve.csv"
    metrics_path = output_dir / "metrics.json"
    report_path = reports_dir / "backtests" / f"{run_id}.md"
    _atomic_write_csv(trades_path, TRADE_FIELDNAMES, [_trade_row(trade) for trade in trades])
    _atomic_write_csv(equity_curve_path, EQUITY_FIELDNAMES, equity_rows)
    _atomic_write_json(metrics_path, metrics)
    _atomic_write_text(report_path, _render_report(metrics, trades))

    return BacktestResult(
        run_id=run_id,
        run_date=run_date,
        market=market_scope.value,
        symbol=normalized_symbol,
        adapter=adapter_name,
        trade_count=len(trades),
        final_equity=Decimal(metrics["final_equity"]),
        total_return_pct=Decimal(metrics["total_return_pct"]),
        max_drawdown_pct=Decimal(metrics["max_drawdown_pct"]),
        metrics_path=metrics_path,
        trades_path=trades_path,
        equity_curve_path=equity_curve_path,
        report_path=report_path,
    )


def _backtest_adapter(name: str) -> TradingPlanBacktestAdapter:
    normalized = name.strip().lower()
    if normalized in {"", "backtrader"}:
        return BacktraderTradingPlanAdapter()
    if normalized in {"simple", "local"}:
        return SimpleTradingPlanAdapter()
    raise ValueError(f"unsupported backtest adapter: {name}")


def _select_plan(
    plans: list[TradingPlanRow],
    *,
    run_date: str,
    market: str,
    symbol: str,
) -> TradingPlanRow:
    matches = [
        plan
        for plan in plans
        if plan.status == "active"
        and plan.market.upper() == market
        and plan.symbol.upper() == symbol
        and (not plan.run_date.strip() or plan.run_date == run_date)
    ]
    if not matches:
        raise ValueError(
            f"no active trading plan matches run_date {run_date} for {market}.{symbol}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"multiple active trading plans match run_date {run_date} for {market}.{symbol}"
        )
    plan = matches[0]
    if plan.entry_zone_high is None:
        raise ValueError("trading plan entry_zone_high is required for backtest")
    if not plan.max_weight.strip():
        raise ValueError("trading plan max_weight is required for backtest")
    return plan


def _read_price_bars(prices_path: Path) -> list[PriceBar]:
    with prices_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = sorted(set(PRICE_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing price column(s): {', '.join(missing)}")
        rows = [row for row in reader if row]
    bars = [
        PriceBar(
            date=_validated_date(row.get("date", "") or ""),
            open=_required_decimal(row, "open"),
            high=_required_decimal(row, "high"),
            low=_required_decimal(row, "low"),
            close=_required_decimal(row, "close"),
        )
        for row in rows
    ]
    return sorted(bars, key=lambda bar: bar.date)


def _simulate_trading_plan(
    *,
    run_id: str,
    plan: TradingPlanRow,
    effective_run_date: str,
    bars: list[PriceBar],
    initial_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> tuple[list[BacktestExecution], list[dict[str, str]]]:
    cash = initial_cash
    quantity = Decimal("0")
    trades: list[BacktestExecution] = []
    equity_rows: list[dict[str, str]] = []
    peak_equity = initial_cash
    max_weight = _parse_percent(plan.max_weight)

    for bar in bars:
        if bar.date > effective_run_date:
            if quantity == 0 and _entry_touched(plan, bar):
                entry_price = _buy_price(plan.entry_zone_high or bar.close, slippage_bps)
                quantity = _buy_quantity(
                    initial_cash=initial_cash,
                    max_weight=max_weight,
                    entry_price=entry_price,
                )
                if quantity > 0:
                    notional = entry_price * quantity
                    fees = _fee(notional, commission_bps)
                    cash -= notional + fees
                    trades.append(
                        _execution(
                            run_id=run_id,
                            plan=plan,
                            run_date=effective_run_date,
                            date=bar.date,
                            side="BUY",
                            price=entry_price,
                            quantity=quantity,
                            notional=notional,
                            fees=fees,
                            cash_after=cash,
                            reason="entry_zone",
                        )
                    )
            elif quantity > 0:
                exit_reason, raw_exit_price = _exit_signal(plan, bar)
                if raw_exit_price is not None:
                    exit_price = _sell_price(raw_exit_price, slippage_bps)
                    notional = exit_price * quantity
                    fees = _fee(notional, commission_bps)
                    cash += notional - fees
                    trades.append(
                        _execution(
                            run_id=run_id,
                            plan=plan,
                            run_date=effective_run_date,
                            date=bar.date,
                            side="SELL",
                            price=exit_price,
                            quantity=quantity,
                            notional=notional,
                            fees=fees,
                            cash_after=cash,
                            reason=exit_reason,
                        )
                    )
                    quantity = Decimal("0")

        equity = cash + (quantity * bar.close)
        if equity > peak_equity:
            peak_equity = equity
        drawdown_pct = (
            Decimal("0")
            if peak_equity == 0
            else ((peak_equity - equity) / peak_equity * Decimal("100"))
        )
        equity_rows.append(
            {
                "run_id": run_id,
                "date": bar.date,
                "cash": _money(cash),
                "position_quantity": _quantity(quantity),
                "close": _price(bar.close),
                "equity": _money(equity),
                "drawdown_pct": _percent(drawdown_pct),
            }
        )

    if quantity > 0 and bars:
        final_bar = bars[-1]
        exit_price = _sell_price(final_bar.close, slippage_bps)
        notional = exit_price * quantity
        fees = _fee(notional, commission_bps)
        cash += notional - fees
        trades.append(
            _execution(
                run_id=run_id,
                plan=plan,
                run_date=effective_run_date,
                date=final_bar.date,
                side="SELL",
                price=exit_price,
                quantity=quantity,
                notional=notional,
                fees=fees,
                cash_after=cash,
                reason="end_of_backtest",
            )
        )
        quantity = Decimal("0")
        peak_equity = max(peak_equity, cash)
        drawdown_pct = (
            Decimal("0")
            if peak_equity == 0
            else ((peak_equity - cash) / peak_equity * Decimal("100"))
        )
        equity_rows[-1] = {
            "run_id": run_id,
            "date": final_bar.date,
            "cash": _money(cash),
            "position_quantity": _quantity(quantity),
            "close": _price(final_bar.close),
            "equity": _money(cash),
            "drawdown_pct": _percent(drawdown_pct),
        }

    return trades, equity_rows


def _entry_touched(plan: TradingPlanRow, bar: PriceBar) -> bool:
    if plan.entry_zone_low is None or plan.entry_zone_high is None:
        return False
    return bar.low <= plan.entry_zone_high and bar.high >= plan.entry_zone_low


def _exit_signal(plan: TradingPlanRow, bar: PriceBar) -> tuple[str, Decimal | None]:
    if plan.stop_loss is not None and bar.low <= plan.stop_loss:
        return "stop_loss", plan.stop_loss
    if plan.target_2 is not None and bar.high >= plan.target_2:
        return "target_2", plan.target_2
    if plan.target_1 is not None and bar.high >= plan.target_1:
        return "target_1", plan.target_1
    return "", None


def _buy_quantity(
    *,
    initial_cash: Decimal,
    max_weight: Decimal,
    entry_price: Decimal,
) -> Decimal:
    max_notional = initial_cash * max_weight
    if entry_price <= 0:
        return Decimal("0")
    return (max_notional / entry_price).to_integral_value(rounding=ROUND_DOWN)


def _buy_price(price: Decimal, slippage_bps: Decimal) -> Decimal:
    return price * (Decimal("1") + slippage_bps / Decimal("10000"))


def _sell_price(price: Decimal, slippage_bps: Decimal) -> Decimal:
    return price * (Decimal("1") - slippage_bps / Decimal("10000"))


def _fee(notional: Decimal, commission_bps: Decimal) -> Decimal:
    return notional * commission_bps / Decimal("10000")


def _execution(
    *,
    run_id: str,
    plan: TradingPlanRow,
    run_date: str,
    date: str,
    side: str,
    price: Decimal,
    quantity: Decimal,
    notional: Decimal,
    fees: Decimal,
    cash_after: Decimal,
    reason: str,
) -> BacktestExecution:
    return BacktestExecution(
        run_id=run_id,
        run_date=run_date,
        date=date,
        market=plan.market.upper(),
        symbol=plan.symbol.upper(),
        side=side,
        price=price,
        quantity=quantity,
        notional=notional,
        fees=fees,
        cash_after=cash_after,
        reason=reason,
    )


def _build_metrics(
    *,
    run_id: str,
    run_date: str,
    market: str,
    symbol: str,
    adapter: str,
    initial_cash: Decimal,
    trades: list[BacktestExecution],
    equity_rows: list[dict[str, str]],
) -> dict[str, object]:
    final_equity = (
        Decimal(equity_rows[-1]["equity"]) if equity_rows else initial_cash
    )
    total_return_pct = (
        Decimal("0")
        if initial_cash == 0
        else ((final_equity - initial_cash) / initial_cash * Decimal("100"))
    )
    max_drawdown_pct = max(
        [Decimal(row["drawdown_pct"]) for row in equity_rows] or [Decimal("0")]
    )
    round_trips = sum(1 for trade in trades if trade.side == "SELL")
    wins = _winning_round_trips(trades)
    win_rate_pct = (
        Decimal("0")
        if round_trips == 0
        else Decimal(wins) / Decimal(round_trips) * Decimal("100")
    )
    return {
        "schema_version": BACKTEST_METRICS_SCHEMA_VERSION,
        "run_id": run_id,
        "run_date": run_date,
        "market": market,
        "symbol": symbol,
        "strategy": "trading_plan",
        "adapter": adapter,
        "initial_cash": _money(initial_cash),
        "final_equity": _money(final_equity),
        "total_return_pct": _percent(total_return_pct),
        "max_drawdown_pct": _percent(max_drawdown_pct),
        "trade_count": len(trades),
        "round_trips": round_trips,
        "win_rate_pct": _percent(win_rate_pct),
    }


def _winning_round_trips(trades: list[BacktestExecution]) -> int:
    wins = 0
    current_buy: BacktestExecution | None = None
    for trade in trades:
        if trade.side == "BUY":
            current_buy = trade
            continue
        if trade.side == "SELL" and current_buy is not None:
            if trade.notional - trade.fees > current_buy.notional + current_buy.fees:
                wins += 1
            current_buy = None
    return wins


def _trade_row(trade: BacktestExecution) -> dict[str, str]:
    return {
        "run_id": trade.run_id,
        "run_date": trade.run_date,
        "date": trade.date,
        "market": trade.market,
        "symbol": trade.symbol,
        "side": trade.side,
        "price": _price(trade.price),
        "quantity": _quantity(trade.quantity),
        "notional": _money(trade.notional),
        "fees": _money(trade.fees),
        "cash_after": _money(trade.cash_after),
        "reason": trade.reason,
    }


def _render_report(
    metrics: dict[str, object],
    trades: list[BacktestExecution],
) -> str:
    lines = [
        f"# Backtest - {metrics['run_id']}",
        "",
        f"标的：{metrics['market']}.{metrics['symbol']}",
        "策略：trading_plan",
        f"执行后端：{metrics['adapter']}",
        f"初始资金：{metrics['initial_cash']}",
        f"最终权益：{metrics['final_equity']}",
        f"总收益率：{metrics['total_return_pct']}%",
        f"最大回撤：{metrics['max_drawdown_pct']}%",
        f"交易次数：{metrics['trade_count']}",
        f"胜率：{metrics['win_rate_pct']}%",
        "",
        "## 交易明细",
    ]
    if not trades:
        lines.append("无交易。")
    for trade in trades:
        lines.append(
            " - "
            f"{trade.date} {trade.side} "
            f"{_quantity(trade.quantity)} @ {_price(trade.price)} "
            f"原因：{trade.reason}"
        )
    return "\n".join(lines) + "\n"


def _atomic_write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(text)
    temp_path.replace(path)


def _required_decimal(row: dict[str, str], fieldname: str) -> Decimal:
    value = (row.get(fieldname) or "").strip()
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid price {fieldname}: {value}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"invalid price {fieldname}: {value}")
    return parsed


def _parse_percent(value: str) -> Decimal:
    raw = value.strip()
    if not raw.endswith("%"):
        raise ValueError(f"invalid max_weight: {value}")
    try:
        parsed = Decimal(raw[:-1])
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid max_weight: {value}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"invalid max_weight: {value}")
    return parsed / Decimal("100")


def _validated_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid price date: {value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid price date: {value}")
    return value


def _validate_positive_decimal(name: str, value: Decimal) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive")


def _validate_non_negative_decimal(name: str, value: Decimal) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be non-negative")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _price(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'))}"


def _quantity(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1'))}"


def _percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"
