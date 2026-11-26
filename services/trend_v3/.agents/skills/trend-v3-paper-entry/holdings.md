# 模拟盘持仓输入 v1

正常日报由同版 `PaperEntry.status` 产生 `paper_entry.holdings`，不是将委托串当持仓。
外层保留 `trend-v3-paper-entry.v1`、date、report_version、results 和 skipped；后两者
供内部审批与委托核查，正常持仓页面不展示。持仓对象使用
`schema: trend-v3-paper-holdings.v1`，date 与独立核实的报告日一致，report_version
与独立核实的版次一致，positions 为数组。缺少新版对象的历史报告仍按旧版展示；
新版对象错误则显示待核，不回退到委托清单。

每行只允许 code、market、product、currency、quantity、average_entry、
unrealized_pnl。market 为 CRYPTO/US/HK/CN/JP/SG/AU/CA/MY；product 为
crypto/stock/etf/future；currency 为对应本币 USD/HKD/CNY/JPY/SGD/AUD/CAD/MYR。
数量、均价、盈亏用有限十进制字符串；null 表示缺证，不能填 0。数量为带符号净仓，
空头为负，零仓不显示。股票/ETF 单位为股，期货为张，加密为明确的基础币代码。
盈亏用本币，正数红色带 +，负数绿色带 −，真实零显示 0.00，未知显示 —。
禁止放入账户、委托编号或原始来源路径。持仓按市场分区，期货另分区，每表仅四列。

私有账本按模拟账户身份、合约、市场、品种、币种和乘数归属；只使用已保存 V3 委托
的实际累计成交数量/金额。重复回执不增加仓位，均价按实成数量加权并扣除合约乘数。
对账/正常新日报冻结已有账户的净仓及估值，不购买额外行情。只有账户身份、币种、
净仓及策略成交归属一致时才显示已核实仓位；混仓或未知不冒领全账户盈亏。
优先读取精确、有限的 `unrealized_pl`，并核对成本与 V3 加权成本吻合；
不因 `pl_val_valid=true` 就采用 `pl_val`。精确字段不可用时，仅在相同数量、币种、乘数、正的实际标记价
和实际成本全部可靠时计算。未确认净仓保留待核，不把失联当归零。核实归零后保存
成交水位，旧回执不复活旧仓；跨日历史快照保留，普通渲染和 status 不读取券商。

## 加密手工来源

加密不连接自动交易。用户在原日报 tab 明确回报完整 JSON，前缀为 `模拟持仓 `。
私有文件中的 JSON 必须与原生会话的实际用户消息规范化正文完全相同；其他 tab、
工具消息、未确认文件或公共投影均无权写入。示例输入：

```json
{"schema":"trend-v3-manual-holdings.v1","strategy":"trend-v3","environment":"SIMULATE","date":"2026-11-26","report_version":"v02","account":"manual-crypto","positions":[{"code":"BTC","market":"CRYPTO","product":"crypto","currency":"USD","quantity":"0.025","average_entry":"64000","unrealized_pnl":null}]}
```

account 是稳定私有别名，不填凭据；仅显式列出的币种更新。用户确认归零写 quantity
为 "0"；省略币种不等于平仓。缺少已确认估值时盈亏为 null。原 tab 在计划和上传
身份已核实后执行 `paper_cli --session UUID manual-holdings PRIVATE_JSON`。入口只读取
原会话、私有状态和已核实报告，不连接 OpenD。JSON 正文可用同版 `paper_entry.encode`
规范化；不代发用户消息，不由样本或普通投影反向灌入私有账本。

## 离线完整报告示例

`examples/holdings-report.json` 全为显式演示数值，四个市场均为已在仓头寸，不是
生产状态或买入指示。正常代码绝不自动加载这个文件。Main 可以用实际同版 renderer：

```sh
python services/trend_v3/.agents/skills/trend-v3-daily-report/render_report.py \
  services/trend_v3/.agents/skills/trend-v3-paper-entry/examples/holdings-report.json \
  /任务证据目录/持仓演示-完整日报.html --report-revision v02
```

输出必须为新的离线文件。页面明确标演示数据，不写正式日报、私有头寸或上传回执；
Main 再依现有交付授权处理完整报告的展示。其余日报内容继续由原 renderer 和模板生成。
