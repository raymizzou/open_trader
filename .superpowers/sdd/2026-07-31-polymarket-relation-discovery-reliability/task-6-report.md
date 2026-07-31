# Task 6 Report: Record Millisecond Opportunity Windows and Recheck Live Rules

## Status

Implemented on `fix/polymarket-relation-discovery`; commit follows after the
focused verification below.

## RED

The prescribed selector was first run before Task 6 tests existed:

```text
332 deselected in 0.59s
```

After adding the episode and live-rule tests, the same selector failed before
the monitor changes:

```text
2 failed, 332 deselected in 0.66s
```

The failures were the expected missing durable `first_positive_at` state and
missing exact-event rule verification.

## GREEN

Focused Task 6 selector:

```text
4 passed, 333 deselected in 0.83s
```

Focused monitor, store, and Dashboard suites:

```text
364 passed in 40.05s
```

Additional checks:

```text
git diff --check: PASS
python -m compileall: PASS
```

## Implementation

- `_upsert_signal()` now returns the durable signal ID, preserves the first
  positive timestamp and initial profit, updates last/peak values, captures
  both exchange book timestamps and local receive timestamps, and records
  millisecond duration.
- Negative economics, quote-age expiry, stream disconnects, rules changes, and
  stale relation discovery close the existing episode with the exact required
  reason. Fresh positive data opens a new signal ID after closure.
- The one-second monitor loop maintains open episodes and closes stale or
  disconnected relation signals.
- The first positive refresh fetches only its source event, rediscovers only
  that event, compares a semantic relation fingerprint, and publishes fresher
  unchanged catalog metadata. A changed identity is closed before the row can
  become actionable.
- Dashboard signal history aliases preserve observed duration, three profit
  values, end reason, notification state, and the book/receive timestamps.

## Files

- `src/open_trader/polymarket_monitor.py`
- `src/open_trader/dashboard_web.py`
- `tests/test_polymarket_monitor.py`
- `tests/test_dashboard_web.py`

## Self-review

- Reused the existing SQLite signal episode API; no history table or new
  persistence schema was added.
- Verified all existing monitor/store/Dashboard tests after changing the
  relation refresh path and stream timeout maintenance.
- Confirmed changed rules remove the opportunity before Codex/actionability and
  preserve the original first/initial episode fields.

## Concerns

The monitor keeps exchange timestamps separately from local receive timestamps
because the existing `ThresholdOrderBook` type's `confirmed_at` field is the
local receive time. Quote freshness intentionally uses local receive age so
existing REST refresh semantics remain compatible; exchange timestamps are
still persisted for audit and projection.
