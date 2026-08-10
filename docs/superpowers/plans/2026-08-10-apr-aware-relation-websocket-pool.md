# APR-Aware Relation WebSocket Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce HK proxy traffic by limiting same-venue threshold-relation WebSocket subscriptions to all normal APR-target relations plus the 100 closest below-target relations, without changing the 60-second REST scan or any trading gate.

**Architecture:** Keep `_active_relation_ids` as the existing `net_edge >= -5%` diagnostic/work pool and add one private real-time relation-ID set selected during the same activity scan. Reuse the existing annualized-yield formula, validation statuses, buy-leg token fields, combined token union, stream swap behavior, and degraded relation health; do not add a module, configuration surface, dependency, or Dashboard change.

**Tech Stack:** Python 3.12, `asyncio`, `Decimal`, pytest, existing Polymarket monitor/store/dashboard runtime, launchd.

## Global Constraints

- Start from the already-created worktree `/Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool` on branch `codex/apr-aware-relation-ws-pool-design`, based on local `main`.
- Implement only in `src/open_trader/polymarket_monitor.py` and `tests/test_polymarket_monitor.py`; update `CHANGELOG.md` before any merge.
- Keep the 60-second activity scan, 24-hour catalog scan, five-minute Top 20 refresh, cross-venue monitor, 15% APR threshold, execution sizing, validation, REST confirmation, freshness, balance, allowance, unwind, notification, and account rules unchanged.
- Keep `RELATION_ACTIVITY_MIN_EDGE = Decimal("-0.05")` and `relations_within_5pct` as diagnostics. They no longer decide relation WebSocket membership.
- Use fixed code constants of 100 for the APR-target anomaly ceiling and prewarm size. Do not add environment variables, config, controls, per-event quotas, or absolute-profit gates.
- Keep the most recent successful real-time pool on any failed or anomalous scan.
- Treat WebSocket messages as discovery triggers only; the existing targeted REST refresh remains executable truth.
- Run focused checks during development. Run `make acceptance` once, as the final Dashboard gate.
- Do not capture screenshots; none were requested.

---

### Task 1: Select and expose the APR-aware real-time pool

**Files:**

- Modify: `tests/test_polymarket_monitor.py`
- Modify: `src/open_trader/polymarket_monitor.py`

- [ ] **Step 1: Replace the old unlimited-subscription expectation with failing APR-pool tests**

  Rename `test_activity_pool_has_no_top_n_relation_cap` so it proves the diagnostic pool remains uncapped while the real-time pool is capped. Reuse the existing dataclass `replace` calls, `threshold_event`, `threshold_book`, `FakeRelationValidator`, and `make_monitor` fixtures rather than adding a production abstraction.

  The first red test must construct 301 below-target relations and assert:

  ```python
  activity = monitor.snapshot()["relation_discovery"]["activity"]
  assert len(monitor._active_relation_ids) == 301
  assert len(monitor._realtime_relation_ids) == 100
  assert activity["relations_within_5pct"] == 301
  assert activity["apr_target_relations"] == 0
  assert activity["apr_target_limit"] == 100
  assert activity["apr_prewarm_relations"] == 100
  assert activity["apr_prewarm_limit"] == 100
  assert activity["subscribed_relations"] <= 100
  assert activity["relation_subscribed_tokens"] <= 2 * activity["subscribed_relations"]
  ```

  Add focused cases alongside it that use fixed UTC `monitor._now` values and relation end dates to prove:

  - all calculable non-rejected relations at or above `MIN_THRESHOLD_ANNUALIZED_YIELD` are selected in addition to the 100 prewarm rows;
  - ranking is annualized yield descending, then net edge descending, then relation ID ascending;
  - a shorter remaining duration ranks ahead when the same profit/cost would otherwise tie;
  - missing, expired, or mismatched end dates stay in `_active_relation_ids` but not `_realtime_relation_ids`;
  - `pending`, `llm_unavailable`, and other non-rejection statuses remain eligible;
  - `llm_rejected` and `deterministic_rejected` are excluded;
  - a below-target relation with non-positive profit can be selected into the prewarm pool;
  - the first 100 may all come from the same event.

  Keep the test data small for each single-rule case. Only the cap test needs hundreds of relations.

- [ ] **Step 2: Run the new selection tests and confirm RED**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k 'apr or realtime_relation or activity_pool'
  ```

  Expected failure: `_realtime_relation_ids` and the new activity metrics do not exist, and the old code still publishes all active relations.

- [ ] **Step 3: Add the minimum state and constants**

  In `src/open_trader/polymarket_monitor.py`, next to the existing activity constants, add only:

  ```python
  RELATION_APR_TARGET_LIMIT = 100
  RELATION_APR_PREWARM_LIMIT = 100
  ```

  In `PolymarketMonitor.__init__`, add:

  ```python
  self._realtime_relation_ids: set[str] = set()
  ```

  Initialize the six auditable activity fields without removing the old fields:

  ```python
  "apr_target_relations": 0,
  "apr_target_limit": RELATION_APR_TARGET_LIMIT,
  "apr_prewarm_relations": 0,
  "apr_prewarm_limit": RELATION_APR_PREWARM_LIMIT,
  "subscribed_relations": 0,
  "relation_subscribed_tokens": 0,
  ```

- [ ] **Step 4: Rank valid activity intents with the existing APR formula**

  In `_refresh_relation_activity`, keep the existing `assess_threshold_relation_activity` loop and `relation_ids` behavior. During that same loop, collect candidates only when all of the following are true:

  - `assessment.intent` exists;
  - the Codex status is not `llm_rejected` or `deterministic_rejected`;
  - both market end dates parse with `_timestamp_or_none`, match, and are later than the scan's `started` timestamp;
  - `simple_annualized_yield` returns a value for the activity intent and common end date.

  Store only the values needed for one deterministic sort:

  ```python
  ranked.append(
      (
          relation.relation_id,
          annualized,
          assessment.intent.net_edge,
      )
  )
  ```

  Sort once:

  ```python
  ranked.sort(key=lambda row: (-row[1], -row[2], row[0]))
  targets = [row[0] for row in ranked if row[1] >= MIN_THRESHOLD_ANNUALIZED_YIELD]
  prewarm = [
      row[0]
      for row in ranked
      if row[1] < MIN_THRESHOLD_ANNUALIZED_YIELD
  ][:RELATION_APR_PREWARM_LIMIT]
  next_realtime_relation_ids = set(targets) | set(prewarm)
  ```

  Do not factor this into a new class or module. The ranking belongs to the scan that already owns the candidate intents and books.

- [ ] **Step 5: Publish the real-time pool atomically and expose counts**

  On a normal scan (`len(targets) <= RELATION_APR_TARGET_LIMIT`), publish `_active_relation_ids` and `_realtime_relation_ids` only after the complete scan and ranking have succeeded. Rebuild subscriptions after both assignments.

  Populate the new activity fields from the completed selection:

  ```python
  "apr_target_relations": len(targets),
  "apr_target_limit": RELATION_APR_TARGET_LIMIT,
  "apr_prewarm_relations": len(prewarm),
  "apr_prewarm_limit": RELATION_APR_PREWARM_LIMIT,
  "subscribed_relations": len(self._relation_ids_subscribed()),
  "relation_subscribed_tokens": len(self._relation_by_token),
  ```

  Keep the existing combined `subscribed_tokens` value for compatibility. When relation catalog state is replaced, intersect both `_active_relation_ids` and `_realtime_relation_ids` with the new relation map before rebuilding subscriptions.

- [ ] **Step 6: Run focused and full monitor tests until GREEN**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k 'apr or realtime_relation or activity_pool'
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q
  ```

  Record the exact pass counts in the implementation handoff.

- [ ] **Step 7: Commit the selection behavior**

  ```bash
  git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
  git commit -m "feat: select relation websocket pool by APR"
  ```

---

### Task 2: Fail closed when the APR-target population is anomalous

**Files:**

- Modify: `tests/test_polymarket_monitor.py`
- Modify: `src/open_trader/polymarket_monitor.py`

- [ ] **Step 1: Add a failing preserve-and-recover test**

  Build a normal first scan and save all three pieces of successful state:

  ```python
  previous_realtime_ids = set(monitor._realtime_relation_ids)
  previous_token_map = {
      token: set(relation_ids)
      for token, relation_ids in monitor._relation_by_token.items()
  }
  previous_handle = monitor._stream_handle
  ```

  Then make 101 eligible relations produce APR at or above 15%, scan again, and assert:

  ```python
  activity = monitor.snapshot()["relation_discovery"]["activity"]
  assert activity["status"] == "degraded"
  assert activity["apr_target_relations"] == 101
  assert monitor._relations_failed is True
  assert monitor._realtime_relation_ids == previous_realtime_ids
  assert monitor._relation_by_token == previous_token_map
  assert monitor._stream_handle is previous_handle
  ```

  Also assert both `monitor.snapshot()` and `monitor.opportunity(relation_id)` expose no actionable threshold relation while relation health is degraded. Reduce the target population to 100, scan once more, and assert the new pool is published and relation health returns to healthy.

- [ ] **Step 2: Run the anomaly test and confirm RED**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k 'apr_target_limit or anomalous'
  ```

  Expected failure: the current scan accepts all 101 relations and does not preserve the last real-time pool.

- [ ] **Step 3: Add the inline anomaly branch before pool publication**

  After ranking and before assigning `_realtime_relation_ids`, branch on `len(targets) > RELATION_APR_TARGET_LIMIT`.

  In that branch:

  - publish the current diagnostic counts, duration, and `apr_target_relations` count with activity status `degraded`;
  - keep `_realtime_relation_ids`, `_relation_by_token`, and the stream handle unchanged;
  - set `_relations_failed = True`;
  - set the next 60-second scan time;
  - record/log a failed activity scan with reason `apr_target_limit`;
  - return before subscription or opportunity refresh.

  Do not raise an exception merely to enter the existing generic failure handler: that handler restores the previous activity payload and would hide the observed target count.

- [ ] **Step 4: Make degraded relation health block threshold actions at the read boundary**

  Both `snapshot()` and `opportunity()` already calculate `relation_health`. In their threshold-hedge guard, use `relation_health["status"]` instead of checking only the catalog snapshot. This reuses the existing health computation and makes `_relations_failed` authoritative without adding a second execution policy.

  Preserve the existing reason shape:

  ```python
  result["eligibility_reason"] = (
      "relation_discovery_" + str(relation_health["status"])
  )
  ```

  Apply the equivalent assignment to the copied rows in `snapshot()`.

- [ ] **Step 5: Run focused and full monitor tests until GREEN**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k 'apr_target_limit or anomalous or relation_health'
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q
  ```

- [ ] **Step 6: Commit the anomaly guard**

  ```bash
  git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
  git commit -m "fix: preserve relation pool on APR anomaly"
  ```

---

### Task 3: Subscribe only buy legs and reconnect only for a changed token union

**Files:**

- Modify: `tests/test_polymarket_monitor.py`
- Modify: `src/open_trader/polymarket_monitor.py`

- [ ] **Step 1: Add failing token-map and reconnect tests**

  Update the large-universe test to expect the normal maximum relation layer: 100 target plus 100 prewarm relations, two buy tokens per relation, 400 distinct relation tokens before cross-layer deduplication, and every `MarketSpec` chunk at or below 250 tokens.

  Add focused tests that assert:

  ```python
  assert set(monitor._relation_by_token) == {
      relation.buy_leg_a.token_id,
      relation.buy_leg_b.token_id,
  }
  ```

  The complementary market YES/NO tokens must not appear unless another monitoring layer independently needs them.

  For reconnect behavior, clear `FakePublicClient.subscribe_specs`, run two successful activity scans with the same combined Top 20 + relation + cross-venue token union, and assert the second scan neither marks `_subscription_dirty` nor calls `subscribe` again. Then rotate relation IDs while retaining the same buy-token union and assert:

  - `_relation_by_token` contains the new relation IDs;
  - `_subscription_dirty` remains false;
  - the stream handle and `connected_at` remain unchanged.

  Finally change one buy token and assert `_subscription_dirty` becomes true; after `_refresh_subscription_if_dirty(client)`, assert a new stream is installed before the old `FakeStream` is closed.

- [ ] **Step 2: Run the subscription tests and confirm RED**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k 'subscription or websocket_safe_chunks or buy_leg or token_union'
  ```

  Expected failures: four tokens are currently emitted per relation and `_run_activity_scan` marks every successful scan dirty.

- [ ] **Step 3: Rebuild the relation token map from the real-time pool and buy legs**

  Change `_rebuild_relation_subscriptions` to iterate `_realtime_relation_ids`. Keep the explicit rejected-status guard as defense in depth, then publish only:

  ```python
  for token in (
      relation.buy_leg_a.token_id,
      relation.buy_leg_b.token_id,
  ):
      token_map.setdefault(token, set()).add(relation_id)
  ```

  Change `_relation_ids_subscribed` to iterate `_realtime_relation_ids`. Keep `_relation_subscription_tokens` only if existing rejection tests still use it; if retained, make it return the same two buy-leg tokens so the name stays truthful.

- [ ] **Step 4: Compare the final combined token union in one place**

  Inside `_rebuild_relation_subscriptions`, capture the combined union before replacing `_relation_by_token`, replace the local map, calculate the combined union again, and mark the subscription dirty only when the union differs:

  ```python
  previous_tokens = (
      set(self._market_by_token)
      | set(self._relation_by_token)
      | self._cross_venue_tokens
  )
  self._relation_by_token = token_map
  current_tokens = (
      set(self._market_by_token)
      | set(self._relation_by_token)
      | self._cross_venue_tokens
  )
  if current_tokens != previous_tokens:
      self._subscription_dirty = True
  ```

  This deliberately compares the shared union, not relation IDs or relation-only tokens. The existing `_subscribe` implementation already opens and installs the replacement before closing the previous stream; keep that code.

- [ ] **Step 5: Remove unconditional activity-scan reconnects**

  Delete the unconditional `self._subscription_dirty = True` at the end of `_run_activity_scan`.

  In `_refresh_relation_activity`, call `_refresh_subscription_if_dirty(client)` rather than `_subscribe(client)` when `resubscribe=True`. The long-running loop already calls `_refresh_subscription_if_dirty`, so an unchanged union becomes a no-op and a changed union still reconnects.

  Do not change Top 20 or cross-venue code paths; their changes remain part of the same combined-union comparison inside `_subscribe`.

- [ ] **Step 6: Prove WebSocket ticks still use targeted REST confirmation**

  Retain and run the existing targeted-refresh tests. Add an assertion only if the existing test does not already prove it:

  - a token tick maps to the affected relation ID through the freshly rebuilt local map;
  - only that relation's two public books are requested;
  - the returned opportunity still requires the existing positive-profit, freshness, validation, readiness, funds, allowance, unwind, and 15% APR gates.

  Do not duplicate every admission test; the existing suite is the executable contract for those gates.

- [ ] **Step 7: Run the full affected test set until GREEN**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py -q
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard_web.py -q
  ```

  Confirm that all subscribe chunks are at most 250 tokens and no unchanged-union test creates another stream.

- [ ] **Step 8: Commit the subscription minimization**

  ```bash
  git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
  git commit -m "perf: narrow relation websocket subscriptions"
  ```

---

### Task 4: Changelog, direct runtime proof, final acceptance, and exact-SHA redeploy

**Files:**

- Modify: `CHANGELOG.md`
- Verify: `src/open_trader/polymarket_monitor.py`
- Verify: `tests/test_polymarket_monitor.py`

- [ ] **Step 1: Update the operator-facing merge log before any merge**

  Add a `## 2026-08-10` entry at the top of `CHANGELOG.md` stating, without promising a numeric traffic reduction, that same-venue relation WebSocket monitoring now:

  - keeps the 60-second full REST scan;
  - subscribes all normal APR-target relations plus 100 closest below target;
  - uses only two hedge buy-leg tokens;
  - avoids reconnecting when the combined token union is unchanged;
  - preserves/fails closed on scan failure or more than 100 APR-target relations.

  Commit this entry before any merge:

  ```bash
  git add CHANGELOG.md
  git commit -m "docs: record relation websocket optimization"
  ```

- [ ] **Step 2: Run the smallest complete automated regression gate**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_polymarket_monitor.py tests/test_dashboard_web.py -q
  git status --short
  ```

  Require an exact pytest pass result and a clean worktree.

- [ ] **Step 3: Deploy the candidate worktree and run the real monitor workflow**

  The Dashboard is the existing owner of the long-running `PolymarketMonitor`. Deploy the candidate worktree against the stable runtime data/config root so the direct check uses real credentials, persisted relation catalog, public books, and WebSocket stream:

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  scripts/install_dashboard_launchd.sh --dry-run --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader --python /Users/ray/projects/open_trader/.venv/bin/python
  scripts/install_dashboard_launchd.sh --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader --python /Users/ray/projects/open_trader/.venv/bin/python
  curl -fsS --max-time 30 http://127.0.0.1:8766/api/prediction-arbitrage/state
  ```

  Inspect the returned relation snapshot and require:

  ```text
  relation_discovery.activity.status = healthy
  relation_discovery.activity.apr_target_limit = 100
  relation_discovery.activity.apr_prewarm_limit = 100
  relation_discovery.activity.subscribed_relations <= apr_target_relations + apr_prewarm_relations
  relation_discovery.activity.relation_subscribed_tokens <= 2 * subscribed_relations
  relation_discovery.websocket.status = connected
  ```

  `subscribed_relations` may be lower than the selected APR target plus prewarm
  counts because validation can reject a relation after selection; the selected
  pool is the upper bound, while the subscription map contains only relations
  still eligible at rebuild time.

  Also inspect fresh monitor logs for a completed activity scan. Do not infer correctness only from process liveness or unit tests.

- [ ] **Step 4: Run the final Dashboard acceptance gate once**

  ```bash
  cd /Users/ray/projects/open_trader/.worktrees/apr-aware-relation-ws-pool
  make acceptance
  ```

  Stop on `FAIL` and fix before rerunning. Report `BLOCKED` as blocked. Only `PASS` permits completion or review handoff.

- [ ] **Step 5: Record the exact accepted SHA and redeploy that SHA**

  After `PASS`, require a clean worktree and record:

  ```bash
  ACCEPTED_RELATION_POOL_SHA=$(git rev-parse HEAD)
  git status --short
  scripts/install_dashboard_launchd.sh --dry-run --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader --python /Users/ray/projects/open_trader/.venv/bin/python
  scripts/install_dashboard_launchd.sh --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader --python /Users/ray/projects/open_trader/.venv/bin/python
  ```

  This deployment must use the accepted worktree SHA and make no source or data changes afterward.

- [ ] **Step 6: Verify fresh long-running processes and HTTP state**

  Inspect both relevant launchd services rather than assuming the restart worked:

  ```bash
  launchctl print "gui/$(id -u)/com.open-trader.frontend-gateway"
  launchctl print "gui/$(id -u)/com.open-trader.legacy-dashboard"
  curl -fsS --max-time 30 http://127.0.0.1:8766/healthz
  curl -fsS --max-time 30 http://127.0.0.1:8766/api/prediction-arbitrage/state
  ```

  Resolve the process IDs from the two `launchctl print` results, then inspect both processes and the legacy Dashboard working directory:

  ```bash
  GATEWAY_RELATION_POOL_PID=$(launchctl print "gui/$(id -u)/com.open-trader.frontend-gateway" | awk '$1 == "pid" {print $3; exit}')
  LEGACY_RELATION_POOL_PID=$(launchctl print "gui/$(id -u)/com.open-trader.legacy-dashboard" | awk '$1 == "pid" {print $3; exit}')
  ps -p "$GATEWAY_RELATION_POOL_PID" -o pid=,lstart=,command=
  ps -p "$LEGACY_RELATION_POOL_PID" -o pid=,lstart=,command=
  lsof -a -p "$LEGACY_RELATION_POOL_PID" -d cwd -Fn
  tail -n 100 logs/frontend_gateway/launchd.out.log
  tail -n 200 logs/legacy_dashboard/launchd.out.log
  curl -sS -o /dev/null -w '%{http_code}\n' --max-time 30 http://127.0.0.1:8766/
  ```

  Verify for the newly started process:

  - PID and start timestamp are fresh;
  - working directory is the accepted worktree/runtime location;
  - reported Git SHA equals `$ACCEPTED_RELATION_POOL_SHA`;
  - fresh logs contain a post-restart healthy activity scan;
  - the review URL returns HTTP 200.

  Inspect two completed 60-second activity scans using separate short polls, each below 60 seconds. When the combined token union is unchanged, require WebSocket `connected_at` to remain unchanged while activity `completed_at` advances. This is the live proof that the minute scanner no longer reconnects unconditionally.

- [ ] **Step 7: Observe the HK route direction without inventing a numeric pass target**

  Compare the same HK VPS/Xray traffic surface used in the original diagnosis after the new process has completed at least two scans. Report:

  - current relation count, APR-target count, prewarm count, relation token count, and combined token count;
  - whether the connection stayed stable across unchanged scans;
  - whether HK proxy traffic moved in the expected direction;
  - any remaining traffic attributable to Top 20, cross-venue, REST scans, or unrelated clients.

  Do not claim a fixed percentage reduction; the design intentionally has no numeric traffic acceptance threshold.

- [ ] **Step 8: Hand off for review without merging unless the user authorizes it**

  Provide the direct review URL `http://127.0.0.1:8766`, accepted SHA, exact automated-test counts, `make acceptance` result, fresh PID/cwd/SHA/log/HTTP evidence, and the observed HK route direction. Do not merge or push merely because verification passed.

---

## Plan Self-Review Checklist

- [ ] Every grill-approved decision is represented by a test or verification step.
- [ ] The plan changes no REST cadence, relation discovery, cross-venue scope, Dashboard UI, config, or trading economics.
- [ ] The implementation reuses `simple_annualized_yield`, `_timestamp_or_none`, existing validation statuses, and existing stream replacement behavior.
- [ ] `_active_relation_ids` remains the uncapped `-5%` diagnostic/work pool; `_realtime_relation_ids` alone controls relation WebSocket membership.
- [ ] The anomalous target count remains observable while the last successful real-time pool is preserved.
- [ ] Both read paths fail closed for threshold opportunities when relation health is degraded.
- [ ] Only buy-leg tokens enter the relation layer, and the final combined union controls reconnects.
- [ ] `CHANGELOG.md` is committed before any merge.
- [ ] Final acceptance precedes exact-SHA redeploy; live process proof follows redeploy.
- [ ] No placeholder, speculative abstraction, dependency, or unrequested screenshot was added.
