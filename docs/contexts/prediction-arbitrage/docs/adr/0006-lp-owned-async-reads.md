---
status: accepted
---

# LP 逐单异步读取与实际资源所有权

2026-10-21，LP-SLOW-READ-20261021。本决定替代 [ADR 0005](0005-lp-serial-fact-cycle.md)
的全市场串行网络读取和市场共享小槽位。金融接纳、计划、轮次、保护和发送仍由同一个业务 owner 决定。
预算、数学、权限、BUY/SELL 下单并发 2、ACK 与终态区别及同请求 UNKNOWN 防重保持原合同。

## 生产入口与时间

`LPD2LiveExecution` 从已有 trading owner 借用 GET 签名能力，建立 `LPReadRuntime`；
`PredictionExecutionService.set_lp_auto_wakeup` 将真实完成事件接回现有 Scheduler。
`lp_auto_scheduled_check` 仅推进/收集当前结果，不等待市场 HTTP。没有 secure context 的旧离线注入端口
继续支持原同步 API；真实 context 缺失签名能力直接失败，不通过 constructor/bootstrap 重建认证。
显式手动 preview、entry、augment、cancel 保持原有新鲜身份、财务及权限校验。
manual status 和 dashboard 只发布内存快照；重启后的命名组需显式命令核验身份，GET 不绑定组或恢复旧财务。

每个当前自动或手动 BUY、双池收益候选各有独立 attempt，最多一个实际在途。起点间隔至少 5 秒，
整轮必需读取共用 5 秒期限；book 每轮重读。metadata、fees、event、native 和 sponsored 奖励
只共享已完成事实，30 秒到期刷新，保留原时间且不延长 60 秒可用期限。必要刷新失败即 UNKNOWN。
provider/account 的真实 Retry-After、实际清理及 owner 未 ACK 均可延后下一轮，不能产生空失败。

5 秒期限判断实际 I/O 完成时间，而非业务 owner 收集时间。期限内完整成功的结果迟收仍按成功计数；
发布时另按原始源时间检查 freshness，book 恰好 10 秒仍可用，超过时只降为 UNKNOWN，不补造一次读取失败。
旧身份、水位及真实超时结果仍不能恢复 known 或清除较新的失败。

runtime 状态发布最近实际依赖时间：在途期限、到期后的清理宽限或下一读取起点。
live/execution 将其接入既有 `plan_wait.dependencies`，Scheduler 不在纯读取等待期间持续执行空轮次。
实际完成事件及显式命令仍提前唤醒，native、控制或设置工作保持即时推进；不增加固定 sleep 或改变错误退避。

清理故障/退役资源没有可运行结果或有限下一期限时，明确发布 `plan_wait.kind=events`，不编造未来日期。
Scheduler 此时无定时检查期限，仅等待真实完成、显式命令或停止事件；既有错误退避保持。
若健康 BUY 等仍有有限下一期限，则继续优先使用该期限，不被故障通道的事件等待遮挡。
实际 I/O 退出会唤醒 owner；BUY、exact 通道及端口关闭失败仍保留原锁存故障语义，
不提前释放池或恢复资金权限。

2026-10-23，LP-CANCEL-20261023 / LPCANCEL-FINANCIAL-P2：仅 financial 的延迟取消清理故障，
在全部子读取（含响应 `aclose`）实际结束后解除当前阻塞，复用原客户端与连接池。
发送 cancel、Future 返回或清理宽限到期均不是恢复边界。未收集的 timeout/fault 通知保持原 attempt
和观察身份，只交付一次；原通知未收集前同 scope 不开始新 attempt，迟到旧结果不能成为当前事实。
下一次读取仍服从原起点节奏、provider/account Retry-After 和当前需求；shutdown 或无有效需求不触发恢复读取。
新 attempt 的完整账户事实仍须经过原金融接纳，解除阻塞本身不授予资金或发送权限。

纯内存 status 将当前 `cleanup_faults` 与 financial 历史分开：`financial_cleanup_faults` 为累计次数，
`last_financial_cleanup_fault` 只保留最近 attempt、未结束 source 名称、故障及实际结束的 monotonic 时间。
历史固定大小，不含账户、订单、凭证或响应内容，scope 退役后仍可观察；查询不驱动网络或状态变更。
本修订不重建端口、不增加撤单前查询，不改变 UNKNOWN、不重放、重启或其他通道的合同。

2026-10-27，#475：将 financial 的实际退出恢复边界扩展到 low 通道（sell / position / holding）。
全部来源读取（含响应关闭）真正结束后，解除该 scope 当前的延迟清理故障；原客户端、连接池、
起点间隔（SELL 30 秒、position 60 秒、其他 5 秒）、provider/account Retry-After 保持不变。
原 timeout 或 fault-only 邮箱未收取前，不替换 demand 的观察身份，也不启动同 scope 新 attempt；
通知只收一次，旧 UNKNOWN 不变为完整事实。其他低频 scope 可在真实清理结束后按原规则继续读取。
该解除不清除 BUY、exact 或端口关闭失败的锁存，也不授予财务权限；财务 UNKNOWN 仍禁止新增 BUY，
必须有新鲜且通过原接纳的财务事实、有效候选及其他既有资格，才能按原策略发送新请求。
本修订不处理首次超时根因，不扫描取消点、不重建池、不改变超时、容量或依赖。

新增只读 `cleanup_fault_details={total,omitted,items}`，仅列出当前故障，最多 16 项，
优先保留阻止新增 BUY 的固定通道。每项只含 lane、kind、since、active、blocks_new_buy、reason：
lane 为 buy / financial / exact / low；kind 为固定需求枚举（无法确定时 unknown）；
since 在故障发生时记录为 UTC 时间，无法取得有效时间时为 null，不在查询时补造。
active 表示该故障当前仍存在，BUY/exact 读取实际结束后的锁存仍属当前故障；
blocks_new_buy 表示该故障自身是否触发固定通道门禁，BUY 故障本身不冒充全局阻塞。
reason 区分 delayed_read_cleanup 与 port_close_failure。`new_buy_block_reasons` 分别报告
cleanup_fault / capacity / deferred，不改变 `new_buy_allowed` 的判断。原 financial 历史诊断保持不变。
快照脱离可变状态，不含账户、订单、请求、token、端点或异常正文；GET 不发网络请求、不唤醒 Scheduler。
auto/state 通过现有 diagnostics 投影发布这些字段；monitor 的 pools.read_health 验证并复制有界数据，
当前固定通道清理故障明确说明「读取清理故障，暂停新增 BUY」，恢复后回到原池/候选状态。
缺失或畸形诊断保持 unknown；不覆盖致命错误、未实际运行或未确认账户身份，不改变订单和财务真值。

第 5 秒封存 UNKNOWN 并取消未完请求，第 6 秒仍未实际退出则锁存 cleanup fault，
停止该 scope 重试；仅实际 BUY 请求 exact BUY 保护撤单，收益候选读取身份不是撤单目标。独立监督线程不依赖市场 loop 的合作。
两个 I/O loop 分别承担市场和账户/精确保护读取；局部市场 loop 阻塞时 C/O 仍可前进。
这不构成对 GIL、操作系统停顿、DNS 或真实交易所的硬实时保证。

SELL 常规市场检查为 30 秒；持仓市场估值及风险为 60 秒，同刻先风险后 SELL。
当前挂单、余额、实际数量成本、receipt、成交归属和终态证明继续独立及时处理。
闭市且无盘口仍需原账户、condition/token、closed/accepting-orders 及确切 book 404 证明；
网络错误和无证明的空盘口不能变成零成本、空账户或可交易状态。

## 整请求清理所有权与诊断（2026-10-25，#455）

同一来源任务从取连接、发请求、等响应头、读体持续拥有请求，直到库内部错误清理与响应关闭实际结束。
普通网络等待的第一次原生取消正常交付；清理开始后及取消展开期间的重复取消保留到实际退出后传播。
使用公开 asyncio Task 取消接口与 httpcore trace，不操作库私有关闭函数、连接池状态或锁。
运行时复用现有来源任务，移除每响应的独立 close 子任务；不增加调度器或放宽资源上限。
直接调用 `source.read()` 的普通原生任务通过一个有主的来源任务转交取消并等其退出；运行时不增加此包装任务。
业务提前停止读体时显式关闭迭代器和响应。关闭失败不返回完整事实，取消与失败并发时保留失败原因链。

金融轮次通过 `diagnostic_attempt` 接线，现有 supervisor 只发布匹配轮次的请求清理快照。
`financial_request_cleanup` 保留最多三个来源的当前请求和最多 32 条已结束历史，整体 JSON 不超过 16 KiB；
分页只增加不透明序号，不累积无限历史。每个公开 trace 回调绑定具体请求，旧页、旧轮次回调不能覆盖当前记录。
固定字段只含来源枚举、阶段、状态、不透明序号/轮次和 monotonic 时间；不读取或保存原始 trace info、
账户、订单、URL、headers、payload、异常正文或库对象。内部平衡 cancel/uncancel 不冒充外层取消。
回调只更新有界内存，不增加 await、锁等待或外部 I/O；诊断失败不影响实际取消与清理。
状态 GET 复制已有快照，原 `last_financial_timeout` 仍观察实际网络任务的等待链。

清理开始、协议层 close 完成、单页请求结束、来源任务实际退出和资金事实完整是不同证据。
五秒 UNKNOWN、六秒清理故障、实际退出前不重试、原通知只收一次、旧事实不恢复权限以及 no-replay 保持原合同。
本地头/体错误与取消复现及候选恢复守卫不证明生产首次触发原因，也不代替读取恢复、有效候选和双池 3+3 的生产验收。
本修订不改变客户端、固定单连接 data 池、解析、策略、预算、准入、发送、前端、配置或依赖。

### 公开取消平衡与统计边界（PR #458 复核）

已安装 AnyIO 4.14.2 的 CancelScope 会对内部投递逐次调用公开 `uncancel()`，并可能吞掉自己的取消异常。
当调用者取消与内部平衡交叠时，计数仍非零不等于网络已经收到取消。来源任务在同步平衡边界后合并检查一次，
只在剩余取消仍存在且未开始清理时向普通网络等待交付取消；不在每次 `uncancel()` 时立即重投，
避免 Python 3.12 在后续平衡归零后仍留下已排队的取消。仅库内部取消的对照必须正常完成。
该修订只增加每任务最多一个短暂 loop callback，不增加任务、注册表或调度架构。
实际生产 HTTPX 客户端为 `timeout=None`，不能声称网络会由 HTTPX 超时额外兜底；
五秒 UNKNOWN、六秒故障和真正退出前保留所有权不变。

资源计数合同针对现有生产 runtime 的分离叶来源：financial 三项、BUY 市场四项
（book/metadata/native_rewards/sponsored_rewards）和 exact 单项读取。
独立端口便利调用 `SourceRequest('market')` 自行 gather 的子任务不属于 runtime 注册表，
不计入 `managed_tasks`；任意调用者自建需求/任务及库内部短暂 connect helper 不在该统计证明内。
不为这种假设性 runtime 需求改变调度器或扩大资源上限；独立复合读取的原合同仍保留验证。

客户端工厂未改动，工厂创建的客户端归 LP 所有。端口构造器确实修改所提供 HTTPX 客户端的公开
`event_hooks['request']` 列表，去重追加一个依赖当前来源所有权上下文的 hook；普通非有主读取无上下文时不执行观察。
这不是对任意假客户端协议的兼容承诺，也不改变外部 HTTPX trace 的原行为。
体错误取消测试在头错误修复后首次执行已通过，仍诚实记为 ALREADY_COVERED；没有补造独立修前 RED，
本次不注入生产故障，不把本地覆盖或 trace 阶段当作首次生产归因。

## 接纳与保护

金融完整性和全局 read ticket 保持原准入。市场结果另按账户、process epoch、generation、
exact order、terms 和 attempt 设 watermark；B 先返回不能拒掉在自己期限内实际完成的 A。
市场结果只更新逐单收益和置信度，不重建挂单、余额、持仓或资金权限。
book 超过原有 10 秒 freshness 时降为 UNKNOWN；实际超时或旧身份结果不能恢复 known。

每次真实失败 attempt 只计一次，完整有效成功清零。第 11 次连续失败锁存精确撤单，
后续成功不撤销意图。新理由不要求 queue baseline 或 matched=0，覆盖部分成交的剩余 BUY。
原 queue 10 次、UNKNOWN 600 秒及 scoring 5/15/60 秒规则可先触发；相同 exact 义务共享发送所有权。
权限或身份不足时保留未发送意图；BUY/SELL 下单容量不限制 CANCEL。ACK 不证明最终成交数量，原 UNKNOWN 请求不重发；新的有效挂单来源可以形成新的独立撤单请求。

已确认的连续失败数、触发原因和发送前的 cancel request ID 写入现有 canonical order 所属会话 payload
的 `lp_read_protection[order_id]`，不增加表或 schema。重启只恢复已提交事实，
不重放旧 attempt 或已登记取消，不把中断 attempt 补计失败；新 order ID 从零开始。
持久化失败不伪报成功，缺少 canonical owner 时保持覆盖/发送门禁，等待完整当前金融身份。

## 有界资源与退出

2026-10-27，#482：income 收益候选与 BUY 共用并发市场通道和 64 个真实 scope 上限，按首次等待年龄跨两类统一准入，不再进入串行 low 通道。仍只有两个 I/O loop 和一个期限监督线程，5 秒轮次期限、5 秒起点节奏、缓存原始时间和真实关闭边界不变。端口实际关闭前保留 retiring 容量；income 的延迟清理与 BUY 同样锁存，不能套用 low 的自动恢复。low 仅服务 sell / position / holding，其清理故障仍阻止新增 BUY 发送，但不阻止独立 income 读取；恢复读取不恢复任何财务或发送权限。

默认 BUY 与 income 共用 scope 上限 64，包含两类的 active、retiring、cleanup fault 及未退出旧 generation。
同一 exact BUY 的旧 scope 未退出前不创建替代。满额、存在 deferred 或固定通道 cleanup fault 时停止新增 BUY；
deferred 不是失败，不宣称已覆盖。按等待年龄准入，保留所有未结束实际所有者。

每 BUY 或 income 最多 4 个来源分支，两池 Gamma 1 + CLOB 3。固定 O 两池/3连接、C 一池/2连接、
低频两池/4连接；总池数 ≤2B+5，连接 ≤4B+9，受管 async task ≤8B+32。
低频同刻先 position 后 SELL；C 的 fills 与其他精确读取分别保留服务机会。
completed reference cache 为 LRU，最多 256 条/8 MiB 编码值。解压后 response ≤4 MiB，
每 branch ≤16 MiB/128 页/20,000 归一化记录；流式检查和有界解压先于 JSON parse。
超过上限返回 incomplete/UNKNOWN，不截断为成功；cache 不接纳过大条目。

pause/resume/shutdown 不释放尚未退出的实际 HTTP、pool 或 native cancel。
close 未排空返回 false，外层 prediction runtime 保留 client、store、claim 和 owner，后续再收集。
状态诊断提供有界的 scope、deferred、fault、cache、资源上限和失败原因；这些观察不授予交易权限。

读取等待必须与 owner 工作分别仲裁：已完成但未收集的控制保存仍可立即运行，
不能仅依据原生线程是否存活；`retry_wait` 的真实 completed+1 秒期限与读取期限取最早值，
到期控制工作优先于仅等待事件。控制两次尝试上限、失败升级和恢复须提交后生效不变。
Scheduler 检查失败保留自身配置的错误重试期限，不被读取冷却或仅等待事件替换；
真实完成和显式命令保持原唤醒语义。到期重试本地 owner 工作不提前发起受 Retry-After 限制的 HTTP。

精确保护撤单的 native 完成在释放实际 lease 后于锁外通知既有 Scheduler。
有界标量代际保留尚未消费的完成通知；owner 的后续保护容量检查只消费进入该检查前
已确认物理线程退出的完成。检查期间或最后容量检查后发生的完成留给下一轮，
不能因 Future 完成或线程刚退出就进入无限事件等待。callback 失败不改原生结果或 lease 清理。
BUY/SELL 下单的 cap2 仍按真实线程/资源生命周期计数；CANCEL 单独保留全部实际句柄，不占下单槽位。通知消费后不因历史 UNKNOWN 或纯等待的 CANCEL 持续空转。

同一有界完成通知还覆盖手动撤单、保护 SELL/cancel_exit 的真实 native 结束，
以及手动入场实际持有的 `_active_sends` reservation 释放。它们复用既有 owner 唤醒和保护检查消费；
不提前释放线程容量，也不缩短整条手动命令和可靠 SQL 的所有权。
同步 owner `_attempt` 仍由同轮保护检查接续，不额外发通知；未获 reservation 不制造完成。
`manual_call` 不统一发送通知，因为 `lp_status` 也使用该 wrapper；纯状态查询不驱动 Scheduler 或读取。

早期 native reservation 释放不等于独占 MANUAL_BUY mutation lease 已解除；可靠 SQL 仍可阻止保护准入。
service 仅在实际持有的独占手动 lease 释放并完成全局/文件锁及 lease 协调锁清理后，
通过窄 live 通知接口再次发布既有有界完成通知，留给后续真实保护检查消费。
该通知与命令结果无关，覆盖拒单 SQL 尾部和没有 native reservation 的实际 preview 查询持有期；
不通知重复 release、未获 lease、非手动 lease 或纯状态查询。早期通知、SQL 所有权、权限及不重放规则不变。

## 验证边界

20 个公开 async/source/state/live 合同覆盖真实 HTTPX loopback 断连、受控取消/退出、独立期限监督、
逐单 literal oracle、保护阈值、cadence、恢复和生产 scheduled-check 接线。
测试用业务时钟和外部事件控制调度，原生独立 watchdog 覆盖 status/close/cleanup 锁等待。
直接消费者按影响清单运行，保留原断言；每次记录原命令、变更身份、真实 exit 和日志。
本决定不是独立 review、CI、发布、合并或部署完成证明。

2026-10-23，LPCANCEL-FLOW-P2：账户事实由两池共享，每个 BUY 的市场读取仍并行进行。
保护意图与某次 request 的历史结果分开：后续读取成功不撤销已触发保护，新合法挂单来源仍为 LIVE 时
可以发送新 request，允许与同 ID 的旧 CANCEL 重叠。重复原始来源不新增发送资格。
沿用 SDK 的实际网络超时，不主动终止 CANCEL，不在进入撤单后追加账户、book、exact 或 fills GET。
正常 exact/终态事实更新每次原请求的审计；旧 receipt 不改写、旧 progress 不覆盖新事实。
启动恢复保护事实和原审计，但不重放旧 request，只有新接纳账户事实可产生新决定。
本规则只改变撤单流程；读取五秒期限、清理所有权、BUY 不确定性、金融接纳和手动/SELL 安全边界保留。
