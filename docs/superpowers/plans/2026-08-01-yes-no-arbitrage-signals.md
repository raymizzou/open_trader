# YES/NO Arbitrage Signal Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the five-second YES/NO `当前机会` panel with a durable, one-second `套利信号` component, observation-only Feishu alerts, background bilingual title translation, and the existing manual preview/confirm order flow.

**Architecture:** Keep the watcher as the sole owner of Polymarket books and profit calculation. Persist the identity and immutable trigger economics in the existing `signals` table, project current actionability by joining durable rows to the monitor snapshot inside the local history endpoint, and let the browser poll only that projection once per second. Reuse the existing notification lease, `llm_cache`, preview endpoint, confirmation modal, execution lock, and circuit breaker; add no database table and no automatic-order state.

**Tech Stack:** Python 3.12, SQLite, asyncio, existing Polymarket watcher and execution service, vanilla JavaScript/CSS, pytest, Node VM renderer tests, Playwright, launchd, existing `make acceptance` gate.

**Approved design:** `docs/superpowers/specs/2026-08-01-yes-no-arbitrage-signals-design.md`

## Non-negotiable boundaries

- Preserve the existing warm Dashboard palette, typography, spacing, borders, density, and component classes. This is an in-place update, not a redesign.
- Standard YES/NO only. Do not change LLM-hedge eligibility, preview, notification copy, or execution behavior.
- Feishu is notification-only: no link, button, wallet, internal ID, rule trace, preview, preflight, or order submission.
- `重新检查` reuses the current fresh preview endpoint. `确认下单` remains the only order-submitting action.
- The browser never fetches Polymarket books and never calculates profit.
- Do not add tables, dependencies, a notification framework, an auto-order toggle, or an auto-order queue/state machine.
- Do not include `预计`, `窗口结束`, `峰值利润`, `最终利润`, `未下单，当前可能已失效`, or neutral `仅监控` copy in the revised YES/NO surface.
- Keep `package-lock.json` out of all commits; its current modification predates implementation.
- Run focused checks during implementation. Run `make acceptance` only once, after source, tests, changelog, and candidate deployment are final.

---

### Task 0: Remove disposable mock scaffolding

**Files:**

- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_web.py`
- Delete: `src/open_trader/dashboard_static/prediction_signal_prototype.css`
- Delete: `src/open_trader/dashboard_static/prediction_signal_prototype.js`

- [ ] **Step 1: Remove only prototype hooks and assets**

Use `apply_patch` to remove the prototype stylesheet/script tags from `index.html`, the `prediction_signal_prototype` query-parameter branch from `dashboard.js`, and the two prototype static-file routes from `dashboard_web.py`. Delete the two untracked prototype asset files with `apply_patch`.

Do not revert `package-lock.json` and do not disturb unrelated user changes.

- [ ] **Step 2: Prove the working diff contains no mock runtime**

Run:

```bash
rg -n 'prediction_signal_prototype|prototype=A' \
  src/open_trader/dashboard_static src/open_trader/dashboard_web.py
git status --short
```

Expected: `rg` has no matches. `git status` still shows the pre-existing `package-lock.json` change, but no prototype assets or prototype-only source diff. There is no commit because the prototype was never production work.

---

### Task 1: Persist signal identity and successful-notification cooldown truth

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Modify: `src/open_trader/polymarket_monitor.py`
- Test: `tests/test_prediction_arbitrage_store.py`
- Test: `tests/test_polymarket_monitor.py`

- [ ] **Step 1: Write failing store tests for immutable trigger fields and cooldown lookup**

Add tests proving:

1. the first upsert stores `opportunity_id`, `yes_max_price`, `no_max_price`, `total_max_cost`, and `initial_profit`;
2. a later upsert for the same open episode updates live economic fields but preserves `started_at`, `first_positive_at`, and `initial_profit`;
3. `notification_sent_since(market_id, since)` returns true only for a successful `notification_sent_at` within the requested rolling window;
4. failed attempts and successful notifications for another `market_id` do not start the cooldown.

The new store method is deliberately narrow:

```python
def notification_sent_since(
    self,
    market_id: str,
    since: datetime,
) -> bool:
    """Return whether this market has a successful delivery at or after since."""
```

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  -k 'signal and (initial_profit or notification_sent_since)' -q
```

Expected: FAIL because the cooldown query and persisted identity/economic fields are not implemented.

- [ ] **Step 2: Implement the smallest store query**

Query only `signals` rows for the exact indexed `market_id`, newest first. Parse `notification_sent_at` from each payload and return true when it is at or after the supplied timezone-aware `since`. Do not add a table, column, migration, general repository abstraction, or background cleanup.

The number of rows per market is naturally bounded by signal episodes and the check occurs only when scheduling a notification. Add a `ponytail:` comment documenting that a dedicated indexed column is warranted only if measured history makes this scan material.

- [ ] **Step 3: Persist the existing opportunity identity and notification facts**

Extend `PolymarketMonitor._upsert_signal()` with fields already owned by the confirmed watcher opportunity:

```python
{
    "opportunity_id": opportunity.get("opportunity_id"),
    "yes_max_price": opportunity.get("yes_max_price"),
    "no_max_price": opportunity.get("no_max_price"),
    "yes_max_cost": opportunity.get("yes_max_cost"),
    "no_max_cost": opportunity.get("no_max_cost"),
    "total_max_cost": opportunity.get("total_max_cost"),
}
```

Do not rename or delete the durable `peak_profit` and `final_profit` audit fields. `initial_profit` remains the actual first qualifying calculated profit, not the configured threshold constant.

- [ ] **Step 4: Run focused tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py tests/test_polymarket_monitor.py \
  -k 'signal or notification' -q
```

Expected: PASS.

- [ ] **Step 5: Commit the durable signal contract**

```bash
git add src/open_trader/prediction_arbitrage_store.py \
  src/open_trader/polymarket_monitor.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_polymarket_monitor.py
git commit -m "feat: persist yes-no signal action identity"
```

---

### Task 2: Add the lightweight YES/NO Feishu notification path

**Files:**

- Modify: `src/open_trader/notifications.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `src/open_trader/polymarket_monitor.py`
- Test: `tests/test_notifications.py`
- Test: `tests/test_prediction_arbitrage_execution.py`
- Test: `tests/test_polymarket_monitor.py`

- [ ] **Step 1: Write failing renderer tests for exact, link-free copy**

Add `render_yes_no_signal_notification(signal)` tests with and without `event_title_zh`. Assert the exact title form:

```text
【YES/NO 套利信号】+$0.38
```

Assert that the body contains only the optional Chinese title above English, YES price, NO price, quantity, maximum cost, current profit, and discovered HKT time. Explicitly assert absence of `http`, Dashboard, Polymarket link, wallet, signal/opportunity IDs, rule traces, action copy, and `未下单，当前可能已失效`.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_notifications.py -k yes_no_signal -q
```

Expected: FAIL because the renderer does not exist.

- [ ] **Step 2: Implement one dedicated renderer**

Add:

`render_yes_no_signal_notification(signal: Mapping[str, object]) -> tuple[str, str]`

Use `ZoneInfo("Asia/Hong_Kong")` and include the literal `HKT`. Render money and prices from the persisted watcher-owned values. Do not modify `render_prediction_opportunity_notification`; that preserves LLM-hedge behavior.

- [ ] **Step 3: Write failing service tests for observation-only delivery**

Add standard-binary tests proving:

- the first open signal reserves the existing lease, sends only to Feishu, and completes as `sent`;
- the standard branch never calls `_prepare_opportunity`, volatile checks, no-submit preflight, preview, or execution;
- a second signal episode for the same `market_id` inside 30 minutes is persisted and marked `notification_state="suppressed"` with `notification_suppressed_reason="market_cooldown"`, without delivery;
- the cooldown is absent when the first delivery failed;
- a failed delivery can be retried up to three reserved attempts while open;
- a closed signal never retries;
- existing threshold-hedge notification tests remain unchanged and passing.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  -k 'notify_ready_opportunity and (standard or cooldown or retry)' -q
```

Expected: FAIL because standard signals are unsupported.

- [ ] **Step 4: Branch before the existing threshold preflight**

Keep `notify_ready_opportunity(opportunity_id, signal_id)` as the observer entry point. Immediately after loading the durable signal, branch on `market_type`:

```python
if signal.get("market_type") == "standard_binary":
    return self._notify_yes_no_signal(signal_id, signal)
```

`_notify_yes_no_signal` must:

1. reject missing/closed/sent/exhausted signals;
2. check `notification_sent_since(market_id, now - timedelta(minutes=30))` before reserving;
3. mark an in-cooldown episode suppressed so the monitor does not repeatedly schedule it;
4. reserve with the existing three-attempt lease;
5. render from the reserved signal snapshot;
6. reuse the existing Feishu-only delivery and fallback logic;
7. complete the attempt through `complete_notification_attempt`.

It must not call `_prepare_opportunity`, preflight, preview, or any trading method. Keep the current threshold branch byte-for-byte equivalent apart from extracting a tiny Feishu-delivery helper if that removes direct duplication.

- [ ] **Step 5: Schedule eligible standard signals without LLM gates**

Change `_schedule_ready_notification()` so an actionable `standard_binary` signal reaches the existing observer immediately after persistence. Keep the current rules/Codex gates for `threshold_hedge` only. Skip `sent`, `suppressed`, closed, leased, and exhausted signals.

Do not create an automatic-order queue. The existing single notification task remains observation-only and is reaped by the watcher loop.

- [ ] **Step 6: Run notification and monitor regression tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  -k 'notification or notify_ready or schedule_ready' -q
```

Expected: PASS, including the unchanged threshold-hedge cases.

- [ ] **Step 7: Commit observation-only alerts**

```bash
git add src/open_trader/notifications.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/polymarket_monitor.py \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py
git commit -m "feat: notify yes-no signals in Feishu"
```

---

### Task 3: Translate titles asynchronously with Codex Luna

**Files:**

- Create: `src/open_trader/prediction_title_translation.py`
- Create: `src/open_trader/schemas/polymarket_title_translation.json`
- Modify: `src/open_trader/polymarket_monitor.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `src/open_trader/dashboard_web.py`
- Test: `tests/test_prediction_title_translation.py`
- Test: `tests/test_polymarket_monitor.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

- [ ] **Step 1: Write failing translator tests**

Cover:

- deterministic namespaced cache keys from normalized English title, model, and prompt version;
- cache hits make no subprocess call;
- a cache miss invokes exactly one Codex subprocess and stores only a validated Chinese translation;
- timeout, non-zero exit, malformed JSONL, invalid schema result, empty translation, and unchanged-English output return `None` without blocking callers or poisoning the cache;
- the command uses `gpt-5.6-luna`, `model_reasoning_effort="high"`, and `service_tier="priority"`;
- market content is treated as untrusted text and the prompt asks for title translation only.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_title_translation.py -q
```

Expected: FAIL because the module and schema do not exist.

- [ ] **Step 2: Implement a concrete translator, not a framework**

Add only these concrete public names:

- `TITLE_TRANSLATION_MODEL = "gpt-5.6-luna"`
- `TITLE_TRANSLATION_PROMPT_VERSION = "polymarket-title-zh-v1"`
- `prediction_title_cache_key(title: str) -> str`
- `cached_prediction_title_zh(store: PredictionArbitrageStore, title: str) -> str | None`
- `CodexTitleTranslator.translate(title: str) -> str | None`

Reuse the repository's existing Codex JSONL parsing pattern and `llm_cache`. The command must include:

```python
[
    "codex", "exec",
    "--model", "gpt-5.6-luna",
    "-c", 'model_reasoning_effort="high"',
    "-c", 'service_tier="priority"',
    "--ephemeral", "--sandbox", "read-only",
    "--skip-git-repo-check", "--ignore-user-config",
    "--ignore-rules", "--disable", "hooks",
    "--output-schema", str(schema_path), "--json", "-",
]
```

Do not use DeepSeek, introduce a provider interface, or add a translation table.

- [ ] **Step 3: Write failing monitor tests for one non-blocking worker**

Inject an optional `title_translator` into `PolymarketMonitor`. Add tests proving:

- universe refresh publishes English events before a deliberately blocked translator completes;
- all misses enter one FIFO worker, never parallel Codex subprocesses;
- cache hits attach `title_zh` to events and standard opportunities;
- translator failure leaves English titles and all actionability unchanged;
- monitor shutdown cancels the one translation worker cleanly.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -k title_translation -q
```

Expected: FAIL because the monitor has no translation worker.

- [ ] **Step 4: Wire the one worker into the watcher lifecycle**

Instantiate `CodexTitleTranslator(prediction_store)` in `serve_dashboard()` and pass it to the monitor. The monitor owns one `asyncio.Queue[str]` and one `_title_translation_task`. Universe normalization enqueues uncached titles after English rows are installed. The worker calls `translate` through `asyncio.to_thread`, stores successful results in a small monitor title map, and never changes signal, readiness, or execution state.

Include `_title_translation_task` in the existing `run_forever()` cancellation block. Do not spawn a worker per title.

- [ ] **Step 5: Add cached Chinese to Feishu without waiting for it**

Immediately before rendering a standard notification, call `cached_prediction_title_zh(store, event_title)` once and add `event_title_zh` only if already present. Never call `translate()` from notification delivery.

- [ ] **Step 6: Run focused translation regressions**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_title_translation.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'title_translation or yes_no_signal or notify_ready' -q
```

Expected: PASS. The tests must demonstrate that signal persistence and notification do not await translation.

- [ ] **Step 7: Commit the translation boundary**

```bash
git add src/open_trader/prediction_title_translation.py \
  src/open_trader/schemas/polymarket_title_translation.json \
  src/open_trader/polymarket_monitor.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/dashboard_web.py \
  tests/test_prediction_title_translation.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py
git commit -m "feat: translate prediction titles in background"
```

---

### Task 4: Project current signal actionability from watcher truth

**Files:**

- Modify: `src/open_trader/dashboard_web.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing history-projection tests**

Extend `_prediction_history_payload` tests with a fake store, monitor, and execution boundary. Prove that every standard signal row exposes:

```text
signal_id
opportunity_id
market_id
event_title
event_title_zh (only when cached)
started_at
ended_at
observed_duration_ms
initial_profit
live_profit
notification_state
actionable_now
```

Cover these cases:

- open + matched current opportunity + healthy snapshot + breaker closed + no active execution => `actionable_now=true`, current `estimated_profit` becomes `live_profit`;
- a later watcher snapshot changes `live_profit` but not `initial_profit`;
- closed, stale, degraded, missing-opportunity, incomplete-opportunity, active-execution, and open-breaker rows all fail closed with `actionable_now=false` and `live_profit=null` where current truth is unavailable;
- actionable open rows sort first; remaining rows sort newest first;
- cached Chinese title appears without changing English;
- executions and incidents retain their current projections.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'prediction_history and (actionable or live_profit or title)' -q
```

Expected: FAIL because history currently reads durable rows only.

- [ ] **Step 2: Reuse the existing state projection once per signal request**

Extend `_prediction_history_payload` with the keyword-only parameters
`monitor: object | None = None` and `execution: object | None = None`, retaining
its existing `store`, `kind`, `limit`, `offset`, and `dict[str, object]` return
contract.

For `kind="signals"`, call the existing `_prediction_state_payload(monitor, store, execution, "")` once and build maps by persisted `opportunity_id`, with `market_id` only as a compatibility fallback. This reuses the current monitor health, active-execution, and breaker truth instead of copying it.

Set `actionable_now` only when all conditions hold:

```python
not row.get("ended_at")
and not state.get("stale")
and state.get("status") not in {"degraded", "unavailable", "error"}
and not state.get("current_execution")
and state.get("breaker", {}).get("open") is False
and current_opportunity.get("actionable") is True
and all required preview identity/economic fields are present
```

Set `live_profit` from the matched opportunity's current `estimated_profit`/`profit` only for a current open match. Keep audit-only `peak_profit` and `final_profit` in the raw payload for compatibility but do not derive display semantics from them.

Resolve `event_title_zh` with the cache-only helper. Any exception must return rows with `actionable_now=false`, never an optimistic button.

- [ ] **Step 3: Pass live dependencies from the HTTP handler**

Update only the history route call:

```python
_prediction_history_payload(
    prediction_store,
    kind=kind,
    limit=limit,
    offset=offset,
    monitor=prediction_monitor,
    execution=prediction_execution_service,
)
```

Keep `/api/prediction-arbitrage/state` and the execution endpoints unchanged.

- [ ] **Step 4: Run backend contract tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k prediction -q
```

Expected: PASS.

- [ ] **Step 5: Commit the fail-closed local projection**

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: project live yes-no signal state"
```

---

### Task 5: Replace `当前机会` with the original-style `套利信号` component

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/serve_dashboard_fixture.py`
- Modify: `tests/e2e/prediction-market.spec.ts`

- [ ] **Step 1: Write failing static and renderer tests for the approved copy**

Update the static contract and Node VM renderer assertions so they require:

- `当前机会`, generic `历史记录`, and `信号历史` are absent from YES/NO;
- tabs are `套利信号`, `交易与合并`, `事故`;
- signal columns are exactly `出现时间（HKT）`, `标的`, `持续`, `触发时利润`, `实时利润`, `通知`, `操作`;
- `峰值利润`, `最终利润`, `窗口结束`, `预计`, and `未下单，当前可能已失效` are absent from the YES/NO renderer;
- a row has `重新检查` only when `actionable_now=true` and the signal request is healthy;
- closed and failed-refresh rows have an empty operation cell and `实时利润` displays `—`;
- Chinese title renders above English when present, otherwise English renders immediately;
- neutral `仅监控` is absent while meaningful degraded labels remain.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'prediction_market_static_contract or prediction_history or prediction_renderer' -q
```

Expected: FAIL against the old panel and columns.

- [ ] **Step 2: Add one-second signal state without changing the five-second page timer**

Extend only `state.predictionMarket`:

```javascript
signalPollId: null,
signalRequestInFlight: false,
signalLastSuccessAt: "",
signalError: "",
```

Add `startPredictionSignalPolling()` and `stopPredictionSignalPolling()`. While the prediction workspace and `yes_no` strategy are visible, call the signal history endpoint immediately and every 1000 ms. Use `signalRequestInFlight` to prevent overlapping requests. Stop the timer when leaving the workspace or switching to `llm_hedge`.

Keep `startPredictionPolling()` at 5000 ms for the full state. Do not add a Polymarket request.

- [ ] **Step 3: Replace only the signal component DOM on one-second success**

Give the right-column panel a stable root:

```html
<section class="pm-panel" data-prediction-history-panel></section>
```

Add `renderPredictionSignalPanel()` that replaces this one element only when `historyKind === "signals"`. A successful signal fetch updates cached `histories.signals`, clears `signalError`, records the local success time, and calls that component renderer. It must not call `renderPredictionMarket()`.

If another tab is selected, update the cached signal rows without switching the tab or replacing unrelated DOM. Remove the current status-driven auto-switch in `fetchPredictionState`; keep the user's selected tab until they click another tab.

On signal fetch failure, retain the last rows, set `signalError`, and rerender only the signal panel if visible. The panel shows the last successful HKT timestamp in danger styling and suppresses all action buttons until recovery.

- [ ] **Step 4: Render the approved original-style layout**

In `predictionYesNoWorkspace()`:

- keep the metric strip, policy note, and `当前监控范围` in the existing left column;
- remove the entire `当前机会` panel;
- place the selected `套利信号`/`交易与合并`/`事故` panel in the existing right column;
- keep the existing preview click delegation by rendering `data-action="participate"` with the persisted `opportunity_id` on `重新检查`;
- render no disabled button when action is unavailable;
- remove only the neutral `.pm-event-state` fallback; preserve explicit degraded/exception states;
- change the YES/NO monitoring-row default `预计净利润` copy to `净利润`;
- show Chinese above English with a small secondary line using existing font/color tokens;
- add `Watcher 数据时间` to the page header from `heartbeat_at` and `信号刷新时间` to the signal panel from the last successful local fetch;
- format both with `Intl.DateTimeFormat` using `timeZone: "Asia/Hong_Kong"`, append `HKT`, and include relative age.

Touch CSS only for the stable signal panel state, bilingual title stacking, clock danger state, and responsive table/action cell. Reuse existing colors, radii, borders, buttons, typography, spacing, and breakpoints.

- [ ] **Step 5: Preserve the existing two-step manual order interaction**

Clicking `重新检查` posts the row's `opportunity_id` to the existing preview endpoint. A complete response opens the existing `确认真实下单` modal; a rejected or incomplete response submits nothing. Only the modal's existing `确认下单` action may post to executions.

Do not add a button to Feishu or bypass preview.

- [ ] **Step 6: Update deterministic browser fixtures and E2E assertions**

Update the fixture signal rows with ISO timestamps, `event_title_zh`, `initial_profit`, changing `live_profit`, `actionable_now`, notification state, and `opportunity_id`.

Add E2E coverage proving:

- the original Variant A hierarchy is now `当前监控范围` left and the selected signal/history panel right;
- only the signal panel element is replaced after a one-second response; the monitoring-scope element identity and an expanded event remain unchanged;
- switching to `交易与合并` survives incoming signals;
- closing the fixture signal removes the button and displays `—`;
- a failed history request freezes/redens the refresh time and removes buttons;
- `重新检查` still produces two preview requests when reopened and exactly one execution submit after confirmation;
- 1440px and 375px layouts have no horizontal overflow and all visible buttons remain at least 44px high;
- LLM hedge candidate behavior is unchanged.

Remove the obsolete prototype SHA/base URL constants and old `当前机会` assertions from `prediction-market.spec.ts`; the production fixture is now the only source.

- [ ] **Step 7: Run focused UI tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k prediction -q

OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: both commands PASS. No screenshot capture is required.

- [ ] **Step 8: Commit the approved UI**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py \
  tests/e2e/serve_dashboard_fixture.py \
  tests/e2e/prediction-market.spec.ts
git commit -m "feat: promote yes-no arbitrage signals"
```

---

### Task 6: Complete direct verification, changelog, acceptance, and exact-SHA deployment

**Files:**

- Modify: `Makefile`
- Modify: `CHANGELOG.md`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write a failing contract test for the sole final gate**

Add `test_acceptance_gate_runs_prediction_playwright` to
`tests/test_dashboard_web.py`. Read the repository `Makefile` and assert that
the `acceptance` recipe runs this exact deterministic browser suite with the
worktree Python:

```text
OPEN_TRADER_PYTHON="$(WORKTREE_ROOT)/.venv/bin/python"
npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_acceptance_gate_runs_prediction_playwright -q
```

Expected: FAIL because `make acceptance` does not yet aggregate Prediction
Playwright.

- [ ] **Step 2: Add Prediction Playwright to `make acceptance`**

After the complete Python suite and before external live checks, add one
fail-as-`FAIL` block:

```make
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	OPEN_TRADER_PYTHON="$(WORKTREE_ROOT)/.venv/bin/python" \
		npm exec playwright test tests/e2e/prediction-market.spec.ts \
		--project=chromium || status=$$?; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
```

This deterministic fixture check cannot return `BLOCKED`; a failure is a code
or local-environment `FAIL`. Keep `SKIP_POLYMARKET_LIVE=1` for unrelated
operator workflows, but do not use it to accept this feature.

- [ ] **Step 3: Run the gate contract test**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_acceptance_gate_runs_prediction_playwright -q
```

Expected: PASS.

- [ ] **Step 4: Commit the final-gate aggregation**

```bash
git add Makefile tests/test_dashboard_web.py
git commit -m "test: include prediction UI in acceptance gate"
```

- [ ] **Step 5: Run the complete focused regression set**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  tests/test_notifications.py \
  tests/test_prediction_title_translation.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q

OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: PASS with exact counts recorded in the handoff notes.

- [ ] **Step 6: Run safe real Polymarket readiness commands**

The ignored prediction config must exist in this exact worktree before treating a generic block as a product failure. Use the current shared operator config without printing secrets:

```bash
test -f config/prediction_arbitrage.json || \
  ln -s /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
    config/prediction_arbitrage.json

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb wallet status \
  --config config/prediction_arbitrage.json

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb preflight \
  --config config/prediction_arbitrage.json --no-submit
```

Expected: wallet status and no-submit preflight report the current real readiness without placing an order. If routing or credentials are externally unavailable, preserve the exact output for the final acceptance classification; do not weaken fail-closed logic.

- [ ] **Step 7: Self-review the diff against the approved design**

Run:

```bash
git diff main...HEAD --check
git diff main...HEAD --stat
if git diff main...HEAD -- src/open_trader tests | \
  rg '^\+.*(TODO|TBD|FIXME|placeholder|prediction_signal_prototype|DeepSeek)'; then
  exit 1
fi
for id in AC-01 AC-02 AC-03 AC-04 AC-05 AC-06 AC-07 AC-08 \
  AC-09 AC-10 AC-11 AC-12 AC-13 AC-14 AC-15; do
  rg -q "$id" \
    docs/superpowers/specs/2026-08-01-yes-no-arbitrage-signals-design.md
  rg -q "$id" \
    docs/superpowers/plans/2026-08-01-yes-no-arbitrage-signals.md
done
git status --short
```

Expected:

- no whitespace errors, prototype hooks, placeholders, or DeepSeek path;
- the renderer and E2E assertions, already covered by focused tests, expose none of the removed YES/NO copy (backend audit-field names may still exist deliberately);
- `package-lock.json` remains excluded;
- every design acceptance scenario has either a focused pytest or Playwright assertion;
- AC-01 through AC-15 appear in both the approved matrix and implementation traceability table;
- no notification code can invoke preview, preflight, or execution;
- no browser code can fetch Polymarket or calculate profit.

- [ ] **Step 8: Add and commit the dated operator changelog before any merge**

Add a `2026-08-01` entry describing:

- `当前机会` replaced by the one-second local `套利信号` component;
- HKT watcher/signal freshness clocks and bilingual title cache;
- link-free, observation-only Feishu alerts with 30-minute successful-delivery cooldown;
- manual `重新检查` -> `确认下单` execution boundary remains;
- LLM hedge remains unchanged.

Then run:

```bash
git add CHANGELOG.md
git commit -m "docs: log yes-no signal workflow"
git status --short
```

Expected: only the pre-existing uncommitted `package-lock.json` remains. Do not merge to `main` in this task.

- [ ] **Step 9: Make the candidate SHA runnable and deploy it for the final gate**

The Makefile resolves `.venv` inside the worktree. Reuse the repository virtual environment without committing it:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Expected: launchd reports `com.open-trader.dashboard` installed and `http://127.0.0.1:8766/` ready from this worktree.

- [ ] **Step 10: Run the one final Dashboard gate**

```bash
make acceptance
```

Expected final status: `PASS`.

This single command must prove AC-01 through AC-15 by aggregating the complete
Python suite, Prediction Playwright, real read-only Polymarket acceptance,
trend drawdown preflight, and live Dashboard process/API/browser/log checks.

- On `FAIL`, diagnose and fix the defect, rerun the relevant focused checks, recommit, redeploy the new candidate, and rerun `make acceptance`.
- On `BLOCKED`, report the real browser/external blocker. Do not substitute curl, fixtures, mocks, screenshots, or unit tests for acceptance.
- Do not describe the work as complete unless this command returns `PASS`.

- [ ] **Step 11: Redeploy the exact accepted SHA without source or data changes**

Record the accepted SHA, then reinstall the same worktree:

```bash
accepted_sha=$(git rev-parse HEAD)
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

launchctl print "gui/$UID/com.open-trader.dashboard"
dashboard_pid=$(lsof -nP -tiTCP:8766 -sTCP:LISTEN | head -n 1)
lsof -a -p "$dashboard_pid" -d cwd -Fn
ps -p "$dashboard_pid" -o pid=,lstart=,command=
git -C "$(lsof -a -p "$dashboard_pid" -d cwd -Fn | sed -n 's/^n//p' | head -n 1)" rev-parse HEAD
tail -n 100 logs/dashboard/launchd.out.log
tail -n 100 logs/dashboard/launchd.err.log
curl --fail --silent --show-error -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/
```

Expected:

- a fresh Dashboard PID after the post-acceptance reinstall;
- cwd is this exact feature worktree;
- live SHA equals `accepted_sha`;
- a fresh `dashboard_runtime` log line has the same PID/cwd/SHA and clean source state;
- no fresh traceback;
- HTTP `200` from `http://127.0.0.1:8766/`.

- [ ] **Step 12: Hand off for user review**

Report the accepted SHA, focused-test and Playwright counts, direct wallet/preflight result, `make acceptance` `PASS`, live PID/cwd/SHA/log proof, and review URL:

```text
http://127.0.0.1:8766/
```

State explicitly that Feishu is notification-only and orders remain manual through `重新检查` then `确认下单`. Do not capture screenshots unless the user asks.

## Acceptance traceability

| Criterion | Implementation and deterministic evidence | Final-gate evidence |
| --- | --- | --- |
| AC-01 | Task 5 renderer/static tests and 1440px/375px Playwright assert the original-style two-column layout and removed copy. | Prediction Playwright inside `make acceptance`. |
| AC-02 | Task 5 Playwright selects `交易与合并` and `事故`, injects signal/state responses, and asserts the selected tab is unchanged. | Prediction Playwright inside `make acceptance`. |
| AC-03 | Task 5 Playwright records request concurrency and asserts monitoring/readiness/metric DOM identity plus expanded-row state across two signal polls. | Prediction Playwright inside `make acceptance`. |
| AC-04 | Task 4 projection tests and Task 5 Playwright assert HKT clocks, frozen danger state, and removal/recovery of actions. | Python suite and Prediction Playwright inside `make acceptance`. |
| AC-05 | Tasks 1 and 4 prove immutable `initial_profit` and watcher-owned `live_profit`; Task 5 proves the three visible states `$0.38`, `$0.44`, and `—`. | Python suite and Prediction Playwright inside `make acceptance`. |
| AC-06 | Task 4 enumerates open, closed, stale, degraded, missing, active-execution, and breaker states; Task 5 asserts button presence rather than disabled-button copy. | Python suite and Prediction Playwright inside `make acceptance`. |
| AC-07 | Task 5 Playwright counts preview and execution requests for accepted and expired signals; existing execution pytest proves submit/idempotency boundaries. | Python suite and Prediction Playwright inside `make acceptance`. |
| AC-08 | Task 2 renderer/service tests assert exact `【YES/NO 套利信号】+$0.38` content, Feishu-only routing, and forbidden-field absence using a notifier double. | Python suite inside `make acceptance`; no real Feishu send. |
| AC-09 | Tasks 1 and 2 test successful-delivery cooldown, failed-delivery retry, per-market isolation, episode persistence, three-attempt ceiling, and close-stop behavior. | Python suite inside `make acceptance`. |
| AC-10 | Task 3 command/cache/timeout tests prove one non-blocking Codex Luna worker; Task 5 proves English-first then cached bilingual UI. | Python suite and Prediction Playwright inside `make acceptance`; no live Codex requirement. |
| AC-11 | Task 5 renderer and Playwright tests assert title-only translation, English retention, no neutral `仅监控`, no `预计`, and preserved exception status. | Python suite and Prediction Playwright inside `make acceptance`. |
| AC-12 | Tasks 2, 3, and 5 retain existing threshold notification/candidate/preview/execution regression cases. | Complete Python suite and Prediction Playwright inside `make acceptance`. |
| AC-13 | Task 5 Playwright checks 1440px/375px overflow, 44px controls, modal Escape, and focus restoration. | Prediction Playwright inside `make acceptance`. |
| AC-14 | Task 6 runs wallet status and `preflight --no-submit`; live prediction and Dashboard acceptance verify schema, fail-closed readiness, process, API, and browser truth. | Live read-only portions of `make acceptance`; external unavailability is `BLOCKED`. |
| AC-15 | Task 6 deploys the candidate, requires final `PASS`, then reinstalls the unchanged accepted SHA and proves new PID, cwd, SHA, fresh logs, and HTTP 200. | Final command output plus post-gate runtime evidence. |

## Final implementation checklist

- [ ] Durable `initial_profit` is immutable; `live_profit` comes only from the current watcher snapshot.
- [ ] `最终利润` is not shown for signals; realized profit remains only in `交易与合并` as `已实现`.
- [ ] One-second requests replace only the selected signal panel DOM.
- [ ] Endpoint failure freezes/redens signal freshness and removes every signal action.
- [ ] Feishu contains no link/button and never calls preflight or execution.
- [ ] Title translation is one background Codex Luna worker and never delays a signal.
- [ ] LLM hedge tests and behavior remain unchanged.
- [ ] Original Dashboard style is preserved on desktop and 375px.
- [ ] AC-01 through AC-15 each have the evidence named in the traceability table.
- [ ] `CHANGELOG.md` is committed before any merge.
- [ ] Final `make acceptance` is `PASS`, then the exact accepted SHA is redeployed and proven live.
