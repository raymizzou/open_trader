# Agent Reason Notifications Design

## Context

The June 18 action notification showed ready trim actions for MRVL, QQQ, SOXX,
and VIXY with the reason "当前价格已达到或高于目标价 1". This was misleading.
The actual `trading_plan.csv` values were not numeric `1` values:

- MRVL `target_1=200`
- QQQ `target_1=690`
- SOXX `target_1=503`
- VIXY `target_1=18.5`

The problem is semantic. For `Underweight` or `reduce` advice, these values are
often downside targets, stop references, or risk-control levels. The current
trade-action reason only mirrors the price trigger status from
`evaluate_plan_quote()`, so it can make a bearish trim recommendation sound like
a bullish target hit. The notification also hides the TradingAgents source
report, even though that report contains the real investment rationale.

## Goal

Action notifications should show why the trading agent recommended the action,
and they should separately show the price or plan condition that made the action
ready. For ready actions, the user should see a concise Chinese reason plus a
short excerpt from the original TradingAgents report.

## Non-Goals

- Do not change the TradingAgents model prompt or decision process.
- Do not build automatic order placement.
- Do not send the full `raw_decision` JSON in Feishu messages.
- Do not reinterpret every possible analyst phrase into a perfect structured
  trading plan. This change improves the notification and audit surface while
  keeping ambiguous inputs conservative.

## Recommended Approach

Carry an agent-reason excerpt through the batch artifacts:

```text
trading_advice.csv advice_summary/raw_decision
-> trading_plan.csv plan_text + agent_reason + agent_excerpt
-> trade_actions.csv reason + agent_reason + agent_excerpt + trigger_reason
-> Feishu action notification
```

This keeps the notification renderer simple and keeps machine-readable artifacts
as the source of truth for future automation.

## Data Contract

Extend `trading_plan.csv` with:

- `agent_reason`: concise Chinese reason derived from the TradingAgents
  `advice_summary`. It should prefer the template `理由` section, then
  `操作计划`, then a short fallback from the whole summary.
- `agent_excerpt`: short original-language excerpt from the TradingAgents
  report, usually one or two sentences from `操作计划` or `理由`.

Extend `trade_actions.csv` with:

- `agent_reason`
- `agent_excerpt`
- `trigger_reason`: the existing price trigger message, preserved separately
  from the investment reason.

The existing `reason` column remains for compatibility during the first
implementation. New notification code should use `agent_reason` for "原因" and
`trigger_reason` for "触发". If `agent_reason` is missing, notification code
falls back to `reason` and shows a human-review note in the message without
changing the persisted action status.

## Notification Behavior

For each ready action, Feishu should render:

```text
原因：<Chinese concise agent reason>
原文：<short TradingAgents excerpt>
触发：<localized trigger condition with current price>
```

For `Underweight`, `reduce`, or trim-like actions, the notification must not say
"达到目标价 1" unless the plan explicitly describes that value as a profit target.
The default wording should be neutral, such as:

```text
触发：当前价 289.54，行动已满足计划中的减仓/风控条件。
```

For buy/add actions, the trigger can still describe entry-zone or add-zone
conditions when those fields are structured and unambiguous.

## Parsing Rules

Use the existing structured `advice_summary` template first:

- `理由`: primary source for `agent_reason` and `agent_excerpt`
- `操作计划`: primary source when it contains the concrete action
- `目标价`: never used as the sole reason for a trim action
- `风控`: can enrich trigger wording, but should not replace the agent reason

The initial implementation may use deterministic text extraction:

- Split `advice_summary` into existing Chinese template sections.
- For `agent_reason`, return a concise Chinese sentence if the source is already
  Chinese; otherwise use a controlled Chinese framing around a short excerpt.
- For `agent_excerpt`, preserve original wording but cap length so Feishu stays
  readable.

## Error Handling

- Missing `agent_reason`: keep CSV generation working, but render
  "原文依据缺失，需人工复核". Continue to let the existing precision-field checks
  decide whether a ready row is displayed as review.
- Missing `agent_excerpt`: show the concise reason and omit the excerpt line.
- Legacy CSV without new columns: notification renderer should not crash.
- Ambiguous target semantics: avoid "目标价 1/目标价 2" wording for trim actions
  unless source text clearly says take profit.

## Components

### `src/open_trader/trading_plan.py`

- Add plan fields for `agent_reason` and `agent_excerpt`.
- Extract these fields from `advice_summary` while building plan rows.
- Preserve compatibility for older plan CSVs by defaulting missing columns to
  empty strings.

### `src/open_trader/trade_actions.py`

- Add action fields for `agent_reason`, `agent_excerpt`, and `trigger_reason`.
- Copy agent fields from the plan into each action row.
- Move the existing quote-status message into `trigger_reason`.
- Keep `reason` populated for compatibility, preferably with `agent_reason` when
  present and the trigger message otherwise.

### `src/open_trader/notifications.py`

- Render `原因` from `agent_reason`.
- Render `原文` from `agent_excerpt` when present.
- Render `触发` from `trigger_reason`, localized with action-aware wording.
- Remove user-facing "目标价 1" wording from trim notifications.

## Testing

Add focused tests for:

- An `Underweight` MRVL-style advice row where `target_1=200` and current price
  is higher than `target_1`; notification must not contain "目标价 1".
- Ready trim notification contains an `原文` line from the TradingAgents summary.
- Legacy `trade_actions.csv` rows without new fields still render.
- `trading_plan.csv` and `trade_actions.csv` include the new fields.
- Existing buy/add trigger wording still works for entry and add zones.

## Acceptance Criteria

- Feishu ready-action sections explain the TradingAgents rationale, not only the
  price trigger.
- The user can see a short original TradingAgents excerpt in the notification.
- Underweight trim actions do not claim the current price reached "target 1".
- CSV outputs remain machine-readable and backward compatible with older rows.
