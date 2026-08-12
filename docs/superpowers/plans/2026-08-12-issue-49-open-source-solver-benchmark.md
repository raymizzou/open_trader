# Issue #49 Open-Source Solver Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Benchmark HiGHS, SCIP, and OR-Tools CP-SAT against the same #48 N-leg
model and the same constraint-generation loop, eliminate unsafe or inoperable
candidates, and produce one reproducible solver/proof-policy recommendation for
#50 or an explicit no-selection result.

**Architecture:** Keep #48 unchanged as the canonical domain and small exact
truth source. Add four flat benchmark modules matching the repository's current
layout: one solver-neutral integer model/engine, one module containing exactly
the three adapters, one subprocess protocol/harness, and one corpus/runner/report
CLI. The three optional solver installations remain outside `pyproject.toml` in
reusable isolated environments. A raw solver claim never becomes a canonical
proof; only an exact-Oracle check may attach the existing #48 `OracleResult`,
while SCIP/VIPR evidence remains benchmark-only until #50 defines its production
proof schema.

**Tech Stack:** Python 3.12 stdlib (`argparse`, `dataclasses`, `enum`, `hashlib`,
`json`, `os`, `pathlib`, `platform`, `resource`, `selectors`, `statistics`,
`subprocess`, `tempfile`, `time`), pytest, HiGHS/highspy 1.15.1,
PySCIPOpt 6.2.1 with SCIP 10.0.2, OR-Tools 9.15.6755, Linux Docker, and
SCIP/VIPR certificate tooling where supported.

## Global constraints

- Do not modify `prediction_n_leg.py`, `prediction_n_leg_oracle.py`, their v1
  wire schemas, their 16-case fixture, or their mathematics to suit a solver.
- Do not touch Prediction Service, SQLite production data, Dashboard, live
  books, credentials, monitors, balances, notifications, or order submission.
- Do not add any candidate solver to `pyproject.toml`; lazy imports must keep the
  ordinary application and test environment solver-free.
- Use one solver thread and a fixed seed inside every worker. Worker-count 1/2/4
  is harness parallelism, not hidden solver parallelism.
- Keep all master/adversary coefficients and exact parent-side recomputation in
  signed 64-bit integers. Reject an unsafe coefficient or row-activity bound as
  `NUMERIC_UNSAFE`; never rescale or round away risk.
- Treat native `FEASIBLE`, `INFEASIBLE`, `OPTIMAL`, bounds, and incumbents as
  claims. The parent validates integer values and every row exactly; the #48
  Oracle or a separately executed certificate checker supplies independent
  evidence.
- A benchmark run may carry the unchanged #48 `OracleResult` only after an
  exact Oracle comparison. Large solver-only and VIPR-checked claims stay in the
  benchmark envelope. #50 owns any production `PayoutProof` extension.
- Quick runs prove code/protocol behavior only. A recommendation requires full
  macOS and Linux runs, at least one complete approved canonical real snapshot,
  all hard safety gates, and report replay from committed JSONL.
- Use native macOS and Linux Docker; do not use a VPS or describe container
  timing as VPS capacity.
- Add no future-vendor factory, plugin registry, remote benchmark service, or
  persistent daemon. `SolverBackend` has exactly three implementations because
  this ticket compares exactly three candidates.
- Follow RED -> verify RED -> minimal GREEN -> verify GREEN for each behavior
  change. `make acceptance` does not apply because no Dashboard/runtime path is
  changed.

## Dependency map

| Task | Depends on | Unlocks |
|---|---|---|
| 1. Protocol and integer IR | #48 on `main` | 2, 4, 5, 6, 7 |
| 2. Common compiler and solve loop | 1 | 4, 5, 6, 8 |
| 3. Reusable environments | 1 | 4, 5, 6, 10 |
| 4. HiGHS adapter | 1, 2, 3 | 9 |
| 5. SCIP/VIPR adapter | 1, 2, 3 | 9 |
| 6. CP-SAT adapter | 1, 2, 3 | 9 |
| 7. Worker harness and failure campaign | 1 | 9 |
| 8. Corpus and independent checking | 2 | 9, 10 |
| 9. Runner, metrics, report, and CLI | 4, 5, 6, 7, 8 | 10 |
| 10. Full evidence and handoff | 3, 9, approved real input | #50 |

Tasks 4, 5, and 6 are independent after Tasks 1-3 and may be implemented in
parallel worktrees. All other edges above are serial.

## File map

- Add `src/open_trader/prediction_solver.py`: benchmark enums/dataclasses,
  solver-neutral integer IR, exact compiler, common master/adversary loop,
  support proof, and deterministic objective.
- Add `src/open_trader/prediction_solver_backends.py`: explicit HiGHS, SCIP, and
  CP-SAT adapters with lazy vendor imports and strict status/value mapping.
- Add `src/open_trader/prediction_solver_worker.py`: line-delimited JSON worker,
  startup handshake, parent subprocess harness, limits, RSS sampling, cleanup,
  and test-only deterministic fault modes.
- Add `src/open_trader/prediction_solver_benchmark.py`: corpus loading/intake,
  Oracle differential checking, full runner, aggregation, elimination,
  recommendation, report replay, and CLI.
- Modify `src/open_trader/__main__.py`: dispatch only the
  `prediction-solver-benchmark` command to the benchmark CLI.
- Add `tests/test_prediction_solver.py`: IR, exact compiler, qualification,
  adversary, support, objective, and constraint-generation tests.
- Add `tests/test_prediction_solver_backends.py`: common adapter contract and
  native integration smoke tests.
- Add `tests/test_prediction_solver_worker.py`: protocol and all failure cleanup
  tests.
- Add `tests/test_prediction_solver_benchmark.py`: corpus, differential checker,
  measurement, hard-gate, ranking, and report replay tests.
- Add `benchmarks/prediction_solver/corpus/synthetic_v1.json`: frozen generated
  problems, literal truth metadata, and manifest fingerprint.
- Add `benchmarks/prediction_solver/corpus/approved_v1.json`: canonical approved
  snapshots plus explicit input gaps; initially no invented case.
- Add `benchmarks/prediction_solver/requirements/{highs,scip,cp_sat}.txt`: one
  pinned optional dependency per environment.
- Add `benchmarks/prediction_solver/Dockerfile.python`: shared Linux image for
  highspy and OR-Tools, selected by a checked requirement-file argument.
- Add `benchmarks/prediction_solver/Dockerfile.scip`: exact SCIP/PySCIPOpt and
  VIPR build with pinned sources and checksums.
- Add `benchmarks/prediction_solver/licenses.json`: source URL, version/commit,
  license evidence, commercial-key requirement, and artifact checksum evidence.
- Add `scripts/build_prediction_solver_envs.sh`: idempotent hash-keyed builder
  for three macOS venvs and three Linux images.
- Modify `.gitignore`: ignore `.benchmark-envs/` and the real-snapshot inbox,
  but not frozen corpus or final result evidence.
- Modify `Makefile`: quick, environment-build, full-macOS, full-Linux, report,
  and report-verification entry points.
- Add final artifacts under
  `benchmarks/prediction_solver/results/issue49/`: `macos.jsonl`, `linux.jsonl`,
  `summary.json`, `environment_manifest.json`, `production_envelope.json`, and
  `report.md`.
- Modify `CHANGELOG.md`: operator-facing #49 result and exact verification.

---

### Task 1: Freeze the benchmark protocol and solver-neutral integer IR

**Files:**
- Add: `src/open_trader/prediction_solver.py`
- Test: `tests/test_prediction_solver.py`

**Interfaces:**

```python
BENCHMARK_PROTOCOL_V1 = "open_trader.prediction_solver.protocol.v1"

class NativeSolveStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"

class BenchmarkClassification(StrEnum):
    CHECKED = "CHECKED"
    CERTIFICATE_CHECKED = "CERTIFICATE_CHECKED"
    MEASUREMENT_ONLY = "MEASUREMENT_ONLY"
    UNKNOWN = "UNKNOWN"

class TerminationReason(StrEnum):
    COMPLETED = "COMPLETED"
    SOFT_TIMEOUT = "SOFT_TIMEOUT"
    HARD_TIMEOUT = "HARD_TIMEOUT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    CRASH = "CRASH"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    NUMERIC_UNSAFE = "NUMERIC_UNSAFE"
    PROOF_UNCLOSED = "PROOF_UNCLOSED"

@dataclass(frozen=True, slots=True)
class IntVariable:
    name: str
    lower: int
    upper: int
    integer: bool = True

@dataclass(frozen=True, slots=True)
class LinearConstraint:
    name: str
    coefficients: tuple[tuple[str, int], ...]
    lower: int | None
    upper: int | None

@dataclass(frozen=True, slots=True)
class LinearObjective:
    sense: Literal["MAX", "MIN"]
    coefficients: tuple[tuple[str, int], ...]

@dataclass(frozen=True, slots=True)
class LinearModel:
    variables: tuple[IntVariable, ...]
    constraints: tuple[LinearConstraint, ...]
    objective: LinearObjective | None

@dataclass(frozen=True, slots=True)
class BackendResult:
    status: NativeSolveStatus
    values: tuple[tuple[str, int], ...]
    objective_value: int | None
    objective_bound: int | None
    native_status: str
    solve_ns: int

class SolverBackend(Protocol):
    name: str
    version: str
    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        raise NotImplementedError
```

`BackendResult.values` contains integers only. Each adapter may round a native
floating value only when it is within `1e-6` of one integer; the shared parent
then recomputes every row with Python integers before accepting it.

- [ ] **Step 1: Add failing IR validation tests**

Add tests that reject duplicate variable/constraint names, references to
unknown variables, Boolean-as-integer values, lower bounds above upper bounds,
out-of-int64 coefficients/bounds, and a possible row activity outside int64.
Also assert that shuffled coefficient tuples canonicalize to the same order and
fingerprint.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver.py -k "linear_model or backend_result"
```

Expected: collection/import failure because `prediction_solver.py` does not
exist.

- [ ] **Step 3: Implement only the types, validation, and exact row checker**

Add:

Add signed bounds `INT64_MIN = -(2**63)` and `INT64_MAX = 2**63 - 1`, plus
these exact call surfaces:

- `validate_linear_model(model: LinearModel) -> None`
- `validate_backend_result(model: LinearModel, result: BackendResult) -> None`
- `linear_model_fingerprint(model: LinearModel) -> str`

`validate_linear_model` computes each row's minimum and maximum possible
activity from coefficient sign and variable bounds and rejects either endpoint
outside signed int64. `validate_backend_result` checks every returned variable,
bound, and row exactly; missing/extra variables and a violated row raise
`UnsafeSolverResult`.

- [ ] **Step 4: Add benchmark envelope types without widening #48**

Add immutable `BenchmarkLimits`, `SolverEvidence`, `CertificateEvidence`, and
`SolverRun`. `SolverRun` contains the raw claim, classification, termination,
optional unchanged `OracleResult`, phase timings, worker/environment metadata,
and diagnostics. Enforce:

```python
if run.classification == BenchmarkClassification.CHECKED:
    assert run.canonical_result is not None
else:
    assert run.canonical_result is None
```

`CERTIFICATE_CHECKED` requires successful checker exit/status and certificate
SHA-256, but still has no #48 `PayoutProof`.

- [ ] **Step 5: Run Task 1 tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver.py -k "linear_model or backend_result or solver_run"
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/open_trader/prediction_solver.py tests/test_prediction_solver.py
git commit -m "feat: define solver benchmark protocol"
```

---

### Task 2: Compile #48 exactly and implement one common solve loop

**Files:**
- Modify: `src/open_trader/prediction_solver.py`
- Modify: `tests/test_prediction_solver.py`

**Interfaces:**

- `compile_terminal_model(problem: ArbitrageProblem) -> LinearModel`
- `compile_master(problem, component, release_profile, cuts,
  excluded_vectors) -> CompiledMaster`, where `cuts` is
  `tuple[WorstStateCut, ...]` and `excluded_vectors` is
  `tuple[tuple[ActionQuantity, ...], ...]`
- `compile_adversary(problem, quantities, *, active_constraint_ids=None) ->
  CompiledAdversary`, with canonical action quantities and an optional frozen
  set of constraint IDs
- `solve_with_constraint_generation(request: OracleRequest,
  backend: SolverBackend, limits: BenchmarkLimits) -> SolverEvidence`

- [ ] **Step 1: Add a test-only brute-force IR backend**

In `tests/test_prediction_solver.py`, add `BruteForceBackend` that enumerates
only the tiny IR variable domains used by focused tests. It must implement the
same `SolverBackend` protocol and make no vendor import.

- [ ] **Step 2: Add failing piecewise-cost and quantity-domain tests**

For each action, compile integer quantity `q`, selected binary `b`, slice-fill
integer `x`, and slice-open binary `y` with:

```text
min_quantity * b <= q <= max_quantity * b
q = sum(slice_fill)
slice_fill <= slice_width * slice_open
previous_slice_fill >= previous_slice_width * next_slice_open
next_slice_open <= previous_slice_open
cost = sum(slice_fill * incremental_cost_upper_bound)
```

Test quantities `0`, `min`, each slice boundary, and `max` against the existing
`cost_upper_bound()` result. Test that values in `(0, min)` and above `max` are
infeasible.

- [ ] **Step 3: Add failing terminal-semantics tests**

Cover every #48 relation rule:

- exactly one atom per contract;
- `IMPLIES`, `MUTUALLY_EXCLUSIVE`, and `EXACTLY_ONE` constrain a relation only
  when all involved selected atoms are normal;
- one exceptional atom relaxes the entire relation, not just its own term;
- each forbidden atom combination adds `sum(z_atom) <= len(atoms) - 1`;
- contradictory terminal constraints return `UNKNOWN`, not a negative business
  result.

The normal-relation relaxation must use the selected exceptional count as a
big-M guard, so a three-contract relation with one exceptional and two normal
YES atoms matches `_violates_normal_relation()` exactly.

- [ ] **Step 4: Implement terminal compilation and reachable release profiles**

Do not assume every terminal atom is reachable. For every atom, solve the
terminal feasibility model once with that atom fixed to one. A contract's
conservative release is the latest `capital_release_at` among reachable atoms.
Any `UNKNOWN` reachability solve that could change that maximum makes the whole
request `UNKNOWN`.

Create one `ReleaseProfile(delay_seconds, occupied_days, release_at)` per
distinct reachable action release. For a profile-specific master:

- disallow actions later than the profile;
- require at least one selected action exactly at the profile;
- reuse #48's ceil-positive seconds and `max(1, ceil(seconds / 86400))` day rule.

This converts the annualization maximum into constants and avoids a bilinear
objective/qualification expression.

- [ ] **Step 5: Add failing qualification tests**

Compile `payout`, `cost`, and `profit = payout - cost`. Assert equivalence with
the #48 Oracle for all four metrics and both comparison directions using exact
integer inequalities:

```text
profit * denominator                                  cmp threshold
profit * 1_000_000 * denominator                      cmp threshold * payout
profit * 365 * 1_000_000 * denominator                cmp threshold * cost * occupied_days
release_delay_seconds * denominator                   cmp threshold
```

A positive margin threshold with non-positive payout is infeasible, matching
#48's explicit guard.

- [ ] **Step 6: Add failing constraint-generation tests**

The test records this exact progression:

1. one deterministic allowed scenario seeds the master;
2. the master proposes an integer portfolio;
3. the adversary fixes those quantities and minimizes payout;
4. a strictly worse scenario gets the `cut:` prefix plus its canonical
   `SettlementScenario` fingerprint;
5. repeated cuts are rejected;
6. only an adversary `OPTIMAL` result closes a fixed-portfolio lower bound;
7. timeout/`FEASIBLE` adversary results become `PROOF_UNCLOSED`;
8. admission returns the first fully proved qualified connected candidate;
9. optimization continues until all component/profile searches close.

- [ ] **Step 7: Implement support minimization and disconnected-vector handling**

Mirror #48 rather than using static graph connectivity:

- expand from selected contracts to relevant relation/forbidden constraints;
- remove relevant constraints in reverse canonical ID order;
- for each removal, re-solve fixed payout and reachable release facts;
- remove a constraint only when payout, release, and failed qualifications are
  unchanged;
- build `SelectedSupportGraph` and call existing
  `split_disconnected_solution()`;
- when a vector splits, add one exact integer no-good for that vector and let
  its child vectors compete normally; never bless the disconnected parent.

Any support recheck that is not closed makes the current request `UNKNOWN`.

- [ ] **Step 8: Implement exact deterministic optimization**

Run each relation component and release profile separately and compare accepted
candidates with the same parent-side key as #48:

```python
(
    -guaranteed_profit_units,
    cost_upper_bound_units,
    len(quantities),
    tuple((item.action_id, item.quantity_lots) for item in quantities),
)
```

Within a model, solve sequentially and lock each completed objective: maximize
profit, minimize cost, minimize leg count, maximize selection of each ascending
action ID to obtain the lexicographically earliest selected IDs, then minimize
each selected quantity in ascending action order. Do not encode the tuple as one
weighted scalar.

- [ ] **Step 9: Differential-test the common engine with the #48 Oracle**

Run the 16 frozen requests through `BruteForceBackend`; admission must match
business/proof safety, optimization must match the exact objective and tie-break,
and raw diagnostic must remain distinct. Add explicit regressions for piecewise
cost, exceptional relation bypass, unreachable late release, signed-64 overflow,
and disconnected support.

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver.py
```

Expected: PASS.

- [ ] **Step 10: Commit Task 2**

```bash
git add src/open_trader/prediction_solver.py tests/test_prediction_solver.py
git commit -m "feat: compile canonical N-leg solver models"
```

---

### Task 3: Build reusable, isolated solver environments once

**Files:**
- Add: `benchmarks/prediction_solver/requirements/highs.txt`
- Add: `benchmarks/prediction_solver/requirements/scip.txt`
- Add: `benchmarks/prediction_solver/requirements/cp_sat.txt`
- Add: `benchmarks/prediction_solver/Dockerfile.python`
- Add: `benchmarks/prediction_solver/Dockerfile.scip`
- Add: `benchmarks/prediction_solver/licenses.json`
- Add: `scripts/build_prediction_solver_envs.sh`
- Modify: `.gitignore`
- Test: `tests/test_prediction_solver_backends.py`

- [ ] **Step 1: Add failing manifest and idempotency tests**

Assert exact pins:

```text
highspy==1.15.1
pyscipopt==6.2.1
ortools==9.15.6755
```

Assert that the build key includes Python major/minor, OS/architecture, the
requirement bytes, adapter protocol version, and Dockerfile bytes. A second
build with the same key must report `REUSED`; changing any input must report
`REBUILD_REQUIRED`.

- [ ] **Step 2: Implement the macOS environment builder**

Create `.benchmark-envs/highs`, `.benchmark-envs/scip`, and
`.benchmark-envs/cp_sat`. Store one `.build-key` beside each venv. Install only
when the key changes; otherwise execute an import/version smoke check and reuse
the existing environment. Add `.benchmark-envs/` to `.gitignore`.

The script accepts only `highs`, `scip`, `cp_sat`, or `all`; unknown values exit
2. It must never install into the project venv or system Python.

- [ ] **Step 3: Implement two Linux build recipes for three images**

`Dockerfile.python` accepts only `highs.txt` or `cp_sat.txt` as a build argument.
`Dockerfile.scip` verifies before extraction:

```text
SCIP 10.0.2 source SHA-256:
eecc29f31e8c8a3089c95ef99dd310d05e1546ba40f4ff36551d75a5f5c47073
VIPR commit:
30f2951d1e90e47afa821bdd1b12b82246656c42
VIPR tarball SHA-256:
bfd905e3378353b5f4e93ad2405c75feed0d477e0a74113496fb2d6e04ca7786
```

Build SCIP with `-DEXACTSOLVE=on -DLPSEXACT=spx -DGMP=on -DMPFR=on`, build
PySCIPOpt 6.2.1 against that installed SCIP, and build `viprchk` plus `viprcomp`.
The build must fail if SCIP reports exact solving disabled or either checker
binary is absent.

- [ ] **Step 4: Freeze license evidence**

`licenses.json` records authoritative project URL, pinned version/commit,
license identifier/evidence path, and `commercial_key_required: false` for:

- HiGHS/highspy: MIT;
- PySCIPOpt: MIT;
- SCIP 10.0.2: Apache-2.0;
- OR-Tools: Apache-2.0;
- VIPR: the full MIT permission notice present in pinned source headers, with
  pinned path `code/viprchk.cpp` and SHA-256
  `2baf9c4593f5b8ef42323fbfb7cbfa0e4dfafff65e636cf6a143561b9dca2738`
  because that repository has no top-level license file in the pinned commit.

An absent or ambiguous evidence field fails the license hard gate; do not infer
approval from a package name.

- [ ] **Step 5: Run environment verification**

```bash
scripts/build_prediction_solver_envs.sh all
scripts/build_prediction_solver_envs.sh all
```

Expected: first run builds/import-checks all three macOS venvs and images;
second run reports six `REUSED` results. Record installed versions, Python,
platform, wheel/source SHA, image ID, and license-manifest fingerprint.

- [ ] **Step 6: Run manifest tests and commit Task 3**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k "manifest or build_key or license"
git add .gitignore benchmarks/prediction_solver scripts/build_prediction_solver_envs.sh tests/test_prediction_solver_backends.py
git commit -m "build: isolate solver benchmark environments"
```

---

### Task 4: Implement and validate the HiGHS adapter

**Files:**
- Add: `src/open_trader/prediction_solver_backends.py`
- Modify: `tests/test_prediction_solver_backends.py`

- [ ] **Step 1: Add the common adapter contract tests**

The contract covers integer variable/row/objective translation, maximize and
minimize, an optimal model, an infeasible model, a timed model with an incumbent,
no-incumbent timeout, exact parent row validation, single-thread/fixed-seed
configuration, and no import of another candidate package.

- [ ] **Step 2: Run the HiGHS contract and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k highs
```

Expected: FAIL because `HighsBackend` is missing.

- [ ] **Step 3: Implement `HighsBackend` with a lazy `highspy` import**

Map only native optimal, feasible-incumbent, infeasible, and all-other/limit
states to the four shared statuses. Disable output, use one thread and seed
4901, set the supplied soft time limit, and return native diagnostics. Never
turn a time-limit incumbent into `OPTIMAL`.

- [ ] **Step 4: Run unit and isolated native smoke tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k highs
PYTHONPATH=src .benchmark-envs/highs/bin/python -m open_trader.prediction_solver_backends --self-check highs
```

Expected: PASS and one JSON object reporting `highspy` 1.15.1.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/open_trader/prediction_solver_backends.py tests/test_prediction_solver_backends.py
git commit -m "feat: add HiGHS benchmark adapter"
```

---

### Task 5: Implement SCIP and independently execute VIPR checks

**Files:**
- Modify: `src/open_trader/prediction_solver_backends.py`
- Modify: `tests/test_prediction_solver_backends.py`

- [ ] **Step 1: Add failing SCIP status and certificate tests**

Test ordinary optimal/infeasible/limit mapping separately from formal mode.
Formal-mode tests require:

- `exact/enable = TRUE`;
- a request-scoped certificate filename under the harness artifact directory;
- certificate generation time and size;
- `viprcomp` followed by single-threaded `viprchk` in a separate subprocess;
- SHA-256 for original and completed certificate;
- missing, corrupt, timed-out, or non-zero checker output maps to
  `UNKNOWN`/`PROOF_UNCLOSED`.

- [ ] **Step 2: Run SCIP tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k scip
```

- [ ] **Step 3: Implement `ScipBackend` with lazy `pyscipopt` import**

Use one thread, seed 4901, hidden output, and the supplied time limit. Native
`optimal` and `infeasible` remain solver claims until checked. `timelimit`,
`memlimit`, `nodelimit`, `gaplimit`, unknown statuses, invalid integer values,
or an unclosed exact solve map no stronger than `UNKNOWN`.

Add `check_vipr_certificate()` as a separate subprocess helper. Restrict every
certificate path to the request's temporary artifact directory using resolved
path containment before opening or executing it.

- [ ] **Step 4: Run wheel smoke and exact Linux certificate smoke**

```bash
PYTHONPATH=src .benchmark-envs/scip/bin/python -m open_trader.prediction_solver_backends --self-check scip
docker run --rm open-trader-prediction-solver-scip:issue49-v1 --self-check scip-exact
```

Expected: macOS reports PySCIPOpt/SCIP versions and ordinary solve support;
Linux additionally generates, completes, corrupts, and rejects/checks a tiny
certificate with explicit timings.

- [ ] **Step 5: Run focused tests and commit Task 5**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k "scip or vipr or certificate"
git add src/open_trader/prediction_solver_backends.py tests/test_prediction_solver_backends.py
git commit -m "feat: add SCIP certificate benchmark adapter"
```

---

### Task 6: Implement and validate the CP-SAT adapter

**Files:**
- Modify: `src/open_trader/prediction_solver_backends.py`
- Modify: `tests/test_prediction_solver_backends.py`

- [ ] **Step 1: Add failing CP-SAT adapter contract tests**

In addition to the common contract, assert that all coefficients and variable
bounds remain signed int64, `MODEL_INVALID` is `UNKNOWN`, `FEASIBLE` is never
`OPTIMAL`, `INFEASIBLE` remains only a claim, `num_search_workers = 1`, and
`random_seed = 4901`.

- [ ] **Step 2: Run CP-SAT tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k cp_sat
```

- [ ] **Step 3: Implement `CpSatBackend` with lazy OR-Tools imports**

Translate the shared IR directly into `CpModel` integer variables and bounded
linear expressions. Preserve the native best objective bound only as diagnostic
evidence; parent exact validation and independent checks determine benchmark
classification.

- [ ] **Step 4: Run unit and isolated native smoke tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_backends.py -k cp_sat
PYTHONPATH=src .benchmark-envs/cp_sat/bin/python -m open_trader.prediction_solver_backends --self-check cp_sat
```

Expected: PASS and one JSON object reporting OR-Tools 9.15.6755.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/open_trader/prediction_solver_backends.py tests/test_prediction_solver_backends.py
git commit -m "feat: add CP-SAT benchmark adapter"
```

---

### Task 7: Isolate native solvers behind one reusable worker harness

**Files:**
- Add: `src/open_trader/prediction_solver_worker.py`
- Test: `tests/test_prediction_solver_worker.py`

**Wire contract:** one startup handshake line, then one request line and exactly
one response line per `request_id`; stdout is protocol-only and stderr is
diagnostic-only.

- [ ] **Step 1: Add strict codec tests**

Reject missing/extra keys, duplicate request IDs, unsupported protocol/backend,
Boolean limits, non-positive limits, malformed canonical requests, invalid UTF-8
or JSON, a response for the wrong request ID, and trailing stdout bytes.

- [ ] **Step 2: Run protocol tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_worker.py -k protocol
```

- [ ] **Step 3: Implement worker and parent harness**

The parent uses `subprocess.Popen(command, start_new_session=True)`,
`selectors.DefaultSelector` for bounded stdout reads, and monotonic deadlines.
The worker applies its own `resource.RLIMIT_AS` before importing a vendor module.
The parent samples process-group RSS from `ps -axo pid=,pgid=,rss=` and records
peak aggregate KiB.

On hard timeout or protocol/process failure:

```python
os.killpg(process.pid, signal.SIGTERM)
try:
    process.wait(timeout=1)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=1)
```

Then verify no row with that PGID remains. The failed request is finalized as
`UNKNOWN`; only the next request may use a rebuilt worker.

- [ ] **Step 4: Add deterministic failure modes and campaign tests**

Test-only CLI modes cover cooperative timeout, unresponsive hang with a child
process, `os._exit(17)`, malformed JSON, truncated JSON, protocol mismatch,
memory growth, absent certificate, corrupt certificate, and checker failure.
For each mode assert:

- current request is `UNKNOWN` and is not retried;
- the old PGID has no survivor;
- worker/rebuild counts remain bounded;
- a fresh worker completes the next known-good request;
- 20 repetitions do not produce monotonically growing orphan RSS.

- [ ] **Step 5: Run worker tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_worker.py
```

Expected: PASS and no matching worker process after the suite.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/open_trader/prediction_solver_worker.py tests/test_prediction_solver_worker.py
git commit -m "feat: isolate solver benchmark workers"
```

---

### Task 8: Freeze the three-layer corpus and independent checking boundary

**Files:**
- Add: `src/open_trader/prediction_solver_benchmark.py`
- Add: `benchmarks/prediction_solver/corpus/synthetic_v1.json`
- Add: `benchmarks/prediction_solver/corpus/approved_v1.json`
- Modify: `.gitignore`
- Test: `tests/test_prediction_solver_benchmark.py`

- [ ] **Step 1: Add failing #48 corpus reuse tests**

Load all 16 cases directly from `tests/fixtures/prediction_n_leg_v1.json`; do not
copy or rewrite them. Assert its frozen SHA remains
`a4680fb2c66dedac9e85db9cd06d0872882ca69b09fba6d9f338d0b97243ecc7`.
Each case keeps its mode, request/result fingerprint, literal expected result,
and exact Oracle budget.

- [ ] **Step 2: Add deterministic synthetic generation tests**

Seed 4901 generates and freezes 24 cases: 18 semantic cases named
`single_contract_complement`, `three_leg_exactly_one`, `implies_exceptional`,
`mutual_exclusion_exceptional`, `forbidden_atoms`, `piecewise_cost`,
`quantity_bounds`, `profit_boundary`, `margin_boundary`,
`annualized_round_up`, `release_delay_boundary`, `unreachable_late_release`,
`disconnected_support`, `same_observation_identity`, `contradictory_terminal`,
`missing_terminal_data`, `missing_valuation`, and `raw_no_arbitrage`; plus six
scale cases with 8/16/32 contracts and sparse/dense constraint graphs.

Semantic cases store literal canonical requests and expected results. Scale
cases beyond the Oracle budget store `truth_method: measurement_only` and may
never satisfy a safety hard gate. Regeneration with seed 4901 must byte-match
the committed file and manifest SHA.

- [ ] **Step 3: Add approved canonical snapshot intake**

The CLI accepts only a strict approval envelope from
`benchmarks/prediction_solver/inbox/approved_component.json`. It contains a
complete canonical `ArbitrageProblem`, stable source alias, approval/generation
IDs, approver, and UTC approval/capture times. Decode with #48, strip all unknown
keys by canonical reserialization, hash configured identity fields with a
corpus-local salt, record source and anonymized fingerprints, and append only
when the anonymized fingerprint is new.

The importer never accepts a SQLite path or network URL. Missing terminal,
valuation, release, or approval data appends an `input_gap` entry instead of a
case. Add `benchmarks/prediction_solver/inbox/` to `.gitignore`.

Initialize `approved_v1.json` with the currently known gap: legacy approved
relations/signals/previews do not contain a complete #48 terminal model and
therefore cannot be converted without invention. This is an honest gate, not a
synthetic "real" case.

- [ ] **Step 4: Implement the differential checker**

For a truth-set request:

- call `find_qualified`, `solve_optimal`, or `diagnose_raw_arbitrage` by mode;
- fixed positive claims must match exact payout, cost, profit, release,
  qualifications, connected support, and optimization tie-break when applicable;
- negative, raw no-arbitrage, optimality, and bounds must match the exact result;
- any false safe, false negative, or false optimal claim records a hard failure;
- only a full match attaches the unchanged `OracleResult` and classification
  `CHECKED`.

For an over-budget case, retain solver measurements. A successful separately
executed VIPR check may classify negative evidence `CERTIFICATE_CHECKED`, but it
does not fabricate a v1 `PayoutProof`. All other uncheckable claims are
`MEASUREMENT_ONLY` or `UNKNOWN`.

- [ ] **Step 5: Add adversarial checker regressions**

Inject a profitable-but-lossy portfolio, false infeasible, false optimality,
wrong tie-break, wrong release, disconnected support, changed problem
fingerprint, and an unverified large negative. Assert each is rejected with the
specific hard-gate/input-check reason and never yields a canonical result.

- [ ] **Step 6: Run corpus/checker tests and commit Task 8**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver_benchmark.py -k "corpus or synthetic or approved or checker"
git add .gitignore src/open_trader/prediction_solver_benchmark.py tests/test_prediction_solver_benchmark.py benchmarks/prediction_solver/corpus
git commit -m "test: freeze solver benchmark corpus"
```

---

### Task 9: Add measurement, elimination, recommendation, report replay, and CLI

**Files:**
- Modify: `src/open_trader/prediction_solver_benchmark.py`
- Modify: `src/open_trader/__main__.py`
- Modify: `tests/test_prediction_solver_benchmark.py`
- Modify: `Makefile`

- [ ] **Step 1: Add failing measurement aggregation tests**

For each warm case/solver/environment, ignore exactly five warmups and aggregate
30 measured samples. Cold startup and forced rebuild each require 30 samples.
Compute:

```python
p50 = statistics.median(values)
p95 = statistics.quantiles(values, n=100, method="inclusive")[94]
worst = max(values)
```

Test separate macOS/Linux groups, first-qualified adapter/checker time,
time-to-optimal, phase timings, rounds/cuts, peak process-group RSS, aggregate
RSS, throughput, rebuild time, and worker counts 1/2/4. Missing or duplicate
samples are report errors, not silently ignored.

- [ ] **Step 2: Add failing hard-gate and ordered-selection tests**

Hard-eliminate a candidate on any false safe/negative/optimal claim,
non-`UNKNOWN` failure mapping, semantic nondeterminism, orphan/unbounded memory,
macOS or Linux install/run failure, or failed open-source/license-key evidence.

If survivors remain, compare without a weighted score in this order:

1. first-qualified p95, then worst;
2. memory, throughput, bounded workers, then rebuild cost;
3. installation/operation evidence;
4. time-to-optimal and bound quality;
5. certificate generation/check cost.

For latency, first compute p95 and worst independently for each mandatory
`(environment, case)` cell. Compare each candidate's descending-sorted vector of
cell p95 values, then its descending-sorted vector of cell worst values. This
minimizes the worst cell and then the next worst without pooling macOS/Linux
samples or inventing weights. Apply the same worst-cell-first rule to memory and
rebuild cells; keep the contributing cell labels in the report.

Solver name controls display order only. If all five comparison stages are
identical, emit `NO_DECISIVE_WINNER` rather than selecting alphabetically. Emit
exactly one of `SELECTED`, `NO_SURVIVOR`, `NO_DECISIVE_WINNER`,
`BLOCKED_REAL_CORPUS_EMPTY`, or `BLOCKED_MISSING_ENVIRONMENT`. Never select a
solver from quick data.

- [ ] **Step 3: Implement JSONL recording and deterministic report generation**

Each record includes case/request/problem fingerprints, Git SHA, CPU/arch,
OS/container/image ID, Python/solver/adapter/protocol/corpus/license versions,
warm/cold/rebuild class, worker ID/count, phase nanoseconds, rounds/cuts,
statuses, checker result, certificate evidence, RSS, termination, and diagnostics.

Generate `summary.json`, `production_envelope.json`, and `report.md` only from
JSONL plus frozen manifests. Sort all groups/keys. A `verify-report` command
regenerates into a temporary directory and byte-compares every generated file.

- [ ] **Step 4: Add CLI and Make targets**

Dispatch from `__main__.py`:

```text
open-trader prediction-solver-benchmark quick
open-trader prediction-solver-benchmark full --environment macos
open-trader prediction-solver-benchmark full --environment linux
open-trader prediction-solver-benchmark report
open-trader prediction-solver-benchmark verify-report
open-trader prediction-solver-benchmark import-approved
```

Add Make targets `prediction-solver-envs`, `prediction-solver-quick`,
`prediction-solver-full-macos`, `prediction-solver-full-linux`,
`prediction-solver-report`, and `prediction-solver-verify-report` using the
existing `PYTHON_BIN` pattern. Full targets are manual and must not run under
ordinary `make test`.

- [ ] **Step 5: Run quick benchmark twice and verify determinism**

```bash
make prediction-solver-quick
make prediction-solver-quick
```

Expected: all three adapters pass the 16 canonical and semantic quick cases,
failure campaign passes, canonical statuses/portfolios/bounds match across both
runs, and only timing/RSS fields differ.

- [ ] **Step 6: Run focused tests and commit Task 9**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_solver.py tests/test_prediction_solver_backends.py tests/test_prediction_solver_worker.py tests/test_prediction_solver_benchmark.py
git add Makefile src/open_trader/__main__.py src/open_trader/prediction_solver_benchmark.py tests/test_prediction_solver_benchmark.py
git commit -m "feat: run and rank solver benchmarks"
```

---

### Task 10: Run both environments, freeze evidence, and hand one decision to #50

**Files:**
- Modify: `benchmarks/prediction_solver/corpus/approved_v1.json`
- Add: `benchmarks/prediction_solver/results/issue49/macos.jsonl`
- Add: `benchmarks/prediction_solver/results/issue49/linux.jsonl`
- Add: `benchmarks/prediction_solver/results/issue49/summary.json`
- Add: `benchmarks/prediction_solver/results/issue49/environment_manifest.json`
- Add: `benchmarks/prediction_solver/results/issue49/production_envelope.json`
- Add: `benchmarks/prediction_solver/results/issue49/report.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Satisfy the approved-real-input gate without inventing data**

Place one upstream-produced, user-approved, complete #48 approval envelope at
`benchmarks/prediction_solver/inbox/approved_component.json`, run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader prediction-solver-benchmark import-approved
```

Review the anonymized canonical problem, provenance, source/anonymized
fingerprints, and retained input gaps. If no complete approved envelope exists,
stop with `BLOCKED_REAL_CORPUS_EMPTY`; framework completion is not solver
selection and Issue #49 must remain open.

- [ ] **Step 2: Build/reuse environments and run the quick gate**

```bash
make prediction-solver-envs
make prediction-solver-quick
```

Expected: PASS with exact solver/environment versions and zero surviving worker
processes after the failure campaign.

- [ ] **Step 3: Run the full macOS benchmark**

```bash
make prediction-solver-full-macos
```

Expected: five warmups plus 30 measurements per required case/solver, 30 cold
and 30 rebuild probes, worker counts 1/2/4, and a complete `macos.jsonl`. No VPS
claim is recorded.

- [ ] **Step 4: Run the full Linux Docker benchmark**

```bash
make prediction-solver-full-linux
```

Expected: the same manifest/order/sample counts in `linux.jsonl`, plus SCIP
exact/VIPR generation and independent-check measurements.

- [ ] **Step 5: Generate and replay the decision**

```bash
make prediction-solver-report
make prediction-solver-verify-report
```

Expected: byte-identical replay and exactly one selected survivor/proof policy,
or `NO_SURVIVOR`. The production envelope states every measured model dimension,
worker/limit, latency/memory/throughput value, supported proof level, and each
limit that maps to `UNKNOWN`. It explicitly delegates actual VPS calibration to
#50/#52.

- [ ] **Step 6: Run final automated verification**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_n_leg.py tests/test_prediction_n_leg_oracle.py tests/test_prediction_solver.py tests/test_prediction_solver_backends.py tests/test_prediction_solver_worker.py tests/test_prediction_solver_benchmark.py
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m compileall -q src/open_trader
git diff --check
```

Record exact output. Do not run `make acceptance`; no Dashboard/runtime path is
in scope.

- [ ] **Step 7: Update changelog before any merge**

Add a dated operator-facing entry naming the selected solver/proof policy or
explicit no-survivor result, hard eliminations, corpus/sample counts, macOS and
Linux environment IDs, p95/worst/RSS evidence, certificate policy, full test
output, and the facts that no production service/order path changed and no VPS
was used.

- [ ] **Step 8: Commit final evidence**

```bash
git add CHANGELOG.md benchmarks/prediction_solver/corpus/approved_v1.json benchmarks/prediction_solver/results/issue49
git commit -m "benchmark: select N-leg solver stack"
```

If the honest result is blocked or no survivor, use:

```bash
git commit -m "benchmark: record no N-leg solver selection"
```

Do not merge #49 until the user reviews and confirms the generated recommendation.

## Final review checklist

- [ ] `prediction_n_leg.py`, `prediction_n_leg_oracle.py`, and the 16-case
  fixture are byte-unchanged from the starting SHA.
- [ ] Ordinary `pyproject.toml` contains none of the three solver packages.
- [ ] All three adapters execute the identical shared compiler/constraint loop.
- [ ] Every native value is checked with exact parent integer arithmetic.
- [ ] Positive/negative/optimal claims never outrun their independent evidence.
- [ ] No solver-only or VIPR record is serialized as a #48 v1 `PayoutProof`.
- [ ] Failure tests prove request finality, PGID cleanup, and next-worker recovery.
- [ ] Quick data cannot produce `SELECTED`.
- [ ] macOS and Linux percentiles remain separate.
- [ ] At least one approved canonical real snapshot exists, or the result is
  explicitly `BLOCKED_REAL_CORPUS_EMPTY`.
- [ ] Report replay is byte-identical to committed summary/envelope/Markdown.
- [ ] Exactly one recommendation/proof policy or an explicit no-selection result
  is present; the user has not yet been bypassed for #50 integration.
- [ ] `CHANGELOG.md` is committed before merge.

## Primary implementation references

- [HiGHS source and Python interface](https://github.com/ERGO-Code/HiGHS)
- [PySCIPOpt installation](https://pyscipopt.readthedocs.io/en/stable/install.html)
- [OR-Tools Python installation](https://developers.google.com/optimization/install/python)
- [OR-Tools CP-SAT solve statuses](https://developers.google.com/optimization/cp/cp_solver)
- [SCIP exact solving and certificate parameters](https://www.scipopt.org/doc-10.0.0/html/EXACT.php)
- [VIPR checker source and format](https://github.com/scipopt/vipr)
