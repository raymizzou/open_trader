# Prediction 套利统一平台与通用 N 腿引擎设计

## 状态

- 日期：2026-08-09
- 最后修订：2026-08-11
- 状态：已批准
- 关联：GitHub #13、#25、#32、#33、#34、#38–#75
- 范围：独立 Prediction Service、统一机会页面、通用二元 N 腿证明与执行框架、关系审批、观察与自动交易控制

## 目标

把当前彼此独立的 YES/NO 套利和 LLM 关系套利收敛为一个 `N_LEG` 通用套利产品，同时把全部 Prediction 运行所有权从 Legacy Dashboard 迁入独立服务。YES/NO 是 `N=2` 的原生关系类型，LLM 是候选关系发现来源；二者在一次切换后不再拥有独立计算器、执行 owner 或永久策略模式。

设计必须从第一天支持任意 N 条二元合约腿，不先写死两腿或三腿；但通用底层不能演变成全市场、全资产类别或全资金账户的万能平台。首个边界只覆盖二元 YES/NO 合约及交易所规则允许的异常结算状态。

成功后的系统满足：

- 操作员在一个页面看到 `N_LEG` 对 current production generation 全部已激活关系产生的合格套利机会。
- `N_LEG` 以一个统一模式决定观察/人工确认或自动交易；关系类型、发现来源、腿数和范围只用于审批、筛选与审计。
- LLM 只能提出语义关系，不能凭置信度直接获得交易权限。
- 同一数学与执行标准同时用于观察、人工确认和自动交易。
- 核心准入先返回任意一个证明完整且合格的连通组合；全局收益最优性是可选后台优化，不能延迟机会准入。
- 每个可执行机会都有可重放的关系、行情、成本、证明和执行记录。
- Dashboard 发布、重启或故障不再拥有或中断 Prediction 监控与执行。

## 非目标

- 不支持标量、连续区间或非二元赔付合约。
- 不在热路径完成充值、跨链、换汇或资金调拨。
- 不建立跨机会的全局资金分配器。
- 首版正式套利 Entry 只允许 `BUY YES` 与 `BUY NO`；SELL 只用于事故修复且不得超过已确认库存。
- 不把 LLM 输出、历史命中率或统计置信度当成结算安全证明。
- 不承诺形式化证书一定成为首个生产标准；它必须参加同一基准后再决定。
- 不设观察期天数、样本数或自动毕业规则；是否开启自动交易由用户决定。

## 产品模型

### 单一 N_LEG 产品

一次切换完成后，每个新机会只有一个 `engine_owner=N_LEG`。产品用以下正交字段表达来源与范围，而不是再建立策略 owner：

- `relation_type`：例如原生互补 YES/NO、原生穷尽组、机械关系或已批准语义关系；
- `discovery_source`：`VENUE_METADATA`、`RULE`、`LLM` 或 `MANUAL`；
- `leg_count`：最终数量大于零的实际腿数，允许两腿或更多腿；
- `scope`：同事件、跨事件、同交易所或跨交易所。

标准 YES/NO 在切换后表示为 `relation_type=NATIVE_COMPLEMENT`、`leg_count=2`；LLM 提议并经用户批准的关系表示为 `discovery_source=LLM`。同一规范组合指纹只能有一个机会、通知与执行记录；其他发现器再次命中时只追加证据。

切换前的历史 `YES_NO` 与 `LLM_RELATION` 记录保持原样，不回写历史身份。迁移 Shadow 可以并行比较旧路径，但不能成为第二个生产 owner。

### 单一执行模式

`N_LEG` 持有以下二选一状态：

- `OBSERVE_MANUAL`：持续发现、证明和展示；真实订单必须由用户确认。
- `AUTO`：同一安全链路通过后可自动提交真实订单。

一次切换后 `N_LEG` 必须初始化为 `OBSERVE_MANUAL`，绝不继承旧 YES/NO 或 LLM 的 `AUTO` 权限。普通服务重启保留当前 N_LEG 状态；证明等级、执行逻辑、资格门槛、单笔资金边界、三个执行风险边界或可执行范围发生安全相关放宽时，整个 `N_LEG` 自动退回 `OBSERVE_MANUAL`，重新获得用户批准后才能进入 `AUTO`。

每个精确、版本化执行范围另有一个服务端能力上限：`OBSERVE_ONLY < MANUAL_CANARY < AUTO_ELIGIBLE`。这是关系类型、同/跨事件、同/跨交易所及 venue/account 集合的安全能力，不是第三种产品模式或第二套开关：

- `OBSERVE_ONLY` 只能发现、证明、展示和生成 would-submit，并固定返回 `order_ready=false`、`reason=SCOPE_OBSERVE_ONLY`；
- `MANUAL_CANARY` 只允许在 `OBSERVE_MANUAL` 下逐批人工确认；
- `AUTO_ELIGIBLE` 仍需全局模式为 `AUTO`，且 exact scope version 已进入用户批准的 `enabled_execution_scope_version`。

任何 scope version 不匹配均 fail-closed，并使整个 N_LEG 回到 `OBSERVE_MANUAL`。发布新的 observable scope 本身也属于安全范围扩大并先触发降级；可观察范围可以大于已启用执行范围，用户随后可以重新批准仍排除新范围的旧 enabled scope，而不必把新范围一并开放。

同一已启用安全范围内，新批准的具体关系继承当前 N_LEG 模式；批准界面在 `AUTO` 时必须明确提示该关系达到 `ORDER_READY` 后可能自动提交。首次启用跨事件、跨交易所或其他新安全范围属于范围扩大，必须先退回观察。

范围晋级不统一假设同一种 Canary：#68 的同交易所跨事件范围先以 `OBSERVE_ONLY` 交付，观察后可由用户直接把 exact scope version 提升为 `AUTO_ELIGIBLE`；#69 的跨交易所同 observation 范围保持 `OBSERVE_ONLY`，#70 开始时用户先把精确 venue/account version 提升为 `MANUAL_CANARY`，此后通过其余 Gate 的方案才可能成为 `ORDER_READY` 并执行 Canary，Canary 完成只为后续提升 `AUTO_ELIGIBLE` 提供证据；#73 同时跨交易所与跨事件的组合范围固定为 `OBSERVE_ONLY`，在未来独立 Canary Ticket 完成前不得进入 `enabled_execution_scope_version`。

服务另有一个优先级更高的全局熔断器。熔断禁止所有真实订单与自动修复，但不停止行情、发现、证明、页面和历史记录。它不是第二个交易模式。

人工确认只替代“是否自动提交”的决定，不能绕过 relation activation、证明等级、资格策略、价格新鲜度、深度、余额、费用、滑点、安全余量、exact scope capability、N_LEG 模式或全局熔断。

## 统一页面

Prediction 套利工作区保留一个主机会列表。YES/NO、LLM、原生关系、机械关系、腿数、同所、跨所、同事件与跨事件均为紧凑筛选或范围标签，不是独立产品或策略开关。

主列表只包含：

- 关系已批准、模型完整并已激活到 current production generation；
- 固定组合证明与独立 verifier 达到统一的 `QUALIFIED_VERIFIED` 标准；
- 保证最低利润至少为共同计价资产的 1 美元；
- 最低净边际至少为 1%，定义为保证最低利润除以最低组合赔付；
- 保证年化收益率至少为 15%；
- 每条腿的保守 `capital_release_at` 明确且仍在未来，最晚资本释放时间距离当前不超过 30 天；
- 行情、成本、关系与规则仍在有效期内。

数学证明有效但低于任一资格门槛的结果只进入漏斗或历史。通过资格但因余额、实际可下单深度或风险限制不足而不能下单的机会可以进入主列表，但必须明确显示“不可执行”及原因；`OBSERVE_ONLY` 可以展示完全相同的 would-submit 固定方案，但不能称为 `ORDER_READY`。只有 exact scope capability 至少为 `MANUAL_CANARY` 且全部真实执行 Gate 通过时才可能成为 `ORDER_READY`；自动交易只消费其中 scope 为 `AUTO_ELIGIBLE` 且当前获准的机会。未获批准、过期、求解超时或证明失败的记录不能伪装成正式套利机会。

页面顶部显示一个 N_LEG 模式和全局熔断状态。另提供带数量徽标的“待批准关系”入口，展开后包含“待批准”和“已批准”两个视图。每个候选关系至少展示：

- 涉及的市场、交易所、关系类型、发现来源和范围；
- 人类可读关系与规范约束表达；
- 结算规则原文、来源链接与规则指纹；
- 日期、时区、数据源、取消及异常结算差异；
- 批准并进入 activation 检查、拒绝及撤销批准动作。

待批准关系不展示预计收益、利润排序或“套利机会”标签，也不触发 Preview 求解；审批依据是关系语义、结算规则和证据，而不是当时价格。拒绝同一规则指纹后不重复打扰；规则文本或任一关键市场身份变化会产生新指纹。批准针对关系版本，不针对每次价格机会；只有 activation 成功进入 current production generation 后才开始生产监控。N_LEG 已为 `AUTO` 时，批准界面必须明确说明同一 enabled scope 内的关系在成为 `ORDER_READY` 后可能自动提交。

观察面板分开呈现模拟机会与人工确认的真实订单。模拟记录不能写成真实成交或已验证成交率。

#60 前部署的页面必须同时理解旧 contract generation 与 N_LEG contract generation，但只渲染并 mutation 当前 production generation 的控制。#60 在同一切换边界更新 owner 与 generation；不得出现新页面控制旧 owner、旧页面控制 N_LEG，或同一页面同时暴露两代生产写入口的混合窗口。

## 独立 Prediction Service

Prediction Service 是单个常驻进程，拥有：

- Prediction HTTP API；
- 原生、机械、LLM 与人工关系发现适配器，以及一个 N_LEG 监控与执行链路；
- 关系发现、候选审核队列与已批准关系目录；
- 通用模型编译、求解、证明与执行计划；
- 交易所客户端、人工及自动执行、部分成交修复；
- Prediction SQLite、机会 Episode、证明和订单历史。

Frontend Gateway 继续作为浏览器唯一入口，并透明转发整个 `/api/prediction-arbitrage/*` 前缀。Legacy Dashboard 不再组装 Prediction 状态、不打开 Prediction 生产数据库、不创建交易客户端，也不启动任何 Prediction monitor 或 execution service。

外部路径保持兼容，包括 state、history、preview、execution、mode、breaker reset、allowance cleanup 和 cross-auto pause 等现有 GET/POST 表面。CSRF、会话校验、输入验证和幂等控制随写入所有权迁入 Prediction Service；Gateway 不包含领域判断。

服务固定监听 loopback `127.0.0.1:8769`，并提供：

- `/healthz`：进程存活、模块身份、PID、Git SHA、模式与所有权状态；
- Prediction state：完成所有权获取和启动对账前返回 503；ready 后单个行情源降级仍返回 200，但明确标记来源状态并禁止该来源产生可下单机会；
- history：只要历史账本可读即可提供，不被单一实时源故障连带关闭。

### 生命周期与单一所有者

每个独立 release 声明 reader/contract generation。启动顺序固定为：非阻塞获取 Prediction 进程生命周期所有权锁，在持锁状态下只读取一次持久化 `minimum_reader_generation`，确认兼容后才打开生产账本、建立交易客户端、执行启动对账、恢复熔断与未结订单、启动监控器，最后进入 ready。禁止锁前预读、双重检查或复用安装阶段缓存；不兼容时必须在打开生产写连接或启动后台线程前释放锁并保持 503。

关闭顺序固定为：先禁止新 mutation 与自动提交，排空或升级正在处理的执行，停止监控器，持久化并关闭账本，最后释放所有权锁。现有逐笔执行锁继续负责单次订单互斥，不能代替服务生命周期所有权。

## 通用 N 腿领域模型

通用底层以以下稳定概念为边界：

- `ArbitrageProblem`：尚未求解的候选交易动作、整数数量变量、终态、约束、成本切片、边界和目标。
- `CandidateAction`：交易所、账户、合约、买卖动作、结算资产、实际 lot step、数量上下限及各终态单位赔付；YES、NO 或不同方向作为独立候选动作，数量为零表示未选择。
- `ConstraintModel`：所有允许结算状态与数量选择必须满足的布尔或整数约束。
- `TerminalStateSet`：每个合约有限、版本化的 terminal atoms；每个 atom 明确规则版本、终态种类、各 CandidateAction 的定点单位赔付和最保守 `capital_release_at`，每个允许联合结算中每个合约恰好选择一个 atom。
- `ExecutableCostSlice`：经过费用、滑点、安全余量和资产折价后的有界增量成本；#48 定义规范表示，后续真实行情适配器负责生成。
- `PortfolioSolution`：规范求解器选择的实际动作、方向、整数数量与目标值；最终正数量腿数为 N，不能预先枚举 Leg 子集。
- `MarketSolution`：只使用市场深度、关系、结算、成本与统一资格约束形成的证明连通组合；不读取个人余额、模式或熔断。
- `ExecutionSolution`：在同一个规范模型上加入实际余额、单笔上限、总未结算资本和执行风险后重新求解并验证的单一证明连通 support、固定方向、整数数量、订单语义与成本边界。
- `PayoutProof`：结构、组合与行情指纹，最坏结算状态、赔付下界、成本上界、求解上下界、gap 和证明等级；同一记录类型也以明确 result kind 保存组件级资格约束不可行的负证明，不另建第二套 proof 模型。
- `QualificationEvaluation`：使用版本化统一策略对 1 美元、1%、15% 和 30 天四项门槛逐项判断。
- `PartialFillProof`：对固定 `ExecutionSolution` 的全部可达成交向量及联合终态给出最坏损失上界或超限反例。
- `RepairPlan`：事故对账后基于 confirmed holdings 生成的固定补齐或退出动作。
- `RepairPartialFillProof`：对固定 `RepairPlan` 的全部可达部分修复状态给出保守总损失上界或超限反例。

规范问题必须稳定、可序列化且与求解器厂商无关。小规模精确 Oracle 直接求解完整问题：枚举受限的方向、整数数量和全部允许终态，返回全局最优组合；它不仅验证一个预先给定的仓位。Oracle 还可消费调用方提供的版本化资格约束，在明确预算内完整枚举全部候选并产生组件级不可行负证明；它不硬编码具体门槛，也不是正式求解器超时后的正向生产 fallback。超过状态/决策预算时返回带原因的 `UNKNOWN`。

现有 YES/NO、现有 LLM 阈值两腿和现有跨所 YES/NO 在迁移期通过薄适配器进入同一底层。业务代码不能直接构造某个求解器厂商的模型；规范模型与求解器适配器之间必须有单一窄接口，以便基准后只保留一个生产实现。

关系图是“关系约束 ↔ 规范合约/结算观测”的二部超图。不同关系只有共享不可变 `market_contract_id`、共享完整 `settlement_observation_key`，或经其他批准关系传递连接时才属于同一组件；标题、主题、LLM 相似度、发现来源或同时盈利不能建立连接。

每个关系组件向求解器提供 M 个候选动作，求解器一次决定哪些数量大于零，最终得到 N 条实际腿。首版正式 Entry 的候选动作只有 `BUY YES` 与 `BUY NO`。不得预先生成全部两腿、三腿或更多腿组合，也不得把互不依赖的 support 拼成一个执行；可分解 support 必须按稳定 ID 拆分后分别重新求解和验证。

核心准入问题寻找任意一个证明连通、固定组合最坏状态已验证且满足版本化资格约束的组合，不等待全局收益最优。候选顺序必须确定且可重放，每个组件、每个快照只发布一个当前组合。剩余独立预算可以继续最大化保证美元利润，并依次以占用资金更少、腿数更少和稳定 ID 更小作为 tie-break；只有全局上下界闭合时才称为 `OPTIMAL`，改进结果只能在固定方案进入执行 FIFO 前替换当前组合。

## 关系与结算约束

二元市场结果使用布尔结算变量表达，常见关系包括：

- A 蕴含 B：`xA <= xB`；
- A 与 B 互斥：`xA + xB <= 1`；
- 一组结果恰好一个成立：`sum(x) = 1`。

这些只是正常结算下的规范约束例子，不代表任何 LLM 输出可直接执行。关系进入模型前分三类：

1. 交易所原生关系，例如官方 YES/NO 或 NegRisk 互斥集合，只有用户已授权对应“交易所 + 解析器版本 + 关系类型”范围时，新实例才可自动批准。
2. 可机械验证关系，例如同一指标、同一结算日期与同一数据源的阈值单调关系，只有规范字段完全匹配且机械规则模板版本已获用户授权时，新实例才可自动批准。
3. LLM 或人工提出的语义关系，特别是跨事件蕴含、互斥或穷尽关系，必须附规则证据并按具体关系版本逐条批准；provider、置信度或同类历史批准不能授予执行权限。

审批单位是带证据和规则指纹的关系版本，不是每次行情产生的具体 Portfolio。关系批准确认语义并授权旁路编译与 activation 检查，不等于 authoritative production fact、结算模型完整、已经开始生产监控或当前可下单。已批准但终态、赔付或释放时间不完整的关系保留在审计目录和漏斗中，但不能进入可求解运行图。

目录分别保存 `source_evidence_fingerprint`、`relation_semantics_fingerprint`、编译器版本和 `compiled_model_fingerprint`。人工或范围批准绑定来源证据与关系语义，运行图证明绑定编译产物。只有原始规则、来源内容和语义指纹完全不变，且编译器只是把批准时已经存在的事实确定性转换为 terminal atoms、payout 或 `capital_release_at` 时，批准状态才可保留；新的 compiled model 仍须重新通过完整性、一致性、预算和 activation 检查。从新来源补入事实，或改变市场身份、结算/异常规则、赔付、资本释放或关系约束，必须生成新版本、链接稳定 `relation_id` 与前序版本并重新批准，不能通过换 ID 绕过 Episode lineage。

目录分别保存 `approval_status`、`model_completeness` 和 `activation_status`。每次新批准或新版本先在旁路构建候选 generation，检查完整性、一致性和求解预算；只有全部通过才原子激活。候选矛盾或超预算分别标记 `ACTIVATION_BLOCKED_INCONSISTENT` 或 `UNSUPPORTED_SIZE`，保留证据但不进入生产；last-known-good generation 继续运行，系统不得自动丢弃某条关系来让候选“通过”。若当前已激活关系被撤销、失效或来源变化，旧 generation 不能继续冒充有效，受影响组件立即进入重建或 `UNKNOWN`。

批准只允许 candidate generation 做旁路编译和验证；candidate 没有生产求解、监控或下单权。若用户确认冲突候选正确，候选 activation 与冲突旧关系的撤销或修正必须放入同一个原子 `proposed change set`：先对完整候选图验证，成功后一次切换 current generation；任一步失败则整个 change set 不生效，旧 current generation 保持不变，不能先撤旧再尝试激活新关系。

跨版本稳定的 `relation_id`、不可变 `market_contract_id` 与会变化的关系/模型版本必须分开。关系目录保存机器可重放的 `event_identity_basis` 和 `settlement_observation_key`：同事件只能来自同交易所不可变官方 event/group ID，或结算机构/Oracle/数据源、被观测指标、观测时点或窗口、时区、规则版本和异常结算规则全部一致的确定性 key。阈值或结果值只是同一 observation 上的谓词，不进入该 key；标题或 LLM 相似性不能成为身份依据。

每个交易所合约默认拥有独立结算身份。标题或现实事件相同不能自动合并变量；跨交易所或跨合约等价、蕴含和互斥必须来自明确批准并成功激活的规则。同一 current generation 中，每个 `relation_id` 只能有一个激活版本和一个组件归属；重复发现只追加证据。

关系目录的领域模型与 store 属于 `PredictionRuntime`。Shadow 只写隔离副本，不能把审批或 activation 隐式提升、复制或双写到生产；生产目录在任一时刻只有持有 production owner lock 的 Prediction Service 可以打开写连接。组件可以跨 generation 合并或拆分，但 candidate generation 中复制的关系没有交易权，current generation 的组件变化必须同时维护 Episode lineage 与既有执行锁。

数学模型必须把每个合约的全部终态作为一等输入，包括正常 YES/NO、取消、无效、退款和特殊拆分赔付。除非交易所规则明确排除，联合状态必须允许“一条腿异常、其他腿正常”以及不同交易所作出不同官方结果。逻辑关系默认只约束正常终态；异常状态同步或联动必须有单独、版本化的规则证据。任何终态赔付、关系语义或资本释放时间无法确定时，结果为 `UNKNOWN`。

## 保证最低利润与仓位

保证最低利润定义为：

```text
所有允许结算状态中的最低组合赔付
- 最坏可执行买入成本
- 全部费用
- 滑点与安全余量
= 保证最低利润
```

它给出可保证的利润下界，实际利润可以更高。只能称为“结算赔付无亏损”或“保证最低利润”，不能称为完全无风险；关系错误、未完整成交、交易所违约、规则执行、稳定币和操作风险仍然存在。

稳健问题概念上是 `max_x min_s [payout(x, s) - cost(x)]`：方向与整数数量 `x` 由主问题选择，终态 `s` 由独立对手问题在全部允许联合结算中寻找最差状态。生产使用反例约束迭代：发现更差状态就持久化并加入主问题，不能把 `s` 与仓位一起当成普通最大化变量。数量从第一天就是交易所实际 lot step 的整数倍，不允许先求连续解再进行未经证明的四舍五入。买入成本、费用和滑点向上取整，赔付与资产估值向下取整。

数学机会与执行能力分开。`MarketSolution` 不读取个人余额、策略模式或全局熔断；余额不足不能把市场机会改写成 `NO_ARBITRAGE`。`ExecutionSolution` 使用同一个规范模型，加入实际余额、单笔资金上限、`max_total_unsettled_capital`、深度和 `max_partial_fill_loss` 后重新求解和验证，确定固定方向、整数数量、订单语义与成本边界。只有该固定方案的部分成交对手证明为 `PARTIAL_FILL_SAFE`，并且 exact scope capability 允许真实执行时，才可能成为 `ORDER_READY`。

`max_total_unsettled_capital` 汇总全部尚未明确释放的 N_LEG 仓位保守占用资本与 active batch 最大资金预留；未知订单、余额、持仓或释放状态继续按最大占用计入。首版不做跨机会资本优化，只在每次准入时原子比较 projected total 并预留；事故 Repair 只能使用原批次仍锁定的预留，不能借用全局剩余额度或新增资本。

总资本上限收紧到低于当前占用时不强制平仓，但立即阻止全部新 Entry。已经处于事故中的 Repair 只有在不新增或扩大预留、全局熔断关闭、`REPAIR_PARTIAL_FILL_SAFE` 与本轮原子准入均通过时，才可继续消费原批次既有预留。

证明事实与业务资格分开。一个版本化且三个旧来源统一使用的资格策略要求：保证最低利润至少 1 美元、最低净边际至少 1%、保证年化至少 15%，并且所有 terminal atom 的保守 `capital_release_at` 明确且在未来、最晚资本释放时间不超过 30 天。占用天数从行情快照到最晚释放时间按 24 小时向上取整且至少 1 天；保证年化为“保证最低利润 / 保守占用资本 × 365 / 占用天数”，禁止平均时间或 `close_at` fallback。BUY-only 首版的保守占用资本是全部腿最大现金 debit 之和，包含成本、费用、滑点、安全余量和资产折价。1% 与 15% 边界使用定点整数交叉相乘判断，等于门槛视为通过。

四项资格是正式准入问题的可行约束，不是先求无门槛最大利润后的页面过滤。输入完整且任一资格谓词已知为 false 时，固定组合为 `NOT_QUALIFIED`，包括最低组合赔付已知且不大于零、利润/边际/年化不足、最保守释放时间已知但不晚于行情快照，或最晚释放超过 30 天；只有输入、terminal atom、赔付或释放上界缺失而使谓词不可判定时才是 `UNKNOWN`。低于门槛的正利润结果仍可保存为证明事实，但不是正式机会。观察、人工和 AUTO 引用同一资格策略版本；任何门槛放宽使整个 N_LEG 退回观察。

每条腿必须绑定明确交易所、账户、链和结算资产。不同资产不得直接相加；首版只允许配置明确认可的共同计价资产，并计入保守折价、手续费和链上成本。跨所机会只能使用已经预存在各交易所的余额。余额不足不改变数学证明，但使机会不可下单。真正跨币种时，换汇或对冲必须成为组合腿并另行设计。

## 证明等级与求解器选择

求解状态、固定组合证明状态、业务资格状态和全局最优性状态必须分开。独立 verifier 找到更差终态或数值不一致时是 `COUNTEREXAMPLE`。最低利润为零或虽为正但低于资格门槛均不是正式机会，但单个候选不合格不能推出整个组件没有合格机会。

观察、人工确认和自动交易共用一个证明标准。初始标准为 `SOLVER_VERIFIED`：

- 金额、数量和赔付使用整数或保守定点数与明确舍入方向；
- 固定候选的主问题可行性、对手问题最坏状态证明及独立 verifier 必须全部完成；任一侧超时、预算耗尽或状态不明时该候选为 `UNKNOWN`；
- 独立校验路径不复用求解器厂商对象，重建规范输入、约束、逐合约 terminal atom、最坏赔付、成本、资格交叉乘法和保守舍入；
- 保存模型与行情指纹、最坏状态、目标值、上下界、gap、版本和重放输入；
- 小 N 必须与完整穷举 oracle 一致，任何 false-safe 都是发布阻断。

固定组合的最坏状态证明完整、四项资格通过且独立校验为 `VERIFIED` 时，即可成为 `QUALIFIED_VERIFIED` 正式机会，不要求整个候选空间的收益 gap 闭合。核心搜索只有在证明资格约束整体不可行后才返回 `NO_QUALIFIED_OPPORTUNITY`；没有在预算内找到合格证明只能返回 `UNKNOWN`。`NO_ARBITRAGE` 仅用于显式诊断：移除四项业务门槛后证明最大保证利润不大于零，不能进入实时准入关键路径。

组件级 `NO_QUALIFIED_OPPORTUNITY` 必须作为现有 proof record 的负结果持久化，绑定 current component generation、完整关系集合、model/quote/cost fingerprints、`qualification_policy_version`、求解器资格约束不可行终态、独立验证方式/结果和重放输入。首版只接受小规模 Oracle 在预算内的完整枚举，或 #49 已证明可由独立 checker 验证的 infeasibility certificate；任一字段缺失、陈旧、版本不匹配或无法检查只能得到 `UNKNOWN`。固定组合 proof 与组件级负 proof 不能互相冒充。

同一个独立 verifier 按 proof result kind 处理两种输入：固定组合分支重建该 `MarketSolution` 或 `ExecutionSolution` 的最坏终态问题；组件不可行分支从完整规范问题、current generation、关系集合、model/quote/cost fingerprints、资格版本和 solver terminal evidence 出发，只可运行预算内完整 Oracle 枚举，或检查已基准通过的 infeasibility certificate。它不能复用求解器厂商对象或信任求解器自报状态；重复运行同一求解器或换一个求解器得到相同结论也不构成独立负证明，不能检查时必须降为 `UNKNOWN`。

可选收益优化继续闭合全局上下界时，已有组合的证明/业务状态保持 `QUALIFIED_VERIFIED`，其最优性状态为 `QUALIFIED_FEASIBLE`；只有闭合后才标记 `OPTIMAL`。优化超时或 gap 未闭合不使已有正式机会失效，也不能把尚未完成固定组合安全证明的 incumbent 送入正式机会。Oracle 只承担小模型标准答案、Shadow 差分和预算内组件级负证明，不作为生产超时后的正向机会 fallback。

证明按影响范围分层指纹：关系与终态形成 `model_fingerprint`，固定动作和数量形成 `portfolio_fingerprint`，订单簿与成本形成 `quote_fingerprint`，资格判断保存 `qualification_policy_version`。关系变化使全部下游失效；数量变化使组合与行情证明失效；行情变化只重算成本与资格；门槛变化不篡改历史数学证明。

`SOLVER_VERIFIED` 不是机器可验证的形式化定理证明，页面与审计必须如实显示。`FORMALLY_VERIFIED` 代表求解器输出可由独立证明检查器验证的证书。SCIP/VIPR 等精确证书能力从第一天参加基准，但不强制成为首版正向机会证明等级；若大组件要产生 `NO_QUALIFIED_OPPORTUNITY` 并重新武装 Episode，则必须有该组件的可独立检查不可行证书，否则保持 `UNKNOWN`。以后升级统一正向证明等级时，观察与自动交易必须同时切换。

HiGHS、SCIP 与 OR-Tools CP-SAT 使用同一规范语料逐一验证：

- 与小 N 穷举 oracle 的正确性；
- OPTIMAL、INFEASIBLE、TIMEOUT、UNKNOWN、bound 和 gap 语义；
- 整数缩放、保守舍入与数值边界；
- 真实和合成关系组件上的 p50、p95、最坏延迟与内存；
- 相同输入的确定性和可重放性；
- Python 3.12、macOS、Linux/VPS 部署与诊断；
- 开源许可证与生产分发约束；
- 精确证书生成和独立检查的额外成本。

只保留满足所有正确性与运行门槛的最简单生产依赖。性能更快不能补偿 false-safe、状态含糊或部署不可用。

## 实时求解链路

关系发现与价格求解严格分离：

```text
慢路径：规则读取 -> LLM/确定性发现 -> 人工或自动批准 -> 编译并缓存约束
热路径：订单簿更新 -> 必要条件筛选 -> MarketSolution -> ExecutionSolution -> 部分成交证明 -> 机会 Episode
提交前：并发刷新全部订单簿 -> 固定方案快速边界与原子 Gate 检查 -> 模拟或真实提交
```

热路径不调用 LLM，也不为每个 tick 重编译关系。关系图按连通分量独立求解；行情更新只触发受影响的组件。队列合并更新并保持 latest-snapshot-wins，旧 tick 不排队追算。求解有硬时限，超时即 `UNKNOWN`。每层证明绑定对应的关系、规则、组合、成本模型和订单簿指纹；任一输入变化会使依赖该输入的下游决策失效。

每个组件最多有一个正在求解的快照和一个待处理的最新快照。新 tick 替换尚未处理的旧 tick；求解结束时若输入指纹已不是最新版本，结果直接丢弃。系统宁可漏掉短暂机会，也不能追算积压并把陈旧结果送入执行。

核心热路径尽快产生一个 `QUALIFIED_VERIFIED MarketSolution`，随后以账户和风险边界求解固定 `ExecutionSolution`。对该固定方案，独立 fill adversary 覆盖所有可达逐腿成交数量和联合 terminal atoms：只有交易所版本化语义证明为真正 FOK 时，该腿成交量才可缩为 `{0, full}`，否则必须按 venue lot 覆盖 `0..submitted_quantity`。它假设未成交订单全部失败、不再修复，已成交仓位按最大已付成本持有到结算。只有全局安全上界不超过 `max_partial_fill_loss` 且 verifier 通过时返回 `PARTIAL_FILL_SAFE`；超限反例返回 `PARTIAL_FILL_UNSAFE`，超时、订单语义或状态不完整返回 `UNKNOWN`。生产不得枚举成交向量乘积或用随机抽样冒充证明。

`ORDER_READY` 只触发提交前快速检查，不要求行情 exact match，也不重新运行完整 Portfolio 或 fill-adversary 优化：系统并发读取全部腿的最新订单簿，对固定方向与数量重算深度、费用、成本和四项资格，并核对订单语义、模型、组合、关系目录、资格、scope、mode、breaker、余额、账本及部分成交证明指纹。新价格等于或优于边界，或虽变差但仍在固定证明边界内时可以继续；任一边界、指纹或原子 Gate 变化就放弃并退回监控重算。

在首个正式组合发布后，独立的剩余预算优化可以继续寻找保证利润更高的方案并报告上下界与 gap。它不得延迟准入、输出 Top-K 或修改已经人工确认、入队、预留或开始提交的固定 `ExecutionSolution`。

实时求解 Ticket 必须同时衡量求解耗时、端到端决策耗时、机会 Episode 存活时长和执行前价格存活率。统计用于用户判断是否开启自动交易，不形成自动毕业 Gate。

## 执行与部分成交修复

真实 Entry 同时受三个独立风险边界约束：

- `max_total_unsettled_capital`：全部尚未明确释放仓位与 active batch 预留的总资本上限；
- `max_partial_fill_loss`：Entry 固定方案在“不再修复并持有到结算”假设下，任意可达部分成交向量的最大允许损失；
- `max_auto_repair_loss`：事故发生后固定 RepairPlan 在任意可达部分修复状态下的最大允许保守总损失。

三者字段、版本和审计独立，不能互相代替。总资本上限与两个损失上限首次部署均为零；第一次真实 Canary 前由用户分别显式设置。放宽任一边界会使整个 N_LEG 退回 `OBSERVE_MANUAL`。

执行前必须使用最新独立行情源并发刷新全部腿，对固定 `ExecutionSolution` 快速重算深度、费用、保证最低利润、四项资格、余额和风险边界，并在第一条远端请求前以单一本地事务完成版本核对、Episode 绑定、资金预留和安全 Gate 获取。所有人工与 AUTO 请求按成功入队顺序进入统一 FIFO；首版全局只有一个 `execution_batch_active`，不同真实批次从首条远端请求到订单、余额、持仓和预留完整对账前不得重叠，同一批次内的腿仍尽量并发并优先使用真实 FOK 或等价订单。

跨市场和跨交易所没有事务原子性。首个部分成交、未知回执或状态不一致必须在任何修复请求前原子持久化 `execution_incident_active`，阻止整个 N_LEG 的其他新订单，并把模式降到 `OBSERVE_MANUAL`；修复成功和完整对账可以释放事故 Gate，但不能自动恢复 AUTO。全成或全拒且状态明确不是事故。

事故降级发生时，所有已经由 AUTO 生成但尚未发送第一条远端请求的 FIFO 项立即失效；它们不能在事故恢复后沿用旧模式、行情、证明或配置继续提交。

事故处理固定为：

1. 独立查询并对账订单、成交、余额、confirmed holdings 和原批次预留；未知状态不能进入自动修复证明。
2. 在“补齐剩余腿”与“退出已成交腿”中生成一个固定、可重放的 `RepairPlan`；SELL 不得超过相同 venue/account 的 confirmed available inventory。
3. 复用 Entry 的 fill-adversary 与 verifier，对当前 confirmed holdings、已发生现金流和固定 RepairPlan 的全部可达部分修复向量及 terminal atoms 求最坏保守总损失；只有有效全局安全上界不超过 `max_auto_repair_loss` 且 verifier 通过时才为安全，任一超限反例即为不安全，超时或输入不完整为 `UNKNOWN`。
4. `REPAIR_PARTIAL_FILL_SAFE` 只证明固定 RepairPlan 的最坏损失；`repair_order_ready` 还必须在第一条远端请求前，以单一本地事务确认全局熔断关闭，并核对 incident、confirmed holdings、RepairPlan/proof、配置、余额、`total_unsettled_capital`、原批次预留和唯一 repair owner 的版本，再从原批次尚未消耗的预留中绑定本轮最大成本。proof 输入变化使证明失效；账本、余额、预留或 owner 冲突使 `repair_order_ready=false`，两者都禁止发单。
5. 修复再次部分成交时重新对账、生成新的固定 RepairPlan 并重新证明；`UNKNOWN`、超限、超卖、预留不足或无方案均禁止自动修复并转全局熔断或人工处理。

自动 Repair 不能创建或扩大资金预留，也不能因为全局资本上限或账户余额仍有空间而追加资本。服务重启必须先恢复 active batch、Episode 执行锁、事故 Gate、confirmed holdings、预留和总未结算资本并完成对账，之后才可 ready。

## 观察与审计

观察不是低标准运行。它经过与自动交易相同的发现、批准、证明、资格策略、成本、余额、执行前刷新和风险链路，只在最后一步把真实 submit 替换成模拟 submit。N_LEG 不会根据天数或样本数自动切换到 `AUTO`。

历史按机会 Episode 记录，而不是保存每个行情 tick。每个 Episode 有唯一 `opportunity_episode_id`、稳定 `episode_lineage_id`、必要的前驱 lineage 和至多一个 `executed_batch_id`。真实 batch 一经绑定，同一 lineage 的人工点击、新 tick、重新求解、收益优化、版本变化、组件拆分/合并、全拒、事故或重启都不能产生第二个真实 batch；该 Episode 继续更新行情和诊断，但标记 `EXECUTED_FOR_EPISODE`。

Episode 只有在当前模型与资格版本下连续 5 分钟的新鲜快照都携带与 current component generation、model/quote/cost fingerprints 和资格版本完全匹配，且由预算内完整 Oracle 枚举或可独立检查证书验证的 `NO_QUALIFIED_OPPORTUNITY` 负 proof record 后才真正结束，并持久化每次 proof ID。`episode_rearm_gap` 初始值为 5 分钟并纳入版本化 AUTO 安全配置；缩短属于安全放宽并使整个 N_LEG 退回观察。期间再次合格、`UNKNOWN`、负证明缺失/版本不匹配、超过 Oracle 预算且没有可检查证书、陈旧行情、服务中断或版本变化都会重置计时；它们不能被统计成“无套利”。组件拆分或合并时，只要后继与尚未关闭前驱共享稳定 `relation_id` 或 `market_contract_id`，就继承已执行锁，并分别满足完整关闭窗口后才产生新的执行资格；首版不提供人工 reset。

模拟 Episode 至少保留四项资格版本、首次/末次可见时间、would-submit-ready 持续时间、真实 `ORDER_READY` 持续时间、最佳和最差保证利润、求解与端到端延迟、总未结算资本、价格或 Gate 失效原因以及 would-submit 固定方案；`OBSERVE_ONLY` 只能累计前者。人工或 AUTO 真实订单另行保留真实回执、各腿成交、事前部分成交损失上界、费用、滑点、修复证明、最终保证利润和实际利润。

证明、执行计划、N_LEG 模式变化、资格策略版本、关系批准/拒绝/撤销、规则失效、熔断和人工恢复均需带时间、操作者、代码版本和相关指纹，能够从生产账本重放。模拟可成交不能被统计为真实成交率；只有人工或自动真实订单能形成实际成交证据。

## 迁移与一次切换

迁移采用 expand-and-contract，但生产路由只切换一次：

1. 冻结 Prediction Service 的 API、所有权、健康和失败语义。
2. 在 Legacy 内先把现有 Prediction runtime 收拢到可独立启动的边界，保持行为不变。
3. 启动隔离 shadow service：使用真实行情和完整计算，使用隔离数据目录，硬拒绝全部交易写入，不持有生产所有权锁。
4. 迁移 preview、execution、mode、breaker 等 mutation 到新服务的独立端口并验证契约，但浏览器仍走旧生产路径。
5. 进入明确维护状态，让整个 Prediction 前缀返回 503，禁用旧 owner 自动重启，停止 Legacy Prediction owner 与 worker 并证明 PID 消失、生产锁释放。
6. 新服务非阻塞获取锁，在持锁状态下检查 generation 兼容，打开生产账本并启动对账；ready 后 Gateway 一次性把整个 `/api/prediction-arbitrage/*` 前缀切到新服务，不允许 GET/POST 分裂或双写。
7. 删除 Legacy 的 Prediction 路由和运行所有权，并证明 Dashboard 与 Prediction 可以独立升级和回滚。

Shadow 不复用生产 SQLite，不创建真实订单、不争抢生产锁，也不写生产通知去重或执行账本。服务所有权交接明确允许计划内 downtime，不为零停机引入双 writer、共享可写 SQLite、leader election 或热交接。

#45 完成后到 #60 maintenance 前，生产关系目录只允许经真实旧 release 回读验证的 backward-compatible expand-only 写入：旧 reader 必须能安全忽略新增表、字段或独立记录；不得删除、重命名、改义旧字段，不得提前写入只有 N_LEG reader 才能理解的 Episode、账本或执行状态，也不得推进 `minimum_reader_generation`。任何无法证明旧 reader 可安全忽略的 schema 或 data 写入必须延迟到 #60 maintenance。

#60 进入维护时先保存并校验包含 schema、目录、账本和 generation metadata 的完整快照。maintenance 内第一笔必须由 N_LEG reader 才能理解的写入，必须与 `minimum_reader_generation=N_LEG_v1` 在同一原子事务提交；若 maintenance 没有此类写入，则流量开放后的第一条 N_LEG 业务写入承担同一原子推进。流量尚未开放且没有 post-cutover N_LEG 业务写入时，失败只能在单一 owner 和 503 下完整恢复该快照，不能选择性回退表或 generation 字段；流量开放并完成首条 N_LEG 业务写入后即越过 irreversible boundary，禁止恢复 pre-N_LEG 快照，只能使用兼容 release、forward-fix 或继续维护 503。

#47 是 #60 前的最终 rehearsal/freeze Gate：它在隔离的生产等价完整快照上演练迁移、故障、完整回退和旧 release 拒绝，并冻结代码、迁移程序及 exact release SHA。#60 只是使用该 exact SHA 执行真实运营 cutover；若准备或执行中需要修改代码、迁移逻辑或 artifact，必须中止切换、保持 503 或完整恢复快照，并返回 #47 重新 rehearsal 与 acceptance，不能沿用旧 PASS。

通用计算引擎另有一次明确的 expand-contract 切换：

1. 完成规范问题与小规模精确 Oracle；
2. 用同一语料基准三个开源求解器并只选择一个生产实现；
3. 让旧 YES/NO、LLM Relation 和跨所 YES/NO 通过薄 Adapter 进行 Shadow 差分；
4. 在隔离 Prediction Service 中完成真实行情只读 N>2 链路和确定性 replay no-submit 链路；缺少 Entry 部分成交证明时必须如实得到 `PARTIAL_FILL_PROOF_REQUIRED`，不能把挡单写成执行能力已完成；
5. 使用已完成 rehearsal 与 acceptance 的 exact release，进入维护窗口，停止旧路径接收新提交并对账在途订单、仓位、资金预留、模式、熔断和事故状态；全部未释放旧仓位与预留必须保守迁入 N_LEG `total_unsettled_capital`，未知状态按最大占用计入或阻断切换；
6. 一次把生产计算、证明、资格与执行 owner 切到 N_LEG，并固定初始化为 `OBSERVE_MANUAL`；切换只开放 Observe，所有真实 N_LEG submit 继续硬拒绝；
7. 保留切换前历史身份，删除旧策略专用赔付、利润和仓位计算器；
8. 完成固定 Entry 的部分成交证明、事故 RepairPlan 证明和人工 Canary 后，只有用户可以决定是否开启 N_LEG `AUTO`。

旧 YES/NO 或 LLM 的 AUTO 状态不能继承到 N_LEG。切换是生产 owner 的单一边界，不允许某些关系继续使用旧计算器而另一些已使用新证明，却没有明确 Shadow 身份和审计。

#60 原子推进到新 contract generation 后，旧 YES_NO、LLM_RELATION 或跨所独立 mode mutation 统一返回 HTTP 410 与稳定错误码 `legacy_strategy_removed`；不能自动映射成修改整个 N_LEG 模式。旧历史记录仍保留原 `strategy_type` 并只读展示。

## 故障语义

- 未完成启动对账或未持有生产所有权：state 与 mutation 返回 503。
- 单一行情源降级：总体 state 可为 200，但该来源不能产生可下单机会。
- 关系、终态、费用、资产折价或行情不确定：证明为 `UNKNOWN`。
- 固定候选的主问题、最坏状态对手问题或 verifier 超时/不明：该候选为 `UNKNOWN`；可选收益优化 gap 未闭合不使已有 `QUALIFIED_VERIFIED` 失效。
- 资格约束整体被证明不可行：`NO_QUALIFIED_OPPORTUNITY`；不能用单个候选失败、超时或 `UNKNOWN` 推出该状态。
- 证明快照过期：机会失效，重新求解，不沿用旧证明。
- 固定 Entry 的部分成交证明为 `PARTIAL_FILL_UNSAFE`、`UNKNOWN` 或缺失：保留市场机会，但 `order_ready=false` 并禁止真实 submit。
- N_LEG 模式为观察：只有 exact scope capability 至少为 `MANUAL_CANARY` 时才允许用户确认后走完整真实 preflight；`OBSERVE_ONLY` 返回 `order_ready=false`、`reason=SCOPE_OBSERVE_ONLY` 并硬拒绝全部真实 submit。
- 全局熔断：禁止人工与自动真实 submit 及自动修复，保持只读监控和审计。
- 未知订单回执、部分成交或状态不一致：持久化事故、停止全部新 Entry 并将模式降为观察；Repair proof 不安全、不明、预留不足或无方案时禁止自动修复并升级熔断/人工处理。

总体原则是宁可错过机会，也不使用陈旧证明、含糊关系或未知执行状态下单。

## 验证策略

每个实现 Ticket 都必须同时包含成功与失败验收；交易路径不能只验证 happy path。整体证据至少包括：

- 规范优化模型单元测试，以及小规模方向、整数数量和终态的完整穷举差分测试；
- 三个求解器的同语料正确性、状态、数值和性能报告；
- 关系审批、规则指纹变化、已批准但模型不完整、重复发现证据和撤销测试；
- 单一 N_LEG 模式、安全范围或配置放宽自动降级和全局熔断测试；
- 1 美元、1%、15%、30 天边界，最晚资本释放时间和禁止 close_at fallback 测试；
- 约束矛盾、逐合约异常终态、正常关系作用域和跨合约独立异常组合测试；
- 任意合格连通组合可先准入、可选收益优化不阻塞、`QUALIFIED_VERIFIED`/`OPTIMAL`/`NO_QUALIFIED_OPPORTUNITY`/`NO_ARBITRAGE`/`UNKNOWN` 状态分离；
- 组件级 `NO_QUALIFIED_OPPORTUNITY` 负 proof record 的 generation、关系集合、model/quote/cost 指纹、资格版本、预算内完整 Oracle 或可检查不可行证书，以及陈旧、缺字段或无法独立检查时降级为 `UNKNOWN`；
- 陈旧行情、缺失费用、异常结算、不同资产、余额不足和超时均 fail-closed；
- 固定 Entry 与 RepairPlan 的所有可达部分成交向量、FOK 语义、cap 边界和小规模 Oracle 差分，抽样或超时不能证明安全；
- 全填、部分填、全拒、未知回执、补齐、退出、confirmed inventory、原预留不足、禁止借用全局剩余额度、Repair 原子 preflight、超限熔断和重启对账；
- 全局 single-flight、FIFO、Episode 每 lineage 至多一个真实 batch、连续 5 分钟重新武装及组件拆分/合并继承；
- exact scope capability、enabled scope version、范围扩大降级和旧范围重新 Go 不误授权新范围；
- `OBSERVE_ONLY` 只产生 would-submit、固定 `order_ready=false(reason=SCOPE_OBSERVE_ONLY)`，且页面和 API 不把它渲染为可提交；
- Shadow 与生产数据库、锁、通知和订单写入隔离；
- API 契约 parity、整个前缀原子切换及 Legacy 所有权删除；
- 旧 reader 对 pre-cutover expand-only 目录写入的真实回读、generation fence 原子故障注入、开放前完整快照恢复、开放后旧快照拒绝，以及 #47 冻结 SHA 与 #60 实际 SHA 相等；
- 多标签页持续轮询下线程、FD、SQLite 连接和响应延迟有界，覆盖 #34 的故障形态；
- 聚焦测试、真实 no-submit/人工小单工作流、进程 PID/cwd/SHA/日志和最终 `make acceptance`。

Dashboard 任务只有最终 `make acceptance` 为 `PASS` 后才可交付；随后必须重部署完全相同的已验收 SHA，并验证新 PID、工作目录、SHA、新鲜日志和 `http://127.0.0.1:8766/` HTTP 200。

## Ticket 组织原则

本设计规模超过单个实现上下文，必须拆为多个可独立验证的 Ticket，并明确依赖：

- 服务迁移以现有 #38 为父范围，分成契约、提取、shadow、mutation、切换、Legacy 删除和独立回滚验证。
- 数学引擎与服务迁移在共享契约冻结后可并行推进，先做规范模型和求解器基准，再做通用证明与实时求解。
- 三个旧路径适配器分别完成 Shadow，全部通过后一次切到 N_LEG 并删除旧策略专用赔付计算；Adapter 不是永久产品边界。
- 统一页面只能消费稳定的统一机会契约，不直接等待全部 N 腿发现能力完成。
- 关系审批目录先于运行时关系图，运行时关系图先于同事件 N 腿监控；观察与执行状态机先于任何真实 N 腿订单。
- 一次 N_LEG 切换前必须有隔离真实行情与 replay 的 N>2 no-submit 垂直证据；它可以以部分成交证明缺失而正确挡单，但不能冒充真实执行安全。
- 固定 Entry 的最坏部分成交证明与事故 RepairPlan 的最坏部分修复证明分别交付，后者复用前者框架；两者完成前不得开放真实 N 腿订单。
- 同交易所同事件、同交易所跨事件、跨交易所同 observation，以及同时跨交易所与跨事件分别建 Ticket；任何新范围先 `OBSERVE_ONLY`，不能搭便车继承人工或 AUTO 权限。
- 核心热路径先交付合格验证组合；剩余预算收益优化是可选支线，不阻塞切换、观察、人工或 AUTO。
- N 腿首次真实自动交易必须排在观察、人工确认订单和事故恢复验证之后；最终切换由用户显式批准，不由统计自动触发。

统一资格策略不另建重复 Ticket：成本与仓位 Ticket 负责计算四项事实和逐项判断，统一 API/页面 Ticket 负责资格投影与筛选，模式/熔断 Ticket 负责版本持久化和放宽后的自动降级。版本化批准目录必须成为实时关系图的显式 blocker。

当前关键依赖链固定为：

- Prediction Service 基础：#39 → #40 → #41 → #42 → #43 → #44 → #45 → #46；#44 另受 #34 阻塞。
- 规范数学：#48 → #49 → #50 → #51；#59 依赖 #48/#42，#52 依赖 #51/#59，#53 依赖 #43/#51。
- 旧路径与控制：#54/#55 依赖 #41/#51，#56 另依赖 #27；#58 依赖 #42/#53；#57 汇合 #46、#54–#56、#58、#59。
- 切换 Gate：#71 汇合 #41/#52/#53/#59；#47 是最终 rehearsal/freeze Gate，显式等待 #30、#46、#52、#53、#54–#59 与 #71；#60 只依赖 #47 并使用其冻结的 exact SHA 执行真实 cutover，之后 #61 删除旧计算器与模式。
- 观察垂直切片：#62 汇合 #52/#60/#57/#58；#63 再汇合 #53/#60/#59/#62，首版仅同交易所同事件 Observe-only。
- 真实执行证明：#74 依赖 #52；#75 依赖 #53/#74；#64 必须同时等待 #47/#58/#63/#74/#75，之后 #65 → #66 → #67 才能由用户决定是否开启 AUTO。
- 范围扩展：#68 依赖 #59/#63，交付同所跨事件观察；#69 依赖 #56/#59/#63，交付跨所同 observation；#70 依赖 #64/#69，完成跨所人工 Canary；#73 依赖 #68/#69，组合跨所与跨事件并保持 `OBSERVE_ONLY`。
- 可选收益优化：#72 依赖 #52/#57，但不阻塞 #60、#63、真实执行或 AUTO。

现有 #32 的年化、短结算和深度取向统一收敛为 1 美元最低利润、1% 最低净边际、15% 保证年化和最晚资本释放不超过 30 天；任何与旧路径不同的机会集合必须在 Shadow 差分中解释。现有 #34 的线程/FD 故障必须由独立服务与有界状态读取验收覆盖，而不是另写临时 Dashboard 补丁。
