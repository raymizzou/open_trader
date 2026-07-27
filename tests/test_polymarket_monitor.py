from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def ns(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def market(
    market_id: str,
    *,
    yes: str = "yes-1",
    no: str = "no-1",
    volume: str = "1000",
    fees_enabled: bool | None = False,
    neg_risk: bool = False,
    active: bool = True,
    accepting_orders: bool = True,
    enable_order_book: bool = True,
    ended: bool = False,
) -> SimpleNamespace:
    return ns(
        id=market_id,
        slug=f"slug-{market_id}",
        question=f"Question {market_id}",
        condition_id=f"condition-{market_id}",
        state=ns(
            active=active,
            closed=not active,
            ended=ended,
            accepting_orders=accepting_orders,
            enable_order_book=enable_order_book,
            neg_risk=neg_risk,
        ),
        outcomes=[ns(label="YES", token_id=yes), ns(label="NO", token_id=no)],
        trading=ns(
            minimum_order_size=Decimal("1"),
            minimum_tick_size=Decimal("0.01"),
            fees_enabled=fees_enabled,
            neg_risk=neg_risk,
        ),
        metrics=ns(volume_24hr=Decimal(volume)),
    )


def event(
    event_id: str,
    *,
    volume: str = "1000",
    markets: tuple[SimpleNamespace, ...] = (),
    active: bool = True,
    closed: bool = False,
    ended: bool = False,
) -> SimpleNamespace:
    return ns(
        id=event_id,
        title=f"Event {event_id}",
        slug=f"event-{event_id}",
        state=ns(active=active, closed=closed, ended=ended),
        metrics=ns(volume_24hr=Decimal(volume)),
        markets=markets,
    )


def order_book(token_id: str, *, now: datetime = NOW) -> SimpleNamespace:
    price = "0.45" if token_id.startswith("yes") else "0.48"
    return ns(
        asset_id=token_id,
        token_id=token_id,
        timestamp=now,
        asks=[ns(price=Decimal(price), size=Decimal("20"))],
        bids=[],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        neg_risk=False,
    )


class FakeStream:
    def __init__(self, messages: list[object] | None = None) -> None:
        self.messages = list(messages or [])
        self.closed = False

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> object:
        if self.messages:
            return self.messages.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class FakePublicClient:
    events: list[object] = []
    books: dict[str, object] = {}
    streams: list[FakeStream] = []
    list_events_calls: list[dict[str, object]] = []
    book_calls: list[list[str]] = []
    subscribe_specs: list[object] = []
    page_mode = False
    fail_list_events = False

    def __init__(self) -> None:
        self.stream = self.streams.pop(0) if self.streams else FakeStream()

    async def list_events(self, **kwargs: object) -> list[object]:
        self.list_events_calls.append(kwargs)
        if self.fail_list_events:
            raise ConnectionError("sentinel network failure")
        if self.page_mode:
            return PagePaginator(list(self.events))  # type: ignore[return-value]
        return list(self.events)

    async def get_order_books(self, *, token_ids: list[str]) -> tuple[object, ...]:
        self.book_calls.append(list(token_ids))
        # Deliberately reverse the response to prove token-id matching.
        return tuple(self.books[token_id] for token_id in reversed(token_ids))

    def subscribe(self, spec: object) -> FakeStream:
        self.subscribe_specs.append(spec)
        return self.stream


@dataclass
class Page:
    items: tuple[object, ...]
    has_more: bool = False


class PagePaginator:
    def __init__(self, items: list[object]) -> None:
        self.pages = [Page(tuple(items[:12]), True), Page(tuple(items[12:]), False)]

    def __aiter__(self) -> "PagePaginator":
        self._index = 0
        return self

    async def __anext__(self) -> Page:
        if self._index >= len(self.pages):
            raise StopAsyncIteration
        page = self.pages[self._index]
        self._index += 1
        return page


@dataclass
class FakeTrading:
    checked_at: datetime = NOW
    balance: Decimal = Decimal("50")
    allowance: Decimal = Decimal("50")
    submit_calls: int = 0
    remediation_calls: int = 0
    merge_calls: int = 0

    def readiness_snapshot(self) -> dict[str, object]:
        return {
            "wallet": "ready",
            "geoblock": "allowed",
            "relayer": "ready",
            "balance": self.balance,
            "allowance": self.allowance,
            "checked_at": self.checked_at,
        }

    def submit_pair_once(self, *args: object, **kwargs: object) -> None:
        self.submit_calls += 1

    def submit_remediation_once(self, *args: object, **kwargs: object) -> None:
        self.remediation_calls += 1

    def merge_once(self, *args: object, **kwargs: object) -> None:
        self.merge_calls += 1


def setup_public(event_rows: list[object]) -> None:
    FakePublicClient.events = event_rows
    books: dict[str, object] = {}
    for row in event_rows:
        for item in getattr(row, "markets", ()):
            outcomes = getattr(item, "outcomes", ())
            for outcome in outcomes:
                token = getattr(outcome, "token_id", "")
                if token:
                    books[token] = order_book(token)
    FakePublicClient.books = books
    FakePublicClient.streams = []
    FakePublicClient.list_events_calls = []
    FakePublicClient.book_calls = []
    FakePublicClient.subscribe_specs = []
    FakePublicClient.page_mode = False
    FakePublicClient.fail_list_events = False


def make_monitor(tmp_path: Path, *, trading: FakeTrading | None = None):
    from open_trader.polymarket_monitor import PolymarketMonitor

    return PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=trading or FakeTrading(),
        public_client_factory=FakePublicClient,
        clock=lambda: NOW,
    )


def test_refresh_uses_official_event_query_and_limits_valid_top_twenty(tmp_path: Path) -> None:
    valid = [event(f"event-{i:02d}", volume=str(1000 - i), markets=(market(f"m-{i:02d}"),)) for i in range(22)]
    valid.extend(
        [
            event("closed", closed=True, markets=(market("closed-market"),)),
            event("ended", ended=True, markets=(market("ended-market"),)),
            event("negative", volume="-1", markets=(market("negative-market"),)),
            event("nan", volume="NaN", markets=(market("nan-market"),)),
        ]
    )
    setup_public(valid)

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    assert FakePublicClient.list_events_calls == [
        {
            "closed": False,
            "ended": False,
            "order": "volume24hr",
            "ascending": False,
            "page_size": 20,
        }
    ]
    snapshot = monitor.snapshot()
    assert len(snapshot["events"]) == 20
    assert snapshot["events"][0]["volume_24h"] == Decimal("1000")
    assert snapshot["diagnostics"]["malformed_events"] == 4


def test_refresh_drains_official_page_shaped_async_paginator(tmp_path: Path) -> None:
    setup_public([event(f"event-{i:02d}", volume=str(1000 - i), markets=(market(f"m-{i:02d}"),)) for i in range(21)])
    FakePublicClient.page_mode = True

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    assert len(monitor.snapshot()["events"]) == 20
    assert monitor.snapshot()["events"][0]["event_id"] == "event-00"


def test_only_exact_active_binary_markets_are_subscribed_and_books_match_by_token_id(
    tmp_path: Path,
) -> None:
    good = market("good", yes="yes-good", no="no-good")
    monitor_only = market("fee", yes="yes-fee", no="no-fee", fees_enabled=True)
    neg_risk = market("neg", yes="yes-neg", no="no-neg", neg_risk=True)
    malformed = ns(id="malformed", state=ns(active=True, closed=False), outcomes=[ns(label="YES", token_id="yes")])
    setup_public([event("e", markets=(good, monitor_only, neg_risk, malformed))])

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    snapshot = monitor.snapshot()

    assert [call for call in FakePublicClient.book_calls] == [["yes-good", "no-good"], ["yes-fee", "no-fee"], ["yes-neg", "no-neg"]]
    spec = FakePublicClient.subscribe_specs[-1]
    assert tuple(spec.token_ids) == ("no-fee", "no-good", "no-neg", "yes-fee", "yes-good", "yes-neg")
    markets = {row["market_id"]: row for row in snapshot["events"][0]["markets"]}
    assert markets["good"]["actionable"] is True
    assert markets["fee"]["actionable"] is False
    assert markets["fee"]["eligibility_reason"] == "fee_unverified_or_enabled"
    assert markets["neg"]["eligibility_reason"] == "neg_risk"
    assert snapshot["diagnostics"]["malformed_markets"] == 1


def test_readiness_is_refreshed_without_mutation_and_candidate_is_fresh(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    trading = FakeTrading()
    monitor = make_monitor(tmp_path, trading=trading)
    monitor.refresh_once()

    opportunity = monitor.opportunity("e:m")
    assert opportunity is not None
    assert opportunity["actionable"] is True
    assert opportunity["confirmed_age_seconds"] <= 10
    assert trading.submit_calls == trading.remediation_calls == trading.merge_calls == 0

    trading.checked_at = NOW - timedelta(seconds=61)
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is None
    assert monitor.snapshot()["health"]["actionable"] is False


def test_task2_account_and_merge_capability_readiness_is_actionable(tmp_path: Path) -> None:
    class Task2ReadyTrading:
        _client = ns(merge_positions=lambda **kwargs: None)

        def account_snapshot(self) -> object:
            return ns(
                wallet_address="0xwallet",
                p_usd_balance=Decimal("50"),
                p_usd_allowance=Decimal("50"),
                checked_at=NOW,
            )

        def geoblock_allowed(self) -> bool:
            return True

    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path, trading=Task2ReadyTrading())
    monitor.refresh_once()
    assert monitor.opportunity("e:m")["actionable"] is True  # type: ignore[index]


def test_candidate_age_is_recomputed_after_confirmation(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    now = [NOW]
    monitor = make_monitor(tmp_path)
    monitor._clock = lambda: now[0]
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is not None

    now[0] = NOW + timedelta(seconds=11)
    stale = monitor.opportunity("e:m")
    assert stale is not None
    assert stale["actionable"] is False
    assert stale["eligibility_reason"] == "monitor_degraded"
    assert monitor.snapshot()["health"]["status"] == "degraded"


@pytest.mark.parametrize("field_value", [None, "not-a-timestamp"])
def test_missing_or_invalid_book_timestamp_fails_closed(tmp_path: Path, field_value: object) -> None:
    setup_public([event("e", markets=(market("m"),))])
    FakePublicClient.books["yes-1"].timestamp = field_value
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is None
    assert monitor.snapshot()["events"][0]["markets"][0]["eligibility_reason"] == "book_timestamp_missing"


def test_missing_readiness_timestamp_fails_closed(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path, trading=FakeTrading(checked_at=None))  # type: ignore[arg-type]
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is None
    assert monitor.snapshot()["health"]["status"] == "degraded"


def test_signal_episode_peaks_close_and_restart(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    first = monitor.opportunity("e:m")
    assert first is not None
    monitor.refresh_once()
    assert len(monitor._store.signal_history("all")) == 1

    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m"),))])
    monitor.refresh_once()
    assert len(monitor._store.signal_history("all")) == 1

    setup_public([event("e", markets=())])
    monitor.refresh_once()
    history = monitor._store.signal_history("all")
    assert len(history) == 1
    assert history[0]["ended_at"] is not None

    restarted = make_monitor(tmp_path)
    assert restarted._store.signal_history("all") == history


def test_signal_peak_strings_are_parsed_and_not_overwritten_by_lower_update(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    first = monitor._store.signal_history("all")[0]
    assert first["peak_estimated_profit"] == "1.4000"

    FakePublicClient.books["yes-1"] = order_book("yes-1")
    FakePublicClient.books["yes-1"].asks = [ns(price=Decimal("0.46"), size=Decimal("20"))]
    monitor.refresh_once()
    assert monitor._store.signal_history("all")[0]["peak_estimated_profit"] == "1.4000"
    restarted = make_monitor(tmp_path)
    restarted.refresh_once()
    assert restarted._store.signal_history("all")[0]["peak_estimated_profit"] == "1.4000"


def test_healthy_quiet_is_distinct_from_degraded_and_runtime_is_throttled(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m", volume="100"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor.snapshot()["health"]["status"] == "healthy"

    setup_public([event("e", markets=())])
    monitor.refresh_once()
    assert monitor.snapshot()["health"]["status"] == "healthy"
    assert monitor.snapshot()["health"]["opportunity_count"] == 0


def test_no_stream_message_for_over_fifteen_seconds_is_degraded(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    now = [NOW]
    monitor = make_monitor(tmp_path)
    monitor._clock = lambda: now[0]
    monitor.refresh_once()
    now[0] = NOW + timedelta(seconds=16)
    assert monitor.snapshot()["health"]["status"] == "degraded"


def test_universe_failure_degrades_prior_snapshot(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    FakePublicClient.fail_list_events = True
    monitor.refresh_once()
    snapshot = monitor.snapshot()
    assert snapshot["events"]
    assert snapshot["health"]["status"] == "degraded"
    assert snapshot["health"]["actionable"] is False


def test_monitor_only_rows_keep_gross_upper_bound(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("fee", fees_enabled=True), market("neg", neg_risk=True)))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    rows = {row["market_id"]: row for row in monitor.snapshot()["events"][0]["markets"]}
    assert isinstance(rows["fee"]["gross_upper_bound"], Decimal)
    assert isinstance(rows["neg"]["gross_upper_bound"], Decimal)


def test_opportunities_use_domain_profit_volume_order(tmp_path: Path) -> None:
    high = market("high", yes="yes-high", no="no-high")
    low = market("low", yes="yes-low", no="no-low")
    setup_public([event("z", markets=(high,)), event("a", markets=(low,))])
    FakePublicClient.books["yes-low"].asks = [ns(price=Decimal("0.46"), size=Decimal("20"))]
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert [item["opportunity_id"] for item in monitor.snapshot()["opportunities"]] == ["z:high", "a:low"]


def test_start_stop_owns_one_daemon_async_thread(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    FakePublicClient.streams = [FakeStream()]
    monitor = make_monitor(tmp_path)
    monitor.start()
    monitor.stop()
    assert monitor._thread is not None
    assert monitor._thread.daemon is True


def test_monitor_once_diagnostic_is_public_and_non_mutating() -> None:
    from open_trader.polymarket_monitor import monitor_once_diagnostic

    setup_public([event(f"event-{i:02d}", markets=(market(f"m-{i:02d}"),)) for i in range(20)])
    FakePublicClient.streams = [FakeStream([object()])]
    report = monitor_once_diagnostic(timeout=0.2, public_client_factory=FakePublicClient)

    assert report == {
        "event_count": 20,
        "volumes": "present",
        "websocket_heartbeat": "pass",
        "paired_book_read": "pass",
        "mutations": 0,
        "result": "PASS",
    }


def test_monitor_once_diagnostic_converts_public_failures_to_blocked() -> None:
    from open_trader.polymarket_monitor import monitor_once_diagnostic

    setup_public([])
    FakePublicClient.fail_list_events = True
    report = monitor_once_diagnostic(timeout=0.1, public_client_factory=FakePublicClient)
    assert report["result"] == "BLOCKED"
    assert report["mutations"] == 0
