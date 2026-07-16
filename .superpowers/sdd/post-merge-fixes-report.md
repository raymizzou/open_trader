# Post-Merge Important Fixes Report

**Reviewed base:** `00350ebe3100b24f79625e180642895c0eec2db9`

**Verification head:** `07ec3b99badbc0daa6a7402781ddffba4fc9d5bb`
**Date:** 2026-07-17 Asia/Shanghai

## Fix commits

1. `a5d5d3b87e669edb61852d5aa6109d439e471523` — `fix: restore Tiger managed holding boundary`
   - Counts every positive ordinary US stock/ETF toward `AccountSnapshot.position_count`.
   - Returns only normalized `managed_symbols` in `AccountSnapshot.positions`.
   - Emits unsupported-asset exceptions only for managed symbols.
   - Restores decision/protection and unseeded watcher regressions.
2. `75ac825b369edbaa37b7aa1388d1db1fc3751d42` — `fix: preserve complete trend replay evidence`
   - Freezes and requires `price_fx_to_account_currency`.
   - Validates and restores `account.position_count`.
   - Freezes prior attention rows and broker label, then rebuilds `option_attention` through the production `build_option_attention` path.
   - Adds a US replay regression proving one remaining slot, 5-share FX sizing, and exact attention equality before and after deliberate process-version correction.
3. `9e419ed12255c01285ce5a96ffac850b69a10a99` — `docs: correct trend review ownership plan`
   - States that Futu has no trend-review entry; Tiger owns the US review.
   - Removes deleted `tiger_long_term_backtest.py` and test references.
   - Documents the actual private `_portfolio_metrics` implementation in `trend_review.py` and executable test commands.

`07ec3b99badbc0daa6a7402781ddffba4fc9d5bb` was committed separately while verification was in progress. It contains other post-merge isolation fixes and was included in the final focused/full test runs; it is not claimed as one of the three commits above.

## TDD evidence

Managed-boundary RED:

```text
.venv/bin/python -m pytest -q \
  tests/test_market_trend.py::test_load_tiger_account_separates_managed_positions_from_account_count \
  tests/test_market_trend.py::test_tiger_unmanaged_holdings_fill_position_cap_without_entering_decisions \
  tests/test_market_trend_watch.py::test_us_watcher_ignores_unmanaged_tiger_holdings_without_protection_seed
3 failed in 0.30s
```

Managed-boundary GREEN:

```text
same three tests
3 passed in 0.19s

.venv/bin/python -m pytest -q tests/test_market_trend.py tests/test_market_trend_watch.py
36 passed in 0.34s
```

Replay RED:

```text
.venv/bin/python -m pytest -q \
  tests/test_trend_review.py::test_us_replay_preserves_position_cap_fx_quantity_and_option_attention
1 failed in 0.28s
Failure: freeze_report_evidence() did not accept price_fx_to_account_currency.
```

Replay GREEN:

```text
.venv/bin/python -m pytest -q \
  tests/test_trend_review.py::test_us_replay_preserves_position_cap_fx_quantity_and_option_attention \
  tests/test_trend_review.py::test_rebuild_uses_only_frozen_inputs_and_fixed_process_version
2 passed in 0.21s

.venv/bin/python -m pytest -q \
  tests/test_trend_review.py tests/test_market_trend.py tests/test_a_share_trend.py
272 passed in 1.23s
```

## Final verification

Focused market/A-share/review/Dashboard suites:

```text
.venv/bin/python -m pytest -q \
  tests/test_market_trend.py tests/test_market_trend_watch.py \
  tests/test_a_share_trend.py tests/test_trend_review.py \
  tests/test_dashboard.py tests/test_dashboard_web.py
600 passed in 20.40s
```

Direct US replay workflow:

```text
env PYTHONPATH=src .venv/bin/python -m open_trader trend-review replay \
  --evidence /tmp/open_trader_replay_direct/pytest/test_us_replay_preserves_posit0/trend_review/evidence/US/12f8c461988178ea1dfc24f5db78a1e092f029f9b763934f77a2c9e75d02d9eb.json \
  --config /tmp/open_trader_replay_direct.env
{"status": "corrected", "market": "US", "date": "2026-07-16", "artifact_path": "/tmp/open_trader_replay_direct/data/trend_review/replays/US/c2d490cfaee95f2393cf13d1e01bd1a12b3d1920437d940359f5bd1ff0a86334.json"}
```

Artifact inspection:

```text
process_version: 07ec3b99badbc0daa6a7402781ddffba4fc9d5bb
position_count: 9
formal action count: 1
estimated_shares: 5
option_attention count: 2
option_attention source_broker values: 老虎, 老虎
```

The first direct invocation without `PYTHONPATH=src` resolved the main checkout's editable install and failed on its older `CandidateInput` shape. The successful command above explicitly executed this worktree.

Full suite, run once as requested:

```text
make test
2313 passed in 26.76s
```

`git diff --check` exited 0 before this report was added.

## Process inspection

- `screen` has `open_trader_dashboard_8766` running from `/Users/ray/projects/open_trader`, PID `78638`, started `Fri Jul 17 01:39:46 2026`; its log name identifies the older `d9fe44b` review deployment.
- The trend A-share/US/HK report and watcher launchd labels are loaded with no active process (`launchctl` PID column `-`, status `0`).
- No live process was restarted or redeployed: this fix branch still requires final review, and the post-merge review explicitly prohibited live migration/restarts before these fixes receive that review. Therefore this report verifies source behavior and the direct isolated replay, not live deployment.
- `make acceptance` was not run because this is a blocker-fix handoff for final review, not a request to present or deploy a completed Dashboard task.
