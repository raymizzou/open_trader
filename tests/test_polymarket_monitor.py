from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

from open_trader.polymarket_relation_discovery import (
    RelationValidation,
    discover_threshold_relation_catalog,
    discover_threshold_relations,
    threshold_relation_payload,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.polymarket_monitor import PolymarketMonitor, _relation_fingerprint
from open_trader.relation_catalog import RelationCatalog


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
    end_date: str | None = None,
) -> SimpleNamespace:
    return ns(
        id=market_id,
        slug=f"slug-{market_id}",
        question=f"Question {market_id}",
        condition_id=f"condition-{market_id}",
        end_date=end_date,
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


def threshold_event(
    *, event_id: str = "threshold-event", token_prefix: str = ""
) -> SimpleNamespace:
    prefix = token_prefix or ""
    return event(
        event_id,
        volume="250",
        markets=(
            threshold_market(
                "threshold-low",
                question="Will Bitcoin be above $90,000 on December 31?",
                yes=f"{prefix}yes-low",
                no=f"{prefix}no-low",
            ),
            threshold_market(
                "threshold-high",
                question="Will Bitcoin be above $100,000 on December 31?",
                yes=f"{prefix}yes-high",
                no=f"{prefix}no-high",
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
    fail_get_event = False
    fail_get_order_books = False

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
        if self.fail_get_order_books:
            raise ConnectionError("sentinel book failure")
        # Deliberately reverse the response to prove token-id matching.
        return tuple(self.books[token_id] for token_id in reversed(token_ids))

    async def get_event(self, *, id: str) -> object:
        self.get_event_calls.append(id)
        if self.fail_get_event:
            raise ConnectionError("sentinel event failure")
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
        self.relation_ids: list[str] = []
        self.cached: dict[str, RelationValidation] = {}
        self.block: threading.Event | None = None

    def validate(self, relation) -> RelationValidation:
        self.calls += 1
        self.relation_ids.append(relation.relation_id)
        if self.block is not None:
            self.block.wait(timeout=5)
        approved = self.status == "approved"
        rejected = self.status == "llm_rejected"
        reason = (
            ("AMBIGUOUS_RULES",)
            if rejected
            else (("LLM_FAILED",) if not approved else ())
        )
        result = RelationValidation(
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
        if result.status in {"approved", "llm_rejected"}:
            self.cached[relation.relation_id] = result
        return result

    def cached_validation(self, relation) -> RelationValidation | None:
        value = self.cached.get(relation.relation_id)
        if value is None:
            return None
        return replace(value, cached=True)


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
    FakePublicClient.fail_get_event = False
    FakePublicClient.fail_get_order_books = False
    PagePaginator.first_page_calls = 0
    PagePaginator.iter_calls = 0


def make_monitor(
    tmp_path: Path,
    *,
    trading: FakeTrading | None = None,
    relation_discovery=discover_threshold_relation_catalog,
    relation_validator=None,
    title_translator=None,
    clock=None,
):
    from open_trader.polymarket_monitor import PolymarketMonitor

    return PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=trading or FakeTrading(),
        public_client_factory=FakePublicClient,
        clock=clock or (lambda: NOW),
        relation_discovery=relation_discovery,
        relation_validator=relation_validator,
        title_translator=title_translator,
    )


def test_auto_eat_observer_runs_once_for_actionable_threshold(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(tmp_path)
    calls: list[tuple[str, str]] = []
    monitor.set_auto_eat_observer(
        lambda opportunity_id, signal_id: calls.append((opportunity_id, signal_id))
    )
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": "threshold:abc",
            "event_id": "e1",
            "question": "Q",
            "started_at": NOW.isoformat(),
            "first_positive_at": NOW.isoformat(),
            "net_edge": Decimal("0.1"),
            "estimated_profit": Decimal("1"),
            "profit": Decimal("1"),
            "market_type": "threshold_hedge",
            "annualized_yield": Decimal("0.20"),
            "eligibility_reason": "actionable",
            "llm_status": "approved",
            "rules_verified_at": NOW.isoformat(),
        }
    )
    opportunity = {
        "market_type": "threshold_hedge",
        "actionable": True,
        "market_id": "threshold:abc",
        "event_id": "e1",
        "question": "Q",
        "opportunity_id": "threshold:abc",
        "rules_verified_at": NOW.isoformat(),
        "relation_validation": {"status": "approved"},
    }

    async def run() -> None:
        monitor._schedule_auto_eat(signal_id, opportunity)
        await monitor._auto_eat_task

    asyncio.run(run())

    assert calls == [("threshold:abc", signal_id)]


def test_auto_eat_observer_skips_non_actionable(tmp_path: Path) -> None:
    monitor = make_monitor(tmp_path)
    calls: list[tuple[str, str]] = []
    monitor.set_auto_eat_observer(
        lambda opportunity_id, signal_id: calls.append((opportunity_id, signal_id))
    )
    monitor._schedule_auto_eat("s1", {
        "market_type": "threshold_hedge",
        "actionable": False,
        "market_id": "threshold:abc",
        "event_id": "e1",
        "question": "Q",
        "opportunity_id": "threshold:abc",
        "rules_verified_at": NOW.isoformat(),
        "relation_validation": {"status": "approved"},
    })
    assert calls == []


def test_snapshot_uses_metrics_refreshed_outside_monitor_lock(tmp_path: Path) -> None:
    monitor = make_monitor(tmp_path)
    monotonic = [0.0]
    usage_calls = 0
    history_calls = 0

    def usage() -> dict[str, int]:
        nonlocal usage_calls
        assert not monitor._lock._is_owned()
        usage_calls += 1
        return {
            "calls": usage_calls,
            "successes": usage_calls,
            "failures": 0,
            "cache_hits": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }

    def history(window: str) -> list[dict[str, object]]:
        nonlocal history_calls
        assert not monitor._lock._is_owned()
        history_calls += 1
        return [{"signal_id": "signal-1"}] if window == "24h" else []

    monitor._monotonic = lambda: monotonic[0]
    monitor._store.llm_usage_24h = usage  # type: ignore[method-assign]
    monitor._store.signal_history = history  # type: ignore[method-assign]

    monitor._refresh_snapshot_metrics()
    assert monitor.snapshot()["relation_discovery"]["codex_usage_24h"]["calls"] == 1
    assert monitor.snapshot()["signals_24h"] == 1
    assert monitor.snapshot()["relation_discovery"]["codex_usage_24h"]["calls"] == 1
    monotonic[0] = 60.0
    monitor._refresh_snapshot_metrics()
    assert monitor.snapshot()["relation_discovery"]["codex_usage_24h"]["calls"] == 2
    assert usage_calls == 2
    assert history_calls == 6


def test_start_primes_snapshot_metrics_before_monitor_thread(tmp_path: Path) -> None:
    monitor = make_monitor(tmp_path)
    calls = 0

    def usage() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "cache_hits": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }

    async def idle() -> None:
        while not monitor._stop_event.is_set():
            await asyncio.sleep(0.01)

    monitor._store.llm_usage_24h = usage  # type: ignore[method-assign]
    monitor.run_forever = idle  # type: ignore[method-assign]

    monitor.start()
    try:
        assert calls == 1
    finally:
        monitor.stop()


def test_cached_title_projection_reads_store_outside_monitor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_trader import polymarket_monitor

    monitor = make_monitor(tmp_path)
    monitor._events = {"event-1": {"title": "Will this happen?"}}
    lock_states: list[bool] = []

    def cached_title(_store: object, _title: str) -> str:
        lock_states.append(monitor._lock._is_owned())
        return "这会发生吗？"

    monkeypatch.setattr(polymarket_monitor, "cached_prediction_title_zh", cached_title)
    monitor._apply_cached_title_projections()

    assert lock_states == [False]
    assert monitor._events["event-1"]["title_zh"] == "这会发生吗？"


def test_cross_venue_tokens_join_existing_subscription_and_refresh_once(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.books.update(
        {
            "cross-a": threshold_book("cross-a", ask="0.40", bid="0.39"),
            "cross-b": threshold_book("cross-b", ask="0.50", bid="0.49"),
        }
    )
    monitor = make_monitor(tmp_path)
    monitor._market_by_token = {"standard": "market-1"}
    monitor._relation_by_token = {"threshold": {"relation-1"}}
    client = FakePublicClient()

    monitor.set_cross_venue_tokens(("cross-b", "cross-a", "cross-a"))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))

    assert len(FakePublicClient.subscribe_specs) == 1
    assert set(FakePublicClient.subscribe_specs[0].token_ids) == {
        "standard",
        "threshold",
        "cross-a",
        "cross-b",
    }
    assert FakePublicClient.book_calls == [["cross-a", "cross-b"]]
    assert set(monitor.cross_venue_books(("cross-a", "cross-b"))) == {
        "cross-a",
        "cross-b",
    }
    assert monitor._market_by_token == {"standard": "market-1"}
    assert monitor._relation_by_token == {"threshold": {"relation-1"}}
    websocket = monitor.snapshot()["relation_discovery"]["websocket"]
    assert websocket["standard_subscribed_tokens"] == 1
    assert websocket["subscribed_tokens"] == 4


def test_failed_subscription_replacement_reports_installed_tokens(
    tmp_path: Path,
) -> None:
    setup_public([])

    class FailingReplacementClient(FakePublicClient):
        def __init__(self) -> None:
            super().__init__()
            self.subscribe_calls = 0

        def subscribe(self, spec: object) -> FakeStream:
            self.subscribe_specs.append(spec)
            self.subscribe_calls += 1
            if self.subscribe_calls > 1:
                raise ConnectionError("replacement failed")
            return self.stream

    monitor = make_monitor(tmp_path)
    monitor._market_by_token = {"old-a": "market-1", "old-b": "market-1"}
    monitor._subscription_dirty = True
    client = FailingReplacementClient()
    asyncio.run(monitor._refresh_subscription_if_dirty(client))

    monitor._market_by_token = {"new": "market-2"}
    monitor._subscription_dirty = True
    with pytest.raises(ConnectionError, match="replacement failed"):
        asyncio.run(monitor._refresh_subscription_if_dirty(client))

    websocket = monitor.snapshot()["relation_discovery"]["websocket"]
    assert monitor._stream_handle is client.stream
    assert websocket["standard_subscribed_tokens"] == 0
    assert websocket["subscribed_tokens"] == 2


def test_relation_token_union_controls_resubscribe_and_uses_only_buy_legs(
    tmp_path: Path,
) -> None:
    setup_public([])
    base = discover_threshold_relations([threshold_event()])[0]
    old_relation = replace(base, relation_id="old-relation")
    rotated_relation = replace(base, relation_id="rotated-relation")
    changed_market_a = replace(
        base.market_a,
        yes_token_id="new-buy-a",
    )
    changed_relation = replace(
        base,
        relation_id="changed-relation",
        market_a=changed_market_a,
        buy_leg_a=replace(base.buy_leg_a, token_id="new-buy-a"),
    )

    class RotatingClient(FakePublicClient):
        def __init__(self) -> None:
            super().__init__()
            self.created_streams = [self.stream]

        def subscribe(self, spec: object) -> FakeStream:
            self.subscribe_specs.append(spec)
            stream = FakeStream()
            self.created_streams.append(stream)
            return stream

    monitor = make_monitor(tmp_path)
    monitor._relations = {old_relation.relation_id: old_relation}
    monitor._active_relation_ids = {old_relation.relation_id}
    monitor._realtime_relation_ids = {old_relation.relation_id}
    monitor._rebuild_relation_subscriptions()
    monitor._subscription_dirty = True
    client = RotatingClient()
    asyncio.run(monitor._refresh_subscription_if_dirty(client))
    first_handle = monitor._stream_handle
    first_connected_at = monitor._stream_connected_at

    monitor._relations = {rotated_relation.relation_id: rotated_relation}
    monitor._active_relation_ids = {rotated_relation.relation_id}
    monitor._realtime_relation_ids = {rotated_relation.relation_id}
    monitor._rebuild_relation_subscriptions()
    asyncio.run(monitor._refresh_subscription_if_dirty(client))

    assert set(monitor._relation_by_token) == {
        rotated_relation.buy_leg_a.token_id,
        rotated_relation.buy_leg_b.token_id,
    }
    assert set().union(*monitor._relation_by_token.values()) == {
        rotated_relation.relation_id
    }
    assert monitor._subscription_dirty is False
    assert len(FakePublicClient.subscribe_specs) == 1
    assert monitor._stream_handle is first_handle
    assert monitor._stream_connected_at == first_connected_at

    monitor._relations = {changed_relation.relation_id: changed_relation}
    monitor._active_relation_ids = {changed_relation.relation_id}
    monitor._realtime_relation_ids = {changed_relation.relation_id}
    monitor._rebuild_relation_subscriptions()
    assert monitor._subscription_dirty is True
    asyncio.run(monitor._refresh_subscription_if_dirty(client))

    assert len(FakePublicClient.subscribe_specs) == 2
    assert monitor._stream_handle is client.created_streams[-1]
    assert first_handle is not None
    assert first_handle.closed is True


def test_cross_venue_rest_failure_does_not_block_same_venue_subscription(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.fail_get_order_books = True
    monitor = make_monitor(tmp_path)
    monitor._market_by_token = {"standard": "market-1"}
    monitor._relation_by_token = {"threshold": {"relation-1"}}
    monitor.set_cross_venue_tokens(("cross-failing",))

    asyncio.run(monitor._refresh_subscription_if_dirty(FakePublicClient()))

    assert len(FakePublicClient.subscribe_specs) == 1
    assert set(FakePublicClient.subscribe_specs[0].token_ids) == {
        "standard",
        "threshold",
        "cross-failing",
    }
    assert monitor.cross_venue_books(("cross-failing",)) == {}
    assert monitor._subscription_dirty is False


def test_cross_venue_generation_discards_concurrent_old_refresh_and_subscription(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.books.update(
        {
            "cross-old": threshold_book("cross-old", ask="0.40", bid="0.39"),
            "cross-new": threshold_book("cross-new", ask="0.50", bid="0.49"),
        }
    )

    class BlockingClient(FakePublicClient):
        def __init__(self) -> None:
            super().__init__()
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def get_order_books(self, *, token_ids: list[str]) -> tuple[object, ...]:
            requested = list(token_ids)
            self.book_calls.append(requested)
            if requested == ["cross-old"]:
                self.refresh_started.set()
                await self.release_refresh.wait()
            return tuple(self.books[token_id] for token_id in reversed(token_ids))

    async def exercise() -> None:
        monitor = make_monitor(tmp_path)
        monitor._market_by_token = {"standard": "market-1"}
        monitor._relation_by_token = {"threshold": {"relation-1"}}
        client = BlockingClient()
        monitor.set_cross_venue_tokens(("cross-old",))

        old_refresh = asyncio.create_task(
            monitor._refresh_subscription_if_dirty(client)
        )
        await asyncio.wait_for(client.refresh_started.wait(), timeout=1)
        monitor.set_cross_venue_tokens(("cross-new",))
        client.release_refresh.set()
        await old_refresh

        assert FakePublicClient.subscribe_specs == []
        assert monitor.cross_venue_books(("cross-old", "cross-new")) == {}
        assert monitor._subscription_dirty is True

        await monitor._refresh_subscription_if_dirty(client)

        assert len(FakePublicClient.subscribe_specs) == 1
        assert set(FakePublicClient.subscribe_specs[0].token_ids) == {
            "standard",
            "threshold",
            "cross-new",
        }
        assert "cross-old" not in FakePublicClient.subscribe_specs[0].token_ids
        assert set(monitor.cross_venue_books(("cross-old", "cross-new"))) == {
            "cross-new"
        }

    asyncio.run(exercise())


def test_obsolete_cross_refresh_failure_preserves_current_overlapping_book(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.books.update(
        {
            "cross-new": threshold_book("cross-new", ask="0.50", bid="0.49"),
            "cross-shared": threshold_book(
                "cross-shared", ask="0.60", bid="0.59"
            ),
        }
    )

    class BlockingFailureClient(FakePublicClient):
        def __init__(self) -> None:
            super().__init__()
            self.refresh_started = asyncio.Event()
            self.release_failure = asyncio.Event()

        async def get_order_books(self, *, token_ids: list[str]) -> tuple[object, ...]:
            assert set(token_ids) == {"cross-old", "cross-shared"}
            self.refresh_started.set()
            await self.release_failure.wait()
            raise ConnectionError("obsolete cross refresh failed")

    async def exercise() -> None:
        monitor = make_monitor(tmp_path)
        client = BlockingFailureClient()
        monitor.set_cross_venue_tokens(("cross-old", "cross-shared"))
        obsolete_refresh = asyncio.create_task(
            monitor._refresh_subscription_if_dirty(client)
        )
        await asyncio.wait_for(client.refresh_started.wait(), timeout=1)

        monitor.set_cross_venue_tokens(("cross-new", "cross-shared"))
        confirmed = await monitor._confirm_cross_venue_books(
            ("cross-new", "cross-shared")
        )
        assert set(confirmed) == {"cross-new", "cross-shared"}
        assert set(monitor.cross_venue_books(("cross-new", "cross-shared"))) == {
            "cross-new",
            "cross-shared",
        }

        client.release_failure.set()
        await obsolete_refresh

        current = monitor.cross_venue_books(
            ("cross-old", "cross-new", "cross-shared")
        )
        assert set(current) == {"cross-new", "cross-shared"}
        assert current["cross-shared"].asks[0].price == Decimal("0.60")
        assert FakePublicClient.subscribe_specs == []

    asyncio.run(exercise())


def test_cross_venue_stream_books_apply_full_books_and_supported_deltas(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.books["cross-a"] = threshold_book(
        "cross-a", ask="0.40", bid="0.39", now=NOW
    )
    monitor = make_monitor(tmp_path)
    client = FakePublicClient()
    monitor.set_cross_venue_tokens(("cross-a",))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))
    FakePublicClient.book_calls.clear()

    asyncio.run(
        monitor._process_stream_event(
            client,
            ns(
                type="book",
                payload=ns(
                    asset_id="cross-a",
                    timestamp=NOW + timedelta(seconds=1),
                    asks=(ns(price="0.41", size="8"),),
                    bids=(ns(price="0.38", size="7"),),
                ),
            ),
        )
    )
    asyncio.run(
        monitor._process_stream_event(
            client,
            ns(
                type="price_change",
                payload=ns(
                    timestamp=NOW + timedelta(seconds=2),
                    price_changes=(
                        ns(asset_id="cross-a", side="SELL", price="0.42", size="6"),
                        ns(asset_id="cross-a", side="BUY", price="0.39", size="5"),
                    ),
                ),
            ),
        )
    )

    book = monitor.cross_venue_books(("cross-a",))["cross-a"]
    assert book.asks == (
        type(book.asks[0])(price=Decimal("0.41"), size=Decimal("8")),
        type(book.asks[0])(price=Decimal("0.42"), size=Decimal("6")),
    )
    assert book.bids == (
        type(book.bids[0])(price=Decimal("0.39"), size=Decimal("5")),
        type(book.bids[0])(price=Decimal("0.38"), size=Decimal("7")),
    )
    assert FakePublicClient.book_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        ns(
            timestamp=NOW - timedelta(seconds=1),
            price_changes=(
                ns(asset_id="cross-a", side="SELL", price="0.42", size="6"),
            ),
        ),
        ns(
            timestamp=NOW + timedelta(seconds=1),
            price_changes=(
                ns(asset_id="cross-a", side="SELL", price="NaN", size="6"),
            ),
        ),
    ],
)
def test_cross_venue_malformed_or_out_of_order_delta_invalidates_without_rest(
    tmp_path: Path, payload: object,
) -> None:
    setup_public([])
    FakePublicClient.books["cross-a"] = threshold_book(
        "cross-a", ask="0.40", bid="0.39", now=NOW
    )
    monitor = make_monitor(tmp_path)
    client = FakePublicClient()
    monitor.set_cross_venue_tokens(("cross-a",))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))
    FakePublicClient.book_calls.clear()

    asyncio.run(
        monitor._process_stream_event(
            client, ns(type="price_change", payload=payload)
        )
    )

    assert monitor.cross_venue_books(("cross-a",)) == {}
    assert FakePublicClient.book_calls == []


def test_cross_venue_token_replacement_drops_removed_books(
    tmp_path: Path,
) -> None:
    setup_public([])
    FakePublicClient.books.update(
        {
            "cross-a": threshold_book("cross-a", ask="0.40", bid="0.39"),
            "cross-b": threshold_book("cross-b", ask="0.50", bid="0.49"),
        }
    )
    monitor = make_monitor(tmp_path)
    client = FakePublicClient()
    monitor.set_cross_venue_tokens(("cross-a",))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))
    FakePublicClient.subscribe_specs.clear()
    FakePublicClient.book_calls.clear()

    monitor.set_cross_venue_tokens(("cross-b",))
    asyncio.run(monitor._refresh_subscription_if_dirty(client))

    assert len(FakePublicClient.subscribe_specs) == 1
    assert FakePublicClient.book_calls == [["cross-b"]]
    assert monitor.cross_venue_books(("cross-a", "cross-b")) == {
        "cross-b": monitor.cross_venue_books(("cross-b",))["cross-b"]
    }


def test_restart_loads_fresh_relation_catalog_without_full_scan(tmp_path: Path) -> None:
    relation = discover_threshold_relations([threshold_event()])[0]
    setup_public([])
    db = PredictionArbitrageStore(tmp_path / "data")
    db.save_relation_state(
        {"relations": [threshold_relation_payload(relation)]},
        full_scanned_at=NOW.isoformat(),
    )
    monitor = make_monitor(tmp_path)
    monitor._load_relation_catalog()
    assert set(monitor._relations) == {relation.relation_id}
    assert FakePublicClient.list_events_calls == []


def test_restart_rehydrates_persisted_catalog_funnel_counts(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    expected = monitor.snapshot()["relation_discovery"]["catalog"]

    setup_public([])
    restarted = make_monitor(tmp_path)
    restarted._load_relation_catalog()
    catalog = restarted.snapshot()["relation_discovery"]["catalog"]
    for key in (
        "events_seen",
        "events_eligible",
        "markets_seen",
        "markets_normalized",
        "threshold_markets",
        "unique_tokens",
        "relation_count",
        "rejection_counts",
    ):
        assert catalog[key] == expected[key]


def test_restart_restores_event_scan_activity_due_state(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    assert asyncio.run(
        monitor._refresh_relation_event(FakePublicClient(), "threshold-event")
    )
    completed_at = monitor.snapshot()["relation_discovery"]["catalog"][
        "last_event_run"
    ]["completed_at"]

    restarted = make_monitor(tmp_path)
    restarted._load_relation_catalog()
    catalog = restarted.snapshot()["relation_discovery"]["catalog"]
    assert catalog["activity_scan_due"] is True
    assert catalog["activity_scan_due_at"] == completed_at


def test_event_scan_without_full_anchor_stays_due_for_full_scan(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(tmp_path)
    assert not asyncio.run(
        monitor._refresh_relation_event(FakePublicClient(), "threshold-event")
    )
    assert monitor._catalog_full_scanned_at is None
    assert monitor._catalog_due(NOW) is True
    assert monitor._store.load_relation_state() is None


def test_full_scan_consumes_every_paginator_page_and_publishes_once(
    tmp_path: Path,
) -> None:
    rows = [
        event(
            f"ordinary-{index}",
            markets=(market(f"market-{index}"),),
        )
        for index in range(21)
    ] + [threshold_event()]
    setup_public(rows)
    FakePublicClient.page_mode = True
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    state = monitor._store.load_relation_state()
    assert state is not None
    assert state["relations"]
    assert PagePaginator.iter_calls == 1
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "healthy"


def test_full_scan_saves_discovery_but_does_not_grow_v2_catalog(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    catalog = RelationCatalog(tmp_path / "catalog")
    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=FakeTrading(),
        public_client_factory=FakePublicClient,
        clock=lambda: NOW,
        relation_discovery=discover_threshold_relation_catalog,
        relation_validator=FakeRelationValidator(),
        relation_catalog=catalog,
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    state = monitor._store.load_relation_state()
    assert state is not None
    assert state["relations"]
    assert catalog.review_rows() == []
    assert catalog.current_generation() == {}


def test_failed_full_scan_keeps_previous_catalog(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    before = set(monitor._relations)
    FakePublicClient.fail_list_events = True
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    assert set(monitor._relations) == before
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "degraded"


def test_candidate_prepare_failure_does_not_fail_full_scan(tmp_path: Path) -> None:
    setup_public(
        [
            event(
                "threshold-event",
                volume="250",
                markets=(
                    threshold_market(
                        "threshold-low",
                        question="Will Bitcoin be above $80,000 on December 31?",
                        yes="yes-low",
                        no="no-low",
                    ),
                    threshold_market(
                        "threshold-mid",
                        question="Will Bitcoin be above $90,000 on December 31?",
                        yes="yes-mid",
                        no="no-mid",
                    ),
                    threshold_market(
                        "threshold-high",
                        question="Will Bitcoin be above $100,000 on December 31?",
                        yes="yes-high",
                        no="no-high",
                    ),
                ),
            )
        ]
    )

    def fail_ingest(relation: object) -> dict[str, object]:
        raise RuntimeError("sentinel ingest failure")

    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=FakeTrading(),
        public_client_factory=FakePublicClient,
        clock=lambda: NOW,
        relation_discovery=discover_threshold_relation_catalog,
        relation_validator=FakeRelationValidator(),
        relation_catalog=SimpleNamespace(ingest_threshold_relation=fail_ingest),
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    assert monitor._relations_failed is False
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "healthy"


def test_full_scan_due_at_twenty_four_hours_without_blocking_universe(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    now = [NOW + timedelta(seconds=1)]
    monitor = make_monitor(tmp_path, clock=lambda: now[0])
    monitor._catalog_full_scanned_at = NOW
    monitor._catalog_last_attempt_at = NOW

    async def exercise() -> None:
        client = FakePublicClient()
        now[0] = NOW + timedelta(hours=23, minutes=59, seconds=59)
        monitor._maybe_schedule_full_scan(client)
        assert monitor._full_scan_task is None
        now[0] = NOW + timedelta(hours=24)
        monitor._maybe_schedule_full_scan(client)
        first = monitor._full_scan_task
        assert first is not None
        monitor._maybe_schedule_full_scan(client)
        assert monitor._full_scan_task is first
        await monitor._refresh_universe_bounded(client)
        await first

    asyncio.run(exercise())
    assert FakePublicClient.list_events_calls
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "healthy"


def test_event_only_scan_replaces_one_event_and_preserves_full_timestamp(
    tmp_path: Path,
) -> None:
    original = threshold_event()
    ordinary = event("ordinary", markets=(market("ordinary-market"),))
    setup_public([original, ordinary])
    monitor = make_monitor(tmp_path)
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    full_scanned_at = monitor.snapshot()["relation_discovery"]["catalog"][
        "full_scanned_at"
    ]
    previous_ids = set(monitor._relations)
    replacement = event(
        "threshold-event",
        markets=(
            threshold_market(
                "threshold-low-new",
                question="Will Bitcoin be above $91,000 on December 31?",
                yes="yes-low-new",
                no="no-low-new",
            ),
            threshold_market(
                "threshold-high-new",
                question="Will Bitcoin be above $101,000 on December 31?",
                yes="yes-high-new",
                no="no-high-new",
            ),
        ),
    )
    setup_public([replacement, ordinary])
    assert asyncio.run(monitor._refresh_relation_event(FakePublicClient(), "threshold-event"))
    assert FakePublicClient.get_event_calls == ["threshold-event"]
    assert set(monitor._relations) != previous_ids
    assert all(
        relation.event_id == "threshold-event" or relation.event_id == "ordinary"
        for relation in monitor._relations.values()
    )
    assert monitor.snapshot()["relation_discovery"]["catalog"]["full_scanned_at"] == full_scanned_at
    assert monitor.snapshot()["relation_discovery"]["catalog"]["activity_scan_due_at"] is not None

    before = set(monitor._relations)
    full_before_failure = monitor.snapshot()["relation_discovery"]["catalog"][
        "full_scanned_at"
    ]
    FakePublicClient.fail_get_event = True
    assert not asyncio.run(
        monitor._refresh_relation_event(FakePublicClient(), "threshold-event")
    )
    assert set(monitor._relations) == before
    assert monitor.snapshot()["relation_discovery"]["catalog"]["full_scanned_at"] == full_before_failure


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
    *,
    low_ask: str = "0.40",
    high_no_ask: str = "0.48",
    size: str = "20",
    token_prefix: str = "",
) -> None:
    prefix = token_prefix or ""
    FakePublicClient.books.update(
        {
            f"{prefix}yes-low": threshold_book(f"{prefix}yes-low", ask=low_ask, bid="0.39", size=size),
            f"{prefix}no-low": threshold_book(f"{prefix}no-low", ask="0.60", bid="0.59", size=size),
            f"{prefix}yes-high": threshold_book(f"{prefix}yes-high", ask="0.60", bid="0.59", size=size),
            f"{prefix}no-high": threshold_book(f"{prefix}no-high", ask=high_no_ask, bid="0.47", size=size),
        }
    )


def test_activity_scan_reconsiders_complete_catalog_each_minute(tmp_path: Path) -> None:
    first = threshold_event(event_id="threshold-a", token_prefix="a-")
    second = threshold_event(event_id="threshold-b", token_prefix="b-")
    setup_public([first, second])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="a-")
    setup_threshold_books(low_ask="0.60", high_no_ask="0.60", token_prefix="b-")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))

    asyncio.run(monitor._refresh_relation_activity(client))
    first_activity = monitor.snapshot()["relation_discovery"]["activity"]
    assert first_activity["relations_considered"] == 2
    assert first_activity["relations_within_5pct"] == 1

    FakePublicClient.books["b-yes-low"] = threshold_book(
        "b-yes-low", ask="0.50", bid="0.49"
    )
    FakePublicClient.books["b-no-high"] = threshold_book(
        "b-no-high", ask="0.52", bid="0.51"
    )
    asyncio.run(monitor._refresh_relation_activity(client))
    second_activity = monitor.snapshot()["relation_discovery"]["activity"]
    assert second_activity["relations_considered"] == 2
    assert second_activity["relations_within_5pct"] == 2


def test_activity_scan_keeps_one_minute_start_to_start_cadence(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    now = [NOW]
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
        clock=lambda: now[0],
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    original = monitor._refresh_relation_books

    async def delayed_books(
        selected_client: object, relation_ids: set[str] | None = None,
    ) -> None:
        await original(selected_client, relation_ids)
        now[0] = NOW + timedelta(seconds=30)

    monitor._refresh_relation_books = delayed_books  # type: ignore[method-assign]

    asyncio.run(monitor._refresh_relation_activity(client))

    assert monitor._activity_next_scan_at == NOW + timedelta(seconds=60)


def test_universe_refresh_subscribes_once_when_relation_activity_is_due(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    FakePublicClient.subscribe_specs.clear()

    monitor.refresh_once()

    assert len(FakePublicClient.subscribe_specs) == 1


def test_subscription_refresh_swaps_handles_without_disconnect_window(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class RecordingStream(FakeStream):
        def __init__(self, label: str) -> None:
            super().__init__()
            self.label = label

        async def close(self) -> None:
            events.append(f"close:{self.label}")
            await super().close()

    class RotatingClient:
        def __init__(self) -> None:
            self.streams = [RecordingStream("old"), RecordingStream("new")]

        def subscribe(self, spec: object) -> RecordingStream:
            del spec
            if not self.streams:
                events.append("subscribe:failed")
                raise ConnectionError("replacement failed")
            stream = self.streams.pop(0)
            events.append(f"subscribe:{stream.label}")
            return stream

    monitor = make_monitor(tmp_path)
    monitor._market_by_token = {"token-1": "market-1"}
    client = RotatingClient()

    async def exercise() -> None:
        await monitor._subscribe(client)
        old = monitor._stream_handle
        await monitor._subscribe(client)

        assert events == ["subscribe:old", "subscribe:new", "close:old"]
        assert old is not None and old.closed is True
        assert monitor._stream_handle is not old
        assert monitor._stream_handle.closed is False  # type: ignore[union-attr]
        current = monitor._stream_handle
        with pytest.raises(ConnectionError, match="replacement failed"):
            await monitor._subscribe(client)
        assert monitor._stream_handle is current
        assert monitor._stream_handle.closed is False  # type: ignore[union-attr]

    asyncio.run(exercise())


def test_bounded_top_twenty_refresh_does_not_wait_for_relation_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader import polymarket_monitor

    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))

    async def slow_relation_activity(
        client: object, *, resubscribe: bool = True,
    ) -> None:
        del client, resubscribe
        await asyncio.sleep(0.2)

    monkeypatch.setattr(
        monitor, "_refresh_relation_activity", slow_relation_activity
    )
    monkeypatch.setattr(
        polymarket_monitor, "PUBLIC_REFRESH_TIMEOUT_SECONDS", 0.05
    )

    started = time.monotonic()
    asyncio.run(monitor._refresh_universe_bounded(FakePublicClient()))

    assert time.monotonic() - started < 0.1
    assert monitor.snapshot()["diagnostics"]["last_error"] is None


def test_background_monitor_prioritizes_top_twenty_before_bulk_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._catalog_loaded = True
    calls: list[str] = []

    async def refresh_universe(
        client: object, *, subscribe: bool = True,
    ) -> None:
        del client, subscribe
        calls.append("universe")
        monitor._universe_at = NOW

    def schedule_full(client: object) -> None:
        del client
        calls.append("full")

    def schedule_activity(client: object) -> None:
        del client
        calls.append("activity")
        monitor._stop_event.set()

    monkeypatch.setattr(monitor, "_refresh_universe_bounded", refresh_universe)
    monkeypatch.setattr(monitor, "_maybe_schedule_full_scan", schedule_full)
    monkeypatch.setattr(
        monitor, "_maybe_schedule_activity_scan", schedule_activity
    )

    asyncio.run(monitor.run_forever())

    assert calls[:3] == ["universe", "full", "activity"]


def test_background_monitor_refreshes_top_twenty_while_bulk_scan_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._catalog_loaded = True
    calls: list[str] = []

    async def refresh_universe(
        client: object, *, subscribe: bool = True,
    ) -> None:
        del client, subscribe
        calls.append("universe")
        monitor._universe_at = NOW

    def schedule_full(client: object) -> None:
        del client
        calls.append("full")

    def schedule_activity(client: object) -> None:
        del client
        calls.append("activity")
        monitor._stop_event.set()

    async def exercise() -> None:
        async def blocked_bulk_scan() -> None:
            await asyncio.Event().wait()

        monitor._activity_scan_task = asyncio.create_task(blocked_bulk_scan())
        await monitor.run_forever()

    monkeypatch.setattr(monitor, "_refresh_universe_bounded", refresh_universe)
    monkeypatch.setattr(monitor, "_maybe_schedule_full_scan", schedule_full)
    monkeypatch.setattr(
        monitor, "_maybe_schedule_activity_scan", schedule_activity
    )

    asyncio.run(exercise())

    assert calls[:3] == ["universe", "full", "activity"]


def test_background_bulk_scan_uses_and_closes_a_separate_public_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    main_client = object()

    class BulkClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    bulk_client = BulkClient()
    monitor._public_client_factory = lambda: bulk_client
    seen: list[object] = []

    async def activity_scan(client: object) -> None:
        seen.append(client)

    monkeypatch.setattr(monitor, "_run_activity_scan", activity_scan)

    async def exercise() -> None:
        monitor._maybe_schedule_activity_scan(main_client)
        assert monitor._activity_scan_task is not None
        await monitor._activity_scan_task

    asyncio.run(exercise())

    assert seen == [bulk_client]
    assert bulk_client.closed is True


def test_bulk_client_factory_failure_is_recorded_by_the_activity_scan(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor._activity = {**monitor._activity, "status": "healthy"}

    def fail_factory() -> object:
        raise ConnectionError("sentinel client construction failure")

    monitor._public_client_factory = fail_factory

    async def exercise() -> None:
        monitor._maybe_schedule_activity_scan(object())
        assert monitor._activity_scan_task is not None
        await monitor._activity_scan_task

    asyncio.run(exercise())

    assert monitor._activity["status"] == "degraded"
    assert monitor.snapshot()["diagnostics"]["last_error"] == "relations:ConnectionError"


def test_bulk_client_factory_failure_is_recorded_by_the_full_scan(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )

    def fail_factory() -> object:
        raise ConnectionError("sentinel client construction failure")

    monitor._public_client_factory = fail_factory

    async def exercise() -> None:
        monitor._maybe_schedule_full_scan(object())
        assert monitor._full_scan_task is not None
        await monitor._full_scan_task

    asyncio.run(exercise())

    catalog = monitor.snapshot()["relation_discovery"]["catalog"]
    assert catalog["status"] == "degraded"
    assert catalog["last_full_run"]["status"] == "failed"
    assert monitor.snapshot()["diagnostics"]["last_error"] == "relations:ConnectionError"


def test_full_scan_waits_for_the_minute_relation_writer(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )

    async def exercise() -> None:
        async def blocked_activity() -> None:
            await asyncio.Event().wait()

        activity = asyncio.create_task(blocked_activity())
        monitor._activity_scan_task = activity
        monitor._maybe_schedule_full_scan(FakePublicClient())
        assert monitor._full_scan_task is None
        activity.cancel()
        with pytest.raises(asyncio.CancelledError):
            await activity

    asyncio.run(exercise())


def test_due_full_scan_runs_before_activity_catchup(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._activity = {**monitor._activity, "status": "healthy"}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_activity(
        client: object, *, resubscribe: bool = True,
    ) -> None:
        del client, resubscribe
        entered.set()
        await release.wait()

    monitor._refresh_relation_activity = blocked_activity  # type: ignore[method-assign]

    async def exercise() -> None:
        activity = asyncio.create_task(
            monitor._run_activity_scan(FakePublicClient())
        )
        monitor._activity_scan_task = activity
        await entered.wait()
        monitor._activity_catchup_requested = True
        monitor._maybe_schedule_full_scan(FakePublicClient())
        assert monitor._full_scan_pending is True
        assert monitor._full_scan_task is None
        release.set()
        await activity
        assert monitor._full_scan_task is not None
        assert monitor._activity_scan_task is None
        await monitor._full_scan_task

    asyncio.run(exercise())


def test_targeted_relation_refresh_does_not_reuse_a_missing_leg(
    tmp_path: Path,
) -> None:
    setup_public(
        [
            threshold_event(event_id="threshold-a", token_prefix="a-"),
            threshold_event(event_id="threshold-b", token_prefix="b-"),
        ]
    )
    setup_threshold_books(token_prefix="a-")
    setup_threshold_books(token_prefix="b-")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()

    async def bootstrap() -> str:
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client, resubscribe=False)
        await monitor._drain_relation_validation(client)
        relation_id = next(
            relation_id
            for relation_id in monitor._active_relation_ids
            if monitor._relations[relation_id].event_id == "threshold-a"
        )
        rows = await monitor._refresh_relation_opportunities(client, {relation_id})
        monitor._merge_relation_rows(rows, {relation_id})
        return relation_id

    relation_id = asyncio.run(bootstrap())
    assert monitor.opportunity(relation_id) is not None

    class PartialBookClient(FakePublicClient):
        async def get_order_books(
            self, *, token_ids: list[str],
        ) -> tuple[object, ...]:
            self.book_calls.append(list(token_ids))
            return (self.books[token_ids[0]],)

    rows = asyncio.run(
        monitor._refresh_relation_opportunities(
            PartialBookClient(), {relation_id}
        )
    )

    assert rows == ()
    required = set(monitor._relation_buy_tokens(relation_id))
    assert required - set(monitor._relation_books)


def test_minute_activity_scan_removes_relations_that_leave_the_live_pool(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()

    async def bootstrap() -> str:
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client, resubscribe=False)
        await monitor._drain_relation_validation(client)
        relation_id = next(iter(monitor._active_relation_ids))
        rows = await monitor._refresh_relation_opportunities(client, {relation_id})
        monitor._merge_relation_rows(rows, {relation_id})
        return relation_id

    relation_id = asyncio.run(bootstrap())
    assert monitor.opportunity(relation_id) is not None
    FakePublicClient.books["yes-low"] = threshold_book(
        "yes-low", ask="0.60", bid="0.59"
    )
    FakePublicClient.books["no-high"] = threshold_book(
        "no-high", ask="0.60", bid="0.59"
    )

    asyncio.run(monitor._run_activity_scan(client))

    assert relation_id not in monitor._active_relation_ids
    assert monitor.opportunity(relation_id) is None


def test_top_twenty_refresh_preserves_current_threshold_opportunity(
    tmp_path: Path,
) -> None:
    setup_public([event("ordinary", markets=(market("ordinary-market"),))])
    monitor = make_monitor(tmp_path)
    threshold = {
        "opportunity_id": "threshold-relation",
        "market_type": "threshold_hedge",
        "confirmed_at": NOW,
    }
    monitor._opportunities["threshold-relation"] = threshold

    asyncio.run(monitor._refresh_universe_bounded(FakePublicClient()))

    assert monitor._opportunities["threshold-relation"] == threshold


def test_background_activity_scan_republishes_fresh_relation_opportunity(
    tmp_path: Path,
) -> None:
    now = [NOW]
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
        clock=lambda: now[0],
    )

    async def exercise() -> None:
        client = FakePublicClient()
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client)
        await monitor._drain_relation_validation(client)
        relation_id = next(iter(monitor._active_relation_ids))
        assert monitor.opportunity(relation_id) is not None
        now[0] += timedelta(seconds=11)
        assert monitor.opportunity(relation_id)["actionable"] is False  # type: ignore[index]

        FakePublicClient.subscribe_specs.clear()
        await monitor._run_activity_scan(client)

        refreshed = monitor.opportunity(relation_id)
        assert refreshed is not None
        assert refreshed["actionable"] is True
        assert refreshed["confirmed_age_seconds"] == 0
        assert FakePublicClient.subscribe_specs == []
        assert monitor._subscription_dirty is False

    asyncio.run(exercise())


def test_activity_scan_does_not_mark_daily_catalog_scanning(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    entered = asyncio.Event()
    release = asyncio.Event()
    original = monitor._refresh_relation_books

    async def blocked_refresh(client: object, relation_ids: set[str]) -> None:
        entered.set()
        await release.wait()
        await original(client, relation_ids)

    monitor._refresh_relation_books = blocked_refresh  # type: ignore[method-assign]

    async def exercise() -> None:
        task = asyncio.create_task(monitor._refresh_relation_activity(FakePublicClient()))
        await entered.wait()
        assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "healthy"
        release.set()
        await task

    asyncio.run(exercise())


def test_observed_milliseconds_preserve_first_positive_and_close_episode(
    tmp_path: Path,
) -> None:
    """A positive relation is one durable episode with millisecond boundaries."""

    now = [NOW]
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
        clock=lambda: now[0],
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor.refresh_once()
    relation_id = next(iter(monitor._active_relation_ids))
    signal = next(
        row
        for row in monitor._store.signal_history("all")
        if row.get("market_id") == relation_id
    )
    first_positive_at = signal.get("first_positive_at")
    initial_profit = signal.get("initial_profit")
    signal_id = signal["signal_id"]

    now[0] += timedelta(milliseconds=100)
    FakePublicClient.books["yes-low"] = threshold_book(
        "yes-low", ask="0.49", bid="0.48", now=now[0]
    )
    monitor.refresh_once()
    updated = monitor._store.signal(signal_id)
    assert updated["first_positive_at"] == first_positive_at
    assert updated["initial_profit"] == initial_profit
    assert updated["last_positive_at"] == now[0].isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert updated["peak_profit"] >= initial_profit

    now[0] += timedelta(milliseconds=175)
    FakePublicClient.books["yes-low"] = threshold_book(
        "yes-low", ask="0.52", bid="0.51", now=now[0]
    )
    FakePublicClient.books["no-high"] = threshold_book(
        "no-high", ask="0.50", bid="0.49", now=now[0]
    )
    monitor.refresh_once()
    closed = monitor._store.signal(signal_id)
    assert closed["observed_duration_ms"] == 275
    assert Decimal(closed["final_profit"]) < 0
    assert closed["ended_reason"] == "profit_non_positive"


def test_upsert_signal_persists_yes_no_action_identity_and_initial_profit(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(tmp_path)
    first = {
        "opportunity_id": "event-1:market-1",
        "event_id": "event-1",
        "market_id": "market-1",
        "question": "Will the event happen?",
        "market_type": "standard_binary",
        "estimated_profit": Decimal("0.11"),
        "yes_max_price": Decimal("0.42"),
        "no_max_price": Decimal("0.47"),
        "yes_max_cost": Decimal("8.40"),
        "no_max_cost": Decimal("9.40"),
        "total_max_cost": Decimal("17.80"),
    }
    signal_id = monitor._upsert_signal(first)
    assert signal_id

    monitor._upsert_signal(
        {
            **first,
            "estimated_profit": Decimal("0.22"),
            "yes_max_price": Decimal("0.43"),
            "no_max_price": Decimal("0.46"),
            "yes_max_cost": Decimal("8.60"),
            "no_max_cost": Decimal("9.20"),
        }
    )

    row = monitor._store.signal(signal_id)
    assert row["opportunity_id"] == "event-1:market-1"
    assert row["yes_max_price"] == "0.43"
    assert row["no_max_price"] == "0.46"
    assert row["yes_max_cost"] == "8.60"
    assert row["no_max_cost"] == "9.20"
    assert row["total_max_cost"] == "17.80"
    assert row["estimated_profit"] == "0.22"
    assert row["initial_profit"] == "0.11"


def test_qualified_yes_no_schedules_ready_before_nonblocking_shadow(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[list[str], list[tuple[str, str]], str]:
        monitor = make_monitor(tmp_path)
        events: list[str] = []
        shadow_calls: list[tuple[str, str]] = []
        monitor.set_ready_observer(lambda _opportunity_id, _signal_id: events.append("ready"))
        monitor.set_shadow_observer(
            lambda opportunity, signal_id: (
                events.append("shadow"),
                shadow_calls.append((str(opportunity["opportunity_id"]), signal_id)),
            )
        )
        signal_id = monitor._upsert_signal(
            {
                "opportunity_id": "event-1:market-1",
                "event_id": "event-1",
                "market_id": "market-1",
                "question": "Will the event happen?",
                "market_type": "standard_binary",
                "actionable": True,
                "estimated_profit": Decimal("0.11"),
            }
        )
        await asyncio.sleep(0.01)
        monitor._reap_notification_task()
        assert signal_id
        return events, shadow_calls, signal_id

    events, shadow_calls, signal_id = asyncio.run(exercise())
    assert events == ["ready", "shadow"]
    assert shadow_calls == [("event-1:market-1", signal_id)]


def test_first_positive_refetches_exact_event_and_verifies_rules(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor.refresh_once()

    relation_id = next(iter(monitor._active_relation_ids))
    row = monitor.opportunity(relation_id)
    assert row is not None
    assert FakePublicClient.get_event_calls == ["threshold-event"]
    assert row["rules_verified_at"] is not None
    assert row["rules_fingerprint"]


def test_same_relation_id_mutation_after_restart_refetches_before_positive(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor.refresh_once()
    relation_id = next(iter(monitor._active_relation_ids))
    original = monitor._relations[relation_id]
    mutated = replace(
        original,
        market_a=replace(original.market_a, rules=original.market_a.rules + " Changed."),
        rules_hash_a="mutated-rules-hash",
    )
    monitor._store.save_relation_state(
        {"relations": [threshold_relation_payload(mutated)]},
        full_scanned_at=NOW.isoformat(),
    )

    restarted = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    restarted._load_relation_catalog()
    restarted._active_relation_ids = {relation_id}
    FakePublicClient.get_event_calls.clear()
    restarted.refresh_once()

    assert FakePublicClient.get_event_calls == ["threshold-event"]
    closed = next(
        row
        for row in restarted._store.signal_history("all")
        if row.get("market_id") == relation_id
    )
    assert closed["ended_reason"] == "rules_changed"
    assert restarted.opportunity(relation_id) is None


def test_generic_open_signal_closes_on_stale_receive_timestamp(
    tmp_path: Path,
) -> None:
    now = [NOW]
    setup_public([])
    monitor = make_monitor(tmp_path, clock=lambda: now[0])
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": "generic-market",
            "event_id": "generic-event",
            "question": "generic",
            "started_at": NOW,
            "first_positive_at": NOW,
            "book_received_at_a": NOW,
            "book_received_at_b": NOW,
            "estimated_profit": Decimal("1"),
        }
    )

    now[0] += timedelta(seconds=10, milliseconds=1)
    monitor._maintain_open_signals()

    assert monitor._store.signal(signal_id)["ended_reason"] == "data_unavailable"


def test_ready_observer_is_called_once_for_order_ready_episode(tmp_path: Path) -> None:
    async def scenario() -> list[tuple[str, str]]:
        setup_public([threshold_event()])
        setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
        monitor = make_monitor(
            tmp_path,
            relation_discovery=discover_threshold_relations,
            relation_validator=FakeRelationValidator(),
        )
        calls: list[tuple[str, str]] = []

        def observer(opportunity_id: str, signal_id: str) -> dict[str, object]:
            calls.append((opportunity_id, signal_id))
            monitor._store.update_signal(
                signal_id,
                {"notification_state": "sent", "notification_attempts": 1},
            )
            return {"state": "sent"}

        monitor.set_ready_observer(observer)
        client = FakePublicClient()
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client)
        await monitor._drain_relation_validation(client)
        await monitor._refresh_relation_opportunities(client, set(monitor._active_relation_ids))
        await asyncio.sleep(0.05)
        monitor._reap_notification_task()
        await monitor._refresh_relation_opportunities(client, set(monitor._active_relation_ids))
        await asyncio.sleep(0.01)
        monitor._reap_notification_task()
        return calls

    calls = asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][0]
    assert calls[0][1]


def test_observation_alert_delivered_after_actionable_signal_closes(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[str], dict[str, object]]:
        setup_public([threshold_event()])
        setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
        monitor = make_monitor(
            tmp_path,
            relation_discovery=discover_threshold_relations,
            relation_validator=FakeRelationValidator(),
        )
        calls: list[str] = []

        def observer(
            opportunity: Mapping[str, object], signal_id: str, lease_id: str
        ) -> dict[str, object]:
            calls.append(signal_id)
            monitor._store.complete_notification_attempt(
                signal_id,
                lease_id,
                kind="observation",
                success=True,
            )
            return {"state": "sent"}

        monitor.set_observation_observer(observer)
        client = FakePublicClient()
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client)
        await monitor._drain_relation_validation(client)
        await monitor._refresh_relation_opportunities(
            client, set(monitor._active_relation_ids)
        )
        signal_id = str(monitor._store.open_signal_history()[0]["signal_id"])
        market_id = str(monitor._store.signal(signal_id)["market_id"])
        monitor._close_signal(market_id, "data_unavailable")
        await asyncio.sleep(0.05)
        monitor._reap_notification_task()
        return calls, monitor._store.signal(signal_id)

    calls, signal = asyncio.run(scenario())
    assert calls == [str(signal["signal_id"])]
    assert signal["observation_state"] == "sent"
    assert signal["ended_reason"] == "data_unavailable"


def test_observation_alert_then_order_ready_both_fire_once_per_episode(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        setup_public([threshold_event()])
        setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
        monitor = make_monitor(
            tmp_path,
            relation_discovery=discover_threshold_relations,
            relation_validator=FakeRelationValidator(),
        )
        observations: list[str] = []
        ready: list[str] = []

        def observation_observer(
            opportunity: Mapping[str, object], signal_id: str, lease_id: str
        ) -> dict[str, object]:
            observations.append(signal_id)
            return {"state": "sent"}

        def ready_observer(
            opportunity_id: str, signal_id: str
        ) -> dict[str, object]:
            ready.append(signal_id)
            monitor._store.update_signal(
                signal_id,
                {"notification_state": "sent", "notification_attempts": 1},
            )
            return {"state": "sent"}

        monitor.set_observation_observer(observation_observer)
        monitor.set_ready_observer(ready_observer)
        client = FakePublicClient()
        await monitor._run_full_relation_scan(client)
        await monitor._refresh_readiness()
        await monitor._refresh_relation_activity(client)
        await monitor._drain_relation_validation(client)
        await monitor._refresh_relation_opportunities(
            client, set(monitor._active_relation_ids)
        )
        await asyncio.sleep(0.05)
        monitor._reap_notification_task()
        await monitor._refresh_relation_opportunities(
            client, set(monitor._active_relation_ids)
        )
        await asyncio.sleep(0.01)
        monitor._reap_notification_task()
        return observations, ready

    observations, ready = asyncio.run(scenario())
    assert len(observations) == 1
    assert len(ready) == 1
    assert observations[0] == ready[0]


def test_schedule_ready_notification_calls_standard_without_rule_or_codex(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[tuple[str, str]]:
        monitor = make_monitor(tmp_path)
        calls: list[tuple[str, str]] = []

        def observer(opportunity_id: str, signal_id: str) -> dict[str, object]:
            calls.append((opportunity_id, signal_id))
            return {"state": "sent"}

        monitor.set_ready_observer(observer)
        signal_id = monitor._store.upsert_signal(
            {
                "opportunity_id": "event-standard:market-1",
                "market_id": "market-1",
                "event_id": "event-standard",
                "question": "Will it happen?",
                "market_type": "standard_binary",
                "started_at": NOW,
                "first_positive_at": NOW,
                "estimated_profit": Decimal("0.38"),
            }
        )
        monitor._schedule_ready_notification(
            signal_id,
            {
                "opportunity_id": "event-standard:market-1",
                "market_id": "market-1",
                "market_type": "standard_binary",
                "actionable": True,
            },
        )
        await asyncio.sleep(0.01)
        monitor._reap_notification_task()
        return calls

    assert asyncio.run(scenario())


def test_cancelled_notification_task_does_not_crash_monitor_shutdown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        monitor = make_monitor(tmp_path)
        task = asyncio.create_task(asyncio.sleep(60))
        monitor._notification_task = task
        monitor._notification_signal_id = "signal-1"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        monitor._reap_notification_task()

        assert monitor._notification_task is None
        assert monitor._notification_signal_id is None

    asyncio.run(scenario())


def test_event_refetch_failure_closes_episode_as_data_unavailable(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    relation_id = next(iter(monitor._active_relation_ids))
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": relation_id,
            "event_id": "threshold-event",
            "question": "threshold relation",
            "started_at": NOW,
            "estimated_profit": Decimal("1"),
        }
    )
    FakePublicClient.fail_get_event = True

    monitor.refresh_once()

    assert monitor._store.signal(signal_id)["ended_reason"] == "data_unavailable"
    assert monitor.opportunity(relation_id) is None


def test_relation_rule_failure_guard_resets_on_fresh_activity_scan(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    relation_id = next(iter(monitor._active_relation_ids))
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": relation_id,
            "event_id": "threshold-event",
            "question": "threshold relation",
            "started_at": NOW,
            "estimated_profit": Decimal("1"),
        }
    )
    FakePublicClient.events[0].markets[0].description += " Cancellation is possible."
    monitor.refresh_once()
    assert monitor._store.signal(signal_id)["ended_reason"] == "rules_changed"

    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor.refresh_once()

    open_rows = [
        row
        for row in monitor._store.signal_history("all")
        if row.get("market_id") == relation_id and row.get("ended_at") is None
    ]
    assert len(open_rows) == 1


def test_recovery_opens_new_signal_after_quote_age(tmp_path: Path) -> None:
    now = [NOW]
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
        clock=lambda: now[0],
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor.refresh_once()
    relation_id = next(iter(monitor._active_relation_ids))
    first_id = next(
        row["signal_id"]
        for row in monitor._store.signal_history("all")
        if row.get("market_id") == relation_id
    )

    now[0] += timedelta(seconds=10, milliseconds=1)
    monitor._maintain_open_signals()
    closed = monitor._store.signal(first_id)
    assert closed["ended_reason"] == "data_unavailable"

    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor.refresh_once()
    open_rows = [
        row
        for row in monitor._store.signal_history("all")
        if row.get("market_id") == relation_id and row.get("ended_at") is None
    ]
    assert len(open_rows) == 1
    assert open_rows[0]["signal_id"] != first_id


def test_rules_changed_closes_episode_before_actionability(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    relation_id = next(iter(monitor._active_relation_ids))
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": relation_id,
            "event_id": "threshold-event",
            "question": "threshold relation",
            "started_at": NOW,
            "estimated_profit": Decimal("1"),
        }
    )
    FakePublicClient.events[0].markets[0].description += " Cancellation is possible."

    monitor.refresh_once()

    closed = monitor._store.signal(signal_id)
    assert FakePublicClient.get_event_calls == ["threshold-event"]
    assert closed["ended_reason"] == "rules_changed"
    assert not any(
        row.get("relation_id") == relation_id
        for row in monitor.snapshot()["opportunities"]
    )


def test_zero_volume_display_metrics_do_not_block_activity_pool(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    relation_id, relation = next(iter(monitor._relations.items()))
    monitor._relations[relation_id] = replace(
        relation,
        event_volume_24h=Decimal("0"),
        event_liquidity=Decimal("0"),
        market_a=replace(relation.market_a, volume_24h=Decimal("0"), liquidity=Decimal("0")),
        market_b=replace(relation.market_b, volume_24h=Decimal("0"), liquidity=Decimal("0")),
    )

    asyncio.run(monitor._refresh_relation_activity(client))

    assert relation_id in monitor._active_relation_ids


def test_failed_activity_scan_keeps_last_completed_pool_and_counts(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    before = monitor.snapshot()["relation_discovery"]["activity"]
    active_before = set(monitor._active_relation_ids)
    FakePublicClient.fail_get_order_books = True

    asyncio.run(monitor._refresh_relation_activity(client))

    after = monitor.snapshot()["relation_discovery"]["activity"]
    assert after["status"] == "degraded"
    for key in ("relations_considered", "relations_within_5pct", "rejection_counts"):
        assert after[key] == before[key]
    assert set(monitor._active_relation_ids) == active_before
    assert monitor._relation_by_token


def test_affected_relations_price_refresh_is_targeted(tmp_path: Path) -> None:
    setup_public(
        [
            threshold_event(event_id="threshold-a", token_prefix="a-"),
            threshold_event(event_id="threshold-b", token_prefix="b-"),
        ]
    )
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="a-")
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="b-")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    FakePublicClient.book_calls.clear()
    relation_a = next(
        relation
        for relation in monitor._relations.values()
        if relation.event_id == "threshold-a"
    )

    asyncio.run(
        monitor._process_stream_event(
            client,
            ns(
                type="price_change",
                payload=ns(asset_id=relation_a.buy_leg_a.token_id, price_changes=()),
            ),
        )
    )

    assert FakePublicClient.book_calls == [
        sorted([relation_a.buy_leg_a.token_id, relation_a.buy_leg_b.token_id])
    ]


def test_activity_scheduler_marks_lagging_and_runs_one_catchup(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    now = [NOW]
    monitor._clock = lambda: now[0]
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = 0

    async def blocked_activity(
        client: object, *, resubscribe: bool = True,
    ) -> None:
        del client, resubscribe
        nonlocal starts
        starts += 1
        entered.set()
        await release.wait()

    monitor._refresh_relation_activity = blocked_activity  # type: ignore[method-assign]
    monitor._activity_next_scan_at = NOW

    async def exercise() -> None:
        client = FakePublicClient()
        await monitor._tick_relation_activity(client)
        await entered.wait()
        now[0] = NOW + timedelta(seconds=61)
        await monitor._tick_relation_activity(client)
        await monitor._tick_relation_activity(client)
        assert starts == 1
        assert monitor.snapshot()["relation_discovery"]["activity"]["status"] == "lagging"
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert starts == 2
        release.set()
        task = monitor._activity_scan_task
        if task is not None:
            await task

    asyncio.run(exercise())


def test_activity_scheduler_does_not_treat_its_own_due_time_as_lagging(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._clock = lambda: NOW
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = 0

    async def blocked_activity(
        client: object, *, resubscribe: bool = True,
    ) -> None:
        del client, resubscribe
        nonlocal starts
        starts += 1
        entered.set()
        await release.wait()

    monitor._refresh_relation_activity = blocked_activity  # type: ignore[method-assign]
    monitor._activity_next_scan_at = NOW

    async def exercise() -> None:
        client = FakePublicClient()
        await monitor._tick_relation_activity(client)
        await entered.wait()
        await monitor._tick_relation_activity(client)

        assert starts == 1
        assert monitor._activity_catchup_requested is False
        assert monitor.snapshot()["relation_discovery"]["activity"]["status"] != "lagging"

        release.set()
        task = monitor._activity_scan_task
        assert task is not None
        await task
        assert starts == 1

    asyncio.run(exercise())


def test_codex_worker_selects_highest_edge_then_reaps_one_at_a_time(
    tmp_path: Path,
) -> None:
    setup_public(
        [
            threshold_event(event_id="threshold-a", token_prefix="a-"),
            threshold_event(event_id="threshold-b", token_prefix="b-"),
        ]
    )
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="a-")
    setup_threshold_books(low_ask="0.49", high_no_ask="0.49", token_prefix="b-")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    expected = next(
        relation_id
        for relation_id, relation in monitor._relations.items()
        if relation.event_id == "threshold-b"
    )

    async def exercise() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_relation_id == expected
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        await monitor._poll_relation_validation(client)
        await asyncio.sleep(0.01)
        assert len(validator.relation_ids) == 2
        assert validator.relation_ids[0] == expected

    asyncio.run(exercise())


def test_codex_worker_queues_negative_two_percent_pool_relation(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.52", high_no_ask="0.50")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    relation_id = next(iter(monitor._active_relation_ids))
    assert monitor._activity["rejection_counts"]["eligible"] == 1

    async def exercise() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_relation_id == relation_id
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)

    asyncio.run(exercise())
    assert validator.relation_ids == [relation_id]
    assert monitor._codex_statuses[relation_id] == "approved"


def test_transient_codex_failure_retries_once_at_the_retry_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.polymarket_monitor as monitor_module

    monkeypatch.setattr(monitor_module, "RELATION_VALIDATION_RETRY_SECONDS", 60)
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    now = [NOW]
    validator = FakeRelationValidator("llm_unavailable")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
        clock=lambda: now[0],
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))

    async def tick() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        assert validator.calls == 1
        now[0] = NOW + timedelta(seconds=59)
        await monitor._poll_relation_validation(client)
        assert validator.calls == 1
        now[0] = NOW + timedelta(seconds=60)
        await monitor._poll_relation_validation(client)
        await asyncio.sleep(0.01)
        assert validator.calls == 2

    asyncio.run(tick())


def test_llm_unavailable_notifies_failure_observer_once_and_resets_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.polymarket_monitor as monitor_module

    monkeypatch.setattr(monitor_module, "RELATION_VALIDATION_RETRY_SECONDS", 60)
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    now = [NOW]
    validator = FakeRelationValidator("llm_unavailable")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
        clock=lambda: now[0],
    )
    calls: list[dict[str, object]] = []
    monitor.set_failure_observer(
        lambda payload: calls.append(dict(payload)) or {"state": "sent"}
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))

    async def scenario() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        await asyncio.sleep(0.01)
        monitor._reap_llm_failure_notification_task()
        assert len(calls) == 1
        assert calls[0]["component"] == "llm_validation"
        assert calls[0]["reason_codes"] == ["LLM_FAILED"]

        relation_id = next(iter(monitor._active_relation_ids))
        validator.status = "approved"
        monitor._codex_statuses[relation_id] = "pending"
        monitor._codex_retry_at.pop(relation_id, None)
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        assert validator.calls == 2

        validator.status = "llm_unavailable"
        validator.cached.pop(relation_id, None)
        monitor._codex_validations.pop(relation_id, None)
        monitor._codex_statuses[relation_id] = "pending"
        monitor._codex_retry_at.pop(relation_id, None)
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        await asyncio.sleep(0.01)
        monitor._reap_llm_failure_notification_task()
        assert len(calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_status", ["approved", "llm_rejected"])
def test_terminal_codex_cache_survives_pool_churn_and_restart(
    tmp_path: Path, terminal_status: str
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    validator = FakeRelationValidator(terminal_status)
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))

    async def validate_once() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)

    asyncio.run(validate_once())
    assert validator.calls == 1
    setup_threshold_books(low_ask="0.60", high_no_ask="0.60")
    asyncio.run(monitor._refresh_relation_activity(client))
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    asyncio.run(monitor._refresh_relation_activity(client))
    asyncio.run(monitor._poll_relation_validation(client))
    assert validator.calls == 1

    restarted = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    restarted._load_relation_catalog()
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    asyncio.run(restarted._refresh_relation_activity(client))

    async def restore_cached_opportunity() -> None:
        relation_ids = set(restarted._active_relation_ids)
        rows = await restarted._refresh_relation_opportunities(client, relation_ids)
        restarted._merge_relation_rows(rows, relation_ids)
        relation_id = next(iter(relation_ids))
        assert restarted.opportunity(relation_id)["llm_status"] == "llm_unavailable"  # type: ignore[index]

        await restarted._poll_relation_validation(client)

        restored = restarted.opportunity(relation_id)
        assert restored is not None
        assert restored["llm_status"] == terminal_status

    asyncio.run(restore_cached_opportunity())
    assert validator.calls == 1


def test_rejected_relations_leave_pending_subscriptions_intact(tmp_path: Path) -> None:
    setup_public(
        [
            threshold_event(event_id="threshold-a", token_prefix="a-"),
            threshold_event(event_id="threshold-b", token_prefix="b-"),
        ]
    )
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="a-")
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51", token_prefix="b-")
    validator = FakeRelationValidator("llm_rejected")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    monitor._market_by_token["top20-token"] = "top20-market"
    asyncio.run(monitor._refresh_relation_activity(client))
    relation_ids = set(monitor._active_relation_ids)
    assert len(relation_ids) == 2
    snapshot = monitor.snapshot()["relation_discovery"]
    assert snapshot["activity"]["subscribed_tokens"] == snapshot["websocket"]["subscribed_tokens"]

    async def reject_one() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await monitor._codex_task
        await monitor._poll_relation_validation(client)
        rejected = monitor._relations[validator.relation_ids[0]]
        rejected_tokens = set(monitor._relation_subscription_tokens(rejected.relation_id))
        subscribed_tokens = set(monitor._relation_by_token)
        assert subscribed_tokens
        assert not rejected_tokens & subscribed_tokens
        assert monitor.snapshot()["relation_discovery"]["codex_queue"]["rejected"] == 1

    asyncio.run(reject_one())


def test_activity_diagnostic_pool_is_uncapped_but_realtime_pool_is_pre_warmed(
    tmp_path: Path,
) -> None:
    setup_public([])
    base = discover_threshold_relations([threshold_event()])[0]
    relations = []
    for index in range(301):
        suffix = f"r{index:03d}"
        market_a = replace(
            base.market_a,
            market_id=f"market-a-{suffix}",
            condition_id=f"condition-a-{suffix}",
            yes_token_id=f"yes-a-{suffix}",
            no_token_id=f"no-a-{suffix}",
        )
        market_b = replace(
            base.market_b,
            market_id=f"market-b-{suffix}",
            condition_id=f"condition-b-{suffix}",
            yes_token_id=f"yes-b-{suffix}",
            no_token_id=f"no-b-{suffix}",
        )
        relation = replace(
            base,
            relation_id=f"relation-{suffix}",
            event_id=f"event-{suffix}",
            market_a=market_a,
            market_b=market_b,
            buy_leg_a=replace(
                base.buy_leg_a,
                market_id=market_a.market_id,
                condition_id=market_a.condition_id,
                token_id=market_a.yes_token_id,
            ),
            buy_leg_b=replace(
                base.buy_leg_b,
                market_id=market_b.market_id,
                condition_id=market_b.condition_id,
                token_id=market_b.no_token_id,
            ),
        )
        relations.append(relation)
        for token in (relation.buy_leg_a.token_id, relation.buy_leg_b.token_id):
            FakePublicClient.books[token] = threshold_book(token, ask="0.50", bid="0.49")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    monitor._set_relation_state(relations, ())

    async def scan() -> None:
        await monitor._refresh_relation_activity(FakePublicClient())

    asyncio.run(scan())
    assert len(monitor._active_relation_ids) == 301
    assert len(monitor._realtime_relation_ids) == 100
    activity = monitor.snapshot()["relation_discovery"]["activity"]
    assert activity["relations_within_5pct"] == 301
    assert activity["apr_target_relations"] == 0
    assert activity["apr_target_limit"] == 100
    assert activity["apr_prewarm_relations"] == 100
    assert activity["apr_prewarm_limit"] == 100
    assert activity["subscribed_relations"] == 100
    assert activity["relation_subscribed_tokens"] == 200
    assert len(monitor._relation_by_token) == 100 * 2
    subscribed_relation_ids = set().union(*monitor._relation_by_token.values())
    assert subscribed_relation_ids == monitor._realtime_relation_ids
    expected_tokens = {
        token
        for relation in relations
        if relation.relation_id in monitor._realtime_relation_ids
        for token in (relation.buy_leg_a.token_id, relation.buy_leg_b.token_id)
    }
    assert set(monitor._relation_by_token) == expected_tokens
    specs = FakePublicClient.subscribe_specs[-1]
    spec_list = specs if isinstance(specs, list) else [specs]
    assert all(len(spec.token_ids) <= 250 for spec in spec_list)
    assert {
        token
        for spec in spec_list
        for token in spec.token_ids
    } == expected_tokens


def test_apr_prewarm_ranking_uses_remaining_duration_and_relation_id_ties(
    tmp_path: Path,
) -> None:
    setup_public([])
    base = discover_threshold_relations([threshold_event()])[0]

    def variant(
        relation_id: str,
        *,
        end_a: str = "2026-12-31T17:00:00Z",
        end_b: str | None = None,
    ):
        market_a = replace(
            base.market_a,
            market_id=f"market-a-{relation_id}",
            condition_id=f"condition-a-{relation_id}",
            yes_token_id=f"yes-a-{relation_id}",
            no_token_id=f"no-a-{relation_id}",
            end_date=end_a,
        )
        market_b = replace(
            base.market_b,
            market_id=f"market-b-{relation_id}",
            condition_id=f"condition-b-{relation_id}",
            yes_token_id=f"yes-b-{relation_id}",
            no_token_id=f"no-b-{relation_id}",
            end_date=end_b or end_a,
        )
        return replace(
            base,
            relation_id=relation_id,
            market_a=market_a,
            market_b=market_b,
            buy_leg_a=replace(
                base.buy_leg_a,
                market_id=market_a.market_id,
                condition_id=market_a.condition_id,
                token_id=market_a.yes_token_id,
            ),
            buy_leg_b=replace(
                base.buy_leg_b,
                market_id=market_b.market_id,
                condition_id=market_b.condition_id,
                token_id=market_b.no_token_id,
            ),
        )

    relations = [variant(f"filler-{index:03d}") for index in range(99)]
    relations.extend(
        [
            variant("short", end_a="2026-09-25T17:00:00Z"),
            variant("long", end_a="2027-05-23T17:00:00Z"),
        ]
    )
    for relation in relations:
        FakePublicClient.books[relation.buy_leg_a.token_id] = threshold_book(
            relation.buy_leg_a.token_id,
            ask="0.49",
            bid="0.48",
        )
        FakePublicClient.books[relation.buy_leg_b.token_id] = threshold_book(
            relation.buy_leg_b.token_id,
            ask="0.49",
            bid="0.48",
        )

    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._set_relation_state(relations, ())
    asyncio.run(monitor._refresh_relation_activity(FakePublicClient()))

    assert len(monitor._realtime_relation_ids) == 100
    assert "short" in monitor._realtime_relation_ids
    assert "long" not in monitor._realtime_relation_ids
    assert "filler-000" in monitor._realtime_relation_ids
    assert "filler-098" in monitor._realtime_relation_ids


def test_apr_pool_excludes_invalid_resolution_dates_and_rejections(
    tmp_path: Path,
) -> None:
    setup_public([])
    base = discover_threshold_relations([threshold_event()])[0]

    def variant(
        relation_id: str,
        *,
        end_a: str = "2026-12-31T17:00:00Z",
        end_b: str | None = None,
    ):
        market_a = replace(
            base.market_a,
            market_id=f"market-a-{relation_id}",
            condition_id=f"condition-a-{relation_id}",
            yes_token_id=f"yes-a-{relation_id}",
            no_token_id=f"no-a-{relation_id}",
            end_date=end_a,
        )
        market_b = replace(
            base.market_b,
            market_id=f"market-b-{relation_id}",
            condition_id=f"condition-b-{relation_id}",
            yes_token_id=f"yes-b-{relation_id}",
            no_token_id=f"no-b-{relation_id}",
            end_date=end_b or end_a,
        )
        return replace(
            base,
            relation_id=relation_id,
            market_a=market_a,
            market_b=market_b,
            buy_leg_a=replace(
                base.buy_leg_a,
                market_id=market_a.market_id,
                condition_id=market_a.condition_id,
                token_id=market_a.yes_token_id,
            ),
            buy_leg_b=replace(
                base.buy_leg_b,
                market_id=market_b.market_id,
                condition_id=market_b.condition_id,
                token_id=market_b.no_token_id,
            ),
        )

    relations = [
        variant("valid"),
        variant("pending"),
        variant("unavailable"),
        variant("rejected"),
        variant("expired", end_a="2026-07-26T17:00:00Z"),
        variant(
            "mismatched",
            end_a="2026-12-31T17:00:00Z",
            end_b="2027-01-01T17:00:00Z",
        ),
        variant("missing", end_a="", end_b=""),
    ]
    for relation in relations:
        FakePublicClient.books[relation.buy_leg_a.token_id] = threshold_book(
            relation.buy_leg_a.token_id,
            ask="0.49",
            bid="0.48",
        )
        FakePublicClient.books[relation.buy_leg_b.token_id] = threshold_book(
            relation.buy_leg_b.token_id,
            ask="0.49",
            bid="0.48",
        )

    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._set_relation_state(relations, ())
    monitor._codex_statuses["unavailable"] = "llm_unavailable"
    monitor._codex_statuses["rejected"] = "llm_rejected"
    asyncio.run(monitor._refresh_relation_activity(FakePublicClient()))

    assert set(monitor._active_relation_ids) == {relation.relation_id for relation in relations}
    assert {"valid", "pending", "unavailable"} <= monitor._realtime_relation_ids
    assert not {
        "rejected",
        "expired",
        "mismatched",
        "missing",
    } & monitor._realtime_relation_ids


def test_apr_target_anomaly_preserves_last_pool_and_blocks_relation_actions(
    tmp_path: Path,
) -> None:
    setup_public([])
    base = discover_threshold_relations([threshold_event()])[0]

    def variant(relation_id: str):
        market_a = replace(
            base.market_a,
            market_id=f"market-a-{relation_id}",
            condition_id=f"condition-a-{relation_id}",
            yes_token_id=f"yes-a-{relation_id}",
            no_token_id=f"no-a-{relation_id}",
        )
        market_b = replace(
            base.market_b,
            market_id=f"market-b-{relation_id}",
            condition_id=f"condition-b-{relation_id}",
            yes_token_id=f"yes-b-{relation_id}",
            no_token_id=f"no-b-{relation_id}",
        )
        return replace(
            base,
            relation_id=relation_id,
            market_a=market_a,
            market_b=market_b,
            buy_leg_a=replace(
                base.buy_leg_a,
                market_id=market_a.market_id,
                condition_id=market_a.condition_id,
                token_id=market_a.yes_token_id,
            ),
            buy_leg_b=replace(
                base.buy_leg_b,
                market_id=market_b.market_id,
                condition_id=market_b.condition_id,
                token_id=market_b.no_token_id,
            ),
        )

    relations = [variant(f"target-{index:03d}") for index in range(101)]
    for relation in relations:
        FakePublicClient.books[relation.buy_leg_a.token_id] = threshold_book(
            relation.buy_leg_a.token_id,
            ask="0.40",
            bid="0.39",
        )
        FakePublicClient.books[relation.buy_leg_b.token_id] = threshold_book(
            relation.buy_leg_b.token_id,
            ask="0.45",
            bid="0.44",
        )

    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._catalog_loaded = True
    monitor._catalog_status = "healthy"
    monitor._catalog_full_scanned_at = NOW
    monitor._set_relation_state([relations[0]], ())
    client = FakePublicClient()
    asyncio.run(monitor._refresh_relation_activity(client))
    monitor._opportunities[relations[0].relation_id] = {
        "opportunity_id": relations[0].relation_id,
        "market_type": "threshold_hedge",
        "relation_id": relations[0].relation_id,
        "confirmed_at": NOW,
        "actionable": True,
        "eligibility_reason": "actionable",
    }
    previous_realtime_ids = set(monitor._realtime_relation_ids)
    previous_token_map = {
        token: set(relation_ids)
        for token, relation_ids in monitor._relation_by_token.items()
    }
    previous_handle = monitor._stream_handle
    monitor._set_relation_state(relations, ())

    asyncio.run(monitor._refresh_relation_activity(client))

    activity = monitor.snapshot()["relation_discovery"]["activity"]
    assert activity["status"] == "degraded"
    assert activity["apr_target_relations"] == 101
    assert monitor._relations_failed is True
    assert monitor._realtime_relation_ids == previous_realtime_ids
    assert monitor._relation_by_token == previous_token_map
    assert monitor._stream_handle is previous_handle
    assert monitor.snapshot()["opportunities"][0]["actionable"] is False
    assert monitor.opportunity(relations[0].relation_id)["actionable"] is False  # type: ignore[index]

    monitor._set_relation_state(relations[:100], ())
    asyncio.run(monitor._refresh_relation_activity(client))

    assert monitor.snapshot()["relation_discovery"]["activity"]["status"] == "healthy"
    assert monitor._relations_failed is False
    assert len(monitor._realtime_relation_ids) == 100


def test_codex_worker_does_not_block_activity_or_price_refresh(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.50", high_no_ask="0.51")
    validator = FakeRelationValidator()
    validator.block = threading.Event()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_relation_activity(client))
    relation = next(iter(monitor._relations.values()))

    async def exercise() -> None:
        await monitor._poll_relation_validation(client)
        assert monitor._codex_task is not None
        await asyncio.sleep(0.01)
        await asyncio.gather(
            monitor._refresh_relation_activity(client),
            monitor._process_stream_event(
                client,
                ns(
                    type="price_change",
                    payload=ns(asset_id=relation.buy_leg_a.token_id, price_changes=()),
                ),
            ),
        )
        validator.block.set()
        await monitor._codex_task
        await monitor._poll_relation_validation(client)

    asyncio.run(exercise())
    assert validator.calls == 1
    assert FakePublicClient.book_calls


def test_threshold_discovery_full_scan_and_only_calls_codex_for_positive_relation(
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
    assert len(FakePublicClient.list_events_calls) == 1
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    assert FakePublicClient.list_events_calls[1] == {
        "closed": False,
        "ended": False,
        "page_size": 100,
    }
    assert PagePaginator.first_page_calls == 1
    assert PagePaginator.iter_calls == 1
    monitor.refresh_once()
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
    assert row["minimum_profit"] == row["estimated_profit"]
    assert row["resolution_at"] == "2026-12-31T17:00:00Z"
    assert row["remaining_days"] == Decimal(
        "157.2083333333333333333333333"
    )
    assert validator.calls == 1
    subscribed = FakePublicClient.subscribe_specs[-1]
    assert {"yes-low", "no-high"} <= set(subscribed.token_ids)
    assert row["confirmed_age_seconds"] == 0
    assert monitor.snapshot()["relation_discovery"]["status"] == "healthy"


def test_nonpositive_threshold_economics_enters_codex_but_stays_invisible(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.52", high_no_ask="0.50")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    monitor.refresh_once()
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    assert validator.calls == 1
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
        relation_discovery=discover_threshold_relation_catalog,
        relation_validator=FakeRelationValidator(),
    )
    monitor.refresh_once()
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
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

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now",
        lambda: NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )
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

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    row = next(
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    )
    assert row["estimated_profit"] == Decimal("0.0020010000000000")
    assert row["actionable"] is False
    assert row["eligibility_reason"] == "annualized_yield_below_minimum"
    distribution = monitor.snapshot()["relation_discovery"]["annualized_distribution"]
    assert distribution["current"]["count"] == 1
    assert distribution["7d"]["count"] == 1
    assert distribution["30d"]["count"] == 1


def test_threshold_annualized_gate_requires_shared_end_and_keeps_low_yield_visible(
    tmp_path: Path,
) -> None:
    source = threshold_event()
    setup_public([source])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    calls: list[tuple[str, str]] = []

    def observer(opportunity_id: str, signal_id: str) -> dict[str, object]:
        calls.append((opportunity_id, signal_id))
        return {"state": "sent"}

    monitor.set_ready_observer(observer)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor._catalog_loaded = True
    relation_id, relation = next(iter(monitor._relations.items()))
    monitor._relations[relation_id] = replace(
        relation,
        market_b=replace(relation.market_b, end_date="2027-09-01T00:00:00Z"),
    )
    monitor._relation_rule_verifications[relation_id] = (
        NOW,
        _relation_fingerprint(monitor._relations[relation_id]),
    )
    monitor.refresh_once()

    row = next(
        item
        for item in monitor.snapshot()["opportunities"]
        if item.get("market_type") == "threshold_hedge"
    )
    assert row["resolution_at"] is None
    assert row["annualized_yield"] is None
    assert row["actionable"] is False
    assert row["eligibility_reason"] == "annualized_yield_unavailable"
    history = monitor._store.signal_history("all")
    assert history[0]["annualized_yield"] == row["annualized_yield"]
    assert calls == []


@pytest.mark.parametrize("end_date", ["not-a-date", Decimal("1e20")])
def test_threshold_annualized_gate_fails_closed_when_end_date_is_invalid(
    tmp_path: Path, end_date: object,
) -> None:
    source = threshold_event()
    setup_public([source])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor._catalog_loaded = True
    relation_id, relation = next(iter(monitor._relations.items()))
    monitor._relations[relation_id] = replace(
        relation,
        market_a=replace(relation.market_a, end_date=end_date),
    )
    monitor._relation_rule_verifications[relation_id] = (
        NOW,
        _relation_fingerprint(monitor._relations[relation_id]),
    )
    monitor.refresh_once()

    row = next(
        item
        for item in monitor.snapshot()["opportunities"]
        if item.get("market_type") == "threshold_hedge"
    )
    assert row["annualized_yield"] is None
    assert row["actionable"] is False
    assert row["eligibility_reason"] == "annualized_yield_unavailable"
    distributions = monitor.snapshot()["relation_discovery"]["annualized_distribution"]
    assert distributions["current"]["count"] == 0
    assert distributions["7d"]["count"] == 0
    assert distributions["30d"]["count"] == 0


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

    assert len(monitor.snapshot()["relation_discovery"]["scan_logs"]) <= 20
    assert make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    ).snapshot()["relation_discovery"]["scan_logs"] == []


def test_only_execution_eligible_active_binary_markets_are_subscribed(
    tmp_path: Path,
) -> None:
    good = market("good", yes="yes-good", no="no-good")
    fee = market("fee", yes="yes-fee", no="no-fee", fees_enabled=True)
    unknown = market(
        "unknown", yes="yes-unknown", no="no-unknown", fees_enabled=None
    )
    neg = market("neg", yes="yes-neg", no="no-neg", neg_risk=True)
    malformed = ns(id="malformed", state=ns(active=True, closed=False), outcomes=[ns(label="YES", token_id="yes")])
    setup_public([event("e", markets=(good, fee, unknown, neg, malformed))])

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    snapshot = monitor.snapshot()

    assert FakePublicClient.book_calls == [
        ["yes-good", "no-good"],
        ["yes-fee", "no-fee"],
        ["yes-unknown", "no-unknown"],
        ["yes-neg", "no-neg"],
    ]
    assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == (
        "no-good",
        "yes-good",
    )
    markets = {row["market_id"]: row for row in snapshot["events"][0]["markets"]}
    assert set(markets) == {"good", "fee", "unknown", "neg"}
    assert markets["fee"]["eligibility_reason"] == "fee_unverified_or_enabled"
    assert markets["unknown"]["eligibility_reason"] == "fee_unverified_or_enabled"
    assert markets["neg"]["eligibility_reason"] == "neg_risk"
    assert snapshot["diagnostics"]["malformed_markets"] == 1


def test_execution_eligible_market_stays_subscribed_without_threshold_candidate(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m"),))])
    FakePublicClient.books["yes-m"].asks = [
        ns(price=Decimal("0.50"), size=Decimal("20"))
    ]
    FakePublicClient.books["no-m"].asks = [
        ns(price=Decimal("0.50"), size=Decimal("20"))
    ]
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor.snapshot()["events"][0]["markets"][0]["eligibility_reason"] == "no_threshold_candidate"
    assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == ("no-m", "yes-m")


def test_execution_eligible_market_stays_subscribed_when_book_read_fails(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m"),))])
    FakePublicClient.fail_get_order_books = True
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == ("no-m", "yes-m")


def test_execution_eligible_websocket_tick_still_confirms_paired_books(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    FakePublicClient.book_calls.clear()
    asyncio.run(monitor._process_stream_event(
        FakePublicClient(),
        ns(type="price_change", payload=ns(asset_id="yes-1", price_changes=())),
    ))
    assert FakePublicClient.book_calls == [["yes-1", "no-1"]]


def test_ambiguous_empty_standard_pool_preserves_prior_subscription(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-old", no="no-old"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    previous_tokens = dict(monitor._market_by_token)
    setup_public([event("e", markets=(market(
        "m", yes="yes-unknown", no="no-unknown", fees_enabled=None
    ),))])
    monitor.refresh_once()
    snapshot = monitor.snapshot()
    assert monitor._market_by_token == previous_tokens
    assert snapshot["health"]["status"] == "degraded"
    assert "universe_refresh_failed" in snapshot["health"]["degraded_reasons"]


def test_malformed_only_replacement_preserves_prior_subscription(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-old", no="no-old"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    previous_tokens = dict(monitor._market_by_token)
    malformed = ns(
        id="malformed",
        state=ns(active=True, closed=False, ended=False),
        outcomes=[ns(label="YES", token_id="yes")],
    )
    setup_public([event("e", markets=(malformed,))])
    monitor.refresh_once()
    snapshot = monitor.snapshot()
    assert monitor._market_by_token == previous_tokens
    assert snapshot["health"]["status"] == "degraded"
    assert "universe_refresh_failed" in snapshot["health"]["degraded_reasons"]


def test_explicitly_ineligible_universe_accepts_empty_standard_pool(tmp_path: Path) -> None:
    setup_public([event("e", markets=(
        market("fee", fees_enabled=True),
        market("neg", yes="yes-neg", no="no-neg", neg_risk=True),
    ))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor._market_by_token == {}
    assert FakePublicClient.subscribe_specs == []
    assert monitor.snapshot()["health"]["status"] == "healthy"


def test_unchanged_universe_token_union_does_not_reconnect(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path, relation_discovery=None)
    client = FakePublicClient()
    asyncio.run(monitor._refresh_universe(client))
    first_handle = monitor._stream_handle
    asyncio.run(monitor._refresh_universe(client))
    assert len(FakePublicClient.subscribe_specs) == 1
    assert monitor._stream_handle is first_handle


def test_large_token_universe_is_subscribed_in_websocket_safe_chunks(
    tmp_path: Path,
) -> None:
    markets = tuple(
        market(
            f"market-{index:03d}",
            yes=f"yes-{index:03d}",
            no=f"no-{index:03d}",
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


def test_subscription_keeps_dirty_when_tokens_change_during_connect(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(tmp_path)
    monitor._market_by_token = {"top20-token": "market"}
    entered = asyncio.Event()
    release = asyncio.Event()
    subscribed: list[object] = []

    class DelayedClient:
        async def subscribe(self, spec: object) -> FakeStream:
            subscribed.append(spec)
            entered.set()
            await release.wait()
            return FakeStream()

    async def exercise() -> None:
        task = asyncio.create_task(monitor._subscribe(DelayedClient()))
        await entered.wait()
        monitor._relation_by_token = {"new-relation-token": {"relation"}}
        monitor._subscription_dirty = True
        release.set()
        await task

    asyncio.run(exercise())

    assert tuple(subscribed[0].token_ids) == ("top20-token",)
    assert monitor._subscription_dirty is True


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
    assert opportunity["tick_size"] == Decimal("0.01")
    assert opportunity["minimum_profit"] == opportunity["estimated_profit"]
    assert opportunity["confirmed_age_seconds"] <= 10
    assert trading.submit_calls == trading.remediation_calls == trading.merge_calls == 0

    trading.checked_at = NOW - timedelta(seconds=61)
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is None
    assert monitor.snapshot()["health"]["actionable"] is False


def test_standard_binary_opportunity_carries_market_end_date_as_resolution_at(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", end_date="2026-12-31T17:00:00Z"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    opportunity = monitor.opportunity("e:m")
    assert opportunity is not None
    assert opportunity["resolution_at"] == datetime(2026, 12, 31, 17, 0, tzinfo=UTC)


def test_successful_paired_book_read_uses_local_receipt_freshness(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    FakePublicClient.books["yes-1"].timestamp = NOW - timedelta(minutes=1)
    FakePublicClient.books["no-1"].timestamp = NOW - timedelta(minutes=1)

    monitor = make_monitor(tmp_path)
    monitor.refresh_once()

    opportunity = monitor.opportunity("e:m")
    assert opportunity is not None
    assert opportunity["actionable"] is True
    assert opportunity["confirmed_at"] == NOW
    assert opportunity["book_timestamp_a"] == NOW - timedelta(minutes=1)


def test_targeted_standard_refresh_rechecks_live_market_metadata(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor.opportunity("e:m") is not None
    monitor._subscription_dirty = False
    FakePublicClient.get_event_calls.clear()
    FakePublicClient.events = [
        event("e", markets=(market("m", fees_enabled=True),))
    ]

    refreshed = monitor.refresh_opportunity("e:m")

    assert refreshed is None
    assert FakePublicClient.get_event_calls == ["e"]
    assert monitor.opportunity("e:m") is None
    assert monitor._market_by_token == {}
    assert monitor._subscription_dirty is True


def test_targeted_metadata_refresh_promotes_newly_eligible_market(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", fees_enabled=True),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    current = dict(monitor._markets["m"])
    monitor._subscription_dirty = False
    FakePublicClient.events = [event("e", markets=(market("m"),))]

    asyncio.run(monitor._refresh_standard_market_metadata(FakePublicClient(), current))

    assert set(monitor._market_by_token) == {"yes-1", "no-1"}
    assert monitor._subscription_dirty is True


def test_targeted_demotion_does_not_reconnect_when_other_layer_owns_tokens(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    current = dict(monitor._markets["m"])
    monitor._relation_by_token = {
        "yes-1": {"relation"},
        "no-1": {"relation"},
    }
    monitor._subscription_dirty = False
    FakePublicClient.events = [event("e", markets=(market("m", fees_enabled=True),))]

    asyncio.run(monitor._refresh_standard_market_metadata(FakePublicClient(), current))

    assert monitor._market_by_token == {}
    assert monitor._subscription_dirty is False


def test_stale_snapshot_preserves_stronger_threshold_blocker(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    monitor._catalog_loaded = True
    monitor._catalog_status = "healthy"
    monitor._catalog_full_scanned_at = NOW
    monitor._opportunities["blocked"] = {
        "opportunity_id": "blocked",
        "market_type": "threshold_hedge",
        "relation_id": "blocked",
        "confirmed_at": NOW - timedelta(seconds=11),
        "actionable": False,
        "eligibility_reason": "remediation_unsafe",
    }

    row = monitor.snapshot()["opportunities"][0]

    assert row["actionable"] is False
    assert row["eligibility_reason"] == "remediation_unsafe"


def test_threshold_opportunity_is_not_blocked_by_standard_universe_failure(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.40", high_no_ask="0.48")
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )
    client = FakePublicClient()
    asyncio.run(monitor._run_full_relation_scan(client))
    asyncio.run(monitor._refresh_readiness())
    asyncio.run(monitor._refresh_relation_activity(client))
    asyncio.run(monitor._drain_relation_validation(client))
    relation_id = next(iter(monitor._active_relation_ids))
    monitor._universe_at = NOW
    monitor._universe_failed = True

    opportunity = monitor.opportunity(relation_id)

    assert opportunity is not None
    assert opportunity["actionable"] is True


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

    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m", fees_enabled=True),))])
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

    setup_public([event("e", markets=(market("m", volume="100", fees_enabled=True),))])
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


def test_connected_quiet_stream_does_not_degrade_monitor_health(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m", fees_enabled=True),))])
    now = [NOW]
    monitor = make_monitor(tmp_path)
    monitor._clock = lambda: now[0]
    monitor.refresh_once()
    now[0] = NOW + timedelta(seconds=16)
    assert monitor.snapshot()["health"]["status"] == "healthy"


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


def test_title_translation_worker_is_fifo_and_does_not_block_english_snapshot(
    tmp_path: Path,
) -> None:
    class BlockingTranslator:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls: list[str] = []
            self.active = 0
            self.max_active = 0

        def translate(self, title: str) -> str | None:
            self.calls.append(title)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
            self.release.wait(timeout=2)
            self.active -= 1
            return f"中文 {title}"

    setup_public([
        event("e-2", markets=(market("m-2"),)),
        event("e-1", markets=(market("m-1"),)),
        threshold_event(),
    ])
    setup_threshold_books()
    translator = BlockingTranslator()

    async def scenario() -> None:
        monitor = make_monitor(
            tmp_path,
            relation_discovery=discover_threshold_relations,
            relation_validator=FakeRelationValidator(),
            title_translator=translator,
        )
        task = asyncio.create_task(monitor.run_forever())
        assert await asyncio.to_thread(translator.started.wait, 1.0)
        english = monitor.snapshot()
        assert [row["title"] for row in english["events"]] == [
            "Event e-1", "Event e-2", "Event threshold-event"
        ]
        assert all("title_zh" not in row for row in english["events"])
        translator.release.set()
        pair = (
            "Will Bitcoin be above $90,000 on December 31? / "
            "Will Bitcoin be above $100,000 on December 31?"
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if pair in translator.calls and all(
                "title_zh" in row for row in monitor.snapshot()["events"]
            ):
                break
        translated = monitor.snapshot()
        assert all("title_zh" in row for row in translated["events"])
        assert translator.max_active == 1
        assert pair in translator.calls
        assert translator.calls.index(pair) > translator.calls.index("Event threshold-event")
        monitor.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert monitor._title_translation_task is None

    asyncio.run(scenario())


def test_universe_retry_attempts_schedule_and_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = make_monitor(tmp_path, relation_discovery=None, relation_validator=None)
    refresh_calls = 0

    class TransportError(RuntimeError):
        pass

    async def fail_refresh(_client: object) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        raise TransportError("temporary failure")

    monitor._refresh_universe_bounded = fail_refresh  # type: ignore[method-assign]
    completion_times = iter((0.0, 5.0, 10.0, 15.0, 20.0))
    last_completion = [0.0]

    def monotonic() -> float:
        try:
            last_completion[0] = next(completion_times)
        except StopIteration:
            pass
        return last_completion[0]

    monkeypatch.setattr(monitor, "_monotonic", monotonic)

    async def scenario() -> tuple[list[float], dict[str, object]]:
        next_refresh = 0.0
        due_times: list[float] = []
        for current in (0.0, 5.0, 10.0, 15.0, 20.0, 25.0):
            next_refresh, _succeeded = await monitor._refresh_universe_if_due(
                object(), current=current, next_refresh=next_refresh
            )
            due_times.append(next_refresh)
        return due_times, monitor.snapshot()["health"]

    due_times, health = asyncio.run(scenario())

    assert refresh_calls == 5
    assert due_times[:4] == [5.0, 10.0, 15.0, 20.0]
    assert health["universe_refresh_attempts"] == 5
    assert health["universe_retry_exhausted"] is True
    assert "universe_retry_exhausted" in health["degraded_reasons"]
    assert "universe_refresh_failed" not in health["degraded_reasons"]


def test_universe_retry_success_resets_attempts_and_restores_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = make_monitor(tmp_path, relation_discovery=None, relation_validator=None)
    calls = 0

    async def refresh(_client: object) -> None:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ConnectionError("temporary failure")
        monitor._universe_at = NOW
        monitor._universe_failed = False

    monitor._refresh_universe_bounded = refresh  # type: ignore[method-assign]
    completion_times = iter((0.0, 5.0, 10.0, 15.0))
    last_completion = [0.0]

    def monotonic() -> float:
        try:
            last_completion[0] = next(completion_times)
        except StopIteration:
            pass
        return last_completion[0]

    monkeypatch.setattr(monitor, "_monotonic", monotonic)

    async def scenario() -> tuple[float, bool, dict[str, object]]:
        next_refresh = 0.0
        for current in (0.0, 5.0, 10.0):
            next_refresh, _succeeded = await monitor._refresh_universe_if_due(
                object(), current=current, next_refresh=next_refresh
            )
        next_refresh, succeeded = await monitor._refresh_universe_if_due(
            object(), current=15.0, next_refresh=next_refresh
        )
        return next_refresh, succeeded, monitor.snapshot()["health"]

    next_refresh, succeeded, health = asyncio.run(scenario())

    assert calls == 4
    from open_trader.polymarket_monitor import UNIVERSE_REFRESH_SECONDS

    assert next_refresh == 15.0 + UNIVERSE_REFRESH_SECONDS
    assert succeeded is True
    assert health["universe_refresh_attempts"] == 0
    assert health["universe_retry_exhausted"] is False
    assert "universe_refresh_failed" not in health["degraded_reasons"]
    assert "universe_retry_exhausted" not in health["degraded_reasons"]


def test_universe_failure_observer_is_scheduled_once_on_attempt_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = make_monitor(tmp_path, relation_discovery=None, relation_validator=None)
    calls: list[dict[str, object]] = []

    class TransportError(RuntimeError):
        pass

    async def fail_refresh(_client: object) -> None:
        raise TransportError("temporary failure")

    monitor._refresh_universe_bounded = fail_refresh  # type: ignore[method-assign]
    monitor.set_failure_observer(
        lambda payload: calls.append(dict(payload)) or {"state": "sent"}
    )
    completion_times = iter((0.0, 5.0, 10.0, 15.0, 20.0))
    monkeypatch.setattr(monitor, "_monotonic", lambda: next(completion_times))

    async def scenario() -> None:
        next_refresh = 0.0
        for current in (0.0, 5.0, 10.0, 15.0, 20.0, 25.0):
            next_refresh, _succeeded = await monitor._refresh_universe_if_due(
                object(), current=current, next_refresh=next_refresh
            )
        await asyncio.sleep(0.01)
        monitor._reap_universe_failure_notification_task()

    asyncio.run(scenario())

    assert calls == [
        {
            "attempts": 5,
            "error_type": "TransportError",
            "last_success_at": None,
        }
    ]
    assert monitor._universe_failure_notification_task is None


def test_threshold_row_exposes_theoretical_and_policy_depth(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    monitor.refresh_once()
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    row = next(
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    )
    assert row["depth_status"] == "pass"
    assert row["max_executable_quantity"] >= row["policy_quantity"]
    assert row["max_executable_cost"] >= row["policy_cost"]
    assert row["policy_quantity"] == row["quantity"]
    assert row["policy_cost"] == row["total_max_cost"]
