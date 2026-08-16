"""Issue #52: live resolver loop, snapshot assembly, and solution bridge."""

from __future__ import annotations

import time
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_live_resolver import (
    LIVE_LIMITS,
    PredictionLiveResolver,
    normalize_problem,
)
from open_trader.prediction_market_solution import AccountView
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    OracleBudget,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import evaluate_fixed_portfolio
from open_trader.prediction_solver import (
    ObjectiveBounds,
    PortfolioCandidate,
    SolverEvidence,
)
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse


AS_OF = datetime(2026, 8, 16, tzinfo=UTC)
BUDGET = OracleBudget(
    max_quantity_vectors=9, max_joint_states=2, max_support_rechecks=1
)


def observation(contract_id: str) -> SettlementObservationKey:
    return SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        f"oracle-{contract_id}",
        f"indicator-{contract_id}",
        AS_OF,
        AS_OF + timedelta(hours=1),
        "UTC",
        "v1",
    )


def raw_action(
    action_id: str, contract_id: str, side: ActionSide, *, venue: str = "polymarket"
) -> CandidateAction:
    return CandidateAction(
        action_id=action_id,
        venue_id=venue,
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id=contract_id,
        settlement_observation_key=observation(contract_id),
        side=side,
        lot_step_units=1,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=2,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(ExecutableCostSlice(1, 2, 1),),
    )


def raw_problem(*, venue: str = "polymarket") -> ArbitrageProblem:
    yes = raw_action("a-yes", "contract-a", ActionSide.BUY_YES, venue=venue)
    no = raw_action("a-no", "contract-a", ActionSide.BUY_NO, venue=venue)
    states = (
        TerminalStateSet(
            "contract-a",
            observation("contract-a"),
            "v1",
            (
                TerminalAtom(
                    "contract-a:yes",
                    TerminalKind.NORMAL_YES,
                    "v1",
                    (ActionPayout("a-yes", 1), ActionPayout("a-no", 0)),
                    AS_OF,
                ),
                TerminalAtom(
                    "contract-a:no",
                    TerminalKind.NORMAL_NO,
                    "v1",
                    (ActionPayout("a-yes", 0), ActionPayout("a-no", 1)),
                    AS_OF,
                ),
            ),
        ),
    )
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "live-test",
        AS_OF,
        "usd-cents",
        (yes, no),
        states,
        ConstraintModel((), ()),
        (),
    )


def row(identity: str, compiled: ArbitrageProblem) -> dict[str, object]:
    return {
        "identity": identity,
        "version_id": f"v-{identity}",
        "fingerprint": f"fp-{identity}",
        "activation": "ACTIVE",
        "relation_type": "IMPLIES",
        "endpoints": [],
        "model": {
            "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
            "payouts": {},
            "capital_release": "2026-08-31T00:00:00Z",
            "problem": canonical_payload(compiled),
        },
    }


class FakeCatalog:
    def __init__(self, rows: dict[str, object], generation: int = 1) -> None:
        self.rows = rows
        self.generation = generation
        self.fail = False

    def current_generation(self) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("catalog refresh failed")
        return dict(self.rows)

    def generation_meta(self) -> dict[str, object]:
        return {"generation": self.generation, "fingerprint": f"gen-{self.generation}"}


class FakeMonitor:
    def __init__(self, books: dict[str, ThresholdOrderBook] | None = None) -> None:
        self.books = books or {}

    def cross_venue_books(self, token_ids: tuple[str, ...]) -> dict[str, ThresholdOrderBook]:
        now = datetime.now(UTC)
        return {
            token: book
            for token, book in self.books.items()
            if token in set(token_ids)
            and (now - book.confirmed_at).total_seconds() <= 30
        }

    def cross_venue_book_meta(self, token_id: str) -> dict[str, object]:
        book = self.books.get(token_id)
        if book is None:
            return {"received_at": None, "exchange_time": None, "sequence": None}
        exchange_time = book.confirmed_at
        return {
            "received_at": book.confirmed_at,
            "exchange_time": exchange_time,
            "sequence": int(exchange_time.timestamp() * 1000),
        }


class FakeExecution:
    def __init__(self, view=None) -> None:
        self.view = view

    def n_leg_account_view(self):
        return self.view


class FakeStore:
    def __init__(self, unsettled: int = 0, max_unsettled: int = 5_000_000) -> None:
        self.unsettled = unsettled
        self.max_unsettled = max_unsettled

    def n_leg_control(self) -> dict[str, object]:
        return {"total_unsettled_capital_units": self.unsettled}

    def n_leg_safety_config_latest(self) -> dict[str, object]:
        return {
            "version": 1,
            "config": {"max_total_unsettled_capital_units": self.max_unsettled},
        }


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.futures: list[Future[WorkerOutcome]] = []

    def submit(self, request: object) -> Future[WorkerOutcome]:
        future: Future[WorkerOutcome] = Future()
        self.requests.append(request)
        self.futures.append(future)
        return future


def live_book(token_id: str, *, price: str = "0.49") -> ThresholdOrderBook:
    level = (BookLevel(Decimal(price), Decimal("2")),)
    return ThresholdOrderBook(token_id, level, level, datetime.now(UTC))


def selected_component(
    component_id: str,
    *,
    relation_fingerprint: str = "r",
    terminal_fingerprint: str = "t",
) -> SelectedComponent:
    return SelectedComponent(
        component_id=component_id,
        contract_ids=("contract-a",),
        constraint_ids=(),
        action_ids=("a-yes", "a-no"),
        admission_score=20_000,
        portfolio=(ActionQuantity("a-yes", 1), ActionQuantity("a-no", 1)),
        relation_fingerprint=relation_fingerprint,
        terminal_fingerprint=terminal_fingerprint,
        portfolio_fingerprint="p",
        status="ACTIVE",
    )


def resolver(
    tmp_path: Path,
    *,
    rows: dict[str, object] | None = None,
    monitor: FakeMonitor | None = None,
    execution: FakeExecution | None = None,
    store: FakeStore | None = None,
    server: FakeServer | None = None,
) -> tuple[PredictionLiveResolver, FakeServer, FakeCatalog]:
    server = server or FakeServer()
    catalog = FakeCatalog(rows or {})
    resolver_instance = PredictionLiveResolver(
        data_dir=tmp_path,
        relation_catalog=catalog,
        monitor=monitor or FakeMonitor(),
        solver_server=server,
        selection_store=MonitorSelectionStore(tmp_path),
        store=store or FakeStore(),
        execution=execution or FakeExecution(),
        poll_interval=0.01,
    )
    return resolver_instance, server, catalog


def valid_selected(rows: dict[str, object]) -> SelectedComponent:
    problem, components = relation_generation_problem(rows)
    component = components[0]
    raw = problem_for_component(problem, component)
    return selected_component(
        component.component_id,
        relation_fingerprint=fingerprint({"constraint_model": raw.constraint_model}),
        terminal_fingerprint=fingerprint(
            {"terminal_state_sets": raw.terminal_state_sets}
        ),
    )


def test_normalize_problem_maps_micro_units_and_payouts() -> None:
    normalized = normalize_problem(raw_problem())
    assert normalized.valuation_unit_id == "usd-micro"
    assert all(
        action.settlement_asset_id == "usd-micro"
        and action.valuation_unit_id == "usd-micro"
        and action.asset_valuation_rule_id == "usd-micro-v1"
        for action in normalized.actions
    )
    payouts: dict[str, set[int]] = {"a-yes": set(), "a-no": set()}
    for payout in (
        payout
        for state in normalized.terminal_state_sets
        for atom in state.atoms
        for payout in atom.payouts
    ):
        payouts[payout.action_id].add(payout.payout_lower_bound_per_lot_units)
    assert payouts == {"a-yes": {1_000_000, 0}, "a-no": {0, 1_000_000}}


def test_snapshot_assembly_and_missing_leg_fail_closed(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, _, _ = resolver(tmp_path, rows=rows)
    instance._reconcile()
    component_id = "component:contract-a"
    monitor = FakeMonitor(
        {"contract-a": live_book("contract-a")}
    )
    instance._monitor = monitor
    snapshot = instance._snapshot_for(valid_selected(rows))
    assert snapshot is not None
    assert {leg.leg_id for leg in snapshot.legs} == {"a-yes", "a-no"}
    assert all(leg.book.available and leg.book.taker_fee_bps == Decimal("0") for leg in snapshot.legs)
    assert all(leg.received_at is not None and leg.sequence is not None for leg in snapshot.legs)

    instance._problem_map[component_id] = raw_problem(venue="predict")
    assert instance._snapshot_for(valid_selected(rows)) is None

    instance._problem_map[component_id] = raw_problem()
    instance._monitor = FakeMonitor()
    assert instance._snapshot_for(valid_selected(rows)) is None


def test_tick_dispatches_solve_request_through_tracking_wrapper(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    component_id = "component:contract-a"
    instance._tick()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.backend == "cp_sat"
    assert request.limits == LIVE_LIMITS
    assert request.request.problem.valuation_unit_id == "usd-micro"
    assert (
        request.request.problem.actions[0].cost_slices[0].incremental_cost_upper_bound_units
        == 490_000
    )
    assert instance._request_components[request.request_id] == component_id


def worker_evidence(problem: ArbitrageProblem) -> dict[str, object]:
    quantities = (ActionQuantity("a-yes", 1), ActionQuantity("a-no", 1))
    evaluation = evaluate_fixed_portfolio(problem, quantities, BUDGET)
    evidence = SolverEvidence(
        native_status="FEASIBLE",
        candidate=PortfolioCandidate(
            quantities, evaluation.guaranteed_profit_units
        ),
        objective_bounds=ObjectiveBounds(
            evaluation.guaranteed_profit_units, None, None, False
        ),
        worst_scenario=evaluation.worst_scenario,
        payout_lower_bound_units=evaluation.payout_lower_bound_units,
        cost_upper_bound_units=evaluation.cost_upper_bound_units,
        guaranteed_profit_units=evaluation.guaranteed_profit_units,
        conservative_capital_release_at=evaluation.conservative_capital_release_at,
        fixed_portfolio_closed=True,
        global_search_closed=False,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(evaluation.worst_state_cut,),
        certificate=None,
    )
    return canonical_payload(evidence)


def worker_outcome(request, evidence: dict[str, object]) -> WorkerOutcome:
    return WorkerOutcome(
        request.request_id,
        "OK",
        "COMPLETED",
        1,
        1,
        0,
        False,
        True,
        WorkerResponse(
            "p",
            "cp_sat",
            request.request_id,
            "OK",
            evidence,
            {},
            (),
        ),
        "9.15.6755",
    )


def test_completed_evidence_becomes_market_and_execution_solution(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    assert len(server.futures) == 1
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    solutions = instance.solutions()
    assert len(solutions) == 1
    market = solutions[0]["market"]
    assert market["guaranteed_profit_units"] == 20_000
    execution = solutions[0]["execution"]
    assert execution["reason"] == "EXECUTABLE"
    assert execution["order_ready"] is False
    assert execution["partial_fill_proof"] == "UNKNOWN"


def test_unavailable_account_leaves_execution_none(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(None),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    solutions = instance.solutions()
    assert len(solutions) == 1
    assert solutions[0]["execution"] is None


def test_reconcile_prunes_stale_selection_and_skips_discovery(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _ = resolver(tmp_path, rows=rows)
    valid = valid_selected(rows)
    stale = selected_component("component:stale", relation_fingerprint="stale-r")
    instance._selection_store.save(
        {valid.component_id: valid, stale.component_id: stale}
    )
    instance._reconcile()
    kept = instance._selection_store.load()[1]
    assert set(kept) == {valid.component_id}
    assert instance._selection == {valid.component_id: valid}
    assert server.requests == []


def test_start_stop_idempotent_and_per_tick_exception_isolation(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, catalog = resolver(tmp_path, rows=rows)
    instance.start()
    thread = instance._thread
    assert thread is not None
    instance.start()
    assert instance._thread is thread
    catalog.fail = True
    time.sleep(0.08)
    assert instance._thread is not None and instance._thread.is_alive()
    instance.stop()
    instance.stop()
    assert instance._thread is None
