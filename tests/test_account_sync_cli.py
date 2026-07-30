from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import open_trader.account_sync_controller as controller_module
import open_trader.cli as cli
import open_trader.dashboard_quotes as quotes_module
from open_trader.account_sync_controller import AccountSyncControllerConfig
from open_trader.account_sync_state import empty_account_sync_state
from open_trader.cli import build_parser


def test_parser_exposes_only_account_sync_commands() -> None:
    parser = build_parser()

    controller = parser.parse_args(["account-sync-controller", "--once"])
    status = parser.parse_args(["account-sync-status", "--json"])

    assert controller.config == Path("config/daily_premarket.env")
    assert controller.data_dir == Path("data")
    assert controller.reports_dir == Path("reports")
    assert controller.portfolio == Path("data/latest/portfolio.csv")
    assert controller.tiger_config_dir == Path("~/.tigeropen/")
    assert controller.account_interval_seconds == 60.0
    assert controller.quote_interval_seconds == 5.0
    assert controller.once is True
    assert status.data_dir == Path("data")
    assert status.json is True

    for command in (
        "check-futu-quotes",
        "check-futu-account",
        "sync-futu-portfolio",
        "check-tiger-account",
        "sync-tiger-portfolio",
    ):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([command])
        assert exc_info.value.code == 2


def test_controller_uses_only_futu_connection_env_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "daily_premarket.env"
    config_path.write_text(
        "OPEN_TRADER_FUTU_HOST=10.0.0.7\n"
        "OPEN_TRADER_FUTU_PORT=12345\n"
        "OPENAI_API_KEY=must-not-be-loaded\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run(config: AccountSyncControllerConfig, *, once: bool) -> int:
        captured.update(config=config, once=once)
        return 0

    monkeypatch.setattr(cli, "run_account_sync_controller", fake_run)
    monkeypatch.setattr(
        cli,
        "load_env_config",
        lambda *_args, **_kwargs: pytest.fail("controller must not load notifier/LLM config"),
    )

    assert (
        cli.main(
            [
                "account-sync-controller",
                "--config",
                str(config_path),
                "--data-dir",
                str(tmp_path / "data"),
                "--reports-dir",
                str(tmp_path / "reports"),
                "--portfolio",
                str(tmp_path / "data/latest/portfolio.csv"),
                "--tiger-config-dir",
                str(tmp_path / ".tigeropen"),
                "--account-interval-seconds",
                "61",
                "--quote-interval-seconds",
                "6",
                "--once",
            ]
        )
        == 0
    )

    config = captured["config"]
    assert isinstance(config, AccountSyncControllerConfig)
    assert config.futu_host == "10.0.0.7"
    assert config.futu_port == 12345
    assert config.account_interval_seconds == 61.0
    assert config.quote_interval_seconds == 6.0
    assert config.tiger_account is None
    assert captured["once"] is True


def test_status_reads_only_published_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    data_dir = tmp_path / "data"
    state = empty_account_sync_state()
    state["generation"] = now
    brokers = state["brokers"]
    assert isinstance(brokers, dict)
    for broker in brokers.values():
        assert isinstance(broker, dict)
        broker.update(
            status="ok",
            attempted_at=now,
            last_success_at=now,
            data_as_of=now,
            period="2026-07",
        )
    _write_json(data_dir / "latest/account_sync_state.json", state)
    _write_json(
        data_dir / "latest/quotes.json",
        {"status": "ok", "last_success_at": now, "stale": False, "quotes": {}},
    )
    _write_json(
        data_dir / "account_sync/controller_status.json",
        {
            "schema_version": "open_trader.account_sync.controller.v1",
            "pid": 4321,
            "started_at": now,
            "working_directory": "/repo",
            "git_sha": "abc123",
            "heartbeat_at": now,
            "phase": "idle",
            "account_loop": {"status": "ok"},
            "quote_loop": {"status": "ok"},
            "blocker": None,
        },
    )

    def fail(*_args: object, **_kwargs: object) -> object:
        pytest.fail("account-sync-status must not construct a live client")

    monkeypatch.setattr(controller_module, "FutuAccountClient", fail)
    monkeypatch.setattr(controller_module, "TigerAccountClient", fail)
    monkeypatch.setattr(quotes_module, "FutuQuoteClient", fail)

    assert cli.main(["account-sync-status", "--data-dir", str(data_dir), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "status",
        "label",
        "reason",
        "portfolio_generation",
        "controller",
        "quotes",
        "brokers",
    }
    assert payload["controller"] == {
        "status": "ok",
        "pid": 4321,
        "git_sha": "abc123",
        "heartbeat_at": now,
    }
    assert payload["quotes"] == {"status": "ok"}
    assert {broker: value["status"] for broker, value in payload["brokers"].items()} == {
        "futu": "ok",
        "tiger": "ok",
        "phillips": "ok",
        "eastmoney": "ok",
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
