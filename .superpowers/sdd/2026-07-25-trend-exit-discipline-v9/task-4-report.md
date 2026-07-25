# Task 4 report: propagate versions and present current exit discipline

## Result

- Current contracts accepted: CN `v9`, US `v6`, HK `v6`.
- Current Markdown and Feishu output no longer emits the partial-profit count.
- Current protection-trigger output distinguishes `2×ATR14 硬止损` from an inherited raised protection line.
- Dashboard action and frozen-parameter views use the report's market/version identity.
- Historical `SELL_PARTIAL` labels, action formatting, and replay paths remain unchanged.
- Removed the uncalled duplicate `renderCnTrendDisciplines()` renderer.

## Verification

Focused contract/UI command:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_review.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py tests/test_a_share_trend.py -q -k 'current_live_strategy_versions or approved_mixed or current_exit_copy or historical_partial'
8 passed, 1371 deselected in 0.34s
```

Focused Task 4 projection/output command:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_review.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py tests/test_a_share_trend.py -q -k 'current_live_strategy_versions or approved_mixed or current_exit_copy or historical_partial or disciplines_show_relaxed_current_entry_rules or dashboard_renders_partial_sell_with_simulation_target_at_desktop_and_mobile or dashboard_accepts_current_live_risk_and_drawdown_contract or acceptance_validates_current_live_strategy_versions'
13 passed, 1366 deselected in 0.95s
```

Full affected projection command from the data-bearing repository root:

```text
1374 passed, 5 failed in 33.41s
```

The five failures are existing Task 1 expectation updates outside this task:
four tests still assert CN `v8`/the old pre-v9 buy behavior for a 2026-07-14
runner, and one exact Feishu test expects the old two-buy output. No Task 4
focused test failed.

Additional checks:

```text
PYTHONPATH=src .venv/bin/python -m compileall -q src/open_trader
git diff --check
```

Both passed.

## Commit

`feat: present current trend exit discipline` (final hash available via `git log`)
