# Task 2 report: contextual candidate ordering and frozen report facts

## RED

Command:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'candidate and industr or payload' -v
```

Exact result before implementation:

```text
====================== 3 failed, 19 passed, 292 deselected in 0.29s =======================
```

The three new tests failed on the missing `industry_contexts` /
`estimated_api_cost_complete` arguments. The existing candidate and payload
tests passed.

## GREEN

Ordering command:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'candidate and industr' -v
```

Exact result:

```text
====================== 17 passed, 298 deselected in 0.26s =======================
```

Replay/payload command:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'payload or replay or industry_context' tests/test_trend_review.py -k 'rebuild and trend' -v
```

Exact result:

```text
====================== 5 passed, 530 deselected in 0.26s =======================
```

Relevant existing tests:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_trend_review.py -q
```

Exact result:

```text
535 passed in 3.14s
```

Additional syntax check:

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src/open_trader/a_share_trend.py src/open_trader/trend_review.py
```

Exact result: no output; exit status 0.

## Changes

- Added report-wide current-context validation and the four specified ordering
  modes while preserving `_candidate_sort_key` for legacy ordering.
- Added the deterministic contextual sort key using the official seven-value
  temperature order, frozen `TrendReport` context/status/cost-completeness
  fields, canonical `api_cost`, and per-candidate `ordering_context` facts.
- Added context and ordering facts to report evidence replay. This required the
  small `trend_review.py` change: without carrying these fields in
  `rebuild_inputs`, replay would recalculate legacy ordering and lose the
  frozen facts.
- No API runner, Dashboard UI, strategy-version, or cost-calculation changes.

## Self-review

- `git diff --check`: PASS.
- Hard-gate filtering still runs before contextual sorting.
- Missing/invalid current context falls back for the entire report and records
  affected IDs and reasons; missing prior history selects current-only mode.
- Contextual sort code is reached only after required context values are
  validated; invalid values are not serialized as zero placeholders.
- Legacy callers keep their original arguments and sorting behavior.

## Commit

Code and tests commit: `fd6c05342e7f0ab36094162b2163649978232eb0`

