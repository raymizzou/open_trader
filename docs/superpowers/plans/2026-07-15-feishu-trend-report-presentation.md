# 飞书 A 股趋势报告中文展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把飞书和本地 Markdown 改成中文“操作优先”清单，同时保持 JSON 审计结构、策略判断和交付状态机不变。

**Architecture:** 继续使用 `TrendReport` 作为唯一渲染输入。在现有 `a_share_trend.py` 中增加小型中文标签映射，并只重排 `render_markdown()`；`_report_payload()` 不经过翻译层，因此 JSON 继续保存原始代码和事实。

**Tech Stack:** Python 标准库、现有 Markdown 文本通知、pytest；不增加模板引擎、飞书卡片协议或依赖。

## Global Constraints

- 只修改展示层，不改选股、排序、仓位、买卖判断、保护线、API、缓存、receipt 或调度。
- Markdown/飞书采用：摘要 → 卖出 → 买入 → 持有/人工复核 → 中文附录 → 免责声明。
- 已知内部动作、原因、API 技术字段和绝对路径不得出现在 Markdown 主文；JSON 保持原始值。
- 未知内部原因显示为 `未知原因（<原代码>）`，不得丢失。
- 历史冻结报告不重写、不生成 revision、不重复发送。
- 代码行为变更后必须运行 `make acceptance`；仅 `PASS` 可完成，并在 PASS 后部署同一 SHA。

---

### Task 1: 中文操作优先 Markdown 渲染

**Files:**
- Modify: `src/open_trader/a_share_trend.py:720-850`
- Test: `tests/test_a_share_trend.py:980-1040`

**Interfaces:**
- Consumes: `render_markdown(report: TrendReport) -> str` 的现有 `TrendReport` 输入。
- Produces: 中文操作优先 Markdown；`_report_payload(report)` 的 JSON 输出保持原样。

- [ ] **Step 1: 写中文化与层级的失败测试**

在 `tests/test_a_share_trend.py` 增加：

```python
def test_markdown_is_operation_first_and_translates_internal_codes() -> None:
    built = replace(
        report(candidates=(candidate("600001"),)),
        holdings=(
            trend_module.HoldingDecision(
                symbol="600025",
                name="华能水电",
                industry="电力",
                action="SELL_ALL",
                reason="left_trend_right_side",
                initial_line=Decimal("9.32"),
                active_line=Decimal("9.32"),
                atr=Decimal("0.10"),
                historical=True,
            ),
        ),
    )
    markdown = render_markdown(built)

    assert markdown.index("## 操作摘要") < markdown.index("## 开盘前：确认卖出")
    assert markdown.index("## 开盘前：确认卖出") < markdown.index(
        "## 09:30–10:00：按顺序考虑买入"
    )
    assert "全部卖出" in markdown
    assert "SELL_ALL" not in markdown
    assert "HOLD" not in markdown
    assert "left_trend_right_side" not in markdown


def test_markdown_translates_exclusion_and_api_facts_without_paths() -> None:
    built = replace(
        report(),
        excluded={
            "002303": ["right_side_days_not_below_10"],
            "159835": ["amount_below_1"],
            "551520": ["atr_unavailable"],
        },
        api_facts=(
            "getUpdateStatus rows=6",
            "getComponentTicker rows=39 cache=client-managed",
            "getTickerSnapshot fields=tmId,tickerName rows=44 cache=client-managed",
        ),
        data_sources=(
            "Trend Animals",
            "Futu CN calendar/QFQ daily K-line",
            "/Users/ray/projects/open_trader/data/latest/portfolio.csv",
        ),
    )
    markdown = render_markdown(built)

    assert "进入右侧趋势已满 10 天" in markdown
    assert "日成交额不足 1 亿元" in markdown
    assert "缺少 ATR 数据" in markdown
    assert "数据更新状态：已检查 6 条" in markdown
    assert "候选池成分：39 条" in markdown
    assert "趋势快照：44 条" in markdown
    assert "getUpdateStatus" not in markdown
    assert "cache=client-managed" not in markdown
    assert "/Users/ray" not in markdown
    assert "东方财富账户快照" in markdown


def test_markdown_unknown_reason_is_visible_but_json_keeps_raw_codes() -> None:
    built = replace(report(), excluded={"600001": ["future_reason_code"]})

    markdown = render_markdown(built)
    payload = trend_module._report_payload(built)

    assert "未知原因（future_reason_code）" in markdown
    assert payload["excluded"]["600001"] == ["future_reason_code"]
```

- [ ] **Step 2: 运行定向测试并确认 RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_markdown_is_operation_first_and_translates_internal_codes \
  tests/test_a_share_trend.py::test_markdown_translates_exclusion_and_api_facts_without_paths \
  tests/test_a_share_trend.py::test_markdown_unknown_reason_is_visible_but_json_keeps_raw_codes -q
```

Expected: 三项失败，因为现有 Markdown 仍输出英文动作、原因、API 方法名和本地路径。

- [ ] **Step 3: 增加集中式中文标签和最小解析 helper**

在 `render_markdown()` 前加入：

```python
ACTION_LABELS = {
    "SELL_ALL": "全部卖出",
    "HOLD": "继续持有",
    "MANUAL_REVIEW": "人工复核",
}

REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "trend_intact": "趋势保持完好",
    "right_side_not_true": "尚未进入右侧趋势",
    "strength_not_above_90": "趋势强度未超过 90",
    "right_side_days_not_below_10": "进入右侧趋势已满 10 天",
    "not_tradable": "当前不可交易",
    "amount_below_1": "日成交额不足 1 亿元",
    "danger_unknown": "危险信号未知",
    "name_missing": "标的名称缺失",
    "asset_missing": "资产类型缺失",
    "unsupported_asset": "不属于 A 股股票或境内 ETF",
    "already_held": "当前账户已经持有",
    "excluded_security": "北交所、ST 或退市标的",
    "unsupported_exchange": "不属于沪深市场",
    "atr_unavailable": "缺少 ATR 数据",
    "data_date_mismatch": "数据日期不一致",
}


def _action_label(value: str) -> str:
    return ACTION_LABELS.get(value, f"未知动作（{value}）")


def _reason_label(value: str) -> str:
    return REASON_LABELS.get(value, f"未知原因（{value}）")


def _api_fact_label(value: str) -> str:
    if value.startswith("getUpdateStatus rows="):
        return f"数据更新状态：已检查 {value.rsplit('=', 1)[-1]} 条"
    if value.startswith("getComponentTicker rows="):
        count = value.split(" rows=", 1)[1].split(" ", 1)[0]
        return f"候选池成分：{count} 条"
    if value.startswith("getTickerSnapshot fields=") and " rows=" in value:
        count = value.split(" rows=", 1)[1].split(" ", 1)[0]
        return f"趋势快照：{count} 条"
    return "其他接口事实：详见 JSON 审计文件"


def _data_source_label(value: str) -> str:
    if value.endswith("/portfolio.csv"):
        return "东方财富账户快照"
    return {
        "Trend Animals": "趋势动物",
        "Futu CN calendar/QFQ daily K-line": "富途 A 股交易日历与前复权日线",
    }.get(value, value)
```

- [ ] **Step 4: 按操作优先层级替换 `render_markdown()`**

保留现有金额、候选、行业和免责声明计算，只重排输出：

```python
def render_markdown(report: TrendReport) -> str:
    freshness = "已更新" if report.account.fresh else "已过期，禁止正式买入"
    sells = [item for item in report.holdings if item.action == "SELL_ALL"]
    holds = [item for item in report.holdings if item.action == "HOLD"]
    reviews = [item for item in report.holdings if item.action == "MANUAL_REVIEW"]
    lines = [
        f"# A股趋势操作计划 · {report.execution_date}",
        "",
        "## 操作摘要",
        "",
        f"数据日期：{report.as_of_date}｜账户：{freshness}",
        f"全部卖出 {len(sells)}｜允许买入 {len(report.buy_actions)}｜"
        f"继续持有 {len(holds)}｜人工复核 {len(reviews)}",
        "",
        "## 开盘前：确认卖出",
        "",
    ]
    if sells:
        for item in sells:
            line = f"- {item.symbol} {item.name}｜{_reason_label(item.reason)}"
            if item.active_line is not None:
                line += f"｜活动保护线 {_money(item.active_line)}"
            lines.append(line)
    else:
        lines.append("- 无需卖出。")

    lines.extend(["", "## 09:30–10:00：按顺序考虑买入", ""])
    if report.buy_actions:
        for index, item in enumerate(report.buy_actions, 1):
            lines.append(
                f"- {index}. {item.symbol} {item.name}｜约 {item.estimated_shares} 股｜"
                f"金额上限 {_money(item.target_amount)} 元｜"
                f"预计保护线 {_money(item.estimated_initial_line)}"
            )
        lines.append("- 实际股数按东方财富实时价格向下取整为 100 股整数倍，不得超过金额上限。")
    else:
        lines.append("- 无允许买入标的。")
    if not sells and not report.buy_actions:
        lines.extend(["", NO_ACTION_TEXT])

    lines.extend(["", "## 继续持有与人工复核", ""])
    for item in [*holds, *reviews]:
        line = f"- {item.symbol} {item.name}｜{_action_label(item.action)}｜{_reason_label(item.reason)}"
        if item.active_line is not None:
            line += f"｜活动保护线 {_money(item.active_line)}"
        lines.append(line)
    if not holds and not reviews:
        lines.append("- 无。")

    lines.extend(["", "## 中文附录", "", "### 前 10 名候选", ""])
    for index, item in enumerate(report.candidates[:10], 1):
        lines.append(
            f"- {index}. {item.symbol} {item.name}｜强度 {item.strength}｜"
            f"右侧 {item.days} 天｜成交额 {item.amount} 亿元｜行业 {item.industry or '未知'}"
        )
    if not report.candidates:
        lines.append("- 无合格候选。")

    lines.extend(["", "### 排除项", ""])
    for symbol, reasons in report.excluded.items():
        lines.append(f"- {symbol}｜{'、'.join(_reason_label(reason) for reason in reasons)}")
    lines.extend(f"- 账户例外｜{item}" for item in report.account.exceptions)
    if not report.excluded and not report.account.exceptions:
        lines.append("- 无。")

    lines.extend(["", "### 数据与成本", ""])
    lines.extend(f"- {_api_fact_label(fact)}" for fact in report.api_facts)
    lines.extend(f"- 数据来源：{_data_source_label(source)}" for source in report.data_sources)
    lines.append(
        "- API 计费估算："
        + ("未知" if report.estimated_api_cost is None else str(report.estimated_api_cost))
    )
    lines.append(
        "- 本次余额变化："
        + ("未知" if report.actual_api_cost is None else str(report.actual_api_cost))
    )
    lines.extend(["", "## 免责声明", "", DISCLAIMER_TEXT, ""])
    return "\n".join(lines)
```

- [ ] **Step 5: 运行 Markdown 与 JSON 定向测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py -k 'markdown or report_json or frozen' -q
```

Expected: PASS；Markdown 全部中文，JSON 仍保存原始代码。

- [ ] **Step 6: 运行全量测试并提交**

Run:

```bash
.venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: 全部 PASS，只有本任务两个文件发生变化。

Commit:

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: present A-share trend reports in Chinese"
```

- [ ] **Step 7: 运行强制验收并部署相同 SHA**

Run:

```bash
make acceptance
git rev-parse HEAD
PYTHONPATH=src .venv/bin/python -m open_trader trend-a-share-report \
  --date 2026-07-14 --config config/daily_premarket.env
```

Expected:

- `make acceptance` 返回 `PASS`。
- 2026-07-14 已有冻结报告，真实命令返回 `existing`，不生成 revision、不重复发送。
- 验收后重启 Dashboard 到该 SHA，并重新安装 CN launchd，使 `WorkingDirectory`、配置路径和 `PYTHONPATH` 指向当前 final worktree。
- 核对新 PID、cwd、SHA、fresh log、Futu 非 stale 和 `http://127.0.0.1:8766/` HTTP 200。

