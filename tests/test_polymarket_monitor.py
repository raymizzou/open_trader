from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.polymarket_relation_discovery import (
    RelationValidation,
    discover_threshold_relations,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
THRESHOLD_RULES = (
    'This market resolves "Yes" if the Binance BTC/USDT close at 12:00 ET '
    "is higher than the price specified in the title. Otherwise it resolves "
    '"No". The resolution source is Binance.'
)


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


def threshold_market(
    market_id: str,
    *,
    question: str,
    yes: str,
    no: str,
    tick_size: Decimal = Decimal("0.01"),
) -> SimpleNamespace:
    return ns(
        id=market_id,
        slug=f"slug-{market_id}",
        question=question,
        condition_id=f"condition-{market_id}",
        description=THRESHOLD_RULES,
        resolution_source="Binance",
        end_date="2026-12-31T17:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
        group_item_threshold="display-only",
        state=ns(
            active=True,
            closed=False,
            ended=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=False,
        ),
        outcomes=[ns(label="YES", token_id=yes), ns(label="NO", token_id=no)],
        trading=ns(
            minimum_order_size=Decimal("1"),
            minimum_tick_size=tick_size,
            fees_enabled=False,
            neg_risk=False,
        ),
        metrics=ns(volume_24hr=Decimal("250")),
    )


def threshold_event() -> SimpleNamespace:
    return event(
        "threshold-event",
        volume="250",
        markets=(
            threshold_market(
                "threshold-low",
                question="Will Bitcoin be above $90,000 on December 31?",
                yes="yes-low",
                no="no-low",
            ),
            threshold_market(
                "threshold-high",
                question="Will Bitcoin be above $100,000 on December 31?",
                yes="yes-high",
                no="no-high",
            ),
        ),
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


def threshold_book(
    token_id: str,
    *,
    ask: str,
    bid: str,
    size: str = "20",
    now: datetime = NOW - timedelta(days=1),
) -> SimpleNamespace:
    return ns(
        asset_id=token_id,
        token_id=token_id,
        timestamp=now,
        asks=[ns(price=Decimal(ask), size=Decimal(size))],
        bids=[ns(price=Decimal(bid), size=Decimal(size))],
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
    get_event_calls: list[str] = []
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

    async def get_event(self, *, id: str) -> object:
        self.get_event_calls.append(id)
        return next(row for row in self.events if str(getattr(row, "id", "")) == id)

    def subscribe(self, spec: object) -> FakeStream:
        self.subscribe_specs.append(spec)
        return self.stream


@dataclass
class Page:
    items: tuple[object, ...]
    has_more: bool = False


class PagePaginator:
    first_page_calls = 0
    iter_calls = 0

    def __init__(self, items: list[object]) -> None:
        self.pages = [Page(tuple(items[:20]), True), Page(tuple(items[20:]), False)]

    async def first_page(self) -> Page:
        type(self).first_page_calls += 1
        return self.pages[0]

    async def iter_items(self):
        type(self).iter_calls += 1
        for page in self.pages:
            for item in page.items:
                yield item

    def __aiter__(self) -> "PagePaginator":
        type(self).iter_calls += 1
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


class FakeRelationValidator:
    def __init__(self, status: str = "approved") -> None:
        self.status = status
        self.calls = 0

    def validate(self, relation) -> RelationValidation:
        self.calls += 1
        approved = self.status == "approved"
        rejected = self.status == "llm_rejected"
        reason = (
            ("AMBIGUOUS_RULES",)
            if rejected
            else (("CODEX_FAILED",) if not approved else ())
        )
        return RelationValidation(
            status=self.status,  # type: ignore[arg-type]
            decision="APPROVE" if approved else ("REJECT" if rejected else None),
            relation=relation.relation if approved else ("NONE" if rejected else None),
            summary=(
                "关系已由 Codex 和程序双重确认。"
                if approved
                else "完整规则无法证明该关系。"
            ),
            reason_codes=reason,
            evidence=(
                (
                    {
                        "market": "A",
                        "field": "rules",
                        "quote": "Binance BTC/USDT close at 12:00 ET",
                    },
                    {
                        "market": "B",
                        "field": "rules",
                        "quote": "Binance BTC/USDT close at 12:00 ET",
                    },
                )
                if approved
                else ()
            ),
            uncertainties=(),
            model="fake-codex",
            prompt_version="polymarket-threshold-relation-v1",
            cache_key="fake-cache-key",
            cached=False,
            structured_result={
                "proof": {
                    "excluded_state": "A=NO,B=YES",
                    "why_excluded": "higher threshold implies lower threshold",
                }
            },
        )


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
    FakePublicClient.get_event_calls = []
    FakePublicClient.page_mode = False
    FakePublicClient.fail_list_events = False
    PagePaginator.first_page_calls = 0
    PagePaginator.iter_calls = 0


def make_monitor(
    tmp_path: Path,
    *,
    trading: FakeTrading | None = None,
    relation_discovery=None,
    relation_validator=None,
):
    from open_trader.polymarket_monitor import PolymarketMonitor

    return PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=trading or FakeTrading(),
        public_client_factory=FakePublicClient,
        clock=lambda: NOW,
        relation_discovery=relation_discovery,
        relation_validator=relation_validator,
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


def test_refresh_reads_only_official_first_page(tmp_path: Path) -> None:
    setup_public([event(f"event-{i:02d}", volume=str(1000 - i), markets=(market(f"m-{i:02d}"),)) for i in range(21)])
    FakePublicClient.page_mode = True

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    assert len(monitor.snapshot()["events"]) == 20
    assert monitor.snapshot()["events"][0]["event_id"] == "event-00"
    assert PagePaginator.first_page_calls == 1
    assert PagePaginator.iter_calls == 0


def setup_threshold_books(
    *, low_ask: str = "0.40", high_no_ask: str = "0.48", size: str = "20"
) -> None:
    FakePublicClient.books.update(
        {
            "yes-low": threshold_book("yes-low", ask=low_ask, bid="0.39", size=size),
            "no-low": threshold_book("no-low", ask="0.60", bid="0.59", size=size),
            "yes-high": threshold_book("yes-high", ask="0.60", bid="0.59", size=size),
            "no-high": threshold_book("no-high", ask=high_no_ask, bid="0.47", size=size),
        }
    )


def test_threshold_discovery_scans_relation_first_page_and_only_calls_codex_for_positive_relation(
    tmp_path: Path,
) -> None:
    rows = [
        event(f"top-{index:02d}", volume=str(1000 - index), markets=(market(f"m-{index:02d}"),))
        for index in range(19)
    ]
    rows.append(threshold_event())
    setup_public(rows)
    FakePublicClient.page_mode = True
    setup_threshold_books()
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    monitor.refresh_once()

    assert FakePublicClient.list_events_calls[0] == {
        "closed": False,
        "ended": False,
        "order": "volume24hr",
        "ascending": False,
        "page_size": 20,
    }
    assert FakePublicClient.list_events_calls[1] == {
        "closed": False,
        "ended": False,
        "page_size": 100,
    }
    assert PagePaginator.first_page_calls == 2
    assert PagePaginator.iter_calls == 0
    assert len(monitor.snapshot()["events"]) == 20
    threshold_rows = [
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    ]
    assert len(threshold_rows) == 1
    row = threshold_rows[0]
    assert row["actionable"] is True
    assert row["llm_status"] == "approved"
    assert row["annualized_yield"] is not None
    assert row["resolution_at"] == "2026-12-31T17:00:00Z"
    assert row["remaining_days"] == Decimal(
        "157.2083333333333333333333333"
    )
    assert validator.calls == 1
    subscribed = FakePublicClient.subscribe_specs[-1]
    assert {"yes-low", "no-high"} <= set(subscribed.token_ids)
    assert row["confirmed_age_seconds"] == 0
    assert monitor.snapshot()["relation_discovery"]["status"] == "healthy"


def test_nonpositive_threshold_economics_never_calls_codex(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.52", high_no_ask="0.50")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    monitor.refresh_once()

    assert validator.calls == 0
    assert not [
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    ]


def test_new_market_refreshes_only_its_event_for_relation_discovery(
    tmp_path: Path,
) -> None:
    setup_public(
        [
            event(
                f"top-{index:02d}",
                volume=str(1000 - index),
                markets=(market(f"m-{index:02d}"),),
            )
            for index in range(20)
        ]
    )
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor.refresh_once()
    list_calls_before = len(FakePublicClient.list_events_calls)
    FakePublicClient.events.append(threshold_event())
    setup_threshold_books()

    asyncio.run(
        monitor._process_stream_event(
            monitor._client,
            ns(
                type="new_market",
                payload=ns(event_message=ns(id="threshold-event")),
            ),
        )
    )

    assert len(FakePublicClient.list_events_calls) == list_calls_before
    assert FakePublicClient.get_event_calls == ["threshold-event"]
    assert any(
        row.get("market_type") == "threshold_hedge"
        for row in monitor.snapshot()["opportunities"]
    )


@pytest.mark.parametrize("validator_status", ["llm_rejected", "llm_unavailable"])
def test_rejected_or_unavailable_threshold_positive_relation_remains_visible(
    tmp_path: Path,
    validator_status: str,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(validator_status),
    )

    monitor.refresh_once()

    rows = [
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    ]
    assert len(rows) == 1
    assert rows[0]["actionable"] is False
    assert rows[0]["llm_status"] == validator_status
    assert rows[0]["eligibility_reason"] in {
        "llm_rejected",
        "llm_unavailable",
    }
    assert rows[0]["estimated_profit"] > 0
    assert rows[0]["llm_summary"]


def test_subcent_threshold_profit_is_visible_and_annualized_distribution_is_reported(
    tmp_path: Path,
) -> None:
    precise = threshold_event()
    for raw_market in precise.markets:
        raw_market.trading.minimum_tick_size = Decimal("0.00005")
    setup_public([precise])
    setup_threshold_books(low_ask="0.49995", high_no_ask="0.49995", size="21")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )

    monitor.refresh_once()

    row = next(
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    )
    assert row["estimated_profit"] == Decimal("0.0020010000000000")
    distribution = monitor.snapshot()["relation_discovery"]["annualized_distribution"]
    assert distribution["current"]["count"] == 1
    assert distribution["7d"]["count"] == 1
    assert distribution["30d"]["count"] == 1


def test_relation_scan_logs_are_bounded_and_not_persisted(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    for _ in range(11):
        monitor.refresh_once()

    assert len(monitor.snapshot()["relation_discovery"]["scan_logs"]) == 20
    assert make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    ).snapshot()["relation_discovery"]["scan_logs"] == []


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


def test_large_token_universe_is_subscribed_in_websocket_safe_chunks(
    tmp_path: Path,
) -> None:
    markets = tuple(
        market(
            f"market-{index:03d}",
            yes=f"yes-{index:03d}",
            no=f"no-{index:03d}",
            fees_enabled=True,
        )
        for index in range(126)
    )
    setup_public([event("large-event", markets=markets)])

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    specs = FakePublicClient.subscribe_specs[-1]
    assert isinstance(specs, list)
    assert [len(spec.token_ids) for spec in specs] == [250, 2]
    assert sorted(token for spec in specs for token in spec.token_ids) == sorted(
        [f"yes-{index:03d}" for index in range(126)]
        + [f"no-{index:03d}" for index in range(126)]
    )


def test_readiness_is_refreshed_without_mutation_and_candidate_is_fresh(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    trading = FakeTrading()
    monitor = make_monitor(tmp_path, trading=trading)
    monitor.refresh_once()

    opportunity = monitor.opportunity("e:m")
    assert opportunity is not None
    assert opportunity["actionable"] is True
    assert opportunity["market_type"] == "standard_binary"
    assert opportunity["fee_status"] == "fee_free"
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


def test_explicit_readiness_is_enriched_with_fresh_geoblock(tmp_path: Path) -> None:
    class Trading:
        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "wallet": "ready",
                "wallet_address": "0xwallet",
                "p_usd_balance": Decimal("50"),
                "p_usd_allowance": Decimal("50"),
                "relayer": "ready",
                "checked_at": NOW,
            }

        def geoblock_allowed(self) -> bool:
            return True

    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path, trading=Trading())  # type: ignore[arg-type]

    monitor.refresh_once()

    assert monitor.snapshot()["readiness"]["geoblock"] == "allowed"  # type: ignore[index]
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


def test_background_monitor_refreshes_readiness_before_it_becomes_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_trader.polymarket_monitor as monitor_module

    class LiveTrading(FakeTrading):
        def readiness_snapshot(self) -> dict[str, object]:
            value = super().readiness_snapshot()
            value["checked_at"] = datetime.now(UTC)
            return value

    class LiveStream(FakeStream):
        async def __anext__(self) -> object:
            await asyncio.sleep(0.005)
            return object()

    monkeypatch.setattr(monitor_module, "READINESS_FRESHNESS_SECONDS", 0.04)
    monkeypatch.setattr(
        monitor_module,
        "READINESS_REFRESH_SECONDS",
        0.01,
        raising=False,
    )
    setup_public([event("e", markets=(market("m"),))])
    FakePublicClient.streams = [LiveStream()]
    monitor = make_monitor(tmp_path, trading=LiveTrading())
    monitor._clock = lambda: datetime.now(UTC)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(monitor.run_forever(), timeout=0.08))

    assert monitor.snapshot()["health"]["status"] == "healthy"


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


def test_monitor_once_accepts_fewer_events_than_the_limit() -> None:
    from open_trader.polymarket_monitor import monitor_once_diagnostic

    setup_public([
        event(f"event-{i:02d}", markets=(market(f"m-{i:02d}"),))
        for i in range(18)
    ])
    FakePublicClient.streams = [FakeStream([object()])]

    report = monitor_once_diagnostic(
        timeout=0.2, public_client_factory=FakePublicClient
    )

    assert report["event_count"] == 18
    assert report["result"] == "PASS"


def test_monitor_once_diagnostic_converts_public_failures_to_blocked() -> None:
    from open_trader.polymarket_monitor import monitor_once_diagnostic

    setup_public([])
    FakePublicClient.fail_list_events = True
    report = monitor_once_diagnostic(timeout=0.1, public_client_factory=FakePublicClient)
    assert report["result"] == "BLOCKED"
    assert report["mutations"] == 0


def test_monitor_once_diagnostic_applies_total_timeout() -> None:
    from open_trader.polymarket_monitor import monitor_once_diagnostic

    class SlowPublicClient(FakePublicClient):
        async def list_events(self, **kwargs: object) -> list[object]:
            del kwargs
            await asyncio.sleep(0.2)
            return []

    started = time.monotonic()
    report = monitor_once_diagnostic(
        timeout=0.01, public_client_factory=SlowPublicClient
    )

    assert time.monotonic() - started < 0.1
    assert report["result"] == "BLOCKED"
    assert report["mutations"] == 0


def test_runtime_refresh_applies_total_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader import polymarket_monitor
    from open_trader.polymarket_monitor import PolymarketMonitor

    class SlowPublicClient(FakePublicClient):
        async def list_events(self, **kwargs: object) -> list[object]:
            del kwargs
            await asyncio.sleep(0.2)
            return []

    monkeypatch.setattr(polymarket_monitor, "PUBLIC_REFRESH_TIMEOUT_SECONDS", 0.01)
    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=FakeTrading(),
        public_client_factory=SlowPublicClient,
        clock=lambda: NOW,
    )
    started = time.monotonic()
    snapshot = monitor.refresh_once()

    assert time.monotonic() - started < 0.1
    assert snapshot["health"]["actionable"] is False
    assert snapshot["diagnostics"]["last_error"] == "universe:TimeoutError"


def test_runtime_confirms_books_with_bounded_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader import polymarket_monitor
    from open_trader.polymarket_monitor import PolymarketMonitor

    class SlowBookClient(FakePublicClient):
        active = 0
        max_active = 0

        async def get_order_books(
            self, *, token_ids: list[str]
        ) -> tuple[object, ...]:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            try:
                await asyncio.sleep(0.02)
                return await super().get_order_books(token_ids=token_ids)
            finally:
                type(self).active -= 1

    rows = tuple(
        market(
            f"m-{i:02d}",
            yes=f"yes-{i:02d}",
            no=f"no-{i:02d}",
            fees_enabled=True,
        )
        for i in range(20)
    )
    setup_public([event("e", markets=rows)])
    monkeypatch.setattr(polymarket_monitor, "PUBLIC_REFRESH_TIMEOUT_SECONDS", 0.15)
    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=FakeTrading(),
        public_client_factory=SlowBookClient,
        clock=lambda: NOW,
    )

    snapshot = monitor.refresh_once()

    assert snapshot["health"]["status"] == "healthy", snapshot["health"]
    assert SlowBookClient.max_active == 8
    assert len(SlowBookClient.book_calls) == 20
