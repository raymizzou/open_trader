# Prediction Wallet Funding Cap $65 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the existing `$60.402411` dedicated Polymarket wallet balance while preserving a fail-closed `$65.00` funding cap.

**Architecture:** Keep `MAX_WALLET_BALANCE` as the single backend policy source. The Dashboard state API already exports that value; the order-confirmation UI will consume it instead of embedding `$50.00`, with no layout change.

**Tech Stack:** Python 3.12, `Decimal`, pytest, vanilla JavaScript, Playwright, macOS launchd.

## Global Constraints

- Wallet funding cap is exactly `$65.00`.
- Normal order cost remains exactly `$20.00`.
- Incident-remediation expected-loss cap remains exactly `$2.00`.
- Wallet balances above `$65.00` fail closed.
- The approved Dashboard layout does not change.
- Acceptance preflight must not submit an order, merge, approval, or transfer.
- Add no dependency or new configuration layer.

---

### Task 1: Raise the Backend Funding Boundary

**Files:**
- Modify: `tests/test_prediction_arbitrage.py`
- Modify: `src/open_trader/prediction_arbitrage.py`

**Interfaces:**
- Consumes: `build_pair_intent(...) -> PairIntent | None`
- Produces: `MAX_WALLET_BALANCE = Decimal("65.00")`, reused by trading validation, execution validation, and Dashboard policy payloads.

- [ ] **Step 1: Write the failing boundary test**

Replace the existing above-cap-only test with:

```python
@pytest.mark.parametrize(
    ("balance", "accepted"),
    (("65.00", True), ("65.01", False)),
)
def test_wallet_funding_cap_boundary(balance: str, accepted: bool) -> None:
    facts = market_facts(minimum_order_size="5")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])

    intent = build_pair_intent(
        facts,
        books,
        balance=Decimal(balance),
        allowance=Decimal("65.00"),
    )

    assert (intent is not None) is accepted
```

- [ ] **Step 2: Verify the new boundary fails**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_prediction_arbitrage.py::test_wallet_funding_cap_boundary
```

Expected: FAIL for the `$65.00` case because the current cap is `$50.00`.

- [ ] **Step 3: Make the minimal policy change**

In `src/open_trader/prediction_arbitrage.py`, set:

```python
MAX_WALLET_BALANCE = Decimal("65.00")
```

- [ ] **Step 4: Verify backend policy consumers**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the backend boundary**

```bash
git add src/open_trader/prediction_arbitrage.py tests/test_prediction_arbitrage.py
git commit -m "feat: raise prediction wallet cap to 65"
```

---

### Task 2: Make UI and Acceptance Copy Follow the Policy

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/serve_dashboard_fixture.py`
- Modify: `tests/e2e/prediction-market.spec.ts`
- Modify: `tests/e2e/prediction-market.spec.ts-snapshots/prediction-market-confirmation-desktop-chromium-darwin.png`
- Modify: `tests/e2e/prediction-market.spec.ts-snapshots/prediction-market-confirmation-mobile-chromium-darwin.png`
- Modify: `docs/superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md`

**Interfaces:**
- Consumes: `payload.policy_limits.max_wallet_balance`, exported by `dashboard_web._prediction_state`.
- Produces: order-confirmation copy showing `$65.00 pUSD`; unchanged markup and layout.

- [ ] **Step 1: Write failing API and static-contract assertions**

In `test_prediction_arbitrage_state_is_schema_valid_when_unavailable`, add:

```python
assert payload["policy_limits"]["max_wallet_balance"] == "65.00"
```

In `test_prediction_market_static_contract_is_present`, replace the old wallet-cap expectation with:

```python
for copy in ("$65", "$20", "$2", "免手续费", "可能只成交一腿", "24h 成交量"):
    assert copy in js
assert "$50.00 pUSD" not in js
```

In `[UI-05] cost disclosure`, expect `$65` instead of `$50`.

- [ ] **Step 2: Verify the UI contract fails**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_dashboard_web.py::test_prediction_arbitrage_state_is_schema_valid_when_unavailable \
  tests/test_dashboard_web.py::test_prediction_market_static_contract_is_present
```

Expected: FAIL because backend policy and confirmation copy still show `$50`.

- [ ] **Step 3: Pass the server policy into the existing modal**

When opening the order modal, include:

```javascript
max_wallet_balance: state.predictionMarket.payload?.policy_limits?.max_wallet_balance
```

In `predictionModalHtml`, format the existing field:

```javascript
const walletCap = predictionMoney(data.max_wallet_balance, "$65.00");
```

Render `${walletCap} pUSD` in the existing “独立钱包” row. Do not alter markup structure or CSS.

- [ ] **Step 4: Update deterministic fixtures and accepted copy**

Set fixture `policy_limits.max_wallet_balance` to `"65"` and update the original design spec’s active `$50` funding-policy and `UI-05` requirements to `$65`.

- [ ] **Step 5: Verify Python and browser behavior**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_dashboard_web.py::test_prediction_arbitrage_state_is_schema_valid_when_unavailable \
  tests/test_dashboard_web.py::test_prediction_market_static_contract_is_present
OPEN_TRADER_PYTHON=.venv/bin/python npx playwright test \
  tests/e2e/prediction-market.spec.ts \
  --grep "UI-05|confirmation golden" \
  --update-snapshots
OPEN_TRADER_PYTHON=.venv/bin/python npx playwright test \
  tests/e2e/prediction-market.spec.ts
```

Expected: API/static tests PASS; 24 prediction-market browser states and both updated confirmation goldens PASS.

- [ ] **Step 6: Visually inspect both changed confirmation snapshots**

Open the desktop and mobile confirmation PNGs. Confirm only `$50.00` → `$65.00` changed, with no clipping, reflow, overflow, or layout drift.

- [ ] **Step 7: Commit the UI contract**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_dashboard_web.py \
  tests/e2e/serve_dashboard_fixture.py \
  tests/e2e/prediction-market.spec.ts \
  tests/e2e/prediction-market.spec.ts-snapshots/prediction-market-confirmation-desktop-chromium-darwin.png \
  tests/e2e/prediction-market.spec.ts-snapshots/prediction-market-confirmation-mobile-chromium-darwin.png \
  docs/superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md
git commit -m "feat: show 65 dollar prediction wallet cap"
```

---

### Task 3: Prove Real Readiness and Run the Final Dashboard Gate

**Files:**
- Verify only: `config/prediction_arbitrage.json`
- Verify only: macOS Keychain service `com.open-trader.polymarket`
- Verify only: launchd label `com.open-trader.dashboard`

**Interfaces:**
- Consumes: committed feature-branch SHA, real Keychain credentials, HK Polymarket route.
- Produces: real wallet/preflight PASS and exact-SHA Dashboard deployment evidence.

- [ ] **Step 1: Run focused prediction-market regression**

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py
git diff --check
git status --short
```

Expected: tests PASS; no whitespace errors; worktree clean.

- [ ] **Step 2: Run real non-mutating checks**

```bash
PYTHONPATH=src .venv/bin/python -m open_trader prediction-arb wallet status \
  --config config/prediction_arbitrage.json
PYTHONPATH=src .venv/bin/python -m open_trader prediction-arb preflight \
  --config config/prediction_arbitrage.json --no-submit
```

Expected: both commands print `result: PASS`; no secret or unmasked private material appears.

- [ ] **Step 3: Deploy the candidate branch to launchd**

```bash
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --python "$PWD/.venv/bin/python"
```

Expected: `installed launchd agent: com.open-trader.dashboard` and HTTP readiness at `http://127.0.0.1:8766/`.

- [ ] **Step 4: Run the final acceptance gate once**

```bash
make acceptance
```

Expected: `PASS`. If it prints `FAIL`, fix and rerun. If it prints `BLOCKED`, report the external blocker and do not substitute mocks or screenshots.

- [ ] **Step 5: Redeploy the exact accepted SHA**

Confirm the SHA and clean tree, then redeploy without changing source:

```bash
git rev-parse HEAD
git status --short
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --python "$PWD/.venv/bin/python"
```

- [ ] **Step 6: Verify post-acceptance runtime evidence**

```bash
pid="$(lsof -nP -tiTCP:8766 -sTCP:LISTEN | head -n 1)"
ps -p "$pid" -o pid=,lstart=,command=
lsof -a -p "$pid" -d cwd -Fn
launchctl print "gui/$UID/com.open-trader.dashboard"
tail -n 40 logs/dashboard/launchd.out.log
tail -n 40 logs/dashboard/launchd.err.log
curl --fail --silent --show-error --output /dev/null \
  --write-out 'HTTP %{http_code}\n' http://127.0.0.1:8766/
```

Expected: new PID, worktree cwd, accepted SHA in runtime evidence, fresh secret-clean logs, and `HTTP 200`.
