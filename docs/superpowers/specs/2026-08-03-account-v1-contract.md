# Account v1 Public Contract

## Scope

本契约冻结 Account Module 的第一版公开读模型。R1 只定义契约并重命名现有
Account Sync Worker，不启动 Account API、不监听 `8768`、不改变 Gateway 路由。

Account 只拥有以下事实：

- 券商账户来源、持仓、现金及其已接受的 publication；
- 报价及报价同步结果；
- 账户/报价同步状态；
- statement publication 的已接受事实。

Trend 继续拥有策略风险、动作、候选、纪律与 decision plan。Research 继续拥有
研究结论、研究事实与标准回测。Account 不复制或推断这些领域的数据。

## HTTP surface

公开读接口只有：

```http
GET /api/v1/account/snapshot
```

浏览器只通过 Frontend Gateway 访问该路径。未来 Account API 只监听 loopback
`127.0.0.1:8768`；Account Sync Worker 不监听端口。R1 不实现这两个运行时变化。

## Successful response

有效 publication 返回 `200`。完整结构如下；除明确标为 optional 的未来新增字段
外，所列字段均为 required：

```json
{
  "schema_version": 1,
  "snapshot_generation": "sha256:5f872ad5cd6380a4b59f7113ac1c958b81f46516829200c356a43c8d46e1f507",
  "account_generation": "sha256:8d98578a5ed8d780f8f7ea4b294f5f8327ac50b483bea697800360ea49201ff0",
  "generated_at": "2026-08-03T12:00:05+08:00",
  "quote_as_of": "2026-08-03T12:00:04+08:00",
  "status": "healthy",
  "stale": false,
  "sources": {
    "account": {
      "status": "healthy",
      "as_of": "2026-08-03T12:00:00+08:00",
      "reason": null,
      "brokers": {
        "futu": {
          "source_kind": "live",
          "status": "healthy",
          "data_as_of": "2026-08-03T12:00:00+08:00",
          "last_success_at": "2026-08-03T12:00:00+08:00",
          "reason": null
        }
      }
    },
    "quotes": {
      "status": "healthy",
      "as_of": "2026-08-03T12:00:04+08:00",
      "reason": null
    }
  },
  "release": {
    "api_git_sha": "0123456789abcdef0123456789abcdef01234567",
    "worker_git_sha": "0123456789abcdef0123456789abcdef01234567"
  },
  "summary": {
    "holding_value_hkd": "1000.00",
    "cash_like_value_hkd": "200.00",
    "portfolio_value_hkd": "1200.00",
    "holding_weight_hkd": "83.33%",
    "cash_like_weight_hkd": "16.67%",
    "holding_count": 1,
    "broker_count": 1
  },
  "broker_summaries": [],
  "positions": [],
  "cash_balances": [],
  "errors": []
}
```

时间均为带 UTC offset 的 ISO 8601 字符串。SHA 均为 40 位小写十六进制 Git
commit ID。金额、数量、价格、比例和汇率使用十进制字符串，不使用 JSON 浮点数；
计数使用 JSON integer。

### Status and source fields

`status` 只能是 `healthy` 或 `stale`；`stale` 必须等于
`status == "stale"`。顶层状态取 `sources.account.status` 与
`sources.quotes.status` 中较差者。

`sources.account.as_of` 是当前已接受账户事实的 publication 时间。
`sources.account.brokers` 的 key 是规范化小写 broker 名；每项包含：

| Field | Type | Meaning |
| --- | --- | --- |
| `source_kind` | string | `live` 或 `statement` |
| `status` | string | `healthy` 或 `stale` |
| `data_as_of` | string | 券商快照或 statement 所代表的数据时间/日期 |
| `last_success_at` | string | 最近一次成功接受该来源的时间 |
| `reason` | string or null | 稳定机器码；健康时为 `null` |

`sources.quotes.as_of` 与顶层 `quote_as_of` 相同，均表示最近一次已接受报价
publication 的时间。`reason` 是稳定机器码，不包含凭据、绝对路径或上游原始响应。

### Summary

`summary` 的字段固定为：

| Field | Type |
| --- | --- |
| `holding_value_hkd` | decimal string |
| `cash_like_value_hkd` | decimal string |
| `portfolio_value_hkd` | decimal string |
| `holding_weight_hkd` | percent string |
| `cash_like_weight_hkd` | percent string |
| `holding_count` | integer |
| `broker_count` | integer |

### Broker summaries

`broker_summaries` 每项固定为：

| Field | Type |
| --- | --- |
| `broker` | string |
| `label` | string |
| `source_kind` | `live` or `statement` |
| `detail_available` | boolean |
| `holding_value_hkd` | decimal string |
| `cash_like_value_hkd` | decimal string |
| `portfolio_value_hkd` | decimal string |
| `holding_count` | integer |

数组按 `broker` 升序排列。

### Positions

`positions` 表示每个券商账户内每个 instrument 的净持仓，不表示 tax lot。每项固定为：

| Field | Type |
| --- | --- |
| `position_id` | string |
| `instrument_id` | string |
| `broker` | string |
| `account_alias` | string |
| `market` | string |
| `asset_class` | string |
| `symbol` | string |
| `name` | string |
| `currency` | string |
| `quantity` | decimal string |
| `cost_price` | decimal string, empty when unavailable |
| `cost_value` | decimal string, empty when unavailable |
| `last_price` | decimal string, empty when unavailable |
| `price_kind` | string |
| `price_as_of` | ISO 8601 string or statement date |
| `market_value` | decimal string |
| `market_value_usd` | decimal string, empty when not applicable |
| `market_value_hkd` | decimal string |
| `cost_value_hkd` | decimal string, empty when unavailable |
| `unrealized_pnl` | decimal string, empty when unavailable |
| `unrealized_pnl_pct` | percent string, empty when unavailable |
| `account_weight_hkd` | percent string |
| `portfolio_weight_hkd` | percent string |
| `statement_id` | string, empty for sources without one |
| `confidence` | string |
| `notes` | string |

数组依次按 `broker`、`account_alias`、`market`、`asset_class`、`symbol`、
`position_id` 的 Unicode code point 升序排列。

### Cash balances

`cash_balances` 每项固定为：

| Field | Type |
| --- | --- |
| `broker` | string |
| `account_alias` | string |
| `currency` | string |
| `cash_balance` | decimal string |
| `available_balance` | decimal string, empty when unavailable |
| `cash_balance_hkd` | decimal string |
| `available_balance_hkd` | decimal string, empty when unavailable |
| `statement_id` | string, empty for sources without one |
| `confidence` | string |
| `notes` | string |

数组依次按 `broker`、`account_alias`、`currency` 升序排列。

## Stable opaque IDs

ID 输入先执行以下规范化：

- `market`: trim 后转大写；
- `asset_class`: trim 后转小写；
- `symbol`: trim 后转大写；
- `broker`: trim 后转小写；
- `account_alias`: 只 trim，保留大小写；它必须是券商来源中稳定的账户别名。

规范数组使用 UTF-8 JSON，`ensure_ascii=false`，分隔符为 `,` 与 `:`，不含多余
空白。随后计算完整 SHA-256：

```text
instrument_id = "ins_" + sha256(json([market, asset_class, symbol])).hexdigest()
position_id = "pos_" + sha256(json([broker, account_alias, instrument_id])).hexdigest()
```

消费者只能把 ID 当作不透明比较键，不得解析它来获得 market、asset class、
symbol、broker 或 account alias。若规范化规则未来需要语义变化，必须发布 v2；
v1 不引入 UUID registry 或持久化 ID 表。

## Generations and ETag

`account_generation` 是以下规范对象的 SHA-256，格式为 `sha256:<hex>`：

```text
{
  "summary": <summary>,
  "broker_summaries": <broker_summaries>,
  "positions": <positions>,
  "cash_balances": <cash_balances>,
  "accepted_account_as_of": <sources.account.as_of>,
  "accepted_broker_data_as_of": <broker -> data_as_of mapping>
}
```

因此，刷新失败但继续保留相同已接受账户事实时，`account_generation` 不变。

`snapshot_generation` 是完整 `200` response object 删除
`snapshot_generation` 字段后的 SHA-256，格式同样为 `sha256:<hex>`。对象递归按 key
升序、数组按本契约规定顺序、UTF-8、`ensure_ascii=false`、无多余空白序列化。
任何可见字段、状态、错误或 release 变化都必须改变 `snapshot_generation`。

响应 ETag 为强 ETag：

```http
ETag: "account-v1-<snapshot_generation 中 sha256: 后的 64 位 hex>"
```

`If-None-Match` 与当前 ETag 完全相等时返回 `304` 且无 body。Workflow 在一次运行
开始时读取并 pin 一个 `snapshot_generation`，不得在同一运行中混用多个 generation。

## Freshness

Account 与 quotes 独立判定 freshness：

- 最近刷新成功且 publication 有效，来源为 `healthy`；
- 最近刷新失败，但存在完整、有效的上一版 publication，来源为 `stale`，接口返回
  `200 stale` 并在 `errors` 说明原因；
- 没有上一版有效 publication、publication 缺失/无效或 schema 不支持时，不得返回
  部分数据，接口返回 `503`；
- `statement` 来源不会仅因墙钟时间流逝自动 stale；新 statement 的接受规则由
  statement publication 流程决定；
- 休市时最后有效价不会仅因 `quote_as_of` 变旧自动成为故障。quotes freshness 由
  计划刷新是否成功、所需 instrument 是否缺失以及值是否合法决定。

`generated_at` 是该快照 publication 的生成时间，不是每次 HTTP 请求的时间。

## Errors and unavailable responses

`errors` 项的固定形状为：

```json
{
  "code": "quotes_refresh_failed",
  "source": "quotes",
  "message": "sanitized operator-safe text",
  "retryable": true
}
```

`code` 和 `source` 是稳定机器字段；`message` 必须去除凭据、账户号码、绝对路径及
上游敏感响应。健康 `200` 的 `errors` 必须为空；stale `200` 至少有一项。

不可用时返回 `503`，固定 envelope 为：

```json
{
  "schema_version": 1,
  "status": "unavailable",
  "release": {
    "api_git_sha": "0123456789abcdef0123456789abcdef01234567",
    "worker_git_sha": "89abcdef0123456789abcdef0123456789abcdef"
  },
  "errors": [
    {
      "code": "account_release_mismatch",
      "source": "release",
      "message": "Account API and Account Sync Worker releases differ",
      "retryable": true
    }
  ]
}
```

503 code 至少包括：

| Code | Meaning |
| --- | --- |
| `account_publication_missing` | 没有有效账户 publication |
| `account_publication_invalid` | 账户 publication 格式或值无效 |
| `quotes_publication_missing` | 没有有效报价 publication |
| `quotes_publication_invalid` | 报价 publication 格式或值无效 |
| `account_schema_unsupported` | 只存在不支持的 persistence schema |
| `account_release_mismatch` | Account API 与 Worker Git SHA 不一致 |

生产 Account API 与 Account Sync Worker 必须运行同一 release SHA。SHA 不一致时，
snapshot 必须返回 `503 account_release_mismatch`；不得用旧文件、Legacy Dashboard
或 Gateway translation 兜底。

未来 `/healthz` 只表示 Account API liveness，因此即使 release mismatch 仍返回
`200`，并固定报告：

```json
{
  "schema_version": "open_trader.account_api.health.v1",
  "module": "account_api",
  "status": "ok",
  "api_git_sha": "0123456789abcdef0123456789abcdef01234567",
  "worker_git_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "release_match": false
}
```

## Evolution and exclusions

v1 只能新增 optional 字段。删除字段、重命名字段、改变类型、改变 ID 规范化或改变
字段语义都必须发布 `/api/v2/account/snapshot`；没有真实迁移需求前只维护 v1。

Account response 明确不得包含：

- `risk_flag` 或任何策略风险判断；
- Trend/Research enrichment；
- 动作建议、交易候选或 decision plan；
- 全局 `actionable` 判断。

展示层可以显示 stale 快照；任何动作 workflow 必须按自己的消费者规则 fail closed。
Gateway 只透明代理，不聚合、翻译、读取 Account 文件或回退 Legacy Dashboard。

## R1 compatibility boundary

R1 把概念、Python 模块/类和 CLI 命令统一命名为 Account Sync Worker。为保持当前
JSON/CSV persistence 与 launchd 运维身份不变，以下历史 token 暂时保留：

- launchd label 与模板文件名：`com.open-trader.account-sync-controller`；
- heartbeat：`data/account_sync/controller_status.json`；
- lock：`data/account_sync/controller.lock`；
- heartbeat schema：`open_trader.account_sync.controller.v1`；
- 现有 Dashboard compatibility payload 中的 `controller` key。

这些 token 是兼容标识，不再表示 HTTP Controller 角色。R1 不提供旧
`account-sync-controller` CLI、旧 Python module/class alias 或 compatibility shell。
