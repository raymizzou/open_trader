from __future__ import annotations

import json
from decimal import Decimal, getcontext
from pathlib import Path

import pytest

from open_trader.a_share_trend import live_trend_strategy_snapshot
from open_trader.strategy_drawdown import (
    UNIFIED_TREND_V5_COMPATIBILITY_REVISION,
    UNIFIED_TREND_V5_PARAMETER_HASHES,
    automatic_bootstrap_strategy_drawdown,
    manual_unlock_strategy_drawdown,
    observe_strategy_equity,
    recover_strategy_drawdown_state,
    strategy_parameter_hash,
    valid_drawdown_decision,
)


def bootstrap(
    data_dir: Path,
    key: dict[str, str],
    *,
    equity: str = "100",
    occurred_at: str = "2026-07-20T09:00:00+08:00",
) -> None:
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        **key,
        parameters={"test_strategy": True},
        baseline_equity=Decimal(equity),
        source_date=occurred_at[:10],
        accepted_git_sha="a" * 40,
        actor="pytest",
        occurred_at=occurred_at,
        reason="first_activation",
        entry_eligible_from=occurred_at[:10],
    )


def test_missing_drawdown_state_fails_closed_without_creating_artifact(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"

    decision = observe_strategy_equity(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v2",
        strategy_version="v2",
        current_equity=Decimal("100"),
        observed_at="2026-07-20T09:00:00+08:00",
    )

    assert decision["entry_allowed"] is False
    assert decision["state_status"] == "missing"
    assert decision["pause_reason"] == "策略累计回撤状态缺失，暂停新开仓"
    assert not (data_dir / "trend_drawdown" / "state.json").exists()


def test_new_version_inherits_high_water_mark_and_pause_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    old = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v8",
        "strategy_version": "v8",
    }
    bootstrap(data_dir, old, equity="100")
    observe_strategy_equity(
        data_dir,
        **old,
        current_equity=Decimal("94"),
        observed_at="2026-07-24T15:00:00+08:00",
    )

    decision = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v9",
        strategy_version="v9",
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=None,
        source_date="2026-07-24",
        accepted_git_sha="b" * 40,
        actor="pytest",
        occurred_at="2026-07-25T08:00:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from="2026-07-27",
        inherit_from=("trend_animals_warm_to_hot/CN/v8", "v8"),
    )

    assert decision["high_water_mark"] == "100"
    assert decision["current_equity"] == "94"
    assert decision["entry_allowed"] is False
    assert decision["paused_at"] == "2026-07-24T15:00:00+08:00"


def test_legacy_missing_decision_remains_readable() -> None:
    decision = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v4|v4",
        "state_status": "missing",
        "status": "paused",
        "status_label": "暂停新开仓",
        "entry_allowed": False,
        "current_equity": "100",
        "high_water_mark": None,
        "drawdown_pct": None,
        "drawdown_limit_pct": "0.05",
        "pause_reason": "策略累计回撤状态缺失，暂停新开仓",
        "paused_at": None,
        "observed_at": "2026-07-20T09:00:00+08:00",
    }

    assert valid_drawdown_decision(
        decision,
        expected_market="CN",
        expected_strategy_id="trend_animals_warm_to_hot/CN/v4",
        expected_strategy_version="v4",
        expected_equity="100",
    )


def test_parameter_hash_is_canonical() -> None:
    assert strategy_parameter_hash({"limit": "0.05", "markets": ["CN", "HK"]}) == (
        strategy_parameter_hash({"markets": ["CN", "HK"], "limit": "0.05"})
    )
    assert len(strategy_parameter_hash({"limit": "0.05"})) == 64


OVERHEAT_TRIM_PARAMETERS = {
    "overheat_trim_fraction": "0.30",
    "overheat_trim_once_per_position": True,
    "overheat_trim_signals": ["boiling", "champagne"],
    "overheat_trim_rounding": "floor_to_market_lot",
    "overheat_trim_below_lot": "no_order_terminal",
    "full_exit_precedes_partial_exit": True,
}


def _v5_snapshot(market: str) -> dict[str, object]:
    pool_ids = (622460,) if market == "US" else (622494,)
    return live_trend_strategy_snapshot(
        market, "verify", pool_ids, strategy_version="v5"
    )


def _seed_legacy_v5_state(
    tmp_path: Path,
    *,
    market: str,
    strategy_version: str = "v5",
    parameter_hash: str | None = None,
) -> Path:
    data_dir = tmp_path / "data"
    strategy_id = f"trend_animals_warm_to_hot/{market}/{strategy_version}"
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        parameters={"legacy": market},
        baseline_equity=Decimal("100"),
        source_date="2026-07-23",
        accepted_git_sha="a" * 40,
        actor="legacy",
        occurred_at="2026-07-23T09:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-24",
    )
    path = data_dir / "trend_drawdown/state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    event = payload["audit_events"][0]
    event["parameter_hash"] = parameter_hash or UNIFIED_TREND_V5_PARAMETER_HASHES[market][0]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return data_dir


@pytest.mark.parametrize(
    ("market", "version", "pool_ids", "old_hash"),
    [
        (
            "CN",
            "v9",
            (622466, 697199),
            "ed65702b9b9fc4310cb3e0caf367e946e4eb1e96a018a8e26a1ed1363e2d02a3",
        ),
        (
            "US",
            "v6",
            (622460, 705013),
            "5230dca5a19119a416a8fe6fff475fc8bfa8c6c6c4b3646c02fe783d364bb11b",
        ),
        (
            "HK",
            "v6",
            (622494, 707617),
            "76feec3ad233995e1d6d257dd53cf437900aa31e71da62200963dfe61832b2e6",
        ),
    ],
)
def test_etf_universe_compatibility_transition_is_audited(
    tmp_path: Path,
    market: str,
    version: str,
    pool_ids: tuple[int, ...],
    old_hash: str,
) -> None:
    data_dir = _seed_legacy_v5_state(
        tmp_path,
        market=market,
        strategy_version=version,
        parameter_hash=old_hash,
    )
    snapshot = live_trend_strategy_snapshot(
        market, "verify", pool_ids, strategy_version=version
    )
    parameters = dict(snapshot["parameters"])

    decision = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/{version}",
        strategy_version=version,
        parameters=parameters,
        baseline_equity=None,
        source_date=None,
        accepted_git_sha="c" * 40,
        actor="acceptance",
        occurred_at="2026-07-26T09:00:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from=None,
    )

    event = decision["parameter_compatibility_event"]
    assert event["old_parameter_hash"] == old_hash
    assert event["new_parameter_hash"] == strategy_parameter_hash(parameters)
    assert event["compatibility_revision"] == "trend_etf_universe_v1"
    state_after_transition = (
        data_dir / "trend_drawdown/state.json"
    ).read_bytes()

    replay = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/{version}",
        strategy_version=version,
        parameters=parameters,
        baseline_equity=None,
        source_date=None,
        accepted_git_sha="d" * 40,
        actor="acceptance-replay",
        occurred_at="2026-07-26T09:01:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from=None,
    )

    assert replay == decision
    assert (data_dir / "trend_drawdown/state.json").read_bytes() == (
        state_after_transition
    )


@pytest.mark.parametrize("market", ["US", "HK"])
def test_unified_v5_compatibility_transition_is_audited(
    tmp_path: Path, market: str
) -> None:
    data_dir = _seed_legacy_v5_state(tmp_path, market=market)
    snapshot = _v5_snapshot(market)
    accepted_sha = "c" * 40

    decision = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/v5",
        strategy_version="v5",
        parameters=snapshot["parameters"],
        baseline_equity=None,
        source_date=None,
        accepted_git_sha=accepted_sha,
        actor="acceptance",
        occurred_at="2026-07-24T11:00:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from=None,
    )

    payload = json.loads(
        (data_dir / "trend_drawdown/state.json").read_text(encoding="utf-8")
    )
    compatibility = [
        event
        for event in payload["audit_events"]
        if event["event_type"] == "parameter_compatibility"
    ]
    assert len(compatibility) == 1
    assert compatibility[0]["old_parameter_hash"] == UNIFIED_TREND_V5_PARAMETER_HASHES[market][0]
    assert compatibility[0]["new_parameter_hash"] == UNIFIED_TREND_V5_PARAMETER_HASHES[market][1]
    assert compatibility[0]["compatibility_revision"] == UNIFIED_TREND_V5_COMPATIBILITY_REVISION
    assert compatibility[0]["accepted_git_sha"] == accepted_sha
    assert decision["parameter_compatibility_event"] == compatibility[0]
    state_after_transition = (data_dir / "trend_drawdown/state.json").read_bytes()
    replay = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/v5",
        strategy_version="v5",
        parameters=snapshot["parameters"],
        baseline_equity=None,
        source_date=None,
        accepted_git_sha="d" * 40,
        actor="acceptance-replay",
        occurred_at="2026-07-24T11:01:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from=None,
    )
    assert replay == decision
    assert (data_dir / "trend_drawdown/state.json").read_bytes() == state_after_transition


@pytest.mark.parametrize("market", ["US", "HK"])
def test_unified_v5_compatibility_rejects_wrong_legacy_identity(
    tmp_path: Path, market: str
) -> None:
    data_dir = _seed_legacy_v5_state(
        tmp_path,
        market=market,
        parameter_hash="f" * 64,
    )
    snapshot = _v5_snapshot(market)
    before = (data_dir / "trend_drawdown/state.json").read_bytes()

    with pytest.raises(ValueError, match="strategy parameters changed without a version bump"):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/v5",
            strategy_version="v5",
            parameters=snapshot["parameters"],
            baseline_equity=None,
            source_date=None,
            accepted_git_sha="c" * 40,
            actor="acceptance",
            occurred_at="2026-07-24T11:00:00+08:00",
            reason="new_strategy_version",
            entry_eligible_from=None,
        )
    assert (data_dir / "trend_drawdown/state.json").read_bytes() == before


@pytest.mark.parametrize("market", ["US", "HK"])
def test_unified_v5_compatibility_rejects_wrong_new_parameters(
    tmp_path: Path, market: str
) -> None:
    data_dir = _seed_legacy_v5_state(tmp_path, market=market)
    snapshot = _v5_snapshot(market)
    changed_parameters = {
        **snapshot["parameters"],
        "target_weight": "0.05",
    }
    before = (data_dir / "trend_drawdown/state.json").read_bytes()

    with pytest.raises(ValueError, match="strategy parameters changed without a version bump"):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/v5",
            strategy_version="v5",
            parameters=changed_parameters,
            baseline_equity=None,
            source_date=None,
            accepted_git_sha="c" * 40,
            actor="acceptance",
            occurred_at="2026-07-24T11:00:00+08:00",
            reason="new_strategy_version",
            entry_eligible_from=None,
        )
    assert (data_dir / "trend_drawdown/state.json").read_bytes() == before


def test_v4_overheat_trim_compatibility_appends_only_a_validated_audit_event(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    old_parameters = {"drawdown_limit": "0.05", "market": "US"}
    request = {
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v4",
        "strategy_version": "v4",
        "parameters": old_parameters,
        "baseline_equity": Decimal("100000"),
        "source_date": "2026-07-17",
        "accepted_git_sha": "a" * 40,
        "actor": "deployment",
        "occurred_at": "2026-07-20T08:00:00+08:00",
        "reason": "first_activation",
        "entry_eligible_from": "2026-07-20",
    }
    automatic_bootstrap_strategy_drawdown(data_dir, **request)
    state_path = data_dir / "trend_drawdown/state.json"
    before = json.loads(state_path.read_text(encoding="utf-8"))
    updated_parameters = {**old_parameters, **OVERHEAT_TRIM_PARAMETERS}

    decision = automatic_bootstrap_strategy_drawdown(
        data_dir,
        **{
            **request,
            "parameters": updated_parameters,
            "baseline_equity": None,
            "source_date": None,
            "accepted_git_sha": "b" * 40,
            "occurred_at": "2026-07-21T08:00:00+08:00",
            "entry_eligible_from": None,
        },
    )

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert after["records"] == before["records"]
    assert after["audit_events"][:-1] == before["audit_events"]
    compatibility = after["audit_events"][-1]
    assert compatibility == {
        "event_id": compatibility["event_id"],
        "event_type": "parameter_compatibility",
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v4",
        "strategy_version": "v4",
        "actor": "deployment",
        "occurred_at": "2026-07-21T08:00:00+08:00",
        "old_parameter_hash": strategy_parameter_hash(old_parameters),
        "new_parameter_hash": strategy_parameter_hash(updated_parameters),
        "compatibility_revision": "trend_overheat_trim_v1",
        "accepted_git_sha": "b" * 40,
    }
    assert decision["parameter_compatibility_event"] == compatibility

    state_after_first_transition = state_path.read_bytes()
    replay = automatic_bootstrap_strategy_drawdown(
        data_dir,
        **{
            **request,
            "parameters": updated_parameters,
            "baseline_equity": None,
            "source_date": None,
            "accepted_git_sha": "b" * 40,
            "occurred_at": "2026-07-21T08:00:00+08:00",
            "entry_eligible_from": None,
        },
    )
    assert replay == decision
    assert state_path.read_bytes() == state_after_first_transition

    with pytest.raises(
        ValueError, match="strategy parameters changed without a version bump"
    ):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            **{
                **request,
                "baseline_equity": None,
                "source_date": None,
                "entry_eligible_from": None,
            },
        )
    assert state_path.read_bytes() == state_after_first_transition


@pytest.mark.parametrize(
    "parameters",
    [
        {**OVERHEAT_TRIM_PARAMETERS, "overheat_trim_fraction": "0.31"},
        {
            **OVERHEAT_TRIM_PARAMETERS,
            "overheat_trim_signals": ["champagne", "boiling"],
        },
        {**OVERHEAT_TRIM_PARAMETERS, "unexpected_parameter": "nope"},
    ],
)
def test_v4_overheat_trim_compatibility_rejects_any_nonapproved_transition(
    tmp_path: Path, parameters: dict[str, object],
) -> None:
    data_dir = tmp_path / "data"
    old_parameters = {"drawdown_limit": "0.05", "market": "HK"}
    request = {
        "market": "HK",
        "strategy_id": "trend_animals_warm_to_hot/HK/v4",
        "strategy_version": "v4",
        "parameters": old_parameters,
        "baseline_equity": Decimal("100000"),
        "source_date": "2026-07-17",
        "accepted_git_sha": "a" * 40,
        "actor": "deployment",
        "occurred_at": "2026-07-20T08:00:00+08:00",
        "reason": "first_activation",
        "entry_eligible_from": "2026-07-20",
    }
    automatic_bootstrap_strategy_drawdown(data_dir, **request)
    state_path = data_dir / "trend_drawdown/state.json"
    before = state_path.read_bytes()

    with pytest.raises(
        ValueError, match="strategy parameters changed without a version bump"
    ):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            **{
                **request,
                "parameters": {**old_parameters, **parameters},
                "baseline_equity": None,
                "source_date": None,
                "entry_eligible_from": None,
            },
        )

    assert state_path.read_bytes() == before


def test_v4_ordinary_parameter_drift_still_requires_a_version_bump(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    request = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
        "parameters": {"drawdown_limit": "0.05", "market": "CN"},
        "baseline_equity": Decimal("100000"),
        "source_date": "2026-07-17",
        "accepted_git_sha": "a" * 40,
        "actor": "deployment",
        "occurred_at": "2026-07-20T08:00:00+08:00",
        "reason": "first_activation",
        "entry_eligible_from": "2026-07-20",
    }
    automatic_bootstrap_strategy_drawdown(data_dir, **request)

    with pytest.raises(
        ValueError, match="strategy parameters changed without a version bump"
    ):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            **{
                **request,
                "parameters": {"drawdown_limit": "0.06", "market": "CN"},
                "baseline_equity": None,
                "source_date": None,
                "entry_eligible_from": None,
            },
        )


def test_automatic_bootstrap_is_audited_and_idempotent_by_parameters(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    request = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
        "parameters": {"position_limit": 10, "drawdown_limit": "0.05"},
        "baseline_equity": Decimal("100000"),
        "source_date": "2026-07-17",
        "accepted_git_sha": "a" * 40,
        "actor": "deployment",
        "occurred_at": "2026-07-20T08:00:00+08:00",
        "reason": "first_activation",
        "entry_eligible_from": "2026-07-20",
    }

    decision = automatic_bootstrap_strategy_drawdown(data_dir, **request)

    assert decision["entry_allowed"] is True
    assert decision["bootstrap_event"] == {
        "accepted_git_sha": "a" * 40,
        "actor": "deployment",
        "baseline_equity": "100000",
        "entry_eligible_from": "2026-07-20",
        "event_id": decision["bootstrap_event"]["event_id"],
        "event_type": "automatic_bootstrap",
        "market": "CN",
        "occurred_at": "2026-07-20T08:00:00+08:00",
        "parameter_hash": strategy_parameter_hash(request["parameters"]),
        "reason": "first_activation",
        "source_date": "2026-07-17",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
    }
    state_path = data_dir / "trend_drawdown" / "state.json"
    before = state_path.read_bytes()
    replay = automatic_bootstrap_strategy_drawdown(
        data_dir,
        **{**request, "accepted_git_sha": "b" * 40},
    )
    assert replay == decision
    assert state_path.read_bytes() == before

    with pytest.raises(
        ValueError, match="strategy parameters changed without a version bump"
    ):
        automatic_bootstrap_strategy_drawdown(
            data_dir,
            **{**request, "parameters": {"position_limit": 9}},
        )


def test_observe_does_not_bootstrap_a_missing_strategy_key(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v4",
        strategy_version="v4",
        parameters={"position_limit": 10},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    state_path = data_dir / "trend_drawdown" / "state.json"
    before = state_path.read_bytes()

    decision = observe_strategy_equity(
        data_dir,
        market="HK",
        strategy_id="trend_animals_warm_to_hot/HK/v4",
        strategy_version="v4",
        current_equity=Decimal("90"),
        observed_at="2026-07-20T17:00:00+08:00",
    )

    assert decision["state_status"] == "missing"
    assert decision["entry_allowed"] is False
    assert state_path.read_bytes() == before


def test_late_bootstrap_blocks_entries_until_the_recorded_trading_date(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "HK",
        "strategy_id": "trend_animals_warm_to_hot/HK/v4",
        "strategy_version": "v4",
    }
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        **key,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T09:31:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-21",
    )

    blocked = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        observed_at="2026-07-20T09:32:00+08:00",
        entry_date="2026-07-20",
    )
    eligible = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        observed_at="2026-07-20T09:33:00+08:00",
        entry_date="2026-07-21",
    )

    assert blocked["status"] == "pending"
    assert blocked["entry_allowed"] is False
    assert blocked["pause_reason"] == "回撤基准将在 2026-07-21 起允许新开仓"
    assert eligible["status"] == "active"
    assert eligible["entry_allowed"] is True


def test_manual_unlock_rejects_a_missing_or_active_strategy(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v2",
        "strategy_version": "v2",
    }
    request = dict(
        **key, current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:05:00+08:00",
        event_id="unlock-cn-v2-001", actor="ray",
    )

    with pytest.raises(ValueError, match="existing paused strategy"):
        manual_unlock_strategy_drawdown(data_dir, **request)
    bootstrap(data_dir, key)
    with pytest.raises(ValueError, match="existing paused strategy"):
        manual_unlock_strategy_drawdown(data_dir, **request)


@pytest.mark.parametrize(
    ("equity", "expected_allowed", "expected_drawdown"),
    [
        ("96", True, "0.04"),
        ("95", False, "0.05"),
        ("94.99", False, "0.0501"),
    ],
)
def test_drawdown_entry_gate_is_inclusive_at_five_percent(
    tmp_path: Path,
    equity: str,
    expected_allowed: bool,
    expected_drawdown: str,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
    }
    bootstrap(data_dir, key)

    decision = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal(equity),
        observed_at="2026-07-21T09:00:00+08:00",
    )

    assert decision["entry_allowed"] is expected_allowed
    assert decision["drawdown_pct"] == expected_drawdown


def test_paused_drawdown_persists_after_equity_recovers_above_the_peak(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "HK",
        "strategy_id": "trend_animals_warm_to_hot/HK/v2",
        "strategy_version": "v2",
    }
    bootstrap(data_dir, key)
    paused = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("95"),
        observed_at="2026-07-21T09:00:00+08:00",
    )

    recovered = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("110"),
        observed_at="2026-07-22T09:00:00+08:00",
    )

    assert paused["paused_at"] == "2026-07-21T09:00:00+08:00"
    assert recovered["entry_allowed"] is False
    assert recovered["high_water_mark"] == "100"
    assert recovered["current_equity"] == "110"
    assert recovered["drawdown_pct"] == "0"
    assert recovered["paused_at"] == paused["paused_at"]


def test_observe_keeps_new_version_and_market_fail_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    base = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN",
        "strategy_version": "v2",
    }
    bootstrap(data_dir, base)
    observe_strategy_equity(
        data_dir,
        **base,
        current_equity=Decimal("90"),
        observed_at="2026-07-21T09:00:00+08:00",
    )

    new_version = observe_strategy_equity(
        data_dir,
        market="CN",
        strategy_id=base["strategy_id"],
        strategy_version="v3",
        current_equity=Decimal("90"),
        observed_at="2026-07-22T09:00:00+08:00",
    )
    other_market = observe_strategy_equity(
        data_dir,
        market="US",
        strategy_id=base["strategy_id"],
        strategy_version="v2",
        current_equity=Decimal("80"),
        observed_at="2026-07-22T09:00:00+08:00",
    )
    old_version = observe_strategy_equity(
        data_dir,
        **base,
        current_equity=Decimal("100"),
        observed_at="2026-07-22T09:00:00+08:00",
    )

    assert new_version["entry_allowed"] is False
    assert new_version["state_status"] == "missing"
    assert other_market["entry_allowed"] is False
    assert other_market["state_status"] == "missing"
    assert old_version["entry_allowed"] is False
    records = json.loads(
        (data_dir / "trend_drawdown" / "state.json").read_text(encoding="utf-8")
    )["records"]
    assert {record["kelly_sample_key"] for record in records} == {
        "CN|trend_animals_warm_to_hot/CN|v2",
    }


def test_corrupt_audit_schema_fails_closed_without_overwriting_state(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    state_path = data_dir / "trend_drawdown" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.strategy_drawdown_state.v1",
                "records": [],
                "audit_events": [{"event_id": "incomplete"}],
            }
        ),
        encoding="utf-8",
    )
    before = state_path.read_bytes()

    decision = observe_strategy_equity(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v2",
        strategy_version="v2",
        current_equity=Decimal("100"),
        observed_at="2026-07-20T09:00:00+08:00",
    )

    assert decision["entry_allowed"] is False
    assert decision["state_status"] == "corrupt"
    assert decision["pause_reason"] == "策略累计回撤状态损坏，暂停新开仓"
    assert state_path.read_bytes() == before


def test_same_version_unlock_rebases_without_touching_kelly_samples_and_is_idempotent(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
    }
    bootstrap(data_dir, key)
    observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("90"),
        observed_at="2026-07-21T09:00:00+08:00",
    )
    samples_path = data_dir / "latest" / "kelly_trade_samples.json"
    samples_path.parent.mkdir(parents=True)
    samples_path.write_bytes(b'{"samples":["preserve-me"]}\n')
    samples_before = samples_path.read_bytes()

    unlocked = manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("92"),
        occurred_at="2026-07-21T09:05:00+08:00",
        event_id="unlock-us-v2-001",
        actor="ray",
    )
    later = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("87"),
        observed_at="2026-07-22T09:00:00+08:00",
    )
    state_path = data_dir / "trend_drawdown" / "state.json"
    state_before_replay = state_path.read_bytes()
    replayed = manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("92"),
        occurred_at="2026-07-21T09:05:00+08:00",
        event_id="unlock-us-v2-001",
        actor="ray",
    )

    assert unlocked["entry_allowed"] is True
    assert unlocked["high_water_mark"] == "92"
    assert later["current_equity"] == "87"
    assert later["entry_allowed"] is False
    assert replayed == later
    assert state_path.read_bytes() == state_before_replay
    assert samples_path.read_bytes() == samples_before
    assert len(json.loads(state_before_replay)["audit_events"]) == 2

    retried = manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("92"),
        occurred_at="2026-07-22T09:05:00+08:00",
        event_id="unlock-us-v2-001",
        actor="ray",
    )
    assert retried == later
    assert state_path.read_bytes() == state_before_replay

    with pytest.raises(ValueError, match="event_id"):
        manual_unlock_strategy_drawdown(
            data_dir,
            **key,
            current_equity=Decimal("93"),
            occurred_at="2026-07-22T09:05:00+08:00",
            event_id="unlock-us-v2-001",
            actor="ray",
        )

    with pytest.raises(ValueError, match="event_id"):
        manual_unlock_strategy_drawdown(
            data_dir,
            **key,
            current_equity=Decimal("92"),
            occurred_at="2026-07-22T09:05:00+08:00",
            event_id="unlock-us-v2-001",
            actor="other-actor",
        )


def test_drawdown_persistence_is_independent_of_ambient_decimal_precision(
    tmp_path: Path,
) -> None:
    key = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v3",
        "strategy_version": "v3",
    }

    def run(data_dir: Path) -> tuple[dict[str, object], bytes]:
        bootstrap(data_dir, key, equity="123456.789")
        decision = observe_strategy_equity(
            data_dir,
            **key,
            current_equity=Decimal("115000.123456"),
            observed_at="2026-07-21T09:00:00+08:00",
        )
        return decision, (
            data_dir / "trend_drawdown" / "state.json"
        ).read_bytes()

    normal_decision, normal_bytes = run(tmp_path / "normal")
    original_precision = getcontext().prec
    try:
        getcontext().prec = 6
        low_precision_decision, low_precision_bytes = run(tmp_path / "low")
    finally:
        getcontext().prec = original_precision

    assert low_precision_decision == normal_decision
    assert low_precision_bytes == normal_bytes


def test_atomic_state_replace_failure_preserves_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v3",
        "strategy_version": "v3",
    }
    bootstrap(data_dir, key)
    state_path = data_dir / "trend_drawdown" / "state.json"
    before = state_path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        observe_strategy_equity(
            data_dir,
            **key,
            current_equity=Decimal("95"),
            observed_at="2026-07-21T09:00:00+08:00",
        )

    assert state_path.read_bytes() == before
    assert {path.name for path in state_path.parent.iterdir()} == {
        ".state.lock",
        "snapshots",
        "state.json",
    }


def test_state_updates_create_distinct_immutable_hashed_snapshots(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v4",
        "strategy_version": "v4",
    }
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        **key,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("94"),
        observed_at="2026-07-21T16:00:00-04:00",
    )

    snapshots = sorted((data_dir / "trend_drawdown/snapshots").glob("*.json"))
    assert len(snapshots) == 2
    for path in snapshots:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["schema_version"] == (
            "open_trader.strategy_drawdown_snapshot.v1"
        )
        assert path.stem == envelope["state_sha256"]


def test_recovery_restores_latest_sticky_pause_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    key = {
        "market": "HK",
        "strategy_id": "trend_animals_warm_to_hot/HK/v4",
        "strategy_version": "v4",
    }
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        **key,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("94"),
        observed_at="2026-07-21T16:00:00+08:00",
    )
    state_path = data_dir / "trend_drawdown/state.json"
    state_path.unlink()

    result = recover_strategy_drawdown_state(
        data_dir,
        actor="acceptance",
        occurred_at="2026-07-22T08:00:00+08:00",
    )

    assert result["status"] == "recovered"
    restored = json.loads(state_path.read_bytes())
    assert restored["records"][0]["paused"] is True
    assert restored["records"][0]["high_water_mark"] == "100"
    decision = observe_strategy_equity(
        data_dir,
        **key,
        current_equity=Decimal("94"),
        observed_at="2026-07-22T08:01:00+08:00",
    )
    assert decision["recovery_event"]["actor"] == "acceptance"
    assert decision["recovery_event"]["state_sha256"] == result["state_sha256"]


def test_recovery_skips_invalid_newest_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v4",
        strategy_version="v4",
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    state_path = data_dir / "trend_drawdown/state.json"
    expected = json.loads(state_path.read_bytes())
    state_path.unlink()
    invalid = data_dir / "trend_drawdown/snapshots" / ("f" * 64 + ".json")
    invalid.write_text('{"state_sha256":"bad"}\n', encoding="utf-8")

    result = recover_strategy_drawdown_state(
        data_dir,
        actor="acceptance",
        occurred_at="2026-07-22T08:00:00+08:00",
    )

    assert result["snapshot"] != str(invalid)
    restored = json.loads(state_path.read_bytes())
    assert restored["records"] == expected["records"]
    assert restored["audit_events"][:-1] == expected["audit_events"]
    assert restored["audit_events"][-1]["event_type"] == "snapshot_recovery"
    assert restored["audit_events"][-1]["snapshot"] == Path(result["snapshot"]).name


def test_recovery_failure_does_not_overwrite_corrupt_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    state_path = data_dir / "trend_drawdown/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"corrupt-state\n")
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="no valid strategy drawdown snapshot"):
        recover_strategy_drawdown_state(
            data_dir,
            actor="acceptance",
            occurred_at="2026-07-22T08:00:00+08:00",
        )

    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    "timestamp",
    ["2026-07-20T09:00:00", "2026-07-20 09:00:00+08:00", "not-a-time"],
)
def test_state_updates_reject_noncanonical_timestamps(
    tmp_path: Path, timestamp: str,
) -> None:
    with pytest.raises(ValueError, match="canonical timezone-aware"):
        observe_strategy_equity(
            tmp_path / "data",
            market="CN",
            strategy_id="trend_animals_warm_to_hot/CN/v3",
            strategy_version="v3",
            current_equity=Decimal("100"),
            observed_at=timestamp,
        )
