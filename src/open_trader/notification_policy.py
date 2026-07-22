from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


MARKET_LABELS = {"CN": "A股", "HK": "港股", "US": "美股"}
BROKER_LABELS = {"CN": "东方财富", "HK": "辉立", "US": "老虎"}
SIDE_LABELS = {"buy": "买入", "sell": "卖出"}
STATUS_LABELS = {
    "pending": "待执行",
    "submitted": "已提交",
    "partially_filled": "部分成交",
    "failed": "失败",
    "blocked": "受阻",
    "uncertain": "状态不确定",
    "conflict": "订单冲突",
    "missed": "错过窗口",
    "missed_window": "错过窗口",
    "incomplete": "未完成",
}
STATUS_ACTIONS = {
    "pending": "检查阻塞原因并人工决定是否继续",
    "submitted": "检查订单是否仍在等待成交",
    "partially_filled": "检查剩余数量并人工决定是否撤单",
    "failed": "检查订单失败原因后人工处理",
    "blocked": "解除阻塞前不要自动重试",
    "uncertain": "核对不可变账本与券商订单，禁止自动重试",
    "conflict": "核对冲突订单，禁止自动提交",
    "missed": "今日不再追单",
    "missed_window": "今日不再追单",
    "incomplete": "核对券商订单并人工处理",
}
REASON_LABELS = {
    "window closed": "窗口已关闭",
    "buy_window_closed": "买入窗口已关闭",
    "broker order conflicts with immutable intent": "券商订单与不可变账本冲突",
    "position_zero_confirmed": "持仓数量已确认为零",
}


@dataclass(frozen=True)
class OrderAlertItem:
    symbol: str
    filled_qty: str = ""
    target_qty: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OrderAlertGroup:
    market: str
    side: str
    status: str
    items: tuple[OrderAlertItem, ...]


def group_order_alerts(
    market: str, events: Iterable[Mapping[str, object]]
) -> list[OrderAlertGroup]:
    normalized_market = market.strip().upper()
    grouped: dict[tuple[str, str], dict[str, OrderAlertItem]] = {}
    for event in events:
        symbol = str(event.get("symbol") or "").strip().upper()
        side = str(event.get("side") or "").strip().lower()
        status = str(event.get("status") or "").strip().lower()
        if not symbol or side not in SIDE_LABELS or status not in STATUS_LABELS:
            continue
        grouped.setdefault((side, status), {})[symbol] = OrderAlertItem(
            symbol=symbol,
            filled_qty=str(event.get("filled_qty") or "").strip(),
            target_qty=str(event.get("target_qty") or "").strip(),
            reason=_reason_label(event.get("reason")),
        )
    return [
        OrderAlertGroup(
            market=normalized_market,
            side=side,
            status=status,
            items=tuple(items[symbol] for symbol in sorted(items)),
        )
        for (side, status), items in sorted(grouped.items())
    ]


def _reason_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in REASON_LABELS:
        return REASON_LABELS[text]
    return "详见动作账本"


def brief_zh_detail(value: object) -> str:
    raw = str(value or "").strip()
    text = raw.splitlines()[0] if raw else ""
    if not text:
        return ""
    if (
        "/Users/" in raw
        or "/private/" in raw
        or any(character.isascii() and character.isalpha() for character in text)
        or not any("\u4e00" <= character <= "\u9fff" for character in text)
    ):
        return "详见控制器日志"
    return text[:160]


def _numeric_detail(value: object) -> str:
    text = str(value).strip()
    try:
        return text if len(text) <= 64 and Decimal(text).is_finite() else "详见控制器日志"
    except InvalidOperation:
        return "详见控制器日志"


def render_daily_title(broker_label: str, market_label: str, report_date: str) -> str:
    return f"【日报｜{broker_label}｜{market_label}趋势报告｜{report_date}】"


def render_attention(
    source: str,
    problem: str,
    event_date: str,
    *,
    happened: str,
    impact: str,
    action: str,
    detail: str = "",
) -> tuple[str, str]:
    lines = [f"发生：{happened}", f"影响：{impact}", f"现在做：{action}"]
    if detail:
        lines.append(f"原因：{brief_zh_detail(detail)}")
    return f"【需处理｜{source}｜{problem}｜{event_date}】", "\n".join(lines)


def render_protection_alert(
    broker_label: str,
    market_label: str,
    symbol: str,
    *,
    last_price: object,
    active_line: object,
) -> tuple[str, str]:
    return (
        f"【紧急｜{broker_label}｜{market_label}保护线触发｜{symbol}】",
        f"最新价：{_numeric_detail(last_price)}\n"
        f"活动保护线：{_numeric_detail(active_line)}\n现在做：人工确认并全部卖出",
    )


def render_order_alert(
    group: OrderAlertGroup, *, broker_label: str, trading_date: str
) -> tuple[str, str]:
    market_label = MARKET_LABELS[group.market]
    status_label = STATUS_LABELS[group.status]
    problem = f"{market_label}{SIDE_LABELS[group.side]}{status_label}"
    lines = [
        f"发生：{len(group.items)} 个标的订单{status_label}",
        f"影响：{market_label}{SIDE_LABELS[group.side]}订单需要人工确认",
        f"现在做：{STATUS_ACTIONS[group.status]}",
        "",
        "标的：",
    ]
    for item in group.items:
        details: list[str] = []
        if item.filled_qty and item.target_qty:
            details.append(f"成交 {item.filled_qty}/{item.target_qty}")
        elif item.target_qty:
            details.append(f"目标 {item.target_qty}")
        if item.reason:
            details.append(f"原因：{item.reason}")
        suffix = f"｜{'｜'.join(details)}" if details else ""
        lines.append(f"- {item.symbol}{suffix}")
    return f"【需处理｜{broker_label}｜{problem}｜{trading_date}】", "\n".join(lines)
