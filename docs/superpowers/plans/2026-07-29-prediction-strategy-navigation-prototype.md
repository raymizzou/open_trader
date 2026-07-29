# Prediction Strategy Navigation Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved `YES/NO套利` and `LLM对冲套利` secondary navigation to the threshold-hedge UI prototype.

**Architecture:** Keep the existing single-file static prototype and add one native two-button strategy switch below the prediction-market page heading. The LLM strategy remains the default; switching to `YES/NO套利` shows a small handoff surface representing the unchanged existing page, while switching back restores the complete LLM prototype without reloading.

**Tech Stack:** Static HTML, CSS, browser JavaScript, existing Open Trader dashboard tokens.

## Global Constraints

- Modify only the throwaway prototype branch; do not change the production Dashboard.
- Keep `预测市场` as the primary navigation item.
- Use the exact labels `YES/NO套利` and `LLM对冲套利`.
- Keep the existing strategy as the default in production; the prototype may open on `LLM对冲套利` for review.
- Do not persist the selected strategy outside the current prototype URL.
- Preserve all existing Variant A states and responsive behavior.

---

### Task 1: Secondary strategy switch

**Files:**
- Modify: `src/open_trader/dashboard_static/polymarket-threshold-hedge-ui-prototype.html`

**Interfaces:**
- Consumes: existing `currentParams()`, `setParams(next)`, `render()`, and `variantA(state)`.
- Produces: a `strategy` URL parameter with values `yes_no` and `llm_hedge`, plus a secondary navigation whose `aria-current` state matches it.

- [ ] **Step 1: Record the failing browser assertion**

Run against the current Variant A:

```js
const labels = [...document.querySelectorAll("[data-strategy]")].map((node) => node.textContent.trim());
if (JSON.stringify(labels) !== JSON.stringify(["YES/NO套利", "LLM对冲套利"])) {
  throw new Error("strategy navigation missing");
}
```

Expected: FAIL with `strategy navigation missing`.

- [ ] **Step 2: Add the strategy navigation markup and styles**

Place this below the page heading:

```html
<nav class="strategy-switch" aria-label="套利策略">
  <button type="button" data-strategy="yes_no">YES/NO套利</button>
  <button type="button" data-strategy="llm_hedge">LLM对冲套利</button>
</nav>
```

Reuse the existing neutral border, surface, primary, and 44px touch-target
tokens. At mobile widths, keep both labels on one two-column row.

- [ ] **Step 3: Add in-page strategy state**

Extend `currentParams()` and `setParams(next)` so `strategy` defaults to
`llm_hedge` in the prototype and remains in the URL. In `render()`, set
`aria-current="page"` only on the selected strategy button.

Add a document click branch:

```js
const strategyButton = event.target.closest("[data-strategy]");
if (strategyButton) {
  setParams({ strategy: strategyButton.dataset.strategy });
  return;
}
```

- [ ] **Step 4: Keep the existing strategy handoff minimal**

When `strategy === "yes_no"`, replace only the prototype-specific LLM content
with:

```html
<section class="panel existing-strategy-handoff">
  <header class="panel-heading">
    <div>
      <h2>YES/NO套利</h2>
      <p>这里接回现在已有的套利监控、机会确认和历史记录，页面内容保持不变。</p>
    </div>
    <span class="pill neutral">现有页面</span>
  </header>
</section>
```

Switching back to `llm_hedge` restores all current prototype states.

- [ ] **Step 5: Verify switching and responsive behavior**

Run:

```js
const labels = [...document.querySelectorAll("[data-strategy]")].map((node) => node.textContent.trim());
const active = document.querySelector('[data-strategy][aria-current="page"]')?.textContent.trim();
const noOverflow = document.documentElement.scrollWidth <= window.innerWidth;
if (JSON.stringify(labels) !== JSON.stringify(["YES/NO套利", "LLM对冲套利"]) || active !== "LLM对冲套利" || !noOverflow) {
  throw new Error("strategy navigation check failed");
}
```

Click `YES/NO套利`, verify the handoff surface appears without reload, then
click `LLM对冲套利` and verify the approved candidate, rejection, confirmation,
and holding states still work. Repeat at 1440px, 768px, and 375px with no
browser console errors.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/polymarket-threshold-hedge-ui-prototype.html
git commit -m "prototype: add prediction strategy switch"
```
