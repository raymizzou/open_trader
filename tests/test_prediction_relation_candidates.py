"""Issue #90: connected threshold IMPLIES components become PENDING candidates."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.polymarket_monitor import PolymarketMonitor
from open_trader.polymarket_relation_discovery import (
    ThresholdBuyLeg,
    ThresholdMarket,
    ThresholdRelation,
    discover_threshold_relations,
    threshold_relation_payload,
)
from open_trader.prediction_arbitrage_store import (
    PredictionArbitrageStore,
    load_relation_state_readonly,
)
from open_trader.prediction_relation_candidates import (
    group_relation_candidates,
    prepare_relation_candidates,
)
from open_trader.relation_catalog import RelationCatalog

import open_trader.cli as cli


RULES = (
    'This market resolves "Yes" if the Binance BTC/USDT close at 12:00 ET '
    "is higher than the price specified in the title. Otherwise it resolves "
    '"No". The resolution source is Binance.'
)


def market(event_id: str, condition_id: str) -> ThresholdMarket:
    return ThresholdMarket(
        event_id=event_id,
        market_id=f"market-{condition_id}",
        condition_id=condition_id,
        question="Will Bitcoin be above $100,000 on December 31?",
        rules=RULES,
        resolution_source="Binance",
        end_date="2027-01-01T00:00:00Z",
        operator=">",
        threshold=Decimal("100000"),
        yes_token_id=f"yes-{condition_id}",
        no_token_id=f"no-{condition_id}",
        group_item_threshold="0",
        fees_enabled=False,
        fee_rate=Decimal("0"),
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
    )


def relation(event_id: str, lower: str, higher: str) -> ThresholdRelation:
    a = market(event_id, lower)
    b = market(event_id, higher)
    return ThresholdRelation(
        relation_id=f"threshold:{lower}:{higher}",
        event_id=event_id,
        market_a=a,
        market_b=b,
        relation="B_IMPLIES_A",
        buy_leg_a=ThresholdBuyLeg("A", a.market_id, a.condition_id, "YES", a.yes_token_id),
        buy_leg_b=ThresholdBuyLeg("B", b.market_id, b.condition_id, "NO", b.no_token_id),
        rules_hash_a="rules-a",
        rules_hash_b="rules-b",
    )


def incomplete_relation(event_id: str, lower: str, higher: str) -> ThresholdRelation:
    return replace(relation(event_id, lower, higher), rules_hash_a="")


def test_two_contract_component_is_filtered() -> None:
    assert group_relation_candidates([relation("e", "a", "b")]) == []


def test_three_contract_component_is_selected_deterministically() -> None:
    relations = [
        relation("e", "b", "c"),
        relation("e", "a", "b"),
    ]
    components = group_relation_candidates(relations)
    assert len(components) == 1
    assert components[0].event_id == "e"
    assert components[0].contract_ids == ("a", "b", "c")
    assert [item.relation_id for item in components[0].relations] == [
        "threshold:a:b",
        "threshold:b:c",
    ]
    assert components == group_relation_candidates(list(reversed(relations)))


def test_component_with_one_incomplete_member_is_skipped(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    relations = [
        relation("e", "a", "b"),
        incomplete_relation("e", "b", "c"),
    ]
    report = prepare_relation_candidates(catalog, relations)
    assert report["status"] == "INCOMPLETE_COMPONENT"
    assert report["prepared"] == 0
    assert report["incomplete"] == 1
    assert catalog.pending_count() == 0


def test_prepare_ingests_only_complete_selected_relations(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    relations = [
        relation("e", "a", "b"),
        relation("e", "b", "c"),
        relation("e", "d", "x"),
    ]
    report = prepare_relation_candidates(catalog, relations)
    assert report["status"] == "PREPARED"
    assert report["prepared"] == 1
    assert len(report["version_ids"]) == 2
    assert catalog.pending_count() == 2


def test_unchanged_fingerprint_does_not_reingest(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    relations = [relation("e", "a", "b"), relation("e", "b", "c")]
    first = prepare_relation_candidates(catalog, relations)
    assert first["status"] == "PREPARED"
    assert catalog.pending_count() == 2

    fingerprint = first["fingerprint"]
    assert isinstance(fingerprint, str)
    second = prepare_relation_candidates(
        catalog, relations, prepared_fingerprints={fingerprint}
    )
    assert second["status"] == "SKIPPED"
    assert second["prepared"] == 0
    assert catalog.pending_count() == 2


def test_two_complete_components_prepare_each_once_and_do_not_cycle(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    relations = [
        relation("e1", "a", "b"),
        relation("e1", "b", "c"),
        relation("e2", "d", "e"),
        relation("e2", "e", "f"),
    ]
    prepared_fingerprints: set[str] = set()

    first = prepare_relation_candidates(
        catalog, relations, prepared_fingerprints=prepared_fingerprints
    )
    assert first["status"] == "PREPARED"
    assert first["prepared"] == 1
    assert isinstance(first["fingerprint"], str)
    prepared_fingerprints.add(first["fingerprint"])
    assert catalog.pending_count() == 2

    second = prepare_relation_candidates(
        catalog, relations, prepared_fingerprints=prepared_fingerprints
    )
    assert second["status"] == "PREPARED"
    assert second["prepared"] == 1
    assert isinstance(second["fingerprint"], str)
    assert second["fingerprint"] != first["fingerprint"]
    prepared_fingerprints.add(second["fingerprint"])
    assert catalog.pending_count() == 4

    third = prepare_relation_candidates(
        catalog, relations, prepared_fingerprints=prepared_fingerprints
    )
    assert third["status"] == "SKIPPED"
    assert third["prepared"] == 0
    assert catalog.pending_count() == 4


def threshold_market(
    market_id: str,
    *,
    threshold: str,
    yes: str,
    no: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=market_id,
        slug=f"slug-{market_id}",
        question=f"Will Bitcoin be above ${threshold} on December 31?",
        condition_id=f"condition-{market_id}",
        description=RULES,
        resolution_source="Binance",
        end_date="2026-12-31T17:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
        group_item_threshold="display-only",
        state=SimpleNamespace(
            active=True,
            closed=False,
            ended=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=False,
        ),
        outcomes=[
            SimpleNamespace(label="YES", token_id=yes),
            SimpleNamespace(label="NO", token_id=no),
        ],
        trading=SimpleNamespace(
            minimum_order_size=Decimal("1"),
            minimum_tick_size=Decimal("0.01"),
            fees_enabled=False,
            neg_risk=False,
        ),
        metrics=SimpleNamespace(volume_24hr=Decimal("250")),
    )


def threshold_event() -> SimpleNamespace:
    return SimpleNamespace(
        id="threshold-event",
        title="Bitcoin",
        slug="bitcoin",
        state=SimpleNamespace(active=True, closed=False, ended=False),
        metrics=SimpleNamespace(volume_24hr=Decimal("250")),
        markets=[
            threshold_market("low", threshold="80,000", yes="yes-low", no="no-low"),
            threshold_market("mid", threshold="90,000", yes="yes-mid", no="no-mid"),
            threshold_market("high", threshold="100,000", yes="yes-high", no="no-high"),
        ],
    )


class FakeClient:
    def __init__(self, events: list[object]) -> None:
        self._events = list(events)

    async def list_events(self, **kwargs: object) -> list[object]:
        return list(self._events)


def test_monitor_full_scan_auto_prepares_at_most_one_candidate(tmp_path: Path) -> None:
    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path / "data"),
        trading=SimpleNamespace(),
        public_client_factory=FakeClient,
        relation_discovery=discover_threshold_relations,
        relation_catalog=RelationCatalog(tmp_path / "data"),
    )
    client = FakeClient([threshold_event()])

    asyncio.run(monitor._run_full_relation_scan(client))
    assert monitor._prepared_candidate_fps
    assert monitor._relation_catalog.pending_count() == 3

    asyncio.run(monitor._run_full_relation_scan(FakeClient([threshold_event()])))
    assert monitor._relation_catalog.pending_count() == 3


def test_cli_relation_candidates_dry_run_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    store = PredictionArbitrageStore(data_dir)
    store.save_relation_state(
        {
            "relations": [
                threshold_relation_payload(relation("e", "a", "b")),
                threshold_relation_payload(relation("e", "b", "c")),
            ]
        },
        full_scanned_at="2026-08-17T00:00:00Z",
    )

    assert (
        cli.main(
            [
                "prediction-arb",
                "relation-candidates",
                "--data-dir",
                str(data_dir),
                "--dry-run",
            ]
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["status"] == "PREPARED"
    assert dry_run["prepared"] == 1
    assert dry_run["version_ids"] == []
    assert RelationCatalog(data_dir).pending_count() == 0

    assert (
        cli.main(
            [
                "prediction-arb",
                "relation-candidates",
                "--data-dir",
                str(data_dir),
                "--apply",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "PREPARED"
    assert len(applied["version_ids"]) == 2
    assert RelationCatalog(data_dir).pending_count() == 2


def test_cli_relation_candidates_dry_run_readonly_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    store = PredictionArbitrageStore(data_dir)
    store.save_relation_state(
        {
            "relations": [
                threshold_relation_payload(relation("e", "a", "b")),
                threshold_relation_payload(relation("e", "b", "c")),
            ]
        },
        full_scanned_at="2026-08-17T00:00:00Z",
    )

    db = data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    db.chmod(0o444)
    try:
        assert load_relation_state_readonly(data_dir) is not None
        assert (
            cli.main(
                [
                    "prediction-arb",
                    "relation-candidates",
                    "--data-dir",
                    str(data_dir),
                    "--dry-run",
                ]
            )
            == 0
        )
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "PREPARED"
        assert report["prepared"] == 1
        assert report["version_ids"] == []
    finally:
        db.chmod(0o644)
