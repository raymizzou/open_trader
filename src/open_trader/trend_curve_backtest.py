"""Offline Trend Animals temperature-transition backtest."""

from __future__ import annotations

import hashlib
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any

from .backtest import PriceBar, _read_price_bars


SCHEMA_VERSION = "open_trader.trend_curve_backtest.v1"
STRATEGY_ID = "trend_curve_warm_to_hot_flat_exit/US/v1"
_CALCULATION_CONTEXT = Context(prec=28)
_ENTRY_TEMPERATURE = "温"
_EXIT_TEMPERATURES = frozenset({"温", "热", "沸"})


@dataclass(frozen=True)
class TemperatureDecision:
    action: str
    reason: str


@dataclass(frozen=True)
class _CurvePoint:
    date: str
    temperature: str
    price: str
    strength: str


def decide_temperature_transition(
    previous_temperature: str | None,
    current_temperature: str,
    held: bool,
) -> TemperatureDecision:
    """Return the fixed v1 decision for one temperature transition."""
    if held:
        if previous_temperature in _EXIT_TEMPERATURES and current_temperature == "平":
            return TemperatureDecision("EXIT", "temperature_to_flat")
        return TemperatureDecision("HOLD", "holding")
    if previous_temperature == _ENTRY_TEMPERATURE and current_temperature == "热":
        return TemperatureDecision("BUY", "warm_to_hot")
    return TemperatureDecision("SKIP", "not_warm_to_hot")


def run_trend_curve_backtest(
    database: Path | str,
    ohlc_csv: Path | str,
    *,
    market: str,
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    initial_cash: Decimal | str | int,
    commission_bps: Decimal | str | int = Decimal("10"),
    slippage_bps: Decimal | str | int = Decimal("5"),
) -> dict[str, object]:
    with localcontext(_CALCULATION_CONTEXT):
        return _run_trend_curve_backtest(
            database,
            ohlc_csv,
            market=market,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
        )


def _run_trend_curve_backtest(
    database: Path | str,
    ohlc_csv: Path | str,
    *,
    market: str,
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    initial_cash: Decimal | str | int,
    commission_bps: Decimal | str | int = Decimal("10"),
    slippage_bps: Decimal | str | int = Decimal("5"),
) -> dict[str, object]:
    normalized_market = str(market).strip().upper()
    if normalized_market != "US":
        raise ValueError("market must be US")
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    requested_start = _canonical_date(start_date, "start_date")
    requested_end = _canonical_date(end_date, "end_date")
    if requested_start > requested_end:
        raise ValueError("start_date must not be after end_date")
    cash = _decimal(initial_cash, "initial_cash")
    commission = _decimal(commission_bps, "commission_bps")
    slippage = _decimal(slippage_bps, "slippage_bps")
    if cash <= 0:
        raise ValueError("initial_cash must be positive")
    if commission < 0:
        raise ValueError("commission_bps must be non-negative")
    if slippage < 0:
        raise ValueError("slippage_bps must be non-negative")
    if slippage >= Decimal("10000"):
        raise ValueError("slippage_bps must be below 10000")

    database_path = Path(database).expanduser()
    prices_path = Path(ohlc_csv).expanduser()
    database_hash = _sha256(database_path)
    prices_hash = _sha256(prices_path)
    curve_points, all_points = _load_curve_points(
        database_path,
        market=normalized_market,
        symbol=normalized_symbol,
        start_date=requested_start,
        end_date=requested_end,
    )
    bars = [
        bar
        for bar in _read_price_bars(prices_path)
        if requested_start <= bar.date <= requested_end
    ]
    if not bars:
        raise ValueError("price CSV has no rows in requested range")
    ohlc_dates = {bar.date for bar in bars}
    missing_curve_dates = [
        point.date for point in curve_points if point.date not in ohlc_dates
    ]
    if missing_curve_dates:
        raise ValueError(
            "trend-curve date has no matching OHLC row: "
            + ", ".join(missing_curve_dates)
        )

    trades, equity_curve, completed_rounds, decisions = _simulate(
        curve_points,
        all_points,
        bars,
        initial_cash=cash,
        commission_bps=commission,
        slippage_bps=slippage,
    )
    metrics = _metrics(
        equity_curve,
        completed_rounds,
        initial_cash=cash,
    )
    return {
        "schema": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "strategy_id": STRATEGY_ID,
        "market": normalized_market,
        "symbol": normalized_symbol,
        "source_hashes": {
            "trend_curve_database": database_hash,
            "ohlc_csv": prices_hash,
        },
        "ranges": {
            "requested": {"start": requested_start, "end": requested_end},
            "trend_curve": {
                "start": curve_points[0].date,
                "end": curve_points[-1].date,
            },
            "ohlc": {"start": bars[0].date, "end": bars[-1].date},
        },
        "assumptions": {
            "execution": "first OHLC bar strictly after decision date",
            "initial_cash": _decimal_text(cash),
            "commission_bps": _decimal_text(commission),
            "slippage_bps": _decimal_text(slippage),
            "whole_shares": True,
            "end_of_data_close": True,
        },
        "decisions": decisions,
        "trades": trades,
        "equity_curve": equity_curve,
        "completed_rounds": completed_rounds,
        "metrics": metrics,
        "buy_and_hold": _buy_and_hold(
            bars,
            initial_cash=cash,
            commission_bps=commission,
            slippage_bps=slippage,
        ),
    }


def _load_curve_points(
    database: Path,
    *,
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> tuple[list[_CurvePoint], list[_CurvePoint]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT curve_date, price, temperature, strength
            FROM trend_curve_points
            WHERE market = ? AND symbol = ? AND curve_date <= ?
            ORDER BY curve_date
            """,
            (market, symbol, end_date),
        ).fetchall()
    points = [
        _CurvePoint(
            date=_canonical_date(row[0], "curve_date"),
            price=_decimal_text(_decimal(row[1], "curve price")),
            temperature=str(row[2]),
            strength=_decimal_text(_decimal(row[3], "curve strength")),
        )
        for row in rows
    ]
    if not points:
        raise ValueError(f"no trend curve points for {market}.{symbol}")
    if any(point.temperature not in {"冻", "寒", "凉", "平", "温", "热", "沸"} for point in points):
        raise ValueError("trend curve temperature is invalid")
    in_range = [point for point in points if start_date <= point.date <= end_date]
    if not in_range:
        raise ValueError(f"no trend curve points for {market}.{symbol} in requested range")
    return in_range, points


def _simulate(
    points: list[_CurvePoint],
    all_points: list[_CurvePoint],
    bars: list[PriceBar],
    *,
    initial_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    bars_by_date = {bar.date: bar for bar in bars}
    bar_dates = [bar.date for bar in bars]
    points_by_date = {point.date: point for point in points}
    timeline = sorted(set(bar_dates) | set(points_by_date))
    cash = initial_cash
    quantity = Decimal("0")
    pending: tuple[str, str, str] | None = None
    entry: dict[str, Decimal | str] | None = None
    trades: list[dict[str, Decimal | str]] = []
    decisions: list[dict[str, str]] = []
    completed: list[dict[str, Decimal | str]] = []
    equity_rows: list[dict[str, Decimal | str]] = []
    peak_equity = initial_cash
    previous_temperature = next(
        (
            point.temperature
            for point in reversed(all_points)
            if point.date < points[0].date
        ),
        None,
    )

    for day in timeline:
        bar = bars_by_date.get(day)
        if pending is not None and pending[1] == day and bar is not None:
            action, _execution_date, reason = pending
            if action == "BUY" and quantity == 0:
                price = _buy_price(bar.open, slippage_bps)
                quantity = (cash / (price * (Decimal("1") + commission_bps / Decimal("10000")))).to_integral_value(rounding=ROUND_DOWN)
                if quantity > 0:
                    notional = price * quantity
                    fees = notional * commission_bps / Decimal("10000")
                    cash -= notional + fees
                    trades.append(
                        _trade(
                            day=day,
                            action="BUY",
                            price=price,
                            quantity=quantity,
                            notional=notional,
                            fees=fees,
                            cash_after=cash,
                            reason=reason,
                        )
                    )
                    entry = {
                        "date": day,
                        "notional": notional,
                        "fees": fees,
                    }
            elif action == "EXIT" and quantity > 0 and entry is not None:
                trade, cash = _exit_trade(
                    day=day,
                    raw_price=bar.open,
                    quantity=quantity,
                    cash=cash,
                    commission_bps=commission_bps,
                    slippage_bps=slippage_bps,
                    reason=reason,
                )
                trades.append(trade)
                completed.append(_complete_round(entry, trade))
                quantity = Decimal("0")
                entry = None
            pending = None

        point = points_by_date.get(day)
        if point is not None:
            held = quantity > 0 or (pending is not None and pending[0] == "BUY")
            decision = decide_temperature_transition(
                previous_temperature,
                point.temperature,
                held,
            )
            decisions.append(
                {
                    "date": point.date,
                    "previous_temperature": previous_temperature or "",
                    "temperature": point.temperature,
                    "action": decision.action,
                    "reason": decision.reason,
                }
            )
            next_bar_index = bisect_right(bar_dates, day)
            if decision.action in {"BUY", "EXIT"} and pending is None and next_bar_index < len(bar_dates):
                pending = (decision.action, bar_dates[next_bar_index], decision.reason)
            previous_temperature = point.temperature

        if bar is not None:
            equity = cash + quantity * bar.close
            peak_equity = max(peak_equity, equity)
            drawdown = Decimal("0") if peak_equity == 0 else (peak_equity - equity) / peak_equity * Decimal("100")
            equity_rows.append(
                {
                    "date": day,
                    "cash": cash,
                    "position_quantity": quantity,
                    "close": bar.close,
                    "equity": equity,
                    "drawdown_pct": max(Decimal("0"), drawdown),
                }
            )

    if quantity > 0 and entry is not None:
        final_bar = bars[-1]
        trade, cash = _exit_trade(
            day=final_bar.date,
            raw_price=final_bar.close,
            quantity=quantity,
            cash=cash,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            reason="end_of_data",
        )
        trades.append(trade)
        completed.append(_complete_round(entry, trade))
        quantity = Decimal("0")
        final_drawdown = (
            Decimal("0")
            if peak_equity == 0
            else (peak_equity - cash) / peak_equity * Decimal("100")
        )
        equity_rows[-1] = {
            "date": final_bar.date,
            "cash": cash,
            "position_quantity": quantity,
            "close": final_bar.close,
            "equity": cash,
            "drawdown_pct": max(Decimal("0"), final_drawdown),
        }

    return (
        [_trade_row(trade) for trade in trades],
        [_equity_row(row) for row in equity_rows],
        [_round_row(round_) for round_ in completed],
        decisions,
    )


def _trade(
    *,
    day: str,
    action: str,
    price: Decimal,
    quantity: Decimal,
    notional: Decimal,
    fees: Decimal,
    cash_after: Decimal,
    reason: str,
) -> dict[str, Decimal | str]:
    return {
        "date": day,
        "action": action,
        "side": action,
        "price": price,
        "quantity": quantity,
        "notional": notional,
        "fees": fees,
        "cash_after": cash_after,
        "reason": reason,
    }


def _exit_trade(
    *,
    day: str,
    raw_price: Decimal,
    quantity: Decimal,
    cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
    reason: str,
) -> tuple[dict[str, Decimal | str], Decimal]:
    price = _sell_price(raw_price, slippage_bps)
    notional = price * quantity
    fees = notional * commission_bps / Decimal("10000")
    cash += notional - fees
    return (
        _trade(
            day=day,
            action="EXIT",
            price=price,
            quantity=quantity,
            notional=notional,
            fees=fees,
            cash_after=cash,
            reason=reason,
        ),
        cash,
    )


def _complete_round(
    entry: dict[str, Decimal | str],
    exit_trade: dict[str, Decimal | str],
) -> dict[str, Decimal | str]:
    entry_cost = entry["notional"] + entry["fees"]  # type: ignore[operator]
    proceeds = exit_trade["notional"] - exit_trade["fees"]  # type: ignore[operator]
    net_pnl = proceeds - entry_cost  # type: ignore[operator]
    net_return = Decimal("0") if entry_cost == 0 else net_pnl / entry_cost  # type: ignore[comparison-overlap,operator]
    outcome = "win" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat"  # type: ignore[operator]
    return {
        "entry_date": entry["date"],
        "exit_date": exit_trade["date"],
        "net_pnl": net_pnl,
        "net_return": net_return,
        "outcome": outcome,
    }


def _metrics(
    equity_curve: list[dict[str, str]],
    completed_rounds: list[dict[str, str]],
    *,
    initial_cash: Decimal,
) -> dict[str, object]:
    final_equity = Decimal(equity_curve[-1]["equity"]) if equity_curve else initial_cash
    total_return_pct = Decimal("0") if initial_cash == 0 else (final_equity - initial_cash) / initial_cash * Decimal("100")
    wins = [Decimal(round_["net_return"]) for round_ in completed_rounds if round_["outcome"] == "win"]
    losses = [Decimal(round_["net_return"]) for round_ in completed_rounds if round_["outcome"] == "loss"]
    flats = [round_ for round_ in completed_rounds if round_["outcome"] == "flat"]
    if not wins:
        payoff_ratio: str | None = None
        payoff_status = "no_wins"
    elif not losses:
        payoff_ratio = None
        payoff_status = "no_losses"
    else:
        average_loss = abs(sum(losses, Decimal("0")) / Decimal(len(losses)))
        if average_loss == 0:
            payoff_ratio = None
            payoff_status = "zero_denominator"
        else:
            payoff_ratio = _decimal_text(
                (sum(wins, Decimal("0")) / Decimal(len(wins))) / average_loss
            )
            payoff_status = "available"
    max_drawdown_pct = max(
        (Decimal(row["drawdown_pct"]) for row in equity_curve),
        default=Decimal("0"),
    )
    completed_count = len(completed_rounds)
    return {
        "final_equity": _decimal_text(final_equity),
        "total_return_pct": _decimal_text(total_return_pct),
        "max_drawdown_pct": _decimal_text(max(Decimal("0"), max_drawdown_pct)),
        "completed_round_count": completed_count,
        "profitable_round_count": len(wins),
        "losing_round_count": len(losses),
        "flat_round_count": len(flats),
        "win_rate": (
            None
            if completed_count == 0
            else _decimal_text(Decimal(len(wins)) / Decimal(completed_count))
        ),
        "payoff_ratio": payoff_ratio,
        "payoff_ratio_status": payoff_status,
    }


def _buy_and_hold(
    bars: list[PriceBar],
    *,
    initial_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
) -> dict[str, object]:
    first_bar = bars[0]
    last_bar = bars[-1]
    buy_price = _buy_price(first_bar.open, slippage_bps)
    quantity = (
        initial_cash
        / (buy_price * (Decimal("1") + commission_bps / Decimal("10000")))
    ).to_integral_value(rounding=ROUND_DOWN)
    buy_notional = buy_price * quantity
    buy_fees = buy_notional * commission_bps / Decimal("10000")
    cash = initial_cash - buy_notional - buy_fees
    sell_price = _sell_price(last_bar.close, slippage_bps)
    sell_notional = sell_price * quantity
    sell_fees = sell_notional * commission_bps / Decimal("10000")
    final_equity = cash + sell_notional - sell_fees
    return {
        "entry_date": first_bar.date,
        "entry_price": _decimal_text(buy_price),
        "exit_date": last_bar.date,
        "exit_price": _decimal_text(sell_price),
        "quantity": _decimal_text(quantity),
        "final_equity": _decimal_text(final_equity),
        "total_return_pct": _decimal_text(
            (final_equity - initial_cash) / initial_cash * Decimal("100")
        ),
    }


def _trade_row(trade: dict[str, Decimal | str]) -> dict[str, str]:
    return {
        key: _decimal_text(value) if isinstance(value, Decimal) else str(value)
        for key, value in trade.items()
    }


def _round_row(round_: dict[str, Decimal | str]) -> dict[str, str]:
    return {
        key: _decimal_text(value) if isinstance(value, Decimal) else str(value)
        for key, value in round_.items()
    }


def _equity_row(row: dict[str, Decimal | str]) -> dict[str, str]:
    return {
        key: _decimal_text(value) if isinstance(value, Decimal) else str(value)
        for key, value in row.items()
    }


def _buy_price(price: Decimal, slippage_bps: Decimal) -> Decimal:
    return price * (Decimal("1") + slippage_bps / Decimal("10000"))


def _sell_price(price: Decimal, slippage_bps: Decimal) -> Decimal:
    return price * (Decimal("1") - slippage_bps / Decimal("10000"))


def _canonical_date(value: date | str | Any, field: str) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return text


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
