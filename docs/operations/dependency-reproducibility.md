# Reproducible development dependencies (#212)

## Contract

Use Python **3.12.14** and uv **0.12.19** for the development baseline. The
application's `requires-python >=3.12` remains unchanged; this baseline is not a
claim that every newer Python/platform combination was tested.

`dev` installs pytest/xdist, `cloud-ssm` installs the pinned Tencent SDKs, and
`browser` is an explicit host-only Playwright extra. Ordinary Docker tests do
not install Python/JS Playwright or browser binaries. Browser asset installation
remains part of the separately authorized host Production Smoke setup.

Start from a clean environment. Use the same steps locally and in Docker:

```sh
uv lock --check --python 3.12.14 --no-python-downloads
uv sync --locked --python 3.12.14 --no-python-downloads --only-group build --no-build
uv sync --locked --python 3.12.14 --no-python-downloads --no-default-groups --group build --extra dev --extra cloud-ssm --no-install-project --no-build-isolation
uv sync --locked --python 3.12.14 --no-python-downloads --offline --no-default-groups --group build --extra dev --extra cloud-ssm --no-build-isolation
```

The first sync bootstraps only locked setuptools; futu-api has no wheel and is
built from its hash-locked source archive using that backend. Build isolation is
disabled deliberately so it cannot resolve fresh build tools outside the lock.
uv does not prune extraneous packages with this mode: use a new environment for
acceptance. The final editable project build works offline. Dependency download
and image build need network access; test execution must remain disconnected.
`--locked` rejects stale metadata and never silently regenerates the lock.

The Dockerfile pins both the Python base and uv image by registry manifest
digest. Debian apt packages remain security-updatable and are not snapshot
pinned: the contract is identical Python package versions on the same platform,
not bit-for-bit identical OS/image layers. Changing architecture can select
different platform-marked dependencies, so compare like-for-like builds.

The image's Git commit is a **synthetic test snapshot**, retained for existing
Git-aware tests. It is not the original source SHA and this image MUST NOT be
used as a production release artifact. Make supplies the original SHA and dirty
state as informational build arguments; the manifest records those, Python,
platform, lock hash, pinned image references, and installed dependency versions.
Direct Docker builds default to unknown provenance unless arguments are given.

## Acceptance on a Docker-capable machine

First commit the reviewed patch on its isolated branch. No merge or deployment
is required. Run from that clean commit; dependency verification never runs
Candidate Acceptance, Host Readiness, or Production Smoke.

```sh
bash scripts/verify_dev_dependencies.sh "$(mktemp -d)"
```

The verifier requires a clean committed checkout. It archives that exact commit
to a disposable context, adds fake credential/data sentinels, builds twice with
`--no-cache`, compares manifests, inspects the real containers for zero mounts
and network isolation, checks sentinels/environment/browser absence, and runs
the focused `make test TEST=...` entry point. It records build, lock, image,
container, isolation, and test evidence outside the checkout; failures stop
verification and do not print PASS.

The `Issue 212 dependency acceptance` workflow runs only for same-repository
pull requests from `fix/212-locked-dev` to main that touch its listed files.
It checks out the PR's exact head SHA with no persisted credentials, uses a
standard `ubuntu-24.04` runner with read-only contents permission and a 30-minute
limit, and retains evidence artifacts for three days. It has no production
secrets, deployment step, write token permissions, or broad CI rollout. Action
references are SHA-pinned; verify replacement references against their official
repositories when updating them. It is a ticket-specific acceptance check,
not the full service or deployment gates.

Inspect the two manifest files, build logs, and focused test results together.
The fake sentinels prove context exclusions without accessing real credentials;
run commands have no host mounts, forwarded secrets, published ports, or Docker
socket. A static contract check alone is not proof of runtime isolation. Preserve
the evidence directory until review; remove only those disposable files/images
when no longer needed.

## Updating deliberately

Update `pyproject.toml` only for an approved dependency change; run `uv lock`
without `--upgrade` to preserve existing versions. Review the complete lock diff,
including transitive additions and hashes. For intended upgrades use a targeted
`uv lock --upgrade-package NAME`. Keep the setuptools build requirement and
`build` group pin equal. Confirm Python/uv image tags and their manifest digests
against the official registries before editing Dockerfile.dev, update this
baseline, then repeat both clean builds and the focused checks above. Never
replace a digest with an unverified guess. Stage the dated CHANGELOG and request
independent review of `git diff --cached` before publication.
