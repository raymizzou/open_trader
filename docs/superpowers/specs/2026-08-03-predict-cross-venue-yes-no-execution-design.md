# Predict.fun × Polymarket Cross-Venue YES/NO Execution Design

**Date:** 2026-08-03

**Status:** Approved; exact-approval and testnet-canary amendment approved 2026-08-03

**Target:** Existing Open Trader prediction-market watcher, execution service, and Dashboard

## 1. Goal

Extend the existing YES/NO workflow from Polymarket-only observation and
execution to protected manual execution across Predict.fun and Polymarket.

The finished path is:

1. discover a Predict market and its explicit Polymarket candidate IDs
2. collect both venues' complete rules and timing metadata
3. let Codex approve only direct, outcome-equivalent market pairs
4. admit approved pairs to the real-time watcher
5. detect either cross-venue complementary direction
6. require at least 15% theoretical simple annualized yield after deterministic
   fees and costs
7. show one manual confirmation with explicit venue labels and price ceilings
8. concurrently submit two bounded FOK legs
9. independently reconcile both venues and remediate a one-leg incident within
   a separate 2 USDT loss budget
10. retain the positions through resolution and reuse the existing automatic
    redemption path

This change never adds unattended automatic entry. A human confirmation is
required for every cross-venue execution.

## 2. Existing Components to Reuse

The implementation extends the existing path rather than creating another
execution system:

- `PredictSource` remains the Predict market, book, status, and balance source.
- `CodexCrossVenueEquivalenceValidator` remains the background semantic
  validator.
- The existing cross-venue monitor remains responsible for candidate state,
  book monitoring, opportunities, signal history, and Dashboard snapshots.
- `PredictionExecutionService` remains the owner of preview, execution locking,
  order state, reconciliation, incident handling, remediation, circuit breaking,
  and audit history.
- The existing Polymarket submitter and automatic redemption path are reused.
- A minimal Predict-specific submit/reconcile adapter is added behind the
  existing execution service; no second orchestration service, generalized
  exchange framework, or new job scheduler is introduced.

The existing standard same-venue execution functions and LLM-hedge behavior
must remain intact for regression safety. The merged YES/NO workspace and new
notifications list only cross-venue opportunities.

## 3. Scope and Non-Goals

### 3.1 In scope

- Predict.fun and Polymarket direct-polarity binary pairs
- both complementary directions:
  - Predict YES + Polymarket NO
  - Polymarket YES + Predict NO
- background Codex rule and cutoff validation
- approved-pair real-time monitoring
- cross-venue signal persistence and Feishu notification
- one protected manual preview and confirmation
- concurrent bounded FOK submission
- independent REST reconciliation
- existing completion, unwind, breaker, and redemption behavior
- shared multi-venue Dashboard health header
- five-stage cross-venue YES/NO funnel
- desktop and mobile confirmation UI

### 3.2 Out of scope

- same-venue YES + NO arbitrage as a product focus
- inverted proposition matching such as `X happens` versus `X does not happen`
- compound, multi-outcome, or Negative Risk equivalence
- global semantic search when Predict provides no Polymarket candidate ID
- automatic entry without a human confirmation
- multiple concurrent cross-venue executions
- a live pUSD/USDT foreign-exchange feed or stablecoin basis haircut
- a second redemption implementation
- a new exchange abstraction, workflow engine, queue service, or database
  solely for future venues
- browser-side book fetching, matching, economics, or risk decisions
- a Dashboard, watcher, or production-config switch between Predict testnet and
  mainnet
- an automatic mainnet order canary in `make acceptance`

## 4. Candidate Discovery and Funnel Semantics

### 4.1 Candidate identity

Predict and Polymarket condition IDs are not expected to be equal.
`polymarketConditionIds` supplied with a Predict market is an explicit candidate
link, not proof that the contracts are equivalent.

V1 uses only these explicit candidate IDs. A Predict market without a candidate
ID remains outside the cross-venue funnel. There is no full-catalog semantic
scan or pre-classification system in V1.

The read-only mainnet research supporting this choice found candidate IDs for
95 of the first 100 open Predict default markets, while also proving that time
metadata can differ and therefore still requires semantic validation. The
evidence is recorded in
`docs/research/2026-08-03-predict-settlement-time.md`.

### 4.2 Five funnel stages

The YES/NO page uses exactly these five stages:

1. **两所对应标的** — explicit Predict-to-Polymarket candidate pairs resolved
2. **正在监视** — low-frequency candidate observation and Codex queue; not yet
   in the real-time arbitrage WebSocket pool
3. **Codex 认为可以** — rule-equivalent pairs approved and admitted to the
   real-time watcher
4. **有套利空间** — approved pairs whose current complementary books produce
   positive nominal economics
5. **可下单明确信号** — fresh dual-REST confirmation, annualized threshold,
   capacity, balance, and risk gates all pass

Codex is never called from stages 4 or 5. The real-time watcher operates only on
already-approved pairs.

## 5. Codex Pair Admission

### 5.1 Input

Deterministic code supplies Codex with:

- both exchange names
- both native market and condition IDs
- direct YES and NO token identities
- complete question and rule text from each venue
- resolution source and invalid/cancellation behavior
- Predict category `startsAt` and `endsAt`
- Polymarket timing metadata
- rules and metadata fingerprints

Market text is untrusted input. The validator cannot follow instructions in the
market content, call tools, browse, or use outside facts.

### 5.2 Approval rule

Codex returns `APPROVE` only when the complete contracts prove all of the
following:

- Predict YES maps directly to Polymarket YES
- Predict NO maps directly to Polymarket NO
- both divergent settlement states are impossible under the written rules
- resolution sources, cancellation behavior, invalid-market behavior, and edge
  cases cannot produce a conflicting result
- a canonical UTC event cutoff is supported by verbatim evidence from both
  rule texts

Raw metadata timestamps do not need to be exactly equal. A metadata mismatch
may pass only when both complete rule texts unambiguously imply the same
canonical cutoff. A fixed tolerance is not used.

Missing, partial, contradictory, or ambiguous rule text produces `REJECT`.
Inverted or compound propositions produce `REJECT` in V1 even when a human
could construct a logical mapping.

### 5.3 Structured result and cache

The validator's structured output contains:

- decision and concise summary
- both market identities and fingerprints
- direct outcome mapping
- canonical UTC cutoff
- verbatim evidence from each venue
- divergent-state findings
- uncertainties
- prompt version and model audit metadata

The current requirement that returned raw `close_at` and `settlement_at` values
exactly equal the source values is removed. Backend validation instead requires
valid identities, current fingerprints, direct-polarity mapping, parseable
canonical cutoff, and evidence belonging to the supplied rules.

Approval is keyed by both rules and timing fingerprints. Any source rule,
timing metadata, outcome identity, or prompt-version change immediately removes
the pair from the real-time pool and returns it to Codex review.

Codex approval allows monitoring only. It never authorizes a trade.

## 6. Event End, Resolution, and Annualization

Predict's scheduled end time is on the category reached through
`categorySlug`, not on the market itself. `Category.endsAt` is normalized as
`event_end_at` and remains distinct from resolved and redeemable state.

For an approved pair:

- Codex supplies the evidence-backed canonical cutoff.
- The existing simple annualized function uses the later of the two
  Codex-validated contractual cutoffs. An approved pair should normally reduce
  both to the same canonical instant; raw metadata does not extend or shorten
  that instant by itself.
- The UI labels this result as theoretical simple annualized yield.
- No arbitrary settlement-delay buffer is added because Predict exposes no
  guaranteed redeemable timestamp.
- Actual capital remains unsettled until redemption, regardless of the
  theoretical annualization timestamp.

The cross-venue entry gate is:

- nominal profit after deterministic fees and calculable gas must be positive
- theoretical simple annualized yield must be at least 15%

There is no separate price-spread or absolute-profit threshold for this
cross-venue path.

## 7. Economics and Position Sizing

### 7.1 Collateral convention

Predict USDT and Polymarket pUSD are treated as equal to one USD and equal to
each other. Stablecoin basis, depeg, and conversion costs are intentionally
ignored by product policy.

### 7.2 Net redeemable units

Requested quantity and exchange-reported `amountFilled` are not sufficient.
The strategy aligns the net redeemable outcome units after deterministic fees.

Before preview:

- Predict's fee and net-share result must be deterministically computable
- both legs must produce equal net redeemable outcome units
- maximum fees and calculable gas must be included in economics and caps
- if Predict cannot provide a deterministic pre-trade result, the opportunity
  is observation-only

After fill, the service reads real positions from both venues and reconciles
actual redeemable units. A mismatch enters remediation.

A residual below the venue's minimum tradable amount may remain as
`dust exposure` only when its worst-case loss plus every other remediation cost
fits within the 2 USDT emergency budget. Dust is shown and audited as unhedged;
it is never presented as a complete hedge.

### 7.3 Entry and portfolio caps

- Normal entry maximum: **20 USDT equivalent**, including fees and calculable
  gas.
- Emergency remediation budget: a separate **2 USDT worst-case loss** limit.
- Maximum incident capital impact: **22 USDT equivalent**.
- Total unsettled cross-venue principal: **100 USDT equivalent**.
- Available venue balance has no product upper limit; each leg only requires
  sufficient current balance and the ability to create its exact market-scoped
  allowance after confirmation.
- The existing 65 pUSD wallet-balance ceiling does not apply to the new
  cross-venue path and remains unchanged for legacy paths.

The 2 USDT limit bounds automatic incident remediation, not all market or venue
risk. The normal entry principal can still be lost if the venues actually
resolve divergently, fail, or violate the accepted 1:1 collateral convention.
The maximum budget placed at risk by one incident remains 20 USDT entry plus
2 USDT remediation.

Before submitting either leg, the service atomically reserves the proposed
maximum total entry cost against the 100 USDT unsettled cap. The cap includes:

- submitted but not conclusively reconciled orders
- filled cross-venue positions not yet redeemed
- unresolved residual exposure after remediation

The reservation is released only when both legs conclusively fail without a
position or when the corresponding capital is actually redeemed.

V1 permits only one active cross-venue execution at a time. Other approved
opportunities remain monitored while the execution lock is held.

### 7.4 Predict account readiness and exact approval

Predict readiness does not require a persistent or unlimited USDT allowance.
An allowance of zero is the expected safe state before and after an execution.
The readiness path must not use the SDK's "fully approved" Boolean, which is
defined for persistent maximum approval. Exact allowance checks read the raw
owner/spender amount and compare it with the one bounded debit.
Before exposing an actionable preview, the server checks:

- API/JWT authentication and Predict-account identity
- available USDT
- the Privy signer and Predict deposit-address relationship
- enough native BNB for one approval transaction and a bounded cleanup
  transaction
- one SDK approval step derived from the selected market's real
  `isNegRisk`, `isYieldBearing`, and BUY side

V1 still admits only standard, non-NegRisk, non-yield-bearing binary markets.
The market-derived scope is retained so a future scope expansion cannot reuse
the wrong exchange spender accidentally.

After human confirmation, the Predict leg follows this sequence:

1. refresh both venues and compute the final bounded Predict debit
2. set the Predict USDT allowance to exactly that debit and wait for its receipt
3. refresh both venues again because approval consumes time
4. if all confirmed ceilings and gates still pass, submit both FOK legs
   concurrently
5. reconcile the Predict order, position, and remaining allowance

If step 2 fails, neither venue order is submitted. If approval succeeds but the
post-approval refresh fails, the service resets the allowance to zero before
releasing the execution lock. A failed reset opens the cross-venue breaker and
alerts; it never proceeds to either order. Any nonzero allowance left after a
conclusive fill or failure is also reset to zero before the execution is closed.

### 7.5 Testnet verification boundary

The existing Predict testnet account is an EOA, while production uses a Predict
Account controlled by a Privy signer. Testnet therefore validates only the
shared adapter path: market and book reads, quote math, caps, exact allowance,
signed order submission, FOK behavior, and REST/position reconciliation.

An explicit operator-only testnet canary command injects BNB Testnet, the
testnet API, and the dedicated EOA Keychain credential into the same trading
adapter methods used by production. The Dashboard, watcher, normal runtime
configuration, and `make acceptance` remain mainnet-only. The canary is never
started automatically. It defaults to no-submit; a mutation requires an
explicit submit flag after showing the market, outcome, quantity, and maximum
test-USDT debit. The hard canary cap is 1 test USDT.

Predict-Account-specific behavior remains covered by:

- deterministic tests proving the mainnet builder receives the Privy signer and
  `predict_account=<deposit address>`
- mainnet read-only JWT, balance, market, book, approval-step, and signed
  no-submit checks
- the already completed bounded mainnet canary that proved exact approval via
  the SDK's Smart Account/Kernel route, filled order, position, receipts, and
  zero remaining allowance

A new mainnet order canary is required only after an SDK upgrade, signer/account
change, Smart Account/Kernel-path change, or expansion to a new market type. It
always requires separate operator authorization.

## 8. Preview and Human Confirmation

### 8.1 Preview

The existing preview boundary receives a cross-venue opportunity ID and builds
one immutable preview containing:

- pair ID and current pair fingerprints
- Codex approval ID, time, summary, canonical cutoff, and evidence
- Predict leg and Polymarket leg with explicit venue labels
- direct BUY outcome, net quantity, FOK strategy, maximum price, maximum cost,
  fee, and settlement asset for each leg
- combined maximum cost, minimum nominal payout, minimum profit, and
  theoretical annualized yield
- available balance on each venue
- current and post-reservation unsettled principal
- normal 20 USDT and emergency 2 USDT limits
- preview expiry and a unique execution ID

The operator is not required to reread both full contracts on every trade.
The modal shows the Codex result and canonical cutoff with expandable evidence.
The human confirmation authorizes only the displayed price ceilings, total
cost, and incident budget.

### 8.2 Confirmation refresh

Immediately after the user confirms and before submitting either order, the
service refreshes by REST:

- both rules and fingerprints
- both market and outcome statuses
- both books and fee facts
- both balances, current allowance state, and exact-approval prerequisites
- Codex approval validity
- execution lock, circuit breaker, and unsettled reservation

The system never silently raises a confirmed price ceiling.

- If both refreshed orders remain within the confirmed price ceilings and all
  gates pass, execution continues.
- If either price exceeds its ceiling, a new preview and a new confirmation are
  required.
- If annualized yield falls below 15%, depth becomes insufficient, a fingerprint
  changes, or any readiness fact fails, the execution is cancelled with no
  order submitted.

## 9. Order Submission, Idempotency, and Reconciliation

### 9.1 Concurrent bounded submission

After confirmation, the execution service completes the exact Predict approval
and then performs the required post-approval refresh. Only after that refresh
passes does it submit the Predict and Polymarket FOK legs concurrently to
minimize naked-leg duration. This is not an atomic transaction; either venue
may succeed while the other fails.

Both submissions use the same local execution ID and venue-specific client
identity where supported. Repeated UI confirmation for the same preview maps to
the same execution ID.

### 9.2 Unknown submission state

A timeout or transport error never causes a blind order retry.

1. Query venue order and account state using the known identity.
2. If the original order is confirmed created, continue tracking it.
3. If the original order is conclusively absent, one bounded retry may be
   allowed by the existing execution state machine.
4. If creation remains unknown, open the global cross-venue breaker, alert, and
   prohibit new execution.

### 9.3 Final REST reconciliation

WebSocket state may discover an opportunity but cannot prove execution. The
service independently reconciles through both venues' REST/account surfaces:

- order creation and final order status
- fill price and paid fees
- actual position and net redeemable units
- wallet balance and allowance changes
- Predict activity/receipt state where applicable

Only reconciled venue state advances the execution record.

## 10. Incident Handling and Circuit Breaker

If both legs fill in the approved net quantities, the execution becomes
`holding_to_resolution` and remains part of unsettled principal until redeemed.

If one leg fills and the other fails, the existing completion/unwind policy is
reused:

- calculate a bounded FOK completion or unwind action from current executable
  books
- include fees, slippage, calculable gas, and dust loss
- submit remediation only when the complete worst-case loss is guaranteed not
  to exceed 2 USDT
- never market-chase beyond the bound

If the 2 USDT limit cannot be guaranteed, the service does not attempt automatic
remediation. It opens the breaker and alerts with the unreconciled exposure.

Unknown order state, single-leg exposure, reconciliation failure, or a
remediation failure pauses **all** new Predict × Polymarket executions. Existing
orders continue to be reconciled; read-only monitoring remains active.

The breaker can be cleared only after a fresh full-account reconciliation shows
a safe known state and the operator explicitly acknowledges the incident.

## 11. Settlement and Redemption

No new redemption subsystem is added.

- The execution remains unsettled until actual redemption.
- Existing automatic redemption is reused once the venue reports the winning
  position redeemable and the account position is independently confirmed.
- Failed redemption records `待兑付`, alerts, and retains the unsettled
  reservation.
- Redemption failure does not trigger repeated blind transactions.
- Principal is released from the 100 USDT cap only after the redeemed collateral
  is observed in the account.

## 12. Notification Policy

Only a transition into funnel stage 5, `可下单明确信号`, schedules a cross-venue
Feishu notification. Stages 1 through 4 remain Dashboard-only observations.

Notification deduplication keys on:

- pair ID
- complementary direction
- both current rules fingerprints

The notification contains both venue/outcome legs, maximum cost, minimum
profit, theoretical annualized yield, canonical cutoff, and a Dashboard deep
link. Opening the link never submits an order; it only opens the current signal,
which must still pass preview and manual confirmation.

Notification delivery remains asynchronous and cannot affect signal
persistence, actionability, preview, or execution. Acceptance uses a
deterministic notifier and sends no real Feishu message.

## 13. Dashboard Design

The approved UI preserves the current warm Open Trader Dashboard visual
language, spacing, typography, density, borders, and navigation.

### 13.1 Shared venue health header

The existing prediction page header becomes shared by the YES/NO and LLM hedge
tabs and displays one card per venue:

- venue name and trading mode
- REST and WebSocket state
- region/API readiness where applicable
- masked wallet address
- available balance and asset
- most recent successful update

At minimum it shows Polymarket and Predict.fun. It replaces the older
Polymarket-only page heartbeat wording. There is no aggregate wallet-balance
ceiling card.

### 13.2 YES/NO workspace

The YES/NO workspace contains, in order:

1. shared venue health header
2. existing strategy tabs
3. five-stage cross-venue funnel
4. one concise cross-venue policy note
5. cross-venue candidate list
6. existing signal, execution, and incident history

The four metric cards formerly shown above the funnel are removed because they
duplicate the funnel.

Every candidate and every leg explicitly names its exchange. Actionable rows
show net units, combined maximum cost, minimum payout, minimum profit,
theoretical annualized yield, canonical cutoff, and Codex status. Observation
rows remain visible even when below the entry threshold or pending review.

### 13.3 Confirmation modal

Desktop keeps the existing 720px modal pattern and presents the two legs side by
side. Mobile uses the same content in one vertical sequence with a fixed action
footer. Both show:

- Predict and Polymarket legs with outcomes and price ceilings
- combined economics and theoretical annualized yield
- non-atomic warning and 2 USDT remediation authorization
- current Codex approval and expandable evidence
- canonical cutoff
- both available balances
- current and post-trade unsettled principal
- refresh/reconfirm behavior
- existing automatic redemption behavior

The confirmation button states the exact maximum combined cost. Visible action
targets remain at least 44px high and the modal remains keyboard dismissible.

## 14. Security and Secrets

Predict credentials remain in macOS Keychain under the existing service names:

- service `com.open-trader.predict`, account `api-key`
- service `com.open-trader.predict`, account `privy-private-key`

The operator-only EOA testnet canary uses its existing isolated Keychain labels:

- service `com.open-trader.predict-testnet-canary`, account `eoa-private-key`

Mainnet and testnet credentials are never interchangeable or selected by a
Dashboard/runtime environment switch.

Credentials are never included in source, configuration JSON, logs, snapshots,
Codex prompts, Feishu messages, screenshots, test fixtures, or this design.

The API key authenticates Predict API requests. The Privy private key signs only
the exact bounded order or redemption action owned by the execution service.
Public wallet addresses may be masked in UI and logs.

## 15. Failure Behavior

| Failure | Required behavior |
| --- | --- |
| Candidate ID missing | Exclude from V1 cross-venue funnel; do not global-scan. |
| Full rules missing or ambiguous | Codex rejects; observation only. |
| Direct polarity not proven | Reject; inverted matching is out of scope. |
| Rules/timing fingerprint changes | Remove real-time admission and requeue Codex. |
| Canonical cutoff unavailable | No annualization and no execution. |
| Predict net units or fee unavailable | Observation only. |
| Book or REST state stale | Remove stage-5 actionability. |
| Price exceeds confirmed ceiling | Cancel; require a new preview and confirmation. |
| Balance, native gas, or exact-approval capability insufficient | Cancel before approval or either submit. |
| Exact approval succeeds but the post-approval refresh fails | Submit neither order; reset allowance to zero. Reset failure opens the breaker and alerts. |
| 20 USDT entry cap exceeded | Size down to a valid common quantity or reject. |
| 100 USDT unsettled cap unavailable | Reject before reservation and submit. |
| Duplicate confirmation | Return the existing execution; never create another. |
| Submit timeout | Reconcile first; never blind-retry. |
| One leg filled | Bounded completion/unwind only if total worst-case loss is at most 2 USDT. |
| Tail below minimum size | Record dust only if all remediation loss remains within 2 USDT. |
| Unknown or unsafe account state | Global cross-venue breaker and alert. |
| Redemption fails | Keep `待兑付` and unsettled reservation; alert without blind retry. |
| Feishu fails | Persist signal and trading state; notification failure is isolated. |

## 16. Acceptance Criteria

### 16.1 Codex admission and discovery

| ID | Given / action | Required observation |
| --- | --- | --- |
| CV-01 | Load Predict markets with explicit Polymarket candidate IDs. | Candidate pairs resolve using the supplied Polymarket IDs even though native condition IDs differ. |
| CV-02 | Load a Predict market without candidate IDs. | No global scan or inferred pair is created. |
| CV-03 | Give Codex equal rules with metadata timestamps differing by one minute or 29 hours. | Approval is allowed only when verbatim full-rule evidence proves one canonical UTC cutoff. |
| CV-04 | Give Codex incomplete time rules, different resolution sources, cancellation differences, or an inverted proposition. | Structured decision is `REJECT`; the pair never enters the real-time pool. |
| CV-05 | Change either rule, timing metadata, token identity, or prompt version after approval. | Cached approval invalidates and the pair returns to low-frequency/Codex review. |
| CV-06 | Observe Codex validation under load. | Codex runs only before real-time admission; no stage-4, stage-5, preview, or execution request calls Codex. |

### 16.2 Economics and risk

| ID | Given / action | Required observation |
| --- | --- | --- |
| CV-07 | Build either complementary direction. | Predict and Polymarket net redeemable units are equal after deterministic fees. |
| CV-08 | Predict cannot supply deterministic fee/net-unit facts. | Opportunity remains visible but cannot become actionable. |
| CV-09 | Evaluate pUSD and USDT legs. | Both assets use the approved 1:1 accounting convention; no FX call or haircut is applied. |
| CV-10 | Gross economics are positive but theoretical simple annualized yield is below 15%. | Stage 4 may remain visible; stage 5 and execution are unavailable. |
| CV-11 | A smaller common quantity fits but the largest book quantity exceeds 20 USDT including fees/gas. | The builder selects the largest valid quantity within 20, rather than rejecting the pair because the larger candidate was examined first. |
| CV-12 | Concurrent previews approach the 100 USDT unsettled cap. | Atomic reservation and the global cross-venue execution lock prevent over-allocation. |
| CV-13 | Wallet balance exceeds the legacy 65 pUSD ceiling but is sufficient. | Cross-venue eligibility is unaffected by the balance being high. |

### 16.3 Preview and execution

| ID | Given / action | Required observation |
| --- | --- | --- |
| CV-14 | Open a current actionable signal. | Preview shows explicit venues, outcomes, net units, currencies, ceilings, economics, Codex evidence, cutoff, balances, unsettled capacity, and risk limits. |
| CV-15 | Confirm while both refreshed books remain within the displayed ceilings. | Exactly two venue submissions start concurrently under one execution ID. |
| CV-16 | Confirm after either price rises above its displayed ceiling or annualized yield falls below 15%. | No order is submitted; a new preview and confirmation are required. |
| CV-17 | Double-click confirmation or repeat the same execution request. | One execution and at most one order per intended venue leg exist. |
| CV-18 | One submit call times out. | The service queries order/account state before any retry. Unknown state opens the breaker without a duplicate order. |
| CV-19 | Both FOK legs fill. | Independent REST/account reconciliation proves both orders and equal net positions, then state becomes `holding_to_resolution`. |
| CV-20 | One leg fills and the other fails. | Remediation occurs only when full worst-case loss is bounded at or below 2 USDT; otherwise all cross-venue entry is halted. |
| CV-21 | A post-fill residual is below minimum order size. | It is recorded visibly as dust only when combined worst-case incident loss remains at or below 2 USDT. |
| CV-22 | One execution is active and another signal is actionable. | The second signal remains visible but cannot be confirmed until full reconciliation of the first. |

### 16.4 Settlement, UI, and notification

| ID | Given / action | Required observation |
| --- | --- | --- |
| CV-23 | A winning position becomes redeemable. | Existing automatic redemption runs once, is independently reconciled, and only then releases unsettled principal. |
| CV-24 | Redemption fails or remains pending. | UI and history show `待兑付`; no capacity is released and no blind transaction loop occurs. |
| CV-25 | Render either strategy tab. | The shared header truthfully shows both venues' REST, WebSocket, wallet, balance, asset, mode, and last success. |
| CV-26 | Render the YES/NO page at 1440px and 375px. | The five funnel stages, explicit exchange labels, candidate rows, and history are readable without the removed four duplicate metric cards or horizontal overflow. |
| CV-27 | Open the cross-venue modal on desktop and mobile. | All approved fields are visible; buttons are at least 44px; Escape closes desktop modal and restores focus; mobile action footer remains usable. |
| CV-28 | A pair enters stage 5 repeatedly without identity change. | One Feishu notification is scheduled for the pair/direction/fingerprints; stages 1–4 send none. No real message is sent in acceptance. |
| CV-29 | LLM hedge and legacy execution regression tests run. | Existing behavior remains unchanged apart from the intentionally shared venue header. |

### 16.5 Exact approval and environment verification

| ID | Given / action | Required observation |
| --- | --- | --- |
| CV-30 | Predict has zero current allowance but valid identity, balance, native gas, and one valid scoped BUY approval step. | Account readiness permits preview; zero allowance is not reported as an account failure. |
| CV-31 | Run the explicit testnet canary after confirming a bounded order. | The formal adapter sets only the exact EOA allowance, submits one bounded testnet order, and independently reconciles order, position, receipts, and remaining allowance. |
| CV-32 | Exact approval succeeds but either refreshed book leaves the confirmed bounds. | Neither venue order is submitted and the Predict allowance is reset to zero. |
| CV-33 | Allowance reset fails after a post-approval cancellation. | The execution remains non-submitted, the global cross-venue breaker opens, and an operator alert is persisted. |
| CV-34 | Build the mainnet Predict client and run live readiness. | The builder uses the Privy signer with the Predict deposit address; JWT, balances, approval step, and signed no-submit order pass without an approval or order transaction. |
| CV-35 | Run `make acceptance`. | No testnet canary, mainnet approval, mainnet order, or live notification occurs; mutation count remains zero. |

### 16.6 Final Dashboard gate

`make acceptance` is the final review-readiness gate and must return `PASS`.
Focused tests and direct workflow checks run during development; the complete
gate runs only after the implementation is otherwise finished.

The final gate must cover:

- complete Python tests
- deterministic desktop and mobile Prediction Playwright tests
- live read-only Predict and Polymarket market, book, balance, status, and
  no-submit readiness checks
- Dashboard API, process version, logs, and browser flows
- all existing non-prediction acceptance checks

The acceptance gate does not submit a real order and does not send a real
Feishu notification.

Only after `make acceptance` returns `PASS` may the exact accepted SHA be
redeployed for review. The handoff must prove the new PID, cwd, exact SHA, fresh
logs, and HTTP 200 review URL. Because the user requested visual confirmation,
desktop and mobile screenshots are supplied after PASS, but screenshots do not
substitute for any gate result.

### 16.7 Required evidence

- CV-01 through CV-13: focused source, Codex-schema, matching, timing,
  economics, sizing, and reservation pytest cases.
- CV-14 through CV-24: execution-service pytest with deterministic Predict and
  Polymarket submit/reconcile doubles, plus direct no-submit workflow checks.
- CV-25 through CV-29: Prediction Playwright at 1440px and 375px, including
  header, funnel, candidate, modal, keyboard, and notification assertions.
- CV-30 through CV-35: focused readiness/execution tests, one explicit
  operator-run EOA testnet canary using the formal adapter, mainnet read-only
  no-submit checks, and the retained historical Smart Account canary metadata.
- Live readiness: real read-only Predict and Polymarket API/account checks with
  no order submission and no secret output.
- Final runtime: `make acceptance` output, exact Git SHA, process PID and cwd,
  fresh logs, HTTP 200 review URL, and the requested desktop/mobile screenshots.

## 17. First Live Cross-Venue Canary

The existing single-venue Predict canary is evidence for Predict authentication
and order mechanics, not proof of cross-venue execution.

The operator-only testnet canary validates the formal adapter before this first
cross-venue mainnet canary. It does not replace the separate confirmation below
because an EOA testnet order cannot prove a two-venue mainnet execution or the
Predict Smart Account route.

After the implementation has passed acceptance and the exact SHA is deployed,
the first cross-venue live canary requires a separate explicit user confirmation
and must:

- use the smallest common executable quantity
- have combined maximum cost no greater than 5 USDT equivalent
- satisfy every normal Codex, economics, balance, capacity, freshness, and risk
  gate
- show the exact pair, direction, quantities, price ceilings, fees, and maximum
  cost before confirmation
- reconcile both venue orders, actual positions, fees, and balances
- remain within the normal 2 USDT remediation limit

If no current pair can satisfy the 5 USDT canary cap, no threshold is relaxed;
the system waits for a suitable opportunity. A successful canary removes only
the one-time 5 USDT operational constraint. The permanent 20/2/100 USDT limits
remain.
