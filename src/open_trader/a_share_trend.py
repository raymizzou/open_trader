from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from .kline_technical_facts import DailyKlineBar


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
    if len(parts) != 2 or len(parts[0]) != 6 or not parts[0].isdigit() or not parts[1]:
        raise ValueError(f"invalid tickerSymbol: {value!r}")
    return parts[0], parts[1]


def evaluate_candidate(
    row: Mapping[str, object], bars: Sequence[DailyKlineBar] | None
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
    )


def _excluded_name(name: str) -> bool:
    normalized = name.strip().upper()
    return normalized.startswith("ST") or normalized.startswith("*ST") or "退" in name


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
    if item.symbol in held_symbols:
        reasons.append("already_held")
    if item.exchange == "BJ" or _excluded_name(item.name):
        reasons.append("excluded_security")
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
        remaining_cash -= item.close * shares
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
    if snapshot is None or snapshot.right_side is None or snapshot.danger is None:
        return "MANUAL_REVIEW", "holding_signal_unknown"
    if snapshot.danger is True:
        return "SELL_ALL", "danger_signal"
    if snapshot.right_side is False:
        return "SELL_ALL", "left_trend_right_side"
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
        effective_atr = current_atr or old_atr
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
    return TrendReport(
        schema_version=1,
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
    )


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
        lines.append("现金也是有效仓位，本日无需交易。")
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
        lines.append(f"- {symbol}：{', '.join(reasons)}")
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
            "本报告是确定性纪律清单，不是订单或成交事实；所有交易由用户人工确认与执行。",
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
                _json_value(asdict(report)),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            json_temp = Path(handle.name)
        markdown_temp.replace(markdown_path)
        json_temp.replace(json_path)
        return markdown_path, json_path
    finally:
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
