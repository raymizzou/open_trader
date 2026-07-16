# Live-Readiness Fixes Report

Date: 2026-07-17 (Asia/Shanghai)

Base: `d9b77f9e6c33d609c122f1cb3a5b682117714801`

Head before this report: `60af2d653e31794367c98e349d13f287419aaaa1`

## Commits

- `6c64a88` — `fix: restore best-effort trend review hooks`
- `41ee09e` — `fix: replay stale Tiger report finalization`
- `60af2d6` — `fix: reconcile trend orders by report revision`

## Important 1: automatic collection without primary-workflow coupling

- `trend-market-report` and `trend-a-share-report` attempt review close capture
  after a `generated` or `existing` report.
- A market report uses the frozen report `as_of_date`; the run date remains the
  fallback for existing artifacts without a returned JSON path.
- Missing/invalid review configuration, Futu failures, malformed report dates,
  and all other review close failures are logged to stderr and cannot change the
  primary report result.
- Both market and A-share watcher commands again pass session-open and
  protection-trigger callbacks through the existing watcher callback isolation.
  Callback failures append `trend_review_callback_failed`, increment the watcher
  exception count, and cannot suppress protection notifications or stop polling.
- No launchd job or dependency was added.

## Important 2: exact stale Tiger replay safety

- Production and replay now share `_finalize_market_report` for the stale-US
  account transform, managed-symbol protection finalization, and prepared
  metadata.
- A stale Tiger snapshot removes every BUY and changes every managed holding to
  `MANUAL_REVIEW` with `stale_tiger_account` in both source generation and replay.
- Frozen rebuild inputs now include the final managed-symbol seed. Replay rejects
  missing market finalization input rather than guessing.
- The production-path regression rebuilds the frozen evidence and compares the
  exact source/replay account, strategy judgments and formal actions, protection
  state, signal snapshots, and strategy snapshot for all three Tiger refresh
  failure classes.

## Important 3: revision-safe simulated order reconciliation

- The immutable ledger identity remains
  `(market, execution_date, full report SHA-256, action index)`.
- The Futu remark now includes a 24-hex compact report identity and stays below
  Futu's documented 64-byte UTF-8 limit.
- Broker reconciliation requires the exact remark plus normalized symbol, side,
  and Decimal-equal quantity.
- A two-revision response-failure regression proves an older same-day order
  cannot satisfy the newer report intent; the newer order is submitted.
- Existing retry behavior remains: an accepted matching order reconciles without
  duplication, while an absent broker order is retried.

## TDD evidence

Focused RED:

```text
.venv/bin/python -m pytest -q \
  tests/test_premarket_cli.py::test_trend_a_share_report_main_dispatches_and_returns_status \
  tests/test_premarket_cli.py::test_watch_trend_a_share_main_uses_independent_lock_and_paths \
  tests/test_premarket_cli.py::test_trend_market_report_dispatches_generic_runner \
  tests/test_premarket_cli.py::test_watch_trend_market_uses_separate_market_paths \
  tests/test_market_trend.py::test_stale_us_tiger_account_blocks_buys_and_marks_holdings_for_review \
  tests/test_trend_review.py::test_open_reconciles_accepted_order_after_response_failure \
  tests/test_trend_review.py::test_newer_revision_cannot_reconcile_to_older_response_failure
9 failed, 3 passed in 1.05s
```

Integrated focused GREEN:

```text
.venv/bin/python -m pytest -q \
  tests/test_premarket_cli.py tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py tests/test_trend_review.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
384 passed in 1.47s
```

Full repository GREEN, run exactly once:

```text
make test
2314 passed in 26.80s
```

`git diff --check` also exited 0 before each logical commit.

## Direct workflow check

A deterministic invocation of the real `cli.main` parser/dispatch path used a
generated US report and an injected review-close failure. It produced:

```text
trend review close failed: direct-smoke review unavailable
{"status": "generated", "report_path": "report.md", "json_path": null}
{'exit_code': 0, 'review_calls': [('US', '2026-07-15')]}
```

This verifies the report command calls automatic review collection and preserves
the successful primary exit/result when collection fails.

## Runtime state and limits of this fix wave

- `screen -ls` showed only the accepted Dashboard process; Dashboard code was not
  changed here.
- All six trend report/watch launchd labels were inspected. Each was `state = not
  running`, with working directory `/Users/ray/projects/open_trader`.
- The deployed/main checkout remains
  `d9fe44b1c4658a09314c51e4410ffd3e430aa1fd`; therefore no pre-change trend
  process was running to restart, and this feature-worktree SHA is not claimed as
  live.
- Real report/watch commands were not run because they consume external paid
  report data, mutate immutable review artifacts, and can submit simulated
  orders. `make acceptance` was not run because this is not a Dashboard handoff
  or deployment task.
