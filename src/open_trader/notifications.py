from __future__ import annotations

import csv
import json
import subprocess
import urllib.error
import urllib.request
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
        f"Open Trader {run_date}：{_status_label(status)}",
        "",
        "摘要：",
        f"- 可执行：{len(ready_rows)}",
        f"- 需复核：{len(review_rows)}",
        f"- 观察中：{len(watch_rows)}",
    ]

    if ready_rows:
        lines.extend(["", "可执行："])
        sorted_ready_rows = sorted(ready_rows, key=_priority_sort_key)
        for row in sorted_ready_rows[:max_ready_sections]:
            lines.extend(["", *_render_ready_section(row)])
        remaining_count = len(sorted_ready_rows) - max_ready_sections
        if remaining_count > 0:
            lines.append(f"- 另有 {remaining_count} 条可执行动作见报告。")

    if review_rows:
        lines.extend(["", "需复核："])
        for row in sorted(review_rows, key=_priority_sort_key):
            if _row_status(row) == "ready":
                lines.extend(["", *_render_ready_section(row)])
                continue
            symbol = row.get("futu_symbol", "").strip()
            priority = _priority_label(row.get("priority", "").strip())
            reason = row.get("error", "").strip() or row.get("reason", "").strip()
            lines.append(f"- {symbol} {priority}: {reason}".rstrip())

    if watch_rows:
        lines.extend(["", f"观察中：{len(watch_rows)} 条动作等待触发。"])

    if report_paths:
        lines.extend(["", "报告："])
        lines.extend(f"- {path}" for path in report_paths)

    return "\n".join(lines).strip() + "\n"


def _render_ready_section(row: Mapping[str, str]) -> list[str]:
    missing_fields = _missing_precise_fields(row)
    action = (
        _action_label("REVIEW")
        if missing_fields
        else _action_label(row.get("action", "").strip())
    )
    lines = [
        (
            f"## {row.get('futu_symbol', '').strip()} | "
            f"{_priority_label(row.get('priority', '').strip())} | {action}"
        )
    ]

    if missing_fields:
        lines.extend(
            [
                f"执行前缺少：{'、'.join(_field_label(field) for field in missing_fields)}",
                f"原因：{row.get('reason', '').strip()}",
            ]
        )
        return lines

    currency = row.get("notional_currency", "").strip()
    trigger_price = _trigger_price(row)
    lines.extend(
        [
            f"当前价：{row.get('last_price', '').strip()}",
            f"当前数量：{row.get('current_quantity', '').strip()}",
            f"当前仓位：{row.get('current_weight', '').strip()}",
            f"当前成本：{row.get('avg_cost_price', '').strip()}",
            f"触发价：{trigger_price}",
            (
                f"本次指令：{_action_label(row.get('action', '').strip())} "
                f"{row.get('suggested_quantity', '').strip()} 股"
            ),
            (
                f"预计金额：{currency} "
                f"{row.get('suggested_notional', '').strip()}"
            ),
            f"交易后数量：{row.get('post_trade_quantity', '').strip()}",
            f"交易后仓位：{row.get('post_trade_weight', '').strip()}",
            f"交易后成本：{row.get('post_trade_avg_cost', '').strip()}",
            f"硬止损：{row.get('stop_price', '').strip()}",
            f"止损风险：{_risk_to_stop_text(row, currency)}",
            f"原因：{row.get('reason', '').strip()}",
        ]
    )
    return lines


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


def _missing_precise_fields(row: Mapping[str, str]) -> list[str]:
    action = row.get("action", "").strip().upper()
    required_fields = [
        "last_price",
        "current_quantity",
        "current_weight",
        "avg_cost_price",
    ]
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
    if action != "SELL_STOP" or row.get("post_trade_quantity", "").strip() != "0":
        required_fields.append("post_trade_avg_cost")
        required_fields.append("risk_to_stop")
    required_fields.extend(
        [
            "stop_price",
            "reason",
        ]
    )
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
    return currency


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
