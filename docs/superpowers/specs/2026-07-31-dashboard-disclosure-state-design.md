# Dashboard Disclosure State Preservation

## Goal

Keep a user's manually opened or closed disclosure panel open or closed when
the dashboard refreshes its account/trend-report HTML, without changing any
other page's behavior.

## Observed root cause

The dashboard polls `/api/quotes` every five seconds. A successful poll reloads
`/api/dashboard`, which calls `renderDashboardViews()` and replaces the
`#account-holdings` HTML. Account-view interactions can also replace the active
account panel through `renderAccountViewPanelOnly()`. Native `<details>` state
lives on the DOM node, so both replacement paths discard the user's `open`
state and recreate the default-collapsed markup.

## Scope

Preserve the `open` boolean for every `<details>` inside the account holdings
surface, including trend-report disclosures, account cash details, decision
detail disclosures, and nested audit/risk/discipline disclosures. Apply the
same behavior to both:

- full account-surface rendering from `renderAccountHoldings()`;
- active account-panel rendering from `renderAccountViewPanelOnly()`.

The initial render remains exactly as today: disclosures that do not carry an
`open` attribute start collapsed.

## Non-goals and safety boundary

- Do not change the account-view tab state model; `state.accountViews` remains
  the authority for `真实持仓` / `模拟盘持仓` / `趋势报告`.
- Do not touch the prediction-market renderer; it already preserves its own
  event/relation expansion keys.
- Do not touch the Kelly lab, standard backtest, navigation, API payloads,
  report selection, strategy rules, or execution behavior.
- Do not use localStorage, URL parameters, or a new backend state store.
- Do not carry disclosure state from one broker, account view, report identity,
  or newly selected panel into another. A snapshot is restored only when the
  rendered surface has the same logical scope.
- Do not change scroll position, focus restoration, or the default collapsed
  state used by browser acceptance checks.

## Design

Add a small DOM-local capture/restore helper in
`src/open_trader/dashboard_static/dashboard.js`:

1. Before replacing an account surface, capture each descendant `<details>` and
   its `open` boolean using a deterministic key derived from its position and
   stable semantic attributes (`class` and existing `data-*` identity). Keep
   the snapshot in memory only for that synchronous render.
2. Render the new HTML normally.
3. Restore only matching keys on the new account surface. Unmatched new
   disclosures keep their normal default state; removed disclosures disappear
   with their content.
4. Gate the snapshot by the active broker/view/report scope so switching tabs or
   brokers cannot leak state from the previous content.

This keeps the renderer authoritative for data and markup while preserving the
one piece of browser-owned interaction state that a DOM replacement otherwise
loses. The helper is called only from the two account-surface replacement
paths, so unrelated workspaces remain byte-for-byte behaviorally unchanged.

## Verification contract

Add focused regression coverage in `tests/test_dashboard_web.py` that exercises
the real JavaScript renderer and asserts:

- the default trend-report disclosure is collapsed;
- opening the top-level discipline disclosure survives a full account render;
- closing it again survives a full account render;
- a nested discipline category and a report audit disclosure survive an active
  panel render;
- changing broker/account view does not transfer an old disclosure state;
- prediction-market expansion behavior remains covered by its existing tests.

Run the focused test first through a red-green cycle, then the dashboard web
and acceptance modules. Before handoff, reproduce the live five-second polling
flow on `127.0.0.1:8766`, inspect the running process/runtime source, and run
the repository's final Dashboard acceptance gate if the change is ready for
review.
