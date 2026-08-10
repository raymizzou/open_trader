from __future__ import annotations

import sqlite3
import json
import os
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_shadow import main, seed_shadow_store


def _populated_store(data_dir: Path) -> PredictionArbitrageStore:
    store = PredictionArbitrageStore(data_dir)
    store.save_relation_state(
        {"relations": [{"relation_id": "relation-1"}]},
        full_scanned_at="2026-08-10T00:00:00Z",
    )
    store.save_llm_cache("cached-1", {"decision": "approve"})
    store.save_llm_cache("cached-2", {"decision": "reject"})
    with store._transaction() as connection:  # production-shaped forbidden row
        connection.execute(
            "INSERT INTO signals(signal_id, market_id, payload, started_at, updated_at) "
            "VALUES ('signal-1', 'market-1', '{}', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')"
        )
    return store


def test_seed_shadow_store_copies_only_relations_and_llm_cache(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")

    report = seed_shadow_store(
        source_data_dir=source.data_dir,
        shadow_data_dir=tmp_path / "shadow",
    )
    destination = PredictionArbitrageStore(tmp_path / "shadow")

    assert destination.load_relation_state() == source.load_relation_state()
    assert destination.load_llm_cache("cached-1") == {"decision": "approve"}
    assert report["relation_state_rows"] == 1
    assert report["llm_cache_rows"] == 2
    assert destination.histories("executions") == []
    assert destination.histories("incidents") == []
    assert destination.signal_history("all") == []
    with sqlite3.connect(f"file:{destination.path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM relation_scan_runs").fetchone() == (0,)


def test_seed_shadow_store_rejects_same_database(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")

    with pytest.raises(ValueError, match="different databases"):
        seed_shadow_store(
            source_data_dir=source.data_dir,
            shadow_data_dir=source.data_dir,
        )


def test_seed_shadow_store_rejects_hardlinked_database(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")
    hardlink_dir = tmp_path / "hardlink" / "prediction_arbitrage"
    hardlink_dir.mkdir(parents=True)
    os.link(source.path, hardlink_dir / source.path.name)

    with pytest.raises(ValueError, match="different databases"):
        seed_shadow_store(
            source_data_dir=source.data_dir,
            shadow_data_dir=hardlink_dir.parent,
        )


def test_shadow_seed_cli_writes_the_report(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")
    report_path = tmp_path / "reports" / "seed.json"

    assert main(
        [
            "--source-data-dir", str(source.data_dir),
            "--shadow-data-dir", str(tmp_path / "shadow"),
            "--report", str(report_path),
        ]
    ) == 0

    assert json.loads(report_path.read_text(encoding="utf-8"))["llm_cache_rows"] == 2
