# Predict.fun cross-venue YES/NO arbitrage design

Date: 2026-08-02
Status: approved conversational design; implementation not started
Branch: `docs/predict-cross-venue-design`

## Goal

Add Predict.fun as a second prediction-market information source and discover
strict YES/NO arbitrage pairs between Predict.fun and Polymarket. Preserve the
existing Polymarket same-venue YES/NO behavior and existing LLM threshold-hedge
behavior.

The first version is observation-only for Predict.fun mainnet. It may discover,
validate, monitor, retain, and notify on cross-venue signals, but it must not
submit a Predict.fun mainnet order.

## Product boundary

### In scope

- Two cross-venue directions:
  - buy Predict.fun YES and Polymarket NO;
  - buy Polymarket YES and Predict.fun NO.
- Explicit Predict.fun-to-Polymarket condition-ID mapping.
- A new, separate Codex equivalence check for strict cross-venue binary events.
- Background candidate discovery followed by real-time monitoring of approved
  pairs.
- A shared exchange-account header used by both existing strategy tabs.
- A five-stage cross-venue funnel embedded in the existing YES/NO page.
- Venue-qualified legs in current monitoring rows, signal history, API output,
  logs, and stored evidence.
- One explicitly invoked minimum-size BNB Testnet order canary after credentials
  are available.

### Out of scope

- Predict.fun same-venue YES/NO scanning.
- Predict.fun mainnet order submission or automatic cross-venue execution.
- Replacing or modifying the existing Polymarket threshold-relation prompt.
- LLM or Codex calls in the real-time arbitrage calculation path.
- Title-based all-pairs matching, preclassification, embeddings, or a generic
  venue-adapter framework.
- Foreign-exchange pricing between settlement assets.
- Changing the existing 15% annualized entry rule, which is owned by separate
  work.
- Redesigning the prediction-market page, its navigation, history interactions,
  or existing Polymarket execution flow.

## Canonical terms

| Term | Meaning |
| --- | --- |
| Venue | `polymarket` or `predict.fun`. It is mandatory on every market and order leg. |
| Predict market ID | Predict.fun numeric REST identifier (`id`). |
| Predict condition ID | Predict.fun native `conditionId`, used only for Predict.fun. |
| Polymarket condition ID | Polymarket-native condition identifier. A value in Predict.fun `polymarketConditionIds[]` is treated as an external reference to this ID. |
| Explicit market pair | One Predict.fun market and one resolved Polymarket market linked by an explicit `polymarketConditionIds[]` value. |
| Candidate monitor | Low-frequency background observation before Codex approval. It is not the real-time WebSocket pool. |
| Approved pair | An explicit market pair whose strict binary equivalence has passed the new Codex check and deterministic post-checks. |
| Arbitrage-space pair | An approved pair whose live books currently produce positive net value before the final actionability gate. |
| Clear order signal | A pair that remains valid after both REST books are refreshed, books are fresh, fees/depth are included, and the existing annualized entry gate passes. In this phase it is an economic signal, not authorization to submit a Predict.fun order. |

## Identifier and mapping rules

Predict.fun identifiers are not interchangeable:

- `id` is the numeric Predict.fun API market ID.
- `conditionId` is the Predict.fun-native on-chain condition hash.
- every element of `polymarketConditionIds[]` is a candidate Polymarket
  condition-ID reference.

The first version processes only markets with a non-empty
`polymarketConditionIds[]` array. It iterates every supplied reference and:

1. queries Gamma with repeated `condition_ids` parameters for both
   `closed=false` and `closed=true`;
2. falls back to `GET https://clob.polymarket.com/markets/{condition_id}` when
   Gamma has no row;
3. requires the returned Polymarket condition ID to equal the requested
   reference;
4. keeps the Predict.fun and Polymarket native IDs as separate, venue-qualified
   values;
5. skips unresolved references and markets with no explicit mapping.

There is no title search or preclassification fallback in this version. The
system records enough coverage counters to decide later whether mainnet mapping
coverage justifies a second discovery method.

The 2026-08-02 read-only Predict.fun testnet sample supports this choice:

- 100 open Predict.fun markets inspected;
- 59 markets supplied 59 unique external references;
- 53 references resolved through Gamma;
- the remaining six resolved through the direct Polymarket CLOB route;
- all 59 requested IDs resolved, and none equalled the Predict.fun-native
  `conditionId`;
- 41 markets had no explicit mapping and would be skipped.

This sample proves the mapping mechanism, not Predict.fun mainnet coverage.
Mainnet coverage remains unknown until the API key is available.

Primary references:

- [Predict.fun Market schema](https://dev.predict.fun/market-14037477d0)
- [Polymarket public client methods](https://docs.polymarket.com/trading/clients/public)
- [Polymarket Gamma market filters](https://docs.polymarket.com/api-reference/markets/list-markets)

## Architecture

### Reuse existing runtime ownership

The Dashboard process remains the sole runtime owner. It starts one additional
isolated async Predict.fun task alongside the existing `PolymarketMonitor`.
There is no new daemon, service manager entry, generic adapter, or second
database.

The existing concrete `PolymarketMonitor` remains unchanged as the Polymarket
source. Add one concrete `PredictSource` responsible only for:

- Predict.fun REST catalogue and market detail requests;
- Predict.fun WebSocket book updates;
- source-specific authentication and health;
- normalization into venue-qualified market/book records.

The cross-venue matcher consumes the concrete source outputs. It does not own
credentials and cannot submit orders.

### Slow path and hot path

The runtime deliberately separates discovery from arbitrage monitoring:

1. The background discovery task loads Predict.fun markets.
2. Explicit Polymarket references are resolved.
3. Cheap deterministic eligibility checks remove unusable markets.
4. The new Codex equivalence check runs outside the real-time watcher.
5. Only approved pairs enter the real-time WebSocket subscription pool.
6. WebSocket updates perform local Decimal book/depth/fee calculations only.
7. A positive live candidate triggers concurrent REST book refreshes from both
   venues.
8. A clear signal opens only if the refreshed books still pass all gates.

`正在监视` in the UI means the low-frequency candidate monitor at step 3. It
does not mean the pair is already subscribed to the real-time WebSocket pool.
Codex approval precedes real-time subscription, so Codex latency never occupies
the arbitrage window.

### Signal episodes

- Allow at most one in-flight REST confirmation per pair.
- Allow at most one open signal episode per pair/direction.
- Do not repeat notifications while an episode remains open.
- Close an episode when its edge disappears, either book becomes stale, source
  health fails, or equivalence evidence is invalidated.
- Rearm only after a fresh crossing from closed to valid.
- Reject out-of-order updates and fail closed during reconnects.

## Cross-venue equivalence check

Add a new prompt, schema, and cache namespace named
`cross-exchange-yes-no-equivalence-v1`. Do not modify or reuse the existing
`polymarket-threshold-relation-v1` prompt.

### Input

For both venue markets, provide:

- exchange, native market ID, and native condition ID;
- question and YES/NO outcomes;
- full resolution rules and cited rule source;
- close, expiry, and expected settlement timestamps;
- mapping evidence and rule fingerprints.

### Approval rule

Approve only when both markets represent exactly the same binary event. The
response must establish that these cross-venue divergent states are impossible:

- Predict.fun YES while Polymarket NO resolves as winner;
- Predict.fun NO while Polymarket YES resolves as winner.

Ambiguous wording, different cut-off times, different data sources, discretionary
resolution, missing rules, unsupported outcomes, or unresolved uncertainty must
return a fail-closed result.

### Structured result

The result carries:

- decision: `APPROVE` or `REJECT`;
- concise reason;
- venue-qualified IDs for both markets;
- evidence for both resolution rules;
- rule fingerprints;
- deterministic checks for outcomes, dates, and identifier consistency.

The deterministic post-check rejects malformed output, missing evidence,
exchange/ID mismatch, stale fingerprints, or any unsupported decision value.

## Price and signal calculation

For each approved pair, calculate only:

- Predict.fun YES plus Polymarket NO;
- Polymarket YES plus Predict.fun NO.

Use Decimal arithmetic and executable ask depth, not displayed midpoint or best
bid. Include known fees and compute the maximum common fill quantity across both
legs. Report:

- venue, outcome, token ID, condition ID, settlement asset, executable price,
  depth, fees, and book timestamp for each leg;
- combined maximum cost;
- minimum nominal payout;
- minimum net profit;
- both expected settlement times;
- inputs required by the existing annualized-entry gate.

The first version treats supported settlement assets as nominally 1:1 for this
calculation and displays each asset explicitly. It does not fetch or apply an FX
rate. This is an accepted simplification because the existing 15% annualized
gate dominates small settlement-asset basis differences. Revisit only if
observed basis becomes material relative to that gate.

## Health and failure behavior

Health is source-specific and strategy-independent. The shared header exposes
every configured venue, including unavailable venues.

For each venue show:

- venue name and chain/network where useful;
- REST and WebSocket state;
- masked public account/wallet address;
- available balance with its returned asset label;
- trading mode such as `可以交易`, `只读`, or `API Key 待分配`;
- last successful update or concise failure reason.

Do not sum balances across different assets.

Failure isolation rules:

- a Predict.fun failure closes cross-venue signals but does not degrade or stop
  existing Polymarket same-venue behavior;
- a missing Predict.fun API key is `pending`, not a Polymarket outage;
- 401/403 responses stop rapid retries and expose an authentication state;
- 429 and transient network failures use bounded backoff;
- stale REST/WS data, reconnect gaps, and rule-fingerprint changes fail closed;
- a fingerprint change immediately invalidates approval and removes the pair
  from real-time subscription until revalidated.

## Dashboard design

The design is a minimal change to the current prediction-market page and must
reuse its existing `.pm-*` visual language, spacing, cards, responsive behavior,
and interactions.

### Shared exchange header

Replace the Polymarket-specific readiness presentation with one shared exchange
account-and-connection header above the existing strategy tabs. YES/NO and LLM
threshold-hedge views render the same header.

Each configured venue gets one compact card containing connection state,
masked wallet, asset-qualified balance, and trading mode. Cards use the current
border/radius/surface styles and wrap when more venues are configured. An
unavailable configured venue remains visible with the truthful reason.

### Strategy tabs

Keep exactly the existing two tabs:

- `YES/NO套利`
- `LLM对冲套利`

Do not add a separate cross-venue tab.

### YES/NO funnel

Add one five-stage cross-venue funnel below the strategy tabs using the existing
funnel panel and stage styles:

1. `两所对应标的`: explicit Predict.fun/Polymarket market pairs resolved;
2. `正在监视`: pairs in the low-frequency candidate monitor;
3. `Codex 认为可以`: strict-equivalence-approved pairs, eligible for real-time
   WebSocket subscription;
4. `有套利空间`: approved pairs with positive live book value;
5. `明确下单信号`: pairs that survive dual REST confirmation and the existing
   final entry gate.

Every count is a market-pair count, not a one-venue market or token count.

Remove the existing four summary cards above the funnel because their content
duplicates the funnel or lower history:

- `当前可参与` is represented by stage 5;
- `监控事件` is represented by stage 2;
- `市场 / Token` belongs in diagnostic detail;
- `过去 24 小时信号` remains in signal history.

### Existing monitoring and signal areas

Keep the existing two-column monitoring/history layout and all current
interactions. Same-venue Polymarket and cross-venue rows appear in the same
YES/NO list. Each row and expanded leg must explicitly show the venue:

- `Predict.fun · YES`;
- `Polymarket · NO`;
- `Polymarket · YES`;
- `Predict.fun · NO`.

A cross-venue stage-5 signal may be displayed and notified during the read-only
phase, but it has no Predict.fun execution button. Existing Polymarket-only
execution behavior is unchanged.

## Data and persistence

Reuse the existing prediction-arbitrage store and API endpoints where their
shape already supports the feature. Add only the source-qualified fields needed
to prevent ID collision and render truthful status.

At minimum, every persisted market/leg/evidence reference carries:

- `exchange`;
- native `market_id`;
- native `condition_id`;
- outcome;
- token ID when supplied;
- settlement asset.

Cross-venue pair identity must be deterministic from both venue-qualified
market identities and direction. Never key records by an unqualified condition
ID or assume native IDs match across venues.

Retain counters for:

- Predict.fun markets seen;
- markets with explicit mapping;
- unique external references;
- Gamma resolutions;
- CLOB fallback resolutions;
- unmapped and unresolved skips;
- candidate-monitored pairs;
- Codex-approved/rejected/pending pairs;
- real-time subscribed pairs;
- arbitrage-space pairs;
- clear signals and notifications.

## Credentials and security

- Store the Predict.fun API key and any signing secret only in macOS Keychain.
- Never place API keys, JWTs, signatures, private keys, or seed phrases in chat,
  repository files, logs, API responses, or screenshots.
- The supplied Predict.fun public wallet/deposit address is
  `0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435`; UI display is masked as
  `0xcE23…f435`.
- Sanitize authentication failures and request metadata before persistence or
  display.
- Read-only mainnet discovery must not require loading a signing key.

## Testnet canary

After credentials are available, provide a separate explicitly invoked CLI
canary for one minimum-size BNB Testnet order. It is not called by the Dashboard,
normal monitoring, or `make acceptance`.

The canary must:

- be hard-bound to chain ID 97 and testnet API endpoints;
- have no mainnet fallback;
- select a standard binary, non-NegRisk market supported by the testnet API;
- submit one minimum-size FOK limit buy;
- prove API acceptance, transaction submission, successful transaction status,
  and expected balance/position delta;
- print only sanitized identifiers and results.

No mainnet Predict.fun canary is part of this scope.

## Verification and acceptance

### Automated checks

- identifier separation and venue-qualified keys;
- repeated Gamma `condition_ids` plus CLOB fallback;
- empty/unresolved mapping skips and coverage counters;
- independent calculations for both cross directions;
- Decimal prices, common depth, fees, payout, profit, and settlement times;
- separate Codex prompt/schema/cache namespace;
- strict-equivalence approval and fail-closed post-checks;
- Codex approval before real-time subscription;
- one in-flight confirmation and one signal episode per pair;
- stale, out-of-order, reconnect, 401/403, 429, and fingerprint invalidation;
- shared-header venue states and explicit exchange labels in rows/history;
- no Predict.fun mainnet submit route reachable from Dashboard behavior.

### Direct workflow checks

- run mainnet read-only catalogue/mapping coverage after the API key arrives;
- verify at least one approved pair can receive both venue books without Codex
  in the hot path;
- force a positive candidate and prove dual REST refresh is required before a
  signal opens;
- disconnect Predict.fun and prove cross signals close while Polymarket
  same-venue monitoring remains healthy;
- explicitly invoke the one-time testnet canary and preserve sanitized proof.

### Dashboard gate

Use focused tests and direct workflow checks during implementation. Run
`make acceptance` only as the final Dashboard gate. Only `PASS` is review-ready.
Missing required mainnet API access, browser access, or live data is `BLOCKED`,
not a substitute pass. After `PASS`, redeploy the exact accepted Git SHA and
verify PID, working directory, SHA, fresh logs, and HTTP 200 before review.

## Accepted simplifications

- Explicit external mappings only; no preclassification until measured mainnet
  coverage proves it necessary.
- Concrete `PredictSource`; no one-implementation adapter hierarchy.
- Same Dashboard process and existing store; no new daemon or database.
- Nominal 1:1 settlement-asset treatment; revisit only with material observed
  basis.
- Predict.fun mainnet is read-only; execution remains future scope.

## Completion criteria

The feature is complete only when:

- the shared exchange header truthfully reports each configured venue;
- the five funnel counts have the documented semantics;
- explicit mapped pairs are validated before real-time subscription;
- both cross directions are calculated from fresh executable books;
- clear signals are retained without duplicate episodes;
- Predict.fun failures cannot break existing Polymarket behavior;
- Predict.fun mainnet submission remains impossible in this scope;
- automated, direct-workflow, process, and final Dashboard acceptance evidence
  meet the repository gates.
