# Daily Readiness Diagnostics Design

## Goal

Make the automated daily premarket run clearly explain whether the output is
usable for trading review, and why not when it is not usable.

This phase covers two connected problems:

- Futu quote diagnostics are currently collapsed into a generic error string.
- The top-level `partial` status can mean different things, including deadline
  fallback, missing quotes, and Futu quote server interruption.

## Scope

This change applies to `DailyPremarketRunner` and the Futu quote diagnostic path
used by the daily automation.

In scope:

- Add structured readiness fields to `daily_run_status.json`.
- Add structured Futu diagnostic fields to `futu_plan_check`.
- Improve the daily Markdown report summary so it states whether the run is
  ready, blocked, or requires manual review.
- Use the same structured reasons in Feishu blocker notifications.
- Preserve existing CSV outputs and latest artifact promotion behavior.

Out of scope:

- Order placement.
- A browser or UI dashboard.
- A full `daily_premarket.py` module split.
- New long-running monitoring or daemon behavior.
- Changing manual CLI command output unless needed to share small helper code.

## Status Compatibility

The existing top-level status remains:

```text
success | partial | failed | already_running
```

This avoids breaking existing CLI behavior, tests, and downstream scripts.

The daily status file adds a separate readiness layer:

```text
readiness: ready | review_required | blocked
```

Readiness means:

- `ready`: advice, trading plan, Futu quotes, and trade actions completed without
  conditions that block review.
- `review_required`: the run produced useful artifacts, but at least one issue
  needs human review before acting. Examples: missing quote for one symbol,
  generated trade action review rows, or advice fallback after deadline.
- `blocked`: the run cannot provide a reliable action-review basis. Examples:
  failed run, Futu OpenD unreachable, quote server interruption, or snapshot
  failure for all active plan symbols.

## Status Reasons

The status payload adds:

```json
{
  "readiness": "blocked",
  "status_reasons": [
    "futu_error"
  ]
}
```

Allowed `status_reasons` values:

- `advice_fallback`: one or more advice rows came from fallback.
- `advice_error`: one or more advice rows failed without fallback.
- `plan_fallback`: one or more trading plan rows came from fallback advice.
- `plan_error`: one or more trading plan rows could not become active plans.
- `futu_error`: Futu quote checking failed before usable snapshots were
  available.
- `missing_quotes`: Futu returned snapshots for some symbols but not all active
  plan symbols.
- `trade_action_review`: generated trade actions include review rows.
- `run_failed`: the daily run failed before normal artifact generation.
- `already_running`: the daily lock prevented a second run from starting.

The existing `status` derives from these reasons:

- `failed` for `run_failed`.
- `already_running` for `already_running`.
- `partial` for any non-empty reason set except normal `trade_action_review`
  when all core daily steps completed.
- `success` when no reason is present.

Readiness derives separately:

- `blocked` for `run_failed`, `already_running`, or `futu_error`.
- `review_required` for `advice_fallback`, `advice_error`, `plan_fallback`,
  `plan_error`, `missing_quotes`, or `trade_action_review`.
- `ready` when no reason is present.

## Futu Diagnostics

`futu_plan_check` keeps the current fields:

```json
{
  "checked": 0,
  "missing": 0,
  "triggered": 0,
  "items": [],
  "error": "网络中断"
}
```

It adds a nested diagnostic object:

```json
{
  "diagnostic": {
    "host": "127.0.0.1",
    "port": 11111,
    "opend_reachable": true,
    "context_ok": true,
    "snapshot_ok": false,
    "error_type": "quote_server_interrupted",
    "message": "网络中断",
    "next_step": "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
  }
}
```

Allowed `error_type` values:

- `none`: no Futu diagnostic error.
- `no_active_plans`: no active trading plans needed quote checks.
- `opend_unreachable`: TCP connection to configured host and port failed.
- `context_failed`: Futu SDK context creation failed.
- `quote_server_interrupted`: snapshot call returned `网络中断`.
- `snapshot_failed`: snapshot call failed for another reason.
- `missing_quotes`: snapshot call succeeded but one or more symbols were absent.

The daily runner does not need a separate recovery mechanism in this phase. It
should produce enough structured information for the user to fix OpenD and rerun.

## Daily Report

The Markdown report adds a short readiness section near the top:

```text
## 可用性判断

- 可用性：阻塞
- 原因：Futu 行情异常
- 下一步：请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。
```

The existing summary, Futu plan checks, and artifacts sections remain. The report
continues to include the raw Futu error when present.

## Feishu Notification Behavior

Blocker notifications use the same readiness and reason fields.

All user-facing notification text must be Chinese. This includes Feishu titles,
body text, reason labels, impact text, and next-step guidance. Diagnostic
messages that are copied into notifications, such as `next_step`, must also be
Chinese. Machine-readable JSON fields such as `readiness`, `status_reasons`, and
`error_type` remain English enums so scripts can parse them reliably, but raw
enum labels should not be used as user-facing reason text in notifications.

Examples:

- `blocked` + `futu_error`: title remains `Open Trader 阻塞通知`; body states
  in Chinese that Futu quote checking failed and includes the diagnostic next
  step in Chinese.
- `review_required` + `missing_quotes`: body states that some symbols lack
  quotes and that trade actions require manual review, in Chinese.
- `review_required` + `trade_action_review`: body states the review row count
  and links the status/report artifacts, in Chinese.

Normal action notifications remain separate as `Open Trader 行动通知`.

Notification rendering failures remain best-effort and must not change run
status or readiness.

## Data Flow

1. The daily runner builds premarket advice and the trading plan as it does
   today.
2. The runner checks Futu quotes through the existing quote client factory.
3. The quote-check path builds `futu_plan_check` with diagnostic details.
4. The runner counts advice, plan, Futu, and trade-action conditions.
5. A small pure helper derives `status_reasons`, top-level `status`, and
   `readiness`.
6. The runner writes JSON, Markdown, log, and notifications using the derived
   values.

## Error Handling

- Futu failures remain non-fatal to the whole daily run when advice and plan
  generation succeeded.
- Futu failures produce `readiness=blocked` and a concrete diagnostic reason.
- Missing quotes produce `readiness=review_required`, not `blocked`, because
  some quote data may still be useful but automated action output is incomplete.
- Failure to render or send Feishu notifications remains best-effort.
- Failure to write the status/report/log continues to use the existing failure
  reporting path.

## Testing

Add focused tests for:

- OpenD unreachable:
  - `status=partial`
  - `readiness=blocked`
  - `status_reasons` includes `futu_error`
  - diagnostic `error_type=opend_unreachable`
  - report includes the next step.
- Snapshot returns `网络中断`:
  - diagnostic `error_type=quote_server_interrupted`
  - `readiness=blocked`
  - Feishu blocker includes the quote-server recovery message.
- Missing quote:
  - diagnostic `error_type=missing_quotes`
  - `readiness=review_required`
  - `status_reasons` includes `missing_quotes`
  - report and blocker notification name the missing quote count.
- Deadline fallback with Futu healthy:
  - `readiness=review_required`
  - reasons include `advice_fallback`
  - reasons do not include `futu_error`.
- Success path:
  - `readiness=ready`
  - `status_reasons=[]`
  - current latest artifact promotion behavior is unchanged.

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_daily_premarket.py -v
.venv/bin/python -m pytest
```

## Acceptance Criteria

- `daily_run_status.json` can distinguish daily run completion status from
  action-review readiness.
- Futu quote failures are classified with a stable `error_type` and next step.
- A user reading only the daily Markdown report can tell whether the result is
  ready, blocked, or requires manual review.
- Feishu notifications remain fully Chinese, including reason labels and next
  steps. Artifact paths may contain English file or directory names.
- Existing CSV schemas and latest artifact promotion behavior remain compatible.
- The full pytest suite passes.
