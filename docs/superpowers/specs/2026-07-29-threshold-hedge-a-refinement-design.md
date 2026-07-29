# Threshold Hedge A UI Refinement

## Decision

Variant A is the selected direction. Keep the existing Open Trader prediction
market visual language and replace the always-visible evidence area with
candidate-level progressive disclosure.

## Strategy navigation

Keep `预测市场` as the primary Open Trader navigation item. Inside that
workspace, add a two-option strategy switch below the page title:

- `同一市场套利` opens the existing YES/NO arbitrage page;
- `LLM对冲套利` opens the threshold-hedge discovery page.

Switching replaces the strategy body without reloading the page. Polymarket
health, wallet, and trading-readiness status are shared; scanner metrics,
candidates, and logs belong to the selected strategy. The existing strategy is
the default, and the selection lasts only for the current page session.

## Candidate row

Each candidate remains compact and shows:

- event and condition identifiers;
- structured decision: `APPROVE`, `REJECT`, or `PARSING`;
- 24h volume;
- maximum cost and minimum payout;
- theoretical minimum profit;
- simple annualized yield;
- data freshness.

The annualized yield is a disclosure button, not plain text. Its label includes
the current value and an expand/collapse indicator. Candidate details are
collapsed by default.

## Expanded candidate detail

Activating the annualized-yield disclosure expands that candidate in place and
shows:

1. Annualized calculation:
   - maximum cost: `$19.46`;
   - minimum payout: `$20.00`;
   - theoretical minimum profit: `$0.54`;
   - estimated capital lock: `47 days`;
   - simple return: `$0.54 / $19.46 = 2.77%`;
   - simple annualized yield: `2.77% * 365 / 47 = 21.55%`.
2. Historical context, labelled as a comparison rather than a second current
   yield: 7-day and 30-day medians plus the current opportunity's percentile.
3. Deterministic payoff proof and validation chain.
4. Codex structured `APPROVE` or `REJECT` result, summary, and reason codes.
5. Order preview and manual-confirmation action only when the decision is
   `APPROVE`.

Simple annualized yield is non-compounding and uses estimated capital-lock
days. The UI states that settlement delays reduce realized annualized yield and
that profit is not locked before both legs fill.

## Model status

The scanner status shows the default Codex profile as:

`sol · xhigh · fast`

This is display-only in the prototype. The production implementation will use
these as the initially selected model, reasoning, and speed values. The
prototype does not add configuration persistence, API calls, or real orders.

## Interaction and responsive behavior

- Candidate details use native `<details>` / `<summary>`.
- Details start closed and are keyboard accessible.
- The yield disclosure keeps a minimum 44px touch target.
- Opening one candidate does not close another.
- At 375px, the row becomes a labelled vertical summary without horizontal
  scrolling.

## Prototype verification

Verify Variant A at 1440px, 768px, and 375px. Check the annualized disclosure,
LLM rejection state, confirmation modal, holding state, visible focus, and
absence of browser console errors.
