# Kelly Single-Market Capital Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce one-market-per-Kelly-experiment rules, split mixed-market mock data, and show fixed market capital pools in the Kelly lab.

**Architecture:** Keep strategy templates market-agnostic and make experiments market-scoped. Add a small market-capital rules module used by the lab loader, order-intent builder, and risk checker. UI reads the validated experiment shape and renders market/budget metadata without adding editable controls.

**Tech Stack:** Python 3, pytest, JSON artifacts under `data/latest`, vanilla dashboard JS/CSS, Playwright E2E.

---

## File Map

- Create `src/open_trader/kelly_market_rules.py`: fixed market constants, currency mapping, and small normalization helpers.
- Modify `src/open_trader/kelly_lab.py`: require `experiment.market`, validate participant market consistency, attach market capital metadata.
- Modify `src/open_trader/kelly_order_intents.py`: include `experiment_market` and market capital fields in each intent.
- Modify `src/open_trader/kelly_order_risk.py`: block missing/cross-market/mismatched-currency intents before entry sizing.
- Modify `src/open_trader/dashboard_static/dashboard.js`: show experiment market and fixed simulated capital pool.
- Modify `src/open_trader/dashboard_static/dashboard.css`: minor layout styling for market/budget metadata if needed.
- Modify `data/latest/kelly_experiments.json`: split the mixed trend-pullback mock into single-market experiments.
- Modify `tests/test_kelly_lab.py`: coverage for validation, budget defaults, and symbol index behavior.
- Modify `tests/test_kelly_order_intents.py`: coverage for propagated experiment market.
- Modify `tests/test_kelly_order_risk.py`: coverage for cross-market and currency blocks.
- Modify `tests/test_dashboard.py`, `tests/test_dashboard_web.py`, or existing dashboard tests where Kelly payload expectations are asserted.
- Modify `tests/e2e/fixtures/kelly-dashboard.json`: single-market fixture data.
- Modify `tests/e2e/kelly-lab.spec.ts`: verify separate US/HK experiments and no editable participant controls.

---

### Task 1: Add Market Capital Rules and Loader Validation

**Files:**
- Create: `src/open_trader/kelly_market_rules.py`
- Modify: `src/open_trader/kelly_lab.py`
- Test: `tests/test_kelly_lab.py`

- [ ] **Step 1: Write failing loader tests**

Append these tests to `tests/test_kelly_lab.py`. Use the existing `write_json` helper already in the file.

```python
def minimal_template_payload() -> dict[str, object]:
    return {
        "schema_version": "open_trader.kelly_strategy_templates.v1",
        "templates": [
            {
                "strategy_id": "trend_pullback_20d",
                "strategy_name": "趋势回调 20D",
                "strategy_version": "v1",
                "entry_rule_description": "价格回调到 20 日均线附近。",
                "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                "max_holding_days": 20,
                "order_type": "limit",
                "market_session": "regular",
            }
        ],
    }


def test_load_kelly_lab_state_rejects_mixed_market_experiment(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_us",
                    "experiment_name": "趋势回调 US",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "market": "US",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "HK",
                            "symbol": "02840",
                            "name": "SPDR Gold",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        }
                    ],
                    "stats": {
                        "completed_samples": 0,
                        "open_samples": 0,
                        "observed_win_rate": "",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="trend_us participant HK.02840 must match experiment market US",
    ):
        load_kelly_lab_state(data_dir)


def test_load_kelly_lab_state_attaches_market_capital_pool(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_us",
                    "experiment_name": "趋势回调 US",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "market": "us",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "us",
                            "symbol": "ram",
                            "name": "RAM",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        }
                    ],
                    "stats": {
                        "completed_samples": 0,
                        "open_samples": 0,
                        "observed_win_rate": "",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    experiment = state["experiments"][0]
    assert experiment["market"] == "US"
    assert experiment["budget_currency"] == "USD"
    assert experiment["market_capital_pool"] == {
        "market": "US",
        "amount": "100000",
        "currency": "USD",
        "enabled": True,
    }
    assert experiment["participants"][0]["market"] == "US"
    assert experiment["participants"][0]["symbol"] == "RAM"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_rejects_mixed_market_experiment tests/test_kelly_lab.py::test_load_kelly_lab_state_attaches_market_capital_pool -q
```

Expected: both tests fail because `market` is not required/normalized and `market_capital_pool` is not attached.

- [ ] **Step 3: Add market rules module**

Create `src/open_trader/kelly_market_rules.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellyMarketCapitalPool:
    market: str
    amount: str
    currency: str
    enabled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "amount": self.amount,
            "currency": self.currency,
            "enabled": self.enabled,
        }


KELLY_MARKET_CAPITAL_POOLS: dict[str, KellyMarketCapitalPool] = {
    "US": KellyMarketCapitalPool(
        market="US",
        amount="100000",
        currency="USD",
        enabled=True,
    ),
    "HK": KellyMarketCapitalPool(
        market="HK",
        amount="500000",
        currency="HKD",
        enabled=True,
    ),
    "CN": KellyMarketCapitalPool(
        market="CN",
        amount="500000",
        currency="CNY",
        enabled=False,
    ),
}


def normalize_kelly_market(value: object) -> str:
    market = str(value or "").strip().upper()
    if market not in KELLY_MARKET_CAPITAL_POOLS:
        raise ValueError("market must be one of: US, HK, CN")
    return market


def kelly_market_currency(market: str) -> str:
    return KELLY_MARKET_CAPITAL_POOLS[normalize_kelly_market(market)].currency


def kelly_market_capital_pool(market: str) -> dict[str, object]:
    return KELLY_MARKET_CAPITAL_POOLS[normalize_kelly_market(market)].to_dict()
```

- [ ] **Step 4: Implement loader validation**

In `src/open_trader/kelly_lab.py`, import helpers:

```python
from .kelly_market_rules import (
    kelly_market_capital_pool,
    kelly_market_currency,
    normalize_kelly_market,
)
```

Update `REQUIRED_EXPERIMENT_FIELDS` to include:

```python
    "market",
```

In `_validate_experiments_payload`, after the experiment is deep-copied and required fields are checked, normalize the experiment before participant validation. If the function currently builds `normalized`, add this helper call in that path:

```python
def _normalize_experiment_market_fields(
    experiment: dict[str, Any],
    *,
    experiment_index: int,
) -> dict[str, Any]:
    normalized = copy.deepcopy(experiment)
    experiment_id = str(normalized.get("experiment_id", "")).strip()
    market = normalize_kelly_market(normalized.get("market"))
    expected_currency = kelly_market_currency(market)
    budget_currency = str(normalized.get("budget_currency", "")).strip().upper()
    if budget_currency != expected_currency:
        raise ValueError(
            f"{experiment_id or experiment_index} budget_currency "
            f"{budget_currency!r} must be {expected_currency!r} for market {market}",
        )

    normalized["market"] = market
    normalized["budget_currency"] = expected_currency
    normalized["market_capital_pool"] = kelly_market_capital_pool(market)

    participants = normalized.get("participants")
    if not isinstance(participants, list):
        raise ValueError(f"kelly_experiments.json experiment {experiment_index} participants must be a list")

    checked_participants: list[dict[str, Any]] = []
    for participant_index, participant in enumerate(participants):
        if not isinstance(participant, dict):
            raise ValueError(
                f"kelly_experiments.json experiment {experiment_index} "
                f"participant {participant_index} must be an object",
            )
        checked = copy.deepcopy(participant)
        participant_market = normalize_kelly_market(checked.get("market"))
        symbol = str(checked.get("symbol", "")).strip().upper()
        if participant_market != market:
            raise ValueError(
                f"{experiment_id or experiment_index} participant "
                f"{participant_market}.{symbol} must match experiment market {market}",
            )
        participant_currency = str(
            checked.get("budget_currency", expected_currency),
        ).strip().upper()
        if participant_currency != expected_currency:
            raise ValueError(
                f"{experiment_id or experiment_index} participant "
                f"{participant_market}.{symbol} budget_currency "
                f"{participant_currency!r} must be {expected_currency!r}",
            )
        checked["market"] = participant_market
        checked["symbol"] = symbol
        checked["budget_currency"] = expected_currency
        checked_participants.append(checked)

    normalized["participants"] = checked_participants
    return normalized
```

Call `_normalize_experiment_market_fields()` before attaching templates and before lifecycle state filtering.

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py -q
```

Expected: all `test_kelly_lab.py` tests pass after updating existing fixtures to include top-level `market` and single-market participant sets.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/kelly_market_rules.py src/open_trader/kelly_lab.py tests/test_kelly_lab.py
git commit -m "feat: enforce kelly experiment market scope"
```

---

### Task 2: Split Single-Market Mock Experiments

**Files:**
- Modify: `data/latest/kelly_experiments.json`
- Test: `tests/test_kelly_lab.py`

- [ ] **Step 1: Write failing fixture test**

Append this test to `tests/test_kelly_lab.py`:

```python
def test_latest_kelly_experiments_are_single_market() -> None:
    state = load_kelly_lab_state(Path("data")).to_dict()

    experiments = state["experiments"]
    assert experiments
    for experiment in experiments:
        market = experiment["market"]
        assert market in {"US", "HK", "CN"}
        assert experiment["market_capital_pool"]["market"] == market
        for participant in experiment["participants"]:
            assert participant["market"] == market
```

- [ ] **Step 2: Run test to verify it fails on current mixed mock**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py::test_latest_kelly_experiments_are_single_market -q
```

Expected: FAIL because the current trend-pullback mock contains both US and HK participants.

- [ ] **Step 3: Update `data/latest/kelly_experiments.json`**

Replace the mixed `trend_pullback_20d_mock_20260707` experiment with two experiments:

```json
{
  "experiment_id": "trend_pullback_20d_us_mock_20260707",
  "experiment_name": "趋势回调 20D Mock US 第一批",
  "strategy_id": "trend_pullback_20d",
  "strategy_version": "v1",
  "market": "US",
  "start_date": "2026-07-07",
  "paper_account": "futu_simulate_us",
  "experiment_budget": "100000",
  "budget_currency": "USD",
  "capital_utilization_pct": "50",
  "allocation_mode": "equal_weight",
  "max_open_position_per_symbol": 1,
  "status": "running",
  "locked": true,
  "participants": [
    {
      "market": "US",
      "symbol": "DRAM",
      "name": "Roundhill Memory ETF",
      "source": "holding",
      "locked": true,
      "per_symbol_budget": "33333.33",
      "budget_currency": "USD"
    },
    {
      "market": "US",
      "symbol": "RAM",
      "name": "2倍做多DRAM ETF-T-REX",
      "source": "holding",
      "locked": true,
      "per_symbol_budget": "33333.33",
      "budget_currency": "USD"
    },
    {
      "market": "US",
      "symbol": "SOXX",
      "name": "iShares费城交易所半导体ETF",
      "source": "holding",
      "locked": true,
      "per_symbol_budget": "33333.33",
      "budget_currency": "USD"
    }
  ]
}
```

Add a separate HK experiment:

```json
{
  "experiment_id": "trend_pullback_20d_hk_mock_20260707",
  "experiment_name": "趋势回调 20D Mock HK 第一批",
  "strategy_id": "trend_pullback_20d",
  "strategy_version": "v1",
  "market": "HK",
  "start_date": "2026-07-07",
  "paper_account": "futu_simulate_hk",
  "experiment_budget": "500000",
  "budget_currency": "HKD",
  "capital_utilization_pct": "50",
  "allocation_mode": "equal_weight",
  "max_open_position_per_symbol": 1,
  "status": "running",
  "locked": true,
  "participants": [
    {
      "market": "HK",
      "symbol": "02840",
      "name": "SPDR金",
      "source": "holding",
      "locked": true,
      "per_symbol_budget": "500000",
      "budget_currency": "HKD"
    }
  ]
}
```

Preserve the existing `stats`, `strategy detail`, `order_sync`, `order_execution`, and `lifecycle_states` content by moving US rows to the US experiment and HK rows to the HK experiment.

- [ ] **Step 4: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: tests pass after updating expected experiment names/counts.

- [ ] **Step 5: Commit**

```bash
git add data/latest/kelly_experiments.json tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "test: use single market kelly experiments"
```

---

### Task 3: Propagate Experiment Market into Order Intents

**Files:**
- Modify: `src/open_trader/kelly_order_intents.py`
- Test: `tests/test_kelly_order_intents.py`

- [ ] **Step 1: Write failing intent test**

Update `test_build_kelly_order_intents_payload_from_pending_lifecycle_states` so the running experiment includes:

```python
"market": "US",
"market_capital_pool": {
    "market": "US",
    "amount": "100000",
    "currency": "USD",
    "enabled": True,
},
```

Add these fields to each expected intent:

```python
"experiment_market": "US",
"market_capital_pool": {
    "market": "US",
    "amount": "100000",
    "currency": "USD",
    "enabled": True,
},
```

Add a second test:

```python
def test_build_kelly_order_intents_skips_cross_market_lifecycle_state() -> None:
    payload = build_kelly_order_intents_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "status": "running",
                "budget_currency": "USD",
                "participants": [
                    {
                        "market": "US",
                        "symbol": "RAM",
                        "per_symbol_budget": "25000",
                        "budget_currency": "USD",
                    }
                ],
                "stats": {"suggested_position_pct": "4%"},
                "lifecycle_states": [
                    {
                        "status": "pending_entry_order",
                        "market": "HK",
                        "symbol": "02840",
                    }
                ],
            }
        ],
        created_at="2026-07-10 13:30",
    )

    assert payload["intent_count"] == 0
    assert payload["intents"] == []
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_intents.py -q
```

Expected: FAIL because intents do not include `experiment_market` and cross-market lifecycle states are not explicitly skipped.

- [ ] **Step 3: Implement intent propagation**

In `build_kelly_order_intents_payload`, compute the experiment market:

```python
experiment_market = str(experiment.get("market", "")).strip().upper()
market_capital_pool = experiment.get("market_capital_pool")
if not isinstance(market_capital_pool, dict):
    market_capital_pool = {}
```

Before appending an intent, skip lifecycle states outside the experiment market:

```python
if experiment_market and market != experiment_market:
    continue
```

Add fields to the intent dict:

```python
"experiment_market": experiment_market,
"market_capital_pool": copy.deepcopy(market_capital_pool),
```

Import `copy` at the top of `src/open_trader/kelly_order_intents.py`.

- [ ] **Step 4: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_intents.py -q
```

Expected: all intent tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/kelly_order_intents.py tests/test_kelly_order_intents.py
git commit -m "feat: carry kelly experiment market in order intents"
```

---

### Task 4: Block Cross-Market and Currency Risk Checks

**Files:**
- Modify: `src/open_trader/kelly_order_risk.py`
- Test: `tests/test_kelly_order_risk.py`

- [ ] **Step 1: Write failing risk tests**

Append:

```python
def test_build_kelly_order_risk_checks_blocks_cross_market_entry() -> None:
    payload = build_kelly_order_risk_checks_payload(
        {
            "schema_version": "open_trader.kelly_order_intents.v1",
            "created_at": "2026-07-10 13:30",
            "intent_count": 1,
            "intents": [
                {
                    "intent_id": "trend:HK:02840:entry",
                    "experiment_id": "trend_us",
                    "experiment_name": "趋势回调 US",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "experiment_market": "US",
                    "market": "HK",
                    "symbol": "02840",
                    "intent_type": "entry",
                    "side": "buy",
                    "suggested_position_pct": "4%",
                    "per_symbol_budget": "25000",
                    "budget_currency": "USD",
                }
            ],
        },
        checked_at="2026-07-10 13:31",
    )

    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["risk_status"] == "blocked"
    assert payload["checks"][0]["execution_status"] == "risk_blocked"
    assert payload["checks"][0]["reason"] == "market scope checks failed"
    assert payload["checks"][0]["check_results"][0] == {
        "check": "experiment_market_matches_symbol",
        "status": "failed",
        "detail": "HK != US",
    }


def test_build_kelly_order_risk_checks_blocks_market_currency_mismatch() -> None:
    payload = build_kelly_order_risk_checks_payload(
        {
            "schema_version": "open_trader.kelly_order_intents.v1",
            "created_at": "2026-07-10 13:30",
            "intent_count": 1,
            "intents": [
                {
                    "intent_id": "trend:HK:02840:entry",
                    "experiment_id": "trend_hk",
                    "experiment_name": "趋势回调 HK",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "experiment_market": "HK",
                    "market": "HK",
                    "symbol": "02840",
                    "intent_type": "entry",
                    "side": "buy",
                    "suggested_position_pct": "4%",
                    "per_symbol_budget": "25000",
                    "budget_currency": "USD",
                }
            ],
        },
        checked_at="2026-07-10 13:31",
    )

    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["check_results"][1] == {
        "check": "budget_currency_matches_market",
        "status": "failed",
        "detail": "USD != HKD",
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_risk.py::test_build_kelly_order_risk_checks_blocks_cross_market_entry tests/test_kelly_order_risk.py::test_build_kelly_order_risk_checks_blocks_market_currency_mismatch -q
```

Expected: FAIL because risk checks do not yet evaluate experiment market or market currency.

- [ ] **Step 3: Implement market scope checks**

In `src/open_trader/kelly_order_risk.py`, import:

```python
from .kelly_market_rules import kelly_market_currency, normalize_kelly_market
```

Add this helper:

```python
def _market_scope_check_results(intent: dict[str, Any]) -> list[dict[str, str]]:
    market = str(intent.get("market", "")).strip().upper()
    experiment_market = str(intent.get("experiment_market", "")).strip().upper()
    budget_currency = str(intent.get("budget_currency", "")).strip().upper()

    results: list[dict[str, str]] = []
    try:
        normalized_market = normalize_kelly_market(market)
        normalized_experiment_market = normalize_kelly_market(experiment_market)
    except ValueError:
        results.append(
            {
                "check": "experiment_market_present",
                "status": "failed",
                "detail": experiment_market or "-",
            }
        )
        return results

    market_matches = normalized_market == normalized_experiment_market
    results.append(
        {
            "check": "experiment_market_matches_symbol",
            "status": "passed" if market_matches else "failed",
            "detail": (
                f"{normalized_market} == {normalized_experiment_market}"
                if market_matches
                else f"{normalized_market} != {normalized_experiment_market}"
            ),
        }
    )

    expected_currency = kelly_market_currency(normalized_market)
    currency_matches = budget_currency == expected_currency
    results.append(
        {
            "check": "budget_currency_matches_market",
            "status": "passed" if currency_matches else "failed",
            "detail": (
                f"{budget_currency} == {expected_currency}"
                if currency_matches
                else f"{budget_currency} != {expected_currency}"
            ),
        }
    )
    return results
```

At the start of `_build_single_check`, after `base` is created and before exit/entry branching, run:

```python
market_scope_results = _market_scope_check_results(intent)
if any(result["status"] == "failed" for result in market_scope_results):
    return {
        **base,
        "risk_status": "blocked",
        "execution_status": "risk_blocked",
        "planned_notional": "",
        "budget_currency": str(intent.get("budget_currency", "")).strip(),
        "reason": "market scope checks failed",
        "check_results": market_scope_results,
    }
```

For approved checks, prepend `market_scope_results` to the existing `check_results` lists so operators see why the order is in scope.

- [ ] **Step 4: Update expected existing tests**

In `test_build_kelly_order_risk_checks_approves_valid_entry_and_exit`, add `experiment_market` and correct HK currency:

```python
"experiment_market": "US",
```

for the US entry and:

```python
"experiment_market": "HK",
"budget_currency": "HKD",
```

for the HK exit.

Update expected `check_results` to include:

```python
{
    "check": "experiment_market_matches_symbol",
    "status": "passed",
    "detail": "US == US",
},
{
    "check": "budget_currency_matches_market",
    "status": "passed",
    "detail": "USD == USD",
},
```

Use the corresponding `HK == HK` and `HKD == HKD` rows for the HK exit.

- [ ] **Step 5: Run risk tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_risk.py -q
```

Expected: all risk tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/kelly_order_risk.py tests/test_kelly_order_risk.py
git commit -m "feat: block cross market kelly orders"
```

---

### Task 5: Render Market and Capital Pool in Kelly Lab UI

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/e2e/kelly-lab.spec.ts`
- Test fixture: `tests/e2e/fixtures/kelly-dashboard.json`

- [ ] **Step 1: Write failing UI tests**

In `tests/e2e/kelly-lab.spec.ts`, update expected tab names and add assertions:

```typescript
await expect(page.getByRole('tab', { name: /趋势回调 20D Mock US 第一批/ })).toHaveAttribute('aria-selected', 'true');
await expect(page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ })).toHaveAttribute('aria-selected', 'false');
await expect(page.getByText('市场')).toBeVisible();
await expect(page.getByText('US', { exact: true })).toBeVisible();
await expect(page.getByText('模拟资金池')).toBeVisible();
await expect(page.getByText('USD 100000')).toBeVisible();
```

After clicking the HK tab, assert:

```typescript
await page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ }).click();
await expect(page.getByText('HK', { exact: true })).toBeVisible();
await expect(page.getByText('HKD 500000')).toBeVisible();
await expect(page.getByLabel('Kelly 标的状态').getByText('HK.02840')).toBeVisible();
await expect(page.getByLabel('Kelly 标的状态').getByText('US.DRAM')).toHaveCount(0);
```

Add a no-editable-controls assertion:

```typescript
await expect(page.getByLabel('Kelly 标的状态').getByRole('checkbox')).toHaveCount(0);
await expect(page.getByRole('button', { name: /新策略|保存配置|添加标的/ })).toHaveCount(0);
```

- [ ] **Step 2: Run Playwright to verify failure**

Run:

```bash
npx playwright test tests/e2e/kelly-lab.spec.ts
```

Expected: FAIL because current fixture/tab names and UI do not show market capital pool.

- [ ] **Step 3: Update dashboard rendering**

In `src/open_trader/dashboard_static/dashboard.js`, locate the Kelly experiment summary card rendering. Add two metric tiles or metadata rows:

```javascript
const market = valueOrDash(experiment.market);
const pool = experiment.market_capital_pool || {};
const capitalPoolText = pool.currency && pool.amount
  ? `${pool.currency} ${pool.amount}`
  : `${valueOrDash(experiment.budget_currency)} ${valueOrDash(experiment.experiment_budget)}`;
```

Render:

```javascript
<div class="kelly-meta-grid">
  <div class="metric-card">
    <span>市场</span>
    <strong>${escapeHtml(market)}</strong>
  </div>
  <div class="metric-card">
    <span>模拟资金池</span>
    <strong>${escapeHtml(capitalPoolText)}</strong>
  </div>
</div>
```

Use the file's existing escaping/render helper names. If they differ from `escapeHtml` or `valueOrDash`, use the existing local helper instead of adding duplicate helpers.

- [ ] **Step 4: Update CSS only if spacing breaks**

If the metric tiles need styling, add to `src/open_trader/dashboard_static/dashboard.css`:

```css
.kelly-meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0;
}
```

Use existing card/metric styles if available; do not add a new visual system.

- [ ] **Step 5: Update E2E fixture**

Update `tests/e2e/fixtures/kelly-dashboard.json` to mirror the split experiments from Task 2:

- US trend-pullback experiment contains only `US.DRAM`, `US.RAM`, `US.SOXX`.
- HK trend-pullback experiment contains only `HK.02840`.
- Breakout experiment remains US-only and uses `market: "US"`, `budget_currency: "USD"`.
- Every experiment has `market_capital_pool`.

- [ ] **Step 6: Run UI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
npx playwright test tests/e2e/kelly-lab.spec.ts
```

Expected: pytest and Playwright pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/test_dashboard.py tests/e2e/fixtures/kelly-dashboard.json tests/e2e/kelly-lab.spec.ts
git commit -m "feat: show kelly market capital pools"
```

---

### Task 6: Final Verification and Changelog

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run focused automated tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_kelly_order_intents.py tests/test_kelly_order_risk.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run Playwright**

Run:

```bash
npx playwright test tests/e2e/kelly-lab.spec.ts
```

Expected: Kelly lab E2E passes and shows separate US/HK experiments.

- [ ] **Step 3: Compile touched Python modules**

Run:

```bash
.venv/bin/python -m py_compile src/open_trader/kelly_market_rules.py src/open_trader/kelly_lab.py src/open_trader/kelly_order_intents.py src/open_trader/kelly_order_risk.py
```

Expected: command exits 0 with no output.

- [ ] **Step 4: Check diff hygiene**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. `git status --short` should show only `CHANGELOG.md` before the changelog commit.

- [ ] **Step 5: Update changelog**

Add under `## 2026-07-10` in `CHANGELOG.md`:

```markdown
- Enforced single-market Kelly paper experiments with fixed US/HK/CN simulated
  capital pools, split mixed-market mock data, and blocked cross-market order
  intents before execution.
```

- [ ] **Step 6: Commit changelog**

```bash
git add CHANGELOG.md
git commit -m "docs: update changelog for kelly market rules"
```

- [ ] **Step 7: Final status**

Run:

```bash
git status --short --branch
git log --oneline -6
```

Expected: clean working tree and recent commits for each task.

---

## Self-Review

- Spec coverage: covered single experiment market, participant market matching,
  fixed US/HK/CN pools, CN disabled, no editable participant controls, order
  intent propagation, risk blocking, UI display, and Playwright verification.
- Deferred-language scan: plan does not use vague implementation language; each
  task includes concrete files, code snippets, commands, and expected outcomes.
- Type consistency: `market`, `experiment_market`, `market_capital_pool`,
  `budget_currency`, and participant fields use the same names across loader,
  intents, risk, and UI tasks.
