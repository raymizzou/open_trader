# Tiger Trend Final-Review Fix Report

Date: 2026-07-17 (Asia/Shanghai)

## Fixes

- Watcher review callbacks are isolated: failures are recorded as
  `trend_review_callback_failed`, counted, and cannot suppress protection
  notifications or terminate the watcher.
- Open and protection-stop order retries reconcile an intent without a result
  against the broker's orders by the unique remark. A broker-accepted order is
  recorded without duplication; an absent order is submitted again.
- Daily review facts omit `actual_equity` unless the source account snapshot is
  fresh and its `source_date` matches the trading date.
- A completed 30-trade batch starts at the minimum selected `entry_date`.
- Trend-review report selection skips malformed JSON and validates schema,
  filename/execution chronology, market/broker identity, collection shape, and
  executable actions before choosing the highest numeric revision.
- Trend reports and watchers no longer require review account IDs or invoke
  automatic review callbacks. `trend-review` remains the explicit interface.
- Legacy Dashboard report freshness falls back to the Shanghai date of
  `generated_at`, keeping Friday HK data visible as stale on Saturday even when
  its execution date is Monday.

Tiger remains the US trend broker, first-run Tiger holdings remain loaded, and
no retired Tiger long-term modules were restored.

## TDD Evidence

Initial focused RED command:

```text
.venv/bin/python -m pytest tests/test_market_trend_watch.py::test_review_callback_failure_is_recorded_without_blocking_protection_notice tests/test_trend_review.py::test_open_retries_intent_when_failed_order_is_absent_at_broker tests/test_trend_review.py::test_open_reconciles_accepted_order_after_response_failure tests/test_trend_review.py::test_close_records_stale_or_misaligned_actual_equity_as_missing tests/test_trend_review.py::test_projection_batch_starts_at_earliest_selected_entry tests/test_premarket_cli.py::test_trend_review_loader_prefers_latest_numeric_revision tests/test_premarket_cli.py::test_trend_market_report_dispatches_generic_runner tests/test_premarket_cli.py::test_watch_trend_market_uses_separate_market_paths tests/test_dashboard.py::test_dashboard_legacy_hk_friday_report_uses_generated_date_for_freshness -q
10 failed in 0.53s
```

Additional session-open and stop-retry RED proof:

```text
.venv/bin/python -m pytest tests/test_market_trend_watch.py::test_session_review_callback_failure_does_not_stop_watcher tests/test_trend_review.py::test_stop_retries_intent_when_failed_order_is_absent_at_broker -q
2 failed in 0.41s
```

Focused GREEN:

```text
.venv/bin/python -m pytest tests/test_market_trend_watch.py tests/test_trend_review.py tests/test_premarket_cli.py tests/test_dashboard.py -q
252 passed in 0.69s
```

Broader affected GREEN:

```text
.venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_market_trend.py tests/test_market_trend_watch.py tests/test_trend_review.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py -q
661 passed in 20.11s
```

Full repository GREEN:

```text
make test
2313 passed in 26.38s
```

`make acceptance`, live report commands, runtime data mutation, and process
inspection/restarts were intentionally not run, as required by this fix-wave
scope.
