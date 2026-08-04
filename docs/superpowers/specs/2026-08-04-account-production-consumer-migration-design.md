# Account 生产消费者迁移与 Legacy 所有权删除设计

**Issue:** #23
**Parent:** #18
**Status:** Design approved for implementation planning
**Baseline:** `main@3bde68a9c1b30e4be9b73bc526b52303aea49500`

## Context

Account API 与 Account Sync Worker 已成为同一 release 下的两个独立进程，浏览器也已通过 Frontend Gateway 读取 `GET /api/v1/account/snapshot`。但生产代码仍有第二条 Account 读取路径：Legacy Dashboard、Trend statement consumer、T-signal、Premarket 与 runtime CLI 会直接读取 `account_sync_state.json`、Worker status、quotes、portfolio CSV 或 statement artifacts。

这意味着 Account artifact 布局仍是跨 module interface，Legacy 仍拥有 Account projection，Account 也无法在 R5 中证明独立升级与回滚。本阶段删除这些生产绕行路径；Account API 与 Account Sync Worker 之外，只有明确列出的 acceptance、forensics 和 offline migration 工具可以直接读取 Account runtime publications。

## Goals

- 所有生产 Account 消费者通过 Account 的版本化 HTTP interface 获取事实。
- 一个会发布独立 artifact 的 workflow execution 只获取一份 Account snapshot，并固定、记录其 `snapshot_generation`。
- Trend 保持 actions、discipline 与 risk verdict 所有权；Account 只发布事实。
- T-signal 与 Premarket 一并迁移，不退役、不保留 raw fallback。
- Legacy `/api/dashboard` 不再读取、构建或返回 Account-owned 数据。
- 浏览器继续通过稳定 ID 组合 Account 与其他 module 数据；Gateway 继续透明代理而不聚合。
- Account 故障只影响依赖 Account 的路径，不阻塞已发布的 Trend、Research、Prediction 展示。

## Non-goals

- 不重设计 Dashboard 布局、文案、交互或视觉语言。
- 不改变 broker selection、account identity、FX、valuation、weight、statement 或 sync cadence 的业务语义。
- 不迁移整个 Trend、Research、Prediction、Kelly 或 Backtest module。
- 不引入数据库、queue、cache、WebSocket、service discovery、container 或第三方依赖。
- 不建立新的 HTTP client class、repository、factory、自动重试或 per-request fallback。
- 不开始 #24，也不清理 R5 仍需使用的旧 release 资产。

## Target Architecture

```text
Broker / Statement
        |
        v
Account Sync Worker
        |
        | atomic publication and promotion
        v
Account runtime publications
        |
        | direct reads allowed only here
        v
Account API :8768
        |
        +--> Trend workers / T-signal / Premarket / runtime CLI
        |
        +--> Frontend Gateway :8766 --> Browser

Legacy Dashboard :8767 --> non-Account module data only --> Gateway --> Browser
```

The Account HTTP seam is the only production read interface. Background consumers call Account API directly on loopback. The browser continues to call the stable Gateway origin; it never connects to port 8768 directly.

## Account HTTP Interface

### Existing snapshot read

`GET /api/v1/account/snapshot` remains the complete bounded Account snapshot. Background calls send:

```http
X-Open-Trader-Account-Route: production
```

The marker makes a production consumer fail closed if it is accidentally pointed at a shadow Account API. The response and ETag rules remain unchanged.

### Accepted statement facts read

Add the narrow immutable read:

```http
GET /api/v1/account/statements/{broker}/{statement_generation}/trade-facts
```

The endpoint:

- accepts only supported statement brokers and canonical `sha256:<64 hex>` generations;
- serves facts only when the requested generation is the broker's currently accepted statement generation;
- validates the immutable generation manifest and `trade_facts_sha256` before responding;
- never exposes the PDF, candidate directory, absolute paths, credentials or parser internals;
- requires the production route marker for production consumers;
- returns no older-generation fallback.

The success body is:

```json
{
  "schema_version": "open_trader.account.statement_trade_facts.v1",
  "broker": "phillips",
  "statement_generation": "sha256:<64 hex>",
  "statement_period": "2026-07-31",
  "trade_facts_cutoff_at": "2026-08-04T16:00:00+08:00",
  "trade_facts_sha256": "sha256:<64 hex>",
  "facts": []
}
```

Malformed broker/generation requests return `400`. A valid but no-longer-accepted generation returns `409 accepted_statement_generation_changed`. A current accepted generation whose publication is missing or invalid returns `503 statement_facts_publication_invalid`.

This endpoint keeps trade facts out of the five-second browser snapshot while removing Trend's raw artifact read.

## Shared HTTP Helpers

Do not add a client class. Add two stdlib-backed functions in one small module:

```python
fetch_account_snapshot(base_url, timeout_seconds)
fetch_statement_trade_facts(
    base_url, broker, statement_generation, timeout_seconds
)
```

They use the Python standard library, send the production marker, enforce a finite timeout, decode JSON, validate the response envelope and return the validated mapping. They translate transport, HTTP and contract failures into a sanitized Account-unavailable error with a stable machine code.

The helpers do not:

- cache or retain a previous response;
- retry;
- read local files;
- call Gateway or Legacy;
- decide whether a workflow is actionable;
- construct broker, Trend or Research clients.

`base_url` and timeout remain injectable for tests and direct isolated workflows; production defaults are `http://127.0.0.1:8768` and one short finite timeout.

## Snapshot Pinning

A workflow execution is one invocation that can publish an independent artifact:

- one Trend report or revision attempt;
- one `run_t_signal_watch_once` call;
- one market-scoped Premarket run;
- one broker-scoped statement consumption attempt;
- one runtime CLI command invocation.

At the start of that execution, the consumer fetches one snapshot and records `snapshot_generation`. It must not refetch by symbol or combine data from multiple generations. A controller daemon may execute many such runs; it does not keep one snapshot for its whole process lifetime.

If a complete attempt is discarded and restarted, the new attempt may fetch a new snapshot. Partial output from the abandoned attempt must not be promoted. Every success, blocked result or failure artifact that used Account input records the pinned generation when one was obtained.

## Consumer Migration

### Trend report generation

At the start of each report/revision attempt, the Trend worker fetches one snapshot. It selects the relevant real positions, exposure and weights from that mapping, then computes Trend-owned actions and risk verdicts. The published report records at least:

```json
{
  "account_input": {
    "snapshot_generation": "sha256:<64 hex>",
    "account_generation": "sha256:<64 hex>",
    "status": "healthy"
  }
}
```

Trend must not fetch Account during a browser query, write Trend conclusions back to Account, or use Legacy/portfolio/raw-file fallback. Existing Futu simulation-account execution adapters remain Trend-owned broker adapters; they are not Account runtime publication reads and are outside this migration.

When required Account or quote sources are stale or unavailable, Trend may publish a truthful blocked report for operator visibility, but it must not publish an executable action or widen risk limits.

### T-signal

Each `run_t_signal_watch_once` call obtains one snapshot and derives its quantity baseline and broker source requirements from the snapshot positions. The artifact records `snapshot_generation` once at the run level.

T-signal requires the Account sources for the relevant positions to be healthy. Its independent market-data adapter remains unchanged. If required Account facts are stale or unavailable, rows become blocked/review records and no notification is sent. The workflow does not read `portfolio.csv`, `account_sync_state.json` or Worker status.

### Premarket

Each market-scoped Premarket run obtains one snapshot before advice, plan and trade-action generation. It uses the same pinned positions and generation through the entire run.

The existing CSV-oriented calculation flow may receive a run-scoped frozen CSV derived solely from that HTTP response. The file lives under the current run, records its source generation in the run status, and is never searched as a `latest` or next-run fallback. This retains current calculation interfaces without keeping Account persistence as a production seam.

Premarket preserves its current fail-closed source policy: Account sources needed by the market and the quote facts required by its action path must be healthy. A stale/unavailable input fails the run and produces no actionable output. Dry-run verification sends no live notification.

### Trend statement consumption

Each broker consumption attempt:

1. fetches one snapshot;
2. pins `snapshot_generation`, `account_generation` and the broker's `accepted_statement_generation`;
3. requests trade facts through the immutable HTTP endpoint using that accepted generation;
4. computes and publishes Trend-owned attribution and actual statistics;
5. records both Account and statement generations.

If promotion changes the accepted generation between steps 1 and 3, the facts endpoint returns the explicit conflict. The attempt publishes a waiting/blocked status and retries only on the next scheduled execution. It never reads `account_statements/generations` directly.

### Runtime CLI

Production CLI paths including Account status, `watch-t`, `run-premarket` and `run-daily-premarket` use the same HTTP helpers. Status-only commands may print a stale snapshot with its generation. Commands that can produce actions return nonzero or publish a blocked result when their required sources are unsuitable.

Account Worker commands, Account API parity/acceptance, forensics and explicitly offline migration commands retain their owner-specific or audit-specific reads. They are not production fallbacks.

## Legacy Dashboard And Browser Composition

### Legacy removal

`/api/dashboard` remains the temporary non-Account module response. Its production build path must not read Account snapshot, portfolio, quotes, Worker status or statement artifacts. Remove Account-owned response fields and construction logic, including:

- summary and broker summaries;
- positions, broker details and cash;
- Account source/controller/sync status;
- quotes and `/api/quotes`;
- Account-rooted holdings/enrichment construction;
- latest-Account overlays in current or historical Trend report reads;
- Account-derived Backtest universe construction.

Legacy can continue to expose existing Trend, Research, Prediction, Kelly and Backtest data. Non-Account enrichment is projected from those modules' own artifacts and keyed by deterministic opaque `instrument_id`. Missing or ambiguous identity is omitted rather than guessed from market/symbol or array order.

### Browser composition

The browser independently fetches:

```text
/api/v1/account/snapshot     Account facts
/api/dashboard               non-Account module data
```

It uses `position_id` as the Account row key and joins optional enrichment only on exact `instrument_id`. Account positions without enrichment still render with an explicit unavailable enrichment state. The browser does not parse opaque IDs, query `/api/quotes`, or fall back to Account-looking fields in `/api/dashboard`.

Backtest selectors may add current Account positions from the already loaded browser snapshot. Legacy validates the requested market/symbol and Backtest contract without reading Account state or rebuilding an Account allowlist.

## Availability And Error Semantics

- `200 healthy`: all consumers may use the snapshot subject to their own rules.
- `200 stale`: display paths may show the snapshot with its times and reasons; action-producing paths fail closed when a required source is stale.
- `503`, timeout, connection failure, invalid JSON, invalid schema or invalid generation: Account input is unavailable for that execution.
- `503 account_release_mismatch`: consumers treat the API/Worker pair as unavailable and never bypass the release check through Gateway, Legacy or local files.
- HTTP helpers do not retry. Existing scheduler/controller next executions provide recovery without hiding incidents.
- No consumer reads its previous local CSV, Legacy response or raw Account artifact after an HTTP failure.
- The browser may retain its last accepted snapshot only as visibly frozen historical context. It does not label it current.
- Existing published Trend, Research and Prediction artifacts remain readable while Account API is unavailable.

Account continues to publish source facts rather than a universal `actionable` flag. Each workflow owns and tests its required-source policy.

## Production Raw-read Audit

Add a deterministic repository audit test that inventories direct Account publication readers. Production access is allowed only in Account API/Account snapshot implementation and Account Sync Worker. Explicitly named exceptions are limited to acceptance, parity/forensics and offline migration tools.

The current migration inventory is:

| Current reader | Resolution in #23 |
| --- | --- |
| `dashboard.py` / `dashboard_web.py` | delete Account reads, fields, projections and `/api/quotes` |
| `daily_premarket.py` | fetch one HTTP snapshot per market run |
| `t_signal_runner.py` | fetch one HTTP snapshot per scan |
| production branches in `cli.py` | call HTTP-backed workflows or status reads |
| `trend_statement_consumer.py` | fetch snapshot and accepted trade facts over HTTP |
| unused real-account readers in `market_trend.py` / `a_share_trend.py` | delete rather than preserve compatibility code |

The initial source allowlist is exact:

- Account owner implementation: `account_api.py`, `account_snapshot.py`, `account_sync_worker.py`, `account_sync_state.py`, `statement_import.py` and `dashboard_quotes.py` when called by Account owner code;
- non-production verification: `dashboard_acceptance.py` and the Account parity command implemented in `account_api.py`;
- tests and test fixtures under `tests/`.

No current forensics or offline migration command needs an additional production-source exception. Adding one later requires naming its exact module and command in both the audit allowlist and operator documentation.

The audit covers at least these publication identities:

- `latest/account_sync_state.json`;
- `latest/portfolio.csv` when used as Account input;
- `latest/quotes.json`;
- `account_sync/controller_status.json`;
- `account_statements/generations` and statement fact loaders;
- imports of Account persistence/projection helpers from production consumers.

Dead legacy readers are deleted. An allowlist entry must name the file and the accepted non-production purpose; broad directory or token exclusions are not allowed.

## Rollback

The running code has no automatic per-request fallback. Operator rollback uses an explicitly retained old release and its documented process sequence. A failed #23 deployment is rolled back as a release, not by restoring Legacy Account fields or raw-file reads in the new release.

Account API and Worker remain a matched release pair. Background consumers may temporarily receive explicit unavailable responses during a pair cutover. #24 owns the full independent upgrade/rollback drill; #23 preserves the required old release assets and does not unlock #24 before operator approval.

## Verification

### Focused automated checks

- HTTP helpers: production marker, timeout, healthy/stale success, `503`, malformed JSON/schema/generation and sanitized errors.
- Statement facts endpoint: supported broker/generation, accepted-only access, hash validation, promotion race and no sensitive fields.
- Trend: one snapshot per report attempt, generation persisted, fixed inputs and fail-closed risk verdict.
- T-signal: one snapshot per scan, snapshot-derived baseline, blocked notification behavior and no raw reads.
- Premarket: one snapshot per market run, run-scoped frozen input, generation persisted, stale/unavailable failure and no previous-file fallback.
- CLI: all production Account commands use HTTP and preserve exit/status semantics.
- Legacy: Account fields and `/api/quotes` absent; no Account publication reads; historical/current Trend views use their own frozen artifacts.
- Browser: exact stable-ID composition, no guessed fallback, Account/Legacy outage isolation and visibly frozen Account state.
- Repository audit: only approved owner and non-production readers remain.

### Direct workflows

Before the final gate:

1. run an isolated Account Worker/API pair and verify production marker, snapshot and statement-facts responses;
2. run one CN/HK/US Trend report attempt and prove each output's pinned generation;
3. run one T-signal scan and one market-scoped Premarket dry run without live notification;
4. run the affected runtime CLI status and action paths;
5. stop Account API and prove Account consumers fail closed while existing Trend, Research and Prediction display endpoints remain available;
6. run the raw-reader audit and inspect process/PID/cwd/SHA/fresh logs.

### Final Dashboard gate

Run focused tests and direct checks during development. Run `make acceptance` only after the implementation candidate is complete. `PASS` is required before review. After PASS, deploy the exact accepted SHA and verify new PID, working directory, Git SHA, fresh logs and HTTP 200 at `http://127.0.0.1:8766/`.

Update and commit the dated operator-facing `CHANGELOG.md` before any merge. Submit evidence to #23 and stop for operator review. Do not label or begin #24 until that review is explicitly accepted.

## Minimality Decisions

- Two module functions replace a client class.
- Python stdlib replaces a new HTTP dependency.
- Existing snapshot contract remains unchanged.
- One narrow immutable facts endpoint replaces snapshot bloat and raw-artifact exceptions.
- Existing CSV-oriented Premarket calculations receive one run-scoped derived input instead of a broad refactor.
- Existing Gateway and two-upstream topology remain unchanged.
- Deletion replaces compatibility readers and fallback switches.
