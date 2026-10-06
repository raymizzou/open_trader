# Issue #263 PR A: metadata 生命周期验证（2026-10-06）

本次只提前投影 market 必需字段，并释放消费后的 Future、metadata 批次局部引用。
不修改 exclusion 资格、扫描锁、调度或候选队列架构。下文保留初始交付与历次审查证据；
各节结果只适用于所列身份，不代表已部署或生产容量通过。

规格：[已批准范围](https://github.com/raymizzou/open_trader/issues/263#issuecomment-6015095114)。
门禁沿用 [agent-verification.md](agent-verification.md)。

## 身份与环境

- base/HEAD：`267bb8844fe98d61bc5be66bfc006e961a91f594`。
- worktree：`/Users/ray/.codex/worktrees/issue263-metadata-lifetime/open_trader`；
  branch：`codex/issue263-metadata-lifetime`；开始时工作树干净。
- 未创建 commit；测试针对该 base 上的工作树源码，最终 index tree 身份由 staged 交付记录给出。
- 生产文件 SHA-256：
  - `polymarket_trading.py`：`d46fd051f9ff988f03f0666f020920650f98889464d03b2bd4de9fd8d355fd98`。
  - `polymarket_lp.py`：`5db8eda1ec183bd4fb1908f230ff7bdf2ce6017dcda4c8575542907438ed691f`。
- macOS 27.0 arm64，Python 3.12.12，SQLite 3.51.2；指定现有环境
  `/Users/ray/projects/open_trader/.venv/bin/python`，`PYTHONPATH=src:tests`。
  SDK `polymarket-client==0.2.0`、Pydantic 2.13.4、HTTPX 0.28.1、pytest 9.1.0。
- 新回归与 probe 禁止 socket connect，使用真实 SDK + MockTransport 或现有离线 exchange/临时 SQLite。
  没有账号数据、生产服务访问、依赖安装或外部 gate。

## 最小修复及调用链

`lp_market_metadata` / `lp_market_metadata_fresh` / `lp_market_metadata_batch`
均经 `_lp_market_metadata_batch_result` 调用 `_fetch_lp_market_metadata`；
后者也递归用于 closed fallback。`_collect_lp_market_pages` 只有该 market worker 调用。
该共享读取点参照 `_lp_event_facts`，在分页消费时投影 SDK 模型，排除未用的规则、
指标、标签、resolution 和 clob reward 明细；保留原解析使用的字段及 mapping 兼容别名。
原结果生成代码不变，仍返回完整 27 字段。

market 与 numeric-event Future 仍按原批次顺序提交、消费；用 deque 移除已消费引用。
仍一次提交原请求集合，最多 8 个执行线程，未引入滚动调度或降低并发。
上层 `refresh_price_history` 初始 metadata 批次在 scratch 序列化写入后立即删除原响应引用；
未知响应的 continue 分支也释放响应。metadata retry 已在独立函数中，返回后其批次局部变量自然释放。
全局 generation、SQLite 错误处理、UNKNOWN、closed 查询和现有停止语义保持原路径。

## RED、GREEN 与失败记录

本节初次交付记录对应审查 tree `90f9e27d3cc690d032a69c5036232f590fe359ca`；
该 tree 保留，生产文件 `polymarket_trading.py` SHA-256 为
`86acb62b4e2720bad33a5f8e931ac5966bc19dc48fcbba74fbd1443cb8664b02`。
下面的 92 项结果与初次内存表不冒充修复后新树证据；追加验证见审查修复节。

每个生命周期切片都先写回归，再改对应生产文件。失败未被跳过或放宽。

| 检查 | 修改前证据 | 修改后证据 |
| --- | --- | --- |
| SDK market → numeric/direct event | 1/101 行的两个初始回归失败；进入 event 仍有 1/101 个完整 Market；此前 27 字段断言已通过 | 四个 open/closed × 1/101 行回归均通过；event 入口完整 Market 为 0 |
| metadata → 下一批/history | 1/1501 行初始两个回归失败；下一批入口有 1502 个原始对象，history 入口有最后一批的 3 个 | 成功及最后一批未知共三项通过；下一批/history 入口原始响应、market map、row 均为 0 |
| 未知最后一批 | 精确 base 重放失败；history 仍持有上一批 1500 行及其 map，共 1501 个对象 | 已知 1500 个 token 仍全部读取，最后一个为 UNKNOWN，原始对象为 0 |
| 分页失败 | 精确 base 重放失败；正常 sibling batch 的 1 个 SDK Market 跨入 event | 失败批次保持 UNKNOWN，正常 sibling 成功；0 个 SDK Market 跨阶段，重试只读失败 50 ID |

精确 base 的两份生产文件通过 `git show <base>:<path>` 导出到 `/tmp`，以原模块名加载后
运行最终生命周期 nodeids：8 failed，全部失败位置是对象存活断言，正常结果断言先通过。
此负向重放在添加慢首批事件握手后重复，仍为 8 failed（9.05s）。
这不是修改后产品失败；证明测试继续拒绝原实现。

原始日志保留于本机：

- `/tmp/issue263-a-sdk-red.log`：2 failed，2.91s；`/tmp/issue263-a-sdk-green.log`：2 passed，1.74s。
- `/tmp/issue263-a-service-red.log`：2 failed，6.55s；`/tmp/issue263-a-service-green.log`：2 passed，5.70s。
- `/tmp/issue263-a-service-partial.log`：3 passed，10.06s。
- `/tmp/issue263-a-page.log`：分页 400/取消两项 passed，1.11s。
- `/tmp/issue263-a-final-baseline-red.log`：最终 8 failed，9.05s。
- `/tmp/issue263-a-focused.log`：首轮 85 passed，22.95s。
- `/tmp/issue263-a-final-focused.log`：最终 92 passed，16.51s。
- `/tmp/issue263-a-lifetime-repeat.log`：新增九项串行重复全部 passed，9.75s。

版本探测发现指定环境未安装 `pytest-xdist`（PackageNotFoundError）；未安装依赖，
`-n 2` 检查未运行。实际 SDK thread pools 和 runtime 8 路在途/停止回归已运行；
101 行用事件握手让首批等待尾批启动，有独立 5 秒 watchdog，不依赖任意 sleep。
所有修改后测试只有既有 websockets legacy deprecation warning。

## 定向检查命令与范围

当前 nodeid 清单在 `/tmp/issue263-a-nested-focused-nodeids.txt`，保留前两轮 101 项并追加十九项。
初次清单仍在 `/tmp/issue263-a-focused-nodeids.txt`。执行：

```sh
PYTHONPATH=src:tests /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  $(cat /tmp/issue263-a-nested-focused-nodeids.txt) -q
```

清单包含下面各函数及其全部参数；明确参数的检查仅运行列出的参数。

```text
tests/test_polymarket_trading.py::test_lp_metadata_preserves_event_evidence_and_market_links
tests/test_polymarket_trading.py::test_lp_metadata_logs_redacted_transport_cause
tests/test_polymarket_trading.py::test_lp_metadata_logs_redacted_rejection_status
tests/test_polymarket_trading.py::test_lp_metadata_limits_market_request_ids_and_preserves_pages
tests/test_polymarket_trading.py::test_lp_metadata_bounds_event_pages_and_releases_completed_sdk_trees
tests/test_polymarket_trading.py::test_lp_metadata_does_not_serialize_event_child_markets
tests/test_polymarket_trading.py::test_lp_metadata_event_pagination_preserves_failure_and_cancellation
tests/test_polymarket_trading.py::test_lp_metadata_closed_fallback_preserves_known_and_only_caches_proven_absence
tests/test_polymarket_trading.py::test_lp_metadata_continues_until_requested_ids_are_accounted
tests/test_polymarket_trading.py::test_lp_metadata_stops_after_all_requested_ids
tests/test_polymarket_trading.py::test_lp_metadata_missing_id_continuation_failure_stays_unknown
tests/test_polymarket_trading.py::test_lp_metadata_error_response_diagnostics_are_bounded_and_redacted
tests/test_polymarket_trading.py::test_lp_metadata_shares_one_public_client_per_call
tests/test_polymarket_trading.py::test_lp_metadata_cache_hit_within_ttl
tests/test_polymarket_trading.py::test_lp_metadata_cache_releases_objects_without_losing_cached_facts
tests/test_polymarket_trading.py::test_expire_lp_metadata_cache_forces_refetch
tests/test_polymarket_trading.py::test_lp_metadata_cache_delta_fetch_only_new_ids
tests/test_polymarket_trading.py::test_lp_metadata_negative_ttl_requeries_after_expiry
tests/test_polymarket_trading.py::test_lp_metadata_cache_not_poisoned_by_failures
tests/test_polymarket_trading.py::test_lp_metadata_refresh_cap_bounds_ids_per_call
tests/test_polymarket_trading.py::test_lp_metadata_batches_preserve_success_and_distinguish_absence_from_failure
tests/test_polymarket_trading.py::test_lp_metadata_cache_keeps_original_twelve_hour_expiry
tests/test_polymarket_trading.py::test_lp_metadata_warm_start_from_persisted_store
tests/test_polymarket_trading.py::test_lp_metadata_persisted_expiry_rollover
tests/test_polymarket_trading.py::test_lp_metadata_warm_entry_expires_at_persisted_stamp
tests/test_polymarket_trading.py::test_lp_metadata_event_read_failure_not_cached
tests/test_lp_metadata_lifetime.py::test_sdk_markets_release_before_numeric_and_direct_event_reads
tests/test_lp_metadata_lifetime.py::test_preparation_releases_metadata_payloads_before_next_batch_and_history
tests/test_lp_metadata_lifetime.py::test_paginated_failure_releases_sdk_rows_and_preserves_other_batch
tests/test_lp_metadata_lifetime.py::test_market_page_cancellation_does_not_prove_absence_or_read_events
tests/test_lp_order_registration_contract.py::test_old_negative_metadata_rechecks_closed_market_without_losing_positive_cache
tests/test_lp_order_registration_contract.py::test_closed_market_trade_only_order_registers_from_sdk_metadata
tests/test_lp_preparation_partial.py::test_partial_metadata_keeps_successful_markets_screenable
tests/test_lp_preparation_partial.py::test_metadata_retry_with_valid_history_cache_finishes_budget
tests/test_lp_preparation_partial.py::test_single_absent_metadata_retry_finishes_budget
tests/test_lp_preparation_partial.py::test_due_metadata_retry_dispatches_before_initial_history_pass_finishes
tests/test_lp_preparation_partial.py::test_interrupted_preparation_retry_remains_paused_and_recoverable
tests/test_polymarket_lp_memory.py::test_metadata_retry_does_not_reload_unrequested_reward_details
tests/test_polymarket_lp_memory.py::test_complete_ranking_trace_matches_issue249_minimum_order_contract
tests/test_lp_auto_recovery.py::test_metadata_full_read_preserves_operator_and_retry_facts
tests/test_lp_auto_recovery.py::test_concurrent_metadata_batches_keep_response_facts_isolated
tests/test_prediction_runtime.py::test_lp_metadata_warmup_advances_beyond_one_batch
tests/test_lp_preparation_recovery.py::test_real_metadata_adapter_keeps_database_chain_for_global_policy
tests/test_lp_preparation_recovery.py::test_global_recovery_before_begin_does_not_reassign_old_attempt
tests/test_lp_preparation_recovery.py::test_begin_preparation_write_failure_cannot_pause_recovered_generation
tests/test_polymarket_lp_memory.py::test_full_normal_and_backup_traversal_matches_issue249_minimum_order_contract
tests/test_lp_preparation_recovery.py::test_global_recovery_fences_late_old_results[history-False]
tests/test_lp_preparation_recovery.py::test_global_recovery_fences_late_old_results[history-True]
tests/test_lp_preparation_recovery.py::test_structured_database_failures_use_global_policy[5-metadata]
tests/test_lp_preparation_recovery.py::test_structured_database_failures_use_global_policy[1-metadata]
tests/test_lp_metadata_lifetime.py::test_canonical_mapping_releases_unused_nested_trees_before_event
tests/test_lp_metadata_lifetime.py::test_mapping_projection_keeps_aliases_event_count_and_read_errors
tests/test_lp_metadata_lifetime.py::test_nested_mapping_errors_keep_whole_call_unknown
tests/test_lp_metadata_lifetime.py::test_mapping_network_failures_remain_isolated_to_the_http_batch
```

这些检查覆盖共享 metadata API 的缓存、cap、字段/费用/event、去重、分页完整性、
closed fallback、失败诊断/400、UNKNOWN、重试、取消；service 的部分准备、重试历史缓存、
SQLite 错误政策、恢复代次与实际 runtime 停止；以及原正常/备用完整遍历 hash。
没有运行整服务或整 backend 测试。

## 串行离线内存对照

本节表格保留初次审查 tree 的测量；新源码的串行小规模复测见下一节。

可复现 probe：`tests/lp_metadata_lifetime_probe.py`。每次独立进程，四次串行运行；
RSS 与 tracemalloc 分开，未并行运行容量实验。每次 500 个 market、500 个 numeric event，
每 event 五个子市场。常规规则文字、状态、双 outcome、价格、费用、奖励、标签等合成输入，
无巨型 padding。仅比较 metadata 调用，不是启动或候选容量实验。

```sh
git show 267bb8844fe98d61bc5be66bfc006e961a91f594:src/open_trader/polymarket_trading.py \
  > /tmp/issue263-a-baseline-polymarket_trading.py
PYTHONPATH=src:tests /Users/ray/projects/open_trader/.venv/bin/python \
  tests/lp_metadata_lifetime_probe.py --mode rss \
  --source /tmp/issue263-a-baseline-polymarket_trading.py
PYTHONPATH=src:tests /Users/ray/projects/open_trader/.venv/bin/python \
  tests/lp_metadata_lifetime_probe.py --mode rss
```

然后按相同顺序各运行一次 `--mode tracemalloc`。旧文件 SHA-256：
`b3b859fd9cc5b3784039d2ccc7cf5728790f90ed02b1a2eb5ab8337ed1947a27`。

| 指标 | base | 修复 |
| --- | ---: | ---: |
| event 入口存活顶层 SDK Market | 500 | 0 |
| 返回后存活顶层 SDK Market | 0 | 0 |
| event 入口 tracemalloc current，MiB | 9.161 | 4.476 |
| tracemalloc peak，MiB | 12.124 | 10.372 |
| 返回后 tracemalloc current，MiB | 3.984 | 3.980 |
| event 入口时 RSS high-water，MiB | 166.484 | 144.094 |
| probe 读取结束 RSS peak，MiB | 170.734 | 148.078 |

RSS 值由 macOS `ru_maxrss` 得到，是进程截至采样时的高水位，不是阶段当前 RSS；
最后指标在 hash 序列化之前采样，包含既有测试 helper 导入。每侧仅单次样本，
不能将差值当作稳定收益或生产节省量。tracemalloc 不包含 SQLite/native/file cache。

四次结果均有 500 行、每行 27 字段、零缺失/失败、60 次请求，完整业务输出 hash：
`86d6d0bd01fac4ad0910c35e81e44d74f79c5a8481e156892e43efef18eca7d6`；
按路径及查询参数排序的请求 hash：
`90042ea9d04cc37cf9b114846c1495a6b6dd74bd6ac58a39d3c3de12947d856d`。
并发 HTTP 请求到达顺序不属于这个排序 hash；提交及消费顺序仍由代码和慢首批回归保持。
JSON 原始结果在 `/tmp/issue263-a-{baseline,current}-{rss,tracemalloc}.json`。

## 独立审查修复：canonical Mapping 嵌套生命周期

本节保留上一轮 tree `0fdf1b3a5d3f2150f05780103cabdcec7e018430` 的验证历史。
101 项通过未覆盖 nested collection 的调用级失败边界；该 tree 在本轮复审被拒绝，
失败复现与最新修复证据见下一节。本节旧源码 SHA-256 为
`139072bbbd9b09af61cee488c1969d6e6296935653895016cefdd8e0bc35c89e`。

反馈针对初次审查 tree 的 `_lp_market_facts` Mapping 分支浅复制。
通过公开 `lp_market_metadata_batch` 复现，不调用私有投影 helper：`list_markets`
返回原已支持的 canonical Mapping，其中 `rewards.clob_rewards`、event 的 `markets`，
以及 state/trading/fee/outcomes/sports/prices 的未用字段持有真实 SDK Market。
事件引用分别为 Mapping 或真实 SDK Event。fixture 只保留 weakref，不保留原响应。

新增两项生命周期回归先跑 RED：event 入口有 9/10 个无用嵌套 SDK 对象；
完整 27 字段、别名、费用与事件的字面量断言已先通过。最小修复后两项 GREEN，
event 入口与返回后均为 0。Mapping 分支只按现有消费者的固定字段投影，
没有通用递归序列化、输入契约扩展或新增 `condition` 别名。

事件引用在原 `_fetch_lp_market_metadata` 引用消费边界裁剪成 id/slug：
仅替换原可重复 canonical 容器，保留过滤后事件数量，包括空 Mapping；
其他迭代器仍按原消费方式处理，未把 collection 异常移入某个 market Future。
嵌套 SDK `model_dump` 失败继续由原 `_model_dict` 忽略。

新增 nodeids：

- `test_canonical_mapping_releases_unused_nested_trees_before_event[mapping/sdk]`：两项，
  SDK 原对象存活与全部 27 字段的独立字面量断言。
- `test_mapping_projection_keeps_aliases_event_count_and_read_errors`：七项，
  无事件、单事件、无效引用过滤、双事件、有效空 Mapping、一次性迭代器及 collection 异常。
  保留 market_id/title/url、tokenId 和 reward/fee 回退；坏嵌套 dump 不致整批失败。
  collection 异常仍使 51 ID 的整次调用 UNKNOWN，包括另一个 1-ID sibling batch。

对应源码与日志：

| 运行 | 结果 | 日志 |
| --- | --- | --- |
| 修复前两个新增生命周期用例 | 2 failed，1.01s | `/tmp/issue263-a-mapping-red.log` |
| 导出旧审查 tree 源码重放九项 | 2 failed（存活断言）/7 passed，0.77s | `/tmp/issue263-a-mapping-reviewed-tree.log` |
| 导出原 base 源码运行七项兼容检查 | 7 passed，0.56s | `/tmp/issue263-a-mapping-base-contract.log` |
| 修复后生命周期两个用例 | 2 passed，1.00s | `/tmp/issue263-a-mapping-green.log` |
| 修复后七项兼容检查 | 7 passed，1.08s | `/tmp/issue263-a-mapping-contract.log` |
| 当前源码全部定向 nodeids | 101 passed，19.39s | `/tmp/issue263-a-mapping-focused.log` |
| 新增九项串行重复 | 9 passed，1.01s | `/tmp/issue263-a-mapping-repeat.log` |

旧源码分别用 `git show 90f9e27d3cc690d032a69c5036232f590fe359ca:src/open_trader/polymarket_trading.py`
和原 base 同路径导出；通过 `/tmp/issue263-a-run-mapping-baseline.py` 以原模块名加载后运行公开用例。
全程沿用上述环境、禁 socket 和离线输入；没有修改历史 tree 或 baseline 文件。
未安装 pytest-xdist，仍未跑 `-n 2`，实际 thread-pool/runtime 8 路检查已包含于 101 项。

新源码另用原 500-market SDK probe 串行复测 RSS、tracemalloc，分别独立进程。
该轮源码 SHA-256 为本节列出的旧值；每次 500 行 × 27 字段、60 请求、event/返回后 SDK Market 为 0，
完整业务/请求 hash 与初次四次实验一致。新样本：

- RSS：event 入口截至当时 high-water 143.906 MiB，读取结束 peak 148.922 MiB；
  `/tmp/issue263-a-mapping-current-rss.json`。
- tracemalloc：event 入口 current 4.466 MiB，peak 9.854 MiB，返回后 current 3.985 MiB；
  `/tmp/issue263-a-mapping-current-tracemalloc.json`。

此 probe 是原 SDK fixture，不是新增 Mapping 树的内存消融；Mapping 证据是公开用例的对象存活，
不从 RSS 的单次波动推导额外稳定收益或生产节省量。当前执行命令为上述 probe 命令，
去掉 `--source`，依次分别使用 `--mode rss`、`--mode tracemalloc`。

## 复审修复：nested Mapping 错误的调用级边界

复审在上述 tree 发现真实差异：51 条 canonical Mapping，首个 50-ID 批次的一条
`outcomes.items()` 抛错，另一个 1-ID sibling 成功。原 base 最终为
`state=unknown`、0 markets、51 failed；被审查树在 worker 提前遍历，错误变成单批失败，
返回 `partial`、1 market、50 failed。此前 events iterator 检查不能证明 nested Mapping 边界。
旧树、源码及其 101 passed 日志保留，不以修复后通过抹去该复审失败。

新增公开 batch API 回归覆盖八处访问：outcomes.items、state、trading、fee_schedule、
rewards、sports、prices 和单个 outcome 的字段访问。每处分别抛普通 RuntimeError，
或以真实 SQLite OperationalError 为 cause 的 RuntimeError；另有取消信号置位的
outcomes.items 场景，共 17 项。事件握手保证正常 sibling 实际开始读取，有独立 5 秒 watchdog。
断言整次调用 0 markets/51 failed；SQLite cause 链、SQLITE_ERROR code/name 原样保留；
取消场景仍为 cancelled，其余为 UNKNOWN。

两个负向检查分别让 1-ID 或 50-ID HTTP 批次 ReadTimeout，另一个批次正常：
必须仍为 partial，成功结果完整保留，仅该 HTTP 批次失败。没有把网络失败改成全调用失败。
所有 fixture 保持禁 socket、离线输入；测试用内存 SQLite 连接显式关闭。

最小修复把既有有限字段投影从 worker 移到 `_fetch_lp_market_metadata` 的调用级消费处，
在已请求 ID 校验后、event 阶段前进行；nested `.items()` / `.get()` 异常直接进入
原外层调用错误处理。使用 `.get()` 和缺失哨兵保留缺字段与显式 None 的区别及现有默认值。
没有添加异常包装、通用异常框架或递归序列化。完整 SDK Market 仍在线程内投影，
累计 rows 中没有重新放回 SDK Market；Mapping 无用嵌套对象仍在 event 前释放。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 修复前当前 tree 的新增 19 项 | 17 failed/2 passed，1.13s | `/tmp/issue263-a-nested-boundary-red.log` |
| 原 base 的同组 19 项 | 19 passed，0.54s | `/tmp/issue263-a-nested-base-contract.log` |
| 精确复审 tree 导出源码重放 | 17 failed/2 passed，0.72s | `/tmp/issue263-a-nested-reviewed-tree.log` |
| 修复后 19 项及 Mapping 生命周期两项 | 21 passed，0.98s | `/tmp/issue263-a-nested-boundary-green.log` |
| 新源码全部直接相关检查 | 120 passed，22.97s | `/tmp/issue263-a-nested-focused.log` |
| 显式关闭测试 SQLite 连接后，新十九项重复 | 19 passed，1.15s | `/tmp/issue263-a-nested-boundary-repeat.log` |
| 最终 fixture 在原 base 对照 | 19 passed，0.54s | `/tmp/issue263-a-nested-base-final.log` |
| 最终 fixture 在复审 tree 对照 | 17 failed/2 passed，0.67s | `/tmp/issue263-a-nested-reviewed-final.log` |

旧源码用 `git show 0fdf1b3a5d3f2150f05780103cabdcec7e018430:src/open_trader/polymarket_trading.py`
导出；`/tmp/issue263-a-run-nested-baseline.py` 按原模块名加载，再运行两个新增公开 nodeid。
本轮没有其他失败尝试；只有既有 websockets deprecation warning。pytest-xdist 仍缺失，
未安装或运行 `-n 2`；实际 SDK/runtime 并发与停止检查在 120 项内通过。

最新源码另串行运行原 500-market SDK probe 的 RSS 与 tracemalloc，各独立进程。
源码 SHA-256 与身份节一致，500 行 × 27 字段、60 请求、完整业务/请求 hash 与旧样本一致，
event 与返回后存活 SDK Market 均为 0：

- RSS high-water：event 入口 143.828 MiB，读取结束 146.063 MiB；
  `/tmp/issue263-a-nested-current-rss.json`。
- tracemalloc：event current 4.251 MiB、peak 7.596 MiB、返回后 current 3.986 MiB；
  `/tmp/issue263-a-nested-current-tracemalloc.json`。

仍是单次本机合成样本，不是 Linux cgroup、Mapping 容量消融或生产容量验收。

## 剩余风险与未运行项

已证实跨阶段暂态持有缩短；旧实现返回后也释放 SDK Market，不能称为返回后对象泄漏。
完整规范化 rows 仍随单次实际调用上限增长，未改变批量/cap 或截断结果。
没有测 Linux cgroup、PSS、SQLite 文件驻留/脏页或实际 704 MiB cap/640 MiB guard；
没有真实响应回放、后台重叠、云端完整启动或 24 小时观察。本次不作生产容量结论。
初次独立审查反馈已处理；新 staged tree 等待协调者复审。
CI、publication、merge、deployment 均未执行，后续由协调者按各自授权安排。

## 2026-10-07 同步最新 main 后的验证

- 基线：`7d3ac942ac4053cc93ebe862953f15b53637695b`，含 #264 与 #262。
- 仅 CHANGELOG 发生文本冲突，双方记录均保留。`git range-diff` 证明原 PR A 的生产和测试补丁不变；本次未新增生产行为。
- 指定现有 Python 环境、`PYTHONPATH=src:tests`：原 54 个函数 nodeid 展开为 120 项，全部通过。
- 开启 `OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS=1`，运行 #262 四个恢复代次/维护/持久化 nodeid 与 #264 两个历史批次发布 nodeid：9 项全部通过。
- 所有执行命令与日志保存在本机 `~/.local/share/open-trader/issue263-implementation/rebase-20261007/a-commands.json`、`a-focused.log`、`a-integration.log`。
- 只有既有 websockets deprecation warning；没有失败、依赖安装、宽泛后端测试或生产操作。当前环境仍无 pytest-xdist。
- 重放的原始 RED、内存对照及历史 CI 仍属于其各自旧 SHA；本次没有重新声称这些实验的结果。
- 本次审查目标为最新基线至 rebased HEAD 的完整 PR diff，加上暂存的交付记录更新；最终 reviewed tree、提交和新 CI 身份由交付记录及 PR 描述绑定。
- 当前生产文件 SHA-256：
  - `src/open_trader/polymarket_lp.py`：`32146eec67343160c62279d4a4fe10da7bea1a8d774a2250abb5853ab7d29695`。
  - `src/open_trader/polymarket_trading.py`：`68b04b13751af967c1996316ff14134aeae9ac6d8cba5c227ed8a5ec18fabd5e`。
