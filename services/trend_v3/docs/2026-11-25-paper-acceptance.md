# 2026-11-25 Trend V3 模拟盘验收诊断

本次确认两处独立的契约不匹配。港股订单已提交，临时验收脚本在撤单前误排除回执。Mini 定时日报 11/25 v01 的选股证据在离线演练中未通过日期字段校验。验收时 #533 仍开放且未部署，候选版本为 `1939250a11a91d0f9b9335e7686b393bdd379df0`。本文记录诊断及待验证项。[需求 #507](https://github.com/raymizzou/open_trader/issues/507) · [候选 PR #533](https://github.com/raymizzou/open_trader/pull/533)

## 港股模拟限价单

- 09:39（UTC+08:00），独立验收脚本在 `SIMULATE` 提交一笔港股限价单，券商接受。该单回执的委托类型为 `ABSOLUTE_LIMIT`。撤单前的匹配要求类型字面值为 `NORMAL`，即使已确认订单号、市场、标记、方向、数量和限价一致，也会排除该回执。原运行及恢复步骤报告 `PENDING_OR_REJECTED`，进程退出码为 0，且未发出撤单请求。
- 09:45:13，主执行代理（Main）按确切的模拟账户和原订单号查询，核对其余委托字段，确认状态为 `SUBMITTED`、零成交。Main 随后发出一次获准的撤单请求。09:45:14，最终读回 `CANCELLED_ALL`、零成交。最终持仓为零，诊断期间新增委托为零。
- 结论限于这笔限价单。回执类型的映射发生在哪一层尚未确认。该脚本在 #533 之外运行。#533 的市价单路径提交 `MARKET`，按 `MARKET` 校验回执。本次限价探针未覆盖该路径。[市价单提交](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_futu.py#L474-L497) · [市价单回执校验](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_futu.py#L539-L556)
- 其余三项获准的美股现货、A 股现货及期货验收均在提交前暂停，无券商拒单证据。加密资产在应用内有模拟入口。OpenD 查询成功，返回零个模拟账户。网页版需登录。另一次 SDK 启动因无法访问默认日志文件，在访问券商前已停止。

独立探针脚本 SHA-256 为 `54c641cde56623c9efc6e8465bc8807629b87e0ad156e61fb9f8a260eb39b0ea`，Futu SDK 版本为 `10.09.6908`。

## 日报的离线演练

| 冻结日报 | 原始生成版本 | 实测结果 |
| --- | --- | --- |
| 11/25 v01 | [`fe2dac23`](https://github.com/raymizzou/open_trader/commit/fe2dac23c0283064145813a9a9ebf2ecf280af5e)，Mini 定时日报 SOP 固定版本 | 使用正确的原始版本参数，退出码 2，报 `daily report artifact date does not match` |
| 11/24 v02 | [`74ac58e5`](https://github.com/raymizzou/open_trader/commit/74ac58e5fde25253adec1324d11d257eb08cf308)，另一套生成工具和发布版本 | 冻结离线演练退出码 0，零候选、零委托 |

11/25 的输入 SHA-256 与日报核验元数据一致。读者投影和选股证据中的 `date` 都是 `2026-11-25`，但选股证据缺少读取器要求的 `report_date`。演练在加载证据时停止，尚未进入交易事实生成。11/24 v02 的选股证据含 `report_date`，该次演练验证了它可被读取并完成空计划。[加载与校验](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_authority.py#L398-L437) · [离线入口](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_cli.py#L89-L107)

源码还显示后续有两处证据缺口。11/25 v01 的读者投影缺少 `collection.targets` 和 `collection.evidence`，两者用于验证榜单日期、版本及来源。选股证据提供 `field_sources`，而信号核验和原始证据读取依赖 `field_records`。这些是静态核对发现的后续缺口，本次演练在日期校验处已停止。日报成功发布与选股证据能否支持模拟盘规划是两个独立的验收结果。[事实生成](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_facts.py#L478-L497) · [榜单与信号核验](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_evidence.py#L151-L205) · [原始证据读取](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/src/trend_v3/paper_facts.py#L66-L99)

## 修复顺序与验证

1. 先用合成回执重现独立探针对 `ABSOLUTE_LIMIT` 的误排除。验证只有唯一一条同一模拟账户、已确认订单号、市场、标记、方向、数量及限价均一致的有效回执可以触发一次撤单。验证身份或委托字段不符、回执冲突及状态未知时均不撤单、不重发。保留 #533 的市价单和丢失回执回归覆盖。[市价单测试](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/tests/test_paper_entry.py#L1850) · [丢失回执测试](https://github.com/raymizzou/open_trader/blob/1939250a11a91d0f9b9335e7686b393bdd379df0/services/trend_v3/tests/test_paper_entry.py#L896)
2. 再以 11/25 v01 的结构构造脱敏演练样例，并保留 11/24 v02 的空计划对照。先验证正确的日期、版本和摘要，再验证榜单目标及逐字段原始证据。缺证据、证据冲突或身份不符时应停止对应候选，且无券商 I/O、无委托写入。原报告证据保持只读，单独补日期别名不足以通过验收。
