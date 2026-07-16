# Dashboard Acceptance Single-Refresh Design

## Goal

Remove the fixed 125-second acceptance delay while retaining a real account
refresh and a before/after stability check.

## Flow

`make acceptance` continues to run the full pytest suite first. The live gate
then:

1. reads `/api/dashboard` as the pre-refresh snapshot;
2. requests `/api/quotes` once, which invokes the existing Futu/Tiger account
   sync when due and refreshes live quotes;
3. reads `/api/dashboard` again as the post-refresh snapshot;
4. validates the quote payload, both Dashboard payloads, stable holdings and
   `reports_dir`, the running PID/cwd/Git SHA, logs, and the existing desktop
   and mobile Chrome flows.

The gate no longer waits for or validates a second quote refresh. The existing
`WAIT_SECONDS` and `--wait-seconds` controls are removed rather than retained as
unused configuration.

## Contract

The acceptance documentation describes one real refresh instead of two refresh
cycles. `PASS`, `FAIL`, and `BLOCKED` semantics remain unchanged. Full tests,
real data, screenshots, browser interaction, and post-acceptance exact-SHA
deployment checks remain unchanged.

## Testing

Update the acceptance unit tests first so they require one quote fetch, two
Dashboard snapshots around it, no sleep, and no refresh-cycle timestamp check.
Run the focused acceptance tests and direct live gate workflow while developing.
Run `make acceptance` only as the final Dashboard handoff gate.
