# Polymarket 关系发现可靠性修复设计

> 日期：2026-07-31
>
> 状态：对话设计已确认，等待书面规格审核
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
8. 生产数据目录只有一个 Dashboard Watcher 写入。

## 3. 非目标

本修复不改变：

- `minimum_profit > 0`；
- `$20` 单组合成本上限；
- `$2` 单腿补救权限；
- 10 秒盘口时效；
- Codex 审核与程序复核边界；
- 钱包、地区、余额、allowance 或人工确认规则；
- YES/NO 同合约套利的 Top 20 监控范围；
- 下单、对账、熔断、通知或持有到结算状态机。

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
- 收到盘口变化后重新计算两腿成本、手续费和最低利润；
- WebSocket 不可用时使用现有订单簿刷新路径兜底；
- 只有 `minimum_profit > 0` 才调用 Codex 或读取 Codex 缓存；
- 正收益消失时关闭既有 signal episode；
- 盘口时效、readiness、余额和补救安全继续按现有规则实时判断。

### 4.5 复用 SQLite 持久化

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

### 4.6 Dashboard 状态

复用现有 `relation_discovery` 区域，增加：

- 最近成功全量扫描时间与年龄；
- 当前关系数；
- 当前正收益数；
- 最近持久化扫描摘要；
- 关系快照状态：`healthy`、`scanning`、`stale` 或 `degraded`。

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

### 4.7 单一生产写入者

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
9. 已发现关系在不重新全扫时仍随盘口变化进入或退出正收益；
10. 持久化扫描摘要不含敏感或原始 payload；
11. Dashboard 三种空状态与扫描历史文案；
12. 现有 YES/NO、执行、事故和历史路径不回归。

完成顺序：

1. 聚焦单元与集成测试；
2. 真实 SDK 事件重放；
3. 真实只读全量 Gamma 扫描；
4. 真实盘口变化与历史摘要检查；
5. 检查唯一生产 Watcher、PID、cwd、Git SHA 和 fresh logs；
6. 最终运行一次 `make acceptance`；
7. `PASS` 后重新部署完全相同的 SHA；
8. 验证新 PID、cwd、SHA、日志、HTTP 200；
9. 从正式 review URL 捕获 LLM 对冲套利受影响区域截图。

只有 `make acceptance` 为 `PASS` 且部署、进程与截图证据齐全，才可描述为
完成或已部署。

## 7. 完成标准

- 真实 SDK 输入不再产生虚假的关系数 `0`；
- 全量扫描每日一次且不阻塞 Top 20 Watcher；
- 新市场无需等待下一次全量扫描；
- 已发现关系的价格和正收益判断保持实时；
- 重启不丢失最近成功关系快照；
- 操作者能从持久化漏斗判断候选在哪一阶段消失；
- 现有交易风险边界完全不变；
- 生产数据目录只有一个 Dashboard Watcher；
- 最终 acceptance、部署和 UI 截图门禁全部通过。
