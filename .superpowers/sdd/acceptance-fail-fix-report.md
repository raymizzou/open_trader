# Dashboard Acceptance FAIL Root-Cause Fix Report

## Confirmed Root Causes

1. At 375px, `.research-chat-input-row input` retained its desktop
   `min-height: 38px`; the mobile target rules did not include it.
2. `_check_tool_workspaces` closed the research modal only after mobile target
   checks succeeded. A failed assertion left the modal open and obscured later
   browser flows.
3. Cancelling `/api/backtests/options` while leaving the backtest workspace could
   disconnect the client during `_send_json`. The resulting BrokenPipe or reset
   was treated as a business error, causing a second response write and an
   unhandled server traceback.
4. Acceptance scanned the process log before `_browser_check`, so tracebacks
   emitted during browser flows were absent from the final result.

## Minimal Fixes

- Added the research input to the existing mobile-only 44px rule. No palette
  token or desktop sizing changed.
- Wrapped research modal validation in `try/finally`; a visible close control is
  now activated even when target validation raises, while the original failure
  still propagates.
- Caught only `BrokenPipeError` and `ConnectionResetError` around JSON response
  transmission. Payload construction and serialization remain outside the catch,
  and existing business exceptions still produce their normal JSON error.
- Moved the single log scan to after `_browser_check`, covering both pre-existing
  and browser-phase errors without duplicate markers.

## TDD RED Evidence

- Real Chromium 375px modal target:
  `npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium -g 'keeps four equal tabs'`
  - `1 failed`; research input received `38`, expected at least `44`.
- Modal cleanup on validation failure:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'closes_research_modal_when_target_check_fails'`
  - `1 failed, 148 deselected`; `page.research_open` remained `True`.
- Real server client disconnect:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'ignores_client_disconnect_while_writing_json'`
  - `1 failed, 153 deselected`; `handle_error` received
    `BrokenPipeError(32, 'Broken pipe')`.
- Browser-phase fresh traceback:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'traceback_written_during_browser_check'`
  - `1 failed, 149 deselected`; acceptance incorrectly returned status `0`.

## GREEN Verification

- Focused Dashboard web and acceptance modules:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`
  - `304 passed in 17.58s`.
- Full real Chromium suite:
  `npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium`
  - `6 passed (2.5s)`.
- Full Python suite:
  `.venv/bin/python -m pytest -q`
  - `2186 passed in 28.73s`, exit `0`.
- `git diff --check`
  - exit `0`.

## Deferred Acceptance Gate

- Per the parent task, `make acceptance` was intentionally not run in this fix
  pass. The parent retains responsibility for restarting the accepted process,
  running the final live gate, checking fresh PID/SHA/logs, and review deployment.
