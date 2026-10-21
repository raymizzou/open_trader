# LP failure, wait and recovery logs

Prediction service logs include one-line `lp_diagnostic` JSON records. They add
observations to the existing `lp_snapshot_stage`, `lp_read_wait`,
`lp_causal_event` and `lp_read_log_limited` lines. Trading decisions, API/CLI
payloads and durable order audits retain their existing meaning. The Dashboard
has no new display for these events.

## Read the scope before the outcome

- `reward_native` and `reward_sponsored` track each selected reward source
  separately. Another market or reward category cannot recover its episode.
  Recovery requires validated identity, configuration, asset and reward totals,
  not just a returned SDK call. Valid empty data can recover. Invalid combined
  totals keep the sponsored source failed while a valid native source can recover.
  Validated active native USD contributions remain a diagnostic lower bound even
  when other configs, rows or assets make native facts incomplete. A fully valid
  combined total below that bound cannot recover; a consistent combined source
  can recover independently. This does not make a partial native total complete
  or change the public catalog's existing reason ordering or unknown totals.
- `account_full` tracks full account reads and publication. `account_planning`
  tracks automatic planning. A planning success does not clear a full sync error.
  Reusing an older valid planning source does not recover the failed current read.
  Dashboard full-read attempts start before the shared reader is called and follow
  that call through registration. Returned source times also fence cache reuse:
  facts read before the latest failed full-read admission cannot recover it, even
  when a newer wrapper call returns those facts (`source_current=false`).
- `entry` tracks one request. Its `post_started` comes from that request's durable
  send boundary. Account-read records omit this field. They say nothing about
  whether other requests were sent.

Actual failures use WARNING. Ordinary generation, capacity and in-progress waits
use INFO. Recovery uses INFO. `stage` distinguishes `account_read` from
`account_publish`, and entry validation/preparation/send from `receipt_apply`.
Planning read errors name only the controlled component (`balance`, `open_orders`
or `positions`). A returned order-economics conflict retains its specific safe
`cause_code`, with `account_order_sync_unknown` as the summary.

`owner_id`, `scope_id`, `attempt_id`, `episode_id`, `request_id`, `round_id` and
`task_id` are opaque process-local identities. Request and round identities are
captured at the actual call boundary. Unknown relationships are `unknown`;
adjacent lines and thread IDs do not prove association. Compare identities,
not arrival order. A late older completion can have `current_attempt=false` and
`event=complete`; it cannot recover a newer failure or wait.

`event_at` is the UTC observation time. Available source read times and trade
generation accompany account records. Recovery retains the first/latest cause,
exception classes, stages, attempts, occurrence times and `failure_count`.
`failure_count` includes failed and waiting observations in that episode.
Unchanged observations aggregate independently by stage and controlled component.
`repeat_count` counts additional observations of the same state, cause and
exception classes in that channel. Other components and publication cannot reset
its window. A repeat summary emits at most once per 60 monotonic seconds per
unchanged channel. Cause or state changes emit immediately. Suppressed observations
still update the episode's count and latest context.

## Illustrative examples

These abbreviated records are examples of the schema, not historical incident
samples. Placeholder identities are not real accounts, orders or requests.

```text
WARNING ... lp_diagnostic {"event":"failed","scope":"reward_sponsored","stage":"reward_sponsored","event_at":"2026-10-21T12:00:00+00:00","scope_id":"<opaque-source>","attempt_id":"<attempt-A>","episode_id":"<episode-A>","cause_code":"reward_read_failed","exception_types":["TransportError","ReadTimeout"],"failure_count":1,"output_continuity":"unknown"}
WARNING ... lp_diagnostic {"event":"failed","scope":"account_full","stage":"account_publish","cause_code":"order_original_quantity_conflict","summary_reason":"account_order_sync_unknown","episode_id":"<episode-B>","failure_count":1}
INFO ... lp_diagnostic {"event":"waiting","scope":"account_full","stage":"account_publish","cause_code":"account_round_invalid","first_cause_code":"order_original_quantity_conflict","episode_id":"<episode-B>","failure_count":2}
INFO ... lp_diagnostic {"event":"recovered","scope":"account_full","stage":"account_publish","episode_id":"<episode-B>","first_cause_code":"order_original_quantity_conflict","latest_cause_code":"account_round_invalid","failure_count":2}
WARNING ... lp_diagnostic {"event":"failed","scope":"entry","stage":"entry_send","request_id":"<opaque-request>","post_started":true,"result":"unknown","cause_code":"send_unknown","exception_types":["ReadTimeout"]}
INFO ... lp_diagnostic {"event":"waiting","scope":"entry","stage":"receipt_apply","request_id":"<another-request>","post_started":true,"result":"accepted","cause_code":"receipt_apply_pending"}
```

`event=sent` at the durable boundary means `post_started=true`; it does not claim
venue acceptance. A later `event=sent,result=accepted` means the accepted receipt
is durable. `receipt_apply_pending` still has `result=accepted`. Only actual
application of the matching accepted receipt can close that wait. An account
recovery does not change an UNKNOWN send into accepted or rejected and does not
authorize replay. Consult the original stored action/session for order truth.

## Limits and safe handling

Events contain only allowlisted reasons/stages, bounded exception class chains,
validated status/times and opaque keyed hashes. They omit exception text, raw
receipts, response bodies, URLs, headers, credentials and raw business identities.
Unrecognized reason text becomes `unknown`; the business result is unchanged.

Each adapter/service retains at most 256 diagnostic scopes in memory, with at most
32 stage/component channels per episode. Old scopes
are evicted. Subsequent records disclose `continuity=incomplete` and
`evicted_scopes`; success without a retained episode emits no invented recovery.
Restarting or replacing an owner starts new opaque identities and loses episode
history. Do not join episodes across owners or claim pre-restart continuity.

Output uses the existing single diagnostic consumer and queue of at most 32
records. Sink I/O runs outside business locks. Blocked or raising handlers cannot
hold a trading operation for output. Queue overflow and sink errors contribute
to `lp_read_log_limited` counts. Logging is best effort: process exit, sink failure
or overflow can lose events. `output_continuity=unknown` explicitly avoids claiming
complete output history, even when the in-memory episode is intact. This log is
not a persistent audit store. Missing lines do not establish success or rejection.
