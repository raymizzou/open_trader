# 控制器拥有 Dashboard 字段设计

## 背景

`2026-07-30-account-sync-controller-design.md` 已经规定账户同步控制器是
`data/latest` 的唯一发布者，Dashboard 只能读取文件。但当前账户持仓表仍在
浏览器中执行以下计算：

- 用 `market_value × fx_to_hkd` 计算港元市值；
- 用账户净值计算账户权重；
- 用全局净值计算组合权重；
- 用行情覆盖结单或账户快照并重算市值、盈亏和权重。

这使文件契约不完整。任一 accepted position 缺少 `fx_to_hkd` 时，前端的
全局分母计算失败，结果是结单账户的港元市值和账户权重为空，同时所有券商
的组合权重都显示 `-`。控制器已经能够重建完整 `portfolio.csv`，但没有把
同一计算结果发布给账户持仓表。

## 目标

- 控制器发布账户持仓表所需的全部事实和派生字段。
- Dashboard 后端只把控制器投影映射到 API，不计算金额、汇率、盈亏或权重。
- Dashboard 前端只负责分组、格式化、转义、状态样式和响应式展示。
- 保留当前 5 秒行情刷新和 60 秒账户刷新带来的展示鲜度。
- accepted source 继续保存券商或结单事实，不被实时行情覆盖。
- 任一必需字段无法确定时失败关闭，不发布部分计算结果。

## 非目标

- 不改变交易、策略、报告或 Kelly 使用的 `portfolio.csv` 语义。
- 不新增数据库、消息队列或新的 `data/latest` 文件。
- 不改变账户页列顺序、文案、颜色或响应式布局。
- 不让 Dashboard API 成为第二个计算层。
- 不把格式化后的 `HKD 1,234.56` 或带正负色的 HTML 写入状态文件。

## 方案选择

### 采用：同一状态文件中的控制器投影

在 `account_sync_state.json` 顶层新增 `dashboard_projection`。原有
`brokers.*.positions`、`cash`、`fx_rates` 和 `summary` 保留 accepted
source 事实；`dashboard_projection` 保存控制器从这些事实和已发布行情
确定性生成的最终账户页面数据。

优点：

- accepted source 与实时展示值边界清楚；
- Dashboard 只读一个既有文件；
- 不重复创建发布文件、锁或恢复流程；
- 行情失败时可以保留上一份完整投影并明确标记过期。

### 不采用：在原始 position 上补字段

直接修改 `brokers.*.positions` 文件更少，但实时行情会覆盖券商或结单原始
价格，审计事实与展示值混在一起。

### 不采用：在 `/api/dashboard` 计算

改动最小，但计算所有权仍在 Dashboard，违背“控制器提供全部字段”的边界，
也会留下 CLI、API 和浏览器三套潜在计算结果。

## 发布契约

`account_sync_state.json` 新增：

```json
{
  "dashboard_projection": {
    "generated_at": "2026-07-31T08:30:05+08:00",
    "quote_as_of": "2026-07-31T08:30:05+08:00",
    "summary": {
      "holding_value_hkd": "809956.16",
      "cash_like_value_hkd": "2083729.99",
      "portfolio_value_hkd": "2893686.15",
      "holding_count": 36
    },
    "broker_summaries": [],
    "broker_positions": [],
    "cash_details": []
  }
}
```

每个 `broker_positions` 行必须包含：

- 身份：`broker`、`account_alias`、`market`、`asset_class`、`symbol`、
  `name`、`currency`；
- 数量与成本：`quantity`、`cost_price`、`cost_value`；
- 价格：`last_price`、`price_kind`、`price_as_of`；
- 市值：`market_value`、`market_value_usd`、`market_value_hkd`、
  `cost_value_hkd`；
- 盈亏：`unrealized_pnl`、`unrealized_pnl_pct`；
- 权重：`account_weight_hkd`、`portfolio_weight_hkd`；
- 来源：`statement_id`、`confidence`、`notes`。

金额、价格和数量使用十进制字符串；权重和盈亏率使用带 `%` 的百分比字符串。
未知值使用空字符串，不能用 `0` 代替未知。已发布行不允许缺少上述键。
`price_kind` 只使用 `live`、`overnight`、`pre_market`、`after_hours`、
`statement` 和 `account_snapshot`，由前端映射成现有中文标签。保留
`overnight` 是为了不丢失现有美股夜盘价格语义。

港元市值和两级权重是每个非现金持仓的必需计算字段，缺失会阻止投影发布。
成本、成本港元值和盈亏是来源可选事实；结单未提供时保留空字符串，不阻止
其他完整字段发布。非 USD 持仓的 `market_value_usd` 为空字符串。

`broker_summaries` 和 `summary` 同样由控制器发布。Dashboard 不再从明细
重复求和。`summary` 保留当前 API 所需的 `holding_count`、`broker_count`、
三项港元金额和持仓/现金两项权重；`cash_details` 保留控制器计算后的
`cash_balance_hkd` 和 `available_balance_hkd`，前端不做汇率换算。

## 计算规则

控制器复用当前组合重建的货币与汇率规则：

- accepted source 明确提供的账户汇率优先；
- 结单来源使用现有月末静态汇率，当前为 HKD `1`、USD `7.8`、CNY
  `1.08`；
- Tiger live 缺少账户汇率时继续失败关闭，不能猜测静态汇率；
- 标准美股期权使用现有每张 `100` 股乘数；
- 结单账户使用结单价格，不叠加实时行情；
- 实时账户有有效行情时使用行情价格，否则保留最后 accepted account
  价格，并由独立行情状态说明陈旧性。

账户权重：

```text
position.market_value_hkd / broker.portfolio_value_hkd
```

组合权重：

```text
position.market_value_hkd / dashboard.summary.portfolio_value_hkd
```

两个分母都包含现金类资产。负持仓市值保留符号。舍入沿用现有金额两位小数、
权重两位百分比规则。

## 更新与失败语义

控制器在以下时点重建 `dashboard_projection`：

1. 任一账户候选被接受并准备发布时；
2. 任一行情周期完成并准备发布 `quotes.json` 时；
3. 启动时已有 accepted state 和 published quotes 时。

投影先在内存中完整生成并校验，再随 `account_sync_state.json` 原子发布。
行情成功后先发布 `quotes.json`，再发布引用该行情时间的完整账户投影。
两次原子替换之间 Dashboard 最多短暂显示上一份完整投影，不会自行拼接计算。

若 accepted source、必需汇率、现金、分母或必需字段校验失败：

- 不发布新的 `dashboard_projection`；
- 保留上一份完整投影；
- 将相应来源或控制器循环标为失败；
- Dashboard 继续显示最后完整投影并明确显示失败或过期状态；
- 没有上一份完整投影时显示数据不可用，不能把缺失字段格式化成 `0`。

来源部分失败时，继续沿用该来源最后 accepted 数据，并允许其他成功来源进入
下一份完整投影；不能只发布成功来源而让全局权重分母丢失。

## Dashboard 边界

Dashboard 后端：

- 从 `dashboard_projection` 原样取得 `summary`、`broker_summaries`、
  `broker_positions` 和 `cash_details`；
- 不再调用账户明细汇率、求和、盈亏或权重计算；
- 投影缺失或无效时返回明确的不可用状态，不回退扫描运行目录。

Dashboard 前端账户持仓表：

- 直接显示 `market_value_usd`、`market_value_hkd`、
  `account_weight_hkd`、`portfolio_weight_hkd` 和
  `unrealized_pnl_pct`；
- 不读取 `fx_to_hkd`；
- 不调用 `quoteAdjustedHolding()`、`quoteAdjustedTotal()` 或
  `percentValue()` 生成账户表字段；
- 行情轮询只重新加载控制器发布的文件投影；
- 仍可进行数字千分位、货币前缀、百分比符号、缺失值、语义颜色和移动端
  标签格式化。

## 测试与验收

自动化测试至少覆盖：

- 控制器使用一个 Tiger live 行和一个无显式汇率的 Phillips/Eastmoney
  结单行，发布完整港元市值、账户权重和组合权重；
- 任一结单行存在时，Tiger 的组合权重仍为确定值而不是 `-`；
- 结单来源使用静态汇率，Tiger live 缺失账户汇率仍失败关闭；
- 行情更新后控制器重算实时账户市值、盈亏和两级权重；
- 结单账户不被实时行情覆盖；
- 投影失败保留上一份完整投影并标记失败；
- `/api/dashboard` 返回的账户字段与状态文件逐字段一致；
- 前端在只有最终字段、没有原始汇率和可重算输入时仍完整展示；
- 前端不再包含账户表的金额、盈亏或权重计算路径。

直接验证使用当前真实四券商数据检查：

- Phillips、东方财富显示港元市值和账户权重；
- 富途、老虎、Phillips、东方财富全部显示组合权重；
- 老虎 ADP 的 API、DOM 和控制器状态文件权重完全相同；
- 控制器 PID、工作目录、Git SHA、心跳和 fresh logs 属于候选 SHA；
- 最终候选 SHA 的 `make acceptance` 返回 `PASS`；
- 重新部署同一已验收 SHA 后，review URL 返回 HTTP 200；
- 提交 Tiger 与一个结单账户的 live review 截图。
