# Repository protections

## Active configuration, verified 2026-10-06

Read-only GitHub ruleset responses confirmed the following active configuration
for `raymizzou/open_trader`. Each linked ruleset is a live source and can change;
this record is a dated snapshot. The earlier proposal and baseline are historical.
This document does not authorize settings changes, merge, tag/release creation,
or deployment. See [verification and delivery](agent-verification.md).

### Main: PR and CI required

Ruleset [24349214, `main-pr-and-ci`](https://github.com/raymizzou/open_trader/rules/24349214)
is active for `refs/heads/main`, with no excluded refs:

- A pull request is required before merge
- The required check context is exactly **`required`**, bound to **GitHub Actions**,
  app/integration ID **15368**. The UI label `CI / required` is not the API context
- Strict status-check policy requires the branch to be up to date before merge
- The required approving-review count is **0**. A single maintainer can use the
  PR path without another GitHub account's approval. Fresh independent agent
  review and explicit user merge approval remain required by the runbook
- Force pushes (`non_fast_forward`) and branch deletion are blocked
- The bypass-actor list is empty; the API reports `current_user_can_bypass: never`

The unchanged [CI workflow](../../.github/workflows/ci.yml) runs all four backend
services with `TEST_N_LEG=1`, non-LIVE portable scenarios and collection checks.
Documentation-only changes receive the same coverage. The aggregate also requires
the retained standalone trend-curve job to be skipped because legacy includes
those tests. Push-head, PR-merge and merged-main runs test distinct SHA identities.
See [CI identity and coverage](ci.md); do not weaken CI to satisfy a rule.

### Version tags: separate creation and immutability rules

Both active tag rulesets target `refs/tags/v*`, with no excluded refs:

- [24349247, `release-tags-immutable`](https://github.com/raymizzou/open_trader/rules/24349247)
  blocks updates and deletion. Its bypass-actor list is empty; the API reports
  `current_user_can_bypass: never`
- [24349295, `release-tags-raymizzou-create`](https://github.com/raymizzou/open_trader/rules/24349295)
  restricts creation. Its only bypass actor is user **raymizzou**, ID **156103254**,
  with mode `always`. This allowance applies only to the creation ruleset;
  it does not bypass the separate update/delete rules

The release creator is therefore selected in the active configuration. This
permission does not authorize a particular tag/release or deployment. Release
selection and Deployment Preflight remain separate; neither PR-head nor synthetic
PR-merge CI replaces trusted CI for the selected final GitHub main SHA.

### Verification evidence and limits

The read-only verification used the detailed ruleset API responses for
[24349214](https://api.github.com/repos/raymizzou/open_trader/rulesets/24349214),
[24349247](https://api.github.com/repos/raymizzou/open_trader/rulesets/24349247), and
[24349295](https://api.github.com/repos/raymizzou/open_trader/rulesets/24349295).
Each reported `enforcement: active`, the targets, rules and bypass actors above.

[PR #260](https://github.com/raymizzou/open_trader/pull/260) is a completed normal
PR-path example. Its head `fc9f73a8d92409a7a34a05405788581fc510e2e4` received a
[successful PR `required` check](https://github.com/raymizzou/open_trader/actions/runs/37390099247/job/112036013871)
from app 15368 before the owner merged it on 2026-10-06. The resulting main commit
`31c357eda48b9c2b3ff9fb2b3a6e1c16b0f1ef11` also received a
[successful main-push `required` check](https://github.com/raymizzou/open_trader/actions/runs/37392131579/job/112043136799).
This shows a completed PR path; it is not a deployment or runtime-health result.

The active policy requires the named check and blocks force pushes, deletion,
and tag updates. This review did not attempt a merge with stale, missing or
failed checks, a direct push to main, or a destructive tag operation. Those
rejection paths were verified by rule inspection, not an end-to-end negative
probe. Classic branch-protection details and GitHub release-object immutability
are not established by these ruleset reads. No settings, refs or production
services were changed to test enforcement.

## Historical baseline, 2026-09-30

Read-only GitHub evidence for `raymizzou/open_trader` showed an empty repository
ruleset list and `main` with `protected: false`. The detailed branch-protection
GET returned 403 because this GitHub App connection has no administration
access; that endpoint's configuration is unknown, not independently verified
as empty. No protection-setting write capability was available. This is the pre-activation snapshot, not the current configuration.
The immutable-release setting was not established in that review. It is distinct
from tag rulesets and remains unverified here.

The [CI workflow](../../.github/workflows/ci.yml) uses workflow name `CI`
and aggregate job `required`. The actual check-run name is exactly `required`,
from the GitHub Actions app, ID **15368**. The UI's `CI / required` display is
not the API context string. This identity was verified on main commit
`0e887e5f4f3cfaa17bfd0e34e0f05174d63fb147` in the
[successful aggregate job](https://github.com/raymizzou/open_trader/actions/runs/36725393989/job/109924939065).

## Future changes and recovery

Re-read the live rules before proposing any change. Settings changes and bypass
exceptions require their own explicit authorization; this record grants none.
Do not add a bot, app, role or user bypass merely to make a blocked action succeed.
If access is insufficient, use the approved settings flow rather than expanding
credentials. Record the recovery path and obtain approval before changing rules.

Preserve dated plans and reports as historical evidence. Current delivery
instructions are in [AGENTS.md](../../AGENTS.md) and
[agent-verification.md](agent-verification.md). Deployment Preflight, Host
Readiness and Production Smoke remain separate from PR merge and tag permissions.

Official references: [available rules and source binding](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
and [repository rules API](https://docs.github.com/en/rest/repos/rules).
