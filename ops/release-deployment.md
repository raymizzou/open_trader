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

若本次发布要保持 N_LEG 暂停，在同一条命令增加 `N_LEG_PAUSED=1`。Smoke
会要求 Prediction Service 的 health 返回 `N_LEG_PAUSED`，核对 LP dashboard
仍可读，并跳过 `/api/prediction-arbitrage/state`；默认 `N_LEG_PAUSED=0`
继续核对正常 N_LEG state。暂停值由
`scripts/install_prediction_service_launchd.sh --n-leg-paused 1` 写入
launchd 环境；后续安装省略参数时保留已有值，恢复必须明确指定
`--n-leg-paused 0`。

必须以 `HEALTHY` 收尾。冒烟只核对 `RELEASE_SERVICES` 选定服务的 `/healthz`、进程、监听器
和日志。选中 Prediction 时，`N_LEG_PAUSED=1` 必须同时满足暂停 health 和可读的 LP
dashboard，且跳过 N-Leg state 请求；`N_LEG_PAUSED=0` 必须满足 running health 和现有
N-Leg state 契约。除既有的
`cwd`/`git_sha` 外,还要求 `code_root`(account 另有 `worker_code_root`)存在且
位于 `EXPECTED_ROOT` 之下——这是防止"服务加载旧发布代码"的关键断言
(#110/#113)。未选定服务可以保持较旧版本；应独立确认其 PID/版本未变化，但不把它们
标记为本次已更新或 exact-SHA 已验收。Prediction 选中时保留 N_LEG 状态契约和浏览器
只读写入护栏；浏览器集成检查在所有 scope 中都保留。任何 `BLOCKED`/`ROLLBACK` 都按
失败处理，先做只读审计，再决定 fix-forward 或回滚；不要在未通过时宣称部署完成。

`RELEASE_SERVICES` 只改变检查和部署范围，不改变交易语义或任何业务规则。
