# Frontend Gateway 双进程部署参考

本文件对应 GitHub Issue [#14](https://github.com/raymizzou/open_trader/issues/14)，用于验证第一阶段的前后端分离链路。

## 当前范围

当前版本只建立进程边界，不做生产端口切换：

```text
浏览器 → Frontend Gateway → Legacy Dashboard
         8766                 8767
```

- Gateway 只提供静态资源、`/healthz` 和 `/api/*` 转发。
- Legacy Dashboard 继续拥有现有数据组装、研究、预测、执行和 worker。
- Gateway 不启动任何 worker，也不导入业务适配器。
- 现有 `scripts/install_dashboard_launchd.sh` 仍管理旧的单进程 Dashboard；它还不是双进程 stack 安装器。
- launchd 的双进程安装、自动回滚和生产切换属于后续 [#15](https://github.com/raymizzou/open_trader/issues/15)。

当前部署绑定 loopback，云端公开访问还需要后续的进程监管和入口层设计；不要直接把 `8767` 暴露到公网。

## 安全的本地验证（不占用生产 8766）

如果本机的旧 Dashboard 正在使用 `8766`，使用临时端口验证：

```bash
export REPO_ROOT=/path/to/open_trader
export PYTHON="$REPO_ROOT/.venv/bin/python"
export PYTHONSAFEPATH=1
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src"
export GATEWAY_PORT=18766
export LEGACY_PORT=18767
export PUBLIC_ORIGIN="http://127.0.0.1:${GATEWAY_PORT}"
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
  --public-origin "$PUBLIC_ORIGIN" \
  --static-dir "$REPO_ROOT/src/open_trader/dashboard_static"
```

如果需要预测市场功能，在 Legacy 命令中按现有部署方式追加 `--prediction-config`；Gateway 不读取该配置。

## 验证顺序

先确认两个健康端点：

```bash
curl -fsS "http://127.0.0.1:${LEGACY_PORT}/healthz"
curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/healthz"
```

Gateway 的响应应包含：

```json
{
  "schema_version": "open_trader.frontend_gateway.health.v1",
  "module": "frontend_gateway",
  "upstream_status": "ok"
}
```

Legacy 的响应应包含：

```json
{
  "schema_version": "open_trader.legacy_dashboard.health.v1",
  "module": "legacy_dashboard"
}
```

再验证静态页、直接 API 和转发 API：

```bash
curl -fsS -o /dev/null -w 'gateway page: %{http_code}\n' \
  "http://127.0.0.1:${GATEWAY_PORT}/"
curl -fsS -o /dev/null -w 'legacy API: %{http_code}\n' \
  "http://127.0.0.1:${LEGACY_PORT}/api/prediction-arbitrage/state"
curl -fsS -o /dev/null -w 'gateway API: %{http_code}\n' \
  "http://127.0.0.1:${GATEWAY_PORT}/api/prediction-arbitrage/state"
```

浏览器只打开 Gateway 地址：

```text
http://127.0.0.1:18766/
```

`8767` 是内部接口，不作为日常用户入口。

## 进程、端口和 runtime 检查

```bash
lsof -nP -iTCP:"$GATEWAY_PORT" -sTCP:LISTEN
lsof -nP -iTCP:"$LEGACY_PORT" -sTCP:LISTEN
ps aux | rg 'open_trader (frontend-gateway|dashboard)'
```

两个进程的启动输出都必须包含 runtime 记录：

```text
frontend_gateway_runtime: {...}
dashboard_runtime: {...}
```

检查记录中的 `pid`、`cwd`、`git_sha` 和 `source_state`。两个进程应来自同一个目标 worktree 和 Git SHA；Gateway health 的 `upstream_status` 必须是 `ok`。

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
4. 当前 #14 没有自动回滚生产服务；原有 `8766` 单进程服务未被本次验证停止。

不要在 #14 阶段手动把 Legacy 直接切到正在使用的生产 `8766`。需要正式切换时，等待 #15 提供带 readiness 检查和回滚路径的 stack 安装器。

## 目标端口参考

双进程最终约定仍是：

```text
Frontend Gateway   127.0.0.1:8766
Legacy Dashboard   127.0.0.1:8767
公开浏览器地址     http://127.0.0.1:8766/
```

在 #15 完成前，上述目标端口只作为接口契约；本文件的可执行验证默认使用 `18766/18767`，避免中断现有 Dashboard。
