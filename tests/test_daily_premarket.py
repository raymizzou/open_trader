from __future__ import annotations

import csv
import json
import plistlib
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
    build_notifier,
    load_env_config,
)
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
)
from open_trader.trade_actions import TradeActionsResult
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
                "OPEN_TRADER_NOTIFIERS=Feishu_App, macos",
                "OPEN_TRADER_FEISHU_APP_ID=cli_test",
                "OPEN_TRADER_FEISHU_APP_SECRET=secret",
                "OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email",
                "OPEN_TRADER_FEISHU_RECEIVE_ID=ray@example.com",
                "OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text",
                "OPEN_TRADER_NOTIFY_DAILY_REPORT=yes",
                "OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=1",
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
    assert config.notifiers == ("feishu_app", "macos")
    assert config.feishu_app_id == "cli_test"
    assert config.feishu_app_secret == "secret"
    assert config.feishu_receive_id_type == "email"
    assert config.feishu_receive_id == "ray@example.com"
    assert config.feishu_message_format == "text"
    assert config.notify_daily_report is True
    assert config.notify_action_triggers is True


def test_load_env_config_rejects_unsupported_feishu_message_format(
    tmp_path: Path,
) -> None:
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
                "OPEN_TRADER_FEISHU_MESSAGE_FORMAT=card",
                "DEEPSEEK_API_KEY=secret",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="OPEN_TRADER_FEISHU_MESSAGE_FORMAT"):
        load_env_config(env)


def test_build_notifier_uses_configured_feishu_and_macos(tmp_path: Path) -> None:
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
        notifiers=("feishu", "macos"),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, CompositeNotifier)
    inner_notifiers = notifier._notifiers
    assert len(inner_notifiers) == 2
    assert isinstance(inner_notifiers[0], FeishuWebhookNotifier)
    assert inner_notifiers[0].webhook_url == config.feishu_webhook_url
    assert inner_notifiers[1].__class__.__name__ == "MacOSNotifier"


def test_build_notifier_uses_configured_feishu_app_and_macos(tmp_path: Path) -> None:
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
        notifiers=("feishu_app", "macos"),
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_receive_id_type="email",
        feishu_receive_id="ray@example.com",
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, CompositeNotifier)
    inner_notifiers = notifier._notifiers
    assert len(inner_notifiers) == 2
    assert isinstance(inner_notifiers[0], FeishuAppNotifier)
    assert inner_notifiers[0].app_id == config.feishu_app_id
    assert inner_notifiers[0].receive_id_type == config.feishu_receive_id_type
    assert inner_notifiers[0].receive_id == config.feishu_receive_id
    assert inner_notifiers[1].__class__.__name__ == "MacOSNotifier"


def test_build_notifier_returns_null_when_none_configured(tmp_path: Path) -> None:
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
    )

    assert isinstance(build_notifier(config), NullNotifier)


def test_build_notifier_rejects_unknown_notifier(tmp_path: Path) -> None:
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
        notifiers=("email",),
    )

    with pytest.raises(ValueError, match="unknown notifier: email"):
        build_notifier(config)


def test_build_notifier_requires_feishu_webhook(tmp_path: Path) -> None:
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
        notifiers=("feishu",),
    )

    with pytest.raises(ValueError, match="OPEN_TRADER_FEISHU_WEBHOOK_URL is required"):
        build_notifier(config)


def test_build_notifier_requires_feishu_app_config(tmp_path: Path) -> None:
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
        notifiers=("feishu_app",),
        feishu_app_id="cli_test",
        feishu_app_secret="",
        feishu_receive_id_type="email",
        feishu_receive_id="ray@example.com",
    )

    with pytest.raises(ValueError, match="OPEN_TRADER_FEISHU_APP_SECRET is required"):
        build_notifier(config)


def test_daily_runner_defaults_to_generate_trade_actions(tmp_path: Path) -> None:
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
    )

    runner = DailyPremarketRunner(config=config)

    assert runner.trade_action_generator is daily_premarket.generate_trade_actions


def test_snapshots_from_futu_status_ignores_nonfinite_last_prices() -> None:
    snapshots = daily_premarket._snapshots_from_futu_status(
        {
            "items": [
                {"futu_symbol": "US.MSFT", "last_price": "399"},
                {"futu_symbol": "US.NAN", "last_price": "NaN"},
                {"futu_symbol": "US.INF", "last_price": "Infinity"},
                {"futu_symbol": "US.NEGINF", "last_price": "-Infinity"},
            ]
        }
    )

    assert snapshots == {
        "US.MSFT": QuoteSnapshot(
            futu_symbol="US.MSFT",
            last_price=Decimal("399"),
        )
    }


def test_derive_daily_state_marks_futu_error_as_blocked() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 0,
            "missing": 0,
            "triggered": 0,
            "items": [],
            "error": "网络中断",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 0, "watch": 1},
    )

    assert state == {
        "status": "partial",
        "readiness": "blocked",
        "status_reasons": ["futu_error"],
    }


def test_derive_daily_state_marks_missing_quote_as_review_required() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 1,
            "triggered": 0,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 1, "watch": 0},
    )

    assert state == {
        "status": "partial",
        "readiness": "review_required",
        "status_reasons": ["missing_quotes", "trade_action_review"],
    }


def test_derive_daily_state_keeps_trade_action_review_as_success_status() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 0,
            "triggered": 1,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 0, "review": 1, "watch": 0},
    )

    assert state == {
        "status": "success",
        "readiness": "review_required",
        "status_reasons": ["trade_action_review"],
    }


def test_derive_daily_state_marks_success_as_ready() -> None:
    state = daily_premarket._derive_daily_state(
        advice_counts={"ok": 1, "fallback": 0, "error": 0},
        plan_counts={"active": 1, "fallback": 0, "error": 0},
        futu_status={
            "checked": 1,
            "missing": 0,
            "triggered": 0,
            "items": [],
            "error": "",
        },
        trade_actions={"actions": 1, "ready": 1, "review": 0, "watch": 0},
    )

    assert state == {
        "status": "success",
        "readiness": "ready",
        "status_reasons": [],
    }


def test_readiness_label_uses_chinese_fallback_for_unknown_values() -> None:
    assert daily_premarket._readiness_label("") == "未分类"
    assert daily_premarket._readiness_label("unexpected") == "未分类"


def test_status_reason_label_uses_chinese_fallback_for_unknown_values() -> None:
    assert daily_premarket._status_reason_label("unexpected_reason") == "其他原因"


def test_daily_status_label_uses_chinese_fallback_for_unknown_values() -> None:
    assert daily_premarket._daily_status_label("unexpected_status") == "未知状态"


def test_blocker_notification_unexpected_status_uses_chinese_fallbacks() -> None:
    body = daily_premarket._blocker_notification_message(
        run_date="2026-06-17",
        status="unexpected_status",
        futu_status={"error": "", "missing": 0, "diagnostic": {}},
        trade_actions={"review": 0},
        artifacts={},
        readiness="",
        status_reasons=["unexpected_reason"],
    )

    assert "状态：未知状态" in body
    assert "可用性：未分类" in body
    assert "原因：其他原因" in body
    assert "unexpected_status" not in body
    assert "unexpected_reason" not in body


def test_blocker_notification_does_not_expose_raw_error_text() -> None:
    body = daily_premarket._blocker_notification_message(
        run_date="2026-06-17",
        status="partial",
        futu_status={
            "error": "Futu OpenD is not reachable",
            "missing": 0,
            "diagnostic": {
                "error_type": "opend_unreachable",
                "next_step": "请启动或重启 Futu OpenD，确认已登录，并检查配置的 host/port 后重新运行每日盘前流程。",
            },
        },
        trade_actions={"review": 0},
        artifacts={},
        error="portfolio not found",
        readiness="blocked",
        status_reasons=["run_failed", "futu_error"],
    )

    assert "运行失败：每日流程未完成。" in body
    assert "Futu 行情异常：行情检查未完成。" in body
    assert "portfolio not found" not in body
    assert "Futu OpenD is not reachable" not in body


def test_render_daily_report_legacy_payload_uses_blocker_next_step() -> None:
    report = daily_premarket._render_daily_report(
        {
            "run_date": "2026-06-17",
            "started_at": "2026-06-17T21:00:00+08:00",
            "finished_at": "2026-06-17T21:01:00+08:00",
            "deadline_at": "2026-06-17T21:10:00+08:00",
            "status": "partial",
            "premarket": {"ok": 1, "fallback": 0, "error": 0},
            "trading_plan": {"active": 1, "fallback": 0, "error": 0},
            "futu_plan_check": {
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": "网络中断",
            },
            "trade_actions": {"actions": 0, "ready": 0, "review": 0, "watch": 0},
            "artifacts": {},
        }
    )

    assert "- 下一步：无需处理。" not in report
    assert "- 下一步：请启动或重启 Futu OpenD，确认行情连接恢复后重新运行每日盘前流程。" in report


def test_blocker_notification_readiness_label_without_readiness_uses_chinese_fallback() -> None:
    body = daily_premarket._blocker_notification_message(
        run_date="2026-06-17",
        status="partial",
        futu_status={"error": "", "missing": 0},
        trade_actions={"review": 0},
        artifacts={},
    )

    assert "可用性：\n" not in body
    assert "可用性：未分类" in body
    assert "unexpected_reason" not in body


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
    def __init__(self, *, market: str = "US", symbol: str = "MSFT") -> None:
        self.market = market
        self.symbol = symbol
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object):
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        if kwargs.get("market"):
            run_dir = data_dir / "runs" / run_date / self.market
        else:
            run_dir = data_dir / "runs" / run_date
        advice_path = run_dir / "trading_advice.csv"
        actions_path = run_dir / "premarket_actions.csv"
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
                    "symbol": self.symbol,
                    "market": self.market,
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
            f"{run_date},{self.symbol},{self.market},ok\n",
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
    def __init__(self, *, market: str = "US", symbol: str = "MSFT") -> None:
        self.market = market
        self.symbol = symbol
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
        market: str | None = None,
    ) -> TradingPlanBuildResult:
        self.calls.append(
            {
                "advice_path": advice_path,
                "data_dir": data_dir,
                "run_date": run_date,
                "update_latest": update_latest,
                "market": market,
            }
        )
        assert advice_path.exists()
        if market:
            plan_path = data_dir / "runs" / run_date / self.market / "trading_plan.csv"
            latest_path = data_dir / "latest" / self.market / "trading_plan.csv"
        else:
            plan_path = data_dir / "runs" / run_date / "trading_plan.csv"
            latest_path = data_dir / "latest" / "trading_plan.csv"
        row = {
            "run_date": run_date,
            "symbol": self.symbol,
            "market": self.market,
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
    def __init__(
        self,
        snapshots: dict[str, QuoteSnapshot] | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 11111,
    ) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.snapshots = snapshots or {
            "US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("399"))
        }

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        assert futu_symbols == list(self.snapshots)
        return self.snapshots

    def close(self) -> None:
        self.closed = True


class FakeTradeActionGenerator:
    def __init__(self, *, market: str = "US", symbol: str = "MSFT") -> None:
        self.market = market
        self.symbol = symbol
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> TradeActionsResult:
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        reports_dir = kwargs["reports_dir"]
        run_date = kwargs["run_date"]
        assert isinstance(data_dir, Path)
        assert isinstance(reports_dir, Path)
        assert isinstance(run_date, str)
        if kwargs.get("market"):
            actions_path = data_dir / "runs" / run_date / self.market / "trade_actions.csv"
            latest_path = data_dir / "latest" / self.market / "trade_actions.csv"
            report_path = reports_dir / "trade_actions" / f"{run_date}-{self.market}.md"
        else:
            actions_path = data_dir / "runs" / run_date / "trade_actions.csv"
            latest_path = data_dir / "latest" / "trade_actions.csv"
            report_path = reports_dir / "trade_actions" / f"{run_date}.md"
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        actions_path.write_text(
            "run_date,symbol,market,futu_symbol,action,priority,last_price,"
            "trigger_status,suggested_quantity,suggested_notional,"
            "notional_currency,current_quantity,current_weight,avg_cost_price,"
            "target_max_weight,cash_available,limit_price,stop_price,"
            "post_trade_quantity,post_trade_weight,post_trade_avg_cost,"
            "risk_to_stop,reason,source_plan,status,error\n"
            f"{run_date},{self.symbol},{self.market},{self.market}.{self.symbol},BUY,high,399,entry_zone,3,1197,"
            "USD,10,1.13%,390,2%,1000,399,340,13,1.40%,392.08,767,"
            f"fixture,data/runs/{run_date}/trading_plan.csv,ready,\n",
            encoding="utf-8",
        )
        report_path.write_text("# Trade Actions\n", encoding="utf-8")
        return TradeActionsResult(
            run_date=run_date,
            action_count=1,
            ready_count=1,
            review_count=0,
            watch_count=0,
            actions_path=actions_path,
            latest_path=latest_path,
            report_path=report_path,
        )


class FailingPlanBuilder:
    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
        market: str | None = None,
    ) -> TradingPlanBuildResult:
        raise RuntimeError("plan builder failed")


class RaisingCloseQuoteClient(FakeQuoteClient):
    def close(self) -> None:
        raise RuntimeError("close failed")


class CapturingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))


class FailingNotifier:
    def notify(self, title: str, message: str) -> None:
        raise RuntimeError("delivery failed")


def _daily_config(tmp_path: Path) -> DailyPremarketConfig:
    return DailyPremarketConfig(
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
    )


def test_daily_config_deadline_for_market_uses_hk_and_us_defaults(
    tmp_path: Path,
) -> None:
    config = _daily_config(tmp_path)

    assert daily_premarket._deadline_for_market(config, "HK") == "09:00"
    assert daily_premarket._deadline_for_market(config, "US") == "21:10"


def test_daily_runner_hk_uses_market_scoped_paths_and_calls_market_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("portfolio\n", encoding="utf-8")
    premarket = FakePremarket(market="HK", symbol="00700")
    plan_builder = FakePlanBuilder(market="HK", symbol="00700")
    trade_actions = FakeTradeActionGenerator(market="HK", symbol="00700")

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))},
            **kwargs,
        ),
        trade_action_generator=trade_actions,
    ).run(run_date="2026-06-19", market="HK")

    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert result.status_path == (
        tmp_path / "data/runs/2026-06-19/HK/daily_run_status.json"
    )
    assert result.report_path == tmp_path / "reports/daily_runs/2026-06-19-HK.md"
    assert result.log_path == tmp_path / "logs/daily_premarket/2026-06-19-HK.log"
    assert status["market"] == "HK"
    assert status["deadline_at"].endswith("09:00:00+08:00")
    assert premarket.calls[0]["market"] == "HK"
    assert plan_builder.calls[0]["market"] == "HK"
    assert trade_actions.calls[0]["market"] == "HK"
    assert status["artifacts"]["status"] == str(result.status_path)
    assert status["artifacts"]["report"] == str(result.report_path)
    assert status["artifacts"]["log"] == str(result.log_path)
    assert status["artifacts"]["latest_trading_plan"] == str(
        tmp_path / "data/latest/HK/trading_plan.csv"
    )
    assert "data/latest/trading_plan.csv" not in status["artifacts"].values()


def test_daily_notify_logs_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = CapturingNotifier()
    runner = DailyPremarketRunner(
        config=_daily_config(tmp_path),
        notifier=notifier,
    )

    with caplog.at_level("INFO", logger="open_trader.daily_premarket"):
        runner._notify("Open Trader 行动通知", "测试正文")

    assert notifier.calls == [("Open Trader 行动通知", "测试正文")]
    assert "通知已发送：Open Trader 行动通知" in caplog.text
    notification_logs = list((tmp_path / "logs/notifications").glob("*.jsonl"))
    assert len(notification_logs) == 1
    payload = json.loads(notification_logs[0].read_text(encoding="utf-8"))
    assert payload["title"] == "Open Trader 行动通知"
    assert payload["channel"] == "CapturingNotifier"
    assert payload["success"] is True


def test_daily_notify_logs_failure_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = DailyPremarketRunner(
        config=_daily_config(tmp_path),
        notifier=FailingNotifier(),
    )

    with caplog.at_level("WARNING", logger="open_trader.daily_premarket"):
        runner._notify("Open Trader 行动通知", "测试正文")

    assert "通知发送失败：Open Trader 行动通知" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "delivery failed" in caplog.text
    notification_logs = list((tmp_path / "logs/notifications").glob("*.jsonl"))
    assert len(notification_logs) == 1
    payload = json.loads(notification_logs[0].read_text(encoding="utf-8"))
    assert payload["title"] == "Open Trader 行动通知"
    assert payload["channel"] == "FailingNotifier"
    assert payload["success"] is False
    assert payload["error_type"] == "RuntimeError"
    assert payload["error"] == "delivery failed"


def test_daily_notify_logs_composite_child_failure_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class WorkingNotifier:
        def notify(self, title: str, message: str) -> None:
            pass

    runner = DailyPremarketRunner(
        config=_daily_config(tmp_path),
        notifier=CompositeNotifier([FailingNotifier(), WorkingNotifier()]),
    )

    with caplog.at_level("INFO", logger="open_trader.daily_premarket"):
        runner._notify("Open Trader 行动通知", "测试正文")

    assert "通知发送失败：Open Trader 行动通知" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "delivery failed" in caplog.text
    assert "通知已发送：Open Trader 行动通知" in caplog.text


def test_daily_runner_writes_success_status_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
    fake_trade_actions = FakeTradeActionGenerator()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=fake_trade_actions,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "success"
    status_path = tmp_path / "data/runs/2026-06-17/US/daily_run_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["premarket"]["ok"] == 1
    assert status["trading_plan"]["active"] == 1
    assert status["futu_plan_check"]["checked"] == 1
    assert fake_trade_actions.calls
    assert fake_trade_actions.calls[0]["plan_path"] == (
        tmp_path / "data/runs/2026-06-17/US/trading_plan.csv"
    )
    assert fake_trade_actions.calls[0]["portfolio_path"] == (
        tmp_path / "data/latest/portfolio.csv"
    )
    assert fake_trade_actions.calls[0]["data_dir"] == tmp_path / "data"
    assert fake_trade_actions.calls[0]["reports_dir"] == tmp_path / "reports"
    assert fake_trade_actions.calls[0]["run_date"] == "2026-06-17"
    assert fake_trade_actions.calls[0]["update_latest"] is False
    assert fake_trade_actions.calls[0]["snapshots"] == {
        "US.MSFT": QuoteSnapshot(
            futu_symbol="US.MSFT",
            last_price=Decimal("399"),
        )
    }
    assert status["trade_actions"] == {
        "actions": 1,
        "ready": 1,
        "review": 0,
        "watch": 0,
    }
    assert status["artifacts"]["trade_actions"] == str(
        tmp_path / "data/runs/2026-06-17/US/trade_actions.csv"
    )
    assert status["artifacts"]["trade_actions_report"] == str(
        tmp_path / "reports/trade_actions/2026-06-17-US.md"
    )
    assert status["artifacts"]["latest_trade_actions"] == str(
        tmp_path / "data/latest/US/trade_actions.csv"
    )
    report = (tmp_path / "reports/daily_runs/2026-06-17-US.md").read_text(
        encoding="utf-8"
    )
    assert "- trade_actions: " in report
    assert "- trade_actions_report: " in report
    assert (tmp_path / "reports/daily_runs/2026-06-17-US.md").exists()


def test_daily_runner_sends_feishu_order_review_after_trade_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "success"
    assert len(notifier.calls) == 1
    title, body = notifier.calls[0]
    assert title == "Open Trader 行动通知"
    assert "Open Trader｜行动通知" in body
    assert "今日结论：有 1 条可采取行动，需人工确认后执行。" in body
    assert "标的：MSFT｜指示：买入 3 股｜优先级：高" in body
    assert "影响：" in body
    assert "reports/" not in body


def test_daily_runner_sends_blocker_notification_when_futu_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 阻塞通知"
    ]
    assert len(blocker_calls) == 1
    _, body = blocker_calls[0]
    assert "Open Trader｜阻塞通知" in body
    assert "日期：2026-06-17｜状态：部分完成" in body
    assert "Futu 行情异常：行情检查未完成。" in body
    assert "Futu OpenD is not reachable" not in body
    assert "原因：Futu 行情异常" in body
    assert "请启动或重启 Futu OpenD" in body


def test_daily_runner_sends_blocker_notification_when_futu_quote_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 阻塞通知"
    ]
    assert len(blocker_calls) == 1
    _, body = blocker_calls[0]
    assert "可用性：需要人工复核" in body
    assert "原因：缺失行情" in body
    assert "缺失行情：1" in body
    assert "报告：" in body


def test_daily_runner_blocker_notification_uses_chinese_readiness_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=InterruptedQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 阻塞通知"
    ]
    assert len(blocker_calls) == 1
    _, body = blocker_calls[0]
    assert "可用性：阻塞" in body
    assert "原因：Futu 行情异常" in body
    assert "下一步：请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。" in body
    assert "futu_error" not in body
    assert "quote_server_interrupted" not in body


def test_daily_runner_sends_blocker_notification_when_run_fails(
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "failed"
    assert len(notifier.calls) == 1
    title, body = notifier.calls[0]
    assert title == "Open Trader 阻塞通知"
    assert "运行失败：每日流程未完成。" in body
    assert "portfolio not found" not in body
    assert "状态文件：" in body


def test_daily_runner_keeps_partial_status_when_blocker_rendering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_blocker_render_error(**kwargs: object) -> str:
        raise RuntimeError("blocker render failed")

    monkeypatch.setattr(
        daily_premarket,
        "_blocker_notification_message",
        raise_blocker_render_error,
    )
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "partial"
    assert "error" not in status
    assert [title for title, _ in notifier.calls] == ["Open Trader 行动通知"]


@pytest.mark.parametrize(
    "expected_status",
    [
        "success",
        "partial",
    ],
)
def test_daily_runner_keeps_success_status_when_order_review_rendering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_status: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    def raise_render_error(**kwargs: object) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(
        daily_premarket,
        "render_feishu_order_review",
        raise_render_error,
    )
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()
    quote_client_factory = (
        FakeQuoteClient if expected_status == "success" else MissingQuoteClient
    )

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=quote_client_factory,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == expected_status
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == expected_status
    assert "error" not in status
    report = result.report_path.read_text(encoding="utf-8")
    assert f"- Status: {expected_status}" in report
    assert "- Status: failed" not in report
    assert (tmp_path / "data/latest/US/trade_actions.csv").exists()
    if expected_status == "success":
        assert notifier.calls == []
    else:
        assert len(notifier.calls) == 1
        title, body = notifier.calls[0]
        assert title == "Open Trader 阻塞通知"
        assert "缺失行情：1" in body


def test_daily_runner_skips_daily_notification_when_report_notify_disabled(
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "success"
    assert notifier.calls == []


def test_daily_runner_skips_daily_notification_in_dry_run(tmp_path: Path) -> None:
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
        notifiers=("feishu",),
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        notify_daily_report=True,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "success"
    assert notifier.calls == []


def test_daily_runner_skips_partial_notification_when_env_report_notify_zero(
    tmp_path: Path,
) -> None:
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
                "OPEN_TRADER_NOTIFIERS=feishu",
                "OPEN_TRADER_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/test",
                "OPEN_TRADER_NOTIFY_DAILY_REPORT=0",
                "DEEPSEEK_API_KEY=secret",
            ]
        ),
        encoding="utf-8",
    )
    config = load_env_config(env)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    notifier = CapturingNotifier()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "partial"
    assert config.notify_daily_report is False
    assert notifier.calls == []


def test_daily_runner_skips_failure_notification_when_report_notify_disabled(
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
        notify_daily_report=False,
    )
    notifier = CapturingNotifier()
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=notifier,
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "failed"
    assert notifier.calls == []


def test_daily_runner_skips_failure_notification_in_dry_run(tmp_path: Path) -> None:
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
        notify_daily_report=True,
    )
    notifier = CapturingNotifier()
    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=notifier,
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "failed"
    assert notifier.calls == []


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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["deadline_at"] == "2026-06-17T21:10:00+08:00"
    assert premarket.calls[0]["deadline_reached"]() is False


def test_daily_runner_defers_latest_promotion_until_final_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
    fake_trade_actions = FakeTradeActionGenerator()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=fake_trade_actions,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "success"
    assert premarket.calls[0]["update_latest"] is False
    assert plan_builder.calls[0]["update_latest"] is False
    assert fake_trade_actions.calls[0]["update_latest"] is False
    assert (tmp_path / "data/latest/US/trading_advice.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/trading_advice.csv").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/US/premarket_actions.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/premarket_actions.csv").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/US/trading_plan.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/trading_plan.csv").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/US/trade_actions.csv").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/trade_actions.csv").read_text(
        encoding="utf-8"
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["artifacts"]["latest_advice"] == str(
        tmp_path / "data/latest/US/trading_advice.csv"
    )
    assert status["artifacts"]["latest_actions"] == str(
        tmp_path / "data/latest/US/premarket_actions.csv"
    )
    assert status["artifacts"]["latest_trading_plan"] == str(
        tmp_path / "data/latest/US/trading_plan.csv"
    )
    assert status["artifacts"]["latest_trade_actions"] == str(
        tmp_path / "data/latest/US/trade_actions.csv"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (hk_latest_dir / "trading_advice.csv").write_text(
        "hk advice\n",
        encoding="utf-8",
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FailingPlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "failed"
    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "old advice\n"
    )
    assert (latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "old actions\n"
    )
    assert (hk_latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "hk advice\n"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (hk_latest_dir / "trading_plan.csv").write_text("hk plan\n", encoding="utf-8")

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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

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
    assert (hk_latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "hk plan\n"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (hk_latest_dir / "premarket_actions.csv").write_text(
        "hk actions\n",
        encoding="utf-8",
    )
    original_write_text = daily_premarket._write_text
    raised = False

    def fail_first_success_report_write(path: Path, text: str) -> None:
        nonlocal raised
        if (
            not raised
            and path == tmp_path / "reports/daily_runs/2026-06-17-US.md"
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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

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
    assert (hk_latest_dir / "premarket_actions.csv").read_text(encoding="utf-8") == (
        "hk actions\n"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (hk_latest_dir / "trading_plan.csv").write_text("hk plan\n", encoding="utf-8")

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

    result = runner.run("2026-06-17", market="US")

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
    assert (hk_latest_dir / "trading_plan.csv").read_text(encoding="utf-8") == (
        "hk plan\n"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (hk_latest_dir / "trading_advice.csv").write_text(
        "hk advice\n",
        encoding="utf-8",
    )
    premarket = FakePremarket()
    plan_builder = FakePlanBuilder()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

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
    assert (hk_latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "hk advice\n"
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
    latest_dir = tmp_path / "data/latest/US"
    hk_latest_dir = tmp_path / "data/latest/HK"
    latest_dir.mkdir(parents=True, exist_ok=True)
    hk_latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (hk_latest_dir / "trading_advice.csv").write_text(
        "hk advice\n",
        encoding="utf-8",
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US", dry_run=True)

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
    assert (hk_latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "hk advice\n"
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
    status_path = tmp_path / "data/runs/2026-06-17/US/daily_run_status.json"
    report_path = tmp_path / "reports/daily_runs/2026-06-17-US.md"
    log_path = tmp_path / "logs/daily_premarket/2026-06-17-US.log"
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

    with RunLock(config.data_dir / "runs" / ".daily_premarket.US.lock"):
        result = runner.run("2026-06-17", market="US")

    assert result.status == "already_running"
    assert status_path.read_text(encoding="utf-8") == '{"status": "active"}\n'
    assert report_path.read_text(encoding="utf-8") == "# active run\n"
    assert log_path.read_text(encoding="utf-8") == '{"status": "active"}\n'
    assert result.log_path == tmp_path / "logs/daily_premarket/2026-06-17-US.lock.log"
    lock_status = json.loads(result.log_path.read_text(encoding="utf-8"))
    assert lock_status["status"] == "already_running"
    assert lock_status["readiness"] == "blocked"
    assert lock_status["status_reasons"] == ["already_running"]


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
        if path == tmp_path / "logs/daily_premarket/2026-06-17-US.lock.log":
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

    with RunLock(config.data_dir / "runs" / ".daily_premarket.US.lock"):
        result = runner.run("2026-06-17", market="US")

    assert result.status == "already_running"


class UnavailableQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        raise FutuQuoteError(
            "Futu OpenD is not reachable",
            error_type="opend_unreachable",
            next_step="请启动或重启 Futu OpenD，确认已登录，并检查配置的 host/port 后重新运行每日盘前流程。",
            opend_reachable=False,
            context_ok=False,
            snapshot_ok=False,
        )


class InterruptedQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        raise FutuQuoteError(
            "网络中断",
            error_type="quote_server_interrupted",
            next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )

    def close(self) -> None:
        pass


def test_daily_runner_marks_partial_when_futu_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/US/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["futu_plan_check"]["error"] == "Futu OpenD is not reachable"


def test_daily_runner_writes_futu_diagnostic_when_opend_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "blocked"
    assert status["status_reasons"] == ["futu_error"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["host"] == "127.0.0.1"
    assert diagnostic["port"] == 11111
    assert diagnostic["error_type"] == "opend_unreachable"
    assert diagnostic["opend_reachable"] is False
    assert diagnostic["context_ok"] is False
    assert diagnostic["snapshot_ok"] is False
    assert "请启动或重启 Futu OpenD" in diagnostic["next_step"]


def test_daily_runner_writes_futu_diagnostic_when_snapshot_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=InterruptedQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "blocked"
    assert status["status_reasons"] == ["futu_error"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["error_type"] == "quote_server_interrupted"
    assert diagnostic["opend_reachable"] is True
    assert diagnostic["context_ok"] is True
    assert diagnostic["snapshot_ok"] is False
    assert diagnostic["next_step"] == "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
    report = result.report_path.read_text(encoding="utf-8")
    assert "## 可用性判断" in report
    assert "- 可用性：阻塞" in report
    assert "- 原因：Futu 行情异常" in report
    assert "- 下一步：请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。" in report


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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/US/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["status"] == "partial"
    assert status["futu_plan_check"]["missing"] == 1


def test_daily_runner_marks_missing_quote_as_review_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
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

    result = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "review_required"
    assert "missing_quotes" in status["status_reasons"]
    diagnostic = status["futu_plan_check"]["diagnostic"]
    assert diagnostic["error_type"] == "missing_quotes"
    assert diagnostic["snapshot_ok"] is True
    assert "缺失 1 个标的行情" in diagnostic["next_step"]


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
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

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

    result = runner.run("2026-06-17", market="US")

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

    result = runner.run("2026-06-17", market="US")

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
        runner.run(run_date, market="US")

    assert not (tmp_path / "data/latest/daily_run_status.json").exists()
    assert not (tmp_path / "reports/latest.md").exists()


def test_launchd_template_runs_daily_premarket_command() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "ops/launchd/com.open-trader.premarket.plist.template"
    ).read_text(encoding="utf-8")

    assert "OPEN_TRADER_LABEL" in template
    assert "run-daily-premarket" in template
    assert "<string>--market</string>" in template
    assert "<string>OPEN_TRADER_MARKET</string>" in template
    assert "<string>--date</string>" in template
    assert "<string>today</string>" in template
    assert "<key>Hour</key>" in template
    assert "<integer>OPEN_TRADER_HOUR</integer>" in template
    assert "<key>Minute</key>" in template
    assert "<integer>OPEN_TRADER_MINUTE</integer>" in template
    assert "launchd-OPEN_TRADER_MARKET.out.log" in template
    assert "launchd-OPEN_TRADER_MARKET.err.log" in template
    assert "OPEN_TRADER_REPO" in template


def test_launchd_installer_default_renders_hk_and_us_jobs(
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

    plists = _launchd_plists(result.stdout)
    by_label = {payload["Label"]: payload for payload in plists}
    assert set(by_label) == {
        "com.open-trader.premarket.hk",
        "com.open-trader.premarket.us",
    }
    _assert_launchd_job(
        by_label["com.open-trader.premarket.hk"],
        repo=repo,
        market="HK",
        hour=8,
        minute=0,
    )
    _assert_launchd_job(
        by_label["com.open-trader.premarket.us"],
        repo=repo,
        market="US",
        hour=18,
        minute=30,
    )


@pytest.mark.parametrize(
    ("market", "label", "hour", "minute"),
    [
        ("HK", "com.open-trader.premarket.hk", 8, 0),
        ("US", "com.open-trader.premarket.us", 18, 30),
    ],
)
def test_launchd_installer_renders_single_market_job(
    market: str,
    label: str,
    hour: int,
    minute: int,
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
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--market",
            market,
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    plists = _launchd_plists(result.stdout)
    assert len(plists) == 1
    assert plists[0]["Label"] == label
    _assert_launchd_job(
        plists[0],
        repo=repo,
        market=market,
        hour=hour,
        minute=minute,
    )


def test_launchd_installer_rejects_unsupported_market_argument(
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
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--market",
            "CN",
        ],
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_launchd_installer_removes_legacy_agent_before_installing_split_jobs(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.open-trader.premarket.plist"
    legacy.write_text("legacy\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--market", "all"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert not legacy.exists()
    assert (agents / "com.open-trader.premarket.hk.plist").exists()
    assert (agents / "com.open-trader.premarket.us.plist").exists()


def test_launchd_installer_removes_legacy_agent_for_single_market_install(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.open-trader.premarket.plist"
    legacy.write_text("legacy\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--market",
            "HK",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert not legacy.exists()
    assert (agents / "com.open-trader.premarket.hk.plist").exists()
    assert not (agents / "com.open-trader.premarket.us.plist").exists()


def test_launchd_installer_dry_run_does_not_remove_legacy_agent(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.open-trader.premarket.plist"
    legacy.write_text("legacy\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--market",
            "all",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert legacy.exists()
    assert not (agents / "com.open-trader.premarket.hk.plist").exists()
    assert not (agents / "com.open-trader.premarket.us.plist").exists()


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
        "OPEN_TRADER_NOTIFIERS",
        "OPEN_TRADER_FEISHU_WEBHOOK_URL",
        "OPEN_TRADER_FEISHU_MESSAGE_FORMAT",
        "OPEN_TRADER_NOTIFY_DAILY_REPORT",
        "OPEN_TRADER_NOTIFY_ACTION_TRIGGERS",
        "DEEPSEEK_API_KEY",
    ]:
        assert key in example
    assert "HK daily workflow deadline is fixed by code at 09:00 Asia/Shanghai" in example
    assert "US daily workflow uses OPEN_TRADER_DEADLINE" in example
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


def test_launchd_uninstaller_defaults_to_hk_us_and_legacy_plists(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    hk = agents / "com.open-trader.premarket.hk.plist"
    us = agents / "com.open-trader.premarket.us.plist"
    legacy = agents / "com.open-trader.premarket.plist"
    for path in [hk, us, legacy]:
        path.write_text("plist\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)

    result = subprocess.run(
        [str(repo / "scripts/uninstall_daily_premarket_launchd.sh")],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert not hk.exists()
    assert not us.exists()
    assert not legacy.exists()
    assert "com.open-trader.premarket.hk.plist" in result.stdout
    assert "com.open-trader.premarket.us.plist" in result.stdout


def test_launchd_uninstaller_removes_only_requested_market(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    hk = agents / "com.open-trader.premarket.hk.plist"
    us = agents / "com.open-trader.premarket.us.plist"
    hk.write_text("hk\n", encoding="utf-8")
    us.write_text("us\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)

    subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--market",
            "HK",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert not hk.exists()
    assert us.exists()


def test_launchd_uninstaller_rejects_unsupported_market_argument(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)

    result = subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--market",
            "CN",
        ],
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def _launchd_plists(plist_text: str) -> list[dict[str, object]]:
    documents = [
        f"<?xml{document}"
        for document in plist_text.split("<?xml")
        if document.strip()
    ]
    return [plistlib.loads(document.encode("utf-8")) for document in documents]


def _assert_launchd_job(
    payload: dict[str, object],
    *,
    repo: Path,
    market: str,
    hour: int,
    minute: int,
) -> None:
    args = payload["ProgramArguments"]
    assert isinstance(args, list)
    args = [str(arg) for arg in args]
    market_index = args.index("--market")
    assert args[market_index + 1] == market
    assert args[args.index("--date") + 1] == "today"
    assert args[args.index("--config") + 1] == f"{repo}/config/daily_premarket.env"
    intervals = payload["StartCalendarInterval"]
    assert isinstance(intervals, list)
    assert {item["Weekday"] for item in intervals} == {1, 2, 3, 4, 5}
    assert {item["Hour"] for item in intervals} == {hour}
    assert {item["Minute"] for item in intervals} == {minute}
    assert payload["StandardOutPath"] == (
        f"{repo}/logs/daily_premarket/launchd-{market}.out.log"
    )
    assert payload["StandardErrorPath"] == (
        f"{repo}/logs/daily_premarket/launchd-{market}.err.log"
    )


def _fake_launchctl_bin(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    return fake_bin


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
    shutil.copy2(
        source_root / "scripts/uninstall_daily_premarket_launchd.sh",
        repo / "scripts/uninstall_daily_premarket_launchd.sh",
    )
    return repo
