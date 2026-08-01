# Issue #15 Dashboard launchd Stack Design

## Goal

把现有单进程 Dashboard 安全迁移为两个独立 launchd job：浏览器继续访问
`127.0.0.1:8766` 的 Frontend Gateway，Legacy Dashboard 在
`127.0.0.1:8767` 提供既有 API。安装失败时自动恢复保留的旧单进程 job。

本设计实现 GitHub Issue #15，不改变 Dashboard 页面、API、业务规则、worker
cadence 或 Gateway 转发逻辑。

## Selected approach

复用现有入口：

- `scripts/install_dashboard_launchd.sh` 默认执行 `--mode stack`。
- `scripts/install_dashboard_launchd.sh --mode single` 执行明确的单进程回滚。
- `scripts/uninstall_dashboard_launchd.sh` 只执行完整卸载。

没有新增 wrapper 或 Python 编排器。新增 wrapper 会复制安装状态机；Python
编排器会为只需 launchctl、lsof、curl 和 plist 的本机操作引入额外代码路径。
一个 shell 安装器和现有 macOS 工具足以覆盖当前迁移。

## Jobs and runtime surfaces

| Role | launchd label | Port | Logs |
| --- | --- | --- | --- |
| Preserved single-process rollback | `com.open-trader.dashboard` | `8766` | `logs/dashboard/launchd.*.log` |
| Frontend Gateway | `com.open-trader.frontend-gateway` | `8766` | `logs/frontend_gateway/launchd.*.log` |
| Legacy Dashboard | `com.open-trader.legacy-dashboard` | `8767` | `logs/legacy_dashboard/launchd.*.log` |

Gateway 和 Legacy 使用独立 plist、PID、stdout 和 stderr。两者使用同一目标
repo/worktree 和 Python，但 Legacy 的 data、reports、daily config 与 prediction
config 仍从现有 runtime-root 规则解析。

Legacy 命令增加 `--public-url http://127.0.0.1:8766/`，保证通知和生成链接不泄漏
内部端口。Gateway 只接收静态目录、公开 origin 和 `8767` upstream 配置。

## Operator interface

现有参数继续保留，并增加：

```text
--mode stack|single    默认 stack
```

默认 stack 模式要求旧单进程 plist 已存在且可通过 `plutil` 检查。这是安全迁移的
回滚资产；缺失时安装器在修改 launchd 状态前退出，并提示先运行
`--mode single` 生成并验证它。

`--mode single` 渲染当前目标 SHA 的旧单进程 plist，停止两个新 job，启动旧 job，
并验证 `8766`。它保留三份 plist，方便再次切换。

## Stack cutover sequence

默认安装按固定顺序执行：

1. 渲染并 lint Gateway 与 Legacy 两份新 plist；lint 保留的旧单进程 plist。
2. 检查 `8766` 和 `8767` listener 所有权。
3. 确认当前公开 `8766` 页面仍返回 HTTP 200。
4. 写入两份新 plist，启动或重启 Legacy job。
5. 等待 Legacy `/healthz` 返回 HTTP 200，且 JSON `module` 为
   `legacy_dashboard`。
6. 只通过 launchctl 停止已确认拥有 `8766` 的旧单进程或旧 Gateway job。
7. 启动 Gateway job。
8. 等待 Gateway `/healthz` 返回 HTTP 200、`module` 为 `frontend_gateway`、
   `upstream_status` 为 `ok`，并确认公开 `/` 返回 HTTP 200。
9. 保留旧单进程 plist，但让旧 job 保持卸载状态。

已经运行 stack 时，同一命令仍按上述所有权规则重启 Legacy 和 Gateway；保留的
旧单进程 plist继续作为失败恢复目标。

## Listener ownership safety

安装器从 `launchctl print gui/$UID/<label>` 读取三个已知 job 的 PID，再与 `lsof`
返回的 listener PID 比较：

- `8766` 只允许无 listener、旧单进程 PID 或 Gateway PID。
- `8767` 只允许无 listener或 Legacy PID。
- 任一端口由其他 PID 占用时，在 bootout、plist 写入或 HTTP 探测前退出。

安装器从不直接 kill PID。所有停止操作都只能作用于三个固定 launchd label。
端口检查工具不可用或输出无法识别时按不安全处理并退出。

## Failure recovery

stack 模式注册统一失败处理：

1. bootout Gateway 和 Legacy 两个新 label；
2. 使用安装前保留的旧单进程 plist bootstrap/kickstart
   `com.open-trader.dashboard`；
3. 等待 `http://127.0.0.1:8766/` 返回 HTTP 200；
4. 原始安装返回非零，并明确报告切换失败及回滚结果。

Legacy 验证失败发生在旧 `8766` 停止前，但仍执行同一清理和恢复检查。若旧服务
也无法恢复，安装器保持非零并输出高优先级错误，不把部分运行的 stack 报告为成功。

## Dry-run and uninstall

默认 `--dry-run`：

- 渲染并 lint Gateway 与 Legacy 两份配置；
- 用带 label 的边界标记把两份 plist 输出到 stdout，便于人工和测试分别检查；
- 不调用 launchctl、lsof 或 curl；
- 不创建日志目录、不写 LaunchAgents、不截断日志。

`--mode single --dry-run` 只渲染、lint 并输出旧单进程 plist，同样无外部副作用。

完整卸载命令依次 bootout 三个固定 label，确认它们均未加载后，分别移除三份
plist。重复运行仍成功并报告未安装状态。任何仍加载的 job 对应 plist都会保留，
命令返回非零；卸载不启动单进程服务，因此不会与回滚语义混用。

## Automated verification

测试通过临时 LaunchAgents 目录和 PATH 中的 launchctl、lsof、curl stub 运行真实
shell 脚本，记录并断言可观察调用顺序：

- 成功路径必须先验证 Legacy，再停止旧 `8766`，最后启动并验证 Gateway。
- Gateway readiness 失败必须停止新 stack、恢复旧 job，并再次得到 `8766` HTTP 200。
- 未知 `8766` 或 `8767` listener 必须在任何状态修改前终止。
- dry-run 必须产生两份有效 plist，且不调用 launchctl、lsof、curl 或写入
  LaunchAgents。
- single 模式必须停止两个新 job、启动旧 job并验证公开端口。
- uninstall 必须幂等处理三个固定 label，且在 job 仍加载时保留其 plist。

保留并更新现有 Dashboard launchd 测试，避免 prediction config、runtime-root、
bootstrap retry 和日志截断行为回归。

## Acceptance and deployment

开发期间只运行聚焦测试、完整 pytest、dry-run 和 stubbed cutover。最终候选 SHA
先以 `--mode single` 部署，使当前 Phase 0.2 之前的 Dashboard acceptance 仍能验证
单进程拓扑；所有相关 controller SHA 按 gate 要求对齐。随后运行一次最终
`make acceptance`。

只有 acceptance 为 `PASS` 时，才用同一已验收 SHA 执行默认 stack 安装。切换后
验证两个 PID、cwd、Git SHA、fresh logs、Legacy/Gateway health、Gateway upstream
状态以及 `http://127.0.0.1:8766/` HTTP 200。该 exact-SHA 重启不再重复 acceptance；
双模块感知的 acceptance 属于后续 Phase 0.3。

## Non-goals

- 不修改 Gateway 或 Legacy Dashboard 的 Python 行为。
- 不改变浏览器 URL、页面、API schema 或 worker 归属。
- 不删除旧单进程模板或 plist。
- 不让 `8767` 监听非 loopback 地址。
- 不实现后续模块拆分或 Phase 0.3 acceptance 改造。
