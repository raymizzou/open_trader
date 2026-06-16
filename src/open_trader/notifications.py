from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        pass


class NotificationSendError(RuntimeError):
    pass


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class MacOSNotifier:
    def notify(self, title: str, message: str) -> None:
        script = (
            f'display notification "{_escape_osascript(message)}" '
            f'with title "{_escape_osascript(title)}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self.notifiers = list(notifiers)

    def notify(self, title: str, message: str) -> None:
        errors: list[str] = []
        for notifier in self.notifiers:
            try:
                notifier.notify(title, message)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise NotificationSendError("; ".join(errors))


class WeComWebhookNotifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        message_format: str = "markdown",
        sender: Callable[[str, dict[str, object], float], None] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if message_format not in {"markdown", "text"}:
            raise ValueError("message_format must be markdown or text")
        self.webhook_url = webhook_url
        self.message_format = message_format
        self.sender = sender or _send_wecom_payload
        self.timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        self.sender(
            self.webhook_url,
            _wecom_payload(message, self.message_format),
            self.timeout_seconds,
        )


def build_notifier_from_values(
    values: Mapping[str, str],
    *,
    dry_run: bool = False,
) -> Notifier:
    names = [
        name.strip().lower()
        for name in values.get("OPEN_TRADER_NOTIFIERS", "").split(",")
        if name.strip()
    ]
    if dry_run or not names or names == ["none"]:
        return NullNotifier()

    notifiers: list[Notifier] = []
    for name in names:
        if name == "macos":
            notifiers.append(MacOSNotifier())
        elif name == "wecom":
            webhook_url = values.get("OPEN_TRADER_WECOM_WEBHOOK_URL", "").strip()
            if not webhook_url:
                raise ValueError(
                    "OPEN_TRADER_WECOM_WEBHOOK_URL is required when wecom notifier is enabled"
                )
            notifiers.append(
                WeComWebhookNotifier(
                    webhook_url=webhook_url,
                    message_format=values.get(
                        "OPEN_TRADER_WECOM_MESSAGE_FORMAT",
                        "markdown",
                    ).strip()
                    or "markdown",
                )
            )
        elif name == "none":
            continue
        else:
            raise ValueError(f"unknown notifier: {name}")
    return CompositeNotifier(notifiers) if notifiers else NullNotifier()


@dataclass
class NotificationState:
    path: Path
    sent: set[str]

    @classmethod
    def load(cls, path: Path) -> NotificationState:
        if not path.exists():
            return cls(path=path, sent=set())
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls(path=path, sent=set())
        sent = payload.get("sent", [])
        if not isinstance(sent, list):
            sent = []
        return cls(path=path, sent={str(item) for item in sent})

    def was_sent(self, run_date: str, futu_symbol: str, trigger_status: str) -> bool:
        return _state_key(run_date, futu_symbol, trigger_status) in self.sent

    def record_sent(self, run_date: str, futu_symbol: str, trigger_status: str) -> None:
        self.sent.add(_state_key(run_date, futu_symbol, trigger_status))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sent": sorted(self.sent)}
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temp_path.replace(self.path)
        except Exception:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise


def render_daily_trade_action_message(
    *,
    run_date: str,
    status: str,
    premarket: Mapping[str, object],
    futu_status: Mapping[str, object],
    action_rows: list[Mapping[str, str]],
    daily_report_path: Path,
    trade_actions_report_path: Path,
) -> str:
    ready = [row for row in action_rows if row.get("status") == "ready"]
    review = [row for row in action_rows if row.get("status") == "review"]
    watch = [row for row in action_rows if row.get("status") == "watch"]
    lines = [
        f"# Open Trader {run_date}: {status}",
        "",
        "Summary:",
        (
            f"- Advice: {premarket.get('ok', 0)} ok, "
            f"{premarket.get('fallback', 0)} fallback, "
            f"{premarket.get('error', 0)} error"
        ),
        f"- Actions: {len(ready)} ready, {len(review)} review, {len(watch)} watch",
        (
            f"- Futu: {futu_status.get('checked', 0)} checked, "
            f"{futu_status.get('missing', 0)} missing, "
            f"{futu_status.get('triggered', 0)} triggered"
        ),
    ]
    _append_action_section(lines, "Ready", ready)
    _append_action_section(lines, "Review", review)
    _append_action_section(lines, "Watch", watch)
    lines.extend(
        [
            "",
            "Reports:",
            f"- {daily_report_path}",
            f"- {trade_actions_report_path}",
        ]
    )
    return "\n".join(lines)


def render_trigger_message(
    *,
    run_date: str,
    row: Mapping[str, str],
    report_path: Path,
) -> str:
    futu_symbol = row.get("futu_symbol", "").strip()
    action = row.get("action", "").strip()
    lines = [
        "# Open Trader Trigger",
        "",
        f"{futu_symbol} {action} triggered",
        f"- Price: {row.get('last_price', '').strip()}",
    ]
    quantity = row.get("suggested_quantity", "").strip()
    if quantity:
        lines.append(f"- Quantity: {quantity}")
    notional = row.get("suggested_notional", "").strip()
    currency = row.get("notional_currency", "").strip()
    if notional:
        lines.append(f"- Notional: {currency} {notional}".strip())
    reason = _trim_reason(row.get("reason", ""))
    if reason:
        lines.append(f"- Reason: {reason}")
    lines.append(f"- Report: {report_path}")
    return "\n".join(lines)


def load_trade_action_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {key: value or "" for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _send_wecom_payload(
    webhook_url: str,
    payload: dict[str, object],
    timeout_seconds: float,
) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise NotificationSendError(
                    f"WeCom webhook returned HTTP {response.status}"
                )
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return
            if payload.get("errcode") not in {0, None}:
                raise NotificationSendError(
                    f"WeCom webhook error {payload.get('errcode')}: "
                    f"{payload.get('errmsg', '')}"
                )
    except urllib.error.URLError as exc:
        raise NotificationSendError(f"WeCom webhook failed: {exc.reason}") from exc


def _wecom_payload(message: str, message_format: str) -> dict[str, object]:
    if message_format == "text":
        return {"msgtype": "text", "text": {"content": message}}
    return {"msgtype": "markdown", "markdown": {"content": message}}


def _append_action_section(
    lines: list[str],
    title: str,
    rows: list[Mapping[str, str]],
) -> None:
    lines.extend(["", f"{title}:"])
    if not rows:
        lines.append("- none")
        return
    for row in rows[:20]:
        lines.append(_format_action_line(row))
    if len(rows) > 20:
        lines.append(f"- ... {len(rows) - 20} more")


def _format_action_line(row: Mapping[str, str]) -> str:
    futu_symbol = row.get("futu_symbol", "").strip()
    action = row.get("action", "").strip()
    priority = row.get("priority", "").strip()
    last_price = row.get("last_price", "").strip()
    quantity = row.get("suggested_quantity", "").strip()
    reason = _trim_reason(row.get("reason", ""))
    text = f"- {futu_symbol} {action} {priority}".rstrip()
    if last_price:
        text += f" @ {last_price}"
    if quantity:
        text += f", qty {quantity}"
    if reason:
        text += f", {reason}"
    return text


def _trim_reason(value: str, max_length: int = 120) -> str:
    reason = " ".join(value.strip().split())
    if len(reason) <= max_length:
        return reason
    return reason[: max_length - 3].rstrip() + "..."


def _state_key(run_date: str, futu_symbol: str, trigger_status: str) -> str:
    return f"{run_date}|{futu_symbol.upper()}|{trigger_status}"


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
