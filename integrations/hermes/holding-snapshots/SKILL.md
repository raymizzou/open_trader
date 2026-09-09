---
name: hermes-holding-snapshots
description: 手工确认 HK/CN 持仓截图后，安全提交 Account 快照并检查报告血缘。
---

# Hermes 持仓快照（个人模板）

这是待安装的 Hermes skill 模板，不是已安装 skill，也不会自行创建 cron、发送消息
或提交真实持仓。安装时把命令中的 release root、共享 Python、确认文件目录、receipt
目录、通知目的地和时间都替换成操作员明确提供的值。

## 约束

- 收齐 `IMG_7259.PNG`、`IMG_7260.PNG`、`IMG_7261.PNG` 等 HK/CN 原图并保留原件；
  raw 与 `derived` 字段分开保存。
- 只对同一 instrument 的完全相同行去重；名称、市场、类型、价格使用既有只读 Futu
  证据。未知日期、覆盖范围、数量或代码返回 `NEEDS_MORE_INPUT`，不得 POST。
- 先给用户一份包含现金政策的短对照表并取得明确确认。首次导入用 `replace`；只有
  明确确认时才允许 `preserve` accepted cash。确认 JSON 必须原样保存。
- Account 的 `received`、`staged`、`published` 分开报告；`202` 只代表 staged。
  `needs_input`、`rejected` 和 `submission_unknown` 也必须按 receipt 原因处理；报告
  血缘检查不能声称生成报告或下单。

## 提交

```bash
PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src" "$PYTHON_BIN" -m \
  open_trader.holding_snapshot_workflow submit \
  --broker "$BROKER" --input "$CONFIRMED_JSON" \
  --receipt "$RECEIPT_DIR/$BROKER.json" \
  --account-url http://127.0.0.1:8768
```

相同确认 JSON 可安全重试。只在 receipt 明确为 `published` 时报告发布完成；
`pending`/`submission_unknown` 继续报告不确定状态并保留文件。
确认后的 JSON 输入和 POST body 上限为 1 MiB；提交 staged 响应和 Dashboard 响应共用
独立的 16 MiB 响应上限。不要把输入上限当成响应上限；超限按不确定/不可用处理。
Workflow 使用显式无代理、拒绝重定向的传输，HTTP_PROXY 等环境变量不会接管 loopback
请求。

## 检查

```bash
PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src" "$PYTHON_BIN" -m \
  open_trader.holding_snapshot_workflow check \
  --account-url http://127.0.0.1:8768 \
  --dashboard-url http://127.0.0.1:8766 \
  --expected-date "$CONFIRMED_TRADING_DATE"
```

此命令只 GET Account 和 Dashboard。结构化 JSON 中的 overdue、pending、controller
或 unavailable 都是可行动问题；健康基线和例行健康变化输出 `[SILENT]`，恢复或新旧
问题变化才通知。Hermes 原生 `--monitor-script` 负责 hash/suppression；不要添加第二
个通知去重状态。每个 broker 摘要保留 `controller_health` 和可行动的
`controller_reason`（健康或非阻塞 `readonly` 时为空；`readonly` 且 blocking 时保留
`controller_blocking` 可行动事实），并使用日期与 holding generation 区分当前账户事实和
冻结报告事实。账户快照另保留 `source_kind` 与持仓/现金 notes；`manual` 不伪装成 `statement`。检查不会因 quote
或全局 snapshot generation 的例行变化报警。staged POST 与 Dashboard 响应共用 16 MiB
上限，超限会明确报告 unavailable。每日截图提醒与此检查分开，交易日必须由已确认日程提供。

## READY-TO-CONFIGURE monitor recipe

下面的脚本和命令只供操作员在得到单独授权后使用；不在模板安装时执行。`hermes cron
create` 会立即启用，且没有 `--paused` 选项。脚本必须位于 `~/.hermes/scripts`，使用
immutable release 和显式共享 Python；它不传 `--expected-date`，因此基础健康检查不能
判定每日输入缺失。expected due date 由另一个已确认交易日/截止时间流程提供。

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

健康 baseline、例行健康日期/版本变化只输出精确 `[SILENT]`；新/变化的 actionable issue
或 recovery 才输出摘要。此 recipe 不创建 job，也不代表已激活或完成三天验收。

## Account consumer compatibility

升级前后先从实际 PID/service 解析 `CONSUMER_ROOT`，再执行现有只读兼容性检查；不要从
当前编辑 checkout 猜测路径：

```bash
CONSUMER_ROOT=/absolute/path/resolved-from-the-running-consumer
PYTHON_BIN=/absolute/path/to/shared-verified-python
PYTHONPATH="$CONSUMER_ROOT:$CONSUMER_ROOT/src" "$PYTHON_BIN" -m \
  open_trader account-sync-status --json
```

回滚必须使用兼容的 consumer/API release pair。旧 `43b74d0` reader 与新 API `ad9234bd`
不兼容；`ad9234bd` 是兼容基线。完整升级/回滚步骤见
[`account-release-upgrade-rollback.md`](../../../docs/operations/account-release-upgrade-rollback.md)。
