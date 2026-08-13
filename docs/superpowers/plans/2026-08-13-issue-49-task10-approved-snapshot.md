# Issue #49 Task 10 Approved Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert one frozen, user-approved production threshold preview into a
complete #48 approval envelope, validate it with the exact Oracle, and import
exactly one anonymized real case into the approved benchmark corpus.

**Architecture:** Use a one-off stdlib Python script under `/private/tmp` to read
three exact rows from the production SQLite database in read-only mode, assert
their frozen identities, and construct existing #48 dataclasses. Write only the
ignored inbox envelope, then use the existing `import-approved` command to
produce the sole tracked data change. Add no exporter, schema, CLI, or runtime
path.

**Tech Stack:** Python 3.12 stdlib (`datetime`, `decimal`, `json`, `pathlib`,
`sqlite3`), existing `prediction_n_leg` dataclasses, existing bounded exact
Oracle, existing `prediction_solver_benchmark.import_approved_snapshot`.

## Global Constraints

- Frozen source database:
  `/Users/ray/projects/open_trader/data/prediction_arbitrage/prediction_arbitrage.sqlite3`.
- Read only preview `bffd03fe104447e589a4539bb9b7846d`, relation
  `threshold:2765212fb8dd441806b4d6dadc833d12a885c69cd5a32579e852c8e3733ab49f`,
  and cache key
  `417ee818c70333531091f01943902816c396e29d6e7057ba39793c0656384b80`.
- Require rule fingerprint
  `281ad84b51843183838f3103cf53e8c2ff06ce77d79effbfba0566cf2a684a6a`
  and cached `APPROVE` / `B_IMPLIES_A` before constructing data.
- Treat `2026-08-13T20:00:00Z` as benchmark-only `capital_release_at` under the
  explicitly approved assumption; do not call it actual venue settlement proof.
- Preserve `SPLIT`/50-50 semantics; never collapse them into ordinary YES/NO.
- Use the frozen 1 USDC, 1%, 15%, and 30-day qualification thresholds.
- Do not modify solver code, tests, fixtures, Dashboard, Prediction Service,
  SQLite data, production processes, orders, or #50.
- Do not run Docker, environment builds, or full benchmarks in this plan. Those
  remain subsequent steps in the existing Task 10 plan after this gate passes.
- The only tracked implementation artifact is
  `benchmarks/prediction_solver/corpus/approved_v1.json`.

---

### Task 1: Freeze and validate the source rows

**Files:**
- Read: `/Users/ray/projects/open_trader/data/prediction_arbitrage/prediction_arbitrage.sqlite3`
- Create temporarily: `/private/tmp/issue49-task10-approved-snapshot.py`
- Create ignored: `benchmarks/prediction_solver/inbox/approved_component.json`

**Interfaces:**
- Consumes: the exact preview, relation catalog entry, and LLM cache entry named
  in Global Constraints.
- Produces: a local envelope with schema
  `open_trader.prediction_solver.approved_envelope.v1`.

- [ ] **Step 1: Record the precondition**

Run:

```bash
jq -e '.cases == []' benchmarks/prediction_solver/corpus/approved_v1.json
test ! -e benchmarks/prediction_solver/inbox/approved_component.json
```

Expected: both commands return zero. If a case or inbox file already exists,
stop and review it rather than overwriting evidence.

- [ ] **Step 2: Write the one-off conversion script**

Create `/private/tmp/issue49-task10-approved-snapshot.py`. The script must:

1. Open SQLite with the URI
   `file:/Users/ray/projects/open_trader/data/prediction_arbitrage/prediction_arbitrage.sqlite3?mode=ro`.
2. Select the exact preview by `preview_id`, the exact relation from
   `relation_state.payload`, and the exact `llm_cache` row by `cache_key`.
3. Assert these source literals before construction:

```python
assert preview["relation_id"] == RELATION_ID
assert preview["created_at"] == "2026-08-12T21:55:32.153149Z"
assert preview["resolution_at"] == "2026-08-13T20:00:00Z"
assert preview["llm_status"] == "approved"
assert preview["relation"] == "B_IMPLIES_A"
assert relation["rules_hash_a"] == RULE_FINGERPRINT
assert relation["rules_hash_b"] == RULE_FINGERPRINT
assert cached["structured_result"]["decision"] == "APPROVE"
assert cached["structured_result"]["relation"] == "B_IMPLIES_A"
assert cached["structured_result"]["uncertainties"] == []
```

4. Build one `ArbitrageProblem` with:

```text
problem_id = the frozen relation_id
as_of = 2026-08-12T21:55:32.153149Z
valuation_unit_id = polymarket-usdc-micro
venue_id = polymarket
account_id = 0x1AF9e3D4141676Af4c6858509D71471561e00562
chain_id = polygon
settlement_asset_id = polymarket-usdc-micro
asset_valuation_rule_id = native-usdc-micro-v1
observation = Pyth Equity.US.SPY/USD close,
              2026-08-13T13:30:00Z..2026-08-13T20:00:00Z,
              America/New_York, rule version = frozen rule fingerprint
action A id = token 52963169872172163415408758081750828216242578213929567808715321072076899137993
action A = BUY_YES, condition 0x44c120ca4ed8782087d0eae5dda95aa4a0f5fbfa27a96e817e5e5c8fe1b0f2a6,
           lot_step_units = 1, quantity_scale = 1, quantity lots 5..20,
           cost 9_357 micro-USDC per lot
action B id = token 78529238963422611135844913270936539547814986961475194528473783315714617177605
action B = BUY_NO, condition 0x8a78eb8696a6491fb526edd3e13b19bdecf51d9bdf4df7921b00bec447d460c7,
           lot_step_units = 1, quantity_scale = 1, quantity lots 5..20,
           cost 986_553 micro-USDC per lot
A terminal payouts for action A = A_YES 1_000_000, A_NO 0, A_SPLIT 500_000
B terminal payouts for action B = B_YES 0, B_NO 1_000_000, B_SPLIT 500_000
capital_release_at = 2026-08-13T20:00:00Z on all six atoms
relation id = approved-b-implies-a, kind = IMPLIES,
              ordered contracts (contract B, contract A)
```

5. Add exactly these forbidden pairs so split settlement is synchronous:

```text
(A_SPLIT, B_YES)
(A_SPLIT, B_NO)
(B_SPLIT, A_YES)
(B_SPLIT, A_NO)
```

6. Add exactly these `QualificationConstraint` values:

```text
GUARANTEED_PROFIT_UNITS >= 1_000_000 / 1
NET_MARGIN_PPM >= 10_000 / 1
ANNUALIZED_RETURN_PPM >= 150_000 / 1
MAX_CAPITAL_RELEASE_DELAY_SECONDS <= 2_592_000 / 1
```

7. Build this envelope around `canonical_payload(problem)`:

```python
envelope = {
    "schema_version": "open_trader.prediction_solver.approved_envelope.v1",
    "source_alias": "production-threshold-preview",
    "approval_id": "task10-user-approval:2026-08-13T10:36:37Z",
    "generation_id": "preview:bffd03fe104447e589a4539bb9b7846d",
    "approver": "operator:ray",
    "approved_at": "2026-08-13T10:36:37Z",
    "captured_at": "2026-08-12T21:55:32.153149Z",
    "problem": canonical_payload(problem),
}
```

8. Write canonical pretty JSON plus one newline to
   `benchmarks/prediction_solver/inbox/approved_component.json` using a
   temporary sibling and `os.replace`.

- [ ] **Step 3: Execute the converter once**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python \
  /private/tmp/issue49-task10-approved-snapshot.py
```

Expected: exit zero, one ignored `approved_component.json`, no database writes,
and no tracked change yet.

- [ ] **Step 4: Decode and validate the model**

Run a read-only Python check that loads `envelope["problem"]`, calls
`problem_from_payload`, and asserts:

```python
assert validate_problem(problem) == ()
assert len(problem.actions) == 2
assert {atom.kind.value for atom in problem.terminal_state_sets[0].atoms} == {
    "NORMAL_NO", "NORMAL_YES", "SPLIT"
}
assert len(problem.constraint_model.forbidden_atom_combinations) == 4
```

Expected: exit zero.

---

### Task 2: Close the exact Oracle and import the anonymized case

**Files:**
- Read ignored: `benchmarks/prediction_solver/inbox/approved_component.json`
- Modify: `benchmarks/prediction_solver/corpus/approved_v1.json`

**Interfaces:**
- Consumes: the validated envelope from Task 1.
- Produces: exactly one anonymized approved real case accepted by the existing
  corpus loader.

- [ ] **Step 1: Run the bounded exact Oracle before import**

Construct this request from the decoded problem:

```python
OracleRequest(
    REQUEST_SCHEMA_V1,
    SearchMode.ADMISSION,
    problem,
    OracleBudget(
        max_quantity_vectors=289,
        max_joint_states=9,
        max_support_rechecks=64,
    ),
)
```

Run `find_qualified(request)` and serialize the result with `canonical_payload`.
Require all of the following:

```python
assert result.unknown_reason is None
assert result.solve_status is not SolveStatus.UNKNOWN
assert result.proof_status is ProofStatus.PROVEN
```

Expected business result: `NO_QUALIFIED_OPPORTUNITY`, because the frozen maximum
20-lot guaranteed profit remains below the 1 USDC threshold. If the exact result
differs, stop and review the mapping; do not weaken the policy.

- [ ] **Step 2: Import through the existing CLI**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  prediction-solver-benchmark import-approved
```

Expected: exit zero and only `approved_v1.json` becomes tracked-dirty.

- [ ] **Step 3: Verify corpus identity and privacy**

Use existing `problem_from_payload` and `fingerprint` to assert:

```python
assert len(corpus["cases"]) == 1
assert len(corpus["input_gaps"]) == 1
assert corpus["input_gaps"][0]["gap_id"] == "legacy-incomplete-terminal-model"
assert case["source_problem_fingerprint"] == fingerprint(source_problem)
assert case["anonymized_problem_fingerprint"] == fingerprint(anonymized_problem)
assert validate_problem(anonymized_problem) == ()
```

Also assert that serialized committed corpus contains none of these raw values:

```text
bffd03fe104447e589a4539bb9b7846d
417ee818c70333531091f01943902816c396e29d6e7057ba39793c0656384b80
threshold:2765212fb8dd441806b4d6dadc833d12a885c69cd5a32579e852c8e3733ab49f
0x1AF9e3D4141676Af4c6858509D71471561e00562
0x44c120ca4ed8782087d0eae5dda95aa4a0f5fbfa27a96e817e5e5c8fe1b0f2a6
0x8a78eb8696a6491fb526edd3e13b19bdecf51d9bdf4df7921b00bec447d460c7
52963169872172163415408758081750828216242578213929567808715321072076899137993
78529238963422611135844913270936539547814986961475194528473783315714617177605
operator:ray
```

Expected: all assertions pass. The public market thresholds, observation
semantics, timestamps, and conservative numeric bounds may remain because they
define the benchmark model rather than private identity.

- [ ] **Step 4: Run focused regression**

Run serially:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_prediction_n_leg.py \
  tests/test_prediction_n_leg_oracle.py \
  tests/test_prediction_solver_benchmark.py
PYTHONSAFEPATH=1 PYTHONPATH="$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m compileall -q \
  src/open_trader/prediction_n_leg.py \
  src/open_trader/prediction_n_leg_oracle.py \
  src/open_trader/prediction_solver_benchmark.py
git diff --check
```

Expected: tests and compilation pass; diff check has no output.

- [ ] **Step 5: Review scope and commit the gate artifact**

Require:

```bash
git status --short
git diff --name-only
```

Expected tracked path: only
`benchmarks/prediction_solver/corpus/approved_v1.json`. The inbox and temporary
script stay ignored/untracked and must not be staged.

After independent Spec and Standards review report no actionable findings:

```bash
git add benchmarks/prediction_solver/corpus/approved_v1.json
git commit -m "benchmark: approve real N-leg snapshot"
```

This commit unlocks, but does not execute, Task 10 environment builds and full
macOS/Linux benchmarks.
