from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from open_trader.a_share_trend import write_protection_state
from open_trader.futu_watch import QuoteSnapshot
from open_trader.market_trend_watch import (
    BROKER_LABELS,
    market_session,
    next_market_open,
    watch_market_protection,
)
from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    NullNotifier,
    XiaoaiSSHNotifier,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class RecordingXiaoaiNotifier(XiaoaiSSHNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class RecordingFeishuNotifier(FeishuWebhookNotifier):
    def __init__(self) -> None:
        pass

    def notify(self, title: str, message: str) -> None:
        pass


class RecordingMacOSNotifier(MacOSNotifier):
    def notify(self, title: str, message: str) -> None:
        pass


def test_hk_regular_sessions_exclude_lunch_and_auction() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    assert market_session(datetime(2026, 7, 16, 9, 29, tzinfo=hk), "HK") == "before"
    assert market_session(datetime(2026, 7, 16, 9, 30, tzinfo=hk), "HK") == "morning"
    assert market_session(datetime(2026, 7, 16, 12, 0, tzinfo=hk), "HK") == "morning"
    assert market_session(datetime(2026, 7, 16, 12, 1, tzinfo=hk), "HK") == "lunch"
    assert market_session(datetime(2026, 7, 16, 16, 0, tzinfo=hk), "HK") == "afternoon"
    assert market_session(datetime(2026, 7, 16, 16, 1, tzinfo=hk), "HK") == "closed"


def test_us_regular_session_is_new_york_dst_aware() -> None:
    summer = datetime(2026, 7, 16, 21, 30, tzinfo=SHANGHAI)
    winter = datetime(2026, 12, 16, 22, 30, tzinfo=SHANGHAI)
    assert market_session(summer, "US") == "open"
    assert market_session(winter, "US") == "open"
    assert market_session(datetime(2026, 7, 17, 4, 1, tzinfo=SHANGHAI), "US") == "closed"


def test_next_market_open_waits_from_early_report_until_next_session() -> None:
    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15", "2026-07-16", "2026-07-17"]

    hk_open = next_market_open(
        Quote(),
        market="HK",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SHANGHAI),
    )
    us_open = next_market_open(
        Quote(),
        market="US",
        now=datetime(2026, 7, 15, 9, 0, tzinfo=SHANGHAI),
    )

    assert hk_open == datetime(2026, 7, 16, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    assert us_open == datetime(2026, 7, 15, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def _write_hk_details(data_dir: Path) -> None:
    run_dir = data_dir / "runs/2026-06"
    run_dir.mkdir(parents=True)
    with (run_dir / "extracted_positions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "statement_id", "broker", "market", "asset_class", "symbol", "name",
            "currency", "quantity", "cost_price", "market_value",
        ])
        writer.writeheader()
        writer.writerow({
            "statement_id": "2026-06-phillips", "broker": "phillips", "market": "HK",
            "asset_class": "stock", "symbol": "700", "name": "腾讯", "currency": "HKD",
            "quantity": "100", "cost_price": "400", "market_value": "50000",
        })
    with (run_dir / "extracted_cash.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "statement_id", "broker", "currency", "cash_balance", "available_balance",
        ])
        writer.writeheader()
        writer.writerow({
            "statement_id": "2026-06-phillips", "broker": "phillips", "currency": "HKD",
            "cash_balance": "10000", "available_balance": "10000",
        })


def _write_us_details(data_dir: Path) -> None:
    run_dir = data_dir / "runs/2026-07-15"
    run_dir.mkdir(parents=True)
    (run_dir / "tiger_account_snapshot.json").write_text(json.dumps({
        "accounts": [],
        "cash_records": [
            {"record_type": "account_total", "currency": "USD", "account_total": "2500"},
            {"currency": "USD", "cash_balance": "1000", "available_balance": "1000"},
        ],
        "position_records": [{
            "market": "US", "sec_type": "STK", "symbol": "NVDA", "name": "NVIDIA",
            "currency": "USD", "position_qty": "10", "average_cost": "140",
            "market_value": "1500",
        }],
    }), encoding="utf-8")


def test_market_watcher_uses_hk_account_and_triggers_once(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    write_protection_state(state_path, {
        "schema_version": 1,
        "managed_symbols": ["00700"],
        "positions": {
            "00700": {
                "initial_line": "11", "active_line": "11", "atr14": "1",
                "position_started_for": "2026-07-15", "tracking_active": False,
                "updated_for": "2026-07-15",
            }
        },
    })

    class Quote:
        host = "127.0.0.1"
        port = 11111

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            assert symbols == ["HK.00700"]
            return {"HK.00700": QuoteSnapshot("HK.00700", Decimal("10"))}

        def close(self) -> None:
            pass

    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    voice = RecordingXiaoaiNotifier()
    opens: list[str] = []
    stops: list[object] = []
    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=data_dir / "trend_hk_phillips/watch_events.jsonl",
        report_lock_path=data_dir / "runs/.trend_hk_phillips_report.lock",
        quote_client=Quote(),
        notifier=CompositeNotifier([NullNotifier(), voice]),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda seconds: None,
        on_session_open=opens.append,
        on_protection_trigger=stops.append,
    )

    assert result.status == "completed"
    assert result.watched_symbol_count == 1
    assert result.trigger_count == 1
    assert opens == ["2026-07-16"]
    assert len(stops) == 1
    assert voice.messages == [
        (
            "港股保护线触发 · 00700",
            "名称：腾讯\n最新价 10 <= 活动保护线 11\n建议动作：全部卖出（人工执行）",
        )
    ]


def test_review_callback_failure_is_recorded_without_blocking_protection_notice(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    events_path = data_dir / "trend_hk_phillips/watch_events.jsonl"
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {"00700": {"active_line": "11"}},
    })

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            return {"HK.00700": QuoteSnapshot("HK.00700", Decimal("10"))}

        def close(self) -> None:
            pass

    def fail_review(event: object) -> None:
        raise RuntimeError("simulate order failed")

    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=events_path,
        report_lock_path=None,
        quote_client=Quote(),
        notifier=CompositeNotifier([
            RecordingFeishuNotifier(), RecordingMacOSNotifier(),
        ]),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        on_protection_trigger=fail_review,
    )

    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert result.status == "completed"
    assert result.exception_count == 1
    assert [event["event_type"] for event in events] == [
        "protection_triggered",
        "trend_review_callback_failed",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]
    assert events[1]["reason"] == "simulate order failed"


def test_session_review_callback_failure_does_not_stop_watcher(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    events_path = data_dir / "trend_hk_phillips/watch_events.jsonl"
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {"00700": {"active_line": "11"}},
    })

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            return {"HK.00700": QuoteSnapshot("HK.00700", Decimal("12"))}

        def close(self) -> None:
            pass

    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=events_path,
        report_lock_path=None,
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        on_session_open=lambda trading_date: (_ for _ in ()).throw(
            RuntimeError("review open failed")
        ),
    )

    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert result.status == "completed"
    assert result.exception_count == 1
    assert events[0]["event_type"] == "trend_review_callback_failed"
    assert events[0]["reason"] == "review open failed"


def test_market_watcher_uses_us_account_and_queues_voice(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_us_details(data_dir)
    state_path = data_dir / "trend_us_tiger/protection_state.json"
    write_protection_state(
        state_path,
        {
            "schema_version": 1,
            "managed_symbols": ["NVDA"],
            "positions": {
                "NVDA": {
                    "initial_line": "151",
                    "active_line": "151",
                    "atr14": "1",
                    "position_started_for": "2026-07-15",
                    "tracking_active": False,
                    "updated_for": "2026-07-15",
                }
            },
        },
    )

    class Quote:
        host = "127.0.0.1"
        port = 11111

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            assert symbols == ["US.NVDA"]
            return {"US.NVDA": QuoteSnapshot("US.NVDA", Decimal("150"))}

        def close(self) -> None:
            pass

    voice = RecordingXiaoaiNotifier()
    now = datetime(2026, 7, 15, 22, 0, tzinfo=SHANGHAI)
    result = watch_market_protection(
        market="US",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=data_dir / "trend_us_tiger/watch_events.jsonl",
        report_lock_path=data_dir / "runs/.trend_us_tiger_report.lock",
        quote_client=Quote(),
        notifier=CompositeNotifier([NullNotifier(), voice]),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda seconds: None,
    )

    assert result.status == "completed"
    assert voice.messages[0][0] == "美股保护线触发 · NVDA"
    assert voice.messages[0][1].startswith("名称：NVIDIA\n最新价 ")


def test_us_watcher_ignores_unmanaged_tiger_holdings_without_protection_seed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_us_details(data_dir)
    state_path = data_dir / "trend_us_tiger/protection_state.json"

    class Quote:
        host = "127.0.0.1"
        port = 11111

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15"]

        def close(self) -> None:
            pass

    result = watch_market_protection(
        market="US",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=data_dir / "trend_us_tiger/watch_events.jsonl",
        report_lock_path=data_dir / "runs/.trend_us_tiger_report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: datetime(2026, 7, 15, 22, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
    )

    assert result.watched_symbol_count == 0
    assert result.exception_count == 0


def test_us_watcher_uses_tiger_label() -> None:
    assert BROKER_LABELS["US"] == "老虎"
