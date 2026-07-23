from __future__ import annotations

import json
import socket
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import open_trader.cli as cli
from open_trader.daily_premarket import DailyPremarketConfig


def _config(tmp_path: Path, *, executor: str = "executor") -> DailyPremarketConfig:
    shared = tmp_path / "shared"
    return DailyPremarketConfig(
        repo=tmp_path / "configured-repo",
        python=tmp_path / "configured-python",
        timezone="Asia/Shanghai",
        deadline="09:00",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=shared / "data",
        reports_dir=shared / "reports",
        logs_dir=shared / "logs",
        portfolio=shared / "data/latest/portfolio.csv",
        trend_executor_host=executor,
    )


def test_trend_market_parser_exposes_run_status_and_resolve() -> None:
    parser = cli.build_parser()

    run = parser.parse_args([
        "trend-market", "run", "--market", "US", "--revision",
        "--config", "config/executor.env",
    ])
    status = parser.parse_args([
        "trend-market", "status", "--market", "HK",
        "--config", "config/status.env",
    ])
    resolve = parser.parse_args([
        "trend-market", "resolve", "--market", "CN",
        "--execution-date", "2026-07-20", "--symbol", "600001",
        "--side", "buy", "--resolution", "confirm-submitted",
        "--actor", "ray", "--reason", "checked Futu history",
        "--futu-order-id", "SIM-42", "--config", "config/resolve.env",
    ])

    assert (run.trend_market_command, run.market, run.revision) == (
        "run", "US", True,
    )
    assert run.config == Path("config/executor.env")
    assert (status.trend_market_command, status.market) == ("status", "HK")
    assert status.config == Path("config/status.env")
    assert resolve.trend_market_command == "resolve"
    assert vars(resolve) | {
        "market": "CN",
        "execution_date": "2026-07-20",
        "symbol": "600001",
        "side": "buy",
        "resolution": "confirm-submitted",
        "actor": "ray",
        "reason": "checked Futu history",
        "futu_order_id": "SIM-42",
    } == vars(resolve)


@pytest.mark.parametrize(
    "argv",
    [
        ["trend-a-share-report"],
        ["watch-trend-a-share"],
        ["trend-market-report", "--market", "US"],
        ["watch-trend-market", "--market", "HK"],
        ["trend-review", "open", "--market", "CN", "--date", "2026-07-20"],
        ["trend-review", "close", "--market", "US", "--date", "2026-07-20"],
    ],
)
def test_removed_trend_operational_commands_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(argv)

    assert exc_info.value.code == 2


def test_trend_market_run_routes_checkout_and_preserves_shared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    checkout = tmp_path / "accepted-checkout"
    checkout.mkdir()
    captured: dict[str, object] = {}
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)

    def run(loaded: DailyPremarketConfig, market: str, **kwargs: object) -> object:
        captured.update(config=loaded, market=market, **kwargs)
        return {"status": "running", "market": market}

    monkeypatch.setattr(cli, "run_trend_market_controller", run, raising=False)

    assert cli.main([
        "trend-market", "run", "--market", "US", "--revision",
        "--config", "config/daily.env",
    ]) == 0

    loaded = captured["config"]
    assert isinstance(loaded, DailyPremarketConfig)
    assert loaded.repo == checkout.resolve()
    assert loaded.python == Path(sys.executable).resolve()
    assert loaded.data_dir == config.data_dir
    assert loaded.reports_dir == config.reports_dir
    assert loaded.logs_dir == config.logs_dir
    assert loaded.portfolio == config.portfolio
    assert captured | {"market": "US", "revision": True} == captured
    assert json.loads(capsys.readouterr().out) == {
        "status": "running", "market": "US",
    }


def test_trend_market_status_routes_without_broker_or_notifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        cli,
        "load_trend_market_status",
        lambda loaded, market: calls.append((loaded, market)) or {
            "effective_mode": "execute", "market": market,
        },
        raising=False,
    )
    monkeypatch.setattr(
        cli, "FutuQuoteClient", lambda **_kwargs: pytest.fail("broker constructed")
    )
    monkeypatch.setattr(
        cli, "build_notifier", lambda _config: pytest.fail("notifier constructed")
    )

    assert cli.main(["trend-market", "status", "--market", "HK"]) == 0

    assert calls == [(config, "HK")]
    assert json.loads(capsys.readouterr().out) == {
        "effective_mode": "execute", "market": "HK",
    }


def test_trend_market_resolve_requires_executor_and_routes_exact_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    artifact = tmp_path / "resolution.json"
    calls: list[dict[str, object]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime(2026, 7, 21, 8, 9, 10, tzinfo=tz)

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(cli, "datetime", FixedDatetime)
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        cli,
        "resolve_trend_action",
        lambda data_dir, **kwargs: calls.append(
            {"data_dir": data_dir, **kwargs}
        ) or artifact,
        raising=False,
    )

    assert cli.main([
        "trend-market", "resolve", "--market", "CN",
        "--execution-date", "2026-07-20", "--symbol", "600001",
        "--side", "sell", "--resolution", "confirm-submitted",
        "--actor", "ray", "--reason", "confirmed in Futu",
        "--futu-order-id", "SIM-42",
    ]) == 0

    assert calls == [{
        "data_dir": config.data_dir,
        "market": "CN",
        "execution_date": "2026-07-20",
        "symbol": "600001",
        "side": "sell",
        "resolution": "confirm-submitted",
        "actor": "ray",
        "reason": "confirmed in Futu",
        "resolved_at": "2026-07-21T08:09:10+08:00",
        "futu_order_id": "SIM-42",
    }]
    assert json.loads(capsys.readouterr().out) == {
        "status": "resolved",
        "market": "CN",
        "execution_date": "2026-07-20",
        "symbol": "600001",
        "side": "sell",
        "resolution": "confirm-submitted",
        "artifact_path": str(artifact),
    }


@pytest.mark.parametrize(
    ("resolution", "order_id", "message"),
    [
        ("confirm-submitted", None, "requires --futu-order-id"),
        ("authorize-retry", "SIM-42", "does not accept --futu-order-id"),
        ("abandon", "SIM-42", "does not accept --futu-order-id"),
    ],
)
def test_trend_market_resolve_validates_conditional_futu_order_id(
    resolution: str,
    order_id: str | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: _config(tmp_path))
    monkeypatch.setattr(
        cli, "resolve_trend_action", lambda *_args, **_kwargs: calls.append(object()),
        raising=False,
    )
    argv = [
        "trend-market", "resolve", "--market", "CN",
        "--execution-date", "2026-07-20", "--symbol", "600001",
        "--side", "buy", "--resolution", resolution,
        "--actor", "ray", "--reason", "checked",
    ]
    if order_id is not None:
        argv.extend(["--futu-order-id", order_id])

    assert cli.main(argv) == 2

    assert message in capsys.readouterr().err
    assert calls == []


def test_readonly_run_and_resolve_have_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(socket, "gethostname", lambda: "readonly-copy")
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        cli, "run_trend_market_controller", lambda *_args, **_kwargs: calls.append("run"),
        raising=False,
    )
    monkeypatch.setattr(
        cli, "resolve_trend_action", lambda *_args, **_kwargs: calls.append("resolve"),
        raising=False,
    )

    assert cli.main(["trend-market", "run", "--market", "US"]) == 2
    first_error = capsys.readouterr().err
    assert cli.main([
        "trend-market", "resolve", "--market", "US",
        "--execution-date", "2026-07-20", "--symbol", "TRV",
        "--side", "buy", "--resolution", "abandon",
        "--actor", "ray", "--reason", "manual decision",
    ]) == 2
    second_error = capsys.readouterr().err

    assert "readonly" in first_error
    assert "readonly" in second_error
    assert calls == []


def test_readonly_status_returns_reason_without_mutating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "readonly-copy")
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)

    assert cli.main(["trend-market", "status", "--market", "US"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_mode"] == "readonly"
    assert "does not match" in payload["blocker"]
    assert not config.data_dir.exists()


def test_trend_market_runtime_error_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: _config(tmp_path))
    monkeypatch.setattr(
        cli,
        "run_trend_market_controller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("busy")),
        raising=False,
    )

    assert cli.main(["trend-market", "run", "--market", "CN"]) == 1
    assert "busy" in capsys.readouterr().err
