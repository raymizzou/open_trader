# 趋势动物收藏夹相对趋势强度 API 调研

> 调研日期：2026-08-02  
> 范围：趋势动物第一方 OpenAPI、项目现有客户端与真实缓存，以及使用现有 API Key 的最小只读抽样。未输出或保存 API Key；未调用交易、通知或反馈提交；未修改业务代码。OpenAPI 中未写入公开说明的 `reGetTickerList` 只读探测返回不可用且未扣费，不作为方案依赖。

## 结论

**能取得收藏夹下标的的相对趋势强度，但不是从收藏夹列表接口一次直接取得。** 官方支持的最短路径是：

1. 免费调用 `getFavoritesTicker`，取得收藏夹内每个标的的 `tmId`；
2. 把这些 `tmId` 放在一次批量 `getTickerSnapshot` 请求中；
3. 跨 A/HK/US 比较时取 `trendStrengthGlobalCurr`；只在同一资产类别内比较时取 `trendStrengthLocalCurr`；
4. 若要截图口径的“页面内强度”，按第一方指南在该收藏池内对
   `trendStrengthGlobalCurr` 排序或计算分位，再归一化到 0–100。

截图中“强度”列下面标注的整数“页面内排名”**不是公开 snapshot 字段**。截至本次核验，第一方实时字段目录共 52 个字段，没有 `rank`、`pageRank`、`order` 或等价字段。但第一方 [AI Agent 接入指南](https://gitee.com/trendAnimalsPro/Api/blob/master/%E8%B6%8B%E5%8A%BF%E5%8A%A8%E7%89%A9API%E6%8E%A5%E5%85%A5AI%20Agent%E6%8C%87%E5%8D%97) 第 8.4 节明确规定：对页面品种池请求 `trendStrengthGlobalCurr`，再在池内排序或计算分位并归一化到 0–100，所得结果就是等同小程序展示口径的页面内趋势相对强度。本次核验的指南提交为 `ed52f7747c4cb02c6d10cb36299448abe07cce68`。

同数据日的本地证据也说明两者不能混用：截图里平安银行显示页面内排名 `81`，而 2026-07-31 的真实 Trend Animals snapshot 对同一 `tmId=306177` 给出 `trendStrengthLocalCurr=99.6`、`trendStrengthGlobalCurr=95.0`。[冻结报告证据](../../reports/trend_a_share/2026-07-31.json#L3227-L3261)

本次 14 行全局强度抽样在截图可见子集上的顺序为：A 股 < 美股 < 港股 < 金融资产 < 3M < 徕博科 < 再生元制药 < 平安银行 < 奎斯特诊疗，与截图的页面内强度顺序完全一致；差异只在 0–100 的整数映射值。

因此，官方 API 足够构造收藏夹页面内相对趋势强度。若需求是与截图中的 `81/75/88` 逐项整数相等，还需要固定与小程序相同的页面全集，并确认第一方未公开的分位、并列和舍入细节；当前只能保证口径与排序一致，不能承诺逐点整数 parity。

## 第一方接口证据

趋势动物公开的 [OpenAPI JSON](https://www.trendtrader.cn/apiData/v3/api-docs) 明确列出：

- `GET /data/getFavoritesCategory`
- `GET /data/getFavoritesTicker`
- `GET /data/getTickerSnapshot`
- `GET /data/getSnapshotColumnBilling`
- `GET /data/getTickerTrendPlot`

本次读取的 OpenAPI SHA-256 为 `048af3d143f4f6672ec687469c2e73e1dda1351dbd18238d1970d750d975e1b2`。第一方 [API 文档界面](https://www.trendtrader.cn/apiData/doc.html) 使用相同服务契约；详细字段说明来自只读 `getApiDocIntro` 与 `getSnapshotColumnBilling` 响应。

### 收藏夹接口直接返回什么

`getFavoritesCategory` 只返回：

- `catName`
- `tickerCount`

`getFavoritesTicker` 可选 `favCategory` 参数，返回：

- `tmId`
- `assetCategory`
- `asset`
- `tickername`（注意这里是小写 `n`）
- `asOfDate`
- `updateDt`

它不返回价格、月涨幅、温度、强度或页面名次。

2026-08-02 的最小真实只读抽样与截图结构一致：

- `getFavoritesCategory` 返回一个“近期收藏”分组，`tickerCount=13`；
- `getFavoritesTicker?favCategory=近期收藏` 返回 13 行；
- 不传 `favCategory` 的“全部”请求返回 14 行，比该分组多一个 `tmId=10002` 的“金融资产”页面根节点；
- 返回列表包含截图中的市场根节点和 `tmId=306177` 平安银行等标的；
- 抽样前后账户 `balance`、`totalConsumed`、`totalRecharged` 的差均为 `0`，与第一方文档将收藏夹接口标记为“免费”一致。

以上响应只在 `/tmp` 中短暂保存用于核验，未将账户收藏明细或凭据写入仓库。收藏夹响应 SHA-256 为 `2e53cbd2deae7adabddf6fe895cbee8ea2d3eb86a56c4754da8340c88c3e0014`；字段计费响应 SHA-256 为 `8640ca31369e4c2aa2d30ab97c93a99e53142df8968f25fca09231e27a16fd1f`。

### 强度字段来自哪里

`getTickerSnapshot` 接受逗号分隔的 `tmIds` 和 `fields`，所以不需要为每个收藏标的各发一次请求；当前项目客户端也会去重并按 URL 长度批量拆分。[批量 snapshot 客户端](../../src/open_trader/trend_animals.py#L337-L399)

与本需求直接相关的字段为：

| 字段 | 第一方定义 | 用法 |
| --- | --- | --- |
| `trendStrengthLocalCurr` | 当前 0–100 本地趋势相对强度，在标的所属资产类别内比较，如 A 股内、港股内或美股内；越高越强 | 同市场收藏夹排序 |
| `trendStrengthGlobalCurr` | 当前 0–100 全局趋势相对强度，与趋势动物当前可用且已更新标的整体比较；支持跨资产比较 | A/HK/US 混合收藏夹的主排序 |
| `trendStrengthLocalChange` | 相对一周前的本地强度变化方向，可能为 `↑↑`、`↑`、空、`↓`、`↓↓` | 展示强度方向，不是数值名次 |
| `trendStrengthLocalPrevWeek` | 一周前本地相对强度 | 周度变化 |
| `trendStrengthLocalPrevMonth` | 一月前本地相对强度 | 月度变化 |
| `return1m` | 最近一月涨幅，小数比例 | 复现截图“月涨幅”，不是趋势强度 |
| `priceIndex` | 平台用于趋势分析的最新价格或指数值，可能与交易所实时价不同 | 复现截图价格时需注明来源 |

项目现有生产字段集已经请求上述本地/全局强度和前周、前月值，[字段清单](../../src/open_trader/a_share_trend.py#L83-L98)；报告把 `trendStrengthLocalCurr` 投影为 `strength`，[平安银行实例](../../reports/trend_a_share/2026-07-31.json#L3227-L3261)。因此无需增加新的评分公式，也无需通过价格历史自行发明“相对强度”。

## 推荐请求形态

以下仅展示参数形态，`<redacted>` 不应替换后写入日志、文档或命令历史：

```text
GET https://www.trendtrader.cn/apiData/data/getUpdateStatus
    ?apiKey=<redacted>

GET https://www.trendtrader.cn/apiData/data/getFavoritesCategory
    ?apiKey=<redacted>

GET https://www.trendtrader.cn/apiData/data/getFavoritesTicker
    ?apiKey=<redacted>
    &favCategory=<URL-encoded category name>

GET https://www.trendtrader.cn/apiData/data/getTickerSnapshot
    ?apiKey=<redacted>
    &tmIds=303121,306177,...
    &fields=tmId,tickerName,tickerSymbol,asset,asOfDate,trendStrengthLocalCurr,trendStrengthGlobalCurr
```

最小数据流：

```text
getUpdateStatus
  -> getFavoritesTicker
  -> collect unique tmId values
  -> one batched getTickerSnapshot
  -> join rows by tmId
  -> global strength desc for cross-market ranking
```

必须检查收藏列表和 snapshot 的 `asOfDate`。项目当前客户端会把 API Key 放在 query string 中、校验通用响应结构，并对 snapshot 做日期和本地缓存控制。[请求与校验](../../src/open_trader/trend_animals.py#L401-L423) [日期缓存](../../src/open_trader/trend_animals.py#L425-L499)

## 成本和速率

第一方 2026-08-02 文档给出的限制与 `priceCost`：

| 调用/字段 | 限制 | `priceCost` |
| --- | ---: | ---: |
| `getFavoritesCategory` | 1 次/秒 | 免费 |
| `getFavoritesTicker` | 1 次/秒 | 免费 |
| `getTickerSnapshot` | 5 次/秒 | 按返回行数和请求字段计费 |
| `trendStrengthLocalCurr` | snapshot 字段 | 每返回行 `0.004` |
| `trendStrengthGlobalCurr` | snapshot 字段 | 每返回行 `0.004` |
| `return1m` | snapshot 字段 | 每返回行 `0.002` |
| `priceIndex` | snapshot 字段 | 每返回行 `0.002` |

按本次“近期收藏”13 行计算，且未超过 20 行、没有批量折扣：

- 只取全局强度：`13 × 0.004 = 0.052`；
- 同时取本地与全局强度：`13 × 0.008 = 0.104`；
- 再加价格和月涨幅：`13 × 0.012 = 0.156`。

若请求截图“全部”中的 14 行，相应为 `0.056`、`0.112`、`0.168`。第一方文档称重复请求命中其缓存不计费，并对 21–100 行按八折、101–300 行按六折；13/14 行不适用折扣。

2026-08-02 的实时 `getApiDocIntro` 和 `getSnapshotColumnBilling` 均把 `balance`、`priceCost` 和消费写成“元”；这与项目历史报告使用“Trend Animals 余额单位”的保守口径不同。[项目历史成本单位](../../src/open_trader/a_share_trend.py#L81-L95) 本次 14 行全局强度真实抽样目录价为 `0.056` 元，因服务端缓存实际余额减少 `0.032` 元。

## “页面内排名”与可复现排名的边界

可以稳定实现三种强度/排名：

1. **跨市场收藏夹排名**：按 `trendStrengthGlobalCurr` 降序，缺失值排最后；
2. **市场内收藏夹排名**：先按 `asset` 分组，再按 `trendStrengthLocalCurr` 降序，缺失值排最后。
3. **小程序页面内强度口径**：在准确的页面品种池内，对
   `trendStrengthGlobalCurr` 排序或计算分位并归一化到 0–100。

第三种是第一方指南明确认可的页面内展示口径，但指南没有公开唯一的 percentile、tie 和 rounding 公式。生产实现应固定一种确定性规则并标注公式；如要求与小程序整数逐点相等，再向供应商确认这三个细节。

若必须复现截图整数，需要使用小程序当时完全相同的收藏池、日期、筛选、并列和舍入规则。不要为此展开全市场；收藏夹接口已经给出正确目标池，缺的只是精确归一化细节。

`getTickerTrendPlot` 也不是替代方案：它可按“强度降序”生成趋势仪表盘或图片，但只返回 Base64 PNG，收费 `0.1`/次，不返回结构化页面名次。

## 认证与运行风险

- 所有相关端点都要求 API Key，且第一方契约把它放在 query string；完整 URL 可能泄漏到代理、访问日志、异常或 shell history。实现时必须复用本地配置，日志中只保留 endpoint、字段名、行数和缓存命中，不打印 URL。
- 收藏夹是账户态数据。当前本机 API Key 的只读抽样与截图结构吻合，但不同 Key、会员权限或小程序账号绑定可能得到不同收藏夹。
- `getFavoritesTicker` 免费不代表后续 snapshot 免费。必须只请求实际使用的字段，并按 `tmId` 批量调用。
- 先调用免费的 `getUpdateStatus`，确认各资产数据日，再拉 snapshot；跨市场更新时间不同，不能把不同 `asOfDate` 的分数强行排成一个“当前”榜单。
- 当前 `TrendAnimalsClient` 已有通用 `_get`、批量 snapshot、缓存、日期校验和凭据防泄漏，但没有收藏夹 wrapper；若后续实现，只需在现有客户端上增加两个薄方法，无需新客户端或新依赖。

## 建议的第一版边界

第一版只做：收藏夹 `tmId` -> 批量 snapshot -> 以全局强度做跨市场排序并生成页面内 0–100 分，同时保留本地强度供市场内解释。不要抓小程序、不要调用图片接口，也不要为了截图整数展开全市场。

如果未来产品明确要求与小程序“页面内排名”逐项相等，再先抓一份同一时刻的页面与 API 对照样本，向趋势动物确认排名分母和并列规则；得到第一方定义后再决定是否扩展。
