# Kalshi 跨交易所预测市场接入研究

日期：2026-08-01

范围：研究与接入设计，不包含代码、实盘授权或部署变更。

## 结论先行

1. **在账户主体、KYC 和实际所在地获得书面资格确认前，当前项目不能启用 Kalshi 事件合约实盘。** Kalshi 2026-06-17 生效的 Member Agreement v1.6 第 VI 节将 People's Republic of China 列为 Event Contracts Restricted Jurisdiction。协议区分“访问平台”和“交易 Event Contracts”：身处、定居或组织于受限司法辖区的成员不得交易事件合约。当前只适合公开只读接入和隔离的 Demo 技术验证，生产写操作必须硬关闭；不得通过香港 VPS、代理或 VPN 绕过限制。[Member Agreement v1.6](https://kalshi.com/docs/kalshi-member-agreement.pdf)
2. **先做 Kalshi 专属只读接入，不先造通用 `VenueAdapter`。** 当前代码仍以 Polymarket 的 condition/token/merge 语义为中心。最小安全路径是新增 Kalshi 数据源和明确的跨场所匹配记录，复用已有存储、锁、预览确认、断路器、事故记录和启动恢复能力；等第二个真实执行通路成熟后，再从实际重复中提炼接口。
3. **最难的不是 API，而是证明两边是同一命题，以及处理非原子执行。** ticker、标题或 LLM 相似度都不能证明结算条件相同。必须固定原生 ID、完整规则、时间窗、时区、结算源、争议/修正规则和规则哈希；LLM 只能找候选，不能授权交易。
4. **新订单使用 V2。** 当前路径是 `POST /portfolio/events/orders`，使用 `side=bid|ask`、fixed-point `price/count`、唯一 `client_order_id`，支持 `fill_or_kill`、`immediate_or_cancel` 和 `good_till_canceled`。[Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
5. **OpenAPI/AsyncAPI 才是协议真相。** Kalshi 官方明确说 SDK 可能滞后。首期公开 REST 不需要增加依赖；Demo 认证阶段再按规范做兼容门禁，决定固定官方 SDK 版本还是实现窄客户端。[SDK overview](https://docs.kalshi.com/sdks/overview)
6. **套利必须按双边可成交深度、动态费用和结算资产折价计算。** Kalshi 以美元结算，Polymarket 涉及稳定币/链上资金，不能默认价值、到账时间或可撤回性完全等价。

## 当前代码边界

现有实现有可复用的运行安全设施，但不是通用交易所抽象：

- `src/open_trader/polymarket_monitor.py` 将发现、盘口、WebSocket 新鲜度和运行状态直接绑定到 Polymarket。
- `src/open_trader/polymarket_trading.py` 是 Polymarket 客户端的窄封装，含余额、下单、撤单、成交核对和 merge。
- `src/open_trader/prediction_arbitrage_execution.py` 已有预览确认 TTL、幂等、进程/文件锁、断路器、启动核对、事故和通知。
- `src/open_trader/prediction_arbitrage_store.py` 可继续保存运行状态、信号、执行、legs、事故和幂等记录。
- `src/open_trader/prediction_arbitrage.py` 中的 `condition_id`、`token_id` 和 merge 是 Polymarket 语义，不能把 Kalshi ticker 伪装成 token 来复用。
- `docs/superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md` 已将 Kalshi/跨场所执行放到后续版本，并明确不先建通用多场所框架。

结论是：**复用运行控制，不复用错误的场所业务语义。** Kalshi 订单、成交、仓位和结算保留原生字段；跨场所只在已审核的事件匹配和执行意图层连接。

## 官方 API 契约

### 环境、认证与分片

| 环境 | REST | WebSocket |
|---|---|---|
| Production | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

生产和 Demo 凭证不共享；Demo 使用模拟资金，其价格和行为不保证反映真实市场。[API environments](https://docs.kalshi.com/getting_started/api_environments) [Demo environment](https://docs.kalshi.com/getting_started/demo_env)

认证使用 API key ID 和 RSA 私钥：

- 请求头为 `KALSHI-ACCESS-KEY`、`KALSHI-ACCESS-TIMESTAMP`（毫秒）和 `KALSHI-ACCESS-SIGNATURE`。
- 签名消息是 `timestamp + HTTP_METHOD + path`；路径包含 `/trade-api/v2/...`，不含 query string。
- 签名算法为 RSA-PSS + SHA-256，结果 Base64 编码。
- 私钥创建后无法再次下载，应存入 macOS Keychain，不写配置、数据库或日志。
- API key 可限定 `read`/`write` scope；只读阶段不创建 `write` key。

官方资料：[API keys](https://docs.kalshi.com/getting_started/api_keys)、[Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)、[Generate API key](https://docs.kalshi.com/api-reference/api-keys/generate-api-key)。官方 [Python starter](https://github.com/Kalshi/kalshi-starter-code-python) 是示例而非稳定 SDK；规范仍以 OpenAPI/AsyncAPI 为准。

Kalshi 正将交易拆到多个 matching-engine shard。`GET /markets`、`GET /events` 和新市场 WebSocket 消息带 `exchange_index`，订单/撤单等 REST 路由也接受此字段；省略默认 shard 0，`-1` 可按 ticker 自动路由但会增加延迟。[Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding)

2026-08-01 的公开 `GET /exchange/status` 只读探测已返回 index 0 和 1，且 shard 的 trading 状态可不同。[Get Exchange Status](https://docs.kalshi.com/api-reference/exchange/get-exchange-status) 因此市场快照、匹配、order intent、撤单、恢复和审计都必须保存/透传原 market 的 `exchange_index`。readiness 要检查目标 shard 状态及 `balance_breakdown` 中该 shard 的余额，不能只看顶层 `trading_active` 或聚合 balance；subaccount 余额也属于具体 shard。[Get Balance](https://docs.kalshi.com/api-reference/portfolio/get-balance)

### 发现、元数据与 REST 盘口

公开 REST 可读取 series、events、markets 和单市场 orderbook，无需登录。[Market data guide](https://docs.kalshi.com/getting_started/quick_start_market_data)

建议发现链路：

1. `GET /events?status=open&with_nested_markets=true` 获取开放事件和嵌套市场；每页最多 200 条，使用 cursor，默认不返回 multivariate events。[Get Events](https://docs.kalshi.com/api-reference/events/get-events)
2. 必要时用 `GET /markets?status=open&mve_filter=exclude` 补齐标准市场；单页最多 1000 条，使用 cursor。[Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
3. 保存 `series_ticker`、`event_ticker`、`ticker`、`exchange_index`、规则、开放/关闭/结算时间、结算来源、`price_level_structure` 和 `price_ranges`。
4. 使用 `min_updated_ts`/更新时间做增量刷新。规则或关键元数据变化时，立即让跨场所匹配失效。

列表必须遍历 cursor 到空，不能把第一页当完整市场集。[Pagination](https://docs.kalshi.com/getting_started/pagination)

单市场 orderbook 是公开端点。fixed-point 响应使用 `orderbook_fp.yes_dollars` 和 `no_dollars`；每档是价格与数量，数组按价格升序，最佳买价在末尾。[Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)

Kalshi 二元盘口只返回 bids：

- YES 最优卖价 = `1 - 最高 NO bid`
- NO 最优卖价 = `1 - 最高 YES bid`

不能把没有 ask 数组理解成没有卖盘。成本必须从对侧 bid 推导 ask，并逐档消耗数量。1–100 tickers 的批量 orderbook 是认证端点；首期无凭证实现只轮询少量候选的单市场盘口。[Multiple Market Orderbooks](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)

### WebSocket 与恢复

WebSocket 连接本身需要认证，即使订阅公开行情 channel，所以完全无凭证的 P1 应使用 REST 轮询。[WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)

Demo 认证阶段：

- `orderbook_delta` 先发 snapshot，再发 delta；消息含 subscription ID 和 sequence。[Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)
- Kalshi 约每 10 秒发 ping，客户端回应 pong。[Keep alive](https://docs.kalshi.com/websockets/connection-keep-alive)
- 可订阅 market ticker、用户 orders/fills/positions 和 market/event lifecycle。[User orders](https://docs.kalshi.com/websockets/user-orders) [User fills](https://docs.kalshi.com/websockets/user-fills) [Market positions](https://docs.kalshi.com/websockets/market-positions) [Lifecycle](https://docs.kalshi.com/websockets/market-and-event-lifecycle)

恢复策略是基于上述官方消息模型的工程推论：断线、sequence gap 或心跳超时后立即把市场标为 stale/non-actionable，丢弃增量簿；指数退避重连后，只有拿到新 snapshot（或 REST 完整快照）才恢复执行。

### 精度和订单方向

价格使用 fixed-point 美元字符串，可能最多四位小数；fractional contracts 数量也使用 fixed-point 字符串。tick 由 `price_level_structure` 和 `price_ranges` 决定。[Fixed-point migration](https://docs.kalshi.com/getting_started/fixed_point_migration)

实现必须：

- 全链路使用 `Decimal`，不使用 float。
- 下单前按该市场的 price ranges 验证 tick，不统一量化 `$0.01`。
- 数量也按 fixed-point 解析/存储，不假定整数。
- 同时保存原始字符串和 Decimal，便于审计。

当前 V2 event order 全部从 YES book 表达：`side=bid` 是买 YES，`side=ask` 是卖 YES，经济上等价于按 `1-price` 买 NO。策略可以表达“持有 NO”，但 wire request 必须转换成 YES-side bid/ask，不能发送 `side=no`。[Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2) 旧方向字段正在迁移，集成只能按目标端点当前 schema 生成请求。[Order direction](https://docs.kalshi.com/getting_started/order_direction)

### 下单、撤单、成交和恢复

V2 `POST /portfolio/events/orders` 的核心字段为：`ticker`、唯一 `client_order_id`、`side=bid|ask`、fixed-point `count/price`、`time_in_force` 和原 market 的 `exchange_index`。另有 self-trade prevention、`post_only`、过期时间、`reduce_only`、`cancel_order_on_pause` 等控制。[Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)

撤单使用对应 V2 endpoint，返回 `order_id/client_order_id/reduced_by/ts_ms`，并接受 `exchange_index`。[Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)

`client_order_id` 是网络不确定性下的幂等键。官方建议提交前生成唯一 UUID；超时可使用同一 ID 重试，重复 ID 会被拒绝。[Create order guide](https://docs.kalshi.com/getting_started/quick_start_create_order)

本项目应更严格：

1. 发请求前把 intent、venue、ticker、exchange index、side、price、count、client ID 和规则哈希事务性落盘。
2. 超时/断线后不得换新 ID 盲重试；先用原 ID、orders 和 fills 核对。
3. 只有明确未创建才可重新提交；状态不明即停止第二腿并进入 incident/reconciliation。
4. 跨场所执行共享一个本地 execution ID，但各 venue leg 保留独立 client order ID。

恢复和审计至少覆盖 [Orders](https://docs.kalshi.com/api-reference/orders/get-orders)、[Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)、[Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions)、[Balance](https://docs.kalshi.com/api-reference/portfolio/get-balance) 和 [Settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements)。REST 账户数据有短暂延迟，应组合 mutation 响应、用户 WebSocket 和 user-data timestamp，而不是用一次 GET 断言最终状态。[User data timestamp](https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp)

目标 shard 的余额不足时，即使聚合 balance 足够也不能下单。未来若需跨 shard 转资，应作为独立、高风险资金动作，不能隐式夹在下单重试里。[Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding)

标准二元市场赢家每份支付 `$1`，按净仓位自动结算。[Market settlement](https://docs.kalshi.com/getting_started/market_settlement) 生命周期包括 initialized、active、inactive、closed、determined、disputed、amended、finalized；重启/重新激活可能取消 resting orders，非 active 状态不得开新仓。[Market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)

### 费用、限流、维护和时钟

2026-07-07 当前官方费率表的一般公式：

- taker：`round_up(M × 0.07 × C × P × (1-P))`
- maker：`round_up(M × 0.0175 × C × P × (1-P))`

`C` 是 contracts，`P` 是美元价格。一般 taker 的 `M` 默认 1、一般 maker 的 `M` 默认 0，除非 fee schedule 对 series 另行指定；fee + position cost 向上舍入到 centicent。部分 series 为不同 multiplier 或零费率，不能硬编码常量。当前表明确没有 settlement fee 和 membership fee。[Fee Schedule, effective 2026-07-07](https://kalshi.com/docs/kalshi-fee-schedule.pdf)

运行时还要读取 series/event 层的计划费用变化；预览必须绑定具体市场适用费率、来源版本和拉取时间。[Series fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes) [Event fee changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes) 费用/返利按实际 fill 舍入，不能只在整笔理论订单末尾舍入。[Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)

认证 API 使用 read/write token buckets。Basic tier 当前为 200 read tokens/s、100 write tokens/s；endpoint 有成本，batch 还按元素计费。429 没有可依赖的 `Retry-After` 或额度头，应指数退避并加抖动。[Rate limits](https://docs.kalshi.com/getting_started/rate_limits)

约束：read/write 分桶预算；429/5xx/超时必须变 unavailable/stale，不能变零盘口/空仓；discovery 慢扫，候选 book/position 快刷；batch 必须整体装入 bucket。

例行维护窗口为每周四美东 03:00–05:00，可能暂停交易/整个 exchange 或断开 WebSocket；应使用 `America/New_York` 处理 DST，不能固定换算 UTC，必要时使用 `cancel_order_on_pause`。[Maintenance](https://docs.kalshi.com/getting_started/maintenance_and_pauses) 内部时间统一保存 UTC aware datetime；启动前检查本机时钟偏差。

## 合规硬门禁

[Member Agreement v1.6, effective 2026-06-17](https://kalshi.com/docs/kalshi-member-agreement.pdf) 第 VI 节明确把 People's Republic of China 列为 Event Contracts Restricted Jurisdiction。它同时说明：该限制本身不必然禁止 membership、平台访问或非 Event Contract 产品，但身处、定居或组织于受限司法辖区者不得交易 Event Contracts。国际用户资格仍受协议、KYC 和当地法律约束。[International access](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)

因此：

- 只允许 production public REST read-only 和隔离 Demo 技术验证；Demo 不证明生产资格。
- 默认 `production_writes_enabled=false`，且代码/配置/UI 都不能由单一环境变量绕过。
- 不创建/加载 production write key，不显示生产下单按钮，不提供“测试一笔”例外。
- 香港 VPS、代理、VPN 或远端节点不得用于规避地点限制；不设计、不实现该路径。
- 未来必须按当时协议、账户/KYC、实际所在地和主体归属重新取得书面确认，旧结论不能沿用。

## 跨场所事件匹配

Kalshi 的 `series/event/market ticker` 与 Polymarket 的 `conditionId/tokenId/slug` 没有共享命名规则。标题相似仍可能在观察窗口/时区、`>`/`>=`、取整、数据修订、settlement source、提前结束、取消/延期/争议、标的单位、scalar/MVE 结构和 payout 上不同。

获准配对应保存：

```text
match_id
canonical_predicate:
  subject / metric / operator / threshold / unit
  observation_start / observation_end / timezone
  settlement_source_primary / fallback
  resolution / cancellation / dispute / amendment rules
  payout_currency / payout_amount
polymarket:
  condition_id / token_ids / outcome_mapping / rules_sha256
kalshi:
  series_ticker / event_ticker / market_ticker / exchange_index
  outcome_mapping / rules_sha256
review:
  matcher_version / reviewed_at / approved_by / expires_at
```

LLM 可提出候选和解释差异；deterministic validator 必须核对谓词、时间、来源、payout 和规则哈希，最终进入 allowlist 仍需人工审核。任一侧规则、时间、结算源或 exchange index 改变，配对立即失效。这个边界与现有设计一致：模型做语义发现/审计，资金动作由确定性规则授权。

## 套利计算和非原子执行

已证明等价的二元命题才可考虑：Kalshi YES + Polymarket NO，或 Polymarket YES + Kalshi NO。数量 `q` 的保守净收益下界：

```text
net_floor = q * common_payout_floor
            - kalshi_executable_cost
            - polymarket_executable_cost
            - kalshi_fee_upper_bound
            - polymarket_fee_upper_bound
            - usd_stablecoin_basis_haircut
            - funding_withdrawal_gas_cost
            - latency_slippage_buffer
            - unmatched_leg_emergency_loss_buffer
```

要求：逐档计算，不用 last/mid；只有规则完全等价才采用 `$1` 名义 payout；美元与稳定币留 basis/资金/链上 haircut；Kalshi fee override 和逐 fill 舍入进入上界。

两交易所没有原子提交。两边都用 FOK 也可能第一边成交、第二边因价格、429 或断线失败。未来获合规批准后的最小执行序列应为：

1. 全局仅一个跨场所 execution in flight，复用现有锁。
2. 确认前重拉双边盘口、费用、目标 shard 状态/余额、规则哈希和时钟；变化即预览失效。
3. 先落盘两个 venue-native legs、exchange index 和幂等键。
4. 仅人工确认、限价 FOK、小额；不用市价单。
5. 第一腿结果不明时不交第二腿；先按订单/fills/positions 核对。
6. 一腿成交、另一腿失败时进入 breaker/incident，按预批损失上限处置，不无限追价。
7. 重启先 reconcile 未终结 legs，再允许新 execution。

现有 `$20` 正常成本、`$2` 紧急损失可作为未来初始上限，但不构成 Kalshi 实盘授权。Kalshi 无 Polymarket merge 语义，`merge_once` 不能跨用。

## 最小接入形状

```text
Kalshi public REST monitor
  -> Kalshi 原生 market/book snapshot
  -> reviewed cross-venue match registry
  -> fee-aware paper opportunity
  -> 现有 store/runtime/dashboard 的只读投影

未来且仅在合规批准后：
Kalshi authenticated client
  -> venue-native order/fill/position legs
  -> 现有 lock / preview / breaker / incident / reconcile controls
```

- 不把 Kalshi 放进 `PolymarketTradingClient`。
- 不修改 Polymarket `BookLevel/PairIntent` 去假装两边字段相同。
- 复用 `PredictionArbitrageStore` 的 execution/leg/incident/idempotency 设施，但载荷显式带 `venue` 和原生 IDs。
- 首期低频发现，只对少量候选拉 book；无需认证 WS 或新增 HTTP/WS/RSA 依赖。
- Demo 阶段再比较固定官方 async SDK 与直接规范实现；必须通过签名、V2、fixed-point 和 exchange-index 兼容测试。
- Demo/production Keychain 名称与配置完全分离；只有出现真实重复后才抽 secret helper。

## 分阶段计划与验收

### P0：资格门禁与公开契约

- 记录协议版本、生效日和 Restricted Jurisdiction 结论；production writes/key 均不存在。
- 真实调用 exchange status、events、markets、单市场 orderbook；验证 cursor、fixed-point、exchange indexes。
- 默认排除 MVE；整个阶段无认证 secret、订单请求或生产写操作。

### P1：Kalshi 只读发现和行情

- 走完 cursor；保存原生 IDs、exchange index、规则、时间、来源和规则哈希。
- 正确推导 ask、逐档成本、tick/price range/fractional count。
- shard 状态单独展示；429/5xx/超时/陈旧/格式变化都 fail closed 为 unavailable/stale。
- focused tests + 真实 public REST。若进入 Dashboard，最终按项目规定运行 `make acceptance`，其结果是唯一完成状态。

### P2：语义匹配与纸面信号

- 正例必须同命题、同时间/时区、同结算源、同异常处理。
- 同标题但不同运算符、时区、来源、争议/取消或 payout 的反例必须拒绝。
- 规则哈希变化立即撤销；LLM 只能进入 candidate queue。
- 信号展示双边深度、费用上界、资产折价、数据年龄和规则证据；没有下单按钮。

### P3：Demo 认证、WS 和订单生命周期

- Demo/production URL 与 Keychain 完全分离；日志无私钥、签名和敏感头。
- RSA-PSS、毫秒时钟、query-free path 符合规范。
- WS snapshot/delta/sequence 正确；断线/gap/心跳超时 fail closed，仅完整 snapshot 后恢复。
- 真实 Demo V2 FOK/IOC/GTC、撤单、orders/fills/positions/balance/settlement 完成。
- 按 market exchange index 路由，以对应 shard status 和 balance breakdown 判断 readiness。
- 超时用同一 client ID 核对/重试；验证 429、维护/暂停、lifecycle、startup reconciliation。
- 明确 Demo 只证明技术兼容，不证明生产价格、成交质量或资格。

### P4：跨场所 paper execution

- replay 覆盖两腿成交、一腿失败、状态不明、部分成交、429、断线、陈旧盘口、费用变化和规则不匹配。
- 不确定状态进入 breaker/incident/reconcile，不能继续新 execution。
- P&L 按逐档价格、逐 fill 费用、USD/稳定币折价和资金成本计算。
- 进程重启后从持久化 legs 恢复。

### P5：Production live trading

**当前状态：BLOCKED（合规），不是待实现。**

只有当时有效的协议、账户/KYC、实际所在地和主体均得到书面允许，才可重新立项。届时仍需独立 production key/最小 write scope/人工双门禁，小额人工确认 FOK、单 execution in flight，生产费用/余额/shard/maintenance preflight，以及真实订单、成交、仓位、日志、PID 和运行 SHA 核对。

没有用户对真实资金 canary 的明确授权，不执行生产试单。任何代理/VPS 绕区方案均不在允许范围。

## 下一步

现在只批准 P0：把合规结论变成生产写入硬门禁，并用无凭证 production REST 做契约探测。P0 通过后再写窄 spec 做 P1 的 Kalshi 专属只读监控。不要先改现有执行模型，也不要在事件匹配审计完成前展示“跨所套利可执行”。

实现 spec 至少要固定：P0/P1 无写操作；原生 ID/exchange index/规则哈希模型；stale/unavailable 真值；contract fixtures + 一次真实 public REST 验证；P2 前无跨所交易意图。

## 官方资料索引

- 合规：[Member Agreement](https://kalshi.com/docs/kalshi-member-agreement.pdf)、[International access](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)
- 环境/认证：[API environments](https://docs.kalshi.com/getting_started/api_environments)、[API keys](https://docs.kalshi.com/getting_started/api_keys)、[Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- 规范：[SDK overview](https://docs.kalshi.com/sdks/overview)、[Official Python starter](https://github.com/Kalshi/kalshi-starter-code-python)
- 数据：[Events](https://docs.kalshi.com/api-reference/events/get-events)、[Markets](https://docs.kalshi.com/api-reference/market/get-markets)、[Orderbook](https://docs.kalshi.com/getting_started/orderbook_responses)、[Pagination](https://docs.kalshi.com/getting_started/pagination)
- 实时：[WebSocket](https://docs.kalshi.com/getting_started/quick_start_websockets)、[Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)
- 交易：[Create V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)、[Cancel V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)、[Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)、[Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions)
- 运行：[Fee Schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)、[Rate limits](https://docs.kalshi.com/getting_started/rate_limits)、[Maintenance](https://docs.kalshi.com/getting_started/maintenance_and_pauses)、[Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding)
