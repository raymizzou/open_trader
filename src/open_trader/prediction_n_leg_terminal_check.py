"""Issue #60 slice 4: A8 terminal-state modeling verification (read-only).

The frozen production fixture ``open_trader.prediction_n_leg_cutover.a8_samples.v1``
pairs, per sample, the legacy LLM proof (``structured_result`` with
``relation`` and ``proof.excluded_state``) with the N_LEG side (``catalog``
embedding the canonical compiled ``problem``).  This tool independently
re-derives each proof's excluded joint YES/NO state from the canonical N_LEG
constraint model — the same evaluation the #52 compile seam uses
(``enumerate_allowed_scenarios``) — and reports any sample where the legacy
proof and the N_LEG model disagree.

Read-only: it only reads the fixture and returns a JSON-serializable report.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from open_trader.prediction_n_leg import (
    OracleBudget,
    RelationKind,
    TerminalKind,
    problem_from_payload,
)
from open_trader.prediction_n_leg_oracle import enumerate_allowed_scenarios


CHECK_SCHEMA_V1 = "open_trader.prediction_n_leg_terminal_check.report.v1"

#: The A8 fixture enumerates joint states of exactly two markets; 3 terminal
#: kinds per market yield 9 raw joint states, well under this budget ceiling.
_ENUMERATION_BUDGET = OracleBudget(
    max_quantity_vectors=1,
    max_joint_states=1024,
    max_support_rechecks=1,
)

_NORMAL_KINDS = (TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO)


def _kind_label(kind: TerminalKind) -> str:
    """``NORMAL_YES`` -> ``YES`` (the legacy proof label vocabulary)."""
    value = kind.value if isinstance(kind.value, str) else str(kind.value)
    return value.removeprefix("NORMAL_")


def _excluded_joint_state_labels(
    problem: object,
    condition_ids: Mapping[str, str],
) -> list[str]:
    """Derive the excluded joint YES/NO state labels from the N_LEG model.

    Enumerates the four joint YES/NO states of the two markets and evaluates
    each against the canonical constraint model exactly the way the compile
    seam's scenario enumeration does (relations plus forbidden atom
    combinations over the full terminal model).  Labels are written in the
    legacy proof vocabulary, oriented through the ``market_a``/``market_b``
    condition ids: ``A=NO,B=YES``.
    """
    states_by_contract = {
        state.market_contract_id: state for state in problem.terminal_state_sets
    }
    kinds_by_atom = {
        atom.atom_id: atom.kind
        for state in states_by_contract.values()
        for atom in state.atoms
    }
    enumeration = enumerate_allowed_scenarios(problem, _ENUMERATION_BUDGET)
    if enumeration.unknown_reason is not None:
        raise ValueError(
            f"scenario enumeration failed: {enumeration.unknown_reason.value}"
        )
    allowed = {
        tuple(
            sorted(
                (selected.market_contract_id, kinds_by_atom[selected.atom_id])
                for selected in scenario.atoms
            )
        )
        for scenario in enumeration.scenarios
    }
    labels = []
    market_a, market_b = condition_ids["A"], condition_ids["B"]
    for kind_a in _NORMAL_KINDS:
        for kind_b in _NORMAL_KINDS:
            joint = (
                (market_a, kind_a),
                (market_b, kind_b),
            )
            if tuple(sorted(joint)) in allowed:
                continue
            labels.append(f"A={_kind_label(kind_a)},B={_kind_label(kind_b)}")
    return sorted(labels)


def _check_sample(
    sample: Mapping[str, object],
) -> tuple[str, str, bool, list[str]]:
    """Return ``(sample_id, proof_label, agreed, derived_excluded_labels)``."""
    cache_key = sample.get("cache_key")
    if not isinstance(cache_key, str) or not cache_key:
        raise ValueError("sample is missing a string cache_key")
    structured = sample.get("structured_result")
    if not isinstance(structured, Mapping):
        raise ValueError("sample is missing the structured_result mapping")
    proof = structured.get("proof")
    if not isinstance(proof, Mapping):
        raise ValueError("sample is missing the proof mapping")
    excluded_state = proof.get("excluded_state")
    if not isinstance(excluded_state, str) or not excluded_state:
        raise ValueError("proof is missing the excluded_state label")
    market_a = structured.get("market_a")
    market_b = structured.get("market_b")
    if not isinstance(market_a, Mapping) or not isinstance(market_b, Mapping):
        raise ValueError("structured_result is missing market_a/market_b")
    condition_a = market_a.get("condition_id")
    condition_b = market_b.get("condition_id")
    if not isinstance(condition_a, str) or not isinstance(condition_b, str):
        raise ValueError("market_a/market_b are missing condition_id values")
    if condition_a == condition_b:
        raise ValueError("market_a and market_b share one condition_id")
    catalog = sample.get("catalog")
    if not isinstance(catalog, Mapping):
        raise ValueError("sample is missing the catalog mapping")
    identity = catalog.get("identity")
    if not isinstance(identity, str) or not identity:
        raise ValueError("catalog is missing the identity string")
    problem_payload = catalog.get("problem")
    if not isinstance(problem_payload, Mapping):
        raise ValueError("catalog is missing the compiled problem payload")

    condition_ids = {"A": condition_a, "B": condition_b}
    _require_consistent_orientation(identity, problem_payload, condition_ids)
    problem = problem_from_payload(problem_payload)
    derived = _excluded_joint_state_labels(problem, condition_ids)
    agreed = derived == [excluded_state]
    return cache_key, excluded_state, agreed, derived


def _require_consistent_orientation(
    identity: str,
    problem_payload: Mapping[str, object],
    condition_ids: Mapping[str, str],
) -> None:
    """Fail unless the identity parts and relation span exactly the A/B markets.

    The identity reads ``IMPLIES|<venue:contract>|<venue:contract>`` and its
    endpoint order follows the discovery payload, not the implication
    direction; the direction lives in the compiled relation constraint's
    ``contract_ids`` (antecedent first), which the scenario enumeration
    evaluates.  Both must reference exactly the two ``market_a``/``market_b``
    condition ids — anything else is an un-mappable orientation for this A8
    implies fixture.
    """
    parts = identity.split("|")
    if len(parts) != 3 or parts[0] != "IMPLIES":
        raise ValueError(f"identity is not an IMPLIES pair: {identity!r}")
    identity_contracts = set()
    for part in parts[1:]:
        venue, _, contract_id = part.partition(":")
        if not venue or not contract_id or contract_id not in condition_ids.values():
            raise ValueError(
                f"identity endpoint {part!r} does not map to market_a/market_b"
            )
        identity_contracts.add(contract_id)
    if identity_contracts != set(condition_ids.values()):
        raise ValueError("identity endpoints do not span both market_a and market_b")
    constraint_model = problem_payload.get("constraint_model")
    if not isinstance(constraint_model, Mapping):
        raise ValueError("problem is missing the constraint_model mapping")
    relations = constraint_model.get("relations")
    if not isinstance(relations, list) or len(relations) != 1:
        raise ValueError("problem must carry exactly one relation constraint")
    relation = relations[0]
    if not isinstance(relation, Mapping) or relation.get("kind") != (
        RelationKind.IMPLIES.value
    ):
        raise ValueError("relation constraint is not IMPLIES")
    raw_relation_contracts = relation.get("contract_ids")
    if not isinstance(raw_relation_contracts, list) or not all(
        isinstance(contract_id, str) for contract_id in raw_relation_contracts
    ):
        raise ValueError(
            "relation constraint contract_ids must be a list of strings"
        )
    relation_contracts = set(raw_relation_contracts)
    if relation_contracts != identity_contracts:
        raise ValueError(
            "relation constraint does not span the identity's two condition ids"
        )


def run_terminal_check(fixture_path: str | Path) -> dict[str, object]:
    """Run the terminal-state agreement check over one frozen A8 fixture."""
    path = Path(fixture_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema") != (
        "open_trader.prediction_n_leg_cutover.a8_samples.v1"
    ):
        raise ValueError(
            "fixture schema must be "
            "open_trader.prediction_n_leg_cutover.a8_samples.v1"
        )
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError("fixture is missing the samples list")
    agreed = 0
    disagreed = []
    errors = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            errors.append(
                {
                    "sample_id": None,
                    "reason": "MALFORMED_SAMPLE",
                    "detail": f"sample #{index} is not an object",
                }
            )
            continue
        sample_id = sample.get("cache_key")
        sample_id = sample_id if isinstance(sample_id, str) else None
        try:
            sample_id, proof_label, sample_agreed, derived = _check_sample(sample)
        except ValueError as exc:
            errors.append(
                {
                    "sample_id": sample_id,
                    "reason": "SAMPLE_CHECK_FAILED",
                    "detail": str(exc),
                }
            )
            continue
        if sample_agreed:
            agreed += 1
        else:
            disagreed.append(
                {
                    "sample_id": sample_id,
                    "expected_excluded_state": proof_label,
                    "derived_excluded_states": derived,
                }
            )
    return {
        "schema": CHECK_SCHEMA_V1,
        "sample_count": len(samples),
        "agreed": agreed,
        "disagreed": disagreed,
        "errors": errors,
        "source": (
            f"fixture={path}; fixture_source={payload.get('source', '')}"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry: exit 0 when every sample agrees, 2 on any disagreement/error."""
    parser = argparse.ArgumentParser(
        prog="open-trader prediction-arb nleg-terminal-check",
        description=(
            "Verify the frozen A8 terminal-state fixture against the canonical "
            "N_LEG constraint model (read-only)."
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="Frozen a8_samples fixture (JSON)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write the JSON report to this path (default: fresh temp dir)",
    )
    args = parser.parse_args(argv)
    if args.report is not None:
        report_path = args.report
    else:
        report_path = (
            Path(tempfile.mkdtemp(prefix="nleg-terminal-check-")) / "report.json"
        )
    try:
        report = run_terminal_check(args.fixture)
    except (OSError, ValueError) as exc:
        report = {
            "schema": CHECK_SCHEMA_V1,
            "sample_count": 0,
            "agreed": 0,
            "disagreed": [],
            "errors": [
                {
                    "sample_id": None,
                    "reason": "FIXTURE_UNAVAILABLE",
                    "detail": str(exc),
                }
            ],
            "source": f"fixture={args.fixture}",
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failed = bool(report["disagreed"]) or bool(report["errors"])
    print(f"sample_count: {report['sample_count']}")
    print(f"agreed: {report['agreed']}")
    print(f"disagreed: {len(report['disagreed'])}")
    print(f"errors: {len(report['errors'])}")
    print(f"result: {'FAIL' if failed else 'PASS'}")
    print(f"report: {report_path}")
    return 2 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
