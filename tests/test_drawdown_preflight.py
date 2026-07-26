from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.drawdown_preflight as drawdown_preflight
from open_trader.drawdown_preflight import (
    DrawdownMarketInput,
    market_preflight_dates,
    run_drawdown_preflight,
)
from open_trader.notifications import Notifier, NullNotifier
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


def run_preflight(
    root: Path,
    inputs: dict[str, DrawdownMarketInput],
    *,
    notifier: Notifier | None = None,
) -> dict[str, object]:
    return run_drawdown_preflight(
        data_dir=root / "data",
        reports_dir=root / "reports",
        market_inputs=inputs,
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier if notifier is not None else NullNotifier(),
    )


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))
        if self.fail:
            raise RuntimeError("notification failed")


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


def test_existing_state_does_not_require_repeated_frozen_baseline(
    tmp_path: Path,
) -> None:
    item = market_input("CN")
    assert run_preflight(tmp_path, {"CN": item})["status"] == "ready"
    state_path = tmp_path / "data/trend_drawdown/state.json"
    before = state_path.read_bytes()

    result = run_preflight(
        tmp_path,
        {
            "CN": replace(
                item,
                baseline_equity=None,
                source_date=None,
                entry_eligible_from=None,
            )
        },
    )

    assert result["status"] == "ready"
    assert result["markets"][0]["status"] == "ready"
    assert state_path.read_bytes() == before


def test_new_strategy_versions_inherit_approved_predecessor_high_water_marks(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    old_versions = {"CN": "v8", "HK": "v5", "US": "v5"}
    equities = {"CN": "100", "HK": "200", "US": "300"}
    for market, version in old_versions.items():
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/{version}",
            strategy_version=version,
            parameters={"drawdown_limit": "0.05", "market": market},
            baseline_equity=Decimal(equities[market]),
            source_date="2026-07-17",
            accepted_git_sha="a" * 40,
            actor="acceptance",
            occurred_at="2026-07-18T08:00:00+08:00",
            reason="first_activation",
            entry_eligible_from="2026-07-20",
        )
        observe_strategy_equity(
            data_dir,
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/{version}",
            strategy_version=version,
            current_equity=Decimal(equities[market]) * Decimal("0.94"),
            observed_at="2026-07-19T08:00:00+08:00",
        )

    target_versions = {"CN": "v9", "HK": "v6", "US": "v6"}
    inputs = {
        market: replace(
            market_input(market),
            baseline_equity=None,
            strategy_snapshot={
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
                "strategy_version": version,
                "parameters": {"drawdown_limit": "0.05", "market": market},
            },
        )
        for market, version in target_versions.items()
    }

    result = run_preflight(tmp_path, inputs)

    assert result["status"] == "ready"
    assert {
        item["market"]: item["high_water_mark"] for item in result["markets"]
    } == equities
    state = json.loads((data_dir / "trend_drawdown/state.json").read_text())
    assert {
        record["market"]: record["current_equity"]
        for record in state["records"]
        if record["strategy_version"] in target_versions.values()
    } == {"CN": "94", "HK": "188", "US": "282"}


def test_missing_approved_predecessor_fails_closed_without_writing_state(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v7",
        strategy_version="v7",
        parameters={"drawdown_limit": "0.05", "market": "CN"},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-18T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    state_path = data_dir / "trend_drawdown/state.json"
    before = state_path.read_bytes()

    target = replace(
        market_input("CN"),
        baseline_equity=None,
        strategy_snapshot={
            "strategy_id": "trend_animals_warm_to_hot/CN/v9",
            "strategy_version": "v9",
            "parameters": {"drawdown_limit": "0.05", "market": "CN"},
        },
    )
    notifier = RecordingNotifier()
    result = run_preflight(
        tmp_path,
        {"CN": target},
        notifier=notifier,
    )

    assert result["status"] == "failed"
    assert result["markets"][0]["status"] == "failed"
    assert (
        "approved predecessor drawdown state is unavailable"
        in result["markets"][0]["error"]
    )
    assert state_path.read_bytes() == before
    assert notifier.calls == [(
        "【需处理｜系统｜累计回撤状态阻断】",
        "\n".join([
            "发生：累计回撤状态未通过部署预检",
            "影响：CN v9 暂停新开仓；卖出和保护线继续运行",
            "现在做：让 Codex 检查回撤预检并重新部署；不要手动解除限制",
            "",
            "明细：",
            "- CN v9：回撤预检失败",
        ]),
    )]
    assert "approved predecessor drawdown state is unavailable" not in notifier.calls[0][1]


def test_first_activation_without_matching_baseline_is_skipped(
    tmp_path: Path,
) -> None:
    target = replace(market_input("CN"), baseline_equity=None)

    result = run_preflight(tmp_path, {"CN": target})

    assert result == {
        "status": "ready",
        "markets": [{
            "market": "CN",
            "status": "skipped",
            "reason": "baseline_missing",
            "source_date": "2026-07-17",
        }],
    }
    assert not (tmp_path / "data/trend_drawdown/state.json").exists()


def test_skipped_market_does_not_block_other_market_bootstrap(
    tmp_path: Path,
) -> None:
    result = run_preflight(
        tmp_path,
        {
            "CN": replace(market_input("CN"), baseline_equity=None),
            "US": market_input("US"),
        },
    )

    assert result["status"] == "ready"
    assert [item["status"] for item in result["markets"]] == [
        "skipped", "bootstrapped"
    ]
    state = json.loads(
        (tmp_path / "data/trend_drawdown/state.json").read_text(encoding="utf-8")
    )
    assert [record["market"] for record in state["records"]] == ["US"]


def test_invalid_matching_baseline_fails(tmp_path: Path) -> None:
    path = tmp_path / "reports/trend_a_share/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    result = run_preflight(
        tmp_path,
        {"CN": replace(market_input("CN"), baseline_equity=None)},
    )

    assert result["status"] == "failed"
    assert result["markets"][0]["failure_status"] == "baseline_invalid"
    assert not (tmp_path / "data/trend_drawdown/state.json").exists()


def test_preflight_accepts_the_approved_v4_overheat_trim_transition(
    tmp_path: Path,
) -> None:
    old = market_input("US")
    assert run_preflight(tmp_path, {"US": old})["status"] == "ready"
    state_path = tmp_path / "data/trend_drawdown/state.json"
    before = json.loads(state_path.read_text(encoding="utf-8"))
    parameters = dict(old.strategy_snapshot["parameters"])
    parameters.update({
        "overheat_trim_fraction": "0.30",
        "overheat_trim_once_per_position": True,
        "overheat_trim_signals": ["boiling", "champagne"],
        "overheat_trim_rounding": "floor_to_market_lot",
        "overheat_trim_below_lot": "no_order_terminal",
        "full_exit_precedes_partial_exit": True,
    })

    result = run_preflight(
        tmp_path,
        {"US": replace(old, strategy_snapshot={**old.strategy_snapshot, "parameters": parameters})},
    )

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert result["markets"][0]["status"] == "ready"
    assert after["records"] == before["records"]
    assert after["audit_events"][:-1] == before["audit_events"]
    assert after["audit_events"][-1]["event_type"] == "parameter_compatibility"

    before_rollback = state_path.read_bytes()
    rollback = run_preflight(tmp_path, {"US": old})
    assert rollback["status"] == "failed"
    assert rollback["markets"][0]["failure_status"] == "parameter_mismatch"
    assert state_path.read_bytes() == before_rollback


def test_late_preflight_reports_entries_blocked_until_eligible_date(
    tmp_path: Path,
) -> None:
    item = replace(market_input("CN"), entry_eligible_from="2026-07-21")

    result = run_preflight(tmp_path, {"CN": item})

    assert result["markets"][0]["entry_allowed"] is False


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
    expected = json.loads(state_path.read_bytes())
    state_path.unlink()
    write_report(tmp_path, "HK", "ok")

    result = run_preflight(tmp_path, {"HK": item})

    assert result["status"] == "ready"
    assert result["markets"][0]["status"] == "recovered"
    assert result["markets"][0]["entry_allowed"] is False
    assert result["markets"][0]["recovery"]["status"] == "recovered"
    assert result["markets"][0]["recovery_event"]["event_type"] == "snapshot_recovery"
    restored = json.loads(state_path.read_bytes())
    assert restored["records"] == expected["records"]
    assert restored["audit_events"][:-1] == expected["audit_events"]


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


def test_load_frozen_baseline_returns_original_account_equity(
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

    assert drawdown_preflight.load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        source_date="2026-07-17",
    ) == drawdown_preflight.FrozenBaselineLookup(
        status="available",
        equity=Decimal("123.45"),
    )


def test_load_frozen_baseline_reports_missing_current_strategy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "metadata": {"market": "US"},
            "strategy_snapshot": {
                "strategy_id": "trend_animals_warm_to_hot/US/v4",
                "strategy_version": "v4",
            },
            "account": {"source_date": "2026-07-17", "net_value": "123.45"},
            "drawdown_summary": {"state_status": "ok"},
        }),
        encoding="utf-8",
    )

    result = drawdown_preflight.load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v5",
        strategy_version="v5",
        source_date="2026-07-17",
    )

    assert result.status == "missing"
    assert result.equity is None
    assert result.error == ""


@pytest.mark.parametrize(
    ("content", "error_text"),
    [
        ("{", "unreadable frozen drawdown baseline"),
        (
            json.dumps({
                "metadata": {"market": "US"},
                "strategy_snapshot": {
                    "strategy_id": "trend_animals_warm_to_hot/US/v4",
                    "strategy_version": "v4",
                },
                "account": {
                    "source_date": "2026-07-17",
                    "net_value": "not-a-number",
                },
                "drawdown_summary": {"state_status": "missing"},
            }),
            "invalid frozen drawdown baseline",
        ),
    ],
)
def test_load_frozen_baseline_rejects_invalid_completed_date_artifacts(
    tmp_path: Path,
    content: str,
    error_text: str,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    result = drawdown_preflight.load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        source_date="2026-07-17",
    )

    assert result.status == "invalid"
    assert error_text in result.error


def test_failure_alert_is_grouped_deduplicated_and_rearmed_after_recovery(
    tmp_path: Path,
) -> None:
    for market in ("CN", "HK", "US"):
        write_report(tmp_path, market, "missing")
    failed_inputs = {
        market: replace(market_input(market), baseline_equity=None)
        for market in ("CN", "HK", "US")
    }
    notifier = RecordingNotifier()
    request = dict(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        market_inputs=failed_inputs,
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier,
    )

    assert run_drawdown_preflight(**request)["status"] == "failed"
    assert run_drawdown_preflight(**request)["status"] == "failed"
    expected = (
        "【需处理｜系统｜累计回撤状态阻断】",
        "\n".join([
            "发生：累计回撤状态未通过部署预检",
            "影响：CN v4、HK v4、US v4 暂停新开仓；卖出和保护线继续运行",
            "现在做：让 Codex 检查回撤预检并重新部署；不要手动解除限制",
            "",
            "明细：",
            "- CN v4：回撤预检失败",
            "- HK v4：回撤预检失败",
            "- US v4：回撤预检失败",
        ]),
    )
    assert notifier.calls == [expected]
    assert json.loads(
        (tmp_path / "data/trend_drawdown/alerts.json").read_text()
    )["active"] == [
        "CN|v4|baseline_invalid",
        "HK|v4|baseline_invalid",
        "US|v4|baseline_invalid",
    ]

    request["market_inputs"] = {
        market: market_input(market) for market in ("CN", "HK", "US")
    }
    assert run_drawdown_preflight(**request)["status"] == "ready"
    state_root = tmp_path / "data/trend_drawdown"
    (state_root / "state.json").unlink()
    shutil.rmtree(state_root / "snapshots")
    request["market_inputs"] = failed_inputs

    assert run_drawdown_preflight(**request)["status"] == "failed"
    assert notifier.calls == [expected, expected]


def test_notification_failure_does_not_change_fail_closed_result(
    tmp_path: Path,
) -> None:
    for market in ("CN", "HK", "US"):
        write_report(tmp_path, market, "missing")
    notifier = RecordingNotifier(fail=True)
    result = run_drawdown_preflight(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        market_inputs={
            market: replace(market_input(market), baseline_equity=None)
            for market in ("CN", "HK", "US")
        },
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier,
    )

    assert result["status"] == "failed"
    assert len(notifier.calls) == 1
    assert not (tmp_path / "data/trend_drawdown/alerts.json").exists()


def test_null_notifier_does_not_record_alert_delivery(
    tmp_path: Path,
) -> None:
    for market in ("CN", "HK", "US"):
        write_report(tmp_path, market, "missing")
    result = run_preflight(
        tmp_path,
        {
            market: replace(market_input(market), baseline_equity=None)
            for market in ("CN", "HK", "US")
        },
    )

    assert result["status"] == "failed"
    assert not (tmp_path / "data/trend_drawdown/alerts.json").exists()
