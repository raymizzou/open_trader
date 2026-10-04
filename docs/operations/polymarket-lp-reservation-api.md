# LP 历史临时预留手动释放 API

本接口只豁免当前 LP 自动池的一笔或一批本地 UNKNOWN 入场请求预留。
它不证明请求未发送、被拒绝或没有成交，也不取消交易所订单。
没有 UI；Codex 可使用 HTTP API。实现或离线测试不授权执行生产释放。

## 请求

服务使用现有 Prediction 本机地址，例如 `http://127.0.0.1:8769`。
先读取 `GET /api/prediction-arbitrage/venues`，取得 `csrf_token` 和响应中的
`ot_prediction_session` cookie。不要把它们记录到工单或终端日志。

读取候选（不写库、不刷新账户、不启用自动池）：

```http
GET /api/prediction-arbitrage/lp/auto/reservations HTTP/1.1
Host: 127.0.0.1:8769
```

单笔释放使用列表返回的 `intent_id`，不能使用市场、token 或金额猜测 ID：

```http
POST /api/prediction-arbitrage/lp/auto/reservations/release HTTP/1.1
Host: 127.0.0.1:8769
Origin: http://127.0.0.1:8769
Cookie: ot_prediction_session=<本次会话 cookie>
X-CSRF-Token: <本次 csrf_token>
Content-Type: application/json

{"confirm":true,"intent_id":"old-round:0","reason":"操作者明确接受迟到订单及历史结果不确定性，豁免此本地预留"}
```

释放本次调用选择时可释放的全部预留，使用下面的正文替换单笔正文：

```json
{"confirm":true,"all_releasable":true,"reason":"操作者明确接受迟到订单及历史结果不确定性，豁免当前可释放预留"}
```

`intent_id` 与 `all_releasable` 必须二选一。`confirm` 和 `all_releasable`
必须为 JSON `true`，不接受数字 `1`。`reason` 必须是非空字符串，最多 500 字符。
额外字段（包括 `force`）会被拒绝。POST 保留 loopback、Host、Origin、session、CSRF、
运行所有权和 production 模式检查；Shadow 禁止变更。

## 精确返回结构

GET 返回三个字段：`reservations`（下面的记录数组）、`account_id`（当前池账户哈希或 null）、
`desired_running`（布尔值）。每条记录包含：

| 字段 | 类型与含义 |
|---|---|
| `intent_id`, `session_id` | 稳定本地 ID，选择使用 intent ID |
| `amount_usd` | 原预留金额的十进制字符串，未知时 null；null 不阻止显式豁免 |
| `market_id`, `condition_id`, `token_id` | 原请求标的，缺失时 null |
| `original_unknown_reason` | 原 unknown 事件原因，或既有核对原因 |
| `eligible` | 当前是否可释放 |
| `exclusion_reason` | 可释放时 null，否则为下表原因码 |
| `manual_reservation_release` | 未手动释放时 null；已释放时为持久标记及原审计快照 |
| `session_revision` | 当前会话版本，缺会话时 null |
| `reservation_version` | 当前预留字段的 SHA256；服务器在事务内复查，不需要客户端提交 |

POST HTTP 200 返回以下全部字段：

| 字段 | 精确类型 |
|---|---|
| `released` | 本次释放的 `{intent_id, session_id, amount_usd}` 数组 |
| `already_released` | 已手动释放的相同结构数组；不重复释放或重写原决定 |
| `skipped` | `{intent_id, session_id, amount_usd, reason}` 数组；ID 不存在时没有 session_id、amount_usd 为 null |
| `released_amount_usd` | 若本次释放任一金额未知则为 null；全部金额已知时为合计十进制字符串，无新增释放时为 `"0"`；不是钱包回款或收益 |
| `state` | 完整当前自动池状态，与 `GET /api/prediction-arbitrage/lp/auto/state` 的执行层状态字段相同；不包含运行时 scheduler 附加字段 |

操作者应检查 `state.slots`、`state.funds`、`state.block_reasons` 和
`state.admission_block_reasons`，不能把 `released` 或 `desired_running=true`
当作已补位或资金已核清的证据。未知当前库存成本仍返回 null；当前持仓成本优先使用 API 成本金额，缺失时用其数量乘平均成本价。
预算始终为配置值，历史盈亏、成交或费用资料只影响报告；报告 UNKNOWN 不否定有效的当前资金事实。

常见排除或跳过原因：

| 原因码 | 含义 |
|---|---|
| `already_released` | 已有手动标记；POST 计入 already_released |
| `account_covered` | 已由账户事实覆盖，不需要手动决定 |
| `account_identity_unknown`, `foreign_account` | 当前池账户不一致或该记录明确属于其他账户 |
| `send_inflight` | 当前进程仍在准备、排队或发送；旧 reserved/sending 或 pending 阶段字段本身不证明活动操作 |
| `not_unknown_entry`, `order_identity_known` | 不是目标 UNKNOWN 临时入场预留，或已有可靠订单 ID；不修改真实订单 |
| `independent_action_unresolved` | 另有未结束的动作，不能用入场豁免覆盖 |
| `session_missing`, `session_binding_conflict` | 无会话或绑定冲突，属于账本/绑定冲突，保留原核对或中止路径 |
| `reservation_amount_unknown` | 原非空金额格式非法或为负数；缺失金额 null 可显式豁免 |
| `session_changed`, `reservation_changed` | 选择后版本变化，事务未释放该条记录；重新读取后由操作者决定下一次请求 |
| `intent_not_found` | 单笔 ID 不在当前池 |

无效 schema/确认返回 HTTP 400，认证或 Shadow 拒绝返回 403，服务不可用或数据库失败返回 503。
现有执行锁忙时返回 400 `execution_lock`，不执行释放；发送锁可等待当前已授权发送退出。
“全部”不包含选择后新增的请求，也不把选择时排除的活动发送自动升级为可释放。
重启后的旧 reserved/sending 请求若无实际活动操作、可靠订单、外账户或独立未决动作，可由操作者豁免。
缺失的 `reserved_usd` 保留为 null，单条返回与审计 `amount_usd` 也为 null，不能用价格乘数量补造原金额。
已豁免的未知本地金额不再阻止正常补位；实际账户资金和库存成本仍须由现有规则核实。

## 审计与后续行为

手动标记为 `manual_reservation_release`，包括 version/state、原 intent/session/account、
时间、操作者上下文（actor、发布 SHA、安全指纹）、single/all 选择、理由、原金额、
原 UNKNOWN 原因及入场动作快照。原请求、动作和金额历史保留；不创建
`reservation_coverage`，不伪造账户快照、rejected、未发送、零库存或利润。

只有没有可靠订单或独立动作的旧空请求容器才作为本地管理容器退役，记录
`manual_release_retired`；该状态不证明交易所零敞口。原 UNKNOWN 审计保持。
若旧本地库存字段仍为正数，可复用已发布且仍有效的当前空账户事实，或由正常账户发布退役该已豁免容器，
记录 `account_coverage_retired`；不要求额外第二次同步，
不抹掉旧数量、成本或原请求审计；迟到本地回调不能重开该容器。正常补位仍校验当前 API 的身份、完整性、
新鲜度和交易代次，实际库存、挂单、可靠订单 ID、独立退出继续由原规则管理。
标记经重复调用、重启、核对和迟到回执保留，原预留不会恢复。

成功释放后，已启用池会唤醒原自动调度；手动暂停的池保持暂停。
正常新增仍需要真实剩余预算、候选和既有提交检查，原请求不被盲目重发。
迟到交易所订单可能在手动释放与正常补位之后出现，产生短暂重复/超额；
它仍按现有 ID 补录、资金/库存核算及收益率轮换规则处理，不为了补名额强卖库存。
