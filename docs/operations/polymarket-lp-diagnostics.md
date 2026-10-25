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
  when unrelated configs, foreign-condition rows or assets make native facts incomplete.
  After the whole observation, contributions from any conflicting config ID are
  excluded; conflicting rows for the requested condition invalidate that row's
  evidence. Repeating an earlier signature cannot restore a conflicting ID.
  Identical duplicates count once, and unrelated unambiguous configs remain valid.
  A fully valid
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


## Financial timeout await snapshot

`GET /api/prediction-arbitrage/lp/auto/state` exposes the latest financial
read timeout at `diagnostics.async_reads.last_financial_timeout`. The same
value is available from `LPReadRuntime.status()`. It is `null` before the first
financial timeout. It is memory-only and survives scope retirement, but not a
process restart. It is observation evidence; it does not grant financial or
trading authority and does not prove the cause of a network stall.

The existing supervisor still seals an incomplete/UNKNOWN result at the
five-second attempt deadline. It immediately records a pending summary without
waiting for the I/O loop. On the owning control loop, the existing cancellation
callback copies await-location metadata **before** cancelling the source tasks.
It stages only values for publication by the supervisor's next existing tick.
Sampling adds no lock wait, network, disk, logging, await or scheduler wakeup to
that callback. Diagnostic failure does not suppress cancellation. Response close,
actual resource exit, the six-second cleanup-fault boundary, recovery, cadence,
Retry-After, identity checks and no-replay protections keep their existing meaning.

Read the record with these limits:

- `attempt` and bounded `observation_id` identify the original timeout, rather
  than a later demand. `observation_id_truncated` discloses identity truncation.
  Old staged captures cannot replace a newer timeout record.
- `started_monotonic`, `deadline_monotonic` and `timeout_monotonic` distinguish
  attempt start, deadline and timeout publication. `captured_monotonic` is the
  actual sampling time, independent of the deadline. These are process-local
  monotonic times, not UTC timestamps or identities across restarts.
- `capture_state=pending` means no capture has been published yet; the I/O loop
  may still be blocked. `captured` means safe metadata was copied, not that the
  full chain was visible. `unavailable` means observation failed; no exception
  message or guessed wait location is exposed. A known sampling time is retained
  even when observation fails. `late=true` explicitly marks sampling after the
  original deadline. Missing sampling time and lateness remain `null`.
- `sources` contains at most `balance`, `open_orders` and `positions`. A captured
  source's `state=done` means its task was already done when sampled; it does not
  certify successful financial facts. `pending` means that source was unfinished
  at capture. Neither state says that cancellation has completed or that a
  response is closed.
- Each source retains the deepest 16 safely observed frames from at most 64
  native await links. Frames contain only a module/file basename, function name
  and line number, with 64-character ASCII text caps and `text_truncated`.
  `walked`, `truncated` and `end` disclose missing context. `end=unknown` is
  normal at an opaque or native Future boundary. No phase is inferred beyond an
  opaque or truncated boundary, even when an earlier frame suggests HTTP work.

The only compatibility bridge is CPython's exact native `async_generator_asend`
type, established from an inert local generator. `gc.get_referents` is used only
on that exact type, and only a single native async-generator referent is followed.
The bridge counts toward the 64-link cap. Custom objects, spoofed type names,
other referents, locals, payloads, headers, URLs, wallets, exceptions and HTTP pool
internals are not inspected. No live driver is advanced or closed by observation.
The record holds copied values, with a hard 16 KiB JSON budget; existing combined
diagnostic payload coverage retains its 256 KiB limit. GET returns detached
values and does not initiate reads, cancel requests, write storage or wake work.

Opaque/native awaiters can prevent this snapshot from identifying a network
phase. Deployment and a later recurrence are separate from implementation and
from root-cause proof. Deployment needs its own authorization and exact-SHA gates;
a restart can remove the current stalled process and its in-memory evidence.

## Request and cleanup ownership (2026-10-25, #455)

The existing runtime source task now owns the whole request: connection
acquisition, send, response headers, response body and actual cleanup. Its first
network cancellation is delivered normally. Once cleanup begins, or that first
cancellation starts unwinding the request, further cancellation requests are
retained until actual exit. Cancellation then propagates to the caller. The
source task uses the public asyncio Task cancellation API and the public httpcore
trace extension; it does not inspect or modify pool state or library locks.
Balanced library cancel/uncancel operations do not become caller cancellations.

Runtime reads reuse their existing source task. The former separate close child
is removed. `managed_tasks` therefore includes the actual I/O and cleanup owners,
with no hidden per-response task. A direct native caller of `source.read()` joins
one owned source task and forwards cancellation to it. The runtime does not add
that join task. The existing limits remain: B <= 64, tasks <= 8B+32, pools <= 2B+5,
connections <= 4B+9. Early body-size rejection explicitly closes the adapter's
iterator as well as the response. Close failure cannot return complete facts.
When cancellation and close failure coincide, cancellation retains the failure
as its cause.

The supervisor still publishes UNKNOWN at five seconds and exposes unfinished
cleanup at six seconds. A retry cannot start before the previous source actually
exits. Recovery keeps the same clients, fixed pools, cadence, provider cooldown,
identity and financial admission rules. Successful cleanup alone grants no
financial authority or permission to send or replay a trade.

The LP client factory sets HTTPX `timeout=None`. Recovery therefore does not
have an extra HTTPX timeout bound. If an internal AnyIO scope balances its own
cancellations while a caller cancellation overlaps, the remaining caller
cancellation must still reach the next ordinary network await. The owner
coalesces one loop callback after the synchronous balance boundary, checks the
remaining public cancellation count, and delivers it only outside cleanup.
It does not inject cancellation on each `uncancel()` call: on Python 3.12 that
could leave a pending injection after the library-only count reaches zero.
Actual source exit, rather than a counter or timeout assumption, still owns drain.

The task accounting above applies to supported production runtime demand shapes:
financial reads use three separate leaf sources; BUY market reads use `book`,
`metadata`, `native_rewards` and `sponsored_rewards`; exact reads use individual
sources. It is not a census of arbitrary caller-created demands/tasks or transient
internally scoped AnyIO connect helpers. The standalone source-port convenience
`SourceRequest('market', ...)` gathers its own child reads outside the runtime
registry. Its child tasks are not included in `managed_tasks`, and its use as a
custom runtime source is outside the stated production accounting proof. Its
standalone behavior remains covered by the existing source contract.

The factory supplies LP-owned HTTPX clients. Constructing the port appends one
deduplicated public request hook to each supplied client's `event_hooks` list;
this is an intentional mutation of those owned clients, not a factory change.
The hook is a no-op when there is no active source ownership context, so ordinary
non-owned reads retain their trace behavior. The supplied-client contract is an
HTTPX AsyncClient with that public hook API, not an arbitrary fake-client protocol.

## Financial request cleanup records

`LPReadRuntime.status()['financial_request_cleanup']` and
`GET /api/prediction-arbitrage/lp/auto/state` at
`diagnostics.async_reads.financial_request_cleanup` expose a copied, memory-only
snapshot. It is null until a financial request has been observed. The existing
supervisor publishes staged values only for the matching attempt. Status GET
copies values; it does not start reads, cancel, retry, write storage or wake work.
The original `last_financial_timeout` await chain remains available separately
and still targets the actual network source task.

- `attempt` binds the snapshot and each row to the runtime attempt. `sequence`
  is an opaque process-local request/page number. Neither contains account,
  order, URL or request identity text. Each public trace callback is bound to
  its own request, so late callbacks cannot relabel another request or attempt.
- `current` retains unfinished requests, at most one per financial source
  (`balance`, `open_orders`, `positions`). `history` retains at most 32 ended
  requests across pages and attempts. The entire cleanup snapshot is at most
  16 KiB JSON; ended history is evicted first, preserving current requests.
- `phase`, `trace_state` and `phase_monotonic` describe the latest observed
  request/header/body stage. Public HTTP/1.1 and HTTP/2 trace names are mapped to
  fixed enums; raw trace info is never read or serialized. External transports
  can omit these trace events. Absence is not evidence of successful cleanup.
- `cleanup_state` is `not_started`, `started`, `completed` or `failed`.
  `cleanup_started_monotonic` and `cleanup_completed_monotonic` distinguish
  entry from completion. `started` with no end is unfinished, regardless of a
  response's closed flag. A protocol cleanup event alone does not prove that
  a pool slot is reusable.
- `cancel_requested_monotonic` records an outstanding source cancellation;
  balanced library cancellation is cleared. `ended_monotonic` and `outcome`
  (`running`, `complete`, `failed`, `cancelled`) describe actual request exit,
  not successful financial parsing or source-task completion. A source can
  continue through later pages. Runtime scope `active` and the timeout/fault
  record remain the authority for source drain and retry admission.

All times are process-local monotonic observations, not UTC or identities across
restarts. Callbacks update bounded memory only, with no lock wait, file/network
I/O or added asynchronous checkpoint. Diagnostic clock/serialization failure is
isolated from cancellation and cleanup. Records contain no request/account/order
text, URL, headers, payload, exception text or library objects. Copies are detached;
process restart loses this history.

Offline real HTTP/1.1 tests reproduce cancellation during header-error cleanup
and verify the same fixed one-connection pool can complete a subsequent full GET
after repair. Body-error, normal-close, repeated cancellation, body-limit and
supervisor recovery cases cover the same ownership boundary. Offline reproduction
proves this local mechanism, not the first production trigger. Deployment and
production acceptance remain separate: normal reads, fresh eligible candidates,
and normal trial/income 3+3 operation under the original market and financial
rules still require their own evidence and authorization.

The body-error cancellation case was ALREADY_COVERED when introduced after the
header fix. It has no independent pre-fix RED; neither that guard nor a later
`body.failed`/cleanup trace proves the first production trigger. No production
fault injection is part of this verification.
