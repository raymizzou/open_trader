from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Callable
from zoneinfo import ZoneInfo

from .daily_premarket import (
    DailyPremarketConfig,
    RunLock,
    send_notification_with_results,
)
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .kline_technical_facts import DailyKlineBar
from .notifications import Notifier, NullNotifier
from .trend_animals import (
    TrendAnimalsClient,
    TrendAnimalsError,
    TrendAnimalsLookupError,
)


NO_ACTION_TEXT = "现金也是有效仓位，本日无需交易。"
DISCLAIMER_TEXT = (
    "本报告是确定性纪律清单，不是订单或成交事实；所有交易由用户人工确认与执行。"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
CANDIDATE_FIELDS = (
    "tmId",
    "tickerName",
    "tickerSymbol",
    "asset",
    "asOfDate",
    "tradableFlag",
    "industryName",
    "amount1d",
    "isTrendRightSide",
    "daysSinceTrendEntry",
    "trendStrengthLocalCurr",
    "stopwinFlagByDangerSignal",
)
HOLDING_FIELDS = CANDIDATE_FIELDS + (
    "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne",
)
@dataclass(frozen=True)
class AShareTrendRunResult:
    status: str
    report_path: Path | None
    json_path: Path | None


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    name: str
    asset_class: str
    quantity: Decimal
    avg_cost_price: Decimal | None
    market_value: Decimal = Decimal("0")


@dataclass(frozen=True)
class AccountSnapshot:
    source_date: str
    fresh: bool
    net_value: Decimal
    available_cash: Decimal
    positions: tuple[AccountPosition, ...]
    exceptions: tuple[str, ...]


@dataclass(frozen=True)
class CandidateInput:
    tm_id: int
    symbol: str
    exchange: str
    name: str
    asset: str
    industry: str
    as_of_date: str
    tradable: object
    amount: Decimal | None
    right_side: object
    days: int | None
    strength: Decimal | None
    danger: object
    close: Decimal | None
    atr: Decimal | None
    pools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateDecision:
    eligible: tuple[CandidateInput, ...]
    excluded: dict[str, list[str]]


@dataclass(frozen=True)
class BuyAction:
    symbol: str
    name: str
    target_amount: Decimal
    estimated_shares: int
    close: Decimal
    estimated_initial_line: Decimal


@dataclass(frozen=True)
class HoldingSnapshot:
    tm_id: int
    symbol: str
    exchange: str
    name: str
    as_of_date: str
    right_side: bool | None
    danger: bool | None
    boiling: bool | None
    champagne: bool | None
    industry: str = ""


@dataclass(frozen=True)
class HoldingDecision:
    symbol: str
    name: str
    industry: str
    action: str
    reason: str
    initial_line: Decimal | None
    active_line: Decimal | None
    atr: Decimal | None
    historical: bool


@dataclass(frozen=True)
class TrendReport:
    schema_version: int
    generated_at: str
    as_of_date: str
    execution_date: str
    account: AccountSnapshot
    api_facts: tuple[str, ...]
    holdings: tuple[HoldingDecision, ...]
    candidates: tuple[CandidateInput, ...]
    excluded: dict[str, list[str]]
    buy_actions: tuple[BuyAction, ...]
    industry_concentration: tuple[tuple[str, int, Decimal], ...]
    data_sources: tuple[str, ...]
    estimated_api_cost: Decimal | None
    actual_api_cost: Decimal | None
    protection_state: dict[str, object]
    signal_snapshots: dict[str, object]
    metadata: dict[str, object]


def _broker_set(value: str) -> set[str]:
    return {
        part.strip().lower()
        for chunk in value.split(",")
        for part in chunk.split(";")
        if part.strip()
    }


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"invalid decimal value: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"invalid decimal value: {value!r}")
    return result


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None or str(value).strip() == "" else _decimal(value)


def _optional_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid integer value: {value!r}")
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f"invalid integer value: {value!r}") from None


def _account_exceptions(rows: Sequence[Mapping[str, str]]) -> list[str]:
    exceptions: list[str] = []
    for row in rows:
        market = row.get("market", "").strip().upper()
        asset_class = row.get("asset_class", "").strip().lower()
        currency = row.get("currency", "").strip().upper()
        if market == "CN" and asset_class in {"stock", "etf"}:
            continue
        if market == "CASH" and asset_class == "cash" and currency == "CNY":
            continue
        symbol = row.get("symbol", "").strip() or "<missing-symbol>"
        name = row.get("name", "").strip() or "<missing-name>"
        exceptions.append(
            f"unsupported Eastmoney asset: {symbol} {name} ({market}/{asset_class})"
        )
    return exceptions


def load_eastmoney_account(
    path: Path,
    *,
    expected_date: str,
    timezone: ZoneInfo = ZoneInfo("Asia/Shanghai"),
) -> AccountSnapshot:
    source_date = datetime.fromtimestamp(path.stat().st_mtime, timezone).date().isoformat()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        brokers = _broker_set(row.get("brokers", ""))
        if "eastmoney" in brokers and brokers != {"eastmoney"}:
            raise ValueError(
                f"portfolio row {row.get('symbol', '')} mixes Eastmoney with other brokers"
            )
    eastmoney = [
        row for row in rows if _broker_set(row.get("brokers", "")) == {"eastmoney"}
    ]
    net_value = sum((_decimal(row["market_value"]) for row in eastmoney), Decimal("0"))
    cash = sum(
        (
            _decimal(row["market_value"])
            for row in eastmoney
            if row.get("market", "").strip().upper() == "CASH"
            and row.get("currency", "").strip().upper() == "CNY"
        ),
        Decimal("0"),
    )
    positions = tuple(
        AccountPosition(
            symbol=row["symbol"].strip(),
            name=row["name"].strip(),
            asset_class=row["asset_class"].strip().lower(),
            quantity=_decimal(row["total_quantity"]),
            avg_cost_price=_optional_decimal(row.get("avg_cost_price", "")),
            market_value=_decimal(row["market_value"]),
        )
        for row in eastmoney
        if row.get("market", "").strip().upper() == "CN"
        and row.get("asset_class", "").strip().lower() in {"stock", "etf"}
        and _decimal(row.get("total_quantity", "")) > 0
    )
    return AccountSnapshot(
        source_date=source_date,
        fresh=source_date == expected_date,
        net_value=net_value,
        available_cash=cash,
        positions=positions,
        exceptions=tuple(_account_exceptions(eastmoney)),
    )


def atr14(bars: Sequence[DailyKlineBar]) -> Decimal | None:
    valid = [bar for bar in bars if None not in (bar.high, bar.low)]
    if len(valid) < 15:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(valid[-15:-1], valid[-14:]):
        high = _decimal(current.high)
        low = _decimal(current.low)
        previous_close = _decimal(previous.close)
        ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return sum(ranges, Decimal("0")) / Decimal("14")


def _kline_metrics(
    bars: Sequence[DailyKlineBar], *, before: str | None = None
) -> tuple[Decimal | None, Decimal | None, tuple[Decimal, ...]]:
    if not bars:
        return None, None, ()
    try:
        atr = atr14(bars)
        close = _decimal(bars[-1].close)
        lows = tuple(
            _decimal(bar.low)
            for bar in bars
            if before is not None and bar.date < before and bar.low is not None
        )[-5:]
    except ValueError:
        return None, None, ()
    return atr, close, lows


def _symbol_parts(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("tickerSymbol must be a string")
    parts = value.strip().upper().rsplit(".", 1)
    if len(parts) == 1:
        exchange, symbol = to_futu_symbol("CN", parts[0]).split(".", 1)
        return symbol, exchange
    if len(parts) != 2 or len(parts[0]) != 6 or not parts[0].isdigit() or not parts[1]:
        raise ValueError(f"invalid tickerSymbol: {value!r}")
    return parts[0], parts[1]


def evaluate_candidate(
    row: Mapping[str, object],
    bars: Sequence[DailyKlineBar] | None,
    *,
    pools: Sequence[str] = (),
) -> CandidateInput:
    symbol, exchange = _symbol_parts(row.get("tickerSymbol"))
    daily_bars = tuple(bars or ())
    atr, close, _ = _kline_metrics(daily_bars)
    tm_id = row.get("tmId")
    if isinstance(tm_id, bool) or not isinstance(tm_id, int):
        raise ValueError("tmId must be an integer")
    return CandidateInput(
        tm_id=tm_id,
        symbol=symbol,
        exchange=exchange,
        name=str(row.get("tickerName") or "").strip(),
        asset=str(row.get("asset") or "").strip(),
        industry=str(row.get("industryName") or "").strip(),
        as_of_date=str(row.get("asOfDate") or "").strip(),
        tradable=row.get("tradableFlag"),
        amount=_optional_decimal(row.get("amount1d")),
        right_side=row.get("isTrendRightSide"),
        days=_optional_int(row.get("daysSinceTrendEntry")),
        strength=_optional_decimal(row.get("trendStrengthLocalCurr")),
        danger=row.get("stopwinFlagByDangerSignal"),
        close=close,
        atr=atr,
        pools=tuple(sorted(set(pools))),
    )


def _excluded_name(name: str) -> bool:
    normalized = name.strip().upper()
    return "ST" in normalized or "退" in name


def _candidate_reasons(
    item: CandidateInput, held_symbols: set[str], expected_date: str | None = None
) -> list[str]:
    reasons: list[str] = []
    if item.right_side is not True:
        reasons.append("right_side_not_true")
    if item.strength is None or item.strength <= 90:
        reasons.append("strength_not_above_90")
    if item.days is None or item.days >= 10:
        reasons.append("right_side_days_not_below_10")
    if item.tradable is not True:
        reasons.append("not_tradable")
    if item.amount is None or item.amount < 1:
        reasons.append("amount_below_1")
    if item.danger is not False:
        reasons.append("danger_signal" if item.danger else "danger_unknown")
    if not item.name:
        reasons.append("name_missing")
    if not item.asset:
        reasons.append("asset_missing")
    elif item.asset not in {"A股", "ETF基金"}:
        reasons.append("unsupported_asset")
    if item.symbol in held_symbols:
        reasons.append("already_held")
    if item.exchange == "BJ" or _excluded_name(item.name):
        reasons.append("excluded_security")
    elif item.exchange not in {"SH", "SZ"}:
        reasons.append("unsupported_exchange")
    if item.atr is None:
        reasons.append("atr_unavailable")
    if expected_date is not None and item.as_of_date != expected_date:
        reasons.append("data_date_mismatch")
    return reasons


def _candidate_sort_key(item: CandidateInput) -> tuple[Decimal, int, Decimal, str]:
    return (
        -item.strength,  # type: ignore[operator]
        item.days,  # type: ignore[return-value]
        -item.amount,  # type: ignore[operator]
        item.symbol,
    )


def build_candidate_list(
    rows: Sequence[CandidateInput],
    *,
    held_symbols: set[str],
    expected_date: str | None = None,
) -> CandidateDecision:
    eligible: list[CandidateInput] = []
    excluded: dict[str, list[str]] = {}
    grouped: dict[str, list[CandidateInput]] = defaultdict(list)
    for item in rows:
        grouped[item.symbol].append(item)
    for symbol in sorted(grouped):
        items = grouped[symbol]
        reasons = list(
            dict.fromkeys(
                reason
                for item in items
                for reason in _candidate_reasons(item, held_symbols, expected_date)
            )
        )
        if reasons:
            excluded[symbol] = reasons
        else:
            eligible.append(min(items, key=_candidate_sort_key))
    eligible.sort(key=_candidate_sort_key)
    return CandidateDecision(tuple(eligible), excluded)


def estimate_buy_actions(
    *,
    ranked: Sequence[CandidateInput],
    account_fresh: bool,
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
) -> list[BuyAction]:
    slots = max(0, 10 - current_position_count)
    if not account_fresh or slots == 0:
        return []
    target = (net_value * Decimal("0.01")).quantize(Decimal("0.01"))
    remaining_cash = available_cash
    actions: list[BuyAction] = []
    for item in ranked:
        if slots == 0 or remaining_cash <= 0:
            break
        if item.close is None or item.close <= 0 or item.atr is None:
            continue
        amount = min(target, remaining_cash)
        shares = int(amount / item.close / 100) * 100
        if shares <= 0:
            continue
        actions.append(
            BuyAction(
                symbol=item.symbol,
                name=item.name,
                target_amount=amount,
                estimated_shares=shares,
                close=item.close,
                estimated_initial_line=item.close - Decimal("2") * item.atr,
            )
        )
        remaining_cash -= amount
        slots -= 1
    return actions


def update_protection_line(
    *,
    old_line: Decimal,
    boiling: bool,
    champagne: bool,
    prior_five_lows: Sequence[Decimal],
) -> Decimal:
    if not (boiling or champagne) or len(prior_five_lows) != 5:
        return old_line
    return max(old_line, min(prior_five_lows))


def _state_positions(prior_state: Mapping[str, object] | None) -> Mapping[str, object]:
    if not prior_state:
        return {}
    positions = prior_state.get("positions", {})
    return positions if isinstance(positions, Mapping) else {}


def _state_decimal(state: Mapping[str, object], key: str) -> Decimal | None:
    return _optional_decimal(state.get(key))


def _holding_action(
    *,
    symbol: str,
    snapshot: HoldingSnapshot | None,
    triggered: set[str],
) -> tuple[str, str]:
    if symbol in triggered:
        return "SELL_ALL", "protection_line_already_triggered"
    if snapshot is not None and snapshot.danger is True:
        return "SELL_ALL", "danger_signal"
    if snapshot is not None and snapshot.right_side is False:
        return "SELL_ALL", "left_trend_right_side"
    if snapshot is None or any(
        signal is None
        for signal in (
            snapshot.right_side,
            snapshot.danger,
            snapshot.boiling,
            snapshot.champagne,
        )
    ):
        return "MANUAL_REVIEW", "holding_signal_unknown"
    return "HOLD", "trend_intact"


def _protection_was_triggered(
    symbol: str,
    old_state: Mapping[str, object],
    watch_events: Sequence[Mapping[str, object]],
) -> bool:
    if not old_state:
        return False
    started_for = old_state.get("position_started_for")
    for event in watch_events:
        if event.get("event_type") != "protection_triggered" or str(
            event.get("symbol", "")
        ).strip() != symbol:
            continue
        event_date = event.get("trading_date")
        if not isinstance(event_date, str):
            occurred_at = event.get("occurred_at")
            event_date = occurred_at[:10] if isinstance(occurred_at, str) else ""
        if not isinstance(started_for, str) or not started_for or not event_date:
            return True
        if event_date >= started_for:
            return True
    return False


def build_report(
    *,
    as_of_date: str,
    execution_date: str,
    account: AccountSnapshot,
    candidates: Sequence[CandidateInput],
    holding_snapshots: Mapping[str, HoldingSnapshot | None],
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None],
    prior_state: Mapping[str, object] | None = None,
    watch_events: Sequence[Mapping[str, object]] = (),
    api_facts: Sequence[str] = (),
    data_sources: Sequence[str] = (),
    estimated_api_cost: Decimal | None = None,
    actual_api_cost: Decimal | None = None,
    generated_at: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> TrendReport:
    held_symbols = {position.symbol for position in account.positions}
    candidate_decision = build_candidate_list(
        candidates, held_symbols=held_symbols, expected_date=as_of_date
    )
    displayed_candidates = candidate_decision.eligible[:10]
    buy_actions = estimate_buy_actions(
        ranked=displayed_candidates,
        account_fresh=account.fresh,
        net_value=account.net_value,
        available_cash=account.available_cash,
        current_position_count=len(account.positions),
    )
    old_positions = _state_positions(prior_state)
    holdings: list[HoldingDecision] = []
    new_positions: dict[str, object] = {}
    industries: Counter[str] = Counter()
    industry_values: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for position in account.positions:
        symbol = position.symbol
        returned_snapshot = holding_snapshots.get(symbol)
        snapshot = (
            returned_snapshot
            if returned_snapshot is not None
            and returned_snapshot.as_of_date == as_of_date
            else None
        )
        old = old_positions.get(symbol)
        old_state = old if isinstance(old, Mapping) else {}
        triggered = (
            {symbol}
            if _protection_was_triggered(symbol, old_state, watch_events)
            else set()
        )
        action, reason = _holding_action(
            symbol=symbol, snapshot=snapshot, triggered=triggered
        )
        initial_line = _state_decimal(old_state, "initial_line")
        active_line = _state_decimal(old_state, "active_line")
        old_atr = _state_decimal(old_state, "atr14")
        position_started_for = old_state.get("position_started_for")
        if not isinstance(position_started_for, str) or not position_started_for:
            position_started_for = as_of_date
        tracking_active = old_state.get("tracking_active") is True
        if snapshot is not None and (
            snapshot.boiling is True or snapshot.champagne is True
        ):
            tracking_active = True
        historical = not old_state
        daily_bars = tuple(bars_by_symbol.get(symbol) or ())
        current_atr, close, lows = _kline_metrics(daily_bars, before=as_of_date)
        if active_line is None and current_atr is not None and close is not None:
            initial_line = active_line = close - Decimal("2") * current_atr
        if active_line is not None and tracking_active and action == "HOLD":
            active_line = update_protection_line(
                old_line=active_line,
                boiling=True,
                champagne=False,
                prior_five_lows=lows,
            )
        if active_line is None and action == "HOLD":
            action, reason = "MANUAL_REVIEW", "holding_kline_unavailable"
        effective_atr = current_atr if current_atr is not None else old_atr
        industry = snapshot.industry if snapshot else ""
        if industry:
            industries[industry] += 1
            industry_values[industry] += position.market_value
        holdings.append(
            HoldingDecision(
                symbol=symbol,
                name=position.name,
                industry=industry,
                action=action,
                reason=reason,
                initial_line=initial_line,
                active_line=active_line,
                atr=effective_atr,
                historical=historical,
            )
        )
        if active_line is not None:
            new_positions[symbol] = {
                "initial_line": str(initial_line),
                "active_line": str(active_line),
                "atr14": str(effective_atr) if effective_atr is not None else "",
                "position_started_for": position_started_for,
                "tracking_active": tracking_active,
                "updated_for": as_of_date,
            }
    industry_concentration = tuple(
        (
            industry,
            count,
            (
                industry_values[industry] * Decimal("100") / account.net_value
                if account.net_value > 0
                else Decimal("0")
            ),
        )
        for industry, count in sorted(industries.items())
    )
    holding_signals = {
        position.symbol: (
            _holding_signal(holding_snapshots[position.symbol])
            if holding_snapshots.get(position.symbol) is not None
            else None
        )
        for position in account.positions
    }
    excluded_signals = {
        symbol: [_candidate_signal(item) for item in candidates if item.symbol == symbol]
        for symbol in candidate_decision.excluded
    }
    ranks = {
        (item.tm_id, item.symbol): rank
        for rank, item in enumerate(candidate_decision.eligible, 1)
    }
    candidate_signals = [
        {
            **_candidate_signal(item),
            "eligible": (item.tm_id, item.symbol) in ranks,
            "excluded_reasons": _candidate_reasons(
                item, held_symbols, as_of_date
            ),
            "rank": ranks.get((item.tm_id, item.symbol)),
            "pools": list(item.pools),
            "source": "Trend Animals",
        }
        for item in candidates
    ]
    return TrendReport(
        schema_version=1,
        generated_at=generated_at
        or datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        as_of_date=as_of_date,
        execution_date=execution_date,
        account=account,
        api_facts=tuple(api_facts),
        holdings=tuple(holdings),
        candidates=displayed_candidates,
        excluded=candidate_decision.excluded,
        buy_actions=tuple(buy_actions),
        industry_concentration=industry_concentration,
        data_sources=tuple(data_sources),
        estimated_api_cost=estimated_api_cost,
        actual_api_cost=actual_api_cost,
        protection_state={"schema_version": 1, "positions": new_positions},
        signal_snapshots={
            "holdings": holding_signals,
            "excluded": excluded_signals,
            "candidates": candidate_signals,
        },
        metadata=dict(metadata or {}),
    )


def _holding_signal(item: HoldingSnapshot) -> dict[str, object]:
    return {
        "tm_id": item.tm_id,
        "symbol": item.symbol,
        "as_of_date": item.as_of_date,
        "right_side": item.right_side,
        "danger": item.danger,
        "boiling": item.boiling,
        "champagne": item.champagne,
    }


def _candidate_signal(item: CandidateInput) -> dict[str, object]:
    return {
        "tm_id": item.tm_id,
        "symbol": item.symbol,
        "exchange": item.exchange,
        "name": item.name,
        "asset": item.asset,
        "industry": item.industry,
        "as_of_date": item.as_of_date,
        "tradable": item.tradable,
        "amount": item.amount,
        "right_side": item.right_side,
        "days": item.days,
        "strength": item.strength,
        "danger": item.danger,
    }


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def render_markdown(report: TrendReport) -> str:
    freshness = "新鲜" if report.account.fresh else "陈旧，禁止正式买入"
    industry_facts = {
        industry: (count, weight)
        for industry, count, weight in report.industry_concentration
    }
    lines = [
        f"# A股趋势操作计划 · {report.as_of_date}",
        "",
        "## 日期与账户新鲜度",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 数据日期：{report.as_of_date}",
        f"- 下一执行日：{report.execution_date}",
        f"- 东方财富账户日期：{report.account.source_date}（{freshness}）",
        f"- 当前持仓席位：{len(report.account.positions)}/10",
        "",
        "## API 原始事实",
        "",
    ]
    lines.extend(f"- {fact}" for fact in report.api_facts)
    if not report.api_facts:
        lines.append("- 无可用 API 事实。")
    lines.extend(["", "## 策略纪律判断", "", "### 全部持仓判断", ""])
    if report.holdings:
        for item in report.holdings:
            line = f"- {item.symbol} {item.name}：{item.action}（{item.reason}）"
            if item.active_line is not None:
                line += f"，活动保护线 {_money(item.active_line)}"
            signal = report.signal_snapshots["holdings"].get(item.symbol)  # type: ignore[union-attr]
            if isinstance(signal, Mapping):
                line += (
                    f"；API信号 right_side={signal.get('right_side')}, "
                    f"danger={signal.get('danger')}, boiling={signal.get('boiling')}, "
                    f"champagne={signal.get('champagne')}"
                )
            lines.append(line)
    else:
        lines.append("- 当前无趋势持仓。")
    lines.extend(["", "### 前 10 名开仓序列", ""])
    if report.candidates:
        for index, item in enumerate(report.candidates[:10], 1):
            industry_count, industry_weight = industry_facts.get(
                item.industry, (0, Decimal("0"))
            )
            lines.append(
                f"- {index}. {item.symbol} {item.name}｜强度 {item.strength}｜"
                f"右侧 {item.days} 天｜成交额 {item.amount} 亿元｜"
                f"行业 {item.industry or '未知'}（已占 {industry_count} 个席位，"
                f"当前仓位 {_money(industry_weight)}%）"
            )
    else:
        lines.append("- 无合格候选。")
    lines.extend(["", "### 下一交易时段正式操作", ""])
    sells = [item for item in report.holdings if item.action == "SELL_ALL"]
    for item in sells:
        lines.append(f"- 卖出全部：{item.symbol} {item.name}（{item.reason}）。")
    for item in report.buy_actions:
        lines.append(
            f"- 买入候选：{item.symbol} {item.name}；仅限 {report.execution_date} "
            f"09:30–10:00；收盘价估算 {item.estimated_shares} 股；"
            f"1% 目标金额 {_money(item.target_amount)} 元；"
            f"预计初始保护线 {_money(item.estimated_initial_line)}；"
            "按东方财富实时价格向下重算为 100 股整数倍且不得超过建议金额。"
        )
    if not sells and not report.buy_actions:
        lines.append(NO_ACTION_TEXT)
    lines.extend(["", "## 行业集中度", ""])
    if report.industry_concentration:
        lines.extend(
            f"- {industry}：当前持仓 {count} 个席位，当前仓位 {_money(weight)}%"
            for industry, count, weight in report.industry_concentration
        )
    else:
        lines.append("- 当前无行业持仓集中事实。")
    lines.extend(["", "## 排除项与账户例外", ""])
    for symbol, reasons in report.excluded.items():
        snapshots = report.signal_snapshots["excluded"].get(symbol)  # type: ignore[union-attr]
        signal_text = ""
        if isinstance(snapshots, list) and snapshots:
            signal_text = "; API信号 " + ", ".join(
                f"{key}={value}" for key, value in snapshots[0].items()
            )
        lines.append(f"- {symbol}：{', '.join(reasons)}{signal_text}")
    lines.extend(f"- 账户例外：{item}" for item in report.account.exceptions)
    if not report.excluded and not report.account.exceptions:
        lines.append("- 无。")
    lines.extend(["", "## 数据来源与 API 成本", ""])
    lines.extend(f"- 数据来源：{source}" for source in report.data_sources)
    lines.append(
        "- API 计费表估算："
        + ("未知" if report.estimated_api_cost is None else str(report.estimated_api_cost))
    )
    lines.append(
        "- 本次运行窗口实际余额差："
        + ("未知" if report.actual_api_cost is None else str(report.actual_api_cost))
    )
    lines.extend(
        [
            "",
            "## 免责声明",
            "",
            DISCLAIMER_TEXT,
            "",
        ]
    )
    return "\n".join(lines)


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _report_payload(report: TrendReport) -> dict[str, object]:
    holding_decisions = [_json_value(asdict(item)) for item in report.holdings]
    top10_candidates = [_json_value(asdict(item)) for item in report.candidates]
    formal_actions = [
        _json_value(asdict(item))
        for item in report.holdings
        if item.action == "SELL_ALL"
    ]
    formal_actions.extend(
        {
            **_json_value(asdict(item)),  # type: ignore[arg-type]
            "action": "BUY",
            "valid_window": f"{report.execution_date} 09:30–10:00",
        }
        for item in report.buy_actions
    )
    payload = {
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "as_of_date": report.as_of_date,
        "execution_date": report.execution_date,
        "account": _json_value(asdict(report.account)),
        "api_facts": list(report.api_facts),
        "strategy_judgments": {
            "holding_decisions": holding_decisions,
            "top10_candidates": top10_candidates,
            "formal_actions": formal_actions,
        },
        "industry_concentration": _json_value(report.industry_concentration),
        "excluded": report.excluded,
        "data_sources": list(report.data_sources),
        "estimated_api_cost": _json_value(report.estimated_api_cost),
        "actual_api_cost": _json_value(report.actual_api_cost),
        "protection_state": report.protection_state,
        "signal_snapshots": _json_value(report.signal_snapshots),
        "metadata": _json_value(report.metadata),
        "disclaimer": DISCLAIMER_TEXT,
    }
    for key in ("delivery_status", "process_version"):
        value = report.metadata.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    if not formal_actions:
        payload["no_action"] = NO_ACTION_TEXT
    return payload


def load_protection_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "positions": {}}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("protection state is unreadable or malformed") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("protection state has an invalid schema")
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("protection state positions must be an object")
    for symbol, state in positions.items():
        if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("protection state symbol must be six digits")
        if not isinstance(state, dict):
            raise ValueError(f"protection state for {symbol} must be an object")
        if _optional_decimal(state.get("initial_line")) is None or _optional_decimal(
            state.get("active_line")
        ) is None:
            raise ValueError(f"protection state for {symbol} has no active line")
        _optional_decimal(state.get("atr14"))
        tracking_active = state.get("tracking_active")
        if tracking_active is not None and not isinstance(tracking_active, bool):
            raise ValueError(f"protection state for {symbol} has invalid tracking state")
        position_started_for = state.get("position_started_for")
        if position_started_for is not None and not isinstance(position_started_for, str):
            raise ValueError(f"protection state for {symbol} has invalid start date")
        if not isinstance(state.get("updated_for"), str):
            raise ValueError(f"protection state for {symbol} has no update date")
    return payload


def write_protection_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent
        ) as handle:
            json.dump(
                _json_value(dict(state)),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_watch_events(path: Path) -> tuple[dict[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError):
        raise ValueError("watch events are unreadable") from None
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"watch event line {line_number} is malformed") from None
        if not isinstance(event, dict):
            raise ValueError(f"watch event line {line_number} is not an object")
        events.append(event)
    return tuple(events)


def write_frozen_report(
    report: TrendReport, reports_dir: Path, revision: bool = False
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = report.as_of_date
    if revision:
        revision_number = 1
        while (reports_dir / f"{stem}-r{revision_number}.md").exists() or (
            reports_dir / f"{stem}-r{revision_number}.json"
        ).exists():
            revision_number += 1
        stem = f"{stem}-r{revision_number}"
    markdown_path = reports_dir / f"{stem}.md"
    json_path = reports_dir / f"{stem}.json"
    if not revision and markdown_path.exists() and json_path.exists():
        json.loads(json_path.read_text(encoding="utf-8"))
        markdown_path.read_text(encoding="utf-8")
        return markdown_path, json_path

    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_backup: Path | None = None
    json_backup: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=reports_dir
        ) as handle:
            handle.write(render_markdown(report))
            markdown_temp = Path(handle.name)
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=reports_dir
        ) as handle:
            json.dump(
                _report_payload(report),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            json_temp = Path(handle.name)
        if markdown_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(markdown_path.read_bytes())
                markdown_backup = Path(handle.name)
        if json_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(json_path.read_bytes())
                json_backup = Path(handle.name)
        try:
            markdown_temp.replace(markdown_path)
            json_temp.replace(json_path)
        except Exception as replace_error:
            rollback_error: Exception | None = None
            for final_path, backup_path in (
                (markdown_path, markdown_backup),
                (json_path, json_backup),
            ):
                try:
                    if backup_path is None:
                        final_path.unlink(missing_ok=True)
                    else:
                        backup_path.replace(final_path)
                except Exception as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise rollback_error from replace_error
            raise
        return markdown_path, json_path
    finally:
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
        if markdown_backup is not None:
            markdown_backup.unlink(missing_ok=True)
        if json_backup is not None:
            json_backup.unlink(missing_ok=True)


def _process_version(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _redact_api_key(value: object, secret: str) -> str:
    text = str(value)
    return text.replace(secret, "<redacted>") if secret else text


def _write_run_log(path: Path, payload: Mapping[str, object], *, append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def _payload_hashes(
    markdown: str,
    report_json: str,
    protection_state: Mapping[str, object] | None = None,
) -> dict[str, str]:
    markdown_bytes = markdown.encode("utf-8")
    json_bytes = report_json.encode("utf-8")
    payload = {
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "json_sha256": hashlib.sha256(json_bytes).hexdigest(),
    }
    content = markdown_bytes + b"\0" + json_bytes
    if protection_state is not None:
        state_bytes = json.dumps(
            protection_state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload["protection_state_sha256"] = hashlib.sha256(state_bytes).hexdigest()
        content += b"\0" + state_bytes
    payload["content_hash"] = hashlib.sha256(content).hexdigest()
    return payload


def _write_delivery_receipt(
    path: Path,
    *,
    status: str,
    generated_at: str,
    artifact_stem: str,
    markdown: str,
    report_json: str,
    protection_state: Mapping[str, object],
) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen_state = json.loads(
        json.dumps(protection_state, ensure_ascii=False, sort_keys=True)
    )
    payload = {
        "status": status,
        "generated_at": generated_at,
        "artifact_stem": artifact_stem,
        "markdown": markdown,
        "report_json": report_json,
        "protection_state": frozen_state,
        **_payload_hashes(markdown, report_json, frozen_state),
    }
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return payload


def _read_delivery_receipt(
    path: Path,
    *,
    artifact_stem: str,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("delivery receipt is unreadable or malformed") from None
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {
        "prepared", "pending", "sent", "delivery_failed", "delivery_unknown"
    }:
        raise ValueError("delivery receipt has an invalid status")
    if payload.get("artifact_stem") != artifact_stem:
        raise ValueError("delivery receipt artifact stem mismatch")
    markdown = payload.get("markdown")
    report_json = payload.get("report_json")
    protection_state = payload.get("protection_state")
    if not isinstance(markdown, str) or not isinstance(report_json, str):
        raise ValueError("delivery receipt has no embedded report payload")
    if not isinstance(protection_state, dict):
        raise ValueError("delivery receipt has no embedded protection state")
    try:
        report_payload = json.loads(report_json)
    except json.JSONDecodeError:
        raise ValueError("delivery receipt report JSON is malformed") from None
    if not isinstance(report_payload, dict):
        raise ValueError("delivery receipt report JSON must be an object")
    hashes = _payload_hashes(markdown, report_json, protection_state)
    if any(payload.get(key) != value for key, value in hashes.items()):
        raise ValueError("delivery receipt content hash mismatch")
    return payload


def _transition_delivery_receipt(
    path: Path,
    receipt: Mapping[str, object],
    *,
    status: str,
    delivery_status: str,
) -> dict[str, object]:
    payload = json.loads(str(receipt["report_json"]))
    if not isinstance(payload, dict):
        raise ValueError("delivery receipt report JSON must be an object")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    metadata["delivery_status"] = delivery_status
    payload["delivery_status"] = delivery_status
    return _write_delivery_receipt(
        path,
        status=status,
        generated_at=str(receipt["generated_at"]),
        artifact_stem=str(receipt["artifact_stem"]),
        markdown=str(receipt["markdown"]),
        report_json=(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ),
        protection_state=receipt["protection_state"],  # type: ignore[arg-type]
    )


def _freeze_receipt_report(
    *,
    receipt: Mapping[str, object],
    reports_dir: Path,
    artifact_stem: str,
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / f"{artifact_stem}.md"
    json_path = reports_dir / f"{artifact_stem}.json"
    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_backup: Path | None = None
    json_backup: Path | None = None
    try:
        with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
            handle.write(str(receipt["markdown"]).encode("utf-8"))
            markdown_temp = Path(handle.name)
        with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
            handle.write(str(receipt["report_json"]).encode("utf-8"))
            json_temp = Path(handle.name)
        if markdown_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(markdown_path.read_bytes())
                markdown_backup = Path(handle.name)
        if json_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(json_path.read_bytes())
                json_backup = Path(handle.name)
        try:
            markdown_temp.replace(markdown_path)
            json_temp.replace(json_path)
        except Exception:
            for final_path, backup_path in (
                (markdown_path, markdown_backup),
                (json_path, json_backup),
            ):
                if backup_path is None:
                    final_path.unlink(missing_ok=True)
                else:
                    backup_path.replace(final_path)
            raise
        return markdown_path, json_path
    finally:
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
        if markdown_backup is not None:
            markdown_backup.unlink(missing_ok=True)
        if json_backup is not None:
            json_backup.unlink(missing_ok=True)


def _artifact_stem(
    *, run_date: str, revision: bool, reports_dir: Path, data_dir: Path
) -> str:
    if not revision:
        return run_date
    number = 1
    while True:
        stem = f"{run_date}-r{number}"
        receipt_path = _receipt_path(data_dir, stem)
        markdown_path = reports_dir / f"{stem}.md"
        json_path = reports_dir / f"{stem}.json"
        if _legacy_sent_pair_matches(
            receipt_path, stem, markdown_path, json_path
        ):
            number += 1
            continue
        receipt = _read_delivery_receipt(receipt_path, artifact_stem=stem)
        if receipt is not None:
            if receipt["status"] != "sent" or not _final_pair_matches(
                receipt, markdown_path, json_path
            ):
                return stem
        elif markdown_path.exists() and json_path.exists():
            markdown_path.read_text(encoding="utf-8")
            json.loads(json_path.read_text(encoding="utf-8"))
        else:
            return stem
        number += 1


def _receipt_path(data_dir: Path, artifact_stem: str) -> Path:
    return data_dir / "trend_a_share/delivery" / f"{artifact_stem}.json"


def _final_pair_matches(
    receipt: Mapping[str, object], markdown_path: Path, json_path: Path
) -> bool:
    try:
        return (
            markdown_path.read_text(encoding="utf-8") == receipt["markdown"]
            and json_path.read_text(encoding="utf-8") == receipt["report_json"]
        )
    except (OSError, UnicodeError):
        return False


def _legacy_sent_pair_matches(
    receipt_path: Path,
    artifact_stem: str,
    markdown_path: Path,
    json_path: Path,
) -> bool:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        report_json = json_path.read_text(encoding="utf-8")
        json.loads(report_json)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict) or any(
        key in receipt for key in ("markdown", "report_json")
    ):
        return False
    return (
        receipt.get("status") == "sent"
        and receipt.get("artifact_stem") == artifact_stem
        and all(
            receipt.get(key) == value
            for key, value in _payload_hashes(markdown, report_json).items()
        )
    )


def _notify_status(notifier: Notifier, title: str, message: str) -> None:
    send_notification_with_results(
        notifier,
        title,
        message,
        channels={"macos"},
    )


def _notify_delivery_status(
    notifier: Notifier, *, run_date: str, delivery_status: str
) -> None:
    if delivery_status in {"sent", "sent_prior_attempt"}:
        title = "A股趋势计划已生成"
    elif delivery_status == "delivery_unknown":
        title = "A股趋势计划交付状态未知"
    else:
        title = "A股趋势计划发送失败"
    _notify_status(
        notifier,
        title,
        f"{run_date} 本地报告已冻结；飞书状态：{delivery_status}",
    )


def _recover_receipt_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    artifact_stem: str,
    notifier: Notifier,
) -> AShareTrendRunResult | None:
    receipt_path = _receipt_path(config.data_dir, artifact_stem)
    receipt = _read_delivery_receipt(
        receipt_path,
        artifact_stem=artifact_stem,
    )
    if receipt is None:
        return None
    prior_status = str(receipt["status"])
    if prior_status in {"prepared", "delivery_failed"}:
        if prior_status == "prepared":
            write_protection_state(
                config.data_dir / "trend_a_share/protection_state.json",
                receipt["protection_state"],  # type: ignore[arg-type]
            )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="pending",
            delivery_status="pending",
        )
        attempts = send_notification_with_results(
            notifier,
            f"A股趋势操作计划 · {run_date}",
            str(receipt["markdown"]),
            channels={"feishu", "feishu_app"},
        )
        delivery_status = (
            "sent"
            if any(
                item.channel.startswith("feishu") and item.success
                for item in attempts
            )
            else "delivery_failed"
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=delivery_status,
            delivery_status=delivery_status,
        )
    elif prior_status == "sent":
        delivery_status = "sent_prior_attempt"
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="sent",
            delivery_status=delivery_status,
        )
    else:
        delivery_status = "delivery_unknown"
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="delivery_unknown",
            delivery_status=delivery_status,
        )
    markdown_path, json_path = _freeze_receipt_report(
        receipt=receipt,
        reports_dir=config.reports_dir / "trend_a_share",
        artifact_stem=artifact_stem,
    )
    _notify_delivery_status(
        notifier,
        run_date=run_date,
        delivery_status=delivery_status,
    )
    return AShareTrendRunResult("generated", markdown_path, json_path)


def _status_date(row: Mapping[str, object]) -> str:
    for key in ("asOfDate", "updateDate", "latestDate", "date"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _updates_ready(rows: Sequence[Mapping[str, object]], run_date: str) -> bool:
    dates = {
        row.get("asset"): _status_date(row)
        for row in rows
        if row.get("asset") in {"A股", "ETF基金"}
    }
    return dates == {"A股": run_date, "ETF基金": run_date}


def _row_tm_id(row: Mapping[str, object]) -> int:
    value = row.get("tmId")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrendAnimalsError("Trend Animals returned an invalid tmId")
    return value


def _billing_field(row: Mapping[str, object]) -> str:
    for key in ("field", "fieldName", "column", "columnName"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _billing_price(row: Mapping[str, object]) -> Decimal:
    for key in ("priceCost", "price", "cost", "unitPrice", "billing"):
        if key in row:
            try:
                value = _decimal(row[key])
            except ValueError:
                raise TrendAnimalsError("snapshot billing returned an invalid price") from None
            if value < 0:
                raise TrendAnimalsError("snapshot billing returned a negative price")
            return value
    raise TrendAnimalsError("snapshot billing returned no price")


def _is_systemic_futu_error(exc: FutuQuoteError) -> bool:
    return exc.error_type in {
        "opend_unreachable",
        "context_failed",
        "quote_server_interrupted",
    }


def _balance(row: Mapping[str, object]) -> Decimal:
    for key in ("balance", "remainingBalance", "amount"):
        if key in row:
            try:
                return _decimal(row[key])
            except ValueError:
                break
    raise TrendAnimalsError("getAccountBalance returned no valid balance")


def _holding_snapshot(row: Mapping[str, object]) -> HoldingSnapshot:
    symbol, exchange = _symbol_parts(row.get("tickerSymbol"))
    return HoldingSnapshot(
        tm_id=_row_tm_id(row),
        symbol=symbol,
        exchange=exchange,
        name=str(row.get("tickerName") or "").strip(),
        as_of_date=str(row.get("asOfDate") or "").strip(),
        right_side=(
            row.get("isTrendRightSide")
            if isinstance(row.get("isTrendRightSide"), bool)
            else None
        ),
        danger=(
            row.get("stopwinFlagByDangerSignal")
            if isinstance(row.get("stopwinFlagByDangerSignal"), bool)
            else None
        ),
        boiling=(
            row.get("stopwinFlagByBoilingTemperature")
            if isinstance(row.get("stopwinFlagByBoilingTemperature"), bool)
            else None
        ),
        champagne=(
            row.get("stopwinFlagByPopChampagne")
            if isinstance(row.get("stopwinFlagByPopChampagne"), bool)
            else None
        ),
        industry=str(row.get("industryName") or "").strip(),
    )


def _attempt_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    artifact_stem: str,
    process_version: str,
    api_factory: Callable[..., object],
    quote_factory: Callable[..., object],
    notifier: Notifier,
) -> AShareTrendRunResult:
    run_day = date.fromisoformat(run_date)
    quote = quote_factory(host=config.futu_host, port=config.futu_port)
    try:
        calendar = quote.get_cn_trading_days(
            start=run_date,
            end=(run_day + timedelta(days=14)).isoformat(),
        )
        if run_date not in calendar:
            return AShareTrendRunResult("holiday", None, None)
        execution_dates = sorted(item for item in calendar if item > run_date)
        if not execution_dates:
            raise FutuQuoteError("Futu CN calendar has no later trading day")
        execution_date = execution_dates[0]

        api = api_factory(
            api_key=config.trend_animals_api_key,
            cache_dir=config.data_dir / "trend_animals/cache",
        )
        update_rows = api.get_update_status()
        if not _updates_ready(update_rows, run_date):
            return AShareTrendRunResult("waiting", None, None)

        balance_before = _balance(api.get_account_balance())
        component_rows = []
        component_pools: defaultdict[int, set[str]] = defaultdict(set)
        for tm_id in (
            config.trend_animals_a_share_tm_id,
            config.trend_animals_etf_tm_id,
        ):
            rows = api.get_components(tm_id=tm_id, expected_date=run_date)
            component_rows.extend(rows)
            for row in rows:
                component_pools[_row_tm_id(row)].add(str(tm_id))
        component_ids = {_row_tm_id(row) for row in component_rows}

        account = load_eastmoney_account(
            config.portfolio,
            expected_date=run_date,
            timezone=ZoneInfo(config.timezone),
        )
        holding_ids: dict[str, int] = {}
        for position in account.positions:
            try:
                holding_ids[position.symbol] = api.search_exact_symbol(position.symbol)
            except TrendAnimalsLookupError:
                continue

        requested_ids = sorted(component_ids | set(holding_ids.values()))
        fields = HOLDING_FIELDS if holding_ids else CANDIDATE_FIELDS
        billing_rows = api.get_snapshot_billing()
        billing = {_billing_field(row): row for row in billing_rows}
        missing_billing = [field for field in fields if field not in billing]
        if missing_billing:
            raise TrendAnimalsError(
                "getSnapshotColumnBilling missing requested field(s): "
                + ", ".join(missing_billing)
            )
        snapshot_rows = (
            api.get_snapshots(
                tm_ids=requested_ids,
                fields=fields,
                expected_date=run_date,
            )
            if requested_ids
            else []
        )
        returned_ids = [_row_tm_id(row) for row in snapshot_rows]
        if len(returned_ids) != len(set(returned_ids)) or sorted(
            returned_ids
        ) != requested_ids:
            raise TrendAnimalsError("getTickerSnapshot returned mismatched tmIds")
        if any(row.get("asOfDate") != run_date for row in snapshot_rows):
            raise TrendAnimalsError("getTickerSnapshot returned a stale data date")
        balance_after = _balance(api.get_account_balance())

        candidates: list[CandidateInput] = []
        holding_snapshots: dict[str, HoldingSnapshot | None] = {
            position.symbol: None for position in account.positions
        }
        rows_by_tm_id = {_row_tm_id(row): row for row in snapshot_rows}
        kline_start = (run_day - timedelta(days=60)).isoformat()
        bars_by_symbol: dict[str, Sequence[DailyKlineBar] | None] = {}
        for tm_id in sorted(component_ids):
            row = rows_by_tm_id.get(tm_id)
            if row is None:
                continue
            try:
                symbol, exchange = _symbol_parts(row.get("tickerSymbol"))
                daily_bars = quote.get_daily_kline(
                    f"{exchange}.{symbol}", start=kline_start, end=run_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                daily_bars = None
            except ValueError:
                daily_bars = None
            candidates.append(
                evaluate_candidate(
                    row,
                    daily_bars,
                    pools=component_pools[tm_id],
                )
            )
        for symbol, tm_id in holding_ids.items():
            row = rows_by_tm_id.get(tm_id)
            if row is not None:
                try:
                    holding_snapshots[symbol] = _holding_snapshot(row)
                except ValueError:
                    holding_snapshots[symbol] = None
            try:
                returned = holding_snapshots[symbol]
                futu_symbol = (
                    f"{returned.exchange}.{symbol}"
                    if returned is not None
                    else to_futu_symbol("CN", symbol)
                )
                bars_by_symbol[symbol] = quote.get_daily_kline(
                    futu_symbol, start=kline_start, end=run_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                bars_by_symbol[symbol] = None
            except ValueError:
                bars_by_symbol[symbol] = None

        estimated_cost = sum(
            (_billing_price(billing[field]) for field in fields), Decimal("0")
        ) * len(requested_ids)
        balance_delta = balance_before - balance_after
        actual_cost = balance_delta if balance_delta >= 0 else None
        cache_events = tuple(getattr(api, "paid_cache_events", ()))
        cache_metadata = {
            "hits": sum(event.get("cache") == "hit" for event in cache_events),
            "misses": sum(event.get("cache") == "miss" for event in cache_events),
            "events": [dict(event) for event in cache_events],
        }
        report = build_report(
            as_of_date=run_date,
            execution_date=execution_date,
            account=account,
            candidates=candidates,
            holding_snapshots=holding_snapshots,
            bars_by_symbol=bars_by_symbol,
            prior_state=load_protection_state(
                config.data_dir / "trend_a_share/protection_state.json"
            ),
            watch_events=load_watch_events(
                config.data_dir / "trend_a_share/watch_events.jsonl"
            ),
            api_facts=(
                f"getUpdateStatus rows={len(update_rows)}",
                f"getComponentTicker rows={len(component_rows)} cache=client-managed",
                f"getTickerSnapshot fields={','.join(fields)} rows={len(snapshot_rows)} cache=client-managed",
            ),
            data_sources=("Trend Animals", "Futu CN calendar/QFQ daily K-line", str(config.portfolio)),
            estimated_api_cost=estimated_cost,
            actual_api_cost=actual_cost,
            metadata={"paid_response_cache": cache_metadata},
        )
        report = replace(
            report,
            metadata={
                **report.metadata,
                "delivery_status": "prepared",
                "process_version": process_version,
            },
        )
        receipt_path = _receipt_path(config.data_dir, artifact_stem)
        receipt = _write_delivery_receipt(
            receipt_path,
            status="prepared",
            generated_at=report.generated_at,
            artifact_stem=artifact_stem,
            markdown=render_markdown(report),
            report_json=(
                json.dumps(
                    _report_payload(report),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ),
            protection_state=report.protection_state,
        )
        write_protection_state(
            config.data_dir / "trend_a_share/protection_state.json",
            report.protection_state,
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="pending",
            delivery_status="pending",
        )
        attempts = send_notification_with_results(
            notifier,
            f"A股趋势操作计划 · {report.as_of_date}",
            str(receipt["markdown"]),
            channels={"feishu", "feishu_app"},
        )
        delivery_status = (
            "sent"
            if any(item.channel.startswith("feishu") and item.success for item in attempts)
            else "delivery_failed"
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=delivery_status,
            delivery_status=delivery_status,
        )
        markdown_path, json_path = _freeze_receipt_report(
            receipt=receipt,
            reports_dir=config.reports_dir / "trend_a_share",
            artifact_stem=artifact_stem,
        )
        _notify_delivery_status(
            notifier,
            run_date=run_date,
            delivery_status=delivery_status,
        )
        return AShareTrendRunResult("generated", markdown_path, json_path)
    finally:
        close = getattr(quote, "close", None)
        if callable(close):
            close()


def run_a_share_trend_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    revision: bool = False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    sleep_fn: Callable[[float], None] = sleep,
    api_factory: Callable[..., object] = TrendAnimalsClient,
    quote_factory: Callable[..., object] = FutuQuoteClient,
    notifier: Notifier | None = None,
) -> AShareTrendRunResult:
    run_day = date.fromisoformat(run_date)
    notifier = notifier or NullNotifier()
    if config.trend_animals_a_share_tm_id != 622466:
        raise ValueError("TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID must be 622466")
    if config.trend_animals_etf_tm_id != 697199:
        raise ValueError("TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID must be 697199")
    report_dir = config.reports_dir / "trend_a_share"
    base_markdown = report_dir / f"{run_date}.md"
    base_json = report_dir / f"{run_date}.json"
    with RunLock(config.data_dir / "runs/.trend_a_share_report.lock"):
        artifact_stem = _artifact_stem(
            run_date=run_date,
            revision=revision,
            reports_dir=report_dir,
            data_dir=config.data_dir,
        )
        if not revision and _legacy_sent_pair_matches(
            _receipt_path(config.data_dir, artifact_stem),
            artifact_stem,
            base_markdown,
            base_json,
        ):
            return AShareTrendRunResult("existing", base_markdown, base_json)
        receipt_path = _receipt_path(config.data_dir, artifact_stem)
        receipt = _read_delivery_receipt(
            receipt_path,
            artifact_stem=artifact_stem,
        )
        if not revision and base_markdown.exists() and base_json.exists():
            if receipt is None:
                base_markdown.read_text(encoding="utf-8")
                json.loads(base_json.read_text(encoding="utf-8"))
                return AShareTrendRunResult("existing", base_markdown, base_json)
            if receipt["status"] == "sent" and _final_pair_matches(
                receipt, base_markdown, base_json
            ):
                return AShareTrendRunResult("existing", base_markdown, base_json)
        recovered = _recover_receipt_report(
            config=config,
            run_date=run_date,
            artifact_stem=artifact_stem,
            notifier=notifier,
        )
        if recovered is not None:
            return recovered
        version = _process_version(config.repo)
        log_path = config.logs_dir / "trend_a_share" / f"{run_date}.log"
        deadline = datetime.combine(run_day, time(18, 0), tzinfo=SHANGHAI)
        notified_waiting = False
        last_error = "Trend Animals update status is not ready"
        _write_run_log(
            log_path,
            {"event": "start", "process_version": version, "run_date": run_date},
            append=False,
        )
        while True:
            try:
                attempt = _attempt_report(
                    config=config,
                    run_date=run_date,
                    artifact_stem=artifact_stem,
                    process_version=version,
                    api_factory=api_factory,
                    quote_factory=quote_factory,
                    notifier=notifier,
                )
                if attempt.status in {"generated", "existing", "holiday"}:
                    return attempt
                last_error = "Trend Animals update status is not ready"
            except (TrendAnimalsError, FutuQuoteError) as exc:
                last_error = _redact_api_key(exc, config.trend_animals_api_key)
            _write_run_log(
                log_path,
                {"event": "retry", "error": last_error, "run_date": run_date},
                append=True,
            )
            now = now_fn()
            if now >= deadline:
                _notify_status(notifier, "A股趋势计划失败", last_error)
                return AShareTrendRunResult("failed", None, None)
            if not notified_waiting:
                _notify_status(notifier, "A股趋势数据等待中", last_error)
                notified_waiting = True
            sleep_fn(min(600.0, max(1.0, (deadline - now).total_seconds())))
