# Task 9 force-attempt audit remediation

Implementation commit: `245dd98f8fc38d691c5ad942c58bd135f59f7d8c`

- Added an fsynced append-only `*.force_attempts.jsonl` beside each statistics cycle marker.
- Each accepted force records a start/terminal pair under one monotonic `attempt_id`, with actor, reason, process SHA, timestamp, and terminal error when failed.
- Ordinary retries and mutable completed-marker updates do not rewrite the force history.

Verification:

- `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_api_fill_sync.py` — `29 passed in 0.61s`
- `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_market_controller.py -k statistics` — `11 passed, 153 deselected in 0.54s`
- `git diff --check` — exit 0

No CN/HK/US force was rerun. No runtime data, deployment, or acceptance environment was touched. Historical force invocations from before this audit existed cannot be reconstructed safely.
