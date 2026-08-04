# Task 15 report: Predict allowance and gas safeguards

## Current phase / blocker

- Phase: implementation and focused verification complete; ready to commit.
- Blocker: none.
- Acceptance: not run by instruction.

## Changed files

- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- `tests/e2e/serve_dashboard_fixture.py`
- `tests/e2e/prediction-market.spec.ts`
- `tests/test_dashboard_web.py`

## What changed

- Extended the existing YES/NO page renderers only:
  - `predictionReadinessStrip` now separates Predict Account USDT/allowance from Privy signer BNB.
  - `predictionCrossVenueFunnel` retains stale stage 1-4 counts/timestamps and renders healthy empty scans.
  - `predictionCrossVenueCandidateHtml` and `predictionModalHtml` show native IDs, official links, frozen bounds, timestamps, Codex/cutoff, caps, unsettled limits, and non-atomic warnings.
  - Existing modal/focus plumbing now supports a residual Predict allowance cleanup modal.
- Cleanup UI requires two human clicks and posts only `{"confirm":true}` to `/api/prediction-arbitrage/predict-allowance/cleanup`.
- Added manual BNB top-up guidance as copyable text and official links only; no wallet/transfer action is created.
- Preserved existing shared header, tabs, warm palette, spacing, hierarchy, LLM hedge rendering, and legacy YES/NO behavior.

## Red commands and outputs

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_dashboard_web.py -k 'prediction' -q
```

Initial red output:

```text
1 failed, 74 passed, 260 deselected, 1 warning in 13.43s
FAILED tests/test_dashboard_web.py::test_prediction_allowance_gas_cleanup_and_cross_order_facts_render
ReferenceError: predictionSafeguardsHtml is not defined
```

```bash
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Initial red output:

```text
3 failed
[chromium] › tests/e2e/prediction-market.spec.ts:33:7 › renders shared venue truth and protects the cross-venue confirmation flow
[chromium] › tests/e2e/prediction-market.spec.ts:105:7 › covers final allowance gas and scan fixture states on desktop and mobile
[chromium] › tests/e2e/prediction-market.spec.ts:138:7 › cleans residual Predict allowance only after a second confirmation
33 passed
```

## Green commands and exact outputs

```bash
git diff --check
```

Output: no output; exit code 0.

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_dashboard_web.py -k 'prediction' -q
```

Output:

```text
........................................................................ [ 96%]
...                                                                      [100%]
75 passed, 260 deselected, 1 warning in 12.94s
```

```bash
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Output:

```text
36 passed (54.7s)
```

## Fixture / browser coverage

Covered at desktop 1440 and mobile 375:

- ready with zero Predict allowance
- insufficient Privy signer BNB with no signal
- signer BNB blocking a stage-5 signal
- residual allowance / breaker
- cleanup confirmation, success, and failure fixture states
- stale venue retaining stages 1-4 while stage 5 is zero
- healthy complete zero-market scan
- active first-canary 5 cap and completed-canary normal 20 cap
- post-approval cancellation as `未下单 · 授权已清零`
- grouped lifecycle history with approval/orders/reconciliation/cleanup receipts

Browser assertions also cover 44px visible buttons, no horizontal overflow, Escape/focus restoration, no countdown copy, and cleanup posting exactly `{"confirm":true}` after the second confirmation.

## Self-review / concerns

- Standards: no new dependency, framework, component architecture, or live mutation path. CSS additions reuse existing palette/tokens and target only the new safety bits.
- Spec: implemented only manual BNB guidance and residual allowance cleanup confirmation. No order, approval, transfer, redemption, notification, or acceptance run was added.
- Concern: the cross-order modal now carries more facts in long existing template strings. This is intentionally minimal for Task 15, but if the page keeps growing, the next task should split only the repeated leg fact markup into a small local helper.
