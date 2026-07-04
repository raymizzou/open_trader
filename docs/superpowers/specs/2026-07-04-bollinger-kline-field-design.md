# K 线布林带字段设计

## 目标

把布林带从 K 线技术事实中的普通嵌套信息提升为固定展示字段，用于回答两个问题：

- 当前日线价格是否贴近或超过布林带上轨，以感知回调风险是否升高。
- 当前日线价格是否接近布林带下轨，以感知是否进入低位机会区域。

该功能只用于 `趋势 / K 线` 事实展示，不生成交易建议，不改变做 T 信号，不发送通知，不影响交易动作或比例。

## 用户体验

`趋势 / K 线` 卡片中始终显示一个布林带区域。该区域位于 RSI、MACD、均线等普通技术事实之前。

布林带区域按状态改变颜色和文案：

- 贴近或超过上轨：红色，显示 `回调风险升高`。
- 接近下轨：绿色，显示 `低位机会区域`。
- 中轨附近或普通区间：正常颜色，显示 `中性区间`。

用户可见文案必须全部为中文，不显示内部枚举值。卡片显示的核心数据为：

- 当前价
- 上轨或下轨
- 偏离幅度
- 下轨 / 中轨 / 上轨 三个参考价

当价格贴近或超过上轨时，示例文案：

```text
当前价格已超过日线布林带上轨
价格处在布林带上沿之外，说明短线偏热。这个状态用于提醒可能接近回调区，不直接给出交易动作。
```

当价格接近下轨时，示例文案：

```text
当前价格接近日线布林带下轨
价格靠近布林带下沿，说明进入低位观察区。这个状态用于提醒可能出现低位机会，不直接给出交易动作。
```

当价格处于中性区间时，示例文案：

```text
当前价格位于日线布林带中性区间
价格没有贴近上轨或下轨，布林带暂未给出需要特别关注的位置提醒。
```

## 数据来源

沿用现有 `technical_facts.json` 生成路径：

- 从 `trading_advice.csv` 的 `raw_decision.state.market_report` 抽取技术事实。
- 仍由 `extract-technical-facts` 和每日盘前流程写入 dated/latest artifact。
- Dashboard 只读取缓存结果，不直接调用 LLM。

本设计不新增 `bollinger_facts.json`，也不新增单独的服务或任务。

## Schema

布林带字段位于每个 timeframe 内：

```json
{
  "timeframe": "daily",
  "timeframe_label": "日线",
  "current_price": "466.20",
  "bollinger": {
    "upper": "459.13",
    "middle": "399.62",
    "lower": "340.11",
    "position": "above_upper",
    "status": "upper_risk",
    "reference_band": "upper",
    "reference_value": "459.13",
    "distance_pct": "1.5%",
    "summary_zh": "当前价格已超过日线布林带上轨",
    "detail_zh": "价格处在布林带上沿之外，说明短线偏热。这个状态用于提醒可能接近回调区，不直接给出交易动作。"
  }
}
```

字段含义：

- `upper`、`middle`、`lower`：布林带三轨数值，缺失时为空字符串。
- `position`：内部位置枚举，用于前端选择文案和颜色。
- `status`：内部状态枚举，用于区分红色、绿色、正常颜色。
- `reference_band`：当前关注的轨道，`upper`、`lower` 或空字符串。
- `reference_value`：当前关注轨道的数值。
- `distance_pct`：当前价相对关注轨道的偏离幅度。
- `summary_zh`：中文主标题。
- `detail_zh`：中文解释，必须是事实提示，不得包含买入、卖出、加仓、减仓、下单等交易指令。

允许的 `position`：

- `above_upper`
- `near_upper`
- `middle_range`
- `near_lower`
- `below_lower`
- `unknown`

允许的 `status`：

- `upper_risk`
- `lower_opportunity`
- `neutral`
- `unknown`

允许的 `reference_band`：

- `upper`
- `lower`
- 空字符串

UI 不直接显示这些枚举。

## 展示规则

Dashboard 渲染 `bollinger` 时按 `status` 选择颜色：

- `upper_risk`：红色卡片，状态文案 `回调风险升高`。
- `lower_opportunity`：绿色卡片，状态文案 `低位机会区域`。
- `neutral`：正常颜色卡片，状态文案 `中性区间`。
- `unknown` 或字段缺失：正常颜色卡片，状态文案 `布林带数据缺失`。

三项指标按状态切换：

- `upper_risk`：`当前价 / 上轨 / 偏离幅度`
- `lower_opportunity`：`当前价 / 下轨 / 偏离幅度`
- `neutral`：`当前价 / 中轨 / 所处区间`
- `unknown`：显示可用数值，缺失项显示 `缺失`

布林带区域始终存在。即使处于中轨附近，也显示正常颜色卡片，而不是隐藏。

## 抽取要求

LLM 抽取器必须：

- 只抽取报告中明确给出的布林带三轨、当前价、位置描述和偏离描述。
- 缺失字段写空字符串，不得猜测。
- 用户可见字段 `summary_zh` 和 `detail_zh` 必须为中文。
- 不输出交易建议、仓位建议、下单建议或动作指令。
- 如果报告没有明确周期，仍使用现有周期缺失规则，使该技术事实不可用。

如果旧缓存中只有弱格式，例如：

```json
"bollinger": {
  "middle": "399.62",
  "upper": "459.13",
  "lower": "340.11",
  "price_position": "upper half of the band"
}
```

前端可尽量展示三轨数值，但不能编造 `summary_zh`、`detail_zh` 或偏离幅度；状态应降级为 `布林带数据缺失` 或 `中性区间`。

## 错误处理

- `technical_facts.json` 文件缺失：沿用现有 K 线卡不可用状态。
- 记录缺失、来源 hash 过期、抽取失败、周期缺失：沿用现有不可用状态。
- 单个 timeframe 的 `bollinger` 缺失：K 线卡仍可用，但布林带区域显示 `布林带数据缺失`。
- 布林带上下轨缺失但其它技术事实存在：不阻断 RSI、MACD、均线展示。

## 测试

后端测试：

- 验证技术事实 fixture 支持新 `bollinger` schema。
- 验证中文字段不能包含交易指令。
- 验证缺失 `bollinger` 不会使整个技术事实记录失败。

前端测试：

- `upper_risk` 渲染红色卡片和中文 `回调风险升高`。
- `lower_opportunity` 渲染绿色卡片和中文 `低位机会区域`。
- `neutral` 仍显示布林带卡片，使用正常颜色和 `中性区间`。
- UI 不显示内部枚举值，如 `upper_risk`、`near_upper`、`above_upper`。

验收：

- 本地 Dashboard 可看到布林带卡片固定出现在 `趋势 / K 线` 中。
- Playwright 验证桌面和移动视口下文字不溢出、不重叠。
- 不修改 `watch-t` schema、通知文案或做 T 规则。
