from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

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
                "OPENAI_API_KEY=secret",
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


def test_load_env_config_rejects_missing_required_values(tmp_path: Path) -> None:
    env = tmp_path / "daily.env"
    env.write_text("OPEN_TRADER_REPO=/tmp/open_trader\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_env_config(env)
    message = str(exc_info.value)
    assert message.startswith("missing config value(s):")
    assert "OPEN_TRADER_PYTHON" in message
    assert "OPENAI_API_KEY" in message


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
                "actions_path": data_dir / "runs" / run_date / "premarket_actions.csv",
                "report_path": Path("reports/premarket") / f"{run_date}.md",
            },
        )()


class FakePlanBuilder:
    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
    ) -> TradingPlanBuildResult:
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
        for path in [plan_path, latest_path]:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as handle:
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
    assert status["premarket"]["eligible"] == 0
    assert status["premarket"]["advice"] == 0
    assert status["premarket"]["actions"] == 0
