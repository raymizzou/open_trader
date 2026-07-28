# Safe Latest Trend Revision Display

Date: 2026-07-27

## Problem

The Dashboard currently replaces the newest validated trend report with the
report frozen in the execution batch. This keeps execution safe, but it also
hides later corrections that do not change any buy or sell action. The current
HK report therefore still shows `00939` as manual review even though revision
`2026-07-24-r1.json` correctly classifies it as hold.

## Decision

When an execution batch exists, keep its report as the only execution source.
The Dashboard may display the newest validated revision only when its complete
`strategy_judgments.formal_actions` list is exactly equal to the locked
report's list.

For an allowed display-only revision:

- `artifact`, `report_sha256`, report counts, holding decisions, risk summary,
  and report body come from the newest revision.
- execution records are still loaded with the locked batch SHA.
- `execution_batch` continues to identify the locked report.
- `revision_anomaly` remains true and the existing warning continues to say
  that execution is locked to the original batch.

If the formal actions differ, keep displaying the locked report and expose the
revision only through report history. Invalid execution batches remain
blocking exactly as today.

## Acceptance

For the current HK data, the default report view must show:

- `持有 1`, `复核 0`;
- `00939 建设银行`;
- `继续持有`;
- strength `96.1`;
- active protection line `8.42`;
- the warning that execution remains locked to the original batch.

Automated coverage must prove both branches:

1. identical formal actions allow the newest revision to be displayed while
   preserving the locked execution batch;
2. changed formal actions keep the locked report displayed.

The report-history behavior and invalid-batch blocking behavior must remain
unchanged. Final readiness requires `make acceptance` to return `PASS`, then
deployment of that exact accepted Git SHA and a fresh browser check of the
default current report view.

## Scope

No execution-batch mutation, report rewriting, new configuration, or new
storage format.
