# Threshold Hedge Validation Auto-Eat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Polymarket 同市场阈值对冲上实现验证期自动吃单：三档模式（观察/手动/auto）、利润硬门槛、频次/余额上限、飞书成功与结算通知、健康检查统计。

**Architecture:** 复用现有 `PredictionExecutionService` 的 preview→confirm→执行/对账链路；新增 sqlite 表（模式 + 尝试记录）与纯策略模块 `validation_eat_policy.py`；监控器只负责把 actionable 信号转给执行服务的 `auto_eat_threshold`；看板提供三档切换。

**Tech Stack:** Python 3.12、sqlite3、pytest、现有 Playwright e2e、launchd。

## Global Constraints

- 模式默认 `observe_only`，未显式切换到 `auto` 绝不下单。
- 利润硬门槛：年化 >15%（`MIN_THRESHOLD_ANNUALIZED_YIELD`）、按最差价格扣最大手续费后 `minimum_profit > 0` 且 `net_edge > 0`。
- 频次：每 `signal_id` 最多一次；关系对（`threshold:<hash>`）`submitted` 后冷静期 300 秒；每日 5 单或累计成本 $25（只统计 `submitted`，上海时区零点重置）。
- 余额 <$10 停止自动吃单。
- 失败不重试；所有决策落 `auto_eat_attempts`。
- 订单必须保持 FOK 限价买单语义，`max_price` 不变。
- 单量沿用现有 intent builder：venue 最低合法量起步、净边际为正前提下取最大数量，不新增 sizing 代码。
- 不新增第三方依赖；不修改 `MAX_NORMAL_COST` / 钱包余额上限；不新建结算逻辑。
- 跨市场保持只读，本计划只改同市场阈值对冲路径。

---

### Task 1: Store 支持验证模式与自动吃单记录

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Test: `tests/test_validation_eat_store.py`（新建）

**Interfaces:**
- Consumes: `PredictionArbitrageStore(data_dir)`、`_new_id()`、`_utc_now()`、`_parse_timestamp()`、`_transaction()`、`_read_connection()`。
- Produces:
  - `get_validation_mode() -> str`（默认 `observe_only`）
  - `set_validation_mode(mode: str) -> str`（非法值抛 `ValueError`）
  - `record_auto_eat_attempt(*, signal_id, market_id, decision, reason="", preview_id="", execution_id="", total_cost=None) -> str`
  - `auto_eat_attempt_exists(signal_id, decision) -> bool`
  - `last_submitted_auto_eat(market_id) -> str | None`
  - `auto_eat_stats(*, now=None) -> dict`：`mode`、`today_attempts`、`today_submitted`、`today_cost`、`rejected_by_reason`

- [ ] **Step 1: 写失败测试**

`tests/test_validation_eat_store.py`：

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def test_validation_mode_defaults_to_observe_only_and_persists(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    assert store.get_validation_mode() == "observe_only"
    assert store.set_validation_mode("manual") == "manual"
    assert PredictionArbitrageStore(tmp_path / "data").get_validation_mode() == "manual"
    try:
        store.set_validation_mode("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid mode must raise")


def test_auto_eat_attempts_and_stats(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.record_auto_eat_attempt(
        signal_id="s1", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    store.record_auto_eat_attempt(
        signal_id="s2", market_id="m1", decision="rejected", reason="cooldown"
    )
    stats = store.auto_eat_stats(now=datetime.now(UTC))
    assert stats["today_submitted"] == 1
    assert stats["today_cost"] == 5.0
    assert stats["realized_pnl"] == 0.0
    assert stats["rejected_by_reason"] == {"cooldown": 1}
    assert store.auto_eat_attempt_exists("s1", "submitted") is True
    assert store.last_submitted_auto_eat("m1") is not None
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_validation_eat_store.py -v`
Expected: FAIL（`AttributeError`，方法不存在）

- [ ] **Step 3: 实现**

`src/open_trader/prediction_arbitrage_store.py`：

在文件顶部 import 区加入 `from zoneinfo import ZoneInfo`。

在 `_create_schema` 的 `executescript` 末尾（`CREATE INDEX IF NOT EXISTS llm_usage_created_at ...` 之后）追加：

```sql
CREATE TABLE IF NOT EXISTS validation_mode (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    mode TEXT NOT NULL CHECK (mode IN ('observe_only', 'manual', 'auto')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_eat_attempts (
    attempt_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    preview_id TEXT,
    execution_id TEXT,
    total_cost TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS auto_eat_attempts_created_at
ON auto_eat_attempts(created_at);

CREATE INDEX IF NOT EXISTS auto_eat_attempts_signal
ON auto_eat_attempts(signal_id, decision);

CREATE INDEX IF NOT EXISTS auto_eat_attempts_market
ON auto_eat_attempts(market_id, created_at DESC);
```

在 `write_runtime` / `load_runtime` 附近新增方法：

```python
VALIDATION_MODES = frozenset({"observe_only", "manual", "auto"})

def get_validation_mode(self) -> str:
    with self._read_connection() as connection:
        row = connection.execute(
            "SELECT mode FROM validation_mode WHERE singleton=1"
        ).fetchone()
    if row is None or str(row["mode"]) not in VALIDATION_MODES:
        return "observe_only"
    return str(row["mode"])

def set_validation_mode(self, mode: str) -> str:
    if mode not in VALIDATION_MODES:
        raise ValueError(f"invalid validation mode: {mode}")
    with self._transaction() as connection:
        connection.execute(
            """
            INSERT INTO validation_mode(singleton, mode, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at
            """,
            (mode, _utc_now()),
        )
    return mode

def record_auto_eat_attempt(
    self,
    *,
    signal_id: str,
    market_id: str,
    decision: str,
    reason: str = "",
    preview_id: str = "",
    execution_id: str = "",
    total_cost: Decimal | None = None,
) -> str:
    attempt_id = _new_id()
    with self._transaction() as connection:
        connection.execute(
            """
            INSERT INTO auto_eat_attempts(
                attempt_id, signal_id, market_id, decision, reason,
                preview_id, execution_id, total_cost, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id, str(signal_id), str(market_id), str(decision), str(reason),
                str(preview_id), str(execution_id),
                _decimal_string(total_cost) if total_cost is not None else None,
                _utc_now(),
            ),
        )
    return attempt_id

def auto_eat_attempt_exists(self, signal_id: str, decision: str) -> bool:
    with self._read_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM auto_eat_attempts WHERE signal_id=? AND decision=? LIMIT 1",
            (str(signal_id), str(decision)),
        ).fetchone()
    return row is not None

def last_submitted_auto_eat(self, market_id: str) -> str | None:
    with self._read_connection() as connection:
        row = connection.execute(
            """
            SELECT created_at FROM auto_eat_attempts
            WHERE market_id=? AND decision='submitted'
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(market_id),),
        ).fetchone()
    return None if row is None else str(row["created_at"])

def auto_eat_stats(self, *, now: datetime | None = None) -> dict[str, object]:
    current = now or _parse_timestamp(_utc_now())
    day_start = (
        current.astimezone(ZoneInfo("Asia/Shanghai"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .isoformat(timespec="seconds")
    )
    with self._read_connection() as connection:
        row = connection.execute(
            """
            SELECT count(*),
                   coalesce(sum(CASE WHEN decision='submitted' THEN 1 ELSE 0 END), 0),
                   coalesce(sum(CASE WHEN decision='submitted' AND total_cost IS NOT NULL
                                     THEN CAST(total_cost AS REAL) ELSE 0 END), 0)
            FROM auto_eat_attempts WHERE created_at >= ?
            """,
            (day_start,),
        ).fetchone()
        rejected = connection.execute(
            """
            SELECT reason, count(*) FROM auto_eat_attempts
            WHERE decision='rejected' AND created_at >= ? GROUP BY reason
            """,
            (day_start,),
        ).fetchall()
        realized = connection.execute(
            """
            SELECT coalesce(sum(
                CASE WHEN state = 'holding_to_resolution'
                      AND json_extract(payload, '$.auto_eat') = json('true')
                     THEN COALESCE(json_extract(payload, '$.minimum_profit'), 0)
                     ELSE 0 END
            ), 0)
            FROM executions WHERE created_at >= ?
            """,
            (day_start,),
        ).fetchone()
    return {
        "mode": self.get_validation_mode(),
        "today_attempts": int(row[0]),
        "today_submitted": int(row[1]),
        "today_cost": float(row[2] or 0.0),
        "realized_pnl": float(realized[0] or 0.0),
        "rejected_by_reason": {str(item[0]): int(item[1]) for item in rejected},
    }
```

注意：`datetime` 已在文件顶部导入（`from datetime import UTC, datetime, timedelta`）；若没有 `Decimal`，在顶部 import 区补 `from decimal import Decimal`。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_validation_eat_store.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/prediction_arbitrage_store.py tests/test_validation_eat_store.py
git commit -m "feat: add validation mode and auto-eat attempts store (#33)"
```

---

### Task 2: `validation_eat_policy.py` 策略模块

**Files:**
- Create: `src/open_trader/validation_eat_policy.py`
- Test: `tests/test_validation_eat_policy.py`（新建）

**Interfaces:**
- Consumes: `PredictionArbitrageStore`（Task 1 方法）、`ThresholdHedgeIntent.total_max_cost`
- Produces: `should_eat(*, store, signal, intent, balance, now) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

`tests/test_validation_eat_policy.py`：

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.validation_eat_policy import (
    DAILY_COST_LIMIT,
    MIN_BALANCE_FLOOR,
    should_eat,
)


def _signal(market_id: str = "m1", signal_id: str = "s1") -> dict[str, object]:
    return {"signal_id": signal_id, "market_id": market_id}


def _intent(cost: Decimal = Decimal("5.00")) -> SimpleNamespace:
    return SimpleNamespace(total_max_cost=cost)


def test_should_eat_allows_first_order_in_auto_mode(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    allowed, reason = should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=datetime.now(UTC),
    )
    assert allowed is True
    assert reason == ""


def test_should_eat_rejects_when_mode_not_auto(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    allowed, reason = should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=datetime.now(UTC),
    )
    assert (allowed, reason) == (False, "mode_not_auto")


def test_should_eat_rejects_duplicate_episode_and_cooldown(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    now = datetime.now(UTC)
    store.record_auto_eat_attempt(
        signal_id="s1", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "episode_duplicate")
    store.record_auto_eat_attempt(
        signal_id="s9", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    assert should_eat(
        store=store, signal=_signal(signal_id="s2"), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "cooldown")


def test_should_eat_rejects_balance_and_daily_caps(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    now = datetime.now(UTC)
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=MIN_BALANCE_FLOOR - Decimal("1"), now=now,
    ) == (False, "insufficient_balance")
    store.record_auto_eat_attempt(
        signal_id="x1", market_id="x1", decision="submitted",
        total_cost=DAILY_COST_LIMIT,
    )
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "daily_cost_cap")
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_validation_eat_policy.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**

`src/open_trader/validation_eat_policy.py`：

```python
"""Validation-phase auto-eat policy: pure gates plus store-backed counters."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Mapping

from .prediction_arbitrage import ThresholdHedgeIntent
from .prediction_arbitrage_store import PredictionArbitrageStore


MIN_BALANCE_FLOOR = Decimal("10.00")
MARKET_COOLDOWN_SECONDS = 300.0
DAILY_ORDER_LIMIT = 5
DAILY_COST_LIMIT = Decimal("25.00")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def should_eat(
    *,
    store: PredictionArbitrageStore,
    signal: Mapping[str, object],
    intent: ThresholdHedgeIntent,
    balance: Decimal,
    now: datetime,
) -> tuple[bool, str]:
    if store.get_validation_mode() != "auto":
        return False, "mode_not_auto"
    signal_id = str(signal.get("signal_id") or "")
    market_id = str(signal.get("market_id") or "")
    if not signal_id or not market_id:
        return False, "signal_unavailable"
    if store.auto_eat_attempt_exists(signal_id, "submitted"):
        return False, "episode_duplicate"
    last = store.last_submitted_auto_eat(market_id)
    if last is not None:
        last_time = _parse(last)
        if last_time is not None and (now - last_time).total_seconds() < MARKET_COOLDOWN_SECONDS:
            return False, "cooldown"
    if balance < MIN_BALANCE_FLOOR:
        return False, "insufficient_balance"
    stats = store.auto_eat_stats(now=now)
    if int(stats["today_submitted"]) >= DAILY_ORDER_LIMIT:
        return False, "daily_cap"
    if Decimal(str(stats["today_cost"])) + intent.total_max_cost > DAILY_COST_LIMIT:
        return False, "daily_cost_cap"
    return True, ""
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_validation_eat_policy.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/validation_eat_policy.py tests/test_validation_eat_policy.py
git commit -m "feat: add validation auto-eat policy gates (#33)"
```

---

### Task 3: 执行服务 `auto_eat_threshold` 自动吃单

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**
- Consumes: `preview()`、`confirm()`、`_prepare_opportunity()`、`_deliver_feishu_notification()`、Task 1/2 方法。
- Produces:
  - `set_validation_mode(mode) -> dict`
  - `auto_eat_threshold(opportunity_id, signal_id) -> dict`
  - `preview(opportunity_id, *, auto_eat=False)`（preview payload 增加 `auto_eat` 标记）

- [ ] **Step 1: 写失败测试**

在 `tests/test_prediction_arbitrage_execution.py` 的 `test_ready_notification_sends_only_after_read_only_proof` 附近追加：

先给 `_notification_signal` fixture 的 payload 补上 `"market_type": "threshold_hedge"`（该 fixture 当前缺少该字段，auto 模式守卫需要它；不影响现有通知测试）：

```python
def _notification_signal(store: PredictionArbitrageStore) -> str:
    return store.upsert_signal(
        {
            "market_id": "relation-1",
            "event_id": "event-threshold",
            "market_type": "threshold_hedge",
            "question": "Fed cuts",
            "started_at": datetime.now(UTC).isoformat(),
            "first_positive_at": datetime.now(UTC).isoformat(),
            "net_edge": Decimal("0.788"),
            "estimated_profit": Decimal("7.88"),
            "notification_state": "pending",
            "notification_attempts": 0,
        }
    )
```

```python
def test_auto_eat_threshold_ignored_outside_auto_mode(tmp_path: Path) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)

    result = service.auto_eat_threshold("threshold-opp-1", signal_id)

    assert result == {"state": "ignored", "reason": "mode_not_auto"}
    assert trading.threshold_submit_calls == 0
    assert store.auto_eat_stats()["today_attempts"] == 0


def test_auto_eat_threshold_submits_once_in_auto_mode(tmp_path: Path) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    store.set_validation_mode("auto")
    signal_id = _notification_signal(store)

    result = service.auto_eat_threshold("threshold-opp-1", signal_id)

    assert result["state"] == "validating" or result.get("execution_id")
    assert trading.threshold_submit_calls == 1
    assert store.auto_eat_stats()["today_submitted"] == 1
    assert len(store.histories("executions")) == 1
    assert service.notify_ready_opportunity(
        "threshold-opp-1", signal_id
    ) == {"state": "ignored", "reason": "mode_auto"}


def test_auto_eat_threshold_rejects_when_daily_cost_cap_reached(
    tmp_path: Path,
) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    store.set_validation_mode("auto")
    store.record_auto_eat_attempt(
        signal_id="other", market_id="other", decision="submitted",
        total_cost=Decimal("25.00"),
    )
    signal_id = _notification_signal(store)

    result = service.auto_eat_threshold("threshold-opp-1", signal_id)

    assert result == {"state": "rejected", "reason": "daily_cost_cap"}
    assert trading.threshold_submit_calls == 0
    assert store.auto_eat_stats()["rejected_by_reason"] == {"daily_cost_cap": 1}
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_execution.py -k auto_eat_threshold -v`
Expected: FAIL（`AttributeError`，方法不存在）

- [ ] **Step 3: 实现**

`src/open_trader/prediction_arbitrage_execution.py`：

顶部 import 区追加：

```python
from .validation_eat_policy import should_eat as _validation_should_eat
```

`preview()` 签名改为 `def preview(self, opportunity_id: str, *, auto_eat: bool = False) -> dict[str, object]:`，并在 `preview_id = self._store.create_preview(...)` 之前插入：

```python
        if auto_eat:
            payload["auto_eat"] = True
```

在现有 `notify_ready_opportunity` 的 `signal = self._store.signal(str(signal_id))` 检查之后、`if signal.get("market_type") == "cross_venue_yes_no":` 之前插入：

```python
        if (
            signal.get("market_type") == "threshold_hedge"
            and self._store.get_validation_mode() == "auto"
        ):
            return {"state": "ignored", "reason": "mode_auto"}
```

在 `notify_ready_opportunity` 之后新增：

```python
    def set_validation_mode(self, mode: str) -> dict[str, object]:
        try:
            value = self._store.set_validation_mode(mode)
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}
        return {"state": "ok", "mode": value}

    def auto_eat_threshold(
        self, opportunity_id: str, signal_id: str
    ) -> dict[str, object]:
        signal = self._store.signal(str(signal_id))
        if signal is None:
            return {"state": "ignored", "reason": "signal_unavailable"}
        if self._store.get_validation_mode() != "auto":
            return {"state": "ignored", "reason": "mode_not_auto"}
        market_id = str(signal.get("market_id") or "")
        prepared = self._prepare_opportunity(str(opportunity_id))
        if isinstance(prepared, dict):
            reason = str(prepared.get("reason") or "not_ready")
            if reason != "circuit_breaker_open":
                self._store.record_auto_eat_attempt(
                    signal_id=str(signal_id), market_id=market_id,
                    decision="rejected", reason=reason,
                )
            return prepared
        opportunity, intent, account = prepared
        if not isinstance(intent, ThresholdHedgeIntent):
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="unsupported_intent",
            )
            return {"state": "rejected", "reason": "unsupported_intent"}
        annualized = _decimal(opportunity.get("annualized_yield"))
        if (
            annualized is None
            or annualized <= MIN_THRESHOLD_ANNUALIZED_YIELD
        ):
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="annualized_below_minimum",
            )
            return {"state": "rejected", "reason": "annualized_below_minimum"}
        if intent.minimum_profit <= 0 or intent.net_edge <= 0:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="threshold_economics",
            )
            return {"state": "rejected", "reason": "threshold_economics"}
        balance = _decimal(account.get("p_usd_balance")) or Decimal("0")
        allowed, reason = _validation_should_eat(
            store=self._store, signal=signal, intent=intent,
            balance=balance, now=datetime.now(UTC),
        )
        if not allowed:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason=reason,
            )
            return {"state": "rejected", "reason": reason}
        preview_result = self.preview(str(opportunity_id), auto_eat=True)
        if not isinstance(preview_result, Mapping) or preview_result.get("state") != "previewed":
            reason = str(preview_result.get("reason") or "preview_failed")
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason=reason,
            )
            return preview_result
        preview_id = str(preview_result["preview_id"])
        confirm_result = self.confirm(preview_id, str(signal_id))
        execution_id = str(confirm_result.get("execution_id") or "")
        confirm_state = str(confirm_result.get("state") or "")
        if execution_id or confirm_state in {
            "validating", "submitting", "reconciling", "holding_to_resolution",
        }:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="submitted", preview_id=preview_id,
                execution_id=execution_id, total_cost=intent.total_max_cost,
            )
            self._notify_auto_eat_success(opportunity, intent, preview_result)
            return confirm_result
        reason = str(confirm_result.get("reason") or "confirm_failed")
        self._store.record_auto_eat_attempt(
            signal_id=str(signal_id), market_id=market_id,
            decision="rejected", reason=reason, preview_id=preview_id,
        )
        return confirm_result

    def _notify_auto_eat_success(
        self,
        opportunity: Mapping[str, object],
        intent: ThresholdHedgeIntent,
        preview_result: Mapping[str, object],
    ) -> None:
        try:
            title = "预测套利验证单已吃"
            message = (
                f"{opportunity.get('question') or opportunity.get('question_a') or ''}\n"
                f"数量 {intent.quantity} · 成本 ${intent.total_max_cost:.4f} · "
                f"预计利润 ${intent.minimum_profit:.4f}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return
```

说明：`_decimal` 和 `ThresholdHedgeIntent` 已在模块内可用；`datetime.now(UTC)` 使用已有 `from datetime import UTC, datetime`。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_execution.py -k "auto_eat_threshold or ready_notification" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/prediction_arbitrage_execution.py tests/test_prediction_arbitrage_execution.py
git commit -m "feat: auto-eat threshold hedge in validation mode (#33)"
```

---

### Task 4: 监控器转发 actionable 信号给自动吃单

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py`
- Test: `tests/test_polymarket_monitor.py`

**Interfaces:**
- Consumes: `PolymarketMonitor`、`_upsert_signal()`、`_store.signal()`
- Produces: `set_auto_eat_observer(observer)`、`_schedule_auto_eat(signal_id, opportunity)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_polymarket_monitor.py` 的 `make_monitor` 相关测试附近追加：

```python
def test_auto_eat_observer_runs_once_for_actionable_threshold(
    tmp_path: Path,
) -> None:
    import asyncio

    monitor = make_monitor(tmp_path)
    calls: list[tuple[str, str]] = []
    monitor.set_auto_eat_observer(
        lambda opportunity_id, signal_id: calls.append((opportunity_id, signal_id))
    )
    signal_id = monitor._store.upsert_signal(
        {
            "market_id": "threshold:abc",
            "event_id": "e1",
            "question": "Q",
            "started_at": NOW.isoformat(),
            "first_positive_at": NOW.isoformat(),
            "net_edge": Decimal("0.1"),
            "estimated_profit": Decimal("1"),
            "profit": Decimal("1"),
            "market_type": "threshold_hedge",
            "annualized_yield": Decimal("0.20"),
            "eligibility_reason": "actionable",
            "llm_status": "approved",
            "rules_verified_at": NOW.isoformat(),
        }
    )
    opportunity = {
        "market_type": "threshold_hedge",
        "actionable": True,
        "market_id": "threshold:abc",
        "event_id": "e1",
        "question": "Q",
        "opportunity_id": "threshold:abc",
        "rules_verified_at": NOW.isoformat(),
        "relation_validation": {"status": "approved"},
    }

    monitor._schedule_auto_eat(signal_id, opportunity)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(monitor._auto_eat_task)
    finally:
        loop.close()

    assert calls == [("threshold:abc", signal_id)]


def test_auto_eat_observer_skips_non_actionable(tmp_path: Path) -> None:
    monitor = make_monitor(tmp_path)
    calls: list[tuple[str, str]] = []
    monitor.set_auto_eat_observer(
        lambda opportunity_id, signal_id: calls.append((opportunity_id, signal_id))
    )
    monitor._schedule_auto_eat("s1", {
        "market_type": "threshold_hedge",
        "actionable": False,
        "market_id": "threshold:abc",
        "event_id": "e1",
        "question": "Q",
        "opportunity_id": "threshold:abc",
        "rules_verified_at": NOW.isoformat(),
        "relation_validation": {"status": "approved"},
    })
    assert calls == []
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_polymarket_monitor.py -k auto_eat_observer -v`
Expected: FAIL（`AttributeError`）

- [ ] **Step 3: 实现**

`src/open_trader/polymarket_monitor.py`：

在 `__init__` 的 observer 字段附近（`self._ready_observer` 下方）加入：

```python
        self._auto_eat_observer: Callable[[str, str], object] | None = None
        self._auto_eat_task: asyncio.Task[object] | None = None
```

在 `set_observation_observer` 之后加入：

```python
    def set_auto_eat_observer(
        self, observer: Callable[[str, str], object]
    ) -> None:
        self._auto_eat_observer = observer

    def _reap_auto_eat_task(self) -> None:
        task = self._auto_eat_task
        if task is None or not task.done():
            return
        try:
            task.result()
        except Exception:
            pass
        self._auto_eat_task = None
```

在 `_schedule_ready_notification` 定义之前加入 `_schedule_auto_eat`（守卫与 ready notification 相同）：

```python
    def _schedule_auto_eat(
        self, signal_id: str | None, opportunity: Mapping[str, object]
    ) -> None:
        observer = self._auto_eat_observer
        if observer is None or signal_id is None:
            return
        if opportunity.get("market_type") != "threshold_hedge":
            return
        if opportunity.get("actionable") is not True:
            return
        if opportunity.get("rules_verified_at") in (None, ""):
            return
        validation = opportunity.get("relation_validation")
        codex_status = (
            validation.get("status")
            if isinstance(validation, Mapping)
            else opportunity.get("llm_status")
        )
        if str(codex_status).strip().lower() != "approved":
            return
        self._reap_auto_eat_task()
        task = self._auto_eat_task
        if task is not None and not task.done():
            return
        signal = self._store.signal(str(signal_id))
        if signal is None or signal.get("ended_at") is not None:
            return
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        if not opportunity_id:
            return
        self._auto_eat_task = asyncio.create_task(
            asyncio.to_thread(observer, opportunity_id, str(signal_id))
        )
```

在 `_upsert_signal` 的 `self._schedule_ready_notification(signal_id, opportunity)` 之后追加一行：

```python
            self._schedule_auto_eat(signal_id, opportunity)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_polymarket_monitor.py -k auto_eat_observer -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "feat: wire threshold auto-eat observer from monitor (#33)"
```

---

### Task 5: Dashboard 状态暴露模式/统计 + 模式切换 API

**Files:**
- Modify: `src/open_trader/dashboard_web.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `_prediction_state_payload()`、`PredictionExecutionService.set_validation_mode()`（Task 3）
- Produces: state payload 新增 `validation_mode`、`auto_eat_stats`；POST `/api/prediction-arbitrage/mode`

- [ ] **Step 1: 写失败测试**

在 `tests/test_dashboard_web.py` 追加（若文件已有 `PredictionArbitrageStore` 导入则复用，否则补 import）：

```python
def test_prediction_state_payload_includes_validation_mode_and_stats(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard_web import _prediction_state_payload
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("manual")
    payload = _prediction_state_payload(
        store=store, monitor=None, execution=None, csrf_token="csrf"
    )

    assert payload["validation_mode"] == "manual"
    assert payload["auto_eat_stats"]["today_submitted"] == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_dashboard_web.py -k validation_mode -v`
Expected: FAIL（KeyError）

- [ ] **Step 3: 实现**

`src/open_trader/dashboard_web.py`：

在 `_prediction_state_payload` 的 `venues = _prediction_venues_payload(...)` 之前加入：

```python
    validation_mode = "observe_only"
    auto_eat_stats: dict[str, object] = {}
    if store is not None:
        get_mode = getattr(store, "get_validation_mode", None)
        if callable(get_mode):
            try:
                validation_mode = get_mode()
            except Exception:
                validation_mode = "observe_only"
        get_stats = getattr(store, "auto_eat_stats", None)
        if callable(get_stats):
            try:
                auto_eat_stats = get_stats()
            except Exception:
                auto_eat_stats = {}
```

在 return dict 中加入：

```python
        "validation_mode": validation_mode,
        "auto_eat_stats": auto_eat_stats,
```

在 `do_POST` 的路径集合中加入 `"/api/prediction-arbitrage/mode"`，并在 `elif path.endswith("/executions"):` 分支后加入：

```python
                    elif path.endswith("/mode"):
                        self._require_prediction_schema(payload, {"mode"})
                        mode = self._required_prediction_string(payload, "mode")
                        result = prediction_execution_service.set_validation_mode(mode)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_dashboard_web.py -k validation_mode -v`
Expected: PASS

手工冒烟（本步骤在测试通过后执行一次）：

```bash
curl -s -X POST http://127.0.0.1:8766/api/prediction-arbitrage/mode \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $(curl -s http://127.0.0.1:8766/api/prediction-arbitrage/state | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrf_token"])')" \
  -d '{"mode":"manual"}'
```

Expected: `{"state":"ok","mode":"manual"}`，随后切回 `observe_only`。

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: expose validation mode and mode-switch API (#33)"
```

---

### Task 6: 看板三档模式按钮

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/index.html`（如需要容器）

**Interfaces:**
- Consumes: state payload 的 `validation_mode` / `auto_eat_stats`、`predictionPost()`、`fetchPredictionState()`
- Produces: `predictionModeBar(payload)` + `data-action="set-mode"`

- [ ] **Step 1: 实现 `predictionModeBar`**

在 `renderPredictionMarket` 之前加入：

```js
function predictionModeBar(payload) {
  const mode = payload.validation_mode || "observe_only";
  const stats = payload.auto_eat_stats || {};
  const modes = [
    ["observe_only", "观察"],
    ["manual", "手动"],
    ["auto", "auto"],
  ];
  const buttons = modes.map(([value, label]) =>
    `<button type="button" class="pm-mode-button${mode === value ? " active" : ""}" data-action="set-mode" data-mode="${value}">${label}</button>`
  ).join("");
  return `<div class="pm-mode-bar" aria-label="验证期吃单模式">${buttons}<span class="pm-mode-stats">今日 ${stats.today_submitted || 0} 单 / $${Number(stats.today_cost || 0).toFixed(2)}</span></div>`;
}
```

把 `renderPredictionMarket` 的 `root.innerHTML` 改为：

```js
  root.innerHTML = `${predictionPageHeader(viewPayload)}${predictionModeBar(viewPayload)}${predictionReadinessStrip(viewPayload, strategy)}${predictionSafeguardsHtml(viewPayload)}${predictionStrategyTabs(strategy)}${predictionErrorAlert()}${predictionExecutionAlert(viewPayload, strategy)}${workspace}`;
```

- [ ] **Step 2: 实现点击切换**

在 `handlePredictionMarketClick` 的 `const participate = ...` 之前加入：

```js
  const modeButton = event.target.closest("[data-action='set-mode']");
  if (modeButton) {
    const mode = String(modeButton.dataset.mode || "");
    if (!["observe_only", "manual", "auto"].includes(mode)) return;
    try {
      await predictionPost("/api/prediction-arbitrage/mode", {mode});
    } catch (error) {
      state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    }
    await fetchPredictionState();
    return;
  }
```

- [ ] **Step 3: 验证前端**

Run: `npm run test:e2e -- tests/e2e/prediction-market.spec.ts`（或仓库现有的 e2e 命令）
Expected: 现有 prediction-market e2e 仍通过；浏览器打开 `http://127.0.0.1:8766/#/prediction_market` 可见三档按钮，点击切换后状态刷新。

- [ ] **Step 4: 提交**

```bash
git add src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: add validation mode switcher to prediction dashboard (#33)"
```

---

### Task 7: 结算通知（实际 vs 预计利润）

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**
- Consumes: `_run_threshold_execution()`、`_store.histories("execution_legs")`、`_deliver_feishu_notification()`
- Produces: `_notify_threshold_settlement(execution_id, intent, quantity, proof)`

- [ ] **Step 1: 写失败测试**

追加：

```python
def test_threshold_settlement_notifies_only_auto_eat_executions(
    tmp_path: Path,
) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    store.set_validation_mode("auto")
    signal_id = _notification_signal(store)
    service.auto_eat_threshold("threshold-opp-1", signal_id)

    assert trading.threshold_submit_calls == 1
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(
        "结算" in title for title, _ in feishu.calls
    ):
        time.sleep(0.05)
    assert any("验证单" in title for title, _ in feishu.calls)
    assert any(
        "结算" in title and "预计利润" in message
        for title, message in feishu.calls
    )
    assert store.auto_eat_stats()["realized_pnl"] > 0
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_execution.py -k settlement_notifies -v`
Expected: FAIL（无结算通知标题）

- [ ] **Step 3: 实现**

在 `_run_threshold_execution` 的 `holding_to_resolution` transition 之后追加：

```python
        self._notify_threshold_settlement(
            execution_id, intent, quantity_a, proof
        )
```

新增方法：

```python
    def _notify_threshold_settlement(
        self,
        execution_id: str,
        intent: ThresholdHedgeIntent,
        quantity: Decimal,
        proof: Mapping[str, object],
    ) -> None:
        row = next(
            (
                item for item in self._store.histories("executions")
                if str(item.get("execution_id")) == str(execution_id)
            ),
            None,
        )
        payload = row.get("payload") if isinstance(row, Mapping) else {}
        if not isinstance(payload, Mapping) or payload.get("auto_eat") is not True:
            return
        expected = intent.minimum_profit
        actual = quantity - (
            intent.total_max_cost / intent.quantity * quantity
        )
        try:
            title = "预测套利验证单结算"
            message = (
                f"关系 {intent.relation_id}\n"
                f"成交数量 {quantity}\n"
                f"预计利润 ${expected:.4f}\n"
                f"实际锁定利润 ${actual:.4f}\n"
                f"证明已验证: {proof.get('verified') is True}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_execution.py -k settlement_notifies -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/prediction_arbitrage_execution.py tests/test_prediction_arbitrage_execution.py
git commit -m "feat: notify threshold settlement for auto-eat orders (#33)"
```

---

### Task 8: 健康检查加入自动吃单统计

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_health.py`
- Test: `tests/test_prediction_arbitrage_health.py`

**Interfaces:**
- Consumes: state payload 的 `auto_eat_stats`（Task 5）
- Produces: `auto_eat` check + summary 字段

- [ ] **Step 1: 写失败测试**

在 `tests/test_prediction_arbitrage_health.py` 的 `run_health_check` 测试附近追加：

```python
def test_health_check_reports_auto_eat_stats(tmp_path: Path) -> None:
    payload = {
        "status": "healthy",
        "health": {
            "status": "healthy",
            "heartbeat_age_seconds": 1.0,
            "universe_age_seconds": 1.0,
        },
        "stale": False,
        "breaker": {"open": False},
        "cross_venue": {"status": "ready", "funnel": {}},
        "relation_discovery": {"status": "healthy", "catalog": {"status": "healthy"}},
        "readiness": {"ready": True},
        "auto_eat_stats": {
            "mode": "auto",
            "today_attempts": 3,
            "today_submitted": 0,
            "today_cost": 0.0,
        },
    }

    report = run_health_check(
        url="http://127.0.0.1:8766",
        data_dir=tmp_path,
        repo_root=tmp_path,
        fetch_state=lambda *args, **kwargs: payload,
        fetch_healthz=lambda *args, **kwargs: True,
        llm_stats=lambda *args, **kwargs: (1, 1),
        process_info=lambda *args, **kwargs: {
            "pid": "1", "sha": "abc", "expected_sha": "abc",
        },
        notify_configured=False,
    )

    checks = {check.name: check for check in report.checks}
    assert checks["auto_eat"].status == "WARN"
    assert "submitted=0" in checks["auto_eat"].value
    assert report.summary["validation_mode"] == "auto"
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_health.py -k auto_eat -v`
Expected: FAIL（KeyError）

- [ ] **Step 3: 实现**

在 `run_health_check` 的 `readiness` 检查之后加入：

```python
    auto_eat = _mapping(payload.get("auto_eat_stats"))
    mode = str(auto_eat.get("mode") or "observe_only")
    submitted = int(auto_eat.get("today_submitted") or 0)
    attempts = int(auto_eat.get("today_attempts") or 0)
    rejected = int(auto_eat.get("today_attempts") or 0) - submitted
    realized = float(auto_eat.get("realized_pnl") or 0.0)
    if mode == "auto" and attempts > 0 and submitted == 0:
        add("auto_eat", "WARN",
            value=f"mode={mode} submitted={submitted} rejected={rejected} realized={realized:.4f}",
            reason="auto mode active but every attempt rejected")
    else:
        add("auto_eat", "PASS",
            value=f"mode={mode} submitted={submitted} rejected={rejected} realized={realized:.4f}")
```

在 `summary` dict 中加入：

```python
            "validation_mode": mode,
            "auto_eat_submitted": submitted,
            "auto_eat_rejected": rejected,
            "auto_eat_realized_pnl": realized,
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_prediction_arbitrage_health.py -k auto_eat -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/open_trader/prediction_arbitrage_health.py tests/test_prediction_arbitrage_health.py
git commit -m "feat: surface auto-eat stats in health check (#33)"
```

---

### Task 9: 回归、Changelog 与收尾

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 全量相关测试**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_validation_eat_store.py tests/test_validation_eat_policy.py tests/test_prediction_arbitrage_execution.py tests/test_polymarket_monitor.py tests/test_dashboard_web.py tests/test_prediction_arbitrage_health.py -q
```

Expected: 全部 PASS

- [ ] **Step 2: 更新 Changelog**

在 `CHANGELOG.md` 顶部加入当日条目，至少包含：

```markdown
## 2026-08-07

- 预测套利：新增验证期自动吃单（三档模式、利润/频次/余额硬门槛、飞书成功与结算通知、健康检查统计）（#33）
```

- [ ] **Step 3: 提交**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for validation auto-eat (#33)"
```

- [ ] **Step 4: 最终状态检查**

Run: `git status --short && git log --oneline -10`
Expected: 工作区干净，提交历史包含 Task 1-9。
