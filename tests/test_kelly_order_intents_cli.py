from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli


def test_kelly_build_order_intents_parser_accepts_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "build-order-intents",
            "--data-dir",
            "data",
            "--created-at",
            "2026-07-10 13:30",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "build-order-intents"
    assert args.data_dir == Path("data")
    assert args.created_at == "2026-07-10 13:30"


def test_kelly_build_order_intents_main_writes_payload_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    latest_path = tmp_path / "data/latest/kelly_order_intents.json"

    def fake_build(**kwargs: object) -> dict[str, object]:
        captured["build_kwargs"] = kwargs
        return {
            "schema_version": "open_trader.kelly_order_intents.v1",
            "created_at": kwargs["created_at"],
            "intent_count": 2,
            "intents": [{"intent_id": "a"}, {"intent_id": "b"}],
        }

    def fake_write(data_dir: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "build_kelly_order_intents", fake_build)
    monkeypatch.setattr(cli, "write_kelly_order_intents", fake_write)

    result = cli.main(
        [
            "kelly",
            "build-order-intents",
            "--data-dir",
            str(tmp_path / "data"),
            "--created-at",
            "2026-07-10 13:30",
        ]
    )

    assert result == 0
    assert captured["build_kwargs"] == {
        "data_dir": tmp_path / "data",
        "created_at": "2026-07-10 13:30",
    }
    assert captured["write_data_dir"] == tmp_path / "data"
    assert captured["payload"]["intent_count"] == 2
    output = capsys.readouterr().out
    assert "intents: 2" in output
    assert f"latest: {latest_path}" in output
