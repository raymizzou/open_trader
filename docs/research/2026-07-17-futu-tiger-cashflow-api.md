# 富途与老虎账户资金流水 API 调研

日期：2026-07-17

## 结论

真实账户可以通过 API 获取足以识别外部现金流的资金流水，但两家能力不同：

- **富途真实账户：可以。** `get_acc_cash_flow` 覆盖出入金、调拨、换汇、买卖金融资产、融资融券利息等所有导致资金变化的事项。
- **富途模拟账户：不可以。** 官方明确说明模拟账户不支持资金流水查询，也不支持订单费用查询。
- **老虎综合真实账户（STANDARD）：可以。** `get_fund_details` 提供出入金、交易、费用、资金划拨、换汇、公司行动等分类；官方的新 SDK 文档将利息和分红也列入资金流水能力。
- **老虎模拟账户：不可以使用 `get_fund_details`。** 官方明确限定该接口只支持综合账户，不支持 PAPER。

因此，真实复盘可以优先从券商 API 自动识别入金和出金；模拟纪律曲线不能依靠资金流水 API，继续使用模拟账户净值/订单事实即可。

## 富途 OpenAPI

### 接口与覆盖范围

Python 接口为：

```python
get_acc_cash_flow(
    clearing_date,
    trd_env=TrdEnv.REAL,
    acc_id=...,
    cashflow_direction=CashFlowDirection.NONE,
)
```

官方说明该接口查询指定清算日的账户资金流水，明确覆盖：

- 入金、出金
- 账户调拨
- 货币兑换
- 买卖金融资产
- 融资融券利息
- 其他所有导致资金变化的事项

返回字段包括唯一 `cashflow_id`、清算日、交收日、币种、自由文本 `cashflow_type`、流入/流出方向、金额及备注。来源：[富途：查询账户资金流水](https://openapi.futunn.com/futu-api-doc/trade/get-acc-cash-flow.html)、[交易定义 `FlowSummaryInfo`](https://openapi.futunn.com/futu-api-doc/en/trade/trade.html)。

股息、一般利息、税费/佣金没有稳定的枚举值；`cashflow_type` 是字符串。因此实现时应保存原始类型与备注，不能假设一份固定类型枚举。订单佣金和税费还可以用 `order_fee_query` 按订单取得明细，包括佣金、平台费、交收费、印花税及监管费等。来源：[富途：查询订单费用](https://openapi.futunn.com/futu-api-doc/trade/order-fee-query.html)。

### 时间和调用限制

- 证券/期货账户必须指定一个 `clearing_date`；查询多日必须逐日调用。
- 同一 `acc_id` 每 30 秒最多调用资金流水接口 20 次。
- 返回按时间正序排列。
- 文档没有分页参数；每次返回该清算日的列表。
- 最低 OpenD 版本要求为 `9.1.5108`。
- **模拟账户不支持资金流水。** 模拟账户也不支持 `order_fee_query`。

来源：[富途：查询账户资金流水—接口限制](https://openapi.futunn.com/futu-api-doc/trade/get-acc-cash-flow.html)、[富途：模拟交易限制](https://openapi.futunn.com/futu-api-doc/en/qa/trade.html)。

## 老虎 OpenAPI

### 完整资金明细

Python 接口为：

```python
TradeClient.get_fund_details(
    seg_types,
    account=None,
    fund_type=None,
    currency=None,
    start=0,
    limit=None,
    start_date=None,
    end_date=None,
)
```

`fund_type` 支持：

- `DEPOSIT_WITHDRAW`：入金、出金
- `TRADE`：交易
- `FEE`：费用，包括佣金/税费类明细
- `FUNDS_TRANSFER`：资金划拨
- `FOREX`：货币兑换
- `CORPORATE_ACTION`：公司行动，包括股息；官方示例同时展示分红、分红税及公司行动手续费
- `ACTIVITY_AWARD`：活动奖励
- `OTHER`：其他；利息等未单列为筛选枚举的流水仍应通过 `ALL` 拉取并按返回的 `type`/`desc` 保存

返回包括记录 ID、描述、币种、账户分段、类型、金额、业务日期和更新时间；官方示例明确展示了分红、分红税和公司行动手续费。来源：[老虎 Python：`get_fund_details`](https://docs.itigerup.com/docs/accounts)、[老虎 Python SDK 源码 `get_fund_details`](https://github.com/tigerfintech/openapi-python-sdk/blob/520e5c803bdadd8e8600bd56ef6ea8baeb2575fc/tigeropen/trade/trade_client.py#L1375-L1409)。

新版官方 SDK 文档将该能力概括为按时间窗获取“入金 / 出金 / 费用 / 利息 / 分红等”资金流水，并明确 **只支持 STANDARD，不支持 PAPER**。来源：[老虎 TypeScript SDK：资金流水明细](https://docs.itigerup.com/docs/trade-ts)。

### 专用出入金接口

`TradeClient.get_funding_history(seg_type=None)` 只查询出入金记录，返回入金、出金、出金费用及退款等类型。它没有日期范围或分页参数；若需要完整、可分页且包含其他资金事项的事实，应使用 `get_fund_details`。来源：[老虎 Python：`get_funding_history`](https://quant.itigerup.com/openapi/zh/python/operation/trade/accountInfo.html)、[老虎 Python SDK 源码](https://github.com/tigerfintech/openapi-python-sdk/blob/520e5c803bdadd8e8600bd56ef6ea8baeb2575fc/tigeropen/trade/trade_client.py#L1358-L1373)。

### 时间范围和分页限制

- `start_date`、`end_date` 格式为 `yyyy-MM-dd`。
- 官方 Python 文档**没有声明最大查询天数或历史保留期限**；不能据此承诺任意久的历史都可取回。
- 使用 offset 分页：`start` 从 0 开始。
- `limit` 默认 50，最大 100。
- 返回带 `item_count`、`page_count`；官方示例按 `start += limit` 循环至空结果。
- `get_fund_details` 仅支持综合账户（STANDARD），不支持模拟账户（PAPER）。

来源：[老虎 Python：资金明细参数与分页示例](https://quant.itigerup.com/openapi/zh/python/operation/trade/accountInfo.html)。

## 对复盘收益口径的影响

实现时不应把所有资金流水都当作外部现金流：

- 只有真正的**入金、出金**需要从收益率计算中剔除。
- 股息、利息、佣金、税费属于策略实际回报的一部分，应保留在收益中。
- 账户内部换汇和同一复盘账户内的分段调拨不是新增/减少资本，不应当作外部现金流。
- 两家都应保存原始流水 ID、类型、描述、币种、金额、业务/清算日期和抓取时间，并用流水 ID 去重；不要只保存计算后的净额。

## 最小落地建议

1. 富途真实账户按每个交易日调用一次 `get_acc_cash_flow`；多日补录逐日拉取，并遵守 20 次/30 秒限制。
2. 老虎综合真实账户按日期窗调用 `get_fund_details(fund_type='ALL')`，每页 100 条，循环分页。
3. 仅把明确分类为入金/出金的记录送入收益率现金流调整；其余流水作为收入或成本保留。
4. 富途和老虎模拟账户不增加现金流水采集，因为官方接口不支持。

本调研仅检查官方文档与官方 SDK 源码，未调用任何带凭据的账户 API。
