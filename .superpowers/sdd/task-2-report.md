# Task 2 report: pending trend execution data display

## Status

PASS. Task 2 is implemented and committed in `a95c9b0`; the follow-up fixture
correction is committed in `47f167b`. The v5 validation hardening is committed
in `8ee7282`.

## Scope completed

- v5 pending buy actions keep JSON nulls and `market_data_status`/`pending_fields`.
- Markdown and Feishu render missing quote, ATR, lot size, quantity, risk, and
  protection values as `待补全`; formal actions remain visibly actionable.
- Unknown-risk summaries show known risk plus unknown symbols without
  formatting null risk values as zero.
- Dashboard Python validation accepts only the v5 pending contract; v1-v4
  validation remains strict.
- v5 validation accepts only its two unknown-risk symbol lists and rejects
  malformed lot-size JSON without raising.
- Dashboard buy cards and risk rows show `待补全` and pending market-data
  labels instead of placeholder zeros/dashes.
- Candidate appendix output no longer renders a missing execution/filter price
  as `None 元`.

## Verification

Command:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Result: `754 passed in 30.16s`.

Fix-wave focused check:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py -k 'v5 or malformed_pending' -q
```

Result: `3 passed, 210 deselected`.

`git diff --check` passed before both commits.

## Concerns

- No concerns in Task 2. Per task scope, `make acceptance`, live trading, and
  service restarts were not run; those belong to the parent integration gate.
