---
status: accepted
---

# Reliable local LP instance ownership

2026-10-24, #432. Mini and Tokyo may share one wallet with distinct stable LP
instance IDs. This extends ADR0005's financial contract and ADR0007's pools.

An explicit instance binds its local database to account, venue and stable ID.
Reliable immutable provenance records the complete signed EIP712 order ID and
exact terms before POST. Failed or uncertain registration cannot POST. A restart
loads identities only; it does not reconstruct requests, plans or send authority.
Removing or changing a bound identity fails closed. A fresh unbound database
without the field retains the prior single-instance behavior.

Only positively local exact orders may enter management, protection, rotation or
cancellation. All four native cancellation boundaries recheck local identity.
Manual, legacy and other nonlocal orders are not adopted, rewritten or canceled.
The state reports their IDs and lack of automatic local protection. A missing
local record does not establish peer ownership.

Ownership UNKNOWN and financial UNKNOWN are separate. A complete known nonlocal
BUY does not by itself forbid a new local BUY. All wallet BUY notional remains
reserved: with budget100, local20 and nonlocal40, remaining capacity is40. Current
wallet identity, source completeness/freshness, unknown terms/positions/pending
requests, funds, allowance, and SELL oversell protections retain their meaning.
Positions and SELLs do not deduct the BUY cap. Independent hosts do not gain an
atomic cross-host spending guarantee.

The optional parallel gate proves distinct stable IDs, the same wallet, supported
implemented ownership policy and exact source/runtime/config identities. It
observes the existing local Mini service and uses the existing Tokyo SSH read
path. Readiness validates staged Tokyo identity and same-host absence; smoke
validates both actual running identities. It does not require complete peer order
attribution or introduce a peer provenance transport. The default handoff still
requires the old owner to stop. Missing, stale or mismatched proof blocks.

Compatible Mini rollout comes first. Legacy order disposition remains explicit.
Geographic refusal remains an operational blocker; no bypass is approved. Offline
code/tests do not establish deployment, runtime health or issue acceptance.
