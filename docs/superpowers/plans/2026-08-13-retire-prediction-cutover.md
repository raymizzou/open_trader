# Retire Prediction Cutover Machinery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the completed #45 one-time Prediction cutover machinery and active Legacy rollback instructions while leaving Service-owned steady-state behavior and verification unchanged.

**Architecture:** This is one deletion-only implementation task: remove two scripts and their dedicated test suite, remove the obsolete operator runbook block, and add the required changelog entry. No replacement code, test, compatibility wrapper, route transition, or runtime mutation is introduced; existing Prediction Service, Gateway, browser, and live no-submit checks remain the supported contract.

**Tech Stack:** Git, Bash 3.2-compatible shell scripts, Python 3.12, pytest, launchd, Frontend Gateway, and the existing `make acceptance` closure gate.

## Global Constraints

- Implement from the approved spec commit `693766367a5089ab7805655af63de93fa2fc8791` in an isolated worktree; do not edit `/Users/ray/projects/open_trader` directly during deletion work.
- Delegate all implementation and review fixes to `worker`; after focused verification, run `reviewer` until it reports no actionable findings.
- Delete exactly `scripts/cutover_prediction_service.sh`, `scripts/prediction_cutover_account_proof.py`, and `tests/test_prediction_cutover_launchd.py`.
- Add no replacement command, helper, wrapper, abstraction, test, skip, tombstone, or migrated cutover assertion.
- Remove only the active #45 cutover and Prediction Legacy rollback instructions from `docs/operations/frontend-gateway-deployment-reference.md`; keep generic Dashboard deployment/recovery and local fixture guidance.
- Add one top operator-facing bullet under `CHANGELOG.md` `## 2026-08-13` before any merge.
- Do not modify route parsing, Gateway Legacy compatibility handling, Prediction Service, Dashboard, browser, acceptance registry, runtime configuration, data, SQLite, ports, processes, or launchd during the implementation task.
- The production route remains `service`; Prediction Legacy rollback is unsupported after deletion.
- Run only focused existing tests during implementation. Run `make acceptance` once as the separately authorized final #45 closure gate after reviewer approval; its suite excludes the 115 retired cutover cases.
- Only `make acceptance` `PASS` permits exact-SHA integration, publication, redeployment, runtime verification, and Issue #45 closure. Fix `FAIL`; report `BLOCKED` without substitutes.

---

### Task 1: Delete the completed cutover path

**Files:**
- Delete: `scripts/cutover_prediction_service.sh`
- Delete: `scripts/prediction_cutover_account_proof.py`
- Delete: `tests/test_prediction_cutover_launchd.py`
- Modify: `docs/operations/frontend-gateway-deployment-reference.md:52-91`
- Modify: `CHANGELOG.md:7`

**Interfaces:**
- Consumes: the approved retirement contract in `docs/superpowers/specs/2026-08-13-retire-prediction-cutover-design.md` and the existing steady-state Service/Gateway verification surfaces.
- Produces: no replacement runtime interface; the repository no longer exposes a #45 cutover entrypoint or supported Prediction Legacy rollback procedure.
- Preserves: Prediction route mode `service`, Gateway Service routing, generic Dashboard stack recovery, Prediction Service release/install behavior, browser checks, and authenticated live no-submit acceptance.

- [ ] **Step 1: Confirm the isolated baseline and exact active references**

Run from `/private/tmp/open_trader-retire-cutover-design`:

```bash
git status --short --branch
git rev-parse HEAD
rg -n --fixed-strings \
  -e 'cutover_prediction_service.sh' \
  -e 'prediction_cutover_account_proof.py' \
  scripts tests docs/operations README.md Makefile
rg -n -e '#45' -e '--target legacy' -e 'Prediction owner rollback' \
  docs/operations/frontend-gateway-deployment-reference.md
```

Expected: the worktree is clean at the approved plan lineage; active filename references occur only in the two retiring scripts, the retiring test, and the #45 block in the Gateway deployment reference. The second search identifies that same active #45/Legacy rollback block.

- [ ] **Step 2: Delete the three retired files and the active runbook block**

Delete the exact files:

```bash
git rm \
  scripts/cutover_prediction_service.sh \
  scripts/prediction_cutover_account_proof.py \
  tests/test_prediction_cutover_launchd.py
```

In `docs/operations/frontend-gateway-deployment-reference.md`, remove the complete contiguous block beginning with:

```text
上面的通用安装器只负责普通 Gateway + Legacy stack 部署，不是 Prediction Service
```

and ending with:

```text
for the #45 operation.
```

Do not replace that block. The preceding generic `scripts/install_dashboard_launchd.sh` instructions must flow directly into `完整卸载三个固定 label（重复运行安全）：`. Do not edit the later temporary-port fixture workflow or its isolated `mode=legacy` route fixture.

- [ ] **Step 3: Prove the retired path and active rollback instructions are absent**

Run:

```bash
test ! -e scripts/cutover_prediction_service.sh
test ! -e scripts/prediction_cutover_account_proof.py
test ! -e tests/test_prediction_cutover_launchd.py

if rg -n --fixed-strings \
  -e 'cutover_prediction_service.sh' \
  -e 'prediction_cutover_account_proof.py' \
  scripts tests docs/operations README.md Makefile; then
  echo 'retired cutover filename remains active' >&2
  exit 1
fi

if rg -n -e '#45' -e '--target legacy' -e 'Prediction owner rollback' \
  docs/operations/frontend-gateway-deployment-reference.md; then
  echo 'active #45 or Legacy rollback instruction remains' >&2
  exit 1
fi

git diff --exit-code HEAD -- \
  Makefile \
  src/open_trader/prediction_arbitrage_acceptance.py \
  tests/e2e/prediction-market.spec.ts
```

Expected: every command exits 0 with no output. The deleted filenames and active rollback instructions have no operational reference, while the browser and live no-submit acceptance definitions are byte-for-byte unchanged.

- [ ] **Step 4: Run the retained Prediction Service launchd/release tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_prediction_service_launchd.py \
  tests/test_prediction_release.py \
  tests/test_prediction_release_launchd.py
```

Expected: exit 0; all selected Prediction Service launchd and release tests pass with zero failures. `tests/test_prediction_cutover_launchd.py` is absent and contributes none of its 115 retired cases.

- [ ] **Step 5: Run the retained Frontend Gateway and acceptance-registry tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_frontend_gateway.py

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_acceptance.py::test_prediction_live_acceptance_reports_authenticated_no_submit_evidence
```

Expected: both commands exit 0 with zero failures; the second command reports exactly `1 passed`. Existing `websockets.legacy` deprecation warnings may remain, but no new warning or error is accepted.

- [ ] **Step 6: Check retained shell syntax and whitespace**

Run:

```bash
for script in \
  scripts/install_prediction_service_launchd.sh \
  scripts/uninstall_prediction_service_launchd.sh \
  scripts/install_dashboard_launchd.sh
do
  bash -n "$script"
done
git diff --check
```

Expected: exit 0 and no output. No remaining installer has a shell syntax or whitespace error.

- [ ] **Step 7: Add the top dated operator changelog entry**

Immediately after `## 2026-08-13`, add:

```markdown
- Completed #45's one-time Prediction owner cutover command, Account proof
  helper, dedicated 115-case cutover suite, and Legacy rollback runbook have
  been retired. The production route remains Service-owned and Legacy rollback
  is unsupported. Retained Prediction Service, Gateway, and live no-submit
  registry checks pass; final `make acceptance` and exact-SHA deployment remain
  the closure gate.
```

Do not edit or reorder prior 2026-08-13 entries.

- [ ] **Step 8: Verify exact diff scope and commit the deletion candidate**

Run:

```bash
git diff --check
git diff --name-status HEAD | sort
test -z "$(git diff --diff-filter=A --name-only HEAD)"
git status --short
```

Expected diff, with no additional path:

```text
D scripts/cutover_prediction_service.sh
D scripts/prediction_cutover_account_proof.py
D tests/test_prediction_cutover_launchd.py
M CHANGELOG.md
M docs/operations/frontend-gateway-deployment-reference.md
```

Stage and commit exactly those paths:

```bash
git add CHANGELOG.md docs/operations/frontend-gateway-deployment-reference.md
git add -u \
  scripts/cutover_prediction_service.sh \
  scripts/prediction_cutover_account_proof.py \
  tests/test_prediction_cutover_launchd.py
git commit -m 'chore: retire prediction cutover machinery'
```

Verify the committed candidate:

```bash
CANDIDATE_SHA="$(git rev-parse HEAD)"
test -n "$CANDIDATE_SHA"
test -z "$(git status --porcelain)"
git show --check --stat --oneline "$CANDIDATE_SHA"
git diff-tree --no-commit-id --name-status -r "$CANDIDATE_SHA" | sort
```

Expected: one clean implementation commit with only the three deletions and two documentation modifications shown above.

---

## Handoff: review, acceptance, integration, deployment, and closure

These steps belong to the coordinating main agent after Task 1. They are not part of the deletion implementation turn.

- [ ] **Review the committed deletion against the approved spec**

Resolve the implementation base and delegate a read-only review of the complete candidate diff against `docs/superpowers/specs/2026-08-13-retire-prediction-cutover-design.md`:

```bash
IMPLEMENTATION_BASE_SHA="$(git rev-parse "$(git log --format=%H --grep='^chore: retire prediction cutover machinery$' -1)^")"
git diff --check "$IMPLEMENTATION_BASE_SHA"..HEAD
git diff --stat "$IMPLEMENTATION_BASE_SHA"..HEAD
```

The reviewer must check exact deletion scope, runbook boundaries, changelog truthfulness, zero replacement code/tests, retained browser/live definitions, and the `service`-route/unsupported-Legacy-rollback contract.

Expected: no actionable findings. Send every actionable finding to `worker`, rerun Task 1 Steps 3-6 and 8 after each fix, then rerun `reviewer` until clean.

- [ ] **Freeze the reviewed candidate and run the final acceptance gate**

Run only after reviewer approval:

```bash
RELEASE_ROOT=/private/tmp/open_trader-retire-cutover-design
PYTHON_BIN=/Users/ray/projects/open_trader/.venv/bin/python
ACCEPTED_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD)"
test -z "$(git -C "$RELEASE_ROOT" status --porcelain)"
git -C "$RELEASE_ROOT" diff --check
cd "$RELEASE_ROOT"
PYTHON_BIN="$PYTHON_BIN" make acceptance
```

Expected: `make acceptance` exits 0 with final status exactly `PASS`. On `FAIL`, return to `worker`, rerun focused checks and `reviewer`, and repeat the gate. On `BLOCKED`, report the blocker and stop without curl, mocks, fixtures, screenshots, or unit tests as substitutes.

- [ ] **Fast-forward the exact accepted SHA into `main` and publish it**

Run only after acceptance `PASS` and without changing the accepted worktree:

```bash
RELEASE_ROOT=/private/tmp/open_trader-retire-cutover-design
ACCEPTED_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD)"
MAIN_ROOT=/Users/ray/projects/open_trader
test -z "$(git -C "$MAIN_ROOT" status --porcelain)"
git -C "$MAIN_ROOT" merge --ff-only "$ACCEPTED_SHA"
test "$(git -C "$MAIN_ROOT" rev-parse HEAD)" = "$ACCEPTED_SHA"
git -C "$MAIN_ROOT" push origin main
REMOTE_SHA="$(git -C "$MAIN_ROOT" ls-remote origin refs/heads/main | awk '{print $1}')"
test "$REMOTE_SHA" = "$ACCEPTED_SHA"
```

Expected: local `main`, `origin/main`, and the accepted SHA are identical. The implementation commit already contains the required dated changelog entry.

- [ ] **Restart only Gateway and Prediction Service at the exact accepted SHA**

Record the old steady-state PIDs and restart boundary. Keep Legacy absent, restart the existing Frontend Gateway label in place, and reinstall Prediction Service from integrated `main`:

```bash
MAIN_ROOT=/Users/ray/projects/open_trader
RUNTIME_ROOT=/Users/ray/projects/open_trader
PYTHON_BIN=/Users/ray/projects/open_trader/.venv/bin/python
ACCEPTED_SHA="$(git -C /private/tmp/open_trader-retire-cutover-design rev-parse HEAD)"
DEPLOY_MARKER=/private/tmp/open-trader-retire-cutover-deploy.marker
OLD_PID_RECORD=/private/tmp/open-trader-retire-cutover-old-pids
: > "$DEPLOY_MARKER"
test "$(git -C "$MAIN_ROOT" rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git -C "$MAIN_ROOT" status --porcelain)"

DOMAIN="gui/$(id -u)"
OLD_GATEWAY_PID="$(launchctl print "$DOMAIN/com.open-trader.frontend-gateway" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
OLD_SERVICE_PID="$(launchctl print "$DOMAIN/com.open-trader.prediction-service" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
test -n "$OLD_GATEWAY_PID"
test -n "$OLD_SERVICE_PID"
printf '%s %s\n' "$OLD_GATEWAY_PID" "$OLD_SERVICE_PID" > "$OLD_PID_RECORD"

if LEGACY_OUTPUT="$(launchctl print "$DOMAIN/com.open-trader.legacy-dashboard" 2>&1)"; then
  echo 'Legacy launchd label is unexpectedly loaded' >&2
  exit 1
fi
case "$LEGACY_OUTPUT" in
  *'Could not find service'*) ;;
  *) printf '%s\n' "$LEGACY_OUTPUT" >&2; exit 1 ;;
esac
test -z "$(lsof -nP -tiTCP:8767 -sTCP:LISTEN 2>/dev/null)"

launchctl kickstart -k "$DOMAIN/com.open-trader.frontend-gateway"

"$MAIN_ROOT/scripts/install_prediction_service_launchd.sh" \
  --mode production \
  --repo-root "$MAIN_ROOT" \
  --runtime-root "$RUNTIME_ROOT" \
  --python "$PYTHON_BIN" \
  --config "$RUNTIME_ROOT/config/prediction_arbitrage.json" \
  --notifier-config "$RUNTIME_ROOT/config/daily_premarket.env" \
  --release-manifest "$MAIN_ROOT/ops/prediction-service-release.json" \
  --expected-sha "$ACCEPTED_SHA"
```

Expected: only Frontend Gateway and Prediction Service restart. No Dashboard stack installer, Legacy start, Account installer, Account restart, route write, cutover, or rollback runs. The existing route remains `service`, Legacy remains absent, Account remains untouched, and this exact-SHA restart does not require another acceptance run.

- [ ] **Verify Gateway and Service plus explicit Legacy absence**

Run:

```bash
MAIN_ROOT=/Users/ray/projects/open_trader
RUNTIME_ROOT=/Users/ray/projects/open_trader
PYTHON_BIN=/Users/ray/projects/open_trader/.venv/bin/python
ACCEPTED_SHA="$(git -C /private/tmp/open_trader-retire-cutover-design rev-parse HEAD)"
DEPLOY_MARKER=/private/tmp/open-trader-retire-cutover-deploy.marker
OLD_PID_RECORD=/private/tmp/open-trader-retire-cutover-old-pids
GATEWAY_LABEL=com.open-trader.frontend-gateway
SERVICE_LABEL=com.open-trader.prediction-service
DOMAIN="gui/$(id -u)"

read -r OLD_GATEWAY_PID OLD_SERVICE_PID < "$OLD_PID_RECORD"
GATEWAY_PID="$(launchctl print "$DOMAIN/$GATEWAY_LABEL" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
SERVICE_PID="$(launchctl print "$DOMAIN/$SERVICE_LABEL" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
test "$GATEWAY_PID" != "$OLD_GATEWAY_PID"
test "$SERVICE_PID" != "$OLD_SERVICE_PID"

for item in "$GATEWAY_PID:8766" "$SERVICE_PID:8769"
do
  pid="${item%%:*}"
  port="${item##*:}"
  test -n "$pid"
  test "$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')" = "$MAIN_ROOT"
  test "$(lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -Fp | sed -n 's/^p//p')" = "$pid"
done

if LEGACY_OUTPUT="$(launchctl print "$DOMAIN/com.open-trader.legacy-dashboard" 2>&1)"; then
  echo 'Legacy launchd label is unexpectedly loaded after redeploy' >&2
  exit 1
fi
case "$LEGACY_OUTPUT" in
  *'Could not find service'*) ;;
  *) printf '%s\n' "$LEGACY_OUTPUT" >&2; exit 1 ;;
esac
test -z "$(lsof -nP -tiTCP:8767 -sTCP:LISTEN 2>/dev/null)"

GATEWAY_HEALTH="$(curl -fsS http://127.0.0.1:8766/healthz)"
SERVICE_HEALTH="$(curl -fsS http://127.0.0.1:8769/healthz)"
"$PYTHON_BIN" - "$ACCEPTED_SHA" "$MAIN_ROOT" "$GATEWAY_HEALTH" "$SERVICE_HEALTH" <<'PY'
import json
import sys

expected_sha, expected_cwd, gateway_raw, service_raw = sys.argv[1:]
gateway = json.loads(gateway_raw)
service = json.loads(service_raw)
assert gateway["module"] == "frontend_gateway"
assert gateway["cwd"] == expected_cwd
assert gateway["git_sha"] == expected_sha
assert gateway["prediction_route_mode"] == "service"
assert gateway["prediction_upstream_status"] == "ok"
assert gateway["account_upstream_status"] == "ok"
assert service["module"] == "prediction_service"
assert service["cwd"] == expected_cwd
assert service["git_sha"] == expected_sha
assert service["mode"] == "production"
assert service["production_owner"] is True
assert service["mutations"] == "enabled"
print("exact accepted runtime verified")
PY

for log in \
  "$RUNTIME_ROOT/logs/frontend_gateway/launchd.out.log" \
  "$RUNTIME_ROOT/logs/prediction_service/launchd.out.log"
do
  test -e "$log"
  test "$log" -nt "$DEPLOY_MARKER"
done

rg -n 'frontend_gateway_runtime:' \
  "$RUNTIME_ROOT/logs/frontend_gateway/launchd.out.log" | tail -1
tail -n 50 \
  "$RUNTIME_ROOT/logs/prediction_service/launchd.out.log" \
  "$RUNTIME_ROOT/logs/prediction_service/launchd.err.log"
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: new Gateway and Service PIDs own only 8766 and 8769, both cwd values are `/Users/ray/projects/open_trader`, both health payloads report `$ACCEPTED_SHA`, Gateway reports `prediction_route_mode=service` with healthy Prediction and Account upstreams, Legacy `launchctl print` fails with `Could not find service`, no process listens on 8767, only Gateway/Prediction Service logs are inspected and are newer than the deployment boundary, the Python verifier prints `exact accepted runtime verified`, and the final command prints `200`.

- [ ] **Close Issue #45 only after publication and runtime proof**

Run:

```bash
MAIN_ROOT=/Users/ray/projects/open_trader
ACCEPTED_SHA="$(git -C "$MAIN_ROOT" rev-parse HEAD)"
gh issue close 45 --comment "Completed at $ACCEPTED_SHA: final acceptance PASS, one-time cutover machinery retired, origin/main matches, and the exact SHA is redeployed with Service route, PID/cwd/SHA/fresh-log/HTTP proof. Legacy Prediction rollback is unsupported."
test "$(gh issue view 45 --json state --jq .state)" = "CLOSED"
```

Expected: Issue #45 reports `CLOSED`. Preserve the accepted SHA and runtime evidence in the final handoff; do not describe the deleted Legacy rollback as available.
