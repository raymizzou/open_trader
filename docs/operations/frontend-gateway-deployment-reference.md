# Frontend Gateway 双进程部署参考

本文件对应 GitHub Issues [#14](https://github.com/raymizzou/open_trader/issues/14)、
[#15](https://github.com/raymizzou/open_trader/issues/15)、
[#16](https://github.com/raymizzou/open_trader/issues/16) 和
[#17](https://github.com/raymizzou/open_trader/issues/17)，用于验证和运维本地双进程链路。

## 当前范围

当前版本的默认 launchd 安装会执行可回滚的生产端口切换：

```text
浏览器 → Frontend Gateway → Legacy Dashboard
         8766                 8767
```

- Gateway 只提供静态资源、`/healthz` 和 `/api/*` 转发。
- Legacy Dashboard 继续拥有非 Prediction 的数据组装、研究、执行和 worker。
- Gateway 不启动任何 worker，也不导入业务适配器。
- `scripts/install_dashboard_launchd.sh` 默认管理 Gateway + Legacy stack；旧的
  `com.open-trader.dashboard` plist 会保留为非 Prediction 回滚资产。
- `scripts/install_dashboard_launchd.sh --mode single` 会停止两个新 job、恢复旧
  `8766` 非 Prediction 单进程并验证 health；它不会删除新 plist。
- `scripts/uninstall_dashboard_launchd.sh` 才是完整卸载，会幂等移除三个已知 job；
  卸载不会启动回滚服务。

当前部署绑定 loopback，云端公开访问还需要后续的进程监管和入口层设计；不要直接把 `8767` 暴露到公网。

Prediction 运行时的当前边界固定如下：

```text
8766 Frontend Gateway: sole browser ingress
8767 Legacy Dashboard: non-Prediction APIs only; Prediction prefix returns 404
8769 Prediction Service: sole Prediction runtime, database owner, read API, and mutation API
18769 isolated Prediction Service check: temporary Shadow only; never a production owner
Legacy Prediction rollback: unsupported
Compatible Prediction Service release rollback: supported until #60 advances minimum_reader_generation
```

`scripts/install_dashboard_launchd.sh` 不安装或回滚 8769；Prediction Service 必须由
`scripts/install_prediction_service_launchd.sh` 单独管理。Dashboard stack 的失败关闭或
`--mode single` fallback 都不会替代它。

## launchd 安装、回滚和卸载

先检查两份新配置，不会调用 `launchctl`、`lsof`、`curl`，也不会写入
`~/Library/LaunchAgents`：

```bash
scripts/install_dashboard_launchd.sh --dry-run
```

首次安装必须先生成保留的单进程非 Prediction fallback plist；已有该 plist 的 stack
刷新可跳过此步。该 fallback 只恢复 Legacy 的非 Prediction 页面：

```bash
scripts/install_dashboard_launchd.sh --mode single
```

随后正式 stack 安装会先验证当前 `8766` 和 Legacy `8767`，确认 Legacy health 通过后才停止
旧的 `8766` job，再启动 Gateway。任何未知 listener 或 Gateway readiness 失败都会
拒绝切换并返回非零；安装器不会自动 bootstrap 单进程。确认原因后，运维人员可以显式
运行上面的 `--mode single` 进行非 Prediction 恢复；Prediction 仍保持不可用，直到兼容的
8769 Prediction Service release 恢复：

```bash
scripts/install_dashboard_launchd.sh
```

完整卸载三个固定 label（重复运行安全）：

```bash
scripts/uninstall_dashboard_launchd.sh
```

运行时检查：

```bash
launchctl list | rg 'com\.open-trader\.(dashboard|frontend-gateway|legacy-dashboard|prediction-service)'
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
lsof -nP -iTCP:8769 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8769/healthz
PYTHONPATH=src .venv/bin/python -m open_trader prediction-arb status --url http://127.0.0.1:8769
tail -n 100 logs/frontend_gateway/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.out.log
tail -n 100 logs/prediction_service/launchd.out.log
```

## 安全的本地验证（不占用生产 8766）

如果本机的旧 Dashboard 正在使用 `8766`，使用临时端口验证：

```bash
export REPO_ROOT=/path/to/open_trader
export PYTHON="$REPO_ROOT/.venv/bin/python"
export PYTHONSAFEPATH=1
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src"
export GATEWAY_PORT=18766
export LEGACY_PORT=18767
export PREDICTION_PORT=18769
export PUBLIC_ORIGIN="http://127.0.0.1:${GATEWAY_PORT}"
export ROUTE_STATE="$(mktemp "${TMPDIR:-/tmp}/open-trader-prediction-route.XXXXXX")"
```

这套临时验证要求先有一个已确认的、隔离的 Shadow Prediction Service 监听
`127.0.0.1:18769`；不要把生产 8769 当作临时进程使用，也不要在这里启动或停止生产服务。
在写入 route record 前，先验证它的 health 身份（`mode=shadow`、`production_owner=false`、
`mutations=prohibited`，以及当前 `pid`/`cwd`/`git_sha`）：

```bash
set -euo pipefail
SERVICE_HEALTH="$(curl -fsS "http://127.0.0.1:${PREDICTION_PORT}/healthz")"
SERVICE_PID_LINES="$(lsof -nP -iTCP:"$PREDICTION_PORT" -sTCP:LISTEN -t)"
SERVICE_PID="$(printf '%s\n' "$SERVICE_PID_LINES" | sed '/^$/d')"
[[ "$(printf '%s\n' "$SERVICE_PID" | sed '/^$/d' | wc -l | tr -d ' ')" == "1" ]]
SERVICE_ENDPOINTS="$(lsof -nP -a -p "$SERVICE_PID" -iTCP:"$PREDICTION_PORT" -sTCP:LISTEN -Fn)"
printf '%s\n' "$SERVICE_ENDPOINTS" | grep -Fqx "n127.0.0.1:${PREDICTION_PORT}"
SERVICE_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
SOURCE_STATE="$(git -C "$REPO_ROOT" status --porcelain)"
[[ -z "$SOURCE_STATE" ]]
printf '%s' "$SERVICE_HEALTH" | "$PYTHON" -c '
import json, sys
expected_pid, expected_cwd, expected_sha = sys.argv[1:]
health = json.load(sys.stdin)
expected = {
    "schema_version": "open_trader.prediction_service.health.v1",
    "module": "prediction_service",
    "status": "running",
    "mode": "shadow",
    "production_owner": False,
    "mutations": "prohibited",
}
if any(health.get(key) != value for key, value in expected.items()):
    raise SystemExit("isolated Prediction Service health identity mismatch")
if health.get("pid") != int(expected_pid):
    raise SystemExit("isolated Prediction Service listener PID mismatch")
if health.get("cwd") != expected_cwd or health.get("git_sha") != expected_sha:
    raise SystemExit("isolated Prediction Service source identity mismatch")
if health.get("source_state") != "clean":
    raise SystemExit("isolated Prediction Service source state is not clean")
print("isolated Prediction Service:", health["pid"], health["cwd"], health["git_sha"], "source_state=clean")
' "$SERVICE_PID" "$REPO_ROOT" "$SERVICE_SHA"
printf '%s\n' '{"schema_version":"open_trader.frontend_gateway.prediction_route.v1","mode":"service","operation_id":"manual-check","updated_at":"2026-08-14T00:00:00Z"}' > "$ROUTE_STATE"
```

终端 A 启动 Legacy Dashboard：

```bash
cd "$REPO_ROOT"
"$PYTHON" -m open_trader dashboard \
  --host 127.0.0.1 \
  --port "$LEGACY_PORT" \
  --portfolio "$REPO_ROOT/data/latest/portfolio.csv" \
  --data-dir "$REPO_ROOT/data" \
  --reports-dir "$REPO_ROOT/reports" \
  --config "$REPO_ROOT/config/daily_premarket.env" \
  --poll-seconds 5 \
  --futu-host 127.0.0.1 \
  --futu-port 11111 \
  --public-url "$PUBLIC_ORIGIN/"
```

终端 B 启动 Frontend Gateway：

```bash
cd "$REPO_ROOT"
"$PYTHON" -m open_trader frontend-gateway \
  --host 127.0.0.1 \
  --port "$GATEWAY_PORT" \
  --upstream-host 127.0.0.1 \
  --upstream-port "$LEGACY_PORT" \
  --prediction-route-state "$ROUTE_STATE" \
  --prediction-upstream-host 127.0.0.1 \
  --prediction-upstream-port "$PREDICTION_PORT" \
  --public-origin "$PUBLIC_ORIGIN" \
  --static-dir "$REPO_ROOT/src/open_trader/dashboard_static"
```

Prediction 市场功能由独立的 `Prediction Service`（8769）提供；Legacy 命令不再接受
Prediction 配置或 owner 参数，Legacy 的 Prediction prefix 固定返回 404。

## 验证顺序

先确认三个健康端点：

```bash
curl -fsS "http://127.0.0.1:${LEGACY_PORT}/healthz"
curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/healthz"
curl -fsS "http://127.0.0.1:${PREDICTION_PORT}/healthz"
```

Gateway 的响应应包含：

```json
{
  "schema_version": "open_trader.frontend_gateway.health.v1",
  "module": "frontend_gateway",
  "upstream_status": "ok",
  "prediction_route_mode": "service",
  "prediction_upstream_status": "ok"
}
```

Legacy 的响应应包含：

```json
{
  "schema_version": "open_trader.legacy_dashboard.health.v1",
  "module": "legacy_dashboard"
}
```

Prediction Service 的响应必须证明运行时身份，而不仅是 HTTP 存活：

```json
{
  "schema_version": "open_trader.prediction_service.health.v1",
  "module": "prediction_service",
  "status": "running",
  "mode": "shadow",
  "production_owner": false,
  "mutations": "prohibited",
  "pid": 12345,
  "cwd": "/path/to/open_trader",
  "git_sha": "<isolated-service-sha>",
  "source_state": "clean"
}
```

生产 `8769` 必须通过同一套唯一 owner 检查；`prediction-arb status` 只接受
`prediction_service.health.v1`、`mode=production`、`production_owner=true`、
`mutations=enabled`、`source_state=clean` 以及健康响应中的 PID、cwd、Git SHA，
未知或 Shadow/Legacy 身份一律 `BLOCKED`。临时 `18769` 只能使用 Shadow 身份
（`production_owner=false`、`mutations=prohibited`），不能被当作生产服务。

再验证静态页、直接 API 和转发 API：

```bash
curl -fsS -o /dev/null -w 'gateway page: %{http_code}\n' \
  "http://127.0.0.1:${GATEWAY_PORT}/"
curl -sS -o /dev/null -w 'legacy Prediction API: %{http_code}\n' \
  "http://127.0.0.1:${LEGACY_PORT}/api/prediction-arbitrage/state"
curl -fsS "http://127.0.0.1:${PREDICTION_PORT}/api/prediction-arbitrage/state"
curl -fsS -o /dev/null -w 'gateway API: %{http_code}\n' \
  "http://127.0.0.1:${GATEWAY_PORT}/api/prediction-arbitrage/state"
```

浏览器只打开 Gateway 地址：

```text
http://127.0.0.1:18766/
```

`8767` 是内部接口，不作为日常用户入口。

验证结束时只停止本次临时验证启动的 Gateway/Legacy PID；不要停止预先存在的隔离
Prediction Service，除非你明确记录并确认它是本次验证启动的 PID。任何情况下都不要
对生产 8769 执行 stop/bootout。

## 进程、端口和 runtime 检查

```bash
lsof -nP -iTCP:"$GATEWAY_PORT" -sTCP:LISTEN
lsof -nP -iTCP:"$LEGACY_PORT" -sTCP:LISTEN
lsof -nP -iTCP:"$PREDICTION_PORT" -sTCP:LISTEN
ps aux | rg 'open_trader (frontend-gateway|dashboard|prediction-service)'
```

三个进程的启动/health 输出都必须包含 runtime 记录：

```text
frontend_gateway_runtime: {...}
dashboard_runtime: {...}
prediction_service `/healthz`: {"pid": ..., "cwd": ..., "git_sha": ..., "mode": ..., "production_owner": ...}
```

检查记录中的 `pid`、`cwd`、`git_sha` 和 `source_state`。三个进程应来自同一个目标
worktree 和 Git SHA；Gateway health 的 `upstream_status` 必须是 `ok`，Prediction
Service health 必须匹配当前隔离模式（shadow）或生产模式（production）及对应 owner 标志。

## 生产验收与 exact-SHA 交付

所有文档和 CHANGELOG 先提交，随后冻结候选 SHA。最终顺序固定为：

1. 运行 Gateway、launchd stack 和双运行时 acceptance 聚焦测试；
2. 运行完整 pytest suite；
3. 从候选 worktree 部署 Gateway、Legacy Dashboard、Prediction Service 及 acceptance
   依赖的后台进程；
4. 直接验证三个 launchd job（Gateway、Legacy、Prediction Service）、三个 listener
   （8766、8767、8769）、三个 health identity 和一条经 `8766` 转发的 `/api/quotes` 请求；
5. 运行一次最终 `make acceptance`；
6. 仅在 `PASS` 后重新部署完全相同的 accepted SHA；
7. 核对三个新 PID、cwd、SHA、source state、启动时间、新鲜 runtime 日志及
   `http://127.0.0.1:8766/` HTTP 200；确认 8769 `/healthz` 为
   `mode=production`、`production_owner=true`，并确认 Gateway Prediction state 经
   8766 返回 200。

`FAIL` 必须修复并从候选验证重新开始；`BLOCKED` 必须报告实际外部或浏览器
阻塞，不能用 curl、fixture、mock 或单元测试替代。exact-SHA 重启未改变源码或
领域运行数据时，不重复运行 acceptance。

## 停止和回退

手动验证时，在两个终端分别按 `Ctrl-C`。如果使用后台进程，只停止已确认属于本次验证的 PID：

```bash
kill "$GATEWAY_PID" "$LEGACY_PID"
```

不要按端口直接强杀未知进程。先用 `lsof` 和 `ps` 确认 PID、命令行和工作目录。

如果 Gateway 返回 `503 legacy_dashboard_unavailable`：

1. 检查 Legacy `healthz` 和 `LEGACY_PORT` 监听状态。
2. 检查两个 runtime 记录的 `cwd` 和 `git_sha` 是否一致。
3. 停止本次 Gateway/Legacy 进程。
4. #15 安装器返回非零并保留诊断日志，不报告切换成功，也不会自动恢复单进程；确认
   原因后再显式运行 `scripts/install_dashboard_launchd.sh --mode single`，仅恢复非
   Prediction 页面。Prediction rollback 仍不支持，需恢复兼容的 8769 Service release。

不要在 #14 的手工验证阶段把 Legacy 直接切到正在使用的生产 `8766`。正式切换只通过
`scripts/install_dashboard_launchd.sh` 执行，让 listener 所有权和 readiness 失败关闭；
不要把它当作自动回滚机制。

## 目标端口参考

生产最终拓扑是三进程、三监听；`8766` 是唯一浏览器入口，`8767` 只承载
非 Prediction Legacy，`8769` 是 Prediction 的唯一运行时和数据库 owner：

```text
Frontend Gateway      127.0.0.1:8766  (sole browser ingress)
Legacy Dashboard      127.0.0.1:8767  (non-Prediction only)
Prediction Service    127.0.0.1:8769  (sole Prediction owner)
公开浏览器地址        http://127.0.0.1:8766/
```

手工不占用生产端口的 Gateway/Legacy 验证仍可使用上文的 `18766/18767`；如需
隔离 Shadow 验证，`18769` 仅是临时验证端口，绝不属于最终拓扑、生产 owner 或
回滚入口。正式安装器使用目标 `8766/8767`，Prediction Service 单独使用 `8769`；
readiness 失败时返回非零并等待人工选择非 Prediction fallback。
