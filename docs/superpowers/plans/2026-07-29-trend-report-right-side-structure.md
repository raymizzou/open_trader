# Trend Report Right-Side Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add prior-to-current right-side count and market-cap ratios to the existing trend-report industry table, with deterministic accessible explanations and no trading-rule changes.

**Architecture:** Extend the existing optional `IndustryContext` data and history path, request both provider aggregate ratios in the existing industry-state snapshot call, and reuse the frozen report payload for Markdown and Dashboard rendering. Keep the locally calculated breadth fields untouched for validation and candidate ordering; the new provider pair is presentation-only.

**Tech Stack:** Python 3.12, stdlib `dataclasses`/`Decimal`/JSON, pytest, existing vanilla JavaScript/CSS Dashboard, existing Playwright Dashboard acceptance.

## Global Constraints

- Start implementation from local `main` in an isolated worktree; the current documentation worktree is not the implementation baseline.
- Do not add dependencies, a component, chart, database, background job, LLM call, or historical API backfill.
- Reuse the existing `行业上下文` table and its desktop/mobile styles.
- Display exactly `右侧个数占比` and `右侧市值占比`; do not render a `变化` column.
- Provider ratios are optional and must not affect `IndustryContext.valid`, candidate ordering, actions, sizing, or risk limits.
- Keep the existing local `right_count`, `valid_count`, `right_share`, and history ordering behavior unchanged.
- Show `未提供` for an invalid current ratio and `当前值 · 基准建立中` when only the prior ratio is missing.
- Generate tooltip and Markdown explanations deterministically; identical values must produce identical copy.
- Preserve legacy frozen reports and v1 industry-context history.
- Run focused tests while developing. Run `make acceptance` only once the source, tests, and changelog are final.
- Only an acceptance `PASS` is review-ready. Redeploy the exact accepted SHA before sharing the review URL.

## Execution Setup

At execution time, use `superpowers:using-git-worktrees` and create exactly:

```bash
git worktree add \
  /Users/ray/projects/open_trader/.worktrees/trend-report-right-side-structure \
  -b feat/trend-report-right-side-structure main
git rev-list --reverse main..docs/trend-report-industry-concentration \
  | xargs git -C /Users/ray/projects/open_trader/.worktrees/trend-report-right-side-structure cherry-pick
```

Run every task below from:

```text
/Users/ray/projects/open_trader/.worktrees/trend-report-right-side-structure
```

---

### Task 1: Extend `IndustryContext` and its existing history

**Files:**
- Modify: `src/open_trader/trend_industry_context.py:17-245, 390-560`
- Test: `tests/test_trend_industry_context.py`

**Interfaces:**
- Consumes: the existing `calculate_industry_context(...)`, `attach_prior_context(...)`, `write_industry_context_history(...)`, and `load_latest_prior_context(...)`.
- Produces: four optional `Decimal` fields on `IndustryContext`: `aggregate_right_count_ratio`, `aggregate_right_market_cap_ratio`, `prior_aggregate_right_count_ratio`, and `prior_aggregate_right_market_cap_ratio`.

- [ ] **Step 1: Write failing extraction, prior, and legacy-history tests**

Extend `_industry(...)` in `tests/test_trend_industry_context.py` with optional provider values and add:

```python
def test_calculation_extracts_optional_aggregate_ratios_without_affecting_validity() -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=list(range(1, 11)),
        member_rows=[_member(tm_id) for tm_id in range(1, 11)],
        industry_row={
            **_industry(),
            "TrendRightSideCountRatio": "0.191",
            "TrendRightSideMktCapRatio": "0.650",
        },
        warm_to_hot_count=0,
    )

    assert context.valid
    assert context.aggregate_right_count_ratio == Decimal("0.191")
    assert context.aggregate_right_market_cap_ratio == Decimal("0.650")


def test_invalid_aggregate_ratios_become_unavailable_without_invalidating_context() -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=list(range(1, 11)),
        member_rows=[_member(tm_id) for tm_id in range(1, 11)],
        industry_row={
            **_industry(),
            "TrendRightSideCountRatio": "NaN",
            "TrendRightSideMktCapRatio": "1.01",
        },
        warm_to_hot_count=0,
    )

    assert context.valid
    assert context.aggregate_right_count_ratio is None
    assert context.aggregate_right_market_cap_ratio is None


def test_attach_prior_context_freezes_provider_aggregate_baseline() -> None:
    current = replace(
        _valid_context(),
        aggregate_right_count_ratio=Decimal("0.191"),
        aggregate_right_market_cap_ratio=Decimal("0.650"),
    )
    prior = replace(
        _valid_context(as_of_date="2026-07-23"),
        aggregate_right_count_ratio=Decimal("0.150"),
        aggregate_right_market_cap_ratio=Decimal("0.600"),
    )

    [attached] = attach_prior_context((current,), {current.industry_tm_id: prior})

    assert attached.prior_aggregate_right_count_ratio == Decimal("0.150")
    assert attached.prior_aggregate_right_market_cap_ratio == Decimal("0.600")


def test_history_loader_accepts_legacy_rows_without_aggregate_fields(
    tmp_path: Path,
) -> None:
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-23T18:00:00+08:00",
        strategy_version="v10",
        contexts=(_valid_context(as_of_date="2026-07-23"),),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload["industries"]:
        for key in (
            "aggregate_right_count_ratio",
            "aggregate_right_market_cap_ratio",
            "prior_aggregate_right_count_ratio",
            "prior_aggregate_right_market_cap_ratio",
        ):
            row.pop(key)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    )

    assert loaded[700001].aggregate_right_count_ratio is None
    assert loaded[700001].aggregate_right_market_cap_ratio is None
```

- [ ] **Step 2: Run the new tests and confirm the missing-field failures**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py::test_calculation_extracts_optional_aggregate_ratios_without_affecting_validity \
  tests/test_trend_industry_context.py::test_invalid_aggregate_ratios_become_unavailable_without_invalidating_context \
  tests/test_trend_industry_context.py::test_attach_prior_context_freezes_provider_aggregate_baseline \
  tests/test_trend_industry_context.py::test_history_loader_accepts_legacy_rows_without_aggregate_fields -q
```

Expected: FAIL because the four dataclass fields and extraction do not exist.

- [ ] **Step 3: Implement the smallest additive model change**

Add the optional fields with `None` defaults:

```python
@dataclass(frozen=True)
class IndustryContext:
    # existing required fields stay unchanged
    aggregate_right_count_ratio: Decimal | None = None
    aggregate_right_market_cap_ratio: Decimal | None = None
    prior_as_of_date: str | None = None
    prior_temperature: str | None = None
    prior_right_share: Decimal | None = None
    prior_aggregate_right_count_ratio: Decimal | None = None
    prior_aggregate_right_market_cap_ratio: Decimal | None = None
    temperature_direction: str | None = None
    right_share_change_pp: Decimal | None = None
```

Use one validator for provider ratios:

```python
def _valid_ratio(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() and Decimal("0") <= parsed <= Decimal("1") else None
```

Inside the already date/id-validated `industry_row` branch, read both documented
keys and their lower-camel aliases:

```python
aggregate_right_count_ratio = _valid_ratio(
    _row_value(
        industry_row,
        "TrendRightSideCountRatio",
        "trendRightSideCountRatio",
    )
)
aggregate_right_market_cap_ratio = _valid_ratio(
    _row_value(
        industry_row,
        "TrendRightSideMktCapRatio",
        "trendRightSideMktCapRatio",
    )
)
```

Set both current fields in the returned context. In `attach_prior_context`,
copy the prior aggregate values after validating that the prior row is valid and
strictly earlier; keep the existing temperature/local-breadth requirements only
around the existing ordering-history fields.

In `_context_from_mapping`, exempt only the four new fields from the
all-fields-present check, parse them with `_valid_ratio`, and leave all existing
legacy validation unchanged.

- [ ] **Step 4: Run the whole core history suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_industry_context.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the core data change**

```bash
git add src/open_trader/trend_industry_context.py tests/test_trend_industry_context.py
git commit -m "feat: record aggregate industry right-side ratios"
```

---

### Task 2: Request, freeze, audit, and render the provider pair

**Files:**
- Modify: `src/open_trader/a_share_trend.py:98-109, 1897-2032, 3717-3970, 5240-5430`
- Verify only: `src/open_trader/market_trend.py:1031-1207`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: the four optional `IndustryContext` fields from Task 1.
- Produces: one existing industry-state request containing both provider fields, frozen JSON/history facts for all markets, and deterministic Markdown transitions/explanations.

- [ ] **Step 1: Write failing shared-request and Markdown tests**

In `test_collect_industry_contexts_queries_only_eligible_industries_and_unions_members`,
return the two provider values from the existing state-row branch and assert:

```python
assert contexts[0].aggregate_right_count_ratio == Decimal("0.191")
assert contexts[0].aggregate_right_market_cap_ratio == Decimal("0.650")
assert "TrendRightSideCountRatio" in trend_module.INDUSTRY_STATE_FIELDS
assert "TrendRightSideMktCapRatio" in trend_module.INDUSTRY_STATE_FIELDS
```

Add a Markdown/payload regression using `_industry_context(...)`:

```python
def test_report_freezes_and_renders_aggregate_right_side_structure() -> None:
    context = replace(
        _industry_context(700001),
        industry="银行",
        aggregate_right_count_ratio=Decimal("0.191"),
        aggregate_right_market_cap_ratio=Decimal("0.650"),
        prior_aggregate_right_count_ratio=Decimal("0.150"),
        prior_aggregate_right_market_cap_ratio=Decimal("0.600"),
    )
    report = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        industry_contexts=(context,),
    )

    payload = trend_module._report_payload(report)
    markdown = trend_module.render_markdown(report)

    assert payload["industry_contexts"][0]["aggregate_right_count_ratio"] == "0.191"
    assert payload["industry_contexts"][0]["aggregate_right_market_cap_ratio"] == "0.650"
    assert "右侧个数占比 15% → 19.1%" in markdown
    assert "右侧市值占比 60% → 65%" in markdown
    assert "高于右侧个数占比 45.9 个百分点" in markdown
    assert "不是账户仓位或上涨概率" in markdown
```

Update the existing HK and US report fakes in `tests/test_market_trend.py` to
return the same provider pair in `INDUSTRY_STATE_FIELDS` rows, then assert the
frozen JSON contains both values. This proves the shared collector covers CN,
HK, and US without market-specific branches.

Expand the CN/HK/US fake billing catalogs with the two new fields at `0.004`
each:

```python
catalog_fields = tuple(dict.fromkeys((*UNIFIED_TREND_FIELDS, *INDUSTRY_STATE_FIELDS)))
return [{
    "field": field,
    "priceCost": (
        "0.071" if field == "tickerName"
        else "0.004" if field in {
            "TrendRightSideCountRatio",
            "TrendRightSideMktCapRatio",
        }
        else "0"
    ),
} for field in catalog_fields]
```

For one eligible industry, update the expected estimate from `0.142` to
`0.150`. Keep the catalog-drift test proving that no paid snapshot starts when a
required field price changes unexpectedly.

- [ ] **Step 2: Run focused integration tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_collect_industry_contexts_queries_only_eligible_industries_and_unions_members \
  tests/test_a_share_trend.py::test_report_freezes_and_renders_aggregate_right_side_structure \
  tests/test_market_trend.py::test_hk_report_uses_simulation_holdings_when_actual_statement_is_stale \
  tests/test_market_trend.py::test_actual_tiger_snapshots_do_not_change_us_simulation_report -q
```

Expected: FAIL because the provider fields are not requested or rendered.

- [ ] **Step 3: Add both fields to the existing state request**

Change only the shared tuple:

```python
INDUSTRY_STATE_FIELDS = (
    "tmId",
    "asOfDate",
    "trendTemperatureCurr",
    "trendStrengthLocalCurr",
    "TrendRightSideCountRatio",
    "TrendRightSideMktCapRatio",
)
```

Do not create another API call. Existing CN/HK/US cost estimation, completeness,
API facts, and replay evidence already iterate `INDUSTRY_STATE_FIELDS`, so they
will include both fields automatically.

- [ ] **Step 4: Add deterministic Python display helpers and Markdown rows**

Place these private helpers next to `render_markdown`:

```python
def _industry_ratio_percent(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{_money(value * Decimal('100'))}%"


def _industry_ratio_transition(
    current: Decimal | None,
    prior: Decimal | None,
) -> str:
    current_text = _industry_ratio_percent(current)
    if current_text is None:
        return "未提供"
    prior_text = _industry_ratio_percent(prior)
    return (
        f"{prior_text} → {current_text}"
        if prior_text is not None
        else f"{current_text} · 基准建立中"
    )


def _industry_structure_explanation(context: IndustryContext) -> str:
    count = context.aggregate_right_count_ratio
    market_cap = context.aggregate_right_market_cap_ratio
    if count is None or market_cap is None:
        return ""
    prior_market_cap = context.prior_aggregate_right_market_cap_ratio
    gap = (market_cap - count) * Decimal("100")
    relation = "高于" if gap > 0 else "低于" if gap < 0 else "等于"
    bias = (
        "右侧更偏大市值成分"
        if gap > 0
        else "右侧更偏小市值成分"
        if gap < 0
        else "两个占比相同"
    )
    text = (
        f"右侧市值占比{relation}右侧个数占比 {_money(abs(gap))} 个百分点，"
        f"{bias}。"
    )
    if prior_market_cap is not None:
        market_cap_change = (market_cap - prior_market_cap) * Decimal("100")
        text = (
            f"较前一有效交易日"
            f"{'上升' if market_cap_change > 0 else '下降' if market_cap_change < 0 else '持平'}"
            f" {_money(abs(market_cap_change))} 个百分点。"
            + text
        )
    prior_count = context.prior_aggregate_right_count_ratio
    if prior_count is not None and prior_market_cap is not None:
        gap_change = gap - (prior_market_cap - prior_count) * Decimal("100")
        text += (
            f"结构差较前值{'扩大' if gap_change > 0 else '收窄' if gap_change < 0 else '持平'}"
            f" {_money(abs(gap_change))} 个百分点。"
        )
    return text + "该指标不是账户仓位或上涨概率。"
```

Append one `### 行业上下文` section in the existing Chinese appendix. Each row
uses the two transition helpers plus `_industry_structure_explanation(context)`.
Do not add another report model.

- [ ] **Step 5: Run the affected Python suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py -q
```

Expected: all tests PASS, including unchanged candidate-ordering tests.

- [ ] **Step 6: Commit API and frozen-report integration**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: freeze right-side structure in trend reports"
```

---

### Task 3: Replace the existing Dashboard breadth columns and add explanations

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:238-370, 3428-3502`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2192-2249, 4989-5035`
- Modify: `src/open_trader/dashboard_acceptance.py:1297-1375`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: frozen aggregate current/prior fields from Task 2.
- Produces: the existing `行业上下文` table with two transition columns, one shared fixed tooltip per section, and pointer/touch/keyboard interaction.

- [ ] **Step 1: Write failing Dashboard rendering tests**

Add a focused `run_dashboard_js(...)` test:

```javascript
const html = renderTrendIndustryContext({
  industry_context_status:{current_complete:true},
  industry_contexts:[{
    industry:"银行",temperature:"热",temperature_direction:"rising",
    strength:"100",warm_to_hot_count:6,valid:true,invalid_reasons:[],
    aggregate_right_count_ratio:"0.191",
    aggregate_right_market_cap_ratio:"0.650",
    prior_aggregate_right_count_ratio:"0.150",
    prior_aggregate_right_market_cap_ratio:"0.600",
  }],
});
for (const text of [
  "右侧个数占比", "右侧市值占比",
  "15% → 19.1%", "60% → 65%",
  "45.9 个百分点", "结构差较前值扩大 0.9 个百分点",
  "该指标不是账户仓位或上涨概率",
]) {
  if (!html.includes(text)) throw new Error(text + "\n" + html);
}
if (html.includes("<th scope=\"col\">变化</th>")
    || html.includes("trend-industry-context-status")) {
  throw new Error("redundant change column remains\n" + html);
}
if ((html.match(/data-trend-industry-help=/g) || []).length < 4
    || !html.includes('role="tooltip"')) {
  throw new Error("accessible metric explanations missing\n" + html);
}
```

Add separate rows proving `19.1% · 基准建立中` and `未提供`.

Update `test_dashboard_compact_report_layout_contract_for_all_markets` so its
industry rows still render for CN/HK/US and no `变化` column returns.

- [ ] **Step 2: Run the focused Dashboard test and confirm failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_renders_right_side_structure_in_existing_industry_table \
  tests/test_dashboard_web.py::test_dashboard_compact_report_layout_contract_for_all_markets -q
```

Expected: FAIL because the old `右侧占比` and `变化` columns remain.

- [ ] **Step 3: Replace only the two table cells and headers**

Add JavaScript equivalents of the Python helpers:

```javascript
function trendIndustryTransition(current, prior) {
  const currentText = trendIndustryPercent(current);
  if (!currentText) return "未提供";
  const priorText = trendIndustryPercent(prior);
  return priorText ? `${priorText} → ${currentText}` : `${currentText} · 基准建立中`;
}

function trendIndustryStructureCopy(context) {
  if (!hasValue(context.aggregate_right_count_ratio)
      || !hasValue(context.aggregate_right_market_cap_ratio)) return "";
  const count = Number(context.aggregate_right_count_ratio);
  const marketCap = Number(context.aggregate_right_market_cap_ratio);
  if (!Number.isFinite(count) || !Number.isFinite(marketCap)) return "";
  const gap = (marketCap - count) * 100;
  const relation = gap > 0 ? "高于" : gap < 0 ? "低于" : "等于";
  const bias = gap > 0 ? "右侧更偏大市值成分"
    : gap < 0 ? "右侧更偏小市值成分" : "两个占比相同";
  let text = `当前右侧市值占比${relation}右侧个数占比 ${formatDisplayNumber(Math.abs(gap))} 个百分点，${bias}。`;
  const priorCount = hasValue(context.prior_aggregate_right_count_ratio)
    ? Number(context.prior_aggregate_right_count_ratio) : null;
  const priorMarketCap = hasValue(context.prior_aggregate_right_market_cap_ratio)
    ? Number(context.prior_aggregate_right_market_cap_ratio) : null;
  if (Number.isFinite(priorMarketCap)) {
    const marketCapChange = (marketCap - priorMarketCap) * 100;
    const marketCapDirection = marketCapChange > 0 ? "上升"
      : marketCapChange < 0 ? "下降" : "持平";
    text = `较前一有效交易日${marketCapDirection} ${formatDisplayNumber(Math.abs(marketCapChange))} 个百分点。${text}`;
  }
  if (Number.isFinite(priorCount) && Number.isFinite(priorMarketCap)) {
    const change = gap - (priorMarketCap - priorCount) * 100;
    const direction = change > 0 ? "扩大" : change < 0 ? "收窄" : "持平";
    text += `结构差较前值${direction} ${formatDisplayNumber(Math.abs(change))} 个百分点。`;
  }
  return `${text}该指标不是账户仓位或上涨概率。`;
}

function trendIndustryRatioChangeCopy(label, definition, current, prior) {
  if (!hasValue(current)) return "";
  let text = `${label}：${definition}。`;
  if (hasValue(prior)) {
    const change = (Number(current) - Number(prior)) * 100;
    const direction = change > 0 ? "上升" : change < 0 ? "下降" : "持平";
    text += `较前一有效交易日${direction} ${formatDisplayNumber(Math.abs(change))} 个百分点。`;
  } else {
    text += "历史基准建立中。";
  }
  return text;
}
```

Render the header labels and available values as existing-font `<button
type="button">` elements with:

```html
class="trend-industry-metric"
data-trend-industry-help="escaped deterministic explanation"
aria-expanded="false"
aria-label="escaped visible value and explanation"
```

Render one sibling `<div class="trend-industry-tooltip" role="tooltip"
aria-hidden="true" hidden></div>` after the existing table wrapper. Remove the
old status cell and `变化` header. Do not alter section order or add a card.

The `右侧个数占比` value explanation uses the same pattern: state
`右侧成分数 ÷ 行业有效成分数`, then the exact prior-day increase/decrease when
available. The `右侧市值占比` explanation uses
`右侧成分总市值 ÷ 行业有效成分总市值` plus
`trendIndustryStructureCopy(context)`. Header triggers show only the two
denominator definitions.

- [ ] **Step 4: Add one delegated tooltip controller and matching CSS**

Reuse the existing `account-holdings` event root. Add small helpers that:

```javascript
function showTrendIndustryHelp(trigger, pinned = false) {
  const tooltip = trigger.closest(".trend-industry-context")
    ?.querySelector(".trend-industry-tooltip");
  const text = trigger.dataset.trendIndustryHelp || "";
  if (!tooltip || !text) return;
  tooltip.textContent = text;
  tooltip.hidden = false;
  tooltip.setAttribute("aria-hidden", "false");
  trigger.dataset.trendIndustryHelpOpen = pinned ? "pinned" : "hover";
  trigger.setAttribute("aria-expanded", String(pinned));
  const target = trigger.getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.min(
    Math.max(12, target.left),
    window.innerWidth - box.width - 12,
  )}px`;
  tooltip.style.top = `${
    target.top >= box.height + 20
      ? target.top - box.height - 8
      : target.bottom + 8
  }px`;
}
```

Use delegated `mouseover`/`mouseout` and `focusin`/`focusout` for temporary
display. Clicking the text pins/unpins the same tooltip; clicking elsewhere or
pressing Escape closes it and restores `aria-expanded="false"`. Do not register
per-row listeners.

Reuse the existing report tokens:

```css
.trend-industry-metric {
  border: 0;
  border-bottom: 1px dashed var(--trend-info);
  background: transparent;
  color: inherit;
  cursor: help;
  font: inherit;
  font-weight: inherit;
  padding: 0 1px;
}

.trend-industry-metric:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
}

.trend-industry-tooltip {
  position: fixed;
  z-index: 20;
  max-width: min(370px, calc(100vw - 24px));
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--primary);
  color: var(--on-primary);
  box-shadow: var(--shadow);
  font-size: 13px;
  font-weight: 400;
  line-height: 1.5;
}
```

Keep the current mobile table-to-card rules; only allow the fixed tooltip to
use `left: 12px; right: 12px` below 760px.

- [ ] **Step 5: Update real-browser acceptance assertions**

Replace the old local numerator/denominator and `变化` text assertions in
`_check_frozen_trend_disciplines(...)` with aggregate-state assertions:

```python
count_text = _trend_ratio_transition(
    context.get("aggregate_right_count_ratio"),
    context.get("prior_aggregate_right_count_ratio"),
)
market_cap_text = _trend_ratio_transition(
    context.get("aggregate_right_market_cap_ratio"),
    context.get("prior_aggregate_right_market_cap_ratio"),
)
assert count_text in context_text
assert market_cap_text in context_text
```

When at least one aggregate value is present, use the real locator to hover,
focus, click, and press Escape on `.trend-industry-metric`, asserting the shared
tooltip becomes visible, contains `不是账户仓位或上涨概率` for the market-cap
value, and closes. At mobile width, also assert each button has at least a
44-by-44 CSS hit target via its surrounding table cell, not by enlarging the
underlined text itself.

Update `test_acceptance_checks_displayed_current_lifecycle_cards_and_industry_context`
with the new frozen fields and fake locator behavior.

- [ ] **Step 6: Run focused Dashboard and acceptance tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the reused-table UI**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: explain right-side structure in industry table"
```

---

### Task 4: Operator log, final gate, and exact-SHA review deployment

**Files:**
- Modify: `CHANGELOG.md`
- Verify: all files changed since `main`
- Runtime: `/tmp/open_trader_dashboard_8766.log`
- Runtime: `screen` session `open_trader_dashboard_8766`
- Runtime: `http://127.0.0.1:8766/`

**Interfaces:**
- Consumes: the complete implementation from Tasks 1-3.
- Produces: one clean final SHA with a `make acceptance` result and an exact-SHA Dashboard review deployment.

- [ ] **Step 1: Add the required dated operator log before any merge**

Add under `## 2026-07-29`:

```markdown
- Added prior-to-current right-side count and market-cap ratios to the existing
  trend-report industry table, with deterministic hover/tap explanations and no
  trading-rule changes. Verified shared CN/HK/US report coverage, legacy history
  compatibility, and desktop/mobile Dashboard behavior.
```

- [ ] **Step 2: Review the final diff and run focused checks only**

Run:

```bash
git diff --check
git diff --stat main...HEAD
.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: no whitespace errors and all focused tests PASS.

- [ ] **Step 3: Commit the changelog and freeze the candidate SHA**

```bash
git add CHANGELOG.md
git commit -m "docs: log right-side structure report"
git status --short
git rev-parse HEAD
```

Expected: clean worktree; record the returned SHA as `ACCEPTED_CANDIDATE_SHA`.

- [ ] **Step 4: Start the candidate Dashboard against real shared data**

Use explicit paths; do not copy fixtures into the linked worktree:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-report-right-side-structure && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Inspect the actual process and fresh log:

```bash
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader dashboard .*--port 8766'
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: one current process from the implementation worktree, no traceback,
and HTTP `200`. Do not run a trend controller or regenerate a report merely to
populate optional fields; a pre-change frozen report must truthfully show
`未提供`, while the next normal controller report establishes the provider
baseline.

- [ ] **Step 5: Run the final Dashboard gate exactly once**

Run only after all source and log changes are committed:

```bash
make acceptance
```

Expected: final line `PASS`. On `FAIL`, fix the failure, commit, restart the
candidate Dashboard, and rerun this step. On `BLOCKED`, stop and report the
external blocker; do not substitute fixtures, curl, screenshots, or unit tests.

- [ ] **Step 6: Redeploy the exact accepted SHA without source/data changes**

Confirm the SHA has not changed:

```bash
git status --short
git rev-parse HEAD
```

Expected: clean worktree and exactly `ACCEPTED_CANDIDATE_SHA`.

Restart the same command from Step 4, then verify:

```bash
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader dashboard .*--port 8766'
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Read the fresh `dashboard_runtime` log line and confirm its `cwd` is the
implementation worktree and `git_sha` is `ACCEPTED_CANDIDATE_SHA`.

- [ ] **Step 7: Hand off for review**

Report only:

- accepted SHA;
- exact focused-test and `make acceptance` results;
- new Dashboard PID/start time/cwd/SHA;
- fresh-log status;
- review URL `http://127.0.0.1:8766/`;
- whether the current frozen reports show real provider values or the truthful
  pre-baseline state.

Do not merge into `main` until the user approves the deployed review.
