# Task 4 Report: Controller channel-aware delivery and protection blocker

## Status

Implemented and committed as `e5a646e` (`feat: track controller notification delivery by channel`).

## TDD evidence

### RED

Command:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_trend_market_controller.py::test_controller_notification_retries_only_feishu_once \
  tests/test_trend_market_controller.py::test_controller_notification_stops_after_one_retry \
  tests/test_trend_market_controller.py::test_legacy_controller_notification_is_not_replayed \
  tests/test_trend_market_controller.py::test_protection_blocker_notifies_feishu_once_per_market_day -q
```

Result: `4 failed in 0.63s`.

The failures matched the missing behavior:

- macOS success made the v1 implementation return final despite Feishu failure;
- `_retry_pending_feishu_notifications` did not exist;
- the old `config.notifiers` guard prevented an existing v1 record from being treated as final;
- `_notify_protection_blocker` did not exist.

### GREEN: focused delivery tests

Same command after the minimal implementation.

Result: `4 passed in 0.47s`.

### GREEN: controller file

Command:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_trend_market_controller.py -q
```

Result: `112 passed in 4.01s`.

### GREEN: full repository

Command:

```bash
PYTHONPATH=src:. /Users/ray/projects/open_trader/.venv/bin/pytest -q
```

Result: `3044 passed in 70.89s (0:01:10)`.

## Changed files

- `src/open_trader/trend_market_controller.py`
  - preserves `_notify_once(title, message, key)`;
  - writes atomic v2 delivery records with frozen Feishu title/body;
  - separates one-shot non-Feishu delivery from Feishu delivery;
  - retries pending Feishu delivery once, for two attempts maximum;
  - treats legacy v1 records as final;
  - renders controller Feishu attention copy with Task 1 policy helpers;
  - emits one Feishu-only protection blocker per market/day;
  - runs the pending retry scan after each loop heartbeat.
- `tests/test_trend_market_controller.py`
  - covers missing-channel retry, exhaustion, exact v2 state, legacy finality, preserved macOS copy, and protection deduplication.

## Self-review

- Notification identity remains `market|execution_date|action|reason`; `occurred_at` is stored but does not defeat deduplication.
- Successful `macos`/`xiaoai` channels cannot satisfy Feishu delivery.
- Delivered channels are not resent; Feishu records stop after attempt 2.
- Retry scans ignore v1, delivered, and exhausted records.
- Protection uses fixed action/reason identity and the A3 attention renderer; it does not relabel or forward individual B2/B3 alerts.
- Existing non-Feishu title/body are passed through unchanged.
- Task 5 OpenD sharing and Task 6 order grouping were not implemented.

## Concerns / deployment boundary

No code concern found in self-review. Per task instruction, no live controller/service was restarted and no deployment verification was attempted; that belongs to Task 7. The direct notifier behavior is exercised with real `CompositeNotifier` channel dispatch and isolated recording/flaky notifier implementations in the focused tests.
