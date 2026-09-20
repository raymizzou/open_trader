from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_views import lp_shortlist
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


T = datetime(2026, 9, 18, tzinfo=UTC)


def test_history_batch_failure_keeps_later_batches_and_partial_results(
    tmp_path: Path,
) -> None:
    conditions = tuple(f"condition-{index:03d}" for index in range(100))
    identities = tuple((condition_id, f"token-{index:03d}") for index, condition_id in enumerate(conditions))

    class HistoryExchange:
        def __init__(self) -> None:
            self.history_calls: list[tuple[str, ...]] = []

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = conditions if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": T,
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            token_id = f"token-{int(condition_id[-3:]):03d}"
            return {
                "market_id": f"market-{condition_id[-3:]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": T,
                "fees_checked_at": T,
                "outcomes": {
                    "yes": {"label": "YES", "token_id": token_id}
                },
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": T,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": T,
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.history_calls.append(token_ids)
            if token_ids and token_ids[0] == "token-020":
                raise IncompleteRead(b"partial")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    exchange = HistoryExchange()
    db = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(db, exchange, clock=lambda: T)

    result = service.refresh_price_history()

    assert result["preparation_outcome"] == "failure"
    assert result["target_count"] == 100
    assert result["request_count"] == 5
    assert result["updated_count"] == 80
    assert result["unknown_count"] == 20
    assert len(exchange.history_calls) == 5
    assert {token for batch in exchange.history_calls for token in batch} == {
        token for _, token in identities
    }
    assert exchange.history_calls[-1] == tuple(f"token-{index:03d}" for index in range(80, 100))

    summaries = db.lp_price_history_summaries(identities, now=T)
    assert sum(summary["state"] == "known" for summary in summaries.values()) == 80
    failed = [
        summary
        for (condition_id, token_id), summary in summaries.items()
        if token_id in {f"token-{index:03d}" for index in range(20, 40)}
    ]
    assert len(failed) == 20
    assert all(summary["state"] == "unknown" for summary in failed)
    preparation_items = db.lp_preparation_items()
    assert len(preparation_items) == 20
    assert {item["condition_id"] for item in preparation_items} == set(conditions[20:40])
    assert all(item["state"] == "waiting_retry" for item in preparation_items)

    snapshot = service.refresh_candidates(force=True)
    failed_ids = set(conditions[20:40])
    recommendations = snapshot["candidates"]
    assert recommendations
    assert all(row["condition_id"] not in failed_ids for row in recommendations)
    healthy = next(
        row for row in snapshot["candidates"] if row["condition_id"] == "condition-000"
    )
    assert healthy["market_id"]
    assert all(row["condition_id"] not in failed_ids for row in snapshot["candidates"])
    assert any(
        reason["condition_id"] in failed_ids
        and reason["code"] == "history_summary_unknown"
        for reason in snapshot["funnel"]["reasons"]["base"]
    )


def test_partial_metadata_keeps_successful_markets_screenable(
    tmp_path: Path,
) -> None:
    current = [T]
    include_d = [False]
    catalog_condition_ids = ("condition-a", "condition-b", "condition-c")

    class PartialMetadataExchange:
        def __init__(self) -> None:
            self.metadata_calls: list[tuple[str, ...]] = []
            self.history_calls: list[tuple[str, ...]] = []
            self.initial_metadata_failure = True

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            all_ids = catalog_condition_ids + (("condition-d",) if include_d[0] else ())
            ids = all_ids if condition_ids is None else tuple(condition_ids)
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in ids
                ],
            }

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in requested
                if condition_id != "condition-b"
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.metadata_calls.append(requested)
            if self.initial_metadata_failure:
                self.initial_metadata_failure = False
                return {
                    "state": "partial",
                    "complete": False,
                    "markets": {
                        "condition-a": self._market("condition-a"),
                        "condition-c": self._market("condition-c"),
                    },
                    "failed_ids": {"condition-b": "IncompleteRead"},
                }
            return {
                "state": "known",
                "complete": True,
                "markets": {condition_id: self._market(condition_id) for condition_id in requested},
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.history_calls.append(token_ids)
            if token_ids == ("token-b",):
                raise IncompleteRead(b"partial")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

    exchange = PartialMetadataExchange()
    db = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(db, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["target_count"] == 2
    assert first["request_count"] == 1
    assert first["updated_count"] == 2
    assert exchange.history_calls == [("token-a", "token-c")]
    assert db.lp_price_history_summary("condition-b", "token-b", now=T) is None
    waiting = db.lp_preparation_items()
    assert [item["condition_id"] for item in waiting] == ["condition-b"]
    assert waiting[0]["state"] == "waiting_retry"

    first_snapshot = service.refresh_candidates(force=True)
    assert first_snapshot["complete"] is False
    candidate_rows = {
        row["condition_id"]: row for row in first_snapshot["candidates"]
    }
    assert {"condition-a", "condition-c"} <= candidate_rows.keys()
    assert "condition-b" not in candidate_rows
    # Issue #143: the merged rank one (condition-a wins the token identity
    # tie-break over condition-c) is the current recommendation.
    assert {
        row["condition_id"] for row in first_snapshot["recommendations"]
    } == {"condition-a"}
    assert first_snapshot["missing_metadata_condition_ids"] == ["condition-b"]

    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["request_count"] == 1
    assert exchange.history_calls == [("token-a", "token-c"), ("token-b",)]
    paused = db.lp_preparation_items()
    assert [item["condition_id"] for item in paused] == ["condition-b"]
    assert paused[0]["state"] == "waiting_retry"
    assert paused[0]["paused"] is False
    assert paused[0]["retry_used"] is False
    assert paused[0]["next_retry_at"] == (
        (T + timedelta(seconds=900))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    include_d[0] = True
    current[0] = T + timedelta(hours=1)
    third = service.refresh_price_history()
    assert third["request_count"] == 2
    assert exchange.history_calls[-1] == ("token-d",)
    d_summary = db.lp_price_history_summary("condition-d", "token-d", now=current[0])
    assert d_summary is not None and d_summary["state"] == "known"
    assert exchange.history_calls.count(("token-b",)) == 2


@pytest.mark.parametrize("reader_kind", ("batch", "direct"))
@pytest.mark.parametrize("with_healthy_a", (True, False))
def test_nonaccepting_market_finishes_unspent_history_retry(
    tmp_path: Path, reader_kind: str, with_healthy_a: bool
) -> None:
    current = [T]
    metadata_closed = [False]
    metadata_calls: list[tuple[str, tuple[str, ...]]] = []
    history_calls: list[tuple[str, ...]] = []
    catalog_ids = ("condition-a", "condition-b") if with_healthy_a else ("condition-b",)

    def market(condition_id: str) -> dict[str, object]:
        return {
            "market_id": f"market-{condition_id[-1]}",
            "condition_id": condition_id,
            "accepting_orders": not (
                condition_id == "condition-b" and metadata_closed[0]
            ),
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("20"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": current[0],
            "fees_checked_at": current[0],
            "outcomes": {
                "yes": {"label": "YES", "token_id": f"token-{condition_id[-1]}"}
            },
        }

    class Exchange:
        def __init__(self) -> None:
            if reader_kind == "batch":
                self.lp_market_metadata_batch = self._metadata_batch
            else:
                self.lp_market_metadata = self._metadata_direct

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in catalog_ids
                ],
            }

        def _metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = tuple(requested)
            metadata_calls.append(("batch", requested))
            return {
                "state": "known",
                "complete": True,
                "markets": {
                    condition_id: market(condition_id) for condition_id in requested
                },
            }

        def _metadata_direct(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            requested = tuple(requested)
            metadata_calls.append(("direct", requested))
            return {
                condition_id: market(condition_id) for condition_id in requested
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(tuple(token_ids))
            history = {}
            if "token-a" in token_ids:
                history["token-a"] = [
                    {"t": start_ts, "p": Decimal("0.500")},
                    {"t": end_ts, "p": Decimal("0.505")},
                ]
            errors = {"token-b": "IncompleteRead"} if "token-b" in token_ids else {}
            return {
                "state": "partial",
                "history": history,
                "errors": errors,
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, Exchange(), clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    assert first["preparation"]["state"] == "partial"
    expected_history_request = ("token-a", "token-b") if with_healthy_a else ("token-b",)
    assert history_calls == [expected_history_request]
    assert first["target_count"] == (2 if with_healthy_a else 1)
    assert first["updated_count"] == (1 if with_healthy_a else 0)
    assert first["unknown_count"] == 1
    assert first["request_count"] == 1
    assert [kind for kind, _ in metadata_calls] == [reader_kind]
    assert metadata_calls[0][1] == catalog_ids
    waiting = store.lp_preparation_items()
    assert len(waiting) == 1
    assert waiting[0]["condition_id"] == "condition-b"
    assert waiting[0]["stage"] == "history"
    assert waiting[0]["state"] == "waiting_retry"
    assert waiting[0]["retry_used"] is False
    assert waiting[0]["paused"] is False
    assert waiting[0]["failed_at"] == T.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert waiting[0]["next_retry_at"] == (
        (T + timedelta(seconds=300))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    a_before = (
        store.lp_price_history_summary("condition-a", "token-a", now=T)
        if with_healthy_a
        else None
    )
    if with_healthy_a:
        assert a_before is not None and a_before["state"] == "known"
    b_before = store.lp_price_history_summary("condition-b", "token-b", now=T)
    assert b_before is not None
    metadata_closed[0] = True
    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "success"
    assert second["preparation"]["state"] == "ready"
    assert second["preparation"]["next_retry_at"] is None
    assert second.get("alert_pending") is not True
    assert store.lp_preparation_items() == []
    assert history_calls == [expected_history_request]
    assert [kind for kind, _ in metadata_calls] == [reader_kind, reader_kind]
    assert all(requested == catalog_ids for _, requested in metadata_calls)
    b_after = store.lp_price_history_summary("condition-b", "token-b", now=current[0])
    assert b_after == b_before
    if with_healthy_a:
        a_after = store.lp_price_history_summary(
            "condition-a", "token-a", now=current[0]
        )
        assert a_after is not None
        assert a_after == a_before

    current[0] = T + timedelta(hours=1)
    wake = service.refresh_price_history()
    assert wake["preparation_outcome"] == "success"
    assert wake["preparation"]["state"] == "ready"
    assert wake.get("alert_pending") is not True
    assert store.lp_preparation_items() == []
    assert history_calls == [expected_history_request]
    assert [kind for kind, _ in metadata_calls] == [reader_kind] * 3


def test_metadata_retry_with_valid_history_cache_finishes_budget(
    tmp_path: Path,
) -> None:
    current = [T]
    metadata_failed = [True]
    metadata_calls: list[tuple[str, ...]] = []
    history_calls: list[tuple[str, ...]] = []

    def market() -> dict[str, object]:
        return {
            "market_id": "market-b",
            "condition_id": "condition-b",
            "accepting_orders": True,
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("20"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": current[0],
            "fees_checked_at": current[0],
            "outcomes": {"yes": {"label": "YES", "token_id": "token-b"}},
        }

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = ("condition-b",) if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = tuple(requested)
            metadata_calls.append(requested)
            if metadata_failed[0]:
                metadata_failed[0] = False
                return {
                    "state": "known",
                    "markets": {},
                    "failed_ids": {"condition-b": "IncompleteRead"},
                }
            return {"state": "known", "markets": {"condition-b": market()}}

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {condition_id: market() for condition_id in requested}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            history_calls.append(tuple(token_ids))
            raise AssertionError("valid cached history must finish metadata retry")

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": "condition-b",
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

    samples = [
        {"t": int((T - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
        {"t": int(T.timestamp()), "p": Decimal("0.505")},
    ]
    store = PredictionArbitrageStore(tmp_path / "data")
    store.lp_save_price_history(
        "condition-b",
        "token-b",
        samples,
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": T,
            "window_start": T - timedelta(hours=24),
            "window_end": T,
            "sample_count": 2,
            "valid_until": T + timedelta(hours=24),
        },
    )
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "waiting_retry"
    assert first["preparation"]["waiting_market_count"] == 1

    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "success"
    assert second["preparation"]["state"] == "ready"
    assert history_calls == []
    assert metadata_calls == [("condition-b",), ("condition-b",)]
    assert store.lp_preparation_items() == []
    cached = store.lp_price_history_summary("condition-b", "token-b", now=current[0])
    assert cached is not None
    assert cached["checked_at"] == T.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    restored = store.lp_price_history_samples("condition-b", "token-b")
    assert [row["t"] for row in restored] == [row["t"] for row in samples]
    assert [Decimal(str(row["p"])) for row in restored] == [
        row["p"] for row in samples
    ]

    snapshot = service.refresh_candidates(force=True)
    assert any(
        row.get("condition_id") == "condition-b"
        for row in snapshot["candidates"]
    )

    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), exchange, clock=lambda: current[0]
    )
    third = restarted.refresh_price_history()
    assert third["preparation_outcome"] == "success"
    assert history_calls == []
    assert restarted.store.lp_preparation_items() == []


def test_single_absent_metadata_retry_finishes_budget(tmp_path: Path) -> None:
    current = [T]
    metadata_failed = [True]
    metadata_calls: list[tuple[str, ...]] = []
    history_calls: list[tuple[str, ...]] = []

    class Exchange:
        @staticmethod
        def lp_reward_catalog(
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = ("condition-b",) if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        @staticmethod
        def lp_market_metadata_batch(
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_calls.append(tuple(requested))
            if metadata_failed[0]:
                metadata_failed[0] = False
                return {
                    "state": "known",
                    "markets": {},
                    "failed_ids": {"condition-b": "IncompleteRead"},
                }
            return {
                "state": "known",
                "markets": {},
                "confirmed_absent_ids": ["condition-b"],
            }

        @staticmethod
        def lp_price_history(
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            history_calls.append(token_ids)
            raise AssertionError("confirmed-absent metadata must not request history")

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, Exchange(), clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "waiting_retry"
    assert first["preparation"]["waiting_market_count"] == 1
    assert metadata_calls == [("condition-b",)]

    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "success"
    assert second["preparation"]["state"] == "ready"
    assert second.get("alert_pending") is not True
    assert second["preparation"]["retrying_market_count"] == 0
    assert second["preparation"]["paused_market_count"] == 0
    assert metadata_calls == [("condition-b",), ("condition-b",)]
    assert history_calls == []
    assert store.lp_preparation_items() == []

    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"),
        Exchange(),
        clock=lambda: current[0] + timedelta(hours=1),
    )
    wake = restarted.refresh_price_history()
    assert wake["preparation_outcome"] == "success"
    assert wake.get("alert_pending") is not True
    assert restarted.preparation_snapshot()["paused_market_count"] == 0
    assert restarted.store.lp_preparation_items() == []
    assert history_calls == []


def test_catalog_failure_does_not_spend_market_retry(tmp_path: Path) -> None:
    def run_case(case_name: str) -> None:
        current = [T]
        phase = ["initial"]
        metadata_failed = [True]
        catalog_calls: list[tuple[str, ...] | None] = []
        metadata_calls: list[tuple[str, ...]] = []
        history_calls: list[tuple[str, ...]] = []

        def market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": {
                    "yes": {"label": "YES", "token_id": f"token-{condition_id[-1]}"}
                },
            }

        class Exchange:
            def lp_reward_catalog(
                self,
                *,
                condition_ids: tuple[str, ...] | None = None,
                stop_event: object = None,
            ) -> dict[str, object]:
                del stop_event
                catalog_calls.append(condition_ids)
                if phase[0] == "catalog-failure":
                    raise TimeoutError("catalog unavailable")
                if phase[0] in {"omit-b", "partial-omit-b"}:
                    requested = ("condition-a",) if condition_ids is None else condition_ids
                else:
                    requested = (
                        ("condition-a", "condition-b")
                        if condition_ids is None
                        else condition_ids
                    )
                return {
                    "state": "known",
                    "complete": phase[0] != "partial-omit-b",
                    "error_type": "IncompleteRead"
                    if phase[0] == "partial-omit-b"
                    else None,
                    "checked_at": current[0],
                    "markets": [
                        {
                            "condition_id": condition_id,
                            "daily_pool_usd": Decimal("100"),
                            "reward_active": True,
                            "rewards_min_size": Decimal("20"),
                            "rewards_max_spread": Decimal("10"),
                        }
                        for condition_id in requested
                    ],
                }

            def lp_market_metadata_batch(
                self,
                requested: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, object]:
                del stop_event
                requested = tuple(requested)
                metadata_calls.append(requested)
                if metadata_failed[0]:
                    metadata_failed[0] = False
                    return {
                        "state": "known",
                        "markets": {
                            condition_id: market(condition_id)
                            for condition_id in requested
                            if condition_id != "condition-b"
                        },
                        "failed_ids": {"condition-b": "IncompleteRead"},
                    }
                return {
                    "state": "known",
                    "markets": {
                        condition_id: market(condition_id) for condition_id in requested
                    },
                }

            def lp_market_metadata(
                self,
                requested: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, dict[str, object]]:
                del stop_event
                return {condition_id: market(condition_id) for condition_id in requested}

            def lp_price_history(
                self,
                token_ids: tuple[str, ...],
                *,
                start_ts: int,
                end_ts: int,
                fidelity: int,
                stop_event: object = None,
            ) -> dict[str, object]:
                del fidelity, stop_event
                history_calls.append(tuple(token_ids))
                if "token-b" in token_ids:
                    raise IncompleteRead(b"retry failure")
                return {
                    "state": "known",
                    "history": {
                        token_id: [
                            {"t": start_ts, "p": Decimal("0.500")},
                            {"t": end_ts, "p": Decimal("0.505")},
                        ]
                        for token_id in token_ids
                    },
                }

            def lp_account_snapshot(self) -> dict[str, object]:
                return {
                    "authenticated": True,
                    "balance": Decimal("10000"),
                    "allowance": Decimal("10000"),
                    "open_orders": [],
                    "positions": [],
                    "checked_at": current[0],
                    "open_orders_complete": True,
                    "positions_complete": True,
                }

            def lp_order_books(
                self,
                token_ids: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, dict[str, object]]:
                del stop_event
                return {
                    token_id: {
                        "condition_id": f"condition-{token_id[-1]}",
                        "token_id": token_id,
                        "received_at": current[0],
                        "bids": [{"price": Decimal("0.50"), "size": Decimal("20")}],
                        "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                    }
                    for token_id in token_ids
                }

        store = PredictionArbitrageStore(tmp_path / case_name)
        retained_summary = None
        if case_name == "catalog-omits-b":
            store.lp_save_price_history(
                "condition-b",
                "token-b",
                [
                    {
                        "t": int((T - timedelta(hours=24)).timestamp()),
                        "p": Decimal("0.500"),
                    },
                    {"t": int(T.timestamp()), "p": Decimal("0.505")},
                ],
                {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T,
                    "window_start": T - timedelta(hours=24),
                    "window_end": T,
                    "sample_count": 2,
                    "valid_until": T + timedelta(hours=24),
                },
            )
            retained_summary = store.lp_price_history_summary(
                "condition-b", "token-b", now=T
            )
            assert retained_summary is not None
        exchange = Exchange()
        service = PolymarketLPService(store, exchange, clock=lambda: current[0])

        first = service.refresh_price_history()
        assert first["preparation_outcome"] in {"failure", "waiting_retry"}
        first_item = next(
            item for item in store.lp_preparation_items() if item["condition_id"] == "condition-b"
        )
        assert first_item["state"] == "waiting_retry"
        assert first_item["retry_used"] is False
        first_retry_at = datetime.fromisoformat(
            str(first_item["next_retry_at"]).replace("Z", "+00:00")
        )
        assert first_retry_at == T + timedelta(seconds=300)

        current[0] = T + timedelta(seconds=300)
        phase[0] = {
            "catalog-failure": "catalog-failure",
            "catalog-omits-b": "omit-b",
            "partial-omits-b": "partial-omit-b",
        }[case_name]
        blocked = service.refresh_price_history()
        assert blocked["preparation_outcome"] in {
            "failure",
            "success",
            "waiting_retry",
        }
        if case_name == "partial-omits-b":
            assert blocked["preparation"]["state"] in {"partial", "waiting_retry"}
        assert not any("token-b" in call for call in history_calls)
        if case_name == "catalog-omits-b":
            assert blocked["target_count"] == 1
            assert blocked.get("alert_pending") is not True
            assert any("token-a" in call for call in history_calls)
            assert all(
                "condition-b" not in call for call in metadata_calls[1:]
            )
            assert store.lp_preparation_items() == []
            assert store.lp_price_history_summary(
                "condition-b", "token-b", now=current[0]
            ) == retained_summary
            current[0] = T + timedelta(hours=1)
            later = service.refresh_price_history()
            assert later["target_count"] == 1
            assert store.lp_preparation_items() == []
            assert all(
                "condition-b" not in call for call in metadata_calls[1:]
            )
            assert not any("token-b" in call for call in history_calls)
            return

        blocked_item = next(
            item for item in store.lp_preparation_items() if item["condition_id"] == "condition-b"
        )
        assert blocked_item["state"] == "waiting_retry"
        assert blocked_item["retry_used"] is False
        assert datetime.fromisoformat(
            str(blocked_item["next_retry_at"]).replace("Z", "+00:00")
        ) == first_retry_at

        phase[0] = "normal"
        current[0] = T + timedelta(seconds=301)
        retried = service.refresh_price_history()
        assert retried["preparation_outcome"] in {"failure", "success"}
        assert history_calls.count(("token-b",)) == 1
        waiting_item = next(
            item for item in store.lp_preparation_items() if item["condition_id"] == "condition-b"
        )
        assert waiting_item["state"] == "waiting_retry"
        assert waiting_item["paused"] is False
        assert waiting_item["retry_used"] is False

        restarted = PolymarketLPService(
            PredictionArbitrageStore(tmp_path / case_name), exchange, clock=lambda: current[0]
        )
        restarted.refresh_price_history()
        assert history_calls.count(("token-b",)) == 1

    run_case("catalog-failure")
    run_case("catalog-omits-b")
    run_case("partial-omits-b")


def test_waiting_history_retry_dispatches_during_later_backfill(
    tmp_path: Path,
) -> None:
    """A due history retry joins the next free bounded group during a long pass."""

    current = [T]
    catalog_ids = ["condition-b"]
    history_calls: list[tuple[str, ...]] = []
    b_retry_times: list[datetime] = []

    def market(condition_id: str) -> dict[str, object]:
        token_id = "token-b" if condition_id == "condition-b" else condition_id.replace(
            "condition-", "token-"
        )
        return {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "accepting_orders": True,
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("20"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": current[0],
            "fees_checked_at": current[0],
            "outcomes": {"yes": {"label": "YES", "token_id": token_id}},
        }

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in catalog_ids
                ],
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del requested, stop_event
            return {condition_id: market(condition_id) for condition_id in catalog_ids}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            if "token-a-040" in token_ids:
                current[0] = T + timedelta(seconds=300)
            if "token-b" in token_ids:
                if len(history_calls) > 1:
                    b_retry_times.append(current[0])
                raise IncompleteRead(b"retry failure")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    assert history_calls == [("token-b",)]
    waiting = store.lp_preparation_items()
    assert len(waiting) == 1
    assert waiting[0]["condition_id"] == "condition-b"
    assert waiting[0]["state"] == "waiting_retry"

    catalog_ids.extend(f"condition-a-{index:03d}" for index in range(161))
    current[0] = T + timedelta(seconds=299)
    result = service.refresh_price_history()
    assert result["preparation_outcome"] == "waiting_retry"
    assert history_calls == [("token-b",)]

    current[0] = T + timedelta(seconds=300)
    result = service.refresh_price_history()
    assert result["preparation_outcome"] == "failure"
    assert result["target_count"] == 162
    assert result["preparation"]["total_count"] == 162
    assert result["preparation"]["completed_count"] == 162
    assert result["unknown_count"] == 1
    assert history_calls.count(("token-b",)) == 2
    assert b_retry_times == [T + timedelta(seconds=300)]
    retry_index = history_calls.index(("token-b",), 1)
    final_a_index = next(
        index
        for index, batch in enumerate(history_calls)
        if "token-a-160" in batch
    )
    assert retry_index < final_a_index
    paused = store.lp_preparation_items()
    assert len(paused) == 1
    assert paused[0]["condition_id"] == "condition-b"
    assert paused[0]["state"] == "waiting_retry"
    assert paused[0]["paused"] is False
    assert paused[0]["retry_used"] is False


def test_preparation_can_publish_valid_results_before_slow_batch_finishes(
    tmp_path: Path,
) -> None:
    history_started = threading.Event()
    release_history = threading.Event()

    def reward(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "daily_pool_usd": Decimal("100"),
            "reward_active": True,
            "rewards_min_size": Decimal("20"),
            "rewards_max_spread": Decimal("10"),
            "checked_at": T,
        }

    def market(condition_id: str, token_id: str) -> dict[str, object]:
        return {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "accepting_orders": True,
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("1"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": T,
            "fees_checked_at": T,
            "outcomes": {"yes": {"label": "YES", "token_id": token_id}},
        }

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            ids = condition_ids or ("condition-ready", "condition-waiting")
            return {
                "state": "known",
                "complete": True,
                "checked_at": T,
                "markets": tuple(reward(condition_id) for condition_id in ids),
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            token_by_condition = {
                "condition-ready": "token-ready",
                "condition-waiting": "token-waiting",
            }
            return {
                condition_id: market(condition_id, token_by_condition[condition_id])
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_started.set()
            assert release_history.wait(timeout=5)
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("1000"),
                "allowance": Decimal("1000"),
                "open_orders": [],
                "positions": [],
                "checked_at": T,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                token_id: {
                    "condition_id": (
                        "condition-ready"
                        if token_id == "token-ready"
                        else "condition-waiting"
                    ),
                    "token_id": token_id,
                    "received_at": T,
                    # Exit-liquidity rule: depth beyond the top bid level.
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                }
                for token_id in token_ids
            }

    db = PredictionArbitrageStore(tmp_path / "data")
    checked_at = T - timedelta(minutes=5)
    db.lp_save_price_history(
        "condition-ready",
        "token-ready",
        [],
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": checked_at,
            "valid_until": T + timedelta(hours=1),
            "window_start": checked_at - timedelta(hours=24),
            "window_end": checked_at,
            "sample_count": 1440,
        },
    )
    service = PolymarketLPService(db, Exchange(), clock=lambda: T)
    refresh_result: dict[str, object] = {}

    def refresh() -> None:
        refresh_result.update(service.refresh_price_history())

    worker = threading.Thread(target=refresh)
    worker.start()
    assert history_started.wait(timeout=2)
    preparation = service.preparation_snapshot()
    assert preparation["completed_count"] == 1
    assert preparation["total_count"] == 2

    snapshot = service.refresh_candidates(force=True)
    ready = next(
        row for row in snapshot["candidates"] if row["condition_id"] == "condition-ready"
    )
    assert ready["market_id"]
    assert any(
        row["condition_id"] == "condition-waiting"
        and row["code"] == "history_summary_unknown"
        for row in snapshot["funnel"]["reasons"]["base"]
    )

    release_history.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert refresh_result["preparation_outcome"] == "success"

    retry_clock = [T]
    retry_conditions = tuple(f"condition-{index:03d}" for index in range(161))

    class RetryExchange:
        def __init__(self) -> None:
            self.history_calls: list[tuple[str, ...]] = []
            self.failed_first_batch = False
            self.final_wait_timed_out = False
            self.retry_seen = threading.Event()

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = retry_conditions if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": retry_clock[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: {
                    "market_id": f"market-{condition_id[-3:]}",
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{int(condition_id[-3:]):03d}",
                        }
                    },
                }
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.history_calls.append(token_ids)
            first_token = token_ids[0]
            if first_token == "token-000" and not self.failed_first_batch:
                self.failed_first_batch = True
                raise IncompleteRead(b"partial")
            if first_token == "token-080":
                assert self.failed_first_batch
                retry_clock[0] = T + timedelta(seconds=300)
            if first_token == "token-000" and self.failed_first_batch:
                self.retry_seen.set()
            if first_token == "token-160":
                if not self.retry_seen.wait(timeout=2):
                    self.final_wait_timed_out = True
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    retry_exchange = RetryExchange()
    retry_store = PredictionArbitrageStore(tmp_path / "retry")
    retry_service = PolymarketLPService(
        retry_store,
        retry_exchange,
        clock=lambda: retry_clock[0],
    )
    retry_result = retry_service.refresh_price_history()

    assert retry_result["preparation_outcome"] == "success"
    assert retry_result["target_count"] == 161
    assert retry_result["request_count"] == 10
    assert retry_result["unknown_count"] == 0
    assert retry_exchange.final_wait_timed_out is False
    assert retry_exchange.history_calls.count(
        tuple(f"token-{index:03d}" for index in range(20))
    ) == 2
    assert retry_exchange.history_calls.index(
        tuple(f"token-{index:03d}" for index in range(20))
    ) < retry_exchange.history_calls.index(
        ("token-160",)
    )
    assert retry_store.lp_preparation_items() == []


def test_partial_retry_pauses_only_failed_items_across_restart(tmp_path: Path) -> None:
    current = [T]
    include_d = [False]
    allow_b_success = [False]
    stale_block = [False]
    stale_started = threading.Event()
    stale_release = threading.Event()
    history_calls: list[tuple[str, ...]] = []

    class Exchange:
        def __init__(self, history_log: list[tuple[str, ...]] | None = None) -> None:
            self.history_log = history_calls if history_log is None else history_log

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            catalog_ids = ("condition-a", "condition-b", "condition-c")
            if include_d[0]:
                catalog_ids += ("condition-d",)
            requested = catalog_ids if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        @staticmethod
        def _market(condition_id: str, checked_at: datetime) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": checked_at,
                "fees_checked_at": checked_at,
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id, current[0])
                for condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.history_log.append(token_ids)
            if stale_block[0] and token_ids != ("token-b",):
                stale_started.set()
                assert stale_release.wait(timeout=3)
            errors = (
                {"token-b": "IncompleteRead"}
                if "token-b" in token_ids and not allow_b_success[0]
                else {}
            )
            return {
                "state": "partial" if errors else "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                    if token_id != "token-b" or allow_b_success[0]
                },
                "errors": errors,
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, Exchange(), clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    assert first["preparation"]["state"] == "partial"
    assert history_calls == [("token-a", "token-b", "token-c")]
    waiting = store.lp_preparation_items()
    assert [item["condition_id"] for item in waiting] == ["condition-b"]
    assert waiting[0]["paused"] is False
    a_before = store.lp_price_history_summary("condition-a", "token-a", now=T)
    c_before = store.lp_price_history_summary("condition-c", "token-c", now=T)
    assert a_before is not None and c_before is not None

    # Issue #143: with the facts still fresh, the scan publishes the healthy
    # markets while condition-b waits for its history retry.
    screenable = service.refresh_candidates(force=True)
    screenable_ids = {row["condition_id"] for row in screenable["candidates"]}
    assert {"condition-a", "condition-c"} <= screenable_ids
    assert "condition-b" not in screenable_ids

    include_d[0] = True
    current[0] = T + timedelta(seconds=299)
    before_due = service.refresh_price_history()
    assert before_due["preparation_outcome"] in {"success", "failure", "waiting_retry"}
    assert before_due["preparation_outcome"] == "waiting_retry"
    assert history_calls == [("token-a", "token-b", "token-c")]

    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "failure"
    assert history_calls == [
        ("token-a", "token-b", "token-c"),
        ("token-b",),
        ("token-d",),
    ]
    paused = store.lp_preparation_items()
    assert [item["condition_id"] for item in paused] == ["condition-b"]
    assert paused[0]["state"] == "waiting_retry"
    assert paused[0]["paused"] is False
    assert paused[0]["retry_used"] is False
    assert store.lp_price_history_summary("condition-d", "token-d", now=current[0]) is not None
    assert store.lp_price_history_summary("condition-a", "token-a", now=current[0]) == a_before
    assert store.lp_price_history_summary("condition-c", "token-c", now=current[0]) == c_before

    current[0] = T + timedelta(hours=23)
    candidate = service.refresh_candidates(force=True)
    # Issue #143 repair 2: after 23 hours the cached metadata is far outside
    # the 60-second candidate freshness window, so the batch renews the
    # expired shared facts once (targeted) before qualifying: the queue is
    # consumed, every market is judged live, and all three passers publish.
    assert candidate["candidates"] != []
    assert candidate["funnel"]["checked"] == 3
    assert candidate["funnel"]["passed"] == 3
    assert candidate["funnel"]["unknown"] == 0
    # Paused condition-b (no history summary) never queues; the three
    # healthy markets a, c, and d are judged live and publish.
    assert {row["condition_id"] for row in candidate["candidates"]} == {
        "condition-a",
        "condition-c",
        "condition-d",
    }
    summaries = store.lp_price_history_summaries(
        (("condition-a", "token-a"), ("condition-c", "token-c")),
        now=current[0],
    )
    directions = [
        {
            "market": {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "outcome": "YES",
            },
            "daily_pool_usd": Decimal("100"),
            "reward_active": True,
            "history_summary": summary,
        }
        for (condition_id, _token_id), summary in summaries.items()
    ]
    assert {row["condition_id"] for row in lp_shortlist(directions, now=current[0])} == {
        "condition-a",
        "condition-c",
    }

    current[0] = T + timedelta(hours=25)
    expired_summaries = store.lp_price_history_summaries(
        (("condition-a", "token-a"), ("condition-c", "token-c")),
        now=current[0],
    )
    expired_directions = [
        {
            "market": {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "outcome": "YES",
            },
            "daily_pool_usd": Decimal("100"),
            "reward_active": True,
            "history_summary": summary,
        }
        for (condition_id, _token_id), summary in expired_summaries.items()
    ]
    assert lp_shortlist(expired_directions, now=current[0]) == []
    expired_snapshot = service.candidate_snapshot()
    assert expired_snapshot["recommendations"] == []
    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), Exchange(), clock=lambda: current[0]
    )
    restarted_result = restarted.refresh_price_history()
    assert restarted_result["preparation_outcome"] in {"failure", "waiting_retry", "success"}
    assert history_calls[-1] == ("token-a", "token-c", "token-d")
    assert "token-b" not in history_calls[-1]
    assert restarted.store.lp_price_history_summary(
        "condition-a", "token-a", now=current[0]
    ) is not None
    a_after_restart = restarted.store.lp_price_history_summary(
        "condition-a", "token-a", now=current[0]
    )
    c_after_restart = restarted.store.lp_price_history_summary(
        "condition-c", "token-c", now=current[0]
    )
    assert a_after_restart is not None and c_after_restart is not None

    allow_b_success[0] = True
    waiting_b = restarted.store.lp_preparation_items()
    assert [item["condition_id"] for item in waiting_b] == ["condition-b"]
    retry_at = datetime.fromisoformat(
        str(waiting_b[0]["next_retry_at"]).replace("Z", "+00:00")
    )
    current[0] = max(current[0], retry_at)
    recovered_result = restarted.refresh_price_history()
    assert recovered_result["preparation_outcome"] == "success"
    assert history_calls[-1] == ("token-b",)
    assert restarted.store.lp_price_history_summary(
        "condition-b", "token-b", now=current[0]
    ) is not None
    assert restarted.store.lp_price_history_summary(
        "condition-a", "token-a", now=current[0]
    ) == a_after_restart
    assert restarted.store.lp_price_history_summary(
        "condition-c", "token-c", now=current[0]
    ) == c_after_restart

    stale_history_calls: list[tuple[str, ...]] = []
    stale_store = PredictionArbitrageStore(tmp_path / "stale")
    current[0] = T
    include_d[0] = False
    allow_b_success[0] = False
    stale_service = PolymarketLPService(
        stale_store,
        Exchange(stale_history_calls),
        clock=lambda: current[0],
    )
    stale_service.refresh_price_history()
    current[0] = T + timedelta(seconds=300)
    stale_service.refresh_price_history()
    assert stale_store.lp_preparation_items()[0]["state"] == "waiting_retry"
    assert stale_store.lp_preparation_items()[0]["paused"] is False

    current[0] = T + timedelta(hours=25)
    stale_block[0] = True
    stale_result: dict[str, object] = {}

    def stale_refresh() -> None:
        stale_result.update(stale_service.refresh_price_history())

    stale_thread = threading.Thread(target=stale_refresh)
    stale_thread.start()
    assert stale_started.wait(timeout=2)
    stale_recovered = stale_service.recover_preparation()
    assert stale_recovered["recovered_condition_ids"] == []
    live_item = stale_store.lp_preparation_items()[0]
    assert live_item["state"] == "retrying"
    assert live_item["paused"] is False
    stale_release.set()
    stale_thread.join(timeout=3)
    assert not stale_thread.is_alive()
    assert stale_result["preparation_outcome"] in {"success", "failure", "waiting_retry"}
    old_item = stale_store.lp_preparation_items()[0]
    assert old_item["state"] == "waiting_retry"
    old_generation = int(old_item["generation"])
    allow_b_success[0] = True
    assert stale_store.lp_recover_preparation_items(
        ["condition-b"]
    ) == [{"condition_id": "condition-b", "recovered": True}]
    replacement = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "stale"),
        Exchange(stale_history_calls),
        clock=lambda: current[0],
    )
    recovered_result = replacement.refresh_price_history()
    assert recovered_result["preparation_outcome"] == "success"
    new_summary = replacement.store.lp_price_history_summary(
        "condition-b", "token-b", now=current[0]
    )
    assert new_summary is not None and new_summary["state"] == "known"
    stale_write = replacement.store.lp_save_price_history_batch(
        (
            {
                "condition_id": "condition-b",
                "token_id": "token-b",
                "samples": [],
                "summary": {"state": "known", "checked_at": current[0]},
            },
        ),
        generation=old_generation,
    )
    assert stale_write == 0
    assert replacement.store.lp_price_history_summary(
        "condition-b", "token-b", now=current[0]
    ) == new_summary

    stale_block[0] = False
    follow_up = replacement.refresh_price_history()
    assert follow_up["preparation_outcome"] in {"success", "failure"}
    assert any("token-b" in call for call in stale_history_calls[2:])


def test_failed_group_does_not_block_other_items_or_duplicate_retry_alerts(
    tmp_path: Path,
) -> None:
    current = [T]
    conditions = tuple(f"condition-{index:03d}" for index in range(100))
    history_calls: list[tuple[str, ...]] = []
    retry_started = threading.Event()
    all_retries_started = threading.Event()
    release_retry = threading.Event()
    notifications: list[tuple[str, str]] = []
    notification_fails = [True]

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = conditions if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": (
                            Decimal("200")
                            if int(condition_id[-3:]) >= 80
                            else Decimal("100")
                        ),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        @staticmethod
        def _market(condition_id: str, checked_at: datetime) -> dict[str, object]:
            token_id = f"token-{int(condition_id[-3:]):03d}"
            return {
                "market_id": f"market-{condition_id[-3:]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": checked_at,
                "fees_checked_at": checked_at,
                "outcomes": {
                    "yes": {"label": "YES", "token_id": token_id}
                },
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id, current[0])
                for condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            first = int(token_ids[0].removeprefix("token-"))
            if first < 80:
                if current[0] >= T + timedelta(seconds=300):
                    retry_started.set()
                    if len(history_calls) == 9:
                        all_retries_started.set()
                    assert release_retry.wait(timeout=2)
                raise IncompleteRead(b"partial")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, Exchange(), clock=lambda: current[0])
    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    assert first["preparation"]["state"] == "partial"
    assert len(history_calls) == 5
    first_snapshot = service.refresh_candidates(force=True)
    # The failed tail batch must not block healthy markets: candidates still
    # project (capped at ten) and the failed batch shows up as base reasons.
    assert "condition-080" in {
        row["condition_id"] for row in first_snapshot["candidates"]
    }
    assert any(
        reason["condition_id"] in {f"condition-{index:03d}" for index in range(80)}
        and reason["code"] == "history_summary_unknown"
        for reason in first_snapshot["funnel"]["reasons"]["base"]
    )
    current[0] = T + timedelta(seconds=299)
    before_due = service.refresh_price_history()
    assert before_due["preparation_outcome"] == "waiting_retry"
    assert len(history_calls) == 5

    current[0] = T + timedelta(seconds=300)
    result_holder: list[dict[str, object]] = []

    def refresh_retry() -> None:
        result_holder.append(service.refresh_price_history())

    worker = threading.Thread(target=refresh_retry)
    worker.start()
    assert retry_started.wait(timeout=2)
    assert all_retries_started.wait(timeout=2)
    in_flight_call_count = len(history_calls)
    assert in_flight_call_count == 9
    restarted_in_flight = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), Exchange(), clock=lambda: current[0]
    )
    in_flight_restart = restarted_in_flight.refresh_price_history()
    assert in_flight_restart["preparation_outcome"] == "busy"
    assert in_flight_restart["alert_pending"] is True
    assert in_flight_restart["preparation"]["fault_alert_state"] == "claimed"
    assert len(history_calls) == in_flight_call_count
    release_retry.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(history_calls) == 9
    waiting = store.lp_preparation_items()
    assert len(waiting) == 80
    assert all(item["state"] == "waiting_retry" for item in waiting)
    assert all(item["paused"] is False for item in waiting)
    assert all(item["retry_used"] is False for item in waiting)
    retry_result = result_holder[0]
    assert retry_result.get("alert_pending") is not True
    preparation = in_flight_restart["preparation"]
    assert preparation["fault_alert_state"] == "claimed"
    assert preparation["paused_market_count"] == 0
    assert "alert_condition_ids" not in preparation

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))
            if notification_fails[0]:
                raise RuntimeError("notification unavailable")

    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=Exchange(),
        notifier=SimpleNamespace(_notifiers=(Feishu(),)),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    notification = execution.notify_lp_preparation_failure(preparation)
    assert notification["state"] == "failed"
    assert len(notifications) == 1
    assert "自动探测并按退避继续补全" in notifications[0][1]
    assert "condition-" not in notifications[0][1]
    finished = service.finish_preparation_alert(
        generation=int(preparation["generation"]), success=False
    )
    assert finished is not None

    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), Exchange(), clock=lambda: current[0]
    )
    restart_result = restarted.refresh_price_history()
    assert restart_result["preparation_outcome"] != "paused"
    assert len(history_calls) == 9
    assert restart_result.get("alert_pending") is not True
    candidate = restarted.refresh_candidates(force=True)
    assert "condition-080" in {
        row["condition_id"] for row in candidate["candidates"]
    }
    notification_fails[0] = False
    wake_result = restarted.refresh_price_history()
    assert wake_result["preparation_outcome"] != "paused"
    assert wake_result.get("alert_pending") is not True
    assert len(history_calls) == 9
    assert len(notifications) == 1


def test_market_retry_budget_is_shared_across_preparation_stages(
    tmp_path: Path,
) -> None:
    current = [T]
    history_calls: list[tuple[str, ...]] = []
    metadata_calls: list[tuple[str, ...]] = []
    first_metadata_failure = [True]

    class Exchange:
        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            outcomes: dict[str, object] = {
                "yes": {
                    "label": "YES",
                    "token_id": f"token-{condition_id[-1]}",
                }
            }
            if condition_id == "condition-b":
                outcomes["no"] = {
                    "label": "NO",
                    "token_id": "token-b-no",
                }
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": outcomes,
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = ("condition-b", "condition-c") if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in condition_ids
            }

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_calls.append(condition_ids)
            if first_metadata_failure[0]:
                first_metadata_failure[0] = False
                return {
                    "state": "partial",
                    "complete": False,
                    "markets": {
                        "condition-c": self._market("condition-c"),
                    },
                    "failed_ids": {"condition-b": "IncompleteRead"},
                }
            return {
                "state": "known",
                "complete": True,
                "markets": {
                    condition_id: self._market(condition_id)
                    for condition_id in condition_ids
                },
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            if any(token.startswith("token-b") for token in token_ids):
                raise IncompleteRead(b"partial")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": (
                        "condition-b"
                        if token_id.startswith("token-b")
                        else "condition-c"
                    ),
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, Exchange(), clock=lambda: current[0])
    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "waiting_retry"
    assert metadata_calls == [("condition-b", "condition-c")]
    assert history_calls == [("token-c",)]
    waiting = store.lp_preparation_items()
    assert waiting[0]["condition_id"] == "condition-b"
    assert waiting[0]["retry_used"] is False

    current[0] = T + timedelta(seconds=299)
    assert service.refresh_price_history()["preparation_outcome"] == "waiting_retry"
    assert metadata_calls == [("condition-b", "condition-c")]
    current[0] = T + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "failure"
    assert second["alert_pending"] is True
    assert second["preparation"]["fault_alert_state"] == "claimed"
    assert second["preparation"]["fault_started_at"] == (
        T.isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    assert second["preparation"]["last_success_at"] is None
    assert second["preparation"]["paused_market_count"] == 0
    assert "alert_condition_ids" not in second["preparation"]
    assert metadata_calls == [
        ("condition-b", "condition-c"),
        ("condition-b", "condition-c"),
    ]
    assert history_calls == [("token-c",), ("token-b", "token-b-no")]
    waiting = store.lp_preparation_items()
    assert [item["condition_id"] for item in waiting] == ["condition-b"]
    assert waiting[0]["state"] == "waiting_retry"
    assert waiting[0]["paused"] is False
    assert waiting[0]["retry_used"] is False

    current[0] = T + timedelta(hours=23)
    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), Exchange(), clock=lambda: current[0]
    )
    third = restarted.refresh_price_history()
    assert third["preparation_outcome"] == "failure"
    assert third["preparation"]["state"] == "partial"
    assert metadata_calls[:2] == [
        ("condition-b", "condition-c"),
        ("condition-b", "condition-c"),
    ]
    assert metadata_calls[-1] == ("condition-b", "condition-c")
    assert history_calls[0] == ("token-c",)
    assert history_calls.count(("token-b", "token-b-no")) == 2
    assert len(history_calls) == 3
    waiting = restarted.store.lp_preparation_items()
    assert waiting[0]["state"] == "waiting_retry"
    assert waiting[0]["paused"] is False
    assert waiting[0]["retry_used"] is False
    candidate = restarted.refresh_candidates(force=True)
    assert "condition-c" in {
        row["condition_id"] for row in candidate["candidates"]
    }
    assert all(
        row["condition_id"] != "condition-b"
        for row in candidate["candidates"]
    )
    assert restarted.refresh_price_history().get("alert_pending") is not True


def test_legacy_paused_history_migrates_only_identified_failed_markets(
    tmp_path: Path,
) -> None:
    current = [T + timedelta(seconds=299)]
    old_attempt = T
    history_calls: list[tuple[str, ...]] = []
    metadata_calls: list[tuple[str, ...]] = []
    allow_b = [False]

    class Exchange:
        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            all_ids = ("condition-a", "condition-b", "condition-c")
            requested = all_ids if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            metadata_calls.append(condition_ids)
            return {
                condition_id: self._market(condition_id)
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            if "token-b" in token_ids:
                assert allow_b[0] is True
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    store.lp_save_price_history(
        "condition-a",
        "token-a",
        [
            {"t": int((T - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
            {"t": int(T.timestamp()), "p": Decimal("0.505")},
        ],
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": T,
            "valid_until": T + timedelta(hours=24),
            "last_attempt_at": old_attempt,
        },
    )
    store.lp_save_price_history(
        "condition-b",
        "token-b",
        [],
        {
            "state": "unknown",
            "checked_at": None,
            "last_attempt_at": old_attempt,
            "last_error": "IncompleteRead",
            "reason": "summary_time_unknown",
        },
    )
    store.lp_save_price_history(
        "condition-ignored",
        "token-ignored",
        [],
        {
            "state": "expired",
            "checked_at": T - timedelta(days=2),
            "valid_until": T - timedelta(days=1),
            "last_attempt_at": old_attempt,
            "last_error": "history_missing",
            "reason": "summary_expired",
        },
    )
    store.lp_save_preparation(
        {
            "state": "paused",
            "stage": "history",
            "generation": 3,
            "attempt": 2,
            "failure_count": 2,
            "paused": True,
            "alert_attempted": True,
            "alert_state": "sent",
            "last_attempt_at": old_attempt,
            "last_failure_at": old_attempt + timedelta(minutes=1),
            "last_progress_at": old_attempt + timedelta(minutes=1),
            "last_success_at": None,
            "next_retry_at": None,
            "completed_count": 2,
            "total_count": 3,
            "metadata_completed_count": 3,
            "metadata_total_count": 3,
            "last_error": "IncompleteRead",
        }
    )

    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] in {"success", "waiting_retry"}
    assert first.get("alert_pending") is not True
    assert history_calls == []
    assert metadata_calls == []
    items = store.lp_preparation_items()
    assert [item["condition_id"] for item in items] == ["condition-b"]
    assert items[0]["state"] == "waiting_retry"
    assert items[0]["paused"] is False
    assert items[0]["retry_used"] is False
    assert items[0]["alert_attempted"] is True
    assert items[0]["alert_state"] == "sent"
    preparation = service.preparation_snapshot()
    assert preparation["paused"] is False
    assert preparation["state"] == "partial"
    assert preparation["paused_market_count"] == 0

    reopened = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), exchange, clock=lambda: current[0]
    )
    reopened_result = reopened.refresh_price_history()
    assert reopened_result["preparation_outcome"] == "waiting_retry"
    assert history_calls == []
    assert metadata_calls == []
    assert reopened.preparation_snapshot()["paused_market_count"] == 0

    current[0] = T + timedelta(seconds=300)
    allow_b[0] = True
    expired_result = reopened.refresh_price_history()
    assert expired_result["preparation_outcome"] == "success"
    assert any("token-b" in call for call in history_calls)
    assert store.lp_preparation_items() == []
    assert reopened.store.lp_price_history_summary(
        "condition-b", "token-b", now=current[0]
    )["state"] == "known"
    assert reopened.store.lp_price_history_summary(
        "condition-a", "token-a", now=current[0]
    )["state"] == "known"
    assert reopened.store.lp_price_history_summary(
        "condition-c", "token-c", now=current[0]
    )["state"] == "known"


def test_new_preparation_alert_excludes_previously_notified_markets(
    tmp_path: Path,
) -> None:
    current = [T]
    condition_ids = ["condition-old"]
    history_calls: list[tuple[str, ...]] = []
    notifications: list[str] = []

    class Exchange:
        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id.removeprefix('condition-')}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id.removeprefix('condition-')}",
                    }
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = tuple(condition_ids or condition_ids_from_external())
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in requested
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            history_calls.append(token_ids)
            raise IncompleteRead(b"partial")

    def condition_ids_from_external() -> tuple[str, ...]:
        return tuple(condition_ids)

    store = PredictionArbitrageStore(tmp_path / "data")
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])

    initial = service.refresh_price_history()
    assert initial["preparation_outcome"] == "failure"
    assert history_calls == [("token-old",)]

    current[0] = T + timedelta(seconds=300)
    old_retry = service.refresh_price_history()
    assert old_retry["alert_pending"] is True
    assert old_retry["preparation"]["fault_alert_state"] == "claimed"
    assert "alert_condition_ids" not in old_retry["preparation"]

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            del title
            notifications.append(message)

    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=exchange,
        notifier=SimpleNamespace(_notifiers=(Feishu(),)),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    assert execution.notify_lp_preparation_failure(old_retry["preparation"])["state"] == "sent"
    assert service.finish_preparation_alert(
        generation=int(old_retry["preparation"]["generation"]), success=True
    ) is not None

    condition_ids.append("condition-new")
    current[0] = T + timedelta(seconds=301)
    new_initial = service.refresh_price_history()
    assert new_initial["preparation_outcome"] == "waiting_retry"
    assert history_calls == [("token-old",), ("token-old",)]
    assert {item["condition_id"] for item in store.lp_preparation_items()} == {
        "condition-old"
    }

    current[0] = T + timedelta(seconds=601)
    before_due = service.refresh_price_history()
    assert before_due["preparation_outcome"] == "waiting_retry"
    assert history_calls == [("token-old",), ("token-old",)]
    assert len(notifications) == 1

    current[0] = T + timedelta(seconds=900)
    new_retry = service.refresh_price_history()
    assert new_retry["preparation_outcome"] == "failure"
    assert new_retry.get("alert_pending") is not True
    assert new_retry["preparation"]["fault_alert_state"] == "sent"
    assert any(call == ("token-old",) for call in history_calls[-2:])
    assert any(call == ("token-new",) for call in history_calls[-2:])
    assert len(notifications) == 1

    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), exchange, clock=lambda: current[0]
    )
    wake = restarted.refresh_price_history()
    assert wake.get("alert_pending") is not True
    assert len(notifications) == 1
    assert {item["condition_id"] for item in store.lp_preparation_items()} == {
        "condition-old",
        "condition-new",
    }


def test_interrupted_preparation_retry_remains_paused_and_recoverable(
    tmp_path: Path,
) -> None:
    current = [T]
    history_calls: list[tuple[str, ...]] = []
    retry_blocked = [False]
    retry_started = threading.Event()
    release_retry = threading.Event()
    allow_success = [False]

    class Exchange:
        @staticmethod
        def lp_reward_catalog(
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": "condition-b",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                ],
            }

        @staticmethod
        def lp_market_metadata(
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: {
                    "market_id": "market-b",
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "minimum_order_size": Decimal("20"),
                    "tick_size": Decimal("0.01"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "metadata_checked_at": current[0],
                    "fees_checked_at": current[0],
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "token-b"}
                    },
                }
                for condition_id in condition_ids
            }

        @staticmethod
        def lp_price_history(
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del fidelity
            history_calls.append(token_ids)
            if retry_blocked[0]:
                retry_started.set()
                assert release_retry.wait(timeout=3)
                if stop_event is not None and stop_event.is_set():
                    return {"state": "cancelled", "history": {}}
            if not allow_success[0]:
                raise IncompleteRead(b"partial")
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    current[0] = T + timedelta(seconds=300)
    retry_blocked[0] = True
    stop_event = threading.Event()
    outcomes: list[dict[str, object]] = []

    worker = threading.Thread(
        target=lambda: outcomes.append(
            service.refresh_price_history(stop_event=stop_event)
        )
    )
    worker.start()
    assert retry_started.wait(timeout=2)
    stop_event.set()
    release_retry.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert outcomes[0]["preparation_outcome"] == "cancelled"
    assert history_calls == [("token-b",), ("token-b",)]

    retry_blocked[0] = False
    current[0] = T + timedelta(days=1)
    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), exchange, clock=lambda: current[0]
    )
    restart_result = restarted.refresh_price_history()
    state = restarted.preparation_snapshot()
    assert restart_result["preparation_outcome"] == "failure"
    assert len(history_calls) == 3
    assert state["retrying_market_count"] == 0
    assert state["paused_market_count"] == 0
    item = restarted.store.lp_preparation_items()[0]
    assert item["condition_id"] == "condition-b"
    assert item["state"] == "waiting_retry"
    assert item["paused"] is False
    assert item["retry_used"] is False
    assert item["failure_count"] == 3
    assert item["error"] == "IncompleteRead"
    assert item["next_retry_at"] == (
        (current[0] + timedelta(seconds=1200))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    repeated = restarted.refresh_price_history()
    assert repeated["preparation_outcome"] == "waiting_retry"
    assert len(history_calls) == 3

    allow_success[0] = True
    current[0] = datetime.fromisoformat(
        str(item["next_retry_at"]).replace("Z", "+00:00")
    )
    recovered_result = restarted.refresh_price_history()
    assert recovered_result["preparation_outcome"] == "success"
    assert history_calls[-1] == ("token-b",)
    assert restarted.store.lp_preparation_items() == []
    assert restarted.store.lp_price_history_summary(
        "condition-b", "token-b", now=current[0]
    )["state"] == "known"


def test_recovering_paused_market_preserves_other_inflight_failures(
    tmp_path: Path,
) -> None:
    current = [T]
    catalog_ids = ["condition-b"]
    history_calls: list[tuple[str, ...]] = []
    b_success = [False]
    c_success = [False]
    c_blocked = [False]
    c_started = threading.Event()
    release_c = threading.Event()

    def history_payload(
        token_ids: tuple[str, ...], start_ts: int, end_ts: int
    ) -> dict[str, object]:
        return {
            "state": "known",
            "history": {
                token_id: [
                    {"t": start_ts, "p": Decimal("0.500")},
                    {"t": end_ts, "p": Decimal("0.505")},
                ]
                for token_id in token_ids
            },
        }

    class Exchange:
        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": current[0],
                "fees_checked_at": current[0],
                "outcomes": {
                    "yes": {"label": "YES", "token_id": f"token-{condition_id[-1]}"}
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = tuple(condition_ids or tuple(catalog_ids))
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in requested
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            if token_ids == ("token-b",):
                if not b_success[0]:
                    return {
                        "state": "partial",
                        "history": {},
                        "errors": {"token-b": "certificate_error"},
                    }
                return history_payload(token_ids, start_ts, end_ts)
            if c_blocked[0]:
                c_started.set()
                assert release_c.wait(timeout=3)
            if not c_success[0]:
                raise IncompleteRead(b"partial")
            return history_payload(token_ids, start_ts, end_ts)

    store = PredictionArbitrageStore(tmp_path / "preserve")
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    assert service.refresh_price_history()["preparation_outcome"] == "failure"
    current[0] = T + timedelta(seconds=300)
    paused = service.refresh_price_history()
    assert paused["preparation"]["paused_market_count"] == 1

    catalog_ids.append("condition-c")
    current[0] = T + timedelta(seconds=301)
    c_blocked[0] = True
    in_flight_result: list[dict[str, object]] = []

    worker = threading.Thread(
        target=lambda: in_flight_result.append(service.refresh_price_history())
    )
    worker.start()
    assert c_started.wait(timeout=2)
    recovered_b = service.recover_preparation()
    assert recovered_b["recovered_condition_ids"] == ["condition-b"]
    release_c.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert in_flight_result[0]["preparation_outcome"] in {"failure", "success"}
    after_c_failure = service.preparation_snapshot()
    assert after_c_failure["waiting_market_count"] == 1
    c_item = next(
        item
        for item in store.lp_preparation_items()
        if item["condition_id"] == "condition-c"
    )
    retry_at = datetime.fromisoformat(
        str(c_item["next_retry_at"]).replace("Z", "+00:00")
    )
    assert retry_at == T + timedelta(seconds=601)

    b_success[0] = True
    current[0] = T + timedelta(seconds=302)
    b_result = service.refresh_price_history()
    assert b_result["preparation_outcome"] in {"success", "waiting_retry"}
    assert history_calls[-1] == ("token-b",)
    b_summary = store.lp_price_history_summary("condition-b", "token-b", now=current[0])
    assert b_summary is not None and b_summary["state"] == "known"
    before_early_wake = list(history_calls)

    current[0] = T + timedelta(seconds=600)
    early_wake = service.refresh_price_history()
    assert early_wake["preparation_outcome"] in {"success", "waiting_retry"}
    assert history_calls == before_early_wake

    c_success[0] = True
    current[0] = T + timedelta(seconds=601)
    c_retry = service.refresh_price_history()
    assert c_retry["preparation_outcome"] == "success"
    assert history_calls[-1] == ("token-c",)
    assert store.lp_price_history_summary("condition-b", "token-b", now=current[0]) == b_summary

    def run_recovery_gap(outcome: str, data_dir: Path) -> None:
        gap_now = [T]
        gap_calls: list[tuple[str, ...]] = []
        old_started = threading.Event()
        release_old = threading.Event()

        class GapExchange:
            @staticmethod
            def lp_reward_catalog(
                *,
                condition_ids: tuple[str, ...] | None = None,
                stop_event: object = None,
            ) -> dict[str, object]:
                del condition_ids, stop_event
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": gap_now[0],
                    "markets": [
                        {
                            "condition_id": "condition-b",
                            "daily_pool_usd": Decimal("100"),
                            "reward_active": True,
                            "rewards_min_size": Decimal("20"),
                            "rewards_max_spread": Decimal("10"),
                        }
                    ],
                }

            @staticmethod
            def lp_market_metadata(
                requested: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, dict[str, object]]:
                del stop_event
                return {
                    condition_id: {
                        "market_id": "market-b",
                        "condition_id": condition_id,
                        "accepting_orders": True,
                        "reward_min_size": Decimal("20"),
                        "reward_max_spread": Decimal("0.10"),
                        "minimum_order_size": Decimal("20"),
                        "tick_size": Decimal("0.01"),
                        "fees_enabled": False,
                        "taker_fee_rate": Decimal("0"),
                        "fee_exponent": Decimal("1"),
                        "metadata_checked_at": gap_now[0],
                        "outcomes": {
                            "yes": {"label": "YES", "token_id": "token-b"}
                        },
                    }
                    for condition_id in requested
                }

            @staticmethod
            def lp_price_history(
                token_ids: tuple[str, ...],
                *,
                start_ts: int,
                end_ts: int,
                fidelity: int,
                stop_event: object = None,
            ) -> dict[str, object]:
                del fidelity, stop_event
                gap_calls.append(token_ids)
                call_number = len(gap_calls)
                if call_number == 1:
                    raise IncompleteRead(b"initial response")
                if call_number == 2:
                    old_started.set()
                    assert release_old.wait(timeout=3)
                    if outcome == "failure":
                        raise IncompleteRead(b"old response")
                if call_number == 3 and outcome == "success":
                    raise IncompleteRead(b"new response")
                return history_payload(token_ids, start_ts, end_ts)

        store = PredictionArbitrageStore(data_dir)
        exchange = GapExchange()
        service = PolymarketLPService(store, exchange, clock=lambda: gap_now[0])
        assert service.refresh_price_history()["preparation_outcome"] == "failure"
        initial_summary = store.lp_price_history_summary(
            "condition-b", "token-b", now=gap_now[0]
        )
        assert initial_summary is not None
        gap_now[0] = T + timedelta(seconds=300)
        old_result: list[dict[str, object]] = []
        old_worker = threading.Thread(
            target=lambda: old_result.append(service.refresh_price_history())
        )
        old_worker.start()
        assert old_started.wait(timeout=2)

        restarted = PolymarketLPService(
            PredictionArbitrageStore(data_dir), exchange, clock=lambda: gap_now[0]
        )
        busy = restarted.refresh_price_history()
        assert busy["preparation_outcome"] == "busy"
        assert len(gap_calls) == 2
        live_item = restarted.store.lp_preparation_items()[0]
        assert live_item["state"] == "retrying"
        assert live_item["paused"] is False
        assert restarted.store.lp_price_history_summary(
            "condition-b", "token-b", now=gap_now[0]
        ) == initial_summary
        assert restarted.recover_preparation()["recovered_condition_ids"] == []

        release_old.set()
        old_worker.join(timeout=3)
        assert not old_worker.is_alive()
        assert old_result
        old_generation = int(restarted.preparation_snapshot()["generation"])
        if outcome == "failure":
            old_item = restarted.store.lp_preparation_items()[0]
            old_generation = int(old_item["generation"])
            assert old_item["state"] == "waiting_retry"
            recovered_items = restarted.store.lp_recover_preparation_items(
                ["condition-b"]
            )
            assert recovered_items == [{"condition_id": "condition-b", "recovered": True}]
            restarted = PolymarketLPService(
                PredictionArbitrageStore(data_dir), exchange, clock=lambda: gap_now[0]
            )
            gap_now[0] = T + timedelta(seconds=301)
            new_result = restarted.refresh_price_history()
            assert new_result["preparation_outcome"] == "success"
            new_summary = restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=gap_now[0]
            )
            assert new_summary is not None and new_summary["state"] == "known"
            stale = restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=old_generation,
                stage="history",
                error="TimeoutError",
                failed_at=gap_now[0],
            )
            assert stale is None
            assert restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=gap_now[0]
            ) == new_summary
            assert restarted.store.lp_preparation_items() == []
        else:
            assert restarted.store.lp_preparation_items() == []
            old_summary = restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=gap_now[0]
            )
            assert old_summary is not None and old_summary["state"] == "known"
            current_generation = int(restarted.preparation_snapshot()["generation"])
            restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=current_generation,
                stage="history",
                error="certificate_error",
                failed_at=gap_now[0],
            )
            restarted = PolymarketLPService(
                PredictionArbitrageStore(data_dir), exchange, clock=lambda: gap_now[0]
            )
            recovered = restarted.recover_preparation()
            assert recovered["recovered_condition_ids"] == ["condition-b"]
            newer_generation = int(restarted.preparation_snapshot()["generation"])
            assert newer_generation > old_generation
            gap_now[0] = T + timedelta(seconds=301)
            newer_failure = restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=newer_generation,
                stage="history",
                error="TimeoutError",
                failed_at=gap_now[0],
            )
            assert newer_failure is not None
            assert newer_failure["state"] == "waiting_retry"
            new_item = restarted.store.lp_preparation_items()[0]
            assert new_item["state"] == "waiting_retry"
            assert datetime.fromisoformat(
                str(new_item["next_retry_at"]).replace("Z", "+00:00")
            ) == T + timedelta(seconds=601)
            stale = restarted.store.lp_save_price_history_batch(
                (
                    {
                        "condition_id": "condition-b",
                        "token_id": "token-b",
                        "samples": [],
                        "summary": {"state": "known", "checked_at": gap_now[0]},
                    },
                ),
                generation=old_generation,
            )
            assert stale == 0
            assert restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=gap_now[0]
            ) == old_summary

    run_recovery_gap("failure", tmp_path / "gap-failure")
    run_recovery_gap("success", tmp_path / "gap-success")

    def run_late_response(outcome: str, data_dir: Path) -> None:
        late_now = [T]
        late_calls: list[tuple[str, ...]] = []
        old_started = threading.Event()
        release_old = threading.Event()

        class LateExchange:
            @staticmethod
            def lp_reward_catalog(
                *,
                condition_ids: tuple[str, ...] | None = None,
                stop_event: object = None,
            ) -> dict[str, object]:
                del condition_ids, stop_event
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": late_now[0],
                    "markets": [
                        {
                            "condition_id": "condition-b",
                            "daily_pool_usd": Decimal("100"),
                            "reward_active": True,
                            "rewards_min_size": Decimal("20"),
                            "rewards_max_spread": Decimal("10"),
                        }
                    ],
                }

            @staticmethod
            def lp_market_metadata(
                requested: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, dict[str, object]]:
                del stop_event
                return {
                    condition_id: {
                        "market_id": "market-b",
                        "condition_id": condition_id,
                        "accepting_orders": True,
                        "reward_min_size": Decimal("20"),
                        "reward_max_spread": Decimal("0.10"),
                        "minimum_order_size": Decimal("20"),
                        "tick_size": Decimal("0.01"),
                        "fees_enabled": False,
                        "taker_fee_rate": Decimal("0"),
                        "fee_exponent": Decimal("1"),
                        "metadata_checked_at": late_now[0],
                        "outcomes": {
                            "yes": {"label": "YES", "token_id": "token-b"}
                        },
                    }
                    for condition_id in requested
                }

            @staticmethod
            def lp_price_history(
                token_ids: tuple[str, ...],
                *,
                start_ts: int,
                end_ts: int,
                fidelity: int,
                stop_event: object = None,
            ) -> dict[str, object]:
                del fidelity, stop_event
                late_calls.append(token_ids)
                if len(late_calls) == 1:
                    raise IncompleteRead(b"initial response")
                if len(late_calls) == 2:
                    old_started.set()
                    assert release_old.wait(timeout=3)
                    if outcome == "failure":
                        raise IncompleteRead(b"old response")
                if len(late_calls) == 3 and outcome == "success":
                    raise IncompleteRead(b"new response")
                return history_payload(token_ids, start_ts, end_ts)

        old_store = PredictionArbitrageStore(data_dir)
        old_exchange = LateExchange()
        old_service = PolymarketLPService(
            old_store, old_exchange, clock=lambda: late_now[0]
        )
        assert old_service.refresh_price_history()["preparation_outcome"] == "failure"
        initial_summary = old_store.lp_price_history_summary(
            "condition-b", "token-b", now=late_now[0]
        )
        assert initial_summary is not None
        late_now[0] = T + timedelta(seconds=300)
        old_result: list[dict[str, object]] = []
        old_worker = threading.Thread(
            target=lambda: old_result.append(old_service.refresh_price_history())
        )
        old_worker.start()
        assert old_started.wait(timeout=2)

        restarted = PolymarketLPService(
            PredictionArbitrageStore(data_dir), old_exchange, clock=lambda: late_now[0]
        )
        busy = restarted.refresh_price_history()
        assert busy["preparation_outcome"] == "busy"
        assert len(late_calls) == 2
        live_item = restarted.store.lp_preparation_items()[0]
        assert live_item["state"] == "retrying"
        assert live_item["paused"] is False
        assert restarted.store.lp_price_history_summary(
            "condition-b", "token-b", now=late_now[0]
        ) == initial_summary
        assert restarted.recover_preparation()["recovered_condition_ids"] == []

        release_old.set()
        old_worker.join(timeout=3)
        assert not old_worker.is_alive()
        assert old_result
        old_generation = int(restarted.preparation_snapshot()["generation"])
        if outcome == "failure":
            old_item = restarted.store.lp_preparation_items()[0]
            old_generation = int(old_item["generation"])
            recovered_items = restarted.store.lp_recover_preparation_items(
                ["condition-b"]
            )
            assert recovered_items == [{"condition_id": "condition-b", "recovered": True}]
            restarted = PolymarketLPService(
                PredictionArbitrageStore(data_dir), old_exchange, clock=lambda: late_now[0]
            )
            late_now[0] = T + timedelta(seconds=301)
            new_result = restarted.refresh_price_history()
            assert new_result["preparation_outcome"] == "success"
            new_summary = restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=late_now[0]
            )
            assert new_summary is not None and new_summary["state"] == "known"
            stale = restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=old_generation,
                stage="history",
                error="TimeoutError",
                failed_at=late_now[0],
            )
            assert stale is None
            assert restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=late_now[0]
            ) == new_summary
            assert restarted.store.lp_preparation_items() == []
        else:
            assert restarted.store.lp_preparation_items() == []
            old_summary = restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=late_now[0]
            )
            assert old_summary is not None and old_summary["state"] == "known"
            current_generation = int(restarted.preparation_snapshot()["generation"])
            restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=current_generation,
                stage="history",
                error="certificate_error",
                failed_at=late_now[0],
            )
            restarted = PolymarketLPService(
                PredictionArbitrageStore(data_dir), old_exchange, clock=lambda: late_now[0]
            )
            recovered = restarted.recover_preparation()
            assert recovered["recovered_condition_ids"] == ["condition-b"]
            newer_generation = int(restarted.preparation_snapshot()["generation"])
            assert newer_generation > old_generation
            late_now[0] = T + timedelta(seconds=301)
            newer_failure = restarted.store.lp_record_preparation_failure(
                "condition-b",
                generation=newer_generation,
                stage="history",
                error="TimeoutError",
                failed_at=late_now[0],
            )
            assert newer_failure is not None
            assert newer_failure["state"] == "waiting_retry"
            new_summary = restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=late_now[0]
            )
            assert new_summary == old_summary
            new_item = restarted.store.lp_preparation_items()[0]
            assert new_item["state"] == "waiting_retry"
            assert datetime.fromisoformat(
                str(new_item["next_retry_at"]).replace("Z", "+00:00")
            ) == T + timedelta(seconds=601)
            stale = restarted.store.lp_save_price_history_batch(
                (
                    {
                        "condition_id": "condition-b",
                        "token_id": "token-b",
                        "samples": [],
                        "summary": {"state": "known", "checked_at": late_now[0]},
                    },
                ),
                generation=old_generation,
            )
            assert stale == 0
            assert restarted.store.lp_price_history_summary(
                "condition-b", "token-b", now=late_now[0]
            ) == old_summary

    run_late_response("failure", tmp_path / "late-failure")
    run_late_response("success", tmp_path / "late-success")


@pytest.mark.parametrize("confirmed_absent", [False, True], ids=["present", "confirmed-absent"])
def test_due_metadata_retry_dispatches_before_initial_history_pass_finishes(
    tmp_path: Path,
    confirmed_absent: bool,
) -> None:
    """A due metadata retry is dispatched at a free history group boundary."""

    current = [T]
    condition_ids = tuple(f"condition-{index:03d}" for index in range(161)) + (
        "condition-b",
    )
    metadata_calls: list[tuple[str, ...]] = []
    history_calls: list[tuple[str, ...]] = []
    metadata_failed = [True]
    b_history_seen = threading.Event()
    final_wait_timed_out = [False]

    def market(condition_id: str) -> dict[str, object]:
        token_id = "token-b" if condition_id == "condition-b" else condition_id.replace(
            "condition-", "token-"
        )
        return {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "accepting_orders": True,
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("20"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": current[0],
            "fees_checked_at": current[0],
            "outcomes": {
                "yes": {"label": "YES", "token_id": token_id},
            },
        }

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = condition_ids or condition_ids_all
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = tuple(requested)
            metadata_calls.append(requested)
            if metadata_failed[0]:
                metadata_failed[0] = False
                return {
                    "state": "known",
                    "markets": {
                        condition_id: market(condition_id)
                        for condition_id in requested
                        if condition_id != "condition-b"
                    },
                    "failed_ids": {"condition-b": "IncompleteRead"},
                }
            if confirmed_absent and "condition-b" in requested:
                return {
                    "state": "known",
                    "markets": {
                        condition_id: market(condition_id)
                        for condition_id in requested
                        if condition_id != "condition-b"
                    },
                    "confirmed_absent_ids": ["condition-b"],
                }
            return {
                "state": "known",
                "markets": {
                    condition_id: market(condition_id) for condition_id in requested
                },
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {condition_id: market(condition_id) for condition_id in requested}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(tuple(token_ids))
            if "token-080" in token_ids:
                current[0] = T + timedelta(seconds=300)
            if "token-b" in token_ids:
                b_history_seen.set()
            if (
                "token-160" in token_ids
                and not confirmed_absent
                and not b_history_seen.wait(timeout=2)
            ):
                final_wait_timed_out[0] = True
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": current[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": (
                        "condition-b"
                        if token_id == "token-b"
                        else token_id.replace("token-", "condition-")
                    ),
                    "token_id": token_id,
                    "received_at": current[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                }
                for token_id in token_ids
            }

    condition_ids_all = condition_ids
    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path / "data")
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])

    result = service.refresh_price_history()

    assert result["preparation_outcome"] == "success"
    assert result["target_count"] == (161 if confirmed_absent else 162)
    assert metadata_calls.count(("condition-b",)) == 1
    assert sum("condition-b" in call for call in metadata_calls) == 2
    assert history_calls.count(("token-b",)) == (0 if confirmed_absent else 1)
    if not confirmed_absent:
        assert history_calls.index(("token-b",)) < history_calls.index(("token-160",))
    assert final_wait_timed_out[0] is False
    assert store.lp_preparation_items() == []

    snapshot = service.refresh_candidates(force=True)
    # Issue 143 repair 2: the token-080 history read advanced the clock by
    # 300s, so the prepared metadata is outside the 60-second candidate
    # freshness window; the first batch renews it once (targeted) before
    # qualifying, and the ten live-qualified passers fill the table and end
    # the round.
    assert snapshot["funnel"]["stop_reason"] == "filled"
    assert snapshot["funnel"]["checked"] == 10
    assert snapshot["funnel"]["passed"] == 10
    assert snapshot["funnel"]["unknown"] == 0
    assert len(snapshot["candidates"]) == 10

    history_count = len(history_calls)
    metadata_retry_count = metadata_calls.count(("condition-b",))
    restarted = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "data"), exchange, clock=lambda: current[0]
    )
    assert restarted.refresh_price_history()["preparation_outcome"] == "success"
    if confirmed_absent:
        assert all("token-b" not in call for call in history_calls[history_count:])
    else:
        assert len(history_calls) == history_count
    assert metadata_calls.count(("condition-b",)) == metadata_retry_count
