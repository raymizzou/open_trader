# Account 生产消费者迁移与 Legacy 所有权删除设计

**Issue:** #23
**Parent:** #18
**Status:** Design approved; revised written spec pending review
**Baseline:** `main@3bde68a9c1b30e4be9b73bc526b52303aea49500`

## Context

Account API 与 Account Sync Worker 已成为同一 release 下的两个独立进程，浏览器也已通过 Frontend Gateway 读取 `GET /api/v1/account/snapshot`。但活跃生产代码仍有第二条 Account 读取路径：Legacy Dashboard、Trend report/statement consumers 与 runtime CLI 会直接读取 `account_sync_state.json`、Worker status、quotes、portfolio CSV、`data/runs/*/extracted_*.csv` 或 statement artifacts。Premarket 与 T-signal 也保留这些读取，但当前不需要继续运行，本阶段将停用其生产入口而不是迁移它们。

这意味着 Account artifact 布局仍是跨 module interface，Legacy 仍拥有 Account projection，Account 也无法在 R5 中证明独立升级与回滚。本阶段删除这些活跃生产绕行路径；Account API 与 Account Sync Worker 之外，只有明确列出的 acceptance、forensics、offline migration 工具和已停用模块内的精确例外可以直接读取 Account runtime publications。

## Goals

- 所有活跃生产 Account 消费者通过 Account 的版本化 HTTP interface 获取事实。
- 一个会发布独立 artifact 的 workflow execution 只获取一份 Account snapshot，并固定、记录其 `snapshot_generation`。
- Trend 保持 actions、discipline 与 risk verdict 所有权；Account 只发布事实。
- Premarket 与 T-signal 的 CLI、调度和运行进程停用；内部实现与历史只读产物暂留。
- Legacy `/api/dashboard` 不再读取、构建或返回 Account-owned 数据。
- 浏览器继续通过稳定 ID 组合 Account 与其他 module 数据；Gateway 继续透明代理而不聚合。
- Account 故障只影响依赖 Account 的路径，不阻塞已发布的 Trend、Research、Prediction 展示。

## Non-goals

- 不重设计 Dashboard 布局、文案、交互或视觉语言。
- 不改变 broker selection、account identity、FX、valuation、weight、statement 或 sync cadence 的业务语义。
- 不迁移整个 Trend、Research、Prediction、Kelly 或 Backtest module。
- 不引入数据库、queue、cache、WebSocket、service discovery、container 或第三方依赖。
- 不建立新的 HTTP client class、repository、factory、通用传输重试或 per-request fallback。
- 不重写或删除已停用的 Premarket、T-signal 内部实现；重新启用另开 issue 并先迁移到 Account API。
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
        +--> Trend workers / runtime CLI
        |
        +--> Frontend Gateway :8766 --> Browser

Legacy Dashboard :8767 --> non-Account module data only --> Gateway --> Browser

Premarket / T-signal --> production CLI, scheduler and process disabled
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

A workflow execution is one active invocation that can publish an independent artifact:

- one Trend report or revision attempt;
- one broker-scoped statement consumption attempt;
- one runtime CLI command invocation.

At the start of that execution, the consumer fetches one snapshot and records `snapshot_generation`. It must not refetch by symbol or combine data from multiple generations. A controller daemon may execute many such runs; it does not keep one snapshot for its whole process lifetime.

If a complete attempt is discarded and restarted, the new attempt may fetch a new snapshot. Partial output from the abandoned attempt must not be promoted. Every success, blocked result or failure artifact that used Account input records the pinned generation when one was obtained.

## Consumer Migration

### Trend report generation

At the start of each report/revision attempt, the Trend worker fetches one snapshot and passes that same mapping through every internal Trend Animals retry for the attempt. It selects the relevant real positions, exposure and weights from that mapping, then computes Trend-owned actions and risk verdicts. The published report records at least:

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

The active real-holding projection becomes a pure conversion from the pinned Account response. Delete `broker_details.py` after its callers move to that conversion, and delete the test-only `load_market_account`, `load_trend_account` and `load_eastmoney_account` raw loaders rather than preserving compatibility paths.

When required Account or quote sources are stale or unavailable, Trend may publish a truthful blocked report for operator visibility, but it must not publish an executable action or widen risk limits.

Account health is evaluated at `instrument_id` granularity. If any real-account position contributing to an instrument has an unhealthy required source, Trend blocks actions for that entire instrument even when a simulation-account position is healthy. Other instruments continue. A blocked instrument remains visible with its reason; a healthy account must not hide an unhealthy constituent of the same instrument.

### Disabled Premarket and T-signal workflows

Remove the `run-premarket`, `run-daily-premarket` and `watch-t` CLI parser and dispatch paths. Ensure the legacy `com.open-trader.premarket`, `com.open-trader.premarket.hk` and `com.open-trader.premarket.us` launchd labels are unloaded and absent; no T-signal watcher process may remain running.

Keep the internal implementations, tests and historical Dashboard artifacts for now. Their exact legacy Account reads are explicit dormant exceptions in the raw-read audit, not production fallbacks. No new consumer may call these workflows. Re-enabling either workflow requires a separate issue that removes the exception and migrates it to the Account HTTP contract first.

### Trend statement consumption

Each broker consumption attempt:

1. fetches one snapshot;
2. pins `snapshot_generation`, `account_generation` and the broker's `accepted_statement_generation`;
3. requests trade facts through the immutable HTTP endpoint using that accepted generation;
4. computes and publishes Trend-owned attribution and actual statistics;
5. records both Account and statement generations.

If promotion changes the accepted generation between steps 1 and 3, the facts endpoint returns the explicit conflict. The workflow discards every output from that attempt and immediately restarts once from a new snapshot. A second conflict stops the execution with a blocked status. Transport failures, timeouts and `503` responses do not retry inside the execution. The workflow never reads `account_statements/generations` directly.

### Runtime CLI

The active `account-sync-status` command uses the snapshot HTTP helper. It may print a stale snapshot with its generation. Its shared production default is `http://127.0.0.1:8768`; an optional `--account-url` exists only for isolated verification and diagnosis. Do not add a new environment variable or global configuration key for the fixed production port.

The disabled Premarket and T-signal CLI commands are removed rather than converted to HTTP.

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

Backtest selectors add current Account positions from the already loaded browser snapshot to the Legacy-owned watchlist options. Legacy validates the canonical market/symbol and Backtest request contract, but does not enforce membership in current Account holdings and does not call Account API. This keeps Backtest usable during an Account outage.

## Availability And Error Semantics

- `200 healthy`: all consumers may use the snapshot subject to their own rules.
- `200 stale`: display paths may show the snapshot with its times and reasons; action-producing paths fail closed when a required source is stale.
- `503`, timeout, connection failure, invalid JSON, invalid schema or invalid generation: Account input is unavailable for that execution.
- `503 account_release_mismatch`: consumers treat the API/Worker pair as unavailable and never bypass the release check through Gateway, Legacy or local files.
- HTTP helpers do not retry. The only workflow-level retry is one complete Trend statement-attempt restart after `409 accepted_statement_generation_changed`; existing scheduler/controller next executions recover all other failures without hiding incidents.
- No consumer reads its previous local CSV, Legacy response or raw Account artifact after an HTTP failure.
- After a successful fetch, the current browser page may retain its last accepted snapshot in memory only as visibly frozen historical context. It disables Account-dependent actions and does not label the snapshot current. A fresh page with no successful snapshot shows Account unavailable; it does not load persisted browser state.
- Existing published Trend, Research and Prediction artifacts remain readable while Account API is unavailable.

Account continues to publish source facts rather than a universal `actionable` flag. Each workflow owns and tests its required-source policy.

## Production Raw-read Audit

Add a deterministic source-scan test that inventories direct Account publication readers. Do not add an AST call-graph or dependency-analysis tool. Active production access is allowed only in Account API/Account snapshot implementation and Account Sync Worker. Explicitly named exceptions are limited to acceptance, parity/forensics, offline migration tools and the exact dormant Premarket/T-signal reads approved by this design.

The current migration inventory is:

| Current reader | Resolution in #23 |
| --- | --- |
| `dashboard.py` / `dashboard_web.py` | delete Account reads, fields, projections and `/api/quotes` |
| `daily_premarket.py` | retain internal code as a dormant exception; remove all production entrypoints and scheduling |
| `t_signal_runner.py` | retain internal code as a dormant exception; remove the production CLI entrypoint and prove no watcher runs |
| production branches in `cli.py` | migrate Account status to HTTP; remove Premarket/T-signal parsers and dispatch |
| `trend_statement_consumer.py` | fetch snapshot and accepted trade facts over HTTP |
| `broker_details.py` and active `load_real_holding_input` callers | replace the runs-directory scan with a pure projection from the report attempt's pinned HTTP snapshot, then delete `broker_details.py` |
| test-only raw loaders in `market_trend.py` / `a_share_trend.py` | delete `load_market_account`, `load_trend_account` and `load_eastmoney_account` rather than preserve compatibility code |

The initial source allowlist is exact:

- Account owner implementation: `account_api.py`, `account_snapshot.py`, `account_sync_worker.py`, `account_sync_state.py`, `statement_import.py` and `dashboard_quotes.py` when called by Account owner code;
- non-production verification: `dashboard_acceptance.py` and the Account parity command implemented in `account_api.py`;
- dormant disabled workflows: exact existing Account reads in `daily_premarket.py` and `t_signal_runner.py` only;
- tests and test fixtures under `tests/`.

No current forensics or offline migration command needs an additional production-source exception. Adding one later requires naming its exact module and command in both the audit allowlist and operator documentation.

The audit covers at least these publication identities:

- `latest/account_sync_state.json`;
- `latest/portfolio.csv` when used as Account input;
- `latest/quotes.json`;
- `account_sync/controller_status.json`;
- `data/runs/*/extracted_positions.csv` and `extracted_cash.csv` when used as current Account input;
- `account_statements/generations` and statement fact loaders;
- imports of Account persistence/projection helpers from production consumers.

All other legacy readers are deleted. A dormant exception or allowlist entry must name the file, exact matched read/import and accepted non-production purpose; broad directory or token exclusions are not allowed. Any new production match fails the test. Re-enabling a dormant workflow requires removing its exception first.

## Rollback

The running code has no automatic per-request fallback. Operator rollback uses an explicitly retained old release and its documented process sequence. A failed #23 deployment is rolled back as a whole release, not by restoring Legacy Account fields or raw-file reads in the new release. After consumers adopt the trade-facts endpoint, the Account pair is not independently rolled back to #22, because that release does not provide the required interface.

Account API and Worker remain a matched release pair. #24 owns the full independent upgrade/rollback drill, but its minimum rollback baseline is the accepted #23 release. Independent rollback in #24 is only between post-#23 Account versions that preserve the snapshot and trade-facts contracts. #23 preserves the required release assets and does not unlock #24 before operator approval.

## Deployment Order

Deploy the exact candidate SHA in dependency order:

1. restart Account API/Worker and verify both snapshot and trade-facts contracts;
2. restart Gateway, Legacy Dashboard and active Trend consumers at the same SHA;
3. unload the Premarket launchd labels and prove that no Premarket or T-signal process or CLI entrypoint remains.

Do not add a dual-read window, compatibility adapter or long-lived mixed-version mode. A short explicit unavailable interval during process restart is acceptable and observable.

## Verification

### Focused automated checks

- HTTP helpers: production marker, timeout, healthy/stale success, `503`, malformed JSON/schema/generation and sanitized errors.
- Statement facts endpoint: supported broker/generation, accepted-only access, hash validation, promotion race and no sensitive fields.
- Trend: one snapshot per report attempt, generation persisted, fixed inputs, per-`instrument_id` failure isolation and fail-closed risk verdict.
- Statement consumer: one full restart after a generation conflict, no retry for transport/`503`, and no partial promotion from an abandoned attempt.
- Disabled workflows: Premarket/T-signal CLI commands absent and their dormant raw reads limited to the exact audit exceptions.
- CLI: active Account status uses HTTP, preserves output semantics and supports only the explicit diagnostic URL override.
- Legacy: Account fields and `/api/quotes` absent; no Account publication reads; historical/current Trend views use their own frozen artifacts.
- Browser: exact stable-ID composition, no guessed fallback, in-memory-only frozen Account state, Account/Legacy outage isolation and browser-composed Backtest options.
- Repository audit: only approved owner and non-production readers remain.

### Direct workflows

Before the final gate:

1. run an isolated Account Worker/API pair and verify production marker, snapshot and statement-facts responses;
2. run one CN/HK/US Trend report attempt and prove each output's pinned generation;
3. run the affected Account status CLI against the isolated API;
4. prove all Premarket/T-signal CLI commands, launchd labels and running processes are absent; do not execute a dry run or send a notification;
5. stop Account API and prove active Account consumers fail closed while existing Trend, Research and Prediction display endpoints remain available;
6. run the raw-reader audit and inspect process/PID/cwd/SHA/fresh logs.

### Final Dashboard gate

Run focused tests and direct checks during development. Run `make acceptance` only after the implementation candidate is complete. `PASS` is required before review. After PASS, deploy the exact accepted SHA and verify new PID, working directory, Git SHA, fresh logs and HTTP 200 at `http://127.0.0.1:8766/`.

Update and commit the dated operator-facing `CHANGELOG.md` before any merge. Submit evidence to #23 and stop for operator review. The evidence includes absence of the disabled CLI/process/launchd paths and contains no Premarket/T-signal live notification. Do not label or begin #24 until that review is explicitly accepted.

## Minimality Decisions

- Two module functions replace a client class.
- Python stdlib replaces a new HTTP dependency.
- Existing snapshot contract remains unchanged.
- One narrow immutable facts endpoint replaces snapshot bloat and raw-artifact exceptions.
- Premarket and T-signal are disabled instead of spending #23 on unused consumer migrations.
- Existing Gateway and two-upstream topology remain unchanged.
- Deletion replaces compatibility readers and fallback switches.
