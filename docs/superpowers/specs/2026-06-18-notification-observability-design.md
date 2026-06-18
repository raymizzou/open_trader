# Notification Observability Design

## Context

The daily premarket workflow can now send Chinese Feishu/macOS notifications during
formal runs. The current notifier path is best effort: notification failures are
swallowed so the trading workflow can still finish, but operators cannot tell
whether a notification was attempted or why it failed.

## Goals

- Keep the daily trading workflow resilient: notification failures must not turn a
  successful data run into a failed daily run.
- Make notification attempts observable in local logs with Chinese user-facing
  messages and non-secret diagnostic fields.
- Add a CLI command that sends one real test notification through the configured
  notifiers and exits non-zero when delivery fails.

## Non-Goals

- Do not retry notifications.
- Do not add a new notification provider.
- Do not expose Feishu app secrets, webhook URLs, or receive IDs in logs or CLI
  output.

## Design

Add a small notification helper that wraps a configured `Notifier` and records
the result of each send attempt. Daily runs will use this helper inside
`DailyPremarketRunner._notify()`. A successful attempt writes an info log event
with the notification title. A failed attempt writes a warning log event with the
title, exception class, and localized error text, then returns normally.

Add `open-trader test-notification --config config/daily_premarket.env`. The
command loads the same notification configuration as `run-daily-premarket`,
sends a short Chinese test message, prints a Chinese success/failure summary, and
returns `0` on success or `1` on delivery failure. Configuration errors continue
to use argparse errors.

The existing `CompositeNotifier` currently suppresses per-provider failures so
one broken provider does not prevent the next provider from running. That behavior
will remain unchanged for daily runs. For the test command, success means the
configured composite completed without raising; this verifies that at least the
top-level notification pipeline is callable. A later provider-level result model
can be added if we need exact per-channel delivery evidence.

## Testing

- Unit test that daily `_notify()` logs success and does not raise.
- Unit test that daily `_notify()` logs failure and does not raise.
- CLI test that `test-notification` loads config, builds a notifier, sends the
  Chinese message, prints success, and exits `0`.
- CLI test that `test-notification` returns `1` and prints failure when the
  notifier raises.
