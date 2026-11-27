# Trusted daily-host update candidates

These sources adapt the existing installed daily launcher, sync verifier and
sync companion. They are candidates only. No installed file, launchd job,
credential, share permission, or runtime was changed by this task.

`prepare.py RELEASE OUTPUT` reads Git identity and clean status from an immutable
release named by its full commit. It copies these candidates into a new output
folder and generates `daily_release.py`. It reads the existing launcher’s
literal space ID or its installed `daily_release.py` binding without executing
either file. The release set includes `run-daily.py`, `daily_sync.py`,
`daily_host.py`, `daily_paper.py`, `daily_handoff.py`, and `daily_release.py`.
Installation remains a separately authorized operation. Keep the existing
`daily_support.py`, `daily_lifecycle.py`, Foundation metadata bridge, approved
share identity and host ownership protections. Install outside the daily writable
workspace. Verify the scheduled SOP, generated release, publisher and sync pin
all identify the same release before enabling the candidates.
Record hashes for all six prepared files and the rendered SOP. Back up those
same installed paths and record which paths are absent. A rollback restores
the previous files and SOP together, including removal of the newly introduced
`daily_handoff.py` if the backup recorded it as absent. Preserve revision,
handoff, upload, broker, and order state for reconciliation.

The existing host sync performs the copy. It recomputes the display from the
frozen report and saved paper state, checks the original source digest separately
from the display digest, retains foreign-file protections and rechecks upload.
The source receipt remains bound to the immutable report; a display receipt also
records `source_sha256`. A forged display hash alone cannot authorize a copy.

The host records every uploaded current day's immutable revision as pending for
the original scheduled tab. It checks launch, process, space/tab/pane and
original transcript identity before sending a planning notice. A verified
transcript final reply must match the stored plan display before the host marks
it displayed. The next existing host run can recover a pending notice.
The tab shows the exact card and runs `paper_cli --report-date DATE cycle PLAN` after
an explicit reply. DATE stays tied to the original report across Taipei
midnight. The handle waits for preopen approvals, checks fresh same-market-day
approvals after opening, and follows existing receipts.
Host sync sends planning notices. Only the original tab can plan or process
Ray's order approval. The display refresh reuses the existing paper state.
After Taipei midnight, the existing per-report companion can continue within its
bounded 24-hour lifetime while saved orders are outstanding before their original
market close. It copies the last observed terminal update before stopping. It
never creates another tab, replays a missed opening or installs a new scheduler.

`status` and historical `rehearse` remain read-only. Local fake-state integration
covers the host verifier, allowed predecessor copy, foreign-file rejection,
source/display binding and previous-day continuation. Actual installation,
iCloud upload and broker acceptance require their own authorized verification.
