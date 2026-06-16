from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.daily_premarket as daily_premarket
from open_trader.daily_premarket import (
    DailyPremarketConfig,
    DailyPremarketRunner,
    NullNotifier,
    RunLock,
    load_env_config,
)
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trading_plan import (
    TRADING_PLAN_FIELDNAMES,
    TradingPlanBuildResult,
)


def test_load_env_config_parses_required_values(tmp_path: Path) -> None:
    env = tmp_path / "daily.env"
    env.write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={tmp_path / '.venv/bin/python'}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=21:10",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=secret",
            ]
        ),
        encoding="utf-8",
    )

    config = load_env_config(env)

    assert config.repo == tmp_path
    assert config.python == tmp_path / ".venv/bin/python"
    assert config.timezone == "Asia/Shanghai"
    assert config.deadline == "21:10"
    assert config.futu_host == "127.0.0.1"
    assert config.futu_port == 11111
    assert config.classifier_model == "deepseek-v4-flash"


def test_load_env_config_rejects_missing_required_values(tmp_path: Path) -> None:
    env = tmp_path / "daily.env"
    env.write_text("OPEN_TRADER_REPO=/tmp/open_trader\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_env_config(env)
    message = str(exc_info.value)
    assert message.startswith("missing config value(s):")
    assert "OPEN_TRADER_PYTHON" in message
    assert "DEEPSEEK_API_KEY" in message
    assert "OPENAI_API_KEY" not in message


def test_run_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    first = RunLock(lock_path)
    second = RunLock(lock_path)

    with first:
        with pytest.raises(RuntimeError, match="daily premarket run already active"):
            with second:
                pass


class FakePremarket:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object):
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        advice_path = data_dir / "runs" / run_date / "trading_advice.csv"
        actions_path = data_dir / "runs" / run_date / "premarket_actions.csv"
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        with advice_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "run_date",
                    "symbol",
                    "market",
                    "asset_class",
                    "portfolio_weight_hkd",
                    "risk_flag",
                    "source",
                    "advice_action",
                    "advice_summary",
                    "raw_decision",
                    "status",
                    "error",
                    "source_status",
                    "fallback_reason",
                    "fallback_from_date",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "run_date": run_date,
                    "symbol": "MSFT",
                    "market": "US",
                    "asset_class": "stock",
                    "portfolio_weight_hkd": "1.13%",
                    "risk_flag": "normal",
                    "source": "fake",
                    "advice_action": "Overweight",
                    "advice_summary": "评级：Overweight",
                    "raw_decision": "{}",
                    "status": "ok",
                    "error": "",
                    "source_status": "ok",
                    "fallback_reason": "",
                    "fallback_from_date": "",
                }
            )
        actions_path.write_text(
            "run_date,symbol,market,status\n"
            f"{run_date},MSFT,US,ok\n",
            encoding="utf-8",
        )
        if kwargs["update_latest"]:
            latest_dir = data_dir / "latest"
            latest_dir.mkdir(parents=True, exist_ok=True)
            (latest_dir / "trading_advice.csv").write_text(
                advice_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (latest_dir / "premarket_actions.csv").write_text(
                actions_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        return type(
            "PremarketResult",
            (),
            {
                "eligible_count": 1,
                "advice_count": 1,
                "action_count": 0,
                "advice_path": advice_path,
                "classifications_path": data_dir
                / "runs"
                / run_date
                / "change_classifications.csv",
                "actions_path": actions_path,
                "report_path": Path("reports/premarket") / f"{run_date}.md",
            },
        )()


class FakePlanBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
    ) -> TradingPlanBuildResult:
        self.calls.append(
            {
                "advice_path": advice_path,
                "data_dir": data_dir,
                "run_date": run_date,
                "update_latest": update_latest,
            }
        )
        assert advice_path.exists()
        plan_path = data_dir / "runs" / run_date / "trading_plan.csv"
        latest_path = data_dir / "latest" / "trading_plan.csv"
        row = {
            "run_date": run_date,
            "symbol": "MSFT",
            "market": "US",
            "source_status": "ok",
            "fallback_reason": "",
            "fallback_from_date": "",
            "rating": "Overweight",
            "entry_zone_low": "380",
            "entry_zone_high": "390",
            "add_price": "",
            "stop_loss": "350",
            "target_1": "395",
            "target_2": "420",
            "max_weight": "3%",
            "catalyst": "fake",
            "time_horizon": "1 week",
            "plan_text": "fake plan",
            "status": "active",
            "error": "",
        }
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with plan_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
            writer.writeheader()
            writer.writerow(row)
        if update_latest:
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            with latest_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
                writer.writeheader()
                writer.writerow(row)
        return TradingPlanBuildResult(
            run_date=run_date,
            plan_count=1,
            plan_path=plan_path,
            latest_path=latest_path,
        )


class FakeQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        assert futu_symbols == ["US.MSFT"]
        return {"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("399"))}

    def close(self) -> None:
        self.closed = True


class FailingPlanBuilder:
    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
    ) -> TradingPlanBuildResult:
        raise RuntimeError("plan builder failed")


class RaisingCloseQuoteClient(FakeQuoteClient):
    def close(self) -> None:
        raise RuntimeError("close failed")


def test_daily_runner_writes_success_status_and_report(tmp_path: Path) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    status_path = tmp_path / "data/runs/2026-06-17/daily_run_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["premarket"]["ok"] == 1
    assert status["trading_plan"]["active"] == 1
    assert status["futu_plan_check"]["checked"] == 1
    assert (tmp_path / "reports/daily_runs/2026-06-17.md").exists()


def test_daily_runner_deadline_uses_requested_run_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 16, 22, 0, tzinfo=tz)

    monkeypatch.setattr(daily_premarket, "datetime", FixedDatetime)
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    premarket = FakePremarket()
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["deadline_at"] == "2026-06-17T21:10:00+08:00"
    assert premarket.calls[0]["deadline_reached"]() is False


def test_daily_runner_defers_latest_promotion_until_final_success(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    premarket = FakePremarket()
    plan_builder = FakePlanBuilder()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    assert premarket.calls[0]["update_latest"] is False
    assert plan_builder.calls[0]["update_latest"] is False
    assert (tmp_path / "data/latest/trading_advice.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/trading_advice.csv").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/premarket_actions.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/premarket_actions.csv").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/trading_plan.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/trading_plan.csv").read_text(
        encoding="utf-8"
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["artifacts"]["latest_advice"] == str(
        tmp_path / "data/latest/trading_advice.csv"
    )
    assert status["artifacts"]["latest_actions"] == str(
        tmp_path / "data/latest/premarket_actions.csv"
    )
    assert status["artifacts"]["latest_trading_plan"] == str(
        tmp_path / "data/latest/trading_plan.csv"
    )


def test_daily_runner_does_not_promote_latest_when_plan_build_fails(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FailingPlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )


def test_daily_runner_rolls_back_latest_set_when_grouped_promotion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        dry_run=False,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")

    def fail_on_actions_replace(source_path: Path, latest_path: Path) -> None:
        if latest_path.name == "premarket_actions.csv":
            raise RuntimeError("replace failed")
        source_path.replace(latest_path)

    monkeypatch.setattr(
        daily_premarket,
        "_replace_latest_path",
        fail_on_actions_replace,
        raising=False,
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "old plan\n"
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "replace failed" in status["error"]


def test_daily_runner_does_not_promote_latest_when_report_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        dry_run=False,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    original_write_text = daily_premarket._write_text
    raised = False

    def fail_first_success_report_write(path: Path, text: str) -> None:
        nonlocal raised
        if (
            not raised
            and path == tmp_path / "reports/daily_runs/2026-06-17.md"
            and "- Status: success" in text
        ):
            raised = True
            raise RuntimeError("status write failed")
        original_write_text(path, text)

    monkeypatch.setattr(
        daily_premarket,
        "_write_text",
        fail_first_success_report_write,
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "old plan\n"
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "status write failed" in status["error"]


def test_daily_runner_returns_failed_when_failure_reporting_writes_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        dry_run=False,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")

    def always_fail_write(path: Path, text: str) -> None:
        raise RuntimeError(f"write failed: {path.name}")

    monkeypatch.setattr(daily_premarket, "_write_text", always_fail_write)

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "old plan\n"
    )


def test_daily_runner_does_not_promote_latest_in_dry_run(tmp_path: Path) -> None:
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
        dry_run=True,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    premarket = FakePremarket()
    plan_builder = FakePlanBuilder()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    assert premarket.calls[0]["update_latest"] is False
    assert plan_builder.calls[0]["update_latest"] is False
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "old plan\n"
    )


def test_daily_runner_dry_run_argument_overrides_config(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    latest_dir = tmp_path / "data/latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", dry_run=True)

    assert result.status == "success"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "old plan\n"
    )


def test_daily_runner_lock_contention_does_not_overwrite_run_artifacts(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    status_path = tmp_path / "data/runs/2026-06-17/daily_run_status.json"
    report_path = tmp_path / "reports/daily_runs/2026-06-17.md"
    log_path = tmp_path / "logs/daily_premarket/2026-06-17.log"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text('{"status": "active"}\n', encoding="utf-8")
    report_path.write_text("# active run\n", encoding="utf-8")
    log_path.write_text('{"status": "active"}\n', encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    with RunLock(config.data_dir / "runs" / ".daily_premarket.lock"):
        result = runner.run("2026-06-17")

    assert result.status == "already_running"
    assert status_path.read_text(encoding="utf-8") == '{"status": "active"}\n'
    assert report_path.read_text(encoding="utf-8") == "# active run\n"
    assert log_path.read_text(encoding="utf-8") == '{"status": "active"}\n'
    assert result.log_path == tmp_path / "logs/daily_premarket/2026-06-17.lock.log"


def test_daily_runner_returns_already_running_when_lock_log_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        dry_run=False,
    )

    def fail_lock_log_write(path: Path, text: str) -> None:
        if path == tmp_path / "logs/daily_premarket/2026-06-17.lock.log":
            raise RuntimeError("lock log write failed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    monkeypatch.setattr(daily_premarket, "_write_text", fail_lock_log_write)
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    with RunLock(config.data_dir / "runs" / ".daily_premarket.lock"):
        result = runner.run("2026-06-17")

    assert result.status == "already_running"


class UnavailableQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        raise FutuQuoteError("Futu OpenD is not reachable")


def test_daily_runner_marks_partial_when_futu_is_unavailable(tmp_path: Path) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["futu_plan_check"]["error"] == "Futu OpenD is not reachable"


class MissingQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        assert futu_symbols == ["US.MSFT"]
        return {}

    def close(self) -> None:
        pass


def test_daily_runner_marks_partial_when_futu_quote_is_missing(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["status"] == "partial"
    assert status["futu_plan_check"]["missing"] == 1


def test_daily_runner_ignores_quote_client_close_failure(tmp_path: Path) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=RaisingCloseQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["futu_plan_check"]["checked"] == 1
    assert status["futu_plan_check"]["error"] == ""


def test_daily_runner_writes_failed_status_when_portfolio_is_missing(
    tmp_path: Path,
) -> None:
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
        dry_run=False,
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "portfolio not found" in status["error"]
    assert set(status["premarket"]) == {
        "eligible",
        "advice",
        "actions",
        "ok",
        "fallback",
        "error",
    }
    assert status["premarket"]["eligible"] == 0
    assert status["premarket"]["advice"] == 0
    assert status["premarket"]["actions"] == 0


def test_daily_runner_writes_failed_status_when_deadline_is_malformed(
    tmp_path: Path,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="bad",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        dry_run=False,
    )
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["deadline_at"] == "invalid:bad"


@pytest.mark.parametrize(
    "run_date",
    ["today", "../latest", "2026-06-17/foo", "2026-02-30"],
)
def test_daily_runner_rejects_malformed_run_dates_without_escaped_writes(
    tmp_path: Path,
    run_date: str,
) -> None:
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
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    with pytest.raises(ValueError, match="run_date must be YYYY-MM-DD"):
        runner.run(run_date)

    assert not (tmp_path / "data/latest/daily_run_status.json").exists()
    assert not (tmp_path / "reports/latest.md").exists()


def test_launchd_template_runs_daily_premarket_command() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "ops/launchd/com.open-trader.premarket.plist.template"
    ).read_text(encoding="utf-8")

    assert "com.open-trader.premarket" in template
    assert "run-daily-premarket" in template
    assert "<key>Hour</key>" in template
    assert "<integer>18</integer>" in template
    assert "<key>Minute</key>" in template
    assert "<integer>30</integer>" in template
    assert "OPEN_TRADER_REPO" in template


def test_daily_env_example_has_required_keys_without_real_secrets() -> None:
    example = (
        Path(__file__).resolve().parents[1] / "config/daily_premarket.env.example"
    ).read_text(encoding="utf-8")

    for key in [
        "OPEN_TRADER_REPO",
        "OPEN_TRADER_PYTHON",
        "OPEN_TRADER_TIMEZONE",
        "OPEN_TRADER_DEADLINE",
        "OPEN_TRADER_FUTU_HOST",
        "OPEN_TRADER_FUTU_PORT",
        "DEEPSEEK_API_KEY",
    ]:
        assert key in example
    assert "OPENAI_API_KEY" not in example
    assert "sk-" not in example


def test_launchd_installer_expands_tilde_paths_for_launchd(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                "OPEN_TRADER_REPO=~/projects/open_trader",
                "OPEN_TRADER_PYTHON=~/.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert f"{home}/projects/open_trader" in result.stdout
    assert f"{home}/.venv/bin/python" in result.stdout
    assert "~/projects/open_trader" not in result.stdout
    assert "~/.venv/bin/python" not in result.stdout


def test_launchd_installer_resolves_relative_python_under_repo(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert f"<string>{repo}/.venv/bin/python</string>" in result.stdout
    assert "<string>.venv/bin/python</string>" not in result.stdout


def test_launchd_installer_uses_last_duplicate_env_values_like_runtime_parser(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    first_repo = tmp_path / "first_repo"
    second_repo = tmp_path / "second_repo"
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={first_repo}",
                f"OPEN_TRADER_PYTHON={first_repo / '.venv/bin/python'}",
                f"OPEN_TRADER_REPO={second_repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert str(first_repo) not in result.stdout
    assert f"<string>{second_repo}</string>" in result.stdout
    assert f"<string>{second_repo}/.venv/bin/python</string>" in result.stdout


def test_launchd_installer_rejects_export_syntax_like_runtime_parser(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"export OPEN_TRADER_REPO={repo}",
                f"OPEN_TRADER_PYTHON={repo / '.venv/bin/python'}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 1
    assert "OPEN_TRADER_REPO and OPEN_TRADER_PYTHON are required" in result.stderr


def test_launchd_installer_preserves_inline_comment_text_like_runtime_parser(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo} # literal suffix",
                f"OPEN_TRADER_PYTHON={repo / '.venv/bin/python'}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert f"{repo} # literal suffix" in result.stdout


def _copy_launchd_installer_assets(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(
        source_root / "ops/launchd/com.open-trader.premarket.plist.template",
        repo / "ops/launchd/com.open-trader.premarket.plist.template",
    )
    shutil.copy2(
        source_root / "scripts/install_daily_premarket_launchd.sh",
        repo / "scripts/install_daily_premarket_launchd.sh",
    )
    return repo
