# Task 3 report

Status: DONE_WITH_CONCERNS

Commit: `4c94f0f`

Implemented:

- v5 BUY preflight accepts missing ATR, quote-derived quantity, and deferred HK lot size.
- Live quote/lot size and same-round cash reservations determine submitted quantity; missing data records a pending action without stopping other actions.
- Filled v5 buys persist `entry_fill_price`; missing ATR leaves protection pending and records the status in the filled event.
- Pending protection recovers from entry-day ATR first, then the first computable/current ATR, using the blended fill price.
- Added protection status labels and controller lot-size forwarding. Full quote-client failure remains retry-blocked; partial missing symbols do not block other actions.

Tests:

```text
.venv/bin/python -m pytest tests/test_trend_review.py tests/test_trend_market_controller.py tests/test_a_share_trend.py tests/test_notification_policy.py -q
613 passed in 8.28s
```

Concerns:

- The one-time late-buy controller plumbing and corrected report workflow are Task 4. `execute_trend_review_open` and `record_trend_review_missed_buys` expose `allow_late_buys` for that integration.
- The quote client exposes lot sizes through `get_lot_sizes`, so a client without that optional method simply leaves lot size pending.

## Review fix wave

- Protection-pending Feishu alerts now use a stable symbol/status identity and cover filled, partial, and incomplete fills.
- v5 `pending_fields` is checked against the actual missing `close`/ATR/lot fields; legacy versions do not request or use live lot sizes.
- Protection recovery now chooses entry-day ATR, then first computable ATR, then current ATR.
- Pending protection clears stale line/ATR fields until recovery.

Verification:

```text
.venv/bin/python -m pytest tests/test_trend_review.py tests/test_trend_market_controller.py tests/test_a_share_trend.py tests/test_notification_policy.py -q
619 passed in 7.90s
```

Fix-wave commits: `0b1c7d7`, `a86b32c` (stable alert de-duplication).

The same focused command remained green after `a86b32c`: `619 passed in 7.90s`.
