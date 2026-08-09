from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from open_trader import market_trend
import open_trader.trend_review as trend_review
from open_trader.a_share_trend import (
    AccountPosition,
    AccountSnapshot,
    CandidateInput,
    RealHoldingInput,
    _report_payload,
    build_report as _build_report,
    live_trend_strategy_snapshot,
    trend_strategy_snapshot,
    load_protection_state,
    write_protection_state,
)
from open_trader.strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    observe_strategy_equity,
)
from open_trader.trend_api_stats import (
    build_trend_api_stats_payload,
    write_trend_api_stats,
)
from open_trader.trend_kelly import (
    calculate_trend_kelly,
    trend_kelly_rounds_from_payload,
)
from open_trader.models import Market, TradeFill
from open_trader.trend_industry_context import IndustryContext


ACCOUNT_INPUT = {
    "snapshot_generation": "sha256:" + "a" * 64,
    "account_generation": "sha256:" + "b" * 64,
    "status": "healthy",
}


def build_report(*args: object, **kwargs: object) -> object:
    kwargs.setdefault("account_input", ACCOUNT_INPUT)
    return _build_report(*args, **kwargs)  # type: ignore[arg-type]


def test_rotation_reservations_keep_two_immutable_slots_across_revisions(
    tmp_path: Path,
) -> None:
    def pair(index: int, sell: str, buy: str) -> dict[str, object]:
        return {
            "pair_index": index,
            "sell_symbol": sell,
            "sell_name": sell,
            "sell_futu_symbol": f"US.{sell}",
            "sell_global_strength": "10",
            "buy_symbol": buy,
            "buy_name": buy,
            "buy_futu_symbol": f"US.{buy}",
            "buy_global_strength": "90",
            "strength_gap": "80",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 40,
            "lot_size": 1,
            "atr": "5",
            "reason": "relative_rotation",
        }

    first = trend_review.reserve_rotation_pairs(
        tmp_path,
        market="US",
        account_key="simulate-102",
        execution_date="2026-08-04",
        pairs=[
            pair(0, "WEAK1", "STRONG1"),
            pair(1, "WEAK2", "STRONG2"),
        ],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:20:00+08:00",
    )
    revised = trend_review.reserve_rotation_pairs(
        tmp_path,
        market="US",
        account_key="simulate-102",
        execution_date="2026-08-04",
        pairs=[pair(0, "OTHERSELL", "OTHERBUY")],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:30:00+08:00",
    )
    no_proposal = trend_review.reserve_rotation_pairs(
        tmp_path, market="US", account_key="simulate-102",
        execution_date="2026-08-04", pairs=[], allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:40:00+08:00",
    )

    assert [(pair["sell_symbol"], pair["buy_symbol"]) for pair in first] == [
        ("WEAK1", "STRONG1"),
        ("WEAK2", "STRONG2"),
    ]
    assert revised == first
    assert no_proposal == first
    stored = tmp_path / "trend_review/rotation_plans/US/simulate-102/2026-08-04/0.json"
    payload = json.loads(stored.read_text(encoding="utf-8"))
    assert payload["allocation_sha256"] == "a" * 64
    assert "report_sha256" not in payload
    with pytest.raises(ValueError, match="rotation reservation is invalid"):
        trend_review.reserve_rotation_pairs(
            tmp_path, market="US", account_key="simulate-102",
            execution_date="2026-08-04", pairs=[], allocation_sha256="b" * 64,
            reserved_at="2026-08-03T16:50:00+08:00",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strength_gap", "NaN"),
        ("strength_gap", "79"),
        ("target_weight", "0"),
        ("target_amount", "-1"),
        ("estimated_shares", 0),
        ("estimated_shares", 41),
        ("atr", "0"),
    ],
)
def test_rotation_reservation_rejects_malformed_sizing(
    tmp_path: Path, field: str, value: object,
) -> None:
    pair = {
        "pair_index": 0,
        "sell_symbol": "WEAK",
        "sell_name": "WEAK",
        "sell_futu_symbol": "US.WEAK",
        "sell_global_strength": "10",
        "buy_symbol": "STRONG",
        "buy_name": "STRONG",
        "buy_futu_symbol": "US.STRONG",
        "buy_global_strength": "90",
        "strength_gap": "80",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 40,
        "lot_size": 10,
        "atr": "5",
        "reason": "relative_rotation",
    }
    pair[field] = value

    with pytest.raises(ValueError, match="rotation reservation facts are invalid"):
        trend_review.reserve_rotation_pairs(
            tmp_path,
            market="US",
            account_key="simulate-102",
            execution_date="2026-08-04",
            pairs=[pair],
            allocation_sha256="a" * 64,
            reserved_at="2026-08-03T16:20:00+08:00",
        )


def test_rotation_reservation_rejects_candidate_reuse_across_slots(
    tmp_path: Path,
) -> None:
    def pair(index: int, sell: str) -> dict[str, object]:
        return {
            "pair_index": index, "sell_symbol": sell, "sell_name": sell,
            "sell_futu_symbol": f"US.{sell}", "sell_global_strength": "10",
            "buy_symbol": "STRONG", "buy_name": "STRONG",
            "buy_futu_symbol": "US.STRONG", "buy_global_strength": "90",
            "strength_gap": "80", "target_weight": "0.04",
            "target_amount": "4000", "estimated_shares": 40,
            "lot_size": 1, "atr": "5", "reason": "relative_rotation",
        }

    with pytest.raises(ValueError, match="rotation reservation facts conflict"):
        trend_review.reserve_rotation_pairs(
            tmp_path, market="US", account_key="simulate-102",
            execution_date="2026-08-04",
            pairs=[pair(0, "WEAK1"), pair(1, "WEAK2")],
            allocation_sha256="a" * 64,
            reserved_at="2026-08-03T16:20:00+08:00",
        )


def test_rotation_revision_fills_only_the_unused_slot(tmp_path: Path) -> None:
    def pair(
        index: int, sell: str, buy: str, *, target_amount: str = "4000",
    ) -> dict[str, object]:
        return {
            "pair_index": index, "sell_symbol": sell, "sell_name": sell,
            "sell_futu_symbol": f"US.{sell}", "sell_global_strength": "10",
            "buy_symbol": buy, "buy_name": buy,
            "buy_futu_symbol": f"US.{buy}", "buy_global_strength": "90",
            "strength_gap": "80", "target_weight": "0.04",
            "target_amount": target_amount, "estimated_shares": 40,
            "lot_size": 1, "atr": "5", "reason": "relative_rotation",
        }

    first = trend_review.reserve_rotation_pairs(
        tmp_path, market="US", account_key="simulate-102",
        execution_date="2026-08-04", pairs=[pair(0, "WEAK1", "STRONG1")],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:20:00+08:00",
    )
    revised = trend_review.reserve_rotation_pairs(
        tmp_path, market="US", account_key="simulate-102",
        execution_date="2026-08-04",
        pairs=[
            pair(0, "WEAK1", "STRONG1", target_amount="3999"),
            pair(1, "WEAK2", "STRONG2"),
        ],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:30:00+08:00",
    )

    assert [item["pair_index"] for item in first] == [0]
    assert [
        (item["pair_index"], item["sell_symbol"], item["buy_symbol"])
        for item in revised
    ] == [(0, "WEAK1", "STRONG1"), (1, "WEAK2", "STRONG2")]


def test_rotation_revision_discards_a_retained_pair_moved_to_another_slot(
    tmp_path: Path,
) -> None:
    def pair(index: int, sell: str, buy: str) -> dict[str, object]:
        return {
            "pair_index": index, "sell_symbol": sell, "sell_name": sell,
            "sell_futu_symbol": f"US.{sell}", "sell_global_strength": "10",
            "buy_symbol": buy, "buy_name": buy,
            "buy_futu_symbol": f"US.{buy}", "buy_global_strength": "90",
            "strength_gap": "80", "target_weight": "0.04",
            "target_amount": "4000", "estimated_shares": 40,
            "lot_size": 1, "atr": "5", "reason": "relative_rotation",
        }

    trend_review.reserve_rotation_pairs(
        tmp_path, market="US", account_key="simulate-102",
        execution_date="2026-08-04", pairs=[pair(0, "WEAK1", "STRONG1")],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:20:00+08:00",
    )
    revised = trend_review.reserve_rotation_pairs(
        tmp_path, market="US", account_key="simulate-102",
        execution_date="2026-08-04",
        pairs=[
            pair(0, "WEAK2", "STRONG2"),
            pair(1, "WEAK1", "STRONG1"),
        ],
        allocation_sha256="a" * 64,
        reserved_at="2026-08-03T16:30:00+08:00",
    )

    assert [
        (item["pair_index"], item["sell_symbol"], item["buy_symbol"])
        for item in revised
    ] == [(0, "WEAK1", "STRONG1"), (1, "WEAK2", "STRONG2")]


def test_cn_historical_and_current_snapshots_normalize_without_cross_version_rewrite() -> None:
    old = live_trend_strategy_snapshot(
        "CN",
        "abc123",
        (622466, 697199),
        strategy_version="v4",
    )
    historic_v6 = live_trend_strategy_snapshot(
        "CN",
        "abc123",
        (622466, 697199),
        strategy_version="v6",
    )
    current = live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )

    assert trend_review.normalize_trend_strategy_snapshot(old, "CN") == old
    assert (
        trend_review.normalize_trend_strategy_snapshot(historic_v6, "CN")
        == historic_v6
    )
    assert (
        trend_review.normalize_trend_strategy_snapshot(current, "CN")
        == current
    )
    assert old["parameters"]["max_filter_price"] == "200"
    assert "kelly_sample_inherits" not in historic_v6["parameters"]
    assert current["strategy_version"] == "v10"
    assert "v13" in trend_review.TREND_STRATEGY_VERSIONS
    assert "max_filter_price" not in current["parameters"]
    assert current["parameters"]["kelly_sample_inherits"][0][
        "opening_strategy_version"
    ] == "v4"


@pytest.mark.parametrize("strategy_version", ["v8", "v9"])
def test_risk_aware_buy_completion_accepts_current_and_legacy_versions(
    strategy_version: str,
) -> None:
    action = {
        "lot_size": 100,
        "estimated_shares": 400,
        "target_amount": "4000",
        "atr": "0.5",
        "planned_stop_risk": "101",
    }
    report = {
        "metadata": {"price_fx_to_account_currency": "1"},
        "risk_summary": {"normal_cost_rate": "0.001"},
        "strategy_snapshot": {"strategy_version": strategy_version},
    }
    snapshot = {"available_cash": "100000"}

    assert trend_review._remaining_buy_quantity(
        action, report, snapshot, (), Decimal("10")
    ) == 100


def frozen_evidence() -> dict[str, object]:
    return {
        "market": "CN",
        "report_id": "2026-07-16",
        "query": {
            "component_pool_ids": [622466, 697199],
            "snapshot_fields": ["tmId"],
        },
        "responses": {
            "components": [{"tmId": 1}],
            "snapshots": [{"tmId": 1}],
        },
        "market_data": {
            "SH.600001": [{"date": "2026-07-16", "close": "10"}]
        },
        "account": {"net_value": "100000"},
        "strategy_snapshot": {"strategy_version": "v1"},
        "process_version": "oldsha",
    }


def test_freeze_and_replay_never_overwrite_original(tmp_path: Path) -> None:
    reference = trend_review.freeze_trend_evidence(tmp_path, frozen_evidence())
    evidence_path = Path(reference["path"])
    original = evidence_path.read_bytes()

    assert trend_review.freeze_trend_evidence(
        tmp_path, frozen_evidence()
    ) == reference
    corrected = trend_review.replay_trend_evidence(
        evidence_path,
        tmp_path,
        fixed_process_version="newsha",
        rebuild=lambda frozen: {
            "status": "corrected",
            "source": frozen["report_id"],
            "process_version": frozen["process_version"],
        },
        replayed_at="2026-07-17T09:00:00+08:00",
    )

    assert evidence_path.read_bytes() == original
    payload = json.loads(corrected.read_text(encoding="utf-8"))
    assert payload["original_evidence_sha256"] == reference["sha256"]
    assert payload["corrected_report"]["process_version"] == "newsha"
    assert corrected.parent.name == "CN"


def test_different_evidence_never_replaces_existing_file(tmp_path: Path) -> None:
    first = trend_review.freeze_trend_evidence(tmp_path, frozen_evidence())
    changed = frozen_evidence()
    changed["report_id"] = "2026-07-17"
    second = trend_review.freeze_trend_evidence(tmp_path, changed)

    assert first["path"] != second["path"]
    assert Path(first["path"]).exists()
    assert Path(second["path"]).exists()


def test_rebuild_marks_missing_original_input_instead_of_guessing() -> None:
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="missing original input: rebuild_inputs",
    ):
        trend_review.rebuild_trend_report_from_evidence(frozen_evidence())


def test_rebuild_uses_only_frozen_inputs_and_fixed_process_version() -> None:
    snapshot = trend_strategy_snapshot("CN", "oldsha", (622466, 697199))
    evidence = {
        **frozen_evidence(),
        "process_version": "newsha",
        "strategy_snapshot": snapshot,
        "rebuild_inputs": {
            "as_of_date": "2026-07-16",
            "execution_date": "2026-07-17",
            "account": {
                "source_date": "2026-07-16",
                "fresh": True,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [],
                "exceptions": [],
                "position_count": 0,
            },
            "candidates": [],
            "holding_snapshots": {},
            "bars_by_symbol": {},
            "prior_state": {"schema_version": 1, "positions": {}},
            "watch_events": [],
            "api_facts": ["frozen"],
            "data_sources": ["frozen"],
            "estimated_api_cost": None,
            "actual_api_cost": None,
            "market": "CN",
            "lot_sizes": {},
            "position_weight": "0.04",
            "position_weight_source": "fallback_4pct",
            "price_fx_to_account_currency": "1",
            "normal_cost_rate": "0.001",
            "candidate_pool_ids": [622466, 697199],
            "generated_at": "2026-07-16T17:00:00+08:00",
            "metadata": {"market": "CN", "broker": "eastmoney"},
            "kelly_rounds": [],
            "kelly_data_reason": "",
        },
    }

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt["process_version"] == "newsha"
    assert rebuilt["strategy_snapshot"]["process_version"] == "newsha"
    assert rebuilt["account"]["net_value"] == "100000"


def test_rebuild_uses_frozen_allocation_daily_bytes_not_latest_pointer(
    tmp_path: Path,
) -> None:
    roots = {
        market: {
            role: {
                "asset": asset,
                "tm_id": index * 10 + role_index,
                "as_of_date": "2026-08-03",
                "global_strength": strength,
            }
            for role_index, (role, asset, strength) in enumerate(
                (("stock", stock, stock_strength), ("etf", etf, etf_strength))
            )
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (
                ("CN", "A股", "ETF基金", "90", "80"),
                ("HK", "港股", "香港ETF", "70", "60"),
                ("US", "美股", "美国ETF", "50", "40"),
            ),
            1,
        )
    }
    snapshot = {
        "version": 1,
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "generator_version": "trend-allocation-v1",
        "git_sha": "a" * 40,
        "roots": roots,
        "markets": {
            "CN": {"rank": 1, "score": "90", "score_source": "A股", "entry_weight": "0.06", "nominal_weight": "0.60"},
            "HK": {"rank": 2, "score": "70", "score_source": "港股", "entry_weight": "0.04", "nominal_weight": "0.40"},
            "US": {"rank": 3, "score": "50", "score_source": "美股", "entry_weight": "0.02", "nominal_weight": "0.20"},
        },
    }
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    daily = tmp_path / "trend_allocation/daily/2026-08-03.json"
    daily.parent.mkdir(parents=True)
    daily.write_text(body, encoding="utf-8")
    allocation = {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "snapshot": snapshot,
    }
    strategy = live_trend_strategy_snapshot(
        "CN", "oldsha", (622466, 697199), allocation=allocation,
    )
    drawdown = automatic_bootstrap_strategy_drawdown(
        tmp_path,
        market="CN",
        strategy_id=str(strategy["strategy_id"]),
        strategy_version=str(strategy["strategy_version"]),
        parameters=strategy["parameters"],
        baseline_equity=Decimal("100000"),
        source_date="2026-07-16",
        accepted_git_sha="a" * 40,
        actor="pytest",
        occurred_at="2026-07-16T18:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-17",
        entry_date="2026-07-17",
    )
    report = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=AccountSnapshot("2026-07-16", True, Decimal("100000"), Decimal("100000"), (), ()),
        candidates=(), holding_snapshots={}, bars_by_symbol={},
        market="CN", process_version="oldsha", strategy_snapshot=strategy,
        metadata={"process_version": "oldsha"},
        allocation_reference=allocation,
        drawdown_summary=drawdown,
    )
    source = _report_payload(report)
    frozen = trend_review.freeze_report_evidence(
        data_dir=tmp_path, report=report, candidates=(), holding_snapshots={},
        bars_by_symbol={}, prior_state={"schema_version": 1, "positions": {}},
        watch_events=(), query={}, responses={}, candidate_pool_ids=(622466, 697199),
        lot_sizes={}, price_fx_to_account_currency=Decimal("1"),
        previous_attention_rows=(), option_attention_broker_label=None,
    )
    evidence = json.loads(Path(frozen["path"]).read_text(encoding="utf-8"))
    assert evidence["rebuild_inputs"]["allocation"]["daily_json"] == body
    latest = tmp_path / "trend_allocation/latest.json"
    latest.write_text(json.dumps({"daily_path": "data/trend_allocation/daily/later.json", "sha256": "0" * 64}), encoding="utf-8")

    assert trend_review.rebuild_trend_report_from_evidence(evidence) == source


def test_rebuild_preserves_excluded_real_holding_reason(tmp_path: Path) -> None:
    strategy = trend_strategy_snapshot("US", "oldsha", (1,))
    real_input = RealHoldingInput(
        status="available",
        reason="",
        source={"broker": "tiger"},
        positions=(
            AccountPosition(
                symbol="US.AGRZ",
                name="AGRZ",
                asset_class="etf",
                quantity=Decimal("1"),
                avg_cost_price=Decimal("20"),
                market_value=Decimal("20"),
            ),
        ),
        holding_snapshots={"US.AGRZ": None},
        bars_by_symbol={"US.AGRZ": None},
        prior_state=None,
        trend_excluded_symbols=("US.AGRZ",),
    )
    report = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=AccountSnapshot(
            source_date="2026-07-16",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
            position_count=0,
        ),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        generated_at="2026-07-16T17:00:00+08:00",
        metadata={"market": "US", "broker": "tiger"},
        market="US",
        process_version="oldsha",
        candidate_pool_ids=(1,),
        strategy_snapshot=strategy,
        real_holdings=real_input,
    )
    frozen = trend_review.freeze_report_evidence(
        data_dir=tmp_path,
        report=report,
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state={"schema_version": 1, "positions": {}},
        watch_events=(),
        query={"component_pool_ids": [1]},
        responses={},
        candidate_pool_ids=(1,),
        lot_sizes={},
        price_fx_to_account_currency=Decimal("1"),
        previous_attention_rows=[],
        option_attention_broker_label="老虎",
        real_holdings_input=real_input,
    )
    evidence = json.loads(Path(frozen["path"]).read_text(encoding="utf-8"))

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt["strategy_judgments"]["real_holding_decisions"][0][
        "reason"
    ] == "holding_trend_excluded"


@pytest.mark.parametrize("strategy_version", ["v4", "v9"])
def test_risk_version_rebuild_uses_frozen_drawdown_decision_after_live_state_changes(
    tmp_path: Path, strategy_version: str,
) -> None:
    snapshot = live_trend_strategy_snapshot(
        "CN",
        "oldsha",
        (622466, 697199),
        strategy_version=strategy_version,
    )
    drawdown = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": "CN",
        "strategy_id": snapshot["strategy_id"],
        "strategy_version": strategy_version,
        "kelly_sample_key": f"CN|trend_animals_warm_to_hot/CN/{strategy_version}|{strategy_version}",
        "state_status": "ok",
        "status": "active",
        "status_label": "纪律内",
        "entry_allowed": True,
        "current_equity": "100000",
        "high_water_mark": "100000",
        "drawdown_pct": "0",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "",
        "paused_at": None,
        "observed_at": "2026-07-16T17:00:00+08:00",
        "bootstrap_event": None,
        "recovery_event": None,
    }
    account = AccountSnapshot(
        source_date="2026-07-16",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        positions=(),
        exceptions=(),
        position_count=0,
    )
    report = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account,
        candidates=[],
        holding_snapshots={},
        bars_by_symbol={},
        generated_at="2026-07-16T17:00:00+08:00",
        metadata={
            "market": "CN", "broker": "eastmoney", "process_version": "oldsha"
        },
        process_version="oldsha",
        candidate_pool_ids=(622466, 697199),
        strategy_snapshot=snapshot,
        drawdown_summary=drawdown,
    )
    source = _report_payload(report)
    frozen = trend_review.freeze_report_evidence(
        data_dir=tmp_path,
        report=report,
        candidates=[],
        holding_snapshots={},
        bars_by_symbol={},
        prior_state={"schema_version": 1, "positions": {}},
        watch_events=[],
        query={"component_pool_ids": [622466, 697199]},
        responses={},
        candidate_pool_ids=(622466, 697199),
        lot_sizes={},
        price_fx_to_account_currency=Decimal("1"),
        previous_attention_rows=[],
        option_attention_broker_label=None,
    )
    evidence = json.loads(Path(frozen["path"]).read_text(encoding="utf-8"))

    automatic_bootstrap_strategy_drawdown(
        tmp_path,
        market="CN",
        strategy_id=str(snapshot["strategy_id"]),
        strategy_version=strategy_version,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100000"),
        source_date="2026-07-16",
        accepted_git_sha="a" * 40,
        occurred_at="2026-07-16T17:01:00+08:00",
        actor="ray",
        reason="first_activation",
        entry_eligible_from="2026-07-17",
    )
    observe_strategy_equity(
        tmp_path,
        market="CN",
        strategy_id=str(snapshot["strategy_id"]),
        strategy_version=strategy_version,
        current_equity=Decimal("90000"),
        observed_at="2026-07-17T17:00:00+08:00",
    )

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt == source
    assert rebuilt["drawdown_summary"] == drawdown
    missing = json.loads(json.dumps(evidence))
    del missing["rebuild_inputs"]["drawdown_summary"]
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="missing original input: drawdown_summary",
    ):
        trend_review.rebuild_trend_report_from_evidence(missing)


def test_v1_rebuild_keeps_legacy_nominal_sizing_without_v2_risk_fields() -> None:
    snapshot = trend_strategy_snapshot("US", "oldsha", (622460,))
    snapshot = {
        **snapshot,
        "strategy_id": "trend_animals_warm_to_hot/US/v1",
        "strategy_version": "v1",
        "effective_from": "2026-07-14",
        "parameters": {
            key: value
            for key, value in snapshot["parameters"].items()
            if key
            not in {
                "single_entry_risk_limit",
                "portfolio_risk_limit",
                "abnormal_loss_buffer",
                "normal_cost_rate",
                "normal_cost_model",
            }
        },
    }
    evidence = {
        **frozen_evidence(),
        "market": "US",
        "strategy_snapshot": snapshot,
        "rebuild_inputs": {
            "as_of_date": "2026-07-16",
            "execution_date": "2026-07-17",
            "account": {
                "source_date": "2026-07-16",
                "fresh": True,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [],
                "exceptions": [],
                "position_count": 0,
            },
            "candidates": [
                {
                    "tm_id": 1,
                    "symbol": "AAPL",
                    "exchange": "US",
                    "name": "Apple",
                    "asset": "US stock",
                    "industry": "Technology",
                    "as_of_date": "2026-07-16",
                    "tradable": True,
                    "amount": "2",
                    "right_side": True,
                    "days": 3,
                    "strength": "96",
                    "danger": False,
                    "close": "100",
                    "atr": "5",
                }
            ],
            "holding_snapshots": {},
            "bars_by_symbol": {},
            "prior_state": {"schema_version": 1, "positions": {}},
            "watch_events": [],
            "market": "US",
            "lot_sizes": {},
            "position_weight": "0.04",
            "position_weight_source": "fallback_4pct",
            "price_fx_to_account_currency": "1",
            "candidate_pool_ids": [622460],
            "generated_at": "2026-07-16T17:00:00+08:00",
            "metadata": {"market": "US", "broker": "tiger"},
            "managed_symbols": [],
            "option_attention": {
                "previous_rows": [],
                "broker_label": "老虎",
            },
        },
    }

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    action = rebuilt["strategy_judgments"]["formal_actions"][0]
    assert action["estimated_shares"] == 40
    assert action["target_amount"] == "4000.00"
    assert "planned_stop_risk" not in action
    assert "risk_skips" not in rebuilt["strategy_judgments"]
    assert "risk_summary" not in rebuilt


def test_us_replay_preserves_position_cap_fx_quantity_and_option_attention(
    tmp_path: Path,
) -> None:
    candidates = [
        CandidateInput(
            tm_id=index,
            symbol=symbol,
            exchange="US",
            name=symbol,
            asset="US stock",
            industry="Technology",
            as_of_date="2026-07-16",
            tradable=True,
            amount=Decimal("2"),
            right_side=True,
            days=index,
            strength=Decimal(str(97 - index)),
            danger=False,
            close=Decimal("100"),
            atr=Decimal("5"),
            temperature_curr="热",
            phase_curr="夏至",
            strength_change="上升",
            boiling=False,
            champagne=False,
        )
        for index, symbol in enumerate(("AAPL", "MSFT"), start=1)
    ]
    account = AccountSnapshot(
        source_date="2026-07-16",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        positions=(),
        exceptions=(),
        position_count=9,
    )
    report = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account,
        candidates=candidates,
        holding_snapshots={},
        bars_by_symbol={},
        generated_at="2026-07-16T17:00:00+08:00",
        metadata={"market": "US", "broker": "tiger"},
        market="US",
        price_fx_to_account_currency=Decimal("7.85"),
        process_version="oldsha",
        candidate_pool_ids=(1,),
    )
    source = _report_payload(report)
    current_rows = market_trend._attention_rows(source["signal_snapshots"]) or []
    previous_rows = [
        {
            **row,
            "right_side": False,
            "temperature_curr": "温",
            "strength_change": "下降",
        }
        for row in current_rows
    ]
    source["option_attention"] = market_trend.build_option_attention(
        current_rows,
        previous_rows,
        market_trend._attention_actions(source),
        "US",
        "老虎",
    )
    frozen = trend_review.freeze_report_evidence(
        data_dir=tmp_path,
        report=report,
        candidates=candidates,
        holding_snapshots={},
        bars_by_symbol={},
        prior_state={"schema_version": 1, "positions": {}},
        watch_events=[],
        query={"component_pool_ids": [1], "snapshot_fields": []},
        responses={},
        candidate_pool_ids=(1,),
        lot_sizes={},
        price_fx_to_account_currency=Decimal("7.85"),
        previous_attention_rows=previous_rows,
        option_attention_broker_label="老虎",
    )
    evidence = json.loads(Path(frozen["path"]).read_text(encoding="utf-8"))
    assert evidence["rebuild_inputs"]["normal_cost_rate"] == "0.001"

    missing_fx = json.loads(json.dumps(evidence))
    del missing_fx["rebuild_inputs"]["price_fx_to_account_currency"]
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="missing original input: price_fx_to_account_currency",
    ):
        trend_review.rebuild_trend_report_from_evidence(missing_fx)
    missing_count = json.loads(json.dumps(evidence))
    del missing_count["rebuild_inputs"]["account"]["position_count"]
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="missing original input: account.position_count",
    ):
        trend_review.rebuild_trend_report_from_evidence(missing_count)
    missing_cost = json.loads(json.dumps(evidence))
    del missing_cost["rebuild_inputs"]["normal_cost_rate"]
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="missing original input: normal_cost_rate",
    ):
        trend_review.rebuild_trend_report_from_evidence(missing_cost)
    changed_cost = json.loads(json.dumps(evidence))
    changed_cost["rebuild_inputs"]["normal_cost_rate"] = "0.003"
    with pytest.raises(
        ValueError,
        match="strategy snapshot does not match report actions",
    ):
        trend_review.rebuild_trend_report_from_evidence(changed_cost)

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    source_actions = source["strategy_judgments"]["formal_actions"]
    rebuilt_actions = rebuilt["strategy_judgments"]["formal_actions"]
    assert rebuilt["account"]["position_count"] == 9
    assert len(rebuilt_actions) == len(source_actions) == 1
    assert rebuilt_actions[0]["estimated_shares"] == source_actions[0]["estimated_shares"] == 5
    assert rebuilt["option_attention"] == source["option_attention"]

    corrected_path = trend_review.replay_trend_evidence(
        Path(frozen["path"]),
        tmp_path,
        fixed_process_version="fixedsha",
        rebuild=trend_review.rebuild_trend_report_from_evidence,
        replayed_at="2026-07-17T09:00:00+08:00",
    )
    corrected = json.loads(corrected_path.read_text(encoding="utf-8"))["corrected_report"]
    assert corrected["process_version"] == "fixedsha"
    assert corrected["strategy_judgments"]["formal_actions"] == source_actions
    assert corrected["option_attention"] == source["option_attention"]


def test_rebuild_preserves_frozen_industry_context_ordering_facts(tmp_path: Path) -> None:
    context = IndustryContext(
        industry_tm_id=621707,
        industry="行业621707",
        as_of_date="2026-07-16",
        component_count=20,
        snapshot_count=20,
        tradable_count=20,
        valid_count=20,
        right_count=10,
        snapshot_coverage=Decimal("1"),
        right_state_coverage=Decimal("1"),
        right_share=Decimal("0.5"),
        warm_to_hot_count=5,
        temperature="热",
        strength=Decimal("90"),
        valid=True,
        invalid_reasons=(),
        member_breadth_collected=False,
        prior_as_of_date="2026-07-15",
        prior_temperature="温",
        prior_right_share=Decimal("0.4"),
        aggregate_right_count_ratio=Decimal("0.51"),
        aggregate_right_market_cap_ratio=Decimal("0.64"),
        prior_aggregate_right_count_ratio=Decimal("0.45"),
        prior_aggregate_right_market_cap_ratio=Decimal("0.58"),
        temperature_direction="rising",
        right_share_change_pp=Decimal("10"),
    )
    candidate = CandidateInput(
        tm_id=1,
        symbol="600001",
        exchange="SH",
        name="示例",
        asset="A股",
        industry="行业621707",
        as_of_date="2026-07-16",
        tradable=True,
        amount=Decimal("2"),
        right_side=True,
        days=3,
        strength=Decimal("96"),
        danger=False,
        close=Decimal("10"),
        atr=Decimal("0.5"),
        industry_tm_id=621707,
        industry_temperature="热",
        market_cap=Decimal("100"),
        temperature_prev="温",
        temperature_curr="热",
        phase="立夏",
    )
    account = AccountSnapshot(
        source_date="2026-07-16",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        positions=(),
        exceptions=(),
        position_count=0,
    )
    report = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account,
        candidates=(candidate,),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"market": "CN", "broker": "eastmoney"},
        process_version="oldsha",
        candidate_pool_ids=(1,),
        industry_contexts=(context,),
        estimated_api_cost=Decimal("0.479"),
        actual_api_cost=Decimal("0.610"),
        estimated_api_cost_complete=False,
    )
    source = _report_payload(report)
    frozen = trend_review.freeze_report_evidence(
        data_dir=tmp_path,
        report=report,
        candidates=(candidate,),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state={"schema_version": 1, "positions": {}},
        watch_events=[],
        query={"component_pool_ids": [1], "snapshot_fields": []},
        responses={},
        candidate_pool_ids=(1,),
        lot_sizes={},
        price_fx_to_account_currency=Decimal("1"),
        previous_attention_rows=[],
        option_attention_broker_label=None,
    )
    evidence = json.loads(Path(frozen["path"]).read_text(encoding="utf-8"))

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt["industry_contexts"] == source["industry_contexts"]
    assert rebuilt["industry_context_status"] == source["industry_context_status"]
    assert rebuilt["api_cost"] == source["api_cost"]
    assert [
        item["symbol"] for item in rebuilt["strategy_judgments"]["top10_candidates"]
    ] == [
        item["symbol"] for item in source["strategy_judgments"]["top10_candidates"]
    ]
    assert rebuilt["strategy_judgments"]["top10_candidates"][0]["ordering_context"] == source[
        "strategy_judgments"
    ]["top10_candidates"][0]["ordering_context"]


class FakeTrendSimClient:
    def __init__(
        self,
        *,
        nav: str = "100000",
        cash: str = "100000",
        positions: list[dict[str, object]] | None = None,
        fail_orders: int = 0,
        accepted_before_failure: bool = False,
    ) -> None:
        self.nav = nav
        self.cash = cash
        self.positions = positions or []
        self.requests: list[dict[str, object]] = []
        self.orders: list[dict[str, object]] = []
        self.list_order_calls: list[dict[str, object]] = []
        self.fail_orders = fail_orders
        self.accepted_before_failure = accepted_before_failure

    def account_snapshot(self) -> dict[str, object]:
        return {
            "acc_id": 101,
            "net_value": self.nav,
            "cash": self.cash,
            "positions": self.positions,
        }

    def place_order(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        order_id = f"SIM-{len(self.requests)}"
        broker_order = {
            **request,
            "order_id": order_id,
            "code": request["futu_code"],
            "trd_side": str(request["side"]).upper(),
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        }
        if self.fail_orders:
            self.fail_orders -= 1
            if self.accepted_before_failure:
                self.orders.append(broker_order)
            raise RuntimeError("place order failed")
        self.orders.append(broker_order)
        return {
            "futu_order_id": order_id,
            "status": "submitted",
        }

    def list_orders(self, **kwargs: object) -> dict[str, object]:
        self.list_order_calls.append(dict(kwargs))
        return {"orders": self.orders}


def relative_rotation_pair(
    *, index: int = 0, sell: str = "WEAK", buy: str = "STRONG",
) -> dict[str, object]:
    return {
        "pair_index": index,
        "sell_symbol": sell,
        "sell_name": sell.title(),
        "sell_futu_symbol": f"SH.{sell}",
        "sell_global_strength": "10",
        "buy_symbol": buy,
        "buy_name": buy.title(),
        "buy_futu_symbol": f"SH.{buy}",
        "buy_global_strength": "90",
        "strength_gap": "80",
        "target_weight": "0.06",
        "target_amount": "6000",
        "estimated_shares": 600,
        "lot_size": 100,
        "atr": "0.1",
        "reason": "relative_rotation",
        "execution_date": "2026-07-20",
        "execution_mode": "automatic",
    }


def relative_rotation_report(
    *,
    pairs: list[dict[str, object]] | None = None,
    real_pairs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "execution_date": "2026-07-20",
        "metadata": {
            "price_fx_to_account_currency": "1",
            "simulate_acc_id": 101,
        },
        "risk_summary": {
            "normal_cost_rate": "0.001",
            "portfolio_remaining_risk": "4000",
        },
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v11",
            "strategy_version": "v11",
        },
        "strategy_judgments": {
            "formal_actions": [],
            "simulate_rotation_pairs": (
                pairs if pairs is not None else [relative_rotation_pair()]
            ),
            "real_rotation_pairs": real_pairs if real_pairs is not None else [],
        },
    }


def full_rotation_positions() -> list[dict[str, object]]:
    return [
        {
            "code": "SH.WEAK",
            "qty": "1000",
            "can_sell_qty": "1000",
            "market_val": "7000",
        },
        *[
            {
                "code": f"SH.HOLD{index}",
                "qty": "100",
                "can_sell_qty": "100",
                "market_val": "1000",
            }
            for index in range(1, 10)
        ],
    ]


def test_relative_rotation_sells_full_market_then_refreshes_and_buys_market(
    tmp_path: Path,
) -> None:
    class FilledClient(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"],
                "dealt_avg_price": "7" if request["side"] == "SELL" else "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [
                    item for item in self.positions if item["code"] != "SH.WEAK"
                ]
                self.cash = "6993"
            return response

    client = FilledClient(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report(real_pairs=[{
        **relative_rotation_pair(), "execution_mode": "manual",
    }])
    report["strategy_judgments"]["simulate_rotation_pairs"][0][
        "opening_strategy_version"
    ] = "v10"

    for minute, price in ((30, "5"), (31, "10")):
        result = trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal(price)},
        )

    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]
    assert all(request["order_type"] == "MARKET" for request in client.requests)
    assert client.requests[0]["qty"] == "1000"
    assert client.requests[1]["qty"] == "600"
    assert result["status"] == "complete"
    assert trend_review.relative_rotations_completed(
        tmp_path, report=report, market="CN", execution_date="2026-07-20"
    )
    sell_fact = next(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/sell-filled.json"
    ))
    sell = json.loads(sell_fact.read_text(encoding="utf-8"))
    assert (sell["exit_reason"], sell["opening_strategy_version"], sell["closing_strategy_version"]) == (
        "relative_rotation", "v10", "v11",
    )
    buy_fact = next(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/buy-filled.json"
    ))
    assert json.loads(buy_fact.read_text(encoding="utf-8"))["opening_strategy_version"] == "v11"

    merged = trend_review._merge_rotation_orders(
        {"orders": [dict(client.orders[0])]}, tmp_path, "CN", "2026-07-20"
    )
    assert merged["orders"][0]["pair_key"]
    stale_status = dict(client.orders[0])
    stale_status["order_status"] = "SUBMITTED"
    upgraded = trend_review._merge_rotation_orders(
        {"orders": [stale_status]}, tmp_path, "CN", "2026-07-20"
    )
    assert upgraded["orders"][0]["status"] == "FILLED"
    prior_buy = {
        "order_id": "prior-weak-buy", "code": "SH.WEAK", "trd_side": "BUY",
        "qty": "1000", "dealt_qty": "1000", "order_status": "FILLED_ALL",
    }
    completed = trend_review._completed_trades([{
        "date": "2026-07-20",
        "orders": trend_review._merge_rotation_orders(
            {"orders": [prior_buy, stale_status]}, tmp_path, "CN", "2026-07-20"
        )["orders"],
    }])
    assert completed and completed[0]["symbol"] == "WEAK"
    conflicting = dict(client.orders[0])
    conflicting["dealt_qty"] = "999"
    with pytest.raises(ValueError, match="conflicting rotation fill order identity"):
        trend_review._merge_rotation_orders(
            {"orders": [conflicting]}, tmp_path, "CN", "2026-07-20"
        )


def test_relative_rotation_named_pending_facts_are_replayed_and_idempotent(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    for _ in range(2):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20", now="2026-07-20T10:30:00+08:00",
            quote_prices={},
        )
    assert len(list(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/preflight-quote-pending.json"
    ))) == 1
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"
    ))
    pending_path = next(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/preflight-quote-pending.json"
    ))
    malformed = pending_path.parent / "arbitrary-pending.json"
    malformed.write_text(
        pending_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid relative rotation fact"):
        trend_review._rotation_events(pending_path.parent)

    class SellWithoutRefresh(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "7",
                "order_status": "FILLED_ALL",
            })
            return response

    client = SellWithoutRefresh(cash="0", positions=full_rotation_positions())
    for minute in (30, 31, 32):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path / "post-sell", report=report, client=client, market="CN",
            execution_date="2026-07-20", now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )
    assert len(list((tmp_path / "post-sell").glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/post-sell-account-pending.json"
    ))) == 1


def test_relative_rotation_events_bind_the_pair_path(
    tmp_path: Path,
) -> None:
    report = relative_rotation_report()
    report_sha = trend_review._report_hash(report)
    pair_key = trend_review._rotation_pair_key("CN", 101, "2026-07-20", report_sha, 0)
    payload = {
        "schema_version": "open_trader.trend_review.rotation.v1",
        "kind": "terminal", "market": "CN", "account_id": 101,
        "execution_date": "2026-07-20", "report_sha256": report_sha,
        "pair_index": 0, "pair_key": pair_key, "status": "complete",
    }
    sibling = (
        tmp_path / "trend_review/ledgers/CN/rotations/2026-07-20"
        / ("f" * 64)
    )
    sibling.mkdir(parents=True)
    (sibling / "terminal.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid relative rotation fact"):
        trend_review._rotation_events(sibling)


def test_relative_rotation_events_reject_incomplete_fill_sidecar(
    tmp_path: Path,
) -> None:
    report = relative_rotation_report()
    report_sha = trend_review._report_hash(report)
    pair_key = trend_review._rotation_pair_key("CN", 101, "2026-07-20", report_sha, 0)
    root = (
        tmp_path / "trend_review/ledgers/CN/rotations/2026-07-20" / pair_key
    )
    root.mkdir(parents=True)
    request = {
        "market": "CN", "futu_code": "SH.WEAK", "side": "SELL",
        "qty": "1000", "remark": "rotation:test",
    }
    payload = {
        "schema_version": "open_trader.trend_review.rotation.v1",
        "kind": "sell_fill", "status": "filled", "market": "CN",
        "account_id": 101, "execution_date": "2026-07-20",
        "report_sha256": report_sha, "pair_index": 0, "pair_key": pair_key,
        "sell_futu_symbol": "SH.WEAK", "buy_futu_symbol": "SH.STRONG",
        "target_qty": "1000", "filled_qty": "1000", "request": request,
        "order": {
            "order_id": "bad-status", "code": "SH.WEAK", "trd_side": "SELL",
            "remark": "rotation:test", "qty": "1000", "dealt_qty": "1000",
            "order_status": "CANCELLED_PART",
        },
    }
    (root / "sell-filled.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid relative rotation fact"):
        trend_review._rotation_events(root)
    payload["order"]["status"] = "FILLED"
    payload["order"]["order_status"] = "FILLED_ALL"
    (root / "sell-filled.json").write_text(json.dumps(payload), encoding="utf-8")
    assert len(trend_review._rotation_events(root)) == 1
    payload["order"]["status"] = "SUBMITTED"
    with pytest.raises(ValueError, match="invalid relative rotation fact"):
        (root / "sell-filled.json").write_text(json.dumps(payload), encoding="utf-8")
        trend_review._rotation_events(root)


def test_relative_rotation_requires_exact_full_fill_and_no_sidecar_on_bad_quantity(
    tmp_path: Path,
) -> None:
    class MalformedFill(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": "not-a-quantity", "order_status": "FILLED_ALL",
            })
            return response

    client = MalformedFill(cash="0", positions=full_rotation_positions())
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=relative_rotation_report(), client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T10:30:00+08:00", quote_prices={"SH.STRONG": Decimal("10")},
    )
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/sell-filled.json"
    ))
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
    ))


def test_relative_rotation_repairs_action_attribution_after_sell_and_buy_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Filled(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            return response

    client = Filled(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    original = trend_review._write_rotation_action_event_once
    crashed = False

    def crash_sell(**kwargs: object) -> Path | None:
        nonlocal crashed
        if kwargs.get("side") == "sell" and not crashed:
            crashed = True
            raise RuntimeError("crash after sell action")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trend_review, "_write_rotation_action_event_once", crash_sell)
    with pytest.raises(RuntimeError, match="crash after sell action"):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20", now="2026-07-20T10:30:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )
    monkeypatch.setattr(trend_review, "_write_rotation_action_event_once", original)
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:31:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]
    assert trend_review.relative_rotations_completed(
        tmp_path, report=report, market="CN", execution_date="2026-07-20"
    )

    client = Filled(cash="0", positions=full_rotation_positions())
    tmp_path = tmp_path / "buy-crash"
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:30:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    crashed = False

    def crash_buy(**kwargs: object) -> Path | None:
        nonlocal crashed
        if kwargs.get("side") == "buy" and not crashed:
            crashed = True
            raise RuntimeError("crash after buy action")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trend_review, "_write_rotation_action_event_once", crash_buy)
    with pytest.raises(RuntimeError, match="crash after buy action"):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20", now="2026-07-20T10:31:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )
    monkeypatch.setattr(trend_review, "_write_rotation_action_event_once", original)
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:32:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]
    assert trend_review.relative_rotations_completed(
        tmp_path, report=report, market="CN", execution_date="2026-07-20"
    )


def test_relative_rotation_opening_version_uses_position_provenance_or_unknown(
    tmp_path: Path,
) -> None:
    pair = relative_rotation_pair()
    report = relative_rotation_report()
    details = trend_review._rotation_opening_strategy_details(
        tmp_path, market="CN", pair=pair, report=report,
        snapshot={"positions": [{
            "code": "SH.WEAK", "qty": "1000", "opening_strategy_version": "v9",
        }]},
    )
    assert details == ("v9", "account_position")
    unknown = trend_review._rotation_opening_strategy_details(
        tmp_path, market="CN", pair=pair, report=report,
        snapshot={"positions": [{"code": "SH.WEAK", "qty": "1000"}]},
    )
    assert unknown == ("unknown", "unattributed_existing_position")
    write_protection_state(
        tmp_path / "trend_a_share/protection_state.json",
        {"schema_version": 1, "positions": {
            "SH.WEAK": {"opening_strategy_version": "v8", "updated_for": "2026-07-20"},
        }},
    )
    protected = trend_review._rotation_opening_strategy_details(
        tmp_path, market="CN", pair=pair, report=report,
        snapshot={"positions": [{"code": "SH.WEAK", "qty": "1000"}]},
    )
    assert protected == ("v8", "protection_state")


def test_relative_rotation_opening_version_recovers_prior_buy_fact_after_snapshot_clears(
    tmp_path: Path,
) -> None:
    trend_review.freeze_discipline_fact(
        tmp_path,
        "CN",
        "2026-07-19",
        "100000",
        [{
            "order_id": "prior-buy",
            "code": "SH.WEAK",
            "trd_side": "BUY",
            "qty": "1000",
            "dealt_qty": "1000",
            "dealt_avg_price": "7",
            "order_status": "FILLED_ALL",
            "remark": "ordinary:prior-buy",
        }],
        {"strategy_id": "trend_animals_warm_to_hot/CN/v10", "strategy_version": "v10"},
    )
    pair = relative_rotation_pair()
    report = relative_rotation_report()
    details = trend_review._rotation_opening_strategy_details(
        tmp_path,
        market="CN",
        pair=pair,
        report=report,
        snapshot={"positions": []},
    )
    assert details == ("v10", "historical_discipline")


def test_relative_rotation_sell_sidecar_persists_historical_opening_version(
    tmp_path: Path,
) -> None:
    trend_review.freeze_discipline_fact(
        tmp_path,
        "CN",
        "2026-07-19",
        "100000",
        [{
            "order_id": "prior-buy",
            "code": "SH.WEAK",
            "trd_side": "BUY",
            "qty": "1000",
            "dealt_qty": "1000",
            "dealt_avg_price": "7",
            "order_status": "FILLED_ALL",
            "remark": "ordinary:prior-buy",
        }],
        {"strategy_id": "trend_animals_warm_to_hot/CN/v10", "strategy_version": "v10"},
    )

    class Filled(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            return response

    client = Filled(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20", now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )
    sell_fact = next(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/sell-filled.json"
    ))
    sell = json.loads(sell_fact.read_text(encoding="utf-8"))
    assert (sell["opening_strategy_version"], sell["opening_strategy_version_source"]) == (
        "v10", "historical_discipline"
    )


@pytest.mark.parametrize(
    ("positions", "source_date", "reason"),
    [
        (full_rotation_positions()[:-1], None, "account_not_full"),
        ([item for item in full_rotation_positions() if item["code"] != "SH.WEAK"], None, "weak_holding_absent"),
        (full_rotation_positions() + [{"code": "SH.STRONG", "qty": "1"}], None, "candidate_already_held"),
        (full_rotation_positions(), "2026-07-19", "stale_account_state"),
    ],
)
def test_relative_rotation_skips_invalid_live_account_once(
    tmp_path: Path,
    positions: list[dict[str, object]],
    source_date: str | None,
    reason: str,
) -> None:
    client = FakeTrendSimClient(positions=positions)
    if source_date is not None:
        original = client.account_snapshot
        client.account_snapshot = lambda: {**original(), "source_date": source_date}  # type: ignore[method-assign]

    for _ in range(2):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now="2026-07-20T10:30:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert client.requests == []
    terminal = list(tmp_path.glob("trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"))
    assert len(terminal) == 1
    assert json.loads(terminal[0].read_text(encoding="utf-8"))["reason"] == reason


def test_relative_rotation_zero_candidate_quantity_never_writes_sell_intent(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    pair = report["strategy_judgments"]["simulate_rotation_pairs"][0]
    pair["lot_size"] = 1000

    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:30:00+08:00",
        quote_prices={"SH.STRONG": Decimal("100")},
    )

    assert client.requests == []
    assert not list(tmp_path.glob("trend_review/ledgers/CN/rotations/**/*sell*intent.json"))


@pytest.mark.parametrize(
    ("status", "filled"),
    [("FILLED_PART", "100"), ("REJECTED", "0"), ("MYSTERY", "0")],
)
def test_relative_rotation_sell_without_full_proof_never_buys(
    tmp_path: Path, status: str, filled: str,
) -> None:
    class SellOutcome(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": filled, "dealt_avg_price": "7",
                "order_status": status,
            })
            return response

    client = SellOutcome(cash="0", positions=full_rotation_positions())
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests] == ["SELL"]


@pytest.mark.parametrize(
    ("buy_outcomes", "expected_buys", "terminal_status"),
    [
        ([('FILLED_PART', '100')], 1, "partial"),
        ([('REJECTED', '0')], 1, "failed"),
        ([('CANCELLED_ALL', '0'), ('FILLED_ALL', '600')], 2, "complete"),
        ([('CANCELLED_ALL', '0'), ('CANCELLED_ALL', '0')], 2, "incomplete"),
    ],
)
def test_relative_rotation_buy_terminal_and_single_retry_rules(
    tmp_path: Path,
    buy_outcomes: list[tuple[str, str]],
    expected_buys: int,
    terminal_status: str,
) -> None:
    class Outcomes(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            if request["side"] == "SELL":
                status, filled = "FILLED_ALL", request["qty"]
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            else:
                status, filled = buy_outcomes.pop(0)
            self.orders[-1].update({
                "dealt_qty": filled, "dealt_avg_price": "10",
                "order_status": status,
            })
            return response

    client = Outcomes(cash="0", positions=full_rotation_positions())
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests].count("BUY") == expected_buys
    terminal = next(tmp_path.glob("trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"))
    assert json.loads(terminal.read_text(encoding="utf-8"))["status"] == terminal_status


def test_relative_rotation_pair_failure_does_not_block_second_pair(
    tmp_path: Path,
) -> None:
    pairs = [
        relative_rotation_pair(index=0, sell="WEAK1", buy="NOQUOTE"),
        relative_rotation_pair(index=1, sell="WEAK2", buy="STRONG2"),
    ]
    pairs[0]["lot_size"] = 1000
    positions = [
        {"code": "SH.WEAK1", "qty": "1000", "can_sell_qty": "1000", "market_val": "7000"},
        {"code": "SH.WEAK2", "qty": "1000", "can_sell_qty": "1000", "market_val": "7000"},
        *[
            {"code": f"SH.HOLD{index}", "qty": "100", "can_sell_qty": "100", "market_val": "1000"}
            for index in range(1, 9)
        ],
    ]

    class FilledSecond(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != request["futu_code"]]
                self.cash = "6993"
            return response

    client = FilledSecond(cash="0", positions=positions)
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(pairs=pairs), client=client,
            market="CN", execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.NOQUOTE": Decimal("100"), "SH.STRONG2": Decimal("10")},
        )

    assert [request["futu_code"] for request in client.requests] == [
        "SH.WEAK2", "SH.STRONG2",
    ]


def test_relative_rotation_defers_sibling_until_ten_slots_are_restored(
    tmp_path: Path,
) -> None:
    pairs = [
        relative_rotation_pair(index=0, sell="WEAK1", buy="STRONG1"),
        relative_rotation_pair(index=1, sell="WEAK2", buy="STRONG2"),
    ]
    positions = [
        {"code": "SH.WEAK1", "qty": "1000", "can_sell_qty": "1000", "market_val": "7000"},
        {"code": "SH.WEAK2", "qty": "1000", "can_sell_qty": "1000", "market_val": "7000"},
        *[
            {"code": f"SH.HOLD{index}", "qty": "100", "can_sell_qty": "100", "market_val": "1000"}
            for index in range(1, 9)
        ],
    ]

    class Filled(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"],
                "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [
                    item for item in self.positions
                    if item["code"] != request["futu_code"]
                ]
                self.cash = str(7000 + Decimal(self.cash))
            else:
                self.positions = [
                    *self.positions,
                    {"code": request["futu_code"], "qty": request["qty"]},
                ]
                self.cash = str(Decimal(self.cash) - Decimal(str(request["qty"])) * 10)
            return response

    client = Filled(cash="0", positions=positions)
    report = relative_rotation_report(pairs=pairs)
    for minute in (30, 31, 32):
        result = trend_review.execute_relative_rotations(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG1": Decimal("10"), "SH.STRONG2": Decimal("10")},
        )

    assert [request["futu_code"] for request in client.requests] == [
        "SH.WEAK1", "SH.STRONG1", "SH.WEAK2", "SH.STRONG2",
    ]
    assert result["status"] == "complete"
    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("reason") == "account_not_full"
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"
        )
    )


def test_relative_rotation_sizing_releases_sold_holding_risk(
    tmp_path: Path,
) -> None:
    report = relative_rotation_report()
    report["risk_summary"]["portfolio_remaining_risk"] = "0"
    report["strategy_judgments"]["holding_decisions"] = [
        {
            "futu_symbol": position["code"],
            "close": "7000" if position["code"] == "SH.WEAK" else "1000",
            "active_line": "6999" if position["code"] == "SH.WEAK" else "999",
        }
        for position in full_rotation_positions()
    ]
    client = FakeTrendSimClient(cash="0", positions=full_rotation_positions())

    quantity = trend_review._rotation_quantity(
        report["strategy_judgments"]["simulate_rotation_pairs"][0],
        report,
        client.account_snapshot(),
        Decimal("10"),
        Decimal("6993"),
    )

    assert quantity > 0


def test_relative_rotation_completion_binds_account_and_pair_key(
    tmp_path: Path,
) -> None:
    report = relative_rotation_report()
    pair = report["strategy_judgments"]["simulate_rotation_pairs"][0]
    report_sha = trend_review._report_hash(report)
    wrong_account_key = trend_review._rotation_pair_key(
        "CN", 202, "2026-07-20", report_sha, pair["pair_index"]
    )
    wrong_root = (
        tmp_path / "trend_review/ledgers/CN/rotations/2026-07-20"
        / wrong_account_key
    )
    wrong_root.mkdir(parents=True)
    (wrong_root / "terminal.json").write_text(json.dumps({
        "schema_version": "open_trader.trend_review.rotation.v1",
        "kind": "terminal",
        "market": "CN",
        "account_id": 202,
        "execution_date": "2026-07-20",
        "report_sha256": report_sha,
        "pair_index": pair["pair_index"],
        "pair_key": wrong_account_key,
        "status": "complete",
    }), encoding="utf-8")

    assert not trend_review.relative_rotations_completed(
        tmp_path, report=report, market="CN", execution_date="2026-07-20"
    )


def test_relative_rotation_uncertainty_is_pending_not_completed(
    tmp_path: Path,
) -> None:
    class UnknownBuy(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"] if request["side"] == "SELL" else "0",
                "dealt_avg_price": "7",
                "order_status": "FILLED_ALL" if request["side"] == "SELL" else "MYSTERY",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            return response

    client = UnknownBuy(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:30:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    result = trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T10:31:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )

    assert result["status"] == "uncertain"
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"
    ))
    assert not trend_review.relative_rotations_completed(
        tmp_path, report=report, market="CN", execution_date="2026-07-20"
    )


def test_relative_rotation_post_sell_zero_quantity_keeps_cash(
    tmp_path: Path,
) -> None:
    class NoSettledCash(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "7",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
            return response

    client = NoSettledCash(cash="0", positions=full_rotation_positions())
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests] == ["SELL"]
    terminal = next(tmp_path.glob("trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"))
    assert json.loads(terminal.read_text(encoding="utf-8"))["reason"] == "post_sell_candidate_quantity_zero"


def test_relative_rotation_crash_after_sell_proof_resumes_one_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SellFilled(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            return response

    client = SellFilled(cash="0", positions=full_rotation_positions())
    original = trend_review._write_rotation_fact
    crashed = False

    def crash_once(root: Path, name: str, payload: dict[str, object]) -> Path:
        nonlocal crashed
        if name == "buy-attempt-1-intent" and not crashed:
            crashed = True
            raise RuntimeError("crash after sell proof")
        return original(root, name, payload)

    monkeypatch.setattr(trend_review, "_write_rotation_fact", crash_once)
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=relative_rotation_report(), client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T10:30:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    with pytest.raises(RuntimeError, match="crash after sell proof"):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now="2026-07-20T10:31:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=relative_rotation_report(), client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T10:32:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )

    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]


def test_relative_rotation_uncertain_buy_restart_never_duplicates(
    tmp_path: Path,
) -> None:
    class UncertainBuy(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            if request["side"] == "BUY":
                self.requests.append(request)
                raise RuntimeError("uncertain transport")
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "7",
                "order_status": "FILLED_ALL",
            })
            self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
            self.cash = "6993"
            return response

    client = UncertainBuy(cash="0", positions=full_rotation_positions())
    for minute in (30, 31, 32):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=relative_rotation_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]


def test_relative_rotation_late_fill_and_next_date_leave_cash_without_retry(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T15:00:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    client.orders[0].update({
        "dealt_qty": "1000", "dealt_avg_price": "7", "order_status": "FILLED_ALL",
    })
    client.positions = [item for item in client.positions if item["code"] != "SH.WEAK"]
    client.cash = "6993"
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-20T15:01:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )
    trend_review.execute_relative_rotations(
        data_dir=tmp_path, report=report, client=client, market="CN",
        execution_date="2026-07-20", now="2026-07-21T09:31:00+08:00",
        quote_prices={"SH.STRONG": Decimal("10")},
    )

    assert [request["side"] for request in client.requests] == ["SELL"]
    terminal = next(tmp_path.glob("trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"))
    assert json.loads(terminal.read_text(encoding="utf-8"))["reason"] == "buy_session_closed"


def test_relative_rotation_active_buy_never_retries_on_next_date(
    tmp_path: Path,
) -> None:
    class ActiveBuy(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            if request["side"] == "SELL":
                self.orders[-1].update({
                    "dealt_qty": request["qty"], "dealt_avg_price": "7",
                    "order_status": "FILLED_ALL",
                })
                self.positions = [item for item in self.positions if item["code"] != "SH.WEAK"]
                self.cash = "6993"
            return response

    client = ActiveBuy(cash="0", positions=full_rotation_positions())
    report = relative_rotation_report()
    for now in (
        "2026-07-20T10:30:00+08:00",
        "2026-07-20T10:31:00+08:00",
        "2026-07-21T09:31:00+08:00",
    ):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="CN",
            execution_date="2026-07-20", now=now,
            quote_prices={"SH.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]
    terminal = next(tmp_path.glob("trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"))
    assert json.loads(terminal.read_text(encoding="utf-8"))["reason"] == "execution_date_ended"


def test_hk_relative_rotation_can_buy_after_ten_during_continuous_session(
    tmp_path: Path,
) -> None:
    report = relative_rotation_report()
    pair = report["strategy_judgments"]["simulate_rotation_pairs"][0]
    pair.update({"sell_futu_symbol": "HK.WEAK", "buy_futu_symbol": "HK.STRONG"})
    positions = [
        {**item, "code": str(item["code"]).replace("SH.", "HK.")}
        for item in full_rotation_positions()
    ]

    class Filled(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            response = super().place_order(request)
            self.orders[-1].update({
                "dealt_qty": request["qty"], "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            })
            if request["side"] == "SELL":
                self.positions = [item for item in self.positions if item["code"] != "HK.WEAK"]
                self.cash = "6993"
            return response

    client = Filled(cash="0", positions=positions)
    for minute in (30, 31):
        trend_review.execute_relative_rotations(
            data_dir=tmp_path, report=report, client=client, market="HK",
            execution_date="2026-07-20",
            now=f"2026-07-20T10:{minute}:00+08:00",
            quote_prices={"HK.STRONG": Decimal("10")},
        )

    assert [request["side"] for request in client.requests] == ["SELL", "BUY"]


def cn_buy_report(
    *, weight: str = "0.04", symbol: str = "600001", shares: int = 400
) -> dict[str, object]:
    return {
        "account": {
            "net_value": "735164.41",
            "fresh": True,
            "source_date": "2026-07-17",
            "positions": [],
        },
        "metadata": {"price_fx_to_account_currency": "1"},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v1",
            "strategy_version": "v1",
            "process_version": "abc123",
            "parameters": {"buy_window": "09:30-10:00"},
            "parameter_rows": [
                {"group": "仓位执行", "name": "买入窗口", "value": "09:30-10:00"}
            ],
        },
        "strategy_judgments": {
            "formal_actions": [
                {
                    "action": "BUY",
                    "symbol": symbol,
                    "target_weight": weight,
                    "lot_size": 100,
                    "estimated_shares": shares,
                    "target_amount": "4000",
                    "atr": "0.5",
                }
            ]
        },
    }


TEST_QUOTE_PRICES = {
    "SH.600001": Decimal("10"),
    "SH.600002": Decimal("10"),
    "SH.600003": Decimal("10"),
    "US.NDAQ": Decimal("10"),
}


def report_with_actions(actions: list[dict[str, object]]) -> dict[str, object]:
    report = cn_buy_report()
    report["strategy_judgments"] = {
        "formal_actions": [
            {
                **action,
                **(
                    {"target_amount": action.get("target_amount", "4000")}
                    if action.get("action") == "BUY"
                    else {}
                ),
            }
            for action in actions
        ]
    }
    return report


@pytest.mark.parametrize(
    ("market", "symbol", "futu_symbol", "now"),
    [
        ("CN", "000001", "SH.000001", "2026-07-20T09:31:00+08:00"),
        ("HK", "3033.HK", "HK.03033", "2026-07-20T09:31:00+08:00"),
        ("US", "BRK_B", "US.BRK.B", "2026-07-20T09:31:00-04:00"),
    ],
)
def test_new_symbol_mapping_report_executes_frozen_futu_code(
    tmp_path: Path,
    market: str,
    symbol: str,
    futu_symbol: str,
    now: str,
) -> None:
    report = report_with_actions([
        {
            "action": "BUY",
            "symbol": symbol,
            "futu_symbol": futu_symbol,
            "target_weight": "0.04",
            "lot_size": 1,
            "estimated_shares": 4,
            "atr": "0.5",
        }
    ])
    report["metadata"] = {
        "symbol_mapping_schema": "open_trader.trend_symbol_mapping.v1"
    }
    client = FakeTrendSimClient()

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market=market,
        execution_date="2026-07-20",
        now=now,
        quote_prices={futu_symbol: Decimal("10")},
    )

    assert result["submitted_count"] == 1
    assert client.requests[0]["futu_code"] == futu_symbol
    action_key = trend_review.trend_action_key(
        market, "2026-07-20", futu_symbol, "buy"
    )
    action_root = (
        tmp_path
        / f"trend_review/ledgers/{market}/actions/2026-07-20"
        / action_key
    )
    event = json.loads(next(action_root.glob("*.json")).read_text(encoding="utf-8"))
    assert event["futu_code"] == futu_symbol


def test_new_symbol_mapping_report_rejects_action_without_frozen_code() -> None:
    report = cn_buy_report()
    report["metadata"] = {
        "symbol_mapping_schema": "open_trader.trend_symbol_mapping.v1"
    }

    with pytest.raises(ValueError, match="frozen Futu symbol"):
        trend_review._preflight_open_actions(report, "CN")


def test_legacy_report_without_mapping_marker_keeps_symbol_conversion() -> None:
    action = cn_buy_report()["strategy_judgments"]["formal_actions"][0]

    assert trend_review._preflight_open_actions(cn_buy_report(), "CN")[0] == [
        action
    ]


def partial_sell_report(
    *,
    symbol: object = "600001",
    target_fraction: object = "0.30",
    lot_size: object = 100,
    estimated_shares: object = 300,
    position_started_for: object = "2026-07-01",
    overheat_signals: object = None,
) -> dict[str, object]:
    return report_with_actions([
        {
            "action": "SELL_PARTIAL",
            "symbol": symbol,
            "reason": "overheat_take_profit",
            "target_fraction": target_fraction,
            "lot_size": lot_size,
            "estimated_shares": estimated_shares,
            "position_started_for": position_started_for,
            "overheat_signals": (
                ["boiling"] if overheat_signals is None else overheat_signals
            ),
        }
    ])


def lock_partial_report(tmp_path: Path, execution_date: str) -> dict[str, object]:
    report = partial_sell_report()
    report_path = tmp_path / f"reports/{execution_date}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date=execution_date,
        report_path=report_path,
        report=report,
        locked_at=f"{execution_date}T09:30:00+08:00",
    )
    return report


def test_partial_action_validation() -> None:
    valid = partial_sell_report()
    assert trend_review._preflight_open_actions(valid, "CN")[0] == [
        valid["strategy_judgments"]["formal_actions"][0]
    ]

    invalid_actions = [
        {"target_fraction": "0.29"},
        {"lot_size": "100.5"},
        {"lot_size": "100.0"},
        {"estimated_shares": -100},
        {"estimated_shares": 250},
        {"position_started_for": "2026-7-01"},
        {"overheat_signals": ["unknown"]},
    ]
    for changes in invalid_actions:
        report = partial_sell_report(**changes)
        with pytest.raises(ValueError, match="partial sell action is invalid"):
            trend_review._preflight_open_actions(report, "CN")
    for field in (
        "target_fraction",
        "lot_size",
        "estimated_shares",
        "position_started_for",
        "overheat_signals",
    ):
        report = partial_sell_report()
        del report["strategy_judgments"]["formal_actions"][0][field]
        with pytest.raises(ValueError, match="partial sell action is invalid"):
            trend_review._preflight_open_actions(report, "CN")


def test_partial_and_full_sell_for_one_symbol_are_rejected() -> None:
    partial = partial_sell_report()["strategy_judgments"]["formal_actions"][0]
    report = report_with_actions([
        partial,
        {"action": "SELL_ALL", "symbol": "SH.600001"},
    ])

    with pytest.raises(ValueError, match="conflicting sell actions"):
        trend_review._preflight_open_actions(report, "CN")


@pytest.mark.parametrize(
    "actions",
    [
        [partial_sell_report()["strategy_judgments"]["formal_actions"][0],
         partial_sell_report(symbol="SH.600001")["strategy_judgments"]["formal_actions"][0]],
        [{"action": "SELL_ALL", "symbol": "600001"},
         {"action": "SELL_ALL", "symbol": "SH.600001"}],
    ],
)
def test_duplicate_sell_actions_for_one_canonical_symbol_are_rejected(
    actions: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError, match="conflicting sell actions"):
        trend_review._preflight_open_actions(report_with_actions(actions), "CN")


def test_legacy_sell_all_immutable_facts_remain_compatible(tmp_path: Path) -> None:
    report = report_with_actions([{"action": "SELL_ALL", "symbol": "600001"}])
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "300"}]),
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )

    progress: list[None] = []
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
        progress=lambda: progress.append(None),
    )

    assert events[-1]["status"] == "submitted"
    assert events[-1]["sell_goal"] == "position_zero"
    assert len(progress) >= len(events)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sell_goal", "position_zero"),
        ("lifecycle_target_qty", "300.5"),
        ("lifecycle_target_qty", "250"),
    ],
)
def test_partial_audit_rejects_tampered_goal_metadata(
    tmp_path: Path, field: str, value: str
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    intent_path = next(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-intent.json"
    ))
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent[field] = value
    intent_path.write_text(json.dumps(intent), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action fact"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="sell",
        )


@pytest.mark.parametrize(
    ("market", "symbol", "futu_code", "live_quantity", "lot_size", "estimate", "target", "now"),
    [
        ("CN", "600001", "SH.600001", "1000", 100, 300, "300", "2026-07-17T09:31:00+08:00"),
        ("US", "AAPL", "US.AAPL", "7", 1, 2, "2", "2026-07-17T09:31:00-04:00"),
        ("HK", "00700", "HK.00700", "1000", 200, 200, "200", "2026-07-17T09:31:00+08:00"),
        ("CN", "600001", "SH.600001", "800", 100, 300, "200", "2026-07-17T09:31:00+08:00"),
    ],
)
def test_partial_live_position_freezes_target_before_first_intent(
    tmp_path: Path,
    market: str,
    symbol: str,
    futu_code: str,
    live_quantity: str,
    lot_size: int,
    estimate: int,
    target: str,
    now: str,
) -> None:
    class IntentCheckingClient(FakeTrendSimClient):
        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            intent_path = next(tmp_path.glob(
                f"trend_review/ledgers/{market}/open/2026-07-17/*-intent.json"
            ))
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            assert intent["sell_goal"] == "partial_30"
            assert intent["position_started_for"] == "2026-07-01"
            assert intent["lifecycle_target_qty"] == target
            return super().place_order(request)

    client = IntentCheckingClient(
        positions=[{"code": futu_code, "qty": live_quantity}]
    )
    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=partial_sell_report(
            symbol=symbol, lot_size=lot_size, estimated_shares=estimate
        ),
        client=client,
        market=market,
        execution_date="2026-07-17",
        now=now,
        quote_prices={},
    )
    intent = json.loads(next(tmp_path.glob(
        f"trend_review/ledgers/{market}/open/2026-07-17/*-intent.json"
    )).read_text(encoding="utf-8"))
    execution_result = json.loads(next(tmp_path.glob(
        f"trend_review/ledgers/{market}/open/2026-07-17/*-result.json"
    )).read_text(encoding="utf-8"))
    event = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            f"trend_review/ledgers/{market}/actions/2026-07-17/*/*.json"
        )
        if json.loads(path.read_text(encoding="utf-8")).get("status") == "submitted"
    )

    assert result["submitted_count"] == 1
    assert client.requests == [{
        "market": market,
        "futu_code": futu_code,
        "side": "sell",
        "order_type": "MARKET",
        "price": "0",
        "qty": target,
        "remark": client.requests[0]["remark"],
    }]
    for payload in (intent, execution_result, event):
        assert payload | {
            "sell_goal": "partial_30",
            "position_started_for": "2026-07-01",
            "lifecycle_target_qty": target,
        } == payload


def test_partial_facts_never_enter_generic_retry_recovery(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    arguments = {
        "data_dir": tmp_path,
        "report": partial_sell_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders[0].update({"dealt_qty": "0", "order_status": "CANCELLED_PART"})

    recovered = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T16:30:00+08:00"
    )

    assert recovered["submitted_count"] == 0
    assert len(client.requests) == 1
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-attempt-2-intent.json"
    ))


def test_partial_restart_recovers_bare_intent_with_frozen_evidence(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    state_path = tmp_path / "trend_a_share/protection_state.json"
    write_protection_state(
        state_path,
        {"schema_version": 1, "positions": {"600001": {
            "position_started_for": "2026-07-01",
            "updated_for": "2026-07-16",
        }}},
    )

    class CrashAfterIntent(FakeTrendSimClient):
        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=CrashAfterIntent(positions=[{"code": "SH.600001", "qty": "1000"}]),
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
            quote_prices={},
        )
    assert list(tmp_path.glob("trend_review/ledgers/CN/open/2026-07-17/*-intent.json"))
    assert not list(tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*"))
    batch_path = tmp_path / "trend_review/ledgers/CN/batches/2026-07-17.json"
    batch = batch_path.read_bytes()
    batch_path.unlink()
    bare_progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )
    batch_path.write_bytes(batch)
    assert bare_progress["lifecycle_target_qty"] == "300"
    assert bare_progress["has_unresolved_order"] is True

    restarted = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=restarted,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices={},
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
    )
    projection = trend_review.rebuild_overheat_trim_projection(
        tmp_path, market="CN", state_path=state_path
    )

    assert result["submitted_count"] == 0
    assert restarted.requests == []
    uncertain = next(event for event in events if event.get("status") == "uncertain")
    assert uncertain | {
        "sell_goal": "partial_30",
        "position_started_for": "2026-07-01",
        "lifecycle_target_qty": "300",
    } == uncertain
    assert projection["positions"]["600001"]["overheat_trim_target_qty"] == "300"


def test_partial_progress_ignores_bare_intent_from_closed_lifecycle(
    tmp_path: Path,
) -> None:
    old_report = lock_partial_report(tmp_path, "2026-07-17")

    class CrashAfterIntent(FakeTrendSimClient):
        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=old_report,
            client=CrashAfterIntent(positions=[{"code": "SH.600001", "qty": "1000"}]),
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
            quote_prices={},
        )
    (tmp_path / "trend_review/ledgers/CN/batches/2026-07-17.json").unlink()

    new_report = partial_sell_report(position_started_for="2026-07-18")
    report_path = tmp_path / "reports/2026-07-18.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(new_report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-18",
        report_path=report_path,
        report=new_report,
        locked_at="2026-07-18T09:30:00+08:00",
    )
    state_path = tmp_path / "trend_a_share/protection_state.json"
    write_protection_state(
        state_path,
        {"schema_version": 1, "positions": {"600001": {
            "position_started_for": "2026-07-18",
            "updated_for": "2026-07-18",
        }}},
    )
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "500"}])

    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=new_report,
        client=client,
        market="CN",
        execution_date="2026-07-18",
        now="2026-07-18T09:31:00+08:00",
        quote_prices={},
    )
    projection = trend_review.rebuild_overheat_trim_projection(
        tmp_path, market="CN", state_path=state_path
    )

    assert client.requests[-1]["qty"] == "100"
    assert projection["positions"]["600001"] | {
        "overheat_trim_started_for": "2026-07-18",
        "overheat_trim_target_qty": "100",
    } == projection["positions"]["600001"]


def test_partial_lifecycle_reuses_confirmed_remainder_on_a_later_report(
    tmp_path: Path,
) -> None:
    report = partial_sell_report()
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    state_path = tmp_path / "trend_a_share/protection_state.json"
    write_protection_state(
        state_path,
        {
            "schema_version": 1,
            "positions": {"600001": {
                "position_started_for": "2026-07-01",
                "updated_for": "2026-07-16",
            }},
        },
    )
    for execution_date in ("2026-07-17", "2026-07-18"):
        report_path = tmp_path / f"reports/{execution_date}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date=execution_date,
            report_path=report_path,
            report=report,
            locked_at=f"{execution_date}T09:30:00+08:00",
        )
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date=execution_date,
            now=f"{execution_date}T09:31:00+08:00",
            quote_prices={},
        )
        if execution_date == "2026-07-17":
            client.orders[0].update(
                {"dealt_qty": "100", "order_status": "CANCELLED_PART"}
            )
            trend_review.execute_trend_review_open(
                data_dir=tmp_path,
                report=report,
                client=client,
                market="CN",
                execution_date=execution_date,
                now=f"{execution_date}T09:32:00+08:00",
                quote_prices={},
            )
            projected = load_protection_state(state_path)["positions"]["600001"]
            assert projected | {
                "overheat_trim_status": "pending",
                "overheat_trim_target_qty": "300",
                "overheat_trim_filled_qty": "100",
                "overheat_trim_started_for": "2026-07-01",
            } == projected

    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert progress["lifecycle_target_qty"] == "300"
    assert progress["filled_qty"] == "100"
    assert client.requests[-1]["qty"] == "200"


def test_partial_abandon_leaves_the_lifecycle_pending_for_a_later_report(
    tmp_path: Path,
) -> None:
    report = partial_sell_report()
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    for execution_date in ("2026-07-17", "2026-07-18"):
        report_path = tmp_path / f"reports/{execution_date}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date=execution_date,
            report_path=report_path,
            report=report,
            locked_at=f"{execution_date}T09:30:00+08:00",
        )
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date=execution_date,
            now=f"{execution_date}T09:31:00+08:00",
            quote_prices={},
        )
        if execution_date == "2026-07-17":
            client.orders.clear()
            trend_review.execute_trend_review_open(
                data_dir=tmp_path,
                report=report,
                client=client,
                market="CN",
                execution_date=execution_date,
                now="2026-07-17T09:32:00+08:00",
                quote_prices={},
            )
            trend_review.resolve_trend_action(
                tmp_path,
                market="CN",
                execution_date=execution_date,
                symbol="600001",
                side="sell",
                resolution="abandon",
                actor="ray",
                reason="broker cannot identify the order",
                resolved_at="2026-07-17T09:33:00+08:00",
            )

    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert progress | {"status": "pending", "filled_qty": "0"} == progress
    assert client.requests[-1]["qty"] == "300"


def test_partial_abandon_retains_confirmed_remainder_for_later_signal(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders[0].update({"dealt_qty": "100", "order_status": "CANCELLED_PART"})
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )
    client.orders.clear()
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:33:00+08:00"
    )
    trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
        resolution="abandon",
        actor="ray",
        reason="manual reconciliation pauses this action",
        resolved_at="2026-07-17T09:34:00+08:00",
    )
    request_count = len(client.requests)
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:35:00+08:00"
    )
    assert len(client.requests) == request_count
    later_report = lock_partial_report(tmp_path, "2026-07-18")
    client.positions = [{"code": "SH.600001", "qty": "500"}]
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=later_report,
        client=client,
        market="CN",
        execution_date="2026-07-18",
        now="2026-07-18T09:31:00+08:00",
        quote_prices={},
    )

    assert client.requests[-1]["qty"] == "200"


def test_partial_abandon_retains_a_reconciled_fill(tmp_path: Path) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    request = dict(client.requests[0])
    client.orders.clear()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices={},
    )
    client.orders = [{
        **request,
        "order_id": "SIM-1",
        "code": "SH.600001",
        "trd_side": "SELL",
        "dealt_qty": "300",
        "dealt_avg_price": "10",
        "order_status": "FILLED_ALL",
    }]
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:33:00+08:00",
        quote_prices={},
    )
    trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
        resolution="abandon",
        actor="ray",
        reason="manual reconciliation supersedes the partial attempt",
        resolved_at="2026-07-17T09:34:00+08:00",
    )

    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert progress | {
        "lifecycle_target_qty": "300",
        "filled_qty": "300",
        "status": "complete",
        "has_unresolved_order": False,
    } == progress


@pytest.mark.parametrize("order_status", ["FILLED_PART", ""])
def test_partial_lifecycle_blocks_a_later_report_for_an_unresolved_old_order(
    tmp_path: Path, order_status: str
) -> None:
    report = partial_sell_report()
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    for execution_date in ("2026-07-17", "2026-07-18"):
        report_path = tmp_path / f"reports/{execution_date}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date=execution_date,
            report_path=report_path,
            report=report,
            locked_at=f"{execution_date}T09:30:00+08:00",
        )
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date=execution_date,
            now=f"{execution_date}T09:31:00+08:00",
            quote_prices={},
        )
        if execution_date == "2026-07-17":
            client.orders[0].update(
                {"dealt_qty": "100", "order_status": order_status}
            )
            trend_review.execute_trend_review_open(
                data_dir=tmp_path,
                report=report,
                client=client,
                market="CN",
                execution_date=execution_date,
                now="2026-07-17T09:32:00+08:00",
                quote_prices={},
            )
            with pytest.raises(
                ValueError, match="not uncertain or is already resolved"
            ):
                trend_review.resolve_trend_action(
                    tmp_path,
                    market="CN",
                    execution_date=execution_date,
                    symbol="600001",
                    side="sell",
                    resolution="authorize-retry",
                    actor="ray",
                    reason="the order is still active",
                    resolved_at="2026-07-17T09:33:00+08:00",
                )

    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert progress["has_unresolved_order"] is True
    assert len(client.requests) == 1
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-18/*-intent.json"
    ))


def test_partial_authorized_retry_uses_one_remaining_frozen_attempt(
    tmp_path: Path,
) -> None:
    execution_date = "2026-07-17"
    report = partial_sell_report()
    report_path = tmp_path / f"reports/{execution_date}.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date=execution_date,
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": execution_date,
        "quote_prices": {},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders[0].update({"dealt_qty": "100", "order_status": "FILLED_PART"})
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )
    client.orders[0]["order_status"] = "CANCELLED_PART"
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:33:00+08:00"
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date=execution_date,
        symbol="600001",
        side="sell",
    )
    assert next(
        event for event in events if event.get("status") == "partially_filled"
    )["observation_path"]

    trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date=execution_date,
        symbol="600001",
        side="sell",
        resolution="authorize-retry",
        actor="ray",
        reason="broker confirms the partial order is cancelled",
        resolved_at="2026-07-17T09:34:00+08:00",
    )
    retried = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:35:00+08:00"
    )
    repeated = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:36:00+08:00"
    )

    retry_intent = next(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-attempt-2-intent.json"
    ))
    assert (
        json.loads(retry_intent.read_text(encoding="utf-8"))["request"]["qty"]
        == "200"
    )
    assert retried["submitted_count"] == 1
    assert repeated["submitted_count"] == 0
    assert len(client.requests) == 2


def test_partial_abandon_does_not_clear_a_later_unresolved_action(
    tmp_path: Path,
) -> None:
    report = partial_sell_report()
    client = FakeTrendSimClient(positions=[{"code": "SH.600001", "qty": "1000"}])
    for execution_date in ("2026-07-17", "2026-07-18", "2026-07-19"):
        report_path = tmp_path / f"reports/{execution_date}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date=execution_date,
            report_path=report_path,
            report=report,
            locked_at=f"{execution_date}T09:30:00+08:00",
        )
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date=execution_date,
            now=f"{execution_date}T09:31:00+08:00",
            quote_prices={},
        )
        if execution_date == "2026-07-17":
            client.orders.clear()
            trend_review.execute_trend_review_open(
                data_dir=tmp_path,
                report=report,
                client=client,
                market="CN",
                execution_date=execution_date,
                now="2026-07-17T09:32:00+08:00",
                quote_prices={},
            )
            trend_review.resolve_trend_action(
                tmp_path,
                market="CN",
                execution_date=execution_date,
                symbol="600001",
                side="sell",
                resolution="abandon",
                actor="ray",
                reason="the original order cannot be identified",
                resolved_at="2026-07-17T09:33:00+08:00",
            )

    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert progress["has_unresolved_order"] is True
    assert len(client.requests) == 2
    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-19/*-intent.json"
    ))


def test_partial_below_lot_is_an_audited_terminal_fact(tmp_path: Path) -> None:
    report = partial_sell_report(symbol="00700", lot_size=200, estimated_shares=0)
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="HK",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    client = FakeTrendSimClient(
        positions=[{"code": "HK.00700", "qty": "300"}]
    )

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="HK",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="HK",
        execution_date="2026-07-17",
        symbol="00700",
        side="sell",
    )

    assert result["submitted_count"] == 0
    assert client.requests == []
    assert not list(tmp_path.glob(
        "trend_review/ledgers/HK/open/2026-07-17/*-intent.json"
    ))
    assert events[-1] | {
        "status": "below_lot",
        "reason": "overheat_target_below_lot",
        "sell_goal": "partial_30",
        "position_started_for": "2026-07-01",
        "lifecycle_target_qty": "0",
        "target_qty": "0",
        "filled_qty": "0",
        "order_ids": [],
    } == events[-1]
    event_path = next(tmp_path.glob(
        "trend_review/ledgers/HK/actions/2026-07-17/*/*.json"
    ))
    tampered = json.loads(event_path.read_text(encoding="utf-8"))
    tampered["target_qty"] = "200"
    event_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="HK",
            execution_date="2026-07-17",
            symbol="00700",
            side="sell",
        )


def test_below_lot_audit_recomputes_observed_live_target(tmp_path: Path) -> None:
    report = partial_sell_report(symbol="00700", lot_size=200, estimated_shares=0)
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="HK",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=FakeTrendSimClient(
            positions=[{"code": "HK.00700", "qty": "300"}]
        ),
        market="HK",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    event_path = next(tmp_path.glob(
        "trend_review/ledgers/HK/actions/2026-07-17/*/*.json"
    ))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    observation_path = (
        tmp_path
        / "trend_review/ledgers/HK/open/2026-07-17"
        / event["observation_path"]
    )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["position_qty"] = "1000"
    body = trend_review._canonical_json_bytes(observation)
    digest = hashlib.sha256(body).hexdigest()
    action_key = trend_review.trend_action_key(
        "HK", "2026-07-17", "HK.00700", "sell"
    )
    replacement = observation_path.with_name(
        f"{action_key}-observation-{digest[:12]}.json"
    )
    replacement.write_bytes(body)
    event["observation_path"] = replacement.name
    event["observation_sha256"] = digest
    event_path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="HK",
            execution_date="2026-07-17",
            symbol="00700",
            side="sell",
        )


def test_partial_future_execution_does_not_freeze_below_lot(tmp_path: Path) -> None:
    report = partial_sell_report(symbol="00700", lot_size=200, estimated_shares=0)
    client = FakeTrendSimClient(
        positions=[{"code": "HK.00700", "qty": "300"}]
    )
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "HK",
        "execution_date": "2026-07-17",
        "quote_prices": {},
    }

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-16T09:31:00+08:00"
    )

    assert not list(tmp_path.glob(
        "trend_review/ledgers/HK/actions/2026-07-17/*/*.json"
    ))
    client.positions = [{"code": "HK.00700", "qty": "1000"}]
    result = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )

    assert result["submitted_count"] == 1
    assert client.requests[0]["qty"] == "200"


@pytest.mark.parametrize(
    "positions",
    [
        [],
        [{"code": "SH.600001", "qty": "not-a-number"}],
    ],
)
def test_partial_missing_or_invalid_live_position_writes_no_intent(
    tmp_path: Path, positions: list[dict[str, object]]
) -> None:
    with pytest.raises(
        trend_review.TrendReviewAccountStateError,
        match="partial sell position",
    ):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=partial_sell_report(),
            client=FakeTrendSimClient(positions=positions),
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
            quote_prices={},
        )

    assert not list(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-intent.json"
    ))


def test_action_identity_ignores_report_revision_and_strategy_version() -> None:
    first = trend_review.trend_action_key(
        "US", "2026-07-20", "US.TRV", "buy"
    )
    second = trend_review.trend_action_key(
        "US", "2026-07-20", "us.trv", "BUY"
    )

    assert first == second
    assert trend_review.trend_attempt_remark(
        "US", "2026-07-20", first, 1
    ) != trend_review.trend_attempt_remark(
        "US", "2026-07-20", first, 2
    )


def test_open_execution_refreshes_broker_orders_before_each_submission(
    tmp_path: Path,
) -> None:
    report = report_with_actions([
        {
            "action": "BUY", "symbol": "600001", "target_weight": "0.04",
            "lot_size": 100, "estimated_shares": 400, "atr": "0.5",
        },
        {
            "action": "BUY", "symbol": "600002", "target_weight": "0.04",
            "lot_size": 100, "estimated_shares": 400, "atr": "0.5",
        },
    ])
    second_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600002", "buy"
    )
    external_second_order = {
        "order_id": "EXTERNAL-2",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-20", second_key, 1
        ),
        "code": "SH.600002",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "0",
        "order_status": "SUBMITTED",
    }

    class RefreshingClient(FakeTrendSimClient):
        def list_orders(self, **kwargs: object) -> dict[str, object]:
            self.list_order_calls.append(dict(kwargs))
            orders = [dict(order) for order in self.orders]
            if len(self.list_order_calls) >= 2:
                orders.append(external_second_order)
            return {"orders": orders}

    client = RefreshingClient()

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:30:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert [request["futu_code"] for request in client.requests] == ["SH.600001"]
    assert client.list_order_calls == [
        {"start": "2026-07-20", "end": "2026-07-20"},
        {"start": "2026-07-20", "end": "2026-07-20"},
    ]


def test_execution_batch_keeps_first_report_sha(tmp_path: Path) -> None:
    first = cn_buy_report()
    revised = {
        **cn_buy_report(),
        "generated_at": "2026-07-20T08:59:00+08:00",
    }

    locked = trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=tmp_path / "2026-07-17.json",
        report=first,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    repeated = trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=tmp_path / "2026-07-17-r1.json",
        report=revised,
        locked_at="2026-07-20T09:31:00+08:00",
    )

    assert repeated == locked
    assert repeated["report_sha256"] == trend_review._report_hash(first)


def test_execution_batch_recovers_report_selected_by_legacy_intent(
    tmp_path: Path,
) -> None:
    old_report = cn_buy_report(shares=300)
    old_path = tmp_path / "reports/2026-07-17.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps(old_report), encoding="utf-8")
    latest_report = cn_buy_report(shares=400)
    latest_path = old_path.with_name("2026-07-17-r1.json")
    latest_path.write_text(json.dumps(latest_report), encoding="utf-8")
    intent_path = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20/legacy-intent.json"
    )
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text(
        json.dumps(
                {
                    "report_sha256": trend_review._report_hash(old_report),
                    "created_at": "2026-07-20T09:29:00+08:00",
                    "request": {"futu_code": "SH.600001", "side": "buy"},
            }
        ),
        encoding="utf-8",
    )

    locked = trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=latest_path,
        report=latest_report,
        locked_at="2026-07-20T09:30:00+08:00",
    )

    assert locked["report_path"] == str(old_path)
    assert locked["report_sha256"] == trend_review._report_hash(old_report)


def test_execution_batch_blocks_when_legacy_report_is_missing(
    tmp_path: Path,
) -> None:
    latest_report = cn_buy_report(shares=400)
    latest_path = tmp_path / "reports/2026-07-17-r1.json"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text(json.dumps(latest_report), encoding="utf-8")
    intent_path = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20/legacy-intent.json"
    )
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text(
        json.dumps(
                {
                    "report_sha256": "a" * 64,
                    "created_at": "2026-07-20T09:29:00+08:00",
                    "request": {"futu_code": "SH.600001", "side": "buy"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="blocked.*matching report"):
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            report_path=latest_path,
            report=latest_report,
            locked_at="2026-07-20T09:30:00+08:00",
        )


def test_existing_exact_broker_order_repairs_result_without_submit(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    client = FakeTrendSimClient()
    client.orders = [
        {
            "order_id": "SIM-EXISTING",
            "remark": trend_review.trend_attempt_remark(
                "CN", "2026-07-20", action_key, 1
            ),
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        }
    ]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices={"SH.600001": Decimal("10")},
    )

    assert result["submitted_count"] == 0
    assert client.requests == []
    assert list(
        tmp_path.glob("trend_review/ledgers/CN/open/2026-07-20/*-result.json")
    )


def test_same_remark_with_conflicting_quantity_fails_closed(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    client = FakeTrendSimClient()
    client.orders = [
        {
            "order_id": "SIM-CONFLICT",
            "remark": trend_review.trend_attempt_remark(
                "CN", "2026-07-20", action_key, 1
            ),
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "999",
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        }
    ]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices={"SH.600001": Decimal("10")},
    )

    assert result["status"] == "conflict"
    assert client.requests == []


def test_same_remark_with_exact_and_conflicting_orders_fails_closed(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    remark = trend_review.trend_attempt_remark(
        "CN", "2026-07-20", action_key, 1
    )
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "now": "2026-07-20T09:31:00+08:00",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    client.orders = [
        {
            "order_id": "SIM-EXACT",
            "remark": remark,
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
        },
        {
            "order_id": "SIM-CONFLICT",
            "remark": remark,
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "999",
        },
    ]

    result = trend_review.execute_trend_review_open(**arguments)

    assert result["status"] == "conflict"
    assert len(client.requests) == 1


def test_unknown_buy_broker_status_becomes_uncertain(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            **client.requests[0],
            "code": "SH.600001",
            "trd_side": "BUY",
            "dealt_qty": "0",
            "order_status": "UNKNOWN",
        }
    ]

    result = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:32:00+08:00"
    )

    assert result["status"] == "uncertain"
    assert len(client.requests) == 1


def test_intent_without_broker_fact_becomes_uncertain_and_never_resubmits(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "now": "2026-07-20T09:31:00+08:00",
        "quote_prices": {"SH.600001": Decimal("10")},
    }

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    client.fail_orders = 0
    recovered = trend_review.execute_trend_review_open(**arguments)
    repeated = trend_review.execute_trend_review_open(
        **{**arguments, "now": "2026-07-20T09:32:00+08:00"}
    )

    assert recovered["status"] == "uncertain"
    assert repeated["status"] == "uncertain"
    assert len(client.requests) == 1
    uncertain = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
        if '"status":"uncertain"' in path.read_text(encoding="utf-8")
    ]
    assert len(uncertain) == 1


def make_uncertain_buy(tmp_path: Path) -> FakeTrendSimClient:
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(
            **arguments, now="2026-07-20T09:31:00+08:00"
        )
    client.fail_orders = 0
    assert trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:32:00+08:00"
    )["status"] == "uncertain"
    return client


@pytest.mark.parametrize(
    ("resolution", "order_id", "expected"),
    [
        ("confirm-submitted", "SIM-42", "resolved_submitted"),
        ("authorize-retry", None, "retry_authorized"),
        ("abandon", None, "abandoned"),
    ],
)
def test_uncertain_action_resolution_is_immutable(
    tmp_path: Path, resolution: str, order_id: str | None, expected: str
) -> None:
    make_uncertain_buy(tmp_path)

    path = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution=resolution,
        actor="ray",
        reason="checked Futu history",
        resolved_at="2026-07-20T09:40:00+08:00",
        futu_order_id=order_id,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == expected
    assert list(path.parent.glob("*.json")) == [path]


def legacy_filled_buy_audit(
    tmp_path: Path, *, attempt: int = 1
) -> tuple[FakeTrendSimClient, dict[str, object], Path]:
    report = cn_buy_report()
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )
    legacy_key = hashlib.sha256(
        "CN:2026-07-20:v1:SH.600001:buy".encode("utf-8")
    ).hexdigest()
    legacy_remark = f"trend-review:CN:2026-07-20:{legacy_key[:24]}"
    intent_path = next(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-20/*-intent.json"
    ))
    result_path = intent_path.with_name(
        intent_path.name.replace("-intent", "-result")
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    intent["request"]["remark"] = legacy_remark
    result["request"]["remark"] = legacy_remark
    if attempt > 1:
        intent["attempt"] = attempt
    stem = legacy_key if attempt == 1 else f"{legacy_key}-attempt-{attempt}"
    legacy_intent_path = intent_path.with_name(f"{stem}-intent.json")
    legacy_result_path = result_path.with_name(f"{stem}-result.json")
    legacy_intent_path.write_text(json.dumps(intent), encoding="utf-8")
    legacy_result_path.write_text(json.dumps(result), encoding="utf-8")
    intent_path.unlink()
    result_path.unlink()
    request = client.requests[0]
    request["remark"] = legacy_remark
    client.positions = [{"code": "SH.600001", "qty": request["qty"]}]
    client.orders = [{
        "order_id": "SIM-1",
        "remark": request["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": request["qty"],
        "dealt_qty": request["qty"],
        "dealt_avg_price": "10",
        "order_status": "FILLED_ALL",
    }]
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T10:01:00+08:00"
    )
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=report_path,
        report=report,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    return client, arguments, legacy_result_path


@pytest.mark.parametrize("attempt", [1, 2])
def test_action_audit_accepts_paired_legacy_result_without_identity_fields(
    tmp_path: Path, attempt: int
) -> None:
    client, arguments, result_path = legacy_filled_buy_audit(
        tmp_path, attempt=attempt
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    del result["report_sha256"]
    del result["action_index"]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    legacy_bytes = result_path.read_bytes()

    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
    )
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T10:02:00+08:00"
    )

    assert any(event.get("status") == "filled" for event in events)
    assert len(client.requests) == 1
    assert result_path.read_bytes() == legacy_bytes


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_report",
        "wrong_index",
        "missing_report",
        "missing_index",
        "request_mismatch",
        "orphan",
        "legacy_remark_mismatch",
        "legacy_path_mismatch",
        "legacy_key_mismatch",
    ],
)
def test_action_audit_rejects_other_legacy_result_identity_gaps(
    tmp_path: Path, mutation: str
) -> None:
    _client, _arguments, result_path = legacy_filled_buy_audit(tmp_path)
    intent_path = result_path.with_name(
        result_path.name.replace("-result", "-intent")
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if mutation == "wrong_report":
        result["report_sha256"] = "0" * 64
    elif mutation == "wrong_index":
        result["action_index"] = 1
    elif mutation == "missing_report":
        del result["report_sha256"]
    elif mutation == "missing_index":
        del result["action_index"]
    elif mutation == "request_mismatch":
        result["request"] = {**result["request"], "qty": "300"}
    elif mutation == "orphan":
        del result["report_sha256"]
        del result["action_index"]
        intent_path.unlink()
    elif mutation == "legacy_remark_mismatch":
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent["request"]["remark"] = "trend-review:CN:2026-07-20:wrong"
        result["request"] = intent["request"]
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
    elif mutation == "legacy_path_mismatch":
        mismatched_intent = intent_path.with_name(f"wrong-{intent_path.name}")
        mismatched_result = result_path.with_name(f"wrong-{result_path.name}")
        intent_path.rename(mismatched_intent)
        result_path.rename(mismatched_result)
        intent_path = mismatched_intent
        result_path = mismatched_result
    else:
        wrong_key = "0" * 64
        wrong_remark = f"trend-review:CN:2026-07-20:{wrong_key[:24]}"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent["request"]["remark"] = wrong_remark
        result["request"] = intent["request"]
        wrong_intent = intent_path.with_name(f"{wrong_key}-intent.json")
        wrong_result = result_path.with_name(f"{wrong_key}-result.json")
        wrong_intent.write_text(json.dumps(intent), encoding="utf-8")
        intent_path.unlink()
        result_path.unlink()
        intent_path = wrong_intent
        result_path = wrong_result
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action fact"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
        )


def test_action_audit_loader_rejects_wrong_identity_event(tmp_path: Path) -> None:
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    path = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "missed.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "market": "US",
            "date": "2026-07-20",
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "missed",
            "recorded_at": "2026-07-20T15:01:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action event identity"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
        )


@pytest.mark.parametrize(
    ("status", "evidence"),
    [
        ("missed", {}),
        (
            "filled",
            {"filled_qty": "100", "target_qty": "100", "order_ids": []},
        ),
    ],
)
def test_action_audit_loader_rejects_terminal_event_without_required_evidence(
    tmp_path: Path,
    status: str,
    evidence: dict[str, object],
) -> None:
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    path = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / f"{status}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": status,
            "recorded_at": "2026-07-20T15:01:00+08:00",
            **evidence,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
        )


def test_action_audit_loader_rejects_position_zero_without_prior_action_fact(
    tmp_path: Path,
) -> None:
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "sell"
    )
    path = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "position-zero.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "sell",
            "status": "incomplete",
            "reason": "position_zero_confirmed",
            "recorded_at": "2026-07-20T15:01:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="sell",
        )


def test_action_audit_loader_rejects_filled_event_without_broker_fact(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=report_path,
        report=report,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    path = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "forged-filled.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "strategy_version": "v1",
            "report_sha256": trend_review._report_hash(report),
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "filled",
            "filled_qty": "400",
            "target_qty": "400",
            "order_ids": ["FORGED"],
            "recorded_at": "2026-07-20T09:31:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
        )


def test_action_audit_loader_rejects_position_zero_for_wrong_report_attempt(
    tmp_path: Path,
) -> None:
    report = report_with_actions([
        {"action": "SELL_ALL", "symbol": "600001"}
    ])
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=report_path,
        report=report,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "sell"
    )
    intent = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20"
        / f"{action_key}-intent.json"
    )
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "report_sha256": "0" * 64,
            "action_index": 1,
            "attempt": 1,
            "request": {
                "market": "CN",
                "futu_code": "SH.600001",
                "side": "sell",
                "order_type": "MARKET",
                "price": "0",
                "qty": "100",
                "remark": trend_review.trend_attempt_remark(
                    "CN", "2026-07-20", action_key, 1
                ),
            },
            "created_at": "2026-07-20T09:31:00+08:00",
        }),
        encoding="utf-8",
    )
    event = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "forged-position-zero.json"
    )
    event.parent.mkdir(parents=True)
    event.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "strategy_version": "v1",
            "report_sha256": trend_review._report_hash(report),
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "sell",
            "status": "incomplete",
            "reason": "position_zero_confirmed",
            "filled_qty": "0",
            "target_qty": "100",
            "order_ids": [],
            "recorded_at": "2026-07-20T09:32:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="sell",
        )


def test_action_audit_loader_rejects_self_reported_broker_snapshot(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        report_path=report_path,
        report=report,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    request = {
        "market": "CN",
        "futu_code": "SH.600001",
        "side": "buy",
        "order_type": "MARKET",
        "price": "0",
        "qty": "400",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-20", action_key, 1
        ),
    }
    intent = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20"
        / f"{action_key}-intent.json"
    )
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "report_sha256": trend_review._report_hash(report),
            "action_index": 0,
            "request": request,
            "created_at": "2026-07-20T09:31:00+08:00",
        }),
        encoding="utf-8",
    )
    event = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "forged-complete-snapshot.json"
    )
    event.parent.mkdir(parents=True)
    event.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "strategy_version": "v1",
            "report_sha256": trend_review._report_hash(report),
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "filled",
            "filled_qty": "400",
            "target_qty": "400",
            "order_ids": ["FORGED"],
            "broker_account_id": 101,
            "broker_position_qty": "400",
            "broker_orders": [{
                "order_id": "FORGED",
                "remark": request["remark"],
                "code": "SH.600001",
                "trd_side": "BUY",
                "qty": "400",
                "dealt_qty": "400",
                "order_status": "FILLED",
            }],
            "recorded_at": "2026-07-20T09:32:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
        )


@pytest.mark.parametrize(("actor", "reason"), [("", "checked"), ("ray", " ")])
def test_resolution_requires_actor_and_reason(
    tmp_path: Path, actor: str, reason: str
) -> None:
    make_uncertain_buy(tmp_path)

    with pytest.raises(ValueError, match="actor and reason"):
        trend_review.resolve_trend_action(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
            resolution="abandon",
            actor=actor,
            reason=reason,
            resolved_at="2026-07-20T09:40:00+08:00",
        )


def test_confirm_submitted_resolution_requires_order_id(tmp_path: Path) -> None:
    make_uncertain_buy(tmp_path)

    with pytest.raises(ValueError, match="Futu order ID"):
        trend_review.resolve_trend_action(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
            resolution="confirm-submitted",
            actor="ray",
            reason="checked Futu history",
            resolved_at="2026-07-20T09:40:00+08:00",
        )


def test_contradictory_resolution_preserves_first_fact(tmp_path: Path) -> None:
    make_uncertain_buy(tmp_path)
    first = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="checked Futu history",
        resolved_at="2026-07-20T09:40:00+08:00",
    )
    original = first.read_bytes()

    with pytest.raises(ValueError, match="already resolved"):
        trend_review.resolve_trend_action(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
            resolution="abandon",
            actor="ray",
            reason="changed my mind",
            resolved_at="2026-07-20T09:41:00+08:00",
        )

    assert first.read_bytes() == original
    assert list(first.parent.glob("*.json")) == [first]


def test_concurrent_contradictory_resolutions_write_one_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_uncertain_buy(tmp_path)
    original = trend_review._write_immutable
    entered = Event()
    release = Event()
    second_entered = Event()
    counter_lock = Lock()
    writes = 0

    def delayed_write(path: Path, body: bytes) -> Path:
        nonlocal writes
        if path.parent.name == "resolutions":
            with counter_lock:
                writes += 1
                write_number = writes
            if write_number == 1:
                entered.set()
                assert release.wait(timeout=2)
            else:
                second_entered.set()
        return original(path, body)

    monkeypatch.setattr(trend_review, "_write_immutable", delayed_write)

    def resolve(resolution: str, resolved_at: str) -> object:
        try:
            return trend_review.resolve_trend_action(
                tmp_path,
                market="CN",
                execution_date="2026-07-20",
                symbol="600001",
                side="buy",
                resolution=resolution,
                actor="ray",
                reason="checked Futu history",
                resolved_at=resolved_at,
                futu_order_id=(
                    "SIM-42" if resolution == "confirm-submitted" else None
                ),
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            resolve, "confirm-submitted", "2026-07-20T09:40:00+08:00"
        )
        assert entered.wait(timeout=2)
        second = pool.submit(
            resolve, "abandon", "2026-07-20T09:41:00+08:00"
        )
        second_entered.wait(timeout=0.2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    paths = list(
        tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/resolutions/*.json"
        )
    )
    assert sum(isinstance(result, Path) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(paths) == 1


def test_only_authorize_retry_permits_attempt_two(tmp_path: Path) -> None:
    client = make_uncertain_buy(tmp_path)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": {"SH.600001": Decimal("10")},
    }

    unresolved = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:33:00+08:00"
    )
    assert unresolved["status"] == "uncertain"
    assert len(client.requests) == 1

    resolution_path = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="broker confirmed no order",
        resolved_at="2026-07-20T09:40:00+08:00",
    )
    assert json.loads(resolution_path.read_text(encoding="utf-8"))["attempt_no"] == 1
    retried = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:41:00+08:00"
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )

    assert retried["submitted_count"] == 1
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN", "2026-07-20", action_key, 2
    )


@pytest.mark.parametrize("kind", ["duplicate", "contradictory"])
def test_resolution_loader_rejects_migrated_duplicate_attempt(
    tmp_path: Path, kind: str,
) -> None:
    client = make_uncertain_buy(tmp_path)
    first = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="broker checked",
        resolved_at="2026-07-20T09:40:00+08:00",
    )
    migrated = json.loads(first.read_text(encoding="utf-8"))
    migrated["resolved_at"] = "2026-07-20T09:41:00+08:00"
    if kind == "contradictory":
        migrated.update(resolution="abandon", status="abandoned")
    (first.parent / f"migrated-{kind}.json").write_text(
        json.dumps(migrated), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="invalid trend action resolution"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=cn_buy_report(),
            client=client,
            market="CN",
            execution_date="2026-07-20",
            now="2026-07-20T09:42:00+08:00",
            quote_prices={"SH.600001": Decimal("10")},
        )


def test_resolution_loader_rejects_future_attempt_authorization(
    tmp_path: Path,
) -> None:
    client = make_uncertain_buy(tmp_path)
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    resolution_root = (
        tmp_path
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / action_key
        / "resolutions"
    )
    resolution_root.mkdir(parents=True)
    (resolution_root / "future.json").write_text(
        json.dumps({
            "schema_version": "open_trader.trend_review.resolution.v1",
            "market": "CN",
            "execution_date": "2026-07-20",
            "action_key": action_key,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "attempt_no": 2,
            "resolution": "authorize-retry",
            "status": "retry_authorized",
            "actor": "ray",
            "reason": "future approval",
            "futu_order_id": None,
            "resolved_at": "2026-07-20T09:40:00+08:00",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend action resolution"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=cn_buy_report(),
            client=client,
            market="CN",
            execution_date="2026-07-20",
            now="2026-07-20T09:41:00+08:00",
            quote_prices={"SH.600001": Decimal("10")},
        )


def test_authorize_retry_is_consumed_by_one_uncertain_attempt(tmp_path: Path) -> None:
    client = make_uncertain_buy(tmp_path)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="broker confirmed no first order",
        resolved_at="2026-07-20T09:40:00+08:00",
    )
    attempt_two = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:41:00+08:00"
    )
    client.orders.clear()

    blocked = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:42:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    ]

    assert attempt_two["submitted_count"] == 1
    assert blocked["status"] == "uncertain"
    assert blocked["submitted_count"] == 0
    assert len(client.requests) == 2
    assert any(
        event.get("status") == "uncertain"
        and event.get("attempt") == 2
        and event.get("reason") == "broker order status is absent"
        for event in events
    )


def make_second_buy_attempt(
    tmp_path: Path,
) -> tuple[
    FakeTrendSimClient,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )
    first_order = {
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "200",
        "dealt_avg_price": "10",
        "order_status": "CANCELLED_PART",
    }
    client.orders = [first_order]
    retried = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:32:00+08:00"
    )

    assert retried["submitted_count"] == 1
    return client, arguments, first_order, client.orders[-1]


def test_old_terminal_order_does_not_mask_latest_attempt_absence(
    tmp_path: Path,
) -> None:
    client, arguments, first_order, _ = make_second_buy_attempt(tmp_path)
    client.orders = [first_order]

    blocked = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:33:00+08:00"
    )

    assert blocked["status"] == "uncertain"
    assert blocked["submitted_count"] == 0
    assert len(client.requests) == 2


def test_latest_attempt_authorization_records_and_consumes_exact_attempt(
    tmp_path: Path,
) -> None:
    client, arguments, first_order, _ = make_second_buy_attempt(tmp_path)
    client.orders = [first_order]
    assert trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:33:00+08:00"
    )["status"] == "uncertain"

    resolution_path = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="broker confirmed no second order",
        resolved_at="2026-07-20T09:40:00+08:00",
    )
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    retried = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:41:00+08:00"
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )

    assert resolution["attempt_no"] == 2
    assert retried["submitted_count"] == 1
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN", "2026-07-20", action_key, 3
    )


def test_uncertain_attempt_rejects_a_second_resolution(tmp_path: Path) -> None:
    client, arguments, first_order, second_order = make_second_buy_attempt(tmp_path)
    client.orders = [
        first_order,
        {**second_order, "order_status": "UNKNOWN"},
    ]
    assert trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:33:00+08:00"
    )["status"] == "uncertain"
    first = trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-20",
        symbol="600001",
        side="buy",
        resolution="authorize-retry",
        actor="ray",
        reason="broker checked",
        resolved_at="2026-07-20T09:40:00+08:00",
    )

    with pytest.raises(ValueError, match="already resolved"):
        trend_review.resolve_trend_action(
            tmp_path,
            market="CN",
            execution_date="2026-07-20",
            symbol="600001",
            side="buy",
            resolution="authorize-retry",
            actor="ray",
            reason="duplicate approval",
            resolved_at="2026-07-20T09:41:00+08:00",
        )

    assert len(list(first.parent.glob("*.json"))) == 1


def test_legacy_intent_is_discovered_by_symbol_and_side(tmp_path: Path) -> None:
    request = {
        "market": "CN",
        "futu_code": "SH.600001",
        "side": "buy",
        "order_type": "MARKET",
        "price": "0",
        "qty": "400",
        "remark": "trend-review:CN:2026-07-20:legacy",
    }
    intent_path = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20/old-strategy-intent.json"
    )
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text(
        json.dumps(
            {
                "report_sha256": "b" * 64,
                "action_index": 0,
                "request": request,
                "created_at": "2026-07-20T09:30:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    client = FakeTrendSimClient()
    client.orders = [
        {
            "order_id": "SIM-LEGACY",
            "remark": request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        }
    ]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 0
    assert client.requests == []
    assert intent_path.exists()
    assert intent_path.with_name("old-strategy-result.json").exists()


def test_broker_only_legacy_remark_repairs_canonical_ledger(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    client.orders = [
        {
            "order_id": "SIM-LEGACY",
            "remark": "trend-review:CN:2026-07-20:old-action",
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        }
    ]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 0
    assert client.requests == []
    result_path = next(
        tmp_path.glob("trend_review/ledgers/CN/open/2026-07-20/*-result.json")
    )
    repaired = json.loads(result_path.read_text(encoding="utf-8"))
    assert repaired["request"]["remark"] == client.orders[0]["remark"]


def test_broker_only_canonical_and_legacy_candidates_fail_closed(
    tmp_path: Path,
) -> None:
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    common = {
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "0",
        "order_status": "SUBMITTED",
    }
    client = FakeTrendSimClient()
    client.orders = [
        {
            **common,
            "order_id": "SIM-CANONICAL",
            "remark": trend_review.trend_attempt_remark(
                "CN", "2026-07-20", action_key, 1
            ),
        },
        {
            **common,
            "order_id": "SIM-LEGACY",
            "remark": "trend-review:CN:2026-07-20:old-action",
        },
    ]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["status"] == "conflict"
    assert client.requests == []


def test_result_only_exact_fact_prevents_new_submission(tmp_path: Path) -> None:
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    request = {
        "market": "CN",
        "futu_code": "SH.600001",
        "side": "buy",
        "order_type": "MARKET",
        "price": "0",
        "qty": "400",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-20", action_key, 1
        ),
    }
    result_path = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20"
        / f"{action_key}-result.json"
    )
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "market": "CN",
                "date": "2026-07-20",
                "report_sha256": trend_review._report_hash(cn_buy_report()),
                "action_index": 0,
                "request": request,
                "response": {"futu_order_id": "SIM-RESULT-ONLY"},
                "submitted_at": "2026-07-20T09:30:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    client = FakeTrendSimClient()

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 0
    assert client.requests == []
    assert result_path.exists()


def test_malformed_result_only_fact_blocks_without_submission(
    tmp_path: Path,
) -> None:
    result_path = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-20/broken-result.json"
    )
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "request": {"futu_code": "SH.600001"},
                "submitted_at": "2026-07-20T09:30:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    client = FakeTrendSimClient()

    with pytest.raises(ValueError, match="invalid trend review result"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=cn_buy_report(),
            client=client,
            market="CN",
                execution_date="2026-07-20",
                now="2026-07-20T09:31:00+08:00",
                quote_prices=TEST_QUOTE_PRICES,
        )

    assert client.requests == []


def test_result_only_attempt_number_is_not_reused(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    first_request = client.requests[0]
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": first_request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "100",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )
    second_request = client.requests[-1]
    attempt_two_intent = next(
        tmp_path.glob(
            "trend_review/ledgers/CN/open/2026-07-17/*-attempt-2-intent.json"
        )
    )
    attempt_two_intent.unlink()
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": first_request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "100",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        },
        {
            "order_id": "SIM-2",
            "remark": second_request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "300",
            "dealt_qty": "100",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        },
    ]

    result = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:34:00+08:00"
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600001", "buy"
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN", "2026-07-17", action_key, 3
    )
    assert client.requests[-1]["qty"] == "200"


def test_open_uses_frozen_report_quantity_despite_live_nav_and_price(tmp_path: Path) -> None:
    client = FakeTrendSimClient(nav="900000")
    report = cn_buy_report(shares=300)

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    repeated = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert client.requests[0]["qty"] == "300"
    assert client.requests[0]["order_type"] == "MARKET"
    assert result["submitted_count"] == 1
    assert repeated["submitted_count"] == 0
    assert len(client.requests) == 1
    events = sorted(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*/*.json")
    )
    assert json.loads(events[-1].read_text(encoding="utf-8")) | {
        "symbol": "600001",
        "side": "buy",
        "status": "submitted",
        "target_qty": "300",
        "order_ids": ["SIM-1"],
        "action_index": 0,
    } == json.loads(events[-1].read_text(encoding="utf-8"))


def test_completed_buy_with_empty_broker_history_never_resubmits(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders.clear()

    result = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1


def test_us_open_uses_us_market_date_after_shanghai_midnight(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    report = cn_buy_report(symbol="NDAQ")
    report["strategy_judgments"]["formal_actions"][0]["lot_size"] = 1

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="US",
        execution_date="2026-07-17",
        now="2026-07-18T00:30:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert client.requests[0]["futu_code"] == "US.NDAQ"


def test_us_open_does_not_carry_market_order_after_close(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    report = cn_buy_report(symbol="NDAQ")
    report["strategy_judgments"]["formal_actions"][0]["lot_size"] = 1

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="US",
        execution_date="2026-07-17",
        now="2026-07-17T19:54:00-04:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["status"] == "missed_window"
    assert result["submitted_count"] == 0
    assert client.requests == []
    events = list(
        tmp_path.glob("trend_review/ledgers/US/actions/2026-07-17/*/*.json")
    )
    assert len(events) == 1
    assert json.loads(events[0].read_text(encoding="utf-8")) | {
        "market": "US",
        "date": "2026-07-17",
        "symbol": "NDAQ",
        "side": "buy",
        "status": "missed",
        "reason": "buy_window_closed",
    } == json.loads(events[0].read_text(encoding="utf-8"))


def _late_buy_audit_fixture(tmp_path: Path) -> dict[str, Path]:
    report = cn_buy_report()
    report_path = tmp_path / "reports/2026-07-16.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = trend_review._report_hash(report)
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.record_trend_review_missed_buys(
        data_dir=tmp_path,
        report=report,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:01:00+08:00",
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600001", "buy"
    )
    request = {
        "market": "CN",
        "futu_code": "SH.600001",
        "side": "buy",
        "order_type": "MARKET",
        "price": "0",
        "qty": "300",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-17", action_key, 1
        ),
    }
    intent = (
        tmp_path
        / "trend_review/ledgers/CN/open/2026-07-17"
        / f"{action_key}-intent.json"
    )
    trend_review._write_immutable(
        intent,
        trend_review._canonical_json_bytes(
            {
                "market": "CN",
                "date": "2026-07-17",
                "report_sha256": report_sha,
                "action_index": 0,
                "request": request,
                "created_at": "2026-07-17T10:03:00+08:00",
            }
        ),
    )
    trend_review._write_immutable(
        trend_review._result_path(intent),
        trend_review._canonical_json_bytes(
            {
                "market": "CN",
                "date": "2026-07-17",
                "report_sha256": report_sha,
                "action_index": 0,
                "request": request,
                "response": {
                    "futu_order_id": "SIM-LATE-1",
                    "status": "submitted",
                },
                "submitted_at": "2026-07-17T10:03:00+08:00",
            }
        ),
    )
    event_path = trend_review._write_action_event(
        data_dir=tmp_path,
        market="CN",
        execution_date="2026-07-17",
        action_key=action_key,
        payload={
            "market": "CN",
            "date": "2026-07-17",
            "strategy_version": report["strategy_snapshot"]["strategy_version"],
            "report_sha256": report_sha,
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "submitted",
            "attempt": 1,
            "target_qty": "300",
            "order_ids": ["SIM-LATE-1"],
        },
        recorded_at="2026-07-17T10:03:00+08:00",
    )
    authorization = (
        tmp_path
        / "trend_controller/CN/late_buy_authorizations/2026-07-17.json"
    )
    trend_review._write_immutable(
        authorization,
        trend_review._canonical_json_bytes(
            {
                "schema_version":
                    "open_trader.trend_controller.late_buy_authorization.v1",
                "market": "CN",
                "as_of_date": "2026-07-16",
                "execution_date": "2026-07-17",
                "report_path": str(report_path),
                "report_sha256": report_sha,
                "actor": "ray",
                "reason": "explicit same-day simulated late buy",
                "authorized_at": "2026-07-17T10:02:00+08:00",
            }
        ),
    )

    return {
        "authorization": authorization,
        "intent": intent,
        "result": trend_review._result_path(intent),
        "event": event_path,
    }


def test_action_audit_accepts_buy_after_bound_late_authorization(
    tmp_path: Path,
) -> None:
    fixture = _late_buy_audit_fixture(tmp_path)

    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="buy",
    )

    assert [event["status"] for event in events] == ["missed", "submitted"]
    authorization = fixture["authorization"]
    payload = json.loads(authorization.read_text(encoding="utf-8"))
    payload["report_sha256"] = "0" * 64
    authorization.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid late buy authorization"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="buy",
        )


def test_action_audit_rejects_result_before_bound_late_authorization(
    tmp_path: Path,
) -> None:
    fixture = _late_buy_audit_fixture(tmp_path)
    result = json.loads(fixture["result"].read_text(encoding="utf-8"))
    result["submitted_at"] = "2026-07-17T10:01:00+08:00"
    fixture["result"].write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid late buy authorization"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="buy",
        )


def test_action_audit_rejects_event_before_bound_late_authorization(
    tmp_path: Path,
) -> None:
    fixture = _late_buy_audit_fixture(tmp_path)
    event = json.loads(fixture["event"].read_text(encoding="utf-8"))
    event["recorded_at"] = "2026-07-17T10:01:00+08:00"
    fixture["event"].write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid late buy authorization"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="buy",
        )


def _corrected_report_late_buy_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], FakeTrendSimClient]:
    original = report_with_actions([])
    original_path = tmp_path / "reports/2026-07-16.json"
    original_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=original_path,
        report=original,
        locked_at="2026-07-17T09:30:00+08:00",
    )

    corrected = cn_buy_report()
    corrected.update(
        {
            "as_of_date": "2026-07-16",
            "execution_date": "2026-07-17",
            "process_version": "a" * 40,
        }
    )
    corrected_path = tmp_path / "reports/2026-07-16-r1.json"
    corrected_path.write_text(json.dumps(corrected), encoding="utf-8")
    client = FakeTrendSimClient()
    missed = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=corrected,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:01:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    assert missed["submitted_count"] == 0
    assert list(
        tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    )
    authorization = (
        tmp_path
        / "trend_controller/CN/late_buy_authorizations/2026-07-17.json"
    )
    trend_review._write_immutable(
        authorization,
        trend_review._canonical_json_bytes(
            {
                "schema_version":
                    "open_trader.trend_controller.late_buy_authorization.v1",
                "market": "CN",
                "as_of_date": "2026-07-16",
                "execution_date": "2026-07-17",
                "report_path": str(corrected_path),
                "report_sha256": trend_review._report_hash(corrected),
                "actor": "ray",
                "reason": "recover buy suppressed by corrected report bug",
                "authorized_at": "2026-07-17T10:02:00+08:00",
            }
        ),
    )
    return corrected, client


def test_corrected_report_late_buy_waits_for_open_market(
    tmp_path: Path,
) -> None:
    corrected, client = _corrected_report_late_buy_fixture(tmp_path)

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=corrected,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T12:30:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["status"] == "missed_window"
    assert result["submitted_count"] == 0
    assert client.requests == []


def test_corrected_report_late_buy_submits_and_audits_at_open_market(
    tmp_path: Path,
) -> None:
    corrected, client = _corrected_report_late_buy_fixture(tmp_path)

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=corrected,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T13:01:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="buy",
    )
    intent_path = next(
        tmp_path.glob("trend_review/ledgers/CN/open/2026-07-17/*-intent.json")
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))

    assert result["status"] == "submitted"
    assert result["submitted_count"] == 1
    assert len(client.requests) == 1
    assert intent["report_sha256"] == trend_review._report_hash(corrected)
    assert [event["status"] for event in events] == ["missed", "submitted"]


def test_corrected_report_authorization_rejects_buy_already_in_locked_report(
    tmp_path: Path,
) -> None:
    original = cn_buy_report()
    original_path = tmp_path / "reports/2026-07-16.json"
    original_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=original_path,
        report=original,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.record_trend_review_missed_buys(
        data_dir=tmp_path,
        report=original,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:01:00+08:00",
    )
    corrected = {
        **cn_buy_report(),
        "as_of_date": "2026-07-16",
        "execution_date": "2026-07-17",
        "process_version": "a" * 40,
    }
    corrected_path = tmp_path / "reports/2026-07-16-r1.json"
    corrected_path.write_text(json.dumps(corrected), encoding="utf-8")
    authorization = (
        tmp_path
        / "trend_controller/CN/late_buy_authorizations/2026-07-17.json"
    )
    trend_review._write_immutable(
        authorization,
        trend_review._canonical_json_bytes(
            {
                "schema_version":
                    "open_trader.trend_controller.late_buy_authorization.v1",
                "market": "CN",
                "as_of_date": "2026-07-16",
                "execution_date": "2026-07-17",
                "report_path": str(corrected_path),
                "report_sha256": trend_review._report_hash(corrected),
                "actor": "ray",
                "reason": "invalid attempt to replace an ordinary missed buy",
                "authorized_at": "2026-07-17T10:02:00+08:00",
            }
        ),
    )

    with pytest.raises(ValueError, match="invalid late buy authorization"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=corrected,
            client=FakeTrendSimClient(),
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T13:01:00+08:00",
            quote_prices=TEST_QUOTE_PRICES,
        )


def test_report_revision_does_not_duplicate_existing_symbol_intent(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    first_report = cn_buy_report(symbol="600001")
    revised_report = cn_buy_report(symbol="600001")
    revised_report["process_version"] = "new-process"

    first = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=first_report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    repeated = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=revised_report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert first["submitted_count"] == 1
    assert repeated["submitted_count"] == 0
    assert len(client.requests) == 1


def test_formal_sell_all_submits_full_position_market_order(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.positions = [{"code": "SH.600001", "qty": "300"}]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report_with_actions(
            [{"action": "SELL_ALL", "symbol": "600001"}]
        ),
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:30:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1] | {
        "side": "sell",
        "order_type": "MARKET",
        "qty": "300",
        "futu_code": "SH.600001",
    } == client.requests[-1]


def test_formal_sell_all_suppresses_conflicting_buy(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "300"}]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report_with_actions(
            [
                {
                    "action": "BUY",
                    "symbol": "600001",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 200,
                    "atr": "0.5",
                },
                {"action": "SELL_ALL", "symbol": "600001"},
            ]
        ),
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert [request["side"] for request in client.requests] == ["sell"]


def test_formal_partial_sell_suppresses_conflicting_buy(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report_with_actions([
            {
                "action": "BUY",
                "symbol": "SH.600001",
                "target_weight": "0.04",
                "lot_size": 100,
                "estimated_shares": 200,
                "atr": "0.5",
            },
            partial_sell_report()["strategy_judgments"]["formal_actions"][0],
        ]),
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert [request["side"] for request in client.requests] == ["sell"]


def test_missed_buys_skip_a_canonically_matching_sell_symbol(
    tmp_path: Path,
) -> None:
    report = report_with_actions([
        partial_sell_report()["strategy_judgments"]["formal_actions"][0],
        {
            "action": "BUY",
            "symbol": "SH.600001",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 200,
            "atr": "0.5",
        },
    ])

    missed = trend_review.record_trend_review_missed_buys(
        data_dir=tmp_path,
        report=report,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:01:00+08:00",
    )

    assert missed == 0
    assert not list(tmp_path.glob("trend_review/ledgers/CN/actions/**/*.json"))


def test_open_submits_all_sells_before_frozen_buys_regardless_of_report_order(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "300"}]

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report_with_actions(
            [
                {
                    "action": "BUY",
                    "symbol": "600002",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 200,
                    "atr": "0.5",
                },
                {"action": "SELL_ALL", "symbol": "600001"},
            ]
        ),
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 2
    assert [request["side"] for request in client.requests] == ["sell", "buy"]
    assert client.requests[-1]["qty"] == "200"


def test_open_stops_unsubmitted_buys_when_a_required_sell_submission_fails(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "300"}]
    client.fail_orders = 1

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report_with_actions(
                [
                    {
                        "action": "BUY",
                        "symbol": "600002",
                        "target_weight": "0.04",
                        "lot_size": 100,
                        "estimated_shares": 200,
                        "atr": "0.5",
                    },
                    {"action": "SELL_ALL", "symbol": "600001"},
                ]
            ),
            client=client,
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
            quote_prices=TEST_QUOTE_PRICES,
        )

    assert [request["side"] for request in client.requests] == ["sell"]


def test_reconciled_rejected_sell_stops_frozen_buys_and_records_failure(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}],
        fail_orders=1,
        accepted_before_failure=True,
    )
    arguments = {
        "data_dir": tmp_path,
        "report": report_with_actions(
            [
                {
                    "action": "BUY",
                    "symbol": "600002",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 200,
                    "atr": "0.5",
                },
                {"action": "SELL_ALL", "symbol": "600001"},
            ]
        ),
        "client": client,
        "market": "CN",
            "execution_date": "2026-07-17",
            "now": "2026-07-17T09:31:00+08:00",
            "quote_prices": TEST_QUOTE_PRICES,
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    client.orders[0].update(
        order_id="SIM-REJECTED",
        order_status="SUBMIT_FAILED",
    )

    with pytest.raises(RuntimeError, match="SUBMIT_FAILED"):
        trend_review.execute_trend_review_open(**arguments)

    assert [request["side"] for request in client.requests] == ["sell"]
    failures = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
        if json.loads(path.read_text(encoding="utf-8")).get("status") == "failed"
    ]
    assert "simulate sell order rejected: SUBMIT_FAILED" in {
        failure["reason"] for failure in failures
    }


def test_open_preflights_all_actions_before_any_broker_or_ledger_side_effect(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    report = report_with_actions(
        [
            {"action": "SELL_ALL", "symbol": "600001"},
            {
                "action": "BUY",
                "symbol": "600002",
                "target_weight": "0.04",
                "lot_size": 100,
                "estimated_shares": 200,
            },
        ]
    )

    with pytest.raises(ValueError, match="trend review buy action is invalid"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=report,
            client=client,
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
            quote_prices=TEST_QUOTE_PRICES,
        )

    assert client.requests == []
    assert not (tmp_path / "trend_review/ledgers/CN").exists()


def test_incomplete_sell_all_recovers_after_execution_date_until_position_is_zero(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(symbol="600003"),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:32:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "300"}]
    report = report_with_actions([
        {
            "action": "BUY",
            "symbol": "600002",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 200,
            "atr": "0.5",
        },
        {"action": "SELL_ALL", "symbol": "600001"},
        {"action": "SELL_ALL", "symbol": "600003"},
    ])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:30:00+08:00"
    )
    remark = client.requests[0]["remark"]
    client.orders = [{
        "order_id": "SIM-1",
        "remark": remark,
        "code": "SH.600001",
        "trd_side": "SELL",
        "qty": "300",
        "dealt_qty": "200",
        "order_status": "CANCELLED_PART",
    }]
    client.positions = [
        {"code": "SH.600001", "qty": "100"},
        {"code": "SH.600003", "qty": "75"},
    ]

    day_two = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )

    assert day_two["submitted_count"] == 1
    assert [request["side"] for request in client.requests] == ["sell", "sell"]
    assert client.requests[-1]["qty"] == "100"
    client.orders = [
        client.orders[0],
        {
            "order_id": "SIM-2",
            "remark": client.requests[-1]["remark"],
            "code": "SH.600001",
            "trd_side": "SELL",
            "qty": "100",
            "dealt_qty": "100",
            "order_status": "FILLED",
        },
    ]
    client.positions = [{"code": "SH.600003", "qty": "75"}]
    request_count = len(client.requests)

    confirmed_zero = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-21T09:31:00+08:00"
    )

    assert confirmed_zero["submitted_count"] == 0
    assert len(client.requests) == request_count
    assert all(request["futu_code"] != "SH.600002" for request in client.requests)
    assert all(request["futu_code"] != "SH.600003" for request in client.requests)
    terminal_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
        if "position_zero_confirmed" in path.read_text(encoding="utf-8")
    ]
    assert {event["symbol"] for event in terminal_events} == {"600001", "600003"}
    completed_sell = next(
        event for event in terminal_events if event["symbol"] == "600001"
    )
    assert completed_sell | {
        "symbol": "600001",
        "side": "sell",
        "status": "filled",
        "reason": "position_zero_confirmed",
    } == completed_sell

    client.positions = [
        {"code": "SH.600001", "qty": "25"},
        {"code": "SH.600003", "qty": "75"},
    ]

    reacquired = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-22T09:31:00+08:00"
    )
    repeated = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-23T09:31:00+08:00"
    )

    assert reacquired["submitted_count"] == 0
    assert repeated["submitted_count"] == 0
    assert len(client.requests) == request_count
    assert sum(
        "position_zero_confirmed" in path.read_text(encoding="utf-8")
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ) == 2


def test_fresh_zero_position_sell_does_not_append_observation_on_repeat(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    report = report_with_actions([
        {"action": "SELL_ALL", "symbol": "600001"}
    ])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }

    first = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    repeated = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )

    assert first["submitted_count"] == 0
    assert repeated["submitted_count"] == 0
    assert client.requests == []
    assert len(list(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-observation-*.json"
    ))) == 1


def test_action_audit_rejects_observation_filename_with_wrong_digest_suffix(
    tmp_path: Path,
) -> None:
    report = report_with_actions([
        {"action": "SELL_ALL", "symbol": "600001"}
    ])
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=FakeTrendSimClient(),
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600001", "sell"
    )
    observation = next(tmp_path.glob(
        "trend_review/ledgers/CN/open/2026-07-17/*-observation-*.json"
    ))
    renamed = observation.with_name(
        f"{action_key}-observation-000000000000.json"
    )
    observation.rename(renamed)
    event_path = next(
        path
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
        if "position_zero_confirmed" in path.read_text(encoding="utf-8")
    )
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["observation_path"] = renamed.name
    event_path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action event evidence"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="sell",
        )


@pytest.mark.parametrize(
    ("status", "expected_submitted"),
    [
        ("failed", 1),
        ("submitted", 1),
        ("missed", 1),
        ("incomplete", 0),
        ("filled", 0),
    ],
)
def test_sell_recovery_stops_only_for_valid_position_zero_terminal_status(
    tmp_path: Path, status: str, expected_submitted: int,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "100"}]
    arguments = {
        "data_dir": tmp_path,
        "report": report_with_actions([
            {"action": "SELL_ALL", "symbol": "600001"}
        ]),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:30:00+08:00"
    )
    action_event = next(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*/*.json")
    )
    (action_event.parent / f"terminal-{status}.json").write_text(
        json.dumps({
            "status": status,
            "reason": "position_zero_confirmed",
        }),
        encoding="utf-8",
    )
    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "SELL",
        "qty": "100",
        "dealt_qty": "50",
        "order_status": "CANCELLED_PART",
    }]
    client.positions = [{"code": "SH.600001", "qty": "50"}]

    recovered = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )

    assert recovered["submitted_count"] == expected_submitted
    assert len(client.requests) == 1 + expected_submitted
    if expected_submitted:
        assert client.requests[-1]["qty"] == "50"


@pytest.mark.parametrize(
    ("dealt_qty", "order_status", "average_price", "terminal_status"),
    [
        ("40", "CANCELLED_PART", "10", "incomplete"),
        ("0", "CANCELLED", None, "incomplete"),
        ("100", "FILLED", "11", "filled"),
    ],
)
def test_position_zero_terminal_uses_actual_broker_fill_facts(
    tmp_path: Path,
    dealt_qty: str,
    order_status: str,
    average_price: str | None,
    terminal_status: str,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "100"}]
    report = report_with_actions([{"action": "SELL_ALL", "symbol": "600001"}])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:30:00+08:00"
    )
    request_count = len(client.requests)
    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "SELL",
        "qty": "100",
        "dealt_qty": dealt_qty,
        "order_status": order_status,
        **({"dealt_avg_price": average_price} if average_price is not None else {}),
    }]
    client.positions = []

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )
    terminal = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
        if "position_zero_confirmed" in path.read_text(encoding="utf-8")
    )

    assert terminal["status"] == terminal_status
    assert terminal["filled_qty"] == dealt_qty
    assert terminal["target_qty"] == "100"
    assert terminal["order_ids"] == ["SIM-1"]
    assert terminal["avg_fill_price"] == (average_price or "")
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
    )
    assert terminal in events
    client.positions = [{"code": "SH.600001", "qty": "25"}]
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-21T09:31:00+08:00"
    )
    assert len(client.requests) == request_count
    assert sum(
        "position_zero_confirmed" in path.read_text(encoding="utf-8")
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ) == 1


def test_positive_sell_recovery_uses_broker_fills_and_live_retry_quantity(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        market="CN",
        execution_date="2026-07-16",
        now="2026-07-16T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )
    client.requests.clear()
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "100"}]
    report = report_with_actions([{"action": "SELL_ALL", "symbol": "600001"}])
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:30:00+08:00"
    )
    action_glob = "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
    event_count = len(list(tmp_path.glob(action_glob)))
    client.orders.clear()
    client.positions = [{"code": "SH.600001", "qty": "50"}]

    unmatched = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )

    assert unmatched["submitted_count"] == 0
    assert unmatched["status"] == "uncertain"
    assert len(client.requests) == 1
    assert len(list(tmp_path.glob(action_glob))) == event_count + 1

    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "SELL",
        "qty": "100",
        "dealt_qty": "20",
        "dealt_avg_price": "10",
        "order_status": "CANCELLED_PART",
    }]
    recovered = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:32:00+08:00"
    )
    latest = next(
        event
        for event in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in tmp_path.glob(action_glob)
        )
        if event.get("status") == "partially_filled"
    )

    assert recovered["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "50"
    assert latest | {
        "status": "partially_filled",
        "filled_qty": "20",
        "target_qty": "100",
        "avg_fill_price": "10",
        "order_ids": ["SIM-1"],
    } == latest


def authorize_retry(
    tmp_path: Path,
    *,
    execution_date: str,
    symbol: str = "600001",
    side: str = "buy",
    resolved_at: str = "2026-07-17T09:40:00+08:00",
) -> Path:
    return trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date=execution_date,
        symbol=symbol,
        side=side,
        resolution="authorize-retry",
        actor="ray",
        reason="broker terminal status checked",
        resolved_at=resolved_at,
    )


def test_partial_buy_amount_cap_uses_current_quote_and_confirmed_notional(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    report = cn_buy_report()
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]
    result = trend_review.execute_trend_review_open(
        **{
            **arguments,
            "now": "2026-07-17T09:32:00+08:00",
            "quote_prices": {"SH.600001": Decimal("15")},
        }
    )
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600001", "buy"
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1] | {
        "qty": "100",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-17", action_key, 2
        ),
    } == client.requests[-1]
    assert Decimal("200") + Decimal(str(client.requests[-1]["qty"])) <= 400


def test_partial_buy_cash_below_one_lot_creates_no_attempt(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]
    client.cash = "999"

    result = trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1


@pytest.mark.parametrize("strategy_version", ["v2", "v3", "v4", "v6", "v7"])
def test_partial_buy_risk_cap_limits_retry_lots(
    tmp_path: Path, strategy_version: str
) -> None:
    report = cn_buy_report()
    report["strategy_snapshot"]["strategy_version"] = strategy_version
    action = report["strategy_judgments"]["formal_actions"][0]
    action["target_amount"] = "10000"
    action["planned_stop_risk"] = "350"
    report["risk_summary"] = {"normal_cost_rate": "0.001"}
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "300",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]
    result = trend_review.execute_trend_review_open(
        **{
            **arguments,
            "now": "2026-07-17T09:32:00+08:00",
            "quote_prices": {"SH.600001": Decimal("15")},
        }
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "100"


def test_partial_buy_after_window_is_marked_missed_once(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.positions = [{"code": "SH.600001", "qty": "200"}]
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:01:00+08:00"
    )
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:02:00+08:00"
    )
    missed = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
        if json.loads(path.read_text(encoding="utf-8")).get("status") == "missed"
    ]

    assert len(client.requests) == 1
    assert client.positions == [{"code": "SH.600001", "qty": "200"}]
    assert len(missed) == 1
    assert missed[0]["reason"] == "buy_window_closed"
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]
    partial = next(
        event for event in events if event.get("status") == "partially_filled"
    )
    assert partial["filled_qty"] == "200"
    assert partial["active_protection_line"] == "9.0"
    protection = json.loads(
        (tmp_path / "trend_a_share/protection_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert protection["positions"]["600001"]["active_line"] == "9.0"


def test_fully_filled_buy_observed_after_window_is_not_marked_missed(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "400",
        "dealt_avg_price": "10",
        "order_status": "FILLED_ALL",
    }]

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:01:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert any(event.get("status") == "filled" for event in events)
    assert not any(event.get("status") == "missed" for event in events)
    assert json.loads(
        (tmp_path / "trend_a_share/protection_state.json").read_text(
            encoding="utf-8"
        )
    )["positions"]["600001"]["active_line"] == "9.0"


def test_response_failure_partial_buy_reconciles_before_after_window_missed(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1, accepted_before_failure=True)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(
            **arguments, now="2026-07-17T09:31:00+08:00"
        )
    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "200",
        "dealt_avg_price": "10",
        "order_status": "CANCELLED_PART",
    }]

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T10:01:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert any(event.get("status") == "partially_filled" for event in events)
    assert sum(event.get("status") == "missed" for event in events) == 1
    assert json.loads(
        (tmp_path / "trend_a_share/protection_state.json").read_text(
            encoding="utf-8"
        )
    )["positions"]["600001"]["active_line"] == "9.0"


def test_partial_buy_restart_after_execution_date_records_durable_missed(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders = [{
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "200",
        "dealt_avg_price": "10",
        "order_status": "CANCELLED_PART",
    }]

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-20T09:31:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert any(event.get("status") == "partially_filled" for event in events)
    assert sum(event.get("status") == "missed" for event in events) == 1
    assert next(
        event for event in events if event.get("status") == "missed"
    )["reason"] == "buy_window_closed"
    assert len(client.requests) == 1


def test_missing_buy_quote_skips_only_that_buy_after_sell(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    report = report_with_actions([
        {"action": "SELL_ALL", "symbol": "600001"},
        {
            "action": "BUY",
            "symbol": "600002",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "600003",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "atr": "0.5",
        },
    ])

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={"SH.600003": Decimal("10")},
    )
    missing_action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600002", "buy"
    )
    missing_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            tmp_path
            / "trend_review/ledgers/CN/actions/2026-07-17"
            / missing_action_key
        ).glob("*.json")
    ]

    assert result["status"] == "quote_unavailable"
    assert result["submitted_count"] == 2
    assert [request["side"] for request in client.requests] == ["sell", "buy"]
    assert [request["futu_code"] for request in client.requests] == [
        "SH.600001",
        "SH.600003",
    ]
    assert any(
        event.get("status") == "pending"
        and event.get("reason") == "current_quote_unavailable"
        for event in missing_events
    )


def test_partial_buy_only_submits_unfilled_remainder(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(**arguments)
    remark = client.requests[0]["remark"]
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": remark,
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]

    result = trend_review.execute_trend_review_open(
        **{**arguments, "now": "2026-07-17T09:32:00+08:00"}
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "200"
    assert client.requests[-1]["remark"] != remark


def test_existing_broker_retry_repairs_result_without_duplicate_submit(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    trend_review.execute_trend_review_open(**arguments)
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-17", "SH.600001", "buy"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        },
        {
            "order_id": "SIM-2",
            "remark": trend_review.trend_attempt_remark(
                "CN", "2026-07-17", action_key, 2
            ),
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "200",
            "dealt_qty": "0",
            "order_status": "SUBMITTED",
        },
    ]

    result = trend_review.execute_trend_review_open(
        **{**arguments, "now": "2026-07-17T09:32:00+08:00"}
    )

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1
    assert len(
        list(
            tmp_path.glob(
                "trend_review/ledgers/CN/open/2026-07-17/*-result.json"
            )
        )
    ) == 2


def test_active_partial_buy_waits_instead_of_duplicate_submission(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
        "now": "2026-07-17T09:31:00+08:00",
    }
    trend_review.execute_trend_review_open(**arguments)
    request = client.requests[0]
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "200",
            "order_status": "FILLED_PART",
        }
    ]

    result = trend_review.execute_trend_review_open(
        **{**arguments, "now": "2026-07-17T09:32:00+08:00"}
    )

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1
    events = sorted(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*/*.json")
    )
    latest = json.loads(events[-1].read_text(encoding="utf-8"))
    assert latest["status"] == "partially_filled"
    assert latest["filled_qty"] == "200"
    assert latest["target_qty"] == "400"
    assert latest["order_ids"] == ["SIM-1"]


def test_filled_buy_records_active_protection_line_without_mutating_report(
    tmp_path: Path,
) -> None:
    report = cn_buy_report()
    report["strategy_judgments"]["formal_actions"][0]["atr"] = "1.25"
    report_path = tmp_path / "frozen-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original_bytes = report_path.read_bytes()
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": json.loads(original_bytes),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    request = client.requests[0]
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": request["remark"],
            "code": "SH.600001",
            "trd_side": "BUY",
            "qty": "400",
            "dealt_qty": "400",
            "dealt_avg_price": "12.50",
            "order_status": "FILLED_ALL",
        }
    ]

    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )

    events = sorted(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*/*.json")
    )
    filled = json.loads(events[-1].read_text(encoding="utf-8"))
    state_path = tmp_path / "trend_a_share/protection_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert filled["status"] == "filled"
    assert filled["avg_fill_price"] == "12.50"
    assert filled["active_protection_line"] == "10.00"
    assert state["positions"]["600001"] | {
        "initial_line": "10.00",
        "active_line": "10.00",
        "atr14": "1.25",
        "position_started_for": "2026-07-17",
        "tracking_active": False,
        "updated_for": "2026-07-17",
    } == state["positions"]["600001"]
    from open_trader.a_share_trend_watch import _load_active_lines

    assert _load_active_lines(state_path) == {"600001": Decimal("10.00")}
    assert report_path.read_bytes() == original_bytes
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == hashlib.sha256(
        original_bytes
    ).hexdigest()


def test_open_marks_intent_uncertain_when_failed_order_is_absent_at_broker(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
        "quote_prices": TEST_QUOTE_PRICES,
    }

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    result = trend_review.execute_trend_review_open(**arguments)

    assert result["status"] == "uncertain"
    assert result["submitted_count"] == 0
    assert len(client.requests) == 1


def test_open_reconciles_accepted_order_after_response_failure(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1, accepted_before_failure=True)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
        "quote_prices": TEST_QUOTE_PRICES,
    }

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    client.orders[0] = {
        "remark": client.orders[0]["remark"],
        "code": " sh.600001 ",
        "trd_side": "BUY",
        "qty": "400.0",
    }
    result = trend_review.execute_trend_review_open(**arguments)

    assert result["status"] == "uncertain"
    assert len(client.requests) == 1
    assert list(tmp_path.glob("trend_review/ledgers/CN/open/*/*-result.json"))


def test_newer_revision_reconciles_same_stable_action_after_response_failure(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1, accepted_before_failure=True)
    first = {
        "data_dir": tmp_path,
        "report": cn_buy_report(symbol="600001"),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
        "quote_prices": TEST_QUOTE_PRICES,
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**first)

    revised = {
        **first,
        "report": {
            **cn_buy_report(symbol="600001"),
            "generated_at": "2026-07-17T09:32:00+08:00",
        },
    }
    result = trend_review.execute_trend_review_open(**revised)

    assert result["status"] == "unchanged"
    assert result["submitted_count"] == 0
    assert len(client.requests) == 1


def test_first_open_binds_discipline_account_with_existing_sell_position(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "100"}]
    )

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report_with_actions(
            [{"action": "SELL_ALL", "symbol": "600001"}]
        ),
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices=TEST_QUOTE_PRICES,
    )

    assert result["submitted_count"] == 1
    assert client.requests[0]["side"] == "sell"
    assert client.requests[0]["qty"] == "100"


def test_close_keeps_actual_equity_out_of_simulation_report_state(tmp_path: Path) -> None:
    path = trend_review.capture_trend_review_close(
        data_dir=tmp_path,
        market="CN",
        trading_date="2026-07-17",
        report=cn_buy_report(),
        simulate_snapshot={"acc_id": 101, "net_value": "101000", "positions": []},
        orders=[
            {"side": "BUY", "status": "FILLED", "notional": "4000"},
            {"side": "SELL", "status": "FILLED", "notional": "4200"},
        ],
        benchmark={
            "date": "2026-07-17",
            "close": "6123.45",
            "source_id": "CSI_500_PRICE",
            "futu_symbol": "SH.000905",
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["discipline_equity_after_fees"] == "101000.00"
    assert "actual_equity" not in payload


@pytest.mark.parametrize(
    "account",
    [
        {"net_value": "735164.41", "fresh": False, "source_date": "2026-07-17"},
        {"net_value": "735164.41", "fresh": True, "source_date": "2026-07-16"},
    ],
)
def test_close_records_stale_or_misaligned_actual_equity_as_missing(
    tmp_path: Path, account: dict[str, object],
) -> None:
    report = cn_buy_report()
    report["account"] = account
    path = trend_review.capture_trend_review_close(
        data_dir=tmp_path,
        market="CN",
        trading_date="2026-07-17",
        report=report,
        simulate_snapshot={"acc_id": 101, "net_value": "101000", "positions": []},
        orders=[],
        benchmark={
            "date": "2026-07-17",
            "close": "6123.45",
            "source_id": "CSI_500_PRICE",
            "futu_symbol": "SH.000905",
        },
    )

    assert "actual_equity" not in json.loads(path.read_text(encoding="utf-8"))


def test_close_rejects_report_without_strategy_snapshot(tmp_path: Path) -> None:
    report = cn_buy_report()
    report.pop("strategy_snapshot")

    with pytest.raises(ValueError, match="strategy snapshot is unavailable"):
        trend_review.capture_trend_review_close(
            data_dir=tmp_path,
            market="CN",
            trading_date="2026-07-17",
            report=report,
            simulate_snapshot={
                "acc_id": 101,
                "net_value": "101000",
                "positions": [],
            },
            orders=[],
            benchmark={
                "date": "2026-07-17",
                "close": "6123.45",
                "source_id": "CSI_500_PRICE",
                "futu_symbol": "SH.000905",
            },
        )


class FiveYearQuote:
    def __init__(self, symbol: str = "US.SPY") -> None:
        self.symbol = symbol

    def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[object]:
        assert symbol == self.symbol
        closes = ("100", "108", "102", "121", "117", "150")
        years = range(2021, 2027)
        return [
            SimpleNamespace(date=f"{year}-08-08", close=close)
            for year, close in zip(years, closes, strict=True)
        ]


class ExplodingQuote:
    def get_daily_kline(self, *_args: object, **_kwargs: object) -> list[object]:
        raise ValueError("quote failed")


def write_rates(root: Path) -> None:
    path = root / "rates/DGS3MO.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DATE,DGS3MO\n2021-08-08,4.0\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("market", "source_id", "symbol"),
    [
        ("CN", "CSI_500_PRICE", "SH.000905"),
        ("HK", "HSI_PRICE", "HK.800000"),
        ("US", "SPY_QFQ", "US.SPY"),
    ],
)
def test_long_term_benchmark_uses_approved_market_identity(
    tmp_path: Path, market: str, source_id: str, symbol: str
) -> None:
    write_rates(tmp_path)
    result = trend_review.refresh_long_term_benchmark(
        tmp_path,
        market,
        FiveYearQuote(symbol=symbol),
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
        process_git_sha="abc123",
    )

    snapshot = json.loads(
        trend_review.long_term_benchmark_snapshot_path(tmp_path, market).read_text(
            encoding="utf-8"
        )
    )

    assert result["status"] == "completed"
    assert snapshot["benchmark"] == {
        "name": trend_review.BENCHMARK_IDENTITIES[market]["name"],
        "source_id": source_id,
        "futu_symbol": symbol,
    }
    assert set(snapshot["windows"]) == {"1Y", "5Y"}
    assert Decimal(snapshot["windows"]["5Y"]["metrics"]["annualized_return_pct"]).is_finite()


def test_long_term_benchmark_calculates_qfq_metrics_and_excess_returns(
    tmp_path: Path,
) -> None:
    write_rates(tmp_path)
    trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        FiveYearQuote(),
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
        process_git_sha="abc123",
    )
    snapshot = trend_review.read_long_term_benchmark_snapshot(tmp_path, "US")
    window = snapshot["windows"]["5Y"]

    assert snapshot["cutoff"] == "2026-08-08"
    assert window["observation_count"] == 6
    assert Decimal(window["metrics"]["max_drawdown_pct"]) == Decimal(50) / Decimal(9)
    assert window["metrics"]["calmar_ratio"] == "1.519624991897783716339100412"
    assert window["metrics"]["sharpe_ratio"] == "6.414798394700615362447213831"
    assert window["daily_returns"][0] == {
        "date": "2022-08-08",
        "return": "0.08",
        "risk_free_return": "0.04",
        "excess_return": "0.04",
    }


def test_long_term_benchmark_refresh_is_monthly_and_failure_preserves_snapshot(
    tmp_path: Path,
) -> None:
    write_rates(tmp_path)
    first = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        FiveYearQuote(),
        now=datetime(2026, 8, 9, tzinfo=UTC),
        process_git_sha="first",
    )
    snapshot_path = trend_review.long_term_benchmark_snapshot_path(tmp_path, "US")
    cycle_path = trend_review.long_term_benchmark_cycle_path(tmp_path, "US", "2026-08")
    body = snapshot_path.read_bytes()
    cycle = cycle_path.read_bytes()
    snapshot_path.unlink()
    skipped = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        ExplodingQuote(),
        now=datetime(2026, 8, 20, tzinfo=UTC),
        process_git_sha="second",
    )
    failed = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        ExplodingQuote(),
        now=datetime(2026, 9, 1, tzinfo=UTC),
        process_git_sha="third",
    )

    assert first["status"] == "completed"
    assert skipped["status"] == "already_completed"
    assert failed["status"] == "failed"
    assert snapshot_path.read_bytes() == body
    assert cycle_path.read_bytes() == cycle


def test_long_term_benchmark_force_requires_audited_reason_and_preserves_success(
    tmp_path: Path,
) -> None:
    write_rates(tmp_path)
    trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        FiveYearQuote(),
        now=datetime(2026, 8, 9, tzinfo=UTC),
        process_git_sha="first",
    )
    with pytest.raises(ValueError, match="actor and reason"):
        trend_review.refresh_long_term_benchmark(
            tmp_path,
            "US",
            FiveYearQuote(),
            now=datetime(2026, 8, 20, tzinfo=UTC),
            process_git_sha="second",
            force=True,
        )
    forced = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        FiveYearQuote(),
        now=datetime(2026, 8, 20, tzinfo=UTC),
        process_git_sha="second",
        force=True,
        actor="operator",
        reason="reconcile provider data",
    )
    snapshot_path = trend_review.long_term_benchmark_snapshot_path(tmp_path, "US")
    cycle_path = trend_review.long_term_benchmark_cycle_path(tmp_path, "US", "2026-08")
    body = snapshot_path.read_bytes()
    cycle = cycle_path.read_bytes()
    failed = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        ExplodingQuote(),
        now=datetime(2026, 8, 20, tzinfo=UTC),
        process_git_sha="second",
        force=True,
        actor="operator",
        reason="reconcile provider data",
    )

    assert forced["status"] == "completed"
    assert trend_review.read_long_term_benchmark_snapshot(tmp_path, "US")["refresh"] == {
        "force": True,
        "actor": "operator",
        "reason": "reconcile provider data",
    }
    assert failed["status"] == "failed"
    assert snapshot_path.read_bytes() == body
    assert cycle_path.read_bytes() == cycle


def test_long_term_benchmark_rejects_unordered_or_short_history(tmp_path: Path) -> None:
    write_rates(tmp_path)

    class InvalidQuote:
        def get_daily_kline(
            self, _symbol: str, *, start: str, end: str
        ) -> list[object]:
            return [
                SimpleNamespace(date="2026-08-08", close="100"),
                SimpleNamespace(date="2021-08-08", close="90"),
            ]

    result = trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        InvalidQuote(),
        now=datetime(2026, 8, 9, tzinfo=UTC),
        process_git_sha="invalid",
    )

    assert result["status"] == "failed"
    assert "strictly increasing" in result["error"]
    assert not trend_review.long_term_benchmark_snapshot_path(tmp_path, "US").exists()


def test_projection_keeps_newer_current_benchmark_facts_alongside_snapshot(
    tmp_path: Path,
) -> None:
    write_rates(tmp_path)
    snapshot = strategy_snapshot("US")
    for trading_date, benchmark_close in (("2026-07-17", "1000"), ("2026-08-08", "1100")):
        trend_review.freeze_discipline_fact(
            tmp_path, "US", trading_date, "100000", [], snapshot
        )
        trend_review.freeze_benchmark_fact(
            tmp_path,
            "US",
            trading_date,
            {
                "date": trading_date,
                "close": benchmark_close,
                "source_id": "SPY_QFQ",
                "futu_symbol": "US.SPY",
            },
        )
    assert trend_review.refresh_long_term_benchmark(
        tmp_path,
        "US",
        FiveYearQuote(),
        now=datetime(2026, 8, 9, tzinfo=UTC),
        process_git_sha="snapshot",
    )["status"] == "completed"

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["schema_version"] == "open_trader.trend_review.projection.v4"
    assert projection["metrics"]["period_net_return"]["discipline_benchmark"]["value"] == "10.0"


@pytest.mark.parametrize(
    ("market", "trading_date", "legacy_identity"),
    [
        ("CN", "2026-07-16", ("CSI_ALL_SHARE_PRICE", "SH.000985")),
        ("HK", "2026-07-17", ("HSCI_PRICE", "HK.800701")),
    ],
)
def test_legacy_benchmark_facts_are_readable_but_cannot_drive_new_metrics(
    tmp_path: Path,
    market: str,
    trading_date: str,
    legacy_identity: tuple[str, str],
) -> None:
    legacy = {
        "schema_version": "open_trader.trend_review.daily.v1",
        "market": market,
        "date": trading_date,
        "discipline_equity_after_fees": "100000",
        "actual_equity": "100000",
        "strategy_snapshot": strategy_snapshot(market),
        "orders": [],
        "benchmark": {
            "date": trading_date,
            "close": "1000",
            "source_id": legacy_identity[0],
            "futu_symbol": legacy_identity[1],
        },
    }
    destination = tmp_path / "trend_review/daily" / market / f"{trading_date}.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(legacy), encoding="utf-8")
    write_rates(tmp_path)

    assert trend_review._load_daily_facts(tmp_path, market)[0]["benchmark"] == legacy["benchmark"]
    with pytest.raises(ValueError, match="source_id"):
        trend_review.freeze_benchmark_fact(
            tmp_path,
            market,
            trading_date,
            legacy["benchmark"],
        )

    projection = trend_review.build_trend_review_projection(tmp_path, market)

    assert projection["schema_version"] == "open_trader.trend_review.projection.v4"
    assert projection["metrics"]["period_net_return"]["discipline_benchmark"] == {
        "value": None,
        "reason": "市场基准缺失",
    }


def test_benchmark_fact_uses_exact_market_qfq_close() -> None:
    class Quote:
        def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[object]:
            assert (symbol, start, end) == ("SH.000905", "2026-07-17", "2026-07-17")
            return [type("Bar", (), {"date": "2026-07-17", "close": 6123.45})()]

    assert trend_review.benchmark_fact(Quote(), "CN", "2026-07-17") == {
        "date": "2026-07-17",
        "close": "6123.45",
        "source_id": "CSI_500_PRICE",
        "futu_symbol": "SH.000905",
    }


def test_stop_sells_full_simulate_position_once(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )

    first = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="event-1",
        client=client,
        now="2026-07-17T10:15:00+08:00",
    )
    repeated = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="event-1",
        client=client,
        now="2026-07-17T10:16:00+08:00",
    )

    assert client.requests == [
        {
            "market": "CN",
            "futu_code": "SH.600001",
            "side": "sell",
            "order_type": "MARKET",
            "price": "0",
            "qty": "300",
            "remark": trend_review.trend_attempt_remark(
                "CN",
                "2026-07-17",
                trend_review.trend_action_key(
                    "CN", "2026-07-17", "SH.600001", "sell"
                ),
                1,
            ),
        }
    ]
    assert first["submitted_count"] == 1
    assert repeated["submitted_count"] == 0


def test_protection_upgrade_does_not_overlap_a_prior_partial_order(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )

    result = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1
    assert any(
        event.get("status") == "reason_added"
        and event.get("reason_id") == "protection-1"
        and event.get("sell_goal") == "position_zero"
        for event in events
    )


@pytest.mark.parametrize(
    ("dealt_qty", "order_status", "live_qty"),
    [
        ("0", "CANCELLED_ALL", "1000"),
        ("300", "FILLED_ALL", "700"),
    ],
)
def test_protection_upgrade_uses_live_remainder_after_terminal_partial_order(
    tmp_path: Path,
    dealt_qty: str,
    order_status: str,
    live_qty: str,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    client.orders[0].update(
        {"dealt_qty": dealt_qty, "order_status": order_status}
    )
    client.positions = [{"code": "SH.600001", "qty": live_qty}]

    result = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1]["qty"] == live_qty
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN",
        "2026-07-17",
        trend_review.trend_action_key(
            "CN", "2026-07-17", "SH.600001", "sell"
        ),
        2,
    )


def test_protection_upgrade_ignores_a_prior_position_lifecycle(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    client.orders[0].update({"dealt_qty": "300", "order_status": "FILLED_ALL"})
    write_protection_state(
        tmp_path / "trend_a_share/protection_state.json",
        {
            "schema_version": 1,
            "positions": {
                "600001": {
                    "position_started_for": "2026-07-18",
                    "updated_for": "2026-07-18",
                }
            },
        },
    )

    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )

    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN",
        "2026-07-18",
        trend_review.trend_action_key(
            "CN", "2026-07-18", "SH.600001", "sell"
        ),
        1,
    )


def test_protection_upgrade_uses_the_latest_partial_lifecycle_action(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    lock_partial_report(tmp_path, "2026-07-18")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    client.orders[0].update(
        {"dealt_qty": "100", "order_status": "CANCELLED_PART"}
    )
    client.positions = [{"code": "SH.600001", "qty": "900"}]
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices={},
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-18",
        now="2026-07-18T09:31:00+08:00",
        quote_prices={},
    )

    result = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )

    assert result["status"] == "submitted"
    assert len(client.requests) == 2
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN",
        "2026-07-18",
        trend_review.trend_action_key(
            "CN", "2026-07-18", "SH.600001", "sell"
        ),
        1,
    )


@pytest.mark.parametrize(
    ("old_order_status", "expected_status"),
    [("SUBMITTED", "submitted"), ("UNKNOWN", "uncertain")],
)
def test_protection_upgrade_checks_earlier_partial_order_history(
    tmp_path: Path, old_order_status: str, expected_status: str
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    lock_partial_report(tmp_path, "2026-07-18")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    first_order = dict(client.orders[0])
    client.orders[0].update({"order_status": "CANCELLED_ALL"})
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices={},
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-18",
        now="2026-07-18T09:31:00+08:00",
        quote_prices={},
    )
    client.orders = [
        {**first_order, "order_status": old_order_status},
        {**client.orders[-1], "order_status": "CANCELLED_ALL"},
    ]

    result = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )

    assert result["status"] == expected_status
    assert result["submitted_count"] == 0
    assert len(client.requests) == 2
    assert client.list_order_calls[-1]["start"] == "2026-07-17"


def test_protection_upgrade_succeeds_after_partial_abandon(tmp_path: Path) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    client.orders.clear()
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
        quote_prices={},
    )
    trend_review.resolve_trend_action(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
        resolution="abandon",
        actor="ray",
        reason="broker cannot identify the partial order",
        resolved_at="2026-07-17T09:33:00+08:00",
    )

    result = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=client,
        now="2026-07-17T10:15:00+08:00",
    )
    progress = trend_review.overheat_trim_progress(
        tmp_path,
        market="CN",
        symbol="600001",
        position_started_for="2026-07-01",
    )

    assert result["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "1000"
    assert progress["lifecycle_target_qty"] == "300"
    assert progress["filled_qty"] == "0"
    repeated = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=client,
        now="2026-07-17T10:16:00+08:00",
    )

    assert repeated["submitted_count"] == 0
    assert len(client.requests) == 2


def test_overheat_projection_ignores_standalone_position_zero_protection(
    tmp_path: Path,
) -> None:
    report = report_with_actions([])
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T09:30:00+08:00",
    )
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=FakeTrendSimClient(
            positions=[{"code": "SH.600001", "qty": "300"}]
        ),
        now="2026-07-17T10:15:00+08:00",
    )
    state_path = tmp_path / "trend_a_share/protection_state.json"
    write_protection_state(
        state_path,
        {
            "schema_version": 1,
            "positions": {
                "600001": {
                    "position_started_for": "2026-07-01",
                    "updated_for": "2026-07-16",
                }
            },
        },
    )

    projection = trend_review.rebuild_overheat_trim_projection(
        tmp_path, market="CN", state_path=state_path
    )

    assert projection["positions"]["600001"] == {
        "position_started_for": "2026-07-01",
        "updated_for": "2026-07-16",
    }


def test_protection_upgrade_waits_for_uncertain_partial_then_sells_live_remainder(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    arguments = {
        "data_dir": tmp_path,
        "report": report,
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-17",
        "quote_prices": {},
    }
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:31:00+08:00"
    )
    client.orders.clear()
    trend_review.execute_trend_review_open(
        **arguments, now="2026-07-17T09:32:00+08:00"
    )

    blocked = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )

    assert blocked["status"] == "uncertain"
    assert len(client.requests) == 1
    authorize_retry(
        tmp_path,
        execution_date="2026-07-17",
        side="sell",
        resolved_at="2026-07-18T10:16:00+08:00",
    )
    resolved = trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:17:00+08:00",
    )

    assert resolved["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "1000"


def test_position_zero_upgrade_audits_only_the_full_exit_attempt(
    tmp_path: Path,
) -> None:
    report = lock_partial_report(tmp_path, "2026-07-17")
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "1000"}]
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
        quote_prices={},
    )
    client.orders[0].update({"dealt_qty": "300", "order_status": "FILLED_ALL"})
    client.positions = [{"code": "SH.600001", "qty": "700"}]
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:15:00+08:00",
    )
    client.orders[-1].update({"dealt_qty": "700", "order_status": "FILLED_ALL"})
    client.positions = []
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-18",
        event_id="protection-1",
        client=client,
        now="2026-07-18T10:16:00+08:00",
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
    )
    terminal = next(
        event
        for event in events
        if event.get("reason") == "position_zero_confirmed"
    )

    assert terminal | {
        "sell_goal": "position_zero",
        "filled_qty": "700",
        "target_qty": "700",
        "order_ids": ["SIM-2"],
    } == terminal


def test_stop_retries_intent_when_failed_order_is_absent_at_broker(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}],
        fail_orders=1,
    )
    arguments = {
        "data_dir": tmp_path,
        "market": "CN",
        "symbol": "600001",
        "trading_date": "2026-07-17",
        "event_id": "event-1",
        "client": client,
        "now": "2026-07-17T10:15:00+08:00",
    }

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_stop(**arguments)
    result = trend_review.execute_trend_review_stop(**arguments)

    assert result["status"] == "uncertain"
    authorize_retry(
        tmp_path,
        execution_date="2026-07-17",
        side="sell",
        resolved_at="2026-07-17T10:16:00+08:00",
    )
    result = trend_review.execute_trend_review_stop(
        **{**arguments, "now": "2026-07-17T10:17:00+08:00"}
    )

    assert result["status"] == "submitted"
    assert result["submitted_count"] == 1
    assert len(client.requests) == 2


def strategy_snapshot(market: str = "CN", version: str = "v1") -> dict[str, object]:
    snapshot = trend_strategy_snapshot(market, "test-sha", ())
    snapshot = {
        **snapshot,
        "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
        "strategy_version": version,
    }
    if version == "v1":
        snapshot["effective_from"] = trend_review.TREND_V1_EFFECTIVE_FROM[market]
        parameters = snapshot["parameters"]
        assert isinstance(parameters, dict)
        snapshot["parameters"] = {
            key: value
            for key, value in parameters.items()
            if key
            not in {
                "single_entry_risk_limit",
                "portfolio_risk_limit",
                "abnormal_loss_buffer",
                "normal_cost_rate",
                "normal_cost_model",
            }
        }
    return snapshot


@pytest.mark.parametrize(
    ("market", "trading_date"),
    [("CN", "2026-07-16"), ("HK", "2026-07-16"), ("US", "2026-07-16")],
)
def test_repository_legacy_snapshots_adapt_without_rewrite(
    market: str, trading_date: str,
) -> None:
    path = Path(f"data/trend_review/daily/{market}/{trading_date}.json")
    original = path.read_bytes()
    snapshot = json.loads(original)["strategy_snapshot"]

    normalized = trend_review.normalize_trend_strategy_snapshot(
        snapshot, market
    )

    assert path.read_bytes() == original
    assert normalized == trend_strategy_snapshot(
        market,
        snapshot["process_version"],
        tuple(snapshot["parameters"]["candidate_pool_ids"]),
    )


def test_separate_fact_legacy_snapshot_adapts_without_rewrite(
    tmp_path: Path,
) -> None:
    legacy = json.loads(
        Path("data/trend_review/daily/CN/2026-07-16.json").read_text(
            encoding="utf-8"
        )
    )["strategy_snapshot"]
    discipline_path = trend_review.freeze_discipline_fact(
        tmp_path, "CN", "2026-07-16", "100000", [], legacy
    )
    trend_review.freeze_actual_equity_fact(
        tmp_path, "CN", "2026-07-16", "100000", [], legacy
    )
    trend_review.freeze_benchmark_fact(
        tmp_path,
        "CN",
        "2026-07-16",
        {
            "date": "2026-07-16",
            "close": "1000",
            "source_id": "CSI_500_PRICE",
            "futu_symbol": "SH.000905",
        },
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [],
        "2026-07-16",
        coverage_start="2026-07-16",
    )
    rates = tmp_path / "rates/DGS3MO.csv"
    rates.parent.mkdir()
    rates.write_text("DATE,DGS3MO\n2026-07-16,4.25\n", encoding="utf-8")
    original = discipline_path.read_bytes()

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert discipline_path.read_bytes() == original
    assert projection["strategy_snapshot"] == trend_strategy_snapshot(
        "CN",
        legacy["process_version"],
        tuple(legacy["parameters"]["candidate_pool_ids"]),
    )


def test_legacy_snapshot_adapter_rejects_unapproved_parameter_drift() -> None:
    snapshot = json.loads(
        Path("data/trend_review/daily/CN/2026-07-16.json").read_text(
            encoding="utf-8"
        )
    )["strategy_snapshot"]
    snapshot["parameters"]["position_limit"] = 9

    with pytest.raises(ValueError, match="known legacy rules"):
        trend_review.normalize_trend_strategy_snapshot(snapshot, "CN")


def actual_fill(
    source_id: str,
    symbol: str,
    side: str,
    quantity: str,
    executed_at: str,
    *,
    account_alias: str = "eastmoney_main",
    price: str = "10",
    source_sequence: int | None = None,
) -> TradeFill:
    return TradeFill(
        source_id=source_id,
        source_order_id=None,
        broker="eastmoney",
        account_alias=account_alias,
        market=Market.CN,
        symbol=symbol,
        currency="CNY",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal("0"),
        executed_at=executed_at,
        source_sequence=source_sequence,
    )


def write_separate_review_facts(
    root: Path,
    *,
    discipline_count: int,
    actual_count: int,
    complete_through: str | None = None,
) -> None:
    snapshot = trend_strategy_snapshot("CN", "test-sha", ())
    start = date(2026, 7, 16)
    fills: list[TradeFill] = []
    for index in range(40):
        trading_date = (start + timedelta(days=index)).isoformat()
        orders: list[dict[str, object]] = []
        if index < discipline_count:
            symbol = f"{600000 + index:06d}"
            orders = [
                {
                    "side": "BUY",
                    "status": "FILLED",
                    "symbol": symbol,
                    "qty": "100",
                    "notional": "1000",
                },
                {
                    "side": "SELL",
                    "status": "FILLED",
                    "symbol": symbol,
                    "qty": "100",
                    "notional": "1010",
                },
            ]
        trend_review.freeze_discipline_fact(
            root,
            "CN",
            trading_date,
            str(100000 + index * 100 - (200 if index % 2 else 0)),
            orders,
            snapshot,
        )
        trend_review.freeze_actual_equity_fact(
            root,
            "CN",
            trading_date,
            str(100000 + index * 80 - (160 if index % 2 else 0)),
            [],
            snapshot,
        )
        trend_review.freeze_benchmark_fact(
            root,
            "CN",
            trading_date,
            {
                "date": trading_date,
                "close": str(1000 + index),
                "source_id": "CSI_500_PRICE",
                "futu_symbol": "SH.000905",
            },
        )
        if index < actual_count:
            symbol = f"{700000 + index:06d}"
            fills.extend(
                [
                    actual_fill(
                        f"buy-{index}", symbol, "BUY", "100", trading_date,
                        source_sequence=index * 2,
                    ),
                    actual_fill(
                        f"sell-{index}", symbol, "SELL", "100", trading_date,
                        source_sequence=index * 2 + 1,
                    ),
                ]
            )
    trend_review.freeze_actual_fill_batch(
        root,
        {"market": "CN", "source": "test"},
        fills,
        complete_through or (start + timedelta(days=39)).isoformat(),
        coverage_start=start.isoformat(),
    )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")


def write_projection_strategy_facts(
    root: Path,
    market: str,
    snapshots: list[dict[str, object]],
) -> None:
    start = date.fromisoformat(trend_review.TREND_V1_EFFECTIVE_FROM[market])
    benchmark_source = {
        "CN": ("CSI_500_PRICE", "SH.000905"),
        "US": ("SPY_QFQ", "US.SPY"),
        "HK": ("HSI_PRICE", "HK.800000"),
    }[market]
    for index, snapshot in enumerate(snapshots):
        trading_date = (start + timedelta(days=index)).isoformat()
        trend_review.freeze_discipline_fact(
            root, market, trading_date, "100000", [], snapshot
        )
        trend_review.freeze_actual_equity_fact(
            root, market, trading_date, "100000", [], snapshot
        )
        trend_review.freeze_benchmark_fact(
            root,
            market,
            trading_date,
            {
                "date": trading_date,
                "close": "1000",
                "source_id": benchmark_source[0],
                "futu_symbol": benchmark_source[1],
            },
        )
    trend_review.freeze_actual_fill_batch(
        root,
        {
            "broker": {"CN": "eastmoney", "US": "tiger", "HK": "phillips"}[market],
            "market": market,
            "source": "test",
        },
        [],
        (start + timedelta(days=len(snapshots) - 1)).isoformat(),
        coverage_start=start.isoformat(),
    )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("market", "strategy_version"),
    [("CN", "v10"), ("US", "v7"), ("HK", "v7")],
)
def test_projection_accepts_current_live_strategy_versions(
    tmp_path: Path, market: str, strategy_version: str,
) -> None:
    snapshot = live_trend_strategy_snapshot(
        market, "test-sha", (), strategy_version=strategy_version
    )
    write_projection_strategy_facts(tmp_path, market, [snapshot, snapshot])

    projection = trend_review.build_trend_review_projection(tmp_path, market)

    assert projection["strategy_snapshot"]["strategy_version"] == strategy_version


def test_projection_prefers_v10_when_current_facts_are_mixed(
    tmp_path: Path,
) -> None:
    snapshots = [
        live_trend_strategy_snapshot(
            "CN", "test-sha", (), strategy_version=version
        )
        for version in ("v8", "v9", "v10")
    ]
    write_projection_strategy_facts(tmp_path, "CN", snapshots)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"]["strategy_version"] == "v10"


def _allocation_ref(
    market: str, *, score: str, rank: int, path: str,
) -> dict[str, object]:
    weights = {1: ("0.06", "0.60"), 2: ("0.04", "0.40"), 3: ("0.02", "0.20")}
    entry_weight, nominal_weight = weights[rank]
    return {
        "daily_path": path,
        "sha256": hashlib.sha256(path.encode()).hexdigest(),
        "snapshot": {
            "version": 1,
            "allocation_date": "2026-08-03",
            "generated_at": "2026-08-03T16:18:00+08:00",
            "markets": {
                market: {
                    "rank": rank,
                    "score": score,
                    "score_source": "A股",
                    "entry_weight": entry_weight,
                    "nominal_weight": nominal_weight,
                }
            },
        },
    }


def test_projection_tolerates_daily_allocation_identity_changes(
    tmp_path: Path,
) -> None:
    snapshots = [
        live_trend_strategy_snapshot(
            "CN", "test-sha", (),
            allocation=_allocation_ref(
                "CN", score="90", rank=1,
                path="data/trend_allocation/daily/2026-08-03.json",
            ),
        ),
        live_trend_strategy_snapshot(
            "CN", "test-sha", (),
            allocation=_allocation_ref(
                "CN", score="92.97", rank=3,
                path="data/trend_allocation/daily/2026-08-05.json",
            ),
        ),
    ]
    write_projection_strategy_facts(tmp_path, "CN", snapshots)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"]["strategy_version"] == "v13"


def test_projection_excludes_non_allocation_parameter_drift_fact(
    tmp_path: Path,
) -> None:
    base = live_trend_strategy_snapshot("CN", "test-sha", ())
    drifted = copy.deepcopy(base)
    drifted["parameters"]["position_limit"] = 9
    write_projection_strategy_facts(tmp_path, "CN", [base, drifted])

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["metric_cutoffs"]["discipline"] == "2026-07-17"


@pytest.mark.parametrize(
    ("market", "strategy_versions"),
    [
        ("CN", ("v4", "v7", "v8", "v9", "v10")),
        ("US", ("v4", "v5", "v6", "v7")),
        ("HK", ("v4", "v5", "v6", "v7")),
    ],
)
def test_projection_accepts_approved_mixed_sample_identities(
    tmp_path: Path,
    market: str,
    strategy_versions: tuple[str, ...],
) -> None:
    snapshots = [
        live_trend_strategy_snapshot(
            market, "test-sha", (), strategy_version=version
        )
        for version in strategy_versions
    ]
    write_projection_strategy_facts(tmp_path, market, snapshots)

    projection = trend_review.build_trend_review_projection(tmp_path, market)

    assert projection["strategy_snapshot"]["strategy_version"] == strategy_versions[-1]


def test_pending_sell_with_zero_live_position_records_completion(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}],
        fail_orders=1,
    )
    arguments = {
        "data_dir": tmp_path,
        "market": "CN",
        "symbol": "600001",
        "trading_date": "2026-07-17",
        "event_id": "event-1",
        "client": client,
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_stop(
            **arguments, now="2026-07-17T10:15:00+08:00"
        )
    client.positions = []

    result = trend_review.execute_trend_review_stop(
        **arguments, now="2026-07-17T10:16:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1
    assert any(
        event.get("status") == "incomplete"
        and event.get("reason") == "position_zero_confirmed"
        for event in events
    )


def test_formal_and_protection_sells_merge_into_one_action(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    report = report_with_actions(
        [
            {
                "action": "SELL_ALL",
                "symbol": "600001",
                "event_id": "formal-danger-1",
                "reason": "danger_signal",
            }
        ]
    )

    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:15:00+08:00",
        quote_prices={},
    )
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=client,
        now="2026-07-17T10:16:00+08:00",
    )

    action_roots = list(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*")
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in action_roots[0].glob("*.json")
    ]
    assert len(action_roots) == 1
    assert {event.get("reason_id") for event in events} >= {
        "formal-danger-1",
        "protection-1",
    }
    assert len(client.requests) == 1
    assert client.requests[0]["remark"] == trend_review.trend_attempt_remark(
        "CN",
        "2026-07-17",
        trend_review.trend_action_key(
            "CN", "2026-07-17", "SH.600001", "sell"
        ),
        1,
    )


def test_protection_sell_then_formal_report_remains_one_auditable_action(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=client,
        now="2026-07-17T10:15:00+08:00",
    )
    client.orders[0].update(
        {
            "dealt_qty": "300",
            "dealt_avg_price": "10",
            "order_status": "FILLED_ALL",
        }
    )
    client.positions = []
    report = report_with_actions(
        [
            {
                "action": "SELL_ALL",
                "symbol": "600001",
                "event_id": "formal-danger-1",
                "reason": "danger_signal",
            }
        ]
    )
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T10:16:00+08:00",
    )

    formal = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:16:00+08:00",
        quote_prices={},
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
    )

    assert formal["submitted_count"] == 0
    assert len(client.requests) == 1
    assert any(event.get("status") == "filled" for event in events)
    assert {event.get("reason_id") for event in events} >= {
        "protection-1",
        "formal-danger-1",
    }
    event_paths = list(
        tmp_path.glob("trend_review/ledgers/CN/actions/2026-07-17/*/*.json")
    )
    protection_reason_path = next(
        path
        for path in event_paths
        if (
            (payload := json.loads(path.read_text(encoding="utf-8"))).get(
                "status"
            )
            == "reason_added"
            and payload.get("strategy_version") == "protection-v1"
        )
    )
    tampered = json.loads(protection_reason_path.read_text(encoding="utf-8"))
    tampered["reason_id"] = "tampered-protection"
    protection_reason_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend action"):
        trend_review.load_trend_action_audit(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            symbol="600001",
            side="sell",
        )


def test_formal_batch_does_not_ignore_tampered_protection_fact(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    trend_review.execute_trend_review_stop(
        data_dir=tmp_path,
        market="CN",
        symbol="600001",
        trading_date="2026-07-17",
        event_id="protection-1",
        client=client,
        now="2026-07-17T10:15:00+08:00",
    )
    intent_path = next(
        tmp_path.glob("trend_review/ledgers/CN/open/2026-07-17/*-intent.json")
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["action_index"] = 1
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    report = report_with_actions(
        [{"action": "SELL_ALL", "symbol": "600001", "reason": "danger_signal"}]
    )
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="no matching report artifact"):
        trend_review.lock_trend_execution_batch(
            tmp_path,
            market="CN",
            execution_date="2026-07-17",
            report_path=report_path,
            report=report,
            locked_at="2026-07-17T10:16:00+08:00",
        )


def test_formal_report_recovers_accepted_protection_intent_with_original_identity(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}],
        fail_orders=1,
        accepted_before_failure=True,
    )
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_stop(
            data_dir=tmp_path,
            market="CN",
            symbol="600001",
            trading_date="2026-07-17",
            event_id="protection-1",
            client=client,
            now="2026-07-17T10:15:00+08:00",
        )
    report = report_with_actions(
        [{"action": "SELL_ALL", "symbol": "600001", "reason": "danger_signal"}]
    )
    report_path = tmp_path / "reports/2026-07-17.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    trend_review.lock_trend_execution_batch(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        report_path=report_path,
        report=report,
        locked_at="2026-07-17T10:16:00+08:00",
    )

    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:16:00+08:00",
        quote_prices={},
    )
    intent_path = next(
        tmp_path.glob("trend_review/ledgers/CN/open/2026-07-17/*-intent.json")
    )
    result_path = intent_path.with_name(intent_path.name.replace("-intent", "-result"))
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert len(client.requests) == 1
    assert result["report_sha256"] == intent["report_sha256"]
    assert result["action_index"] == intent["action_index"]

    client.orders[0].update(
        {
            "dealt_qty": "300",
            "dealt_avg_price": "10",
            "order_status": "FILLED_ALL",
        }
    )
    client.positions = []
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T10:17:00+08:00",
        quote_prices={},
    )
    events, _ = trend_review.load_trend_action_audit(
        tmp_path,
        market="CN",
        execution_date="2026-07-17",
        symbol="600001",
        side="sell",
    )

    assert len(client.requests) == 1
    assert any(event.get("status") == "filled" for event in events)


def test_merged_sell_retry_uses_live_remaining_position(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    arguments = {
        "data_dir": tmp_path,
        "market": "CN",
        "symbol": "600001",
        "trading_date": "2026-07-17",
        "event_id": "protection-1",
        "client": client,
    }
    trend_review.execute_trend_review_stop(
        **arguments, now="2026-07-17T10:15:00+08:00"
    )
    client.orders = [
        {
            "order_id": "SIM-1",
            "remark": client.requests[0]["remark"],
            "code": "SH.600001",
            "trd_side": "SELL",
            "qty": "300",
            "dealt_qty": "200",
            "dealt_avg_price": "10",
            "order_status": "CANCELLED_PART",
        }
    ]
    client.positions = [{"code": "SH.600001", "qty": "100"}]

    retried = trend_review.execute_trend_review_stop(
        **arguments, now="2026-07-17T10:16:00+08:00"
    )

    assert retried["submitted_count"] == 1
    assert client.requests[-1]["qty"] == "100"
    assert client.requests[-1]["remark"] == trend_review.trend_attempt_remark(
        "CN",
        "2026-07-17",
        trend_review.trend_action_key(
            "CN", "2026-07-17", "SH.600001", "sell"
        ),
        2,
    )


@pytest.mark.parametrize("broker_fact", ["active", "absent", "ambiguous"])
def test_sell_recovery_never_overlaps_inconclusive_broker_state(
    tmp_path: Path, broker_fact: str
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "300"}]
    )
    arguments = {
        "data_dir": tmp_path,
        "market": "CN",
        "symbol": "600001",
        "trading_date": "2026-07-17",
        "event_id": "protection-1",
        "client": client,
    }
    trend_review.execute_trend_review_stop(
        **arguments, now="2026-07-17T10:15:00+08:00"
    )
    order = {
        "order_id": "SIM-1",
        "remark": client.requests[0]["remark"],
        "code": "SH.600001",
        "trd_side": "SELL",
        "qty": "300",
        "dealt_qty": "0",
        "order_status": "SUBMITTED" if broker_fact == "active" else "UNKNOWN",
    }
    client.orders = (
        []
        if broker_fact == "absent"
        else [order, {**order, "order_id": "SIM-2"}]
        if broker_fact == "ambiguous"
        else [order]
    )

    result = trend_review.execute_trend_review_stop(
        **arguments, now="2026-07-17T10:16:00+08:00"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.glob(
            "trend_review/ledgers/CN/actions/2026-07-17/*/*.json"
        )
    ]

    assert result["submitted_count"] == 0
    assert len(client.requests) == 1
    if broker_fact == "active":
        assert result["status"] == "submitted"
    else:
        assert any(event.get("status") == "uncertain" for event in events)


def write_review_history(
    root: Path,
    *,
    discipline_count: int,
    actual_count: int,
    complete_through: str | None = None,
) -> None:
    snapshot = strategy_snapshot()
    start = date(2026, 7, 16)
    fills: list[TradeFill] = []
    for index in range(40):
        trading_date = (start + timedelta(days=index)).isoformat()
        orders: list[dict[str, object]] = []
        if index < discipline_count:
            symbol = f"{600000 + index:06d}"
            orders = [
                {
                    "side": "BUY",
                    "status": "FILLED",
                    "symbol": symbol,
                    "qty": "100",
                    "notional": "1000",
                },
                {
                    "side": "SELL",
                    "status": "FILLED",
                    "symbol": symbol,
                    "qty": "100",
                    "notional": "1010",
                },
            ]
        trend_review.freeze_discipline_fact(
            root,
            "CN",
            trading_date,
            str(100000 + index * 100 - (200 if index % 2 else 0)),
            orders,
            snapshot,
        )
        trend_review.freeze_actual_equity_fact(
            root,
            "CN",
            trading_date,
            str(100000 + index * 80 - (160 if index % 2 else 0)),
            [],
            snapshot,
        )
        trend_review.freeze_benchmark_fact(
            root,
            "CN",
            trading_date,
            {
                "date": trading_date,
                "close": str(1000 + index),
                "source_id": "CSI_500_PRICE",
                "futu_symbol": "SH.000905",
            },
        )
        if index < actual_count:
            symbol = f"{700000 + index:06d}"
            fills.extend(
                [
                    actual_fill(
                        f"buy-{index}", symbol, "BUY", "100", trading_date,
                        source_sequence=index * 2,
                    ),
                    actual_fill(
                        f"sell-{index}", symbol, "SELL", "100", trading_date,
                        source_sequence=index * 2 + 1,
                    ),
                ]
            )
    trend_review.freeze_actual_fill_batch(
        root,
        {"market": "CN", "source": "test"},
        fills,
        complete_through or (start + timedelta(days=39)).isoformat(),
        coverage_start=start.isoformat(),
    )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")


def test_actual_fill_identity_includes_account_alias(tmp_path: Path) -> None:
    paths = trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"market": "CN", "source": "test"},
        [
            actual_fill(
                "shared-id", "600001", "BUY", "100", "2026-07-16",
                account_alias="account-a",
            ),
            actual_fill(
                "shared-id", "600001", "BUY", "100", "2026-07-16",
                account_alias="account-b",
            ),
        ],
        "2026-07-16",
    )

    assert len(set(paths)) == 2
    assert all(path.exists() for path in paths)


def test_actual_fill_identity_is_idempotent_only_for_identical_payload(
    tmp_path: Path,
) -> None:
    fill = actual_fill("fill-1", "600001", "BUY", "100", "2026-07-16")
    first = trend_review.freeze_actual_fill_batch(
        tmp_path, {"market": "CN"}, [fill], "2026-07-16"
    )
    second = trend_review.freeze_actual_fill_batch(
        tmp_path, {"market": "CN"}, [fill], "2026-07-16"
    )

    assert second == first
    with pytest.raises(FileExistsError, match="immutable artifact collision"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"market": "CN"},
            [
                actual_fill(
                    "fill-1", "600001", "BUY", "100", "2026-07-16",
                    price="11",
                )
            ],
            "2026-07-16",
        )


@pytest.mark.parametrize("legacy_sequence", [99, None])
def test_actual_fill_reimport_preserves_legacy_embedded_sequence(
    legacy_sequence: int | None,
    tmp_path: Path,
) -> None:
    fill = actual_fill(
        "fill-1",
        "600001",
        "BUY",
        "100",
        "2026-07-16",
        source_sequence=7,
    )
    [path] = trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [fill],
        "2026-07-16",
        coverage_start="2026-07-01",
    )
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["source_sequence"] = legacy_sequence
    path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    original = path.read_bytes()

    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [fill],
        "2026-07-16",
        coverage_start="2026-06-16",
    )

    assert path.read_bytes() == original
    _, coverage = trend_review._load_actual_fills(tmp_path, "CN")
    assert ("2026-06-16", "2026-07-16") in coverage


def test_singleton_fill_ignores_obsolete_sequence_conflicts(tmp_path: Path) -> None:
    first = actual_fill(
        "fill-1",
        "600001",
        "BUY",
        "100",
        "2026-07-16",
        source_sequence=4,
    )
    second = replace(first, source_sequence=0)
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [first],
        "2026-07-16",
        coverage_start="2026-07-01",
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [second],
        "2026-07-16",
        coverage_start="2026-06-16",
    )

    fills, _ = trend_review._load_actual_fills(tmp_path, "CN")

    assert len(fills) == 1


def test_same_day_fill_order_conflict_still_fails_closed(tmp_path: Path) -> None:
    buy = actual_fill(
        "buy",
        "600001",
        "BUY",
        "100",
        "2026-07-16",
        source_sequence=0,
    )
    sell = actual_fill(
        "sell",
        "600001",
        "SELL",
        "100",
        "2026-07-16",
        source_sequence=1,
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [buy, sell],
        "2026-07-16",
        coverage_start="2026-07-01",
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [replace(buy, source_sequence=1), replace(sell, source_sequence=0)],
        "2026-07-16",
        coverage_start="2026-06-16",
    )

    with pytest.raises(ValueError, match="conflicting actual fill source order"):
        trend_review._load_actual_fills(tmp_path, "CN")


def test_actual_fill_batch_preflights_all_collisions_before_writing(
    tmp_path: Path,
) -> None:
    existing = actual_fill("existing", "600001", "BUY", "100", "2026-07-16")
    trend_review.freeze_actual_fill_batch(
        tmp_path, {"market": "CN"}, [existing], "2026-07-16"
    )
    before = {
        path: path.read_bytes()
        for path in (tmp_path / "trend_review/facts").rglob("*.json")
    }

    with pytest.raises(FileExistsError, match="immutable artifact collision"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"market": "CN"},
            [
                actual_fill("new", "600002", "BUY", "100", "2026-07-16"),
                actual_fill(
                    "existing",
                    "600001",
                    "BUY",
                    "100",
                    "2026-07-16",
                    price="11",
                ),
            ],
            "2026-07-16",
        )

    assert {
        path: path.read_bytes()
        for path in (tmp_path / "trend_review/facts").rglob("*.json")
    } == before


def test_actual_fill_completeness_freezes_explicit_coverage_interval(
    tmp_path: Path,
) -> None:
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [
            actual_fill(
                "fill", "600001", "BUY", "100", "2026-07-16",
                source_sequence=4,
            )
        ],
        "2026-07-17",
        coverage_start="2026-07-01",
    )

    path = next(
        (tmp_path / "trend_review/facts/actual_fill_completeness/CN").glob("*.json")
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["coverage_start"] == "2026-07-01"
    assert payload["coverage_end"] == "2026-07-17"
    fill_path = next(
        (tmp_path / "trend_review/facts/actual_fills/CN").glob("*.json")
    )
    assert "source_sequence" not in json.loads(fill_path.read_text(encoding="utf-8"))
    loaded, _ = trend_review._load_actual_fills(tmp_path, "CN")
    assert loaded[0]["source_sequence"] == 4


@pytest.mark.parametrize("source_sequence", [3, None])
def test_reimport_keeps_legacy_fill_body_and_recovers_batch_order(
    source_sequence: int | None, tmp_path: Path,
) -> None:
    fill = actual_fill(
        "legacy-fill", "600001", "BUY", "100", "2026-07-16",
        source_sequence=source_sequence,
    )
    payload = {
        "schema_version": "open_trader.trend_review.fill.v1",
        **asdict(fill),
    }
    payload.pop("source_sequence")
    identity = trend_review._actual_fill_identity(payload)
    digest = trend_review.hashlib.sha256(
        trend_review._canonical_json_bytes({"identity": identity})
    ).hexdigest()
    path = (
        tmp_path / "trend_review/facts/actual_fills/CN" / f"{digest}.json"
    )
    path.parent.mkdir(parents=True)
    original = trend_review._canonical_json_bytes(payload)
    path.write_bytes(original)

    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"market": "CN"},
        [fill],
        "2026-07-16",
        coverage_start="2026-07-16",
    )

    assert path.read_bytes() == original
    loaded, _ = trend_review._load_actual_fills(tmp_path, "CN")
    assert loaded[0].get("source_sequence") == source_sequence


def test_tiger_reimport_keeps_legacy_fill_body_byte_for_byte(
    tmp_path: Path,
) -> None:
    fill = TradeFill(
        source_id="tiger-fill",
        source_order_id="order-1",
        broker="tiger",
        account_alias="tiger_main",
        market=Market.US,
        symbol="AAPL",
        currency="USD",
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("200"),
        fees=None,
        executed_at="2026-07-16T09:30:00-04:00",
        source_sequence=None,
    )
    payload = {
        "schema_version": "open_trader.trend_review.fill.v1",
        **asdict(fill),
    }
    payload.pop("source_sequence")
    identity = trend_review._actual_fill_identity(payload)
    digest = trend_review.hashlib.sha256(
        trend_review._canonical_json_bytes({"identity": identity})
    ).hexdigest()
    path = tmp_path / "trend_review/facts/actual_fills/US" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    original = trend_review._canonical_json_bytes(payload)
    path.write_bytes(original)

    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "tiger"},
        [fill],
        "2026-07-16",
        coverage_start="2026-07-16",
    )

    assert path.read_bytes() == original
    loaded, _ = trend_review._load_actual_fills(tmp_path, "US")
    assert len(loaded) == 1
    assert trend_review._canonical_json_bytes(loaded[0]) == original


def test_actual_fill_batch_rejects_non_iso_execution_timestamp(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="ISO date or timestamp"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"market": "CN"},
            [actual_fill("fill", "600001", "BUY", "100", "2026-07-16 garbage")],
            "2026-07-16",
        )


@pytest.mark.parametrize("coverage_start", ["not-a-date", "2026-07-17"])
def test_actual_fill_batch_rejects_invalid_coverage_start(
    coverage_start: str, tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="coverage_start"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"market": "CN"},
            [],
            "2026-07-16",
            coverage_start=coverage_start,
        )


def test_actual_fill_batch_write_failure_removes_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fdopen = trend_review.os.fdopen
    calls = 0

    def fail_second_write(descriptor: int, mode: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            trend_review.os.close(descriptor)
            raise OSError("simulated immutable write failure")
        return real_fdopen(descriptor, mode)

    monkeypatch.setattr(trend_review.os, "fdopen", fail_second_write)

    with pytest.raises(OSError, match="simulated immutable write failure"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"market": "CN"},
            [actual_fill("new", "600002", "BUY", "100", "2026-07-16")],
            "2026-07-16",
        )

    assert list((tmp_path / "trend_review/facts").rglob("*.json")) == []


@pytest.mark.parametrize(
    ("broker", "market", "currency"),
    [
        ("eastmoney", Market.HK, "HKD"),
        ("phillips", Market.CN, "CNY"),
    ],
)
def test_actual_fill_batch_rejects_market_outside_broker_authority(
    broker: str,
    market: Market,
    currency: str,
    tmp_path: Path,
) -> None:
    fill = TradeFill(
        source_id="wrong-market",
        source_order_id=None,
        broker=broker,
        account_alias=f"{broker}_main",
        market=market,
        symbol="00700" if market is Market.HK else "600001",
        currency=currency,
        side="BUY",
        quantity=Decimal("100"),
        price=Decimal("10"),
        fees=Decimal("0"),
        executed_at="2026-07-16",
    )

    with pytest.raises(ValueError, match="market does not match source metadata"):
        trend_review.freeze_actual_fill_batch(
            tmp_path,
            {"broker": broker},
            [fill],
            "2026-07-16",
        )

    assert not (tmp_path / "trend_review/facts").exists()


def write_review_history(
    root: Path,
    *,
    completed_trades: int,
    days: int,
    with_actual_fill_coverage: bool = True,
) -> None:
    daily = root / "trend_review/daily/CN"
    daily.mkdir(parents=True)
    start = date(2026, 7, 16)
    snapshot = strategy_snapshot()
    for index in range(days):
        trading_date = (start + timedelta(days=index)).isoformat()
        orders: list[dict[str, object]] = []
        if index < completed_trades:
            symbol = f"{600000 + index:06d}"
            orders = [
                {"side": "BUY", "status": "FILLED", "symbol": symbol, "qty": "100"},
                {"side": "SELL", "status": "FILLED", "symbol": symbol, "qty": "100"},
            ]
        payload = {
            "schema_version": "open_trader.trend_review.daily.v1",
            "market": "CN",
            "date": trading_date,
            "discipline_equity_after_fees": str(100000 + index * 100),
            "actual_equity": str(100000 + index * 80),
            "strategy_snapshot": snapshot,
            "report_sha256": f"report-{index}",
            "orders": orders,
            "benchmark": {
                "date": trading_date,
                "close": str(1000 + index),
                "source_id": "CSI_500_PRICE",
                "futu_symbol": "SH.000905",
            },
        }
        (daily / f"{trading_date}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")
    if with_actual_fill_coverage:
        trend_review.freeze_actual_fill_batch(
            root,
            {"broker": "eastmoney"},
            [],
            (start + timedelta(days=days - 1)).isoformat(),
            coverage_start=start.isoformat(),
        )


@pytest.mark.parametrize(("discipline_count", "actual_count"), [(29, 30), (30, 29), (31, 31)])
def test_projection_metrics_do_not_depend_on_legacy_round_counts(
    tmp_path: Path,
    discipline_count: int,
    actual_count: int,
) -> None:
    write_separate_review_facts(
        tmp_path,
        discipline_count=discipline_count,
        actual_count=actual_count,
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": None,
        "actual": None,
        "required": 30,
    }
    assert projection["sample_details"] == {
        "discipline": None,
        "actual": None,
    }
    assert projection["sample_cutoffs"] == {
        "discipline": None,
        "actual": None,
    }
    assert projection["metrics"]["calmar"]["discipline"]["value"] is not None
    assert projection["metrics"]["calmar"]["actual"] == {
        "value": None,
        "reason": "实际执行日终净值缺失",
    }
    assert "batch" not in projection
    assert "batch_path" not in projection
    assert not (tmp_path / "trend_review/batches").exists()
    assert set(projection["metrics"]) == {
        "period_net_return",
        "market_excess_return",
        "max_drawdown",
        "calmar",
        "sharpe",
    }
    assert all(
        set(values) == {
            "discipline",
            "actual",
            "discipline_benchmark",
            "actual_benchmark",
        }
        for values in projection["metrics"].values()
    )
    assert projection["metrics"]["market_excess_return"]["discipline_benchmark"] == {
        "value": "0",
        "reason": None,
    }
    assert Decimal(
        projection["metrics"]["market_excess_return"]["discipline"]["value"]
    ) == Decimal(
        projection["metrics"]["period_net_return"]["discipline"]["value"]
    ) - Decimal(
        projection["metrics"]["period_net_return"]["discipline_benchmark"]["value"]
    )


def test_actual_cycles_close_partials_opening_positions_rebuys_and_dedupe() -> None:
    fills = [
        asdict(actual_fill("partial-1", "600001", "SELL", "40", "2026-07-16")),
        asdict(actual_fill("partial-1", "600001", "SELL", "40", "2026-07-16")),
        asdict(actual_fill("partial-2", "600001", "SELL", "60", "2026-07-17")),
        asdict(actual_fill("rebuy", "600001", "BUY", "10", "2026-07-18")),
        asdict(actual_fill("reclose", "600001", "SELL", "10", "2026-07-19")),
    ]

    cycles = trend_review._completed_cycles(
        fills,
        [{"symbol": "600001", "opened_at": "2026-07-01", "quantity": "100"}],
    )

    assert [(cycle["entry_date"], cycle["exit_date"]) for cycle in cycles] == [
        ("2026-07-01", "2026-07-17"),
        ("2026-07-18", "2026-07-19"),
    ]
    assert [len(cycle["fills"]) for cycle in cycles] == [2, 2]


def test_completed_cycles_ignores_identical_duplicate_payloads() -> None:
    buy = asdict(actual_fill("buy-1", "600001", "BUY", "100", "2026-07-16"))
    sell = asdict(actual_fill("sell-1", "600001", "SELL", "100", "2026-07-17"))

    cycles = trend_review._completed_cycles([buy, dict(buy), sell])

    assert len(cycles) == 1
    assert len(cycles[0]["fills"]) == 2


def test_completed_cycles_uses_authoritative_source_sequence_for_date_only_fills() -> None:
    buy = asdict(
        actual_fill("z-buy", "600001", "BUY", "100", "2026-07-16", source_sequence=0)
    )
    sell = asdict(
        actual_fill("a-sell", "600001", "SELL", "100", "2026-07-16", source_sequence=1)
    )

    cycles = trend_review._completed_cycles([sell, buy])

    assert len(cycles) == 1
    assert [fill["side"] for fill in cycles[0]["fills"]] == ["BUY", "SELL"]


def test_completed_cycles_use_precise_timestamps_without_source_sequence() -> None:
    fills = [
        asdict(
            actual_fill(
                "sell", "600001", "SELL", "100",
                "2026-07-16T10:01:00+00:00",
            )
        ),
        asdict(
            actual_fill(
                "buy", "600001", "BUY", "100",
                "2026-07-16T10:00:00+00:00",
            )
        ),
    ]

    cycles = trend_review._completed_cycles(fills)

    assert len(cycles) == 1


def test_completed_cycles_sort_precise_timestamps_by_instant_not_offset_text() -> None:
    fills = [
        asdict(
            actual_fill(
                "sell", "600001", "SELL", "100",
                "2026-07-16T08:00:00+00:00",
            )
        ),
        asdict(
            actual_fill(
                "buy", "600001", "BUY", "100",
                "2026-07-16T09:00:00+02:00",
            )
        ),
    ]

    assert len(trend_review._completed_cycles(fills)) == 1


@pytest.mark.parametrize("sequences", [(None, None), (0, 0)])
def test_completed_cycles_rejects_ambiguous_date_only_fill_order(
    sequences: tuple[int | None, int | None],
) -> None:
    fills = [
        asdict(actual_fill("buy", "600001", "BUY", "100", "2026-07-16", source_sequence=sequences[0])),
        asdict(actual_fill("sell", "600001", "SELL", "100", "2026-07-16", source_sequence=sequences[1])),
    ]

    with pytest.raises(ValueError, match="ambiguous actual fill order"):
        trend_review._completed_cycles(fills)


def test_legacy_completeness_without_fill_order_fails_closed_for_same_day_fills(
    tmp_path: Path,
) -> None:
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [
            actual_fill(
                "buy", "600001", "BUY", "100", "2026-07-16",
                source_sequence=1,
            ),
            actual_fill(
                "sell", "600001", "SELL", "100", "2026-07-16",
                source_sequence=2,
            ),
        ],
        "2026-07-16",
        coverage_start="2026-07-16",
    )
    completeness_path = next(
        (
            tmp_path
            / "trend_review/facts/actual_fill_completeness/CN"
        ).glob("*.json")
    )
    legacy = json.loads(completeness_path.read_text(encoding="utf-8"))
    legacy.pop("fill_order")
    completeness_path.write_text(json.dumps(legacy), encoding="utf-8")

    fills, _ = trend_review._load_actual_fills(tmp_path, "CN")

    with pytest.raises(ValueError, match="ambiguous actual fill order"):
        trend_review._completed_cycles(fills)


@pytest.mark.parametrize("reverse", [False, True])
def test_completed_cycles_rejects_conflicting_payloads_for_one_identity(
    reverse: bool,
) -> None:
    buy = asdict(actual_fill("fill-1", "600001", "BUY", "100", "2026-07-16"))
    sell = asdict(actual_fill("fill-1", "600001", "SELL", "100", "2026-07-16"))

    with pytest.raises(ValueError, match="conflicting actual fill identity"):
        trend_review._completed_cycles([sell, buy] if reverse else [buy, sell])


def test_legacy_fill_completeness_does_not_fabricate_statistics_availability(
    tmp_path: Path,
) -> None:
    write_review_history(
        tmp_path,
        completed_trades=30,
        days=40,
        with_actual_fill_coverage=False,
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [],
        "2026-08-24",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": None,
        "actual": None,
        "required": 30,
    }


def test_missing_actual_fill_source_stops_common_cutoff(tmp_path: Path) -> None:
    write_review_history(
        tmp_path,
        completed_trades=30,
        days=40,
        with_actual_fill_coverage=False,
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None


def test_projection_without_cutoff_keeps_latest_complete_discipline_snapshot(
    tmp_path: Path,
) -> None:
    write_review_history(
        tmp_path,
        completed_trades=0,
        days=2,
        with_actual_fill_coverage=False,
    )
    latest_path = tmp_path / "trend_review/daily/CN/2026-07-17.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest["strategy_snapshot"] = {
        **latest["strategy_snapshot"],
        "process_version": "latest-sha",
    }
    latest_path.write_text(json.dumps(latest), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None
    assert projection["strategy_snapshot"]["strategy_id"] == (
        "trend_animals_warm_to_hot/CN/v3"
    )
    assert projection["strategy_snapshot"]["process_version"] == "latest-sha"
    assert projection["strategy_snapshot"] == trend_strategy_snapshot(
        "CN", "latest-sha", ()
    )
    assert projection["strategy_snapshot"]["parameter_rows"]


def test_us_projection_belongs_to_tiger_trend_account(tmp_path: Path) -> None:
    daily = tmp_path / "trend_review/daily/US"
    daily.mkdir(parents=True)
    daily.joinpath("2026-07-16.json").write_text(json.dumps({
        "schema_version": "open_trader.trend_review.daily.v1",
        "market": "US",
        "date": "2026-07-16",
        "discipline_equity_after_fees": "100000",
        "actual_equity": "100000",
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/US/v1",
            "strategy_name": "美股短线右侧趋势",
            "strategy_version": "v1",
            "market": "US",
            "effective_from": "2026-07-17",
            "process_version": "test-sha",
            "parameter_rows": [
                {"group": "rules", "name": "limit", "value": "10"}
            ],
            "parameters": {},
        },
        "report_sha256": "report-us",
        "orders": [],
        "benchmark": {
            "date": "2026-07-16",
            "close": "100",
            "source_id": "SPY_QFQ",
            "futu_symbol": "US.SPY",
        },
    }), encoding="utf-8")
    rates = tmp_path / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["broker"] == "tiger"


def test_projection_rejects_wrong_benchmark_identity(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)
    path = sorted((tmp_path / "trend_review/daily/CN").glob("*.json"))[0]
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["benchmark"]["source_id"] = "WRONG"
    path.write_text(json.dumps(fact), encoding="utf-8")

    with pytest.raises(ValueError, match="benchmark source_id"):
        trend_review.build_trend_review_projection(tmp_path, "CN")


def test_projection_ignores_exit_for_position_held_before_tracking(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)
    first_path = sorted((tmp_path / "trend_review/daily/CN").glob("*.json"))[0]
    first = json.loads(first_path.read_text(encoding="utf-8"))
    first["orders"].insert(  # type: ignore[union-attr]
        0,
        {
            "side": "SELL",
            "status": "FILLED",
            "symbol": "PREEXISTING",
            "qty": "1900",
        },
    )
    first_path.write_text(json.dumps(first), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["discipline"] is None
    assert "batch_path" not in projection


def test_projection_count_does_not_use_legacy_partial_exit(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=29, days=40)
    daily = tmp_path / "trend_review/daily/CN"
    entry_path = sorted(daily.glob("*.json"))[29]
    exit_path = sorted(daily.glob("*.json"))[30]
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    entry["orders"] = [
        {"side": "BUY", "status": "FILLED", "symbol": "700000", "qty": "100"},
        {"side": "SELL", "status": "FILLED", "symbol": "700000", "qty": "40"},
    ]
    entry_path.write_text(json.dumps(entry), encoding="utf-8")
    exit_fact = json.loads(exit_path.read_text(encoding="utf-8"))
    exit_fact["strategy_snapshot"] = {
        **exit_fact["strategy_snapshot"],
        "strategy_version": "v2",
    }
    exit_fact["orders"] = [
        {"side": "SELL", "status": "FILLED", "symbol": "700000", "qty": "60"}
    ]
    exit_path.write_text(json.dumps(exit_fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["discipline"] is None
    assert projection["metric_cutoffs"]["discipline"] == "2026-08-24"
    assert "batch_path" not in projection


def test_cn_actual_metric_remains_unavailable_when_equity_has_a_gap(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    missing = (
        tmp_path
        / "trend_review/facts/actual_equity/CN/2026-07-18.json"
    )
    missing.unlink()

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None
    assert projection["interval"] == {
        "start": "2026-07-16",
        "end": None,
    }
    assert projection["metric_cutoffs"]["actual"] is None


def test_cn_statement_fill_coverage_does_not_create_actual_metric_cutoff(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    completeness_root = (
        tmp_path / "trend_review/facts/actual_fill_completeness/CN"
    )
    for path in completeness_root.glob("*.json"):
        path.unlink()
    trend_review.freeze_actual_fill_batch(
        tmp_path, {"market": "CN"}, [], "2026-07-16", coverage_start="2026-07-16"
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path, {"market": "CN"}, [], "2026-07-18", coverage_start="2026-07-18"
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None
    assert projection["metric_cutoffs"]["actual"] is None


def test_legacy_complete_through_without_start_only_covers_that_date(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    completeness_root = (
        tmp_path / "trend_review/facts/actual_fill_completeness/CN"
    )
    for path in completeness_root.glob("*.json"):
        path.unlink()
    payload = {
        "schema_version": "open_trader.trend_review.fill_completeness.v1",
        "market": "CN",
        "complete_through": "2026-07-17",
        "source_metadata": {},
        "fill_identities": [],
    }
    completeness_root.joinpath("legacy.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None


def test_common_cutoff_uses_available_benchmark_calendar() -> None:
    assert trend_review._common_cutoff(
        "2026-07-16",
        {"2026-07-16", "2026-07-17", "2026-07-20"},
        {"2026-07-16", "2026-07-17"},
        {"2026-07-16", "2026-07-17", "2026-07-20"},
    ) == "2026-07-17"


def test_common_cutoff_requires_benchmark_on_effective_date() -> None:
    assert trend_review._common_cutoff(
        "2026-07-16",
        {"2026-07-16", "2026-07-17"},
        {"2026-07-16", "2026-07-17"},
        {"2026-07-17"},
    ) is None


def test_portfolio_metrics_use_return_drawdown_calmar_and_sharpe_formulas() -> None:
    metrics = trend_review._portfolio_metrics(
        [
            {"date": "2025-01-01", "equity": "100"},
            {"date": "2025-07-02", "equity": "120"},
            {"date": "2026-01-01", "equity": "110"},
        ],
        {date(2025, 1, 1): Decimal("0")},
        Decimal("100"),
    )

    assert Decimal(metrics["total_return_pct"]) == Decimal("10")
    assert Decimal(metrics["max_drawdown_pct"]) == Decimal(100) / Decimal(12)
    assert Decimal(metrics["calmar_ratio"]) == Decimal("1.2")
    expected_sharpe = (
        (Decimal("0.2") - Decimal(1) / Decimal(12)) / Decimal(2)
    ) / (
        (
            (
                Decimal("0.2")
                - (Decimal("0.2") - Decimal(1) / Decimal(12)) / Decimal(2)
            ) ** 2
            + (
                -Decimal(1) / Decimal(12)
                - (Decimal("0.2") - Decimal(1) / Decimal(12)) / Decimal(2)
            ) ** 2
        )
        / Decimal(2)
    ).sqrt() * Decimal(252).sqrt()
    assert abs(Decimal(metrics["sharpe_ratio"]) - expected_sharpe) < Decimal("1e-24")


def test_metric_boundaries_return_none_for_zero_risk_or_too_few_returns() -> None:
    rates = {date(2026, 1, 1): Decimal("0")}
    one_point = trend_review._portfolio_metrics(
        [{"date": "2026-01-01", "equity": "100"}], rates, Decimal("100")
    )
    flat = trend_review._portfolio_metrics(
        [
            {"date": "2026-01-01", "equity": "100"},
            {"date": "2026-01-02", "equity": "100"},
            {"date": "2026-01-03", "equity": "100"},
        ],
        rates,
        Decimal("100"),
    )

    assert one_point["sharpe_ratio"] is None
    assert one_point["calmar_ratio"] is None
    assert flat["sharpe_ratio"] is None
    assert flat["calmar_ratio"] is None


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_metric_formulas_reject_non_finite_values(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        trend_review._annualized_sharpe([Decimal("0.1"), value])
    with pytest.raises(ValueError, match="finite"):
        trend_review._portfolio_metrics(
            [
                {"date": "2026-01-01", "equity": "100"},
                {"date": "2026-01-02", "equity": str(value)},
            ],
            {date(2026, 1, 1): Decimal("0")},
            Decimal("100"),
        )


def test_review_keeps_simulation_count_when_actual_equity_is_missing(
    tmp_path: Path,
) -> None:
    write_review_history(
        tmp_path,
        completed_trades=0,
        days=40,
        with_actual_fill_coverage=False,
    )
    for path in (tmp_path / "trend_review/daily/CN").glob("*.json"):
        fact = json.loads(path.read_text(encoding="utf-8"))
        fact.pop("actual_equity", None)
        path.write_text(json.dumps(fact), encoding="utf-8")
    fills: list[dict[str, object]] = []
    for index in range(4):
        symbol = f"SIM{index}"
        common = {
            "source": "simulation",
            "source_id": "simulation:futu:101",
            "broker": "futu",
            "account_id": "101",
            "market": "CN",
            "symbol": symbol,
            "currency": "CNY",
            "strategy_id": "trend_animals_warm_to_hot/CN/v3",
            "strategy_version": "v3",
            "attribution_status": "attributed",
            "exclusion_reason": "",
            "costs_complete": False,
            "normal_cost_rate": "0.001",
            "normal_cost_model": "estimated complete round-trip normal costs",
            "report_sha256": "a" * 64,
        }
        fills.extend([
            {
                **common,
                "fill_id": f"{symbol}-buy",
                "order_id": f"{symbol}-buy",
                "side": "buy",
                "quantity": "1",
                "price": "10",
                "fee": "0",
                "filled_at": f"2026-07-{16 + index * 2:02d}T10:00:00+08:00",
            },
            {
                **common,
                "fill_id": f"{symbol}-sell",
                "order_id": f"{symbol}-sell",
                "side": "sell",
                "quantity": "1",
                "price": "11",
                "fee": "0",
                "filled_at": f"2026-07-{17 + index * 2:02d}T10:00:00+08:00",
            },
        ])
    actual_common = {
        "source": "actual",
        "source_id": "actual:eastmoney:eastmoney_main",
        "broker": "eastmoney",
        "account_id": "eastmoney_main",
        "market": "CN",
        "symbol": "ACTUAL",
        "currency": "CNY",
        "strategy_id": "trend_animals_warm_to_hot/CN/v3",
        "strategy_version": "v3",
        "attribution_status": "attributed",
        "exclusion_reason": "",
        "costs_complete": True,
        "statement_period": "2026-07",
        "execution_granularity": "statement_trade_date",
        "timestamp_semantics": "market_close_ordering_sentinel",
    }
    fills.extend([
        {
            **actual_common,
            "fill_id": "actual-buy",
            "order_id": "actual-buy",
            "side": "buy",
            "quantity": "1",
            "price": "10",
            "fee": "0.1",
            "filled_at": "2026-07-16T15:00:00+08:00",
            "statement_sequence": 1,
        },
        {
            **actual_common,
            "fill_id": "actual-sell",
            "order_id": "actual-sell",
            "side": "sell",
            "quantity": "1",
            "price": "12",
            "fee": "0.1",
            "filled_at": "2026-07-17T15:00:00+08:00",
            "statement_sequence": 2,
        },
    ])
    stats = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v3",
            "strategy_version": "v3",
        }],
        generated_at="2026-08-24T18:00:00+08:00",
        statistics_cutoff_at="2026-08-24T15:00:00+08:00",
    )
    stats["sources"] = [
        {
            "source": "simulation",
            "source_id": "simulation:futu:101",
            "broker": "futu",
            "account_id": "101",
            "market": "CN",
            "orders_seen": 8,
            "fill_count": 8,
            "statistics_cutoff_at": "2026-08-24T15:00:00+08:00",
            "status": "available",
        },
        {
            "source": "actual",
            "source_id": "actual:eastmoney:eastmoney_main",
            "broker": "eastmoney",
            "account_id": "eastmoney_main",
            "market": "CN",
            "orders_seen": 2,
            "fill_count": 2,
            "statistics_cutoff_at": "2026-07-31T15:00:00+08:00",
            "status": "available",
        },
    ]
    write_trend_api_stats(tmp_path, stats)

    result = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert result["sample_counts"] == {
        "discipline": 4,
        "actual": 1,
        "required": 30,
    }
    assert result["sample_details"]["discipline"]["eligible_sample_count"] == 4
    assert result["metrics"]["period_net_return"]["discipline"]["value"] is not None
    assert result["metrics"]["period_net_return"]["actual"]["value"] is None
    assert result["metrics"]["period_net_return"]["actual"]["reason"] == (
        "实际执行日终净值缺失"
    )


def projection_stats_fill(
    label: str,
    *,
    source: str,
    broker: str,
    account_id: str,
    market: str,
    strategy_version: str,
    side: str,
    filled_at: str,
    attribution_status: str = "attributed",
    exclusion_reason: str = "",
    statement_sequence: int | None = None,
) -> dict[str, object]:
    fill = {
        "fill_id": f"{label}-{side}",
        "order_id": f"{label}-{side}",
        "source": source,
        "source_id": f"{source}:{broker}:{account_id}",
        "broker": broker,
        "account_id": account_id,
        "market": market,
        "symbol": label,
        "currency": {"CN": "CNY", "HK": "HKD", "US": "USD"}[market],
        "side": side,
        "quantity": "1",
        "price": "10" if side == "buy" else "11",
        "fee": "0" if source == "simulation" else "0.1",
        "filled_at": filled_at,
        "strategy_id": (
            f"trend_animals_warm_to_hot/{market}/{strategy_version}"
            if attribution_status == "attributed"
            else ""
        ),
        "strategy_version": (
            strategy_version if attribution_status == "attributed" else ""
        ),
        "attribution_status": attribution_status,
        "exclusion_reason": exclusion_reason,
        "costs_complete": source == "actual",
    }
    if source == "simulation":
        fill.update({
            "normal_cost_rate": "0.001",
            "normal_cost_model": "estimated complete round-trip normal costs",
            "report_sha256": "a" * 64,
        })
    if broker in {"eastmoney", "phillips"}:
        fill.update({
            "statement_period": "2026-07",
            "execution_granularity": "statement_trade_date",
            "timestamp_semantics": "market_close_ordering_sentinel",
            "statement_sequence": statement_sequence,
        })
    return fill


def write_projection_metric_history(
    root: Path,
    market: str,
    *,
    discipline_days: int,
    actual_days: int,
    benchmark_days: int,
) -> None:
    start = date.fromisoformat(trend_review.TREND_V1_EFFECTIVE_FROM[market])
    snapshot = strategy_snapshot(market)
    benchmark_source, benchmark_symbol = {
        "CN": ("CSI_500_PRICE", "SH.000905"),
        "HK": ("HSI_PRICE", "HK.800000"),
        "US": ("SPY_QFQ", "US.SPY"),
    }[market]
    for index in range(benchmark_days):
        trading_date = (start + timedelta(days=index)).isoformat()
        if index < discipline_days:
            trend_review.freeze_discipline_fact(
                root,
                market,
                trading_date,
                str(100000 + index * 100),
                [],
                snapshot,
            )
        if index < actual_days:
            trend_review.freeze_actual_equity_fact(
                root,
                market,
                trading_date,
                str(100000 + index * 80),
                [],
                snapshot,
            )
        trend_review.freeze_benchmark_fact(
            root,
            market,
            trading_date,
            {
                "date": trading_date,
                "close": str(1000 + index),
                "source_id": benchmark_source,
                "futu_symbol": benchmark_symbol,
            },
        )
    if market == "US" and actual_days:
        trend_review.freeze_actual_fill_batch(
            root,
            {"broker": "tiger"},
            [],
            (start + timedelta(days=actual_days - 1)).isoformat(),
            coverage_start=start.isoformat(),
        )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True, exist_ok=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")


def write_projection_stats(
    root: Path,
    fills: list[dict[str, object]],
    *,
    market: str,
    strategy_version: str = "v3",
    simulation_cutoff: str | None = None,
    actual_cutoff: str | None = None,
) -> dict[str, object]:
    artifact_cutoff = "2026-08-24T16:00:00+08:00"
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": market,
            "strategy_id": f"trend_animals_warm_to_hot/{market}/{strategy_version}",
            "strategy_version": strategy_version,
        }],
        generated_at="2026-08-24T18:00:00+08:00",
        statistics_cutoff_at=artifact_cutoff,
    )
    sources = []
    for source, cutoff in (
        ("simulation", simulation_cutoff),
        ("actual", actual_cutoff),
    ):
        if cutoff is None:
            continue
        broker = "futu" if source == "simulation" else {
            "CN": "eastmoney",
            "HK": "phillips",
            "US": "tiger",
        }[market]
        account_id = "101" if source == "simulation" else {
            "CN": "eastmoney_main",
            "HK": "phillips_main",
            "US": "tiger_main",
        }[market]
        source_fills = [fill for fill in fills if fill["source"] == source]
        sources.append({
            "source": source,
            "source_id": f"{source}:{broker}:{account_id}",
            "broker": broker,
            "account_id": account_id,
            "market": market,
            "orders_seen": len(source_fills),
            "fill_count": len(source_fills),
            "statistics_cutoff_at": cutoff,
            "status": "available",
        })
    payload["sources"] = sources
    write_trend_api_stats(root, payload)
    return payload


def test_projection_discipline_sample_count_matches_kelly_qualified_pool(
    tmp_path: Path,
) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=4, actual_days=0, benchmark_days=4
    )
    fills = [
        projection_stats_fill(
            label,
            source="simulation",
            broker="futu",
            account_id="101",
            market="US",
            strategy_version="v3",
            side=side,
            filled_at=filled_at,
        )
        for label, side, filled_at in (
            ("ONE", "buy", "2026-07-17T10:00:00-04:00"),
            ("ONE", "sell", "2026-07-18T10:00:00-04:00"),
            ("TWO", "buy", "2026-07-19T10:00:00-04:00"),
            ("TWO", "sell", "2026-07-20T10:00:00-04:00"),
        )
    ]
    payload = write_projection_stats(
        tmp_path,
        fills,
        market="US",
        simulation_cutoff="2026-08-24T16:00:00+08:00",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "US")
    kelly = calculate_trend_kelly(
        trend_kelly_rounds_from_payload(payload),
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )

    assert projection["sample_counts"]["discipline"] == kelly.eligible_sample_count


def test_projection_actual_zero_sample_count_is_available_with_exclusions(
    tmp_path: Path,
) -> None:
    write_projection_metric_history(
        tmp_path, "CN", discipline_days=2, actual_days=2, benchmark_days=2
    )
    fills = [
        projection_stats_fill(
            "MANUAL",
            source="actual",
            broker="eastmoney",
            account_id="eastmoney_main",
            market="CN",
            strategy_version="v3",
            side=side,
            filled_at=f"2026-07-{16 + index:02d}T15:00:00+08:00",
            attribution_status="outside_strategy",
            exclusion_reason="no_matching_opening_strategy_action",
            statement_sequence=index + 1,
        )
        for index, side in enumerate(("buy", "sell"))
    ]
    write_projection_stats(
        tmp_path,
        fills,
        market="CN",
        actual_cutoff="2026-08-24T16:00:00+08:00",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["actual"] == 0
    assert projection["sample_details"]["actual"]["available"] is True
    assert projection["sample_details"]["actual"]["excluded_candidate_count"] == 1
    assert projection["sample_details"]["actual"]["exclusion_reasons"] == [{
        "reason": "no_matching_opening_strategy_action",
        "count": 1,
    }]


def test_projection_sample_conservation_is_visible(tmp_path: Path) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=3, actual_days=0, benchmark_days=3
    )
    fills = [
        projection_stats_fill(
            label,
            source="simulation",
            broker="futu",
            account_id="101",
            market="US",
            strategy_version="v3",
            side=side,
            filled_at=filled_at,
        )
        for label, side, filled_at in (
            ("CLOSED", "buy", "2026-07-17T10:00:00-04:00"),
            ("CLOSED", "sell", "2026-07-18T10:00:00-04:00"),
            ("OPEN", "buy", "2026-07-19T10:00:00-04:00"),
        )
    ]
    write_projection_stats(
        tmp_path,
        fills,
        market="US",
        simulation_cutoff="2026-08-24T16:00:00+08:00",
    )

    detail = trend_review.build_trend_review_projection(tmp_path, "US")[
        "sample_details"
    ]["discipline"]

    assert detail["discovered_candidate_count"] == (
        detail["eligible_sample_count"]
        + detail["excluded_candidate_count"]
        + detail["incomplete_open_candidate_count"]
    )


def test_projection_metric_cutoffs_are_source_specific(tmp_path: Path) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=4, actual_days=2, benchmark_days=4
    )
    write_projection_stats(
        tmp_path,
        [],
        market="US",
        simulation_cutoff="2026-08-24T16:00:00+08:00",
        actual_cutoff="2026-08-23T16:00:00+08:00",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["metric_cutoffs"] == {
        "discipline": "2026-07-20",
        "actual": "2026-07-18",
    }
    assert projection["sample_cutoffs"] == {
        "discipline": "2026-08-24T16:00:00+08:00",
        "actual": "2026-08-23T16:00:00+08:00",
    }
    assert set(projection["metrics"]["period_net_return"]) == {
        "discipline",
        "actual",
        "discipline_benchmark",
        "actual_benchmark",
    }
    assert (
        projection["metrics"]["period_net_return"]["discipline_benchmark"]
        != projection["metrics"]["period_net_return"]["actual_benchmark"]
    )


def test_projection_daily_metrics_ignore_kelly_identity_boundary(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=0, days=3)
    path = tmp_path / "trend_review/daily/CN/2026-07-18.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = live_trend_strategy_snapshot(
        "CN", "test-sha", (), strategy_version="v4"
    )
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["discipline"] is None
    assert projection["metric_cutoffs"]["discipline"] == "2026-07-18"
    assert Decimal(
        projection["metrics"]["period_net_return"]["discipline"]["value"]
    ) == Decimal("0.2")
    assert (
        projection["metrics"]["period_net_return"]
        ["discipline_benchmark"]["value"]
        is not None
    )


def test_projection_actual_metrics_ignore_kelly_identity_boundary(
    tmp_path: Path,
) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=0, actual_days=3, benchmark_days=3
    )
    path = tmp_path / "trend_review/facts/actual_equity/US/2026-07-18.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = live_trend_strategy_snapshot(
        "US", "test-sha", (), strategy_version="v4"
    )
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["sample_counts"]["actual"] is None
    assert projection["metric_cutoffs"]["actual"] == "2026-07-19"
    assert Decimal(
        projection["metrics"]["period_net_return"]["actual"]["value"]
    ) == Decimal("0.16")
    assert (
        projection["metrics"]["period_net_return"]["actual_benchmark"]["value"]
        is not None
    )


def test_projection_metric_cutoff_stops_before_missing_equity_value(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=0, days=3)
    path = tmp_path / "trend_review/daily/CN/2026-07-17.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact.pop("discipline_equity_after_fees")
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["metric_cutoffs"]["discipline"] == "2026-07-16"
    assert projection["metrics"]["period_net_return"]["discipline"] == {
        "value": "0",
        "reason": None,
    }


def test_missing_common_metric_cutoff_preserves_discipline_metrics(
    tmp_path: Path,
) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=3, actual_days=0, benchmark_days=3
    )
    write_projection_stats(
        tmp_path,
        [],
        market="US",
        simulation_cutoff="2026-08-24T16:00:00+08:00",
        actual_cutoff="2026-08-24T16:00:00+08:00",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["common_cutoff"] is None
    assert projection["metrics"]["period_net_return"]["discipline"]["value"] is not None
    assert projection["metrics"]["period_net_return"]["actual"]["value"] is None


@pytest.mark.parametrize(
    ("market", "broker", "account_id", "hour"),
    [
        ("CN", "eastmoney", "eastmoney_main", 15),
        ("HK", "phillips", "phillips_main", 16),
    ],
)
def test_cn_hk_actual_metric_cutoff_unavailable_with_statement_rounds(
    tmp_path: Path,
    market: str,
    broker: str,
    account_id: str,
    hour: int,
) -> None:
    write_projection_metric_history(
        tmp_path, market, discipline_days=2, actual_days=2, benchmark_days=2
    )
    start = date.fromisoformat(trend_review.TREND_V1_EFFECTIVE_FROM[market])
    fills = [
        projection_stats_fill(
            "ROUND",
            source="actual",
            broker=broker,
            account_id=account_id,
            market=market,
            strategy_version="v3",
            side=side,
            filled_at=(
                f"{(start + timedelta(days=index)).isoformat()}T{hour:02d}:00:00+08:00"
            ),
            statement_sequence=index + 1,
        )
        for index, side in enumerate(("buy", "sell"))
    ]
    write_projection_stats(
        tmp_path,
        fills,
        market=market,
        actual_cutoff="2026-08-24T16:00:00+08:00",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, market)

    assert projection["sample_counts"]["actual"] == 1
    assert projection["metric_cutoffs"]["actual"] is None
    assert projection["metrics"]["period_net_return"]["actual"] == {
        "value": None,
        "reason": "实际执行日终净值缺失",
    }


def test_projection_does_not_mix_strategy_versions(tmp_path: Path) -> None:
    write_separate_review_facts(tmp_path, discipline_count=31, actual_count=31)
    path = tmp_path / "trend_review/facts/discipline/CN/2026-08-15.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = strategy_snapshot(version="v2")
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"]["strategy_version"] == "v3"
    assert projection["strategy_snapshot"]["effective_from"] == "2026-07-20"
    assert projection["sample_counts"]["discipline"] is None
    assert projection["metric_cutoffs"]["discipline"] == "2026-08-24"


@pytest.mark.parametrize("stream", ["discipline", "actual_equity"])
def test_projection_excludes_v1_facts_with_wrong_strategy_id(
    stream: str,
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=31, actual_count=31)
    path = tmp_path / f"trend_review/facts/{stream}/CN/2026-08-15.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = {
        **fact["strategy_snapshot"],
        "strategy_id": "trend_animals_warm_to_hot/US/v1",
    }
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": None,
        "actual": None,
        "required": 30,
    }


def test_projection_excludes_noncanonical_parameters_within_one_strategy_version(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    path = tmp_path / "trend_review/facts/discipline/CN/2026-07-17.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"]["parameters"] = {"position_limit": 99}
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"] == trend_strategy_snapshot(
        "CN", "test-sha", ()
    )


def test_projection_allows_process_version_change_without_parameter_change(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    for stream in ("discipline", "actual_equity"):
        path = tmp_path / f"trend_review/facts/{stream}/CN/2026-08-24.json"
        fact = json.loads(path.read_text(encoding="utf-8"))
        fact["strategy_snapshot"]["process_version"] = "new-code-sha"
        path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"]["process_version"] == "new-code-sha"


def test_projection_can_show_latest_snapshot_from_actual_stream(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    for path in (
        tmp_path / "trend_review/facts/actual_fill_completeness/CN"
    ).glob("*.json"):
        path.unlink()
    path = tmp_path / "trend_review/facts/actual_equity/CN/2026-08-24.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"]["process_version"] = "actual-latest-sha"
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None
    assert projection["strategy_snapshot"]["process_version"] == "actual-latest-sha"


def test_projection_shows_current_strategy_before_first_effective_sample(
    tmp_path: Path,
) -> None:
    snapshot = strategy_snapshot("US")
    trend_review.freeze_discipline_fact(
        tmp_path,
        "US",
        "2026-07-16",
        "100000",
        [],
        snapshot,
    )
    rates = tmp_path / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-16,4.25\n", encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "US")

    assert projection["common_cutoff"] is None
    assert projection["sample_counts"] == {
        "discipline": None,
        "actual": None,
        "required": 30,
    }
    assert projection["strategy_snapshot"] == trend_strategy_snapshot(
        "US", "test-sha", ()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("market", "US"), ("effective_from", "2026-07-15")],
)
def test_close_report_rejects_wrong_strategy_market_or_effective_date(
    field: str, value: str,
) -> None:
    report = cn_buy_report()
    report["strategy_snapshot"][field] = value

    with pytest.raises(ValueError, match="strategy snapshot is unavailable"):
        trend_review.validate_trend_review_close_report(
            report, "2026-07-17", "CN"
        )


def test_projection_snapshot_uses_latest_fact_without_common_cutoff_gating(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(
        tmp_path,
        discipline_count=0,
        actual_count=0,
        complete_through="2026-07-17",
    )
    for stream in ("discipline", "actual_equity"):
        path = tmp_path / f"trend_review/facts/{stream}/CN/2026-08-24.json"
        fact = json.loads(path.read_text(encoding="utf-8"))
        fact["strategy_snapshot"] = {
            **fact["strategy_snapshot"],
            "process_version": "future-sha",
        }
        path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None
    assert projection["strategy_snapshot"]["process_version"] == "future-sha"
    assert projection["strategy_snapshot"] == trend_strategy_snapshot(
        "CN", "future-sha", ()
    )
    assert projection["strategy_snapshot"]["strategy_version"] == "v3"


def test_projection_does_not_rebuild_actual_samples_from_legacy_fills(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=0, days=2)
    snapshot = strategy_snapshot()
    trend_review.freeze_actual_equity_fact(
        tmp_path,
        "CN",
        "2026-07-17",
        "100000",
        [{"symbol": "600001", "quantity": "100", "opened_at": "2026-07-17"}],
        snapshot,
    )
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [actual_fill("sell-1", "600001", "SELL", "100", "2026-07-16")],
        "2026-07-17",
        coverage_start="2026-07-16",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["actual"] is None
