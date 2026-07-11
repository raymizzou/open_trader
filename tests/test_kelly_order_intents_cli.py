from __future__ import annotations

import json
from pathlib import Path

import pytest

import open_trader.cli as cli
from tests.test_kelly_order_intents import _write_entry_exit_lab_fixtures


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
            "intents": [
                {
                    "intent_id": "a",
                    "suggested_position_pct": "3%",
                    "parameter_source": "futu_paper_order_samples",
                    "strategy_stats_generated_at": "2026-07-11 12:01",
                    "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                    "source_trade_samples_digest": "a" * 64,
                },
                {"intent_id": "b"},
            ],
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
    assert captured["payload"]["intents"][0] == {
        "intent_id": "a",
        "suggested_position_pct": "3%",
        "parameter_source": "futu_paper_order_samples",
        "strategy_stats_generated_at": "2026-07-11 12:01",
        "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
        "source_trade_samples_digest": "a" * 64,
    }
    output = capsys.readouterr().out
    assert "intents: 2" in output
    assert f"latest: {latest_path}" in output


@pytest.mark.parametrize(
    "strategy_stats_state",
    ["missing", "malformed", "stale", "incomplete"],
)
def test_kelly_build_order_intents_cli_writes_only_exit_when_stats_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    strategy_stats_state: str,
) -> None:
    data_dir = tmp_path / "data"
    _write_entry_exit_lab_fixtures(
        data_dir,
        strategy_stats_state=strategy_stats_state,
    )

    result = cli.main(
        [
            "kelly",
            "build-order-intents",
            "--data-dir",
            str(data_dir),
            "--created-at",
            "2026-07-11 12:02",
        ]
    )

    assert result == 0
    payload = json.loads(
        (data_dir / "latest/kelly_order_intents.json").read_text(encoding="utf-8")
    )
    assert payload["intent_count"] == 1
    assert [item["intent_type"] for item in payload["intents"]] == ["exit"]
    assert "intents: 1" in capsys.readouterr().out

    risk_result = cli.main(
        [
            "kelly",
            "check-order-risk",
            "--data-dir",
            str(data_dir),
            "--checked-at",
            "2026-07-11 12:03",
        ]
    )

    assert risk_result == 0
    risk_payload = json.loads(
        (data_dir / "latest/kelly_order_risk_checks.json").read_text(
            encoding="utf-8"
        )
    )
    assert risk_payload["approved_count"] == 1
    assert risk_payload["blocked_count"] == 0
    assert "approved: 1" in capsys.readouterr().out
