from __future__ import annotations

import json
from decimal import Decimal, getcontext
from pathlib import Path

import pytest

from open_trader.strategy_drawdown import (
    manual_unlock_strategy_drawdown,
    observe_strategy_equity,
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


def test_manual_unlock_bootstraps_an_audited_strategy_baseline(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    decision = manual_unlock_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v2",
        strategy_version="v2",
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:05:00+08:00",
        event_id="unlock-cn-v2-001",
        actor="ray",
    )

    assert decision["entry_allowed"] is True
    assert decision["high_water_mark"] == "100"
    assert decision["current_equity"] == "100"
    assert decision["drawdown_pct"] == "0"
    assert decision["kelly_sample_key"] == (
        "CN|trend_animals_warm_to_hot/CN/v2|v2"
    )
    payload = json.loads(
        (data_dir / "trend_drawdown" / "state.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == "open_trader.strategy_drawdown_state.v1"
    assert payload["records"][0]["paused"] is False
    assert payload["audit_events"] == [
        {
            "actor": "ray",
            "event_id": "unlock-cn-v2-001",
            "event_type": "manual_unlock",
            "market": "CN",
            "occurred_at": "2026-07-20T09:05:00+08:00",
            "previous_high_water_mark": None,
            "previous_paused": None,
            "rebased_high_water_mark": "100",
            "strategy_id": "trend_animals_warm_to_hot/CN/v2",
            "strategy_version": "v2",
        }
    ]


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
    manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:00:00+08:00",
        event_id="bootstrap-us-v2",
        actor="ray",
    )

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
    manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:00:00+08:00",
        event_id="bootstrap-hk-v2",
        actor="ray",
    )
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


def test_new_version_and_market_get_isolated_baselines(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    base = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN",
        "strategy_version": "v2",
    }
    manual_unlock_strategy_drawdown(
        data_dir,
        **base,
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:00:00+08:00",
        event_id="bootstrap-cn-v2",
        actor="ray",
    )
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

    assert new_version["entry_allowed"] is True
    assert new_version["high_water_mark"] == "90"
    assert new_version["kelly_sample_key"].endswith("|v3")
    assert other_market["entry_allowed"] is True
    assert other_market["high_water_mark"] == "80"
    assert old_version["entry_allowed"] is False
    records = json.loads(
        (data_dir / "trend_drawdown" / "state.json").read_text(encoding="utf-8")
    )["records"]
    assert {record["kelly_sample_key"] for record in records} == {
        "CN|trend_animals_warm_to_hot/CN|v2",
        "CN|trend_animals_warm_to_hot/CN|v3",
        "US|trend_animals_warm_to_hot/CN|v2",
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
    manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:00:00+08:00",
        event_id="bootstrap-us-v2",
        actor="ray",
    )
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
        manual_unlock_strategy_drawdown(
            data_dir,
            **key,
            current_equity=Decimal("123456.789"),
            occurred_at="2026-07-20T09:00:00+08:00",
            event_id="bootstrap-cn-v3",
            actor="ray",
        )
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
    manual_unlock_strategy_drawdown(
        data_dir,
        **key,
        current_equity=Decimal("100"),
        occurred_at="2026-07-20T09:00:00+08:00",
        event_id="bootstrap-cn-v3",
        actor="ray",
    )
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
        "state.json",
    }


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
