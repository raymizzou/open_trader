from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.kelly_paper_order_sync import (
    FakeFutuPaperOrderClient,
    FutuSimulatePaperOrderClient,
    build_kelly_paper_order_sync_report,
    load_kelly_order_links,
    load_kelly_experiment_symbol_index_details,
    load_kelly_experiment_symbol_index,
    sync_kelly_paper_orders,
    write_kelly_paper_order_sync_report,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeFutuOrderContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.order_calls: list[dict[str, object]] = []

    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 111,
                        "acc_index": 0,
                        "trd_env": "REAL",
                        "acc_type": "CASH",
                        "acc_status": "ACTIVE",
                    },
                    {
                        "acc_id": 222,
                        "acc_index": 1,
                        "trd_env": "SIMULATE",
                        "acc_type": "SECURITY",
                        "acc_status": "ACTIVE",
                    },
                ]
            ),
        )

    def order_list_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        order_market: str,
    ) -> tuple[int, FakeDataFrame]:
        self.order_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "order_market": order_market,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "code": "US.RAM",
                        "trd_side": "BUY",
                        "create_time": "2026-07-09 10:01:00",
                        "price": "12.34",
                        "qty": "800",
                        "dealt_qty": "800",
                        "dealt_avg_price": "12.35",
                        "order_status": "FILLED_ALL",
                        "order_id": "SIM-10001",
                    },
                    {
                        "code": "US.SOXX",
                        "trd_side": "SELL",
                        "create_time": "2026-07-09 10:02:00",
                        "price": "246.80",
                        "qty": "20",
                        "dealt_qty": "0",
                        "dealt_avg_price": "",
                        "order_status": "SUBMITTED",
                        "order_id": "SIM-10002",
                    },
                ]
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeDiagnosticFutuOrderContext(FakeFutuOrderContext):
    def order_list_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        order_market: str,
    ) -> tuple[int, FakeDataFrame]:
        self.order_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "order_market": order_market,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "code": "US.RAM",
                        "trd_side": "BUY",
                        "create_time": "2026-07-09 10:01:00",
                        "price": "12.34",
                        "qty": "800",
                        "dealt_qty": "800",
                        "dealt_avg_price": "12.35",
                        "order_status": "FILLED_ALL",
                        "order_id": "SIM-10001",
                    },
                    {
                        "code": "US.SOXX",
                        "trd_side": "SELL",
                        "create_time": "2026-07-09 10:02:00",
                        "price": "246.80",
                        "qty": "20",
                        "dealt_qty": "0",
                        "dealt_avg_price": "",
                        "order_status": "SUBMITTED",
                        "order_id": "SIM-10002",
                    },
                    {
                        "code": "US.XYZ",
                        "trd_side": "BUY",
                        "order_status": "SUBMITTED",
                        "order_id": "SIM-10003",
                    },
                    {
                        "code": "RAM",
                        "trd_side": "BUY",
                        "order_status": "SUBMITTED",
                        "order_id": "SIM-10004",
                    },
                ]
            ),
        )


def test_load_kelly_experiment_symbol_index_maps_unique_participants(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_exp",
                    "participants": [
                        {"market": "US", "symbol": "RAM"},
                        {"market": "US", "symbol": "SOXX"},
                    ],
                },
                {
                    "experiment_id": "breakout_exp",
                    "participants": [
                        {"market": "US", "symbol": "MSFT"},
                        {"market": "US", "symbol": "SOXX"},
                    ],
                },
            ],
        },
    )

    index = load_kelly_experiment_symbol_index(data_dir)

    assert index[("US", "RAM")] == "trend_exp"
    assert index[("US", "MSFT")] == "breakout_exp"
    assert ("US", "SOXX") not in index


def test_load_kelly_experiment_symbol_index_details_preserves_ambiguous_symbols(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_exp",
                    "participants": [
                        {"market": "US", "symbol": "RAM"},
                        {"market": "US", "symbol": "SOXX"},
                    ],
                },
                {
                    "experiment_id": "breakout_exp",
                    "participants": [
                        {"market": "US", "symbol": "SOXX"},
                    ],
                },
            ],
        },
    )

    details = load_kelly_experiment_symbol_index_details(data_dir)

    assert details.unique == {("US", "RAM"): "trend_exp"}
    assert details.ambiguous == {("US", "SOXX"): ["breakout_exp", "trend_exp"]}


def test_load_kelly_order_links_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_kelly_order_links(tmp_path / "data") == {}


def test_load_kelly_order_links_indexes_by_futu_order_id(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_order_links.json",
        {
            "schema_version": "open_trader.kelly_order_links.v1",
            "links": [
                {
                    "futu_order_id": "SIM-10002",
                    "experiment_id": "breakout_exp",
                    "strategy_id": "breakout_10d",
                    "market": "US",
                    "symbol": "SOXX",
                    "side": "buy",
                    "created_at": "2026-07-10 12:30",
                    "source": "kelly_auto_order",
                }
            ],
        },
    )

    assert load_kelly_order_links(data_dir) == {
        "SIM-10002": {
            "futu_order_id": "SIM-10002",
            "experiment_id": "breakout_exp",
            "strategy_id": "breakout_10d",
            "market": "US",
            "symbol": "SOXX",
            "side": "buy",
            "created_at": "2026-07-10 12:30",
            "source": "kelly_auto_order",
        }
    }


def test_futu_simulate_paper_order_client_reads_simulate_orders() -> None:
    client = FutuSimulatePaperOrderClient(
        host="127.0.0.1",
        port=11111,
        experiment_symbol_index={("US", "RAM"): "trend_exp"},
        context_factory=FakeFutuOrderContext,
        connectivity_checker=lambda host, port: True,
    )

    orders = client.list_orders()

    assert orders == [
        {
            "experiment_id": "trend_exp",
            "market": "US",
            "symbol": "RAM",
            "side": "buy",
            "submitted_at": "2026-07-09 10:01:00",
            "order_price": "12.34",
            "order_qty": "800",
            "filled_qty": "800",
            "avg_fill_price": "12.35",
            "status": "filled",
            "order_id": "SIM-10001",
        }
    ]
    assert client.context.order_calls == [
        {
            "trd_env": "SIMULATE",
            "acc_id": 222,
            "acc_index": 1,
            "refresh_cache": True,
            "order_market": "N/A",
        }
    ]


def test_futu_simulate_paper_order_client_prefers_order_link_for_ambiguous_symbol(
    tmp_path: Path,
) -> None:
    client = FutuSimulatePaperOrderClient(
        host="127.0.0.1",
        port=11111,
        experiment_symbol_index={("US", "RAM"): "trend_exp"},
        ambiguous_symbol_index={("US", "SOXX"): ["breakout_exp", "trend_exp"]},
        order_link_index={
            "SIM-10002": {
                "futu_order_id": "SIM-10002",
                "experiment_id": "breakout_exp",
                "strategy_id": "breakout_10d",
                "market": "US",
                "symbol": "SOXX",
                "side": "sell",
                "created_at": "2026-07-10 12:30",
                "source": "kelly_auto_order",
            }
        },
        context_factory=FakeFutuOrderContext,
        connectivity_checker=lambda host, port: True,
    )

    payload = sync_kelly_paper_orders(
        tmp_path / "data",
        client,
        synced_at="2026-07-10 12:35",
    )
    report = build_kelly_paper_order_sync_report(payload, client)

    assert payload["orders"][1]["experiment_id"] == "breakout_exp"
    assert payload["orders"][1]["symbol"] == "SOXX"
    assert report["matched_orders"][1] == {
        "market": "US",
        "symbol": "SOXX",
        "order_id": "SIM-10002",
        "experiment_id": "breakout_exp",
        "reason": "matched_by_order_link",
    }
    assert report["skipped_orders"] == []


def test_futu_simulate_paper_order_client_records_diagnostics(
    tmp_path: Path,
) -> None:
    client = FutuSimulatePaperOrderClient(
        host="127.0.0.1",
        port=11111,
        experiment_symbol_index={("US", "RAM"): "trend_exp"},
        ambiguous_symbol_index={("US", "SOXX"): ["breakout_exp", "trend_exp"]},
        context_factory=FakeDiagnosticFutuOrderContext,
        connectivity_checker=lambda host, port: True,
    )

    payload = sync_kelly_paper_orders(
        tmp_path / "data",
        client,
        synced_at="2026-07-10 09:30",
    )
    report = build_kelly_paper_order_sync_report(payload, client)
    report_path = write_kelly_paper_order_sync_report(tmp_path / "data", report)
    stored_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["orders"] == [
        {
            "experiment_id": "trend_exp",
            "market": "US",
            "symbol": "RAM",
            "side": "buy",
            "submitted_at": "2026-07-09 10:01:00",
            "order_price": "12.34",
            "order_qty": "800",
            "filled_qty": "800",
            "avg_fill_price": "12.35",
            "status": "filled",
            "order_id": "SIM-10001",
        }
    ]
    assert stored_report == report
    assert report["schema_version"] == "open_trader.kelly_paper_order_sync_report.v1"
    assert report["counts"] == {
        "matched": 1,
        "skipped_untracked_symbol": 1,
        "skipped_ambiguous_symbol": 1,
        "skipped_invalid_code": 1,
        "orders_written": 1,
    }
    assert report["matched_orders"] == [
        {
            "market": "US",
            "symbol": "RAM",
            "order_id": "SIM-10001",
            "experiment_id": "trend_exp",
            "reason": "matched",
        }
    ]
    assert report["skipped_orders"] == [
        {
            "market": "US",
            "symbol": "SOXX",
            "order_id": "SIM-10002",
            "reason": "ambiguous_symbol",
            "experiment_ids": ["breakout_exp", "trend_exp"],
        },
        {
            "market": "US",
            "symbol": "XYZ",
            "order_id": "SIM-10003",
            "reason": "untracked_symbol",
        },
        {
            "code": "RAM",
            "order_id": "SIM-10004",
            "reason": "invalid_code",
        },
    ]


def test_futu_simulate_paper_order_client_closes_context() -> None:
    client = FutuSimulatePaperOrderClient(
        host="127.0.0.1",
        port=11111,
        experiment_symbol_index={},
        context_factory=FakeFutuOrderContext,
        connectivity_checker=lambda host, port: True,
    )

    client.close()

    assert client.context.closed is True
