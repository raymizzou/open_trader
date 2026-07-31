# Open Trader 渐进式前后分离参考架构

状态：已确认的参考架构。本文记录长期方向与迁移约束，不授权实现，也不固定各模块的迁移顺序。

## 背景

当前浏览器已经通过 HTTP 获取动态数据，但 `dashboard` 进程同时承担静态页面、
查询聚合、结单导入、回测、Research Chat、Polymarket Watcher、Codex 审核与
预测市场执行。账户同步与 CN/HK/US 趋势控制器则由独立 `launchd` 进程运行，
通过 `data/`、`reports/` 和 SQLite 与 Dashboard 协作。

这形成了两个问题：

1. 前端页面服务器与重量级后台生命周期耦合，重启 Dashboard 会影响 Watcher
   和 Codex 队列。
2. 后台虽然分成多个进程，操作员却必须理解并管理每个 worker 的启动、版本、
   配置和工作目录。

2026-07-31 的运行快照还显示，`/api/dashboard` 单次响应约 36 MB、耗时约
10.8 秒，而行情刷新默认每 5 秒再次加载完整 Dashboard。前后分离必须同时缩小
前端 interface，不能只把现有大接口搬到另一个端口。

## 已确认目标

- 前端轻量，只负责静态页面、交互、格式化和状态展示。
- 前端所有动态数据和命令都通过后台 HTTP interface。
- Account、Trend、Research、Prediction 模块可以运行不同 Git SHA、独立环境
  和独立版本。
- 模块可以独立升级和回退，不要求其他模块或前端重启。
- 迁移期间 `127.0.0.1:8766` 与现有交易自动化持续可用。
- 每个迁移步骤可以独立回到旧实现。
- 操作员只管理一个声明式 Open Trader stack，不逐个启动后台 worker。
- 本地与未来云端部署使用同一模块拓扑。

## 选择的方式

采用稳定 Frontend Gateway 加绞杀式迁移。

Gateway 始终占用 `127.0.0.1:8766`，只提供静态资源和同源路由，不读取业务
文件、不连接外部依赖、不执行领域计算。旧 Dashboard 后台先作为 Legacy
Dashboard Module 放到 Gateway 后面；之后按领域逐条迁移 HTTP 路由。未迁移
路由继续使用旧模块，已迁移路由可以独立切换和回退。

不采用浏览器直连多个后台端口，因为那会把服务发现、跨域、认证和版本兼容转移
给前端。不采用一次性重写统一后台，因为它不满足持续可用、故障隔离和逐步回退。

## 目标架构

```text
                    Open Trader Stack
        一份声明式 manifest / 一个操作入口
   frontend@A account@B trend@C research@D prediction@E
                              |
                              v
Browser -> Frontend Gateway :8766
           静态资源 + 同源路由；无业务实现
                              |
       +----------------------+----------------------+
       |                      |                      |
       v                      v                      v
 /api/v1/account        /api/v1/trend        /api/v1/research
 Account Module          Trend Module          Research Module
 interface + workers     interface + workers   interface + workers
       |                      |                      |
       +----------------------+----------------------+------+
                              |                             |
                              v                             v
                    /api/v1/prediction             未迁移路由
                     Prediction Module        Legacy Dashboard Module
                     interface + workers          逐步缩小并删除
                              |
                              v
      原子文件快照 / 不可变执行账本 / Prediction SQLite
                              |
                              v
 OpenD / Tiger / 趋势动物 / TradingAgents / DeepSeek / Codex /
 Polymarket / 飞书
```

## 模块与 interface

### Open Trader Stack

Stack 是部署 interface，不是业务模块。它声明每个模块的版本、运行环境、端口、
持久卷、健康检查和启动依赖。操作员通过一个 stack 操作完成启动、状态检查、升级
和回退。Stack 可以由本地 launchd 安装器或未来的云端编排 adapter 实现；本文
不要求自建进程监督器。

### Frontend Gateway

职责：

- 提供静态 HTML/CSS/JavaScript。
- 保持 `127.0.0.1:8766` 稳定。
- 按版本化路径把请求路由到模块。
- 在目标模块不可用时返回明确的模块级错误。

禁止：

- 读取 `data/`、`reports/` 或 SQLite。
- 连接 OpenD、趋势动物、Codex 或其他外部依赖。
- 聚合金额、权重、策略、报告或执行结果。
- 启动任何领域 worker。

### Account Module

拥有账户、现金、真实持仓、行情、结单候选和 Dashboard 账户投影。

其实现包含现有账户同步循环：账户约 60 秒刷新、行情约 5 秒刷新。浏览器只读取
模块发布的完整投影；外部读取和持久化发布仍保持单一 writer。

### Trend Module

拥有 CN/HK/US 趋势报告、控制器状态、模拟持仓、保护监控、执行账本和人工
resolution。

现有三个市场控制循环迁入该模块的实现，但仍可保持独立进程和现有执行围栏。
它们不再是操作员单独管理的产品表面，由 Trend Module 的部署 adapter 启动、
检查和回收。

### Research Module

拥有盘前研究、TradingAgents/DeepSeek 调用、决策事实、标准回测和 Research
Chat。定时盘前任务与模型任务是其内部 worker。

### Prediction Module

拥有 Polymarket 关系发现、WebSocket 实时池、Codex 审核队列、机会状态、执行、
熔断、SQLite 历史和飞书通知。Frontend Gateway 或 Legacy Dashboard 的重启不能
重启这些 worker。

### Legacy Dashboard Module

迁移期 adapter，承接尚未迁移的现有 HTTP 路由。它不得获得新领域职责。每个
领域迁移后，相关路由和实现从 Legacy Module 删除；全部迁移完成后删除该模块。

## 持久化 seam

首轮迁移继续复用现有文件和 SQLite，不引入消息队列、Redis 或新的中心数据库。

- Account、Trend、Research 使用版本化 schema 和原子文件替换。
- 趋势执行继续使用不可变账本和 broker reconciliation。
- Prediction 继续使用其 SQLite，并保持单一生产 writer。
- HTTP interface 与持久化 schema 分别版本化；模块不得通过跨模块 Python import
  共享实现细节。
- 一个领域只有一个发布者。Gateway、Legacy Module 和其他模块不得绕过拥有者
  修改该领域数据。

## 数据流

### 查询

1. 浏览器请求 `/api/v1/<module>/...`。
2. Gateway 只选择目标模块并转发。
3. 模块从自己拥有的已发布状态构造轻量响应。
4. 列表只返回列表所需字段；大报告、decision plan、历史和逐标的详情按需获取。

行情轮询只更新行情相关响应，不再触发完整 Dashboard 重载。

### 命令

结单导入、Research Chat、回测和预测市场操作进入拥有该行为的模块。模块负责输入
校验、权限、幂等、持久化和错误语义；Gateway 不实现这些规则。

### 后台工作

worker 随所属模块部署，由模块的运行 adapter 管理。worker 可以是内部线程、
子进程或独立容器，但这些是模块实现细节。模块 interface 必须统一报告 worker
健康、Git SHA、启动时间、依赖状态和最新成功时间。

## 部署与升级

每个模块拥有独立 release 目录或镜像、Git SHA、运行环境、配置和健康检查。

查询模块升级时：

1. 并行启动新版本。
2. 检查 interface、真实数据和运行元数据。
3. 原子切换 Gateway 路由。
4. 保留旧版本作为即时回退目标，确认稳定后再停止。

单一 writer 模块升级时不得并行写入。必须先阻止旧 writer 产生新动作，完成锁、
账本和外部事实 reconciliation，再把写入所有权交给新版本。Trend 和 Prediction
继续沿用其 fail-closed 与单一 writer 规则。

云端部署不要求操作员逐个启动模块或 worker。一个声明式 stack 可以内部启动多个
容器或进程；模块数量不等于操作步骤数量。

## 渐进迁移规则

1. 先建立 Gateway，并把所有现有路由转发给 Legacy Dashboard Module；行为保持
   不变。
2. 每个领域迁移作为独立 spec、plan、branch 和验收周期。
3. 新旧实现并存期间使用真实数据做投影对比，但只有一个生产 writer。
4. 只有该领域的 focused tests、真实工作流、进程检查和需要的 Dashboard 验收通过
   后才切换路由。
5. 切换失败只回退该领域路由或 writer 所有权，不回退整个 stack。
6. Legacy Module 不接受新能力，最终自然缩小到零。

本文不固定 Account、Trend、Research、Prediction 的迁移先后。迁移顺序必须在
各模块 spec 中根据风险、收益和当前运行状态单独选择，避免把整个架构改造变成一
次不可回退的大计划。

## 故障语义

- 一个模块不可用时，Gateway 和其他模块继续服务。
- 前端明确显示受影响模块为 failed、stale、blocked 或 unavailable，不用旧数据
  伪装正常。
- 查询失败不得触发同步、模型调用或交易动作。
- worker 失败由所属模块报告，不能导致 Frontend Gateway 重启。
- 外部依赖失败只降级其拥有模块；交易和资金相关操作继续 fail closed。
- Gateway 不能把不同版本模块的部分响应拼成一个看似完整的领域事实。

## 验证要求

后续每个模块迁移至少证明：

- 版本化 HTTP interface 与持久化 schema 的兼容测试通过。
- 新旧实现对同一真实发布物产生等价的必要展示事实。
- 列表与轮询响应不携带未请求的大型详情。
- 独立升级不会重启 Frontend Gateway 或其他模块。
- 模块级回退不会改变其他模块的版本或状态。
- writer 交接不存在双写、重复动作或不明执行窗口。
- live PID、工作目录或镜像、Git SHA、fresh logs 和健康状态属于已验证版本。
- 可见 UI 变化仍满足项目的 Dashboard acceptance 与截图要求。

## 非目标

- 首轮引入消息队列、服务网格、Kubernetes 或新的中心数据库。
- 把所有模块合并成一个新的巨型后台进程。
- 让浏览器直接发现或调用各后台端口。
- 在本参考架构中确定所有 endpoint 字段和模块迁移顺序。
- 改变现有交易策略、执行规则、报告语义或风险限制。

## 长期完成标准

- 浏览器只依赖 Frontend Gateway 和版本化 HTTP interface。
- Frontend Gateway 没有业务或 worker 实现。
- 四个后台模块能运行不同版本并独立升级、回退。
- worker 全部由所属模块管理，操作员只操作一个 stack。
- Legacy Dashboard Module 被删除。
- 前端列表和行情轮询保持轻量，不再传输完整决策与报告集合。
- 本地和云端使用相同模块所有权与 interface，只更换部署 adapter。
