"""Offline metadata lifetime checks at the SDK and preparation boundaries."""
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
import gc
import socket
import sqlite3
import threading
import weakref

import httpx
import pytest
from polymarket.models.gamma.market import Market

from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import _LPCandidateQueryExchange
from test_polymarket_trading import (
    SIGNER, WALLET, _lp_cache_clock, _lp_market_page_response,
    _lp_market_payload, _lp_mock_public_client,
)


NOW = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("metadata fixtures must not connect to a socket")

    monkeypatch.setattr(socket.socket, "connect", denied)
    yield
    assert not attempts


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("count", [1, 101])
def test_sdk_markets_release_before_numeric_and_direct_event_reads(monkeypatch, count, closed):
    conditions = tuple(f"0x{index:064x}" for index in range(count))
    references = []
    event_entry_counts = []
    requests = []
    lock = threading.Lock()
    tail_started = threading.Event()
    parse = Market.parse_response

    def observed_parse(cls, payload):
        market = parse(payload)
        with lock:
            references.append(weakref.ref(market))
        return market

    monkeypatch.setattr(Market, "parse_response", classmethod(observed_parse))
    _lp_cache_clock(monkeypatch, NOW)

    def handler(request):
        with lock:
            requests.append((request.url.path, tuple(request.url.params.multi_items())))
        if request.url.path == "/markets/keyset":
            if closed and request.url.params.get("closed") != "true":
                return _lp_market_page_response(request, ())
            ids = request.url.params.get_list("condition_ids")
            if count > 50:
                if conditions[-1] in ids:
                    tail_started.set()
                elif conditions[0] in ids:
                    assert tail_started.wait(5), "Independent slow-first-batch watchdog"
            return _lp_market_page_response(request, (
                dict(_lp_market_payload(condition),
                     events=[{"id": "direct-event" if condition == conditions[-1]
                              else str(int(condition, 16) + 1)}],
                     acceptingOrders=True, closed=False, feesEnabled=True,
                     orderMinSize="20", orderPriceMinTickSize="0.01",
                     feeSchedule={"exponent": 2, "rate": "0.02", "takerOnly": True,
                                  "rebateRate": "0.001"},
                     rewardsMinSize="25", rewardsMaxSpread=3.5,
                     gameId="game-1", gameStartTime="2026-10-06T01:00:00Z",
                     oneDayPriceChange="-0.12", description="Settlement rules.")
                for condition in ids
            ))
        gc.collect()
        with lock:
            event_entry_counts.append(sum(ref() is not None for ref in references))
        if request.url.path == "/events/keyset":
            ids = request.url.params.get_list("id")
        else:
            assert request.url.path == "/events/direct-event"
            ids = ["direct-event"]
        events = [{"id": event_id, "slug": "parent-event", "ended": True,
                   "startTime": "2026-10-06T01:00:00Z",
                   "finishedTimestamp": "2026-10-06T02:00:00Z", "markets": []}
                  for event_id in ids]
        return httpx.Response(200, json={"events": events} if len(ids) != 1
                              or request.url.path == "/events/keyset" else events[0],
                              request=request)

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(),
        public_client_factory=lambda: _lp_mock_public_client(handler),
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions)
        assert result["state"] == "known", result
        assert result["failed_ids"] == {}
        assert result["confirmed_absent_ids"] == result["deferred_ids"] == ()
        assert tuple(result["markets"]) == conditions
        for index, condition in enumerate(conditions):
            assert result["markets"][condition] == {
                "market_id": f"market-{condition}", "condition_id": condition,
                "metadata_checked_at": NOW, "fees_checked_at": NOW,
                "market_title": "Will this market resolve Yes?",
                "market_url": f"https://polymarket.com/event/parent-event/market-{condition[-8:]}",
                "event_id": "direct-event" if condition == conditions[-1] else str(index + 1),
                "game_id": "game-1", "game_start_time": NOW.replace(hour=1),
                "event_start_time": NOW.replace(hour=1), "event_ended": True,
                "event_finished_at": NOW.replace(hour=2),
                "price_change_24h": Decimal("-0.12"),
                "price_change_24h_source": "polymarket.prices.one_day_price_change",
                "accepting_orders": True, "closed": False, "resolved": None,
                "exchange_type": "CLOB", "tick_size": Decimal("0.01"),
                "minimum_order_size": Decimal("20"), "fee": Decimal("0"),
                "fees_enabled": True, "fee_exponent": Decimal("2"),
                "taker_fee_rate": Decimal("0.02"), "reward_min_size": Decimal("25"),
                "reward_max_spread": Decimal("0.035"),
                "outcomes": {"yes": {"label": "Yes", "token_id": "yes-token"},
                             "no": {"label": "No", "token_id": "no-token"}},
            }
        assert len(references) == count
        assert event_entry_counts and set(event_entry_counts) == {0}, event_entry_counts
        assert all(ref() is None for ref in references)
        market_queries = [dict(params) for path, params in requests if path == "/markets/keyset"]
        assert len(market_queries) == ((count + 49) // 50) * (2 if closed else 1)
        assert all(query["limit"] == "100" for query in market_queries)
    finally:
        adapter.close()


class WeakDict(dict):
    """Observe original exchange payloads, without retaining them in the test."""


@pytest.mark.parametrize("count, unknown_last", [(1, False), (1501, False), (1501, True)])
def test_preparation_releases_metadata_payloads_before_next_batch_and_history(tmp_path, count, unknown_last):
    references = []
    boundary_counts = []
    batch_sizes = []
    history_tokens = set()
    lock = threading.Lock()

    def observe_boundary():
        gc.collect()
        with lock:
            boundary_counts.append(sum(ref() is not None for ref in references))

    def tracked(value):
        payload = WeakDict(value)
        references.append(weakref.ref(payload))
        return payload

    class Exchange(_LPCandidateQueryExchange):
        def lp_market_metadata_batch(self, conditions, *, stop_event=None):
            observe_boundary()
            batch_sizes.append(len(conditions))
            if unknown_last and len(batch_sizes) == 2:
                return tracked({"state": "unknown", "markets": None})
            markets = super().lp_market_metadata(conditions, stop_event=stop_event)
            return tracked({
                "state": "known",
                "markets": tracked({key: tracked(row) for key, row in markets.items()}),
                "failed_ids": {}, "confirmed_absent_ids": (), "deferred_ids": (),
            })

        def lp_price_history(self, tokens, **kwargs):
            observe_boundary()
            with lock:
                history_tokens.update(tokens)
            return super().lp_price_history(tokens, **kwargs)

    exchange = Exchange(NOW, {f"M{i:04}": Decimal("100") for i in range(count)})
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW,
    )
    result = service.refresh_price_history()
    assert result["state"] == ("partial" if unknown_last else "known"), result
    assert batch_sizes == ([1] if count == 1 else [1500, 1])
    assert len(history_tokens) == count - int(unknown_last)
    assert set(boundary_counts) == {0}, boundary_counts
    assert all(ref() is None for ref in references)


def test_paginated_failure_releases_sdk_rows_and_preserves_other_batch(monkeypatch):
    conditions = tuple(f"0x{index:064x}" for index in range(51))
    references = []
    event_counts = []
    calls = []
    reject = True
    parse = Market.parse_response

    def observed_parse(cls, payload):
        market = parse(payload)
        references.append(weakref.ref(market))
        return market

    monkeypatch.setattr(Market, "parse_response", classmethod(observed_parse))

    def handler(request):
        if request.url.path == "/events/keyset":
            gc.collect()
            event_counts.append(sum(ref() is not None for ref in references))
            return httpx.Response(200, json={"events": [{"id": "42", "markets": []}]}, request=request)
        assert request.url.path == "/markets/keyset"
        ids = tuple(request.url.params.get_list("condition_ids"))
        cursor = request.url.params.get("after_cursor")
        calls.append((ids, cursor))
        if reject and cursor:
            return httpx.Response(400, json={"error": "offline page rejected"}, request=request)
        rows = ids[:-1] if reject and len(ids) == 50 else ids
        return _lp_market_page_response(request, (
            dict(_lp_market_payload(condition), events=[{"id": "42"}])
            for condition in rows
        ), next_cursor="second" if reject and len(ids) == 50 else None)

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(),
        public_client_factory=lambda: _lp_mock_public_client(handler),
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions)
        assert result["state"] == "partial"
        assert tuple(result["markets"]) == conditions[-1:]
        assert set(result["failed_ids"]) == set(conditions[:50])
        assert set(result["failed_ids"].values()) == {"market_read_RequestRejectedError"}
        assert result["confirmed_absent_ids"] == result["deferred_ids"] == ()
        assert set(event_counts) == {0}, event_counts
        assert all(ref() is None for ref in references)
        reject = False
        calls.clear()
        recovered = adapter.lp_market_metadata_batch(conditions)
        assert recovered["state"] == "known"
        assert tuple(recovered["markets"]) == conditions
        assert recovered["failed_ids"] == {}
        assert calls == [(conditions[:50], None)]
    finally:
        adapter.close()


def test_market_page_cancellation_does_not_prove_absence_or_read_events():
    conditions = ("0x" + "a" * 64, "0x" + "b" * 64)
    stop = threading.Event()
    calls = []

    def handler(request):
        assert request.url.path == "/markets/keyset"
        cursor = request.url.params.get("after_cursor")
        calls.append(cursor)
        if cursor:
            stop.set()
            return _lp_market_page_response(request, ())
        return _lp_market_page_response(request, (
            dict(_lp_market_payload(conditions[0]), events=[{"id": "42"}]),
        ), next_cursor="second")

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(),
        public_client_factory=lambda: _lp_mock_public_client(handler),
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions, stop_event=stop)
        assert calls == [None, "second"]
        assert result["state"] == "cancelled"
        assert tuple(result["markets"]) == conditions[:1]
        assert result["failed_ids"] == {conditions[0]: "event_read_cancelled"}
        assert result["deferred_ids"] == conditions[1:]
        assert result["confirmed_absent_ids"] == ()
    finally:
        adapter.close()


@pytest.mark.parametrize("event_shape", ["mapping", "sdk"])
def test_canonical_mapping_releases_unused_nested_trees_before_event(monkeypatch, event_shape):
    from polymarket.models.gamma.event import Event

    condition = "0x" + "a" * 64
    references = []
    entry_counts = []
    _lp_cache_clock(monkeypatch, NOW)

    def child():
        value = Market.parse_response(_lp_market_payload("0x" + "b" * 64))
        references.append(weakref.ref(value))
        return value

    class Public:
        def list_markets(self, **kwargs):
            if event_shape == "sdk":
                event = Event.parse_response({"id": "42", "slug": "reference",
                                             "markets": [_lp_market_payload("0x" + "b" * 64)]})
                references.extend((weakref.ref(event), weakref.ref(event.markets[0])))
            else:
                event = {"id": "42", "slug": "reference", "markets": [child()]}
            return ({
                "market_id": "canonical-market", "conditionId": condition,
                "title": "Canonical title", "slug": "canonical-slug",
                "state": {"accepting_orders": True, "closed": False, "resolved": False,
                          "unused": child()},
                "trading": {"minimum_tick_size": "0.01", "minimum_order_size": "20",
                            "fees_enabled": True, "unused": child(),
                            "fee_schedule": {"rate": "0.02", "exponent": 2,
                                             "taker_only": True, "unused": child()}},
                "rewards": {"rewards_min_size": "25", "rewards_max_spread": "3.5",
                            "clob_rewards": [child()]},
                "sports": {"game_id": "game-1", "game_start_time": NOW, "unused": child()},
                "prices": {"one_day_price_change": "-0.12", "unused": child()},
                "outcomes": {"YES": {"label": "Yes", "tokenId": "yes-token", "unused": child()},
                             "NO": {"label": "No", "token_id": "no-token", "unused": child()}},
                "events": [event],
            },)

        def list_events(self, **kwargs):
            gc.collect()
            entry_counts.append(sum(ref() is not None for ref in references))
            return ({"id": "42", "slug": "parent", "state": {"ended": True},
                     "schedule": {"start_time": NOW, "finished_at": NOW}},)

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(), public_client_factory=Public,
    )
    try:
        result = adapter.lp_market_metadata_batch((condition,))
        assert result["state"] == "known", result
        assert result["failed_ids"] == {}
        assert result["deferred_ids"] == result["confirmed_absent_ids"] == ()
        assert result["markets"] == {condition: {
            "market_id": "canonical-market", "condition_id": condition,
            "metadata_checked_at": NOW, "fees_checked_at": NOW,
            "market_title": "Canonical title", "market_url": "https://polymarket.com/event/parent/canonical-slug",
            "event_id": "42", "game_id": "game-1", "game_start_time": NOW,
            "event_start_time": NOW, "event_ended": True, "event_finished_at": NOW,
            "price_change_24h": Decimal("-0.12"),
            "price_change_24h_source": "polymarket.prices.one_day_price_change",
            "accepting_orders": True, "closed": False, "resolved": False,
            "exchange_type": "CLOB", "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal("20"), "fee": Decimal("0"), "fees_enabled": True,
            "fee_exponent": Decimal("2"), "taker_fee_rate": Decimal("0.02"),
            "reward_min_size": Decimal("25"), "reward_max_spread": Decimal("0.035"),
            "outcomes": {"yes": {"label": "Yes", "token_id": "yes-token"},
                         "no": {"label": "No", "token_id": "no-token"}},
        }}
        assert entry_counts == [0], entry_counts
        assert all(ref() is None for ref in references)
    finally:
        adapter.close()


@pytest.mark.parametrize("event_case", ["none", "single", "invalid", "double", "empty_mapping",
                                       "one_shot", "iteration_failure"])
def test_mapping_projection_keeps_aliases_event_count_and_read_errors(monkeypatch, event_case):
    conditions = tuple(f"0x{index:064x}" for index in range(51))
    event_calls = []
    _lp_cache_clock(monkeypatch, NOW)

    class BrokenDump:
        def model_dump(self):
            raise ValueError("ignored nested model serialization failure")

    class FailedEvents:
        def iter_items(self):
            raise RuntimeError("event collection failure")

    class Public:
        def list_markets(self, *, condition_ids, **kwargs):
            rows = []
            for condition in condition_ids:
                events = [{"id": "42", "slug": "reference"}]
                if condition != conditions[0] or event_case == "none":
                    events = []
                elif event_case == "invalid":
                    events.extend((None, "invalid", BrokenDump()))
                elif event_case == "double":
                    events.append({"id": "43"})
                elif event_case == "empty_mapping":
                    events.append({})
                elif event_case == "one_shot":
                    events = iter(events)
                elif event_case == "iteration_failure":
                    events = FailedEvents()
                rows.append({
                    "condition_id": condition, "market_id": "alias-market", "title": "Alias title",
                    "url": "https://example.invalid/canonical", "state": BrokenDump(),
                    "trading": {"fees_enabled": True, "fee_schedule": BrokenDump()},
                    "rewards": BrokenDump(), "rewards_min_size": "25", "rewards_max_spread": "3.5",
                    "taker_fee_rate": "0.05", "sports": BrokenDump(), "prices": BrokenDump(),
                    "outcomes": {"YeS": {"tokenId": "yes-token"}, "no": BrokenDump()},
                    "events": events,
                })
            return rows

        def list_events(self, **kwargs):
            event_calls.append(tuple(kwargs["ids"]))
            return ({"id": "42", "slug": "parent", "state": {"ended": True}},)

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(), public_client_factory=Public,
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions)
        assert result["confirmed_absent_ids"] == result["deferred_ids"] == ()
        if event_case == "iteration_failure":
            assert result["state"] == "unknown"
            assert result["markets"] == {}
            # Keep the original whole-call failure boundary, including the sibling batch.
            assert result["failed_ids"] == dict.fromkeys(conditions, "market_read_RuntimeError")
            assert event_calls == []
            return
        assert result["state"] == "known", result
        assert result["failed_ids"] == {}
        assert tuple(result["markets"]) == conditions
        assert all(len(row) == 27 for row in result["markets"].values())
        linked = event_case in {"single", "invalid"}
        assert event_calls == ([(42,)] if linked or event_case == "one_shot" else [])
        assert result["markets"][conditions[0]] == {
            "market_id": "alias-market", "condition_id": conditions[0],
            "metadata_checked_at": NOW, "fees_checked_at": NOW,
            "market_title": "Alias title", "market_url": "https://example.invalid/canonical",
            "event_id": "42" if linked else None, "game_id": None, "game_start_time": None,
            "event_start_time": None, "event_ended": True if linked else None, "event_finished_at": None,
            "price_change_24h": None, "price_change_24h_source": None,
            "accepting_orders": None, "closed": None, "resolved": None, "exchange_type": "CLOB",
            "tick_size": None, "minimum_order_size": None, "fee": None, "fees_enabled": True,
            "fee_exponent": Decimal("1"), "taker_fee_rate": Decimal("0.05"),
            "reward_min_size": Decimal("25"), "reward_max_spread": Decimal("0.035"),
            "outcomes": {"yes": {"label": "YeS", "token_id": "yes-token"}},
        }
    finally:
        adapter.close()


_NESTED_ACCESS_FAULTS = (
    "outcomes-items", "state-access", "trading-access", "fee-access",
    "rewards-access", "sports-access", "prices-access", "outcome-access",
)


@pytest.mark.parametrize("location, error_kind", [
    (location, kind) for location in _NESTED_ACCESS_FAULTS for kind in ("runtime", "sqlite")
] + [("outcomes-items", "cancelled")])
def test_nested_mapping_errors_keep_whole_call_unknown(monkeypatch, location, error_kind):
    conditions = tuple(f"0x{index:064x}" for index in range(51))
    calls = []
    sibling_seen = threading.Event()
    stop = threading.Event()
    _lp_cache_clock(monkeypatch, NOW)

    def fail():
        assert sibling_seen.wait(5), "Independent sibling-read watchdog"
        if error_kind == "cancelled":
            stop.set()
        if error_kind == "sqlite":
            with closing(sqlite3.connect(":memory:")) as connection:
                try:
                    connection.execute("SELECT * FROM missing_fixture_table")
                except sqlite3.Error as exc:
                    raise RuntimeError("nested fixture failure") from exc
        raise RuntimeError("nested fixture failure")

    class FaultMapping(dict):
        def __init__(self, values, *, key=None, broken_items=False):
            super().__init__(values)
            self.key = key
            self.broken_items = broken_items

        def items(self):
            if self.broken_items:
                fail()
            return super().items()

        def get(self, key, default=None):
            if key == self.key:
                fail()
            return super().get(key, default)

        def __getitem__(self, key):
            if key == self.key:
                fail()
            return super().__getitem__(key)

    class Public:
        def list_markets(self, *, condition_ids, **kwargs):
            calls.append(tuple(condition_ids))
            if len(condition_ids) == 1:
                sibling_seen.set()
            rows = []
            for condition in condition_ids:
                row = {
                    "condition_id": condition, "state": {"accepting_orders": True},
                    "trading": {"minimum_order_size": "20", "fee_schedule": {"rate": "0.02"}},
                    "rewards": {"rewards_min_size": "20"}, "sports": {"game_id": "game"},
                    "prices": {"one_day_price_change": "0.1"},
                    "outcomes": {"yes": {"token_id": "yes-token"}}, "events": [],
                }
                if condition == conditions[0]:
                    if location == "outcomes-items":
                        row["outcomes"] = FaultMapping(row["outcomes"], broken_items=True)
                    elif location == "fee-access":
                        row["trading"]["fee_schedule"] = FaultMapping(row["trading"]["fee_schedule"], key="rate")
                    elif location == "outcome-access":
                        row["outcomes"]["yes"] = FaultMapping(row["outcomes"]["yes"], key="token_id")
                    else:
                        name, key = {
                            "state-access": ("state", "accepting_orders"),
                            "trading-access": ("trading", "minimum_order_size"),
                            "rewards-access": ("rewards", "rewards_min_size"),
                            "sports-access": ("sports", "game_id"),
                            "prices-access": ("prices", "one_day_price_change"),
                        }[location]
                        row[name] = FaultMapping(row[name], key=key)
                rows.append(row)
            return rows

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(), public_client_factory=Public,
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions, stop_event=stop)
        assert sorted(map(len, calls)) == [1, 50]
        assert result["state"] == ("cancelled" if error_kind == "cancelled" else "unknown"), result
        assert result["markets"] == {}
        assert result["confirmed_absent_ids"] == result["deferred_ids"] == ()
        assert result["failed_ids"] == dict.fromkeys(conditions, "market_read_RuntimeError")
        for facts in result["failure_facts"].values():
            assert tuple(facts["error_chain"]) == (
                ("RuntimeError", "OperationalError") if error_kind == "sqlite" else ("RuntimeError",)
            )
            if error_kind == "sqlite":
                assert facts["sqlite_errorcode"] == sqlite3.SQLITE_ERROR
                assert facts["sqlite_errorname"] == "SQLITE_ERROR"
            else:
                assert "sqlite_errorcode" not in facts
    finally:
        adapter.close()


@pytest.mark.parametrize("failed_size", [1, 50])
def test_mapping_network_failures_remain_isolated_to_the_http_batch(failed_size):
    conditions = tuple(f"0x{index:064x}" for index in range(51))

    class Public:
        def list_markets(self, *, condition_ids, **kwargs):
            if len(condition_ids) == failed_size:
                raise httpx.ReadTimeout("offline batch timeout")
            return [{"condition_id": condition, "outcomes": {"yes": {"tokenId": "yes-token"}},
                     "events": []} for condition in condition_ids]

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), object(), public_client_factory=Public,
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions)
        failed = conditions[-1:] if failed_size == 1 else conditions[:50]
        known = conditions[:50] if failed_size == 1 else conditions[-1:]
        assert result["state"] == "partial"
        assert tuple(result["markets"]) == known
        assert result["failed_ids"] == dict.fromkeys(failed, "market_read_ReadTimeout")
        assert result["confirmed_absent_ids"] == result["deferred_ids"] == ()
    finally:
        adapter.close()
