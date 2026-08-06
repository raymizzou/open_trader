# 凯利公式在趋势复盘系统中的仓位用法与文章审计

日期：2026-07-19

## 结论先行

给定文章的核心公式本身没有写错，但它把公式输出的**账户风险比例**反复叫成“仓位比例”。这是全文最严重的问题，也使前 3 个案例的百分比在下单语义上都错了。

本项目当前不应让 Kelly 自动决定订单金额。最小且可靠的用法是：继续执行《纪律.md》的固定 4%/2% 首仓上限；Kelly 只能作为**下一策略版本的向下否决门**，即缩小或取消未来新仓，绝不放大现有上限。只有在净成本、样本收缩、尾部压力和组合相关性都有可靠数据后，再考虑 `min(分数 Kelly 名义仓位, 现有固定上限, 流动性/保证金上限)`。

## 1. 公式：先把“风险比例”和“名义仓位”分开

Kelly 的原始目标不是最大化单笔期望收益，而是最大化重复下注后的期望对数财富。Kelly 原论文写成对各结果状态的对数增长求和；Thorp 在证券语境中写为：

\[
g(f)=\mathbb E[\log(1+fR)]
\]

其中 `R` 是每 1 元名义头寸的**净收益率**，`f = 名义头寸 / 交易前账户净值`。一般情况下应直接求：

\[
f^*=\arg\max_{f\in\mathcal F}\mathbb E[\log(1+fR)]
\]

`R` 必须逐状态包含佣金、点差、滑点、资金费、借贷利息、Gas、税费及强平费用；可行域 `\mathcal F` 还必须满足平台保证金、流动性、组合敞口及 `1+fR>0` 等约束。[Kelly 1956 原论文及 DOI](https://doi.org/10.1002/j.1538-7305.1956.tb03809.x)，[可读 PDF](https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf)，[Thorp 2006，第 7–8 节](https://gwern.net/doc/statistics/decision/2006-thorp.pdf)，[出版 DOI](https://doi.org/10.1016/S1872-0978(06)01009-X)。

若每笔交易真的只有两个净结果：

- 胜率 `p`，名义头寸收益 `+W`；
- 败率 `q=1-p`，名义头寸收益 `-L`；

则：

\[
g(f)=p\log(1+fW)+q\log(1-fL)
\]

\[
\boxed{f^*_{\text{notional}}=\frac{pW-qL}{WL}=\frac pL-\frac qW}
\]

如果另定义 `k=fL` 为“该笔正常亏损发生时损失多少账户净值”，并令 `b=W/L`，才得到文章使用的经典赔率式：

\[
\boxed{k^*_{\text{risk}}=p-\frac qb=\frac{bp-q}{b}=\frac{pW-qL}{W}}
\]

因此二者关系是：

\[
\boxed{f^*_{\text{notional}}=\frac{k^*_{\text{risk}}}{L}}
\]

文章的“净优势 ÷ Win%”算出的是 `k_risk`，不是名义仓位 `f_notional`。只有当输一次会亏掉全部下注本金（`L=100%`）时，两者才相同。

当 `W=L` 时，`k_risk=2p-1` 是**精确式**而非近似式；对应名义仓位是 `(2p-1)/L`。所以文章的“52% 胜率 → 4% 仓位”实际上是“52% 胜率 → 4% 账户风险预算”。

## 2. 对给定文章的逐项审计

### 2.1 正确或方向正确的部分

- Kelly 确实解决“已知/估计结果分布时，下多少”，不预测涨跌。
- 最大化长期期望对数增长、避免过度下注，是 Kelly/Thorp 的原意。
- 现实参数不确定、非平稳、数据挖掘偏差会使全 Kelly 严重过度下注；Thorp 明确建议在估计不确定时低于估计的全 Kelly，并讨论 `0.5f*` 到 `f*` 的保护作用。[Thorp 2006，第 7.3 节](https://gwern.net/doc/statistics/decision/2006-thorp.pdf)
- 在零无风险利率的连续扩散近似下，使用 `c` 倍 Kelly 的相对增长率为 `c(2-c)`，风险标准差为全 Kelly 的 `c` 倍；所以半 Kelly 在该特定模型下保留 75% 的增长率、承担 50% 的标准差。这不是任意肥尾/离散交易分布下的普适保证。
- 成本、尾部、同向敞口、杠杆和强平必须进入计算，这些方向都对。

### 2.2 公式与数字错误

| 位置 | 文章写法 | 审计结果 |
|---|---|---|
| 主公式及口算第 ③ 步 | `(pW-qL)/W` 称“建议仓位” | 这是 `k_risk`，即正常亏损状态下损失的账户净值比例。名义仓位还要除以 `L`。 |
| 对称速表 | `52%→4%，55%→10%...` 仓位 | 这是赔率为 1:1 且失败会损失全部下注风险单位时的风险预算；若止损为 5%，55% 对应全 Kelly 名义仓位是 `10%/5%=200%`。 |
| 案例 1 | 55%、+5%/-3% → 半 Kelly 14%/笔 | `28%` 是全 Kelly 风险预算；全 Kelly 名义仓位是 `28%/3%=9.33x`，半 Kelly是 `4.67x`，正常止损损失账户 14%。52% 和 51% 时，半 Kelly名义仓位分别是 `3.87x`、`3.60x`，不是 11.6%、10.8% 名义仓位。 |
| 案例 2 | 70%、+2%/-1% → 14%–27% 仓位 | `55%` 是风险预算。全 Kelly 名义仓位是 `55x`，1/4–1/2 Kelly 是 `13.75x–27.5x`。这类“套利”的罕见崩盘状态若未进入分布，二元均值公式尤其危险。成本上升例的 1/4–1/2 名义仓位是约 `9.58x–19.17x`；第三组是 `5x–10x`。 |
| 案例 3 | 平均亏损 8.3%，得到 2%–4% 仓位 | 8.3% 的算术平均正确，但把三状态分布压成一次“平均亏损”不再是精确 Kelly。按文章的平均二元法，1/4–1/2 名义仓位也应是 `24%–48%`。保留三状态 `(+7%,58%; -5%,35%; -25%,7%)`，直接解 `Σ p_i r_i/(1+fr_i)=0` 得全 Kelly 名义仓位约 `63.46%`，1/4–1/2 为 `15.86%–31.73%`。 |
| 案例 4 | 相关性 >0.7 时合并仓位打 7 折 | 这是无来源的启发式，不是 Kelly 修正。Thorp 明确指出：精确求同时下注时，只有协方差/相关系数仍不够，必须使用完整联合分布。扩散近似下才有 `F*=C^{-1}(M-R)`。 |
| 案例 5 | 5x 永续约 -10% 强平；`10÷3≈1.67` | 两处独立错误。首先 `10÷3=3.33`，不是 1.67。其次 5x 的强平距离不能统一写成 10%。按 Bybit 线性 USDT 隔离保证金官方式，在忽略额外保证金、费率和档位扣减，且 MMR=0.5% 的简化条件下，`P_liq/P_entry=(1-1/5)/(1-0.005)=0.8040`，距离约 19.60%。真实值还取决于仓位档位、MMR、平仓费、保证金模式和 Mark Price。 |
| 案例 6 | 先按策略算，再按因子聚合并统一乘 1/2 | “按共同因子看总敞口”方向正确，但这不是组合 Kelly 算法。需要同一时间轴上的联合收益/情景分布；单策略 Kelly 相加后再统一打折没有增长最优保证。 |

### 2.3 容易误导但不一定是纯算术错误的部分

1. **“最近至少 200 笔”没有理论保证。** 可靠度取决于真实优势大小、先验、序列相关、市场状态、策略选择过程和分布漂移，不存在通用的 200 笔安全线。参数不确定研究的结论是应按不确定度收缩，而不是跨过固定样本数就突然相信原始胜率。[Baker & McHale 2013](https://doi.org/10.1287/deca.2013.0271)
2. **“贝叶斯收缩到 50%”必须声明先验。** Beta-Binomial 下后验均值为 `(wins+α)/(n+α+β)`；只有对称先验 `α=β` 的先验均值才是 50%，其“等效样本量”是 `α+β`。Bayesian Kelly 应随后验状态更新，不是把任意小样本机械线性拉到 50%。[Browne & Whitt 1996，作者机构 PDF](https://business.columbia.edu/sites/default/files-efs/pubfiles/6343/bayes_kelly.pdf)，[DOI](https://doi.org/10.2307/1428168)
3. **平均赢幅/平均亏幅只对真正的二点分布精确。** Meme 插针案例本身已经证明需要完整分布；算平均数后再套二元公式会丢失 `log` 对尾部和破产状态的非线性惩罚。
4. **硬止损不能保证截尾。** Bybit 官方说明市价退出会滑点、流动性不足可部分成交；止损还可能因 Mark Price 与 Last Traded Price 的触发差异而晚于强平。因此复盘应使用实际成交亏损分布，不应把计划止损直接当最大亏损。[Bybit 订单与强平 FAQ](https://www.bybit.com/en/help-center/article/FAQ-Order-Execution-and-Liquidation)，[Bybit 市价单滑点规则](https://www.bybit.com/en/help-center/article/Market-Order-with-Slippage-Tolerance)
5. **全 Kelly 的路径风险非常大。** Thorp 的连续近似在 `r=0` 时给出全 Kelly 财富曾跌至初始财富 `x` 的概率为 `x`；即模型内也有 50% 概率曾腰斩。Kelly 是渐近增长最优，不是“保证曲线稳定”或“把运气变成可重复增长”。
6. **风险厌恶 >1 等于分数 Kelly 只在特定模型内成立。** Ziemba 给出的 `δ=1/(1-α)` 关系在连续时间对数正态资产或离散时间正态资产下精确，在其他分布下只是近似。[Ziemba 2003，CFA Research Foundation 原著，第 169–170 页](https://rpc.cfainstitute.org/sites/default/files/-/media/documents/book/rf-publication/2003/rf-v2003-n3-3924-pdf.pdf)；连续时间组合背景可参见 [Merton 1969 DOI](https://doi.org/10.2307/1926560)。

## 3. 本项目现有 Kelly 实现审计

当前《纪律.md》明确写着“当前运行版本不使用 Kelly 调整仓位”，这在下列问题修正前应保持不变。

截至 2026-07-19，`data/latest/kelly_trade_samples.json` 的规范样本数是 0，`data/latest/kelly_strategy_stats.json` 三个实验的 `completed_samples` 也都是 0。趋势复盘里 A 股显示的是 0 个纪律闭环和 4 个实际周期；这 4 个实际周期来自当前策略版本生效前已经存在的开仓，不能倒灌成该版本的 Kelly 样本。美股、港股纪律闭环也都是 0。因此当前没有任何可用于本策略 Kelly 估参的合格闭环样本，文章所谓 200 笔门槛在本项目眼下不是主要矛盾。

### 3.1 `net_pnl_pct` 实际是毛收益

`src/open_trader/kelly_trade_samples.py::_completed_sample` 当前计算：

```text
gross_pnl = exit_notional - entry_notional
net_pnl_pct = gross_pnl / entry_notional
```

成交均价已经反映实际成交滑点，但佣金、平台费、税费、借贷/资金费等没有扣除。字段名 `net_pnl_pct` 因此不准确，且会系统性高估 `W`、低估 `L`。用于 Kelly 前至少应有可审计的 `fees_total` 与真正的 `net_pnl`；加密永续还需要 funding、借币利率、强平费用。

这里不能靠给富途模拟盘多调一个费用接口解决：富途官方明确说明模拟账户不支持 `order_fee_query`。纪律模拟样本必须二选一：冻结一套保守、可版本化的券商费用模型；或用能够与纪律动作一一关联的真实成交费用校准。费用来源不完整时只展示毛收益诊断，不生成 Kelly 仓位建议。[富途订单费用接口限制](https://openapi.futunn.com/futu-api-doc/en/trade/order-fee-query.html)，[富途模拟交易限制](https://openapi.futunn.com/futu-api-doc/en/qa/trade.html)。

### 3.2 经典风险比例被当成名义仓位

`src/open_trader/kelly_strategy_stats.py::_kelly_fraction` 使用：

```text
p - (1-p) / payoff_ratio
```

这正是上文的 `k_risk`。随后它被写入 `suggested_position_pct` 并与 4% 名义仓位上限直接比较，量纲不一致。固定 4% 上限通常让结果保持保守，但不能让错误语义变正确。

两条可选修正路径：

- 若继续二元近似：明确改名为 `kelly_risk_fraction`，再用 `notional_fraction = risk_fraction / avg_net_loss_fraction` 转成名义仓位；
- 更推荐：直接从每笔净名义收益率 `r_i` 数值最大化 `mean(log1p(f*r_i))`，输出天然就是名义仓位 `f`，无需赔率语义转换。

### 3.3 小样本收缩在第 200 笔不连续

当前 `<200` 笔时使用：

```text
adjusted_p = (wins + 100) / (n + 200)
```

这等价于 Beta(100,100) 的强先验后验均值；但到 `n>=200` 时突然完全改用原始胜率。例：199 笔 120 胜时调整胜率约 55.14%；下一笔即使亏损，变成 200 笔 120 胜后代码却跳到 60%。

此外，这与 `docs/superpowers/specs/2026-07-11-kelly-trade-samples-design.md` 写的线性 `min(n/200,1)` 也不同。应只保留一个连续、明确先验含义且经过样本外验证的规则；不能以 200 为断点关闭收缩。

### 3.4 Flat 被隐含当作 Loss

`raw_win_rate = wins / completed`，而公式中的 `q=1-p` 包含 flat；但 `avg_net_loss` 又只对 loss 求平均。这相当于把零收益样本按平均亏损赔率处理。

若存在 flat，正确做法是使用完整三状态分布；在完全二元化时也应先明确 flat 的处理并保持概率与回报状态一致。直接优化经验 `log1p` 可自然保留 flat。

### 3.5 平均数丢失尾部与组合同时暴露

现实现只保存胜负均值并逐实验算 Kelly，没有用完整单笔收益分布做增长目标，也没有同步多个持仓的联合收益情景。因此它不能支持文章案例 3 的肥尾修正，也不能支持案例 4/6 的组合 Kelly。季度/月度重新计算不会补回这些丢失的信息。

### 3.6 Partial fill 被跳过

`src/open_trader/kelly_trade_samples.py::_order_skip_reason` 将任何 partial fill 标记为 `partial_fill_not_supported`；买卖数量不同也不会形成完成样本。跳过并记录诊断比错误配对更安全，但实际交易中部分成交往往与流动性、滑点和压力期相关，长期全部删除会造成选择偏差。开始用 Kelly 前应先支持按成交明细加权配对及其真实费用；当前零样本阶段无需提前搭复杂撮合器。

## 4. 复盘系统的最小可靠用法

### 阶段 A：现在就能用——诊断和否决，不自动调仓

保持当前 4%/2% 固定首仓规则。先积累当前版本的合格闭环；样本为 0 时只显示“不可估计”，不展示伪精确的 0% Kelly。将来每个策略版本可在复盘页增加 Kelly 诊断，但对当前版本不追溯改仓，对下一版本也只能向下调整：

1. 展示样本区间、完成笔数、胜/负/平、成本覆盖项、原始与收缩参数；
2. 使用逐笔真实净收益计算当前固定仓位 4%/2% 下的 `mean(log1p(f*r_i))`；
3. 保守估计或尾部压力后 `g(f)<=0` 时，Kelly 只做“禁止新增”信号；
4. `g(f)>0` 也不放大，仍执行固定上限；Kelly 只允许把下一版本从 4%/2% 降到更低或跳过；
5. Kelly 变化不触发持仓中补仓或再平衡，符合《纪律.md》。

这利用了 Kelly 最有价值的部分——识别过度下注和负增长策略——而不让不稳定参数制造虚假的精确仓位。

现有代码的最小接入点已经存在于 `build_trend_review_projection`：它能重建纪律模拟和实际执行的完整持仓周期。接入时只新增一个独立、只读的版本校准产物，不改变当前五项复盘指标；Kelly 样本必须额外证明开仓发生在版本生效后、完整成本可得且属于纪律内交易，不能直接复用页面上的周期计数。用户批准新版本后，再把下调后的 4%/2% 写入新的 `strategy_snapshot.target_weight`；当前 A 股下单逻辑仍从硬编码 `CN_TARGET_WEIGHTS` 取值，实施时需让它消费已批准快照，不能由 Dashboard 临时覆盖。

### 阶段 B：数据足够后——单策略分数 Kelly

每个完成样本至少记录：

- 策略/规则版本和市场状态；
- 交易前策略净值、名义头寸、杠杆和保证金模式；
- 进出场实际成交额；
- 佣金、税费、点差/滑点、资金费、借贷利息、Gas、强平费；
- 净 PnL、净名义收益率 `R`、账户收益；
- 计划止损、实际触发价、实际成交价和最大不利变动；
- 与其他持仓重叠的时间区间和共同因子标签。

然后按策略版本/可解释市场状态滚动估计，保留样本外记录；用完整收益分布求 `f*`，先乘固定的保守分数 `c`，再走现有风控：

\[
f_{exec}=\min(c f^*,\ f_{strategy\ cap},\ f_{market\ cap},\ f_{liquidity},\ f_{margin/liquidation})
\]

`c=1/4` 可作为研究起点，但它不是由“少于 200 笔”自动推导出的定理；最终应由参数不确定度、样本外表现和可接受回撤确定。

### 阶段 C：只有同时持仓明显时——组合 Kelly

把同一时间桶内各策略/币种净收益组成向量 `R_t`，求：

\[
\max_{\mathbf f}\frac1T\sum_t\log(1+\mathbf f^\top\mathbf R_t)
\]

并加入净敞口、市场、因子、保证金和最坏情景约束。若只做连续小波动近似，可用 Thorp 的：

\[
\mathbf F^*=\mathbf C^{-1}(\mathbf M-\mathbf R_f)
\]

但加密的跳跃、肥尾和强平使完整情景法更可信。不要实现“相关性大于 0.7 就打 7 折”作为所谓 Kelly 算法；若只需要当前固定上限，现有总敞口硬帽已经更简单、更可审计。

## 5. 杠杆与强平的系统规则

Bybit 当前官方线性 USDT 隔离保证金多头公式为：

\[
P_{liq}=\frac{P_0Q-P_0Q/\lambda-M_{extra}/(1-fee)-MM_{deduction}}
{Q-Q\cdot MMR}
\]

实际平台还按 Mark Price 触发强平；Cross/Portfolio Margin 的页面强平价只是参考，真正由账户 MMR 决定。[Bybit 隔离保证金强平价官方公式](https://www.bybit.com/en/help-center/article/Liquidation-Price-Calculation-under-Isolated-Mode-Unified-Trading-Account)，[Bybit 保证金/强平说明](https://www.bybit.com/en/help-center/article/FAQ-USDT-Perpetual-and-Expiry-Contracts)。

因此复盘系统不应自行用 `约 1/杠杆` 生成安全仓位。应读取或按对应交易所、合约、档位、保证金模式计算实际强平价，并同时检查：

- 计划止损与强平都使用什么触发价格；
- 止损市价滑点后的压力退出价；
- 多持仓对 Cross/Portfolio Margin MMR 的共同影响；
- 降杠杆是否只是增加保证金，还是同时减少名义敞口；Bybit 官方明确说明杠杆本身不改变给定名义头寸的 PnL，只改变初始保证金。

最终约束应是可验证的“压力退出后仍不触发强平”，而不是文章中没有平台定义的“安全系数 ≥2”。

## 6. 主要原始资料

1. John L. Kelly Jr., 1956, *A New Interpretation of Information Rate*: [DOI](https://doi.org/10.1002/j.1538-7305.1956.tb03809.x), [PDF](https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf).
2. Edward O. Thorp, 1969, *Optimal Gambling Systems for Favorable Games*: [作者站 PDF](https://www.edwardothorp.com/wp-content/uploads/2016/11/OptimalGamblingSystemsForFavorableGames.pdf), [DOI](https://doi.org/10.2307/1402118).
3. Edward O. Thorp, 2006, *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market*: [全文 PDF](https://gwern.net/doc/statistics/decision/2006-thorp.pdf), [出版 DOI](https://doi.org/10.1016/S1872-0978(06)01009-X).
4. Rose D. Baker and Ian G. McHale, 2013, *Optimal Betting Under Parameter Uncertainty*: [INFORMS/DOI](https://doi.org/10.1287/deca.2013.0271).
5. Sid Browne and Ward Whitt, 1996, *Portfolio Choice and the Bayesian Kelly Criterion*: [作者机构 PDF](https://business.columbia.edu/sites/default/files-efs/pubfiles/6343/bayes_kelly.pdf), [DOI](https://doi.org/10.2307/1428168).
6. William T. Ziemba, 2003, *The Stochastic Programming Approach to Asset, Liability, and Wealth Management*: [CFA Research Foundation PDF](https://rpc.cfainstitute.org/sites/default/files/-/media/documents/book/rf-publication/2003/rf-v2003-n3-3924-pdf.pdf).
7. Bybit 官方文档：[隔离保证金强平公式](https://www.bybit.com/en/help-center/article/Liquidation-Price-Calculation-under-Isolated-Mode-Unified-Trading-Account)，[订单与强平 FAQ](https://www.bybit.com/en/help-center/article/FAQ-Order-Execution-and-Liquidation)，[市场单滑点](https://www.bybit.com/en/help-center/article/Market-Order-with-Slippage-Tolerance)，[USDT 永续 FAQ](https://www.bybit.com/en/help-center/article/FAQ-USDT-Perpetual-and-Expiry-Contracts)。
8. 富途官方文档：[订单费用接口](https://openapi.futunn.com/futu-api-doc/en/trade/order-fee-query.html)，[模拟交易限制](https://openapi.futunn.com/futu-api-doc/en/qa/trade.html)。
