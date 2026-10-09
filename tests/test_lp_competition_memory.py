"""Competition ownership, bounded persistence and full-pool projection contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sqlite3
from threading import Event

import pytest

from open_trader import polymarket_lp
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_trading import CompetitionOpener, make_competition_adapter
from test_polymarket_lp import _LPCandidateQueryExchange


NOW = datetime(2026, 10, 9, 8, tzinfo=UTC)


def test_adapter_does_not_copy_previous_round_before_reading_pages():
    opener = CompetitionOpener()

    class Previous(dict):
        def items(self):
            assert opener.calls, "previous round eagerly copied before page read"
            return super().items()

    previous = Previous({"old": (Decimal("9.1234567890123456789"), NOW)})
    result = make_competition_adapter(opener).lp_market_competitiveness(previous=previous)
    assert result["not_updated"] == ["old"]
    assert "old" not in result["competitiveness"]
    assert previous == {"old": (Decimal("9.1234567890123456789"), NOW)}


@pytest.mark.parametrize("mode", ["complete", "partial", "failed", "cancelled", "resumed"])
def test_adapter_retains_valid_previous_facts_only_when_needed(monkeypatch, mode):
    from open_trader import polymarket_trading
    monkeypatch.setattr(polymarket_trading, "LP_COMPETITIVENESS_RETRY_PAUSE_SECONDS", 0)
    previous = {"old": (Decimal("9.1234567890123456789"), NOW), "bad": ("unknown", NOW),
                "": (Decimal("1"), NOW), "condition-a": (Decimal("8"), NOW)}
    before = dict(previous)
    opener = CompetitionOpener(page_two_failures=-1 if mode == "partial" else 0, fail_all=mode == "failed")
    stop = Event()
    if mode == "cancelled":
        stop.set()
    result = make_competition_adapter(opener).lp_market_competitiveness(
        previous=previous, stop_event=stop, start_cursor="Mg==" if mode == "resumed" else None)
    assert previous == before
    assert "bad" not in result["competitiveness"] and "" not in result["competitiveness"]
    assert result["state"] == {"complete": "known", "partial": "partial", "failed": "unknown",
                              "cancelled": "unknown", "resumed": "known"}[mode]
    assert result["complete"] is (mode in ("complete", "resumed"))
    assert result["not_updated"] == (["old"] if mode in ("complete", "partial") else ["condition-a", "old"])
    if mode == "complete":
        assert "old" not in result["competitiveness"]
    else:
        assert result["competitiveness"]["old"] == before["old"]
    if mode in ("complete", "partial"):
        assert result["competitiveness"]["condition-b"] == (Decimal("0"), result["round_checked_at"])
    assert result["resume_cursor"] == ("Mg==" if mode == "partial" else None)


def test_monitor_refresh_keeps_one_cache_owner_without_round_map_or_snapshot(tmp_path, monkeypatch):
    exchange = make_competition_adapter(CompetitionOpener())
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange)
    read = PolymarketTradingClient.lp_market_competitiveness
    rounds = []

    def capture(self, **kwargs):
        result = read(self, **kwargs)
        rounds.append(result)
        return result

    monkeypatch.setattr(PolymarketTradingClient, "lp_market_competitiveness", capture)
    copy = polymarket_lp.deepcopy

    def reject_snapshot(value, *args):
        assert value is not service._competition_state, "discarded full-round snapshot"
        return copy(value, *args)

    monkeypatch.setattr(polymarket_lp, "deepcopy", reject_snapshot)
    assert service.refresh_competition_cache(snapshot=False) is None
    first_map = service._competition_state["competitiveness"]
    assert rounds[0]["competitiveness"] == {}, "native reader accumulated a full round"
    assert service.store.lp_competitiveness_map() == first_map
    monkeypatch.setattr(polymarket_lp, "deepcopy", copy)
    snapshot = service.refresh_competition_cache()
    expected = dict(snapshot["competitiveness"])
    snapshot["competitiveness"].clear()
    snapshot["not_updated"].append("mutated")
    assert service._competition_state["competitiveness"] == expected
    assert service._competition_state["competitiveness"] is first_map
    assert rounds[1]["competitiveness"] == {}
    assert "mutated" not in service._competition_state["not_updated"]


@pytest.mark.parametrize("reader_kind", ["legacy", "subclass", "instance_override"])
def test_legacy_reader_result_keeps_snapshot_isolation(tmp_path, reader_kind):
    result = {"state": "known", "complete": True, "round_checked_at": NOW,
              "competitiveness": {"a": (Decimal("0"), NOW)}, "not_updated": []}

    class Exchange:
        def lp_market_competitiveness(self, **kwargs):
            return result

    if reader_kind == "subclass":
        class CustomReader(PolymarketTradingClient):
            lp_market_competitiveness = Exchange.lp_market_competitiveness
        from test_polymarket_trading import TradingConfig, SIGNER, WALLET
        from types import SimpleNamespace
        exchange = CustomReader(TradingConfig(SIGNER, WALLET), client=SimpleNamespace())
    elif reader_kind == "instance_override":
        exchange = make_competition_adapter(CompetitionOpener())
        exchange.lp_market_competitiveness = Exchange().lp_market_competitiveness
    else:
        exchange = Exchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange)
    snapshot = service.refresh_competition_cache()
    result["competitiveness"].clear()
    result["not_updated"].append("changed")
    assert service._competition_state["competitiveness"] == snapshot["competitiveness"]
    assert service._competition_state["not_updated"] == []
    snapshot["competitiveness"].clear()
    assert service._competition_state["competitiveness"] == {"a": (Decimal("0"), NOW)}


@pytest.mark.parametrize("failure", [None, "validation", "write", "iterator"])
def test_persistence_streams_and_rolls_back_late_failures(tmp_path, monkeypatch, failure):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([("old", Decimal("8"), NOW)])
    connections = []
    connection = store._connection

    def capture():
        current = connection()
        connections.append(current)
        if failure == "write":
            current.execute("CREATE TEMP TRIGGER fail_late BEFORE INSERT ON lp_market_competitiveness "
                            "WHEN NEW.condition_id='row-8' BEGIN SELECT RAISE(ABORT,'late write'); END")
        return current

    monkeypatch.setattr(store, "_connection", capture)

    def entries():
        for index in range(12):
            if index >= 2:
                assert connections and connections[0].total_changes >= index - 1, "round buffered before writing"
            if failure == "iterator" and index == 8:
                raise RuntimeError("late input failure")
            yield ("old" if index == 0 else f"row-{index}",
                   "invalid" if failure == "validation" and index == 8 else Decimal("0.1234567890123456789"), NOW)

    if failure:
        error = {"validation": ValueError, "write": sqlite3.IntegrityError, "iterator": RuntimeError}[failure]
        with pytest.raises(error):
            store.lp_competitiveness_upsert(entries())
        assert store.lp_competitiveness_map() == {"old": (Decimal("8"), NOW)}
    else:
        assert store.lp_competitiveness_upsert(entries()) == 12
        assert len(store.lp_competitiveness_map()) == 12
        assert store.lp_competitiveness_entry("row-11") == (Decimal("0.1234567890123456789"), NOW)


def test_service_persistence_does_not_build_full_entries_list(tmp_path):
    streamed = []
    class Store:
        def lp_competitiveness_upsert(self, entries):
            streamed.append(not isinstance(entries, (list, tuple)))
            assert list(entries) == [("a", Decimal("0"), NOW)]

    service = PolymarketLPService(Store(), object())
    service._persist_competition({"competitiveness": {"a": (Decimal("0"), NOW)}})
    assert streamed == [True], "service buffered full round"


@pytest.mark.parametrize("state", ["known", "partial", "unknown"])
def test_scoped_projection_only_reads_missing_stale_identities(tmp_path, monkeypatch, state):
    store = PredictionArbitrageStore(tmp_path)
    old = NOW - timedelta(hours=4)
    store.lp_competitiveness_upsert([(cid, Decimal("7"), old) for cid in ("fresh", "stale", "missing", "unrelated")])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {"state": state, "complete": state == "known", "round_checked_at": NOW,
        "competitiveness": {"fresh": (Decimal("0"), NOW), "stale": (Decimal("2"), old),
                            "future": (Decimal("3"), NOW + timedelta(seconds=1)), "unrelated": (Decimal("4"), NOW),
                            "untouched": {"value": Decimal("1.1234567890123456789"), "checked_at": NOW - timedelta(minutes=1)}}}
    calls = []
    reader = store.lp_competitiveness_map

    def scoped(*, condition_ids=None):
        assert condition_ids is not None, "full persisted map queried"
        calls.append(set(condition_ids))
        return {**reader(condition_ids=condition_ids), "unrelated": (Decimal("4"), NOW)}

    monkeypatch.setattr(store, "lp_competitiveness_map", scoped)
    required = ("fresh", "stale", "missing", "absent", "future", "untouched")
    # The compatibility projection is our reference for every required identity.
    monkeypatch.setattr(store, "lp_competitiveness_map", reader)
    reference = service._competition_entries()
    monkeypatch.setattr(store, "lp_competitiveness_map", scoped)
    actual = service._competition_entries(required)
    assert actual == {cid: reference[cid] for cid in required if cid in reference}
    assert calls == [{"stale", "missing", "absent", "future"}]
    assert actual["fresh"] == {"value": Decimal("0"), "checked_at": NOW, "source": "fresh", "updated": True}
    assert actual["stale"]["source"] == "store"
    assert actual["stale"]["updated"] is None
    assert actual["untouched"]["updated"] is False
    assert actual["untouched"]["source"] == "fresh"
    calls.clear()
    assert service._competition_entries(("fresh",)) == {"fresh": actual["fresh"]}
    assert not calls


def test_scoped_store_reads_all_requested_ids_in_bounded_queries(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    ids = [f"id-{index}" for index in range(805)]
    store.lp_competitiveness_upsert((cid, Decimal(index), NOW) for index, cid in enumerate(ids + ["unrelated"]))
    sql = []
    connection = store._connection

    def traced():
        current = connection()
        current.set_trace_callback(sql.append)
        return current

    monkeypatch.setattr(store, "_connection", traced)
    actual = store.lp_competitiveness_map(condition_ids=iter(ids + [ids[0]]))
    assert actual == {cid: (Decimal(index), NOW) for index, cid in enumerate(ids)}
    selects = [query for query in sql if query.lstrip().startswith("SELECT")]
    assert len(selects) == 3
    assert all("WHERE condition_id IN" in query for query in selects)
    assert any(query == "BEGIN" for query in sql)
    sql.clear()
    assert store.lp_competitiveness_map(condition_ids=[]) == {}
    assert not sql


@pytest.mark.parametrize("failure", [False, True])
def test_projection_preserves_legacy_store_and_failure_unknown(tmp_path, failure):
    calls = []
    class Store:
        def lp_competitiveness_map(self):
            calls.append(True)
            if failure:
                raise RuntimeError("store unavailable")
            return {"a": (Decimal("2"), NOW), "unrelated": (Decimal("3"), NOW)}

    service = PolymarketLPService(Store(), object(), clock=lambda: NOW)
    actual = service._competition_entries(("a", "missing"))
    assert calls == [True]
    assert actual == ({} if failure else {"a": {"value": Decimal("2"), "checked_at": NOW,
                                               "updated": None, "source": "store"}})


def test_projection_keeps_captured_round_when_publication_interleaves(tmp_path):
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), object())
    old = {"round_checked_at": NOW, "competitiveness": {"a": (Decimal("1"), NOW)}}
    new = {"round_checked_at": NOW + timedelta(minutes=1),
           "competitiveness": {"a": (Decimal("2"), NOW + timedelta(minutes=1))}}
    service._competition_state = old
    def publish_during_projection():
        with service._competition_lock:
            service._competition_state = new
        return NOW
    service.clock = publish_during_projection
    assert service._competition_entries(("a",)) == {"a": {"value": Decimal("1"), "checked_at": NOW,
                                                          "source": "fresh", "updated": True}}
    assert service._competition_state is new


def test_projection_keeps_old_facts_until_first_batch_commit(tmp_path, monkeypatch):
    exchange = make_competition_adapter(CompetitionOpener())
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    old = {"state": "known", "round_checked_at": NOW,
           "competitiveness": {"condition-a": (Decimal("9"), NOW), "old": (Decimal("2"), NOW)}}
    service.store.lp_competitiveness_upsert([("old", Decimal("2"), NOW)])
    service._competition_state = old
    entered, release = Event(), Event()
    read = exchange._urlopen_fn

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10), "page read not released"
        return read(*args, **kwargs)

    monkeypatch.setattr(exchange, "_urlopen_fn", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.refresh_competition_cache, snapshot=False)
        try:
            assert entered.wait(10), "page read did not start"
            assert service._competition_state is old
            assert service._competition_entries(("condition-a", "old"))["condition-a"]["value"] == Decimal("9")
        finally:
            release.set()
        assert pending.result(timeout=10) is None
    assert "old" not in old["competitiveness"]
    assert service._competition_entries(("old",))["old"]["value"] == Decimal("2")
    assert service._competition_state is old
    assert old["competitiveness"]["condition-a"][0] == Decimal("16.6")
    assert service._competition_state["competitiveness"]["condition-b"][0] == 0


def test_queue_requests_every_direction_identity_and_preserves_ranking(tmp_path, monkeypatch):
    from open_trader import polymarket_lp_views
    exchange = _LPCandidateQueryExchange(NOW, {f"M{index:02}": Decimal("57.6") for index in range(25)})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    service.refresh_price_history()
    service.refresh_competition_cache()
    projection = service._competition_entries
    trial = polymarket_lp_views.lp_trial_candidates
    required_calls = []

    def scoped(condition_ids=None, **kwargs):
        assert condition_ids is not None, "queue projected entire competition universe"
        required_calls.append(set(condition_ids))
        actual = projection(condition_ids, **kwargs)
        reference = projection()
        assert actual == {cid: reference[cid] for cid in condition_ids if cid in reference}
        return actual

    monkeypatch.setattr(service, "_competition_entries", scoped)
    def compare_trial(facts, **kwargs):
        actual = trial(facts, **kwargs)
        reference = trial(facts, **{**kwargs, "competition": projection()})
        assert actual == reference, "complete candidate output or full ranking changed"
        return actual
    monkeypatch.setattr(polymarket_lp_views, "lp_trial_candidates", compare_trial)
    result = service.refresh_candidates()
    assert required_calls == [{f"condition-M{index:02}" for index in range(25)}]
    assert result["funnel"]["read"] == 25
    normal = service._candidate_queue_state["queue_normal"]
    assert len(normal) == 25
    assert {row["condition_id"] for row in normal} == required_calls[0]


def test_trial_view_reads_required_keys_without_copying_input_mapping():
    from open_trader.polymarket_lp_views import lp_trial_candidates
    from test_polymarket_lp_views import _trial_direction, NOW as view_now
    class Competition(dict):
        def items(self):
            raise AssertionError("view copied every competition key")
    value = {"condition-A": (Decimal("2"), view_now), "unrelated": (Decimal("8"), view_now)}
    directions = [_trial_direction("A")]
    expected = lp_trial_candidates(directions, competition=value, account_budget_facts={}, now=view_now)
    actual = lp_trial_candidates(directions, competition=Competition(value), account_budget_facts={}, now=view_now)
    assert actual == expected
    actual["rows"][0]["competition"]["value"] = "mutated"
    assert value["condition-A"] == (Decimal("2"), view_now)


def test_candidate_snapshot_copies_only_round_metadata_under_its_own_lock(tmp_path):
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), object(), clock=lambda: NOW)

    class Competition(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("candidate snapshot copied full competition map")

    class NotUpdated(list):
        def __iter__(self):
            assert service._competition_lock.locked(), "round metadata read without competition lock"
            assert not service._candidate_state_lock._is_owned(), "new nested lock ordering"
            return super().__iter__()

    state = {"state": "partial",
             "competitiveness": Competition({f"id-{i}": (Decimal("2"), NOW) for i in range(1200)}),
             "not_updated": NotUpdated(["a", "b"])}
    service._competition_state = state
    snapshot = service.candidate_snapshot()
    assert snapshot["funnel"]["competition_state"] == "partial"
    assert snapshot["funnel"]["competition_not_updated"] == ["a", "b"]
    snapshot["funnel"]["competition_not_updated"].append("mutated")
    assert state["not_updated"] == ["a", "b"]
    with service._competition_lock:
        service._competition_state = {"state": "known", "not_updated": []}
    next_snapshot = service.candidate_snapshot()
    assert next_snapshot["funnel"]["competition_state"] == "known"
    assert next_snapshot["funnel"]["competition_not_updated"] == []
