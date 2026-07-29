# Trend Industry Breadth, Sorting, Cost, and Discipline Dashboard Design

**Date:** 2026-07-24
**Status:** Approved in conversation
**Markets:** CN, US, HK

**2026-07-29 amendment:** Complete small industry groups no longer fail a
minimum component or valid-row count. See
`2026-07-29-trend-industry-minimum-buy-display-design.md`.

## Goal

Improve warm-to-hot candidate ordering with auditable industry context, show the
exact right-side breadth movement when history exists, report each market
report's own Trend Animals API cost, and redesign the report so the frozen
discipline is a first-class dashboard rather than two long lists.

The change must preserve the existing entry gates, position sizing, risk
limits, protection lines, exit rules, action workflow, process controls, and
audit evidence.

## Confirmed Product Decisions

1. Candidate context is a secondary ordering layer. It never allows a candidate
   to bypass an existing hard gate.
2. CN, US, and HK remain separate reports. There is no cross-market fee total.
3. The ordering is deterministic and lexicographic. There is no weighted score.
4. If current industry context is incomplete for any eligible candidate, the
   entire report uses the prior individual-only ordering. The report never
   mixes contextual and legacy ordering.
5. Previous-day criteria are used only when every eligible industry has a valid
   prior observation. Otherwise those criteria are omitted for the whole
   report.
6. The approved dashboard direction is the **discipline lifecycle card**
   layout, with all existing report components retained.
7. New strategy versions continue the existing 30-trade review samples rather
   than restarting them.

## Verified API Facts

The authorized live sampling established these current facts:

- For warm-to-hot pool `622466`, `getAllBasicComponentsFlag=0` and `1` both
  returned the same 11 current components. The two live component calls cost
  `0.155` Trend Animals balance units in total.
- For industry `621707` (`化学制药`), both flag values returned the same 157
  current A-share components. The two live component calls cost `0.262`
  balance units in total.
- Therefore the warm-to-hot candidate pool is not a valid full-industry
  denominator, but an industry's own `industryTmId` is a usable full-industry
  component source.
- The current billing catalog prices the minimum member breadth fields as:
  `tradableFlag = 0.001` and `isTrendRightSide = 0.002` per instrument.
  Identifier and date fields are free.
- The catalog prices `trendTemperatureCurr` and
  `trendStrengthLocalCurr` at `0.004` each per queried industry instrument.
- For the sampled 157-member industry, one component call plus the minimum
  breadth and industry-state fields is approximately `0.610` balance units at
  the observed component-call price. This is an operational estimate, not a
  hard-coded billing rule.

The public API does not accept a historical date parameter. Historical
comparisons must be built from locally frozen daily observations going
forward.

## Scope

### In scope

- Industry temperature direction and current temperature.
- Industry current local trend strength.
- Industry warm-to-hot candidate count.
- Industry right-side count, valid denominator, share, prior share, and
  percentage-point change.
- Deterministic contextual ordering for eligible candidates.
- A dated industry-context artifact for future comparisons.
- Strategy-version and sample-inheritance updates.
- Per-report API-cost presentation.
- Discipline lifecycle cards generated from the report's frozen strategy
  snapshot.
- Re-layout of all existing Dashboard report components on desktop and mobile.
- Markdown, JSON, Dashboard, and Feishu report consistency.

### Out of scope

- Changing entry gates, target weights, Kelly math, risk budgets, drawdown
  rules, protection lines, exit rules, or execution windows.
- A new market-wide or broad-category breadth subscription. Every candidate in
  one report already shares the same market category, so it cannot distinguish
  candidates inside that report.
- Using the warm-to-hot source pool's own heat as an extra score. That would be
  circular because the pool is already the candidate source.
- Historical backfill from the current universe.
- A fixed “good breadth” threshold such as 18%.
- A weighted composite score, tuning UI, new chart library, new frontend
  framework, or new persistent database.

## Industry Context Collection

### Collection boundary

The existing candidate and holding snapshots run first. Existing hard-gate
logic identifies the eligible candidate set. Industry breadth is then queried
only for the distinct `industryTmId` values used by those eligible candidates.

This boundary avoids paying for industries that cannot affect a buy decision.
The existing same-date response cache remains authoritative for reruns.

### Requests

For every distinct eligible industry:

1. Reuse `TrendAnimalsClient.get_components()` with the industry `tmId` and the
   report's `asOfDate`. Keep the client's existing
   `getAllBasicComponentsFlag=0`; sampling showed no difference for the tested
   industry.
2. Union and deduplicate all returned member `tmId` values across eligible
   industries.
3. Issue one minimal member snapshot request for:
   - `tmId`
   - `asOfDate`
   - `tradableFlag`
   - `isTrendRightSide`
4. Issue one minimal industry snapshot request for:
   - `tmId`
   - `asOfDate`
   - `trendTemperatureCurr`
   - `trendStrengthLocalCurr`

The existing candidate snapshots already contain the prior/current instrument
temperature and industry ID. Warm-to-hot counts require no additional paid
fields.

### Current-day calculations

For each industry:

- `component_count`: distinct members returned by the industry component call.
- `snapshot_count`: distinct current-date member snapshot rows.
- `tradable_count`: rows with `tradableFlag=true`.
- `valid_count`: tradable rows whose `isTrendRightSide` value is a boolean.
- `right_count`: valid rows with `isTrendRightSide=true`.
- `right_share = right_count / valid_count`.
- `warm_to_hot_count`: distinct current candidate-pool instruments in that
  industry whose transition is exactly `温 → 热` or `温 → 沸`, before the other
  hard gates are applied.
- `temperature`: the industry instrument's current temperature.
- `strength`: the industry instrument's current local strength.

The report displays exact counts with the share, for example:

`34 / 122 = 27.9%`

### Data quality

An industry's current context is valid only when:

- all rows use the report's exact `asOfDate`;
- `snapshot_count / component_count >= 90%`;
- at least 90% of currently tradable rows have a boolean right-side state;
- the industry temperature is a known Trend Animals temperature; and
- industry strength is finite and between 0 and 100 inclusive.

Invalid current context does not silently become zero.

If any eligible industry fails current validation, all eligible candidates use
the existing individual-only ordering. The report still records the invalid
industry, reason, counts, and coverage for audit.

## Daily History

Each successful report freezes one dated, market-specific industry-context
artifact. It contains:

- schema version, market, data date, generation time, and strategy version;
- industry ID and name;
- all current counts, coverage values, temperature, strength, and share;
- the latest earlier locally stored observation used for comparison; and
- the derived temperature direction and right-share percentage-point change.

The prior observation is the latest valid earlier trading-data date, not the
previous calendar day.

For a given report, previous-day ordering criteria are enabled only when all
eligible industries have a valid prior observation. Otherwise the report
omits temperature direction and breadth change for every candidate while
retaining the current temperature, strength, warm-to-hot count, and current
right-side share.

No historical value is inferred from today's universe.

## Deterministic Ordering

Existing hard gates run first. Eligible, not-already-held candidates are then
ordered by the following keys:

1. Industry temperature direction: rising, unchanged, falling.
2. Current industry temperature, using the official order
   `冻 < 寒 < 凉 < 平 < 温 < 热 < 沸`.
3. Industry local trend strength, descending.
4. Industry warm-to-hot count, descending.
5. Industry right-side share change in percentage points, descending.
6. Current industry right-side share, descending.
7. Individual local trend strength, descending.
8. Individual right-side days, ascending.
9. Individual one-day amount, descending.
10. Symbol, ascending.

Keys 1 and 5 are omitted for the whole report when complete prior context is
not available. Keys 1 through 6 are omitted for the whole report when current
context is invalid, which restores the existing keys 7 through 10 exactly.

The candidate limit, cash and seat processing, and “consider the next ranked
candidate when a higher candidate cannot form a valid buy action” behavior do
not change.

An industry at `沸` can rank ahead of `热`, but this does not increase position
size. Existing per-instrument overheat handling and the CN `沸` target weight
remain unchanged.

## Strategy Versions and Sample Continuity

This selection change is versioned rather than silently modifying existing
strategy facts:

- CN becomes `v8`.
- US becomes `v5`.
- HK becomes `v5`.

The user explicitly approved continuing the existing 30-trade review samples:

- CN v8 matches eligible samples opened under CN v4, v7, or v8.
- US v5 matches eligible samples opened under US v4 or v5.
- HK v5 matches eligible samples opened under HK v4 or v5.

The inheritance is explicit and non-recursive in the machine strategy
contract. Each new strategy snapshot lists every inherited identity. Reports
and trade statistics display the contributing opening strategy versions so
the cross-version sample is not hidden.

CN v5 and v6 remain excluded from CN's sample. Older US and HK versions remain
excluded.

The human A-share discipline document advances from document version 1 to
document version 2 and records the CN v8 machine version, new data fields,
ordering, fallback, fee display, and the approved sample-continuity exception.

## API Cost Semantics

Each market report retains its own balance boundary:

`actual_api_cost = balance_before - balance_after`

when the result is finite and nonnegative. The boundary must include candidate,
industry, and industry-member requests made for that report.

The snapshot billing catalog continues to produce an estimate for paid
snapshot fields. The public catalog does not currently provide a contractual
component-call price, so an estimate that excludes component fees is marked
incomplete rather than presented as a total.

Presentation rules:

- Actual available: `本报告 API 费用：实扣 X`
- Actual unavailable, complete estimate available:
  `本报告 API 费用：估算 X（实扣不可得）`
- Actual unavailable and component fees are not estimable:
  `本报告 API 费用：未知（快照估算 X；成分费用未计）`

The unit is `Trend Animals 余额单位`; the UI does not invent a currency symbol.

The same semantics appear in JSON, Markdown, the Dashboard report header, and
the Feishu daily report. Audit details preserve both raw actual and estimated
fields plus estimate completeness.

Manual probes made outside report generation are not attributed to a market
report.

## Discipline Dashboard

### Source of truth

The lifecycle cards render the selected historical report's frozen
`strategy_snapshot.parameter_rows`. They do not use hard-coded current rules.
Opening an old report therefore shows the discipline that created that report.

The existing parameter groups map to the approved lifecycle:

- `候选来源` and `入场过滤` → **入场硬门槛**
- `候选排序` → **确定性排序**
- `仓位执行` → **仓位与执行**
- protection establishment and tracking rows from `退出保护` →
  **持有管理**
- forced-exit and partial-profit rows from `退出保护` → **退出纪律**
- `累计回撤` remains in the existing risk summary

Every card shows its key frozen rules without expansion. Expanding the card
shows all exact parameter labels and values, current-day evidence, and the
number of candidates or positions affected.

### Desktop order

1. Report identity, dates, strategy version, account state, history/back
   actions, and per-report API cost.
2. Existing buy/sell/review/hold/seat metrics.
3. Existing controller and process status as a compact strip.
4. Discipline lifecycle cards.
5. Industry earning-effect context beside today's action priority.
6. Existing risk summary, drawdown facts, and simulation overlay.
7. Existing formal buy plan with industry confirmation columns.
8. Existing sell, review, hold, candidate fallback, and risk-skip sections.
9. Existing audit details, exclusion reasons, industry concentration, data
   sources, artifact identity, and raw API facts.

### Mobile order

At 760 px and below:

1. Report identity, compact counts, and API cost.
2. Urgent sell and review actions.
3. Single-column discipline lifecycle cards.
4. Industry context.
5. Buy actions and remaining holdings.
6. Risk and simulation facts.
7. Audit details.

This mobile exception prevents urgent exits from being pushed below a long
discipline section.

### Visual constraints

- Reuse existing warm semantic tokens, typography, border radii, and focus
  styles.
- Use CSS Grid and native `<details>/<summary>` only.
- Add no frontend dependency, custom chart library, new font, gradient,
  decorative animation, or new color system.
- All statuses include text; color is never the only signal.
- Interactive summaries and buttons remain at least 44 px tall with visible
  keyboard focus.
- Desktop values use tabular figures where useful.
- At 375 px there is no page-level horizontal overflow.

## Report Payload

The frozen report remains the source of truth. It gains only the minimum
additional facts needed for replay:

- market-level industry-context status and fallback reason;
- the dated industry-context records used for ordering;
- each candidate's resolved context and contextual ordering fields;
- estimated-cost completeness; and
- the new strategy version and explicit sample-inheritance identities.

The Dashboard projection also includes the frozen strategy parameter rows
needed to render lifecycle cards. It does not fetch live discipline rules.

Historical and latest report endpoints use the same projection.

## Failure and Recovery

- Industry component or snapshot API failure follows the existing report retry
  behavior.
- Complete current API responses with invalid breadth data do not fail the
  report; they produce an explicit contextual-ordering fallback.
- Missing prior history is a normal bootstrap state, not an error.
- A stale or wrong-date row is never used in current or prior calculations.
- Invalid current context never becomes zero and never partially reorders the
  candidate list.
- Cached same-date responses prevent paid duplication on report recovery and
  revision runs.
- Receipt recovery continues to freeze and deliver the already prepared
  report; it does not recompute breadth.
- Existing process locks and controller ownership remain unchanged.

## Verification

Implementation follows test-driven development.

Focused automated coverage must prove:

- valid denominator, share, coverage, temperature direction, and
  percentage-point calculations;
- unique member and candidate counting;
- exact contextual key order and legacy fallback;
- whole-report omission of previous-day keys when any prior context is absent;
- stale, missing, and malformed industry data handling;
- estimate completeness and all three cost labels;
- CN v8, US v5, and HK v5 sample identity matching;
- explicit exclusion of unapproved historical versions;
- historical reports render their own frozen discipline rows;
- all existing report components remain present;
- desktop and mobile discipline order, keyboard operation, 44 px targets, and
  no horizontal overflow.

Before final acceptance:

1. Run focused tests during development.
2. Run one direct current-date report workflow per affected market when
   practical, using same-date caches and recording the actual report fee.
3. Inspect controller, `screen`, and `launchctl` state for old code.
4. Restart affected long-running processes on the new SHA.
5. Verify fresh PID, working directory, SHA, logs, and report output.
6. Run `make acceptance` exactly once as the final Dashboard gate.
7. Only after `PASS`, redeploy the exact accepted SHA and verify the review URL
   returns HTTP 200.

`FAIL` is fixed and rerun. `BLOCKED` is reported as blocked and is not replaced
with fixture or curl-only evidence.

## Success Criteria

- Two eligible candidates in different industries are ordered by the approved
  industry context before the existing individual keys.
- The report shows exact right counts, denominators, shares, and day-over-day
  change when valid history exists.
- The first valid day shows current context without inventing historical
  change.
- Any invalid current eligible-industry context restores the prior ordering for
  the entire report.
- Every CN, US, and HK report shows only its own API cost with an honest
  actual/estimate label.
- The lifecycle discipline is visible near the top of every report while all
  existing report components remain available.
- Historical reports show their own frozen discipline.
- Approved cross-version sample continuity is visible and testable.
