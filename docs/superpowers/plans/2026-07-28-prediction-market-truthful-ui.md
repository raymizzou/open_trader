# Prediction Market Truthful UI Implementation Plan

> **Selected design:** Prototype A, “延续当前 Dashboard”.
>
> **Safety invariant:** the browser may render and submit only authoritative
> backend data. Missing, stale, degraded, or unknown state is unavailable and
> cannot open an order confirmation.

## Scope

- Keep the existing Polymarket V1 backend and endpoints.
- Remove fabricated/default trading facts from the prediction-market page.
- Reduce the readiness area to four status cells and four monitoring metrics.
- Remove the permanent first-order verification card and duplicate WebSocket
  metric.
- Keep direct, single-opportunity execution through the existing preview,
  confirm, and breaker-reset endpoints.
- Preserve the approved layout at desktop and mobile widths.
- Do not add a new venue abstraction, configuration layer, or endpoint.

## Task 1: Capture the approved prototype

**Files**

- Historical only:
  `src/open_trader/dashboard_static/prediction-market-truthful-ui-prototype.html`

**Steps**

1. Commit the A/B/C comparison so the decision has an auditable reference.
2. Treat A as the only production target.
3. Delete the prototype from the final source tree after the production UI
   matches A; it remains available in Git history.

## Task 2: Make the backend response authoritative

**Files**

- Modify: `src/open_trader/dashboard_web.py`
- Modify if required: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_dashboard_web.py`
- Test if required: `tests/test_prediction_arbitrage_execution.py`

**Steps**

1. Add failing tests that require the state payload to expose:
   - explicit health status;
   - the first human-readable backend failure reason;
   - a real heartbeat timestamp or no timestamp;
   - server-owned normal, emergency, wallet, and minimum-profit limits.
2. Add failing tests that require an order preview to contain every fact shown
   in the confirmation modal: market, legs, quantity, total cost, expected net
   profit, available balance, and policy limits.
3. Make the smallest response-shaping change needed to pass those tests.
4. Keep unknown/degraded/stale health distinct in the payload; the UI will map
   every non-healthy value to “不可用”.

## Task 3: Implement layout A with truthful rendering

**Files**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

**Steps**

1. Add failing JavaScript behavior tests for the approved state matrix.
2. Replace the readiness strip with:
   - 交易钱包;
   - 可用余额;
   - 地区与连接;
   - 实盘状态.
3. Replace the metrics with:
   - 当前可参与;
   - 监控事件;
   - 市场 / Token;
   - 过去 24 小时信号.
4. Render `-` or “数据未返回” for absent informational facts. Never replace
   missing values with zero, a time, a dollar amount, a venue attribute, or a
   healthy status.
5. Render “正常” only for an explicitly healthy watcher. Render all other or
   unknown states as “不可用”, show the first backend reason, and disable
   participation.
6. Remove the permanent first-order card and duplicate WebSocket metric.
7. Keep incomplete opportunities visible, label them “数据不完整”, use `-` for
   missing fields, and disable their participation button.
8. Open the order modal only from a complete, latest backend preview. Remove
   fallback to the opportunity currently rendered in the list.
9. If preview data is incomplete, do not open the modal and show
   “预览数据不完整，未下单”.
10. Render completed-trade, incident, and reset UI only from their real fields:
    - incomplete success details become “交易已完成，详情数据未返回”;
    - incomplete incident facts become `-` or “事故详情未返回”;
    - reset shows known incident facts and explains that the existing reset
      endpoint performs live checks after the click.
11. Remove post-action query-string scenario mutation from the real confirm and
    reset flows.

## Task 4: Lock the UI contract into acceptance

**Files**

- Modify:
  `docs/superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md`
- Modify: `src/open_trader/prediction_arbitrage_acceptance.py`
- Modify: `tests/e2e/prediction-market.spec.ts`
- Update:
  `tests/e2e/prediction-market.spec.ts-snapshots/*.png`
- Delete:
  `src/open_trader/dashboard_static/prediction-market-truthful-ui-prototype.html`

**Acceptance matrix**

1. **Loading:** no invented balances, counts, timestamps, health, policy, or
   opportunity values.
2. **Healthy/no opportunity:** watcher is normal; real zero counts are allowed
   only when supplied by a healthy completed snapshot.
3. **Healthy/actionable:** complete backend fields appear; participate is
   enabled; sorting remains actionable first, then profit, then volume.
4. **Healthy/incomplete opportunity:** the opportunity remains visible; missing
   facts are `-`; status is “数据不完整”; action is disabled.
5. **Unavailable/degraded/stale/unknown:** watcher is unavailable; first backend
   reason is visible; metrics without current results are `-`; action is
   disabled.
6. **Preview complete:** confirmation shows only preview values, including the
   wallet cap; one click can submit only the selected opportunity once.
7. **Preview incomplete:** no modal, no execution request, and the user sees
   “预览数据不完整，未下单”.
8. **Executing:** the selected opportunity is locked against duplicate clicks.
9. **Success complete:** a refresh-scoped banner shows only returned execution
   facts; the durable record appears in history.
10. **Success incomplete:** no invented execution details; show
    “交易已完成，详情数据未返回”.
11. **Incident complete/incomplete:** the breaker remains active; facts are
    real or `-`; no inferred positions, remediation, or notification status.
12. **Reset:** “重新检查并解除” calls the existing live-reset endpoint; any
    failed check keeps the breaker and displays its backend reason.
13. **History:** signal, execution/merge, and incident rows contain only stored
    records; no raw tick feed and no fabricated sample row.
14. **Responsive:** the approved A hierarchy remains usable at 1920, 1440, 768,
    and 375 CSS pixels without hidden status or action controls.

## Task 5: Verify and deploy the accepted SHA

**Focused checks**

1. Run the modified Python and JavaScript behavior tests.
2. Run the prediction-market Playwright spec at desktop and mobile sizes.
3. Run authenticated no-submit wallet/preflight checks. Do not place a real
   order.
4. Start the dashboard from the feature worktree and inspect the actual page,
   API response, PID, working directory, Git SHA, and fresh logs.

**Final gate**

1. Commit all source, spec, tests, and approved snapshots.
2. Run `make acceptance` once as the final review-readiness gate.
3. Continue fixing until the result is `PASS`; `FAIL` and `BLOCKED` are not
   completion.
4. Redeploy the exact accepted Git SHA without changing source or data.
5. Verify the new PID, working directory, Git SHA, fresh log timestamp, and an
   HTTP 200 from the review URL before asking for user review.
