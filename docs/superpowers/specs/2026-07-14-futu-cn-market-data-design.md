# Futu-Only A-Share Market Data Design

## Goal

Use Futu OpenD as the only market-data source for A-share features. Remove
AKShare and keep every existing A-share workflow usable with the same Futu
connection already used for US and HK quotes.

## Scope

The change covers existing Shanghai and Shenzhen A-share stocks and ETFs:

- Dashboard quote refresh, current value, and unrealized profit/loss.
- Quote watchers and trading-plan checks.
- Historical daily K-lines and the local price cache.
- Standard strategy backtests, including the CSI 300 benchmark.
- Technical facts and daily premarket workflows that consume the shared Futu
  quote universe or daily K-line client.

Beijing Stock Exchange securities are out of scope because the existing
Eastmoney import and backtest contracts support only Shanghai and Shenzhen
A-shares.

## Data Source Contract

Futu OpenD is the sole source. Do not fall back to AKShare or another public
endpoint when OpenD is stopped, quote login is unavailable, or A-share quote
rights are missing. Return the existing actionable Futu diagnostic instead so
the operator can restore OpenD or account permissions.

Delete the AKShare provider, its tests, its Dashboard selection branch, and the
`akshare` project dependency.

## Symbol Normalization

Use one shared Futu symbol conversion path for every quote consumer:

- US symbols remain `US.<symbol>`.
- Numeric HK symbols remain zero-padded as `HK.<five digits>`.
- Shanghai stocks and ETFs use `SH.<six digits>`.
- Shenzhen stocks and ETFs use `SZ.<six digits>`.
- The CSI 300 benchmark `CN.000300` maps to `SH.000300`.

Reject blank, malformed, mismatched, or unsupported symbols before calling
OpenD. Do not send the internal portfolio prefix `CN` to Futu.

## Runtime Flow

`DashboardQuoteService` includes eligible CN rows in the existing quote batch.
The existing `FutuQuoteClient.get_snapshots()` call retrieves US, HK, SH, and SZ
snapshots together and returns the same quote-row schema. Dashboard overlay
then uses those fresh CN snapshots instead of statement prices or incidental
backtest cache contents.

Historical consumers continue calling the existing daily K-line protocol.
The Futu client normalizes the internal market symbol before
`request_history_kline()`, so A-share instruments and `SH.000300` use the same
cache and backtest pipeline as US and HK instruments.

## Failure Handling

Keep the current Futu connection, network-interruption, snapshot, and K-line
errors. A failed A-share request must be visible as a failed or partial source;
it must not silently reuse AKShare data. Existing last-successful Dashboard
quotes may remain marked stale under the current stale-data contract.

## Verification

Use test-driven changes for symbol conversion, CN quote-universe inclusion,
Dashboard quote overlay, A-share historical K-lines, and backtest provider
selection. Run the focused tests and the complete Python suite.

Then use the real logged-in OpenD to verify current A-share holdings plus
`SH.000300` snapshots/history. Run the affected Dashboard workflow and finish
with `make acceptance`. Only `PASS` permits merging.

After acceptance, commit the exact accepted SHA, merge it into `main`, deploy
that SHA, and verify the new process PID, working directory, Git SHA, fresh
logs, and HTTP 200 review URL.
