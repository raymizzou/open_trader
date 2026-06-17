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
        f"Open Trader {run_date}: {status}",
        "",
        "Summary:",
        f"- Ready: {len(ready_rows)}",
        f"- Review: {len(review_rows)}",
        f"- Watch: {len(watch_rows)}",
    ]

    if ready_rows:
        lines.extend(["", "Ready:"])
        sorted_ready_rows = sorted(ready_rows, key=_priority_sort_key)
        for row in sorted_ready_rows[:max_ready_sections]:
            lines.extend(["", *_render_ready_section(row)])
        remaining_count = len(sorted_ready_rows) - max_ready_sections
        if remaining_count > 0:
            lines.append(f"- {remaining_count} additional ready action(s) in report.")

    if review_rows:
        lines.extend(["", "Review:"])
        for row in sorted(review_rows, key=_priority_sort_key):
            if _row_status(row) == "ready":
                lines.extend(["", *_render_ready_section(row)])
                continue
            symbol = row.get("futu_symbol", "").strip()
            priority = row.get("priority", "").strip()
            reason = row.get("error", "").strip() or row.get("reason", "").strip()
            lines.append(f"- {symbol} {priority}: {reason}".rstrip())

    if watch_rows:
        lines.extend(["", f"Watch: {len(watch_rows)} action(s) waiting for trigger."])

    if report_paths:
        lines.extend(["", "Reports:"])
        lines.extend(f"- {path}" for path in report_paths)

    return "\n".join(lines).strip() + "\n"


def _render_ready_section(row: Mapping[str, str]) -> list[str]:
    missing_fields = _missing_precise_fields(row)
    action = "REVIEW" if missing_fields else row.get("action", "").strip()
    lines = [
        (
            f"## {row.get('futu_symbol', '').strip()} | "
            f"{row.get('priority', '').strip()} | {action}"
        )
    ]

    if missing_fields:
        lines.extend(
            [
                f"Missing before action: {', '.join(missing_fields)}",
                f"Reason: {row.get('reason', '').strip()}",
            ]
        )
        return lines

    currency = row.get("notional_currency", "").strip()
    trigger_price = _trigger_price(row)
    lines.extend(
        [
            f"Current price: {row.get('last_price', '').strip()}",
            f"Current quantity: {row.get('current_quantity', '').strip()}",
            f"Current weight: {row.get('current_weight', '').strip()}",
            f"Current average cost: {row.get('avg_cost_price', '').strip()}",
            f"Trigger price: {trigger_price}",
            (
                f"This order: {row.get('action', '').strip()} "
                f"{row.get('suggested_quantity', '').strip()} shares"
            ),
            (
                f"Estimated notional: {currency} "
                f"{row.get('suggested_notional', '').strip()}"
            ),
            f"Post-trade quantity: {row.get('post_trade_quantity', '').strip()}",
            f"Post-trade weight: {row.get('post_trade_weight', '').strip()}",
            f"Post-trade average cost: {row.get('post_trade_avg_cost', '').strip()}",
            f"Hard stop: {row.get('stop_price', '').strip()}",
            f"Risk to stop: {_risk_to_stop_text(row, currency)}",
            f"Reason: {row.get('reason', '').strip()}",
        ]
    )
    return lines


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
        return "full exit"
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


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
