# Account API Production Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Complete Issue #21 by routing the browser's Account read path through Frontend Gateway to a production-mode Account API, while preserving independent Legacy modules, immutable rollback, single-writer safety, and the final Dashboard acceptance gate.

**Architecture:** Keep the existing three-process stack. Frontend Gateway sends the exact path `/api/v1/account/snapshot` to Account API on `127.0.0.1:8768` and every other `/api/*` path to Legacy Dashboard on `127.0.0.1:8767`. The browser owns the presentation join between Account snapshot facts and Legacy Trend/Research enrichment, using opaque stable IDs. A baseline SHA freezes all Account-side changes before a later cutover SHA changes Gateway, Legacy projection, browser, acceptance, and documentation.

**Tech Stack:** Python 3.12, stdlib `http.server` and `http.client`, launchd shell installers, vanilla browser JavaScript, pytest, Node-based JavaScript unit probes, Playwright through `make acceptance`.

**Approved design:** `docs/superpowers/specs/2026-08-03-account-api-production-cutover-design.md`

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/issue-21-account-api-cutover` on `codex/issue-21-account-api-cutover`, which was created from local `main` at `add5c6c3179f06430219cf986f61344e42a5ef40`.
- Preserve the user's unrelated dirty files in `/Users/ray/projects/open_trader`; use it only as the shared runtime-data root and never as the source checkout for a candidate process.
- Do not change Account v1 fields, Account/quote publication semantics, Worker cadence, broker selection, valuation, strategy, report, execution, Dashboard layout, or statement command semantics.
- Add no dependency, cache, database, queue, service discovery layer, WebSocket, BFF, or fallback data path.
- Browser Account renderers must never read Account values from `/api/dashboard`, including on first load, `503`, timeout, or later recovery.
- `/api/quotes` remains implemented for other consumers, but browser source and browser acceptance must not request it.
- Account API and Worker must always share one release SHA. Gateway and Legacy may intentionally differ from that SHA only during the rollback drill.
- `scripts/install_dashboard_launchd.sh --mode single` remains an explicit operator break-glass command, but no #21 test, deployment, rollback, or acceptance command may invoke it.
- Use focused tests during development. Run `make acceptance` exactly as the final Dashboard gate after all source and documentation commits are frozen.
- Before any merge, commit the dated operator-facing `CHANGELOG.md` entry. Do not push, merge, close Issue #21, or unlock Issue #22 until the operator accepts R3.
- Apply Ponytail: reuse `_opaque_id`, the existing Gateway proxy, current launchd installers, existing Dashboard renderers, and current acceptance machinery. Do not introduce a router class, client SDK, state machine class, or new test framework.

## Release Checkpoints

| Checkpoint | Last task included | Permitted production files | Runtime purpose |
| --- | --- | --- | --- |
| Baseline SHA | Task 4 | Account stable-ID helper, Account API mode/guard, Account API installer, Worker installer lock check | First production Account release and rollback target |
| Cutover SHA | Task 11 | Gateway routing, Legacy enrichment ID, browser polling/composition, installer readiness, acceptance, docs/log | Final candidate and accepted deployment |

After Task 4, record `git rev-parse HEAD` as the baseline SHA. Tasks 6–11 must not modify:

- `src/open_trader/account_snapshot.py`
- `src/open_trader/account_api.py`
- `src/open_trader/account_sync_worker.py`
- `ops/launchd/com.open-trader.account-api.plist.template`
- `scripts/install_account_api_launchd.sh`
- `scripts/install_account_sync_launchd.sh`

If any of those files must change, stop, discard the recorded checkpoint, make the correction, rerun Tasks 1–5, and record a new baseline SHA.

## Task 1: Publish the Existing Stable-ID Constructors

**Files:**

- Modify: `src/open_trader/account_snapshot.py`
- Modify: `tests/test_account_api.py`

- [ ] **Step 1: Write the failing public-helper test**

Import `build_instrument_id` and `build_position_id` from `open_trader.account_snapshot`. Extend the existing opaque-ID test so it asserts:

```python
instrument_id = build_instrument_id("us", "OPTION", " vixy260821c22000 ")
assert instrument_id == position["instrument_id"]
assert build_position_id("FUTU", "futu_main", instrument_id) == position["position_id"]
```

Also assert case/outer-whitespace normalization is identical to `_position_row()` and changing `asset_class` changes `instrument_id`.

- [ ] **Step 2: Prove the test is red**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_account_api.py -k 'opaque or stable_id'
```

Expected: collection fails because the two public functions do not exist.

- [ ] **Step 3: Add only the two public functions**

In `account_snapshot.py`, keep `_opaque_id()` private and add:

```python
def build_instrument_id(market: str, asset_class: str, symbol: str) -> str:
    return _opaque_id("ins_", [
        market.strip().upper(),
        asset_class.strip().lower(),
        symbol.strip().upper(),
    ])


def build_position_id(broker: str, account_alias: str, instrument_id: str) -> str:
    return _opaque_id("pos_", [
        broker.strip().lower(),
        account_alias.strip(),
        instrument_id,
    ])
```

Change `_position_row()` to call those functions. Do not change `_opaque_id()`, hash input order, prefixes, or public snapshot output.

- [ ] **Step 4: Prove parity and Account tests are green**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_account_api.py
```

Expected: all tests pass, including the existing deterministic-ID and parity mismatch cases.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/account_snapshot.py tests/test_account_api.py
git commit -m "refactor: expose Account stable IDs (#21)"
```

## Task 2: Add Production Mode and the Shadow Traffic Guard

**Files:**

- Modify: `src/open_trader/account_api.py`
- Modify: `tests/test_account_api.py`

- [ ] **Step 1: Write failing mode and health tests**

Add tests that create Account API in its default mode and in `mode="production"`. Assert `/healthz` reports `shadow` by default and `production` explicitly. Add a parser/entrypoint test proving `--mode` accepts only `shadow|production` and defaults to `shadow`.

- [ ] **Step 2: Write failing route-marker tests**

Use the existing Account API server fixture and send:

```http
X-Open-Trader-Account-Route: production
```

Assert:

- shadow returns HTTP 503;
- the response has exactly the frozen unavailable top-level fields `schema_version`, `status`, `release`, and `errors`;
- `schema_version == 1`, `status == "unavailable"`;
- the one error is `{"code": "account_api_shadow_only", "source": "release", "message": "Account API is running in shadow mode", "retryable": true}`;
- `release` carries the API and current Worker SHA values;
- production accepts the marker and returns the normal snapshot response;
- a direct request without the marker still works in both modes;
- ETag/304 behavior remains unchanged in production.

- [ ] **Step 3: Prove the tests are red**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_account_api.py -k 'mode or shadow_only or marker'
```

Expected: failures show hard-coded shadow health and no request guard.

- [ ] **Step 4: Implement the minimum mode surface**

In `account_api.py`:

- define `AccountApiMode = Literal["shadow", "production"]`;
- define the marker constants once;
- add `mode: AccountApiMode = "shadow"` to `create_account_api()`;
- add `mode: AccountApiMode = "shadow"` to `serve_account_api()`;
- add CLI `--mode` with fixed choices and default `shadow`;
- expose the selected mode in `/healthz` and the runtime log;
- before `load_account_snapshot()`, reject only the exact production marker when mode is shadow;
- build the contract-safe unavailable payload locally from the runtime API SHA and `load_worker_git_sha(data_dir)`;
- leave direct parity requests marker-free.

Do not generalize this into authentication middleware or alter snapshot validation.

- [ ] **Step 5: Run the Account suite**

```bash
.venv/bin/python -m pytest -q tests/test_account_api.py
```

Expected: all Account API, parity, ETag, unavailable-envelope, and CLI tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/account_api.py tests/test_account_api.py
git commit -m "feat: add Account API production mode (#21)"
```

## Task 3: Make the Account API Installer Mode-Aware

**Files:**

- Modify: `ops/launchd/com.open-trader.account-api.plist.template`
- Modify: `scripts/install_account_api_launchd.sh`
- Modify: `tests/test_account_api_launchd.py`

- [ ] **Step 1: Write failing installer tests**

Update the template assertion to require:

```text
account-api --data-dir OPEN_TRADER_DATA_DIR --mode OPEN_TRADER_ACCOUNT_API_MODE
```

Add tests proving:

- installer default renders `shadow`;
- `--mode production` renders `production`;
- any other mode exits 2 without touching launchd;
- readiness accepts health only when `mode` equals the requested mode;
- production mode still requires exact PID, cwd, sole `127.0.0.1:8768` listener, API SHA, Worker SHA, and `release_match: true`;
- a timeout removes only the Account API launchd label and names the expected mode in stderr.

- [ ] **Step 2: Prove the tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_account_api_launchd.py
```

Expected: template and argument assertions fail because mode is fixed to shadow.

- [ ] **Step 3: Implement one installer argument**

Add `MODE="shadow"`, parse `--mode`, validate `shadow|production`, substitute `OPEN_TRADER_ACCOUNT_API_MODE`, and pass the expected mode into `health_matches()`. Preserve every existing PID/cwd/SHA/listener check. For a production install, the exact timeout text is:

```text
Account API did not publish matching production health
```

- [ ] **Step 4: Run focused tests and a real dry run**

```bash
.venv/bin/python -m pytest -q tests/test_account_api_launchd.py
scripts/install_account_api_launchd.sh --dry-run --mode production \
  --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python | plutil -lint -
```

Expected: tests pass and plist lint prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add ops/launchd/com.open-trader.account-api.plist.template scripts/install_account_api_launchd.sh tests/test_account_api_launchd.py
git commit -m "ops: install Account API by runtime mode (#21)"
```

## Task 4: Require Writer-Lock Release Before Worker Bootstrap

**Files:**

- Modify: `scripts/install_account_sync_launchd.sh`
- Modify: `tests/test_account_sync_launchd.py`

- [ ] **Step 1: Write the failing installer safety test**

Extend the fake launchd test to hold `data/account_sync/controller.lock` in a child Python process after bootout. Assert the installer does not call bootstrap while the lock is held, then succeeds once the child releases it. Add a timeout case that exits nonzero and never bootstraps if the lock remains held.

- [ ] **Step 2: Prove the test is red**

```bash
.venv/bin/python -m pytest -q tests/test_account_sync_launchd.py -k 'lock or bootout'
```

Expected: current installer bootstraps immediately after launchd disappearance.

- [ ] **Step 3: Add one stdlib lock probe**

After `wait_agent_absent` and before truncating logs or bootstrapping, call a small inline Python probe using `fcntl.flock(LOCK_EX | LOCK_NB)` on `$DATA_DIR/account_sync/controller.lock`. Retry within `WAIT_SECONDS`; release immediately on success. On timeout print:

```text
account sync writer lock is still held
```

and exit nonzero. Do not modify Worker locking code or add a second lock.

- [ ] **Step 4: Run Worker installer and Account regression tests**

```bash
.venv/bin/python -m pytest -q tests/test_account_sync_launchd.py tests/test_account_api_launchd.py tests/test_account_api.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit and freeze the baseline SHA**

```bash
git add scripts/install_account_sync_launchd.sh tests/test_account_sync_launchd.py
git commit -m "ops: wait for Account writer lock release (#21)"
git rev-parse HEAD
git diff --quiet HEAD -- src/open_trader/account_snapshot.py src/open_trader/account_api.py src/open_trader/account_sync_worker.py ops/launchd/com.open-trader.account-api.plist.template scripts/install_account_api_launchd.sh scripts/install_account_sync_launchd.sh
```

Expected: `git rev-parse` prints the immutable baseline SHA and `git diff --quiet` exits 0. Record the SHA in the execution notes used for the Issue #21 evidence.

## Task 5: Prove the Baseline Account Release Before Browser Cutover

**Files:**

- Create runtime checkout: `/Users/ray/projects/open_trader/.worktrees/issue-21-account-api-baseline`
- Read runtime data: `/Users/ray/projects/open_trader/data`
- Read/write launchd plists and candidate logs through the existing installers
- Do not modify tracked source files

- [ ] **Step 1: Create an immutable detached baseline checkout**

From the cutover worktree, set `baseline_sha` to the Task 4 SHA and run:

```bash
baseline_root=/Users/ray/projects/open_trader/.worktrees/issue-21-account-api-baseline
test ! -e "$baseline_root"
git worktree add --detach "$baseline_root" "$baseline_sha"
ln -s ../../.venv "$baseline_root/.venv"
test "$(git -C "$baseline_root" rev-parse HEAD)" = "$baseline_sha"
test -z "$(git -C "$baseline_root" status --porcelain)"
```

Expected: clean detached checkout at the exact baseline SHA.

- [ ] **Step 2: Record the old Account runtime evidence**

Record, without secrets:

- launchd PIDs for `com.open-trader.account-sync-controller` and `com.open-trader.account-api`;
- each process cwd and Git SHA;
- sole listener on `127.0.0.1:8768`;
- `controller_status.json` heartbeat and last-success timestamps;
- `latest/account_sync_state.json` generation and accepted timestamps;
- `latest/quotes.json` last-success timestamp;
- SHA-256 of the three JSON files.

This is pre-change evidence, not a completion claim.

- [ ] **Step 3: Preflight on temporary ports**

Run all Account tests from the detached checkout, then start a production-mode Account API with `create_account_api(Path("/Users/ray/projects/open_trader/data"), host="127.0.0.1", port=0, mode="production")` in a short Python probe. Prove health mode, normal `200`, ETag, conditional `304`, and parity against `/Users/ray/projects/open_trader/data`. Stop the probe before touching launchd.

Expected: no request is routed through live Gateway and no runtime publication is changed.

- [ ] **Step 4: Install the baseline Worker safely**

```bash
"$baseline_root/scripts/install_account_sync_launchd.sh" \
  --repo-root "$baseline_root" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Verify the old PID is gone, the lock transition completed, the new PID/cwd/SHA match baseline, and no second Worker exists. Wait for both a successful Account refresh and a successful quote publication whose timestamps are after the new process start. A content hash may remain equal when facts have not changed.

- [ ] **Step 5: Install the baseline API in production mode**

```bash
"$baseline_root/scripts/install_account_api_launchd.sh" \
  --mode production \
  --repo-root "$baseline_root" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Verify exact PID/cwd/SHA, sole listener, `mode: production`, `release_match: true`, healthy snapshot, ETag/304, parity PASS, fresh runtime log, and empty/fresh stderr. Save this machine evidence before continuing.

- [ ] **Step 6: Confirm the browser is not cut over yet**

Verify live Gateway still sends `/api/v1/account/snapshot` to Legacy and that no source commit after the baseline has been deployed. This expected pre-cutover result closes the baseline checkpoint; it is not the final operator gate.

## Task 6: Route the Exact Account Path Through Gateway

**Files:**

- Modify: `src/open_trader/frontend_gateway.py`
- Modify: `ops/launchd/com.open-trader.frontend-gateway.plist.template`
- Modify: `tests/test_frontend_gateway.py`

- [ ] **Step 1: Generalize the existing test helper to two upstreams**

Allow `_gateway()` to receive both Legacy and Account ports. Keep `_Upstream`; make its health body configurable instead of adding another server class.

- [ ] **Step 2: Write failing exact-route and transparent-response tests**

Prove:

- `/api/v1/account/snapshot` and the same path with a query go only to Account;
- `/api/v1/account/snapshot/child`, `/api/dashboard`, `/api/quotes`, statements, simulate, and all other `/api/*` go only to Legacy;
- Account `200`, `304`, contract `503`, ETag, repeated headers, body, status, and reason pass through unchanged;
- Account transport failure alone yields Gateway 503 `account_module_unavailable`;
- Legacy transport failure still yields `legacy_dashboard_unavailable`;
- a caller-supplied marker is removed and Account receives exactly `X-Open-Trader-Account-Route: production`;
- Account does not receive Legacy Origin/Referer authority.

- [ ] **Step 3: Write failing dual-health tests**

Assert Gateway `/healthz` always returns 200 and reports:

- compatibility `upstream_status` for Legacy;
- `legacy_upstream_status`;
- `account_upstream_status` equal to `ok` only for a 200 Account health payload whose module is `account_api` and mode is `production`;
- Account shadow/unreachable and Legacy unreachable independently as unavailable.

- [ ] **Step 4: Prove the tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_frontend_gateway.py
```

Expected: Account requests still reach Legacy and dual-health fields are absent.

- [ ] **Step 5: Extend the existing proxy without adding a router abstraction**

Add `account_upstream_host="127.0.0.1"` and `account_upstream_port=8768` to `FrontendGatewayConfig`, validate both loopback addresses and ports, and add CLI/plist arguments. In `_proxy()`, choose one host/port/authority/origin/error tuple from the parsed exact path and reuse all existing request and response forwarding. Strip the marker from copied headers, then add it only for the Account route.

Keep `upstream_status` as the Legacy compatibility alias and add only the two explicit health fields required above.

- [ ] **Step 6: Run Gateway tests and plist validation**

```bash
.venv/bin/python -m pytest -q tests/test_frontend_gateway.py
plutil -lint ops/launchd/com.open-trader.frontend-gateway.plist.template
```

Expected: all tests pass and plist is valid.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/frontend_gateway.py ops/launchd/com.open-trader.frontend-gateway.plist.template tests/test_frontend_gateway.py
git commit -m "feat: route Account snapshots through Gateway (#21)"
```

## Task 7: Add Only the Transitional Enrichment Identity

**Files:**

- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing projection test**

Extend the existing `load_dashboard_state()` holding projection test. Assert every real holding has the `instrument_id` produced by `build_instrument_id(market, asset_class, symbol)`. Assert the transitional holding does not gain `position_id`, Account status, quantity, price, valuation, weight, or Account release fields.

- [ ] **Step 2: Prove the test is red**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py -k 'holding and instrument_id'
```

Expected: `instrument_id` is absent.

- [ ] **Step 3: Add one field at the shared projection point**

Import `build_instrument_id` from `account_snapshot` and set `holding["instrument_id"]` in `_merge_holding()` from the holding row's market, asset class, and symbol. Do not copy any Account projection values and do not change the current Legacy API schema version.

- [ ] **Step 4: Run Dashboard projection tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose holding enrichment identity (#21)"
```

## Task 8: Split Browser Loading and Remove the Quote Reload Loop

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Replace the old quote-loop test with Account polling tests**

In the existing Node probes, stub `/api/dashboard` and `/api/v1/account/snapshot` independently. Assert DOMContentLoaded starts both immediately, Account repeats every 5000 ms, timeout is 4000 ms, only one Account request can be active, and no call targets `/api/quotes`.

Assert the second Account request sends the first successful ETag through `If-None-Match`, while Dashboard is not reloaded by Account polling.

- [ ] **Step 2: Write failing state-transition tests**

Cover:

- first-load Account failure leaves `accountSnapshot === null` and renders Account unavailable while Dashboard-owned panels render;
- later 503/network/AbortError preserves the last snapshot, retains its ETag, marks the view frozen/unavailable, and disables Account-dependent actions;
- 304 keeps the cached healthy or stale snapshot, clears transport error, and restores the correct action gate;
- 200 stale replaces the snapshot and disables actions;
- 200 healthy atomically replaces snapshot/ETag and enables eligible actions;
- Legacy `/api/dashboard` failure does not clear a healthy Account snapshot.

- [ ] **Step 3: Prove the polling tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'account_poll or quote_poll or independent_load or 304'
```

Expected: current browser calls `/api/quotes`, reloads Dashboard, and has no Account state.

- [ ] **Step 4: Implement the smallest independent Account state**

Replace `quotes`, `quotePayload`, `refreshActive`, and `quoteIntervalId` with:

```javascript
accountSnapshot: null,
accountEtag: "",
accountError: null,
accountRequestInFlight: false,
accountIntervalId: null,
```

Add only `loadAccountSnapshot()` and `scheduleAccountPolling()`:

- create an `AbortController` per request and clear its four-second timer in `finally`;
- return immediately when a request is in flight;
- send `If-None-Match` only when an ETag exists;
- handle 304 without calling `response.json()`;
- preserve snapshot and ETag on failure;
- start immediately and use fixed `setInterval(loadAccountSnapshot, 5000)`;
- remove `refreshQuotes()` and the `loadDashboard()` call chain from quote polling.

Make `loadDashboard()` render only Dashboard-owned state and make `renderLoadError()` stop clearing Account state or the Account interval.

- [ ] **Step 5: Run the focused browser tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'account_poll or quote_poll or independent_load or 304'
```

Expected: the new state transitions pass and request capture contains no `/api/quotes`.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: poll Account snapshots independently (#21)"
```

## Task 9: Move Account Rendering to Snapshot State and Stable IDs

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write the conflicting-owner test**

Return deliberately conflicting Account values from `/api/dashboard` and Account snapshot: totals, broker totals, quantity, price, source status, cash, and account alias. Assert rendered Account cards, header summary, holdings, source rows, cash, and connection state use only snapshot values. Repeat after an Account 503 and assert Legacy values never appear.

- [ ] **Step 2: Write stable-ID composition tests**

Use positions and enrichment rows where symbols look equal but IDs differ. Assert:

- row key is exactly `position_id`;
- one unique matching `instrument_id` supplies Trend/Research enrichment;
- missing ID or zero/multiple enrichment matches displays the Account position and an enrichment-unavailable state;
- no broker/market/symbol/index fallback occurs.

- [ ] **Step 3: Write owner-isolation and statement-gate tests**

Assert Account failure leaves Trend simulate/report, Research, Prediction, Kelly, and backtest controls usable. Assert Legacy failure leaves Account cards/holdings usable. Assert statement upload is enabled only for a current healthy Account state, including healthy cached state reconfirmed by 304, and disabled for first load, stale, frozen 503, timeout, and network failure.

- [ ] **Step 4: Prove the rendering tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'account_owner or stable_id or statement_upload or module_isolation'
```

Expected: renderers still read `state.dashboard` Account fields and use symbol/index identity.

- [ ] **Step 5: Redirect existing render helpers to Account snapshot**

Change only current Account helpers:

- header summary reads `state.accountSnapshot.summary`;
- `brokerSummaries()` reads `broker_summaries`;
- `accountHoldingGroups()` reads `positions`, keys by `position_id`, and joins `state.dashboard.holdings` by a unique `instrument_id`;
- `getCashRows()` reads `cash_balances`;
- `brokerSyncStatus()` and `brokerSourceStatus()` read `sources.account.brokers`;
- connection panel reads `sources.quotes`, `quote_as_of`, current transport state, and the last accepted snapshot time;
- `brokerAccountAlias()` reads snapshot summaries/cash/positions, not Dashboard Account projections;
- statement markup computes `accountActionsEnabled()` from current healthy, non-frozen Account state.

Reuse existing render functions and CSS status classes. Add no layout, interaction, or copy redesign beyond the explicit unavailable/frozen truth state.

- [ ] **Step 6: Scan for forbidden browser ownership**

```bash
rg -n 'state\.dashboard\??\.(summary|broker_summaries|broker_positions|cash_rows|cash_details|account_sync|source_statuses)|/api/quotes|state\.quotes|quotePayload|quoteIntervalId|refreshQuotes' src/open_trader/dashboard_static/dashboard.js
```

Expected: no matches.

- [ ] **Step 7: Run the complete Dashboard web suite**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py
```

Expected: all tests pass. Update old tests only where they asserted the superseded browser read path; keep server `/api/quotes` tests because endpoint removal belongs to #23.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: render Account state from stable snapshots (#21)"
```

## Task 10: Make Stack Installation Fail Closed

**Files:**

- Modify: `scripts/install_dashboard_launchd.sh`
- Modify: `tests/test_dashboard_launchd_stack.py`
- Modify: `tests/test_prediction_arbitrage_launchd.py` only if its shared installer assertion fails

- [ ] **Step 1: Write failing stack tests**

Assert stack-mode readiness requires Gateway health with Legacy `ok` and Account `ok`. Simulate Legacy failure, Account shadow health, Account unavailable, and Gateway bootstrap failure. In every case assert nonzero exit, no automatic bootstrap of `com.open-trader.dashboard`, and no invocation of `--mode single` behavior.

Keep a separate test proving an explicit operator call with `--mode single` still installs the single-process job.

- [ ] **Step 2: Prove the tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_launchd_stack.py
```

Expected: current failure path restores the single-process Dashboard.

- [ ] **Step 3: Delete automatic fallback**

Delete `restore_single()` and `fail_stack()`. On stack readiness failure, print the specific failing component, leave the failure visible, and exit nonzero. Extend `health_matches()` to require both `legacy_upstream_status == "ok"` and `account_upstream_status == "ok"` for Gateway readiness. Do not change explicit `install_single()`.

- [ ] **Step 4: Run installer suites and dry-run stack plist checks**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_launchd_stack.py tests/test_prediction_arbitrage_launchd.py
scripts/install_dashboard_launchd.sh --dry-run --mode stack \
  --repo-root "$PWD" --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Expected: tests pass; dry run renders only Gateway and Legacy stack plists.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_dashboard_launchd.sh tests/test_dashboard_launchd_stack.py
git diff --quiet -- tests/test_prediction_arbitrage_launchd.py || git add tests/test_prediction_arbitrage_launchd.py
git commit -m "ops: fail closed on Dashboard stack cutover (#21)"
```

## Task 11: Extend Acceptance, Runbooks, and the Operator Log

**Files:**

- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `docs/operations/account-api-shadow-runtime.md`
- Create: `docs/operations/account-api-production-cutover.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write failing Account runtime acceptance tests**

Add `--account-url` defaulting to `http://127.0.0.1:8768` and `--account-log`. Extend `_runtime_evidence()` or add the smallest Account-specific validator to prove:

- Account health schema/module/status/mode production;
- PID, cwd, candidate SHA, source state, start time, sole listener, and fresh log;
- `api_git_sha == worker_git_sha == expected_sha` during normal final deployment;
- snapshot contract, production route, ETag, conditional 304, and parity;
- Gateway health reports both upstreams healthy.

Do not require Gateway SHA to equal a historical baseline during rollback; normal final acceptance still requires all processes at the final candidate SHA.

- [ ] **Step 2: Write failing browser-network acceptance tests**

Extend the Playwright request/response observers to wait for at least two five-second Account poll opportunities and prove:

- the page requested `/api/v1/account/snapshot` through port 8766;
- a later request carries `If-None-Match` and receives 304 or a contract-valid 200;
- no browser request targets `/api/quotes`;
- `/api/dashboard` is still requested for Legacy-owned modules;
- deterministic conflicting Account values in Dashboard do not appear in Account UI;
- desktop and mobile Account state remain usable while the other owner is independently degraded in test fixtures.

- [ ] **Step 3: Prove acceptance tests are red**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k 'account_api or account_poll or runtime_evidence or browser_requests'
```

Expected: Account runtime arguments and browser proof are absent.

- [ ] **Step 4: Implement the minimum acceptance additions**

Reuse `_runtime_evidence`, `_runtime_health_errors`, `_log_errors`, and existing Playwright request tracking. Add `ACCOUNT_API_LOG` to Makefile and pass both Account URL and log to `open_trader.dashboard_acceptance`. Replace browser-side acceptance use of `_fetch_quotes_payload()` with the Account snapshot/ETag check, but leave the server endpoint validation helpers in place if other tests still exercise them.

- [ ] **Step 5: Run focused acceptance and all impacted suites**

```bash
.venv/bin/python -m pytest -q \
  tests/test_account_api.py \
  tests/test_account_api_launchd.py \
  tests/test_account_sync_launchd.py \
  tests/test_frontend_gateway.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_launchd_stack.py \
  tests/test_dashboard_acceptance.py
```

Expected: all tests pass.

- [ ] **Step 6: Write the production runbook**

Update the shadow runbook to explain the two modes and link the new production cutover runbook. The production runbook must contain:

- the recorded baseline SHA, plus commands that derive and verify the immutable cutover SHA from the candidate checkout;
- exact detached-worktree, Worker, Account API, and Dashboard stack installer commands;
- preflight, post-start publication, PID/cwd/SHA/listener/log, parity, ETag/304 checks;
- inverse rollback and re-cutover sequence with writer-lock checks;
- explicit prohibition on `--mode single` for #21;
- the controlled Account-only fault injection and recovery steps;
- cleanup only after operator acceptance.

Update README with the production ownership route and runbook link. Add a dated `2026-08-03` CHANGELOG entry describing operator-visible Account polling, failure isolation, stable-ID join, and fail-closed deployment. Do not claim acceptance before it runs.

- [ ] **Step 7: Commit documentation and freeze the cutover SHA**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile README.md docs/operations/account-api-shadow-runtime.md docs/operations/account-api-production-cutover.md CHANGELOG.md
git commit -m "docs: prepare Account API production cutover (#21)"
git rev-parse HEAD
git status --short
```

Expected: record the cutover SHA and the worktree is clean. Confirm the Account-side protected-file diff from the baseline is empty:

```bash
git diff --exit-code "$baseline_sha"..HEAD -- \
  src/open_trader/account_snapshot.py \
  src/open_trader/account_api.py \
  src/open_trader/account_sync_worker.py \
  ops/launchd/com.open-trader.account-api.plist.template \
  scripts/install_account_api_launchd.sh \
  scripts/install_account_sync_launchd.sh
```

## Task 12: Prove Cutover, Independent Rollback, Fault Isolation, and Final Acceptance

**Files:**

- Runtime checkouts and launchd jobs only until evidence forces a source correction
- If a correction changes the candidate SHA, update the external execution record and Issue #21 evidence draft before the final test run; do not make a tracked file self-reference its own commit SHA
- Do not modify the user-owned root checkout

- [ ] **Step 1: Run the complete automated suite before live cutover**

```bash
.venv/bin/python -m pytest -q
```

Expected: complete pytest suite passes. If a source correction is necessary, commit it, rerun the relevant focused suite and full pytest, update cutover SHA documentation, and keep `make acceptance` unrun.

- [ ] **Step 2: Preflight the complete candidate on temporary ports**

Start candidate Account API in production mode, Legacy Dashboard, and Gateway on temporary loopback ports using shared runtime data. Prove exact Account routing, non-Account routing, Account marker, production guard, Dashboard shell, snapshot 200/304/503 behavior, and no change to live 8766. Stop every temporary process and verify the temporary listeners are gone.

- [ ] **Step 3: Move Account Worker and API from baseline to cutover SHA**

Use the same safe order as Task 5:

1. record baseline PID/cwd/SHA and publication timestamps;
2. install cutover Worker and prove old PID gone plus writer lock released before bootstrap;
3. wait for successful post-start Account and quote publication;
4. install cutover Account API in production mode;
5. prove API/Worker SHA match, mode, listener, health, snapshot, ETag/304, parity, and fresh logs.

Do not deploy Gateway until all five checks pass.

- [ ] **Step 4: Deploy the cutover Gateway/Legacy stack**

```bash
scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Verify Gateway 8766, Legacy 8767, and Account API 8768 listeners; exact process cwd/SHA; dual-upstream health; Account route ETag/304; `/api/dashboard` and another Legacy module route; and HTTP 200 at `http://127.0.0.1:8766/`.

- [ ] **Step 5: Perform the independent rollback drill**

Leave cutover Gateway/Legacy running. Move only Worker/API back to the baseline detached checkout using the safe stop-lock-start-publication-API sequence. Prove:

- Gateway/Legacy remain cutover SHA;
- Account API/Worker both report baseline SHA and production mode;
- browser Account requests still use `/api/v1/account/snapshot`;
- no `/api/quotes` or Legacy Account fallback occurs;
- cross-module SHA difference is not reported as Account release mismatch.

Then repeat the safe sequence to return Worker/API to cutover SHA and prove all processes are stable.

- [ ] **Step 6: Perform the controlled Account-only fault injection**

Boot out only `com.open-trader.account-api`. Verify:

- Gateway Account route returns explicit 503;
- Gateway health stays HTTP 200 with Legacy healthy and Account unavailable;
- `/api/dashboard`, Trend simulate/report, Research, Prediction, Kelly, and backtest surfaces remain available;
- the browser preserves the last Account snapshot as visibly frozen and disables statement upload;
- the Account Worker continues and remains the sole writer.

Restart Account API from the unchanged cutover SHA. Verify a browser poll recovers on 304 or 200, actions reflect the recovered domain state, and fresh API logs identify the new PID/SHA/cwd. Return the runtime to stable health.

- [ ] **Step 7: Run the one final Dashboard gate**

Only after all source changes and runtime drills are complete:

```bash
make acceptance
```

Expected: final line `PASS`. On `FAIL`, diagnose, fix, recommit, rerun focused/full tests and the runtime sequence, then rerun the final gate. On `BLOCKED`, report the external blocker and do not substitute fixtures, curl, screenshots, or unit tests.

- [ ] **Step 8: Redeploy the exact accepted SHA**

Without changing source or data, reinstall Worker, Account API production mode, and Dashboard stack from the exact accepted cutover checkout. Verify new PIDs, exact cwd/SHA, Worker/API release match, fresh logs after the new process starts, all three listeners, Gateway dual health, and HTTP 200 from:

```text
http://127.0.0.1:8766/
```

This exact-SHA restart does not require a second acceptance run.

- [ ] **Step 9: Prepare the operator-review evidence**

Post a secret-free Issue #21 comment containing:

- restated target and non-goals;
- baseline and accepted cutover SHAs;
- focused/full test counts and `make acceptance: PASS`;
- baseline, cutover, rollback, re-cutover, and fault-injection PID/cwd/SHA/publication evidence;
- Account ETag/304 and browser no-`/api/quotes` proof;
- final review URL.

Leave Issue #21 open at the operator-review gate. Do not merge, push, close it, delete either runtime checkout, or unlock #22 until the user explicitly accepts R3.

## Final Self-Review Checklist

- [ ] Every approved design decision has a corresponding implementation or verification step.
- [ ] No task after baseline freeze changes an Account-side protected file.
- [ ] Browser forbidden-owner scan is empty while server `/api/quotes` compatibility remains.
- [ ] Missing or ambiguous enrichment identity never falls back to symbol matching.
- [ ] Stack failure cannot automatically restore Legacy Account ownership.
- [ ] Runtime proof distinguishes normal all-at-cutover deployment from intentional mixed-SHA rollback.
- [ ] Final `make acceptance` is last, reports PASS, and the exact accepted SHA is redeployed before review.
- [ ] `CHANGELOG.md` is committed before any future merge.
- [ ] No secrets, screenshots, speculative abstractions, or new dependencies were added.
