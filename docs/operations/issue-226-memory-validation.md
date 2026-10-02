# Issue #226 memory validation — 2026-10-02

Scope: current universe, authenticated cloud Shadow with N-leg paused; Air
remains the trading owner. Whole-service peak budget is **1,000,000,000 bytes**.
Actual CVM startup/continuous-feed acceptance is **UNKNOWN**; 24-hour observation
has **not started**. This change alone does not update the installed systemd unit.

## Implementation and equivalence

Reward SDK pages are consumed immediately. Normalized catalog rows, metadata
and intermediate direction details use process-private temporary SQLite with
a bounded page cache. Published readers keep an immutable generation; each
lookup decodes a separate value. Only complete normal/backup queue details stay
resident. Durable history, orders, sessions, pending actions and audit retention
are unchanged. Compact universe identifiers, screening summaries/reasons and
the full eligible queues still scale with their actual counts.

The filtering, ranking, freshness and UNKNOWN rules remain the same. Automated
checks compare complete output hashes with unmodified `4fbddaae061e95cbbbb3db4d5793e40e33ec2cae`:

- Six fixed-clock steps: ties, yield change, UNKNOWN, missing books, expiry,
  and reentry.
- Full traversal of 14 normal and 12 backup markets, including rejection and
  missing-book holes; all 52 outcome tokens must be visited.
- Object-lifetime checks fail on the previous retaining implementation.
  An overlapping refresh leaves old readers on their original generation.
- Candidate eviction preserves an active session, open order, position,
  UNKNOWN action and reservations; the protection tick still runs afterward.
- Guard tests cover actual v1/v2 limits, host pressure, missing evidence,
  self-only stop, bounded shutdown and no automatic restart.

## Serial offline measurements

Same dependencies and Docker environment: 1 CPU, 1 GiB memory/swap ceiling
(no additional swap), no network/credentials/profiler. Synthetic input has
18,000 markets, 40 continuously eligible markets and three full preparation/
traversal rounds one business hour apart. Each round visited all 40 eligible
tokens and prepared 18,000 histories. Every published business hash matched.

| Round | Peak RSS MiB, old → new | End RSS MiB, old → new | End cgroup MiB, old → new | Seconds, old → new |
| --- | ---: | ---: | ---: | ---: |
| Initial | 260.2 → 224.3 | 221.2 → 186.4 | 241.7 → 198.1 | 19.57 → 21.74 |
| Refresh 1 | 266.0 → 230.0 | 235.1 → 223.2 | 255.8 → 235.2 | 4.56 → 6.82 |
| Refresh 2 | 266.0 → 230.0 | 257.1 → 225.2 | 277.8 → 237.2 | 4.92 → 5.66 |

The first implementation reduced retained memory but did not reduce peak RSS
(266.0 → 267.4 MiB). Review found full direction/catalog materialization; the
row-based repair produced the table above. Disk I/O costs time. Docker did not
expose a cgroup peak counter, so the sampled cgroup values are **not peak proof**.
Synthetic results do not certify the real CVM load or 24-hour stability.

A separate 18,000-market pass forced one metadata failure and its due retry
during the same history traversal. Old/new complete business hashes matched;
all 18,000 histories and 40 candidate tokens completed, with no outstanding
preparation retry. Peak RSS was 259.9 → 226.8 MiB, end cgroup usage 257.8 →
201.0 MiB, and elapsed time 9.25 → 10.53 seconds. This uses the batched metadata
fixture on both revisions; compare within this pair, not across the two workloads.

Reproduce in each revision's isolated image using the same replay file:

```sh
PYTHONPATH=src:tests python tests/lp_memory_replay.py --markets 18000 --rounds 3
# New version: fail if any full-round result/count differs from old JSONL.
PYTHONPATH=src:tests python tests/lp_memory_replay.py --markets 18000 --rounds 3 \
  --baseline /path/to/old.jsonl
```

The replay rejects observed process/cgroup usage above 1 GB; an absent cgroup
peak remains absent evidence. Docker limits and the live systemd guard are
separate protections.

## Development evidence and failed attempts

The task was rebased without conflicts onto `525b78e056a1a2f2071696cbf567840900bfa727`.
Post-rebase focused Docker checks: **603 passed, 1 existing macOS Keychain skip**
(two workers). This report does not claim a final exact-commit service gate result.
After local commit and execution, consult `verification.json` in the operator
evidence directory for that identity/result; no Candidate/Host/Smoke claim
follows from development checks.

The first service run had **2 failed, 3233 passed, 1 skipped**. A local datetime
subclass could not be pickled; serialization now preserves its timestamp value
without its local class. The other failure was an unrelated background-thread
warning captured by a synchronous capacity test. The assertion now checks the
requesting thread and still rejects its own WARNING. Controlled foreign-warning
injection reproduced the original failure; own INFO→WARNING mutation still
fails. The repaired pre-rebase service run had **3237 passed, 1 skipped**.

Expected RED tests covered SDK/detail retention, resource caps, missing monitor
evidence, the datetime defect and full direction expansion (40 live objects). The retry
path also reloaded all reward rows; a one-market metadata retry retained 201
detail objects in its RED test. It now retains only requested reward rows;
200-market retry/UNKNOWN completion and preparation tests pass: 28 passed both
on the host and in two-worker Docker checks.
A misplaced test block and a new active-session fixture without required history
each failed once; both fixtures were repaired without weakening assertions.
An early replay stopped at the warm queue's pending count rather than full
token coverage; those measurements were discarded. A separate all-eligible
diagnostic replay was stopped and was not used as evidence. A Python 3.9 fixture
conversion and an incomplete diagnostic PYTHONPATH also failed before execution;
subsequent commands used the project's Python 3.12 environment.

Local evidence: `/Users/ray/.local/share/open-trader/issue226-memory-4fbddaae/`:
`ranking-baseline-full.json`, `backfill-baseline-full.json`, `memory-before.jsonl`,
`memory-after.jsonl` (first implementation), `memory-bounded.jsonl` (table above),
`memory-retry-before.jsonl` / `memory-retry-after.jsonl` (forced retry),
and `verification.json` (final commit/check/review identity when completed).
No real account payloads, secrets or heap dumps are included.

## Actual CVM acceptance still required

The read-only observation found about 1.10 GiB available and no swap; Prediction
was stopped and its installed unit had no memory cap. That observation is not
a future capacity guarantee. Follow [the cloud startup protocol](prediction-cloud.md#226-bounded-shadow-startup-before-the-24-hour-run):
install the reviewed unit only in an authorized release, verify effective
memory/CPU/PID limits and current host headroom, then complete first full
catalog/metadata/history preparation, candidate traversal and fresh display.

Only after that succeeds, start 24-hour observation including a 12-hour metadata
refresh. Guard stop, OOM, missing evidence or incomplete supply fails the attempt;
retain evidence and keep Shadow stopped. Do not automatically restart or affect
Air/other CVM services. Publication, merge and deployment remain separate actions.
