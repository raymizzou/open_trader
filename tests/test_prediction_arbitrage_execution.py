from __future__ import annotations

import inspect
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import PairIntent, ThresholdHedgeIntent, ThresholdHedgeLeg
from open_trader.prediction_arbitrage_execution import PredictionExecutionService, _call
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_title_translation import prediction_title_cache_key
from open_trader.predict_cross_venue import CrossVenueIntent, CrossVenueLeg
from open_trader.polymarket_trading import (
    AccountSnapshot,
    LegResult,
    PairSubmission,
    ThresholdHedgeSubmission,
    ThresholdLegResult,
)


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


def _threshold_intent() -> ThresholdHedgeIntent:
    return ThresholdHedgeIntent(
        relation_id="relation-1",
        event_id="event-threshold",
        relation="B_IMPLIES_A",
        leg_a=ThresholdHedgeLeg(
            label="A", condition_id="condition-a", market_id="market-a",
            outcome="YES", token_id="a-token", quantity=Decimal("10"),
            max_price=Decimal("0.10"), max_cost=Decimal("1.00"), tick_size=Decimal("0.01"),
        ),
        leg_b=ThresholdHedgeLeg(
            label="B", condition_id="condition-b", market_id="market-b",
            outcome="NO", token_id="b-token", quantity=Decimal("10"),
            max_price=Decimal("0.11"), max_cost=Decimal("1.10"), tick_size=Decimal("0.01"),
        ),
        quantity=Decimal("10"), maximum_fee=Decimal("0.02"),
        total_max_cost=Decimal("2.12"), minimum_payout=Decimal("10"),
        minimum_profit=Decimal("7.88"), net_edge=Decimal("0.788"),
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


class ThresholdMonitor(FakeMonitor):
    def __init__(self, intent: ThresholdHedgeIntent) -> None:
        super().__init__(_intent())
        self.threshold_intent = intent
        self.rules_hash_a = "hash-a"
        self.rules_hash_b = "hash-b"
        self.cache_key = "cache-key"
        self.rules_verified = True
        self.codex_status = "approved"
        self.book_age_seconds = Decimal("1")
        self.remediation_safe = True
        self.annualized_yield: object = Decimal("0.20")

    def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        if opportunity_id != "threshold-opp-1":
            return None
        intent = self.threshold_intent
        now = datetime.now(UTC)
        confirmed_at = now - timedelta(seconds=float(self.book_age_seconds))
        rules_verified_at = now if self.rules_verified else None
        return {
            "opportunity_id": opportunity_id,
            "intent_type": "threshold_hedge",
            "event_id": intent.event_id,
            "event_title": "Fed cuts",
            "event_slug": "fed-cuts-2026",
            "market_id": intent.relation_id,
            "market_type": "threshold_hedge",
            "relation_id": intent.relation_id,
            "relation": intent.relation,
            "condition_id_a": intent.leg_a.condition_id,
            "condition_id_b": intent.leg_b.condition_id,
            "question_a": "Will BTC be above 90k?",
            "question_b": "Will BTC be above 100k?",
            "rules_hash_a": self.rules_hash_a,
            "rules_hash_b": self.rules_hash_b,
            "cache_key": self.cache_key,
            "relation_validation": {"status": self.codex_status},
            "llm_status": self.codex_status,
            "llm_decision": "APPROVE" if self.codex_status == "approved" else "REJECT",
            "llm_summary": "The higher threshold implies the lower threshold.",
            "rules_verified_at": rules_verified_at,
            "rules_fingerprint": self.cache_key,
            "actionable": self.codex_status == "approved" and self.remediation_safe,
            "eligibility_reason": "actionable" if self.codex_status == "approved" and self.remediation_safe else "not_ready",
            "confirmed_at": confirmed_at,
            "confirmed_age_seconds": self.book_age_seconds,
            "book_timestamp_a": confirmed_at,
            "book_timestamp_b": confirmed_at,
            "book_received_at_a": now,
            "book_received_at_b": now,
            "intent": intent,
            "tick_size": Decimal("0.01"),
            "leg_a": {
                "question": "Will BTC be above 90k?",
                "outcome": intent.leg_a.outcome,
                "quantity": intent.leg_a.quantity,
                "max_price": intent.leg_a.max_price,
                "max_cost": intent.leg_a.max_cost,
            },
            "leg_b": {
                "question": "Will BTC be above 100k?",
                "outcome": intent.leg_b.outcome,
                "quantity": intent.leg_b.quantity,
                "max_price": intent.leg_b.max_price,
                "max_cost": intent.leg_b.max_cost,
            },
            "planned_amount": intent.total_max_cost,
            "maximum_fee": intent.maximum_fee,
            "total_max_cost": intent.total_max_cost,
            "minimum_payout": intent.minimum_payout,
            "minimum_profit": intent.minimum_profit,
            "estimated_profit": intent.minimum_profit,
            "net_edge": intent.net_edge,
            "annualized_yield": self.annualized_yield,
        }


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
        self.account_fresh = True
        self.balance = None
        self.allowance = None
        self.geoblock = True

    def account_snapshot(self) -> AccountSnapshot:
        self._account_reads += 1
        # Fixtures perform an explicit clean startup read before preview and
        # execution; expose the post-merge balance only on the later proof read.
        balance = self.balance if self.balance is not None else (Decimal("22") if self._account_reads >= 4 else Decimal("20"))
        allowance = self.allowance if self.allowance is not None else Decimal("20")
        checked_at = datetime.now(UTC) if self.account_fresh else datetime.now(UTC) - timedelta(seconds=61)
        return AccountSnapshot(
            wallet_address="0x" + "1" * 40,
            p_usd_balance=balance,
            p_usd_allowance=allowance,
            open_order_ids=(),
            positions=(),
            checked_at=checked_at,
        )

    def geoblock_allowed(self) -> bool:
        return self.geoblock

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


class ThresholdTrading(FakeTrading):
    def __init__(self, *, result: str = "both_filled") -> None:
        super().__init__(result=result)
        self.holding_positions: tuple[dict[str, str], ...] = ()
        self.threshold_preflight_calls = 0
        self.threshold_submit_calls = 0
        self.threshold_reconcile_calls = 0
        self.threshold_reconcile_kwargs: list[dict[str, object]] = []

    def no_submit_threshold_preflight(
        self, intent: ThresholdHedgeIntent
    ) -> dict[str, object]:
        self.threshold_preflight_calls += 1
        return getattr(self, "threshold_preflight_result", {"result": "PASS", "intent": intent})

    def account_snapshot(self) -> AccountSnapshot:
        base = super().account_snapshot()
        return AccountSnapshot(
            wallet_address=base.wallet_address,
            p_usd_balance=base.p_usd_balance,
            p_usd_allowance=base.p_usd_allowance,
            open_order_ids=base.open_order_ids,
            positions=self.holding_positions,
            checked_at=base.checked_at,
        )

    def submit_threshold_hedge_once(
        self, intent: ThresholdHedgeIntent
    ) -> ThresholdHedgeSubmission:
        self.threshold_submit_calls += 1
        return ThresholdHedgeSubmission(
            leg_a=ThresholdLegResult(
                "A", intent.leg_a.outcome, intent.leg_a.condition_id,
                intent.leg_a.token_id, True, "filled", "a-order", intent.quantity,
                ("a-trade",), "none",
            ),
            leg_b=ThresholdLegResult(
                "B", intent.leg_b.outcome, intent.leg_b.condition_id,
                intent.leg_b.token_id, True, "filled", "b-order", intent.quantity,
                ("b-trade",), "none",
            ),
        )

    def reconcile_threshold_hedge(
        self, *, intent: ThresholdHedgeIntent, since: datetime,
        leg_a: ThresholdLegResult, leg_b: ThresholdLegResult,
    ) -> dict[str, object]:
        self.threshold_reconcile_calls += 1
        self.threshold_reconcile_kwargs.append({
            "intent": intent, "since": since, "leg_a": leg_a, "leg_b": leg_b,
        })
        return {
            "status": "ok",
            "leg_a_quantity": intent.quantity,
            "leg_b_quantity": intent.quantity,
            "execution_proof": {
                "verified": True,
                "venue": "polymarket",
                "positions_verified": True,
                "matched_refs": {
                    "A": {"token_id": intent.leg_a.token_id, "order_ids": ["a-order"], "trade_ids": ["a-trade"]},
                    "B": {"token_id": intent.leg_b.token_id, "order_ids": ["b-order"], "trade_ids": ["b-trade"]},
                },
                "position_refs": {
                    "A": {"token_id": intent.leg_a.token_id, "quantity": "10"},
                    "B": {"token_id": intent.leg_b.token_id, "quantity": "10"},
                },
            },
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
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
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


def test_preview_refreshes_only_the_selected_opportunity(tmp_path: Path) -> None:
    class TargetedMonitor(FakeMonitor):
        def __init__(self) -> None:
            super().__init__(_intent())
            self.targeted_calls = 0
            self.full_refresh_calls = 0

        def refresh_opportunity(self, opportunity_id: str) -> dict[str, object] | None:
            self.targeted_calls += 1
            return self.opportunity(opportunity_id)

        def refresh_once(self) -> dict[str, object]:
            self.full_refresh_calls += 1
            return super().refresh_once()

    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading()
    monitor = TargetedMonitor()
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

    preview = service.preview("opp-1")

    assert preview["state"] == "previewed"
    assert monitor.targeted_calls == 1
    assert monitor.full_refresh_calls == 0


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_preview_fails_closed_when_targeted_refresh_fails(
    tmp_path: Path, failure: str,
) -> None:
    class FailingTargetedMonitor(FakeMonitor):
        def __init__(self) -> None:
            super().__init__(_intent())
            self.snapshot_calls = 0

        def refresh_opportunity(
            self, opportunity_id: str,
        ) -> dict[str, object] | None:
            assert opportunity_id == "opp-1"
            if failure == "exception":
                raise ConnectionError("sentinel refresh failure")
            return None

        def snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {"opportunities": [self.opportunity("opp-1")]}

    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading()
    monitor = FailingTargetedMonitor()
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

    result = service.preview("opp-1")

    assert result == {"state": "rejected", "reason": "opportunity_unavailable"}
    assert monitor.snapshot_calls == 0
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0


def threshold_execution_fixture(tmp_path: Path):
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = ThresholdTrading()
    monitor = ThresholdMonitor(_threshold_intent())
    macos = ChannelNotifier("macos")
    feishu = ChannelNotifier("feishu")
    notifier = CompositeTestNotifier(macos, feishu)
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=trading,
        notifier=notifier,
        lock_path=tmp_path / "execution.lock",
    )
    assert service.reconcile_startup()["state"] == "ready"
    service.test_notifiers = (macos, feishu)  # type: ignore[attr-defined]
    return service, trading, store, monitor


def _notification_signal(store: PredictionArbitrageStore) -> str:
    return store.upsert_signal(
        {
            "market_id": "relation-1",
            "event_id": "event-threshold",
            "question": "Fed cuts",
            "started_at": datetime.now(UTC).isoformat(),
            "first_positive_at": datetime.now(UTC).isoformat(),
            "net_edge": Decimal("0.788"),
            "estimated_profit": Decimal("7.88"),
            "notification_state": "pending",
            "notification_attempts": 0,
        }
    )


def _standard_notification_signal(
    store: PredictionArbitrageStore, *, market_id: str = "market-1"
) -> str:
    now = datetime.now(UTC).isoformat()
    return store.upsert_signal(
        {
            "opportunity_id": f"event-standard:{market_id}",
            "market_id": market_id,
            "event_id": "event-standard",
            "event_title": "Will it happen?",
            "question": "Will it happen?",
            "market_type": "standard_binary",
            "started_at": now,
            "first_positive_at": now,
            "yes_max_price": Decimal("0.40"),
            "no_max_price": Decimal("0.40"),
            "quantity": Decimal("10"),
            "total_max_cost": Decimal("8.00"),
            "estimated_profit": Decimal("2.00"),
            "profit": Decimal("2.00"),
            "notification_state": "pending",
            "notification_attempts": 0,
        }
    )


def _cross_venue_notification_signal(store: PredictionArbitrageStore) -> str:
    now = datetime.now(UTC).isoformat()
    return store.upsert_signal(
        {
            "opportunity_id": "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
            "market_id": "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
            "event_id": "public-pair",
            "market_type": "cross_venue_yes_no",
            "execution_mode": "observe_only",
            "clear_signal": True,
            "started_at": now,
            "first_positive_at": now,
            "total_max_cost": Decimal("9.45"),
            "minimum_profit": Decimal("0.55"),
            "estimated_profit": Decimal("0.55"),
            "legs": [
                {
                    "exchange": "predict.fun",
                    "outcome": "YES",
                    "quantity": Decimal("10"),
                    "max_cost": Decimal("4.10"),
                    "settlement_asset": "USDT",
                    "token_id": "public-predict-yes",
                },
                {
                    "exchange": "polymarket",
                    "outcome": "NO",
                    "quantity": Decimal("10"),
                    "max_cost": Decimal("5.10"),
                    "settlement_asset": "USDC",
                    "token_id": "public-poly-no",
                },
            ],
        }
    )


def standard_notification_fixture(tmp_path: Path):
    service, trading, store, monitor = execution_fixture(tmp_path)
    macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    return service, trading, store, monitor, macos, feishu


def test_notify_monitor_failure_uses_feishu_only_and_operator_copy(
    tmp_path: Path,
) -> None:
    service, _trading, _store, _monitor, macos, feishu = (
        standard_notification_fixture(tmp_path)
    )

    result = service.notify_monitor_failure(
        {
            "attempts": 5,
            "error_type": "TransportError",
            "last_success_at": "2026-08-01T12:00:00+00:00",
        }
    )

    assert result == {"state": "sent"}
    assert macos.calls == 0
    assert feishu.calls == 1
    title, message = feishu.messages[-1]
    assert title == "预测市场监控需要人工干预"
    assert "连续 5 次刷新失败" in message
    assert "自动重试已停止" in message
    assert "TransportError" in message
    assert "2026-08-01T12:00:00+00:00" in message
    assert "Dashboard：http://127.0.0.1:8766/" in message
    assert "重启承载预测监控的 Dashboard 服务" in message
    assert "Polymarket 连接" in message


def test_notify_monitor_failure_sanitizes_error_and_reports_delivery_failure(
    tmp_path: Path,
) -> None:
    service, _trading, _store, _monitor, _macos, feishu = (
        standard_notification_fixture(tmp_path)
    )
    feishu.fail = True

    result = service.notify_monitor_failure(
        {
            "attempts": 5,
            "error_type": "TransportError: secret-token",
            "last_success_at": None,
        }
    )

    assert result == {"state": "failed", "reason": "notification_failed"}
    assert feishu.calls == 1
    assert "unknown_error" in feishu.messages[-1][1]
    assert "secret-token" not in feishu.messages[-1][1]
    assert "从未成功" in feishu.messages[-1][1]


def test_notify_ready_opportunity_standard_sends_feishu_observation_without_preflight(
    tmp_path: Path,
) -> None:
    service, trading, store, _monitor, macos, feishu = standard_notification_fixture(tmp_path)
    signal_id = _standard_notification_signal(store)
    service._prepare_opportunity = lambda *_args: pytest.fail(  # type: ignore[method-assign]
        "standard observation must not prepare an order"
    )

    result = service.notify_ready_opportunity("opp-1", signal_id)

    assert result == {"state": "sent", "signal_id": signal_id}
    assert feishu.calls == 1
    assert macos.calls == 0
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0
    assert store.active_execution() is None
    assert store.signal(signal_id)["notification_state"] == "sent"  # type: ignore[index]


def test_notify_ready_opportunity_cross_venue_sends_without_prepare_or_trading(
    tmp_path: Path,
) -> None:
    service, trading, store, _monitor, macos, feishu = standard_notification_fixture(tmp_path)
    signal_id = _cross_venue_notification_signal(store)
    service._prepare_opportunity = lambda *_args: pytest.fail(  # type: ignore[method-assign]
        "cross observation must not prepare an order"
    )

    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result == {"state": "sent", "signal_id": signal_id}
    assert feishu.calls == 1
    assert macos.calls == 0
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0
    assert store.active_execution() is None


def _cross_intent(*, predict_price: Decimal = Decimal("0.45")) -> CrossVenueIntent:
    now = datetime.now(UTC)
    return CrossVenueIntent(
        pair_id="public-pair",
        direction="PREDICT_YES_POLYMARKET_NO",
        legs=(
            CrossVenueLeg("predict.fun", "predict-market", "predict-condition", "YES", "predict-yes", "USDT", Decimal("5"), Decimal("5"), predict_price, Decimal("2.30"), Decimal("0.05"), "USDT", now, None),
            CrossVenueLeg("polymarket", "poly-market", "poly-condition", "NO", "poly-no", "pUSD", Decimal("5"), Decimal("5"), Decimal("0.47"), Decimal("2.40"), Decimal("0.05"), "pUSD", now, now + timedelta(days=30)),
        ),
        quantity=Decimal("5"), calculable_gas=Decimal("0"), total_max_cost=Decimal("4.70"),
        maximum_fee=Decimal("0.10"), minimum_payout=Decimal("5"), minimum_profit=Decimal("0.30"),
        annualized_yield=Decimal("0.16"), canonical_cutoff=now + timedelta(days=30),
        resolution_at=now + timedelta(days=30), actionable=True, quote_available=True,
    )


class CrossVenueMonitor:
    def __init__(self, intent: CrossVenueIntent) -> None:
        self.intent = intent
        self.overrides: dict[str, object] = {}

    def snapshot(self) -> dict[str, object]:
        now = datetime.now(UTC)
        opportunity: dict[str, object] = {
            "opportunity_id": f"cross:{self.intent.pair_id}:{self.intent.direction}",
            "market_type": "cross_venue_yes_no", "funnel_stage": 5,
            "actionable": True, "clear_signal": True, "intent": self.intent,
            "confirmed_at": now, "confirmed_age_seconds": Decimal("1"),
            "canonical_cutoff": self.intent.canonical_cutoff,
            "codex_approval": {"decision": "APPROVE", "cache_key": "cross-cache", "direct_outcome_mapping": {"predict_yes": "YES", "predict_no": "NO", "polymarket_yes": "YES", "polymarket_no": "NO"}, "evidence": [{"exchange": "predict.fun", "quote": "same rules"}, {"exchange": "polymarket", "quote": "same rules"}]},
            "rules_fingerprints": {"predict.fun": "predict-fingerprint", "polymarket": "poly-fingerprint"},
        }
        opportunity.update(self.overrides)
        return {"opportunities": [opportunity]}


class CrossPredictTrading:
    def __init__(self) -> None:
        self.account_calls = 0
        self.allowance_ready = True

    def account_snapshot(self) -> dict[str, object]:
        self.account_calls += 1
        return {"wallet_address": "0xpredict", "available_usdt": "5", "allowance_ready": self.allowance_ready, "open_orders": (), "positions": (), "checked_at": datetime.now(UTC)}


def _cross_service(tmp_path: Path) -> tuple[PredictionExecutionService, PredictionArbitrageStore, FakeTrading, CrossVenueMonitor, CrossPredictTrading]:
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = FakeTrading()
    cross = CrossVenueMonitor(_cross_intent())
    predict = CrossPredictTrading()
    service = PredictionExecutionService(
        store=store, monitor=FakeMonitor(_intent()), trading=trading, predict_trading=predict,
        notifier=CompositeTestNotifier(ChannelNotifier("macos"), ChannelNotifier("feishu")),
        lock_path=tmp_path / "execution.lock",
    )
    service.set_cross_venue_monitor(cross)
    assert service.reconcile_startup()["state"] == "ready"
    return service, store, trading, cross, predict


def test_cross_venue_stage_five_preview_is_server_owned(tmp_path: Path) -> None:
    service, _store, _trading, _cross, _predict = _cross_service(tmp_path)

    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert preview["market_type"] == "cross_venue_yes_no"
    assert [leg["exchange"] for leg in preview["buy_legs"]] == ["predict.fun", "polymarket"]
    assert preview["net_quantity"] == "5"
    assert preview["maximum_total_cost"] == "4.70"
    assert preview["minimum_payout"] == "5"
    assert preview["minimum_profit"] == "0.30"
    assert preview["annualized_yield"] >= "0.15"
    assert preview["canonical_cutoff"].endswith("Z")
    assert preview["codex_approval"]["decision"] == "APPROVE"
    assert preview["balances"]["predict.fun"]["asset"] == "USDT"
    assert preview["balances"]["polymarket"]["asset"] == "pUSD"
    assert preview["unsettled"]["limit"] == "100"
    assert preview["policy_limits"]["max_normal_cost"] == "20"
    assert preview["policy_limits"]["max_emergency_loss"] == "2"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda cross, _trading, _predict: cross.overrides.update({"confirmed_age_seconds": Decimal("11")}), "books_stale"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, legs=(replace(cross.intent.legs[0], book_timestamp=datetime.now(UTC) - timedelta(seconds=11)), cross.intent.legs[1]))), "books_stale"),
        (lambda cross, _trading, _predict: cross.overrides.update({"codex_approval": {"decision": "REJECT"}}), "codex_not_approved"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, canonical_cutoff=None)), "canonical_cutoff_invalid"),
        (lambda _cross, trading, _predict: setattr(trading, "balance", Decimal("1")), "account_insufficient"),
        (lambda _cross, _trading, predict: setattr(predict, "allowance_ready", False), "account_insufficient"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, quote_available=False)), "opportunity_not_actionable"),
    ],
)
def test_cross_venue_preview_fails_closed_on_current_admission_changes(
    tmp_path: Path, change: object, reason: str
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    change(cross, trading, predict)  # type: ignore[operator]

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "rejected", "reason": reason
    }


def test_cross_venue_confirmation_rechecks_fingerprint_before_no_submit_release(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, _predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    cross.overrides["rules_fingerprints"] = {
        "predict.fun": "changed", "polymarket": "poly-fingerprint"
    }

    accepted = service.confirm(str(preview["preview_id"]), "cross-fingerprint-change")
    assert accepted["execution_id"] == preview["execution_id"]
    service._threads[str(accepted["execution_id"])].join(timeout=5)
    execution = service.execution(str(accepted["execution_id"]))

    assert execution["state"] == "both_rejected"
    assert execution["evidence"][-1]["reason"] == "opportunity_changed"
    assert store.cross_unsettled_principal() == Decimal("0")
    assert trading.preflight_calls == 0
    assert trading.batch_calls == 0


def test_cross_venue_preview_respects_cross_only_breaker(tmp_path: Path) -> None:
    service, _store, _trading, _cross, _predict = _cross_service(tmp_path)
    service._cross_breaker_open = True

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "locked", "reason": "cross_circuit_breaker_open"
    }


def test_standard_notification_uses_only_cached_title_translation(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor, _macos, feishu = standard_notification_fixture(tmp_path)
    store.save_llm_cache(
        prediction_title_cache_key("Will it happen?"),
        {"title_zh": "会发生吗？"},
    )
    signal_id = _standard_notification_signal(store)

    assert service.notify_ready_opportunity("opp-1", signal_id)["state"] == "sent"
    assert "会发生吗？" in feishu.messages[-1][1]


def test_notify_ready_opportunity_standard_suppresses_same_market_within_cooldown(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor, _macos, feishu = standard_notification_fixture(tmp_path)
    first_signal = _standard_notification_signal(store)
    assert service.notify_ready_opportunity("opp-1", first_signal)["state"] == "sent"
    store.close_signal(
        "market-1", ended_at=datetime.now(UTC).isoformat(), reason="data_unavailable"
    )
    second_signal = _standard_notification_signal(store)

    result = service.notify_ready_opportunity("opp-1", second_signal)

    assert result == {"state": "ignored", "reason": "market_cooldown"}
    assert feishu.calls == 1
    current = store.signal(second_signal)
    assert current["notification_state"] == "suppressed"  # type: ignore[index]
    assert current["notification_suppressed_reason"] == "market_cooldown"  # type: ignore[index]


def test_notify_ready_opportunity_standard_cooldown_ignores_failed_delivery(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor, _macos, feishu = standard_notification_fixture(tmp_path)
    feishu.fail = True
    first_signal = _standard_notification_signal(store)
    assert service.notify_ready_opportunity("opp-1", first_signal)["state"] == "failed"
    store.close_signal(
        "market-1", ended_at=datetime.now(UTC).isoformat(), reason="data_unavailable"
    )
    feishu.fail = False
    second_signal = _standard_notification_signal(store)

    result = service.notify_ready_opportunity("opp-1", second_signal)

    assert result == {"state": "sent", "signal_id": second_signal}
    assert feishu.calls == 2


def test_notify_ready_opportunity_standard_retries_three_times_and_stops_when_closed(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor, _macos, feishu = standard_notification_fixture(tmp_path)
    feishu.fail = True
    signal_id = _standard_notification_signal(store)

    for _ in range(3):
        assert service.notify_ready_opportunity("opp-1", signal_id)["state"] == "failed"
    assert service.notify_ready_opportunity("opp-1", signal_id) == {
        "state": "ignored",
        "reason": "notification_attempts_exhausted",
    }
    assert feishu.calls == 3
    assert store.signal(signal_id)["notification_attempts"] == 3  # type: ignore[index]

    store.close_signal(
        "market-1", ended_at=datetime.now(UTC).isoformat(), reason="data_unavailable"
    )
    assert service.notify_ready_opportunity("opp-1", signal_id) == {
        "state": "ignored",
        "reason": "signal_closed",
    }


def test_ready_notification_sends_only_after_read_only_proof(tmp_path: Path) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)

    result = service.notify_ready_opportunity("threshold-opp-1", signal_id)

    assert result["state"] == "sent"
    assert trading.threshold_preflight_calls == 1
    assert trading.threshold_submit_calls == 0
    assert trading.batch_calls == 0
    assert store.active_execution() is None
    assert store.signal(signal_id)["notification_state"] == "sent"  # type: ignore[index]
    assert store.signal(signal_id)["order_ready_at"]  # type: ignore[index]
    assert "notification_error_code" not in store.signal(signal_id)  # type: ignore[operator]
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    assert macos.calls == 0
    assert feishu.calls == 1


def test_two_service_instances_reserve_one_notification_attempt(tmp_path: Path) -> None:
    service, _, store, _ = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)
    store2 = PredictionArbitrageStore(tmp_path / "data")
    trading2 = ThresholdTrading()
    monitor2 = ThresholdMonitor(_threshold_intent())
    notifier2 = CompositeTestNotifier(ChannelNotifier("macos"), ChannelNotifier("feishu"))
    service2 = PredictionExecutionService(
        store=store2,
        monitor=monitor2,
        trading=trading2,
        notifier=notifier2,
        lock_path=tmp_path / "execution-2.lock",
    )
    assert service2.reconcile_startup()["state"] == "ready"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: item.notify_ready_opportunity("threshold-opp-1", signal_id),
                (service, service2),
            )
        )

    assert [result["state"] for result in results].count("sent") == 1
    assert sum(notifier.calls for notifier in notifier2._notifiers) <= 1
    assert store.signal(signal_id)["notification_attempts"] == 1  # type: ignore[index]


@pytest.mark.parametrize(
    "failure",
    (
        "breaker",
        "active_execution",
        "rules",
        "codex",
        "book_age",
        "economics",
        "account_freshness",
        "balance",
        "allowance",
        "geoblock",
        "relayer",
        "emergency_unwind",
        "preflight",
        "annualized",
    ),
)
def test_ready_notification_fails_closed_when_any_preflight_check_fails(
    tmp_path: Path, failure: str
) -> None:
    service, trading, store, monitor = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)
    if failure == "breaker":
        service._breaker_open = True  # type: ignore[attr-defined]
    elif failure == "active_execution":
        preview = service.preview("threshold-opp-1")
        store.consume_preview_and_create_execution(str(preview["id"]), "active")
    elif failure == "rules":
        monitor.rules_verified = False
    elif failure == "codex":
        monitor.codex_status = "pending"
    elif failure == "book_age":
        monitor.book_age_seconds = Decimal("11")
    elif failure == "economics":
        monitor.threshold_intent = replace(
            monitor.threshold_intent, minimum_profit=Decimal("0"), net_edge=Decimal("0")
        )
    elif failure == "account_freshness":
        trading.account_fresh = False
    elif failure == "balance":
        trading.balance = Decimal("1")
    elif failure == "allowance":
        trading.allowance = Decimal("1")
    elif failure == "geoblock":
        trading.geoblock = False
    elif failure == "relayer":
        trading.relayer_fresh = False
    elif failure == "emergency_unwind":
        monitor.remediation_safe = False
    elif failure == "preflight":
        trading.threshold_preflight_result = {"result": "FAIL"}
    elif failure == "annualized":
        monitor.annualized_yield = Decimal("0.149999")

    result = service.notify_ready_opportunity("threshold-opp-1", signal_id)

    assert result["state"] != "sent"
    assert trading.threshold_submit_calls == 0
    assert trading.batch_calls == 0
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    assert macos.calls == 0
    assert feishu.calls == 0


def test_ready_notification_final_intent_race_fails_closed(tmp_path: Path) -> None:
    service, trading, store, monitor = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)

    class ChangingThresholdMonitor(ThresholdMonitor):
        def __init__(self, original: ThresholdMonitor) -> None:
            super().__init__(original.threshold_intent)
            self.calls = 0

        def refresh_once(self) -> dict[str, object]:
            return {"opportunities": [self.opportunity("threshold-opp-1")]}

        def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
            self.calls += 1
            value = super().opportunity(opportunity_id)
            if value is not None and self.calls == 2:
                value["intent"] = replace(
                    self.threshold_intent,
                    total_max_cost=Decimal("2.13"),
                    minimum_profit=Decimal("7.87"),
                )
                value["total_max_cost"] = Decimal("2.13")
                value["minimum_profit"] = Decimal("7.87")
            return value

    changing = ChangingThresholdMonitor(monitor)
    service._monitor = changing  # type: ignore[attr-defined]

    result = service.notify_ready_opportunity("threshold-opp-1", signal_id)

    assert result == {"state": "failed", "reason": "opportunity_changed"}
    assert trading.threshold_submit_calls == 0
    assert trading.batch_calls == 0
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    assert macos.calls == 0
    assert feishu.calls == 0


@pytest.mark.parametrize("final_change", ("rules", "codex", "remediation", "hash"))
def test_ready_notification_rechecks_final_rule_and_codex_proof(
    tmp_path: Path, final_change: str
) -> None:
    service, trading, store, monitor = threshold_execution_fixture(tmp_path)
    signal_id = _notification_signal(store)

    class FinalChangeMonitor(ThresholdMonitor):
        def __init__(self, original: ThresholdMonitor) -> None:
            super().__init__(original.threshold_intent)
            self.calls = 0

        def refresh_once(self) -> dict[str, object]:
            return {"opportunities": [self.opportunity("threshold-opp-1")]}

        def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
            self.calls += 1
            value = super().opportunity(opportunity_id)
            if value is None or self.calls != 2:
                return value
            if final_change == "rules":
                value["rules_verified_at"] = None
            elif final_change == "codex":
                value["relation_validation"] = {"status": "pending"}
                value["llm_status"] = "pending"
            elif final_change == "remediation":
                value["remediation_safe"] = False
            else:
                value["rules_hash_b"] = "changed-hash"
            return value

    service._monitor = FinalChangeMonitor(monitor)  # type: ignore[attr-defined]

    result = service.notify_ready_opportunity("threshold-opp-1", signal_id)

    assert result == {"state": "failed", "reason": "opportunity_changed"}
    assert trading.threshold_submit_calls == 0
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    assert macos.calls == 0
    assert feishu.calls == 0


def test_ready_notification_retries_at_most_three_times_and_dedupes_episode(
    tmp_path: Path,
) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    feishu.fail = True
    signal_id = _notification_signal(store)

    for _ in range(3):
        result = service.notify_ready_opportunity("threshold-opp-1", signal_id)
        assert result["state"] == "failed"
    for _ in range(2):
        assert service.notify_ready_opportunity("threshold-opp-1", signal_id)["state"] == "ignored"
    assert feishu.calls == 3
    assert macos.calls == 0
    assert store.signal(signal_id)["notification_attempts"] == 3  # type: ignore[index]
    assert store.signal(signal_id)["notification_error_code"] == "delivery_failed"  # type: ignore[index]
    assert "notification_error" not in store.signal(signal_id)  # type: ignore[operator]

    store.close_signal(
        "relation-1", ended_at=datetime.now(UTC).isoformat(), reason="data_unavailable"
    )
    assert service.notify_ready_opportunity("threshold-opp-1", signal_id)["state"] == "ignored"

    next_signal_id = _notification_signal(store)
    feishu.fail = False
    assert service.notify_ready_opportunity("threshold-opp-1", next_signal_id)["state"] == "sent"
    assert feishu.calls == 4
    assert trading.threshold_submit_calls == 0
    assert trading.batch_calls == 0


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
            "holding_to_resolution",
            "neutralized_incident",
            "directional_incident",
            "merge_incident",
        }:
            return value
        time.sleep(0.01)
    return service.execution(execution_id)  # type: ignore[attr-defined]


def test_threshold_equal_fill_holds_without_merge(tmp_path: Path) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)

    preview = service.preview("threshold-opp-1")
    execution = service.confirm(str(preview["id"]), "threshold-request-1")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "holding_to_resolution"
    assert trading.threshold_preflight_calls == 1
    assert trading.threshold_submit_calls == 1
    assert trading.threshold_reconcile_calls == 1
    assert trading.merge_calls == 0
    evidence = final["evidence"]
    held = next(item for item in evidence if item.get("phase") == "holding_to_resolution")
    assert held["execution_proof"]["matched_refs"]["A"]["trade_ids"] == ["a-trade"]
    assert held["execution_proof"]["matched_refs"]["B"]["trade_ids"] == ["b-trade"]
    assert store.active_execution() is None


def test_two_threshold_holdings_do_not_block_a_new_preview(tmp_path: Path) -> None:
    service, _, store, _ = threshold_execution_fixture(tmp_path)
    payload = {
        "opportunity_id": "historical-threshold",
        "intent_type": "threshold_hedge",
        "market_type": "threshold_hedge",
        "intent": service._intent_payload(_threshold_intent()),
    }
    for index in range(2):
        preview_id = store.create_preview(payload, expires_at=(datetime.now(UTC) + timedelta(seconds=5)).isoformat())
        execution = store.consume_preview_and_create_execution(preview_id, f"historical-{index}")
        store.transition_execution(
            str(execution["execution_id"]),
            state="holding_to_resolution",
            evidence={"phase": "holding_to_resolution"},
        )

    result = service.preview("threshold-opp-1")
    assert result["state"] == "previewed"


def test_threshold_confirm_rejects_changed_rule_hash_before_post(tmp_path: Path) -> None:
    service, trading, _, monitor = threshold_execution_fixture(tmp_path)
    preview = service.preview("threshold-opp-1")
    monitor.rules_hash_b = "changed-hash"

    execution = service.confirm(str(preview["id"]), "threshold-rule-change")
    final = wait_until_terminal(service, str(execution["execution_id"]))

    assert final["state"] == "both_rejected"
    assert trading.threshold_preflight_calls == 0
    assert trading.threshold_submit_calls == 0
    evidence = final["evidence"]
    assert any(item.get("reason") == "rule_hash_changed" for item in evidence)


def test_startup_recognizes_known_threshold_holdings_without_merge(tmp_path: Path) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    payload = {
        "opportunity_id": "historical-threshold",
        "intent_type": "threshold_hedge",
        "market_type": "threshold_hedge",
        "intent": service._intent_payload(_threshold_intent()),
    }
    preview_id = store.create_preview(payload, expires_at=(datetime.now(UTC) + timedelta(seconds=5)).isoformat())
    execution = store.consume_preview_and_create_execution(preview_id, "known-holding")
    store.transition_execution(str(execution["execution_id"]), state="holding_to_resolution", evidence={"phase": "holding_to_resolution"})
    trading.holding_positions = (
        {"condition_id": "condition-a", "token_id": "a-token", "size": "10"},
        {"condition_id": "condition-b", "token_id": "b-token", "size": "10"},
    )

    result = service.reconcile_startup()

    assert result["state"] == "ready"
    assert trading.merge_calls == 0


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


def test_threshold_preview_preserves_both_market_questions_for_confirmation(
    tmp_path: Path,
) -> None:
    service, _, _, _ = threshold_execution_fixture(tmp_path)

    preview = service.preview("threshold-opp-1")

    assert preview["question_a"] == "Will BTC be above 90k?"
    assert preview["question_b"] == "Will BTC be above 100k?"
    assert preview["llm_status"] == "approved"
    assert preview["llm_decision"] == "APPROVE"
    assert preview["llm_summary"] == "The higher threshold implies the lower threshold."


@pytest.mark.parametrize(
    ("value", "expected_state", "expected_reason"),
    [
        (None, "rejected", "annualized_yield_unavailable"),
        ("NaN", "rejected", "annualized_yield_unavailable"),
        ("Infinity", "rejected", "annualized_yield_unavailable"),
        (Decimal("0.149999"), "rejected", "annualized_yield_below_minimum"),
        (Decimal("0.15"), "previewed", None),
    ],
)
def test_threshold_preview_enforces_annualized_floor(
    tmp_path: Path,
    value: object,
    expected_state: str,
    expected_reason: str | None,
) -> None:
    service, _, _, monitor = threshold_execution_fixture(tmp_path)
    monitor.annualized_yield = value

    result = service.preview("threshold-opp-1")

    assert result["state"] == expected_state
    if expected_reason is not None:
        assert result["reason"] == expected_reason


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
