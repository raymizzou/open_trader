# Trend Report Industry Concentration Explanation Design

**Date:** 2026-07-28
**Status:** Approved in conversation
**Markets:** CN, US, HK

## Goal

Explain industry concentration without diluting a trend strategy's natural
preference for the strongest industries.

The report will show current and projected industry exposure, planned stop-loss
risk by industry, and industry breadth structure. These facts are explanatory:
they do not add an industry cap, change candidate ordering, reduce target
weights, or block an otherwise valid buy.

## Confirmed Decisions

1. Industry concentration is not a new entry gate.
2. The existing single-entry and portfolio planned-risk budgets remain the
   authority for sizing and admitting buys.
3. The report distinguishes nominal exposure from planned stop-loss risk.
4. Industry right-side count breadth and right-side market-cap breadth explain
   whether a trend is broad or led by larger constituents.
5. Missing explanatory data is shown as unavailable. It is never invented,
   coerced to zero, or allowed to invalidate an otherwise valid report.
6. Existing reports remain immutable; there is no historical backfill.

## Current Evidence

The 2026-07-28 CN report illustrates the missing explanation:

- Bank holdings would move from 2 to 8 after the proposed actions.
- Bank account weight would move from 7.90% to approximately 31.90%.
- The complete post-action portfolio planned stop-loss risk is 1.53% of account
  net value, below the existing 4% normal-risk limit.
- The existing local breadth calculation found 8 right-side instruments among
  42 valid bank constituents, or 19.05%.
- The new Trend Animals aggregate fields returned 19.1% right-side count
  breadth and 65.0% right-side market-cap breadth for the same bank industry and
  data date.

The useful conclusion is not simply "concentrated" or "safe." It is:

> The account follows a bank-led trend, nominal exposure is concentrated,
> planned stop-loss risk remains within budget, and the industry trend is led
> disproportionately by larger constituents.

## Scope

### In scope

- Current and projected position count by industry.
- Current and projected nominal account weight by industry.
- Existing, new, and projected planned stop-loss risk by industry.
- Each industry's share of total projected planned stop-loss risk.
- Existing locally calculated right-side count breadth.
- The new Trend Animals `TrendRightSideMktCapRatio` field.
- JSON, Markdown, and Dashboard presentation.
- Explicit unavailable states and additive audit evidence.

### Out of scope

- An industry position-count or account-weight cap.
- A new candidate filter, ranking key, weighted score, or position-sizing rule.
- Changing the 0.4% single-entry planned-risk limit, 4% portfolio normal-risk
  limit, or 1% abnormal-loss buffer.
- Treating a protection line as a maximum-loss guarantee.
- Market-wide breadth, fixed breadth thresholds, or a breadth-based market
  regime gate.
- `heatScore` fields, which describe product browsing popularity rather than
  the strategy's trend or account risk.
- The new API-feedback workflow and the retired warm-to-flat list.
- Notification redesign, historical reconstruction, a new database, or a new
  charting dependency.

## Data Contract

Each report adds an `industry_exposure` list. Every row contains:

- `industry` and, when available, `industry_tm_id`;
- `current_position_count`;
- `projected_position_count`;
- `current_weight_pct`;
- `projected_weight_pct`;
- `existing_planned_stop_risk_pct`;
- `new_planned_stop_risk_pct`;
- `projected_planned_stop_risk_pct`;
- `projected_risk_share_pct`;
- `right_count`;
- `right_valid_count`;
- `right_count_ratio`;
- `right_market_cap_ratio`;
- `breadth_difference_pp`; and
- `as_of_date`.

All monetary risk and weight percentages use account net value as their
denominator. `projected_risk_share_pct` instead uses total projected planned
stop-loss risk, so the report states that denominator explicitly.

No field silently changes denominator between markets.

## Exposure Calculation

### Current exposure

Current position count and market value come from the frozen account snapshot.
Current weight is:

`industry current market value / account net value`

Positions with an unavailable industry are grouped under `未分类`; they are not
discarded.

### Projected exposure

Start with the current positions, then apply the report's frozen actions:

- remove `SELL_ALL` positions;
- subtract the frozen quantity for `SELL_PARTIAL`;
- add each planned buy at its frozen estimated quantity and execution reference
  price; and
- convert into account currency with the same frozen FX fact used by sizing.

Projected weight is:

`industry projected market value / account net value`

If a required quantity, price, FX fact, or industry is missing, the affected
projected value is unavailable. The report does not substitute target amount,
zero, or a current quote fetched after the report was frozen.

## Planned Stop-Loss Risk Attribution

Existing surviving positions reuse the current portfolio-risk formula:

`quantity × max(0, reference price - active protection line) + normal costs`

New positions reuse each `BuyAction.planned_stop_risk`. Sell-all positions
contribute zero projected risk. Partial sells retain their full pre-fill risk
until the reduced quantity appears in a fresh account snapshot, matching the
existing portfolio-risk engine's conservative treatment.

For each industry:

`projected industry risk = surviving risk + new planned risk`

`projected risk share = projected industry risk / total projected portfolio risk`

This is attribution only. The existing risk engine continues to decide whether
the portfolio stays within its single-entry and portfolio limits.

The report keeps the existing disclosure that the risk budget is a target, not
a maximum-loss guarantee. Protection lines do not model gaps, limit-down
conditions, unavailable liquidity, or execution slippage.

## Breadth Data

The existing industry-member calculation remains the source of:

- right-side count;
- valid denominator;
- count ratio;
- coverage validation; and
- historical count-ratio change used by the existing ordering.

Do not buy the new `TrendRightSideCountRatio` field: it duplicates an audited
metric already calculated by the code.

Add only `TrendRightSideMktCapRatio` to the existing eligible-industry state
snapshot request. At the 2026-07-28 billing catalog price this costs 0.004 Trend
Animals balance units per queried eligible industry and uses the existing
same-date response cache.

`breadth_difference_pp` is:

`(right market-cap ratio - right count ratio) × 100`

The report displays the signed difference without inventing a universal
"healthy" threshold. Missing, non-finite, out-of-range, or wrong-date aggregate
values render as `未提供` and do not affect ordering or actions.

## Presentation

The compact report row is:

> 银行｜持仓 2→8｜账户权重 7.90%→31.90%｜计划止损风险
> 0.40%→1.53%｜风险贡献 100%｜右侧个数 19.1%｜右侧市值
> 65.0%｜差值 +45.9pp

Presentation rules:

- place the compact industry rows next to the existing portfolio-risk summary;
- keep the action list visually primary;
- sort rows by projected planned stop-loss risk descending, then industry name;
- label every denominator in the expanded audit view;
- show `未提供` rather than hiding an incomplete metric; and
- preserve the existing detailed current concentration and industry-context
  evidence in the audit section.

No new chart is required. A short text row communicates the decision facts
without adding a visualization dependency.

## Error Handling

- Invalid account net value makes weight percentages unavailable.
- Missing industry identity groups current exposure as `未分类`; a projected buy
  without industry identity has unavailable industry attribution.
- Missing protection-line facts retain the existing fail-closed behavior for
  new buys.
- Missing optional market-cap breadth affects presentation only.
- A zero total projected planned risk makes risk-share percentages unavailable,
  not zero.
- Existing frozen report validation accepts the new fields additively and does
  not reinterpret old reports.

## Verification

Focused tests must cover:

1. current and projected counts and weights across hold, sell-all, partial-sell,
   and buy actions;
2. existing, new, projected, and portfolio-share risk attribution;
3. an industry-led example equivalent to the 2026-07-28 bank report;
4. missing industry, price, FX, protection-line, and market-cap breadth facts;
5. JSON serialization and Markdown rendering;
6. Dashboard desktop and mobile rendering without horizontal overflow; and
7. identical candidate ordering and buy actions with and without the optional
   market-cap breadth field.

Development uses focused tests. Because this changes Dashboard behavior, the
final review gate is `make acceptance`; only `PASS` is review-ready. The exact
accepted Git SHA must then be redeployed and verified before the review URL is
handed to the user.

## Rollout

The change is additive and begins with newly generated reports only. No strategy
version changes because filters, ordering, sizing, risk limits, and actions do
not change.

If future evidence shows industry-wide gaps regularly exceed the existing
abnormal-loss allowance, that is a separate risk-budget design. It must not be
smuggled into this explanatory report change.
