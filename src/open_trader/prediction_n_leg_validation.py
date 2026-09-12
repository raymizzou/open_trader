"""Issue #71: N>=3 no-submit validation harness.

Two independent paths that report PASS/FAIL/BLOCKED honestly and never place
an order:

- ``run_replay`` replays a frozen canonical snapshot (problem + capture-time
  books + fingerprints) through the #52 compile/normalize/request seams and
  the #50 solve/verify seam, requires a solver-chosen portfolio with at least
  three positive legs, and differentially checks the fixed portfolio against
  the #48 exact oracle (``evaluate_fixed_portfolio``).
- ``run_live`` reads the approved ACTIVE relation set from a v2 catalog in
  SQLite ``mode=ro`` (never a write connection), compiles the same seams, and
  fetches current books through an injectable read-only seam.  With no ACTIVE
  N>=3 relation it returns BLOCKED with a precise reason instead of inventing
  one (production state today, tracked as #88).

The preflight decision is no longer a hardcoded UNKNOWN: every replay and
live run proves the fixed execution solution with the #74 fill adversary and
reports the three-state result.  ``order_ready`` stays False in this harness
(observe phase, no scope capability), and a fail-closed execution seam raises
on any submit/mutation call and every report asserts zero side effects.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from open_trader.prediction_arbitrage import BookLevel
from open_trader.polymarket_relation_discovery import _mechanical_fee_fields
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_live_resolver import (
    PredictionLiveResolver,
    USD_UNITS_PER_DOLLAR,
    _leg_token_by_contract,
    normalize_problem,
    resolve_leg_token,
)
from open_trader.prediction_market_solution import (
    EXECUTABLE_REASON,
    AccountView,
    ExecutionSolution,
    MarketSolution,
    build_solve_request,
    execution_solution_from_market,
    market_solution_from_verification,
)
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    REQUEST_SCHEMA_V1,
    ActionQuantity,
    ActionSide,
    OracleBudget,
    OracleRequest,
    RelationKind,
    SearchMode,
    TerminalKind,
    canonical_payload,
    fingerprint,
    problem_from_payload,
)
from open_trader.prediction_n_leg_mode import DEFAULT_SAFETY_CONFIG
from open_trader.prediction_n_leg_oracle import (
    evaluate_fixed_portfolio,
    evaluate_paper_portfolio,
)
from open_trader.prediction_n_leg_read_model import (
    PARTIAL_FILL_PROOF_REQUIRED,
    PARTIAL_FILL_UNSAFE,
    SCOPE_OBSERVE_ONLY,
)
from open_trader.prediction_partial_fill import (
    PARTIAL_FILL_SAFE,
    PARTIAL_FILL_UNKNOWN,
    fill_adversary_problem_from_market_solution,
    prove_partial_fill,
)
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
    economic_fingerprint,
)
from open_trader.prediction_n_leg_validation_books import (
    PaperBook,
    paper_book_from_payload,
)
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_server import SolverServerOwner
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    ProofInput,
    VerificationResult,
    candidate_evidence_from_payload,
    model_fingerprint,
    quote_fingerprint,
    solve,
    VerificationStatus,
    verification_result_from_payload,
    verify,
)


FROZEN_SNAPSHOT_SCHEMA_V1 = "open_trader.prediction_n_leg_validation.frozen_snapshot.v1"
REPORT_SCHEMA_V1 = "open_trader.prediction_n_leg_validation.report.v1"
PAPER_REPORT_SCHEMA_V1 = "open_trader.prediction_n_leg_validation.paper_three_way.v1"
MIN_LEGS = 3

# Small-scale budget that covers the N=3 fixture (8 quantity vectors, 8 joint
# states); the production live budget stays unchanged.
VALIDATION_BUDGET = OracleBudget(
    max_quantity_vectors=16, max_joint_states=16, max_support_rechecks=2
)
VALIDATION_LIMITS = BenchmarkLimits(
    soft_time_limit_ms=2_000,
    hard_time_limit_ms=4_000,
    memory_limit_bytes=1 << 30,
    max_constraint_generation_rounds=3,
)


def live_budget_from_flags(
    live_max_joint_states: int | None, live_max_quantity_vectors: int | None
) -> OracleBudget:
    """Assemble the live-path budget from the CLI budget flags.

    ``None`` (flag absent) keeps the corresponding ``VALIDATION_BUDGET``
    value and the support-recheck cap always stays the original constant, so
    the default assembly is field-identical to ``VALIDATION_BUDGET``.  Only
    the live path consumes this; the replay path keeps its
    ``VALIDATION_BUDGET`` default and ``VALIDATION_LIMITS`` stay untouched.
    """

    return OracleBudget(
        max_quantity_vectors=(
            VALIDATION_BUDGET.max_quantity_vectors
            if live_max_quantity_vectors is None
            else live_max_quantity_vectors
        ),
        max_joint_states=(
            VALIDATION_BUDGET.max_joint_states
            if live_max_joint_states is None
            else live_max_joint_states
        ),
        max_support_rechecks=VALIDATION_BUDGET.max_support_rechecks,
    )

_VALIDATION_ACCOUNT = AccountView(10**18, 10**18, 0)

#: The validation harness proves every fixed solution with the #74 fill
#: adversary.  Caps default to the mode contract's safety config; the proof
#: runs synchronously under its own hard time limit (independent of the
#: master solve limits) and a timeout is reported UNKNOWN, never safe.
DEFAULT_CAP_CONFIG_VERSION = "caps-v1"
PROOF_TIME_LIMIT_MS = 4_000


class FailClosedExecution:
    """No-submit seam: any trading mutation raises before a remote call."""

    def __init__(self, account: AccountView = _VALIDATION_ACCOUNT) -> None:
        self._account = account
        self.submit_attempts = 0
        self.mutation_attempts = 0

    def n_leg_account_view(self) -> AccountView:
        return self._account

    def submit(self, *args: object, **kwargs: object) -> object:
        self.submit_attempts += 1
        raise AssertionError("no-submit validation: submit must never be reached")

    def mutate(self, *args: object, **kwargs: object) -> object:
        self.mutation_attempts += 1
        raise AssertionError("no-submit validation: mutation must never be reached")


class _OwnershipLock:
    """Exclusive lock over the isolated validation data dir."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle = None

    def __enter__(self) -> "_OwnershipLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(f"validation ownership unavailable: {self._path}") from exc
        self._handle = handle
        return self

    def __exit__(self, *exc: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def readonly_v2_relations(db_path: str | Path) -> dict[str, object]:
    """Export the current v2 catalog relation set with SQLite mode=ro."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"catalog database not found: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        meta = connection.execute(
            "SELECT generation_number FROM catalog_v2_meta WHERE singleton=1"
        ).fetchone()
        latest = dict(
            connection.execute("SELECT identity, version_id FROM catalog_v2_latest")
        )
        versions: dict[str, tuple[str, str, str, str]] = {}
        for version_id, identity, payload, status, activation_status in connection.execute(
            "SELECT version_id, identity, payload, status, activation_status "
            "FROM catalog_v2_versions"
        ):
            versions[version_id] = (
                identity,
                payload,
                status or "",
                activation_status or "",
            )
        rows: dict[str, dict[str, object]] = {}
        for identity, version_id in latest.items():
            record = versions.get(version_id)
            if record is None:
                continue
            _, payload_raw, status, activation = record
            try:
                payload = json.loads(payload_raw)
            except (TypeError, ValueError):
                continue
            model = payload.get("model") if isinstance(payload, dict) else None
            rows[identity] = {
                "version_id": version_id,
                "status": status,
                "activation": activation,
                "endpoints": payload.get("endpoints") if isinstance(payload, dict) else [],
                "model": model if isinstance(model, dict) else {},
            }
        return {
            "generation": int(meta[0]) if meta else 0,
            "rows": rows,
        }
    finally:
        connection.close()


def frozen_snapshot_from_file(path: str | Path) -> dict[str, object]:
    """Load a frozen snapshot and reject any content/source tampering."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != FROZEN_SNAPSHOT_SCHEMA_V1:
        raise ValueError(f"not a frozen validation snapshot: {path}")
    problem = payload.get("problem")
    books = payload.get("books")
    source = payload.get("source")
    if not isinstance(problem, dict) or not isinstance(books, dict) or not isinstance(source, dict):
        raise ValueError("frozen snapshot requires problem, books and source")
    if fingerprint({"problem": problem, "books": books}) != payload.get("content_fingerprint"):
        raise ValueError("frozen snapshot content fingerprint mismatch")
    if fingerprint(source) != payload.get("source_fingerprint"):
        raise ValueError("frozen snapshot source fingerprint mismatch")
    return payload


def _snapshot_from_frozen(snapshot: Mapping[str, object]) -> ComponentSnapshot:
    component_id = str(snapshot.get("component_id") or "frozen")
    problem = problem_from_payload(snapshot["problem"])
    books = snapshot["books"]
    legs: list[SnapshotLeg] = []
    for action in problem.actions:
        raw = books.get(action.action_id)
        if not isinstance(raw, dict):
            raise ValueError(f"frozen snapshot has no book for action {action.action_id}")
        levels = raw.get("asks") if action.side.value == "BUY_YES" else raw.get("bids")
        if not isinstance(levels, list) or not levels:
            raise ValueError(f"frozen snapshot has no executable book for action {action.action_id}")
        converted = tuple(
            BookLevel(Decimal(str(level["price"])), Decimal(str(level["size"])))
            for level in levels
        )
        book = (
            LegBook((), converted, None, True)
            if action.side.value == "BUY_YES"
            else LegBook(converted, (), None, True)
        )
        legs.append(
            SnapshotLeg(
                action.action_id,
                book,
                problem.as_of,
                problem.as_of,
                None,
            )
        )
    return ComponentSnapshot(component_id, tuple(legs))


def _solve_verified(
    component_id: str,
    problem: object,
    *,
    budget: OracleBudget,
    limits: BenchmarkLimits,
    code_version: str,
) -> tuple[object, object, MarketSolution | None, float]:
    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, budget),
        limits,
        quote_fingerprint(problem),
        0,
        code_version,
    )
    started = time.perf_counter()
    evidence = candidate_evidence_from_payload(solve(canonical_payload(proof_input)))
    if evidence.candidate is None:
        # Component-negative path: the exact #48 oracle proves NO_QUALIFIED_
        # OPPORTUNITY (or UNKNOWN) without a raw candidate.
        verification = verification_result_from_payload(
            verify(canonical_payload(proof_input)), source=proof_input
        )
    else:
        verification = verification_result_from_payload(
            verify(canonical_payload(evidence)), source=evidence
        )
    elapsed = time.perf_counter() - started
    market = market_solution_from_verification(component_id, problem, evidence, verification)
    return evidence, verification, market, elapsed


def _oracle_differential(
    problem: object,
    market: MarketSolution,
    budget: OracleBudget,
    verified_worst_scenario: object,
) -> dict[str, object]:
    evaluation = evaluate_fixed_portfolio(problem, market.quantities, budget)
    if verified_worst_scenario is None:
        verified_atoms = ()
    else:
        verified_atoms = tuple(
            sorted(
                (atom.market_contract_id, atom.atom_id)
                for atom in verified_worst_scenario.atoms
            )
        )
    checks = (
        (
            "quantities",
            tuple((q.action_id, q.quantity_lots) for q in evaluation.quantities),
            tuple((q.action_id, q.quantity_lots) for q in market.quantities),
        ),
        (
            "guaranteed_profit_units",
            evaluation.guaranteed_profit_units,
            market.guaranteed_profit_units,
        ),
        (
            "cost_upper_bound_units",
            evaluation.cost_upper_bound_units,
            market.bounded_cost_units,
        ),
        (
            "payout_lower_bound_units",
            evaluation.payout_lower_bound_units,
            market.bounded_payout_units,
        ),
        (
            "worst_scenario_atoms",
            tuple(
                sorted((atom.market_contract_id, atom.atom_id) for atom in evaluation.worst_scenario.atoms)
            ),
            verified_atoms,
        ),
        ("qualification", evaluation.failed_qualification_ids, ()),
    )
    results = [
        {
            "check": name,
            "oracle": oracle_value,
            "verified": verified_value,
            "pass": oracle_value == verified_value,
        }
        for name, oracle_value, verified_value in checks
    ]
    return {
        "pass": all(item["pass"] for item in results),
        "checks": results,
        "evaluation": {
            "quantities": [
                {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
                for q in evaluation.quantities
            ],
            "guaranteed_profit_units": evaluation.guaranteed_profit_units,
            "cost_upper_bound_units": evaluation.cost_upper_bound_units,
            "payout_lower_bound_units": evaluation.payout_lower_bound_units,
            "worst_scenario_atoms": [
                {"market_contract_id": atom.market_contract_id, "atom_id": atom.atom_id}
                for atom in evaluation.worst_scenario.atoms
            ],
            "failed_qualification_ids": evaluation.failed_qualification_ids,
        },
    }


def _prove_fixed_solution(
    market: MarketSolution,
    problem: object,
    execution: ExecutionSolution,
    *,
    cap_config_version: str,
    max_partial_fill_loss: int,
    max_auto_repair_loss: int,
    proof_time_limit_ms: int,
) -> dict[str, object]:
    """Run the #74 fill-adversary proof for one fixed execution solution."""
    try:
        adversary = fill_adversary_problem_from_market_solution(
            execution,
            problem,
            cap_config_version=cap_config_version,
            max_partial_fill_loss=max_partial_fill_loss,
            max_auto_repair_loss=max_auto_repair_loss,
        )
        record, counterexample = prove_partial_fill(
            adversary, time_limit_ms=proof_time_limit_ms
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "order_ready": False,
            "partial_fill_proof": PARTIAL_FILL_UNKNOWN,
            "reason": PARTIAL_FILL_PROOF_REQUIRED,
            "proof": {
                "status": PARTIAL_FILL_UNKNOWN,
                "reason": f"INVALID_INPUT:{exc}",
                "lower_bound_units": 0,
                "upper_bound_units": 0,
                "fingerprint": None,
            },
            "quantities": [
                {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
                for q in market.quantities
            ],
            "capital_use_units": execution.capital_use_units,
            "market_solution_fingerprint": execution.market_solution_fingerprint,
        }
    reason = (
        PARTIAL_FILL_UNSAFE
        if record.status == PARTIAL_FILL_UNSAFE
        else (
            PARTIAL_FILL_PROOF_REQUIRED
            if record.status != PARTIAL_FILL_SAFE
            else SCOPE_OBSERVE_ONLY
        )
    )
    return {
        "order_ready": False,
        "partial_fill_proof": record.status,
        "reason": reason,
        "proof": {
            "status": record.status,
            "solver_termination": record.solver_termination,
            "verifier_status": record.verifier_status,
            "lower_bound_units": record.solver_lower_bound,
            "upper_bound_units": record.solver_upper_bound,
            "cap_units": record.max_partial_fill_loss,
            "fingerprint": record.fingerprint,
            "counterexample": counterexample,
        },
        "quantities": [
            {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
            for q in market.quantities
        ],
        "capital_use_units": execution.capital_use_units,
        "market_solution_fingerprint": execution.market_solution_fingerprint,
    }


def _execution_decision(
    market: MarketSolution,
    problem: object,
    account: AccountView = _VALIDATION_ACCOUNT,
    *,
    cap_config_version: str = DEFAULT_CAP_CONFIG_VERSION,
    max_partial_fill_loss: int = int(
        DEFAULT_SAFETY_CONFIG["max_partial_fill_loss_units"]
    ),
    max_auto_repair_loss: int = int(
        DEFAULT_SAFETY_CONFIG["max_auto_repair_loss_units"]
    ),
    proof_time_limit_ms: int = PROOF_TIME_LIMIT_MS,
) -> dict[str, object]:
    execution = execution_solution_from_market(
        market,
        problem,
        account,
        max_total_unsettled_capital=account.available_units,
    )
    return _prove_fixed_solution(
        market,
        problem,
        execution,
        cap_config_version=cap_config_version,
        max_partial_fill_loss=max_partial_fill_loss,
        max_auto_repair_loss=max_auto_repair_loss,
        proof_time_limit_ms=proof_time_limit_ms,
    )


def run_replay(
    snapshot: Mapping[str, object],
    *,
    budget: OracleBudget = VALIDATION_BUDGET,
    limits: BenchmarkLimits = VALIDATION_LIMITS,
    code_version: str = "issue-71",
) -> dict[str, object]:
    started = time.perf_counter()
    component_id = str(snapshot.get("component_id") or "frozen")
    try:
        problem = normalize_problem(problem_from_payload(snapshot["problem"]))
        component_snapshot = _snapshot_from_frozen(snapshot)
        request = build_solve_request(
            problem,
            component_snapshot,
            budget=budget,
            limits=limits,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        evidence, verification, market, solve_seconds = _solve_verified(
            component_id,
            request.request.problem,
            budget=budget,
            limits=limits,
            code_version=code_version,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "status": "FAIL",
            "reason": f"INVALID_FROZEN_SNAPSHOT: {exc}",
            "component_id": component_id,
        }
    if market is None:
        return {
            "status": "FAIL",
            "reason": f"{verification.status.value}:{verification.unknown_reason or 'NO_MARKET_SOLUTION'}",
            "component_id": component_id,
        }
    legs = tuple(q for q in market.quantities if q.quantity_lots > 0)
    if len(legs) < MIN_LEGS:
        return {
            "status": "FAIL",
            "reason": "N_LESS_THAN_3",
            "component_id": component_id,
            "legs": len(legs),
            "quantities": [
                {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
                for q in market.quantities
            ],
        }
    proof = verification.solution.payout_proof
    oracle_started = time.perf_counter()
    oracle = _oracle_differential(
        request.request.problem,
        market,
        budget,
        proof.worst_scenario,
    )
    oracle_seconds = time.perf_counter() - oracle_started
    expected = snapshot.get("expected")
    expected_checks = []
    if isinstance(expected, dict):
        expected_actions = tuple(expected.get("portfolio_actions") or ())
        actual_actions = tuple(q.action_id for q in legs)
        expected_checks.append(
            {
                "check": "portfolio_actions",
                "expected": expected_actions,
                "actual": actual_actions,
                "pass": expected_actions == actual_actions,
            }
        )
        expected_profit = expected.get("guaranteed_profit_units")
        if expected_profit is not None:
            expected_checks.append(
                {
                    "check": "guaranteed_profit_units",
                    "expected": expected_profit,
                    "actual": market.guaranteed_profit_units,
                    "pass": expected_profit == market.guaranteed_profit_units,
                }
            )
    proof = verification.solution.payout_proof
    if not oracle["pass"] or not all(item["pass"] for item in expected_checks):
        return {
            "status": "FAIL",
            "reason": "ORACLE_DIFFERENTIAL" if not oracle["pass"] else "EXPECTED_MISMATCH",
            "component_id": component_id,
            "legs": len(legs),
            "oracle_differential": oracle,
            "expected_vs_actual": expected_checks,
        }
    return {
        "status": "PASS",
        "component_id": component_id,
        "legs": len(legs),
        "quantities": [
            {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
            for q in market.quantities
        ],
        "market": {
            "guaranteed_profit_units": market.guaranteed_profit_units,
            "bounded_cost_units": market.bounded_cost_units,
            "bounded_payout_units": market.bounded_payout_units,
            "capital_release_at": (
                market.capital_release_at.isoformat() if market.capital_release_at else None
            ),
            "global_search_closed": market.global_search_closed,
            "qualification_fingerprint": proof.qualification_fingerprint,
            "worst_state_atoms": [
                {
                    "market_contract_id": atom.market_contract_id,
                    "atom_id": atom.atom_id,
                }
                for atom in (proof.worst_scenario.atoms if proof.worst_scenario else ())
            ],
        },
        "execution_decision": _execution_decision(market, request.request.problem),
        "oracle_differential": oracle,
        "expected_vs_actual": expected_checks,
        "fingerprints": {
            "model": model_fingerprint(request.request.problem),
            "quote": quote_fingerprint(request.request.problem),
            "structure": market.structure_fingerprint,
            "portfolio": fingerprint({"quantities": market.quantities}),
            "verification": market.verification_fingerprint,
            "content": snapshot.get("content_fingerprint"),
            "source": snapshot.get("source_fingerprint"),
        },
        "constraint_generation_rounds": {
            "master_rounds": evidence.solver_evidence.master_rounds,
            "adversary_rounds": evidence.solver_evidence.adversary_rounds,
        },
        "timings": {
            "solve_seconds": round(solve_seconds, 6),
            "oracle_seconds": round(oracle_seconds, 6),
            "end_to_end_seconds": round(time.perf_counter() - started, 6),
        },
    }


PAPER_THREE_WAY_TEMPLATE = "FOOTBALL_REGULAR_TIME_3WAY_V1"
PAPER_BOOK_FRESHNESS = timedelta(seconds=10)
_PAPER_MISSING = object()


def _paper_row_items(
    catalog_rows: object,
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    """Normalize a catalog mapping or an exported row list for paper mode."""

    if isinstance(catalog_rows, Mapping):
        return tuple(
            (str(identity), row)
            for identity, row in catalog_rows.items()
            if isinstance(row, Mapping)
        )
    if isinstance(catalog_rows, Sequence) and not isinstance(
        catalog_rows, (str, bytes)
    ):
        rows: list[tuple[str, Mapping[str, object]]] = []
        for index, row in enumerate(catalog_rows):
            if not isinstance(row, Mapping):
                continue
            identity = row.get("identity") or row.get("version_id") or index
            rows.append((str(identity), row))
        return tuple(rows)
    raise ValueError("catalog_rows must be a mapping or row sequence")


def _paper_field(
    value: object, *names: str, default: object = None
) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        try:
            return getattr(value, name)
        except (AttributeError, TypeError):
            continue
    return default


def _paper_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _paper_supported_candidate(
    identity: str, row: Mapping[str, object]
) -> tuple[
    str,
    Mapping[str, object],
    object,
    Mapping[str, Mapping[str, object]],
    Mapping[str, Mapping[str, object]],
] | None:
    """Decode one supported three-way row without entering formal admission."""

    model = _paper_mapping(row.get("model"))
    if model is None or model.get("template") != PAPER_THREE_WAY_TEMPLATE:
        return None
    if row.get("relation_type", "EXACTLY_ONE") != "EXACTLY_ONE":
        return None
    if model.get("member_count") != 3 or not model.get("group_id"):
        return None
    directions = _paper_mapping(model.get("directions"))
    rules = _paper_mapping(model.get("rules"))
    tokens = _paper_mapping(model.get("tokens"))
    payouts = _paper_mapping(model.get("payouts"))
    problem_payload = _paper_mapping(model.get("problem"))
    if any(value is None for value in (directions, rules, tokens, payouts, problem_payload)):
        return None
    try:
        problem = normalize_problem(
            problem_from_payload(problem_payload, allow_unknown_data=True)
        )
    except (TypeError, ValueError):
        return None
    contracts = tuple(sorted(state.market_contract_id for state in problem.terminal_state_sets))
    if len(contracts) != 3 or len(set(contracts)) != 3 or len(problem.actions) != 3:
        return None
    if set(directions) != set(contracts) or set(rules) != set(contracts):
        return None
    if set(directions.values()) != {"HOME_WIN", "DRAW", "AWAY_WIN"}:
        return None
    if set(tokens) != set(contracts) or set(payouts) != set(contracts):
        return None
    if any(
        not isinstance(rules[contract], str) or not rules[contract].strip()
        for contract in contracts
    ):
        return None
    for contract in contracts:
        token_pair = _paper_mapping(tokens[contract])
        payout = _paper_mapping(payouts[contract])
        if (
            token_pair is None
            or not isinstance(token_pair.get("YES"), str)
            or not token_pair["YES"].strip()
            or not isinstance(token_pair.get("NO"), str)
            or not token_pair["NO"].strip()
            or token_pair["YES"] == token_pair["NO"]
            or payout is None
            or payout.get("NORMAL_YES") != 1
            or payout.get("NORMAL_NO") != 0
        ):
            return None
    incomplete_reasons = model.get("incomplete_reasons", ())
    if not isinstance(incomplete_reasons, Sequence) or isinstance(
        incomplete_reasons, (str, bytes)
    ):
        return None
    if any(reason != "MISSING_CAPITAL_RELEASE_AT" for reason in incomplete_reasons):
        return None
    actions_by_contract = {
        action.market_contract_id: action for action in problem.actions
    }
    if set(actions_by_contract) != set(contracts):
        return None
    relation = tuple(problem.constraint_model.relations)
    if (
        len(relation) != 1
        or relation[0].kind.value != "EXACTLY_ONE"
        or set(relation[0].contract_ids) != set(contracts)
    ):
        return None
    endpoint_values = row.get("endpoints")
    if not isinstance(endpoint_values, Sequence) or isinstance(
        endpoint_values, (str, bytes)
    ):
        return None
    endpoints: dict[str, Mapping[str, object]] = {}
    for endpoint in endpoint_values:
        if not isinstance(endpoint, Mapping):
            return None
        contract = endpoint.get("contract_id")
        if not isinstance(contract, str) or contract in endpoints:
            return None
        endpoints[contract] = endpoint
    if set(endpoints) != set(contracts):
        return None
    return identity, row, problem, endpoints, {
        contract: _paper_mapping(tokens[contract]) or {}
        for contract in contracts
    }


def _paper_fee_facts(
    endpoint: Mapping[str, object],
    model: Mapping[str, object],
    contract: str,
) -> tuple[Decimal, dict[str, object], str | None]:
    """Decode official fee facts; legacy base-fee fields are ignored."""

    fees_enabled, fee_rate = _mechanical_fee_fields(endpoint)
    # Catalog rows flatten the official fee flag/rate. Keep accepting that
    # normalized shape while the discovery path continues to read the nested
    # Gamma/SDK representation.
    if fees_enabled is None:
        direct_enabled = _paper_field(
            endpoint, "fees_enabled", "feesEnabled", default=_PAPER_MISSING
        )
        if direct_enabled is not _PAPER_MISSING:
            fees_enabled = (
                direct_enabled if type(direct_enabled) is bool else None
            )
    if fee_rate is None:
        direct_rate = _paper_field(
            endpoint, "fee_rate", "feeRate", default=_PAPER_MISSING
        )
        if direct_rate is not _PAPER_MISSING:
            try:
                fee_rate = (
                    direct_rate
                    if isinstance(direct_rate, Decimal)
                    else Decimal(str(direct_rate))
                )
            except (InvalidOperation, TypeError, ValueError):
                fee_rate = None
            if fee_rate is not None and not fee_rate.is_finite():
                fee_rate = None
    model_facts = _paper_mapping(model.get("fee_facts"))
    model_fact = (
        _paper_mapping(model_facts.get(contract))
        if model_facts is not None
        else None
    )
    exponent = _paper_field(endpoint, "fee_exponent", "feeExponent", default=None)
    if exponent is None and model_fact is not None:
        exponent = _paper_field(model_fact, "exponent", "fee_exponent", default=None)
    taker_only = _paper_field(endpoint, "taker_only", "takerOnly", default=None)
    if taker_only is None and model_fact is not None:
        taker_only = _paper_field(model_fact, "taker_only", "takerOnly", default=None)
    if fees_enabled is False:
        if fee_rate is not None and (not fee_rate.is_finite() or fee_rate != 0):
            return Decimal("0"), {}, "UNKNOWN_FEE_FACTS"
        return Decimal("0"), {
            "status": "FREE",
            "rate": "0",
            "exponent": None,
            "taker_only": None,
        }, None
    if fees_enabled is not True or fee_rate is None:
        return Decimal("0"), {}, "UNKNOWN_FEE_FACTS"
    if (
        not fee_rate.is_finite()
        or fee_rate < 0
        or fee_rate > 1
        or type(exponent) is not int
        or exponent != 1
        or taker_only is not True
    ):
        return Decimal("0"), {}, "UNKNOWN_FEE_FACTS"
    return fee_rate * Decimal("10000"), {
        "status": "CHARGING",
        "rate": format(fee_rate, "f"),
        "exponent": exponent,
        "taker_only": taker_only,
    }, None


def _paper_usd(units: int) -> str:
    value = Decimal(units) / Decimal(USD_UNITS_PER_DOLLAR)
    return format(value.normalize(), "f")


def _paper_blocked(
    reason: str,
    data_dir: Path,
    detail: str,
    *,
    component_id: str | None = None,
    evaluated_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "schema_version": PAPER_REPORT_SCHEMA_V1,
        "mode": "PAPER_THREE_WAY",
        "status": "BLOCKED",
        "reason": reason,
        "detail": detail,
        "component_id": component_id,
        "qualification_status": "UNKNOWN",
        "capital_release_at": None,
        "evaluated_at": (
            evaluated_at.isoformat() if evaluated_at is not None else None
        ),
        "order_ready": False,
        "zero_side_effects": {
            "submitted_orders": 0,
            "mutation_attempts": 0,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
    }


def _paper_action_cost(action: object, quantity_lots: int) -> int:
    total = 0
    remaining = quantity_lots
    for cost_slice in action.cost_slices:
        if remaining < cost_slice.first_lot:
            break
        last = min(remaining, cost_slice.last_lot)
        total += (last - cost_slice.first_lot + 1) * (
            cost_slice.incremental_cost_upper_bound_units
        )
    return total


def _paper_price_bound(book: PaperBook, action: object, quantity_lots: int) -> Decimal:
    """Return the last ask price consumed by one fixed paper leg."""

    remaining = quantity_lots
    for level in book.asks:
        lots = int(
            (level.size * action.quantity_scale / action.lot_step_units).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if lots <= 0:
            continue
        if lots >= remaining:
            return level.price
        remaining -= lots
    raise ValueError("paper quantity exceeds executable ask depth")


def _paper_consumed_prices_on_tick(
    book: PaperBook, action: object, quantity_lots: int
) -> bool:
    """Check the exact Decimal tick grid for the levels a paper order consumes."""

    tick_size = book.tick_size
    if tick_size is None or not tick_size.is_finite() or tick_size <= 0:
        return False
    remaining = quantity_lots
    for level in book.asks:
        available_lots = int(
            (
                level.size * action.quantity_scale / action.lot_step_units
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        if available_lots <= 0:
            continue
        if level.price % tick_size != 0:
            return False
        remaining -= min(available_lots, remaining)
        if remaining <= 0:
            return True
    return False


def _observation_blocked(reason: str, detail: str) -> dict[str, object]:
    """Return the small, side-effect-free result shape used by observation."""

    return {
        "status": "BLOCKED",
        "reason": reason,
        "detail": detail,
        "qualification_status": "UNKNOWN",
        "order_ready": False,
        "execution_calls": 0,
        "zero_side_effects": {
            "submitted_orders": 0,
            "mutation_attempts": 0,
            "catalog_read_only": True,
        },
    }


def _observation_projection(report: Mapping[str, object]) -> dict[str, object]:
    """Project the existing paper report to the monitor's immutable result."""

    result = dict(report)
    economics = report.get("economics")
    if isinstance(economics, Mapping):
        result.update(
            {
                "quantity_lots": economics.get("quantity_lots"),
                "payout_lower_bound_units": economics.get("payout_lower_bound_units"),
                "cost_upper_bound_units": economics.get("cost_upper_bound_units"),
                "guaranteed_profit_units": economics.get("guaranteed_profit_units"),
            }
        )
        cost = economics.get("cost_upper_bound_units")
        profit = economics.get("guaranteed_profit_units")
        if isinstance(cost, int) and cost > 0 and isinstance(profit, int):
            result["net_roi"] = Decimal(profit) / Decimal(cost)
        else:
            result["net_roi"] = None
    result.setdefault("execution_calls", 0)
    result.setdefault("order_ready", False)
    return result


def _native_observation(
    row: Mapping[str, object],
    books_payload: Mapping[str, object],
    *,
    as_of: datetime | None,
    budget: OracleBudget,
) -> dict[str, object]:
    """Price one complete native YES/NO pair from fresh token asks."""

    model = _paper_mapping(row.get("model"))
    problem_payload = _paper_mapping(model.get("problem")) if model else None
    if model is None or problem_payload is None:
        return _observation_blocked("UNKNOWN_MODEL_FACTS", "native row has no compiled problem")
    try:
        problem = normalize_problem(
            problem_from_payload(problem_payload, allow_unknown_data=True)
        )
    except (TypeError, ValueError) as exc:
        return _observation_blocked("UNKNOWN_MODEL_FACTS", f"invalid native problem: {exc}")
    if (
        row.get("relation_type") != "NATIVE_COMPLEMENT"
        or len(problem.actions) != 2
        or {action.side for action in problem.actions}
        != {ActionSide.BUY_YES, ActionSide.BUY_NO}
    ):
        return _observation_blocked("UNSUPPORTED_STRUCTURE", "observation supports one native YES/NO pair")
    endpoints = {
        str(endpoint.get("contract_id")): endpoint
        for endpoint in row.get("endpoints", ())
        if isinstance(endpoint, Mapping) and isinstance(endpoint.get("contract_id"), str)
    }
    contracts = {action.market_contract_id for action in problem.actions}
    if set(endpoints) != contracts:
        return _observation_blocked("UNKNOWN_MODEL_FACTS", "native endpoints do not cover both actions")
    relations = tuple(problem.constraint_model.relations)
    states = tuple(problem.terminal_state_sets)
    if (
        len(relations) != 1
        or relations[0].kind != RelationKind.NATIVE_COMPLEMENT
        or set(relations[0].contract_ids) != contracts
        or {state.market_contract_id for state in states} != contracts
        or any(
            {atom.kind for atom in state.atoms}
            != {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO, TerminalKind.SPLIT}
            for state in states
        )
        or len({state.settlement_observation_key.indicator_id for state in states}) != 1
    ):
        return _observation_blocked(
            "UNSUPPORTED_STRUCTURE",
            "native observation requires a same-condition complement model",
        )
    evaluated_at = as_of
    if evaluated_at is not None:
        if (
            not isinstance(evaluated_at, datetime)
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() != UTC.utcoffset(evaluated_at)
        ):
            return _observation_blocked("INVALID_AS_OF", "as_of must be a UTC-aware datetime")
        evaluated_at = evaluated_at.astimezone(UTC)
    if evaluated_at is None:
        evaluated_at = datetime.now(UTC)

    fee_bps_by_contract: dict[str, Decimal] = {}
    fee_reports: dict[str, dict[str, object]] = {}
    for contract in sorted(contracts):
        fee_bps, fee_report, fee_reason = _paper_fee_facts(
            endpoints[contract], model, contract
        )
        if fee_reason is not None:
            return _observation_blocked(
                fee_reason, f"fee facts unavailable or unsupported for {contract}"
            )
        fee_bps_by_contract[contract] = fee_bps
        fee_reports[contract] = fee_report

    books: dict[str, PaperBook] = {}
    for contract in sorted(contracts):
        if contract not in books_payload:
            return _observation_blocked("MISSING_BOOKS", f"book missing for token {contract}")
        try:
            books[contract] = paper_book_from_payload(
                books_payload[contract],
                token_id=contract,
                taker_fee_bps=fee_bps_by_contract[contract],
            )
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"TOKEN_ID_MISMATCH", "MISSING_BOOKS"} else "MISSING_BOOKS"
            return _observation_blocked(reason, f"invalid book for token {contract}: {exc}")

    minimum_lots: dict[str, int] = {}
    depth_lots: dict[str, int] = {}
    for action in problem.actions:
        contract = action.market_contract_id
        book = books[contract]
        if not book.available or not book.asks:
            return _observation_blocked("MISSING_BOOKS", f"book for {contract} is unavailable")
        if (
            book.minimum_order_size is None
            or not book.minimum_order_size.is_finite()
            or book.minimum_order_size <= 0
            or book.tick_size is None
            or not book.tick_size.is_finite()
            or book.tick_size <= 0
        ):
            return _observation_blocked("UNKNOWN_ORDER_RULES", f"minimum size or tick is unknown for {contract}")
        if action.lot_step_units != 1 or action.quantity_scale != 1:
            return _observation_blocked("UNKNOWN_ORDER_RULES", "native observation sizing requires unit lots")
        minimum_notional = book.minimum_order_notional
        if minimum_notional is not None and (
            not minimum_notional.is_finite() or minimum_notional <= 0
        ):
            return _observation_blocked(
                "UNKNOWN_ORDER_RULES",
                f"minimum order notional is invalid for {contract}",
            )
        if book.confirmed_at > evaluated_at:
            return _observation_blocked("FUTURE_BOOK", f"book for {contract} is newer than evaluation time")
        if evaluated_at - book.confirmed_at > PAPER_BOOK_FRESHNESS:
            return _observation_blocked("STALE_BOOK", f"book for {contract} is older than 10s")
        legal_lots = max(
            action.min_quantity_lots,
            int((book.minimum_order_size).to_integral_value(rounding=ROUND_CEILING)),
            (
                int(
                    (minimum_notional / book.asks[0].price).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                if minimum_notional is not None
                else 1
            ),
        )
        available_lots = int(
            sum(level.size for level in book.asks).to_integral_value(rounding=ROUND_FLOOR)
        )
        minimum_lots[contract] = legal_lots
        depth_lots[contract] = available_lots
        if available_lots < legal_lots:
            return _observation_blocked("INSUFFICIENT_DEPTH", f"book for {contract} cannot fill its minimum legal size")

    quantity_lots = max(minimum_lots.values())
    if any(depth < quantity_lots for depth in depth_lots.values()):
        return _observation_blocked("INSUFFICIENT_DEPTH", f"native pair cannot fill common quantity {quantity_lots}")
    for action in problem.actions:
        if not _paper_consumed_prices_on_tick(books[action.market_contract_id], action, quantity_lots):
            return _observation_blocked("OFF_TICK_PRICE", f"consumed ask price for {action.market_contract_id} is not aligned to its tick")
    prepared_actions = tuple(
        replace(action, min_quantity_lots=quantity_lots, max_quantity_lots=quantity_lots)
        for action in problem.actions
    )
    prepared_problem = replace(problem, actions=prepared_actions)
    component_id = f"native:{':'.join(sorted(contracts))}"
    snapshot = ComponentSnapshot(
        component_id,
        tuple(
            SnapshotLeg(
                action.action_id,
                books[action.market_contract_id],
                books[action.market_contract_id].confirmed_at,
                books[action.market_contract_id].confirmed_at,
                None,
            )
            for action in prepared_actions
        ),
    )
    try:
        request = build_solve_request(
            prepared_problem,
            snapshot,
            budget=budget,
            limits=VALIDATION_LIMITS,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        priced_problem = request.request.problem
        quantities = tuple(
            ActionQuantity(action.action_id, quantity_lots)
            for action in priced_problem.actions
        )
        evaluation = evaluate_paper_portfolio(priced_problem, quantities, budget)
    except (TypeError, ValueError, OverflowError) as exc:
        return _observation_blocked("UNKNOWN_MODEL_FACTS", f"native model could not be evaluated: {exc}")
    cost = evaluation.cost_upper_bound_units
    profit = evaluation.guaranteed_profit_units
    charging = any(report.get("status") == "CHARGING" for report in fee_reports.values())
    return {
        "status": "PASS",
        "reason": None,
        "component_id": component_id,
        "relation_type": "NATIVE_COMPLEMENT",
        "quantity_lots": quantity_lots,
        "payout_lower_bound_units": evaluation.payout_lower_bound_units,
        "cost_upper_bound_units": cost,
        "guaranteed_profit_units": profit,
        "net_roi": Decimal(profit) / Decimal(cost) if cost > 0 else None,
        "qualification_status": evaluation.time_qualification,
        "evaluated_at": evaluated_at.isoformat(),
        "fees": {"status": "CHARGING" if charging else "FREE", "legs": fee_reports},
        "order_ready": False,
        "execution_calls": 0,
        "zero_side_effects": {
            "submitted_orders": 0,
            "mutation_attempts": 0,
            "catalog_read_only": True,
        },
    }


def price_observation(
    candidate: object,
    books: Mapping[str, object],
    *,
    as_of: datetime | None = None,
    budget: OracleBudget = VALIDATION_BUDGET,
) -> dict[str, object]:
    """Price one supported observation candidate with bounded fresh asks.

    The existing three-way paper path remains the source of truth for its
    fixed equal-lot economics.  Native pairs use the same book adapter,
    integer cost slices, and paper oracle, with BUY_NO reading the NO token's
    asks.  This seam never creates an execution solution or performs a write.
    """

    if not isinstance(books, Mapping):
        return _observation_blocked("MISSING_BOOKS", "observation books must be a token mapping")
    if isinstance(candidate, Mapping) and any(
        name in candidate for name in ("relation_type", "endpoints", "model")
    ):
        selected = (
            str(candidate.get("identity") or candidate.get("version_id") or "observation"),
            candidate,
        )
    else:
        try:
            rows = _paper_row_items(candidate)
        except ValueError:
            return _observation_blocked("INVALID_CATALOG_ROWS", "candidate must be a catalog row or row mapping")
        selected = next(
            ((identity, row) for identity, row in sorted(rows, key=lambda item: item[0])
             if isinstance(row, Mapping)),
            None,
        )
    if selected is None:
        return _observation_blocked("NO_SUPPORTED_CANDIDATE", "no candidate row was supplied")
    _, row = selected
    if row.get("relation_type") == "NATIVE_COMPLEMENT":
        return _native_observation(row, books, as_of=as_of, budget=budget)
    if row.get("model", {}).get("template") == PAPER_THREE_WAY_TEMPLATE if isinstance(row.get("model"), Mapping) else False:
        # The shared core validates all three-way model, fee, order-rule,
        # depth, freshness, and unknown-release facts without touching disk.
        report = _price_three_way_core(
            {str(selected[0]): row},
            book_source=lambda _token_ids: books,
            data_dir=Path(),
            budget=budget,
            as_of=as_of,
        )
        return _observation_projection(report)
    return _observation_blocked("UNSUPPORTED_STRUCTURE", "observation supports native pairs and football three-way rows")


def _price_three_way_core(
    catalog_rows: object,
    *,
    book_source: Callable[[tuple[str, ...]], Mapping[str, object]] | None,
    data_dir: str | Path,
    budget: OracleBudget = VALIDATION_BUDGET,
    catalog: Mapping[str, object] | None = None,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Price one supported three-way relation using one read-only book batch.

    This path intentionally performs a fixed equal-lot paper calculation. It
    never enters formal relation admission, creates a payout proof, or calls an
    execution seam. A missing release date keeps qualification UNKNOWN while
    the monetary result remains inspectable.
    """

    data_dir = Path(data_dir)
    evaluated_at = as_of
    if evaluated_at is not None:
        if (
            not isinstance(evaluated_at, datetime)
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() != UTC.utcoffset(evaluated_at)
        ):
            return _paper_blocked(
                "INVALID_AS_OF",
                data_dir,
                "as_of must be a UTC-aware datetime",
            )
        evaluated_at = evaluated_at.astimezone(UTC)
    try:
        rows = _paper_row_items(catalog_rows)
    except ValueError as exc:
        return _paper_blocked("INVALID_CATALOG_ROWS", data_dir, str(exc), evaluated_at=evaluated_at)
    candidate = next(
        (
            item
            for item in sorted(rows, key=lambda value: value[0])
            if _paper_supported_candidate(*item) is not None
        ),
        None,
    )
    if candidate is None:
        return _paper_blocked(
            "NO_SUPPORTED_THREE_WAY",
            data_dir,
            "no supported FOOTBALL_REGULAR_TIME_3WAY_V1 row with known payout/rule facts",
            evaluated_at=evaluated_at,
        )
    decoded = _paper_supported_candidate(*candidate)
    assert decoded is not None
    identity, row, problem, endpoints, tokens = decoded
    component_id = f"component:{':'.join(sorted(tokens))}"
    model = row["model"]
    if not isinstance(model, Mapping):
        return _paper_blocked(
            "UNKNOWN_MODEL_FACTS",
            data_dir,
            "supported row has no model facts",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    if not callable(book_source):
        return _paper_blocked(
            "PAPER_BOOK_SOURCE_UNAVAILABLE",
            data_dir,
            "no read-only paper book seam configured",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    actions_by_contract = {
        action.market_contract_id: action for action in problem.actions
    }
    token_by_contract: dict[str, str] = {}
    fee_bps_by_contract: dict[str, Decimal] = {}
    fee_reports: dict[str, dict[str, object]] = {}
    for contract in sorted(actions_by_contract):
        action = actions_by_contract[contract]
        endpoint = endpoints[contract]
        model_token = tokens[contract].get("YES")
        endpoint_token = endpoint.get("yes_token_id")
        if endpoint_token is not None and not isinstance(endpoint_token, str):
            return _paper_blocked(
                "TOKEN_ID_MISMATCH",
                data_dir,
                f"invalid YES token for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        if endpoint_token is not None and endpoint_token != model_token:
            return _paper_blocked(
                "TOKEN_ID_MISMATCH",
                data_dir,
                f"endpoint/model YES token differs for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        token = endpoint_token or model_token
        if not isinstance(token, str) or not token.strip():
            return _paper_blocked(
                "UNKNOWN_MODEL_FACTS",
                data_dir,
                f"missing YES token for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        if action.side != ActionSide.BUY_YES:
            return _paper_blocked(
                "UNKNOWN_MODEL_FACTS",
                data_dir,
                "supported three-way paper legs must buy YES tokens",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        fee_bps, fee_report, fee_reason = _paper_fee_facts(endpoint, model, contract)
        if fee_reason is not None:
            return _paper_blocked(
                fee_reason,
                data_dir,
                f"fee facts unavailable or unsupported for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        token_by_contract[contract] = token
        fee_bps_by_contract[contract] = fee_bps
        fee_reports[contract] = fee_report
    token_ids = tuple(token_by_contract[contract] for contract in sorted(token_by_contract))
    try:
        raw_books = book_source(token_ids)
    except Exception as exc:
        return _paper_blocked(
            "MISSING_BOOKS",
            data_dir,
            f"paper book fetch failed: {exc}",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    if not isinstance(raw_books, Mapping):
        return _paper_blocked(
            "MISSING_BOOKS",
            data_dir,
            "paper book source did not return a token mapping",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    if evaluated_at is None:
        evaluated_at = datetime.now(UTC)
    books: dict[str, PaperBook] = {}
    for contract in sorted(actions_by_contract):
        token = token_by_contract[contract]
        if token not in raw_books:
            return _paper_blocked(
                "MISSING_BOOKS",
                data_dir,
                f"book missing for token {token}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        try:
            books[token] = paper_book_from_payload(
                raw_books[token],
                token_id=token,
                taker_fee_bps=fee_bps_by_contract[contract],
            )
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"TOKEN_ID_MISMATCH", "MISSING_BOOKS"} else "MISSING_BOOKS"
            return _paper_blocked(
                reason,
                data_dir,
                f"invalid book for token {token}: {exc}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
    minimum_lots: dict[str, int] = {}
    depth_lots_by_contract: dict[str, int] = {}
    minimum_notional_by_contract: dict[str, Decimal | None] = {}
    order_rules_by_contract: dict[str, dict[str, object]] = {}
    for contract in sorted(actions_by_contract):
        action = actions_by_contract[contract]
        book = books[token_by_contract[contract]]
        if not book.available or not book.asks:
            return _paper_blocked(
                "MISSING_BOOKS",
                data_dir,
                f"book for {contract} is unavailable",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        if (
            book.minimum_order_size is None
            or not book.minimum_order_size.is_finite()
            or book.minimum_order_size <= 0
            or book.tick_size is None
            or not book.tick_size.is_finite()
            or book.tick_size <= 0
        ):
            return _paper_blocked(
                "UNKNOWN_ORDER_RULES",
                data_dir,
                f"minimum size or tick is unknown for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        minimum_notional = book.minimum_order_notional
        if minimum_notional is not None and (
            not minimum_notional.is_finite() or minimum_notional <= 0
        ):
            return _paper_blocked(
                "UNKNOWN_ORDER_RULES",
                data_dir,
                f"minimum order notional is invalid for {contract}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        if action.lot_step_units != 1 or action.quantity_scale != 1:
            return _paper_blocked(
                "UNKNOWN_ORDER_RULES",
                data_dir,
                "three-way paper sizing requires unit lots",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        size_lots = int(
            (book.minimum_order_size * action.quantity_scale / action.lot_step_units).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        notional_lots = 1
        if minimum_notional is not None:
            notional_lots = int(
                (minimum_notional / book.asks[0].price).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        minimum_lots[contract] = max(
            action.min_quantity_lots, size_lots, notional_lots
        )
        minimum_notional_by_contract[contract] = minimum_notional
        order_rules_by_contract[contract] = {
            "minimum_order_size": format(book.minimum_order_size, "f"),
            "minimum_order_notional": (
                format(minimum_notional, "f")
                if minimum_notional is not None
                else None
            ),
            "tick_size": format(book.tick_size, "f"),
            "status": "KNOWN" if minimum_notional is not None else "UNKNOWN_MINIMUM_NOTIONAL",
        }
        if book.confirmed_at > evaluated_at:
            return _paper_blocked(
                "FUTURE_BOOK",
                data_dir,
                f"book for {contract} is newer than evaluation time",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        if evaluated_at - book.confirmed_at > PAPER_BOOK_FRESHNESS:
            return _paper_blocked(
                "STALE_BOOK",
                data_dir,
                f"book for {contract} is older than {PAPER_BOOK_FRESHNESS.total_seconds():g}s",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
        depth_lots = int(
            (
                sum(level.size for level in book.asks)
                * action.quantity_scale
                / action.lot_step_units
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        depth_lots_by_contract[contract] = depth_lots
        if depth_lots < minimum_lots[contract]:
            return _paper_blocked(
                "INSUFFICIENT_DEPTH",
                data_dir,
                f"book for {contract} cannot fill its minimum legal size",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
    quantity_lots = max(minimum_lots.values())
    if quantity_lots <= 0:
        return _paper_blocked(
            "UNKNOWN_ORDER_RULES",
            data_dir,
            "common legal quantity is not positive",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    for contract, depth_lots in depth_lots_by_contract.items():
        if depth_lots < quantity_lots:
            return _paper_blocked(
                "INSUFFICIENT_DEPTH",
                data_dir,
                f"book for {contract} cannot fill common quantity {quantity_lots}",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
    for contract in sorted(actions_by_contract):
        action = actions_by_contract[contract]
        if not _paper_consumed_prices_on_tick(
            books[token_by_contract[contract]], action, quantity_lots
        ):
            return _paper_blocked(
                "OFF_TICK_PRICE",
                data_dir,
                f"consumed ask price for {contract} is not aligned to its tick",
                component_id=component_id,
                evaluated_at=evaluated_at,
            )
    prepared_actions = tuple(
        replace(
            action,
            min_quantity_lots=quantity_lots,
            max_quantity_lots=quantity_lots,
        )
        for action in problem.actions
    )
    prepared_problem = replace(problem, actions=prepared_actions)
    snapshot = ComponentSnapshot(
        component_id,
        tuple(
            SnapshotLeg(
                action.action_id,
                books[token_by_contract[action.market_contract_id]],
                books[token_by_contract[action.market_contract_id]].confirmed_at,
                books[token_by_contract[action.market_contract_id]].confirmed_at,
                None,
            )
            for action in prepared_actions
        ),
    )
    try:
        request = build_solve_request(
            prepared_problem,
            snapshot,
            budget=budget,
            limits=VALIDATION_LIMITS,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        priced_problem = request.request.problem
        quantities = tuple(
            ActionQuantity(action.action_id, quantity_lots)
            for action in priced_problem.actions
        )
        evaluation = evaluate_paper_portfolio(priced_problem, quantities, budget)
    except (TypeError, ValueError, OverflowError) as exc:
        return _paper_blocked(
            "UNKNOWN_MODEL_FACTS",
            data_dir,
            f"paper model could not be evaluated: {exc}",
            component_id=component_id,
            evaluated_at=evaluated_at,
        )
    total_cost = evaluation.cost_upper_bound_units
    net = evaluation.guaranteed_profit_units
    fee_rates = {
        str(report.get("rate"))
        for report in fee_reports.values()
        if report.get("status") == "CHARGING"
    }
    charging = any(report.get("status") == "CHARGING" for report in fee_reports.values())
    fee_report: dict[str, object] = {
        "status": "CHARGING" if charging else "FREE",
        "rate": next(iter(fee_rates)) if len(fee_rates) == 1 else "0" if not charging else None,
        "legs": fee_reports,
    }
    legs = []
    for action in priced_problem.actions:
        leg_cost = _paper_action_cost(action, quantity_lots)
        legs.append(
            {
                "action_id": action.action_id,
                "contract_id": action.market_contract_id,
                "token_id": token_by_contract[action.market_contract_id],
                "side": action.side.value,
                "quantity_lots": quantity_lots,
                "cost_upper_bound_units": leg_cost,
                "cost_upper_bound": _paper_usd(leg_cost),
                "price_upper_bound": format(
                    _paper_price_bound(
                        books[token_by_contract[action.market_contract_id]],
                        action,
                        quantity_lots,
                    ),
                    "f",
                ),
                "fee": fee_reports[action.market_contract_id],
                "minimum_order_size": order_rules_by_contract[
                    action.market_contract_id
                ]["minimum_order_size"],
                "minimum_order_notional": (
                    format(
                        minimum_notional_by_contract[action.market_contract_id],
                        "f",
                    )
                    if minimum_notional_by_contract[action.market_contract_id]
                    is not None
                    else None
                ),
                "tick_size": order_rules_by_contract[action.market_contract_id][
                    "tick_size"
                ],
                "confirmed_at": books[
                    token_by_contract[action.market_contract_id]
                ].confirmed_at.isoformat(),
                "execution_style": "FOK_MARKETABLE",
                "settlement_holding": "HOLD_TO_SETTLEMENT",
            }
        )
    return {
        "schema_version": PAPER_REPORT_SCHEMA_V1,
        "mode": "PAPER_THREE_WAY",
        "status": "PASS",
        "reason": None,
        "component_id": component_id,
        "template": PAPER_THREE_WAY_TEMPLATE,
        "group_id": model.get("group_id"),
        "evaluated_at": evaluated_at.isoformat(),
        "capital_release_at": (
            evaluation.conservative_capital_release_at.isoformat()
            if evaluation.conservative_capital_release_at is not None
            else None
        ),
        "qualification_status": evaluation.time_qualification,
        "quantity_domain": [0, quantity_lots],
        "legs": legs,
        "economics": {
            "quantity_lots": quantity_lots,
            "payout_lower_bound_units": evaluation.payout_lower_bound_units,
            "cost_upper_bound_units": total_cost,
            "guaranteed_profit_units": net,
            "payout_lower_bound": _paper_usd(evaluation.payout_lower_bound_units),
            "cost_upper_bound": _paper_usd(total_cost),
            "guaranteed_profit": _paper_usd(net),
            "economic_decision": "PROFITABLE" if net > 0 else "REJECTED",
        },
        "fees": fee_report,
        "order_rules": {
            "status": (
                "KNOWN"
                if all(value is not None for value in minimum_notional_by_contract.values())
                else "UNKNOWN_MINIMUM_NOTIONAL"
            ),
            "legs": order_rules_by_contract,
        },
        "worst_case_joint_state": canonical_payload(evaluation.worst_scenario),
        "order_ready": False,
        "execution_decision": None,
        "zero_side_effects": {
            "submitted_orders": 0,
            "mutation_attempts": 0,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
        "fingerprints": {
            "catalog_generation": (
                catalog.get("generation") if isinstance(catalog, Mapping) else None
            ),
            "catalog_rows": fingerprint({"rows": catalog_rows}),
            "problem": fingerprint(priced_problem),
            "books": economic_fingerprint(snapshot),
        },
    }


def run_paper_three_way(
    catalog_rows: object,
    *,
    book_source: Callable[[tuple[str, ...]], Mapping[str, object]] | None,
    data_dir: str | Path,
    budget: OracleBudget = VALIDATION_BUDGET,
    catalog: Mapping[str, object] | None = None,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Apply the CLI paper-mode isolation guard around the shared core."""

    data_dir = Path(data_dir)
    evaluated_at = as_of
    if evaluated_at is not None:
        if (
            not isinstance(evaluated_at, datetime)
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() != UTC.utcoffset(evaluated_at)
        ):
            return _paper_blocked(
                "INVALID_AS_OF",
                data_dir,
                "as_of must be a UTC-aware datetime",
            )
        evaluated_at = evaluated_at.astimezone(UTC)
    if (data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3").exists():
        return _paper_blocked(
            "NON_ISOLATED_DATA_DIR",
            data_dir,
            "refusing to use an existing prediction_arbitrage.sqlite3",
            evaluated_at=evaluated_at,
        )
    return _price_three_way_core(
        catalog_rows,
        book_source=book_source,
        data_dir=data_dir,
        budget=budget,
        catalog=catalog,
        as_of=as_of,
    )


class _ReadonlyCatalogAdapter:
    """Read-only v2 catalog seam for RuntimeRelationGraph/PredictionLiveResolver."""

    def __init__(
        self,
        rows: Mapping[str, Mapping[str, object]],
        generation: object,
    ) -> None:
        self._rows = {identity: dict(row) for identity, row in rows.items()}
        self._generation = int(generation) if generation is not None else 0

    def current_generation(self) -> dict[str, object]:
        return {identity: dict(row) for identity, row in self._rows.items()}

    def generation_meta(self) -> dict[str, object]:
        return {
            "generation": self._generation,
            "fingerprint": fingerprint({"generation": self._generation}),
        }


class _MonitorAdapter:
    """Adapt the injected read-only book seam to the live resolver monitor API."""

    def __init__(
        self, book_source: Callable[[tuple[str, ...]], Mapping[str, object]]
    ) -> None:
        self._book_source = book_source

    def cross_venue_books(self, token_ids: tuple[str, ...]) -> Mapping[str, object]:
        return self._book_source(token_ids)

    def cross_venue_book_meta(self, token_id: str) -> dict[str, object]:
        # ponytail: no venue timing/sequence seam; the resolver only uses these
        # for order_ready, which stays False under the frozen no-submit decision.
        return {"exchange_time": None, "sequence": None}


def _seed_selected_component(
    problem: object, component: object
) -> SelectedComponent:
    sub = problem_for_component(problem, component)
    return SelectedComponent(
        component_id=component.component_id,
        contract_ids=component.contract_ids,
        constraint_ids=component.constraint_ids,
        action_ids=component.action_ids,
        # ponytail: empty portfolio and zero score claim no approval; the live
        # resolver computes the real verified portfolio from the injected solver.
        admission_score=0,
        portfolio=(),
        relation_fingerprint=fingerprint({"constraint_model": sub.constraint_model}),
        terminal_fingerprint=fingerprint(
            {"terminal_state_sets": sub.terminal_state_sets}
        ),
        portfolio_fingerprint=fingerprint({"quantities": ()}),
        status="ACTIVE",
    )


def _positive_market_legs(market: MarketSolution) -> int:
    return sum(1 for quantity in market.quantities if quantity.quantity_lots > 0)


def _live_execution_decision(
    market: MarketSolution,
    execution_solution: ExecutionSolution,
    proof_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    status = str(
        execution_solution.partial_fill_proof
        if execution_solution is not None
        else PARTIAL_FILL_UNKNOWN
    )
    execution_reason = (
        str(execution_solution.reason or "")
        if execution_solution is not None
        else ""
    )
    # Mirror the read model chain: a non-EXECUTABLE solution reports its own
    # reason (the proof is only meaningful for a fixed, executable solution),
    # then the #74 three-state proof gates everything else.
    if execution_reason not in (EXECUTABLE_REASON, ""):
        reason = execution_reason
    elif status == PARTIAL_FILL_UNSAFE:
        reason = PARTIAL_FILL_UNSAFE
    elif status != PARTIAL_FILL_SAFE:
        reason = PARTIAL_FILL_PROOF_REQUIRED
    else:
        reason = SCOPE_OBSERVE_ONLY
    proof = dict(proof_payload) if isinstance(proof_payload, Mapping) else {}
    return {
        "order_ready": False,
        "reason": reason,
        "partial_fill_proof": status,
        "proof": {
            "status": status,
            "solver_termination": proof.get("solver_termination"),
            "verifier_status": proof.get("verifier_status"),
            "lower_bound_units": proof.get("solver_lower_bound"),
            "upper_bound_units": proof.get("solver_upper_bound"),
            "cap_units": proof.get("max_partial_fill_loss"),
            "fingerprint": proof.get("fingerprint"),
        },
        "quantities": [
            {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
            for q in market.quantities
        ],
        "capital_use_units": (
            execution_solution.capital_use_units
            if execution_solution is not None
            else 0
        ),
        "market_solution_fingerprint": (
            execution_solution.market_solution_fingerprint
            if execution_solution is not None
            else None
        ),
    }


def _live_resolver_pass(
    component_id: str,
    market: MarketSolution,
    execution_solution: ExecutionSolution,
    catalog_rows: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, object] | None,
    execution: FailClosedExecution,
    data_dir: Path,
    started: float,
    proof_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": "PASS",
        "component_id": component_id,
        "legs": _positive_market_legs(market),
        "qualified_verified": True,
        "guaranteed_profit_units": market.guaranteed_profit_units,
        "execution_decision": _live_execution_decision(
            market, execution_solution, proof_payload
        ),
        "fingerprints": {
            "quote": market.quote_fingerprint,
            "structure": market.structure_fingerprint,
            "portfolio": fingerprint({"quantities": market.quantities}),
            "qualification": market.verification_fingerprint,
            "catalog_generation": (
                catalog.get("generation") if isinstance(catalog, dict) else None
            ),
            "catalog_rows": fingerprint({"rows": catalog_rows}),
        },
        "zero_side_effects": {
            "submitted_orders": execution.submit_attempts,
            "mutation_attempts": execution.mutation_attempts,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
        "timings": {
            "end_to_end_seconds": round(time.perf_counter() - started, 6),
        },
    }


def _live_negative_pass(
    component_id: str,
    catalog_rows: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, object] | None,
    execution: FailClosedExecution,
    data_dir: Path,
    started: float,
    verification: VerificationResult | None,
) -> dict[str, object]:
    proof = verification.negative_proof if verification is not None else None
    return {
        "status": "PASS",
        "component_id": component_id,
        "legs": 0,
        "qualified_verified": False,
        "guaranteed_profit_units": None,
        "execution_decision": None,
        "fingerprints": {
            "catalog_generation": (
                catalog.get("generation") if isinstance(catalog, dict) else None
            ),
            "catalog_rows": fingerprint({"rows": catalog_rows}),
            "negative_proof": (
                fingerprint(canonical_payload(proof)) if proof is not None else None
            ),
            "qualification": (
                proof.qualification_fingerprint if proof is not None else None
            ),
        },
        "zero_side_effects": {
            "submitted_orders": execution.submit_attempts,
            "mutation_attempts": execution.mutation_attempts,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
        "timings": {
            "end_to_end_seconds": round(time.perf_counter() - started, 6),
        },
    }


def run_live(
    catalog_rows: Mapping[str, Mapping[str, object]],
    *,
    book_source: Callable[[tuple[str, ...]], Mapping[str, object]] | None,
    data_dir: str | Path,
    budget: OracleBudget = VALIDATION_BUDGET,
    limits: BenchmarkLimits = VALIDATION_LIMITS,
    code_version: str = "issue-71",
    execution: FailClosedExecution | None = None,
    catalog: Mapping[str, object] | None = None,
    solver_server: object | None = None,
    poll_timeout_seconds: float = 15.0,
    # Issue #114: optional contract -> {"yes_token_id", "no_token_id"} for
    # legacy catalog rows; rows that already carry tokens win per contract.
    leg_token_map: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    data_dir = Path(data_dir)
    execution = execution or FailClosedExecution()
    if (data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3").exists():
        return _live_blocked(
            "NON_ISOLATED_DATA_DIR",
            data_dir,
            execution,
            "refusing to write validation stores into an existing prediction_arbitrage.sqlite3",
        )
    started = time.perf_counter()
    try:
        problem, components = relation_generation_problem(catalog_rows)
    except (TypeError, ValueError) as exc:
        return _live_blocked(
            "NO_ACTIVE_RELATION_WITH_COMPILED_MODEL", data_dir, execution, str(exc)
        )
    n3_components = [
        item for item in (components or ()) if len(item.action_ids) >= MIN_LEGS
    ]
    if problem is None or not n3_components:
        return _live_blocked(
            "NO_ACTIVE_N3_RELATION",
            data_dir,
            execution,
            "no approved ACTIVE same-venue N>=3 relation in the read-only catalog (tracked as #88)",
        )
    component = n3_components[0]
    if book_source is None:
        return _live_blocked(
            "LIVE_BOOK_SOURCE_UNAVAILABLE", data_dir, execution, "no read-only book seam configured"
        )
    monitor = _MonitorAdapter(book_source)
    raw = problem_for_component(problem, component)
    # Issue #114: resolve each action's book read to its direction's CLOB
    # token; generation rows win over the injected map, unmapped contracts
    # fall back to their market_contract_id (mechanical contract-is-token).
    merged_leg_tokens: dict[str, dict[str, object]] = {
        **{
            str(contract): dict(entry)
            for contract, entry in (leg_token_map or {}).items()
            if isinstance(entry, Mapping)
        },
        **_leg_token_by_contract(catalog_rows),
    }
    token_ids = tuple(
        resolve_leg_token(action, merged_leg_tokens) for action in raw.actions
    )
    try:
        books = monitor.cross_venue_books(token_ids)
    except (TypeError, ValueError, OverflowError) as exc:
        return _live_blocked("MISSING_BOOKS", data_dir, execution, f"book fetch failed: {exc}")
    if any(books.get(token) is None for token in token_ids):
        return _live_blocked("MISSING_BOOKS", data_dir, execution, "one or more books missing")

    owned_solver_server = solver_server is None
    resolver: PredictionLiveResolver | None = None
    resolution = None
    lock_path = data_dir / "prediction_arbitrage" / ".nleg-validation.lock"
    lock = _OwnershipLock(lock_path)
    try:
        lock.__enter__()
    except RuntimeError as exc:
        return _live_blocked(
            "VALIDATION_LOCK_UNAVAILABLE", data_dir, execution, str(exc)
        )
    try:
        if owned_solver_server:
            solver_server = SolverServerOwner(
                [
                    sys.executable,
                    "-m",
                    "open_trader.prediction_solver_worker",
                    "--backend",
                    "cp_sat",
                ]
            )
        adapter = _ReadonlyCatalogAdapter(
            catalog_rows,
            catalog.get("generation") if isinstance(catalog, dict) else 0,
        )
        store = PredictionArbitrageStore(data_dir)
        selection_store = MonitorSelectionStore(data_dir)
        selection_store.save(
            {
                component.component_id: _seed_selected_component(
                    problem, component
                )
            }
        )
        resolver = PredictionLiveResolver(
            data_dir=data_dir,
            relation_catalog=adapter,
            monitor=monitor,
            solver_server=solver_server,
            selection_store=selection_store,
            store=store,
            execution=execution,
            budget=budget,
            limits=limits,
            code_version=code_version,
            leg_token_map=merged_leg_tokens,
        )
        resolver.start()
        deadline = time.monotonic() + poll_timeout_seconds
        while time.monotonic() < deadline:
            candidate = resolver.latest_resolution(component.component_id)
            if candidate is None:
                time.sleep(0.01)
                continue
            resolution = candidate
            if (
                resolution.status == VerificationStatus.QUALIFIED_VERIFIED
                and resolver.latest_execution(component.component_id) is None
            ):
                # The execution solution (with its #74 proof) is produced in
                # the same tick right after the resolution; wait for it.
                time.sleep(0.01)
                continue
            break
    except RuntimeError as exc:
        return _live_blocked(
            "VALIDATION_RUNTIME_ERROR", data_dir, execution, str(exc), status="FAIL"
        )
    finally:
        if resolver is not None:
            resolver.stop()
        if owned_solver_server and solver_server is not None:
            solver_server.close()
        lock.__exit__(None, None, None)

    if execution.submit_attempts != 0 or execution.mutation_attempts != 0:
        raise RuntimeError(
            "no-submit validation violated: submission/mutation reached "
            f"(submit_attempts={execution.submit_attempts}, "
            f"mutation_attempts={execution.mutation_attempts})"
        )
    if resolution is None or resolution.status == VerificationStatus.UNKNOWN:
        return _live_blocked(
            "NO_QUALIFIED_SOLUTION",
            data_dir,
            execution,
            "resolver produced no qualified N>=3 MarketSolution within timeout",
            status="FAIL",
        )
    if resolution.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
        verification = resolver.latest_verification(component.component_id)
        return _live_negative_pass(
            component.component_id,
            catalog_rows,
            catalog,
            execution,
            data_dir,
            started,
            verification,
        )
    market = resolution.market_solution
    if market is None:
        return _live_blocked(
            "NO_QUALIFIED_SOLUTION",
            data_dir,
            execution,
            "resolver produced no qualified N>=3 MarketSolution within timeout",
            status="FAIL",
        )
    if _positive_market_legs(market) < MIN_LEGS:
        return _live_blocked(
            "N_LESS_THAN_3",
            data_dir,
            execution,
            f"solver selected {_positive_market_legs(market)} positive legs",
            status="FAIL",
        )
    execution_solution = resolver.latest_execution(component.component_id)
    proof_payload = resolver.latest_partial_fill_proof(component.component_id)
    if execution_solution is None:
        return _live_blocked(
            "NO_QUALIFIED_SOLUTION",
            data_dir,
            execution,
            "resolver produced no execution solution for the qualified market",
            status="FAIL",
        )
    return _live_resolver_pass(
        component.component_id,
        market,
        execution_solution,
        catalog_rows,
        catalog,
        execution,
        data_dir,
        started,
        proof_payload,
    )


def _live_blocked(
    reason: str,
    data_dir: Path,
    execution: FailClosedExecution,
    detail: str,
    *,
    status: str = "BLOCKED",
) -> dict[str, object]:
    return {
        "status": status,
        "reason": reason,
        "detail": detail,
        "zero_side_effects": {
            "submitted_orders": execution.submit_attempts,
            "mutation_attempts": execution.mutation_attempts,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
    }


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def build_report(
    *,
    replay: dict[str, object] | None,
    live: dict[str, object] | None,
    data_dir: str | Path,
) -> dict[str, object]:
    statuses = [
        section.get("status")
        for section in (replay, live)
        if isinstance(section, dict) and section.get("status") in ("PASS", "FAIL", "BLOCKED")
    ]
    if len(statuses) < 2:
        overall = "BLOCKED"
        reason = "BOTH_SECTIONS_REQUIRED"
    elif "FAIL" in statuses:
        overall, reason = "FAIL", "FAIL"
    elif "BLOCKED" in statuses:
        overall, reason = "BLOCKED", "BLOCKED"
    else:
        overall, reason = "PASS", None
    report = {
        "schema_version": REPORT_SCHEMA_V1,
        "status": overall,
        "reason": reason,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "git_sha": _git_sha(),
        "captured_at": datetime.now(UTC).isoformat(),
        "data_dir": str(Path(data_dir)),
        "replay": replay,
        "live": live,
    }
    if isinstance(live, dict) and isinstance(live.get("zero_side_effects"), dict):
        report["zero_side_effects"] = live["zero_side_effects"]
    return report


def _book_source_from_flag(value: str | None) -> Callable[[tuple[str, ...]], Mapping[str, object]] | None:
    if not value:
        return None
    module_name, _, attr = value.partition(":")
    if not module_name or not attr:
        raise ValueError("--book-source must be MODULE:ATTR")
    module = importlib.import_module(module_name)
    target = getattr(module, attr, None)
    if not callable(target):
        raise ValueError(f"--book-source target is not callable: {value}")
    return target


def _paper_as_of_from_flag(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--paper-as-of must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("--paper-as-of must include a UTC offset")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader prediction-arb nleg-validate")
    parser.add_argument("--replay", type=Path, help="Frozen N>=3 validation snapshot (JSON)")
    parser.add_argument(
        "--paper-three-way",
        type=Path,
        help="Catalog rows JSON for the read-only three-way paper path",
    )
    parser.add_argument(
        "--paper-as-of",
        help="UTC evaluation timestamp for paper replay (default: current UTC time)",
    )
    parser.add_argument(
        "--live-catalog",
        type=Path,
        default=Path("data/prediction_arbitrage/prediction_arbitrage.sqlite3"),
        help="Read-only v2 relation catalog SQLite path",
    )
    parser.add_argument(
        "--book-source",
        default="",
        help=(
            "MODULE:ATTR callable returning current books for token ids; paper mode "
            "can use open_trader.prediction_n_leg_validation_books:paper_live_books"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(tempfile.mkdtemp(prefix="nleg-validate-")),
        help="Isolated validation data dir (default: fresh temp dir)",
    )
    parser.add_argument("--report", type=Path, help="Write the JSON report to this path")
    parser.add_argument(
        "--live-max-joint-states",
        type=int,
        default=None,
        help=(
            "Live-path joint-state budget cap (default: keep the fixed "
            "validation budget value); replay is unaffected"
        ),
    )
    parser.add_argument(
        "--live-max-quantity-vectors",
        type=int,
        default=None,
        help=(
            "Live-path quantity-vector budget cap (default: keep the fixed "
            "validation budget value); replay is unaffected"
        ),
    )
    args = parser.parse_args(argv)
    for flag_name, value in (
        ("--live-max-joint-states", args.live_max_joint_states),
        ("--live-max-quantity-vectors", args.live_max_quantity_vectors),
    ):
        if value is not None and value < 1:
            parser.error(f"{flag_name} must be >= 1 (got {value})")
    if args.paper_three_way is not None:
        try:
            rows = json.loads(args.paper_three_way.read_text(encoding="utf-8"))
            report = run_paper_three_way(
                rows,
                book_source=_book_source_from_flag(args.book_source),
                data_dir=args.data_dir,
                as_of=_paper_as_of_from_flag(args.paper_as_of),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            report = _paper_blocked(
                "PAPER_INPUT_UNAVAILABLE",
                args.data_dir,
                str(exc),
            )
        text = json.dumps(report, indent=2, sort_keys=True)
        if args.report is not None:
            Path(args.report).write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2
    replay = None
    if args.replay is not None:
        try:
            replay = run_replay(frozen_snapshot_from_file(args.replay))
        except (OSError, ValueError) as exc:
            replay = {"status": "BLOCKED", "reason": "REPLAY_UNAVAILABLE", "detail": str(exc)}
    live = None
    try:
        catalog = readonly_v2_relations(args.live_catalog)
        live = run_live(
            catalog["rows"],
            book_source=_book_source_from_flag(args.book_source),
            data_dir=args.data_dir,
            budget=live_budget_from_flags(
                args.live_max_joint_states, args.live_max_quantity_vectors
            ),
            catalog=catalog,
        )
    except (OSError, ValueError) as exc:
        live = {"status": "BLOCKED", "reason": "LIVE_CATALOG_UNAVAILABLE", "detail": str(exc)}
    report = build_report(replay=replay, live=live, data_dir=args.data_dir)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report is not None:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
