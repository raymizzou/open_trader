from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from open_trader.drawdown_preflight import (
    DrawdownMarketInput,
    frozen_missing_baseline,
    market_preflight_dates,
    run_drawdown_preflight,
)
from open_trader.notifications import NullNotifier
from open_trader.strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    observe_strategy_equity,
)


def market_input(market: str, *, error: str = "") -> DrawdownMarketInput:
    return DrawdownMarketInput(
        market=market,
        strategy_snapshot={
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
            "strategy_version": "v4",
            "parameters": {"drawdown_limit": "0.05", "market": market},
        },
        baseline_equity=Decimal({"CN": "100", "HK": "200", "US": "300"}[market]),
        source_date="2026-07-17",
        entry_eligible_from="2026-07-20",
        error=error,
    )


def run_preflight(root: Path, inputs: dict[str, DrawdownMarketInput]) -> dict[str, object]:
    return run_drawdown_preflight(
        data_dir=root / "data",
        reports_dir=root / "reports",
        market_inputs=inputs,
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=NullNotifier(),
    )


def write_report(root: Path, market: str, state_status: str) -> Path:
    directory = {
        "CN": "trend_a_share",
        "HK": "trend_hk_phillips",
        "US": "trend_us_tiger",
    }[market]
    path = root / "reports" / directory / "2026-07-17.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "metadata": {"market": market},
                "drawdown_summary": {"state_status": state_status},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_first_activation_bootstraps_markets_and_is_idempotent(tmp_path: Path) -> None:
    inputs = {market: market_input(market) for market in ("CN", "HK", "US")}

    first = run_preflight(tmp_path, inputs)
    state_path = tmp_path / "data/trend_drawdown/state.json"
    before = state_path.read_bytes()
    second = run_preflight(tmp_path, inputs)

    assert first["status"] == "ready"
    assert [item["status"] for item in first["markets"]] == [
        "bootstrapped", "bootstrapped", "bootstrapped"
    ]
    assert [item["status"] for item in second["markets"]] == [
        "ready", "ready", "ready"
    ]
    assert state_path.read_bytes() == before
    state = json.loads(before)
    assert {event["reason"] for event in state["audit_events"]} == {
        "first_activation"
    }


def test_historical_ok_report_prevents_rebuilding_missing_state(tmp_path: Path) -> None:
    report = write_report(tmp_path, "US", "ok")
    report_before = report.read_bytes()

    result = run_preflight(tmp_path, {"US": market_input("US")})

    assert result["status"] == "failed"
    assert result["markets"][0]["status"] == "failed"
    assert "snapshot" in result["markets"][0]["error"]
    assert not (tmp_path / "data/trend_drawdown/state.json").exists()
    assert report.read_bytes() == report_before


def test_state_loss_recovers_exact_snapshot_instead_of_rebasing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    item = market_input("HK")
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="HK",
        strategy_id=item.strategy_snapshot["strategy_id"],
        strategy_version="v4",
        parameters=item.strategy_snapshot["parameters"],
        baseline_equity=Decimal("200"),
        source_date=item.source_date,
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-18T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-18",
    )
    observe_strategy_equity(
        data_dir,
        market="HK",
        strategy_id=item.strategy_snapshot["strategy_id"],
        strategy_version="v4",
        current_equity=Decimal("180"),
        observed_at="2026-07-19T16:00:00+08:00",
    )
    state_path = data_dir / "trend_drawdown/state.json"
    expected = state_path.read_bytes()
    state_path.unlink()
    write_report(tmp_path, "HK", "ok")

    result = run_preflight(tmp_path, {"HK": item})

    assert result["status"] == "ready"
    assert result["markets"][0]["status"] == "recovered"
    assert result["markets"][0]["entry_allowed"] is False
    assert state_path.read_bytes() == expected


def test_unavailable_market_does_not_block_other_market_bootstrap(tmp_path: Path) -> None:
    result = run_preflight(
        tmp_path,
        {
            "CN": market_input("CN", error="Futu account unavailable"),
            "US": market_input("US"),
        },
    )

    assert result["status"] == "unavailable"
    assert [item["status"] for item in result["markets"]] == [
        "unavailable", "bootstrapped"
    ]
    state = json.loads(
        (tmp_path / "data/trend_drawdown/state.json").read_text(encoding="utf-8")
    )
    assert [record["market"] for record in state["records"]] == ["US"]


def test_market_preflight_dates_move_late_bootstrap_to_next_session() -> None:
    assert market_preflight_dates(
        "CN",
        now=datetime.fromisoformat("2026-07-20T09:31:00+08:00"),
        trading_days=["2026-07-17", "2026-07-20", "2026-07-21"],
    ) == ("2026-07-17", "2026-07-21")
    assert market_preflight_dates(
        "US",
        now=datetime.fromisoformat("2026-07-20T08:00:00+08:00"),
        trading_days=["2026-07-17", "2026-07-20", "2026-07-21"],
    ) == ("2026-07-17", "2026-07-20")


def test_frozen_missing_report_supplies_original_account_baseline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17-r2.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "metadata": {"market": "US"},
                "strategy_snapshot": {
                    "strategy_id": "trend_animals_warm_to_hot/US/v4",
                    "strategy_version": "v4",
                },
                "account": {"source_date": "2026-07-17", "net_value": "123.45"},
                "drawdown_summary": {"state_status": "missing"},
            }
        ),
        encoding="utf-8",
    )

    assert frozen_missing_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        source_date="2026-07-17",
    ) == Decimal("123.45")
