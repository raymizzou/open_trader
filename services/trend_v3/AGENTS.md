# Trend V3

This service is an independent Python package. Keep runtime code and test
dependencies in this directory. The service uses its own `pyproject.toml`.
The freshness gate compares the class data date with the expected data date
supplied by the caller. An exact match only passes the freshness condition.
The caller owns calendar decisions and all other opening conditions.

For a private Trend V3 daily observation report, use the
[daily report skill](.agents/skills/trend-v3-daily-report/SKILL.md) from the
repository revision selected for the run.

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
