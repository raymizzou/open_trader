# 发布部署清单(新 SHA 上生产)

本清单把"新 SHA 发布到本机 launchd 生产"固定为四步。每一步都以仓库现成的
install 脚本为准;任何一步失败即停止,不要临场发明替代做法。

背景:#110(2026-09-02)部署事故中,有人用 PlistBuddy 按下标手工改
`ProgramArguments`,漏改了 `PYTHONPATH`、`--release-manifest`、`--static-dir`
三处,服务经 venv 可编辑安装继续加载旧发布 `src`,而 `healthz` 的
`cwd`/`git_sha` 取自新目录,Production Smoke 全绿——服务实际跑旧代码 1.5 小时。
为此:healthz 现在暴露 `code_root`(及 account 的 `worker_code_root`),冒烟会断言
它位于 `EXPECTED_ROOT` 之下(#113)。

## 红线:改 plist 只许经 install 脚本

> **警示(先读)**:launchd plist 只允许通过仓库现成的 install 脚本从模板**整体
> 重写**生成;**严禁**手工用 PlistBuddy(或任何等价工具)按下标修改
> `ProgramArguments` 的某一项。模板的 `ProgramArguments`、`EnvironmentVariables`
> 中的 `PYTHONPATH`、以及 CLI 的 `--release-manifest`/`--static-dir` 等键是**一组
> 必须同时指向同一发布目录的键**,逐项手改极易漏改其中之一,造成"服务跑旧代码、
> 冒烟全绿"的假部署(#110/#113)。核对 plist 只读,不手改。

## PR 合并与发布边界

正式交付先按[验证手册](../docs/operations/agent-verification.md#first-reviewable-implementation)
建立独立分支/worktree，完成适用开发与安全检查；Main 依据长期授权仅暂存、提交和
推送任务文件，创建普通（非 Draft）PR，无需再次询问推送或 PR 权限，然后进行独立
审查与 CI。范围内修复先更新同一 PR，再刷新审查；最终审查、CI 和反馈门禁
完成后，由用户明确批准 GitHub 合并。手册中的明确 local-only/no-push/
do-not-publish/draft 覆盖指令仍适用。本地 `main` 只同步远端，不能通过本地合并或
直接推送绕过 PR。
PR head、GitHub 合成 PR merge commit、最终 GitHub `main` commit 是不同身份；
发布选定最后一种完整 SHA，核对其 CI 后，在明确授权的部署准备中运行 Candidate
Acceptance。可接受该 SHA 的 main `push` 或仅限 `refs/heads/main` 的
`workflow_dispatch` CI；取两种事件中最新的 exact-SHA run。较新的 pending、failed 或
cancelled run 阻止复用旧绿色结果，artifact 必须保留并匹配实际事件。自动 CI 缺失时，
在相应授权下执行 `gh workflow run ci.yml --ref main`，核对实际 SHA 并等待完整可信证据；
手动入口不接受任意 SHA。四个后端分区、portable、app 15368 的 `required`、当前 attempt、
artifact、retirement policy、lock 与 runtime 身份检查不变。PR CI 不能代替该 SHA 的 Candidate `PASS`，合并不自动运行验收或部署。

验收失败时先审计全部错误及下游影响，范围内修复走独立分支、检查、任务分支/PR
更新、独立审查、CI 与反馈门禁、用户批准合并，再对新的最终 main SHA 验收；
不直接修补 main 或生产。发布/tag 创建、
生产变更、owner 切换、回滚各按其授权范围执行。保留不可变目录、精确 SHA/
`code_root`、单一 owner、备份与回滚证据；`ROLLBACK` 结果本身不授权执行回滚。
[仓库保护设计](../docs/operations/repository-protection.md)尚未启用，另行审批。

## 四步发布流程

以下记号:`<SHA>` 为待发布 40 位 commit SHA;`<新发布>` 为不可变发布目录;
`<运行时根>` 为共享运行时根(数据/配置所在,跨发布不变)。

### 第 1 步:建不可变发布目录

```bash
git clone <本仓路径> <新发布>
cd <新发布>
git checkout --detach <SHA>
git status --porcelain --untracked-files=all   # 必须为空(clean)
git rev-parse HEAD                              # 必须等于 <SHA>
```

发布目录中的源码和 Git 状态一经确认即保持不变；安装器仍会按既有脚本写入该发布目录下
的 ignored runtime logs。后续步骤不编辑源码、不改变 Git 状态。

### 第 2 步:按范围运行 install 脚本,把选定服务指到新发布

`RELEASE_SERVICES` 是 Host Readiness 和 Production Smoke 的范围选择器，默认值为
`gateway legacy account prediction`，用于保持全栈兼容。可选值是 `gateway`、`legacy`、
`account`、`prediction`，也可以用空格指定组合，例如 `gateway`、`prediction`、
`account`、`legacy`、`gateway prediction` 或 `gateway legacy`。同一次发布中选定的服务
必须使用同一个 `<SHA>` 和 `<新发布>`；未选定服务可以继续运行较旧的 clean immutable
release。运维应确认未选定服务的 PID 和版本仍未变化，但不要把它们标记为本次已更新或
exact-SHA 已验收。

正式安装的前置条件是 Candidate Acceptance `PASS`、Host Readiness `READY` 和单独明确的
部署授权。`RELEASE_SERVICES` 只决定 readiness/Smoke 的检查范围；每个 installer 仍须
使用与范围匹配的脚本和 `--mode`，不会由 Makefile 自动执行安装。
全新主机上尚无旧实例时，用 `make host-readiness FIRST_DEPLOY=1`：必须没有任何
`com.open-trader.*` launchd 服务或 plist，且所选服务的监听端口均未被占用。
该模式仅跳过旧 Account 状态检查，其他预检仍须通过；现有主机沿用默认模式。

每个脚本先 `--dry-run` 核对输出,确认无误后去掉 `--dry-run` 正式执行。
以脚本实际参数为准(先读脚本 usage);不要凭记忆抄参数。
克隆出的发布目录不包含自身 `.venv`,而 Prediction 与 Account API 默认是
`shadow` 模式,不适用于本次生产流程;因此以下示例都显式指定共享运行时 Python 与所需模式。

1. Prediction Service(8769)，当 `prediction` 被选中时：

   ```bash
   scripts/install_prediction_service_launchd.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode production \
     --release-manifest <新发布>/ops/prediction-service-release.json \
     --expected-sha <SHA>
   scripts/install_prediction_service_launchd.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode production \
     --release-manifest <新发布>/ops/prediction-service-release.json \
     --expected-sha <SHA>
   ```

2. Frontend Gateway(8766)+ Legacy Dashboard(8767) stack，当 `gateway legacy` 被选中时：

   ```bash
   scripts/install_dashboard_launchd.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode stack
   scripts/install_dashboard_launchd.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode stack
   ```

   只更新已有 stack 中的 Gateway 时使用 `--mode gateway`。它要求
   `<运行时根>/config/prediction-route.json` 已存在，只写入并重启 Gateway，保留
   Legacy、Account、Prediction 的 plist、PID、日志和 route state：

   ```bash
   scripts/install_dashboard_launchd.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode gateway
   scripts/install_dashboard_launchd.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode gateway
   ```

   只更新 Legacy 时使用 `--mode legacy`：

   ```bash
   scripts/install_dashboard_launchd.sh --dry-run \
     --repo-root <新发布> --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python --mode legacy
   scripts/install_dashboard_launchd.sh \
     --repo-root <新发布> --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python --mode legacy
   ```

   Gateway 与 Legacy 的组合才使用上面的 `--mode stack`。Gateway-only 不是 stack migration；
   未知的 `8766` listener 或缺少既有 route state 属于切换前置检查，会在写入前拒绝操作。
   Gateway readiness 发生在目标启动后，失败时 installer 返回非零并报告原因，不会自动
   bootstrap 单进程或宣称已回滚。

3. Account API(8768)与 account-sync worker，当 `account` 被选中时：

   ```bash
   scripts/install_account_release.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python
   scripts/install_account_release.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python
   ```

   `install_account_release.sh` 使用现有 worker-first 顺序：先更新 worker 并等待新
   发布，再更新 API 并做同一 SHA 的交叉核对。不要用 API 和 worker 的独立命令替代该
   release wrapper。

install 脚本会从 `<新发布>/ops/launchd/*.plist.template` 整体重写
`~/Library/LaunchAgents` 下的 plist——这正是红线所要求的唯一改法。

### 第 3 步:由 installer 完成范围内一次重启

正式 installer 会在自身流程中完成选定服务的停止、启动和 health 等待。Gateway-only
只重启 Gateway；`gateway legacy` 才使用 stack 的既有顺序；Account 通过
`install_account_release.sh` 按 worker-first 顺序更新 worker 和 API。不要例行手工重复
`bootout`/`bootstrap`，也不要为同一 SHA 追加一次部署、验收、再部署循环。

Prediction Service 首次拉起会预热大库，health 从启动到 `running` 约需 35-60 秒；不要在
此期间判定失败。

### 第 4 步:Production Smoke 绑定新发布验证

```bash
make production-smoke \
  RELEASE_SERVICES='<选定服务，例如 gateway 或 gateway prediction>' \
  REPOSITORY_ROOT=<运行时根> \
  PYTHON_BIN=<运行时根>/.venv/bin/python \
  PLAYWRIGHT_NODE_PATH=<运行时根>/node_modules \
  EXPECTED_SHA=<SHA> \
  EXPECTED_ROOT=<新发布> \
  EXPECTED_RUNTIME_ROOT=<运行时根>
```

受支持的 Prediction 生产和 Shadow 入口永久暂停非 LP 业务。Smoke 的
`N_LEG_PAUSED` 接受历史值 0 或 1；省略、0 和 1 都要求 health 返回
`N_LEG_PAUSED`，核对可读的 LP dashboard，并跳过
`/api/prediction-arbitrage/state`。非法输入仍拒绝。
installer 可以保留历史原始配置，但 `--n-leg-paused 0` 不再恢复非 LP 业务。
这不改变部署、凭据、启动准入或交易授权边界。

必须以 `HEALTHY` 收尾。冒烟只核对 `RELEASE_SERVICES` 选定服务的 `/healthz`、进程、监听器
和日志。选中 Prediction 时，所有兼容暂停配置都必须满足暂停 health 和可读的 LP
dashboard，且跳过 N-Leg state 请求。running、缺失或矛盾的暂停证据仍阻断。除既有的
`cwd`/`git_sha` 外,还要求 `code_root`(account 另有 `worker_code_root`)存在且
位于 `EXPECTED_ROOT` 之下——这是防止"服务加载旧发布代码"的关键断言
(#110/#113)。未选定服务可以保持较旧版本；应独立确认其 PID/版本未变化，但不把它们
标记为本次已更新或 exact-SHA 已验收。Prediction 选中时保留 N_LEG 状态契约和浏览器
只读写入护栏；浏览器集成检查在所有 scope 中都保留。任何 `BLOCKED`/`ROLLBACK` 都按
失败处理，先做只读审计，再决定 fix-forward 或回滚；不要在未通过时宣称部署完成。

`RELEASE_SERVICES` 只改变检查和部署范围，不改变交易语义或任何业务规则。


## 迁移前存量持仓恢复

`prediction-arb recover-external-positions` 已退役，返回退出码 2 和
`N_LEG_PAUSED`，不会取得运行时/执行锁、读取凭据或登记事故确认。
此处不再提供可执行的恢复命令，也没有已授权的替代运维流程。

遇到 `unknown_external_state` 时，保留事故、账户身份、持仓、精确订单 ID 和
UNKNOWN 原始证据，停止依赖该恢复步骤的操作。不要删除事故、清空数据库、伪造
执行记录或通过手工 SQL 解除门禁。先只读审计并提出明确的离线恢复范围；取得
单独批准后，才可制定并验证停机 owner、备份、账户/订单身份和数量边界的恢复方案。
显式 Python 库接口仍用于隔离诊断，但其存在不授权操作生产数据。既有切换迁移、
整库恢复及其他已授权维护继续遵守各自的合同；本节不引入新维护协议。
