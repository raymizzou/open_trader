# Issue #16：Dashboard 双运行时 Acceptance 设计

## 背景

Phase 0.2 已将 Dashboard 切换为两个 loopback 进程：Frontend Gateway
监听 `127.0.0.1:8766`，Legacy Dashboard 监听 `127.0.0.1:8767`。现有
`dashboard_acceptance` 只从 `8766` 推导一个 PID、工作目录、Git SHA 和
`dashboard_runtime` 日志，因此 Gateway 存活不能证明 Legacy 仍可用，也不能
阻止两个端口运行不同代码。

## 目标

- acceptance 必须证明 `8766` 和 `8767` 都有唯一监听进程，且 PID 不同。
- 分别证明两个进程的模块身份、工作目录、Git SHA、源码状态、启动时间和
  新鲜 runtime 日志。
- 保留现有业务、账户、报价和桌面/移动浏览器验收路径，继续全部通过稳定的
  `8766` 入口执行。
- 让 Gateway 或 Legacy 任一缺失、身份错误、代码错误或日志过期都返回
  `FAIL`；浏览器/外部环境不可用仍按现有规则返回 `BLOCKED`。

## 非目标

- 不修改 Dashboard 页面、业务规则、账户同步、报价或 API schema。
- 不实现 Phase 0.4 的生产切换或完整回滚手册。
- 不为做双进程等价比较而再次请求大型 `/api/dashboard` payload。
- 不让 acceptance 为单进程 rollback 模式返回 `PASS`。

## 运行时检查

`dashboard_acceptance.main` 在现有业务检查之前执行一个双目标 preflight。目标
只是一组参数，不新增独立服务或运行时注册表：

| 目标 | 默认 URL | 健康模块 | runtime 日志 | 日志前缀 |
| --- | --- | --- | --- | --- |
| Gateway | `http://127.0.0.1:8766` | `frontend_gateway` | `logs/frontend_gateway/launchd.out.log` | `frontend_gateway_runtime: ` |
| Legacy | `http://127.0.0.1:8767` | `legacy_dashboard` | `logs/legacy_dashboard/launchd.out.log` | `dashboard_runtime: ` |

检查顺序如下：

1. 用 `lsof` 为两个 URL 找到唯一 LISTEN PID；缺失、多个 PID 或两个 PID
   相同立即失败。
2. 分别请求两个 `/healthz`，要求 schema version、`module`、`pid`、
   `started_at`、`cwd`、`git_sha` 和 `source_state` 存在且类型正确；模块身份
   必须与目标匹配。
3. 用 `lsof` 和 `ps` 读取实际 cwd 与进程启动时间，并与期望 worktree、健康
   响应和期望 SHA 比较。期望 SHA 仍来自 `--expected-sha` 或
   `--expected-root` 的 `HEAD`；源码状态仍由 Git status 判断为 clean。
4. 从各自日志中读取目标前缀的最新 runtime 记录，要求它是文件首条候选
   记录、PID/cwd/SHA/source state 与目标一致、启动时间不早于候选进程，且
   文件修改时间不早于候选进程。保留现有 traceback/错误标记检查。

两个目标的错误合并到同一个 `errors` 列表，仍由 `classify_result` 统一产生
`PASS`、`FAIL` 或 `BLOCKED`。运行时错误永远是 `FAIL`，不会因浏览器 blocker
而降级成 `BLOCKED`。

## 业务与浏览器路径

- `8766` 继续承载现有 `/api/dashboard`、`/api/quotes`、实时账户刷新以及
  桌面/移动浏览器检查。
- `8767` 只做轻量 `/healthz` schema/必需字段检查，避免重复大型 payload，也
  不对易变报价或时间戳做陈旧快照相等比较。
- 原有 `--url` 和 `--log` 保持为 Gateway 入口兼容参数；新增
  `--legacy-url` 和 `--legacy-log`，默认值指向 `8767` 与 Legacy 日志。

## 测试策略

在 `tests/test_dashboard_acceptance.py` 增加聚焦测试：

- 两个监听器 PID 缺失、重复或相同会失败。
- Gateway/Legacy 健康模块身份或必需字段错误会失败。
- 任一目标 cwd、Git SHA、source state、进程启动时间或 runtime 日志不匹配
  会失败。
- 日志包含旧前缀、旧 PID、旧时间或前置陈旧内容会失败。
- 双目标全部匹配时，现有 `PASS/FAIL/BLOCKED` 语义不变，业务 payload 仍只
  通过 `8766` 检查。

## 交付与运行验证

实现完成后先运行聚焦测试和直接双端口健康检查，最后执行一次 `make acceptance`。
只有 `PASS` 才继续：将相同 accepted SHA 部署到两个 launchd job，并复核两个
PID、cwd、Git SHA、启动时间、新鲜日志及 `http://127.0.0.1:8766/` 的 HTTP 200。
CHANGELOG 在合并前提交；本 issue 与父 issue #13 保持开放，直到用户明确授权
远端集成。
