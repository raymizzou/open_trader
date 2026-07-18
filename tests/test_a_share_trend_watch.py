from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from collections.abc import Callable, Mapping

import pytest

from open_trader.a_share_trend_watch import (
    _deliver_trigger_notification,
    _notify_trend_review_deadline,
    append_watch_event,
    watch_a_share_protection,
)
from open_trader.daily_premarket import RunLock
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    NullNotifier,
    XiaoaiSSHNotifier,
    XiaoaiVoiceSuppressed,
)


class SequenceClock:
    def __init__(self, values: list[str]) -> None:
        self.values = [datetime.fromisoformat(value) for value in values]

    def __call__(self) -> datetime:
        if not self.values:
            raise AssertionError("watcher requested an unexpected clock value")
        return self.values.pop(0)


class SequenceQuote:
    def __init__(
        self,
        snapshots: list[dict[str, Decimal] | Exception],
        *,
        trading_days: list[str] | Exception | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.trading_days = (
            ["2026-07-15"] if trading_days is None else trading_days
        )
        self.calendar_calls: list[tuple[str, str]] = []
        self.snapshot_calls: list[list[str]] = []
        self.closed = False

    def get_cn_trading_days(self, *, start: str, end: str) -> list[str]:
        self.calendar_calls.append((start, end))
        if isinstance(self.trading_days, Exception):
            raise self.trading_days
        return self.trading_days

    def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        self.snapshot_calls.append(symbols)
        if not self.snapshots:
            raise AssertionError("watcher requested an unexpected snapshot")
        response = self.snapshots.pop(0)
        if isinstance(response, Exception):
            raise response
        return {
            symbol: QuoteSnapshot(futu_symbol=symbol, last_price=price)
            for symbol, price in response.items()
        }

    def close(self) -> None:
        self.closed = True


class RecordingNotifier(FeishuWebhookNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class RecordingMacOSNotifier(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FlakyNotifier(RecordingNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempt_count = 0

    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery failed")
        super().notify(title, message)


class FlakyMacOSNotifier(RecordingMacOSNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempt_count = 0

    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery failed")
        super().notify(title, message)


class RecordingXiaoaiNotifier(XiaoaiSSHNotifier):
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail
        self.attempt_count = 0

    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        if self.fail:
            raise RuntimeError("ssh failed")
        self.messages.append((title, message))


class SuppressedXiaoaiNotifier(RecordingXiaoaiNotifier):
    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        raise XiaoaiVoiceSuppressed("quiet hours")


def portfolio(
    tmp_path: Path,
    *,
    symbol: str | None = "600900",
    name: str = "长江电力",
    asset_class: str = "stock",
) -> Path:
    path = tmp_path / "portfolio.csv"
    fieldnames = [
        "brokers",
        "market",
        "currency",
        "asset_class",
        "symbol",
        "name",
        "total_quantity",
        "avg_cost_price",
        "market_value",
    ]
    rows = []
    if symbol is not None:
        rows.append(
            {
                "brokers": "eastmoney",
                "market": "CN",
                "currency": "CNY",
                "asset_class": asset_class,
                "symbol": symbol,
                "name": name,
                "total_quantity": "100",
                "avg_cost_price": "28",
                "market_value": "2800",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def state(
    tmp_path: Path,
    *,
    symbol: str = "600900",
    active_line: str | None = "27.31",
) -> Path:
    path = tmp_path / "protection_state.json"
    position: dict[str, object] = {
        "initial_line": "27.31",
        "atr14": "0.5",
        "updated_for": "2026-07-14",
    }
    if active_line is not None:
        position["active_line"] = active_line
    path.write_text(
        json.dumps(
            {"schema_version": 1, "positions": {symbol: position}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def run_once(
    tmp_path: Path,
    *,
    quote: SequenceQuote,
    now: str = "2026-07-15T09:30:00+08:00",
    portfolio_path: Path | None = None,
    state_path: Path | None = None,
    events_path: Path | None = None,
    notifier: RecordingNotifier | None = None,
    on_session_open: Callable[[str], None] | None = None,
    on_protection_trigger: Callable[[Mapping[str, object]], None] | None = None,
) -> object:
    return watch_a_share_protection(
        portfolio_path=portfolio_path or portfolio(tmp_path),
        state_path=state_path or state(tmp_path),
        events_path=events_path or tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=notifier or RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock([now]),
        sleep_fn=lambda seconds: None,
        on_session_open=on_session_open,
        on_protection_trigger=on_protection_trigger,
    )


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def interrupted(message: str = "网络中断") -> FutuQuoteError:
    return FutuQuoteError(message, error_type="quote_server_interrupted")


def test_watcher_calls_review_open_and_stop_hooks_once(tmp_path: Path) -> None:
    opens: list[str] = []
    stops: list[Mapping[str, object]] = []

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        on_session_open=opens.append,
        on_protection_trigger=stops.append,
    )

    assert opens == ["2026-07-15"]
    assert len(stops) == 1
    assert stops[0]["event_type"] == "protection_triggered"
    assert stops[0]["event_id"]


def test_watcher_retries_review_callback_on_next_poll(tmp_path: Path) -> None:
    attempts: list[str] = []

    def review(trading_date: str) -> None:
        attempts.append(trading_date)
        if len(attempts) == 1:
            raise RuntimeError("temporary review failure")

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path, active_line="20"),
        events_path=tmp_path / "events.jsonl",
        quote_client=SequenceQuote(
            [
                {"SH.600900": Decimal("27.30")},
                {"SH.600900": Decimal("27.20")},
            ]
        ),
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=lambda seconds: None,
        on_session_open=review,
    )

    assert result.status == "closed"
    assert attempts == ["2026-07-15", "2026-07-15", "2026-07-15"]


def test_closed_trading_day_runs_compensation_before_exit(tmp_path: Path) -> None:
    opens: list[str] = []

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=SequenceQuote([], trading_days=["2026-07-15"]),
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(["2026-07-15T15:01:00+08:00"]),
        sleep_fn=lambda seconds: None,
        on_session_open=opens.append,
    )

    assert result.status == "closed"
    assert opens == ["2026-07-15"]


def test_trend_review_deadline_notification_is_sent_once(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    action = (
        data_dir
        / "trend_review/ledgers/CN/actions/2026-07-15/key"
        / "2026-07-15T09-31-00+08-00.json"
    )
    action.parent.mkdir(parents=True)
    action.write_text(
        json.dumps(
            {
                "symbol": "600900",
                "status": "submitted",
                "recorded_at": "2026-07-15T09:31:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    events = data_dir / "trend_a_share/watch_events.jsonl"
    notifier = RecordingNotifier()

    _notify_trend_review_deadline(
        data_dir=data_dir,
        market="CN",
        trading_date="2026-07-15",
        now=datetime.fromisoformat("2026-07-15T09:59:00+08:00"),
        events_path=events,
        notifier=notifier,
    )
    for _ in range(2):
        _notify_trend_review_deadline(
            data_dir=data_dir,
            market="CN",
            trading_date="2026-07-15",
            now=datetime.fromisoformat("2026-07-15T10:00:00+08:00"),
            events_path=events,
            notifier=notifier,
        )

    assert notifier.messages == [
        ("趋势模拟执行未完成 · 2026-07-15", "600900 · 已提交")
    ]
    assert read_events(events)[0]["event_type"] == "trend_review_deadline_notified"


def test_watcher_alerts_once_per_symbol_per_day(tmp_path: Path) -> None:
    quote = SequenceQuote(
        [
            {"SH.600900": Decimal("27.30")},
            {"SH.600900": Decimal("27.20")},
        ]
    )
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path, symbol="600900"),
        state_path=state(tmp_path, symbol="600900", active_line="27.31"),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=CompositeNotifier([feishu, macos]),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=lambda seconds: None,
    )

    assert result.trigger_count == 1
    assert sum("全部卖出" in message for _, message in feishu.messages) == 1
    assert len(macos.messages) == 1
    events = read_events(tmp_path / "events.jsonl")
    assert [event["event_type"] for event in events] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]
    assert set(events[0]) == {
        "event_id",
        "symbol",
        "trading_date",
        "event_type",
        "occurred_at",
        "last_price",
        "active_line",
    }


def test_trigger_queues_one_voice_alert_with_name(tmp_path: Path) -> None:
    voice = RecordingXiaoaiNotifier()
    events_path = tmp_path / "events.jsonl"

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([RecordingNotifier(), voice]),
    )

    assert voice.messages == [
        (
            "A股保护线触发 · 600900",
            "名称：长江电力\n最新价 27.30 <= 活动保护线 27.31\n建议动作：全部卖出（人工执行）",
        )
    ]
    assert read_events(events_path)[-1]["event_type"].endswith("queued_xiaoai")


def test_voice_failure_is_terminal_and_warns_feishu(tmp_path: Path) -> None:
    voice = RecordingXiaoaiNotifier(fail=True)
    feishu = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([feishu, voice]),
    )

    assert voice.attempt_count == 1
    assert sum("语音播报失败" in title for title, _ in feishu.messages) == 1
    assert read_events(events_path)[-1]["reason"] == "音箱连接或播放失败"


def test_restart_never_replays_voice(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    first = RecordingXiaoaiNotifier(fail=True)
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([RecordingNotifier(), first]),
    )
    restarted = RecordingXiaoaiNotifier()

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.20")}]),
        events_path=events_path,
        notifier=CompositeNotifier([RecordingNotifier(), restarted]),
    )

    assert first.attempt_count == 1
    assert restarted.attempt_count == 0


def test_trigger_quiet_hours_suppresses_voice_without_failure_warning(
    tmp_path: Path,
) -> None:
    voice = SuppressedXiaoaiNotifier()
    feishu = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    _deliver_trigger_notification(
        events_path=events_path,
        notifier=CompositeNotifier([feishu, voice]),
        trading_date="2026-07-15",
        now=datetime.fromisoformat("2026-07-15T23:00:00+08:00"),
        symbol="600900",
        position_name="长江电力",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
        delivered_feishu=set(),
        delivered_macos=set(),
        replay=False,
    )

    assert voice.messages == []
    assert voice.attempt_count == 1
    assert not any("语音播报失败" in title for title, _ in feishu.messages)
    assert read_events(events_path)[-1]["event_type"].endswith(
        "suppressed_quiet_hours_xiaoai"
    )


def test_voice_suppressed_after_lock_wait_does_not_warn_feishu(
    tmp_path: Path,
) -> None:
    voice = SuppressedXiaoaiNotifier()
    feishu = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    _deliver_trigger_notification(
        events_path=events_path,
        notifier=CompositeNotifier([feishu, voice]),
        trading_date="2026-07-15",
        now=datetime.fromisoformat("2026-07-15T22:59:59+08:00"),
        symbol="600900",
        position_name="长江电力",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
        delivered_feishu=set(),
        delivered_macos=set(),
        replay=False,
    )

    assert voice.attempt_count == 1
    assert not any("语音播报失败" in title for title, _ in feishu.messages)
    assert read_events(events_path)[-1]["event_type"].endswith(
        "suppressed_quiet_hours_xiaoai"
    )


def test_voice_and_feishu_failure_never_recurse(tmp_path: Path) -> None:
    voice = RecordingXiaoaiNotifier(fail=True)
    feishu = FlakyNotifier(failures=10)

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        notifier=CompositeNotifier([feishu, voice]),
    )

    assert voice.attempt_count == 1
    assert feishu.attempt_count == 2


def test_held_symbol_can_speak_again_on_next_trading_date(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    first = RecordingXiaoaiNotifier()
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([RecordingNotifier(), first]),
    )
    next_day = RecordingXiaoaiNotifier()

    run_once(
        tmp_path,
        now="2026-07-16T09:30:00+08:00",
        quote=SequenceQuote(
            [{"SH.600900": Decimal("27.20")}], trading_days=["2026-07-16"]
        ),
        events_path=events_path,
        notifier=CompositeNotifier([RecordingNotifier(), next_day]),
    )

    assert first.attempt_count == 1
    assert next_day.attempt_count == 1


@pytest.mark.parametrize(
    ("symbol", "name", "futu_symbol"),
    [
        ("920000", "北交所持仓", "BJ.920000"),
        ("600001", "*ST持仓", "SH.600001"),
    ],
)
def test_watcher_monitors_every_current_cn_holding_even_if_not_candidate_eligible(
    tmp_path: Path, symbol: str, name: str, futu_symbol: str
) -> None:
    quote = SequenceQuote([{futu_symbol: Decimal("10")}])

    result = run_once(
        tmp_path,
        quote=quote,
        portfolio_path=portfolio(tmp_path, symbol=symbol, name=name),
        state_path=state(tmp_path, symbol=symbol, active_line="9"),
    )

    assert result.watched_symbol_count == 1
    assert quote.snapshot_calls == [[futu_symbol]]


def test_holiday_exits_silently(tmp_path: Path) -> None:
    quote = SequenceQuote([], trading_days=[])
    notifier = RecordingNotifier()

    result = run_once(tmp_path, quote=quote, notifier=notifier)

    assert result.status == "holiday"
    assert result.trigger_count == 0
    assert quote.snapshot_calls == []
    assert notifier.messages == []
    assert quote.closed is True


def test_holiday_does_not_require_portfolio_or_state_files(tmp_path: Path) -> None:
    quote = SequenceQuote([], trading_days=[])

    result = watch_a_share_protection(
        portfolio_path=tmp_path / "missing-portfolio.csv",
        state_path=tmp_path / "missing-state.json",
        events_path=tmp_path / "missing-events.jsonl",
        quote_client=quote,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(["2026-07-15T09:30:00+08:00"]),
        sleep_fn=lambda seconds: None,
    )

    assert result.status == "holiday"
    assert result.watched_symbol_count == 0


def test_watcher_waits_from_0925_until_open(tmp_path: Path) -> None:
    quote = SequenceQuote([{"SH.600900": Decimal("28")}])
    sleeps: list[float] = []

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T09:25:00+08:00", "2026-07-15T09:30:00+08:00"]
        ),
        sleep_fn=sleeps.append,
    )

    assert result.status == "completed"
    assert sleeps == [300.0]
    assert len(quote.snapshot_calls) == 1


def test_watcher_pauses_for_lunch(tmp_path: Path) -> None:
    quote = SequenceQuote([{"SH.600900": Decimal("28")}])
    sleeps: list[float] = []

    watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T11:31:00+08:00", "2026-07-15T13:00:00+08:00"]
        ),
        sleep_fn=sleeps.append,
    )

    assert sleeps == [5340.0]
    assert len(quote.snapshot_calls) == 1


def test_watcher_stops_after_1500_without_polling(tmp_path: Path) -> None:
    quote = SequenceQuote([])

    result = run_once(tmp_path, quote=quote, now="2026-07-15T15:00:01+08:00")

    assert result.status == "closed"
    assert quote.snapshot_calls == []
    assert quote.closed is True


def test_symbol_absent_from_latest_portfolio_is_not_watched(tmp_path: Path) -> None:
    quote = SequenceQuote([])

    result = run_once(
        tmp_path,
        quote=quote,
        portfolio_path=portfolio(tmp_path, symbol=None),
        state_path=state(tmp_path, symbol="600900"),
    )

    assert result.watched_symbol_count == 0
    assert result.trigger_count == 0
    assert quote.snapshot_calls == []


@pytest.mark.parametrize("initial_state", ["empty_portfolio", "missing_line"])
def test_watcher_without_comparable_symbols_keeps_polling_for_updates(
    tmp_path: Path, initial_state: str
) -> None:
    portfolio_path = portfolio(
        tmp_path, symbol=None if initial_state == "empty_portfolio" else "600900"
    )
    state_path = state(
        tmp_path, active_line=None if initial_state == "missing_line" else "27.31"
    )
    quote = SequenceQuote([{"SH.600900": Decimal("28")}])
    sleeps: list[float] = []

    def make_comparable(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) != 1:
            return
        if initial_state == "empty_portfolio":
            portfolio(tmp_path, symbol="600900")
        else:
            state(tmp_path, active_line="27.31")

    result = watch_a_share_protection(
        portfolio_path=portfolio_path,
        state_path=state_path,
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=make_comparable,
    )

    assert result.status == "closed"
    assert quote.snapshot_calls == [["SH.600900"]]
    assert sleeps == [5, 5]


def test_position_removed_after_start_is_not_watched_again(tmp_path: Path) -> None:
    portfolio_path = portfolio(tmp_path)
    quote = SequenceQuote([{"SH.600900": Decimal("28")}])

    def remove_position(_: float) -> None:
        portfolio(tmp_path, symbol=None)

    result = watch_a_share_protection(
        portfolio_path=portfolio_path,
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=remove_position,
    )

    assert result.status == "closed"
    assert result.watched_symbol_count == 0
    assert quote.snapshot_calls == [["SH.600900"]]


def test_domestic_etf_is_watched(tmp_path: Path) -> None:
    quote = SequenceQuote([{"SH.510300": Decimal("3.50")}])

    result = run_once(
        tmp_path,
        quote=quote,
        portfolio_path=portfolio(
            tmp_path, symbol="510300", name="沪深300ETF", asset_class="etf"
        ),
        state_path=state(tmp_path, symbol="510300", active_line="3.60"),
    )

    assert result.trigger_count == 1
    assert quote.snapshot_calls == [["SH.510300"]]


def test_missing_protection_line_is_visible_and_never_compared(tmp_path: Path) -> None:
    quote = SequenceQuote([])
    notifier = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    result = run_once(
        tmp_path,
        quote=quote,
        state_path=state(tmp_path, active_line=None),
        events_path=events_path,
        notifier=notifier,
    )

    assert result.exception_count == 1
    assert result.trigger_count == 0
    assert quote.snapshot_calls == []
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_line_missing",
        "protection_line_missing_notification_delivered",
    ]
    assert any("人工" in message for _, message in notifier.messages)


def test_missing_line_notification_retries_next_poll_after_failure(
    tmp_path: Path,
) -> None:
    feishu = FlakyNotifier(failures=1)
    macos = RecordingMacOSNotifier()
    events_path = tmp_path / "events.jsonl"

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path, active_line=None),
        events_path=events_path,
        quote_client=SequenceQuote([]),
        notifier=CompositeNotifier([feishu, macos]),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=lambda seconds: None,
    )

    assert feishu.attempt_count == 2
    assert macos.messages == []
    assert result.exception_count == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_line_missing",
        "protection_line_missing_notification_delivered",
    ]


def test_existing_same_day_trigger_without_receipt_retries_after_restart(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    append_watch_event(
        events_path,
        symbol="600900",
        trading_date="2026-07-15",
        event_type="protection_triggered",
        occurred_at="2026-07-15T09:31:00+08:00",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
    )
    quote = SequenceQuote([{"SH.600900": Decimal("27.20")}])
    notifier = RecordingNotifier()

    result = run_once(
        tmp_path,
        quote=quote,
        events_path=events_path,
        notifier=notifier,
    )

    assert result.trigger_count == 0
    assert len(notifier.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_feishu",
    ]


def test_opend_failure_reconnects_once_and_announces_recovery(tmp_path: Path) -> None:
    failed = SequenceQuote([interrupted()])
    recovered = SequenceQuote([{"SH.600900": Decimal("28")}])
    replacements = iter([recovered])
    sleeps: list[float] = []
    notifier = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=events_path,
        quote_client=failed,
        quote_client_factory=lambda: next(replacements),
        notifier=notifier,
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T09:30:00+08:00", "2026-07-15T09:30:01+08:00"]
        ),
        sleep_fn=sleeps.append,
    )

    assert result.status == "completed"
    assert sleeps == [60]
    assert failed.closed is True
    assert recovered.closed is True
    assert sum("中断" in title for title, _ in notifier.messages) == 1
    assert sum("恢复" in title for title, _ in notifier.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "monitor_interrupted",
        "monitor_recovered",
    ]


def test_calendar_failure_uses_the_same_reconnect_path(tmp_path: Path) -> None:
    failed = SequenceQuote([], trading_days=interrupted())
    recovered = SequenceQuote([{"SH.600900": Decimal("28")}])
    sleeps: list[float] = []

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=failed,
        quote_client_factory=lambda: recovered,
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T09:30:00+08:00", "2026-07-15T09:30:01+08:00"]
        ),
        sleep_fn=sleeps.append,
    )

    assert result.status == "completed"
    assert sleeps == [60]
    assert recovered.calendar_calls == [("2026-07-15", "2026-07-15")]


def test_initial_opend_failure_retries_until_a_client_is_available(
    tmp_path: Path,
) -> None:
    recovered = SequenceQuote([{"SH.600900": Decimal("28")}])
    clients: list[object] = [interrupted("OpenD unavailable"), recovered]
    sleeps: list[float] = []
    notifier = RecordingNotifier()

    def factory() -> object:
        item = clients.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=None,
        quote_client_factory=factory,
        notifier=notifier,
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T09:30:00+08:00", "2026-07-15T09:30:01+08:00"]
        ),
        sleep_fn=sleeps.append,
    )

    assert result.status == "completed"
    assert sleeps == [60]
    assert sum("中断" in title for title, _ in notifier.messages) == 1
    assert sum("恢复" in title for title, _ in notifier.messages) == 1


def test_missing_quote_is_recorded_unknown_not_safe(tmp_path: Path) -> None:
    quote = SequenceQuote([{}])
    notifier = RecordingNotifier()
    events_path = tmp_path / "events.jsonl"

    result = run_once(
        tmp_path,
        quote=quote,
        events_path=events_path,
        notifier=notifier,
    )

    assert result.unknown_quote_count == 1
    assert result.trigger_count == 0
    assert [event["event_type"] for event in read_events(events_path)] == [
        "quote_unknown",
        "quote_unknown_notification_delivered",
    ]
    assert any("未知" in message for _, message in notifier.messages)
    assert not any("安全" in message for _, message in notifier.messages)


def test_quote_notification_retries_after_failed_process_restarts(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    failed = FlakyNotifier(failures=1)
    first = run_once(
        tmp_path,
        quote=SequenceQuote([{}]),
        events_path=events_path,
        notifier=failed,
    )
    delivered = RecordingNotifier()

    second = run_once(
        tmp_path,
        quote=SequenceQuote([{}]),
        events_path=events_path,
        notifier=delivered,
    )

    assert first.unknown_quote_count == 1
    assert second.unknown_quote_count == 0
    assert failed.attempt_count == 1
    assert len(delivered.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "quote_unknown",
        "quote_unknown_notification_delivered",
    ]


def test_trigger_notification_success_suppresses_after_restart(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    first_feishu = RecordingNotifier()
    first_macos = RecordingMacOSNotifier()
    first = run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([first_feishu, first_macos]),
    )
    restarted_feishu = RecordingNotifier()
    restarted_macos = RecordingMacOSNotifier()

    restarted = run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.20")}]),
        events_path=events_path,
        notifier=CompositeNotifier([restarted_feishu, restarted_macos]),
    )

    assert first.trigger_count == 1
    assert restarted.trigger_count == 0
    assert len(first_feishu.messages) == len(first_macos.messages) == 1
    assert restarted_feishu.messages == restarted_macos.messages == []
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]


def test_trigger_notification_replays_after_price_rebounds(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    failed_feishu = FlakyNotifier(failures=1)
    failed_macos = FlakyMacOSNotifier(failures=1)
    first = run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier([failed_feishu, failed_macos]),
    )
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    restarted = run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("28.00")}]),
        events_path=events_path,
        notifier=CompositeNotifier([feishu, macos]),
    )

    assert first.trigger_count == 1
    assert restarted.trigger_count == 0
    assert "今日已触发活动保护线 27.31" in feishu.messages[0][1]
    assert "最新价 28.00 <=" not in feishu.messages[0][1]
    assert len(macos.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]


def test_trigger_notification_replays_before_quote_unknown_handling(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=CompositeNotifier(
            [FlakyNotifier(failures=1), FlakyMacOSNotifier(failures=1)]
        ),
    )
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    run_once(
        tmp_path,
        quote=SequenceQuote([{}]),
        events_path=events_path,
        notifier=CompositeNotifier([feishu, macos]),
    )

    assert any("此前提醒未完整送达" in message for _, message in feishu.messages)
    assert len(macos.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)][:3] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]


def test_trigger_retries_only_incomplete_required_channel_group(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    feishu = FlakyNotifier(failures=1)
    macos = RecordingMacOSNotifier()

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=events_path,
        quote_client=SequenceQuote(
            [
                {"SH.600900": Decimal("27.30")},
                {"SH.600900": Decimal("27.20")},
            ]
        ),
        notifier=CompositeNotifier([feishu, macos]),
        poll_seconds=5,
        reconnect_seconds=60,
        now_fn=SequenceClock(
            [
                "2026-07-15T09:30:00+08:00",
                "2026-07-15T09:30:05+08:00",
                "2026-07-15T15:00:01+08:00",
            ]
        ),
        sleep_fn=lambda seconds: None,
    )

    assert result.trigger_count == 1
    assert feishu.attempt_count == 2
    assert len(feishu.messages) == 1
    assert len(macos.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered",
        "protection_triggered_notification_delivered_macos",
        "protection_triggered_notification_delivered_feishu",
    ]


def test_trigger_null_notifier_never_writes_delivery_receipt(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    result = run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events_path,
        notifier=NullNotifier(),
    )

    assert result.trigger_count == 1
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered"
    ]


def test_legacy_generic_trigger_receipt_does_not_suppress_required_groups(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    for event_type in (
        "protection_triggered",
        "protection_triggered_notification_delivered",
    ):
        append_watch_event(
            events_path,
            symbol="600900",
            trading_date="2026-07-15",
            event_type=event_type,
            occurred_at="2026-07-15T09:31:00+08:00",
            last_price=Decimal("27.30"),
            active_line=Decimal("27.31"),
        )
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("28.00")}]),
        events_path=events_path,
        notifier=CompositeNotifier([feishu, macos]),
    )

    assert len(feishu.messages) == 1
    assert len(macos.messages) == 1
    assert [event["event_type"] for event in read_events(events_path)][-2:] == [
        "protection_triggered_notification_delivered_feishu",
        "protection_triggered_notification_delivered_macos",
    ]


def test_trigger_notification_does_not_replay_after_position_is_removed(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    append_watch_event(
        events_path,
        symbol="600900",
        trading_date="2026-07-15",
        event_type="protection_triggered",
        occurred_at="2026-07-15T09:31:00+08:00",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
    )
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    result = run_once(
        tmp_path,
        quote=SequenceQuote([]),
        portfolio_path=portfolio(tmp_path, symbol=None),
        events_path=events_path,
        notifier=CompositeNotifier([feishu, macos]),
    )

    assert result.watched_symbol_count == 0
    assert feishu.messages == macos.messages == []
    assert [event["event_type"] for event in read_events(events_path)] == [
        "protection_triggered"
    ]


@pytest.mark.parametrize(
    ("active_line", "snapshots", "fact_type"),
    [
        (None, [], "protection_line_missing"),
        ("27.31", [{}], "quote_unknown"),
    ],
)
def test_manual_exception_null_notifier_never_writes_delivery_receipt(
    tmp_path: Path,
    active_line: str | None,
    snapshots: list[dict[str, Decimal]],
    fact_type: str,
) -> None:
    events_path = tmp_path / "events.jsonl"

    run_once(
        tmp_path,
        quote=SequenceQuote(snapshots),
        state_path=state(tmp_path, active_line=active_line),
        events_path=events_path,
        notifier=NullNotifier(),
    )

    assert [event["event_type"] for event in read_events(events_path)] == [fact_type]


def test_manual_quote_exception_is_sent_to_feishu_not_macos(tmp_path: Path) -> None:
    quote = SequenceQuote([{}])
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=CompositeNotifier([feishu, macos]),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(["2026-07-15T09:30:00+08:00"]),
        sleep_fn=lambda seconds: None,
    )

    assert len(feishu.messages) == 1
    assert macos.messages == []


def test_report_lock_contention_retries_without_stopping_watcher(
    tmp_path: Path,
) -> None:
    report_lock_path = tmp_path / "data/runs/.trend_a_share_report.lock"
    held_lock = RunLock(report_lock_path)
    held_lock.__enter__()
    sleeps: list[float] = []

    def release_report_lock(seconds: float) -> None:
        sleeps.append(seconds)
        held_lock.__exit__(None, None, None)

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path),
        state_path=state(tmp_path),
        events_path=tmp_path / "events.jsonl",
        report_lock_path=report_lock_path,
        quote_client=SequenceQuote([{"SH.600900": Decimal("28")}]),
        notifier=RecordingNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=SequenceClock(
            ["2026-07-15T09:30:00+08:00", "2026-07-15T09:30:05+08:00"]
        ),
        sleep_fn=release_report_lock,
    )

    assert result.status == "completed"
    assert sleeps == [5]
