from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_views import lp_shortlist
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


T = datetime(2026, 9, 18, tzinfo=UTC)


def _direction(condition_id: str, summary: dict[str, object]) -> dict[str, object]:
    return {
        "market": {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "outcome": "YES",
            "accepting_orders": True,
        },
        "daily_pool_usd": Decimal("100"),
        "reward_active": True,
        "history_summary": summary,
    }


def test_daily_history_cache_readers_and_shortlist_share_expiry(
    tmp_path: Path,
) -> None:
    db = PredictionArbitrageStore(tmp_path / "data")
    db.lp_save_price_history_batch(
        (
            {
                "condition_id": "condition-valid",
                "token_id": "token-valid",
                "samples": [{"t": int(T.timestamp()), "p": Decimal("0.500")}],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T,
                    "valid_until": T + timedelta(hours=24),
                },
            },
            {
                "condition_id": "condition-early",
                "token_id": "token-early",
                "samples": [{"t": int(T.timestamp()), "p": Decimal("0.500")}],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T,
                    "valid_until": T + timedelta(hours=2),
                },
            },
            {
                "condition_id": "condition-future",
                "token_id": "token-future",
                "samples": [{"t": int((T + timedelta(hours=25)).timestamp()), "p": Decimal("0.500")}],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T + timedelta(hours=25),
                    "valid_until": T + timedelta(hours=49),
                },
            },
            {
                "condition_id": "condition-invalid",
                "token_id": "token-invalid",
                "samples": [{"t": int(T.timestamp()), "p": Decimal("0.500")}],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": "not-a-timestamp",
                    "valid_until": T + timedelta(hours=24),
                },
            },
        )
    )

    identities = (
        ("condition-valid", "token-valid"),
        ("condition-early", "token-early"),
        ("condition-future", "token-future"),
        ("condition-invalid", "token-invalid"),
    )

    for checked_at, expected_state, expected_shortlist in (
        (T + timedelta(hours=3), "known", {"condition-valid"}),
        (T + timedelta(hours=23, minutes=59), "known", {"condition-valid"}),
        (T + timedelta(hours=24), "expired", set()),
    ):
        single = db.lp_price_history_summary(
            "condition-valid", "token-valid", now=checked_at
        )
        batch = db.lp_price_history_summaries(identities, now=checked_at)
        assert single is not None
        assert single["state"] == expected_state
        assert batch[("condition-valid", "token-valid")]["state"] == expected_state
        assert batch[("condition-early", "token-early")]["state"] == "expired"
        assert batch[("condition-future", "token-future")]["state"] == "unknown"
        assert batch[("condition-invalid", "token-invalid")]["state"] == "unknown"

        directions = [
            _direction(condition_id, batch[(condition_id, token_id)])
            for condition_id, token_id in identities
        ]
        assert {
            row["condition_id"] for row in lp_shortlist(directions, now=checked_at)
        } == expected_shortlist


def test_daily_history_refresh_reuses_persisted_valid_results(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    current = [T]

    class HistoryExchange:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": "condition-reused",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            assert condition_ids == ("condition-reused",)
            return {
                "condition-reused": {
                    "market_id": "market-reused",
                    "condition_id": "condition-reused",
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "token-reused"}
                    },
                }
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
            del stop_event
            assert token_ids == ("token-reused",)
            assert fidelity == 1
            self.history_calls.append(
                {
                    "token_ids": token_ids,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                }
            )
            return {
                "state": "known",
                "history": {
                    "token-reused": [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                },
            }

    exchange = HistoryExchange()
    db = PredictionArbitrageStore(data_dir)
    service = PolymarketLPService(db, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "success"
    assert first["updated_count"] == 1
    assert first["request_count"] == 1
    assert len(exchange.history_calls) == 1
    initial = db.lp_price_history_summary(
        "condition-reused", "token-reused", now=T
    )
    assert initial is not None
    assert initial["valid_until"] == "2026-09-19T00:00:00.000000Z"
    preserved_fields = {
        key: initial[key]
        for key in ("checked_at", "window_start", "window_end", "sample_count", "amplitude")
    }

    for hours in (1, 12, 23):
        current[0] = T + timedelta(hours=hours)
        if hours in {12, 23}:
            db = PredictionArbitrageStore(data_dir)
            service = PolymarketLPService(db, exchange, clock=lambda: current[0])
        refreshed = service.refresh_price_history()
        assert refreshed["preparation_outcome"] == "success"
        assert refreshed["state"] == "known"
        assert refreshed["request_count"] == 0
        assert refreshed["updated_count"] == 0
        assert refreshed["preparation"]["completed_count"] == 1
        cached = db.lp_price_history_summary(
            "condition-reused", "token-reused", now=current[0]
        )
        assert cached is not None
        assert {
            key: cached[key]
            for key in ("checked_at", "window_start", "window_end", "sample_count", "amplitude")
        } == preserved_fields
        assert cached["valid_until"] == "2026-09-19T00:00:00.000000Z"
        assert len(exchange.history_calls) == 1


def test_daily_history_refresh_only_fetches_missing_or_expired_directions(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    current = [T]
    db = PredictionArbitrageStore(data_dir)
    db.lp_save_price_history_batch(
        (
            {
                "condition_id": "condition-a",
                "token_id": "token-a",
                "samples": [
                    {"t": int((T - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
                    {"t": int(T.timestamp()), "p": Decimal("0.505")},
                ],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T,
                    "window_start": T - timedelta(hours=24),
                    "window_end": T,
                    "sample_count": 2,
                    "valid_until": T + timedelta(hours=24),
                },
            },
            {
                "condition_id": "condition-b",
                "token_id": "token-b",
                "samples": [
                    {"t": int((T - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")}
                ],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T - timedelta(hours=3),
                    "valid_until": T - timedelta(hours=1),
                    "sample_count": 1,
                },
            },
        )
    )

    class HistoryExchange:
        def __init__(self) -> None:
            self.history_calls: list[tuple[str, ...]] = []

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                    for condition_id in ("condition-a", "condition-b", "condition-c")
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
                    "market_id": f"market-{condition_id[-1]}",
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{condition_id[-1]}",
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
    service = PolymarketLPService(db, exchange, clock=lambda: current[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "success"
    assert first["target_count"] == 3
    assert first["request_count"] == 1
    assert first["updated_count"] == 2
    assert exchange.history_calls == [("token-b", "token-c")]
    preserved = db.lp_price_history_summary("condition-a", "token-a", now=T)
    assert preserved is not None
    assert preserved["checked_at"] == "2026-09-18T00:00:00.000000Z"
    assert preserved["sample_count"] == 2

    current[0] = T + timedelta(hours=24)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "success"
    assert second["request_count"] == 1
    assert second["updated_count"] == 3
    assert exchange.history_calls[-1] == ("token-a", "token-b", "token-c")


def test_daily_history_failure_preserves_cache_and_per_market_budget(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    current = [T]
    db = PredictionArbitrageStore(data_dir)
    db.lp_save_price_history_batch(
        (
            {
                "condition_id": "condition-a",
                "token_id": "token-a",
                "samples": [
                    {"t": int((T - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
                    {"t": int(T.timestamp()), "p": Decimal("0.505")},
                ],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": T,
                    "window_start": T - timedelta(hours=24),
                    "window_end": T,
                    "sample_count": 2,
                    "valid_until": T + timedelta(hours=24),
                },
            },
        )
    )

    class HistoryExchange:
        def __init__(self) -> None:
            self.history_calls: list[tuple[str, ...]] = []
            self.b_failures = 0
            self.include_c = False

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            condition_ids = ("condition-a", "condition-b", "condition-c") if self.include_c else ("condition-a", "condition-b")
            return {
                "state": "known",
                "complete": True,
                "checked_at": current[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                    for condition_id in condition_ids
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
                    "market_id": f"market-{condition_id[-1]}",
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{condition_id[-1]}",
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
            if "token-b" in token_ids and self.b_failures < 2:
                self.b_failures += 1
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
    service = PolymarketLPService(db, exchange, clock=lambda: current[0])
    original_a = db.lp_price_history_summary("condition-a", "token-a", now=T)
    assert original_a is not None

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "failure"
    assert exchange.history_calls == [("token-b",)]
    assert db.lp_price_history_summary("condition-a", "token-a", now=T) == original_a

    current[0] = T + timedelta(seconds=299)
    before_retry = service.refresh_price_history()
    assert exchange.history_calls == [("token-b",)]
    assert before_retry["preparation_outcome"] in {"waiting_retry", "failure"}
    assert db.lp_price_history_summary("condition-a", "token-a", now=current[0]) == original_a

    current[0] = T + timedelta(seconds=300)
    retry = service.refresh_price_history()
    assert retry["preparation_outcome"] == "failure"
    assert exchange.history_calls == [("token-b",), ("token-b",)]
    assert db.lp_price_history_summary("condition-a", "token-a", now=current[0]) == original_a

    restarted = PolymarketLPService(
        PredictionArbitrageStore(data_dir), exchange, clock=lambda: current[0]
    )
    restarted.refresh_price_history()
    assert exchange.history_calls == [("token-b",), ("token-b",)]

    current[0] = T + timedelta(hours=25)
    exchange.include_c = True
    next_day = PolymarketLPService(
        PredictionArbitrageStore(data_dir), exchange, clock=lambda: current[0]
    )
    refreshed = next_day.refresh_price_history()
    assert refreshed["request_count"] == 2
    assert exchange.history_calls[-1] == ("token-a", "token-c")
    updated_a = next_day.store.lp_price_history_summary(
        "condition-a", "token-a", now=current[0]
    )
    updated_c = next_day.store.lp_price_history_summary(
        "condition-c", "token-c", now=current[0]
    )
    assert updated_a is not None and updated_a["checked_at"] == "2026-09-19T01:00:00.000000Z"
    assert updated_c is not None and updated_c["state"] == "known"
    assert exchange.history_calls.count(("token-b",)) == 3
