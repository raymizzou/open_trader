# Prediction Wallet Funding Cap: $65

## Decision

Raise the dedicated Polymarket wallet funding cap from `$50.00` to `$65.00`.
Keep the `$20.00` normal order cap and `$2.00` incident-remediation loss cap
unchanged.

## Behavior

- A wallet balance at or below `$65.00` may pass readiness checks.
- A wallet balance above `$65.00` remains fail-closed.
- No order is submitted during acceptance preflight.

## UI

Keep the approved layout unchanged. Every policy and confirmation surface that
shows the wallet funding cap must display `$65.00`; no surface may retain the
old `$50.00` policy text.

## Verification

- Update the funding-cap boundary tests to cover `$65.00` and `$65.01`.
- Assert the server policy payload and order-confirmation UI show `$65.00`.
- Run focused prediction-market tests.
- Run real wallet status and `--no-submit` preflight.
- Run `make acceptance` only as the final Dashboard review gate.
