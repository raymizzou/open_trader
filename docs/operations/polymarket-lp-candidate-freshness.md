# LP candidate freshness and recovery

The candidate pool retains the last successful estimate for 300 seconds from
its own judgment time. This display lifetime does not authorize a BUY. Ranking
and submission check the source timestamps and current account safeguards.
`refresh_failed` retains an old estimate with its original timestamps; it does
not confirm that the estimate is executable. Expired pool rows leave the active
pool, and the existing exploration rotation continues their recovery.

## Sources and refresh ownership

| Fact | Source and owner | Candidate freshness | Execution boundary |
| --- | --- | --- | --- |
| Market status, trading rules and fees | Gamma metadata; candidate maintenance uses the fresh adapter | At most 60 seconds; maintenance refreshes from the 30-second lead | Submission reads and validates the selected market again. A 12-hour descriptive metadata cache does not extend this limit. |
| Native and sponsored reward configuration | Official reward API for the selected condition IDs; candidate maintenance | At most 60 seconds; maintenance refreshes from the 30-second lead | Recheck current reward rules before entry. Missing facts remain UNKNOWN; an explicit zero reward is not a missing response. |
| Order book | CLOB book API; candidate maintenance batches selected tokens | At most 60 seconds for candidate comparison | Entry and presend book/account checks retain the stricter 10-second limit. Source receipt time is distinct from estimate publication time. |
| Account orders, positions, balance and allowance | Authenticated complete account API; account reconciliation and maintenance | At most 60 seconds for candidate comparison | Current financial confirmation has its existing 60-second limit; presend checks retain identity, completeness, in-flight, generation, coverage and freshness fences. |
| Price history summary | Persisted official 24-hour history; the existing history preparation thread | 24 hours, or an earlier explicit expiry | Maintenance reads the stored summary without downloading history. Entry retains the final history check. |
| Competition | Official competition API; the existing dedicated refresh thread and committed cache/SQLite facts | Existing 3-hour competition window | Candidate paths use the committed facts and their true check times. They do not add a synchronous competition scan. |

These limits reflect separate decision uses. This change does not increase a
TTL or add a strategy setting. Event and reward deadlines can expire guidance
earlier than its usual freshness limit.

## Bounded requalification

Before automatic ranking excludes stale source facts, it invokes the existing
maintenance path with the IDs of the stale markets that triggered the check.
A fresh display head cannot hide a stale market whose yield is highest at the
configured BUY level. One attempt refreshes at most ten retry-eligible markets,
with one account read and one batch per due source class. It then recomputes
qualification and ranking at the configured BUY level. It does not admit a
stale candidate directly or run a synchronous whole-catalog scan.

A market-specific metadata, fee, reward or book failure retains only that
market's prior facts. Its existing retry ladder is 60, 120, then 300 seconds;
a successful requalification resets it. Healthy markets keep their own
30-second refresh lead. Account uncertainty and failure of the entire book
response retain the account/source-wide gate and backoff. The scheduler uses
both healthy source ages and failed-market deadlines, with its existing
1-to-300-second wait bounds.

If a high-yield candidate cannot refresh, a vacancy can use the next qualified
candidate. The failed candidate remains in recovery and competes again after
a successful read. A failed newcomer refresh alone does not authorize removal
of a healthy incumbent at the configured price level. Off-level removal is
owned by the separate automatic selection rules.

## Correlated diagnostics

`lp_candidate_maintenance` records `begin`, five `source` summaries (account,
metadata, fees, reward and books), and `end` under one `batch_id`. Each summary
contains at most ten markets. Market identities use a stable truncated SHA-256
correlation value. Records capture source age, read/reuse/failure outcome,
safe error category or explicit `unknown`, and the actual market retry deadline
or successful recovery. A guarded publication is distinct from a new write.

Maintenance reuses the existing 32-record queue and single diagnostic consumer.
It adds no exchange read. Overflow and output errors use the existing counters;
records can be dropped, and absence of a record is not success evidence. Logs
contain no raw exception message, credential, account or order payload. The
fresh metadata adapter can return the same read's safe failure facts to
maintenance; other callers retain its default market-only return shape.

The historical first source failure and Parker's exact historical rejection
remain unknown. These records support the next natural incident; they do not
prove that an earlier production incident has been reconstructed or recovered.
