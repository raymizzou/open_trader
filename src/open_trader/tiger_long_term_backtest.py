from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import urlopen

from .standard_strategies import StrategyBar
from .tiger_long_term import allocate_target_weights, rebalance_reasons, sma200_state


CENT = Decimal("0.01")
MAX_DGS3MO_PUBLICATION_LAG_DAYS = 10


class TigerUsFeeModel:
    """Tiger's published US-stock fees as captured on 2026-07-14."""

    commission_per_share = Decimal("0.0049")
    commission_minimum = Decimal("0.99")
    commission_notional_cap = Decimal("0.005")
    platform_per_share = Decimal("0.005")
    platform_minimum = Decimal("1")
    platform_notional_cap = Decimal("0.005")
    settlement_per_share = Decimal("0.003")
    settlement_notional_cap = Decimal("0.07")
    sec_sell_rate = Decimal("0.0000206")
    finra_sell_per_share = Decimal("0.000195")
    finra_minimum = Decimal("0.01")
    finra_maximum = Decimal("9.79")

    def fee(self, side: str, quantity: Decimal, price: Decimal) -> Decimal:
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("Tiger fee side must be BUY or SELL")
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise ValueError("Tiger fee quantity and price must be positive and finite")
        trade_value = quantity * price
        if quantity < 1:
            return min(trade_value * Decimal("0.01"), Decimal("1")).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )

        commission = max(
            min(quantity * self.commission_per_share, trade_value * self.commission_notional_cap),
            self.commission_minimum,
        )
        platform = max(
            min(quantity * self.platform_per_share, trade_value * self.platform_notional_cap),
            self.platform_minimum,
        )
        settlement = min(
            quantity * self.settlement_per_share,
            trade_value * self.settlement_notional_cap,
        )
        total = sum(
            (self._cent(commission), self._cent(platform), self._cent(settlement)),
            Decimal("0"),
        )
        if normalized_side == "SELL":
            sec = max(trade_value * self.sec_sell_rate, CENT)
            finra = min(
                max(quantity * self.finra_sell_per_share, self.finra_minimum),
                self.finra_maximum,
            )
            total += self._cent(sec) + self._cent(finra)
        return total

    @staticmethod
    def _cent(value: Decimal) -> Decimal:
        return value.quantize(CENT, rounding=ROUND_HALF_UP)


def load_dgs3mo_csv(path: Path) -> dict[date, Decimal]:
    rates: dict[date, Decimal] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            date_field = _dgs3mo_date_field(reader.fieldnames)
            for row in reader:
                raw_date = str(row.get(date_field) or "").strip()
                raw_rate = str(row.get("DGS3MO") or "").strip()
                if raw_rate in {"", "."}:
                    continue
                try:
                    observation_date = date.fromisoformat(raw_date)
                    rate = Decimal(raw_rate)
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError("DGS3MO CSV contains an invalid observation") from exc
                if observation_date in rates:
                    raise ValueError("DGS3MO CSV contains a duplicate date")
                if not rate.is_finite() or rate < 0:
                    raise ValueError("DGS3MO rate must be finite and non-negative")
                rates[observation_date] = rate
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    if not rates:
        raise ValueError("DGS3MO series has no valid observations")
    return dict(sorted(rates.items()))


def cash_growth(rate: Decimal, calendar_days: int) -> Decimal:
    if not rate.is_finite() or rate < 0 or calendar_days < 0:
        raise ValueError("cash rate and calendar days must be non-negative")
    return (Decimal("1") + rate / Decimal("100")) ** (
        Decimal(calendar_days) / Decimal("365")
    ) - Decimal("1")


def ensure_dgs3mo_rates(
    data_dir: Path,
    end_date: date,
    *,
    opener: Callable[[str], Any] = urlopen,
) -> tuple[dict[date, Decimal], str]:
    path = data_dir / "rates" / "DGS3MO.csv"
    if path.exists() and _last_csv_date(path) >= end_date:
        return load_dgs3mo_csv(path), hashlib.sha256(path.read_bytes()).hexdigest()

    query = urlencode({
        "id": "DGS3MO",
        "cosd": "1962-01-02",
        "coed": end_date.isoformat(),
    })
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{query}"
    try:
        with opener(url) as response:
            body = response.read()
    except OSError as exc:
        raise ValueError("DGS3MO download failed") from exc
    if not body:
        raise ValueError("DGS3MO download was empty")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(body)
        rates = load_dgs3mo_csv(temp_path)
        latest = _last_csv_date(temp_path)
        if (end_date - latest).days > MAX_DGS3MO_PUBLICATION_LAG_DAYS:
            raise ValueError("DGS3MO download is stale")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return rates, hashlib.sha256(body).hexdigest()


def _last_csv_date(path: Path) -> date:
    latest: date | None = None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            date_field = _dgs3mo_date_field(reader.fieldnames)
            for row in reader:
                try:
                    observation_date = date.fromisoformat(
                        str(row.get(date_field) or "").strip()
                    )
                except ValueError:
                    continue
                latest = observation_date if latest is None else max(latest, observation_date)
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    return latest or date.min


def _dgs3mo_date_field(fieldnames: Sequence[str] | None) -> str:
    if fieldnames and "DGS3MO" in fieldnames:
        for candidate in ("DATE", "observation_date"):
            if candidate in fieldnames:
                return candidate
    raise ValueError("DGS3MO CSV must contain DATE or observation_date and DGS3MO")


def run_tiger_long_term_backtest(
    bars_by_symbol: Mapping[str, Sequence[StrategyBar]],
    risk_groups: Mapping[str, str],
    rates: Mapping[date, Decimal],
    *,
    initial_cash: Decimal,
) -> dict[str, object]:
    if not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial cash must be positive and finite")
    if not bars_by_symbol or set(bars_by_symbol) != set(risk_groups):
        raise ValueError("bars and risk groups must contain the same members")
    ordered = {
        symbol: _validated_bars(symbol, bars)
        for symbol, bars in sorted(bars_by_symbol.items())
    }
    if not rates or any(not rate.is_finite() or rate < 0 for rate in rates.values()):
        raise ValueError("cash rates must be finite and non-negative")

    warmup_start = max(bars[0].date for bars in ordered.values())
    evaluation_start = _add_months(warmup_start, 12)
    evaluation_boundary = _add_months(evaluation_start, 60)
    sessions = sorted({
        bar.date
        for bars in ordered.values()
        for bar in bars
        if evaluation_start <= bar.date < evaluation_boundary
    })
    if len(sessions) < 2:
        raise ValueError("Tiger backtest requires a five-year evaluation range")
    for symbol, bars in ordered.items():
        if sum(bar.date < evaluation_start for bar in bars) < 200:
            raise ValueError(f"{symbol} does not have a full SMA200 warm-up")

    strategy = _simulate_portfolio(
        ordered,
        risk_groups,
        rates,
        sessions,
        evaluation_start,
        initial_cash,
        force_long=False,
    )
    benchmark = _simulate_portfolio(
        ordered,
        risk_groups,
        rates,
        sessions,
        evaluation_start,
        initial_cash,
        force_long=True,
    )
    return {
        "evaluation_start": evaluation_start.isoformat(),
        "evaluation_end": sessions[-1].isoformat(),
        "strategy": strategy,
        "benchmark": benchmark,
    }


def run_spy_buy_hold_backtest(
    bars: Sequence[StrategyBar],
    rates: Mapping[date, Decimal],
    *,
    initial_cash: Decimal,
) -> dict[str, object]:
    ordered = _validated_bars("SPY", bars)
    if not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial cash must be positive and finite")
    evaluation_start = _add_months(ordered[0].date, 12)
    boundary = _add_months(evaluation_start, 60)
    evaluation = [bar for bar in ordered if evaluation_start <= bar.date < boundary]
    if len(evaluation) < 2:
        raise ValueError("SPY buy-and-hold requires a five-year evaluation range")

    fee_model = TigerUsFeeModel()
    cash = initial_cash
    cash_interest = Decimal("0")
    quantity = Decimal("0")
    fees = Decimal("0")
    slippage = Decimal("0")
    curve: list[dict[str, str]] = []
    orders: list[dict[str, str]] = []
    previous_date: date | None = None
    for index, bar in enumerate(evaluation):
        if previous_date is not None:
            interest = cash * cash_growth(
                _rate_on_or_before(rates, previous_date),
                (bar.date - previous_date).days,
            )
            cash += interest
            cash_interest += interest
        if index == 1:
            execution_price = bar.open * Decimal("1.0005")
            quantity = cash / execution_price
            fee = fee_model.fee("BUY", quantity, execution_price)
            quantity = (cash - fee) / execution_price
            fee = fee_model.fee("BUY", quantity, execution_price)
            quantity = (cash - fee) / execution_price
            notional = quantity * execution_price
            cash -= notional + fee
            fees += fee
            slippage = quantity * (execution_price - bar.open)
            orders.append({
                "symbol": "SPY",
                "side": "BUY",
                "decision_date": evaluation[0].date.isoformat(),
                "execution_date": bar.date.isoformat(),
                "quantity": _decimal_text(quantity),
                "open_price": _decimal_text(bar.open),
                "execution_price": _decimal_text(execution_price),
                "fees": _decimal_text(fee),
                "slippage_cost": _decimal_text(slippage),
                "reason": "buy_and_hold_entry",
            })
        equity = cash + quantity * bar.close
        curve.append({
            "date": bar.date.isoformat(),
            "equity": _decimal_text(equity),
            "cash": _decimal_text(cash),
        })
        previous_date = bar.date

    metrics = _portfolio_metrics(curve, rates, initial_cash)
    final_equity = Decimal(curve[-1]["equity"])
    final_invested_weight = quantity * evaluation[-1].close / final_equity
    metrics.update({
        "orders": orders,
        "cash_interest": _decimal_text(cash_interest),
        "fees": _decimal_text(fees),
        "slippage_cost": _decimal_text(slippage),
        "costs": _decimal_text(fees + slippage),
        "turnover_pct": _decimal_text(
            quantity * Decimal(orders[0]["execution_price"]) / initial_cash
            * Decimal("100")
        ),
        "time_in_market_pct": _decimal_text(
            Decimal(len(evaluation) - 1) / Decimal(len(evaluation)) * Decimal("100")
        ),
        "round_trips": 0,
        "final_invested_weight": _decimal_text(final_invested_weight),
        "segments": _six_month_segments(curve, rates, initial_cash, evaluation_start),
    })
    return metrics


def build_validation_gate(
    strategy: Mapping[str, object],
    benchmark: Mapping[str, object],
    *,
    cash_annualized_return_pct: Decimal,
    provenance_ok: bool,
) -> dict[str, object]:
    reasons: list[str] = []
    strategy_sharpe = _optional_decimal(strategy.get("sharpe_ratio"))
    benchmark_sharpe = _optional_decimal(benchmark.get("sharpe_ratio"))
    if strategy_sharpe is None:
        reasons.append("sharpe_undefined")
    else:
        if strategy_sharpe < Decimal("0.8"):
            reasons.append("sharpe_below_floor")
        if benchmark_sharpe is None:
            reasons.append("benchmark_sharpe_undefined")
        elif strategy_sharpe < benchmark_sharpe:
            reasons.append("sharpe_below_benchmark")

    strategy_calmar = _optional_decimal(strategy.get("calmar_ratio"))
    benchmark_calmar = _optional_decimal(benchmark.get("calmar_ratio"))
    if strategy_calmar is None:
        reasons.append("calmar_undefined")
    else:
        if strategy_calmar < Decimal("0.8"):
            reasons.append("calmar_below_floor")
        if benchmark_calmar is None:
            reasons.append("benchmark_calmar_undefined")
        elif strategy_calmar < benchmark_calmar:
            reasons.append("calmar_below_benchmark")

    strategy_return = _required_decimal(strategy, "annualized_return_pct")
    if strategy_return <= cash_annualized_return_pct:
        reasons.append("return_below_cash")
    if _required_decimal(strategy, "max_drawdown_pct") > _required_decimal(
        benchmark,
        "max_drawdown_pct",
    ):
        reasons.append("drawdown_above_benchmark")
    if not provenance_ok:
        reasons.append("provenance_incomplete")
    reasons.append("calibration_required")
    return {
        "passed": False,
        "policy_id": "tiger_risk_adjusted/v1",
        "reasons": reasons,
    }


def _validated_bars(symbol: str, bars: Sequence[StrategyBar]) -> list[StrategyBar]:
    ordered = list(bars)
    if not ordered or any(current.date <= previous.date for previous, current in zip(ordered, ordered[1:])):
        raise ValueError(f"{symbol} bars must be non-empty and strictly chronological")
    for bar in ordered:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ValueError(f"{symbol} contains an invalid price")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError(f"{symbol} contains an invalid OHLC bar")
    return ordered


def _simulate_portfolio(
    bars_by_symbol: Mapping[str, Sequence[StrategyBar]],
    risk_groups: Mapping[str, str],
    rates: Mapping[date, Decimal],
    sessions: Sequence[date],
    evaluation_start: date,
    initial_cash: Decimal,
    *,
    force_long: bool,
) -> dict[str, object]:
    symbols = tuple(sorted(bars_by_symbol))
    maps = {symbol: {bar.date: bar for bar in bars} for symbol, bars in bars_by_symbol.items()}
    histories = {
        symbol: [bar for bar in bars if bar.date < evaluation_start]
        for symbol, bars in bars_by_symbol.items()
    }
    latest_close = {symbol: history[-1].close for symbol, history in histories.items()}
    quantities = {symbol: Decimal("0") for symbol in symbols}
    pending: dict[str, dict[str, object]] = {}
    previous_states: dict[str, str] = {}
    cash = initial_cash
    cash_interest = Decimal("0")
    total_fees = Decimal("0")
    slippage_cost = Decimal("0")
    turnover = Decimal("0")
    round_trips = 0
    contributions = {symbol: Decimal("0") for symbol in symbols}
    orders: list[dict[str, str]] = []
    curve: list[dict[str, str]] = []
    member_weights: list[dict[str, str]] = []
    fee_model = TigerUsFeeModel()
    previous_session: date | None = None
    in_market_days = 0

    for session in sessions:
        if previous_session is not None:
            days = (session - previous_session).days
            interest = cash * cash_growth(_rate_on_or_before(rates, previous_session), days)
            cash += interest
            cash_interest += interest

        for symbol in symbols:
            bar = maps[symbol].get(session)
            instruction = pending.get(symbol)
            if bar is None or instruction is None:
                continue
            open_marks = dict(latest_close)
            open_marks[symbol] = bar.open
            equity_at_open = cash + sum(
                quantities[item] * open_marks[item]
                for item in symbols
            )
            target_weight = instruction["target_weight"]
            assert isinstance(target_weight, Decimal)
            target_value = equity_at_open * target_weight
            current_value = quantities[symbol] * bar.open
            delta_value = target_value - current_value
            if abs(delta_value) <= Decimal("0.000001"):
                pending.pop(symbol, None)
                continue
            side = "BUY" if delta_value > 0 else "SELL"
            execution_price = bar.open * (
                Decimal("1.0005") if side == "BUY" else Decimal("0.9995")
            )
            quantity = abs(delta_value) / execution_price
            if side == "SELL":
                quantity = min(quantity, quantities[symbol])
            fee = fee_model.fee(side, quantity, execution_price)
            if side == "BUY" and quantity * execution_price + fee > cash:
                quantity = max(Decimal("0"), (cash - fee) / execution_price)
                if quantity <= 0:
                    pending.pop(symbol, None)
                    continue
                fee = fee_model.fee(side, quantity, execution_price)
            notional = quantity * execution_price
            raw_notional = quantity * bar.open
            if side == "BUY":
                cash -= notional + fee
                quantities[symbol] += quantity
                contributions[symbol] -= notional + fee
            else:
                cash += notional - fee
                quantities[symbol] -= quantity
                if quantities[symbol] < Decimal("0.000000000001"):
                    quantities[symbol] = Decimal("0")
                    round_trips += 1
                contributions[symbol] += notional - fee
            total_fees += fee
            slippage = abs(notional - raw_notional)
            slippage_cost += slippage
            turnover += notional
            orders.append({
                "symbol": symbol,
                "side": side,
                "decision_date": str(instruction["decision_date"]),
                "execution_date": session.isoformat(),
                "quantity": _decimal_text(quantity),
                "open_price": _decimal_text(bar.open),
                "execution_price": _decimal_text(execution_price),
                "fees": _decimal_text(fee),
                "slippage_cost": _decimal_text(slippage),
                "reason": str(instruction["reason"]),
            })
            pending.pop(symbol, None)

        for symbol in symbols:
            bar = maps[symbol].get(session)
            if bar is not None:
                histories[symbol].append(bar)
                latest_close[symbol] = bar.close

        equity = cash + sum(quantities[symbol] * latest_close[symbol] for symbol in symbols)
        actual = {
            symbol: quantities[symbol] * latest_close[symbol] / equity
            if equity > 0 else Decimal("0")
            for symbol in symbols
        }
        if any(weight > 0 for weight in actual.values()):
            in_market_days += 1
        curve.append({
            "date": session.isoformat(),
            "equity": _decimal_text(equity),
            "cash": _decimal_text(cash),
        })
        states = {
            symbol: "LONG" if force_long else sma200_state(histories[symbol])
            for symbol in symbols
        }
        targets = allocate_target_weights(states, risk_groups)
        member_weights.extend({
            "date": session.isoformat(),
            "symbol": symbol,
            "weight": _decimal_text(actual[symbol]),
            "target_weight": _decimal_text(targets[symbol]),
        } for symbol in symbols)
        reasons = rebalance_reasons(
            actual,
            targets,
            previous_states,
            states,
            risk_groups,
        )
        state_changed = {
            symbol
            for symbol in symbols
            if previous_states.get(symbol) != states[symbol]
        }
        if state_changed:
            candidates = symbols
        else:
            candidates = tuple(reasons)
        for symbol in candidates:
            if abs(actual[symbol] - targets[symbol]) <= Decimal("0.000000001"):
                continue
            reason = reasons.get(symbol, "state_change_reallocation")
            if symbol in state_changed:
                before = previous_states.get(symbol)
                after = states[symbol]
                if after == "LONG" and before != "LONG":
                    reason = "initial_allocation" if force_long and before is None else "sma200_entry"
                elif before == "LONG" and after != "LONG":
                    reason = "sma200_exit"
            pending[symbol] = {
                "target_weight": targets[symbol],
                "decision_date": session.isoformat(),
                "reason": reason,
            }
        previous_states = states
        previous_session = session

    for symbol in symbols:
        contributions[symbol] += quantities[symbol] * latest_close[symbol]
    metrics = _portfolio_metrics(curve, rates, initial_cash)
    metrics.update({
        "orders": orders,
        "equity_curve": curve,
        "member_weights": member_weights,
        "cash_interest": _decimal_text(cash_interest),
        "fees": _decimal_text(total_fees),
        "slippage_cost": _decimal_text(slippage_cost),
        "costs": _decimal_text(total_fees + slippage_cost),
        "turnover_pct": _decimal_text(turnover / initial_cash * Decimal("100")),
        "time_in_market_pct": _decimal_text(
            Decimal(in_market_days) / Decimal(len(sessions)) * Decimal("100")
        ),
        "round_trips": round_trips,
        "profit_contributions": {
            symbol: _decimal_text(value)
            for symbol, value in contributions.items()
        },
        "segments": _six_month_segments(curve, rates, initial_cash, evaluation_start),
    })
    return metrics


def _portfolio_metrics(
    curve: Sequence[Mapping[str, str]],
    rates: Mapping[date, Decimal],
    initial_cash: Decimal,
) -> dict[str, object]:
    if not curve:
        return {
            "total_return_pct": "0",
            "annualized_return_pct": "0",
            "max_drawdown_pct": "0",
            "sharpe_ratio": None,
            "calmar_ratio": None,
        }
    equities = [Decimal(row["equity"]) for row in curve]
    dates = [date.fromisoformat(row["date"]) for row in curve]
    final = equities[-1]
    total_return = (final / initial_cash - Decimal("1")) * Decimal("100")
    elapsed_days = max(1, (dates[-1] - dates[0]).days)
    annualized = (
        (final / initial_cash) ** (Decimal("365") / Decimal(elapsed_days))
        - Decimal("1")
    ) * Decimal("100") if final > 0 else Decimal("-100")
    peak = equities[0]
    max_drawdown = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * Decimal("100"))
    excess_returns: list[Decimal] = []
    for previous_equity, current_equity, previous_date, current_date in zip(
        equities,
        equities[1:],
        dates,
        dates[1:],
    ):
        if previous_equity <= 0:
            continue
        portfolio_return = current_equity / previous_equity - Decimal("1")
        risk_free_return = cash_growth(
            _rate_on_or_before(rates, previous_date),
            (current_date - previous_date).days,
        )
        excess_returns.append(portfolio_return - risk_free_return)
    sharpe = _annualized_sharpe(excess_returns)
    calmar = annualized / max_drawdown if max_drawdown else None
    return {
        "total_return_pct": _decimal_text(total_return),
        "annualized_return_pct": _decimal_text(annualized),
        "max_drawdown_pct": _decimal_text(max_drawdown),
        "sharpe_ratio": None if sharpe is None else _decimal_text(sharpe),
        "calmar_ratio": None if calmar is None else _decimal_text(calmar),
    }


def _annualized_sharpe(excess_returns: Sequence[Decimal]) -> Decimal | None:
    if len(excess_returns) < 2:
        return None
    mean = sum(excess_returns, Decimal("0")) / Decimal(len(excess_returns))
    variance = sum(
        ((value - mean) ** 2 for value in excess_returns),
        Decimal("0"),
    ) / Decimal(len(excess_returns))
    if variance == 0:
        return None
    return mean / variance.sqrt() * Decimal(252).sqrt()


def _six_month_segments(
    curve: Sequence[Mapping[str, str]],
    rates: Mapping[date, Decimal],
    initial_cash: Decimal,
    evaluation_start: date,
) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for index in range(10):
        start = _add_months(evaluation_start, index * 6)
        end = _add_months(evaluation_start, (index + 1) * 6)
        rows = [row for row in curve if start <= date.fromisoformat(row["date"]) < end]
        segment_initial = Decimal(rows[0]["equity"]) if rows else initial_cash
        metrics = _portfolio_metrics(rows, rates, segment_initial)
        segments.append({
            "index": index + 1,
            "start": start.isoformat(),
            "end": end.isoformat(),
            **metrics,
        })
    return segments


def _rate_on_or_before(rates: Mapping[date, Decimal], target: date) -> Decimal:
    ordered_dates = sorted(rates)
    index = bisect_right(ordered_dates, target) - 1
    if index < 0:
        raise ValueError(f"DGS3MO has no observation on or before {target.isoformat()}")
    return rates[ordered_dates[index]]


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = (
        date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        - date(year, month, 1)
    ).days
    return date(year, month, min(value.day, days_in_month))


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("cannot serialize a non-finite decimal")
    return format(value, "f")


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("validation metric must be decimal or null") from exc
    if not parsed.is_finite():
        raise ValueError("validation metric must be finite")
    return parsed


def _required_decimal(payload: Mapping[str, object], key: str) -> Decimal:
    parsed = _optional_decimal(payload.get(key))
    if parsed is None:
        raise ValueError(f"validation metric {key} is required")
    return parsed
