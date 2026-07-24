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

## Follow-up review fixes

The review regression tests were written first and failed before the fix:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'external_context_status or non_candidate_contexts' -v
```

Exact result:

```text
====================== 3 failed, 315 deselected in 0.36s =======================
```

The failures covered a caller-supplied contextual status being retained when
the current context was missing, and invalid/no-history contexts unrelated to
eligible candidates changing the report mode.

After the narrow fix:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'external_context_status or non_candidate_contexts' -v
```

Exact result:

```text
====================== 3 passed, 315 deselected in 0.26s =======================
```

Required coverage command:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'candidate and industr or payload' tests/test_trend_review.py -k 'rebuild and trend' -q
```

Exact result:

```text
5 passed, 533 deselected in 0.26s
```

Full focused two-file suite:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_trend_review.py -q
```

Exact result:

```text
538 passed in 4.16s
```

The final status now always comes from the actual candidate decision; a
caller-supplied status can contribute only non-canonical extra facts when its
mode agrees. Current validation and history completeness inspect only industry
IDs referenced by eligible candidates, while all supplied contexts remain
frozen in the report. Follow-up fix commit: `88b3c867a6e4501272b578d5a9ed5d83f2dc7f47`.
