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
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_live_resolver import (
    PredictionLiveResolver,
    USD_UNITS_PER_DOLLAR,
    normalize_problem,
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
    OracleBudget,
    OracleRequest,
    SearchMode,
    canonical_payload,
    fingerprint,
    problem_from_payload,
)
from open_trader.prediction_n_leg_mode import DEFAULT_SAFETY_CONFIG
from open_trader.prediction_n_leg_oracle import evaluate_fixed_portfolio
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
    token_ids = tuple(action.market_contract_id for action in raw.actions)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader prediction-arb nleg-validate")
    parser.add_argument("--replay", type=Path, help="Frozen N>=3 validation snapshot (JSON)")
    parser.add_argument(
        "--live-catalog",
        type=Path,
        default=Path("data/prediction_arbitrage/prediction_arbitrage.sqlite3"),
        help="Read-only v2 relation catalog SQLite path",
    )
    parser.add_argument(
        "--book-source",
        default="",
        help="MODULE:ATTR callable returning current books for token ids (live path)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(tempfile.mkdtemp(prefix="nleg-validate-")),
        help="Isolated validation data dir (default: fresh temp dir)",
    )
    parser.add_argument("--report", type=Path, help="Write the JSON report to this path")
    args = parser.parse_args(argv)
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
