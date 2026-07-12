# Dashboard 统一验收门设计

## 目标

任何 Dashboard 行为改动只有在自动测试、真实数据、真实浏览器、刷新稳定性和运行版本五道门全部通过后，才能标记为完成或部署成功。

## 状态

- `PASS`：五道门全部通过，退出码 0。
- `FAIL`：页面、数据、进程、日志或测试结果错误，退出码 1。
- `BLOCKED`：浏览器或必要外部环境不可用，退出码 2。

非 `PASS` 禁止使用“完成”“部署成功”或“验收通过”。

## 最小实现

- 新增 `scripts/accept_dashboard.py`，检查两次真实 `/api/dashboard`、预期 A 股数量、全资产权重 100%、运行 PID/cwd/Git SHA 和错误日志。
- 使用 Playwright 的系统 Chrome 分别验证桌面与移动宽度：页面无“看板数据加载失败”，可点击 `A 股` 和 `东方财富`，并显示 5 条持仓。
- 两次 API/页面检查之间等待配置的刷新稳定窗口；正式验收覆盖至少两个后台刷新周期。
- Playwright 或 Chrome 不可用时返回 `BLOCKED`，不得以 curl、单元测试或 Mock 替代浏览器门。
- 将规则写入 `AGENTS.md`，并提供 `make acceptance` 唯一入口。

## 当前故障回归

真实 portfolio 中存在 `market=OTHER` 行。市场范围型投研产物只支持 `HK/US/CN`；Dashboard 必须继续展示 OTHER 持仓，但不得把 OTHER 传入市场范围解析器。
