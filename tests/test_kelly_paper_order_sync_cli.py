from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.kelly_paper_order_sync import (
    FakeFutuPaperOrderClient,
    FutuSimulatePaperOrderClient,
)


def test_kelly_sync_paper_orders_parser_accepts_fake_mode() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(
        [
            "kelly",
            "sync-paper-orders",
            "--fake",
            "--data-dir",
            "data",
            "--synced-at",
            "2026-07-09 11:00",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "sync-paper-orders"
    assert args.fake is True
    assert args.data_dir == Path("data")
    assert args.synced_at == "2026-07-09 11:00"


def test_kelly_sync_paper_orders_parser_accepts_futu_simulate_mode() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(
        [
            "kelly",
            "sync-paper-orders",
            "--futu-simulate",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "sync-paper-orders"
    assert args.futu_simulate is True
    assert args.host == "127.0.0.1"
    assert args.port == 11111


def test_kelly_sync_paper_orders_fake_wires_sync_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_default_orders() -> tuple[dict[str, object], ...]:
        return (
            {
                "experiment_id": "trend_pullback_20d_exp_20260707",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "order_id": "SIM-10001",
            },
        )

    def fake_sync_kelly_paper_orders(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        client = kwargs["client"]
        assert isinstance(client, FakeFutuPaperOrderClient)
        return {
            "environment": "SIMULATE",
            "orders": client.list_orders(),
            "synced_at": kwargs["synced_at"],
        }

    monkeypatch.setattr(cli, "default_fake_kelly_paper_orders", fake_default_orders)
    monkeypatch.setattr(cli, "sync_kelly_paper_orders", fake_sync_kelly_paper_orders)

    result = cli.main(
        [
            "kelly",
            "sync-paper-orders",
            "--fake",
            "--data-dir",
            str(tmp_path / "data"),
            "--synced-at",
            "2026-07-09 11:00",
        ]
    )

    assert result == 0
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["synced_at"] == "2026-07-09 11:00"
    output = capsys.readouterr().out
    assert "environment: SIMULATE" in output
    assert "orders: 1" in output
    assert f"latest: {tmp_path / 'data/latest/kelly_paper_orders.json'}" in output


def test_kelly_sync_paper_orders_futu_simulate_wires_sync_and_closes_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    closed: list[bool] = []

    class FakeFutuSimulateClient:
        environment = "SIMULATE"
        source = "futu_simulate_paper_order_client"

        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        def list_orders(self) -> list[dict[str, object]]:
            return [
                {
                    "experiment_id": "trend_exp",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "order_id": "SIM-10001",
                }
            ]

        def close(self) -> None:
            closed.append(True)

    def fake_load_index(data_dir: Path) -> dict[tuple[str, str], str]:
        captured["index_data_dir"] = data_dir
        return {("US", "RAM"): "trend_exp"}

    def fake_sync_kelly_paper_orders(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        client = kwargs["client"]
        assert isinstance(client, FakeFutuSimulateClient)
        return {
            "environment": "SIMULATE",
            "orders": client.list_orders(),
            "synced_at": kwargs["synced_at"],
        }

    monkeypatch.setattr(cli, "FutuSimulatePaperOrderClient", FakeFutuSimulateClient)
    monkeypatch.setattr(cli, "load_kelly_experiment_symbol_index", fake_load_index)
    monkeypatch.setattr(cli, "sync_kelly_paper_orders", fake_sync_kelly_paper_orders)

    result = cli.main(
        [
            "kelly",
            "sync-paper-orders",
            "--futu-simulate",
            "--data-dir",
            str(tmp_path / "data"),
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--synced-at",
            "2026-07-09 11:30",
        ]
    )

    assert result == 0
    assert captured["index_data_dir"] == tmp_path / "data"
    assert captured["client_kwargs"] == {
        "host": "127.0.0.1",
        "port": 11111,
        "experiment_symbol_index": {("US", "RAM"): "trend_exp"},
    }
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["synced_at"] == "2026-07-09 11:30"
    assert closed == [True]
    output = capsys.readouterr().out
    assert "environment: SIMULATE" in output
    assert "orders: 1" in output
    assert f"latest: {tmp_path / 'data/latest/kelly_paper_orders.json'}" in output


def test_kelly_sync_paper_orders_requires_fake_mode() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["kelly", "sync-paper-orders"])

    assert exc_info.value.code == 2


def test_kelly_sync_paper_orders_rejects_multiple_sources() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["kelly", "sync-paper-orders", "--fake", "--futu-simulate"])

    assert exc_info.value.code == 2
