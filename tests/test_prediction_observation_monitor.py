"""Focused tests for the persistent, read-only observation pool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Mapping

import open_trader.prediction_observation_monitor as observation_monitor_module
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_observation_monitor import PredictionObservationMonitor
from open_trader.prediction_read_model import prediction_state_payload
from open_trader.polymarket_monitor import PolymarketMonitor
from open_trader.relation_catalog import RelationCatalog
from test_prediction_n_leg_validation import paper_books, paper_three_way_rows
from test_mechanical_relations import complement_relation


NOW = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)


def dated_rows(**kwargs: object) -> dict[str, dict[str, object]]:
    rows = deepcopy(paper_three_way_rows(**kwargs))
    for row in rows.values():
        for endpoint in row["endpoints"]:
            endpoint["end_date"] = "2026-08-18T00:00:00Z"
    return rows


def native_row(tmp_path: Path, name: str, end_date: object) -> dict[str, object]:
    catalog = RelationCatalog(tmp_path / name)
    relation = complement_relation()
    relation = replace(
        relation,
        market=replace(relation.market, fees_enabled=False, fee_rate=Decimal("0")),
    )
    entry = catalog.ingest_mechanical_relation(relation)
    row = catalog.review_rows()[0]
    row["version_id"] = name
    row["endpoints"] = [
        {**endpoint, "end_date": end_date, "expires_at": end_date, "market_date": end_date}
        for endpoint in row["endpoints"]
    ]
    return row


def test_results_update_and_expire(tmp_path: Path) -> None:
    now = [NOW]
    book_time = [NOW]
    rows = dated_rows()
    prices = ["0.30", "0.32", "0.33"]

    def source(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        return paper_books(tuple(prices), now=book_time[0])

    monitor = PredictionObservationMonitor(
        catalog=rows,
        store=PredictionArbitrageStore(tmp_path),
        book_source=source,
        clock=lambda: now[0],
    )

    first = monitor.refresh_once()
    result = first["members"][0]["result"]
    assert result["status"] == "PASS"
    assert result["guaranteed_profit_units"] == 250_000
    assert result["execution_calls"] == 0
    first_success = result["last_success_at"]

    # Every blocked follow-up keeps the last successful economics visible for
    # audit while marking the current observation unusable.
    retained_rows = dated_rows()
    for endpoint in retained_rows["paper-three"]["endpoints"]:
        endpoint["condition_id"] = f"condition-{endpoint['contract_id']}"

    class RetentionSource:
        mode = "free"

        def observation_status(
            self, *, condition_id: str | None = None, token_id: str | None = None
        ) -> Mapping[str, object]:
            del condition_id, token_id
            return {"status": "OPEN"}

        def observation_source_metadata(
            self, *, condition_id: str | None = None, token_id: str | None = None
        ) -> Mapping[str, object]:
            del condition_id, token_id
            if self.mode == "fee":
                return {
                    "status": "OPEN",
                    "source_facts": {"fees_enabled": None},
                }
            if self.mode == "rule":
                return {
                    "status": "OPEN",
                    "source_facts": {"settlement_rules": "changed official rules"},
                }
            return {"status": "OPEN"}

    retention_clock = [NOW]
    retention_book_time = [NOW]
    retention_source = RetentionSource()

    def retained_books(token_ids: tuple[str, ...]) -> Mapping[str, object]:
        if retention_source.mode == "missing":
            return {}
        return paper_books(
            ("0.30", "0.32", "0.33"), now=retention_book_time[0]
        )

    retained_monitor = PredictionObservationMonitor(
        catalog=retained_rows,
        store=PredictionArbitrageStore(tmp_path / "retained"),
        monitor=retention_source,
        book_source=retained_books,
        clock=lambda: retention_clock[0],
    )
    retained_baseline = retained_monitor.refresh_once()["members"][0]["result"]
    retained_economics = {
        key: retained_baseline[key]
        for key in (
            "quantity_lots",
            "payout_lower_bound_units",
            "cost_upper_bound_units",
            "guaranteed_profit_units",
            "net_roi",
        )
    }
    assert retained_baseline["cost_upper_bound_units"] == 4_750_000
    assert retained_baseline["payout_lower_bound_units"] == 5_000_000
    assert retained_baseline["guaranteed_profit_units"] == 250_000
    assert retained_baseline["net_roi"] == Decimal("1") / Decimal("19")

    for mode, advance, refresh_books in (
        ("stale", 11, False),
        ("missing", 13, False),
        ("fee", 15, True),
        ("rule", 17, True),
    ):
        retention_source.mode = mode
        retention_clock[0] = NOW + timedelta(seconds=advance)
        if refresh_books:
            retention_book_time[0] = retention_clock[0]
        blocked = retained_monitor.refresh_once()["members"][0]["result"]
        assert blocked["status"] == "BLOCKED"
        assert blocked["current"] is False
        assert blocked["last_success_at"] == retained_baseline["last_success_at"]
        assert {key: blocked.get(key) for key in retained_economics} == retained_economics
        if mode == "stale":
            assert blocked["reason"] == "STALE_BOOK"
        elif mode == "missing":
            assert blocked["reason"] == "MISSING_BOOKS"
        elif mode == "fee":
            assert blocked["reason"] == "UNKNOWN_FEE_FACTS"
        else:
            assert blocked["reason"] == "SOURCE_RULES_CHANGED"

    prices[:] = ["0.31", "0.32", "0.33"]
    now[0] += timedelta(seconds=1)
    book_time[0] = now[0]
    throttled = monitor.refresh_once()
    throttled_result = throttled["members"][0]["result"]
    assert throttled_result["guaranteed_profit_units"] == 250_000
    assert throttled_result["last_success_at"] == first_success

    prices[:] = ["0.35", "0.35", "0.35"]
    now[0] += timedelta(seconds=1)
    book_time[0] = now[0]
    second = monitor.refresh_once()
    result = second["members"][0]["result"]
    assert result["status"] == "PASS"
    assert result["guaranteed_profit_units"] == -250_000
    assert result["last_success_at"] != first_success

    now[0] += timedelta(seconds=11)
    stale = monitor.refresh_once()
    result = stale["members"][0]["result"]
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "STALE_BOOK"
    assert result["last_success_at"] == second["members"][0]["result"]["last_success_at"]
    assert result["execution_calls"] == 0

    for reason, row_kwargs, book_kwargs in (
        ("UNKNOWN_FEE_FACTS", {"fees_enabled": None}, {}),
        ("UNKNOWN_ORDER_RULES", {}, {"omit_rules_for": "b"}),
        ("INSUFFICIENT_DEPTH", {}, {"depth": "4"}),
    ):
        case_rows = dated_rows(**row_kwargs)
        case_monitor = PredictionObservationMonitor(
            catalog=case_rows,
            store=PredictionArbitrageStore(tmp_path / reason),
            book_source=lambda token_ids, book_kwargs=book_kwargs: paper_books(
                ("0.30", "0.32", "0.33"), now=NOW, **book_kwargs
            ),
            clock=lambda: NOW,
        )
        blocked = case_monitor.refresh_once()["members"][0]["result"]
        assert blocked["status"] == "BLOCKED"
        assert blocked["reason"] == reason
        assert blocked["execution_calls"] == 0


def test_source_fee_facts_reprice_and_rule_drift_blocks(tmp_path: Path) -> None:
    now = [NOW]
    row = native_row(tmp_path, "source-facts", "2026-08-18T00:00:00Z")

    class SourceFacts:
        def __init__(self) -> None:
            self.facts: dict[str, object] = {
                "fees_enabled": False,
                "fee_rate": "0",
            }

        def observation_status(
            self, *, condition_id: str | None = None, token_id: str | None = None
        ) -> Mapping[str, object]:
            del condition_id, token_id
            return {"status": "OPEN"}

        def observation_source_metadata(
            self, *, condition_id: str | None = None, token_id: str | None = None
        ) -> Mapping[str, object]:
            del condition_id, token_id
            return {
                "status": "OPEN",
                "source_facts": deepcopy(self.facts),
            }

    source = SourceFacts()

    def books(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        del token_ids
        return {
            token: {
                "asks": [{"price": price, "size": "20"}],
                "confirmed_at": now[0],
                "minimum_order_size": "1",
                "tick_size": "0.01",
            }
            for token, price in (("yes-1", "0.40"), ("no-1", "0.45"))
        }

    monitor = PredictionObservationMonitor(
        catalog={"source-facts": row},
        store=PredictionArbitrageStore(tmp_path / "source-facts-store"),
        monitor=source,
        book_source=books,
        clock=lambda: now[0],
    )
    free = monitor.refresh_once()["members"][0]["result"]
    assert free["fees"]["status"] == "FREE"
    assert free["cost_upper_bound_units"] == 850_000

    source.facts = {
        "fees_enabled": True,
        "fee_rate": "0.04",
        "fee_exponent": 1,
        "taker_only": True,
    }
    now[0] += timedelta(seconds=2)
    charging = monitor.refresh_once()["members"][0]["result"]
    assert charging["fees"]["status"] == "CHARGING"
    assert charging["cost_upper_bound_units"] == 869_500
    assert charging["cost_upper_bound_units"] > free["cost_upper_bound_units"]

    source.facts["settlement_rules"] = "changed official rules"
    now[0] += timedelta(seconds=2)
    drifted = monitor.refresh_once()["members"][0]["result"]
    assert drifted["status"] == "BLOCKED"
    assert drifted["reason"] == "SOURCE_RULES_CHANGED"


def test_official_source_fees_and_rules_reprice_observation(tmp_path: Path) -> None:
    now = [NOW]
    source = {
        "rate": Decimal("0"),
        "exponent": 1,
        "taker_only": True,
        "fees_enabled": False,
        "fee_schedule_fields": ("rate", "exponent", "taker_only"),
        "description": "official index",
    }

    class EmptyStream:
        def __aiter__(self) -> "EmptyStream":
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

        async def close(self) -> None:
            return None

    class GammaClient:
        def __init__(self) -> None:
            self.status_calls: list[dict[str, object]] = []
            self.book_calls: list[tuple[str, ...]] = []

        def market(self, *, closed: bool) -> SimpleNamespace:
            schedule_fields = source["fee_schedule_fields"]
            schedule = (
                SimpleNamespace(
                    **{
                        name: source[name]
                        for name in schedule_fields
                    }
                )
                if schedule_fields is not None
                else None
            )
            return SimpleNamespace(
                condition_id="condition-1",
                id="market-1",
                description=source["description"],
                state=SimpleNamespace(active=not closed, closed=closed),
                outcomes=[
                    SimpleNamespace(label="YES", token_id="yes-1"),
                    SimpleNamespace(label="NO", token_id="no-1"),
                ],
                trading=SimpleNamespace(
                    fees_enabled=source["fees_enabled"],
                    fee_schedule=schedule,
                    minimum_order_size=Decimal("5"),
                    minimum_tick_size=Decimal("0.01"),
                ),
            )

        async def list_events(self, **kwargs: object) -> list[object]:
            del kwargs
            return []

        async def list_markets(self, **kwargs: object) -> list[object]:
            self.status_calls.append(dict(kwargs))
            if kwargs.get("closed") is True:
                return []
            return [self.market(closed=kwargs.get("closed") is True)]

        async def get_order_books(self, *, token_ids: list[str]) -> list[object]:
            self.book_calls.append(tuple(token_ids))
            prices = {"yes-1": "0.40", "no-1": "0.55"}
            return [
                SimpleNamespace(
                    asset_id=token,
                    timestamp=now[0],
                    asks=[SimpleNamespace(price=prices[token], size=Decimal("20"))],
                        bids=[SimpleNamespace(price="0.39", size=Decimal("20"))],
                        minimum_order_size=Decimal("5"),
                        minimum_tick_size=Decimal("0.01"),
                )
                for token in token_ids
            ]

        def subscribe(self, _spec: object) -> EmptyStream:
            return EmptyStream()

    class Trading:
        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "wallet": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
                "checked_at": now[0],
            }

    catalog = RelationCatalog(tmp_path / "catalog")
    relation = replace(
        complement_relation(),
        market=replace(
            complement_relation().market,
            fees_enabled=True,
            fee_rate=Decimal("0.02"),
        ),
    )
    catalog.ingest_mechanical_relation(relation)
    catalog_row = catalog.review_rows()[0]
    catalog.ingest(
        {
            "discovery_source": catalog_row["discovery_source"],
            "discovered_at": catalog_row["discovered_at"],
            "relation_type": catalog_row["relation_type"],
            "semantics": {"statement": catalog_row["statement"]},
            "markets": [
                {
                    **endpoint,
                    "fee_exponent": 1,
                    "taker_only": True,
                }
                for endpoint in catalog_row["endpoints"]
            ],
            "model": {**catalog_row["model"], "completeness": "COMPLETE"},
            "source_evidence": [{"event_id": "event-1", "relation_type": "NATIVE_COMPLEMENT"}],
        }
    )
    client = GammaClient()
    source_monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "source-store"),
        trading=Trading(),
        public_client_factory=lambda: client,
        clock=lambda: now[0],
        relation_discovery=None,
    )
    source_monitor.set_observation_tokens(("yes-1", "no-1"))
    source_monitor.set_observation_conditions(("condition-1",))
    source_monitor.refresh_once()
    free_source = source_monitor.observation_source_metadata(
        condition_id="condition-1"
    )
    assert isinstance(free_source, Mapping)

    def fresh_books(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        prices = {"yes-1": "0.40", "no-1": "0.55"}
        return {
            token: {
                "token_id": token,
                "asks": [{"price": prices[token], "size": "20"}],
                "bids": [{"price": "0.39", "size": "20"}],
                "confirmed_at": now[0],
                "minimum_order_size": "5",
                "tick_size": "0.01",
            }
            for token in token_ids
        }

    observation = PredictionObservationMonitor(
        catalog=catalog,
        store=PredictionArbitrageStore(tmp_path / "observation-store"),
        monitor=source_monitor,
        book_source=fresh_books,
        clock=lambda: now[0],
    )

    free = observation.refresh_once()["members"][0]["result"]
    assert free["fees"]["status"] == "FREE"
    assert free["cost_upper_bound_units"] == 4_750_000

    source["fees_enabled"] = True
    source["rate"] = Decimal("0.04")
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    charging = observation.refresh_once()["members"][0]["result"]
    assert charging["fees"]["status"] == "CHARGING"
    assert charging["cost_upper_bound_units"] == 4_847_500
    assert charging["guaranteed_profit_units"] == 152_500
    charging_source = source_monitor.observation_source_metadata(
        condition_id="condition-1"
    )
    assert isinstance(charging_source, Mapping)
    assert charging_source["source_fingerprint"] != free_source["source_fingerprint"]

    # An optional SDK fee flag may be present but unset.  That source pass is
    # also unknown and must not reuse the old charging schedule from catalog.
    source["fees_enabled"] = None
    source["fee_schedule_fields"] = None
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    optional_result = observation.refresh_once()["members"][0]["result"]
    assert optional_result["status"] == "BLOCKED"
    assert optional_result["reason"] == "UNKNOWN_FEE_FACTS"
    assert optional_result["current"] is False
    assert optional_result["cost_upper_bound_units"] == charging["cost_upper_bound_units"]
    optional_coverage = observation.snapshot()["coverage"]
    assert optional_coverage["fresh"] == 0
    assert optional_coverage["positive"] == 0
    assert optional_coverage["non_positive"] == 0

    # A currently enabled source with no fee schedule must not inherit the
    # catalog's older known charging schedule.  It is blocked until the
    # complete current schedule returns, while the last economics stay stale.
    source["fees_enabled"] = True
    source["fee_schedule_fields"] = None
    now[0] += timedelta(seconds=301)
    missing_fee = source_monitor.refresh_once()
    missing_result = observation.refresh_once()["members"][0]["result"]
    assert missing_result["status"] == "BLOCKED"
    assert missing_result["reason"] == "UNKNOWN_FEE_FACTS"
    assert missing_result["current"] is False
    assert missing_result["cost_upper_bound_units"] == charging["cost_upper_bound_units"]
    missing_coverage = observation.snapshot()["coverage"]
    assert missing_coverage["fresh"] == 0
    assert missing_coverage["positive"] == 0
    assert missing_coverage["non_positive"] == 0

    # A partial current schedule is equally unknown; each required fee fact
    # must arrive from the same current source pass.
    source["fee_schedule_fields"] = ("rate",)
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    partial_result = observation.refresh_once()["members"][0]["result"]
    assert partial_result["status"] == "BLOCKED"
    assert partial_result["reason"] == "UNKNOWN_FEE_FACTS"
    assert partial_result["current"] is False

    # Restoring the complete current schedule returns the hand-worked
    # charging economics.
    source["fee_schedule_fields"] = ("rate", "exponent", "taker_only")
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    restored = observation.refresh_once()["members"][0]["result"]
    assert restored["status"] == "PASS"
    assert restored["cost_upper_bound_units"] == 4_847_500
    assert restored["guaranteed_profit_units"] == 152_500

    # A complete schedule without an explicit boolean fee flag is still an
    # unknown current fee group; it must not inherit the catalog's enabled
    # flag or publish current economics.
    source["fees_enabled"] = None
    source["fee_schedule_fields"] = ("rate", "exponent", "taker_only")
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    optional_complete_result = observation.refresh_once()["members"][0]["result"]
    assert optional_complete_result["status"] == "BLOCKED"
    assert optional_complete_result["reason"] == "UNKNOWN_FEE_FACTS"
    assert optional_complete_result["current"] is False
    assert optional_complete_result["cost_upper_bound_units"] == restored["cost_upper_bound_units"]
    optional_complete_coverage = observation.snapshot()["coverage"]
    assert optional_complete_coverage["fresh"] == 0
    assert optional_complete_coverage["ranking"] == []

    # An explicit fee-free source replaces the old charging group completely.
    source["fees_enabled"] = False
    source["fee_schedule_fields"] = None
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    explicit_free = observation.refresh_once()["members"][0]["result"]
    assert explicit_free["status"] == "PASS"
    assert explicit_free["current"] is True
    assert explicit_free["fees"]["status"] == "FREE"
    assert explicit_free["cost_upper_bound_units"] == 4_750_000
    assert explicit_free["payout_lower_bound_units"] == 5_000_000
    assert explicit_free["guaranteed_profit_units"] == 250_000

    # Requiring a complete true schedule also proves recovery after the
    # explicit free replacement.
    source["fees_enabled"] = True
    source["fee_schedule_fields"] = ("rate", "exponent", "taker_only")
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    restored_after_replacement = observation.refresh_once()["members"][0]["result"]
    assert restored_after_replacement["status"] == "PASS"
    assert restored_after_replacement["fees"]["status"] == "CHARGING"
    assert restored_after_replacement["cost_upper_bound_units"] == 4_847_500
    assert restored_after_replacement["guaranteed_profit_units"] == 152_500

    source["description"] = "changed official rules"
    now[0] += timedelta(seconds=301)
    source_monitor.refresh_once()
    drifted = observation.refresh_once()["members"][0]["result"]
    assert drifted["status"] == "BLOCKED"
    assert drifted["reason"] == "SOURCE_RULES_CHANGED"


def test_shared_pool_uses_end_date_and_keeps_members(tmp_path: Path) -> None:
    now = [NOW]
    rows = {
        "A": native_row(tmp_path, "A", "2026-08-16T03:00:00Z"),
        "B": dated_rows()["paper-three"],
        "C": native_row(tmp_path, "C", "2026-08-19T00:00:00Z"),
        "D": dated_rows()["paper-three"],
    }
    rows["B"]["version_id"] = "B"
    rows["B"]["endpoints"] = [
        {
            **endpoint,
            "end_date": date,
            "expires_at": date,
            "market_date": date,
        }
        for endpoint, date in zip(
            rows["B"]["endpoints"],
            (
                "2026-08-17T23:00:00+00:00",
                "2026-08-18T01:00:00+02:00",
                "2026-08-18T00:30:00Z",
            ),
            strict=True,
        )
    ]
    rows["D"] = deepcopy(rows["C"])
    rows["D"]["version_id"] = "D"
    rows["D"]["endpoints"] = [
        {key: value for key, value in endpoint.items() if key not in {"end_date", "expires_at", "market_date"}}
        for endpoint in rows["D"]["endpoints"]
    ]
    monitor = PredictionObservationMonitor(
        catalog=rows,
        store=PredictionArbitrageStore(tmp_path / "pool"),
        book_source=lambda token_ids: {},
        clock=lambda: now[0],
        pool_limit=2,
    )

    first = monitor.refresh_once()
    assert [member["identity"] for member in first["members"]] == ["A", "B"]
    assert first["members"][1]["end_date"] == "2026-08-18T00:30:00+00:00"
    assert first["members"][0]["overdue"] is False
    assert first["members"][0]["capital_release_status"] == "KNOWN"
    assert first["members"][1]["capital_release_status"] == "UNKNOWN"
    assert "D" in first["coverage"]["exclusions"]

    now[0] = datetime(2026, 8, 16, 5, 0, tzinfo=UTC)
    crossed = monitor.refresh_once()
    assert [member["identity"] for member in crossed["members"]] == ["A", "B"]
    assert crossed["members"][0]["overdue"] is True
    assert crossed["members"][0]["capital_release_status"] == "KNOWN"
    assert crossed["members"][1]["capital_release_status"] == "UNKNOWN"
    assert crossed["members"][0]["result"]["status"] == "BLOCKED"
    assert crossed["members"][0]["result"]["reason"] == "MISSING_BOOKS"

    rows["E"] = deepcopy(rows["C"])
    rows["E"]["version_id"] = "E"
    rows["E"]["endpoints"] = [
        {**endpoint, "end_date": "2026-08-15T00:00:00Z"}
        for endpoint in rows["E"]["endpoints"]
    ]
    unchanged = monitor.refresh_once()
    assert [member["identity"] for member in unchanged["members"]] == ["A", "B"]

    rows["A"]["source_status"] = "RESOLVED"
    replaced = monitor.refresh_once()
    assert [member["identity"] for member in replaced["members"]] == ["B", "E"]

    # Economics are reported for the retained member; a negative paper result
    # does not turn the bounded pool into a profit-ranked replacement list.
    negative_rows = {"A": deepcopy(dated_rows()["paper-three"])}
    negative_rows["A"]["version_id"] = "A"
    negative_monitor = PredictionObservationMonitor(
        catalog=negative_rows,
        store=PredictionArbitrageStore(tmp_path / "negative"),
        book_source=lambda token_ids: paper_books(("0.35", "0.35", "0.35"), now=NOW),
        clock=lambda: NOW,
        pool_limit=1,
    )
    negative = negative_monitor.refresh_once()
    assert negative["members"][0]["result"]["guaranteed_profit_units"] < 0
    negative_rows["E"] = deepcopy(negative_rows["A"])
    negative_rows["E"]["version_id"] = "E"
    negative_rows["E"]["endpoints"] = [
        {**endpoint, "end_date": "2026-08-15T00:00:00Z"}
        for endpoint in negative_rows["E"]["endpoints"]
    ]
    retained_negative = negative_monitor.refresh_once()
    assert [member["identity"] for member in retained_negative["members"]] == ["A"]


def test_changed_generation_discards_late_result(tmp_path: Path) -> None:
    now = [NOW]
    old = dated_rows()
    old["paper-three"]["version_id"] = "old-version"
    current: dict[str, dict[str, object]] = {"A": old["paper-three"]}
    replacement = dated_rows()
    replacement["paper-three"]["version_id"] = "new-version"
    replacement["paper-three"]["endpoints"] = [
        {**endpoint, "end_date": "2026-08-19T00:00:00Z"}
        for endpoint in replacement["paper-three"]["endpoints"]
    ]
    revoked = deepcopy(replacement["paper-three"])
    revoked["lifecycle"] = "REVOKED"
    revoked["status"] = "REVOKED"
    generation = [1]
    late_action: list[str | None] = ["replace"]

    def candidates() -> dict[str, object]:
        return {
            "generation": generation[0],
            "generation_fingerprint": f"generation-{generation[0]}",
            "rows": current,
        }

    def books(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        if late_action[0] == "replace":
            current["A"] = replacement["paper-three"]
            generation[0] = 2
            late_action[0] = None
        elif late_action[0] == "revoke":
            current["A"] = revoked
            generation[0] = 3
            late_action[0] = None
        return paper_books(("0.30", "0.32", "0.33"), now=now[0])

    monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=candidates,
        store=PredictionArbitrageStore(tmp_path),
        book_source=books,
        clock=lambda: now[0],
    )
    monitor.refresh_once()
    discarded = monitor.snapshot()["members"][0]["result"]
    assert discarded["reason"] == "CATALOG_CHANGED"
    assert discarded["current"] is False
    assert discarded["version_id"] == "old-version"

    now[0] += timedelta(seconds=2)
    refreshed = monitor.refresh_once()
    assert refreshed["members"][0]["version_id"] == "new-version"
    assert refreshed["members"][0]["result"]["status"] == "PASS"

    # A delayed quote must not revive a result after the catalog withdraws
    # the member; the next pass releases the slot and a later valid version
    # may enter again as a fresh observation.
    late_action[0] = "revoke"
    generation[0] = 2
    now[0] += timedelta(seconds=2)
    revoked_late = monitor.refresh_once()
    revoked_result = revoked_late["members"][0]["result"]
    assert revoked_result["reason"] == "CATALOG_CHANGED"
    assert revoked_result["current"] is False
    now[0] += timedelta(seconds=2)
    released = monitor.refresh_once()
    assert released["members"] == []
    current["A"] = replacement["paper-three"]
    generation[0] = 4
    now[0] += timedelta(seconds=2)
    reentered = monitor.refresh_once()
    assert reentered["members"][0]["result"]["status"] == "PASS"


def test_background_watermark_failure_blocks_late_result(tmp_path: Path) -> None:
    """A failed final catalog watermark cannot publish delayed quotes as current."""

    catalog = RelationCatalog(tmp_path / "catalog")
    relation = replace(
        complement_relation(),
        market=replace(
            complement_relation().market,
            fees_enabled=False,
            fee_rate=Decimal("0"),
        ),
    )
    catalog.ingest_mechanical_relation(relation)
    original_meta = catalog.observation_generation_meta
    fail_meta = [False]

    def generation_meta() -> dict[str, object]:
        if fail_meta[0]:
            raise OSError("catalog watermark unavailable")
        return original_meta()

    catalog.observation_generation_meta = generation_meta  # type: ignore[method-assign]

    class DelayedBooks:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def __call__(self, token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
            self.started.set()
            while not self.release.is_set():
                time.sleep(0.01)
            prices = {"yes-1": "0.40", "no-1": "0.45"}
            return {
                token: {
                    "token_id": token,
                    "asks": [{"price": prices[token], "size": "20"}],
                    "bids": [],
                    "confirmed_at": NOW,
                    "minimum_order_size": "1",
                    "tick_size": "0.01",
                }
                for token in token_ids
            }

    books = DelayedBooks()
    monitor = PredictionObservationMonitor(
        catalog=catalog,
        store=PredictionArbitrageStore(tmp_path / "store"),
        book_source=books,
        clock=lambda: NOW,
    )
    monitor.start()
    try:
        assert books.started.wait(2)
        replacement = replace(
            relation,
            market=replace(relation.market, rules="changed official rules"),
        )
        catalog.ingest_mechanical_relation(replacement)
        fail_meta[0] = True
        books.release.set()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = monitor.snapshot()
            members = snapshot.get("members")
            if isinstance(members, list) and members and members[0].get("result"):
                break
            time.sleep(0.01)
        snapshot = monitor.snapshot()
        assert snapshot["status"] in {"ERROR", "STALE", "UNKNOWN"}
        result = snapshot["members"][0]["result"]
        assert result["status"] != "PASS"
        assert result["current"] is not True
    finally:
        books.release.set()
        monitor.stop()

    # A fresh public catalog read failure must invalidate the previously
    # published economics, even though their historical amounts remain
    # available for audit.  The catalog reader below opens a new SQLite
    # connection for each public snapshot so the injected external failure
    # exercises the actual storage boundary rather than _read_catalog.
    catalog_data_dir = tmp_path / "public-catalog-failure"
    stable_catalog = RelationCatalog(catalog_data_dir)
    stable_catalog.ingest_mechanical_relation(relation)

    def fresh_catalog_snapshot() -> dict[str, object]:
        return RelationCatalog(catalog_data_dir).observation_snapshot()

    def public_books(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        prices = {"yes-1": "0.40", "no-1": "0.45"}
        return {
            token: {
                "token_id": token,
                "asks": [{"price": prices[token], "size": "20"}],
                "bids": [],
                "confirmed_at": NOW,
                "minimum_order_size": "1",
                "tick_size": "0.01",
            }
            for token in token_ids
        }

    public_store = PredictionArbitrageStore(tmp_path / "public-catalog-store")
    public_monitor = PredictionObservationMonitor(
        catalog=stable_catalog,
        candidate_source=fresh_catalog_snapshot,
        store=public_store,
        book_source=public_books,
        clock=lambda: NOW,
    )
    baseline = public_monitor.refresh_once()
    baseline_result = baseline["members"][0]["result"]
    assert baseline_result["status"] == "PASS"
    assert baseline_result["current"] is True
    assert baseline["coverage"]["fresh"] == 1
    assert baseline["coverage"]["positive"] == 1

    original_connect = sqlite3.connect
    catalog_read_failure = [False]

    def faulting_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if catalog_read_failure[0] and str(database) == str(stable_catalog.path):
            raise sqlite3.OperationalError("catalog SQLite read unavailable")
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = faulting_connect  # type: ignore[assignment]
    try:
        catalog_read_failure[0] = True
        failed = public_monitor.refresh_once()
    finally:
        sqlite3.connect = original_connect  # type: ignore[assignment]
    failed_result = failed["members"][0]["result"]
    assert failed["status"] in {"ERROR", "UNKNOWN"}
    assert failed_result["current"] is False
    assert failed_result["cost_upper_bound_units"] == baseline_result["cost_upper_bound_units"]
    assert failed["coverage"]["fresh"] == 0
    assert failed["coverage"]["positive"] == 0
    assert failed["coverage"]["non_positive"] == 0
    assert failed["results"] == []
    assert failed["coverage"]["ranking"] == []
    for name in (
        "fresh_count",
        "computed_count",
        "positive_count",
        "non_positive_count",
        "qualified_count",
        "unknown_count",
    ):
        assert failed["coverage"][name] == 0
    assert failed["coverage"]["blocked_count"] == failed["coverage"]["pool_count"]
    for name in (
        "pool_count",
        "capacity",
        "latest_count",
        "waiting_count",
        "excluded_count",
        "native_count",
        "three_way_count",
        "subscribed_tokens",
        "all_leg_subscribed_count",
    ):
        assert failed["coverage"][name] == baseline["coverage"][name]
    failed_api = prediction_state_payload(
        store=public_store,
        monitor=None,
        execution=None,
        csrf_token="csrf-token",
        observation_snapshot=failed,
    )
    api_coverage = failed_api["n_leg_coverage"]
    for name in (
        "fresh_count",
        "computed_count",
        "positive_count",
        "non_positive_count",
        "qualified_count",
        "unknown_count",
        "blocked_count",
    ):
        assert api_coverage[name] == failed["coverage"][name]
    assert api_coverage["ranking"] == []
    assert api_coverage["capacity"] == failed["coverage"]["capacity"]
    assert all(
        item.get("result", {}).get("current") is not True
        for item in [*failed["members"], *failed["latest"]]
        if isinstance(item.get("result"), Mapping)
    )

    recovered = public_monitor.refresh_once()
    assert recovered["members"][0]["result"]["current"] is True

    # A delayed public refresh must also fail closed when the catalog changes
    # before its final read.  The final reader is the same external SQLite
    # boundary, so a missing final snapshot cannot publish the old quote.
    delayed_started = threading.Event()
    delayed_release = threading.Event()

    def delayed_public_books(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        delayed_started.set()
        while not delayed_release.wait(0.01):
            pass
        return public_books(token_ids)

    delayed_monitor = PredictionObservationMonitor(
        catalog=stable_catalog,
        candidate_source=fresh_catalog_snapshot,
        store=PredictionArbitrageStore(tmp_path / "delayed-public-store"),
        book_source=delayed_public_books,
        clock=lambda: NOW,
    )
    refresh_thread = threading.Thread(target=delayed_monitor.refresh_once)
    refresh_thread.start()
    try:
        assert delayed_started.wait(2)
        replacement = replace(
            relation,
            market=replace(relation.market, rules="changed official rules"),
        )
        stable_catalog.ingest_mechanical_relation(replacement)
        catalog_read_failure[0] = True
        sqlite3.connect = faulting_connect  # type: ignore[assignment]
        delayed_release.set()
        refresh_thread.join(2)
        late_snapshot = delayed_monitor.snapshot()
    finally:
        catalog_read_failure[0] = False
        sqlite3.connect = original_connect  # type: ignore[assignment]
        delayed_release.set()
        refresh_thread.join(2)
    late_result = late_snapshot["members"][0]["result"]
    assert late_snapshot["status"] in {"ERROR", "UNKNOWN", "STALE"}
    assert late_result["status"] != "PASS"
    assert late_result["current"] is not True


def test_snapshot_does_not_wait_for_catalog_copy(tmp_path: Path) -> None:
    rows = dated_rows()
    copy_started = threading.Event()
    release_copy = threading.Event()
    block_catalog = [False]
    blocked_once = [False]

    class BlockingCatalog(dict[str, object]):
        def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
            copy_started.set()
            if not release_copy.wait(timeout=5):
                raise AssertionError("catalog copy was not released")
            return {
                key: deepcopy(value, memo)
                for key, value in self.items()
            }

    def source() -> dict[str, object]:
        if block_catalog[0] and not blocked_once[0]:
            blocked_once[0] = True
            return BlockingCatalog(rows)
        return rows

    monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=source,
        store=PredictionArbitrageStore(tmp_path / "store"),
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"), now=NOW
        ),
        clock=lambda: NOW,
    )
    baseline = monitor.refresh_once()
    block_catalog[0] = True

    with ThreadPoolExecutor(max_workers=2) as workers:
        refresh_future = workers.submit(monitor.refresh_once)
        try:
            assert copy_started.wait(timeout=2)
            snapshot_future = workers.submit(monitor.snapshot)
            observed = snapshot_future.result(timeout=1)
        finally:
            release_copy.set()
        refresh_future.result(timeout=5)

    assert observed == baseline


def test_background_refresh_cannot_overwrite_newer_explicit_refresh(
    tmp_path: Path,
) -> None:
    rows = dated_rows()
    newer_rows = deepcopy(rows)
    newer_rows["paper-three"]["version_id"] = "generation-2"
    now = [NOW]
    copy_started = threading.Event()
    release_copy = threading.Event()
    newer_source_started = threading.Event()
    source_phase = ["baseline"]

    class BlockingCatalog(dict[str, object]):
        def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
            copy_started.set()
            if not release_copy.wait(timeout=5):
                raise AssertionError("background catalog copy was not released")
            return {key: deepcopy(value, memo) for key, value in self.items()}

    def source() -> dict[str, object]:
        if source_phase[0] == "baseline":
            return rows
        if source_phase[0] == "background":
            source_phase[0] = "explicit"
            return BlockingCatalog(rows)
        newer_source_started.set()
        return newer_rows

    monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=source,
        store=PredictionArbitrageStore(tmp_path / "store"),
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"), now=now[0]
        ),
        clock=lambda: now[0],
    )
    baseline = monitor.refresh_once()
    now[0] = NOW + timedelta(seconds=6)
    source_phase[0] = "background"

    monitor.start()
    try:
        assert copy_started.wait(timeout=2)

        explicit_started = threading.Event()
        explicit_finished = threading.Event()

        def explicit_refresh() -> dict[str, object]:
            explicit_started.set()
            try:
                return monitor.refresh_once()
            finally:
                explicit_finished.set()

        with ThreadPoolExecutor(max_workers=1) as workers:
            explicit_future = workers.submit(explicit_refresh)
            assert explicit_started.wait(timeout=2)
            if newer_source_started.wait(timeout=2):
                assert explicit_finished.wait(timeout=2)
            release_copy.set()
            explicit = explicit_future.result(timeout=5)
    finally:
        release_copy.set()
        monitor.stop()

    assert explicit["generation_fingerprint"] != baseline["generation_fingerprint"]
    assert monitor.snapshot()["generation_fingerprint"] == explicit["generation_fingerprint"]


def test_stop_skips_queued_background_refresh(tmp_path: Path) -> None:
    refresh_calls = 0
    queued = threading.Event()
    refresh_lock = threading.Lock()

    class SignalingLock:
        def acquire(self) -> bool:
            queued.set()
            return refresh_lock.acquire()

        def release(self) -> None:
            refresh_lock.release()

        def __enter__(self) -> SignalingLock:
            self.acquire()
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self.release()

    def source() -> dict[str, object]:
        nonlocal refresh_calls
        refresh_calls += 1
        return {}

    monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=source,
        store=PredictionArbitrageStore(tmp_path / "store"),
        clock=lambda: NOW,
    )
    monitor._refresh_lock = SignalingLock()  # type: ignore[assignment]
    refresh_lock.acquire()
    monitor.start()
    stop_thread: threading.Thread | None = None
    try:
        assert queued.wait(timeout=2)
        stop_thread = threading.Thread(target=monitor.stop)
        stop_thread.start()
        assert monitor._stop.wait(timeout=2)
    finally:
        refresh_lock.release()
        if stop_thread is not None:
            stop_thread.join(timeout=5)
        monitor.stop()

    assert refresh_calls == 0


def test_snapshot_does_not_wait_for_publication_build(
    tmp_path: Path, monkeypatch
) -> None:
    rows = dated_rows()
    generation = [1]
    build_started = threading.Event()
    release_build = threading.Event()
    block_once = [True]
    original_display_fields = observation_monitor_module._display_fields

    def source() -> dict[str, object]:
        return {
            "generation": generation[0],
            "generation_fingerprint": f"generation-{generation[0]}",
            "rows": rows,
        }

    def blocking_display_fields(
        row: Mapping[str, object],
        *,
        identity: str,
        candidate: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if block_once[0]:
            block_once[0] = False
            build_started.set()
            if not release_build.wait(timeout=5):
                raise AssertionError("publication build was not released")
        return original_display_fields(row, identity=identity, candidate=candidate)

    monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=source,
        store=PredictionArbitrageStore(tmp_path / "store"),
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"), now=NOW
        ),
        clock=lambda: NOW,
    )
    initial = monitor.refresh_once()
    generation[0] = 2
    rows["paper-three"]["version_id"] = "generation-2"
    monkeypatch.setattr(observation_monitor_module, "_display_fields", blocking_display_fields)

    with ThreadPoolExecutor(max_workers=2) as workers:
        refresh_future = workers.submit(monitor.refresh_once)
        assert build_started.wait(timeout=2)
        snapshot_future = workers.submit(monitor.snapshot)
        try:
            observed = snapshot_future.result(timeout=1)
        finally:
            release_build.set()
        refreshed = refresh_future.result(timeout=5)

    assert observed == initial
    assert observed["generation"] == 1
    assert observed["generation_fingerprint"] == "generation-1"
    assert refreshed["generation"] == 2
    assert refreshed["generation_fingerprint"] == "generation-2"
    assert monitor.snapshot()["generation_fingerprint"] == "generation-2"


def test_restart_restores_members_with_fresh_books(tmp_path: Path) -> None:
    """SQLite membership survives restart, while source status and books revalidate."""

    class ExternalSource:
        def __init__(self, statuses: Mapping[str, Mapping[str, object]]) -> None:
            self.statuses = dict(statuses)
            self.status_calls: list[str] = []
            self.fail = False

        def observation_status(
            self, *, condition_id: str | None = None, token_id: str | None = None
        ) -> Mapping[str, object] | None:
            key = condition_id or token_id
            if key is None:
                return None
            self.status_calls.append(key)
            if self.fail:
                raise OSError("source unavailable")
            return self.statuses.get(key)

    def native_books(confirmed_at: datetime) -> dict[str, dict[str, object]]:
        return {
            token: {
                "token_id": token,
                "asks": [{"price": price, "size": "20"}],
                "bids": [],
                "confirmed_at": confirmed_at,
                "minimum_order_size": "1",
                "tick_size": "0.01",
            }
            for token, price in (
                ("yes-1", "0.40"),
                ("no-1", "0.45"),
                ("yes-e", "0.40"),
                ("no-e", "0.45"),
            )
        }

    first_rows = {
        "A": native_row(tmp_path, "restart-a", "2026-08-18T00:00:00Z"),
        "B": dated_rows()["paper-three"],
    }
    first_rows["A"]["version_id"] = "A"
    first_rows["B"]["version_id"] = "B"
    first_rows["B"]["endpoints"] = [
        {**endpoint, "condition_id": f"condition-{contract}"}
        for endpoint, contract in zip(
            first_rows["B"]["endpoints"], ("b-a", "b-b", "b-c"), strict=True
        )
    ]
    store_path = tmp_path / "restart-store"

    def books(confirmed_at: datetime) -> dict[str, dict[str, object]]:
        return {
            **native_books(confirmed_at),
            **paper_books(("0.30", "0.32", "0.33"), now=confirmed_at),
        }

    first = PredictionObservationMonitor(
        catalog=first_rows,
        store=PredictionArbitrageStore(store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
    )
    initial = first.refresh_once()
    assert [member["identity"] for member in initial["members"]] == ["A", "B"]

    replacement = deepcopy(first_rows["A"])

    def rename_native(value: object) -> object:
        if isinstance(value, str):
            return (
                value.replace("yes-1", "yes-e")
                .replace("no-1", "no-e")
                .replace("condition-1", "condition-e")
            )
        if isinstance(value, dict):
            return {key: rename_native(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename_native(item) for item in value]
        return value

    replacement = rename_native(replacement)
    assert isinstance(replacement, dict)
    replacement["version_id"] = "E"
    replacement["endpoints"] = [
        {**endpoint, "end_date": "2026-08-17T00:00:00Z"}
        for endpoint in replacement["endpoints"]
    ]
    restart_rows = {"A": first_rows["A"], "B": first_rows["B"], "E": replacement}
    class EmptyStream:
        def __aiter__(self) -> "EmptyStream":
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class PublicStatusClient:
        def __init__(
            self,
            statuses: Mapping[str, bool],
            book_rows: Mapping[str, Mapping[str, object]],
        ) -> None:
            self.statuses = dict(statuses)
            self.book_rows = dict(book_rows)
            self.list_markets_calls: list[dict[str, object]] = []

        async def list_events(self, **kwargs: object) -> list[object]:
            del kwargs
            return []

        async def list_markets(self, **kwargs: object) -> list[object]:
            self.list_markets_calls.append(dict(kwargs))
            requested = {str(item) for item in kwargs.get("condition_ids", ())}
            closed = kwargs.get("closed") is True
            return [
                {
                    "condition_id": condition,
                    "state": {"active": not is_closed, "closed": is_closed},
                    "outcomes": [],
                    # This public fixture explicitly proves the known
                    # fee-free path.  An omitted fee group is UNKNOWN under
                    # the source-facts contract, so it must not mask the
                    # restart test's intended STALE_BOOK result.
                    "trading": {"fees_enabled": False, "fee_schedule": None},
                }
                for condition, is_closed in self.statuses.items()
                if condition in requested and is_closed is closed
            ]

        async def get_order_books(
            self, *, token_ids: list[str]
        ) -> list[Mapping[str, object]]:
            return [self.book_rows[token] for token in token_ids if token in self.book_rows]

        def subscribe(self, _spec: object) -> EmptyStream:
            return EmptyStream()

    def public_books(confirmed_at: datetime) -> dict[str, dict[str, object]]:
        return {
            token: {
                "token_id": token,
                "timestamp": confirmed_at,
                "asks": [{"price": "0.40", "size": "20"}],
                "bids": [{"price": "0.39", "size": "20"}],
                "minimum_order_size": "1",
                "tick_size": "0.01",
            }
            for token in (
                "yes-1",
                "no-1",
                "yes-e",
                "no-e",
                "paper-token-a",
                "paper-token-b",
                "paper-token-c",
            )
        }

    class PublicTrading:
        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "wallet": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "checked_at": NOW,
            }

    first_source = PublicStatusClient(
        {
            "condition-1": False,
            "condition-b-a": False,
            "condition-b-b": False,
            "condition-b-c": False,
        },
        public_books(NOW),
    )
    first_public_monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "public-source-first"),
        trading=PublicTrading(),
        public_client_factory=lambda: first_source,
        clock=lambda: NOW,
        relation_discovery=None,
    )
    first_public_monitor.set_observation_tokens(
        ("yes-1", "no-1", "paper-token-a", "paper-token-b", "paper-token-c")
    )
    first_public_monitor.set_observation_conditions(
        ("condition-1", "condition-b-a", "condition-b-b", "condition-b-c")
    )
    first_public_monitor.refresh_once()
    assert [call["closed"] for call in first_source.list_markets_calls] == [False, True]

    restart_source = PublicStatusClient(
        {
            "condition-1": True,
            "condition-b-a": False,
            "condition-b-b": False,
            "condition-b-c": False,
            "condition-e": False,
        },
        public_books(NOW - timedelta(seconds=11)),
    )
    restarted_public_monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "public-source-restart"),
        trading=PublicTrading(),
        public_client_factory=lambda: restart_source,
        clock=lambda: NOW - timedelta(seconds=11),
        relation_discovery=None,
    )
    restarted_public_monitor.set_observation_tokens(
        (
            "yes-1",
            "no-1",
            "yes-e",
            "no-e",
            "paper-token-a",
            "paper-token-b",
            "paper-token-c",
        )
    )
    restarted_public_monitor.set_observation_conditions(
        ("condition-1", "condition-b-a", "condition-b-b", "condition-b-c", "condition-e")
    )
    restarted_public_monitor.refresh_once()
    assert [call["closed"] for call in restart_source.list_markets_calls] == [False, True]
    assert restarted_public_monitor.observation_status(condition_id="condition-1")["status"] == "CLOSED"
    assert restarted_public_monitor.observation_status(condition_id="condition-b-a")["status"] == "OPEN"
    restart = PredictionObservationMonitor(
        catalog={},
        candidate_source=lambda: {
            "generation": 2,
            "generation_fingerprint": "restart-2",
            "rows": restart_rows,
        },
        store=PredictionArbitrageStore(store_path),
        monitor=restarted_public_monitor,
        book_source=restarted_public_monitor.observation_books,
        clock=lambda: NOW,
        pool_limit=2,
    )
    restored = restart.refresh_once()
    assert [member["identity"] for member in restored["members"]] == ["B", "E"]
    restored_reasons = [(member["identity"], member["result"].get("reason")) for member in restored["members"]]
    assert restored_reasons == [("B", "STALE_BOOK"), ("E", "STALE_BOOK")], restored_reasons

    # A missing catalog row does not prove resolution or erase its durable slot.
    missing_store_path = tmp_path / "missing-store"
    seeded = PredictionObservationMonitor(
        catalog=first_rows,
        store=PredictionArbitrageStore(missing_store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    seeded.refresh_once()
    missing = PredictionObservationMonitor(
        catalog={},
        candidate_source=lambda: {"rows": {"B": first_rows["B"]}},
        store=PredictionArbitrageStore(missing_store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    missing_snapshot = missing.refresh_once()
    assert {member["identity"] for member in missing_snapshot["members"]} == {"A", "B"}
    missing_member = next(
        member for member in missing_snapshot["members"] if member["identity"] == "A"
    )
    assert missing_member["result"]["reason"] == "CATALOG_ROW_MISSING"
    assert missing_snapshot["pool_count"] == 2
    assert missing_snapshot["coverage"]["pool_count"] == 2
    assert missing_snapshot["coverage"]["capacity"] == "2/2"
    assert missing_snapshot["coverage"]["latest_count"] == 1
    assert missing_snapshot["coverage"]["fresh"] == 1
    assert missing_snapshot["coverage"]["computed_count"] == 1
    assert missing_snapshot["coverage"]["blocked_count"] == 1
    assert missing_snapshot["coverage"]["native_count"] == 1
    assert missing_snapshot["coverage"]["three_way_count"] == 1
    assert [item["identity"] for item in missing_snapshot["latest"]] == ["B"]
    assert missing_member["stage"] == "OBSERVING"
    assert missing_member["result"]["current"] is False
    assert all(item["identity"] != "E" for item in missing_snapshot["members"])

    restored_missing = PredictionObservationMonitor(
        catalog=first_rows,
        store=PredictionArbitrageStore(missing_store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    restored_missing_snapshot = restored_missing.refresh_once()
    assert {member["identity"] for member in restored_missing_snapshot["members"]} == {
        "A",
        "B",
    }
    assert all(
        member["result"]["status"] == "PASS"
        for member in restored_missing_snapshot["members"]
    )
    assert restored_missing_snapshot["coverage"]["pool_count"] == 2
    assert restored_missing_snapshot["coverage"]["latest_count"] == 2

    both_missing = PredictionObservationMonitor(
        catalog={},
        candidate_source=lambda: {"rows": {}},
        store=PredictionArbitrageStore(missing_store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    both_missing_snapshot = both_missing.refresh_once()
    assert {member["identity"] for member in both_missing_snapshot["members"]} == {
        "A",
        "B",
    }
    assert both_missing_snapshot["pool_count"] == 2
    assert both_missing_snapshot["coverage"]["capacity"] == "2/2"
    assert both_missing_snapshot["coverage"]["latest_count"] == 0
    assert both_missing_snapshot["coverage"]["blocked_count"] == 2
    assert both_missing_snapshot["coverage"]["fresh"] == 0
    assert both_missing_snapshot["coverage"]["computed_count"] == 0
    assert both_missing_snapshot["coverage"]["positive"] == 0
    assert both_missing_snapshot["coverage"]["non_positive"] == 0
    assert both_missing_snapshot["coverage"]["native_count"] == 1
    assert both_missing_snapshot["coverage"]["three_way_count"] == 1
    assert both_missing_snapshot["latest"] == []
    assert all(
        member["result"]["reason"] == "CATALOG_ROW_MISSING"
        and member["result"]["current"] is False
        for member in both_missing_snapshot["members"]
    )

    recovered_both = PredictionObservationMonitor(
        catalog=first_rows,
        store=PredictionArbitrageStore(missing_store_path),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    recovered_both_snapshot = recovered_both.refresh_once()
    assert all(
        member["result"]["status"] == "PASS"
        for member in recovered_both_snapshot["members"]
    )

    unknown_store_path = tmp_path / "unknown-store"
    unknown_source = ExternalSource({})
    unknown_source_monitor = PredictionObservationMonitor(
        catalog=first_rows,
        store=PredictionArbitrageStore(unknown_store_path),
        monitor=unknown_source,
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    unknown_source_monitor.refresh_once()
    unknown_source.fail = True
    unknown_snapshot = unknown_source_monitor.refresh_once()
    assert {member["identity"] for member in unknown_snapshot["members"]} == {"A", "B"}
    assert all(
        member["result"]["reason"] == "SOURCE_UNKNOWN"
        for member in unknown_snapshot["members"]
    )
    # A later bounded source pass that proves every member OPEN restores the
    # same retained structures and permits a current paper result.
    unknown_source.statuses = {
        condition: {"status": "OPEN"}
        for condition in ("condition-1", "condition-b-a", "condition-b-b", "condition-b-c")
    }
    unknown_source.fail = False
    recovered_snapshot = unknown_source_monitor.refresh_once()
    assert {member["identity"] for member in recovered_snapshot["members"]} == {"A", "B"}
    assert all(
        member["result"]["status"] == "PASS"
        for member in recovered_snapshot["members"]
    )

    # A transient catalog read failure preserves the already published pool.
    read_failure = [False]

    def read_catalog() -> dict[str, object]:
        if read_failure[0]:
            raise OSError("catalog unavailable")
        return {"rows": first_rows}

    read_failure_monitor = PredictionObservationMonitor(
        catalog={},
        candidate_source=read_catalog,
        store=PredictionArbitrageStore(tmp_path / "read-failure"),
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    read_failure_monitor.refresh_once()
    read_failure[0] = True
    preserved = read_failure_monitor.refresh_once()
    assert {member["identity"] for member in preserved["members"]} == {"A", "B"}
    assert preserved["status"] == "ERROR"
    assert preserved["coverage"]["catalog_error"] == "catalog unavailable"

    # A failed membership write leaves the durable in-memory reservation visible.
    failing_store = PredictionArbitrageStore(tmp_path / "failing-store")
    seeded_failure = PredictionObservationMonitor(
        catalog=first_rows,
        store=failing_store,
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    seeded_failure.refresh_once()

    def fail_save(_members: object) -> None:
        raise OSError("disk full")

    failing_store.save_observation_pool_members = fail_save  # type: ignore[method-assign]
    failed = PredictionObservationMonitor(
        catalog={},
        candidate_source=lambda: {
            "rows": {
                "A": {**first_rows["A"], "source_status": "RESOLVED"},
                "B": first_rows["B"],
                "E": replacement,
            }
        },
        store=failing_store,
        book_source=lambda token_ids: books(NOW),
        clock=lambda: NOW,
        pool_limit=2,
    )
    failed_snapshot = failed.refresh_once()
    assert [member["identity"] for member in failed_snapshot["members"]] == ["A", "B"]
    assert "save observation membership failed" in str(
        failed_snapshot["coverage"]["persistence_error"]
    )

    restart.start()
    restart.start()
    assert restart.thread_alive
    restart.stop()


def test_membership_load_failure_preserves_durable_pool(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A failed SQLite membership read cannot make a nearer candidate take slots."""

    first_rows = {
        "A": native_row(tmp_path, "load-a", "2026-08-18T00:00:00Z"),
        "B": dated_rows()["paper-three"],
    }
    first_rows["A"]["version_id"] = "A"
    first_rows["B"]["version_id"] = "B"
    first_rows["B"]["endpoints"] = [
        {**endpoint, "condition_id": f"condition-{contract}"}
        for endpoint, contract in zip(
            first_rows["B"]["endpoints"], ("b-a", "b-b", "b-c"), strict=True
        )
    ]
    candidate_rows = deepcopy(first_rows)
    candidate_rows["C"] = deepcopy(first_rows["A"])
    candidate_rows["C"]["version_id"] = "C"
    candidate_rows["C"]["identity"] = "C"
    candidate_rows["C"]["endpoints"] = [
        {**endpoint, "end_date": "2026-08-16T03:00:00Z"}
        for endpoint in candidate_rows["C"]["endpoints"]
    ]
    store = PredictionArbitrageStore(tmp_path / "durable-membership")

    def books(_token_ids: tuple[str, ...]) -> dict[str, object]:
        return {}

    seeded = PredictionObservationMonitor(
        catalog=first_rows,
        store=store,
        book_source=books,
        clock=lambda: NOW,
        pool_limit=2,
    )
    seeded_snapshot = seeded.refresh_once()
    assert [member["identity"] for member in seeded_snapshot["members"]] == ["A", "B"]

    original_connect = sqlite3.connect

    def durable_identities() -> list[str]:
        connection = original_connect(store.path)
        try:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT identity FROM observation_pool_members ORDER BY identity"
                ).fetchall()
            ]
        finally:
            connection.close()

    before = durable_identities()
    assert before == ["A", "B"]

    failed = [True]

    def faulting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        if failed[0] and args:
            path = args[0]
            if isinstance(path, (str, Path)) and str(path).endswith("prediction_arbitrage.sqlite3"):
                raise OSError("durable membership read unavailable")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", faulting_connect)
    restarted = PredictionObservationMonitor(
        catalog={},
        candidate_source=lambda: {"rows": candidate_rows},
        store=store,
        book_source=books,
        clock=lambda: NOW,
        pool_limit=2,
    )
    failed_snapshot = restarted.refresh_once()
    assert failed_snapshot["status"] == "ERROR"
    assert failed_snapshot["coverage"]["persistence_error"] == (
        "load observation membership failed: durable membership read unavailable"
    )
    assert failed_snapshot["members"] == []
    assert durable_identities() == before

    failed[0] = False
    restored = restarted.refresh_once()
    assert [member["identity"] for member in restored["members"]] == ["A", "B"]
    assert durable_identities() == before
