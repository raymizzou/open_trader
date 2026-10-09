# #306 本机批次发布验证

2026-10-09，当前基础 HEAD 为 `c692d9a52c9f904139cf3ba1fc0f69fdd8506e96`（初始量化 baseline 为 `664d461a4f2a93db3bf4dfc31bf5dcff341be6d3`），分支为
`codex/issue306-competition-batches`。实现尚未提交，本文记录本机工作树检查，初次 Standards 无阻断、Spec P1 已做本轮修复，原 reviewer 的新 staged review 待协调者安排；不代表 CI、云端容量、部署或健康验收。

## 行为与锁边界

原生 `lp_market_competitiveness` 按现有 500 行 API 页读取，每批最多 500 行提取后调用提交入口，不先形成整轮事实 map。每条保留 `condition_id`、Decimal 和该页实际读取时间；数值 JSON 也按 Decimal 解析。竞争行及页恢复游标同 SQLite 事务提交，页内中断保留当前页游标，完整页提交才推进下一页。空页可以提交进度；错误或重复游标不能跳过未提交页。

编码、网络、写事务 BEGIN 等待及 body 均不占竞争发布锁。仅 COMMIT 和 cache 更新共用短锁，成功提交后才发布；提交前取消回滚，提交后停止仍完成 cache 发布。原生投影在短锁内同时捕获所需 cache 项、版本和 SQLite 读快照，锁外查询 fallback。仅支持 `condition_ids`、仅接受泛化 `**kwargs` 或未成功固定快照的兼容 map reader 在同一锁内完成 fallback；其读取可能延长发布等待，但不将写入准备或 body 移入锁内。只有显式、可用作关键字的 `connection` 参数及成功固定的非空快照可走锁外查询；泛化 `**kwargs` 不能证明该能力。显式接受 connection 的 reader 必须使用传入快照，不得静默重新开连接。

内部 cache 保持单所有者，不逐批复制整表。每轮只用一次上一轮 key 集合跟踪缺席项；完整非续传轮结束后移除活动 cache 的缺席 key，SQLite 旧事实不删除，source 转为 store、updated 为未知。中断或续传轮保留未更新 cache 事实。显式 snapshot 和 scoped 投影与内部状态隔离。

一次队列构建共用一个投影，竞争版本加入复用和发布判定。新批提交不主动清空有效队列，也不逐批构建全池；现有调度下一次调用合并重排。已开始的构建不追赶刷新；构建序号和竞争版本共同防止旧结果覆盖已发布的新结果。其余账户、资金、盘口、收益和终态门禁沿用既有规则。

## 聚焦检查

Python：`/Users/ray/.local/share/open-trader/release-envs/d9a6167e2f0faa416974c01a1fa481fc71652364/bin/python`，`PYTHONPATH=src`，macOS，未安装依赖。

首轮最终选择 125 个直接受影响或新增 nodeid，参数展开后 **159 passed**，耗时 21.90 秒。机械 rebase 及 P1 修复后选择 126 个 nodeid，**163 passed**，耗时 21.91 秒。一条既有 websockets 弃用警告。节点清单与完整命令、输出位于：

- `/Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/focused-nodeids-final.txt`
- 同目录首轮 `focused-command.json`、`focused-final.log`；修复后 `focused-nodeids-repair.txt`、`repair-focused-command.json`、`repair-focused-final.log`。

覆盖提交前不可见、physical COMMIT 返回但 cache 未发布窗口、cache fresh A/store-only B 的一致性、后批失败保留前批、整批 SQL 失败与游标回滚、stop/重启/幂等、同值新时间、空页/错误/重复游标、旧库新增恢复表、完整非续传轮缺席 cache 清理、兼容 reader/显式 snapshot、UNKNOWN/显式零/Decimal、全部候选及排序、版本合并和逆序构建防覆盖。同步使用 Event/barrier 与独立真实时间 watchdog，未用 sleep 安排正确性时序。共享 `_transaction` 的既有 commit、rollback、日志失败及计数诊断消费者通过。

全部尝试记录在同目录 `attempts.md`：初始 5 项 RED 均失败；COMMIT 窗口首次断言不足而通过（不作为 RED），强化后在旧代码失败；首版 6 项 GREEN。第一轮消费者 36 passed/3 failed，失败为批准替换的整轮 map 转移、整轮 cache 替换和落库失败仍发布断言，修正契约且保留负断言。之后 43、23、80、2、2 项聚焦轮次通过。首个 benchmark 因 harness 传入 generator，view 未产生 queue 字段而 KeyError；修正为支持的 list 后成功，不使用失败结果作为收益证据。

## 公开输入及复现

白名单源为 `/Users/ray/.local/share/open-trader/issue290-followup-diagnosis/public-cache.jsonl.gz`，先读取并核对 `public-cache-manifest.json`：

- 源 gzip SHA-256：`800e2fdde04a6a6d0c98de4e0bc84a4547524224f5513845df070d8f2a3c7d64`。
- 初始竞争事实 130,713 行；更新输入为其前 128,500 行，沿用公开竞争值并赋予固定的回放读取时间。
- 更新输入规范 JSON SHA-256：`591f8c4945e1bb6d697861c98cc6c70cd4b293f45ab638911c327392d80061a9`。
- 两版相同初始 DB SHA-256：`d0be7c115a8e3ea743cac5eaa5a60cc8b098446b342586a784069037ed147e6e`。
- 候选 identity 为公开 preparation 的全部 21,909 项；入选/history/budget 字段是受控 fixture，无账户来源。并发历史准备写入 16 批 × 100 行 × 60 个受控价格样本。

**这是同规模公开缓存派生回放，不是当次云端原样 128,500 行，也不是完整生产数据库。** 本机云端诊断目录只有脚本/结果，缺少该原始输入；没有执行 SSH 或生产操作补数据。

可复现 runner：`tests/lp_competition_batch_replay.py`。结果、输入、初始 DB 与 baseline 源码均在 `/Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/`。回放 baseline 使用历史基线 `664d461a4f2a93db3bf4dfc31bf5dcff341be6d3` 的三个生产模块，其余 source 来自回放当时的工作树；这不是当前基线 `c692d9a52c9f904139cf3ba1fc0f69fdd8506e96` 或当前暂存树的 benchmark，当前树未重新运行 benchmark。Python、SQLite 版本、完整输入/候选文件 hash 均写在 JSON。历史成对回放顺序执行，没有并行测试。命令模板如下（将 `<mode>` 分别替换为 `baseline-final`、`new-final`；baseline 的 PYTHONPATH 使用证据目录的 `baseline-src:tests`，新版用 `src:tests`）：

```sh
PYTHONPATH=src:tests /Users/ray/.local/share/open-trader/release-envs/d9a6167e2f0faa416974c01a1fa481fc71652364/bin/python \
  tests/lp_competition_batch_replay.py \
  --input /Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/competition-input.jsonl \
  --candidates /Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/candidate-identities.json \
  --initial-db /Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/initial/prediction_arbitrage/prediction_arbitrage.sqlite3 \
  --output /Users/ray/.local/share/open-trader/issue306-batch-replay-20261009/<mode>.json \
  --mode <mode> --batch 500 --pause 0.01
```

## 参数测量与结果

同输入六种批量/间隔全部成功；结果均保留，未舍弃失败后重跑为绿。选择 **500 行、10 ms 可中断等待**：比 100/250 行减少事务和连接数量；比不等待或 1 ms 等待降低本次历史 BEGIN 最大等待，接受后台整轮变慢。不是全平台最优值，未改 busy_timeout、内存 guard 或 SQL。

| 批量 / 间隔 | 刷新 s | 最大事务观测跨度 ms | 历史 BEGIN 最大等待 ms |
|---|---:|---:|---:|
| baseline 整轮 | 0.999 | 792.57 | 821.32 |
| 100 / 1 ms | 7.636 | 40.65 | 25.78 |
| 250 / 1 ms | 5.462 | 52.33 | 27.09 |
| 500 / 0 ms | 4.085 | 51.96 | 114.70 |
| 500 / 1 ms | 4.618 | 61.72 | 27.94 |
| 250 / 10 ms | 12.426 | 60.71 | 4.61 |
| 500 / 10 ms | 7.869 | 43.82 | 12.19 |

最终成对结果（`baseline-final.json`、`new-final.json`）：

| 指标 | baseline | 新版 |
|---|---:|---:|
| 竞争事务数 | 1 | 257 |
| 整轮刷新 s | 1.0119 | 7.9300 |
| 最大事务观测跨度 ms | 807.00 | 42.99 |
| 最大 body/guard 等待观测 ms | 767.81 | 39.90 |
| 最大 COMMIT ms | 39.04 | 11.09 |
| 最大 connection close ms | 0.14 | 9.44 |
| 最大准备/连接设置 ms | 0.18 | 13.70 |
| 历史 BEGIN 最大等待 ms | 821.43 | 12.14 |
| 历史单批最大完成 ms | 854.86 | 55.52 |
| 刷新阶段进程 RSS high-water MiB | 227.09 | 227.09 |
| 含候选构建的进程 RSS high-water MiB | 365.67 | 366.28 |
| 采样 WAL 峰值 B | 25,552,272 | 2,224,832 |
| 历史写入错误/超时 | 0 | 0 |

最大事务观测跨度缩短约 94.67%。该跨度从 BEGIN 返回至 close 完成，**不等于准确物理写锁时长**；各列最大值可以来自不同事务，不能相加。准备值包含连接设置；instrumented body 列包括提交 guard 的获取等待。WAL 为 10 ms 采样峰值，不代表文件缓存归属或完整容量。RSS 是进程 high-water，包含输入/初态准备；**没有观察到下降**。Mac 无 Linux cgroup，anon/file/total 和 dirty pages 未测。该机 baseline 也没有重现云端 5 秒 SQLITE_BUSY，结论限于本次并发实验的显著等待缩短、无历史写入超时；不能宣称唯一原因或云端根治。

完成后的全部事实、全部 scoped 投影与全部候选输出（含完整 normal/backup 队列、理由、来源、更新时间和排序）两版一致：

- 130,713 事实 SHA-256：`d3a1820bb51b1ed7c46f35b6e8108a9c17900b9bd7afc93136d6ddb061ca0d7b`。
- 21,909 项完整投影 SHA-256：`0672293c7e400430c54a3fe01bf916652141373df08ebec7d88463fff5c1fbd6`。
- 完整候选输出/排序 SHA-256：`13f0b5d67a6b36da3deecedc99703c81c44b993fb319f50b3422cca84dcbbe31`，normal 10,407、backup 0；其余候选的排除理由也在 hash 中。

成对回放后补充队列发布逆序保护及本轮 P1 兼容 reader 能力判定；新增投影守卫要求显式 connection 和已固定快照。量化所用原生 store 已满足该条件，其执行的锁外查询分支、批量/等待、竞争读取、持久化、发布及候选 view 不变；没有为新基础 SHA 扩展 benchmark。回放不是完整 service/runtime 验收。最终源码/测试冻结后的 staged tree identity 与文件 hash 由协调交接记录固定；不把基础 HEAD 单独当作未提交实现的身份。

以上是首版 worker 阶段记录：该阶段未执行完整后端/服务 suite、CI、独立 review、提交/推送、GitHub 写入、merge、release、部署、SSH/cloud、service/order 操作。其后 Main 的发布、CI 及本轮修复见下节；新的 staged tree 仍需独立复审。


## 独立 Spec P1 修复与 rebase

初次 tree `8806f3a2607c1348464d898d8df9f7a3987f4ef1` 的 Spec review 发现：`condition_ids=None, **kwargs` wrapper 可忽略 connection，签名 bind 成功却不是共享快照能力证明。新增 Event/barrier RED 确定性读到 A旧/B新；无 snapshot 入口及返回空 connection 同样复现，snapshot 创建失败则暴露旧 fallback 未执行。先取得 3 failed（11.34 s）与空快照 1 failed（2.25 s），日志为 `repair-red.log`、`repair-red-empty.log`。修复后的四种场景 4 passed（1.30 s），日志 `repair-green.log`。原生并发 fixture 改为显式声明并转发 connection，提交不阻塞和稳定旧投影负断言保持。相关两 worker 交错 7 passed（1.85 s），两条既有弃用警告；结果记录在 `repair-concurrent.log` 与 `repair-concurrent-command.json`。

`git rebase --autostash origin/main` 仅 CHANGELOG 冲突，保留 upstream #305 的 GLM 检查记录和本票记录；其余十个任务文件与保存副本逐字一致。未创建任务 commit，冲突 autostash 与十一文件/双补丁备份保留在证据目录。新 main 仅有 CHANGELOG/runbook 文档变化，已读新 runbook；GLM profile/provider 实际前置由协调者处理，未访问凭据或机器 config。P3 命名建议未扩展实施范围。


## PR #307 CI 与 fresh cache P2 修复

Main 将已审查实现提交为 `5dd64fd75abcb9ee6f60b7d8fc44d839f182a3f9` 并发布 Draft PR #307。原两条 CI 终态均失败：push run `37896769516` 的 required check `113714101664`、PR run `37896854772` 的 required check `113714306614` 均来自 GitHub Actions app **15368**。API/check head 均为分支 head；各七个实际 checkout 日志及各五个 artifacts 确认 push 被测 SHA 为该 head、PR 为 synthetic merge `a830f17117ccfb96066a3a77e80008f16af0b9f0`，base 为 `c692d9a52c9f904139cf3ba1fc0f69fdd8506e96`。完整 CI jobs、required/app 和失败日志在原证据目录的 `ci/summary.json`、`ci/handoff.md`。Main 持有 CI、反馈和交付责任；GLM blocked，不能称 review-ready。

本轮将三个有据 CI 失败与有效 P2 反馈一起修复。P2 线程 [discussion_r4227595819](https://github.com/raymizzou/open_trader/pull/307#discussion_r4227595819) 由 Main 记录为 unresolved/blocking；worker 不回复或 resolve。当前 parent HEAD 为上述 `5dd64fd…`，本轮未提交。

- 原三条失败 nodeid 本机聚焦 RED：3 failed，6.08 s。过期 fixture 仍挂单项 entry reader，实际生产边界已是 scoped map；调整为显式转发 condition_ids/connection 的 map wrapper，仍在 metadata 已取得后的同一业务阶段暂停。metadata/competition_lookup 两分支、61 s 业务过期、无 cooldown/无候选负断言及原 5 s watchdog 全保留。
- schema 只在精确 table-set 中增加批准的新表 `lp_competition_progress`；WAL、foreign_keys、busy_timeout、版本、索引和查询计划断言不变。
- 相同 fixture 对 `c692d9a5` 与 head 捕获完整结构化 trace，递归逐字段比较。只有三个 state 各增加 `competition_version` 和 `build_sequence`，共六处；trial 数量四个、state 数量三个一致。所有其余值、source、时间、理由和完整排序逐字段一致。去除精确的两个内部字段后恢复原完整业务 golden `ffbde7cce77aa091183c50a0d355b9f5a59d7f8111053cb2315dbfc51bde2bdb`，没有替换该期望 hash。测试单独断言竞争版本 `[1,1,1]`、构建序号 `[1,3,4]`，其余字段仍全量进入旧 golden。

P2 先新增“全部请求 identity 命中新鲜 cache 不打开 SQLite”确定性 RED（1 failed、2 passed，1.44 s），再恢复最小无 DB 快路径。一次读取固定一个 clock 值，cache 判定及最终投影使用同一时间；只有 scoped 请求全部命中新鲜事实或空请求可以直接返回。混合、缺失、超龄、未来时间继续按既有 fallback 规则；需要 fallback 时仍 pin SQLite 与捕获 cache/metadata 版本，不能使用 connection 的 legacy wrapper 仍在发布锁内读取。`condition_ids=None` 明确保留全库读取，不遗漏未缓存的持久事实。返回字段独立，显式零和 Decimal 不变。cache 原子捕获的并发 fixture 改为 Event/barrier，在实际捕获阶段安排发布；保持稳定旧投影断言，clock 不用于偷偷改变发布状态。

本机隔离、固定 20,000 个不同公开 fixture identity 的相同新鲜 Decimal cache，通过真实 `_candidate_market_rejection` 测量；不含账户、网络、凭据或生产数据。与外部 review 测量独立：

| 同输入 P2 probe | 修复前 head | 本轮实现 |
|---|---:|---:|
| SQLite 连接数 | 20,000 | 0 |
| 耗时 s | 10.500209 | 0.102335 |
| 全部返回 | 20,000 | 20,000 |

输入 hash `cae87d152640889caedd46319542a7a5652c8dd0993a5242500e064cd57da0d7`；全部返回 hash `09135bd3f277a04ad5cd677560fd9258d5573377cc6056829fd688d1544e54a8`，完整返回文件逐字节相同。两版 interpreter 同为已选 Python 3.12.12，同 DB 初态；probe 执行时没有并行测试。源码文件 hash 和精确 runner 为 `ci-repair/before-cache-probe.json`、`after-cache-probe.json`、`cache-probe.py`；这不是新的云端容量结论，也不是重跑原 128,500 行 benchmark。

过期错误的负向证明采用独立 subprocess 内存 mutation：将 `_candidate_source_expired` 临时置为总 False。metadata/competition_lookup 两分支都在“无 cooldown”断言失败，实际错误为 `event_starting_soon:1`（2 failed，0.19 s），不是 watchdog 超时；源码未写入 mutation。正常首轮 GREEN 为 8 passed（1.99 s），最终直接消费者 55 个选择 nodeid 展开 **83 passed**（9.10 s），相关两 worker Event/barrier/过期场景 **13 passed**（1.72 s）。保留既有弃用警告。完整 RED/GREEN、负向日志、节点及命令在证据目录 `ci-repair/attempts.md`、`red-original-three.log`、`p2-red.log`、`negative-expired-receipt.log`、`focused-serial.log`、`focused-concurrent.log`；trace 在 `baseline-trace.json`、`head-trace.json`、`trace-field-diff.json`、`trace-comparison.json`。

原性能回放约 8 s vs 1 s、RSS 无下降、派生非云端原样及历史测量身份限制保持；没有无关 benchmark 扩展。未重跑旧 CI 或完整 suite，未提高 timeout、skip、弱化断言，未处理 P3 polish，未改机器/GLM config 或接触凭据。worker 不 commit/push/GitHub 回复/resolve/merge/deploy，stage 后冻结交原 Standards/Spec reviewer；Main 负责统一复审、发布及新 head CI。
