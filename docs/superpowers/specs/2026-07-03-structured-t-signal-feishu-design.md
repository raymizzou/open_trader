# Structured T Signal Feishu Design

## Goal

Make each 做T Feishu alert more structured while keeping the existing one-message-per-symbol notification flow.

## Confirmed Format

Each actionable signal sends one text message. The title contains the app name, market symbol, and Chinese action:

```text
Open Trader｜做T提醒｜HK.02840｜卖出做T
```

The body uses fixed sections:

```text
动作：卖出做T
比例：10%
状态：盘中有效，等待执行确认

结论：
价格高于 VWAP 后受压，5分钟 RSI 偏高，出现高抛做T信号。

依据：
1. 价格高于 VWAP 后受压
2. 5分钟 RSI 处于偏高区间

时间：2026-07-03 10:51:59
```

## Rules

- Keep one Feishu message per symbol.
- Do not show raw action enum values such as `BUY_T` or `SELL_T`.
- Use Chinese action labels: `买入做T`, `卖出做T`.
- Keep `比例` explicit. If no ratio exists, show `-`.
- Render evidence as numbered lines using existing `signal.evidence`.
- Use the signal `updated_at` timestamp for `时间`.
- Preserve existing notification dedupe and timeline behavior.

## Testing

Add focused unit coverage for title and body rendering in `tests/test_t_signal_runner.py`. Existing runner tests continue to verify send-once and dedupe behavior.
