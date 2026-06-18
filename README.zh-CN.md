# Open Trader

[English](README.md)

Open Trader 是一个本地优先的投资组合分析工具，用于把券商月结单和实时行情转成结构化持仓、盘前交易建议、交易计划和可人工复核的行动报告。

它面向“人仍然掌控最终决策”的工作流：Open Trader 负责读取数据、调用分析模型、通过 Futu OpenD 检查实时行情并写出报告，但不会自动下单。

## 功能

- 导入每月券商账单，生成标准化持仓 CSV。
- 使用 TradingAgents 和 DeepSeek 为每个标的生成盘前建议。
- 保留原始模型输出和标准化 trader 模板，便于追溯。
- 当每日运行超过硬截止时间或单个标的分析失败时，自动 fallback 到该标的最近一次成功建议。
- 从建议摘要生成机器可读的 trading plan。
- 通过 Futu OpenD 实时行情检查 trading plan。
- 生成可复核的 trade action CSV 和 Markdown 报告。
- 在 macOS 上通过 `launchd` 自动运行每日盘前流程。

## 安全说明

本项目不是投资建议，也不能替代人工复核。模型输出可能不完整、过期或错误。任何投资决策前，都应人工检查生成的建议、计划、行情检查结果和交易动作。

Open Trader 不会提交订单。任何下单流程都应保持为单独、明确、经人工确认的步骤。

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
- `OPEN_TRADER_CLASSIFIER_MODEL`：默认 `deepseek-v4-flash`。

## 常用流程

### 导入月度账单

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --futu /path/to/futu.pdf \
  --tiger /path/to/tiger.pdf \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

主要输出：

```text
data/latest/portfolio.csv
```

### 手动运行盘前建议

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --max-workers 3 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1
```

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

### 生成交易动作

```bash
.venv/bin/python -m open_trader generate-trade-actions \
  --plan data/latest/trading_plan.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-16
```

## 每日自动化

安装 macOS 用户级 `launchd` 定时任务：

```bash
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

## 输出文件

单次运行输出：

```text
data/runs/<YYYY-MM-DD>/HK/trading_advice.csv
data/runs/<YYYY-MM-DD>/HK/change_classifications.csv
data/runs/<YYYY-MM-DD>/HK/premarket_actions.csv
data/runs/<YYYY-MM-DD>/HK/trading_plan.csv
data/runs/<YYYY-MM-DD>/HK/trade_actions.csv
data/runs/<YYYY-MM-DD>/HK/daily_run_status.json
data/runs/<YYYY-MM-DD>/US/trading_advice.csv
data/runs/<YYYY-MM-DD>/US/change_classifications.csv
data/runs/<YYYY-MM-DD>/US/premarket_actions.csv
data/runs/<YYYY-MM-DD>/US/trading_plan.csv
data/runs/<YYYY-MM-DD>/US/trade_actions.csv
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
data/latest/US/trading_advice.csv
data/latest/US/premarket_actions.csv
data/latest/US/trading_plan.csv
data/latest/US/trade_actions.csv
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

## 许可证

TBD.
