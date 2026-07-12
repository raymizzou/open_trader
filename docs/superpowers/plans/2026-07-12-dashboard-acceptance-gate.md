# Dashboard Acceptance Gate Implementation Plan

1. 先增加 `OTHER` 持仓加载回归，修复 Dashboard 400。
2. 为 payload、进程、日志和状态分类编写失败测试。
3. 实现 `scripts/accept_dashboard.py` 与 `make acceptance`。
4. 写入 `AGENTS.md` 的 PASS/FAIL/BLOCKED 规则。
5. 安装 Playwright dev 依赖，运行全测并重启 Dashboard。
6. 用真实数据等待两个刷新周期并运行统一验收；只有 PASS 才交付。
