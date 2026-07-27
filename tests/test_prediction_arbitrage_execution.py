from __future__ import annotations

import inspect
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import PairIntent
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.polymarket_trading import AccountSnapshot, LegResult, PairSubmission


def _intent() -> PairIntent:
    return PairIntent(
        event_id="event-1",
        market_id="market-1",
        condition_id="condition-1",
        yes_token_id="yes-token",
        no_token_id="no-token",
        quantity=Decimal("10"),
        yes_max_price=Decimal("0.40"),
        no_max_price=Decimal("0.40"),
        yes_max_cost=Decimal("4.00"),
        no_max_cost=Decimal("4.00"),
        total_max_cost=Decimal("8.00"),
        minimum_profit=Decimal("2.00"),
        net_edge=Decimal("0.20"),
    )


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))


class FakeMonitor:
    def __init__(self, intent: PairIntent) -> None:
        self.intent = intent
        self.actionable = True
        self.refresh_calls = 0
        self.opportunity_calls = 0
        self.include_freshness = True
        self.include_tick = True
        self.tick_size = Decimal("0.01")

    def refresh_once(self) -> dict[str, object]:
        self.refresh_calls += 1
        return {"opportunities": [self.opportunity("opp-1")]}

    def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        self.opportunity_calls += 1
        if opportunity_id != "opp-1":
            return None
        result: dict[str, object] = {
            "opportunity_id": opportunity_id,
            "event_id": self.intent.event_id,
            "market_id": self.intent.market_id,
            "condition_id": self.intent.condition_id,
            "question": "Will it happen?",
            "volume_24h": Decimal("1000"),
            "actionable": self.actionable,
            "eligibility": "actionable" if self.actionable else "stale",
            "confirmed_age_seconds": Decimal("1"),
            "intent": self.intent,
            "quantity": self.intent.quantity,
            "yes_max_price": self.intent.yes_max_price,
            "no_max_price": self.intent.no_max_price,
            "yes_max_cost": self.intent.yes_max_cost,
            "no_max_cost": self.intent.no_max_cost,
            "total_max_cost": self.intent.total_max_cost,
            "minimum_profit": self.intent.minimum_profit,
            "estimated_profit": self.intent.minimum_profit,
            "net_edge": self.intent.net_edge,
            "yes_token_id": self.intent.yes_token_id,
            "no_token_id": self.intent.no_token_id,
        }
        if self.include_freshness:
            result["confirmed_at"] = datetime.now(UTC)
            result["confirmed_age_seconds"] = Decimal("1")
        else:
            result.pop("confirmed_age_seconds", None)
        if self.include_tick:
            result["tick_size"] = self.tick_size
        return result


class FakeTrading:
    def __init__(self, *, result: str = "both_filled") -> None:
        self.result = result
        self.preflight_calls = 0
        self.preflight_ticks: list[Decimal] = []
        self.batch_calls = 0
        self.batch_leg_names: tuple[str, ...] = ()
        self.batch_quantities: tuple[Decimal, ...] = ()
        self.merge_calls = 0
        self.reconcile_calls = 0
        self._account_reads = 0
        self._post_started = threading.Event()
        self._release_post = threading.Event()
        self.relayer_fresh = True

    def account_snapshot(self) -> AccountSnapshot:
        self._account_reads += 1
        balance = Decimal("22") if self._account_reads >= 3 else Decimal("20")
        return AccountSnapshot(
            wallet_address="0x" + "1" * 40,
            p_usd_balance=balance,
            p_usd_allowance=Decimal("20"),
            open_order_ids=(),
            positions=(),
            checked_at=datetime.now(UTC),
        )

    def geoblock_allowed(self) -> bool:
        return True

    def readiness_snapshot(self) -> dict[str, object]:
        return {
            "wallet": "ready",
            "geoblock": "allowed",
            "relayer": "ready",
            "checked_at": datetime.now(UTC)
            if self.relayer_fresh
            else datetime.now(UTC) - timedelta(seconds=61),
        }

    def no_submit_preflight(self, intent: PairIntent, *, tick_size: Decimal = Decimal("0.01")) -> dict[str, object]:
        self.preflight_calls += 1
        self.preflight_ticks.append(tick_size)
        return {"result": "PASS", "intent": intent, "tick_size": tick_size}

    def submit_pair_once(self, intent: PairIntent, *, tick_size: Decimal = Decimal("0.01")) -> PairSubmission:
        self.batch_calls += 1
        self.batch_leg_names = ("YES", "NO")
        self.batch_quantities = (intent.quantity, intent.quantity)
        self._post_started.set()
        if self.result == "delayed":
            return PairSubmission(
                yes=LegResult("YES", True, "pending", "yes-order", Decimal("0"), (), "none"),
                no=LegResult("NO", True, "pending", "no-order", Decimal("0"), (), "none"),
            )
        if self.result == "ambiguous":
            return PairSubmission(
                yes=LegResult("YES", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
                no=LegResult("NO", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
            )
        if self.result == "count_only":
            return PairSubmission(
                yes=LegResult("YES", True, "filled", "yes-order", intent.quantity, ("yes-trade",), "none"),
                no=LegResult("NO", True, "filled", "no-order", intent.quantity, ("no-trade",), "none"),
            )
        if self.result == "both_rejected":
            return PairSubmission(
                yes=LegResult("YES", False, "rejected", "", Decimal("0"), (), "rejected"),
                no=LegResult("NO", False, "rejected", "", Decimal("0"), (), "rejected"),
            )
        return PairSubmission(
            yes=LegResult("YES", True, "filled", "yes-order", intent.quantity, ("yes-trade",), "none"),
            no=LegResult("NO", True, "filled", "no-order", intent.quantity, ("no-trade",), "none"),
        )

    def reconcile(self, *, condition_id: str, since: datetime) -> dict[str, object]:
        self.reconcile_calls += 1
        if self.result == "delayed":
            return {"status": "pending", "yes_quantity": Decimal("0"), "no_quantity": Decimal("0")}
        if self.result == "ambiguous":
            return {"status": "pending", "yes_quantity": Decimal("0"), "no_quantity": Decimal("0")}
        if self.result == "count_only":
            return {"status": "ok", "trade_count": 2, "position_count": 2}
        if self.result == "pending_with_quantities":
            return {"status": "pending", "yes_quantity": Decimal("10"), "no_quantity": Decimal("10")}
        return {
            "status": "ok",
            "yes_quantity": Decimal("10"),
            "no_quantity": Decimal("10"),
        }

    def merge_once(self, *, condition_id: str, quantity: Decimal) -> dict[str, object]:
        self.merge_calls += 1
        return {"status": "confirmed", "quantity": quantity}


def execution_fixture(tmp_path: Path, *, result: str = "both_filled"):
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading(result=result)
    monitor = FakeMonitor(_intent())
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=trading,
        notifier=FakeNotifier(),
        lock_path=tmp_path / "execution.lock",
    )
    return service, trading, store, monitor


def wait_until_terminal(service: object, execution_id: str, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.execution(execution_id)  # type: ignore[attr-defined]
        if value.get("state") in {
            "both_rejected",
            "complete",
            "neutralized_incident",
            "directional_incident",
            "merge_incident",
        }:
            return value
        time.sleep(0.01)
    return service.execution(execution_id)  # type: ignore[attr-defined]


def test_one_confirm_posts_exactly_one_equal_fok_batch_and_merges(tmp_path: Path) -> None:
    service, trading, store, _ = execution_fixture(tmp_path, result="both_filled")
    preview = service.preview("opp-1")
    assert trading.batch_calls == 0
    assert trading.preflight_calls == 0

    execution = service.confirm(str(preview["id"]), "browser-request-1")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert trading.batch_calls == 1
    assert trading.batch_leg_names == ("YES", "NO")
    assert trading.batch_quantities == (Decimal("10"), Decimal("10"))
    assert trading.merge_calls == 1
    assert final["state"] == "complete"
    rows = store.histories("executions")
    assert rows[0]["state"] == "complete"


def test_preview_rechecks_without_signing_and_serializes_only_safe_intent(tmp_path: Path) -> None:
    service, trading, _, monitor = execution_fixture(tmp_path)
    preview = service.preview("opp-1")

    assert monitor.refresh_calls == 1
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0
    assert preview["expires_at"]
    assert preview["intent"]["quantity"] == "10"
    assert "PairIntent" not in repr(preview)
    assert not {"prices", "quantity", "wallet", "limits"} & set(inspect.signature(service.preview).parameters)


def test_browser_economics_are_not_service_inputs(tmp_path: Path) -> None:
    service, _, _, _ = execution_fixture(tmp_path)
    assert set(inspect.signature(service.preview).parameters) == {"opportunity_id"}
    assert set(inspect.signature(service.confirm).parameters) == {"preview_id", "idempotency_key"}


def test_both_rejected_is_terminal_without_merge_or_breaker(tmp_path: Path) -> None:
    service, trading, store, _ = execution_fixture(tmp_path, result="both_rejected")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "reject-request")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "both_rejected"
    assert trading.batch_calls == 1
    assert trading.merge_calls == 0
    assert service.preview("opp-1")["state"] == "previewed"
    legs = sqlite3.connect(store.path).execute(
        "SELECT leg_id FROM execution_legs ORDER BY leg_id"
    ).fetchall()
    assert [row[0].rsplit(":", 1)[-1] for row in legs] == ["NO", "YES"]


def test_same_idempotency_key_returns_same_execution_and_other_request_is_busy(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="delayed")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(40)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    first = service.confirm(str(preview["id"]), "same-request")
    second = service.confirm(str(preview["id"]), "same-request")
    assert second["execution_id"] == first["execution_id"]
    wait_until_terminal(service, str(first["execution_id"]))
    assert trading.batch_calls == 1


def test_worsened_price_rejects_before_external_mutation(tmp_path: Path) -> None:
    service, trading, _, monitor = execution_fixture(tmp_path)
    preview = service.preview("opp-1")
    monitor.actionable = False
    # The monitor's actionability is the server-side freshness/price gate.
    execution = service.confirm(str(preview["id"]), "worse-price")
    final = wait_until_terminal(service, str(execution["execution_id"]))
    assert final["state"] == "both_rejected"
    assert trading.batch_calls == 0


def test_ambiguous_post_is_reconciled_without_second_batch(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="ambiguous")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(40)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "ambiguous-request")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert trading.batch_calls == 1
    assert trading.reconcile_calls >= 1
    assert final["state"] == "directional_incident"
    assert service.preview("opp-1")["state"] == "locked"


def test_delayed_result_polls_and_locks_after_deadline(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="delayed")
    clock = iter(float(index) for index in range(40))
    service._clock = lambda: next(clock)  # type: ignore[attr-defined]
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "delayed-request")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert trading.batch_calls == 1
    assert trading.reconcile_calls >= 30
    assert final["state"] == "directional_incident"


def test_count_only_reconcile_never_proves_equal_fills(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="count_only")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(40)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "count-only")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.merge_calls == 0


def test_pending_quantities_never_prove_fills(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="pending_with_quantities")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(40)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "pending-quantities")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.merge_calls == 0


def test_whitespace_idempotency_returns_existing_execution(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="both_filled")
    preview = service.preview("opp-1")
    first = service.confirm(str(preview["id"]), "  normalized-request  ")
    wait_until_terminal(service, str(first["execution_id"]))
    second = service.confirm(str(preview["id"]), "normalized-request")

    assert second["execution_id"] == first["execution_id"]
    assert trading.batch_calls == 1
    assert trading.merge_calls == 1


def test_concurrent_same_idempotency_returns_same_execution(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="delayed")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(200)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")

    def confirm() -> dict[str, object]:
        return service.confirm(str(preview["id"]), "concurrent-request")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(), (0, 1)))
    assert len({response.get("execution_id") for response in responses}) == 1
    wait_until_terminal(service, str(responses[0]["execution_id"]))
    assert trading.batch_calls == 1


def test_stale_relayer_readiness_is_rejected_before_submit(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path)
    trading.relayer_fresh = False

    result = service.preview("opp-1")

    assert result["state"] == "rejected"
    assert result["reason"] == "relayer_unavailable"
    assert trading.batch_calls == 0


def test_missing_freshness_is_rejected_before_submit(tmp_path: Path) -> None:
    service, trading, _, monitor = execution_fixture(tmp_path)
    monitor.include_freshness = False

    result = service.preview("opp-1")

    assert result["state"] == "rejected"
    assert trading.batch_calls == 0


def test_missing_tick_size_is_rejected_before_submit(tmp_path: Path) -> None:
    service, trading, _, monitor = execution_fixture(tmp_path)
    monitor.include_tick = False

    result = service.preview("opp-1")

    assert result["state"] == "rejected"
    assert trading.batch_calls == 0


def test_supported_non_default_tick_is_forwarded_without_defaulting(tmp_path: Path) -> None:
    service, trading, store, monitor = execution_fixture(tmp_path)
    monitor.tick_size = Decimal("0.005")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "tick-005")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "complete"
    assert trading.preflight_ticks == [Decimal("0.005")]
    assert store.histories("executions")[0]["tick_size"] == "0.005"


def test_second_service_cannot_submit_while_file_lock_is_held(tmp_path: Path) -> None:
    first, trading, _, _ = execution_fixture(tmp_path / "first")
    second, _, _, _ = execution_fixture(tmp_path / "second")
    second._lock_path = first._lock_path  # type: ignore[attr-defined]
    first._process_lock.acquire()  # type: ignore[attr-defined]
    try:
        preview = second.preview("opp-1")
        assert preview["state"] == "busy"
        assert trading.batch_calls == 0
    finally:
        first._process_lock.release()  # type: ignore[attr-defined]
