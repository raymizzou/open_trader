from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader import cli


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
        market: str, process_version: str, pool_ids: tuple[int, ...]
    ) -> dict[str, object]:
        strategy_calls.append((market, process_version, pool_ids))
        return {
            "strategy_id": "trend_animals_warm_to_hot/CN/v4",
            "strategy_version": "v4",
        }

    monkeypatch.setattr(cli, "load_futu_simulate_trend_account", load_account)
    monkeypatch.setattr(cli, "live_trend_strategy_snapshot", strategy_snapshot)

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
    state = json.loads(
        (data_dir / "trend_drawdown" / "state.json").read_text(encoding="utf-8")
    )
    assert state["audit_events"][0]["event_id"] == "unlock-cn-v4-001"
    assert state["audit_events"][0]["occurred_at"] == "2026-07-20T09:30:00+08:00"

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
    monkeypatch.setattr(cli, "build_notifier", lambda config: cli.NullNotifier())
    monkeypatch.setattr(cli, "require_trend_review_config", lambda cfg, market: 101)
    monkeypatch.setattr(cli, "_process_version", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli,
        "_drawdown_preflight_now",
        lambda: datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
    )

    def load_account(**kwargs: object) -> object:
        account_calls.append((str(kwargs["market"]), str(kwargs["expected_date"])))
        return SimpleNamespace(
            net_value=Decimal({"CN": "100", "HK": "200", "US": "300"}[kwargs["market"]])
        )

    monkeypatch.setattr(cli, "load_futu_simulate_trend_account", load_account)
    monkeypatch.setattr(
        cli,
        "live_trend_strategy_snapshot",
        lambda market, process_version, pool_ids: {
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
    assert account_calls == [
        ("CN", "2026-07-17"),
        ("HK", "2026-07-17"),
        ("US", "2026-07-17"),
    ]
