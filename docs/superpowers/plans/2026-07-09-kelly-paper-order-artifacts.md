# Kelly Paper Order Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load local `data/latest/kelly_paper_orders.json` and attach its orders to each Kelly experiment's `order_sync.orders`.

**Architecture:** Keep strategy templates and experiments as the primary required artifacts. Treat `kelly_paper_orders.json` as optional in this phase: missing file means no injected order details, while malformed schema raises a Kelly Lab unavailable error like other invalid artifacts. Orders are grouped by `experiment_id` and only attached to the matching experiment.

**Tech Stack:** Python JSON artifact loader, existing `kelly_lab.py`, pytest, existing dashboard and Playwright verification.

---

## Files

- Modify: `src/open_trader/kelly_lab.py`
  - Add `PAPER_ORDERS_SCHEMA_VERSION`.
  - Add optional loader for `kelly_paper_orders.json`.
  - Validate orders list shape lightly.
  - Group orders by `experiment_id`.
  - Attach grouped orders to `experiment["order_sync"]["orders"]`.

- Modify: `tests/test_kelly_lab.py`
  - Add backend tests for optional missing artifact and successful injection.

- Modify: `data/latest/kelly_paper_orders.json`
  - Add local mock artifact for the current dashboard demo data.

## Task 1: Backend Tests

**Files:**
- Modify: `tests/test_kelly_lab.py`

- [ ] **Step 1: Add a failing test for paper order injection**

Add this test after `test_load_kelly_lab_state_filters_manual_lifecycle_states_to_participants`:

```python
def test_load_kelly_lab_state_attaches_paper_orders_by_experiment_id(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
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
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第一批",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
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
                            "market": "US",
                            "symbol": "RAM",
                            "name": "RAM ETF",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        }
                    ],
                    "order_sync": {
                        "status": "success",
                        "environment": "SIMULATE",
                        "last_synced_at": "2026-07-08 10:08",
                        "order_count": 1,
                        "fill_count": 1,
                    },
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
    write_json(
        data_dir / "latest" / "kelly_paper_orders.json",
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "orders": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "submitted_at": "2026-07-08 10:01",
                    "order_price": "12.34",
                    "order_qty": "800",
                    "filled_qty": "800",
                    "avg_fill_price": "12.34",
                    "status": "filled",
                    "order_id": "SIM-10001",
                },
                {
                    "experiment_id": "other_experiment",
                    "market": "US",
                    "symbol": "MSFT",
                    "side": "buy",
                    "order_id": "SIM-99999",
                },
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    orders = state["experiments"][0]["order_sync"]["orders"]
    assert orders == [
        {
            "experiment_id": "trend_pullback_20d_exp_20260707",
            "market": "US",
            "symbol": "RAM",
            "side": "buy",
            "submitted_at": "2026-07-08 10:01",
            "order_price": "12.34",
            "order_qty": "800",
            "filled_qty": "800",
            "avg_fill_price": "12.34",
            "status": "filled",
            "order_id": "SIM-10001",
        }
    ]
```

- [ ] **Step 2: Add a test that missing paper orders remains optional**

Add:

```python
def test_load_kelly_lab_state_keeps_existing_order_sync_when_paper_orders_missing(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "breakout_10d",
                    "strategy_name": "突破 10D",
                    "strategy_version": "v1",
                    "entry_rule_description": "突破区间。",
                    "exit_rule_description": "跌回突破位或 10 天到期。",
                    "max_holding_days": 10,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "breakout_10d_exp_20260707",
                    "experiment_name": "突破 10D 第一批",
                    "strategy_id": "breakout_10d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "50000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "40",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "MSFT",
                            "name": "Microsoft",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "20000",
                            "budget_currency": "USD",
                        }
                    ],
                    "order_sync": {
                        "status": "success",
                        "environment": "SIMULATE",
                        "order_count": 0,
                        "fill_count": 0,
                    },
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

    assert state["available"] is True
    assert state["experiments"][0]["order_sync"] == {
        "status": "success",
        "environment": "SIMULATE",
        "order_count": 0,
        "fill_count": 0,
    }
```

- [ ] **Step 3: Run tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_attaches_paper_orders_by_experiment_id tests/test_kelly_lab.py::test_load_kelly_lab_state_keeps_existing_order_sync_when_paper_orders_missing -q
```

Expected: first test fails because `kelly_paper_orders.json` is not loaded; second test passes.

## Task 2: Artifact Loader Implementation

**Files:**
- Modify: `src/open_trader/kelly_lab.py`

- [ ] **Step 1: Add schema constant**

Add near existing schema constants:

```python
PAPER_ORDERS_SCHEMA_VERSION = "open_trader.kelly_paper_orders.v1"
```

- [ ] **Step 2: Load optional paper orders in `load_kelly_lab_state`**

In `load_kelly_lab_state`, add:

```python
paper_orders_path = latest_dir / "kelly_paper_orders.json"
```

After experiments are validated, add:

```python
paper_orders = _load_optional_paper_orders(paper_orders_path)
experiments = _attach_paper_orders_to_experiments(experiments, paper_orders)
```

- [ ] **Step 3: Add helper functions**

Add below `_load_json_object`:

```python
def _load_optional_paper_orders(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = _load_json_object(path)
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=PAPER_ORDERS_SCHEMA_VERSION,
    )
    orders = payload.get("orders")
    if not isinstance(orders, list):
        raise ValueError(f"{path.name} must contain an orders list")
    validated: list[dict[str, Any]] = []
    for index, order in enumerate(orders):
        if not isinstance(order, dict):
            raise ValueError(f"{path.name} order {index} must be an object")
        experiment_id = order.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError(f"{path.name} order {index} has invalid experiment_id")
        normalized = copy.deepcopy(order)
        for key in ("market", "symbol", "side", "status"):
            if isinstance(normalized.get(key), str):
                normalized[key] = normalized[key].strip()
        validated.append(normalized)
    return validated


def _attach_paper_orders_to_experiments(
    experiments: list[dict[str, Any]],
    paper_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not paper_orders:
        return experiments
    orders_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for order in paper_orders:
        experiment_id = order.get("experiment_id")
        if not isinstance(experiment_id, str):
            continue
        orders_by_experiment.setdefault(experiment_id, []).append(copy.deepcopy(order))

    attached: list[dict[str, Any]] = []
    for experiment in experiments:
        normalized = copy.deepcopy(experiment)
        experiment_id = normalized.get("experiment_id")
        if isinstance(experiment_id, str) and experiment_id in orders_by_experiment:
            order_sync = normalized.get("order_sync")
            if not isinstance(order_sync, dict):
                order_sync = {}
            else:
                order_sync = copy.deepcopy(order_sync)
            order_sync["orders"] = orders_by_experiment[experiment_id]
            normalized["order_sync"] = order_sync
        attached.append(normalized)
    return attached
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py -q
```

Expected: PASS.

## Task 3: Local Artifact And Verification

**Files:**
- Create: `data/latest/kelly_paper_orders.json`

- [ ] **Step 1: Add local mock artifact**

Create `data/latest/kelly_paper_orders.json` with:

```json
{
  "schema_version": "open_trader.kelly_paper_orders.v1",
  "orders": [
    {
      "experiment_id": "trend_pullback_20d_mock_20260707",
      "market": "US",
      "symbol": "RAM",
      "side": "buy",
      "submitted_at": "2026-07-08 10:01",
      "order_price": "12.34",
      "order_qty": "800",
      "filled_qty": "800",
      "avg_fill_price": "12.34",
      "status": "filled",
      "order_id": "SIM-10001"
    },
    {
      "experiment_id": "trend_pullback_20d_mock_20260707",
      "market": "HK",
      "symbol": "02840",
      "side": "sell",
      "submitted_at": "2026-07-08 10:03",
      "order_price": "218.80",
      "order_qty": "100",
      "filled_qty": "0",
      "avg_fill_price": "-",
      "status": "submitted",
      "order_id": "SIM-10002"
    },
    {
      "experiment_id": "breakout_10d_mock_20260707",
      "market": "US",
      "symbol": "MSFT",
      "side": "buy",
      "submitted_at": "2026-07-08 10:05",
      "order_price": "505.10",
      "order_qty": "20",
      "filled_qty": "0",
      "avg_fill_price": "-",
      "status": "rejected",
      "order_id": "SIM-20001"
    }
  ]
}
```

- [ ] **Step 2: Verify API output**

Run:

```bash
.venv/bin/python - <<'PY'
from open_trader.kelly_lab import load_kelly_lab_state
from pathlib import Path
state = load_kelly_lab_state(Path("data")).to_dict()
for experiment in state["experiments"]:
    print(experiment["experiment_name"], [order["order_id"] for order in experiment.get("order_sync", {}).get("orders", [])])
PY
```

Expected:

```text
趋势回调 20D Mock 第一批 ['SIM-10001', 'SIM-10002']
突破 10D Mock 第一批 ['SIM-20001']
```

## Task 4: Regression, Commit, Deploy

**Files:**
- Commit `src/open_trader/kelly_lab.py`, `tests/test_kelly_lab.py`, and `data/latest/kelly_paper_orders.json` if tracked by project policy.

- [ ] **Step 1: Run focused and relevant tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
git diff --check
```

Expected: all pass and no diff whitespace output.

- [ ] **Step 2: Commit implementation**

Run:

```bash
git add src/open_trader/kelly_lab.py tests/test_kelly_lab.py data/latest/kelly_paper_orders.json
git commit -m "feat: load kelly paper order artifacts"
```

- [ ] **Step 3: Restart dashboard and browser verify**

Run:

```bash
kill $(lsof -tiTCP:8766 -sTCP:LISTEN) 2>/dev/null || true
sleep 1
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766'
node - <<'NODE'
const { chromium, expect } = require('@playwright/test');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1800, height: 900 } });
  await page.goto('http://127.0.0.1:8766/', { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '凯利实验室' }).click();
  await expect(page.getByLabel('Kelly 订单同步').getByText('SIM-10001')).toBeVisible();
  await page.getByRole('tab', { name: /突破 10D Mock 第一批/ }).click();
  await expect(page.getByLabel('Kelly 订单同步').getByText('SIM-20001')).toBeVisible();
  await browser.close();
})();
NODE
```

Expected: script exits 0.
