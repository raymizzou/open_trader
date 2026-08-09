# Prediction 契约与生产基线（2026-08-10）

## 范围

这是 #39 的只读基线：冻结现有外部路径、关键字段、控制语义和真实运行归属，供后续
8769 Prediction Service 迁移做 parity 对照。本票不切路由、不重启进程、不修改 SQLite、
模式、熔断器或订单行为。

可执行契约位于 `open_trader.prediction_api_contract.PREDICTION_API_CONTRACT_V1`；
`tests/test_prediction_api_contract.py` 用真实 `create_dashboard_server` HTTP 请求锁定路径、
成功状态、关键响应字段、会话/CSRF、严格请求字段、400/403，以及 Legacy unavailable
state 的现有 200 行为。

## 外部 API 基线

| 方法 | 路径 | 精确输入 | 当前成功状态 |
| --- | --- | --- | --- |
| GET | `/api/prediction-arbitrage/state` | 无 | 200 |
| GET | `/api/prediction-arbitrage/history` | `kind`, `limit`, `offset` | 200 |
| POST | `/api/prediction-arbitrage/preview` | `opportunity_id` | 200 |
| POST | `/api/prediction-arbitrage/executions` | `preview_id`, `idempotency_key` | 200 |
| POST | `/api/prediction-arbitrage/mode` | `mode` | 200 |
| POST | `/api/prediction-arbitrage/circuit-breaker/reset` | `incident_id` | 200 |
| POST | `/api/prediction-arbitrage/predict-allowance/cleanup` | `confirm=true` | 200 |
| POST | `/api/prediction-arbitrage/cross-auto/pause` | `confirm=true` | 200 |

`history.kind` 只接受 `signals`、`executions`、`incidents`。state 的关键顶层字段为
`status`、`health`、`readiness`、`stale`、`events`、`opportunities`、`venues`、
`cross_venue`、`relation_discovery`、`validation_mode`、`cross_auto`、
`current_execution`、`breaker` 与 `csrf_token`。history 固定返回 `kind`、`items`、
`total`、`limit`、`offset`、`has_more`。

所有 POST 当前都要求 loopback client、精确 Host/Origin、`ot_prediction_session` HttpOnly
Strict cookie、`X-CSRF-Token`，并拒绝任何多余或缺少的 JSON 字段。边界错误为 400，
授权错误为 403，正文超过 1 MiB 为 413；内部不可用当前一般为 500。

## 产品与故障语义

- 每个机会恰好属于 `YES_NO`、`LLM_RELATION`、`N_LEG` 之一。
- 每种策略独立持有 `OBSERVE_MANUAL` 或 `AUTO`；N 腿初始为
  `OBSERVE_MANUAL`。人工确认只改变是否自动提交，不跳过关系批准、当前证明、新鲜行情、
  正保证利润、深度、余额、风险边界和熔断检查。
- 观察、人工确认与自动提交使用同一安全标准。全局熔断器独立于三个策略开关：它阻止
  人工/自动真实提交和自动修复，但不停止行情、发现、证明、展示和历史。

当前 Legacy 的 `validation_mode=observe_only|manual|auto` 与跨所
`configured_mode=observe_only|manual_confirm|auto_submit` 是迁移输入，不等同于目标的三个
独立策略开关。后续 Ticket 必须显式映射并保留当时生产状态。

| 情形 | Prediction Service 目标语义 | 是否可下单 |
| --- | --- | --- |
| `/healthz` 200 | 只证明进程存活，不证明领域 ready | 否 |
| 未持有生产所有权或启动对账未完成 | state 与 mutation 503 | 否 |
| 单一行情源降级 | state 200，明确标记受影响来源 | 受影响来源否 |
| 历史账本可读 | history 200，不被单一实时源故障连带关闭 | 不适用 |
| 关系、规则、费用、行情未知或证明陈旧 | proof=`UNKNOWN` | 否 |

已冻结的迁移差异：Legacy 在三个 Prediction 组件都缺失时，state 仍返回 HTTP 200，payload
为 `status=unavailable`、`stale=true`、breaker open。8769 完成前不能把这项当前事实误写成
已经具备 503 readiness 语义。

## 生产运行快照

采集时间：2026-08-10 00:05–00:10 CST；工作目录
`/Users/ray/projects/open_trader`。

| 组件 | PID | 端口 | loaded SHA / 状态 |
| --- | ---: | ---: | --- |
| Frontend Gateway | 16261 | 8766 | `e85326cfc151966797f0769d0f7a6eed4af51d56`, clean, upstreams ok |
| Legacy Dashboard | 16002 | 8767 | `e85326cfc151966797f0769d0f7a6eed4af51d56`, clean |
| Prediction health checker | 91498 | 无 HTTP 端口 | 2 小时间隔进程，cwd 同上 |

本地 `main=e85326cf`，生产正运行该 SHA；`origin/main=2e199179`，本地 main 比 remote 多 6 个
与 Trend review 有关的提交。Prediction HTTP、monitor、跨所 monitor、execution 和生产
SQLite 的 owner 仍是 Legacy Dashboard。独立进程只有健康检查器，尚无 8769 listener。

采集时 state 为 healthy、`stale=false`、breaker closed；`validation_mode=auto`，跨所
`configured_mode=auto_submit`、`effective_mode=auto_submit`、`armed=true`。跨所为 ready，
funnel 为 matched 13 / monitored 13；relation discovery 单源为 degraded。最近一条健康检查
（2026-08-09 23:30 CST）因此为 WARN，而总体 state 仍可读。这些是只读证据，不是本票设置
或批准的模式变更。

## #27–#38 事实清单

| 编号 | Tracker 状态 | 当前 main / 生产 | 未合并或剩余事实 |
| --- | --- | --- | --- |
| #27 | Issue OPEN | `6dcc88d4` 已进 main 与生产；当前 matched 13 | `codex/issue-27-gamma-clob-parse@a1b101f4` 已是 main 祖先，不再是未合并实现；Issue 未关闭 |
| #28 | Issue OPEN | `10530445`/`64809e3f` 已进 main 与生产 | 无已知未合并实现；本次未验证真实飞书送达 |
| #29 | Issue OPEN | PR #35–#37 已进 main；健康进程 PID 91498 运行中 | 最新周期为 WARN；Issue 仍等待运营闭环 |
| #30 | Issue OPEN | 当前 acceptance registry 没有 live matched/unresolved 配对检查 | 未实现；原 blocker #27 的代码已合并，但 tracker 依赖未更新 |
| #31 | Issue CLOSED | 无独立实现 | 已由 #32 与 #33 取代 |
| #32 | Issue OPEN | `36cfaeef` 已进 main 与生产 | `feat/issue-32-short-settlement-depth-screening@314fdfde` 已是 main 祖先；Issue 未关闭 |
| #33 | Issue OPEN | `86b7cbc4` 已进 main 与生产；运行时为 auto | 无已知未合并实现；Issue 未关闭 |
| #34 | Issue OPEN | `f4983487` 的 signal-count cache 已进 main 与生产，但原线程/FD 故障未证明消失 | `fix/prediction-state-timeout@f4983487` 已是 main 祖先；按 Issue 评论 blocked by #41 |
| #35 | PR MERGED | #29 健康服务，`f4bb7122` merge | 不是 Issue |
| #36 | PR MERGED | #29 SHA 读取修复，`ca308877` merge | 不是 Issue |
| #37 | PR MERGED | #29 SHA 读取修复，`826c6295` merge | 不是 Issue |
| #38 | Issue OPEN | 未迁移；生产 owner 仍是 Legacy | 已批准设计在 `codex/prediction-arbitrage-platform-design` 与当前 #39 分支；无服务实现 |

后续 Ticket 必须以“代码是否在 main”“运行 SHA 是否包含它”“Tracker 是否关闭”三项分别
判断，不能从其中一项推断另外两项。

## 复核命令

```bash
git rev-parse main origin/main
git rev-list --left-right --count origin/main...main
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
launchctl print gui/$(id -u)/com.open-trader.prediction-arbitrage-health
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
curl -fsS http://127.0.0.1:8766/api/prediction-arbitrage/state
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_api_contract.py
```
