# Prediction Service Read-Load Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound Prediction Service HTTP resource use and keep the existing production-sized history endpoint responsive under the expected four-tab polling load.

**Architecture:** Replace the unbounded `ThreadingHTTPServer` instance with one small stdlib subclass that reserves one of eight global slots before starting a handler thread and exposes process-local load counters. Keep a one-second, server-local history single-flight cache in that same subclass, using `concurrent.futures.Future` to share one computation among identical admitted requests. Do not change the read model, SQLite layer, route contract, Gateway, Dashboard, launchd, or runtime ownership.

**Tech Stack:** Python 3.12 stdlib (`http.server`, `threading`, `concurrent.futures`, `time`, `sqlite3`), the existing `PredictionRuntime`/read model, and pytest.

## Global Constraints

- Use one fixed global limit of exactly eight admitted requests across health, state, history, and every POST path.
- Reject before handler-thread creation with HTTP 503, `Retry-After: 1`, `Connection: close`, and exactly `{"error":"prediction service busy"}`.
- Release every admitted slot in `finally`, including parse, auth, handler, response-write, and disconnect failures.
- Cache only successful GET history payloads by the validated `(kind, limit, offset)` tuple for exactly one monotonic second.
- One admitted leader computes a missing history key; admitted duplicates wait at most five seconds and reuse its completed value.
- Never cache failures or serve expired history as stale fallback.
- State, health, and every mutation remain uncached and keep their existing response/auth/audit behavior.
- Add only `http_load={limit,active,overload_rejections,history_cache_hits,history_cache_misses}` to successful or unavailable `/healthz` payloads; `active` includes the health request itself.
- Add no dependency, configuration knob, connection pool, long-lived SQLite connection, generic cache framework, route priority, background refresh, Gateway/Dashboard/launchd/release change, or production write.
- Verification may read the production SQLite database only through a read-only backup into a temporary directory. It must never submit an order or mutate the production DB/WAL.

---

### Task 1: Bound the global HTTP admission point

**Files:**
- Modify: `src/open_trader/prediction_service.py:1-24,97-218,405-408`
- Modify: `tests/test_prediction_service.py:1-89,206-328`

**Interfaces:**
- Produces: `_PredictionHTTPServer(ThreadingHTTPServer)`; it remains compatible with callers expecting `ThreadingHTTPServer`.
- Produces: `_PredictionHTTPServer.http_load_snapshot() -> dict[str, int]`.
- Preserves: `create_prediction_server(...) -> ThreadingHTTPServer` and all current route/auth/lifecycle behavior.
- Establishes for Task 2: server-owned synchronization and zero-valued history hit/miss counters.

- [ ] **Step 1: Add a failing eight-slot burst test**

  In `tests/test_prediction_service.py`, start the real server factory and monkeypatch `prediction_state_payload` with a function that owns a tracked temporary-SQLite read context, increments a protected active count, signals when eight calls have entered, waits on a release event, and closes/decrements in `finally`. Send eight state requests with `ThreadPoolExecutor`, then send 40 more requests while the first eight are blocked.

  Assert all of the following literal facts before releasing the leaders:

  ```python
  assert entered.wait(timeout=5)
  assert max_active == 8
  assert server.http_load_snapshot()["active"] == 8
  assert overflow_statuses == [503] * 40
  assert overflow_payloads == [{"error": "prediction service busy"}] * 40
  assert overflow_retry_after == ["1"] * 40
  assert server.http_load_snapshot()["overload_rejections"] == 40
  ```

  After setting the release event, assert the eight admitted calls return 200, `active` becomes zero within five seconds, and the tracking store has zero live read contexts. Close the server and join its serving thread in `finally` even when an assertion fails.

- [ ] **Step 2: Run the focused test and verify RED**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py -k 'global_http_capacity'
  ```

  Expected: FAIL because the current `ThreadingHTTPServer` starts more than eight handlers and has no `http_load_snapshot`.

- [ ] **Step 3: Add a failing mixed-method and health-evidence test**

  Use a production fake runtime. Hold four state GET handlers and four authenticated preview POST handlers at the same time, then prove a ninth GET and a ninth POST both receive the exact busy response without entering state projection, auth, body read, or execution dispatch. Release one admitted call and prove a new POST can enter, so the slot is reusable.

  With no calls blocked, GET `/healthz` and assert:

  ```python
  assert health["http_load"] == {
      "limit": 8,
      "active": 1,
      "overload_rejections": 2,
      "history_cache_hits": 0,
      "history_cache_misses": 0,
  }
  ```

- [ ] **Step 4: Implement the minimal server subclass**

  Add module constants for the fixed limit and busy body. Keep the implementation in `prediction_service.py`; do not add a new module or dependency.

  The subclass must use this division of responsibility:

  ```python
  class _PredictionHTTPServer(ThreadingHTTPServer):
      def process_request(self, request: object, client_address: object) -> None:
          if not self._request_slots.acquire(blocking=False):
              self._record_overload()
              try:
                  request.sendall(self._busy_response)  # type: ignore[attr-defined]
              except OSError:
                  pass
              finally:
                  self.shutdown_request(request)
              return
          self._record_admitted()
          try:
              super().process_request(request, client_address)
          except BaseException:
              self._release_admitted()
              raise

      def process_request_thread(self, request: object, client_address: object) -> None:
          try:
              super().process_request_thread(request, client_address)
          finally:
              self._release_admitted()
  ```

  Build `_busy_response` once from `_BUSY_BODY` so `Content-Length` cannot drift. Protect active/counter reads and writes with one `threading.Lock`; use `threading.BoundedSemaphore(8)` for admission. Replace only the factory's final `ThreadingHTTPServer(...)` construction with `_PredictionHTTPServer(...)`.

  In the health handler, call `http_load_snapshot()` while the current health request still owns its slot and append that mapping to the existing payload.

- [ ] **Step 5: Run Task 1 GREEN and commit**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py -k 'global_http_capacity or mixed_http_capacity or health'
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py tests/test_prediction_api_contract.py
  git diff --check
  ```

  Commit only the two Task 1 files:

  ```bash
  git add src/open_trader/prediction_service.py tests/test_prediction_service.py
  git commit -m "fix: bound prediction service request concurrency"
  ```

### Task 2: Collapse identical history reads

**Files:**
- Modify: `src/open_trader/prediction_service.py:1-24,97-218`
- Modify: `tests/test_prediction_service.py:228-266`

**Interfaces:**
- Consumes: `_PredictionHTTPServer` and `http_load_snapshot()` from Task 1.
- Produces: `_PredictionHTTPServer.history_payload(key: tuple[str, int, int], compute: Callable[[], dict[str, object]]) -> dict[str, object]`.
- Uses: `key: tuple[str, int, int]` after `_query_int` and `kind` validation.
- Preserves: `prediction_history_payload(...)` arguments, filtering, order, pagination, total count, and JSON response shape.

- [ ] **Step 1: Add a failing single-flight and TTL test**

  Monkeypatch the module-level `prediction_history_payload` with a protected counter and controllable result. Start eight identical history requests together and hold the first computation until every client has connected. Assert all eight responses are identical 200 payloads while the underlying function ran once.

  Patch the module monotonic clock to deterministic values and assert the counters at each boundary:

  ```python
  assert calls == 1
  assert load["history_cache_misses"] == 1
  assert load["history_cache_hits"] == 7

  clock[0] = 0.999
  assert get_history() == first_payload
  assert calls == 1

  clock[0] = 1.001
  assert get_history() == second_payload
  assert calls == 2
  ```

  Use distinct `(kind, limit, offset)` tuples once each and assert they do not share entries.

- [ ] **Step 2: Run the single-flight test and verify RED**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py -k 'history_single_flight or history_cache_ttl'
  ```

  Expected: FAIL because each request currently calls `prediction_history_payload` independently and no cache counters advance.

- [ ] **Step 3: Add failing error, timeout, and no-stale tests**

  Cover three separate paths:

  1. The leader raises `sqlite3.OperationalError`; all admitted followers wake and receive `503 {"error":"prediction history unavailable"}`, the key is absent from the success cache, and the next call recomputes successfully.
  2. Patch `_HISTORY_WAIT_SECONDS` to `0.05`; hold the leader longer than that and assert a follower receives the same explicit 503, releases its HTTP slot, and does not cancel or replace the leader.
  3. Populate a success, advance beyond the one-second TTL, then make recomputation fail; assert 503 rather than the expired payload.

  Also retain the existing invalid-query test to prove validation still returns 400 before any cache lookup.

- [ ] **Step 4: Implement server-local single-flight with stdlib `Future`**

  Add no cache class. Store these values directly on `_PredictionHTTPServer`:

  ```python
  self._history_cache: dict[
      tuple[str, int, int], tuple[float, dict[str, object]]
  ] = {}
  self._history_flights: dict[
      tuple[str, int, int], Future[dict[str, object]]
  ] = {}
  ```

  Under the existing server lock, first remove every expired cache entry so unique pagination keys cannot accumulate forever. Then return and count an unexpired success; otherwise join an existing `Future` and count a hit, or install a new `Future` and count one miss. The leader computes outside the lock. On success, store `(time.monotonic() + 1.0, payload)`, remove the flight, and resolve the future. On exception, remove the flight, set the same exception on the future, and do not alter any still-valid success. Followers use `future.result(timeout=5.0)`.

  Parse and validate `kind`, `limit`, and `offset` once in the handler, then call `server.history_payload(key, lambda: prediction_history_payload(...))`. Catch computation exceptions and `FutureTimeoutError` at the history-route boundary and return only:

  ```python
  self._send_json(
      HTTPStatus.SERVICE_UNAVAILABLE,
      {"error": "prediction history unavailable"},
  )
  ```

  Do not pass the cache through Runtime and do not cache state, health, POST, validation errors, or unavailable-runtime responses.

- [ ] **Step 5: Run Task 2 GREEN and commit**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py -k 'history or global_http_capacity or mixed_http_capacity or health'
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py tests/test_prediction_read_model.py \
    tests/test_prediction_api_contract.py tests/test_prediction_shadow_validation.py
  git diff --check
  ```

  Commit only the two Task 2 files:

  ```bash
  git add src/open_trader/prediction_service.py tests/test_prediction_service.py
  git commit -m "fix: collapse duplicate prediction history reads"
  ```

### Task 3: Direct production-sized load proof and final review

**Files:**
- Create temporarily, then delete: `/private/tmp/issue34_prediction_read_load.py`
- Write ignored evidence: `.superpowers/sdd/2026-08-11-prediction-service-read-load/direct-load-report.json`
- Modify only if a check fails: `src/open_trader/prediction_service.py`, `tests/test_prediction_service.py`
- Do not modify: Gateway, Dashboard, launchd, SQLite schema/store, Runtime, read model, strategy, execution, or order code.

**Interfaces:**
- Uses: the real `create_prediction_server` and `PredictionArbitrageStore` against a temporary read-only backup.
- Produces: one JSON report containing SHA, source size/row counts and hashes, HTTP status/latency counts, peak/final resources, load counters, and cleanup evidence.
- Submits: no mutation request and no trading/order call.

- [ ] **Step 1: Build a disposable, fail-closed load harness**

  Create the temporary script with a CLI accepting exactly:

  ```text
  --source-db PATH
  --duration-seconds 1800
  --wave-interval-seconds 1.5
  --report PATH
  ```

  The script must:

  - record SHA-256 and stat data for the source main DB and existing `-wal` before backup;
  - open the source with SQLite URI `mode=ro`, use `Connection.backup()` into a `TemporaryDirectory`, and explicitly close both connections;
  - instantiate `PredictionArbitrageStore` only on the copied data directory;
  - start `create_prediction_server(runtime=fake_production_runtime, port=0)` on loopback, where every mutation/trading method raises `AssertionError`;
  - record baseline `threading.enumerate()`, `/dev/fd`, and `/usr/sbin/lsof` rows for only the copied DB/WAL;
  - every 1.5 seconds issue four state plus four identical `signals&limit=100&offset=0` history GETs concurrently, requiring eight 200s and each latency `<= 5.0` seconds;
  - wrap the real history projection with a controllable delay for one 48-client burst, requiring no more than eight active handlers and every overflow to be the exact busy 503 with `Retry-After: 1`;
  - after every wave, after the burst, and at exit, wait at most five seconds for `active == 0`, then require handler threads, FDs, and copied-DB/WAL handles to return to baseline plus at most two transient descriptors;
  - close the HTTP server and store references in `finally`, join all client/server threads, and write the report atomically even on failure;
  - re-hash the production main DB and WAL and fail if either changed;
  - exit nonzero on any timeout, unexpected status, mutation/trading access, resource-bound breach, hash change, or cleanup residue.

  Report these literal top-level keys so the evidence is reviewable:

  ```python
  {
      "status": "PASS" | "FAIL",
      "git_sha": str,
      "duration_seconds": float,
      "source": {"path": str, "bytes": int, "rows": dict, "hashes": dict},
      "requests": {"by_status": dict, "count": int, "max_latency_seconds": float},
      "resources": {"baseline": dict, "peak": dict, "final": dict},
      "http_load": dict,
      "production_unchanged": bool,
      "trading_calls": int,
      "errors": list,
  }
  ```

- [ ] **Step 2: Run the full 30-minute direct workflow**

  Run from the Issue #34 worktree:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
    /private/tmp/issue34_prediction_read_load.py \
    --source-db /Users/ray/projects/open_trader/data/prediction_arbitrage/prediction_arbitrage.sqlite3 \
    --duration-seconds 1800 \
    --wave-interval-seconds 1.5 \
    --report .superpowers/sdd/2026-08-11-prediction-service-read-load/direct-load-report.json
  ```

  Expected: exit 0 and report `status=PASS`, `trading_calls=0`, `production_unchanged=true`, normal waves all 200 within five seconds, slow-burst active peak no greater than eight, explicit overflow 503s, and final resources back within the specified bounds.

- [ ] **Step 3: Remove the temporary harness and prove no residue**

  Delete only `/private/tmp/issue34_prediction_read_load.py` after preserving the ignored report. Then run:

  ```bash
  test ! -e /private/tmp/issue34_prediction_read_load.py
  /usr/sbin/lsof -nP -iTCP:8769 -sTCP:LISTEN
  pgrep -fal 'issue34_prediction_read_load|pytest.*prediction_service'
  git status --short
  ```

  Expected: the temporary script is absent; `lsof` and `pgrep` show no Issue #34 listener or worker; Git status contains only intentional tracked changes/commits.

- [ ] **Step 4: Run the relevant suites, then the complete repository gate**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py tests/test_prediction_read_model.py \
    tests/test_prediction_api_contract.py tests/test_prediction_shadow_validation.py \
    tests/test_prediction_service_launchd.py
  make test
  git diff --check main...HEAD
  ```

  Expected: all commands pass. Do not run `make acceptance`: this ticket changes the independent 8769 HTTP resource boundary, not Dashboard UI/Gateway routing, and #44 remains responsible for release/cutover.

- [ ] **Step 5: Review the complete diff and stop before merge**

  Run the repository code-review workflow against fixed point `main`, fix every blocking Standards or Spec finding with a fresh RED/GREEN cycle, and rerun Step 4 plus any review-targeted test.

  Report the branch SHA, exact focused/full test output, direct-report path and summary, production hash proof, and no-listener/process proof. Do not add a dependency, update `CHANGELOG.md`, merge, push, deploy, install launchd, route Gateway, or stop Legacy until the user explicitly requests the next action.
