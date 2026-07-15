# US Session Label Colors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing overnight, premarket, regular, and after-hours text labels four distinct accessible colors without changing price layout or behavior.

**Architecture:** Keep the existing `renderQuotePrice()` and `.session-quote-label` path. Add the selected `price_session` as an escaped semantic `data-session` attribute, then let four native CSS attribute selectors color only the label. No component, state, API, or valuation changes.

**Tech Stack:** Static JavaScript, CSS, pytest/Node helper tests, Playwright acceptance.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-15-us-session-price-colors-design.md` exactly.
- Color only the session label; price and quote time retain their current colors.
- Use exact colors: overnight `#6941C6`, premarket `#B54708`, regular `#175CD3`, after-hours `#027A48`.
- Preserve the text label so color is never the only session indicator.
- Preserve the compact single-line desktop/mobile layout and fallback source-session behavior.
- Do not change quote selection, valuation, API, Header, watcher, notification, order, dependency, or configuration behavior.
- After the committed change, deploy the exact SHA, run `make acceptance`, require `PASS`, then restart and verify the exact accepted SHA, PID, cwd, fresh logs, and HTTP 200.

---

### Task 1: Add semantic session colors to the existing label

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:5935-5946`
- Modify: `src/open_trader/dashboard_static/dashboard.css:3407-3431`
- Modify: `tests/test_dashboard_web.py` near `test_dashboard_renders_one_compact_us_session_price_and_header_time`

**Interfaces:**
- Consumes: existing quote field `price_session` with `overnight`, `pre_market`, `regular`, or `after_hours`.
- Produces: `data-session="<price_session>"` on the existing `.session-quote-label` span.
- Preserves: the existing HTML structure, label copy, selected price, time/fallback text, and all valuation behavior.

- [ ] **Step 1: Add failing JavaScript and CSS mapping tests**

Extend the existing compact-price JavaScript test with all four session keys:

```python
output = run_dashboard_js(r'''
const sessions = {
  overnight: "夜盘",
  pre_market: "盘前",
  regular: "盘中",
  after_hours: "盘后",
};
for (const [key, label] of Object.entries(sessions)) {
  const html = renderQuotePrice({market:"US"}, {
    last_price:"61.50",
    price_session:key,
    price_time:"2026-07-15 03:03:01.150",
    current_session_quote:true,
  });
  if (!html.includes(label) || !html.includes(`data-session="${key}"`)) {
    throw new Error(`${key}: ${html}`);
  }
}
console.log("ok");
''')
assert "ok" in output
```

Add a static CSS test:

```python
def test_dashboard_session_labels_use_distinct_semantic_colors() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    for session, color in {
        "overnight": "#6941C6",
        "pre_market": "#B54708",
        "regular": "#175CD3",
        "after_hours": "#027A48",
    }.items():
        assert (
            f'.session-quote-label[data-session="{session}"] {{\n'
            f'  color: {color};\n'
            "}"
        ) in css
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py \
  -k 'compact_us_session_price_and_header_time or session_labels_use_distinct_semantic_colors' -q
```

Expected: both new assertions fail because the semantic attribute and four CSS rules do not exist.

- [ ] **Step 3: Add the semantic attribute and native CSS rules**

In `renderQuotePrice()`, retain the source session key and add it only to the existing label span:

```javascript
const sessionKey = String(quote.price_session || "");
const session = String(holding && holding.market || "").toUpperCase() === "US"
  ? sessionQuoteLabel(sessionKey) : "";
if (!session) return escapeHtml(String(quote.last_price));
const detail = quote.current_session_quote
  ? quoteTimeEt(quote.price_time)
  : "上一有效价";
return `<span class="session-quote"><span class="session-quote-label" data-session="${escapeHtml(sessionKey)}">${escapeHtml(session)}</span><strong class="session-quote-price">${escapeHtml(String(quote.last_price))}</strong>${detail ? `<span class="session-quote-time">· ${escapeHtml(detail)}</span>` : ""}</span>`;
```

Keep the existing default label color as a defensive fallback, then add exactly these selectors after `.session-quote-label`:

```css
.session-quote-label[data-session="overnight"] {
  color: #6941C6;
}

.session-quote-label[data-session="pre_market"] {
  color: #B54708;
}

.session-quote-label[data-session="regular"] {
  color: #175CD3;
}

.session-quote-label[data-session="after_hours"] {
  color: #027A48;
}
```

Do not color `.session-quote-price` or `.session-quote-time`.

- [ ] **Step 4: Run focused and complete tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS with the four semantic mappings covered and no existing Dashboard regression.

- [ ] **Step 5: Commit the implementation**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: color US session labels"
```

- [ ] **Step 6: Run the required live acceptance and exact-SHA deployment**

Stop the current 8766 screen/listener, wait until the port is free, clear the runtime log, and start the committed worktree SHA on port 8766. Then run:

```bash
make acceptance
```

Expected: final JSON has `"status": "PASS"`, with real quotes, two strictly newer refresh cycles, and desktop/mobile browser checks.

After `PASS`, restart the exact accepted SHA without source or data changes. Verify:

```text
one 8766 listener
cwd=/Users/ray/projects/open_trader/.worktrees/us-session-price-dashboard
running SHA equals accepted SHA
fresh log lines contain the new PID
GET / returns HTTP 200
GET /api/quotes returns HTTP 200
```

