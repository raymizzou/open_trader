# Task 5 report: reserve unsettled cross principal atomically

## Implementation

- Added `cross_execution_reservations` with one durable reservation per execution,
  fixed-point decimal text amounts, reservation state, and release audit fields.
- Cross-venue confirmation now uses the store's existing `BEGIN IMMEDIATE`
  transaction: it first returns an execution already linked to the preview,
  totals currently reserved principal, rejects a total above the existing 100
  principal limit with `cross_unsettled_cap`, then writes the execution and its
  reservation before commit.
- Added `cross_unsettled_principal()` and idempotent
  `release_cross_reservation(execution_id, reason=...)`. Release reasons are
  limited to `no_submit`, `both_rejected`, and `redeemed`; other states remain
  reserved.
- Legacy preview/execution payloads remain unchanged and do not create a cross
  reservation. No credentials, venue payloads, live requests, or orders are
  persisted or invoked.

## Tests

Focused required command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -k 'cross or preview' -q
```

Output: `8 passed, 31 deselected in 0.10s`.

Required full store command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Output: `39 passed in 0.46s`.

The test coverage exercises two store instances against one SQLite file, the
90-plus-20 cap race, rollback on cap rejection, reservation/execution
co-commit, preview and idempotency-key replay, allowed release idempotency,
risk-state retention, and legacy payload compatibility.

## TDD and takeover note

At takeover, the named source and tests were already committed as
`547f897 feat: reserve cross venue unsettled principal`; the worktree had no
uncommitted test edits. Consequently the brief's expected pre-implementation
RED run could not be reproduced without discarding the inherited implementation,
which was not authorized. The focused command above verifies the inherited
tests against the committed implementation.

## Self-review

- Reviewed the Task 5 diff against the brief and the existing transaction,
  idempotency, schema-version, and payload-sanitization paths.
- `git show --check` and `git diff --check` passed.
- The cap query and reservation insert are in the same `BEGIN IMMEDIATE`
  transaction as execution creation; cap failure rolls the transaction back.
- No generic ledger, new dependency, live integration, secret storage, or
  venue response storage was introduced.

## Concerns

- None for Task 5 scope. Lifecycle proof for `no_submit`, conclusive rejection,
  and observed redemption is intentionally enforced by the later execution and
  reconciliation task; this store task only exposes the narrow idempotent
  release primitive required by that workflow.

## Commit

- `547f897 feat: reserve cross venue unsettled principal`

## Fix round 1: fail-closed release proof and admitting cap race

### Findings addressed

- `release_cross_reservation` now reads the reservation, execution state, and
  durable evidence in its existing immediate transaction before changing a
  reserved row. A released row remains an idempotent no-op.
- `no_submit` requires terminal `both_rejected`, `submitted: false`, and
  numeric zero positions for both `predict.fun` and `polymarket`.
- `both_rejected` requires terminal `both_rejected`, numeric zero positions
  for both venues, and `no_position_observed: true`.
- `redeemed` requires terminal `complete`, numeric zero positions for both
  venues, `redemption.observed: true`, and a finite positive value in the
  structured `redemption.redeemed_collateral` mapping.
- Free-form strings such as `"proven_zero"` and `"observed"` are rejected;
  failed proof leaves the reservation untouched.
- The two-store race now starts at 80 principal and races two 20-cost
  confirmations. Exactly one commits; the other observes the committed row and
  fails with `cross_unsettled_cap`, leaving principal at 100.

### RED

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -k 'cross or preview' -q
```

Output before the fix: `1 failed, 9 passed, 31 deselected in 0.22s`.
The failure showed a `validating` execution could release its reservation by
passing `reason="redeemed"` without any terminal state or structured proof.

### GREEN

Focused command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -k 'cross or preview' -q
```

Output: `10 passed, 31 deselected in 0.15s`.

Required full store command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Output: `41 passed in 0.31s`.

### Self-review

- Verified each allowed reason against execution state and structured evidence;
  all other state/evidence combinations raise before the update.
- Repeated approved release remains a no-op, while invalid proof rolls back and
  retains capacity.
- Reviewed the 80-plus-two-20 race: the existing `BEGIN IMMEDIATE` transaction
  serializes the sum-and-insert, so the second store instance reads 100 and
  cannot create a third reservation.
- `git diff --check` passed. No credentials, venue payloads, live requests, or
  order paths were added or invoked.

### Concerns

- The structured evidence keys documented above are now the narrow contract
  the later execution/reconciliation task must emit before releasing capacity.
- The worktree has no `ruff` executable; the required focused and full store
  test suites passed.
