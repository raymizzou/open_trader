from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader import cli


def test_kelly_build_trade_samples_parser_accepts_generated_at() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "build-trade-samples",
            "--generated-at",
            "2026-07-11 11:00",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "build-trade-samples"
    assert args.generated_at == "2026-07-11 11:00"


def test_kelly_build_trade_samples_main_loads_inputs_and_writes_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    paper_orders_payload = {
        "schema_version": "open_trader.kelly_paper_orders.v1",
        "orders": [],
    }
    (latest_dir / "kelly_paper_orders.json").write_text(
        json.dumps(paper_orders_payload),
        encoding="utf-8",
    )
    latest_path = latest_dir / "kelly_trade_samples.json"

    class FakeKellyLabState:
        available = True
        experiments = [{"experiment_id": "trend_us"}]
        error = ""

    def fake_load_kelly_lab_state(
        data_dir_arg: Path,
        *,
        include_strategy_capital: bool = True,
        include_trade_samples: bool = True,
    ) -> FakeKellyLabState:
        captured["load_data_dir"] = data_dir_arg
        captured["include_strategy_capital"] = include_strategy_capital
        captured["include_trade_samples"] = include_trade_samples
        return FakeKellyLabState()

    def fake_build(
        experiments: list[dict[str, object]],
        paper_orders_payload_arg: dict[str, object],
        *,
        generated_at: str | None,
    ) -> dict[str, object]:
        captured["experiments"] = experiments
        captured["paper_orders_payload"] = paper_orders_payload_arg
        captured["generated_at"] = generated_at
        return {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "sample_count": 2,
            "open_position_count": 1,
            "skipped_order_count": 3,
            "stats_by_experiment": {},
        }

    def fake_write(data_dir_arg: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir_arg
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "load_kelly_lab_state", fake_load_kelly_lab_state)
    monkeypatch.setattr(cli, "build_kelly_trade_samples_payload", fake_build)
    monkeypatch.setattr(cli, "write_kelly_trade_samples", fake_write)

    result = cli.main(
        [
            "kelly",
            "build-trade-samples",
            "--data-dir",
            str(data_dir),
            "--generated-at",
            "2026-07-11 11:00",
        ]
    )

    assert result == 0
    assert captured["load_data_dir"] == data_dir
    assert captured["include_strategy_capital"] is False
    assert captured["include_trade_samples"] is False
    assert captured["experiments"] == [{"experiment_id": "trend_us"}]
    assert captured["paper_orders_payload"] == paper_orders_payload
    assert captured["generated_at"] == "2026-07-11 11:00"
    assert captured["write_data_dir"] == data_dir
    output = capsys.readouterr().out
    assert "samples: 2" in output
    assert "open_positions: 1" in output
    assert "skipped_orders: 3" in output
    assert f"latest: {latest_path}" in output
