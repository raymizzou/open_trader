# Predict.fun 接入研究

日期：2026-08-01  
范围：`open_trader` 的跨交易所预测市场研究，不修改业务代码、配置或运行中的服务。

本文把用户所说的 “predict” 解释为 [Predict.fun](https://predict.fun/)。如果指的是另一家交易所，需要替换本报告的接口结论。

## 结论先行

Predict.fun 有完整的公开 REST API、订单簿 WebSocket、JWT 账户认证和官方 Python/TypeScript SDK，技术上可以接入。但它不是当前 Polymarket 路径的可替换数据源：链是 BNB Chain，抵押资产是 USDT，签名/审批/结算模型不同，而且官方下单接口目前以单个订单为单位，不能把跨交易所两腿当成原子交易。

建议按以下顺序推进：

1. **只读接入**：市场目录、市场状态、YES 订单簿、WebSocket 重连与新鲜度。
2. **跨市场映射**：利用 Predict 市场中的 `polymarketConditionIds` / `kalshiMarketTicker` 作为候选线索，再逐条核对规则、结算和结果语义。
3. **Predict 单 venue no-submit 闸门**：读取账户、审批和余额；用官方 SDK 构造并签名订单，但不广播。
4. **单 venue 受控测试**：先验证 Predict 自己的订单生命周期和链上结算，再讨论跨 venue 下单。
5. **跨 venue 生产执行**：必须另写设计，覆盖两腿非原子、单腿成交、不同抵押资产、gas、取消、结算延迟和人工恢复。

当前仓库的预测市场设计已经明确把 `Predict.fun`、Kalshi 和跨 venue 语义匹配列为 deferred，因此本研究不应直接把它接进现有 `PairIntent` 或 Polymarket 下单器。[现有设计的 rollout 与 deferred 范围](../superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md#21-rollout)

## 1. 官方接口与接入面

| 能力 | 官方入口 | 认证 | 接入判断 |
| --- | --- | --- | --- |
| REST 主网 | `https://api.predict.fun/` | API key；个人操作再加 JWT | 市场、订单簿、账户、下单等 API；官方文档仍标为 beta。 |
| REST 测试网 | `https://api-testnet.predict.fun/` | 文档说明可无 API key 访问 | 适合先做读路径和签名/订单流程验证，不代表主网权限已就绪。 |
| WebSocket | `wss://ws.predict.fun/ws` | 握手时传 `x-api-key`，也支持 `apiKey` 查询参数 | 适合实时订单簿、交易状态、市场状态和账户事件。 |
| 文档 | [OpenAPI 文档](https://api.predict.fun/docs) | — | 应锁定实际使用的 API 版本并保存响应样本。 |
| SDK | [Python SDK](https://github.com/PredictDotFun/sdk-python)、[TypeScript SDK](https://github.com/PredictDotFun/sdk) | — | 用 SDK 做 EIP-712 订单构造、签名和链上操作；不要手写签名结构。 |

官方概览给出的主网/测试网地址、SDK、API key 和默认速率限制是：测试网无 key 时 240 requests/min；主网默认每个 API key 240 requests/min。主网建议使用 `ap-northeast-1`，因此跨境运行还要把网络延迟和连接稳定性纳入新鲜度判断。[官方 API 概览](https://dev.predict.fun/)

### 本地只读核验

2026-08-01 在本机执行了以下公开测试网请求：

```sh
curl -sS --max-time 10 -H 'Accept: application/json' \
  -w '\nHTTP %{http_code}\n' \
  'https://api-testnet.predict.fun/v1/markets?first=1&status=OPEN'
```

结果为 HTTP 200。返回样本包含一个 OPEN 市场、YES/NO outcomes、`decimalPrecision=2`、非零 `feeRateBps`、`isNegRisk=false`，并出现 `polymarketConditionIds`。这只验证了公开测试网读路径和响应形状，不验证主网 API key、JWT、钱包、审批、签名或真实下单。

### 只读所需的 REST

- `GET /v1/markets`：分页发现市场，可按状态、标签、变体等筛选。[市场列表](https://dev.predict.fun/get-markets-25326905e0)
- `GET /v1/markets/{id}`：读取完整市场和结果定义。[市场详情](https://dev.predict.fun/get-market-by-id-25552989e0)
- `GET /v1/markets/{id}/orderbook`：读取订单簿；官方返回的价格以 YES 视角表示。[订单簿接口](https://dev.predict.fun/get-the-orderbook-for-a-market-25326908e0)
- `GET /v1/search`：可用于搜索市场，但搜索默认速率限制低于普通市场读取，不能作为实时轮询主路径。[搜索接口](https://dev.predict.fun/search-categories-and-markets-27399810e0)

### WebSocket 主题

第一版只需要订阅这些主题：

- `predictOrderbook/{marketId}`：聚合的 `bids` / `asks`、版本和更新时间；有快照。
- `predictTradingStatus/{marketId}`：`OPEN`、`MATCHING_NOT_ENABLED`、`CANCEL_ONLY`、`CLOSED`。
- `predictMarketStatus/{marketId}`：市场、outcome 和 resolution 状态；有快照。
- `predictMarketChanged/{marketId}`：市场定义变更。
- `predictWalletEvents/{jwt}`：账户订单、成交、链上提交、成功或失败事件；没有快照，必须配合 REST 重建状态。

WebSocket 使用 JSON 文本帧，服务端约每 15 秒发 heartbeat，客户端必须回显；断线后要指数退避、重新订阅并重新获取快照。`orderAccepted` 不是最终成交或结算成功，账户事件还会继续报告链上提交、成功和失败。[WebSocket 总则](https://dev.predict.fun/general-information-1915499m0)、[订阅主题](https://dev.predict.fun/subscription-topics-1915507m0)、[请求格式](https://dev.predict.fun/request-format-1915501m0)

## 2. 市场身份与跨 venue 映射

Predict 的市场模型已经暴露了跨交易所线索：

- Predict 自己的 `id`、`conditionId`、`question`、`description`、`outcomes`、`resolution`、`tradingStatus`、`status`。
- 结构性参数：`decimalPrecision`、`feeRateBps`、`isNegRisk`、`isYieldBearing`、`marketVariant`、`marketType`。
- 结果级字段：outcome 的 label、`indexSet`、`onChainId`、bid/ask。
- 跨 venue 字段：`polymarketConditionIds` 是数组；`kalshiMarketTicker` 是字符串。[官方 Market 模型](https://dev.predict.fun/market-14037477d0)、[官方 Outcome 模型](https://dev.predict.fun/outcome-14037514d0)

这些字段只能作为**候选映射证据**，不能单独当作“两个市场规则完全等价”的证明。特别是 `polymarketConditionIds` 没有给出可直接复用的 outcome 级 YES/NO 映射，也没有替代对方交易所规则页面的作用。

跨 venue 候选至少要双向核对：

1. 问题文字和描述是否是同一事件，而不是相近标题。
2. YES/NO 的含义、结果集合、时间边界、时区和截止条件是否一致。
3. resolution provider/oracle、取消/无效/争议处理、结算时间和 payout 是否一致。
4. `conditionId`、outcome token、`indexSet`、`onChainId` 只在各自 venue 内解释；不能把同名或同值 ID 当作跨 venue token identity。
5. fee、最小数量、tick/decimal precision、深度、交易状态和可结算状态是否同时满足。

研究阶段建议保留 venue-qualified identity，而不是立即泛化现有类型：

```text
venue
venue_market_id
venue_condition_id
venue_outcome_id / token_id
outcome_label
index_set
chain_id
collateral
decimal_precision
fee_rate_bps
market_variant
resolution_fingerprint
source_refs
observed_at
```

其中 `resolution_fingerprint` 应由规则、时间、oracle 和 outcome 定义产生；它比标题或外部 condition ID 更适合作为执行前的相等性闸门。

## 3. 订单簿与价格归一化

Predict 的 REST/WebSocket 订单簿以 YES 价格返回，价格范围是 0 到 1，`asks` 按价格升序、`bids` 按价格降序，数量是 shares。NO 侧应按市场的 `decimalPrecision` 用精确十进制定点计算：

```text
NO ask = 1 - YES bid
NO bid = 1 - YES ask
```

因此不能用二进制浮点直接做 `1 - price`，也不能把 Yes/No 的 token ID 顺序硬编码。官方文档特别说明了 decimal precision、空侧和价格排序的处理。[订单簿细节](https://dev.predict.fun/doc-685654)

只读 watcher 的最小流程是：

1. REST 拉取市场目录和完整市场定义。
2. 过滤 `status`、`tradingStatus`、可见性、二元 outcome 和 resolution 状态。
3. 订阅订单簿、交易状态、市场状态；保存 WS 版本和 `updateTimestampMs`。
4. 每次重连先重新取 REST 快照，再恢复订阅，避免把旧 book 当成新 book。
5. 对跨 venue 候选同时保存两边的 book 时间、版本、交易状态和来源，任一边过期就 fail closed。

## 4. 认证、钱包和链上依赖

认证链路不是静态 token：先 `GET /v1/auth/message` 取得动态消息，用 EOA 或 Predict Account 签名，再 `POST /v1/auth` 得到 JWT；主网请求还要带 API key。动态 message 不应硬编码。[官方认证流程](https://dev.predict.fun/doc-663127)、[获取认证消息](https://dev.predict.fun/get-auth-message-25326899e0)

有两种账户选择：

- **EOA**：第一版更简单，单一私钥、单一 maker/signer，适合先做 no-submit 和单 venue 验证。
- **Predict Account Smart Wallet**：需要 Predict account/deposit address 和 Privy wallet private key 两套身份；订单的 maker/signer 是 Predict account，Privy 钱包负责签名/链上操作。官方订单指南还要求为 Privy 钱包准备 BNB 以完成审批或取消。

SDK 常量和 README 使用 BNB Mainnet chain ID `56`、BNB Testnet chain ID `97`；市场交易抵押资产是 BNB Chain 上的 USDT，示例金额使用 18 位 base units。官方 FAQ 的个别文字出现 “ETH” 表述，而链、SDK 和订单指南均指向 BNB，因此正式上线前应向 Predict 支持确认 gas 资产和账户模式，不应凭 FAQ 单句配置。[官方 Python SDK](https://github.com/PredictDotFun/sdk-python)、[官方合约常量](https://github.com/PredictDotFun/sdk/blob/main/src/Constants.ts)、[订单指南](https://dev.predict.fun/doc-679306)

对 `open_trader` 的凭据建议：API key、EOA/Privy 私钥和 JWT 生成所需的身份均放 macOS Keychain；配置文件只保存非敏感的账户地址、chain 和环境。JWT 在进程启动时动态生成，过期或 WebSocket 认证失败时重新走认证流程。

审批依赖至少包括 USDT 和相应 Conditional Tokens/CTF Exchange；NegRisk、yield-bearing 等变体的合约不同，不能把标准二元市场的审批地址复用过去。让 pinned 官方 SDK 负责合约地址、typed data 和审批检查，比在业务代码中复制地址更安全。

## 5. 下单、取消和结算状态机

官方订单流程是：读取市场 `feeRateBps` 和变体 → 用 SDK 构造 order → 构造 EIP-712 typed data → 签名和计算 hash → `POST /v1/orders`。订单请求包含 price/amount、`isFillOrKill`、`isPostOnly`、slippage、`isMinAmountOut` 等约束。[创建订单](https://dev.predict.fun/create-an-order-32534694e0)、[TypeScript SDK 示例](https://github.com/PredictDotFun/sdk)

正式执行至少要区分这些状态：

```text
signed
  -> submitted_to_api
  -> order_accepted / order_not_accepted
  -> matched
  -> transaction_submitted
  -> transaction_success / transaction_failed
```

`order_accepted` 只表示撮合 API 接受订单，不能直接当作 filled，更不能当作链上结算成功。重启恢复也必须从订单 REST、match events、account activity 和 wallet events 重新拼出最终状态。[订单事件主题](https://dev.predict.fun/subscription-topics-1915507m0)、[账户 activity](https://dev.predict.fun/get-account-activity-32534697e0)、[订单 matches](https://dev.predict.fun/get-order-match-events-25663812e0)

取消有一个容易踩坑的双层语义：`POST /v1/orders/remove` 或按 hash 移除可以快速把订单从撮合簿移走，但官方说明这**不等于链上取消**；最终状态仍要做链上取消和 REST/WS reconciliation。[快速移除订单](https://dev.predict.fun/remove-orders-from-the-orderbook-25326904e0)、[按 hash 移除](https://dev.predict.fun/remove-orders-by-hash-38139973e0)

当前公开订单创建文档展示的是单个 `order` 请求；没有看到可以把两个不同 venue 的签名订单作为一个原子提交的官方批量接口。即使 Predict 自己未来提供批量，Predict 与 Polymarket/Kalshi 之间仍然不可能由一个 venue API 保证原子性。因此跨 venue 两腿必须按“两个独立执行请求”设计，而不是沿用当前 Polymarket 的 batch 语义。

## 6. 跨交易所套利的经济约束

只有在两边规则等价、每股最低 payout 等于同一数值且抵押资产可按审慎汇率折算时，才可以计算候选边际。以 Predict YES 与另一 venue NO 为例，候选成本应至少是：

```text
total_cost =
    predict_yes_ask
  + other_venue_no_ask
  + predict_fee
  + other_venue_fee
  + gas_and_chain_cost
  + settlement_delay_buffer
  + one_leg_remediation_reserve
```

反方向也要计算。Predict 的 YES/NO 价格先按 `decimalPrecision` 归一化；`feeRateBps` 以市场实际值为准，不能沿用当前 V1 的 fee-free 假设。我们在 2026-08-01 对 Predict 测试网公开市场做了只读检查，`GET /v1/markets?first=1&status=OPEN` 返回 HTTP 200；样本市场包含 Yes/No、`decimalPrecision=2`、`feeRateBps=200` 和 `isNegRisk=false`。这只证明公开读路径可用，也证明不能默认费用为零，不代表主网账户、签名、审批或下单已就绪。

真正的“可参与”还要加上：

- 两边都在可交易状态且 book 新鲜。
- 两边可同时满足数量、最小订单、tick 和深度。
- fee、gas、汇率、充值/提现和结算等待成本已计入。
- 两边的 USD/USDT/pUSD 不是假设可瞬时互换；资金要分 venue 预置。
- 任何一腿超时、拒单、只成交部分或链上失败都有明确的止损/补腿/平仓路径。
- 价差阈值必须覆盖报价漂移和非原子执行，而不只是覆盖当前两档 ask 的算术差。

在这些条件完成前，UI 只能显示“跨 venue 候选/观察”，不能显示当前产品语义中的 `可参与`，也不能把它描述为无风险套利。

## 7. 与当前 `open_trader` 的边界

当前仓库的设计是 V1 Polymarket、V2 Kalshi、V3 Predict.fun；跨 venue matching 和通用 venue framework 都被明确 deferred，并且 V1 不引入通用多 venue 抽象。[当前设计](../superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md#21-rollout)

可以复用的部分：

- `Decimal` 价格、数量、tick 和深度计算。
- signal/readiness/reconciliation/incident 的状态机思路。
- Keychain 取密钥、freshness 和 fail-closed 习惯。
- 只读 watcher 与后台慢速 discovery 分层的运行模型。

不能直接复用的部分：

- `PairIntent`、`ConfirmedBooks` 和 `condition_id` 目前默认同一 venue 的 YES/NO 对。
- `PolymarketTradingClient` 的 token、pUSD 6 位 base units、relayer readiness 和 `merge_once` 都是 Polymarket 特定实现。
- Predict 是 BNB/USDT/18 位 base units；Predict 的 order accepted、链上结算和取消状态也不同。
- 当前 Polymarket batch 发送不能被解释为跨 venue 原子性。

所以第一笔代码不应是“给 `PolymarketTradingClient` 加一个 `predict=True` 分支”。更小、更安全的切口是独立的 Predict public adapter，只输出带 venue 身份的新鲜市场/盘口快照；在映射规则和账户闸门获批前，不进入现有执行器。

## 8. 推荐的实施闸门

| 阶段 | 产物 | 通过条件 | 禁止事项 |
| --- | --- | --- | --- |
| 0. 兼容性确认 | API key、目标 venue、EOA/Smart Wallet、SDK 版本、支持回复 | 明确主网权限、chain/gas、限频、订单/取消语义 | 不改生产执行路径 |
| 1. Predict 只读 | REST + WS adapter、快照/重连/新鲜度 | 测试网和公开主网读路径可重复；状态与 book 一致 | 不签名、不下单 |
| 2. 映射发现 | `polymarketConditionIds`/Kalshi ticker 候选 + 规则指纹 | 双向核对通过，映射有来源和版本 | 不把候选发成 `可参与` |
| 3. no-submit 账户闸门 | JWT、余额、审批、订单构造/签名、账户读取 | 不广播也能验证完整 typed data 和风险字段 | 不发人工“测试单” |
| 4. 单 venue 受控执行 | Predict 自己的订单、WS 事件、链上 tx、取消/恢复 | 可证明 accepted/matched/settled/failed 的最终状态 | 不先做跨 venue 两腿 |
| 5. 跨 venue 设计 | 独立资金、两腿状态机、单腿恢复、熔断和审计 | 新 spec 明确批准后才可实现 | 不把两 venue 当一个账户或一笔原子订单 |

## 9. 需要用户/官方支持确认的事项

在进入实现前，最关键的决策是：

1. 目标是否是 **Predict.fun ↔ Polymarket**，还是也要覆盖 Kalshi？Predict 已提供两类外部市场字段，但两类规则和资金模型仍需分别验证。
2. 第一阶段是否明确为只读 + no-submit？这是风险最低、也最符合当前仓库 deferred 边界的方案。
3. 账户采用 EOA 还是 Predict Account Smart Wallet？如果选 Smart Wallet，需要准备 Predict account 地址、Privy signer 和 BNB gas。
4. 是否已经有主网 API key 和可用于测试的独立账户？没有的话只能做公开读路径和 SDK 离线构造。
5. 向 Predict 支持确认：主网 API key 申请/地域限制、实际 order rate limit、是否存在官方批量订单、订单 accepted 到链上 settlement 的最终性、标准/NegRisk/yield-bearing 的审批要求，以及文档中 ETH/BNB 表述差异。

## 官方资料

- [Predict.fun API 概览](https://dev.predict.fun/)
- [REST API 文档](https://api.predict.fun/docs)
- [WebSocket 总则](https://dev.predict.fun/general-information-1915499m0)
- [WebSocket 订阅主题](https://dev.predict.fun/subscription-topics-1915507m0)
- [认证](https://dev.predict.fun/doc-663127)
- [市场列表](https://dev.predict.fun/get-markets-25326905e0) / [市场详情](https://dev.predict.fun/get-market-by-id-25552989e0)
- [Market 模型](https://dev.predict.fun/market-14037477d0) / [Outcome 模型](https://dev.predict.fun/outcome-14037514d0)
- [订单簿接口](https://dev.predict.fun/get-the-orderbook-for-a-market-25326908e0) / [订单簿细节](https://dev.predict.fun/doc-685654)
- [创建订单](https://dev.predict.fun/create-an-order-32534694e0) / [订单指南](https://dev.predict.fun/doc-679306)
- [订单/账户活动](https://dev.predict.fun/get-orders-25326902e0) / [持仓](https://dev.predict.fun/get-positions-32675933e0) / [账户 activity](https://dev.predict.fun/get-account-activity-32534697e0)
- [Python SDK](https://github.com/PredictDotFun/sdk-python) / [TypeScript SDK](https://github.com/PredictDotFun/sdk) / [官方合约常量](https://github.com/PredictDotFun/sdk/blob/main/src/Constants.ts)
