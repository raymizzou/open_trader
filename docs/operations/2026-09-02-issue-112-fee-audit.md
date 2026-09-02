# #112 费率只读审计报告（2026-09-02）

## 目的与方法

按 #112 验收标准第一条，量化当前生产 ACTIVE 关系集中收费市场的占比与利润虚高幅度，
并验证「gamma 顶层费率字段」作为 fail-closed 甄别主源的数据可靠性（Q1 决策）。

全部只读，未触碰生产写入路径：

- 数据源 1：生产 relation catalog（`~/projects/open_trader/data/prediction_arbitrage/prediction_arbitrage.sqlite3`，
  拷贝至 `/tmp` 后分析；生产 release root = `open_trader-releases/d9dc6919`，服务 PID 29532）。
- 数据源 2：gamma 公开 API 按市场逐个重拉（`/markets?condition_ids=<id>`，closed 市场需 `closed=true`）。
- 数据源 3：CLOB `/fee-rate?token_id=` 与 `/book?token_id=` 抽样旁证。
- 数据源 4：生产 state 端点只读 GET（`127.0.0.1:8769/api/prediction-arbitrage/state`）。

## 关键结论

1. **当前 ACTIVE 关系集 100% 落在收费市场：160/160 个市场 `feesEnabled=true`，816/816 条关系至少含一个收费市场。0 个免费、0 个未知。**
   也就是说 #112 上线后，在 catalog 市场构成改变之前，全部 N_LEG 机会会持续显示
   `UNKNOWN`（`FEE_CHARGING_UNMODELED`）——不是部署后一天的空窗，而是**直到出现免费市场或完成费用建模为止的常态**。
2. **收费远不止体育**。票面以体育（5%）为背景，实际当前集合按 `feeType` 分三类：
   finance_prices（4%）、weather（5%）、crypto（7%），全部 `takerOnly=true`、`exponent=1`、带 maker rebate（0.2–0.25）。
   阈值关系（IMPLIES）捕捉的价格/天气/crypto 类市场恰好都是收费类型。
3. **gamma 字段可靠性 = 甄别策略验证通过**：所有 160 个市场的顶层 `feesEnabled` + `feeSchedule{rate,takerOnly,exponent,rebateRate}` 100% 有值
   （这些行 `trading` 为 null，字段在顶层；现有 `_nested` 先查顶层再查容器，解析侧天然兼容）。
   Q1 的 gamma 主源策略成立，无需 `/fee-rate` 兜底。
4. **`/fee-rate` 端点实证不可用**：对 CryptoPunks 两个 token 均返回固定 `{"base_fee":1000}`（与真实 7% 对不上），
   对天气市场（按 condition id）直接 404。与 py-clob-client#326 的已知不一致一致；维持「仅审计对照、不进运行时」的决策。
5. **审计时点生产无正在显示的机会**（state：0 opportunities、0 open episodes），
   故无「正在被虚高的显示值」可测量；虚高是结构性的——一旦这些市场的盘口凑出正边际，现行代码就会把它显示为合格。

## 费率明细（按市场类型）

| feeType | 市场数 | schedule.rate | takerOnly | rebateRate | 代表市场 | 到期分布 |
|---|---|---|---|---|---|---|
| finance_prices_fees | 151 | 0.04 | true | 0.25 | AAPL 等美股每日收盘价阈值 | 2026-08（已过期） |
| weather_fees | 7 | 0.05 | true | 0.25 | Mt. Washington 风速阈值 | 2026-10 |
| crypto_fees_v2 | 1（2 token，NATIVE_COMPLEMENT） | 0.07 | true | 0.20 | CryptoPunks floor ≥50 ETH | 2027-01-01 |

费率公式（Polymarket 文档口径，`exponent=1`）：每份 taker 费 = `rate × p × (1−p)`，p 为成交价，50¢ 时最大。

## 虚高幅度量化

- 每 $1 赔付的最坏情形 taker 费（p=0.5）：finance 1.0¢、weather 1.25¢、crypto 1.75¢/份。
- 对照资格门槛（净边际 ≥ 1%）：weather/crypto 类市场的费用本身就可能吃掉 1.25–1.75 个百分点的边际，
  即「显示 2% 边际」扣费后可能趋近 0 或为负——与票面判断一致，且 crypto 类更严重。
- 实时例证（审计时点）：CryptoPunks complement yes/no ask = 0.92/0.28，组合成本 1.20，显示边际 −20%（无机会）；
  若盘口凑到 0.98（+2% 显示边际），taker 费 ≈ 0.07×(0.92×0.08 + 0.08×0.92) ≈ 1.0–1.9¢/对，扣费后 ≈ 0.1–1.0%，
  极端价位下转负。票面 NegRisk N=4 体育例（合计 96¢、费 ≈3.6¢/单位 ≈ 成本 3.8%）仍然成立。

## 对 #112 实现的输入

- 甄别主源 = gamma 顶层 `feesEnabled` + `feeSchedule.rate`（解析兼容已在，缺的是进 catalog 行、进 resolver、进读模型）。
- `fee_rate` 必须按市场逐个携带（0.04/0.05/0.07 不同），不能全局常数——与既定设计一致。
- gamma 批量接口限制（condition_ids 不支持多值；closed 过滤敏感）只影响审计脚本，不影响生产 discovery。

## 顺带观察（不属于 #112，建议另立票）

- **catalog 卫生**：816 条 ACTIVE 关系中 806 条的首端点 2026-08 已到期（多为已关闭的 finance 日市），
  仍以 ACTIVE 留在 catalog。属 #110 一类「结算中市场」治理问题，与费率无关。
- 阈值/机械编解码捕捉的市场类型（价格、天气、crypto 阈值）系统性落在收费类型上；
  若要有免费可交易机会，需要市场宇宙扩展或费用建模（#112 的 out-of-scope 后续票）。

## 审计工件

- 生产 catalog 副本与原始数据：`/tmp/zcode_issue112_audit/`（active_payloads.jsonl、cond_fee_status.json、fresh9_fee.json、state.json、cryptopunks_books.json）。
