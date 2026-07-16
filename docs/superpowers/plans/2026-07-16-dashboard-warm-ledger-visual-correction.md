# Dashboard Warm Ledger Visual Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved A warm-ledger palette across every Dashboard surface, align the trend report with the 1600px Dashboard shell, and make the exact visual contract a permanent Playwright-backed acceptance requirement.

**Architecture:** Keep the existing static HTML and JavaScript unchanged. Update the shared CSS tokens and the two trend-report layout rules, then extend the existing Python Playwright acceptance flow so it checks real computed styles, wide/desktop/mobile geometry, overflow behavior, and saved screenshots on every future `make acceptance` run.

**Tech Stack:** Vanilla CSS, existing HTML/JavaScript Dashboard, Python 3.12 + pytest, Playwright sync API with installed Chrome, existing TypeScript Playwright fixture tests, existing `screen` deployment.

## Global Constraints

- Exact colors: background `#F7F5F1`, surface `#FFFEFA`, soft surface `#F2EEE7`, text `#201D18`, muted `#746E64`, accent/focus `#8B5E34`, border `#D8D2C8`, primary card `#24211D`, danger/profit `#B42318`, success/loss `#2F855A`.
- Apply those shared tokens to the homepage, trend report, backtest, Kelly Lab, trading decisions, symbol detail, and research chat.
- Keep broker colors only on tabs, thin card markers, and small status marks.
- Preserve every data field, label, filter, navigation path, API contract, and trading behavior.
- Keep the Dashboard shell at `max-width: 1600px`; the trend report fills its content area and aligns with the header and holdings panel.
- Desktop A-share buy tables scroll only inside their stage; mobile at `760px` and below keeps the existing card layout with no page-level horizontal overflow.
- Keep system fonts, tabular numerals, 44px mobile targets, visible focus, WCAG AA contrast, and reduced-motion behavior.
- Add no dependency, font, icon library, theme switcher, chart, build step, or screenshot pixel-diff system.
- Do not run `make acceptance` during Tasks 1–3. Run it only in Task 4 as the final gate.
- A Dashboard task is complete only after `make acceptance` returns `PASS`, the accepted SHA is redeployed, and PID/CWD/SHA/log/HTTP checks pass.

## File Structure

- `src/open_trader/dashboard_static/dashboard.css`: owns the exact palette, shared surface styling, report width, desktop buy-table overflow, and mobile reset.
- `tests/test_dashboard_web.py`: protects the static CSS token and responsive-rule contract without launching a browser.
- `tests/e2e/dashboard-warm-ledger.spec.ts`: gives fast Playwright feedback against the fixture server while developing; it verifies the exact colors and report layout before the final real-data gate.
- `src/open_trader/dashboard_acceptance.py`: owns the permanent real-browser acceptance assertions, the `1920 / 1440 / 375` viewport matrix, and screenshot capture.
- `tests/test_dashboard_acceptance.py`: protects the permanent acceptance helper and viewport orchestration with fakes; no live browser is required for these unit tests.

---

### Task 1: Lock and apply the exact A palette

**Files:**
- Modify: `tests/test_dashboard_web.py:43-132`
- Modify: `tests/e2e/dashboard-warm-ledger.spec.ts:1-230`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1-20, 3743-3752`

**Interfaces:**
- Consumes: existing CSS custom properties and the existing `expectWarmSurface()` Playwright helper.
- Produces: one exact global token set consumed by every existing Dashboard selector; no new runtime interface.

- [ ] **Step 1: Change the static assertions to the approved exact tokens**

Replace the old token loop and P/L assertions in `test_dashboard_warm_ledger_theme_and_broker_accents()` with:

```python
    for token in (
        "--bg: #f7f5f1;", "--surface: #fffefa;",
        "--surface-soft: #f2eee7;", "--text: #201d18;",
        "--muted: #746e64;", "--accent: #8b5e34;",
        "--line: #d8d2c8;", "--primary: #24211d;",
        "--success: #2f855a;", "--danger: #b42318;",
    ):
        assert token in css
```

Replace the P/L checks with:

```python
    assert ".pnl-profit { color: var(--danger);" in css
    assert ".pnl-loss { color: var(--success);" in css
```

Replace `test_dashboard_muted_text_meets_aa_on_soft_surface()` with:

```python
def test_dashboard_muted_text_meets_aa_on_approved_soft_surface() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "--muted: #746e64;" in css
    assert "--surface-soft: #f2eee7;" in css
```

- [ ] **Step 2: Change fixture-browser expectations to the same exact palette**

Add these constants after the Playwright import:

```typescript
const warmLedger = {
  bg: '#F7F5F1',
  surface: '#FFFEFA',
  soft: '#F2EEE7',
  text: '#201D18',
  muted: '#746E64',
  accent: '#8B5E34',
  line: '#D8D2C8',
  primary: '#24211D',
  danger: '#B42318',
  success: '#2F855A',
} as const;

const rgb = {
  bg: 'rgb(247, 245, 241)',
  surface: 'rgb(255, 254, 250)',
  text: 'rgb(32, 29, 24)',
  accent: 'rgb(139, 94, 52)',
  line: 'rgb(216, 210, 200)',
  primary: 'rgb(36, 33, 29)',
  danger: 'rgb(180, 35, 24)',
  success: 'rgb(47, 133, 90)',
} as const;
```

Change `expectWarmSurface()` to:

```typescript
async function expectWarmSurface(page: Page, selector: string) {
  const surface = page.locator(selector);
  await expect(surface).toBeVisible();
  await expect(surface).toHaveCSS('background-color', rgb.surface);
  await expect(surface).toHaveCSS('border-top-color', rgb.line);
  await expect(surface).toHaveCSS('border-top-width', '1px');
}
```

Add this test after the `brokers` constant:

```typescript
test('renders the exact approved warm-ledger contract', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');

  const tokens = await page.evaluate(() => {
    const styles = getComputedStyle(document.documentElement);
    return Object.fromEntries([
      '--bg', '--surface', '--surface-soft', '--text', '--muted',
      '--accent', '--line', '--primary', '--danger', '--success',
    ].map((name) => [name, styles.getPropertyValue(name).trim().toUpperCase()]));
  });
  expect(tokens).toEqual({
    '--bg': warmLedger.bg,
    '--surface': warmLedger.surface,
    '--surface-soft': warmLedger.soft,
    '--text': warmLedger.text,
    '--muted': warmLedger.muted,
    '--accent': warmLedger.accent,
    '--line': warmLedger.line,
    '--primary': warmLedger.primary,
    '--danger': warmLedger.danger,
    '--success': warmLedger.success,
  });
  await expect(page.locator('body')).toHaveCSS('background-color', rgb.bg);
  await expect(page.locator('body')).toHaveCSS('color', rgb.text);
  await expect(page.locator('#refresh-quotes')).toHaveCSS('background-color', rgb.accent);
  await expect(page.locator('.current-view-card')).toHaveCSS('background-color', rgb.primary);
  await expectWarmSurface(page, '.header-brand-panel');
  await expectWarmSurface(page, '.holdings-panel');
});
```

Update existing P/L, focus, and mobile inner-surface expectations to `rgb.danger`, `rgb.success`, `rgb.accent`, `rgb.surface`, and `rgb.line` respectively.

- [ ] **Step 3: Run the focused tests and confirm they fail against the old colors**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_warm_ledger_theme_and_broker_accents tests/test_dashboard_web.py::test_dashboard_muted_text_meets_aa_on_approved_soft_surface -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
```

Expected: pytest fails on `--bg: #f7f5f1`; Playwright fails because the browser still renders the old white/amber values.

- [ ] **Step 4: Replace only the shared palette and P/L literals**

Replace the root token block with:

```css
:root {
  color-scheme: light;
  --bg: #f7f5f1;
  --surface: #fffefa;
  --surface-soft: #f2eee7;
  --text: #201d18;
  --muted: #746e64;
  --accent: #8b5e34;
  --line: #d8d2c8;
  --primary: #24211d;
  --success: #2f855a;
  --danger: #b42318;
  --shadow: 0 8px 30px rgba(68, 55, 38, 0.06);
  --accent-strong: var(--accent);
  --border: var(--line);
  --ok: var(--success);
  --on-primary: #ffffff;
  --panel-soft: var(--surface-soft);
  --warning: var(--accent);
}
```

Replace the P/L rules with:

```css
.pnl-profit { color: var(--danger); font-weight: 800; }
.pnl-loss { color: var(--success); font-weight: 800; }
```

Do not alter the four broker accent values.

- [ ] **Step 5: Re-run the focused palette checks**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_warm_ledger_theme_and_broker_accents tests/test_dashboard_web.py::test_dashboard_muted_text_meets_aa_on_approved_soft_surface -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
```

Expected: both commands pass; Playwright reports all `dashboard-warm-ledger.spec.ts` tests passing.

- [ ] **Step 6: Commit the exact palette**

```bash
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/e2e/dashboard-warm-ledger.spec.ts
git commit -m "style: apply exact warm ledger palette"
```

---

### Task 2: Align the report and preserve readable wide tables

**Files:**
- Modify: `tests/e2e/dashboard-warm-ledger.spec.ts:6-40` and append focused layout tests
- Modify: `tests/test_dashboard_web.py` near responsive CSS assertions
- Modify: `src/open_trader/dashboard_static/dashboard.css:1460-1470, 1640-1675, 3850-3915`

**Interfaces:**
- Consumes: existing `.dashboard-shell`, `.dashboard-header`, `.trend-report-workspace`, `.cn-trend-buy`, `.cn-trend-table`, and `.cn-trend-card` elements.
- Produces: CSS-only report alignment and internal scroll behavior; no DOM or JavaScript change.

- [ ] **Step 1: Add an actionable Eastmoney report to the Playwright fixture**

Add this `eastmoney` entry beside the existing `futu` report inside `installLedgerFixture()`:

```typescript
      eastmoney: {
        available: true,
        broker_label: '东方财富',
        market_label: 'A股',
        report_date: '2026-07-16',
        data_date: '2026-07-15',
        generated_at: '2026-07-16T11:07:21+08:00',
        account_status: '账户数据非实时，执行前核对现金与持仓',
        buy_window: '09:30–10:00',
        counts: { sell: 0, buy: 1, hold: 0, review: 0 },
        sell_actions: [],
        review_actions: [],
        hold_actions: [],
        buy_actions: [{
          symbol: '600519', name: '贵州茅台', filter_price: '1501.00',
          close: '1500.00', temperature_prev: '温', temperature_curr: '热',
          phase: '小暑', strength: '97.7', industry: '白酒',
          industry_temperature: '热', market_cap: '19000', amount: '35',
          target_weight: '0.04', target_amount: '40000',
          estimated_shares: '26', estimated_initial_line: '1425.00',
        }],
        audit: {
          candidates: [], excluded: {}, industry_concentration: [],
          data_sources: ['fixture'],
        },
      },
```

- [ ] **Step 2: Add desktop alignment and internal-scroll Playwright checks**

Append:

```typescript
test('aligns the A-share report with the 1600px shell and scrolls only the buy table', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await installLedgerFixture(page);
  await page.goto('/');
  await page.getByRole('tab', { name: /东方财富/ }).click();
  await page.getByRole('button', { name: '当天趋势报告' }).click();

  const geometry = await page.evaluate(() => {
    const shell = document.querySelector('.dashboard-shell')!.getBoundingClientRect();
    const header = document.querySelector('.dashboard-header')!.getBoundingClientRect();
    const report = document.querySelector('#trend-report-workspace')!.getBoundingClientRect();
    const stage = document.querySelector('.cn-trend-buy')!;
    const stageStyle = getComputedStyle(stage);
    return {
      shellWidth: shell.width,
      headerLeft: header.left,
      headerRight: header.right,
      reportLeft: report.left,
      reportRight: report.right,
      pageFits: document.documentElement.scrollWidth <= window.innerWidth,
      stageClientWidth: stage.clientWidth,
      stageScrollWidth: stage.scrollWidth,
      overflowX: stageStyle.overflowX,
    };
  });
  expect(geometry.shellWidth).toBeCloseTo(1600, 0);
  expect(geometry.reportLeft).toBeCloseTo(geometry.headerLeft, 0);
  expect(geometry.reportRight).toBeCloseTo(geometry.headerRight, 0);
  expect(geometry.pageFits).toBe(true);
  expect(geometry.overflowX).toBe('auto');
  expect(geometry.stageScrollWidth).toBeGreaterThan(geometry.stageClientWidth);
});
```

Append the mobile counterpart:

```typescript
test('keeps the A-share report card-based with no page overflow on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 844 });
  await installLedgerFixture(page);
  await page.goto('/');
  await page.getByRole('tab', { name: /东方财富/ }).click();
  await page.getByRole('button', { name: '当天趋势报告' }).click();

  await expect(page.locator('.cn-trend-buy')).toHaveCSS('overflow-x', 'hidden');
  await expect(page.locator('.cn-trend-table thead')).toBeHidden();
  await expect(page.locator('.cn-trend-buy .cn-trend-card')).toHaveCSS('display', 'grid');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
```

- [ ] **Step 3: Add static CSS contract assertions**

Add to `test_dashboard_command_center_css_keeps_accessible_responsive_states()`:

```python
    assert ".trend-report-workspace" in css
    report_css = css.split(".trend-report-workspace {", 1)[1].split("}", 1)[0]
    assert "max-width: none;" in report_css
    assert ".cn-trend-buy {\n  overflow-x: auto;\n}" in css
    assert ".cn-trend-buy .cn-trend-table" in css
    assert "min-width: 1600px;" in css
    assert ".cn-trend-buy {\n    overflow-x: hidden;\n  }" in mobile
    assert "min-width: 0;" in mobile
```

- [ ] **Step 4: Run the layout checks and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_command_center_css_keeps_accessible_responsive_states -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
```

Expected: pytest fails because the report still has `max-width: 1180px`; Playwright fails alignment and buy-stage overflow assertions.

- [ ] **Step 5: Apply the minimal CSS layout change**

Change only the report width declaration:

```css
.trend-report-workspace {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  margin: 0 auto;
  max-width: none;
  padding: 22px;
  width: 100%;
}
```

Add immediately after `.cn-trend-buy`:

```css
.cn-trend-buy {
  border-left-color: var(--ok);
  overflow-x: auto;
}

.cn-trend-buy .cn-trend-table {
  min-width: 1600px;
  table-layout: auto;
}

.cn-trend-buy .cn-trend-table th,
.cn-trend-buy .cn-trend-table td {
  overflow-wrap: normal;
  white-space: nowrap;
}
```

Replace the existing one-line `.cn-trend-buy { border-left-color: var(--ok); }` rule rather than duplicating it.

Inside `@media (max-width: 760px)`, add before the existing `.cn-trend-table` mobile rule:

```css
  .cn-trend-buy {
    overflow-x: hidden;
  }

  .cn-trend-buy .cn-trend-table {
    min-width: 0;
  }

  .cn-trend-buy .cn-trend-table th,
  .cn-trend-buy .cn-trend-table td {
    overflow-wrap: anywhere;
    white-space: normal;
  }
```

- [ ] **Step 6: Re-run focused layout verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_command_center_css_keeps_accessible_responsive_states -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
```

Expected: both commands pass; the 1920px test reports a 1600px shell, aligned content edges, stage overflow, and no page overflow; the 375px test reports cards and no page overflow.

- [ ] **Step 7: Commit the report layout**

```bash
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/e2e/dashboard-warm-ledger.spec.ts
git commit -m "style: widen trend report workspace"
```

---

### Task 3: Make the visual contract permanent in `make acceptance`

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:1-35, 736-910, 1014-1102`
- Modify: `tests/test_dashboard_acceptance.py:1313-1480` and add focused helper tests

**Interfaces:**
- Consumes: existing `_browser_check(url, expected_cn, payload, reports_dir)` and `_check_account_holdings(page, payload, reports_dir=...)`.
- Produces: `_check_visual_contract(page) -> None`, `_check_open_report_layout(page, workspace, broker) -> None`, the viewport matrix `wide_desktop / desktop / mobile`, and screenshots under `/tmp/open_trader_dashboard_acceptance/`.

- [ ] **Step 1: Add failing unit tests for exact computed styles and geometry**

Add these tests before the existing browser orchestration test:

```python
def visual_contract_page(*, accent: str = "#8B5E34") -> object:
    expected = dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
    expected["--accent"] = accent

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def count(self) -> int:
            return 1

        def focus(self) -> None:
            pass

        def evaluate(self, expression: str) -> dict[str, str]:
            if "outlineColor" in expression:
                return {
                    "outlineColor": "rgb(139, 94, 52)",
                    "outlineStyle": "solid", "outlineWidth": "3px",
                }
            if self.selector == "body":
                return {"backgroundColor": "rgb(247, 245, 241)", "color": "rgb(32, 29, 24)"}
            if self.selector == "#refresh-quotes":
                return {"backgroundColor": "rgb(139, 94, 52)", "borderTopColor": "rgb(139, 94, 52)"}
            if self.selector == ".current-view-card":
                return {"backgroundColor": "rgb(36, 33, 29)", "borderTopColor": "rgb(36, 33, 29)"}
            return {"backgroundColor": "rgb(255, 254, 250)", "borderTopColor": "rgb(216, 210, 200)"}

    class Page:
        def evaluate(
            self, expression: str, names: list[str] | None = None
        ) -> dict[str, str]:
            assert names == list(dashboard_acceptance.WARM_LEDGER_TOKENS)
            return expected

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    return Page()


def test_acceptance_visual_contract_accepts_exact_warm_ledger() -> None:
    dashboard_acceptance._check_visual_contract(visual_contract_page())


def test_acceptance_visual_contract_rejects_palette_drift() -> None:
    with pytest.raises(AssertionError, match="--accent"):
        dashboard_acceptance._check_visual_contract(
            visual_contract_page(accent="#A16207")
        )
```

Add a geometry test around a small fake `Page.evaluate()` result:

```python
def test_acceptance_open_report_layout_requires_aligned_wide_shell_and_table_scroll() -> None:
    class Stage:
        def evaluate(self, expression: str) -> dict[str, object]:
            return {"clientWidth": 1500, "scrollWidth": 1600, "overflowX": "auto"}

        def count(self) -> int:
            return 1

    class Workspace:
        def locator(self, selector: str) -> Stage:
            assert selector == ".cn-trend-buy"
            return Stage()

    class Page:
        viewport_size = {"width": 1920, "height": 1080}

        def evaluate(self, expression: str) -> dict[str, float]:
            return {
                "shellWidth": 1600, "headerLeft": 176, "headerRight": 1744,
                "reportLeft": 176, "reportRight": 1744,
            }

    dashboard_acceptance._check_open_report_layout(Page(), Workspace(), "eastmoney")
```

- [ ] **Step 2: Run the new helper tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'visual_contract or open_report_layout'
```

Expected: FAIL because `WARM_LEDGER_TOKENS`, `_check_visual_contract`, and `_check_open_report_layout` do not exist.

- [ ] **Step 3: Add the permanent constants and visual helper**

Add near the acceptance constants:

```python
WARM_LEDGER_TOKENS = {
    "--bg": "#F7F5F1",
    "--surface": "#FFFEFA",
    "--surface-soft": "#F2EEE7",
    "--text": "#201D18",
    "--muted": "#746E64",
    "--accent": "#8B5E34",
    "--line": "#D8D2C8",
    "--primary": "#24211D",
    "--danger": "#B42318",
    "--success": "#2F855A",
}
ACCEPTANCE_SCREENSHOT_DIR = Path("/tmp/open_trader_dashboard_acceptance")
```

Add before `_check_page_safety()`:

```python
def _check_visual_contract(page: Any) -> None:
    names = list(WARM_LEDGER_TOKENS)
    actual = page.evaluate(
        "names => { const styles = getComputedStyle(document.documentElement); "
        "return Object.fromEntries(names.map(name => "
        "[name, styles.getPropertyValue(name).trim().toUpperCase()])); }",
        names,
    )
    assert actual == WARM_LEDGER_TOKENS, (
        f"Dashboard A 色板漂移：{actual}"
    )

    expected = {
        "body": {"backgroundColor": "rgb(247, 245, 241)", "color": "rgb(32, 29, 24)"},
        "#refresh-quotes": {"backgroundColor": "rgb(139, 94, 52)", "borderTopColor": "rgb(139, 94, 52)"},
        ".current-view-card": {"backgroundColor": "rgb(36, 33, 29)", "borderTopColor": "rgb(36, 33, 29)"},
    }
    surface = {"backgroundColor": "rgb(255, 254, 250)", "borderTopColor": "rgb(216, 210, 200)"}
    for selector in (
        ".header-brand-panel", ".header-assets-panel", ".header-source-panel",
        ".holdings-panel", ".kelly-lab-panel", ".trend-report-workspace",
        ".backtest-workspace", ".symbol-detail-panel", ".research-chat-modal",
    ):
        expected[selector] = surface
    expression = (
        "element => { const styles = getComputedStyle(element); return {"
        "backgroundColor: styles.backgroundColor, "
        "borderTopColor: styles.borderTopColor, color: styles.color}; }"
    )
    for selector, required in expected.items():
        locator = page.locator(selector)
        assert locator.count() == 1, f"A 色板验收缺少表面 {selector}"
        actual_style = locator.evaluate(expression)
        assert all(actual_style.get(key) == value for key, value in required.items()), (
            f"{selector} 未使用 A 色板：{actual_style}"
        )

    focus_target = page.locator("#refresh-quotes")
    focus_target.focus()
    focus = focus_target.evaluate(
        "element => { const styles = getComputedStyle(element); return {"
        "outlineColor: styles.outlineColor, outlineStyle: styles.outlineStyle, "
        "outlineWidth: styles.outlineWidth}; }"
    )
    assert focus == {
        "outlineColor": "rgb(139, 94, 52)",
        "outlineStyle": "solid", "outlineWidth": "3px",
    }, f"主操作焦点未使用 A 色板：{focus}"
```

When implementing, keep the fake `Page.evaluate()` signature compatible with the second `names` argument shown above.

- [ ] **Step 4: Add the real report geometry and overflow helper**

Add:

```python
def _check_open_report_layout(page: Any, workspace: Any, broker: str) -> None:
    viewport = getattr(page, "viewport_size", None) or {}
    width = viewport.get("width", 0)
    if width >= 1920:
        geometry = page.evaluate("""() => {
          const shell = document.querySelector('.dashboard-shell').getBoundingClientRect();
          const header = document.querySelector('.dashboard-header').getBoundingClientRect();
          const report = document.querySelector('#trend-report-workspace').getBoundingClientRect();
          return {shellWidth: shell.width, headerLeft: header.left, headerRight: header.right,
                  reportLeft: report.left, reportRight: report.right};
        }""")
        assert abs(geometry["shellWidth"] - 1600) <= 1, "1920px 下 Dashboard shell 不是 1600px"
        assert abs(geometry["headerLeft"] - geometry["reportLeft"]) <= 1, "趋势报告左边线未与 Header 对齐"
        assert abs(geometry["headerRight"] - geometry["reportRight"]) <= 1, "趋势报告右边线未与 Header 对齐"

    if broker != "eastmoney":
        return
    buy_stage = workspace.locator(".cn-trend-buy")
    assert buy_stage.count() == 1, "A 股趋势报告缺少正式买入区"
    if width <= 760:
        cards = buy_stage.locator(".cn-trend-card:visible")
        assert cards.count() >= 1, "A 股趋势报告手机端缺少卡片"
        return
    overflow = buy_stage.evaluate(
        "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, "
        "overflowX: getComputedStyle(element).overflowX})"
    )
    assert overflow["overflowX"] == "auto", "A 股正式买入区未启用内部横向滚动"
    assert overflow["scrollWidth"] > overflow["clientWidth"], "A 股正式买入宽表没有可滚动内容"
```

Call `_check_open_report_layout(page, workspace, broker)` immediately after confirming the open workspace. Keep the existing page-level overflow assertion after it. Extend `TabbedAccountLocator.evaluate()` so the existing unit fake models the desktop buy-stage overflow without weakening its focus check:

```python
    def evaluate(self, expression: str) -> bool | dict[str, object]:
        if self.selector.endswith(".cn-trend-buy"):
            return {
                "clientWidth": 1500, "scrollWidth": 1600,
                "overflowX": "auto",
            }
        assert "document.activeElement" in expression
        self.page.focus_checks.append(self.selector)
        return self.selector == self.page.active
```

- [ ] **Step 5: Expand the browser matrix and save screenshots**

At the start of `_browser_check()` after launching the browser:

```python
            ACCEPTANCE_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
```

Replace the viewport tuple with:

```python
            for name, viewport in (
                ("wide_desktop", {"width": 1920, "height": 1080}),
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 375, "height": 844}),
            ):
```

Call `_check_visual_contract(page)` immediately after `page.goto()`. Then save the portfolio screenshot:

```python
                    page.screenshot(
                        path=str(ACCEPTANCE_SCREENSHOT_DIR / f"{name}-portfolio.png"),
                        full_page=True,
                    )
```

Extend `_check_account_holdings()` with a defaulted keyword-only screenshot argument so existing direct unit calls remain side-effect free:

```python
def _check_account_holdings(
    page: Any,
    payload: dict[str, Any],
    *,
    reports_dir: Path | None = None,
    screenshot_dir: Path | None = None,
) -> None:
```

Pass `screenshot_dir=ACCEPTANCE_SCREENSHOT_DIR` only from `_browser_check()`. Inside `_check_account_holdings()`, after `_check_open_report_layout()` and only while the Eastmoney report is open, save:

```python
        if broker == "eastmoney" and screenshot_dir is not None:
            width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
            page.screenshot(
                path=str(screenshot_dir / f"{width}-trend-report.png"),
                full_page=True,
            )
```

- [ ] **Step 6: Update the browser-orchestration fake for three viewports and screenshots**

In `test_browser_check_treats_page_error_as_desktop_failure_and_runs_mobile()`:

- rename the initial failure flag and expected first error from `desktop` to `wide_desktop`;
- make `Browser.new_page()` choose names from `("wide_desktop", "desktop", "mobile")` by page index;
- add `Page.screenshot()` that records `(self.name, path)`;
- make `Page.evaluate()` return exact token/style/geometry dictionaries for the new helper expressions and `True` for page-overflow expressions;
- update the expected widths to `[1920, 1440, 375]`;
- update the second-run decision errors to all three viewport names;
- assert portfolio screenshots exist for all three names and trend-report screenshots exist for widths `1920`, `1440`, and `375`.

Use this exact name mapping in the fake browser:

```python
        def new_page(self, **kwargs: object) -> Page:
            names = ("wide_desktop", "desktop", "mobile")
            name = names[self.pages]
            self.pages += 1
            viewport = kwargs["viewport"]
            viewport_widths.append(viewport["width"])
            return Page(name, viewport)
```

Use these exact fake methods so the permanent checks exercise computed styles, geometry, and screenshots instead of being bypassed:

```python
        def evaluate(
            self, expression: str, argument: object | None = None
        ) -> object:
            if "getPropertyValue" in expression:
                assert argument == list(dashboard_acceptance.WARM_LEDGER_TOKENS)
                return dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
            if "const shell" in expression:
                return {
                    "shellWidth": 1600,
                    "headerLeft": 176, "headerRight": 1744,
                    "reportLeft": 176, "reportRight": 1744,
                }
            assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
            evaluated.append(self.name)
            return True

        def screenshot(self, *, path: str, full_page: bool) -> None:
            assert full_page is True
            screenshots.append((self.name, path))
```

Override `Locator.evaluate()` in the same test as follows, delegating focus and buy-stage behavior to the base fake:

```python
        def focus(self) -> None:
            pass

        def evaluate(self, expression: str) -> object:
            if "getComputedStyle" in expression:
                if "outlineColor" in expression:
                    return {"outlineColor": "rgb(139, 94, 52)", "outlineStyle": "solid", "outlineWidth": "3px"}
                if self.selector == "body":
                    return {"backgroundColor": "rgb(247, 245, 241)", "color": "rgb(32, 29, 24)"}
                if self.selector == "#refresh-quotes":
                    return {"backgroundColor": "rgb(139, 94, 52)", "borderTopColor": "rgb(139, 94, 52)", "color": "rgb(255, 255, 255)"}
                if self.selector == ".current-view-card":
                    return {"backgroundColor": "rgb(36, 33, 29)", "borderTopColor": "rgb(36, 33, 29)", "color": "rgb(255, 255, 255)"}
                if self.selector.endswith(".cn-trend-buy"):
                    return {"clientWidth": 1500, "scrollWidth": 1600, "overflowX": "auto"}
                return {"backgroundColor": "rgb(255, 254, 250)", "borderTopColor": "rgb(216, 210, 200)", "color": "rgb(32, 29, 24)"}
            return super().evaluate(expression)
```

- [ ] **Step 7: Run the focused acceptance unit suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: all `tests/test_dashboard_acceptance.py` tests pass, including exact palette drift rejection, 1920px geometry, three viewport orchestration, and screenshot calls.

- [ ] **Step 8: Run all development checks except the final acceptance gate**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
git diff --check
```

Expected: both test commands pass and `git diff --check` prints nothing. Do not run `make acceptance` yet.

- [ ] **Step 9: Commit the permanent acceptance contract**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: enforce dashboard visual acceptance"
```

---

### Task 4: Run the final gate and redeploy the accepted SHA

**Files:**
- Verify: all files committed by Tasks 1–3
- Runtime: `screen` session `open_trader_dashboard_8766`
- Log: `/tmp/open_trader_dashboard_8766.log`
- Screenshots: `/tmp/open_trader_dashboard_acceptance/`

**Interfaces:**
- Consumes: committed `HEAD`, the live Dashboard command, real API/data/report files, existing `make acceptance` gate.
- Produces: one accepted and redeployed Git SHA at `http://127.0.0.1:8766/`.

- [ ] **Step 1: Run the full automated suite before touching the live process**

```bash
.venv/bin/python -m pytest -q
npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium
git status --short
```

Expected: pytest and Playwright pass. `git status --short` shows only the user's pre-existing untracked files, not uncommitted implementation changes.

- [ ] **Step 2: Inspect the existing live Dashboard before replacing it**

```bash
screen -ls | rg 'open_trader_dashboard_8766' || true
lsof -nP -iTCP:8766 -sTCP:LISTEN || true
ps -axo pid,lstart,command | rg 'open_trader dashboard .*--port 8766' || true
launchctl list | rg 'open-trader|dashboard' || true
```

Expected: record the existing screen session, listener PID, start time, command, and any service-manager ownership before stopping it.

- [ ] **Step 3: Start the candidate from the committed HEAD with a fresh log**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill "$listener"
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: the old listener exits and a new screen-owned Dashboard starts from `/Users/ray/projects/open_trader`.

- [ ] **Step 4: Verify the candidate process and direct workflow before the final gate**

```bash
PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
ps -p "$PID" -o pid=,lstart=,command=
lsof -a -p "$PID" -d cwd -Fn
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766/
curl -fsS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool | sed -n '1,40p'
sed -n '1,120p' /tmp/open_trader_dashboard_8766.log
```

Expected: new PID and timestamp, CWD `/Users/ray/projects/open_trader`, HTTP 200, valid real JSON, and no traceback or Dashboard load failure in the fresh log.

- [ ] **Step 5: Run `make acceptance` as the final gate**

```bash
make acceptance
```

Expected: final JSON reports `"status": "PASS"`, the current PID, no errors, and no blocker. Confirm all six files exist:

```bash
find /tmp/open_trader_dashboard_acceptance -maxdepth 1 -type f -name '*.png' -print | sort
```

Expected files:

```text
/tmp/open_trader_dashboard_acceptance/1440-trend-report.png
/tmp/open_trader_dashboard_acceptance/1920-trend-report.png
/tmp/open_trader_dashboard_acceptance/375-trend-report.png
/tmp/open_trader_dashboard_acceptance/desktop-portfolio.png
/tmp/open_trader_dashboard_acceptance/mobile-portfolio.png
/tmp/open_trader_dashboard_acceptance/wide_desktop-portfolio.png
```

If the gate returns `FAIL`, diagnose, fix, commit, restart the candidate, and rerun this step. If it returns `BLOCKED`, stop and report the browser/environment blocker; do not substitute curl, fixtures, or screenshots for acceptance.

- [ ] **Step 6: Redeploy the exact accepted SHA without source changes**

```bash
ACCEPTED_SHA=$(git rev-parse HEAD)
OLD_PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
LOG_SIZE=$(stat -f '%z' /tmp/open_trader_dashboard_8766.log 2>/dev/null || echo 0)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: no source or data change occurs between acceptance and this restart.

- [ ] **Step 7: Verify the post-acceptance review deployment**

```bash
NEW_PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
test "$NEW_PID" != "$OLD_PID"
ps -p "$NEW_PID" -o pid=,lstart=,command=
lsof -a -p "$NEW_PID" -d cwd -Fn
test "$(git -C /Users/ray/projects/open_trader rev-parse HEAD)" = "$ACCEPTED_SHA"
tail -c +$((LOG_SIZE + 1)) /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766/
```

Expected: a different PID, CWD `/Users/ray/projects/open_trader`, exact accepted SHA, fresh post-restart log content without errors, and HTTP 200. Only now provide `http://127.0.0.1:8766/` for user review.
