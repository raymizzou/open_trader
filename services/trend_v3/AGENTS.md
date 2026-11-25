# Trend V3

This service is an independent Python package. Keep runtime code and test
dependencies in this directory. The service uses its own `pyproject.toml`.
The freshness gate compares the class data date with the expected data date
supplied by the caller. An exact match only passes the freshness condition.
The caller owns calendar decisions and all other opening conditions.

## Daily report delivery

- In every session or tab that delivers a Trend V3 report to Ray, use the
  [daily report skill](.agents/skills/trend-v3-daily-report/SKILL.md) from the
  selected repository revision. Deliver the entire current report as one HTML
  file in the existing `trend_v3` iCloud folder. Link to that file in the
  delivering session. Use that link as the sole delivery, even for a
  single-item correction.
- Deliver the first report at Taipei 08:30 from available evidence, including
  missing data. Scheduled sessions then use the pinned skill collection.py
  control state to check once per elapsed hourly slot until all frozen target
  evidence is verified and the final HTML is published and read back. Never
  delay the initial report, replay missed slots, or poll from normal read/offline
  entry points. Show the report date once in the heading and only overall status plus the next check or confirmation time in the header. Keep pending information on its affected class or card.
- Use the selected revision's [V1 report renderer](.agents/skills/trend-v3-daily-report/render_report.py)
  for scheduled, manual, offline, and revision work. The dated masthead links
  to the short, long, live, simulation, and Kelly sections in that order.
  For threshold-v1 reports, keep every verified class with strength ≤20
  (short) or ≥80 (long), including zero and boundary values, checked against
  an independently frozen complete financial-assets board. Legacy reports
  retain their original bottom two and top two seats. Each class expands to three labelled blocks, in order: `大类直属`,
  `趋势动物里面能够查到`, and `相关的标的`, including empty blocks.
  Verified membership wins over an exact platform match, which wins over
  other evidence-backed related instruments. Show each instrument once per
  class, with explicit `直属` / `趋势动物可查` markers where verified.
  Keep watch eligibility independent. Put general `coverage_note` text after
  all three blocks even when no direct cards are known; retain the older
  direct-only partial note for compatible inputs. Never infer membership or an exact match from legacy
  list placement, class identity, or a missing strength/date.
  Show the verified direction seat and board strength with the class. Show both
  global and same-asset-category strengths side by side on every
  instrument card, preserving source precision and zero. Select both fields for
  every exact platform match, independently of opening signals or comparison
  groups. Reuse each verified applicable-version cache field separately and merge
  missing fields into authorized requests under the shared ledger rules. A
  settled temperature-only ETF response does not cover missing strengths;
  separately confirmed existing authority may permit their retrieval after
  ledger checks. Keep the BTC-specific standalone-request restriction scoped.
  Consolidate independently verified split strength facts before projection, or
  use the optional per-field evidence contract in the daily report skill. Only
  matching explicit identities, applicable dates and versions may bridge rows;
  conflicts and withdrawals mask that metric independently. Never inherit
  trend, events or eligibility from duplicate quote rows.
  Use only global strength for direction sorting; give each missing field its
  own public reason. Apply this to scheduled, manual and offline entry points.
  Lookup-only cards mask both strengths; invalid dates or identity resolution
  must never restore stale quotes, strengths, events or watch eligibility. Keep
  both
  direction panels when evidence is missing. The live reserved block and both Kelly values retain `未接入`.
  The simulation section also retains that placeholder for old reports and
  reports without verified paper-entry status. The official scheduled tab can
  display the current redacted output of the same-version
  [paper-entry skill](.agents/skills/trend-v3-paper-entry/SKILL.md).
  Use whole-card green only when the actual quote day matches the applicable
  market target validated by original-report-day calendar evidence. Stale,
  missing, invalid, future or contradictory dates use yellow. Show one actual
  quote date per card; show the target only for a known stale date. Green is a
  quote-date conclusion, independent of signals, watch eligibility and report
  completion. Never infer a target from wall-clock or fetch time. Keep field
  dates, source paths and collection diagnostics private.
- For a read-only request, reuse the verified current HTML when it comes from
  the effective template and renderer and matches the HTML rendered from the
  latest applicable local evidence. Otherwise render the complete V1 report
  within the existing permissions. Revisions preserve still-valid facts and
  entries and render the full report.
- For an instrument with verified identity, market, and selected group, show
  `查不到` in the trend and strength values when a completed applicable
  Trend Animals lookup confirms an exact match is absent from the entire
  applicable result. Show `待核` in those values when the lookup failed, was
  skipped, has incomplete pages, or remains unresolved. The reason may give
  verified, publishable provenance or explain the status. It defaults to the
  same status label. Keep verified values on ordinary cards and show `待核`
  for unavailable trend and strength. The special-signal cell lists distinct
  confirmed event names joined by `，`. It shows `无` when no confirmed event
  can be listed and absence is confirmed. Missing or unverified signal evidence
  displays `待查`. Legacy verified display projections remain readable. Keep lookup and coverage
  evidence for eligibility and ranking in the underlying records.
- Follow the existing data and fee authorizations. This delivery rule grants
  no authority for trading, additional fees, or additional data access.

## Implementation

For every new assigned Trend V3 task, Main creates a fresh implementation
worker after plan approval. The worker implements the full assigned batch in
the isolated task worktree using GPT-6.2-Sol, high reasoning and Fast
(priority). Reuse that worker for in-scope corrections to the same task.
Main reads the actual code, tests and diff, verifies the evidence, and assigns
in-scope corrections. Main owns acceptance, all Git writes and PR delivery.
The worker uses Git read-only.

Run the service checks from this directory with Python 3.12:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m ruff check src tests
.venv/bin/python -m pytest tests
```

### Herdr pane cleanup

Main checks task-owned Herdr panes after each worker handoff and when a phase
ends. Close a worker pane promptly once its assigned work and repairs are
complete or a named owner has accepted the handoff. Before closing, confirm
that output and evidence are saved, all commands have finished, and the live
pane belongs to this task. Verify the closure in Herdr.

Keep workers with active repairs. Keep daily interactive report tabs for Ray's
follow-up until Ray asks to close them. Leave user panes and panes from other
tasks alone. Follow the separate repository rules for deleting a worktree or
branch.

## PR review and merge

Main's implementation checks are separate from independent PR review. Follow
the [repository PR workflow](../../AGENTS.md#review-and-merge) for external
review and exact-candidate `CI / required` evidence. Ray must explicitly
authorize the merge for the current PR head.

## Deployment on Mini

For a deployable Trend V3 B1 service, use this sequence:

1. Obtain Ray's explicit approval to deploy the exact GitHub main SHA to Mini.
2. On Mini, fetch GitHub main, check out the approved SHA, and verify that
   the checkout matches it.
3. Install Trend V3 from that checkout and run its unit tests.
4. After the unit tests pass, reload the Trend V3 launchd job.

The current slice contains the package and unit tests and has no launchd job.
Existing OpenTrader releases use the
[full-evidence deployment path](../../docs/operations/deployment-preflight.md).
