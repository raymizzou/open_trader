"""Issue #64 repair round 2: queue-head driver findings (P1/P2/P3), one
red->green case per review finding, all through the public driver seam
(``NLegOrderQueueDriver.tick``) with injected books/source/trading factories.

Fixtures come from the approved Slice-5 harness in
``test_prediction_n_leg_confirm`` (caps-configured store, frozen fixture
solution enqueued with its bound #74 proof).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from test_prediction_n_leg_confirm import (
    AS_OF,
    _FakeTrading,
    _e2e_driver,
    _e2e_source_factory,
    _enqueued_e2e_store,
    _fixture_books_snapshot,
    _recon_context_factory,
)


# ---------------------------------------------------------------------------
# Issue #65 A2 drills: observation tests over existing behavior, all through
# the public driver seam with fake clients/fixtures. No behavior change.
# ---------------------------------------------------------------------------


def _active_batch_count(tmp_path: Path) -> int:
    """Read-only count of batches in an active lifecycle state."""
    import sqlite3

    from test_prediction_n_leg_confirm import _e2e_db

    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM n_leg_batches"
            " WHERE state IN ('ACTIVE', 'AWAITING_RECONCILIATION', 'INCIDENT')"
        ).fetchone()[0]


def test_d1_post_submit_crash_real_restart_reconciles_single_batch(
    tmp_path: Path,
) -> None:
    import sqlite3

    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from test_prediction_n_leg_confirm import _e2e_db

    # Legs submitted and filled, receipts folded, reconciliation NOT yet
    # run (the drill's crash point): the batch is AWAITING_RECONCILIATION.
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    driver = _e2e_driver(store, trading, base_source, recon=False)

    first = driver.tick(now=AS_OF)

    batch_id = str(first["submitted"])
    assert store.n_leg_batch(batch_id)["state"] == "AWAITING_RECONCILIATION"
    assert _active_batch_count(tmp_path) == 1

    # TRUE restart: the store holds no open sqlite connection between
    # actions, so dropping the objects closes every connection; disk
    # durability is proven by a raw read, then everything is re-constructed
    # from the SAME database file with zero shared in-memory state.
    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        assert connection.execute(
            "SELECT state FROM n_leg_batches WHERE execution_batch_id=?",
            (batch_id,),
        ).fetchone()[0] == "AWAITING_RECONCILIATION"
    del store, driver

    store2 = PredictionArbitrageStore(tmp_path / "data")

    # The restarted store sees the batch state (:33 semantics).
    assert store2.n_leg_batch(batch_id)["state"] == "AWAITING_RECONCILIATION"
    assert _active_batch_count(tmp_path) == 1

    # Within the window the restarted watch reports the ordinary visible
    # batch-active skip, still owning exactly one active batch.
    watch = _e2e_driver(store2, _FakeTrading([]), base_source, recon=False)
    assert watch.tick(now=AS_OF + timedelta(seconds=30)) == {
        "skipped": "EXECUTION_BATCH_ACTIVE"
    }
    assert _active_batch_count(tmp_path) == 1

    # The production reconciliation factory completes the batch after the
    # restart: RECONCILED_FULL, ownership released, exactly one batch ever.
    from open_trader.prediction_n_leg_driver import (
        trading_reconciliation_context_factory,
    )

    venue = _VenueAccount(
        positions=(
            {"token_id": "action-a", "size": "10"},
            {"token_id": "action-b", "size": "10"},
        )
    )
    closer = _watch_driver(
        store2,
        _FakeTrading([]),
        base_source,
        trading_reconciliation_context_factory(store2, venue),
    )
    assert closer.tick(now=AS_OF + timedelta(seconds=31)) == {
        "reconciled": batch_id
    }
    assert str(store2.n_leg_batch(batch_id)["state"]).startswith("RECONCILED")
    assert _active_batch_count(tmp_path) == 0
    assert store2.n_leg_control()["active_batch_id"] is None
    assert store2.n_leg_control()["total_unsettled_capital_units"] == 1020
    stored = next(
        r
        for r in store2.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    assert stored["state"] == "SUBMITTED"


def test_d2_two_component_fifo_head_leaves_and_second_batch_waits_for_gate(
    tmp_path: Path,
) -> None:
    from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver
    from open_trader.prediction_n_leg_driver import (
        trading_reconciliation_context_factory,
    )
    from test_prediction_n_leg_canary_report import _bound_proof
    from test_prediction_n_leg_confirm import (
        COMPONENT_ID,
        _confirm,
        _solution_entry,
    )

    # Two components enqueue cross-component FIFO: the fixture family is the
    # head, another family waits behind it.
    store, _row, base_source = _enqueued_e2e_store(tmp_path)
    entry_b = _solution_entry(component_id="component:other")
    _confirm(
        store,
        [entry_b],
        component_id="component:other",
        idempotency_key="d2-b",
        partial_fill_proof=_bound_proof(store, entry_b, base_source),
    )
    pending = [r for r in store.n_leg_requests() if r["state"] == "PENDING"]
    assert [r["component_id"] for r in pending] == [
        COMPONENT_ID,
        "component:other",
    ]

    def books(cid):
        # The head component rotated out of qualification: no current plan.
        return None if cid == COMPONENT_ID else _fixture_books_snapshot(AS_OF)

    untouched = _FakeTrading([])
    driver = NLegOrderQueueDriver(
        store,
        books_provider=books,
        source_factory=_e2e_source_factory(base_source),
        trading=untouched,
    )

    # The invalid head leaves the queue fail-closed with zero side effects.
    first = driver.tick(now=AS_OF)
    assert first["abandoned"] == "BOOK_UNAVAILABLE"
    assert untouched.calls == []
    remaining = [
        r["component_id"]
        for r in store.n_leg_requests()
        if r["state"] == "PENDING"
    ]
    assert remaining == ["component:other"]
    assert store.n_leg_request_head()["component_id"] == "component:other"

    # A third family enqueues while the execution gate is free.
    entry_c = _solution_entry(component_id="component:third")
    _confirm(
        store,
        [entry_c],
        component_id="component:third",
        idempotency_key="d2-c",
        partial_fill_proof=_bound_proof(store, entry_c, base_source),
    )

    # The second family's batch is admitted and submitted; the gate closes.
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([dict(filled), dict(filled), dict(filled), dict(filled)])
    driver = NLegOrderQueueDriver(
        store,
        books_provider=lambda cid: _fixture_books_snapshot(AS_OF),
        source_factory=_e2e_source_factory(base_source),
        trading=trading,
    )
    second = driver.tick(now=AS_OF + timedelta(seconds=1))
    batch_b = str(second["submitted"])
    assert store.n_leg_batch(batch_b)["state"] == "AWAITING_RECONCILIATION"
    assert store.n_leg_control()["active_batch_id"] == batch_b
    assert _active_batch_count(tmp_path) == 1

    # The third family's admission MUST WAIT for the first batch's gate:
    # a single active execution batch is the whole admission surface.
    waiting = driver.tick(now=AS_OF + timedelta(seconds=2))
    assert waiting == {"skipped": "EXECUTION_BATCH_ACTIVE"}
    assert store.n_leg_control()["active_batch_id"] == batch_b
    assert _active_batch_count(tmp_path) == 1

    # The gate releases with the reconciliation factory completing batch B.
    venue = _VenueAccount(
        positions=(
            {"token_id": "action-a", "size": "10"},
            {"token_id": "action-b", "size": "10"},
        )
    )
    closer = _watch_driver(
        store,
        _FakeTrading([]),
        base_source,
        trading_reconciliation_context_factory(store, venue),
    )
    assert closer.tick(now=AS_OF + timedelta(seconds=3)) == {
        "reconciled": batch_b
    }
    assert store.n_leg_control()["active_batch_id"] is None

    # Only now is the next family's batch admitted — at most one active
    # batch through the whole drill.
    third = driver.tick(now=AS_OF + timedelta(seconds=4))
    batch_c = str(third["submitted"])
    assert batch_c != batch_b
    assert store.n_leg_control()["active_batch_id"] == batch_c
    assert _active_batch_count(tmp_path) == 1


# P1 (review round 2): without a reconciliation factory the driver must never
# skip an all-filled batch silently forever — after
# ``reconciliation_timeout_seconds`` (default 60) the queue row and the tick
# result must carry a visible blocked state with a stable reason.


def test_p1_awaiting_reconciliation_beyond_timeout_is_a_visible_blocked_state(
    tmp_path: Path,
) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    driver = _e2e_driver(store, trading, base_source, recon=False)

    first = driver.tick(now=AS_OF)

    batch_id = str(first["submitted"])
    batch = store.n_leg_batch(batch_id)
    assert batch["state"] == "AWAITING_RECONCILIATION"

    within_timeout = driver.tick(now=AS_OF + timedelta(seconds=30))
    assert within_timeout == {"skipped": "EXECUTION_BATCH_ACTIVE"}

    blocked = driver.tick(now=AS_OF + timedelta(seconds=61))

    assert blocked["blocked"] == "RECONCILIATION_TIMEOUT"
    assert blocked["execution_batch_id"] == batch_id
    stored = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    assert stored["payload"]["reconciliation_overdue"] is True
    assert stored["state"] == "ADMITTED"


# P1 (review round 2): the PRODUCTION reconciliation factory — the one the
# runtime wires from the trading client's account snapshot (balances +
# positions) — must take an all-filled batch to RECONCILED_FULL, release the
# active batch ownership and free the reservation, exactly like the test
# factory pinned by E1.


class _VenueAccount:
    """Duck-typed PolymarketTradingClient account-snapshot surface: fresh
    collateral balance/allowance plus venue positions on every read."""

    def __init__(self, positions: tuple[dict[str, str], ...]) -> None:
        self.p_usd_balance = Decimal("1000")
        self.p_usd_allowance = Decimal("1000")
        self.positions = positions
        self.checked_at = datetime.now(UTC)

    def account_snapshot(self):
        self.checked_at = datetime.now(UTC)
        return self


def test_p1_production_trading_reconciliation_factory_completes_full_batch(
    tmp_path: Path,
) -> None:
    from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver
    from open_trader.prediction_n_leg_driver import (
        trading_reconciliation_context_factory,
    )

    store, row, base_source = _enqueued_e2e_store(tmp_path)
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    venue = _VenueAccount(
        positions=(
            {"token_id": "action-a", "size": "10"},
            {"token_id": "action-b", "size": "10"},
        )
    )
    driver = NLegOrderQueueDriver(
        store,
        books_provider=lambda component_id: _fixture_books_snapshot(AS_OF),
        source_factory=_e2e_source_factory(base_source),
        trading=trading,
        reconciliation_context_factory=trading_reconciliation_context_factory(
            store, venue
        ),
    )

    summary = driver.tick(now=AS_OF)

    batch = store.n_leg_batch(str(summary["submitted"]))
    assert str(batch["state"]).startswith("RECONCILED")
    control = store.n_leg_control()
    assert control["active_batch_id"] is None
    # The ledger keeps the conservative protected bound (510/leg) exactly as
    # the approved E1 hand math pins it.
    assert control["total_unsettled_capital_units"] == 1020
    rows = store.n_leg_requests()
    assert (
        [r["state"] for r in rows if r["request_id"] == row["request_id"]]
        == ["SUBMITTED"]
    )


# P2 (review round 2): a non-ValueError from one leg's submit (or from receipt
# construction) must never escape the fold: the leg books an UNKNOWN receipt
# and the incident stop-the-world path runs — no wedged batch, PENDING cleared.


class _ExplodingTrading:
    """Fake adapter: Exception instances in the script are raised, other
    outcomes are returned (mirrors ``_FakeTrading``'s interface)."""

    def __init__(self, outcomes) -> None:
        self.calls: list[str] = []
        self.outcomes = list(outcomes)

    def submit_n_leg_leg_once(
        self, *, client_order_id, token_id, quantity_lots, max_cost_units, timeout_seconds=15
    ):
        self.calls.append(client_order_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_p2_leg_submit_runtime_error_books_unknown_and_opens_incident(
    tmp_path: Path,
) -> None:
    store, _row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _ExplodingTrading(
        [RuntimeError("venue socket exploded"), {"state": "FILLED", "error_code": None}]
    )
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    batch = store.n_leg_batch(str(summary["submitted"]))
    assert batch["incident"]["reason"] == "UNKNOWN_ORDER_STATE"
    legs = {leg["client_order_id"]: leg for leg in batch["legs"]}
    unknown = [
        leg
        for leg in legs.values()
        if leg["receipt"]["state"] == "UNKNOWN"
    ]
    assert len(unknown) == 1
    assert len(unknown[0]["client_order_id"]) > 0
    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["breaker_open"] is True
    assert all(
        r["state"] != "PENDING" for r in store.n_leg_requests()
    )


# P2 (review round 2): the frozen #51 source books must carry the snapshot
# legs' modeled taker fee (fee_ppm = bps x 100) so charging markets decode
# with fee-bearing economics; fee-free markets stay byte-identical; an
# unmodelable bps figure abandons the build fail-closed.


def _charging_rows() -> dict[str, object]:
    from test_prediction_live_resolver import fee_endpoint, fee_row, row, raw_problem

    charging = {
        "venue": "polymarket",
        "contract_id": "contract-a",
        "yes_token_id": "yes-token-a",
        "no_token_id": "no-token-a",
        "fees_enabled": True,
        "fee_rate": "0.05",  # 500 bps
    }
    base = row("r:a", raw_problem())
    base["endpoints"] = [charging]
    return {"r:a": base}


def test_p2_frozen_source_books_carry_charging_market_fee(tmp_path: Path) -> None:
    from open_trader.prediction_market_solution import cost_slices_from_book
    from open_trader.prediction_live_resolver import USD_UNITS_PER_DOLLAR
    from test_prediction_live_resolver import (
        RecordingMonitor,
        live_book,
        raw_problem,
        resolver,
        valid_selected,
    )

    rows = _charging_rows()
    monitor = RecordingMonitor(
        {
            "yes-token-a": live_book("yes-token-a", price="0.40"),
            "no-token-a": live_book("no-token-a", price="0.40"),
        }
    )
    instance, _server, _catalog = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()
    component_id = "component:contract-a"
    problem = instance._problem_map[component_id]
    snapshot = instance._snapshot_for(valid_selected(rows))
    assert snapshot is not None

    books = instance._frozen_source_books(problem, snapshot)

    assert books is not None
    # 500 bps -> 50,000 ppm on every charging leg of the component.
    assert all(book.fee_ppm == 50_000 for book in books)
    # The frozen bounded cost now includes the fee (#51 accounting, hand
    # math): ask 0.40 -> protected 400,001 (tick 1); per-lot fee
    # ceil(400,001 x 50,000 / 1e6) = 20,001 -> unit cost 420,002.
    action = problem.actions[0]
    book = books[0]
    slices = cost_slices_from_book(
        action,
        book.book,
        fee_ppm=book.fee_ppm,
        tick_units=book.tick_units,
        haircut_ppm=book.haircut_ppm,
        price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
    )
    assert slices[0].incremental_cost_upper_bound_units == 420_002


def test_p2_frozen_source_books_stay_fee_free_and_byte_identical(
    tmp_path: Path,
) -> None:
    from test_prediction_live_resolver import (
        FakeMonitor,
        live_book,
        resolver,
        row,
        raw_problem,
        valid_selected,
    )

    rows = {"r:a": row("r:a", raw_problem())}
    instance, _server, _catalog = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a", price="0.40")}),
    )
    instance._reconcile()
    problem = instance._problem_map["component:contract-a"]
    snapshot = instance._snapshot_for(valid_selected(rows))
    assert snapshot is not None

    books = instance._frozen_source_books(problem, snapshot)

    assert books is not None
    assert all(book.fee_ppm == 0 for book in books)
    assert all(book.tick_units == 1 for book in books)


def test_p2_unmodelable_frozen_fee_bps_abandons_the_build(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage import BookLevel
    from open_trader.prediction_snapshot_scheduler import (
        ComponentSnapshot,
        LegBook,
        SnapshotLeg,
    )
    from test_prediction_live_resolver import FakeMonitor, raw_problem, resolver

    from decimal import Decimal

    instance, _server, _catalog = resolver(
        tmp_path, rows={}, monitor=FakeMonitor()
    )
    problem = raw_problem()
    level = (BookLevel(Decimal("0.40"), Decimal("10")),)
    snapshot = ComponentSnapshot(
        "component:contract-a",
        (
            SnapshotLeg(
                "a-yes",
                LegBook(bids=(), asks=level, taker_fee_bps=None, available=True),
                AS_OF,
                AS_OF,
                1,
            ),
            SnapshotLeg(
                "a-no",
                LegBook(bids=level, asks=(), taker_fee_bps=Decimal("0"), available=True),
                AS_OF,
                AS_OF,
                2,
            ),
        ),
    )

    assert instance._frozen_source_books(problem, snapshot) is None


# P3 (review round 2): a queue row frozen WITHOUT a partial-fill proof must be
# abandoned with the stable literal on its first tick — never a KeyError that
# escapes the abandon path and retries the same head forever.


def test_p3_missing_partial_fill_proof_abandons_with_literal(
    tmp_path: Path,
) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    stored = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    payload = {
        key: value
        for key, value in stored["payload"].items()
        if key != "partial_fill_proof"
    }
    no_proof = store.n_leg_request_enqueue(
        component_id="component:noproof",
        idempotency_key="e2e-noproof",
        payload=payload,
    )
    # make the proof-less row the queue head
    store.n_leg_request_update(
        str(row["request_id"]), state="ABANDONED", abandon_reason="TEST_SETUP"
    )
    trading = _FakeTrading([])
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    assert summary["abandoned"] == "PARTIAL_FILL_PROOF_REQUIRED"
    assert summary["request_id"] == str(no_proof["request_id"])
    stored_row = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == no_proof["request_id"]
    )
    assert stored_row["state"] == "ABANDONED"
    assert stored_row["abandon_reason"] == "PARTIAL_FILL_PROOF_REQUIRED"
    assert trading.calls == []


# P3 (review round 2, ruling 7): the solve-request snapshot's per-leg sequence
# baselines are frozen at request-build time, replayed by solutions(), and the
# confirm freeze stores them in the queue payload — otherwise the preflight
# monotonicity check is dead code.


def test_p3_resolver_freezes_solve_request_sequences(tmp_path: Path) -> None:
    from open_trader.prediction_market_solution import AccountView
    from test_prediction_live_resolver import (
        FakeExecution,
        FakeMonitor,
        live_book,
        resolver,
        row,
        raw_problem,
        valid_selected,
        worker_evidence,
        worker_outcome,
    )

    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _catalog = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._reconcile()
    snapshot = instance._snapshot_for(valid)
    assert snapshot is not None
    expected = {
        leg.leg_id: leg.sequence
        for leg in snapshot.legs
        if type(leg.sequence) is int
    }
    assert expected

    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()

    (entry,) = instance.solutions()
    assert entry["sequences"] == expected


# P3 (review round 2, ruling 8): ``max_leg_submit_seconds`` written to the
# versioned safety config is the driver's per-leg submit timeout — a leg whose
# submission outlives it surfaces as UNKNOWN(timeout) and opens the incident.


class _LaggingTrading:
    """Fake adapter honouring the timeout contract: a submission that outlives
    the driver-requested timeout surfaces as UNKNOWN(timeout)."""

    def __init__(self, lag_seconds: float) -> None:
        self.lag_seconds = lag_seconds
        self.received_timeouts: list[int] = []

    def submit_n_leg_leg_once(
        self, *, client_order_id, token_id, quantity_lots, max_cost_units, timeout_seconds=15
    ):
        self.received_timeouts.append(timeout_seconds)
        if timeout_seconds < self.lag_seconds:
            return {"state": "UNKNOWN", "error_code": "timeout"}
        return {"state": "FILLED", "error_code": None}


def test_p3_max_leg_submit_seconds_config_threaded_to_driver(
    tmp_path: Path,
) -> None:
    from open_trader.prediction_n_leg_mode import n_leg_update_safety_config

    store, _row, base_source = _enqueued_e2e_store(tmp_path)
    # The four caps stay identical, so the admission caps fingerprint and the
    # frozen proof binding are untouched; only the timeout key is new.
    safety = store.n_leg_safety_config_latest()
    config = {
        key: value
        for key, value in safety["config"].items()
        if key != "caps_configured"
    }
    config["max_leg_submit_seconds"] = 2
    n_leg_update_safety_config(
        store,
        config=config,
        base_version=int(safety["version"]),
    )

    trading = _LaggingTrading(lag_seconds=3)
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    assert trading.received_timeouts == [2, 2]
    batch = store.n_leg_batch(str(summary["submitted"]))
    assert batch["incident"]["reason"] == "UNKNOWN_ORDER_STATE"
    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["breaker_open"] is True
    assert all(r["state"] != "PENDING" for r in store.n_leg_requests())


# ---------------------------------------------------------------------------
# Repair round 3 (review round 2 findings), same seam discipline as above.
# ---------------------------------------------------------------------------


# F1 (review round 2, P2): ``complete_reconciliation`` was attempted exactly
# once, on the submit tick — one transient factory ValueError (the typical
# FOK-just-filled / account-snapshot-not-yet-visible
# N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE) wedged the batch in
# AWAITING_RECONCILIATION forever, holding active_batch_id and the
# reservation. The watch must now retry ``complete_reconciliation`` on EVERY
# tick through a freshly verified context (never completing without one);
# the overdue visibility window keeps its semantics.


class _FlakyReconciliationFactory:
    """Factory that raises the stable source-unavailable literal for the
    first ``failures`` attempts, then delegates to the real fixture factory
    (a fresh context per attempt), counting every attempt."""

    def __init__(self, inner, failures: int) -> None:
        self._inner = inner
        self._failures = failures
        self.attempts = 0

    def __call__(self, batch_id: str):
        self.attempts += 1
        if self._failures > 0:
            self._failures -= 1
            raise ValueError("N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE")
        return self._inner(batch_id)


def _watch_driver(store, trading, base_source, factory):
    from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver

    return NLegOrderQueueDriver(
        store,
        books_provider=lambda component_id: _fixture_books_snapshot(AS_OF),
        source_factory=_e2e_source_factory(base_source),
        trading=trading,
        reconciliation_context_factory=factory,
    )


def test_f1_reconciliation_watch_retries_and_completes_after_transient_failure(
    tmp_path: Path,
) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    factory = _FlakyReconciliationFactory(
        _recon_context_factory(base_source, store), failures=1
    )
    driver = _watch_driver(store, trading, base_source, factory)

    first = driver.tick(now=AS_OF)
    batch_id = str(first["submitted"])
    assert store.n_leg_batch(batch_id)["state"] == "AWAITING_RECONCILIATION"
    assert factory.attempts == 1

    second = driver.tick(now=AS_OF + timedelta(seconds=2))

    assert second == {"reconciled": batch_id}
    batch = store.n_leg_batch(batch_id)
    assert str(batch["state"]).startswith("RECONCILED")
    control = store.n_leg_control()
    assert control["active_batch_id"] is None
    # The ledger keeps the conservative protected bound (510/leg) exactly as
    # the approved E1 hand math pins it.
    assert control["total_unsettled_capital_units"] == 1020
    stored = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    assert stored["state"] == "SUBMITTED"


def test_f1_reconciliation_watch_keeps_retrying_while_overdue(
    tmp_path: Path,
) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    factory = _FlakyReconciliationFactory(
        _recon_context_factory(base_source, store), failures=10**9
    )
    driver = _watch_driver(store, trading, base_source, factory)

    first = driver.tick(now=AS_OF)
    batch_id = str(first["submitted"])
    assert store.n_leg_batch(batch_id)["state"] == "AWAITING_RECONCILIATION"
    assert factory.attempts == 1

    blocked = driver.tick(now=AS_OF + timedelta(seconds=61))
    assert blocked["blocked"] == "RECONCILIATION_TIMEOUT"
    assert blocked["execution_batch_id"] == batch_id
    still = driver.tick(now=AS_OF + timedelta(seconds=62))
    assert still["blocked"] == "RECONCILIATION_TIMEOUT"

    # The single-shot wedge is gone: the watch keeps retrying on every tick
    # (submit tick + the two overdue ticks) while the row stays flagged.
    assert factory.attempts == 3
    stored = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    assert stored["payload"]["reconciliation_overdue"] is True
    assert stored["state"] == "ADMITTED"


# F2 (review round 2, P3): an ACTIVE batch (a leg still resting, or a process
# death between admission and receipt folding) had NO watchdog: the tick
# silently returned EXECUTION_BATCH_ACTIVE forever while the queue head
# starved. The watch now covers ANY active batch beyond
# ``reconciliation_timeout_seconds`` with the same visibility as the AWAITING
# case (row flag + log + visible blocked reason) and never re-drives a
# submission (zero further trading calls).


def test_f2_active_batch_beyond_timeout_is_a_visible_blocked_state(
    tmp_path: Path,
) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading(
        [
            {"state": "FILLED", "error_code": None},
            {"state": "OPEN", "error_code": None},
        ]
    )
    driver = _e2e_driver(store, trading, base_source)

    first = driver.tick(now=AS_OF)
    batch_id = str(first["submitted"])
    batch = store.n_leg_batch(batch_id)
    assert batch["state"] == "ACTIVE"
    assert batch["incident"] is None

    within = driver.tick(now=AS_OF + timedelta(seconds=30))
    assert within == {"skipped": "EXECUTION_BATCH_ACTIVE"}

    blocked = driver.tick(now=AS_OF + timedelta(seconds=61))

    assert blocked["blocked"] == "ACTIVE_BATCH_OVERDUE"
    assert blocked["execution_batch_id"] == batch_id
    stored = next(
        r
        for r in store.n_leg_requests()
        if r["request_id"] == row["request_id"]
    )
    assert stored["payload"]["active_batch_overdue"] is True
    # Fail-closed red line: the watchdog never re-drives a submission.
    assert len(trading.calls) == 2
    assert store.n_leg_control()["active_batch_id"] == batch_id




# F4 (review round 2, P3): ``driver_execution_source`` built the heavy #51
# source outside the lock and re-inserted it with a bare ``setdefault`` — a
# frozen-solution rotation during the build (a new retained-books generation
# plus cached-source invalidation, exactly what ``_build_solve_request`` and
# the frozen-solution store path do) let the STALE generation build win the
# insertion and survive until the next rotation. The re-insertion must
# verify the books generation inside the lock and discard a stale build;
# the next access rebuilds from the new generation and cached-None keeps its
# semantics. Seam: the real resolver chain (real catalog, CP-SAT, #74
# prover) pinned by the repair-round-1 e2e, through the public accessor.


def test_f4_driver_execution_source_discards_stale_generation_build(
    tmp_path: Path,
) -> None:
    import threading

    from test_prediction_n_leg_fail_closed_e2e import _issue64_real_chain

    _store, instance, _monitor, component_id = _issue64_real_chain(tmp_path)

    # A first access builds and caches the generation-G1 source.
    first = instance.driver_execution_source(component_id)
    assert first is not None
    g1_books = instance._source_books[component_id]
    assert first["source"].books is g1_books

    # Deterministic replay of the race: the accessor's build starts (it has
    # already read the G1 retained books), then the frozen-solution rotation
    # swaps in a fresh G2 retained-books tuple and invalidates cached
    # sources, then the build finishes and offers its STALE result for
    # insertion.
    original_build = instance._build_execution_source
    original_account_view = instance._account_view
    build_started = threading.Event()
    rotation_done = threading.Event()
    built: list[object] = []
    returned: list[object] = []

    def gated_account_view():
        build_started.set()
        assert rotation_done.wait(timeout=30)
        return original_account_view()

    def recording_build(arg_component_id):
        result = original_build(arg_component_id)
        built.append(result)
        return result

    instance._account_view = gated_account_view
    instance._build_execution_source = recording_build
    # The rotation invalidates the cached source first (the accessor must
    # start a fresh build), then swaps the retained books mid-build.
    instance._sources.pop(component_id, None)

    def accessor() -> None:
        returned.append(instance.driver_execution_source(component_id))

    thread = threading.Thread(target=accessor)
    thread.start()
    assert build_started.wait(timeout=30)
    problem = instance._problem_map[component_id]
    snapshot = instance._snapshot_for(instance._selection[component_id])
    g2_books = instance._frozen_source_books(problem, snapshot)
    assert g2_books is not None and g2_books is not g1_books
    instance._source_books[component_id] = g2_books
    rotation_done.set()
    thread.join(timeout=30)

    # The stale G1 build completed but must be discarded: not re-inserted,
    # not returned.
    assert len(built) == 1 and built[0] is not None
    stale_source = built[0][0]
    assert stale_source.books is g1_books
    assert returned[0] is None
    assert component_id not in instance._sources
    # The next access rebuilds from the new generation — never the stale
    # generation object.
    second = instance.driver_execution_source(component_id)
    assert second is not None
    assert second["source"].books is g2_books
    assert second["source"] is not stale_source
