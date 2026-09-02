# 发布部署清单(新 SHA 上生产)

本清单把"新 SHA 发布到本机 launchd 生产"固定为五步。每一步都以仓库现成的
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

## 五步发布流程

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

发布目录一经建好即为不可变:后续所有步骤只读它,不再有任何写操作。

### 第 2 步:捕获部署前基线 JSON

正式 install 脚本可能重启服务,因此必须先捕获基线。在部署动作(bootout/bootstrap)之前,
从当前 Prediction 只读状态接口导出
`current_execution`/`last_execution` 基线并妥善保存(脱敏后归档):

```bash
curl -fsS http://127.0.0.1:8769/api/prediction-arbitrage/state \
  | python3 -m json.tool > <基线文件>.json
```

第 5 步的 `PRE_DEPLOY_SUBMISSION_BASELINE` 指向该文件;冒烟会在浏览器压测前后
各比对一次,防止发布窗口内发生计划外提交。

### 第 3 步:按序运行 install 脚本,把五个服务指到新发布

每个脚本先 `--dry-run` 核对输出,确认无误后去掉 `--dry-run` 正式执行。
以脚本实际参数为准(先读脚本 usage);不要凭记忆抄参数。
克隆出的不可变发布目录不包含自身 `.venv`,而 Prediction 与 Account API 默认是
`shadow` 模式,不适用于本次生产流程;因此以下示例都显式指定共享运行时 Python 与所需模式。

1. Prediction Service(8769):

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

2. Frontend Gateway(8766)+ Legacy Dashboard(8767)stack:

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

3. Account API(8768)与 account-sync worker:

   ```bash
   scripts/install_account_api_launchd.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode production
   scripts/install_account_api_launchd.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python \
     --mode production
   scripts/install_account_sync_launchd.sh --dry-run \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python
   scripts/install_account_sync_launchd.sh \
     --repo-root <新发布> \
     --runtime-root <运行时根> \
     --python <运行时根>/.venv/bin/python
   ```

install 脚本会从 `<新发布>/ops/launchd/*.plist.template` 整体重写
`~/Library/LaunchAgents` 下的 plist——这正是红线所要求的唯一改法。

### 第 4 步:bootout / bootstrap 五个服务

install 脚本自身会完成停止/重启与 health 等待;只有当某个服务需要显式重启时,
才手工执行(标签以 plist 内 `Label` 为准):

```bash
launchctl bootout gui/$(id -u)/<label> || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

- `bootstrap` 偶发 `Input/output error` 时,等 5 秒后原样重试一次,即可成功。
- Prediction Service 首次拉起会预热大库,health 从启动到 `running` 约需
  35-60 秒;不要在此期间判定失败。

### 第 5 步:Production Smoke 绑定新发布验证

```bash
make production-smoke \
  EXPECTED_SHA=<SHA> \
  EXPECTED_ROOT=<新发布> \
  EXPECTED_RUNTIME_ROOT=<运行时根> \
  PRE_DEPLOY_SUBMISSION_BASELINE=<基线文件>.json
```

必须以 `HEALTHY` 收尾。冒烟会逐一核对四个服务的 `/healthz`:除既有的
`cwd`/`git_sha` 外,还要求 `code_root`(account 另有 `worker_code_root`)存在且
位于 `EXPECTED_ROOT` 之下——这是防止"服务加载旧发布代码"的关键断言
(#110/#113)。任何 `BLOCKED`/`ROLLBACK` 都按失败处理,先做只读审计,再决定
fix-forward 或回滚;不要在未通过时宣称部署完成。
