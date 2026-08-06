# Systematic Trading Plan Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic target-position, risk, and event-history core that live monitoring and backtests will share.

**Architecture:** Add one focused `systematic_plan` module for condition evaluation and target-order calculation, one `portfolio_risk` module for the selected 10% rule, and one `plan_events` module for append-only JSONL history and status replay. Keep legacy CSV parsing, broker adapters, Backtrader, notifications, and Dashboard rendering unchanged in this sub-project.

**Tech Stack:** Python 3.12, frozen dataclasses, `Decimal`, stdlib JSON, pytest.

## Global Constraints

- Use target aggregate positions, never fixed repeatable sell instructions.
- All prices and quantities use `Decimal`; do not introduce floating-point arithmetic.
- A protection condition targets zero and is evaluated like any other deterministic condition.
- Current instrument weight above 10% is allowed to hold or reduce but cannot increase.
- JSONL is the event source for this version; do not add a database.
- No new dependency, framework, background process, broker behavior, backtest behavior, or Dashboard behavior in this plan.
- Work one red-green cycle at a time. Verify the intended failure before writing production code.

---

### Task 1: Evaluate An Upper-Price Target Condition

**Files:**
- Create: `src/open_trader/systematic_plan.py`
- Create: `tests/test_systematic_plan.py`

**Interfaces:**
- Consumes: `Decimal` prices and quantities.
- Produces: `PlanCondition`, `StrategyPlan`, `PlanEvaluation`, and `evaluate_plan(plan, *, last_price, as_of)`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime
from decimal import Decimal

from open_trader.systematic_plan import (
    PlanCondition,
    StrategyPlan,
    evaluate_plan,
)


def test_plan_targets_reduced_position_when_upper_price_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="trim-at-resistance",
                kind="price_at_or_above",
                target_quantity=Decimal("300"),
                trigger_price=Decimal("65"),
                reason="10 EMA resistance",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("65"),
        as_of=datetime.fromisoformat("2026-07-13T10:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "trim-at-resistance"
    assert result.target_quantity == Decimal("300")
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py::test_plan_targets_reduced_position_when_upper_price_is_reached -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.systematic_plan'`.

- [ ] **Step 3: Write the minimum implementation**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


ConditionKind = Literal["price_at_or_above", "price_at_or_below", "deadline"]
EvaluationStatus = Literal["waiting", "triggered"]


@dataclass(frozen=True)
class PlanCondition:
    condition_id: str
    kind: ConditionKind
    target_quantity: Decimal
    reason: str
    trigger_price: Decimal | None = None
    deadline: datetime | None = None


@dataclass(frozen=True)
class StrategyPlan:
    plan_id: str
    market: str
    symbol: str
    current_quantity: Decimal
    conditions: tuple[PlanCondition, ...]


@dataclass(frozen=True)
class PlanEvaluation:
    plan_id: str
    status: EvaluationStatus
    condition_id: str
    target_quantity: Decimal
    reason: str


def evaluate_plan(
    plan: StrategyPlan,
    *,
    last_price: Decimal,
    as_of: datetime,
) -> PlanEvaluation:
    del as_of
    for condition in plan.conditions:
        if (
            condition.kind == "price_at_or_above"
            and condition.trigger_price is not None
            and last_price >= condition.trigger_price
        ):
            return PlanEvaluation(
                plan_id=plan.plan_id,
                status="triggered",
                condition_id=condition.condition_id,
                target_quantity=condition.target_quantity,
                reason=condition.reason,
            )
    return PlanEvaluation(
        plan_id=plan.plan_id,
        status="waiting",
        condition_id="",
        target_quantity=plan.current_quantity,
        reason="",
    )
```

- [ ] **Step 4: Run the test to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py::test_plan_targets_reduced_position_when_upper_price_is_reached -v
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/systematic_plan.py tests/test_systematic_plan.py
git commit -m "feat: evaluate systematic price targets"
```

### Task 2: Evaluate A Protection Condition

**Files:**
- Modify: `src/open_trader/systematic_plan.py`
- Modify: `tests/test_systematic_plan.py`

**Interfaces:**
- Consumes: Task 1 `StrategyPlan` and `PlanCondition`.
- Produces: `evaluate_plan` support for `price_at_or_below` without changing its signature.

- [ ] **Step 1: Write the failing test**

```python
def test_plan_targets_zero_when_protection_price_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="exit-at-protection",
                kind="price_at_or_below",
                target_quantity=Decimal("0"),
                trigger_price=Decimal("57"),
                reason="structural support invalidated",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("57"),
        as_of=datetime.fromisoformat("2026-07-13T10:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "exit-at-protection"
    assert result.target_quantity == Decimal("0")
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py::test_plan_targets_zero_when_protection_price_is_reached -v
```

Expected: FAIL because the result status is `waiting`.

- [ ] **Step 3: Add the lower-price branch**

Replace the condition predicate inside `evaluate_plan` with:

```python
        price_hit = (
            condition.trigger_price is not None
            and (
                condition.kind == "price_at_or_above"
                and last_price >= condition.trigger_price
                or condition.kind == "price_at_or_below"
                and last_price <= condition.trigger_price
            )
        )
        if price_hit:
```

Keep the existing `PlanEvaluation` return body unchanged.

- [ ] **Step 4: Run the focused and module tests**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/systematic_plan.py tests/test_systematic_plan.py
git commit -m "feat: evaluate systematic protection targets"
```

### Task 3: Evaluate A Deadline Condition

**Files:**
- Modify: `src/open_trader/systematic_plan.py`
- Modify: `tests/test_systematic_plan.py`

**Interfaces:**
- Consumes: Task 1 `evaluate_plan` interface.
- Produces: deterministic `deadline` evaluation using the existing `as_of` argument.

- [ ] **Step 1: Write the failing test**

```python
def test_plan_targets_reduced_position_when_deadline_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="trim-at-deadline",
                kind="deadline",
                target_quantity=Decimal("300"),
                deadline=datetime.fromisoformat("2026-07-15T16:00:00"),
                reason="bounce window expired",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("63"),
        as_of=datetime.fromisoformat("2026-07-15T16:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "trim-at-deadline"
    assert result.target_quantity == Decimal("300")
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py::test_plan_targets_reduced_position_when_deadline_is_reached -v
```

Expected: FAIL because the result status is `waiting`.

- [ ] **Step 3: Add the deadline predicate**

Delete `del as_of` and evaluate either predicate:

```python
        price_hit = (
            condition.trigger_price is not None
            and (
                condition.kind == "price_at_or_above"
                and last_price >= condition.trigger_price
                or condition.kind == "price_at_or_below"
                and last_price <= condition.trigger_price
            )
        )
        deadline_hit = (
            condition.kind == "deadline"
            and condition.deadline is not None
            and as_of >= condition.deadline
        )
        if price_hit or deadline_hit:
```

- [ ] **Step 4: Run the module tests**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/systematic_plan.py tests/test_systematic_plan.py
git commit -m "feat: evaluate systematic plan deadlines"
```

### Task 4: Derive An Idempotent Order From A Target

**Files:**
- Modify: `src/open_trader/systematic_plan.py`
- Modify: `tests/test_systematic_plan.py`

**Interfaces:**
- Consumes: current and target aggregate `Decimal` quantities.
- Produces: `TargetOrder` and `order_for_target(current_quantity, target_quantity)`.

- [ ] **Step 1: Write the failing test**

```python
from open_trader.systematic_plan import order_for_target


def test_order_uses_only_the_remaining_difference_to_target() -> None:
    first = order_for_target(
        current_quantity=Decimal("400"),
        target_quantity=Decimal("300"),
    )
    after_partial_fill = order_for_target(
        current_quantity=Decimal("330"),
        target_quantity=Decimal("300"),
    )

    assert first is not None
    assert first.side == "sell"
    assert first.quantity == Decimal("100")
    assert after_partial_fill is not None
    assert after_partial_fill.side == "sell"
    assert after_partial_fill.quantity == Decimal("30")
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py::test_order_uses_only_the_remaining_difference_to_target -v
```

Expected: collection fails because `order_for_target` is not defined.

- [ ] **Step 3: Write the minimum implementation**

```python
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class TargetOrder:
    side: OrderSide
    quantity: Decimal


def order_for_target(
    *,
    current_quantity: Decimal,
    target_quantity: Decimal,
) -> TargetOrder | None:
    difference = target_quantity - current_quantity
    if difference == 0:
        return None
    return TargetOrder(
        side="buy" if difference > 0 else "sell",
        quantity=abs(difference),
    )
```

- [ ] **Step 4: Run the module tests**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py -v
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/systematic_plan.py tests/test_systematic_plan.py
git commit -m "feat: derive orders from aggregate targets"
```

### Task 5: Apply The 10% New-Risk Limit

**Files:**
- Create: `src/open_trader/portfolio_risk.py`
- Create: `tests/test_portfolio_risk.py`

**Interfaces:**
- Consumes: current/proposed quantity, current per-unit HKD value, and portfolio net asset value.
- Produces: `PortfolioRiskResult` and `apply_single_instrument_limit(..., max_weight=Decimal("0.10"))`.

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal

from open_trader.portfolio_risk import apply_single_instrument_limit


def test_risk_caps_new_target_at_ten_percent() -> None:
    result = apply_single_instrument_limit(
        current_quantity=Decimal("5"),
        proposed_quantity=Decimal("15"),
        unit_value_hkd=Decimal("100"),
        portfolio_nav_hkd=Decimal("10000"),
    )

    assert result.final_quantity == Decimal("10")
    assert result.status == "adjusted"
    assert result.reason == "single-instrument target exceeds 10%"


def test_risk_allows_existing_overweight_position_but_blocks_an_increase() -> None:
    result = apply_single_instrument_limit(
        current_quantity=Decimal("12"),
        proposed_quantity=Decimal("15"),
        unit_value_hkd=Decimal("100"),
        portfolio_nav_hkd=Decimal("10000"),
    )

    assert result.final_quantity == Decimal("12")
    assert result.status == "blocked_increase"
    assert result.reason == "现有仓位超限，禁止加仓"
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_portfolio_risk.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.portfolio_risk'`.

- [ ] **Step 3: Write the minimum implementation**

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal


RiskStatus = Literal["allowed", "adjusted", "blocked_increase"]


@dataclass(frozen=True)
class PortfolioRiskResult:
    proposed_quantity: Decimal
    final_quantity: Decimal
    status: RiskStatus
    reason: str


def apply_single_instrument_limit(
    *,
    current_quantity: Decimal,
    proposed_quantity: Decimal,
    unit_value_hkd: Decimal,
    portfolio_nav_hkd: Decimal,
    max_weight: Decimal = Decimal("0.10"),
) -> PortfolioRiskResult:
    if unit_value_hkd <= 0 or portfolio_nav_hkd <= 0:
        raise ValueError("unit value and portfolio NAV must be positive")
    max_quantity = (
        portfolio_nav_hkd * max_weight / unit_value_hkd
    ).to_integral_value(rounding=ROUND_DOWN)
    if current_quantity > max_quantity and proposed_quantity > current_quantity:
        return PortfolioRiskResult(
            proposed_quantity=proposed_quantity,
            final_quantity=current_quantity,
            status="blocked_increase",
            reason="现有仓位超限，禁止加仓",
        )
    if proposed_quantity > max_quantity and proposed_quantity > current_quantity:
        return PortfolioRiskResult(
            proposed_quantity=proposed_quantity,
            final_quantity=max_quantity,
            status="adjusted",
            reason="single-instrument target exceeds 10%",
        )
    return PortfolioRiskResult(
        proposed_quantity=proposed_quantity,
        final_quantity=proposed_quantity,
        status="allowed",
        reason="",
    )
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest tests/test_portfolio_risk.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Run the related portfolio-action tests**

Run:

```bash
.venv/bin/pytest tests/test_trade_actions.py -q
```

Expected: all existing trade-action tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/portfolio_risk.py tests/test_portfolio_risk.py
git commit -m "feat: enforce systematic position limit"
```

### Task 6: Append And Replay Plan Events

**Files:**
- Create: `src/open_trader/plan_events.py`
- Create: `tests/test_plan_events.py`

**Interfaces:**
- Consumes: a JSONL path and `PlanEvent` values.
- Produces: `append_plan_event(path, event)`, `load_plan_events(path)`, and `replay_plan_status(events, plan_id)`.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from open_trader.plan_events import (
    PlanEvent,
    append_plan_event,
    load_plan_events,
    replay_plan_status,
)


def test_events_append_and_replay_current_plan_status(tmp_path: Path) -> None:
    path = tmp_path / "plan_events.jsonl"
    append_plan_event(
        path,
        PlanEvent(
            event_id="event-1",
            plan_id="US.DRAM:2026-07-13:v1",
            event_type="plan_activated",
            occurred_at="2026-07-13T09:00:00+08:00",
            payload={"target_quantity": "400"},
        ),
    )
    append_plan_event(
        path,
        PlanEvent(
            event_id="event-2",
            plan_id="US.DRAM:2026-07-13:v1",
            event_type="condition_triggered",
            occurred_at="2026-07-13T10:00:00+08:00",
            payload={"condition_id": "trim-at-resistance"},
        ),
    )

    events = load_plan_events(path)

    assert [event.event_id for event in events] == ["event-1", "event-2"]
    assert replay_plan_status(events, "US.DRAM:2026-07-13:v1") == "triggered"
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_plan_events.py::test_events_append_and_replay_current_plan_status -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.plan_events'`.

- [ ] **Step 3: Write the minimum implementation**

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


PlanEventType = Literal[
    "plan_activated",
    "condition_triggered",
    "plan_completed",
    "plan_invalidated",
    "plan_expired",
    "plan_missed",
]


@dataclass(frozen=True)
class PlanEvent:
    event_id: str
    plan_id: str
    event_type: PlanEventType
    occurred_at: str
    payload: dict[str, Any]


def append_plan_event(path: Path, event: PlanEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def load_plan_events(path: Path) -> list[PlanEvent]:
    if not path.exists():
        return []
    return [
        PlanEvent(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def replay_plan_status(events: Iterable[PlanEvent], plan_id: str) -> str:
    statuses = {
        "plan_activated": "active",
        "condition_triggered": "triggered",
        "plan_completed": "completed",
        "plan_invalidated": "invalidated",
        "plan_expired": "expired",
        "plan_missed": "missed",
    }
    status = "missing"
    for event in events:
        if event.plan_id == plan_id:
            status = statuses[event.event_type]
    return status
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest tests/test_plan_events.py -v
```

Expected: `1 passed`.

- [ ] **Step 5: Run the complete core test set**

Run:

```bash
.venv/bin/pytest tests/test_systematic_plan.py tests/test_portfolio_risk.py tests/test_plan_events.py -q
```

Expected: `7 passed`.

- [ ] **Step 6: Run the full suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/plan_events.py tests/test_plan_events.py
git commit -m "feat: record systematic plan events"
```

## Follow-On Plans

This core plan intentionally stops before integration. Write and execute these
as separate reviewed plans after the core interfaces are green:

1. Futu/Tiger real-order observation and simulated execution integration.
2. Shared live/backtest strategy adapter, benchmark data, maximum drawdown,
   Sharpe, and activation gates.
3. Structured plan generation, parameter provenance, and instrument-age
   routing.
4. Dashboard final-decision view, notifications, process restart, and final
   `make acceptance` verification.
