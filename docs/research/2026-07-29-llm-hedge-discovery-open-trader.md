# LLM Hedge Discovery 如何应用到 Open Trader

> 研究日期：2026-07-29
>
> 研究对象：[Prediction Market Arbitrage Compendium / Strategy E](https://github.com/Oceanjackson1/Prediction-Market-Arbitrage-Compendium/blob/main/strategies/strategy-e-llm-hedge-discovery.md)
>
> 结论先行：该策略可以作为“跨市场语义候选发现器”，不能直接作为自动套利执行器。Open Trader 的首个版本应只读、不可下单，并用确定性真值表、完整规则快照和人工批准把 LLM 输出降级为线索。
>
> 后续产品决策：操作者在明确风险边界后选择支持“Polymarket 同平台跨合约阈值套利”的人工确认实盘版本。研究结论仍用于限定风险；最终实现范围以 [设计规格](../../superpowers/specs/2026-07-29-polymarket-threshold-hedge-design.md) 为准。

## 一、执行摘要

### 已核实事实

1. Strategy E 的核心想法是让 LLM 找出两个预测市场之间的逻辑关系，再买入一个理论上“所有允许状态至少有一腿赔付”的组合。其实现来源 PolyClaw 自称是实验性、未经审计的软件，并把 LLM 用于 hedge discovery，而不是提供形式化证明。[Strategy E](https://github.com/Oceanjackson1/Prediction-Market-Arbitrage-Compendium/blob/main/strategies/strategy-e-llm-hedge-discovery.md)；[PolyClaw README](https://github.com/chainstacklabs/polyclaw)
2. Strategy E 对单向蕴含的示例有一处关键方向错误。若 `A ⇒ B`，保证至少一腿赔付的是 `NO(A) + YES(B)`；文中同时列出的 `YES(A) + NO(B)` 在允许状态 `A=0, B=1` 下两腿都输。
3. PolyClaw 在审计版本 `1f5a8ab` 中比 Strategy E 的摘要更谨慎地区分了蕴含方向，但仍只把市场问题文本交给 LLM，没有同时提交完整规则、resolution source、截止日期和澄清记录；返回市场又使用问题文本的模糊匹配。因此它不足以构成可审计的语义证书。[hedge.py@1f5a8ab](https://github.com/chainstacklabs/polyclaw/blob/1f5a8ab02b9fd5aa2bbb5b88e5bca8b8947655d1/scripts/hedge.py)；[llm_client.py@1f5a8ab](https://github.com/chainstacklabs/polyclaw/blob/1f5a8ab02b9fd5aa2bbb5b88e5bca8b8947655d1/lib/llm_client.py)
4. Polymarket 官方文档明确要求通过规则而不只是标题理解结算条件；结果还可能是 `Unknown / 50-50`，此时 YES 和 NO 各值 `$0.50`。规则可被澄清，争议也会使结算延期。[Resolution](https://docs.polymarket.com/concepts/resolution)
5. Polymarket 的 FOK 是“单个订单全成或全撤”，批量提交的多个订单仍各自独立接受或拒绝。因此跨市场两腿没有原子成交保证。[Place Orders](https://docs.polymarket.com/trading/place-orders)；[Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)
6. 标准 CTF merge 只能合并同一个 `conditionId` 下数量相等的互补头寸。任意两个由 LLM 发现的跨条件头寸不能 merge，只能持有到结算、redeem，或分别卖出。[Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens)；[ConditionalTokens.sol](https://github.com/gnosis/conditional-tokens-contracts/blob/master/contracts/ConditionalTokens.sol)
7. Open Trader 当前实现专门处理同一市场的 YES+NO：已有深度定价、tick/min-size、FOK、预览确认、过期重读、全局执行锁、熔断、地理限制、钱包与 relayer 就绪检查、事故记录和 Dashboard fail-closed；但数据模型、盈利公式和成功后的 merge 路径都假定“同一个 condition”。参见 [prediction_arbitrage.py](../../src/open_trader/prediction_arbitrage.py)、[prediction_arbitrage_execution.py](../../src/open_trader/prediction_arbitrage_execution.py)、[polymarket_monitor.py](../../src/open_trader/polymarket_monitor.py)。

### 推断

- 这个方法的经济价值可能成立，但“LLM 发现了关系”不是套利成立的证据。套利成立需要三张独立证书：

  1. **语义证书**：完整结算规则确实排除了某些状态；
  2. **数学证书**：在所有仍允许的结算状态中，组合净赔付下界为正；
  3. **执行证书**：按真实深度、手续费和部分成交风险计算后，仍能锁定要求的净收益。

- Open Trader 已经具备大部分执行安全积木，但缺少最重要的“语义证书”和跨 condition 生命周期。直接复用现有 `PairIntent` 或 merge 流程会制造错误安全感。
- 最小且正确的应用不是自动交易，而是在现有预测市场页面增加“语义候选（不可下单）”，让 LLM 缩小人工研究范围，确定性代码负责反例和收益下界。

### 建议

先做一个不发订单、不改现有同市场套利路径的只读 MVP。只有候选经过完整规则快照、确定性验证和人工批准，才允许进入实时订单簿测算；即使进入测算，第一版也不显示“参与”按钮。跨市场实盘应作为后续独立项目，不能混入现有同 condition 的执行模型。

---

## 二、原策略究竟在做什么

### 2.1 设想的流程

Strategy E / PolyClaw 大致分成四步：

1. 从 Gamma 市场列表取问题和价格；
2. 让 LLM 判断市场之间是否存在必要条件或覆盖关系；
3. 选择两个 outcome token，使被认为允许的状态至少有一腿支付 `$1`；
4. 比较两腿价格之和与 `$1`，若有折价则交易。

PolyClaw 还把候选分成 T1/T2/T3 覆盖等级，并在代码中为所有 “necessary” 关系统一设置 `NECESSARY_PROBABILITY = 0.98`。[coverage.py@1f5a8ab](https://github.com/chainstacklabs/polyclaw/blob/1f5a8ab02b9fd5aa2bbb5b88e5bca8b8947655d1/lib/coverage.py)

### 2.2 必须纠正的概念

**事实：** `0.98` 是实现中的主观参数，不是交易所保证、合约约束或经校准的统计结果。代码还把 Gamma `outcomePrices` 同时当作概率和成本，计算 `coverage = target_price + (1-target_price)×0.98`，没有使用可成交 asks、深度、手续费、tick 或最小订单。仓库中也没有对应测试。T1 “≥95% coverage” 只能表示模型认为较可信，不能称为无风险套利。

**推断：** 若关系有 2% 概率判断错误，最坏状态可能是两腿都归零。把 `1 - 成本` 展示成固定 profit 会掩盖尾部损失；它至多是“关系为真时的 gross floor”。

**建议：** Open Trader 不引入 coverage tier 作为可交易等级。交易资格必须是离散状态集合上的确定性下界，不使用 LLM 自报概率替代证明。

### 2.3 Strategy E 的方向性错误

令 A、B 是两个市场结算为 YES 的布尔值，且规则确实保证 `A ⇒ B`。允许状态为：

| A | B | 是否允许 | `NO(A)+YES(B)` | `YES(A)+NO(B)` |
|---:|---:|:---:|---:|---:|
| 0 | 0 | 是 | 1 | 1 |
| 0 | 1 | 是 | 2 | 0 |
| 1 | 0 | 否 | 0 | 2 |
| 1 | 1 | 是 | 1 | 1 |

**事实：** `NO(A)+YES(B)` 的 gross payout floor 是 `$1/组`；`YES(A)+NO(B)` 的 floor 是 `$0/组`。

审计版本 PolyClaw 的 `derive_covers_from_implications` 实际按正确方向构造组合；错误来自 Compendium 汇总页，不能据此否定代码中所有方向处理，但足以说明二手策略说明不可作为交易规格。[hedge.py@1f5a8ab](https://github.com/chainstacklabs/polyclaw/blob/1f5a8ab02b9fd5aa2bbb5b88e5bca8b8947655d1/scripts/hedge.py)

**建议：** LLM 只返回关系类型和证据，不返回最终买腿。确定性代码根据关系枚举状态并生成组合，避免自然语言中的方向错误进入订单层。

---

## 三、形式化套利条件

### 3.1 通用定义

对一组允许结算状态 `S`，腿 `i` 的数量为 `q_i`，状态 `s` 下每份支付为 `p_i(s)`：

```text
gross_floor = min over s in S of Σ(q_i × p_i(s))

net_floor =
  gross_floor
  - executable_acquisition_cost
  - taker_fees
  - gas_or_conversion_cost
  - execution_buffer
  - settlement_risk_buffer
```

只有同时满足以下条件才可称为“可执行套利候选”：

```text
net_floor >= MIN_PROFIT_USD
net_floor / executable_acquisition_cost >= MIN_EDGE
所有腿数量、tick、最小订单、深度、规则和账户状态均已通过校验
```

Open Trader 当前同市场路径的边界是：正常总成本上限 `$20`、最小净利润 `$1`、最小边际 `1%`、钱包上限 `$65`，并要求等量 YES/NO。跨市场研究应先沿用这些保守上限，不应因为机会更复杂而放宽。[prediction_arbitrage.py](../../src/open_trader/prediction_arbitrage.py)

### 3.2 四类可验证关系

令每腿购买相等份数 `q`：

| 已证明关系 | 允许状态约束 | 理论组合 | gross floor |
|---|---|---|---:|
| `A ⇒ B` | 不允许 `(1,0)` | `NO(A)+YES(B)` | `q` |
| `B ⇒ A` | 不允许 `(0,1)` | `YES(A)+NO(B)` | `q` |
| A、B 互斥 | 不允许 `(1,1)` | `NO(A)+NO(B)` | `q` |
| A、B 完备 | 不允许 `(0,0)` | `YES(A)+YES(B)` | `q` |

若两腿数量不同，以 `A ⇒ B` 的组合为例，gross floor 是 `min(q_A, q_B)`。多出的份数仍是方向性风险，因此 Open Trader 应只按能够成对锁定的相等数量计算收益下界。

### 3.3 50-50、取消和非二值边界

**事实：** Polymarket 的市场在无法明确判断时可能以 50-50 结算；YES/NO 各支付 `$0.50`。[Resolution](https://docs.polymarket.com/concepts/resolution)

**推断：** 仅在 `{0,1}` 上成立的蕴含，并不自动覆盖一个市场为 `0.5`、另一个市场为 `0` 或 `1` 的情况。不同规则对取消、数据源中断、措辞歧义和截止时间的处理也可能不一致。

**建议：** 状态枚举器必须包含每个市场规则允许的所有 payout 向量。只要无法从规则明确排除 `0.5` 或特殊结算，候选就保持 `research_only`；不得用“通常会这样结算”补足证明。

---

## 四、语义可靠性：LLM 可以做什么，不能做什么

### 4.1 原实现的问题

**事实：**

- PolyClaw 的 hedge prompt 主要输入 question 文本；其 market dataclass 虽有 `end_date`，却没有完整 rules / `resolutionSource`，`hedge.py` 也不把 description 或 end date 发给模型。Polymarket 官方说明真正控制结算的是 rules、resolution source、end date 和 edge cases。[hedge.py@1f5a8ab](https://github.com/chainstacklabs/polyclaw/blob/1f5a8ab02b9fd5aa2bbb5b88e5bca8b8947655d1/scripts/hedge.py)；[Resolution](https://docs.polymarket.com/concepts/resolution)
- Gamma 的市场和事件接口可提供 `description`、`resolutionSource`、`endDate`、`conditionId` 等字段，应一并固定下来。[Get market by ID](https://docs.polymarket.com/api-reference/markets/get-market-by-id)；[Get event by ID](https://docs.polymarket.com/api-reference/events/get-event-by-id)
- 原实现通过自然语言问题做模糊字符串映射，可能把相似标题映射到错误市场。

**推断：** 仅凭标题得出的 `A ⇒ B` 很容易被时间窗口、统计口径、地域、数据修订、结算来源和“截至/期间/宣布/生效”等词击穿。

### 4.2 已发表实证对 LLM 的警告

**事实：** AFT 2025 的 IMDEA Networks 论文在 46,360 个美国大选市场对上先由 LLM 标出 1,576 个 dependent pairs，约束 checker 后剩 374 个，人工核验最终只有 13 个真正符合论文定义，其中 11 个是 NegRisk–NegRisk、2 个是 NegRisk–Single。换言之，checker 后候选的人审命中率约为 `13/374 = 3.5%`；论文流程本身也明确保留 checker 和人工核验。[Unravelling the Probabilistic Forest，第 14 页](https://suarez-tangil.networks.imdea.org/papers/2025aft-arbitrage.pdf)

**推断：** 即使研究级 prompt 加约束 checker，raw LLM 仍更适合高召回找线索，而不适合担任自动交易准入器。且 13 个真关系几乎都利用官方 NegRisk 结构，进一步说明优先读取平台声明的结构关系，比让 LLM 猜任意市场对更可靠。

### 4.3 Open Trader 的 LLM 输出契约

**建议：** 使用项目已有的 OpenAI 依赖和严格 JSON 模式，不新增 OpenRouter、多模型编排、向量数据库或通用 agent 平台。模型输出只允许：

```json
{
  "market_a_condition_id": "0x...",
  "market_b_condition_id": "0x...",
  "relation": "A_IMPLIES_B",
  "rule_evidence": [
    {
      "market": "A",
      "clause": "精确规则片段的短引用",
      "source_url": "https://..."
    }
  ],
  "counterexample": {
    "state": "A=true, B=false",
    "excluded_by": "哪一条规则排除该状态"
  },
  "uncertainties": []
}
```

并执行以下 fail-closed 规则：

1. 只能引用输入中存在的精确 `conditionId`，禁止模糊匹配标题；
2. 缺少完整 description、resolution source、end date 或原始 URL 时不调用模型；
3. JSON 多字段、少字段、未知枚举、证据无法回指原文，一律拒绝；
4. 市场文本视为不可信数据，不能改变系统指令、调用工具或触发交易；
5. 模型必须主动给出反例；无法解释为何反例被规则排除，则关系无效；
6. LLM 服务失败时，只让“语义候选”区域不可用，不影响现有同市场监控。

### 4.4 人工批准不是可选项

**建议：** 每个关系建立人工 allowlist 记录，至少固定：

- 两个 exact condition IDs；
- 两边完整规则文本的规范化 hash；
- relation 类型和应买 outcomes；
- resolution source、end date、时区；
- 审核人、审核时间、批准到期时间；
- 特殊结算和争议路径；
- 一条明确的反例证明。

任何标题、description、source、end date、condition ID 或 rules hash 变化，都立即将状态降为 `rules_changed`，撤销批准。LLM 重跑不能自动恢复批准。

---

## 五、执行现实：价格、深度和非原子两腿

### 5.1 显示价格不是可成交价格

**事实：** Polymarket UI 的 midpoint/last trade 不是保证可执行的报价；买入要吃 asks，卖出要吃 bids，真实成本取决于完整深度。[Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook)

**建议：** 沿用 Open Trader 当前深度遍历方式，但扩展为多 token 同批 `/books` 快照；每条腿必须验证 asset ID、condition ID、timestamp、hash、tick size、min order size、所需累计数量和最坏成交价。[Get order books](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body)

### 5.2 FOK 不能让两腿原子化

**事实：**

- FOK 保证单个订单全成或全撤；FAK 可部分成交并取消余量。[Place Orders](https://docs.polymarket.com/trading/place-orders)
- `postOrders` 可批量提交，但每个返回项独立成功或失败；这不是事务。[Place Orders](https://docs.polymarket.com/trading/place-orders)
- 部分市场对 taker 有延迟，延迟期间订单不能取消，且会重新校验；成交记录还会经历 `MATCHED/MINED/CONFIRMED/RETRYING/FAILED`。[Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)

**推断：** 两个跨市场 FOK 同时发出，仍可能出现第一腿成交、第二腿失败。此时原本的“套利”变成单边预测头寸。

**建议：** 只读 MVP 不提交订单。后续实盘必须复用现有执行锁、预览 TTL、确认时重读、breaker、逐腿回执核对和 incident/remediation 状态机，但要为“跨 condition、不可 merge”设计新的恢复策略。

### 5.3 手续费、tick 和最小订单

**事实：**

- 当前 Polymarket 并非所有市场都免 taker fee，费用随类别和价格变化；不能把零费率写死。[Fees](https://docs.polymarket.com/trading/fees)
- order book 返回 `min_order_size`、`tick_size`、`neg_risk`；tick 还可能通过 websocket 的 `tick_size_change` 动态变化。[Place Orders](https://docs.polymarket.com/trading/place-orders)；[Market WebSocket](https://docs.polymarket.com/api-reference/wss/market)
- 官方页面对 `min_order_size` 的单位存在冲突，order-book 数组排序描述也不完全一致。因此不能依赖文档中的全局单位或数组首尾；应以当前 CLOB/SDK 运行时校验，并显式求 `max(bid)` / `min(ask)`。[Get order book](https://docs.polymarket.com/api-reference/market-data/get-order-book)；[Market Details](https://docs.polymarket.com/market-data/market-details)

**建议：**

1. 每次预览和确认都重新取各腿 fee、tick、min size；
2. fee 未知、字段冲突或 tick 变化时使预览失效；
3. 用真实成交成本加 taker fee 计算 `net_floor`；
4. 对延迟、价格跳动和退出成本保留显式 buffer；
5. 继续执行 `$1` 和 `1%` 双门槛，且以扣除所有 buffer 后的值判断。

### 5.4 Split + 卖出不是无滑点捷径

Strategy E 建议 split 抵押品得到 YES+NO，再卖掉不想要的一侧。

**事实：** split 的确把 `$1` 抵押品变成同一市场一对 YES+NO；merge 则只对同一市场的等量互补 token 生效。[Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens)

**推断：** “split 后卖掉一侧”只是从 asks 买入改成向 bids 卖出，不会消除深度、手续费或两市场非原子风险。只有当 split 成本加卖出两条 bids 的全量可执行经济性更好时，它才有意义。

**建议：** MVP 不实现 split 路由。后续也只把它当作另一条确定性报价路径，与直接 FOK 买入按 all-in cost 比较；不得默认它更优。

---

## 六、结算与持仓生命周期

### 6.1 普通跨 condition 组合

**事实：** 每个 Polymarket 二元市场有自己的 condition 和 outcome tokens；标准 merge 接收一个 condition 下的 partition。[Markets & Events](https://docs.polymarket.com/concepts/markets-events)；[ConditionalTokens.sol](https://github.com/gnosis/conditional-tokens-contracts/blob/master/contracts/ConditionalTokens.sol)

**推断：** 即使两个市场存在严密逻辑关系，合约也不知道这层关系。跨 condition 组合没有 `$1` 的链上提前兑现能力，会占用资金直到两边结算，且可能一边先结算、一边争议延期。

**事实：** `endDate` 表示市场开始具备 resolution 资格，不是保证兑付完成的时刻；规则澄清、proposal 和 dispute 都可能延长实际兑付时间。[Resolution](https://docs.polymarket.com/concepts/resolution)；[How Are Markets Clarified?](https://help.polymarket.com/en/articles/13364548-how-are-markets-clarified)

**建议：** 收益率应计入较晚一边的实际结算期限和资金占用；执行记录要分别跟踪每个 condition 的澄清、resolution、redeem 和退出状态。现有成功后自动 merge 的逻辑不能用于跨市场组合。

### 6.2 Negative Risk 不是 LLM 推断关系

**事实：**

- Polymarket 的 Negative Risk 只适用于平台明确链接的一组互斥结果，并允许把一个结果的 NO 转换为其他结果的 YES。[Negative Risk](https://docs.polymarket.com/concepts/negative-risk)
- 官方 Neg Risk 合约仓库的前提是组内恰有一个 YES；平局、无法判定或全 NO 都是重要边界。[neg-risk-ctf-adapter](https://github.com/Polymarket/neg-risk-ctf-adapter)
- 当前合约页已将旧 Neg Risk Adapter 标记为 CLOB v1 deprecated，同时列出 V2/Combos 相关合约，说明实现处于版本迁移边界。[Contract Addresses](https://docs.polymarket.com/resources/contracts)
- 当前 V2 adapter 源码的转换还可能扣除独立 `feeBips`，输出 collateral 和 outcome tokens 都按 `amount - feeAmount` 计算；不能只扣 CLOB taker fee。[NegRiskCtfCollateralAdapter.sol](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/adapters/NegRiskCtfCollateralAdapter.sol)

**建议：**

1. 绝不把 LLM 推断出的任意互斥关系当成 `negRisk`；
2. 仅信任市场元数据中官方声明的 group/linkage；
3. augmented neg risk 的 `Other`/占位结果保持不可交易；
4. 在当前官方 SDK、合约地址和 no-submit 路径重新验证前，Open Trader 继续把 negRisk 排除在现有执行路径之外；
5. 若以后支持 negRisk，应作为独立、平台声明型策略，不需要 LLM。

### 6.3 抵押品名称已发生变化

**事实：** Strategy E 写的是 USDC.e；PolyClaw 当前 README 和 Polymarket 当前文档描述的是 2026-04-28 切换后的 pUSD。应以当前官方合约页为准，不应复制旧常量。[PolyClaw README](https://github.com/chainstacklabs/polyclaw)；[Contract Addresses](https://docs.polymarket.com/resources/contracts)

**建议：** 所有 collateral、exchange 和 adapter 地址从当前官方 SDK/配置读取并在启动时校验，禁止从研究文章硬编码。

---

## 七、映射到 Open Trader

### 7.1 可直接复用的部分

| Open Trader 现有能力 | 如何复用 |
|---|---|
| Gamma/订单簿监控、REST 同批 books、WS freshness | 获取两个 condition 的完整事实和实时深度 |
| Decimal 深度遍历、tick/min-size 校验 | 计算每腿全量可执行成本 |
| preview → confirm、短 TTL、确认时重读 | 防止候选与下单事实漂移 |
| geoblock、账户、钱包、relayer readiness | 保留为实盘硬门槛 |
| 单机会全局锁、breaker、事故与 remediation | 约束跨市场部分成交风险 |
| Dashboard fail-closed 与 E2E 状态覆盖 | 展示候选、规则变更和不可用原因 |
| OpenAI 依赖、严格 JSON 解析习惯 | 实现最小语义提取器，不增新框架 |

代码入口：[polymarket_monitor.py](../../src/open_trader/polymarket_monitor.py)、[prediction_arbitrage.py](../../src/open_trader/prediction_arbitrage.py)、[prediction_arbitrage_execution.py](../../src/open_trader/prediction_arbitrage_execution.py)、[polymarket_trading.py](../../src/open_trader/polymarket_trading.py)、[prediction_arbitrage_store.py](../../src/open_trader/prediction_arbitrage_store.py)。

本次基线验证运行了上述预测市场相关的 6 个 focused test 文件，结果为 `182 passed in 6.43s`。这证明现有积木有较完整的回归保护，但不是跨市场策略已经正确的证据。

### 7.2 不能直接复用的假设

| 当前假设 | 跨市场 hedge 的差异 |
|---|---|
| 一个 `MarketFacts` / `PairIntent` 对应一个 condition | 至少两个独立 condition 和多个 rules snapshots |
| YES+NO 的 payout 恒为 `$1` | 取决于人工批准的跨市场关系和全部特殊状态 |
| 两腿成交后可 merge | 跨 condition 不可 merge |
| fee=0、negRisk=false 才进入当前路径 | 新路径要按每腿真实 fee 计算；negRisk 仍分离 |
| signal 以单一 `market_id` 标识 | 需要稳定的 composite hedge ID 和版本化规则 hash |
| `actionable` 可显示“参与” | LLM 候选在 MVP 中必须明确不可下单 |

**建议：** 不把跨市场关系硬塞进现有 `PairIntent`，也不在 MVP 迁移 store schema。第一版只在 monitor runtime 里生成短生命周期候选，保持现有交易表和执行接口完全不变。

### 7.3 建议的数据状态

MVP 只需四个对用户可见状态：

```text
llm_candidate       模型提出关系，未人工验证，不可下单
manual_approved     人工固定规则与关系，仍不可下单
priced_read_only    已按实时深度计算净下界，仍不可下单
invalidated         规则、截止时间、来源、book 或模型证据变化
```

不要在第一版引入“置信度分层”“多模型投票”“自动升级为 actionable”或通用 N-leg 策略平台。

---

## 八、最小 MVP

### 8.1 范围

1. 扩充现有市场事实读取：保存 question、完整 description、resolutionSource、endDate、conditionId、token IDs、更新时间及规范化 `rules_hash`。
2. 在后台低频调用一次严格 JSON LLM 提取，只产出 relation proposal、证据和反例。
3. 用确定性状态枚举器验证关系对应的买腿与 gross floor；代码不接受模型给出的收益数字。
4. 只允许两种批准方式：operator allowlist 人工固定关系和双方 rules hash；或对“同一数据源、同一时间点、同一修订口径”的阈值嵌套关系做可符号证明，例如 `X>5 ⇒ X>3`。任一口径不一致仍回到人工审核。
5. 对已批准候选同批读取真实 books，计算等量份数、手续费和净收益下界。
6. Dashboard 新增一个小区域：“语义候选（研究模式，不可下单）”。
7. 不新增下单 API，不调用 CLOB order endpoint，不 split、不 merge、不 redeem。

### 8.2 明确不做

- 不复制 PolyClaw 的模糊问题匹配；
- 不复制固定 `0.98` coverage；
- 不新增 OpenRouter 依赖；
- 不做 rotating residential proxy；交易前继续使用官方 geoblock 检查并遵守可用性限制。Polymarket 帮助中心明确禁止使用 VPN、proxy 或类似工具绕过地域限制。[Geographic restrictions API](https://docs.polymarket.com/api-reference/geoblock)；[Geographic Restrictions](https://help.polymarket.com/en/articles/13364163-geographic-restrictions)
- 不做 unattended trading；
- 不为未来策略先建通用平台、vector DB 或 N-leg DSL；
- 不更改现有同市场套利的执行语义。

### 8.3 为什么这是最小正确切片

**推断：** 当前最大不确定性不是能否发两个订单，而是模型在完整规则上能否达到足够低的假阳性，以及现实市场是否存在持续、可成交的折价。只读 MVP 可以用最低复杂度回答这两个问题，同时不会把错误语义传到真钱执行。

---

## 九、验收标准

### 9.1 单元测试

- `A⇒B`、`B⇒A`、互斥、完备四类真值表全部覆盖；
- Strategy E 中错误的反向组合必须得到 floor `0` 并拒绝；
- 数量不等时只对 `min(q_A,q_B)` 计入保证赔付；
- 任一规则允许 `0.5/Unknown` 且未被状态枚举覆盖时，候选不可批准；
- LLM 返回非 exact condition ID、未知枚举、额外/缺失字段或无法回指的证据时拒绝；
- 缺 description/source/endDate、rules hash 变化或规则过期时降为 `invalidated`；
- fee 未知、negRisk、stale/mismatched book、tick/min-size 不支持、深度不足时不能进入 `priced_read_only`；
- 只有扣除 fee 和全部 buffer 后同时满足 `$1`、`1%`、总成本 `$20`、钱包 `$65` 才显示通过测算；
- 对候选流程断言绝不调用 order POST。

### 9.2 集成测试

- 两个及以上 token 在同一次 `/books` 请求中读取，并逐个校验 asset/condition/timestamp/hash；
- websocket `tick_size_change` 使已有测算立即失效；
- LLM 超时、限流或 schema 错误只影响候选区域，现有同市场监控继续工作；
- description、resolution source、end date 或 rules hash 任一变化会撤销人工批准；
- 同市场现有 `PairIntent`、预览、确认和 merge 测试保持原样通过。

### 9.3 Dashboard / E2E

- 清楚区分“LLM 候选”“人工批准”“实时只读测算”，均显示“不可下单”；
- 缺字段显示“数据不可用”，不补造规则、概率或收益；
- 用户能看到双方问题、condition IDs、resolution source、截止时间、规则 hash、关系、反例、gross/net floor、深度和失效原因；
- 现有同市场机会的“参与”按钮不受影响；
- desktop/mobile、LLM unavailable、rules changed、unknown resolution、stale book 都有 E2E 覆盖。

### 9.4 项目最终门禁

这是 Dashboard 行为变化时，必须遵循项目现有门禁：

1. 开发期间跑 focused tests 和真实只读工作流；
2. 最后一步由实现者运行 `make acceptance`；
3. 只有 `PASS` 才可称为完成；
4. 重新部署 exact accepted SHA；
5. 校验新 PID、cwd、Git SHA、新日志和 review URL HTTP 200，再请用户验收。

`BLOCKED` 不能用 fixture、curl、mock 或截图替代。

---

## 十、从只读到实盘的 Go / No-Go

只有同时满足以下条件，才值得设计第二阶段跨市场执行：

1. 建立一套使用完整规则文本的已知关系/非关系 benchmark；
2. 在“允许进入实盘审核的子集”中，假阳性必须为 0；这仍不能替代逐对人工批准；
3. 每个候选可复现其 exact condition IDs、rules hashes、模型输入输出和人工决策；
4. 连续观察期证明按真实深度、费用和 buffer 后仍有足够频率与容量；
5. 完成每腿 no-submit 签名、订单参数和回执核对；
6. 为“一腿成交、另一腿失败”写出不依赖 cross-condition merge 的人工恢复手册；
7. 明确资金最长占用期、争议延期和分别 redeem 的操作路径；
8. 对当前官方 SDK、pUSD、exchange/adapter 地址完成一次新的主源核验。

### No-Go 条件

以下任一成立都应停止自动执行：

- 关系只能靠常识而不能由两边规则文字证明；
- resolution source、时间范围、统计口径或 50-50 处理不一致；
- LLM 只能给结论，不能给可核对的反例排除证据；
- 需要用历史 midpoint 而不是实时可执行深度才能盈利；
- 一腿失败后的最大方向损失超出正常 `$20` 风险包络；
- 需要把普通逻辑关系冒充官方 negRisk 才能提前退出；
- 当前合约/SDK 路径仍处于未核实的版本迁移状态。

---

## 最终建议

把这个策略应用到 Open Trader 的正确方式，是把 LLM 放在最上游做“找线索”，而不是放在最下游做“准许下单”：

```text
完整规则事实
  → LLM 提出关系和反例
  → 确定性状态枚举
  → 人工固定 condition IDs + rules hashes
  → 实时深度/手续费净下界
  → 只读 Dashboard
```

这条路径复用了 Open Trader 已有的真实数据、风险门禁和 fail-closed 设计，同时把新策略最危险的三处假设——LLM 语义、跨腿原子性、跨 condition merge——明确隔离。若只读数据后来证明机会稀少，系统也只增加了一个小型研究面板；若机会真实存在，再以独立规格设计跨市场执行和恢复流程。
