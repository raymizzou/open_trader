# Changelog

Every push to `main` must add one dated entry here. Keep entries short and
operator-facing: what changed, which workflow is affected, and what was verified.

## 2026-08-10

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
