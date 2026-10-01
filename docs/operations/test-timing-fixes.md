# Timing-test stability repairs

Date: 2026-10-01 UTC. Audit base: `3f023fce39d55b5792eddb17dd2fb4dbf7065bb0`.

The [traceability CSV](test-timing-traceability.csv) records a disposition,
retained contract, change, and validation evidence for every one of the original
211 audit mechanisms. These are **not 211 observed flaky tests**: the audit
classified 2 historical CI failures, 155 static risks, 40 synchronization/hang
hardening items, and 14 intentional real-time contracts. A `fixed` disposition
can mean synchronization or cleanup hardening while retaining a real timeout.
`intentional` means the specified real-time test is deliberately retained.

Disposition totals: **197 fixed/hardened; 14 intentionally unchanged; 0 deferred**.
Implementation status is separate from validation: the browser and root-owned
fixture limitations below remain UNKNOWN until their supported gates run.

## PR 225 review repair and current-main integration

The original three PR commits were replayed onto local main
`4fbddaae061e95cbbbb3db4d5793e40e33ec2cae`. The six conflicting files preserve
both intents: both dated changelog histories, session-isolated Smoke scenarios,
complete current account facts, LP observation-stage logging, and explicit
business-clock/completion controls. Production source, dependency manifests,
locks, and CI workflow remain identical to that main revision.

Readiness now keeps the actual 30-second refresh and 60-second freshness
constants. Explicit stream turns cover 0/29/30/31/61/91 seconds, the exact
refresh boundary, latest checked-at values, normal stop and closed stream, and
stale readiness after refreshing stops. The current bulk-book integration keeps
900 distinct tokens, a duplicate market, nine batches of at most 100 tokens,
eight concurrent batches, and all 451 market confirmation assertions. Admission,
release and completion queues observe the actual 8+1 waves.

Both offline browser-fixture tests explicitly use `ProxyHandler({})`. An
autouse regression injects an unavailable system HTTP proxy, disables bypass
discovery, and resets the cached default opener; the tests still exercise real
HTTP, independent cookies, dynamic listener ownership and process cleanup.
The original clients failed both tests under that controlled proxy.

Review diagnostics on the original head had 7 passes and 3 failures: the
local HTTP probe reached a configured macOS system proxy, and two direct
shebang launches stalled at `/usr/bin/env`. Direct HTTP/no-proxy requests
returned 200 and explicit interpreter launches ran the same scripts normally.
The original Docker attempt stopped before tests at a Pillow TLS download
error. These attempts remain evidence, not passes for this repair.

The first repair host selection completed 16 cases, including both proxy
regressions, then stalled while loading a native Python module. A macOS process
sample showed dyld code-signature mapping blocked in `fcntl`; no lock-owner
assertion had executed. The owned test process was terminated (exit 137).
Host diagnostics do not replace Docker gates. Historical runs and hashes below
refer to the original PR revisions and do not transfer to this rebased tree;
fresh scoped verification and independent review are required before publication.

## What changed

- Monitor and runtime tests distinguish business-clock boundaries from thread,
  event-loop, startup, and publication progress. Wait for the actual task or
  Future to finish before checking delivery, deduplication, or negative state
- LP tests explicitly hold and release owners, observe followers joining the
  same Future, keep an independent risk loop active during history waits, and
  drain abandoned reads before asserting cooldown or late-result rejection
- Solver tests keep startup-inclusive deadlines and poison/cleanup behavior.
  Additional phase-proven tests establish blocked writes and descendants before
  asserting the intended timeout or cleanup. Pure business-rule cases use
  local deterministic collaborators rather than racing process cold startup
- Account, frontend, and trend tests use isolated dates/clocks, real completion
  boundaries, state-scoped fixtures, and bounded worker cleanup. The trend CLI
  keeps separate real process/protocol and timeout/kill/reap regressions
- Real contention tests with otherwise unbounded executor shutdown execute their
  complete original assertions in a child process supervised by an independent
  real-time watchdog. No test is skipped by this wrapper; nonzero child status,
  assertion failures, and watchdog expiration fail the parent test

Only tests, test support/configuration, and documentation change. Production
business code, deadline constants, access settings, CI policy, and dependency
locks are unchanged. No merge, release, deployment, Candidate Acceptance,
Host Readiness, or Production Smoke is part of this change.

## Reproductions and negative evidence

The original historical failures were observed in
[CI run 36741575218](https://github.com/raymizzou/open_trader/actions/runs/36741575218)
at an earlier commit. On the audit baseline both pass normally, so a plain green
rerun was not used as proof of a repair:

- Original readiness test fails under an injected 100ms startup delay; its
  replacement passes the same delay, proves refresh at the controlled boundary,
  and still rejects stale readiness after refresh stops
- Original universe recovery test fails with an 80ms subscription delay; the
  replacement passes and cannot declare recovery before every required stage
  and subscription completes
- Original preparation-lock owner test fails when its controller is delayed
  300ms, beyond the helper's former 200ms automatic release. The repaired test
  includes this schedule and keeps the owner alive until explicit release
- Original runtime cadence, proxy-retry, reward-publication, controller-ledger,
  and trend CLI cold-start fixtures were also reproduced under controlled
  delays. Their repaired counterparts pass those same injected schedules

Targeted, unpublished wrong-behavior mutations remain failing: accepting stale
facts, skipping a readiness refresh or subscription stage, publishing too early,
reversing notification order, sending duplicates, ignoring invalidation or
cooldown, waiting for an optional public book, accepting contradictory receipts,
omitting timeout/kill/group cleanup, and bypassing relevant lock guards.
These are scoped counterexamples, not a claim of exhaustive mutation coverage.

## Verification and limitations

Host diagnostics use Python 3.12.14, pytest 9.1.1, pytest-xdist 3.8.0, and the
unchanged `uv.lock` development/cloud-SSM environment. Commands run with
`PYTHONDONTWRITEBYTECODE=1` and `TZ=Asia/Shanghai`. Individual scope results and
repetition counts appear in the CSV; they must not be added together as a count
of unique tests. The normal full backend suite was not run locally.

The environment has no Docker. Host results are **not Docker gate results**;
the Draft PR's standard Linux GitHub Actions run is the authoritative offline
service gate for its exact head/base/synthetic merge identities. Required CI
must finish successfully before the work can be treated as ready for merge,
and merging still needs separate user approval.

Independent staged review found two additional defects in the new fixtures:
clock origin/decision-boundary skew in the real five-second cadence integration,
and missing independent watchdogs around infinite solver fixtures. The repair
uses actual real scheduling samples and post-ready, process-group-owned fallback
timers whose marker is an explicit test failure. The same scheduling faults and
ignored-deadline mutations now fail or pass as intended, with detached fixture
groups reclaimed. The Python-only E2E fixture isolation check also now contrasts
observably different initial payloads and rejects a shared-session mutation.

All failed diagnostic attempts were retained and diagnosed, including corrected
new-fixture errors, baseline environment failures, and expected negative runs:

- LP full-scope host execution initially produced 31 failures because an injected
  SOCKS fallback required an optional package absent from the locked Docker
  dependencies. The same ImportError was confirmed on the untouched baseline.
  Omitting only `ALL_PROXY`/`all_proxy` for the offline diagnostic, retaining
  HTTP(S) proxy and sandbox controls, produced 729 passed / 1 existing macOS
  Keychain skip across the 12 affected files. No dependency or assertion changed
- A root-owned cloud identity fixture cannot pass as the unprivileged host user;
  untouched-baseline checks reproduce that limitation. It remains CI-required
- The host lacks macOS `plutil`. A `/tmp` implementation matching Dockerfile.dev's
  plistlib lint wrapper supports scoped diagnostics only; it is neither a native
  macOS pass nor Docker evidence
- Node/TypeScript collection covers 26 local fixture E2E cases. One attempted
  fixture-only browser run failed all 26 before any test body because sandboxed
  Chromium could not create AF_UNIX sockets. No browser assertion ran. Browser
  execution was then stopped without escalation or retries under the existing
  zero-browser-cost development policy. Browser behavior remains **UNKNOWN**;
  no Production Smoke, live URL, or new browser CI gate was used
- Existing optional-dependency and platform skips are reported as such. No new
  skip, xfail, retry-to-green logic, or weakened business assertion was added

## First PR CI and follow-up repairs

[Run 36839602608](https://github.com/raymizzou/open_trader/actions/runs/36839602608)
tested head `8ab4a761b1d6b14a3338e1ffa2e8fb71930df1bf`, base
`3f023fce39d55b5792eddb17dd2fb4dbf7065bb0`, and synthetic merge
`f9041a57c3ca3f8d94348d45a0daed494b7a4d1b`. Gateway (55 tests), Account
(355), and Legacy (5,083 plus 31 subtests) passed their offline Docker gates.
Prediction with two workers and `TEST_N_LEG=1` had 4,107 passed, 5 existing
skips, and two failures; the exact Actions `required` check correctly failed.
The failed run remains evidence, not a retry-to-green candidate.

- The downstream canary report imported the real-chain fixture without its
  module's autouse clock. In the local reproduction on the published head,
  refreshed books were 1.459s ahead of the frozen monitor time, so the real
  driver correctly rejected them as `BOOK_UNAVAILABLE`.
  The helper and both complete-chain consumers now share one business instant
  for books, resolver/scheduler, confirmation, driver, and reconciliation.
  Original ledger/profit/conservation/report assertions remain, with explicit
  submitted-summary and exactly-two-order assertions. All three consumer files
  pass 58 tests each in two serial and two two-worker runs. Local +/-61s clock
  jumps and a 50ms setup delay pass; future/stale books and omitted
  reconciliation still reject the positive chain
- The runtime TTL test drained its trade reader but not a sibling reward
  publisher. A controlled legal interleaving reproduces the exact 1-versus-2
  assertion when the reward publisher holds the dashboard lock and the next
  refresh returns cached data. The original CI schedule itself was not traced.
  Capture and join both real producers under the existing 5s watchdog, then
  verify each requested refresh actually read its account input. The test checks
  committed timestamps at 0s, just before 60s, and after 61s. Three serial and
  three two-worker related runs pass 10 tests each; the full runtime file passes
  71 tests. The same forced interleaving passes, and TTL=0/120 mutations fail

These are tests-only follow-up repairs. The original 211-record inventory is
unchanged; the newly exposed canary consumer is documented here and in its
shared-helper record. Host follow-up evidence requires independent re-review
and fresh exact-candidate Docker CI; PR status carries that latest verdict.

## Reproducing affected checks

Use the repository's normal Docker development routes from
[agent-verification.md](agent-verification.md), with explicit affected services
or `TEST=...`; do not run Candidate Acceptance for this PR. Prediction changes
include normally omitted N-leg/solver files, so the existing CI planner restores
that coverage. Serial and `TEST_WORKERS=2` runs test the same assertions; clocks
remain local and real watchdogs remain independent.

The CSV gives each audited test's exact file/function and its verification
mapping. Dedicated real timeout, cancellation, latency, and process cleanup
cases intentionally retain real clocks. Static E2E repairs require the existing
supported browser validation route or separately approved fixture-only execution;
backend CI success does not turn their browser result into PASS.
