# 富途模拟股票账户自动路由设计

## 目标

富途模拟盘的港股、美股和 A 股股票读写都由系统自动选择适合当前市场的模拟股票账户。配置中的账户 ID 只作为偏好；若它指向期权账户、其他市场账户或已停用账户，系统自动回退到唯一合适的账户，不再要求用户人工驱动。

Dashboard 模拟持仓、趋势控制器、趋势报告账户读取、Kelly 模拟执行和验收检查继续复用同一个 `FutuSimulateOrderExecutionClient`，不在各调用点重复选择逻辑。

## 账户选择

候选账户必须同时满足：

- `trd_env == SIMULATE`
- `acc_status` 为 `ACTIVE` 或空
- `trdmarket_auth` 包含当前 `trd_market`
- `sim_acc_type` 支持股票：`STOCK` 或 `STOCK_AND_OPTION`

选择顺序：

1. 配置 ID 命中合格候选时使用该账户。
2. 配置 ID 不合格或不存在、且只有一个合格候选时，自动使用唯一候选。
3. 没有合格候选时安全失败，并说明当前市场没有模拟股票账户。
4. 多个合格候选且配置未消歧时安全失败，并列出候选 ID。

客户端对外仍暴露既有 `account = {"acc_id", "acc_index"}`，调用方接口不变。

## Dashboard

`TrendSimulatePositionService` 不新增接口或缓存。部署后重启 Dashboard，使旧进程内存中的港股 OPTION ID 失效；共享客户端会自动路由到 HK STOCK 账户，`/api/trend-simulate-positions/phillips` 应返回实际港股模拟持仓。

## 测试

自动测试覆盖：

- HK 配置误指 `OPTION` 时自动选择唯一 `STOCK`。
- US 自动接受 `STOCK_AND_OPTION`，拒绝纯 `OPTION`。
- CN 只选择具备 CN 授权的 `STOCK`。
- 无候选和多候选保持安全失败。
- Dashboard 模拟持仓服务经共享客户端取得正确账户持仓。

真实富途模拟盘测试对 CN、HK、US 各提交一笔远离市价的限价股票单，使用唯一 smoke-test 备注；确认实际订单账户与自动选择结果一致后立即撤单，并核对 `dealt_qty == 0`。若任一订单成交，立即停止后续测试并报告实际持仓。

最终按项目规则运行一次 `make acceptance`。仅当结果为 `PASS` 时，部署该验收 Git SHA，重启 Dashboard 和三个趋势控制器，并验证 PID、工作目录、Git SHA、新日志、HTTP 200 与港股模拟持仓页面。

## 非目标

- 不触碰真实账户或实盘下单。
- 不改变趋势策略、仓位计算、报告哈希或防重账本。
- 不新增配置项、依赖、账户缓存或后台任务。
