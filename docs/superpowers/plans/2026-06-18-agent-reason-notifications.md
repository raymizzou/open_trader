# Agent Reason Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show TradingAgents rationale and source excerpts in action notifications while separating investment reasons from price trigger conditions.

**Architecture:** Carry two new agent-derived fields from `trading_advice.csv` into `trading_plan.csv`, then into `trade_actions.csv`, then into Feishu notification rendering. Preserve existing CSV compatibility by defaulting missing new columns to empty strings and keeping the old `reason` column populated.

**Tech Stack:** Python dataclasses, standard-library `csv`, `Decimal`, pytest, existing Open Trader batch artifact conventions.

---

## File Structure

- Modify `src/open_trader/trading_plan.py`: extend `TRADING_PLAN_FIELDNAMES`, `TradingPlanRow`, plan-row building, legacy loading, and deterministic excerpt helpers.
- Modify `tests/test_trading_plan.py`: assert new plan fields, extraction behavior, and legacy compatibility.
- Modify `tests/test_trading_plan_cli.py`: update direct `TradingPlanRow` construction for the new fields.
- Modify `src/open_trader/trade_actions.py`: extend `TRADE_ACTION_FIELDNAMES`, copy agent fields from `TradingPlanRow`, and preserve trigger text separately.
- Modify `tests/test_trade_actions.py`: update field schema, helpers, and assertions for propagated agent fields.
- Modify `src/open_trader/notifications.py`: render `原因`, `原文`, and `触发` from the new fields with action-aware fallback.
- Modify `tests/test_notifications.py`: update field schema and cover new ready trim, legacy, missing excerpt, and buy/add behavior.

---

### Task 1: Add Agent Reason Fields To Trading Plans

**Files:**
- Modify: `src/open_trader/trading_plan.py`
- Modify: `tests/test_trading_plan.py`
- Modify: `tests/test_trading_plan_cli.py`

- [ ] **Step 1: Write failing tests for plan field schema and extraction**

Add this helper near `msft_advice_summary()` in `tests/test_trading_plan.py`:

```python
def mrvl_underweight_summary() -> str:
    return "\n".join(
        [
            "评级：Underweight",
            (
                "操作计划：Reduce MRVL to approximately half the portfolio's normal "
                "weighting by selling into the $290-300 zone."
            ),
            "风控：Set a hard stop at $244.",
            "仓位：",
            "催化剂：Nvidia partnership remains supportive.",
            "目标价：200.0",
            "时间窗口：3-6 months",
            (
                "理由：The bear demonstrated that normalized earnings imply a "
                "~316x P/E, while MACD divergence and collapsing volume show "
                "technical exhaustion."
            ),
        ]
    )
```

In `test_build_trading_plan_extracts_structured_prices_and_writes_latest`, add:

```python
    assert rows[0]["agent_reason"] == "微软AI商业化路径清晰。"
    assert rows[0]["agent_excerpt"] == "微软AI商业化路径清晰。"
```

Add this new test after `test_build_trading_plan_extracts_structured_prices_and_writes_latest`:

```python
def test_build_trading_plan_extracts_agent_reason_and_excerpt(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "MRVL",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.29%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": mrvl_underweight_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))

    assert rows[0]["target_1"] == "200"
    assert rows[0]["agent_reason"].startswith(
        "TradingAgents建议减仓，原文依据：The bear demonstrated"
    )
    assert "normalized earnings imply a ~316x P/E" in rows[0]["agent_reason"]
    assert rows[0]["agent_excerpt"].startswith(
        "The bear demonstrated that normalized earnings imply a ~316x P/E"
    )
    assert "目标价：200.0" not in rows[0]["agent_reason"]
```

In `test_load_trading_plan_rows_reads_active_rows`, add these CSV values to the written row:

```python
                "agent_reason": "agent reason",
                "agent_excerpt": "agent excerpt",
```

And add these expected dataclass fields:

```python
            agent_reason="agent reason",
            agent_excerpt="agent excerpt",
```

In `test_load_trading_plan_rows_accepts_legacy_rows_without_source_status`, change the legacy optional field filter to:

```python
    legacy_fieldnames = [
        field
        for field in TRADING_PLAN_FIELDNAMES
        if field
        not in {
            "source_status",
            "fallback_reason",
            "fallback_from_date",
            "agent_reason",
            "agent_excerpt",
        }
    ]
```

And add:

```python
    assert rows[0].agent_reason == ""
    assert rows[0].agent_excerpt == ""
```

In `tests/test_trading_plan_cli.py`, update the direct `TradingPlanRow` constructor in `test_check_futu_plan_main_reports_plan_statuses` by adding:

```python
        agent_reason="",
        agent_excerpt="",
```

- [ ] **Step 2: Run the plan tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py -v
```

Expected: FAIL because `agent_reason` and `agent_excerpt` are not in `TRADING_PLAN_FIELDNAMES` or `TradingPlanRow`.

- [ ] **Step 3: Extend the trading plan schema and dataclass**

In `src/open_trader/trading_plan.py`, add the two new field names after `plan_text`:

```python
    "plan_text",
    "agent_reason",
    "agent_excerpt",
    "status",
```

Add the dataclass fields after `plan_text`:

```python
    plan_text: str
    agent_reason: str
    agent_excerpt: str
    status: str
```

Update `_base_plan_row()` parameters after `plan_text`:

```python
    plan_text: str = "",
    agent_reason: str = "",
    agent_excerpt: str = "",
    status: str,
```

Update the returned dict after `plan_text`:

```python
        "plan_text": plan_text,
        "agent_reason": agent_reason,
        "agent_excerpt": agent_excerpt,
        "status": status,
```

Update `_trading_plan_from_row()` after `plan_text`:

```python
        agent_reason=row.get("agent_reason", "").strip(),
        agent_excerpt=row.get("agent_excerpt", "").strip(),
        status=row.get("status", "").strip(),
```

Update `load_trading_plan_rows()` optional columns:

```python
        optional = {
            "source_status",
            "fallback_reason",
            "fallback_from_date",
            "agent_reason",
            "agent_excerpt",
        }
```

- [ ] **Step 4: Add deterministic agent excerpt helpers**

Add these helpers below `_parse_template()` in `src/open_trader/trading_plan.py`:

```python
def _agent_reason_and_excerpt(
    sections: dict[str, str],
    *,
    advice_action: str,
) -> tuple[str, str]:
    source = (
        sections.get("理由", "").strip()
        or sections.get("操作计划", "").strip()
        or " ".join(value.strip() for value in sections.values() if value.strip())
    )
    excerpt = _excerpt_text(source)
    if not excerpt:
        return "", ""
    if _contains_cjk(excerpt):
        return excerpt, excerpt
    action = advice_action.strip()
    if action:
        return f"TradingAgents建议{_action_reason_label(action)}，原文依据：{excerpt}", excerpt
    return f"TradingAgents原文依据：{excerpt}", excerpt


def _excerpt_text(text: str, *, max_chars: int = 220) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    sentence_parts = re.split(r"(?<=[。.!?])\s+", normalized)
    selected = sentence_parts[0].strip() if sentence_parts else normalized
    if len(selected) <= max_chars:
        return selected
    return selected[: max_chars - 1].rstrip() + "..."


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _action_reason_label(action: str) -> str:
    normalized = action.strip().lower()
    if any(word in normalized for word in ("underweight", "sell", "reduce", "trim")):
        return "减仓"
    if any(word in normalized for word in ("overweight", "buy", "accumulate", "add")):
        return "买入或加仓"
    if "hold" in normalized:
        return "继续持有"
    return action.strip()
```

- [ ] **Step 5: Populate the new fields while building active plan rows**

In `_plan_row_from_advice()`, after `sections = _parse_template(...)` and before `_base_plan_row(...)`, add:

```python
    agent_reason, agent_excerpt = _agent_reason_and_excerpt(
        sections,
        advice_action=row.get("advice_action", "").strip(),
    )
```

Then pass these values into the active `_base_plan_row()` call:

```python
        plan_text=row.get("advice_summary", "").strip(),
        agent_reason=agent_reason,
        agent_excerpt=agent_excerpt,
        status="active",
```

Do not populate these fields for `status="error"` rows. For `status="manual_review"` rows, leave them empty because the summary did not parse into the expected template.

- [ ] **Step 6: Run the plan tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add src/open_trader/trading_plan.py tests/test_trading_plan.py
git add tests/test_trading_plan_cli.py
git commit -m "feat: carry agent reasons in trading plans"
```

---

### Task 2: Propagate Agent Reasons Into Trade Actions

**Files:**
- Modify: `src/open_trader/trade_actions.py`
- Modify: `tests/test_trade_actions.py`

- [ ] **Step 1: Write failing tests for trade action schema and propagation**

In `tests/test_trade_actions.py`, update `test_trade_action_fieldnames_are_stable()` by inserting these fields after `"risk_to_stop"`:

```python
        "agent_reason",
        "agent_excerpt",
        "trigger_reason",
```

Update `active_plan()` to accept agent-field parameters:

```python
    agent_reason: str = "",
    agent_excerpt: str = "",
```

Then pass them into `TradingPlanRow` after `plan_text`:

```python
        agent_reason=agent_reason,
        agent_excerpt=agent_excerpt,
```

Update `msft_plan_row()` to accept agent-field parameters:

```python
    agent_reason: str = "",
    agent_excerpt: str = "",
```

Then include them in the returned row:

```python
        "agent_reason": agent_reason,
        "agent_excerpt": agent_excerpt,
```

Add this test near the other `build_trade_action_row` tests:

```python
def test_build_trade_action_row_preserves_agent_reason_and_trigger() -> None:
    row = build_trade_action_row(
        plan=active_plan(
            max_weight="",
            plan_text="操作计划：Reduce MSFT exposure at current levels.",
            agent_reason=(
                "TradingAgents建议减仓，原文依据：The bear demonstrated that "
                "normalized earnings imply a ~316x P/E."
            ),
            agent_excerpt=(
                "The bear demonstrated that normalized earnings imply a ~316x P/E."
            ),
        ),
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(),
        source_plan="plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["agent_reason"].startswith("TradingAgents建议减仓")
    assert row["agent_excerpt"] == (
        "The bear demonstrated that normalized earnings imply a ~316x P/E."
    )
    assert row["trigger_reason"] == "fixture message"
    assert row["reason"] == row["agent_reason"]
```

- [ ] **Step 2: Run the trade action tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions.py -v
```

Expected: FAIL because the new fields are not in `TRADE_ACTION_FIELDNAMES` and `TradingPlanRow` does not yet provide them in all test construction sites.

- [ ] **Step 3: Extend trade action field names**

In `src/open_trader/trade_actions.py`, add these fields after `"risk_to_stop"` in `TRADE_ACTION_FIELDNAMES`:

```python
    "agent_reason",
    "agent_excerpt",
    "trigger_reason",
    "reason",
```

- [ ] **Step 4: Populate the new fields in action rows**

In `build_trade_action_row()`, set `trigger_reason` from the quote status and prefer the plan agent reason for `reason`:

```python
    trigger_reason = quote_status.message
    reason = plan.agent_reason.strip() or trigger_reason
```

When plan text forces a buy into a trim, preserve the trigger and update only the fallback reason:

```python
    if action == "BUY" and _plan_text_implies_trim(plan.plan_text):
        action, priority = "TRIM", "medium"
        trigger_reason = "Plan text indicates trim at current levels."
        reason = plan.agent_reason.strip() or trigger_reason
```

Add the new row values before `"reason"`:

```python
        "agent_reason": plan.agent_reason.strip(),
        "agent_excerpt": plan.agent_excerpt.strip(),
        "trigger_reason": trigger_reason,
        "reason": reason,
```

Keep all existing sizing and status logic unchanged.

- [ ] **Step 5: Update any remaining test `TradingPlanRow` constructors**

Search:

```bash
rg -n "TradingPlanRow\\(" tests src
```

For every direct constructor that does not yet set the new fields, add:

```python
        agent_reason="",
        agent_excerpt="",
```

Constructors or helpers used for the new propagation test may set non-empty values; keep those.

- [ ] **Step 6: Run the trade action tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_actions.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add src/open_trader/trade_actions.py tests/test_trade_actions.py tests/test_trading_plan.py
git commit -m "feat: propagate agent reasons to trade actions"
```

---

### Task 3: Render Agent Reasons And Original Excerpts In Feishu Notifications

**Files:**
- Modify: `src/open_trader/notifications.py`
- Modify: `tests/test_notifications.py`

- [ ] **Step 1: Update notification test CSV field names**

In `tests/test_notifications.py`, insert these `FIELDNAMES` after `"risk_to_stop"`:

```python
    "agent_reason",
    "agent_excerpt",
    "trigger_reason",
```

Update `_action_row()` to include default new fields after `"risk_to_stop"`:

```python
        "agent_reason": "",
        "agent_excerpt": "",
        "trigger_reason": "",
        "reason": "ready fixture",
```

- [ ] **Step 2: Write failing notification tests for agent reason rendering**

Add this test after `test_render_feishu_order_review_keeps_ready_trim_with_blank_cost_and_stop`:

```python
def test_render_feishu_order_review_shows_agent_reason_excerpt_and_neutral_trim_trigger(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "trade_actions.csv"
    _write_actions(
        actions_path,
        [
            _action_row(
                symbol="MRVL",
                futu_symbol="US.MRVL",
                action="TRIM",
                priority="medium",
                last_price="289.54",
                trigger_status="target_1_hit",
                suggested_quantity="5",
                suggested_notional="1447.7",
                notional_currency="USD",
                current_quantity="10",
                current_weight="1.29%",
                avg_cost_price="169.81",
                limit_price="289.54",
                stop_price="",
                post_trade_quantity="5",
                post_trade_weight="0.91%",
                post_trade_avg_cost="169.81",
                risk_to_stop="",
                agent_reason=(
                    "TradingAgents建议减仓，原文依据：The bear demonstrated that "
                    "normalized earnings imply a ~316x P/E."
                ),
                agent_excerpt=(
                    "The bear demonstrated that normalized earnings imply a ~316x P/E."
                ),
                trigger_reason="Current price is at or above target 1.",
                reason=(
                    "TradingAgents建议减仓，原文依据：The bear demonstrated that "
                    "normalized earnings imply a ~316x P/E."
                ),
                status="ready",
            )
        ],
    )

    body = render_feishu_order_review(
        run_date="2026-06-18",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：TradingAgents建议减仓，原文依据：The bear demonstrated" in body
    assert "原文：The bear demonstrated that normalized earnings imply a ~316x P/E." in body
    assert "触发：当前价 289.54，行动已满足计划中的减仓/风控条件。" in body
    assert "目标价 1" not in body
    assert "Current price is at or above target 1." not in body
```

Add this legacy compatibility test:

```python
def test_render_feishu_order_review_supports_legacy_rows_without_agent_fields(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "legacy_trade_actions.csv"
    legacy_fieldnames = [
        field
        for field in FIELDNAMES
        if field not in {"agent_reason", "agent_excerpt", "trigger_reason"}
    ]
    with actions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fieldnames)
        writer.writeheader()
        row = _action_row(
            symbol="RKLB",
            futu_symbol="US.RKLB",
            action="ADD",
            reason="price entered entry zone",
            status="ready",
        )
        writer.writerow({field: row[field] for field in legacy_fieldnames})

    body = render_feishu_order_review(
        run_date="2026-06-17",
        status="success",
        actions_path=actions_path,
        report_paths=[],
    )

    assert "原因：价格进入计划买入区间。" in body
    assert "原文依据缺失，需人工复核。" in body
```

- [ ] **Step 3: Run the notification tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: FAIL because notification rendering does not yet use `agent_reason`, `agent_excerpt`, or `trigger_reason`.

- [ ] **Step 4: Add notification rendering helpers**

In `src/open_trader/notifications.py`, add these helpers above `_localized_note()`:

```python
def _agent_reason_line(row: Mapping[str, str]) -> str:
    agent_reason = row.get("agent_reason", "").strip()
    if agent_reason:
        return f"原因：{_sentence(agent_reason)}"
    fallback = _localized_note(row.get("reason", "").strip())
    if fallback:
        return f"原因：{_sentence(fallback)}"
    return "原因：原文依据缺失，需人工复核。"


def _agent_excerpt_line(row: Mapping[str, str]) -> str:
    excerpt = row.get("agent_excerpt", "").strip()
    if not excerpt:
        return ""
    return f"原文：{excerpt}"


def _missing_agent_reason_line(row: Mapping[str, str]) -> str:
    if row.get("agent_reason", "").strip():
        return ""
    return "原文依据缺失，需人工复核。"


def _trigger_reason_line(row: Mapping[str, str]) -> str:
    trigger_reason = row.get("trigger_reason", "").strip()
    action = row.get("action", "").strip().upper()
    last_price = row.get("last_price", "").strip()
    if action in {"TRIM", "TAKE_PROFIT", "SELL_STOP"} and trigger_reason:
        if action == "SELL_STOP":
            return f"触发：当前价 {last_price}，行动已满足计划中的止损条件。"
        return f"触发：当前价 {last_price}，行动已满足计划中的减仓/风控条件。"
    if trigger_reason:
        return f"触发：{_sentence(_localized_note(trigger_reason))}"
    fallback = row.get("reason", "").strip()
    if fallback:
        return f"触发：{_sentence(_localized_note(fallback))}"
    return ""
```

- [ ] **Step 5: Use new helpers in ready sections**

In `_render_ready_section()`, replace:

```python
            f"原因：{_localized_note(row.get('reason', '').strip())}",
```

with:

```python
            _agent_reason_line(row),
```

Then append excerpt, missing-source note, and trigger lines after the risk-control insert:

```python
    excerpt_line = _agent_excerpt_line(row)
    if excerpt_line:
        lines.append(excerpt_line)
    missing_agent_reason = _missing_agent_reason_line(row)
    if missing_agent_reason:
        lines.append(missing_agent_reason)
    trigger_line = _trigger_reason_line(row)
    if trigger_line:
        lines.append(trigger_line)
```

Keep `_risk_control_text()` insertion before the reason line.

- [ ] **Step 6: Use agent reason in blocked detail lines**

In `_blocked_detail_lines()`, replace:

```python
        f"原因：{_sentence(_localized_note(row.get('reason', '').strip()))}",
```

with:

```python
        _agent_reason_line(row),
```

This makes review rows show the original reason when available while preserving the existing fallback.

- [ ] **Step 7: Remove target-1 wording from localized trim fallback**

In `_localized_note()`, change:

```python
        "Current price is at or above target 1.": "当前价格已达到或高于目标价 1。",
        "Current price is at or above target 2.": "当前价格已达到或高于目标价 2。",
```

to:

```python
        "Current price is at or above target 1.": "当前价格已满足计划触发条件。",
        "Current price is at or above target 2.": "当前价格已满足计划触发条件。",
```

This protects legacy rows and blocked rows from using misleading target labels.

- [ ] **Step 8: Run the notification tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS after updating the old assertion in `test_render_feishu_order_review_translates_review_errors_and_hides_paths` from:

```python
    assert "原因：当前价格已达到或高于目标价 1。" in body
```

to:

```python
    assert "原因：当前价格已满足计划触发条件。" in body
```

- [ ] **Step 9: Commit Task 3**

Run:

```bash
git add src/open_trader/notifications.py tests/test_notifications.py
git commit -m "feat: show agent rationale in action notifications"
```

---

### Task 4: End-To-End Regression And June 18 Preview

**Files:**
- Modify only if tests reveal a missed wiring issue: `src/open_trader/trading_plan.py`, `src/open_trader/trade_actions.py`, `src/open_trader/notifications.py`, matching tests.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py tests/test_trade_actions.py tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 3: Regenerate June 18 plan and trade actions from existing local artifacts**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from decimal import Decimal
import csv
from open_trader.trading_plan import build_trading_plan, load_trading_plan_rows
from open_trader.trade_actions import generate_trade_actions
from open_trader.futu_watch import QuoteSnapshot

repo = Path(".")
run_date = "2026-06-18"
data_dir = repo / "data"
reports_dir = repo / "reports"
advice_path = data_dir / "runs" / run_date / "trading_advice.csv"
portfolio_path = data_dir / "latest" / "portfolio.csv"
existing_actions_path = data_dir / "runs" / run_date / "trade_actions.csv"

snapshots = {}
with existing_actions_path.open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        futu_symbol = row["futu_symbol"]
        last_price = row["last_price"]
        if futu_symbol and last_price:
            snapshots[futu_symbol] = QuoteSnapshot(
                futu_symbol,
                Decimal(last_price),
                run_date,
            )

plan_result = build_trading_plan(
    advice_path,
    data_dir,
    run_date=run_date,
    update_latest=False,
)
for plan in load_trading_plan_rows(plan_result.plan_path):
    if plan.futu_symbol not in snapshots:
        raise RuntimeError(f"missing prior quote snapshot for {plan.futu_symbol}")

result = generate_trade_actions(
    plan_path=plan_result.plan_path,
    portfolio_path=portfolio_path,
    data_dir=data_dir,
    reports_dir=reports_dir,
    snapshots=snapshots,
    run_date=run_date,
    update_latest=False,
)
print(result.actions_path)
PY
```

Expected: command exits 0 and rewrites dated `data/runs/2026-06-18/trading_plan.csv`, `data/runs/2026-06-18/trade_actions.csv`, and `reports/trade_actions/2026-06-18.md` without touching `data/latest`.

- [ ] **Step 4: Preview the June 18 notification body**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from open_trader.notifications import render_feishu_order_review

body = render_feishu_order_review(
    run_date="2026-06-18",
    status="success",
    actions_path=Path("data/runs/2026-06-18/trade_actions.csv"),
    report_paths=[],
)
print(body)
PY
```

Expected output:

- Contains `原文：` for MRVL, QQQ, SOXX, and VIXY ready actions.
- Contains `触发：当前价 ...，行动已满足计划中的减仓/风控条件。` for trim actions.
- Does not contain `目标价 1`.
- Does not contain raw English trigger text `Current price is at or above target 1.`.

- [ ] **Step 5: Inspect generated CSV headers**

Run:

```bash
.venv/bin/python - <<'PY'
import csv
for path in [
    "data/runs/2026-06-18/trading_plan.csv",
    "data/runs/2026-06-18/trade_actions.csv",
]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        print(path)
        print(csv.DictReader(handle).fieldnames)
PY
```

Expected: `trading_plan.csv` contains `agent_reason` and `agent_excerpt`; `trade_actions.csv` contains `agent_reason`, `agent_excerpt`, and `trigger_reason`.

- [ ] **Step 6: Commit verification fixture changes only if dated artifacts are intentionally tracked**

Run:

```bash
git status --short
```

If regenerated dated artifacts are tracked and the project normally keeps them, commit them with:

```bash
git add data/runs/2026-06-18/trading_plan.csv data/runs/2026-06-18/trade_actions.csv reports/trade_actions/2026-06-18.md
git commit -m "chore: refresh june 18 action artifacts"
```

If generated artifacts are not meant to be committed, leave them unstaged and mention them in the completion summary.

- [ ] **Step 7: Final implementation summary**

Before reporting completion, run:

```bash
git status --short
git log --oneline -5
```

Expected: working tree either clean or contains only explicitly explained generated artifacts. Final response should include the test commands and whether the June 18 preview no longer contains `目标价 1`.

---

## Self-Review Checklist

- Spec coverage: Task 1 implements plan fields and extraction; Task 2 carries fields into trade actions; Task 3 renders Chinese reason, original excerpt, and neutral trigger wording; Task 4 verifies tests and a June 18 preview.
- Backward compatibility: Task 1 and Task 3 include legacy CSV tests; Task 2 keeps the old `reason` column.
- Ambiguous target semantics: Task 3 removes target-1 wording from trim notifications and localized fallback.
- Machine-readable artifacts: Task 1 and Task 2 add explicit columns rather than burying source text only in notification rendering.
