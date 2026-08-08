# Trend candidate ranking, cost, and audit design

Date: 2026-08-08

Status: design approved in conversation; implementation not started

Scope: CN, HK, and US Trend Animals candidate fetching, common candidate ranking,
report audit, and report/Dashboard projection. Holding snapshot optimization is
explicitly deferred.

## Goal

Reduce Trend Animals charges without weakening entry discipline, risk controls,
or execution safety, while removing industry-derived ranking interference.

This is a cost and strategy-simplification change, not a return-optimization
exercise. Historical replay is a correctness check only; no return, drawdown,
or turnover improvement threshold is used to select the new order.

## Current evidence

The frozen US report for 2026-08-07 contains 134 raw candidates, 10
discipline-qualified candidates, 10 simulated holdings, and 12 real-only
holding snapshots.

Current ranking is industry-first. In `context_current_only` mode it uses
industry temperature, industry strength, industry warm-to-hot count, industry
member right-side share, and only then individual local strength.

The frozen known-field estimate is 19.648 units. Of that, 2,786 industry-member
snapshot rows cost 8.358 units, about 43% of the whole estimate and 73% of the
previously optimized 11.426 estimate. Those rows recompute member right-side
share and coverage, which are not discipline gates and are only late ranking or
display fields.

The same candidate evaluation and ranking functions already serve CN, HK, and
US. CN has a separate report runner, but it currently performs the same
complete-snapshot request and can consume the same staged-fetch result. Candidate
pool component requests must remain because they identify the securities to
screen; eligible-industry component requests are unnecessary and leave.

## Approaches considered

1. Keep the current industry-first order and optimize only field fetching. This
   lowers cost but preserves the ranking interference that prompted the change.
2. Rank by individual strength, retaining several industry confirmations. This
   is cheaper, but industry strength and breadth still complicate the order
   without serving a discipline rule.
3. Use one cross-asset individual strength for the primary order, retain only
   industry temperature as an exact-tie key, and remove industry breadth. This
   is the selected approach because it is deterministic, minimal, and uses only
   fields required by discipline or qualified-candidate expansion.

## Decisions

### 1. Use one fixed discipline fetch waterfall in all three markets

Do not fetch complete snapshots for the raw candidate pool. Keep holdings on
their existing complete-snapshot path, and screen non-held candidates in this
fixed order:

1. Fetch free identity fields: ID, symbol, name, asset, and data date. Apply the
   existing market, asset, held-symbol, symbol, and date checks.
2. Fetch individual local strength for survivors and apply the unchanged local
   strength gate of at least 95.
3. Fetch market cap for survivors and apply the unchanged CNY-adjusted minimum.
4. Fetch previous/current individual temperature for survivors and apply the
   unchanged warm-to-hot/boiling transition gate.
5. Fetch the remaining discipline fields only for survivors: tradability,
   industry ID and name, amount, right-side flag, right-side days, current
   phase, and danger flag. Resolve ATR through the existing Futu path.
6. Fetch industry temperature once for the surviving candidate and required
   holding industries. Apply the unchanged allowed-temperature gate.
7. Fetch remaining display, execution, rotation, and audit expansion fields
   only for discipline-qualified candidates. This includes global strength.

The order is fixed. It does not dynamically optimize itself and does not add a
new cache. Existing exact-request/date caching remains authoritative.

Every stage retains strict unique-ID, requested-ID, and data-date validation. A
failed or malformed stage fails the affected market closed, reports incomplete
data, and produces no new BUY plan. It must never fall back automatically to the
old all-candidate complete request or to eligible-industry member scanning.

CN and the shared HK/US runner reuse one staged-fetch helper and the existing
candidate evaluator. There is no second discipline model.

### 2. Use one individual-first order across stocks and ETFs

The local strength value remains the discipline gate. After qualification,
stocks and ETFs compete for the same ten positions using this exact order:

1. individual global strength descending;
2. industry temperature descending, only when global strength ties;
3. right-side days ascending;
4. amount descending;
5. symbol ascending.

Global strength is the primary ranking value because the candidate pools mix
asset categories and it supplies one cross-category basis. Industry temperature
is already required by discipline and adds no marginal request. Right-side days
and amount are already discipline fields and remain deterministic tie-breakers.

Industry strength, warm-to-hot count, industry history direction/change, and
industry right-side share do not participate in ranking.

A candidate with all discipline checks satisfied but missing or invalid global
strength remains discipline-qualified but is not plan-eligible. It is listed in
the final-plan audit as `全局强度缺失，无法排序`; local strength is not silently
used as a mixed-basis fallback.

For the frozen 2026-08-07 US candidates the expected qualified order remains:

`GRMN -> WTW -> ABNB -> REGN -> TEAM -> CRWD -> HPQ -> PATH -> SWK -> WSM`

Normal BUY planning consumes the full qualified sequence, not only the displayed
Top 10. Rotation comparison formulas, local/global comparison basis, threshold,
sizing, and execution rules remain unchanged.

### 3. Version the strategy behavior

The simplified order is new strategy behavior and must not rewrite old-version
replay semantics. Activate it only for these new versions:

- CN `v13`;
- HK `v11`;
- US `v11`.

Old versions keep their existing order and remain replayable. The new versions
inherit existing Kelly samples and keep all discipline, exit, risk, sizing,
drawdown, and rotation parameters unchanged.

The three versions activate as one deployment. If any market fails validation,
none of the three new versions becomes current.

### 4. Remove industry member breadth and unused industry fields

Do not request component membership for eligible industries or member fields
such as `tradableFlag` and `isTrendRightSide`.

New reports no longer calculate, require, or project:

- component, snapshot, tradable, or right-side member counts;
- member snapshot or right-state coverage;
- member-derived right-side share or its history/change;
- aggregate right-side count or market-cap ratios;
- industry strength.

The industry context displayed in new reports retains industry name,
temperature, and temperature direction when prior temperature history is
available. Historical artifacts retain their existing fields and remain
readable; they are not rewritten.

### 5. Make audit mean final-plan audit

Reuse the existing `risk_skips`, normal buy actions, and rotation results. Do
not add a parallel audit model.

After normal-BUY and rotation planning completes:

- a candidate selected by a normal BUY or rotation BUY is plan-included and
  cannot also be skipped;
- every other discipline-qualified candidate, including ranks below Top 10,
  appears with its decisive final-plan reason;
- when ordinary buying is blocked by full slots, include the rotation outcome
  when available;
- a qualified candidate outside the two rotation comparison slots is labelled
  accordingly;
- other risk, cash, mapping, sizing, or missing-global-strength blockers use
  their existing decisive reason;
- discipline failures appear last with only symbol, name, and
  `没有通过纪律`.

For the frozen report, GRMN is rotation-planned rather than skipped, while WTW
records the full-slot context and `强度差 12.3 小于门槛 20`.

### 6. Remove duplicate empty buy-plan presentation

Render the normal buy-plan section only when it contains an executable normal
BUY. When empty, omit its heading, `无允许买入标的`, and associated no-trade
sentence.

Automatic rotation and real-account manual-rotation sections keep their existing
positions. Summary counts remain. Skip reasons appear only in candidate audit.

### 7. Keep holding optimization out of this change

Simulated and real-only holdings keep complete snapshots because those fields
feed exits, protection, risk, and rotation. Optimize them only after measuring
the three-market result from this change.

### 8. Regenerate the current reports after deployment

After the three new strategy versions are deployed with submission disabled,
regenerate the most recent valid trading-day report for CN, HK, and US. Publish
each result as a new immutable revision and promote it to the market's current
display revision. Keep the prior revision available for audit; do not overwrite
or delete it.

Regeneration must remain no-submit. It may update report and projection data but
must not create broker orders. The final Dashboard acceptance gate runs only
after all three regenerated revisions are selected as current, so acceptance
covers the exact data the user will review.

## Cost model and guard

For the frozen 2026-08-07 US input under the 2026-08-08 field catalogue:

- staged non-held candidate screening and qualified expansion: 1.230;
- 10 simulated holding complete rows: 0.710;
- 12 real-only holding complete rows: 0.852;
- 15 unique required industry-temperature rows at 0.004: 0.060;
- eligible-industry component/member breadth: 0.

The frozen known-field target is 2.852 units, down 16.796 units, or about
85.5%, from 19.648. This is an estimate, not a promise that the live balance
delta will exactly equal 2.852; provider cache state, component-pool billing,
and billing timing can change the actual debit.

CN and HK receive equivalent frozen-input budgets during implementation. Cost
regression tests for all three markets must prove:

- zero eligible-industry component/member requests;
- no paid field request contains candidates that failed an earlier stage;
- the approved frozen-input estimate is not exceeded.

There is no global runtime hard cap because candidate and holding counts vary.
The report continues to display the actual non-negative balance delta and known-
field estimate/completeness honestly.

## Output contract

Must remain identical for the same frozen input:

- discipline thresholds and discipline-qualified symbol set;
- candidate and holding field values that remain in the report;
- holding decisions and protection lines;
- risk formulas, limits, and drawdown state-transition rules;
- exits;
- rotation comparison basis, threshold, sizing, ordering by strength gap, and
  execution dates.

Intentional differences:

- new-version qualified-candidate order and normal-BUY priority;
- lower cost totals and changed request/cache metadata;
- removed industry strength/breadth/right-side fields in new projections;
- detailed discipline-failure fields/reasons become generic
  `没有通过纪律`;
- final-plan audit does not label a planned target as skipped;
- an empty normal buy-plan section is omitted;
- strategy versions, generation time, and hashes.

## Mock UI acceptance

Before implementation planning, provide one read-only Mock UI in the existing
report visual language using the 2026-08-07 US sample. It must show:

- the unchanged summary, market ranking, and automatic-rotation hierarchy;
- no empty normal buy-plan section;
- a final-plan audit containing qualified-but-unplanned candidates and reasons;
- discipline failures at the end with `没有通过纪律`;
- no industry right-side share, coverage, aggregate right-side ratio, or
  industry-strength field.

The Mock is for display confirmation only. It has no real mutations, does not
replace production report rendering, and introduces no new UI interaction.

## Verification

Implementation is accepted only when:

1. Focused tests prove that early discipline failures are absent from every
   later paid request in CN, HK, and US.
2. Ranking tests freeze the new-version key, mixed stock/ETF competition,
   missing-global-strength behavior, and the 2026-08-07 order above.
3. Old-version tests prove historical ranking semantics remain unchanged.
4. Focused tests prove no eligible-industry component/member request is made
   and no removed industry field is required by report or Dashboard projection.
5. Report tests prove GRMN is planned rather than skipped, WTW shows the below-
   threshold reason, discipline failures appear last, and the empty normal buy-
   plan section is absent.
6. Each market replays its most recent 20 complete local trading-day artifacts
   without paid API calls. Differences are limited to the intended new-version
   order and its downstream plan priority; discipline sets, risk, exits, and
   rotation formulas remain unchanged.
7. Each market runs one latest-data direct workflow with real API data and
   submission disabled. The discipline set, ordering, audit, rotation, request
   trace, estimated cost, and actual debit are inspected.
8. All three cost-regression budgets pass. Any one-market failure blocks the
   three-version activation.
9. Relevant automated tests pass with exact output recorded.
10. Deploy the new versions with submission disabled, regenerate one latest
    immutable revision for CN, HK, and US, promote each revision to current, and
    verify that the prior revisions remain available.
11. `make acceptance` then runs once as the final Dashboard gate over the
    regenerated current revisions. Only `PASS` permits the exact accepted SHA
    to be redeployed and verified by PID, working directory, Git SHA, fresh
    logs, HTTP 200, and review URL.

## Non-goals

- changing discipline thresholds;
- changing risk, sizing, exit, drawdown, or rotation-comparison rules;
- optimizing or changing holding snapshot fields;
- approximate or cross-date market-data caching;
- a weighted composite score, quotas, or adaptive ranking optimizer;
- a new audit schema, cache service, or runtime hard cost cap;
- rewriting historical reports;
- using return, drawdown, or turnover outcomes as an optimization target.
