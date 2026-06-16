from pathlib import Path

from open_trader.notifications import (
    CompositeNotifier,
    NotificationSendError,
    NotificationState,
    RecordingNotifier,
    WeComWebhookNotifier,
    render_daily_trade_action_message,
    render_trigger_message,
)


def test_wecom_webhook_notifier_sends_markdown_payload() -> None:
    calls = []
    notifier = WeComWebhookNotifier(
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        sender=lambda url, payload, timeout: calls.append((url, payload, timeout)),
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader", "# Report")

    assert calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
            {"msgtype": "markdown", "markdown": {"content": "# Report"}},
            3.0,
        )
    ]


def test_composite_notifier_continues_after_child_failure() -> None:
    class FailingNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("webhook failed")

    recording = RecordingNotifier()
    composite = CompositeNotifier([FailingNotifier(), recording])

    try:
        composite.notify("Title", "Message")
    except NotificationSendError as exc:
        assert "webhook failed" in str(exc)
    else:
        raise AssertionError("expected NotificationSendError")

    assert recording.messages == [("Title", "Message")]


def test_render_daily_trade_action_message_groups_rows() -> None:
    message = render_daily_trade_action_message(
        run_date="2026-06-17",
        status="success",
        premarket={"ok": 12, "fallback": 0, "error": 0},
        futu_status={"checked": 13, "missing": 0, "triggered": 2},
        action_rows=[
            {
                "futu_symbol": "US.MSFT",
                "action": "BUY",
                "priority": "high",
                "last_price": "399",
                "suggested_quantity": "3",
                "status": "ready",
                "reason": "entered entry zone",
            },
            {
                "futu_symbol": "US.TSLA",
                "action": "REVIEW",
                "priority": "medium",
                "last_price": "",
                "suggested_quantity": "",
                "status": "review",
                "reason": "missing_quote",
            },
            {
                "futu_symbol": "US.AAPL",
                "action": "HOLD",
                "priority": "low",
                "last_price": "210",
                "suggested_quantity": "",
                "status": "watch",
                "reason": "wait",
            },
        ],
        daily_report_path=Path("reports/daily_runs/2026-06-17.md"),
        trade_actions_report_path=Path("reports/trade_actions/2026-06-17.md"),
    )

    assert "# Open Trader 2026-06-17: success" in message
    assert "- Actions: 1 ready, 1 review, 1 watch" in message
    assert "- US.MSFT BUY high @ 399, qty 3, entered entry zone" in message
    assert "- US.TSLA REVIEW medium, missing_quote" in message
    assert "- US.AAPL HOLD low @ 210, wait" in message


def test_render_trigger_message_contains_action_detail() -> None:
    message = render_trigger_message(
        run_date="2026-06-17",
        row={
            "futu_symbol": "US.MSFT",
            "action": "BUY",
            "last_price": "399",
            "suggested_quantity": "3",
            "suggested_notional": "1197",
            "notional_currency": "USD",
            "reason": "entered entry zone",
            "trigger_status": "entry_zone",
        },
        report_path=Path("reports/trade_actions/2026-06-17.md"),
    )

    assert "# Open Trader Trigger" in message
    assert "US.MSFT BUY triggered" in message
    assert "- Price: 399" in message
    assert "- Quantity: 3" in message
    assert "- Notional: USD 1197" in message


def test_notification_state_records_sent_keys(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    state = NotificationState.load(path)

    assert state.was_sent("2026-06-17", "US.MSFT", "entry_zone") is False
    state.record_sent("2026-06-17", "US.MSFT", "entry_zone")
    state.save()

    reloaded = NotificationState.load(path)
    assert reloaded.was_sent("2026-06-17", "US.MSFT", "entry_zone") is True
    assert reloaded.was_sent("2026-06-17", "US.MSFT", "stop_loss_hit") is False
