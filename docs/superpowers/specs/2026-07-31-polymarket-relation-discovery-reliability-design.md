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

当前实现还有两个独立的可靠性缺口：

- 关系扫描只读取第一屏 100 个事件，与既有全量 keyset paginator 规格不符；
- 扫描日志只保存在进程内存中，无法回答过去几天在哪个阶段淘汰了多少候选。

## 2. 目标

修复后必须做到：

1. 官方 SDK 模型与等价 JSON 输入产生相同的关系集合；
2. 每 24 小时完成一次全量关系发现；
3. `new_market` 只定向扫描对应事件；
4. 进程重启直接加载最近成功的关系快照；
5. 已发现关系的盘口和正收益判断保持实时；
6. 每次全量或定向扫描都留下可审计的持久化摘要；
7. Dashboard 能区分“未发现关系”“有关系但无正收益”和“正收益被后置条件
   阻止”；
8. 正收益出现后 10 秒内进入 Dashboard，并记录系统实际观察到的机会窗口；
9. 只有已经通过全部下单准入与 no-submit 预检的机会才发送一次飞书，通知
   本身不触发订单；
10. 生产数据目录只有一个 Dashboard Watcher 写入。

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

SDK 实际每页最多返回 100 个事件，即使请求 `page_size=500`。实测 20 秒约
读取 6,400 个事件，因此全量关系发现不能继续位于普通 Top 20 刷新的
30 秒超时内。

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
6. 更新需要订阅的 token 集合。

定向补扫不改变最近成功全量扫描时间，也不触发全市场遍历。

### 4.4 关系每日发现，价格持续计算

关系定义变化慢，盘口变化快。全量扫描频率不决定价格检查频率。

对已发现关系：

- 持续订阅相关 outcome token 的 WebSocket 盘口；
- 收到盘口变化后只重新读取并计算受影响的关系，不全量重读所有关系订单簿；
- WebSocket 不可用时使用现有订单簿刷新路径兜底；
- 按真实可成交深度、最小下单量、tick size 和费用计算两腿成本与最低利润；
- 只有 `minimum_profit > 0` 才打开 signal episode 并调用 Codex 或读取缓存；
- 正收益消失时关闭既有 signal episode；
- 盘口时效、readiness、余额和补救安全继续按现有规则实时判断。

10 秒是盘口允许的最大年龄，不是轮询周期。WebSocket 消息到达后立即计算；
从本机收到第一条使组合转正的盘口消息，到机会出现在 Dashboard 的目标上限
为 10 秒。定时兜底只用于断流和漏消息恢复。

当前实现收到任一关系 token 更新后会重新批量读取全部关系订单簿。全量关系
快照扩大后，这条路径无法满足时效目标，必须改为按消息涉及的
`relation_ids` 定向刷新；全量订单簿读取只用于每日快照首次发布和断流恢复。

### 4.5 系统观察到的机会窗口

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

### 4.6 正收益关系复核与飞书通知

每日关系快照只是发现来源。某个关系首次转正后，在 Codex 和下单准入前重新
读取该事件，并确定性核对：

- event id、两条 condition id 和 outcome token；
- source、结束时间与市场状态；
- 两腿 rules hash 与当前关系方向。

任一字段变化即关闭窗口并标记 `rules_changed`，不调用 Codex、不通知。

飞书通知复用 Dashboard 已注入的现有飞书 notifier 和
`send_notification_with_results`，不新增通知服务。通知资格必须依次满足：

1. 上述实时关系复核通过；
2. Codex 审核和现有程序复核通过；
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

### 4.7 复用 SQLite 持久化

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
scope            full | event
event_id         full 时为空
status           completed | failed
started_at
completed_at
payload
```

摘要 payload 只保存计数和安全原因码：

```text
events_seen
events_eligible
markets_seen
markets_normalized
threshold_markets
relations_discovered
relations_with_books
positive_candidates
rejection_counts
error_code
```

关系快照成功发布后立即执行一次批量订单簿读取，再完成该次扫描摘要。
`positive_candidates` 是这次初始盘口计算的正收益数量，不等待 Codex。
部分订单簿不可用不回滚已经成功的关系快照；摘要仍为 `completed`，
`relations_with_books` 只计成功读取的关系，其余计入
`rejection_counts.book_unavailable`。

不保存钱包地址、token IDs、完整 rules、Codex prompt/stdout、异常正文或原始
Gamma 响应。第一版不增加清理任务；每日全量加少量定向摘要的数据量可以忽略。

现有 `signals` 表继续保存正收益 candidate episode，不重复建立机会历史表。

### 4.8 Dashboard 状态

复用现有 `relation_discovery` 区域，增加：

- 最近成功全量扫描时间与年龄；
- 当前关系数；
- 当前正收益数；
- 最近持久化扫描摘要；
- 关系快照状态：`healthy`、`scanning`、`stale` 或 `degraded`；
- 每个正收益窗口的首次/最后观察时间、观察时长、初始/峰值/最终利润、
  结束原因和飞书发送结果。

空状态必须区分：

```text
relations_discovered = 0
    → 本轮未发现可验证关系

relations_discovered > 0 && positive_candidates = 0
    → 已发现关系，当前盘口无正收益

positive_candidates > 0 && actionable = 0
    → 展示每个候选的现有 eligibility_reason
```

扫描日志标题移除“内存”字样。候选卡、预览和确认交互不变。

### 4.9 单一生产写入者

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
8. `new_market` 只替换对应事件关系并持久化；
9. WebSocket 更新只刷新受影响关系，并在 10 秒目标内进入或退出正收益；
10. episode 保留首次时间和初始利润，更新峰值，关闭时记录最终利润、毫秒时长
    与结束原因；
11. 断流或任一腿盘口超过 10 秒以 `data_unavailable` 关闭，恢复后开启新
    episode；
12. 实时关系字段变化以 `rules_changed` 关闭且不通知；
13. Codex、程序复核、账户、地区、relayer、熔断或 no-submit 任一不通过时
    不通知；
14. 全部准入通过且最终盘口仍新鲜、正收益时，按确认模板每个 episode 只通知
    一次；
15. 飞书失败只在窗口仍可下单时重试，窗口关闭后不补发；
16. 通知路径不调用任何 submit 方法，不创建 execution；
17. 持久化扫描摘要、signal 和通知正文不含敏感或原始 payload；
18. Dashboard 三种空状态、扫描历史、窗口时长与通知状态文案；
19. 现有 YES/NO、人工预览确认、执行事故和历史路径不回归。

完成顺序：

1. 聚焦单元与集成测试；
2. 真实 SDK 事件重放；
3. 真实只读全量 Gamma 扫描；
4. 真实盘口订阅、定向刷新耗时与窗口历史检查；
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
- 已发现关系按受影响 token 实时更新，10 秒内进入 Dashboard；
- 重启不丢失最近成功关系快照；
- 每个机会留下可解释的系统观察窗口和结束原因；
- 飞书只代表发送时已经通过全部下单条件，且观察模式没有提交订单；
- 飞书失败不产生过期提醒，同一窗口成功通知不重复；
- 操作者能从持久化漏斗判断候选在哪一阶段消失；
- 现有交易风险边界完全不变；
- 生产数据目录只有一个 Dashboard Watcher；
- 最终 acceptance、部署和 UI 截图门禁全部通过。
