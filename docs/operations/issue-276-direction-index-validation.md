# Issue #276: direction index validation

Recorded 2026-10-07. Scope: remove the second full direction-detail scratch
database in `lp_trial_candidates`. Both production callers retain their input
forms; builtin `dict.values()` is also preserved. The queue builder supplies a `ValuesView` of its private scratch mapping;
exclusion reprojection supplies a list, with a tuple fallback. The projection
keeps keys or sequence positions and reads the original details on demand.
No local class or source-capturing closure is added.

## Source and inputs

- Baseline: `f7667e8add1b5d1698f44d583ea234cda6fe3770`.
- Final production file Git blob: `860c13d9af8a48930766283ca27e1bec0a8fde10`.
- Mechanical rebase base: `407ef82913a2a6ed8b770ae10d17041c45210f1b` (#278).
  Only workflow documentation changed; production source and lock were unchanged.
  The same-date CHANGELOG conflict retains both operator entries.
- Unchanged `uv.lock` SHA-256:
  `0469e17dde40a3ae3bc9cb8d8b6498156091769c9a5acb0592670fb974634c89`.
- The Linux replay uses the saved whitelisted public cache from the #263
  diagnosis. Raw caches and service logs are not included in this PR.
- Private evidence: `/Users/ray/.local/share/open-trader/issue276-direction-index/`.
  `nodeids.json`, `red.log`, `native-values-red.log`, `focused-final.log`, `concurrent.log`,
  `retention-mutation-final.log`, `business-traces.json`, `comparison.json`,
  `measurement-manifest.json`, and `final-forward/` retain commands, source/input
  hashes, container configurations, both execution orders and results.
  Initial probes are retained in `run-1/` and `run-2-initial-implementation/`.
  Final tables below use the repaired production blob in both execution orders.
  Replay scripts reuse the existing phase probe and business-trace helpers.

## Focused regressions

Existing Python environment:
`/Users/ray/.local/share/open-trader/release-envs/d9a6167e2f0faa416974c01a1fa481fc71652364/bin/python`,
with `PYTHONPATH=src` against this worktree. Tests use explicit nodeids from
`nodeids.json`, not a full service suite.

- Before the fix: 4 failed, 8 passed. Failures detect the duplicate database
  at the real queue-builder and exclusion-reprojection seams, including
  controlled exception and cancellation exits. The eight full-output hashes
  were captured from the unmodified baseline; the initial placeholder-hash
  calibration reported eight expected assertion failures before pinning them.
- Initial independent review found that builtin `dict.values()` also satisfies
  `ValuesView` but exposes `.mapping`, not `_mapping`. Two added regressions
  failed with `AttributeError`; the repaired projection supports both views.
- Final focused serial selection: 68 passed. It covers the new regressions,
  all directly affected trial projections, six-step ranking and three-step
  full normal/backup traversal, existing private readers, cooldown/late-result
  and recovery-generation checks, freshness and independent exploration.
- Two-worker selection (`-n 2 --dist loadgroup`): 19 passed. It covers all new
  cases plus concurrent directional exclusions and in-flight global recovery.
- The new lifecycle cases disable cyclic GC and repeat each normal, exception
  and cancellation path three times. Weak references must clear on return.
  A controlled per-call local-class mutation retains the source and fails the
  lifecycle assertion (1 expected failure); the mutation is not in production.
- A barrier-controlled projection keeps reading the old facts when newer
  prepared inputs are published, then discards its old queue. Invalid time
  returns without reading the source. Existing YES/NO isolation, one-hour
  reference-price freshness, UNKNOWN and complete queue tests remain intact.
- Complete business traces equal baseline: ranking 6 steps,
  `f808ff54cecef7d1c11ed902b3a9ba1b5e45a6186aa6e622a041b798a30de7dd`;
  backfill 3 steps,
  `3186d92369743116bd94cfd7216d6bb7df92e5b06c19338af3905b0d46190025`.

## Linux same-input measurements

Linux aarch64, Python 3.12.14, SQLite 3.40.1, existing tagged development image
`open-trader-dev:2124aeacc661`. Every probe container has 704 MiB memory,
704 MiB memory+swap (no swap), 1 CPU, 96 tasks, network disabled, no mounts.
Both baseline/final and final/baseline execution orders exit zero without OOM.
Only these stopped probe containers were removed. No tagged images, production
state, limits, guard, caches or unrelated services were changed.

The first container in each pair has about 46 MiB more initial file-cache
charges. This reverses with execution order. Shared image-page accounting is
a likely cause; page ownership was not traced. Thus raw cgroup totals cannot
alone attribute savings to this change. The comparison below uses each run's
own `before_trial` to `inside_trial_after_shortlist` increase.

| Execution order / version | Cgroup increase MiB | Anonymous increase MiB | File-cache increase MiB | Live scratch increase B |
| --- | ---: | ---: | ---: | ---: |
| Baseline first | 32.875 | 0.016 | 31.879 | 33,427,456 |
| Final second | 0.750 | 0.750 | 0.000 | 0 |
| Final first | 0.750 | 0.754 | 0.000 | 0 |
| Baseline second | 32.875 | 0.020 | 31.879 | 33,427,456 |

The redundant 33,427,456 B database disappears. Trial-phase cgroup growth falls
by 32.125 / 32.125 MiB; anonymous growth increases by 0.734 MiB for the index.
Full raw phase samples are retained privately, including cgroup, anonymous,
file cache and live temporary-file bytes.

| Execution order | Trial seconds baseline → final | Queue-build seconds baseline → final |
| --- | ---: | ---: |
| Baseline then final | 1.354 → 0.799 | 21.741 → 23.676 |
| Final then baseline | 1.465 → 0.744 | 20.394 → 17.849 |

These instrumented elapsed times are observations, not speed gates or a
guarantee. No new absolute or percentage duration ceiling is imposed.

All four runs produce the same full projection hash:
`d8d1557035fcec0e949eaee74d7d5a3ab0431d21fdefc08bf8c1e22204ebf36b`,
with 316 normal / 0 backup candidates. Live scratch bytes are
97,349,632 → 63,922,176 inside the baseline/final trial respectively;
both return to 26,583,040 after queue return and 13,291,520 after history
return, before explicit GC. GC does not reduce scratch-file bytes further.
The summary parser initially expected an `overlap_queue_400` label; the current
source reaches `history_0`. That parser error was corrected against retained
raw output; no container run failed or was hidden.

## Limits and delivery

The replay uses synthetic reward/account inputs and stops at an early history
checkpoint (`request_count=0`). It does not replay live SDK responses, dense
network history or complete startup. Cloud x86_64/Python 3.12.4/SQLite 3.26.0
differs from this environment. Savings have no fixed 32 MiB floor. Overall
cloud capacity remains #263; this record is not live startup or long-run
acceptance. No merge, release or deployment is part of this verification.

Independent staged review and exact push-head / synthetic PR-merge CI are
recorded with the delivered PR. Local results must be associated with the
committed source and lock identities; CI evidence from a different SHA does
not transfer.
