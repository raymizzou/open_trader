# Issue 27 真实验证（2026-08-07，branch codex/issue-27-gamma-clob-parse @ 659e3ffa）

直接运行（不启动/重启任何 watcher，只读）：

```
PYTHONPATH=src python - <<'PY'
... PredictSource(config.predict).list_open_markets()
    → resolve_explicit_market_pairs(markets, gamma_lookup=PublicClient.list_markets)
PY
```

## 结果

| 指标 | 修复前（issue 实测） | 修复后 |
|---|---|---|
| Predict 开放市场 | 16 | 16 |
| 带候选 conditionId | 14 | 14 |
| matched_pairs | 0 | **13** |
| unresolved | 14 | **1** |

13 个 pair 全部从 Gamma SDK `Market` 对象解析出 YES/NO token、精确 `end_date`（close_at，settlement_at 回退为 close_at）与费率；市场均为 700 bps、close 2026-10-01 / 2027-01-01。

## 唯一 unresolved 的原因（fail-closed，非解析缺陷）

`0x1a659ad30363047271` "Will Base launch a token by December 31"：

- Gamma SDK `Market.state.end_date = None`（源数据没有收盘时间）；
- tokens 可正常解析（YES/NO token_id 均存在）；
- 按确认的规则「没有可信 close 就不配对」，保持 unresolved，绝不用 CLOB `end_date_iso`（00:00 日期）或任何近似值冒充。

## 形状语义核验

- Gamma SDK `state.end_date` 与 Gamma REST `endDate` 逐笔一致（Ethiopia/LoL 3 市场实测）。
- CLOB REST `end_date_iso` 是事件日期取 00:00，与 `endDate` 不是同一语义（LoL 市场差 16–19.5 小时）→ 已按 Gamma-only 决策移除 CLOB 兜底。
