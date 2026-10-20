# Permanent N-leg retirement in CI (#279)

N-leg business is permanently retired. Default CI uses `TEST_N_LEG=0` for every
branch push, PR candidate and merged-main push. Four backend services and
non-LIVE portable scenarios remain required. Production code, pause state,
tests and dependencies are retained. This policy adds no production action.

## Classification

The old filename wildcard matched 32 files at baseline
`407ef82913a2a6ed8b770ae10d17041c45210f1b`. Review retained these mixed/shared files:

| Active file | Shared contract |
| --- | --- |
| `tests/test_prediction_monitor_selection_driver.py` | Runtime empty-driver metrics boundary |
| `tests/test_prediction_n_leg_cutover.py` | Monotonic minimum-reader-generation fence |
| `tests/test_run_nleg_cutover.py` | Database/WAL recovery and production-owner protection |

Only the following 29 files are retired from default execution, each with reason
`N-leg permanently retired`. `scripts/ci_nleg_retired.json` is the explicit
manifest; the selector validates it against the reviewed allowlist. New files
remain active, including new files with an N-leg or solver prefix. Unreviewed,
duplicate, missing, unsafe or damaged entries fail before building tests.

- `tests/test_prediction_executable_cost.py`
- `tests/test_prediction_live_resolver.py`
- `tests/test_prediction_market_solution.py`
- `tests/test_prediction_monitor_selection.py`
- `tests/test_prediction_n_leg_canary_report.py`
- `tests/test_prediction_n_leg_confirm.py`
- `tests/test_prediction_n_leg_driver.py`
- `tests/test_prediction_n_leg_episodes.py`
- `tests/test_prediction_n_leg_execution.py`
- `tests/test_prediction_n_leg_fail_closed_e2e.py`
- `tests/test_prediction_n_leg_lineage_inheritance.py`
- `tests/test_prediction_n_leg_metrics.py`
- `tests/test_prediction_n_leg_mode.py`
- `tests/test_prediction_n_leg_oracle.py`
- `tests/test_prediction_n_leg_preflight.py`
- `tests/test_prediction_n_leg_read_model.py`
- `tests/test_prediction_n_leg_shadow.py`
- `tests/test_prediction_n_leg_terminal_check.py`
- `tests/test_prediction_n_leg_validation.py`
- `tests/test_prediction_n_leg_validation_books.py`
- `tests/test_prediction_partial_fill.py`
- `tests/test_prediction_snapshot_scheduler.py`
- `tests/test_prediction_solver.py`
- `tests/test_prediction_solver_backends.py`
- `tests/test_prediction_solver_benchmark.py`
- `tests/test_prediction_solver_server.py`
- `tests/test_prediction_solver_verified.py`
- `tests/test_prediction_solver_worker.py`
- `tests/test_run_nleg_no_submit_validation.py`

All `test_lp_*.py`, `test_polymarket_lp*.py`, `test_polymarket_trading.py` and
`test_prediction_n_leg.py` remain active. Store, runtime, service, cloud, launchd,
authentication, read-only and health coverage remains active. This includes the
six runtime/service pause guards and the newer paused-shadow runtime guards.
`test_prediction_n_leg_validation.py` stays at its original path and importable:
active observation-monitor tests import its fixture provider.

## Evidence and manual diagnosis

The complete collected backend universe equals the executed active collections
plus the declared retired collection. They must be disjoint. Collection errors,
gaps, overlaps, duplicates and undeclared retirement fail closed. Retired nodes
are labelled `not-executed-permanent-retirement`; they are never counted as passed.
The trusted deployment preflight verifies this policy and the actual executions
against same-SHA, current-attempt main-push artifacts. See [CI](ci.md) and
[Deployment Preflight](deployment-preflight.md) for unchanged identity boundaries.

Manual diagnosis can use `make test SERVICE=prediction TEST_N_LEG=1` or explicit
`make test TEST='tests/test_prediction_solver.py::test_name'`. These commands do
not enable production N-leg and do not produce default trusted CI evidence.
There is no periodic full N-leg run or restoration regression requirement.

JSON/JUnit artifacts retain selected nodeids, results, collection errors,
setup/call/teardown durations, worker identity, CPU model/provenance, architecture,
CPU count and wall seconds, with source,
run/attempt, policy, manifest, lock, dependency and image identities. The runner
keeps failed records and preserves the first failure status. Artifacts expire
after three days. Missing or inconsistent evidence cannot describe success. Successful records
require complete setup/call/teardown measurements (or the legitimate skip phases),
Python/platform/architecture and explicit CPU identity/count availability. Producer
and preflight share JUnit parsing: each testcase must match an exact execution
identity and phase-derived outcome, with finite nonnegative time. Empty, missing,
extra or duplicate cases fail closed. CPU
model reads use `/proc/cpuinfo` on Linux or read-only native `sysctl` on macOS.
Unavailable model reads are explicit unknown and cannot prove CPU comparability;
they do not change test outcomes.

## Timing validation

The 29/32 classification is not a timing estimate. A valid Prediction comparison
uses the same candidate source, Python 3.12.14/uv 0.12.19 lock, runner/CPU and two
workers, changing only retirement selection. Record both complete selections,
outcomes, phase times and wall time. Solver grouping, fault-iteration counts,
HTTP polling and business deadlines are outside this change.

Local focused checks establish routing and evidence behavior. They do not
establish GitHub exact-SHA CI, deployment readiness or health. Timing results and
any environment blocker must be reported with the actual run evidence.
