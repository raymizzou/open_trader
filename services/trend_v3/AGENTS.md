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
- Make one scheduled pass per Taipei calendar day at 08:30, or once when
  started late. Perform the authorized retrieval and instrument checks for
  that pass, then write the report from available evidence. Never check
  report-wide data completeness or poll for more data before delivery.
- Use the selected revision's [V1 report renderer](.agents/skills/trend-v3-daily-report/render_report.py)
  for scheduled, manual, offline, and revision work. The dated masthead links
  to the short, long, live, simulation, and Kelly sections in that order.
  Keep each verified class from the market board's bottom two (short) and top
  two (long) seats. Each class expands to three labelled blocks, in order: `大类直属`,
  `趋势动物里面能够查到`, and `相关的标的`, including empty blocks.
  Verified membership wins over an exact platform match, which wins over
  other evidence-backed related instruments. Show each instrument once per
  class, with explicit `直属` / `趋势动物可查` markers where verified.
  Keep watch eligibility independent. Put general `coverage_note` text after
  all three blocks even when no direct cards are known; retain the older
  direct-only partial note for compatible inputs. Never infer membership or an exact match from legacy
  list placement, class identity, or a missing strength/date.
  Show the verified direction seat and board strength with the class. Show
  instrument strength with its card. For BTC spot observation, use the verified
  same-asset-category strength for the applicable provider data date. Keep both
  direction panels when evidence is missing. The live reserved block and the
  simulation section each show a
  `positions-note` paragraph with `未接入`. The two Kelly values each show
  `未接入`.
  Only individual instrument cards may change visually without Ray's approval.
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
  can be listed, including on lookup-only cards. Keep lookup and coverage
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
