# Polymarket 关系发现可靠性修复设计

> 日期：2026-07-31
>
> 状态：对话设计与 grill 已确认，等待书面规格复核
>
> 修订依据：[Polymarket 同平台跨合约阈值套利设计](./2026-07-29-polymarket-threshold-hedge-design.md)

## 1. 问题

生产 Dashboard 连续运行后始终显示 `0` 个关系候选、`0` 次 Codex 调用和
`0` 个正收益候选。实时诊断确认这不是前端漏显示，而是关系发现入口拒绝了
官方 Polymarket SDK 返回的真实模型：

- SDK 的 `outcomes` 是具有 `yes` / `no` 属性的对象，现有解析器只支持
  mapping、sequence 或 JSON 字符串；
- SDK 的 `end_date` 是带时区的 `datetime`，现有解析器只接受字符串；
- 同一个实时 BTC 阈值事件直接使用 SDK 模型时发现 `0` 个关系，使用同一
  模型的 JSON 表示时发现 `55` 个关系。

当前实现还有三个独立的可靠性缺口：

- 关系扫描只读取第一屏 100 个事件，与既有全量 keyset paginator 规格不符；
- 发现关系后直接把所有关系 token 放入实时路径；关系数量按阈值市场组合呈
  二次增长，大量无深度、远离盈亏平衡的合约会拖慢订单簿读取和 WebSocket；
- 扫描日志只保存在进程内存中，无法回答过去几天在哪个阶段淘汰了多少候选。

## 2. 目标

修复后必须做到：

1. 官方 SDK 模型与等价 JSON 输入产生相同的关系集合；
2. 每 24 小时完成一次全量关系发现；
3. `new_market` 只定向扫描对应事件；
4. 进程重启直接加载最近成功的关系快照；
5. 每分钟从全量关系库重新筛选可能成交的关系，只对第二层候选维持实时盘口；
6. 每次全量或定向扫描都留下可审计的持久化摘要；
7. Codex 在关系进入第二层时后台前置审核，规则不变时重复进出只读缓存；
8. Dashboard 实时展示两层漏斗、淘汰原因、刷新状态和 WebSocket 健康；
9. 实时池内正收益出现后 10 秒内进入 Dashboard，并记录系统实际观察到的
   机会窗口；
10. 只有已经通过全部下单准入与 no-submit 预检的机会才发送一次飞书，通知
    本身不触发订单；
11. 生产数据目录只有一个 Dashboard Watcher 写入。

## 3. 非目标

本修复不改变：

- `minimum_profit > 0`；
- `$20` 单组合成本上限；
- `$2` 单腿补救权限；
- 10 秒盘口时效；
- Codex 审核与程序复核边界；
- 钱包、地区、余额、allowance 或人工确认规则；
- YES/NO 同合约套利的 Top 20 监控范围；
- 下单、对账、熔断或持有到结算状态机。

本阶段明确保持观察模式，不自动下单，也不按通知次数自动升级。何时接通自动
下单完全由操作者另行决定；届时单独设计和验收执行策略。飞书只发窗口开启
通知，不发窗口关闭通知。

不新增依赖、独立服务、消息队列或通用扫描框架。

## 4. 设计决策

### 4.1 在关系发现边界规范化 SDK 模型

`discover_threshold_relations(events)` 在处理事件前，将具有
`model_dump` 的官方 SDK 模型转换为 `model_dump(by_alias=True,
mode="json")` 的普通 JSON 结构。dict、测试 double 和已规范化 JSON
继续按现有路径处理。

不扩大 `_text` 的通用输入范围，也不在每个字段分别维护 SDK 特例。关系发现
后续逻辑继续只处理普通 JSON、`Decimal` 和冻结 dataclass。

### 4.2 每 24 小时全量发现一次

全量扫描依据“最近一次成功完成时间”调度，不依赖固定时区：

```text
无持久化快照                    → 启动后立即全量扫描
最近成功全量扫描年龄 < 24 小时  → 加载快照，不重复扫描
最近成功全量扫描年龄 >= 24 小时 → 后台执行一次全量扫描
```

SDK 实际每页最多返回 100 个事件，即使请求 `page_size=500`。2026-07-31
实测读取 16,058 个活跃事件并发现关系共耗时 32.5 秒，因此全量关系发现不能
继续位于普通 Top 20 刷新的 30 秒超时内。

全量关系发现作为 `PolymarketMonitor` 内的独立后台任务运行：

- 同一进程最多一个全量扫描；
- keyset paginator 读取到 `next_cursor` 为空；
- 扫描中的临时关系不替换当前快照；
- 全量成功后一次性原子替换内存关系集合并持久化；
- 全量失败时保留上次成功快照和实时盘口订阅；
- 快照超过 24 小时后标记 `stale` 并禁止新下单，不伪装为最新。

普通 Top 20 YES/NO Watcher 不等待全量扫描，继续维持现有心跳、盘口和
readiness 刷新。

### 4.3 新市场定向补扫

收到 `new_market` 后：

1. 读取该 `event_id` 的完整事件；
2. 规范化官方 SDK 模型；
3. 重新发现该事件的全部关系；
4. 用新结果替换快照中同一事件的旧关系；
5. 原子持久化更新后的关系快照；
6. 立即对该事件运行第二层成交筛选，并据结果更新订阅。

定向补扫不改变最近成功全量扫描时间，也不触发全市场遍历。

### 4.4 两层漏斗：关系目录与成交候选

关系定义变化慢，盘口变化快。第一层保存完整关系目录，不直接决定 WebSocket
订阅；第二层每分钟从完整目录重新选择可能成交的关系。

```text
第一层：每日关系发现
扫描事件 → 合格事件 → 阈值市场 → 程序关系候选 → 持久化关系库

第二层：每分钟成交筛选
全部关系 → 盘口可用 → 最小深度/金额合格 → 距盈亏平衡 5% 内
         → Codex 审核 → WebSocket 实时池 → 正收益 → 可下单 → 飞书
```

第二层每分钟批量读取第一层全部关系所需的买入腿订单簿。每条关系必须同时
满足：

1. 两个买入腿都有有效卖盘；
2. 两腿都能成交共同的最小下单数量；
3. tick size 和费用可确定；
4. 该最小数量的含费总成本不超过现有 `$20` 上限；
5. 含费最低收益率 `minimum_profit / minimum_payout >= -5%`。

成交量和 Gamma liquidity 只用于排序、展示和诊断，不作为硬准入门槛，避免
漏掉近期没有成交但已经出现真实挂单深度的关系。每轮从第一层全部关系重新
计算；上一轮被排除的关系下一分钟仍会重新考虑。`new_market` 完成定向关系
发现后立即运行第二层，不等待分钟边界。

同一进程最多运行一个第二层扫描。扫描按固定一分钟节拍启动且不并发；若上一轮
超过一分钟，状态标记 `lagging`，完成后立即执行错过的下一轮。扫描失败时保留
上一轮实时池，不把候选错误清零。

2026-07-31 全量实测 16,058 个活跃事件产生 4,879 条关系、1,989 个买入
token；现有 8 路并发、每批 100 个 token 的订单簿探测耗时 1.29 秒，5% 筛选
后为 341 条关系、374 个 token。首版不增加 Top N 或硬容量上限；现有
250-token 分片订阅继续处理更大集合。若生产指标证明订阅规模仍有问题，再
依据实际容量增加上限。

### 4.5 Codex 在第二层前置并持久缓存

第一层的程序发现负责结构筛选：比较符、阈值、标准化问题/规则模板、数据源和
结束时间。关系首次进入 5% 成交候选池时，立即在后台启动 Codex 语义审核，
不等待价格真正转正。Codex 验证：

- 标的、指标、单位和币种；
- 数据源、观察时间窗、时区和聚合方式；
- 异常、取消和模糊结算条款；
- 程序计算的蕴含方向是否排除了全部反例。

Codex 审核进行中时可以先订阅该关系，避免审核阻塞实时盘口；但在审核通过前
不得通知或下单。明确拒绝后移出 WebSocket 实时池，后续分钟扫描仍展示
`codex_rejected`，但直接命中拒绝缓存。

Codex 使用一个后台 worker，按当前含费收益率从高到低处理；已经转正的关系
排在未转正关系之前，但不取消正在运行的审核。队列、最老等待时间和当前
relation id 写入运行状态。这样不会用数百个并发 Codex 进程拖慢 Dashboard；
首次部署可能有审核积压，Dashboard 必须如实显示 `codex_pending`，不得将其
计为可下单。

复用现有 `llm_cache`，收窄关系审核缓存键。`approved` 与
`llm_rejected` 都是持久终态；同一关系反复进入/退出 5% 池不重新调用 Codex。
缓存键只包含问题、完整规则、condition ID、数据源、结束时间、模型和 prompt
版本；移除可能因价格/成交量变化而更新的通用 `updated_at`。这些语义字段变化
才产生新缓存键并重审。超时、服务不可用和无效输出不是终态，以
`next_retry_at` 限制为每条关系最多每小时重试一次，避免分钟扫描重复调用。

### 4.6 实时池价格计算

对第二层已订阅关系：

- WebSocket 消息到达后立即计算；
- 只重新读取并计算消息涉及的 `relation_ids`，不全量重读实时池；
- WebSocket 不可用时使用每分钟订单簿扫描恢复；
- 按真实可成交深度、最小下单量、tick size 和费用计算最低利润；
- `minimum_profit > 0` 时打开 signal episode；
- 正收益消失时关闭既有 signal episode；
- 盘口时效、readiness、余额和补救安全继续按现有规则实时判断。

10 秒是实时池盘口允许的最大年龄，不是轮询周期。从本机收到第一条使组合转正
的盘口消息，到机会出现在 Dashboard 的目标上限为 10 秒。未进入实时池的关系
最多等待下一分钟扫描及其执行耗时后入池；规格不把这段时间伪装成毫秒级覆盖。

### 4.7 系统观察到的机会窗口

窗口从真实可成交深度计算首次得到 `minimum_profit > 0` 开始，而不是从中间价、
最佳卖价之和或 Codex 通过开始。每个 signal episode 保存：

```text
first_positive_at
last_positive_at
ended_at
observed_duration_ms
initial_profit
peak_profit
final_profit
leg_a_book_at
leg_b_book_at
leg_a_received_at
leg_b_received_at
ended_reason
```

这些字段描述“系统观察到的窗口”，不声称覆盖系统启动前、断流期间或上游没有
推送的真实市场窗口。

同一 episode 更新时，`first_positive_at`、初始利润和 signal `started_at`
保持不变，只更新最后观察时间、当前值与峰值；关闭时一次性写入最终值、时长
和原因。

- `minimum_profit <= 0`：以 `profit_non_positive` 关闭；
- 任一腿盘口超过 10 秒或 WebSocket 断流：立即以 `data_unavailable` 关闭；
- 数据恢复后仍为正收益：开启新的 episode，不延续旧窗口；
- 关系源字段变化：以 `rules_changed` 关闭；
- 关系快照过期：以 `relation_discovery_stale` 关闭。

事件循环每秒检查一次已打开窗口的盘口年龄，确保没有新消息时也能按 10 秒
边界关闭。窗口历史复用现有 `signals.payload`，不新增机会历史表。

### 4.8 正收益关系复核与飞书通知

每日关系快照和前置 Codex 缓存只是发现来源。某个关系首次转正后，在下单准入
前重新读取该事件，并确定性核对：

- event id、两条 condition id 和 outcome token；
- source、结束时间与市场状态；
- 两腿 rules hash 与当前关系方向。

任一字段变化即关闭窗口并标记 `rules_changed`，当前缓存失效且不通知；新关系
在下一轮第二层筛选中按新缓存键重新进入前置审核。

飞书通知复用 Dashboard 已注入的现有飞书 notifier 和
`send_notification_with_results`，不新增通知服务。通知资格必须依次满足：

1. 上述实时关系复核通过；
2. 前置 Codex 缓存状态为 `approved`，现有程序复核通过；
3. 最新账户、余额、allowance、地区、relayer、熔断与并发执行检查通过；
4. 对当前 intent 执行现有 `no-submit` 预检并通过；
5. 发送前重新读取两腿盘口、重建 intent，报价年龄不超过 10 秒且仍为正收益。

实现上在 `PredictionExecutionService` 增加一个只读通知预检入口，复用
`preview`、最终校验和 `no-submit` 逻辑；`PolymarketMonitor` 只在窗口变化时
调用该入口，不复制钱包或执行规则。该入口绝不调用提交订单方法，也不创建
execution。最终验收必须以 fake trading client 断言所有 submit 方法调用数
为 `0`。

每个 episode 最多成功通知一次。通知结果直接写回该 signal payload：

```text
notification_state       pending | sent | failed
notification_attempts
notification_sent_at
notification_error_code
order_ready_at
```

发送失败时，只在同一 episode 仍通过全部下单条件时重试；窗口关闭后停止，
不补发过期机会。每个 episode 最多尝试三次，复用监控循环调度，不增加
独立队列；重试前重新执行上述第 1 至 5 步。飞书成功响应后立即标记 `sent`，
同一窗口价格继续变化不再重复通知；窗口关闭后重新转正是新 episode，可以
再次通知。

通知标题和正文固定为：

```text
【仅观察·未下单】Polymarket 正收益机会｜+$0.38

事件：2026 年美联储会降息多少次？
组合：
• 买入「至少降息 2 次」YES：10.00 份 × $0.53 = $5.30
• 买入「至少降息 3 次」NO：10.00 份 × $0.42 = $4.20

拟下单金额：$9.50
预计费用：$0.12
最大总成本：$9.62
最低兑付：$10.00
保底净利润：+$0.38（+3.95%）

发现时间：2026-07-31 10:46:53.696 +08:00
信号→发送：1.2 秒
盘口年龄：184 毫秒
关系复核：通过
机会状态：观察中

机会编号：pm_01K...
市场链接：https://polymarket.com/event/...
Dashboard：http://127.0.0.1:8766/...
```

“拟下单金额”按当前可成交深度和现有成本上限计算；利润必须是扣除预计费用后
的最低利润。正文不显示钱包地址、token id、Codex 原文或内部 rules。能收到
该通知代表 `order_ready_at` 时刻同一 intent 已满足下单条件；它不承诺用户
读到消息时盘口仍然存在。窗口关闭详情只写 Dashboard，不再发送第二条飞书。

### 4.9 复用 SQLite 持久化

在现有 prediction-arbitrage SQLite 中增加两个窄表。

`relation_state` 只保存一份当前关系快照：

```text
singleton
payload
full_scanned_at
updated_at
```

`payload` 包含重建 `ThresholdRelation` 所需的完整、已规范化字段。写入使用
现有短连接事务；全量扫描成功或定向事件合并后整体替换。

`relation_scan_runs` 每次扫描保存一个小摘要：

```text
scan_id
scope            full | event | activity
event_id         仅 event 时有值
status           completed | failed
started_at
completed_at
payload
```

`full` / `event` 摘要 payload 只保存计数和安全原因码：

```text
events_seen
events_eligible
markets_seen
markets_normalized
threshold_markets
relations_discovered
rejection_counts
error_code
```

关系快照成功发布后立即触发一次 `activity` 扫描。每分钟 activity 摘要保存：

```text
relations_considered
tokens_expected
tokens_probed
relations_with_books
relations_with_minimum_depth
relations_within_5pct
codex_pending
codex_approved
codex_rejected
subscribed_relations
subscribed_tokens
positive_candidates
order_ready
notifications_sent
rejection_counts
duration_ms
next_scan_at
```

部分订单簿不可用不回滚关系快照或清空上一轮实时池；缺失项计入
`rejection_counts.book_unavailable`。activity 摘要保留最近 7 天，每次写入时
用一条 SQL 删除更早的 activity 行；`full` 和 `event` 摘要不受此清理影响。

`relation_state.payload` 必须保存重建关系和订阅所需的公开 outcome token ID
与完整规则。扫描摘要、signals、Dashboard 和飞书不复制或展示 token ID、
完整 rules、Codex prompt/stdout、异常正文或原始 Gamma 响应；钱包地址同样
不写入这些记录。

现有 `signals` 表继续保存正收益 candidate episode，不重复建立机会历史表。

### 4.10 Dashboard 状态

复用现有 `relation_discovery` 区域，增加：

- 第一层漏斗：扫描事件、合格事件、阈值市场、程序关系和唯一 token；
- 第二层漏斗：本轮关系、盘口可用、最小深度/金额合格、5% 池、Codex
  pending/approved/rejected、订阅关系/token、正收益、可下单和飞书已发；
- 各层 `rejection_counts`，包括盘口缺失、深度不足、金额超限、超过 5%、
  Codex 拒绝/不可用、规则变化和准入阻止；
- 最近成功全量扫描时间、年龄、状态和耗时；
- 最近第二层扫描时间、耗时、下次倒计时和 `healthy` / `scanning` /
  `lagging` / `degraded` 状态；
- WebSocket 连接状态、订阅 token 数、最后消息时间与年龄；
- 关系快照状态：`healthy`、`scanning`、`stale` 或 `degraded`；
- 每个正收益窗口的首次/最后观察时间、观察时长、初始/峰值/最终利润、
  结束原因和飞书发送结果。

空状态必须区分：

```text
relations_discovered = 0
    → 本轮未发现可验证关系

relations_discovered > 0 && positive_candidates = 0
    → 展示第二层漏斗，说明关系在哪一步被筛除

positive_candidates > 0 && actionable = 0
    → 展示每个候选的现有 eligibility_reason
```

漏斗使用最近一次已完成扫描的持久化摘要；扫描进行中继续显示上轮数字并标注
`scanning`，不闪成零。扫描日志标题移除“内存”字样。候选卡、预览和确认
交互不变。

### 4.11 单一生产写入者

生产 `data/prediction_arbitrage/prediction_arbitrage.sqlite3` 只允许端口 8766
的正式 Dashboard Watcher 使用。最终部署时停止当前共用生产 data-dir 的
18766 旧预览 Watcher。

本修复不新增跨进程租约或分布式锁。若未来确实需要多个 Dashboard 并行，
预览实例必须使用独立 data-dir。

## 5. 错误处理

- 无快照且首次全量扫描失败：关系区域 `degraded`，关系下单不可用；
- 有未过期快照且后台扫描失败：继续实时观察旧关系，显示扫描失败；
- 快照超过 24 小时：显示候选但禁止下单，原因 `relation_discovery_stale`；
- SQLite 关系快照写入失败：不发布新的内存关系集合，保留旧快照；
- 单个 `new_market` 定向扫描失败：不删除该事件的旧关系；
- 第二层扫描失败：保留上轮订阅和漏斗，状态 `degraded`；实时盘口超过 10 秒
  后仍按 `data_unavailable` 关闭窗口；
- 第二层扫描超过一分钟：不并发启动下一轮，状态 `lagging`；
- Codex 终态缓存命中：不调用模型；瞬时失败一小时内不重复调用；
- 关系实时复核失败：关闭对应窗口，原因 `rules_changed` 或安全原因码；
- 飞书发送失败：记录安全原因码，仅在同一窗口仍可下单时重试，绝不触发订单；
- 未知 SDK 字段形状：计入安全原因码并 fail closed，不抛出原始响应到页面。

## 6. 测试与验收

测试必须先失败再实现，至少覆盖：

1. 使用官方 SDK 模型类或经官方 SDK validation 构造的脱敏事件：
   SDK 模型与其 JSON dump 发现相同关系；
2. SDK Outcomes 对象与带时区 `datetime` end date；
3. 两页以上 paginator 被完整读取；
4. 普通 Top 20 刷新不等待全量关系扫描；
5. 24 小时内重启加载持久化快照且不重复全扫；
6. 超过 24 小时后台重扫，成功后原子替换；
7. 全量失败保留旧快照并按年龄 fail closed；
8. 每分钟从全部持久化关系重新筛选，不只复查上轮实时池；
9. 两腿盘口、最小深度、含费 `$20` 上限和 `-5%` 边界计算；
10. 零成交量但满足盘口条件的关系仍可入池，成交量不成为硬门槛；
11. `new_market` 只替换对应事件关系并立即运行第二层；
12. 第二层失败保留旧实时池和漏斗，超时显示 `lagging` 且不并发；真实全量
    activity 扫描耗时小于 60 秒；
13. 首次进入 5% 池触发 Codex；终态通过/拒绝在重复进出时命中缓存，规则或
    审核版本变化才重审，通用 `updated_at` 变化不重审，瞬时失败受一小时
    限频；单 worker 按收益率排序且不阻塞第二层扫描和 WebSocket；
14. Codex pending 可订阅、明确拒绝不订阅，且首版没有 Top N 截断；
15. WebSocket 更新只刷新受影响关系，并在 10 秒目标内进入或退出正收益；
16. episode 保留首次时间和初始利润，更新峰值，关闭时记录最终利润、毫秒时长
    与结束原因；
17. 断流或任一腿盘口超过 10 秒以 `data_unavailable` 关闭，恢复后开启新
    episode；
18. 实时关系字段变化以 `rules_changed` 关闭且不通知；
19. Codex、程序复核、账户、地区、relayer、熔断或 no-submit 任一不通过时
    不通知；
20. 全部准入通过且最终盘口仍新鲜、正收益时，按确认模板每个 episode 只通知
    一次；
21. 飞书失败只在窗口仍可下单时重试，窗口关闭后不补发；
22. 通知路径不调用任何 submit 方法，不创建 execution；
23. activity 摘要保留 7 天且不清理 full/event；扫描摘要、signal 和通知正文
    不含敏感或原始 payload；
24. Dashboard 两层漏斗、淘汰原因、扫描状态、WebSocket 健康、窗口时长与
    通知状态文案；
25. 现有 YES/NO、人工预览确认、执行事故和历史路径不回归。

完成顺序：

1. 聚焦单元与集成测试；
2. 真实 SDK 事件重放；
3. 真实只读全量 Gamma 扫描；
4. 真实订单簿第二层漏斗、分钟重选耗时、订阅规模和窗口历史检查；
5. 使用明确标为测试的消息验证正式飞书配置和发送结果；真实机会通知只由
   全部准入通过的运行时信号触发；
6. 检查唯一生产 Watcher、PID、cwd、Git SHA 和 fresh logs；
7. 最终运行一次 `make acceptance`；
8. `PASS` 后重新部署完全相同的 SHA；
9. 验证新 PID、cwd、SHA、日志、HTTP 200；
10. 从正式 review URL 捕获 LLM 对冲套利受影响区域截图。

只有 `make acceptance` 为 `PASS` 且部署、进程与截图证据齐全，才可描述为
完成或已部署。

## 7. 完成标准

- 真实 SDK 输入不再产生虚假的关系数 `0`；
- 全量扫描每日一次且不阻塞 Top 20 Watcher；
- 新市场无需等待下一次全量扫描；
- 每分钟从全量关系库重选 5% 成交候选，不以成交量硬过滤；
- 生产全量 activity 扫描在一分钟内完成；
- Codex 在 5% 池前置，规则不变时重复进出不重审；
- 只有第二层候选进入 WebSocket；实时池按受影响 token 更新并在 10 秒内进入
  Dashboard；
- 重启不丢失最近成功关系快照；
- Dashboard 即使最终为零也能展示两层漏斗、淘汰原因和链路健康；
- 每个机会留下可解释的系统观察窗口和结束原因；
- 飞书只代表发送时已经通过全部下单条件，且观察模式没有提交订单；
- 飞书失败不产生过期提醒，同一窗口成功通知不重复；
- 操作者能从持久化漏斗判断候选在哪一阶段消失；
- 现有交易风险边界完全不变；
- 生产数据目录只有一个 Dashboard Watcher；
- 最终 acceptance、部署和 UI 截图门禁全部通过。
