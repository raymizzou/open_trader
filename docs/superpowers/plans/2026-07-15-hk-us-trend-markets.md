# 港美趋势交易接入实施计划

> 按 `superpowers:executing-plans` 分批执行；每项行为变更先写失败测试，再做最小实现。

**目标：** 接入趋势动物港股/美股温转热候选池，分别为辉立港股和富途美股生成独立趋势操作计划与盘中保护线监控，并在首页账户卡展示紧凑摘要。

**架构：** 保留 `a_share_trend.py` 作为趋势规则与报告模型的单一来源，仅为市场、整手和账户新鲜度增加参数。新增 `market_trend.py` 作为港美账户/API/调度薄适配层；将现有 watcher 参数化后由 `market_trend_watch.py` 提供港美会话。CLI 和 launchd 只负责注入 `US|HK`。

**技术栈：** Python 3.12、现有 Trend Animals/Futu OpenD 客户端、pytest、macOS launchd、现有原生 Dashboard HTML/CSS/JS。

## 任务 1：市场配置、符号与行情原语

**文件：**
- 修改 `src/open_trader/daily_premarket.py`
- 修改 `src/open_trader/futu_quote.py`
- 修改 `src/open_trader/a_share_trend.py`
- 修改 `config/daily_premarket.env.example`
- 测试 `tests/test_daily_premarket.py`
- 测试 `tests/test_futu_quote.py`
- 测试 `tests/test_a_share_trend.py`

1. 写失败测试：解析 US/HK tmId 列表与初始受管标的；通用 Futu 交易日历；港股 lot size；US/HK Trend Animals 符号、候选门槛、整股/整手和陈旧辉立账户仍可给建议。
2. 运行这些精确测试并确认 RED。
3. 最小实现：新增配置字段；`get_trading_days(market=...)` 与 `get_lot_sizes(...)`；为趋势公共函数增加默认保持 CN 行为的 `market`、`lot_sizes`、`require_fresh_account` 参数。
4. 运行精确测试并确认 GREEN。

## 任务 2：港美报告工作流

**文件：**
- 新增 `src/open_trader/market_trend.py`
- 新增 `tests/test_market_trend.py`

1. 写失败测试覆盖：Futu US 实时账户刷新、辉立 HK 日结单账户加载、仅管理显式/历史建议标的、候选池并集去重、API 更新日期门槛、最多一小时每 10 分钟重试、报告/JSON/状态/事件路径完全隔离、Feishu 完整报告和失败通知。
2. 运行测试确认 RED。
3. 实现 `run_market_trend_report(market="US"|"HK")`：US 使用富途实时账户，HK 使用最新辉立结单；池 ID 为列表；报告中标记账户源日期及辉立人工核对要求；将建议买入加入 `managed_symbols`，以后成交后自动纳管。
4. 运行测试确认 GREEN。

## 任务 3：港美保护线 watcher

**文件：**
- 修改 `src/open_trader/a_share_trend_watch.py`
- 新增 `src/open_trader/market_trend_watch.py`
- 修改 `tests/test_a_share_trend_watch.py`
- 新增 `tests/test_market_trend_watch.py`

1. 写失败测试：HK 09:30–12:00/13:00–16:00、US 纽约 09:30–16:00（含 DST）、5 秒轮询、常规盘外休眠、一次触发、OpenD 中断通知/60 秒重连/恢复、缺行情不视为安全。
2. 运行测试确认 RED。
3. 将现有 watcher 参数化账户加载、市场符号、交易日历、会话与通知标签；保持 A 股入口兼容；新增港美薄入口。
4. 运行新旧 watcher 测试确认 GREEN。

## 任务 4：CLI 与独立 launchd 调度

**文件：**
- 修改 `src/open_trader/cli.py`
- 新增 `ops/launchd/com.open-trader.trend-market-report.plist.template`
- 新增 `ops/launchd/com.open-trader.trend-market-watch.plist.template`
- 修改 `scripts/install_daily_premarket_launchd.sh`
- 修改 `scripts/uninstall_daily_premarket_launchd.sh`
- 修改 `tests/test_premarket_cli.py`
- 修改 `tests/test_daily_premarket.py`

1. 写失败测试：`trend-market-report --market US|HK`、`watch-trend-market --market US|HK`，以及 HK 18:00、US 09:00 报告任务和各自独立 watcher 任务。
2. 运行测试确认 RED。
3. 实现 CLI 与四个独立 launchd job；报告自身只重试到 HK 19:00 / US 10:00。
4. 运行测试与 `plutil -lint` 确认 GREEN。

## 任务 5：首页紧凑趋势摘要

**文件：**
- 修改 `src/open_trader/dashboard.py`
- 修改 `src/open_trader/dashboard_static/dashboard.js`
- 按需修改 `src/open_trader/dashboard_static/dashboard.css`
- 修改 `tests/test_dashboard.py`
- 修改 `tests/test_dashboard_web.py`
- 修改 `tests/test_dashboard_acceptance.py`

1. 写失败测试：Dashboard 状态读取 US/HK 最新 JSON 与最近保护事件；富途、辉立账户卡显示数据日、运行状态、买/卖/人工复核数量和最近保护提醒；Tiger/Eastmoney 保持现状；移动端无新增横向滚动。
2. 运行测试确认 RED。
3. 实现只读摘要和现有账户卡内的紧凑渲染，不新增页面或导航。
4. 运行 Dashboard 精确测试确认 GREEN。

## 任务 6：审查、全量验证与部署

1. 运行受影响测试组、格式/静态检查和全量 `pytest`，记录精确输出。
2. 按 `code-review` 技能审查规格与仓库标准，修复发现并复验。
3. 提交功能分支，把精确提交安全集成到主工作树，保留用户无关改动。
4. 在主工作树运行真实 API/OpenD 报告预检与 watcher `--once`；检查并重启相关 `launchctl`/`screen` 旧进程，核对 PID、工作目录、Git SHA 和新日志。
5. 以 `make acceptance` 作为最后验收门；非 `PASS` 继续修复或报告 `BLOCKED`。
6. `PASS` 后重新部署完全相同的已验收 SHA，验证新 PID、cwd、SHA、新日志及 review URL HTTP 200，再交付 URL。
