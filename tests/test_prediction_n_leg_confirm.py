"""Issue #64 manual-confirm FIFO queue: enqueue-side acceptance cases (B1-B6).

The confirm seam is ``open_trader.prediction_n_leg_confirm.confirm_enqueue``:
the server re-fetches the component's CURRENT solution at POST time,
re-verifies eligibility + caps + proof, and only then freezes and enqueues.
Rotation never hard-rejects (no 409 path): still-qualified rotations bind the
current solution and record the displayed/bound audit block; a rotation that
drops out of qualification rejects back to monitoring.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg import canonical_payload, fingerprint
from open_trader.prediction_n_leg_confirm import (
    QUEUE_LIMIT,
    NLegConfirmRejected,
    confirm_enqueue,
)
from open_trader.prediction_n_leg_mode import (
    ensure_same_event_same_venue_scope,
    n_leg_update_qualification_policy,
    n_leg_update_safety_config,
    n_leg_upsert_scope,
)

from test_prediction_executable_cost import AS_OF, _rehash_market_payload
from test_prediction_n_leg_execution import source_and_solution


SCOPE_ID = "SAME_EVENT_SAME_VENUE"
COMPONENT_ID = "component:contract-a:contract-b"


def _store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def _caps_store(tmp_path: Path) -> PredictionArbitrageStore:
    """Fresh store with the SAME_EVENT_SAME_VENUE scope raised to
    MANUAL_CANARY, the four caps confirmed in one explicit write, and a
    qualification policy matched to the two-leg fixture's honest economics
    (profit 2980 units, payout 4000, cost 1020 at 100 units/$; margin 0.745,
    annualized 13.59 over a 20-day release)."""
    store = _store(tmp_path)
    ensure_same_event_same_venue_scope(store)
    n_leg_upsert_scope(
        store,
        scope_id=SCOPE_ID,
        capability="MANUAL_CANARY",
        members={
            "relation_type": "complement",
            "same_event": True,
            "same_venue": True,
            "venues": ["polymarket"],
        },
        base_scope_version=1,
    )
    n_leg_update_qualification_policy(
        store,
        policy={
            "min_profit_usd": "0.002",
            "min_net_margin": "0.01",
            "min_annualized_return": "0.15",
            "max_capital_release_days": 30,
        },
        base_version=1,
    )
    n_leg_update_safety_config(
        store,
        config={
            "episode_rearm_gap_seconds": 300,
            "max_per_trade_cost_units": 25_000_000,
            "max_total_unsettled_capital_units": 100_000_000,
            "max_partial_fill_loss_units": 1_000_000,
            "max_auto_repair_loss_units": 1_000_000,
        },
        base_version=1,
    )
    return store


def _solution_entry(
    *,
    component_id: str = COMPONENT_ID,
    release_at: str | None = None,
) -> dict[str, object]:
    """One resolver-shaped solution entry whose economics re-verify as
    QUALIFIED_VERIFIED under the default policy (independent source of truth:
    bounded cost 1020, payout 4000, guaranteed profit 2980 units; margin
    0.745, release 20d -> annualized 13.59)."""
    source, execution = source_and_solution()
    market_payload = canonical_payload(source.decode_market())
    if release_at is not None:
        # Rotation-reject variant only: a mutated payload fails admission
        # decode, which is exactly the fail-closed behavior under test.
        market_payload["capital_release_at"] = release_at
        _rehash_market_payload(market_payload)
    execution_payload = canonical_payload(execution)
    # The resolver stores post-proof solutions (replace(execution,
    # partial_fill_proof=record.status)); model the same proven state. The
    # execution fingerprint does not cover the proof status field.
    execution_payload["partial_fill_proof"] = "PARTIAL_FILL_SAFE"
    return {
        "component_id": component_id,
        "market": market_payload,
        "execution": execution_payload,
        "fee": {
            "status": "fee_free",
            "charging_contracts": [],
            "unknown_contracts": [],
            "modeled": True,
            "taker_fee_rate_bps": 0,
            "taker_fee_units": 0,
        },
    }


def _confirm(
    store: PredictionArbitrageStore,
    solutions: list[dict[str, object]],
    *,
    component_id: str = COMPONENT_ID,
    displayed_fingerprint: str = "sha256:displayed",
    idempotency_key: str = "idem-1",
    partial_fill_proof=None,
):
    return confirm_enqueue(
        store,
        solutions,
        component_id=component_id,
        displayed_fingerprint=displayed_fingerprint,
        idempotency_key=idempotency_key,
        now=AS_OF,
        partial_fill_proof=partial_fill_proof,
    )


# B1: OBSERVE_ONLY scope rejects hard (even a direct API call), no queue row.


def test_b1_observe_only_scope_hard_rejects_without_queue_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ensure_same_event_same_venue_scope(store)
    solutions = [_solution_entry()]

    with pytest.raises(NLegConfirmRejected) as excinfo:
        _confirm(store, solutions)

    assert excinfo.value.reason == "SCOPE_OBSERVE_ONLY"
    assert excinfo.value.http_status == 403
    assert store.n_leg_requests() == []


# B2: rotation that stays qualified enqueues the CURRENT solution with the
# full displayed/bound audit block and rotated=True.


def test_b2_rotated_but_qualified_binds_current_with_audit_block(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    bound = fingerprint(entry["execution"])
    assert bound != "sha256:displayed"

    result = _confirm(
        store, [entry], displayed_fingerprint="sha256:displayed"
    )

    assert result["state"] == "PENDING"
    assert result["rotated"] is True
    assert result["bound_fingerprint"] == bound
    assert result["displayed_fingerprint"] == "sha256:displayed"
    rows = store.n_leg_requests()
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["execution_solution_fingerprint"] == bound
    assert payload["audit"] == {
        "displayed_fingerprint": "sha256:displayed",
        "bound_fingerprint": bound,
        "rotated": True,
    }
    assert payload["market"]["guaranteed_profit_units"] == 2980


# B3: rotation that drops out of qualification rejects and leaves no row.


def test_b3_rotated_out_of_qualification_rejects_without_queue_row(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    stale = _solution_entry(release_at=(AS_OF - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    with pytest.raises(NLegConfirmRejected) as missing:
        _confirm(store, [], idempotency_key="idem-missing")

    assert missing.value.reason == "COMPONENT_SOLUTION_UNAVAILABLE"
    with pytest.raises(NLegConfirmRejected) as excinfo:
        _confirm(store, [stale], idempotency_key="idem-stale")

    assert excinfo.value.reason == "COMPONENT_NOT_QUALIFIED"
    assert store.n_leg_requests() == []


# B4: a double click with the same idempotency key returns the same row and
# never enqueues twice.


def test_b4_same_idempotency_key_returns_same_request_row(tmp_path: Path) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()

    first = _confirm(store, [entry], idempotency_key="idem-double")
    second = _confirm(store, [entry], idempotency_key="idem-double")

    assert first["request_id"] == second["request_id"]
    assert len(store.n_leg_requests()) == 1


# B5: FIFO queue rules — one PENDING per component, five across components.


def test_b5_duplicate_component_and_full_queue(tmp_path: Path) -> None:
    store = _caps_store(tmp_path)
    entries = [
        _solution_entry(component_id=f"component:c{index}") for index in range(QUEUE_LIMIT)
    ]
    for index, entry in enumerate(entries):
        _confirm(store, [entry], component_id=entry["component_id"], idempotency_key=f"idem-{index}")

    with pytest.raises(NLegConfirmRejected) as duplicate:
        _confirm(
            store,
            entries,
            component_id=entries[0]["component_id"],
            idempotency_key="idem-duplicate",
        )
    assert duplicate.value.reason == "QUEUE_DUPLICATE"

    sixth = _solution_entry(component_id="component:c999")
    with pytest.raises(NLegConfirmRejected) as full:
        _confirm(store, [sixth], component_id="component:c999", idempotency_key="idem-sixth")
    assert full.value.reason == "QUEUE_FULL"

    rows = store.n_leg_requests()
    assert [row["fifo_index"] for row in rows] == sorted(row["fifo_index"] for row in rows)
    assert len(rows) == QUEUE_LIMIT


# B5+: review round 2 (ruling 3): the FIFO queue rules are enforced INSIDE
# the enqueue transaction (BEGIN IMMEDIATE), so concurrent confirms with
# different idempotency keys for one component cannot both insert.


def test_p2_enqueue_rejects_duplicate_component_inside_the_transaction(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    _confirm(store, [entry], idempotency_key="idem-first")

    with pytest.raises(ValueError, match="QUEUE_DUPLICATE"):
        store.n_leg_request_enqueue(
            component_id=COMPONENT_ID,
            idempotency_key="idem-second",
            payload={"component_id": COMPONENT_ID},
        )
    assert len(store.n_leg_requests()) == 1


def test_p2_enqueue_rejects_full_queue_inside_the_transaction(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    for index in range(QUEUE_LIMIT):
        _confirm(
            store,
            [_solution_entry(component_id=f"component:c{index}")],
            component_id=f"component:c{index}",
            idempotency_key=f"idem-{index}",
        )

    with pytest.raises(ValueError, match="QUEUE_FULL"):
        store.n_leg_request_enqueue(
            component_id="component:c999",
            idempotency_key="idem-sixth",
            payload={"component_id": "component:c999"},
        )
    assert len(store.n_leg_requests()) == QUEUE_LIMIT


def test_p2_concurrent_confirms_same_component_exactly_one_wins(
    tmp_path: Path,
) -> None:
    import threading

    store = _caps_store(tmp_path)
    entry = _solution_entry()
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def worker(key: str) -> None:
        barrier.wait()
        try:
            row = _confirm(store, [entry], idempotency_key=key)
            results.append(("ok", str(row["request_id"])))
        except NLegConfirmRejected as exc:
            results.append(("rejected", exc.reason))

    threads = [
        threading.Thread(target=worker, args=(key,))
        for key in ("idem-t1", "idem-t2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _ in results) == ["ok", "rejected"]
    assert [reason for kind, reason in results if kind == "rejected"] == [
        "QUEUE_DUPLICATE"
    ]
    assert (
        len({request_id for kind, request_id in results if kind == "ok"}) == 1
    )
    assert len(store.n_leg_requests()) == 1


# P3 (review round 2, ruling 7): the confirm freeze stores each leg's
# solve-request sequence baseline so the queue-head preflight can enforce
# monotonicity (SEQUENCE_REGRESSED) instead of it being dead code.


def test_p3_confirm_freezes_sequence_baselines(tmp_path: Path) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    entry["sequences"] = {"action-a": 11, "action-b": 12}

    _confirm(store, [entry], idempotency_key="idem-seq")

    payload = store.n_leg_requests()[0]["payload"]
    assert payload["sequences"] == {"action-a": 11, "action-b": 12}


# B6: breaker / incident / batch gates each reject with their literal.


def test_b6_open_gates_reject_with_gate_literals(tmp_path: Path) -> None:
    import sqlite3

    store = _caps_store(tmp_path)
    entry = _solution_entry()
    db_path = tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE n_leg_controls SET breaker_open=1, breaker_reason='TEST'")

    with pytest.raises(NLegConfirmRejected) as breaker:
        _confirm(store, [entry], idempotency_key="idem-breaker")
    assert breaker.value.reason == "GLOBAL_BREAKER_OPEN"

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE n_leg_controls SET breaker_open=0")
        connection.execute(
            "INSERT INTO incidents(incident_id, execution_id, payload, acknowledgement, acknowledged_at, created_at, updated_at)"
            " VALUES ('incident-64', 'execution-64', '{}', NULL, NULL, '2026-08-14T00:00:00Z', '2026-08-14T00:00:00Z')"
        )

    with pytest.raises(NLegConfirmRejected) as incident:
        _confirm(store, [entry], idempotency_key="idem-incident")
    assert incident.value.reason == "EXECUTION_INCIDENT_ACTIVE"

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM incidents")
        connection.execute(
            "UPDATE n_leg_controls SET active_batch_id='batch-64'"
        )

    with pytest.raises(NLegConfirmRejected) as batch:
        _confirm(store, [entry], idempotency_key="idem-batch")
    assert batch.value.reason == "EXECUTION_BATCH_ACTIVE"

    assert store.n_leg_requests() == []


# ---------------------------------------------------------------------------
# Slice 2 service surface: POST /api/prediction-arbitrage/n-leg/orders/confirm
# rides the production whitelist + auth and maps rejections to HTTP statuses;
# /state carries the real queue/caps/batch payload.
# ---------------------------------------------------------------------------


class _StubExecution:
    _breaker_open = False
    _cross_breaker_open = False

    def n_leg_mode_contract(self):
        from open_trader.prediction_n_leg_mode import n_leg_mode_contract

        return n_leg_mode_contract(_shared_runtime_store[0])


class _ConfirmRuntime:
    state = "RUNNING"
    mode = "production"
    production_owner = True
    legacy_retired = True

    def __init__(self, store, solutions):
        self.store = store
        self.execution = _StubExecution()
        self.monitor = None
        self.cross_venue_monitor = None
        self._solutions = solutions

    def n_leg_solutions(self):
        return self._solutions


_shared_runtime_store: list = []


def _confirm_post(base: str, payload: dict[str, object]):
    from test_prediction_api_contract import _post, _json_response

    return _json_response(_post(base, "/api/prediction-arbitrage/n-leg/orders/confirm", payload))


def test_confirm_endpoint_enqueues_replays_and_maps_rejections(
    tmp_path: Path,
) -> None:
    import urllib.error
    from datetime import UTC, datetime

    from open_trader.prediction_service import create_prediction_server
    from test_prediction_api_contract import _serve

    store = _caps_store(tmp_path)
    # The HTTP handler confirms against real wall-clock time, so the fixture
    # release must be future-dated relative to now, not AS_OF.
    entry = _solution_entry(
        release_at=(datetime.now(UTC) + timedelta(days=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    runtime = _ConfirmRuntime(store, [entry])
    _shared_runtime_store.clear()
    _shared_runtime_store.append(store)
    server = create_prediction_server(
        runtime=runtime,  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    with _serve(server) as base:
        status, _headers, body = _confirm_post(
            base,
            {
                "component_id": COMPONENT_ID,
                "displayed_fingerprint": "sha256:displayed",
                "idempotency_key": "http-idem-1",
            },
        )
        assert status == 200
        assert body["state"] == "PENDING"
        first_id = body["request_id"]

        status, _headers, replay = _confirm_post(
            base,
            {
                "component_id": COMPONENT_ID,
                "displayed_fingerprint": "sha256:displayed",
                "idempotency_key": "http-idem-1",
            },
        )
        assert status == 200
        assert replay["request_id"] == first_id
        assert len(store.n_leg_requests()) == 1

        with pytest.raises(urllib.error.HTTPError) as conflict:
            _confirm_post(
                base,
                {
                    "component_id": COMPONENT_ID,
                    "displayed_fingerprint": "sha256:displayed",
                    "idempotency_key": "http-duplicate",
                },
            )
        assert conflict.value.code == 409
        assert conflict.value.read().decode("utf-8").find("QUEUE_DUPLICATE") >= 0


def test_state_payload_carries_real_queue_caps_and_batch(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from open_trader.prediction_service import create_prediction_server
    from test_prediction_api_contract import _Monitor, _serve, _json_response

    store = _caps_store(tmp_path)
    entry = _solution_entry(
        release_at=(datetime.now(UTC) + timedelta(days=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    runtime = _ConfirmRuntime(store, [entry])
    runtime.monitor = _Monitor()
    _shared_runtime_store.clear()
    _shared_runtime_store.append(store)
    server = create_prediction_server(
        runtime=runtime,  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    with _serve(server) as base:
        _confirm_post(
            base,
            {
                "component_id": COMPONENT_ID,
                "displayed_fingerprint": "sha256:displayed",
                "idempotency_key": "state-idem-1",
            },
        )
        status, _headers, state = _json_response(base + "/api/prediction-arbitrage/state")
        assert status == 200
        orders = state["n_leg_orders"]
        assert orders["caps"]["configured"] is True
        assert orders["caps"]["acknowledged_version"] == orders["caps"]["safety_config_version"]
        assert orders["caps"]["values"]["max_per_trade_cost_units"] == 25_000_000
        assert len(orders["queue"]) == 1
        row = orders["queue"][0]
        assert row["position"] == 1
        assert row["component_id"] == COMPONENT_ID
        assert row["state"] == "PENDING"
        assert row["rotated"] is True
        assert orders["batch"] == {"execution_batch_id": None, "state": None}


# ---------------------------------------------------------------------------
# Slice 5: queue-head driver + real submit path (E1-E5), with a fake trading
# client and injected books/source factories through the approved seams.
# ---------------------------------------------------------------------------


class _FakeTrading:
    def __init__(self, outcomes):
        self.calls = []
        self.outcomes = list(outcomes)

    def submit_n_leg_leg_once(self, *, client_order_id, token_id, quantity_lots, max_cost_units, timeout_seconds=15):
        self.calls.append(client_order_id)
        if self.outcomes:
            return self.outcomes.pop(0)
        return {"state": "UNKNOWN", "error_code": "unscripted"}


def _fixture_books_snapshot(now):
    """Fresh books matching the two-leg fixture economics (asks 0.40 x 10)."""
    from open_trader.prediction_arbitrage import BookLevel
    from open_trader.prediction_snapshot_scheduler import ComponentSnapshot, LegBook, SnapshotLeg

    level = (BookLevel(Decimal("0.40"), Decimal("10")),)
    # Both fixture actions are BUY_YES: fresh asks at the frozen price.
    return ComponentSnapshot(
        COMPONENT_ID,
        (
            SnapshotLeg("action-a", LegBook(bids=(), asks=level, taker_fee_bps=Decimal("0"), available=True), now, now, 1),
            SnapshotLeg("action-b", LegBook(bids=(), asks=level, taker_fee_bps=Decimal("0"), available=True), now, now, 2),
        ),
    )


def _enqueued_e2e_store(tmp_path, *, release_future=True):
    """A caps-configured store with the fixture solution confirmed in, the
    bound #74 proof record frozen into the row."""
    from open_trader.prediction_executable_cost import (
        execution_solution_from_payload,
    )
    from open_trader.prediction_n_leg_execution import ExecutionSolutionSource
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    base_source, _ = source_and_solution()
    source = ExecutionSolutionSource(
        _decodable_execution(entry),
        entry["market"],
        base_source.component,
        base_source.books,
        base_source.account_snapshot,
        AS_OF,
    )
    market = source.decode_market()
    execution = execution_solution_from_payload(
        _decodable_execution(entry),
        market_solution=market,
        account_snapshot=source.account_snapshot,
        now=AS_OF,
    )
    from open_trader.prediction_n_leg import fingerprint
    from open_trader.prediction_n_leg_execution import (
        PartialFillProofRecord,
        execution_solution_binding,
    )

    # The bound proof must carry the store's CURRENT caps version; enter()
    # re-checks it against the admission's cap_config_version.
    safety_version = int(store.n_leg_safety_config_latest()["version"])
    proof_values: dict[str, object] = {
        **execution_solution_binding(execution),
        "cap_config_version": f"caps-v{safety_version}",
        "max_partial_fill_loss": 100,
        "max_auto_repair_loss": 10,
        "solver_lower_bound": 0,
        "solver_upper_bound": 100,
        "solver_termination": "CLOSED",
        "solver_evidence_fingerprint": "solver-evidence-v1",
        "verifier_status": "QUALIFIED_VERIFIED",
        "verifier_fingerprint": "verifier-v1",
        "verifier_evidence_fingerprint": "verifier-evidence-v1",
        "status": "PARTIAL_FILL_SAFE",
        "schema_version": "open_trader.prediction_n_leg.partial_fill_proof.v1",
    }
    proof_values["fingerprint"] = fingerprint(proof_values)
    bound_proof = PartialFillProofRecord(**proof_values)
    result = _confirm(
        store,
        [entry],
        displayed_fingerprint="sha256:displayed",
        idempotency_key="e2e-enqueue",
        partial_fill_proof=bound_proof.to_payload(),
    )
    assert result["state"] == "PENDING"
    return store, result, base_source


def _decodable_execution(entry):
    """The frozen execution payload without the proof-status display key,
    which admission decode does not accept (exact-field contract)."""
    return {
        key: value
        for key, value in entry["execution"].items()
        if key != "partial_fill_proof"
    }


def _e2e_source_factory(base_source):
    from open_trader.prediction_n_leg_execution import ExecutionSolutionSource

    def factory(frozen):
        return ExecutionSolutionSource(
            _decodable_execution(frozen),
            frozen["market"],
            base_source.component,
            base_source.books,
            base_source.account_snapshot,
            AS_OF,
        )

    return factory


def _recon_context_factory(base_source, store, now=AS_OF):
    from open_trader.prediction_n_leg_execution import (
        ConfirmedHolding,
        ReconciliationContext,
        SettlementCashFlow,
    )

    def factory(batch_id):
        batch = store.n_leg_batch(batch_id)
        flows = []
        holdings = []
        for leg in batch["legs"]:
            receipt = leg["receipt"]
            flows.append(
                SettlementCashFlow(
                    leg["client_order_id"],
                    receipt.get("venue_order_id"),
                    leg["venue_id"],
                    leg["account_id"],
                    leg["settlement_asset_id"],
                    int(receipt["cumulative_cost_units"]),
                    int(receipt["cumulative_fee_units"]),
                    now,
                    now,
                    receipt.get("rest_observation_version") if receipt.get("rest_confirmed") else receipt.get("sequence"),
                    bool(receipt["rest_confirmed"]),
                )
            )
            if int(receipt["cumulative_filled_quantity"]) > 0:
                holdings.append(
                    ConfirmedHolding(
                        leg["venue_id"],
                        leg["account_id"],
                        leg["asset_id"],
                        int(receipt["cumulative_filled_quantity"]),
                        now,
                        now,
                    )
                )
        return ReconciliationContext(
            f"{batch_id}:v1",
            base_source.account_snapshot,
            tuple(holdings),
            tuple(flows),
            now,
            now,
            now,
        )

    return factory


def _e2e_driver(store, trading, base_source, *, recon=False, timeout_seconds=1):
    from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver

    return NLegOrderQueueDriver(
        store,
        books_provider=lambda component_id: _fixture_books_snapshot(AS_OF),
        source_factory=_e2e_source_factory(base_source),
        trading=trading,
        reconciliation_context_factory=(
            _recon_context_factory(base_source, store) if recon else None
        ),
        submit_timeout_seconds=timeout_seconds,
    )


def test_e1_two_legs_filled_complete_release_and_submitted_row(tmp_path) -> None:
    from test_prediction_n_leg_execution import proof  # noqa: F401  (fixture parity)

    store, row, base_source = _enqueued_e2e_store(tmp_path)
    # The adapter reports the actual fill cost (below the FOK bound).
    filled = {"state": "FILLED", "error_code": None, "cost_units": 400}
    trading = _FakeTrading([filled, filled])
    driver = _e2e_driver(store, trading, base_source, recon=True)

    summary = driver.tick(now=AS_OF)

    assert summary["submitted"]
    assert len(trading.calls) == 2
    batch_id = summary["submitted"]
    batch = store.n_leg_batch(batch_id)
    assert str(batch["state"]).startswith("RECONCILED")
    control = store.n_leg_control()
    assert control["active_batch_id"] is None
    # The reservation ownership is released with the batch; the ledger keeps
    # the conservative protected bound (510/leg) until venue settlement —
    # actual fill cost (400/leg) is booked on the receipts.
    assert control["total_unsettled_capital_units"] == 1020
    rows = store.n_leg_requests()
    assert [r["state"] for r in rows if r["request_id"] == row["request_id"]] == ["SUBMITTED"]
    # lineage execution lock survives the completed batch
    import sqlite3

    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM n_leg_lineage_claims").fetchone()[0] == 1


def _e2e_db(tmp_path):
    return tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"


def test_e2_one_filled_one_rejected_opens_incident_and_clears_queue(tmp_path) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    # a second component waits behind the head
    second = _solution_entry(component_id="component:other")
    _confirm(store, [second], component_id="component:other", idempotency_key="e2e-second")
    assert len([r for r in store.n_leg_requests() if r["state"] == "PENDING"]) == 2

    trading = _FakeTrading([{"state": "FILLED", "error_code": None}, {"state": "REJECTED", "error_code": None}])
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    batch = store.n_leg_batch(summary["submitted"])
    # the incident row exists before any repair action: plan stays unset
    assert batch["incident"] is not None
    assert batch.get("repair_plan") is None
    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["active_batch_id"] == summary["submitted"]
    # every other PENDING row is cleared
    assert [r["state"] for r in store.n_leg_requests() if r["request_id"] != row["request_id"]] == ["ABANDONED"]
    # a new confirm is rejected by the incident gate
    with pytest.raises(NLegConfirmRejected) as excinfo:
        _confirm(store, [_solution_entry(component_id="component:third")], component_id="component:third", idempotency_key="e2e-third")
    assert excinfo.value.reason == "EXECUTION_INCIDENT_ACTIVE"


def test_e3_receipt_replay_does_not_double_record(tmp_path) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading([{"state": "FILLED", "error_code": None}, {"state": "FILLED", "error_code": None}])
    driver = _e2e_driver(store, trading, base_source)
    summary = driver.tick(now=AS_OF)
    batch_id = summary["submitted"]

    import sqlite3

    def transition_count():
        with sqlite3.connect(_e2e_db(tmp_path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM n_leg_transitions WHERE execution_batch_id=?", (batch_id,)
            ).fetchone()[0]

    first = transition_count()
    batch = store.n_leg_batch(batch_id)
    for leg in batch["legs"]:
        store.n_leg_transition_append(
            batch_id,
            kind="SUBMISSION_ATTEMPT",
            idempotency_key=f"submission:{leg['client_order_id']}",
            payload={"client_order_id": leg["client_order_id"], "outcome": "FILLED"},
        )
    assert transition_count() == first


def test_e4_leg_timeout_books_unknown_receipt_and_opens_incident(tmp_path) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading([{"state": "UNKNOWN", "error_code": "timeout"}, {"state": "FILLED", "error_code": None}])
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    batch = store.n_leg_batch(summary["submitted"])
    assert batch["incident"]["reason"] == "UNKNOWN_ORDER_STATE"
    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["breaker_open"] is True
    assert all(r["state"] != "PENDING" for r in store.n_leg_requests())


def test_e5_sell_action_fails_closed_before_any_submit(tmp_path) -> None:
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    stored = next(r for r in store.n_leg_requests() if r["request_id"] == row["request_id"])
    payload = dict(stored["payload"])
    execution = dict(payload["execution"])
    legs = [dict(leg) for leg in execution["execution_legs"]]
    legs[0]["side"] = "SELL_YES"
    execution["execution_legs"] = legs
    payload["execution"] = execution
    sell_row = store.n_leg_request_enqueue(
        component_id="component:sell",
        idempotency_key="e2e-sell",
        payload=payload,
    )
    # make the SELL row the queue head
    store.n_leg_request_update(str(row["request_id"]), state="ABANDONED", abandon_reason="TEST_SETUP")
    trading = _FakeTrading([])
    driver = _e2e_driver(store, trading, base_source)

    summary = driver.tick(now=AS_OF)

    assert summary["abandoned"] == "UNSUPPORTED_ACTION"
    assert trading.calls == []


# ---------------------------------------------------------------------------
# Slice 6: incident-gate unlock + breaker reset closure.
# ---------------------------------------------------------------------------


def _incident_store(tmp_path, *, outcome):
    """Drive one confirm through admission to an incident batch."""
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading([outcome, {"state": "FILLED", "error_code": None, "cost_units": 400}])
    driver = _e2e_driver(store, trading, base_source)
    summary = driver.tick(now=AS_OF)
    return store, summary["submitted"], base_source


def test_f1_unlock_rejects_unknown_receipt_and_keeps_incident(tmp_path) -> None:
    store, batch_id, _ = _incident_store(
        tmp_path, outcome={"state": "UNKNOWN", "error_code": "timeout"}
    )

    with pytest.raises(ValueError, match="N_LEG_INCIDENT_UNKNOWN_RECEIPT"):
        store.n_leg_acknowledge_incident(
            batch_id,
            acknowledgement={"actor": "operator", "reconciliation": "fresh_clean"},
        )

    assert store.n_leg_batch(batch_id)["state"] == "INCIDENT"
    assert store.n_leg_incident_batch() is not None


def test_f2_unlock_releases_gate_keeps_mode_and_lineage(tmp_path) -> None:
    store, batch_id, _ = _incident_store(
        tmp_path, outcome={"state": "REJECTED", "error_code": None}
    )
    assert store.n_leg_incident_batch() is not None

    from open_trader.prediction_n_leg_mode import (
        n_leg_mode_contract,
        n_leg_order_readiness,
    )

    acknowledged = store.n_leg_acknowledge_incident(
        batch_id,
        acknowledgement={"actor": "operator", "reconciliation": "fresh_clean"},
    )
    assert acknowledged["state"] == "INCIDENT_ACKNOWLEDGED"
    assert store.n_leg_incident_batch() is None
    contract = n_leg_mode_contract(store)
    assert contract["mode"] == "MANUAL"
    assert contract["execution_gates"]["incident_active"] is False
    # The unrepaired partial fill also trips the global breaker; the closure
    # loop is acknowledge -> breaker reset (fresh_clean ack above).
    assert store.n_leg_control()["breaker_open"] is True
    assert store.n_leg_breaker_reset() == {
        "state": "ready",
        "reason": "reset_confirmed",
    }
    assert n_leg_order_readiness(store)["order_ready"] is True
    # the lineage execution lock survives the incident
    import sqlite3

    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM n_leg_lineage_claims"
        ).fetchone()[0] == 1
    # confirm is no longer blocked by the incident gate
    result = _confirm(
        store,
        [_solution_entry(component_id="component:after")],
        component_id="component:after",
        idempotency_key="after-unlock",
    )
    assert result["state"] == "PENDING"


def test_f3_breaker_reset_requires_acknowledged_fresh_clean(tmp_path) -> None:
    store, batch_id, _ = _incident_store(
        tmp_path, outcome={"state": "UNKNOWN", "error_code": "timeout"}
    )
    assert store.n_leg_control()["breaker_open"] is True

    with pytest.raises(ValueError, match="N_LEG_BREAKER_INCIDENT_NOT_ACKNOWLEDGED"):
        store.n_leg_breaker_reset()

    with pytest.raises(ValueError, match="N_LEG_INCIDENT_UNKNOWN_RECEIPT"):
        store.n_leg_acknowledge_incident(
            batch_id,
            acknowledgement={"actor": "operator", "reconciliation": "fresh_clean"},
        )
    # An acknowledgement WITHOUT a fresh-clean reconciliation does not open
    # the breaker: seed the ack record through the same transition seam and
    # verify the reset keeps demanding fresh_clean.
    batch = store.n_leg_batch(batch_id)
    for leg in batch["legs"]:
        store.n_leg_transition_append(
            batch_id,
            kind="SUBMISSION_ATTEMPT",
            idempotency_key=f"probe:{leg['client_order_id']}",
            payload={"client_order_id": leg["client_order_id"]},
        )
    store.n_leg_transition_append(
        batch_id,
        kind="INCIDENT_ACKNOWLEDGED",
        idempotency_key=f"incident-acknowledge:{batch_id}",
        payload={"batch": batch_id, "actor": "operator", "reconciliation": "pending"},
    )
    with pytest.raises(ValueError, match="N_LEG_BREAKER_RECONCILIATION_REQUIRED"):
        store.n_leg_breaker_reset()


# ---------------------------------------------------------------------------
# Repair round 3 (F5, review round 2): the rotated audit flag compared the
# card's LIGHT read-model fingerprint against the HEAVY #51 bound fingerprint
# — two structurally different payload families — so every production confirm
# recorded rotated=True even with no rotation at all. ``rotated`` must be a
# same-generation signal: the incoming card fingerprint compared against the
# CURRENT entry's read-model fingerprint (the same formula the card renders),
# while the light/heavy family split is recorded explicitly as
# ``payload_family`` on the frozen audit block (heavy path only; the light
# fallback keeps the exact approved B2 block shape).
# ---------------------------------------------------------------------------


def _heavy_source_block():
    """The fixture's heavy #51 family payloads (the resolver-retained shape),
    structurally distinct from the entry's display payload."""
    source, execution = source_and_solution()
    return {
        "market": canonical_payload(source.decode_market()),
        "execution": canonical_payload(execution),
    }


def test_f5_same_generation_heavy_confirm_is_not_rotated(tmp_path: Path) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    # What the card displayed: the read-model fingerprint of the CURRENT
    # entry (no rotation between render and confirm).
    displayed = fingerprint(entry["execution"])
    heavy = _heavy_source_block()

    result = confirm_enqueue(
        store,
        [entry],
        component_id=COMPONENT_ID,
        displayed_fingerprint=displayed,
        idempotency_key="f5-same",
        now=AS_OF,
        execution_source=heavy,
    )

    assert result["state"] == "PENDING"
    assert result["rotated"] is False
    assert result["bound_fingerprint"] == fingerprint(heavy["execution"])
    stored = store.n_leg_requests()[0]
    assert stored["payload"]["audit"] == {
        "displayed_fingerprint": displayed,
        "bound_fingerprint": fingerprint(heavy["execution"]),
        "rotated": False,
        "payload_family": "heavy",
    }


def test_f5_cross_generation_heavy_confirm_is_rotated(tmp_path: Path) -> None:
    store = _caps_store(tmp_path)
    current = _solution_entry()
    stale_generation = _solution_entry()
    # The generation the card was rendered from: same component, different
    # solution (one unit more capital) — its read-model fingerprint is what
    # the confirm request brings in.
    stale_generation["execution"] = {
        **stale_generation["execution"],
        "capital_use_units": (
            int(stale_generation["execution"]["capital_use_units"]) + 1
        ),
    }
    displayed = fingerprint(stale_generation["execution"])
    heavy = _heavy_source_block()

    result = confirm_enqueue(
        store,
        [current],
        component_id=COMPONENT_ID,
        displayed_fingerprint=displayed,
        idempotency_key="f5-cross",
        now=AS_OF,
        execution_source=heavy,
    )

    assert result["state"] == "PENDING"
    assert result["rotated"] is True
    assert result["bound_fingerprint"] == fingerprint(heavy["execution"])
    stored = store.n_leg_requests()[0]
    assert stored["payload"]["audit"]["rotated"] is True
    assert stored["payload"]["audit"]["payload_family"] == "heavy"


def test_f5_light_path_same_generation_keeps_exact_block_shape(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    entry = _solution_entry()
    displayed = fingerprint(entry["execution"])

    result = _confirm(
        store,
        [entry],
        displayed_fingerprint=displayed,
        idempotency_key="f5-light",
    )

    assert result["state"] == "PENDING"
    assert result["rotated"] is False
    stored = store.n_leg_requests()[0]
    assert stored["payload"]["audit"] == {
        "displayed_fingerprint": displayed,
        "bound_fingerprint": fingerprint(entry["execution"]),
        "rotated": False,
    }


# ---------------------------------------------------------------------------
# Issue #122: the confirm seam runs the same executed-lock decision the
# admission transaction enforces (R5 precheck), and the frozen lineage
# identity is the resolver entry's graph lineage, never a synthetic string.
# ---------------------------------------------------------------------------


def _merge_rotation_graph(tmp_path: Path):
    """A real runtime graph over the confirm store's own SQLite: two disjoint
    NEW families that one bridge relation later merges into one successor."""
    from test_prediction_runtime_graph import make_graph, row

    graph, state, meta = make_graph(
        tmp_path / "data",
        {
            "IMPLIES|polymarket:ca|polymarket:cb": row(
                "v1", [("polymarket", "ca"), ("polymarket", "cb")]
            ),
            "IMPLIES|polymarket:cd|polymarket:ce": row(
                "v2", [("polymarket", "cd"), ("polymarket", "ce")]
            ),
        },
    )
    graph.refresh()
    return graph, state, meta


def _claim_family(
    store: PredictionArbitrageStore, tmp_path: Path, component_id: str
) -> None:
    """Record the executed-family claim through the real admission seam, then
    free the single-active-batch gate (not under test) with the approved
    controls-seam idiom."""
    import sqlite3

    store.n_leg_create_batch(
        {
            "execution_batch_id": "batch-family",
            "opportunity_episode_id": "episode-family",
            "episode_lineage_id": f"lineage:{component_id}",
            "mode": "MANUAL",
            "state": "ACTIVE",
            "entry_fingerprint": "entry-family",
            "execution_solution_fingerprint": "solution-family",
            "total_unsettled_capital_units": 1,
            "component_id": component_id,
        }
    )
    with sqlite3.connect(
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    ) as connection:
        connection.execute("UPDATE n_leg_controls SET active_batch_id=NULL")


def test_issue122_confirm_precheck_rejects_inherited_claim_without_queue_row(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    graph, state, meta = _merge_rotation_graph(tmp_path)
    components = graph.components()
    executed, _spared = sorted(
        components.values(), key=lambda component: component.component_id
    )
    _claim_family(store, tmp_path, executed.component_id)

    # Real rotation: the bridge relation merges both families into one.
    from test_prediction_runtime_graph import row

    state["IMPLIES|polymarket:cb|polymarket:cd"] = row(
        "v3", [("polymarket", "cb"), ("polymarket", "cd")]
    )
    meta["generation"] += 1
    graph.refresh()
    merged = list(graph.components().values())
    assert len(merged) == 1 and merged[0].change_kind == "MERGE"
    successor = merged[0]
    assert executed.lineage_id in successor.predecessor_lineage_ids

    entry = _solution_entry(component_id=successor.component_id)
    entry["lineage_id"] = successor.lineage_id

    with pytest.raises(NLegConfirmRejected) as excinfo:
        _confirm(
            store,
            [entry],
            component_id=successor.component_id,
            idempotency_key="issue122-precheck",
        )

    assert excinfo.value.reason == "N_LEG_LINEAGE_INHERITED_CLAIMED"
    assert store.n_leg_requests() == []


def test_issue122_confirm_freezes_resolver_lineage_identity(
    tmp_path: Path,
) -> None:
    store = _caps_store(tmp_path)
    graph, _state, _meta = _merge_rotation_graph(tmp_path)
    components = graph.components()
    assert len(components) == 2
    component = next(iter(components.values()))

    entry = _solution_entry(component_id=component.component_id)
    entry["lineage_id"] = component.lineage_id

    result = _confirm(
        store,
        [entry],
        component_id=component.component_id,
        idempotency_key="issue122-freeze",
    )

    assert result["state"] == "PENDING"
    payload = store.n_leg_requests()[0]["payload"]
    # The frozen identity is the graph lineage truth, read from the graph.
    assert payload["episode_lineage_id"] == component.lineage_id


def test_issue122_confirm_precheck_blocks_oracle_format_component_id(
    tmp_path: Path,
) -> None:
    """The production confirm identity is the oracle component id
    (``component:<contracts>``), never the graph's sha256 digest: the R5
    precheck must therefore run on the frozen lineage string the resolver's
    ``_lineage_by_component`` mapping carries (``prediction_live_resolver.
    _reconcile``), or a claimed family's successor is never rejected before
    anything is enqueued."""
    store = _caps_store(tmp_path)
    graph, state, meta = _merge_rotation_graph(tmp_path)
    components = graph.components()
    executed, _spared = sorted(
        components.values(), key=lambda component: component.component_id
    )
    _claim_family(store, tmp_path, executed.component_id)

    # Real rotation: the bridge relation merges both families into one.
    from test_prediction_runtime_graph import row

    state["IMPLIES|polymarket:cb|polymarket:cd"] = row(
        "v3", [("polymarket", "cb"), ("polymarket", "cd")]
    )
    meta["generation"] += 1
    graph.refresh()
    merged = list(graph.components().values())
    assert len(merged) == 1 and merged[0].change_kind == "MERGE"
    successor = merged[0]
    assert executed.lineage_id in successor.predecessor_lineage_ids

    # Production identity shape: the oracle component id is rebuilt from the
    # successor's venue-qualified contracts exactly as the resolver's
    # lineage map does (contract ids stripped of the venue prefix).
    raw_contracts = sorted(
        contract.split(":", 1)[1] if ":" in contract else contract
        for contract in successor.contract_ids
    )
    oracle_component_id = f"component:{':'.join(raw_contracts)}"
    entry = _solution_entry(component_id=oracle_component_id)
    entry["lineage_id"] = successor.lineage_id

    with pytest.raises(NLegConfirmRejected) as excinfo:
        _confirm(
            store,
            [entry],
            component_id=oracle_component_id,
            idempotency_key="issue122-oracle-precheck",
        )

    assert excinfo.value.reason == "N_LEG_LINEAGE_INHERITED_CLAIMED"
    assert store.n_leg_requests() == []
