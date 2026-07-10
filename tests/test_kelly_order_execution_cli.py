from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli


def test_kelly_execute_orders_parser_accepts_dry_run_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "execute-orders",
            "--data-dir",
            "data",
            "--dry-run",
            "--executed-at",
            "2026-07-10 13:32",
            "--limit-price",
            "US.RAM=12.50",
            "--order-qty",
            "HK.02840=1",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "execute-orders"
    assert args.data_dir == Path("data")
    assert args.dry_run is True
    assert args.futu_simulate is False
    assert args.executed_at == "2026-07-10 13:32"
    assert args.limit_price == ["US.RAM=12.50"]
    assert args.order_qty == ["HK.02840=1"]


def test_kelly_execute_orders_main_writes_payload_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    latest_path = tmp_path / "data/latest/kelly_order_executions.json"

    def fake_execute(**kwargs: object) -> dict[str, object]:
        captured["execute_kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_executions.v1",
            "environment": "DRY_RUN",
            "source": "dry_run",
            "executed_at": kwargs["executed_at"],
            "execution_count": 3,
            "submitted_count": 0,
            "dry_run_count": 1,
            "skipped_count": 2,
            "failed_count": 0,
            "executions": [],
        }

    def fake_write(data_dir: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "execute_kelly_orders", fake_execute)
    monkeypatch.setattr(cli, "write_kelly_order_executions", fake_write)

    result = cli.main(
        [
            "kelly",
            "execute-orders",
            "--data-dir",
            str(tmp_path / "data"),
            "--dry-run",
            "--executed-at",
            "2026-07-10 13:32",
            "--limit-price",
            "US.RAM=12.50",
        ]
    )

    assert result == 0
    assert captured["execute_kwargs"] == {
        "data_dir": tmp_path / "data",
        "dry_run": True,
        "executed_at": "2026-07-10 13:32",
        "limit_prices": {"US.RAM": "12.50"},
        "order_quantities": {},
        "client": None,
    }
    assert captured["write_data_dir"] == tmp_path / "data"
    assert captured["payload"]["execution_count"] == 3
    output = capsys.readouterr().out
    assert "environment: DRY_RUN" in output
    assert "executions: 3" in output
    assert "dry_run: 1" in output
    assert "submitted: 0" in output
    assert "skipped: 2" in output
    assert "failed: 0" in output
    assert f"latest: {latest_path}" in output


def test_kelly_execute_orders_main_writes_links_for_futu_simulate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        def close(self) -> None:
            captured["client_closed"] = True

    def fake_execute(**kwargs: object) -> dict[str, object]:
        captured["execute_kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_executions.v1",
            "environment": "SIMULATE",
            "source": "fake",
            "executed_at": kwargs["executed_at"],
            "execution_count": 1,
            "submitted_count": 1,
            "dry_run_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "executions": [
                {
                    "submitted": True,
                    "futu_order_id": "SIM-1",
                    "experiment_id": "trend",
                }
            ],
        }

    def fake_write(data_dir: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir
        captured["payload"] = payload
        return tmp_path / "data/latest/kelly_order_executions.json"

    def fake_write_links(data_dir: Path, payload: dict[str, object]) -> Path:
        captured["links_data_dir"] = data_dir
        captured["links_payload"] = payload
        return tmp_path / "data/latest/kelly_order_links.json"

    monkeypatch.setattr(cli, "FutuSimulateOrderExecutionClient", FakeClient)
    monkeypatch.setattr(cli, "execute_kelly_orders", fake_execute)
    monkeypatch.setattr(cli, "write_kelly_order_executions", fake_write)
    monkeypatch.setattr(cli, "write_kelly_order_links_from_executions", fake_write_links)

    result = cli.main(
        [
            "kelly",
            "execute-orders",
            "--data-dir",
            str(tmp_path / "data"),
            "--futu-simulate",
            "--simulate-acc-id",
            "12958917",
            "--executed-at",
            "2026-07-10 13:32",
            "--limit-price",
            "HK.02840=2950",
            "--order-qty",
            "HK.02840=1",
        ]
    )

    assert result == 0
    assert captured["client_kwargs"] == {
        "host": "127.0.0.1",
        "port": 11111,
        "simulate_acc_id": 12958917,
    }
    assert captured["execute_kwargs"]["dry_run"] is False
    assert captured["execute_kwargs"]["client"].__class__ is FakeClient
    assert captured["execute_kwargs"]["limit_prices"] == {"HK.02840": "2950"}
    assert captured["execute_kwargs"]["order_quantities"] == {"HK.02840": "1"}
    assert captured["links_data_dir"] == tmp_path / "data"
    assert captured["links_payload"]["submitted_count"] == 1
    assert captured["client_closed"] is True
