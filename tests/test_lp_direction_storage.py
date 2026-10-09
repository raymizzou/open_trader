"""Compact direction persistence through real preparation and public readers."""
from collections.abc import Mapping, ValuesView
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import sqlite3
import hashlib
import json
from threading import Event
import weakref

import pytest

from open_trader import polymarket_lp, polymarket_lp_scratch as scratch, polymarket_lp_views as views
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_lp_candidate_exclusions import ExclusionExchange
from test_polymarket_lp_views import NOW


BULKY = "distinctive-unrecognized-market-field:" + "0123456789abcdef" * 4096


class Summary(dict):
    """Weak-referenceable summary at the batch handoff boundary."""


def _base(name, *, two_sides, stamp=NOW):
    return {
        "market_id": f"market-{name}", "condition_id": f"condition-{name}",
        "market_title": f"Market {name}", "accepting_orders": True, "exchange_type": "CLOB",
        "metadata_checked_at": stamp, "fees_checked_at": stamp,
        "tick_size": Decimal("0.01"), "minimum_order_size": Decimal("20"),
        "reward_min_size": Decimal("20") if name == "a" else None,
        "reward_max_spread": Decimal("0.10") if name == "a" else None,
        "fees_enabled": False, "fee": Decimal("0"),
        "outcomes": {
            "yes": {"label": "YES", "token_id": f"token-condition-{name}"},
            **({"no": {"label": "NO", "token_id": f"no-condition-{name}"}} if two_sides else {}),
        },
        "unrecognized": {"text": BULKY, "amount": Decimal("1.2300"), "state": "UNKNOWN", "stamp": stamp},
        # Pre-existing overlays must survive when no new overlay replaces them.
        "_metadata_reward_min_size": Decimal("17"),
        "_metadata_reward_max_spread": Decimal("0.08"),
    }


class DirectionExchange(ExclusionExchange):
    def lp_market_metadata(self, condition_ids, **kwargs):
        return {cid: _base(cid.removeprefix("condition-"), two_sides=self.two_sides, stamp=self.now)
                for cid in condition_ids}

    def lp_reward_catalog(self, **kwargs):
        result = super().lp_reward_catalog(**kwargs)
        result["markets"] = [{**row, "rewards_min_size": Decimal("30"),
                              "rewards_max_spread": Decimal("7")} for row in result["markets"]]
        return result

    def lp_account_snapshot(self):
        return {**super().lp_account_snapshot(), "open_orders": [
            {"token_id": "token-condition-a", "status": "LIVE"}]}


def _service(path, *, two_sides=True, pools=None, exclusions_enabled=False):
    exchange = DirectionExchange(NOW, pools or {"a": Decimal("100"), "b": Decimal("90")})
    exchange.two_sides = two_sides
    store = PredictionArbitrageStore(path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now,
                                 exclusions_enabled=exclusions_enabled)
    assert service.refresh_competition_cache()["state"] == "known"
    assert service.refresh_price_history()["state"] == "known"
    store.lp_save_screening_snapshot({"event_end_confirmations": {
        "condition-a": {"state": "UNKNOWN", "checked_at": "2026-09-15T01:00:00Z"}}})
    for name in exchange.pools:
        for side in (("YES", "NO") if two_sides else ("YES",)):
            token = ("token-" if side == "YES" else "no-") + f"condition-{name}"
            store.lp_save_price_history(f"condition-{name}", token, [], _summary(name, side))
    return service, exchange


def _summary(name, side):
    # These are literal Store inputs/expected values, not business recomputation.
    return {"state": "unknown", "reason": "fixture-missing"} if (name, side) == ("b", "NO") else {
        "state": "known", "condition_id": f"condition-{name}",
        "token_id": ("token-" if side == "YES" else "no-") + f"condition-{name}",
        "amplitude": "0.005", "latest_midpoint": "0.505" if side == "YES" else "0.495",
        "checked_at": NOW.isoformat().replace("+00:00", "Z"),
        "valid_until": (NOW + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    }


def _expected(*, two_sides):
    rows = []
    for name in ("a", "b"):
        for side in (("YES", "NO") if two_sides else ("YES",)):
            market = {
                **_base(name, two_sides=two_sides),
                "token_id": ("token-" if side == "YES" else "no-") + f"condition-{name}",
                "outcome": side, "reward_min_size": Decimal("20" if name == "a" else "30"),
                "reward_max_spread": Decimal("0.10" if name == "a" else "0.07"),
                "_reward_catalog_min_size": Decimal("30"), "_reward_catalog_max_spread": Decimal("7"),
                "_metadata_reward_min_size": Decimal("20" if name == "a" else "17"),
                "_metadata_reward_max_spread": Decimal("0.10" if name == "a" else "0.08"),
            }
            rows.append({"market": market, "reward_active": True,
                         "daily_pool_usd": Decimal("100" if name == "a" else "90"),
                         "reward_checked_at": NOW, "reward_guidance_deadline": None,
                         "event_end_confirmation": ({"state": "UNKNOWN", "checked_at": "2026-09-15T01:00:00Z"} if name == "a" else None),
                         "history_summary": _summary(name, side),
                         **({"known_participation": True} if (name, side) == ("a", "YES") else {})})
    return rows


def test_direction_storage_shares_metadata_and_preserves_full_values(tmp_path, monkeypatch):
    original_encode, original_trial = scratch._encode, views.lp_trial_candidates
    payload_sizes, builds = [], []

    def measured_encode(value):
        payload = original_encode(value)
        # Read-only measurement at the SQLite serialization boundary.
        if isinstance(value, Mapping) and isinstance(value.get("market"), Mapping):
            payload_sizes.append(len(payload))
        return payload

    def checked_trial(facts, **kwargs):
        assert isinstance(facts, ValuesView)
        assert isinstance(facts._mapping, Mapping)
        expected = _expected(two_sides=len(facts) == 4)
        assert list(facts) == expected
        assert list(facts) == expected
        mutated = facts._mapping[next(iter(facts._mapping))]
        mutated["market"]["unrecognized"]["text"] = "caller mutation"
        mutated["market"]["outcomes"].clear()
        mutated["history_summary"].clear()
        assert list(facts) == expected
        projected = original_trial(facts, **kwargs)
        assert original_trial(facts, **kwargs) == projected
        builds.append(projected)
        return projected

    monkeypatch.setattr(scratch, "_encode", measured_encode)
    monkeypatch.setattr(views, "lp_trial_candidates", checked_trial)
    totals = []
    for two_sides in (False, True):
        service, _ = _service(tmp_path / str(two_sides), two_sides=two_sides)
        payload_sizes.clear()
        assert service._candidate_queue_state_build() is not None
        assert len(payload_sizes) == (4 if two_sides else 2)
        totals.append(sum(payload_sizes))
    assert len(builds) == 2
    # Two more direction facts cannot write either 64-KiB shared base again.
    assert totals[1] - totals[0] < len(BULKY), totals


@pytest.mark.parametrize("failure", [None, CancelledError, RuntimeError])
def test_direction_reader_keeps_generation_and_releases_on_exit(tmp_path, monkeypatch, failure):
    connections = []
    original_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def observed_connect(database, *args, **kwargs):
        if database == "":
            kwargs["factory"] = ObservedConnection
        connection = original_connect(database, *args, **kwargs)
        if database == "":
            connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    service, exchange = _service(tmp_path)
    old_metadata = weakref.ref(service._prepared_inputs["metadata"])
    old_resources = tuple(connection for connection in connections if not connection.closed)
    entered, release = Event(), Event()
    original_trial = views.lp_trial_candidates
    sources, reader_resources = [], []

    def blocked_trial(facts, **kwargs):
        sources.append(weakref.ref(facts._mapping))
        reader_resources.extend(connection for connection in connections
                                if connection not in old_resources and not connection.closed)
        assert list(facts) == _expected(two_sides=True)
        entered.set()
        assert release.wait(10), "independent projection watchdog"
        # The held reader still sees its original immutable generation.
        assert list(facts) == _expected(two_sides=True)
        assert old_metadata() is not None
        assert all(not connection.closed for connection in old_resources)
        if failure is not None:
            raise failure("external reader interrupted")
        return original_trial(facts, **kwargs)

    def build_without_retaining_exception():
        try:
            return service._candidate_queue_state_build()
        except (CancelledError, RuntimeError) as error:
            # A Future retaining a traceback is an active reader; end it here.
            return type(error), str(error)

    monkeypatch.setattr(views, "lp_trial_candidates", blocked_trial)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(build_without_retaining_exception)
        try:
            assert entered.wait(10), "reader did not reach projection"
            exchange.now += timedelta(minutes=1)
            assert service.refresh_price_history()["state"] == "known"
            assert service._prepared_input_snapshot()["metadata"]["condition-a"]["metadata_checked_at"] == exchange.now
            assert old_metadata() is not None
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result is None if failure is None else result == (failure, "external reader interrupted")
    assert service._candidate_queue_state is None, "stale generation was published"
    assert sources[0]() is None
    assert old_metadata() is None
    assert all(connection.closed for connection in old_resources)
    assert reader_resources and all(connection.closed for connection in reader_resources)
    monkeypatch.setattr(views, "lp_trial_candidates", original_trial)
    state = service._candidate_queue_state_build()
    assert state is not None
    assert state["metadata_by_condition"]["condition-a"]["metadata_checked_at"] == exchange.now


def _complete_projection_trace(path, monkeypatch):
    service, exchange = _service(path, pools={name: Decimal("100") for name in "abcdef"},
                                 exclusions_enabled=True)
    for name in "cd":
        for side in ("YES", "NO"):
            summary = _summary(name, side)
            summary["checked_at"] = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
            token = ("token-" if side == "YES" else "no-") + f"condition-{name}"
            service.store.lp_save_price_history(f"condition-{name}", token, [], summary)
    for name, token in (("e", "token-condition-e"), ("f", "")):
        assert service.store.lp_record_market_exclusion(
            f"condition-{name}", token, "history_amplitude_exceeded",
            checked_at=NOW, cooldown_until=NOW + timedelta(hours=1), now=NOW)
    original_trial = views.lp_trial_candidates
    trials, states = [], []

    def observed_trial(facts, **kwargs):
        result = original_trial(facts, **kwargs)
        assert original_trial(facts, **kwargs) == result
        trials.append(deepcopy(result))
        return result

    monkeypatch.setattr(views, "lp_trial_candidates", observed_trial)
    first = service._candidate_queue_state_build()
    assert [row["condition_id"] for row in first["queue_normal"]] == ["condition-a", "condition-e", "condition-b"]
    assert [row["condition_id"] for row in first["queue_backup"]] == ["condition-c", "condition-d"]
    def snapshot(state):
        return deepcopy({key: dict(value) if key in {
            "directions_by_condition", "metadata_by_condition", "reward_market_by_condition"
        } else value for key, value in state.items()})

    states.append(snapshot(first))
    assert service._candidate_queue_state_build() == first
    revision = service._candidate_exclusion_revision
    assert service._exclude_candidate(
        "condition-c", "no-condition-c", "history_amplitude_exceeded", checked_at=NOW)
    assert service._candidate_exclusion_revision > revision
    assert service._candidate_queue_state is None
    second = service._candidate_queue_state_build()
    assert [row["market"]["outcome"] for row in second["directions_by_condition"]["condition-c"]] == ["YES"]
    states.append(snapshot(second))
    exchange.now += timedelta(minutes=1)
    assert service.refresh_price_history()["state"] == "known"
    third = service._candidate_queue_state_build()
    assert third["version"] > second["version"]
    states.append(snapshot(third))
    return {"trials": trials, "states": states}


def test_compact_direction_projection_matches_complete_queues_and_overrides(tmp_path, monkeypatch):
    trace = _complete_projection_trace(tmp_path, monkeypatch)
    assert [state.pop("competition_version") for state in trace["states"]] == [1, 1, 1]
    assert [state.pop("build_sequence") for state in trace["states"]] == [1, 3, 4]
    # The two internal queue fences are checked separately; keep the complete business golden.
    digest = hashlib.sha256(json.dumps(trace, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Recorded independently with this fixture against read-only 3ad0d3ec source.
    assert digest == "ffbde7cce77aa091183c50a0d355b9f5a59d7f8111053cb2315dbfc51bde2bdb"


def test_service_exclusion_of_nonqueued_direction_preserves_existing_queue(tmp_path):
    service, _ = _service(tmp_path, pools={name: Decimal(100) for name in "abc"},
                          exclusions_enabled=True)
    for token in ("token-condition-c", "no-condition-c"):
        service.store.lp_save_price_history("condition-c", token, [], {"state": "unknown"})
    service.refresh_candidates()
    old_queue = service._candidate_queue_state
    assert "condition-c" not in old_queue["directions_by_condition"]
    old_directions = deepcopy(dict(old_queue["directions_by_condition"]))
    old_rows = deepcopy((old_queue["queue_normal"], old_queue["queue_backup"]))
    assert old_directions, "regression requires an existing populated queue"
    assert service._exclude_candidate("condition-c", "token-condition-c",
        "history_amplitude_exceeded", checked_at=NOW)
    assert not service._candidate_allowed((("condition-c", "token-condition-c"),))
    assert service._candidate_allowed((("condition-c", "no-condition-c"),))
    assert dict(old_queue["directions_by_condition"]) == old_directions
    assert (old_queue["queue_normal"], old_queue["queue_backup"]) == old_rows
    for key in ("directions_by_condition", "metadata_by_condition", "reward_market_by_condition"):
        assert "condition-c" not in old_queue[key], f"exclusion inserted an absent {key} condition"
    assert service._candidate_queue_state is None, "exclusion did not invalidate the queue"


def test_cached_queue_stays_compact_through_renewals_exclusions_and_publication(tmp_path):
    service, exchange = _service(tmp_path, pools={str(i): Decimal(100) for i in range(32)},
                                 exclusions_enabled=True)
    state = service._candidate_queue_state_build()
    source = service._prepared_inputs["metadata"]
    reference = weakref.ref(source)
    for key in ("directions_by_condition", "metadata_by_condition", "reward_market_by_condition"):
        assert not isinstance(state[key], dict), f"{key} retained the expanded universe"
    assert state["directions_by_condition"]._source._metadata._source is state["metadata_by_condition"]._source
    assert state["metadata_by_condition"]._source["condition-0"] == source["condition-0"]
    before = deepcopy(state["directions_by_condition"]["condition-31"])
    for offset in range(0, 32, 8):
        exchange.now += timedelta(minutes=2)
        ids = tuple(f"condition-{i}" for i in range(offset, offset + 8))
        assert service._renew_batch_shared_facts(state, ids, stop_event=None) is not None
        for cid in ids:
            assert state["directions_by_condition"][cid][0]["market"]["metadata_checked_at"] == exchange.now
        assert service.store.lp_record_market_exclusion(
            ids[0], f"no-{ids[0]}", "history_amplitude_exceeded", checked_at=exchange.now,
            cooldown_until=exchange.now + timedelta(hours=1), now=exchange.now)
        service._candidate_queue_state = state
        service._evict_excluded_candidates(ids[0])
        assert [d["market"]["outcome"] for d in state["directions_by_condition"][ids[0]]] == ["YES"]
        for key in ("directions_by_condition", "metadata_by_condition", "reward_market_by_condition"):
            assert not isinstance(state[key], dict)
        directions = state["directions_by_condition"]
        assert all(BULKY not in str(row) for row in directions._source._values.values())
        assert all(isinstance(key, str) for keys in directions._keys.values() for key in keys)
        assert len(directions._source._values) <= 64, "renewal accumulated obsolete direction versions"
    assert source["condition-0"]["metadata_checked_at"] == NOW, "renewal mutated published metadata"
    assert before[0]["market"]["unrecognized"]["text"] == BULKY
    # History invalidation builds another queue while this old reader stays usable.
    for _ in range(2):
        service._candidate_history_version += 1
        replacement = service._candidate_queue_state_build()
        assert replacement is not state
        assert not isinstance(replacement["directions_by_condition"], dict)
    assert service.refresh_price_history()["state"] == "known"
    assert reference() is source
    assert state["directions_by_condition"]["condition-31"][0]["market"]["unrecognized"]["text"] == BULKY
    del source, state, replacement, directions
    assert reference() is None, "obsolete queue generation needs GC to close"


def test_candidate_build_consumes_history_batches_without_accumulating_rows(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, pools={str(i): Decimal(100) for i in range(450)})
    batches = getattr(service.store, "lp_price_history_summary_batches", None)
    assert callable(batches), "candidate build has no bounded snapshot reader"
    references = []

    def tracked_batches(*args, **kwargs):
        for batch in batches(*args, **kwargs):
            wrapped = {key: Summary(row) for key, row in batch.items()}
            references.extend(weakref.ref(row) for row in wrapped.values())
            yield wrapped
            del wrapped

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate build used accumulated summaries")

    monkeypatch.setattr(service.store, "lp_price_history_summary_batches", tracked_batches)
    monkeypatch.setattr(service.store, "lp_price_history_summaries", forbidden)
    original_fact = polymarket_lp._lp_direction_fact

    def fact(*args, **kwargs):
        assert not any(ref() is not None for ref in references), "history batches retained into ranking"
        return original_fact(*args, **kwargs)

    monkeypatch.setattr("open_trader.polymarket_lp._lp_direction_fact", fact)
    state = service._candidate_queue_state_build()
    assert len(state["directions_by_condition"]) == 450
    assert len(references) == 900
    assert all(ref() is None for ref in references)


def test_queue_direction_assignment_restores_reorders_and_replaces_tokens(tmp_path):
    service, exchange = _service(tmp_path, exclusions_enabled=True)
    state = service._candidate_queue_state_build()
    directions = state["directions_by_condition"]
    original = directions["condition-a"]
    assert service._exclude_candidate("condition-a", "no-condition-a", "history_amplitude_exceeded", checked_at=NOW)
    assert [row["market"]["outcome"] for row in directions["condition-a"]] == ["YES"]
    exchange.now += timedelta(hours=2)
    assert service._candidate_allowed((("condition-a", "no-condition-a"),))
    # Mapping assignment must accept recovered direction sets, as dict did.
    directions["condition-a"] = list(reversed(original))
    assert directions["condition-a"] == list(reversed(original))
    assert service._renew_batch_shared_facts(state, ("condition-a",), stop_event=None) is not None
    assert [row["market"]["outcome"] for row in directions["condition-a"]] == ["NO", "YES"]
    changed = deepcopy(directions["condition-a"])
    changed[0]["market"]["token_id"] = "replacement-no"
    changed[0]["market"]["one_direction_only"] = {"state": "UNKNOWN"}
    directions["condition-a"] = changed
    assert directions["condition-a"] == changed
    saved = deepcopy(state)
    directions["condition-a"] = []
    assert directions["condition-a"] == []
    directions["new-condition"] = changed
    # Same dict contract: supplied condition grouping does not rewrite market identity.
    assert directions["new-condition"] == changed
    state["metadata_by_condition"].pop("condition-a")
    assert "condition-a" not in state["metadata_by_condition"]
    state["metadata_by_condition"].clear()
    directions.clear()
    assert len(directions) == 0 and len(directions._source._values) == 0
    assert saved["directions_by_condition"]["condition-a"] == changed
    assert saved["metadata_by_condition"]["condition-a"]["metadata_checked_at"] == exchange.now
    assert service._prepared_inputs["metadata"]["condition-a"]["metadata_checked_at"] == NOW


@pytest.mark.parametrize("phase", ["screen", "rebuild"])
def test_renewal_does_not_resurrect_condition_evicted_during_rebuild(tmp_path, monkeypatch, phase):
    service, exchange = _service(tmp_path, exclusions_enabled=True)
    state = service._candidate_queue_state_build()
    exchange.now += timedelta(minutes=2)
    original = polymarket_lp._lp_direction_fact
    excluded = False
    def exclude_once():
        nonlocal excluded
        if not excluded:
            excluded = True
            assert service._exclude_candidate("condition-a", "", "history_amplitude_exceeded", checked_at=exchange.now)
    def exclude_during_rebuild(*args, **kwargs):
        row = original(*args, **kwargs)
        exclude_once()
        return row
    if phase == "rebuild":
        monkeypatch.setattr(polymarket_lp, "_lp_direction_fact", exclude_during_rebuild)
    else:
        screen = service._screen_candidate_batch
        def exclude_during_screen(*args, **kwargs):
            result = screen(*args, **kwargs)
            exclude_once()
            return result
        monkeypatch.setattr(service, "_screen_candidate_batch", exclude_during_screen)
    service._renew_batch_shared_facts(state, ("condition-a",), stop_event=None)
    assert excluded
    for key in ("directions_by_condition", "metadata_by_condition", "reward_market_by_condition"):
        assert "condition-a" not in state[key], f"renewal resurrected excluded {key}"


@pytest.mark.parametrize("mutation", ["filter", "pop"])
@pytest.mark.parametrize("reader_kind", ["lookup", "deepcopy"])
def test_queue_lookup_keeps_old_values_during_concurrent_removal(tmp_path, monkeypatch, mutation, reader_kind):
    service, _ = _service(tmp_path)
    state = service._candidate_queue_state_build()
    directions = state["directions_by_condition"]
    expected = directions["condition-a"]
    entered, release, attempted = Event(), Event(), Event()
    original = scratch.LPReadScratch.__getitem__
    def blocked(self, key):
        row = original(self, key)
        if self is directions._source._values and not entered.is_set():
            entered.set()
            assert release.wait(10), "independent lookup watchdog"
        return row
    monkeypatch.setattr(scratch.LPReadScratch, "__getitem__", blocked)
    original_copy = scratch.LPReadScratch.__deepcopy__
    def blocked_copy(self, memo):
        clone = original_copy(self, memo)
        if self is directions._source._values:
            entered.set()
            assert release.wait(10), "independent deepcopy watchdog"
        return clone
    monkeypatch.setattr(scratch.LPReadScratch, "__deepcopy__", blocked_copy)
    def remove():
        attempted.set()
        if mutation == "filter":
            directions.filter_tokens("condition-a", lambda token: token.startswith("token-"))
        else:
            directions.pop("condition-a")
    with ThreadPoolExecutor(2) as workers:
        reader = workers.submit(lambda: directions["condition-a"] if reader_kind == "lookup" else deepcopy(directions))
        try:
            assert entered.wait(10)
            mutator = workers.submit(remove)
            assert attempted.wait(10)
        finally:
            release.set()
        old = reader.result(timeout=10)
        assert (old if reader_kind == "lookup" else old["condition-a"]) == expected
        mutator.result(timeout=10)
    assert expected == _expected(two_sides=True)[:2]
    if mutation == "filter":
        assert [row["market"]["outcome"] for row in directions["condition-a"]] == ["YES"]
    else:
        assert "condition-a" not in directions


@pytest.mark.parametrize("failure", [RuntimeError, CancelledError])
def test_failed_summary_snapshot_discards_partial_rows_and_closes(tmp_path, monkeypatch, failure):
    from contextlib import closing
    service, _ = _service(tmp_path, pools={str(i): Decimal(100) for i in range(220)})
    original = service.store.lp_price_history_summary_batches
    closed = Event()
    connections = []
    connect = service.store._connection
    def observed():
        connection = connect()
        connections.append(connection)
        return connection
    monkeypatch.setattr(service.store, "_connection", observed)
    read_connections = []
    def fail_second_batch(*args, **kwargs):
        try:
            with closing(original(*args, **kwargs)) as batches:
                yield next(batches)
                read_connections.append(connections[-1])
                assert read_connections[-1].in_transaction
                raise failure("controlled second summary batch failure")
        finally:
            closed.set()
    monkeypatch.setattr(service.store, "lp_price_history_summary_batches", fail_second_batch)
    trial = views.lp_trial_candidates
    observed = []
    def unknown_trial(facts, **kwargs):
        assert all("history_summary" not in row for row in facts), "partial history survived read failure"
        observed.append(len(facts))
        return trial(facts, **kwargs)
    monkeypatch.setattr(views, "lp_trial_candidates", unknown_trial)
    state = service._candidate_queue_state_build()
    assert closed.is_set()
    # All 440 directions are unavailable/UNKNOWN, including the 400 already read.
    assert observed == [440]
    assert state["queue_normal"] == state["queue_backup"] == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        read_connections[0].execute("SELECT 1")
