# Issue #49 Open-Source Solver Benchmark Design

## Status

- Date: 2026-08-12
- State: discussion approved; written review pending
- Issue: GitHub #49, `[预测套利 12] 基准验证 HiGHS、SCIP 与 OR-Tools CP-SAT`
- Depends on: GitHub #48 canonical N-leg model and exact Oracle
- Hands off to: GitHub #50 production solver/verifier engine and GitHub #52 runtime worker sizing

## Goal

Run HiGHS, SCIP, and OR-Tools CP-SAT against the same complete #48
`ArbitrageProblem`, the same constraint-generation algorithm, and the same
versioned corpus. Eliminate any unsafe or operationally unusable candidate,
then recommend one open-source solver and one proof level for production.

The benchmark answers four separate questions:

1. Can the solver produce a connected integer portfolio whose fixed worst-case
   payout and all qualification constraints are proved?
2. Can it continue from that first qualified portfolio to a globally optimal
   result without confusing a feasible incumbent with a closed optimum?
3. Can its positive, negative, timeout, crash, and certificate claims be checked
   independently and mapped to the #48 result semantics without false safety?
4. Is its Python 3.12 installation, worker isolation, latency, memory, and
   licensing simple enough to operate on macOS and Linux?

## Scope boundary

Issue #49 is an isolated benchmark and selection ticket. It may add benchmark
adapters, worker harnesses, fixtures, failure injection, measurements, and a
selection report. Nothing in this ticket is connected to the Prediction
Service, HTTP request handling, live monitors, a production database, live
books, balances, credentials, or order submission.

Issue #50 will consume the selected solver and proof level and implement the
production `SOLVER_VERIFIED` engine and independent verifier. Issue #49 must not
pre-implement that runtime or make all three solver dependencies permanent
application dependencies.

The benchmark does not use a real VPS. It uses native macOS and Linux Docker:

- Linux Docker proves installation, protocol, failure isolation, and Linux
  behavior.
- macOS and Linux measurements are reported separately; they are never merged
  into one percentile.
- Container timing is not described as target-VPS capacity. #50/#52 must
  calibrate the selected solver on the actual production host before setting
  final runtime concurrency and latency limits.

## Reused canonical contract

The benchmark reuses #48 without creating a second mathematical model:

- Input: `ArbitrageProblem` and its embedded qualification constraints.
- Search modes: admission, optimization, and optional raw-arbitrage diagnostic.
- Positive output: `PortfolioSolution` with `PayoutProof`.
- Negative output: the same discriminated `PayoutProof` for
  `NO_QUALIFIED_OPPORTUNITY` or raw `NO_ARBITRAGE` when independently proved.
- Status axes: solve, proof, business, and optimality status remain separate.
- Optimization output: `ObjectiveBounds`, including an honest open or closed
  gap.

Business code and benchmark cases never construct a vendor model. Vendor
variables, constraints, statuses, logs, and native handles stay inside the
corresponding adapter process.

## External benchmark seam

The parent harness exposes one logical operation:

```text
solve(canonical_problem, mode, limits) -> solver_run
```

`canonical_problem` is a serialized #48 `ArbitrageProblem`. `mode` is one of
admission, optimization, or raw diagnostic. `limits` contains only benchmark
execution budgets such as wall time, memory, and constraint-generation rounds;
it does not contain business thresholds or vendor tuning hidden from the
canonical problem.

`solver_run` is a benchmark envelope around the canonical result. It contains:

- the #48 status axes;
- an optional canonical `PortfolioSolution`/`PayoutProof`;
- objective bounds and gap;
- master/adversary round count, generated counterexamples, and closure state;
- phase timings, peak memory, termination reason, worker identity, and solver
  diagnostics;
- solver, adapter, protocol, platform, and corpus versions.

The envelope does not become a second production proof type. Canonical proof
objects remain the only mathematical payloads.

## Worker architecture and protocol

Each solver runs in its own long-lived subprocess and isolated dependency
environment. The parent harness communicates through a versioned line-delimited
JSON protocol:

1. Startup handshake reports protocol, adapter, solver, Python, platform, and
   license metadata.
2. Each stdin line contains one canonical request.
3. Each stdout line contains exactly one matching unified `solver_run`.
4. Stderr is diagnostic-only and is captured separately.

The worker has no production database, network, credentials, exchange client,
or order-submission access. The parent owns the monotonic wall clock, process
group, hard timeout, memory observation, and result validation.

On a hang, crash, non-zero exit, invalid JSON, protocol mismatch, or hard
timeout, the parent terminates and reaps the whole worker process group. The
current request remains `UNKNOWN`; rebuilding a worker for the next request
must not retroactively turn that request into success. The harness checks for
orphan descendants and records rebuild time before continuing.

This worker boundary is required because an in-process native solver cannot be
reliably recovered after a native hang or crash. Starting a fresh container for
every solve is rejected because container startup would contaminate the hot
path timing being measured.

## Common constraint-generation algorithm

All three adapters run the same logical loop:

```text
master problem proposes an integer Portfolio
    -> adversary fixes that Portfolio and finds its worst allowed settlement
    -> a violating settlement becomes a canonical cut
    -> master resolves with accumulated cuts
    -> stop when the fixed Portfolio is fully proved or a limit is reached
```

The master chooses integer quantities directly from the #48 quantity domains;
it does not enumerate two-leg, three-leg, or arbitrary leg subsets. A valid
positive result must retain only one connected support under the canonical
relationship graph.

The same solver candidate handles the master and adversary phases so the
benchmark compares complete candidate stacks, not a favorable mixture of
vendors. Cuts are represented canonically so round counts and counterexamples
can be replayed and compared.

Admission may stop as soon as one fixed portfolio has all of the following:

- canonical model feasibility;
- a closed fixed-portfolio worst-state proof;
- every embedded qualification constraint passing;
- connected selected support;
- successful independent benchmark checking where the case is inside the #48
  Oracle budget.

That point records the adapter's first-qualified timestamp. Truth-set cases also
record when the independent Oracle check finishes. Global optimization may
continue with remaining budget. It must not delay or invalidate the already
proved candidate merely because the global objective gap remains open.

## Result and proof semantics

The benchmark preserves the distinctions established by #48:

- `QUALIFIED_FEASIBLE`: a fixed portfolio is safe and qualified. Global
  optimality is not required.
- `OPTIMAL`: the canonical global objective and tie-break are proved and the
  lower and upper bounds are closed.
- `NO_QUALIFIED_OPPORTUNITY`: the complete qualified search is independently
  proved infeasible by the exact Oracle or a supported independently checked
  infeasibility certificate.
- `NO_ARBITRAGE`: only the explicit raw diagnostic, after removing business
  qualification constraints, is independently proved to have no positive
  guaranteed-profit portfolio. It is a benchmark/diagnostic mode, not a second
  solve in the future production admission hot path.
- `UNKNOWN`: timeout, crash, malformed or incomplete input, numeric failure,
  unclosed fixed-portfolio proof, unclosed infeasibility claim, unsupported
  certificate, or any other state that cannot be checked safely.

A solver's own `INFEASIBLE` or `OPTIMAL` label is evidence to inspect, not an
independent proof. Repeating the same solver or asking a second solver for the
same label also does not create an independent negative proof.

For controlled small cases, every returned positive portfolio is handed back to
the #48 Oracle as fixed actions and integer quantities. The Oracle verifies the
worst payout, conservative cost and release time, qualification facts, and
connected support. If several terminal scenarios attain the same worst payout,
the exact scenario identity need not match; the returned scenario must attain
the same proved lower bound. Admission mode may return any fully checked
qualified portfolio. Optimization mode must match the Oracle's canonical
objective and tie-break.

For controlled negative cases, `NO_QUALIFIED_OPPORTUNITY`, raw
`NO_ARBITRAGE`, bounds, gaps, and `OPTIMAL` claims must match the exact Oracle.
Any false-safe positive, false negative, or false optimality claim eliminates
the solver candidate.

For cases beyond the Oracle budget, a negative result is accepted only when the
solver emits an infeasibility certificate that a separately versioned checker
can validate from the canonical problem. Without such a certificate the result
is `UNKNOWN`. Large-case performance results without an independent truth path
are marked as benchmark measurements, not production-safe proof.

SCIP/VIPR and any other actually available formal-certificate path are measured
separately for generation time, certificate size, checker time, checker memory,
and failure diagnostics. The final report selects one explicit production proof
policy and applies it identically to Observe, Manual, and Auto. The policy may
require different evidence for positive and negative result kinds, but those
rules are fixed up front and cannot vary opportunistically per request.

## Versioned benchmark corpus

The corpus has three layers.

### #48 canonical fixtures

All 16 pinned #48 Oracle cases are decoded from their serialized form and run
unchanged through every candidate. They remain the first regression truth set.

### Deterministic synthetic cases

A bounded, versioned set varies the dimensions that matter to the production
envelope without creating an uncontrolled Cartesian product:

- contract and candidate-action counts;
- relation/constraint counts and connectivity;
- terminal atoms and exceptional per-contract terminal states;
- selected leg count N;
- integer lot minima, maxima, and step sizes;
- piecewise executable costs;
- conservative rounding and signed-integer boundaries;
- valid relations, contradictory constraints, and incomplete/unknown input;
- qualified feasible, globally optimal, no-qualified, raw no-arbitrage, and
  forced-unknown outcomes.

The generator seed, case manifest, canonical problem fingerprint, expected
truth method, and Oracle budget are frozen with the corpus.

### Approved real-component snapshots

The real layer consists only of anonymized, frozen canonical
`ArbitrageProblem` snapshots derived read-only from approved relationship-graph
components. Every snapshot records a stable source alias, approval/generation
provenance, canonical fingerprint, and capture time, but no account secret,
credential, live venue identifier that is not needed by the model, or order
book connection.

Snapshots are deduplicated by canonical fingerprint. Benchmark execution never
reads a production database or live network. A production component that cannot
be converted to the #48 contract is recorded as a named input-coverage gap; it
is not silently discarded from the report.

## Run tiers and reusable environments

There are only two run tiers:

- Quick: correctness, protocol, and focused failure tests suitable for local
  development and CI. Quick results cannot select the production solver.
- Full: manually invoked reproducible performance, concurrency, failure, and
  certificate benchmark that produces the selection report.

Installation is not part of each run. The project maintains three pinned local
macOS virtual environments and three pinned Linux Docker images, one per
solver. They are built once and reused. CI uses cached or prebuilt equivalents.
An environment rebuild occurs only when its dependency manifest, solver
version, adapter/protocol version, Python version, or target platform changes.

The three solver packages remain isolated benchmark dependencies. The main
application dependency set does not gain all three candidates.

## Measurement protocol

For each warm benchmark case and solver/environment pair, the full tier runs
five unreported warmups followed by 30 measured repetitions. A dedicated probe
uses 30 measured repetitions for cold worker startup and 30 for forced worker
rebuild so p95 and worst values have a consistent sample basis.

The report includes:

- solver `time_to_first_qualified`, from accepted request until the adapter
  closes the fixed candidate's own proof;
- checked `time_to_first_qualified`, including the independent Oracle check on
  truth-set cases where that check is available;
- end-to-end `time_to_optimal` when global bounds close;
- canonical decode/model-build, master solve, adversary solve, independent
  check, and serialization time;
- master/adversary rounds, generated counterexamples, and total closure time;
- p50, p95, and worst latency;
- peak worker-process-group RSS and aggregate RSS under concurrency;
- cold-start and post-failure rebuild time;
- per-worker throughput and bounded worker counts 1, 2, and 4;
- repeated-input semantic determinism, timeouts, termination reasons, and
  diagnostic quality.

Performance runs use the same pinned case order and manifest. Timing variance
may differ, but repeated runs of the same solver and input must not change the
canonical status, portfolio, proof facts, bounds, or termination classification
unless the run hits an explicitly recorded resource limit.

Every run appends one JSONL record containing the case and problem fingerprints,
Git SHA, CPU/architecture, OS/container identity, solver/environment versions,
cold or warm classification, phase timings, canonical status, checker result,
memory, termination reason, and worker identity. Machine-readable summaries and
the human-readable Markdown report are generated from that JSONL; statistics
are not copied by hand.

## Failure campaign

Each adapter is tested with deterministic injections for:

- cooperative solver timeout;
- parent-enforced hard timeout/hang;
- worker crash and non-zero exit;
- malformed or truncated output;
- protocol-version mismatch;
- memory-limit termination where the platform supports enforcement;
- certificate absence, corruption, and checker failure.

The observed result must be `UNKNOWN`, the current request must not be retried
into a success classification, the worker process group must be gone, and a
fresh worker must complete the next known-good request. Repeated failure runs
must show bounded worker count and no monotonically growing orphan memory.

## Candidate elimination and selection

A solver is eliminated immediately by any of the following:

- a false-safe portfolio;
- a false `NO_QUALIFIED_OPPORTUNITY`, `NO_ARBITRAGE`, or `OPTIMAL` claim;
- mapping a timeout, crash, or unclosed proof to anything stronger than
  `UNKNOWN`;
- semantic nondeterminism for the same canonical input and limits;
- an orphaned worker or unbounded process/memory growth in the failure campaign;
- failure to install and run on Python 3.12 macOS or Linux;
- a production license that is not open source or requires a commercial key or
  commercial runtime authorization.

Survivors are compared in this fixed order rather than by an opaque weighted
score:

1. `time_to_first_qualified` p95 and worst latency;
2. memory, throughput, bounded-worker behavior, and rebuild cost;
3. installation and operational simplicity;
4. `time_to_optimal` and bound quality;
5. formal certificate generation and independent-check cost.

The report recommends exactly one survivor and one proof level, explains the
decisive evidence, and records why the other two were rejected. If no candidate
survives the hard gates, the honest result is no selection and #50 remains
blocked; the benchmark must not choose the least unsafe candidate.

A solver without independently checkable large-model infeasibility certificates
may still win. Its production contract must then return `UNKNOWN` for large
negative cases rather than claiming `NO_QUALIFIED_OPPORTUNITY`.

The user confirms the recommendation before #50 integrates it. Long-term
production keeps only the selected dependency, not all three benchmark stacks.

## Production envelope handoff

The selection report freezes a versioned measured envelope for the selected
stack. It records, per environment:

- contracts and candidate actions;
- relationship/constraint count;
- terminal atoms and relevant joint-state pressure;
- quantity-domain and cost-slice dimensions;
- constraint-generation rounds and counterexamples;
- worker count;
- wall-time and memory limits;
- observed `time_to_first_qualified`, `time_to_optimal`, throughput, and rebuild
  behavior;
- supported positive, negative, and formal-certificate proof levels;
- every limit that maps to `UNKNOWN`.

The envelope is derived from the measured safe corpus; it does not guess a
permanent maximum N. #50 may operate only inside the selected, checkable
envelope. #52 may tighten concurrency and latency after actual-host calibration
but cannot loosen proof semantics or bypass a correctness gate.

The Oracle remains a small-model truth source, Shadow comparator, and
budget-bounded negative verifier. The handoff must not turn it into a positive
fallback when the selected production solver times out.

## Deliverables and completion evidence

Issue #49 is complete only when the repository contains:

- one thin canonical adapter implementation for each candidate solver;
- one reusable parent worker harness and versioned protocol;
- the three-layer frozen corpus and expected truth metadata;
- quick correctness/failure tests;
- a reproducible full benchmark entry point;
- raw JSONL results and generated machine-readable summary;
- a generated Markdown comparison report;
- one explicit solver/proof-level recommendation or an explicit no-survivor
  result;
- pinned installation and license evidence for Python 3.12 macOS and Linux;
- the measured production-envelope handoff for #50/#52.

Final verification includes the quick suite, a complete full benchmark on both
declared environments, replay of the generated report from raw JSONL, worker
failure cleanup checks, and `git diff --check`. Dashboard acceptance and live
service deployment do not apply because this ticket changes no Dashboard or
production runtime behavior.

## Out of scope

- Production solver or verifier integration.
- Prediction Service scheduling, HTTP handlers, monitoring, or order execution.
- Live order books, balances, accounts, credentials, and venue connectivity.
- Real VPS capacity claims or final production worker tuning.
- A permanent abstraction supporting arbitrary future solver vendors.
- Keeping three long-term solver dependencies after selection.
- Changing #48 canonical mathematics, qualification rules, or exact Oracle
  semantics merely to favor a solver candidate.
