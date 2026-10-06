"""Regression tests for global preparation recovery fencing candidate scans."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.polymarket_lp import PolymarketLPService
from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity
from tests.test_lp_candidate_exclusions import ExclusionExchange
from tests.test_lp_auto_refill_contract import RefillPublic, prepare


@pytest.mark.parametrize("cached_queue", [False, True])
def test_global_recovery_discards_inflight_candidate_scan(
    runtime, tmp_path, monkeypatch, cached_queue
):
    class Public(RefillPublic):
        fail_catalog = False

        def list_current_rewards(self, *, sponsored):
            if self.fail_catalog:
                with sqlite3.connect(tmp_path / "dependency.sqlite3") as connection:
                    connection.execute("SELECT * FROM missing_table")
            return super().list_current_rewards(sponsored=sponsored)

    public = Public(runtime.clock)
    store, adapter, account, lp, _ = runtime(public_client=public)

    @contextmanager
    def histories(request, **kwargs):
        body = json.loads(request.data)
        yield SimpleNamespace(
            read=lambda: json.dumps(
                {
                    "history": {
                        token: [
                            {"t": body["start_ts"], "p": "0.40"},
                            {"t": body["end_ts"], "p": "0.401"},
                        ]
                        for token in body["markets"]
                    }
                }
            ).encode()
        )

    adapter._urlopen_fn = histories
    store.lp_competitiveness_upsert(
        (_refill_identity(i)[1], Decimal("2.5"), runtime.clock[0])
        for i in range(1, 7)
    )
    assert lp.refresh_price_history()["preparation_outcome"] == "success"
    assert lp._prepared_input_snapshot() is not None
    assert not lp._candidate_pool and not lp._candidate_qualification_facts
    if cached_queue:
        assert lp._candidate_queue_state_build() is not None

    entered, release = threading.Event(), threading.Event()
    read_books = adapter.lp_order_books

    def blocked_books(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Independent scan watchdog"
        return read_books(*args, **kwargs)

    monkeypatch.setattr(adapter, "lp_order_books", blocked_books)
    results, errors = [], []

    def scan():
        try:
            results.append(lp.refresh_candidates())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=scan)
    worker.start()
    try:
        assert entered.wait(5), "Scan did not reach books"
        public.fail_catalog = True
        paused = lp.refresh_price_history()["preparation"]
        assert paused["paused"] is True
        assert paused["last_sqlite_errorcode"] == sqlite3.SQLITE_ERROR
        public.fail_catalog = False
        recovered = lp.recover_preparation(
            scope="global", expected_generation=paused["generation"]
        )
        assert recovered["generation"] == paused["generation"] + 1
        assert lp._prepared_input_snapshot() is None
        release.set()
        worker.join(5)
        assert not worker.is_alive() and not errors, errors
        current = lp.preparation_snapshot()
        assert account.posts == account.cancels == []
        assert current["generation"] == recovered["generation"]
        assert current["attempt"] == 0
        assert not lp._candidate_pool
        assert not lp._candidate_qualification_facts
        assert results[0]["state"] != "ready"
        assert lp.candidate_snapshot()["scanning"] is False
        assert lp.refresh_price_history()["preparation_outcome"] == "success"
        current_scan = lp.refresh_candidates()
        assert current_scan["state"] == "ready"
        assert len(lp._candidate_pool) == 6
        assert len(lp._candidate_qualification_facts) == 6
        public = lp.candidate_snapshot()
        for field in ("candidates", "recommendations", "selected_results"):
            assert public[field]
            assert all(
                "global_recovery_generation" not in row for row in public[field]
            )
        global_generation = lp.preparation_snapshot()[
            "global_recovery_generation"
        ]
        assert all(
            row["global_recovery_generation"] == global_generation
            for row in lp._candidate_pool.values()
        )
        assert all(
            facts["global_recovery_generation"] == global_generation
            for facts in lp._candidate_qualification_facts.values()
        )
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive(), "Scan cleanup watchdog"


def test_global_recovery_keeps_old_display_but_blocks_old_facts_and_maintenance(
    runtime,
):
    store, _adapter, _account, lp, execution, _public = prepare(
        runtime, count=2, target=2
    )
    assert len(execution._auto_pool.candidates()) == 2
    runtime.clock[0] += timedelta(seconds=61)
    before_maintenance = lp.candidate_snapshot()
    maintained = lp.refresh_candidate_recommendations()
    assert maintained["candidates"]
    assert maintained["last_success_at"] != before_maintenance["last_success_at"]
    assert len(execution._auto_pool.candidates()) == 2
    old_pool = deepcopy(lp._candidate_pool)
    old_facts = deepcopy(lp._candidate_qualification_facts)
    paused = {
        **lp.preparation_snapshot(),
        "state": "paused",
        "paused": True,
        "last_error": "OperationalError",
    }
    store.lp_save_preparation(paused)
    recovered = lp.recover_preparation(
        scope="global", expected_generation=paused["generation"]
    )
    assert recovered["generation"] == paused["generation"] + 1
    assert lp._candidate_pool == old_pool
    assert lp._candidate_qualification_facts == old_facts
    assert execution._auto_pool.candidates() == []

    condition_id = next(iter(old_pool))
    row = old_pool[condition_id]
    assert not lp._candidate_pool_record_success(
        condition_id,
        {**row, "late": True},
        judged_at=lp._now(),
        facts={"directions": [], "account": {}},
        global_recovery_generation=paused["generation"] - 1,
    )
    lp._candidate_pool_record_failure(
        condition_id,
        attempted_at=lp._now(),
        global_recovery_generation=paused["generation"] - 1,
    )
    lp._candidate_pool_record_rejection(
        condition_id,
        judged_at=lp._now(),
        global_recovery_generation=paused["generation"] - 1,
    )
    assert lp._candidate_pool == old_pool
    assert lp._candidate_qualification_facts == old_facts
    lp.refresh_candidate_recommendations()
    assert lp._candidate_pool == old_pool
    assert lp._candidate_qualification_facts == old_facts


def test_screening_snapshot_store_fences_stale_global_generation(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation({"generation": 1, "state": "paused", "paused": True})
    store.lp_recover_preparation_items((), expected_generation=1)
    assert (
        store.lp_save_screening_snapshot(
            {
                "global_recovery_generation": 1,
                "scan_started_at": "2026-10-06T00:00:00Z",
                "pool": {"stale": {"condition_id": "stale"}},
            }
        )
        is None
    )
    assert store.lp_screening_snapshot() is None
    saved = store.lp_save_screening_snapshot(
        {
            "global_recovery_generation": 2,
            "scan_started_at": "2026-10-06T00:00:01Z",
            "pool": {"fresh": {"condition_id": "fresh"}},
        }
    )
    assert saved["pool"]["fresh"]["condition_id"] == "fresh"


@pytest.mark.parametrize("recover", [False, True])
def test_maintenance_reward_rejection_respects_global_recovery_cutoff(
    tmp_path, recover
):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 6, tzinfo=UTC)
    exchange = ExclusionExchange(now, {"m": Decimal("20")})
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(
        store,
        exchange,
        clock=lambda: exchange.now,
        exclusions_enabled=True,
    )
    service.refresh_competition_cache()
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_candidates()["candidates"]
    assert len(service._candidate_pool) == len(service._candidate_qualification_facts) == 1

    exchange.inactive.add("condition-m")
    exchange.now += timedelta(seconds=61)
    entered, release = threading.Event(), threading.Event()
    read_rewards = exchange.lp_reward_catalog

    def delayed_rewards(**kwargs):
        entered.set()
        assert release.wait(5), "Independent maintenance reward watchdog"
        return read_rewards(**kwargs)

    if recover:
        exchange.lp_reward_catalog = delayed_rewards
        results, errors = [], []

        def maintain():
            try:
                results.append(service.refresh_candidate_recommendations())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=maintain)
        worker.start()
        try:
            assert entered.wait(5), "Maintenance did not reach reward read"
            paused = {
                **service.preparation_snapshot(),
                "state": "paused",
                "paused": True,
                "last_error": "OperationalError",
            }
            store.lp_save_preparation(paused)
            recovered = service.recover_preparation(
                scope="global", expected_generation=paused["generation"]
            )
            assert recovered["generation"] == paused["generation"] + 1
            release.set()
            worker.join(5)
            assert not worker.is_alive() and not errors, errors
            assert len(service._candidate_pool) == 1
            assert len(service._candidate_qualification_facts) == 1
            assert store.lp_market_exclusion_counts(now=exchange.now) == {}
            exchange.inactive.clear()
            assert service.refresh_price_history()["preparation_outcome"] == "success"
            assert service.refresh_candidates()["candidates"]
        finally:
            release.set()
            worker.join(5)
            assert not worker.is_alive(), "Maintenance cleanup watchdog"
    else:
        result = service.refresh_candidate_recommendations()
        assert result["candidates"] == []
        assert store.lp_market_exclusion_counts(now=exchange.now) == {
            "reward_inactive": 1
        }
        assert not service._candidate_pool
        assert not service._candidate_qualification_facts
