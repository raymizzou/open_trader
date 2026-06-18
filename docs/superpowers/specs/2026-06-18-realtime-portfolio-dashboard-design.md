# Realtime Portfolio Dashboard Design

## Goal

Add a local web dashboard that shows the user's current portfolio with live Futu
quote refreshes.

The first version is a lightweight monitoring surface. It helps the user inspect
holdings, broker/account breakdowns, live prices, refreshed HKD market values,
data health, and existing trade actions. It does not run AI analysis, generate
new trading plans, generate new trade actions, send notifications, place orders,
or write refreshed prices back to `data/latest`.

## Scope

The dashboard runs only on the local machine:

```bash
.venv/bin/python -m open_trader dashboard
```

It listens on `127.0.0.1` by default. The first version has no login, no LAN
mode, and no public deployment mode.

The dashboard can:

- Read existing portfolio artifacts.
- Fetch live quotes from Futu OpenD.
- Poll quotes automatically every 5 seconds.
- Let the user click an immediate refresh button.
- Show clear in-page failures when refreshes fail.
- Show existing `trade_actions.csv` rows as a read-only summary.

The dashboard cannot:

- Update `data/latest/portfolio.csv`.
- Run `run-premarket`, `build-trading-plan`, or `generate-trade-actions`.
- Send Feishu or macOS notifications.
- Unlock trading or place orders.

## Selected Approach

Use a Python local web service with a simple frontend.

The backend stays inside the existing Python project and reuses current modules:

- `FutuQuoteClient` for live quote snapshots.
- `load_futu_quote_universe()` semantics for quoteable portfolio rows.
- Existing CSV contracts for portfolio, broker detail, cash, and trade action
  data.

The frontend is a static page served by the local service. It calls JSON API
endpoints for portfolio state and quote refreshes.

Alternatives considered:

- Static page plus a periodically written JSON file. This is simple, but it
  weakens live failure reporting and manual refresh behavior.
- A separate React/Vite app. This gives more frontend structure, but the first
  version does not need a Node build chain and the repo is currently Python/CLI
  oriented.

## Data Sources

The dashboard reads these local artifacts:

- Merged holdings: `data/latest/portfolio.csv`
- Broker position details: latest `data/runs/<YYYY-MM>/extracted_positions.csv`
- Broker cash details: latest `data/runs/<YYYY-MM>/extracted_cash.csv`
- Existing trade actions: `data/latest/trade_actions.csv`

The backend should discover the latest monthly import directory by finding
`data/runs/<YYYY-MM>/extracted_positions.csv` and choosing the most recent valid
month. If broker detail files are missing, the dashboard still renders the
merged portfolio and marks broker breakdowns unavailable.

Live quote data comes from Futu OpenD snapshot requests. It overlays display
fields only:

- latest price
- refreshed market value
- refreshed HKD market value
- refreshed unrealized PnL when enough cost data exists
- quote status and stale status

Missing live values stay unknown. They must not be coerced to zero.

## Dashboard Layout

Default view is the merged holdings table.

Top summary:

- Portfolio total in HKD from the merged CSV.
- Live refreshed portfolio value when quotes are available.
- Intraday or refreshed PnL based on available live quote data.
- Futu OpenD connection state.
- Last successful refresh time.
- Current polling interval.

Left side:

- Broker filters: all, Futu, Tiger, Phillips.
- Market filters: US, HK, cash, review/data-check.
- Broker/account summary using broker detail files when available.

Main table:

- Symbol and name.
- Market and asset class.
- Total quantity.
- Latest live price.
- Refreshed HKD market value.
- Portfolio weight.
- PnL or unknown marker.
- Existing trade action status when present.
- Data health marker.

Rows remain grouped by merged symbol by default. When a merged symbol exists in
more than one broker/account, the user can expand the row to see broker detail
rows.

Broker detail rows show:

- broker
- account alias
- quantity
- statement price
- statement market value
- cost value
- unrealized PnL
- confidence
- notes or missing-field status

Right side:

- Read-only summary of existing `trade_actions.csv` rows grouped by ready,
  review, and watch.
- Quote refresh health and latest error details.
- Paths for the currently loaded portfolio, broker detail, and trade action
  artifacts.

## Quote Refresh Behavior

The dashboard refreshes all quoteable holdings by default.

Quoteable holdings follow the existing Futu universe rules:

- supported markets: `US`, `HK`
- supported asset classes: `stock`, `etf`, `fund`, `option`
- quantity must be finite and non-zero

The default polling interval is 5 seconds. It should be configurable through a
CLI flag:

```bash
.venv/bin/python -m open_trader dashboard --poll-seconds 5
```

The page starts polling when loaded. It also exposes an immediate refresh
button. If a refresh is already active, a new manual refresh should not start a
second overlapping backend snapshot request.

The backend should request all current quoteable symbols in one snapshot call
where possible. If the symbol list is empty, the refresh response is successful
with zero quotes.

## API Shape

The exact route names can be adjusted during implementation, but the first
version should expose these concepts:

- `GET /`: static dashboard page.
- `GET /api/dashboard`: portfolio, broker detail, cash detail, trade action,
  loaded paths, and current server config.
- `GET /api/quotes`: latest quote snapshot status for all quoteable holdings.

`/api/dashboard` should be usable even when OpenD is unavailable. The user must
still be able to inspect static portfolio data and see why live quotes are not
available.

`/api/quotes` returns structured status:

- `ok`: all requested quoteable symbols returned usable prices.
- `partial`: the request succeeded but one or more symbols are missing quotes.
- `failed`: the snapshot request failed.

Each quote row includes:

- `market`
- `symbol`
- `futu_symbol`
- `last_price`
- `quote_time` when available
- `status`
- `error` when applicable

Each response includes:

- `requested_count`
- `quote_count`
- `missing_count`
- `fetched_at`
- `last_success_at`
- `stale`
- `diagnostic`

## Error Handling

Failures are visible in the page. They are not sent to Feishu or macOS in the
first version.

Use the existing Futu diagnostic categories where possible:

- `opend_unreachable`: OpenD TCP connection failed.
- `context_failed`: Futu quote context creation failed.
- `quote_server_interrupted`: snapshot failed with the known quote-server
  interruption state, often requiring `qot_logined=True`.
- `snapshot_failed`: Futu returned a snapshot failure.
- `missing_quotes`: snapshot succeeded but some symbols were absent.

UI behavior:

- Top connection state changes from healthy to failed or partial.
- The right health panel shows the failure type and next step.
- The main table keeps the last successful quote values when available.
- Stale prices are visibly marked stale.
- Rows with missing live quotes show `-` or `缺行情`; they never show `0`.
- If `portfolio.csv` is missing or malformed, the page shows a blocking
  portfolio-data error and does not start quote polling.

## CLI Behavior

Add a `dashboard` command to the existing CLI.

Initial options:

- `--host`, default `127.0.0.1`
- `--port`, default `8765`
- `--portfolio`, default `data/latest/portfolio.csv`
- `--data-dir`, default `data`
- `--reports-dir`, default `reports`
- `--poll-seconds`, default `5`
- `--futu-host`, default `127.0.0.1`
- `--futu-port`, default `11111`

Successful startup prints:

- dashboard URL
- portfolio path
- selected broker detail month, if found
- Futu host and port
- poll interval

The command runs until interrupted.

## Frontend Requirements

The first frontend should be small and dependency-light.

It should provide:

- A merged portfolio table.
- Expandable broker/account detail rows.
- Broker and market filters.
- Auto-refresh state.
- Manual refresh button.
- In-page error panel.
- Existing trade action summary.
- Clear unknown/stale markers.

The UI text should be Chinese by default. Machine-readable enums can remain in
English when they are part of existing CSV/API contracts, but user-facing labels
should use Chinese.

## Testing

Automated tests should not require real OpenD.

Coverage:

- Portfolio CSV loads into merged dashboard rows.
- Broker detail files load into expandable broker/account rows.
- Missing broker detail files keep the merged dashboard usable.
- Quoteable symbol selection matches existing Futu universe behavior.
- Live quote values overlay display values without writing to
  `data/latest/portfolio.csv`.
- Missing quote values remain unknown and are not rendered as zero.
- OpenD unreachable, context failure, quote-server interruption, snapshot
  failure, and partial missing quotes produce structured API diagnostics.
- `dashboard` CLI starts the local service with expected config.

Manual verification after implementation:

```bash
.venv/bin/python -m open_trader dashboard --poll-seconds 5
```

Then open the printed local URL and verify:

- The page renders the current portfolio.
- Broker detail rows can expand for multi-broker holdings.
- Quotes refresh automatically.
- Manual refresh works.
- Stale/failure states are visible when OpenD is stopped or quote login is
  unavailable.

## Deferred Work

These are intentionally out of scope for the first version:

- Futu account sync as a live holding source.
- LAN/mobile access.
- Authentication.
- Public deployment.
- Running AI analysis from the page.
- Generating or promoting new trade actions from the page.
- Feishu/macOS failure notifications.
- WebSocket streaming.
