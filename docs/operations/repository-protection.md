# Proposed repository protections (not active)

This is the design for a separately approved settings change after the PR-first
workflow documentation is reviewed. It does **not** enable rules, authorize a
settings write, grant persistent access, create a tag/release, merge, or deploy.
The documentation stage does not complete the repository-protection rollout.

## Observed baseline, 2026-09-30

Read-only GitHub evidence for `raymizzou/open_trader` showed an empty repository
ruleset list and `main` with `protected: false`. The detailed branch-protection
GET returned 403 because this GitHub App connection has no administration
access; that endpoint's configuration is unknown, not independently verified
as empty. No protection-setting write capability was available. Re-read actual
settings before implementation; this snapshot is not an ongoing guarantee.
The immutable-release setting has not been established and is outside this stage.

The current [CI workflow](../../.github/workflows/ci.yml) uses workflow name `CI`
and aggregate job `required`. The actual check-run name is exactly `required`,
from the GitHub Actions app, ID **15368**. The UI's `CI / required` display is
not the API context string. This identity was verified on main commit
`0e887e5f4f3cfaa17bfd0e34e0f05174d63fb147` in the
[successful aggregate job](https://github.com/raymizzou/open_trader/actions/runs/36725393989/job/109924939065).

## Proposed main ruleset

Target only `refs/heads/main` and require:

- A pull request before merging; no task integration through local main or
  direct push. Keep the explicit user merge-approval boundary in the
  [agent verification runbook](agent-verification.md)
- Required status check context **`required`**, source **GitHub Actions**,
  integration/app ID **15368**; require branches to be up to date before merging
  (`strict_required_status_checks_policy: true` in a ruleset). Bind the source,
  rather than accepting a same-named status from another app
- **Zero mandatory second-person GitHub approvals** for this single-maintainer
  workflow. This avoids requiring another GitHub account; it does not remove
  fresh independent agent review of the staged diff, re-review after changes,
  or the user's explicit merge approval
- Block force pushes and branch deletion, with no default bypass actors or
  administrator bypass. Do not grant a bot, app, team or user bypass merely to
  make an otherwise blocked operation succeed

Confirm the actual UI/API representation and selected enforcement mode during
implementation. Do not require a guessed `CI / required` context or all
individual service checks: routed jobs can legitimately be skipped while the
fail-closed aggregate succeeds. Test that stale/missing/failed aggregate evidence
blocks merge and that an up-to-date documentation-only PR can satisfy the
aggregate under the existing routing. Do not alter CI routes to satisfy a rule.

## Proposed tag controls: creation versus immutability

Keep the two purposes in separate tag rulesets targeting `refs/tags/v*`:

1. **Immutable existing tags:** prohibit updates and deletion, with no default
   bypass. This applies even to whoever is permitted to create a release tag
2. **Restricted tag creation:** restrict creation to an explicitly selected
   release creator through the applicable creation-rule allowance/bypass. The
   user must choose that actor and approve its exact scope before activation.
   A creation-only allowance must not bypass the separate update/delete rules

The release creator has **not** been selected. Do not silently choose the agent,
a broad administrator role, or the repository owner, and do not activate a
creation restriction that would leave no approved release path. Tag creation
permission does not authorize a particular tag/release or deployment. Tags must
identify a deliberately selected final GitHub main SHA under the release process;
PR head or synthetic merge commits are not deployment acceptance evidence.

## Activation and verification handoff

After documentation review, present the exact main and tag rules, actor choice,
and resulting access/merge consequences for separate user approval. If the
current connection still cannot administer rules, have the user perform the
settings change through an approved GitHub flow; do not expand credentials or
permissions merely to bypass that limit. No setting is active until read-back
confirms its target, enforcement, check context/source and bypass list.

Verify the configured policy and observed merge eligibility without actually
merging, force-pushing, deleting refs, or deploying as a probe. Record any checks
that cannot be demonstrated as UNKNOWN. Record the approved recovery path before
activation; changes to rules or exceptions require their own authorization.
Historical plans/reports remain preserved; current delivery instructions are in
[AGENTS.md](../../AGENTS.md) and [agent-verification.md](agent-verification.md).

Official references: [available rules and source binding](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
and [repository rules API](https://docs.github.com/en/rest/repos/rules).
