# Changelog

Every push to `main` must add one dated entry here. Keep entries short and
operator-facing: what changed, which workflow is affected, and what was verified.

## 2026-09-04

- 新增手动 `trend-curve portfolio-backtest`：按持仓 CSV 与显式排除清单预检 US
  固定整股 sleeves，归一化可用市值权重并输出策略/买入持有对照版本化 JSON；不写结果文件、不接入交易链路。
  专用验证：`make test-trend-curve`。
- 修复离线 `trend-curve backtest` 对合法非交易日曲线观测的对齐：信号在首个后续 OHLC 开盘执行；较新平/非 `{热, 沸}` 观测会在执行前取消排队 BUY，避免过期买入。专用验证：`make test-trend-curve`（26 passed）。
- 新增手动 `trend-curve collect --reconcile-and-notify`：曲线写入 SQLite 后按最新数据日期直接请求五字段 Trend Animals 快照，仅对账三项温度/强度字段并发送 Feishu 一致或异常结果；失败保留已提交曲线行且不接入趋势报告。专用验证：`make test-trend-curve`。

## 2026-09-03

- 扩展手动 `trend-curve collect` 支持从 `portfolio.csv` 读取全部 `ai_eligible=true` 的 CN/HK/US
  持仓并通过本地符号映射构建曲线请求（组合模式由
  `config/trend_curve_portfolio_exclusions.json` 显式排除 `US.AGRZ`），新增失败时
  Feishu-only 通知选项；兼容四段曲线响应。
  聚焦验证：`make test-trend-curve`。

- 新增离线 `trend-curve backtest`：从现有 SQLite 趋势曲线与固定 OHLC CSV 回测 US 单标的严格 `温→热` 入场、`温/热/沸→平` 退出，次日开盘执行、区间末收盘平仓；输出版本化 JSON，不写结果库。验证：`make test-trend-curve`。
- #64 N 腿人工确认真实订单（引擎 + 人工扳机，MANUAL 模式）：人工确认只替代 AUTO 决策、不绕过任何安全闸。① 版本化安全配置新增单笔资金上限 `max_per_trade_cost_units`（默认 0）与预检阈值 `max_quote_age_seconds`/`max_cross_leg_skew_seconds`（默认 10/5）；四个上限必须一次整写（部分写 400），整写后在 config JSON 落 `caps_configured: true` 标记，下单门只看标记（`n_leg_caps_gate`，默认全零拒绝 CAPS_NOT_CONFIGURED）。② 新表 `n_leg_execution_requests`（自增 FIFO 序、request_id/idempotency_key UNIQUE、PENDING/ADMITTED/ABANDONED/SUBMITTED + abandon_reason）与确认端点 `POST /api/prediction-arbitrage/n-leg/orders/confirm`（白名单+生产鉴权+1MiB cap）：POST 时服务端取该组件**当前**方案重验（QUALIFIED_VERIFIED + 证明 SAFE + 费用已知 + 单笔/未清上限），轮转仍合格按当前方案入队并记录 displayed/bound 指纹 + rotated 审计块（无「已轮转 409」硬拒），掉出合格拒绝退回监控；同组件 PENDING→QUEUE_DUPLICATE、总队列≥5→QUEUE_FULL、同幂等键重放返回同一行；/state 新增 `n_leg_orders`（queue/caps/batch）。③ 预检纯函数 `prediction_n_leg_preflight`：用 `cost_slices_from_book` 对冻结数量按新鲜盘口重算（深度/费用含 #117 taker 项/资格四检/单笔上限），行情年龄≤10s、跨腿 exchange_time 偏差≤5s、sequence 存在（Polymarket sequence 为本地合成值，代码与文档如实注明局限）；价格边界制：等于或优于冻结成本 PASS，变差但在已证明成本上界内且资格仍立 PRICE_WITHIN_BOUNDS，超界 PRICE_BEYOND_BOUND。④ 存量迁移（user_version 12，expand-only）：`n_leg_controls.total_unsettled_capital_version`，`n_leg_create_batch` 扩展 `expected_versions` 单事务 CAS（contract/policy/mode/scope/capability/方案与账户指纹/caps 指纹逐一比对，不符抛 N_LEG_ADMISSION_VERSION_STALE）与未清资本上限校验（超抛 N_LEG_ADMISSION_UNSETTLED_CAP），拒绝零副作用（无批次行、无谱系锁、总账与版本不动），写 units 事务同事务 bump 版本。⑤ 队首驱动 `prediction_n_leg_driver`（进程内 1s 单线程）：门全关取队首→live 盘口预检→不过 ABANDONED 带原因退回监控→过则原子准入→线程池并发每腿一次 FOK BUY（`PolymarketTradingClient.submit_n_leg_leg_once`，超时 `max_leg_submit_seconds` 默认 15s，回执经 `apply_receipt` 幂等折叠，尝试记 `n_leg_transitions`）；SELL/非 BUY 动作 UNSUPPORTED_ACTION 不发任何单；任一腿超时/异常/回执未知→记未知回执→立即事故（停单、清空全部 PENDING、降 MANUAL，绝不自动重试）。⑥ 事故闭环：模式契约 incident 门接未确认 N_LEG 批次事故；`POST /n-leg/incidents/acknowledge` 四步原子（回执全终态且一致→账本重算一致→ack 记录→门释放），未知回执拒绝保持事故态；`POST /n-leg/circuit-breaker/reset` 需事故已确认且对账 fresh_clean 才放行；不验证系统外补救、不改模式、不解谱系锁。⑦ UI 四面（按已批准 mock）：order_ready 真时卡片「人工确认下单」按钮、确认弹窗 nleg_order（腿明细/最低赔付/四上限只读展示，`leg_details`/`maximum_payout` 随投影透传）、指标区下队列行、只读事故横幅+解锁弹窗（卡片翻 EXECUTION_INCIDENT_ACTIVE、队列行变事故态）；确认/解锁 POST 新端点、幂等键沿用 predictionIdempotencyKey()、按钮忙碌冻结防连点。**交付后也不自动交易**：四上限默认零 + 唯一 scope 仍 OBSERVE_ONLY，部署后行为不变，直到用户显式配置与提升。AUTO 扳机（#66）、自动修复发送器（#75）、SELL 入口腿均范围外。验证：8 个施工切片全部红→绿（caps 门 A1-A4、确认入队 B1-B6+端点+state、预检 C1-C3 含 #117 手算 3,760,000 units 锁定、准入 D1-D4 含并发恰一成一败版本恰+1、发单 E1-E5、闭环 F1-F3、UI 四面+连点冻结 6 例、e2e 全链+门矩阵 2 例）；聚焦全绿后全量 Docker `make test` 终态见同票评审记录。前轮报告的「已知边界：生产 source 工厂未接通」已由下方同日修复轮 1 落实；其中「投影指纹对真实 resolver 载荷恒不成立」经复验为误报，一并更正。

- #64 修复轮 1（打通生产发单最后一里 + 更正误报）：① resolver 在方案冻结处保留重型解算输入——每次派发把求解请求自身的快照腿重建为 #51 规范盘口（逐动作 token、tick 下限 1、无 maker 费/折扣事实绑 0，经济自洽且保守），新方案冻结即失效旧构建，轮转/负证明/清理与 `_solutions` 同生命周期同步丢弃、锁纪律一致、内存有界；② 新公共访问器 `PredictionLiveResolver.driver_execution_source(component_id)`：按冻结代至多一次构建 `ExecutionSolutionSource`（冻结报价时刻重放，准入重解码确定性忠实）+ 绑定该重型方案六事实的 #74 证明（构建失败按冻结代缓存 None，与 UNKNOWN 证明缓存同款 fail-closed 不重试）；③ 生产 source 工厂改由该冻结材料构建 `ExecutionSolutionSource`，确认入队冻结准入级重型方案载荷与绑定证明（无保留材料时回落原载荷），`N_LEG_SOURCE_UNAVAILABLE` 仅保留为材料缺失（组件轮出/进程重启）的防御性 fail-closed，不再是常态路径；④ 新增真实链路 e2e：真实 resolver（真实目录+CP-SAT 解算+真实 #74 证明+真实 store）产出方案 → confirm 入队 → 队首驱动预检+原子准入 → fake 交易客户端提交 → 回执幂等折叠 → 对账完成批次 RECONCILED，断言队首行 SUBMITTED、无 ABANDONED/SOURCE_UNAVAILABLE、批次方案指纹=冻结重型方案、台账保守留存 16,000,040 units（手算：20 手×2 腿×400,001）；该 e2e 同时锁定「真实 resolver 载荷下投影 order_ready=True 可达」。⑤ 更正误报：前轮「投影 EXECUTION_FINGERPRINT_MISMATCH 对真实 resolver 载荷恒不成立（既有限制）」与双指纹实验矛盾（`fingerprint(market_dataclass)` == `fingerprint(canonical_payload(canonical_payload(market)))`），真实链路 e2e 实证指纹门通过、order_ready=True 可达——该「已知边界」披露删除，confirm 相关注释更正；确认门四项重验与投影指纹门原样保留作纵深防御。

- #64 修复轮 2（评审 7 项全修，各配红→绿用例）：P1 生产对账工厂缺失致首单后队列卡死、提交异常逃逸事故路径、冻结盘口费率口径、队列去重原子化、证明缺失死循环、序号回退检查、超时/对账窗口进配置。

- #64 收费市场口径说明：修复轮 2 起冻结保守上界按 #51 口径计入 taker 费（500bps、ask 0.40、tick 1 时每 lot 400,001+20,001=420,002 units，20 手×2 腿合计 16,800,080 units），高于页面参考数字 $16.24（=16,240,000 units，#117 轻量报价口径 12,000 units/lot）；预检价格边界与预留一律以冻结保守上界为准（≥页面参考），页面数字仅为点开时参考。

- #64 修复轮 3（复审 5 项全修）：对账失败改为逐 tick 重试直至成功（超时旗标保持可见、绝不无凭强行 complete）、非对账态活跃批次超时看门狗（只可见不重驱动）、CHANGELOG 费用合计算术更正（16,800,080）、resolver 冻结 source 跨轮转竞态丢弃陈旧代、rotated 审计旗标改为同代比对（同代 False/跨代 True，重型路径记 payload_family）。

- 新增手动 `trend-curve collect`：使用 MMKV helper 读取临时凭据，将 Trend Animals 曲线以
  `(market, symbol, curve_date)` 单行键写入 SQLite，重复采集幂等、提供方修订只覆盖匹配日期；与 UI、后台、调度和交易链路隔离。聚焦验证：6 passed。

- 修复手动 `trend-curve collect` 对当前 WeChat MMKV 包装记录与 Trend Animals 直接曲线列表的兼容，固定请求近一年按日历史，并支持复制快照的 `--mmkv-path`（需同名 `.crc`）。聚焦验证：9 passed。

- #114 实盘报价方向→token 映射（修「实盘永远查不到书」哑态，为 #64 人工首单扫清报价侧阻塞）：monitor 盘口缓存只认 CLOB token id，而 threshold IMPLIES 动作的 `market_contract_id` 是 condition id（#111 后每合约 BUY_YES/BUY_NO 双动作），live resolver 以 condition id 查书永远 MISS（fail-closed 安全但哑）。修复四件：① token 落库（`relation_catalog.py`）——threshold 发现 payload 市场字典新写 `yes_token_id`/`no_token_id`（取自 discovery 市场对象）；机械 negRisk EXACTLY_ONE 分支按市场对象携带写、NATIVE_COMPLEMENT 分支不写（「合约号即 token」不变式锁定，endpoints 键集零变化）；`_normalise_discovery` 允许可选 token 字段（缺失/null 合法、str 校验、纯透传——legacy 行归一化输出与现状逐键等价），`_converted` endpoints 投影同规则透传；关系 identity（venue:contract_id）与 problem/schema/fingerprint 零改动。② resolver 方向解析（`prediction_live_resolver.py`）——新增 `_leg_token_by_contract`（从同代 generation rows 提取 contract→YES/NO token 对，同合约冲突→剔除该合约映射，照 #112 fee-unknown 模式）与 `resolve_leg_token`（映射命中→按 `action.side` 取该方向 token；映射缺失→回落 `market_contract_id` 本身：机械类正确不回归，legacy IMPLIES 按 condition id 查不到书 → snapshot None fail-closed 不劣化）；`_reconcile` 与 fee 同锁纪律构建保存，构造函数新增可选 `leg_token_map` 注入（rows 优先、注入只补缺）；`_snapshot_for` 按方向 token 查书；reconcile 末尾 resolver 调专用份额接口 `set_n_leg_tokens(解析 token 全集)` 接回订阅——该 N-leg 份额与 cross-venue 引擎在同一共享 monitor 上的 `set_cross_venue_tokens` 份额并列、并集生效、互不驱逐（生产 runtime 中两者共享同一 monitor 实例，`prediction_runtime.py:620/:699`；评审 P1 发现原独占替换实现会互相清订阅/拆配对；monitor 无该接口如 validation 适配器则 hasattr 跳过；集合变化仍触发 REST 快照+重订阅，日常走 stream 增量）。③ 诊断水位（`polymarket_monitor.py` 仅 `snapshot()` 组装点）——`snapshot()["diagnostics"]` 新增 `cross_venue_token_count`（engine+N-leg 份额并集 token 数）与 `n_leg_cross_venue_token_count`（N-leg 份额 token 数）。④ validation 对齐——`run_live` 新增可选 `leg_token_map` 参数，book 预检 token_ids 改方向解析（rows 提取优先、注入补缺、缺失回落合约号）并传合并映射给 resolver；编排器 `build_contract_token_map(events)` 升级为 conditionId→`{"yes_token_id","no_token_id"}`（clobTokenIds 两个方向都取），并把该映射写进副本激活 endpoints（token 真正落库）；`contract_keyed_live_books` 及其用例退役（其「BUY_NO 腿读 YES 书」的错向翻译一并消除），默认 `--book-source` 直接 token-keyed `live_books`，books 注册表改存方向对。运维注意：存量 ACTIVE 行（payload 无 token 字段）维持不报价——回落 condition id 查不到书、snapshot fail-closed None，与修复前行为一致不劣化；实盘恢复需新数据入库（重新 discovery/ingest 携带 token 的关系后自然接管，零迁移零停机）。验证：批准用例逐条红→绿（ingest 4 例：legacy 兼容锁/threshold/negRisk token 落库/NATIVE_COMPLEMENT 不变式；resolver `_snapshot_for` 直接 seam 5 例：双动作方向解析+请求集合恰为手写 {yes,no} 字面量+两腿 token 不同、无映射 fail-closed None、无映射请求 token==合约号、reconcile 后订阅种子（终走 `set_n_leg_tokens(全集)`）、诊断 token 数；run_live 公共入口 2 例：方向解析真 token 到书查询（BUY_NO 腿读 NO token 书）、legacy rows+注入可跑通；另锁冲突剔除 1 例与 orchestrator 新映射形状 1 例）；红线原样绿灯（机械类现状、#111 归一化、#112 fee 全套，全量内含）；全量 Docker `make test` 终态复核 7861 passed、5 skipped、9 deselected（exit 0，P1 修复后终态 = 修复前 7859 + F1/F2 两例；基线 43b74d0b 全绿，5 skip 均为既有 Keychain 1+highspy 3+大小写卷 1）。评审修复（P1 并列份额）：resolver 与 cross-venue 引擎共享同一 monitor 实例，原 `set_cross_venue_tokens` 独占替换+缓存书 prune 会让两者互相清订阅/拆配对（引擎每次发布报价清掉 resolver 种子 → N-leg 快照长时间 fail-closed；resolver 每次 reconcile 替换 → prune 引擎批准对缓存书 → `_process_local_book` 查书为空拆掉 live 配对；resolver 空 generation 也会误清订阅）——修复为 monitor 新增 `set_n_leg_tokens` N-leg 专用份额（替换/prune 只作用本份额，缓存书保两份额并集），`_cross_venue_tokens` 全部消费点改走 `_cross_venue_effective_tokens()` 并集，订阅/websocket/轮询机制零改动；防回归用例 F1（两份额互不驱逐+诊断并集/N-leg 双计数）、F2（订阅与 REST 快照按并集执行），B4/B5 用例道具语义等价翻转（记录/摆数改走 `set_n_leg_tokens`，断言不变/增双计数）。
- #117 Polymarket taker 费用进成本切片(#112 费用闸门的后续):完成之后,收费且费率已知的市场重新参与资格判定,面板上的利润/净边际/年化全部是扣费后数字。每份费用公式(exponent=1、taker-only)= `rate × p × (1−p)`,p 为成交价,50¢ 最高;成本切片按档取该档最高可成交价处的费用上界(p + rate·p·(1−p) 对 p 严格递增,故取档内上界价合法;费率项在 0.5 封顶),精确有理数运算、每档只做一次向上取整,禁止把费率线性化为 `fee_ppm×(1−p)`(会低估)。费用以 bps 存入快照腿(`LegBook.taker_fee_bps`,400=4%),免费市场为 0 且切片产出与此前逐字节一致(红线回归锁定)。费率拿不到的市场(fees_enabled=True 但 fee_rate 缺失/不可解析、同合约多 endpoint 状态或费率不一致、合约无任何 fee 事实)整个组件不解算——无快照、无派发、/state 无行,比 #112 的"出 UNKNOWN 行"更严格;旧 catalog 行(无费率)等轮转自然跳过,零迁移。解算条目的 fee block 在请求构建时冻结(`{status, charging_contracts, unknown_contracts, modeled, taker_fee_rate_bps, taker_fee_units}`),`taker_fee_units` 为选中数量下有费/无费切片成本差(整数运算、与切片同口径),目录轮转后 `solutions()` 只回放冻结值,不再读取时现算,消灭 block 与切片费率不一致的竞态;`FEE_CHARGING_UNMODELED` 常量删除,`FEE_UNKNOWN` 保留为纯防御闸门(缺 block/status 非法/charging 但 modeled 非 True → UNKNOWN、order_ready=False)。AC#5 决策:全腿按 taker 计费(maker rebate 只会降成本,保守方向);部分成交费用随成交价放大且 ≤ 切片上界;不做 maker 策略。UI 零改动,新字段随 /state 自然暴露。src 改动仅 `prediction_market_solution.py`(切片费项+haircut 改按含费计)/`prediction_live_resolver.py`(费率事实聚合、快照腿 bps、fee block 冻结)/`prediction_n_leg_read_model.py`(charging+modeled 放行、常量删除)。既有测试按批准语义翻转并逐处披露:resolver 侧 #112 三个 fail-closed 用例改断言整组件跳过、fee block 形状断言补新字段、`row()` fixture 补免费 fee 事实(无费率事实不再派发)、快照断言改费率感知;读模型侧 charging 无 modeled → reason 改 `FEE_UNKNOWN`、常量用例删除;e2e 侧 fee_a block 断言补新字段、fee_b 翻为"收费按扣费后数字 QUALIFIED_VERIFIED"、fee_c 翻为整组件跳过、A4/模块头/c1/a7 过时的"费用硬编码 0"表述更新、两个 codec fixture(c2/c3 never-qualified)本地钉为 fees_enabled=False(无费率事实不再派发,保持其派发语义);机械 codec fixture 文件(test_mechanical_relations.py)零改动。新增:Seam A 切片费项 4 例、Seam B 费率事实/冻结回归 5 例、Seam C modeled 闸门 3 例、Seam D 互补双腿 100 股全链路 2 例($0.48 → 净利 2,003,200 units QUALIFIED_VERIFIED、$0.49 → 净利 800 units NOT_QUALIFIED,费 1,996,800/1,999,200 units 手算全为精确整数)。范围外:#114 token 映射/执行层、#115 预算、#119 UI、maker 策略、shadow/validation 费用对齐、executable_cost.py 线性 fee_ppm/BookBinding、schema/fingerprint 改动。披露一处 brief 内部矛盾的裁定:Seam A A3 的 0.90 档手算(3,600)与锁定决策 D8 字面公式 `min(protected, PU//2)`(0.5 封顶后为 10,000)冲突;按批准用例与目标公式实现为 `m = protected`(联合上界 p+rate·p·(1−p) 严格递增,即 D8 自身论证;所有 ≤0.50 档两种读法逐字节相同),A3 逐字通过。tests/test_prediction_n_leg_validation.py 的 `relation_payload` fixture 同样钉为已证免费(无费率事实不再派发,live 用例需恢复派发语义)。评审修复:fee block 的 status/contracts 改为与切片同源自快照腿(消灭「快照→派发」窗口内目录轮转导致的 block/切片不一致),并补两向轮转回归用例。rebase 组合披露(#114 先合入 main,同函数动刀):`_snapshot_for` 盘口查找走 #114 方向 token,费率事实仍按 contract id 查(两维度正交;自动合并曾把费率键错接在 token 上,已修正并交由评审复核);#114 的 `token_row` resolver fixture 补钉 `fees_enabled: False`(无费率事实不再派发),新增组合回归 1 例(token 映射+收费费率同场:方向 token 读书、contract id 查费、两腿 bps=400)。
- #119 机会卡片费用明细(#117 的 UI 后续):#117 已把冻结 fee block 随每条解算带进 /state,但卡片上看不到费用——本次把数字摆上机会卡片。读模型 `project_n_leg_solution` 的 market 投影新增 `fee` 键:resolver 冻结 block 原样透传(`dict(fee)` 逐值拷贝,六键 `{status, charging_contracts, unknown_contracts, modeled, taker_fee_rate_bps, taker_fee_units}`),block 缺失或非 Mapping → `None` 绝不造数;fee_state/fee_modeled/资格判定链零改动(#117 闸门语义原样,charging 无 modeled 仍 UNKNOWN+FEE_UNKNOWN)。面板 `predictionUnifiedOpportunityCard` tag 行在 episode pill 后新增一枚费用 pill(与 #106 episode pill 同模式,零布局形状变化):charging+modeled → 「taker 费 <总额> · 费率 <bps/100>%」(总额走 `predictionNLegUnitsMoney`、费率按 `Number(taker_fee_rate_bps)/100` 显示),fee_free → 「免费市场」,block 缺失/unknown → 无 pill。用户可见效果:收费市场卡片直接显示扣费后 taker 费总额与费率,免费市场标「免费市场」,未知费用不出示。测试 seam 为 `project_n_leg_solution` 公共入口(`tests/test_prediction_n_leg_read_model.py`,沿用 #117 用例的 fixture 构法):批准用例 119-A 收费六键 block(500bps/240000 units,取自 #117 e2e 手算场)逐值等值透传、119-B `fee=None` → `market["fee"] is None` 且资格 fee_status 值仍 `fee_unknown`(防御行为不变);119-A 先红(KeyError: 'fee')后绿。全量 Docker `make test` 终态 7878 passed、5 skipped、9 deselected(exit 0,= 改前 7875 + 新增 3 例;5 skip 均为既有 Keychain 1+highspy 3+大小写卷 1)。范围外:#117 费用逻辑本身、卡片操作行「不可下单 · 不可下单」既有措辞重复(另行关注)。
- #120 盘口订阅计数显示(#114 的 UI 后续):#114 已在 `snapshot()["diagnostics"]` 记录两条订阅份额的盘口 token 数,但 /state 与面板都看不到——本次把它们透出成水位。读模型 `prediction_state_payload` 在 safe_snapshot 之后读取 `diagnostics`:仅当其为 Mapping 且两个计数都能 `int()` 解析时,/state 加法式新增 `monitor_subscription` 键(`{cross_venue_token_count, n_leg_cross_venue_token_count}`);diagnostics 缺失/非 Mapping/计数损坏 → 键缺省,绝不造数、绝不抛异常(捕获 KeyError/TypeError/ValueError 回落 None)。面板 readiness 条 venue 卡渲染仅 Polymarket 卡(`!isPredict`)读该键,在 `pm-venue-states` 盘口状态行后、钱包行前插一行 `<small>盘口订阅 <N> · 套利监测 <M></small>`(`predictionNumber` 渲染,缺省回 "0"),键缺省则无行,布局形状零变化;Predict.fun 卡不渲染。用户可见效果:面板直接可见共享 monitor 上 cross-venue 引擎与 N-leg 两条订阅份额各自覆盖的盘口数,「订阅了却查不到书」的哑态一眼可辨。测试 seam 为 `prediction_state_payload` 公共入口(`tests/test_prediction_read_model.py`,沿 `test_state_payload_exposes_monitor_thread_snapshot_additively` 的 `_Monitor` fake 子类惯用法):批准用例 120-A diagnostics 计数 {37,12} 原样透出到 `monitor_subscription`、120-B 无 diagnostics 快照键缺省且无异常;120-A 先红(KeyError: 'monitor_subscription')后绿。全量 Docker `make test` 终态 7878 passed、5 skipped、9 deselected(exit 0)。范围外:`polymarket_monitor.py` 诊断生产端、订阅/份额逻辑本身、额外订阅 UI。

## 2026-09-02

- #111 编译动作身份补方向维度(#110 后续,恢复链式家族剩余 21 条):修复同家族 IMPLIES 对同一合约携带 BUY_YES vs BUY_NO 两个身份、`_merge_one` 按同名不同内容整组 fail-closed 的拦截(动作 id 此前只有 `{venue}:{contract}`,无方向)。改动三件,共用同一归一化函数为唯一权威:① `prediction_n_leg.py` 新增纯函数 `canonicalize_directional_actions` —— IMPLIES 类问题每合约镜像 canonical 双动作(`{venue}:{contract}:BUY_YES`/`:BUY_NO`,其余字段原样,`cost_slices=(1,1,0)` 方向无关),每个终态原子赔付表按 Polymarket 结算语义补双条目(NORMAL_YES:(YES 1,NO 0)、NORMAL_NO:(0,1)、VOID:(0,0)),幂等、动作按 id 排序,合约动作数非 1 非 canonical 对、或 IMPLIES 问题出现非结算 kind 一律 `ValueError` fail-closed;EXACTLY_ONE 类(NATIVE_COMPLEMENT token 级合约/negRisk 全 BUY_YES,本无方向冲突)原样通过,机械编译器零改动;单动作但 id 非 `{venue}:{contract}` 惯例(如受控 intake 自定义 problem)保守放行不镜像,维持既有合并行为。② threshold 编译器 `_threshold_complete_model` 构造后过同一函数,新 ingest 即 canonical(与旧数据升级路径逐字节同构,T2 断言锁定);顶层 `payouts` 摘要字段不动(不进合并 seam)。③ `prediction_monitor_selection._member_problems` 解码后过同一函数 —— 读取路径惰性归一化单点覆盖全部合并调用方(replace 整组预检/activate_many 组件预检/#102 事件门/monitor 后台/rebuild 预检),存储 payload 永不重写、零迁移零停机,执行批次 frozen problem 不经该路径不受影响;生产旧格式 ACTIVE 行与新 ingest 经归一化后合并一致,不再互相拦截。数据披露:#110 fixture 逐字重放(文件零改动)51 条全部激活、generation 31→52,`_conflict_free_implies` 24→45;非方向冲突红线(rules_hash 不同、结算源不同)回归用例原样保持整组拦截,doctor 归因路径更新字面量后全绿。既有测试按批准语义等价翻转并逐处披露:A2 断言 30 过/21 拦→51 过/0 拦、方向冲突类用例(同合约不同 side 整组拦→不再拦)、共享 helper 与 doctor/oracle/solver/legacy_retirement 中旧 id 字面量换新格式、incremental_activation 文件头过时的 one-side-per-contract 不变式注释更新。范围外另开票:#114 实盘 resolver 以合约号充当报价 token(方向维度缺失,阻塞 #64)、#115 实盘解算预算 9 远低于已激活家族规模(32/64 今日已超,双动作后 1024/4096,仅影响 live 资格不影响激活)、#116 同市场被 IMPLIES 与 EXACTLY_ONE 双重建模的合并拦截(前瞻)。验证:新增归一化/编译器一致性/红线/solver 形状用例逐条红→绿(新文件 `tests/test_problem_canonicalization.py` 及各既有套件增补);全量 Docker `make test` 7815 passed、5 skipped、8 deselected(exit 0;5 skip 均为既有 Keychain 1+highspy 3+大小写卷 1,零新增 skip;基线 #107 后 7803)。评审修复(P2):catalog-doctor 的 `_problem_of` 解码未与 seam 同步归一化,旧格式 generation 的非方向冲突会失去归因与 proposed_removal(seam 以 canonical id 报冲突、doctor 按裸 id 找 holders);修复为 `_problem_of` 解码后过同一 `canonicalize_directional_actions`(归因算法零改动),并以 fixture 两条共享合约 `0x0630…3a71` 的逐字旧格式 IMPLIES payload(其一 account_id 拆分制造非方向冲突)红→绿锁定:report 归因 conflict 键为 canonical id、holders 覆盖两侧、恰一条 proposed_removal、剩余集可编译;聚焦 doctor 18 passed、相邻 stale_component_scope+monitor_selection 28 passed。
- #112 Polymarket 费率 fail-closed：N_LEG 链路按市场携带真实 taker 费率事实（gamma 顶层 `feesEnabled`/`feeSchedule.rate`；机械编解码新增解析，发现载荷 markets 与 catalog 行 endpoints 全链透传，v2 版本指纹覆盖费用字段）。live resolver 按代聚合 contract→fee 状态（冲突/缺失→fee_unknown）并随 solution 输出 `fee` 块；读模型新增第 5 项资格检查 `fee_status` 与判定链第 0 位费用否决：费率非零未建模或不可确定 → qualification `UNKNOWN`、`order_ready=false`、reason `FEE_CHARGING_UNMODELED`/`FEE_UNKNOWN`；`fee` 块缺失永久默认 fee_unknown；费率确定为 0 行为逐字节不变；dashboard 仅增两条 reason 文案。只读审计先行（`docs/operations/2026-09-02-issue-112-fee-audit.md`）：当前 ACTIVE 160/160 市场全部收费（finance 4%/weather 5%/crypto 7%），gamma 字段覆盖 100%，CLOB `/fee-rate` 实证不可用。运维注意：存量 catalog 行无费用字段，下一次发现轮转刷新前全部机会显示 `UNKNOWN`（已批准空窗）；费用进成本切片见 #117。验证：全量 Docker `make test` 7825 passed、5 skipped、8 deselected（期间 owner-lock 两测在并行 Docker 负载下 flake，空闲复跑 2 passed）。
- Trend Animals 单条旧快照若属于当前模拟/真实持仓，不再阻塞 CN/HK/US 整份趋势报告；通常该持仓为 `MANUAL_REVIEW / holding_signal_unknown`，但已有待处理的耐久保护线卖出仍保持 `SELL_ALL / protection_line_already_triggered`，旧快照本身不得新建卖出。旧快照字段与既有保护线仅展示，Dashboard 以琥珀色、Feishu/Markdown 明确标注「沿用」/「旧值仅展示，不参与当天决策」；候选行及未来/畸形日期、缺失/重复/意外/错误 tmId 仍严格拦截。验证：focused/invariant Docker `43 passed`、host system-Chrome computed-style `1 passed`、全量 Docker `make test` `7849 passed, 5 skipped, 9 deselected`。
- Trend protection safety tightened: simulated trigger Feishu is suppressed while callback/local/operational alerts remain; only healthy live Futu US real holdings are monitored read-only with isolated state/event ledgers and replay; protection prices render to two decimals with ROUND_HALF_UP. Verification: focused protection/replay/notification tests and scoped trend suite.
- #113 服务实际加载代码路径可见可验:四服务 healthz 及 account worker 发布新增 `code_root`/`worker_code_root`(取自被 import 的 open_trader 包路径,非 cwd);production-smoke 的 `check_health` 断言其位于 EXPECTED_ROOT 之下(收口 2026-09-02 #110 部署事故:plist 漏改 PYTHONPATH/--release-manifest/--static-dir 致服务跑旧代码而冒烟全绿,此为收口)。新增 `ops/release-deployment.md` 五步发布清单,launchd plist 只许经 install 脚本整体重写,禁止手工按下标改。验证:聚焦门 395+94+527 passed;全量 Docker `make test` 7804 passed、5 skipped、8 deselected(exit 0)。
- 发布清单修正为先捕获部署前提交基线、再执行 install，并为全部安装示例显式指定共享运行时 Python 与 production/stack 模式。验证：Markdown/diff inspection + `git diff --check`。
- Candidate 进程测试（仅测试）现将 spawn 就绪与业务 deadline 分离，并使用 5 秒 worker 成功预算；验证：聚焦 Docker 68 passed/36.27s，0.25 CPU worker 文件 66 passed/126.79s、Trend exactly-once 2 passed/78.18s。

## 2026-09-01

- #110 stale-capital 检查从全目录缩回组件级:修复异构截止日目录被单一晚期结算市场整体阻塞(当日生产事故:唯一 ACTIVE 关系 as_of=2027-01-01T05:00Z 使 51 条更早结算候选全部 `ACTIVATION_BLOCKED_INCONSISTENT`,全部命中全集合 stale 聚合谓词)。改动:① oracle `RelationComponent` 新增必填 `as_of` 字段,`build_relation_components` 新增 keyword-only `as_of_by_contract`(组件 as_of=组件内合约映射最大值;不传映射回落整问题 as_of,多组件异构问题必须传映射),入口整问题校验改为逐组件切片 `validate_problem`(`ValueError("invalid problem: …")` 消息风格不变;`prediction_n_leg.py`/`validate_problem` stale 语义零改动);② `relation_generation_problem` 从各成员编译问题构建合约→as_of 映射(共享合约取较晚值,保守方向)传入组件构建,`problem_for_component` 切片改用 `component.as_of`(monitor driver/live_resolver/n_leg_validation 三下游经该函数自动受益);③ 目录 `_global_activation_ok` 删除两条全集合 stale 比较(单位一致与坏日期拦截保留),`_aggregates` 停止维护 max_as_of/min_release(仅存单位聚合);④ catalog-doctor stale 归因改组件级(与 oracle 相同的合约+观测键连通规则,组件 as_of=成员最大值,组件内比较),字段 `merged_as_of` 改名 `component_as_of`,发现携带组件合约清单与成员 identity 清单(stale 明细移入 `stale_identities`),CLI 打印同步。事故 fixture `tests/fixtures/issue_110_incident_payloads.json`(poison+51 条生产 payload,只读导出,逐字未改;测试内按批准规则补 `event_identity_basis`,identity 不变)。数据披露:45 条 IMPLIES 分 4 个合约家族,同家族成员对共享合约携带冲突 action 身份(BUY_YES vs BUY_NO),`_merge_one` 按 fail-closed 设计拦截(此前被 stale 谓词先拦而从未暴露);action 身份归一化另开票。组件内交叉时间线仍拦(A3 构造拓扑实测 `ACTIVATION_BLOCKED_INCONSISTENT`),单位冲突/坏日期/B2 单成员 stale 保护原样。验证:新增 7 条批准验收用例(C1 组件时间线、C2 切片校验、B2 单成员 stale 仍拦、B1 poison+24 条互不冲突 IMPLIES 异构编译、A1 poison 后 IMPLIES 激活、A2 51 条批量重放恰 30 过/21 拦/generation 31、A3 共享合约交叉时间线仍拦)逐条红→绿;既有测试按批准语义等价翻转(不相交时间线 BLOCKED→APPROVED 共 4 例、聚合重算 helper 与并发探针镜像缩为单位聚合、RelationComponent 断言补 as_of、lifecycle/auto_confirm 两类死锁场景改同 identity 漂移构造且 reset_pending/blocked 保护断言原样保留),全部逐条披露;全量 Docker `make test` 7780 passed、5 skipped、8 deselected(exit 0;5 skip 均为既有 Keychain 1 + highspy 3 + 大小写卷探测 1,零新增 skip;含评审修复新增 3 条 doctor 正向用例;首轮一次运行 2 例 shadow 偶发经同树重跑排除)。评审修复:doctor 跨行观测键连通对齐 oracle(观测键指纹分组提升为跨行全局,镜像 merged-state 语义;合约直连与关系/forbidden 连通语义不变),并补正向归因覆盖(共享合约跨行 stale、仅观测键连通跨行 stale、catalog-doctor CLI 打印正向断言)。
- #107 fail-closed 端到端收口(纯测试票,`src/` 零改动):新增 `tests/test_prediction_n_leg_fail_closed_e2e.py` 23 用例,在真 HTTP 服务(`create_prediction_server` → `GET /api/prediction-arbitrage/state`)锁定 N_LEG 全部 fail-closed 安全语义——$1/1%/15%/30 天门槛恰与略低、成本缺失/释放未知→UNKNOWN、释放超窗→NOT_QUALIFIED、播种层与活链路层余额不足 fail-closed、OBSERVE_ONLY 恒 `order_ready=false`/`SCOPE_OBSERVE_ONLY`、行情陈旧(31s)不派发求解、求解超时撤回已呈现行、播种装配层陈旧 monitor→资格总览 UNKNOWN、播种验证状态 UNKNOWN(`SOLVER_OR_VERIFIER_UNKNOWN`)行→行级 qualification UNKNOWN/永不 order-ready、规则变化公开路径(`rules_changed`)代际剪枝撤行并锁定 `SOURCE_CHANGED_REAPPROVAL` 的 /state 呈现——并以同一活链路(真 v2 目录+进程内 CP-SAT 真求解+真 #52 resolver)N=2/3/4/5 参数化证明无腿数硬编码(NegRisk N=4 问题数字 $0.96×125 → 利润 5,000,000 units/边际 0.04;真实 codec 五类终态模型按锁定语义 fail-closed 无合格行)。验证:新文件 23 用例全绿(修复轮 1 增补播种装配 2 例后复验,相邻套件 `legacy_retirement`+`n_leg_read_model` 47 用例全绿),相邻 6 个装配来源测试文件 147 用例全绿(21 用例轮);全量 Docker `7790 passed, 1 failed, 5 skipped, 8 deselected`(唯一失败为基线 Makefile 文本断言 `test_dashboard_acceptance.py::test_production_smoke_binds_checks_to_release_and_runtime_roots`,f49dac6f 为 smoke 行加 `NODE_PATH=` 未同步该断言);该基线断言失配随后由 main 上的 c10a5296(双序容错)修复,本票并行的一行修复为免重复已在 rebase 时丢弃;rebase 到 #110(3e763fa6)之上后重跑:聚焦 23 用例全绿、#110 相邻五套件 199 用例全绿,全量 Docker `make test` `7803 passed, 0 failed, 5 skipped, 8 deselected`,`make candidate-acceptance` 收 `Candidate Acceptance: PASS`(61 场景 passed、LIVE 3 按例 deselected)。
- Terminal-fill reconciliation proofs no longer make broker simulated holdings or statistics unavailable when they coexist with ordinary action events.
- Host Playwright invocations now resolve repository-owned dependencies from isolated worktrees and immutable releases; Host Readiness also loads the target Production Smoke config/spec with `--list` to detect browser failures earlier.
- #106 最小机会 Episode + 统一卡片修正:机会从「每快照一行」升级为生命周期记录 —— 首个合格快照开启 Episode(表 `opportunity_episodes`/`opportunity_episode_proofs`,共享 SQLite,expand-only),关闭须自第一个被接受负证明起连续 5 分钟新鲜 NO_QUALIFIED_OPPORTUNITY(`episode_rearm_gap_seconds` 首个消费者);再次合格/UNKNOWN/绑定不匹配/超预算/陈旧行情/generation·资格版本变化/重启一律清空计时、不计「无套利」;组件下架立即关档(独立死因 COMPONENT_RETIRED);would-submit-ready 转换累计(停机不计);重启 `load_open()` 恢复;统一列表行内显示 episode 徽标(标签行首位)与「保证最低利润」tile 小字。卡片六项修正(用户评审):指标白话标签+门槛小字(读 checks.threshold)、净边际取值键修正(N_LEG 行读 `net_margin`,legacy `net_edge` 兜底,修 #105 遗留恒 `-`)、删无数据源的「极端风险」、order_ready 并入底部行动行、删 410 过渡提示、行构建器补 title(catalog 市场问题文本,身份串兜底)、「!」悬停指标说明(纯 CSS)。OBSERVE_ONLY,无下单路径。验证:新增 `test_prediction_n_leg_episodes.py` 13 用例红→绿;live_resolver 17、read_model 24、legacy_retirement 25、dashboard_web 417 全绿;全量 Docker `7764 passed, 5 skipped, 7 deselected`,当时唯一失败为既有无关时间炸弹 `test_trend_review.py::test_projection_marks_current_month_benchmark_failure_with_prior_snapshot`(硬编码 2026-08,当日 09 月触发;经用户批准随本票一并修复)。修复:trend_review 投影构建器贯通时钟注入(`build_trend_review_projection`/`_read_current_long_term_benchmark_failure` 加 keyword-only `now`,默认真实时钟、生产行为不变;月份统一转市场时区推导),两个时间炸弹测试改为注入时钟+推导月份,50 处调用逐一排查无其它同款;全量 Docker 升为 7770 passed / 0 failed。
- Dashboard 富途账户嵌入式趋势报告的 Kelly 观察分页现已复用真实账户视图事件入口；375px 全页回归覆盖下一页/上一页、12 个样本行和无横向溢出，独立趋势报告分页回归保持通过。

## 2026-08-31

- #109 修复(#60 遗留):fence≥2 时旧观察/ready 通知通道静默 —— 18:43/18:46 实证切换后仍发【观察提醒】卡(+$0.004/+0.02% 机会,旧扫描层 minimum_profit>0 严格谓词 + 发送侧零经济门槛,未接 N_LEG 资格策略)。修复:`PredictionRuntime.start()` 生产路径的 `set_ready_observer`/`set_observation_observer` 并入既有 `if not legacy_retired` 门(与 auto-eat 同风格,三 observer 一门;monitor 对 None observer 与 auto-eat 同为容忍早退;shadow 接线与 failure 告警不变)。跨所 monitor 自有 ready 接线不在本修复范围(当前因 predict 快照不可用而惰性,归 #105 一并处置)。验证:新增 2 用例(fence=2 三 observer 全不接线/fence=1 全照常)全绿;retirement 20、runtime+service+contract 100 全绿。
- Prediction Service release-generation tests now use a version-agnostic contract derived from each manifest; explicit compatibility tests remain, with no runtime or release behavior changed.
- Trend Report now shows the selected simulated Kelly closed-round sample with compact read-only metrics and fixed 10-row pagination; unavailable evidence fails closed. Verification: focused Docker `17 passed`, host 375px `1 passed`, full Docker `7734 passed, 5 skipped, 7 deselected`, browser Python `6 passed`, Playwright `BLOCKED` because cached Chromium 1228 is missing, and Candidate Acceptance `PASS`.
- #60 切换 Phase A(切片 1/4,store 层):新增 `prediction_n_leg_cutover.py` —— `run_n_leg_cutover_migration(store, manifest)` 在**单事务**内把全部未结算旧账迁入 N_LEG 账本(非终态执行按 payload `total_max_cost` 或清单价、USD→微美元整数 ROUND_UP 保守计价;`reserved` 跨所预留按预留额计一次防双计;未解释/不可计价状态 fail-closed 拒绝 `N_LEG_CUTOVER_BLOCKED_*`,全事务回滚),同事务初始化 `n_leg_controls` 单例(MANUAL、`contract_generation=2`、不读 `validation_mode`/`cross_auto_state`,旧 AUTO 权限不继承)、补种 SAME_EVENT_SAME_VENUE OBSERVE_ONLY scope、把 `minimum_reader_generation` 推进到 `N_LEG_READER_GENERATION=2` 并落 control_events 审计;幂等重跑返回 already_migrated 不双计。`activate_approved_relations(store)` 经 v2 目录 API 把全部 model-complete APPROVED 作为一条原子 generation 激活,published+blocked 与 considered 对账不平即拒绝。store 新增 `advance_minimum_reader_generation`(只升不降、幂等、可组合进调用方事务)。验证:新增 12 用例(13_340_000 计价、未知态阻断、事务中途故障全回滚、无 AUTO 继承、幂等、fence 只升、目录对账、清单失配,及修复轮补充:fence≥2 无迁移阻断(INCONSISTENT_STATE)、tampered contract_generation 阻断、幂等重跑回报真实迁移值(13_340_000/generation 2)、激活审批 git_sha 溯源非空)全绿;mode 17、store 120、catalog/validation 67 既有套件全绿。切换后旧提交入口 410、运行时双态、编排器与 smoke 替换见后续切片。
- #60 切换 Phase A(切片 2/4,运行时/服务双态):同一代码在 fence=1 下行为与今日逐字节一致;fence≥2(`minimum_reader_generation>=2`,启动持锁后单次读取,`legacy_retired`)时——① `/preview`、`/executions`、`/mode`、`/circuit-breaker/reset`、`/cross-auto/pause` 五个旧 POST 统一 410 + `error_code=legacy_strategy_removed`(常量 `LEGACY_STRATEGY_REMOVED`),GET 含 `/history`(原 `strategy_type`)与 `/n-leg/*`、`/llm-provider`、`/predict-allowance/cleanup` 不变;② 旧 auto-eat observer 不接线、cross-auto 有效模式强制 `observe_only`(配置/armed 不继承);③ state 的 opportunities 改为 N_LEG 投影行(`engine_owner="N_LEG"`,零 ACTIVE 关系时空列表);④ dashboard 在 `contract_generation>=2` 时隐藏旧控制(模式按钮/跨所状态/参与按钮);⑤ 旧 release manifest(`reader_generation=1`)在 fence=2 下启动即 `PredictionRuntimeCompatibilityError`。验证:新增 18 用例全绿;冻结契约 6、读模型 43、runtime/service/execution 380、launchd/release/gateway/health 107、面板 JS 400、邻接(store/cutover/resolver/driver)174 全绿。旧端点 410 于 fence=1 完全不影响现网(冻结契约锁定)终审修复:fence=2 下五个旧端点的 410 判定移到生产鉴权之前(未鉴权也 410;fence=1 未鉴权仍 403,冻结次序不变)。
- `#60 切换 Phase A(切片 3/4,编排器)`:新增 `scripts/run_nleg_cutover.py` —— 计划内停机窗口的 fail-closed 编排器,九个子命令:`precheck`(只读对账:非终态执行计价预览、清单完整性、validation_mode/cross_auto 状态记录、目录 APPROVED/model-complete 计数、fence 与 n_leg_controls 状态;任何未解释项 exit 2 并逐条列出)、`snapshot`(生产库仅 mode=ro 经在线 backup 拷贝 + md5/sha256 sidecar,校验副本 fence 与源一致)、`maintenance`(原子改写 Gateway `config/prediction-route.json` 的 maintenance/service 模式,拒绝畸形输入,时间戳备份原文件)、`stop-verify`(两 launchd label 可验证缺席 + runtime 记录非 ready + runtime.lock flock 空闲;输出确切 bootout 命令供操作员执行)、`migrate`(默认仅副本;`--production` 需通过停机守卫否则 exit 2 零写入;调用切片 1 的 `run_n_leg_cutover_migration` + `activate_approved_relations(actor, git_sha)` 并落迁移结果 JSON:units、计价计数、published/blocked 与原因、fence 前后、审计 id)、`post-verify`(fence==2、controls 单例一致、ACTIVE 数==published 数;`--url` 时探测 healthz/state/旧 POST 410)、`evidence`(schema `open_trader.prediction_cutover.evidence.n_leg_v1`:exact SHA、快照指纹、迁移指纹、downtime 起止、停机证据、post-verify 结果、irreversible boundary 占位 null)、`restore`(整库恢复,目标 fence≥2 时拒绝除非 `--force` + 精确认可串,禁止部分回退)、`dry-run`(副本全程演练 + 生产侧 md5 sidecar 前后逐字节一致证明)。实现注记:ACTIVE 计数按 `catalog_v2_generations` anchor/delta 只读重放(与 `_scan_generation` 同语义;`activate_many` 的 ACTIVE 真值在 generation 成员,facade `current_generation()` 对监控选路同样以成员身份为准)。验证:新增 27 用例全绿(预检 exit 0/2、快照-恢复往返 md5 一致、生产守卫 held-lock 零写入、路由备份与畸形拒绝、dry-run 全管线、证据字段齐全、修复轮补充:restore --production 接入与 migrate 相同的停机守卫(守卫失败 exit 2 零变更)、恢复改为先完整拷贝+fsync 再删 WAL 兄弟文件(拷贝失败不再伤及目标库));切片 1 套件 12、#71 编排器回归 42 全绿终审修复:生产数据目录判定改锚 runtime-root 布局标记(数据目录父级存在 prediction-service-runtime.json 即视为生产,不再锚定脚本检出路径;从 release 检出运行时不带 --production 也会被拒),拒绝信息指明 --production --runtime-root;live 410 探针经真实服务器验证可过,`legacy_strategy_removed` 常量单一来源导入。
- `#60 切换 Phase A(切片 4/4,契约注记/smoke/A8)`:基线文档 `docs/operations/prediction-contract-baseline-2026-08-10.md` 追加 2026-08-31 注记:#39 三策略黄金契约只约束服务迁移与 #60 前兼容阶段;#60 切换后(reader fence>=2、contract generation 2)冻结的旧 mutation 集退役为 HTTP 410 `legacy_strategy_removed`(五端点,见 `tests/test_prediction_legacy_retirement.py`),Gateway `/api/prediction-arbitrage/*` 路径不变。smoke/host-readiness 探针换代:`host-readiness` 的 `preflight --no-submit` 换为 `nleg-validate --replay`(离线合成 fixture,按报告 JSON 断言 replay PASS + 离线 live 原因 `LIVE_CATALOG_UNAVAILABLE`,严格只读),删 `monitor-once`/`cross-auto status`;`production-smoke` 新增只读 state 断言 `n_leg.contract_generation==2`、`mode=="MANUAL"`、SAME_EVENT_SAME_VENUE capability `OBSERVE_ONLY`、机会行 `engine_owner=="N_LEG"`(空列表允许),并注明该目标断言 #60 切换后世界、是切换 release SHA 的门;钱包/healthz/浏览器/JS 门不变。A8 终态建模验证:新增 `src/open_trader/prediction_n_leg_terminal_check.py` + CLI `prediction-arb nleg-terminal-check --fixture`(走规范模型 `problem_from_payload` 解码 + compile seam 公共评估 `enumerate_allowed_scenarios`,枚举两市场四联合 YES/NO 态,与 LLM 证明的 `excluded_state` 逐样本对拍;任何分歧/不可映射/不可解码即 fail)。冻结生产证据 fixture `tests/fixtures/prediction_n_leg_cutover_a8_samples.json`(192 条真实 threshold/IMPLIES 样本,只读抽取自 llm_cache × catalog_v2 APPROVED,2026-08-31;方向覆盖为 B_IMPLIES_A/excluded A=NO,B=YES——A_IMPLIES_B 历史证明对应的目录关系最新版本均已 REJECTED,反向由 codec 单测兜底)。验证:**全量对拍 192/192 agreed、disagreed 0、PASS(A8 准入证据)**;新增 3 用例(全量通过/篡改单样本检出 exit 2/坏样本隔离 exit 2)+ validation/contract 18 全绿;Makefile 语法与两目标解析 OK。
- Fresh sessions now silently verify repository identity and bind gate/runtime claims to exact-SHA evidence; `git diff --check` passed, and a fresh ephemeral read-only Codex startup probe correctly reported all four gate locations, the five repository identity checks, and the exact-SHA/UNKNOWN rule. Documentation-only, with no runtime or test behavior changed.
- #105 统一机会列表（只读展示层；求解/资格/执行/写路径零改动）：其一，关系审核计数改为「代际纯」——`RelationCatalog.review_counts()` 只按 identity 分类最新版本，且 ACTIVATED 仅在版本属于 `current_generation()` 时计入（成员判定抽为 `_in_generation` 与 `list("approved_active")` 共用，不再重复谓词），落代后的历史 APPROVED+ACTIVE 记录改计 ACTIVATION_BLOCKED；修复后生产展示对齐 #60 切换证据（已激活 60 / 激活阻断 862 / 待批准 56，此前 ACTIVATED 虚增至 866）。relations 列表端点的 `pending_count()` 保持「队列深度」语义（含同 identity 重复待审版本，供去重流程），展示纯计数走 `review_counts()` 的 `pending_count` 键。其二，retired-fence（fence≥2）机会行新增正交分类字段与展示事实：`relation_type`/`discovery_source`/`scope{event,venue}`/`scope_label`/`qualification_policy_version` 来自 `current_generation()` 关系行（端点 contract_ids ⊆ 组件 contracts 才贡献；合并组件按排序 `/` 连接去重值，如 `IMPLIES/NATIVE_COMPLEMENT`→`LLM/VENUE_METADATA`；无目录匹配时字段留空、行照常输出），`episode` 预留槽恒为 `{opportunity_episode_id, episode_lineage_id, status} = null`（#106 填充），would-submit 腿行新增 `venue`（目录端点场所）与 `expires_at`（该合约终态原子 `capital_release_at` 最大值的 ISO 日期，回落关系级 `capital_release`；投影腿行本身无 close/end 时间，注释已注明该约束）——投影行与内嵌 `n_leg_solution.execution.legs` 同步携带，重复 component_id 投影只出单行。其三，Dashboard：筛选栏首位新增「引擎」筛选（全部/N_LEG，按 `engine_owner` 集合交集/子串匹配，与既有腿数/范围筛选可组合；暂不加场所/到期筛选），统一卡片渲染分类 chips（relation_type、discovery_source、腿数徽标、`资格 v1`）与卡头虚线 `Episode —` 占位（新增一条 `.pm-pill.episode` 虚线样式，无新样式表），would-submit 腿行新增 场所 chip 与 到期（ISO 日期、nowrap）列，would-submit 计划补「成交证明」三态值；关系审核 chips 零 JS 改动（直读 payload 计数）。验证：新增批准用例 A1–A4（review_counts 代际纯/逐 identity 去重/终态历史排除/payload 投影与目录计数相等，A1 同 fixture 交叉断言 `list("approved_active")==1` 防漂移）、B1–B4（fence≥2 HTTP /state 全行契约：IMPLIES 组件 `relation_type/discovery_source/scope/scope_label/order_ready/reason/partial_fill_proof/episode/腿 venue+expires_at`、重复投影单行、合并组件排序连接、无目录匹配留空）逐条先红后绿；D1–D3（引擎筛选渲染+筛选组合、卡片分类 chips/Episode 占位/场所到期列、审核 chips 显示 60/862/56 代际纯计数）逐条红绿；聚焦四文件 92+399 全绿；全量 Docker `make test` 7726 passed、5 skipped、6 deselected（exit 0；5 skip 均为既有 Keychain 1 + highspy 3 + 大小写卷探测 1，无新增 skip；压力/浏览器标记照常排除）。

## 2026-08-30

- #108 backend concurrency tests now await their real execution-worker and HTTP request cleanup boundaries. Focused checks passed, each case repeated 20/20 in Docker, and make test passed (7631 passed, 5 skipped, 6 deselected); Candidate Acceptance was not run.
- #71 收尾：真实行情 book-source 适配器 + 隔离目录激活编排器（只新增文件，不改 harness 判定语义/BLOCKED 分类/报告 schema，生产服务代码零触碰，生产目录 N≥3 关系的受控 intake 明确不在本工单）。新增 `src/open_trader/prediction_n_leg_validation_books.py` 的 `live_books(token_ids)`：作为 nleg-validate `--book-source` 注入 seam，复用 monitor 同一只读 Polymarket CLOB 通道（默认 `AsyncPublicClient` + `get_order_books`，解析直接复用 monitor 的 `_asks`/`_value`/`_items` 助手），返回与 `PolymarketMonitor.cross_venue_books` 同形状的 `ThresholdOrderBook` 映射；客户端工厂可注入，路径上仅存在读端点与 `close`，任何下单/撤单/merge/redeem/allowance 调用不存在；未知 token 不产生键、不抛异常。新增 `scripts/run_nleg_no_submit_validation.py` 编排器：生产 SQLite 仅以 `mode=ro` 打开并经在线 backup API 拷贝到隔离目录 → 从真实 venue 元数据（`--event`/`--slug` 指定或按 24h 成交量自动扫描）经既有 #103 机械关系 codecs 派生同事件同所 N≥3 穷尽组（EXACTLY_ONE）payload（不手写任何字段）→ 仅在副本内 ingest→approve→激活为 ACTIVE（激活走既有 v2-backed facade approve 链，其内部运行 v2 `_activate_many_locked` 批量激活核心并落 activation 记账；因 `readonly_v2_relations` 读取嵌套 `model` 形状而 facade 转换存顶层编译字段，存储 payload 按两种既有读法同时携带同一份 codec 产出，无编造值）→ 调既有 `prediction nleg-validate` CLI（`--live-catalog` 副本、`--data-dir` 隔离子目录、`--book-source open_trader.prediction_n_leg_validation_books:live_books`、报告落盘）→ 对生产库做运行前后 md5 校验和并写 `*.production-checksum.json` 侧注证明零生产写入；退出码 0/1/2 透传，编排器前置守卫在 `--live-catalog` 指向生产路径时于任何写入发生前拒绝（exit 2，与 harness `NON_ISOLATED_DATA_DIR` 双保险）；`--replica-ready` 仅供测试跳过副本构造直接使用预激活副本。真实运行（真实 NegRisk 事件 + 实时盘口）由主代理作为 preflight 另行执行并另行记录。验证：新测试 7 例逐条先红后绿（适配器 3：形状契约/仅读端点/未知 token 不抛；编排器 3：副本激活后 `readonly_v2_relations` 视角 ACTIVE 且成员≥3 同事件同所、`--live-catalog` 指向生产路径被拒且生产文件字节不变、backup+激活全程原库字节不变；端到端 1：合成副本 + fake books + 既有 in-process solver seam 全链报告 `status=PASS`、live `order_ready=False`、零 mutation、报告含 PID/cwd/数据目录/时间戳、伪生产库前后 md5 相等入侧注）。聚焦 `tests/test_prediction_n_leg_validation.py tests/test_prediction_n_leg_validation_books.py tests/test_run_nleg_no_submit_validation.py` 19 passed（基线 13 条 harness 测试零修改）；相关回归 `tests/test_polymarket_monitor.py tests/test_prediction_service.py` 217 passed。真实运行前两项已核实的事实供主代理 preflight 参考：其一，机械组 codec 每合约编译 5 种终态（N=3 共 125 原始联合态），超过 harness 固定 `VALIDATION_BUDGET` 的 16 联合态上限（探针实测 `ORACLE_STATE_LIMIT_EXCEEDED`），故 codec 派生关系的 live 栏可能诚实返回 FAIL/BLOCKED 而非 PASS；其二，本 macOS 主机 `RLIMIT_AS` 不允许下调（`setrlimit` 抛 `ValueError: current limit exceeds maximum limit`），自有 solver worker 每请求应用 `memory_limit_bytes` 即崩溃（`PROTOCOL_MISMATCH`），live 拥有式 solver 服务器在本机不可用（端到端冒烟因此按既有 harness 测试范式注入 in-process solver seam）。两项均在现有文件权限之外，未做任何修改。同日增补(用户已批准的最小范围扩展):`prediction nleg-validate` harness CLI 新增 `--live-max-joint-states`/`--live-max-quantity-vectors` 两 int 旗标(缺省=保持 `VALIDATION_BUDGET` 原值 16/16;值必须 ≥1,否则 `parser.error` 退出码 2),仅作用于 live 路径——新增组装函数 `live_budget_from_flags` 用旗标值(缺省回落原常量、support rechecks 恒为原常量 2)组装 `OracleBudget` 显式传入 `run_live`;replay 路径预算、`VALIDATION_LIMITS` 时间限、判定语义/BLOCKED 分类/报告 schema 零改动(超预算/超时仍 UNKNOWN→FAIL fail-closed);编排器 `run_nleg_no_submit_validation.py` 新增同名两旗标并仅在显式给出时透传进 harness argv。验证:B1 默认不变锁(monkeypatch 捕获 `main()` 传入 `run_live` 的 budget,断言与 `VALIDATION_BUDGET` 逐字段相等 16/16/2 且 `run_replay` 默认仍原常量;红:CLI 未传 budget 时 `KeyError: 'budget'`→绿)、旗标 ≥1 拒绝(退出码 2;红:暂摘守卫→绿)、编排器 `_parse_args`/`_harness_argv` 透传带/不带两形态(红:`unrecognized arguments` 退出码 2→绿)、B2 端到端红绿对:`derive_n3_group` 真实 codec 派生关系(5 终态/合约,N=3 共 125 联合态)经 `activate_replica_catalog` 激活于副本,同场景不带旗标→live 栏 FAIL `NO_QUALIFIED_SOLUTION`(UNKNOWN 路径,探针实测 `ORACLE_STATE_LIMIT_EXCEEDED` 的 125-vs-16 预算饥饿),带 `--live-max-joint-states 256 --live-max-quantity-vectors 64`→live 栏 PASS(`NO_QUALIFIED_OPPORTUNITY` 负证明路径,`qualified_verified=False`、`negative_proof` 指纹非空、整体 PASS、无 order-ready 决策、零副作用、伪生产库前后字节不变入侧注);红:暂断 harness budget 接线时带旗标 leg `assert 1 == 0` 失败,恢复后绿(单测 1.4s)。B2 已披露偏差:机械 codec 固定产出空 `qualification_constraints`,而负证明路径要求 worker 主搜索返回 candidate=None(仅当资格约束使全部组合不可行),且 EXACTLY_ONE 模型允许 VOID/REFUND/SPLIT 全零赔付场景组合——纯真实 payload 在任意价格下既无 ≥3 腿合格组合也无可达负证明(带旗标实测为确定性 FAIL `N_LESS_THAN_3`「solver selected 1 positive legs」);测试按既有 harness 测试范式(`relation_payload(qualification=True)` 同款 min-profit≥1 约束)向已激活副本 payload 注入该资格约束以到达批准的负证明断言对,注入仅存在于测试,生产代码与判定语义零改动。主代理真实 preflight(纯 codec payload、无注入)应预期:默认预算 live 栏 UNKNOWN→FAIL `NO_QUALIFIED_SOLUTION`,带旗标确定性 FAIL `N_LESS_THAN_3`。聚焦 `tests/test_prediction_n_leg_validation.py tests/test_prediction_n_leg_validation_books.py tests/test_run_nleg_no_submit_validation.py` 24 passed(前序 19 条零修改 + 新增 5 条:B1 默认锁、旗标 ≥1 拒绝、透传两例、B2 红绿对)。同日再增补(增量 c,真实运行实证的两个管道缺口,均为加法式管道、判定语义零改动):真实 NegRisk 运行发现缺口 A——生产目录副本继承 921 APPROVED/46 PENDING 时间线,一次性 approve→activate 被代际一致性门拒绝(`ACTIVATION_BLOCKED_INCONSISTENT`),而空副本激活正常(真实事件 863487 EXACTLY_ONE 3 市场组在空副本激活 ACTIVE 成功实证);缺口 B——harness live 路径用动作 `market_contract_id` 请求盘口,真实机械关系该字段是 conditionId(0x 前缀,实测三动作皆是),而 CLOB `get_order_books` 按 clobTokenId 键控 → `MISSING_BOOKS`。修法:其一,编排器新增 `--fresh-replica` 显式旗标——跳过生产库 .backup,在 `<work-dir>/catalog/...` 直接建空目录数据库再派生+激活(与 `--replica-ready` 互斥,副本已存在即拒;日志如实标注 `fresh replica (no production data)`);生产库前后 md5 侧注照常生成仍证零写入;默认生产副本模式保持原样,激活被治理门拒绝时如实报错退出,不静默切换模式。其二,`prediction_n_leg_validation_books` 新增 conditionId 键制包装:模块级映射注册表(`set_contract_token_map` 注入/`contract_token_map` 只读快照)+ `contract_keyed_live_books(token_ids)`——把请求的 conditionId 翻译成该市场 YES clobTokenId 取书、返回仍以请求的 conditionId 为键,映射缺失的 id 跳过不抛,取书走同一 `live_books` 只读 seam(零写约束原样继承);编排器在 fresh/生产副本两模式下于调用 harness 前从派生 group 的原事件市场元数据经 discovery 自身 `_outcome_tokens` codec 构建 `conditionId→YES clobTokenId` 映射并注入注册表,`--book-source` 缺省时有映射则改用 `contract_keyed_live_books`(映射仅影响取书键翻译,判定语义/报告 schema 零改动),显式传入的 `--book-source` 永远原样透传;`--replica-ready` 不触碰注册表。验证:三切片逐条先红后绿——C1 红为 `ImportError: cannot import name 'contract_keyed_live_books'`,绿后适配器文件 6 passed(注册映射请求 cond-a/cond-b/cond-x → 客户端仅收到 [yes-a, yes-b]、返回键为 cond-a/cond-b 书形状同 V1、cond-x 缺席不抛;空注册表返回空映射且客户端零调用;零写 allowlist 沿用 RecordingFake);C2 红为 `unrecognized arguments: --fresh-replica`,绿后编排器文件含端到端 `--fresh-replica --events-json` 合成场景报告 PASS(副本内 ACTIVE N≥3、导出关系集不含生产 stand-in 残留、伪生产库字节不变、md5 侧注 `zero_production_write=true`)与 fresh+replica-ready 互斥拒绝(exit 2);C3 红为注入断言失败(注册表空、缺省 book-source 未解析),绿后编排器在两种模式下注入注册表 `{condition-0..2: yes-0..2}` 且缺省 `--book-source` 解析为 `contract_keyed_live_books`、显式值原样透传。聚焦三文件 19 passed(books 6 + orchestrator 13,前序 24 条聚焦测试零修改保持绿);harness 判定文件 `prediction_n_leg_validation.py` 本次零改动。同日修复轮(reviewer P2/P3,均在批准范围内;仅改编排器与其测试+本日志):P2——隔离守卫未覆盖 facade 派生写路径:`activate_replica_catalog` 原以副本祖父目录构造 `RelationCatalog`,store 落在 `<parent-parent>/prediction_arbitrage/prediction_arbitrage.sqlite3`;若操作者把 `--live-catalog` 放进生产数据目录(如 `<prod>/prediction_arbitrage/validation.sqlite3`),守卫只比对 `--live-catalog` 自身与默认生产路径,派生路径不受检查,生产库会被 `SqliteCatalogStore` 以读写连接打开(WAL+schema)并在 `facade.approve` 的 `BEGIN IMMEDIATE` 下承受写锁争用(reviewer 已用合成 stand-in 复现),违背编排器「生产库在两种模式下都不以写方式打开」的 docstring 承诺。修法:`activate_replica_catalog` 增校验副本文件名必须为 `prediction_arbitrage.sqlite3`(原仅查父目录名),并以副本自身 data_dir 构造前校验 `default_catalog_path(...)` resolve 后等于 `replica_db.resolve()` 才构造(杜绝任何不一致的祖父目录派生写路径);`_refusal_reason` 增派生写路径(resolve 后)命中生产库的前置拒绝(exit 2,发生在任何 store 构造/backup 之前)。P3——失败路径违背 docstring exit-2 契约:backup/derive/activate 步骤异常原以 traceback+exit 1 逃逸,且 md5 侧注只在 harness 之后写,最需要零写入证据的失败运行(真实运行缺口 A 的 `ACTIVATION_BLOCKED_INCONSISTENT` 即此路径)反而没有侧注。修法:三步骤包进 refusal 路径(stderr 一行 `[nleg-no-submit] refused: <step>: <exc 摘要>`,返回 2);只要 before-md5 已计算,退出前都写侧注,侧注新增 `failure` 字段记录 `<step>: <exc 摘要>`(成功为 null);成功路径行为与既有测试断言不变。验证:新增 4 例先红后绿——P2 两例:「生产 stand-in 在 `<dir>/prediction_arbitrage/prediction_arbitrage.sqlite3` + `--live-catalog <dir>/prediction_arbitrage/validation.sqlite3`」场景(红:`facade.approve` 对生产 stand-in 抛 `ValueError: relation version not found` 而非 exit 2;绿:exit 2+stderr 拒绝原因+生产字节不变+monkeypatch `SqliteCatalogStore.__init__` 记录证明生产路径从未进入任何写 store)、副本文件名非规范名直接 `ValueError`(红:无文件名校验,误派生链在别处炸 `relation version not found`);P3 两例:derive 失败(空 events-json 无 N≥3 组;红:derive `ValueError` traceback 逃逸、无侧注)与 activate 失败(monkeypatch 抛 `RuntimeError`;红:RuntimeError traceback 逃逸、无侧注),绿后两场景均 exit 2+stderr 含 `[nleg-no-submit] refused: <step>:`+侧注存在且 `zero_production_write=true`、before/after md5 相等+生产 stand-in 字节不变。聚焦三文件 35 passed(前序 31 条零修改保持绿+新增 4)。同日修复轮 2(reviewer 三项新发现,均为「侧注必写+exit 2 契约」的遗漏分支;仅改编排器与其测试+本日志;P2 隔离守卫经复核完整、无断言削弱):其一[P2]——fresh-replica 模式下步骤 refusal 时侧注写入崩溃:`--fresh-replica` 在 activate 前无任何 mkdir(backup 步骤被跳过,首个 mkdir 在 `RelationCatalog.__init__`),derive refusal(如 `--events-json []` 或网络失败)后无条件侧注写抛 `FileNotFoundError`→traceback、exit 1、无侧注,而真实 preflight 正是 fresh-replica 模式;修法:侧注写前 `sidecar_path.parent.mkdir(parents=True, exist_ok=True)`。其二[P3]——fresh-replica「副本已存在」refusal 提前 return 2 无侧注(此时 checksum_before 已算);修法:改为 try 内 raise,记录 failure 原因后落穿统一侧注写,不再提前 return(stderr 为 `[nleg-no-submit] refused: backup: refusing: …`,退出码 2 与拒绝事实不变)。其三[P3]——`--replica-ready` 的 `_verify_replica_ready` 在 try/except 之外,RuntimeError 直接 traceback、exit 1、无侧注;同类,harness 调用抛出未被其自身捕获的异常(如 no-submit 违规 RuntimeError)也绕过侧注;修法:verify 步骤与 harness 调用纳入同一 refusal/侧注处理(refusal→stderr+exit 2;harness 异常→stderr+exit 2 且侧注 `failure` 如实记录;harness 正常返回的 0/1/2 照旧透传,不吞成功返回码语义);模块 docstring exit-code 契约同步补注 verify/harness 两步。验证:新增 3 例逐条先红后绿——T1 fresh-replica+`--events-json []`(红:侧注写在 `run_nleg_no_submit_validation.py` 侧注写行抛 `FileNotFoundError` 逃逸 main、无侧注;绿:exit 2+stderr 含 `[nleg-no-submit] refused: derive:`+无 Traceback+侧注存在 `zero_production_write=true` 且 `failure` 含 derive+生产 stand-in 字节不变);T2 fresh-replica+预建副本文件(红:提前 return 2 无侧注;绿:exit 2+stderr 含 refusal+侧注存在且 `failure` 含 refusing/already exists+`md5_before==md5_after`+预存副本文件字节不变);T3 `--replica-ready --live-catalog` 指向仅 PENDING 无 ACTIVE N≥3 的副本(红:`_verify_replica_ready` RuntimeError traceback 逃逸;绿:exit 2+stderr 含 `[nleg-no-submit] refused: verify:` 与 no ACTIVE+侧注存在 `zero_production_write=true`)。聚焦三文件 38 passed(前序 35 条零修改保持绿+新增 3);harness `prediction_n_leg_validation.py` 与 books 模块零改动。同日修复轮 3(reviewer 两项新发现,同属「隔离/证据契约」缺陷类收尾;仅改编排器与其测试+本日志;harness 与 books 模块零改动):其一[P3]——旗标 <1 经转发 SystemExit 逃逸:编排器 `_parse_args` 对 `--live-max-joint-states`/`--live-max-quantity-vectors` 原只 `type=int` 不校验,<1 值透传至 harness,其 `parser.error` 抛出的 `SystemExit(2)` 不被编排器 `except Exception` 捕获而逃逸(实测 `--fresh-replica --live-max-joint-states 0`:激活已完成、work 目录已建、stderr 无编排器拒绝行、无侧注);修法两层——`_parse_args` 内对两旗标 ≥1 校验(与 harness 同契约,拒绝消息含 refusing,`parser.error` 退出 2,发生在 checksum/任何文件系统工作之前),并对 `harness_main` 调用单独捕获 `SystemExit` 折入统一 refusal(stderr `[nleg-no-submit] refused:` 行+侧注 `failure` 字段+exit 2;harness 正常返回的 0/1/2 照旧透传,不吞成功返回码语义),模块 docstring exit-code 契约同步补注。其二[P3]——`--report`(或其侧注路径)指向生产库被覆写:`--replica-ready --report <生产库>` 时 harness 将报告 JSON 原样覆写生产 SQLite(exit 0、侧注事后如实记 md5 变化但事前不阻止);修法:`_refusal_reason` 前置拒绝块扩展,`report_path` 与其 `with_suffix(".production-checksum.json")` 侧注路径 resolve 后命中生产路径集合(与既有隔离守卫同一集合)即拒绝 exit 2、不进入任何写入。验证:新增 2 例逐条先红后绿——T1 `--fresh-replica --events-json 合成 --live-max-joint-states 0`(红:work 目录已被激活流程创建、stderr 仅 harness argparse 错误无 refusing;绿:`SystemExit` code 2+stderr 含 refusing+work 目录不存在+生产 stand-in 字节不变,校验在 checksum 前故无侧注属预期);T2 `--replica-ready --live-catalog 有效副本 --report <生产 stand-in>`(红:harness 报告 PASS 覆写生产库、exit 0、编排器记 checksum CHANGED;绿:exit 2+stderr 含拒绝+生产 stand-in 字节不变+报告/侧注均未写)。聚焦三文件 40 passed(前序 38 条零修改保持绿+新增 2);全量 `make test` 7629 passed、3 skipped、1 deselected(exit 0;基线 7627+新增 2)。同日修复轮 4(reviewer 三项发现,均限编排器;仅改编排器与其测试+本日志;harness `prediction_n_leg_validation.py` 与 books 模块零改动):其一[P2]——生产库 SQLite 兄弟文件不在守卫集合:`_refusal_reason` 的 `production_paths` 原只含两主库路径,而 WAL 模式下 `<库>-wal`/`-shm`/`-journal` 是生产库在线组成部分,`--report <生产库>-wal` 实测守卫放行后报告 JSON 覆写 `-wal`、checkpoint 把垃圾帧刷入主库(生产库毁为 `file is not a database`)而侧记主文件 md5 前后一致——`zero_production_write: true` 证据被伪证;修法:`production_paths` 并入两生产路径的 `{p}-wal`/`{p}-shm`/`{p}-journal` 兄弟文件,`--live-catalog`/`--report`/侧注路径统一按该集合前置拒绝(exit 2,任何写入前)。其二[P3]——`--work-dir` 缺省 `Path(tempfile.mkdtemp(...))` 在 argparse 定义时急切求值,每次解析(含拒绝解析)都创建并泄漏空临时目录,与「校验发生在任何文件系统工作之前」披露不符;修法:`default=None`,旗标 ≥1 校验通过后惰性 `mkdtemp`(拒绝解析不再触碰文件系统),模块 docstring 退出码契约同步补注缺省 work 目录的创建时机。其三[P3]——轮 3 的 `except SystemExit` 第二层防御无测试覆盖;补测 monkeypatch harness main 抛 `SystemExit(2)`。验证:新增 3 例——T1 `-wal` 变体拒绝(WAL stand-in + 有效预激活副本 + `--report <stand-in>-wal`;红:harness 报告 PASS 覆写 stand-in、exit 0;绿:exit 2+stderr 含拒绝+stand-in 字节不变+主库 `integrity_check=ok`+生产 stand-in 字节不变;测试先 `gc.collect()` 强制关闭既有 thread-local 连接再落 stand-in 字节,规避既有「GC 触发连接关闭时 SQLite 删除 `-wal`」行为对断言的干扰)、T2 拒绝解析不泄漏临时目录(`--live-max-joint-states 0` 拒绝后以系统临时区 `nleg-no-submit-*` glob 前后差集断言;红:泄漏空目录 `nleg-no-submit-tjoege60`;绿:差集为空)、T3 SystemExit 折入(绿:分支轮 3 已在故天生绿——exit 2、stderr `[nleg-no-submit] refused: harness:` 行、侧注 `failure` 含 SystemExit 摘要、`zero_production_write=true`、生产 stand-in 字节不变;红证据由变异检查给出:暂把分支改为 `raise` 后 `SystemExit: 2` 逃逸 main、无拒绝行无侧注,恢复字节一致后复绿)。聚焦三文件 43 passed(前序 40 条零修改保持绿+新增 3);全量 `make test` 7632 passed、3 skipped、1 deselected(exit 0;基线 7629+新增 3)。同日修复轮 5(reviewer 两项新发现;仅改编排器与其测试+本日志;harness `prediction_n_leg_validation.py` 与 books 模块零改动):其一[P2]——大小写变体绕过生产守卫:macOS 大小写不敏感卷上,`--report <生产库大写变体名>` 经精确 Path 相等比对放行、实际写命中生产文件(`os.path.normcase` 在 macOS 恒等,单用不可修);修法:新增统一判定 `_write_hits_production`——目标已存在时对守卫集合逐成员 `os.path.samefile`(全平台正确),目标不存在时保守回落「精确相等或 casefold 后相等即拒绝」;`--live-catalog` 直指/派生写路径/`--report`/侧注路径四类比对全部换用该判定,守卫集合(两生产路径主文件 + `-wal`/`-shm`/`-journal` 兄弟文件)不变。其二[P3]——守卫自身异常逃逸契约:`--report .`/`/` 使守卫构造侧注候选的 `with_suffix` 抛 ValueError(该调用在 try 之外)→ traceback exit 1 无侧注;修法:守卫内先按空名拒绝退化报告路径(`.`/`/` 的 `Path.name` 为空,不再对其构造 `with_suffix` 侧注候选),守卫求值整体包入 try/except、任何守卫异常折入统一拒绝(exit 2、stderr refused 行)。验证:新增 3 测试逐条先红后绿——已存在大写变体 `--report` 拒绝且生产 stand-in 字节不变(修复前红:放行)、不存在路径 casefold 变体拒绝、`--report .` exit 2 拒绝无 traceback(修复前红);聚焦三文件 46 passed(基线 43 + 新增 3);全量 `make test` 7635 passed、3 skipped、1 deselected(exit 0)。同日修复轮 6(Docker/Linux 可移植性,新 main #108 将 `make test` Docker 化为 Linux 容器后暴露;仅改编排器测试文件+本日志;生产代码/harness/books 零改动):轮 5 的 T1 `test_report_case_variant_of_existing_production_is_refused` 前置「已存在的大写变体=生产文件同一文件」只在大小写不敏感卷成立,Linux 容器(大小写敏感卷)下该变体是不同且不存在的名字,`variant.exists()`/`samefile` 前置断言必失败(Docker 全量实测 1 failed / 7630 passed;macOS 直跑 46 passed 未暴露)。修法:测试自身探测卷语义——新增 `_volume_case_insensitive(directory)`(tmp 下写 `case-probe-a` 探针文件,再按大写拼写 `CASE-PROBE-A` 检查其是否与之互为别名(`os.path.samefile`,与 T1 前置断言同一原语;两名互为别名仅当卷折叠大小写;任何探测异常 fail-closed 返回 False)+ `_case_variant_skip_reason(directory)`(探测 True→返回 None,T1 照常执行全部变体断言;False→返回明确 skip 理由:该场景在大小写敏感卷物理不存在,跨平台「不存在路径 casefold 变体拒绝」由同文件 T2 覆盖且 Linux 下通过);T1 开头经该判定 `pytest.skip(理由)`,macOS 上判定为 None、T1 全部原断言零削弱照跑。红绿(macOS 上无法直接红,语义仅 Linux 失败,按批准方式锁定分支逻辑):红1 `NameError: _volume_case_insensitive`(分支锁定测试先行);首版 symlink 模拟(同目录 `CASE-PROBE-A`→`case-probe-a`)在真实大小写不敏感卷上物理不可能——折叠大小写的目录查找把探针自身的写入路由进该 symlink(ELOOP),属测试设计缺陷而非实现缺陷,遂按批准的 monkeypatch 探测函数方式重写:以探测函数两种返回值锁定 `_case_variant_skip_reason` 两分支(True→None、False→含 case-sensitive volume 与 T2 测试名的理由),真实探测另与独立「读回真相」交叉验证(写 `read-probe` 经大写拼写 `READ-PROBE` 读回比较,与探测所用 exists+samefile 不同原语,防探测与文件系统静默不一致);红2 `NameError: _case_variant_skip_reason`→绿。验证:同日增补(缺口 D,用户已批准;仅改编排器 `scripts/run_nleg_no_submit_validation.py`、其测试 `tests/test_run_nleg_no_submit_validation.py` 与本日志;生产 `src/` 零改动,`DEFAULT_QUALIFICATION_POLICY` 只 import 不改):策略以规范形态接进验证链——机械 codec 派生关系的 `qualification_constraints` 恒空,使 harness 的 `NO_QUALIFIED_OPPORTUNITY` 负证明路径不可达(无注入场景实测确定性 live FAIL `N_LESS_THAN_3`「solver selected 1 positive legs」,真实运行 live 栏 FAIL `NO_QUALIFIED_SOLUTION` 同根因)。修法(生产形状、零行为新增面):编排器新增纯函数 `qualification_constraints_from_policy`——输入复用 mode contract `_validated_policy` 同款校验(非法键/值 `ValueError`),输出恰四条规范求解器约束(`rule_version` 统一 `"v1"`,精确整数十字相乘的有理数,读模型语义四门):`minimum-profit-usd`=GUARANTEED_PROFIT_UNITS ≥ min_profit_usd×1e6 micro-units(默认 "1.00"→1_000_000/1)、`minimum-net-margin`=NET_MARGIN_PPM ≥ 1% 的 PPM 精确表示 10_000/1(表示法按 `prediction_solver._qualification_formula` 的 PPM 语义选定——该公式把 numerator/denominator 读作 parts per million,(1,100) 在其中等价 1e-8 故不取)、`minimum-annualized-return`=ANNUALIZED_RETURN_PPM ≥ 15% → 150_000/1、`maximum-release-delay`=MAX_CAPITAL_RELEASE_DELAY_SECONDS ≤ days×86400(默认 30→2_592_000/1,LESS_THAN_OR_EQUAL);`activate_replica_catalog` 在 payload 组装处把转换出的约束并入 problem 的顶层与嵌套 model 两份(与既有 `add_min_profit_qualification` 注入的写入形状完全一致;codec 未产出编译 problem 即拒绝,fail-closed);新旗标 `--qualification-policy <json 文件路径>`——缺省=import 的 `DEFAULT_QUALIFICATION_POLICY`(单一事实来源,不复制字面量),文件不可读/malformed JSON/策略字段非法均在 parse 时 `parser.error` 前置拒绝 exit 2(先于 checksum 与缺省 work 目录创建,与既有旗标校验风格一致;`--replica-ready` 自备副本模式不注入——激活仅在派生分支发生);激活后日志记录策略摘要与 constraint_id 列表;编译链 `_merge`/`problem_for_component`/`build_solve_request` 原样透传至 `_qualification_formula`,无任何生产代码改动。测试侧注入退役为生产形状接线:`test_fresh_replica_mode_end_to_end_pass_without_production_data` 删除原 `activate_then_qualify` monkeypatch 包装注入,改为依赖真实编排器接线(`--replica-ready` 自备副本类 B2 测试的注入按批准保留——那是测试自备数据的合法 seeding,不经编排器激活;退役不减少测试数)。验证:D1–D3 逐条先红后绿——D1 默认策略逐字段精确断言、自定义值("2.50"/"0.02"/"0.20"/45→2_500_000、20_000、200_000、3_888_000)逐一换算、非法输入(缺键/多余键/负数/非 decimal/0 天)拒绝,红=函数不存在 AttributeError 7 例;D2 `--fresh-replica`(真实 codec 派生)+缺省策略+三 0.50 无套利假盘口+in-process solver+扩展预算 → 整体 exit 0、live PASS(`NO_QUALIFIED_OPPORTUNITY` 负证明、`qualified_verified=False`、`negative_proof` 指纹、零副作用),`readonly_v2_relations` 视角 problem 恰含四条约束且逐字段精确断言,日志含策略摘要与四 constraint_id,红=exit 1、live FAIL `N_LESS_THAN_3`;D3 `--qualification-policy` 自定义 json → 副本约束随值变化(stub harness 快路径经 readonly 导出断言),非法文件(缺文件/malformed/负值/非对象 JSON)前置拒绝 exit 2 且无 work 目录、无侧注、生产字节不变,红证据=暂禁旗标后 5 例全失败(自定义例 SystemExit、拒绝例无 refusing 标记),恢复后绿。聚焦三文件(macOS 直跑)60 passed(基线 47 + 新增 13:D1 7+D2 1+D3 5);全量 Docker `make test` 7644 passed、5 skipped、6 deselected、exit 0、零 failed(基线 7631 + 新增 13;5 skip 均为既有 Keychain 1+highspy 3+T1 卷探测 1)。真实运行(live 栏负证明实证)由主代理另行执行并另行记录。同日增补(缺口 E,用户已批准;仅改 `src/open_trader/prediction_solver_worker.py`、`tests/test_prediction_solver_worker.py`、`tests/test_prediction_solver_server.py`(E2 落点)与本日志):实证根因——本 macOS 主机内核不支持下调 `RLIMIT_AS`,`resource.setrlimit(RLIMIT_AS, <任意值>)` 必然抛 `ValueError: current limit exceeds maximum limit`(实测 1KB 也拒);worker `_apply_rlimit_as`(:411-421)未捕获 → 每条带 `memory_limit_bytes` 的请求被拒(`worker request rejected: current limit exceeds maximum limit`,exit 2,原始 stderr 已抓取复现),生产求解子进程路径在 macOS 全废(生产未暴露仅因 0 条 ACTIVE 关系从未发起求解,#63 激活后立即撞上);Docker/Linux 上 setrlimit(RLIMIT_AS) 正常工作。修法(用户已批准修法 1,平台性拒绝→跳过):仅对 `resource.setrlimit` 调用捕获 `ValueError/OSError`,拒绝时跳过内存限应用并向 stderr 发一行简短诊断(含目标 limit 值与拒绝原因;worker stderr 由 owner 捕获作诊断,stdout JSON 行协议零污染),不抛出;`_positive_int` 校验、`hard_cap` 计算与最终调用 `(target_soft, current_hard)` 传参的钳制语义(既有 `test_rlimit_as_clamps_to_a_finite_hard_limit_without_raising_it` 锁定)完全不变。生产影响:macOS 带内存限请求从必拒变为无内存限运行——该限在 macOS 从未成功应用过,无安全回退,只是把假性拒绝变成诚实降级;修复先于 #63 激活真实关系落地;Linux/Docker 强制语义不变。验证——E1 五测试逐条先红后绿(沿用既有 worker 测试 monkeypatch 范式):T1 平台无关红(monkeypatch setrlimit 抛该 ValueError、getrlimit (INF, INF),修前 `_apply_rlimit_as(1<<30)` 异常穿透);T2 macOS 本机红(不 monkeypatch 直调,修前真实 setrlimit 抛 ValueError;Linux/Docker 上此用例修前修后均绿——平台差异语义由 T1 锁定;测试以 try/finally 恢复原 rlimit,防止 Linux 修前路径残留进程级限值污染同进程后续测试);T3 诊断红(修前异常先于诊断;修后 stderr 含 limit 值 1073741824 与拒绝原因);T4 成功路径不变(finite soft=4096 下调至 2048 调用仍发生,元组 `(RLIMIT_AS,(2048,RLIM_INFINITY))`,与既有两条 rlimit 测试互补不重复);T5 校验不变(0/-1/1.5/"1024"/True 仍被 `_positive_int` 拒绝,`WorkerProtocolError(ValueError)`);E2 端到端红绿(真实 `SolverServerOwner` 子进程 `--backend cp_sat` + fixture 最小合法求解请求 memory 1GiB/soft 5s/hard 10s,落 `tests/test_prediction_solver_server.py`):修前 macOS 实测 outcome UNKNOWN/PROTOCOL_MISMATCH(worker exit 2、stderr `worker request rejected: current limit exceeds maximum limit`),修后 OK/COMPLETED/cleanup_proven 且原始 worker stderr 恰一行 `worker memory limit not applied: setrlimit(RLIMIT_AS, 1073741824) rejected: current limit exceeds maximum limit`(returncode 0、response status OK、stdout 协议不受影响);Linux/Docker 上 E2 修前也绿(平台差异,如实记录;全量 Docker 实测修后 Linux 在 RLIMIT_AS=1GiB 下 cp_sat 求解正常)。既有全部测试零修改、零新增 skip/xfail。验证计数:聚焦 5 文件(worker/server/validation/books/no-submit)134 passed(修前基线 124 + 新增 10;worker 59→68、server 5→6);全量 Docker `make test` 7654 passed、5 skipped、6 deselected、exit 0、零 failed(基线 7644 + 新增 10;5 skip 均为既有 Keychain 1+highspy 3+T1 卷探测 1)。真实生产运行由主代理另行执行并另行记录。
- 预测套利 LLM 关系校验输出加固（Ticket 04，不改任何通过/拒绝判定：APPROVE 门槛、`_deterministic_result`、rules 一致性检查、`RELATION_PROMPT_VERSION`/缓存 key 全部不动，缓存命中路径行为不变）。zhipu coding 通道：base URL 可经 `OPEN_TRADER_ZHIPU_BASE_URL` 覆盖（新增 `zhipu_base_url()`，默认仍为标准端点常量，原值原位）。输出重试：OUTPUT_INVALID 现按 `OPEN_TRADER_LLM_OUTPUT_RETRIES`（默认 2；负数/非整型拒绝）在 call 内重试，第 3 次起在 user 消息尾部追加按违规类型的中文定向修复指令（新纯函数 `output_repair_directive`：声明上次输出违反 schema 对应约束、要求重新输出符合 OUTPUT CONTRACT 的完整 JSON 单一对象、不重述市场数据）；transport 失败不重试；每次尝试前仍检查 `max_llm_calls`，耗尽立即返回 `{PROVIDER}_BUDGET_EXHAUSTED`；重试耗尽后的最终 validation 字段（status/reason `{PROVIDER}_OUTPUT_INVALID`/summary/model/provider）与现状一致且不写缓存。自动切换：新增 `OPEN_TRADER_PREDICTION_LLM_FALLBACK_PROVIDER`（默认空=禁用；非法值宽容落禁用）；主引擎在 OUTPUT_INVALID 重试耗尽 / transport 失败 / 入口熔断开路，且 fallback 可用（≠当前选中引擎、`provider_credentials_configured()` 为真、fallback 熔断未开、预算有余）时用 fallback 执行一次尝试——成功则正常 `_validated`+写缓存（`provider=fallback`），并首次实际切换时经 `set_llm_provider` 落库、写 `auto_failover` 审计（`source`/`trigger`=主因码/`violation`，异常仅 warning）；失败则返回 `llm_unavailable`、`reason_codes=(主因, {FALLBACK}_{…})`、summary 取备因文案；**自动切换后不自动回切**，仍由操作员显式选择引擎。NO_BALANCE 分类：`_http_failure_reason` 对 429/402 且响应体 `error.code=="1113"` 或 `error.message`（小写）含 "余额不足"/"insufficient balance" 返回 `{PROVIDER}_NO_BALANCE`（文案「账户余额不足，请充值或切换引擎。」）；401/403→AUTH_FAILED、无 body 匹配的 429→RATE_LIMITED 等既有顺序与结果不变。落库聚合：`record_llm_call` 新增可选 `violation`/`reason`（提供时必须非空 str 且 ≤64 字符否则 `ValueError`；OUTPUT_INVALID 尝试记违规分类、structured 为 None 记 `not_json`；transport 失败尝试记原因码；成功行不带两者）；`llm_usage_24h`/`llm_usage_24h_by_provider` 新增 `invalid_outputs` 计数与按 provider 的 `violations`/`failure_reasons` 分布——**这些键仅在非零/非空时出现**（协调者批准的条件键设计），无违规时段的聚合 payload 与旧形状逐键一致，旧行无字段不计入、不报错。重试冷却分级：monitor 对非 BUDGET/NO_BALANCE 类 `llm_unavailable` 的重试冷却由 3600s 收紧为 300s（新常量 `RELATION_VALIDATION_TRANSIENT_RETRY_SECONDS = LLM_CIRCUIT_COOLDOWN_SECONDS`，新纯函数 `relation_validation_retry_delay(reason_codes)`；`_BUDGET_EXHAUSTED`/`_NO_BALANCE` 结尾仍为 `RELATION_VALIDATION_RETRY_SECONDS` 3600s，常量不改名不改值）。Dashboard/payload：`provider_snapshot()` 与 `/api/prediction-arbitrage/llm-provider` 顶层新增 `fallback` 键（禁用时为空串；逐 provider 卡片键集不变）。输出解析共享披露：`_parse_structured` 新增代码围栏剥离（模型输出以三反引号围栏包裹但内容为有效 JSON 时现可正常解析），该函数同时被跨场所等价校验（`predict_cross_venue`）与标题翻译（`prediction_title_translation`）共享，这两个流程对「围栏包裹但内容有效的 JSON」从基线的 `json.loads` 失败（等价校验返回 `{PROVIDER}_OUTPUT_INVALID` 不可用、标题翻译记失败）变为接受并照常走各自原有下游校验门槛；方向为可用性提升，无任何拒绝→批准的反转。经批准的既有测试调整披露（共 4 处）：`test_unavailable_results_are_not_cached` 构造 validator 处加 `output_retries=0` pin（意图是「不缓存」语义，非重试计数）；`tests/test_llm_providers.py` 的 `_FakeApiError.__init__` 加可选 `body`；`test_transient_codex_failure_retries_once_at_the_retry_boundary` 改为 monkeypatch `RELATION_VALIDATION_TRANSIENT_RETRY_SECONDS`（冷却分级后 transient 边界由新常量控制，+59s/+61s 断言与其余内容不动）；`test_current_provider_prefers_store_row_over_env_and_defaults` 的 `provider_snapshot()` 精确等值期望字典增补 `"fallback": ""` 一键（S4 语义要求禁用时键存在）。验证：B1–B2、R1–R3、S1/S2/S4、V1–V8、M1 全部存在且逐条红绿；聚焦 `tests/test_llm_providers.py tests/test_polymarket_relation_discovery.py tests/test_polymarket_monitor.py tests/test_prediction_arbitrage_store.py tests/test_prediction_service.py` 480 passed（基线 448 + 新增 32 例）；全量 `make test` 7595 passed、3 skipped、1 deselected（exit 0；基线 7563 + 32，3 个 skip 为既有 solver highspy 缺失声明）；未运行 acceptance/deploy/push。
- 预测套利熔断 reset 已知持仓归类修复（Ticket 05，动因：2026-08-27 GTA 事故善后——8·29 处理时标准 `circuit-breaker/reset` 被账户中 Clarity 执行 `d29b1379…` 的两腿持仓误拒为 `unknown_external_state`，操作员被迫直写库定向确认并手工把执行移回终态）。已知持仓归类：`_reset_breaker`（`src/open_trader/prediction_arbitrage_execution.py`）持仓归类现传入 `known_tokens=self._known_holding_tokens()`，与 `reconcile_startup` 同语义——`holding_to_resolution` 执行（Threshold/Pair intent）的腿不再计入 `unknown_external_state`，也不计入事故执行自身的 `directional_imbalance`，startup 语义与 `_position_totals`/`_known_holding_tokens` 零改动。对冲完整性：新增 `_holding_imbalances` 逐笔已知持仓检查两腿对冲——两腿数量不等且剩余腿非已结算可兑付（redeemable）时仍拒绝 reset，拒绝原因 `directional_imbalance`；恰一腿>0 且该腿 redeemable（结算待兑付，与 `reconcile_cross_holdings_once` 的 winner 判定同方向）视为正常结算态放行；未知 token 持仓（不在任何 holding_to_resolution 执行）仍拒 `unknown_external_state`；事故执行自身 intent 单腿失衡仍拒 `directional_imbalance`（既有行为不变）。拒绝路径：不再把执行转入 `reset_denied` 非终态（该状态全仓库零读取点，曾要求操作员手工搬运），执行保持原状态，拒绝证据仍写事故 `last_reset_denial`（reason/blocking_reasons/at），并新增 `holding_imbalances` 明细键（仅非空时出现）；成功路径保留「active 匹配事故执行则转 `directional_incident`」收尾块，遗留 `reset_denied` 脏行可被后续成功 reset 自动收尾。已知披露：结算窗口期快照若缺 redeemable 标志，拒绝原因码可能从 `unknown_external_state` 变为 `directional_imbalance`——拒绝事实不变，仅原因更精确。验证：新增测试 6 例（经批准验收用例）对基线实现全部失败（stash 红灯验证）、修复后全绿——对冲完整已知持仓 + 事故 token 零落地 → reset 成功（fresh_clean 确认、执行回终态、熔断释放）；已知持仓 A 有 B 无 → 拒 `directional_imbalance` 且 `unknown_external_state` 不在 blocking_reasons；已结算 winner 腿 → 放行；未知 token → 仍拒 `unknown_external_state`；遗留 `reset_denied` 行被成功 reset 自动收尾；startup recovery 事故 + 已知持仓的 GTA 式回放无需直写库（`IncidentTrading` 新增 4 个 account_mode 桩）；聚焦 `tests/test_prediction_arbitrage_execution.py` 286 passed（基线 280）、`tests/test_prediction_service.py tests/test_prediction_api_contract.py` 72 passed；全量 `make test` 7601 passed、3 skipped、1 deselected（exit 0；基线 7595 + 新增 6）；非破坏性 preflight 用生产库副本（sqlite3 .backup）回放 8·29 场景类（Clarity `d29b1379…` 两腿真实 token + 事故零落地）：旧语义两腿均判 unknown（复现 8·29 拒绝）、新语义 unknown 空、holding_imbalances 空。尚未运行 acceptance/部署/push（按流程合并后另跑）。

- #108 now records the verified Docker/Candidate boundary: the image includes
  Python, Git, bash, zsh, make, the Node runtime, and `procps`, but excludes npm,
  Python/JS Playwright, Chromium/browser assets, host mounts, network, published
  ports, the Docker socket, the home directory, and credentials. Docker dev and
  `make candidate-acceptance` use one hermetic container; Candidate runs the
  full backend suite followed by portable non-LIVE scenarios with `&&`, and
  `make acceptance` is its non-mutating alias. Verified macOS-host browser
  evidence is `5 passed, 7602 deselected` for the Python marker suite and `20
  passed` for fixture Playwright Smoke; Host Readiness was not run. The safe-dust
  repair is test-only (`1 passed` focused node; `266 passed` execution file),
  with no production notification ordering change. The post-rebase mixed HTTP
  capacity failure was a test-only slot-release timing race, repaired by waiting
  for active requests to fall from 8 to the independently expected 4 before
  replacement; focused Docker verification passed 5/5, with no production
  prediction-service behavior changed. Final post-rebase Candidate PASS:
  backend `7597 passed, 4 skipped, 6 deselected, 1 warning`; portable scenarios
  `61 passed, 3 deselected, 1 warning`. Production Smoke, migration, merge,
  push, deployment, and live service mutation were not run.
- #108 Production Smoke now requires an existing absolute `EXPECTED_RUNTIME_ROOT`
  for the shared prediction-service logs, runs host tests and Playwright from
  the validated release root, blocks browser writes before navigation, and
  rechecks the unchanged submission markers after Playwright.
- #108 post-merge Docker-context follow-up: stable-main `make acceptance` stopped
  before tests when a cache-miss `pytest` fetch hit a PyPI TLS failure; production
  was untouched. A separate read-only audit found `.code-review-graph`,
  `.scratch`, and `.superpowers` entering the stable-checkout Docker context; the
  follow-up adds only their exact `.dockerignore` entries, with Dockerfile and
  dependency/download policy unchanged. Observed context fell from `288.31MB` to
  `28.80MB`; a minimal Docker ignore-semantics probe passed and a generated
  Candidate image proved all three `/workspace` paths absent. Final Candidate
  PASS: backend `7597 passed, 4 skipped, 6 deselected, 1 warning`; portable
  scenarios `61 passed, 3 deselected, 1 warning`. Host Readiness, Production
  Smoke, migration, services, push, deploy, and any production mutation were not
  run; the PyPI TLS issue was not fixed by this change.

## 2026-08-29

- 预测套利 preflight 失败证据结构化落库（Ticket 03，纯可观测性增量，不改任何通过/拒绝判定与 preflight 通过路径行为）：pair 与 threshold 两路 preflight 失败（含 preflight 自身抛异常）现在把结构化摘要写入该次 `validation_rejected` transition 的 evidence——新增 `_preflight_failure_evidence` 白名单提取（`signer_match`/`wallet_match`/`account_reads`/`geoblock`/`fok_pair_signed_not_submitted`/`equal_requested_shares`/`conditions`/`merge`/`error_code`/`result`/`posted`，经 `_safe_mapping` 规范；返回体 None/非 Mapping 或抛异常时落非空最小证据 `{"error_code": <code 或 "preflight_failed">, "preflight_exception": True}`，code 取自异常 `error_code`）；`_finish_rejected` 新增 keyword-only `extra_evidence`（其余调用点零改动零行为变化）；pair 路径 preflight 异常不再落入 `execution_error:*` 泛化事故，而是与 threshold 一致走 `validation_rejected`（原因优先取返回体/异常的 error_code，兜底 `preflight_failed`），并修复 threshold 异常被吞成裸 `preflight_failed` 的历史零细节形态；threshold 失败飞书通知在「原因」行下逐子项列中文状态行（签名者/钱包/账户读取/地区限制/FOK 双腿签名/数量一致，值 ∈ yes/pass/allowed → ✅，否则 ❌ 附原值），无证据时保持原文案，标题不变。store 侧经批准的精确增补：`_safe_value` 对精确键 `fok_pair_signed_not_submitted`（preflight 公开状态，值仅 pass/fail 等短状态，无签名材料）豁免 "signed" 子串丢弃规则，token 名与 sensitive 名规则及其余含 signed 键（如 `signed_pair`/`signature`）照旧丢弃。preflight 通过的执行 evidence 不含 `preflight` 键。验证：T1–T9 与 store S1/S2 逐条红绿；聚焦 `tests/test_prediction_arbitrage_execution.py` 280 passed、`tests/test_prediction_arbitrage_store.py` 111 passed、相关回归 `tests/test_polymarket_trading.py tests/test_prediction_read_model.py tests/test_prediction_arbitrage.py` 150 passed；全量 `make test` 首跑 7557 passed、6 failed（失败均为 worktree 缺 gitignored `data/trend_review` 历史快照的既有环境问题，按 08-27 先例补只读软链后复跑）7563 passed、3 skipped、1 deselected（exit 0）。
- 预测套利监控线程自愈与通知模板重设计（Ticket 02）：修复自动吃单任务回收路径吞不掉 `asyncio.CancelledError` 的缺陷（同类隐患 `_poll_relation_validation` 一并修复），该缺陷曾杀死整个 monitor 线程、launchd 进程存活但关系扫描静默停转 37 小时；monitor 线程新增监督器——崩溃自动重启（退避 1/2/4/… 封顶 60 秒），连续 10 次崩溃后放弃并发明确终态告警（不受限流），恢复判定为重启后首次 universe 刷新成功；外部真实取消（stop/超时）与 `SystemExit`/`KeyboardInterrupt` 原样重抛不算崩溃；重启时清理指向已死事件循环的任务引用与 universe 重试状态；崩溃通知 300 秒限流（只限通知不限重启），文案带「连续第 N 次 · 累计 M 次」；崩溃/放弃/恢复均经既有 failure observer 通道发飞书（`component="monitor_thread"`），通知失败只记诊断绝不杀死监督器；`snapshot()` 新增 `thread` 键（status/连续崩溃/重启/最近崩溃与恢复时间/停摆秒数），读模型 state payload 透传该键（非 Mapping 时给 `{}`）。飞书通知模板重设计：健康检查改为 PASS/WARN/FAIL 三态中文模板（标题带北京时间与失败项数，正文列中文检查项、阈值与 Dashboard/PID/版本短 SHA，`HealthReport` 新增 `checked_at` 字段并 additive 输出 JSON）；执行服务 LLM 校验不可用与行情刷新重试耗尽两分支文案按新模板重写；健康检查新增「监控线程」检查项（running=PASS，gave_up/缺失=FAIL）。验证：T1–T8/T10–T12、HT1–HT4、ET1–ET3 共 18 条批准用例逐条红绿；聚焦 `tests/test_polymarket_monitor.py tests/test_prediction_arbitrage_health.py tests/test_prediction_arbitrage_execution.py` 459 passed；`tests/test_prediction_read_model.py` 21 passed（含冻结 fixture 经批准的 `"thread": {}` 增补）；全量 `make test` 7550 passed、3 skipped、1 deselected（exit 0）。
## 2026-08-28

- Dashboard 当前趋势报告选择现在按 canonical 文件名的数字修订号排序，同一新鲜度、生成时间和执行日下 `-r2` 优先于 `-r1` 与基础报告；非 canonical 历史工件仍按文件名兜底。验证：回归用例 RED（错误选中基础报告）后 GREEN（`1 passed`），Dashboard 聚焦模块 `716 passed`；首次 `make test` 为 `7498 passed, 6 failed, 3 skipped, 1 deselected`，六个失败均由缺失的 ignored 历史快照导致；精确恢复快照后，中断重跑已通过其中 1 个，`PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q --lf` 再通过其余 `5 passed in 0.48s`，未再次运行完整套件。
- 预测套利提交通道新增可观测性与三查自证清白（Ticket 01）：`polymarket_trading` 的 `submit_pair_once`/`submit_threshold_hedge_once` 在 POST 异常时把脱敏三件套（`error_code`/`error_type`/`message`，500 字符截断）记入 `last_submit_error()`，dataclass 返回值保持 ambiguous 不变；执行层在 reconciling evidence 并入 `post_error_*` 三键，核对窗口 30 秒超时后改走一次账户快照三查（挂单空、两腿无持仓、余额 Decimal 精确相等），全过则落新终态 `submit_failed_cleared`（store 终态与 partial index 同步扩充）、不开熔断、不建事故、推飞书「预测套利单提交失败（已自证零落地）」并自动恢复下单；任一查不过或快照不可得则维持原 `directional_incident` 人工路径，事故 evidence 附带 `zero_landing_check` 摘要。读模型 state payload 新增 `last_execution` 摘要（state/updated_at/event_title/post_error_*/zero_landing），冻结黄金 fixture（`tests/test_prediction_read_model.py` 内联 JSON，经用户批准增补）同步手工最小更新——diff 仅新增 `last_execution` 一键、零无关漂移；Dashboard「交易与合并」表状态走 `predictionExecutionStatusLabel` 映射，`predictionExecutionAlert` 对 10 分钟内的 `submit_failed_cleared` 渲染 info 横幅（含原因、自证依据与「已自动恢复」）。验证：AC1–AC9 逐条红绿（新增 12 例）；聚焦 `tests/test_polymarket_trading.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py` 共 741 passed；`tests/test_prediction_read_model.py` 20 passed；相关回归 `tests/test_prediction_arbitrage.py` 50 passed。修复轮 1：三查 `_zero_landing_check` 的腿持仓 token 键由仅认 `token_id` 改为与执行层既有读法一致的三键回退链（`token_id`→`tokenId`→`asset_id`），生产 `tokenId`/`asset_id` 拼写下不再把已持有腿 token 误判为零落地，新增生产拼写红绿测试 2 例、既有测试零修改。修复轮 2：飞书自清通知的「原因」行按 `post_error` 是否有值条件化，ambiguous 且无 `last_submit_error` 详情时省略该行，消息不再渲染残缺「原因：｜：」；新增红绿测试 1 例、既有测试零修改。
- 修复 CN V17 回撤预检使用官方 A 股、ETF、REITs 三池身份，并将 `kelly_sample_inherits` 从回撤身份哈希中排除而保留审计 lineage；真实回撤参数漂移仍要求升版。验证：两个 acceptance exact cases `2 passed`，直接受影响套件 `86 passed`。
- 修复 CN V17 报告在回撤基准后置 bootstrap 后的生命周期：普通崩溃重试与显式 revision 均从新 `ok` 决策重建 FIFO/席位并更新 planning/replay 引用，已 `ok` 冻结事实保持不变。验证：生命周期五用例 `5 passed`，受影响模块 `1329 passed`。
- CN V17 now accepts a current/ready official REIT warm-to-hot pool with zero rows while rejecting stale-only REIT data; A-share and ETF empty/stale behavior remains fail-closed. Its public strategy snapshot/report source label now names A-share, ETF fund components, and REITs while CN V16 remains unchanged. Verification: source-label regression RED then GREEN; focused empty/stale pool and Trend Animals checks pass.
- 将 `make acceptance` 收敛为合并后的 clean `main` 一次 runtime-only 验证；合并前由 worktree `make test` 与当前工件只读/临时副本 preflight 负责，验收失败先完成一次全量只读错误及下游依赖审计，再批量 fix-forward，不在单个修复之间重复验收。结果按起始 SHA 绑定，精确 SHA 部署仍需 `PASS` 与显式授权，push/部署仍需显式授权。
- CN V17 报告修订现在保留旧两来源标签的精确回放，同时在版本/标签变更时重捕官方三池、回撤、FIFO 与席位，并同步冻结 evidence/planning/replay 引用；共享 allocation-v2 冻结契约和 staged publisher 拒绝控制器不可执行的计划。Futu 实盘统计先排除 option 等非股票成交，再执行股票数量/价格校验；零价股票仍失败。验证：V17 lineage、staging、Futu filtering 聚焦用例及 `make test` 通过。
- Routine `make test` and `make acceptance` gates now exclude registered pressure cases; `make test-pressure` retains the 10k relation check.
- N_LEG 读模型投影(issue #104,纯数据层,不改任何页面)新增资格画像与余额诊断:dashboard `n_leg_solutions` 每条目现在输出 `qualification`(四项定点整数检查——最低利润、净边际、年化(净边际 × 365 ÷ 剩余天数,按 24h 向上取整)、资本释放窗口(≤ 30 天且严格在未来),任一不可判定整体 UNKNOWN,另含最坏状态与 OPTIMAL/QUALIFIED_FEASIBLE 最优性)、`funding`(按 venue 的 required/available/allowance 诊断,余额与授权错误码分立 `INSUFFICIENT_BALANCE`/`INSUFFICIENT_ALLOWANCE`,快照缺失 fail-closed 为 UNKNOWN)、顶层 `main_list`/`executable`(true/false/null 三态)/`blocked_reason`;`would_submit` 收紧为与未来真实执行同一资格策略(执行方案存在 ∧ EXECUTABLE ∧ QUALIFIED_VERIFIED,不叠加读时资金门);Prediction 服务启动时幂等注册 exact scope `SAME_EVENT_SAME_VENUE`(capability 固定 OBSERVE_ONLY,成员仅 polymarket 同事件同 venue,已存在则跳过绝不 bump 版本,失败仅记日志不阻断启动)。验证:12 条已批准验收用例(资格四项边界、UNKNOWN fail-closed、余额/授权分立错误码与钱优先、FUNDING_UNKNOWN 三态、种子化幂等、调用方策略透传)随聚焦套件 `tests/test_prediction_n_leg_read_model.py tests/test_prediction_n_leg_mode.py` 39 passed;rebase 后全量 `make test` 7516 passed、3 skipped。

## 2026-08-27

- CN 趋势 v17 现纳入官方 A 股、ETF 与 REITs 温转热池；运行不查询收藏夹，REITs 沿用现有入场纪律、名义仓位与跨资产轮换。版本升级默认继承上一版本 Kelly 样本与最新市场回撤状态，缺少历史回撤基线时安全跳过；Dashboard 当前报告按 canonical v17 与结构化 v2 计划校验，历史冻结报告保持可读。验证：CN v17/REITs 与回撤、Kelly、Dashboard 聚焦用例 14 passed。
- Dashboard 现允许当前 CN v16/HK v14/US v14 报告在既有计划止损风险超过组合审计上限时继续选中；止损风险仍仅作审计。
- 修复 Dashboard 分离趋势报告验收误报：`计划止损风险仅审计，不参与买入数量` 现在允许展示，不再被误判为持仓或执行信息；其余禁用文案校验保持不变。验证：回归用例 RED 后 GREEN（`1 passed, 1 warning`），Dashboard 审计-only UI 回归 `1 passed`；`make test` `7460 passed, 3 skipped, 1 warning`（exit 0）。
- 趋势当前报告修订现在从冻结事实刷新显式变更的 allocation，并同步替换 planning manifest 中的 content-addressed market component；当前 CN/HK/US 执行继续只取最新报告，Dashboard 风险校验按生成器的 Decimal 运算顺序计算。验证：控制器回归 `3 passed in 5.65s`，修订 manifest 聚焦 `5 passed in 0.92s`，legacy fixture 聚焦 `6 passed in 0.60s`；`make test` `7459 passed, 3 skipped, 1 warning in 714.02s`（exit 0）。
- 趋势控制器现在只执行当前派生周期的最新有效 CN/HK/US 报告；历史报告与未完成历史批次仅供审计，不再执行、阻塞或回退；跨周期的在途报告生成结果会被丢弃，当前周期重新生成。验证：当前规则/客户端/修订聚焦 `5 passed`，legacy snapshot 聚焦 `6 passed`，全量 `make test` `7453 passed, 3 skipped, 1 warning`（exit 0）。
- 关系目录生命周期治理（issue-96，修复 ACTIVE 代际共享时间线死锁 `ACTIVATION_BLOCKED_INCONSISTENT`）：facade 新增 `expire_stale_members`——`capital_release` 早于注入 `now` 的 ACTIVE 成员以全新 EXPIRED 语义自动退出代际（非 revoke，v2 新增 cause 通道 `expired`），逐条 `expired` 审计（intent 先行）并在同一笔写事务内完成成员退出与审批重置——要么全部落库、要么全部回滚；被时间线卡死的 APPROVED+BLOCKED 候选随轮转回到 PENDING 重新入队（根因解锁）。队列治理：REVOKED/EXPIRED 身份的重复发现直接拒收（审计 `intake-rejected`，不再生成新 PENDING），候选准备阶段对这类被拒收结果计入 skipped 而非 PREPARED、不占用每轮候选名额；新增有界 `reject_stale_pending` 清退非 latest 的 PENDING 僵尸（诊断 `STALE_NON_LATEST`，上限默认 100）；`approve_many` 对 blocked/error 结果补写 `approve-blocked` / `approve-error` 审计；新增单事务批量 `revoke_many` 及 `POST /relations/revoke-batch`。Tier 1 直启（无 dry-run、无试点）：新增 `config/relation_auto_confirm.json` 白名单仅含 `VENUE_METADATA × NATIVE_COMPLEMENT / EXACTLY_ONE`（tier1 active，每轮上限 100），确定性规则及其余来源按缺席保持人工；全量扫描完成后经异常隔离钩子执行轮转→自动确认（actor `lifecycle:expire` / `auto-confirm:tier1-venue-metadata`），错误率 >5% 熔断该 tier 并通知，全部受阻零错误仅告警不熔断，手动轮与监控观察轮经 runner 内单锁串行、后到者见空队列作正常空报告而不因良性重叠误触熔断；配置逐 tier fail-closed 并在轮次报告上报错（策略文件存在但无 enabled tier 时仍装配 runner 照常上报错误，缺文件才视为功能关闭；文件存在但为无效 UTF-8 的二进制损坏同样 fail-closed 上报配置错误而不阻断运行时启动）；新增 `POST /relations/auto-confirm-round` 与 `/stale-reject`（confirm 契约与 approve-batch 一致）。EXPIRED 状态映射 history 视图。验证：聚焦 `tests/test_relation_lifecycle.py tests/test_relation_auto_confirm.py tests/test_relation_catalog.py tests/test_relation_catalog_service.py` 76 passed；监控/候选相关套件 233 passed（修复第 3 轮后：上述四套件连候选管线 `tests/test_prediction_relation_candidates.py tests/test_mechanical_relations.py` 共 110 passed）。
- 关系审批路径（#98）：v2 目录持久层改为脏行落盘 + generation 快照增量编码（delta/1000 次锚点，仅成员变化时追加），激活校验改为增量——GROUP_BUDGET/可满足性按合约分量、#102 事件门按编译终态观察键分量、新增全局陈旧资本释放与估值单位守卫；facade 新增单事务批量 `approve_many`（逐条独立结果，条目冲突不中断），单条 `approve` 统一为一笔原子写事务；新增 `POST /api/prediction-arbitrage/relations/approve-batch`（confirm 必填、空 items 400、shadow 403 沿用）。#94 并发回归按新语义重写：并发较新版本不被陈旧审批回退的性质保留，合约不相交的陈旧候选现在合法激活。基准：10k 激活态单条 approve 中位 25.05ms（p95 39.5ms）、10k 条批量 5.34s（脚本 `scripts/benchmark_relation_activation.py`，阈值 50ms/60s 内）。验证：关系目录聚焦套件 155 passed；10k 规模正确性批量测试通过（批量 7.2s、只断言正确性）；增量/全量 oracle 一致性矩阵（500+ 步、含观察键与陈旧形状）全等；全量 `make test` 7447 passed、3 skipped（exit 0，worktree 环境补 gitignored `data/trend_review` 软链后）；acceptance/deploy/push 尚未运行。

## 2026-08-26

- Trend Allocation v2 now preserves and ranks each market using its own latest completed-session date; the no-submit three-market regeneration helper passes the allocation date to all runners, and US latest-session resolution now skips weekends/market holidays to select the prior completed session. Verification: focused allocation/discipline/regeneration/market-date suite `143 passed`.
- Dashboard history acceptance now requires ledger-referenced action presence for all strategy versions while allowing allocation-v2 rows without row-level execution metadata; legacy rows remain strict. Verification: approved Case 1 RED then GREEN; Case 2 GREEN before and after; focused dashboard acceptance file `393 passed`; six restored legacy-fixture tests `6 passed`; full `make test` `7429 passed, 3 skipped, 1 warning` (exit 0).
- Trend 当前 CN v16 / HK v14 / US v14 按自有账户 NAV × 4% 记录名义数量（不足一手保留最小一手），每轮只选最新同日修订；实时执行检查重复标的、动态替换席位并按可卖数量封顶卖出，实盘计划保持只读不自动提交。验证：受影响 Trend/Dashboard/acceptance 套件 2107 passed；全量 make test 7427 passed、3 skipped（exit 0）。

## 2026-08-25

- Trend reports now pause new entries when existing planned risk is already above 4%; exact equality retains minimum-lot behavior. Verification: approved Case 1 RED then GREEN; equality/validator/paused-payload boundaries `3 passed`; full `make test` `7261 passed, 3 skipped, 1 warning` (exit 0).
- Launchd dry-run plist linting now uses unique BSD-compatible `mktemp` templates, and the mixed Prediction Service HTTP capacity test keeps admitted handlers alive for its bounded orchestration. Verification: approved launchd cases RED `5 failed` then GREEN `5 passed`; capacity case `1 passed` plus the exact 20-run loop `20/20 passed`; focused suites `316 passed, 1 warning`; full `make test` `7260 passed, 3 skipped, 1 warning` (exit 0). Acceptance/deploy/push were not run after this fix-forward.
- Trend nominal sizing: current CN v16 / HK-US v14 target amount is simulated or real account NAV times 4%, or the smaller positive frozen Kelly cap; cash, cost, and lot size constrain quantity/executability rather than rewriting the target, and stop risk is audit-only and does not cap quantity. An executable current formal BUY must fit carried cash; cash-insufficient minimum-lot rows remain no-submit and cannot enter FIFO even if re-authorized or tampered. For current CN16/HK14/US14 formal BUYs, the `executable` field is mandatory boolean; only literal `True` enters FIFO or direct simulated execution, and missing remains compatible only for legacy CN15/HK13/US13. Explicit-v2 replay/validation covers formal buys, real read-only buys, and simulate/real rotations, with audit values serialized and bound to frozen price, ATR, initial line, cost, risk, and risk pct; the legacy serialized shape remains unchanged. Current Core validation rejects strict carried-cash overspend for real read-only BUY plans and simulated/real rotation pairs; equality is allowed. Current real manual rotation sizing is cash-constrained and emits no impossible pair when carried cash is negative. Current FIFO independently validates automatic rotation cash using paired sale proceeds net of frozen normal cost, counted once, then consumes deduplicated formal and automatic-rotation BUY cash in one global-strength order; malformed current cash, FX, price, shares, or lot facts fail closed. FIFO cost rate uses risk summary first, then frozen strategy parameters; if neither valid source exists, current FIFO fails closed. Duplicate frozen candidate symbols fail closed before price/ATR lookup for current versions. Dashboard sizing replay is independently implemented, not delegated to Core; markerless current reports project as v2 while retaining frozen quantities, and the latest valid same-day revision is selected before immutable batch lock. Markerless current reports remain subject to exact frozen nominal sizing validation. Markerless legacy CN15/HK13/US13 retains staged allocation-v2 FIFO and frozen completion quantity (no live quote repricing/substitution regression). Forced-sale cash is net of frozen normal cost in report generation, Core replay, and Dashboard replay; Dashboard independently requires current formal BUY target weight to equal the frozen configured/Kelly-capped nominal weight; sell-only reports freeze zero planned buy seats. Dashboard applies the same independent strict replay to markerless current reports and rejects missing/non-boolean formal authorization before any portfolio-risk-overflow branch. Current real manual rotation validation uses frozen available cash plus forced/paired sale value net of frozen cost, never NAV; a missing legacy sale value contributes zero and is accepted only when available cash independently proves affordability. Legacy CN v15 / HK-US v13 and historical behavior remain frozen. Verification: authoritative non-sandbox worktree `make test`: `7401 passed, 3 skipped, 1 warning`, exit 0; acceptance/deploy/push/merge/rebase/review-pass not claimed.

## 2026-08-24

- Dashboard launchd installation now stops the prior job before truncating candidate logs, preventing mixed-generation log freshness failures. Verification: approved Case 1 RED `4 failed in 5.65s` and GREEN `4 passed in 5.33s`; reviewer Case 2 RED `2 failed in 4.33s` (stdout/stderr truncation failures) and GREEN `2 passed in 2.34s`; focused suites `458 passed, 1 warning`; full `make test` `7260 passed, 3 skipped, 1 warning` (exit 0); acceptance/deploy/push not run after this fix.
- `make acceptance` no longer gates on real Predict/Polymarket market or account state; local fixture-backed Prediction Playwright and stable fake-client/unit coverage remain. Verification: approved Case 1 RED then GREEN (`1 passed`); focused suites `865 passed, 1 warning`; rendered plan has no external-live tokens and Playwright precedes Dashboard; `make test` `7254 passed, 3 skipped, 1 warning` (exit 0); acceptance/deploy/push not run.
- Concurrent relation approvals now preserve committed generation members; the #94 production concurrency regression is enabled. Verification: all three focused approved regressions 3 passed; original #94 concurrency regression repeated 20/20 passed; related catalog suites 97 passed; full `make test` exit 0 (7231 passed, 3 skipped, 1 warning); acceptance/deploy/push not run for this change.
- 审批队列新增机械关系候选（issue-103，`discovery_source=VENUE_METADATA`）：官方 YES/NO token 互补对（新类型 NATIVE_COMPLEMENT）与 negRisk 互斥穷尽组（EXACTLY_ONE），均编译为 5 终态/合约的 EXACTLY_ONE 模型、PENDING 人工审批；全量扫描每轮最多自动准备 1 条机械候选。验证：聚焦 `tests/test_mechanical_relations.py tests/test_prediction_relation_candidates.py tests/test_polymarket_monitor.py tests/test_relation_catalog.py tests/test_relation_catalog_v2.py tests/test_polymarket_relation_discovery.py` 为 320 passed（含 #94 并发回归）；rebase 到 4971d9c7 后完整 `make test` 7255 passed、3 skipped（退出码 0，rebase 首跑中 1 个 launchd 安装测试因 /tmp 并行残留瞬时失败，单独与全量复跑均通过）；尚未运行 acceptance/deploy/push。
- Dashboard acceptance now retries transient initial Account snapshot 503s, and US/Futu trade stats use the canonical Futu actual-source cutoff instead of retired Tiger data. Verification: approved cases 2 passed; relevant Dashboard files 665 passed; full `make test` 7228 passed, 3 skipped, 1 xfailed, 1 warning (exit 0); acceptance/deploy/push not run after this fix-forward.
- 趋势报告移动端：执行状态摘要保持触控目标，375px 控制器卡片约束在报告范围内。验证：Case 1/2 聚焦测试 2 passed；完整 make test 7225 passed、3 skipped、1 xfailed（退出码 0）；尚未运行 acceptance/deploy/push。
- `make acceptance` 在离线 pytest 通过后先确认当前 checkout 为干净 `main`，再按 Account → Dashboard → 全市场 Trend 顺序 dry-run 并刷新本地 launchd runtime，避免旧 SHA 进入 live 验收。验证：聚焦 `test_make_acceptance_refreshes_main_runtime_after_tests_before_live_checks` 1 passed；完整 `make test` 7226 passed、3 skipped、1 xfailed（退出码 0）；尚未运行 acceptance/deploy/push。

## 2026-08-23

- 趋势报告允许在一手动作证据完整覆盖溢出的前提下，序列化/展示生效最小交易单位超过剩余 4% 风险预算；缺少该证据或证据被篡改的超预算报告仍拒绝。验证：scope/invariant 回归 17 passed；聚焦报告/看板测试 813 passed；完整套件 7224 passed、3 skipped、1 xfailed（退出码 0）；尚未运行 acceptance/deploy/push。
- Futu US 历史报告验收边界固定为 2026-08-20：此前账本动作跳过历史投影且不读取 Tiger 报告，边界日及之后仍要求 Futu 冻结报告。验证：聚焦 `PYTHONPATH="$PWD/src" /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k 'futu_history_ignores_pre_cutover or rejects_history_that_drops_ledger_referenced_old_action'` 为 `2 passed, 384 deselected`；完整 acceptance 文件为 `386 passed, 1 warning in 4.87s`；此前缺失的 worktree legacy snapshot cases 在复制三项 unchanged local facts 后为 `6 passed in 4.98s`；`PYTHONPATH="$PWD/src" make test` 为 `7208 passed, 3 skipped, 1 xfailed, 1 warning in 663.33s`；本次 fix-forward 尚未运行 acceptance/deploy/push。
- #102 关系目录激活门升级为「分量级同事件校验」：激活（`RelationCatalogV2.replace` 批量发布）在编译预检成功之后，对 prospective generation（既有 ACTIVE + 本批新关系）的编译产物按求解器同口径合并规则（`build_relation_components`：settlement_observation_key 指纹相同自动合并 + 显式 relation + forbidden 组合）计算分量，断言每个分量内全部合约单一 venue 且 `event_identity_basis` 唯一（逐字节）。违规分量中本批新 identity 落新 blocked cause `ACTIVATION_BLOCKED_CROSS_EVENT`，诊断写明冲突合约与双方 basis/venue；已激活身份留任，store 回滚到调用前快照（沿用编译预检惯例），干净分量照常发布。`event_identity_basis` 正式化为事件身份字段并保留进 v2 存储（`_converted` 不再丢弃），且纳入版本指纹（同关系重新上报不同 basis → 新 PENDING 版本，旧 ACTIVE 留任供求解）。升级前的存量版本若参与激活且某合约无 basis → 该分量落新 cause `ACTIVATION_BLOCKED_EVENT_IDENTITY_MISSING`（同样「新挡旧留、回滚」）。生产 generation 当前为 0、无存量 ACTIVE，无需数据迁移。v1 schema 必填集合、`_EXCLUDED_FIELDS` 均未改动。验证：新增 12 个验收/回归用例（含跨批连坐、跨 venue、NegRisk 放行、标题相似不误并）；聚焦 5 套件全绿；`make test` 退出码 0。dashboard 六态映射识别两个新 blocked cause；补同批连坐用例。
- 市场仅按股票根全局强度排名；席位 20/15/10，新目标统一 4%；ETF 根不参与市场排名，股票级 ETF 候选仍按个体强度入选。
- 每个目标日冻结快照，分别派生模拟/实盘买卖计划；卖出为「清仓」/「轮换」，展示与执行审计分离。同日修订复用冻结事实并持久化重建保护状态；实盘仅人工执行。
- 模拟先卖后买，买入不依赖卖出完成/所得资金；按不可变 FIFO/目标总量，跳过数据缺失候选，账户+标的串行化，防止卖出做空、买入重复/超报告目标。部分/非终态/未知订单 fail-closed；可操作失败通知人工。验证：order contract `30 passed`；snapshot/report integrity contract `10 passed`；staggered recovery contract `3 passed`；revision identity contract `14 passed`；dashboard availability contract `3 passed`；final review fixes: Cases 20-28 `19 passed`, Cases 29-30 `6 passed`, Cases 31-32 `3 passed`；ticket-wide contract `91 passed`；affected report/review/controller `1264 passed`；worktree-bound make test `7194 passed, 3 skipped, 1 xfailed, 1 warning in 707.54s`；分支未合并，未运行 acceptance。

## 2026-08-20

- 趋势纪律 v2：三市场资源排名改为股票根节点全局强度，按 20/15/10 席位与 80%/60%/40% 名义仓位冻结；个股候选统一按全局强度排序，轮换支持动态席位并先完成卖出再刷新账户、行情后买入。Dashboard 补充信号清仓/轮换清仓分类和 v2 资源卡；聚焦趋势、执行、通知与 Dashboard 回归通过；聚焦 v2 测试 8 passed，隔离 worktree 全量 `make test` 通过（6944 passed, 3 skipped, 1 xpassed）。
- 调整项目协作流程：开发与评审固定在隔离 worktree，运行时验收仅在合入 local `main` 后按适用范围执行；本次仅 docs/config，验证范围为 `git diff --check`、role/config probes 与 review，不涉及 tests、acceptance、deploy 或 push。

- 关系目录 v2 激活闸门 fail-closed：`replace()` 在发布前对「将成为 ACTIVE 的完整集合」跑全量编译预检（`relation_generation_problem`，编译缝本身未改）。候选与存量集合合并编译冲突（action/terminal state set/relation/forbidden atom combination/qualification constraint 任一 key 的规范化 payload 不一致）或触发 `STALE_CAPITAL_RELEASE_AT`（候选 as_of 使存量行的资本释放时间过期）时：本次 change_set 中不在先前 generation 的身份全部 block 为 `ACTIVATION_BLOCKED_INCONSISTENT`，先前成员批准与 ACTIVE 原样保留（当前 generation 不变），被 block 身份不进 new_generation/approved；编译通过则行为与原先完全一致。`_satisfiable`/`GROUP_BUDGET` 语义不变。门面批准链去掉发布前的 v2 approve 预写（其与 replace 的成功路径完全冗余，且会把候选身份提前写进 generation 使闸门快照失真），失败时由 `_activate` 补记 status=APPROVED + activation_status，对外状态可见性不变。新增 `prediction-arb catalog-doctor` 只读诊断：对当前 generation 迭代归因编译失败（冲突 key 的持有者/side/规范化 payload 摘要按「少数派移除、平票按序列化字典序」确定性提案；STALE 行按「行内任一 atom 的 `capital_release_at < 合并最大 as_of`」判定（严格小于，直接以行内 atom 的 capital_release_at 与合并 as_of 严格比较，与 oracle 判据逐 atom 一致，相等不算 stale；as_of 早于全部 atom 释放时间的合法行不算 stale）提案），输出 compiles/conflicts/stale/proposed_removal/remaining/error，全程零写入；清理执行走 `RelationCatalog.rebuild_generation(drop_identities, actor, git_sha, ...)`：逐条校验 drop 身份确为 ACTIVE 成员，整集重新发布（不经过 v2 cause 账本，杜绝整组 UNKNOWN 污染），被 drop 版本记 REVOKED，逐条写 `catalog_v2_audit` 审计行（v2 命名空间新表；存量 v1 时代同名 `relation_catalog_audit` 表不写不改）（表首次写入时懒建，只读打开零写入），drop 后集合仍不可编译默认拒绝、`--allow-uncompilable` 也由 fail-closed 闸门兜底拒绝。CLI `prediction-arb catalog-doctor --data-dir <dir>` 默认只读报告、`--json` 全量 JSON；`--apply` 必须显式 `--drop` 身份列表 + `--yes`。验证：聚焦测试 110 passed（catalog/doctor/service/runtime_graph 相关：doctor 报告与 apply、审计行、CLI、v2 闸门冲突/STALE/兼容三态与门面 AC 回归，含「相等不算 stale」「混批只移除旧行」两个新回归）+ seam 消费方 45 passed；`make test` 全量 6952 passed / 3 skipped（既有 highspy 声明）/ 1 xfailed（既有 #94）/ 退出码 0。真实数据只读冒烟最终结果：proposed_removal 92、remaining 60、compiles=True——修复前因「顶层 capital_release 相等被误判 stale」曾收敛到 152/0，已按 oracle 严格小于语义修正；冒烟只读（库文件 mtime 与内容均不变），报告列出 ≥20 个冲突 key 与过期身份行。第二轮评审修复：诊断与清理预检全部按编译缝准入集合（ACTIVE 且模型完整，准入判据统一复用 `relation_row_admitted`）口径工作——doctor 的 merged_as_of/conflict 持有者收集/remaining/compiles 与 `rebuild_generation` drop 后预检均只在准入行上进行，cause/UNKNOWN 成员不参与归因与预检、不进任何提案（报告新增 `excluded` 数量供运维参考）；新增评审场景回归（revoke 制造的 as_of 较晚 UNKNOWN 成员不再抬高 merged_as_of 误删 oracle 新鲜 ACTIVE 行、不参与冲突平票、不抬高 rebuild 预检）。交付部署：合并 `5a741d71` 后全栈重装对齐（网关+legacy、account-api production+sync、趋势三控制器+allocation、prediction-service+health 全部新 PID @ `5a741d71`）；经用户确认后执行目录清理 `catalog-doctor --apply`（移除 92 条：32 条冲突少数派 + 60 条 8/17 已结算过期关系，92 条 intent 审计行落 `catalog_v2_audit`），真实 catalog `relation_generation_problem` 恢复编译（60 条 ACTIVE / 10 个分量），prediction-service `tick failed` 停止累积（冻结于第 72521 条）、healthz 全绿；验收代码面全过（全量 pytest + prediction LIVE/OPS 场景），dashboard 对齐类失败全消，futu/趋势记账遗留项归属另一工作流不在此范围。

## 2026-08-19

- 趋势报告买入名单改为「信号优先」并升级策略版本 CN v13→v14、HK/US v11→v12（三市场同批）。用户裁定：报告的职责是提示什么在趋势上，仓位约束只决定买多少、不否决入场。触发场景：2026-08-19 HK 报告 06160 百济神州因「最小交易单位 100 股超过名义仓位上限」被排除（一手超 4% 目标仓位但 0.4% 风险/现金充足）；且 US 8/4–8/14、HK 7/28 因单点数据缺失（SNOW 活动保护线、00027 价格）整批暂停、整张名单被藏。新语义（仅 v14/v12 生效，FINAL_PLAN_TREND_VERSIONS 门控；≤v13/v11 行为与证据回放逐字冻结）：(1) 名单成员=全部通过趋势过滤的候选；名义仓位/单笔风险 0.4%/组合剩余风险/Kelly 正值收缩/现金/席位六类约束只定量+提示，定量=max(最小一手, 常规取整)，一手超限照列一手；(2) 仅存两类整批熔断：策略累计回撤 ≥5%（人工解锁）、Kelly 上限=0；账户级事实（净值/现金/汇率）整体缺失保留整批停止并标为数据错误；(3) 单点数据缺失降级为逐标的提示+报告级数据缺陷横幅（持仓价格/保护线缺失→「组合剩余风险不可用」继续列名单；候选价格/ATR 缺失→该行提示；HK 每手未知→shares=0 列入），根因（SNOW/00027 等）另开工单不在本次；(4) BuyAction 新增 sizing_note/executable 字段，Dashboard 买入表新增「额外风险」列、待条件行置灰且动作显示「待条件」，Markdown/推送行尾追加「｜额外风险：…」（现金场景专属模板「现金不足一手（需约 X，可用 Y）」）；(5) 资源模拟只有可执行条目扣减现金/风险/席位，risk_summary 的 new_planned_risk 只计可执行并附注「另有 N 条待现金/席位，未计入」（硬预算数值 0.4%/4% 不变，超支如实呈现）；(6) 执行器适配：executable=False 的 BUY 写 pending（waiting_for_cash_or_slot）不下单不记 missed、_execution_completed 视为完成不阻塞轮换；0 股数据缺失条目不产轮换对、执行侧对无效 lot/ATR 记 terminal skipped（rotation_sizing_inputs_invalid）不再抛异常；(7) Dashboard 校验放宽仅两处：planned_stop_risk_pct>0.4% 仅当股数=最小一手、shares=0 仅当数据缺失类提示，其余不变量保留，旧报告（v13/v11 及更早、旧 risk_skips、无新字段）继续通过校验与回放。同步升级所有版本钉死点（strategy_drawdown ALLOCATION_PROJECTION_VERSIONS、a_share_trend/trend_review/dashboard/dashboard_acceptance 版本集合、regenerate 脚本 EXPECTED_VERSIONS），旧版本全部保留可读；纪律参数「买入数量」行新文案仅对 ≥v14/≥v12 生效，旧版本期望快照逐字不变。验证：聚焦 1705 passed；全量 6855 passed / 3 skipped（首跑 25 个 prediction solver 子进程类失败为环境抖动，复跑全绿）；06160 复现：v14/v12 下列入 100 股+额外风险「一手超过名义仓位上限」+executable=True，v11 回放决策不变（仍 risk_skip）；线上 2026-08-19 CN v13 / HK v11 / US v11 报告校验仍 valid。reviewer 三轮：首轮 4 发现（P0 旧版本报告被校验拒绝、P1 执行器异常/误下单、P2 语义未按版本门控破坏回放、P3 提示文案重复）全部修复；次轮 2 P1（待条件条目永不终态化卡死轮换、每手未知生成 0 股轮换对执行抛异常）+2 P3（缺回归测试、旧回放多两个 risk_summary 键）全部修复；第三轮 No findings。交付部署：合并 0cc8edd7 后全栈重装对齐（趋势三控制器+allocation、网关+legacy 栈、account-api production+sync；全部 PID/SHA 对齐 @ea7d9fa3——含存量测试夹具修复提交后的二次重装）。附带修复：#74 落地遗留的存量测试断裂（`test_prediction_read_model` n-leg 投影夹具仍用 UNKNOWN 证明并断言 order_ready True，在合并前 main@52d774ce 即确定性复现、与本次改动无关），按 #74 fail-closed 规范夹具改为 PARTIAL_FILL_SAFE（46 相关测试通过，happy-path 断言保留）。`make acceptance` 终验 **PASS**（第三次运行：首轮 FAIL 即上述存量断裂；次轮 FAIL 为 kickstart 重启不满足验收日志形态——控制器 runtime 记录由安装脚本写入、网关/legacy 需全新日志文件，改按交接惯例安装脚本全栈重装后解决；第三轮全量 6889 passed / 3 skipped / 1 xfail、63 场景含 LIVE 与浏览器流全过、errors 空、三市场回撤前置 ready）。验收后核对：网关 PID 72669 cwd=repo @ea7d9fa3 与 main HEAD 一致、legacy 72602、account-api 90795 api/worker 同 SHA、review URL HTTP 200。06160 验证：2026-08-18 HK 报告中 06160 为唯一排除项（最小交易单位超名义仓位），以该报告冻结事实重放（close 220.8、ATR 7.936、净值 993,411.73、资源排名第 3 权重 2%→19,868 < 一手 22,080）→ 新语义列入 BUY 100 股、目标金额 22,080、可执行、额外风险「一手超过名义仓位上限」；注：TA update 状态仅服务当前周期，as_of 08-18 无法在线重产，故用冻结证据离线重放；2026-08-19 数据下 06160 未通过趋势过滤（不在候选池，「如果没有其他排除项」的前提当日不成立）。三市场报告同批重产（run_date=2026-08-19，执行日 08-20）：首轮 r1（CN v14 / HK v12 / US v12）07:20 整批发布时三市场全部处于「策略累计回撤状态缺失，暂停新开仓」——新版本身份无回撤基准（验收前置当时因 allocation 对当日 stale 回退检查了旧身份 v10/v8 故显示 ready），属版本升级配套遗漏而非新语义缺陷；以 actor=codex-trend-signal-first 运行 `trend-drawdown-preflight`（allocation latest 指向 08-20 快照使版本解析为 v14/v12）自动补登记：CN v14 基准 987,912.14/HWM 1,005,774.543、HK v12 991,810.245/1,008,982.326、US v12 1,004,801.196/1,010,708.978（均 new_strategy_version 继承，entry_eligible_from 08-20；US 前置显示 entry_allowed=false 仅为美东仍在 08-19 的日期门，执行日 08-20 的报告不受影响）。随后重出 r2 全部发布（PASS、零下单、回撤 active、零 risk_skips）：**US 11 条候选全部列入买入名单**（TEAM 192 股可执行；PCOR/CTSH/IQV/MANH/TGT/ELF/A/NTRA/VRTX/TMO 共 10 条带「10 个持仓席位已满」提示、executable=False——旧语义下这 11 条全部被藏进排除项）、**CN 5 条**（苏州银行 4500 股可执行；红利ETF/上海银行/江苏银行/宁波银行 4 条席位提示）、HK 当日无趋势过滤候选（名单为空属正常）。allocation latest 指针最终恢复 08-20（凌晨预跑产出的 08-20 快照保留并生效）。
- #74 预测套利 N 腿固定方案的最坏部分成交损失证明（fill adversary）上线：新增 `prediction_partial_fill.py` 规范问题（版本化订单语义表 v1：Polymarket FOK=原子 {0,全成}、PredictIt=可部分成交 0..q lot、未知组合→整个证明 UNKNOWN；跨腿一律独立组合）+ CP-SAT 对手最大化（成本 f_i×ceil(max_cost/q_i)、赔付 floor、全整数）+ 独立 verify 复算，三态证明 `PARTIAL_FILL_SAFE`（闭合最优+复算一致+≤cap）/`PARTIAL_FILL_UNSAFE`（定点整数复算反例+成交向量与终态）/`UNKNOWN`（超时/语义未知/输入不完整一律 fail-closed）；`prediction_n_leg_validation.py` 回放与 live 两处硬编码 `partial_fill_proof=UNKNOWN` 决策（ponytail）替换为真实证明；live resolver 热路径同步执行（独立 1s 时限、按对手问题指纹缓存、超时不重试），证明与 UNSAFE 反例持久化新表 `partial_fill_proofs`（键=对手指纹，跨重启 read-before-solve）；读模型透出三态+安全上界；`PartialFillProofRecord` v1 schema（#52 预埋）原样启用。观察预期：observe 阶段 cap=0 且跨腿独立组合下最坏损失几乎必然>0，多数方案将如实显示 UNSAFE+上界，这是诚实建模而非故障（PredictIt 语义为保守超集建模，后续查证其 API 原子性后可升级语义表收紧域）。验证：新增 `tests/test_prediction_partial_fill.py` 14 例（差分断言：生产 SAFE⇒Oracle 穷举最坏≤cap 防 false-safe、UNSAFE⇒Oracle 复现同值反例、超时注入→UNKNOWN）；焦点六套件 207 passed；全量 6861 passed / 7 failed（trend_review 缺 git-ignored 本地 fixture 的环境性失败，主仓有 fixture 可过，与本改动无关）；端到端冒烟（CP 闭合 650000=Oracle 精确一致、verify 一致、payload 往返）；耗时 p50=7.4ms/p95=9.2ms（远低于 1s 线上时限，threads=1 后端未动）；reviewer 两轮——首轮 3 P3（lot 步进语义、语义表版本未进指纹、sqlite3.Error 未捕获）全部修复，复审 No findings。#63 页面 UI、#64 preflight、#75 RepairPlan 未动。
- #74 交付部署事故与热修（prediction-service 下线约 2 小时后恢复）：部署时连续 5 次 install 失败，报 `candidate_cleanup_not_proven`（真实原因被清理失败掩盖）。诊断（faulthandler 探针 + 信号追踪 + 数据实证）拆出两个与本仓代码无关的叠加根因：(1) 共享 `.venv` 的 editable 安装 8 月 3 日起指向已删除的 `.worktrees/keychain-secret-write/src`，任何新进程 `import open_trader` 失败——已修复为指回主仓 `src`（运行期环境修复，无代码提交）；(2) 2026-08-19 23:08:51 actor=system 自动批准在 catalog v2 连发 3 个 generation 放入 4 条共享 market `polymarket:0x38f9…` 的 IMPLIES 关系，`relation_generation_problem` 编译冲突抛 ValueError，`live_resolver.start()` 启动线程前直接调用 `_reconcile()` 使异常穿透 → 服务自该时刻起无法重启（旧进程靠 tick 级异常吞掉存活，部署重启引爆）。热修 `120716d8`：`start()` 内 reconcile 包 try/except + `logger.exception`，线程照常启动，tick 按 generation 变化自动重试自愈——符合 #52 规范「矛盾组件隔离而非拖垮服务」，其余 151 条 ACTIVE 关系的 N 腿求解与全部服务功能恢复；未动任何关系数据（该撤销哪条冲突关系属运营决策，已建后续工单）。验证：真实生产 catalog 副本上精确复现事故异常并在新代码下 `start()` 成功（真实库零写入）；热修分支 39 passed（含新增回归测试：fixture 触发同款 conflict ValueError、start() 不抛、日志、线程存活、stop 干净）；reviewer No findings。后续工单：激活闸门为何放进编译冲突的关系组合（自动批准路径疑点）、PredictIt 订单语义查证。交付部署：热修合入 23801b68 后 install 一次成功；prediction-service PID 24316 启动于 02:25:38、cwd=repo、healthz status=running/runtime_state=RUNNING/production_owner=true、git_sha=23801b68 与 main HEAD 一致、source_state=clean；日志实证热修生效（`startup reconcile failed` ×1 后服务照常 RUNNING，tick 每 ~10s 重试 reconcile 并记录 `tick failed`——自愈循环存活，目录数据修复前持续）；修复后的 venv 对新进程 import 正常。已推送（d66d70d2..23801b68）。
- 关系审批抽屉详情改为内联展开在被点击行正下方（用户反馈：详情渲染在整个列表最下方，要滚很久才能看到，体验差）。根因：原设计是"点开详情后隐藏列表、只显示详情"，但 `dashboard.css` 的 `.pm-relation-list { display: grid }` 覆盖了模板 `hidden` 属性的 UA `display: none`，列表没藏掉、详情又被拼在抽屉末尾。现改为手风琴式：详情 section 移入列表内、紧跟匹配行渲染（`relationDetailHtml()` 提取复用），列表与分页恒可见；被展开行高亮（accent 边框），再次点击该行或"收起详情"（原"返回列表"）即折叠不发请求；展开后 `scrollIntoView({block:"nearest"})` 定位。改动仅前端 `dashboard.js`/`dashboard.css` + dashboard-web 测试（+54/-24），后端 API、状态结构、审批动作逻辑零变化（`[data-relation-reason]`/`[data-relation-note]` 在新结构下仍唯一）。验证：聚焦 relation 11 passed、dashboard-web 全文件 380 passed、`node --check` 通过；复审（reviewer）逐项核过事件委托顺序（approve/reject 按钮自带 version-id 不会误触行折叠）、折叠比较的空值/类型边界、提取等价性、scrollIntoView 时机、轮询快照测试改动必要性，No findings。交付部署：合并 6a0eab97 后全栈重装对齐（prediction-service production、account-api production、account-sync、趋势三控制器+allocation、health、网关+legacy 栈；prediction-service 首次候选遇已知 runtime.lock 竞态瞬态由 KeepAlive 自愈，account-api 首次健康探测 90s 未确认亦为已知瞬态、直查 healthz 即 production/SHA 对齐）；生产冒烟经真实 Chromium（Playwright 对 8766）：点待批准第一行 → 详情内联展开于该行正下方（DOM 序 row→detail→后续行、detail 在列表 section 内）、列表与分页保持可见、行带 expanded 高亮、再点该行折叠，全程只读零审批操作。`make acceptance` 终验 **PASS**（第二次运行：首轮 FAIL 为 LIVE-03 Polymarket 账户预检读取瞬时 `sdk_error`——隔离探测公开端点全部 HTTP 200、geoblock allowed、复跑完整 preflight PASS 零 mutation，属外部间歇抖动，与本改动无关；第二次 63 场景全过含 LIVE-01~03 与 Playwright 浏览器流，dashboard 验收 errors 空）。全量套件 6850 passed / 3 skipped / 1 xpassed（#94 预存竞态备案）。验收后核对：网关 PID 50559 cwd=repo @6a0eab97 与 main HEAD 一致（部署即被验收 SHA，验收后无源码/数据变更）、review URL HTTP 200（0.9ms）、网关/legacy/account-api 三个 err.log 均 0 字节。评审 URL http://127.0.0.1:8766/（预测市场页，点"待批准"芯片开审批抽屉）。已推送（60b4e226..ad3893f5）。
- 修复关系校验轮询 SQLite 连接风暴（llm-provider-switch 2cb02b9d 回归，#95 交付次日起线上复发"关系审批抽屉列表挂起/空"）：`_poll_relation_validation` 对 ~900 活跃关系逐个 `cached_validation`，每关系每轮 1 次共享新键 + 每个 legacy 键各 1 次 `load_llm_cache`，而 `load_llm_cache` 每次调用经 `_read_connection` 新开并关闭 SQLite 连接（库 280MB）；`run_forever` 的 `wait_for` 1s 超时使重扫至少每秒一轮 → 每秒数千次连接开关，asyncio 线程 CPU ~100%，GIL 饿死 HTTP 请求线程（state/relations >30s）→ 网关 503 → 抽屉列表挂起；重启无效（1 分钟内复发）。faulthandler 全线程栈定位（唯一热线程即轮询→load_llm_cache）；附带纠正早前误判：legacy 探测并非失效——部署以来已真实迁移 681 条旧裁决（llm_cache 新增行含旧模型载荷即迁移签名），LLM 校验管线存活，事故唯一根因是连接风暴。修复三处（拷问定稿：启动迁移/每轮批量/2s 只罩重扫）：(1) store 新增 `load_llm_cache_entries(keys)`——strip/去重/空输入零连接、单连接内 900 键分块 `IN` 批量读、只返回命中；(2) `LlmRelationValidator`——`_cached_validation` 删除 legacy 探测循环（热路径只读共享新键），新增 `cached_validations(relations)`（一次批量取回、命中记 cache hit、无 legacy）与 `migrate_legacy_validations(relations)`（进程启动对全部活跃关系一次性迁移 legacy 裁决、幂等：新键已存在即跳过、含 env 模型键），新增 `relation_cache_key(relation)` 显式键约定（monitor 与 validator 的 prompt_version 不再隐式耦合）；(3) monitor——`run_forever` 装载 catalog 后恰一次启动迁移（异常安全不阻断启动），`_poll_relation_validation` 收割段不受 2s 最小重扫间隔节流（`RELATION_RESCAN_MIN_INTERVAL_SECONDS`），rescan 改批量 restore（恢复先于 retry_at 检查、恢复后刷新块、不可计算 cache_key 的关系跳过 restore 走原路径，语义与改前逐关系等价）。不动 title 翻译与 cross-venue 的同构 legacy 探测（栈证据不在热循环）。验证：聚焦 store/discovery/monitor 307 passed + discovery/monitor/arbitrage/store 357 passed（含新增批量恰 1 次调用零逐键、legacy 不再被热路径探测、迁移幂等/无效行跳过、节流罩重扫不罩收割、启动迁移恰一次/抛错不阻断、自定义 prompt_version 键约定等 19 个定向用例）；复审两轮——第一轮 3 发现（P2 启动迁移接线零测试、P3 键隐式约定、P3 无效行分支未覆盖）全部修复，第二轮 No findings。全量套件对照 main 无新增失败（31 个预存失败为 solver 子进程握手/缺 data 文件等环境问题，与本改动无关）。交付部署：合并 851af64c 后重装 prediction-service（两次候选失败均为已知瞬态：bootout 后 runtime.lock 竞态、启动瞬间 account 探测 unavailable——account-api 本身全程健康），PID 65994 运行至今；生产实测：真实浏览器抽屉满页 50 行 / 967 条 / 分页正常 / 零 console 错误（修前 0 行卡"正在加载"），/state 0.08-0.18s、/relations 0.26-0.53s（修前常态 503）；CPU 从 ~100% 降至稳态 ~60-72%（30s 窗实测 59-72%），未达最初 <10% 目标——剩余热点经 `sample` 定位为重扫内每关系 Decimal 幂/模年化运算（mpd_qpowmod/dec_hash）与 resolver/driver 周期性 catalog 全表重解析（pysqlite_cursor_iternext），已定为二期工单（Decimal 年化记忆化 + catalog 代数短路），用户裁定主要功能无碍先行收尾；偶发影响仍在（复验时捕获一次 console 503 与一次 3.18s /relations）。`make acceptance` 终验 **PASS**（第三次运行：首轮 FAIL 为网关/account/趋势控制器等仍在旧 SHA——全栈重装对齐 851af64c 解决；次轮 FAIL 为验收脚本误在 worktree 目录运行致期望 SHA/日志路径错位，非服务问题；第三轮 63 场景全过、全量 6850 passed / 3 skipped / 1 xfail#94、errors 空）。验收后核对：prediction-service PID 65994 cwd=repo @851af64c、网关 PID 94804 cwd=repo、review URL HTTP 200（1.2ms）、err.log 4.4KB 全为已知项（两次安装候选失败残留栈、#93 备案冷启动 predict 快照 warning 单条、一条修前即存在的 websockets 代理 StopIteration——监控重连兜住）。评审 URL http://127.0.0.1:8766/（预测市场页，点"待批准"芯片开审批抽屉）。已推送（b2f266ea..851af64c）。
- 美股趋势账户身份切换：老虎(Tiger) → 富途(Futu)（用户已完成券商调仓，美股趋势组合现位于富途真实账户；趋势纪律引擎/富途模拟盘零改动，非策略变更）。代码侧全部"美股趋势 = tiger"接线切为 futu：US 真实持仓来源改读富途共享账户快照（14 只股票 SNOW/RJF/REGN/PYPL/NUE/MMM/LPLA/LH/KO/GRMN/GPN/DGX/CRNX/ADP 按完整真实持仓纪律运行，富途期权仓列为账户例外可见不动作、港股持仓不入 US 视图，`REAL_HOLDING_TREND_EXCLUDED_SYMBOLS` 保持 `{"US.AGRZ"}`）；每日统计周期实盘成交读取由 `TigerActualFillClient` 换成新 `FutuActualFillClient`（真实环境、只取美股股票类，历史 ("actual","tiger") 数据保留为只读来源；实盘成交费用改用富途 `order_fee_query` 按单查询真实费用——OpenD 订单列表本身不返回费用字段，费用不可得时显式记录 `costs_complete=false` 与降级原因（如查询失败或券商仅月结的第三方费），不静默按零费处理，此类成交不计入净值/胜率统计）；状态目录 `trend_us_tiger` → `trend_us_futu`（data 与 reports 两处）；期权关注纳入 US 真实持仓行（`signal_snapshots.real_holdings` 持久化供日间 diff，`REAL_` 前缀 `source_action` 标识，行序在后并在合并中胜出）；通知身份"老虎"→"富途"；Dashboard：富途卡变"趋势/美股趋势交易"、老虎卡变"已调仓/现金管理"（仅账户视图，账户同步与 `tiger_account.py` 保留），验收断言翻转（断言老虎趋势身份不存在）。新增 `scripts/cutover_us_tiger_to_futu.py`（幂等可审计、`--dry-run` 模式：归档 7 月老富途状态目录防陈旧保护线复活、迁移保护线/真实保护线/观察事件/投递去重账本、从最后一份老虎报告重建 attention 基线防对比旧数据假变化；**脚本已写未执行**，由主代理在切割窗口执行并重启相关服务）。验证：grep 干净（src/scripts 无 `trend_us_tiger` 生产引用、market_trend/a_share_trend/dashboard/trend_api_stats 无 US 绑定 tiger 残留）、12 个受影响测试套件全部通过（含新增四类用例：期权关注真实持仓 diff、期权仓→账户例外、富途实盘成交客户端 mock OpenD、迁移脚本 tmp 目录 dry-run）、`market_paths('US')` 解析到 `data/trend_us_futu`/`reports/trend_us_futu`、迁移脚本 dry-run 全校验 PASS 零落盘。设计文档 `docs/superpowers/specs/2026-08-19-futu-us-trend-cutover-design.md`（声明替代 07-16 设计的账户定位，旧文档不改写）。已推送（5e9bc327..2bd780cc）。实际切割执行记录（2026-08-20 上午在主仓线上执行，效果已验证）：脚本执行时发现已提交的两步缺失、靠手工补齐——(1) 老虎时代 US 批次账本 `data/trend_review/ledgers/US/batches/`（23 个文件）使 US 控制器冷启动时 `_durable_report_cycles` 仍从市场键控的批次账本拿到老虎报告作为 durable 周期，这些周期过不了 futu 身份的 `_valid_report` 校验、全部判「未完成」，控制器试图从 2026-07-20 补产旧报告并对 Trend Animals 旧日期永久失败；已归档（移动不删除）至 `data/archive/trend_us_batches-tiger-era-20260820T090607/`；(2) 为切割前最后一个周期（as_of 2026-08-18 / execution 2026-08-19）写入 revision request（baseline 为空）+ `report_missing=True` 的 legacy cutover 记录，锚定该周期为已完成。两步已回填进 `scripts/cutover_us_tiger_to_futu.py`（幂等、`--dry-run` 不落盘、fail-closed、manifest 记录；锚定周期由批次账本+每日收盘账本推算「当前周期的前一个」，日期语义与线上一致）。修复后控制器正确生成首份富途身份报告 `reports/trend_us_futu/2026-08-19.json`（execution 08-20）：14 只真实持仓全部入纪律（12 只 HOLD 带初始保护线、LPLA/NUE 危险信号 SELL_ALL）、期权关注 28 行含 12 行底仓行、VIXY 期权列账户例外、投递去重防住重复飞书。全栈服务重装对齐 31a189a3（account-api 曾误装 shadow 版本已改回 production）。后续修复（2026-08-20）：富途账户视图 14 只真实持仓被误拆出 7 只（ADP、DGX、GPN、LPLA、MMM、RJF、SNOW）到「非趋势持仓」——根因是老虎时代买入证据只存在于遗留只读目录 `reports/trend_us_tiger`，新富途身份仅扫 `reports/trend_us_futu` 导致证据失联；修复为 (futu, US) 成员计算并扫当前目录与遗留目录（BUY 正式动作与自动轮换买入证据取并集），白名单不动，14 只全部回到「趋势持仓」分组。

## 2026-08-18

- #95 跟进：关系审批抽屉列表语句的市场标题改为完整显示——用户反馈 28 字符/侧 + "…" 截断后两个市场标题都被掐掉、无法判断关系内容；彻底删除 `_LIST_TITLE_LIMIT`/`_truncate_title()` 及 `list()` 的 `truncate_statement` 路径，列表与 detail 恒同全文语句，长标题靠行内既有 `overflow-wrap` 换行（版本号 `v-…` 中间省略保留，非阅读信息）。改动仅 `relation_catalog.py` + 测试（+6/-21），网关静态无变化。验证：聚焦 catalog/catalog-service/read-model/dashboard-web 441 passed；截断符号全仓 grep 零残留；复审（005d9ba7）无发现。交付部署：合并 22091d99，重装 prediction-service 后线上 `/relations?view=pending_approval` 实测行语句全文无省略号（另一会话期间又两次并行合并推进 main 至 597b229e 并随其重装 prediction-service，其余服务由本次对齐）；`make acceptance` 终验 **PASS**（6839 passed / 3 skipped / 1 xfail#94、全场景含 LIVE 与浏览器流、dashboard 验收 errors 空；本跟进共三次运行——第一次 1 条 test_frontend_gateway 路由排水用例在全量负载下 5s socket 超时闪失（隔离复跑 1 passed、整文件 43 passed、该文件近期零改动），第二次为并行会话推进 main 致 SHA 漂移，均非本改动缺陷）。验收后核对：全服务 @597b229e、网关 PID 39710 cwd=repo、review URL HTTP 200、err.log 仅 130 字节（#93 备案的服务冷启动单条 predict 快照 warning，degraded 无、不复发）、列表语句全文。评审 URL http://127.0.0.1:8766/（预测市场页）。未推送。
- 默认 LLM 引擎迁回 DeepSeek（deepseek-v4-flash）：`DEFAULT_PROVIDER` 由 zhipu 改为 deepseek，env 样例同步；迁移前真实 Key 冒烟通过（最小调用 8.6s；校验形状调用 21.9s 成功且 `_valid_structured_result` 通过、输出 3,240 token——历史上 5.5% 成功率的 DEEPSEEK_HTTP_ERROR 未复现）。看板一键切换与三引擎能力不变；上一批输出诊断/契约加固与 120s 智谱超时全部保留。受默认值变化影响的测试断言按新默认重排（显式 default_provider 用例不动；「空表 set 默认引擎为 no-op」的语义用 select 助手/角色互换保持原测试意图）。线上持久化选择行（当前为 zhipu）由部署后 POST 切至 deepseek。
- 排查 `ZHIPU_OUTPUT_INVALID` 并补可诊断与防复发：120s 超时部署后 83 次智谱调用 1 次输出无效（该次输出 3,269 token 远低于 16,384 上限、reasoning 2,521——排除截断与超时，是真实 schema 形状偏差；`json_object` 只保证合法 JSON 不保证 schema，Codex 旧路径另有 `--output-schema` 强制，HTTP 路径没有；三个引擎共用同一份校验 prompt 属设计使然）。旧代码在输出无效时丢弃原文、零痕迹，无法事后定位。处置三件：(1) 新增 `_structured_result_violation` / `_equivalence_result_violation` 粗分类器，两校验器 OUTPUT_INVALID 分支与标题翻译无效分支现在打 `llm_output_invalid`/`title_output_invalid` warning（含违规标签 + 原始输出前 1200/300 字符），下次复现即可精确定位偏差类型；(2) 两份校验 prompt 追加 OUTPUT CONTRACT（精确枚举顶层键与 market 键、schema_version 必须是 JSON 数字、reason_codes 只能用既有枚举、任何层级不得加键），prompt_version 不动以保 3,467 条共享缓存；(3) `_valid_structured_result`/`_valid_equivalence_result` 语义零改动，fail-closed 不变。另证：该账号限流严格（2 并发即全 429），线上 429 由监控重试兜底。
- 用真实数据定智谱校验超时：从生产 relation_state 抽 10 条真实关系载荷、以 180s 宽松上限重放 glm-5 校验（thinking on），7 次成功耗时 42.0/44.4/46.4/48.4/59.9/59.9/93.1 秒、0 次结构化无效——现行 60s 默认恰好掐掉长审计（线上 ZHIPU_TIMEOUT 失败主因），另 3/10 为快速连发触发的 ZHIPU_RATE_LIMITED（监控层数分钟后重试兜底，时间线证实会转成功，不在本次处理）。`validation_completers` 的智谱校验调用改用 `zhipu_validation_timeout()`：默认 **120s**（覆盖实测最慢 93.1s 并留 ~29% 余量），`OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS` 可覆盖、非法值回退 120；`zhipu_completion` 函数默认 60s 不变（标题翻译等轻调用实测秒级，维持 30s 档）。
- #95 修复统一 N_LEG 页关系审批完全不可用的回归：#57 统一页切换使审批抽屉 `relationReviewDrawer`（功能完整、接 `/relations/{id}/approve|reject|revoke`）与唯一入口按钮所在页头双双成为死代码，页面只剩 958 行 `B_IMPLIES_A · IMPLIES · deterministic_rule` 黑话只读列表（后端投影丢弃 endpoints、statement 即方向码、`review_rows()` 每次轮询全量序列化）。现为单一审批台架构：底部区块收敛为六态计数芯片概览（点击直达对应过滤视图）+ 页头"关系审核 N"徽章双入口同开抽屉；词汇统一——`catalog.list()` 接受六态键（`pending/approved_active/activation_blocked/history` 旧键保留为兼容别名，`activation_blocked` 收窄为六态语义、INCOMPLETE 行归入 `approved_model_incomplete`），六态判定下沉 `review_state()` 供 catalog 与 read model 共用，state 载荷 `relation_review` 变为计数聚合；方向码 statement 读时派生人类语句"B『前件』为 YES ⇒ A『后件』必须 YES"（列表每侧截断 28 字符、详情全文），行/详情透出 `direction_code` 小标签与前/后件角色标注；抽屉 tab 带计数徽章、50/页分页（复用 #92 的 limit/offset）。复审修复：P0 端点存储顺序（`_normalise_discovery` 按 contract_id 重排）会翻转派生语句的蕴含方向——ingest 持久化 `semantics.antecedent/consequent_contract_id` 与 `endpoints[i].role`，存量行走 `problem.constraint_model.relations` IMPLIES 约束序（`_sort_values` 保留原序）可靠回退、无角色且无约束证词时回显方向码绝不猜测；P2 5 秒轮询重渲染清空抽屉"决定原因/备注"表单——重渲染前后快照/回填；P3 列表行 venue 转义。删除死代码 `predictionPageHeader`；补 JS 已引用但 CSS 缺失的 `.pm-relation-badge.ok/.block` 色调与芯片/计数徽章/标签/分页等 mock 已批样式。批量操作与 intake 治理另立 #96、死代码清扫另立 #97。验证：聚焦 catalog/catalog-service/read-model/dashboard-web 441+90 passed；10 个新回归测试在修复前实现上实测全红（含 B/A 码 × 前件 contract_id 排序高/低对抗 fixture）；复审双向验证（还原旧 src 后回归全红）通过，第二轮复审仅余本 CHANGELOG 门槛。全量套件兜底零新增失败（对照 main：25 个预存失败为 solver-worker/runtime 基线、6 个为 worktree 缺未跟踪 data/trend_review 数据）。交付部署：合并 5adcf0c0 后全栈重装对齐（期间另一会话两次并行合并推进 main 至 5bb96aa9，prediction-service 随其重装，其余服务由本次对齐：网关/legacy/account-api（首次误装 shadow 模式致验收 FAIL，改 `--mode production` 后过）/account-sync/趋势三控制器+allocation 经各自安装脚本，controller_runtime 记录随之刷新）；生产冒烟经真实浏览器全链路：切页→页头徽章/六态芯片→抽屉分页列表→详情（派生语句方向 $315⇒$305 正确、前/后件角色正确）→填原因备注真实拒绝预选版本 v-59535a…02be（958→957，GET 确认 REJECTED，err.log 0 字节）。`make acceptance` 终验 **PASS**（6839 passed / 3 skipped / 1 xfail#94、全部场景含 LIVE-01~03 与 Playwright 浏览器流、dashboard 验收 errors 空；第五次运行——前四次 FAIL 分别为只重装了网关/预测服务未对齐全栈、手动 bootstrap 未走安装脚本致控制器无 runtime 记录、并行会话推进 main 致 SHA 漂移、account-api 误装 shadow 模式，均非本改动缺陷）。验收后核对：全服务 @5bb96aa9、网关 PID 56828 cwd=repo、review URL HTTP 200、relation_review 待批准 957 与芯片计数一致。评审 URL http://127.0.0.1:8766/（预测市场页）。未推送。
- #93 修复 state API 读路径同步做链上账户快照导致 `/api/prediction-arbitrage/state` 10-28s 慢查：predict 账户快照改为后台刷新 + 缓存读。原 `_fresh_predict_account_snapshot` 方法体改名 `_live_predict_account_snapshot`（执行/交易路径全部 16 个调用点机械改名、保持实时，语义零变化）；新增 `_refresh_predict_account_snapshot`（实时拉取后写入 RLock 保护的内存缓存，失败保留旧值、靠既有 ≤60s 年龄门自然过期）；`_fresh_predict_account_snapshot` 重写为纯缓存读（零网络 IO）。新组件 `PredictAccountSnapshotRefresher` 以 30s daemon 线程在生产与 shadow 两条启动路径预热/刷新缓存（启动先 tick，~10s 冷窗口内 predict venue 显示不可用，与拉取失败同形态 fail-closed；刷新失败记 warning 日志）。读模型 `_prediction_predict_account_snapshot` 删除 `_predict_trading.account_snapshot` 直连兜底（无有效缓存返回 `{}`），每次 state 请求从 2 次实时拉取（web3 RPC + `/v1/orders`、`/v1/positions` 分页）变为 2 次锁内字典读。验证：聚焦 execution/read-model/dashboard-web/service/runtime/n-leg/refresher/live-resolver/selection-driver 回归 947 passed（含新增"state 载荷构建期间 live `account_snapshot` 零调用"回归）；本地 shadow（拷贝生产 320MB 数据、同 keychain、worktree 代码）冷窗口请求 40ms、缓存预热后 predict 余额经缓存正常展示，60s 内 12 次采样延迟 0.03-0.13s（修前 10-28s），`/n-leg/mode` 2.8ms 无回归。生产已随本次跟进重装（`scripts/install_prediction_service_launchd.sh --mode production`）：PID 78608 @ `15af18a2`，runtime record ready，healthz 200；predict 余额 104.94 USDT 经缓存展示并跨 >60s 年龄门持续在（后台刷新存活的直接证明），err.log 0 字节零刷新告警；`/state` 稳态 30 次采样 avg 0.34s / max 1.02s（修前 10-28s），重启后预热窗口内偶发 8-16s 离群（同时刻 `/relations` 0.27s，与 predict 快照无关，属预热期其他后台负载，稳态 30 连测超 2s 为 0），`/relations` 0.26-1.3s、`/n-leg/mode` 9ms 无回归。
- 预测市场 LLM 语义校验改为「操作员一键可选的单一引擎」：新增 Codex / DeepSeek / 智谱 GLM 三个 provider（新模块 `llm_providers.py`，智谱走 OpenAI 兼容端点 `open.bigmodel.cn/api/paas/v4`，默认模型 glm-5，`ZHIPU_API_KEY` 在 `config/daily_premarket.env` 配置；Codex 仍走 CLI 登录，DeepSeek 沿用既有 Key）。严格模式：选谁用谁、失败即停止新下单并通知（不再自动降级到另一家），熔断（3 次失败/5 分钟）与预算（`max_llm_calls`）保留。选择持久化在 prediction store 新表 `llm_provider_selection`（含 control_events 审计），启动默认值 `OPEN_TRADER_PREDICTION_LLM_PROVIDER`（默认 zhipu）；服务新增 `GET/POST /api/prediction-arbitrage/llm-provider`；Dashboard「关联合约扫描」面板新增三选一控件（未配 Key 的引擎置灰），切换即时生效无需重启。校验缓存键改为按「关系+提示词版本」跨引擎共享：批准与拒绝结论在切换引擎后继续复用不重审，并自动把旧 Codex/DeepSeek 键（gpt-5.6-sol/deepseek-v4-flash/gpt-5.6-luna）迁移到共享键，存量 3,467 条校验与 550 条标题翻译零空窗。标题翻译同样跟随所选引擎。运营文案中性化：`CODEX_PENDING/CODEX_FAILED` → `LLM_PENDING/LLM_FAILED`，看板「Codex queue/Codex 认为可以/跨所弹窗 Codex 结论」等改为「LLM …」，降级通知改为引擎无关文案。每日盘前 advice 流程的 DeepSeek 用法不受影响。聚焦回归 llm_providers/relation/cross-venue/title/runtime/execution/monitor/store/dashboard 1181 passed；全量套件 6794 passed / 3 skipped（#91 时敏并发用例在全量负载下闪失一次，单独复跑通过，与本改动无关）。代码评审后补四处修复：llm-provider POST 缺 return（切换成功后 handler 线程 UnboundLocalError）、set_llm_provider 的 before 未按调用方默认解析（env 默认非 zhipu 时首次点选 zhipu 被静默吞掉）、monitor 重启恢复路径引用已改名的 cached_validation（死代码，恢复为公共单参方法）、失败路径 verdict 未固定 model（切换竞态下原因码与模型可能来自不同引擎）；每处均配了回归测试（对缺陷敏感，回退修复即红）。复审无新发现，相关聚焦 416+226+255 passed。未部署、未推送。部署后热修一处线上复现缺陷：glm-5 在 json_object 模式下标题翻译返回 `{"answer": …}` 而非约定的单键 `{"title_zh": …}`（旧 Codex CLI 靠 --output-schema 强制、API 不约束键名），提示词末句补上显式键名契约后真实调用复验通过；校验提示词经真实 glm-5 调用实测结构化形状全对（`_valid_structured_result` True），未改。另一处线上实测热修：state 读模型安全过滤器会剥掉键名含 "credential" 的字段，provider 快照的 `credentials` 键因此到不了看板（引擎置灰失效），改名为 `configured` 并补过滤器防回归测试；独立 llm-provider 端点的 `credentials_configured` 字段不受影响。另将 llm-provider 端点测试对 DEEPSEEK/ZHIPU Key 环境变量做封闭隔离（全量套件下早前用例会把真实 env 文件载入 os.environ，导致断言受环境泄漏影响），干净与伪造 Key 两种环境双跑 684 passed。交付部署：生产 prediction-service 重装至合并后 main（PID 22118 @ `58186e40`，err.log 0 字节），llm-provider 端点实测 selected=zhipu / 三引擎凭据齐备，热切换 POST 往返（zhipu→codex→zhipu）带审计记录；glm-5 真实调用验证翻译与校验提示词（共享缓存迁移后存量 3,467 条校验与 550 条翻译零重审）；随部署重装网关/账户/趋势控制器等全部 launchd 服务对齐 SHA。`make acceptance` 终验 PASS（全量 6813+ passed / 1 xfail#94、playwright 浏览器流、实时 API、账户刷新、进程/日志核验全过，评审 URL http://127.0.0.1:8766/）。线上智谱引擎运行中：切换后 58 次调用 39 成功（历史同期 Codex 0/1992、DeepSeek 205/3749），失败均按严格语义 fail-closed 并自动重试。未推送。
- #91 补齐 issue 要求的生产形态并发回归测试：同进程组合 #52 live resolver 线程、#87 monitor-selection driver 线程、relations review 读路径（`review_rows`/`pending_count`/`list`）与 monitor 形态的 auto-prepare 写入（`ingest_threshold_relation`+`approve`），全部共享同一个 SQLite v2 `RelationCatalog` facade；断言读路径零失败、写路径仅允许 busy "locked"、重开 catalog 后 12 个批准版本终态一致。验证：当前 main（thread-local 连接修复后）通过；将共享连接缺陷注回 `SqliteCatalogStore` 后同一测试立刻复现生产的 `cannot start a transaction within a transaction` 错误签名并失败。聚焦 catalog/v2/driver/live-resolver 回归 51 passed。随本次跟进用安装脚本重装生产 prediction-service（模板本就不含 `OPEN_TRADER_DISABLE_NLEG_BACKGROUND`），摘掉临时禁用、恢复 N-leg 后台并发：PID 62733 @ `174e71d9`，runtime record ready，selection 指标非空（driver 实例存在），~6 分钟并发 tick 后错误日志 0 行 0 嵌套事务错误（修复前历史累计 46,679 条），`/relations?view=pending` 0.43s。state API 10-28s 慢查清与 catalog 无关（请求线程同步做链上账户快照），另立 #93。
- #92 修复 #90 自动候选准备重启即重复 ingest 的问题：`prepare_relation_candidates` 不再用进程内存指纹集合，改为从 v2 catalog 派生"已准备"集合——组件全部成员 relation identity 已有 PENDING/APPROVED 版本即跳过（与 ingest 共用同一 canonicalize 路径），重启和源文本漂移都不再为同一真实组件新增 COMPLETE PENDING 版本；monitor 移除 `_prepared_candidate_fps`。生产审计：948 个 PENDING 分布在 948 个不同 identity，未发现同 identity 重复，修复前 `/relations?view=pending` 载荷 11.9MB/10.4s。新增 `prediction-arb catalog-dedup --dry-run/--apply [--limit]`：同 identity 多个 COMPLETE PENDING 保留 `catalog_v2_latest` 指向版本、其余 REJECTED（只翻状态不删行，meta 留 actor/git_sha/保留版本，单写事务批量，每轮有界可重跑）；relations 列表 API 行去掉 `model.problem`（detail 保留），支持 `limit`/`offset`（默认 500）并返回 `total`。不自动 approve/activate，不动 solver/执行/订单。聚焦 candidate/catalog/v2/sqlite/service/monitor/read-model/selection 回归 262 passed；本地生产部署与验证随本次 issue 跟进执行；未推送。
- #92 附带修复 acceptance 门禁的两处遗留违规（`test_production_consumers_do_not_open_prediction_sqlite_directly` 自 #71/#90 起红）：`relation-candidates --apply` 读 `relation_state` 改用 `mode=ro` 只读加载器（该命令只读 store、写全部走 catalog）；`nleg-validate --live-catalog` 的默认 SQLite 路径字面量移入 `relation_catalog.default_catalog_path`，cli 不再直接出现路径/构造器字符串。聚焦 health/candidates/catalog/service/nleg-validation 回归 91 passed。
- #91 并发测试经双提交基线统计证实为 main 既有 ~50% 闪失（b2f266ea 与 c07ad91a 闪失率相同、与本分支零文件交集），根因立项 #94（catalog 批准版本在并发 ingest + resolver 活动下从重开视图丢失），测试暂以非严格 xfail 隔离并修复假对象缺 `initial_verified_profit` 字段噪声，#94 修复后移除标记。

## 2026-08-17

- 修复预测套利预检失败只报笼统 `preflight_failed`、吞掉真实原因的问题：阈值/标准 pair 预检失败现在把底层 `error_code`（如 `order_amount_mismatch`）作为失败原因透传到飞书通知和执行记录，取不到时回退 `preflight_failed`，并补充常见错误码中文提示。仅改变失败原因展示，不改变下单、风控或经济逻辑。聚焦 execution 回归 252 passed；未部署、未推送。
- 修复阈值套利签名时 `max_spend` 未含手续费预算、导致 Polymarket 手续费市场 SDK 自动缩量、签名股数低于预期而被预检拒绝的问题：签名改为 `max_spend = leg.max_cost + maximum_fee + 1 分`（1 分仅越过 SDK 的 `<=` 边界，不写入订单），并覆盖一条腿零手续费、另一腿占满手续费预算的边界。真实机会重放预检由 `BLOCKED(order_amount_mismatch)` 变 `PASS`。聚焦 trading 回归 76 passed、execution 回归 250 passed；未部署、未推送。
- 测试稳定性：修复 prediction service HTTP 并发压力用例在全量测试下的提交顺序竞态（先确认 4 个 state 请求进入 handler 再提交 4 个 preview，并提高 client timeout）、跨所 auto_submit 残差事件用例在全量测试下的 reconcile 结果消费竞态（提供两份相同残差结果），以及 dashboard acceptance 中 account gateway 标记用例的 client timeout。仅测试改动，无运行时代码变化。
- #52 把 #82/#83/#84 接进生产 PredictionRuntime，形成第一条真正运行的实时 N_LEG 求解链路：`PredictionLiveResolver` 以 250ms daemon 线程推进目录 generation 并裁剪 #77 selected set；Polymarket 最新盘口归一化为 micro-USDC 成本切片后经 #54 的两个常驻 worker 求解，复用同一次 worker evidence 做 #50 verify 产出 `MarketSolution`，再用只读账户 seam 做最小资金检查产出 `ExecutionSolution`；结果只保存在内存，由 state API 透传给 #85 read model。selected set 的 discovery/refresh 单列为 #87；Predict、指标、ORDER_READY 和订单行为仍不在本 scope。聚焦 live-resolver/scheduler/market-solution/monitor-selection/runtime-graph 回归 66 passed；完整 runtime 套件仍保留基线已有的 5 个 sandbox 相关失败。未部署、未推送。
- #71 新增隔离的 N>=3 no-submit 垂直验证 harness 与 `prediction-arb nleg-validate` CLI：replay 路径用冻结快照走 #52/#50 seams 并用 #48 exact oracle 差分；live 路径只读导出 v2 catalog 后走 #82 graph、#83 scheduler、#52 resolver，隔离 data_dir/ownership lock，任何 submit/mutation 都在远端调用前拒绝。当前生产还没有 ACTIVE 的 N>=3 relation（单列 #88），因此 live 如实报告 BLOCKED、replay PASS，整体 BLOCKED；不产生订单或账本写入。聚焦验证/求解相关回归 54 passed；未部署、未推送。
- #87 把 #77 selected set 的刷新驱动接进 production `PredictionRuntime`：`PredictionMonitorSelectionDriver` 以 1s daemon 线程观察 relation catalog generation，在 #52 live resolver 空闲时按 FIFO 跑一次 #77 discovery/rank 并持久化 top-10；忙时保留有界 pending 队列（最多 32 个 generation key），失败保留 pending 并按 5s 退避重试，同时暴露 `selection_pending` / `selection_failures_consecutive` 到 state API，Dashboard N_LEG 指标条新增「Selection 积压 / 连续失败」卡片。不新增订单、通知、LLM、Predict 或 solution 持久化。聚焦 driver/live-resolver/monitor-selection/runtime-graph/dashboard 回归 57 passed；完整 runtime/service 套件仍受 sandbox 的进程/套接字限制影响。未部署、未推送。
- #89 停止 full relation scan 自动把两腿 INCOMPLETE IMPLIES 写入 v2 catalog；discovery 证据继续保存在 `relation_state` 和 scan 统计中，v2 catalog 改为只接受受控 candidate。新增 `relation-ingest` 入口，只允许同所同事件、模型完整、N>=3 且 `model.problem` 可解码验证的 relation；新增 `catalog-cleanup --dry-run/--apply`，将现存 PENDING 且无完整模型的候选标记为 REJECTED，保留版本历史不删除。聚焦 catalog/monitor/selection/validation/runtime-graph 回归 188 passed；未部署、未推送。
- #90 新增自动候选准备：full relation scan 后，把 `relation_state` 里的同事件 pairwise IMPLIES 按 contract 连通分量分组，筛选 3..10 contracts 且全部可编译 COMPLETE 的组件，每轮最多自动 ingest 1 个为 PENDING candidate；成功 fingerprint 进入集合避免重复写入，候选准备失败不影响 scan 健康。新增 `relation-candidates --dry-run/--apply` 诊断/执行。不自动 approve/activate。聚焦 candidate/relation-catalog/monitor/selection 回归 172 passed；未部署、未推送。
- 修复 `relation-candidates --dry-run` 对生产只读 SQLite 仍走写连接、导致 `readonly database` 的问题：dry-run 改用 `mode=ro` 只读读取 `relation_state`，apply 保持原写路径。聚焦 candidate/relation-catalog/monitor 回归 153 passed；未部署、未推送。
- 修复 `RelationCatalogV2` 读路径并发竞态：`PredictionLiveResolver` 与 selection driver 同时读共享 SQLite connection 时可能触发 `cannot start a transaction within a transaction`。改为 store 级 `RLock` 串行化 `_state`/`_read`，保持写语义和 schema 不变。聚焦 relation-catalog/monitor-selection/live-resolver/validation 回归 58 passed；未部署、未推送。
- 修复 `RelationCatalogV2` 在 `COMMIT` 失败时未回滚、留下悬挂事务并污染共享连接的问题：`begin_read` / `_state` / `commit_write` 的 COMMIT 失败现在都会 ROLLBACK 并重抛。聚焦 relation-catalog/v2/sqlite/monitor-selection/live-resolver 回归 87 passed；未部署、未推送。
- 为 #91 临时恢复 PredictionService/UI：`PredictionRuntime` 增加 `enable_n_leg_background`，`OPEN_TRADER_DISABLE_NLEG_BACKGROUND=1` 时跳过 `PredictionLiveResolver` 和 selection driver 启动；运行时仍可达 RUNNING。这是等待 #91 彻底修复的临时缓解，不改变求解/执行语义。聚焦 runtime/live-resolver/monitor-selection 回归 60 passed；未部署、未推送。
- #91 根因修复：`RelationCatalogV2` 不再共享一个跨线程 SQLite connection，改为 `threading.local()` 每线程连接与事务状态；移除 Python lock，写冲突交给 `BEGIN IMMEDIATE` + `busy_timeout`。并发读写测试通过，聚焦 catalog/v2/sqlite/monitor-selection/live-resolver 回归 90 passed；未部署、未推送。
- #91 追加修复：`SqliteCatalogStore._state()` 现在缓存当前线程加载的目录状态，写事务/失败后失效；避免 relation review 列表每行都全量重读 SQLite 的 O(N²) 问题。聚焦 catalog/v2/sqlite/monitor-selection/live-resolver 回归 91 passed；未部署、未推送。

## 2026-08-16

- #85 adds the N_LEG read-model projection and bounded metrics for the dashboard: serialized #84 MarketSolution/ExecutionSolution payloads are projected into per-opportunity market fields (minimum profit, maximum cost, capital release, structure/quote/verification fingerprints, per-leg quantity/price/cost) and execution fields (would-submit, ORDER_READY, machine-code reason, execution-solution fingerprint, projected and unsettled capital, per-leg plan). ORDER_READY binds to the execution-solution fingerprint and the n_leg scope capability: observe-only scope, missing/changed fingerprint, non-executable reason, or over-cap projected total fail closed without losing the market qualification; MANUAL + MANUAL_CANARY is order-ready. The dashboard renders the "下单计划 · would-submit" block and the bounded compile/solve/end-to-end/queue/timeout/stale/survival metric overview with Chinese labels; `prediction_state_payload` accepts optional `n_leg_solutions`/`n_leg_metrics` without changing existing calls. No scheduling/solver wiring, real orders, notifications, or mode changes were added. Focused read-model/metrics/dashboard render tests pass.
- 趋势报告就绪状态：控制器 `status.json` 新增 `waiting` 字段，报告未产出时说明缺什么（如「下一执行日 2026-08-17 报告未产出，正在补产：香港ETF 2026-08-13 → 2026-08-14」）；看板趋势报告头部新增全宽就绪横幅——就绪显示绿色「最新报告 · 可用于下一交易日 X」，未就绪显示琥珀色「报告未就绪」+ 缺口原因；后端报告投影新增 `ready_for_next_trading_day` 字段（执行日未过期即视为可给下一交易日使用）。不改变报告生成、执行、订单或通知行为。聚焦趋势报告/控制器/看板接受测试通过。
- #84 adds MarketSolution and ExecutionSolution for the selected N_LEG components: real order books are converted into executable cost slices, the #50 solver/verifier produces a QUALIFIED_VERIFIED MarketSolution (worst payout minus max executable cost), and an unchanged structure fingerprint re-verifies the fixed portfolio against current cost slices instead of a raw solve. At most one ExecutionSolution is derived under account and capital bounds, component-level NO_QUALIFIED_OPPORTUNITY consumes only an exactly-matching #50 negative proof, and timeout/crash map to UNKNOWN. No ORDER_READY/scope gating, partial-fill proof, LLM, or order behavior was added; execution outputs are observe-only (order_ready=false, partial_fill_proof=UNKNOWN).

- #83 adds the latest-snapshot-wins real-time scheduler for the selected N_LEG monitor components: one in-flight solve plus one latest pending snapshot per component, an economic snapshot fingerprint (price/depth/fee/availability only) that skips re-solving unchanged books, stale-result dropping, and fail-closed UNKNOWN on timeout/crash/busy with no immediate retry. The solve dispatch reuses the two resident #54 solver harnesses and a `build_solve_request` seam; snapshot timing (local received, exchange time, book sequence) gates ORDER_READY fail-closed. No MarketSolution qualification, proof, account/risk, or order behavior was added.

- #82 adds the catalog-layer runtime relation graph with deterministic Episode lineage: ACTIVE model-complete relations are partitioned into venue-qualified relation groups, changes are classified (PURE_UPDATE / SPLIT / MERGE / EXTEND / NEW / REMOVE), lineage ids are deterministic, and the mapping plus audit persist atomically with restart recovery. The relation catalog now exposes a monotonic generation number and whole-generation fingerprint via `generation_meta()`. No order-book reading, solving, LLM, opportunity, or order behavior was added.
- #81 把 Polymarket threshold 关系从 INCOMPLETE 确定性 enrich 成 COMPLETE v2 目录版本：仅当 resolution_source 与 end_date 都可确定时才产出 COMPLETE（否则保持 INCOMPLETE，不 fallback），同一 identity 的新 fingerprint 版本保持 PENDING、需重新人工批准/激活，旧 INCOMPLETE 历史版本不迁移不动；terminal_states 固定 NORMAL_YES/NORMAL_NO/VOID，VOID 赔付按 #80 保守口径为 $0，capital_release 取两个市场 end_date 的最大值，并把编译后的 N_LEG 结构问题（占位成本、真实账户/chain/成本留给 #52）以 model.problem 存入 catalog 供 #77 relation_generation_components 消费。不调 LLM、不读订单簿、不产生订单/通知/模式变化。聚焦 catalog/monitor-selection 测试通过。

- #80 将跨所 YES/NO N_LEG 影子模型的 `extreme_loss` 最坏口径从伪精确的 VOID=$0.50 / REFUND=$1.00 改为「至少一条腿可被平台酌情作废到 $0」（NORMAL + 至少一腿作废）的最坏情况；该值仍只读展示，不进入任何资格/门槛逻辑。原因是 Polymarket/Predict.fun 没有可靠的确定性 void/refund 公式，平台酌情作废属尾部风险。聚焦 shadow 测试 22 passed，真实 oracle smoke 返回 extreme_loss=-8.92。
- #77 selects and persists at most 10 non-overlapping N_LEG monitor components from the active, model-complete relation generation. Background discovery resolves one candidate component at a time only on idle capacity; `initial_verified_profit` comes from the verifier's worst-payout-minus-cost proof (solver OPTIMAL/NOT_PROVEN recorded separately), components rank by verified profit with a stable id tie-break, and the store keeps component identity, fixed admission score, selected portfolio and relation/terminal/portfolio fingerprints without in-flight solves, pending snapshots or quote freshness. No latest-snapshot-wins scheduling, real-time cost recompute, dedupe, Manual/AUTO change, preflight, funding reservation or order mutation was added; the output is the versioned selected-monitor set consumed by #52.

- 修复预测套利阈值对冲订单的飞书通知语义：去掉 confirm 阶段提前发出的假「已吃」；改为按腿发「预测套利单已提交」（订单号+限价+数量）和「预测套利单已吃」（成交数量+订单号），REST 对账证明到位后发整单「预测套利单结算」，提交前整单失败与提交后按腿被拒分别发「预测套利单提交失败」并带失败原因；同时修复「观察提醒」保底净利润显示 +$0.00 的问题（threshold_hedge 快照补 `minimum_profit`）。auto-eat 与手动两条路径统一发通知。聚焦 execution/monitor/notifications 回归通过（250 + 50）。

- #57 replaces the two YES/NO and LLM strategy tabs with a single unified N_LEG opportunity page: a one-row filter (discovery source / relation type / leg count / scope), venue-qualified opportunity cards with qualification status, order_ready reason and extreme risk, a MANUAL confirm action, a read-only capital-usage strip, and a six-state relation review list projected from the versioned relation catalog. The read model forward-projects current opportunities into N_LEG labels while keeping legacy `strategy_type` for history, and the page only exposes `MANUAL`/`AUTO` (OBSERVE_ONLY stays inside MANUAL). No production notification, mode, or order owner changed; the page only projects server facts. Focused read-model/dashboard/relation tests and Playwright block-order/mock-parity checks pass.

- 修复预测市场「观察提醒」在阈值套利市场（如 SPY）反复出现/消失时每 ~1 分钟重复发送飞书的问题：观察通知现在按 `market_id` 做 30 分钟成功送达冷却，同一市场 30 分钟内不再重复发送，发送失败仍按原逻辑重试，不影响 YES/NO 下单路径的既有 30 分钟冷却。聚焦 store/execution/monitor 回归通过（349 + 128）。

- Relax timing-sensitive test timeouts for the Prediction Service HTTP concurrency limit and the VIPR certificate subprocess checks so they no longer fail deterministically on a slower local machine. This is a test-only change; no runtime, service, or Dashboard behavior changed.

- #78 adds the v2 relation-catalog core module (`relation_catalog_v2.py`) as a standalone, not-yet-wired building block: venue-qualified relation identity, frozen approval fingerprints, a single cause ledger plus atomic generation snapshot, per-component consistency/budget, and a SQLite persistence seam. No runtime, service, Dashboard, monitor, or production behavior is changed; the existing v1 catalog remains the active catalog. Focused catalog and SQLite tests pass.

- Declare `ortools==9.15.6755` as a production dependency so the #54 Shadow CP-SAT solver is installed from `pyproject.toml`/`uv.lock` on future deploys instead of requiring a manual pip install; the lock now pins `protobuf==6.33.6` to satisfy or-tools.

- #56 upgrades the cross-venue YES/NO N_LEG Shadow adapter to model two venue-qualified contracts: per-venue account/chain/settlement identity frozen into the durable snapshot, per-leg capital release (Predict event_end_at / Polymarket settlement_at, latest-wins), 1:1 USDT:pUSD valuation, and a read-only `extreme_loss` over VOID/REFUND terminal states while the normal guarantee keeps the legacy same-basis qualification gates. No production notification, mode, or order owner changed, and no new tables or workers were added; deterministic VOID/REFUND trigger extraction was re-scoped in #80 to a display-only at-least-one-leg-void-to-$0 tail-risk warning, while SPLIT remains deferred to #52. Focused shadow/cross-venue/solver tests and a real CP-SAT smoke pass; also aligns the standard-binary shadow settlement asset with its valuation unit.

- #79 replaces the v1 relation catalog with the v2 core as the active catalog. Relation identity is venue-qualified, approval freezes a single fingerprint, mutations use version_id-only conflict detection, UNKNOWN derives from a single cause ledger, consistency/budget is per connected component, and current_generation returns full relation facts. The Prediction Service and Dashboard now read the v2 projection, the v1 catalog module is removed, and no Manual/AUTO, order, Solver, or notification behavior changed. Focused catalog, monitor, service, and Dashboard tests pass.

- #58 adds the versioned N_LEG `MANUAL/AUTO` backend contract: component-wise contract/policy/scope/safety versions, execution scope capabilities starting at `OBSERVE_ONLY`, unified four-gate qualification policy references, safety gates (global breaker, incident/batch) with loosen-to-MANUAL downgrades, and version-checked `/n-leg/{mode,config,scope}` mutations (409 on mismatch). It initializes `MANUAL`, reads legacy `observe_only` as `MANUAL`, leaves the old `/mode` and `/cross-auto/*` endpoints and `cross_auto_state` untouched, and does not take production control before #60. Focused mode/api-contract/store/shadow/read-model tests pass; no page and no #59 wiring.
## 2026-08-15

- #54 adds a production-owned, two-server generic solver owner and a no-submit
  N_LEG Shadow caller for already-qualified legacy YES/NO opportunities. Legacy
  notification/execution remains first and unchanged; standard and cross-venue
  monitors only enqueue an asynchronous canonical snapshot after their durable
  signal. This version intentionally does not depend on #59: the
  relation-catalog switch-authorization gate is deferred, and the Shadow
  compares directly against the durable legacy signal. Per-Episode
  latest-wins/dedupe/stale-discard results persist solely in existing
  `signals.payload`, and the existing Prediction page now shows a read-only
  compact comparison plus summary/history state. No order, notifier, database
  table, config, network endpoint, retry framework, or additional worker pool
  was added. Focused runtime/monitor/store/solver/render checks pass; sandbox
  HTTP/browser process launch remains unavailable, and final acceptance,
  deploy, merge, and push have not run.

- #59 adds the production-owned, versioned relation catalog for system-discovered market relations. The existing Prediction page now exposes a same-page review drawer with complete titles, venue, discovery/market/expiry dates and evidence; approval is version/fingerprint-bound, incomplete models cannot activate, invalidation is local to the affected component, and replacement publishes one atomic generation. No Shadow intake, manual creation, auto-approval, Solver/opportunity, quote, monitoring, order, notification, or new page was added. Focused catalog/Service/Runtime/monitor tests and the Prediction Dashboard browser review flow pass; final acceptance remains the release gate.

## 2026-08-14

- #53 adds the durable, no-submit N-leg execution boundary: strict cumulative
  receipt and partial-fill-proof contracts, an isolated N-leg SQLite schema,
  one active batch/retained lineage claim, incident gate, conservative capital
  occupancy, and an immutable manual-only RepairPlan. No runtime, network,
  Adapter, order, CLI, HTTP, Dashboard, notification, or production simulation
  behavior was added. Focused N-leg/Store checks pass.

- #51 round-six integration makes the existing Predict REST/WS accepted book retain its validated YES/NO outcome token IDs, so the public executable resolver can bind both Predict market and order token identity. Market tamper coverage now rehashes the full current artifact payload before deep validation. No watcher, cache, persistence, order, or Dashboard behavior was added.

- #51 round-five hardening converts venue price units into the component valuation scale before solving while retaining protected venue limits for #74, returns canonical decoded prior artifacts, binds Predict market plus outcome-token identity, and retains original component policy evidence to authenticate old relation identity. Terminal settlement/rule semantic changes now invalidate a prior proof, and the public release gate covers exactly 30 days plus one second. No watcher, cache, persistence, order, or Dashboard behavior was added.

- #51 round-four hardening binds USD qualification scale into sorted component policy identity, distinguishes an authenticated prior cache miss from a forged prior, and rejects mutable/malformed order-book ask containers before conversion. Receipt-only refresh still reuses the fixed market; depth and verified policy changes solve again. Public resolver boundaries now exercise real #50 BruteForce solve/verify at equality and one-unit-below for the four admission gates. No watcher, cache, persistence, order, or Dashboard behavior was added.

- #51 round-three hardening normalizes every verified book once into immutable quote evidence used by both conservative solver costs and #74 execution legs. Component identity now binds the original relation model and cost policy; Market/Execution payload decoders require their current component/book or market/account sources and recompute all retained evidence. Per-leg protected prices are now venue limit prices with explicit scale, rather than lot costs. Focused resolver/#50 and worker checks cover source tampering, source-free decoder rejection, receipt reuse, depth invalidation, account binding, and non-unit lot conversion. No watcher, cache, persistence, order, or Dashboard behavior was added.

- #51 round-two hardening makes prior market reuse source-proof-bound instead of self-hash-bound, binds each component action to its expected venue/native book and verified cost policy, and freezes per-leg #74 handoff evidence. Account balance containers now fail closed before iteration; shallow books exclude only that action so remaining connected support reaches #50; fixed-plan funding caps are checked over the public two-leg total. Resolver-focused checks cover Predict/Polymarket binding, strict codecs, source tampering, account shape, depth, and aggregate caps. No watcher, cache, persistence, order, or Dashboard behavior was added.

- #51 review fixes bind every market/execution quantitative field into its artifact fingerprint, add caller-supplied fixed-market re-funding without an internal cache, enforce the per-trade cap over the total plan, fail malformed accounts closed, and require nonzero tick plus verified fee-rule identity. Canonical #50 now has exact `RETURN_ON_COST_PPM` qualification so the 1% gate divides by conservative cost, not payout. Predict books require both source and receipt freshness. Focused resolver/N-leg/oracle/solver/verified checks pass; no watcher, persistence, order, or Dashboard behavior was added.

- #51 adds a one-shot, immutable N-leg executable-cost resolver. It converts only visible BUY asks from existing Polymarket/Predict book types into protected integer cost slices, sends the complete connected component through existing #50 solve/verify, retains a qualified market proof on funding failure, and emits only a non-order-ready `PARTIAL_FILL_PROOF_REQUIRED` handoff. No watcher, cache, persistence, mode, notification, Dashboard, order, or repair behavior was added. Focused resolver/N-leg/oracle/solver/verified/worker checks pass; reused #49 macOS CP-SAT/OR-Tools 9.15.6755 reports backend self-check `OPTIMAL` and a real public resolve→verify returns `EXECUTION_SOLUTION`/`QUALIFIED_VERIFIED`.

- #50 worker replay keeps legacy handshake-v1 `version` semantics and adds a strict handshake-v2 `solver_version` field for native proof evidence; old v1 workers remain version-unavailable rather than guessed. Direct solve no longer accepts worker-only request IDs. Host WorkerHarness, benchmark, and isolated CP-SAT bridge checks passed.

- #50 proof replay now has only the public `solve`/`verify` seams: `verify` maps canonical semantic drift to `UNKNOWN` while strict codecs still reject it; structural model identity is separated from executable quote costs, and worker evidence retains the native solver version. Verified with host-visible WorkerHarness and isolated CP-SAT positive/negative paths; no database, network, order, or notification change.

- #50 proof review hardening: terminal atom rule version must match its state-set rule; quote fingerprints are recalculated from canonical executable costs; replay decoding binds model/portfolio/quote/generation to its supplied source evidence. Component `NO_QUALIFIED_OPPORTUNITY` is now emitted only by a completed bounded exact Oracle proof; CP-SAT no-candidate remains `UNKNOWN`. CP-SAT `FEASIBLE` admission evidence no longer claims native `OPTIMAL`.

- #50 新增纯序列化 `SOLVER_VERIFIED` 证明入口：CP-SAT `FEASIBLE` 候选可持久化为带模型、组合、行情和 generation 指纹的 evidence；独立 verifier 仅以该 canonical input 和固定组合，经既有精确 Oracle 重算最坏结算、成本、资本释放与资格。固定候选合格才为 `QUALIFIED_VERIFIED`，不合格仅为候选级 `NOT_QUALIFIED`；组件级 `NO_QUALIFIED_OPPORTUNITY` 仅来自完整 exact Oracle，CP-SAT 无候选或不完整路径为 `UNKNOWN`。复用既有 WorkerHarness serialized I/O；无数据库、网络、订单或通知改动。验证：proof/N_LEG/Oracle/solver 回归通过；本地 `/private/tmp` WorkerHarness 清理证明受限，单独记录为 `CLEANUP_UNPROVEN`。

- 趋势复盘基准现以独立的显著完整交易胜率和统一五项指标计分板呈现，桌面端分列策略表现/市场基准，移动端逐行市场基准；无计算或执行变更。验证：聚焦 `trend_review` 测试 `3 + 36 passed`；完整 Dashboard 三文件套件在本地权限下为 `1006 passed, 1 warning in 48.09s`。未运行 `make acceptance`、部署、合并或推送。

- #49 标准化 solver adapters/worker benchmark：先通过稳定性硬门槛，再做正套利输出速度实验；在当前 macOS/current-corpus 边界内选择 OR-Tools CP-SAT。Linux cleanup proof 未完成，不构成正式跨平台全量选择；Issue 已关闭。

- 趋势复盘“策略与市场基准”现分别显示纪律模拟和实际执行的完整交易胜率：只统计既有归因、成本完整的完整闭环，成本后 `net_pnl > 0` 才计胜，持平仍计入分母；零闭环显示“数据不足”，来源不可用不伪装为 `0%`。投影已升至 v5，Controller 会将 v4 视为旧投影并重建。验证：6 个受影响测试文件 `1539 passed, 1 warning`；只读真实三市场投影为 CN 模拟 `2/11`、HK 模拟 `1/6`、US 模拟 `2/8`，三市场实际执行均 `0/0`。未运行 `make acceptance`、部署、合并或推送。

- #46 final corrections make the independent Prediction Service health contract
  fail closed unless it is the production `prediction_service` owner with clean
  source/cwd/SHA/PID facts; `prediction-arb status` now validates `/healthz` for
  production `8769` and Gateway identity for `8766` without a Dashboard process
  scan. The obsolete Legacy-vs-Shadow validator command and its dedicated
  module/tests were removed, and setup/runbook docs now install and verify the
  sole `8769` owner. Focused health/CLI/source metadata tests pass; no live
  service, launchd, route/data, merge, deploy, or acceptance run was performed.
  Account outage acceptance now waits boundedly for the launchd label and 8768
  listener to disappear before its first outage probe; the deterministic
  regression passes and now validates the cheap Gateway health contract instead
  of recomputing slow Prediction state during the outage check.

## 2026-08-13

- #46 removes retired Prediction owner/config flags from Dashboard launchd
  templates and installer. Legacy remains non-Prediction on 8767 while
  Prediction Service owns 8769; fresh stack bootstrap now seeds the Service
  route, and active runbooks describe fail-closed/manual non-Prediction
  recovery. Current-parser, launchd, shell/plist, and diff checks pass. No
  live launchd, service, route/data, or acceptance run was performed.

- Prediction Dashboard state refreshes now use a signal-history generation to
  preserve independently-polled signals only when that history advances during
  the state request. Closed signals keep live profit hidden and operations
  unavailable without blocking newer signals from a subsequently started state
  request. Both deterministic request orderings, focused polling/static checks,
  and all 35 Prediction Market browser cases pass; no live command, deployment,
  restart, or acceptance run was performed.

- Completed #45's one-time Prediction owner cutover command, Account proof
  helper, dedicated 115-case cutover suite, and Legacy rollback runbook have
  been retired. The production route remains Service-owned and Legacy rollback
  is unsupported. Retained Prediction Service, Gateway, and live no-submit
  registry checks pass; final `make acceptance` and exact-SHA deployment remain
  the closure gate.

- Dashboard launchd reinstall now makes its initial label observation and
  continues through the configured elapsed wait bound before refusing a still
  loaded job, preventing a one-second wait from bootstrapping before delayed
  removal is proven. Focused launchd checks pass; no live command, deployment,
  restart, or acceptance run was performed.

- Prediction shadow-validation tests now isolate their launchd-label probe, so
  a running local Prediction Service cannot change fake acceptance outcomes.
  The full shadow-validation file and focused notifier launchd checks pass; no
  production behavior, live service, deployment, or acceptance run changed.

- Prediction dashboard state polling now skips an overlapping slow request and
  reuses the initial signal-history request, preventing one browser tab from
  multiplying service reads during a slow refresh. Focused dashboard checks
  pass; no live command, deployment, restart, or acceptance run was performed.

- Prediction Service production launchd now supplies the existing daily
  notifier configuration to its runtime, matching Legacy notification
  injection; shadow remains unconfigured/read-only. Focused service and
  launchd rendering checks pass; no live command, deployment, or acceptance
  run was performed.

- #45 aligns Gateway public-state readiness proof with the production
  read-model contract (`readiness.ready == true`), rejecting invented
  status-only payloads. Focused readiness, happy cutover, and rollback checks
  pass; no live command, deployment, or acceptance run was performed.

- #45 marks post-cutover lock-holder evidence unavailable when its capture
  fails, instead of treating an empty holder list as verified availability.
  Focused malformed-after evidence check passes; no live command, deployment,
  or acceptance run was performed.

- #45 validates joined macOS runtime-lock output at the capture boundary,
  canonicalizing `p<PID>` holders while accepting only `f<FD>` ancillary
  records. Unknown or PID-less output fails closed before route mutation and
  after maintenance writes truthful failed evidence. Focused lock and
  maintenance-failure checks pass; no live command, deployment, or acceptance
  run was performed.

- #45 live-preflight follow-up uses joined `lsof -Fp` runtime-lock probes so
  macOS file-descriptor records cannot invalidate owner evidence, and the
  Dashboard stack installer now waits up to its configured bounded timeout for
  delayed Legacy shutdown. Focused cutover/install checks pass; no live
  command, deployment, or acceptance run was performed.

- #45 runtime-lock evidence now accepts only the documented macOS `p<PID>` plus
  `f<FD>` records from joined `lsof -Fp`, retaining fail-closed rejection of
  unknown fields. Focused before/after evidence and maintenance-failure checks
  pass; no live command, deployment, or acceptance run was performed.

- #45 round-7 focused closeouts require strict Account controller/API argv and
  health proof at capture time while validating persisted evidence against the
  canonical stored contract, so later plist edits cannot erase truthful before
  observations. Historical Account heartbeats remain valid for repeat checks;
  current preflight/after captures stay fresh and unchanged. Focused repeat,
  plist-drift, and strict Account checks pass; reviewer/affected-full gates and
  live cutover remain pending. No deployment, restart, 8769 install, or `make
  acceptance` ran.

## 2026-08-12

- #45 round-5 fixes prove the existing Account release at process level: real
  controller/API launchd argv, controller status PID/cwd/SHA/fresh heartbeat,
  API production health and 8768 ownership, with exact before/after identity
  preservation and no Account restart. Account may remain on an older SHA than
  the accepted Prediction release. Failed evidence now preserves captured
  before observations while representing unavailable after observations as
  null. Focused round-5 checks pass: Account old-release (1), Account identity
  preflight (10), partial failed evidence (1), happy/evidence (4), key
  service/rollback selectors (13), and malformed/repeat (4). `bash -n` and
  `git diff --check` pass; affected/full gates remain pending reviewer
  recheck. Live cutover, deployment, restart, 8769 install, and `make
  acceptance` have not run.

- #45 round-4 reviewer fixes add the real split Account topology (sync
  controller plus API/8768 health), preserve `ready`/`failed`/`stopped`
  runtime-record semantics for rollback and repeat, require maintenance
  failure evidence after post-maintenance Legacy inspection errors, and keep
  only the documented heartbeat/time fields volatile in direct/public parity.
  Focused checks on this worktree SHA pass: Account evidence (1), stopped
  rollback/repeat/cutover (1), maintenance/heartbeat/preview contract (4),
  Account preflight (5), evidence/repeat/secrets (7), and service/rollback
  failure matrices (28). Shell syntax and diff checks pass. The historical
  `a2adf509` affected/full results (671/5,884) remain historical; final gates
  for this new SHA are pending reviewer recheck. Live cutover, deployment,
  restart, 8769 install, and `make acceptance` have not run.

- #45 round-3 review fixes continue the fail-closed integrated bootstrap and
  maintenance rollback with one evidence validator, observed pre-#45 runtime
  identity, Account preservation checks, direct/public no-submit parity, and
  private temporary-file cleanup. Focused checks on this SHA pass: 19 service
  failure cases, 5 malformed/repeat evidence cases, and the CAS race case three
  times; the full cutover-file attempt reached 46 passes before its cold-start
  race observation timed out, so no final-file PASS is claimed. The prior
  implementation SHA `a2adf509` historically passed the affected 671-case and
  full 5,884-case gates; final affected/full gates for this new SHA remain
  pending reviewer recheck. Live cutover, deployment, restart, 8769 install,
  and `make acceptance` have not run.

- #45 freezes Prediction Gateway `legacy`, `service`, and `maintenance` route
  modes with in-flight drain, adds Legacy owner-off launchd rendering, and
  records the bounded cutover/rollback script. Tasks 1–4 provide frozen
  Legacy/Service parity and durable idempotency proof; the merge-main SHA is
  `97c2766ce7aaf1f50efe8a2226d3d89209ded3e6`. Shell/plist/diff gates passed
  and the exact full branch gate passed `5870 passed, 1 warning in 917.38s`.
  Compatibility bootstrap, live cutover, deployment/restart, 8769 install, and
  `make acceptance` have not run.

- 补录 Tiger 美股历史证据缺口 XLV、PYPL 与 Phillips 港股 HK.06823
  （实时账户名称 HKT-SS）：仅改变 Account 与 Trend Report 两处真实持仓的趋势归类；
  归类优先使用规范化 Futu 标识，券商别名不覆盖已确认身份；交易、下单、模拟持仓和
  历史报告内容不变；聚焦归类、fail-closed 与两处 DOM 回归通过。

- Account 与 Trend Report 两处实盘持仓现按历史正式买入计划，在 A 股、港股和美股拆分为
  “趋势持仓”和“非趋势持仓”；Tiger 美股历史证据缺口 AMZN、CRNX、GRMN、KO、LH、NUE、
  REGN 通过受源代码控制的 allowlist 补录；验收在 Account 原子发布瞬间遇到可重试
  503 时交由既有重试边界处理；模拟持仓与交易行为不变；已通过聚焦 Dashboard 回归，
  最终 `make acceptance` PASS，并按验收同一 SHA 部署。

- #48 冻结 solver-independent、版本化的 N_LEG 模型与有界精确 Oracle 语料：Admission、
  Optimization 与显式 Raw diagnostic 分离，确定性 support proof 和穷尽负证明均有可回放
  的请求/结果指纹；net margin 以最低 payout 计算，annualization 以向上取整的 24 小时天数
  （至少一天）计算。canonical action 明确携带 venue/account/chain 与 quantity bounds，
  Oracle enumeration 和 direct portfolio evaluation 均拒绝范围外数量；
  不完整终局输入和不支持版本均 fail-closed，正/负证据共用版本化 `PayoutProof`，指纹与输入
  顺序无关。最终修复 identity-only proof 合并、signed-64 派生运算溢出和亚秒 release delay
  向下取整，并加固五个边界：正 margin 门槛拒绝非正 payout、决策预算在惰性数量枚举前用
  signed-64 算术计数、含零数量的重复 action ID 按任意顺序拒绝、缺失或空白 terminal rule
  identity 统一返回 terminal-data `UNKNOWN`、负证明 wire decoder 拒绝重复 rejection ID。
  已验证 N_LEG 聚焦套件 `172 passed in 0.33s`、Prediction arithmetic 加 N_LEG 回归
  `223 passed in 2.01s`、Admission/Optimization/两类 diagnostic 直接回放
  `6 passed, 71 deselected in 0.05s`；16-case 语料未改且 SHA-256 仍为
  `a4680fb2c66dedac9e85db9cd06d0872882ca69b09fba6d9f338d0b97243ecc7`，并通过
  `compileall` 与 `git diff --check`。项目 venv 全库实际运行结果为
  `205 failed, 5482 passed, 3 skipped, 1 warning in 194.53s`，不是 green gate；失败与 #48
  无关，主要为 legacy fixture 缺失和 sandbox 禁止 localhost socket bind/browser process。
  未改 Prediction runtime、Dashboard、solver dependency 或 order path。

- 将 Open Trader 以 Apache License 2.0 正式开放许可，并在 README 与 Python 包元数据中
  发布相同的 SPDX 标识；运行代码、交易流程、后台服务与数据均未改变。已核对许可证官方
  原文、包元数据解析与 Git 差异；本次不涉及行为或 Dashboard acceptance。

- 完成通用 N_LEG Prediction 套利设计与开发 Ticket 契约收敛：热路径接受任一已验证合格
  连通组合而不等待全局最优，组件级负证明仅接受预算内完整 Oracle 或可独立检查证书；
  Entry/Repair 部分成交风险、OBSERVE_ONLY、Episode 重武装、scope Canary 与一次切换边界
  均 fail-closed。已完成数学/架构双重审查，并逐项回读验证 GitHub Issues #48–#52、
  #57–#58、#62–#63、#69–#70、#73 正文；本次仅更新设计与 Ticket，不改变生产行为。

## 2026-08-11

- Prediction Service release (#44): clean local Git checkouts can now be installed,
  upgraded, stopped, or compatibly rolled back as the single managed 8769 owner.
  Production startup checks the persisted minimum reader generation under the
  owner lock before writable Store/client/thread construction, and launchd
  transitions retain exact ready/failed/stopped evidence for the later #45 cutover.
  Shell/plist checks, the isolated install/upgrade/rollback/stop workflow, and
  relevant regressions passed; the final full branch gate passed 5,585 tests.

- #34 为独立 Prediction Service 增加固定 8 请求的全局 HTTP 准入边界，并对相同
  history 查询做 1 秒进程内 single-flight；过载在创建 handler 线程前返回带
  `Retry-After: 1` 的明确 503，state/health/写操作不缓存，既有鉴权、历史排序和
  mutation 语义不变。使用生产 SQLite 的只读一致性副本连续验证 30 分钟：1,201 轮、
  9,656 请求、最大延迟 2.882 秒、峰值 active 8、最终 active 0、零交易调用；最终
  5,518 项全库测试通过。此票未部署、未改 Gateway/Dashboard/launchd 或生产数据。

- #43 在独立 Prediction Service 的 production owner 上开放 Preview、人工确认及 LLM
  自动执行 observer：8769 复用既有 `PredictionExecutionService` 的实时校验、幂等
  execution lock、资金预留、回执与启动对账，不另建交易状态机；mutation 响应沿用 Legacy
  安全投影，删除内部 intent 并遮罩钱包。YES/NO、LLM 关系及跨所 Preview 均通过隔离临时
  SQLite/fake venue 的真实 HTTP 工作流，Shadow 仍在读取 body 或调用下游前拒绝所有写入。
  此票未部署、未改 Gateway/Legacy/UI/launchd、未触发真实订单；直接幂等确认只产生一个
  execution 和一个假下单批次，相关执行与 monitor 回归 622 项通过。

- #42 为独立 Prediction Service 增加 production owner 与四项控制操作（模式切换、
  熔断重置、Predict allowance 清理、跨所自动暂停）：仅在持有唯一 runtime lock、
  安全策略已登记且启动对账为 RUNNING 后才绑定端口；控制请求沿用 loopback、
  Host/Origin、session/CSRF、严格 JSON 与 1 MiB 限制，并将幂等结果和安全降级写入
  SQLite 审计。此票未部署、未改 Gateway/Legacy/UI/launchd；已通过临时数据/fake venue
  直接工作流、410 项相关回归及跨所 monitor 兼容检查，退出后无残留 8769 listener。

- #41 新增独立的只读 Shadow Prediction Service（`127.0.0.1:8769`）及 launchd
  安装、重启、卸载和有界双路 parity 验证；Shadow 使用独立 SQLite、禁止网络提交，并在
  任何写入尝试或语义差异时 fail-closed。最终 live 验证同市场与跨所 Codex 均为 3/3、
  三次关系扫描完成、无 DeepSeek/只读违规/语义差异，重启后 PID/SHA/监听一致，随后
  launchd label、plist 与 8769 均已清理。

## 2026-08-10

- #40 将 Legacy Dashboard 内的 Prediction 资源组装收拢为一个按数据目录持有
  `runtime.lock` 的 `PredictionRuntime`，固定启动/关闭顺序并接入 SIGTERM 优雅关闭；核心
  初始化、重复 owner 和对账未就绪（异常或 locked 返回）均 fail-closed。此阶段不启动 8769、不改 Gateway、API、
  SQLite、策略或订单语义。已验证 Prediction/执行聚焦套件、Dashboard 全量回归及隔离
  Legacy 启停工作流。

- #39 冻结 Prediction API 与生产基线：新增可执行 v1 契约和真实 HTTP golden tests，
  覆盖 state/history、6 条 mutation、关键字段、会话/CSRF、严格输入与 400/403；明确
  `YES_NO`、`LLM_RELATION`、`N_LEG` 的独立模式、共享安全标准、全局熔断，以及目标
  Prediction Service 的 liveness/readiness、单源降级和 503 语义。记录 #27–#38 在
  main、分支和生产运行时的事实；本票不改路由、进程归属、SQLite、模式或订单行为。

- 普通 YES/NO WebSocket 只保留当前执行规则允许的免手续费、非明确 neg-risk 市场；Top 20 展示与五分钟 REST 诊断保持不变，关系和跨所 token 仍独立保留。看板现显示标准层实际成功安装的实时 Token；畸形或全量解析失败导致的空池保留上一订阅并 fail-closed。验证：标准监控与 Dashboard 聚焦测试 503 通过、fixture-aware 完整 Python 套件 5340 通过及 public monitor diagnostic 均通过。

- 修复三市场 Controller 仍把 v3 趋势复盘投影当作当前版本、导致 v4 投影被重复捕获或旧文件不升级的问题；现与 Dashboard 和投影生成器统一要求 v4，控制器重启后会沿既有收盘恢复流程重建旧投影。验证：控制器回归通过。

- 趋势复盘改为单一共享标尺，同时展示纪律模拟、实际执行、同期市场、市场 1 年与市场
  5 年；A 股固定对比中证 500（`SH.000905`）、港股固定对比恒生指数
  （`HK.800000`）、美股固定对比 SPY（`US.SPY`）。长期基准由现有三市场 Controller
  每月独立刷新一次，报告、交易统计和基准任一失败均不阻断另外两项；不足一年的 Calmar
  与 Sharpe 明确显示“观察期不足”，5 年收益按 CAGR 展示。真实 OpenD 刷新已验证三市场
  2026-08 快照、月内重复调用不改哈希，以及 v4 投影的批准身份和 1 年/5 年数值。

- 同场阈值关系 WebSocket 监控改为 APR 感知池：60 秒完整 REST 扫描保持不变，实时订阅保留全部正常年化达标关系及年化门槛下最接近的预热关系；关系层只订阅两条对冲买腿，联合 token 集合未变化时不重连。扫描失败或年化达标关系异常超限时保留上一成功订阅池并 fail-closed。验证：监控回归、真实候选进程与最终 Dashboard acceptance 均已通过。

## 2026-08-09

- 修复趋势复盘把 Kelly 策略版本边界误用于连续日终净值的问题：未满 30 笔仍显示已有模拟盘与同期市场绩效，30 笔门槛只控制 Kelly 启用；实盘缺少日终净值时继续明确不可用。
- 跨所自动下单改由 SQLite `configured_mode` 与 `armed` 持久状态唯一授权：模式/arm 仅可由本机 CLI 修改，迁移或损坏状态一律 fail-closed 为 observe-only，安装器已拒绝退役的 `--cross-execution-mode` 覆盖项。验证范围包括存储/监控/看板/安装器回归、完整 pytest、干跑不改状态及最终 Dashboard acceptance；部署不会自动 arm。
- 三市场趋势报告降低候选筛选成本：A 股升级为 v13，港股/美股升级为 v11；候选按个股
  全局强度排序，行业只取温度和方向，不再请求合格行业成分及成员快照。纪律、持仓、退出、
  风险、仓位和轮换门槛保持原规则；审计区只解释“已通过纪律但未进入最终买入计划”的原因，
  未通过纪律的候选统一折叠在末尾，空的普通买入区不再展示。新增三市场一次性校验后才发布的
  无下单修订脚本；最新真实请求估算为 CN 2.976、HK 0.781、US 2.852，合计 6.609，
  实扣均为 0（缓存命中）。相关 Python/Dashboard 测试 1574 通过。

## 2026-08-08

- Dashboard 验收不再把账户快照刷新期间允许的短暂 503 控制台提示重复判错；其他接口的
  HTTP 错误仍由带 URL 的响应检查拦截。验证：Dashboard acceptance 单测 349 通过。
- 修复 Predict WebSocket 初始盘口后的重复/回滚版本把整条来源误标为 stale：这些旧帧现在
  只被忽略，已接受盘口与 ready 健康态继续保留；真正畸形的盘口仍 fail-closed。验证：
  Predict source、跨所监控与只读验收聚焦测试 235 通过，真实 11-market WS 持续探针保持
  ready 且零提交。
- 跨所 YES/NO 新增持久化 `auto_submit`：仅新进入 stage-5、Codex 已核准且非
  `manual_only` 的机会可并发提交双腿；首笔 5 USDT，完成双腿成交、REST 对账和
  Predict 授权归零后，同一指纹才升至 20 USDT。补救最多 2 USDT、未结算最多
  100 USDT、每日新增本金最多 100 USDT；同一信号不重试、不排队，同一 canonical
  pair 同时只允许一笔。所有拒绝在看板/API/历史显示稳定原因码、中文说明、当前值/
  上限、场所、时间及是否需人工操作；紧急暂停为单向持久化，重新 arm 只可通过本机
  CLI。飞书终态/事故通知失败不会中断当前对账，但会暂停后续自动入场；通知就绪是 arm
  的前置条件。验证：最终以完整聚焦套件、prediction-market e2e 与 `make acceptance`
  的结果为准。
- Trend statistics now refresh once per natural market cycle inside the existing market controllers; reports remain independent and use the last accepted statistics snapshot on failure.
- CN/HK accepted statements update only their own actual statistics, Kelly remains simulation-only, and Dashboard exposes independent counts, cutoffs, exclusions, and refresh status.
- 预测市场 YES/NO 页移除「当前监控范围」事件列表，主内容只保留「套利信号」（仍含
  交易与合并/事故 tab），24h 成交量移入套利信号表格新列（数据来自既有 signals 表，
  无后端改动）；跨所 YES/NO 漏斗与「可下单候选」保留，且候选列表只展示
  `manual_confirm` 且数据完整的候选，observe_only 与截止时间无效的候选回到漏斗计数。
  验证：dashboard 单测 374 通过，prediction-market e2e 34/34 通过，最终以
  `make acceptance` 为准。
- 预测市场存储瘦身与历史接口稳定性：LLM 缓存命中不再逐次落库（改为进程内计数，
  重启归零），真实调用仅保留最近 7 天（启动时清理，软上限）；一次性清理约 700 万行
  死数据并收缩 SQLite（约 1.3GB → 小）。套利信号历史面板只显示最近 30 天（SQL 层
  过滤），全量 signals 保留用于分析；历史接口每次请求只查一次标题翻译缓存，信号历史
  轮询从 1 秒降到 5 秒，看板 cache 数字标注为“本次运行”，消除请求堆积导致的持续
  503。验证：分支全量单测 5045 通过，6 个 trend_review 为 worktree 缺数据的
  环境性失败；acceptance PASS 后按验收 SHA 重部署。
- 跨所 YES/NO 文字一致人工批准：标题（question）规范化后一字不差的候选自动实时监控；
  年化 ≥15% 后显示“待人工批准”，由你在确认弹窗人工下单，永不自动执行（auto-eat 拦截）；
  严格等价配对保持自动路径语义。漏斗改为单行递减（正在监视 → 正收益 → 年化达标 → 已提交），
  准入来源（Codex 认为可以 / 文字一致）放底部图例；列表只展示可下单候选两类（人工 / 自动），
  人工类确认弹窗固定展示“结算规则可能不一致”警告与最坏损失，审核失败不进可下单列表。
  验证：监控/执行/通知/dashboard 单测与 e2e 全绿（Python 5047 通过，6 个 trend_review
  为 worktree 缺 `data/trend_review` 的环境性失败，补数据后 289/289 通过；prediction-market
  e2e 36/36 通过）。

## 2026-08-07

- #26 LLM 校验 prompt 统一：Codex 与 DeepSeek 两条路径使用同一固定 prompt（内嵌输出
  JSON Schema），只严格解析返回；prompt 版本提升使旧缓存失效。新增 2 个回归测试。
- #26 加固：DeepSeek 返回空内容时立即重试一次，仍为空才 `DEEPSEEK_EMPTY_CONTENT`
  fail-closed。新增 2 个回归测试。
- #26 修复：prompt 内嵌 schema 后模型偶发把 schema 文档本身当返回（导致
  `DEEPSEEK_OUTPUT_INVALID`）；新增 OUTPUT RULE 禁止回显 schema 元键，prompt 版本
  提升使旧缓存失效。实测 5/5 输出合规、0 次回显。
- #26 修复：Codex 连续 3 次失败后熔断 5 分钟，期间阈值关系与跨市场等价校验直接走
  DeepSeek，不再每轮先等 Codex 子进程失败（约 30-45 秒）；冷却后自动复探 Codex，
  恢复即优先。已验：两条 validator 相关测试 147 通过；真实链路 Codex 401 →
  DeepSeek APPROVE → 确定性校验 fail-closed 通过；线上重启后以 dashboard_runtime
  SHA 为准。
- #26 修复：fallback 回调类型标注修正为 `tuple[str|None, str|None]`；DeepSeek 失败
  原因细分（缺 key/空内容/超时/连接/认证/限流/HTTP），具体原因码透传到校验结果，
  不再一律显示 `DEEPSEEK_FAILED`。已验：两条 validator 测试 150 通过。
- 规范：`docs/superpowers/specs/` 与 `docs/superpowers/plans/` 不再纳入 git 跟踪
  （加入 `.gitignore`；已跟踪的 140 个 spec、159 个 plan 文件改为 untrack，文件保留
  在本地，历史提交不受影响）。
- #33 验证期自动吃单：看板新增三档模式（观察/手动/auto，sqlite 持久化，切换即时生效，
  切回观察或手动即暂停所有自动下单）。auto 模式对 Polymarket 同市场阈值对冲自动真实吃
  小单：年化 >15%、按最差价格扣最大手续费后净边际 >0 为硬门槛；每 signal 最多一次、
  关系对 submitted 后 5 分钟冷静期、每日 5 单或 $25（只统计 submitted）、余额 <$10 停；
  失败只记录不重试。成功吃单与结算（实际 vs 预计利润）发飞书，auto 模式不再发 order-ready；
  健康检查新增 auto_eat 模式/单数/拒绝/已实现盈亏统计。默认保持 observe_only。
- #33 修复：结算通知改为读取执行 payload 的 auto_eat 标记，消除“执行线程先完成、
  尝试记录后落库”导致的偶发漏发；结算测试轮询改用独立 30 秒预算。
- #32 套利筛选取向改为“年化 >15% 硬门槛 + 短结算优先 + 盘口深度充足”：低于 15% 的
  信号不再出现在看板 state/历史列表（`annualized_distribution`、`signals_24h` 统计保留）；
  达标候选中按 可参与 → 年化降序 → 结算期升序 → 绝对利润 → 成交量排序；机会行新增
  “理论可执行深度”（全簿扫价、含手续费后净边际仍为正的最大数量/成本）与“当前 $20
  政策下单量”双值；LLM 对冲候选区改为紧凑表格（标的/年化/结算期/理论深度/政策下单量/
  状态/操作），并移除左侧“可观察标的”面板。验证：627 个相关单测通过；全量 5010 通过
  （7 个失败为 worktree 缺少 `data/trend_review` 运行数据的环境性失败，main 上通过）；
  最终状态以候选 SHA 的 `make acceptance` 为准。
- #29 新增独立 prediction-arbitrage 健康检查后端服务（launchd label
  `com.open-trader.prediction-arbitrage-health`，RunAtLoad/KeepAlive，只装生产环境，
  与其他 worktree 部署无关）：每 2 小时检查 8766 状态端点与 healthz、heartbeat≤60s、
  universe≤300s、breaker、cross-venue、relation catalog、universe_retry_exhausted、
  readiness（执行被挡即 FAIL）、LLM 近 2 小时成功率、dashboard PID/运行 SHA；结果
  PASS/WARN/FAIL 每次发飞书（PASS 一行摘要，WARN/FAIL 逐项带值带原因）；飞书未配置
  显示 WARN，发送失败记日志下轮重试；`prediction-arb health-check --once` 可手动单跑。
  新增 24 个回归测试。
- #29 健康检查的运行 SHA 改为解析 legacy-dashboard 启动日志里最后一条
  `dashboard_runtime.git_sha`（进程真正加载的版本），不再读进程 cwd 的 git HEAD，
  避免 main pull 后把旧进程误报为最新；日志缺失时该检查 WARN。新增 2 个回归测试。
- #27 跨市场配对解析改为只读 Gamma 主查：Gamma SDK `Market` 对象（嵌套
  `state.end_date`、`outcomes.yes/no.token_id`、`trading` 费率）与旧格式 dict
  （`outcomes` + `clobTokenIds`）都能解析 YES/NO token 并构造配对，旧格式保持
  兼容；移除 CLOB REST 兜底（`end_date_iso` 只是事件日期取 00:00，不是收盘时间，
  无法提供可信 close）。无 resolution date 时 settlement 回退为 close_at，
  fees disabled 且无费率表时按 0 处理、费率未知仍失败关闭。Gamma 发现失败时
  跨所状态置为 degraded，快照带具体 `discovery_error`，看板跨所漏斗显示降级
  pill、上次成功快照与失败原因，不再静默显示 0。真实验证 16 个 Predict 开放
  市场 14 个候选：matched_pairs 13、unresolved 1（该市场源数据无 end_date，
  按规则失败关闭）。相关自动测试 466 个通过，最终状态以候选 SHA 的
  `make acceptance` 为准。
- #28 预测套利阈值信号（APR≥15% 且规则 approved）一旦 actionable 立即发飞书观察提醒，
  不再等待 fresh no-submit 复核；构造后 30 秒内因 data_unavailable 关闭的信号仍会收到
  提醒。同一信号只提醒一次，同一机会再次出现按新 episode 重新提醒；原 order-ready
  通知（fresh preflight 通过后）保留。观察提醒标题为「观察提醒」，正文标注
  「观察中 · 未下单」；order-ready 标题为「可下单提醒」并标注「待人工确认」，两者独立
  去重互不覆盖。新增 6 个回归（store/execution/monitor/通知文案）；最终状态以候选
  SHA 的 `make acceptance` 为准。

## 2026-08-06

- #25 预测套利 LLM 校验降级（子任务 1/5）：Codex CLI 401/超时/输出无效时自动改用
  DeepSeek（默认 `deepseek-v4-flash` + `reasoning_effort=max`，模型可用
  `OPEN_TRADER_LLM_FALLBACK_MODEL`、effort 可用
  `OPEN_TRADER_LLM_FALLBACK_REASONING_EFFORT` 覆盖），阈值关系与跨市场等价两条校验链
  都覆盖；降级 prompt 携带完整 JSON schema，DeepSeek 结果按模型单独缓存，Codex
  恢复后重新走 Codex。24h LLM 统计按 Codex/DeepSeek 分开并在看板两处展示，候选卡片
  标签按实际模型显示且 hover 可查看该模型评价；实时两层漏斗与关联合约扫描默认折叠
  只显示状态。Codex 与 DeepSeek 双失败时立即发一次飞书（每个故障周期一次），降级
  运行只进统计。相关自动测试 523 个通过，最终状态以候选 SHA 的 `make acceptance`
  为准。
- #24 把 Account API 与 Account Sync Worker 作为同一个 Account release 管理：新增
  `scripts/install_account_release.sh` 按「停旧 writer → 等锁释放 → 启新 writer →
  等新发布 → 启新 API → 同 SHA 交叉校验」顺序安装，并输出含 PID/cwd/SHA/启动时间/
  heartbeat/generation/8768 监听与日志路径的证据 JSON；升级与回滚都只改 Account
  双进程，Gateway/Legacy/Trend/Research/Prediction 进程与 PID 保持不变。新增
  Account release 升级/回滚 runbook 与 2026-08-06 有界演练记录：候选
  `40b3b83a` 升级、API 单独重启、Worker 停 20 秒故障、反向回滚到 `14526bc9`，
  每次快照均 200 healthy 且 release SHA 匹配；最终状态仍以候选 SHA 的
  `make acceptance` 与同 SHA 重部署证据为准。
- Dashboard 验收改为时间无关：报告只要求每个市场最新有效冻结报告可读、可显示且
  状态文案如实（`current`/`stale` 均可，不再要求当日产出或周末放宽窗口）；控制器
  只要求 execute 进程身份真实（PID/cwd/SHA/心跳/日志）且 unavailable/blocked 状态
  契约合法并如实展示，不再要求 healthy 或首次成功；模拟盘实时价只验接口可访问与
  `current_valuation` 结构完整，不再与第二次 live 抓取严格相等，持仓事实字段做
  有界收敛。浏览器验收改从页面当前 dashboard 状态读取趋势报告/复盘/控制器投影，
  持仓来源改用账户快照，投研讨论入口改用稳定 `position_id`，轮换组按当前页面
  DOM 契约校验已触发轮换并确认未达标项不展示，Account 快照瞬态 503（发布窗口内）
  允许有界重试且浏览器轮询仍须出现 304/有效 200。报告/控制器生成正确性继续由
  确定性 pytest 覆盖，验收相关回归 344 个
  通过。
- 修复复盘投影把每日变化的资源分配字段（市场分数、快照路径/SHA、排名与目标仓位）算入策略身份、导致 CN/HK/US 控制器在同版本区间内出现新一天分配后持续阻塞的问题；身份计算现在与回撤身份同口径排除分配动态字段，非分配策略参数漂移仍失败关闭。新增分配变化容忍与非分配漂移排除回归，趋势复盘与控制器测试 439 个通过。
- 真实持仓不再因多币种或负现金（打新/融资场景）被判不可用：现金按快照汇率折算合计、允许为负，资产净值作为真实轮换买入金额基准，模拟盘现金约束不变。新增多币种负现金放行、现金行损坏失败关闭与负现金轮换净值计价回归，相关趋势测试 892 个通过。
- 重生成趋势报告时，已终态（已执行/结束）的轮换对不再重新冻结进新报告，避免修订版被误当作新可执行动作而重复下单；未执行轮换对保持原冻结语义。新增已终态轮换对跳过与未执行保留回归，相关趋势测试 893 个通过。
- 趋势报告与看板不再展示未达轮换门槛（如强度差小于 20）或数据不可用/仓位阻止的轮换比较行，只显示已触发的合格轮换；报告 Markdown 无合格项时显示“无”。相关看板与趋势测试 1029 个通过。
- 看板买入计划拆分为“模拟盘正式买入计划”（只显示可执行正式买入，跳过项不再显示）与“实盘买入计划”（按实盘轮换买入腿展示，人工确认）；相对强度轮换改为原表格式展示（模拟盘自动/实盘手动两张表），只显示已触发项。相关看板测试 367 个通过。
- 看板历史报告投影把已执行轮换腿（自动轮换卖出/买入）补进 `sell_actions`/`buy_actions`，使执行台账在历史动作中可见；未执行轮换仍只在轮换区展示。新增投影回归，看板测试 588 个通过。
- 验收契约对齐当前架构：持仓浏览器检查与券商来源面板改读账户快照（#23 后 `/api/dashboard` 不再返回账户字段），`actual_overlay` 按当前空投影契约校验，移除对实时账户快照逐次等价的过严检查。相关验收测试 327 个通过。
- 验收投影校验补上自动轮换执行腿（#25 验收修复）：冻结报告动作投影现在与 Dashboard 一致地把已执行的自动轮换 sell/buy 计入 `sell_actions`/`buy_actions`，避免含轮换动作的报告误报不一致。相关验收测试 345 个通过。
- Account API launchd 安装器在健康等待超时后不再卸载已启动的 job（账户 Worker 发布匹配快照可能超过默认等待窗口）；默认等待提升到 90 秒，超时且进程存活时保留运行并告警。

## 2026-08-05

- 修复资源排名轮换冻结契约把同资产大类（local 基准）轮换对的强度差按全局强度核对、导致 US 趋势报告生成失败的问题；local 基准现在与比较快照同口径按大类内强度差校验，global 基准行为不变。新增 local 基准轮换对冻结契约回归，相关趋势/控制器/复盘测试 877 个通过。
- 修复 Trend allocation 守护进程在每日 16:20 等待窗口内每 5 秒新建一次 Futu 行情连接、OpenD 异常时线程持续泄漏直至进程卡死的问题；等待阶段不再访问 Futu，只在收盘窗口按需查询交易日历，并新增等待阶段零外部调用回归。

## 2026-08-04

- #23 将生产 Account 消费者统一迁到带 `X-Open-Trader-Account-Route: production` 的 HTTP 快照和 accepted statement-facts 路由：Trend 每轮冻结一个 `account_input` generation，Legacy `/api/dashboard` 不再返回或读取 Account 字段且 `/api/quotes` 返回 404，浏览器以 exact `position_id`/`instrument_id` 合成持仓与 Backtest 选项。Premarket/T-signal CLI、launchd 路径和 watcher 已禁用；候选验收新增 Account/Legacy 独立请求、强制观察快照更新、三市场冻结 generation、受控 Account 故障时非 Account 模块可读及无通知路径，并从共享主仓库/运行根读取被 Git 忽略的预测市场凭据配置与验收 Python，受控 Account 停机后也以同一运行根和 Python 恢复，避免隔离 worktree 误报外部环境不可用、部署后指向缺失文件或门禁在测试前失败。部署先启动匹配的 Account Worker/API、验证两条生产路由，再同 SHA 重启 Gateway/Legacy/Trend；故障时整体回滚 #23，绝不将新消费者与 #22 Account 或 Legacy 原始读取混用。已完成专项验收回归与全量自动测试；最终状态仍以候选 SHA 的 `make acceptance` 与同 SHA 重部署证据为准。
- 修复 Trend allocation 守护进程重启后把当天已生成的合法不可变快照重新计算、最终触发同日快照碰撞的问题；合法终态会原样恢复，`waiting/retrying` 状态也会直接从当天快照恢复为 `ready`，不再访问富途或 Trend Animals。仅显式 `--revision` 允许重新生成，损坏的状态、指针或快照继续失败关闭。
- 修复 Predict 实时验收把明确范围外的 NegRisk、收益型、非 YES/NO 等市场误报为脏数据并逐条请求分类的问题；范围外记录现在先行跳过，普通 YES/NO 仍严格校验。Live readiness 只读取首个合格标的及盘口，不再等待 4,101 条市场全量扫描；正常 watcher 仍保持全量发现。只读 guard 改为直接拦截 SDK 变更方法和底层链上发送，不再代理破坏有状态的本地签名；盘口按 SDK 要求保留普通价格/数量单位，避免二次放大。真实 REST/WebSocket、账户与签名未提交预检通过且零变更调用。
- 跨场执行最终防护确认：确认弹窗不设 TTL、提交前强制当前盘口刷新；Predict 只允许精确买入授权并在成交/失败后清零，残留授权清理由人工确认触发且不搬运 USDT；BNB gas signer 与 Predict 账户分开展示并提示人工充值；首笔跨场 canary 上限保持 5 USDT；过期漏斗与成功空扫描都按事实展示，历史按执行阶段分组；验收允许完整空扫描通过但仍要求零授权、零清理、零订单、零转账/赎回和零真实通知。
- #22 把辉立与东方财富结单上传迁到 Account Module：浏览器仅经 Gateway 调用 Account API，验证后的 PDF 以内容 hash 原子暂存为不可变 generation，Account Sync Worker 验证后才 promotion 并在快照发布 accepted generation；Trend 控制器异步、幂等消费对应成交事实，失败不再回滚 Account。Legacy 上传路由和同步 Trend 副作用已删除；真实隔离 runtime 验证两份实际结单均返回 `202 staged`、四券商同步正常、两份 generation promotion 且 Trend 重复消费安全。
- Account 当前估值改为由 Account Sync Worker 从已接受持仓构造 OpenD 报价范围，覆盖 Tiger/Futu 以及辉立、东方财富的港美 A 股票、ETF、基金、期权和未知资产；缺少当前或已保留报价时继续失败关闭，不以结单价冒充实时价。Account v1 与三市场模拟盘新增完整的 `current_valuation`，同时给出美元/港元市值；现金和货币基金保持原契约。Dashboard 仍显示“实时价”，优先渲染 owner 发布值，估值变化时仅对实时价、美元市值和港元市值短暂高亮，不改变页面布局或轮询。专项回归 815 个、全量测试 4,505 个通过；最终状态仍以合并 SHA 的 `make acceptance` 与同 SHA 重部署证据为准。
- 修复 Account API 将 A 股 OpenD 行情键 `SH./SZ.` 误按业务市场 `CN.` 校验、导致完整行情发布返回 503 的问题；校验统一复用既有富途代码规范化规则。回归及账户发布相关测试 92 个、全量测试 4,506 个通过，当前真实发布数据直接读取恢复为 `200 healthy`；最终状态仍以修复合并 SHA 的完整验收为准。
- Dashboard 验收中的辉立账户总资产改为用同一页面快照内的 OpenD 当前估值与现金重新求和，不再拿会随行情变化的当前资产和结单日总额比较；最新结单月份校验继续保留。Dashboard 验收回归 320 个通过，三市场控制器仍须随最终 SHA 一并重部署后重新运行完整门禁。

## 2026-08-03

- 跨场 YES/NO 仅在 Predict.fun 与 Polymarket 的完整规则、直接 YES/NO 极性和统一 UTC 截止时间经 Codex 核准后进入监控；不确定、过期或不等价的候选保持可见但不可执行。
- 受保护的人工跨场执行保留现有风险上限、双腿价格/成本上限、签名预检、幂等与对账边界；Predict 适配器、五阶段漏斗、顶部状态和确认弹窗只展示事实，不自动提交、撤单或赎回。
- 预测市场验收现在分别报告 Predict REST/WS 盘口、Predict JWT/余额/授权、已签名未提交预检、Polymarket 来源/账户/预检，以及零变更调用和零真实通知；缺少外部、浏览器或 Keychain 环境明确为 `BLOCKED`，认证或读取异常明确为脱敏 `FAIL`。
- Polymarket 预检现在由验收侧硬性拦截下单、赎回、其他变更和通知调用，并要求显式 `posted: false`；SDK 内部读取也通过代理 self 经过同一只读边界。Playwright 通过后写入带一次性 nonce、有效期、实际 `18766` fixture URL、`8766` review URL/healthz 和当前 Git SHA 的 handoff，registry 只消费一次匹配 nonce。
- 修正验收只读边界对 SDK 底层 HTTP `request/send` 和本地 `create_market_order` 签名的误判：普通 GET/HEAD/OPTIONS 读取及未提交签名可以通过，通知、下单、撤单和赎回仍硬失败并计入安全计数。

- #21 为 Account API 生产切换补齐验收与运维交接：浏览器经 `8766` 独立轮询带 ETag 的 `/api/v1/account/snapshot`，不再请求 `/api/quotes`；Legacy 继续提供其余模块，任一 owner 降级不覆盖另一方。验收会核对稳定 ID 关联、双上游健康、Worker/API 同一 SHA、listener/runtime 日志、ETag/304 与 API parity；Account API 启动日志同时记录候选 Git SHA 与源码洁净状态，浏览器取证后冻结 Legacy 与 Account 两个轮询，并从 Account 页面状态按标的匹配持仓和实时来源、识别 `healthy` 来源状态，不依赖 Legacy 快照或页面排序。Dashboard 风险校验从冻结参数读取资源排名目标仓位，允许第一名 6% 而不放宽 4% 组合风险预算；冻结报告投影校验复用 Dashboard 的双强度字段投影，历史执行缓存也会在同一 action 目录追加成交事件时立即失效，stack 安装失败关闭且不自动进入 single 模式。新增切换、逆向回滚、writer-lock 与 Account-only 故障恢复 runbook，并同步当前轮换比较、CN v12、HK/US v10 与预测市场历史窗口的验收夹具。最终状态仍以候选 SHA 的 `make acceptance` 和同 SHA 重部署证据为准。
- Dashboard 冻结报告验收同步资源排名强度投影：报告快照中的大类内/全局强度现在与 API 动作列表按同一口径比对。
- Dashboard 验收持仓表列定义同步到趋势强度可见性：实盘/模拟盘现在都校验“大类内强度”和“全局强度”两列，浏览器验收计数与实际页面一致。
- 修复验收投影对旧报告缺少轮换比较字段的兼容读取，并同步 HK/US v10 资源排名夹具；预测监控的 7 日分布测试固定其测试时钟，完整 Dashboard 验收 310 个通过。
- 修复资源排名策略升级后旧 CN v11、HK/US v9 报告因兼容审计版本被误判为不可读的问题；当前 CN v12、HK/US v10 仍独立拥有新状态，旧报告只读兼容验证通过。
- 将资源排名轮换升级为同资产大类比较“大类内强度”、跨股票/ETF 比较“全局强度”，仅在强度差达到 20（含）时形成最多两组轮换；冻结比较快照并在 Dashboard/报告显示口径、两侧强度、差值和未触发原因。当前资源排名报告版本为 CN v12、HK/US v10，沿用既有 Kelly/回撤身份，不重新积累；账户视图刷新改用保留滚动位置的原生焦点恢复。相关趋势、控制器、Dashboard 与验收回归通过（除工作树缺失的 2026-07-16 忽略复盘快照测试）。
- Dashboard 验收白名单曾同步 CN v11、HK/US v9 资源排名策略版本，并按冻结快照验证第 1/2/3 名的 6%/4%/2% 目标仓位及买入上限；页面“当前纪律”也复用同一冻结快照，不再回退显示旧版 4%。截图仍在浏览器流程中生成，但缺失不再影响可选截图规则下的 `PASS`。Dashboard 与验收回归 560 个、全量测试 4,435 个通过。
- R2 增加仅监听 `127.0.0.1:8768` 的只读 Account API shadow：提供强 ETag 的 v1 快照、稳定发布读取、独立 live parity 与 launchd 运维路径。Account Sync Worker 仍是唯一 writer，Gateway/Dashboard 不变；最终验证仍需完成 Worker/API 同一精确 Git SHA 的运行时证明，尚未宣告部署或验收。
- 资源排名策略的累计回撤身份不再包含每日变化的快照路径、SHA、排名、分数和仓位；上线前的旧全量哈希会保留高水位并只追加一次兼容审计，稳定策略参数仍逐项验真，报告策略参数也必须与冻结资源快照一致。终态状态将阶段、阻塞原因、快照引用与 `latest` 绑定为同一次读取，富途日历不可用仍结构化标记为 `BLOCKED`，程序错误不再被误报为外部阻塞。
- 累计回撤预检现在读取当天已终态的共享资源快照，使 CN v11、HK/US v9 正确继承既有高水位而非被误判为状态缺失；未生成当天快照时仍沿用原预检路径，相关回撤与资源排名测试 69 个通过。
- 资源排名任务重启后若当天已有终态快照，现在只刷新当前 PID、Git SHA 和心跳，不再访问富途或趋势动物，也不读取移动的最新指针或改写不可变快照；损坏的终态引用继续失败关闭，资源任务与市场控制器测试 169 个通过。
- 修复资源排名成功时空的失败原因被报告冻结层误判为无效，导致 CN/HK/US 新报告拒绝同一份正常共享快照的问题；正常生产输入统一冻结为空字符串，已冻结报告仍拒绝空值，真实 `2026-08-03-r1` 快照直接验证通过，相关趋势报告与控制器测试 648 个通过。
- 冻结 Account v1 快照契约，并把账户/报价的单写者统一重命名为 Account Sync Worker；运行命令改为 `account-sync-worker`，现有 launchd label、heartbeat/lock 文件及 JSON/CSV persistence 保持不变。本阶段不启动 Account API、不切换 Gateway 流量，也不改变 Dashboard、策略、报告或执行行为。
- 修复 Dashboard 与账户同步 launchd 重装在旧 job 异步移除完成前立即 bootstrap 的启动竞态；安装器现在确认 label 已消失后再启动，并移除账户同步在 RunAtLoad 后多余的 `kickstart -k`，避免新进程被立即杀死和节流重启。本阶段不改变页面、策略、报告、执行或 worker 业务行为。
- 新增收盘后的 `trend-allocation` 共享快照：以收藏夹 A股/ETF基金、港股/香港ETF、美股/美国ETF 六个根节点的全局强度统一排名，三个市场报告冻结同一路径与 SHA；新开仓按第 1/2/3 名使用 6%/4%/2%，不追加强制调仓或重置既有 Kelly、回撤历史。缺失或过期时整份沿用最近成功排名并标记 A 股交易日陈旧天数，冷启动失败关闭。
- 满 10 个席位且强度差至少 20 时，每市场、账户、交易日最多冻结两组强弱轮换；模拟盘只在市价卖出全量成交并刷新账户后自动市价买入，实盘仅生成当日报告内的手动卖出后买入建议。分配任务在 A 股收盘后运行，CN/HK/US 控制器等待其当日终态再生成下一份报告。
- 验证：聚焦行为套件 1,822 个通过，确定性三市场/轮换工作流 13 个通过，Trend Animals 分配与 API 回归 86 个通过；2026-08-03 实时只读快照成功冻结为 `data/trend_allocation/daily/2026-08-03.json`（SHA-256 `21527e48f75cc4b82d1722f10be07fca5b3541bc8f19f4175c35c1daf3037f83`），排名为港股第 1、美股第 2、A 股第 3，重复运行未覆盖该不可变文件。

## 2026-08-02

- 新增仅只读的 Predict.fun 市场与盘口来源，REST 与 WebSocket 健康状态分别发布；仅通过 Predict 的 `polymarketConditionIds` 显式匹配 Polymarket，并在订阅前经过独立 Codex 结算等价性闸门。
- 跨场仅监控 `Predict YES + Polymarket NO` 和 `Polymarket YES + Predict NO` 两个方向；使用 `Decimal` 和主线既有的 15% 年化准入 helper。五阶段漏斗依次展示显式匹配、受监控、Codex 核准、套利空间和明确信号；信号持久化为仅观察记录。
- Predict.fun 主网不存在 signer、下单、授权或自动执行路径。Predict API key 尚未分配，主网 REST/WS 运行验证仍待完成；不得将当前状态视为 Predict 已可用。
- 修复预测市场状态接口因 24 小时 Codex 用量逐行载入、持锁读取历史/标题指标及每个只读标题查询重复协商 SQLite WAL 模式而超时的问题：用量改为 SQLite 单行聚合，状态快照仅读取锁外每分钟刷新的用量、年化和 24h signal 指标及 Monitor 已投影标题，WAL 只在 Store 初始化时设置，Dashboard 不再逐行重查标题或写入 cache-hit 事件；保留既有审计数据与真实 Codex 调用统计。使用现有 702 万条用量记录验证状态接口恢复 HTTP 200，相关回归 468 个通过。

## 2026-08-01

- 预测市场 Top 20 监控列表刷新失败后改为每 5 秒自动重试；任一次成功即恢复正常 5 分钟节奏，连续 5 次失败后停止重试、保持 YES/NO 失败关闭，并通过 Feishu 提醒人工重启承载预测监控的 Dashboard 服务。
- 预测监控维护循环改为只读取未结束的 signal，并缓存不可变历史 action 投影，避免历史记录增长后阻塞 Dashboard 历史报告接口。
- LLM 阈值对冲信号现在按共同合约到期日计算剩余时间和含最高模拟交易费的年化净回报；低于 15% 的机会仍保留观察，但不会通知或预览。信号表改为完整英文标的在上、完整缓存中文在下，便于核对入场条件；共享盘口深度计算改为等价整数路径，保持全目录每分钟重扫并避免扫描拖慢看板。
- 趋势报告不再为仅持仓行业付费展开全体成分；候选行业的精确宽度、当日排序、动作、风险及 Dashboard 行业字段/状态保持不变，仅持仓行业继续展示供应商聚合比例。若该行业日后首次重新成为候选，可能暂时使用仅当前数据排序。使用 2026-07-31 三市场冻结账本验证减少 18 次成分调用和 3,610 个成员快照，成员字段费用减少 10.830 Trend Animals 余额单位。
- 修复 Dashboard stack 在 `RunAtLoad` bootstrap 后重复 `kickstart -k`、可能留下孤儿 listener 并触发 launchd 重启循环的问题；安装和回滚现在只执行一次受管启动。
- 稳定预测市场 loading 状态的浏览器验收：信号组件恢复历史后仍明确验证无下单按钮，不再把局部刷新前的瞬时空态当作交易安全条件。
- 将预测市场 `当前机会` 替换为每秒局部刷新的 `套利信号` 组件；新增 HKT Watcher/信号新鲜度时钟与中英标的标题缓存；Feishu 改为无链接、仅观察的通知，并按市场成功送达设置 30 分钟冷却；人工下单仍保持 `重新检查` → `确认下单` 边界，LLM 对冲套利行为不变。
- Dashboard launchd 安装器现在默认把已验证的 Legacy Dashboard `8767` 与轻量 Frontend Gateway `8766` 作为一个双进程 stack 切换；切换失败会自动恢复保留的单进程 job，`--mode single` 可明确回滚，完整卸载会幂等移除三个已知 job。未知端口 listener 会在任何状态修改前阻止安装。
- Dashboard acceptance 现在分别验证 Frontend Gateway `8766` 与 Legacy Dashboard `8767` 的独立 PID、模块身份、工作目录、Git SHA、源码状态、启动时间和新鲜 runtime 日志；账户、报价、API 与浏览器流程仍只通过稳定的 `8766` 入口执行，单进程 rollback 模式不再满足最终 PASS 条件。
- 修复非交易日上午的趋势保护轮询把正常 `holiday` 结果误判为异常、导致 CN 控制器阻塞的问题；仅零异常且零未知报价的 holiday 结果不再阻断，其他保护异常继续失败关闭。
- 新增轻量 `frontend-gateway` 进程：前端静态资源、健康检查与 `/api/*` 请求统一经网关访问，现有 Dashboard 作为仅监听 loopback 的 Legacy Backend 保持业务兼容；Gateway 启动入口会在加载单体 CLI 前分流，不再连带导入交易、研究、预测 adapter 或 worker。补充双进程部署参考与回滚步骤。本次仅合入渐进迁移边界，当前生产 launchd 仍保持单进程，待后续部署 issue 再切换。
- Dashboard 验收与交付不再强制截图；仅在用户显式要求时提供，缺少截图不再影响 `PASS`、完成或部署判定。
- 修复 YES/NO 与 LLM 对冲套利页面被错误锁定：普通 Top 20 刷新与每分钟关系扫描不再争抢 30 秒预算或重复关闭 WebSocket，静默但已连接的 Watcher 不再误报不可用；两类策略分别判断健康状态，分钟扫描会立即发布新机会并恢复已缓存的 Codex 结论。人工预览只定向复核所选机会；Codex 已批准但盘口过期的正收益候选可主动刷新两腿，刷新后仍满足条件才生成确认单，并在弹窗保留双市场、利润与 Codex 理由。最终下单仍需用户在确认弹窗中明确提交。
- Dashboard 账户持仓、趋势纪律与审计折叠区现在会在报价轮询刷新、账户视图重绘和券商标签切换后保留用户选择的展开/收起状态；切换报告身份时不复用旧状态。验证 375px 账户视图 Playwright 回归 1 个通过。

## 2026-07-31

- 将预测市场顶部 Watcher 状态改为只反映 Polymarket WebSocket/心跳连接；盘口过期仍保持失败关闭并明确提示“当前盘口暂不可交易”。验证 Dashboard Web 测试 286 个通过。
- Added Homebrew CLI directories to the Dashboard launchd `PATH`, so the
  Polymarket relation validator can invoke Codex instead of failing before any
  model tokens are produced.
- 修复 Polymarket 每分钟关系扫描把自身的启动时刻误判为落后、从而连续补跑的问题；扫描启动时会先推进下一到期时间，真正超过一分钟才补跑一次。Dashboard 交易计划只投影页面使用的回测汇总，并按文件版本缓存 64 MB 源计划解析，不再每五秒解析和传输完整曲线、成交及信号明细；模拟盘快照在启动前预热，过期时返回最近快照并在后台刷新，避免关系扫描拖住页面。启动身份日志先于预热输出，保证验收能核对准确 PID/SHA。验证 624 个 Dashboard、Web、monitor 与模拟盘测试通过。
- 修复 Polymarket 官方 SDK 模型导致关系候选恒为 0；关系目录改为每日全量扫描并持久化。新增每分钟 5% 成交候选漏斗、Codex 前置缓存、定向 WebSocket、机会窗口历史，以及“已可下单但观察模式未提交”的飞书通知。Dashboard 实时展示两层漏斗、淘汰原因、扫描耗时、Codex 队列和 WebSocket 健康；最终验证 654 个预测市场与 Dashboard 测试通过。
- Final verification for the broker source panel passed the full Dashboard
  gate (`3902 passed`, live status `PASS`) after the accepted SHA was deployed;
  desktop and mobile source-panel screenshots were captured for operator review.
- Stabilized Dashboard browser acceptance by checking broker source timestamps
  against each viewport's live page payload, avoiding false failures when the
  account-sync controller publishes a newer accepted time during the gate.
- Simplified the Dashboard account-source panel by grouping live accounts and
  broker statements and showing each broker's own accepted data time. Removed
  the redundant global quote, heartbeat, controller, and refresh labels while
  preserving file-backed status, quote polling, and per-broker failure states.
- Persisted provider-verified 富途代码、趋势动物代码和 `tmId` as one immutable
  mapping, with one exact discovery attempt and a permanent rule-versioned miss
  instead of retries. Initialized the already verified `SH.515450` mapping;
  new simulated actions now freeze and execute the exact mapped 富途 code while
  legacy reports retain their existing conversion path.
- Expanded each frozen CN/HK/US industry context from eligible candidates to
  the union of candidates and current holdings, sorted by descending trend
  strength with invalid rows last. Holding-only lookup failures remain local to
  their row and display as `趋势代码映射异常`; same-day report revisions now keep
  matching immutable `-rN` industry-history snapshots.
- Made Dashboard browser acceptance compare volatile controller-owned prices
  with the page's current state instead of a pre-navigation snapshot. Live
  quote fetch and valid-price checks remain strict, so normal price movement no
  longer causes a false DOM mismatch. Slow but successful live OpenD responses
  now get a bounded 30-second API and DOM wait instead of a false timeout.
- Moved account-table financial fields into the account-sync controller's
  published Dashboard projection. The browser now only renders controller
  values, so missing FX or quotes cannot blank HKD market values and weights;
  API and DOM acceptance now compare the published fields end to end.
- Made each Dashboard trend-report main view select the newest valid artifact
  immediately, including the next US execution-day report before New York
  midnight. Invalid artifacts still fall through safely, and all older valid
  reports remain available from history. The final Dashboard gate now has an
  explicit, opt-in Polymarket-live waiver for unrelated venue outages; its
  default remains strict. Complete, current quote fallbacks now preserve
  account-sync health; missing or stale quotes remain abnormal.
- Merged the statement-upload and restored market-discipline fixes into `main`.
  A real Phillips upload now shows four securities including `03308`, dated
  2026-07-29, with HKD 628,326.07 total assets; final acceptance was skipped at
  operator request.
- Required every user-visible UI change to include screenshots from the exact
  deployed and accepted SHA in the final response. Responsive or mobile changes
  require desktop and mobile views; missing, stale, or irrelevant screenshots
  now block an accepted/completed claim without adding a user-approval wait.

## 2026-07-30

- Kept Phillips equity holdings whose statement row omits `LastBoughtOn`, so
  uploaded statements no longer silently drop transferred positions such as
  `03308`.
- Made each statement account header show that broker's accepted statement date
  instead of the shared detail month, so a newer 东方财富 upload is no longer
  labeled with 辉立's older date.
- Made uploaded 东方财富 and 辉立 statements authoritative after broker-period
  freshness validation: same-period replacements no longer fail or roll back
  when derived trade statistics cannot yet satisfy their cutoff-time invariant.
  The Dashboard reports `统计待重建` while retaining the accepted statement and
  previous statistics; 辉立 Payment and Deposit rows no longer create false
  incomplete-execution warnings.
- Kept statement-only Phillips and Eastmoney holdings out of the Futu live-quote
  universe while preserving their accepted positions and labeling their
  statement prices explicitly in the Dashboard. A stale-only Hong Kong ETF
  dynamic root or resolved child now means zero ETF candidates, while
  stale-only secondary industry breadth becomes a visible invalid context and
  falls back to individual ordering. Current-date validation remains strict
  for every real candidate pool. The final gate now reads the launchd-owned
  Dashboard log instead of an obsolete temporary path; verified 3,868 full
  tests plus 11 focused launchd and gate tests.
- Added the sole account/quote sync controller: broker reads now validate a
  candidate before atomic publication, while the Dashboard only projects
  accepted files. Removed the Dashboard refresh action and the old rollback
  path; failed, stale, and unverified sources retain visible last-accepted data
  but pause account-dependent actions and show `人工复核`. Added the
  `account-sync-status` and `install_account_sync_launchd.sh` operator paths;
  Dashboard acceptance now verifies those degraded states without skipping the
  three market reports, while the separate process gate still rejects unhealthy
  controllers. Browser acceptance confirms file polling started, then freezes
  its page snapshot so a background refresh cannot detach controls mid-check.
- Restored the current HK and US trend-report entry discipline to the same
  fail-closed gates used by A shares: individual temperature, strength, phase,
  industry temperature, right-side/tradability/danger flags, candidate age,
  ATR, and CNY-normalized market-cap and turnover thresholds. Reports now fetch
  and freeze industry evidence before selecting candidates, so an industry
  below `温` cannot appear in any buy view. The new rules are published as v8,
  inheriting v7 Kelly and drawdown history while historical HK/US v4-v7
  identities remain unchanged. Verified 3 focused below-warm report scenarios
  and 798 focused report, strategy-identity, drawdown-preflight, and Dashboard
  acceptance tests.
- Sorted CN/HK/US real and simulated trend-report holding rows by report
  strength. Rows now reuse the existing light green, light pink, and soft gray
  backgrounds to distinguish current buy/hold membership, non-trend holdings,
  and trend-lookup blacklist exclusions without changing the ten-column table,
  strategy, execution, or Feishu output.
- Made real-holding Trend Animals lookup market-aware so same-code crypto rows
  no longer hide valid US stocks or ETFs. Successful mappings remain persistent;
  exact misses are cached per market and report data date, then retried on the
  next data date. A missing real symbol now degrades only its own row.
- Excluded `US.AGRZ` from real-holding trend requests while keeping the position
  visible, read-only, with empty trend fields and sorted last. Simulated strategy,
  Kelly, risk, execution, the existing ten Dashboard columns, and Feishu output
  are unchanged. Verified the focused provider/report/replay/Dashboard suites
  with 1,526 passing tests and live US stock/ETF lookup results.
- Added read-only `真实持仓` / `模拟盘持仓` tabs to the existing CN/HK/US
  `盘中持续 · 已有持仓` report stage. Real-account decisions are frozen per
  report and never affect simulated strategy actions, counts, risk, Kelly,
  Feishu, or execution; legacy and unavailable snapshots remain explicit.
- Hidden unavailable or missing 富途期权异动 buttons instead of rendering
  misleading disabled controls; available rows retain the existing native
  detail dialog. Verified the dashboard, acceptance, and three-market report
  suites before the final live acceptance gate.
- Prevented the final CN/HK/US cycle-status write from moving a live heartbeat
  backward after a long strict historical audit finishes. The phase transition
  now preserves the latest audit heartbeat until the next controller poll.
- Kept CN/HK/US controller heartbeats fresh while the first strict historical
  action-audit pass is still reading large immutable ledgers. Progress updates
  are throttled to five seconds and do not skip or weaken any audit checks.
- Isolated the completed-audit cache regression test from live Futu OpenD and
  made the concurrent Feishu retry test wait for the sending process explicitly,
  so Dashboard acceptance no longer hangs or races on test-process timing.
- Corrected the CN/HK/US Dashboard `盘中持续 · 已有持仓` projection: holding
  rows now recover industry and available right-side days from frozen snapshots,
  show the shared `行业` column, and use the frozen industry-first discipline
  order. Invalid or missing industry context falls back to individual ordering;
  the source report payload remains unchanged. Verified the affected suites
  with 946 passed tests.
- Prevented CN/HK/US trend controllers from repeatedly revalidating already
  completed historical action audits on every polling cycle. Each process still
  validates them once after startup, then keeps heartbeats fresh during live
  monitoring; verified against the large US audit ledger and controller tests.

## 2026-07-29

- Dashboard 美股/港股趋势报告在正式买入和继续持有标的下增加富途“期权异动”按钮；同日数据可查看只读详情，缺失或过期时置灰。移除旧跨市场“期权关注”入口，并从飞书趋势报告删除该段落。
- Removed the local 10-component and 10-valid-row minimums from trend industry
  context validation, so complete small ETF groups no longer disable industry
  ordering for an entire market. CN/HK/US buy plans now reuse each report's
  frozen industry context when the action row lacks industry temperature, and
  their current discipline summaries now state the real industry-first
  candidate order instead of the legacy four-key stock-only order.
  Verified with 1,219 focused tests and offline rebuilds of the latest frozen
  CN/HK/US evidence; the US two-member healthcare ETF context remains valid and
  restores industry-first ordering.
- Fixed the live Dashboard acceptance hover check to select one real industry
  metric when a report contains multiple industry rows, preventing Playwright
  strict-mode failures while preserving the tooltip content assertion.
- Added the first manual-confirmation Polymarket threshold-hedge path: a
  time-bounded scan of the first 100 active Gamma events for same-event
  relations, deterministic proofs plus structured Codex
  validation/cache metrics, truthful rejected/unavailable reasons, separate
  condition BUY/FOK submission and reconciliation, `holding_to_resolution`
  for multiple unresolved combinations, folded in-memory scan logs, and a
  Dashboard switch between `YES/NO套利` and `LLM对冲套利`. LLM candidates start
  folded and disclose their current annualized calculation, historical
  distribution, structured Codex evidence, independent legs, and
  confirmation action in place; cross-condition legs never invoke merge.
  Confirmation now also fails closed against contradictory LLM status,
  relation/outcome directions, condition/token identities, quantities,
  economics, or settlement timing, and every blocked candidate keeps its
  reason visible. Verified 311 focused monitor/Dashboard tests and all 55
  prediction-market browser scenarios across desktop, tablet, and mobile; live
  order submission remains behind the existing explicit preview/confirm gate.
- Allowed an explicitly authorized same-day simulated late buy to bind a
  higher, hash-verified corrective report when the execution batch had already
  frozen a bug-suppressed report. Recovery still requires a prior missed event,
  an open market, a new action absent from the locked report, immutable order
  evidence, and normal Kelly attribution. Verified the full trend-review suite.
- Prevented standalone protection-line full exits from entering the legacy 30%
  overheat-trim lifecycle rebuild. A valid protection sell can no longer block
  a later US trend report merely because the day's frozen report had no formal
  sell action. Verified the protection audit and market-report suites.
- Corrected the shared CN/HK/US Trend Animals-to-Futu symbol mapping, including
  four-digit Hong Kong provider codes and underscore-form US class shares, and
  made every simulated holding's Futu daily price refresh independent from
  provider lookup success. Mapping failures now remain explicit manual-review
  signals and no longer masquerade as missing prices or pause otherwise eligible
  simulated entries. Verified focused symbol, provider-client, three-market
  report, and risk regression tests.
- Allowed a same-date trend-report revision to enrich legacy industry history
  with the newly available right-side count and market-cap ratios, while still
  rejecting any change to previously recorded industry facts; frozen-evidence
  replays and strict Dashboard artifact validation now preserve all four
  current/prior ratios. Verified the history, replay, Dashboard, and A-share
  report suites against the shared runtime data.
- Added prior-to-current right-side count and market-cap ratios to the existing
  trend-report industry context table and Markdown/JSON outputs. The Dashboard
  now explains both denominators and structure gap on hover, focus, or click;
  missing provider data remains unavailable and does not change strategy
  ordering or risk actions. Verified focused context, report, market, and
  Dashboard suites before the final acceptance gate.
- Verified the unified CN/HK/US trend reports in the live Dashboard with
  Playwright: buy, sell, review, and holding rows expose the same temperature
  change and phase columns, and every market includes current industry
  temperature. Generated and selected the pre-execution HK `2026-07-28-r1`
  revision; the in-session US report remained immutable after its execution
  batch lock and passed the same rendered-report checks.

## 2026-07-28

- Unified the CN/US/HK trend-report action tables so buy, sell, review, and
  holding stages use the same complete columns, including temperature and
  phase; legacy holding reports now enrich phase read-only from frozen
  snapshots. Verified cross-market web parity and frozen-report regressions.
- Made HK/US protection-monitor timestamps timezone-aware so triggered orders
  remain valid in the action ledger and Dashboard simulation/history checks;
  acceptance now reuses the shared report projection and recognizes validated
  synthetic protection actions without inventing frozen reports, and refreshes
  live simulated positions before each browser viewport.
- Changed prediction-market monitored events to start collapsed and preserve
  each operator-selected expanded/collapsed state across watcher refreshes.
  Added the behavior as the explicit `UI-14` acceptance scenario.
- Reworked the prediction-market workspace to the approved truth-driven
  Variant A: exactly four readiness cards and four live metrics, visible volume
  ranking, fail-closed incomplete data, fresh-preview-only confirmation,
  truthful execution/incident history, and responsive desktop/mobile layouts.
  Expanded the acceptance registry to 62 scenarios and added deterministic
  golden coverage for unavailable, unknown, and incomplete states.
- Disabled the legacy TradingAgents daily premarket automation and its HK/US
  start, action, blocker, and completion notifications while preserving manual
  runs, historical artifacts, and all three trend controllers. Verified no
  matching process, launchd job, plist, cron, `at`, or `screen` task remained;
  the notification-off checks and full test suite passed.

## 2026-07-27

- Allowed the Dashboard to show a newer validated trend-report revision when
  its formal actions exactly match the execution-locked report, while keeping
  execution events bound to the original batch SHA. Verified both same-action
  display and changed-action fallback paths plus invalid-batch/history
  regressions.
- Corrected the fixed CN/HK/US Futu stock-simulation account deployment,
  archived the previous HK option-account report and ledger generation without
  rewriting history, and restarted HK from an account-bound no-replay cycle.
  Verified all three configured accounts with real SIMULATE submit/cancel
  orders; order IDs `7606013`, `7606014`, and `7606015` all finished
  `CANCELLED_ALL` with zero fills. Refreshed the account-bound trade statistics
  and made Dashboard acceptance recognize a frozen US report whose execution
  date is already the current Shanghai operator date.

## 2026-07-26

- Added the local Polymarket prediction-market monitor and exact approved
  execution UI: top-20/5-minute discovery, visible 24h volume, one confirmed
  two-FOK request, merge handling, bounded one-leg incidents, Keychain and
  loopback-only protection, durable signal/trade/incident history, macOS
  launchd deployment, and the fixed 54-scenario acceptance registry. Verified
  with the prediction focused tests and desktop/mobile golden screens; live
  venue/Keychain checks remain explicitly BLOCKED until configured.
- Moved the current CN/HK/US trend review into a default-closed, audit-style
  disclosure directly after audit details in the Trend Report tab; its compact
  summary shows both sample counts, while frozen historical reports still
  exclude current review data.
- Removed the standalone CN/HK/US trend-review tabs and rendered each market's
  review metrics directly below its current trend report; keyboard, mobile, and
  Dashboard acceptance checks now enforce the three-tab account layout.
- Published the ETF-enabled parameters under CN v10 and US/HK v7, inheriting
  audited drawdown high-water marks and approved Kelly samples from v9/v6;
  missing frozen baselines remain skippable while malformed baselines fail.
- Allowed Dashboard acceptance to skip only a genuinely absent completed-date
  frozen drawdown baseline while preserving visible market-level evidence;
  Futu/calendar outages still block, malformed baseline artifacts still fail,
  and runtime entry protection remains fail-closed.
- Silenced external cumulative-drawdown alerts during deployment acceptance and
  consolidated real multi-market failures into one actionable Chinese message
  without weakening fail-closed entry controls. Focused/full tests pass; the
  live acceptance-actor preflight still fails closed on the existing
  same-version parameter-mismatch gate.
- Removed the duplicate current-strategy parameter table from trend review
  pages and kept the folded report discipline as the single rule surface.
  Dashboard current discipline now uses the configured CN/HK/US stock-and-ETF
  candidate pools even when the selected report predates ETF integration;
  frozen historical report parameters remain unchanged.

## 2026-07-25

- Expanded trend selection to mainland-China, US, and Hong Kong ETFs: CN v9
  now admits eligible ETF-fund candidates while preserving historical replay;
  US loads the fixed ETF warm-to-hot pool; HK resolves its dynamic warm-to-hot
  child from the stable ETF root and treats no match as an empty candidate set.
  Verified focused/full tests and the live supplier pool resolution.
- Changed the Dashboard discipline cards and acceptance checks to use the
  current market strategy version instead of obsolete frozen report rules,
  while retaining frozen parameters for historical audit and legacy actions.
- Prevented once-only US/HK protection checks from sleeping until the next
  market open on weekends or holidays, so controller heartbeats and
  reconciliation continue while markets are closed.
- Published CN v9, US v6, and HK v6 trend discipline: A-share hot/boiling
  entries now share the 4% ceiling; new reports no longer create 30% overheat
  trims or trailing-line raises; danger, right-side exit, temperature-flat, and
  2×ATR14 protection still sell all. Existing Kelly samples, drawdown state,
  raised protection lines, and frozen partial exits remain compatible.
- Updated Dashboard acceptance to match separated trend reports: report pages no
  longer expect simulation/real-account overlays or execution-status rows;
  backend payload checks remain unchanged. Report-page Playwright checks now run
  independently of controller and simulated-holdings checks, so failures there
  no longer suppress report validation or screenshots.
- Split oversized Trend Animals snapshot queries into cacheable URL-safe batches;
  US industry-context refreshes no longer fail with HTTP 414.

## 2026-07-24

- Reworked the shared CN/HK/US trend-report Dashboard layout to prioritize the
  summary, sell, buy, hold, and industry tables; compacted fields, rendered
  multi-symbol plans as rows, and moved discipline, risk, controller, and audit
  details into closed disclosures with Playwright coverage across all markets.
- Kept the final Dashboard gate strict on weekdays while accepting the latest
  valid frozen market snapshot after the Friday close; added regression coverage.
- Added eligible-industry breadth context and prior-day history to deterministic
  CN/HK/US trend ordering with whole-report legacy fallback; standardized
  independent per-market API-cost reporting and advanced CN v8 / US-HK v5 while
  preserving approved Kelly samples.
- Replaced the long trend-discipline list with frozen lifecycle cards, kept the
  cumulative-drawdown pause visible, and bound authorized late-buy evidence to
  its report and authorization window; verified 3,471 tests and the Dashboard
  acceptance gate (`PASS`) on `f1162e1`.
- Completed the unified CN/US/HK trend-display rollout; final Dashboard
  acceptance passed with 3,423 tests and all three market controllers ready.
- Unified CN/US/HK trend-report buy tables around one column order and explicit
  missing-value labels while preserving market-specific discipline and audit
  sections; verified the focused Dashboard web and acceptance suites.
- Normalized legacy US/HK trend-report market-cap and daily-turnover fields to
  the fixed CNY-亿元 display contract, including risk-skip rows, and made
  incomplete buy cells explicitly report 数据未提供; verified focused Dashboard
  projection and web suites.
- Hardened the already-authorized CN same-day late-buy audit so its immutable
  missed event filename/body digest and subsequent fill remain verifiable after
  controller restart.
- Added an append-only CN trend-report revision migration that can select an
  already delivered report without rewriting the original revision completion
  or rerunning the report.
- Unified CN/US/HK trend entry and flat-temperature exit discipline; US/HK now
  use frozen local-currency-to-CNY thresholds, industry snapshots, and v5
  cold-start samples while CN retains its v4+v7 exception. Verified the full
  test suite, real US report/evidence, and current review projections.

## 2026-07-23

- Added the current CN trend rules as v7: accept warm, hot, or boiling
  industries, remove the static CNY 200 filter-price cap, and retain
  ATR14-based protection/risk sizing. As a one-time exception, its Kelly
  sample pool inherits only approved CN v4 samples and excludes v1, v5, and v6;
  hardened live acceptance against duplicate terminal action observations.

- Reworked the A-share trend candidate audit into a desktop comparison table
  and mobile cards that show each reported exclusion's actual value against the
  frozen strategy requirement, retain historical ATR explanations, and expose
  unknown rule codes without changing candidate selection or execution.

- Unified CN/HK/US Trend Animals `boiling`/`champagne` exits: the first signal
  per full position lifecycle trims 30% in SIMULATE (market-lot rounded), while
  protection, danger, right-side exit, and CN temperature-flat still sell all;
  real accounts remain manual. Verified the Dashboard acceptance gate (`PASS`)
  on deployed SHA `b6b94ce` and `3166` tests after merging to `main`.
- Fixed Dashboard manual refresh to invalidate and reload the active Tiger,
  Phillips, or Eastmoney simulated-holdings view, so post-report additions do
  not remain stale; verified `3026` tests, live Dashboard/API refreshes, and
  the Dashboard acceptance gate (`PASS`) on deployed SHA `3ed7ec3`.
- Consolidated Feishu trend notifications: retained A/B1/C routing, grouped
  actionable order failures by market, side, and status, and merged OpenD
  connectivity/rate-limit incidents across CN, HK, and US while preserving
  per-market order types.
- Added persisted one-retry Feishu delivery, legacy-safe A7 review routing,
  frozen deadline-group retry, and bounded local-channel delivery; verified
  `3085` automated tests on merged `main`.

## 2026-07-22

- Replaced separate trend report and watcher jobs with one resilient controller
  per market: only the designated executor host can generate reports or submit
  orders, failed reports retry, incomplete actions reconcile by stable broker
  identity, and duplicate orders are rejected before submission.
- Retired the TradingAgents daily-report dependency, added state-change/cooldown
  alert suppression, isolated quote failures by market, and made Dashboard
  simulation-versus-real-account comparisons and numeric precision truthful.
- Verified `3025` tests, live controller and Dashboard processes, desktop/mobile
  flows, and the Dashboard acceptance gate (`PASS`) on deployed SHA `2f51376`.

## 2026-07-20

- Added fixed-risk and conservative Kelly sizing to frozen CN/HK/US trend
  reports, with 0.4% entry risk, a 4% portfolio budget, a 1% abnormal-loss
  buffer, a 5% drawdown limit, and Kelly restricted to reducing new-entry risk.
- Added rolling simulation/actual win-rate and payoff statistics, statement/API
  updates, read-only real-account comparison, and truthful execution-day status;
  verified `2695` tests, live account/API refreshes, desktop/mobile flows, and
  the Dashboard acceptance gate (`PASS`) on the deployed Git SHA.

## 2026-07-18

- Closed the CN/HK/US trend-simulation execution loop with idempotent Futu
  market orders, buy/sell recovery, partial-fill and failure status, and
  pre-close incomplete-execution alerts linked back to each frozen report.
- Added same-level real holdings, simulated holdings, trend report, and review
  views with attributed simulated positions and persistent immutable report
  history; verified `2486` tests, live account/API refreshes, desktop/mobile
  flows, and the Dashboard acceptance gate (`PASS`) on the deployed Git SHA.

## 2026-07-16

- Added desktop-only, right-aligned Phillips and Eastmoney statement uploads
  with local PDF validation, transactional per-broker replacement, and closed
  Eastmoney position handling; verified the real 2026-07-16 upload-to-render
  Playwright flow, `2243` tests, and the Dashboard acceptance gate (`PASS`).
- Unified A-share, US, and HK trend reports around the same action-first desktop
  tables and mobile cards while retaining market-specific facts, excluded closed
  zero-quantity positions from Dashboard holdings, and expanded real acceptance
  across all three markets; verified `2200` tests and the Dashboard acceptance
  gate (`PASS`).

## 2026-07-15

- Decoupled US/HK/CN trend actions from account snapshot freshness, replaced
  the 1% trial sizing with a 4% fallback weight, kept malformed accounts fail
  closed, and published the corrected HK revision without resending Feishu;
  verified `2059` tests and the Dashboard acceptance gate (`PASS`).
- Made each US holding display and value one Futu-selected overnight, premarket,
  regular, or after-hours price with a compact session-colored label, truthful
  fallback text, correct standard-option valuation, and two-cycle acceptance
  coverage; verified `2004` tests and the Dashboard acceptance gate (`PASS`).
- Moved Open Trader voice playback to the XiaoAI speaker's native TTS over
  serialized SSH calls, removing the runtime dependency on the Xiaozhi HTTP/TTS
  stack while preserving the existing alert allowlist and quiet hours; verified
  with the full test suite, live test/A-share/HK/US playback after stopping the
  old runtime, and the Dashboard acceptance gate.
- Restored XiaoAI voice playback for queued Open Trader notifications by sending
  explicit external TTS start/stop state without opening a conversation or stop
  listener; verified the test, A-share, HK, and US templates on the live speaker
  with one queue submission each and no notification retry.
- Added the Eastmoney A-share trend workflow: cached Trend Animals signals,
  Futu protection-line monitoring, frozen Markdown/JSON reports, and a Chinese
  operation-first Feishu checklist for manual execution.
- Made intraday alerts retry until both Feishu and macOS receive a protection
  trigger, without repeating facts or alerting positions already removed from
  the account; verified `1859` tests and the final Dashboard acceptance gate
  (`PASS`).

## 2026-07-14

- Grouped holdings by broker account with strategy-horizon labels, split account
  and whole-portfolio weights into separate columns, and added distinct low-
  saturation broker colors to account headers and strategy summaries while
  keeping holding tables white; verified merged `main` with `1622` tests and
  the full Dashboard acceptance gate (`PASS`) on a dedicated port.
- Added one daily decision plan per holding with a 10% position cap, repeatable
  condition notifications, mandatory benchmark backtest gates, and non-executable
  fallback evidence showing maximum drawdown, Sharpe, and Calmar ratios; Dashboard
  acceptance now rejects missing risk metrics or K-line current prices.
- Replaced AKShare with Futu OpenD as the sole A-share real-time and historical
  market-data source across Dashboard quotes, backtests, watches, and T signals;
  verified 26/26 live quotes and the full Dashboard acceptance gate (`PASS`).

## 2026-07-13

- Refreshed the Dashboard command-center styling without changing its displayed
  data contract, and added configurable acceptance URL/log settings so isolated
  worktrees can be verified on a separate port.
- Replaced the stale Phillips snapshot with the latest archived 2026-07-10
  statement, using its authoritative HKD base cash total and excluding closed
  zero-value positions; the Dashboard now reports HKD 628,554.06 total assets.
- Made Dashboard acceptance verify the latest archived Phillips PDF instead of
  fixed portfolio row counts, preserved partial-source results with visible
  failures, and verified merged `main` with `1504` tests plus desktop/mobile
  acceptance (`PASS`).

## 2026-07-12

- Added optional Eastmoney statement path and PDF password loading from the
  existing local premarket environment file, while keeping explicit CLI paths
  authoritative and secrets outside version control.
- Imported the encrypted Eastmoney statement into the unified portfolio source,
  restoring five A-share holdings and one CNY cash row alongside the existing
  broker data.
- Restarted the live Dashboard on port `8766` and verified the merged `main`
  with `1445` passing tests plus desktop/mobile Playwright acceptance (`PASS`)
  against all 33 portfolio rows.
- Kept pending Kelly exits available when unified strategy stats are missing,
  malformed, stale, or incomplete, while suppressing entries until stats recover.
- Bound entry risk approval to the current validated trade evidence and strategy
  stats through exact timestamps, parameter provenance, and a canonical SHA-256
  evidence digest; restored the original two-decimal trade-sample rounding rules.
- Required unified strategy stats to cover every currently configured experiment
  before any entry can pass risk, while preserving exit approval on config/stats
  failures and isolating provenance validation from optional order artifacts.
- Changed pending-entry lifecycle and intent text to state that sizing and risk are
  still pending, removing pre-risk percentage and approval claims from artifacts
  and the dashboard.

## 2026-07-11

- 将 Kelly 交易证据与运行时 `kelly_strategy_stats.json` 分离，让仪表盘与订单
  仓位统一使用同一策略统计源，并在统计缺失、无效、过期或不完整时关闭入场
  路径（fail closed）。
- Completed the Kelly trade-sample closed loop on `main`: synced paper orders can
  now generate `kelly_trade_samples.json`, overlay per-strategy sample stats in
  Kelly Lab, and show the parameter source plus skipped-order count in the
  dashboard.
- Kept sample artifacts out of producer command dependencies so rebuilding order
  intents, strategy capital, or trade samples is not blocked by stale/corrupt
  sample stats.
- Verified on merged `main` with focused Kelly/dashboard pytest coverage
  (`134 passed`), Kelly Playwright (`1 passed`), `compileall`, `git diff --check`,
  and a restarted live dashboard on port `8766`.

## 2026-07-10

- Fixed the daily US/HK premarket workflow so non-dry-run automation refreshes
  the live Futu and Tiger portfolio before generating premarket advice and trade
  actions, preventing stale holdings from producing false manual-review
  blockers.
- Changed single-share trim sizing so a triggered `TRIM` action on a 1-share
  holding produces a 1-share ready action instead of rounding to zero and
  requiring manual review.
- Verified on local `main` with the full pytest suite, replayed the 2026-07-09
  US blocker scenario as `ready=2 review=0`, and confirmed the US launchd
  premarket job was not running stale code.
- Added the Kelly strategy lab workflow for paper-trading experiments, including
  strategy details, symbol-level lifecycle states, Kelly parameter derivation,
  risk-checked order intents, execution records, and Futu order linkage.
- Connected Futu SIMULATE order execution and order sync so submitted paper
  orders can be attributed back to strategy samples and used for future Kelly
  parameter updates.
- Added explicit Futu trading-market selection for HK, US, and CN simulate
  accounts so paper-order sync and execution target the intended market account.
- Enforced single-market Kelly paper experiments with fixed per-strategy
  simulated budgets of `30000 USD`, `200000 HKD`, and disabled `150000 CNY`,
  split mixed-market mock data, and blocked cross-market order intents before
  execution.
- Added automatic Futu SIMULATE market routing for Kelly paper-order sync and
  execution so commands follow experiment/order markets by default while still
  allowing manual `--trd-market` overrides.
- Added strategy-level Kelly capital snapshots, capital-aware order risk checks,
  and a Kelly Lab capital panel showing occupied, available, and next-order
  impact per strategy.
- Added Kelly trade sample generation from synced Futu paper orders, including
  derived win rate, payoff ratio, Kelly sizing stats, and dashboard source
  visibility.
- Verified with focused Kelly/dashboard pytest coverage, compile checks,
  `git diff --check`, live Futu SIMULATE HK order execution/sync, and live
  US/CN simulate-account order probes.
- Added a mandatory `make acceptance` Dashboard gate with PASS/FAIL/BLOCKED
  results across tests, real data, refresh stability, process version, logs,
  and desktop/mobile Chrome flows; fixed OTHER holdings breaking Dashboard loads
  and Tiger refreshes converting preserved CN rows to OTHER. The gate now also
  checks the full 33-row portfolio, seven Phillips-linked rows, and the exact
  Eastmoney statement total; live broker refreshes fail closed and restore the
  prior CSV if they would remove another broker's holdings. Browser verification
  ignores Chrome's unattributed favicon 404 while still failing every observed
  business API or page-resource HTTP error.
- Fixed newer single-broker imports hiding older brokers' account details by
  loading the latest detail snapshot per broker; acceptance now rejects an
  empty Phillips account card in both the API payload and rendered page.
- Added password-prompted Eastmoney A-share statement imports using an explicit
  month-end CNY/HKD rate, plus AKShare daily prices for standard-strategy research.
- Kept the Dashboard holdings layout unchanged while adding the existing A-share
  market and Eastmoney broker filters.
- Added one global dashboard workspace for read-only standard-strategy research
  across current holdings and watchlist symbols, with trend-pullback,
  breakout-momentum, and range-mean-reversion strategies.
- Added buy-and-hold and market-index comparisons, explicit actual data dates,
  fixed cost and sizing assumptions, and standalone auditable artifacts.
- Preserved real nonzero Futu daily volume for breakout research and fixed the
  price/action chart to render the serialized close-price series.
- Verified with `192` focused and `1134` full pytest tests, three fresh real 1Y
  MSFT/Futu API runs with 320 positive-volume MSFT and SPY rows, and separate
  Playwright submissions for all three strategies proving visible equity,
  price-path, and action-marker geometry with no console or network errors.

## 2026-07-11

- Added a dashboard backtest price-sync status line so operators can see when
  automatic price backfill succeeds or fails during page load.

## 2026-07-10

- Added a dashboard action to fetch missing backtest price CSVs from Futu daily
  K-line data and refresh the per-holding backtest readiness state.
- Marked sell-side, hold, and underweight trading plans as unsupported by the
  first buy-side backtest engine instead of showing misleading missing fields.
- Added sell-side trading-plan backtests for underweight/reduce/trim/sell
  ratings, seeded from current dashboard holding quantity and verified through
  pytest plus a local dashboard click check.
- Added a dashboard backtest-status filter so operators can isolate holdings
  that are ready to run, missing prices, missing plan fields, or unsupported.
- Added live counts to the dashboard backtest-status filter, scoped by the
  current market and broker filters.
- Made dashboard loads automatically fetch missing backtest daily K-line price
  CSVs through Futu so operators do not need to manually fill price data first.
- Removed the manual backtest price-fetch button from the dashboard detail view;
  missing price data is now handled by automatic dashboard loading.

## 2026-07-09

- Added a read-only `run-backtest` MVP for active trading-plan rows, producing
  trades, equity curve, metrics, and Markdown report artifacts without updating
  `data/latest` or placing orders.
- Added dashboard backtest entry buttons that open a per-holding回测详情 view
  without showing backtest metrics on the main holdings table.
- Added a dashboard-only backtest run action that uses the local latest trading
  plan and `data/prices/<market>/<symbol>.csv`, then refreshes the detail view.
- Added dashboard backtest readiness details so operators can see missing plan
  fields and price CSV paths before running a backtest.
- Documented the first backtest workflow in both READMEs.
- Verified with focused backtest/dashboard pytest coverage, the full pytest
  suite, and a local dashboard click check on `127.0.0.1:8766`.

## 2026-07-04

- Added Futu daily-K Bollinger fact generation for dashboard K-line cards, fixed
  Futu/Tiger live-sync asset-class inference for type-less positions, and
  removed the duplicate technical-fact grid from those cards after live
  dashboard verification across all current HK/US eligible holdings.
- Added a fixed Bollinger-band display in the dashboard K-line card, with red
  upper-band risk, green lower-band opportunity, and neutral middle-range
  states.
- Backfilled Bollinger facts from real HK/US latest TradingAgents reports when
  model extraction fails, and verified the live dashboard renders those facts
  without `undefined`.
- Stabilized the daily HK/US premarket workflow around `portfolio.csv` holdings,
  report-symbol filtering, non-blocking facts/summary artifacts, configurable
  worker concurrency, and Feishu start/completion notifications.
- Verified with the full pytest suite and `git diff --check`.

## 2026-07-03

- Added holdings-table 做T signal details with fixed ratio sizing, signal
  evidence, precondition checks, notification timeline, and session-gated pulse
  highlighting.
- Enabled HK 做T signal generation through Futu realtime subscriptions for
  1-minute K lines, 5-minute K lines, and order book data.
- Changed 做T Feishu alerts to one structured Chinese message per symbol with
  action, ratio, status, conclusion, numbered evidence, and timestamp.
- Verified with the full pytest suite, Playwright against the local dashboard,
  live HK Futu signal generation, and a real Feishu app notification send.

## 2026-07-02

- Reworked the dashboard holdings table around the operator fields: quantity,
  cost price, live price, USD/HKD market value, portfolio weight, and P/L.
- Split holdings into `美股正股`, `美股期权`, `港股正股`, and `港股期权`
  sections, kept each section sorted by portfolio weight, and kept broker
  context inside the trading decision detail.
- Added the Futu anomaly signal card to the trading decision detail so
  technical, capital-flow, and derivatives anomaly signals display in Chinese
  without leaking raw enum/schema text.
- Verified with focused dashboard/Futu facts pytest, live local dashboard
  deployment on `127.0.0.1:8766`, and Playwright checks for section order,
  section weight sorting, detail expansion, and the anomaly signal card.

## 2026-07-01

- Fixed Phillips statement parsing for `UT OTCU` money-market-fund rows so the
  Phillip HKD Money Market Fund is included in monthly holdings refreshes.
- Refreshed the local Phillips monthly baseline from the 2026-06 statement and
  verified live Futu/Tiger sync preserves the updated statement rows.
- Verified with focused parser/account-sync tests and dashboard API checks for
  `2026-06 月结单导入`.

## 2026-06-30

- Canonicalized `portfolio.csv` grouping so daily HK/US workflows consume
  deduplicated current holdings instead of repeated broker rows.
- Hardened Futu and Tiger portfolio sync merges, including malformed cash rows,
  stale Tiger FX rows, mixed-broker fallback safety, and multi-broker cash detail
  preservation.
- Stabilized daily startup by clearing successful run locks and adding bounded
  OpenAI-compatible request timeouts for classifier, facts, and TradingAgents
  summary post-processing.
- Added blocker notifications when TradingAgents advice, trading plans, or
  summaries degrade to fallback/error so missing US reports are visible to the
  operator.
- Verified with live Futu/Tiger syncs, `data/latest/portfolio.csv` duplicate
  count `0`, US daily runner `success / ready`, local dashboard deployment on
  `127.0.0.1:8766`, Playwright desktop/mobile checks, and `832` passing tests.

## 2026-06-23

- Added fixed TradingAgents decision facts for dashboard display:
  `趋势 / K 线` uses `趋势`, `位置`, `动能`, `关键位`, `风险`;
  `新闻 / 舆论` uses `方向`, `变化`, `催化`, `风险`, `热度`.
- Added LLM extraction and validation for `decision_facts.json`, with per-module
  fallback to `缺失` when a module cannot be extracted safely.
- Wired decision facts into the daily premarket pipeline and dashboard payload,
  including source-hash freshness checks.
- Updated the local dashboard cards so missing fixed fields show `缺失` instead
  of explanatory filler or raw English TradingAgents prose.
- Documented local dashboard deployment on port `8766` and added structured API
  checks for `SOXX` decision facts.
- 2026-08-08: Trend statistics forced refreshes now retain an fsynced append-only audit trail with fail-closed history validation, preserving one-attempt evidence across retries.
