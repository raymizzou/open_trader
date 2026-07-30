# Sole Account and Quote Sync Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one long-running controller the only process that reads Dashboard account/quote APIs and publishes their files, while the Dashboard becomes a read-only projection that reports failures and stale data truthfully.

**Architecture:** Add a file-contract module shared by the controller, Dashboard, premarket flow, and 做T watcher, plus one controller module that owns Futu/Tiger account reads, Futu quote reads, candidate validation, and atomic publication. Accepted broker details live in `account_sync_state.json`; `portfolio.csv` is rebuilt only from those accepted details and accepted statement candidates. The Dashboard never imports or constructs an account/quote client and never scans run directories for account truth.

**Tech Stack:** Python 3.12, standard-library JSON/CSV/`fcntl`, existing Futu and Tiger adapters, existing `DashboardQuoteService`, launchd, vanilla HTML/CSS/JavaScript, pytest, existing Dashboard browser acceptance.

## Global Constraints

- Follow [the approved design](../specs/2026-07-30-account-sync-controller-design.md) and the approved UI mock at `.superpowers/brainstorm/11649-1785393949/content/account-sync-status-ui-mock.html`.
- Only `account-sync-controller` may instantiate `FutuAccountClient`, `TigerAccountClient`, or the Futu Dashboard quote client for this account/quote data path.
- Only the controller may write:
  - `data/latest/account_sync_state.json`
  - `data/latest/portfolio.csv`
  - `data/latest/quotes.json`
  - `data/account_sync/controller_status.json`
- A failed candidate never touches `portfolio.csv`. It updates only that source's failure fields while preserving its accepted positions, cash, summary, period, and last-success timestamp.
- A complete successful snapshot replaces the broker's accepted rows. Position decreases, an empty position list, disappeared symbols, and zero balances are valid outcomes.
- Persistent source states are only `ok`, `failed`, and `unknown`. `stale` is derived while reading:
  - live account stale threshold: 180 seconds;
  - quotes stale threshold: 15 seconds;
  - controller heartbeat stale threshold: 15 seconds.
- `skipped` is a scheduler event only and must never be persisted over a source state.
- Per-source publishing is allowed. If Futu fails and Tiger succeeds, Tiger publishes, Futu retains its accepted data with `failed`, and global health is abnormal.
- Error text written to JSON must be sanitized for account numbers, Tiger secrets/tokens/private-key paths, and home/repository paths.
- Statement upload/import may write validated candidate/run artifacts, but it must never update `data/latest/portfolio.csv`.
- Premarket and 做T may read published files, but they must fail closed for every affected broker whose account state is `failed`, `stale`, or `unknown`.
- Trading controllers retain direct pre-submit account/order/fill verification, but those reads cannot publish Dashboard account/quote files.
- Do not add a database, queue, file-watcher daemon, generic repository layer, or a multi-writer lock.
- Use focused tests and direct checks during development. Run `make acceptance` only once the final committed candidate is ready.

---

### Task 1: Define the accepted-state and health contract

**Files:**

- Create: `src/open_trader/account_sync_state.py`
- Create: `tests/test_account_sync_state.py`

**Interfaces:**

- Produce:

```python
ACCOUNT_STATE_VERSION = 1
REQUIRED_BROKERS = ("futu", "tiger", "phillips", "eastmoney")
LIVE_BROKERS = ("futu", "tiger")
ACCOUNT_STALE_SECONDS = 180
QUOTE_STALE_SECONDS = 15
CONTROLLER_STALE_SECONDS = 15

@dataclass(frozen=True)
class BrokerAccountCandidate:
    broker: str
    source_kind: str
    data_as_of: str
    period: str
    positions: tuple[Position, ...]
    cash: tuple[CashBalance, ...]
    fx_rates: tuple[dict[str, str], ...]
    summary: dict[str, object]

def empty_account_sync_state() -> dict[str, object]: ...
def load_account_sync_state(path: Path) -> dict[str, object]: ...
def accept_candidate(
    state: Mapping[str, object],
    candidate: BrokerAccountCandidate,
    *,
    attempted_at: str,
) -> dict[str, object]: ...
def record_source_failure(
    state: Mapping[str, object],
    broker: str,
    *,
    attempted_at: str,
    message: str,
    sensitive_values: Sequence[str] = (),
    sensitive_roots: Sequence[Path] = (),
) -> dict[str, object]: ...
def effective_source_status(
    source: Mapping[str, object],
    *,
    now: datetime,
) -> str: ...
def project_account_sync_health(
    state: Mapping[str, object],
    controller_status: Mapping[str, object],
    quotes: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]: ...
def accepted_portfolio_rows(state: Mapping[str, object]) -> list[dict[str, str]]: ...
def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None: ...
def write_portfolio_atomic(path: Path, rows: Sequence[Mapping[str, str]]) -> None: ...
```

The persisted source object contains `source_kind`, `status`, `attempted_at`,
`last_success_at`, `data_as_of`, `period`, `message`, complete normalized
`positions`, complete normalized `cash`, `fx_rates`, and `summary`. Position and
cash rows use the existing detail field names and decimal-as-string convention.

- [ ] **Step 1: Write failing state initialization and round-trip tests**

Require a missing or malformed file to return all four sources as `unknown`, with
empty accepted data, and never infer health from `portfolio.csv`.

```python
state = load_account_sync_state(tmp_path / "missing.json")
assert state["version"] == 1
assert set(state["brokers"]) == set(REQUIRED_BROKERS)
assert {item["status"] for item in state["brokers"].values()} == {"unknown"}
```

Round-trip one `Position`, one `CashBalance`, an account-specific USD/HKD rate,
and a summary through `accept_candidate`, `write_json_atomic`, and
`load_account_sync_state`. Assert exact decimal strings and no account ID field.

- [ ] **Step 2: Run the state tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_account_sync_state.py
```

Expected: collection fails because `open_trader.account_sync_state` does not
exist.

- [ ] **Step 3: Implement the minimal state schema and serializers**

Use plain dictionaries at the JSON boundary and the existing `Position`,
`CashBalance`, `Market`, and `AssetClass` types internally. Reject unknown
versions and structurally invalid broker payloads by returning the all-unknown
state; do not partially trust malformed JSON.

`record_source_failure` must copy accepted data unchanged:

```python
failed = deepcopy(state)
source = failed["brokers"][broker]
source["status"] = "failed"
source["attempted_at"] = attempted_at
source["message"] = sanitize_sync_error(message)
failed["generation"] = attempted_at
```

Do not change `last_success_at`, `data_as_of`, `period`, `positions`, `cash`,
`fx_rates`, or `summary`.

- [ ] **Step 4: Write failing freshness and global-health tests**

Cover these exact branches:

```python
assert effective_source_status(live_ok_179_seconds_old, now=now) == "ok"
assert effective_source_status(live_ok_181_seconds_old, now=now) == "stale"
assert effective_source_status(statement_ok_two_months_old, now=now) == "ok"
assert effective_source_status(failed_source, now=now) == "failed"
assert effective_source_status(unknown_source, now=now) == "unknown"
```

Require global `status == "ok"` only when the controller heartbeat is at most
15 seconds old, all four required sources are effectively `ok`, quotes are
effectively current, and `portfolio_generation` is non-empty. Every other
branch returns `status == "abnormal"` plus a stable reason.

- [ ] **Step 5: Implement freshness, controller validation, and safe errors**

Validate `controller_status.json` against:

```python
{
    "schema_version": "open_trader.account_sync.controller.v1",
    "pid": int,
    "started_at": aware_iso_datetime,
    "working_directory": str,
    "git_sha": str,
    "heartbeat_at": aware_iso_datetime,
    "phase": str,
    "account_loop": dict,
    "quote_loop": dict,
    "blocker": str | None,
}
```

Render effective broker entries with `status`, `data_as_of`, `last_success_at`,
`message`, and a Chinese display string. Missing status files must remain
`unknown / 数据未验证`.

Sanitize errors by replacing the `sensitive_values` and `sensitive_roots` passed
by the controller, then masking long digit sequences, Tiger config paths, home
paths, and repository-absolute paths.

- [ ] **Step 6: Implement deterministic portfolio reconstruction**

Deserialize only accepted source rows. Build the projection with the existing
`build_portfolio_rows` and `StaticMonthEndFxProvider`, using accepted source FX
rates first and existing configured fallbacks `HKD=1`, `USD=7.8`, `CNY=1.08`.
After aggregation, apply a live FX rate only to rows whose `brokers` field names
exactly one live broker; mixed-source rows keep the deterministic currency
fallback.

Before building, reject duplicate position identities
`(broker, account_alias, market, asset_class, symbol, currency)` and duplicate
cash identities `(broker, account_alias, currency)`. Do not compare against old
portfolio rows.

- [ ] **Step 7: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_account_sync_state.py
```

Expected: all state, freshness, sanitization, and deterministic projection tests
pass.

Commit:

```bash
git add src/open_trader/account_sync_state.py tests/test_account_sync_state.py
git commit -m "feat: define accepted account sync state"
```

---

### Task 2: Convert broker snapshots into complete candidates

**Files:**

- Modify: `src/open_trader/futu_account.py`
- Modify: `src/open_trader/tiger_account.py`
- Modify: `tests/test_futu_account.py`
- Modify: `tests/test_tiger_account.py`

**Interfaces:**

- Produce:

```python
def build_futu_account_candidate(
    snapshot: FutuAccountSnapshot,
    *,
    run_date: str,
    data_as_of: str,
    fallback_fx_to_hkd: Mapping[str, Decimal],
) -> BrokerAccountCandidate: ...

def build_tiger_account_candidate(
    snapshot: TigerAccountSnapshot,
    *,
    run_date: str,
    data_as_of: str,
) -> BrokerAccountCandidate: ...
```

- Keep the old sync interfaces temporarily so this commit remains runnable while
  CLI and premarket still import them. The new controller must not call them;
  Task 6 deletes them after every production caller has been removed.

- [ ] **Step 1: Write failing candidate-completeness tests**

Port the valuable mapping, account-total reconciliation, masking, asset-class,
cash-currency, and live-FX coverage from the old sync tests to the two candidate
builders. Add explicit tests for:

```python
assert len(candidate.positions) == 14
assert candidate.summary["position_count"] == 14
assert candidate.summary["account_count"] == 1
assert candidate.data_as_of == "2026-07-30T11:56:54+08:00"
```

Require malformed required fields, no real account, duplicate identities, or a
failed account/position/cash query to raise the broker's existing typed error.
Require an empty but complete position list to produce a valid candidate with
`position_count == 0`.

- [ ] **Step 2: Run the focused candidate tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_futu_account.py tests/test_tiger_account.py \
  -k 'candidate or complete_zero or duplicate_identity'
```

Expected: failures because the candidate builders do not exist.

- [ ] **Step 3: Build Futu candidates from the existing mapper**

Reuse `map_snapshot_to_portfolio_inputs`,
`_unmapped_total_asset_positions`, and the current asset-class inference.
Treat every existing `blocking_errors` item as candidate rejection; do not emit
a low-confidence candidate that can later publish.

Build `summary` from the same normalized candidate:

```python
summary = {
    "account_count": len(snapshot.accounts),
    "position_count": len(positions),
    "cash_count": len(cash_balances),
    "account_aliases": sorted({item.account_alias for item in [*positions, *cash_balances]}),
}
```

Do not read an old `portfolio.csv` and do not write any file.

- [ ] **Step 4: Build Tiger candidates from the existing mapper**

Reuse `map_snapshot_to_portfolio_inputs`, `_snapshot_fx_to_hkd`, and
`_unmapped_total_asset_positions`. Serialize the account-specific rates into
`candidate.fx_rates`. Keep account masking in diagnostic serialization and
exclude raw `account` from accepted rows.

Do not merge preserved portfolio rows and do not write any file.

- [ ] **Step 5: Keep existing sync coverage green during migration**

Do not change the old sync behavior in this task. Retain its tests until Task 6
removes the last production callers. Add candidate coverage alongside the
valuable existing tests for:

- broker SDK response completeness;
- normalized positions and cash;
- unmapped account-total reconciliation;
- invalid required fields;
- duplicate/collision normalization;
- secret/account masking.

No code outside the future controller may call a broker candidate builder and
then publish `latest`.

- [ ] **Step 6: Run affected broker suites and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_futu_account.py tests/test_tiger_account.py
```

Expected: both complete suites pass with the new candidate builders and the
temporary compatibility sync interfaces unchanged.

Commit:

```bash
git add src/open_trader/futu_account.py src/open_trader/tiger_account.py \
  tests/test_futu_account.py tests/test_tiger_account.py
git commit -m "feat: build validated broker candidates"
```

---

### Task 3: Accept statement candidates and publish broker generations

**Files:**

- Modify: `src/open_trader/account_sync_state.py`
- Modify: `tests/test_account_sync_state.py`
- Create: `src/open_trader/account_sync_controller.py`
- Create: `tests/test_account_sync_controller.py`
- Delete: `src/open_trader/dashboard_account_sync.py`
- Delete: `tests/test_dashboard_account_sync.py`

**Interfaces:**

- Produce:

```python
def load_latest_statement_candidate(
    data_dir: Path,
    broker: Literal["phillips", "eastmoney"],
) -> BrokerAccountCandidate | None: ...

@dataclass(frozen=True)
class AccountSyncControllerConfig:
    data_dir: Path
    reports_dir: Path
    portfolio_path: Path
    futu_host: str
    futu_port: int
    tiger_config_dir: Path
    tiger_account: str | None
    account_interval_seconds: float = 60.0
    quote_interval_seconds: float = 5.0

class AccountSyncController:
    def sync_accounts_once(self) -> dict[str, object]: ...
    def sync_quotes_once(self) -> dict[str, object]: ...
    def write_heartbeat(self, *, blocker: str | None = None) -> None: ...
```

- [ ] **Step 1: Write failing statement-candidate selection tests**

Seed multiple monthly/daily run directories. Require Phillips selection by
latest statement date and Eastmoney by latest statement month, independent of
directory modification time. Ignore failed/malformed detail files and never
select Futu/Tiger run artifacts as statement sources.

Require statement candidates to carry `source_kind == "statement"`,
`period`, normalized positions/cash, and a non-real-time summary.

- [ ] **Step 2: Implement statement candidate loading**

Read only `extracted_positions.csv`, `extracted_cash.csv`, and the matching
manifest/statement IDs already produced by the import pipeline. Reuse the
shared detail serializers from Task 1. Candidate loading is read-only; accepting
it and publishing still belongs to the controller.

- [ ] **Step 3: Write failing publication tests for the original regression**

Seed accepted Tiger state with 8 positions and a successful 14-position Tiger
candidate. Assert:

```python
controller.sync_accounts_once()
published = load_account_sync_state(data_dir / "latest/account_sync_state.json")
assert published["brokers"]["tiger"]["summary"]["position_count"] == 14
assert len(published["brokers"]["tiger"]["positions"]) == 14
assert set(read_portfolio_symbols(portfolio_path)) >= set(all_14_symbols)
assert stale_removed_symbol not in read_portfolio_symbols(portfolio_path)
```

Spy on atomic writes and require `portfolio.csv` to be replaced before
`account_sync_state.json`. Assert there is no restore write and no
`_assert_preserves_other_brokers` equivalent.

- [ ] **Step 4: Write failing partial-success and persistence tests**

Cover:

1. Futu fetch fails, Tiger succeeds.
2. Futu accepted rows remain byte-for-byte equivalent in state except status
   fields.
3. Tiger accepted rows and `portfolio.csv` update.
4. Global projected health is abnormal.
5. Constructing a new controller from disk preserves the failure.
6. A scheduler skip leaves that failure unchanged.
7. A later Futu success clears only Futu's failure.

Also simulate a candidate-validation failure after writing a diagnostic run
artifact and assert Dashboard-facing accepted state remains old.

- [ ] **Step 5: Implement per-source candidate publication**

For each due source:

```text
fetch -> normalize -> validate -> copy current state -> accept candidate
      -> rebuild whole portfolio from copied state
      -> atomic replace portfolio.csv
      -> atomic replace account_sync_state.json
```

On failure:

```text
copy current state -> record source failed -> atomic replace state only
```

Process Phillips, Eastmoney, Futu, and Tiger independently so one failure does
not prevent later sources from attempting. Write diagnostic candidates under:

```text
data/account_sync/runs/<filesystem-safe-generation>/<broker>.json
```

Those artifacts are never read by the Dashboard.

- [ ] **Step 6: Remove the Dashboard-owned service**

Delete `DashboardAccountSyncService`, its preservation guard, rollback helper,
thread lock, and tests. Do not replace them with another Dashboard-side wrapper.

- [ ] **Step 7: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_account_sync_state.py tests/test_account_sync_controller.py
```

Expected: the 8-to-14 regression, removal of disappeared holdings, partial
success, persistence, and no-rollback assertions all pass.

Commit:

```bash
git add src/open_trader/account_sync_state.py \
  src/open_trader/account_sync_controller.py \
  tests/test_account_sync_state.py tests/test_account_sync_controller.py
git rm src/open_trader/dashboard_account_sync.py tests/test_dashboard_account_sync.py
git commit -m "feat: publish accepted account generations"
```

---

### Task 4: Add quote publication, scheduling, heartbeat, and singleton lock

**Files:**

- Modify: `src/open_trader/account_sync_controller.py`
- Modify: `src/open_trader/dashboard_quotes.py`
- Modify: `tests/test_account_sync_controller.py`
- Modify: `tests/test_dashboard_quotes.py`

**Interfaces:**

- Produce:

```python
def run_account_sync_controller(
    config: AccountSyncControllerConfig,
    *,
    once: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int: ...

def load_published_quotes(
    path: Path,
    *,
    now: datetime,
) -> dict[str, object]: ...
```

- [ ] **Step 1: Write failing cadence tests**

With a fake monotonic clock, require immediate startup work followed by quote
cycles at 5 seconds and account cycles at 60 seconds:

```python
assert events[:3] == ["account", "quote", "heartbeat"]
assert account_attempts == [0.0, 60.0, 120.0]
assert quote_attempts == list(range(0, 121, 5))
```

Require account failures not to mutate quote state and quote failures not to
mutate account state.

- [ ] **Step 2: Write failing persisted-quote tests**

Seed `quotes.json` with a last successful payload, restart the quote service,
force the next Futu quote fetch to fail, and require:

```python
assert payload["status"] == "failed"
assert payload["last_success_at"] == previous_success
assert payload["quotes"] == stale_marked_previous_quotes
```

Require `load_published_quotes` to return `unknown` for a missing/malformed file
and derive stale after 15 seconds even if the stored status says `ok`.

- [ ] **Step 3: Seed and publish the existing quote service**

Keep the existing quote normalization/session logic. Initialize
`DashboardQuoteService.last_success_at` and `.last_quotes` from the accepted
`quotes.json`; call `.refresh()` only inside the controller and atomically write
its `to_dict()` result.

The controller writes a quote failure payload that retains accepted quotes and
records the current attempt error. No Dashboard request may call `.refresh()`.

- [ ] **Step 4: Write failing heartbeat and singleton tests**

Require the controller status schema from Task 1, fixed process metadata
(captured once at startup), fresh heartbeat every loop, and independent
`account_loop`/`quote_loop` results.

Hold `data/account_sync/controller.lock` in one process/context and assert a
second `run_account_sync_controller(..., once=True)` returns non-zero without
performing account or quote reads. The error text must contain
`已有同步控制器运行`.

- [ ] **Step 5: Implement the loop and process-lifetime `fcntl` lock**

Acquire `LOCK_EX | LOCK_NB` once before any external read and hold the open file
handle until process exit. The lock is not imported or used by Dashboard,
statement import, or broker modules.

Capture PID, working directory, Git SHA, and start time once. Refresh only
heartbeat and loop result fields. Sleep only until the nearest due cadence; do
not create worker threads.

- [ ] **Step 6: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_account_sync_controller.py tests/test_dashboard_quotes.py
```

Expected: cadence, restart fallback, stale derivation, heartbeat, and singleton
tests pass.

Commit:

```bash
git add src/open_trader/account_sync_controller.py \
  src/open_trader/dashboard_quotes.py \
  tests/test_account_sync_controller.py tests/test_dashboard_quotes.py
git commit -m "feat: run sole account and quote controller"
```

---

### Task 5: Expose only controller and file-status CLIs, then install with launchd

**Files:**

- Modify: `src/open_trader/cli.py`
- Create: `tests/test_account_sync_cli.py`
- Delete: `tests/test_futu_account_cli.py`
- Delete: `tests/test_tiger_account_cli.py`
- Modify: `tests/test_futu_watch_cli.py`
- Create: `ops/launchd/com.open-trader.account-sync-controller.plist.template`
- Create: `scripts/install_account_sync_launchd.sh`
- Create: `scripts/uninstall_account_sync_launchd.sh`
- Create: `tests/test_account_sync_launchd.py`

**CLI contract:**

```text
open-trader account-sync-controller
  --config config/daily_premarket.env
  --data-dir data
  --reports-dir reports
  --portfolio data/latest/portfolio.csv
  --tiger-config-dir ~/.tigeropen/
  --account-interval-seconds 60
  --quote-interval-seconds 5
  [--once]

open-trader account-sync-status --data-dir data [--json]
```

- [ ] **Step 1: Write failing CLI ownership tests**

Require the parser to expose the two commands above. Require these old commands
to be rejected as unknown:

- `check-futu-quotes`
- `check-futu-account`
- `sync-futu-portfolio`
- `check-tiger-account`
- `sync-tiger-portfolio`

Patch all broker/quote client constructors to fail in the
`account-sync-status` test; require the command to print only the accepted file
health, controller PID/SHA/heartbeat, and per-source status.

- [ ] **Step 2: Implement the CLI and remove old direct-read branches**

Load only `OPEN_TRADER_FUTU_HOST` and `OPEN_TRADER_FUTU_PORT` from the existing
env file for controller config; do not require unrelated LLM/notifier settings.
Wire `--once` to one account cycle, one quote cycle, one final heartbeat, and
exit.

Remove old parser definitions, main branches, direct-client imports, and
result-print helpers. Keep broker clients importable by the controller module,
not the general CLI.

- [ ] **Step 3: Write failing launchd rendering and lifecycle tests**

Require:

```python
assert plist["Label"] == "com.open-trader.account-sync-controller"
assert plist["RunAtLoad"] is True
assert plist["KeepAlive"] is True
assert plist["ProgramArguments"] contains ["-m", "open_trader", "account-sync-controller"]
assert plist["WorkingDirectory"] == "OPEN_TRADER_REPO"
```

The dry run must render runtime-root data/config paths, logs under
`logs/account_sync/`, and no Tiger secret/private-key/token content.

The installer must retry transient `launchctl bootstrap`, kickstart the job,
then wait for a valid fresh `controller_status.json` whose working directory
and Git SHA match the deployed repository. The uninstaller targets only this
label and refuses to delete a still-loaded plist.

- [ ] **Step 4: Implement template and scripts**

Follow the existing Dashboard launchd script conventions. Use:

```text
logs/account_sync/launchd.out.log
logs/account_sync/launchd.err.log
```

Do not install the controller inside the Dashboard job and do not add a manual
HTTP trigger.

- [ ] **Step 5: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_account_sync_cli.py tests/test_account_sync_launchd.py \
  tests/test_futu_watch_cli.py
```

Expected: only the controller command can instantiate live clients, status is
file-only, and launchd lifecycle tests pass.

Commit:

```bash
git add src/open_trader/cli.py tests/test_account_sync_cli.py \
  tests/test_futu_watch_cli.py \
  ops/launchd/com.open-trader.account-sync-controller.plist.template \
  scripts/install_account_sync_launchd.sh \
  scripts/uninstall_account_sync_launchd.sh \
  tests/test_account_sync_launchd.py
git rm tests/test_futu_account_cli.py tests/test_tiger_account_cli.py
git commit -m "feat: install sole account sync controller"
```

---

### Task 6: Remove statement and premarket portfolio writers

**Files:**

- Modify: `src/open_trader/futu_account.py`
- Modify: `src/open_trader/tiger_account.py`
- Modify: `src/open_trader/pipeline.py`
- Modify: `src/open_trader/statement_import.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `tests/test_futu_account.py`
- Modify: `tests/test_tiger_account.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_statement_import.py`
- Modify: `tests/test_daily_premarket.py`
- Modify: `tests/test_premarket_cli.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**

- Produce:

```python
def require_published_portfolio(
    *,
    data_dir: Path,
    portfolio_path: Path,
    market: str,
    now: datetime,
) -> Path: ...
```

- Remove:
  - portfolio `--update-latest` from `import-statements`;
  - `refresh_live_portfolio`;
  - premarket imports of Futu/Tiger account clients and broker sync functions;
  - `FutuPortfolioSyncResult`, `TigerPortfolioSyncResult`,
    `sync_futu_portfolio`, and `sync_tiger_portfolio`;
  - broker-local latest backup/restore and preserved-row merge helpers used only
    by those sync functions;
  - statement-import rollback snapshots of `portfolio.csv`;
  - pipeline promotion/backup code whose only purpose is replacing the latest
    portfolio;
  - `ImportResult.latest_path`, `run_uploaded_statement(portfolio_path=...)`,
    and `StatementImportService(portfolio_path=...)`.

- [ ] **Step 1: Write failing statement candidate-only tests**

For CLI and Dashboard uploads, seed `data/latest/portfolio.csv` with sentinel
bytes, import a newer Phillips or Eastmoney statement, and assert:

```python
assert portfolio_path.read_bytes() == sentinel
assert result.run_dir.joinpath("extracted_positions.csv").is_file()
assert result.run_dir.joinpath("extracted_cash.csv").is_file()
```

Require CLI help not to contain the portfolio `--update-latest` option.
Unrelated market-scoped extractor commands may retain their own
`--update-latest`; do not remove those.

- [ ] **Step 2: Make all statement import paths candidate-only**

Call `_run_import(..., replace_latest_broker=parser.broker)` from uploaded and
monthly statement flows, make `_run_import` candidate-only, and delete its
`update_latest` parameter plus the latest portfolio backup/promotion branch.
Keep atomic run-directory promotion, archive rollback, fill freezing, reports,
and `trend_api_stats.json`.

The response should report the accepted candidate/run path, not claim that the
Dashboard portfolio was refreshed.

Update Dashboard server construction to create `StatementImportService` without
a portfolio argument. This is only dependency wiring; the broader Dashboard
file-projection change remains in Task 8.

- [ ] **Step 3: Write failing premarket file-gate tests**

Patch Futu/Tiger constructors to fail if called. Seed current accepted state for
the target market and require the runner to use the configured
`data/latest/portfolio.csv`.

Parameterize `failed`, `stale`, `unknown`, missing state, and stale controller
heartbeat. Each must fail before `premarket_runner` runs, with a concise broker
and last-success reason.

- [ ] **Step 4: Replace live refresh with published-file validation**

`require_published_portfolio` reads `account_sync_state.json` and
`controller_status.json`, derives health through Task 1, checks every broker
named by target-market portfolio rows, and returns the existing portfolio path
only when current.

Production `run-daily-premarket` always uses this validator. A dry run may skip
external side effects, but it must not generate a current-position action from
failed/stale/unknown state.

- [ ] **Step 5: Delete the now-unreferenced broker publishers**

After CLI and premarket no longer import the old sync functions, delete both
sync result dataclasses, both sync functions, latest backup/restore helpers, and
preserved-row merge code used only by them. Delete tests whose only contract is
updating/restoring `data/latest/portfolio.csv`; retain all client and candidate
normalization/completeness tests.

- [ ] **Step 6: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_futu_account.py tests/test_tiger_account.py \
  tests/test_pipeline.py tests/test_statement_import.py \
  tests/test_daily_premarket.py tests/test_premarket_cli.py \
  tests/test_dashboard_web.py
```

Expected: statement imports leave the latest portfolio unchanged, and premarket
uses only current accepted files.

Commit:

```bash
git add src/open_trader/futu_account.py src/open_trader/tiger_account.py \
  src/open_trader/pipeline.py src/open_trader/statement_import.py \
  src/open_trader/daily_premarket.py src/open_trader/cli.py \
  src/open_trader/dashboard_web.py \
  tests/test_futu_account.py tests/test_tiger_account.py \
  tests/test_pipeline.py tests/test_statement_import.py \
  tests/test_daily_premarket.py tests/test_premarket_cli.py \
  tests/test_dashboard_web.py
git commit -m "fix: remove secondary portfolio publishers"
```

---

### Task 7: Fail 做T closed per affected broker

**Files:**

- Modify: `src/open_trader/t_signal_runner.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_t_signal_runner.py`
- Modify: `tests/test_futu_watch_cli.py`

**Interface changes:**

```python
@dataclass(frozen=True)
class TSignalWatchResult:
    run_date: str
    market: str
    signal_count: int
    notified_count: int
    run_path: Path
    latest_path: Path
    blocked_count: int = 0

def run_t_signal_watch_once(
    *,
    portfolio_path: Path,
    account_state_path: Path,
    controller_status_path: Path,
    data_dir: Path,
    run_date: str,
    market: str,
    session_phase: str,
    market_data_client: TSignalMarketDataClient,
    interpreter: TSignalInterpreterProtocol | None = None,
    notifier: Notifier | None = None,
    now_fn: Any = datetime.now,
) -> TSignalWatchResult: ...
```

- [ ] **Step 1: Write failing broker-scoped safety tests**

Seed one healthy Tiger holding and one stale Futu holding. Require market facts,
interpretation, and notification to run only for Tiger. The Futu record must be
written as `action == "REVIEW"`, `status == "error"`,
`notification.should_notify is False`, with:

```text
账户数据已过期，数据截至 2026-07-30T11:56:54+08:00，仅供人工复核。
```

Repeat for `failed`, `unknown`, missing account state, and stale controller
heartbeat. For a mixed-broker portfolio row, any unsafe named broker blocks the
whole row.

- [ ] **Step 2: Implement source gating before market-data reads**

Parse the existing portfolio `brokers` field into canonical broker names.
Validate the controller heartbeat directly and use per-source effective status;
do not let an unrelated quote-loop failure block a 做T runner that has its own
market-data client. Build REVIEW records from the previous signal when
available so history stays visible, but clear suggested ratio and disable
notifications.

Healthy broker rows continue even if another independent broker is unhealthy.
Report the count as `blocked_count`.

- [ ] **Step 3: Wire fixed state paths in CLI**

`watch-t --data-dir data` derives:

```text
data/latest/account_sync_state.json
data/account_sync/controller_status.json
```

Do not add a bypass or `--force` flag.

- [ ] **Step 4: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_t_signal_runner.py tests/test_futu_watch_cli.py
```

Expected: unsafe rows produce REVIEW-only artifacts and no market-data or
notification calls.

Commit:

```bash
git add src/open_trader/t_signal_runner.py src/open_trader/cli.py \
  tests/test_t_signal_runner.py tests/test_futu_watch_cli.py
git commit -m "fix: gate t signals on accepted account state"
```

---

### Task 8: Make Dashboard APIs pure file projections

**Files:**

- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`

**Payload changes:**

```python
DashboardState.account_sync: dict[str, object]
```

`account_sync` contains:

```json
{
  "status": "ok",
  "label": "同步正常",
  "controller": {
    "status": "ok",
    "pid": 123,
    "git_sha": "abc123",
    "heartbeat_at": "2026-07-30T12:10:05+08:00"
  },
  "brokers": {
    "tiger": {
      "status": "ok",
      "data_as_of": "2026-07-30T12:10:00+08:00",
      "last_success_at": "2026-07-30T12:10:00+08:00",
      "message": ""
    }
  }
}
```

- [ ] **Step 1: Write failing API no-side-effect tests**

Patch `FutuAccountClient`, `TigerAccountClient`,
`DashboardQuoteService.refresh`, and every controller constructor to raise if
called. Start the HTTP server and request `/api/dashboard` and `/api/quotes`.

Require:

```python
assert dashboard_payload["account_sync"]["status"] == "ok"
assert len(dashboard_payload["broker_positions"]) == 14
assert quotes_payload == seeded_quotes_file_payload
```

Also assert neither request changes the mtime or bytes of any accepted file.

- [ ] **Step 2: Write failing accepted-state-only detail tests**

Seed:

- accepted Tiger state with 14 positions;
- a newer failed run directory containing 8 positions;
- `portfolio.csv` with a deliberately different aggregate count.

Require account cards, broker summary, real holdings, cash, source status, and
trend-report account context to use the 14 accepted rows. The failed/newer run
directory must have no effect.

- [ ] **Step 3: Replace run-directory discovery**

In `load_dashboard_state`:

- load `account_sync_state.json`;
- get normalized accepted `broker_positions` and `cash_details` from that state;
- derive statement period from accepted statement sources;
- build broker summaries and source statuses from those accepted rows/statuses;
- add `project_account_sync_health(state, controller_status, quotes, now=now)`;
- stop calling `_latest_broker_details`,
  `latest_broker_detail_month`, or newest Tiger snapshot metrics for account
  truth.

Delete those obsolete discovery helpers when no callers remain.

- [ ] **Step 4: Make `/api/quotes` a file read**

Replace `build_quotes_payload(quote_service, account_sync_service)` with:

```python
def build_quotes_payload(config: DashboardConfig) -> dict[str, object]:
    return load_published_quotes(
        config.data_dir / "latest" / "quotes.json",
        now=datetime.now(SHANGHAI_TZ),
    )
```

Remove `quote_service` and `account_sync_service` parameters from
`create_dashboard_server`; remove the account sync construction in
`serve_dashboard`. Keep unrelated simulation/trading services unchanged.

- [ ] **Step 5: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_dashboard.py tests/test_dashboard_web.py
```

Expected: Dashboard APIs return accepted files, ignore newer failed candidates,
perform no broker reads, and perform no writes.

Commit:

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py \
  tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "fix: make dashboard account data file only"
```

---

### Task 9: Implement the approved read-only status UI

**Files:**

- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/e2e/dashboard-warm-ledger.spec.ts`
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`

- [ ] **Step 1: Write failing static and rendering tests**

Require:

```python
assert 'id="refresh-quotes"' not in html
assert "刷新账户与行情" not in html + js
assert 'id="account-sync-status"' in html
assert "accountSyncReloadNeeded" not in js
```

Render each state and assert visible text:

- `同步正常`
- `同步异常`
- `同步失败 · 数据截至 11:56`
- `数据已过期 · 数据截至 11:56`
- `同步状态未知 · 数据未验证`
- `人工复核`

Require text labels in addition to semantic color classes.

- [ ] **Step 2: Replace the refresh control and polling behavior**

Replace the button with a read-only status block containing
`account-sync-status` and controller heartbeat text. Remove click binding,
disabled/loading state, and account-sync response handling.

The existing five-second browser poll continues to fetch `/api/quotes` and then
reloads `/api/dashboard` with `preserveOnError: true`. This only rereads local
published files; it cannot trigger external sync.

- [ ] **Step 3: Render broker status and stale-data banner**

Add one helper that reads `state.dashboard.account_sync.brokers[broker]`.
Use it in:

- broker cards;
- source rows;
- the selected account holdings header;
- row action labels.

For `failed`, `stale`, or `unknown`, render a full-width banner above that
broker's accepted rows, including data-as-of and the reason actions are paused.
Render the row action as non-executable `人工复核` instead of the `做T` button.

- [ ] **Step 4: Make account rows originate from accepted broker details**

Change `accountHoldingGroups()` to iterate
`state.dashboard.broker_positions`, grouped by broker, and enrich a row with
matching strategy/quote data when available. Do not require a matching
`portfolio.csv` holding to display an accepted account position.

This is the UI-side proof that a 14-position accepted Tiger snapshot renders 14
rows even if an unrelated aggregate projection is temporarily different.

- [ ] **Step 5: Implement mobile layout from the mock**

At `max-width: 760px`:

- put sync status before assets;
- keep broker/source status text visible;
- render account rows as cards;
- hide table header;
- prevent page-level horizontal overflow;
- retain symbol, quantity, price, value, P/L, and review status.

Add browser assertions at desktop and mobile widths:

```javascript
expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth)
expect(visibleTigerRows).toHaveCount(14)
```

- [ ] **Step 6: Extend the acceptance contract**

The acceptance checker must fail when:

- the refresh button or old text is visible;
- controller status is missing/stale/wrong SHA/wrong working directory/dead PID;
- a required source is unknown/failed/stale in the seeded normal flow;
- accepted count differs from visible broker rows;
- failed/stale UI lacks its banner or still exposes a 做T action;
- mobile has horizontal page overflow.

Acceptance may use a controlled failed-state fixture to prove error rendering,
but that fixture does not substitute for the required live account/quote refresh
in the final real run.

- [ ] **Step 7: Run and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
```

If the Playwright dependency is available, also run:

```bash
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

Expected: desktop/mobile status, 14 accepted rows, stale banner, and no refresh
button all pass.

Commit:

```bash
git add src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py tests/test_dashboard_acceptance.py \
  tests/e2e/dashboard-warm-ledger.spec.ts \
  tests/e2e/fixtures/kelly-dashboard.json
git commit -m "feat: show read only account sync health"
```

---

### Task 10: Update operator docs, prove the live chain, and pass the final gate

**Files:**

- Modify: `README.md`
- Modify: `docs/monthly_portfolio_import.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update operator documentation**

Document only this production chain:

```text
Futu account / Futu quotes / Tiger account
                   |
                   v
      account-sync-controller (single PID)
                   |
                   v
 account_sync_state.json / portfolio.csv / quotes.json
                   |
                   v
             Dashboard reads
```

Replace old direct check/sync and statement `--update-latest` examples with:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader account-sync-status --data-dir data
scripts/install_account_sync_launchd.sh --repo-root "$PWD"
```

Explain that failures retain last accepted data but mark it failed/stale and
pause new account-dependent actions.

- [ ] **Step 2: Add the dated changelog entry before any merge**

Add a `2026-07-30` operator-facing entry covering:

- sole account/quote controller;
- candidate-before-publish and removal of rollback;
- read-only Dashboard and removed refresh button;
- truthful failed/stale/unknown status;
- premarket/做T fail-closed behavior;
- new launchd install/status commands.

Commit documentation before any merge:

```bash
git add README.md docs/monthly_portfolio_import.md CHANGELOG.md
git commit -m "docs: explain sole account sync ownership"
```

- [ ] **Step 3: Run all focused suites**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_account_sync_state.py \
  tests/test_account_sync_controller.py \
  tests/test_account_sync_cli.py \
  tests/test_account_sync_launchd.py \
  tests/test_futu_account.py \
  tests/test_tiger_account.py \
  tests/test_dashboard_quotes.py \
  tests/test_pipeline.py \
  tests/test_statement_import.py \
  tests/test_daily_premarket.py \
  tests/test_premarket_cli.py \
  tests/test_t_signal_runner.py \
  tests/test_futu_watch_cli.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: all selected tests pass.

- [ ] **Step 4: Run the full automated suite**

Run:

```bash
make test
```

Expected: the complete pytest suite passes.

- [ ] **Step 5: Run the real controller workflow before acceptance**

Stop old Dashboard/controller processes that can retain pre-change code. Install
and start the controller from the candidate worktree, then start the read-only
Dashboard from the same Git SHA.

Verify:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader account-sync-status --data-dir data --json
launchctl print "gui/$UID/com.open-trader.account-sync-controller"
launchctl print "gui/$UID/com.open-trader.dashboard"
curl --fail --silent http://127.0.0.1:8766/api/dashboard
curl --fail --silent http://127.0.0.1:8766/api/quotes
```

Inspect fresh logs and accepted files. Require:

- one controller PID;
- controller and Dashboard working directories equal the candidate worktree;
- controller and Dashboard Git SHA equal `git rev-parse HEAD`;
- fresh account and quote successes;
- Tiger accepted/visible position count matches the real snapshot;
- no Dashboard-triggered broker read after repeated browser/API polling;
- HTTP 200.

- [ ] **Step 6: Prove one controlled failure without changing accepted rows**

Record the accepted Tiger positions/cash/summary hash, stop the launchd
controller, and run one foreground `account-sync-controller --once` with a new
empty `mktemp -d` Tiger config directory. This makes Tiger configuration fail
while the real Futu account and quote reads remain available. Verify the Tiger
accepted-data hash is unchanged, Tiger changes to `failed`, global health is
abnormal, and Tiger 做T actions are blocked.

Then reinstall/start the real launchd controller with the normal Tiger config
and require a later successful cycle to clear the failure before acceptance.

Do not edit accepted JSON by hand for this direct workflow proof.

- [ ] **Step 7: Run `make acceptance` as the final gate**

After all source and documentation commits are complete and live processes run
that exact SHA:

```bash
make acceptance
```

Expected: final line `PASS`.

- `FAIL`: continue diagnosing/fixing, recommit, redeploy, and rerun acceptance.
- `BLOCKED`: report the external/browser blocker; do not present the task for
  review.
- Do not run acceptance after any intermediate edit.

- [ ] **Step 8: Redeploy the exact accepted SHA**

Without changing source or data, restart both launchd jobs from the exact SHA
that passed acceptance. Verify new PIDs, working directories, Git SHA, fresh
logs/heartbeats, and:

```bash
curl --fail --silent --output /dev/null http://127.0.0.1:8766/
```

Expected: HTTP 200. This exact-SHA restart does not require another acceptance
run.

- [ ] **Step 9: Hand off for review**

Report:

- accepted Git SHA;
- focused/full test results;
- `make acceptance: PASS`;
- controller PID/cwd/SHA/heartbeat;
- Dashboard PID/cwd/SHA;
- Tiger accepted and visible position counts;
- review URL `http://127.0.0.1:8766/`.

Do not merge into `main` without explicit authorization. If authorized and
local `main` has not moved, use a fast-forward merge so the accepted SHA remains
the deployed SHA.
