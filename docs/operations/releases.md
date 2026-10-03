# Exact-SHA release artifacts

This runbook covers source artifacts and a reviewable GitHub Draft Release.
Follow [agent verification](agent-verification.md) for development, review and
GitHub merge. Artifact verification does not run Deployment Preflight, install
services, restart production, or authorize trading.

## Artifact contract

The release identifies one existing `vX.Y.Z` or `vX.Y.Z-rc.N` tag and its fully
peeled 40-character commit SHA. Version components cannot have leading zeroes,
and an `rc` number must be positive.
That commit must belong to GitHub `main` history; a PR head or synthetic PR merge
commit is not a substitute. Later main commits do not change the selected SHA.
Do not move, overwrite or recreate the selected tag to repair a release.

The source payload is a self-contained Git bundle containing only the
`refs/heads/release` ref at the selected SHA and its reachable history. The
original tag object SHA is recorded as provenance outside the bundle; the bundle
does not reproduce that tag ref or an annotated tag signature. It is built from
Git objects, never by copying a possibly dirty working directory, and does not
collect unrelated branch refs.
The original `uv.lock` accompanies the bundle unchanged. Dependency instructions
consume that lock; the artifact does not include a portable virtualenv or an
offline dependency cache. Never use the synthetic-commit `Dockerfile.dev` image
as a production release artifact.

The supported artifact-validation baseline is **Linux x86_64, Ubuntu 24.04,
Python 3.12.14 and uv 0.12.19**. This records where validation runs, not portable
runtime support or evidence of a cloud deployment. In particular, the initial
CentOS 8 / systemd 239 host described in [prediction-cloud.md](prediction-cloud.md)
needs its own current architecture, dependency and deployment evidence.

An independent `release-manifest.json` records the version/tag, commit and tree,
lock hash, platform/tool versions, CI and packaging run identities, actual
verification scope and exclusions, compatibility generations and asset hashes.
Its asset inventory excludes its own hash; `SHA256SUMS` covers the manifest and
the other delivered assets. Checksums prove byte consistency, not provenance by
themselves: verify the expected repository, tag, SHA and trusted workflow evidence.

Keep `ops/prediction-service-release.json` unchanged. Its strict schema contains
only `schema_version`, `reader_generation` and `contract_generation`; the new
release metadata must not be inserted into it. Copy compatibility generations
from that file at the selected SHA, not from the build checkout's current branch.

## Required CI evidence

Before building, resolve and pin the existing tag and verify all of the following
for that exact commit:

- The latest applicable `.github/workflows/ci.yml` run is a `push` to `main` in
  this repository, and its latest attempt completed successfully
- Its exact aggregate check name is `required`, from GitHub Actions app **15368**,
  and the check belongs to that same run and SHA
- The run's retained evidence is present, downloadable and consistent with the
  selected SHA and all four backend services and the non-LIVE portable scenarios

A same-named check from another app/workflow, a PR run, an earlier successful
attempt, another SHA, missing evidence, failure, cancellation or an incomplete
run fails closed. CI artifacts currently expire after three days. Copy verified
evidence into the release assets while available; a URL to expired evidence is
not a replacement. If evidence has expired, stop and report the blocker rather
than claiming that unavailable checks passed. All five scopes must succeed, including
for documentation-only changes. No skipped service or N-leg-disabled result qualifies.
Each artifact name binds scope, SHA, run ID and attempt. Its GitHub digest, repository,
workflow identity, selection, environment hashes, and portable collection partition
must agree. The `required` check must link to the successful aggregate job, not only
share its display name. See [ci.md](ci.md).

## Build, restore and review

The release workflow accepts version-tag pushes and an explicit manual retry
for an **already existing** tag. It never creates a tag. A manual dispatch's
`github.sha` is workflow context, not artifact identity; always resolve the tag.

The build/verification job has read-only repository permissions. It resolves CI
evidence, packages once, then restores into a fresh directory. Restoration must
prove the original commit, tree, lock and compatibility generations; a clean
detached checkout; the existing Prediction Git identity inspection; and an
imported code root inside the restored checkout. Keep verification output outside
that checkout so it stays clean. Do not read production data or credentials.

The delivered set contains `source.bundle`, `uv.lock`, `INSTALL.txt`,
`ci-evidence.json`, `artifact-verification.json`, `installation.log`,
`release-manifest.json`, `SHA256SUMS`, and five
`ci-<scope>-<SHA>-<run>-<attempt>.zip` archives. Each includes `evidence.json`;
portable also includes collection partition metadata and its log. Follow `INSTALL.txt` for
locked dependency installation in a new environment; network access is required
to obtain dependencies.

For a read-only build, use a clean checkout at the exact tag commit with freshly
fetched `origin/main` history and the existing tag. Git, `gh` with read access to
the repository/Actions evidence, and the stated Python/uv baseline are required.
The output must be outside the checkout and must not already exist:

```sh
python3 scripts/release_artifacts.py build \
  --repository raymizzou/open_trader --tag vX.Y.Z \
  --output /absolute/new-release-assets
```

To verify a downloaded complete set, run the verifier from a trusted, reviewed
checkout. First authenticate the intended repository, version tag and exact 40-character
commit SHA independently on GitHub. Supply that trusted SHA; do not copy it only
from the downloaded manifest. The CLI imports restored code only after matching
that expected commit identity. Choose a new restoration destination that does not
already exist:

```sh
python3 scripts/verify_release_artifacts.py /absolute/release-assets \
  --destination /absolute/new-restored-checkout --expected-sha <trusted-40hex-SHA>
```

This restores source and validates its artifact identity; it does not start a
service. Check the reported SHA and recorded provenance against the already
authenticated GitHub release. A tampered manifest plus recomputed checksums
cannot be detected by checksums alone.

The write-permission job performs only non-executing checksum, Git and metadata
verification: it never imports artifact code, runs its Makefile, or installs dependencies.
It checks out the immutable workflow SHA for its trusted verifier. The read-only
build computes selection and base-image metadata; its manifest SHA-256 reaches
the writer through a separate job output and is checked before asset processing.
The writer also binds that manifest to the original requested tag and the source
SHA resolved by the separate identity step, before any bundle restoration or API use.
The writer revalidates fresh GitHub evidence using those authenticated data and
pure archive validation. Executable restoration and source-selection checks happen
solely in the read-only build job.

The separate Draft Release job receives only the completed, verified artifact
set and has the `contents: write` permission needed to create the draft and upload
assets. A failed build cannot reach it. Work is serialized per tag. A retry must
not overwrite an existing tag, published release, or asset with different bytes;
stop on a conflict and preserve the evidence for review. Only an identical
asset set can resume without replacement. A fresh rebuild includes a new builder
run identity and installation log and can therefore conflict even at the same
source SHA. Retry the failed draft job with its original retained artifact set;
do not delete or replace assets to force a rebuilt set through.

Upload every asset while the release is still a draft, then download and verify
the complete set before considering it ready for manual review. A partial upload
is an incomplete draft, not a release. Neither workflow success nor a draft
grants permission to publish. GitHub's [immutable release flow](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
requires assets before publication: `release: published` is too late to build or
add them, and any post-publication job must be read-only. Do not depend on
[`GITHUB_TOKEN`-created tag/release events](https://docs.github.com/en/actions/concepts/security/github_token)
to trigger another workflow, or add a personal access token to work around event
suppression.

## Publication and deployment boundaries

**Formal publication requires fresh protection verification and verified immutable
release settings.** The 2026-10-03 readback confirmed three active rulesets:
[main](https://github.com/raymizzou/open_trader/rules/24349214) requires a PR and
strict `required` from app 15368 with no bypass;
[tag immutability](https://github.com/raymizzou/open_trader/rules/24349247) blocks
updates/deletions with no bypass; and
[tag creation](https://github.com/raymizzou/open_trader/rules/24349295) permits only
raymizzou (user 156103254) to bypass its creation restriction. Do not recreate
these protections. Tag restrictions do not prove release-asset immutability, which
is still unverified. Re-read current enforcement and release immutability before
publication; unknown settings block it. GitHub's
[immutability setting applies only to future releases](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes);
enabling it later does not establish an earlier release's immutability.

Implementation, branch push, Draft PR, GitHub merge, tag creation/push, Draft
Release creation/upload, formal publication, repository settings and deployment
are separate authorization boundaries. This implementation grants none of the
later operational permissions. Obtain the applicable explicit approval before
creating or retrying a real draft, changing tags/settings, or publishing. After
all assets and prerequisites are verified, an authorized operator publishes the
draft manually. Do not publish first and plan to attach missing files afterward.

Archived release CI is durable historical provenance for the exact source SHA and
recorded test environment. After download, offline verification checks those
preserved bytes without requiring their original temporary Actions artifacts to
remain available. It does not claim current forward-deployment readiness.

Deployment Preflight remains separate, for a selected final GitHub `main` SHA
retained in main history during an explicitly authorized forward deployment. It
requires fresh, available trusted main-push CI and checks the selected source,
lock and existing runtime identity; it does not rerun backend tests. The aliases
`candidate-acceptance` and `acceptance` invoke this lightweight gate. A later main
tip alone does not invalidate the selected retained SHA. Release archives do not
substitute for fresh forward-preflight evidence or current runtime checks.

Preserve separate exact-SHA Preflight `PASS`, fresh Host Readiness `READY`, explicit
deployment authorization and Production Smoke `HEALTHY` under
[agent verification](agent-verification.md), [Deployment Preflight](deployment-preflight.md),
and the [deployment runbook](../../ops/release-deployment.md). An existing authorized
compatible rollback remains separate. Unknown or changed identity blocks forward
deployment; release verification never authorizes rollback or retagging.
