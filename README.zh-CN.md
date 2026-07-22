# Open Trader

[English](README.md)

Open Trader 是一个本地优先的投资组合分析工具，用于把券商月结单和实时行情转成结构化持仓、盘前交易建议、交易计划和可人工复核的行动报告。

它面向“人仍然掌控最终决策”的工作流：Open Trader 负责读取数据、调用分析模型、通过
Futu OpenD 检查实时行情并写出报告。趋势动物控制器还可以从唯一、明确指定的执行主机
提交有防重保护的富途模拟订单。

## 功能

- 导入每月券商账单，生成标准化持仓 CSV。
- 从富途真实证券账户只读拉取实时持仓和现金，并与其他券商月结单数据合并为统一
  portfolio CSV。
- 从老虎 Tiger OpenAPI 只读拉取实时持仓和现金，并在保留非 Tiger 行的基础上更新统一
  portfolio CSV。
- 使用 TradingAgents 和 DeepSeek 为每个标的生成盘前建议。
- 保留原始模型输出和标准化 trader 模板，便于追溯。
- 从 TradingAgents 建议和报告输出中抽取 K 线技术事实，并缓存为
  `technical_facts.json`。
- 当每日运行超过硬截止时间或单个标的分析失败时，自动 fallback 到该标的最近一次成功建议。
- 从建议摘要生成机器可读的 trading plan。
- 通过 Futu OpenD 实时行情检查 trading plan。
- 生成可复核的 trade action CSV 和 Markdown 报告。
- 为老虎生成美股趋势报告，为辉立生成港股趋势报告。
- 在指定主机上为每个市场运行一个可自我对账的趋势动物控制器，并在每次模拟下单前先核对券商事实。
- 在仪表盘中把富途展示为只读的美股/港股期权关注聚合入口。
- 查看本地实时持仓仪表盘，支持实时行情刷新和陈旧数据警告。
- 在 macOS 上通过 `launchd` 自动运行每日盘前流程。

## 安全说明

本项目不是投资建议，也不能替代人工复核。模型输出可能不完整、过期或错误。任何投资决策前，都应人工检查生成的建议、计划、行情检查结果和交易动作。

普通流程和仪表盘不会提交订单。趋势动物控制器只允许在配置的执行主机上提交富途模拟
订单，绝不会提交真实资金订单。启用自动化前必须核对执行主机名和控制器状态。

## 快速开始

创建 Python 3.12 虚拟环境并安装项目：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

准备每日自动化配置：

```bash
cp config/daily_premarket.env.example config/daily_premarket.env
```

编辑 `config/daily_premarket.env`，填入本地配置：

```env
DEEPSEEK_API_KEY=your-deepseek-api-key
OPEN_TRADER_REPO=/path/to/open_trader
OPEN_TRADER_PYTHON=/path/to/open_trader/.venv/bin/python
OPEN_TRADER_FUTU_HOST=127.0.0.1
OPEN_TRADER_FUTU_PORT=11111
```

启动并登录 Futu OpenD，然后确认行情可用：

```bash
.venv/bin/python -m open_trader check-futu-quotes \
  --portfolio data/latest/portfolio.csv
```

先分别跑一次港股和美股每日盘前 dry run：

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env \
  --dry-run

.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

再跑一次指定市场的真实检查：

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env
```

## 配置

每日自动化的本地配置文件是：

```text
config/daily_premarket.env
```

这个文件已被 Git 忽略，不能提交。模板文件是：

```text
config/daily_premarket.env.example
```

关键配置：

- `DEEPSEEK_API_KEY`：TradingAgents 和变化分类器都会使用。
- `OPEN_TRADER_REPO`：本仓库的绝对路径。
- `OPEN_TRADER_PYTHON`：定时任务使用的 Python 可执行文件。
- `OPEN_TRADER_TIMEZONE`：默认 `Asia/Shanghai`。
- `OPEN_TRADER_DEADLINE`：美股每日硬截止时间，默认 `21:10`。
  港股每日流程固定使用 Asia/Shanghai `09:00` 截止时间。
- `OPEN_TRADER_FUTU_HOST`：Futu OpenD 地址，通常是 `127.0.0.1`。
- `OPEN_TRADER_FUTU_PORT`：Futu OpenD 行情端口，通常是 `11111`。
- `OPEN_TRADER_TREND_EXECUTOR_HOST`：唯一允许生成趋势报告并提交模拟趋势订单的主机，其值必须与该机器的 `hostname` 完整输出一致。
- `OPEN_TRADER_CLASSIFIER_MODEL`：默认 `deepseek-v4-flash`。

Tiger OpenAPI 账户同步会读取老虎官方
`~/.tigeropen/tiger_openapi_config.properties`、CLI `--account` 或
`TIGEROPEN_*` 环境变量。支持的环境变量包括 `TIGEROPEN_TIGER_ID`、
`TIGEROPEN_ACCOUNT`、`TIGEROPEN_PRIVATE_KEY_PATH`、`TIGEROPEN_PRIVATE_KEY`，
以及可选的 `TIGEROPEN_SECRET_KEY`、`TIGEROPEN_TOKEN`。优先使用官方
properties 文件或 private key path，不建议把原始私钥直接放进环境变量。

## 常用流程

### 导入月度账单

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

主要输出：

```text
data/latest/portfolio.csv
```

Futu 和 Tiger 的当前持仓通过 live account sync 更新，不再依赖月结单导入。

东方财富 A 股对账单使用月末 CNY/HKD 汇率导入；例如 2026-06-30 香港金管局月末汇率为
`1.1549`：

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-07 \
  --eastmoney /Users/ray/Downloads/电子对账单.pdf \
  --cny-hkd 1.1549 \
  --fx-date 2026-06-30 \
  --data-dir data \
  --update-latest
```

`--fx-date` 记录该官方汇率的实际日期，避免把导入月份的月末误记为汇率日期。命令会
在终端提示输入 PDF 密码，不应把密码写进命令、配置或日志。导入器只读取对账单
首页的当前汇总表，不导入成交明细；`--update-latest` 会保留其他券商行，并替换已有的
东方财富行。标准策略研究会按需使用 AKShare 获取 A 股日线，AKShare 不需要密钥。

### 手动运行盘前建议

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --max-workers 3 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1
```

盘前建议生成后，会从每条 TradingAgents 建议和报告中抽取 K 线技术事实。抽取结果
写入 `technical_facts.json`；按市场运行的每日流程成功后，会把它和其他 latest
产物一起按市场 promotion。

### 回填 K 线技术事实

如果需要从已有 advice CSV 重新生成技术事实，可以运行：

```bash
open-trader extract-technical-facts \
  --advice data/runs/2026-06-19/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-19 \
  --market US \
  --update-latest
```

使用 `--market HK` 或 `--market US` 时，dated cache 会写入对应市场的 run
目录；加上 `--update-latest` 后，会 promotion 到对应市场的 latest 路径，例如
`data/latest/HK/technical_facts.json` 或
`data/latest/US/technical_facts.json`。

### 固定交易事实字段

按市场运行 TradingAgents 后，Open Trader 会为看板抽取固定中文字段：

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_facts.json
data/latest/<MARKET>/decision_facts.json
```

手动命令：

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

看板的 `趋势 / K 线` 和 `新闻 / 舆论` 只展示固定字段。所有标的使用同一套字段：

- `趋势 / K 线`：`趋势`、`位置`、`动能`、`关键位`、`风险`
- `新闻 / 舆论`：`方向`、`变化`、`催化`、`风险`、`热度`

缺少字段值时显示 `缺失`，不会在这些插件字段中直接展示 TradingAgents 英文原文，
也不会展示“未提及”“无有效证据”“人工复核”这类解释性占位文案。

### 生成 Trading Plan

```bash
.venv/bin/python -m open_trader build-trading-plan \
  --advice data/latest/trading_advice.csv \
  --data-dir data \
  --date 2026-06-16
```

### 检查 Futu 实时行情

```bash
.venv/bin/python -m open_trader check-futu-plan \
  --plan data/latest/trading_plan.csv
```

如果命令显示已经连接到 `127.0.0.1:11111`，但快照接口报 `网络中断`，先确认 OpenD 的行情服务器是否登录：

```bash
.venv/bin/python - <<'PY'
from futu import OpenQuoteContext
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
ret, data = ctx.get_global_state()
print(ret, data)
ctx.close()
PY
```

重点看 `qot_logined`。如果 `trd_logined=True` 但 `qot_logined=False`，说明交易服务器已登录，但行情服务器未登录，`get_market_snapshot()` 会返回 `网络中断`。恢复步骤：

```bash
ps aux | grep -i FutuOpenD
pkill -f FutuOpenD
ps aux | grep -i FutuOpenD
open /Applications/FutuOpenD_10.7.6718_Mac/FutuOpenD.app
```

重新登录 OpenD 后，再跑上面的 `get_global_state()`。看到 `qot_logined=True` 后，再执行 `check-futu-plan`；成功输出应包含 `last_price=...`。

### 同步 Tiger OpenAPI 持仓

Tiger 实时账户同步是只读流程，不会下单。真实持仓和现金直接来自
Tiger OpenAPI，不依赖 `portfolio.csv` 作为数据源。`data/latest/portfolio.csv`
只是默认合并基线：存在时用来保留非 Tiger 行并替换 Tiger-only 行；缺失时仍会
生成只包含 Tiger 实时数据的 dated portfolio。月度 `import-statements` 只处理仍依赖
statement 的券商；Tiger sync 是当前账户刷新流程。

```bash
.venv/bin/python -m open_trader check-tiger-account

.venv/bin/python -m open_trader sync-tiger-portfolio \
  --date 2026-06-19
```

上面的 sync 命令默认不会更新 latest，是用于复核的 no-latest run。先检查
`data/runs/2026-06-19/tiger_account_snapshot.json`、
`data/runs/2026-06-19/portfolio.csv` 和
`reports/tiger_account/2026-06-19.md`。确认后再 promotion：

```bash
.venv/bin/python -m open_trader sync-tiger-portfolio \
  --date 2026-06-19 \
  --update-latest
```

如果现有聚合行同时包含 Tiger 和其他券商，流程会停止并要求人工检查，不会自动拆分。
如果 Tiger 返回的数据格式异常，流程会先写出 dated artifacts 和报告，然后阻止 latest
promotion。

### 生成交易动作

```bash
.venv/bin/python -m open_trader generate-trade-actions \
  --plan data/latest/trading_plan.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-16
```

### 回测 Trading Plan

用历史日线 OHLC 行情，对一个 active trading-plan 行做单标的只读回测：

```bash
.venv/bin/python -m open_trader run-backtest \
  --plan data/latest/trading_plan.csv \
  --prices data/prices/US/MSFT.csv \
  --symbol MSFT \
  --market US \
  --date 2026-06-16 \
  --adapter backtrader
```

第一版回测策略刻意保持窄边界：只使用选中 `trading_plan.csv` 行里的 entry zone、
stop loss、targets 和 max weight；从 plan 日期之后的日线开始评估；计入手续费和
滑点；输出独立产物到 `data/backtests/<run_id>/`，并在 `reports/backtests/`
写 Markdown 报告。默认执行后端是 `backtrader`，也保留 `simple` 作为本地回退。
该命令不会下单，也不会更新 `data/latest`。

### 在看板运行标准策略研究

从唯一的全局入口依次进入：`持仓实时看板` → `策略回测` → `当前持仓/自选股` →
`单一标的` → `趋势回调/突破动量/区间均值回归` → `时间范围` → `运行回测`。
工作区刻意隐藏 Backtrader，并把所选策略同时与“买入持有”和市场指数比较：美股默认
使用 `SPY`，港股默认使用 `HK.02800`。

时间范围支持 `6M`、`1Y`、`3Y`、`5Y` 和自定义。Futu 实际可用历史行情可能短于
请求范围，因此每次结果都会明确显示实际数据起止日期，并展示初始资金、策略最大
仓位、手续费和滑点等固定研究假设。每次运行会把清单、信号、交易、净值曲线、指标
和报告写到 `data/backtests/<run_id>/` 与 `reports/backtests/`，不会修改持仓、
trading plan 或订单状态。

标准策略结果仅供研究。自定义策略编辑和自动执行明确不在范围内；看板不会下单。

### 部署本机前端仪表盘

```bash
.venv/bin/python -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --poll-seconds 5 \
  --host 127.0.0.1 \
  --port 8766
```

默认会在本机 `http://127.0.0.1:8765` 提供服务；上面的部署示例固定使用
`http://127.0.0.1:8766`，方便后续一直访问同一个本机地址。仪表盘会读取
`data/latest/portfolio.csv`、`data/broker_positions/` 下的券商明细产物，
以及已存在的最新交易动作和报告。

桌面端的辉立和东方财富账户标题右侧提供 `上传结单`。选择一份不超过 20 MiB 的
PDF 后会立即导入，不再显示预览或二次确认；移动端不提供该入口。服务只接受本机
loopback 请求，并自动读取结单内的完整日期。上传使用固定换算率
`USD/HKD = 7.8`、`CNY/HKD = 1.08`，只整体替换对应券商的数据并保留其他券商；
旧于现有来源日期、无法解析或写入失败的文件不会改变当前持仓。

东方财富 PDF 密码只从 Dashboard 使用的本地配置文件读取：

```text
OPEN_TRADER_EASTMONEY_PDF_PASSWORD=本机密码
```

密码不会提交给浏览器或写入日志。成功导入后，辉立原件归档到
`data/statements/phillips/<完整日期>/statement.pdf`，东方财富原件归档到
`data/statements/eastmoney/<月份>/statement.pdf`；同一期间仅在完整解析和持仓更新
成功后替换旧归档。这个入口不会重跑趋势报告、发送通知或启动 watcher。

生成盘中做 T 信号：

```bash
.venv/bin/python -m open_trader watch-t \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --date 2026-07-02 \
  --market US \
  --session-phase regular \
  --once
```

该命令会写入 `data/runs/<YYYY-MM-DD>/<market>/t_signals.json`，并更新
`data/latest/<market>/t_signals.json`。持仓表每行会显示 `做T` 按钮；展开后可查看
确定性的动作、比例、信号依据、当前状态和提醒 timeline。同一信号周期只提醒一次，
整个流程只读，不会下单。

Futu OpenD 行情可用时，仪表盘会用 OpenD 刷新价格。如果某次刷新失败，它会保留最近一次成功的行情快照，并显示刷新失败或数据陈旧警告，而不是隐藏问题。

仪表盘还会在可用时读取 `technical_facts.json`。标的详情会显示技术事实的生成日期
和底层行情数据日期；`趋势 / K 线` 卡片固定展示日线布林带位置，贴近或超过上轨时标红提示
`回调风险升高`，接近下轨时标绿提示 `低位机会区域`，中轨附近按正常颜色展示。
如果文件缺失、记录缺失、来源 hash 已过期、抽取失败或周期信息不完整，
会把该记录标记为不可用，不会把过期技术事实当作当前数据展示。

仪表盘是只读工具：不会下单，也不会修改数据。

### 投研结论与 LLM 深度讨论

当前端标的存在 `data/research_data/<market>/<symbol>/<date>/` 投研包时，
仪表盘会在标的详情页展示投研结论。

投研包需要包含：

- `dashboard_view.json`：给前端渲染的结论视图。
- `combined_input.json`：TradingAgents 原始输出和本地用户上下文。
- `llm_system_prompt.md`：打开聊天窗口时自动加载的系统提示词。

标的详情页会展示两张结论卡：

- `投研给出的结论`：TradingAgents 原始结论。
- `我和 LLM 探讨后的结论`：点击聊天窗口里的 `生成最终结论` 前显示 `缺失`。

聊天记录保存在 `data/research_chat/sessions/`。生成最终结论后，系统会把
`user_llm_conclusion.json` 写回投研包，并更新该投研包的 `dashboard_view.json`。
这个流程只服务于人工复核，不会下单，也不会修改交易动作文件。

如果希望关闭终端后前端仍继续运行，可以用 `screen` 启动后台会话：

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true

screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766'
```

验证前端部署：

```bash
curl -sS http://127.0.0.1:8766/ | head
curl -sS http://127.0.0.1:8766/api/dashboard | head -c 500
ps aux | rg 'open_trader dashboard'
```

Dashboard 行为改动部署后必须运行统一验收门：

```bash
make acceptance
```

它会运行全量测试，并检查真实 API 数据、一次真实账户与行情刷新、运行目录与
Git SHA、错误日志，以及系统 Chrome 中的桌面和移动端 `A 股` / `东方财富`
筛选流程。只有 `PASS` 可以标记为完成；`FAIL` 必须修复，浏览器不可用则
返回 `BLOCKED`，不能用 curl 或单元测试替代。

也可以用结构化检查确认 API 和 SOXX 决策事实是否存在：

```bash
.venv/bin/python - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8766/api/dashboard", timeout=10) as response:
    payload = json.load(response)

print("holding_count", payload.get("summary", {}).get("holding_count"))
print("detail_available", payload.get("detail_available"))
print(
    "has_soxx_decision_facts",
    any(
        holding.get("market") == "US"
        and holding.get("symbol") == "SOXX"
        and bool(holding.get("decision_facts"))
        for holding in payload.get("holdings", [])
    ),
)
PY
```

需要停止后台仪表盘时：

```bash
screen -S open_trader_dashboard_8766 -X quit
```

## 每日自动化

### 部署每日盘前定时任务

安装 macOS 用户级 `launchd` 定时任务：

```bash
scripts/install_daily_premarket_launchd.sh --dry-run --market all
scripts/install_daily_premarket_launchd.sh
```

默认会安装两条任务：

- `com.open-trader.premarket.hk`：周一到周五 08:00 Asia/Shanghai 运行。
- `com.open-trader.premarket.us`：周一到周五 18:30 Asia/Shanghai 运行。

也可以只安装单个市场：

```bash
scripts/install_daily_premarket_launchd.sh --market HK
scripts/install_daily_premarket_launchd.sh --market US
```

定时任务会显式区分市场：

```text
.venv/bin/python -m open_trader run-daily-premarket --market HK --date today --config config/daily_premarket.env
.venv/bin/python -m open_trader run-daily-premarket --market US --date today --config config/daily_premarket.env
```

港股流程固定使用 09:00 Asia/Shanghai 作为硬截止时间，保证港股开盘前形成可复核状态；美股流程使用 `OPEN_TRADER_DEADLINE`，通常是 21:10 Asia/Shanghai。如果某个标的在截止时间前没有拿到新建议，runner 会复用该标的最近一次成功建议，并把状态标记为 `fallback`。

卸载定时任务：

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

验证定时任务部署：

```bash
launchctl list | rg 'open-trader|premarket'
plutil -lint \
  ~/Library/LaunchAgents/com.open-trader.premarket.hk.plist \
  ~/Library/LaunchAgents/com.open-trader.premarket.us.plist
```

预期应看到这两个已加载的 label：

```text
com.open-trader.premarket.hk
com.open-trader.premarket.us
```

定时任务运行后，可以检查日志：

```bash
tail -n 100 logs/daily_premarket/launchd-HK.out.log
tail -n 100 logs/daily_premarket/launchd-HK.err.log
tail -n 100 logs/daily_premarket/launchd-US.out.log
tail -n 100 logs/daily_premarket/launchd-US.err.log
```

### 运维趋势市场控制器

趋势动物只有一个运维入口：`trend-market`。只在唯一执行主机的本地、Git 忽略配置中写入
完整主机名：

```bash
hostname
# 只在执行主机上设置为上面命令的完整输出：
OPEN_TRADER_TREND_EXECUTOR_HOST=ray-mac
```

未配置或不匹配时，该部署自动进入 `readonly`。只读机器可以展示已有数据，但不生成趋势
报告、不运行趋势控制器、不修改订单、不监控保护线、不抓取收盘事实，也不发送趋势任务
通知。系统不自动故障转移：必须先停止旧执行主机、核对其富途订单和不可变账本，再显式
修改执行主机名，才能提升新主机。

执行主机为 CN、HK、US 各安装一个 `RunAtLoad`/`KeepAlive` 持久控制器。从 worktree
部署时必须显式传入共享配置的绝对路径：

```bash
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --dry-run --trend-only --market all
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
```

执行主机只安装以下三个 label；只读安装会移除所有旧趋势任务和控制器，不安装任何新任务：

```text
com.open-trader.trend-market-controller.cn
com.open-trader.trend-market-controller.hk
com.open-trader.trend-market-controller.us
```

状态、前台运行、报告修订和不确定订单处置都使用同一个命令空间，并显式传入共享配置：

```bash
.venv/bin/python -m open_trader trend-market status --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
.venv/bin/python -m open_trader trend-market run --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
.venv/bin/python -m open_trader trend-market run --market US --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
.venv/bin/python -m open_trader trend-market resolve \
  --market US --execution-date 2026-07-20 --symbol TRV --side buy \
  --resolution confirm-submitted --futu-order-id SIM-42 \
  --actor ray --reason "verified in Futu order history" \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

`status` 只读；`run` 通常由 `launchd` 持有；`run --revision` 只能在执行批次锁定前申请
修订报告。不确定行动只能追加以下一种不可变处置：

- `confirm-submitted`：记录已核实的富途订单 ID，绝不重提。
- `authorize-retry`：确认未提交订单，只授权下一次带新编号的尝试；不得传订单 ID。
- `abandon`：终止行动，不再尝试；不得传订单 ID。

每次处置都要求非空的 actor 和 reason，只追加审计事实，不编辑或删除 intent、券商观察、
result 或旧处置。只有 `authorize-retry` 能让未解决 intent 进入下一次尝试。

控制器会以有界退避持续补同一数据日和执行日的缺失报告，即使买入窗口已经关闭。原子
冻结前失败会重算同一逻辑报告；冻结后的投递失败只重试投递。第一次符合执行条件的检查
会把最新有效报告 SHA 冻结为当天批次；其后的修订只作为异常展示，不能改变已锁批次。

行动身份固定为 `(market, execution_date, symbol, side)`。每次尝试使用带编号、可确定重建
的富途 remark；提交前总是核对本地不可变事实以及富途当前和历史订单。部分成交买单必须
等当前尝试终止，且只能在市场窗口内按冻结股数、金额、现金、手数和风险上限补余量。
窗口关闭后保留已成交仓位，余量只记一次 `missed`，绝不补迟到订单。同日正式卖出和保护
卖出会合并为一个 sell 行动；已有尝试仍活动或状态不明时不得重叠卖出。

迁移必须加 fence：先检查当前任务和进程，再运行 installer。installer 在加载任何新控制器
前必须卸载选中的旧任务，并确认旧 label 和进程都已消失。如果仍有孤儿进程或任一 fence
检查失败，它不会加载新控制器；先停止并诊断孤儿，再重跑 installer。新控制器在任何符合
条件的提交前，会先用富途当前/历史订单对账不可变账本。安装后还要独立确认没有旧报告或
监控进程：

```bash
launchctl list | rg 'com\.open-trader\.(trend|premarket)'
ps aux | rg 'open_trader .*trend|trend-market run'
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps aux | rg 'open_trader trend-market run'
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.out.log
```

控制器状态位于 `data/trend_controller/<MARKET>/status.json`。应核对 mode、执行/本地主机、
PID、工作目录、Git SHA、phase、heartbeat、blocker 和 next check。回滚也必须加 fence：先
停止全部趋势自动化，在自动化保持停止的状态下部署旧源码，用富途核对每个本地 intent；
只有事实证明安全时，才能显式恢复旧 watcher。控制器可能仍存活时绝不能直接启动旧 watcher。

最终 acceptance 返回 `PASS` 后，必须重新部署完全相同的 accepted SHA，不能把 acceptance
进程本身当作部署。进入 accepted worktree，确认 SHA 和 clean 状态，再用共享配置重启三个
控制器，并从完全相同的 worktree 重启仪表盘：

```bash
cd /Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec
export ACCEPTED_SHA=replace-with-full-accepted-sha
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --short)"

pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p || true
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

逐个检查 CN/HK/US 状态文件：PID 必须存活，`working_directory` 必须是 accepted worktree，
`git_sha` 必须等于 `$ACCEPTED_SHA`，两次读取之间 `heartbeat_at` 必须前进。然后核对新鲜的
控制器/仪表盘日志，以及两个 HTTP 端点。把最终进程列表与安装前记录比较：每个控制器
status PID 都必须是新 PID，`lstart` 时间必须晚于这次 exact-SHA 重装：

```bash
.venv/bin/python - <<'PY'
from datetime import datetime
import json
import os
from pathlib import Path
import time

accepted_sha = os.environ["ACCEPTED_SHA"]
worktree = "/Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec"
root = Path("/Users/ray/projects/open_trader/data/trend_controller")

def read(market):
    return json.loads((root / market / "status.json").read_text(encoding="utf-8"))

before = {market: read(market) for market in ("CN", "HK", "US")}
time.sleep(10)
for market, previous in before.items():
    current = read(market)
    pid = int(current["pid"])
    os.kill(pid, 0)
    assert current["working_directory"] == worktree
    assert current["git_sha"] == accepted_sha
    assert datetime.fromisoformat(current["heartbeat_at"]) > datetime.fromisoformat(
        previous["heartbeat_at"]
    )
    print(market, pid, current["git_sha"], current["heartbeat_at"])
PY

pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p
tail -n 80 /Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec/logs/daily_premarket/launchd-trend-controller-*.{out,err}.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | \
  .venv/bin/python -m json.tool >/dev/null
```

只有 URL 返回 HTTP 200、API 是有效 JSON、三个 heartbeat 都前进，而且进程/状态/日志证据
都属于 accepted SHA，才算完成 review 部署。

## 输出文件

单次运行输出：

```text
data/runs/<YYYY-MM-DD>/HK/trading_advice.csv
data/runs/<YYYY-MM-DD>/HK/change_classifications.csv
data/runs/<YYYY-MM-DD>/HK/premarket_actions.csv
data/runs/<YYYY-MM-DD>/HK/trading_plan.csv
data/runs/<YYYY-MM-DD>/HK/trade_actions.csv
data/runs/<YYYY-MM-DD>/HK/technical_facts.json
data/runs/<YYYY-MM-DD>/HK/decision_facts.json
data/runs/<YYYY-MM-DD>/HK/daily_run_status.json
data/runs/<YYYY-MM-DD>/US/trading_advice.csv
data/runs/<YYYY-MM-DD>/US/change_classifications.csv
data/runs/<YYYY-MM-DD>/US/premarket_actions.csv
data/runs/<YYYY-MM-DD>/US/trading_plan.csv
data/runs/<YYYY-MM-DD>/US/trade_actions.csv
data/runs/<YYYY-MM-DD>/US/technical_facts.json
data/runs/<YYYY-MM-DD>/US/decision_facts.json
data/runs/<YYYY-MM-DD>/US/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>-HK.md
reports/daily_runs/<YYYY-MM-DD>-US.md
logs/daily_premarket/<YYYY-MM-DD>-HK.log
logs/daily_premarket/<YYYY-MM-DD>-US.log
```

最新 promoted 输出：

```text
data/latest/portfolio.csv
data/latest/HK/trading_advice.csv
data/latest/HK/premarket_actions.csv
data/latest/HK/trading_plan.csv
data/latest/HK/trade_actions.csv
data/latest/HK/technical_facts.json
data/latest/HK/decision_facts.json
data/latest/US/trading_advice.csv
data/latest/US/premarket_actions.csv
data/latest/US/trading_plan.csv
data/latest/US/trade_actions.csv
data/latest/US/technical_facts.json
data/latest/US/decision_facts.json
```

## 开发

运行测试：

```bash
.venv/bin/python -m pytest
```

项目入口：

```bash
.venv/bin/python -m open_trader --help
```

安装后的 CLI 入口：

```bash
open-trader --help
```

每次推送 `main` 前，都必须在 `CHANGELOG.md` 增加一条带日期的记录。记录应说明
用户可见变化、影响的流程和已经做过的验证。

## 许可证

TBD.
