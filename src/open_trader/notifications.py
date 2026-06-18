from __future__ import annotations

import csv
import json
import subprocess
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol


class NotificationError(RuntimeError):
    pass


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        pass


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class MacOSNotifier:
    def notify(self, title: str, message: str) -> None:
        script = (
            f'display notification "{_escape_osascript(message)}" '
            f'with title "{_escape_osascript(title)}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def notify(self, title: str, message: str) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(title, message)
            except Exception:
                continue


PostJson = Callable[[str, dict[str, object], float], dict[str, object]]
PostJsonWithHeaders = Callable[
    [str, dict[str, object], dict[str, str], float],
    dict[str, object],
]


class FeishuWebhookNotifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        post_json: PostJson | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url
        self._post_json = post_json or _post_json
        self.timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": f"{title}\n\n{message}"},
        }
        response = self._post_json(self.webhook_url, payload, self.timeout_seconds)
        if "code" in response:
            code = response.get("code")
        elif "StatusCode" in response:
            code = response.get("StatusCode")
        else:
            raise NotificationError("Feishu webhook error missing: ")

        if code not in {0, "0"}:
            message = response.get("msg") or response.get("StatusMessage") or ""
            raise NotificationError(f"Feishu webhook error {code}: {message}")


class FeishuAppNotifier:
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        receive_id_type: str,
        receive_id: str,
        post_json: PostJsonWithHeaders | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id_type = receive_id_type
        self.receive_id = receive_id
        self._post_json = post_json or _post_json_with_headers
        self.timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        token = self._tenant_access_token()
        response = self._post_json(
            (
                "https://open.feishu.cn/open-apis/im/v1/messages"
                f"?receive_id_type={self.receive_id_type}"
            ),
            {
                "receive_id": self.receive_id,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": f"{title}\n\n{message}"},
                    ensure_ascii=False,
                ),
            },
            {"Authorization": f"Bearer {token}"},
            self.timeout_seconds,
        )
        code = response.get("code")
        if code not in {0, "0"}:
            raise NotificationError(
                f"Feishu message error {code}: {response.get('msg', '')}"
            )

    def _tenant_access_token(self) -> str:
        response = self._post_json(
            self.token_url,
            {"app_id": self.app_id, "app_secret": self.app_secret},
            {},
            self.timeout_seconds,
        )
        code = response.get("code")
        if code not in {0, "0"}:
            raise NotificationError(
                f"Feishu token error {code}: {response.get('msg', '')}"
            )
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise NotificationError("Feishu token error missing: tenant_access_token")
        return token


def render_feishu_order_review(
    *,
    run_date: str,
    status: str,
    actions_path: Path,
    report_paths: list[Path],
    max_ready_sections: int = 5,
) -> str:
    rows = _read_action_rows(actions_path)
    ready_rows = [row for row in rows if _effective_status(row) == "ready"]
    review_rows = [row for row in rows if _effective_status(row) == "review"]
    watch_rows = [row for row in rows if _effective_status(row) == "watch"]

    lines = [
        "Open Trader｜行动通知",
        f"日期：{run_date}｜状态：{_status_label(status)}",
        "",
        _conclusion_line(ready_rows),
    ]

    if ready_rows:
        lines.extend(["", "可采取行动："])
        sorted_ready_rows = sorted(ready_rows, key=_priority_sort_key)
        for index, row in enumerate(sorted_ready_rows[:max_ready_sections], start=1):
            lines.extend(["", *_render_ready_section(row, index=index)])
        remaining_count = len(sorted_ready_rows) - max_ready_sections
        if remaining_count > 0:
            lines.append(f"- 另有 {remaining_count} 条可采取行动未展开。")

    if review_rows:
        lines.extend(["", "暂不能行动："])
        lines.append(f"- 另有 {len(review_rows)} 条需处理事项。")
        for row in sorted(review_rows, key=_priority_sort_key):
            lines.extend(["", *_render_blocked_section(row)])

    if watch_rows:
        lines.extend(["", f"观察中：{len(watch_rows)} 条动作等待触发。"])

    return "\n".join(lines).strip() + "\n"


def _conclusion_line(ready_rows: list[dict[str, str]]) -> str:
    if ready_rows:
        return f"今日结论：有 {len(ready_rows)} 条可采取行动，需人工确认后执行。"
    return "今日结论：暂无可采取行动。"


def _render_ready_section(row: Mapping[str, str], *, index: int) -> list[str]:
    missing_fields = _missing_precise_fields(row)
    action = (
        _action_label("REVIEW")
        if missing_fields
        else _action_label(row.get("action", "").strip())
    )
    quantity = row.get("suggested_quantity", "").strip()
    lines = [_action_heading(row, action=action, quantity=quantity, index=index)]

    if missing_fields:
        lines.extend(_blocked_detail_lines(row, missing_fields=missing_fields))
        return lines

    currency = _currency_label(row.get("notional_currency", "").strip())
    trigger_price = _trigger_price(row)
    lines.extend(
        [
            f"当前价：{row.get('last_price', '').strip()}",
            f"触发价：{trigger_price}",
            (
                f"预计金额：{currency} "
                f"{row.get('suggested_notional', '').strip()}"
            ),
            _ready_impact_text(row),
            _agent_reason_line(row),
        ]
    )
    risk_control = _risk_control_text(row, currency)
    if risk_control:
        lines.insert(-1, risk_control)
    excerpt_line = _agent_excerpt_line(row)
    if excerpt_line:
        lines.append(excerpt_line)
    trigger_line = _trigger_reason_line(row)
    if trigger_line:
        lines.append(trigger_line)
    return lines


def _render_blocked_section(row: Mapping[str, str]) -> list[str]:
    action = _action_label("REVIEW" if _row_status(row) == "ready" else "MANUAL")
    lines = [_action_heading(row, action=action, quantity="", index=None)]
    explicit_error = row.get("error", "").strip()
    if explicit_error:
        reason = _localized_note(explicit_error)
        lines.extend(
            [
                f"阻塞：{_sentence(reason)}",
                "影响：系统无法生成可直接执行的行动，请先处理该问题。",
            ]
        )
        return lines
    missing_fields = _missing_precise_fields(row)
    if missing_fields:
        lines.extend(_blocked_detail_lines(row, missing_fields=missing_fields))
        return lines
    reason = _localized_note(
        row.get("error", "").strip() or row.get("reason", "").strip()
    )
    lines.extend(
        [
            f"阻塞：{_sentence(reason)}",
            "影响：系统无法生成可直接执行的行动，请先处理该问题。",
        ]
    )
    return lines


def _action_heading(
    row: Mapping[str, str],
    *,
    action: str,
    quantity: str,
    index: int | None,
) -> str:
    prefix = f"{index}. " if index is not None else "- "
    quantity_text = f" {quantity} 股" if quantity else ""
    return (
        f"{prefix}标的：{_symbol_label(row)}｜指示：{action}{quantity_text}"
        f"｜优先级：{_priority_label(row.get('priority', '').strip())}"
    )


def _ready_impact_text(row: Mapping[str, str]) -> str:
    current_quantity = row.get("current_quantity", "").strip()
    current_weight = _display_percent(row.get("current_weight", "").strip())
    post_trade_quantity = row.get("post_trade_quantity", "").strip()
    post_trade_weight = _display_percent(row.get("post_trade_weight", "").strip())
    post_trade_avg_cost = row.get("post_trade_avg_cost", "").strip()
    post_cost_text = f"、成本 {post_trade_avg_cost}" if post_trade_avg_cost else ""
    return (
        "影响："
        f"当前数量 {current_quantity} 股、当前仓位 {current_weight}；"
        f"执行后数量 {post_trade_quantity} 股、仓位 {post_trade_weight}"
        f"{post_cost_text}。"
    )


def _risk_control_text(row: Mapping[str, str], currency: str) -> str:
    stop_price = row.get("stop_price", "").strip()
    risk_to_stop = _risk_to_stop_text(row, currency)
    parts: list[str] = []
    if stop_price:
        parts.append(f"硬止损 {stop_price}")
    if risk_to_stop:
        parts.append(f"止损风险 {risk_to_stop}")
    if not parts:
        return ""
    return f"风控：{'，'.join(parts)}。"


def _display_percent(value: str) -> str:
    stripped = value.strip()
    if not stripped.endswith("%"):
        return stripped
    number = stripped[:-1].strip()
    try:
        rounded = Decimal(number).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return stripped
    return f"{rounded}%"


def _blocked_detail_lines(
    row: Mapping[str, str],
    *,
    missing_fields: list[str],
) -> list[str]:
    return [
        f"阻塞：执行前缺少{'、'.join(_field_label(field) for field in missing_fields)}。",
        "影响：系统无法计算精确数量、金额、交易后仓位或风险，暂不能执行。",
        _agent_reason_line(row),
    ]


def _sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.endswith(("。", "！", "？", ".", "!", "?")):
        return stripped
    return f"{stripped}。"


def _symbol_label(row: Mapping[str, str]) -> str:
    symbol = row.get("symbol", "").strip()
    if symbol:
        return symbol
    futu_symbol = row.get("futu_symbol", "").strip()
    return futu_symbol.rsplit(".", 1)[-1] if futu_symbol else ""


def _status_label(status: str) -> str:
    return {
        "success": "成功",
        "partial": "部分完成",
        "failed": "失败",
    }.get(status.strip().lower(), status)


def _action_label(action: str) -> str:
    return {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "SELL_STOP": "止损卖出",
        "TAKE_PROFIT": "止盈卖出",
        "HOLD": "持有",
        "REVIEW": "人工复核",
        "MANUAL": "人工处理",
    }.get(action.strip().upper(), action)


def _priority_label(priority: str) -> str:
    return {
        "critical": "最高",
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(priority.strip().lower(), priority)


def _field_label(field: str) -> str:
    return {
        "last_price": "当前价",
        "current_quantity": "当前数量",
        "current_weight": "当前仓位",
        "avg_cost_price": "当前成本",
        "limit_price": "触发价",
        "suggested_quantity": "本次数量",
        "suggested_notional": "预计金额",
        "notional_currency": "金额币种",
        "post_trade_quantity": "交易后数量",
        "post_trade_weight": "交易后仓位",
        "post_trade_avg_cost": "交易后成本",
        "risk_to_stop": "止损风险",
        "stop_price": "硬止损",
        "reason": "原因",
    }.get(field, field)


def _currency_label(currency: str) -> str:
    return {
        "USD": "美元",
        "HKD": "港元",
        "CNY": "人民币",
        "CNH": "离岸人民币",
    }.get(currency.strip().upper(), currency)


def _agent_reason_line(row: Mapping[str, str]) -> str:
    agent_reason = row.get("agent_reason", "").strip()
    if agent_reason:
        concise_reason = _concise_agent_reason(row, agent_reason)
        return f"原因：{_sentence(concise_reason)}"
    fallback = _localized_note(row.get("reason", "").strip())
    if fallback:
        return f"原因：{_sentence(fallback)}"
    return "原因：原文依据缺失，需人工复核。"


def _agent_excerpt_line(row: Mapping[str, str]) -> str:
    excerpt = row.get("agent_excerpt", "").strip()
    if not excerpt:
        return ""
    return f"原文：{excerpt}"


def _missing_agent_reason_line(row: Mapping[str, str]) -> str:
    if row.get("agent_reason", "").strip():
        return ""
    return "原文依据缺失，需人工复核。"


def _trigger_reason_line(row: Mapping[str, str]) -> str:
    trigger_reason = row.get("trigger_reason", "").strip()
    if not trigger_reason:
        return ""
    action = row.get("action", "").strip().upper()
    last_price = row.get("last_price", "").strip()
    if action in {"TRIM", "TAKE_PROFIT", "SELL_STOP"} and trigger_reason:
        if action == "SELL_STOP":
            return f"触发：当前价 {last_price}，行动已满足计划中的止损条件。"
        return f"触发：当前价 {last_price}，行动已满足计划中的减仓/风控条件。"
    return f"触发：{_sentence(_localized_note(trigger_reason))}"


def _concise_agent_reason(row: Mapping[str, str], agent_reason: str) -> str:
    concise_reason = agent_reason.split("，原文依据：", 1)[0].strip()
    if concise_reason and _contains_cjk(concise_reason):
        return concise_reason
    if concise_reason and "TradingAgents" in concise_reason and not _looks_english_only(
        concise_reason
    ):
        return concise_reason
    return _agent_reason_fallback(row)


def _agent_reason_fallback(row: Mapping[str, str]) -> str:
    action = row.get("action", "").strip().upper()
    action_text = {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "SELL_STOP": "止损卖出",
        "TAKE_PROFIT": "止盈卖出",
        "HOLD": "持有",
    }.get(action, "处理")
    return f"TradingAgents建议{action_text}，需结合原文确认"


def _contains_ascii_letters(text: str) -> bool:
    return any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in text)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _looks_english_only(text: str) -> bool:
    return _contains_ascii_letters(text) and not _contains_cjk(text)


def _localized_note(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    translated = {
        "price entered entry zone": "价格进入计划买入区间。",
        "missing avg cost": "当前成本缺失。",
        "missing portfolio position for sell sizing": "缺少持仓信息，无法计算卖出数量。",
        "missing portfolio position for buy-side sizing": "缺少持仓信息，无法计算买入数量。",
        "invalid last price": "当前价无效。",
        "current quantity below one share for sell sizing": "当前数量不足 1 股，无法计算卖出数量。",
        "suggested quantity below one share": "建议数量不足 1 股。",
        "no same-currency cash available": "没有可用的同币种现金。",
        "no remaining target budget": "目标仓位预算已用完。",
        "no remaining entry budget": "首笔建仓预算已用完。",
        "missing positive fx_to_hkd for sell-side sizing": "缺少有效汇率，无法计算卖出后仓位。",
        "missing positive fx_to_hkd for buy-side sizing": "缺少有效汇率，无法计算买入后仓位。",
        "Stop loss was hit.": "已触发止损。",
        "Current price is at or below the stop loss.": "当前价格已达到或低于止损价。",
        "Current price is at or above target 1.": "当前价格已满足计划触发条件。",
        "Current price is at or above target 2.": "当前价格已满足计划触发条件。",
        "Current price is inside the planned entry zone.": "当前价格位于计划买入区间。",
        "Current price is near the planned add price.": "当前价格接近计划加仓价。",
        "Plan text indicates trim at current levels.": "计划正文要求在当前价位减仓。",
        "unparseable target max weight": "目标最大仓位无法解析",
    }.get(normalized)
    if translated is not None:
        return translated
    if any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in normalized):
        return "系统原因，需人工复核。"
    return normalized


def _missing_precise_fields(row: Mapping[str, str]) -> list[str]:
    action = row.get("action", "").strip().upper()
    required_fields = [
        "last_price",
        "current_quantity",
        "current_weight",
    ]
    if action not in {"TRIM", "TAKE_PROFIT", "SELL_STOP"}:
        required_fields.append("avg_cost_price")
    if action != "SELL_STOP":
        required_fields.append("limit_price")
    required_fields.extend(
        [
            "suggested_quantity",
            "suggested_notional",
            "notional_currency",
            "post_trade_quantity",
            "post_trade_weight",
        ]
    )
    if action not in {"TRIM", "TAKE_PROFIT", "SELL_STOP"}:
        required_fields.append("post_trade_avg_cost")
        required_fields.append("risk_to_stop")
        required_fields.append("stop_price")
    required_fields.append("reason")
    return [field for field in required_fields if not row.get(field, "").strip()]


def _effective_status(row: Mapping[str, str]) -> str:
    status = _row_status(row)
    if status == "ready" and _missing_precise_fields(row):
        return "review"
    return status


def _trigger_price(row: Mapping[str, str]) -> str:
    limit_price = row.get("limit_price", "").strip()
    if limit_price:
        return limit_price
    if row.get("action", "").strip().upper() == "SELL_STOP":
        return row.get("last_price", "").strip()
    return ""


def _risk_to_stop_text(row: Mapping[str, str], currency: str) -> str:
    risk_to_stop = row.get("risk_to_stop", "").strip()
    if risk_to_stop:
        return f"{currency} {risk_to_stop}".strip()
    if (
        row.get("action", "").strip().upper() == "SELL_STOP"
        and row.get("post_trade_quantity", "").strip() == "0"
    ):
        return "全部退出"
    return ""


def _read_action_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _row_status(row: Mapping[str, str]) -> str:
    return row.get("status", "").strip().lower()


def _priority_sort_key(row: Mapping[str, str]) -> tuple[int, str]:
    priority_order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }
    priority = row.get("priority", "").strip().lower()
    symbol = row.get("futu_symbol", "").strip()
    return priority_order.get(priority, len(priority_order)), symbol


def _post_json(
    url: str,
    payload: dict[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise NotificationError(f"Feishu webhook request failed: {exc}") from exc

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise NotificationError(
            f"Feishu webhook returned invalid JSON: {response_body}"
        ) from exc

    if not isinstance(parsed, dict):
        raise NotificationError("Feishu webhook returned non-object JSON")
    return parsed


def _post_json_with_headers(
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, object]:
    merged_headers = {
        "Content-Type": "application/json; charset=utf-8",
        **headers,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=merged_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except Exception as exc:
        raise NotificationError(f"Feishu app request failed: {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NotificationError("Feishu app returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise NotificationError("Feishu app returned non-object JSON")
    return parsed


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
