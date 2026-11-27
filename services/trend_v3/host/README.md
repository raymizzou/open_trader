# Trusted daily-host update candidates

These sources adapt the existing installed daily launcher, sync verifier and
sync companion. They are candidates only. No installed file, launchd job,
credential, share permission, or runtime was changed by this task.

`prepare.py RELEASE OUTPUT` reads Git identity and clean status from an immutable
release named by its full commit. It copies these candidates into a new output
folder and generates `daily_release.py`. The native space ID comes from the
existing installed launcher; no host or user ID is stored in these sources.
Installation remains a separately authorized operation. Keep the existing
`daily_support.py`, `daily_lifecycle.py`, Foundation metadata bridge, approved
share identity and host ownership protections. Install outside the daily writable
workspace. Verify the scheduled SOP, generated release, publisher and sync pin
all identify the same release before enabling the candidates.

The existing host sync performs the copy. It recomputes the display from the
frozen report and saved paper state, checks the original source digest separately
from the display digest, retains foreign-file protections and rechecks upload.
The source receipt remains bound to the immutable report; a display receipt also
records `source_sha256`. A forged display hash alone cannot authorize a copy.

The scheduled original tab plans after the initial trusted report upload. It
shows the exact card and runs `paper_cli --report-date DATE cycle PLAN` after
an explicit reply. DATE stays tied to the original report across Taipei
midnight. The handle waits for preopen approvals, checks fresh same-market-day
approvals after opening, and follows existing receipts.
Refresh and host sync do not plan, approve, connect to a broker or submit orders.
After Taipei midnight, the existing per-report companion can continue within its
bounded 24-hour lifetime while saved orders are outstanding before their original
market close. It copies the last observed terminal update before stopping. It
never creates another tab, replays a missed opening or installs a new scheduler.

`status` and historical `rehearse` remain read-only. Local fake-state integration
covers the host verifier, allowed predecessor copy, foreign-file rejection,
source/display binding and previous-day continuation. Actual installation,
iCloud upload and broker acceptance require their own authorized verification.
