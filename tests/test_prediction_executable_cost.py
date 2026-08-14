from datetime import UTC, datetime, timedelta
from dataclasses import replace
from decimal import Decimal

import open_trader.prediction_executable_cost as executable_cost
import pytest

from open_trader.prediction_executable_cost import (
    ResolutionStatus,
    AccountBalance,
    AccountSnapshot,
    BookBinding,
    component_fingerprint,
    execution_solution_from_payload,
    ImmutableBook,
    VerifiedComponent,
    market_solution_from_payload,
    resolve_component,
)
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.predict_source import PredictBook
from open_trader.prediction_n_leg_oracle import evaluate_fixed_portfolio
from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    OBSERVATION_SCHEMA_V1,
    OracleBudget,
    OracleRequest,
    PortfolioCandidate,
    PROBLEM_SCHEMA_V1,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    SearchMode,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
)
from open_trader.prediction_solver import BenchmarkLimits, ObjectiveBounds, SolverEvidence
from test_prediction_solver import BruteForceBackend
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    CandidateEvidence,
    model_fingerprint,
    proof_input_from_payload,
)


def test_unknown_status_is_explicit() -> None:
    assert ResolutionStatus.UNKNOWN.value == "UNKNOWN"


AS_OF = datetime(2026, 8, 14, tzinfo=UTC)


def component(book_kind: str = "polymarket") -> VerifiedComponent:
    key = SettlementObservationKey(OBSERVATION_SCHEMA_V1, "oracle", "indicator", AS_OF, AS_OF, "UTC", "rules-v1")
    action = CandidateAction(
        "action-a", "venue-a", "account-a", "chain-a", "contract-a", key, ActionSide.BUY_YES,
        1, 1, 1, 1, "usd-cents", "usd-cents", "usd-cents-v1", (ExecutableCostSlice(1, 1, 1),),
    )
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1, "component-a", AS_OF, "usd-cents", (action,),
        (TerminalStateSet("contract-a", key, "rules-v1", (
            TerminalAtom("yes", TerminalKind.NORMAL_YES, "rules-v1", (ActionPayout("action-a", 200),), AS_OF + timedelta(days=1)),
        )),),
        ConstraintModel((), ()),
        (QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 1, 1),),
    )
    return VerifiedComponent(problem, component_fingerprint(problem, (binding(book_kind),), 100), 100, (binding(book_kind),))


def binding(book_kind: str = "polymarket") -> BookBinding:
    return BookBinding("action-a", "venue-a", "action-a", book_kind, "fee-v1", 100_000, 1, 100_000, 100)


def two_leg_component() -> VerifiedComponent:
    base = component().problem
    action_b = replace(base.actions[0], action_id="action-b", venue_id="venue-b", account_id="account-b")
    states = tuple(replace(state, atoms=tuple(
        replace(atom, payouts=(ActionPayout("action-a", 200), ActionPayout("action-b", 200))) for atom in state.atoms
    )) for state in base.terminal_state_sets)
    problem = replace(base, actions=(base.actions[0], action_b), terminal_state_sets=states)
    return VerifiedComponent(
        problem, component_fingerprint(problem, (binding(), BookBinding("action-b", "venue-b", "action-b", "polymarket", "fee-v1", 100_000, 1, 100_000, 100)), 100), 100,
        (binding(), BookBinding("action-b", "venue-b", "action-b", "polymarket", "fee-v1", 100_000, 1, 100_000, 100)),
    )


def two_leg_books() -> tuple[ImmutableBook, ImmutableBook]:
    return executable_book(), ImmutableBook("action-b", "action-b", ThresholdOrderBook("action-b", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF), 100_000, 1, 100_000, 100, "venue-b", "fee-v1")


def decode_market(payload):
    return executable_cost.market_solution_from_payload(payload, component=component(), books=(executable_book(),), now=AS_OF)


def decode_execution(payload, market):
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    return executable_cost.execution_solution_from_payload(payload, market_solution=market, account_snapshot=account, now=AS_OF)


def test_missing_book_fails_closed_before_solving() -> None:
    result = resolve_component(
        component(), (), None,
        BenchmarkLimits(100, 200, 1_000_000, 4),
        OracleBudget(2, 2, 2),
        now=AS_OF,
    )

    assert result.status is ResolutionStatus.UNKNOWN
    assert result.failure_reason == "BOOK_STATE_UNKNOWN"
    assert result.market_solution is None


def test_predict_book_requires_both_fresh_source_and_receipt_timestamps(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    fresh = ImmutableBook(
        "action-a", "action-a", PredictBook("action-a", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF, AS_OF),
        100_000, 1, 100_000, 100, "venue-a", "fee-v1",
    )
    stale_receipt = replace(fresh, book=replace(fresh.book, received_at=AS_OF - timedelta(seconds=11)))

    assert resolve_component(component("predict.fun"), (fresh,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.EXECUTION_SOLUTION
    assert resolve_component(component("predict.fun"), (stale_receipt,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.UNKNOWN


def test_wrong_venue_or_native_book_identity_fails_closed(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    wrong_venue = replace(executable_book(), venue_id="venue-b")
    wrong_native = replace(executable_book(), native_id="other")

    for book in (wrong_venue, wrong_native):
        assert resolve_component(component(), (book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.UNKNOWN


def test_component_owned_book_policy_rejects_self_authorized_cost_or_kind(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    unauthorized_zero_fee = replace(executable_book(), fee_ppm=0, fee_rule_id="caller-says-zero")
    wrong_kind = ImmutableBook(
        "action-a", "action-a", PredictBook("action-a", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF, AS_OF),
        100_000, 1, 100_000, 100, "venue-a", "fee-v1",
    )

    for book in (unauthorized_zero_fee, wrong_kind):
        assert resolve_component(component(), (book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.UNKNOWN


def test_malformed_book_ask_containers_fail_closed_before_iteration(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    threshold = replace(executable_book(), book=ThresholdOrderBook("action-a", [], (), AS_OF))
    predict = ImmutableBook("action-a", "action-a", PredictBook("action-a", [], (), AS_OF, AS_OF), 100_000, 1, 100_000, 100, "venue-a", "fee-v1")

    assert resolve_component(component(), (threshold,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.UNKNOWN
    assert resolve_component(component("predict.fun"), (predict,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF).status is ResolutionStatus.UNKNOWN


def test_predict_receipt_refresh_reuses_identical_economic_market_solution(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    first_book = ImmutableBook("action-a", "action-a", PredictBook("action-a", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF, AS_OF), 100_000, 1, 100_000, 100, "venue-a", "fee-v1")
    first = resolve_component(component("predict.fun"), (first_book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    now = AS_OF + timedelta(seconds=1)
    refreshed_book = replace(first_book, book=replace(first_book.book, received_at=now))

    refreshed = resolve_component(component("predict.fun"), (refreshed_book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=now, prior_market_solution=first.market_solution)

    assert refreshed.market_solution == first.market_solution
    assert captured["calls"] == 1


def test_prior_market_solution_re_solves_when_visible_depth_changes(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    first = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    changed = replace(executable_book(), book=ThresholdOrderBook("action-a", (BookLevel(Decimal("0.41"), Decimal("1")),), (), AS_OF))

    refreshed = resolve_component(component(), (changed,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF, prior_market_solution=first.market_solution)

    assert refreshed.status is ResolutionStatus.EXECUTION_SOLUTION
    assert captured["calls"] == 2


@pytest.mark.parametrize("change", ("fee", "tick", "haircut", "native", "rule", "usd"))
def test_prior_component_policy_changes_are_cache_misses(monkeypatch, change) -> None:
    captured = qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 10_000, 10_000),), 10_000, 0, 10_000)
    first = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    new_binding = binding()
    new_book = executable_book()
    usd = 100
    if change == "fee":
        new_binding, new_book = replace(new_binding, fee_ppm=50_000), replace(new_book, fee_ppm=50_000)
    elif change == "tick":
        new_binding, new_book = replace(new_binding, tick_units=2), replace(new_book, tick_units=2)
    elif change == "haircut":
        new_binding, new_book = replace(new_binding, haircut_ppm=50_000), replace(new_book, haircut_ppm=50_000)
    elif change == "native":
        new_binding = replace(new_binding, native_id="native-b")
        new_book = replace(new_book, native_id="native-b", book=ThresholdOrderBook("native-b", new_book.book.asks, (), AS_OF))
    elif change == "rule":
        new_binding, new_book = replace(new_binding, fee_rule_id="fee-v2"), replace(new_book, fee_rule_id="fee-v2")
    else:
        usd = 200
    problem = component().problem
    changed_component = VerifiedComponent(problem, component_fingerprint(problem, (new_binding,), usd), usd, (new_binding,))

    refreshed = resolve_component(changed_component, (new_book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF, prior_market_solution=first.market_solution)

    assert refreshed.failure_reason != "PRIOR_MARKET_SOLUTION_INVALID"
    assert captured["calls"] == 2


def qualified_solver(monkeypatch):
    captured = {"calls": 0}

    def fake_solve(payload, **_kwargs):
        captured["calls"] += 1
        captured["input"] = proof_input_from_payload(payload)
        quantities = tuple(ActionQuantity(action.action_id, action.min_quantity_lots) for action in captured["input"].request.problem.actions)
        evaluation = evaluate_fixed_portfolio(
            captured["input"].request.problem,
            quantities,
            captured["input"].request.budget,
        )
        evidence = CandidateEvidence(
            CANDIDATE_EVIDENCE_SCHEMA_V1,
            captured["input"], "test", "1",
            executable_cost.model_fingerprint(captured["input"].request.problem),
            executable_cost.fingerprint({"quantities": quantities}),
            SolverEvidence(
                "FEASIBLE", PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
                ObjectiveBounds(evaluation.guaranteed_profit_units, evaluation.guaranteed_profit_units + 1, 1, False),
                evaluation.worst_scenario, evaluation.payout_lower_bound_units,
                evaluation.cost_upper_bound_units, evaluation.guaranteed_profit_units,
                evaluation.conservative_capital_release_at, True, False, 1, 1,
                (evaluation.worst_state_cut,), None,
            ),
        )
        return executable_cost.canonical_payload(evidence)

    monkeypatch.setattr(executable_cost, "solve", fake_solve)
    return captured


def executable_book() -> ImmutableBook:
    return ImmutableBook(
        "action-a", "action-a",
        ThresholdOrderBook("action-a", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF),
        fee_ppm=100_000, tick_units=1, haircut_ppm=100_000,
        price_units_per_quote_unit=100, venue_id="venue-a", fee_rule_id="fee-v1",
    )


def test_visible_multilevel_asks_floor_lots_and_ceil_protected_cost(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    original = component().problem
    action = replace(
        original.actions[0], lot_step_units=10, quantity_scale=100,
        min_quantity_lots=2, max_quantity_lots=3,
        cost_slices=(ExecutableCostSlice(1, 3, 1),),
    )
    component_with_minimum = VerifiedComponent(
        replace(original, actions=(action,)), component_fingerprint(replace(original, actions=(action,)), (binding(),), 100), 100, (binding(),),
    )
    book = ImmutableBook(
        "action-a", "action-a",
        ThresholdOrderBook("action-a", (
            BookLevel(Decimal("0.401"), Decimal("0.19")),
            BookLevel(Decimal("0.501"), Decimal("0.11")),
            BookLevel(Decimal("0.50"), Decimal("0.01")),
        ), (), AS_OF),
        100_000, 1, 100_000, 100, "venue-a", "fee-v1",
    )
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)

    result = resolve_component(component_with_minimum, (book,), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(4, 2, 2), now=AS_OF)

    assert result.status is ResolutionStatus.EXECUTION_SOLUTION
    assert captured["input"].request.problem.actions[0].cost_slices == (
        ExecutableCostSlice(1, 1, 7), ExecutableCostSlice(2, 2, 8),
    )
    assert result.execution_solution.execution_legs[0].protected_price_units == 52
    assert result.execution_solution.execution_legs[0].price_units_per_quote_unit == 100


def test_shallow_action_is_excluded_while_connected_visible_action_reaches_verifier(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    two = two_leg_component()
    action_b = two.problem.actions[1]
    shallow = replace(executable_book(), book=ThresholdOrderBook("action-a", (BookLevel(Decimal("0.40"), Decimal("0.5")),), (), AS_OF))
    visible = ImmutableBook("action-b", "action-b", ThresholdOrderBook("action-b", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF), 100_000, 1, 100_000, 100, "venue-b", "fee-v1")
    account = AccountSnapshot(AS_OF, (
        AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),
        AccountBalance("venue-b", "account-b", "usd-cents", 1_000, 1_000),
    ), 1_000, 0, 1_000)

    result = resolve_component(two, (shallow, visible), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)

    assert result.status is ResolutionStatus.EXECUTION_SOLUTION
    assert captured["input"].request.problem.actions == (replace(action_b, cost_slices=(ExecutableCostSlice(1, 1, 51),)),)


def test_qualified_fixed_market_plan_becomes_non_order_ready_execution_solution(
    monkeypatch,
) -> None:
    book = executable_book()
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000,
        0,
        1_000,
    )
    captured = qualified_solver(monkeypatch)
    result = resolve_component(
        component(), (book,), account,
        BenchmarkLimits(100, 200, 1_000_000, 4),
        OracleBudget(2, 2, 2),
        now=AS_OF,
    )

    assert captured["input"].request.problem.actions[0].cost_slices[0].incremental_cost_upper_bound_units == 51
    assert result.status is ResolutionStatus.EXECUTION_SOLUTION
    assert result.market_solution.global_search_closed is False
    assert result.execution_solution.order_ready is False
    assert result.execution_solution.reason == "PARTIAL_FILL_PROOF_REQUIRED"
    assert result.execution_solution.execution_legs[0].side == "BUY_YES"
    assert result.execution_solution.execution_legs[0].max_cost_units == 51
    assert result.execution_solution.execution_legs[0].source_fingerprint.startswith("sha256:")


def test_public_resolver_runs_real_solve_and_verify_with_bruteforce_backend() -> None:
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)

    result = resolve_component(
        component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(4, 4, 4),
        now=AS_OF, solver_backend=BruteForceBackend(),
    )

    assert result.status is ResolutionStatus.EXECUTION_SOLUTION
    assert result.market_solution.verification_result.status.value == "QUALIFIED_VERIFIED"


def test_insufficient_fixed_funding_retains_market_solution_without_second_solve(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 50, 50),),
        1_000, 0, 1_000,
    )

    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )

    assert result.status is ResolutionStatus.INSUFFICIENT_FUNDS
    assert result.market_solution is not None
    assert result.execution_solution is None
    assert result.market_solution.quantities == (ActionQuantity("action-a", 1),)
    assert captured["calls"] == 1


def test_balances_are_not_netted_across_accounts_or_assets(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (
            AccountBalance("venue-a", "account-a", "usd-cents", 50, 50),
            AccountBalance("venue-a", "account-other", "usd-cents", 10_000, 10_000),
            AccountBalance("venue-a", "account-a", "other-asset", 10_000, 10_000),
        ),
        20_000, 0, 20_000,
    )

    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)

    assert result.status is ResolutionStatus.INSUFFICIENT_FUNDS


@pytest.mark.parametrize(
    ("account", "expected"),
    (
        (AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000), AccountBalance("venue-b", "account-b", "usd-cents", 1_000, 1_000)), 100, 0, 1_000), ResolutionStatus.PER_TRADE_CAP_EXCEEDED),
        (AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000), AccountBalance("venue-b", "account-b", "usd-cents", 1_000, 1_000)), 1_000, 900, 1_000), ResolutionStatus.UNSETTLED_CAP_EXCEEDED),
    ),
)
def test_public_two_leg_plan_applies_aggregate_caps_without_netting(monkeypatch, account, expected) -> None:
    qualified_solver(monkeypatch)
    result = resolve_component(two_leg_component(), two_leg_books(), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(4, 4, 2), now=AS_OF)

    assert result.status is expected
    assert result.market_solution is not None
    assert result.execution_solution is None


def test_stale_account_retains_market_solution_as_unknown(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF - timedelta(seconds=61),
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )

    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )

    assert result.status is ResolutionStatus.ACCOUNT_STATE_UNKNOWN
    assert result.market_solution is not None
    assert result.execution_solution is None


def test_malformed_account_balance_fails_closed_as_account_unknown(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (object(),), 1_000, 0, 1_000)

    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )

    assert result.status is ResolutionStatus.ACCOUNT_STATE_UNKNOWN
    assert result.market_solution is not None


@pytest.mark.parametrize("balances", (None, [], 1, (object(),)))
def test_account_snapshot_requires_tuple_of_well_formed_balances(monkeypatch, balances) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, balances, 1_000, 0, 1_000)

    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)

    assert result.status is ResolutionStatus.ACCOUNT_STATE_UNKNOWN


def test_prior_market_solution_rechecks_funding_without_resolving_again(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    funded = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )
    first = resolve_component(
        component(), (executable_book(),), funded,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )
    unfunded = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 50, 50),),
        1_000, 0, 1_000,
    )

    refreshed = resolve_component(
        component(), (executable_book(),), unfunded,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2),
        now=AS_OF, prior_market_solution=first.market_solution,
    )

    assert refreshed.status is ResolutionStatus.INSUFFICIENT_FUNDS
    assert refreshed.market_solution == first.market_solution
    assert captured["calls"] == 1


def test_market_solution_decoder_recomputes_its_fingerprint(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )
    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )
    payload = executable_cost.canonical_payload(result.market_solution)

    assert executable_cost.canonical_payload(decode_market(payload)) == payload
    payload["fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        decode_market(payload)


def test_prior_rejects_forged_outer_hash_without_matching_source_proof(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    first = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(first.market_solution)
    payload["bounded_cost_units"] += 1
    payload["fingerprint"] = executable_cost.fingerprint({
        key: payload[key] for key in (
            "bounded_cost_units", "bounded_payout_units", "guaranteed_profit_units", "capital_release_at",
            "global_search_closed", "relation_fingerprint", "economic_quote_fingerprint", "verification_fingerprint",
            "candidate_evidence", "verification_result", "quotes", "execution_legs", "quantities",
        )
    })

    with pytest.raises(ValueError, match="fingerprint"):
        decode_market(payload)


def test_market_solution_decoder_rejects_rehashed_execution_source_tamper(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(result.market_solution)
    payload["execution_legs"][0]["source_fingerprint"] = "sha256:" + "0" * 64
    payload["fingerprint"] = executable_cost.fingerprint({
        key: payload[key] for key in (
            "bounded_cost_units", "bounded_payout_units", "guaranteed_profit_units", "capital_release_at",
            "global_search_closed", "relation_fingerprint", "economic_quote_fingerprint", "verification_fingerprint",
            "candidate_evidence", "verification_result", "quotes", "execution_legs", "quantities",
        )
    })

    with pytest.raises(ValueError, match="fingerprint"):
        decode_market(payload)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    (
        ("leg", "native_id", "other-native"),
        ("leg", "protected_price_units", 42),
        ("leg", "max_fee_units", 6),
        ("quote", "native_id", "other-native"),
    ),
)
def test_market_source_decoder_rejects_rehashed_quote_or_leg_tamper(monkeypatch, target, field, value) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(result.market_solution)
    payload["execution_legs" if target == "leg" else "quotes"][0][field] = value
    payload["fingerprint"] = executable_cost.fingerprint({
        key: payload[key] for key in (
            "bounded_cost_units", "bounded_payout_units", "guaranteed_profit_units", "capital_release_at",
            "global_search_closed", "relation_fingerprint", "economic_quote_fingerprint", "verification_fingerprint",
            "candidate_evidence", "verification_result", "quotes", "execution_legs", "quantities",
        )
    })

    with pytest.raises(ValueError):
        decode_market(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (("bounded_cost_units", 52), ("bounded_payout_units", 201),
     ("guaranteed_profit_units", 148), ("global_search_closed", True)),
)
def test_market_solution_decoder_rejects_all_bound_semantic_mutations(monkeypatch, field, value) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(result.market_solution)
    payload[field] = value

    with pytest.raises(ValueError, match="fingerprint"):
        decode_market(payload)


def test_execution_solution_decoder_recomputes_its_fingerprint(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )
    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )
    payload = executable_cost.canonical_payload(result.execution_solution)

    assert executable_cost.canonical_payload(decode_execution(payload, result.market_solution)) == payload
    payload["order_ready"] = True
    with pytest.raises(ValueError, match="partial-fill"):
        decode_execution(payload, result.market_solution)
    payload = executable_cost.canonical_payload(result.execution_solution)
    payload["capital_use_units"] += 1
    with pytest.raises(ValueError, match="fingerprint"):
        decode_execution(payload, result.market_solution)


def test_execution_decoder_requires_matching_market_and_account(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(result.execution_solution)
    changed_account = replace(account, max_per_trade_cost_units=50)

    with pytest.raises(ValueError, match="source mismatch"):
        executable_cost.execution_solution_from_payload(payload, market_solution=result.market_solution, account_snapshot=changed_account, now=AS_OF)


def test_codecs_reject_uppercase_hash_and_out_of_range_int(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),), 1_000, 0, 1_000)
    result = resolve_component(component(), (executable_book(),), account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF)
    payload = executable_cost.canonical_payload(result.market_solution)
    payload["fingerprint"] = payload["fingerprint"].upper()
    with pytest.raises(ValueError):
        decode_market(payload)
    payload = executable_cost.canonical_payload(result.market_solution)
    payload["bounded_cost_units"] = 2**63
    with pytest.raises(ValueError):
        decode_market(payload)
