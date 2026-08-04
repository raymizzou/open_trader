# Account Production Consumer Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Account HTTP contract the only active production read seam, pin one Account snapshot per Trend execution, remove Account ownership from Legacy Dashboard, and disable the unused Premarket and T-signal production entrypoints.

**Architecture:** Reuse the existing Account API, snapshot, statement publication, Frontend Gateway, Trend report, and Dashboard composition paths. Add two stdlib HTTP functions and one immutable statement-facts GET route; pass a pinned mapping through existing Trend functions; delete raw readers and Legacy Account projections instead of adding adapters or fallback modes. The Gateway remains a transparent two-upstream proxy and the browser remains the only Account/Legacy composition layer.

**Tech Stack:** Python 3.12 stdlib (`urllib`, `json`, `Decimal`), pytest, vanilla JavaScript, existing Node VM/browser checks, launchd, and `make acceptance`.

## Global Constraints

- Implement in the isolated worktree `/Users/ray/projects/open_trader/.worktrees/issue-23-consumer-migration-design`, branch `codex/issue-23-consumer-migration-design`, based on local `main@3bde68a9c1b30e4be9b73bc526b52303aea49500`. Preserve unrelated files in `/Users/ray/projects/open_trader`.
- Treat `docs/superpowers/specs/2026-08-04-account-production-consumer-migration-design.md` as the approved contract. Do not start Issue #24.
- Background consumers call `http://127.0.0.1:8768` directly and send `X-Open-Trader-Account-Route: production`. The browser calls Account and Legacy through Gateway port 8766. Gateway does not aggregate.
- Do not add a client class, repository, service locator, cache, retry framework, environment variable, third-party HTTP dependency, dual-read period, local-file fallback, or browser persistence.
- Fetch one Account snapshot per report/revision invocation, statement consumption attempt, or active CLI invocation. Internal Trend Animals retries reuse that mapping. Only a complete statement attempt may restart once after `409 accepted_statement_generation_changed`.
- Account publishes facts; Trend retains action, discipline, sizing, and risk ownership. A bad required Account source cannot create an executable action or widen a risk limit.
- Keep Futu simulation-account adapters, internal Premarket/T-signal implementations, their historical artifacts, Account owner readers, acceptance/parity readers, and explicitly offline tools. Remove only the production entrypoints and active non-owner Account reads named by the spec.
- Legacy `/api/dashboard` must not read or return Account-owned state. Browser enrichment joins only exact `instrument_id`; Account rows use `position_id`. No symbol/name/array-order fallback.
- Use focused tests and direct checks while developing. Run `make acceptance` once, as the final Dashboard gate. Only `PASS` permits review handoff.
- Before asking for review, deploy the exact accepted SHA and verify new PID, cwd, SHA, fresh logs, and HTTP 200 at `http://127.0.0.1:8766/`.
- Do not capture screenshots or send Premarket/T-signal notifications; neither was requested.
- Update and commit the dated operator-facing `CHANGELOG.md` before any merge into `main`.

## File Map

- Add `src/open_trader/account_http.py`: two stdlib Account read functions, fixed production default, finite timeout, envelope checks, and sanitized stable errors.
- Add `tests/test_account_http.py`: helper marker, timeout, HTTP/transport error, JSON, schema, and generation coverage.
- Modify `src/open_trader/account_api.py` and `tests/test_account_api.py`: immutable accepted statement-facts GET route.
- Modify `src/open_trader/trend_statement_consumer.py` and `tests/test_trend_statement_consumer.py`: HTTP-only statement consumption and one bounded generation-conflict restart.
- Modify `src/open_trader/cli.py` and `tests/test_account_sync_cli.py`: HTTP-backed `account-sync-status`; remove unused production command entrypoints.
- Modify `src/open_trader/a_share_trend.py`, `src/open_trader/market_trend.py`, and `src/open_trader/trend_market_controller.py`: one pinned snapshot per report attempt and pure Account-response projection.
- Delete `src/open_trader/broker_details.py` and `tests/test_broker_details.py`; remove test-only raw loaders from `a_share_trend.py` and `market_trend.py`.
- Modify `tests/test_real_holding_input.py`, `tests/test_a_share_trend.py`, `tests/test_market_trend.py`, and `tests/test_trend_market_controller.py`: pinned generation, pure projection, fixed retry input, and per-instrument blocking.
- Modify `tests/test_premarket_cli.py`, `tests/test_futu_watch_cli.py`, `tests/test_trend_market_cli.py`, and existing launchd scripts only where a focused absence assertion finds a real gap.
- Modify `src/open_trader/dashboard.py`, `src/open_trader/dashboard_web.py`, `src/open_trader/dashboard_static/dashboard.js`, `tests/test_dashboard.py`, and `tests/test_dashboard_web.py`: non-Account Legacy response, frozen Trend views, exact-ID browser enrichment, and browser-composed Backtest holdings.
- Add `tests/test_account_production_readers.py`: deterministic raw-reader boundary scan.
- Modify `src/open_trader/dashboard_acceptance.py`, `tests/test_dashboard_acceptance.py`, `docs/operations/account-api-production-cutover.md`, and `CHANGELOG.md`: final proof and operator handoff.

---

### Task 1: Add the Two Stdlib Account Read Functions

**Files:**

- Add: `src/open_trader/account_http.py`
- Add: `tests/test_account_http.py`
- Modify: `src/open_trader/account_api.py:20-35` (export the existing route-marker constants for reuse)
- Modify: `tests/test_account_api.py:425-500` (rename constant references only if needed)

**Interfaces:**

`DEFAULT_ACCOUNT_API_URL` is `http://127.0.0.1:8768` and
`DEFAULT_ACCOUNT_TIMEOUT_SECONDS` is `5.0`. The public call signatures are
`fetch_account_snapshot(base_url=DEFAULT_ACCOUNT_API_URL,
timeout_seconds=DEFAULT_ACCOUNT_TIMEOUT_SECONDS) -> dict[str, object]` and
`fetch_statement_trade_facts(base_url, broker, statement_generation,
timeout_seconds) -> dict[str, object]`.

The exception carries one sanitized machine `code`; it never retains response bodies, paths, credentials, or raw transport text. Keep one private `_get_json` function rather than a client class.

- [ ] **Step 1: Add failing helper contract tests**

  Cover exactly:

  - both requests send `X-Open-Trader-Account-Route: production`;
  - the supplied finite timeout reaches `urllib.request.urlopen`;
  - snapshot accepts `200 healthy` and `200 stale` only when the bounded v1 envelope and SHA generations are valid;
  - statement facts accepts only the exact v1 envelope, matching broker/generation, canonical hashes, offset-aware cutoff, and a facts list;
  - `409` preserves only `accepted_statement_generation_changed` for workflow control;
  - `503`, other HTTP statuses, timeout/connection errors, invalid JSON, invalid schema, and invalid generation become sanitized `AccountHttpError` codes;
  - neither function retries or reads a local path.

  Use a local `ThreadingHTTPServer` fixture or a monkeypatched `urlopen`; do not add an HTTP mocking dependency.

- [ ] **Step 2: Run the focused tests and confirm the red state**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_account_http.py -q
  ```

  Expected: FAIL because `open_trader.account_http` does not exist.

- [ ] **Step 3: Export the existing marker names and add the minimum helper**

  Rename the existing private constants in `account_api.py` to public contract names, then import them from `account_http.py`:

  ```python
  ACCOUNT_ROUTE_HEADER = "X-Open-Trader-Account-Route"
  PRODUCTION_ROUTE_MARKER = "production"
  ```

  Implement a single bounded GET path:

  ```python
  def _get_json(url: str, timeout_seconds: float) -> dict[str, object]:
      if timeout_seconds <= 0:
          raise ValueError("timeout_seconds must be positive")
      request = urllib.request.Request(
          url,
          headers={ACCOUNT_ROUTE_HEADER: PRODUCTION_ROUTE_MARKER},
      )
      try:
          with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
              payload = json.load(response)
      except urllib.error.HTTPError as error:
          code = _safe_http_error_code(error)
          raise AccountHttpError(code) from None
      except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
          raise AccountHttpError("account_unavailable") from None
      if not isinstance(payload, dict):
          raise AccountHttpError("account_contract_invalid")
      return payload
  ```

  Validate only the published envelopes needed by consumers. Do not duplicate Account valuation/business rules or add ETag caching.

- [ ] **Step 4: Run focused helper and existing Account API tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_account_http.py tests/test_account_api.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the helper**

  ```bash
  git add src/open_trader/account_http.py src/open_trader/account_api.py \
    tests/test_account_http.py tests/test_account_api.py
  git commit -m "feat: add Account HTTP read helpers (#23)"
  ```

---

### Task 2: Expose Only the Currently Accepted Statement Facts

**Files:**

- Modify: `src/open_trader/account_api.py:90-155`
- Modify: `tests/test_account_api.py:425-610`
- Reuse unchanged: `src/open_trader/statement_import.py:271-330`

**Interface:**

`GET /api/v1/account/statements/{broker}/{statement_generation}/trade-facts` returns the exact `open_trader.account.statement_trade_facts.v1` response from the approved spec. It uses the current Account snapshot only to prove accepted generation, then reuses `load_statement_trade_facts` for immutable publication/hash validation.

- [ ] **Step 1: Add failing route tests**

  Add server-level tests proving:

  - production mode plus the production marker returns the six public metadata fields and `facts`;
  - shadow mode rejects the production marker with `503 account_api_shadow_only`;
  - unsupported broker and malformed generation return `400`;
  - a valid generation different from the current accepted generation returns `409 accepted_statement_generation_changed` without reading that generation;
  - an accepted generation with missing, mutated, or hash-invalid facts returns `503 statement_facts_publication_invalid`;
  - the response contains no PDF bytes, candidate directory, manifest path, absolute path, password, or parser detail;
  - the existing snapshot ETag and statement upload behavior remain unchanged.

- [ ] **Step 2: Run the route tests and confirm failure**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_account_api.py \
      -k 'statement_trade_facts or production_accepts_marker or statement_command' -q
  ```

  Expected: FAIL with 404 for the new GET route.

- [ ] **Step 3: Add one route branch before the generic 404**

  Parse exactly three suffix components (`broker`, `generation`, `trade-facts`); validate broker with `STATEMENT_BROKERS` and generation with the existing `statement_generation_digest`. Load the current snapshot once, compare its accepted generation, and only then load facts:

  ```python
  manifest, facts = load_statement_trade_facts(data_dir, broker, generation)
  payload = {
      "schema_version": "open_trader.account.statement_trade_facts.v1",
      "broker": broker,
      "statement_generation": generation,
      "statement_period": manifest["statement_period"],
      "trade_facts_cutoff_at": manifest["trade_facts_cutoff_at"],
      "trade_facts_sha256": manifest["trade_facts_sha256"],
      "facts": facts,
  }
  ```

  Return the approved frozen error envelopes. Do not expose the manifest or add an older-generation fallback.

- [ ] **Step 4: Run Account API and statement-import tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_account_api.py tests/test_statement_import.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the endpoint**

  ```bash
  git add src/open_trader/account_api.py tests/test_account_api.py
  git commit -m "feat: expose accepted statement facts (#23)"
  ```

---

### Task 3: Migrate Trend Statement Consumption to HTTP

**Files:**

- Modify: `src/open_trader/trend_statement_consumer.py`
- Modify: `tests/test_trend_statement_consumer.py`
- Verify unchanged caller: `src/open_trader/trend_market_controller.py:2895-2920`

**Interface:**

The public signature becomes
`consume_accepted_statement_facts(*, data_dir: Path, reports_dir: Path,
broker: str, generated_at: str | None = None,
account_url: str = DEFAULT_ACCOUNT_API_URL) -> dict[str, object]`.

The local `data_dir` remains only for the Trend lock, Trend consumption status, and Trend-owned statistics. It is never used to read Account snapshot or statement generations/facts.

- [ ] **Step 1: Replace raw-publication fixtures with failing HTTP seam tests**

  Monkeypatch `fetch_account_snapshot` and `fetch_statement_trade_facts`. Prove:

  - one snapshot and one facts call on success;
  - the facts call uses the accepted generation from that snapshot;
  - `snapshot_generation`, `account_generation`, and `statement_generation` appear in consumed, waiting, failed/blocked, and already-consumed results whenever obtained;
  - one first-call `accepted_statement_generation_changed` discards the attempt, fetches one new snapshot, and succeeds with only the second generation;
  - a second conflict returns `status="blocked"` and `reason="accepted_statement_generation_changed"` after exactly two snapshot/facts calls;
  - transport, timeout, invalid contract, and `503` return a sanitized blocked result after exactly one call;
  - no abandoned attempt writes Trend API statistics or a consumed status;
  - no test stages a raw statement merely to exercise this consumer.

- [ ] **Step 2: Run the focused consumer tests and confirm the red state**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_trend_statement_consumer.py -q
  ```

  Expected: FAIL because the consumer still calls `load_account_snapshot` and `load_statement_trade_facts` locally.

- [ ] **Step 3: Replace the raw reads with a two-attempt loop**

  Keep the existing lock and atomic status writer. The only retry branch is explicit:

  ```python
  for attempt_index in range(2):
      try:
          snapshot = fetch_account_snapshot(
              account_url, DEFAULT_ACCOUNT_TIMEOUT_SECONDS
          )
      except AccountHttpError as error:
          return _blocked_status({}, broker, "", error.code, generated_at)
      generation = str(snapshot["accepted_statement_generation"].get(broker) or "")
      if not generation:
          return _waiting_for_promotion_status(snapshot, broker)
      try:
          facts_payload = fetch_statement_trade_facts(
              account_url, broker, generation, DEFAULT_ACCOUNT_TIMEOUT_SECONDS
          )
      except AccountHttpError as error:
          if error.code == "accepted_statement_generation_changed" and attempt_index == 0:
              continue
          return _blocked_status(snapshot, broker, generation, error.code, generated_at)
      return _consume_facts_payload(
          snapshot=snapshot,
          facts_payload=facts_payload,
          data_dir=data_dir,
          reports_dir=reports_dir,
          broker=broker,
          generated_at=generated_at,
      )
  ```

  Keep cutoff, deduplication, statistics calculation, and atomic writes in this module. Delete imports of `load_account_snapshot`, `load_worker_git_sha`, and `load_statement_trade_facts`.

- [ ] **Step 4: Run consumer and controller tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_trend_statement_consumer.py \
      tests/test_trend_market_controller.py -q
  ```

  Expected: PASS; controller uses the fixed 8768 default without a new config field.

- [ ] **Step 5: Commit the statement consumer migration**

  ```bash
  git add src/open_trader/trend_statement_consumer.py \
    tests/test_trend_statement_consumer.py tests/test_trend_market_controller.py
  git commit -m "refactor: read Trend statement facts from Account API (#23)"
  ```

---

### Task 4: Move `account-sync-status` to the Snapshot Endpoint

**Files:**

- Modify: `src/open_trader/cli.py:25-40, 885-905, 1360-1405`
- Modify: `tests/test_account_sync_cli.py:1-190`

**Interface:**

```text
open-trader account-sync-status [--account-url URL] [--json]
```

The default is the shared `DEFAULT_ACCOUNT_API_URL`. Human output keeps the status, reason, quote status, and per-broker status while adding `snapshot_generation` and `account_generation`. JSON output is a small CLI projection of those same Account snapshot facts; it no longer exposes Worker files or constructs Account clients.

- [ ] **Step 1: Add failing parser and command tests**

  Replace the file-seeding test with a monkeypatched `fetch_account_snapshot`. Prove:

  - parser default is port 8768 and `--account-url` overrides it;
  - `--data-dir` is rejected for `account-sync-status`;
  - one command invocation makes one snapshot call with the shared finite timeout;
  - healthy and stale JSON output include both generations, quote status, broker statuses, and the Account reason;
  - human output labels stale truthfully;
  - `AccountHttpError` exits non-zero with only its sanitized code;
  - no Futu/Tiger client, Account publication loader, or local JSON reader is called.

- [ ] **Step 2: Run the focused CLI tests and confirm failure**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_account_sync_cli.py -q
  ```

  Expected: FAIL because the parser still accepts `--data-dir` and the command reads three local publications.

- [ ] **Step 3: Replace the command branch with one HTTP call**

  Add `--account-url` with the shared default. Keep one small `_account_status_projection(snapshot)` function in `cli.py`; do not add a status class or new config object. Remove `project_account_sync_health` from this active CLI path and delete unused imports after `rg` confirms no other caller in `cli.py`.

- [ ] **Step 4: Run CLI tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_account_sync_cli.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the runtime CLI migration**

  ```bash
  git add src/open_trader/cli.py tests/test_account_sync_cli.py
  git commit -m "refactor: read Account status over HTTP (#23)"
  ```

---

### Task 5: Pin One Account Snapshot Through Each Trend Report Invocation

**Files:**

- Modify: `src/open_trader/a_share_trend.py:920-1000, 1683-1975, 2103-2250, 4520-4675, 4769-5360, 6663-6815, 7650-7985, 8218-8335`
- Modify: `src/open_trader/market_trend.py:1-80, 874-1210, 1490-1585`
- Modify: `src/open_trader/trend_market_controller.py:450-500` only if new-report validation needs an assertion at the controller seam
- Delete: `src/open_trader/broker_details.py`
- Delete: `tests/test_broker_details.py`
- Modify: `tests/test_real_holding_input.py`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `tests/test_trend_market_controller.py`
- Remove obsolete test imports/cases from any test found by `rg 'load_(market_account|trend_account|eastmoney_account)|broker_details' tests src/open_trader`

**Interfaces:**

The projection signature becomes
`load_real_holding_input(account_snapshot: Mapping[str, object], market: str,
*, state_path: Path) -> RealHoldingInput`.

`run_a_share_trend_report` and `run_market_trend_report` fetch once before their internal wait/retry loops, then pass the same mapping into `_attempt_report` / `_attempt_market_report`. New reports freeze:

```json
"account_input": {
  "snapshot_generation": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "account_generation": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "status": "healthy"
}
```

Historical reports without `account_input` remain readable. Every newly generated report must contain it.

- [ ] **Step 1: Rewrite real-holding tests around a snapshot mapping**

  Replace run-directory CSV fixtures in `tests/test_real_holding_input.py` with bounded Account response mappings. Cover:

  - CN selects Eastmoney, HK Phillips, and US Tiger positions and matching account currency cash;
  - positions use current Account values and retain exact `instrument_id` internally;
  - invalid quantity/value/currency remains fail closed;
  - a stale/unavailable contributing broker or required quote source marks each affected `instrument_id` blocked;
  - an unaffected instrument remains available and can continue to a read-only decision;
  - every row sharing one `instrument_id` receives the same block decision, so no partial action leaks through;
  - no function scans `data/runs`, `extracted_positions.csv`, or `extracted_cash.csv`.

  Extend `RealHoldingInput` only with the minimum identity/blocking state needed by the existing evaluator, for example:

  ```python
  instrument_ids_by_symbol: Mapping[str, str] = field(default_factory=dict)
  blocked_instrument_ids: Mapping[str, str] = field(default_factory=dict)
  ```

  Do not add a new real-account model hierarchy.

- [ ] **Step 2: Add failing report pinning tests**

  In A-share and HK/US report tests, use a fetch spy and an attempt spy to prove:

  - one invocation fetches exactly one snapshot;
  - every internal Trend Animals retry receives the identical mapping object;
  - CN/HK/US generated JSON freezes both generations and Account status;
  - Account fetch failure creates no executable report/action and is visible to the controller as a blocker;
  - per-instrument Account blocking produces `MANUAL_REVIEW` with an Account-source reason only for affected real instruments;
  - real rotation selection excludes blocked instruments without treating their occupied slots as free;
  - healthy instruments and the entire simulated-account path continue unchanged;
  - `valid_frozen_report_contract` accepts historical reports without `account_input`, validates it whenever present, and new-report construction tests require it.

- [ ] **Step 3: Run the focused Trend tests and confirm the red state**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_real_holding_input.py \
      tests/test_a_share_trend.py \
      tests/test_market_trend.py \
      tests/test_trend_market_controller.py \
      -k 'real_holding or account_input or snapshot or retry or rotation' -q
  ```

  Expected: FAIL because the current projection scans raw run CSVs and each report attempt has no pinned HTTP input.

- [ ] **Step 4: Convert the real-holding loader into a pure projection**

  Filter the Account response by the existing market-to-broker map. Use `sources.account.brokers` and `sources.quotes` only as input facts; keep the existing Trend cash, supported-asset, quantity, protection-state, enrichment, and read-only decision rules.

  Reuse the existing per-symbol manual-review path. Add one exact blocked-symbol input to `_evaluate_holding_positions`, derived from `blocked_instrument_ids`, and one exclusion input to `_plan_account_rotation_pairs`; do not turn a single blocked instrument into a global real-tab failure.

- [ ] **Step 5: Fetch once outside both existing retry loops**

  In `run_market_trend_report`, fetch after validation/lock acquisition and before `_run_market_trend_retry`; inject the mapping into `attempt_dependencies`. In `run_a_share_trend_report`, fetch before its `while True` and add the same mapping to every `_attempt_report` call. Do not fetch inside `_attempt_market_report`, `_attempt_report`, per-pool loops, or symbol loops.

  Add `account_input: dict[str, object]` to `TrendReport`, pass it through `build_report`, and serialize it once in `_report_payload`.

- [ ] **Step 6: Delete raw compatibility code**

  After callers move:

  - delete `broker_details.py` and its direct tests;
  - delete test-only `load_market_account`, `load_trend_account`, and `load_eastmoney_account` plus their obsolete tests;
  - remove their imports;
  - run `rg` to prove no caller remains.

  ```bash
  rg -n 'broker_details|load_market_account|load_trend_account|load_eastmoney_account|extracted_(positions|cash)\.csv' \
    src/open_trader tests
  ```

  Expected: only the approved dormant workflows, owner/verification code, fixtures, or no matches; no active Trend report reader.

- [ ] **Step 7: Run the complete affected Trend suite**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_real_holding_input.py \
      tests/test_a_share_trend.py \
      tests/test_market_trend.py \
      tests/test_trend_market_controller.py \
      tests/test_trend_review.py -q
  ```

  Expected: PASS.

- [ ] **Step 8: Commit the pinned Trend input**

  ```bash
  git add -A src/open_trader tests
  git commit -m "refactor: pin Account input for Trend reports (#23)"
  ```

---

### Task 6: Remove Premarket and T-signal Production Entrypoints

**Files:**

- Modify: `src/open_trader/cli.py:1-115, 390-530, 820-890, 1950-2075, 2250-2310`
- Modify: `tests/test_premarket_cli.py`
- Modify: `tests/test_futu_watch_cli.py`
- Modify: `tests/test_trend_market_cli.py`
- Verify: `scripts/install_daily_premarket_launchd.sh:450-480`
- Verify: `scripts/uninstall_daily_premarket_launchd.sh:60-80`

**Boundary:** Remove parser and dispatch branches for `run-premarket`, `run-daily-premarket`, and `watch-t`. Keep `advice/premarket.py`, `daily_premarket.py`, `t_signal.py`, `t_signal_runner.py`, their direct unit tests, and historical Dashboard rendering.

- [ ] **Step 1: Add failing absence tests**

  Change CLI tests to assert all three removed command names fail parser dispatch with exit code 2. Add source/script assertions proving:

  - no parser or `args.command` branch remains for the three names;
  - install/uninstall scripts unload `com.open-trader.premarket`, `.hk`, and `.us`;
  - no current launchd template installs those labels;
  - no T-signal launchd label/template exists.

  Keep direct internal workflow tests; delete only tests whose sole purpose is exercising a removed CLI branch.

- [ ] **Step 2: Run the focused CLI tests and confirm failure**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_premarket_cli.py \
      tests/test_futu_watch_cli.py \
      tests/test_trend_market_cli.py -q
  ```

  Expected: FAIL because all three parsers still exist.

- [ ] **Step 3: Delete parser/dispatch code and only newly unused imports**

  Remove the three parser blocks and command branches. Then identify unused Premarket/T-signal imports with `rg`; do not remove helpers still used by Trend controller or other active CLI commands.

- [ ] **Step 4: Run internal workflow and CLI coverage**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_premarket_cli.py \
      tests/test_daily_premarket.py \
      tests/test_futu_watch_cli.py \
      tests/test_t_signal.py \
      tests/test_t_signal_runner.py \
      tests/test_trend_market_cli.py -q
  ```

  Expected: PASS; internal implementations remain importable, but production CLI entrypoints are absent.

- [ ] **Step 5: Commit the production disablement**

  ```bash
  git add src/open_trader/cli.py tests/test_premarket_cli.py \
    tests/test_futu_watch_cli.py tests/test_trend_market_cli.py
  git commit -m "chore: disable unused Premarket and T-signal commands (#23)"
  ```

---

### Task 7: Compose Backtest Holdings in the Browser

**Files:**

- Modify: `src/open_trader/dashboard_web.py:1116-1205`
- Modify: `src/open_trader/dashboard_static/dashboard.js:424-560`
- Modify: `tests/test_dashboard_web.py:1260-1400, 1600-1665`

**Contract:** Legacy returns strategies, defaults, benchmarks, and a watchlist-only universe. The browser derives the `holdings` choice from the Account snapshot already in memory. The Backtest server validates canonical market, symbol, strategy, dates, and numeric risk inputs, but does not call Account or require current universe membership.

- [ ] **Step 1: Add failing server tests**

  Prove:

  - `/api/backtests/options` reads `latest/watchlist.csv` and does not call `load_dashboard_state`, read portfolio/Account publications, or contact Account API;
  - its `universe` contains `watchlist` and an empty `holdings` list for shape stability;
  - `parse_standard_backtest_request` accepts a valid canonical stock/ETF absent from both holdings and watchlist;
  - unsupported market, invalid symbol, strategy/range/date/numeric errors still return 400;
  - the run path still uses the existing Futu price provider and strategy validator.

- [ ] **Step 2: Add failing browser tests**

  In the existing Node VM harness, seed Account positions and Legacy watchlist options. Prove:

  - “当前持仓” is built from Account stock/ETF positions and deduplicated by canonical market/symbol;
  - “关注列表” remains usable with no Account snapshot;
  - an Account refresh updates the holdings selector without refetching Legacy options;
  - cash and unsupported market/assets do not become Backtest options;
  - Account outage after success keeps visibly frozen rows but disables Account-dependent actions; Backtest watchlist remains usable.

- [ ] **Step 3: Run the focused Backtest tests and confirm failure**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard_web.py \
      -k 'standard_backtest or backtest_options' -q
  ```

  Expected: FAIL because the server still builds holdings from Legacy Account state and rejects symbols outside that universe.

- [ ] **Step 4: Remove server membership ownership and derive browser holdings**

  Build server options directly from the existing watchlist reader and `_build_backtest_universe([], watchlist_rows)`. Remove the `build_standard_backtest_options_payload` call and `allowed` set from `parse_standard_backtest_request`; retain `normalize_backtest_symbol` and `validate_standard_backtest_request`.

  Add one browser function that computes Account Backtest rows at render time:

  ```javascript
  function accountBacktestUniverse() {
    const seen = new Set();
    return (state.accountSnapshot?.positions || []).flatMap((position) => {
      const market = String(position.market || "").toUpperCase();
      const symbol = String(position.symbol || "").toUpperCase();
      const asset = String(position.asset_class || "").toLowerCase();
      const key = `${market}:${symbol}`;
      if (!["CN", "HK", "US"].includes(market)
          || !["stock", "etf"].includes(asset)
          || !symbol || seen.has(key)) return [];
      seen.add(key);
      return [{market, symbol, name: String(position.name || "")}];
    });
  }
  ```

  `renderStandardBacktest` uses this function only for source `holdings`; it does not mutate or persist the Account snapshot.

- [ ] **Step 5: Run Backtest and browser tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard_web.py \
      -k 'standard_backtest or backtest_options or account_snapshot' -q
  ```

  Expected: PASS.

- [ ] **Step 6: Commit browser-owned Backtest composition**

  ```bash
  git add src/open_trader/dashboard_web.py \
    src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
  git commit -m "refactor: compose Backtest holdings in the browser (#23)"
  ```

---

### Task 8: Remove Account Ownership from Legacy Dashboard

**Files:**

- Modify: `src/open_trader/dashboard.py:1-50, 212-430, 792-835, 927-985, 2020-2580, 3170-3245, 3845-3915`
- Modify: `src/open_trader/dashboard_web.py:1235-1260, 1405-1430`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8945-8985`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py:5600-5620, 6480-6740, 13240-13340, 13800-13860`

**Legacy response fields after cutover:**

```python
{
    "trade_actions": [],
    "holding_enrichment": [],
    "kelly_lab": {},
    "backtest_universe": {"holdings": [], "watchlist": []},
    "trend_reports": {},
    "trend_reviews": {},
    "trend_controllers": {},
    # existing non-Account Research/Prediction fields remain unchanged
}
```

The exact output may retain existing non-Account configuration metadata, but it must not retain `portfolio_path`, Account summary/broker/cash/position/source/controller/sync fields, Account-rooted `holdings`, or quotes.

- [ ] **Step 1: Add failing Legacy ownership tests**

  Seed Account publications with sentinel values and non-Account module artifacts with different sentinel values. Assert:

  - `load_dashboard_state` and `/api/dashboard` do not open `account_sync_state.json`, `portfolio.csv`, `quotes.json`, `account_sync/controller_status.json`, statement generations, or extracted broker CSVs;
  - the removed Account field names are absent from the response;
  - `/api/quotes` returns 404 and `build_quotes_payload` no longer exists;
  - current and historical Trend report endpoints render the report's frozen `real_holdings`/Account-related audit content without current Account overlays;
  - Trend, Research, Prediction, Kelly, Backtest watchlist, and controller projections remain available;
  - module enrichment is built only from non-Account artifacts across fixed markets CN/HK/US, carries deterministic `instrument_id`, and omits rows whose identity cannot be built exactly;
  - module enrichment never invents a row from Account portfolio membership.

- [ ] **Step 2: Add failing browser composition tests**

  Update the existing exact-ID harness to use `state.dashboard.holding_enrichment`. Prove:

  - `position_id` is the row key;
  - exactly one equal `instrument_id` enriches an Account position;
  - zero or multiple matches leave the Account row visible with `enrichment_status="unavailable"`;
  - no symbol, name, array index, or opaque-ID parsing fallback occurs;
  - Legacy failure does not erase a valid Account snapshot; Account failure does not hide already loaded Trend/Research/Prediction views;
  - a fresh page with no successful snapshot shows Account unavailable, while a current page retains only its in-memory frozen snapshot and disables actions.

- [ ] **Step 3: Run focused Legacy/browser tests and confirm failure**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_dashboard.py tests/test_dashboard_web.py \
      -k 'account or holdings or enrichment or quotes or historical_trend or outage' -q
  ```

  Expected: FAIL because `load_dashboard_state` still starts from Account state, the browser still reads `dashboard.holdings`, and `/api/quotes` still exists.

- [ ] **Step 4: Build enrichment from existing module artifacts**

  Replace `holding_rows` with a stable union of rows already returned by the existing non-Account artifact readers for `{"CN", "HK", "US"}`. Reuse `_latest_by_market_symbol`, `_merge_holding`, and `build_instrument_id`; remove `positions_by_holding` and the broker-detail branch from `_merge_holding`. Rename the result to `holding_enrichment`.

  Do not add a new enrichment class, registry, or fuzzy identity resolver. Require a supported market, non-empty canonical symbol, and a resolved stock/ETF asset class before calling `build_instrument_id`; skip any module row that lacks those exact identity inputs.

- [ ] **Step 5: Delete Legacy Account reads, fields, and report overlays**

  Simplify `DashboardState` and `to_dict`. Remove:

  - `load_account_sync_state`, Account dashboard projection, accepted portfolio fallback, published quotes, controller health, summary/broker/cash/position projections;
  - `broker_positions`/`cash_details` arguments and fallback reads in `_load_trend_reports`, `_load_broker_trend_report`, `_project_broker_trend_report`, and `load_historical_trend_report`;
  - Account overlay helper code with no remaining caller;
  - `build_quotes_payload` and the `/api/quotes` branch.

  Leave `FrontendGateway` generic `/api/*` proxy behavior unchanged: a Gateway `/api/quotes` request naturally forwards Legacy's 404.

- [ ] **Step 6: Switch the browser to the exact enrichment field**

  Change `getHoldings` and `accountHoldingGroups` to read only `state.dashboard.holding_enrichment`. Keep the existing exact `instrument_id` match and `position_id` key. Do not retain a fallback to `dashboard.holdings`.

- [ ] **Step 7: Run the complete Dashboard unit/browser suite**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_dashboard.py \
      tests/test_dashboard_web.py \
      tests/test_frontend_gateway.py -q
  ```

  Expected: PASS. The Gateway routing test may still show `/api/quotes` as a Legacy-routed path; endpoint-removal proof belongs to the Legacy server test.

- [ ] **Step 8: Commit the atomic Legacy/browser cutover**

  ```bash
  git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py \
    src/open_trader/dashboard_static/dashboard.js \
    tests/test_dashboard.py tests/test_dashboard_web.py tests/test_frontend_gateway.py
  git commit -m "refactor: remove Account data from Legacy Dashboard (#23)"
  ```

---

### Task 9: Enforce the Production Raw-reader Boundary

**Files:**

- Add: `tests/test_account_production_readers.py`
- Modify only if the scan finds a missed active reader: the exact offending production file

**Audit shape:** Use a small source scan, not AST/call-graph tooling. Owner and acceptance modules may be excluded by exact path. Dormant Premarket/T-signal exceptions must be exact `(path, matched pattern)` entries, not whole-directory ignores.

- [ ] **Step 1: Add the failing inventory test**

  Scan `src/open_trader/**/*.py` for at least:

  ```python
  FORBIDDEN_PATTERNS = (
      r"account_sync_state\.json",
      r"latest[\"' /]+portfolio\.csv",
      r"latest[\"' /]+quotes\.json",
      r"account_sync[\"' /]+controller_status\.json",
      r"extracted_(?:positions|cash)\.csv",
      r"account_statements[\"' /]+generations",
      r"\bload_account_snapshot\b",
      r"\bload_account_sync_state\b",
      r"\bload_statement_trade_facts\b",
      r"\bdashboard_projection_from_state\b",
      r"\baccepted_portfolio_rows\b",
  )
  ```

  Exact owner exclusions:

  - `account_api.py`, `account_snapshot.py`, `account_sync_worker.py`, `account_sync_state.py`, `statement_import.py`;
  - `dashboard_quotes.py` only for Account-owner quote publication;
  - `dashboard_acceptance.py` and Account parity code in `account_api.py`.

  Exact dormant matches may remain only in `daily_premarket.py` and `t_signal_runner.py`. The test failure must print path, line, and pattern for every unexpected reader.

- [ ] **Step 2: Run the audit and inspect the expected failures**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_account_production_readers.py -q
  ```

  Expected on first run: FAIL if any active reader was missed by Tasks 3-8. Do not add it to the allowlist; remove or migrate it.

- [ ] **Step 3: Remove missed active readers and freeze only exact dormant matches**

  For dormant exceptions, assert the precise matched import/read and its count. A new line or new pattern in those modules must fail. Keep tests/fixtures outside the production scan.

- [ ] **Step 4: Run the audit plus all migrated consumer tests**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
      tests/test_account_production_readers.py \
      tests/test_account_http.py \
      tests/test_account_api.py \
      tests/test_account_sync_cli.py \
      tests/test_trend_statement_consumer.py \
      tests/test_real_holding_input.py \
      tests/test_dashboard.py \
      tests/test_dashboard_web.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the boundary test and any missed-reader deletion**

  ```bash
  git add src/open_trader tests/test_account_production_readers.py
  git commit -m "test: enforce Account production reader boundary (#23)"
  ```

---

### Task 10: Prove the Cutover, Update the Operator Log, and Deploy the Accepted SHA

**Files:**

- Modify: `src/open_trader/dashboard_acceptance.py:930-990, 4720-4760`
- Modify: `tests/test_dashboard_acceptance.py:6860-6940`
- Modify: `docs/operations/account-api-production-cutover.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add failing acceptance checks for the new ownership boundary**

  Replace the positive `/api/quotes` probe with checks proving:

  - Account snapshot and accepted statement-facts endpoints respond through their intended routes with the production marker;
  - Legacy `/api/dashboard` lacks all removed Account fields and `/api/quotes` returns 404;
  - the browser makes independent Account and Legacy requests and never requests `/api/quotes`;
  - one real Account/quote refresh updates the Account snapshot;
  - published current CN/HK/US Trend report JSON includes `account_input` generations;
  - removed CLI names are absent;
  - legacy Premarket launchd labels and Premarket/T-signal processes are absent;
  - Trend/Research/Prediction remain reachable when Account is intentionally unavailable during the direct isolation check;
  - no notification or dry-run command is executed.

- [ ] **Step 2: Run acceptance unit tests and confirm the intended red state**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
  ```

  Expected: FAIL until the acceptance probe stops requiring a successful Legacy `/api/quotes` response and gains the new boundary checks.

- [ ] **Step 3: Update the acceptance probe and cutover runbook**

  Reuse existing process metadata, HTTP, browser, and launchd helpers. Do not add screenshot gates. Document the exact deployment order and whole-release rollback:

  1. Account API/Worker;
  2. verify snapshot and statement-facts;
  3. Gateway/Legacy/Trend at the same SHA;
  4. unload/prove absence of disabled paths;
  5. rollback #23 as a whole release if needed; never pair #23 consumers with #22 Account.

- [ ] **Step 4: Run focused acceptance tests and the full automated suite**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q

  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
  ```

  Expected: all tests PASS with the exact totals recorded in the Issue #23 evidence.

- [ ] **Step 5: Run direct isolated workflows without notifications**

  Start the candidate Account Worker/API pair on isolated loopback ports or the approved local production ports, then verify:

  ```bash
  curl -fsS -H 'X-Open-Trader-Account-Route: production' \
    http://127.0.0.1:8768/api/v1/account/snapshot

  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m open_trader account-sync-status --json

  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m open_trader run-premarket --help
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m open_trader run-daily-premarket --help
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
    /Users/ray/projects/open_trader/.venv/bin/python -m open_trader watch-t --help
  ```

  Expected: snapshot/status succeed; each removed command exits through argparse with code 2. Resolve a currently accepted broker generation from the snapshot and request its trade-facts route with the production marker. Run one CN/HK/US report/revision workflow through the existing controller-safe invocation and inspect each JSON `account_input`; do not send notifications.

  Inspect `launchctl list` and process listings. Unload only the exact legacy Premarket labels if present, then prove the three labels and any Premarket/T-signal watcher process are absent. Stop Account temporarily and prove consumers fail closed while already published non-Account module endpoints remain readable, then restore it.

- [ ] **Step 6: Update and commit the dated operator-facing changelog before merge**

  Add a 2026-08-04 entry covering the HTTP consumer migration, statement-facts route, pinned Trend input, disabled workflows, Legacy Account removal, exact-ID browser composition, tests, deployment order, and whole-release rollback.

  ```bash
  git add src/open_trader/dashboard_acceptance.py \
    tests/test_dashboard_acceptance.py \
    docs/operations/account-api-production-cutover.md CHANGELOG.md
  git commit -m "docs: record Account consumer migration (#23)"
  ```

- [ ] **Step 7: Run the one final Dashboard gate**

  Ensure no source/data change occurs after this point, then run:

  ```bash
  make acceptance
  ```

  Expected: `PASS`. On `FAIL`, fix the cause, rerun focused tests, commit, and rerun `make acceptance`. On `BLOCKED`, report the real blocker; do not substitute fixtures, curl, unit tests, or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA and verify review readiness**

  Record `git rev-parse HEAD`, redeploy that exact SHA in dependency order, and verify:

  - Account API/Worker, Gateway, Legacy, and active Trend PID/cwd/SHA;
  - fresh post-restart logs and timestamps;
  - Account API/Worker release match;
  - Gateway health and `HTTP 200` at `http://127.0.0.1:8766/`;
  - statement-facts route and snapshot route;
  - disabled CLI/launchd/process absence;
  - no live Premarket/T-signal notification was sent.

  This exact-SHA restart does not require another acceptance run. Attach the evidence to Issue #23, give the review URL to the operator, and stop. Do not merge or unlock #24 until the operator explicitly accepts the result.
