"""One-shot construction of executable N-leg market and funding evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum

from open_trader.predict_source import PredictBook
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg import (
    ActionQuantity,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ExecutableCostSlice,
    OracleBudget,
    OracleRequest,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    SearchMode,
    canonical_payload,
    fingerprint,
    problem_from_payload,
)
from open_trader.prediction_n_leg_oracle import build_relation_components, cost_upper_bound
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    VerificationResult,
    VerificationStatus,
    candidate_evidence_from_payload,
    model_fingerprint,
    quote_fingerprint,
    solve,
    verification_result_from_payload,
    verify,
)
from open_trader.prediction_n_leg import PortfolioCandidate


BOOK_FRESHNESS_SECONDS = 10
ACCOUNT_FRESHNESS_SECONDS = 60
_PPM = 1_000_000
_SECONDS_PER_DAY = 24 * 60 * 60
_SEMANTIC_AS_OF = datetime(1970, 1, 1, tzinfo=UTC)
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class ResolutionStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
    EXECUTION_SOLUTION = "EXECUTION_SOLUTION"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PER_TRADE_CAP_EXCEEDED = "PER_TRADE_CAP_EXCEEDED"
    UNSETTLED_CAP_EXCEEDED = "UNSETTLED_CAP_EXCEEDED"
    ACCOUNT_STATE_UNKNOWN = "ACCOUNT_STATE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerifiedComponent:
    """A fully verified single relation component whose quotes are supplied separately."""

    problem: ArbitrageProblem
    relation_fingerprint: str
    usd_units_per_dollar: int
    book_bindings: tuple["BookBinding", ...]

    def __post_init__(self) -> None:
        if not isinstance(self.problem, ArbitrageProblem):
            raise ValueError("component problem must be canonical")
        if self.relation_fingerprint != component_fingerprint(self.problem, self.book_bindings):
            raise ValueError("component relation fingerprint mismatch")
        if type(self.usd_units_per_dollar) is not int or self.usd_units_per_dollar <= 0:
            raise ValueError("usd units per dollar must be positive")
        if not isinstance(self.book_bindings, tuple) or {
            binding.action_id for binding in self.book_bindings if isinstance(binding, BookBinding)
        } != {action.action_id for action in self.problem.actions} or len(self.book_bindings) != len(self.problem.actions):
            raise ValueError("component must bind every action to verified book policy")
        components = build_relation_components(self.problem)
        if len(components) != 1 or set(components[0].action_ids) != {action.action_id for action in self.problem.actions}:
            raise ValueError("component must contain exactly one connected support")


@dataclass(frozen=True, slots=True)
class BookBinding:
    """Component-owned venue identity and independently verified cost policy."""

    action_id: str
    venue_id: str
    native_id: str
    book_kind: str
    fee_rule_id: str
    fee_ppm: int
    tick_units: int
    haircut_ppm: int
    price_units_per_quote_unit: int

    def __post_init__(self) -> None:
        if (
            not all(isinstance(value, str) and value for value in (self.action_id, self.venue_id, self.native_id, self.fee_rule_id))
            or self.book_kind not in {"polymarket", "predict.fun"}
            or any(type(value) is not int or value < 0 for value in (self.fee_ppm, self.haircut_ppm))
            or type(self.tick_units) is not int or self.tick_units <= 0
            or type(self.price_units_per_quote_unit) is not int or self.price_units_per_quote_unit <= 0
        ):
            raise ValueError("invalid verified book binding")


def component_fingerprint(problem: ArbitrageProblem, bindings: tuple[BookBinding, ...]) -> str:
    """Stable component identity includes relation and trusted execution policy, never receipt time."""
    return fingerprint({
        "problem": canonical_payload(replace(problem, as_of=_SEMANTIC_AS_OF)),
        "book_bindings": bindings,
    })


@dataclass(frozen=True, slots=True)
class ImmutableBook:
    """A verified, immutable existing Adapter/Monitor book plus venue cost facts."""

    action_id: str
    native_id: str
    book: ThresholdOrderBook | PredictBook
    fee_ppm: int
    tick_units: int
    haircut_ppm: int
    price_units_per_quote_unit: int
    venue_id: str
    fee_rule_id: str


@dataclass(frozen=True, slots=True)
class ExecutionLegEvidence:
    action_id: str
    venue_id: str
    account_id: str
    chain_id: str
    native_id: str
    side: str
    quantity_lots: int
    protected_price_units: int
    price_units_per_quote_unit: int
    max_fee_units: int
    max_cost_units: int
    settlement_asset_id: str
    valuation_unit_id: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class QuoteLevelEvidence:
    first_lot: int
    last_lot: int
    protected_price_units: int
    max_fee_per_lot_units: int
    max_cost_per_lot_units: int


@dataclass(frozen=True, slots=True)
class CanonicalQuoteEvidence:
    """The single normalized, immutable source for costs and #74 limit facts."""

    action_id: str
    venue_id: str
    account_id: str
    chain_id: str
    native_id: str
    book_kind: str
    binding: BookBinding
    source_fingerprint: str
    economic_fingerprint: str
    levels: tuple[QuoteLevelEvidence, ...]

    @property
    def cost_slices(self) -> tuple[ExecutableCostSlice, ...]:
        return tuple(ExecutableCostSlice(level.first_lot, level.last_lot, level.max_cost_per_lot_units) for level in self.levels)


@dataclass(frozen=True, slots=True)
class AccountBalance:
    venue_id: str
    account_id: str
    asset_id: str
    available_units: int
    allowance_units: int


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    captured_at: datetime
    balances: tuple[AccountBalance, ...]
    max_per_trade_cost_units: int
    unsettled_capital_units: int
    max_total_unsettled_capital_units: int


@dataclass(frozen=True, slots=True)
class MarketSolution:
    problem: ArbitrageProblem
    quantities: tuple[ActionQuantity, ...]
    bounded_cost_units: int
    bounded_payout_units: int
    guaranteed_profit_units: int
    capital_release_at: datetime
    global_search_closed: bool
    relation_fingerprint: str
    economic_quote_fingerprint: str
    verification_fingerprint: str
    candidate_evidence: CandidateEvidence
    verification_result: VerificationResult
    quotes: tuple[CanonicalQuoteEvidence, ...]
    execution_legs: tuple[ExecutionLegEvidence, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ExecutionSolution:
    market_solution_fingerprint: str
    account_snapshot_fingerprint: str
    quantities: tuple[ActionQuantity, ...]
    capital_use_units: int
    execution_legs: tuple[ExecutionLegEvidence, ...]
    order_ready: bool
    reason: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    status: ResolutionStatus
    failure_reason: str | None
    market_solution: MarketSolution | None = None
    execution_solution: ExecutionSolution | None = None


def resolve_component(
    component: VerifiedComponent,
    books: tuple[ImmutableBook, ...],
    account_snapshot: AccountSnapshot | None,
    limits: BenchmarkLimits,
    budget: OracleBudget,
    *,
    now: datetime,
    prior_market_solution: MarketSolution | None = None,
) -> ResolutionResult:
    """Resolve one fixed component; funding never changes the chosen portfolio."""
    try:
        if not isinstance(component, VerifiedComponent) or not isinstance(limits, BenchmarkLimits) or not isinstance(budget, OracleBudget):
            raise ValueError("invalid input")
        problem, economic_quote, quotes = _problem_with_visible_asks(component, books, now)
    except (InvalidOperation, ValueError):
        return ResolutionResult(ResolutionStatus.UNKNOWN, "BOOK_STATE_UNKNOWN")
    if problem is None:
        return ResolutionResult(ResolutionStatus.NO_QUALIFIED_OPPORTUNITY, "INSUFFICIENT_VISIBLE_DEPTH")
    if prior_market_solution is not None:
        try:
            prior = market_solution_from_payload(canonical_payload(prior_market_solution), component=component, books=books, now=now, require_current_source=False)
        except ValueError:
            return ResolutionResult(ResolutionStatus.UNKNOWN, "PRIOR_MARKET_SOLUTION_INVALID")
        if (
            prior.relation_fingerprint == component.relation_fingerprint
            and prior.economic_quote_fingerprint == economic_quote
            and prior.quotes == quotes
            and prior.execution_legs == _execution_legs(problem, prior.quantities, quotes)
        ):
            return _fund_fixed_solution(prior_market_solution, account_snapshot, now)

    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, budget),
        limits,
        quote_fingerprint(problem),
        0,
        "issue-51",
    )
    try:
        evidence = candidate_evidence_from_payload(solve(canonical_payload(proof_input)))
        verification = verification_result_from_payload(verify(canonical_payload(evidence)), source=evidence)
    except (ValueError, TypeError):
        return ResolutionResult(ResolutionStatus.UNKNOWN, "SOLVER_OR_VERIFIER_UNKNOWN")
    if verification.status != VerificationStatus.QUALIFIED_VERIFIED or verification.solution is None:
        status = ResolutionStatus.NO_QUALIFIED_OPPORTUNITY if verification.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY else ResolutionStatus.UNKNOWN
        return ResolutionResult(status, verification.status.value)
    proof = verification.solution.payout_proof
    market = _market_solution(
        problem, verification.solution.quantities, proof, evidence, verification, component.relation_fingerprint, economic_quote,
        quotes,
    )
    return _fund_fixed_solution(market, account_snapshot, now)


def _problem_with_visible_asks(
    component: VerifiedComponent,
    books: tuple[ImmutableBook, ...],
    now: datetime,
) -> tuple[ArbitrageProblem | None, str, tuple[CanonicalQuoteEvidence, ...]]:
    if not isinstance(books, tuple) or not _utc(now):
        raise ValueError("books must be an immutable tuple")
    by_action = {item.action_id: item for item in books if isinstance(item, ImmutableBook)}
    if len(by_action) != len(books) or set(by_action) != {action.action_id for action in component.problem.actions}:
        raise ValueError("books must exactly cover component actions")
    converted: list[CandidateAction] = []
    available_ids: set[str] = set()
    quotes: list[CanonicalQuoteEvidence] = []
    for action in component.problem.actions:
        book = by_action[action.action_id]
        binding = next(binding for binding in component.book_bindings if binding.action_id == action.action_id)
        quote = _normalize_quote(action, book, binding, now)
        if quote is None:
            continue
        converted.append(replace(action, max_quantity_lots=quote.levels[-1].last_lot, cost_slices=quote.cost_slices))
        available_ids.add(action.action_id)
        quotes.append(quote)
    if not converted:
        return None, "", ()
    terminal_sets = tuple(
        replace(state_set, atoms=tuple(
            replace(atom, payouts=tuple(payout for payout in atom.payouts if payout.action_id in available_ids))
            for atom in state_set.atoms
        ))
        for state_set in component.problem.terminal_state_sets
    )
    rules = component.problem.terminal_state_sets
    problem = replace(
        component.problem,
        as_of=now,
        actions=tuple(converted),
        terminal_state_sets=terminal_sets,
        qualification_constraints=_qualification_constraints(component, rules),
    )
    canonical_quotes = tuple(quotes)
    return problem, fingerprint({"quotes": canonical_quotes, "relation": component.relation_fingerprint}), canonical_quotes


def _normalize_quote(action: CandidateAction, source: ImmutableBook, binding: BookBinding, now: datetime) -> CanonicalQuoteEvidence | None:
    if (
        not isinstance(source.book, ThresholdOrderBook | PredictBook)
        or source.action_id != action.action_id
        or binding.action_id != action.action_id
        or binding.venue_id != action.venue_id
        or source.venue_id != binding.venue_id
        or source.native_id != binding.native_id
        or source.fee_rule_id != binding.fee_rule_id
        or source.fee_ppm != binding.fee_ppm
        or source.tick_units != binding.tick_units
        or source.haircut_ppm != binding.haircut_ppm
        or source.price_units_per_quote_unit != binding.price_units_per_quote_unit
        or (binding.book_kind == "polymarket") != isinstance(source.book, ThresholdOrderBook)
        or (binding.book_kind == "predict.fun") != isinstance(source.book, PredictBook)
    ):
        raise ValueError("invalid book")
    if any(type(value) is not int or value < 0 for value in (source.fee_ppm, source.haircut_ppm)) or type(source.tick_units) is not int or source.tick_units <= 0 or type(source.price_units_per_quote_unit) is not int or source.price_units_per_quote_unit <= 0:
        raise ValueError("missing cost facts")
    timestamps, asks, actual_id = _book_asks(source.book, action)
    if source.native_id != actual_id or any(
        not _utc(timestamp)
        or (now - timestamp).total_seconds() < 0
        or (now - timestamp).total_seconds() > BOOK_FRESHNESS_SECONDS
        for timestamp in timestamps
    ):
        raise ValueError("stale or mismatched book")
    levels: list[QuoteLevelEvidence] = []
    first = 1
    for level in asks:
        if not _valid_level(level):
            raise ValueError("malformed visible ask")
        lots = int((level.size * action.quantity_scale / action.lot_step_units).to_integral_value(rounding=ROUND_FLOOR))
        if lots <= 0:
            continue
        protected = int((level.price * source.price_units_per_quote_unit).to_integral_value(rounding=ROUND_CEILING)) + source.tick_units
        venue_cost = _ceil_div(protected * action.lot_step_units, action.quantity_scale)
        fee = _ceil_div(venue_cost * source.fee_ppm, _PPM)
        haircut = _ceil_div((venue_cost + fee) * source.haircut_ppm, _PPM)
        unit_cost = venue_cost + fee + haircut
        last = min(action.max_quantity_lots, first + lots - 1)
        levels.append(QuoteLevelEvidence(first, last, protected, fee, unit_cost))
        first = last + 1
        if last == action.max_quantity_lots:
            break
    if not levels or levels[-1].last_lot < action.min_quantity_lots:
        return None
    source_fingerprint = fingerprint({"binding": binding, "book": _economic_book(source, action), "action": action})
    return CanonicalQuoteEvidence(
        action.action_id, action.venue_id, action.account_id, action.chain_id, source.native_id,
        binding.book_kind, binding, source_fingerprint,
        fingerprint(_economic_book(source, action)), tuple(levels),
    )


def _book_asks(book: ThresholdOrderBook | PredictBook, action: CandidateAction) -> tuple[tuple[datetime, ...], tuple[BookLevel, ...], str]:
    if isinstance(book, ThresholdOrderBook):
        return (book.confirmed_at,), book.asks, book.token_id
    return (book.source_timestamp, book.received_at), book.yes_asks if action.side.value == "BUY_YES" else book.no_asks, book.market_id


def _qualification_constraints(component: VerifiedComponent, rules: tuple[object, ...]) -> tuple[QualificationConstraint, ...]:
    rule_version = next((getattr(item, "rule_version", None) for item in rules), None)
    if not isinstance(rule_version, str) or not rule_version:
        raise ValueError("missing verified rule version")
    return (
        QualificationConstraint("minimum-profit-usd", rule_version, QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, component.usd_units_per_dollar, 1),
        QualificationConstraint("minimum-return-on-cost", rule_version, QualificationMetric.RETURN_ON_COST_PPM, Comparison.GREATER_THAN_OR_EQUAL, 10_000, 1),
        QualificationConstraint("minimum-annualized-return", rule_version, QualificationMetric.ANNUALIZED_RETURN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 150_000, 1),
        QualificationConstraint("maximum-release-delay", rule_version, QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS, Comparison.LESS_THAN_OR_EQUAL, 30 * _SECONDS_PER_DAY, 1),
    )


def _execution_legs(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    quotes: tuple[CanonicalQuoteEvidence, ...],
) -> tuple[ExecutionLegEvidence, ...]:
    """Freeze the exact #74 handoff facts while the source books are verified."""
    actions = {action.action_id: action for action in problem.actions}
    by_action = {quote.action_id: quote for quote in quotes}
    result: list[ExecutionLegEvidence] = []
    for quantity in quantities:
        action = actions[quantity.action_id]
        quote = by_action[quantity.action_id]
        remaining = quantity.quantity_lots
        protected_price = 0
        max_fee = 0
        for level in quote.levels:
            take = min(remaining, level.last_lot - level.first_lot + 1)
            if take <= 0:
                continue
            protected_price = max(protected_price, level.protected_price_units)
            max_fee += take * level.max_fee_per_lot_units
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise ValueError("execution evidence exceeds visible asks")
        result.append(ExecutionLegEvidence(
            action.action_id, action.venue_id, action.account_id, action.chain_id, quote.native_id,
            action.side.value, quantity.quantity_lots, protected_price, quote.binding.price_units_per_quote_unit,
            max_fee, cost_upper_bound(problem, (quantity,)), action.settlement_asset_id,
            action.valuation_unit_id, quote.source_fingerprint,
        ))
    return tuple(result)


def _market_solution(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    proof: object,
    evidence: CandidateEvidence,
    verification: VerificationResult,
    relation_fingerprint: str,
    economic_quote: str,
    quotes: tuple[CanonicalQuoteEvidence, ...],
) -> MarketSolution:
    verification_fingerprint = fingerprint(canonical_payload(proof))
    values = {
        "bounded_cost_units": proof.cost_upper_bound_units,
        "bounded_payout_units": proof.payout_lower_bound_units,
        "guaranteed_profit_units": proof.guaranteed_profit_units,
        "capital_release_at": proof.conservative_capital_release_at,
        "global_search_closed": evidence.solver_evidence.global_search_closed,
        "relation_fingerprint": relation_fingerprint,
        "economic_quote_fingerprint": economic_quote,
        "verification_fingerprint": verification_fingerprint,
        "candidate_evidence": evidence,
        "verification_result": verification,
        "quotes": quotes,
        "execution_legs": _execution_legs(problem, quantities, quotes),
        "quantities": quantities,
    }
    return MarketSolution(
        problem, quantities, proof.cost_upper_bound_units, proof.payout_lower_bound_units,
        proof.guaranteed_profit_units, proof.conservative_capital_release_at,
        evidence.solver_evidence.global_search_closed, relation_fingerprint,
        economic_quote, verification_fingerprint, evidence, verification, quotes,
        _execution_legs(problem, quantities, quotes), fingerprint(values),
    )


def market_solution_from_payload(
    payload: object, *, component: VerifiedComponent, books: tuple[ImmutableBook, ...], now: datetime, require_current_source: bool = True,
) -> MarketSolution:
    """Decode a MarketSolution only when every retained binding recomputes."""
    value = _exact_object(payload, {
        "problem", "quantities", "bounded_cost_units", "bounded_payout_units",
        "guaranteed_profit_units", "capital_release_at", "global_search_closed",
        "relation_fingerprint", "economic_quote_fingerprint", "verification_fingerprint", "fingerprint",
        "candidate_evidence", "verification_result", "quotes", "execution_legs",
    })
    try:
        problem = problem_from_payload(value["problem"])
        quantities = tuple(_quantity_from_payload(item) for item in _array(value["quantities"]))
        evidence = candidate_evidence_from_payload(value["candidate_evidence"])
        verification = verification_result_from_payload(value["verification_result"], source=evidence)
        quotes = tuple(_quote_from_payload(item) for item in _array(value["quotes"]))
        solution = MarketSolution(
            problem, quantities, _nonnegative_int(value["bounded_cost_units"]),
            _nonnegative_int(value["bounded_payout_units"]), _int(value["guaranteed_profit_units"]),
            _datetime(value["capital_release_at"]), _bool(value["global_search_closed"]),
            _fingerprint(value["relation_fingerprint"]), _fingerprint(value["economic_quote_fingerprint"]),
            _fingerprint(value["verification_fingerprint"]),
            evidence, verification, quotes,
            tuple(_execution_leg_from_payload(item) for item in _array(value["execution_legs"])),
            _fingerprint(value["fingerprint"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid market solution: {exc}") from exc
    expected = fingerprint({
        "bounded_cost_units": solution.bounded_cost_units,
        "bounded_payout_units": solution.bounded_payout_units,
        "guaranteed_profit_units": solution.guaranteed_profit_units,
        "capital_release_at": solution.capital_release_at,
        "global_search_closed": solution.global_search_closed,
        "relation_fingerprint": solution.relation_fingerprint,
        "economic_quote_fingerprint": solution.economic_quote_fingerprint,
        "verification_fingerprint": solution.verification_fingerprint,
        "candidate_evidence": solution.candidate_evidence,
        "verification_result": solution.verification_result,
        "quotes": solution.quotes,
        "execution_legs": solution.execution_legs,
        "quantities": solution.quantities,
    })
    if (
        solution.fingerprint != expected
        or solution.relation_fingerprint != component.relation_fingerprint
        or solution.verification_result.status != VerificationStatus.QUALIFIED_VERIFIED
        or solution.verification_result.solution is None
        or solution.candidate_evidence.proof_input.request.problem != solution.problem
        or solution.candidate_evidence.solver_evidence.global_search_closed != solution.global_search_closed
        or solution.verification_result.solution.quantities != solution.quantities
        or solution.verification_result.solution.payout_proof.cost_upper_bound_units != solution.bounded_cost_units
        or solution.verification_result.solution.payout_proof.payout_lower_bound_units != solution.bounded_payout_units
        or solution.verification_result.solution.payout_proof.guaranteed_profit_units != solution.guaranteed_profit_units
        or solution.verification_result.solution.payout_proof.conservative_capital_release_at != solution.capital_release_at
        or solution.verification_fingerprint != fingerprint(canonical_payload(solution.verification_result.solution.payout_proof))
        or tuple(sorted(solution.quantities, key=lambda item: item.action_id)) != solution.quantities
        or len({item.action_id for item in solution.quantities}) != len(solution.quantities)
        or not _valid_execution_legs(solution.problem, solution.quantities, solution.quotes, solution.execution_legs)
    ):
        raise ValueError("market solution fingerprint mismatch")
    if not require_current_source:
        return solution
    try:
        current_problem, current_economic, current_quotes = _problem_with_visible_asks(component, books, now)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("market solution source invalid") from exc
    if current_problem is None or current_economic != solution.economic_quote_fingerprint or current_quotes != solution.quotes or canonical_payload(_semantic_problem(current_problem)) != canonical_payload(_semantic_problem(solution.problem)) or _execution_legs(current_problem, solution.quantities, current_quotes) != solution.execution_legs:
        raise ValueError("market solution source mismatch")
    return solution


def execution_solution_from_payload(
    payload: object, *, market_solution: MarketSolution, account_snapshot: AccountSnapshot, now: datetime,
) -> ExecutionSolution:
    """Decode the #74 handoff only when it remains explicitly non-order-ready."""
    value = _exact_object(payload, {
        "market_solution_fingerprint", "account_snapshot_fingerprint", "quantities",
        "capital_use_units", "execution_legs", "order_ready", "reason", "fingerprint",
    })
    try:
        solution = ExecutionSolution(
            _fingerprint(value["market_solution_fingerprint"]),
            _fingerprint(value["account_snapshot_fingerprint"]),
            tuple(_quantity_from_payload(item) for item in _array(value["quantities"])),
            _nonnegative_int(value["capital_use_units"]),
            tuple(_execution_leg_from_payload(item) for item in _array(value["execution_legs"])), _bool(value["order_ready"]),
            _text(value["reason"]), _fingerprint(value["fingerprint"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid execution solution: {exc}") from exc
    expected = fingerprint({
        "market": solution.market_solution_fingerprint,
        "account": solution.account_snapshot_fingerprint,
        "quantities": solution.quantities,
        "capital_use_units": solution.capital_use_units,
        "execution_legs": solution.execution_legs,
    })
    if (
        solution.order_ready
        or solution.reason != "PARTIAL_FILL_PROOF_REQUIRED"
        or solution.fingerprint != expected
        or tuple(sorted(solution.quantities, key=lambda item: item.action_id)) != solution.quantities
        or len({item.action_id for item in solution.quantities}) != len(solution.quantities)
        or tuple(item.action_id for item in solution.execution_legs) != tuple(item.action_id for item in solution.quantities)
    ):
        raise ValueError("execution solution partial-fill or fingerprint mismatch")
    funded = _fund_fixed_solution(market_solution, account_snapshot, now)
    if (
        funded.status != ResolutionStatus.EXECUTION_SOLUTION
        or funded.execution_solution is None
        or funded.execution_solution != solution
        or solution.market_solution_fingerprint != market_solution.fingerprint
        or solution.account_snapshot_fingerprint != fingerprint(account_snapshot)
        or solution.capital_use_units != sum(leg.max_cost_units for leg in solution.execution_legs)
    ):
        raise ValueError("execution solution source mismatch")
    return solution


def _fund_fixed_solution(market: MarketSolution, account: AccountSnapshot | None, now: datetime) -> ResolutionResult:
    if not _valid_account_snapshot(account, now):
        return ResolutionResult(ResolutionStatus.ACCOUNT_STATE_UNKNOWN, "ACCOUNT_STATE_UNKNOWN", market)
    assert account is not None
    debits: dict[tuple[str, str, str], int] = {}
    actions = {action.action_id: action for action in market.problem.actions}
    for quantity in market.quantities:
        action = actions.get(quantity.action_id)
        if action is None:
            return ResolutionResult(ResolutionStatus.UNKNOWN, "MARKET_SOLUTION_INVALID", market)
        debit = cost_upper_bound(market.problem, (quantity,))
        key = (action.venue_id, action.account_id, action.settlement_asset_id)
        debits[key] = debits.get(key, 0) + debit
    balances = {(item.venue_id, item.account_id, item.asset_id): item for item in account.balances}
    if any(key not in balances for key in debits):
        return ResolutionResult(ResolutionStatus.ACCOUNT_STATE_UNKNOWN, "ACCOUNT_STATE_UNKNOWN", market)
    if any(debit > balances[key].available_units or debit > balances[key].allowance_units for key, debit in debits.items()):
        return ResolutionResult(ResolutionStatus.INSUFFICIENT_FUNDS, "INSUFFICIENT_FUNDS", market)
    capital_use = sum(debits.values())
    if capital_use > account.max_per_trade_cost_units:
        return ResolutionResult(ResolutionStatus.PER_TRADE_CAP_EXCEEDED, "PER_TRADE_CAP_EXCEEDED", market)
    if account.unsettled_capital_units + capital_use > account.max_total_unsettled_capital_units:
        return ResolutionResult(ResolutionStatus.UNSETTLED_CAP_EXCEEDED, "UNSETTLED_CAP_EXCEEDED", market)
    account_fingerprint = fingerprint(account)
    execution = ExecutionSolution(
        market.fingerprint, account_fingerprint, market.quantities, capital_use, market.execution_legs, False,
        "PARTIAL_FILL_PROOF_REQUIRED", fingerprint({
            "market": market.fingerprint, "account": account_fingerprint,
            "quantities": market.quantities, "capital_use_units": capital_use,
            "execution_legs": market.execution_legs,
        }),
    )
    return ResolutionResult(ResolutionStatus.EXECUTION_SOLUTION, None, market, execution)


def _valid_account_snapshot(snapshot: AccountSnapshot | None, now: datetime) -> bool:
    if not isinstance(snapshot, AccountSnapshot) or not _utc(now) or not _utc(snapshot.captured_at):
        return False
    if (now - snapshot.captured_at).total_seconds() < 0 or (now - snapshot.captured_at).total_seconds() > ACCOUNT_FRESHNESS_SECONDS:
        return False
    if any(type(value) is not int or not 0 <= value <= _INT64_MAX for value in (snapshot.max_per_trade_cost_units, snapshot.unsettled_capital_units, snapshot.max_total_unsettled_capital_units)):
        return False
    if not isinstance(snapshot.balances, tuple):
        return False
    if not all(
        isinstance(balance, AccountBalance)
        and all(isinstance(value, str) and value for value in (balance.venue_id, balance.account_id, balance.asset_id))
        and type(balance.available_units) is int and 0 <= balance.available_units <= _INT64_MAX
        and type(balance.allowance_units) is int and 0 <= balance.allowance_units <= _INT64_MAX
        for balance in snapshot.balances
    ):
        return False
    keys = {(balance.venue_id, balance.account_id, balance.asset_id) for balance in snapshot.balances}
    return len(keys) == len(snapshot.balances)


def _economic_book(book: ImmutableBook, action: CandidateAction) -> dict[str, object]:
    _, asks, actual_id = _book_asks(book.book, action)
    return {
        "action_id": book.action_id, "native_id": actual_id, "fee_ppm": book.fee_ppm,
        "venue_id": book.venue_id, "fee_rule_id": book.fee_rule_id,
        "tick_units": book.tick_units, "haircut_ppm": book.haircut_ppm,
        "price_units_per_quote_unit": book.price_units_per_quote_unit,
        "asks": tuple((str(level.price), str(level.size)) for level in asks),
    }


def _valid_level(level: object) -> bool:
    return isinstance(level, BookLevel) and isinstance(level.price, Decimal) and isinstance(level.size, Decimal) and level.price > 0 and level.size > 0 and level.price.is_finite() and level.size.is_finite()


def _utc(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _semantic_model_fingerprint(problem: ArbitrageProblem) -> str:
    return model_fingerprint(replace(problem, as_of=_SEMANTIC_AS_OF))


def _exact_object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected fields")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("must be an array")
    return value


def _int(value: object) -> int:
    if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError("must be a signed int64 integer")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("must be non-empty text")
    return value


def _nonnegative_int(value: object) -> int:
    value = _int(value)
    if value < 0:
        raise ValueError("must be non-negative")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("must be a boolean")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("must be an RFC3339 UTC datetime")
    decoded = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if not _utc(decoded):
        raise ValueError("must be UTC")
    return decoded


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError("must be a SHA-256 fingerprint")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError("must be a lowercase SHA-256 fingerprint")
    return value


def _quantity_from_payload(value: object) -> ActionQuantity:
    item = _exact_object(value, {"action_id", "quantity_lots"})
    if not isinstance(item["action_id"], str) or not item["action_id"]:
        raise ValueError("quantity action ID is invalid")
    quantity = _nonnegative_int(item["quantity_lots"])
    return ActionQuantity(item["action_id"], quantity)


def _execution_leg_from_payload(value: object) -> ExecutionLegEvidence:
    item = _exact_object(value, {
        "action_id", "venue_id", "account_id", "chain_id", "native_id", "side",
        "quantity_lots", "protected_price_units", "price_units_per_quote_unit", "max_fee_units", "max_cost_units",
        "settlement_asset_id", "valuation_unit_id", "source_fingerprint",
    })
    side = _text(item["side"])
    if side not in {"BUY_YES", "BUY_NO"}:
        raise ValueError("execution leg must buy a canonical side")
    return ExecutionLegEvidence(
        *(_text(item[name]) for name in ("action_id", "venue_id", "account_id", "chain_id", "native_id")),
        side,
        *(_nonnegative_int(item[name]) for name in ("quantity_lots", "protected_price_units", "price_units_per_quote_unit", "max_fee_units", "max_cost_units")),
        *(_text(item[name]) for name in ("settlement_asset_id", "valuation_unit_id")),
        _fingerprint(item["source_fingerprint"]),
    )


def _binding_from_payload(value: object) -> BookBinding:
    item = _exact_object(value, {"action_id", "venue_id", "native_id", "book_kind", "fee_rule_id", "fee_ppm", "tick_units", "haircut_ppm", "price_units_per_quote_unit"})
    return BookBinding(
        *(_text(item[name]) for name in ("action_id", "venue_id", "native_id", "book_kind", "fee_rule_id")),
        *(_nonnegative_int(item[name]) for name in ("fee_ppm", "tick_units", "haircut_ppm", "price_units_per_quote_unit")),
    )


def _quote_from_payload(value: object) -> CanonicalQuoteEvidence:
    item = _exact_object(value, {"action_id", "venue_id", "account_id", "chain_id", "native_id", "book_kind", "binding", "source_fingerprint", "economic_fingerprint", "levels"})
    levels = []
    for raw in _array(item["levels"]):
        level = _exact_object(raw, {"first_lot", "last_lot", "protected_price_units", "max_fee_per_lot_units", "max_cost_per_lot_units"})
        decoded = QuoteLevelEvidence(*(_nonnegative_int(level[name]) for name in ("first_lot", "last_lot", "protected_price_units", "max_fee_per_lot_units", "max_cost_per_lot_units")))
        if decoded.first_lot <= 0 or decoded.last_lot < decoded.first_lot:
            raise ValueError("invalid quote level")
        levels.append(decoded)
    quote = CanonicalQuoteEvidence(
        *(_text(item[name]) for name in ("action_id", "venue_id", "account_id", "chain_id", "native_id", "book_kind")),
        _binding_from_payload(item["binding"]), _fingerprint(item["source_fingerprint"]), _fingerprint(item["economic_fingerprint"]), tuple(levels),
    )
    if quote.binding.action_id != quote.action_id or quote.binding.venue_id != quote.venue_id or quote.binding.native_id != quote.native_id or quote.binding.book_kind != quote.book_kind:
        raise ValueError("quote binding mismatch")
    return quote


def _semantic_problem(problem: ArbitrageProblem) -> ArbitrageProblem:
    return replace(problem, as_of=_SEMANTIC_AS_OF)


def _valid_execution_legs(
    problem: ArbitrageProblem, quantities: tuple[ActionQuantity, ...], quotes: tuple[CanonicalQuoteEvidence, ...], legs: tuple[ExecutionLegEvidence, ...]
) -> bool:
    actions = {action.action_id: action for action in problem.actions}
    if tuple(leg.action_id for leg in legs) != tuple(quantity.action_id for quantity in quantities):
        return False
    if tuple(quote.action_id for quote in quotes) != tuple(action.action_id for action in problem.actions):
        return False
    for leg, quantity in zip(legs, quantities, strict=True):
        action = actions.get(quantity.action_id)
        if action is None or (
            leg.venue_id, leg.account_id, leg.chain_id, leg.side, leg.quantity_lots,
            leg.settlement_asset_id, leg.valuation_unit_id,
        ) != (
            action.venue_id, action.account_id, action.chain_id, action.side.value,
            quantity.quantity_lots, action.settlement_asset_id, action.valuation_unit_id,
        ) or leg.max_cost_units != cost_upper_bound(problem, (quantity,)) or leg != _execution_legs(problem, (quantity,), quotes)[0]:
            return False
    return True


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor
