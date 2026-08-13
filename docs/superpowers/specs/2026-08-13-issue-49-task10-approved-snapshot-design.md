# Issue #49 Task 10 Approved Snapshot Design

## Goal

Create one user-approved, complete #48 approval envelope from an already
persisted production threshold preview, then use the existing Task 10 import
path. This is a one-time evidence conversion for the solver benchmark. It does
not create a production exporter or a production payout proof.

## Frozen source

- Source alias: `production-threshold-preview`
- Preview ID: `bffd03fe104447e589a4539bb9b7846d`
- Captured at: `2026-08-12T21:55:32.153149Z`
- Relation ID:
  `threshold:2765212fb8dd441806b4d6dadc833d12a885c69cd5a32579e852c8e3733ab49f`
- Approval cache key:
  `417ee818c70333531091f01943902816c396e29d6e7057ba39793c0656384b80`
- Rule fingerprint:
  `281ad84b51843183838f3103cf53e8c2ff06ce77d79effbfba0566cf2a684a6a`
- Approved relationship: the SPY close above USD 800 contract implies the SPY
  close above USD 795 contract.
- User approval recorded at `2026-08-13T10:36:37Z`.

The conversion reads only this frozen relation, preview, and cached approval.
It does not select a newer row, query external APIs, synthesize a market, or
derive approval from a positive signal alone.

## Canonical #48 mapping

Use `polymarket-usdc-micro` as both settlement and valuation unit. One share is
one quantity lot and pays `1_000_000` units on a winning normal terminal state.
The approved executable domain is 5 through 20 equal-share lots.

Create two actions:

- Buy YES on the USD 795 contract at protected price `0.009`.
- Buy NO on the USD 800 contract at protected price `0.986`.

Use one conservative cost slice per action, covering lots 1 through 20. Add the
existing fee formula `quantity * 0.04 * price * (1 - price)` per leg and round
each per-share total cost upward to the next micro-USDC:

- USD 795 YES: `9_357` units per lot.
- USD 800 NO: `986_553` units per lot.

The resulting 20-lot cost bound is `19.918200` USDC, conservatively above the
persisted `19.9181784` USDC bound by `0.0000216` USDC.

Both contracts use the same versioned observation identity: the Pyth
`Equity.US.SPY/USD` close for the 2026-08-13 US regular session, including the
rules' last-valid-Pyth and official-primary-exchange fallbacks. The observation
window is `2026-08-13T13:30:00Z` through `2026-08-13T20:00:00Z`, timezone
`America/New_York`, and the persisted rule fingerprint is the rule version.

Each contract has three terminal atoms:

- `NORMAL_YES`
- `NORMAL_NO`
- `SPLIT`, paying `500_000` units per selected action lot

The `SPLIT` atom represents the identical 50-50 exceptional settlement in both
market rules. Add an ordered `IMPLIES` relation from the USD 800 contract to the
USD 795 contract. Add four forbidden atom combinations so `SPLIT` must occur on
both contracts together and cannot be paired with a normal atom on the other
contract. The implication applies only to ordinary YES/NO atoms, as required by
the #48 Oracle.

## Explicit benchmark-only release assumption

Map the persisted preview `resolution_at` of `2026-08-13T20:00:00Z` to every
terminal atom's `capital_release_at`. This is the user's approved Task 10
benchmark assumption. It is not evidence of actual venue settlement latency,
must remain visible in the source envelope provenance, and must not be reused
as a production trading or payout proof.

## Qualification policy

Use the already frozen caller-supplied policy:

- guaranteed profit at least `1_000_000` micro-USDC (1 USDC);
- net margin at least `10_000` ppm (1%);
- annualized return at least `150_000` ppm (15%);
- maximum capital release delay at most `2_592_000` seconds (30 days).

The imported real case may honestly have no qualified opportunity. Task 10
benchmarks solver agreement and evidence; it does not weaken thresholds to
force a positive result.

## Artifact and verification flow

1. Write the complete local envelope to
   `benchmarks/prediction_solver/inbox/approved_component.json`.
2. Decode it with `problem_from_payload`, require `validate_problem(problem)` to
   be empty, and run the bounded exact Oracle before import.
3. Run the existing `import-approved` command.
4. Confirm exactly one anonymized approved case, recompute both source and
   anonymized fingerprints, and verify that raw account, contract, token,
   relation, approval, and cache identities do not appear in the committed
   corpus.
5. Remove no retained legacy input gap; it remains historical evidence.
6. Only after these checks may Task 10 build/reuse environments and start the
   quick/full benchmark sequence.

## Scope boundary

This design permits the ignored inbox artifact, the anonymized
`approved_v1.json` case, final Task 10 result files, and the required changelog
entry. It adds no exporter, CLI, schema, database mutation, external fetch,
solver behavior, Dashboard change, production integration, order, VPS run, or
automatic #50 handoff.
