from __future__ import annotations

import json
from pathlib import Path

import pytest

import open_trader.cli as cli


def test_kelly_check_order_risk_parser_accepts_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "check-order-risk",
            "--data-dir",
            "data",
            "--checked-at",
            "2026-07-10 13:31",
            "--max-entry-position-pct",
            "4",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "check-order-risk"
    assert args.data_dir == Path("data")
    assert args.checked_at == "2026-07-10 13:31"
    assert args.max_entry_position_pct == "4"


def test_kelly_check_order_risk_main_writes_payload_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    latest_path = tmp_path / "data/latest/kelly_order_risk_checks.json"

    def fake_build(**kwargs: object) -> dict[str, object]:
        captured["build_kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_risk_checks.v1",
            "checked_at": kwargs["checked_at"],
            "max_entry_position_pct": kwargs["max_entry_position_pct"],
            "intent_count": 3,
            "approved_count": 2,
            "blocked_count": 1,
            "checks": [],
        }

    def fake_write(data_dir: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "build_kelly_order_risk_checks", fake_build)
    monkeypatch.setattr(cli, "write_kelly_order_risk_checks", fake_write)

    result = cli.main(
        [
            "kelly",
            "check-order-risk",
            "--data-dir",
            str(tmp_path / "data"),
            "--checked-at",
            "2026-07-10 13:31",
            "--max-entry-position-pct",
            "4",
        ]
    )

    assert result == 0
    assert captured["build_kwargs"] == {
        "data_dir": tmp_path / "data",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
    }
    assert captured["write_data_dir"] == tmp_path / "data"
    assert captured["payload"]["blocked_count"] == 1
    output = capsys.readouterr().out
    assert "intents: 3" in output
    assert "approved: 2" in output
    assert "blocked: 1" in output
    assert f"latest: {latest_path}" in output


def test_kelly_check_order_risk_main_passes_latest_strategy_capital(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    strategy_capital_payload = {"strategies": []}
    (latest_dir / "kelly_strategy_capital.json").write_text(
        json.dumps(strategy_capital_payload),
        encoding="utf-8",
    )

    def fake_build(**kwargs: object) -> dict[str, object]:
        captured["build_kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_risk_checks.v1",
            "intent_count": 0,
            "approved_count": 0,
            "blocked_count": 0,
            "checks": [],
        }

    def fake_write(data_dir_arg: Path, payload: dict[str, object]) -> Path:
        return data_dir_arg / "latest/kelly_order_risk_checks.json"

    monkeypatch.setattr(cli, "build_kelly_order_risk_checks", fake_build)
    monkeypatch.setattr(cli, "write_kelly_order_risk_checks", fake_write)

    result = cli.main(
        [
            "kelly",
            "check-order-risk",
            "--data-dir",
            str(data_dir),
        ]
    )

    assert result == 0
    assert captured["build_kwargs"]["strategy_capital_payload"] == {
        "strategies": [],
    }
