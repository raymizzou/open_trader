"""Issue #52: live resolver loop, snapshot assembly, and solution bridge."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
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
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    OracleBudget,
    QualificationConstraint,
    QualificationMetric,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_episodes import (
    CLOSE_COMPONENT_RETIRED,
    CLOSE_NO_QUALIFIED_OPPORTUNITY,
    EpisodeTracker,
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


def raw_problem(
    *,
    venue: str = "polymarket",
    contract: str = "contract-a",
    yes_id: str = "a-yes",
    no_id: str = "a-no",
) -> ArbitrageProblem:
    yes = raw_action(yes_id, contract, ActionSide.BUY_YES, venue=venue)
    no = raw_action(no_id, contract, ActionSide.BUY_NO, venue=venue)
    states = (
        TerminalStateSet(
            contract,
            observation(contract),
            "v1",
            (
                TerminalAtom(
                    f"{contract}:yes",
                    TerminalKind.NORMAL_YES,
                    "v1",
                    (ActionPayout(yes_id, 1), ActionPayout(no_id, 0)),
                    AS_OF,
                ),
                TerminalAtom(
                    f"{contract}:no",
                    TerminalKind.NORMAL_NO,
                    "v1",
                    (ActionPayout(yes_id, 0), ActionPayout(no_id, 1)),
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
    # #117: rows carry proven fee-free endpoint facts for their contracts --
    # a component whose contracts have no fee fact at all is unmodelable and
    # is skipped whole, so every non-fee fixture states its fee facts.
    contracts = sorted({action.market_contract_id for action in compiled.actions})
    return {
        "identity": identity,
        "version_id": f"v-{identity}",
        "fingerprint": f"fp-{identity}",
        "activation": "ACTIVE",
        "relation_type": "IMPLIES",
        "endpoints": [
            {"venue": "polymarket", "contract_id": contract, "fees_enabled": False}
            for contract in contracts
        ],
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
    def __init__(
        self,
        unsettled: int = 0,
        max_unsettled: int = 5_000_000,
        qualification_policy_version: int = 1,
    ) -> None:
        self.unsettled = unsettled
        self.max_unsettled = max_unsettled
        self.qualification_policy_version = qualification_policy_version

    def n_leg_control(self) -> dict[str, object]:
        return {
            "total_unsettled_capital_units": self.unsettled,
            "qualification_policy_version": self.qualification_policy_version,
        }

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


def live_book(
    token_id: str,
    *,
    price: str = "0.49",
    confirmed_at: datetime | None = None,
) -> ThresholdOrderBook:
    level = (BookLevel(Decimal(price), Decimal("2")),)
    return ThresholdOrderBook(
        token_id, level, level, confirmed_at or datetime.now(UTC)
    )


def selected_component(
    component_id: str,
    *,
    relation_fingerprint: str = "r",
    terminal_fingerprint: str = "t",
    contract_ids: tuple[str, ...] = ("contract-a",),
    action_ids: tuple[str, ...] = ("a-yes", "a-no"),
) -> SelectedComponent:
    return SelectedComponent(
        component_id=component_id,
        contract_ids=contract_ids,
        constraint_ids=(),
        action_ids=action_ids,
        admission_score=20_000,
        portfolio=tuple(ActionQuantity(action_id, 1) for action_id in action_ids),
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


def valid_selected(
    rows: dict[str, object],
    *,
    contract_ids: tuple[str, ...] = ("contract-a",),
    action_ids: tuple[str, ...] = ("a-yes", "a-no"),
) -> SelectedComponent:
    problem, components = relation_generation_problem(rows)
    component = components[0]
    raw = problem_for_component(problem, component)
    return selected_component(
        component.component_id,
        relation_fingerprint=fingerprint({"constraint_model": raw.constraint_model}),
        terminal_fingerprint=fingerprint(
            {"terminal_state_sets": raw.terminal_state_sets}
        ),
        contract_ids=contract_ids,
        action_ids=action_ids,
    )


def negative_raw_problem(
    *, contract: str = "contract-n", yes_id: str = "n-yes", no_id: str = "n-no"
) -> ArbitrageProblem:
    """One contract whose every non-zero portfolio fails the $3 min-profit
    gate at 0.49 book prices: the exact component negative the oracle closes
    exhaustively (candidate=None evidence -> component proof request)."""
    problem = raw_problem(contract=contract, yes_id=yes_id, no_id=no_id)
    constraint = QualificationConstraint(
        constraint_id="q-min-profit",
        rule_version="v1",
        metric=QualificationMetric.GUARANTEED_PROFIT_UNITS,
        comparison=Comparison.GREATER_THAN_OR_EQUAL,
        threshold_numerator=3_000_000,
        threshold_denominator=1,
    )
    return replace(
        problem, qualification_constraints=(constraint,)
    )


def negative_evidence(problem: ArbitrageProblem) -> dict[str, object]:
    """Worker evidence with no candidate: the resolver must run the exact
    component proof request (the oracle negative path)."""
    evidence = SolverEvidence(
        native_status="INFEASIBLE",
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=False,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(),
        certificate=None,
    )
    return canonical_payload(evidence)


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


# --------------------------------------------------------------------------
# Issue #117 (S3): the resolver aggregates per-contract fee FACTS (state +
# rate) from the catalog generation endpoints, models the charging-market
# taker fee into the snapshot legs' taker_fee_bps (the cost slices are built
# from those books), and freezes a per-solution fee block at request-build
# time. A market whose rate is missing/unparseable/conflicting is
# unmodelable and the whole component is skipped -- no snapshot, no solve,
# no solutions() entry -- exactly like a book miss.
# --------------------------------------------------------------------------


def fee_endpoint(
    contract_id: str,
    *,
    fees_enabled: object = None,
    fee_rate: object = None,
    include_fees: bool = True,
) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "venue": "polymarket",
        "contract_id": contract_id,
    }
    if include_fees:
        endpoint["fees_enabled"] = fees_enabled
        endpoint["fee_rate"] = fee_rate
    return endpoint


def fee_row(
    identity: str,
    endpoints: list[dict[str, object]],
) -> dict[str, object]:
    base = row(identity, raw_problem())
    base["endpoints"] = endpoints
    return base


def token_row(
    identity: str,
    contract_id: str,
    yes_token_id: str,
    no_token_id: str,
) -> dict[str, object]:
    """A generation row whose endpoint carries handwritten token literals."""
    base = row(identity, raw_problem())
    base["endpoints"] = [
        {
            "venue": "polymarket",
            "contract_id": contract_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            # #117: proven fee-free fact so the resolver dispatches the
            # component (token mapping alone does not make a fee fact).
            "fees_enabled": False,
        }
    ]
    return base


class RecordingMonitor(FakeMonitor):
    """FakeMonitor that records every cross-venue token request."""

    def __init__(self, books: dict[str, ThresholdOrderBook] | None = None) -> None:
        super().__init__(books)
        self.requested: list[tuple[str, ...]] = []

    def cross_venue_books(self, token_ids: tuple[str, ...]) -> dict[str, ThresholdOrderBook]:
        self.requested.append(tuple(token_ids))
        return super().cross_venue_books(token_ids)


class SubscriptionMonitor(RecordingMonitor):
    """RecordingMonitor that also records N-leg share seeding (B4)."""

    def __init__(self, books: dict[str, ThresholdOrderBook] | None = None) -> None:
        super().__init__(books)
        self.subscriptions: list[set[str]] = []

    def set_n_leg_tokens(self, token_ids: object) -> None:
        self.subscriptions.append({str(token) for token in token_ids})


def resolved_solutions(
    tmp_path: Path,
    rows: dict[str, object],
    *,
    contract_ids: tuple[str, ...] = ("contract-a",),
) -> list[dict[str, object]]:
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
    )
    valid = valid_selected(rows, contract_ids=contract_ids)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    return instance.solutions()


def skipped_component(
    tmp_path: Path, rows: dict[str, object]
) -> tuple[PredictionLiveResolver, FakeServer, SelectedComponent]:
    """Reconcile a single-component selection whose fee facts are
    unmodelable; the caller asserts the whole-component skip."""
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._reconcile()
    return instance, server, valid


def test_solutions_report_fee_free_from_endpoint_facts(tmp_path: Path) -> None:
    """B4 (#117): fees_enabled=False is proven fee-free -- legs carry
    taker_fee_bps=Decimal("0"), the block is fully modeled with zero rate and
    zero units, and the economics equal the pre-#117 numbers."""
    rows = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=False)],
        )
    }
    instance, server, _ = resolver(
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
    assert all(leg.book.taker_fee_bps == Decimal("0") for leg in snapshot.legs)
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()

    (entry,) = instance.solutions()

    assert entry["fee"] == {
        "status": "fee_free",
        "charging_contracts": [],
        "unknown_contracts": [],
        "modeled": True,
        "taker_fee_rate_bps": 0,
        "taker_fee_units": 0,
    }
    assert entry["market"]["guaranteed_profit_units"] == 20_000


def test_solutions_report_fee_charging_when_enabled_or_rate_positive(
    tmp_path: Path,
) -> None:
    """B1 (#117): a modelable charging market (fees_enabled=True + rate, or
    fees_enabled=False with a positive rate) solves with the fee priced into
    the slices: snapshot legs carry taker_fee_bps=Decimal("400") and the
    frozen block is {fee_charging, modeled=True, 400 bps, units>0}."""
    for endpoints in (
        [fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.04")],
        [fee_endpoint("contract-a", fees_enabled=False, fee_rate="0.04")],
    ):
        rows = {"r:a": fee_row("r:a", endpoints)}
        instance, server, _ = resolver(
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
        assert all(leg.book.taker_fee_bps == Decimal("400") for leg in snapshot.legs)
        instance._tick()
        request = server.requests[0]
        # price 0.49 -> price term 490,000 + fee 0.04 x 490,000 x 510,000/1e6
        # = 9,996 -> 499,996 per lot (hand math, 1 share/lot).
        assert (
            request.request.problem.actions[0]
            .cost_slices[0]
            .incremental_cost_upper_bound_units
            == 499_996
        )
        server.futures[0].set_result(
            worker_outcome(request, worker_evidence(request.request.problem))
        )
        instance._tick()

        (entry,) = instance.solutions()
        assert entry["fee"]["status"] == "fee_charging"
        assert entry["fee"]["charging_contracts"] == ["contract-a"]
        assert entry["fee"]["unknown_contracts"] == []
        assert entry["fee"]["modeled"] is True
        assert entry["fee"]["taker_fee_rate_bps"] == 400
        assert entry["fee"]["taker_fee_units"] > 0


def test_unmodelable_rate_skips_the_whole_component(tmp_path: Path) -> None:
    """B2 (#117): fees_enabled=True with a missing fee_rate is unmodelable
    (the #112 gate read it as charging; #117 cannot bound the fee), so the
    component is skipped like a book miss: no snapshot, no dispatch, and no
    solutions() entry."""
    rows = {
        "r:a": fee_row("r:a", [fee_endpoint("contract-a", fees_enabled=True)])
    }
    instance, server, valid = skipped_component(tmp_path, rows)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []


def test_conflicting_rates_skip_the_component(tmp_path: Path) -> None:
    """B3 (#117): two endpoints for one contract quoting rates 0.04 vs 0.05
    disagree on the fee fact -> unmodelable -> skipped, no solve."""
    rows = {
        "r:a": fee_row(
            "r:a",
            [
                fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.04"),
                fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.05"),
            ],
        )
    }
    instance, server, valid = skipped_component(tmp_path, rows)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []


def test_solution_fee_block_stays_frozen_across_catalog_rotation(
    tmp_path: Path,
) -> None:
    """B5 (#117/Q3): after a solution exists, a new catalog batch moving the
    rate to 0.05 reconciles into a new generation; solutions() must keep
    replaying the frozen request-time fee block (400 bps) instead of
    recomputing it from the current facts."""
    rows = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.04")],
        )
    }
    instance, server, catalog = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    frozen = instance.solutions()[0]["fee"]
    assert frozen["taker_fee_rate_bps"] == 400

    catalog.rows = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.05")],
        )
    }
    catalog.generation = 2
    instance._tick()

    assert instance.solutions()[0]["fee"] == frozen
    assert instance.solutions()[0]["fee"]["taker_fee_rate_bps"] == 400


def stale_pending_block_after_rotation(
    tmp_path: Path,
    *,
    rows: dict[str, object],
    rotated_rows: dict[str, object],
) -> tuple[PredictionLiveResolver, FakeServer, object]:
    """Pin the snapshot->dispatch window (#117/D3): R1 dispatches, a newer
    book parks a stale-stamped pending snapshot in the scheduler, the
    resolver loop then reconciles a fee-rotated catalog generation (a bare
    ``_reconcile`` -- no scheduler refresh, exactly the loop interleave
    between snapshot stamping and request build), and R1's completion
    dispatches the stale snapshot as R2. Returns the resolver, server, and
    the dispatched R2, whose fee block must follow the STAMPED legs."""
    monitor = FakeMonitor({"contract-a": live_book("contract-a")})
    instance, server, catalog = resolver(tmp_path, rows=rows, monitor=monitor)
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    first = server.requests[0]
    assert len(server.requests) == 1
    # A newer book changes the economic fingerprint: the scheduler parks the
    # newest snapshot as pending behind the in-flight solve -- stamped with
    # the PRE-rotation fee facts.
    monitor.books["contract-a"] = live_book("contract-a", price="0.50")
    instance._tick()
    assert len(server.requests) == 1
    # The loop reconciles a fee rotation between the pending stamp and its
    # dispatch: only the facts map turns over, the parked snapshot does not.
    catalog.rows = rotated_rows
    catalog.generation = 2
    instance._reconcile()
    server.futures[0].set_result(
        worker_outcome(first, worker_evidence(first.request.problem))
    )
    dispatched = server.requests[1]
    return instance, server, dispatched


def test_fee_block_follows_stale_snapshot_legs_when_facts_rotate_to_charging(
    tmp_path: Path,
) -> None:
    """R2a (#117/D3): a component stamped fee-free (snapshot legs at 0 bps)
    keeps narrating fee_free even when the catalog facts rotate to charging
    between the snapshot stamp and the pending dispatch -- the frozen block
    comes from the same stamped legs as the slices, never fee_charging."""
    rows = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=False)],
        )
    }
    rotated = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.04")],
        )
    }
    instance, _, dispatched = stale_pending_block_after_rotation(
        tmp_path, rows=rows, rotated_rows=rotated
    )

    # the slices follow the STAMPED (fee-free) legs: 0.50, no fee term
    assert (
        dispatched.request.problem.actions[0]
        .cost_slices[0]
        .incremental_cost_upper_bound_units
        == 500_000
    )
    # so the block must follow the same legs -- consistent, never fee_charging
    assert instance._request_components[dispatched.request_id][2] == {
        "status": "fee_free",
        "charging_contracts": [],
        "unknown_contracts": [],
        "modeled": True,
        "taker_fee_rate_bps": 0,
        "taker_fee_units": 0,
    }


def test_fee_block_follows_stale_snapshot_legs_when_facts_rotate_to_free(
    tmp_path: Path,
) -> None:
    """R2b (#117/D3, inverse): legs stamped charging at 400 bps keep the
    block fee_charging with the stamped rate and units after the catalog
    facts rotate to fees_enabled=False -- block and slices agree on the OLD
    economics until the next solve refreshes both."""
    rows = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=True, fee_rate="0.04")],
        )
    }
    rotated = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=False)],
        )
    }
    instance, _, dispatched = stale_pending_block_after_rotation(
        tmp_path, rows=rows, rotated_rows=rotated
    )

    # the slices follow the STAMPED (charging) legs: 0.50 + 0.04 x 0.5 x 0.5
    assert (
        dispatched.request.problem.actions[0]
        .cost_slices[0]
        .incremental_cost_upper_bound_units
        == 510_000
    )
    assert instance._request_components[dispatched.request_id][2] == {
        "status": "fee_charging",
        "charging_contracts": ["contract-a"],
        "unknown_contracts": [],
        "modeled": True,
        "taker_fee_rate_bps": 400,
        # both legs stamped charging: 2 x 0.04 x 0.5 x 0.5 x 1e6 per lot
        "taker_fee_units": 20_000,
    }


def test_unmodelable_fee_facts_skip_instead_of_presenting_unknown(
    tmp_path: Path,
) -> None:
    """#117 flip of the #112 fail-closed cases: a missing fees_enabled key,
    an unparseable rate, and a contract absent from the generation endpoints
    are all unmodelable -- the component no longer solves and presents a
    fee_unknown row; it is skipped whole (no dispatch, no solution)."""
    missing = {
        "r:a": fee_row("r:a", [fee_endpoint("contract-a", include_fees=False)])
    }
    instance, server, valid = skipped_component(tmp_path, missing)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []

    unparseable = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-a", fees_enabled=False, fee_rate="abc")],
        )
    }
    instance, server, valid = skipped_component(tmp_path, unparseable)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []

    # a component contract absent from the generation endpoints has no fee
    # fact at all: the component is skipped, never solved as fee-free
    foreign = {
        "r:a": fee_row(
            "r:a",
            [fee_endpoint("contract-other", fees_enabled=False)],
        )
    }
    instance, server, valid = skipped_component(tmp_path, foreign)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []


def test_conflicting_fee_states_skip_the_component(tmp_path: Path) -> None:
    """#117 flip: one contract whose endpoints disagree on fees_enabled
    (False vs True) is unmodelable -> skipped whole, no solve."""
    rows = {
        "r:a": fee_row(
            "r:a", [fee_endpoint("contract-a", fees_enabled=False)]
        ),
        "r:b": fee_row(
            "r:b", [fee_endpoint("contract-a", fees_enabled=True)]
        ),
    }
    instance, server, valid = skipped_component(tmp_path, rows)
    assert instance._snapshot_for(valid) is None
    instance._tick()
    assert server.requests == []
    assert instance.solutions() == []


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
    # #117 fee-aware: the row's proven fee-free endpoint facts put 0 bps on
    # every leg book (unmodelable facts would have skipped the component).
    assert all(leg.book.available and leg.book.taker_fee_bps == Decimal("0") for leg in snapshot.legs)
    assert all(leg.received_at is not None and leg.sequence is not None for leg in snapshot.legs)

    instance._problem_map[component_id] = raw_problem(venue="predict")
    assert instance._snapshot_for(valid_selected(rows)) is None

    instance._problem_map[component_id] = raw_problem()
    instance._monitor = FakeMonitor()
    assert instance._snapshot_for(valid_selected(rows)) is None


def test_snapshot_resolves_leg_tokens_by_action_direction(tmp_path: Path) -> None:
    """B1: one contract with BUY_YES+BUY_NO actions and a token map reads each
    leg's own direction token; the request set is exactly the handwritten
    {yes,no} literals and the two legs of the same market differ."""
    rows = {"r:a": token_row("r:a", "contract-a", "yes-token-a", "no-token-a")}
    monitor = RecordingMonitor({
        "yes-token-a": live_book("yes-token-a", price="0.49"),
        "no-token-a": live_book("no-token-a", price="0.61"),
    })
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()

    snapshot = instance._snapshot_for(valid_selected(rows))

    assert snapshot is not None
    assert {leg.leg_id for leg in snapshot.legs} == {"a-yes", "a-no"}
    requested = {token for call in monitor.requested for token in call}
    assert requested == {"yes-token-a", "no-token-a"}
    prices = {leg.leg_id: leg.book.asks[0].price for leg in snapshot.legs}
    assert prices == {"a-yes": Decimal("0.49"), "a-no": Decimal("0.61")}


def test_snapshot_combines_direction_tokens_with_charging_fee_facts(
    tmp_path: Path,
) -> None:
    """#114+#117 composition: each leg's book is read by its direction-resolved
    CLOB token while the 400 bps fee fact is found by contract id."""
    charging = {
        "venue": "polymarket",
        "contract_id": "contract-a",
        "yes_token_id": "yes-token-a",
        "no_token_id": "no-token-a",
        "fees_enabled": True,
        "fee_rate": "0.04",
    }
    base = row("r:a", raw_problem())
    base["endpoints"] = [charging]
    rows = {"r:a": base}
    monitor = RecordingMonitor({
        "yes-token-a": live_book("yes-token-a", price="0.48"),
        "no-token-a": live_book("no-token-a", price="0.52"),
    })
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()

    snapshot = instance._snapshot_for(valid_selected(rows))

    assert snapshot is not None
    requested = {token for call in monitor.requested for token in call}
    assert requested == {"yes-token-a", "no-token-a"}
    assert all(leg.book.taker_fee_bps == Decimal("400") for leg in snapshot.legs)


def test_snapshot_without_leg_map_fails_closed_for_implies(tmp_path: Path) -> None:
    """B2: an IMPLIES action with no token mapping falls back to the condition
    id, finds no book under that key, and the snapshot fails closed None."""
    rows = {"r:a": row("r:a", raw_problem())}
    monitor = RecordingMonitor({"unrelated-token": live_book("unrelated-token")})
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()

    snapshot = instance._snapshot_for(valid_selected(rows))

    assert snapshot is None
    requested = {token for call in monitor.requested for token in call}
    assert requested == {"contract-a"}


def test_snapshot_without_leg_map_requests_contract_ids(tmp_path: Path) -> None:
    """B3: with no token mapping the request token stays market_contract_id —
    the mechanical contract-is-token invariant does not regress."""
    rows = {"r:a": row("r:a", raw_problem())}
    monitor = RecordingMonitor({"contract-a": live_book("contract-a")})
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()

    snapshot = instance._snapshot_for(valid_selected(rows))

    assert snapshot is not None
    assert {leg.leg_id for leg in snapshot.legs} == {"a-yes", "a-no"}
    requested = {token for call in monitor.requested for token in call}
    assert requested == {"contract-a"}


def test_reconcile_seeds_monitor_cross_venue_subscription(tmp_path: Path) -> None:
    """B4: after reconcile the resolver seeds its monitor's N-leg share
    (`set_n_leg_tokens` — parallel to the cross-venue engine's share on a
    shared monitor, #114 P1) with the full resolved token set."""
    rows = {"r:a": token_row("r:a", "contract-a", "yes-token-a", "no-token-a")}
    monitor = SubscriptionMonitor({
        "yes-token-a": live_book("yes-token-a"),
        "no-token-a": live_book("no-token-a"),
    })
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)

    instance._reconcile()

    assert monitor.subscriptions == [{"yes-token-a", "no-token-a"}]


def test_conflicting_token_facts_drop_the_contract_mapping(tmp_path: Path) -> None:
    """Issue #114 decision 8: two generation rows disagreeing about one
    contract's tokens drop that mapping entirely — the resolver then falls
    back to the contract id and finds no book (never guesses a direction)."""

    conflicting = token_row("r:b", "contract-a", "other-yes", "other-no")
    rows = {
        "r:a": token_row("r:a", "contract-a", "yes-token-a", "no-token-a"),
        "r:b": conflicting,
    }
    monitor = RecordingMonitor({
        "yes-token-a": live_book("yes-token-a"),
        "no-token-a": live_book("no-token-a"),
    })
    instance, _, _ = resolver(tmp_path, rows=rows, monitor=monitor)
    instance._reconcile()

    # Both rows compile into one component over contract-a; the mapping for
    # contract-a is dropped, so the request falls back to the condition id.
    selected = valid_selected(rows)
    assert instance._snapshot_for(selected) is None
    requested = {token for call in monitor.requested for token in call}
    assert "contract-a" in requested
    assert "yes-token-a" not in requested


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
    assert instance._request_components[request.request_id][0] == component_id


def worker_evidence(
    problem: ArbitrageProblem,
    *,
    action_ids: tuple[str, ...] = ("a-yes", "a-no"),
) -> dict[str, object]:
    quantities = tuple(ActionQuantity(action_id, 1) for action_id in action_ids)
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
    # #74: the synchronous proof ran on the live hot path; the 2-leg
    # contract-a adversary closes at a worst-case loss of 490,000 against a
    # zero cap, so the decision is UNSAFE and the record is queryable.
    assert execution["partial_fill_proof"] == "PARTIAL_FILL_UNSAFE"
    proof = instance.latest_partial_fill_proof(valid.component_id)
    assert proof is not None
    assert proof["status"] == "PARTIAL_FILL_UNSAFE"
    assert proof["solver_termination"] == "CLOSED"
    assert proof["verifier_status"] == "QUALIFIED_VERIFIED"
    assert proof["solver_lower_bound"] == proof["solver_upper_bound"] == 490_000
    assert proof["max_partial_fill_loss"] == 0
    assert proof["cap_config_version"] == "caps-v1"
    assert instance.latest_execution(valid.component_id) is not None


def test_proof_persists_to_real_store_and_replays_without_resolve(
    tmp_path: Path,
) -> None:
    import open_trader.prediction_live_resolver as resolver_module

    rows = {"r:a": row("r:a", raw_problem())}
    store = PredictionArbitrageStore(tmp_path / "data")
    store.n_leg_safety_config_write(
        1, {"max_total_unsettled_capital_units": 5_000_000}
    )
    instance, server, _ = resolver(
        tmp_path / "run1",
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
        store=store,
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    proof = instance.latest_partial_fill_proof(valid.component_id)
    assert proof is not None and proof["status"] == "PARTIAL_FILL_UNSAFE"
    # Rows are keyed by the stable adversary fingerprint (read-before-solve
    # key), not the record's own fingerprint.
    cache_key = instance._fill_proof_components[valid.component_id]
    persisted = store.partial_fill_proof(cache_key)
    assert persisted is not None
    assert persisted["proof"] == proof
    assert persisted["proof_fingerprint"] == cache_key

    # A fresh resolver on the same store replays the persisted proof instead
    # of re-running the fill-adversary solver (read-before-solve).
    calls = {"n": 0}
    original = resolver_module.prove_partial_fill

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    resolver_module.prove_partial_fill = counting
    try:
        instance2, server2, _ = resolver(
            tmp_path / "run2",
            rows=rows,
            monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
            execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
            store=store,
        )
        instance2._selection_store.save({valid.component_id: valid})
        instance2._tick()
        request2 = server2.requests[0]
        server2.futures[0].set_result(
            worker_outcome(request2, worker_evidence(request2.request.problem))
        )
        instance2._tick()
        assert instance2.latest_partial_fill_proof(valid.component_id) == proof
        assert calls["n"] == 0
    finally:
        resolver_module.prove_partial_fill = original


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


def test_stale_inflight_outcome_is_dropped_after_generation_prunes_component(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, catalog = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert instance._request_components[request.request_id][0] == valid.component_id

    catalog.rows = {}
    catalog.generation = 2
    instance._tick()
    assert valid.component_id not in instance._selection
    assert request.request_id not in instance._request_components

    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    assert instance.solutions() == []


def test_exceptional_worker_future_does_not_leak_request_components(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    instance, server, _ = resolver(
        tmp_path,
        rows=rows,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    assert len(server.futures) == 1
    request = server.requests[0]
    server.futures[0].set_exception(RuntimeError("worker startup failed"))
    instance._tick()
    assert instance._request_components == {}
    assert instance.solutions() == []


def test_account_view_cache_covers_unavailable_case(tmp_path: Path) -> None:
    class CountingExecution(FakeExecution):
        def __init__(self, view=None) -> None:
            super().__init__(view)
            self.calls = 0

        def n_leg_account_view(self):
            self.calls += 1
            return self.view

    execution = CountingExecution(None)
    instance, _, _ = resolver(tmp_path, execution=execution)

    assert instance._account_view() is None
    assert execution.calls == 1
    assert instance._account_view() is None
    assert execution.calls == 1

    execution.view = AccountView(2_000_000, 2_000_000, 0)
    assert instance._account_view() is None
    assert execution.calls == 1

    instance._account_view_cached_at = datetime.now(UTC) - timedelta(seconds=61)
    refreshed = instance._account_view()
    assert refreshed == AccountView(2_000_000, 2_000_000, 0)
    assert execution.calls == 2


def test_normalize_problem_fails_closed_on_unknown_payout_scale() -> None:
    problem = raw_problem()
    states = tuple(
        replace(
            state,
            atoms=tuple(
                replace(
                    atom,
                    payouts=tuple(
                        ActionPayout(payout.action_id, 2)
                        if payout.action_id == "a-yes"
                        else payout
                        for payout in atom.payouts
                    ),
                )
                for atom in state.atoms
            ),
        )
        for state in problem.terminal_state_sets
    )
    scaled_dollar = replace(problem, terminal_state_sets=states)
    with pytest.raises(ValueError, match="unsupported payout scale"):
        normalize_problem(scaled_dollar)


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


def conflicting_rows() -> dict[str, object]:
    """Two ACTIVE rows sharing action ``a-yes`` with conflicting definitions.

    This reproduces the #74 production incident: one action compiled under
    two inconsistent relations makes ``relation_generation_problem`` raise
    ``ValueError`` at the shared compile seam.
    """
    base = raw_problem()
    conflicting = replace(
        base,
        actions=tuple(
            replace(action, max_quantity_lots=1)
            if action.action_id == "a-yes"
            else action
            for action in base.actions
        ),
    )
    return {"r:one": row("r:one", base), "r:two": row("r:two", conflicting)}


class EpisodeClock:
    """Mutable injected clock for episode timing tests."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


def episode_resolver(
    tmp_path: Path,
    rows: dict[str, object],
    clock: EpisodeClock,
    tracker: EpisodeTracker,
    monitor: FakeMonitor | None = None,
) -> tuple[PredictionLiveResolver, FakeServer, FakeCatalog]:
    server = FakeServer()
    catalog = FakeCatalog(rows)
    instance = PredictionLiveResolver(
        data_dir=tmp_path,
        relation_catalog=catalog,
        monitor=monitor or FakeMonitor(),
        solver_server=server,
        selection_store=MonitorSelectionStore(tmp_path),
        store=FakeStore(),
        execution=FakeExecution(AccountView(1_000_000, 1_000_000, 0)),
        poll_interval=0.01,
        now_fn=clock,
        episode_tracker=tracker,
    )
    return instance, server, catalog


class RecordingEpisodeTracker(EpisodeTracker):
    """EpisodeTracker that records the outcomes the resolver reports to it."""

    def __init__(self, *, now_fn) -> None:
        super().__init__(now_fn=now_fn)
        self.qualified_reports: list[dict[str, object]] = []
        self.negative_reports: list[dict[str, object]] = []

    def observe_qualified(
        self, component_id, lineage_id, profit, would_submit,
        plan_snapshot, fingerprints, now,
    ) -> None:
        self.qualified_reports.append(dict(fingerprints))
        super().observe_qualified(
            component_id, lineage_id, profit, would_submit,
            plan_snapshot, fingerprints, now,
        )

    def observe_negative(
        self,
        component_id,
        *,
        proof_fingerprint,
        generation,
        model_fingerprint,
        quote_fingerprint,
        qualification_fingerprint,
        binding_matches,
        quote_fresh,
        gap_seconds,
        now,
        qualification_policy_version=None,
    ) -> None:
        self.negative_reports.append(
            {
                "generation": generation,
                "qualification_policy_version": qualification_policy_version,
            }
        )
        super().observe_negative(
            component_id,
            proof_fingerprint=proof_fingerprint,
            generation=generation,
            model_fingerprint=model_fingerprint,
            quote_fingerprint=quote_fingerprint,
            qualification_fingerprint=qualification_fingerprint,
            binding_matches=binding_matches,
            quote_fresh=quote_fresh,
            gap_seconds=gap_seconds,
            now=now,
            qualification_policy_version=qualification_policy_version,
        )


def test_t12_qualified_outcome_opens_an_ongoing_episode(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    clock = EpisodeClock()
    tracker = EpisodeTracker(now_fn=clock)
    instance, server, _ = episode_resolver(
        tmp_path,
        rows,
        clock,
        tracker,
        monitor=FakeMonitor({"contract-a": live_book("contract-a")}),
    )
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()

    episodes = instance.n_leg_episodes()
    assert episodes[valid.component_id]["status"] == "ONGOING"


def test_t15_stale_quote_on_quiet_book_resets_the_close_timer(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    clock = EpisodeClock()
    tracker = EpisodeTracker(now_fn=clock)
    monitor = FakeMonitor(
        {"contract-a": live_book("contract-a", confirmed_at=clock.now)}
    )
    instance, server, catalog = episode_resolver(
        tmp_path, rows, clock, tracker, monitor=monitor
    )
    valid = valid_selected(rows)
    component_id = valid.component_id
    instance._selection_store.save({component_id: valid})
    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(request, worker_evidence(request.request.problem))
    )
    instance._tick()
    assert instance.n_leg_episodes()[component_id]["status"] == "ONGOING"

    # Generation 2 tightens qualification; the structure is unchanged, so the
    # same component now proves negative.
    catalog.rows = {
        "r:a": row(
            "r:a",
            negative_raw_problem(
                contract="contract-a", yes_id="a-yes", no_id="a-no"
            ),
        )
    }
    catalog.generation = 2

    # One accepted negative starts the close window at +60.
    clock.now = clock.now + timedelta(seconds=60)
    monitor.books["contract-a"] = live_book(
        "contract-a", price="0.50", confirmed_at=clock.now
    )
    instance._tick()
    negative_request = server.requests[-1]
    server.futures[-1].set_result(
        worker_outcome(negative_request, negative_evidence(negative_request.request.problem))
    )
    instance._tick()
    assert (
        tracker.episode(component_id).negative_close_started_at == clock.now
    )

    # The book goes quiet and ages out: the tick-level freshness check must
    # clear the window without any new evidence events.
    stale_at = clock.advance(240)
    instance._tick()
    episode = tracker.episode(component_id)
    assert episode.status == "ONGOING"
    assert episode.negative_close_started_at is None

    # A fresh negative right after the clear cannot close (it restarts the
    # window at its own timestamp).
    clock.now = stale_at + timedelta(seconds=60)
    monitor.books["contract-a"] = live_book(
        "contract-a", price="0.51", confirmed_at=clock.now
    )
    instance._tick()
    fresh_request = server.requests[-1]
    server.futures[-1].set_result(
        worker_outcome(fresh_request, negative_evidence(fresh_request.request.problem))
    )
    instance._tick()
    projection = instance.n_leg_episodes()[component_id]
    assert projection["status"] == "ONGOING"


def test_t14_reconcile_removal_closes_the_episode_as_retired(
    tmp_path: Path,
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    clock = EpisodeClock()
    tracker = EpisodeTracker(now_fn=clock)
    instance, _, catalog = episode_resolver(tmp_path, rows, clock, tracker)
    valid = valid_selected(rows)
    instance._selection_store.save({valid.component_id: valid})
    instance._reconcile()
    tracker.observe_qualified(
        valid.component_id,
        "L1",
        Decimal("1.20"),
        False,
        None,
        {
            "component_generation": 1,
            "model_fingerprint": "sha256:model",
            "quote_fingerprint": "sha256:quote",
            "qualification_fingerprint": "sha256:qual",
            "qualification_policy_version": "v1",
        },
        clock.now,
    )
    catalog.rows = {}
    catalog.generation = 2
    retire_at = clock.advance(30)
    instance._reconcile()
    projection = instance.n_leg_episodes()[valid.component_id]
    assert projection["status"] == "CLOSED"
    assert projection["close_reason"] == CLOSE_COMPONENT_RETIRED
    assert tracker.episode(valid.component_id).closed_at == retire_at


def test_t13_five_minutes_of_fresh_negatives_close_the_episode(
    tmp_path: Path,
) -> None:
    rows = {"r:n": row("r:n", raw_problem(contract="contract-n", yes_id="n-yes", no_id="n-no"))}
    clock = EpisodeClock()
    tracker = EpisodeTracker(now_fn=clock)
    monitor = FakeMonitor(
        {"contract-n": live_book("contract-n", confirmed_at=clock.now)}
    )
    instance, server, catalog = episode_resolver(
        tmp_path, rows, clock, tracker, monitor=monitor
    )
    valid = valid_selected(
        rows, contract_ids=("contract-n",), action_ids=("n-yes", "n-no")
    )
    component_id = valid.component_id
    instance._selection_store.save({component_id: valid})

    def qualified_cycle() -> None:
        instance._tick()
        request = server.requests[-1]
        server.futures[-1].set_result(
            worker_outcome(request, worker_evidence(request.request.problem))
        )
        instance._tick()

    def negative_cycle(price: str, at: datetime) -> None:
        clock.now = at
        monitor.books["contract-n"] = live_book(
            "contract-n", price=price, confirmed_at=at
        )
        instance._tick()
        request = server.requests[-1]
        server.futures[-1].set_result(
            worker_outcome(request, negative_evidence(request.request.problem))
        )
        instance._tick()

    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(
            request,
            worker_evidence(
                request.request.problem, action_ids=("n-yes", "n-no")
            ),
        )
    )
    instance._tick()
    assert instance.n_leg_episodes()[component_id]["status"] == "ONGOING"

    # Generation 2 tightens the qualification policy: same structure, so the
    # selection is kept and the same component now proves negative.
    catalog.rows = {"r:n": row("r:n", negative_raw_problem())}
    catalog.generation = 2
    opened_at = clock.now
    negative_cycle("0.50", opened_at + timedelta(seconds=60))
    assert (
        tracker.episode(component_id).negative_close_started_at
        == opened_at + timedelta(seconds=60)
    )
    negative_cycle("0.51", opened_at + timedelta(seconds=240))
    assert instance.n_leg_episodes()[component_id]["status"] == "ONGOING"
    negative_cycle("0.52", opened_at + timedelta(seconds=300))
    assert instance.n_leg_episodes()[component_id]["status"] == "ONGOING"
    negative_cycle("0.53", opened_at + timedelta(seconds=360))
    projection = instance.n_leg_episodes()[component_id]
    assert projection["status"] == "CLOSED"
    assert projection["close_reason"] == CLOSE_NO_QUALIFIED_OPPORTUNITY


def test_t21_resolver_reports_the_real_qualification_policy_version(
    tmp_path: Path,
) -> None:
    rows = {"r:n": row("r:n", raw_problem(contract="contract-n", yes_id="n-yes", no_id="n-no"))}
    clock = EpisodeClock()
    tracker = RecordingEpisodeTracker(now_fn=clock)
    monitor = FakeMonitor(
        {"contract-n": live_book("contract-n", confirmed_at=clock.now)}
    )
    instance, server, catalog = episode_resolver(
        tmp_path, rows, clock, tracker, monitor=monitor
    )
    valid = valid_selected(
        rows, contract_ids=("contract-n",), action_ids=("n-yes", "n-no")
    )
    component_id = valid.component_id
    instance._selection_store.save({component_id: valid})

    instance._tick()
    request = server.requests[0]
    server.futures[0].set_result(
        worker_outcome(
            request,
            worker_evidence(
                request.request.problem, action_ids=("n-yes", "n-no")
            ),
        )
    )
    instance._tick()

    # The qualified report carries the version from the controls row (the
    # FakeStore's n_leg_control qualification_policy_version=1), not a
    # hardcoded None.
    assert tracker.qualified_reports
    assert tracker.qualified_reports[-1]["qualification_policy_version"] == "1"

    # The negative report carries the same real version.
    catalog.rows = {"r:n": row("r:n", negative_raw_problem())}
    catalog.generation = 2
    clock.now = clock.now + timedelta(seconds=60)
    monitor.books["contract-n"] = live_book(
        "contract-n", price="0.50", confirmed_at=clock.now
    )
    instance._tick()
    negative_request = server.requests[-1]
    server.futures[-1].set_result(
        worker_outcome(
            negative_request, negative_evidence(negative_request.request.problem)
        )
    )
    instance._tick()
    assert tracker.negative_reports
    assert tracker.negative_reports[-1]["qualification_policy_version"] == "1"


def test_start_survives_startup_reconcile_conflict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rows = conflicting_rows()
    # Guard: the fixture must exercise the production failure mode itself.
    with pytest.raises(ValueError, match="conflicts across compiled relations"):
        relation_generation_problem(rows)
    instance, server, _ = resolver(tmp_path, rows=rows)
    with caplog.at_level(
        logging.ERROR, logger="open_trader.prediction_live_resolver"
    ):
        instance.start()
    assert "startup reconcile failed" in caplog.text
    thread = instance._thread
    assert thread is not None and thread.is_alive()
    instance.stop()
    instance.stop()
    assert instance._thread is None
    assert server.requests == []
