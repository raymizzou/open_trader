from __future__ import annotations

import csv
from pathlib import Path

import pytest

from open_trader.broker_details import load_broker_detail_snapshot


POSITION_FIELDS = [
    "statement_id",
    "broker",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
]
CASH_FIELDS = ["statement_id", "broker", "currency", "cash_balance"]


def write_detail_rows(
    data_dir: Path,
    run_name: str,
    *,
    positions: list[dict[str, str]],
    cash: list[dict[str, str]],
) -> None:
    run_dir = data_dir / "runs" / run_name
    run_dir.mkdir(parents=True)
    with (run_dir / "extracted_positions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=POSITION_FIELDS)
        writer.writeheader()
        writer.writerows(positions)
    with (run_dir / "extracted_cash.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CASH_FIELDS)
        writer.writeheader()
        writer.writerows(cash)


def position(statement_id: str, broker: str, symbol: str) -> dict[str, str]:
    return {
        "statement_id": statement_id,
        "broker": broker,
        "market": "HK",
        "asset_class": "stock",
        "symbol": symbol,
        "name": symbol,
        "currency": "HKD",
        "quantity": "100",
        "cost_price": "10",
    }


def cash(statement_id: str, broker: str) -> dict[str, str]:
    return {
        "statement_id": statement_id,
        "broker": broker,
        "currency": "HKD",
        "cash_balance": "1000",
    }


def test_statement_snapshot_uses_newest_statement_period_across_runs(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_detail_rows(
        data_dir,
        "2026-07-30",
        positions=[position("2026-07-10-phillips", "phillips", "0010")],
        cash=[cash("2026-07-10-phillips", "phillips")],
    )
    write_detail_rows(
        data_dir,
        "2026-07",
        positions=[
            position("2026-07-29-phillips", "phillips", "3690"),
            position("2026-07-29-phillips", "phillips", "9858"),
        ],
        cash=[cash("2026-07-29-phillips", "phillips")],
    )

    snapshot = load_broker_detail_snapshot(data_dir, "phillips")

    assert snapshot.available is True
    assert snapshot.snapshot_period == "2026-07-29"
    assert snapshot.source_kind == "statement"
    assert [row["symbol"] for row in snapshot.positions] == ["3690", "9858"]
    assert [row["statement_id"] for row in snapshot.cash] == ["2026-07-29-phillips"]


def test_tiger_live_snapshot_beats_newer_statement_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_detail_rows(
        data_dir,
        "2026-07-30",
        positions=[position("2026-07-29-tiger-live", "tiger", "LIVE")],
        cash=[cash("2026-07-29-tiger-live", "tiger")],
    )
    write_detail_rows(
        data_dir,
        "2026-07-31",
        positions=[position("2026-07-30-tiger", "tiger", "STALE")],
        cash=[cash("2026-07-30-tiger", "tiger")],
    )

    snapshot = load_broker_detail_snapshot(data_dir, "tiger")

    assert snapshot.source_kind == "live_account"
    assert snapshot.snapshot_period == "2026-07-29"
    assert [row["symbol"] for row in snapshot.positions] == ["LIVE"]


@pytest.mark.parametrize("broker", ["tiger", "phillips", "eastmoney"])
def test_missing_broker_details_are_unavailable(tmp_path: Path, broker: str) -> None:
    snapshot = load_broker_detail_snapshot(tmp_path / "data", broker)

    assert snapshot.available is False
    assert snapshot.positions == ()
    assert snapshot.cash == ()
    assert snapshot.snapshot_period == ""
    assert snapshot.reason
