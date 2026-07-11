# Kelly Strategy Capital Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build strategy-level Kelly capital snapshots, use them in order risk checks, and render the approved capital UI at the top of each Kelly strategy tab.

**Architecture:** Add a focused backend module that computes capital by `experiment_id` from experiments, order executions, and synced paper orders. Attach the computed snapshot to each Kelly experiment in `load_kelly_lab_state()`, pass it into order risk checks, then render `experiment.capital` in the existing Kelly Lab strategy card.

**Tech Stack:** Python stdlib JSON/Decimal, existing `open_trader` CLI/data artifact patterns, vanilla dashboard JavaScript/CSS, pytest, Playwright.

---

## File Structure

- Create `src/open_trader/kelly_strategy_capital.py`
  - Owns capital snapshot calculation and `kelly_strategy_capital.json` writing.
  - Public functions:
    - `build_kelly_strategy_capital_payload(experiments, paper_orders_payload=None, order_executions_payload=None, calculated_at=None)`
    - `write_kelly_strategy_capital(data_dir, payload)`
    - `load_kelly_strategy_capital(data_dir)`
- Create `tests/test_kelly_strategy_capital.py`
  - Unit tests for capital calculation, pending reservations, filled positions, sells, realized P/L, and artifact writing.
- Modify `src/open_trader/kelly_lab.py`
  - Load optional `latest/kelly_strategy_capital.json`.
  - Attach matching `capital` object to each experiment.
  - If the artifact is missing, synthesize an unavailable capital object so the UI has a stable fallback.
- Modify `src/open_trader/kelly_order_risk.py`
  - Accept optional strategy capital snapshots.
  - Add `strategy_available_capital` check for buy/entry intents.
  - Preserve exit behavior.
- Modify `src/open_trader/cli.py`
  - Add `open-trader kelly build-strategy-capital`.
  - Wire `kelly build-order-risk-checks` to use latest strategy capital when present.
- Modify `src/open_trader/dashboard_static/dashboard.js`
  - Add `renderKellyStrategyCapital(experiment)`.
  - Insert it near the top of `renderKellyExperimentCard()` before order execution/sync/rules.
- Modify `src/open_trader/dashboard_static/dashboard.css`
  - Add compact capital panel, metric grid, utilization bar, and responsive wrapping styles.
- Modify `tests/test_kelly_lab.py`, `tests/test_kelly_order_risk.py`, `tests/test_dashboard_web.py`, `tests/e2e/kelly-lab.spec.ts`, and `tests/e2e/fixtures/kelly-dashboard.json`.
- Modify `CHANGELOG.md` after implementation.

---

## Task 1: Backend Capital Snapshot Artifact

**Files:**
- Create: `src/open_trader/kelly_strategy_capital.py`
- Create: `tests/test_kelly_strategy_capital.py`

- [ ] **Step 1: Write failing test for empty strategy capital**

Add this test to `tests/test_kelly_strategy_capital.py`:

```python
from __future__ import annotations

from pathlib import Path

from open_trader.kelly_strategy_capital import (
    build_kelly_strategy_capital_payload,
    write_kelly_strategy_capital,
)


def test_build_kelly_strategy_capital_payload_initializes_empty_experiment() -> None:
    payload = build_kelly_strategy_capital_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "experiment_budget": "30000",
                "budget_currency": "USD",
                "participants": [
                    {"market": "US", "symbol": "RAM"},
                    {"market": "US", "symbol": "SOXX"},
                ],
            }
        ],
        calculated_at="2026-07-10 21:00",
    )

    assert payload == {
        "schema_version": "open_trader.kelly_strategy_capital.v1",
        "calculated_at": "2026-07-10 21:00",
        "strategy_count": 1,
        "strategies": [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "currency": "USD",
                "budget": "30000",
                "occupied_notional": "0",
                "position_notional": "0",
                "reserved_order_notional": "0",
                "available_notional": "30000",
                "utilization_pct": "0",
                "open_buy_order_count": 0,
                "realized_pnl": "0",
                "updated_at": "2026-07-10 21:00",
                "symbol_occupancy": [],
                "next_order_impact": {},
            }
        ],
    }
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital.py::test_build_kelly_strategy_capital_payload_initializes_empty_experiment -q
```

Expected: fail with `ModuleNotFoundError: No module named 'open_trader.kelly_strategy_capital'`.

- [ ] **Step 3: Implement minimal capital module**

Create `src/open_trader/kelly_strategy_capital.py` with:

```python
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


STRATEGY_CAPITAL_SCHEMA_VERSION = "open_trader.kelly_strategy_capital.v1"


def build_kelly_strategy_capital_payload(
    experiments: list[dict[str, Any]],
    *,
    paper_orders_payload: dict[str, Any] | None = None,
    order_executions_payload: dict[str, Any] | None = None,
    calculated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = calculated_at or _current_timestamp()
    strategies = [
        _empty_strategy_capital(experiment, updated_at=timestamp)
        for experiment in experiments
        if isinstance(experiment, dict) and str(experiment.get("experiment_id", "")).strip()
    ]
    return {
        "schema_version": STRATEGY_CAPITAL_SCHEMA_VERSION,
        "calculated_at": timestamp,
        "strategy_count": len(strategies),
        "strategies": strategies,
    }


def write_kelly_strategy_capital(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_strategy_capital.json"
    _write_json_atomic(path, payload)
    return path


def load_kelly_strategy_capital(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_strategy_capital.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != STRATEGY_CAPITAL_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {STRATEGY_CAPITAL_SCHEMA_VERSION!r}",
        )
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        raise ValueError(f"{path.name} must contain a strategies list")
    return payload


def _empty_strategy_capital(
    experiment: dict[str, Any],
    *,
    updated_at: str,
) -> dict[str, Any]:
    budget = _parse_decimal(experiment.get("experiment_budget")) or Decimal("0")
    currency = str(experiment.get("budget_currency", "")).strip().upper()
    return {
        "experiment_id": str(experiment.get("experiment_id", "")).strip(),
        "experiment_name": str(experiment.get("experiment_name", "")).strip(),
        "market": str(experiment.get("market", "")).strip().upper(),
        "currency": currency,
        "budget": _decimal_text(budget),
        "occupied_notional": "0",
        "position_notional": "0",
        "reserved_order_notional": "0",
        "available_notional": _decimal_text(budget),
        "utilization_pct": "0",
        "open_buy_order_count": 0,
        "realized_pnl": "0",
        "updated_at": updated_at,
        "symbol_occupancy": [],
        "next_order_impact": {},
    }


def _parse_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip().replace(",", "").rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
```

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital.py::test_build_kelly_strategy_capital_payload_initializes_empty_experiment -q
```

Expected: `1 passed`.

- [ ] **Step 5: Write failing test for reserved buy orders and positions**

Add:

```python
def test_build_kelly_strategy_capital_payload_counts_reserved_orders_and_positions() -> None:
    payload = build_kelly_strategy_capital_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "experiment_budget": "30000",
                "budget_currency": "USD",
            }
        ],
        paper_orders_payload={
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "status": "submitted",
                    "limit_price": "150",
                    "quantity": "8",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "SOXX",
                    "side": "buy",
                    "status": "filled",
                    "filled_avg_price": "620",
                    "filled_qty": "10",
                },
            ]
        },
        calculated_at="2026-07-10 21:05",
    )

    capital = payload["strategies"][0]
    assert capital["reserved_order_notional"] == "1200"
    assert capital["position_notional"] == "6200"
    assert capital["occupied_notional"] == "7400"
    assert capital["available_notional"] == "22600"
    assert capital["utilization_pct"] == "24.67"
    assert capital["open_buy_order_count"] == 1
    assert capital["symbol_occupancy"] == [
        {"market": "US", "symbol": "RAM", "notional": "1200"},
        {"market": "US", "symbol": "SOXX", "notional": "6200"},
    ]
```

- [ ] **Step 6: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital.py::test_build_kelly_strategy_capital_payload_counts_reserved_orders_and_positions -q
```

Expected: fail because paper orders are not counted yet.

- [ ] **Step 7: Implement order aggregation**

Update `kelly_strategy_capital.py`:

```python
OPEN_BUY_STATUSES = {"pending", "submitted", "submitting", "partially_filled"}
FILLED_STATUSES = {"filled"}


def build_kelly_strategy_capital_payload(
    experiments: list[dict[str, Any]],
    *,
    paper_orders_payload: dict[str, Any] | None = None,
    order_executions_payload: dict[str, Any] | None = None,
    calculated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = calculated_at or _current_timestamp()
    orders = _payload_items(paper_orders_payload, "orders")
    strategies = [
        _strategy_capital(experiment, orders=orders, updated_at=timestamp)
        for experiment in experiments
        if isinstance(experiment, dict) and str(experiment.get("experiment_id", "")).strip()
    ]
    return {
        "schema_version": STRATEGY_CAPITAL_SCHEMA_VERSION,
        "calculated_at": timestamp,
        "strategy_count": len(strategies),
        "strategies": strategies,
    }


def _strategy_capital(
    experiment: dict[str, Any],
    *,
    orders: list[dict[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    capital = _empty_strategy_capital(experiment, updated_at=updated_at)
    experiment_id = capital["experiment_id"]
    currency = capital["currency"]
    reserved = Decimal("0")
    positions: dict[tuple[str, str], Decimal] = {}
    open_buy_count = 0

    for order in orders:
        if str(order.get("experiment_id", "")).strip() != experiment_id:
            continue
        side = str(order.get("side", "")).strip().lower()
        status = str(order.get("status", "")).strip().lower()
        market = str(order.get("market", "")).strip().upper()
        symbol = str(order.get("symbol", "")).strip().upper()
        if side != "buy" or not market or not symbol:
            continue
        if status in OPEN_BUY_STATUSES:
            notional = _order_notional(order)
            reserved += notional
            positions[(market, symbol)] = positions.get((market, symbol), Decimal("0")) + notional
            open_buy_count += 1
        elif status in FILLED_STATUSES:
            notional = _filled_notional(order)
            positions[(market, symbol)] = positions.get((market, symbol), Decimal("0")) + notional

    position_notional = sum(positions.values(), Decimal("0")) - reserved
    occupied = position_notional + reserved
    budget = _parse_decimal(capital["budget"]) or Decimal("0")
    available = max(Decimal("0"), budget - occupied)
    utilization_pct = (occupied / budget * Decimal("100")) if budget else Decimal("0")

    capital.update(
        {
            "occupied_notional": _decimal_text(occupied),
            "position_notional": _decimal_text(position_notional),
            "reserved_order_notional": _decimal_text(reserved),
            "available_notional": _decimal_text(available),
            "utilization_pct": _decimal_text(utilization_pct.quantize(Decimal("0.01"))),
            "open_buy_order_count": open_buy_count,
            "symbol_occupancy": [
                {"market": market, "symbol": symbol, "notional": _decimal_text(notional)}
                for (market, symbol), notional in sorted(positions.items())
                if notional > 0
            ],
        }
    )
    return capital
```

Add helpers:

```python
def _payload_items(payload: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get(key)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _order_notional(order: dict[str, Any]) -> Decimal:
    quantity = _parse_decimal(order.get("quantity") or order.get("order_qty")) or Decimal("0")
    price = _parse_decimal(order.get("limit_price") or order.get("price")) or Decimal("0")
    return quantity * price


def _filled_notional(order: dict[str, Any]) -> Decimal:
    quantity = _parse_decimal(order.get("filled_qty") or order.get("quantity") or order.get("order_qty")) or Decimal("0")
    price = _parse_decimal(order.get("filled_avg_price") or order.get("avg_price") or order.get("limit_price") or order.get("price")) or Decimal("0")
    return quantity * price
```

- [ ] **Step 8: Run capital tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/open_trader/kelly_strategy_capital.py tests/test_kelly_strategy_capital.py
git commit -m "feat: build kelly strategy capital snapshots"
```

---

## Task 2: Attach Capital To Kelly Lab State

**Files:**
- Modify: `src/open_trader/kelly_lab.py`
- Modify: `tests/test_kelly_lab.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_kelly_lab.py`:

```python
def test_load_kelly_lab_state_attaches_strategy_capital_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(data_dir / "latest" / "kelly_strategy_templates.json", minimal_template_payload())
    write_json(data_dir / "latest" / "kelly_experiments.json", minimal_experiment_payload())
    write_json(
        data_dir / "latest" / "kelly_strategy_capital.json",
        {
            "schema_version": "open_trader.kelly_strategy_capital.v1",
            "calculated_at": "2026-07-10 21:10",
            "strategy_count": 1,
            "strategies": [
                {
                    "experiment_id": "trend_us",
                    "currency": "USD",
                    "budget": "30000",
                    "occupied_notional": "7400",
                    "position_notional": "6200",
                    "reserved_order_notional": "1200",
                    "available_notional": "22600",
                    "utilization_pct": "24.67",
                    "open_buy_order_count": 1,
                    "realized_pnl": "0",
                    "updated_at": "2026-07-10 21:10",
                    "symbol_occupancy": [{"market": "US", "symbol": "RAM", "notional": "1200"}],
                    "next_order_impact": {},
                }
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    assert state["experiments"][0]["capital"]["available_notional"] == "22600"
    assert state["experiments"][0]["capital"]["open_buy_order_count"] == 1
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_attaches_strategy_capital_snapshot -q
```

Expected: fail because `capital` is missing.

- [ ] **Step 3: Implement optional capital loading**

Modify `src/open_trader/kelly_lab.py`:

- Add schema constant:

```python
STRATEGY_CAPITAL_SCHEMA_VERSION = "open_trader.kelly_strategy_capital.v1"
```

- In `load_kelly_lab_state()` after order execution attachment:

```python
strategy_capital = _load_optional_strategy_capital(latest_dir / "kelly_strategy_capital.json")
experiments = _attach_strategy_capital_to_experiments(experiments, strategy_capital)
```

- Add:

```python
def _load_optional_strategy_capital(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = _load_json_object(path)
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=STRATEGY_CAPITAL_SCHEMA_VERSION,
    )
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        raise ValueError(f"{path.name} must contain a strategies list")
    return [copy.deepcopy(item) for item in strategies if isinstance(item, dict)]


def _attach_strategy_capital_to_experiments(
    experiments: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_experiment = {
        str(item.get("experiment_id", "")).strip(): item
        for item in strategies
        if str(item.get("experiment_id", "")).strip()
    }
    attached = []
    for experiment in experiments:
        normalized = copy.deepcopy(experiment)
        experiment_id = str(normalized.get("experiment_id", "")).strip()
        normalized["capital"] = copy.deepcopy(by_experiment.get(experiment_id, {"available": False}))
        attached.append(normalized)
    return attached
```

- [ ] **Step 4: Run test and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_attaches_strategy_capital_snapshot -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/open_trader/kelly_lab.py tests/test_kelly_lab.py
git commit -m "feat: attach kelly strategy capital to lab state"
```

---

## Task 3: Risk Check Blocks Insufficient Strategy Capital

**Files:**
- Modify: `src/open_trader/kelly_order_risk.py`
- Modify: `tests/test_kelly_order_risk.py`

- [ ] **Step 1: Write failing test**

Add:

```python
def test_build_kelly_order_risk_checks_blocks_entry_when_strategy_capital_insufficient() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={
            "strategies": [
                {
                    "experiment_id": "trend",
                    "currency": "USD",
                    "available_notional": "500",
                }
            ]
        },
    )

    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["planned_notional"] == "1000"
    assert check["reason"] == "entry risk checks failed"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "failed",
        "detail": "1000 <= 500 USD",
    }
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_risk.py::test_build_kelly_order_risk_checks_blocks_entry_when_strategy_capital_insufficient -q
```

Expected: fail because `strategy_capital_payload` is not accepted.

- [ ] **Step 3: Implement optional capital risk check**

Update signatures in `src/open_trader/kelly_order_risk.py`:

```python
def build_kelly_order_risk_checks(
    data_dir: Path,
    *,
    checked_at: str | None = None,
    max_entry_position_pct: str = "4",
    strategy_capital_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

```python
def build_kelly_order_risk_checks_payload(
    intent_payload: dict[str, Any],
    *,
    checked_at: str | None = None,
    max_entry_position_pct: str = "4",
    strategy_capital_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

Build index:

```python
capital_by_experiment = _strategy_capital_by_experiment(strategy_capital_payload)
```

Pass into `_build_single_check()`.

Add when planned notional is available:

```python
capital = capital_by_experiment.get(str(intent.get("experiment_id", "")).strip())
if capital is not None:
    available = _parse_positive_decimal(capital.get("available_notional"))
    currency = str(capital.get("currency") or budget_currency).strip().upper()
    passed = available is not None and planned <= available
    check_results.append(
        {
            "check": "strategy_available_capital",
            "status": "passed" if passed else "failed",
            "detail": f"{_decimal_text(planned)} <= {_decimal_text(available or Decimal('0'))} {currency}",
        }
    )
```

Add helper:

```python
def _strategy_capital_by_experiment(
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return {}
    return {
        str(item.get("experiment_id", "")).strip(): item
        for item in strategies
        if isinstance(item, dict) and str(item.get("experiment_id", "")).strip()
    }
```

- [ ] **Step 4: Run risk tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_risk.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/open_trader/kelly_order_risk.py tests/test_kelly_order_risk.py
git commit -m "feat: block kelly orders on strategy capital"
```

---

## Task 4: CLI Builds And Uses Capital Artifact

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_kelly_order_risk_cli.py` or existing CLI risk test file
- Create if missing: `tests/test_kelly_strategy_capital_cli.py`

- [ ] **Step 1: Write failing parser test**

Create `tests/test_kelly_strategy_capital_cli.py`:

```python
from __future__ import annotations

from open_trader import cli


def test_kelly_build_strategy_capital_parser_accepts_timestamp() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "build-strategy-capital",
            "--calculated-at",
            "2026-07-10 21:20",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "build-strategy-capital"
    assert args.calculated_at == "2026-07-10 21:20"
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital_cli.py::test_kelly_build_strategy_capital_parser_accepts_timestamp -q
```

Expected: parser rejects `build-strategy-capital`.

- [ ] **Step 3: Add CLI command**

In `src/open_trader/cli.py`, import:

```python
from .kelly_strategy_capital import (
    build_kelly_strategy_capital_payload,
    write_kelly_strategy_capital,
)
```

Add subparser:

```python
kelly_build_strategy_capital_parser = kelly_subparsers.add_parser(
    "build-strategy-capital",
    help="Build Kelly strategy capital snapshots.",
)
kelly_build_strategy_capital_parser.add_argument(
    "--calculated-at",
    default=None,
    help="Timestamp to store in the capital snapshot.",
)
```

Handle in `main()`:

```python
if args.kelly_command == "build-strategy-capital":
    state = load_kelly_lab_state(args.data_dir)
    if not state.available:
        raise ValueError(state.error or "Kelly Lab data is not available")
    paper_orders = _load_optional_json(args.data_dir / "latest" / "kelly_paper_orders.json")
    executions = _load_optional_json(args.data_dir / "latest" / "kelly_order_executions.json")
    payload = build_kelly_strategy_capital_payload(
        state.experiments,
        paper_orders_payload=paper_orders,
        order_executions_payload=executions,
        calculated_at=args.calculated_at,
    )
    path = write_kelly_strategy_capital(args.data_dir, payload)
    print(f"strategies: {payload['strategy_count']}")
    print(f"latest: {path}")
    return 0
```

Add helper near other CLI JSON helpers:

```python
def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else None
```

- [ ] **Step 4: Run parser test**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital_cli.py -q
```

Expected: pass.

- [ ] **Step 5: Add test that risk CLI passes latest capital**

Add to the existing Kelly risk CLI test file:

```python
def test_kelly_build_order_risk_checks_passes_strategy_capital_when_available(tmp_path, monkeypatch):
    captured = {}

    def fake_build(data_dir, **kwargs):
        captured["data_dir"] = data_dir
        captured["kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_risk_checks.v1",
            "checked_at": "2026-07-10 21:25",
            "max_entry_position_pct": "4",
            "intent_count": 0,
            "approved_count": 0,
            "blocked_count": 0,
            "checks": [],
        }

    monkeypatch.setattr(cli, "build_kelly_order_risk_checks", fake_build)
    monkeypatch.setattr(cli, "write_kelly_order_risk_checks", lambda data_dir, payload: tmp_path / "data/latest/kelly_order_risk_checks.json")
    (tmp_path / "data/latest").mkdir(parents=True)
    (tmp_path / "data/latest/kelly_strategy_capital.json").write_text('{"strategies":[]}', encoding="utf-8")

    result = cli.main(["kelly", "build-order-risk-checks", "--data-dir", str(tmp_path / "data")])

    assert result == 0
    assert captured["kwargs"]["strategy_capital_payload"] == {"strategies": []}
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital_cli.py tests/test_kelly_order_risk_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/open_trader/cli.py tests/test_kelly_strategy_capital_cli.py tests/test_kelly_order_risk_cli.py
git commit -m "feat: wire kelly strategy capital cli"
```

---

## Task 5: Dashboard Capital UI

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
- Modify: `tests/e2e/kelly-lab.spec.ts`

- [ ] **Step 1: Add failing JS rendering test**

In `tests/test_dashboard_web.py`, add a Node-based test beside existing Kelly render tests:

```python
def test_dashboard_renders_kelly_strategy_capital_panel() -> None:
    script = DASHBOARD_RENDER_TEST_PREFIX + r'''
dashboard = {
  kelly_lab: {
    available: true,
    experiment_count: 1,
    experiments: [{
      experiment_id: "trend_us",
      experiment_name: "趋势回调 20D / US 第一批",
      strategy_id: "trend_pullback_20d",
      strategy_version: "v1",
      market: "US",
      status: "running",
      experiment_budget: "30000",
      budget_currency: "USD",
      capital_utilization_pct: "50",
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        strategy_version: "v1",
        entry_rule_description: "价格回调到 20 日均线附近。",
      },
      stats: { sample_stage: "insufficient" },
      capital: {
        currency: "USD",
        budget: "30000",
        occupied_notional: "8460",
        position_notional: "6200",
        reserved_order_notional: "2260",
        available_notional: "21540",
        utilization_pct: "28.2",
        open_buy_order_count: 2,
        realized_pnl: "420",
        updated_at: "2026-07-10 21:30",
        symbol_occupancy: [{ market: "US", symbol: "RAM", notional: "3720" }],
        next_order_impact: {
          market: "US",
          symbol: "RAM",
          estimated_notional: "1200",
          available_after_order: "20340",
          risk_status: "approved",
          reason: "capital is sufficient",
        },
      },
    }],
  },
};
state.workspaceView = "kelly_lab";
const html = renderKellyLabPanel();
for (const required of ["策略资金", "总资金", "USD 30,000", "可用资金", "USD 21,540", "已占用", "USD 8,460", "下一笔下单影响", "US.RAM", "资金足够"]) {
  if (!html.includes(required)) {
    throw new Error(`missing ${required}`);
  }
}
'''
    run_node_script(script)
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_kelly_strategy_capital_panel -q
```

Expected: fail because the strings are missing.

- [ ] **Step 3: Implement `renderKellyStrategyCapital()`**

In `dashboard.js`, add before `renderKellyExperimentCard()`:

```javascript
function renderKellyStrategyCapital(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const capital = entry.capital && typeof entry.capital === "object" ? entry.capital : {};
  if (capital.available === false || !Object.keys(capital).length) {
    return `
      <section class="kelly-strategy-capital" aria-label="Kelly 策略资金">
        <div class="kelly-strategy-capital-header">
          <h4>策略资金</h4>
        </div>
        <p class="kelly-order-empty">策略资金数据暂不可用。</p>
      </section>
    `;
  }
  const currency = firstPresent(capital.currency, entry.budget_currency);
  const metrics = [
    ["总资金", formatMoney(capital.budget, currency)],
    ["已占用", formatMoney(capital.occupied_notional, currency)],
    ["可用资金", formatMoney(capital.available_notional, currency), "primary"],
    ["占用率", hasValue(capital.utilization_pct) ? `${formatPlain(capital.utilization_pct)}%` : ""],
    ["未完成买单", capital.open_buy_order_count],
    ["已实现盈亏", formatMoney(capital.realized_pnl, currency)],
  ];
  return `
    <section class="kelly-strategy-capital" aria-label="Kelly 策略资金">
      <div class="kelly-strategy-capital-header">
        <div>
          <h4>策略资金</h4>
          <p>按 experiment_id 独立归因；买单提交即占用。</p>
        </div>
        ${hasValue(capital.updated_at) ? `<span>${escapeHtml(formatPlain(capital.updated_at))}</span>` : ""}
      </div>
      <dl class="kelly-capital-metric-grid">
        ${metrics.map(([label, value, tone]) => `
          <div class="${tone === "primary" ? "primary" : ""}">
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatPlain(value))}</dd>
          </div>
        `).join("")}
      </dl>
      ${renderKellyCapitalBar(capital)}
      <div class="kelly-capital-breakdown-grid">
        ${renderKellyCapitalBreakdown(capital, currency)}
        ${renderKellyCapitalSymbolOccupancy(capital, currency)}
        ${renderKellyCapitalNextOrderImpact(capital, currency)}
      </div>
    </section>
  `;
}
```

Add helper functions for bar, breakdown, symbols, and next order impact. Keep them pure string renderers using `escapeHtml`, `formatPlain`, `formatMoney`, and `hasValue`.

Insert in `renderKellyExperimentCard()`:

```javascript
${renderKellyStrategyCapital(entry)}
```

Place it after the entry summary paragraph and before `renderKellyOrderExecution(entry)`.

- [ ] **Step 4: Add CSS**

In `dashboard.css`, add styles:

```css
.kelly-strategy-capital {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface);
  padding: 12px;
}

.kelly-strategy-capital-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 10px;
}

.kelly-strategy-capital-header h4 {
  margin: 0;
  font-size: 14px;
}

.kelly-strategy-capital-header p,
.kelly-strategy-capital-header span {
  margin: 2px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.kelly-capital-metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}

.kelly-capital-metric-grid > div,
.kelly-capital-pane {
  border: 1px solid var(--border-subtle);
  border-radius: 7px;
  background: var(--surface-muted);
  padding: 10px;
}

.kelly-capital-metric-grid > div.primary {
  border-color: rgba(47, 139, 112, 0.35);
  background: rgba(47, 139, 112, 0.08);
}

.kelly-capital-metric-grid dt,
.kelly-capital-pane dt {
  color: var(--text-muted);
  font-size: 11px;
}

.kelly-capital-metric-grid dd,
.kelly-capital-pane dd {
  margin: 2px 0 0;
  font-size: 16px;
  font-weight: 800;
}

.kelly-capital-utilization-bar {
  display: flex;
  height: 10px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--surface-muted);
  margin: 12px 0;
}

.kelly-capital-utilization-bar .position {
  background: var(--accent-green);
}

.kelly-capital-utilization-bar .reserved {
  background: var(--accent-warning);
}

.kelly-capital-breakdown-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1.25fr;
  gap: 10px;
}

.kelly-capital-pane h5 {
  margin: 0 0 8px;
  font-size: 12px;
}

.kelly-capital-line {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}

@media (max-width: 900px) {
  .kelly-capital-metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .kelly-capital-breakdown-grid {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 5: Run dashboard web test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_kelly_strategy_capital_panel -q
```

Expected: pass.

- [ ] **Step 6: Update Playwright fixture and E2E**

Add `capital` objects to at least two experiments in `tests/e2e/fixtures/kelly-dashboard.json`:

- US trend: available `21540`
- HK trend: available `155000`

In `tests/e2e/kelly-lab.spec.ts`, assert:

```typescript
const capitalPanel = page.getByLabel('Kelly 策略资金');
await expect(capitalPanel.getByText('可用资金')).toBeVisible();
await expect(capitalPanel.getByText('USD 21,540')).toBeVisible();
await page.getByRole('tab', { name: /趋势回调 20D Mock HK/ }).click();
await expect(page.getByLabel('Kelly 策略资金').getByText('HKD 155,000')).toBeVisible();
```

- [ ] **Step 7: Run UI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
npx playwright test tests/e2e/kelly-lab.spec.ts
```

Expected: pytest passes and Playwright passes.

- [ ] **Step 8: Commit Task 5**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/e2e/fixtures/kelly-dashboard.json tests/e2e/kelly-lab.spec.ts
git commit -m "feat: show kelly strategy capital panel"
```

---

## Task 6: Final Verification And Changelog

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update changelog**

Add under `2026-07-10`:

```markdown
- Added strategy-level Kelly capital snapshots, capital-aware order risk
  checks, and a Kelly Lab capital panel showing occupied, available, and
  next-order impact per strategy.
```

- [ ] **Step 2: Run focused backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_capital.py tests/test_kelly_lab.py tests/test_kelly_order_risk.py tests/test_kelly_order_risk_cli.py tests/test_kelly_strategy_capital_cli.py -q
```

Expected: all pass.

- [ ] **Step 3: Run focused dashboard tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
npx playwright test tests/e2e/kelly-lab.spec.ts
```

Expected: all pass.

- [ ] **Step 4: Run compile and diff checks**

Run:

```bash
.venv/bin/python -m py_compile src/open_trader/kelly_strategy_capital.py src/open_trader/kelly_lab.py src/open_trader/kelly_order_risk.py src/open_trader/cli.py
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 5: Run direct CLI workflow**

Run:

```bash
tmpdir=$(mktemp -d)
cp -R data/latest "$tmpdir/latest"
.venv/bin/open-trader kelly build-strategy-capital --data-dir "$tmpdir" --calculated-at "2026-07-10 21:45"
.venv/bin/open-trader kelly build-order-risk-checks --data-dir "$tmpdir" --checked-at "2026-07-10 21:46"
rm -rf "$tmpdir"
```

Expected:

- `build-strategy-capital` prints a strategy count and latest path.
- `build-order-risk-checks` prints approved/blocked counts and latest path.

- [ ] **Step 6: Commit final docs**

```bash
git add CHANGELOG.md
git commit -m "docs: update changelog for kelly strategy capital"
```

---

## Self-Review

- Spec coverage: backend artifact, risk check, UI placement, unavailable fallback, next-order impact, Playwright tab switching, and changelog are covered.
- Scope: one vertical slice. It intentionally does not implement complex FIFO lots, cross-strategy netting, real-money order routing, or account-level cash reconciliation.
- Data model: `experiment.capital` matches the approved UI spec and remains attributed by `experiment_id`.
- TDD: each behavior starts with a failing test before production code.
