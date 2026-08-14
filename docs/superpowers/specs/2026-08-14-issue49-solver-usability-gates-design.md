# Issue #49 solver usability gate simplification

## Goal

Issue #49 is a bounded investigation of whether HiGHS, SCIP, and CP-SAT are usable for the N-leg solver workload. The benchmark must complete with an honest result even when one solver is unavailable or unsafe. Orchestration version bookkeeping must not invalidate otherwise comparable measurements.

## Evidence identity

Git SHA and environment ID remain recorded audit fields. They are not cross-environment or current-worktree eligibility gates.

Each environment's records must remain internally consistent with that environment's manifest evidence. macOS and Linux may have different producer Git SHAs and environment IDs when all comparison-relevant inputs match:

- canonical corpus and case/request/problem fingerprints;
- benchmark profile, limits, sample plan, worker counts, and record schema;
- solver, adapter, Python, immutable image, and build versions;
- license/source evidence and proof policy.

The macOS handoff reader must compare these core fields and must not rebuild Git-SHA-derived identity from the current checkout. Existing valid macOS evidence is reusable after an orchestration-only commit.

## Worker transport identity

The handshake PID remains required to be a valid positive diagnostic value, but it is not required to equal the direct `Popen` PID. That equality is invalid for wrapper transports: the direct host process is the Docker CLI while the worker runs in a container PID namespace.

Protocol version, backend name, worker version, immutable image ID, validated container ID, and cleanup proof remain strict. Process-group cleanup continues to use the direct host process PID/PGID, not the handshake PID.

Before any further full Linux execution, each immutable solver image must pass one real Docker request smoke covering startup handshake, one solve response, CID capture, container exit, and strict absence proof.

## Failure semantics

A solver-specific failure is a benchmark finding, not a global orchestration failure. With cleanup proven, the benchmark records and hard-eliminates the affected solver for:

- `CRASH` or exit 137;
- `MEMORY_LIMIT`;
- `INVALID_OUTPUT`;
- `PROTOCOL_MISMATCH`;
- an independent checker hard failure.

Other solvers continue. The final result may truthfully be `SELECTED`, `NO_SURVIVOR`, or `NO_DECISIVE_WINNER`. Soft and hard timeouts remain measured `UNKNOWN` outcomes under the existing contract.

After the first fatal or hard-check result with proven cleanup, no more samples are scheduled for that solver/environment. The existing fatal record is retained; missing later cells are accepted only for that hard-eliminated solver/environment. No synthetic success, failure, or timing samples are generated. Surviving solver/environment cells must still have the complete frozen sample matrix, and the report must distinguish planned from observed record counts.

The whole benchmark still stops without publication when:

- worker or container cleanup is unproven;
- corpus, problem, sample matrix, schema, or evidence files are invalid;
- immutable solver/image/build identity or comparison-relevant inputs drift;
- publication would overwrite or conflict with existing evidence.

## Scope

Tracked implementation scope is limited to:

- `src/open_trader/prediction_solver_worker.py`
- `tests/test_prediction_solver_worker.py`
- `src/open_trader/prediction_solver_benchmark.py`
- `tests/test_prediction_solver_benchmark.py`

No semantic fingerprint framework, protocol relay, new schema, retry framework, solver math change, Dockerfile/dependency change, production integration, or Issue #50 work is included.

## Verification and continuation

Implementation is test-first. It must prove:

1. native and Docker-wrapped workers accept valid handshakes without relying on cross-namespace PID equality;
2. malformed protocol/backend/version/PID values still fail closed;
3. the current macOS package is accepted despite an orchestration-only Git SHA change while core identity mutations are rejected;
4. solver-specific fatal/check failures eliminate only that solver and do not prevent remaining solver measurements;
5. cleanup-unproven and corrupted evidence still stop the whole run;
6. each immutable Docker image completes the one-request smoke with no survivor.

After focused and full regression verification, an independent reviewer must return Spec PASS and Quality PASS before benchmark execution resumes. Existing macOS evidence must remain byte-identical. Linux full may run only after all three Docker request smokes pass.
