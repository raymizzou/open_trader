# Threshold Hedge A Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refine prototype Variant A so each candidate is compact by default and its annualized yield expands the candidate's calculation, validation, LLM evidence, and action details.

**Architecture:** Keep the existing single-file, no-dependency HTML prototype. Reuse its market-pair, evidence, LLM, economics, modal, and URL-state helpers; add one native `<details>` candidate disclosure for Variant A and remove duplicate always-visible annualized information.

**Tech Stack:** Static HTML, CSS, browser JavaScript, native `<details>` / `<summary>`, existing Open Trader dashboard tokens.

## Global Constraints

- Modify only the throwaway prototype branch; do not change the production Dashboard.
- Keep the existing Open Trader prediction-market visual language.
- Candidate details are closed by default and keyboard accessible.
- The annualized disclosure has a minimum 44px touch target.
- Use `$0.54 / $19.46 = 2.77%` and `2.77% * 365 / 47 = 21.55%`.
- Show settlement-delay and pre-fill profit caveats.
- Show the default profile as `sol · xhigh · fast`.
- Keep the page responsive without horizontal scrolling at 375px.

---

### Task 1: Candidate-level annualized disclosure

**Files:**
- Modify: `src/open_trader/dashboard_static/polymarket-threshold-hedge-ui-prototype.html`

**Interfaces:**
- Consumes: existing `pairHtml()`, `llmHtml(state)`, `evidenceHtml(state)`, `economicsHtml(state)`, `distributionHtml()`, and `actionHtml(state)` render helpers.
- Produces: `candidateDisclosureHtml(state)`, returning a closed native `<details>` candidate with the current state reflected in its summary and body.

- [ ] **Step 1: Record the current failing UI assertions**

Run this browser-side check against Variant A:

```js
({
  closedCandidates: document.querySelectorAll(".candidate-disclosure:not([open])").length,
  yieldButtons: [...document.querySelectorAll(".candidate-yield")].map((node) => node.textContent.trim()),
  oldYield: document.body.innerText.includes("22.8%"),
  modelProfile: document.body.innerText.includes("sol · xhigh · fast"),
})
```

Expected before implementation:

```js
{ closedCandidates: 0, yieldButtons: [], oldYield: true, modelProfile: false }
```

- [ ] **Step 2: Add the native candidate disclosure**

Add the Variant A candidate summary and body:

```html
<details class="panel candidate-disclosure">
  <summary>
    <span class="candidate-title">#HD-2051 · BTC 同事件阈值覆盖</span>
    <span class="pill approved">APPROVE</span>
    <span>24h $415k</span>
    <span class="ok">+$0.54</span>
    <span class="candidate-yield">21.5% · 查看详情</span>
  </summary>
  <div class="candidate-detail">
    <!-- annualized calculation, history, validation, LLM result, and action -->
  </div>
</details>
```

Do not add `open`; the detail must start closed.

- [ ] **Step 3: Put the calculation and existing evidence inside the candidate**

Render these values before the validation and LLM sections:

```html
<div class="annualized-formula">
  <div><span>理论最低利润</span><strong>$0.54</strong></div>
  <div><span>含费最大成本</span><strong>$19.46</strong></div>
  <div><span>预计资金占用</span><strong>47 天</strong></div>
  <div><span>简单收益率</span><strong>$0.54 / $19.46 = 2.77%</strong></div>
  <div><span>简单年化</span><strong>2.77% * 365 / 47 = 21.55%</strong></div>
</div>
```

Keep the warning that settlement delays lower realized annualized yield and that profit is not locked before both legs fill.

- [ ] **Step 4: Remove duplicate annualized displays**

Delete the page-level `简单年化` metric from the scan metric strip. Change `distributionHtml()` to `历史同类参考`, containing only:

```text
当前候选：21.5% · 高于历史同类 58%
7 天：P50 21% · P90 34% · n=86
30 天：P50 19% · P90 31% · n=302
```

Replace every remaining prototype value `22.8%` with `21.5%`.

- [ ] **Step 5: Show the selected Codex profile**

Change the Codex health item to:

```text
sol · xhigh · fast
默认模型 · reasoning · speed
```

This remains display-only in the prototype.

- [ ] **Step 6: Verify the refined interaction**

Reload Variant A and rerun:

```js
const closed = document.querySelectorAll(".candidate-disclosure:not([open])").length === 1;
const yieldText = document.querySelector(".candidate-yield")?.textContent.includes("21.5%");
const oldYieldGone = !document.body.innerText.includes("22.8%");
const profileVisible = document.body.innerText.includes("sol · xhigh · fast");
const noOverflow = document.documentElement.scrollWidth <= window.innerWidth;
if (![closed, yieldText, oldYieldGone, profileVisible, noOverflow].every(Boolean)) {
  throw new Error("Variant A refinement check failed");
}
```

Click the candidate summary and verify the formula, validation chain, LLM result, and manual-confirmation action become visible. Submit the prototype confirmation and verify it reaches `HOLDING · 待结算`. Repeat at 1440px, 768px, and 375px; confirm LLM rejection has no enabled order action and the browser console has no errors.

- [ ] **Step 7: Commit the prototype refinement**

```bash
git add src/open_trader/dashboard_static/polymarket-threshold-hedge-ui-prototype.html
git commit -m "prototype: refine candidate annualized disclosure"
```
