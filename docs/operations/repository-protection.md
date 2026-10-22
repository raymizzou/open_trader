# Repository protections and dated read-back

This document records settings for `raymizzou/open_trader`; it does not authorize
settings writes, a PR merge, tag/release creation or deployment. Future changes
need applicable settings authorization and exact read-back. The current delivery
policy is in [AGENTS.md](../../AGENTS.md) and the
[agent verification runbook](agent-verification.md).

## Historical baseline, 2026-09-30

The read-only snapshot on this date showed an empty repository ruleset list and
`main` with `protected: false`. The detailed branch-protection GET returned 403
because that GitHub App connection lacked administration access. That endpoint
was UNKNOWN, not independently verified as empty. This is historical evidence,
not the current settings. Immutable-release settings have not been established
and remain outside this change.

## Verified settings, 2026-10-22

The three repository rulesets below were active before this change, with creation
dates of 2026-10-02. Under the user-approved `CI-MERGE-RULES-20261022` plan, Main
removed only the `required_status_checks` object from main ruleset **24349214**.
Before/after snapshots and field comparison were verified at
**2026-10-22T01:20:19Z**. All other main fields/rules and both complete tag
rulesets were preserved. No bypass was added and no ruleset was disabled.

The removed object required context `required` from GitHub Actions app **15368**,
with `strict_required_status_checks_policy: true` and
`do_not_enforce_on_create: false`. Merely setting strict to false would still
require status success; removing this object implements the approved policy of
merging without waiting for feature-push or synthetic PR-merge CI.

An authenticated `gh` GET of traditional `branches/main/protection` returned
**404, Branch not protected**. This resolves the earlier connection's 403 access
limit for that endpoint; it does not mean repository rulesets are off. These
are dated read-back facts, not an indefinite guarantee of future settings.

### Main: 24349214, `main-pr-and-ci`

- Target `branch`, enforcement `active`, include only `refs/heads/main`, exclude
  none; `bypass_actors=[]`.
- Rules retained: `deletion`, `non_fast_forward` and `pull_request`. Keep the
  PR-only route; no local-main integration, direct main push, force-push or
  deletion. A newer main alone does not require feature-branch synchronization.
- PR parameters retained: `required_approving_review_count=0`,
  `dismiss_stale_reviews_on_push=false`, `required_reviewers=[]`,
  `require_code_owner_review=false`, `require_last_push_approval=false`,
  `required_review_thread_resolution=false`, and
  `require_extra_approval_for_unattributed_changes=false`.
  Allowed merge methods remain `merge`, `squash`, `rebase`.
- No required-status rule remains in this ruleset. The name is unchanged;
  its historical `ci` suffix does not create a current status requirement.

Zero mandatory GitHub approvals does not remove required independent agent
review, complete feedback collection or reproducible blocker repair. Main owns
and reports that work alongside delivery. After explicit authorization for that
PR's merge, verify repository/base `main`, open state, expected head and absence
of conflicts, then merge through GitHub with the expected head. Unknown
mergeability needs a bounded metadata refresh; actual conflicts need scoped
resolution. Do not wait for feature/PR CI, pending review/dots or an old
review-ready/full-acceptance label. These duties do not create a replacement
premerge waiting gate. Repairs after merge require a new isolated branch and PR,
with separate merge authorization.

### Tags: creation and immutability remain separate

Both tag rulesets remain active and target only `refs/tags/v*`, with no excludes:

| ID / name | Retained rules | Retained bypass actors |
| --- | --- | --- |
| 24349247 / `release-tags-immutable` | `deletion`, `update` | none |
| 24349295 / `release-tags-raymizzou-create` | `creation` | User **156103254**, mode `always` |

The creation actor is an existing observed setting, not a new selection by this
change. Its creation-only allowance does not bypass the separate update/delete
ruleset. Tag creation permission does not authorize any particular tag/release
or deployment. Tags must identify a deliberately selected final GitHub main SHA
under the release process.

## CI and deployment evidence

[CI](ci.md) now runs all four backend services plus non-LIVE portable scenarios
automatically on each final main push, including documentation-only and
unavailable/empty diff cases. Preserve the existing separately authorized
main-only manual workflow with no inputs, exact-SHA checkout and plan/required
main-ref guards. There are no feature/PR triggers or cross-SHA concurrency cancellation.
Old feature branches may still carry their old workflows during migration; do
not synchronize/push solely to remove those runs, or wait for them.

The aggregate API check-run name remains exactly **`required`**, emitted by
**GitHub Actions**, app ID **15368**; the UI display `CI / required` is not the API
context. Main records each final SHA, run/attempt and actual result. Merge success
is not CI success. Failed, cancelled or missing results stay visible; Main owns
focused diagnosis and a scoped repair/revert proposal through PRs, without
automatic main writes, revert, retry-until-green or deployment.

Deployment still needs successful trusted exact-SHA main CI (push or the existing
main-only manual workflow), selecting the newest run across both events and its
latest attempt with artifacts bound to the real event. Newer unsuccessful runs
block older green evidence. Deployment also needs complete
required artifacts, source/lock/runtime verification, explicit deployment
authorization, fresh Host Readiness and Production Smoke. A failed main SHA
cannot be deployed and does not change running production. An older main ancestor
needs its own valid evidence. Optional rollback advisory remains independent;
retired classification and all other required tests remain unchanged.

Official references: [available rules and source binding](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
and [repository rules API](https://docs.github.com/en/rest/repos/rules).
