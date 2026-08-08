# Task 9 force-attempt audit remediation

Implementation commits:

- `245dd98f8fc38d691c5ad942c58bd135f59f7d8c` — append-only force-attempt audit
- `1c89193fcb7f40470417163692ab0fcb6e4fd8c8` — strict fail-closed replay validation

- Added an fsynced append-only `*.force_attempts.jsonl` beside each statistics cycle marker.
- Each accepted force records a start/terminal pair under one monotonic `attempt_id`, with actor, reason, process SHA, timestamp, and terminal error when failed.
- Existing audit history must have valid UTF-8/newline framing, schema fields, immutable pair identity, increasing IDs, and exactly one valid terminal per start before another force can append.
- Terminal events use their actual UTC completion/failure time rather than copying the start timestamp.
- Ordinary retries and mutable completed-marker updates do not rewrite the force history.

Verification:

- `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_api_fill_sync.py` — `46 passed in 0.65s`
- `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_market_controller.py -k statistics` — `11 passed, 153 deselected in 0.51s`
- `git diff --check` — exit 0

No CN/HK/US force was rerun. No runtime data, deployment, or acceptance environment was touched. Historical force invocations from before this audit existed cannot be reconstructed safely.
