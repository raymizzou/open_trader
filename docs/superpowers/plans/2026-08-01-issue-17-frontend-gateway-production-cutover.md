# Issue #17 Frontend Gateway Production Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Phase 0 by documenting, accepting, and redeploying the existing Frontend Gateway plus Legacy Dashboard production stack at one immutable Git SHA.

**Architecture:** Reuse the existing launchd installer, health endpoints, runtime records, and Dashboard acceptance gate. Change only operator documentation and the changelog, then prove the committed candidate through focused tests, the complete suite, direct production-stack checks, one final `make acceptance`, and exact-SHA redeployment.

**Tech Stack:** Markdown, Bash 3.2-compatible operator commands, macOS launchd, Python 3.12/pytest, curl, lsof, and the existing Open Trader acceptance modules.

## Global Constraints

- Start from local `main@5b337ffd43451831279627feb27f2dcb8c0879e0` in the isolated `feat/issue-17-frontend-gateway-production-cutover` worktree.
- The stable user and review URL is `http://127.0.0.1:8766/`; `127.0.0.1:8767` remains internal-only.
- Do not change Dashboard presentation, API contracts, strategy, reports, execution, notifications, account-sync, controllers, or worker cadence.
- Do not add a deployment wrapper, diagnostic collector, dependency, process manager, or new rollback implementation.
- Commit the dated `CHANGELOG.md` entry before freezing or accepting the candidate.
- Run `make acceptance` only after all implementation changes and other verification are complete; only `PASS` permits handoff.
- After `PASS`, redeploy the exact accepted SHA without source or domain-data changes, then verify both runtime identities and HTTP 200.
- Do not merge, push, or close Issues #14 through #17 without explicit user authorization.

---

### Task 1: Complete The Operator Documentation

**Files:**
- Modify: `README.md:70-83`
- Modify: `README.md:414-430`
- Modify: `README.md:483-529`
- Modify: `docs/operations/frontend-gateway-deployment-reference.md:1-64`
- Modify: `docs/operations/frontend-gateway-deployment-reference.md:159-209`
- Modify: `CHANGELOG.md:6-18`

**Interfaces:**
- Consumes: the existing `scripts/install_dashboard_launchd.sh` stack/single modes, Gateway and Legacy health schemas, and Issue #17 design.
- Produces: one non-contradictory README entrypoint, one detailed production runbook, and the required dated operator-facing changelog entry.

- [ ] **Step 1: Attach ignored local runtime dependencies to the worktree**

Run from the Issue #17 worktree:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
test -e config/prediction_arbitrage.json || \
  ln -s /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
    config/prediction_arbitrage.json

test -x .venv/bin/python
test -f config/prediction_arbitrage.json
git status --short
```

Expected: both ignored paths resolve, and Git reports no new tracked change.

- [ ] **Step 2: Make README use the launchd stack as the persistent Dashboard path**

Replace the current `Deploy Local Frontend Dashboard` instructions with this
standalone section:

```markdown
### Deploy the Local Dashboard Stack

The persistent macOS Dashboard runs as one launchd-managed stack:

```text
Browser → Frontend Gateway → Legacy Dashboard
          127.0.0.1:8766     127.0.0.1:8767
```

`http://127.0.0.1:8766/` is the only user and review URL. The Legacy listener
on `8767` owns the existing backend behavior and must remain loopback-only.

Install or refresh both processes with one command:

```bash
scripts/install_dashboard_launchd.sh --dry-run
scripts/install_dashboard_launchd.sh
```

Check both jobs, listeners, health identities, the forwarded quotes API, and
fresh startup logs:

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
curl -fsS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/api/quotes
tail -n 20 logs/frontend_gateway/launchd.out.log
tail -n 20 logs/legacy_dashboard/launchd.out.log
```

Restore the preserved single-process layout without deleting the stack plists:

```bash
scripts/install_dashboard_launchd.sh --mode single
```

See [Frontend Gateway 双进程部署参考](docs/operations/frontend-gateway-deployment-reference.md)
for cutover order, automatic failure recovery, temporary-port smoke checks,
exact-SHA acceptance, and complete uninstall instructions.
```

Remove the later `screen -S open_trader_dashboard_8766` persistence and stop
instructions so README does not offer a second production owner for `8766`.
In the prediction-market section, retain the wallet preflight and status check
but replace the repeated install/rollback list with a link to the standalone
Dashboard stack section.

- [ ] **Step 3: Add the production acceptance sequence to the operations reference**

Update the opening links to cover Issues #14, #15, #16, and #17. Add this
section after the existing runtime-record checks:

```markdown
## 生产验收与 exact-SHA 交付

所有文档和 CHANGELOG 先提交，随后冻结候选 SHA。最终顺序固定为：

1. 运行 Gateway、launchd stack 和双运行时 acceptance 聚焦测试；
2. 运行完整 pytest suite；
3. 从候选 worktree 部署 Gateway、Legacy Dashboard 及 acceptance 依赖的后台进程；
4. 直接验证两个 launchd job、两个 listener、两个 health identity 和一条经
   `8766` 转发的 `/api/quotes` 请求；
5. 运行一次最终 `make acceptance`；
6. 仅在 `PASS` 后重新部署完全相同的 accepted SHA；
7. 核对两个新 PID、cwd、SHA、source state、启动时间、新鲜 runtime 日志及
   `http://127.0.0.1:8766/` HTTP 200。

`FAIL` 必须修复并从候选验证重新开始；`BLOCKED` 必须报告实际外部或浏览器
阻塞，不能用 curl、fixture、mock 或单元测试替代。exact-SHA 重启未改变源码或
领域运行数据时，不重复运行 acceptance。
```

Keep the existing install, automatic rollback, temporary-port smoke, health
schema, and uninstall instructions unchanged.

- [ ] **Step 4: Add the dated operator-facing changelog entry**

Add this bullet at the top of the existing `2026-08-01` section:

```markdown
- 完成 Frontend Gateway Phase 0 生产交付：README 与运维手册现在明确稳定的 `8766` 用户入口、内部 `8767` Legacy Dashboard、单命令双进程 stack 安装、双运行时诊断及 `--mode single` 回滚，并要求最终 PASS 后重新部署完全相同的 accepted SHA。本阶段没有页面、策略、报告、执行或 worker 行为变化。
```

- [ ] **Step 5: Check documentation consistency**

```bash
rg -n 'Deploy the Local Dashboard Stack|127\.0\.0\.1:8766|127\.0\.0\.1:8767|--mode single' README.md
rg -n '生产验收与 exact-SHA 交付|make acceptance|FAIL|BLOCKED' \
  docs/operations/frontend-gateway-deployment-reference.md
rg -n '本阶段没有页面、策略、报告、执行或 worker 行为变化' CHANGELOG.md
if rg -n 'screen -S open_trader_dashboard_8766' README.md; then
  exit 1
fi
git diff --check
git diff -- README.md docs/operations/frontend-gateway-deployment-reference.md CHANGELOG.md
```

Expected: all required operator terms are present, the stale screen owner is
absent, no whitespace error is reported, and only the three intended files
changed.

- [ ] **Step 6: Commit the production handoff documentation**

```bash
git add README.md docs/operations/frontend-gateway-deployment-reference.md CHANGELOG.md
git commit -m "docs: complete frontend gateway production handoff"
```

---

### Task 2: Review And Freeze The Candidate

**Files:**
- Verify: `README.md`
- Verify: `docs/operations/frontend-gateway-deployment-reference.md`
- Verify: `CHANGELOG.md`
- Verify: `tests/test_frontend_gateway.py`
- Verify: `tests/test_frontend_gateway_cli.py`
- Verify: `tests/test_dashboard_launchd_stack.py`
- Verify: `tests/test_dashboard_web.py`
- Verify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: the committed Task 1 documentation and existing production stack behavior.
- Produces: a reviewed, clean candidate SHA plus exact focused and full-suite results.

- [ ] **Step 1: Review the complete Issue #17 diff**

Use the `code-review` skill with base
`5b337ffd43451831279627feb27f2dcb8c0879e0`. Check repository standards and
every Issue #17 acceptance criterion. If review finds a blocking documentation
gap, make the smallest correction, rerun Task 1 Step 5, and commit it before
continuing. Do not add runtime code for speculative convenience.

- [ ] **Step 2: Run focused Gateway, stack, health, and dual-runtime tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_frontend_gateway.py \
  tests/test_frontend_gateway_cli.py \
  tests/test_dashboard_launchd_stack.py \
  tests/test_dashboard_web.py::test_dashboard_healthz_reports_legacy_module_runtime \
  tests/test_dashboard_web.py::test_dashboard_healthz_reuses_startup_runtime_metadata \
  tests/test_dashboard_web.py::test_dashboard_dual_runtime_requires_loopback_host \
  tests/test_dashboard_acceptance.py::test_acceptance_main_reports_distinct_dual_runtime_pids \
  tests/test_dashboard_acceptance.py::test_acceptance_rejects_missing_legacy_listener \
  tests/test_dashboard_acceptance.py::test_acceptance_rejects_listener_cwd_and_running_sha \
  tests/test_dashboard_acceptance.py::test_make_acceptance_wires_gateway_and_legacy_runtime_logs \
  tests/test_dashboard_acceptance.py::test_acceptance_main_rejects_same_gateway_and_legacy_pid \
  tests/test_dashboard_acceptance.py::test_acceptance_business_and_browser_checks_stay_on_gateway \
  -q
```

Expected: every selected test passes. Record pytest's exact pass count for the
Issue #17 comment.

- [ ] **Step 3: Run the complete pytest suite against shared runtime data**

```bash
ISSUE17_ROOT="$PWD"
cd /Users/ray/projects/open_trader
PYTHONSAFEPATH=1 PYTHONPATH="$ISSUE17_ROOT:$ISSUE17_ROOT/src" \
  "$ISSUE17_ROOT/.venv/bin/python" -m pytest "$ISSUE17_ROOT/tests" -q
cd "$ISSUE17_ROOT"
```

Expected: the full suite passes. Record the exact pass count and duration.
Running from the repository root intentionally exposes the established shared,
ignored runtime snapshots while importing code and tests from the candidate.

- [ ] **Step 4: Freeze the clean candidate SHA**

```bash
git diff --check
test -z "$(git status --short)"
git rev-parse HEAD > /tmp/open_trader_issue17_candidate_sha
git show --stat --oneline HEAD
```

Expected: the worktree is clean and the temp file contains the committed
candidate SHA. Do not change source or documentation after this point unless a
verification failure requires a new committed candidate and a complete rerun.

---

### Task 3: Deploy And Directly Verify The Candidate Stack

**Files:**
- Execute: `scripts/install_dashboard_launchd.sh`
- Execute: `scripts/install_account_sync_launchd.sh`
- Execute: `scripts/install_daily_premarket_launchd.sh`
- Inspect: `logs/frontend_gateway/launchd.out.log`
- Inspect: `logs/legacy_dashboard/launchd.out.log`
- Inspect: `data/account_sync/controller_status.json`
- Inspect: `data/trend_controller/{CN,HK,US}/status.json`

**Interfaces:**
- Consumes: the clean candidate SHA and shared runtime root `/Users/ray/projects/open_trader`.
- Produces: a live candidate stack with two launchd jobs, distinct listeners, correct health identities, a successful forwarded API, and acceptance-owned background processes on the candidate SHA.

- [ ] **Step 1: Deploy every process used by final acceptance**

```bash
scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_account_sync_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
```

Expected: Dashboard stack, account-sync, and CN/HK/US trend controllers start
from this worktree. If listener ownership or readiness fails, use the existing
installer rollback, diagnose the real owner, and do not run `make acceptance`.

- [ ] **Step 2: Verify both jobs, listeners, health endpoints, and forwarding**

```bash
CANDIDATE_SHA="$(sed -n '1p' /tmp/open_trader_issue17_candidate_sha)"

launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard

GATEWAY_PID="$(lsof -nP -tiTCP:8766 -sTCP:LISTEN)"
LEGACY_PID="$(lsof -nP -tiTCP:8767 -sTCP:LISTEN)"
test -n "$GATEWAY_PID"
test -n "$LEGACY_PID"
test "$GATEWAY_PID" != "$LEGACY_PID"

curl -fsS http://127.0.0.1:8766/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8767/healthz | .venv/bin/python -m json.tool
curl -fsS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/api/quotes

lsof -a -p "$GATEWAY_PID" -d cwd -Fn
lsof -a -p "$LEGACY_PID" -d cwd -Fn
ps -p "$GATEWAY_PID" -o lstart=
ps -p "$LEGACY_PID" -o lstart=
tail -n 20 logs/frontend_gateway/launchd.out.log
tail -n 20 logs/legacy_dashboard/launchd.out.log

printf '%s\n' "$GATEWAY_PID" > /tmp/open_trader_issue17_gateway_pid
printf '%s\n' "$LEGACY_PID" > /tmp/open_trader_issue17_legacy_pid
```

Expected: health schemas identify `frontend_gateway` and `legacy_dashboard`;
both payloads report this worktree, `$CANDIDATE_SHA`, and `source_state:
clean`; Gateway reports `upstream_status: ok`; the forwarded API returns
`200`; the two cwd records match this worktree; and both startup logs contain
the current PID, SHA, cwd, start time, and clean source state.

- [ ] **Step 3: Verify acceptance-owned background status**

```bash
.venv/bin/python -m json.tool \
  /Users/ray/projects/open_trader/data/account_sync/controller_status.json
for market in CN HK US; do
  .venv/bin/python -m json.tool \
    "/Users/ray/projects/open_trader/data/trend_controller/$market/status.json"
done
```

Expected: every status has a live PID, fresh heartbeat, this worktree, and
`$CANDIDATE_SHA`. A controller business blocker may remain truthful, but stale
or mismatched process identity must be fixed before the final gate.

---

### Task 4: Run The Final Gate, Redeploy The Accepted SHA, And Hand Off

**Files:**
- Execute: `Makefile:14-49`
- Execute: `scripts/install_dashboard_launchd.sh`
- Inspect: `logs/frontend_gateway/launchd.out.log`
- Inspect: `logs/legacy_dashboard/launchd.out.log`
- External write: GitHub Issue #17 only

**Interfaces:**
- Consumes: the committed, reviewed, fully tested, directly verified candidate stack.
- Produces: `make acceptance: PASS`, exact-SHA post-acceptance runtime proof, HTTP 200 review URL, and an evidence comment on Issue #17.

- [ ] **Step 1: Run `make acceptance` as the final Dashboard gate**

```bash
make acceptance
```

Expected: complete pytest, prediction-market browser/live checks, drawdown
preflight, real account/quote refresh, and Dashboard dual-runtime/browser checks
all pass; final Dashboard JSON reports `status: PASS`, `errors: []`, no blocker,
and distinct Gateway and Legacy PIDs.

On `FAIL`, diagnose and fix the smallest root cause, add a focused regression
test for any behavior change, commit the new candidate, and repeat Tasks 2-4.
On `BLOCKED`, report the actual blocker and stop without presenting the task
for review.

- [ ] **Step 2: Prove the accepted SHA is unchanged and clean**

```bash
CANDIDATE_SHA="$(sed -n '1p' /tmp/open_trader_issue17_candidate_sha)"
ACCEPTED_SHA="$(git rev-parse HEAD)"
test "$ACCEPTED_SHA" = "$CANDIDATE_SHA"
test -z "$(git status --short)"
```

Expected: accepted SHA equals the frozen candidate and the worktree remains
clean. Do not modify source, documentation, or domain runtime data.

- [ ] **Step 3: Redeploy the exact accepted Dashboard stack**

```bash
scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Expected: launchd starts fresh Gateway and Legacy processes from the exact
accepted SHA. Do not rerun `make acceptance` when this restart made no source
or domain-data change.

- [ ] **Step 4: Verify new process identities, log freshness, and the review URL**

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
OLD_GATEWAY_PID="$(sed -n '1p' /tmp/open_trader_issue17_gateway_pid)"
OLD_LEGACY_PID="$(sed -n '1p' /tmp/open_trader_issue17_legacy_pid)"
NEW_GATEWAY_PID="$(lsof -nP -tiTCP:8766 -sTCP:LISTEN)"
NEW_LEGACY_PID="$(lsof -nP -tiTCP:8767 -sTCP:LISTEN)"

test -n "$NEW_GATEWAY_PID"
test -n "$NEW_LEGACY_PID"
test "$NEW_GATEWAY_PID" != "$NEW_LEGACY_PID"
test "$NEW_GATEWAY_PID" != "$OLD_GATEWAY_PID"
test "$NEW_LEGACY_PID" != "$OLD_LEGACY_PID"

launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -a -p "$NEW_GATEWAY_PID" -d cwd -Fn
lsof -a -p "$NEW_LEGACY_PID" -d cwd -Fn
ps -p "$NEW_GATEWAY_PID" -o lstart=
ps -p "$NEW_LEGACY_PID" -o lstart=

curl -fsS http://127.0.0.1:8766/healthz | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8767/healthz | .venv/bin/python -m json.tool
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/

stat -f '%Sm %N' logs/frontend_gateway/launchd.out.log
stat -f '%Sm %N' logs/legacy_dashboard/launchd.out.log
rg 'frontend_gateway_runtime:' logs/frontend_gateway/launchd.out.log | tail -n 1
rg 'dashboard_runtime:' logs/legacy_dashboard/launchd.out.log | tail -n 1
```

Expected: both PIDs are new and distinct; launchd, lsof cwd, health payload,
and runtime records agree on this worktree and `$ACCEPTED_SHA`; each log
timestamp/runtime record is newer than its process start; Gateway upstream is
`ok`; and the only review URL returns HTTP `200`.

- [ ] **Step 5: Comment on Issue #17 and stop before integration**

Before the external write, restate that the target is
`raymizzou/open_trader#17`. Comment with:

- focused and complete pytest pass counts and durations;
- final `make acceptance` result and accepted SHA;
- direct Gateway/Legacy job, listener, health, and forwarded-API results;
- both post-redeploy PIDs, cwd, SHA, source state, start time, and log freshness;
- `http://127.0.0.1:8766/` HTTP 200; and
- an explicit statement that no page, strategy, report, execution, or worker
  behavior changed.

Use:

```bash
gh issue comment 17 --repo raymizzou/open_trader --body-file \
  /tmp/open_trader_issue17_comment.md
```

Create the temporary body from the results captured in Tasks 2-4, verify it
contains no secret or credential, then post it. Keep Issues #14 through #17
open. Do not merge or push.

---

## Plan Self-Review

- README stable URL, internal port, single install command, dual-process diagnostics, and executable rollback map to Task 1 Step 2.
- The dated changelog and explicit no-behavior-change boundary map to Task 1 Step 4 and precede candidate freezing.
- Focused and complete pytest evidence maps to Task 2 Steps 2-3.
- Candidate deployment plus two jobs, listeners, health identities, and one forwarded API map to Task 3 Steps 1-2.
- `make acceptance` is the last source gate in Task 4 Step 1; `FAIL` and `BLOCKED` retain their required semantics.
- Exact-SHA redeployment, new PID/cwd/SHA/start/log proof, and HTTP 200 map to Task 4 Steps 2-4.
- Existing scripts and stdlib tools are reused; no runtime code, dependency, wrapper, screenshot, or new abstraction is planned.
- GitHub evidence is limited to Issue #17, and integration remains explicitly unauthorized.
