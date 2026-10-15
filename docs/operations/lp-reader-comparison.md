# LP Reader comparison logs

The current LP flow remains the only trading authority. The Reader observer uses
values already read by that flow. It does not make an extra API request, write
business state, send an order, cancel an order, change a pause, or reset a
protection timer. Differences and observer failures do not stop the round.

`prediction_runtime` attaches the observer when it constructs the LP service.
The observer starts its memory-only worker on the first automatic-round sample.
Look for `lp_reader_comparison` in the existing Prediction logs. There is no new
HTTP endpoint, UI, command, or second polling loop.

## Records and coverage

- `event=summary`: emitted on the first completed sample, then on the next
  completion at least 60 seconds later. `counts` contains cumulative field
  counts for `match`, `mismatch`, and `not_comparable`, plus skipped frames and
  observer errors. `coverage` records actual compared fields by name. These are
  process-local counters, not a per-round audit.
- `event=difference`: first or changed mismatch details, at most 12 fields.
  Mismatches take priority over matches. `omitted_differences` counts details
  beyond that bound. Repeated identical differences remain in the counters.
- `compared`: fields actually compared in this sample. UNKNOWN, missing
  provenance, skipped samples, and empty coverage do not count as matches.
- Each detail has `field`, `old`, `new`, `delta`, `basis`, `status`, `reason`,
  `source_id`, `order_hash`, `as_of`, and available `source_times`. Numeric
  comparison uses exact Decimal values. Order identities are hashed; arbitrary
  strings and exception messages are excluded. Target identities are hashed.
- `round_id` correlates the sample with the primary round. Unsafe identifiers
  are hashed. `source_id` identifies the captured source packet; original
  acquisition and evaluation timestamps remain separate from the round ID.

Account comparison covers current exposure and captured per-order facts. BUY
estimates use captured order terms, remaining quantity, direction, and original
business evaluation time through `MarketReader` and `calculate_current`.
Candidate target comparison uses the final adopted candidate inputs and frozen
legacy selection constraints. These constraints are comparison policy, not
invented API facts. Existing-order target selection or retained legacy control
that cannot be paired is `not_comparable`; account and yield fields remain
independently comparable. Unread historical fees remain UNKNOWN.

The observer permits one running comparison and one pending frame. A new pending
frame replaces the old pending frame and increments `skipped`. Capture rejects
frames above 4 MiB of value content or 50,000 value nodes; it never truncates a
frame and calls it complete. Only plain immutable values reach the worker.
Comparison runs separately from the existing Queue32 diagnostic output sink.
Output can be dropped or fail without delaying trading. Sink loss is reported by
the existing `lp_read_log_limited` counters; absence of output proves nothing.

Shutdown stops admission and drops pending work. A running pure comparison may
finish naturally and does not keep the trading owner. Existing real SDK reads
and senders retain their original drain requirements.

## Independent Reader and evidence limits

`MarketReader.read_current(ReadScope, deadline)` is an opt-in, bounded independent
acquisition interface. It is offline-tested and is **not** started by the live
automatic loop. It allows at most two native reads, keeps timed-out tasks in
their capacity slots until they really finish, and bases retry deadlines on
actual failure time and upstream Retry-After. Callers must drain/close the Reader
before releasing its source adapter. Late results cannot modify returned frames.

After an authorized deployment, check the exact running version and actual
summary coverage. Classify differences as source pairing, legacy control,
Reader/calculation defects, or insufficient evidence. A silent log, zero useful
samples, or only `not_comparable` records means UNKNOWN. Live comparison logs do
not validate independent acquisition, and do not authorize a switch in trading
authority. Merge, deployment, and any production investigation remain separate
from this implementation's offline verification.
