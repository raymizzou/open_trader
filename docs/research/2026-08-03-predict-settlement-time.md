# Predict.fun market end, resolution, and redemption timing

Date: 2026-08-03

## Conclusion

Predict.fun does provide the scheduled market/category end time. In the current
mainnet schema it is `Category.startsAt` / `Category.endsAt`, reached through a
market's `categorySlug`; it is not a `Market.settlementAt` field.

`endsAt` is the scheduled end used for market-rule and annualized-duration
comparison. It is not a guaranteed timestamp at which collateral is already
redeemable. Redemption becomes possible after the market is resolved and the
on-chain payout is reported, so final availability is state-driven.

## Primary-source evidence

- The current official `Market` schema includes `categorySlug`, lifecycle
  status, outcomes, and resolution data, but no `startsAt`, `endsAt`, or
  `settlementAt` field: [Predict Market schema](https://dev.predict.fun/market-14037477d0).
- The official category response contains `startsAt`, `endsAt`, `status`, and
  `resolutionProvider`, together with its markets:
  [Get category by slug](https://dev.predict.fun/get-category-by-slug-25326911e0).
- The official API describes `MarketStatus` as the lifecycle from registration
  through resolution, and `OutcomeStatus` as the resolution status:
  [MarketStatus](https://dev.predict.fun/marketstatus-14037482d0),
  [OutcomeStatus](https://dev.predict.fun/outcomestatus-14037516d0).
- The official SDK redeems a resolved position with `conditionId` and
  `indexSet`; it does not accept a scheduled settlement timestamp:
  [Predict Python SDK](https://github.com/PredictDotFun/sdk-python#redeeming-positions),
  [Predict TypeScript SDK](https://github.com/PredictDotFun/sdk#how-to-redeem-positions).

## Mainnet read-only verification

A read-only probe used the configured API key without printing it or submitting
transactions. For the first 100 open `DEFAULT` markets:

- 100 markets belonged to 25 unique categories;
- all 25 categories had both `startsAt` and `endsAt`;
- 95 markets exposed at least one `polymarketConditionIds` candidate;
- all 95 candidate Polymarket markets were found;
- 57 Predict `endsAt` values exactly matched Polymarket `endDate`;
- 36 differed: 21 by one minute, 14 by 29 hours, and one by five hours;
- two Polymarket candidates had no `endDate`.

This confirms both that Predict end times are available and that candidate IDs
do not prove time/rule equivalence.

### Mismatch examples

- **One minute earlier on Predict:** Extended FDV above $150M one day after
  launch is `2027-01-01T04:59:00Z` on Predict and `2027-01-01T05:00:00Z` on
  Polymarket. Both rule texts say December 31, 2026 at 11:59 PM ET, so Predict's
  metadata reflects the written cutoff while Polymarket's `endDate` is one
  minute later.
- **Twenty-nine hours later on Predict:** Will Jesus Christ return before 2027
  is `2027-01-01T05:00:00Z` on Predict and `2026-12-31T00:00:00Z` on
  Polymarket. Both rule texts say December 31, 2026 at 11:59 PM ET, so the
  written rules agree even though Polymarket's `endDate` does not.
- **Five hours earlier on Predict:** Will Pump.fun perform an airdrop by
  December 31, 2026 is `2027-01-01T00:00:00Z` on Predict and
  `2027-01-01T05:00:00Z` on Polymarket. Polymarket's written rule says 11:59 PM
  ET, while the Predict category description does not reproduce that rule.

Raw timestamp equality therefore creates false negatives, while a fixed
tolerance can create false positives. The executable cutoff must be supported
by the complete written rules, with metadata differences retained as evidence.

## Integration correction

1. Fetch the market, then fetch its category by `categorySlug`.
2. Normalize category `endsAt` as `event_end_at`; do not expect
   `Market.settlementAt` in the current schema.
3. Keep `event_end_at` separate from the observed resolved/redeemable state.
4. Use the later validated venue end time for the existing theoretical
   annualized calculation, while labeling it as theoretical until redemption.
5. Require cross-venue rule/time validation before execution; do not accept or
   reject a pair from raw timestamp equality alone.
