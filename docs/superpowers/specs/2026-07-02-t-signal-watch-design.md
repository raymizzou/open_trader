# 做T实时提醒设计

## 目标

新增一个只读的做T实时提醒工具，用于港股和美股已有持仓的底仓做T观察。第一版只监控 `data/latest/portfolio.csv` 里的 HK/US 持仓，不自动下单，不从无仓开始做日内短线。

工具在盘中生成 `BUY_T`、`SELL_T`、`HOLD`、`REVIEW` 信号。盘前、盘后、休市或行情状态不完整时，只允许展示观察信息或 `REVIEW`，不输出买卖点。信号通过后台 watcher 产生通知，并在现有 dashboard 的持仓列表中提供入口查看详情。

## 用户体验

复用现有持仓列表，不新增独立做T列表。每个持仓行的 `交易决策` 按钮旁新增 `做T` 按钮。点击后在现有详情区域展示该标的的做T详情。

详情页展示：

- 当前做T信号：`BUY_T`、`SELL_T`、`HOLD`、`REVIEW`。
- 建议操作比例：固定比例枚举，不使用“小T / 大T”文案。
- 信号依据：日内位置、短线动能、流动性、硬约束结果。
- 消息 timeline：什么时候生成信号、什么时候发送通知、现在是否仍处于触发状态。
- 当前状态：用中文描述，例如“BUY_T 已通知，等待价格反弹后出现 SELL_T 信号”。

通知同一轮信号只发一次。UI 仍持续刷新当前状态和 timeline。完成一组 `BUY_T -> SELL_T` 或 `SELL_T -> BUY_T` 后，当天允许进入下一轮通知。

## 范围

第一版包含：

- HK/US 已有持仓监控。
- 启动时持仓作为底仓基准。
- 盘中做T信号。
- 盘前、盘后、休市观察和阻断。
- 最新价、日内涨跌、1m/5m K线、VWAP、短线均线、成交量、买卖盘价差、盘口深度。
- 规则层比例映射。
- AI 动作解读和中文依据。
- 中文通知。
- dashboard 做T详情。
- 固定 JSON schema 和测试 fixture。

第一版不包含：

- 自动下单。
- 从无仓开始的 day trade。
- 逐笔成交、大单方向、资金流、Futu 异动信号接入。
- 每只股票自定义做T比例上限。
- 修改现有 `generate-trade-actions` 执行输出。
- 把 AI 输出作为硬约束的替代。

## 架构

新增独立 watcher，而不是把逻辑塞进现有 `watch-futu`。

模块边界：

- `t_signal`：做T规则信号、比例映射、AI 解读、schema 校验、timeline 事件。
- `watch-t` CLI：长运行进程，读取持仓，连接 Futu OpenD，轮询行情，写入信号，发送通知。
- Futu 数据层：复用现有 `FutuQuoteClient` 的错误诊断风格，并扩展或新增边界以读取 1m/5m K线和买卖盘。
- 通知层：复用 `notifications.py`，通知文案保持中文，并写入可审计结果。
- dashboard API：给每个 holding 附加对应 `t_signal`。
- dashboard UI：持仓行新增 `做T` 按钮，详情页渲染信号、比例、依据和 timeline。

数据流：

```text
data/latest/portfolio.csv
-> 读取 HK/US 持仓并记录启动时底仓基准
-> Futu OpenD 拉最新价、日内涨跌、1m/5m K线、VWAP/均线/成交量、买卖盘
-> 规则层生成结构化信号和比例候选
-> AI 读取结构化信号，输出动作解读和依据
-> 硬约束复核，必要时降级为 HOLD/REVIEW
-> 写入 data/runs/<date>/<market>/t_signals.json
-> 更新 data/latest/<market>/t_signals.json
-> 如需通知，发送一次中文通知并记录结果
-> dashboard 读取最新 t_signal 并展示
```

## 动作和比例

动作枚举固定为：

- `BUY_T`：低吸买回或临时加仓，后续等待 `SELL_T` 完成闭环。
- `SELL_T`：高抛减出，后续等待 `BUY_T` 买回。
- `HOLD`：信号不足或中性区间。
- `REVIEW`：数据、时段、流动性、趋势或 AI 输出需要人工复核。

建议操作比例由规则层确定，AI 只能解释，不得自由生成比例。

第一版比例枚举固定为：

- 空字符串：无可执行比例。
- `6`
- `10`
- `15`
- `20`

比例按启动时底仓基准计算。默认最大比例为 `20%`。如果数据缺失、盘口不合格、价差过大、趋势单边、盘前盘后或硬约束阻断，比例必须为空，动作只能是 `HOLD` 或 `REVIEW`。

## Schema

单标的信号使用版本化 schema：`open_trader.t_signal.v1`。后端 JSON 文件、dashboard payload 和测试 fixture 必须使用同一字段集合。UI 不得依赖临时字段。

```json
{
  "schema_version": "open_trader.t_signal.v1",
  "run_date": "2026-07-02",
  "market": "HK",
  "symbol": "00700",
  "futu_symbol": "HK.00700",
  "name": "腾讯控股",
  "session_phase": "regular",
  "updated_at": "2026-07-02T14:23:08+08:00",
  "action": "BUY_T",
  "suggested_ratio": "10",
  "current_status": "BUY_T 已通知，等待 SELL_T 信号",
  "signal_summary_zh": "价格低于 VWAP 后回收，接近早盘支撑，短线动能修复，适合按 10% 底仓比例低吸买回。",
  "price": {
    "last_price": "376.40",
    "day_change_pct": "-1.20",
    "vwap": "378.10",
    "ma_1m": "376.55",
    "ma_5m": "376.85",
    "day_low": "374.80",
    "day_high": "382.20"
  },
  "liquidity": {
    "bid": "376.35",
    "ask": "376.40",
    "spread_pct": "0.013",
    "bid_depth": "52000",
    "ask_depth": "47000",
    "depth_status": "pass"
  },
  "technical": {
    "rsi_5m": "34",
    "volume_ratio_5m": "1.30",
    "price_position": "below_vwap_reclaim",
    "trend_state": "range_rebound"
  },
  "hard_gates": [
    {
      "name": "session_phase",
      "status": "pass",
      "message_zh": "当前处于盘中交易时段。"
    }
  ],
  "evidence": [
    {
      "name": "vwap_reclaim",
      "direction": "buy",
      "strength": "medium",
      "message_zh": "价格低于 VWAP 后回收。"
    }
  ],
  "timeline": [
    {
      "event_at": "2026-07-02T14:21:36+08:00",
      "event_type": "notification_sent",
      "action": "BUY_T",
      "suggested_ratio": "10",
      "message_zh": "已发送 BUY_T 通知。"
    }
  ],
  "notification": {
    "should_notify": false,
    "notified": true,
    "dedupe_key": "2026-07-02|HK.00700|cycle-1|BUY_T",
    "last_notified_at": "2026-07-02T14:21:36+08:00"
  },
  "status": "ok",
  "error": ""
}
```

Fixed enums:

- `session_phase`: `pre_market`, `regular`, `post_market`, `closed`, `unknown`
- `action`: `BUY_T`, `SELL_T`, `HOLD`, `REVIEW`
- `suggested_ratio`: empty string, `6`, `10`, `15`, `20`
- `depth_status`: `pass`, `thin`, `wide_spread`, `missing`
- `price_position`: `near_support`, `near_resistance`, `below_vwap_reclaim`, `above_vwap_reject`, `middle_range`, `breakout`, `breakdown`, `unknown`
- `trend_state`: `range_rebound`, `range_fade`, `uptrend`, `downtrend`, `choppy`, `unknown`
- `hard_gates.status`: `pass`, `block`, `warn`, `missing`
- `evidence.direction`: `buy`, `sell`, `neutral`, `risk`
- `evidence.strength`: `low`, `medium`, `high`
- `timeline.event_type`: `signal_created`, `signal_changed`, `notification_sent`, `notification_suppressed`, `signal_expired`, `review_required`
- `status`: `ok`, `review`, `blocked`, `error`, `stale`

## AI Boundary

AI receives only structured signal inputs and returns an explanation payload. It may choose an action interpretation only within the allowed set produced by hard constraints.

AI must output:

- `action`
- `signal_summary_zh`
- rationale for the suggested ratio
- evidence references using existing signal names

AI must not:

- invent prices, indicators, or unavailable data
- override hard gates
- output ratios outside the fixed enum
- produce executable order quantities
- output raw English prose for user-visible fields

Invalid AI output is discarded. The system then emits `REVIEW` with preserved rule-layer evidence.

## Notification Behavior

Notifications are tied to signal cycles, not raw price ticks.

Rules:

- A `BUY_T` notification is sent once for a cycle.
- After `BUY_T`, repeated `BUY_T` triggers are suppressed and shown only in UI.
- The next allowed notification for that cycle is `SELL_T`.
- A `SELL_T` notification completes the `BUY_T -> SELL_T` cycle.
- The reverse `SELL_T -> BUY_T` path follows the same rule.
- After a cycle completes, another cycle may start on the same day.
- `HOLD` does not notify.
- `REVIEW` notifies only when it represents a new blocker that needs attention.

Timeline stores both sent and suppressed events so the UI can explain why no new notification was sent.

## Error Handling

Do not guess through missing or malformed market data.

- Futu OpenD unreachable: write `status=error`; UI shows行情不可用; do not send buy/sell notification.
- Snapshot available but K-line missing: write `status=review`; action must be `HOLD` or `REVIEW`.
- Buy/sell order book missing: `depth_status=missing`; action must be `HOLD` or `REVIEW`.
- Spread too wide or depth too thin: hard gate `block`; action becomes `REVIEW`.
- Pre-market, post-market, closed, or unknown session: action must be `HOLD` or `REVIEW`; `suggested_ratio=""`.
- Illegal AI JSON, missing required fields, or illegal enums: discard AI output and emit `REVIEW`.
- AI conflicts with hard gates: hard gates win; add `review_required` timeline event.
- Notification failure: keep signal JSON and UI output; record notification failure in audit log.

## CLI

Add a long-running command:

```bash
.venv/bin/python -m open_trader watch-t \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --config config/daily_premarket.env \
  --host 127.0.0.1 \
  --port 11111 \
  --poll-seconds 5
```

Diagnostic mode:

```bash
.venv/bin/python -m open_trader watch-t \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --config config/daily_premarket.env \
  --once
```

Expected terminal output includes run date, monitored symbol count, generated signal count, notification count, and paths for dated/latest signal artifacts.

## Artifacts

Dated artifacts:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/t_signals.json
```

Latest artifacts:

```text
data/latest/<MARKET>/t_signals.json
```

The writer must use atomic replace semantics. Latest promotion should not leave partially written files.

## Dashboard Integration

Dashboard payload assembly attaches `t_signal` to each matching holding by `(market, symbol)` or `futu_symbol`.

UI changes:

- Widen the first holdings-table column only as much as needed for two compact buttons.
- Add `做T` next to `交易决策`.
- Clicking `做T` opens the same detail panel area, in a `t_signal` view.
- The detail view renders action, suggested ratio, current status, signal summary, evidence rows, hard gates, and timeline.
- User-visible text is Chinese. Machine enums remain internal.
- Missing t_signal data shows “暂无做T信号” rather than a blank panel.

Interactive UI work must be verified with Playwright before completion.

## Testing

Unit tests should cover:

- Schema completeness and enum validation.
- HK/US portfolio loading from existing holdings only.
- Session phase behavior: only `regular` can produce `BUY_T` or `SELL_T`.
- Ratio mapping to `6`, `10`, `15`, `20`.
- Missing K-line, missing order book, wide spread, thin depth, no bottom position, and single-direction trend downgrade to `HOLD` or `REVIEW`.
- AI output validation and downgrade path.
- Notification dedupe by cycle.
- Timeline events for created, changed, sent, suppressed, expired, and review-required states.
- Atomic dated/latest writes.
- Dashboard payload attaches `t_signal` to holdings.
- UI renders the `做T` button and t-signal detail.

Manual real-data verification:

```bash
.venv/bin/python -m open_trader watch-t \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --config config/daily_premarket.env \
  --once
```

Success means the command connects to Futu OpenD, produces at least one HK or US t-signal artifact, and dashboard displays the signal detail. If live data is unavailable, the command must produce a clear `REVIEW` or `error` state without a traceback.
