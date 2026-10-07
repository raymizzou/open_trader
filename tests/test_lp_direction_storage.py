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

from open_trader import polymarket_lp_scratch as scratch, polymarket_lp_views as views
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_lp_candidate_exclusions import ExclusionExchange
from test_polymarket_lp_views import NOW


BULKY = "distinctive-unrecognized-market-field:" + "0123456789abcdef" * 4096


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
    states.append(deepcopy(first))
    assert service._candidate_queue_state_build() == first
    revision = service._candidate_exclusion_revision
    assert service._exclude_candidate(
        "condition-c", "no-condition-c", "history_amplitude_exceeded", checked_at=NOW)
    assert service._candidate_exclusion_revision > revision
    assert service._candidate_queue_state is None
    second = service._candidate_queue_state_build()
    assert [row["market"]["outcome"] for row in second["directions_by_condition"]["condition-c"]] == ["YES"]
    states.append(deepcopy(second))
    exchange.now += timedelta(minutes=1)
    assert service.refresh_price_history()["state"] == "known"
    third = service._candidate_queue_state_build()
    assert third["version"] > second["version"]
    states.append(deepcopy(third))
    return {"trials": trials, "states": states}


def test_compact_direction_projection_matches_complete_queues_and_overrides(tmp_path, monkeypatch):
    trace = _complete_projection_trace(tmp_path, monkeypatch)
    digest = hashlib.sha256(json.dumps(trace, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Recorded independently with this fixture against read-only 3ad0d3ec source.
    assert digest == "ffbde7cce77aa091183c50a0d355b9f5a59d7f8111053cb2315dbfc51bde2bdb"
