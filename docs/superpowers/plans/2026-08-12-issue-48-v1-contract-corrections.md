# Issue #48 Canonical v1 Contract Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the unshipped N_LEG v1 canonical contract and bounded exact Oracle so qualification cannot false-pass, every v1 identity is deterministic and fail-closed, and positive/negative outcomes share one proof record.

**Architecture:** Keep the existing two-module boundary: `prediction_n_leg.py` owns immutable wire/domain types, validation, canonical serialization, and decoding; `prediction_n_leg_oracle.py` owns exact enumeration and proof construction. Rewrite v1 in place, update the literal 16-case corpus after each wire-contract change, and add no compatibility layer, solver dependency, or runtime integration.

**Tech Stack:** Python 3.12 stdlib (`dataclasses`, `datetime`, `enum`, `hashlib`, `itertools`, `json`) and pytest.

## Global Constraints

- Python remains `>=3.12`; add no dependency.
- Supported schemas are exact v1 constants; unknown versions fail closed.
- Entry actions remain BUY YES/BUY NO only; zero means unselected.
- All money, payouts, quantities, thresholds, and derived arithmetic remain signed 64-bit integers with conservative rounding.
- Threshold values remain caller supplied; do not hard-code 1 USD, 1%, 15%, or 30 days.
- Rewrite the unshipped v1 fixture in place; do not add v2 or compatibility shims.
- Do not touch Prediction runtime, Dashboard, live books, balances, solvers, notifications, or order submission.
- Follow RED → verify RED → minimal GREEN → verify GREEN for every behavior change.
- The literal corpus must not compute expected values at test runtime.
- `make acceptance` is not applicable because no Dashboard/runtime path changes.

## File map

- Modify `src/open_trader/prediction_n_leg.py`: canonical types, schema constants, input/result validation, serialization and decoding.
- Modify `src/open_trader/prediction_n_leg_oracle.py`: quantity domain, qualification arithmetic, proof creation, and request fail-closed gates.
- Modify `tests/test_prediction_n_leg.py`: canonical contract, decoder, version, unknown-data, and result-kind tests.
- Modify `tests/test_prediction_n_leg_oracle.py`: qualification counterexamples, quantity-domain behavior, deterministic proof tests, and corpus SHA.
- Modify `tests/fixtures/prediction_n_leg_v1.json`: all 16 canonical requests, literal results, and fingerprints.
- Modify `CHANGELOG.md`: exact corrected behavior and verification evidence.

---

### Task 1: Correct qualification mathematics

**Files:**
- Modify: `src/open_trader/prediction_n_leg_oracle.py:315-347`
- Test: `tests/test_prediction_n_leg_oracle.py:552-620`

**Interfaces:**
- Consumes: `PortfolioEvaluation.payout_lower_bound_units`, `cost_upper_bound_units`, `guaranteed_profit_units`, and `conservative_capital_release_at`.
- Produces: `_occupied_days(problem: ArbitrageProblem, evaluation: PortfolioEvaluation) -> int` and corrected `_qualification_passes(...) -> bool`.

- [ ] **Step 1: Add the net-margin regression**

Add a one-action case whose cost is 60, payout is 100, guaranteed profit is 40, and net-margin threshold is 500,000 ppm:

```python
def test_net_margin_uses_minimum_payout_as_denominator() -> None:
    key = observation()
    built = problem(
        (action("a", "a", key, (ExecutableCostSlice(1, 1, 60),)),),
        (state("a", key, "a", (("yes", TerminalKind.NORMAL_YES, 100),), AS_OF + timedelta(days=1)),),
    )
    built = replace(
        built,
        qualification_constraints=(
            QualificationConstraint(
                "margin", "v1", QualificationMetric.NET_MARGIN_PPM,
                Comparison.GREATER_THAN_OR_EQUAL, 500_000, 1,
            ),
        ),
    )

    evaluation = evaluate_fixed_portfolio(
        built, (ActionQuantity("a", 1),), OracleBudget(2, 2, 2),
    )

    assert evaluation.guaranteed_profit_units == 40
    assert evaluation.payout_lower_bound_units == 100
    assert evaluation.failed_qualification_ids == ("margin",)
```

- [ ] **Step 2: Run the net-margin test and verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg_oracle.py::test_net_margin_uses_minimum_payout_as_denominator
```

Expected: FAIL because the current implementation divides by cost and returns no failed qualification IDs.

- [ ] **Step 3: Add the occupied-day regression**

```python
def test_annualized_return_rounds_occupied_time_up_to_at_least_one_day() -> None:
    key = observation()
    built = problem(
        (action("a", "a", key, (ExecutableCostSlice(1, 1, 100),)),),
        (state("a", key, "a", (("yes", TerminalKind.NORMAL_YES, 101),), AS_OF + timedelta(hours=12)),),
    )
    built = replace(
        built,
        qualification_constraints=(
            QualificationConstraint(
                "annual", "v1", QualificationMetric.ANNUALIZED_RETURN_PPM,
                Comparison.GREATER_THAN_OR_EQUAL, 5_000_000, 1,
            ),
        ),
    )

    evaluation = evaluate_fixed_portfolio(
        built, (ActionQuantity("a", 1),), OracleBudget(2, 2, 2),
    )

    assert evaluation.failed_qualification_ids == ("annual",)
```

- [ ] **Step 4: Run the occupied-day test and verify RED**

Run the test by exact node ID. Expected: FAIL because 12 hours is currently annualized as half a day and incorrectly passes 500%.

- [ ] **Step 5: Implement the conservative formulas**

Change the net-margin denominator and add a checked day helper:

```python
elif constraint.metric == QualificationMetric.NET_MARGIN_PPM:
    left = _checked_product(
        evaluation.guaranteed_profit_units,
        1_000_000,
        constraint.threshold_denominator,
    )
    right = _checked_multiply(
        constraint.threshold_numerator,
        evaluation.payout_lower_bound_units,
    )
elif constraint.metric == QualificationMetric.ANNUALIZED_RETURN_PPM:
    occupied_days = _occupied_days(problem, evaluation)
    left = _checked_product(
        evaluation.guaranteed_profit_units,
        365,
        1_000_000,
        constraint.threshold_denominator,
    )
    right = _checked_product(
        constraint.threshold_numerator,
        evaluation.cost_upper_bound_units,
        occupied_days,
    )


def _occupied_days(problem: ArbitrageProblem, evaluation: PortfolioEvaluation) -> int:
    seconds = _release_delay_seconds(problem, evaluation)
    return max(1, _checked_add(seconds, 86_399) // 86_400)
```

Keep `_release_delay_seconds` unchanged for the delay-seconds metric.

- [ ] **Step 6: Run both new tests and the existing qualification/overflow tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg_oracle.py -k "qualification or net_margin or annualized or subsecond or overflow"
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/open_trader/prediction_n_leg_oracle.py tests/test_prediction_n_leg_oracle.py
git commit -m "fix: correct N-leg qualification arithmetic"
```

---

### Task 2: Make canonical v1 input complete and fail-closed

**Files:**
- Modify: `src/open_trader/prediction_n_leg.py:11-205,302-800`
- Modify: `src/open_trader/prediction_n_leg_oracle.py:105-126,610-624,708-754`
- Test: `tests/test_prediction_n_leg.py`
- Test: `tests/test_prediction_n_leg_oracle.py`
- Modify: `tests/fixtures/prediction_n_leg_v1.json`

**Interfaces:**
- Produces constants `REQUEST_SCHEMA_V1`, `PROBLEM_SCHEMA_V1`, `OBSERVATION_SCHEMA_V1`, and later-consumed `PAYOUT_PROOF_SCHEMA_V1`.
- Produces `CandidateAction` with explicit venue/account/chain and selected-quantity bounds.
- Produces structurally representable incomplete `TerminalAtom` data that is stopped before enumeration.
- Changes `_quantity_ranges(actions) -> tuple[tuple[int, ...], ...]` to enumerate zero plus the explicit selected range.

- [ ] **Step 1: Add failing canonical-action and quantity-domain tests**

Update the shared action helper to construct:

```python
CandidateAction(
    action_id=action_id,
    venue_id="test-venue",
    account_id="test-account",
    chain_id="test-chain",
    market_contract_id=contract_id,
    settlement_observation_key=key,
    side=ActionSide.BUY_YES,
    lot_step_units=1,
    quantity_scale=1,
    min_quantity_lots=1,
    max_quantity_lots=cost_slices[-1].last_lot,
    settlement_asset_id="usd-cents",
    valuation_unit_id="usd-cents",
    asset_valuation_rule_id="usd-cents-v1",
    cost_slices=cost_slices,
)
```

Add tests asserting:

```python
def test_explicit_quantity_bounds_drive_the_oracle_domain() -> None:
    key = observation()
    bounded = replace(
        action("a", "a", key, (ExecutableCostSlice(1, 5, 1),)),
        min_quantity_lots=2,
        max_quantity_lots=3,
    )
    built = problem(
        (bounded,),
        (state("a", key, "a", (("yes", TerminalKind.NORMAL_YES, 2),)),),
    )

    assert quantity_vector_count(built) == 3


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("venue_id", "", "INVALID_IDENTIFIER"),
        ("account_id", "", "INVALID_IDENTIFIER"),
        ("chain_id", "", "INVALID_IDENTIFIER"),
        ("min_quantity_lots", 0, "INVALID_QUANTITY_BOUNDS"),
        ("max_quantity_lots", 6, "QUANTITY_BOUNDS_EXCEED_COST_SLICES"),
    ),
)
def test_candidate_action_requires_complete_identity_and_bounds(field, value, code) -> None:
    base = sample_problem()
    malformed = replace(base, actions=(replace(base.actions[0], **{field: value}),))
    assert code in {issue.code for issue in validate_problem(malformed)}
```

- [ ] **Step 2: Run the new action tests and verify RED**

Expected: constructor/attribute failures because the fields and explicit range do not exist.

- [ ] **Step 3: Add failing schema-version tests**

Cover both wire and direct objects:

```python
@pytest.mark.parametrize("path", ("request", "problem", "observation"))
def test_unknown_schema_versions_fail_closed(path: str) -> None:
    key = observation()
    base = problem(
        (action("a", "a", key),),
        (state("a", key, "a", (("yes", TerminalKind.NORMAL_YES, 2),)),),
    )
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, base, OracleBudget(2, 2, 2))
    payload = canonical_payload(request)
    if path == "request":
        payload["schema_version"] = "future.v999"
        direct = replace(request, schema_version="future.v999")
    elif path == "problem":
        payload["problem"]["schema_version"] = "future.v999"
        direct = replace(request, problem=replace(base, schema_version="future.v999"))
    else:
        payload["problem"]["actions"][0]["settlement_observation_key"]["schema_version"] = "future.v999"
        changed_key = replace(base.actions[0].settlement_observation_key, schema_version="future.v999")
        changed_action = replace(base.actions[0], settlement_observation_key=changed_key)
        changed_state = replace(base.terminal_state_sets[0], settlement_observation_key=changed_key)
        direct = replace(
            request,
            problem=replace(base, actions=(changed_action,), terminal_state_sets=(changed_state,)),
        )

    with pytest.raises(ModelDecodeError):
        request_from_payload(payload)

    assert oracle.find_qualified(direct).unknown_reason == UnknownReason.INVALID_MODEL
```

- [ ] **Step 4: Add failing incomplete-terminal tests**

For both `capital_release_at` and `rule_version`, remove the JSON key, decode the request, and assert the canonical decoded atom contains `None`. Then run Admission and assert `UNKNOWN_TERMINAL_DATA`. Keep the existing missing-payout case in the same parametrized contract.

```python
@pytest.mark.parametrize("missing", ("payout", "rule_version", "capital_release_at"))
def test_incomplete_terminal_data_returns_one_unknown_reason(missing: str) -> None:
    key = observation()
    built = problem(
        (action("a", "a", key),),
        (state("a", key, "a", (("yes", TerminalKind.NORMAL_YES, 2),)),),
    )
    base = OracleRequest(
        REQUEST_SCHEMA_V1,
        SearchMode.ADMISSION,
        built,
        OracleBudget(2, 2, 2),
    )
    payload = canonical_payload(base)
    atom = payload["problem"]["terminal_state_sets"][0]["atoms"][0]
    if missing == "payout":
        atom["payouts"] = []
    else:
        atom.pop(missing)

    request = request_from_payload(payload)
    result = oracle.find_qualified(request)
    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.UNKNOWN_TERMINAL_DATA
```

- [ ] **Step 5: Run the schema and terminal tests and verify RED**

Expected: future schemas decode, and missing rule/release keys currently raise before Oracle classification.

- [ ] **Step 6: Implement exact schemas and the expanded action contract**

Add exact constants and fields:

```python
REQUEST_SCHEMA_V1 = "open_trader.prediction_n_leg.request.v1"
PROBLEM_SCHEMA_V1 = "open_trader.prediction_n_leg.problem.v1"
OBSERVATION_SCHEMA_V1 = "open_trader.prediction_n_leg.observation.v1"
PAYOUT_PROOF_SCHEMA_V1 = "open_trader.prediction_n_leg.payout_proof.v1"


@dataclass(frozen=True, slots=True)
class CandidateAction:
    action_id: str
    venue_id: str
    account_id: str
    chain_id: str
    market_contract_id: str
    settlement_observation_key: SettlementObservationKey
    side: ActionSide
    lot_step_units: int
    quantity_scale: int
    min_quantity_lots: int
    max_quantity_lots: int
    settlement_asset_id: str
    valuation_unit_id: str
    asset_valuation_rule_id: str
    cost_slices: tuple[ExecutableCostSlice, ...]


@dataclass(frozen=True, slots=True)
class TerminalAtom:
    atom_id: str
    kind: TerminalKind
    rule_version: str | None
    payouts: tuple[ActionPayout, ...]
    capital_release_at: datetime | None
```

Validate exact schema values, non-empty identity fields, `0 < min <= max`, and `max <= cost_slices[-1].last_lot`. Decode missing/null atom rule/release as `None`; preserve all other exact-key checks.

- [ ] **Step 7: Implement the explicit quantity domain and terminal UNKNOWN gate**

```python
def _quantity_ranges(actions: tuple[CandidateAction, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(
        (0, *range(action.min_quantity_lots, action.max_quantity_lots + 1))
        for action in actions
    )
```

Map `MISSING_ACTION_PAYOUT`, `MISSING_TERMINAL_RULE_IDENTITY`, and `MISSING_CAPITAL_RELEASE_AT` to `UNKNOWN_TERMINAL_DATA`. Check the request schema before quantity counting.

- [ ] **Step 8: Update all test constructors and mechanically rewrite corpus requests**

Every fixture action receives:

```json
{
  "venue_id": "fixture-venue",
  "account_id": "fixture-account",
  "chain_id": "fixture-chain",
  "min_quantity_lots": 1,
  "max_quantity_lots": "the existing final cost-slice last_lot"
}
```

Use a one-off script outside the repository to update all primary and additional replay requests, then recompute literal request/result payloads and fingerprints through the public decoders/Oracle:

```python
import json
from pathlib import Path

from open_trader.prediction_n_leg import canonical_payload, fingerprint, request_from_payload
from open_trader.prediction_n_leg_oracle import diagnose_raw_arbitrage, find_qualified, solve_optimal
from open_trader.prediction_n_leg import SearchMode

path = Path("tests/fixtures/prediction_n_leg_v1.json")
corpus = json.loads(path.read_text(encoding="utf-8"))

def run(request):
    if request.mode == SearchMode.ADMISSION:
        return find_qualified(request)
    if request.mode == SearchMode.OPTIMIZATION:
        return solve_optimal(request)
    return diagnose_raw_arbitrage(request.problem, request.budget)

for case in corpus["cases"]:
    for replay in (case, *case.get("additional_replays", ())):
        for action in replay["request"]["problem"]["actions"]:
            action["venue_id"] = "fixture-venue"
            action["account_id"] = "fixture-account"
            action["chain_id"] = "fixture-chain"
            action["min_quantity_lots"] = 1
            action["max_quantity_lots"] = action["cost_slices"][-1]["last_lot"]
        request = request_from_payload(replay["request"])
        result = run(request)
        replay["request"] = canonical_payload(request)
        replay["expected_request_fingerprint"] = fingerprint(request)
        replay["expected_result"] = canonical_payload(result)
        replay["expected_result_fingerprint"] = fingerprint(result)

path.write_text(
    json.dumps(corpus, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
```

Run it with `PYTHONPATH=src` only after the corresponding RED tests exist and production changes are GREEN. Review the JSON diff and compare each case's business status, selected quantities, payout/cost/profit, and unknown reason against the pre-change corpus; only already-approved qualification corrections may change semantics.

- [ ] **Step 9: Update the pinned corpus SHA and run the complete focused suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py
```

Expected: PASS with all 16 cases replayed twice.

- [ ] **Step 10: Commit Task 2**

```bash
git add src/open_trader/prediction_n_leg.py src/open_trader/prediction_n_leg_oracle.py tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py tests/fixtures/prediction_n_leg_v1.json
git commit -m "feat: complete N-leg canonical v1 inputs"
```

---

### Task 3: Unify proof records and deterministic qualification identity

**Files:**
- Modify: `src/open_trader/prediction_n_leg.py:54-84,219-285,834-925`
- Modify: `src/open_trader/prediction_n_leg_oracle.py:511-566,622-654,766-929`
- Test: `tests/test_prediction_n_leg.py`
- Test: `tests/test_prediction_n_leg_oracle.py`
- Modify: `tests/fixtures/prediction_n_leg_v1.json`

**Interfaces:**
- Produces `ProofResultKind` and one public `PayoutProof` record for portfolio and exhaustive negative evidence.
- Removes public `ExhaustiveSearchProof`.
- Preserves `PortfolioSolution.payout_proof` and changes `OracleResult.negative_proof` to `PayoutProof | None`.
- Produces `_qualification_fingerprint(problem: ArbitrageProblem) -> str` using stable constraint-ID order.

- [ ] **Step 1: Add failing unified-proof contract tests**

Freeze the discriminated fields:

```python
class ProofResultKind(StrEnum):
    PORTFOLIO = "PORTFOLIO"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
    NO_ARBITRAGE = "NO_ARBITRAGE"
```

Add one positive and two negative round-trip tests asserting every proof has `schema_version == PAYOUT_PROOF_SCHEMA_V1`, the matching `result_kind`, and only its branch's fields populated. Mutate one field from the other branch and assert `result_from_payload` raises `ModelDecodeError`.

- [ ] **Step 2: Run unified-proof tests and verify RED**

Expected: missing enum/schema/result kind and the existing separate `ExhaustiveSearchProof` type.

- [ ] **Step 3: Add the qualification-order determinism regression**

Construct two direct requests whose qualification tuples are reversed, assert their request fingerprints are equal, run exhaustive Admission, then assert:

```python
assert first.negative_proof is not None
assert second.negative_proof is not None
assert first.negative_proof.qualification_fingerprint == second.negative_proof.qualification_fingerprint
assert fingerprint(first) == fingerprint(second)
```

- [ ] **Step 4: Run determinism test and verify RED**

Expected: request fingerprints match but qualification and result fingerprints differ.

- [ ] **Step 5: Implement the single discriminated PayoutProof**

Replace the two proof dataclasses with one versioned record:

```python
@dataclass(frozen=True, slots=True)
class PayoutProof:
    schema_version: str
    result_kind: ProofResultKind
    problem_fingerprint: str
    portfolio_fingerprint: str | None
    worst_scenario: SettlementScenario | None
    worst_state_cut: WorstStateCut | None
    payout_lower_bound_units: int | None
    cost_upper_bound_units: int | None
    guaranteed_profit_units: int | None
    conservative_capital_release_at: datetime | None
    selected_support_graph: SelectedSupportGraph | None
    proof_method: str
    request_fingerprint: str | None
    source_problem_fingerprint: str | None
    qualification_fingerprint: str | None
    quantity_vectors_total: int | None
    quantity_vectors_examined: int | None
    joint_states_per_vector: int | None
    rejection_counts: tuple[tuple[str, int], ...]
```

Portfolio proofs use `proof_method="BOUNDED_EXACT_ORACLE_V1"`; exhaustive negatives use `proof_method="EXHAUSTIVE_ORACLE_V1"`. Result decoding validates the complete branch invariants and rejects mixed records.

- [ ] **Step 6: Make qualification identity canonical**

```python
def _qualification_fingerprint(problem: ArbitrageProblem) -> str:
    constraints = tuple(
        sorted(problem.qualification_constraints, key=lambda item: item.constraint_id)
    )
    return fingerprint({
        "schema_version": PROBLEM_SCHEMA_V1,
        "qualification_constraints": constraints,
    })
```

Use this helper for both portfolio and negative proofs. Do not depend on caller tuple order.

- [ ] **Step 7: Update proof construction, decoding, and validation**

Update `build_portfolio_solution`, `build_exhaustive_search_proof`, `_validate_result`, and all proof payload decoders. Remove every import and construction of `ExhaustiveSearchProof`.

- [ ] **Step 8: Regenerate literal corpus results and update its pinned SHA**

Reuse the Task 2 one-off process, now changing only proof/result wire shape and fingerprints. Hand-check at minimum:

- a qualified non-optimal portfolio;
- an optimal portfolio;
- `NO_QUALIFIED_OPPORTUNITY`;
- `NO_ARBITRAGE` with source problem fingerprint;
- every `UNKNOWN` case has no proof.

- [ ] **Step 9: Run focused and affected Prediction suites**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage.py tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py
```

Expected: both PASS.

- [ ] **Step 10: Commit Task 3**

```bash
git add src/open_trader/prediction_n_leg.py src/open_trader/prediction_n_leg_oracle.py tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py tests/fixtures/prediction_n_leg_v1.json
git commit -m "feat: unify N-leg payout proof records"
```

---

### Task 4: Final verification, changelog, and independent review

**Files:**
- Modify: `CHANGELOG.md`
- Verify: all Task 1-3 files

**Interfaces:**
- Consumes: final corrected v1 contract and corpus.
- Produces: exact operator-facing verification evidence and a reviewed, clean branch.

- [ ] **Step 1: Run exact focused tests from a clean committed tree**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py
```

Record the exact pass count and duration.

- [ ] **Step 2: Run affected Prediction regression**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage.py tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py
```

Record the exact result.

- [ ] **Step 3: Run direct corpus replay cases**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg_oracle.py -k "qualified_not_optimal or no_qualified_positive_raw or no_arbitrage"
```

Expected: admission, optimization, and both negative proof kinds pass from fresh decoded objects.

- [ ] **Step 4: Run compile and diff checks**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m compileall -q src/open_trader/prediction_n_leg.py src/open_trader/prediction_n_leg_oracle.py
git diff --check
```

Expected: both exit zero.

- [ ] **Step 5: Run the normal project full suite once**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
```

Record the exact output. Do not describe the repository gate as green if legacy fixtures or sandbox socket tests still fail.

- [ ] **Step 6: Update the dated changelog entry**

Amend the 2026-08-12 #48 entry to state:

- net margin now uses minimum payout;
- annualization uses ceiling 24-hour days with a one-day minimum;
- canonical actions include venue/account/chain and explicit quantity bounds;
- incomplete terminal inputs and unsupported versions fail closed;
- positive and negative evidence share versioned `PayoutProof`;
- fingerprints are order invariant;
- exact focused, affected, and full-suite results;
- no runtime, Dashboard, solver dependency, or order path changed.

- [ ] **Step 7: Commit verification evidence**

```bash
git add CHANGELOG.md
git commit -m "docs: record Issue 48 correction verification"
```

- [ ] **Step 8: Run final two-axis code review**

Use `/code-review` against fixed point `281929e782f7cbdc3e6836756c720594ea77d3e7`. Standards source is `AGENTS.md`; spec sources are GitHub Issue #48 and `docs/superpowers/specs/2026-08-12-issue-48-v1-contract-corrections-design.md`.

Any false-safe, wire ambiguity, non-determinism, or missing acceptance criterion is a merge blocker. Fix through a new RED/GREEN cycle and rerun Steps 1-5.

- [ ] **Step 9: Confirm branch state**

```bash
git status --short --branch
git log --oneline 281929e782f7cbdc3e6836756c720594ea77d3e7..HEAD
```

Expected: clean isolated branch. Do not merge or push without a separate user decision.

## Completion mapping

- False-safe net margin and annualization: Task 1.
- Venue/account/chain identity and explicit quantity bounds: Task 2.
- Missing payout/rule/release `UNKNOWN`: Task 2.
- Strict v1 compatibility gate: Task 2.
- One proof record and result-kind validation: Task 3.
- Order-invariant qualification/result fingerprints: Task 3.
- Literal corpus, regression evidence, changelog, and independent review: Tasks 2-4.
