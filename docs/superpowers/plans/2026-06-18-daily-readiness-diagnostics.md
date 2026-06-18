# Daily Readiness Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured Futu diagnostics and daily readiness semantics so every automated daily run clearly says whether its output is ready, blocked, or requires manual review.

**Architecture:** Extend the existing Futu quote error type with stable diagnostic metadata, then keep daily orchestration inside `DailyPremarketRunner`. Add small pure helpers in `daily_premarket.py` for status reasons, readiness, Chinese labels, and report/notification text so behavior is easy to test without real Futu OpenD.

**Tech Stack:** Python 3.12 dataclasses, existing argparse/pytest style, existing `Notifier` protocol, existing CSV/Markdown artifact writers.

---

## File Structure

- Modify `src/open_trader/futu_quote.py`
  - Give `FutuQuoteError` structured metadata: `error_type`, `next_step`, `opend_reachable`, `context_ok`, and `snapshot_ok`.
  - Classify OpenD unreachable, context failure, quote-server interruption, and generic snapshot failure at the source.

- Modify `src/open_trader/daily_premarket.py`
  - Add helpers to build Futu diagnostic dictionaries.
  - Add helpers to derive `status_reasons`, top-level `status`, and `readiness`.
  - Add Chinese labels and next-step rendering for Markdown reports and Feishu blocker notifications.
  - Preserve current latest artifact promotion and CSV output behavior.

- Modify `tests/test_futu_quote.py`
  - Cover typed Futu quote errors and Chinese next-step text.

- Modify `tests/test_daily_premarket.py`
  - Cover readiness/status reasons for OpenD unreachable, quote-server interruption, missing quotes, fallback, failure, and success.
  - Cover Chinese report and Feishu blocker notification text.

---

### Task 1: Add Structured Futu Quote Errors

**Files:**
- Modify: `tests/test_futu_quote.py`
- Modify: `src/open_trader/futu_quote.py`

- [ ] **Step 1: Add failing tests for Futu error metadata**

Add these tests to `tests/test_futu_quote.py` after `FakeFailingContext`:

```python
class FakeInterruptedContext(FakeOpenQuoteContext):
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        return -1, "网络中断"


def test_futu_quote_error_preserves_diagnostic_metadata() -> None:
    error = FutuQuoteError(
        "网络中断",
        error_type="quote_server_interrupted",
        next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=False,
    )

    assert str(error) == "网络中断"
    assert error.error_type == "quote_server_interrupted"
    assert error.next_step == "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
    assert error.opend_reachable is True
    assert error.context_ok is True
    assert error.snapshot_ok is False


def test_futu_quote_client_classifies_unreachable_opend() -> None:
    with pytest.raises(FutuQuoteError) as exc_info:
        FutuQuoteClient(
            host="127.0.0.1",
            port=11111,
            context_factory=FakeOpenQuoteContext,
            connectivity_checker=lambda host, port: False,
        )

    error = exc_info.value
    assert error.error_type == "opend_unreachable"
    assert error.opend_reachable is False
    assert error.context_ok is False
    assert error.snapshot_ok is False
    assert "请启动或重启 Futu OpenD" in error.next_step


def test_futu_quote_client_classifies_quote_server_interruption() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeInterruptedContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_snapshots(["US.VIXY"])

    error = exc_info.value
    assert str(error) == "网络中断"
    assert error.error_type == "quote_server_interrupted"
    assert error.opend_reachable is True
    assert error.context_ok is True
    assert error.snapshot_ok is False
    assert "qot_logined=True" in error.next_step
```

Update the existing `test_futu_quote_client_fails_fast_when_opend_port_is_not_reachable` to also assert the new metadata:

```python
    assert exc_info.value.error_type == "opend_unreachable"
    assert exc_info.value.opend_reachable is False
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py -v
```

Expected: new tests fail because `FutuQuoteError` does not yet accept structured metadata.

- [ ] **Step 3: Implement structured `FutuQuoteError`**

In `src/open_trader/futu_quote.py`, replace the current `FutuQuoteError` class and add the next-step constants near the top of the file:

```python
OPEND_UNREACHABLE_NEXT_STEP = (
    "请启动或重启 Futu OpenD，确认已登录，并检查配置的 host/port 后重新运行每日盘前流程。"
)
CONTEXT_FAILED_NEXT_STEP = (
    "请确认 futu-api 可用、OpenD 已启动且登录正常，然后重新运行每日盘前流程。"
)
QUOTE_INTERRUPTED_NEXT_STEP = (
    "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
)
SNAPSHOT_FAILED_NEXT_STEP = (
    "请检查 OpenD 行情服务状态和网络连接，然后重新运行每日盘前流程。"
)


class FutuQuoteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "snapshot_failed",
        next_step: str = SNAPSHOT_FAILED_NEXT_STEP,
        opend_reachable: bool | None = None,
        context_ok: bool | None = None,
        snapshot_ok: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.next_step = next_step
        self.opend_reachable = opend_reachable
        self.context_ok = context_ok
        self.snapshot_ok = snapshot_ok
```

In `_default_context_factory`, change the missing-package error to carry metadata:

```python
        raise FutuQuoteError(
            "futu-api is not installed. Install it with: "
            ".venv/bin/python -m pip install futu-api",
            error_type="context_failed",
            next_step="请在当前虚拟环境安装 futu-api 后重新运行每日盘前流程。",
            opend_reachable=None,
            context_ok=False,
            snapshot_ok=False,
        ) from exc
```

In `FutuQuoteClient.__init__`, replace the unreachable and context-failed raises with:

```python
        if not connectivity_checker(host, port):
            raise FutuQuoteError(
                f"Futu OpenD is not reachable at {host}:{port}. "
                "Start OpenD, log in, and check the configured host and port.",
                error_type="opend_unreachable",
                next_step=OPEND_UNREACHABLE_NEXT_STEP,
                opend_reachable=False,
                context_ok=False,
                snapshot_ok=False,
            )
```

```python
        except Exception as exc:
            raise FutuQuoteError(
                f"failed to connect to Futu OpenD at {host}:{port}: {exc}",
                error_type="context_failed",
                next_step=CONTEXT_FAILED_NEXT_STEP,
                opend_reachable=True,
                context_ok=False,
                snapshot_ok=False,
            ) from exc
```

In `get_snapshots`, replace the generic non-zero return handling with:

```python
        if ret_code != 0:
            message = str(data)
            if "网络中断" in message:
                raise FutuQuoteError(
                    message,
                    error_type="quote_server_interrupted",
                    next_step=QUOTE_INTERRUPTED_NEXT_STEP,
                    opend_reachable=True,
                    context_ok=True,
                    snapshot_ok=False,
                )
            raise FutuQuoteError(
                message,
                error_type="snapshot_failed",
                next_step=SNAPSHOT_FAILED_NEXT_STEP,
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            )
```

- [ ] **Step 4: Run the focused tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py -v
```

Expected: all tests in `tests/test_futu_quote.py` pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/open_trader/futu_quote.py tests/test_futu_quote.py
git commit -m "feat: classify futu quote errors"
```

---

### Task 2: Add Pure Daily Readiness Helpers

**Files:**
- Modify: `tests/test_daily_premarket.py`
- Modify: `src/open_trader/daily_premarket.py`

- [ ] **Step 1: Add failing tests for status reason and readiness helpers**

Add these tests near the other pure helper tests in `tests/test_daily_premarket.py`:

```python
def test_derive_daily_state_marks_futu_error_as_blocked() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 0,
            "missing": 0,
            "triggered": 0,
            "items": [],
            "error": "网络中断",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 0, "watch": 1},
    )

    assert state == {
        "status": "partial",
        "readiness": "blocked",
        "status_reasons": ["futu_error"],
    }


def test_derive_daily_state_marks_missing_quote_as_review_required() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 1,
            "triggered": 0,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 1, "watch": 0},
    )

    assert state == {
        "status": "partial",
        "readiness": "review_required",
        "status_reasons": ["missing_quotes", "trade_action_review"],
    }


def test_derive_daily_state_keeps_trade_action_review_as_success_status() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 0,
            "triggered": 1,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 1, "watch": 0},
    )

    assert state == {
        "status": "success",
        "readiness": "review_required",
        "status_reasons": ["trade_action_review"],
    }


def test_derive_daily_state_marks_success_as_ready() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 0,
            "triggered": 0,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 1, "review": 0, "watch": 0},
    )

    assert state == {
        "status": "success",
        "readiness": "ready",
        "status_reasons": [],
    }
```

- [ ] **Step 2: Run the focused helper tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'derive_daily_state' -v
```

Expected: fail because `_derive_daily_state` does not exist.

- [ ] **Step 3: Implement the daily state helper**

Add this helper in `src/open_trader/daily_premarket.py` near `_notification_message`:

```python
def _derive_daily_state(
    *,
    advice_counts: dict[str, int],
    plan_counts: dict[str, int],
    futu_status: dict[str, object],
    trade_actions: dict[str, int],
    run_failed: bool = False,
    already_running: bool = False,
) -> dict[str, object]:
    reasons: list[str] = []
    if run_failed:
        reasons.append("run_failed")
    if already_running:
        reasons.append("already_running")
    if int(advice_counts.get("fallback", 0) or 0) > 0:
        reasons.append("advice_fallback")
    if int(advice_counts.get("error", 0) or 0) > 0:
        reasons.append("advice_error")
    if int(plan_counts.get("fallback", 0) or 0) > 0:
        reasons.append("plan_fallback")
    if int(plan_counts.get("error", 0) or 0) > 0:
        reasons.append("plan_error")
    if str(futu_status.get("error", "")).strip():
        reasons.append("futu_error")
    if int(futu_status.get("missing", 0) or 0) > 0:
        reasons.append("missing_quotes")
    if int(trade_actions.get("review", 0) or 0) > 0:
        reasons.append("trade_action_review")

    if run_failed:
        status = "failed"
    elif already_running:
        status = "already_running"
    elif any(reason != "trade_action_review" for reason in reasons):
        status = "partial"
    else:
        status = "success"

    if any(reason in {"run_failed", "already_running", "futu_error"} for reason in reasons):
        readiness = "blocked"
    elif reasons:
        readiness = "review_required"
    else:
        readiness = "ready"

    return {
        "status": status,
        "readiness": readiness,
        "status_reasons": reasons,
    }
```

- [ ] **Step 4: Run the focused helper tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'derive_daily_state' -v
```

Expected: all `derive_daily_state` tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: derive daily readiness state"
```

---

### Task 3: Add Futu Diagnostic Payloads to Daily Runs

**Files:**
- Modify: `tests/test_daily_premarket.py`
- Modify: `src/open_trader/daily_premarket.py`

- [ ] **Step 1: Add failing runner tests for Futu diagnostics**

Add this fake client near `UnavailableQuoteClient` in `tests/test_daily_premarket.py`:

```python
class InterruptedQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        raise FutuQuoteError(
            "网络中断",
            error_type="quote_server_interrupted",
            next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )

    def close(self) -> None:
        pass
```

Update `UnavailableQuoteClient` to raise a structured error:

```python
class UnavailableQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        raise FutuQuoteError(
            "Futu OpenD is not reachable",
            error_type="opend_unreachable",
            next_step="请启动或重启 Futu OpenD，确认已登录，并检查配置的 host/port 后重新运行每日盘前流程。",
            opend_reachable=False,
            context_ok=False,
            snapshot_ok=False,
        )
```

Add these tests near the existing Futu partial tests:

```python
def test_daily_runner_writes_futu_diagnostic_when_opend_is_unavailable(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "blocked"
    assert status["status_reasons"] == ["futu_error"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["host"] == "127.0.0.1"
    assert diagnostic["port"] == 11111
    assert diagnostic["error_type"] == "opend_unreachable"
    assert diagnostic["opend_reachable"] is False
    assert diagnostic["context_ok"] is False
    assert diagnostic["snapshot_ok"] is False
    assert "请启动或重启 Futu OpenD" in diagnostic["next_step"]


def test_daily_runner_writes_futu_diagnostic_when_snapshot_is_interrupted(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=InterruptedQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "blocked"
    assert status["status_reasons"] == ["futu_error"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["error_type"] == "quote_server_interrupted"
    assert diagnostic["opend_reachable"] is True
    assert diagnostic["context_ok"] is True
    assert diagnostic["snapshot_ok"] is False
    assert diagnostic["next_step"] == "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"


def test_daily_runner_marks_missing_quote_as_review_required(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "review_required"
    assert "missing_quotes" in status["status_reasons"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["error_type"] == "missing_quotes"
    assert diagnostic["snapshot_ok"] is True
    assert "缺失 1 个标的行情" in diagnostic["next_step"]
```

- [ ] **Step 2: Run the focused runner tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'futu_diagnostic or missing_quote_as_review_required' -v
```

Expected: fail because daily status does not yet include `readiness`, `status_reasons`, or `diagnostic`.

- [ ] **Step 3: Implement diagnostic helpers**

Add these helpers in `src/open_trader/daily_premarket.py` near `_derive_daily_state`:

```python
def _futu_diagnostic(
    *,
    host: str,
    port: int,
    error_type: str,
    message: str = "",
    next_step: str = "",
    opend_reachable: bool | None = None,
    context_ok: bool | None = None,
    snapshot_ok: bool | None = None,
) -> dict[str, object]:
    return {
        "host": host,
        "port": port,
        "opend_reachable": opend_reachable,
        "context_ok": context_ok,
        "snapshot_ok": snapshot_ok,
        "error_type": error_type,
        "message": message,
        "next_step": next_step,
    }


def _successful_futu_diagnostic(*, host: str, port: int) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="none",
        message="",
        next_step="",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=True,
    )


def _no_active_plans_diagnostic(*, host: str, port: int) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="no_active_plans",
        message="没有需要检查行情的 active trading plan。",
        next_step="",
        opend_reachable=None,
        context_ok=None,
        snapshot_ok=None,
    )


def _missing_quotes_diagnostic(
    *,
    host: str,
    port: int,
    missing: int,
) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="missing_quotes",
        message=f"缺失 {missing} 个标的行情。",
        next_step=f"请人工复核缺失的 {missing} 个标的行情，再决定是否执行相关交易动作。",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=True,
    )


def _error_futu_diagnostic(
    *,
    host: str,
    port: int,
    error: FutuQuoteError,
) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type=getattr(error, "error_type", "snapshot_failed"),
        message=str(error),
        next_step=getattr(error, "next_step", "请检查 OpenD 行情服务状态后重新运行每日盘前流程。"),
        opend_reachable=getattr(error, "opend_reachable", None),
        context_ok=getattr(error, "context_ok", None),
        snapshot_ok=getattr(error, "snapshot_ok", None),
    )
```

- [ ] **Step 4: Integrate diagnostics into `_check_futu_plan`**

In `_check_futu_plan`, update each return payload:

For no active plans:

```python
                return {
                    "checked": 0,
                    "missing": 0,
                    "triggered": 0,
                    "items": [],
                    "error": "",
                    "diagnostic": _no_active_plans_diagnostic(
                        host=self.config.futu_host,
                        port=self.config.futu_port,
                    ),
                }
```

For successful snapshot path, build `diagnostic` after the loop:

```python
            diagnostic = (
                _missing_quotes_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    missing=missing,
                )
                if missing
                else _successful_futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                )
            )
            return {
                "checked": len(active_plans),
                "missing": missing,
                "triggered": triggered,
                "items": items,
                "error": "",
                "diagnostic": diagnostic,
            }
```

For `except FutuQuoteError as exc`:

```python
        except FutuQuoteError as exc:
            return {
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": str(exc),
                "diagnostic": _error_futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    error=exc,
                ),
            }
```

In `_write_failure`, add a diagnostic to the failure payload's `futu_plan_check`:

```python
                "diagnostic": _futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    error_type="none",
                    message="",
                    next_step="",
                    opend_reachable=None,
                    context_ok=None,
                    snapshot_ok=None,
                ),
```

- [ ] **Step 5: Integrate daily state into the status payload**

In `_run_locked`, replace the current `status = (...)` block with:

```python
        daily_state = _derive_daily_state(
            advice_counts=advice_counts,
            plan_counts=plan_counts,
            futu_status=futu_status,
            trade_actions=trade_action_counts,
        )
        status = str(daily_state["status"])
```

Pass `readiness` and `status_reasons` into `_write_status_and_report` by adding parameters:

```python
            readiness=str(daily_state["readiness"]),
            status_reasons=list(daily_state["status_reasons"]),
```

Update `_write_status_and_report` signature:

```python
        readiness: str,
        status_reasons: list[str],
```

Add these fields to its `payload`:

```python
            "readiness": readiness,
            "status_reasons": status_reasons,
```

In `_write_failure`, add:

```python
        daily_state = _derive_daily_state(
            advice_counts={"ok": 0, "fallback": 0, "error": 0},
            plan_counts={"active": 0, "fallback": 0, "error": 0},
            futu_status={"checked": 0, "missing": 0, "triggered": 0, "items": [], "error": ""},
            trade_actions={"actions": 0, "ready": 0, "review": 0, "watch": 0},
            run_failed=True,
        )
```

and include in the failure payload:

```python
            "readiness": daily_state["readiness"],
            "status_reasons": daily_state["status_reasons"],
```

- [ ] **Step 6: Run the focused runner tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'futu_diagnostic or missing_quote_as_review_required or derive_daily_state' -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: write daily readiness diagnostics"
```

---

### Task 4: Render Chinese Readiness Reports and Blocker Notifications

**Files:**
- Modify: `tests/test_daily_premarket.py`
- Modify: `src/open_trader/daily_premarket.py`

- [ ] **Step 1: Add failing tests for Chinese report and notification text**

Add these assertions to `test_daily_runner_writes_futu_diagnostic_when_snapshot_is_interrupted` after reading `status`:

```python
    report = result.report_path.read_text(encoding="utf-8")
    assert "## 可用性判断" in report
    assert "- 可用性：阻塞" in report
    assert "- 原因：Futu 行情异常" in report
    assert "- 下一步：请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。" in report
```

Add this test near the blocker notification tests:

```python
def test_daily_runner_blocker_notification_uses_chinese_readiness_text(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=InterruptedQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 阻塞通知"
    ]
    assert len(blocker_calls) == 1
    _, body = blocker_calls[0]
    assert "可用性：阻塞" in body
    assert "原因：Futu 行情异常" in body
    assert "下一步：请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。" in body
    assert "futu_error" not in body
    assert "quote_server_interrupted" not in body
```

Update the existing `test_daily_runner_sends_blocker_notification_when_futu_is_unavailable` assertion:

```python
    assert "原因：Futu 行情异常" in body
    assert "请启动或重启 Futu OpenD" in body
```

Update the existing `test_daily_runner_sends_blocker_notification_when_futu_quote_is_missing` assertion:

```python
    assert "可用性：需要人工复核" in body
    assert "原因：缺失行情" in body
    assert "缺失行情：1" in body
```

- [ ] **Step 2: Run the focused rendering tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'chinese_readiness_text or snapshot_is_interrupted or blocker_notification_when_futu' -v
```

Expected: fail because report and blocker notification still use the older generic text.

- [ ] **Step 3: Add Chinese label helpers**

Add these helpers near `_daily_status_label` in `src/open_trader/daily_premarket.py`:

```python
def _readiness_label(readiness: str) -> str:
    return {
        "ready": "可复核",
        "review_required": "需要人工复核",
        "blocked": "阻塞",
    }.get(readiness.strip().lower(), readiness)


def _status_reason_label(reason: str) -> str:
    return {
        "advice_fallback": "使用历史建议",
        "advice_error": "建议生成异常",
        "plan_fallback": "交易计划使用历史建议",
        "plan_error": "交易计划异常",
        "futu_error": "Futu 行情异常",
        "missing_quotes": "缺失行情",
        "trade_action_review": "交易动作需要人工复核",
        "run_failed": "运行失败",
        "already_running": "已有任务运行中",
    }.get(reason.strip().lower(), reason)


def _reason_labels(reasons: object) -> list[str]:
    if not isinstance(reasons, list):
        return []
    return [_status_reason_label(str(reason)) for reason in reasons]


def _diagnostic_next_step(payload: dict[str, object]) -> str:
    futu = _mapping(payload.get("futu_plan_check"))
    diagnostic = _mapping(futu.get("diagnostic"))
    next_step = str(diagnostic.get("next_step", "")).strip()
    if next_step:
        return next_step
    readiness = str(payload.get("readiness", "")).strip()
    if readiness == "blocked":
        return "请先处理阻塞原因，再重新运行每日盘前流程。"
    if readiness == "review_required":
        return "请先人工复核标记项，再决定是否执行交易动作。"
    return "无需处理。"
```

- [ ] **Step 4: Render readiness in the Markdown report**

In `_render_daily_report`, after the initial metadata lines and before `"## Summary"`, insert:

```python
    readiness = str(payload.get("readiness", "")).strip()
    reason_labels = _reason_labels(payload.get("status_reasons"))
    lines.extend(
        [
            "",
            "## 可用性判断",
            "",
            f"- 可用性：{_readiness_label(readiness)}",
            f"- 原因：{', '.join(reason_labels) if reason_labels else '无'}",
            f"- 下一步：{_diagnostic_next_step(payload)}",
        ]
    )
```

- [ ] **Step 5: Render blocker notifications from readiness fields**

Change `_blocker_notification_message` signature to include optional readiness and reasons:

```python
    readiness: str = "",
    status_reasons: list[str] | None = None,
```

At the top of `_blocker_notification_message`, compute labels:

```python
    reason_labels = [_status_reason_label(reason) for reason in (status_reasons or [])]
    diagnostic = _mapping(futu_status.get("diagnostic"))
    diagnostic_next_step = str(diagnostic.get("next_step", "")).strip()
```

After the date/status line, add:

```python
        f"可用性：{_readiness_label(readiness)}",
        f"原因：{', '.join(reason_labels) if reason_labels else '未分类'}",
```

Replace the fixed generic next step with:

```python
            f"下一步：{diagnostic_next_step or '请先处理阻塞项，再重新运行每日盘前流程。'}",
```

Update both `_blocker_notification_message` call sites to pass:

```python
                        readiness=str(daily_state["readiness"]),
                        status_reasons=list(daily_state["status_reasons"]),
```

For `_write_failure`, pass:

```python
                    readiness=str(daily_state["readiness"]),
                    status_reasons=list(daily_state["status_reasons"]),
```

- [ ] **Step 6: Run the focused rendering tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'chinese_readiness_text or snapshot_is_interrupted or blocker_notification_when_futu or sends_blocker_notification_when_run_fails' -v
```

Expected: selected tests pass and notification body does not expose raw English reason enums.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: render chinese daily readiness"
```

---

### Task 5: Full Verification and Docs Review

**Files:**
- Verify: `docs/superpowers/specs/2026-06-18-daily-readiness-diagnostics-design.md`
- Verify: `README.md`
- Verify: `README.zh-CN.md`

- [ ] **Step 1: Run the focused Futu and daily test suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_daily_premarket.py -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 3: Check that user-facing notification strings are Chinese**

Run:

```bash
rg -n "Open Trader 阻塞通知|Open Trader｜阻塞通知|可用性：|原因：|下一步：|futu_error|quote_server_interrupted" src/open_trader/daily_premarket.py tests/test_daily_premarket.py
```

Expected:

- `futu_error` and `quote_server_interrupted` appear only as machine-readable enum values in status fields/tests.
- Feishu body assertions use Chinese labels such as `Futu 行情异常`, `缺失行情`, and `下一步：`.

- [ ] **Step 4: Review the diff**

Run:

```bash
git diff -- src/open_trader/futu_quote.py src/open_trader/daily_premarket.py tests/test_futu_quote.py tests/test_daily_premarket.py
```

Expected:

- No CSV schema changes.
- `daily_run_status.json` payload gains only `readiness`, `status_reasons`, and `futu_plan_check.diagnostic`.
- Notification and report text added for readiness is Chinese.

- [ ] **Step 5: Confirm README and spec do not need changes**

Run:

```bash
git diff -- README.md README.zh-CN.md docs/superpowers/specs/2026-06-18-daily-readiness-diagnostics-design.md
```

Expected: no diff. This feature is covered by status JSON, Markdown reports,
Feishu text, and tests; README updates are not required for this implementation.

- [ ] **Step 6: Report final verification**

Collect these outputs for the final response:

```bash
git log --oneline -5
git status --short
```

Expected:

- Working tree is clean.
- Recent commits include the implementation commits from Tasks 1-4.
