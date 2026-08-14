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


class ResolutionStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
    MARKET_SOLUTION = "MARKET_SOLUTION"
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

    def __post_init__(self) -> None:
        if not isinstance(self.problem, ArbitrageProblem):
            raise ValueError("component problem must be canonical")
        if self.relation_fingerprint != model_fingerprint(self.problem):
            raise ValueError("component relation fingerprint mismatch")
        if type(self.usd_units_per_dollar) is not int or self.usd_units_per_dollar <= 0:
            raise ValueError("usd units per dollar must be positive")
        components = build_relation_components(self.problem)
        if len(components) != 1 or set(components[0].action_ids) != {action.action_id for action in self.problem.actions}:
            raise ValueError("component must contain exactly one connected support")


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
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ExecutionSolution:
    market_solution_fingerprint: str
    account_snapshot_fingerprint: str
    quantities: tuple[ActionQuantity, ...]
    capital_use_units: int
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
        problem, economic_quote = _problem_with_visible_asks(component, books, now)
    except (InvalidOperation, ValueError):
        return ResolutionResult(ResolutionStatus.UNKNOWN, "BOOK_STATE_UNKNOWN")
    if problem is None:
        return ResolutionResult(ResolutionStatus.NO_QUALIFIED_OPPORTUNITY, "INSUFFICIENT_VISIBLE_DEPTH")
    if prior_market_solution is not None:
        try:
            prior = market_solution_from_payload(canonical_payload(prior_market_solution))
        except ValueError:
            return ResolutionResult(ResolutionStatus.UNKNOWN, "PRIOR_MARKET_SOLUTION_INVALID")
        if (
            prior.relation_fingerprint == model_fingerprint(problem)
            and prior.economic_quote_fingerprint == economic_quote
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
    market = _market_solution(problem, verification.solution.quantities, proof, evidence, economic_quote)
    return _fund_fixed_solution(market, account_snapshot, now)


def _problem_with_visible_asks(
    component: VerifiedComponent,
    books: tuple[ImmutableBook, ...],
    now: datetime,
) -> tuple[ArbitrageProblem | None, str]:
    if not isinstance(books, tuple) or not _utc(now):
        raise ValueError("books must be an immutable tuple")
    by_action = {item.action_id: item for item in books if isinstance(item, ImmutableBook)}
    if len(by_action) != len(books) or set(by_action) != {action.action_id for action in component.problem.actions}:
        raise ValueError("books must exactly cover component actions")
    converted: list[CandidateAction] = []
    economic_books: list[dict[str, object]] = []
    for action in component.problem.actions:
        book = by_action[action.action_id]
        slices = _cost_slices(action, book, now)
        if slices is None:
            return None, ""
        converted.append(replace(action, max_quantity_lots=slices[-1].last_lot, cost_slices=slices))
        economic_books.append(_economic_book(book, action))
    rules = component.problem.terminal_state_sets
    problem = replace(
        component.problem,
        as_of=now,
        actions=tuple(converted),
        qualification_constraints=_qualification_constraints(component, rules),
    )
    return problem, fingerprint({"books": tuple(economic_books), "relation": component.relation_fingerprint})


def _cost_slices(action: CandidateAction, source: ImmutableBook, now: datetime) -> tuple[ExecutableCostSlice, ...] | None:
    if (
        not isinstance(source.book, ThresholdOrderBook | PredictBook)
        or source.action_id != action.action_id
        or source.venue_id != action.venue_id
        or not isinstance(source.fee_rule_id, str)
        or not source.fee_rule_id
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
    slices: list[ExecutableCostSlice] = []
    first = 1
    for level in asks:
        if not _valid_level(level):
            raise ValueError("malformed visible ask")
        lots = int((level.size * action.quantity_scale / action.lot_step_units).to_integral_value(rounding=ROUND_FLOOR))
        if lots <= 0:
            continue
        base = int((level.price * source.price_units_per_quote_unit * action.lot_step_units / action.quantity_scale).to_integral_value(rounding=ROUND_CEILING))
        protected = base + source.tick_units
        fee = _ceil_div(protected * source.fee_ppm, _PPM)
        haircut = _ceil_div((protected + fee) * source.haircut_ppm, _PPM)
        unit_cost = protected + fee + haircut
        last = min(action.max_quantity_lots, first + lots - 1)
        slices.append(ExecutableCostSlice(first, last, unit_cost))
        first = last + 1
        if last == action.max_quantity_lots:
            break
    if not slices or slices[-1].last_lot < action.min_quantity_lots:
        return None
    return tuple(slices)


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


def _market_solution(problem: ArbitrageProblem, quantities: tuple[ActionQuantity, ...], proof: object, evidence: CandidateEvidence, economic_quote: str) -> MarketSolution:
    verification_fingerprint = fingerprint(canonical_payload(proof))
    relation_fingerprint = model_fingerprint(problem)
    values = {
        "bounded_cost_units": proof.cost_upper_bound_units,
        "bounded_payout_units": proof.payout_lower_bound_units,
        "guaranteed_profit_units": proof.guaranteed_profit_units,
        "capital_release_at": proof.conservative_capital_release_at,
        "global_search_closed": evidence.solver_evidence.global_search_closed,
        "relation_fingerprint": relation_fingerprint,
        "economic_quote_fingerprint": economic_quote,
        "verification_fingerprint": verification_fingerprint,
        "quantities": quantities,
    }
    return MarketSolution(
        problem, quantities, proof.cost_upper_bound_units, proof.payout_lower_bound_units,
        proof.guaranteed_profit_units, proof.conservative_capital_release_at,
        evidence.solver_evidence.global_search_closed, relation_fingerprint,
        economic_quote, verification_fingerprint, fingerprint(values),
    )


def market_solution_from_payload(payload: object) -> MarketSolution:
    """Decode a MarketSolution only when every retained binding recomputes."""
    value = _exact_object(payload, {
        "problem", "quantities", "bounded_cost_units", "bounded_payout_units",
        "guaranteed_profit_units", "capital_release_at", "global_search_closed",
        "relation_fingerprint", "economic_quote_fingerprint", "verification_fingerprint", "fingerprint",
    })
    try:
        problem = problem_from_payload(value["problem"])
        quantities = tuple(_quantity_from_payload(item) for item in _array(value["quantities"]))
        solution = MarketSolution(
            problem, quantities, _nonnegative_int(value["bounded_cost_units"]),
            _nonnegative_int(value["bounded_payout_units"]), _int(value["guaranteed_profit_units"]),
            _datetime(value["capital_release_at"]), _bool(value["global_search_closed"]),
            _fingerprint(value["relation_fingerprint"]), _fingerprint(value["economic_quote_fingerprint"]),
            _fingerprint(value["verification_fingerprint"]), _fingerprint(value["fingerprint"]),
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
        "quantities": solution.quantities,
    })
    if (
        solution.relation_fingerprint != model_fingerprint(solution.problem)
        or solution.fingerprint != expected
        or tuple(sorted(solution.quantities, key=lambda item: item.action_id)) != solution.quantities
        or len({item.action_id for item in solution.quantities}) != len(solution.quantities)
    ):
        raise ValueError("market solution fingerprint mismatch")
    return solution


def execution_solution_from_payload(payload: object) -> ExecutionSolution:
    """Decode the #74 handoff only when it remains explicitly non-order-ready."""
    value = _exact_object(payload, {
        "market_solution_fingerprint", "account_snapshot_fingerprint", "quantities",
        "capital_use_units", "order_ready", "reason", "fingerprint",
    })
    try:
        solution = ExecutionSolution(
            _fingerprint(value["market_solution_fingerprint"]),
            _fingerprint(value["account_snapshot_fingerprint"]),
            tuple(_quantity_from_payload(item) for item in _array(value["quantities"])),
            _nonnegative_int(value["capital_use_units"]), _bool(value["order_ready"]),
            _text(value["reason"]), _fingerprint(value["fingerprint"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid execution solution: {exc}") from exc
    expected = fingerprint({
        "market": solution.market_solution_fingerprint,
        "account": solution.account_snapshot_fingerprint,
        "quantities": solution.quantities,
        "capital_use_units": solution.capital_use_units,
    })
    if (
        solution.order_ready
        or solution.reason != "PARTIAL_FILL_PROOF_REQUIRED"
        or solution.fingerprint != expected
        or tuple(sorted(solution.quantities, key=lambda item: item.action_id)) != solution.quantities
        or len({item.action_id for item in solution.quantities}) != len(solution.quantities)
    ):
        raise ValueError("execution solution partial-fill or fingerprint mismatch")
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
        market.fingerprint, account_fingerprint, market.quantities, capital_use, False,
        "PARTIAL_FILL_PROOF_REQUIRED", fingerprint({
            "market": market.fingerprint, "account": account_fingerprint,
            "quantities": market.quantities, "capital_use_units": capital_use,
        }),
    )
    return ResolutionResult(ResolutionStatus.EXECUTION_SOLUTION, None, market, execution)


def _valid_account_snapshot(snapshot: AccountSnapshot | None, now: datetime) -> bool:
    if not isinstance(snapshot, AccountSnapshot) or not _utc(now) or not _utc(snapshot.captured_at):
        return False
    if (now - snapshot.captured_at).total_seconds() < 0 or (now - snapshot.captured_at).total_seconds() > ACCOUNT_FRESHNESS_SECONDS:
        return False
    if any(type(value) is not int or value < 0 for value in (snapshot.max_per_trade_cost_units, snapshot.unsettled_capital_units, snapshot.max_total_unsettled_capital_units)):
        return False
    if not all(
        isinstance(balance, AccountBalance)
        and all(isinstance(value, str) and value for value in (balance.venue_id, balance.account_id, balance.asset_id))
        and type(balance.available_units) is int and balance.available_units >= 0
        and type(balance.allowance_units) is int and balance.allowance_units >= 0
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


def _exact_object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected fields")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("must be an array")
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("must be an integer")
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
    int(value[7:], 16)
    return value


def _quantity_from_payload(value: object) -> ActionQuantity:
    item = _exact_object(value, {"action_id", "quantity_lots"})
    if not isinstance(item["action_id"], str) or not item["action_id"]:
        raise ValueError("quantity action ID is invalid")
    quantity = _nonnegative_int(item["quantity_lots"])
    return ActionQuantity(item["action_id"], quantity)


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor
