from __future__ import annotations

import csv
import urllib.request
from pathlib import Path

import pytest

from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    NotificationError,
    render_feishu_order_review,
)


WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test"

FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "priority",
    "last_price",
    "trigger_status",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "current_quantity",
    "current_weight",
    "avg_cost_price",
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
    "post_trade_quantity",
    "post_trade_weight",
    "post_trade_avg_cost",
    "risk_to_stop",
    "reason",
    "source_plan",
    "status",
    "error",
]


def test_feishu_webhook_notifier_sends_text_payload() -> None:
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout_seconds
        return {"code": 0, "msg": "success"}

    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        post_json=fake_post,
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader", "hello")

    assert captured == {
        "url": WEBHOOK_URL,
        "payload": {"msg_type": "text", "content": {"text": "Open Trader\n\nhello"}},
        "timeout": 3.0,
    }


def test_feishu_webhook_notifier_raises_on_api_error() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"code": 19024, "msg": "bad webhook"}

    notifier = FeishuWebhookNotifier(
        webhook_url=WEBHOOK_URL,
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Feishu webhook error 19024"):
        notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_accepts_status_code_success() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"StatusCode": 0, "StatusMessage": "success"}

    notifier = FeishuWebhookNotifier(
        webhook_url=WEBHOOK_URL,
        post_json=fake_post,
    )

    notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_raises_on_status_code_error() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"StatusCode": 19024, "StatusMessage": "bad webhook"}

    notifier = FeishuWebhookNotifier(
        webhook_url=WEBHOOK_URL,
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="bad webhook"):
        notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_raises_when_response_omits_code() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"msg": "bad webhook"}

    notifier = FeishuWebhookNotifier(
        webhook_url=WEBHOOK_URL,
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Feishu webhook error missing"):
        notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_raises_on_stdlib_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr("open_trader.notifications.urllib.request.urlopen", fake_urlopen)

    notifier = FeishuWebhookNotifier(webhook_url=WEBHOOK_URL)

    with pytest.raises(NotificationError, match="Feishu webhook request failed"):
        notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_raises_on_invalid_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_trader.notifications.urllib.request.urlopen",
        _fake_urlopen_with_body("not-json"),
    )

    notifier = FeishuWebhookNotifier(webhook_url=WEBHOOK_URL)

    with pytest.raises(NotificationError, match="returned invalid JSON"):
        notifier.notify("Open Trader", "hello")


def test_feishu_webhook_notifier_raises_on_non_object_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_trader.notifications.urllib.request.urlopen",
        _fake_urlopen_with_body("[0]"),
    )

    notifier = FeishuWebhookNotifier(webhook_url=WEBHOOK_URL)

    with pytest.raises(NotificationError, match="returned non-object JSON"):
        notifier.notify("Open Trader", "hello")


def test_composite_notifier_continues_after_child_failure() -> None:
    events: list[str] = []

    class Failing:
        def notify(self, title: str, message: str) -> None:
            events.append("failing")
            raise RuntimeError("boom")

    class Working:
        def notify(self, title: str, message: str) -> None:
            events.append(f"{title}:{message}")

    CompositeNotifier([Failing(), Working()]).notify("title", "body")

    assert events == ["failing", "title:body"]


def test_render_feishu_order_review_includes_precise_ready_fields(tmp_path: Path) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                run_date="2026-06-17",
                symbol="RKLB",
                market="US",
                futu_symbol="US.RKLB",
                action="ADD",
                priority="high",
                last_price="109",
                trigger_status="entry_zone",
                suggested_quantity="80",
                suggested_notional="8720",
                notional_currency="USD",
                current_quantity="120",
                current_weight="1.36%",
                avg_cost_price="101.20",
                target_max_weight="2.20%",
                cash_available="10000",
                limit_price="102",
                stop_price="94",
                post_trade_quantity="200",
                post_trade_weight="2.20%",
                post_trade_avg_cost="104.32",
                risk_to_stop="3000",
                reason="price entered entry zone",
                source_plan="data/runs/2026-06-17/trading_plan.csv",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[Path("reports/trade_actions/2026-06-17.md")],
    )

    assert "Open Trader 2026-06-17: success" in body
    assert "US.RKLB | high | ADD" in body
    assert "Current price: 109" in body
    assert "Current quantity: 120" in body
    assert "Current weight: 1.36%" in body
    assert "Current average cost: 101.20" in body
    assert "Trigger price: 102" in body
    assert "This order: ADD 80 shares" in body
    assert "Estimated notional: USD 8720" in body
    assert "Post-trade quantity: 200" in body
    assert "Post-trade weight: 2.20%" in body
    assert "Post-trade average cost: 104.32" in body
    assert "Hard stop: 94" in body
    assert "Risk to stop: USD 3000" in body
    assert "Reason: price entered entry zone" in body


def test_render_feishu_order_review_marks_missing_post_trade_fields_review(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                run_date="2026-06-17",
                symbol="MSFT",
                market="US",
                futu_symbol="US.MSFT",
                action="ADD",
                priority="high",
                last_price="390",
                trigger_status="entry_zone",
                suggested_quantity="6",
                suggested_notional="2340",
                notional_currency="USD",
                current_quantity="10",
                current_weight="1.13%",
                avg_cost_price="",
                target_max_weight="2%",
                cash_available="10000",
                limit_price="390",
                stop_price="340",
                post_trade_quantity="",
                post_trade_weight="",
                post_trade_avg_cost="",
                risk_to_stop="",
                reason="missing avg cost",
                source_plan="data/runs/2026-06-17/trading_plan.csv",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="partial",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "Ready: 0" in body
    assert "Review: 1" in body
    assert "US.MSFT | high | REVIEW" in body
    assert (
        "Missing before action: avg_cost_price, post_trade_quantity, "
        "post_trade_weight, post_trade_avg_cost, risk_to_stop"
    ) in body


def test_render_feishu_order_review_keeps_ready_sell_stop_with_blank_limit_price(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                futu_symbol="US.MSFT",
                action="SELL_STOP",
                priority="critical",
                last_price="339",
                trigger_status="stop_loss_hit",
                suggested_quantity="10",
                suggested_notional="3390",
                notional_currency="USD",
                current_quantity="10",
                current_weight="1.13%",
                avg_cost_price="300",
                limit_price="",
                stop_price="340",
                post_trade_quantity="0",
                post_trade_weight="0%",
                post_trade_avg_cost="",
                risk_to_stop="",
                reason="Stop loss was hit.",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "US.MSFT | critical | SELL_STOP" in body
    assert "Trigger price: 339" in body
    assert "Current price: 339" in body
    assert "Hard stop: 340" in body
    assert "Risk to stop: full exit" in body
    assert "Missing before action" not in body


def test_render_feishu_order_review_truncates_ready_rows_and_includes_reports(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(futu_symbol="US.AAA", priority="high", status="ready"),
            _action_row(futu_symbol="US.BBB", priority="medium", status="ready"),
            _action_row(futu_symbol="US.CCC", priority="low", status="ready"),
            _action_row(futu_symbol="US.DDD", priority="critical", status="review"),
            _action_row(futu_symbol="US.EEE", priority="low", status="watch"),
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[
            Path("reports/trade_actions/2026-06-17.md"),
            Path("data/runs/2026-06-17/trade_actions.csv"),
        ],
        max_ready_sections=2,
    )

    assert "Ready: 3" in body
    assert "Review: 1" in body
    assert "Watch: 1" in body
    assert "US.AAA | high | BUY" in body
    assert "US.BBB | medium | BUY" in body
    assert "US.CCC | low | BUY" not in body
    assert "1 additional ready action(s) in report." in body
    assert "reports/trade_actions/2026-06-17.md" in body
    assert "data/runs/2026-06-17/trade_actions.csv" in body


def _write_actions(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _action_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-17",
        "symbol": "AAA",
        "market": "US",
        "futu_symbol": "US.AAA",
        "action": "BUY",
        "priority": "high",
        "last_price": "10",
        "trigger_status": "entry_zone",
        "suggested_quantity": "5",
        "suggested_notional": "50",
        "notional_currency": "USD",
        "current_quantity": "1",
        "current_weight": "1%",
        "avg_cost_price": "9",
        "target_max_weight": "2%",
        "cash_available": "100",
        "limit_price": "10",
        "stop_price": "8",
        "post_trade_quantity": "6",
        "post_trade_weight": "2%",
        "post_trade_avg_cost": "9.83",
        "risk_to_stop": "12",
        "reason": "ready fixture",
        "source_plan": "plan.csv",
        "status": "ready",
        "error": "",
    }
    row.update(overrides)
    return row


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def _fake_urlopen_with_body(body: str) -> object:
    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        return _FakeResponse(body)

    return fake_urlopen
