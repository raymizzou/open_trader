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
- For a read-only request, reuse the verified current HTML if it matches the
  latest applicable evidence. Otherwise render from available local evidence
  within the existing permissions. For a revision, carry forward all other
  still-valid entries. If there are no displayable entries, use the
  title-only template without making a claim about the absence of signals.
- For a known related trading instrument with verified identity, market,
  and selected group, show `查不到` when a completed applicable Trend Animals
  lookup confirms no match. Show `待核` when the lookup failed, was skipped,
  or remains unresolved. Keep the group visible when it has only lookup
  status entries. These labels give no signal, eligibility, or ranking
  conclusion.
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
