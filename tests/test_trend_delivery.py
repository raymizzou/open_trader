import hashlib
import json
from pathlib import Path

from open_trader.notifications import FeishuWebhookNotifier
from open_trader.trend_delivery import (
    deliver_daily_trend_text,
    retry_daily_trend_text,
)


class RecordingNotifier(FeishuWebhookNotifier):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(webhook_url="https://example.invalid")
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise RuntimeError("network down")


def test_first_message_owns_day_and_revision_cannot_replace_it(tmp_path: Path) -> None:
    notifier = RecordingNotifier()
    ledger = tmp_path / "delivery/2026-07-15.json"
    assert deliver_daily_trend_text(
        notifier, ledger_path=ledger, title="success", message="first"
    ) == "sent"
    assert deliver_daily_trend_text(
        notifier, ledger_path=ledger, title="revision", message="second"
    ) == "sent_prior_message"
    assert notifier.messages == [("success", "first")]


def test_failed_transport_retries_exact_frozen_text(tmp_path: Path) -> None:
    ledger = tmp_path / "delivery/2026-07-15.json"
    failing = RecordingNotifier(fail=True)
    assert deliver_daily_trend_text(
        failing, ledger_path=ledger, title="failure", message="frozen"
    ) == "delivery_failed"
    recovered = RecordingNotifier()
    assert deliver_daily_trend_text(
        recovered, ledger_path=ledger, title="new report", message="must not replace"
    ) == "sent"
    assert recovered.messages == [("failure", "frozen")]


def test_pending_delivery_retries_the_same_frozen_text(tmp_path: Path) -> None:
    ledger = tmp_path / "delivery/2026-07-15.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "status": "pending",
                "title": "frozen",
                "message": "body",
                "content_sha256": hashlib.sha256(b"frozen\0body").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    notifier = RecordingNotifier()
    assert retry_daily_trend_text(notifier, ledger_path=ledger) == "sent"
    assert notifier.messages == [("frozen", "body")]
