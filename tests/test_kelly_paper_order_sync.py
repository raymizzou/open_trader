from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.kelly_paper_order_sync import (
    FakeFutuPaperOrderClient,
    sync_kelly_paper_orders,
)


def test_sync_kelly_paper_orders_writes_latest_artifact_from_fake_client(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    client = FakeFutuPaperOrderClient(
        orders=(
            {
                "experiment_id": "trend_pullback_20d_exp_20260707",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "submitted_at": "2026-07-08 10:01",
                "order_price": "12.34",
                "order_qty": "800",
                "filled_qty": "800",
                "avg_fill_price": "12.34",
                "status": "filled",
                "order_id": "SIM-10001",
            },
        ),
    )

    payload = sync_kelly_paper_orders(
        data_dir,
        client,
        synced_at="2026-07-09 10:30",
    )

    latest_path = data_dir / "latest" / "kelly_paper_orders.json"
    stored = json.loads(latest_path.read_text(encoding="utf-8"))
    assert stored == payload
    assert stored == {
        "schema_version": "open_trader.kelly_paper_orders.v1",
        "environment": "SIMULATE",
        "source": "fake_futu_paper_order_client",
        "synced_at": "2026-07-09 10:30",
        "orders": [
            {
                "experiment_id": "trend_pullback_20d_exp_20260707",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "submitted_at": "2026-07-08 10:01",
                "order_price": "12.34",
                "order_qty": "800",
                "filled_qty": "800",
                "avg_fill_price": "12.34",
                "status": "filled",
                "order_id": "SIM-10001",
            }
        ],
    }


def test_sync_kelly_paper_orders_rejects_non_simulate_environment(
    tmp_path: Path,
) -> None:
    client = FakeFutuPaperOrderClient(environment="REAL", orders=())

    with pytest.raises(ValueError, match="SIMULATE"):
        sync_kelly_paper_orders(tmp_path / "data", client)

    assert not (tmp_path / "data" / "latest" / "kelly_paper_orders.json").exists()


def test_sync_kelly_paper_orders_rejects_order_without_experiment_id(
    tmp_path: Path,
) -> None:
    client = FakeFutuPaperOrderClient(
        orders=(
            {
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "order_id": "SIM-10001",
            },
        ),
    )

    with pytest.raises(ValueError, match="experiment_id"):
        sync_kelly_paper_orders(tmp_path / "data", client)

    assert not (tmp_path / "data" / "latest" / "kelly_paper_orders.json").exists()
