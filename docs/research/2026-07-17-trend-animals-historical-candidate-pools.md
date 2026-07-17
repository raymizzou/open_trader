# 趋势动物历史候选池可得性调研

> 调研日期：2026-07-17  
> 范围：核对趋势动物公开官方接口契约、本项目已有响应，并执行用户授权的最小真实抽样；未输出 API Key，实际费用为 `0.000`。

## 结论

**目前不能通过趋势动物公开 API 按指定历史日期补取候选池或 ticker snapshot。**

- `getComponentTicker` 的官方参数只有 `apiKey`、`tmId`、`getAllBasicComponentsFlag`。
- `getTickerSnapshot` 的官方参数只有 `apiKey`、`tmIds`、`fields`。
- 两个端点均没有 `date`、`asOfDate`、`startDate` 或 `endDate` 参数。调用方不能选择历史时点；响应中的 `asOfDate` 是服务端返回值，不是查询条件。
- 官方 OpenAPI 当前也没有另一个“按日期获取组合成分/快照”的端点。`getTickerTrendPlot` 是单 ticker 趋势图接口，同样没有历史日期参数，不能替代历史候选池。

来源：[趋势动物官方 OpenAPI JSON](https://www.trendtrader.cn/apiData/v3/api-docs)、[官方接口文档界面](https://www.trendtrader.cn/apiData/doc.html)。本次读取到的 OpenAPI 文档 SHA-256 为 `7ad39ebe06d3f7163a2c62032735d15de71178fcd1ac2ac4f22150d437048d57`。

因此，不能现在向服务端请求“上 30 次可能交易”来做严格的时间点回测。本次产品决策是取消“30 笔前用回测替代复盘”，继续积累每日证据只用于正式复盘和缺陷追溯。

## 是否等于“服务端永远只返回最新一日”

需要精确区分：

1. **可以确认**：公开 API 只能取得服务端在调用时决定的快照，客户端无法指定过去日期。
2. **不能把它表述成**：`getComponentTicker` 的每一行永远都是最新日期。本项目取得过同一响应混有较早 `asOfDate` 的成分，因此生产客户端会忽略较旧行，并要求至少存在当期行。[生产客户端的日期处理](../../src/open_trader/trend_animals.py#L241)
3. 混入旧行不构成历史查询能力：调用方不能选择日期，也不能保证返回某日完整成分，因而不能据此重建任意历史候选池。

严格结论是：**公开接口是无历史时点参数的当前快照接口；它可能夹带旧成分行，但不提供可控、完整、可复现的历史候选池查询。**

## 本项目的实际调用事实

本项目调用 `getComponentTicker` 时只向服务端发送 `tmId` 和 `getAllBasicComponentsFlag`；调用 `getTickerSnapshot` 时只发送 `tmIds` 和 `fields`。[生产客户端调用代码](../../src/open_trader/trend_animals.py#L129)

代码中的 `expected_date` 没有进入请求参数。它只用于：

- 生成本地缓存身份；
- 收到响应后校验每行 `asOfDate`；
- 对 `getComponentTicker` 忽略比预期日期更早的个别行；
- 对 snapshot 日期不一致直接报错。

参见[缓存与响应日期校验](../../src/open_trader/trend_animals.py#L203)。所以把 `expected_date` 改成过去日期不会向服务端发起历史查询，只会让当前响应无法通过日期校验。

## 现在已经保存了多少历史

本地 `data/trend_animals/cache/responses/` 和不可变复盘证据中，已能看到 2026-07-14、2026-07-15、2026-07-16 的部分 A 股/ETF、美股、港股成分和快照。不可变复盘证据目前包括：

| 市场 | 已冻结报告日 | 成分数 | snapshot 数 |
|---|---:|---:|---:|
| A 股 | 2026-07-16 | 28 | 32 |
| 港股 | 2026-07-16 | 4 | 4 |
| 美股 | 2026-07-15 | 13 | 13 |
| 美股 | 2026-07-16 | 47 | 47 |

这些文件证明项目能够**从采集日开始积累**每日候选池，但不能证明服务端能补发更早历史。当前覆盖只有约三个数据日，也不足以严格还原“最近 30 笔完整交易”。

## 2026-07-17 真实接口抽样

经用户授权，使用项目现有客户端和配置进行了一次最小真实抽样，未输出 API Key：

- A 股组合 `622466` 返回 `asOfDate=2026-07-16` 的 20 个成分；
- 从中抽取 2 个标的，请求 `tmId`、`tickerSymbol`、`asOfDate`、
  `tradableFlag`、`isTrendRightSide`、`trendTemperatureCurr` 六个字段；
- snapshot 返回 JSON 行，接口不存在 CSV 响应或“每日覆盖清单”；
- 本次 snapshot 账户余额差为 `0.000`；成分命中本地缓存，snapshot 未命中缓存；
- 使用隔离缓存把 `expected_date` 改为 `2026-07-15` 后，真实接口仍返回
  `2026-07-16`，客户端报错：
  `getComponentTicker returned data for '2026-07-16'; expected 2026-07-15`。

该抽样直接验证：`expected_date` 不会让服务端返回指定历史日。真实响应缓存见
[`960120...c5.json`](../../data/trend_animals/cache/responses/96012097405a973805aabfc4bb3a50731ee48363a5d4b1af376ad1c74c0391c5.json)。

## 对回测替代方案的影响

当前不应上线“真实交易不足 30 笔时，自动补取过去 30 笔候选并回测”，因为关键输入取不到。用今天的候选池反套历史行情会产生幸存者偏差和前视偏差，不是该策略的真实历史回测。

最终处理：

1. 不实现历史回测替代，也不使用今天的候选池反套历史行情。
2. 继续按市场、数据日和策略版本保存每日缓存及不可变 evidence，用于正式复盘和缺陷追溯。
3. 样本不足期间只显示真实执行进度 `N/30`，不制造回测指标。

## 核验边界

- 官方 OpenAPI 与文档界面公开可访问，足以核对端点及参数。
- 初始官方契约核验未调用带 API Key 的业务接口；随后经用户授权执行了上述最小真实抽样。
- 抽样未输出凭据，账户余额差为 `0.000`；关于混合旧行的判断仍来自项目此前保存的第一方响应及生产客户端处理逻辑。
