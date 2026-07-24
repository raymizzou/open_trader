# Task 3 report: eligible-industry context collection

## RED

The first focused collector test was written before the runner helper existed:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k collect_industry_contexts -v
```

Exact result:

```text
FAILED ...::test_collect_industry_contexts_queries_only_eligible_industries_and_unions_members
AttributeError: module 'open_trader.a_share_trend' has no attribute 'collect_industry_contexts'
```

## GREEN

Focused runner/context suite:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_industry_context.py -q
```

Exact result:

```text
362 passed in 1.53s
```

Receipt/history focused command:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'receipt or context_history' \
  tests/test_market_trend.py -k 'receipt or context_history' -v
```

Exact result: `14 passed, 331 deselected`.

Replay regression suite: `tests/test_trend_review.py` — `220 passed in 1.97s`.
Compilation and `git diff --check` both passed.

## Changes

- Added `INDUSTRY_MEMBER_FIELDS`, `INDUSTRY_STATE_FIELDS`, and the shared
  `collect_industry_contexts` helper in `a_share_trend.py`.
- CN keeps its temperature-only entry-gate snapshot; breadth/state collection
  now occurs after candidate and holding K-line evaluation and only for
  hard-gate-eligible industry IDs.
- US/HK use the same collector and shared `data/trend_industry_context`
  history root. Member IDs are unioned/deduplicated, state is one batch, and
  warm-to-hot counts use distinct original candidate-pool IDs.
- Balance-after is below every report-attributed Trend Animals request.
  Component-call estimates are marked incomplete unless the exact expected
  component events are same-date cache hits and all context fields have catalog
  prices; balance delta remains authoritative for actual cost.
- Frozen receipt delivery/recovery now writes context history without refetch;
  same-date history is idempotent and conflicting content is rejected.
- Extended CN/US/HK fake-client and history tests. No strategy, Kelly,
  Dashboard, or discipline-document changes.

## Self-review

- Industry component requests are keyed from the post-hard-gate candidate
  decision, so excluded-only industries are never queried.
- Context failures propagate through the existing report retry/failure path;
  invalid but complete current breadth still freezes a legacy-ordering report.
- Receipt recovery performs no paid API calls and validates history content
  again, so a conflicting same-date artifact cannot be silently accepted.
- `make acceptance` was not run: this is runner/data collection work, not a
  Dashboard task requiring the acceptance gate.

## Commit

Implementation and tests: `bdd1cd8397f6802c2b529f70b1bb583c73b822cd`

## Review fix

The review found that replacing `CandidateInput.industry_temperature` with
the later breadth state could re-run the CN entry hard gate and remove a
candidate whose early gate temperature was allowed. The runner now preserves
the early temperature; the later state remains frozen in `industry_contexts`.

RED regression:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k cn_entry_gate_keeps_early_temperature -v
```

Before the fix, the assertion failed because `top10_candidates` was `[]`
instead of `['000001', '000002']` while the later state was `平`.

GREEN covering runners:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q
```

Exact result: `346 passed in 1.35s`.

The context suite also passed: `17 passed in 0.03s`; compilation and
`git diff --check` passed.

Review-fix commit: `fa9d0699f9423b651bf40e507600f0b1045153f9`
