from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.cli as cli
from open_trader.advice.premarket import PremarketResult
from open_trader.cli import build_parser
from open_trader.notifications import CompositeNotifier


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
            "deepseek-v4-flash",
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
    assert captured["model"] == "deepseek-v4-flash"

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
    assert "--market" in output
    assert "--config" in output
    assert "--dry-run" in output


def test_run_daily_premarket_requires_market() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-daily-premarket", "--date", "2026-06-17"])

    assert exc_info.value.code == 2


def test_run_daily_premarket_rejects_invalid_market() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            ["run-daily-premarket", "--date", "2026-06-17", "--market", "CN"]
        )

    assert exc_info.value.code == 2


def test_run_daily_premarket_main_wires_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *, config: object, notifier: object) -> None:
            captured["config"] = config
            captured["notifier"] = notifier

        def run(self, *, run_date: str, market: str, dry_run: bool):
            captured["run_date"] = run_date
            captured["market"] = market
            captured["runner_dry_run"] = dry_run
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

    def fake_build_notifier(config: object) -> object:
        captured["notifier_config"] = config
        return object()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)
    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(cli, "build_notifier", fake_build_notifier)

    result = cli.main(
        [
            "run-daily-premarket",
            "--date",
            "2026-06-17",
            "--market",
            "US",
            "--config",
            str(tmp_path / "daily.env"),
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["config_path"] == tmp_path / "daily.env"
    assert captured["dry_run"] is True
    assert captured["notifier_config"] is captured["config"]
    assert captured["notifier"] is not None
    assert captured["run_date"] == "2026-06-17"
    assert captured["market"] == "US"
    assert captured["runner_dry_run"] is True
    output = capsys.readouterr().out
    assert "status: success" in output
    assert "status_json:" in output
    assert "report:" in output
    assert "log:" in output


@pytest.mark.parametrize("status", ["failed", "already_running"])
def test_run_daily_premarket_main_returns_nonzero_for_unsuccessful_runner_status(
    status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRunner:
        def __init__(self, *, config: object, notifier: object) -> None:
            pass

        def run(self, *, run_date: str, market: str, dry_run: bool):
            return type(
                "DailyRunResult",
                (),
                {
                    "status": status,
                    "status_path": tmp_path
                    / "data/runs/2026-06-17/daily_run_status.json",
                    "report_path": tmp_path / "reports/daily_runs/2026-06-17.md",
                    "log_path": tmp_path / "logs/daily_premarket/2026-06-17.log",
                },
            )()

    def fake_load_env_config(path: Path, *, dry_run: bool):
        return object()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)
    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(cli, "build_notifier", lambda config: object())

    result = cli.main(
        ["run-daily-premarket", "--date", "2026-06-17", "--market", "US"]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert f"status: {status}" in output
    assert "status_json:" in output
    assert "report:" in output
    assert "log:" in output


def test_test_notification_main_sends_chinese_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, str]] = []
    config = SimpleNamespace()

    class FakeNotifier:
        def notify(self, title: str, message: str) -> None:
            sent.append((title, message))

    def fake_load_env_config(path: Path, *, dry_run: bool):
        assert path == tmp_path / "daily.env"
        assert dry_run is False
        return config

    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: FakeNotifier())

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 0
    assert sent == [("Open Trader 测试通知", "这是一条 Open Trader 测试通知。")]
    assert "通知测试已发送" in capsys.readouterr().out


def test_test_notification_main_returns_nonzero_when_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("delivery failed")

    def fake_load_env_config(path: Path, *, dry_run: bool):
        assert dry_run is False
        return SimpleNamespace()

    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(cli, "build_notifier", lambda config: FailingNotifier())

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 1
    assert "通知测试失败" in capsys.readouterr().err


def test_test_notification_main_returns_nonzero_when_composite_child_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, str]] = []

    class FailingNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("feishu failed")

    class WorkingNotifier:
        def notify(self, title: str, message: str) -> None:
            sent.append((title, message))

    def fake_load_env_config(path: Path, *, dry_run: bool):
        assert dry_run is False
        return SimpleNamespace()

    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(
        cli,
        "build_notifier",
        lambda config: CompositeNotifier([FailingNotifier(), WorkingNotifier()]),
    )

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 1
    assert sent == [("Open Trader 测试通知", "这是一条 Open Trader 测试通知。")]
    error = capsys.readouterr().err
    assert "通知测试失败" in error
    assert "feishu failed" in error


def test_run_daily_premarket_accepts_today_date() -> None:
    parser = build_parser()

    args = parser.parse_args(["run-daily-premarket", "--date", "today", "--market", "US"])

    assert args.date == "today"
    assert args.market == "US"


def test_run_daily_premarket_today_reports_invalid_timezone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_load_env_config(path: Path, *, dry_run: bool):
        return SimpleNamespace(timezone="Invalid/Timezone")

    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run-daily-premarket", "--date", "today", "--market", "US"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "Invalid/Timezone" in error


def test_extract_technical_facts_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["extract-technical-facts", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--advice" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--market" in output
    assert "--update-latest" in output


def test_extract_technical_facts_requires_advice() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["extract-technical-facts"])

    assert exc_info.value.code == 2


def test_extract_technical_facts_rejects_invalid_market() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            ["extract-technical-facts", "--advice", "advice.csv", "--market", "CN"]
        )

    assert exc_info.value.code == 2


def test_extract_technical_facts_main_wires_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    advice_path = tmp_path / "trading_advice.csv"
    advice_path.write_text("run_date,symbol,market,raw_decision\n", encoding="utf-8")

    class FakeExtractor:
        pass

    def fake_generate_technical_facts(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            run_date="2026-06-19",
            records=3,
            extracted=2,
            failed=1,
            reused=0,
            run_path=tmp_path / "data/runs/2026-06-19/HK/technical_facts.json",
            latest_path=tmp_path / "data/latest/HK/technical_facts.json",
        )

    monkeypatch.setattr(cli, "LLMTechnicalFactsExtractor", FakeExtractor)
    monkeypatch.setattr(cli, "generate_technical_facts", fake_generate_technical_facts)

    result = cli.main(
        [
            "extract-technical-facts",
            "--advice",
            str(advice_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-19",
            "--market",
            "hk",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["advice_path"] == advice_path
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-19"
    assert captured["market"] == "HK"
    assert captured["update_latest"] is True
    assert isinstance(captured["extractor"], FakeExtractor)
    output = capsys.readouterr().out
    assert "run_date: 2026-06-19" in output
    assert "technical_facts: 3" in output
    assert "extracted: 2" in output
    assert "failed: 1" in output
    assert "reused: 0" in output
    assert "technical_facts_json:" in output
    assert "latest:" in output


def test_extract_technical_facts_missing_advice_reports_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    constructed = False

    class FailingExtractor:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True
            raise Exception("LLM should not be initialized for a missing advice file")

    advice_path = tmp_path / "missing_trading_advice.csv"
    monkeypatch.setattr(cli, "LLMTechnicalFactsExtractor", FailingExtractor)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["extract-technical-facts", "--advice", str(advice_path)])

    assert exc_info.value.code == 2
    assert constructed is False
    error = capsys.readouterr().err
    assert str(advice_path) in error
    assert "LLM should not be initialized" not in error
    assert "Traceback" not in error


def test_extract_technical_facts_extractor_init_failure_reports_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    advice_path.write_text("run_date,symbol,market,raw_decision\n", encoding="utf-8")

    class FailingExtractor:
        def __init__(self) -> None:
            raise Exception("missing LLM credentials")

    monkeypatch.setattr(cli, "LLMTechnicalFactsExtractor", FailingExtractor)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["extract-technical-facts", "--advice", str(advice_path)])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "missing LLM credentials" in error
    assert "Traceback" not in error


def test_extract_decision_facts_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["extract-decision-facts", "--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--advice" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--market" in output
    assert "--update-latest" in output


def test_extract_decision_facts_main_wires_generator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    advice = tmp_path / "trading_advice.csv"
    advice.write_text("run_date,symbol,market,raw_decision\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeExtractor:
        pass

    def fake_generate_decision_facts(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            run_date="2026-06-22",
            records=2,
            extracted=2,
            failed=0,
            run_path=tmp_path / "data/runs/2026-06-22/US/decision_facts.json",
            latest_path=tmp_path / "data/latest/US/decision_facts.json",
        )

    monkeypatch.setattr(cli, "LLMDecisionFactsExtractor", lambda: FakeExtractor())
    monkeypatch.setattr(cli, "generate_decision_facts", fake_generate_decision_facts)

    result = cli.main(
        [
            "extract-decision-facts",
            "--advice",
            str(advice),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-22",
            "--market",
            "US",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["advice_path"] == advice
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-22"
    assert captured["market"] == "US"
    assert captured["update_latest"] is True
    output = capsys.readouterr().out
    assert "decision_facts: 2" in output
    assert "decision_facts_json:" in output


def test_extract_futu_skill_facts_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["extract-futu-skill-facts", "--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--market" in output
    assert "--window-days" in output
    assert "--update-latest" in output


def test_extract_futu_skill_facts_main_wires_generator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text("market,symbol,asset_class\nUS,NVDA,stock\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeCompositeExtractor:
        pass

    def fake_generate_futu_skill_facts(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            run_date="2026-07-01",
            records=1,
            generated=1,
            failed=0,
            run_path=tmp_path / "data/runs/2026-07-01/US/futu_skill_facts.json",
            latest_path=tmp_path / "data/latest/US/futu_skill_facts.json",
        )

    monkeypatch.setattr(cli, "FutuSkillFactsExtractor", lambda: FakeCompositeExtractor())
    monkeypatch.setattr(cli, "generate_futu_skill_facts", fake_generate_futu_skill_facts)

    result = cli.main(
        [
            "extract-futu-skill-facts",
            "--portfolio",
            str(portfolio),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-07-01",
            "--market",
            "US",
            "--window-days",
            "14",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["portfolio_path"] == portfolio
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-07-01"
    assert captured["market"] == "US"
    assert captured["update_latest"] is True
    assert captured["window_days"] == 14
    assert isinstance(captured["extractor"], FakeCompositeExtractor)
    output = capsys.readouterr().out
    assert "futu_skill_facts: 1" in output
    assert "futu_skill_facts_json:" in output


def test_extract_futu_skill_facts_rejects_invalid_window_days(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text("market,symbol,asset_class\nUS,NVDA,stock\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "extract-futu-skill-facts",
                "--portfolio",
                str(portfolio),
                "--date",
                "2026-07-02",
                "--window-days",
                "0",
            ]
        )

    assert excinfo.value.code == 2


def test_extract_tradingagents_summary_main_writes_summary_with_fake_extractor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    advice = tmp_path / "trading_advice.csv"
    plan = tmp_path / "trading_plan.csv"
    actions = tmp_path / "trade_actions.csv"

    def write_csv(
        path: Path,
        fieldnames: list[str],
        rows: list[dict[str, str]],
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    write_csv(
        advice,
        [
            "run_date",
            "symbol",
            "market",
            "advice_action",
            "advice_summary",
            "raw_decision",
            "fallback_from_date",
        ],
        [
            {
                "run_date": "2026-06-23",
                "symbol": "DRAM",
                "market": "US",
                "advice_action": "Underweight",
                "advice_summary": "TradingAgents 认为价格延伸且财报前风险回报转弱。",
                "raw_decision": json.dumps(
                    {"state": {"final_trade_decision": "Underweight DRAM"}},
                    ensure_ascii=False,
                ),
                "fallback_from_date": "2026-06-22",
            }
        ],
    )
    write_csv(
        plan,
        [
            "run_date",
            "symbol",
            "market",
            "rating",
            "agent_reason",
            "agent_excerpt",
        ],
        [
            {
                "run_date": "2026-06-23",
                "symbol": "DRAM",
                "market": "US",
                "rating": "Underweight",
                "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
                "agent_excerpt": "Price is extended.",
            }
        ],
    )
    write_csv(
        actions,
        ["run_date", "symbol", "market", "action", "reason", "agent_reason"],
        [
            {
                "run_date": "2026-06-23",
                "symbol": "DRAM",
                "market": "US",
                "action": "TRIM",
                "reason": "Current price is at or above target 1.",
                "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
            }
        ],
    )

    class FakeExtractor:
        def extract(self, **kwargs: str) -> dict[str, object]:
            assert kwargs["market"] == "US"
            assert kwargs["symbol"] == "DRAM"
            return {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "core_reason": (
                    "内存周期仍具支撑，但价格涨幅过快、技术动能转弱且财报前波动风险上升，"
                    "所以 TA 建议先降低仓位保留弹性。"
                ),
                "reason_fields": {
                    "main_judgment": "结构性主题仍成立但短期风险回报转弱",
                    "evidence_1": "价格涨幅过快且技术动能转弱",
                    "evidence_2": "财报前波动风险上升",
                    "risk_or_counterpoint": "内存周期仍具支撑",
                    "action_logic": "减仓控制风险并保留后续弹性",
                },
            }

    monkeypatch.setattr(cli, "LLMTradingAgentsSummaryExtractor", FakeExtractor)

    result = cli.main(
        [
            "extract-tradingagents-summary",
            "--advice",
            str(advice),
            "--plan",
            str(plan),
            "--actions",
            str(actions),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-23",
            "--market",
            "US",
            "--update-latest",
        ]
    )

    assert result == 0
    summary_path = (
        tmp_path
        / "data"
        / "runs"
        / "2026-06-23"
        / "US"
        / "tradingagents_summary.json"
    )
    latest_path = tmp_path / "data" / "latest" / "US" / "tradingagents_summary.json"
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["latest_run_date"] == "2026-06-23"
    assert payload["records"][0]["ta_report_date"] == "2026-06-22"
    assert latest_path.exists()
    output = capsys.readouterr().out
    assert "run_date: 2026-06-23" in output
    assert "summaries: 1" in output
    assert "extracted: 1" in output
    assert "failed: 0" in output
    assert f"summary_json: {summary_path}" in output
    assert f"latest: {latest_path}" in output
