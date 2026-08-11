from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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
from open_trader.predict_trading import PredictLegResult, PredictTradingClient


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
        self.positions: tuple[object, ...] = ()
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
            positions=self.positions,  # type: ignore[arg-type]
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


def test_control_mode_allows_only_risk_reduction_while_breaker_is_open(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor = execution_fixture(tmp_path)
    audit = {"actor": "local_operator", "git_sha": "abc123"}
    store.set_validation_mode("auto")
    service._breaker_open = True

    assert service.set_validation_mode("manual", audit=audit) == {
        "state": "ok",
        "mode": "manual",
    }
    store.set_validation_mode("observe_only")
    assert service.set_validation_mode("manual", audit=audit) == {
        "state": "locked",
        "reason": "circuit_breaker_open",
    }
    assert service.set_validation_mode("auto", audit=audit) == {
        "state": "locked",
        "reason": "circuit_breaker_open",
    }


def test_control_pause_is_immediate_but_upgrade_waits_for_active_execution(
    tmp_path: Path,
) -> None:
    service, _trading, store, _monitor = execution_fixture(tmp_path)
    audit = {"actor": "local_operator"}
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    store.arm_cross_auto()
    preview = service.preview("opp-1")
    store.consume_preview_and_create_execution(
        str(preview["preview_id"]), "active-control"
    )

    paused = service.pause_cross_auto(audit=audit)
    assert paused["armed"] is False
    assert paused["reason"] == "operator_paused"
    assert service.set_validation_mode("auto", audit=audit) == {
        "state": "busy",
        "reason": "active_execution",
    }


def test_control_maintenance_conflict_reuses_execution_lock(tmp_path: Path) -> None:
    service, _trading, _store, _monitor = execution_fixture(tmp_path)
    held = service._acquire_global_lock()
    assert held is not None
    try:
        assert service.reset_breaker(
            "incident-1", audit={"actor": "local_operator"}
        ) == {"state": "busy", "reason": "control_in_progress"}
        assert service.cleanup_predict_allowance(
            confirm=True, audit={"actor": "local_operator"}
        ) == {"state": "busy", "reason": "control_in_progress"}
    finally:
        service._release_global_lock(held)


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
    cutoff = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    return store.upsert_signal(
        {
            "opportunity_id": "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
            "market_id": "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
            "event_id": "public-pair",
            "pair_id": "public-pair",
            "direction": "PREDICT_YES_POLYMARKET_NO",
            "market_type": "cross_venue_yes_no",
            "execution_mode": "observe_only",
            "funnel_stage": 5,
            "actionable": True,
            "clear_signal": True,
            "quote_available": True,
            "started_at": now,
            "first_positive_at": now,
            "total_max_cost": Decimal("9.45"),
            "minimum_profit": Decimal("0.55"),
            "estimated_profit": Decimal("0.55"),
            "annualized_yield": Decimal("0.16"),
            "canonical_cutoff": cutoff,
            "notification_dedupe_identity": {
                "pair_id": "public-pair",
                "direction": "PREDICT_YES_POLYMARKET_NO",
                "predict_fingerprint": "predict-fingerprint",
                "polymarket_fingerprint": "poly-fingerprint",
                "predict_market_id": "predict-market",
                "predict_condition_id": "predict-condition",
                "predict_yes_token_id": "predict-yes",
                "predict_no_token_id": "predict-no",
                "polymarket_market_id": "poly-market",
                "polymarket_condition_id": "poly-condition",
                "polymarket_yes_token_id": "poly-yes",
                "polymarket_no_token_id": "poly-no",
            },
            "rules_fingerprints": {
                "predict.fun": "predict-fingerprint",
                "polymarket": "poly-fingerprint",
            },
            "approved_candidates": {
                "predict.fun": {
                    "market_id": "predict-market",
                    "condition_id": "predict-condition",
                    "yes_token_id": "predict-yes",
                    "no_token_id": "predict-no",
                    "rules_fingerprint": "predict-fingerprint",
                },
                "polymarket": {
                    "market_id": "poly-market",
                    "condition_id": "poly-condition",
                    "yes_token_id": "poly-yes",
                    "no_token_id": "poly-no",
                    "rules_fingerprint": "poly-fingerprint",
                },
            },
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


def test_notify_monitor_failure_llm_validation_uses_feishu_operator_copy(
    tmp_path: Path,
) -> None:
    service, _trading, _store, _monitor, macos, feishu = (
        standard_notification_fixture(tmp_path)
    )

    result = service.notify_monitor_failure(
        {
            "component": "llm_validation",
            "reason_codes": ["CODEX_FAILED", "DEEPSEEK_FAILED"],
            "summary": "Codex 与 DeepSeek 校验均不可用，当前不可下单。",
        }
    )

    assert result == {"state": "sent"}
    assert macos.calls == 0
    assert feishu.calls == 1
    title, message = feishu.messages[-1]
    assert title == "预测市场 LLM 校验不可用"
    assert "Codex 与 DeepSeek 校验均不可用" in message
    assert "CODEX_FAILED · DEEPSEEK_FAILED" in message
    assert "Dashboard：http://127.0.0.1:8766/" in message


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


def test_cross_venue_notification_rechecks_stage_5_without_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    signal_id = _cross_venue_notification_signal(store)
    monkeypatch.setattr(
        store,
        "create_preview",
        lambda *_args, **_kwargs: pytest.fail("notification must not create a preview"),
    )

    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result == {"state": "sent", "signal_id": signal_id}
    assert feishu.calls == 1
    assert macos.calls == 0
    assert cross.refresh_calls == 1
    assert trading.cross_preflight_calls == 0
    assert predict.preflight_calls == 0
    assert store.active_execution() is None
    assert store.cross_unsettled_principal() == Decimal("0")
    assert "/?prediction_signal=" in feishu.messages[-1][1]


def test_cross_venue_notification_failure_keeps_signal_actionable(
    tmp_path: Path,
) -> None:
    service, store, _trading, _cross, _predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    feishu.fail = True
    signal_id = _cross_venue_notification_signal(store)

    assert service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    ) == {"state": "failed", "reason": "notification_failed"}

    signal = store.signal(signal_id)
    assert signal is not None
    assert signal["actionable"] is True
    assert signal["funnel_stage"] == 5
    assert signal["ended_at"] is None


def test_gas_notification_only_for_blocked_stage_5_signal_episode(
    tmp_path: Path,
) -> None:
    service, store, _trading, _cross, predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    predict.gas_ready = False
    predict.minimum_top_up_bnb = "0.02"

    assert feishu.calls == 0
    signal_id = _cross_venue_notification_signal(store)

    assert service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    ) == {"state": "sent", "signal_id": signal_id, "reason": "insufficient_bnb"}
    assert service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    ) == {"state": "ignored", "reason": "already_sent"}
    assert feishu.calls == 1
    assert "BNB" in feishu.messages[-1][1]
    assert "0.02" in feishu.messages[-1][1]

    store.close_signal(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
        ended_at=datetime.now(UTC),
        reason="episode_rotated",
    )
    new_signal_id = store.upsert_signal(
        {
            **store.signal(signal_id),  # type: ignore[arg-type]
            "ended_at": None,
            "ended_reason": None,
            "notification_state": "pending",
            "notification_attempts": 0,
        }
    )
    assert new_signal_id != signal_id
    assert service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", new_signal_id
    ) == {"state": "sent", "signal_id": new_signal_id, "reason": "insufficient_bnb"}
    assert feishu.calls == 2


def test_allowance_incident_notifies_once_per_generation(tmp_path: Path) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    predict.allowance = "2.4"
    predict.clear_results.extend(
        [
            {"status": "failed", "market_id": "predict-market", "allowance": "2.4"},
            {"status": "failed", "market_id": "predict-market", "allowance": "2.4"},
        ]
    )

    first = service.cleanup_predict_allowance(confirm=True)
    second = service.cleanup_predict_allowance(confirm=True)

    assert first == {"state": "locked", "reason": "predict_allowance_cleanup_failed"}
    assert second == {"state": "locked", "reason": "predict_allowance_cleanup_failed"}
    assert feishu.calls == 1


def _cross_intent(*, predict_price: Decimal = Decimal("0.45")) -> CrossVenueIntent:
    now = datetime.now(UTC)
    return CrossVenueIntent(
        pair_id="public-pair",
        direction="PREDICT_YES_POLYMARKET_NO",
        legs=(
            CrossVenueLeg("predict.fun", "predict-market", "predict-condition", "YES", "predict-yes", "USDT", Decimal("5"), Decimal("5"), predict_price, Decimal("2.30"), Decimal("0.05"), "USDT", now, None, Decimal("1")),
            CrossVenueLeg("polymarket", "poly-market", "poly-condition", "NO", "poly-no", "pUSD", Decimal("5"), Decimal("5"), Decimal("0.47"), Decimal("2.40"), Decimal("0.05"), "pUSD", now, now + timedelta(days=30), Decimal("1")),
        ),
        quantity=Decimal("5"), calculable_gas=Decimal("0.10"), total_max_cost=Decimal("4.80"),
        maximum_fee=Decimal("0.10"), minimum_payout=Decimal("5"), minimum_profit=Decimal("0.20"),
        annualized_yield=Decimal("0.16"), canonical_cutoff=now + timedelta(days=30),
        resolution_at=now + timedelta(days=30), actionable=True, quote_available=True,
    )


def test_cross_holding_recognizes_canonical_predict_position() -> None:
    leg = _cross_intent().legs[0]
    snapshot = {
        "positions": (
            {
                "market_id": leg.market_id,
                "condition_id": leg.condition_id,
                "token_id": leg.token_id,
                "outcome": leg.outcome,
                "quantity": "5",
                "redeemable": True,
            },
        )
    }

    assert PredictionExecutionService._cross_position_quantity(snapshot, leg) == Decimal("5")
    assert PredictionExecutionService._cross_redeemable_winner(snapshot, leg) == {
        "venue": "predict.fun",
        "condition_id": "predict-condition",
        "outcome": "YES",
        "token_id": "predict-yes",
        "quantity": Decimal("5"),
    }


class CrossVenueMonitor:
    def __init__(self, intent: CrossVenueIntent) -> None:
        self.intent = intent
        self.overrides: dict[str, object] = {}
        self.refresh_calls = 0
        self.refresh_requests: list[dict[str, object]] = []
        self.refresh_intent_resolver: object | None = None
        self.available = True
        self.call_log: list[str] = []
        self.max_normal_cost_requests: list[Decimal | None] = []

    def _opportunity(self, intent: CrossVenueIntent) -> dict[str, object]:
        now = datetime.now(UTC)
        opportunity: dict[str, object] = {
            "opportunity_id": f"cross:{intent.pair_id}:{intent.direction}",
            "pair_id": intent.pair_id,
            "market_type": "cross_venue_yes_no", "funnel_stage": 5,
            "execution_mode": "manual_confirm",
            "actionable": True, "clear_signal": True, "intent": intent,
            "direction": intent.direction,
            "confirmed_at": now, "confirmed_age_seconds": Decimal("1"),
            "canonical_cutoff": intent.canonical_cutoff,
            "signal_episode_id": "signal-episode-1",
            "codex_approval": {"decision": "APPROVE", "cache_key": "cross-cache", "direct_outcome_mapping": {"predict_yes": "YES", "predict_no": "NO", "polymarket_yes": "YES", "polymarket_no": "NO"}, "evidence": [{"exchange": "predict.fun", "quote": "same rules"}, {"exchange": "polymarket", "quote": "same rules"}]},
            "rules_fingerprints": {"predict.fun": "predict-fingerprint", "polymarket": "poly-fingerprint"},
            "approved_candidates": {
                "predict.fun": {"market_id": "predict-market", "condition_id": "predict-condition", "yes_token_id": "predict-yes", "no_token_id": "predict-no", "rules_fingerprint": "predict-fingerprint"},
                "polymarket": {"market_id": "poly-market", "condition_id": "poly-condition", "yes_token_id": "poly-yes", "no_token_id": "poly-no", "rules_fingerprint": "poly-fingerprint"},
            },
        }
        opportunity.update(self.overrides)
        return opportunity

    def refresh_opportunity(
        self,
        opportunity_id: str,
        *,
        target_quantity: Decimal | None = None,
        max_total_cost: Decimal | None = None,
        prefer_smallest: bool = False,
    ) -> dict[str, object] | None:
        self.refresh_calls += 1
        self.call_log.append("refresh")
        self.max_normal_cost_requests.append(max_total_cost)
        self.refresh_requests.append(
            {
                "opportunity_id": opportunity_id,
                "target_quantity": target_quantity,
                "max_total_cost": max_total_cost,
                "prefer_smallest": prefer_smallest,
            }
        )
        if not self.available:
            return None
        intent = self.intent
        resolver = self.refresh_intent_resolver
        if callable(resolver):
            intent = resolver(
                opportunity_id=opportunity_id,
                target_quantity=target_quantity,
                max_total_cost=max_total_cost,
                prefer_smallest=prefer_smallest,
            )
        value = self._opportunity(intent)
        return value if value["opportunity_id"] == opportunity_id else None

    def snapshot(self) -> dict[str, object]:
        opportunity = self._opportunity(self.intent)
        return {
            "mode": opportunity["execution_mode"],
            "status": "ready",
            "opportunities": [opportunity],
        }


class CrossPolymarketTrading(FakeTrading):
    def __init__(self) -> None:
        super().__init__()
        self.cross_preflight_calls = 0
        self.cross_submit_calls = 0
        self.cross_reconcile_calls = 0
        self.submit_started = threading.Event()
        self.submit_release = threading.Event()
        self.submit_barrier: threading.Barrier | None = None
        self.block_submit = False
        self.submit_results: list[ThresholdLegResult] = []
        self.reconcile_results: list[dict[str, object]] = []
        self.balance_after_cross_submit: Decimal | None = None
        self.cross_submitted_legs: list[CrossVenueLeg] = []
        self.cross_remediation_options: dict[str, dict[str, object]] = {}
        self.cross_remediation_option_calls: list[dict[str, object]] = []
        self.cross_remediation_calls: list[dict[str, object]] = []
        self.call_log: list[str] = []
        self.omit_canary_fee_proof = False
        self.omit_canary_order_proof = False

    def no_submit_cross_leg_preflight(self, leg: CrossVenueLeg) -> dict[str, object]:
        self.call_log.append("poly_preflight")
        self.cross_preflight_calls += 1
        return {"result": "PASS", "leg": leg}

    def submit_cross_leg_once(self, leg: CrossVenueLeg) -> ThresholdLegResult:
        self.call_log.append("poly_submit")
        self.cross_submit_calls += 1
        self.cross_submitted_legs.append(leg)
        self.submit_started.set()
        if self.submit_barrier is not None:
            self.submit_barrier.wait(timeout=2)
        if self.block_submit:
            assert self.submit_release.wait(timeout=2)
        if self.balance_after_cross_submit is not None:
            self.balance = self.balance_after_cross_submit
        if self.submit_results:
            return self.submit_results.pop(0)
        return ThresholdLegResult(
            "polymarket", leg.outcome, leg.condition_id, leg.token_id,
            True, "filled", "poly-order", leg.net_quantity, ("poly-trade",), "none",
        )

    def reconcile_cross_leg(
        self, leg: CrossVenueLeg, result: ThresholdLegResult, *, since: datetime
    ) -> dict[str, object]:
        self.call_log.append("poly_reconcile")
        self.cross_reconcile_calls += 1
        if self.reconcile_results:
            return {
                **self.reconcile_results.pop(0),
                "minimum_order_size": leg.minimum_order_size,
            }
        proof: dict[str, object] = {"verified": True, "venue": "polymarket"}
        if not self.omit_canary_order_proof:
            proof["matched_refs"] = {
                "token_id": leg.token_id,
                "order_ids": [result.order_id],
                "trade_ids": list(result.trade_ids),
            }
        result_payload = {
            "status": "verified", "verified": True,
            "filled_quantity": result.filled_quantity,
            "position_quantity": leg.net_quantity,
            "minimum_order_size": leg.minimum_order_size,
            "execution_proof": proof,
        }
        return result_payload

    def cross_remediation_option(self, **kwargs: object) -> dict[str, object]:
        self.cross_remediation_option_calls.append(dict(kwargs))
        side = str(kwargs.get("side", ""))
        return dict(self.cross_remediation_options.get(side, {"fresh": False}))

    def submit_remediation_once(self, order: dict[str, object]) -> LegResult:
        self.cross_remediation_calls.append(dict(order))
        return LegResult(
            str(order.get("leg", "NO")), True, "filled", "cross-unwind-order",
            Decimal(str(order.get("quantity", order.get("shares", "0")))),
            ("cross-unwind-trade",), "none",
        )


class CrossPredictTrading:
    def __init__(self) -> None:
        self.account_calls = 0
        self.scope_ready = True
        self.gas_ready = True
        self.allowance_breaker = False
        self.allowance = "0"
        self.allowance_raw = "0"
        self.use_legacy_allowance_ready = False
        self.allowance_ready = True
        self.account_available = True
        self.balance = Decimal("5")
        self.minimum_order_size = Decimal("1")
        self.positions: tuple[object, ...] = ()
        self.preflight_calls = 0
        self.cross_entry_preflight_orders: list[dict[str, object]] = []
        self.cross_entry_submit_orders: list[dict[str, object]] = []
        self.cross_entry_allowed = True
        self.submit_calls = 0
        self.reconcile_calls = 0
        self.submit_started = threading.Event()
        self.submit_release = threading.Event()
        self.submit_barrier: threading.Barrier | None = None
        self.block_submit = False
        self.submit_results: list[PredictLegResult] = []
        self.reconcile_results: list[dict[str, object]] = []
        self.balance_after_cross_submit: Decimal | None = None
        self.cross_remediation_options: dict[str, dict[str, object]] = {}
        self.cross_remediation_option_calls: list[dict[str, object]] = []
        self.default_filled_quantity = Decimal("5")
        self.call_log: list[str] = []
        self.predict_account = "0xpredict-account"
        self.gas_signer = "0xgas-signer"
        self.chain = "bnb-mainnet"
        self.sdk_version = "predict-sdk-1"
        self.approval_step_id = "approval-step-buy"
        self.bnb_balance = "0.01"
        self.required_bnb = "0.003"
        self.minimum_top_up_bnb = "0"
        self.approval_calls: list[tuple[str, int]] = []
        self.clear_calls: list[str] = []
        self.approval_results: list[dict[str, object]] = []
        self.clear_results: list[dict[str, object]] = []
        self.omit_canary_fee_proof = False
        self.omit_canary_order_proof = False

    def account_snapshot(self) -> dict[str, object]:
        self.call_log.append("predict_account")
        self.account_calls += 1
        if not self.account_available:
            raise RuntimeError("account unavailable")
        snapshot = {
            "wallet_address": "0xpredict",
            "available_usdt": str(self.balance),
            "available_usdt_raw": str(int(self.balance * Decimal(10**18))),
            "open_orders": (),
            "positions": self.positions,
            "checked_at": datetime.now(UTC),
        }
        if self.use_legacy_allowance_ready:
            snapshot["allowance_ready"] = self.allowance_ready
            return snapshot
        snapshot.update(
            {
                "allowance": self.allowance,
                "allowance_raw": self.allowance_raw,
                "scope_ready": self.scope_ready,
                "gas_ready": self.gas_ready,
                "allowance_breaker": self.allowance_breaker,
                "predict_account": self.predict_account,
                "gas_signer": self.gas_signer,
                "chain": self.chain,
                "sdk_version": self.sdk_version,
                "approval_step_id": self.approval_step_id,
                "bnb_balance": self.bnb_balance,
                "required_bnb": self.required_bnb,
                "minimum_top_up_bnb": self.minimum_top_up_bnb,
            }
        )
        return snapshot

    def set_exact_buy_allowance(self, market_id: str, exact_debit_wei: int) -> dict[str, object]:
        self.call_log.append("set_allowance")
        self.approval_calls.append((market_id, exact_debit_wei))
        if self.approval_results:
            result = self.approval_results.pop(0)
            if "allowance" in result:
                self.allowance = str(result["allowance"])
            if "allowance_raw" in result:
                self.allowance_raw = str(result["allowance_raw"])
            self.allowance_breaker = (
                Decimal(self.allowance) != 0 or Decimal(self.allowance_raw) != 0
            )
            return result
        self.allowance = str(Decimal(exact_debit_wei) / Decimal(10**18))
        self.allowance_raw = str(exact_debit_wei)
        self.allowance_breaker = exact_debit_wei != 0
        return {
            "status": "confirmed",
            "market_id": market_id,
            "exact_debit_wei": exact_debit_wei,
            "allowance": self.allowance,
            "allowance_raw": self.allowance_raw,
            "transaction_hash": "0xapprove",
        }

    def clear_buy_allowance(self, market_id: str) -> dict[str, object]:
        self.call_log.append("clear_allowance")
        self.clear_calls.append(market_id)
        if self.clear_results:
            return self.clear_results.pop(0)
        self.allowance = "0"
        self.allowance_raw = "0"
        self.allowance_breaker = False
        return {
            "status": "confirmed",
            "market_id": market_id,
            "allowance": "0",
            "allowance_raw": "0",
            "transaction_hash": "0xclear",
        }

    def no_submit_buy_preflight(
        self, market_id: str, token_id: str, quantity_wei: int
    ) -> PredictLegResult:
        self.preflight_calls += 1
        assert (market_id, token_id) == ("predict-market", "predict-yes")
        assert quantity_wei > 0
        return PredictLegResult(True, "preflight")

    def no_submit_cross_buy_preflight(self, order: dict[str, object]) -> PredictLegResult:
        self.call_log.append("predict_preflight")
        self.preflight_calls += 1
        self.cross_entry_preflight_orders.append(dict(order))
        assert order["venue"] == "predict.fun"
        assert order["market_id"] == "predict-market"
        assert order["token_id"] == "predict-yes"
        assert order["execution_id"] == order["idempotency_key"]
        return PredictLegResult(
            self.cross_entry_allowed,
            "preflight" if self.cross_entry_allowed else "rejected",
            error_code="none" if self.cross_entry_allowed else "rejected",
        )

    def submit_buy_once(
        self, market_id: str, token_id: str, quantity_wei: int
    ) -> PredictLegResult:
        self.call_log.append("predict_submit")
        self.submit_calls += 1
        self.submit_started.set()
        if self.submit_barrier is not None:
            self.submit_barrier.wait(timeout=2)
        if self.block_submit:
            assert self.submit_release.wait(timeout=2)
        if self.balance_after_cross_submit is not None:
            self.balance = self.balance_after_cross_submit
        if self.submit_results:
            return self.submit_results.pop(0)
        return PredictLegResult(True, "filled", "predict-order")

    def submit_cross_buy_once(self, order: dict[str, object]) -> PredictLegResult:
        self.cross_entry_submit_orders.append(dict(order))
        return self.submit_buy_once(
            str(order["market_id"]),
            str(order["token_id"]),
            int(Decimal(str(order["requested_quantity"])) * Decimal(10**18)),
        )

    def reconcile_buy(
        self, market_id: str, token_id: str, order_hash: str
    ) -> dict[str, object]:
        self.call_log.append("predict_reconcile")
        self.reconcile_calls += 1
        assert (market_id, token_id) == ("predict-market", "predict-yes")
        if self.reconcile_results:
            return {
                **self.reconcile_results.pop(0),
                "minimum_order_size": self.minimum_order_size,
            }
        proof: dict[str, object] = {"verified": True, "venue": "predict.fun"}
        if not self.omit_canary_order_proof:
            proof.update({"order_ids": [order_hash], "trade_ids": ["predict-trade"]})
        if not self.omit_canary_fee_proof:
            proof["fee"] = Decimal("0.05")
        result_payload = {
            "status": "verified", "verified": True,
            "filled_quantity": self.default_filled_quantity,
            "position_quantity": self.default_filled_quantity,
            "minimum_order_size": self.minimum_order_size,
            "execution_proof": proof,
        }
        if not self.omit_canary_fee_proof:
            result_payload["actual_fee"] = Decimal("0.05")
        return result_payload

    def cross_remediation_option(self, **kwargs: object) -> dict[str, object]:
        self.cross_remediation_option_calls.append(dict(kwargs))
        side = str(kwargs.get("side", ""))
        return dict(self.cross_remediation_options.get(side, {"fresh": False}))

    def submit_cross_remediation_once(self, order: dict[str, object]) -> PredictLegResult:
        self.submit_calls += 1
        return PredictLegResult(True, "filled", "predict-remediation-order")


def _fresh_cross_option(
    leg: CrossVenueLeg,
    *,
    side: str,
    price: Decimal,
    fee: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
    residual_dust: Decimal = Decimal("0"),
) -> dict[str, object]:
    option: dict[str, object] = {
        "venue": leg.exchange,
        "market_id": leg.market_id,
        "condition_id": leg.condition_id,
        "token_id": leg.token_id,
        "outcome": leg.outcome,
        "side": side,
        "quantity": leg.net_quantity,
        "executable_price": price,
        "fee": fee,
        "slippage": slippage,
        "residual_dust": residual_dust,
    }
    if side == "BUY":
        option["max_spend"] = leg.net_quantity * price + fee + slippage
    else:
        option["shares"] = leg.net_quantity
        option["min_price"] = price
    return {"fresh": True, "checked_at": datetime.now(UTC), "option": option}


def _cross_service(tmp_path: Path) -> tuple[PredictionExecutionService, PredictionArbitrageStore, CrossPolymarketTrading, CrossVenueMonitor, CrossPredictTrading]:
    store = PredictionArbitrageStore(tmp_path / "data")
    trading = CrossPolymarketTrading()
    cross = CrossVenueMonitor(_cross_intent())
    predict = CrossPredictTrading()
    service = PredictionExecutionService(
        store=store, monitor=FakeMonitor(_intent()), trading=trading, predict_trading=predict,
        notifier=CompositeTestNotifier(ChannelNotifier("macos"), ChannelNotifier("feishu")),
        lock_path=tmp_path / "execution.lock",
    )
    call_log: list[str] = []
    trading.call_log = predict.call_log = cross.call_log = call_log
    service.set_cross_venue_monitor(cross)
    assert service.reconcile_startup()["state"] == "ready"
    return service, store, trading, cross, predict


def _cross_execution(
    service: PredictionExecutionService, *, idempotency_key: str = "cross-submit"
) -> tuple[dict[str, object], dict[str, object]]:
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    accepted = service.confirm(str(preview["preview_id"]), idempotency_key)
    execution_id = str(accepted["execution_id"])
    wait_until_terminal(service, execution_id)
    deadline = time.monotonic() + 3
    while execution_id in service._threads and time.monotonic() < deadline:
        time.sleep(0.01)
    assert execution_id not in service._threads
    return accepted, service.execution(execution_id)


def test_cross_exact_allowance_wraps_current_dual_rest_refresh_before_submit(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)

    _accepted, final = _cross_execution(service, idempotency_key="cross-exact-allowance")

    assert final["state"] == "holding_to_resolution"
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == ["predict-market"]
    set_allowance = cross.call_log.index("set_allowance")
    refreshes = [index for index, item in enumerate(cross.call_log) if item == "refresh"]
    first_refresh = max(index for index in refreshes if index < set_allowance)
    second_refresh = min(index for index in refreshes if index > set_allowance)
    predict_preflights = [
        index for index, item in enumerate(cross.call_log) if item == "predict_preflight"
    ]
    poly_preflights = [
        index for index, item in enumerate(cross.call_log) if item == "poly_preflight"
    ]
    predict_submit = cross.call_log.index("predict_submit")
    poly_submit = cross.call_log.index("poly_submit")
    predict_reconcile = cross.call_log.index("predict_reconcile")
    poly_reconcile = cross.call_log.index("poly_reconcile")
    clear_allowance = cross.call_log.index("clear_allowance")
    assert first_refresh < set_allowance < second_refresh
    assert predict_preflights[0] < poly_preflights[0] < set_allowance
    assert second_refresh < predict_preflights[1] < poly_preflights[1]
    assert poly_preflights[1] < predict_submit
    assert poly_preflights[1] < poly_submit
    assert predict_submit < predict_reconcile < clear_allowance
    assert poly_submit < poly_reconcile < clear_allowance
    assert final["evidence"][-1]["predict_allowance"] == {
        "market_id": "predict-market",
        "after": "0",
        "zero_verified": True,
    }
    assert (trading.cross_submit_calls, predict.submit_calls) == (1, 1)


@pytest.mark.parametrize("failed_venue", ("predict.fun", "polymarket"))
def test_cross_refreshed_preflight_failure_clears_without_submit_or_retry(
    tmp_path: Path, failed_venue: str,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    if failed_venue == "predict.fun":
        original = predict.no_submit_cross_buy_preflight

        def predict_preflight(order: dict[str, object]) -> PredictLegResult:
            result = original(order)
            return (
                PredictLegResult(False, "rejected", error_code="rejected")
                if predict.preflight_calls == 2
                else result
            )

        predict.no_submit_cross_buy_preflight = predict_preflight  # type: ignore[method-assign]
    else:
        original = trading.no_submit_cross_leg_preflight

        def polymarket_preflight(leg: CrossVenueLeg) -> dict[str, object]:
            result = original(leg)
            return {"result": "FAIL"} if trading.cross_preflight_calls == 2 else result

        trading.no_submit_cross_leg_preflight = polymarket_preflight  # type: ignore[method-assign]

    _accepted, final = _cross_execution(
        service, idempotency_key=f"cross-refreshed-preflight-{failed_venue}"
    )

    assert final["state"] == "both_rejected"
    assert (trading.cross_preflight_calls, predict.preflight_calls) == (2, 2)
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert predict.clear_calls == ["predict-market"]
    assert predict.allowance == "0"
    assert store.cross_unsettled_principal() == Decimal("0")
    evidence = final["evidence"][-1]
    assert evidence["reason"] == "cross_preflight_failed"
    assert evidence["submitted"] is False
    assert evidence["status_text"] == "未下单 · 授权已清零"
    assert evidence["predict_allowance"]["zero_verified"] is True
    assert "predict_submit" not in cross.call_log
    assert "poly_submit" not in cross.call_log


def test_cross_exact_approval_failure_posts_neither_venue(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.approval_results.append(
        {
            "status": "failed",
            "error_code": "receipt_failed",
            "possible_mutation": False,
            "market_id": "predict-market",
            "allowance": "0",
            "allowance_raw": "0",
        }
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-approval-fails")

    assert final["state"] == "both_rejected"
    assert final["evidence"][-1]["status_text"] == "未下单"
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == []
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_ambiguous_exact_approval_holds_reservation_and_opens_incident_without_submit(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.approval_results.append(
        {
            "status": "failed",
            "error_code": "receipt_ambiguous",
            "possible_mutation": True,
            "market_id": "predict-market",
            "allowance": "0",
            "allowance_raw": "0",
        }
    )

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-approval-ambiguous"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_approval_unverified"
    assert service._cross_breaker_open is True
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == []
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert store.cross_unsettled_principal() == Decimal("4.80")
    incidents = store.histories("incidents")
    assert len(incidents) == 1
    assert incidents[0]["reason"] == "predict_allowance_approval_unverified"


def test_cross_malformed_success_like_receipt_opens_approval_incident_without_submit(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    sdk_calls: list[tuple[object, bool, int]] = []
    step = SimpleNamespace(id="approval-step-buy", spender="0xspender")

    def sdk_set_approval(
        approval_step: object, *, approved: bool, amount: int
    ) -> object:
        sdk_calls.append((approval_step, approved, amount))
        return SimpleNamespace(
            success=True,
            receipt={"status": 1.5, "transactionHash": "0xmalformed"},
            cause=None,
        )

    adapter = PredictTradingClient.__new__(PredictTradingClient)
    adapter._builder = SimpleNamespace(set_approval=sdk_set_approval)  # type: ignore[attr-defined]
    adapter._approval_scope_for_market = lambda _market_id: object()  # type: ignore[method-assign]
    adapter._approval_facts_for_scope = lambda _scope, *, exact_debit_wei: {  # type: ignore[method-assign]
        "predict_account": predict.predict_account,
        "gas_signer": predict.gas_signer,
        "approval_step_id": predict.approval_step_id,
        "approval_spender": step.spender,
        "allowance": "0",
        "allowance_raw": "0",
        "allowance_breaker": False,
        "exact_debit_wei": exact_debit_wei,
    }
    adapter._approval_step = lambda _scope: step  # type: ignore[method-assign]
    adapter._raw_allowance = lambda _step: 2_300_000_000_000_000_000  # type: ignore[method-assign]

    def malformed_approval(market_id: str, exact_debit_wei: int) -> Mapping[str, object]:
        predict.call_log.append("set_allowance")
        predict.approval_calls.append((market_id, exact_debit_wei))
        result = adapter.set_exact_buy_allowance(market_id, exact_debit_wei)
        if result.get("status") == "confirmed":
            predict.allowance = str(Decimal(exact_debit_wei) / Decimal(10**18))
            predict.allowance_raw = str(exact_debit_wei)
            predict.allowance_breaker = True
        return result

    predict.set_exact_buy_allowance = malformed_approval  # type: ignore[method-assign]

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-approval-malformed-success-status"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_approval_unverified"
    assert service._cross_breaker_open is True
    assert sdk_calls == [(step, True, 2_300_000_000_000_000_000)]
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == []
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_cross_post_approval_refresh_breach_clears_allowance_without_submit(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    refreshes = 0

    def resolver(**_: object) -> CrossVenueIntent:
        nonlocal refreshes
        refreshes += 1
        if refreshes < 3:
            return cross.intent
        return replace(
            cross.intent,
            legs=(replace(predict_leg, max_cost=Decimal("2.31")), polymarket_leg),
            total_max_cost=Decimal("4.81"),
            minimum_profit=Decimal("0.19"),
        )

    cross.refresh_intent_resolver = resolver

    _accepted, final = _cross_execution(service, idempotency_key="cross-post-approval-breach")

    assert final["state"] == "both_rejected"
    assert final["evidence"][-1]["status_text"] == "未下单 · 授权已清零"
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == ["predict-market"]
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)


def test_cross_post_approval_breach_cleanup_failure_persists_one_immediate_incident(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    refreshes = 0

    def resolver(**_: object) -> CrossVenueIntent:
        nonlocal refreshes
        refreshes += 1
        if refreshes < 3:
            return cross.intent
        return replace(
            cross.intent,
            legs=(replace(predict_leg, max_cost=Decimal("2.31")), polymarket_leg),
            total_max_cost=Decimal("4.81"),
            minimum_profit=Decimal("0.19"),
        )

    cross.refresh_intent_resolver = resolver
    predict.clear_results.append(
        {"status": "confirmed", "market_id": "predict-market", "allowance": "0.01"}
    )

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-post-approval-breach-cleanup-fails"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_cleanup_failed"
    assert service._cross_breaker_open is True
    assert predict.clear_calls == ["predict-market"]
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert store.cross_unsettled_principal() == Decimal("4.80")
    incidents = store.histories("incidents")
    assert len(incidents) == 1
    assert incidents[0]["reason"] == "predict_allowance_cleanup_failed"


def test_cross_confirmed_approval_with_unverified_post_read_opens_incident_without_submit(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.approval_results.append(
        {
            "status": "confirmed",
            "market_id": "predict-market",
            "allowance": "2.30",
            "transaction_hash": "0xapprove",
        }
    )

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-approval-post-read-unverified"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_approval_unverified"
    assert service._cross_breaker_open is True
    assert predict.approval_calls == [
        ("predict-market", 2_300_000_000_000_000_000)
    ]
    assert predict.clear_calls == []
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert store.cross_unsettled_principal() == Decimal("4.80")
    incidents = store.histories("incidents")
    assert len(incidents) == 1
    assert incidents[0]["reason"] == "predict_allowance_approval_unverified"


def test_cross_exact_approval_rejects_mismatched_raw_post_read_without_submit(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.approval_results.append(
        {
            "status": "confirmed",
            "market_id": "predict-market",
            "allowance": "2.3",
            "allowance_raw": "2299999999999999999",
            "transaction_hash": "0xapprove",
        }
    )

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-approval-raw-post-read-mismatch"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_approval_unverified"
    assert (trading.cross_submit_calls, predict.submit_calls) == (0, 0)
    assert predict.clear_calls == []
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_cross_cleanup_failure_opens_breaker_and_persists_one_incident(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    predict.clear_results.append(
        {"status": "confirmed", "market_id": "predict-market", "allowance": "0.01"}
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-cleanup-fails")

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "predict_allowance_cleanup_failed"
    assert service._cross_breaker_open is True
    assert predict.clear_calls == ["predict-market"]
    assert (trading.cross_submit_calls, predict.submit_calls) == (1, 1)
    incidents = store.histories("incidents")
    assert len(incidents) == 1
    assert incidents[0]["reason"] == "predict_allowance_cleanup_failed"


def test_cross_both_rejected_clears_allowance_before_releasing_capacity(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    trading.submit_results.append(
        ThresholdLegResult(
            "polymarket", "NO", "poly-condition", "poly-no", False,
            "rejected", "", Decimal("0"), (), "rejected",
        )
    )
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    trading.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    predict.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-both-rejects-clear")

    assert final["state"] == "both_rejected"
    assert predict.clear_calls == ["predict-market"]
    assert cross.call_log.index("clear_allowance") < cross.call_log.index("predict_account", cross.call_log.index("clear_allowance"))
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_unknown_submit_keeps_allowance_and_breaker_fail_closed(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.submit_results.append(PredictLegResult(False, "ambiguous", "predict-order", "ambiguous"))
    predict.reconcile_results.append(
        {"status": "unknown", "verified": False, "conclusively_absent": False}
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-unknown-keeps-allowance")

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "cross_reconciliation_unknown"
    assert predict.clear_calls == []
    assert service._cross_breaker_open is True
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_cross_predict_accepted_without_order_id_is_unknown_without_cleanup_or_remediation(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.submit_results.append(PredictLegResult(True, "accepted", "", "none"))

    _accepted, final = _cross_execution(
        service, idempotency_key="cross-predict-empty-order-id"
    )

    assert final["state"] == "directional_incident"
    assert final["evidence"][-1]["reason"] == "cross_reconciliation_unknown"
    assert predict.allowance == "2.3"
    assert predict.clear_calls == []
    assert predict.cross_remediation_option_calls == []
    assert trading.cross_remediation_option_calls == []
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_residual_predict_allowance_startup_locks_and_operator_cleanup_is_read_only(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"
    service._cross_breaker_open = False

    result = service.reconcile_startup()

    assert result["state"] == "locked"
    assert result["reason"] == "residual_predict_allowance"
    assert predict.clear_calls == []
    assert trading.cross_submit_calls == 0
    assert store.unacknowledged_incident()["reason"] == "residual_predict_allowance"

    cleanup = service.cleanup_predict_allowance(confirm=True)

    assert cleanup["state"] == "ready"
    assert cleanup["before_allowance"] == "2.4"
    assert cleanup["after_allowance"] == "0"
    assert cleanup["usdt_moved"] is False
    assert predict.clear_calls == ["predict-market"]


@pytest.mark.parametrize("confirm", [False])
def test_predict_allowance_cleanup_rejects_without_confirmation(
    tmp_path: Path, confirm: bool,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"

    result = service.cleanup_predict_allowance(confirm=confirm)

    assert result == {"state": "locked", "reason": "confirmation_required"}
    assert predict.clear_calls == []


def test_predict_allowance_cleanup_rejects_active_execution_without_mutation(
    tmp_path: Path,
) -> None:
    service, store, _trading, _cross, predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    store.consume_preview_and_create_execution(str(preview["preview_id"]), "cleanup-active")
    predict.allowance = "2.4"

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {"state": "locked", "reason": "active_execution"}
    assert predict.clear_calls == []


def test_predict_allowance_cleanup_uses_residual_breaker_as_reason_to_clear(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"
    predict.allowance_breaker = True

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {
        "state": "ready",
        "before_allowance": "2.4",
        "after_allowance": "0",
        "usdt_moved": False,
    }
    assert predict.clear_calls == ["predict-market"]


def test_predict_allowance_cleanup_clears_raw_residual_with_zero_human_projection(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "0"
    predict.allowance_raw = "1"
    predict.allowance_breaker = True

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {
        "state": "ready",
        "before_allowance": "0",
        "after_allowance": "0",
        "usdt_moved": False,
    }
    assert predict.clear_calls == ["predict-market"]


def test_predict_allowance_cleanup_rejects_insufficient_bnb_without_mutation(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"
    predict.minimum_top_up_bnb = "0.001"

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {
        "state": "locked",
        "reason": "insufficient_bnb",
        "minimum_top_up_bnb": "0.001",
    }
    assert predict.clear_calls == []


def test_control_cleanup_predict_allowance_is_idempotent_when_already_zero(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {
        "state": "ready",
        "before_allowance": "0",
        "after_allowance": "0",
        "usdt_moved": False,
    }
    assert predict.clear_calls == []


def test_control_cleanup_predict_allowance_does_not_mutate_when_audit_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"

    def fail_start(**_kwargs: object) -> str:
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(store, "begin_control_event", fail_start)

    result = service.cleanup_predict_allowance(
        confirm=True, audit={"actor": "local_operator"}
    )

    assert result == {"state": "locked", "reason": "audit_persistence_failed"}
    assert predict.clear_calls == []


def test_control_cleanup_predict_allowance_recovers_started_audit_without_second_chain_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"
    original_finish = store.finish_control_event
    finish_calls = 0

    def fail_first_finish(
        event_id: str, *, outcome: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise sqlite3.OperationalError("audit unavailable")
        return original_finish(event_id, outcome=outcome, payload=payload)

    monkeypatch.setattr(store, "finish_control_event", fail_first_finish)

    first = service.cleanup_predict_allowance(
        confirm=True, audit={"actor": "local_operator"}
    )
    started = store.latest_control_event(
        "cleanup_predict_allowance", "predict_allowance"
    )
    second = service.cleanup_predict_allowance(
        confirm=True, audit={"actor": "local_operator"}
    )

    assert first == {"state": "locked", "reason": "audit_persistence_failed"}
    assert started is not None and started["outcome"] == "started"
    assert second == {
        "state": "ready",
        "before_allowance": "2.4",
        "after_allowance": "0",
        "usdt_moved": False,
    }
    assert predict.clear_calls == ["predict-market"]
    finished = store.latest_control_event(
        "cleanup_predict_allowance", "predict_allowance"
    )
    assert finished is not None and finished["outcome"] == "succeeded"


def test_predict_allowance_cleanup_rejects_changed_identity_after_clear(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "2.4"
    original = predict.clear_buy_allowance

    def changed_identity_clear(market_id: str) -> dict[str, object]:
        result = original(market_id)
        predict.gas_signer = "0xchanged"
        return result

    predict.clear_buy_allowance = changed_identity_clear  # type: ignore[method-assign]

    result = service.cleanup_predict_allowance(confirm=True)

    assert result == {"state": "locked", "reason": "predict_allowance_cleanup_failed"}
    assert predict.clear_calls == ["predict-market"]


def test_cross_canary_cap_stays_five_until_exact_zero_allowance_success_is_verified(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    predict.reconcile_results.append(
        {
            "status": "verified",
            "verified": True,
            "filled_quantity": Decimal("5"),
            "position_quantity": Decimal("5"),
            "actual_fee": Decimal("0.05"),
            "execution_proof": {
                "verified": True,
                "venue": "predict.fun",
                "order_ids": ["predict-order"],
                "trade_ids": ["predict-trade"],
                "fee": Decimal("0.05"),
            },
        }
    )
    trading.reconcile_results.append(
        {
            "status": "verified",
            "verified": True,
            "filled_quantity": Decimal("5"),
            "position_quantity": Decimal("5"),
            "actual_fee": Decimal("0.05"),
            "execution_proof": {
                "verified": True,
                "venue": "polymarket",
                "fee": Decimal("0.05"),
                "matched_refs": {
                    "token_id": "poly-no",
                    "order_ids": ["poly-order"],
                    "trade_ids": ["poly-trade"],
                },
            },
        }
    )

    first = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    _accepted, final = _cross_execution(service, idempotency_key="cross-canary-first")
    second = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    predict.gas_signer = "0xchanged-gas-signer"
    changed = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert first["policy_limits"]["max_normal_cost"] == "5"
    assert final["evidence"][-1]["canary_verified"] is True
    assert second["policy_limits"]["max_normal_cost"] == "20"
    assert changed["policy_limits"]["max_normal_cost"] == "5"
    assert cross.max_normal_cost_requests[:2] == [Decimal("5"), Decimal("5")]
    assert cross.max_normal_cost_requests[-2:] == [Decimal("20"), Decimal("5")]


@pytest.mark.parametrize(
    ("venue", "field"),
    (
        ("predict.fun", "filled_quantity"),
        ("predict.fun", "position_quantity"),
        ("polymarket", "filled_quantity"),
        ("polymarket", "position_quantity"),
    ),
)
def test_cross_canary_requires_each_adapter_quantity_to_equal_the_exact_intent(
    venue: str, field: str,
) -> None:
    reconciled: dict[str, dict[str, object]] = {
        "predict.fun": {
            "verified": True,
            "filled_quantity": Decimal("5"),
            "position_quantity": Decimal("5"),
            "actual_fee": Decimal("0.05"),
            "execution_proof": {
                "verified": True,
                "venue": "predict.fun",
                "order_ids": ["predict-order"],
                "trade_ids": ["predict-trade"],
                "fee": Decimal("0.05"),
            },
        },
        "polymarket": {
            "verified": True,
            "filled_quantity": Decimal("5"),
            "position_quantity": Decimal("5"),
            "actual_fee": Decimal("0.02496"),
            "execution_proof": {
                "verified": True,
                "venue": "polymarket",
                "matched_refs": {
                    "token_id": "poly-no",
                    "order_ids": ["poly-order"],
                    "trade_ids": ["poly-trade"],
                },
                "fee": Decimal("0.02496"),
            },
        },
    }
    reconciled[venue][field] = Decimal("4.9")

    assert PredictionExecutionService._cross_canary_reconciliation_verified(
        reconciled, Decimal("5")
    ) is False


@pytest.mark.parametrize(
    ("name", "configure"),
    [
        (
            "cancellation",
            lambda trading, cross, predict: setattr(
                cross,
                "refresh_intent_resolver",
                _post_approval_breach_resolver(cross),
            ),
        ),
        (
            "both_rejected",
            lambda trading, _cross, predict: (
                trading.submit_results.append(ThresholdLegResult("polymarket", "NO", "poly-condition", "poly-no", False, "rejected", "", Decimal("0"), (), "rejected")),
                predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected")),
                trading.reconcile_results.append({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}),
                predict.reconcile_results.append({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}),
            ),
        ),
        (
            "one_leg_incident",
            lambda _trading, _cross, predict: (
                predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected")),
                predict.reconcile_results.append({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}),
            ),
        ),
        (
            "cleanup_failure",
            lambda _trading, _cross, predict: predict.clear_results.append({"status": "confirmed", "market_id": "predict-market", "allowance": "0.01"}),
        ),
        (
            "partial_reconciliation",
            lambda _trading, _cross, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("4.9"), "actual_fee": Decimal("0.05"), "execution_proof": {"verified": True, "venue": "predict.fun", "order_ids": ["predict-order"], "trade_ids": ["predict-trade"], "fee": Decimal("0.05")}}),
        ),
        (
            "equal_but_below_expected_quantity",
            lambda _trading, _cross, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("4.9"), "position_quantity": Decimal("4.9"), "minimum_order_size": Decimal("1"), "actual_fee": Decimal("0.05"), "execution_proof": {"verified": True, "venue": "predict.fun", "order_ids": ["predict-order"], "trade_ids": ["predict-trade"], "fee": Decimal("0.05")}}),
        ),
        (
            "fee_disagreement",
            lambda _trading, _cross, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("5"), "actual_fee": Decimal("0.04"), "execution_proof": {"verified": True, "venue": "predict.fun", "order_ids": ["predict-order"], "trade_ids": ["predict-trade"], "fee": Decimal("0.05")}}),
        ),
        (
            "missing_fee_proof",
            lambda trading, _cross, predict: (
                setattr(trading, "omit_canary_fee_proof", True),
                setattr(predict, "omit_canary_fee_proof", True),
            ),
        ),
        (
            "polymarket_refs_only_under_predict_key",
            lambda trading, _cross, _predict: trading.reconcile_results.append(
                {
                    "status": "verified",
                    "verified": True,
                    "filled_quantity": Decimal("5"),
                    "position_quantity": Decimal("5"),
                    "minimum_order_size": Decimal("1"),
                    "actual_fee": Decimal("0.05"),
                    "execution_proof": {
                        "verified": True,
                        "venue": "polymarket",
                        "fee": Decimal("0.05"),
                        "matched_refs": {
                            "predict.fun": {
                                "order_ids": ["predict-order"],
                                "trade_ids": ["predict-trade"],
                            }
                        },
                    },
                }
            ),
        ),
        (
            "predict_refs_only_under_polymarket_key",
            lambda _trading, _cross, predict: predict.reconcile_results.append(
                {
                    "status": "verified",
                    "verified": True,
                    "filled_quantity": Decimal("5"),
                    "position_quantity": Decimal("5"),
                    "minimum_order_size": Decimal("1"),
                    "actual_fee": Decimal("0.05"),
                    "execution_proof": {
                        "verified": True,
                        "venue": "predict.fun",
                        "fee": Decimal("0.05"),
                        "matched_refs": {
                            "polymarket": {
                                "order_ids": ["poly-order"],
                                "trade_ids": ["poly-trade"],
                            }
                        },
                    },
                }
            ),
        ),
        (
            "polymarket_direct_refs_with_predict_venue",
            lambda trading, _cross, _predict: trading.reconcile_results.append(
                {
                    "status": "verified",
                    "verified": True,
                    "filled_quantity": Decimal("5"),
                    "position_quantity": Decimal("5"),
                    "minimum_order_size": Decimal("1"),
                    "actual_fee": Decimal("0.05"),
                    "execution_proof": {
                        "verified": True,
                        "venue": "predict.fun",
                        "fee": Decimal("0.05"),
                        "order_ids": ["poly-order"],
                        "trade_ids": ["poly-trade"],
                    },
                }
            ),
        ),
        (
            "predict_direct_refs_with_polymarket_venue",
            lambda _trading, _cross, predict: predict.reconcile_results.append(
                {
                    "status": "verified",
                    "verified": True,
                    "filled_quantity": Decimal("5"),
                    "position_quantity": Decimal("5"),
                    "minimum_order_size": Decimal("1"),
                    "actual_fee": Decimal("0.05"),
                    "execution_proof": {
                        "verified": True,
                        "venue": "polymarket",
                        "fee": Decimal("0.05"),
                        "order_ids": ["predict-order"],
                        "trade_ids": ["predict-trade"],
                    },
                }
            ),
        ),
    ],
)
def test_cross_canary_cap_stays_five_after_non_graduating_outcomes(
    tmp_path: Path,
    name: str,
    configure: object,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    first = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    configure(trading, cross, predict)  # type: ignore[operator]

    _accepted, final = _cross_execution(service, idempotency_key=f"cross-canary-negative-{name}")
    for incident in store.histories("incidents"):
        if incident.get("acknowledged") is not True:
            store.acknowledge_incident(str(incident["incident_id"]), {"operator": "test"})
    service._breaker_open = False
    service._cross_breaker_open = False
    second = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert first["policy_limits"]["max_normal_cost"] == "5"
    assert final["evidence"][-1].get("canary_verified") is not True
    if name == "cleanup_failure":
        assert second == {"state": "rejected", "reason": "residual_predict_allowance"}
    else:
        assert second["policy_limits"]["max_normal_cost"] == "5"


def _post_approval_breach_resolver(cross: CrossVenueMonitor) -> object:
    predict_leg, polymarket_leg = cross.intent.legs
    refreshes = 0

    def resolver(**_: object) -> CrossVenueIntent:
        nonlocal refreshes
        refreshes += 1
        if refreshes < 3:
            return cross.intent
        return replace(
            cross.intent,
            legs=(replace(predict_leg, max_cost=Decimal("2.31")), polymarket_leg),
            total_max_cost=Decimal("4.81"),
            minimum_profit=Decimal("0.19"),
        )

    return resolver


def test_cross_venue_submits_both_legs_concurrently_and_deduplicates_preview(
    tmp_path: Path,
) -> None:
    service, _store, trading, _cross, predict = _cross_service(tmp_path)
    barrier = threading.Barrier(2)
    trading.submit_barrier = predict.submit_barrier = barrier
    trading.block_submit = predict.block_submit = True
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    first = service.confirm(str(preview["preview_id"]), "same-ui-key")
    assert trading.submit_started.wait(timeout=2)
    assert predict.submit_started.wait(timeout=2)
    duplicate = service.confirm(str(preview["preview_id"]), "same-ui-key")
    different_key = service.confirm(str(preview["preview_id"]), "different-ui-key")
    trading.submit_release.set()
    predict.submit_release.set()
    final = wait_until_terminal(service, str(first["execution_id"]))

    assert {first["execution_id"], duplicate["execution_id"], different_key["execution_id"]} == {first["execution_id"]}
    assert (trading.cross_submit_calls, predict.submit_calls) == (1, 1)
    assert len(predict.cross_entry_preflight_orders) == 2
    assert predict.cross_entry_preflight_orders[-1:] == predict.cross_entry_submit_orders
    assert predict.cross_entry_submit_orders[0]["execution_id"] == first["execution_id"]
    assert final["state"] == "holding_to_resolution"


def test_cross_predict_entry_rejection_posts_neither_venue_and_stays_fail_closed(
    tmp_path: Path,
) -> None:
    service, _store, trading, _cross, predict = _cross_service(tmp_path)
    predict.cross_entry_allowed = False

    _accepted, final = _cross_execution(service, idempotency_key="cross-entry-bound-rejected")

    assert final["state"] == "both_rejected"
    assert predict.cross_entry_preflight_orders
    assert predict.cross_entry_submit_orders == []
    assert predict.submit_calls == 0
    assert trading.cross_submit_calls == 0


def test_cross_venue_uses_one_fresh_bounded_completion_only_below_emergency_limit(
    tmp_path: Path,
) -> None:
    service, _store, _trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(replace(predict_leg, max_cost=Decimal("1.90")), polymarket_leg),
        total_max_cost=Decimal("4.40"),
        minimum_profit=Decimal("0.60"),
    )
    predict.submit_results.extend(
        (
            PredictLegResult(False, "rejected", "", "rejected"),
        )
    )
    predict.cross_remediation_options["BUY"] = _fresh_cross_option(
        predict_leg, side="BUY", price=Decimal("0.18"), fee=Decimal("0.10")
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-bounded-completion")

    assert final["state"] == "holding_to_resolution"
    assert predict.submit_calls == 2
    assert final["evidence"][-1]["remediation_worst_case_loss"] == "1.10"
    assert final["evidence"][-1].get("canary_verified") is not True


def test_cross_venue_never_completes_an_opposite_partial_fill(
    tmp_path: Path,
) -> None:
    service, _store, trading, _cross, predict = _cross_service(tmp_path)
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    predict.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    trading.reconcile_results.append(
        {
            "status": "verified",
            "verified": True,
            "filled_quantity": Decimal("4"),
            "position_quantity": Decimal("4"),
            "execution_proof": {"verified": True},
        }
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-partial")

    assert final["state"] == "directional_incident"
    assert predict.submit_calls == 1
    assert service._cross_breaker_open is True


def test_cross_venue_never_completes_above_emergency_limit(
    tmp_path: Path,
) -> None:
    service, _store, _trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(replace(predict_leg, max_cost=Decimal("2.01")), polymarket_leg),
        total_max_cost=Decimal("4.51"),
        minimum_profit=Decimal("0.49"),
    )
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    predict.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-over-limit")

    assert final["state"] == "directional_incident"
    assert predict.submit_calls == 1
    assert service._cross_breaker_open is True


def test_cross_holding_settlement_uses_post_fill_baseline_not_preorder_balance(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(
            replace(predict_leg, requested_quantity=Decimal("10"), net_quantity=Decimal("10")),
            replace(polymarket_leg, requested_quantity=Decimal("10"), net_quantity=Decimal("10")),
        ),
        quantity=Decimal("10"),
        minimum_payout=Decimal("10"),
        minimum_profit=Decimal("5.20"),
    )
    predict.default_filled_quantity = Decimal("10")
    trading.balance = Decimal("100")
    predict.balance = Decimal("100")
    trading.balance_after_cross_submit = Decimal("90")
    predict.balance_after_cross_submit = Decimal("90")

    accepted, holding = _cross_execution(service, idempotency_key="cross-post-fill-baseline")

    assert holding["state"] == "holding_to_resolution"
    assert holding["evidence"][-1]["settlement_baseline"] == {
        "polymarket": "90", "predict.fun": "90",
    }
    predict.positions = (
        {"tokenId": "predict-yes", "amount": "10", "redeemable": True, "outcome": "YES"},
    )
    assert service.reconcile_cross_holdings_once() == {
        "complete": 0, "pending": 1, "unknown": 0,
    }
    predict.positions = ()
    trading.balance = Decimal("100")
    predict.balance = Decimal("100")

    completed = service.reconcile_cross_holdings_once()

    assert completed == {"complete": 1, "pending": 0, "unknown": 0}
    assert service.execution(str(accepted["execution_id"]))["state"] == "complete"
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_holding_settlement_requires_exact_redeemable_net_quantity(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(
            replace(predict_leg, requested_quantity=Decimal("10"), net_quantity=Decimal("10")),
            replace(polymarket_leg, requested_quantity=Decimal("10"), net_quantity=Decimal("10")),
        ),
        quantity=Decimal("10"),
        minimum_payout=Decimal("10"),
        minimum_profit=Decimal("5.20"),
    )
    predict.default_filled_quantity = Decimal("10")
    trading.balance = predict.balance = Decimal("100")
    trading.balance_after_cross_submit = predict.balance_after_cross_submit = Decimal("90")
    accepted, holding = _cross_execution(service, idempotency_key="cross-partial-redemption")

    assert holding["state"] == "holding_to_resolution"
    predict.positions = (
        {"tokenId": "predict-yes", "amount": "5", "redeemable": True, "outcome": "YES"},
    )
    assert service.reconcile_cross_holdings_once() == {
        "complete": 0, "pending": 1, "unknown": 0,
    }
    predict.positions = ()
    predict.balance = Decimal("95")
    trading.balance = Decimal("100")

    assert service.reconcile_cross_holdings_once() == {
        "complete": 0, "pending": 0, "unknown": 1,
    }
    assert service.execution(str(accepted["execution_id"]))["state"] == "holding_to_resolution"
    assert store.cross_unsettled_principal() > Decimal("0")


def test_cross_remediation_prefers_fresh_cheaper_polymarket_unwind_once(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    predict.cross_remediation_options["BUY"] = _fresh_cross_option(
        predict_leg, side="BUY", price=Decimal("0.20"), fee=Decimal("0.10"),
    )
    trading.cross_remediation_options["SELL"] = _fresh_cross_option(
        polymarket_leg, side="SELL", price=Decimal("0.95"), fee=Decimal("0.05"),
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-cheaper-unwind")

    assert final["state"] == "neutralized_incident"
    assert [order["side"] for order in trading.cross_remediation_calls] == ["SELL"]
    assert predict.submit_calls == 1
    assert service._cross_breaker_open is True
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_cross_remediation_completes_from_fresh_bound_option_within_limit(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, _predict = _cross_service(tmp_path)
    _predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(cross.intent, calculable_gas=Decimal("0.10"))
    trading.submit_results.append(
        ThresholdLegResult(
            "polymarket", "NO", "poly-condition", "poly-no", False,
            "rejected", "", Decimal("0"), (), "rejected",
        )
    )
    trading.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    trading.cross_remediation_options["BUY"] = _fresh_cross_option(
        polymarket_leg,
        side="BUY",
        price=Decimal("0.18"),
        fee=Decimal("0.05"),
        residual_dust=Decimal("0.05"),
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-fresh-completion")

    assert final["state"] == "holding_to_resolution"
    assert trading.cross_submit_calls == 2
    assert trading.cross_submitted_legs[-1].max_cost == Decimal("0.95")
    assert final["evidence"][-1]["remediation_worst_case_loss"] == "1.10"
    assert final["evidence"][-1].get("canary_verified") is not True


@pytest.mark.parametrize("option_state", ("stale", "missing", "over_limit"))
def test_cross_remediation_rejects_nonactionable_fresh_options_without_order(
    tmp_path: Path, option_state: str,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(replace(predict_leg, max_cost=Decimal("1.00")), polymarket_leg),
        total_max_cost=Decimal("3.50"),
        minimum_profit=Decimal("1.50"),
    )
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    if option_state == "stale":
        option = _fresh_cross_option(predict_leg, side="BUY", price=Decimal("0.10"))
        option["checked_at"] = datetime.now(UTC) - timedelta(seconds=11)
        predict.cross_remediation_options["BUY"] = option
    elif option_state == "over_limit":
        predict.cross_remediation_options["BUY"] = _fresh_cross_option(
            predict_leg, side="BUY", price=Decimal("0.50"),
        )

    _accepted, final = _cross_execution(service, idempotency_key=f"cross-no-remediation-{option_state}")

    assert final["state"] == "directional_incident"
    assert predict.submit_calls == 1
    assert trading.cross_submit_calls == 1
    assert service._cross_breaker_open is True


def test_cross_holding_reconciliation_releases_once_after_observed_redemption(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    trading.balance = Decimal("20")
    accepted, holding = _cross_execution(service, idempotency_key="cross-redemption")
    assert holding["state"] == "holding_to_resolution"
    predict.positions = (
        {"tokenId": "predict-yes", "amount": "5", "redeemable": True, "outcome": "YES"},
    )

    observed = service.reconcile_cross_holdings_once()
    predict.positions = ()
    predict.balance = Decimal("10")

    first = service.reconcile_cross_holdings_once()
    second = service.reconcile_cross_holdings_once()

    assert observed == {"complete": 0, "pending": 1, "unknown": 0}
    assert first == {"complete": 1, "pending": 0, "unknown": 0}
    assert second == {"complete": 0, "pending": 0, "unknown": 0}
    assert service.execution(str(accepted["execution_id"]))["state"] == "complete"
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_holding_reconciliation_accepts_expired_persisted_cutoff(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    trading.balance = Decimal("20")
    accepted, holding = _cross_execution(service, idempotency_key="cross-expired-cutoff")
    assert holding["state"] == "holding_to_resolution"

    execution_id = str(accepted["execution_id"])
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[0]))
        payload["intent"]["canonical_cutoff"] = "2020-01-01T00:00:00Z"
        connection.execute(
            "UPDATE executions SET payload = ? WHERE execution_id = ?",
            (json.dumps(payload), execution_id),
        )

    predict.positions = (
        {"tokenId": "predict-yes", "amount": "5", "redeemable": True, "outcome": "YES"},
    )
    observed = service.reconcile_cross_holdings_once()
    predict.positions = ()
    predict.balance = Decimal("10")

    released = service.reconcile_cross_holdings_once()

    assert observed == {"complete": 0, "pending": 1, "unknown": 0}
    assert released == {"complete": 1, "pending": 0, "unknown": 0}
    assert service.execution(execution_id)["state"] == "complete"
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_holding_keeps_capacity_when_only_losing_venue_collateral_grows(
    tmp_path: Path,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    trading.balance = Decimal("20")
    accepted, holding = _cross_execution(service, idempotency_key="cross-losing-bump")
    assert holding["state"] == "holding_to_resolution"
    predict.positions = (
        {"tokenId": "predict-yes", "amount": "5", "redeemable": True, "outcome": "YES"},
    )

    observed = service.reconcile_cross_holdings_once()
    predict.positions = ()
    trading.balance = Decimal("25")

    pending = service.reconcile_cross_holdings_once()
    execution = service.execution(str(accepted["execution_id"]))

    assert observed == {"complete": 0, "pending": 1, "unknown": 0}
    assert pending == {"complete": 0, "pending": 0, "unknown": 1}
    assert execution["state"] == "holding_to_resolution"
    assert execution["evidence"][-1]["status_text"] == "待兑付"
    assert store.cross_unsettled_principal() == Decimal("4.80")


def test_cross_reservation_release_failure_becomes_an_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    trading.submit_results.append(
        ThresholdLegResult(
            "polymarket", "NO", "poly-condition", "poly-no", False,
            "rejected", "", Decimal("0"), (), "rejected",
        )
    )
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    trading.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    predict.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    monkeypatch.setattr(
        store,
        "release_cross_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("release failed")),
    )

    _accepted, final = _cross_execution(service, idempotency_key="cross-release-failure")

    assert final["state"] == "directional_incident"
    assert service._cross_breaker_open is True
    assert store.cross_unsettled_principal() > Decimal("0")


@pytest.mark.parametrize(
    ("name", "configure", "state", "released", "breaker", "predict_calls", "dust_loss"),
    [
        ("both_filled", lambda trading, predict: None, "holding_to_resolution", False, False, 1, None),
        (
            "both_rejected",
            lambda trading, predict: (
                trading.submit_results.append(ThresholdLegResult("polymarket", "NO", "poly-condition", "poly-no", False, "rejected", "", Decimal("0"), (), "rejected")),
                predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected")),
                trading.reconcile_results.append({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}),
                predict.reconcile_results.append({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}),
            ),
            "both_rejected", True, False, 1, None,
        ),
        (
            "timeout_found",
            lambda _trading, predict: predict.submit_results.append(PredictLegResult(False, "ambiguous", "predict-order", "ambiguous")),
            "holding_to_resolution", False, False, 1, None,
        ),
        (
            "absent_retry",
            lambda _trading, predict: (
                predict.submit_results.extend((PredictLegResult(False, "ambiguous", "predict-order", "ambiguous"), PredictLegResult(True, "filled", "predict-order"))),
                predict.reconcile_results.extend(({"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}, {"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("5"), "execution_proof": {"verified": True}})),
            ),
            "holding_to_resolution", False, False, 2, None,
        ),
        (
            "unknown",
            lambda _trading, predict: (
                predict.submit_results.append(PredictLegResult(False, "ambiguous", "predict-order", "ambiguous")),
                predict.reconcile_results.append({"status": "unknown", "verified": False, "conclusively_absent": False}),
            ),
            "directional_incident", False, True, 1, None,
        ),
        (
            "position_mismatch",
            lambda _trading, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("4"), "execution_proof": {"verified": True}}),
            "directional_incident", False, True, 1, None,
        ),
        (
            "safe_dust",
            lambda _trading, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("4.9"), "execution_proof": {"verified": True}}),
            "holding_to_resolution", False, False, 1, Decimal("0.10"),
        ),
        (
            "unsafe_dust",
            lambda _trading, predict: predict.reconcile_results.append({"status": "verified", "verified": True, "filled_quantity": Decimal("5"), "position_quantity": Decimal("4.9"), "worst_case_loss": Decimal("2.01"), "execution_proof": {"verified": True}}),
            "directional_incident", False, True, 1, None,
        ),
    ],
)
def test_cross_venue_reconciliation_contains_independent_outcomes(
    tmp_path: Path,
    name: str,
    configure: object,
    state: str,
    released: bool,
    breaker: bool,
    predict_calls: int,
    dust_loss: Decimal | None,
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    configure(trading, predict)  # type: ignore[operator]

    _accepted, final = _cross_execution(service, idempotency_key=f"cross-{name}")

    assert final["state"] == state
    assert predict.submit_calls == predict_calls
    assert trading.cross_submit_calls == 1
    assert service._cross_breaker_open is breaker
    assert (store.cross_unsettled_principal() == Decimal("0")) is released
    if dust_loss is not None:
        evidence = final["evidence"][-1]
        assert evidence["unhedged_units"] == "0.1"
        assert evidence["worst_case_loss"] == "0.1"
        assert evidence["reconciliation"]["predict.fun"]["minimum_order_size"] == "1"
        assert evidence["reconciliation"]["polymarket"]["minimum_order_size"] == "1"
        assert evidence.get("canary_verified") is not True


def test_cross_preview_is_server_owned_without_expires_at_or_countdown(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, _predict = _cross_service(tmp_path)

    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert preview["market_type"] == "cross_venue_yes_no"
    assert preview["signal_episode_id"] == "signal-episode-1"
    assert [leg["exchange"] for leg in preview["buy_legs"]] == ["predict.fun", "polymarket"]
    assert preview["net_quantity"] == "5"
    assert preview["maximum_total_cost"] == "4.80"
    assert preview["minimum_payout"] == "5"
    assert preview["minimum_profit"] == "0.20"
    assert preview["annualized_yield"] >= "0.15"
    assert preview["canonical_cutoff"].endswith("Z")
    assert preview["codex_approval"]["decision"] == "APPROVE"
    assert preview["balances"]["predict.fun"]["asset"] == "USDT"
    assert preview["balances"]["polymarket"]["asset"] == "pUSD"
    assert preview["unsettled"]["limit"] == "100"
    assert preview["policy_limits"]["max_normal_cost"] == "5"
    assert preview["policy_limits"]["max_emergency_loss"] == "2"
    assert "expires_at" not in preview


def test_cross_venue_preview_requires_named_gas_inside_cost_and_profit(
    tmp_path: Path,
) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.intent = replace(
        cross.intent,
        calculable_gas=Decimal("0"),
        total_max_cost=Decimal("4.70"),
        minimum_profit=Decimal("0.30"),
    )

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "rejected",
        "reason": "cross_venue_economics",
    }

    cross.intent = replace(
        cross.intent,
        calculable_gas=Decimal("0.10"),
        total_max_cost=Decimal("4.80"),
        minimum_profit=Decimal("0.20"),
    )
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert preview["state"] == "previewed"
    assert preview["maximum_total_cost"] == "4.80"
    assert preview["minimum_profit"] == "0.20"


def test_cross_venue_execution_mode_is_server_authority_for_preview_and_confirm(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    opportunity_id = "cross:public-pair:PREDICT_YES_POLYMARKET_NO"

    cross.overrides["execution_mode"] = "observe_only"
    rejected = service.preview(opportunity_id)

    assert rejected == {"state": "rejected", "reason": "cross_execution_mode"}
    assert trading.cross_submit_calls == 0
    assert predict.submit_calls == 0

    cross.overrides["execution_mode"] = "manual_confirm"
    preview = service.preview(opportunity_id)
    assert preview["state"] == "previewed"

    # A stale/forged preview cannot authorize a fresh observe-only opportunity.
    cross.overrides["execution_mode"] = "observe_only"
    accepted = service.confirm(str(preview["preview_id"]), "cross-observe-only")
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "both_rejected"
    assert final["evidence"][-1]["reason"] == "cross_execution_mode"
    assert trading.cross_submit_calls == 0
    assert predict.submit_calls == 0


def test_manual_only_cross_venue_rejected_by_auto_eat_but_valid_for_confirm(
    tmp_path: Path,
) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.intent = replace(cross.intent, manual_only=True)
    cross.overrides["manual_only"] = True
    cross.overrides["manual_reason"] = "UNRESOLVED_UNCERTAINTY"
    opportunity_id = "cross:public-pair:PREDICT_YES_POLYMARKET_NO"

    auto = service.preview(opportunity_id, auto_eat=True)
    assert auto == {"state": "rejected", "reason": "manual_only_requires_approval"}

    preview = service.preview(opportunity_id)
    assert preview["state"] == "previewed"

    opportunity = cross._opportunity(cross.intent)
    assert service._validate_cross_venue_opportunity(opportunity, cross.intent) is None

    strict = CrossVenueMonitor(_cross_intent())
    strict.overrides["codex_approval"] = {
        "decision": "REJECT",
        "cache_key": "",
        "direct_outcome_mapping": {},
        "evidence": [],
    }
    assert service._validate_cross_venue_opportunity(
        strict._opportunity(strict.intent), strict.intent
    ) == "codex_not_approved"


def test_auto_submit_cross_venue_runs_once_without_pretrade_notification(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    cross.overrides["execution_mode"] = "auto_submit"
    assert store.arm_cross_auto()["armed"] is True
    signal_id = _cross_venue_notification_signal(store)
    store.update_signal(signal_id, {"execution_mode": "auto_submit"})

    accepted = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "holding_to_resolution"
    assert (predict.submit_calls, trading.cross_submit_calls) == (1, 1)
    assert macos.calls == 0
    assert feishu.calls == 1
    assert store.cross_auto_attempts()[0]["decision"] == "submitted"
    assert service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    ) == {"state": "ignored", "reason": "signal_already_attempted"}
    assert (predict.submit_calls, trading.cross_submit_calls) == (1, 1)


def test_auto_submit_busy_signal_is_rejected_without_queue_or_submitted_record(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    cross.overrides["execution_mode"] = "auto_submit"
    assert store.arm_cross_auto()["armed"] is True
    first_signal = _cross_venue_notification_signal(store)
    store.update_signal(first_signal, {"execution_mode": "auto_submit"})
    second_payload = dict(store.signal(first_signal) or {})
    second_payload.update({"market_id": "public-pair-2", "execution_mode": "auto_submit"})
    second_signal = store.upsert_signal(second_payload)
    predict.block_submit = True

    first = service.auto_submit_cross_venue(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", first_signal
    )
    assert first.get("execution_id")
    assert predict.submit_started.wait(timeout=2)

    second = service.auto_submit_cross_venue(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", second_signal
    )
    assert second["state"] == "rejected"
    assert second["reason"] == "active_execution"
    attempts = {row["signal_id"]: row for row in store.cross_auto_attempts(limit=10)}
    assert attempts[second_signal]["decision"] == "rejected"
    assert attempts[second_signal]["reason_code"] == "active_execution"
    assert trading.cross_submit_calls <= 1
    assert predict.submit_calls == 1

    predict.submit_release.set()
    final = wait_until_terminal(service, str(first["execution_id"]))
    assert final["state"] == "holding_to_resolution"


def test_cross_auto_status_pauses_armed_auto_when_monitor_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    assert store.arm_cross_auto()["armed"] is True
    monkeypatch.setattr(
        cross,
        "snapshot",
        lambda: {"mode": "observe_only", "status": "degraded", "opportunities": []},
    )

    status = service.cross_auto_status()

    assert status["configured_mode"] == "auto_submit"
    assert status["effective_mode"] == "observe_only"
    assert status["armed"] is True


def test_cross_auto_status_keeps_manual_confirm_effective_when_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.set_cross_auto_mode("manual_confirm", "operator_configured")
    monkeypatch.setattr(
        cross,
        "snapshot",
        lambda: {
            "status": "degraded",
            "readiness": {"status": "unavailable"},
            "opportunities": [],
        },
    )
    service._notifier = FakeNotifier()

    status = service.cross_auto_status()

    assert status["configured_mode"] == "manual_confirm"
    assert status["effective_mode"] == "manual_confirm"


@pytest.mark.parametrize(
    ("snapshot", "notification_ready"),
    (
        ({"status": "degraded", "opportunities": []}, True),
        ({"status": "ready", "readiness": {"status": "unavailable"}}, True),
        ({"status": "ready", "readiness": {"status": "ready"}}, False),
    ),
)
def test_cross_auto_status_fails_closed_when_current_readiness_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, object],
    notification_ready: bool,
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.arm_cross_auto()
    monkeypatch.setattr(cross, "snapshot", lambda: snapshot)
    if not notification_ready:
        service._notifier = FakeNotifier()

    assert service.cross_auto_status()["effective_mode"] == "observe_only"


def test_cross_auto_status_fails_closed_when_primary_monitor_readiness_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.arm_cross_auto()
    monkeypatch.setattr(
        cross,
        "snapshot",
        lambda: {"status": "ready", "opportunities": []},
    )

    class DegradedPrimaryMonitor:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "degraded",
                "readiness": {"status": "unavailable"},
            }

    service._monitor = DegradedPrimaryMonitor()

    assert service.cross_auto_status()["effective_mode"] == "observe_only"


def test_cross_auto_status_keeps_armed_auto_when_primary_monitor_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.arm_cross_auto()
    monkeypatch.setattr(
        cross,
        "snapshot",
        lambda: {"status": "ready", "opportunities": []},
    )

    class HealthyPrimaryMonitor:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "healthy",
                "readiness": {
                    "wallet": "ready",
                    "geoblock": "allowed",
                    "relayer": "ready",
                    "balance": "20",
                },
            }

    service._monitor = HealthyPrimaryMonitor()

    assert service.cross_auto_status()["effective_mode"] == "auto_submit"


def test_execution_mode_comes_from_store_when_monitor_snapshot_disagrees(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    cross.overrides["execution_mode"] = "observe_only"

    assert service._configured_cross_execution_mode() == "auto_submit"


def test_pause_before_claim_records_cross_auto_paused_without_order(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    store.pause_cross_auto("operator_paused")
    cross.overrides["execution_mode"] = "auto_submit"
    signal_id = _cross_venue_notification_signal(store)

    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result["reason"] == "cross_auto_paused"
    assert result["facts"]["current"] == "paused"
    assert "cross-auto arm" in result["facts"]["operator_action"]
    assert store.cross_auto_attempts()[0]["reason_code"] == "cross_auto_paused"
    assert (predict.submit_calls, trading.cross_submit_calls) == (0, 0)


@pytest.mark.parametrize("configured_mode", ("manual_confirm", "observe_only"))
def test_nonautomatic_mode_claim_records_complete_configuration_rejection(
    tmp_path: Path, configured_mode: str
) -> None:
    service, store, trading, _cross, predict = _cross_service(tmp_path)
    store.set_cross_auto_mode(configured_mode, "operator_configured")
    signal_id = _cross_venue_notification_signal(store)

    result = service.auto_submit_cross_venue(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result["reason"] == "configured_mode_not_auto_submit"
    assert result["facts"]["current"] == configured_mode
    assert "cross-auto mode auto_submit" in result["facts"]["operator_action"]
    attempt = store.cross_auto_attempts()[0]
    assert attempt["decision"] == "rejected"
    assert attempt["reason_code"] == "configured_mode_not_auto_submit"
    assert attempt["current"] == configured_mode
    assert attempt["operator_action"] == result["facts"]["operator_action"]
    assert (predict.submit_calls, trading.cross_submit_calls) == (0, 0)


def test_pause_after_submission_started_does_not_interrupt_reconciliation(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, predict = _cross_service(tmp_path)
    store.arm_cross_auto()
    cross.overrides["execution_mode"] = "auto_submit"
    signal_id = _cross_venue_notification_signal(store)

    accepted = service.auto_submit_cross_venue(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    assert accepted.get("execution_id")
    store.pause_cross_auto("operator_paused")

    final = wait_until_terminal(service, str(accepted["execution_id"]))
    assert final["state"] == "holding_to_resolution"
    assert predict.submit_calls == 1


def test_auto_submit_safe_dust_emits_residual_event_without_success(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    cross.overrides["execution_mode"] = "auto_submit"
    predict.reconcile_results.append(
        {
            "status": "verified",
            "verified": True,
            "filled_quantity": Decimal("5"),
            "position_quantity": Decimal("4.9"),
            "execution_proof": {"verified": True},
        }
    )
    assert store.arm_cross_auto()["armed"] is True
    signal_id = _cross_venue_notification_signal(store)

    accepted = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "holding_to_resolution"
    titles = [title for title, _message in feishu.messages]
    assert "自动下单残差事件" in titles
    assert "自动下单已完成" not in titles


def test_auto_submit_preview_requires_automatic_authority(tmp_path: Path) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.overrides["execution_mode"] = "auto_submit"

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "rejected",
        "reason": "cross_execution_mode",
    }
    assert service.preview(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", auto_submit=True
    )["auto_submit"] is True


def test_auto_submit_rejection_records_safe_reason_without_orders(tmp_path: Path) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    cross.overrides["execution_mode"] = "auto_submit"
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    signal_id = _cross_venue_notification_signal(store)
    store.update_signal(signal_id, {"execution_mode": "auto_submit"})

    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "cross_auto_paused"
    attempt = store.cross_auto_attempts()[0]
    assert attempt["reason_code"] == "cross_auto_paused"
    assert attempt["reason_zh"]
    assert attempt["operator_action_required"] is True
    assert (predict.submit_calls, trading.cross_submit_calls) == (0, 0)
    status = service.cross_auto_status()
    assert status["configured_mode"] == "auto_submit"
    assert status["effective_mode"] == "observe_only"
    assert status["armed"] is False


def test_auto_submit_terminal_feishu_failure_pauses_future_entries(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    cross.overrides["execution_mode"] = "auto_submit"
    assert store.arm_cross_auto()["armed"] is True
    signal_id = _cross_venue_notification_signal(store)
    store.update_signal(signal_id, {"execution_mode": "auto_submit"})
    feishu.fail = True

    accepted = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "holding_to_resolution"
    assert store.cross_auto_state()["reason"] == "notification_delivery_failed"


def test_auto_submit_incident_feishu_failure_pauses_future_entries(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, predict = _cross_service(tmp_path)
    _macos, feishu = service._notifier._notifiers  # type: ignore[attr-defined]
    cross.overrides["execution_mode"] = "auto_submit"
    predict.submit_results.append(PredictLegResult(False, "rejected", "", "rejected"))
    predict.reconcile_results.append(
        {"status": "absent", "conclusively_absent": True, "position_quantity": Decimal("0")}
    )
    assert store.arm_cross_auto()["armed"] is True
    signal_id = _cross_venue_notification_signal(store)
    feishu.fail = True

    accepted = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "directional_incident"
    assert store.cross_auto_state()["reason"] == "notification_delivery_failed"


def test_auto_submit_rejects_venue_minimum_above_canary_limit(tmp_path: Path) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    cross.overrides["execution_mode"] = "auto_submit"
    cross.intent = replace(
        cross.intent,
        legs=(
            replace(cross.intent.legs[0], max_cost=Decimal("2.60")),
            replace(cross.intent.legs[1], max_cost=Decimal("2.50")),
        ),
        total_max_cost=Decimal("5.20"),
        minimum_payout=Decimal("6"),
        minimum_profit=Decimal("0.80"),
    )
    assert store.arm_cross_auto()["armed"] is True
    signal_id = _cross_venue_notification_signal(store)

    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert result["reason"] == "cross_venue_minimum_exceeds_canary"
    assert result["facts"]["current"] == "5.20"
    assert result["facts"]["limit"] == "5"
    assert (predict.submit_calls, trading.cross_submit_calls) == (0, 0)


@pytest.mark.parametrize(
    "reason",
    (
        "cross_auto_paused",
        "cross_auto_daily_principal_cap",
        "cross_pair_unsettled",
        "active_execution",
        "books_stale",
        "insufficient_bnb",
        "account_insufficient",
        "notification_config_unavailable",
        "manual_only_requires_approval",
        "configured_mode_not_auto_submit",
    ),
)
def test_auto_submit_rejection_codes_have_safe_operator_facts(
    tmp_path: Path, reason: str
) -> None:
    service, _store, _trading, _cross, _predict = _cross_service(tmp_path)

    facts = service._cross_auto_facts(
        reason, signal_id="signal-1", opportunity_id="cross:pair:direction"
    )

    assert facts["reason_code"] == reason
    assert isinstance(facts["reason_zh"], str) and facts["reason_zh"]
    assert facts["venue"]
    assert set(facts) == {
        "reason_code", "reason_zh", "current", "limit", "venue",
        "operator_action_required", "operator_action", "signal_id", "opportunity_id",
    }


@pytest.mark.parametrize(
    "cutoff",
    [
        "2099-12-31 23:59:00Z",
        "2099-12-31T23:59Z",
        "2099-12-31T23:59:00",
        "2099-12-31",
        "2099-12-31T23:59:00+08:00",
        "2020-01-01T00:00:00Z",
    ],
)
def test_cross_venue_preview_rejects_noncanonical_server_cutoff(
    tmp_path: Path, cutoff: str
) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.overrides["canonical_cutoff"] = cutoff

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "rejected",
        "reason": "canonical_cutoff_invalid",
    }


@pytest.mark.parametrize(
    "cutoff",
    [
        "2099-12-31 23:59:00Z",
        "2099-12-31T23:59Z",
        "2099-12-31T23:59:00",
        "2099-12-31",
        "2099-12-31T23:59:00+08:00",
    ],
)
def test_cross_venue_intent_payload_rejects_noncanonical_cutoff(
    cutoff: str,
) -> None:
    payload = PredictionExecutionService._intent_payload(_cross_intent())
    payload["canonical_cutoff"] = cutoff

    assert PredictionExecutionService._intent_from_payload(payload) is None


def test_cross_venue_intent_payload_accepts_expired_exact_cutoff_for_holding() -> None:
    payload = PredictionExecutionService._intent_payload(_cross_intent())
    payload["canonical_cutoff"] = "2020-01-01T00:00:00Z"

    intent = PredictionExecutionService._intent_from_payload(payload)

    assert intent is not None
    assert intent.canonical_cutoff == datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda cross, _trading, _predict: cross.overrides.update({"confirmed_age_seconds": Decimal("11")}), "books_stale"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, legs=(replace(cross.intent.legs[0], book_timestamp=datetime.now(UTC) - timedelta(seconds=11)), cross.intent.legs[1]))), "books_stale"),
        (lambda cross, _trading, _predict: cross.overrides.update({"codex_approval": {"decision": "REJECT"}}), "codex_not_approved"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, canonical_cutoff=None)), "canonical_cutoff_invalid"),
        (lambda cross, _trading, _predict: setattr(cross, "intent", replace(cross.intent, canonical_cutoff=datetime(2020, 1, 1, tzinfo=UTC))), "canonical_cutoff_invalid"),
        (lambda _cross, trading, _predict: setattr(trading, "balance", Decimal("1")), "account_insufficient"),
        (lambda _cross, _trading, predict: setattr(predict, "allowance_breaker", True), "residual_predict_allowance"),
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


def test_cross_venue_preview_accepts_zero_allowance_snapshot_without_allowance_ready(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.allowance = "0"
    predict.scope_ready = True
    predict.gas_ready = True
    predict.allowance_breaker = False

    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")

    assert preview["state"] == "previewed"
    assert preview["balances"]["predict.fun"]["available_balance"] == "5"


def test_fresh_predict_account_snapshot_normalizes_legacy_allowance_ready_fallback(
    tmp_path: Path,
) -> None:
    service, _store, _trading, _cross, predict = _cross_service(tmp_path)
    predict.use_legacy_allowance_ready = True
    predict.allowance_ready = True

    snapshot = service._fresh_predict_account_snapshot()

    assert snapshot is not None
    assert snapshot["scope_ready"] is True
    assert snapshot["gas_ready"] is True
    assert snapshot["allowance_breaker"] is False


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


@pytest.mark.parametrize("elapsed_seconds", (11, 3600))
def test_cross_preview_no_ttl_accepts_same_episode_better_prices_after_elapsed_window(
    tmp_path: Path, elapsed_seconds: int
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    preview_id = str(preview["preview_id"])
    with sqlite3.connect(service._store.path) as connection:  # type: ignore[attr-defined]
        connection.execute(
            "UPDATE previews SET expires_at=? WHERE preview_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=elapsed_seconds)).isoformat(),
                preview_id,
            ),
        )
    predict_leg, polymarket_leg = cross.intent.legs
    cross.intent = replace(
        cross.intent,
        legs=(
            replace(predict_leg, max_price=Decimal("0.44"), max_cost=Decimal("2.25")),
            replace(polymarket_leg, max_price=Decimal("0.46"), max_cost=Decimal("2.35")),
        ),
        total_max_cost=Decimal("4.70"),
        minimum_profit=Decimal("0.30"),
        annualized_yield=Decimal("0.17"),
    )

    accepted = service.confirm(preview_id, f"cross-no-ttl-{elapsed_seconds}")
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert final["state"] == "holding_to_resolution"
    assert trading.cross_submit_calls == 1
    assert predict.submit_calls == 1


def test_cross_venue_confirmation_refreshes_and_releases_with_current_zero_position_proof(
    tmp_path: Path,
) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    assert cross.refresh_calls == 1
    cross.available = False

    accepted = service.confirm(str(preview["preview_id"]), "cross-withdrawn")
    service._threads[str(accepted["execution_id"])].join(timeout=5)
    execution = service.execution(str(accepted["execution_id"]))

    assert execution["state"] == "both_rejected"
    assert execution["evidence"][-1]["reason"] == "opportunity_unavailable"
    assert execution["evidence"][-1]["positions"] == {
        "predict.fun": "0", "polymarket": "0"
    }
    assert store.cross_unsettled_principal() == Decimal("0")
    assert trading.preflight_calls == 0
    assert predict.account_calls >= 2
    assert cross.refresh_calls >= 2


def test_cross_venue_confirmation_retains_reservation_without_current_account_proof(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    cross.available = False
    predict.account_available = False

    accepted = service.confirm(str(preview["preview_id"]), "cross-proof-unavailable")
    service._threads[str(accepted["execution_id"])].join(timeout=5)

    assert service.execution(str(accepted["execution_id"]))["state"] == "directional_incident"
    assert store.cross_unsettled_principal() == Decimal("4.80")
    assert service._cross_breaker_open is True


def test_cross_venue_confirmation_rejects_when_cross_breaker_opens_after_preview(
    tmp_path: Path,
) -> None:
    service, store, _trading, _cross, _predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    service._cross_breaker_open = True

    accepted = service.confirm(str(preview["preview_id"]), "cross-breaker-confirm")
    service._threads[str(accepted["execution_id"])].join(timeout=5)

    execution = service.execution(str(accepted["execution_id"]))
    assert execution["state"] == "both_rejected"
    assert execution["evidence"][-1]["reason"] == "cross_circuit_breaker_open"
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_venue_refresh_rejects_changed_ceilings_without_cached_snapshot(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    changed_predict = replace(
        cross.intent.legs[0], max_price=Decimal("0.46"), max_cost=Decimal("2.35")
    )
    cross.intent = replace(
        cross.intent,
        legs=(changed_predict, cross.intent.legs[1]),
        total_max_cost=Decimal("4.85"),
        minimum_profit=Decimal("0.15"),
    )

    accepted = service.confirm(str(preview["preview_id"]), "cross-ceiling-change")
    service._threads[str(accepted["execution_id"])].join(timeout=5)

    execution = service.execution(str(accepted["execution_id"]))
    assert execution["state"] == "both_rejected"
    assert execution["evidence"][-1]["reason"] == "opportunity_changed"
    assert cross.refresh_calls >= 2
    assert store.cross_unsettled_principal() == Decimal("0")


@pytest.mark.parametrize(
    "mutate",
    (
        lambda opportunity, intent: (
            {**opportunity, "signal_episode_id": "signal-episode-2"},
            intent,
        ),
        lambda opportunity, intent: (
            opportunity,
            replace(
                intent,
                legs=tuple(
                    replace(leg, requested_quantity=Decimal("6"), net_quantity=Decimal("6"))
                    for leg in intent.legs
                ),
                quantity=Decimal("6"),
                minimum_payout=Decimal("6"),
            ),
        ),
        lambda opportunity, intent: (
            opportunity,
            replace(
                intent,
                legs=(
                    replace(intent.legs[0], max_cost=Decimal("2.31")),
                    intent.legs[1],
                ),
                total_max_cost=Decimal("4.81"),
                minimum_profit=Decimal("0.19"),
            ),
        ),
        lambda opportunity, intent: (
            opportunity,
            replace(intent, annualized_yield=Decimal("0.149999")),
        ),
    ),
)
def test_cross_preview_matches_rejects_full_frozen_envelope_changes(
    tmp_path: Path, mutate: object
) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    preview = dict(service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO"))
    preview.setdefault("signal_episode_id", "signal-episode-1")
    live = cross.refresh_opportunity("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    assert live is not None
    live_intent = service._intent_from_opportunity(live)
    assert isinstance(live_intent, CrossVenueIntent)

    mutated_live, mutated_intent = mutate(live, live_intent)  # type: ignore[operator]

    assert not service._cross_preview_matches(preview, mutated_live, mutated_intent)


def test_cross_preview_canary_quantity_requests_smallest_and_freezes_exact_quantity(
    tmp_path: Path,
) -> None:
    service, _store, trading, cross, predict = _cross_service(tmp_path)
    small = cross.intent
    predict_leg, polymarket_leg = small.legs
    large = replace(
        small,
        legs=(
            replace(
                predict_leg,
                requested_quantity=Decimal("10"),
                net_quantity=Decimal("10"),
                max_cost=Decimal("4.60"),
            ),
            replace(
                polymarket_leg,
                requested_quantity=Decimal("10"),
                net_quantity=Decimal("10"),
                max_cost=Decimal("4.80"),
            ),
        ),
        quantity=Decimal("10"),
        total_max_cost=Decimal("9.50"),
        minimum_payout=Decimal("10"),
        minimum_profit=Decimal("0.50"),
        annualized_yield=Decimal("0.16"),
    )
    cross.intent = large
    cross.refresh_intent_resolver = lambda **kwargs: (
        small
        if kwargs.get("target_quantity") == Decimal("5")
        or (
            kwargs.get("target_quantity") is None
            and kwargs.get("max_total_cost") == Decimal("5")
            and kwargs.get("prefer_smallest") is True
        )
        else large
    )

    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    accepted = service.confirm(str(preview["preview_id"]), "cross-canary-quantity")
    final = wait_until_terminal(service, str(accepted["execution_id"]))

    assert preview["net_quantity"] == "5"
    assert cross.refresh_requests[0] == {
        "opportunity_id": "cross:public-pair:PREDICT_YES_POLYMARKET_NO",
        "target_quantity": None,
        "max_total_cost": Decimal("5"),
        "prefer_smallest": True,
    }
    assert cross.refresh_requests[1]["target_quantity"] == Decimal("5")
    assert final["state"] == "holding_to_resolution"
    assert predict.cross_entry_submit_orders[0]["requested_quantity"] == Decimal("5")
    assert trading.cross_submitted_legs[0].net_quantity == Decimal("5")


def test_cross_venue_confirmation_rejects_changed_approved_candidate_ids(
    tmp_path: Path,
) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    preview = service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO")
    cross.overrides["approved_candidates"] = {
        "predict.fun": {"market_id": "predict-market", "condition_id": "predict-condition", "yes_token_id": "predict-changed", "no_token_id": "predict-no", "rules_fingerprint": "predict-fingerprint"},
        "polymarket": {"market_id": "poly-market", "condition_id": "poly-condition", "yes_token_id": "poly-yes", "no_token_id": "poly-no", "rules_fingerprint": "poly-fingerprint"},
    }

    accepted = service.confirm(str(preview["preview_id"]), "cross-candidate-change")
    service._threads[str(accepted["execution_id"])].join(timeout=5)

    execution = service.execution(str(accepted["execution_id"]))
    assert execution["state"] == "both_rejected"
    assert execution["evidence"][-1]["reason"] == "opportunity_changed"
    assert store.cross_unsettled_principal() == Decimal("0")


def test_cross_venue_preview_does_not_fall_back_to_cached_snapshot(tmp_path: Path) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.refresh_opportunity = None  # type: ignore[method-assign]

    assert service.preview("cross:public-pair:PREDICT_YES_POLYMARKET_NO") == {
        "state": "rejected", "reason": "opportunity_unavailable"
    }


@pytest.mark.parametrize(
    ("intent", "approved_candidates"),
    [
        (
            lambda intent: replace(intent, direction="POLYMARKET_YES_PREDICT_NO"),
            None,
        ),
        (
            lambda intent: intent,
            {
                "predict.fun": {"market_id": "predict-market", "condition_id": "predict-condition", "yes_token_id": "predict-no", "no_token_id": "predict-yes", "rules_fingerprint": "predict-fingerprint"},
                "polymarket": {"market_id": "poly-market", "condition_id": "poly-condition", "yes_token_id": "poly-yes", "no_token_id": "poly-no", "rules_fingerprint": "poly-fingerprint"},
            },
        ),
        (lambda intent: intent, {}),
    ],
)
def test_cross_venue_preview_requires_direction_and_approved_candidate_identity(
    tmp_path: Path, intent: object, approved_candidates: object
) -> None:
    service, _store, _trading, cross, _predict = _cross_service(tmp_path)
    cross.intent = intent(cross.intent)  # type: ignore[operator]
    if approved_candidates is not None:
        cross.overrides["approved_candidates"] = approved_candidates

    assert service.preview(
        f"cross:public-pair:{cross.intent.direction}"
    ) == {"state": "rejected", "reason": "cross_venue_identity"}


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
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and trading.threshold_submit_calls == 0:
        time.sleep(0.05)
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


def test_threshold_settlement_notifies_only_auto_eat_executions(
    tmp_path: Path,
) -> None:
    service, trading, store, _ = threshold_execution_fixture(tmp_path)
    store.set_validation_mode("auto")
    signal_id = _notification_signal(store)
    service.auto_eat_threshold("threshold-opp-1", signal_id)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and trading.threshold_submit_calls == 0:
        time.sleep(0.05)
    assert trading.threshold_submit_calls == 1
    _macos, feishu = service.test_notifiers  # type: ignore[attr-defined]
    feishu_messages = feishu.messages
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(
        "结算" in title for title, _ in feishu_messages
    ):
        time.sleep(0.05)
    assert any("验证单" in title for title, _ in feishu_messages)
    assert any(
        "结算" in title and "预计利润" in message
        for title, message in feishu_messages
    )
    assert store.auto_eat_stats()["realized_pnl"] > 0


def test_notify_observation_sends_immediately_dedupes_and_survives_close(
    tmp_path: Path,
) -> None:
    service, trading, store, monitor = threshold_execution_fixture(tmp_path)
    macos, feishu = service.test_notifiers
    signal_id = _notification_signal(store)
    opportunity = monitor.opportunity("threshold-opp-1")
    reserved = store.reserve_notification_attempt(
        signal_id, kind="observation", lease_seconds=0
    )
    store.close_signal(
        "relation-1",
        ended_at=datetime.now(UTC).isoformat(),
        reason="data_unavailable",
    )

    result = service.notify_observation(
        opportunity, signal_id, str(reserved["lease_id"])
    )

    assert result == {"state": "sent", "signal_id": signal_id}
    assert trading.threshold_preflight_calls == 0
    assert trading.threshold_submit_calls == 0
    assert macos.calls == 0
    assert feishu.calls == 1
    assert "【观察提醒】" in feishu.messages[-1][0]
    current = store.signal(signal_id)
    assert current is not None
    assert current["observation_state"] == "sent"
    assert current["notification_state"] == "pending"
    assert service.notify_observation(
        opportunity, signal_id, "stale-lease"
    ) == {"state": "ignored", "reason": "already_sent"}
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
    assert set(inspect.signature(service.preview).parameters) == {
        "opportunity_id", "auto_eat", "auto_submit",
    }
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


def test_reset_breaker_requires_fresh_clean_account_and_acknowledges_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trading, store, _ = incident_fixture(tmp_path, result="unsafe")
    preview = service.preview("opp-1")
    execution = store.consume_preview_and_create_execution(str(preview["id"]), "reset-clean")
    incident_id = store.open_incident(str(execution["execution_id"]), {"state": "directional_incident"})
    original_acknowledge = store.acknowledge_incident
    acknowledgement_calls = 0

    def acknowledge_once(
        target_incident_id: str, payload: Mapping[str, object]
    ) -> None:
        nonlocal acknowledgement_calls
        acknowledgement_calls += 1
        original_acknowledge(target_incident_id, payload)

    monkeypatch.setattr(store, "acknowledge_incident", acknowledge_once)

    result = service.reset_breaker(incident_id)
    repeated = service.reset_breaker(incident_id)

    assert result["state"] == "ready"
    assert result["reason"] == "reset_confirmed"
    assert repeated == result
    assert acknowledgement_calls == 1
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
