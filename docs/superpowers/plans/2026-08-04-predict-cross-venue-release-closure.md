# Predict Cross-Venue Release Closure Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release the already-built Predict.fun × Polymarket ordinary binary YES/NO flow without adding product scope.

**Architecture:** Keep the existing `PredictSource`, cross-venue matcher, execution coordinator, and Dashboard UI. This plan only corrects six verified production-boundary defects, proves the existing behavior, and publishes the accepted SHA.

**Tech Stack:** Python, pytest, Predict REST/SDK 0.0.22, Playwright, launchd, Git.

## Global Constraints

- Only Predict.fun × Polymarket cross-venue ordinary binary YES/NO is in scope.
- No same-venue arbitrage, new venue, NegRisk/yield-bearing support, redesign, pre-classification system, or architecture refactor.
- No live order, token transfer, allowance transaction, or real notification is part of acceptance.
- All transfers remain manual operator actions.
- Fixture/UI proof must not be described as live trading proof.
- `make acceptance` runs only after focused verification and review are clean.
- Only an acceptance `PASS` may be merged and deployed as complete.
- Findings outside this list are recorded for later; they are not implemented in this release.

---

### Task 1: Close the six verified production-boundary defects

**Files:**
- Modify: `src/open_trader/predict_source.py`
- Modify: `src/open_trader/predict_trading.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_predict_source.py`
- Test: `tests/test_predict_trading.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**
- Consumes: the existing Predict REST/SDK adapter and execution reconciliation flow.
- Produces: official-schema discovery, exact-order reconciliation, normalized amounts, and zero-mutation no-submit behavior.

- [ ] Accept the official Market response without requiring invented `marketType`, `collateralToken.symbol`, or `minimumOrderSize` fields; parsing failures must not become a healthy empty scan.
- [ ] Request subsequent market pages with `after` from the prior response cursor.
- [ ] Query matches by `orderHashes` and require the target order hash on the matching participant.
- [ ] Normalize Predict position and fee raw values from 18-decimal units.
- [ ] Block raw-chain mutation methods such as `send_raw_transaction` and `transact` in no-submit verification.
- [ ] After an order-by-hash 404, complete the independent order-list, match, activity, and position reads before declaring the order absent.
- [ ] Commit only these fixes and their regression tests.

### Task 2: Focused zero-mutation verification

**Files:**
- Verify: `tests/test_predict_source.py`
- Verify: `tests/test_predict_cross_venue.py`
- Verify: `tests/test_predict_trading.py`
- Verify: `tests/test_polymarket_trading.py`
- Verify: `tests/test_prediction_arbitrage_store.py`
- Verify: `tests/test_prediction_arbitrage_execution.py`
- Verify: `tests/test_notifications.py`
- Verify: `tests/test_dashboard_web.py`
- Verify: `tests/test_prediction_arbitrage_acceptance.py`
- Verify: `tests/test_dashboard_acceptance.py`
- Verify: `tests/test_account_api.py`
- Verify: `tests/e2e/prediction-market.spec.ts`

- [ ] Run the focused Python suite and require zero failures.
- [ ] Run the Chromium prediction-market scenarios and require zero failures.
- [ ] Run `prediction-arb preflight --no-submit` and require signer/wallet/readiness checks to pass without any mutation.
- [ ] Confirm no live order, transfer, allowance, or notification was produced.

### Task 3: One bounded independent review

**Files:**
- Review: all files changed from `main` to the candidate SHA.

- [ ] Ask one fresh reviewer to inspect the full branch against the frozen constraints and official Predict contract.
- [ ] Fix only Critical/Important findings that directly violate a listed acceptance condition.
- [ ] Record non-blocking or out-of-scope findings in the backlog without implementing them.
- [ ] Allow at most one correction round and one confirmation review; if an in-scope blocker remains, stop and report `BLOCKED` instead of expanding the design.

### Task 4: Final acceptance gate

**Files:**
- Verify: `Makefile`
- Verify: runtime services and Dashboard acceptance artifacts.

- [ ] Deploy the exact candidate SHA to all required local services.
- [ ] Run `make acceptance` as the final gate.
- [ ] On `FAIL`, fix only the regression that caused the gate failure; do not add features.
- [ ] On `BLOCKED`, stop and report the external/runtime blocker.
- [ ] On `PASS`, record the exact accepted SHA unchanged.

### Task 5: Publish the accepted SHA

**Files:**
- Verify: `CHANGELOG.md`

- [ ] Confirm the dated operator-facing changelog entry is committed.
- [ ] Fast-forward local `main` to the accepted SHA while preserving unrelated dirty-root files.
- [ ] Push `main` without force.
- [ ] Redeploy the same accepted SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200.
- [ ] Capture the requested existing feature states as screenshots; do not add new UI to manufacture evidence.

## Release Stop Condition

The release is done only when the frozen six defects are closed, focused verification and bounded review are clean, `make acceptance` is `PASS`, and the same SHA is on `main`, remote, and the verified runtime. Otherwise it is explicitly `FAIL` or `BLOCKED`; the scope does not grow.
