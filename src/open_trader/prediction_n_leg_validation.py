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

The preflight decision is intentionally frozen at ``order_ready=false``,
``partial_fill_proof=UNKNOWN``, ``reason=PARTIAL_FILL_PROOF_REQUIRED`` until
#74 ships a partial-fill proof.  A fail-closed execution seam raises on any
submit/mutation call and every report asserts zero side effects.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_live_resolver import USD_UNITS_PER_DOLLAR, normalize_problem
from open_trader.prediction_market_solution import (
    AccountView,
    ExecutionSolution,
    MarketSolution,
    build_solve_request,
    execution_solution_from_market,
    market_solution_from_verification,
    negative_proof_matches,
)
from open_trader.prediction_monitor_selection import (
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
from open_trader.prediction_n_leg_oracle import evaluate_fixed_portfolio
from open_trader.prediction_n_leg_read_model import PARTIAL_FILL_PROOF_REQUIRED
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
)
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    ProofInput,
    VerificationStatus,
    candidate_evidence_from_payload,
    model_fingerprint,
    quote_fingerprint,
    solve,
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
        legs.append(
            SnapshotLeg(
                action.action_id,
                LegBook((), converted, None, True),
                problem.as_of,
                problem.as_of,
                None,
            )
        )
    return ComponentSnapshot(component_id, tuple(legs))


def _snapshot_from_live_books(
    component_id: str,
    problem: object,
    books: Mapping[str, object],
) -> ComponentSnapshot | None:
    legs: list[SnapshotLeg] = []
    for action in problem.actions:
        book = books.get(action.market_contract_id) or books.get(action.action_id)
        if book is None or not hasattr(book, "asks") or not hasattr(book, "bids"):
            return None
        confirmed_at = getattr(book, "confirmed_at", None)
        legs.append(
            SnapshotLeg(
                action.action_id,
                LegBook(
                    bids=tuple(book.bids),
                    asks=tuple(book.asks),
                    taker_fee_bps=Decimal("0"),
                    available=True,
                ),
                confirmed_at,
                confirmed_at,
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


def _execution_decision(
    market: MarketSolution,
    problem: object,
    account: AccountView = _VALIDATION_ACCOUNT,
) -> dict[str, object]:
    execution = execution_solution_from_market(
        market,
        problem,
        account,
        max_total_unsettled_capital=account.available_units,
    )
    # ponytail: forced decision until #74 proves partial-fill safety; revisit
    # when #74 ships, do not read execution.reason here.
    return {
        "order_ready": False,
        "reason": PARTIAL_FILL_PROOF_REQUIRED,
        "partial_fill_proof": "UNKNOWN",
        "quantities": [
            {"action_id": q.action_id, "quantity_lots": q.quantity_lots}
            for q in market.quantities
        ],
        "capital_use_units": execution.capital_use_units,
        "market_solution_fingerprint": execution.market_solution_fingerprint,
    }


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
) -> dict[str, object]:
    data_dir = Path(data_dir)
    execution = execution or FailClosedExecution()
    started = time.perf_counter()
    try:
        problem, components = relation_generation_problem(catalog_rows)
    except (TypeError, ValueError) as exc:
        return _live_blocked(
            "NO_ACTIVE_RELATION_WITH_COMPILED_MODEL", data_dir, execution, str(exc)
        )
    n3_components = [item for item in (components or ()) if len(item.action_ids) >= MIN_LEGS]
    if problem is None or not n3_components:
        return _live_blocked(
            "NO_ACTIVE_N3_RELATION",
            data_dir,
            execution,
            "no approved ACTIVE same-venue N>=3 relation in the read-only catalog (tracked as #88)",
        )
    component = n3_components[0]
    raw = problem_for_component(problem, component)
    live_problem = normalize_problem(raw)
    if book_source is None:
        return _live_blocked(
            "LIVE_BOOK_SOURCE_UNAVAILABLE", data_dir, execution, "no read-only book seam configured"
        )
    token_ids = tuple(action.market_contract_id for action in live_problem.actions)
    try:
        books = book_source(token_ids)
        component_snapshot = _snapshot_from_live_books(
            component.component_id, live_problem, books
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _live_blocked(
            "MISSING_BOOKS", data_dir, execution, f"book fetch failed: {exc}"
        )
    if component_snapshot is None:
        return _live_blocked("MISSING_BOOKS", data_dir, execution, "one or more books missing")
    try:
        request = build_solve_request(
            live_problem,
            component_snapshot,
            budget=budget,
            limits=limits,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        evidence, verification, market, solve_seconds = _solve_verified(
            component.component_id,
            request.request.problem,
            budget=budget,
            limits=limits,
            code_version=code_version,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _live_blocked(
            "SOLVER_UNAVAILABLE", data_dir, execution, str(exc)
        )
    if market is None and verification.status != VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
        return _live_blocked(
            "UNKNOWN",
            data_dir,
            execution,
            f"{verification.status.value}:{verification.unknown_reason or 'NO_MARKET_SOLUTION'}",
            status="FAIL",
        )
    if verification.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
        proven = negative_proof_matches(
            evidence,
            verification,
            request.request.problem,
            code_version=code_version,
        )
        if not proven:
            return _live_blocked(
                "NEGATIVE_PROOF_MISMATCH", data_dir, execution, "negative proof does not bind",
                status="FAIL",
            )
        return _live_pass(
            component.component_id,
            0,
            False,
            None,
            None,
            request,
            evidence,
            verification,
            catalog_rows,
            catalog,
            execution,
            data_dir,
            started,
            solve_seconds,
        )
    assert market is not None
    legs = len([q for q in market.quantities if q.quantity_lots > 0])
    if legs < MIN_LEGS:
        return _live_blocked(
            "N_LESS_THAN_3", data_dir, execution, f"solver selected {legs} positive legs",
            status="FAIL",
        )
    return _live_pass(
        component.component_id,
        legs,
        True,
        market.guaranteed_profit_units,
        _execution_decision(market, request.request.problem),
        request,
        evidence,
        verification,
        catalog_rows,
        catalog,
        execution,
        data_dir,
        started,
        solve_seconds,
    )


def _live_pass(
    component_id: str,
    legs: int,
    qualified: bool,
    profit: int | None,
    decision: dict[str, object] | None,
    request: object,
    evidence: object,
    verification: object,
    catalog_rows: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, object] | None,
    execution: FailClosedExecution,
    data_dir: Path,
    started: float,
    solve_seconds: float,
) -> dict[str, object]:
    return {
        "status": "PASS",
        "component_id": component_id,
        "legs": legs,
        "qualified_verified": qualified,
        "guaranteed_profit_units": profit,
        "execution_decision": decision,
        "fingerprints": {
            "model": model_fingerprint(request.request.problem),
            "quote": quote_fingerprint(request.request.problem),
            "portfolio": (
                fingerprint({"quantities": verification.solution.quantities})
                if verification.solution is not None
                else None
            ),
            "qualification": (
                (
                    verification.solution.payout_proof.qualification_fingerprint
                    if verification.solution is not None
                    else verification.negative_proof.qualification_fingerprint
                )
                if (
                    verification.solution is not None
                    or verification.negative_proof is not None
                )
                else None
            ),
            "catalog_generation": (
                catalog.get("generation") if isinstance(catalog, dict) else None
            ),
            "catalog_rows": fingerprint({"rows": catalog_rows}),
        },
        "constraint_generation_rounds": {
            "master_rounds": evidence.solver_evidence.master_rounds,
            "adversary_rounds": evidence.solver_evidence.adversary_rounds,
        },
        "zero_side_effects": {
            "submitted_orders": execution.submit_attempts,
            "mutation_attempts": execution.mutation_attempts,
            "data_dir": str(data_dir),
            "catalog_read_only": True,
        },
        "timings": {
            "solve_seconds": round(solve_seconds, 6),
            "end_to_end_seconds": round(time.perf_counter() - started, 6),
        },
    }


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
