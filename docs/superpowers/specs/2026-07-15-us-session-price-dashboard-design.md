# 美股分时段价格 Dashboard 设计

## 状态

- 日期：2026-07-15
- 状态：已确认
- UI：Visual Companion V4

## 目标

Dashboard 的美股持仓只展示一个当前用于估值的价格，同时明确标注该价格来自
夜盘、盘前、盘中还是盘后，以及当前行情时间。市值和浮盈亏使用同一个选中价格，
避免价格展示与估值不一致。

## 已确认的产品行为

- 支持四个美股时段：夜盘、盘前、盘中、盘后。
- 每个标的只展示当前使用的一个价格，不同时铺开四个价格。
- 当前时段价格驱动市值和浮盈亏。
- 当前时段没有有效报价时，回退到最近有效价格，但保留真实时段标签并标记
  `上一有效价`。
- 美股休市、周末或时段间没有新报价时，继续展示最近有效价格；Header 同时显示
  美股休市或部分报价状态。
- 标的行情时间使用 `America/New_York (ET)`；Header 的全局获取时间使用
  `Asia/Shanghai (CST)`。
- 股票、ETF 和美股期权使用同一组件。没有扩展时段报价的期权通常回退到盘中价。
- 非美股继续使用现有价格展示，不增加美股时段标签。

## 数据来源

继续使用当前 `FutuQuoteClient` 和同一个 OpenD 连接，不增加依赖或后台任务。

Dashboard 专用行情读取从 `get_market_snapshot()` 取得：

- `last_price`
- `pre_price`
- `after_price`
- `overnight_price`
- `update_time`

再从 `get_market_state()` 取得每个标的的富途 `market_state`。市场状态是时段判断的
事实来源；前端不得根据浏览器时间自行猜测时段。这样可以覆盖节假日、提前收市和
不同标的的实际交易状态。

现有 `FutuQuoteClient.get_snapshots()` 和公共 `QuoteSnapshot` 保持不变。盘中 watcher
继续只依赖原来的 `last_price`，不接入 Dashboard 分时段行情链路。

## 时段与价格选择

Dashboard 按富途市场状态选择价格：

| 富途状态 | 当前时段 | 首选字段 | 缺失时的回退顺序 |
| --- | --- | --- | --- |
| `OVERNIGHT` | 夜盘 | `overnight_price` | 盘后、盘中、盘前 |
| `PRE_MARKET_BEGIN` | 盘前 | `pre_price` | 夜盘、盘后、盘中 |
| `MORNING`, `AFTERNOON` | 盘中 | `last_price` | 盘前、夜盘、盘后 |
| `AFTER_HOURS_BEGIN` | 盘后 | `after_price` | 盘中、盘前、夜盘 |
| `PRE_MARKET_END`, `WAITING_OPEN` | 盘前已结束 | 最近有效价 | 盘前、夜盘、盘后、盘中 |
| `AFTER_HOURS_END` | 盘后已结束 | 最近有效价 | 盘后、盘中、盘前、夜盘 |
| `CLOSED`, `NONE` 或其他非交易状态 | 休市 | 最近有效价 | 盘后、盘中、盘前、夜盘 |

所有候选价格必须是有限且大于零的数值。只有在交易中的状态选中对应首选字段时，
`current_session_quote=true`；交易中使用回退字段、时段已结束或休市时均为 `false`。
结果始终返回实际来源时段，绝不把盘中价标成夜盘价。

富途只为整条市场快照提供一个 `update_time`，没有为四个时段字段分别提供成交时间。
因此：

- 选中当前时段字段时，价格旁显示该快照的 `update_time`，文案称为`行情时间`，
  不称为成交时间。
- 使用回退字段时，不伪造精确时间；显示真实时段和`上一有效价`。
- 全局 `fetched_at` 只在 Header 显示一次。

## 后端边界

给现有 `FutuQuoteClient` 增加 Dashboard 专用的分时段快照读取和市场状态读取能力，
不修改 watcher 使用的方法。`DashboardQuoteService` 负责：

1. 读取 Dashboard 当前报价标的。
2. 获取四个时段价格和逐标的市场状态。
3. 按上述规则选出唯一估值价。
4. 继续使用现有最后成功缓存和 stale 行为。
5. 在报价结果中汇总当前时段缺价数量和市场状态查询异常。

`/api/quotes` 的每个美股报价在现有字段之外增加：

- `price_session`: `overnight`, `pre_market`, `regular`, `after_hours` 或空字符串。
- `price_time`: 当前时段行情的美东时间；回退价无法可靠对应时为空字符串。
- `current_session_quote`: 是否使用了当前时段的首选字段。
- `market_state`: 富途原始市场状态，供诊断和测试使用。

现有 `last_price` 字段改为当前选中的唯一估值价，前端现有市值和盈亏计算继续复用
该字段。顶层结果增加 `fallback_count`，只统计交易中缺少当前时段价格而发生的回退。
正常休市或时段已结束时使用最近有效价不计为异常，Header 显示美股休市。存在交易中
回退价或市场状态查询异常时，顶层状态为 `partial`，Header 给出明确但简短的说明。

## UI

沿用现有持仓表格和账户分组，不新增卡片、弹窗或独立价格面板。

美股价格单元格采用 V4 紧凑格式：

```text
夜盘 61.50 · 03:03 ET
```

进入其他时段时，标签和选中价格一起切换。使用回退价时显示：

```text
盘后 62.22 · 上一有效价
```

Header 继续显示行情状态，并只在此处显示一次全局获取时间，例如：

```text
行情正常 · 刷新于 2026-07-15 15:03:13 CST
```

桌面端和手机端都只显示一个价格。时段不能只靠颜色区分；文本标签必须始终存在。
数值使用等宽数字，现有键盘、对比度和响应式规则保持不变。

## 异常与降级

- 当前时段字段缺失但存在其他有效价：返回回退价，`current_session_quote=false`，
  Header 显示`部分标的当前时段无报价`。
- 市场状态查询失败但快照成功：使用原 `last_price`，不猜测时段，结果标为
  `partial`。
- 所有价格字段都无效：沿用现有 `missing_quote` 行为。
- 快照请求失败：沿用现有最后成功缓存，将缓存标为 stale，并显示恢复指引。
- 缓存必须保留选中价格、时段、行情时间和是否回退，避免错误地把旧价显示成当前价。
- Dashboard 保持只读，不修改持仓文件，不影响通知、下单或 watcher。

## 实现范围

预计修改：

- `src/open_trader/futu_quote.py`
- `src/open_trader/dashboard_quotes.py`
- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- 对应的行情、Dashboard API 和静态前端测试

不新增数据库、配置项、定时任务、前端框架或第三方依赖。

## 验证

自动化测试至少覆盖：

- 四个富途价格字段的解析与非法值过滤。
- 富途市场状态到四个时段的映射。
- 每个时段的首选价格和确定性回退顺序。
- 当前时段缺价、市场状态失败、全字段缺失和 stale 缓存。
- `/api/quotes` 的估值价、时段、行情时间、回退标记和汇总状态。
- watcher 的原 `QuoteSnapshot` 契约和行为不变。
- 桌面端和手机端只显示一个紧凑价格，Header 是唯一的全局获取时间位置。

实盘验证必须直接查询当前真实美股持仓，核对富途逐标的 `market_state`、选中字段、
Dashboard API 和浏览器展示。实现后的最终门禁是 `make acceptance`；只有 `PASS`
才可描述为完成。随后必须部署精确的已验收 Git SHA，并核对新 PID、工作目录、Git
SHA、新日志和 review URL 的 HTTP 200。

## 非目标

- 修改任何 watcher、通知或自动下单行为。
- 保存四时段历史价格或绘制分时图。
- 同时展示四个时段的价格。
- 为非美股增加类似的时段模型。
- 为缺失的扩展时段价格进行推算或插值。
