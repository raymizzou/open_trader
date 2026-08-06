# 用行业右侧宽度优化趋势选股：证据、边界与最小验证方案

> 调研日期：2026-07-24  
> 研究问题：截图中的“行业温度、温转热个数、右侧品种占比”能否科学地优化当前趋势选股。  
> 结论口径：以下严格区分**证据**、**推断**和**建议**；不把供应商专有字段等同于论文中的公开指标。

## 结论先行

截图的方向是合理的，但“右侧品种占比 18%”本身不能直接产生买入结论。至少还缺四件事：

1. 18% 的完整分母、有效覆盖率和成分范围；
2. 同一口径的自身历史分布；
3. 行业相对全市场的宽度，而不只是行业绝对宽度；
4. 该信号在 A 股、当前策略和真实交易成本下的未来样本外结果。

当前最值得做的不是给三个指标随意加权，而是从现在开始冻结完整行业成分和每日快照，保留当前策略为基线，预先锁定少量对照版本。学术证据支持“行业动量存在”和“市场状态会改变横截面动量表现”，也有直接研究发现市场/行业 breadth 含有独立预测信息；但没有原始资料证明趋势动物的 `isTrendRightSide`、温度分级或截图中的 18% 阈值本身有超额收益。

最小可验证版本只需要四个量：

\[
B_{g,t}=\frac{\text{行业 }g\text{ 中右侧标的数}}{\text{行业 }g\text{ 中右侧状态有效的标的数}}
\]

\[
W_{g,t}=\frac{\text{行业 }g\text{ 中当日“温}\rightarrow\text{热/沸”的标的数}}{\text{行业 }g\text{ 中温度状态有效的标的数}}
\]

\[
RB_{g,t}=B_{g,t}-B_{\text{全市场},t}
\]

\[
M_{g,t}^{20}=
\left(\prod_{d=t-21}^{t-1}(1+r_{g,d})-1\right)
-
\left(\prod_{d=t-21}^{t-1}(1+r_{m,d})-1\right)
\]

其中 `B` 是右侧存量，`W` 是温转热流量，`RB` 剔除全市场共同涨跌，`M20` 是可由价格重建的 20 个交易日行业相对强度。行业温度保留为类别变量，不先验映射成任意的 1–5 分。

## 1. 当前系统与截图信息的真实差距

### 证据：当前 A 股策略已经用到个股和行业趋势状态

当前运行代码要求个股由“温”转“热/沸”、个股明确处于右侧，同时检查强度、市值、成交额、节气、可交易、危险信号、ATR 等条件；实时 A 股版本目前允许行业温度为“温、热、沸”。通过过滤后，排序仍是“个股强度降序、右侧天数升序、成交额降序、股票代码升序”，没有行业右侧占比或行业相对强度。来源：[当前 A 股候选过滤与排序（`a_share_trend.py`）](../../src/open_trader/a_share_trend.py#L1318)、[当前实时策略快照版本与行业温度投影（`a_share_trend.py`）](../../src/open_trader/a_share_trend.py#L560)。

### 证据：现有候选响应不能充当行业 breadth 的分母

生产客户端请求 `getComponentTicker` 时固定发送 `getAllBasicComponentsFlag=0`，取得的是策略组合成分；它不是代码中已经维护的一份完整行业股票母集。来源：[生产客户端 `get_components` 调用（`trend_animals.py`）](../../src/open_trader/trend_animals.py#L129)。

趋势动物官方 OpenAPI 只公开了 `getComponentTicker` 的参数名和默认值，没有解释 `getAllBasicComponentsFlag` 两个取值对应的业务集合，也没有公开 `isTrendRightSide` 或温度的计算公式。来源：[趋势动物官方 OpenAPI JSON](https://www.trendtrader.cn/apiData/v3/api-docs)。

### 推断

不能用“温转热候选池中的右侧比例”代替“全行业右侧比例”。前者的分母已经先经过温度、组合或供应商筛选，既近似自证，又会随候选池规则改变。正确分母必须是信号产生前、当天可知、完整且点时一致的行业母集。

### 证据：目前无法补齐严格历史

趋势动物公开 API 没有日期查询参数；项目此前的官方契约核验和真实抽样均确认，客户端不能指定过去日期取得完整候选池或快照。因此不能拿今天的成分反套过去行情来制造历史 breadth。来源：[趋势动物历史候选池可得性调研](2026-07-17-trend-animals-historical-candidate-pools.md)、[趋势动物官方 OpenAPI JSON](https://www.trendtrader.cn/apiData/v3/api-docs)。

### 建议

在拿到“完整行业母集”且核清 `getAllBasicComponentsFlag` 的实际契约前，只展示供应商给出的聚合图，不让它影响订单。最小的数据动作是从可用日起每天冻结：

- 行业母集及每个标的的行业 ID、行业名；
- `isTrendRightSide`、当前/前一温度、数据日期；
- 母集总数、有效状态数、缺失数；
- API 查询参数、响应内容哈希、进程 Git SHA；
- 当天行业分类变化和标的上市、退市、ST、停牌状态。

## 2. 学术证据到底支持什么

### 2.1 行业动量 / 跨行业相对强度

### 证据

Moskowitz 与 Grinblatt 发现，行业收益中的动量能解释相当一部分个股动量；买入过去赢家行业、卖出过去输家行业的策略，在控制规模、账面市值比、个股动量、平均收益离散度和微观结构影响后仍有显著收益。来源：[《Do Industries Explain Momentum?》— *The Journal of Finance*](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00146)。

针对中国市场的后续原始研究报告了显著行业动量，并发现其主要出现在高成交量行业，低成交量行业不明显。来源：[《Industry momentum and trading volume: evidence from China》— *Managerial Finance*](https://doi.org/10.1108/MF-08-2022-0397)。

不过，较新的研究表明行业动量与更广泛的因子动量高度相关，行业动量未必是一个独立风险因子。来源：[《Factor Momentum》— *The Review of Financial Studies*](https://academic.oup.com/rfs/article-abstract/36/8/3034/6988043)、[《Factor Momentum and the Momentum Factor》— *The Journal of Finance*](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.13131)。

### 推断

“行业先强、个股再强”有合理证据，但不能推出趋势动物“行业温度热/沸”就是论文里的行业动量。可重建的行业价格相对强度 `M20` 应与供应商温度并列保存，分别验证增量信息；若二者高度重复，就保留更透明、成本更低的一项。

### 2.2 市场 / 行业 breadth

### 证据

Zaremba 等人把 breadth 定义为一个市场或行业内平均上涨股数与下跌股数之差，再除以上涨与下跌股数之和：

\[
MBR_{i,t}=\frac{RS_{i,t-1}-FS_{i,t-1}}{RS_{i,t-1}+FS_{i,t-1}}
\]

他们对 64 个市场、1973–2018 年的国家和行业组合做横截面检验，报告高 breadth 组合未来收益高于低 breadth 组合；结果在控制动量、趋势、规模、价值、波动率等变量后仍存在。研究还发现国家层面的 breadth 效应强于行业层面，频繁换仓带来的成本是实际实施问题。来源：[《Herding for profits: Market breadth and the cross-section of global equity returns》— *Economic Modelling*](https://www.sciencedirect.com/science/article/pii/S0264999319312982)。

该研究为避免过小组合，要求行业/国家组合平均至少覆盖 10 家公司；其行业 breadth 是上涨/下跌股数，不是专有的“右侧”状态。来源：[《Herding for Profits》作者稿，数据与样本要求](https://assets.super.so/e46b77e7-ee08-445e-b43f-4ffd88ae0a0e/files/78ca4dbe-e3ff-4876-a807-c2244f2adc51.pdf)。

### 推断

`isTrendRightSide=True` 的占比和学术 breadth 都在描述“有多少股票共同处于某个正向状态”，所以值得检验；但二者信号定义、时间尺度和供应商处理均不同。学术结果只能支持“把它当候选解释变量”，不能支持“18% 就加仓”。

### 2.3 breadth 为什么应与市场状态一起看

### 证据

Cooper、Gutierrez 与 Hameed 发现美国横截面动量利润依赖先前市场状态：1929–1995 年间，正市场状态之后的月度动量收益为正，负市场状态之后没有同样结果。来源：[《Market States and Momentum》— *The Journal of Finance*](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2004.00665.x)。

Daniel 与 Moskowitz 发现动量会发生少见但严重的崩盘，尤其在市场下跌和高波动后的“恐慌状态”，并常与市场急速反弹同时发生。来源：[《Momentum Crashes》— NBER Working Paper 20439 / *Journal of Financial Economics*](https://www.nber.org/papers/w20439)。

中国证据并不机械复制美国结果。Cheema 与 Nartea 报告，中国样本中的动量状态依赖方向与美国研究不同。来源：[《Momentum returns, market states, and market dynamics: Is China different?》— *International Review of Economics & Finance*](https://doi.org/10.1016/j.iref.2017.04.003)。

### 推断

全市场右侧比例更适合做风险状态或信号可信度变量，而不是直接改某只股票的分数。截图中的“美股整体右侧 18%”若处于其自身历史低分位且仍在下降，可能意味着个股强势缺乏广泛参与；但在没有同口径历史和 A 股样本外证据前，这只能是待检验假设。

## 3. 最小指标定义

以下定义不需要机器学习、任意权重或新依赖。

### 3.1 点时一致的母集

对每个交易日 `t` 和行业 `g`，先冻结母集 `U(g,t)`：

- 使用当天实际可知的行业归属；
- 沪深 A 股范围与策略一致，但上市、退市、ST、停牌、字段缺失分别记录，不事后删除；
- 先定义母集，再统计温度、右侧、强度，不能用结果字段反过来筛母集；
- 同一公司、同一证券去重规则固定并版本化。

`N_total(g,t)=|U(g,t)|`；`N_right_valid(g,t)` 是右侧字段为明确布尔值的数量；覆盖率为：

\[
C^R_{g,t}=\frac{N_{\text{right valid}}(g,t)}{N_{\text{total}}(g,t)}
\]

缺失值不当作 `False`。覆盖率不够时信号应为“不可用”，而不是错误的低 breadth。

### 3.2 右侧存量、温转热流量和市场残差

\[
B_{g,t}=\frac{\sum_{i\in U(g,t)}1[\text{right}_{i,t}=True]}
{N_{\text{right valid}}(g,t)}
\]

\[
W_{g,t}=
\frac{\sum_{i\in U(g,t)}
1[T_{i,t-1}=\text{温}\land T_{i,t}\in\{\text{热,沸}\}]}
{N_{\text{temperature pair valid}}(g,t)}
\]

\[
RB_{g,t}=B_{g,t}-B_{m,t}
\]

`B` 回答“这个行业已经有多宽”，`W` 回答“今天有多少新扩散”，`RB` 回答“这是不是仅由全市场共同上涨造成”。截图显示“温转热个数”时，应同时展示 `W`；原始个数会系统性偏爱股票数多的行业。

### 3.3 行业价格相对强度

用母集内个股前一日可得市值做权重，构造行业日收益；首个研究版本只计算一个 20 交易日窗口，并截止到 `t-1`：

\[
M_{g,t}^{20}=R_{g,t-21:t-1}-R_{m,t-21:t-1}
\]

选择 20 日是贴近当前短线策略的**研究起点**，不是论文证明的最优参数；不得再同时扫描 5、10、20、40、60、120 日后只报告最好者。

### 3.4 行业温度

行业温度只按 `温 / 热 / 沸` 三个类别分层比较结果，不先验假定“温=1、热=2、沸=3”之间等距。供应商若没有公开计算公式或版本号，必须冻结原始值和响应哈希，防止规则漂移无法审计。

### 3.5 最小可靠性规则

以下是**建议的起始口径，不是学术定理**：

- `N_total < 10`：不生成行业 breadth，合并到更高一级行业或标为不可用；
- `C^R < 90%` 或温度成对覆盖率 `< 90%`：不生成对应信号；
- 不用固定的 18%、30%、50% 当跨市场阈值；
- 若要判断高低，使用只包含 `t` 以前数据的行业自身滚动分位；
- 新行业不足一个完整回看窗时，不补猜历史。

## 4. 怎样最小地接入当前趋势选股

### 建议：先影子记录，不立即改仓位

保持当前版本的过滤、排序、4%/2% 目标仓位和风险上限不变。每日对所有候选额外记录 `B`、`W`、`RB`、`M20`，同时保存如果采用新规则时会得到的候选次序。这样不会让一个尚未验证的截图指标直接影响订单。

### 建议：预先锁定四个版本

只比较以下四个版本，避免参数海选：

| 版本 | 唯一变化 |
|---|---|
| A | 当前运行策略，作为基线 |
| B | A，但行业温度只允许“热/沸” |
| C | A 的全部硬过滤不变；候选先按 `RB`、再按 `W` 降序，随后沿用当前个股排序 |
| D | C 的基础上，在 `W` 之后加入 `M20` 降序 |

这四个版本分别回答：

1. 放宽到“温”是否损害选择；
2. 行业右侧宽度是否有增量；
3. 温转热流量是否能区分正在扩散和已经拥挤；
4. 透明的价格相对强度是否还能增加信息。

不要先做 `0.3×温度+0.4×breadth+0.3×强度` 一类综合分。权重、窗口、阈值和行业层级一旦同时搜索，很容易只是在有限历史中挑噪声。

### 建议：全市场 breadth 暂只做影子风险门

另记录一个不下单的市场状态：

\[
G_t=1[B_{m,t}\text{ 位于仅用历史计算的低分位且 } \Delta_5B_{m,t}<0]
\]

比较 `G=0` 与 `G=1` 下当前策略的收益、回撤和失败交易。只有未来样本显示低且继续恶化的 market breadth 稳定降低净收益或放大回撤，下一策略版本才考虑“禁止新开仓”；不要据此强制卖出现有仓位，更不要在高 breadth 时放大仓位。

## 5. 回测与前瞻验证的防偏差清单

### 5.1 时间边界

- `t` 日信号只能使用 `t` 日报告生成时已经到达的数据；
- 当前策略在下一交易日 09:30–10:00 执行，新版本必须沿用同一执行延迟；
- `M20` 截止 `t-1`，除非能够证明 `t` 日收盘数据在订单决策前已稳定到达；
- 行业状态、成分、ST、停牌和可交易性全部使用当日版本。

### 5.2 分类漂移

GICS 会在公司重大重组或新年报出现时复核公司分类，并每年复核分类结构；这直接说明行业标签不是永久常量。来源：[《Global Industry Classification Standard (GICS) Methodology》，第 4–5 节](https://www.msci.com/documents/1296102/11185224/GICS%2BMethodology%2B2022.pdf)。

Kenneth French 行业组合在每年 6 月按“当时”的四位 SIC 归类，并使用上一财年的 Compustat SIC 或当年 6 月 CRSP SIC，展示了点时行业归属的标准做法。来源：[Kenneth R. French Data Library — Detail for 12 Industry Portfolios](https://mba.tuck.dartmouth.edu/pages/faculty/ken.French/Data_Library/det_12_ind_port.html)。

中证全指行业指数也先按中证全指当期样本和中证行业分类划分 11 个一级行业，再构造各级行业指数。来源：[《中证全指行业指数编制方案》— 中证指数有限公司](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/H30199_Index_Methodology_cn.pdf)。

因此，不能把今天的行业归属回填到历史。每天必须冻结 `industryTmId`、行业名和分类来源；发生迁移时历史记录不改写。

### 5.3 幸存者与退市偏差

只下载今天仍上市的股票会把失败公司从历史母集删除。CRSP 官方数据专门维护退市收益，并将退市后价值与最后交易价比较；若证券被确认一文不值，退市收益为 `-100%`。来源：[CRSP US Stock & Indexes Databases Data Descriptions Guide — Delisting Return](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-data-descriptions-guide-crspaccess/)。

项目在 A 股研究中也必须保存当日母集和退出原因；不能因后来退市、ST 或长期停牌而从历史 breadth 和收益中消失。

### 5.4 样本选择和数据窥探

Harvey、Liu 与 Zhu 指出，在大量因子被反复试验时，传统 `t=2` 不足以控制假发现，并认为真实未来样本是最干净的样本外验证。来源：[《… and the Cross-Section of Expected Returns》— *The Review of Financial Studies*](https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF)。

因此本研究需要：

- 在看到结果前锁定 A/B/C/D、20 日窗口、交易成本和评价指标；
- 记录所有尝试过的版本，失败版本不能从试验数中消失；
- 用未来逐日冻结的数据做真正样本外验证；
- 先报告净收益、相对基线收益、最大回撤、换手和完整交易数，不只报告最佳 Sharpe；
- 重叠的 5/20 日前瞻收益按决策日分块估计不确定度，不能把每只股票当完全独立样本。

### 5.5 当前数据能做与不能做的事

当前可做：

- 从 2026-07-24 起建立完整、不可变的行业 breadth 影子账；
- 对每个真实候选生成 A/B/C/D 的当日反事实排序；
- 随实际成交和退出逐步比较净结果。

当前不能做：

- 用 2026-07-24 的行业成分重建 2025 年 breadth；
- 用候选池里的 20–30 只股票假装代表全市场行业宽度；
- 用截图中的 18% 直接恢复供应商公式或历史阈值；
- 因为论文在美国或多国组合上成立，就宣称 A 股当前短线策略已经获得同样增益。

## 6. 最终建议

1. **把截图当研究假设，不当新纪律。** “右侧比例高、温转热多、行业温度高”方向有行业动量和 breadth 证据，但供应商字段未被论文直接验证。
2. **先解决分母。** 没有完整、点时一致的行业母集，就没有科学的右侧占比；候选池占比应明确禁止进入策略。
3. **存量、流量、市场残差分开。** 使用 `B`、`W`、`RB`，不要只看一个绝对比例。
4. **增加一个透明对照。** 用 `M20` 检验行业价格相对强度；若供应商温度没有增量，就不需要复杂评分。
5. **先做排序增强，不自动加仓。** 未来样本外通过后，最小上线方式是把 `RB/W/M20` 放在现有个股排序之前；市场 breadth 只有证明能降低净回撤后才做新开仓否决，仓位仍由现有风险规则决定。

