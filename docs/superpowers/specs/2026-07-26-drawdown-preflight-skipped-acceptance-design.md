# Drawdown Preflight Skipped Acceptance Design

## Problem

`make acceptance` currently treats every `trend-drawdown-preflight` exit code
`2` as `BLOCKED`. That conflates two different situations:

- the completed-date frozen Futu baseline does not exist, so the drawdown
  assertion cannot be performed;
- Futu, its trading calendar, or another required external dependency is
  unavailable, so the environment cannot be evaluated.

A completely fresh installation can therefore never pass Dashboard acceptance
until a matching frozen baseline or audited drawdown state already exists.
Dashboard behavior may be valid even though the unrelated drawdown assertion
has no data to inspect.

## Goal

Allow Dashboard acceptance to continue when the only missing prerequisite is a
nonexistent completed-date frozen baseline. The skipped assertion must remain
visible in output and must not weaken live trading safety.

## Non-goals

- Do not change the 5% cumulative-drawdown rule.
- Do not synthesize a baseline from live or partial account values.
- Do not change strategy versions, predecessor inheritance, parameter identity,
  notifications, or controller entry checks.
- Do not make Futu outages, malformed artifacts, or corrupt drawdown state
  acceptable.
- Do not split the public acceptance workflow into additional operator commands.

## Acceptance Status Model

The drawdown preflight module owns status classification. `Makefile` continues
to consume only the command exit code.

| Condition | Market status | CLI exit effect | Acceptance effect |
| --- | --- | --- | --- |
| Audited state already exists | `ready` | success | continue |
| State was initialized from a valid frozen baseline | `bootstrapped` | success | continue |
| State was restored from an audited snapshot | `recovered` | success | continue |
| No matching completed-date frozen baseline exists | `skipped` | success | continue |
| Futu or trading-calendar access is unavailable | `unavailable` | exit `2` | `BLOCKED` |
| Matching data is malformed, invalid, or inconsistent | `failed` | exit `1` | `FAIL` |

Overall status precedence is:

```text
failed > unavailable > ready
```

`ready` is the overall status when every market is one of `ready`,
`bootstrapped`, `recovered`, or `skipped`. Individual skipped markets remain in
the JSON result; they are not relabeled as ready.

## Frozen Baseline Interface

The current `frozen_missing_baseline(...) -> Decimal | None` interface cannot
distinguish absence from invalid data. Replace that ambiguity at the existing
loader seam with three explicit outcomes:

- `available`: a matching market, strategy identity, strategy version, source
  date, positive account net value, and `drawdown_summary.state_status ==
  "missing"` were found;
- `missing`: the completed-date report files are readable, but none contains the
  requested current strategy identity and version. This includes a completed
  date that has reports only for older strategy versions;
- `invalid`: a completed-date report file is unreadable or malformed, or a
  report claiming the requested current strategy identity and version has an
  invalid source date, net value, or drawdown status.

Only `missing` becomes `skipped`. `invalid` becomes `failed`.

The implementation should use the smallest local representation that keeps the
three outcomes explicit. It must not introduce a general result framework or a
new cross-package abstraction.

## Data Flow

For each CN, HK, and US market:

1. Load the trading calendar and derive the latest completed date and next
   eligible entry date.
2. Build the effective strategy snapshot for the entry date.
3. If an audited state record already exists for that strategy identity,
   validate and reuse it; no new baseline is required.
4. Otherwise inspect frozen reports for the completed-date baseline.
5. Classify the result:
   - valid baseline: initialize or inherit through the existing preflight flow;
   - no matching baseline: emit `skipped` with
     `reason = "baseline_missing"` and do not mutate drawdown state;
   - invalid candidate: emit `failed`;
   - dependency failure before classification: emit `unavailable`.
6. Aggregate the market results and return the corresponding CLI exit code.

The preflight JSON for a skipped market includes:

```json
{
  "market": "CN",
  "status": "skipped",
  "reason": "baseline_missing",
  "source_date": "2026-07-24"
}
```

This gives operators and CI evidence that the assertion did not run.

## `make acceptance` Behavior

The acceptance sequence remains:

1. full automated tests;
2. drawdown preflight;
3. live Dashboard acceptance.

Because a baseline-only skip exits successfully, step 3 still runs. The
preflight JSON visibly records skipped markets, and the final Dashboard gate may
return `PASS`.

Exit `2` remains `BLOCKED`; all other nonzero exits remain `FAIL`. The Makefile
must not inspect error strings or duplicate preflight classification.

## Safety Invariants

Skipping the acceptance assertion does not create a baseline, mark drawdown
state healthy, or authorize entries. Runtime strategy and controller checks
continue to fail closed while required drawdown state is absent.

A skip is allowed only for proven absence. Existing but malformed or
inconsistent artifacts are failures because treating them as absent could hide
data corruption.

## Verification

Automated coverage must prove:

1. no matching frozen baseline produces a visible per-market `skipped` result,
   overall success, and CLI exit `0`;
2. a mixture of ready and skipped markets succeeds without losing market-level
   details;
3. Futu connection or trading-calendar failure remains `unavailable`, CLI exit
   `2`, and `BLOCKED`;
4. malformed or invalid matching baseline data produces `failed`, CLI exit `1`,
   and `FAIL`;
5. an existing audited state remains ready without requiring a fresh baseline;
6. a skipped preflight does not create or modify drawdown state;
7. `make acceptance` continues into live Dashboard acceptance after a
   baseline-only skip.

The final live check must exercise both environments: one with a valid baseline
and one with no matching baseline. Neither may use live account net value as a
historical substitute.
