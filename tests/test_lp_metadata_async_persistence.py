from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from polymarket.models.gamma.market import Market

from open_trader import polymarket_trading
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


UTC = timezone.utc


def _adapter(store, monkeypatch, *, policy=None, absent_ids=(), titles=None, clock=None,
             read_gate=None, public_closes=None):
    read_at = datetime(2026, 10, 15, 4, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = read_at if clock is None else clock[0]
            return current if tz is None else current.astimezone(tz)

    class OfflinePublic:
        def list_markets(self, *, condition_ids, page_size, closed=None):
            assert page_size == 100
            if read_gate is not None and read_gate[0] in condition_ids:
                read_gate[1].set()
                assert read_gate[2].wait(10), "independent API-gate watchdog expired"
            return tuple(Market.parse_response({
                "id": cid[-4:], "conditionId": cid,
                "slug": "metadata-349", "question": (titles or {}).get(cid, "Metadata snapshot"),
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["349001", "349002"]', "events": [],
            }) for cid in condition_ids if cid not in absent_ids)

        def close(self):
            if public_closes is not None:
                public_closes.append(self)

    monkeypatch.setattr(polymarket_trading, "datetime", Clock)
    kwargs = {} if policy is None else {"metadata_writer_policy": policy}
    return PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40), client=object(),
        public_client_factory=OfflinePublic, metadata_cache=store, **kwargs,
    )


def _wait_status(adapter, predicate):
    deadline = time.monotonic() + 5
    wake = threading.Event()
    while True:
        status = adapter.lp_metadata_persistence_status()
        if predicate(status):
            return status
        assert time.monotonic() < deadline, status
        wake.wait(0.005)


def _prime(adapter):
    adapter.lp_market_metadata(("0x" + "f" * 64,))
    _wait_status(adapter, lambda s: s["saved_entries"] == 1)


@contextmanager
def _blocked_database(store, monkeypatch, *, batch_sizes=None):
    started, finished = threading.Event(), threading.Event()
    real_connect = sqlite3.connect
    blocker = real_connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")

    class ObservedConnection(sqlite3.Connection):
        tracked = False

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                self.tracked = True
                started.set()
            return super().execute(sql, parameters)

        def close(self):
            try:
                super().close()
            finally:
                if self.tracked:
                    finished.set()

        def executemany(self, sql, parameters):
            if batch_sizes is not None and "lp_market_metadata_cache" in sql:
                batch_sizes.append(len(parameters))
            return super().executemany(sql, parameters)

    def connect(*args, **kwargs):
        if Path(args[0]) == store.path:
            kwargs["factory"] = ObservedConnection
        return real_connect(*args, **kwargs)

    # Keep the external driver observer installed after releasing the lock so
    # subsequent FIFO batches remain observable until pytest teardown.
    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        yield started, finished
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_public_metadata_returns_while_sqlite_cache_write_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_at = datetime(2026, 10, 15, 4, tzinfo=UTC)
    condition_id = "0x" + "a" * 64
    prime_id = "0x" + "b" * 64
    market = Market.parse_response({
        "id": "34901",
        "conditionId": condition_id,
        "slug": "metadata-async-349",
        "question": "Will the metadata cache save finish later?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.50", "0.50"]',
        "clobTokenIds": '["349001", "349002"]',
        "events": [],
    })

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return read_at if tz is None else read_at.astimezone(tz)

    class OfflinePublic:
        def list_markets(self, *, condition_ids, page_size, closed=None):
            assert page_size == 100
            if tuple(condition_ids) == (prime_id,):
                raise RuntimeError("offline priming failure")
            assert tuple(condition_ids) == (condition_id,)
            return (market,)

        def close(self):
            pass

    monkeypatch.setattr(polymarket_trading, "datetime", Clock)
    store = PredictionArbitrageStore(tmp_path)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(), public_client_factory=OfflinePublic, metadata_cache=store,
    )
    # Schema/warm restore is synchronous; physical pruning is native writer
    # work. Prime without saving a row, then prove that maintenance completed.
    prime = adapter.lp_market_metadata_batch((prime_id,))
    assert prime["state"] == "unknown"
    assert prime["confirmed_absent_ids"] == ()
    assert prime["failed_ids"] == {prime_id: "market_read_RuntimeError"}
    prime_status = _wait_status(adapter, lambda status:
        status["prune_completed_transactions"] == 1
        and status["prune_pending"] == status["prune_inflight"] == 0
        and status["pending_entries"] == status["inflight_entries"] == 0)
    assert prime_status["prune_failed"] == prime_status["prune_unknown_outcome"] == 0
    assert prime_status["saved_entries"] == 0
    (tmp_path / "prime-maintenance-proof.json").write_text(__import__('json').dumps(prime_status))

    write_started = threading.Event()
    write_finished = threading.Event()
    committed = threading.Event()
    real_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        tracked_write = False

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                self.tracked_write = True
                write_started.set()
            result = super().execute(sql, parameters)
            if sql == "COMMIT" and self.tracked_write:
                committed.set()
            return result

        def close(self):
            try:
                super().close()
            finally:
                if self.tracked_write:
                    write_finished.set()

    def observed_connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        return real_connect(*args, **kwargs)

    # A second real connection holds SQLite's writer lock. The driver events
    # distinguish an unfinished write from a timed-out/failed save that returned.
    blocker = real_connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    result: Future = Future()
    caller_done = threading.Event()

    def read_public():
        try:
            result.set_result(adapter.lp_market_metadata_batch((condition_id,)))
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            caller_done.set()

    caller = threading.Thread(target=read_public, name="metadata-349-public-test")
    try:
        caller.start()
        assert write_started.wait(2), "public read did not reach the real SQLite write"
        returned_while_blocked = caller_done.wait(2)
        assert not write_finished.is_set(), "SQLite save already finished or timed out"
        assert not committed.is_set()
        assert blocker.execute(
            "SELECT COUNT(*) FROM lp_market_metadata_cache"
        ).fetchone()[0] == 0
        assert returned_while_blocked, (
            "public metadata read waited for the unfinished SQLite cache save"
        )
        facts = result.result(timeout=0)
        assert facts["state"] == "known"
        assert facts["checked_at"] == read_at
        assert facts["confirmed_absent_ids"] == ()
        assert facts["failed_ids"] == {}
        assert facts["failure_facts"] == {}
        assert facts["deferred_ids"] == ()
        assert set(facts["markets"]) == {condition_id}
        payload = facts["markets"][condition_id]
        assert payload["condition_id"] == condition_id
        assert payload["market_id"] == "34901"
        assert payload["metadata_checked_at"] == read_at
        assert payload["outcomes"] == {
            "yes": {"label": "Yes", "token_id": "349001"},
            "no": {"label": "No", "token_id": "349002"},
        }
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        caller.join(timeout=5)
        adapter.close()
        assert not caller.is_alive(), "public reader did not finish after lock release"
        assert write_finished.wait(5), "actual SQLite write was not reclaimed"


def test_public_metadata_write_failure_is_visible_and_next_save_can_succeed(
    tmp_path, monkeypatch, caplog,
):
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    # Public Store schema/prune setup completes before fault injection.
    store.lp_metadata_cache_prune()
    rejected_id, saved_id = "0x" + "c" * 64, "0x" + "d" * 64
    adapter = _adapter(store, monkeypatch)
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER reject_metadata BEFORE INSERT ON
            lp_market_metadata_cache BEGIN SELECT RAISE(ABORT, 'private-349-secret'); END""")
    try:
        first = adapter.lp_market_metadata_batch((rejected_id,))
        assert first["state"] == "known"
        assert first["markets"][rejected_id]["market_id"] == "cccc"
        failed = _wait_status(adapter, lambda s: s["failed_entries"] == 1)
        assert failed["saved_entries"] == 0
        assert failed["unknown_outcome_entries"] == 1
        assert failed["pending_entries"] == failed["inflight_entries"] == 0
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM lp_market_metadata_cache").fetchone()[0] == 0
            connection.execute("DROP TRIGGER reject_metadata")
        second = adapter.lp_market_metadata((saved_id,))
        assert second[saved_id]["market_id"] == "dddd"
        settled = _wait_status(adapter, lambda s: s["saved_entries"] == 1)
        assert settled["failed_entries"] == 1
        rows = store.lp_metadata_cache_entries(now=datetime(2026, 10, 15, 4, tzinfo=UTC))
        assert set(rows) == {saved_id}  # Failed snapshot was never replayed.
        messages = [r for r in caplog.records if r.name == "open_trader.lp_metadata_writer"]
        assert len(messages) == 1
        assert "IntegrityError" in messages[0].getMessage()
        assert "private-349-secret" not in messages[0].getMessage()
        assert messages[0].exc_info is None
    finally:
        adapter.close()


@pytest.mark.parametrize("capacity", ("entries", "bytes"))
def test_public_metadata_fifo_queue_evicts_oldest_pending_without_key_merge(
    tmp_path, monkeypatch, capacity,
):
    store = PredictionArbitrageStore(tmp_path)
    ids = ["0x" + c * 64 for c in "abcd"]
    padding = "x" * 10_000
    titles = {cid: title + padding for cid, title in zip(ids, ("A", "B1", "C", "D"))}
    # Each literal snapshot fits between 10k and 11.25k encoded bytes.
    # 45k therefore holds four, never five; 3000 entries cannot bind here.
    policy = {"max_entries": 4 if capacity == "entries" else 3000,
              "max_encoded_bytes": 1_000_000 if capacity == "entries" else 45_000,
              "batch_rows": 2}
    adapter = _adapter(store, monkeypatch, policy=policy, titles=titles)
    try:
        _prime(adapter)
        with sqlite3.connect(store.path) as connection:
            connection.executescript("""CREATE TABLE cache_audit(title TEXT);
                CREATE TRIGGER cache_audit AFTER INSERT ON lp_market_metadata_cache
                BEGIN INSERT INTO cache_audit VALUES(json_extract(NEW.payload,'$.market_title')); END;
                CREATE TRIGGER cache_audit_update AFTER UPDATE ON lp_market_metadata_cache
                BEGIN INSERT INTO cache_audit VALUES(json_extract(NEW.payload,'$.market_title')); END;""")
        batch_sizes = []
        with _blocked_database(store, monkeypatch, batch_sizes=batch_sizes) as (started, finished):
            assert adapter.lp_market_metadata((ids[0],))[ids[0]]["market_title"] == "A" + padding
            assert started.wait(2)
            for cid, title in ((ids[1], "B1"), (ids[2], "C"),
                               (ids[1], "B2"), (ids[1], "B3"), (ids[3], "D")):
                titles[cid] = title + padding
                assert adapter.lp_market_metadata_fresh((cid,))[cid]["market_title"] == title + padding
            status = adapter.lp_metadata_persistence_status()
            assert status["inflight_entries"] == 1
            assert status["pending_entries"] == 3  # B2 and B3 each retain a FIFO slot.
            assert status["dropped_overflow"] == 2
            assert 10_000 < status["inflight_bytes"] < 11_250
            assert 30_000 < status["pending_bytes"] < 33_750
            assert status["pending_bytes"] + status["inflight_bytes"] <= policy["max_encoded_bytes"]
            assert not finished.is_set()
        settled = _wait_status(adapter, lambda s: s["saved_entries"] == 5)
        assert settled["pending_entries"] == settled["inflight_entries"] == 0
        assert sum(batch_sizes) == 4
        assert all(1 <= size <= 2 for size in batch_sizes)
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT title FROM cache_audit ORDER BY rowid").fetchall() == [
                ("A" + padding,), ("B2" + padding,), ("B3" + padding,), ("D" + padding,),
            ]
        saved = store.lp_metadata_cache_entries(now=datetime(2026, 10, 15, 4, tzinfo=UTC))
        assert ids[2] not in saved
        assert saved[ids[1]][1]["market_title"] == "B3" + padding
    finally:
        adapter.close()


def test_public_metadata_oversize_snapshot_is_dropped_without_losing_result(
    tmp_path, monkeypatch,
):
    store = PredictionArbitrageStore(tmp_path)
    large_id, small_id = "0x" + "a" * 64, "0x" + "b" * 64
    title = "large snapshot " + "x" * 5000
    adapter = _adapter(store, monkeypatch,
                       policy={"max_entries": 4, "max_encoded_bytes": 2000},
                       titles={large_id: title})
    try:
        result = adapter.lp_market_metadata_batch((large_id,))
        assert result["state"] == "known"
        assert result["markets"][large_id]["market_title"] == title
        status = adapter.lp_metadata_persistence_status()
        assert status["dropped_oversize"] == 1
        assert status["pending_entries"] == status["inflight_entries"] == 0
        assert status["pending_bytes"] == status["inflight_bytes"] == 0
        assert adapter.lp_market_metadata((small_id,))[small_id]["market_id"] == "bbbb"
        _wait_status(adapter, lambda s: s["saved_entries"] == 1)
        saved = store.lp_metadata_cache_entries(now=datetime(2026, 10, 15, 4, tzinfo=UTC))
        assert set(saved) == {small_id}
    finally:
        adapter.close()


def test_late_cache_batch_cannot_erase_or_mutate_newer_public_facts(
    tmp_path, monkeypatch,
):
    store = PredictionArbitrageStore(tmp_path)
    cid = "0x" + "a" * 64
    first_at = datetime(2026, 10, 15, 4, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=1)
    clock = [first_at]
    titles = {cid: "A"}
    adapter = _adapter(store, monkeypatch, clock=clock, titles=titles)
    try:
        _prime(adapter)
        with _blocked_database(store, monkeypatch) as (started, finished):
            old = adapter.lp_market_metadata((cid,))[cid]
            assert old["market_title"] == "A"
            assert started.wait(2)
            titles[cid] = "B"
            clock[0] = second_at
            new = adapter.lp_market_metadata_fresh((cid,))[cid]
            assert new["market_title"] == "B"
            assert new["metadata_checked_at"] == second_at
            old["market_title"] = "mutated A"
            old["outcomes"]["yes"]["token_id"] = "mutated-token"
            new["outcomes"]["yes"]["token_id"] = "mutated B caller"
            assert adapter.lp_metadata_persistence_status()["pending_entries"] == 1
            assert not finished.is_set()
        settled = _wait_status(adapter, lambda s: s["saved_entries"] == 3)
        assert settled["pending_entries"] == settled["inflight_entries"] == 0
        current = adapter.lp_market_metadata((cid,))[cid]
        assert current["market_title"] == "B"
        assert current["metadata_checked_at"] == second_at
        assert current["outcomes"]["yes"]["token_id"] == "349001"
        persisted = store.lp_metadata_cache_entries(now=second_at)[cid]
        assert persisted[0] == second_at.timestamp() + 43200
        assert persisted[1]["market_title"] == "B"
        assert persisted[1]["metadata_checked_at"] == "2026-10-15T04:01:00.000000Z"
        assert persisted[1]["outcomes"]["yes"]["token_id"] == "349001"
        assert old["metadata_checked_at"] == first_at
    finally:
        adapter.close()


def test_completed_background_save_restores_original_ttl_after_restart(
    tmp_path, monkeypatch,
):
    store = PredictionArbitrageStore(tmp_path)
    positive, negative = "0x" + "a" * 64, "0x" + "b" * 64
    read_at = datetime(2026, 10, 15, 4, tzinfo=UTC)
    clock = [read_at]
    adapter = _adapter(store, monkeypatch, absent_ids=(negative,), clock=clock)
    restarted = None
    requests = []

    class OfflineGuard:
        def list_markets(self, *, condition_ids, **kwargs):
            requests.append(tuple(condition_ids))
            raise RuntimeError("offline restart guard")

        def close(self):
            pass

    try:
        fetched = adapter.lp_market_metadata_batch((positive, negative))
        assert fetched["state"] == "known"
        assert fetched["confirmed_absent_ids"] == (negative,)
        assert fetched["markets"][positive]["metadata_checked_at"] == read_at
        _wait_status(adapter, lambda s: s["saved_entries"] == 2)
        persisted = store.lp_metadata_cache_entries(now=read_at)
        assert persisted[positive][0] == read_at.timestamp() + 43200
        assert persisted[negative] == (read_at.timestamp() + 3600, None)
        adapter.close()  # Completion was observed before close; close is not a flush.
        assert adapter.lp_metadata_persistence_status()["thread_alive"] is False
        clock[0] = read_at + timedelta(seconds=3599)
        restarted = PolymarketTradingClient(
            adapter.config, client=object(), public_client_factory=OfflineGuard,
            metadata_cache=store,
        )
        warm = restarted.lp_market_metadata_batch((positive, negative))
        assert warm["state"] == "known"
        assert warm["confirmed_absent_ids"] == (negative,)
        assert warm["markets"][positive]["metadata_checked_at"] == "2026-10-15T04:00:00.000000Z"
        assert requests == []
        clock[0] = read_at + timedelta(seconds=3600)
        expired_negative = restarted.lp_market_metadata_batch((positive, negative))
        assert set(expired_negative["markets"]) == {positive}
        assert expired_negative["confirmed_absent_ids"] == ()
        assert expired_negative["failed_ids"] == {negative: "market_read_RuntimeError"}
        assert requests == [(negative,)]
        clock[0] = read_at + timedelta(seconds=43200)
        expired_positive = restarted.lp_market_metadata_batch((positive,))
        assert expired_positive["failed_ids"] == {positive: "market_read_RuntimeError"}
        assert expired_positive["markets"][positive]["metadata_checked_at"] == "2026-10-15T04:00:00.000000Z"
        assert requests == [(negative,), (positive,)]
    finally:
        adapter.close()
        if restarted is not None:
            restarted.close()


@pytest.mark.parametrize("active_producers", (False, True))
def test_runtime_stop_retains_owner_until_actual_metadata_writer_exit(
    tmp_path, monkeypatch, active_producers, request,
):
    import json, shutil, subprocess
    from tests.lp_d2_service_harness import ServiceChild
    class MetadataChild(ServiceChild):
        def launch(self,scenario,*,expected_running=None):
            # Only this scenario copies its assertion helper into the actual
            # selected clean release before the real producer/verifier runs.
            shutil.copyfile(Path(__file__),self.release_root/'tests'/Path(__file__).name)
            for args in (['add','tests/'+Path(__file__).name],
                ['-c','user.name=CI metadata fixture','-c','user.email=fixture@invalid','commit','-qm','actual metadata assertion helper']):
                subprocess.run(['git','-C',str(self.release_root),*args],check=True,capture_output=True)
            self.sha=subprocess.check_output(['git','-C',str(self.release_root),'rev-parse','HEAD'],text=True).strip()
            return super().launch(scenario,expected_running=expected_running)
    scenario='consumer-ci-metadata-active' if active_producers else 'consumer-ci-metadata-idle'
    with MetadataChild(tmp_path,scenario=scenario) as child:
        child.release('metadata');child.start(expected_running=None)
        child.wait('ci-metadata-assertions-passed')
        # Helper assertions passed and cleanup released the claim; the harness
        # readiness RuntimeError now exits 1 instead of the claimed-failure 78.
        assert child.wait_exit()==1,child.output.read_text()
        result=json.loads(child.marker('ci-metadata-observations.json').read_text())
        assert result['state']=='STOPPED' and result['production_owner'] is False
        assert result['active_producers'] is active_producers
        assert child.actual_posts()==0
        child.assert_owner_available()


def _exercise_actual_runtime_metadata_stop(runtime, adapter, monkeypatch, active_producers, real_connect):
    """Original writer/producer/native assertions against the actual claimed Runtime."""
    import asyncio
    from types import SimpleNamespace
    from open_trader import prediction_runtime as runtime_module
    from open_trader.polymarket_monitor import PolymarketMonitor
    from open_trader.predict_cross_venue import PredictCrossVenueMonitor
    # Local D2 admission stays RUNNING despite unavailable financial facts and
    # the persisted incident; the Scheduler owns API reads and keeps BUYs gated.
    with pytest.raises(OSError,match='offline account financial facts unavailable'):
        adapter.lp_reader_source_port().read('balance',SimpleNamespace(account=adapter.config.wallet_address))
    assert runtime.state == "RUNNING"
    assert runtime.production_owner is True
    state = runtime.lp_auto_state()
    assert state["new_capacity_usd"] is None
    assert state["scheduler_running"] is True
    store = runtime.store
    assert isinstance(store, PredictionArbitrageStore)
    cross = monitor = monitor_thread = None
    source_started, source_cancelled = threading.Event(), threading.Event()
    monitor_started, monitor_closed = threading.Event(), threading.Event()
    producer_io = []
    if active_producers:
        class OfflinePredictSource:
            async def list_open_markets(self):
                producer_io.append("predict-markets")
                source_started.set()
                try:
                    await asyncio.Future()
                finally:
                    source_cancelled.set()

            def snapshot(self):
                return {"rest": "ready", "ws": "ready"}

        class OfflineStream:
            async def __anext__(self):
                monitor_started.set()
                await asyncio.Future()

            async def close(self):
                pass

        class OfflineMonitorPublic:
            async def list_events(self, **kwargs):
                producer_io.append("polymarket-events")
                return []

            def subscribe(self, spec):
                producer_io.append("polymarket-subscribe")
                return OfflineStream()

            async def close(self):
                monitor_closed.set()

        def offline_http(*args, **kwargs):
            raise RuntimeError("offline account HTTP boundary")

        # Compose actual production monitor lifecycles on this owned runtime.
        # No stop implementation, worker, cleanup, or trading result is mocked.
        # The separate real adapter keeps monitor readiness HTTP fully offline.
        monitor_trading = PolymarketTradingClient(
            adapter.config, client=object(), urlopen_fn=offline_http,
        )
        monitor = PolymarketMonitor(
            store=store, trading=monitor_trading,
            public_client_factory=OfflineMonitorPublic,
        )
        monitor.set_cross_venue_tokens(("offline-monitor-token",))
        runtime.monitor = monitor
        monitor.start()
        monitor_thread = monitor._thread
        assert monitor_started.wait(2), "real monitor did not begin offline stream work"
        cross_monitor = PredictCrossVenueMonitor(
            predict_source=OfflinePredictSource(), polymarket_monitor=monitor,
            validator=object(), gamma_lookup=lambda *args, **kwargs: (),
        )
        cross = runtime_module._CrossVenueRuntime(cross_monitor)
        runtime._cross_runtime = cross
        cross.start()
        assert source_started.wait(2), "real cross producer did not begin offline I/O"
        assert cross.thread_alive
        assert monitor_thread is not None and monitor_thread.is_alive()
    _prime(adapter)
    inflight_id, pending_id = "0x" + "a" * 64, "0x" + "b" * 64
    at_commit, release_commit = threading.Event(), threading.Event()

    class CommitGate(sqlite3.Connection):
        metadata_write = False

        def executemany(self, sql, parameters):
            if "lp_market_metadata_cache" in sql:
                self.metadata_write = True
            return super().executemany(sql, parameters)

        def execute(self, sql, parameters=()):
            if sql == "COMMIT" and self.metadata_write:
                at_commit.set()
                assert release_commit.wait(20), "independent commit-gate watchdog expired"
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        if Path(args[0]) == store.path:
            kwargs["factory"] = CommitGate
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    stop_errors = []
    stop_done = threading.Event()

    def stop_runtime():
        try:
            runtime.stop()
        except BaseException as exc:
            stop_errors.append(exc)
        finally:
            stop_done.set()

    stopper = threading.Thread(target=stop_runtime, name="metadata-349-runtime-stop")
    try:
        assert adapter.lp_market_metadata((inflight_id,))[inflight_id]["market_id"] == "aaaa"
        assert at_commit.wait(2)
        assert adapter.lp_market_metadata((pending_id,))[pending_id]["market_id"] == "bbbb"
        assert adapter.lp_metadata_persistence_status()["pending_entries"] == 1
        stopper.start()
        status = _wait_status(adapter, lambda s: s["dropped_shutdown"] == 1)
        assert status["pending_entries"] == 0
        assert status["inflight_entries"] == 1
        if active_producers:
            # Producer stop must proceed while the actual metadata COMMIT is
            # still held, rather than waiting out its five-second join first.
            assert source_cancelled.wait(2), "metadata join prevented real cross producer shutdown"
            assert monitor_closed.wait(2), "metadata join prevented real monitor shutdown"
            assert not release_commit.is_set()
        assert stop_done.wait(7), "runtime stop exceeded its writer join watchdog"
        assert len(stop_errors) == 1
        assert isinstance(stop_errors[0], RuntimeError)
        assert runtime.state == "STOPPING"
        assert runtime.store is store
        assert runtime._prediction_trading is adapter
        assert runtime.production_owner is True
        contender = runtime_module._RuntimeOwnershipLock(store.path.parent / "runtime.lock")
        with pytest.raises(runtime_module.PredictionRuntimeOwnershipError):
            contender.acquire()
        assert adapter.lp_metadata_persistence_status()["thread_alive"] is True
        if active_producers:
            assert cross is not None and not cross.thread_alive
            assert monitor_thread is not None and not monitor_thread.is_alive()
            finished_io = tuple(producer_io)
            assert cross.refresh_opportunity("late-after-stop") is None
            assert tuple(producer_io) == finished_io
            assert finished_io.count("predict-markets") == 1
            assert finished_io.count("polymarket-subscribe") == 1
        release_commit.set()
        _wait_status(adapter, lambda s: not s["thread_alive"])
        runtime.stop()
        assert runtime.state == "STOPPED"
        assert runtime.production_owner is False
        assert runtime._prediction_trading is None
        # The real Store has per-action connections and no close method;
        # runtime keeps that inert object under its existing cleanup contract.
        assert runtime.store is store
        assert adapter.lp_metadata_persistence_status()["thread_alive"] is False
        saved = store.lp_metadata_cache_entries(now=datetime(2026, 10, 15, 4, tzinfo=UTC))
        assert inflight_id in saved
        assert pending_id not in saved
        contender.acquire()
        contender.release()
    finally:
        release_commit.set()
        if stopper.ident is not None:
            stopper.join(timeout=7)
        runtime.stop()
        adapter.close()
        if cross is not None:
            cross.stop()
        if monitor is not None:
            monitor.stop()
            monitor_trading.close()
        assert not stopper.is_alive()


def test_metadata_writer_close_is_idempotent_and_rejects_late_persistence(
    tmp_path, monkeypatch,
):
    store = PredictionArbitrageStore(tmp_path)
    cid = "0x" + "a" * 64
    api_started, api_release = threading.Event(), threading.Event()
    closes = []
    adapter = _adapter(store, monkeypatch, read_gate=(cid, api_started, api_release),
                       public_closes=closes)
    result = Future()

    def read():
        try:
            result.set_result(adapter.lp_market_metadata((cid,)))
        except BaseException as exc:
            result.set_exception(exc)

    reader = threading.Thread(target=read, name="metadata-349-late-public-read")
    try:
        _prime(adapter)
        assert len(closes) == 1
        with pytest.raises(RuntimeError, match="before first use"):
            adapter.attach_metadata_cache(None)
        reader.start()
        assert api_started.wait(2)
        adapter.close()
        stopped = adapter.lp_metadata_persistence_status()
        assert stopped["state"] == "STOPPED"
        assert stopped["thread_alive"] is False
        api_release.set()
        assert result.result(timeout=3)[cid]["market_id"] == "aaaa"
        reader.join(timeout=3)
        late = adapter.lp_metadata_persistence_status()
        assert late["dropped_closed"] == 1
        assert late["saved_entries"] == 1
        assert late["pending_entries"] == late["inflight_entries"] == 0
        adapter.close()
        adapter.stop_lp_metadata_writer(timeout=0)
        assert adapter.lp_metadata_persistence_status() == late
        assert len(closes) == 2
        assert closes[0] is not closes[1]
        saved = store.lp_metadata_cache_entries(now=datetime(2026, 10, 15, 4, tzinfo=UTC))
        assert set(saved) == {"0x" + "f" * 64}
        # Closing before the first save must not start a new writer either.
        unused = _adapter(store, monkeypatch)
        unused.close()
        try:
            assert unused.lp_market_metadata((cid,))[cid]["market_id"] == "aaaa"
            unused_status = unused.lp_metadata_persistence_status()
            assert unused_status["thread_alive"] is False
            assert unused_status["dropped_closed"] == 1
        finally:
            unused.close()
        no_store = _adapter(None, monkeypatch)
        try:
            no_store.lp_market_metadata((cid,))
            assert no_store.lp_metadata_persistence_status()["state"] == "NOT_STARTED"
        finally:
            no_store.close()
    finally:
        api_release.set()
        if reader.ident is not None:
            reader.join(timeout=3)
        adapter.close()
        assert not reader.is_alive()


def test_exclusion_reads_keep_failure_and_missing_semantics(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    positive, absent, unknown = ("0x" + char * 64 for char in "abc")
    adapter = _adapter(store, monkeypatch, absent_ids=(absent,))
    warm_fallback = None
    read_at = datetime(2026, 10, 15, 4, tzinfo=UTC)
    try:
        first = adapter.lp_market_metadata_batch((positive, absent))
        assert first["state"] == "known"
        assert first["confirmed_absent_ids"] == (absent,)
        _wait_status(adapter, lambda s: s["saved_entries"] == 2)
        adapter.configure_lp_candidate_exclusions(True)
        denied = {"lp_market_exclusions"}
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            if Path(args[0]) == store.path:
                selected = [False]

                def authorizer(action, table, column, database, source):
                    if action == sqlite3.SQLITE_SELECT:
                        selected[0] = True
                    return (sqlite3.SQLITE_DENY if selected[0] and action == sqlite3.SQLITE_READ
                            and table in denied else sqlite3.SQLITE_OK)

                connection.set_authorizer(authorizer)
            return connection

        monkeypatch.setattr(sqlite3, "connect", connect)
        # Cached positive and proved absence remain exactly their known facts.
        cached = adapter.lp_market_metadata_batch((positive, absent))
        assert cached["state"] == "known"
        assert cached["markets"][positive] == first["markets"][positive]
        assert cached["confirmed_absent_ids"] == (absent,)
        for public_read in (adapter.lp_market_metadata, adapter.lp_market_metadata_batch,
                            adapter.lp_market_metadata_fresh):
            with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
                public_read((unknown,))
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            adapter.lp_market_metadata_fresh((absent,))
        assert adapter.lp_metadata_persistence_status()["saved_entries"] == 2
        adapter.configure_lp_candidate_exclusions(False)
        disabled = adapter.lp_market_metadata_fresh((absent,), include_diagnostics=True)
        assert disabled["state"] == "known"
        assert disabled["confirmed_absent_ids"] == (absent,)
        assert disabled["failed_ids"] == {}
        _wait_status(adapter, lambda s: s["saved_entries"] == 3)
        # A denied warm-cache SELECT degrades to the documented public API
        # fallback. It never proves absence or bypasses exclusion failures.
        denied.clear()
        denied.add("lp_market_metadata_cache")
        closes = []
        warm_fallback = _adapter(store, monkeypatch, public_closes=closes)
        fallback = warm_fallback.lp_market_metadata_batch((positive,))
        assert fallback["state"] == "known"
        assert fallback["markets"][positive]["metadata_checked_at"] == read_at
        assert fallback["confirmed_absent_ids"] == ()
        assert fallback["failed_ids"] == {}
        assert len(closes) == 1
        _wait_status(warm_fallback, lambda s: s["saved_entries"] == 1)
        denied.clear()
        persisted = store.lp_metadata_cache_entries(now=read_at)
        assert set(persisted) == {positive, absent}
        assert persisted[absent] == (read_at.timestamp() + 3600, None)
    finally:
        adapter.close()
        if warm_fallback is not None:
            warm_fallback.close()
