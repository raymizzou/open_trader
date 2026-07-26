from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader import cli
import open_trader.a_share_trend as trend_module
from open_trader.strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    observe_strategy_equity,
)


def test_trend_drawdown_unlock_cli_writes_and_prints_audited_rebase(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "daily.env"
    config = SimpleNamespace(
        data_dir=data_dir,
        futu_host="127.0.0.1",
        futu_port=11111,
        repo=tmp_path,
        timezone="Asia/Shanghai",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )
    account_calls: list[dict[str, object]] = []
    strategy_calls: list[tuple[str, str, tuple[int, ...]]] = []
    strategy_execution_dates: list[str | None] = []
    clock = ["2026-07-20T09:30:00+08:00"]
    account_equity = ["95000"]

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "require_trend_review_config", lambda cfg, market: 101)
    monkeypatch.setattr(cli, "_process_version", lambda repo: "accepted-sha")
    monkeypatch.setattr(
        cli,
        "_drawdown_unlock_now",
        lambda timezone: datetime.fromisoformat(clock[0]),
    )

    def load_account(**kwargs: object) -> object:
        account_calls.append(kwargs)
        return SimpleNamespace(net_value=Decimal(account_equity[0]))

    def strategy_snapshot(
        market: str, process_version: str, pool_ids: tuple[int, ...], **kwargs: object
    ) -> dict[str, object]:
        strategy_calls.append((market, process_version, pool_ids))
        strategy_execution_dates.append(kwargs.get("execution_date"))
        return {
            "strategy_id": "trend_animals_warm_to_hot/CN/v4",
            "strategy_version": "v4",
        }

    monkeypatch.setattr(cli, "load_futu_simulate_trend_account", load_account)
    monkeypatch.setattr(cli, "live_trend_strategy_snapshot", strategy_snapshot)

    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v4",
        strategy_version="v4",
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100000"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    observe_strategy_equity(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v4",
        strategy_version="v4",
        current_equity=Decimal("94000"),
        observed_at="2026-07-20T09:00:00+08:00",
    )

    argv = [
        "trend-drawdown-unlock",
        "--config", str(config_path),
        "--market", "CN",
        "--event-id", "unlock-cn-v4-001",
        "--actor", "ray",
    ]
    result = cli.main(argv)

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["entry_allowed"] is True
    assert output["high_water_mark"] == "95000"
    assert account_calls == [{
        "host": "127.0.0.1",
        "port": 11111,
        "simulate_acc_id": 101,
        "market": "CN",
        "expected_date": "2026-07-20",
    }]
    assert strategy_calls == [
        ("CN", "accepted-sha", (622466, 697199)),
    ]
    assert strategy_execution_dates == ["2026-07-20"]
    state = json.loads(
        (data_dir / "trend_drawdown" / "state.json").read_text(encoding="utf-8")
    )
    unlock_event = next(
        event for event in state["audit_events"]
        if event["event_type"] == "manual_unlock"
    )
    assert unlock_event["event_id"] == "unlock-cn-v4-001"
    assert unlock_event["occurred_at"] == "2026-07-20T09:30:00+08:00"

    state_path = data_dir / "trend_drawdown" / "state.json"
    state_before_retry = state_path.read_bytes()
    clock[0] = "2026-07-21T09:30:00+08:00"
    assert cli.main(argv) == 0
    retry_output = json.loads(capsys.readouterr().out)
    assert retry_output["high_water_mark"] == "95000"
    assert state_path.read_bytes() == state_before_retry
    assert account_calls[-1]["expected_date"] == "2026-07-21"


@pytest.mark.parametrize(
    "unsafe_override",
    [
        ("--current-equity", "95000"),
        ("--strategy-id", "operator-selected"),
        ("--strategy-version", "v1"),
        ("--occurred-at", "2020-01-01T00:00:00+08:00"),
    ],
)
def test_trend_drawdown_unlock_cli_rejects_operator_state_overrides(
    unsafe_override: tuple[str, str],
) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([
            "trend-drawdown-unlock",
            "--market", "CN",
            "--event-id", "unlock-cn-v4-001",
            "--actor", "ray",
            *unsafe_override,
        ])


def test_trend_drawdown_preflight_cli_bootstraps_all_markets_independently(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1",
        futu_port=11111,
        timezone="Asia/Shanghai",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )
    account_calls: list[tuple[str, str]] = []

    class Quote:
        closed = False

        def __init__(self, **_: object) -> None:
            pass

        def get_trading_days(
            self, *, market: str, start: str, end: str
        ) -> list[str]:
            assert start < "2026-07-17" < end
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "FutuQuoteClient", Quote)
    def unexpected_build_notifier(config: object) -> object:
        raise AssertionError("acceptance must not build an external notifier")

    monkeypatch.setattr(cli, "build_notifier", unexpected_build_notifier)
    monkeypatch.setattr(cli, "require_trend_review_config", lambda cfg, market: 101)
    monkeypatch.setattr(cli, "_process_version", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli,
        "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
    )
    for market, directory, equity in (
        ("CN", "trend_a_share", "100"),
        ("HK", "trend_hk_phillips", "200"),
        ("US", "trend_us_tiger", "300"),
    ):
        path = config.reports_dir / directory / "2026-07-17.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "metadata": {"market": market},
            "strategy_snapshot": {
                "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
                "strategy_version": "v4",
            },
            "account": {"source_date": "2026-07-17", "net_value": equity},
            "drawdown_summary": {"state_status": "missing"},
        }), encoding="utf-8")

    def load_account(**kwargs: object) -> object:
        account_calls.append((str(kwargs["market"]), str(kwargs["expected_date"])))
        return SimpleNamespace(
            net_value=Decimal({"CN": "100", "HK": "200", "US": "300"}[kwargs["market"]])
        )

    monkeypatch.setattr(cli, "load_futu_simulate_trend_account", load_account)
    monkeypatch.setattr(
        cli,
        "live_trend_strategy_snapshot",
        lambda market, process_version, pool_ids, **kwargs: {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
            "strategy_version": "v4",
            "parameters": {"market": market},
        },
    )

    result = cli.main([
        "trend-drawdown-preflight",
        "--config", str(tmp_path / "daily.env"),
        "--repo", str(tmp_path),
        "--actor", "acceptance",
    ])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert [item["status"] for item in output["markets"]] == [
        "bootstrapped", "bootstrapped", "bootstrapped"
    ]
    assert account_calls == []


@pytest.mark.parametrize(
    ("market", "source_date", "entry_date", "expected_version"),
    [
        ("CN", "2026-07-26", "2026-07-27", "v10"),
        ("US", "2026-07-23", "2026-07-24", "v5"),
        ("US", "2026-07-26", "2026-07-27", "v7"),
        ("HK", "2026-07-26", "2026-07-27", "v7"),
    ],
)
def test_trend_drawdown_preflight_uses_entry_date_for_market_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    source_date: str,
    entry_date: str,
    expected_version: str,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1",
        futu_port=11111,
        timezone="Asia/Shanghai",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )
    calls: list[tuple[str, str | None, str]] = []
    market_inputs: dict[str, object] = {}

    class Quote:
        def __init__(self, **_: object) -> None:
            pass

        def get_trading_days(self, **_: object) -> list[str]:
            return []

        def close(self) -> None:
            pass

    def strategy_snapshot(
        current_market: str,
        process_version: str,
        pool_ids: tuple[int, ...],
        *,
        execution_date: str | None = None,
    ) -> dict[str, object]:
        snapshot = trend_module.live_trend_strategy_snapshot(
            current_market,
            process_version,
            pool_ids,
            execution_date=execution_date,
        )
        calls.append((current_market, execution_date, str(snapshot["strategy_version"])))
        return snapshot

    def preflight_dates(
        current_market: str,
        *,
        now: datetime,
        trading_days: list[str],
    ) -> tuple[str, str]:
        if current_market == market:
            return source_date, entry_date
        return "2026-07-23", "2026-07-24"

    def run_preflight(**kwargs: object) -> dict[str, object]:
        market_inputs.update(kwargs["market_inputs"])
        return {"status": "ready"}

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "FutuQuoteClient", Quote)
    monkeypatch.setattr(cli, "build_notifier", lambda config: cli.NullNotifier())
    monkeypatch.setattr(cli, "_process_version", lambda repo: "sha")
    monkeypatch.setattr(
        cli,
        "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-24T08:00:00+08:00"),
    )
    monkeypatch.setattr(cli, "market_preflight_dates", preflight_dates)
    monkeypatch.setattr(cli, "live_trend_strategy_snapshot", strategy_snapshot)
    monkeypatch.setattr(cli, "run_drawdown_preflight", run_preflight)

    assert cli.main([
        "trend-drawdown-preflight",
        "--config", str(tmp_path / "daily.env"),
        "--repo", str(tmp_path),
        "--actor", "pytest",
    ]) == 0
    assert (market, entry_date, expected_version) in calls
    assert (
        market_inputs[market].strategy_snapshot["strategy_version"]
        == expected_version
    )
    assert market_inputs[market].baseline_equity is None


def test_trend_drawdown_preflight_skips_missing_frozen_baseline_without_live_nav(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1",
        futu_port=11111,
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )
    account_calls: list[str] = []

    class Quote:
        def __init__(self, **_: object) -> None:
            pass

        def get_trading_days(self, **_: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "FutuQuoteClient", Quote)
    monkeypatch.setattr(cli, "build_notifier", lambda config: cli.NullNotifier())
    monkeypatch.setattr(cli, "require_trend_review_config", lambda cfg, market: 101)
    monkeypatch.setattr(cli, "_process_version", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli, "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
    )
    monkeypatch.setattr(
        cli, "live_trend_strategy_snapshot",
        lambda market, process_version, pool_ids, **kwargs: {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
            "strategy_version": "v4",
            "parameters": {"market": market},
        },
    )
    monkeypatch.setattr(
        cli,
        "load_futu_simulate_trend_account",
        lambda **kwargs: account_calls.append(str(kwargs["market"])),
    )

    result = cli.main([
        "trend-drawdown-preflight", "--config", str(tmp_path / "daily.env"),
        "--repo", str(tmp_path),
    ])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert [item["status"] for item in output["markets"]] == [
        "skipped", "skipped", "skipped"
    ]
    assert {item["reason"] for item in output["markets"]} == {
        "baseline_missing"
    }
    assert account_calls == []
    assert not (config.data_dir / "trend_drawdown/state.json").exists()


def test_trend_drawdown_preflight_blocks_when_futu_calendar_is_unavailable(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1",
        futu_port=11111,
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )

    class UnavailableQuote:
        def __init__(self, **_: object) -> None:
            pass

        def get_trading_days(self, **_: object) -> list[str]:
            raise RuntimeError("Futu trading calendar unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "FutuQuoteClient", UnavailableQuote)
    monkeypatch.setattr(cli, "build_notifier", lambda config: cli.NullNotifier())
    monkeypatch.setattr(cli, "_process_version", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli,
        "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
    )
    monkeypatch.setattr(
        cli,
        "live_trend_strategy_snapshot",
        lambda market, process_version, pool_ids, **kwargs: {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
            "strategy_version": "v4",
            "parameters": {"market": market},
        },
    )

    result = cli.main([
        "trend-drawdown-preflight",
        "--config", str(tmp_path / "daily.env"),
        "--repo", str(tmp_path),
    ])

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "unavailable"
    assert [item["status"] for item in output["markets"]] == [
        "unavailable", "unavailable", "unavailable"
    ]
    assert all(
        "Futu trading calendar unavailable" in item["error"]
        for item in output["markets"]
    )


def test_trend_drawdown_preflight_reuses_existing_audited_state_without_new_baseline(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1",
        futu_port=11111,
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
    )

    class Quote:
        def __init__(self, **_: object) -> None:
            pass

        def get_trading_days(self, **_: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run: config)
    monkeypatch.setattr(cli, "FutuQuoteClient", Quote)
    built_notifiers: list[object] = []

    def build_recording_notifier(current_config: object) -> cli.NullNotifier:
        built_notifiers.append(current_config)
        return cli.NullNotifier()

    monkeypatch.setattr(cli, "build_notifier", build_recording_notifier)
    monkeypatch.setattr(cli, "_process_version", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli,
        "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
    )
    monkeypatch.setattr(
        cli,
        "live_trend_strategy_snapshot",
        lambda market, process_version, pool_ids, **kwargs: {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
            "strategy_version": "v4",
            "parameters": {"market": market},
        },
    )
    for market, equity in (("CN", "100"), ("HK", "200"), ("US", "300")):
        automatic_bootstrap_strategy_drawdown(
            config.data_dir,
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/v4",
            strategy_version="v4",
            parameters={"market": market},
            baseline_equity=Decimal(equity),
            source_date="2026-07-17",
            accepted_git_sha="a" * 40,
            actor="deployment",
            occurred_at="2026-07-18T08:00:00+08:00",
            reason="first_activation",
            entry_eligible_from="2026-07-20",
        )

    result = cli.main([
        "trend-drawdown-preflight",
        "--config", str(tmp_path / "daily.env"),
        "--repo", str(tmp_path),
    ])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert [item["status"] for item in output["markets"]] == [
        "ready", "ready", "ready"
    ]
    assert built_notifiers == [config]
