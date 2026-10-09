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

## Workstation setup and comparison

Use this procedure when setting up Air or Mini, repairing development dependency
drift, or comparing the machines. Keep the same selected Git commit, Python/uv
baseline, and dependency extras on both machines. Compare like-for-like platforms;
machine-specific paths and credentials remain local.

1. Fetch GitHub and identify the exact commit to use. An unmerged reviewed branch
   can be fetched into an isolated worktree; adding it to GitHub `main` requires
   separate merge approval. Local `main` may fast-forward to already merged GitHub
   commits after checking its worktree and runtime consumers. Record the worktree,
   branch, SHA, and status. Use all project instructions and their linked runbooks
   from that checkout together.
   A stale worktree keeps its old rules until it is updated; copying a newer
   `AGENTS.md` into an older checkout does not synchronize the workflow.
2. Install or locate the exact uv version in the Contract above. If that Python
   version is absent, provision it with `uv python install 3.12.14 --no-bin` before
   running the locked installation commands. Keep the existing Python/lock
   versions when an installation fails; report the error rather than substituting
   a newer or older version.
3. Select a fresh development environment in the isolated worktree:

   ```sh
   test ! -e .venv && test ! -L .venv || {
     printf '%s\n' 'Use a new worktree: .venv already exists or is a symlink.' >&2
     exit 1
   }
   export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
   ```

   Then run the Contract's locked installation commands in that shell. Preserve
   any existing environment until its consumers
   are known. In particular, a main-checkout `.venv` referenced by launchd is a
   runtime dependency, not an environment to upgrade for development. Virtual
   environments are rebuilt from the lock on each machine, not copied between them.
4. Validate the new environment and capture evidence outside the repository:

   ```sh
   (
   set -eu
   export PYTHONDONTWRITEBYTECODE=1
   test -z "$(git status --porcelain)" || exit 1
   source_sha=$(git rev-parse HEAD)
   evidence_dir=$(mktemp -d)
   uv --version > "$evidence_dir/uv-version.txt"
   .venv/bin/python --version
   uv sync --check --locked --python 3.12.14 --no-python-downloads --offline \
     --no-default-groups --group build --extra dev --extra cloud-ssm --no-build-isolation
   uv pip check --python .venv/bin/python
   PYTHONPATH=src .venv/bin/python -c 'import open_trader; print(open_trader.__file__)'
   OPEN_TRADER_TEST_SOURCE_SHA="$source_sha" \
     OPEN_TRADER_TEST_SOURCE_STATE=clean \
     .venv/bin/python scripts/dev_dependency_manifest.py > "$evidence_dir/dependencies.json"
   test "$(git rev-parse HEAD)" = "$source_sha"
   test -z "$(git status --porcelain)" || exit 1
   printf '%s\n' "$evidence_dir"
   )
   ```

   Check the reported Python and uv versions against the Contract, and confirm
   that the import path belongs to this worktree. The manifest reuses the existing
   test-only evidence format; it is not release or deployment evidence. Compare
   `source_sha`, `source_state`, `python`, `platform`, `lock_sha256`, `base_images`,
   and the complete sorted `dependencies` list between Air and Mini. Both states
   must be `clean`, and both uv versions must match the Contract. Dependency
   installation and consistency checks do not replace directly affected tests
   when application code changes.

Agent configuration is a separate comparison. Record Codex CLI/app versions,
the effective model and reasoning setting for each task role, the loaded global
and project instruction files, enabled plugin/skill versions, custom hooks, and
required MCP tools on both machines. The Herdr implementation role follows
[the project workflow](agent-verification.md#implementation-and-tdd); other roles
retain their selected models. New sessions must load the updated project rules.
Use the same selected plugin/skill versions where the workflow depends on them;
an installed plugin or matching configuration file alone does not prove it loaded.

Synchronize only reviewed non-secret global settings and portable instruction
files. Keep login state, tokens, private keys, local paths, sessions, runtime data,
and machine-generated hook trust state on their owning machine. Configure and
verify path-dependent hooks/MCP tools locally instead of copying the entire
`~/.codex` directory. Shared project requirements remain in this repository, so
their behavior does not depend on one machine's optional skills or plugins.

Report project, agent configuration, and dependency comparisons separately.
Claim workstation parity only after all three are verified on both machines.
An inaccessible machine, an unverified loaded setting, or an unexplained
platform/package difference remains UNKNOWN or a recorded difference, not PASS.

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

The `Development dependency acceptance` workflow runs only for same-repository
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
replace a digest with an unverified guess. Add the dated CHANGELOG, then follow
the [PR-first delivery loop](agent-verification.md#first-reviewable-implementation):
Main stages/commits/pushes only task files and opens a normal PR under standing
authorization without another push/PR question, subject to its explicit
local-only/no-push/do-not-publish/draft overrides. Independent GLM review and CI
follow publication; in-scope repairs update the same PR before refreshed review.
Final review, CI and feedback gates precede explicit user approval for GitHub merge.
