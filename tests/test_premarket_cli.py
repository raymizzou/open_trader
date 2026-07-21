from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import open_trader.cli as cli
from open_trader.advice.premarket import PremarketResult
from open_trader.cli import build_parser
from open_trader.daily_premarket import DailyPremarketConfig, NotificationAttempt
from open_trader.notifications import CompositeNotifier


def test_dashboard_cli_reads_three_distinct_simulate_account_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "serve_dashboard",
        lambda config, **_: captured.setdefault("config", config),
    )
    monkeypatch.setattr(
        cli,
        "_load_optional_env_values",
        lambda _: {
            "OPEN_TRADER_TREND_REVIEW_CN_SIMULATE_ACC_ID": "101",
            "OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID": "102",
            "OPEN_TRADER_TREND_REVIEW_HK_SIMULATE_ACC_ID": "103",
        },
    )

    assert cli.main(["dashboard"]) == 0
    config = captured["config"]
    assert getattr(config, "trend_review_cn_simulate_acc_id") == 101
    assert getattr(config, "trend_review_us_simulate_acc_id") == 102
    assert getattr(config, "trend_review_hk_simulate_acc_id") == 103


@pytest.mark.parametrize("value", ["not-an-integer", "-1"])
def test_dashboard_cli_rejects_invalid_simulate_account_id(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "serve_dashboard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_load_optional_env_values",
        lambda _: {"OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID": value},
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["dashboard"])

    assert exc_info.value.code == 2
    assert (
        "OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID must be a positive integer"
        in capsys.readouterr().err
    )


def test_dashboard_cli_rejects_duplicate_positive_simulate_account_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "serve_dashboard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_load_optional_env_values",
        lambda _: {
            "OPEN_TRADER_TREND_REVIEW_CN_SIMULATE_ACC_ID": "101",
            "OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID": "101",
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["dashboard"])

    assert exc_info.value.code == 2
    assert (
        "trend review simulate account IDs must be distinct"
        in capsys.readouterr().err
    )


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
    assert "--max-workers" in output


def test_trend_a_share_report_parser_has_expected_defaults() -> None:
    args = build_parser().parse_args(["trend-a-share-report"])

    assert args.date == "today"
    assert args.config == Path("config/daily_premarket.env")
    assert args.revision is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [("generated", 0), ("existing", 0), ("holiday", 0), ("failed", 1)],
)
def test_trend_a_share_report_main_dispatches_and_returns_status(
    status: str,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    review_calls: list[tuple[object, str, str]] = []
    config = SimpleNamespace(
        timezone="Asia/Shanghai",
        trend_animals_api_key="secret",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
    )

    def fake_runner(**kwargs: object) -> object:
        captured.update(kwargs)
        json_path = tmp_path / "report.json"
        json_path.write_text(
            json.dumps({"as_of_date": "2026-07-14"}), encoding="utf-8"
        )
        return SimpleNamespace(
            status=status,
            report_path=tmp_path / "report.md" if status == "generated" else None,
            json_path=json_path if status == "generated" else None,
        )

    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: "notifier")
    monkeypatch.setattr(cli, "run_a_share_trend_report", fake_runner)
    monkeypatch.setattr(
        cli,
        "run_trend_review_close",
        lambda *args: (
            review_calls.append(args),
            (_ for _ in ()).throw(RuntimeError("review failed")),
        )[1],
    )

    result = cli.main([
        "trend-a-share-report", "--date", "2026-07-14",
        "--config", str(tmp_path / "daily.env"), "--revision",
    ])

    assert result == expected
    assert captured == {
        "config": config,
        "run_date": "2026-07-14",
        "revision": True,
        "notifier": "notifier",
    }
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == status
    assert review_calls == (
        [(config, "CN", "2026-07-14")]
        if status in {"generated", "existing"}
        else []
    )
    assert ("trend review close failed: review failed" in output.err) is bool(
        review_calls
    )


def test_trend_review_loader_prefers_latest_numeric_revision(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports/trend_a_share"
    report_dir.mkdir(parents=True)
    valid = {
        "schema_version": 1,
        "execution_date": "2026-07-16",
        "as_of_date": "2026-07-16",
        "generated_at": "2026-07-16T18:00:00+08:00",
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [], "holding_decisions": [], "top10_candidates": [],
        },
    }
    for filename, version in (
        ("2026-07-16.json", "base"),
        ("2026-07-16-r9.json", "r9"),
        ("2026-07-16-r10.json", "r10"),
    ):
        (report_dir / filename).write_text(
            json.dumps({**valid, "version": version}),
            encoding="utf-8",
        )
    invalid_reports = [
        {**valid, "schema_version": 2},
        {**valid, "metadata": {"market": "US", "broker": "tiger"}},
        {**valid, "as_of_date": "2026-07-17"},
        {**valid, "strategy_judgments": {**valid["strategy_judgments"], "formal_actions": [{"action": "WAIT", "symbol": "600001"}]}},
        {
            **valid,
            "strategy_judgments": {
                **valid["strategy_judgments"],
                "formal_actions": [
                    {
                        "action": "BUY",
                        "symbol": "600001",
                        "target_weight": "0.04",
                        "lot_size": 100,
                    }
                ],
            },
        },
    ]
    for revision, payload in enumerate(invalid_reports, 11):
        (report_dir / f"2026-07-16-r{revision}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    config = SimpleNamespace(
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
    )

    report = cli._load_trend_review_report(
        config,
        "CN",
        "2026-07-16",
        date_field="as_of_date",
    )

    assert report["version"] == "r10"


def test_trend_review_loader_accepts_report_named_for_as_of_date(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports/trend_us_tiger"
    report_dir.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "execution_date": "2026-07-17",
        "as_of_date": "2026-07-16",
        "generated_at": "2026-07-17T09:00:00+08:00",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [], "holding_decisions": [], "top10_candidates": [],
        },
    }
    (report_dir / "2026-07-16.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    config = SimpleNamespace(
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
    )

    assert cli._load_trend_review_report(
        config, "US", "2026-07-16", date_field="as_of_date"
    ) == report


@pytest.mark.parametrize(
    "available_buy_symbols",
    [
        ("SH.600002", "SH.600003"),
        ("SH.600002",),
    ],
)
def test_trend_review_open_passes_available_quotes_without_blocking_sell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_buy_symbols: tuple[str, ...],
) -> None:
    report_dir = tmp_path / "reports/trend_a_share"
    report_dir.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "execution_date": "2026-07-17",
        "as_of_date": "2026-07-16",
        "generated_at": "2026-07-16T18:00:00+08:00",
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [
                {"action": "SELL_ALL", "symbol": "600001"},
                {
                    "action": "BUY",
                    "symbol": "600002",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 300,
                    "target_amount": "3000",
                    "atr": "0.5",
                },
                {
                    "action": "BUY",
                    "symbol": "600003",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 300,
                    "target_amount": "3000",
                    "atr": "0.5",
                },
            ],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
    (report_dir / "2026-07-16.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    config = SimpleNamespace(
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
        futu_host="127.0.0.1",
        futu_port=11111,
        timezone="Asia/Shanghai",
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )
    client = SimpleNamespace(
        close=lambda: None,
        place_order=lambda request: {"request": request},
    )
    captured: dict[str, object] = {}
    executed_reports: list[object] = []
    authorizations: list[str] = []
    quoted: list[list[str]] = []
    quote_closes: list[bool] = []
    monkeypatch.setattr(
        cli,
        "FutuSimulateOrderExecutionClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        cli,
        "FutuQuoteClient",
        lambda **kwargs: SimpleNamespace(
            get_snapshots=lambda symbols: (
                quoted.append(list(symbols))
                or {
                    symbol: SimpleNamespace(last_price=Decimal("10"))
                    for symbol in symbols
                    if symbol in available_buy_symbols
                }
            ),
            close=lambda: quote_closes.append(True),
        ),
    )
    monkeypatch.setattr(
        cli,
        "require_trend_executor",
        lambda config: authorizations.append("checked"),
        raising=False,
    )

    def execute(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        executed_reports.append(kwargs["report"])
        kwargs["client"].place_order({"futu_code": "SH.600001"})
        return {
            "status": "submitted",
            "submitted_count": 1,
            "artifact_paths": [str(tmp_path / "result.json")],
        }

    monkeypatch.setattr(cli, "execute_trend_review_open", execute)

    result = cli.run_trend_review_open(config, "CN", "2026-07-17")
    revised = json.loads(json.dumps(report))
    revised["generated_at"] = "2026-07-16T18:01:00+08:00"
    revised["strategy_judgments"]["formal_actions"][1]["estimated_shares"] = 200
    (report_dir / "2026-07-16-r1.json").write_text(
        json.dumps(revised), encoding="utf-8"
    )
    repeated = cli.run_trend_review_open(config, "CN", "2026-07-17")

    assert captured["report"] == report
    assert executed_reports == [report, report]
    assert captured["quote_prices"] == {
        symbol: Decimal("10") for symbol in available_buy_symbols
    }
    assert quoted == [
        ["SH.600002", "SH.600003"],
        ["SH.600002", "SH.600003"],
    ]
    assert quote_closes == [True, True]
    assert authorizations == ["checked", "checked", "checked", "checked"]
    assert result["artifact_path"] == str(tmp_path / "result.json")
    assert repeated["artifact_path"] == str(tmp_path / "result.json")
    assert (
        config.data_dir
        / "trend_review/ledgers/CN/batches/2026-07-17.json"
    ).exists()


def test_trend_review_stop_checks_executor_again_at_broker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = SimpleNamespace(
        close=lambda: None,
        place_order=lambda request: {"request": request},
    )
    authorizations: list[str] = []
    monkeypatch.setattr(
        cli, "FutuSimulateOrderExecutionClient", lambda **kwargs: delegate
    )
    monkeypatch.setattr(
        cli,
        "require_trend_executor",
        lambda config: authorizations.append("checked"),
        raising=False,
    )

    def execute(**kwargs: object) -> dict[str, object]:
        kwargs["client"].place_order({"futu_code": "SH.600001"})
        return {"status": "submitted"}

    monkeypatch.setattr(cli, "execute_trend_review_stop", execute)

    result = cli.run_trend_review_stop(
        SimpleNamespace(
            data_dir=tmp_path,
            futu_host="127.0.0.1",
            futu_port=11111,
            trend_review_cn_simulate_acc_id=101,
            trend_review_us_simulate_acc_id=102,
            trend_review_hk_simulate_acc_id=103,
        ),
        "CN",
        {
            "symbol": "600001",
            "trading_date": "2026-07-20",
            "event_id": "event-1",
            "occurred_at": "2026-07-20T09:31:00+08:00",
        },
    )

    assert result == {"status": "submitted"}
    assert authorizations == ["checked", "checked"]


def test_trend_a_share_report_invalid_private_config_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(
        timezone="Asia/Shanghai",
        trend_animals_api_key="",
        trend_animals_a_share_tm_id=0,
        trend_animals_etf_tm_id=0,
    )
    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)

    assert cli.main(["trend-a-share-report"]) == 2
    assert "TREND_ANIMALS_API_KEY" in capsys.readouterr().err


def test_trend_a_share_report_whitespace_api_key_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(
        timezone="Asia/Shanghai",
        trend_animals_api_key="   ",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
    )
    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)

    assert cli.main(["trend-a-share-report"]) == 2
    assert "TREND_ANIMALS_API_KEY" in capsys.readouterr().err


def test_trend_a_share_report_wrong_positive_pool_id_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(
        timezone="Asia/Shanghai",
        trend_animals_api_key="secret",
        trend_animals_a_share_tm_id=1,
        trend_animals_etf_tm_id=697199,
    )
    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(
        cli,
        "run_a_share_trend_report",
        lambda **kwargs: pytest.fail("invalid config must not run"),
    )

    assert cli.main(["trend-a-share-report"]) == 2
    assert "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID" in capsys.readouterr().err


def test_watch_trend_a_share_parser_has_safe_defaults() -> None:
    args = build_parser().parse_args(["watch-trend-a-share"])

    assert args.config == Path("config/daily_premarket.env")
    assert args.poll_seconds == 5.0
    assert args.reconnect_seconds == 60.0
    assert args.once is False


@pytest.mark.parametrize("value", ["0", "-1"])
@pytest.mark.parametrize("option", ["--poll-seconds", "--reconnect-seconds"])
def test_watch_trend_a_share_rejects_non_positive_intervals(
    option: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["watch-trend-a-share", option, value])

    assert exc_info.value.code == 2


def test_watch_trend_a_share_main_uses_independent_lock_and_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    config = SimpleNamespace(
        timezone="Asia/Shanghai",
        data_dir=tmp_path / "data",
        portfolio=tmp_path / "portfolio.csv",
        futu_host="127.0.0.1",
        futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )
    quote = object()
    simulation_account = object()
    account_calls: list[dict[str, object]] = []

    class RecordingLock:
        def __init__(self, path: Path) -> None:
            captured["lock_path"] = path

        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def fake_watcher(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            status="completed",
            watched_symbol_count=1,
            trigger_count=0,
            exception_count=0,
            unknown_quote_count=0,
            events_path=tmp_path / "data/trend_a_share/watch_events.jsonl",
        )

    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: "notifier")
    monkeypatch.setattr(cli, "FutuQuoteClient", lambda **kwargs: quote)
    monkeypatch.setattr(
        cli,
        "load_futu_simulate_trend_account",
        lambda **kwargs: account_calls.append(kwargs) or simulation_account,
        raising=False,
    )
    monkeypatch.setattr(cli, "RunLock", RecordingLock)
    monkeypatch.setattr(cli, "watch_a_share_protection", fake_watcher)
    monkeypatch.setattr(cli, "run_trend_review_open", lambda *args: None)
    monkeypatch.setattr(cli, "run_trend_review_stop", lambda *args: None, raising=False)

    assert cli.main(
        [
            "watch-trend-a-share",
            "--config",
            str(tmp_path / "daily.env"),
            "--poll-seconds",
            "2.5",
            "--reconnect-seconds",
            "30",
            "--once",
        ]
    ) == 0

    assert captured["lock_path"] == tmp_path / "data/runs/.trend_a_share_watch.lock"
    assert captured["portfolio_path"] == config.portfolio
    assert captured["state_path"] == tmp_path / "data/trend_a_share/protection_state.json"
    assert captured["events_path"] == tmp_path / "data/trend_a_share/watch_events.jsonl"
    assert captured["report_lock_path"] == tmp_path / "data/runs/.trend_a_share_report.lock"
    assert captured["quote_client"] is None
    assert callable(captured["quote_client_factory"])
    assert captured["quote_client_factory"]() is quote
    assert captured["notifier"] == "notifier"
    assert captured["poll_seconds"] == 2.5
    assert captured["reconnect_seconds"] == 30.0
    assert captured["once"] is True
    assert captured["account_loader"](
        config.portfolio,
        expected_date="2026-07-17",
        timezone=ZoneInfo("Asia/Shanghai"),
    ) is simulation_account
    assert account_calls == [{
        "host": "127.0.0.1",
        "port": 11111,
        "simulate_acc_id": 101,
        "market": "CN",
        "expected_date": "2026-07-17",
    }]
    assert callable(captured["on_session_open"])
    assert callable(captured["on_protection_trigger"])
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_trend_market_parsers_have_safe_defaults() -> None:
    report = build_parser().parse_args(["trend-market-report", "--market", "US"])
    watch = build_parser().parse_args(["watch-trend-market", "--market", "HK"])

    assert report.market == "US"
    assert report.date == "today"
    assert report.revision is False
    assert watch.market == "HK"
    assert watch.poll_seconds == 5.0
    assert watch.reconnect_seconds == 60.0
    assert watch.once is False


def test_trend_market_help_names_tiger_us(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["trend-market-report", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Tiger US or Phillips HK" in help_text
    assert "Futu US" not in help_text


def test_trend_review_parsers_keep_markets_separate() -> None:
    parser = build_parser()
    opened = parser.parse_args(
        ["trend-review", "open", "--market", "CN", "--date", "2026-07-17"]
    )
    closed = parser.parse_args(
        ["trend-review", "close", "--market", "US", "--date", "2026-07-17"]
    )
    replayed = parser.parse_args(
        ["trend-review", "replay", "--evidence", "evidence.json"]
    )

    assert (opened.trend_review_command, opened.market) == ("open", "CN")
    assert (closed.trend_review_command, closed.market) == ("close", "US")
    assert replayed.evidence == Path("evidence.json")
    assert opened.config == closed.config == replayed.config == Path(
        "config/daily_premarket.env"
    )


@pytest.mark.parametrize("command", ["open", "close"])
def test_trend_review_command_dispatches_and_prints_json(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(data_dir=tmp_path / "data")
    calls: list[tuple[object, str, str]] = []

    def run(loaded: object, market: str, trading_date: str) -> dict[str, object]:
        calls.append((loaded, market, trading_date))
        return {
            "status": "submitted" if command == "open" else "captured",
            "market": market,
            "date": trading_date,
            "artifact_path": str(tmp_path / "artifact.json"),
        }

    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, f"run_trend_review_{command}", run, raising=False)

    result = cli.main(
        [
            "trend-review",
            command,
            "--market",
            "CN",
            "--date",
            "2026-07-17",
            "--config",
            str(tmp_path / "daily.env"),
        ]
    )

    assert result == 0
    assert calls == [(config, "CN", "2026-07-17")]
    assert json.loads(capsys.readouterr().out)["market"] == "CN"


def test_trend_review_replay_dispatches_without_live_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(data_dir=tmp_path / "data", repo=tmp_path)
    evidence = tmp_path / "evidence.json"
    calls: list[tuple[object, Path]] = []

    def replay(loaded: object, path: Path) -> dict[str, object]:
        calls.append((loaded, path))
        return {
            "status": "corrected",
            "market": "CN",
            "date": "2026-07-16",
            "artifact_path": str(tmp_path / "corrected.json"),
        }

    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "run_trend_review_replay", replay, raising=False)

    assert cli.main(
        ["trend-review", "replay", "--evidence", str(evidence)]
    ) == 0
    assert calls == [(config, evidence)]
    assert json.loads(capsys.readouterr().out)["status"] == "corrected"


def test_trend_market_report_dispatches_generic_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    review_calls: list[tuple[object, str, str]] = []
    config = SimpleNamespace(
        timezone="Asia/Shanghai", trend_animals_api_key="secret",
        trend_animals_us_tm_ids=(622460,), trend_animals_hk_tm_ids=(622494,),
        trend_review_us_simulate_acc_id=0,
    )
    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: "notifier")

    report_json = tmp_path / "us.json"
    report_json.write_text(
        json.dumps({"as_of_date": "2026-07-14"}),
        encoding="utf-8",
    )

    def runner(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            status="generated", report_path=tmp_path / "us.md",
            json_path=report_json,
        )

    monkeypatch.setattr(cli, "run_market_trend_report", runner)
    monkeypatch.setattr(
        cli,
        "run_trend_review_close",
        lambda *args: (
            review_calls.append(args),
            (_ for _ in ()).throw(RuntimeError("review failed")),
        )[1],
    )

    assert cli.main([
        "trend-market-report", "--market", "US", "--date", "2026-07-15",
        "--revision", "--config", str(tmp_path / "daily.env"),
    ]) == 0
    assert captured == {
        "config": config, "market": "US", "run_date": "2026-07-15",
        "revision": True, "notifier": "notifier",
    }
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "generated"
    assert review_calls == [(config, "US", "2026-07-14")]
    assert "trend review close failed: review failed" in output.err


@pytest.mark.parametrize(
    ("market", "account_id", "root"),
    [
        ("HK", 103, "trend_hk_phillips"),
        ("US", 102, "trend_us_tiger"),
    ],
)
def test_watch_trend_market_uses_simulation_account_and_separate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    market: str,
    account_id: int,
    root: str,
) -> None:
    captured: dict[str, object] = {}
    account_calls: list[dict[str, object]] = []
    simulation_account = object()
    config = SimpleNamespace(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        portfolio=tmp_path / "portfolio.csv", futu_host="127.0.0.1", futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )

    class Lock:
        def __init__(self, path: Path) -> None:
            captured["watch_lock"] = path

        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def watcher(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            status="completed", watched_symbol_count=1, trigger_count=0,
            exception_count=0, unknown_quote_count=0,
            events_path=kwargs["events_path"],
        )

    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: "notifier")
    monkeypatch.setattr(cli, "RunLock", Lock)
    monkeypatch.setattr(cli, "watch_market_protection", watcher)
    monkeypatch.setattr(
        cli,
        "load_futu_simulate_trend_account",
        lambda **kwargs: account_calls.append(kwargs) or simulation_account,
    )
    monkeypatch.setattr(cli, "run_trend_review_open", lambda *args: None)
    monkeypatch.setattr(cli, "run_trend_review_stop", lambda *args: None, raising=False)

    assert cli.main([
        "watch-trend-market", "--market", market, "--once",
        "--config", str(tmp_path / "daily.env"),
    ]) == 0

    assert captured["watch_lock"] == tmp_path / f"data/runs/.{root}_watch.lock"
    assert captured["state_path"] == tmp_path / f"data/{root}/protection_state.json"
    assert captured["events_path"] == tmp_path / f"data/{root}/watch_events.jsonl"
    assert captured["report_lock_path"] == tmp_path / f"data/runs/.{root}_report.lock"
    assert captured["market"] == market
    assert captured["quote_client"] is None
    assert callable(captured["quote_client_factory"])
    assert captured["account_loader"](
        config.portfolio,
        expected_date="2026-07-17",
        timezone=ZoneInfo("Asia/Shanghai"),
    ) is simulation_account
    assert account_calls == [{
        "host": "127.0.0.1",
        "port": 11111,
        "simulate_acc_id": account_id,
        "market": market,
        "expected_date": "2026-07-17",
    }]
    assert callable(captured["on_session_open"])
    assert callable(captured["on_protection_trigger"])
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


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
        def __init__(
            self,
            *,
            config: object,
            notifier: object,
            portfolio_refresher: object,
        ) -> None:
            captured["config"] = config
            captured["notifier"] = notifier
            captured["portfolio_refresher"] = portfolio_refresher

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
    assert captured["portfolio_refresher"] is None
    assert captured["run_date"] == "2026-06-17"
    assert captured["market"] == "US"
    assert captured["runner_dry_run"] is True
    output = capsys.readouterr().out
    assert "status: success" in output
    assert "status_json:" in output
    assert "report:" in output
    assert "log:" in output


def test_run_daily_premarket_main_overrides_max_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        max_workers=4,
    )

    class FakeRunner:
        def __init__(
            self,
            *,
            config: object,
            notifier: object,
            portfolio_refresher: object,
        ) -> None:
            captured["config"] = config
            captured["portfolio_refresher"] = portfolio_refresher

        def run(self, *, run_date: str, market: str, dry_run: bool):
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

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)
    monkeypatch.setattr(cli, "load_env_config", lambda path, *, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: object())

    result = cli.main(
        [
            "run-daily-premarket",
            "--date",
            "2026-06-17",
            "--market",
            "US",
            "--max-workers",
            "12",
        ]
    )

    assert result == 0
    loaded_config = captured["config"]
    assert isinstance(loaded_config, DailyPremarketConfig)
    assert loaded_config.max_workers == 12
    assert captured["portfolio_refresher"] is cli.refresh_live_portfolio
    assert config.max_workers == 4

@pytest.mark.parametrize("status", ["failed", "already_running"])
def test_run_daily_premarket_main_returns_nonzero_for_unsuccessful_runner_status(
    status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRunner:
        def __init__(
            self,
            *,
            config: object,
            notifier: object,
            portfolio_refresher: object,
        ) -> None:
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


def test_test_notification_reports_quiet_hour_voice_suppression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace()

    class FakeNotifier:
        def notify(self, title: str, message: str) -> None:
            pass

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: FakeNotifier())
    monkeypatch.setattr(
        cli,
        "send_notification_with_results",
        lambda *args, **kwargs: [
            NotificationAttempt(
                channel="xiaoai",
                success=False,
                suppressed=True,
            )
        ],
    )

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 0
    assert "语音已跳过：静默时段" in capsys.readouterr().out


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
