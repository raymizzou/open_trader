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
- For a read-only request, reuse the verified current, complete HTML generated
  by the skill. For a revision, carry forward all other current sections and
  entries that remain verified. Apply the correction to the complete report.
- When a lookup for a known related trading instrument completes and verifies
  that Trend Animals returned no match, keep the instrument in its group with
  its verified identity and the exact reader-visible text `查不到`. Keep that
  group visible even if it has no other entries. This display is a narrow
  exception to the older skill whitelist for instrument cards and groups
  without cards. `查不到` means the source returned no match for that lookup.
  Treat it as missing source data and draw no conclusions about signals,
  eligibility, or ranking. A failed or unattempted lookup remains unknown.
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
