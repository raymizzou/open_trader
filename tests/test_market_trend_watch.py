from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from open_trader.a_share_trend import (
    AccountPosition,
    AccountSnapshot,
    write_protection_state,
)
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.market_trend_watch import (
    BROKER_LABELS,
    market_session,
    next_market_open,
    watch_market_protection as _watch_market_protection,
)
import open_trader.market_trend_watch as market_watch_module
from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    NullNotifier,
    XiaoaiSSHNotifier,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def watch_market_protection(**kwargs: object) -> object:
    if "account_loader" not in kwargs:
        state_path = Path(kwargs["state_path"])

        def account_loader(
            path: Path, *, expected_date: str, timezone: ZoneInfo
        ) -> AccountSnapshot:
            del path, timezone
            positions = tuple(
                AccountPosition(
                    symbol,
                    {"00700": "腾讯", "NVDA": "NVIDIA"}.get(symbol, symbol),
                    "stock",
                    Decimal("100"),
                    Decimal("10"),
                    Decimal("1000"),
                )
                for symbol in market_watch_module._load_active_lines(state_path)
            )
            return AccountSnapshot(
                source_date=expected_date,
                fresh=True,
                net_value=Decimal("100000"),
                available_cash=Decimal("100000"),
                positions=positions,
                exceptions=(),
            )

        kwargs["account_loader"] = account_loader
    return _watch_market_protection(**kwargs)


class RecordingXiaoaiNotifier(XiaoaiSSHNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class RecordingFeishuNotifier(FeishuWebhookNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FlakyOrderFeishuNotifier(RecordingFeishuNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.order_attempt_count = 0
        self.order_attempts: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        if "卖出失败" in title:
            self.order_attempt_count += 1
            self.order_attempts.append((title, message))
            if self.failures:
                self.failures -= 1
                raise RuntimeError("Feishu unavailable")
        super().notify(title, message)


class RecordingMacOSNotifier(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def watcher_error(message: str) -> FutuQuoteError:
    return FutuQuoteError(message, error_type="quote_server_interrupted")


def test_once_market_watcher_returns_abnormal_when_reconnect_client_fails(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=None,
        quote_client_factory=lambda: (_ for _ in ()).throw(
            watcher_error("reconnect offline")
        ),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1


def test_market_watcher_default_clock_records_aware_timestamps(
    tmp_path: Path,
) -> None:
    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise watcher_error("calendar offline")

        def close(self) -> None:
            pass

    events_path = tmp_path / "events.jsonl"

    watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=events_path,
        report_lock_path=tmp_path / "report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
    )

    event = json.loads(events_path.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(event["occurred_at"]).tzinfo is not None


def test_once_market_watcher_returns_abnormal_when_calendar_fails(
    tmp_path: Path,
) -> None:
    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise watcher_error("calendar offline")

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=quote,
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1
    assert quote.closed is True


def test_once_market_watcher_reraises_failure_for_borrowed_quote(
    tmp_path: Path,
) -> None:
    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise watcher_error("calendar offline")

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    with pytest.raises(FutuQuoteError, match="calendar offline"):
        watch_market_protection(
            market="HK",
            data_dir=tmp_path / "data",
            portfolio_path=tmp_path / "unused.csv",
            state_path=tmp_path / "state.json",
            events_path=tmp_path / "events.jsonl",
            report_lock_path=tmp_path / "report.lock",
            quote_client=quote,
            close_quote_client=False,
            notifier=NullNotifier(),
            poll_seconds=5,
            reconnect_seconds=5,
            once=True,
            now_fn=lambda: now,
        )

    assert quote.closed is False


def test_once_market_watcher_deduplicates_durable_interruption(
    tmp_path: Path,
) -> None:
    error = watcher_error("calendar offline")
    events_path = tmp_path / "events.jsonl"
    feishu = RecordingFeishuNotifier()
    macos = RecordingMacOSNotifier()
    notifier = CompositeNotifier([feishu, macos])
    now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise error

    for quote in (Quote(), Quote()):
        with pytest.raises(FutuQuoteError, match="calendar offline"):
            watch_market_protection(
                market="HK",
                data_dir=tmp_path / "data",
                portfolio_path=tmp_path / "unused.csv",
                state_path=tmp_path / "state.json",
                events_path=events_path,
                report_lock_path=tmp_path / "report.lock",
                quote_client=quote,
                close_quote_client=False,
                notifier=notifier,
                poll_seconds=5,
                reconnect_seconds=5,
                once=True,
                now_fn=lambda: now,
            )

    assert feishu.messages == []
    assert [title for title, _ in macos.messages] == ["港股价格监控中断"]
    assert [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] == ["monitor_interrupted"]


def test_once_market_watcher_returns_abnormal_when_snapshot_fails(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {"00700": {"active_line": "11"}},
    })

    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, _symbols: list[str]) -> dict[str, QuoteSnapshot]:
            raise watcher_error("snapshot offline")

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=quote,
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1
    assert quote.closed is True


def test_once_market_watcher_recovers_after_snapshot_outage_ends(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    events_path = tmp_path / "events.jsonl"
    feishu = RecordingFeishuNotifier()
    macos = RecordingMacOSNotifier()
    notifier = CompositeNotifier([feishu, macos])
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {"00700": {"active_line": "11"}},
    })

    class Quote:
        def __init__(self, snapshot: dict[str, QuoteSnapshot] | Exception) -> None:
            self.snapshot = snapshot

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-22"]

        def get_snapshots(self, _symbols: list[str]) -> dict[str, QuoteSnapshot]:
            if isinstance(self.snapshot, Exception):
                raise self.snapshot
            return self.snapshot

    now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    def watch(snapshot: dict[str, QuoteSnapshot] | Exception) -> object:
        return watch_market_protection(
            market="HK",
            data_dir=tmp_path / "data",
            portfolio_path=tmp_path / "unused.csv",
            state_path=state_path,
            events_path=events_path,
            report_lock_path=tmp_path / "report.lock",
            quote_client=Quote(snapshot),
            close_quote_client=False,
            notifier=notifier,
            poll_seconds=5,
            reconnect_seconds=5,
            once=True,
            now_fn=lambda: now,
        )

    for _ in range(2):
        with pytest.raises(FutuQuoteError, match="snapshot offline"):
            watch(watcher_error("snapshot offline"))

    assert feishu.messages == []
    assert [title for title, _ in macos.messages] == ["港股价格监控中断"]
    assert [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] == ["monitor_interrupted"]

    assert watch({"HK.00700": QuoteSnapshot("HK.00700", Decimal("12"))}).status == (
        "completed"
    )
    assert [title for title, _ in macos.messages] == [
        "港股价格监控中断",
        "港股价格监控恢复",
    ]
    assert [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ] == ["monitor_interrupted", "monitor_recovered"]


def test_once_market_watcher_returns_abnormal_when_account_snapshot_fails(
    tmp_path: Path,
) -> None:
    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def close(self) -> None:
            pass

    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        account_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("account snapshot offline")
        ),
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1


def test_once_market_watcher_returns_abnormal_when_failed_client_cannot_close(
    tmp_path: Path,
) -> None:
    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise watcher_error("calendar offline")

        def close(self) -> None:
            raise RuntimeError("close failed")

    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1


def test_once_market_completed_result_becomes_abnormal_when_close_fails(
    tmp_path: Path,
) -> None:
    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def close(self) -> None:
            raise RuntimeError("close failed")

    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    result = watch_market_protection(
        market="HK",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        account_loader=lambda _path, *, expected_date, timezone: AccountSnapshot(
            source_date=expected_date,
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        now_fn=lambda: now,
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
    )

    assert result.status == "abnormal"
    assert result.exception_count == 1


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


def test_closed_us_watcher_compensates_before_waiting_for_next_open(
    tmp_path: Path, monkeypatch,
) -> None:
    opens: list[str] = []

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-17", "2026-07-20"]

        def close(self) -> None:
            pass

    with pytest.raises(RuntimeError, match="stop before long wait"):
        watch_market_protection(
            market="US",
            data_dir=tmp_path / "data",
            portfolio_path=tmp_path / "unused.csv",
            state_path=tmp_path / "state.json",
            events_path=tmp_path / "events.jsonl",
            report_lock_path=tmp_path / "report.lock",
            quote_client=Quote(),
            notifier=NullNotifier(),
            poll_seconds=5,
            reconnect_seconds=60,
            now_fn=lambda: datetime(
                2026, 7, 17, 19, 0, tzinfo=ZoneInfo("America/New_York")
            ),
            sleep_fn=lambda seconds: (_ for _ in ()).throw(
                RuntimeError("stop before long wait")
            ),
            on_session_open=opens.append,
        )

    assert opens == ["2026-07-17"]


def test_once_weekend_watcher_returns_without_waiting_for_next_open(
    tmp_path: Path,
) -> None:
    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-27"]

    result = watch_market_protection(
        market="US",
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "unused.csv",
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        report_lock_path=tmp_path / "report.lock",
        quote_client=Quote(),
        close_quote_client=False,
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: datetime(
            2026, 7, 25, 10, 30, tzinfo=ZoneInfo("America/New_York")
        ),
        sleep_fn=lambda _seconds: pytest.fail("once market watcher slept"),
        account_loader=lambda *_args, **_kwargs: pytest.fail(
            "weekend account was loaded"
        ),
    )

    assert result.status == "completed"
    assert result.watched_symbol_count == 0
    assert result.trigger_count == 0


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
    account_calls: list[str] = []

    def load_simulation_account(
        path: Path, *, expected_date: str, timezone: ZoneInfo
    ) -> AccountSnapshot:
        del path, timezone
        account_calls.append(expected_date)
        return AccountSnapshot(
            source_date=expected_date,
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("50000"),
            positions=(
                AccountPosition(
                    "00700", "腾讯", "stock", Decimal("100"),
                    Decimal("400"), Decimal("50000"),
                ),
            ),
            exceptions=(),
        )

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
        account_loader=load_simulation_account,
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
    assert account_calls == ["2026-07-16", "2026-07-16"]
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
        "trend_review_callback_failure_notified",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
        "trend_review_callback_failure_notification_attempted_feishu",
        "trend_review_callback_failure_notification_delivered_feishu",
    ]
    assert events[1]["reason"] == "simulate order failed"


def test_protection_callback_failures_group_once_per_watcher_iteration(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    events_path = data_dir / "trend_hk_phillips/watch_events.jsonl"
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {
            "00700": {"active_line": "11"},
            "00941": {"active_line": "11"},
        },
    })

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            return {
                symbol: QuoteSnapshot(symbol, Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    feishu = RecordingFeishuNotifier()
    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=events_path,
        report_lock_path=None,
        quote_client=Quote(),
        notifier=feishu,
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        on_protection_trigger=lambda _event: (_ for _ in ()).throw(
            RuntimeError("simulate order failed")
        ),
    )

    callback_events = [
        event
        for event in (
            json.loads(line) for line in events_path.read_text().splitlines()
        )
        if event["event_type"] == "trend_review_callback_failed"
    ]
    order_messages = [
        (title, message)
        for title, message in feishu.messages
        if "卖出失败" in title
    ]
    assert result.exception_count == 2
    assert len(callback_events) == 2
    assert [title for title, _ in order_messages] == [
        "【需处理｜辉立｜港股卖出失败｜2026-07-16】"
    ]
    assert "- 00700" in order_messages[0][1]
    assert "- 00941" in order_messages[0][1]


def test_protection_callback_group_exhausts_after_next_watcher_iteration(
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

    feishu = FlakyOrderFeishuNotifier(failures=2)
    for _ in range(3):
        watch_market_protection(
            market="HK",
            data_dir=data_dir,
            portfolio_path=tmp_path / "unused.csv",
            state_path=state_path,
            events_path=events_path,
            report_lock_path=None,
            quote_client=Quote(),
            notifier=feishu,
            poll_seconds=5,
            reconnect_seconds=60,
            once=True,
            now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            sleep_fn=lambda seconds: None,
            on_protection_trigger=lambda _event: (_ for _ in ()).throw(
                RuntimeError("simulate order failed")
            ),
        )

    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert feishu.order_attempt_count == 2
    assert any(
        event["event_type"]
        == "trend_review_callback_failure_notification_exhausted_feishu"
        for event in events
    )
    assert not any(
        event["event_type"]
        == "trend_review_callback_failure_notification_delivered_feishu"
        for event in events
    )


@pytest.mark.parametrize(
    ("second_subset_failure", "expected_attempt_count"),
    [(False, 2), (True, 3)],
    ids=("callbacks-recover", "different-subset-fails"),
)
def test_failed_callback_group_retries_frozen_payload(
    tmp_path: Path,
    second_subset_failure: bool,
    expected_attempt_count: int,
) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    events_path = data_dir / "trend_hk_phillips/watch_events.jsonl"
    write_protection_state(state_path, {
        "schema_version": 1,
        "positions": {
            "00700": {"active_line": "11"},
            "00941": {"active_line": "11"},
        },
    })

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            return {
                symbol: QuoteSnapshot(symbol, Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    callback_count = 0

    def fail_first_iteration(_event: object) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count <= 2 or (
            second_subset_failure and callback_count == 3
        ):
            raise RuntimeError("simulate order failed")

    feishu = FlakyOrderFeishuNotifier(failures=1)
    macos = RecordingMacOSNotifier()
    for _ in range(2):
        watch_market_protection(
            market="HK",
            data_dir=data_dir,
            portfolio_path=tmp_path / "unused.csv",
            state_path=state_path,
            events_path=events_path,
            report_lock_path=None,
            quote_client=Quote(),
            notifier=CompositeNotifier([feishu, macos]),
            poll_seconds=5,
            reconnect_seconds=60,
            once=True,
            now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            sleep_fn=lambda seconds: None,
            on_protection_trigger=fail_first_iteration,
        )

    assert feishu.order_attempt_count == expected_attempt_count
    assert feishu.order_attempts[0] == feishu.order_attempts[1]
    assert "- 00700" in feishu.order_attempts[1][1]
    assert "- 00941" in feishu.order_attempts[1][1]
    assert sum(
        title == "趋势模拟执行失败 · 2026-07-16"
        for title, _ in macos.messages
    ) == 1


def test_deadline_group_retries_frozen_payload_after_ledger_changes(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    events_path = data_dir / "trend_hk_phillips/watch_events.jsonl"
    write_protection_state(state_path, {"schema_version": 1, "positions": {}})
    action_path = (
        data_dir
        / "trend_review/ledgers/HK/actions/2026-07-16/00700-buy/first.json"
    )
    action_path.parent.mkdir(parents=True)
    action_path.write_text(
        json.dumps({"symbol": "00700", "side": "buy", "status": "incomplete"}),
        encoding="utf-8",
    )

    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, _symbols: list[str]) -> dict[str, QuoteSnapshot]:
            return {}

        def close(self) -> None:
            pass

    class FlakyDeadlineFeishu(RecordingFeishuNotifier):
        def __init__(self) -> None:
            super().__init__()
            self.attempts: list[tuple[str, str]] = []

        def notify(self, title: str, message: str) -> None:
            self.attempts.append((title, message))
            if len(self.attempts) == 1:
                raise RuntimeError("Feishu unavailable")
            super().notify(title, message)

    feishu = FlakyDeadlineFeishu()

    def watch() -> None:
        watch_market_protection(
            market="HK",
            data_dir=data_dir,
            portfolio_path=tmp_path / "unused.csv",
            state_path=state_path,
            events_path=events_path,
            report_lock_path=None,
            quote_client=Quote(),
            notifier=feishu,
            poll_seconds=5,
            reconnect_seconds=60,
            once=True,
            now_fn=lambda: datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI),
            sleep_fn=lambda _seconds: None,
            on_session_open=lambda _date: None,
        )

    watch()
    action_path.write_text(
        json.dumps({"symbol": "00700", "side": "buy", "status": "filled"}),
        encoding="utf-8",
    )
    watch()
    watch()

    assert len(feishu.attempts) == 2
    assert feishu.attempts[0] == feishu.attempts[1]
    assert len(feishu.messages) == 1


def test_directionless_callback_failures_share_stable_batch_identity(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "watch_events.jsonl"
    feishu = RecordingFeishuNotifier()
    macos = RecordingMacOSNotifier()
    notifier = CompositeNotifier([feishu, macos])
    now = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)

    for reason in ("批次异常一", "批次异常二"):
        market_watch_module._run_review_callback(
            lambda _value, _reason=reason: (_ for _ in ()).throw(
                RuntimeError(_reason)
            ),
            "2026-07-16",
            events_path=events_path,
            trading_date="2026-07-16",
            now=now,
            notifier=notifier,
            market="HK",
            market_label="港股",
            broker_label="辉立",
        )

    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    attempts = [
        event
        for event in events
        if event["event_type"]
        == "trend_review_callback_failure_notification_attempted_feishu"
    ]
    expected_group_id = hashlib.sha256(
        b"2026-07-16|HK|failed"
    ).hexdigest()
    assert [title for title, _ in feishu.messages] == [
        "【需处理｜辉立｜港股批次执行失败｜2026-07-16】"
    ]
    assert len(attempts) == 1
    assert attempts[0]["group_id"] == expected_group_id


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

    feishu = RecordingFeishuNotifier()
    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=events_path,
        report_lock_path=None,
        quote_client=Quote(),
        notifier=CompositeNotifier([feishu, RecordingMacOSNotifier()]),
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
    assert [event["event_type"] for event in events] == [
        "trend_review_callback_failed",
        "trend_review_callback_failure_notified",
        "trend_review_callback_failure_notification_attempted_feishu",
        "trend_review_callback_failure_notification_delivered_feishu",
    ]
    assert events[0]["reason"] == "review open failed"
    assert [title for title, _ in feishu.messages] == [
        "【需处理｜辉立｜港股批次执行失败｜2026-07-16】"
    ]


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
