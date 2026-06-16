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
    assert "--ta-provider" in output
    assert "--ta-deep-model" in output
    assert "--ta-quick-model" in output
    assert "--ta-timeout-seconds" in output
    assert "--ta-max-retries" in output
    assert "--symbol-timeout-seconds" in output
    assert "--no-symbol-timeout" in output
    assert "--exclude-symbols" in output
    assert "--max-workers" in output
    assert "--dry-run" in output


def test_run_premarket_main_wires_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeSubprocessRunner:
        def __init__(
            self,
            *,
            project_path: Path,
            config_overrides: dict[str, object],
            timeout_seconds: float | None,
        ) -> None:
            captured["tradingagents_path"] = project_path
            captured["tradingagents_config_overrides"] = config_overrides
            captured["symbol_timeout_seconds"] = timeout_seconds

    class FakeOpenAIClassifierClient:
        def __init__(self, *, model: str) -> None:
            captured["model"] = model

    class FakeChangeClassifier:
        def __init__(self, client: object) -> None:
            captured["classifier_client"] = client

    def fake_run_premarket(**kwargs: object) -> PremarketResult:
        captured.update(kwargs)
        advice_runner_factory = kwargs["advice_runner_factory"]
        assert callable(advice_runner_factory)
        captured["factory_result"] = advice_runner_factory()
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

    monkeypatch.setattr(cli, "TradingAgentsSubprocessRunner", FakeSubprocessRunner)
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
            "--ta-provider",
            "deepseek",
            "--ta-deep-model",
            "deepseek-v4-pro",
            "--ta-quick-model",
            "deepseek-v4-flash",
            "--ta-timeout-seconds",
            "45",
            "--ta-max-retries",
            "1",
            "--symbol-timeout-seconds",
            "90",
            "--max-workers",
            "4",
            "--symbols",
            "VIXY,QQQ",
            "--exclude-symbols",
            "AGRZ, ARGG",
            "--classifier-model",
            "gpt-5.4-mini",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["run_date"] == "2026-06-16"
    assert captured["portfolio_path"] == Path("portfolio.csv")
    assert captured["symbols"] == {"VIXY", "QQQ"}
    assert captured["excluded_symbols"] == {"AGRZ", "ARGG"}
    assert captured["update_latest"] is False
    assert captured["advice_runner"] is None
    assert isinstance(captured["factory_result"], FakeSubprocessRunner)
    assert captured["tradingagents_path"] == Path("/tmp/TradingAgents")
    assert captured["tradingagents_config_overrides"] == {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-v4-pro",
        "quick_think_llm": "deepseek-v4-flash",
        "llm_timeout": 45,
        "llm_max_retries": 1,
    }
    assert captured["symbol_timeout_seconds"] == 90
    assert captured["max_workers"] == 4
    assert captured["model"] == "gpt-5.4-mini"

    output = capsys.readouterr().out
    assert "eligible: 2" in output
    assert "advice: 2" in output
    assert "actions: 1" in output
    assert "advice_csv:" in output
    assert "actions_csv:" in output
    assert "report:" in output


def test_run_premarket_main_allows_disabling_symbol_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSubprocessRunner:
        def __init__(
            self,
            *,
            project_path: Path,
            config_overrides: dict[str, object],
            timeout_seconds: float | None,
        ) -> None:
            captured["symbol_timeout_seconds"] = timeout_seconds

    class FakeOpenAIClassifierClient:
        def __init__(self, *, model: str) -> None:
            pass

    class FakeChangeClassifier:
        def __init__(self, client: object) -> None:
            pass

    def fake_run_premarket(**kwargs: object) -> PremarketResult:
        advice_runner_factory = kwargs["advice_runner_factory"]
        assert callable(advice_runner_factory)
        advice_runner_factory()
        data_dir = kwargs["data_dir"]
        reports_dir = kwargs["reports_dir"]
        assert isinstance(data_dir, Path)
        assert isinstance(reports_dir, Path)
        return PremarketResult(
            eligible_count=1,
            advice_count=1,
            action_count=0,
            advice_path=data_dir / "runs" / "2026-06-16" / "trading_advice.csv",
            classifications_path=data_dir
            / "runs"
            / "2026-06-16"
            / "change_classifications.csv",
            actions_path=data_dir / "runs" / "2026-06-16" / "premarket_actions.csv",
            report_path=reports_dir / "premarket" / "2026-06-16.md",
        )

    monkeypatch.setattr(cli, "TradingAgentsSubprocessRunner", FakeSubprocessRunner)
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
            "--no-symbol-timeout",
        ]
    )

    assert result == 0
    assert captured["symbol_timeout_seconds"] is None


def test_run_daily_premarket_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-daily-premarket", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--date" in output
    assert "--config" in output
    assert "--dry-run" in output


def test_run_daily_premarket_main_wires_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *, config: object) -> None:
            captured["config"] = config

        def run(self, run_date: str):
            captured["run_date"] = run_date
            return type(
                "DailyRunResult",
                (),
                {
                    "status": "success",
                    "status_path": tmp_path
                    / "data/runs/2026-06-17/daily_run_status.json",
                    "report_path": tmp_path / "reports/daily_runs/2026-06-17.md",
                    "log_path": tmp_path / "logs/daily_premarket/2026-06-17.log",
                },
            )()

    def fake_load_env_config(path: Path, *, dry_run: bool):
        captured["config_path"] = path
        captured["dry_run"] = dry_run
        return object()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)
    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)

    result = cli.main(
        [
            "run-daily-premarket",
            "--date",
            "2026-06-17",
            "--config",
            str(tmp_path / "daily.env"),
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["config_path"] == tmp_path / "daily.env"
    assert captured["dry_run"] is True
    assert captured["run_date"] == "2026-06-17"
    output = capsys.readouterr().out
    assert "status: success" in output
    assert "status_json:" in output
    assert "report:" in output
    assert "log:" in output


def test_run_daily_premarket_accepts_today_date() -> None:
    parser = build_parser()

    args = parser.parse_args(["run-daily-premarket", "--date", "today"])

    assert args.date == "today"
