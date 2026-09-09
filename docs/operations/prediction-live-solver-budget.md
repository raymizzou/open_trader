# Prediction live solver budget

Updated 2026-09-09 for issue #115.

The bounded live oracle budget is `(max_quantity_vectors=16,
max_joint_states=25, max_support_rechecks=1)`. This covers the current
two-contract IMPLIES shape (16 quantity vectors and 9 raw states) and the
two-endpoint native complement shape (4 quantity vectors and 25 raw states,
23 allowed after relation pruning). These vector counts assume each action
quantity is binary (`0` or `1`); larger per-action domains may exceed the
budget even for smaller component shapes. It only removes those budget-induced
`UNKNOWN` results; it does not establish that a qualified opportunity exists.

The quantity-vector budget limits exact negative proof enumeration. Constraint
generation may still produce a candidate before that check, and candidate
verification independently applies the raw-state and support-recheck limits.
The CP-SAT time/memory/round limits remain the live defaults: 1,000 ms soft,
2,000 ms hard, 1 GiB, and 3 rounds. The budget is not an order-leg cap.

Complete larger components remain fail-closed: the expanded IMPLIES chain has
two directional actions per contract (10 actions at n=5 and 12 at n=6),
1,024 and 4,096 quantity vectors, and returns
`UNKNOWN / ORACLE_DECISION_LIMIT_EXCEEDED`. The implementation does not
truncate, split, or bypass full component proofs. Fees, release semantics,
execution authorization, AUTO mode, discovery scheduling, and production data
are outside this change.

Measurement provenance: diagnostic code SHA
`005458ea90cde0b070b3b1f2d33b60dd31cca7d2`, offline Python 3.12 with
OR-Tools 9.15.6755, using the real `WorkerHarness` and CP-SAT backend. These
measurements are diagnostic only, not a production latency or profitability
guarantee. For the two-contract four-action synthetic chain, the first cold
solve was 421.60 ms and verification 19.74 ms; two warm solves were
75.35–75.46 ms and independent negative proofs were 18.03–18.16 ms. The
n=5/n=6 diagnostic runs deliberately used each full-family enumeration budget:
n=5 used 1,024 quantity vectors and 243 raw states, while n=6 used 4,096
quantity vectors and 729 raw states. They returned no CP-SAT candidate in
444.55/471.50 ms, while the independent negative proof did not finish within
an external 20-second diagnostic limit. The shipped `(16, 25, 1)` bound rejects
those larger shapes before that enumeration, so the >19-second observation is
not a runtime claim about the current bounded setting; because the limit
includes process startup and solve, it supports only a proof-time lower bound,
not an exact verification duration.

The four budget regressions, the full daily premarket module, and the three
launchd-uninstaller regressions pass together in Docker (`173 passed in
41.63s`) on the working tree based on `75c4cf7b0b7523f1a1b3b585cb8195619b77a50c`.
The daily premarket tests restore the process environment after each case and
declare their own dummy `DEEPSEEK_API_KEY` where the summary client is part of
the intended path. The required full Docker `make test` then passed with
8,125 passed, 5 skipped, 9 deselected, and 1 warning in 983.94s; Candidate
Acceptance was not run.
