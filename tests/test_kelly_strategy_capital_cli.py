from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader import cli


def test_kelly_build_strategy_capital_parser_accepts_timestamp() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "build-strategy-capital",
            "--calculated-at",
            "2026-07-10 21:20",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "build-strategy-capital"
    assert args.calculated_at == "2026-07-10 21:20"


def test_kelly_build_strategy_capital_main_loads_inputs_and_writes_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    paper_orders_payload = {"orders": [{"experiment_id": "trend"}]}
    order_executions_payload = {"executions": [{"experiment_id": "trend"}]}
    (latest_dir / "kelly_paper_orders.json").write_text(
        json.dumps(paper_orders_payload),
        encoding="utf-8",
    )
    (latest_dir / "kelly_order_executions.json").write_text(
        json.dumps(order_executions_payload),
        encoding="utf-8",
    )
    latest_path = latest_dir / "kelly_strategy_capital.json"

    class FakeKellyLabState:
        available = True
        experiments = [{"experiment_id": "trend"}]
        error = ""

    def fake_build(
        experiments: list[dict[str, object]],
        *,
        paper_orders_payload: dict[str, object] | None,
        order_executions_payload: dict[str, object] | None,
        calculated_at: str | None,
    ) -> dict[str, object]:
        captured["experiments"] = experiments
        captured["paper_orders_payload"] = paper_orders_payload
        captured["order_executions_payload"] = order_executions_payload
        captured["calculated_at"] = calculated_at
        return {
            "schema_version": "open_trader.kelly_strategy_capital.v1",
            "calculated_at": calculated_at,
            "strategy_count": 1,
            "strategies": [],
        }

    def fake_write(data_dir_arg: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir_arg
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "load_kelly_lab_state", lambda data_dir_arg: FakeKellyLabState())
    monkeypatch.setattr(cli, "build_kelly_strategy_capital_payload", fake_build)
    monkeypatch.setattr(cli, "write_kelly_strategy_capital", fake_write)

    result = cli.main(
        [
            "kelly",
            "build-strategy-capital",
            "--data-dir",
            str(data_dir),
            "--calculated-at",
            "2026-07-10 21:20",
        ]
    )

    assert result == 0
    assert captured["experiments"] == [{"experiment_id": "trend"}]
    assert captured["paper_orders_payload"] == paper_orders_payload
    assert captured["order_executions_payload"] == order_executions_payload
    assert captured["calculated_at"] == "2026-07-10 21:20"
    assert captured["write_data_dir"] == data_dir
    assert captured["payload"] == {
        "schema_version": "open_trader.kelly_strategy_capital.v1",
        "calculated_at": "2026-07-10 21:20",
        "strategy_count": 1,
        "strategies": [],
    }
    output = capsys.readouterr().out
    assert "strategies: 1" in output
    assert f"latest: {latest_path}" in output
