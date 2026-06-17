# Feishu Order Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a Feishu daily order-review sheet that shows exact actionable trade details instead of generic premarket summaries.

**Architecture:** Extend `trade_actions.csv` so the machine-readable action rows contain current average cost, post-trade quantity, post-trade weight, post-trade average cost, and risk-to-stop. Add a focused `notifications.py` module that renders deterministic Feishu text and sends `msg_type=text` webhook payloads. Wire `run-daily-premarket` to generate dated trade actions, render the Feishu order-review sheet from dated artifacts, and notify only after artifacts are written.

**Tech Stack:** Python 3.12, stdlib `urllib.request` for Feishu webhooks, `csv.DictReader`/`DictWriter`, `Decimal`, pytest.

---

## Source Notes

- Feishu custom bots send messages by calling the group bot webhook. Use the official custom bot guide as the primary reference: https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN
- First version uses plain text payloads:

```json
{
  "msg_type": "text",
  "content": {
    "text": "message body"
  }
}
```

Rich Feishu cards are out of scope for this plan.

## File Structure

- Modify `src/open_trader/trade_actions.py`: add stable CSV fields for current cost, post-trade values, and risk-to-stop; compute those fields while sizing rows.
- Modify `tests/test_trade_actions.py`: cover the new columns and conservative `REVIEW` behavior when cost data is missing or malformed.
- Modify `src/open_trader/advice/prompts/change_classifier.md`: require concrete evidence and ban circular rationale.
- Modify `tests/test_change_classifier.py`: assert the prompt contains evidence requirements.
- Create `src/open_trader/notifications.py`: notifier protocol, no-op notifier, macOS notifier move target, Feishu webhook notifier, deterministic Feishu order-review renderer.
- Create `tests/test_notifications.py`: webhook payload, renderer output, `REVIEW` downgrade text, and body truncation behavior.
- Modify `src/open_trader/daily_premarket.py`: import notification types, parse notifier config, generate trade actions during the daily run, add artifacts/status fields, send Feishu order-review text after dated artifacts are written.
- Modify `tests/test_daily_premarket.py`: config parsing, daily trade-action generation, notification body, dry-run no-webhook behavior, notification failure isolation.
- Modify `config/daily_premarket.env.example`: add Feishu notification variables.

---

### Task 1: Extend Trade Action Rows With Post-Trade Fields

**Files:**
- Modify: `src/open_trader/trade_actions.py`
- Modify: `tests/test_trade_actions.py`

- [ ] **Step 1: Write the failing fieldname and sizing tests**

Add `avg_cost_price`, `post_trade_quantity`, `post_trade_weight`, `post_trade_avg_cost`, and `risk_to_stop` to `test_trade_action_fieldnames_are_stable` in `tests/test_trade_actions.py`:

```python
def test_trade_action_fieldnames_are_stable() -> None:
    assert TRADE_ACTION_FIELDNAMES == (
        "run_date",
        "symbol",
        "market",
        "futu_symbol",
        "action",
        "priority",
        "last_price",
        "trigger_status",
        "suggested_quantity",
        "suggested_notional",
        "notional_currency",
        "current_quantity",
        "current_weight",
        "avg_cost_price",
        "target_max_weight",
        "cash_available",
        "limit_price",
        "stop_price",
        "post_trade_quantity",
        "post_trade_weight",
        "post_trade_avg_cost",
        "risk_to_stop",
        "reason",
        "source_plan",
        "status",
        "error",
    )
```

Add a ready buy assertion near the existing buy sizing tests:

```python
def test_buy_action_includes_post_trade_position_and_cost() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="5%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(
            quantity="10",
            cash="10000",
            market_value="390",
            market_value_hkd="3042",
            weight="0.0039",
            fx_to_hkd="7.8",
            avg_cost_price="300",
        ),
        source_plan="data/runs/2026-06-16/trading_plan.csv",
    )

    assert row["status"] == "ready"
    assert row["avg_cost_price"] == "300"
    assert row["suggested_quantity"] == "6"
    assert row["suggested_notional"] == "2340"
    assert row["post_trade_quantity"] == "16"
    assert row["post_trade_avg_cost"] == "333.75"
    assert row["post_trade_weight"] == "6.24%"
    assert row["risk_to_stop"] == "800"
```

Add a malformed-cost test:

```python
def test_buy_action_reviews_when_average_cost_is_invalid() -> None:
    context = portfolio_context(avg_cost_price="0")

    row = build_trade_action_row(
        plan=active_plan(max_weight="5%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=context,
        source_plan="data/runs/2026-06-16/trading_plan.csv",
    )

    assert row["status"] == "review"
    assert row["action"] == "REVIEW"
    assert row["error"] == "invalid portfolio sizing field(s): avg_cost_price"
    assert row["post_trade_quantity"] == ""
    assert row["post_trade_avg_cost"] == ""
```

Update the test helper `portfolio_context` signature:

```python
def portfolio_context(
    *,
    quantity: str = "10",
    cash: str = "1000",
    market_value: str = "3900",
    market_value_hkd: str = "30420",
    weight: str = "0.039",
    fx_to_hkd: str = "7.8",
    avg_cost_price: str = "300",
) -> PortfolioActionContext:
    invalid_fields: tuple[str, ...] = ()
    parsed_avg_cost = Decimal(avg_cost_price)
    if parsed_avg_cost <= 0:
        invalid_fields = ("avg_cost_price",)
        parsed_avg_cost = Decimal("0")
    return PortfolioActionContext(
        positions={
            ("US", "MSFT"): PortfolioPositionSnapshot(
                currency="USD",
                quantity=Decimal(quantity),
                market_value=Decimal(market_value),
                market_value_hkd=Decimal(market_value_hkd),
                weight=Decimal(weight),
                fx_to_hkd=Decimal(fx_to_hkd),
                avg_cost_price=parsed_avg_cost,
                invalid_fields=invalid_fields,
            )
        },
        cash_by_currency={"USD": Decimal(cash)},
        total_market_value_hkd=Decimal("780000"),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions.py::test_trade_action_fieldnames_are_stable tests/test_trade_actions.py::test_buy_action_includes_post_trade_position_and_cost tests/test_trade_actions.py::test_buy_action_reviews_when_average_cost_is_invalid -v
```

Expected: fail because the new fields and `avg_cost_price` dataclass argument do not exist.

- [ ] **Step 3: Implement the schema and calculations**

In `src/open_trader/trade_actions.py`, update `TRADE_ACTION_FIELDNAMES`:

```python
TRADE_ACTION_FIELDNAMES = (
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "priority",
    "last_price",
    "trigger_status",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "current_quantity",
    "current_weight",
    "avg_cost_price",
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
    "post_trade_quantity",
    "post_trade_weight",
    "post_trade_avg_cost",
    "risk_to_stop",
    "reason",
    "source_plan",
    "status",
    "error",
)
```

Update `PortfolioPositionSnapshot`:

```python
@dataclass(frozen=True)
class PortfolioPositionSnapshot:
    currency: str
    quantity: Decimal
    market_value: Decimal
    market_value_hkd: Decimal
    weight: Decimal
    fx_to_hkd: Decimal
    avg_cost_price: Decimal
    invalid_fields: tuple[str, ...] = ()
```

Update `PORTFOLIO_REQUIRED_FIELDNAMES` to include `avg_cost_price`:

```python
PORTFOLIO_REQUIRED_FIELDNAMES = (
    "market",
    "asset_class",
    "symbol",
    "currency",
    "total_quantity",
    "avg_cost_price",
    "market_value",
    "fx_to_hkd",
    "market_value_hkd",
    "portfolio_weight_hkd",
)
```

In `load_portfolio_action_context`, parse `avg_cost_price` and pass it into `PortfolioPositionSnapshot`:

```python
avg_cost_price = _position_decimal(row, "avg_cost_price", invalid_fields)
if avg_cost_price is not None and avg_cost_price <= 0:
    invalid_fields.append("avg_cost_price")
    avg_cost_price = None

positions[key] = PortfolioPositionSnapshot(
    currency=currency,
    quantity=quantity or Decimal("0"),
    market_value=market_value or Decimal("0"),
    market_value_hkd=market_value_hkd or Decimal("0"),
    weight=weight or Decimal("0"),
    fx_to_hkd=fx_to_hkd or Decimal("0"),
    avg_cost_price=avg_cost_price or Decimal("0"),
    invalid_fields=tuple(invalid_fields),
)
```

Add these helper functions below `_review_row`:

```python
def _set_post_trade_fields(
    row: dict[str, str],
    *,
    position: PortfolioPositionSnapshot,
    quantity_delta: Decimal,
    execution_price: Decimal,
    portfolio: PortfolioActionContext,
) -> None:
    post_quantity = position.quantity + quantity_delta
    row["post_trade_quantity"] = _decimal_to_text(post_quantity)
    if post_quantity < 0:
        row["post_trade_quantity"] = ""
        row["post_trade_weight"] = ""
        row["post_trade_avg_cost"] = ""
        row["risk_to_stop"] = ""
        return

    post_market_value = post_quantity * execution_price
    if portfolio.total_market_value_hkd > 0 and position.fx_to_hkd > 0:
        post_weight = (post_market_value * position.fx_to_hkd) / portfolio.total_market_value_hkd
        row["post_trade_weight"] = _percent_to_text(post_weight)

    if post_quantity == 0:
        row["post_trade_avg_cost"] = ""
    elif quantity_delta > 0:
        total_cost = (position.quantity * position.avg_cost_price) + (quantity_delta * execution_price)
        row["post_trade_avg_cost"] = _decimal_to_text(total_cost / post_quantity)
    else:
        row["post_trade_avg_cost"] = _decimal_to_text(position.avg_cost_price)

    stop_price = _optional_decimal(row.get("stop_price", "") or "")
    if stop_price is not None and stop_price > 0 and post_quantity > 0:
        risk = max(Decimal("0"), (execution_price - stop_price) * post_quantity)
        row["risk_to_stop"] = _decimal_to_text(risk)
```

Initialize the new row fields in `build_trade_action_row`:

```python
"avg_cost_price": _decimal_to_text(position.avg_cost_price if position else None),
"post_trade_quantity": "",
"post_trade_weight": "",
"post_trade_avg_cost": "",
"risk_to_stop": "",
```

For sell rows, pass `portfolio` into `_size_sell_action_row`, require average cost, and set post-trade fields:

```python
def _size_sell_action_row(
    row: dict[str, str],
    action: str,
    quote_status: PlanQuoteStatus,
    position: PortfolioPositionSnapshot | None,
    portfolio: PortfolioActionContext,
) -> dict[str, str]:
    if quote_status.last_price <= 0:
        return _review_row(row, "invalid last price")
    if position is None:
        return _review_row(row, "missing portfolio position for sell sizing")
    invalid_fields = _invalid_position_fields(
        position,
        ("total_quantity", "avg_cost_price", "fx_to_hkd", "market_value_hkd"),
    )
    if invalid_fields:
        return _review_row(
            row,
            f"invalid portfolio sizing field(s): {', '.join(invalid_fields)}",
        )

    if action == "TRIM":
        quantity = (position.quantity * Decimal("0.5")).to_integral_value(
            rounding=ROUND_DOWN
        )
    else:
        quantity = position.quantity

    if quantity < 1:
        return _review_row(row, "current quantity below one share for sell sizing")

    if action != "SELL_STOP":
        row["limit_price"] = _decimal_to_text(quote_status.last_price)
    row["suggested_quantity"] = _decimal_to_text(quantity)
    row["suggested_notional"] = _decimal_to_text(quantity * quote_status.last_price)
    _set_post_trade_fields(
        row,
        position=position,
        quantity_delta=-quantity,
        execution_price=quote_status.last_price,
        portfolio=portfolio,
    )
    row["status"] = "ready"
    return row
```

For buy rows, extend the invalid field list and set post-trade fields after `suggested_notional`:

```python
invalid_fields = _invalid_position_fields(
    position,
    ("total_quantity", "market_value", "market_value_hkd", "fx_to_hkd", "avg_cost_price"),
)
...
_set_post_trade_fields(
    row,
    position=position,
    quantity_delta=quantity,
    execution_price=quote_status.last_price,
    portfolio=portfolio,
)
```

Update the sell call site in `build_trade_action_row`:

```python
return _size_sell_action_row(row, action, quote_status, position, portfolio)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py
git commit -m "feat: enrich trade action sizing fields"
```

---

### Task 2: Tighten Change Classifier Prompt Quality

**Files:**
- Modify: `src/open_trader/advice/prompts/change_classifier.md`
- Modify: `tests/test_change_classifier.py`

- [ ] **Step 1: Write the failing prompt test**

Add to `tests/test_change_classifier.py`:

```python
def test_prompt_requires_concrete_evidence_and_bans_circular_rationale() -> None:
    prompt = load_prompt()

    assert "concrete evidence" in prompt
    assert "price" in prompt
    assert "stop" in prompt
    assert "target weight" in prompt
    assert "Do not write circular rationale" in prompt
    assert "because severity is high" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_change_classifier.py::test_prompt_requires_concrete_evidence_and_bans_circular_rationale -v
```

Expected: fail because the prompt does not yet contain the concrete evidence rules.

- [ ] **Step 3: Update the prompt**

Append this section to `src/open_trader/advice/prompts/change_classifier.md` before the final safety line:

```markdown
Concrete evidence requirements:

- summary must state the actual trading change, not just that a change exists.
- rationale must include concrete evidence from the input when available: price,
  stop, target, target weight, quantity, percent trim/add, prior-vs-latest
  action change, catalyst, or risk condition.
- watch_trigger must include a specific price, level, event, or condition when
  the latest advice provides one.
- If the source advice does not provide enough detail for a concrete rationale,
  set include_in_report to false unless the action itself changed materially.

Do not write circular rationale. Banned examples:

- "This matters because severity is high."
- "Review the position, price condition, and order risk."
- "The suggested action changed and needs review."
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_change_classifier.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/prompts/change_classifier.md tests/test_change_classifier.py
git commit -m "docs: require concrete classifier evidence"
```

---

### Task 3: Add Feishu Notification Module

**Files:**
- Create: `src/open_trader/notifications.py`
- Create: `tests/test_notifications.py`

- [ ] **Step 1: Write failing tests for payload and rendering**

Create `tests/test_notifications.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    NotificationError,
    render_feishu_order_review,
)


def test_feishu_webhook_notifier_sends_text_payload() -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout_seconds
        return {"code": 0, "msg": "success"}

    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        post_json=fake_post,
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader", "hello")

    assert captured == {
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "payload": {"msg_type": "text", "content": {"text": "Open Trader\n\nhello"}},
        "timeout": 3.0,
    }


def test_feishu_webhook_notifier_raises_on_api_error() -> None:
    def fake_post(url: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
        return {"code": 19024, "msg": "bad webhook"}

    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Feishu webhook error 19024"):
        notifier.notify("Open Trader", "hello")


def test_composite_notifier_continues_after_child_failure() -> None:
    events: list[str] = []

    class Failing:
        def notify(self, title: str, message: str) -> None:
            events.append("failing")
            raise RuntimeError("boom")

    class Working:
        def notify(self, title: str, message: str) -> None:
            events.append(f"{title}:{message}")

    CompositeNotifier([Failing(), Working()]).notify("title", "body")

    assert events == ["failing", "title:body"]


def test_render_feishu_order_review_includes_precise_ready_fields(tmp_path: Path) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    actions_path.write_text(
        "\n".join(
            [
                "run_date,symbol,market,futu_symbol,action,priority,last_price,trigger_status,suggested_quantity,suggested_notional,notional_currency,current_quantity,current_weight,avg_cost_price,target_max_weight,cash_available,limit_price,stop_price,post_trade_quantity,post_trade_weight,post_trade_avg_cost,risk_to_stop,reason,source_plan,status,error",
                "2026-06-17,RKLB,US,US.RKLB,ADD,high,109,entry_zone,80,8720,USD,120,1.36%,101.20,2.20%,10000,102,94,200,2.20%,104.32,3000,price entered entry zone,data/runs/2026-06-17/trading_plan.csv,ready,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[Path("reports/trade_actions/2026-06-17.md")],
    )

    assert "Open Trader 2026-06-17: success" in body
    assert "US.RKLB | high | ADD" in body
    assert "Trigger price: 102" in body
    assert "This order: ADD 80 shares" in body
    assert "Estimated notional: USD 8720" in body
    assert "Post-trade quantity: 200" in body
    assert "Post-trade weight: 2.20%" in body
    assert "Post-trade average cost: 104.32" in body
    assert "Hard stop: 94" in body
    assert "Risk to stop: USD 3000" in body


def test_render_feishu_order_review_marks_missing_post_trade_fields_review(tmp_path: Path) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    actions_path.write_text(
        "\n".join(
            [
                "run_date,symbol,market,futu_symbol,action,priority,last_price,trigger_status,suggested_quantity,suggested_notional,notional_currency,current_quantity,current_weight,avg_cost_price,target_max_weight,cash_available,limit_price,stop_price,post_trade_quantity,post_trade_weight,post_trade_avg_cost,risk_to_stop,reason,source_plan,status,error",
                "2026-06-17,MSFT,US,US.MSFT,ADD,high,390,entry_zone,6,2340,USD,10,1.13%,,2%,10000,390,340,,,,,missing avg cost,data/runs/2026-06-17/trading_plan.csv,ready,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="partial",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "US.MSFT | high | REVIEW" in body
    assert "Missing before action: avg_cost_price, post_trade_quantity, post_trade_weight, post_trade_avg_cost, risk_to_stop" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'open_trader.notifications'`.

- [ ] **Step 3: Implement `src/open_trader/notifications.py`**

Create `src/open_trader/notifications.py`:

```python
from __future__ import annotations

import csv
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol


class NotificationError(RuntimeError):
    pass


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        pass


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class MacOSNotifier:
    def notify(self, title: str, message: str) -> None:
        script = (
            f'display notification "{_escape_osascript(message)}" '
            f'with title "{_escape_osascript(title)}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def notify(self, title: str, message: str) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(title, message)
            except Exception:
                continue


PostJson = Callable[[str, dict[str, object], float], dict[str, object]]


class FeishuWebhookNotifier:
    def __init__(
        self,
        *,
        webhook_url: str,
        post_json: PostJson | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url
        self._post_json = post_json or _post_json
        self.timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        text = f"{title}\n\n{message}" if title else message
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        response = self._post_json(self.webhook_url, payload, self.timeout_seconds)
        code = response.get("code", 0)
        if code not in {0, "0"}:
            raise NotificationError(
                f"Feishu webhook error {code}: {response.get('msg', '')}"
            )


def render_feishu_order_review(
    *,
    run_date: str,
    status: str,
    actions_path: Path,
    report_paths: list[Path],
    max_ready_sections: int = 5,
) -> str:
    rows = _read_action_rows(actions_path)
    ready = [row for row in rows if row.get("status") == "ready"]
    review = [row for row in rows if row.get("status") == "review"]
    watch = [row for row in rows if row.get("status") == "watch"]
    lines = [
        f"Open Trader {run_date}: {status}",
        "",
        "Summary:",
        f"- Ready: {len(ready)}",
        f"- Review: {len(review)}",
        f"- Watch: {len(watch)}",
    ]

    if ready:
        lines.extend(["", "Ready:"])
        for row in sorted(ready, key=_priority_sort_key)[:max_ready_sections]:
            lines.extend(["", *_render_action_section(row)])
        remaining = len(ready) - max_ready_sections
        if remaining > 0:
            lines.append(f"- {remaining} additional ready action(s) in report.")

    if review:
        lines.extend(["", "Review:"])
        for row in sorted(review, key=_priority_sort_key)[:5]:
            futu_symbol = row.get("futu_symbol", "").strip()
            priority = row.get("priority", "").strip()
            error = row.get("error", "").strip() or row.get("reason", "").strip()
            lines.append(f"- {futu_symbol} {priority}: {error}")

    if watch:
        lines.extend(["", f"Watch: {len(watch)} action(s) waiting for trigger."])

    if report_paths:
        lines.extend(["", "Reports:"])
        lines.extend(f"- {path}" for path in report_paths)
    return "\n".join(lines).strip() + "\n"


def _render_action_section(row: Mapping[str, str]) -> list[str]:
    missing = _missing_precise_fields(row)
    status = "REVIEW" if missing else row.get("action", "").strip()
    symbol = row.get("futu_symbol", "").strip()
    priority = row.get("priority", "").strip()
    lines = [f"## {symbol} | {priority} | {status}", ""]
    if missing:
        lines.extend(
            [
                f"Missing before action: {', '.join(missing)}",
                f"Reason: {row.get('reason', '').strip()}",
            ]
        )
        return lines

    currency = row.get("notional_currency", "").strip()
    lines.extend(
        [
            "Current:",
            f"- Last price: {row.get('last_price', '').strip()}",
            f"- Current quantity: {row.get('current_quantity', '').strip()}",
            f"- Current weight: {row.get('current_weight', '').strip()}",
            f"- Current average cost: {row.get('avg_cost_price', '').strip()}",
            "",
            "Suggested action:",
            f"- Trigger price: {row.get('limit_price', '').strip()}",
            f"- This order: {row.get('action', '').strip()} {row.get('suggested_quantity', '').strip()} shares",
            f"- Estimated notional: {currency} {row.get('suggested_notional', '').strip()}",
            f"- Post-trade quantity: {row.get('post_trade_quantity', '').strip()}",
            f"- Post-trade weight: {row.get('post_trade_weight', '').strip()}",
            f"- Post-trade average cost: {row.get('post_trade_avg_cost', '').strip()}",
            "",
            "Risk:",
            f"- Hard stop: {row.get('stop_price', '').strip()}",
            f"- Risk to stop: {currency} {row.get('risk_to_stop', '').strip()}",
            "",
            "Why it matters:",
            f"- {row.get('reason', '').strip()}",
        ]
    )
    return lines


def _missing_precise_fields(row: Mapping[str, str]) -> list[str]:
    required = [
        "avg_cost_price",
        "post_trade_quantity",
        "post_trade_weight",
        "post_trade_avg_cost",
        "risk_to_stop",
    ]
    return [field for field in required if not row.get(field, "").strip()]


def _read_action_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _priority_sort_key(row: Mapping[str, str]) -> tuple[int, str]:
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return (
        priority_order.get(row.get("priority", "").strip().lower(), 99),
        row.get("futu_symbol", "").strip(),
    )


def _post_json(url: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise NotificationError(f"Feishu webhook request failed: {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NotificationError(f"Feishu webhook returned invalid JSON: {body}") from exc
    if not isinstance(parsed, dict):
        raise NotificationError("Feishu webhook returned non-object JSON")
    return parsed


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/notifications.py tests/test_notifications.py
git commit -m "feat: add feishu notification renderer"
```

---

### Task 4: Parse Notification Config

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`
- Modify: `config/daily_premarket.env.example`

- [ ] **Step 1: Write failing config tests**

In `tests/test_daily_premarket.py`, update `test_load_env_config_parses_required_values` env content:

```python
"OPEN_TRADER_NOTIFIERS=feishu,macos",
"OPEN_TRADER_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/test",
"OPEN_TRADER_NOTIFY_DAILY_REPORT=1",
"OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=0",
```

Add assertions:

```python
assert config.notifiers == ("feishu", "macos")
assert config.feishu_webhook_url == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
assert config.notify_daily_report is True
assert config.notify_action_triggers is False
```

Add a notifier factory test:

```python
def test_build_notifier_uses_configured_feishu_and_macos(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )

    notifier = daily_premarket.build_notifier(config)

    assert notifier.__class__.__name__ == "CompositeNotifier"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_load_env_config_parses_required_values tests/test_daily_premarket.py::test_build_notifier_uses_configured_feishu_and_macos -v
```

Expected: fail because the config fields and `build_notifier` do not exist.

- [ ] **Step 3: Implement config fields and factory**

In `src/open_trader/daily_premarket.py`, import notification classes:

```python
from .notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    Notifier,
    NullNotifier,
    render_feishu_order_review,
)
```

Remove local `Notifier`, `NullNotifier`, and `MacOSNotifier` class definitions from `daily_premarket.py` after the import is in place.

Extend `DailyPremarketConfig`:

```python
notifiers: tuple[str, ...] = ()
feishu_webhook_url: str = ""
notify_daily_report: bool = False
notify_action_triggers: bool = False
```

Add helpers:

```python
def _csv_config(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _bool_config(value: str, *, default: bool = False) -> bool:
    if not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_notifier(config: DailyPremarketConfig) -> Notifier:
    notifiers: list[Notifier] = []
    for name in config.notifiers:
        if name == "macos":
            notifiers.append(MacOSNotifier())
        elif name == "feishu":
            if not config.feishu_webhook_url:
                raise ValueError("OPEN_TRADER_FEISHU_WEBHOOK_URL is required when feishu notifier is enabled")
            notifiers.append(FeishuWebhookNotifier(webhook_url=config.feishu_webhook_url))
        elif name:
            raise ValueError(f"unknown notifier: {name}")
    if not notifiers:
        return NullNotifier()
    return CompositeNotifier(notifiers)
```

In `load_env_config`, pass the new fields:

```python
notifiers=_csv_config(values.get("OPEN_TRADER_NOTIFIERS", "")),
feishu_webhook_url=values.get("OPEN_TRADER_FEISHU_WEBHOOK_URL", ""),
notify_daily_report=_bool_config(values.get("OPEN_TRADER_NOTIFY_DAILY_REPORT", "0")),
notify_action_triggers=_bool_config(values.get("OPEN_TRADER_NOTIFY_ACTION_TRIGGERS", "0")),
```

In `src/open_trader/cli.py`, use the factory:

```python
from .daily_premarket import DailyPremarketRunner, build_notifier, load_env_config
```

and in the `run-daily-premarket` block:

```python
result = DailyPremarketRunner(
    config=config,
    notifier=build_notifier(config),
).run(
    run_date=run_date,
    dry_run=args.dry_run,
)
```

Update `config/daily_premarket.env.example`:

```bash
OPEN_TRADER_NOTIFIERS=feishu,macos
OPEN_TRADER_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/example
OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text
OPEN_TRADER_NOTIFY_DAILY_REPORT=1
OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=1
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/cli.py tests/test_daily_premarket.py config/daily_premarket.env.example
git commit -m "feat: configure feishu notifications"
```

---

### Task 5: Generate Trade Actions Inside Daily Premarket

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing daily-run test**

In `tests/test_daily_premarket.py`, import `replace` and `TradeActionsResult`:

```python
from dataclasses import replace
from open_trader.trade_actions import TradeActionsResult
```

Add a fake generator:

```python
class FakeTradeActionGenerator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> TradeActionsResult:
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        reports_dir = kwargs["reports_dir"]
        run_date = kwargs["run_date"]
        assert isinstance(data_dir, Path)
        assert isinstance(reports_dir, Path)
        assert isinstance(run_date, str)
        actions_path = data_dir / "runs" / run_date / "trade_actions.csv"
        latest_path = data_dir / "latest" / "trade_actions.csv"
        report_path = reports_dir / "trade_actions" / f"{run_date}.md"
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        actions_path.write_text(
            "run_date,symbol,market,futu_symbol,action,priority,last_price,trigger_status,suggested_quantity,suggested_notional,notional_currency,current_quantity,current_weight,avg_cost_price,target_max_weight,cash_available,limit_price,stop_price,post_trade_quantity,post_trade_weight,post_trade_avg_cost,risk_to_stop,reason,source_plan,status,error\n"
            f"{run_date},MSFT,US,US.MSFT,BUY,high,399,entry_zone,3,1197,USD,10,1.13%,390,2%,1000,399,340,13,1.40%,392.08,767,fixture,data/runs/{run_date}/trading_plan.csv,ready,\n",
            encoding="utf-8",
        )
        report_path.write_text("# Trade Actions\n", encoding="utf-8")
        return TradeActionsResult(
            run_date=run_date,
            action_count=1,
            ready_count=1,
            review_count=0,
            watch_count=0,
            actions_path=actions_path,
            latest_path=latest_path,
            report_path=report_path,
        )
```

Update the daily success test that constructs `DailyPremarketRunner` to pass `trade_action_generator=fake_trade_actions`, then assert:

```python
assert fake_trade_actions.calls
assert fake_trade_actions.calls[0]["plan_path"] == tmp_path / "data/runs/2026-06-16/trading_plan.csv"
assert fake_trade_actions.calls[0]["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
assert fake_trade_actions.calls[0]["update_latest"] is False

status_payload = json.loads((tmp_path / "data/runs/2026-06-16/daily_run_status.json").read_text(encoding="utf-8"))
assert status_payload["trade_actions"] == {"actions": 1, "ready": 1, "review": 0, "watch": 0}
assert status_payload["artifacts"]["trade_actions"] == str(tmp_path / "data/runs/2026-06-16/trade_actions.csv")
assert status_payload["artifacts"]["trade_actions_report"] == str(tmp_path / "reports/trade_actions/2026-06-16.md")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k "daily" -v
```

Expected: fail because `DailyPremarketRunner` does not accept or invoke `trade_action_generator`.

- [ ] **Step 3: Implement daily trade-action generation**

In `src/open_trader/daily_premarket.py`, import `generate_trade_actions` and result type:

```python
from .trade_actions import TradeActionsResult, generate_trade_actions
```

Extend `DailyPremarketRunner.__init__`:

```python
trade_action_generator: Callable[..., TradeActionsResult] = generate_trade_actions,
```

Assign it:

```python
self.trade_action_generator = trade_action_generator
```

After `futu_status = self._check_futu_plan(plan_result.plan_path)`, build snapshots from the `futu_status` items:

```python
snapshots = _snapshots_from_futu_status(futu_status)
trade_actions_result = self.trade_action_generator(
    plan_path=plan_result.plan_path,
    portfolio_path=self.config.portfolio,
    data_dir=self.config.data_dir,
    reports_dir=self.config.reports_dir,
    snapshots=snapshots,
    run_date=run_date,
    update_latest=False,
)
trade_action_counts = {
    "actions": trade_actions_result.action_count,
    "ready": trade_actions_result.ready_count,
    "review": trade_actions_result.review_count,
    "watch": trade_actions_result.watch_count,
}
```

Add helper near `_mapping`:

```python
def _snapshots_from_futu_status(futu_status: dict[str, object]) -> dict[str, QuoteSnapshot]:
    items = futu_status.get("items")
    if not isinstance(items, list):
        return {}
    snapshots: dict[str, QuoteSnapshot] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        futu_symbol = str(item.get("futu_symbol", "")).strip()
        last_price_text = str(item.get("last_price", "")).strip()
        if not futu_symbol or not last_price_text:
            continue
        try:
            last_price = Decimal(last_price_text)
        except Exception:
            continue
        snapshots[futu_symbol] = QuoteSnapshot(futu_symbol=futu_symbol, last_price=last_price)
    return snapshots
```

Pass `trade_actions=trade_action_counts` into `_write_status_and_report`.

Extend `_write_status_and_report` signature:

```python
trade_actions: dict[str, int],
```

Add it to the payload:

```python
"trade_actions": trade_actions,
```

Add artifacts:

```python
"trade_actions": str(trade_actions_result.actions_path),
"trade_actions_report": str(trade_actions_result.report_path),
"latest_trade_actions": str(self.config.data_dir / "latest" / "trade_actions.csv"),
```

Update `_render_daily_report` artifact list to include `trade_actions` and `trade_actions_report`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: generate trade actions in daily run"
```

---

### Task 6: Send Feishu Order-Review Sheet From Daily Run

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing notification test**

Add a capturing notifier to `tests/test_daily_premarket.py`:

```python
class CapturingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
```

Add a test around a successful daily run:

```python
def test_daily_run_sends_feishu_order_review_after_trade_actions(tmp_path: Path) -> None:
    fake_premarket = FakePremarket()
    fake_plan = FakePlanBuilder()
    fake_trade_actions = FakeTradeActionGenerator()
    notifier = CapturingNotifier()
    config = daily_config(tmp_path)
    config = replace(
        config,
        notify_daily_report=True,
        notifiers=("feishu",),
    )

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=fake_premarket,
        plan_builder=fake_plan,
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=fake_trade_actions,
        notifier=notifier,
    ).run("2026-06-16")

    assert result.status == "success"
    assert notifier.messages
    title, body = notifier.messages[-1]
    assert title == "Open Trader daily order review"
    assert "Open Trader 2026-06-16: success" in body
    assert "US.MSFT | high | BUY" in body
    assert "Post-trade average cost" in body
```

If the file does not already expose `daily_config`, add:

```python
def daily_config(tmp_path: Path) -> DailyPremarketConfig:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    portfolio.parent.mkdir(parents=True, exist_ok=True)
    portfolio.write_text("market,asset_class,symbol,currency,total_quantity,avg_cost_price,market_value,fx_to_hkd,market_value_hkd,portfolio_weight_hkd\nUS,stock,MSFT,USD,10,390,3990,7.8,31122,1.13%\n", encoding="utf-8")
    return DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=portfolio,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_run_sends_feishu_order_review_after_trade_actions -v
```

Expected: fail because the daily runner still sends the short premarket notification message.

- [ ] **Step 3: Render and send the order-review sheet**

In `DailyPremarketRunner._run_locked`, replace the final `_notify` call with:

```python
if self.config.notify_daily_report and not dry_run:
    self._notify(
        "Open Trader daily order review",
        render_feishu_order_review(
            run_date=run_date,
            status=status,
            actions_path=trade_actions_result.actions_path,
            report_paths=[
                trade_actions_result.report_path,
                report_path,
            ],
        ),
    )
else:
    self._notify(
        "Open Trader daily premarket",
        _notification_message(status, plan_counts, futu_status, advice_counts),
    )
```

In `_write_failure`, keep the short failure notification because there is no trade-action artifact to render:

```python
self._notify(
    "Open Trader daily premarket",
    _notification_message("failed", {}, {}, {}),
)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_notifications.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: send feishu daily order review"
```

---

### Task 7: Verify CLI And Full Test Suite

**Files:**
- Modify only if verification exposes a concrete defect.

- [ ] **Step 1: Run focused CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions_cli.py tests/test_premarket_cli.py tests/test_daily_premarket.py -v
```

Expected: pass.

- [ ] **Step 2: Run all tests**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Run dry-run daily command if local env is present**

Run:

```bash
.venv/bin/python -m open_trader run-daily-premarket --date today --config config/daily_premarket.env --dry-run
```

Expected if local secrets and Futu are available: exit `0`, writes dated artifacts, and does not send Feishu webhook because `--dry-run` is set.

Expected if local env is absent or incomplete: clean argparse error with the missing config key, no traceback.

- [ ] **Step 4: Commit verification fixes if needed**

If Step 1 or Step 2 required code changes, commit them:

```bash
git add <changed-files>
git commit -m "fix: stabilize feishu order review workflow"
```

If no code changes were required after the previous commit, do not create an empty commit.

---

## Self-Review

- Spec coverage: The plan covers Feishu config, deterministic rendering, concrete action fields, prompt tightening, dated daily artifacts, no stale latest reads, dry-run no-send behavior, webhook failure isolation, and `REVIEW` downgrades when exact calculations cannot be made.
- Red-flag scan: No unfinished markers or incomplete sections remain.
- Type consistency: `FeishuWebhookNotifier`, `CompositeNotifier`, `render_feishu_order_review`, `TradeActionsResult`, and the new trade-action fields are introduced before later tasks use them.
- Scope check: Intraday `watch-actions` notification is not implemented in this plan. The approved user focus was the Feishu daily order-review report; a separate plan should cover intraday trigger dedupe and notification state if still desired.
