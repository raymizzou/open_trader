from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.advice.premarket import PremarketResult
from open_trader.cli import build_parser


@pytest.mark.parametrize("value", [None, "", "   ", " , , "])
def test_parse_symbol_subset_returns_none_for_blank_values(
    value: str | None,
) -> None:
    assert cli._parse_symbol_subset(value) is None


def test_run_premarket_parser_accepts_valid_date() -> None:
    parser = build_parser()

    args = parser.parse_args(["run-premarket", "--date", "2026-06-16"])

    assert args.date == "2026-06-16"


@pytest.mark.parametrize(
    "value",
    ["2026-6-16", "today", "2026-06-16/foo", "2026-02-30"],
)
def test_run_premarket_parser_rejects_invalid_dates(value: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-premarket", "--date", value])

    assert exc_info.value.code == 2


def test_parse_symbol_subset_normalizes_comma_separated_values() -> None:
    assert cli._parse_symbol_subset(" vixy, QQQ,, tqqq ") == {"VIXY", "QQQ", "TQQQ"}


def test_run_premarket_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-premarket", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--date" in output
    assert "--portfolio" in output
    assert "--tradingagents-path" in output
    assert "--dry-run" in output


def test_run_premarket_main_wires_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeAdapter:
        @classmethod
        def from_project_path(cls, path: Path) -> FakeAdapter:
            captured["tradingagents_path"] = path
            return cls()

    class FakeOpenAIClassifierClient:
        def __init__(self, *, model: str) -> None:
            captured["model"] = model

    class FakeChangeClassifier:
        def __init__(self, client: object) -> None:
            captured["classifier_client"] = client

    def fake_run_premarket(**kwargs: object) -> PremarketResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        reports_dir = kwargs["reports_dir"]
        assert isinstance(data_dir, Path)
        assert isinstance(reports_dir, Path)
        return PremarketResult(
            eligible_count=2,
            advice_count=2,
            action_count=1,
            advice_path=data_dir / "runs" / "2026-06-16" / "trading_advice.csv",
            classifications_path=data_dir
            / "runs"
            / "2026-06-16"
            / "change_classifications.csv",
            actions_path=data_dir / "runs" / "2026-06-16" / "premarket_actions.csv",
            report_path=reports_dir / "premarket" / "2026-06-16.md",
        )

    monkeypatch.setattr(cli, "TradingAgentsAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIClassifierClient", FakeOpenAIClassifierClient)
    monkeypatch.setattr(cli, "ChangeClassifier", FakeChangeClassifier)
    monkeypatch.setattr(cli, "run_premarket", fake_run_premarket)

    result = cli.main(
        [
            "run-premarket",
            "--date",
            "2026-06-16",
            "--portfolio",
            "portfolio.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--tradingagents-path",
            "/tmp/TradingAgents",
            "--symbols",
            "VIXY,QQQ",
            "--classifier-model",
            "gpt-5.4-mini",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["run_date"] == "2026-06-16"
    assert captured["portfolio_path"] == Path("portfolio.csv")
    assert captured["symbols"] == {"VIXY", "QQQ"}
    assert captured["update_latest"] is False
    assert captured["tradingagents_path"] == Path("/tmp/TradingAgents")
    assert captured["model"] == "gpt-5.4-mini"

    output = capsys.readouterr().out
    assert "eligible: 2" in output
    assert "advice: 2" in output
    assert "actions: 1" in output
    assert "advice_csv:" in output
    assert "actions_csv:" in output
    assert "report:" in output
