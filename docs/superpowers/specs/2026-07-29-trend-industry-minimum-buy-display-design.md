# Trend Industry Minimum and Buy Display Fix Design

**Date:** 2026-07-29
**Status:** Approved in conversation
**Markets:** CN, US, HK

## Problem

The local industry-context validator rejects an otherwise complete context when
it has fewer than 10 components or fewer than 10 valid rows. For the U.S.
medical ETF group, two complete rows therefore invalidate the whole report's
industry ordering and restore individual-only ordering.

Separately, HK and US buy rows freeze `industry_temperature` as null even when
the same report contains a matching frozen industry context with a valid
temperature. The Dashboard consequently renders `数据未提供`.

## Design

Remove the local `component_count >= 10` and `valid_count >= 10` requirements.
Keep exact-date matching, snapshot coverage, right-state coverage, known
temperature, and finite strength validation unchanged. Counts and ratios remain
visible, including small denominators such as `2 / 2`; the Dashboard must not
present a small sample as missing data.

Keep the existing deterministic contextual ordering unchanged. Once the
complete two-member ETF context is valid, all three markets use the existing
industry-first keys followed by individual strength and the existing tie
breakers. Genuine missing, stale, low-coverage, or malformed context still
triggers the existing report-wide fallback.

For buy-plan display, resolve the industry context already frozen in the report
by `industry_tm_id`, with the existing industry-name fallback. Render the
context temperature when the buy action's direct `industry_temperature` is
missing. A direct action value remains authoritative when present, and missing
or invalid context remains explicitly unavailable.

## Scope

- Remove the two local minimum-count validation reasons and their tests.
- Update the earlier industry-breadth design so documentation matches behavior.
- Add a regression test proving a complete two-member context participates in
  contextual ordering.
- Add a cross-market Dashboard regression test proving buy rows display the
  matching frozen context temperature.
- Regenerate and compare CN, HK, and US reports without submitting trades.

No new score, threshold, dependency, ETF-to-sector remapping, entry gate,
position-sizing rule, or risk rule is introduced.

## Verification

Use red-green tests at the industry calculation, candidate ordering, and
Dashboard rendering seams. Then run focused suites, regenerate all three market
reports in no-submit/revision mode, compare action ordering, run
`make acceptance` once as the final gate, and redeploy the exact accepted SHA.
