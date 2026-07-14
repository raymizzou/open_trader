from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.a_share_trend_watch import (
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
    )


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def interrupted(message: str = "网络中断") -> FutuQuoteError:
    return FutuQuoteError(message, error_type="quote_server_interrupted")


def test_watcher_alerts_once_per_symbol_per_day(tmp_path: Path) -> None:
    quote = SequenceQuote(
        [
            {"SH.600900": Decimal("27.30")},
            {"SH.600900": Decimal("27.20")},
        ]
    )
    notifier = RecordingNotifier()

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path, symbol="600900"),
        state_path=state(tmp_path, symbol="600900", active_line="27.31"),
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        notifier=notifier,
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
    assert sum("全部卖出" in message for _, message in notifier.messages) == 1
    events = read_events(tmp_path / "events.jsonl")
    assert [event["event_type"] for event in events] == ["protection_triggered"]
    assert set(events[0]) == {
        "event_id",
        "symbol",
        "trading_date",
        "event_type",
        "occurred_at",
        "last_price",
        "active_line",
    }


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
        "protection_line_missing"
    ]
    assert any("人工" in message for _, message in notifier.messages)


def test_existing_same_day_trigger_suppresses_repeat_after_restart(
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
    assert len(read_events(events_path)) == 1
    assert not any("全部卖出" in message for _, message in notifier.messages)


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
        "quote_unknown"
    ]
    assert any("未知" in message for _, message in notifier.messages)
    assert not any("安全" in message for _, message in notifier.messages)


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
