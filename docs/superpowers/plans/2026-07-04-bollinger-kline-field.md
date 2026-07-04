# Bollinger Kline Field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed Bollinger-band display area to the dashboard `趋势 / K 线` card, with red upper-band risk, green lower-band opportunity, and neutral middle-range states.

**Architecture:** Extend the existing `technical_facts.json` schema inside each timeframe instead of adding a new artifact. Backend validation remains permissive for missing legacy `bollinger` data but validates known enum values and rejects visible Chinese text that contains trading instructions. Frontend rendering adds a dedicated Bollinger card before the existing RSI/MACD/MA rows and hides all internal enum values from the UI.

**Tech Stack:** Python 3, pytest, vanilla JavaScript dashboard helpers, CSS, Node VM tests, existing `open_trader.technical_facts` and dashboard static assets.

---

## File Structure

- Modify `src/open_trader/technical_facts.py`: add Bollinger enum constants, validate optional `timeframes[].bollinger`, reject instruction-like Chinese text in `summary_zh` and `detail_zh`, and update the LLM system prompt with the fixed schema.
- Modify `src/open_trader/dashboard_static/dashboard.js`: render a fixed Bollinger section in `klineTechnicalFactsPlugin()`, map internal statuses to Chinese labels, and keep legacy/empty Bollinger data graceful.
- Modify `src/open_trader/dashboard_static/dashboard.css`: add compact styles for the fixed Bollinger card, red/green/neutral status colors, and responsive behavior.
- Modify `tests/test_technical_facts.py`: add backend validation coverage for accepted Bollinger schema and rejected trading-instruction text.
- Modify `tests/test_dashboard_web.py`: add Node VM tests for upper-risk, lower-opportunity, neutral, and no-enum UI rendering.

## Task 1: Backend Bollinger Schema Validation

**Files:**
- Modify: `src/open_trader/technical_facts.py`
- Test: `tests/test_technical_facts.py`

- [ ] **Step 1: Write failing tests for valid and invalid Bollinger payloads**

Add imports in `tests/test_technical_facts.py`:

```python
from open_trader.technical_facts import _validate_facts
```

Add these tests near the existing validation tests:

```python
def valid_bollinger_facts() -> dict[str, object]:
    return {
        "schema_version": "open_trader.technical_facts.v1",
        "status": "present",
        "source_date": "2026-07-04",
        "market_data_as_of": "2026-07-03",
        "symbol": "US.MSFT",
        "timeframes": [
            {
                "timeframe": "daily",
                "timeframe_label": "日线",
                "current_price": "466.20",
                "bollinger": {
                    "upper": "459.13",
                    "middle": "399.62",
                    "lower": "340.11",
                    "position": "above_upper",
                    "status": "upper_risk",
                    "reference_band": "upper",
                    "reference_value": "459.13",
                    "distance_pct": "1.5%",
                    "summary_zh": "当前价格已超过日线布林带上轨",
                    "detail_zh": "价格处在布林带上沿之外，说明短线偏热。这个状态用于提醒可能接近回调区，不直接给出交易动作。",
                },
            }
        ],
    }


def test_validate_facts_accepts_fixed_bollinger_schema() -> None:
    _validate_facts(valid_bollinger_facts())


def test_validate_facts_rejects_invalid_bollinger_status() -> None:
    facts = valid_bollinger_facts()
    timeframe = facts["timeframes"][0]  # type: ignore[index]
    timeframe["bollinger"]["status"] = "buy_signal"  # type: ignore[index]

    with pytest.raises(ValueError, match="bollinger status is invalid"):
        _validate_facts(facts)


def test_validate_facts_rejects_bollinger_trading_instruction_text() -> None:
    facts = valid_bollinger_facts()
    timeframe = facts["timeframes"][0]  # type: ignore[index]
    timeframe["bollinger"]["detail_zh"] = "价格接近下轨，建议加仓。"  # type: ignore[index]

    with pytest.raises(ValueError, match="bollinger detail_zh contains trading instruction"):
        _validate_facts(facts)


def test_validate_facts_allows_missing_or_legacy_bollinger_object() -> None:
    facts = valid_bollinger_facts()
    timeframe = facts["timeframes"][0]  # type: ignore[index]
    timeframe["bollinger"] = {"upper": "459.13", "middle": "399.62", "lower": "340.11"}  # type: ignore[index]

    _validate_facts(facts)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_technical_facts.py::test_validate_facts_accepts_fixed_bollinger_schema tests/test_technical_facts.py::test_validate_facts_rejects_invalid_bollinger_status tests/test_technical_facts.py::test_validate_facts_rejects_bollinger_trading_instruction_text tests/test_technical_facts.py::test_validate_facts_allows_missing_or_legacy_bollinger_object -q
```

Expected: FAIL because `_validate_facts` does not yet validate Bollinger status or trading-instruction text.

- [ ] **Step 3: Implement backend validation and prompt update**

In `src/open_trader/technical_facts.py`, add constants near `UNKNOWN_TIMEFRAME_VALUES`:

```python
BOLLINGER_POSITIONS = {
    "above_upper",
    "near_upper",
    "middle_range",
    "near_lower",
    "below_lower",
    "unknown",
}
BOLLINGER_STATUSES = {
    "upper_risk",
    "lower_opportunity",
    "neutral",
    "unknown",
}
BOLLINGER_REFERENCE_BANDS = {"", "upper", "lower"}
BOLLINGER_VISIBLE_TEXT_FIELDS = ("summary_zh", "detail_zh")
BOLLINGER_TRADING_INSTRUCTION_PATTERN = re.compile(
    r"(?:建议买入|建议卖出|买入|卖出|加仓|减仓|下单|建仓|平仓|止盈|止损|仓位|执行)"
)
```

Replace `_validate_facts()` with:

```python
def _validate_facts(facts: dict[str, object]) -> None:
    if not isinstance(facts, dict):
        raise ValueError("technical facts must be an object")
    if facts.get("schema_version") != FACTS_SCHEMA_VERSION:
        raise ValueError("technical facts schema_version is invalid")
    if not isinstance(facts.get("status"), str) or not facts.get("status"):
        raise ValueError("technical facts status is missing")
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list):
        raise ValueError("technical facts timeframes must be a list")
    for timeframe in timeframes:
        if isinstance(timeframe, dict):
            _validate_bollinger_payload(timeframe.get("bollinger"))
```

Add helpers below `_validate_facts()`:

```python
def _validate_bollinger_payload(payload: object) -> None:
    if payload in {None, ""}:
        return
    if not isinstance(payload, dict):
        raise ValueError("bollinger must be an object")
    position = str(payload.get("position") or "").strip()
    if position and position not in BOLLINGER_POSITIONS:
        raise ValueError("bollinger position is invalid")
    status = str(payload.get("status") or "").strip()
    if status and status not in BOLLINGER_STATUSES:
        raise ValueError("bollinger status is invalid")
    reference_band = str(payload.get("reference_band") or "").strip()
    if reference_band not in BOLLINGER_REFERENCE_BANDS:
        raise ValueError("bollinger reference_band is invalid")
    for field_name in BOLLINGER_VISIBLE_TEXT_FIELDS:
        value = payload.get(field_name)
        if value in {None, ""}:
            continue
        if not isinstance(value, str):
            raise ValueError(f"bollinger {field_name} must be a string")
        if BOLLINGER_TRADING_INSTRUCTION_PATTERN.search(value):
            raise ValueError(f"bollinger {field_name} contains trading instruction")
```

Update `_technical_facts_system_prompt()` so the returned string includes:

```python
"布林带必须放在每个 timeframe 的 bollinger 对象中，字段包含 upper、middle、lower、"
"position、status、reference_band、reference_value、distance_pct、summary_zh、"
"detail_zh。position 只能使用 above_upper、near_upper、middle_range、near_lower、"
"below_lower、unknown；status 只能使用 upper_risk、lower_opportunity、neutral、"
"unknown；reference_band 只能使用 upper、lower 或空字符串。summary_zh 和 detail_zh "
"必须是中文事实提示，不得包含买入、卖出、加仓、减仓、下单、仓位等交易指令。"
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit backend validation**

```bash
git add src/open_trader/technical_facts.py tests/test_technical_facts.py
git commit -m "feat: validate bollinger technical facts"
```

## Task 2: Dashboard Bollinger Rendering

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing dashboard JS tests**

Add this test in `tests/test_dashboard_web.py` near `test_dashboard_renders_usable_kline_technical_facts_with_timeframe_labels`:

```python
def test_dashboard_renders_fixed_bollinger_card_without_internal_enums() -> None:
    script = r'''
const holding = {
  technical_facts: {
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {message: "日线数据截至 2026-07-03"},
    facts: {
      timeframes: [{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "466.20",
        bollinger: {
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "above_upper",
          status: "upper_risk",
          reference_band: "upper",
          reference_value: "459.13",
          distance_pct: "1.5%",
          summary_zh: "当前价格已超过日线布林带上轨",
          detail_zh: "价格处在布林带上沿之外，说明短线偏热。",
        },
        rsi: {value: "56.88"},
        macd: {crossover: "金叉后延续"},
        moving_averages: {summary: "价格在主要均线上方"},
      }],
    },
  },
};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "布林带" in html
    assert "回调风险升高" in html
    assert "当前价格已超过日线布林带上轨" in html
    assert "当前价" in html
    assert "上轨" in html
    assert "偏离幅度" in html
    assert "technical-bollinger-card upper-risk" in html
    assert "upper_risk" not in html
    assert "above_upper" not in html
```

Add parameterized coverage:

```python
@pytest.mark.parametrize(
    ("status", "expected_label", "expected_class"),
    [
        ("lower_opportunity", "低位机会区域", "lower-opportunity"),
        ("neutral", "中性区间", "neutral"),
        ("unknown", "布林带数据缺失", "unknown"),
    ],
)
def test_dashboard_renders_bollinger_status_variants(
    status: str,
    expected_label: str,
    expected_class: str,
) -> None:
    script = f'''
const holding = {{
  technical_facts: {{
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {{message: "日线数据截至 2026-07-03"}},
    facts: {{
      timeframes: [{{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "388.20",
        bollinger: {{
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "middle_range",
          status: "{status}",
          reference_band: "",
          reference_value: "",
          distance_pct: "",
          summary_zh: "",
          detail_zh: "",
        }},
      }}],
    }},
  }},
}};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert expected_label in html
    assert f"technical-bollinger-card {expected_class}" in html
    assert status not in html
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_fixed_bollinger_card_without_internal_enums tests/test_dashboard_web.py::test_dashboard_renders_bollinger_status_variants -q
```

Expected: FAIL because the dashboard does not render a dedicated Bollinger card yet.

- [ ] **Step 3: Implement dashboard JS rendering**

In `src/open_trader/dashboard_static/dashboard.js`, change `klineTechnicalFactsPlugin()` so usable detail builds a Bollinger section:

```javascript
const timeframes = detail.facts && Array.isArray(detail.facts.timeframes)
  ? detail.facts.timeframes
  : [];
const rows = timeframes.flatMap((timeframe) => technicalFactRowsForTimeframe(timeframe));
const bollingerHtml = renderBollingerSection(timeframes);
...
bodyHtml: `${bollingerHtml}${renderTechnicalFactRows(rows)}`,
```

In `technicalFactRowsForTimeframe(timeframe)`, do not add Bollinger as a normal fact row. Keep current RSI/MACD/trend/ATR/support/resistance/MA rows.

Add these helpers near the technical fact helpers:

```javascript
function renderBollingerSection(timeframes) {
  const timeframesWithObjects = Array.isArray(timeframes)
    ? timeframes.filter((timeframe) => timeframe && typeof timeframe === "object")
    : [];
  const preferred = timeframesWithObjects.find((timeframe) => {
    const key = String(timeframe.timeframe || timeframe.period || "").toLowerCase();
    return key === "daily" || key === "day" || key === "1d";
  }) || timeframesWithObjects[0];
  if (!preferred) {
    return renderBollingerCard({}, "", "");
  }
  const bollinger = preferred.bollinger && typeof preferred.bollinger === "object"
    ? preferred.bollinger
    : {};
  return renderBollingerCard(bollinger, preferred.current_price, timeframeLabel(preferred));
}

function renderBollingerCard(bollinger, currentPrice, timeframe) {
  const status = bollingerStatus(bollinger);
  const statusMeta = bollingerStatusMeta(status);
  const summary = firstPresent(
    bollinger.summary_zh,
    defaultBollingerSummary(status, timeframe),
  );
  const detail = firstPresent(
    bollinger.detail_zh,
    defaultBollingerDetail(status),
  );
  return `
    <section class="technical-bollinger-card ${escapeHtml(statusMeta.className)}">
      <div class="technical-bollinger-header">
        <span>${escapeHtml(timeframe ? `${timeframe}布林带` : "布林带")}</span>
        <strong>${escapeHtml(statusMeta.label)}</strong>
      </div>
      <div class="technical-bollinger-copy">
        <strong>${escapeHtml(summary)}</strong>
        <p>${escapeHtml(detail)}</p>
      </div>
      ${renderBollingerBand(bollinger, currentPrice)}
      ${renderBollingerMetrics(bollinger, currentPrice, status)}
    </section>
  `;
}

function bollingerStatus(bollinger) {
  const status = String(bollinger && bollinger.status ? bollinger.status : "").trim();
  if (["upper_risk", "lower_opportunity", "neutral", "unknown"].includes(status)) {
    return status;
  }
  return "unknown";
}

function bollingerStatusMeta(status) {
  const map = {
    upper_risk: { label: "回调风险升高", className: "upper-risk" },
    lower_opportunity: { label: "低位机会区域", className: "lower-opportunity" },
    neutral: { label: "中性区间", className: "neutral" },
    unknown: { label: "布林带数据缺失", className: "unknown" },
  };
  return map[status] || map.unknown;
}

function defaultBollingerSummary(status, timeframe) {
  const label = timeframe || "日线";
  if (status === "upper_risk") {
    return `当前价格贴近或超过${label}布林带上轨`;
  }
  if (status === "lower_opportunity") {
    return `当前价格接近${label}布林带下轨`;
  }
  if (status === "neutral") {
    return `当前价格位于${label}布林带中性区间`;
  }
  return "布林带数据缺失";
}

function defaultBollingerDetail(status) {
  if (status === "upper_risk") {
    return "价格靠近布林带上沿，说明短线偏热。这个状态用于提醒可能接近回调区，不直接给出交易动作。";
  }
  if (status === "lower_opportunity") {
    return "价格靠近布林带下沿，说明进入低位观察区。这个状态用于提醒可能出现低位机会，不直接给出交易动作。";
  }
  if (status === "neutral") {
    return "价格没有贴近上轨或下轨，布林带暂未给出需要特别关注的位置提醒。";
  }
  return "当前报告没有提供完整布林带事实。";
}

function renderBollingerBand(bollinger, currentPrice) {
  const lower = indicatorValue(bollinger.lower);
  const middle = indicatorValue(bollinger.middle);
  const upper = indicatorValue(bollinger.upper);
  const markerStyle = bollingerMarkerStyle(bollinger, currentPrice);
  return `
    <div class="technical-bollinger-band">
      <div class="technical-bollinger-track">
        <span class="technical-bollinger-marker" style="${escapeHtml(markerStyle)}"></span>
      </div>
      <div class="technical-bollinger-labels">
        <span>下轨 ${escapeHtml(formatPlain(lower || "缺失"))}</span>
        <span>中轨 ${escapeHtml(formatPlain(middle || "缺失"))}</span>
        <span>上轨 ${escapeHtml(formatPlain(upper || "缺失"))}</span>
      </div>
    </div>
  `;
}

function bollingerMarkerStyle(bollinger, currentPrice) {
  const lower = numericValue(bollinger.lower);
  const upper = numericValue(bollinger.upper);
  const current = numericValue(currentPrice);
  if (lower === null || upper === null || current === null || upper <= lower) {
    return "left: 50%";
  }
  const raw = ((current - lower) / (upper - lower)) * 100;
  const clamped = Math.max(2, Math.min(98, raw));
  return `left: ${clamped.toFixed(1)}%`;
}

function renderBollingerMetrics(bollinger, currentPrice, status) {
  const referenceLabel = bollingerReferenceLabel(bollinger, status);
  const referenceValue = firstPresent(bollinger.reference_value, bollingerReferenceValue(bollinger, status));
  const distance = firstPresent(bollinger.distance_pct, bollingerDistanceFallback(status));
  return renderDecisionFactRows([
    { label: "当前价", value: currentPrice },
    { label: referenceLabel, value: referenceValue },
    { label: "偏离幅度", value: distance },
  ]);
}

function bollingerReferenceLabel(bollinger, status) {
  if (status === "upper_risk") {
    return "上轨";
  }
  if (status === "lower_opportunity") {
    return "下轨";
  }
  if (status === "neutral") {
    return "中轨";
  }
  const referenceBand = String(bollinger.reference_band || "");
  if (referenceBand === "upper") {
    return "上轨";
  }
  if (referenceBand === "lower") {
    return "下轨";
  }
  return "参考轨道";
}

function bollingerReferenceValue(bollinger, status) {
  if (status === "upper_risk") {
    return bollinger.upper;
  }
  if (status === "lower_opportunity") {
    return bollinger.lower;
  }
  if (status === "neutral") {
    return bollinger.middle;
  }
  return firstPresent(bollinger.upper, bollinger.lower, bollinger.middle);
}

function bollingerDistanceFallback(status) {
  if (status === "neutral") {
    return "中性区间";
  }
  return "缺失";
}

function numericValue(value) {
  if (!hasValue(value)) {
    return null;
  }
  const numeric = Number.parseFloat(String(indicatorValue(value)).replace(/[%,$]/g, ""));
  return Number.isFinite(numeric) ? numeric : null;
}
```

- [ ] **Step 4: Implement CSS**

In `src/open_trader/dashboard_static/dashboard.css`, add near `.technical-fact-grid`:

```css
.technical-bollinger-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 10px;
  margin-bottom: 10px;
  padding: 12px;
}

.technical-bollinger-card.upper-risk {
  background: #fff0ee;
  border-color: #e4b4b0;
}

.technical-bollinger-card.lower-opportunity {
  background: #e8f5ee;
  border-color: #b9ddc9;
}

.technical-bollinger-card.neutral,
.technical-bollinger-card.unknown {
  background: var(--surface-soft);
}

.technical-bollinger-header {
  align-items: center;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  min-width: 0;
}

.technical-bollinger-header span,
.technical-bollinger-header strong {
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 12px;
  font-weight: 800;
  min-height: 26px;
  padding: 4px 9px;
}

.technical-bollinger-card.upper-risk .technical-bollinger-header span,
.technical-bollinger-card.upper-risk .technical-bollinger-header strong {
  background: #fff7f5;
  border-color: #e4b4b0;
  color: var(--danger);
}

.technical-bollinger-card.lower-opportunity .technical-bollinger-header span,
.technical-bollinger-card.lower-opportunity .technical-bollinger-header strong {
  background: #f4fbf7;
  border-color: #b9ddc9;
  color: var(--ok);
}

.technical-bollinger-copy {
  display: grid;
  gap: 5px;
}

.technical-bollinger-copy strong {
  font-size: 15px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.technical-bollinger-copy p {
  color: var(--muted);
  font-size: 13px;
  font-weight: 700;
  line-height: 1.5;
  margin: 0;
}

.technical-bollinger-card.upper-risk .technical-bollinger-copy p {
  color: #6b3434;
}

.technical-bollinger-card.lower-opportunity .technical-bollinger-copy p {
  color: #2f604a;
}

.technical-bollinger-band {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 8px;
  padding: 10px;
}

.technical-bollinger-track {
  background: linear-gradient(90deg, #dcece4 0%, #edf1ef 50%, #f3c8c8 100%);
  border-radius: 999px;
  height: 10px;
  position: relative;
}

.technical-bollinger-marker {
  background: var(--danger);
  border: 2px solid var(--surface);
  border-radius: 999px;
  box-shadow: 0 0 0 1px rgba(168, 61, 61, 0.28);
  height: 18px;
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 18px;
}

.technical-bollinger-card.lower-opportunity .technical-bollinger-marker {
  background: var(--ok);
  box-shadow: 0 0 0 1px rgba(33, 110, 70, 0.28);
}

.technical-bollinger-card.neutral .technical-bollinger-marker,
.technical-bollinger-card.unknown .technical-bollinger-marker {
  background: var(--muted);
  box-shadow: 0 0 0 1px rgba(111, 118, 109, 0.22);
}

.technical-bollinger-labels {
  color: var(--muted);
  display: grid;
  font-size: 12px;
  font-weight: 800;
  gap: 8px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.technical-bollinger-labels span {
  overflow-wrap: anywhere;
}

.technical-bollinger-labels span:nth-child(2) {
  text-align: center;
}

.technical-bollinger-labels span:nth-child(3) {
  text-align: right;
}
```

Extend existing mobile media rules where `.technical-fact-grid` becomes one column:

```css
.technical-bollinger-header,
.technical-bollinger-labels {
  grid-template-columns: 1fr;
}
```

Use a flex override for `.technical-bollinger-header` on mobile:

```css
.technical-bollinger-header {
  align-items: stretch;
  flex-direction: column;
}
```

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_usable_kline_technical_facts_with_timeframe_labels tests/test_dashboard_web.py::test_dashboard_renders_fixed_bollinger_card_without_internal_enums tests/test_dashboard_web.py::test_dashboard_renders_bollinger_status_variants -q
```

Expected: PASS.

- [ ] **Step 6: Commit dashboard rendering**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render bollinger kline card"
```

## Task 3: Documentation And Verification

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update Chinese README**

In `README.zh-CN.md`, in the dashboard technical facts paragraph, replace the sentence about reading `technical_facts.json` with:

```markdown
仪表盘还会在可用时读取 `technical_facts.json`。标的详情会显示技术事实的生成日期
和底层行情数据日期；`趋势 / K 线` 卡片固定展示日线布林带位置，贴近或超过上轨时标红提示
`回调风险升高`，接近下轨时标绿提示 `低位机会区域`，中轨附近按正常颜色展示。
如果文件缺失、记录缺失、来源 hash 已过期、抽取失败或周期信息不完整，
会把该记录标记为不可用，不会把过期技术事实当作当前数据展示。
```

- [ ] **Step 2: Update changelog**

Add under `## Unreleased` or the latest 2026-07-04 section in `CHANGELOG.md`:

```markdown
- Added a fixed Bollinger-band display in the dashboard K-line card, with red upper-band risk, green lower-band opportunity, and neutral middle-range states.
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_technical_facts.py tests/test_dashboard_web.py::test_dashboard_renders_usable_kline_technical_facts_with_timeframe_labels tests/test_dashboard_web.py::test_dashboard_renders_fixed_bollinger_card_without_internal_enums tests/test_dashboard_web.py::test_dashboard_renders_bollinger_status_variants -q
```

Expected: PASS.

- [ ] **Step 4: Run broader dashboard tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 5: Start or restart local dashboard**

Check the listener:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

If a stale process is serving the port, stop that PID. Start the dashboard:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --poll-seconds 5 \
  --host 127.0.0.1 \
  --port 8766
```

Expected: dashboard serves `http://127.0.0.1:8766`.

- [ ] **Step 6: Verify UI with Playwright**

Use Playwright against `http://127.0.0.1:8766`:

```javascript
await page.goto("http://127.0.0.1:8766");
await page.getByText("交易决策").first().click();
await page.getByText("趋势 / K 线").waitFor();
await page.getByText("布林带").waitFor();
```

Check desktop and mobile screenshots:

```javascript
await page.setViewportSize({ width: 1280, height: 900 });
await page.screenshot({ path: "reports/bollinger-kline-desktop.png", fullPage: true });
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: "reports/bollinger-kline-mobile.png", fullPage: true });
```

Expected: no overlapping text; Bollinger section appears in the K-line card. If live data lacks Bollinger facts, confirm the card shows `布林带数据缺失` instead of failing.

- [ ] **Step 7: Commit docs and verification notes**

```bash
git add README.zh-CN.md CHANGELOG.md
git commit -m "docs: document bollinger kline display"
```

## Self-Review

- Spec coverage: backend schema, Chinese-only visible text, no trading actions, fixed dashboard display, red/green/neutral states, legacy fallback, and tests are covered by Tasks 1-3.
- Placeholder scan: no TBD, TODO, or implementation-later placeholders remain.
- Type consistency: plan uses `bollinger.position`, `bollinger.status`, `reference_band`, `reference_value`, `distance_pct`, `summary_zh`, and `detail_zh` consistently across backend, frontend, and tests.
