# Retire TradingAgents Daily Chain

## Goal

Remove TradingAgents and DeepSeek from normal deployment and acceptance so a
missing DeepSeek balance cannot block the trend strategy. Trend reports remain
mandatory and continue to drive the only automated execution path.

## Decision

The scheduled TradingAgents premarket jobs are retired. Deployment removes the
HK and US premarket launchd jobs instead of installing them. The manual
TradingAgents commands and their historical artifacts remain available for
forensics, but no controller, deployment, Dashboard refresh, or acceptance check
invokes them.

The Dashboard stops presenting TradingAgents, technical-fact, decision-fact,
and AI change-classification output as current strategy inputs. Acceptance stops
requiring those files and stops translating their DeepSeek errors into a
deployment blocker. Existing files are neither deleted nor backfilled.

The trend-market controllers are unchanged: the executor host still generates
one required trend report per cycle, retries a failed generation, validates the
frozen report before execution, reconciles Futu before submission, and prevents
duplicate orders. Read-only hosts still generate no reports and place no orders.

## Runtime Flow

1. Deployment unloads all legacy TradingAgents premarket jobs.
2. Only the three persistent trend-market controllers are installed on the
   designated executor host.
3. Each controller generates or recovers its required trend report and executes
   only after the existing report, account, quote, window, host, and duplicate
   checks pass.
4. Dashboard acceptance verifies trend artifacts, live controller processes,
   broker/account data, and browser flows without consulting TradingAgents or
   DeepSeek artifacts.

## Failure Semantics

- A missing or failed trend report remains blocking and retryable.
- Missing TradingAgents/DeepSeek artifacts are ignored because they are no
  longer part of the strategy.
- Historical TradingAgents artifacts remain read-only and never satisfy a
  current trend prerequisite.

## Verification

- Focused tests prove deployment removes premarket jobs and installs only trend
  controllers.
- Focused Dashboard and acceptance tests prove TradingAgents is absent from the
  current strategy view and required-source checks.
- A direct install/restart check proves legacy jobs are unloaded and fresh trend
  controller PIDs run the new Git SHA.
- `make acceptance` is the final gate. After PASS, redeploy the exact accepted
  SHA and verify PID, working directory, Git SHA, fresh logs, and HTTP 200.

## Non-goals

- Deleting historical reports or the manual TradingAgents implementation.
- Adding a feature flag, replacement LLM, or automatic migration/backfill.
- Changing trend signals, sizing, protection rules, or duplicate prevention.
