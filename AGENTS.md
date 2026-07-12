# Project Instructions

## Verification Discipline

For any behavior change, especially changes that affect notifications, background
watchers, launchd jobs, screen sessions, or other long-running processes, do not
stop at unit tests.

Before reporting that the change is done:

1. Run the relevant automated tests and confirm the exact pass/fail output.
2. Run the affected command or workflow directly when it is practical, and check
   the real output.
3. If a background process can keep old code in memory, inspect running
   processes and service managers such as `screen` and `launchctl`.
4. Stop or restart old processes that are still using pre-change code.
5. Verify fresh logs from the new process, including PID/timestamp when useful,
   before claiming the live behavior has changed.

Do not describe a change as fully verified if only tests were run and the live
background process was not checked.

## Dashboard Definition of Done

Run `make acceptance` after every Dashboard behavior change. Its result is the
only completion status:

- `PASS`: automated tests, real API/data, two refresh cycles, process version,
  logs, and desktop/mobile browser flows all passed.
- `FAIL`: a page, data, process, log, or test check failed.
- `BLOCKED`: the required browser or external environment is unavailable.

Only `PASS` may be described as complete, deployed successfully, or accepted.
`FAIL` must be fixed. `BLOCKED` must be reported as blocked and must not be
substituted with curl, fixtures, mocks, screenshots, or unit tests.
