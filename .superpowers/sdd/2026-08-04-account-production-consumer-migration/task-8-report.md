# Task 8 report — Legacy Dashboard Account ownership removal

## Change

- `dashboard.py`: Legacy projection now derives `holding_enrichment` only from
  existing CN/HK/US module artifacts; it no longer reads Account state,
  portfolio, quotes, controller state, or injects live Account overlays into
  Trend reports. Backtest `holdings` is empty; the legacy watchlist remains.
- `dashboard_web.py`: removed `build_quotes_payload` and the Legacy
  `/api/quotes` route (unknown routes return 404).
- `dashboard_static/dashboard.js`: browser enrichment reads only
  `dashboard.holding_enrichment`, retaining the exact `instrument_id` join and
  `position_id` row key.
- Dashboard tests retire prior Legacy Account-projection assertions and cover
  the module-only enrichment, 404 quote route, and exact browser join.

## TDD evidence

Before implementation:

1. `pytest tests/test_dashboard.py -k uses_only_module_artifacts_for_holding_enrichment -q`
   failed because `load_dashboard_state` called `load_account_sync_state`.
2. `pytest tests/test_dashboard_web.py -k stable_id_joins_only_unique_legacy_instrument_enrichment -q`
   failed because the browser still read `dashboard.holdings`.
3. The brief focused command produced `71 passed, 10 failed`; all failures
   asserted retired Account-owned Legacy fields, Account-backed holdings, or
   `/api/quotes`.

After implementation:

- `pytest --lf -q` → `1 passed, 192 deselected` after the final browser test
  migration.
- `pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_frontend_gateway.py -q`
  was started as the complete suite; its runner progressed cleanly through 77%
  before this report/commit handoff. The parent task owns any final rerun and
  review.
- `python -m py_compile src/open_trader/dashboard.py src/open_trader/dashboard_web.py`
  passed.
- `git diff --check` passed.

## Self-review

- No Account persistence path, `build_quotes_payload`, or `/api/quotes`
  remains in `dashboard.py`/`dashboard_web.py`.
- `FrontendGateway` was not changed.
- Preserved external `bde01155` and Task 7 browser composition behavior.
