from __future__ import annotations

import csv
import json
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
import open_trader.notifications as notifications

from open_trader.notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    NotificationError,
    XiaozhiVoiceNotifier,
    render_feishu_order_review,
    render_xiaozhi_voice_notification,
)


WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
ALLOWED_NOW = lambda: datetime.fromisoformat("2026-07-15T22:59:59+08:00")
PROTECTION_TITLE = "A股保护线触发 · 600000"
PROTECTION_MESSAGE = "名称：浦发银行\n最新价 9.98 <= 活动保护线 10.01"


def xiaozhi_voice_allowed(now: datetime) -> bool:
    assert hasattr(notifications, "xiaozhi_voice_allowed")
    return notifications.xiaozhi_voice_allowed(now)


def xiaozhi_not_after(now: datetime) -> str:
    assert hasattr(notifications, "xiaozhi_not_after")
    return notifications.xiaozhi_not_after(now)

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
    "agent_reason",
    "agent_excerpt",
    "trigger_reason",
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


def test_feishu_app_notifier_sends_text_message() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout_seconds,
            }
        )
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tenant-token"}
        return {"code": 0, "data": {"message_id": "om_test"}}

    notifier = FeishuAppNotifier(
        app_id="cli_test",
        app_secret="secret",
        receive_id_type="email",
        receive_id="ray@example.com",
        post_json=fake_post,
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader 行动通知", "测试正文")

    assert calls == [
        {
            "url": "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            "payload": {"app_id": "cli_test", "app_secret": "secret"},
            "headers": {},
            "timeout": 3.0,
        },
        {
            "url": "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=email",
            "payload": {
                "receive_id": "ray@example.com",
                "msg_type": "text",
                "content": json.dumps(
                    {"text": "Open Trader 行动通知\n\n测试正文"},
                    ensure_ascii=False,
                ),
            },
            "headers": {"Authorization": "Bearer tenant-token"},
            "timeout": 3.0,
        },
    ]


def test_feishu_app_notifier_raises_on_token_error() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"code": 999, "msg": "bad app"}

    notifier = FeishuAppNotifier(
        app_id="cli_test",
        app_secret="secret",
        receive_id_type="email",
        receive_id="ray@example.com",
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Feishu token error 999"):
        notifier.notify("Open Trader", "hello")


def test_xiaozhi_voice_notifier_sends_payload_and_bearer_header() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout_seconds,
            }
        )
        return {"code": 0, "message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        timeout_seconds=2.5,
        now_fn=ALLOWED_NOW,
    )

    notifier.notify("Open Trader 测试通知", "这是一条测试通知。")

    assert calls == [
        {
            "url": "http://127.0.0.1:8003/xiaozhi/notify/speak",
            "payload": {
                "device_id": "speaker-1",
                "title": "Open Trader 测试通知",
                "message": "这是一条测试通知。",
                "not_after": "2026-07-15T23:00:00+08:00",
            },
            "headers": {"Authorization": "Bearer voice-token"},
            "timeout": 2.5,
        }
    ]


@pytest.mark.parametrize(
    ("title", "message", "expected"),
    [
        (
            PROTECTION_TITLE,
            PROTECTION_MESSAGE,
            "Open Trader 紧急提醒：A股浦发银行，代码600000，最新价9.98，已触及活动保护线10.01。建议全部卖出，请查看飞书确认并人工执行。",
        ),
        (
            "港股保护线触发 · 00700",
            "名称：腾讯控股\n最新价 399.8 <= 活动保护线 400",
            "Open Trader 紧急提醒：港股腾讯控股，代码00700，最新价399.8，已触及活动保护线400。建议全部卖出，请查看飞书确认并人工执行。",
        ),
        (
            "美股保护线触发 · NVDA",
            "名称：\n最新价 150.25 <= 活动保护线 151.00",
            "Open Trader 紧急提醒：美股代码NVDA，最新价150.25，已触及活动保护线151.00。建议全部卖出，请查看飞书确认并人工执行。",
        ),
    ],
)
def test_render_xiaozhi_protection_template(
    title: str, message: str, expected: str
) -> None:
    assert render_xiaozhi_voice_notification(title, message) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Open Trader 美股开始通知",
        "Open Trader 港股阻塞通知",
        "Open Trader 美股行动通知",
        "Open Trader 港股完成通知",
        "Open Trader｜做T提醒｜US ARM｜买入做T",
        "A股趋势操作计划 · 2026-07-15",
        "Open Trader 其他通知",
    ],
)
def test_render_xiaozhi_skips_non_protection_business_events(title: str) -> None:
    assert render_xiaozhi_voice_notification(title, "正文") is None


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("2026-07-15T07:59:59+08:00", False),
        ("2026-07-15T08:00:00+08:00", True),
        ("2026-07-15T22:59:59+08:00", True),
        ("2026-07-15T23:00:00+08:00", False),
    ],
)
def test_xiaozhi_voice_hours(value: str, allowed: bool) -> None:
    assert xiaozhi_voice_allowed(datetime.fromisoformat(value)) is allowed


def test_xiaozhi_deadline_is_23_shanghai() -> None:
    assert xiaozhi_not_after(ALLOWED_NOW()) == "2026-07-15T23:00:00+08:00"


def test_xiaozhi_voice_notifier_sends_protection_payload() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout_seconds,
            }
        )
        return {"code": 0, "message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        timeout_seconds=2.5,
        now_fn=ALLOWED_NOW,
    )

    notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)

    assert calls == [
        {
            "url": "http://127.0.0.1:8003/xiaozhi/notify/speak",
            "payload": {
                "device_id": "speaker-1",
                "title": PROTECTION_TITLE,
                "message": "Open Trader 紧急提醒：A股浦发银行，代码600000，最新价9.98，已触及活动保护线10.01。建议全部卖出，请查看飞书确认并人工执行。",
                "not_after": "2026-07-15T23:00:00+08:00",
            },
            "headers": {"Authorization": "Bearer voice-token"},
            "timeout": 2.5,
        }
    ]


def test_xiaozhi_voice_notifier_skips_daily_action_notification() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append({"payload": payload})
        return {"code": 0}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
    )

    notifier.notify("Open Trader 美股行动通知", "Open Trader｜行动通知")

    assert calls == []


def test_xiaozhi_voice_notifier_skips_quiet_hours() -> None:
    calls: list[object] = []
    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=lambda *args: calls.append(args) or {"code": 0},
        now_fn=lambda: datetime.fromisoformat("2026-07-15T23:00:00+08:00"),
    )

    notifier.notify("Open Trader 测试通知", "测试")

    assert calls == []


def test_xiaozhi_voice_notifier_raises_on_api_error() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"code": 404, "message": "device_offline"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        now_fn=ALLOWED_NOW,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice error 404: device_offline"):
        notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)


def test_xiaozhi_voice_notifier_raises_when_response_omits_code() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        now_fn=ALLOWED_NOW,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice error missing: code"):
        notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)


def test_xiaozhi_voice_notifier_wraps_transport_failure() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        raise TimeoutError("timed out")

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        now_fn=ALLOWED_NOW,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice request failed: timed out"):
        notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)


def test_xiaozhi_voice_notifier_raises_on_invalid_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_trader.notifications.urllib.request.urlopen",
        _fake_urlopen_with_body("not-json"),
    )

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        now_fn=ALLOWED_NOW,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice returned invalid JSON"):
        notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)


def test_xiaozhi_voice_notifier_raises_on_non_object_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_trader.notifications.urllib.request.urlopen",
        _fake_urlopen_with_body("[0]"),
    )

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        now_fn=ALLOWED_NOW,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice returned non-object JSON"):
        notifier.notify(PROTECTION_TITLE, PROTECTION_MESSAGE)


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

    assert "Open Trader｜行动通知" in body
    assert "今日结论：有 1 条可采取行动，需人工确认后执行。" in body
    assert "标的：RKLB｜指示：加仓 80 股｜优先级：高" in body
    assert "当前价：109" in body
    assert "触发价：102" in body
    assert "预计金额：美元 8720" in body
    assert "影响：当前数量 120 股、当前仓位 1.36%；执行后数量 200 股、仓位 2.20%、成本 104.32。" in body
    assert "风控：硬止损 94，止损风险 美元 3000。" in body
    assert "原因：价格进入计划买入区间。" in body
    assert "USD" not in body
    assert "reports/" not in body
    assert "price entered entry zone" not in body


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

    assert "今日结论：暂无可采取行动。" in body
    assert "标的：MSFT｜指示：人工复核｜优先级：高" in body
    assert "阻塞：执行前缺少当前成本、交易后数量、交易后仓位、交易后成本、止损风险。" in body
    assert "影响：系统无法计算精确数量、金额、交易后仓位或风险，暂不能执行。" in body


def test_render_feishu_order_review_keeps_ready_sell_stop_with_blank_limit_price(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="MSFT",
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

    assert "标的：MSFT｜指示：止损卖出 10 股｜优先级：最高" in body
    assert "触发价：339" in body
    assert "当前价：339" in body
    assert "风控：硬止损 340，止损风险 全部退出。" in body
    assert "执行前缺少" not in body


def test_render_feishu_order_review_keeps_ready_trim_with_blank_cost_and_stop(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="VIXY",
                futu_symbol="US.VIXY",
                action="TRIM",
                priority="medium",
                last_price="21.7",
                trigger_status="target_1_hit",
                suggested_quantity="100",
                suggested_notional="2170",
                notional_currency="USD",
                current_quantity="200",
                current_weight="3.05%",
                avg_cost_price="0",
                limit_price="21.7",
                stop_price="",
                post_trade_quantity="100",
                post_trade_weight="1.366871988339962877122505175%",
                post_trade_avg_cost="",
                risk_to_stop="",
                reason="Current price is at or above target 1.",
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

    assert "今日结论：有 1 条可采取行动，需人工确认后执行。" in body
    assert "标的：VIXY｜指示：减仓 100 股｜优先级：中" in body
    assert "影响：当前数量 200 股、当前仓位 3.05%；执行后数量 100 股、仓位 1.37%。" in body
    assert "风控：" not in body
    assert "成本 。" not in body
    assert "硬止损 ，" not in body
    assert "暂不能行动" not in body
    assert "阻塞：" not in body


def test_render_feishu_order_review_shows_agent_reason_excerpt_and_neutral_trim_trigger(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="MRVL",
                futu_symbol="US.MRVL",
                action="TRIM",
                priority="medium",
                last_price="289.54",
                trigger_status="target_1_hit",
                suggested_quantity="5",
                suggested_notional="1447.7",
                notional_currency="USD",
                current_quantity="10",
                current_weight="1.29%",
                avg_cost_price="169.81",
                limit_price="289.54",
                stop_price="",
                post_trade_quantity="5",
                post_trade_weight="0.91%",
                post_trade_avg_cost="169.81",
                risk_to_stop="",
                agent_reason=(
                    "TradingAgents建议减仓，理由是估值或盈利质量风险上升。"
                ),
                agent_excerpt=(
                    "The bear demonstrated that normalized earnings imply a ~316x P/E."
                ),
                trigger_reason="Current price is at or above target 1.",
                reason="TradingAgents建议减仓，理由是估值或盈利质量风险上升。",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-18",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：TradingAgents建议减仓，理由是估值或盈利质量风险上升。" in body
    assert "原文：The bear demonstrated that normalized earnings imply a ~316x P/E." in body
    assert "触发：当前价 289.54，行动已满足计划中的减仓/风控条件。" in body
    assert "目标价 1" not in body
    assert "Current price is at or above target 1." not in body


def test_render_feishu_order_review_uses_chinese_fallback_for_english_only_agent_reason(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="NVDA",
                futu_symbol="US.NVDA",
                action="ADD",
                priority="high",
                last_price="120",
                suggested_quantity="10",
                suggested_notional="1200",
                notional_currency="USD",
                current_quantity="20",
                current_weight="1.00%",
                avg_cost_price="100",
                limit_price="118",
                stop_price="110",
                post_trade_quantity="30",
                post_trade_weight="1.50%",
                post_trade_avg_cost="106.67",
                risk_to_stop="100",
                agent_reason="The bull case remains intact.",
                agent_excerpt="The bull case remains intact.",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-18",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：TradingAgents建议加仓，需结合原文确认。" in body
    assert "原文：The bull case remains intact." in body
    assert "原因：The bull case remains intact." not in body


def test_render_feishu_order_review_keeps_cjk_reason_with_ascii_acronyms(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="MSFT",
                futu_symbol="US.MSFT",
                action="ADD",
                priority="high",
                agent_reason="微软AI商业化路径清晰。",
                agent_excerpt="微软AI商业化路径清晰。",
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-18",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：微软AI商业化路径清晰。" in body
    assert "原因：TradingAgents建议加仓，需结合原文确认。" not in body


def test_render_feishu_order_review_truncates_ready_rows_and_includes_reports(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(symbol="AAA", futu_symbol="US.AAA", priority="high", status="ready"),
            _action_row(symbol="BBB", futu_symbol="US.BBB", priority="medium", status="ready"),
            _action_row(symbol="CCC", futu_symbol="US.CCC", priority="low", status="ready"),
            _action_row(symbol="DDD", futu_symbol="US.DDD", priority="critical", status="review"),
            _action_row(symbol="EEE", futu_symbol="US.EEE", priority="low", status="watch"),
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

    assert "今日结论：有 3 条可采取行动，需人工确认后执行。" in body
    assert "标的：AAA｜指示：买入 5 股｜优先级：高" in body
    assert "标的：BBB｜指示：买入 5 股｜优先级：中" in body
    assert "标的：CCC｜指示：买入 5 股｜优先级：低" not in body
    assert "另有 1 条可采取行动未展开。" in body
    assert "另有 1 条需处理事项。" in body
    assert "观察中：1 条动作等待触发。" in body
    assert "reports/" not in body


def test_render_feishu_order_review_translates_review_errors_and_hides_paths(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="BOTZ",
                futu_symbol="US.BOTZ",
                priority="high",
                status="review",
                error="unparseable target max weight",
                avg_cost_price="",
                limit_price="",
                suggested_quantity="",
                suggested_notional="",
                post_trade_quantity="",
                post_trade_weight="",
                post_trade_avg_cost="",
                risk_to_stop="",
                stop_price="",
            ),
            _action_row(
                symbol="VIXY",
                futu_symbol="US.VIXY",
                priority="medium",
                status="ready",
                action="TRIM",
                reason="Current price is at or above target 1.",
                avg_cost_price="",
                post_trade_quantity="",
                post_trade_weight="",
                post_trade_avg_cost="",
                risk_to_stop="",
            ),
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="partial",
        actions_path=actions_path,
        report_paths=[
            Path("reports/trade_actions/2026-06-17.md"),
            Path("reports/daily_runs/2026-06-17.md"),
        ],
    )

    assert "今日结论：暂无可采取行动。" in body
    assert "标的：BOTZ｜指示：人工处理｜优先级：高" in body
    assert "阻塞：目标最大仓位无法解析。" in body
    assert "标的：VIXY｜指示：人工复核｜优先级：中" in body
    assert "阻塞：执行前缺少交易后数量、交易后仓位。" in body
    assert "影响：系统无法计算精确数量、金额、交易后仓位或风险，暂不能执行。" in body
    assert "原因：当前价格已满足计划触发条件。" in body
    assert "unparseable target max weight" not in body
    assert "Current price is at or above target 1." not in body
    assert "reports/" not in body


def test_render_feishu_order_review_translates_structured_sell_sizing_error(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="SOXX",
                futu_symbol="US.SOXX",
                priority="high",
                status="review",
                error="missing portfolio position for sell sizing",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-18",
        status="partial",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "阻塞：缺少持仓信息，无法计算卖出数量。" in body
    assert "missing portfolio position for sell sizing" not in body


def test_render_feishu_order_review_supports_legacy_rows_without_agent_fields(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "legacy_trade_actions.csv"
    legacy_fieldnames = [
        field
        for field in FIELDNAMES
        if field not in {"agent_reason", "agent_excerpt", "trigger_reason"}
    ]
    with actions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fieldnames)
        writer.writeheader()
        row = _action_row(
            symbol="RKLB",
            futu_symbol="US.RKLB",
            action="ADD",
            reason="price entered entry zone",
            status="ready",
        )
        writer.writerow({field: row[field] for field in legacy_fieldnames})

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：价格进入计划买入区间。" in body
    assert "原文依据缺失，需人工复核。" not in body
    assert "触发：价格进入计划买入区间。" not in body


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
        "agent_reason": "",
        "agent_excerpt": "",
        "trigger_reason": "",
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
