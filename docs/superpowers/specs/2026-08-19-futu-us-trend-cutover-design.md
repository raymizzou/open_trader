# Futu US Trend Account Cutover (Tiger → Futu)

## Status

- Date: 2026-08-19
- Issue: none (operator-approved account cutover)
- Decision: approved design; implementation complete in worktree
  `futu-us-trend-cutover` (branch `feat/futu-us-trend-cutover`), migration
  script written but **not executed** — the operator (main agent) runs it at
  the cutover window
- Runtime impact: US trend real-holding identity and actual-fill source move
  from tiger to futu; the trend discipline engine (futu simulate account) is
  unchanged

This design replaces the account positioning of
`2026-07-16-tiger-trend-futu-options-attention-design.md` where the two
conflict (US real holdings are now on futu, not tiger). That earlier document
is not rewritten; this document is the authoritative record for the cutover
decisions.

## 决策摘要 (Decision summary)

The US trend portfolio moved to the futu real account; the tiger account has
effectively exited US trend (1 股 AGRZ + 港元货币基金 + 少量现金). Code wiring
for "US trend = tiger" switches to "futu". This is an account-identity /
data-source cutover, not a strategy change.

1. Simulate engine untouched: 4% simulated-net-value position sizing, cash /
   seat constraints, automatic paper orders, Kelly samples (market +
   strategy_id + entry version keyed) all stay as-is. Env variable names and
   account ids unchanged (`OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID` etc.).
2. US real-holding source becomes the futu real-time snapshot (the shared
   account snapshot already syncs the futu broker via `account_sync_worker`;
   `load_real_holding_input` reads by broker key). The 14 futu stocks (SNOW,
   RJF, REGN, PYPL, NUE, MMM, LPLA, LH, KO, GRMN, GPN, DGX, CRNX, ADP) are
   trend-recommended names and run the full real-holding discipline (daily
   checks, forced-exit signals, active protection lines — names without
   history establish lines at switch-time price − 2×ATR14, ratchet up only,
   rotation comparison). Real positions still never enter Kelly.
3. `REAL_HOLDING_TREND_EXCLUDED_SYMBOLS` stays `{"US.AGRZ"}` — no expansion.
4. Actual fills: a new Futu real-account US stock fill client replaces
   `TigerActualFillClient` in the daily statistics cycle. All US stock fills
   are attributed (the user confirmed old trades remain valid samples).
   Historical `("actual","tiger")` data stays readable as legacy; the new
   source is `("actual","futu")`.
5. Option attention additionally includes US real-holding rows — they are the
   covered-call base positions, and their trend-field changes enter the
   attention list and the daily Feishu delivery.
6. Futu option positions (`asset_class == "option"`) are listed explicitly as
   **account exceptions** on the real-holding page (visible, excluded from
   trend judgment, produce no actions). Futu HK holdings (e.g. 02840 SPDR金)
   never enter the US real-holding view.
7. Tiger is removed from all US trend wiring (identity, real source, fills).
   Account sync is preserved (`REQUIRED_BROKERS` unchanged); the Dashboard
   tiger card becomes 已调仓 / 现金管理 (account view only).
8. State directories rename `trend_us_tiger` → `trend_us_futu` (data and
   reports). A migration script is provided but not executed here (operator
   runs it at the cutover window).

## 账户与入口 (Accounts & entries)

| Surface | Before | After |
| --- | --- | --- |
| US trend broker identity (`MARKET_SETTINGS`, `broker_by_market`, `expected_broker`) | tiger | futu |
| US notification label | 老虎 / 美股 | 富途 / 美股 |
| US report directory (data + reports) | `trend_us_tiger` | `trend_us_futu` |
| US real-holding source | tiger account snapshot | futu account snapshot |
| Actual fills in daily statistics | `TigerActualFillClient` | `FutuActualFillClient` (real env) |
| Dashboard US trend card | tiger, 趋势 / 美股趋势交易 | futu, 趋势 / 美股趋势交易 |
| Dashboard tiger card | trend identity | 已调仓 / 现金管理 (account view only) |
| Option attention rows | candidates + simulated holdings | + US real-holding rows (last, win merge) |
| Simulated account (engine) | futu (unchanged) | futu (unchanged) |

Real-holding attention rows are marked by a `REAL_`-prefixed `source_action`
(schema contract `OPTION_ATTENTION_KEYS` is an exact key set, so no new row
keys are introduced). Rows persist under `signal_snapshots.real_holdings`
(symbol → `_holding_signal` row or `None`) so `_previous_attention_rows` can
diff them across reports. Category logic (risk / strengthened / watch) is
reused, not reinvented.

Account exceptions surface through the existing mechanism: `RealHoldingInput`
carries `account_exceptions`, serialized as a joined string key on
`real_holding_decisions_source` (all-string values contract of
`_valid_trend_collections`), rendered with the "unsupported Futu asset: "
prefix and displayed as 富途账户不支持的资产 by the account page.

## 数据职责 (Data responsibilities)

- `_SOURCE_MARKETS` now contains `("actual","futu")` for US; `("actual","tiger")`
  remains a legacy-readable US source (historical artifacts must still
  validate). CN (eastmoney) and HK (phillips) sources unchanged.
- Statistics sync: `sync_trend_api_stats` / `run_trend_statistics_cycle`
  parameter `tiger_client` renamed `actual_client`; the US cycle requires a
  Futu actual client and derives the actual broker from the first fill
  (`broker == "futu"`, source id `actual:futu:<account_id>`).
- Actual-fill fee口径 (fee semantics): OpenD's `history_order_list_query`
  returns no fee columns, so `FutuActualFillClient` queries the real fee per
  order via `order_fee_query` (same authoritative-broker semantics as the old
  Tiger `get_order(show_charges=True)` path). A fill is
  `costs_complete: true` only when the fee query returned a concrete
  `fee_amount`; otherwise the fill is recorded with
  `costs_complete: false`, `cost_source: unavailable` and an explicit
  `cost_degradation_reason` (query failure or broker `N/A`) — the fee is
  never silently assumed zero, and rounds built from degraded fills are
  excluded from win-rate/payoff statistics as `costs_incomplete`. Known
  limitation: components the broker settles only monthly (e.g. some
  third-party fees) cannot be reported per order and therefore count as
  degraded; this is surfaced per fill, never silent.
- Protection state, real protection state, watch events, and the
  `daily_delivery/` dedup ledger move from `data/trend_us_tiger/` to
  `data/trend_us_futu/` so protection-line continuity holds and the cutover
  day does not re-send Feishu deliveries.
- The attention baseline is rebuilt from the last tiger report (see
  Migration) — never from the July legacy baseline — so the first futu report
  diffs against the most recent tiger view (2026-08-18), not month-old data.
- `reports/trend_us_tiger/` is kept in place as read-only history; it is
  neither migrated nor deleted.

## 迁移 (Migration)

`scripts/cutover_us_tiger_to_futu.py` is idempotent and auditable, with a
`--dry-run` mode that prints the full plan and validations without touching
the filesystem. Steps:

1. Validate sources: tiger state files exist and parse (JSON / JSONL), the
   `daily_delivery/` ledger is non-empty, and `reports/trend_us_tiger/` has at
   least one parseable report with a valid `signal_snapshots` baseline shape.
   Any validation failure aborts before a destructive step.
2. Archive (move, never delete) the legacy July-era `data/trend_us_futu/` and
   `reports/trend_us_futu/` into timestamped directories under
   `data/archive/` and `reports/archive/` — stale protection lines and July
   reports must not resurrect (July reports would otherwise become the
   "previous rows" for the first futu report and produce fake attention
   diffs).
3. Migrate `protection_state.json`, `real_protection_state.json`,
   `watch_events.jsonl`, and `daily_delivery/` from `data/trend_us_tiger/` to
   `data/trend_us_futu/`. Per-item skip when the target already exists
   (idempotent re-run).
4. Rebuild `data/trend_us_futu/attention_baseline.json` from the last
   `reports/trend_us_tiger/*.json` (max as_of_date / generated_at):
   `as_of_date` plus the full `signal_snapshots` with `holdings` and
   `real_holdings` ensured as symbol maps (the shape the new real-holding-row
   attention logic requires). Written with 0600 permissions.
5. Write `data/trend_us_futu/.cutover-complete.json` (completion marker; a
   re-run exits early with the marker) and a `manifest.json` inside each
   archive directory. The manifest records executed actions, moved items,
   baseline source/as-of-date/counts, ledger range, and every validation.
6. `data/trend_us_tiger/` remainder (July baseline, delivery receipts, logs)
   stays in place; code no longer reads that directory.

The operator executes the real migration at the cutover window, then restarts
the affected services (launchd) — this is outside the worker scope.

## 验收 (Acceptance)

- No `trend_us_tiger` references remain in `src/` or `scripts/` production
  code (history-preserving comments aside).
- No `"tiger"` US-bound references remain in `market_trend.py`,
  `a_share_trend.py`, `dashboard.py`, `trend_api_stats.py`.
- Full listed test suites pass (market_trend, market_trend_watch,
  trend_market_controller, trend_api_fill_sync, dashboard, dashboard_web,
  dashboard_acceptance, drawdown_preflight, trend_simulate_positions,
  trend_report_regeneration, strategy_drawdown_cli, trend_review), including
  new tests: option attention real-holding diff, option-position account
  exception, FutuActualFillClient with mock OpenD, migration script tmp-dir
  dry-run.
- `market_trend.market_paths(..., 'US')` resolves `data/trend_us_futu` /
  `reports/trend_us_futu`.
- `scripts/cutover_us_tiger_to_futu.py --dry-run` runs and prints the plan
  without writing (worktree has no data; the operator verifies against the
  main tree, or the pointed `--data-root/--reports-root` dry-run does).

## 非目标 (Non-goals)

- Simulate engine, discipline rules, strategy version numbers
  (`CURRENT_TREND_STRATEGY_VERSIONS` etc.), Kelly logic, drawdown baselines —
  zero changes.
- HK (phillips) and CN (eastmoney) flows — untouched.
- `REAL_HOLDING_TREND_EXCLUDED_SYMBOLS` expansion (stays `{"US.AGRZ"}`).
- Real order placement / automatic trading mechanisms.
- Executing the migration script, restarting launchd/services, or deploying —
  the operator does this at the cutover window.
- `make acceptance` — the operator's final gate.
- Tiger account sync (`REQUIRED_BROKERS` / `LIVE_BROKERS` unchanged;
  `tiger_account.py` retained).
- Rewriting the 07-16 design document's historical content or any
  CHANGELOG history entries.
- Note for git readers: the referenced
  `2026-07-16-tiger-trend-futu-options-attention-design.md` was never
  committed — `docs/superpowers/specs/` is git-ignored, so it exists only in
  the main checkout, not in repository history. This document (added with
  `git add -f` at commit time) is the authoritative record.
