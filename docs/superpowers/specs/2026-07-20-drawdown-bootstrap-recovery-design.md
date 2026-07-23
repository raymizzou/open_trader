# Drawdown Bootstrap and Recovery Design

## Goal

Restore v4 entry eligibility without weakening the existing fail-closed drawdown
discipline. Deployment may create a baseline only for a provably new strategy
identity. Report generation remains observation-only.

## Scope

- Add a deployment preflight for CN, HK, and US Futu simulation accounts.
- Distinguish first activation, a new strategy version, state loss, state
  corruption, and an in-place parameter change.
- Persist automatic-bootstrap audit data and immutable hashed state snapshots.
- Recover only from a valid state snapshot.
- Project bootstrap and blocking details through the existing Dashboard drawdown
  section and existing notification channels.
- Extend the final Dashboard acceptance gate and live deployment verification.

External cash-flow adjustment and new Dashboard pages are out of scope. Until a
separate cash-flow design ships, unexplained account changes remain an operational
fail-closed condition.

## Chosen Approach

Add a dedicated `trend-drawdown-preflight` command and call it from the required
acceptance/deployment workflow. This keeps the mutation outside
`trend-a-share-report` and `trend-market-report`, which continue to call only
`observe_strategy_equity`.

Rejected alternatives:

- Initializing inside report generation violates the report-path safety boundary
  and makes an ordinary retry capable of changing the risk baseline.
- Reusing `trend-drawdown-unlock` gives a first deployment the false audit meaning
  of a human override after a sticky pause.

## Strategy Identity

The state key remains `(market, strategy_id, strategy_version)`. Each record also
stores a SHA-256 parameter hash computed from the canonical JSON representation of
the complete `strategy_snapshot.parameters` object. This intentionally treats any
parameter change as trading-significant; maintaining a second hand-curated list of
significant fields would create a drift-prone identity system.

Preflight applies these rules under the existing state lock:

1. Existing key and matching parameter hash: leave the record and original
   bootstrap event unchanged.
2. Existing key and different parameter hash: fail; the strategy version must be
   incremented.
3. Missing key in a valid state file: create one automatic baseline for the new
   market/version key.
4. Rollback to an old version: load and continue its existing record.

The accepted Git SHA is audit context, not identity. A new SHA with the same
parameter hash does not reset the high-water mark.

## First Activation and State-Loss Detection

Frozen report JSON files are the immutable evidence that the drawdown discipline
has run before. Preflight scans the existing CN, HK, and US report directories:

- No report for any market has `drawdown_summary.state_status == "ok"`, and the
  shared state file is absent: first activation is allowed.
- Any historical report has `state_status == "ok"`, and the shared state file is
  absent: state loss; restore from a valid snapshot or fail.
- The state file is malformed or fails validation: restore from a valid snapshot
  or fail without overwriting the bad file.

Dashboard projections are not used to reconstruct state.

## Baseline Source and Timing

Each market is processed independently. Preflight resolves that market's most
recent completed trading date, loads the configured Futu simulation account used
by formal reports, and freezes its net value with that source date. If a current
missing-state v4 report already contains the same market, strategy identity, and
completed-date account snapshot, preflight may reuse that frozen snapshot instead
of relabeling a later live value.

The automatic audit event records:

- event type `automatic_bootstrap` and a deterministic event ID;
- market, strategy ID, strategy version, and parameter hash;
- baseline equity and account snapshot source date;
- accepted Git SHA, actor/deployment identity, occurrence time, and reason
  (`first_activation` or `new_strategy_version`).

CN and HK initialization must finish before 09:30 local market time. US
initialization must finish before its regular trading session begins. If the entry
window has begun, preflight may record the baseline for future reports but marks
the current execution date ineligible for regenerated BUY actions. It never
rewrites an existing report; a permitted rerun creates the next revision and uses
the existing report delivery path.

## State and Snapshots

The drawdown state schema is extended with bootstrap identity fields and an
explicit automatic-bootstrap audit event. Manual unlock keeps its existing
meaning and preserves Kelly samples.

Every successful state write first validates the complete payload, then writes the
normal atomic `state.json`, and also creates an immutable snapshot envelope under
`data/trend_drawdown/snapshots/`. The envelope contains the canonical state bytes
and their SHA-256 digest. Existing snapshot paths are never overwritten.

On recovery, preflight searches newest-first and accepts only a snapshot whose
digest and state schema both validate. It restores the exact records, high-water
marks, sticky pause flags, and audit events. If none validate, the market remains
blocked and the state file is not synthesized from current equity.

Normal observation stays fail-closed for absent or corrupt state. Sell, hold, and
protection-line watcher paths do not depend on entry eligibility and continue to
run.

## Dashboard and Alerts

The existing drawdown area displays current simulation equity, high-water mark,
drawdown, and the 5% limit. On the bootstrap date it also displays that the
baseline was automatically established, with baseline equity and source date.
Git SHA, parameter hash, actor, event identity, and recovery details appear in the
existing audit detail.

Missing, corrupt, parameter-mismatch, and recovery-failed states use the existing
notifier at high priority. A small persistent ledger deduplicates by
`(market, strategy_version, failure_status)`. Successful recovery clears that
active key so a later recurrence can alert again. Alert failure does not unlock
entries or stop sell/protection processing.

## Command Result and Failure Semantics

`trend-drawdown-preflight` returns one structured result per market:

- `ready`: an existing matching state is retained;
- `bootstrapped`: a legal first/new-version baseline is recorded;
- `recovered`: a valid snapshot restored the shared state;
- `unavailable`: the market account or completed-date snapshot is unavailable;
- `failed`: loss, corruption, identity mismatch, invalid snapshot, or code error.

One unavailable market does not prevent other markets from being checked. The
overall preflight exits nonzero unless all three markets are ready,
bootstrapped, or recovered. Acceptance maps external account/browser
unavailability to `BLOCKED`; integrity and implementation failures remain `FAIL`.

## Verification

Implementation follows red-green-refactor at the state and CLI seams. Focused
tests cover:

- first activation, new version, rollback, and same-version idempotency;
- same-version parameter mismatch;
- historical-ok state loss and corrupt-state handling;
- hashed snapshot creation, recovery, and invalid-snapshot rejection;
- independent CN/HK/US outcomes and automatic audit contents;
- buy-window cutoff and frozen-report immutability;
- alert deduplication, recovery reset, and unaffected sell/watch paths;
- Dashboard projection and browser-visible bootstrap/blocking details.

After focused and full automated tests, run the real three-market preflight and
report workflows. Inspect `launchctl`, `screen`, process working directories,
PIDs, Git SHAs, and fresh logs. Run `make acceptance` exactly once as the final
gate. Only `PASS` permits redeploying the exact accepted SHA and presenting the
HTTP 200 review URL.
