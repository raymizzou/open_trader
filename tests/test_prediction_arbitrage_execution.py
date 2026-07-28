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
from open_trader.prediction_arbitrage_execution import PredictionExecutionService, _call
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
            "market_type": "standard_binary",
            "fee_status": "fee_free",
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
        self.reconcile_kwargs: list[dict[str, object]] = []
        self._account_reads = 0
        self._post_started = threading.Event()
        self._release_post = threading.Event()
        self.relayer_fresh = True

    def account_snapshot(self) -> AccountSnapshot:
        self._account_reads += 1
        # Fixtures perform an explicit clean startup read before preview and
        # execution; expose the post-merge balance only on the later proof read.
        balance = Decimal("22") if self._account_reads >= 4 else Decimal("20")
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
            "relayer_ready": True,
            "merge": "ready",
            "merge_ready": True,
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

    def reconcile(
        self,
        *,
        condition_id: str,
        since: datetime,
        yes_token_id: str | None = None,
        no_token_id: str | None = None,
        yes_order_id: str | None = None,
        no_order_id: str | None = None,
        yes_trade_ids: tuple[str, ...] = (),
        no_trade_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        self.reconcile_calls += 1
        self.reconcile_kwargs.append(
            {
                "condition_id": condition_id,
                "since": since,
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "yes_order_id": yes_order_id,
                "no_order_id": no_order_id,
                "yes_trade_ids": yes_trade_ids,
                "no_trade_ids": no_trade_ids,
            }
        )
        if self.result == "delayed":
            return {"status": "pending", "yes_quantity": Decimal("0"), "no_quantity": Decimal("0")}
        if self.result == "ambiguous":
            return {"status": "pending", "yes_quantity": Decimal("0"), "no_quantity": Decimal("0")}
        if self.result == "count_only":
            return {"status": "ok", "trade_count": 2, "position_count": 2}
        if self.result == "pending_with_quantities":
            return {"status": "pending", "yes_quantity": Decimal("10"), "no_quantity": Decimal("10")}
        if self.result == "forged_proof":
            return {
                "status": "ok",
                "yes_quantity": Decimal("10"),
                "no_quantity": Decimal("10"),
                "execution_proof": {
                    "verified": True,
                    "venue": "polymarket",
                    "positions_verified": True,
                    "matched_refs": {
                        "YES": {
                            "token_id": "yes-token",
                            "order_ids": ["unrelated-order"],
                            "trade_ids": ["unrelated-trade"],
                        },
                        "NO": {
                            "token_id": "no-token",
                            "order_ids": ["no-order"],
                            "trade_ids": ["no-trade"],
                        },
                    },
                },
            }
        return {
            "status": "ok",
            "yes_quantity": Decimal("10"),
            "no_quantity": Decimal("10"),
            "execution_proof": {
                "verified": True,
                "venue": "polymarket",
                "positions_verified": True,
                "position_refs": {
                    "YES": {"token_id": "yes-token", "quantity": "10"},
                    "NO": {"token_id": "no-token", "quantity": "10"},
                },
                "matched_refs": {
                    "YES": {
                        "token_id": "yes-token",
                        "order_ids": ["yes-order"],
                        "trade_ids": ["yes-trade"],
                    },
                    "NO": {
                        "token_id": "no-token",
                        "order_ids": ["no-order"],
                        "trade_ids": ["no-trade"],
                    },
                },
            },
        }

    def merge_once(self, *, condition_id: str, quantity: Decimal) -> dict[str, object]:
        self.merge_calls += 1
        if self.result == "merge_missing_ref":
            return {"status": "confirmed", "confirmed": True}
        return {
            "status": "confirmed",
            "confirmed": True,
            "quantity": quantity,
            "transaction_hash": "0xmerge-hash",
            "transaction_id": "merge-transaction",
        }


class IncidentTrading(FakeTrading):
    """Controlled venue for bounded one-leg and restart tests."""

    def __init__(self, *, result: str) -> None:
        super().__init__(result=result)
        self.remediation_calls: list[dict[str, object]] = []
        self.account_mode = "clean"
        self.cancel_calls: list[tuple[str, ...]] = []
        self._remediated = False

    def account_snapshot(self) -> AccountSnapshot:
        base = super().account_snapshot()
        positions: tuple[dict[str, str], ...] = ()
        if self.account_mode == "yes_only":
            positions = ({"condition_id": "condition-1", "token_id": "yes-token", "size": "10"},)
        elif self.account_mode == "no_only":
            positions = ({"condition_id": "condition-1", "token_id": "no-token", "size": "10"},)
        elif self.account_mode == "equal_pair":
            positions = (
                {"condition_id": "condition-1", "token_id": "yes-token", "size": "10"},
                {"condition_id": "condition-1", "token_id": "no-token", "size": "10"},
            )
        elif self.account_mode == "settled":
            positions = (
                {
                    "condition_id": "settled-condition",
                    "token_id": "settled-token",
                    "size": "10",
                    "current_value": "0",
                    "redeemable": "True",
                },
            )
        elif self.account_mode == "open_order":
            return AccountSnapshot(
                wallet_address=base.wallet_address,
                p_usd_balance=base.p_usd_balance,
                p_usd_allowance=base.p_usd_allowance,
                open_order_ids=("open-order",),
                positions=positions,
                checked_at=datetime.now(UTC),
            )
        return AccountSnapshot(
            wallet_address=base.wallet_address,
            p_usd_balance=base.p_usd_balance,
            p_usd_allowance=base.p_usd_allowance,
            open_order_ids=base.open_order_ids,
            positions=positions,
            checked_at=datetime.now(UTC),
        )

    def reconcile(self, **kwargs: object) -> dict[str, object]:
        self.reconcile_calls += 1
        yes_order = str(kwargs.get("yes_order_id") or "yes-order")
        no_order = str(kwargs.get("no_order_id") or "no-order")
        yes_trades = tuple(kwargs.get("yes_trade_ids") or ("yes-trade",))
        no_trades = tuple(kwargs.get("no_trade_ids") or ("no-trade",))
        if self._remediated or self.result == "equal_pair":
            return {
                "status": "ok",
                "yes_quantity": Decimal("10"),
                "no_quantity": Decimal("10"),
                "execution_proof": {
                    "verified": True,
                    "venue": "polymarket",
                    "positions_verified": True,
                    "position_refs": {
                        "YES": {"token_id": "yes-token", "quantity": "10"},
                        "NO": {"token_id": "no-token", "quantity": "10"},
                    },
                    "matched_refs": {
                        "YES": {"token_id": "yes-token", "order_ids": [yes_order], "trade_ids": list(yes_trades)},
                        "NO": {"token_id": "no-token", "order_ids": [no_order], "trade_ids": list(no_trades)},
                    },
                },
            }
        filled = "YES" if self.result == "yes_only" else "NO"
        return {
            "status": "ok",
            "yes_quantity": Decimal("10") if filled == "YES" else Decimal("0"),
            "no_quantity": Decimal("10") if filled == "NO" else Decimal("0"),
            "execution_proof": {
                "verified": True,
                "venue": "polymarket",
                "positions_verified": True,
                "position_refs": {
                    filled: {
                        "token_id": "yes-token" if filled == "YES" else "no-token",
                        "quantity": "10",
                    }
                },
                "matched_refs": {
                    "YES": {
                        "token_id": "yes-token",
                        "order_ids": [yes_order] if filled == "YES" else [],
                        "trade_ids": list(yes_trades) if filled == "YES" else [],
                    },
                    "NO": {
                        "token_id": "no-token",
                        "order_ids": [no_order] if filled == "NO" else [],
                        "trade_ids": list(no_trades) if filled == "NO" else [],
                    },
                },
            },
        }

    def remediation_options(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        if self.result == "yes_only":
            return {
                "fresh": True,
                "complete": {
                    "leg": "NO", "side": "BUY", "token_id": "no-token",
                    "quantity": Decimal("10"), "amount": Decimal("1.20"),
                    "max_spend": Decimal("1.20"), "max_price": Decimal("0.12"),
                    "loss": Decimal("1.20"),
                },
                "unwind": {
                    "leg": "YES", "side": "SELL", "token_id": "yes-token",
                    "shares": Decimal("10"), "quantity": Decimal("10"),
                    "min_price": Decimal("0.15"), "loss": Decimal("1.50"),
                },
            }
        if self.result == "no_only":
            return {
                "fresh": True,
                "complete": {
                    "leg": "YES", "side": "BUY", "token_id": "yes-token",
                    "quantity": Decimal("10"), "amount": Decimal("1.80"),
                    "max_spend": Decimal("1.80"), "max_price": Decimal("0.18"),
                    "loss": Decimal("1.80"),
                },
                "unwind": {
                    "leg": "NO", "side": "SELL", "token_id": "no-token",
                    "shares": Decimal("10"), "quantity": Decimal("10"),
                    "min_price": Decimal("0.09"), "loss": Decimal("0.90"),
                },
            }
        return {
            "fresh": True,
            "complete": {"loss": Decimal("2.01")},
            "unwind": {"loss": Decimal("2.01")},
        }

    def submit_remediation_once(self, order: dict[str, object]) -> LegResult:
        self.remediation_calls.append(dict(order))
        self._remediated = True
        return LegResult(
            str(order.get("leg", "NO")), True, "filled", "remediation-order",
            Decimal(str(order.get("quantity", order.get("shares", "10")))),
            ("remediation-trade",), "none",
        )

    def cancel_orders(self, order_ids: tuple[str, ...]) -> tuple[str, ...]:
        self.cancel_calls.append(order_ids)
        self.account_mode = "clean"
        return order_ids


class ChannelNotifier:
    def __init__(self, channel: str, *, fail: bool = False) -> None:
        self.channel = channel
        self.fail = fail
        self.calls = 0

    def notify(self, title: str, message: str) -> None:
        del title, message
        self.calls += 1
        if self.fail:
            raise RuntimeError("delivery failed")


class CompositeTestNotifier:
    def __init__(self, *notifiers: ChannelNotifier) -> None:
        self._notifiers = list(notifiers)


def test_new_service_starts_locked_until_startup_reconciliation(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading()
    service = PredictionExecutionService(
        store=store,
        monitor=FakeMonitor(_intent()),
        trading=trading,
        notifier=CompositeTestNotifier(ChannelNotifier("macos"), ChannelNotifier("feishu")),
        lock_path=tmp_path / "execution.lock",
    )

    result = service.preview("opp-1")

    assert result == {"state": "locked", "reason": "circuit_breaker_open"}
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0


def execution_fixture(tmp_path: Path, *, result: str = "both_filled"):
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading(result=result)
    monitor = FakeMonitor(_intent())
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=trading,
        notifier=CompositeTestNotifier(
            ChannelNotifier("macos"), ChannelNotifier("feishu")
        ),
        lock_path=tmp_path / "execution.lock",
    )
    assert service.reconcile_startup()["state"] == "ready"
    return service, trading, store, monitor


def incident_fixture(tmp_path: Path, *, result: str, notifier: object | None = None):
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = IncidentTrading(result=result)
    monitor = FakeMonitor(_intent())
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=trading,
        notifier=notifier or CompositeTestNotifier(
            ChannelNotifier("macos"), ChannelNotifier("feishu")
        ),
        lock_path=tmp_path / "execution.lock",
    )
    assert service.reconcile_startup()["state"] == "ready"
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(200)).__next__  # type: ignore[attr-defined]
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
    evidence = rows[0]["evidence"]
    reconciled = next(item for item in evidence if item.get("phase") == "reconciled")
    assert reconciled["yes_quantity"] == "10"
    assert reconciled["no_quantity"] == "10"
    assert reconciled["execution_proof"]["verified"] is True
    assert reconciled["execution_proof"]["positions_verified"] is True
    assert reconciled["execution_proof"]["matched_refs"]["YES"]["trade_ids"] == ["yes-trade"]
    merge_result = next(item for item in evidence if item.get("phase") == "merge_result")
    assert merge_result["confirmed"] is True
    assert merge_result["transaction_hash"] == "0xmerge-hash"
    assert merge_result["transaction_id"] == "merge-transaction"
    assert trading.reconcile_kwargs[0]["yes_token_id"] == "yes-token"
    assert trading.reconcile_kwargs[0]["no_token_id"] == "no-token"
    assert trading.reconcile_kwargs[0]["yes_order_id"] == "yes-order"
    assert trading.reconcile_kwargs[0]["no_order_id"] == "no-order"
    assert trading.reconcile_kwargs[0]["yes_trade_ids"] == ("yes-trade",)
    assert trading.reconcile_kwargs[0]["no_trade_ids"] == ("no-trade",)


def test_preview_rechecks_without_signing_and_serializes_only_safe_intent(tmp_path: Path) -> None:
    service, trading, _, monitor = execution_fixture(tmp_path)
    preview = service.preview("opp-1")

    assert monitor.refresh_calls == 1
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0
    assert preview["expires_at"]
    assert preview["intent"]["quantity"] == "10"
    assert preview["market_type"] == "standard_binary"
    assert preview["fee_status"] == "fee_free"
    assert preview["merge_value"] == "10"
    assert preview["available_balance"] == "20"
    assert preview["policy_limits"] == {
        "max_wallet_balance": "65.00",
        "max_normal_cost": "20.00",
        "max_emergency_loss": "2.00",
        "min_estimated_profit": "1.00",
    }
    assert "PairIntent" not in repr(preview)
    assert not {"prices", "quantity", "wallet", "limits"} & set(inspect.signature(service.preview).parameters)


def test_preview_returns_busy_when_execution_is_active(tmp_path: Path) -> None:
    service, _, store, _ = execution_fixture(tmp_path)
    preview = service.preview("opp-1")
    store.consume_preview_and_create_execution(str(preview["id"]), "active-request")

    result = service.preview("opp-1")

    assert result["state"] == "busy"
    assert result["reason"] == "active_execution"


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


def test_merge_without_transaction_reference_never_completes(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="merge_missing_ref")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "merge-no-ref")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "merge_incident"
    assert trading.merge_calls == 1
    merge_result = next(
        item
        for item in service.execution(str(execution["execution_id"]))["evidence"]
        if item.get("phase") == "merge_result"
    )
    assert merge_result["confirmed"] is False
    assert "transaction_hash" not in merge_result


def test_forged_reconcile_proof_never_authorizes_merge(tmp_path: Path) -> None:
    service, trading, _, _ = execution_fixture(tmp_path, result="forged_proof")
    service._sleep = lambda _: None  # type: ignore[attr-defined]
    service._clock = iter(float(index) for index in range(40)).__next__  # type: ignore[attr-defined]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "forged-proof")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.merge_calls == 0


def test_collaborator_kwarg_filtering_preserves_supported_legacy_kwargs() -> None:
    captured: dict[str, object] = {}

    def legacy(*, condition_id: str, since: datetime) -> dict[str, object]:
        captured.update(condition_id=condition_id, since=since)
        return {"status": "ok"}

    moment = datetime.now(UTC)
    assert _call(
        legacy,
        condition_id="condition-1",
        since=moment,
        yes_token_id="yes-token",
    ) == {"status": "ok"}
    assert captured == {"condition_id": "condition-1", "since": moment}


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


def test_yes_only_chooses_lower_loss_completion_and_keeps_breaker_open(tmp_path: Path) -> None:
    notifier = CompositeTestNotifier(ChannelNotifier("macos"), ChannelNotifier("feishu"))
    service, trading, store, _ = incident_fixture(
        tmp_path, result="yes_only", notifier=notifier
    )
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "yes-only-remediation")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "neutralized_incident"
    assert len(trading.remediation_calls) == 1
    assert trading.remediation_calls[0]["leg"] == "NO"
    assert trading.remediation_calls[0]["side"] == "BUY"
    assert trading.merge_calls == 1
    assert service.preview("opp-1")["state"] == "locked"
    incident = store.unacknowledged_incident()
    assert incident is not None
    assert incident["state"] == "neutralized_incident"


def test_no_only_chooses_lower_loss_unwind_without_merge(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="no_only")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "no-only-remediation")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "neutralized_incident"
    assert len(trading.remediation_calls) == 1
    assert trading.remediation_calls[0]["leg"] == "NO"
    assert trading.remediation_calls[0]["side"] == "SELL"
    assert trading.merge_calls == 0


def test_unwind_quantity_mismatch_is_rejected_without_order(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="no_only")

    def mismatched_options(**kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "fresh": True,
            "complete": {"loss": Decimal("2.01")},
            "unwind": {
                "leg": "NO",
                "side": "SELL",
                "token_id": "no-token",
                "shares": Decimal("9"),
                "quantity": Decimal("10"),
                "min_price": Decimal("0.09"),
                "loss": Decimal("0.90"),
            },
        }

    trading.remediation_options = mismatched_options  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "mismatched-unwind")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []
    assert trading.merge_calls == 0


def test_unwind_without_explicit_shares_is_rejected_without_order(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="no_only")

    def missing_shares_options(**kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "fresh": True,
            "complete": {"loss": Decimal("2.01")},
            "unwind": {
                "leg": "NO",
                "side": "SELL",
                "token_id": "no-token",
                "quantity": Decimal("10"),
                "min_price": Decimal("0.09"),
                "loss": Decimal("0.90"),
            },
        }

    trading.remediation_options = missing_shares_options  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "missing-unwind-shares")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []


def test_string_remediation_amounts_are_rejected_without_order(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="yes_only")

    def string_options(**kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "fresh": True,
            "complete": {
                "leg": "NO",
                "side": "BUY",
                "token_id": "no-token",
                "quantity": "10",
                "amount": "1.20",
                "max_spend": "1.20",
                "max_price": "0.12",
                "loss": "1.20",
            },
            "unwind": {"loss": Decimal("3")},
        }

    trading.remediation_options = string_options  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "string-remediation-fields")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []


def test_complete_shares_only_candidate_is_rejected_without_order(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="yes_only")

    def shares_only_options(**kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "fresh": True,
            "complete": {
                "leg": "NO",
                "side": "BUY",
                "token_id": "no-token",
                "shares": Decimal("10"),
                "amount": Decimal("1.20"),
                "max_spend": Decimal("1.20"),
                "max_price": Decimal("0.12"),
                "loss": Decimal("1.20"),
            },
            "unwind": {"loss": Decimal("3")},
        }

    trading.remediation_options = shares_only_options  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "shares-only-completion")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []


def test_completion_requires_strict_verified_reconciliation_proof(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="yes_only")
    original_reconcile = trading.reconcile

    def partial_reconcile(**kwargs: object) -> dict[str, object]:
        result = original_reconcile(**kwargs)
        if trading._remediated:
            proof = dict(result["execution_proof"])
            proof["verified"] = False
            proof["partial_verified"] = True
            result["execution_proof"] = proof
        return result

    trading.reconcile = partial_reconcile  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "partial-remediation-proof")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.merge_calls == 0


def test_completion_merge_requires_fresh_neutral_post_state(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="yes_only")
    trading.account_mode = "equal_pair"
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "stale-remediation-merge")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "merge_incident"
    assert trading.merge_calls == 1


def test_one_leg_above_two_dollars_sends_no_remediation(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "unsafe-remediation")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []
    assert trading.merge_calls == 0


def test_notification_failure_does_not_block_one_leg_risk_work(tmp_path: Path) -> None:
    notifier = CompositeTestNotifier(
        ChannelNotifier("macos", fail=True), ChannelNotifier("feishu", fail=True)
    )
    service, trading, store, _ = incident_fixture(
        tmp_path, result="yes_only", notifier=notifier
    )
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "notification-failure")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "neutralized_incident"
    assert len(trading.remediation_calls) == 1
    evidence = final["evidence"]
    attempts = next(item["attempts"] for item in evidence if item.get("phase") == "incident_open")
    assert {attempt["channel"] for attempt in attempts} == {"macos", "feishu"}
    assert store.unacknowledged_incident() is not None


def test_startup_reconciliation_cancels_known_orders_once_and_stays_locked(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="unsafe")
    trading.account_mode = "open_order"

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert trading.cancel_calls == [("open-order",)]
    assert service.preview("opp-1")["state"] == "locked"


def test_startup_confirmed_merge_requires_fresh_neutral_post_state(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="equal_pair")
    trading.account_mode = "equal_pair"
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "startup-stale-merge")

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert result["reason"] == "equal_pair"
    assert result["merge"] == "incident"
    assert result["reconciled"] is False
    incident = store.unacknowledged_incident()
    assert incident is not None
    assert incident["state"] == "merge_incident"
    assert incident["execution_id"] == execution["execution_id"]


def test_startup_merge_attempt_evidence_blocks_duplicate_merge_after_restart(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="equal_pair")
    trading.account_mode = "equal_pair"
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "startup-merge-attempt")
    store.transition_execution(
        str(execution["execution_id"]),
        state="merging",
        evidence={
            "phase": "startup_merge_attempt",
            "idempotency_key": f"startup-merge:{execution['execution_id']}:10",
            "condition_id": "condition-1",
            "quantity": "10",
        },
    )

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert result["reason"] == "equal_pair"
    assert result["merge"] == "pending"
    assert result["reconciled"] is False
    assert result["merge_reason"] == "merge_attempt_in_flight"
    assert trading.merge_calls == 0


def test_startup_confirmed_merge_evidence_reconciles_clean_execution(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "startup-confirmed-merge")
    store.transition_execution(
        str(execution["execution_id"]),
        state="merging",
        evidence={
            "phase": "merge_result",
            "status": "confirmed",
            "confirmed": True,
            "transaction_hash": "0xconfirmed-startup",
        },
    )

    result = service.reconcile_startup()

    assert result["state"] == "ready"
    assert result["readiness"] == "reconciled"
    assert trading.merge_calls == 0
    assert service.execution(str(execution["execution_id"]))["state"] == "complete"
    assert store.unacknowledged_incident() is None


def test_startup_remediation_merge_attempt_blocks_duplicate_merge(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="yes_only")
    trading.account_mode = "equal_pair"
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "remediation-merge-attempt")
    store.transition_execution(
        str(execution["execution_id"]),
        state="merging",
        evidence={
            "phase": "remediation_merge_attempt",
            "idempotency_key": f"remediation-merge:{execution['execution_id']}:10",
            "condition_id": "condition-1",
            "quantity": "10",
        },
    )

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert result["merge"] == "pending"
    assert result["merge_reason"] == "merge_attempt_in_flight"
    assert trading.merge_calls == 0


def test_clean_startup_becomes_ready_only_after_fresh_reconciliation(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="unsafe")

    result = service.reconcile_startup()

    assert result["state"] == "ready"
    assert result["readiness"] == "fresh"
    assert service.preview("opp-1")["state"] == "previewed"
    assert trading.batch_calls == 0


def test_settled_zero_value_positions_do_not_lock_startup(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="unsafe")
    trading.account_mode = "settled"

    result = service.reconcile_startup()

    assert result["state"] == "ready"
    assert result["readiness"] == "fresh"


def test_reset_breaker_denies_directional_imbalance_without_orders(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    # Seed a durable execution/incident without a venue mutation.
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "reset-seed")
    incident_id = store.open_incident(str(execution["execution_id"]), {"state": "directional_incident"})
    service._breaker_open = True  # type: ignore[attr-defined]
    trading.account_mode = "yes_only"

    result = service.reset_breaker(incident_id)

    assert result["state"] == "locked"
    assert result["reason"] == "directional_imbalance"
    assert service.preview("opp-1")["state"] == "locked"
    assert trading.batch_calls == 0


def test_reset_breaker_requires_fresh_clean_account_and_acknowledges_incident(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "reset-clean")
    incident_id = store.open_incident(str(execution["execution_id"]), {"state": "directional_incident"})

    result = service.reset_breaker(incident_id)

    assert result["state"] == "ready"
    assert result["reason"] == "reset_confirmed"
    assert store.unacknowledged_incident() is None
    assert service.preview("opp-1")["state"] == "previewed"
    assert trading.batch_calls == 0


def test_startup_incident_without_local_execution_is_durable_and_resettable(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    trading.account_mode = "open_order"

    locked = service.reconcile_startup()

    assert locked["state"] == "locked"
    incidents = store.histories("incidents")
    assert len(incidents) == 1
    incident_id = str(incidents[0]["incident_id"])
    trading.account_mode = "clean"
    reset = service.reset_breaker(incident_id)
    assert reset["state"] == "ready"


def test_startup_does_not_unlock_with_existing_unacknowledged_terminal_incident(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "terminal-incident")
    store.transition_execution(
        str(execution["execution_id"]),
        state="merge_incident",
        evidence={"phase": "merge_result", "confirmed": False},
    )
    store.open_incident(str(execution["execution_id"]), {"state": "merge_incident"})

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert result["reason"] == "unacknowledged_incident"
    assert trading.batch_calls == 0


def test_reset_denies_terminal_unconfirmed_merge_from_incident_evidence(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "terminal-merge")
    store.transition_execution(
        str(execution["execution_id"]),
        state="merge_incident",
        evidence={"phase": "merge_result", "confirmed": False},
    )
    incident_id = store.open_incident(
        str(execution["execution_id"]), {"state": "merge_incident"}
    )

    result = service.reset_breaker(incident_id)

    assert result["state"] == "locked"
    assert result["reason"] == "pending_merge"
    assert trading.batch_calls == 0


def test_missing_feishu_or_macos_blocks_preview_readiness(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = IncidentTrading(result="unsafe")
    monitor = FakeMonitor(_intent())
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=trading,
        notifier=ChannelNotifier("macos"),
        lock_path=tmp_path / "execution.lock",
    )
    # This test targets notifier gating itself; bypass the constructor lock
    # without implying that production can skip startup reconciliation.
    service._breaker_open = False  # type: ignore[attr-defined]

    result = service.preview("opp-1")

    assert result["state"] == "rejected"
    assert result["reason"] == "notification_config_unavailable"
    assert trading.batch_calls == 0


def test_stale_account_snapshot_blocks_reset(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "stale-reset")
    incident_id = store.open_incident(str(execution["execution_id"]), {"state": "directional_incident"})
    original = trading.account_snapshot

    def stale_snapshot() -> AccountSnapshot:
        value = original()
        return AccountSnapshot(
            wallet_address=value.wallet_address,
            p_usd_balance=value.p_usd_balance,
            p_usd_allowance=value.p_usd_allowance,
            open_order_ids=value.open_order_ids,
            positions=value.positions,
            checked_at=datetime.now(UTC) - timedelta(seconds=61),
        )

    trading.account_snapshot = stale_snapshot  # type: ignore[method-assign]
    result = service.reset_breaker(incident_id)

    assert result["state"] == "locked"
    assert result["reason"] == "account_stale"


def test_malformed_account_collections_block_reset(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "malformed-reset")
    incident_id = store.open_incident(str(execution["execution_id"]), {"state": "directional_incident"})
    trading.account_snapshot = lambda: {  # type: ignore[method-assign]
        "wallet_address": "0x" + "1" * 40,
        "p_usd_balance": Decimal("20"),
        "p_usd_allowance": Decimal("20"),
        "open_order_ids": "not-a-collection",
        "positions": "not-a-collection",
        "checked_at": datetime.now(UTC),
    }

    result = service.reset_breaker(incident_id)

    assert result["state"] == "locked"
    assert result["reason"] == "account_malformed"
    assert store.unacknowledged_incident() is not None


def test_remediation_rejects_wrong_token_or_quantity_candidate(tmp_path: Path) -> None:
    service, trading, _, _ = incident_fixture(tmp_path, result="yes_only")

    def wrong_options(**kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "fresh": True,
            "complete": {
                "leg": "NO", "side": "BUY", "token_id": "yes-token",
                "quantity": Decimal("9"), "amount": Decimal("1.08"),
                "max_spend": Decimal("1.08"), "max_price": Decimal("0.12"),
                "loss": Decimal("1.08"),
            },
            "unwind": {"loss": Decimal("3")},
        }

    trading.remediation_options = wrong_options  # type: ignore[method-assign]
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "wrong-remedy")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "directional_incident"
    assert trading.remediation_calls == []


def test_one_leg_neutralization_does_not_mark_first_live_order_validated(tmp_path: Path) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="yes_only")
    preview = service.preview("opp-1")
    execution = service.confirm(str(preview["id"]), "one-leg-runtime")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "neutralized_incident"
    runtime = store.load_runtime() or {}
    assert runtime.get("first_live_order") != "validated"
