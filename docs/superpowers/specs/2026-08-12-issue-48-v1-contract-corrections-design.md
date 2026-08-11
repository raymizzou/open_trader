# Issue #48 Canonical v1 Contract Corrections

## Status

- Date: 2026-08-12
- Scope: corrections to the unshipped Issue #48 canonical model and exact Oracle
- Decision: replace the current v1 contract in place; do not retain compatibility
  shims or introduce v2
- Production impact: none; the branch is not connected to Prediction runtime,
  Dashboard, a production solver, live books, or order submission

## Goal

Make the Issue #48 implementation match the live ticket and its approved design
before the canonical model becomes an input to solver adapters. The correction
must remove two false-safe qualification paths, make v1 identity deterministic
and fail-closed, and freeze one proof record for positive and negative results.

## Canonical model

### CandidateAction identity and quantity domain

`CandidateAction` gains explicit `venue_id`, `account_id`, `chain_id`,
`min_quantity_lots`, and `max_quantity_lots` fields.

- All identity fields are non-empty strings.
- Zero always means “not selected”.
- A selected quantity is an integer in the inclusive
  `min_quantity_lots..max_quantity_lots` range.
- `min_quantity_lots` is positive and no greater than
  `max_quantity_lots`.
- Cost slices remain contiguous from lot one and must cover at least through
  `max_quantity_lots`; quantities above the explicit maximum are invalid even
  when an extra cost slice is present.
- The Oracle enumerates `{0} ∪ [min_quantity_lots, max_quantity_lots]` for each
  action. It still does not enumerate leg subsets separately.

### Supported schema versions

The module defines exact v1 constants for the request, problem, observation,
and proof schemas. Wire decoders reject any other value. Direct in-memory Oracle
requests with an unsupported version return `UNKNOWN:INVALID_MODEL`.

This is a strict v1 decoder, not a best-effort future-version decoder.

### Incomplete terminal data

`TerminalAtom.rule_version` and `TerminalAtom.capital_release_at` may be `null`
only to represent structurally preserved unknown input. A wire payload missing
either key is normalized to the same canonical `null` representation.

Validation assigns explicit terminal-data issue codes for:

- a missing action payout;
- a missing or blank terminal-atom rule identity;
- a missing terminal-atom capital release time.

The Oracle maps all three to `UNKNOWN:UNKNOWN_TERMINAL_DATA` before scenario
enumeration. Valid solving code therefore never calculates with a null rule or
release time. Other malformed fields remain explicit model errors.

## Qualification mathematics

Threshold values remain caller supplied and versioned. #48 does not hard-code
the 1 USD, 1%, 15%, or 30-day values.

The metric semantics are frozen as follows:

- Net margin is
  `guaranteed_profit / payout_lower_bound`, using checked integer cross
  multiplication. A non-positive payout cannot pass a positive margin rule.
- Annualized return is
  `guaranteed_profit / cost_upper_bound * 365 / occupied_days`.
- `occupied_days` is the positive release delay rounded up in 24-hour units and
  is at least one day, matching the approved design.
- `MAX_CAPITAL_RELEASE_DELAY_SECONDS` continues to use a conservative ceiling
  to the next whole second; it does not reuse the annualization day rounding.
- Every intermediate add, subtract, multiply, and rounding result remains in
  the signed 64-bit canonical domain or returns `UNKNOWN:NUMERIC_OVERFLOW`.

## One PayoutProof record

The separate `ExhaustiveSearchProof` public model is removed. `PayoutProof`
becomes a single versioned, discriminated record with `result_kind`:

- `PORTFOLIO`
- `NO_QUALIFIED_OPPORTUNITY`
- `NO_ARBITRAGE`

Portfolio proofs populate the existing portfolio, worst-state, payout, cost,
release, and support fields. Negative proofs populate the exhaustive method,
request/source-problem/qualification fingerprints, counts, and rejection
fields. Fields belonging to the other result kind are `null` and the decoder
rejects mixed or incomplete combinations.

`PortfolioSolution` continues to carry its portfolio `PayoutProof`.
`OracleResult.negative_proof` also uses `PayoutProof`; there is no second public
proof type.

The canonical request fingerprint transitively binds the complete #48 problem,
relations, terminal model, cost slices, and qualification constraints. Runtime
generation IDs and live quote fingerprints remain out of scope for #48 and are
not invented in the math contract.

## Deterministic identity

Qualification fingerprints are computed from the qualification constraints in
stable `constraint_id` order with an explicit schema identity. Therefore two
problems with the same canonical problem/request fingerprint also produce the
same qualification fingerprint, proof payload, and result fingerprint.

All fixture requests and expected results are regenerated after the v1 schema
change. The corpus file hash remains pinned by the tests.

## Error semantics

- Unsupported schema: wire `ModelDecodeError`; direct Oracle
  `UNKNOWN:INVALID_MODEL`.
- Missing payout, terminal rule identity, or release time:
  `UNKNOWN:UNKNOWN_TERMINAL_DATA`.
- Unknown common valuation: `UNKNOWN:UNKNOWN_VALUATION`.
- Decision, state, support, or numeric budget failure: the existing precise
  `UNKNOWN` reason.
- A malformed or mixed proof result-kind payload: `ModelDecodeError`.

No incomplete input may become a feasible or negative proved result.

## TDD and verification

Implementation proceeds one failing regression at a time:

1. Net-margin denominator counterexample: cost 60, payout 100, profit 40, 50%
   threshold must fail.
2. Annualization counterexample: cost 100, profit 1, release in 12 hours, 500%
   threshold must fail after one-day rounding.
3. Candidate action identity and explicit quantity-bound validation/enumeration.
4. Missing payout, rule identity, and release time all replay to the same
   terminal-data `UNKNOWN` result.
5. Unknown v1 schema values fail closed on wire and direct calls.
6. Reordered qualification constraints keep request, qualification-proof, and
   result fingerprints identical.
7. Positive and both negative outcomes round-trip through one `PayoutProof`
   result-kind contract; mixed payloads are rejected.
8. The complete 16-case Oracle corpus is regenerated and replayed twice from
   fresh decoded objects.

Final verification runs the focused N_LEG suites, the affected Prediction
arithmetic suite, compile checks, and `git diff --check`. The repository-wide
pytest result is recorded truthfully. `make acceptance` and process deployment
checks do not apply because this ticket changes no Dashboard or runtime path.

## Out of scope

- Production HiGHS, SCIP, or CP-SAT integration.
- Live order books, balances, runtime generation ownership, or quote adapters.
- Prediction Service, Dashboard, notifications, or order submission.
- SELL Entry or partial-fill repair.
- Compatibility with the discarded, unshipped pre-correction v1 fixture.
