# Hermes 手工持仓快照运行手册

本手册描述“截图确认 → Account 暂存 → Account Worker 发布 → 报告血缘检查”的
每日流程。它不启用 Hermes job、不生成报告，也不提交订单。每次操作都要保留原始
截图和确认后的 JSON；`received`、`staged`、`published` 是不同状态，不能互相代替。

## 每日人工确认

1. 接收当日全部 HK/CN 截图（例如 `IMG_7259.PNG`、`IMG_7260.PNG`、
   `IMG_7261.PNG`）。原图必须放在本次操作目录，供复核和回放；上线验收时由操作员
   在该目录提供这三个原文件，不要把私人截图复制进仓库或文档。
2. 逐行保留截图中的 raw 字段，并把任何清洗、推导、映射字段标为 `derived`。
   只有完全相同且属于同一 instrument 的重复行才可去重。名称、市场、类型、价格
   使用现有只读 Futu 证据解析；用户不会另行提供代码，不能凭猜测补代码。
3. 日期、覆盖范围、数量或代码有任意不确定时，结果必须是
   `NEEDS_MORE_INPUT`，不要 POST。向用户展示一份简短对照表，至少列出市场、名称、
   代码、数量、成本价和现金政策，得到明确确认后再继续。
4. 首次导入使用 `replace`；只有用户明确确认的政策才可 `preserve` 既有 accepted
   cash。保存用户确认的完整 JSON，不改写原始截图。

## 提交与检查

以下命令中的路径是示例，实际运行时必须替换为已验证的不可变 release checkout、
共享 Python 和独立 receipt 目录。不要从当前可编辑 checkout 猜测运行代码。

```bash
RELEASE_ROOT=/absolute/path/to/verified-release
PYTHON_BIN=/absolute/path/to/shared-verified-python
INPUT=/absolute/path/to/confirmed/holdings-phillips.json
RECEIPT_DIR=/absolute/path/to/receipts
mkdir -p "$RECEIPT_DIR"

PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src" "$PYTHON_BIN" -m \
  open_trader.holding_snapshot_workflow submit \
  --broker phillips --input "$INPUT" \
  --receipt "$RECEIPT_DIR/phillips.json" \
  --account-url http://127.0.0.1:8768

PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src" "$PYTHON_BIN" -m \
  open_trader.holding_snapshot_workflow check \
  --account-url http://127.0.0.1:8768 \
  --dashboard-url http://127.0.0.1:8766 \
  --expected-date YYYY-MM-DD
```

提交命令只在 `published` 时返回成功；`received`/`staged`/`pending`、
`submission_unknown`、`rejected` 和 `needs_input` 都要按 receipt 原因处理。网络中断
后可以用同一个确认文件重试；服务端按内容生成 immutable generation。receipt 只保存
broker、日期、请求摘要、generation、状态和安全原因，不保存完整持仓或现金。

确认后的提交 JSON（本地输入和 POST body）上限为 1 MiB；这是输入限制，不是所有
HTTP 响应的限制。提交接口的 staged 响应和 `check` 读取的 Dashboard JSON 共用独立的
16 MiB 响应上限，以容纳正常数据；超过该上限会明确报告不可用/不确定，不会按截断
JSON 当作普通数据解析。Workflow 使用显式的无代理、拒绝重定向传输；HTTP_PROXY 等
环境变量不会把这些 loopback 请求转发到代理。

检查命令是只读的，不生成报告、不写 status 文件、不发送通知；它在成功输出结构化
JSON 时即使发现业务问题或 Account/Dashboard 暂不可用也返回 0，问题会写在 JSON 中。
只有参数错误或未预期执行错误才是非 0。检查结果不能被描述为报告已重新生成，亦不
能被描述为已下单。

每个 broker 的检查摘要会保留 `controller_health` 与可行动的
`controller_reason`（健康或非阻塞 `readonly` 时 reason 规范化为空；`readonly` 且
blocking 时仍是可行动的 `controller_blocking`），并使用日期和 holding generation
区分当前账户事实与报告中冻结的事实。账户快照的 `source_kind` 与持仓/现金 `notes`
保留来源说明：`manual` 仍表示手工快照、`statement` 仍表示官方结单；最新账户不会覆盖既有报告，也不会因 quote 或全局
snapshot generation 的例行变化触发告警。

## Hermes 监控（只配置，不启用）

准备上线时使用 Hermes 原生 `--monitor-script`，让监控脚本运行上面的只读 `check`
并将稳定 JSON 交给 Hermes 的内置 hash/suppression。不要增加自定义去重文件、队列或
心跳定时器。下面是由操作员填值、复制并在另一步明确授权后执行的
READY-TO-CONFIGURE 配方；`hermes cron create` 会立即启用，而且没有 `--paused` 选项，
所以最后一条命令是后续单独授权的 ACTIVATION 步骤，本轮不执行任何复制、chmod 或
create。脚本固定使用 immutable release 与显式共享 Python：

```bash
RELEASE_ROOT=/absolute/path/to/verified-release
PYTHON_BIN=/absolute/path/to/shared-verified-python
ACCOUNT_URL=http://127.0.0.1:8768
DASHBOARD_URL=http://127.0.0.1:8766
MONITOR_SCRIPT="$HOME/.hermes/scripts/holding-snapshots-check.sh"
MONITOR_SCHEDULE='every 15m'
DELIVERY_TARGET='REPLACE_WITH_EXPLICIT_DELIVERY_TARGET'
MONITOR_PROMPT='读取 Hermes 注入的 monitor JSON/diff，不要再次运行 check。首次健康基线或仅例行健康日期/版本变化时只输出精确字符串 [SILENT]；只有新的或变化的 actionable issue，或从既有 issue 恢复时输出简短摘要；不要输出 heartbeat、quote、price、value、PID 或全局 snapshot generation。'

mkdir -p "$(dirname "$MONITOR_SCRIPT")"
cat > "$MONITOR_SCRIPT" <<SH
#!/bin/sh
set -eu
export PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src"
exec "$PYTHON_BIN" -m open_trader.holding_snapshot_workflow check \
  --account-url "$ACCOUNT_URL" \
  --dashboard-url "$DASHBOARD_URL"
SH
chmod 0755 "$MONITOR_SCRIPT"

hermes cron create "$MONITOR_SCHEDULE" "$MONITOR_PROMPT" --name '持仓链路监控' --monitor-script "$MONITOR_SCRIPT" --deliver "$DELIVERY_TARGET" --continuity
```

这个基础健康监控没有 `--expected-date`，因此不能单独判定每日输入是否缺失；expected
due date 必须由另一条已确认交易日/截止时间流程明确提供，再单独配置带
`--expected-date` 的检查。每日截图收集提醒也仍是另一条人工流程，不能由廉价健康检查
代替。通知时间和投递目的地由操作员明确填写。

原生 monitor prompt 必须包含以下等价的精确规则（此处只保存待配置文案，不创建 job）：

```text
读取 Hermes 注入的 monitor JSON/diff，不要再次运行 check。首次基线健康，或仅发生例行
健康日期/版本变化时，只输出精确字符串 [SILENT]。发现新的/变化的 actionable issue，
或从既有 issue 恢复时，输出简短的 issue/recovery 摘要；不要输出 heartbeat、quote、
price、value、PID 或全局 snapshot generation。
```

模板安装前先确认脚本位于 `~/.hermes/scripts` 要求的路径，且只读检查使用 immutable
release。此阶段是 READY-TO-CONFIGURE 配方，不代表 job 已创建或运行过三天验收。

## Account release 升级前置条件

Account API 与 Account Sync Worker 是同一 release pair，但实际运行的每个 consumer
都必须先用自己的 code root 执行现有 `account-sync-status --json`，因为 consumer 的
升级独立于报告/Gateway 进程。旧 reader `43b74d0` 与新 API `ad9234bd` 会产生
`account_contract_invalid`；兼容的新 reader 可读旧/新 response。先单独授权升级兼容
consumer，再升级 Account pair；升级后再次读取实际 consumer。不能用 API `/healthz`
推断 consumer 已升级或已读到新数据。

升级前后都要从实际 PID/service 的 code root 解析出 `CONSUMER_ROOT`，再使用共享 Python
执行同一只读兼容性检查；不要从当前编辑 checkout 猜测路径：

```bash
CONSUMER_ROOT=/absolute/path/resolved-from-the-running-consumer
PYTHON_BIN=/absolute/path/to/shared-verified-python
PYTHONPATH="$CONSUMER_ROOT:$CONSUMER_ROOT/src" "$PYTHON_BIN" -m \
  open_trader account-sync-status --json
```

回滚也必须使用彼此兼容的 consumer/API release pair；`43b74d0` reader 与新 API
`ad9234bd` 不兼容，而 `ad9234bd` 是本任务的兼容基线。完整步骤复用
[`account-release-upgrade-rollback.md`](account-release-upgrade-rollback.md)，本流程不
修改 installer，也不创建新的 readiness framework。

## Futu SDK 连接边界

Account Worker、趋势控制器和纸面同步使用共享的 Futu trade-context adapter。它在首次
初始化前设置 SDK 公共 `set_sync_query_connect_timeout(10)`，并把首次连接失败限制为
一次 SDK 初始化尝试；这不是严格的十秒构造 deadline，SDK 自带的 socket/握手等待仍约
为固定的 20 秒 + 20 秒。首次失败会关闭 context 并让上层保留原有 typed
`trade_context_failed`。首次成功后 SDK 的异步 reconnect 仍按 SDK 原行为工作，不会被
adapter 关闭或禁用。运行时必须提供 `OpenSecTradeContext._init_connect_sync`、
`set_sync_query_connect_timeout` 和 `RET_OK`；不兼容的 futu-api 版本会在构造前清晰失败。
