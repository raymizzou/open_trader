from __future__ import annotations

import csv
import json
import plistlib
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.daily_premarket as daily_premarket
from open_trader.advice.premarket import PremarketResult
from open_trader.daily_premarket import (
    DailyPremarketConfig,
    DailyPremarketRunner as _DailyPremarketRunner,
    NullNotifier,
    RunLock,
    build_notifier,
    load_env_config,
    send_notification_with_results,
)
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_skill_facts import FutuSkillFactResult
from open_trader.futu_watch import QuoteSnapshot
from open_trader.notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    XiaoaiSSHNotifier,
    XiaoaiVoiceSuppressed,
)
from open_trader.trade_actions import TradeActionsResult
from open_trader.technical_facts import source_hash
from open_trader.decision_facts import extract_decision_sources
from open_trader.decision_source_availability import SourceFailure
from open_trader.tradingagents_summary import TradingAgentsSummaryResult
from open_trader.trading_plan import (
    TRADING_PLAN_FIELDNAMES,
    TradingPlanBuildResult,
)


def test_load_env_config_parses_required_values_and_executor_host(
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
                "OPEN_TRADER_NOTIFIERS=Feishu_App, macos",
                "OPEN_TRADER_FEISHU_APP_ID=cli_test",
                "OPEN_TRADER_FEISHU_APP_SECRET=secret",
                "OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email",
                "OPEN_TRADER_FEISHU_RECEIVE_ID=ray@example.com",
                "OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text",
                "OPEN_TRADER_XIAOAI_HOST=192.168.1.107",
                f"OPEN_TRADER_XIAOAI_SSH_KEY={tmp_path / 'speaker-key'}",
                "OPEN_TRADER_NOTIFY_DAILY_REPORT=yes",
                "OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=1",
                "TREND_ANIMALS_API_KEY=trend-secret",
                "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID=622466",
                "TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID=697199",
                "TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460, 700001",
                "TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494",
                "OPEN_TRADER_TREND_US_SYMBOLS=AAPL, VIXY",
                "OPEN_TRADER_TREND_HK_SYMBOLS=00700, 02800",
                "OPEN_TRADER_TREND_REVIEW_CN_SIMULATE_ACC_ID=101",
                "OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID=102",
                "OPEN_TRADER_TREND_REVIEW_HK_SIMULATE_ACC_ID=103",
                "OPEN_TRADER_TREND_EXECUTOR_HOST=ray-mac",
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
    assert config.max_workers == 8
    assert config.classifier_model == "deepseek-v4-flash"
    assert config.notifiers == ("feishu_app", "macos")
    assert config.feishu_app_id == "cli_test"
    assert config.feishu_app_secret == "secret"
    assert config.feishu_receive_id_type == "email"
    assert config.feishu_receive_id == "ray@example.com"
    assert config.feishu_message_format == "text"
    assert config.xiaoai_host == "192.168.1.107"
    assert config.xiaoai_ssh_key == tmp_path / "speaker-key"
    assert config.notify_daily_report is True
    assert config.notify_action_triggers is True
    assert config.trend_animals_api_key == "trend-secret"
    assert config.trend_animals_a_share_tm_id == 622466
    assert config.trend_animals_etf_tm_id == 697199
    assert config.trend_animals_us_tm_ids == (622460, 700001)
    assert config.trend_animals_hk_tm_ids == (622494,)
    assert config.trend_us_symbols == ("AAPL", "VIXY")
    assert config.trend_hk_symbols == ("00700", "02800")
    assert config.trend_review_cn_simulate_acc_id == 101
    assert config.trend_review_us_simulate_acc_id == 102
    assert config.trend_review_hk_simulate_acc_id == 103
    assert config.trend_executor_host == "ray-mac"


def test_load_env_config_defaults_executor_host_to_empty(tmp_path: Path) -> None:
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

    assert load_env_config(env).trend_executor_host == ""


def test_shared_env_loader_accepts_other_positive_a_share_pool_ids(
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
                "DEEPSEEK_API_KEY=secret",
                "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID=1",
                "TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID=2",
            ]
        ),
        encoding="utf-8",
    )

    config = load_env_config(env)

    assert (config.trend_animals_a_share_tm_id, config.trend_animals_etf_tm_id) == (
        1,
        2,
    )


def test_notification_results_can_select_only_feishu_channels() -> None:
    sent: list[str] = []

    class Feishu(FeishuWebhookNotifier):
        def __init__(self) -> None:
            pass

        def notify(self, title: str, message: str) -> None:
            sent.append("feishu")

    class MacOS(MacOSNotifier):
        def notify(self, title: str, message: str) -> None:
            sent.append("macos")

    attempts = send_notification_with_results(
        CompositeNotifier([Feishu(), MacOS()]),
        "title",
        "message",
        channels={"feishu", "feishu_app"},
    )

    assert sent == ["feishu"]
    assert [attempt.channel for attempt in attempts] == ["feishu"]


def test_notification_results_can_select_only_macos() -> None:
    sent: list[str] = []

    class Feishu(FeishuWebhookNotifier):
        def __init__(self) -> None:
            pass

        def notify(self, title: str, message: str) -> None:
            sent.append("feishu")

    class MacOS(MacOSNotifier):
        def notify(self, title: str, message: str) -> None:
            sent.append("macos")

    attempts = send_notification_with_results(
        CompositeNotifier([Feishu(), MacOS()]),
        "title",
        "message",
        channels={"macos"},
    )

    assert sent == ["macos"]
    assert [attempt.channel for attempt in attempts] == ["macos"]


def test_notification_results_report_xiaoai_quiet_hours_as_suppressed() -> None:
    class Suppressed(XiaoaiSSHNotifier):
        def __init__(self) -> None:
            pass

        def notify(self, title: str, message: str) -> None:
            raise XiaoaiVoiceSuppressed("quiet hours")

    attempts = send_notification_with_results(
        Suppressed(),
        "Open Trader 测试通知",
        "测试",
        channels={"xiaoai"},
    )

    assert len(attempts) == 1
    assert attempts[0].channel == "xiaoai"
    assert attempts[0].success is False
    assert attempts[0].suppressed is True


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


def test_build_notifier_rejects_removed_xiaozhi_channel(tmp_path: Path) -> None:
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
        notifiers=("xiaozhi",),
    )

    with pytest.raises(ValueError, match="unknown notifier: xiaozhi"):
        build_notifier(config)


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


def test_build_notifier_uses_configured_feishu_app_and_xiaoai(tmp_path: Path) -> None:
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
        notifiers=("feishu_app", "xiaoai"),
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_receive_id_type="email",
        feishu_receive_id="ray@example.com",
        xiaoai_host="192.168.1.107",
        xiaoai_ssh_key=tmp_path / "speaker-key",
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, CompositeNotifier)
    inner_notifiers = notifier._notifiers
    assert len(inner_notifiers) == 2
    assert isinstance(inner_notifiers[0], FeishuAppNotifier)
    assert isinstance(inner_notifiers[1], XiaoaiSSHNotifier)
    assert inner_notifiers[1].host == config.xiaoai_host
    assert inner_notifiers[1].ssh_key == config.xiaoai_ssh_key


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


def test_refresh_live_portfolio_syncs_futu_then_tiger(
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
    )
    futu_path = tmp_path / "data/runs/2026-07-09/futu-portfolio.csv"
    tiger_path = tmp_path / "data/runs/2026-07-09/portfolio.csv"
    calls: list[str] = []
    closed: list[str] = []

    class FakeFutuClient:
        def __init__(self, *, host: str, port: int) -> None:
            calls.append(f"futu_client:{host}:{port}")

        def fetch_snapshot(self) -> object:
            calls.append("futu_snapshot")
            return object()

        def close(self) -> None:
            closed.append("futu")

    class FakeTigerClient:
        def __init__(self, *, config: object) -> None:
            calls.append(f"tiger_client:{config}")

        def fetch_snapshot(self) -> object:
            calls.append("tiger_snapshot")
            return object()

        def close(self) -> None:
            closed.append("tiger")

    def fake_sync_futu_portfolio(**kwargs: object) -> object:
        calls.append("sync_futu")
        assert kwargs["portfolio_path"] == config.portfolio
        assert kwargs["data_dir"] == config.data_dir
        assert kwargs["reports_dir"] == config.reports_dir
        assert kwargs["run_date"] == "2026-07-09"
        assert kwargs["update_latest"] is True
        return type("Result", (), {"portfolio_path": futu_path})()

    def fake_sync_tiger_portfolio(**kwargs: object) -> object:
        calls.append("sync_tiger")
        assert kwargs["portfolio_path"] == config.portfolio
        assert kwargs["data_dir"] == config.data_dir
        assert kwargs["reports_dir"] == config.reports_dir
        assert kwargs["run_date"] == "2026-07-09"
        assert kwargs["update_latest"] is True
        return type("Result", (), {"portfolio_path": tiger_path})()

    monkeypatch.setattr(daily_premarket, "FutuAccountClient", FakeFutuClient)
    monkeypatch.setattr(
        daily_premarket,
        "sync_futu_portfolio",
        fake_sync_futu_portfolio,
    )
    monkeypatch.setattr(
        daily_premarket,
        "load_tiger_account_config",
        lambda **kwargs: "tiger-config",
    )
    monkeypatch.setattr(daily_premarket, "TigerAccountClient", FakeTigerClient)
    monkeypatch.setattr(
        daily_premarket,
        "sync_tiger_portfolio",
        fake_sync_tiger_portfolio,
    )

    result = daily_premarket.refresh_live_portfolio(
        run_date="2026-07-09",
        market="US",
        config=config,
    )

    assert result == tiger_path
    assert calls == [
        "futu_client:127.0.0.1:11111",
        "futu_snapshot",
        "sync_futu",
        "tiger_client:tiger-config",
        "tiger_snapshot",
        "sync_tiger",
    ]
    assert closed == ["futu", "tiger"]


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


def test_build_notifier_requires_xiaoai_config(tmp_path: Path) -> None:
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
        notifiers=("xiaoai",),
        xiaoai_host="192.168.1.107",
    )

    with pytest.raises(ValueError, match="OPEN_TRADER_XIAOAI_SSH_KEY is required"):
        build_notifier(config)


def test_daily_runner_defaults_to_generate_trade_actions_and_summary(
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
    )

    runner = _DailyPremarketRunner(config=config)

    assert runner.trade_action_generator is daily_premarket.generate_trade_actions
    assert runner.summary_generator is daily_premarket.generate_tradingagents_summary
    assert (
        runner.summary_extractor_factory
        is daily_premarket.LLMTradingAgentsSummaryExtractor
    )
    assert runner.decision_plan_generator is daily_premarket.generate_daily_decision_plans


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


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (
            ["technical_facts"],
            ".venv/bin/python -m open_trader extract-technical-facts --advice data/latest/US/trading_advice.csv --data-dir data --date 2026-06-19 --market US --update-latest",
        ),
        (
            ["decision_facts.kline", "decision_facts.news_sentiment"],
            ".venv/bin/python -m open_trader extract-decision-facts --advice data/latest/US/trading_advice.csv --data-dir data --date 2026-06-19 --market US --update-latest",
        ),
        (
            ["tradingagents_summary"],
            ".venv/bin/python -m open_trader extract-tradingagents-summary --advice data/latest/US/trading_advice.csv --plan data/latest/US/trading_plan.csv --actions data/latest/US/trade_actions.csv --data-dir data --date 2026-06-19 --market US --update-latest",
        ),
        (
            ["futu_skill_facts.news_sentiment", "futu_skill_facts.capital_anomaly"],
            ".venv/bin/python -m open_trader extract-futu-skill-facts --portfolio data/latest/portfolio.csv --data-dir data --date 2026-06-19 --market US --update-latest",
        ),
    ],
)
def test_blocker_notification_includes_deduplicated_source_retry_command(
    sources: list[str], expected: str
) -> None:
    failures = [
        SourceFailure("US", "MSFT", source, "failed") for source in sources
    ]

    body = daily_premarket._blocker_notification_message(
        run_date="2026-06-19",
        status="failed",
        futu_status={"error": "", "missing": 0, "diagnostic": {}},
        trade_actions={"review": 0},
        artifacts={},
        readiness="blocked",
        source_failures=failures,
    )

    assert expected in body
    assert body.count(expected) == 1


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


def test_daily_premarket_includes_tradingagents_summary_artifact(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "data/runs/2026-06-23/US/tradingagents_summary.json"
    latest_summary_path = tmp_path / "data/latest/US/tradingagents_summary.json"
    payload = {
        "run_date": "2026-06-23",
        "market": "US",
        "started_at": "2026-06-23T18:30:00+08:00",
        "finished_at": "2026-06-23T18:35:00+08:00",
        "deadline_at": "2026-06-23T21:10:00+08:00",
        "status": "ok",
        "readiness": "ready",
        "status_reasons": [],
        "premarket": {},
        "trading_plan": {},
        "futu_plan_check": {},
        "trade_actions": {},
        "artifacts": {
            "tradingagents_summary": str(summary_path),
            "latest_tradingagents_summary": str(latest_summary_path),
        },
    }

    report = daily_premarket._render_daily_report(payload)

    assert "tradingagents_summary" in report
    assert str(summary_path) in report
    assert "latest_tradingagents_summary" in report
    assert str(latest_summary_path) in report


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


def test_run_lock_removes_lock_file_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"

    with RunLock(lock_path):
        assert lock_path.exists()

    assert not lock_path.exists()


class FakePremarket:
    def __init__(self, *, market: str = "US", symbol: str = "MSFT", source_failure: str = "") -> None:
        self.market = market
        self.symbol = symbol
        self.source_failure = source_failure
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
        technical_facts_path = run_dir / "technical_facts.json"
        decision_facts_path = run_dir / "decision_facts.json"
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        raw_decision = json.dumps(
            {"state": {"market_report": "market report", "sentiment_report": "sentiment report", "news_report": "news report"}}
        )
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
                    "raw_decision": raw_decision,
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
        technical_facts_path.write_text(json.dumps({
            "schema_version": "open_trader.technical_facts_cache.v1",
            "run_date": run_date,
            "records": [{"run_date": run_date, "market": self.market, "symbol": self.symbol, "source_hash": source_hash("market report"), "extraction_status": "ok", "facts": {"timeframes": [{"timeframe": "daily"}]}, "freshness": {"status": "fresh"}, "error": ""}],
        }) + "\n", encoding="utf-8")
        decision_sources = extract_decision_sources(raw_decision)
        decision_facts_path.write_text(json.dumps({
            "schema_version": "open_trader.decision_facts.v1",
            "run_date": run_date,
            "records": [{"run_date": run_date, "market": self.market, "symbol": self.symbol, "kline": {"status": "ok", "source_hash": decision_sources.kline_hash, "fields": {field: "值" for field in ("trend", "position", "momentum", "key_levels", "risk")}}, "news_sentiment": {"status": "ok", "source_hash": decision_sources.news_sentiment_hash, "fields": {field: "值" for field in ("direction", "change", "catalyst", "risk", "attention")}}, "error": ""}],
        }) + "\n", encoding="utf-8")
        if self.source_failure == "technical_facts":
            payload = json.loads(technical_facts_path.read_text(encoding="utf-8"))
            payload["records"][0].update(extraction_status="extraction_failed", error="技术抽取失败")
            technical_facts_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        elif self.source_failure.startswith("decision_facts."):
            payload = json.loads(decision_facts_path.read_text(encoding="utf-8"))
            module = self.source_failure.removeprefix("decision_facts.")
            payload["records"][0][module].update(status="error", error="决策来源失败")
            decision_facts_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
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
                "technical_facts_path": technical_facts_path,
                "decision_facts_path": decision_facts_path,
                "report_path": Path("reports/premarket") / f"{run_date}.md",
            },
        )()


class EmptyPremarket:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object):
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        market = kwargs["market"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        assert isinstance(market, str)
        run_dir = data_dir / "runs" / run_date / market
        advice_path = run_dir / "trading_advice.csv"
        classifications_path = run_dir / "change_classifications.csv"
        actions_path = run_dir / "premarket_actions.csv"
        report_path = Path("reports/premarket") / f"{run_date}-{market}.md"
        run_dir.mkdir(parents=True, exist_ok=True)
        advice_path.write_text(
            "run_date,symbol,market,asset_class,portfolio_weight_hkd,risk_flag,"
            "source,advice_action,advice_summary,raw_decision,status,error,"
            "source_status,fallback_reason,fallback_from_date\n",
            encoding="utf-8",
        )
        classifications_path.write_text(
            "run_date,symbol,include_in_report,change_type,severity,"
            "suggested_action,summary,rationale,watch_trigger,status,error\n",
            encoding="utf-8",
        )
        actions_path.write_text(
            "run_date,symbol,market,portfolio_weight_hkd,severity,change_type,"
            "suggested_action,summary,rationale,watch_trigger\n",
            encoding="utf-8",
        )
        return type(
            "PremarketResult",
            (),
            {
                "eligible_count": 0,
                "advice_count": 0,
                "action_count": 0,
                "advice_path": advice_path,
                "classifications_path": classifications_path,
                "actions_path": actions_path,
                "report_path": report_path,
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


class CapturingPlanBuilder(FakePlanBuilder):
    pass


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


class FakeSummaryExtractor:
    pass


class FakeTradingAgentsSummaryGenerator:
    def __init__(self, source_failure: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.source_failure = source_failure

    def __call__(self, **kwargs: object) -> TradingAgentsSummaryResult:
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        market = kwargs["market"]
        advice_path = kwargs["advice_path"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        assert isinstance(market, str)
        assert isinstance(advice_path, Path)
        symbol = next(csv.DictReader(advice_path.open(encoding="utf-8")))["symbol"]
        run_path = data_dir / "runs" / run_date / market / "tradingagents_summary.json"
        latest_path = data_dir / "latest" / market / "tradingagents_summary.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(
            json.dumps(
                {
                    "schema_version": "open_trader.tradingagents_summary.v1",
                    "generated_at": "2026-06-17T21:05:00+08:00",
                    "latest_run_date": run_date,
                    "market": market,
                    "records": [{
                        "schema_version": "open_trader.tradingagents_summary.v1",
                        "market": market,
                        "symbol": symbol,
                        "latest_run_date": run_date,
                        "ta_report_date": run_date,
                        "ta_view": "看多",
                        "current_action": "持有",
                        "core_reason": "基本面稳健",
                        "reason_fields": {"main_judgment": "基本面稳健", "evidence_1": "盈利增长", "evidence_2": "现金流充足", "risk_or_counterpoint": "估值偏高", "action_logic": "继续持有"},
                        "source_hash": "sha256:" + "a" * 64,
                        "error": "TradingAgents摘要失败" if self.source_failure else "",
                    }],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return TradingAgentsSummaryResult(
            run_date=run_date,
            records=1,
            extracted=1,
            failed=0,
            reused=0,
            run_path=run_path,
            latest_path=latest_path,
        )


class FailingTradingAgentsSummaryGenerator:
    def __init__(self, message: str = "summary failed") -> None:
        self.message = message
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> TradingAgentsSummaryResult:
        self.calls.append(kwargs)
        raise RuntimeError(self.message)


class FakeFutuFactsGenerator:
    def __init__(self, source_failure: str = "") -> None:
        self.calls: list[dict[str, object]] = []
        self.source_failure = source_failure

    def __call__(self, **kwargs: object) -> FutuSkillFactResult:
        self.calls.append({key: value for key, value in kwargs.items() if key != "extractor"})
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        market = kwargs["market"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        assert isinstance(market, str)
        run_path = data_dir / "runs" / run_date / market / "futu_skill_facts.json"
        latest_path = data_dir / "latest" / market / "futu_skill_facts.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        symbol = "00700" if market == "HK" else "MSFT"
        modules = {name: {"status": "ok"} for name in ("news_sentiment", "technical_anomaly", "capital_anomaly", "derivatives_anomaly")}
        if self.source_failure:
            modules[self.source_failure] = {"status": "error", "error": "Futu来源失败"}
        run_path.write_text(json.dumps({
            "schema_version": "open_trader.futu_skill_facts.v1",
            "run_date": run_date,
            "records": [{"run_date": run_date, "market": market, "symbol": symbol, **modules, "error": ""}],
        }) + "\n", encoding="utf-8")
        return FutuSkillFactResult(run_date, 1, 1, 0, run_path, latest_path)


class FailingFutuFactsGenerator:
    def __call__(self, **_: object) -> FutuSkillFactResult:
        raise RuntimeError("futu service unavailable")


class FailingNotifier:
    def notify(self, title: str, message: str) -> None:
        raise RuntimeError("delivery failed")


class FakeDecisionPlanGenerator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("decision plan failed")
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        market = kwargs["market"]
        assert isinstance(data_dir, Path)
        run_path = data_dir / "runs" / str(run_date) / str(market) / "decision_plans.json"
        latest_path = data_dir / "latest" / str(market) / "decision_plans.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps({
            "schema_version": "open_trader.decision_plans.v1",
            "run_date": run_date,
            "market": market,
            "records": [],
        }) + "\n", encoding="utf-8")
        return SimpleNamespace(run_path=run_path, latest_path=latest_path, records=0)


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


def test_require_trend_review_config_returns_selected_market_values(
    tmp_path: Path,
) -> None:
    config = replace(
        _daily_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )

    assert daily_premarket.require_trend_review_config(config, "CN") == 101
    assert daily_premarket.require_trend_review_config(config, "US") == 102


def test_trend_execution_mode_requires_exact_named_host(tmp_path: Path) -> None:
    config = replace(_daily_config(tmp_path), trend_executor_host="ray-mac")

    matched = daily_premarket.trend_execution_mode(
        config,
        hostname_fn=lambda: "ray-mac",
    )
    assert matched.mode == "execute"
    assert matched.reason == "executor host matched"

    mismatch = daily_premarket.trend_execution_mode(
        config,
        hostname_fn=lambda: "laptop",
    )
    assert mismatch.mode == "readonly"
    assert mismatch.executor_host == "ray-mac"
    assert mismatch.local_host == "laptop"
    assert mismatch.reason == (
        "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"
    )


def test_missing_executor_host_is_readonly(tmp_path: Path) -> None:
    config = _daily_config(tmp_path)

    mode = daily_premarket.trend_execution_mode(
        config,
        hostname_fn=lambda: "ray-mac",
    )
    assert mode.mode == "readonly"
    assert mode.executor_host == ""
    assert mode.local_host == "ray-mac"
    assert mode.reason == "OPEN_TRADER_TREND_EXECUTOR_HOST is not configured"

    with pytest.raises(ValueError, match="trend automation is readonly"):
        daily_premarket.require_trend_executor(
            config,
            hostname_fn=lambda: "ray-mac",
        )


def _daily_runner(
    *,
    summary_generator: object | None = None,
    summary_extractor_factory: object = FakeSummaryExtractor,
    futu_facts_generator: object | None = None,
    decision_plan_generator: object | None = None,
    **kwargs: object,
) -> _DailyPremarketRunner:
    return _DailyPremarketRunner(
        **kwargs,
        summary_generator=summary_generator or FakeTradingAgentsSummaryGenerator(),
        summary_extractor_factory=summary_extractor_factory,
        futu_facts_generator=futu_facts_generator or FakeFutuFactsGenerator(),
        futu_facts_extractor_factory=FakeSummaryExtractor,
        decision_plan_generator=decision_plan_generator or FakeDecisionPlanGenerator(),
    )


def test_daily_runner_status_excludes_retired_tiger_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    refreshed = tmp_path / "data/runs/2026-06-19/portfolio.csv"
    refreshed.parent.mkdir(parents=True, exist_ok=True)
    refreshed.write_text("symbol\nMSFT\n", encoding="utf-8")
    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        portfolio_refresher=lambda **_: refreshed,
    ).run("2026-06-19", market="US")

    assert result.status == "success"
    payload = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert not any(key.startswith("tiger_" + "long_term") for key in payload["artifacts"])
    assert "tiger_" + "long_term_strategy_failed" not in payload["status_reasons"]


def _notification_rows(
    tmp_path: Path,
    run_date: str,
    market: str,
) -> list[dict[str, str]]:
    return list(
        csv.DictReader(
            (tmp_path / f"logs/notifications/{run_date}-{market}.csv").open(
                encoding="utf-8"
            )
        )
    )


def test_daily_runner_generates_futu_facts_from_refreshed_portfolio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nold\n", encoding="utf-8")
    refreshed_portfolio = tmp_path / "data/runs/2026-06-19/portfolio.csv"
    refreshed_portfolio.parent.mkdir(parents=True, exist_ok=True)
    refreshed_portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    futu_facts = FakeFutuFactsGenerator()

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        portfolio_refresher=lambda **_: refreshed_portfolio,
        futu_facts_generator=futu_facts,
    ).run("2026-06-19", market="US")

    assert result.status == "success"
    assert futu_facts.calls == [{
        "portfolio_path": refreshed_portfolio,
        "data_dir": config.data_dir,
        "run_date": "2026-06-19",
        "market": "US",
        "update_latest": False,
    }]


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [
        ("tradingagents_summary", "TradingAgents摘要失败"),
        ("technical_facts", "技术抽取失败"),
        ("decision_facts.kline", "决策来源失败"),
        ("decision_facts.news_sentiment", "决策来源失败"),
        ("futu_skill_facts.news_sentiment", "Futu来源失败"),
        ("futu_skill_facts.technical_anomaly", "Futu来源失败"),
        ("futu_skill_facts.capital_anomaly", "Futu来源失败"),
        ("futu_skill_facts.derivatives_anomaly", "Futu来源失败"),
    ],
)
def test_daily_runner_publishes_successful_sources_when_record_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_error: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = replace(_daily_config(tmp_path), notify_daily_report=True)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    latest_dir = config.data_dir / "latest/US"
    latest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("technical_facts.json", "decision_facts.json", "tradingagents_summary.json", "futu_skill_facts.json"):
        (latest_dir / name).write_text("stale-old-marker\n", encoding="utf-8")
    notifier = CapturingNotifier()
    premarket_source = source if source.startswith(("technical_", "decision_facts.")) else ""
    summary = FakeTradingAgentsSummaryGenerator(source == "tradingagents_summary")
    futu_module = source.removeprefix("futu_skill_facts.") if source.startswith("futu_skill_facts.") else ""

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(source_failure=premarket_source),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        summary_generator=summary,
        futu_facts_generator=FakeFutuFactsGenerator(futu_module),
        notifier=notifier,
    ).run(run_date="2026-06-19", market="US")

    assert result.status == "failed"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["readiness"] == "blocked"
    assert status["source_failures"] == [{
        "market": "US",
        "symbol": "MSFT",
        "source": source,
        "error": expected_error,
    }]
    assert "source_incomplete" in status["status_reasons"]
    run_dir = config.data_dir / "runs/2026-06-19/US"
    for name in ("technical_facts.json", "decision_facts.json", "tradingagents_summary.json", "futu_skill_facts.json"):
        assert (latest_dir / name).read_text(encoding="utf-8") == (run_dir / name).read_text(encoding="utf-8")
    assert "stale-old-marker" not in (latest_dir / "technical_facts.json").read_text(encoding="utf-8")
    blocker = next(message for title, message in notifier.calls if "阻塞通知" in title)
    assert "US.MSFT" in blocker
    assert source in blocker
    assert expected_error in blocker
    assert "launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.us" in blocker


def test_daily_config_deadline_for_market_uses_hk_and_us_defaults(
    tmp_path: Path,
) -> None:
    config = _daily_config(tmp_path)

    assert daily_premarket._deadline_for_market(config, "HK") == "09:00"
    assert daily_premarket._deadline_for_market(config, "US") == "21:10"


def test_daily_config_for_hk_uses_shanghai_timezone(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="UTC",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
    )

    hk_config = daily_premarket._config_for_market(config, "HK")
    us_config = daily_premarket._config_for_market(config, "US")

    assert hk_config.timezone == "Asia/Shanghai"
    assert hk_config.deadline == "09:00"
    assert daily_premarket._deadline_at(hk_config, "2026-06-19").isoformat() == (
        "2026-06-19T09:00:00+08:00"
    )
    assert us_config.timezone == "UTC"
    assert us_config.deadline == "21:10"


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

    result = _daily_runner(
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


def test_daily_runner_refreshes_portfolio_before_premarket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    stale_portfolio = config.portfolio
    stale_portfolio.parent.mkdir(parents=True, exist_ok=True)
    stale_portfolio.write_text("stale\n", encoding="utf-8")
    refreshed_portfolio = tmp_path / "data/runs/2026-06-19/portfolio.csv"
    refreshed_portfolio.parent.mkdir(parents=True, exist_ok=True)
    refreshed_portfolio.write_text("fresh\n", encoding="utf-8")
    refresh_calls: list[dict[str, object]] = []
    premarket = FakePremarket(market="US", symbol="MSFT")
    trade_actions = FakeTradeActionGenerator(market="US", symbol="MSFT")

    def refresh_portfolio(**kwargs: object) -> Path:
        refresh_calls.append(kwargs)
        return refreshed_portfolio

    _daily_runner(
        config=config,
        portfolio_refresher=refresh_portfolio,
        premarket_runner=premarket,
        plan_builder=FakePlanBuilder(market="US", symbol="MSFT"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("390"))},
            **kwargs,
        ),
        trade_action_generator=trade_actions,
    ).run(run_date="2026-06-19", market="US")

    assert refresh_calls == [
        {
            "run_date": "2026-06-19",
            "market": "US",
            "config": config,
        }
    ]
    assert premarket.calls[0]["portfolio_path"] == refreshed_portfolio
    assert trade_actions.calls[0]["portfolio_path"] == refreshed_portfolio


def test_daily_runner_uses_real_premarket_fact_generators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("portfolio\n", encoding="utf-8")
    premarket = FakePremarket(market="US", symbol="MSFT")

    _daily_runner(
        config=config,
        premarket_runner=premarket,
        plan_builder=FakePlanBuilder(market="US", symbol="MSFT"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("390"))},
            **kwargs,
        ),
        trade_action_generator=FakeTradeActionGenerator(market="US", symbol="MSFT"),
    ).run(run_date="2026-06-19", market="US")

    call = premarket.calls[0]
    assert "technical_facts_generator" not in call
    assert "decision_facts_generator" not in call


def test_daily_runner_falls_back_when_premarket_omits_technical_facts_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("portfolio\n", encoding="utf-8")
    premarket = FakePremarket(market="US", symbol="MSFT")

    def premarket_runner(**kwargs: object) -> PremarketResult:
        result = premarket(**kwargs)
        return PremarketResult(
            eligible_count=result.eligible_count,
            advice_count=result.advice_count,
            action_count=result.action_count,
            advice_path=result.advice_path,
            classifications_path=result.classifications_path,
            actions_path=result.actions_path,
            report_path=result.report_path,
            decision_facts_path=result.decision_facts_path,
        )

    result = _daily_runner(
        config=config,
        premarket_runner=premarket_runner,
        plan_builder=FakePlanBuilder(market="US", symbol="MSFT"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("390"))}, **kwargs
        ),
        trade_action_generator=FakeTradeActionGenerator(market="US", symbol="MSFT"),
    ).run(run_date="2026-06-19", market="US")

    assert result.status == "success"
    assert (tmp_path / "data/latest/US/technical_facts.json").exists()


def test_hk_daily_runner_uses_market_notification_titles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    base_config = _daily_config(tmp_path)
    config = DailyPremarketConfig(
        **{
            **base_config.__dict__,
            "notify_daily_report": True,
        }
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("portfolio\n", encoding="utf-8")
    notifier = CapturingNotifier()

    _daily_runner(
        config=config,
        premarket_runner=FakePremarket(market="HK", symbol="00700"),
        plan_builder=FakePlanBuilder(market="HK", symbol="00700"),
        quote_client_factory=lambda **kwargs: FakeQuoteClient(
            {"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))},
            **kwargs,
        ),
        trade_action_generator=FakeTradeActionGenerator(market="HK", symbol="00700"),
        notifier=notifier,
    ).run(run_date="2026-06-19", market="HK")

    assert [title for title, _ in notifier.calls] == [
        "Open Trader 港股开始通知",
        "Open Trader 港股行动通知",
        "Open Trader 港股完成通知",
    ]
    rows = _notification_rows(tmp_path, "2026-06-19", "HK")
    assert len(rows) == 3
    assert [row["title"] for row in rows] == [
        "Open Trader 港股开始通知",
        "Open Trader 港股行动通知",
        "Open Trader 港股完成通知",
    ]
    for row in rows:
        assert row["market"] == "HK"
        assert row["channel"] == "CapturingNotifier"
        assert row["success"] == "true"


def test_daily_notify_logs_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = CapturingNotifier()
    runner = _daily_runner(
        config=_daily_config(tmp_path),
        notifier=notifier,
    )

    with caplog.at_level("INFO", logger="open_trader.daily_premarket"):
        runner._notify(
            "Open Trader 美股行动通知",
            "测试正文",
            market="US",
            run_date="2026-06-17",
        )

    assert notifier.calls == [("Open Trader 美股行动通知", "测试正文")]
    assert "通知已发送：Open Trader 美股行动通知" in caplog.text
    rows = _notification_rows(tmp_path, "2026-06-17", "US")
    assert len(rows) == 1
    assert rows[0]["market"] == "US"
    assert rows[0]["title"] == "Open Trader 美股行动通知"
    assert rows[0]["channel"] == "CapturingNotifier"
    assert rows[0]["success"] == "true"


def test_daily_notify_logs_failure_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = _daily_runner(
        config=_daily_config(tmp_path),
        notifier=FailingNotifier(),
    )

    with caplog.at_level("WARNING", logger="open_trader.daily_premarket"):
        runner._notify(
            "Open Trader 港股阻塞通知",
            "测试正文",
            market="HK",
            run_date="2026-06-19",
        )

    assert "通知发送失败：Open Trader 港股阻塞通知" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "delivery failed" in caplog.text
    rows = _notification_rows(tmp_path, "2026-06-19", "HK")
    assert len(rows) == 1
    assert rows[0]["market"] == "HK"
    assert rows[0]["title"] == "Open Trader 港股阻塞通知"
    assert rows[0]["channel"] == "FailingNotifier"
    assert rows[0]["success"] == "false"
    assert rows[0]["error_type"] == "RuntimeError"
    assert rows[0]["error"] == "delivery failed"


def test_daily_notify_logs_composite_child_failure_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class WorkingNotifier:
        def notify(self, title: str, message: str) -> None:
            pass

    runner = _daily_runner(
        config=_daily_config(tmp_path),
        notifier=CompositeNotifier([FailingNotifier(), WorkingNotifier()]),
    )

    with caplog.at_level("INFO", logger="open_trader.daily_premarket"):
        runner._notify(
            "Open Trader 美股行动通知",
            "测试正文",
            market="US",
            run_date="2026-06-17",
        )

    assert "通知发送失败：Open Trader 美股行动通知" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "delivery failed" in caplog.text
    assert "通知已发送：Open Trader 美股行动通知" in caplog.text
    rows = _notification_rows(tmp_path, "2026-06-17", "US")
    assert [row["market"] for row in rows] == ["US", "US"]
    assert [row["title"] for row in rows] == [
        "Open Trader 美股行动通知",
        "Open Trader 美股行动通知",
    ]
    assert [row["success"] for row in rows] == ["false", "true"]


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
    fake_summary = FakeTradingAgentsSummaryGenerator()

    runner = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=fake_trade_actions,
        summary_generator=fake_summary,
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
    assert len(fake_summary.calls) == 1
    assert fake_summary.calls[0]["advice_path"] == (
        tmp_path / "data/runs/2026-06-17/US/trading_advice.csv"
    )
    assert fake_summary.calls[0]["plan_path"] == (
        tmp_path / "data/runs/2026-06-17/US/trading_plan.csv"
    )
    assert fake_summary.calls[0]["actions_path"] == (
        tmp_path / "data/runs/2026-06-17/US/trade_actions.csv"
    )
    assert fake_summary.calls[0]["data_dir"] == tmp_path / "data"
    assert fake_summary.calls[0]["run_date"] == "2026-06-17"
    assert fake_summary.calls[0]["market"] == "US"
    assert isinstance(fake_summary.calls[0]["extractor"], FakeSummaryExtractor)
    assert fake_summary.calls[0]["update_latest"] is False
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
    assert status["artifacts"]["tradingagents_summary"] == str(
        tmp_path / "data/runs/2026-06-17/US/tradingagents_summary.json"
    )
    assert status["artifacts"]["latest_tradingagents_summary"] == str(
        tmp_path / "data/latest/US/tradingagents_summary.json"
    )
    assert (tmp_path / "data/latest/US/tradingagents_summary.json").read_text(
        encoding="utf-8"
    ) == (
        tmp_path / "data/runs/2026-06-17/US/tradingagents_summary.json"
    ).read_text(encoding="utf-8")
    report = (tmp_path / "reports/daily_runs/2026-06-17-US.md").read_text(
        encoding="utf-8"
    )
    assert "- trade_actions: " in report
    assert "- trade_actions_report: " in report
    assert "- tradingagents_summary: " in report
    assert "- latest_tradingagents_summary: " in report
    assert (tmp_path / "reports/daily_runs/2026-06-17-US.md").exists()


def test_daily_runner_stops_when_portfolio_filters_to_no_report_symbols(
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
    config.portfolio.write_text(
        "market,asset_class,symbol,ai_eligible,risk_flag\n"
        "US,cash,USD_CASH,false,normal\n",
        encoding="utf-8",
    )
    premarket = EmptyPremarket()
    plan_builder = CapturingPlanBuilder()

    runner = _daily_runner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        summary_generator=FakeTradingAgentsSummaryGenerator(),
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "failed"
    assert plan_builder.calls == []
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/US/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["readiness"] == "blocked"
    assert status["status_reasons"] == ["no_report_symbols"]
    assert status["error"] == "portfolio filters produced no US report symbols"
    assert status["premarket"]["eligible"] == 0
    assert status["artifacts"]["advice"].endswith(
        "data/runs/2026-06-17/US/trading_advice.csv"
    )
    report = (tmp_path / "reports/daily_runs/2026-06-17-US.md").read_text(
        encoding="utf-8"
    )
    assert "- 原因：过滤后无报告标的" in report
    assert "- portfolio: " in report


def test_daily_runner_retains_only_missing_source_latest_when_summary_generation_fails(
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
        notify_daily_report=True,
    )
    latest_dir = tmp_path / "data/latest/US"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "tradingagents_summary.json").write_text(
        '{"old": true}\n',
        encoding="utf-8",
    )
    failing_summary = FailingTradingAgentsSummaryGenerator("summary service unavailable")
    notifier = CapturingNotifier()

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        summary_generator=failing_summary,
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "failed"
    assert len(failing_summary.calls) == 1
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    artifacts = status["artifacts"]
    assert status["status"] == "failed"
    assert status["readiness"] == "blocked"
    assert "tradingagents_summary_failed" in status["status_reasons"]
    assert "source_incomplete" in status["status_reasons"]
    assert "error" not in status
    assert artifacts["advice"] == str(
        tmp_path / "data/runs/2026-06-17/US/trading_advice.csv"
    )
    assert artifacts["trading_plan"] == str(
        tmp_path / "data/runs/2026-06-17/US/trading_plan.csv"
    )
    assert artifacts["trade_actions"] == str(
        tmp_path / "data/runs/2026-06-17/US/trade_actions.csv"
    )
    assert artifacts["tradingagents_summary"] == ""
    assert artifacts["latest_tradingagents_summary"] == ""
    assert status["source_failures"][0]["error"] == "summary service unavailable"
    assert (latest_dir / "trading_advice.csv").exists()
    assert (latest_dir / "premarket_actions.csv").exists()
    assert (latest_dir / "trading_plan.csv").exists()
    assert (latest_dir / "trade_actions.csv").exists()
    assert (latest_dir / "tradingagents_summary.json").read_text(
        encoding="utf-8"
    ) == '{"old": true}\n'
    report = result.report_path.read_text(encoding="utf-8")
    assert "- Status: failed" in report
    assert "TradingAgents 摘要生成异常" in report
    assert "- tradingagents_summary: " in report
    assert f"- latest_tradingagents_summary: {latest_dir}" not in report
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 美股阻塞通知"
    ]
    assert len(blocker_calls) == 1
    _, body = blocker_calls[0]
    assert "Open Trader｜阻塞通知" in body
    assert "TradingAgents 摘要生成异常" in body
    assert "决策来源不完整" in body
    assert "报告：" in body


def test_daily_runner_retains_only_missing_source_latest_when_futu_generation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    config = _daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    latest_futu = config.data_dir / "latest/US/futu_skill_facts.json"
    latest_futu.parent.mkdir(parents=True, exist_ok=True)
    latest_futu.write_text('{"old": true}\n', encoding="utf-8")

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        futu_facts_generator=FailingFutuFactsGenerator(),
    ).run("2026-06-17", market="US")

    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert result.status == "failed"
    assert {failure["source"] for failure in status["source_failures"]} == {
        "futu_skill_facts.news_sentiment",
        "futu_skill_facts.technical_anomaly",
        "futu_skill_facts.capital_anomaly",
        "futu_skill_facts.derivatives_anomaly",
    }
    assert {failure["error"] for failure in status["source_failures"]} == {
        "futu service unavailable"
    }
    assert latest_futu.read_text(encoding="utf-8") == '{"old": true}\n'
    assert (config.data_dir / "latest/US/technical_facts.json").exists()


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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "success"
    assert [title for title, _ in notifier.calls] == [
        "Open Trader 美股开始通知",
        "Open Trader 美股行动通知",
        "Open Trader 美股完成通知",
    ]
    start_body = notifier.calls[0][1]
    assert "Open Trader｜开始通知" in start_body
    assert "状态：开始运行｜并发：8" in start_body
    title, body = notifier.calls[1]
    assert title == "Open Trader 美股行动通知"
    assert "Open Trader｜行动通知" in body
    assert "今日结论：有 1 条可采取行动，需人工确认后执行。" in body
    assert "标的：MSFT｜指示：买入 3 股｜优先级：高" in body
    assert "影响：" in body
    assert "reports/" not in body
    finish_body = notifier.calls[2][1]
    assert "Open Trader｜完成通知" in finish_body
    assert "状态：成功｜可用性：可复核" in finish_body
    assert "并发：8" in finish_body


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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 美股阻塞通知"
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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=MissingQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 美股阻塞通知"
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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=InterruptedQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "partial"
    blocker_calls = [
        call for call in notifier.calls if call[0] == "Open Trader 美股阻塞通知"
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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=notifier,
    ).run("2026-06-17", market="US")

    assert result.status == "failed"
    assert [title for title, _ in notifier.calls] == [
        "Open Trader 美股开始通知",
        "Open Trader 美股阻塞通知",
        "Open Trader 美股完成通知",
    ]
    title, body = notifier.calls[1]
    assert title == "Open Trader 美股阻塞通知"
    assert "运行失败：每日流程未完成。" in body
    assert "portfolio not found" not in body
    assert "状态文件：" in body
    finish_body = notifier.calls[2][1]
    assert "Open Trader｜完成通知" in finish_body
    assert "状态：失败｜可用性：阻塞" in finish_body
    rows = _notification_rows(tmp_path, "2026-06-17", "US")
    assert len(rows) == 3
    assert [row["title"] for row in rows] == [
        "Open Trader 美股开始通知",
        "Open Trader 美股阻塞通知",
        "Open Trader 美股完成通知",
    ]
    for row in rows:
        assert row["market"] == "US"
        assert row["success"] == "true"


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

    result = _daily_runner(
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
    assert [title for title, _ in notifier.calls] == [
        "Open Trader 美股开始通知",
        "Open Trader 美股行动通知",
        "Open Trader 美股完成通知",
    ]


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

    result = _daily_runner(
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
        assert [title for title, _ in notifier.calls] == [
            "Open Trader 美股开始通知",
            "Open Trader 美股完成通知",
        ]
    else:
        assert [title for title, _ in notifier.calls] == [
            "Open Trader 美股开始通知",
            "Open Trader 美股阻塞通知",
            "Open Trader 美股完成通知",
        ]
        title, body = notifier.calls[1]
        assert title == "Open Trader 美股阻塞通知"
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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
    runner = _daily_runner(
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
    runner = _daily_runner(
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
    runner = _daily_runner(
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
    fake_decision_plans = FakeDecisionPlanGenerator()

    runner = _daily_runner(
        config=config,
        premarket_runner=premarket,
        plan_builder=plan_builder,
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=fake_trade_actions,
        decision_plan_generator=fake_decision_plans,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17", market="US")

    assert result.status == "success"
    assert premarket.calls[0]["update_latest"] is False
    assert plan_builder.calls[0]["update_latest"] is False
    assert fake_trade_actions.calls[0]["update_latest"] is False
    assert fake_decision_plans.calls[0]["update_latest"] is False
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
    assert (tmp_path / "data/latest/US/decision_plans.json").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/decision_plans.json").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "data/latest/US/technical_facts.json").read_text(
        encoding="utf-8"
    ) == (tmp_path / "data/runs/2026-06-17/US/technical_facts.json").read_text(
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
    assert status["artifacts"]["latest_technical_facts"] == str(
        tmp_path / "data/latest/US/technical_facts.json"
    )


def test_daily_runner_promotes_decision_facts(tmp_path: Path) -> None:
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

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-06-17", market="US")

    run_decision_facts = tmp_path / "data/runs/2026-06-17/US/decision_facts.json"
    latest_decision_facts = tmp_path / "data/latest/US/decision_facts.json"
    assert result.status == "success"
    assert latest_decision_facts.read_text(encoding="utf-8") == (
        run_decision_facts.read_text(encoding="utf-8")
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["artifacts"]["decision_facts"] == str(run_decision_facts)
    assert status["artifacts"]["latest_decision_facts"] == str(latest_decision_facts)


def test_promote_latest_set_skips_non_blocking_fact_placeholders(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs/2026-07-06/US"
    latest_dir = data_dir / "latest/US"
    run_dir.mkdir(parents=True)
    latest_dir.mkdir(parents=True)
    advice_path = run_dir / "trading_advice.csv"
    actions_path = run_dir / "premarket_actions.csv"
    plan_path = run_dir / "trading_plan.csv"
    trade_actions_path = run_dir / "trade_actions.csv"
    technical_facts_path = run_dir / "technical_facts.json"
    decision_facts_path = run_dir / "decision_facts.json"
    for path, text in (
        (advice_path, "new advice\n"),
        (actions_path, "new actions\n"),
        (plan_path, "new plan\n"),
        (trade_actions_path, "new trade actions\n"),
    ):
        path.write_text(text, encoding="utf-8")
    technical_facts_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts_cache.v1",
                "status": "skipped",
                "reason": "daily_premarket_non_blocking",
                "records": [],
            }
        ),
        encoding="utf-8",
    )
    decision_facts_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.decision_facts.v1",
                "status": "skipped",
                "reason": "daily_premarket_non_blocking",
                "records": [],
            }
        ),
        encoding="utf-8",
    )
    latest_technical_facts = latest_dir / "technical_facts.json"
    latest_decision_facts = latest_dir / "decision_facts.json"
    latest_technical_facts.write_text(
        '{"run_date":"2026-07-05","records":[{"symbol":"MSFT"}]}\n',
        encoding="utf-8",
    )
    latest_decision_facts.write_text(
        '{"run_date":"2026-07-05","records":[{"symbol":"MSFT"}]}\n',
        encoding="utf-8",
    )

    daily_premarket._promote_latest_set(
        advice_path=advice_path,
        actions_path=actions_path,
        plan_path=plan_path,
        trade_actions_path=trade_actions_path,
        technical_facts_path=technical_facts_path,
        decision_facts_path=decision_facts_path,
        data_dir=data_dir,
        market="US",
    )

    assert (latest_dir / "trading_advice.csv").read_text(encoding="utf-8") == (
        "new advice\n"
    )
    assert latest_technical_facts.read_text(encoding="utf-8") == (
        '{"run_date":"2026-07-05","records":[{"symbol":"MSFT"}]}\n'
    )
    assert latest_decision_facts.read_text(encoding="utf-8") == (
        '{"run_date":"2026-07-05","records":[{"symbol":"MSFT"}]}\n'
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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


def test_daily_runner_rolls_back_latest_set_when_technical_facts_promotion_fails(
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
    latest_dir = tmp_path / "data/latest/US"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (latest_dir / "trade_actions.csv").write_text(
        "old trade actions\n",
        encoding="utf-8",
    )
    (latest_dir / "technical_facts.json").write_text(
        '{"run_date":"2026-06-16","records":[]}\n',
        encoding="utf-8",
    )

    def fail_on_technical_facts_replace(source_path: Path, latest_path: Path) -> None:
        if latest_path.name == "technical_facts.json":
            raise RuntimeError("technical facts replace failed")
        source_path.replace(latest_path)

    monkeypatch.setattr(
        daily_premarket,
        "_replace_latest_path",
        fail_on_technical_facts_replace,
        raising=False,
    )

    runner = _daily_runner(
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
    assert (latest_dir / "trade_actions.csv").read_text(encoding="utf-8") == (
        "old trade actions\n"
    )
    assert (latest_dir / "technical_facts.json").read_text(encoding="utf-8") == (
        '{"run_date":"2026-06-16","records":[]}\n'
    )
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "technical facts replace failed" in status["error"]


def test_daily_runner_rolls_back_latest_set_when_decision_facts_promotion_fails(
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
    latest_dir = tmp_path / "data/latest/US"
    latest_dir.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")
    (latest_dir / "trading_advice.csv").write_text("old advice\n", encoding="utf-8")
    (latest_dir / "premarket_actions.csv").write_text(
        "old actions\n",
        encoding="utf-8",
    )
    (latest_dir / "trading_plan.csv").write_text("old plan\n", encoding="utf-8")
    (latest_dir / "trade_actions.csv").write_text(
        "old trade actions\n",
        encoding="utf-8",
    )
    (latest_dir / "technical_facts.json").write_text(
        '{"run_date":"2026-06-16","records":[]}\n',
        encoding="utf-8",
    )
    (latest_dir / "decision_facts.json").write_text(
        '{"run_date":"2026-06-16","records":[]}\n',
        encoding="utf-8",
    )

    def fail_on_decision_facts_replace(source_path: Path, latest_path: Path) -> None:
        if latest_path.name == "decision_facts.json":
            assert (latest_dir / "trading_advice.csv").read_text(
                encoding="utf-8"
            ) != "old advice\n"
            assert (latest_dir / "premarket_actions.csv").read_text(
                encoding="utf-8"
            ) != "old actions\n"
            assert (latest_dir / "trading_plan.csv").read_text(
                encoding="utf-8"
            ) != "old plan\n"
            assert (latest_dir / "trade_actions.csv").read_text(
                encoding="utf-8"
            ) != "old trade actions\n"
            assert (latest_dir / "technical_facts.json").read_text(
                encoding="utf-8"
            ) != '{"run_date":"2026-06-16","records":[]}\n'
            raise RuntimeError("decision facts replace failed")
        source_path.replace(latest_path)

    monkeypatch.setattr(
        daily_premarket,
        "_replace_latest_path",
        fail_on_decision_facts_replace,
        raising=False,
    )

    runner = _daily_runner(
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
    assert (latest_dir / "trade_actions.csv").read_text(encoding="utf-8") == (
        "old trade actions\n"
    )
    assert (latest_dir / "technical_facts.json").read_text(encoding="utf-8") == (
        '{"run_date":"2026-06-16","records":[]}\n'
    )
    assert (latest_dir / "decision_facts.json").read_text(encoding="utf-8") == (
        '{"run_date":"2026-06-16","records":[]}\n'
    )
    assert list(latest_dir.glob("*.backup")) == []
    assert list(latest_dir.glob(".*.tmp")) == []
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "decision facts replace failed" in status["error"]


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

    runner = _daily_runner(
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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
    runner = _daily_runner(
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

    runner = _daily_runner(
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

    result = _daily_runner(
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

    result = _daily_runner(
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

    runner = _daily_runner(
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

    result = _daily_runner(
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

    runner = _daily_runner(
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

    runner = _daily_runner(
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
    runner = _daily_runner(
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
    runner = _daily_runner(
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


def test_launchd_template_runs_persistent_trend_controller() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "ops/launchd/com.open-trader.trend-market-controller.plist.template"
    ).read_text(encoding="utf-8")

    assert "trend-market" in template
    assert "run" in template
    assert "OPEN_TRADER_CONFIG" in template
    assert "<key>RunAtLoad</key>\n  <true/>" in template
    assert "<key>KeepAlive</key>\n  <true/>" in template
    assert "<key>ThrottleInterval</key>\n  <integer>30</integer>" in template
    assert "StartCalendarInterval" not in template


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


def test_launchd_installer_renders_cn_controller(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--trend-only",
            "--market",
            "CN",
        ],
        text=True,
        capture_output=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    plists = _launchd_plists(result.stdout)
    assert len(plists) == 1
    _assert_trend_controller_job(
        plists[0],
        repo=repo,
        config=repo / "config/daily_premarket.env",
        market="CN",
    )


def test_launchd_installer_trend_all_renders_exactly_three_controllers(
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
                "OPEN_TRADER_TREND_EXECUTOR_HOST=wrong-host",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--trend-only",
            "--market",
            "all",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    plists = _launchd_plists(result.stdout)
    assert {item["Label"] for item in plists} == {
        "com.open-trader.trend-market-controller.cn",
        "com.open-trader.trend-market-controller.hk",
        "com.open-trader.trend-market-controller.us",
    }
    assert f"local host: {_local_hostname()}" in result.stdout
    assert f"configured executor host: {_local_hostname()}" in result.stdout
    assert "effective mode: execute" in result.stdout
    for payload, market in zip(plists, ["CN", "HK", "US"], strict=True):
        _assert_trend_controller_job(
            payload,
            repo=repo,
            config=repo / "config/daily_premarket.env",
            market=market,
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


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_launchd_installer_renders_single_trend_market_controller(
    market: str,
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
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run", "--trend-only", "--market", market,
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    plists = _launchd_plists(result.stdout)
    assert len(plists) == 1
    _assert_trend_controller_job(
        plists[0],
        repo=repo,
        config=repo / "config/daily_premarket.env",
        market=market,
    )


def test_launchd_installer_binds_shared_config_but_runs_installer_checkout(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "shared/config.env"
    config.parent.mkdir()
    configured_repo = tmp_path / "configured-repo"
    configured_python = tmp_path / "shared-python/bin/python"
    config.write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={configured_repo}",
                f"OPEN_TRADER_PYTHON={configured_python}",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--config",
            str(config),
            "--dry-run",
            "--trend-only",
            "--market",
            "US",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    payload = _launchd_plists(result.stdout)[0]
    _assert_trend_controller_job(
        payload,
        repo=repo,
        config=config,
        market="US",
        python=configured_python,
    )
    assert str(configured_repo) not in str(payload)


def test_launchd_installer_parses_single_quoted_values_like_runtime_dotenv(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "shared config #1/config.env"
    config.parent.mkdir()
    configured_repo = tmp_path / "configured repo #1"
    python = "bin/python #runner"
    config.write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO='{configured_repo}'",
                f"OPEN_TRADER_PYTHON='{python}'",
                "OPEN_TRADER_TREND_EXECUTOR_HOST='wrong host #old'",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST='{_local_hostname()}'",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--config",
            str(config),
            "--dry-run",
            "--trend-only",
            "--market",
            "US",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    payload = _launchd_plists(result.stdout)[0]
    _assert_trend_controller_job(
        payload,
        repo=repo,
        config=config,
        market="US",
        python=configured_repo / python,
    )
    assert f"configured executor host: {_local_hostname()}" in result.stdout


@pytest.mark.parametrize("executor_host", [None, "another-host"])
def test_launchd_installer_readonly_renders_no_trend_controller(
    tmp_path: Path,
    executor_host: str | None,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    lines = [f"OPEN_TRADER_REPO={repo}", "OPEN_TRADER_PYTHON=.venv/bin/python"]
    if executor_host is not None:
        lines.append(f"OPEN_TRADER_TREND_EXECUTOR_HOST={executor_host}")
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(lines), encoding="utf-8"
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--dry-run",
            "--trend-only",
            "--market",
            "all",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert _launchd_plists(result.stdout) == []
    assert "effective mode: readonly" in result.stdout
    assert "configured executor host: " in result.stdout


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
            "JP",
        ],
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_launchd_installer_removes_legacy_agent_before_installing_ordinary_jobs(
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


def test_launchd_installer_cn_controller_preserves_ordinary_legacy_agent(
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
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "CN",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert legacy.exists()
    assert "removed legacy launchd agent" not in result.stdout
    assert (agents / "com.open-trader.trend-market-controller.cn.plist").exists()
    assert not (agents / "com.open-trader.premarket.hk.plist").exists()
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


def test_launchd_installer_executor_migrates_only_requested_market(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label in _all_trend_labels():
        (agents / f"{label}.plist").write_text("old\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    launchctl_log = tmp_path / "launchctl.log"
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(launchctl_log),
        },
    )

    assert not (agents / "com.open-trader.trend-us-report.plist").exists()
    assert not (agents / "com.open-trader.trend-us-watch.plist").exists()
    assert (agents / "com.open-trader.trend-market-controller.us.plist").exists()
    for label in _all_trend_labels():
        if ".us" in label or "trend-us-" in label:
            continue
        assert (agents / f"{label}.plist").exists()
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    load_index = next(i for i, call in enumerate(calls) if call.startswith("load "))
    for label in ["com.open-trader.trend-us-report", "com.open-trader.trend-us-watch"]:
        bootout_index = next(i for i, call in enumerate(calls) if call.endswith(label))
        print_index = next(
            i
            for i, call in enumerate(calls)
            if call.startswith("print ") and call.endswith(label)
        )
        assert bootout_index < print_index < load_index
    controller_print = next(
        i
        for i, call in enumerate(calls)
        if call.startswith("print ")
        and call.endswith("com.open-trader.trend-market-controller.us")
    )
    assert controller_print < load_index
    assert not any("trend-hk-" in call or "controller.hk" in call for call in calls)
    assert not any("trend-a-share" in call or "controller.cn" in call for call in calls)


def test_launchd_installer_readonly_cleans_all_trend_automation(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label in _all_trend_labels():
        (agents / f"{label}.plist").write_text("old\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    launchctl_log = tmp_path / "launchctl.log"
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
                "OPEN_TRADER_TREND_EXECUTOR_HOST=another-host",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(launchctl_log),
        },
    )

    assert "effective mode: readonly" in result.stdout
    assert not any((agents / f"{label}.plist").exists() for label in _all_trend_labels())
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("load ") for call in calls)
    for label in _all_trend_labels():
        assert any(call.endswith(label) for call in calls)


def test_launchd_installer_refuses_load_while_legacy_label_is_present(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    launchctl_log = tmp_path / "launchctl.log"
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(launchctl_log),
            "OPEN_TRADER_PRINT_EXIT": "0",
        },
    )

    assert result.returncode == 1
    assert "legacy launchd job is still loaded" in result.stderr
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("load ") for call in calls)


def test_launchd_installer_rejects_orphan_process_for_selected_market(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    launchctl_log = tmp_path / "launchctl.log"
    pgrep_log = tmp_path / "pgrep.log"
    _write_launchd_executor_config(repo, _local_hostname())

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(launchctl_log),
            "OPEN_TRADER_PGREP_LOG": str(pgrep_log),
            "OPEN_TRADER_PGREP_MATCH": "watch-trend-market",
        },
    )

    assert result.returncode == 1
    assert "legacy trend process is still running for US" in result.stderr
    assert not any(
        call.startswith("load ")
        for call in launchctl_log.read_text(encoding="utf-8").splitlines()
    )
    patterns = pgrep_log.read_text(encoding="utf-8").splitlines()
    assert any("watch-trend-market" in pattern for pattern in patterns)
    assert all("--market[[:space:]]+HK" not in pattern for pattern in patterns)


def test_launchd_installer_stops_all_labels_before_orphan_process_fence(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    operations = tmp_path / "operations.log"
    _write_launchd_executor_config(repo, _local_hostname())

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "all",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_OPERATION_LOG": str(operations),
            "OPEN_TRADER_PGREP_MATCH": "--market[[:space:]]+HK",
        },
    )

    assert result.returncode == 1
    calls = operations.read_text(encoding="utf-8").splitlines()
    first_probe = next(i for i, call in enumerate(calls) if call.startswith("pgrep "))
    for market in ["cn", "hk", "us"]:
        print_index = next(
            i
            for i, call in enumerate(calls)
            if call.startswith("launchctl print ")
            and call.endswith(f"trend-market-controller.{market}")
        )
        assert print_index < first_probe
    assert not any(call.startswith("launchctl load ") for call in calls)


def test_launchd_installer_fences_every_selected_market_before_first_load(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    operations = tmp_path / "operations.log"
    _write_launchd_executor_config(repo, _local_hostname())

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "all",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_OPERATION_LOG": str(operations),
        },
    )

    assert result.returncode == 0, result.stderr
    calls = operations.read_text(encoding="utf-8").splitlines()
    probes = [i for i, call in enumerate(calls) if call.startswith("pgrep ")]
    first_load = next(
        i for i, call in enumerate(calls) if call.startswith("launchctl load ")
    )
    assert len(probes) == 6
    assert max(probes) < first_load


def test_launchd_installer_readonly_fails_when_orphan_process_remains(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    launchctl_log = tmp_path / "launchctl.log"
    _write_launchd_executor_config(repo, "another-host")

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(launchctl_log),
            "OPEN_TRADER_PGREP_MATCH": "trend-a-share-report",
        },
    )

    assert result.returncode == 1
    assert "legacy trend process is still running for CN" in result.stderr
    assert "readonly host: no trend controller installed" not in result.stdout
    assert not any(
        call.startswith("load ")
        for call in launchctl_log.read_text(encoding="utf-8").splitlines()
    )


def test_launchd_installer_does_not_match_other_market_legacy_process(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_launchctl_bin(tmp_path)
    pgrep_log = tmp_path / "pgrep.log"
    _write_launchd_executor_config(repo, _local_hostname())

    result = subprocess.run(
        [
            str(repo / "scripts/install_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "OPEN_TRADER_PGREP_LOG": str(pgrep_log),
            "OPEN_TRADER_PGREP_MATCH": "--market[[:space:]]+HK",
        },
    )

    assert result.returncode == 0, result.stderr
    patterns = pgrep_log.read_text(encoding="utf-8").splitlines()
    assert len(patterns) == 2
    assert all(pattern.startswith("-f ^") for pattern in patterns)
    assert all("--market[[:space:]]+US" in pattern for pattern in patterns)


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
    cn_report = agents / "com.open-trader.trend-a-share-report.plist"
    cn_watch = agents / "com.open-trader.trend-a-share-watch.plist"
    for path in [hk, us, legacy, cn_report, cn_watch]:
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
    assert cn_report.exists()
    assert cn_watch.exists()
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


def test_launchd_uninstaller_removes_only_cn_jobs(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    hk = agents / "com.open-trader.premarket.hk.plist"
    us = agents / "com.open-trader.premarket.us.plist"
    cn_report = agents / "com.open-trader.trend-a-share-report.plist"
    cn_watch = agents / "com.open-trader.trend-a-share-watch.plist"
    cn_controller = agents / "com.open-trader.trend-market-controller.cn.plist"
    for path in [hk, us, cn_report, cn_watch, cn_controller]:
        path.write_text("plist\n", encoding="utf-8")

    subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--market",
            "CN",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
        },
    )

    assert hk.exists()
    assert us.exists()
    assert not cn_report.exists()
    assert not cn_watch.exists()
    assert not cn_controller.exists()


def test_launchd_uninstaller_explicit_all_removes_cn_jobs(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    paths = [
        agents / "com.open-trader.premarket.hk.plist",
        agents / "com.open-trader.premarket.us.plist",
        agents / "com.open-trader.premarket.plist",
        *(agents / f"{label}.plist" for label in _all_trend_labels()),
    ]
    for path in paths:
        path.write_text("plist\n", encoding="utf-8")

    subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--market",
            "all",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
        },
    )

    assert not any(path.exists() for path in paths)


def test_launchd_uninstaller_trend_only_removes_only_requested_market(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label in _all_trend_labels():
        (agents / f"{label}.plist").write_text("plist\n", encoding="utf-8")

    subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
        },
    )

    for label in _all_trend_labels():
        exists = (agents / f"{label}.plist").exists()
        assert exists is not ("trend-us-" in label or ".us" in label)
    calls = (tmp_path / "launchctl.log").read_text(encoding="utf-8").splitlines()
    for label in [
        "com.open-trader.trend-market-controller.us",
        "com.open-trader.trend-us-report",
        "com.open-trader.trend-us-watch",
    ]:
        assert any(call.startswith("bootout ") and call.endswith(label) for call in calls)
        assert any(call.startswith("print ") and call.endswith(label) for call in calls)


@pytest.mark.parametrize(
    "present_label",
    [
        "com.open-trader.trend-market-controller.us",
        "com.open-trader.trend-us-report",
    ],
)
def test_launchd_uninstaller_preserves_selected_plist_when_label_remains_loaded(
    tmp_path: Path,
    present_label: str,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    selected = [
        "com.open-trader.trend-market-controller.us",
        "com.open-trader.trend-us-report",
        "com.open-trader.trend-us-watch",
    ]
    for label in selected:
        (agents / f"{label}.plist").write_text("plist\n", encoding="utf-8")
    log = tmp_path / "launchctl.log"

    result = subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(log),
            "OPEN_TRADER_PRESENT_LABEL": present_label,
        },
    )

    target = agents / f"{present_label}.plist"
    assert result.returncode == 1
    assert target.exists()
    assert f"launchd job is still loaded: {present_label}" in result.stderr
    assert f"removed launchd agent: {target}" not in result.stdout


def test_launchd_uninstaller_all_stops_after_residual_label(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label in _all_trend_labels():
        (agents / f"{label}.plist").write_text("plist\n", encoding="utf-8")
    present = "com.open-trader.trend-hk-watch"

    result = subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "all",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
            "OPEN_TRADER_LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
            "OPEN_TRADER_PRESENT_LABEL": present,
        },
    )

    assert result.returncode == 1
    assert (agents / f"{present}.plist").exists()
    assert (agents / "com.open-trader.trend-market-controller.us.plist").exists()


def test_launchd_uninstaller_accepts_failed_bootout_when_print_is_absent(
    tmp_path: Path,
) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    selected = [
        "com.open-trader.trend-market-controller.us",
        "com.open-trader.trend-us-report",
        "com.open-trader.trend-us-watch",
    ]
    for label in selected:
        (agents / f"{label}.plist").write_text("plist\n", encoding="utf-8")

    result = subprocess.run(
        [
            str(repo / "scripts/uninstall_daily_premarket_launchd.sh"),
            "--trend-only",
            "--market",
            "US",
        ],
        capture_output=True,
        encoding="utf-8",
        env={
            "HOME": str(home),
            "PATH": f"{_fake_launchctl_bin(tmp_path)}:/usr/bin:/bin",
            "OPEN_TRADER_BOOTOUT_EXIT": "5",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not any((agents / f"{label}.plist").exists() for label in selected)


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
            "JP",
        ],
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def _launchd_plists(plist_text: str) -> list[dict[str, object]]:
    documents: list[str] = []
    remaining = plist_text
    while "<?xml" in remaining:
        _, _, remaining = remaining.partition("<?xml")
        body, end, remaining = remaining.partition("</plist>")
        if not end:
            break
        documents.append(f"<?xml{body}</plist>")
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


def _assert_trend_controller_job(
    payload: dict[str, object],
    *,
    repo: Path,
    config: Path,
    market: str,
    python: Path | None = None,
) -> None:
    lower = market.lower()
    assert payload["Label"] == f"com.open-trader.trend-market-controller.{lower}"
    assert payload["WorkingDirectory"] == str(repo)
    assert payload["EnvironmentVariables"] == {"PYTHONPATH": f"{repo}/src"}
    assert payload["ProgramArguments"] == [
        str(python or repo / ".venv/bin/python"),
        "-m",
        "open_trader",
        "trend-market",
        "run",
        "--market",
        market,
        "--config",
        str(config),
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 30
    assert "StartCalendarInterval" not in payload
    assert payload["StandardOutPath"] == (
        f"{repo}/logs/daily_premarket/launchd-trend-controller-{lower}.out.log"
    )
    assert payload["StandardErrorPath"] == (
        f"{repo}/logs/daily_premarket/launchd-trend-controller-{lower}.err.log"
    )


def _local_hostname() -> str:
    return subprocess.run(
        ["hostname"], check=True, capture_output=True, encoding="utf-8"
    ).stdout.strip()


def _all_trend_labels() -> list[str]:
    return [
        "com.open-trader.trend-a-share-report",
        "com.open-trader.trend-a-share-watch",
        "com.open-trader.trend-hk-report",
        "com.open-trader.trend-hk-watch",
        "com.open-trader.trend-us-report",
        "com.open-trader.trend-us-watch",
        "com.open-trader.trend-market-controller.cn",
        "com.open-trader.trend-market-controller.hk",
        "com.open-trader.trend-market-controller.us",
    ]


def _write_launchd_executor_config(repo: Path, executor_host: str) -> None:
    (repo / "config/daily_premarket.env").write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={repo}",
                "OPEN_TRADER_PYTHON=.venv/bin/python",
                f"OPEN_TRADER_TREND_EXECUTOR_HOST={executor_host}",
            ]
        ),
        encoding="utf-8",
    )


def _fake_launchctl_bin(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/usr/bin/env bash
if [[ -n "${OPEN_TRADER_LAUNCHCTL_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$OPEN_TRADER_LAUNCHCTL_LOG"
fi
if [[ -n "${OPEN_TRADER_OPERATION_LOG:-}" ]]; then
  printf 'launchctl %s\\n' "$*" >> "$OPEN_TRADER_OPERATION_LOG"
fi
if [[ "${1:-}" == "print" ]]; then
  if [[ -n "${OPEN_TRADER_PRESENT_LABEL:-}" && "$*" == *"$OPEN_TRADER_PRESENT_LABEL" ]]; then
    exit 0
  fi
  exit "${OPEN_TRADER_PRINT_EXIT:-1}"
fi
if [[ "${1:-}" == "bootout" ]]; then
  exit "${OPEN_TRADER_BOOTOUT_EXIT:-0}"
fi
exit 0
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    pgrep = fake_bin / "pgrep"
    pgrep.write_text(
        """#!/usr/bin/env bash
if [[ -n "${OPEN_TRADER_PGREP_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$OPEN_TRADER_PGREP_LOG"
fi
if [[ -n "${OPEN_TRADER_OPERATION_LOG:-}" ]]; then
  printf 'pgrep %s\\n' "$*" >> "$OPEN_TRADER_OPERATION_LOG"
fi
pattern="${*: -1}"
if [[ -n "${OPEN_TRADER_PGREP_MATCH:-}" && "$pattern" == *"$OPEN_TRADER_PGREP_MATCH"* ]]; then
  echo "4242"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    pgrep.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
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
    for name in [
        "com.open-trader.trend-market-controller.plist.template",
    ]:
        source = source_root / "ops/launchd" / name
        shutil.copy2(source, repo / "ops/launchd" / name)
    shutil.copy2(
        source_root / "scripts/install_daily_premarket_launchd.sh",
        repo / "scripts/install_daily_premarket_launchd.sh",
    )
    shutil.copy2(
        source_root / "scripts/uninstall_daily_premarket_launchd.sh",
        repo / "scripts/uninstall_daily_premarket_launchd.sh",
    )
    return repo
