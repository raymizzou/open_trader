# CN v7 Kelly Sample Continuity Design

## Goal

Keep the existing CN v4 closed-round sample history when the relaxed CN entry
rules become v7. This is a one-time compatibility rule, not a general policy
for future strategy versions.

## Strategy identity

The relaxed CN strategy becomes `trend_animals_warm_to_hot/CN/v7`, effective
2026-07-24. A new version is required because v6 already has an audited
drawdown parameter identity.

For a v7 calculation, the accepted sample identities are exactly:

- `trend_animals_warm_to_hot/CN/v4` opened under v4
- `trend_animals_warm_to_hot/CN/v7` opened under v7

CN v1, v5, and v6 are excluded. All HK, US, and future strategy versions keep
the existing exact-identity behavior.

The v7 frozen strategy parameters will state this inheritance explicitly so
the report, drawdown parameter hash, and operator-facing discipline agree.

## Data flow

One shared identity-matching rule will be used by:

1. Kelly sizing, when it selects eligible simulation closed rounds.
2. Trend statistics, when it builds simulation and actual v7 summaries.

The rule is one-way: v7 sees v4 and v7 rounds, while a v4 report still sees
only v4 rounds. Existing fills, rounds, attribution, fees, and returns remain
unchanged. No sample is copied or relabelled, so round IDs remain unique and
auditable.

The Dashboard continues to request the exact current v7 statistics row. That
row is derived from v4 plus v7 rounds by the shared rule. Only simulation
rounds affect Kelly; actual rounds remain display-only.

After deployment, the existing statistics sync is rerun to derive v7 summary
rows from the unchanged source records. Today's CN report is then regenerated
against the audited v7 drawdown state.

## Failure behavior

All existing eligibility checks remain mandatory: complete costs, attributed
round, finite return, canonical close time, and simulation source for Kelly.
Malformed or duplicate eligible rounds continue to fail closed.

The compatibility rule matches the exact market, strategy IDs, and versions
listed above. It has no prefix matching or fallback that could silently admit
another version.

## Verification

Tests will prove:

- CN v7 includes eligible v4 and v7 rounds without duplication.
- CN v1, v5, and v6 rounds are excluded.
- CN v4, HK, US, and unrelated strategies remain exact-identity scoped.
- Simulation and actual v7 statistics use the same one-way inheritance rule.
- The generated v7 report exposes the inherited sample count and retains the
  relaxed entry rules.
- The Dashboard loads the v7 report and its derived trade statistics.

Final verification includes focused tests, the full test suite, a real
statistics sync, today's report regeneration, `make acceptance`, and
post-acceptance redeployment of the exact accepted SHA.
