from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock

import pytest

from open_trader import market_trend
import open_trader.trend_review as trend_review
from open_trader.a_share_trend import (
    AccountSnapshot,
    CandidateInput,
    _report_payload,
    build_report,
    live_trend_strategy_snapshot,
    trend_strategy_snapshot,
)
from open_trader.strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    observe_strategy_equity,
)


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


def test_v4_rebuild_uses_frozen_drawdown_decision_after_live_state_changes(
    tmp_path: Path,
) -> None:
    snapshot = live_trend_strategy_snapshot(
        "CN", "oldsha", (622466, 697199)
    )
    drawdown = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": "CN",
        "strategy_id": snapshot["strategy_id"],
        "strategy_version": "v4",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v4|v4",
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
        strategy_version="v4",
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
        strategy_version="v4",
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
        return {"orders": self.orders}


def cn_buy_report(
    *, weight: str = "0.04", symbol: str = "600001", shares: int = 400
) -> dict[str, object]:
    return {
        "account": {
            "net_value": "735164.41",
            "fresh": True,
            "source_date": "2026-07-17",
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

    trend_review.resolve_trend_action(
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
    assert len(terminal_events) == 1
    assert terminal_events[0] | {
        "symbol": "600001",
        "side": "sell",
        "status": "filled",
        "reason": "position_zero_confirmed",
    } == terminal_events[0]

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
    ) == 1


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


@pytest.mark.parametrize("strategy_version", ["v2", "v3", "v4"])
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
            "source_id": "CSI_ALL_SHARE_PRICE",
            "futu_symbol": "SH.000985",
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
            "source_id": "CSI_ALL_SHARE_PRICE",
            "futu_symbol": "SH.000985",
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
                "source_id": "CSI_ALL_SHARE_PRICE",
                "futu_symbol": "SH.000985",
            },
        )


def test_benchmark_fact_uses_exact_market_qfq_close() -> None:
    class Quote:
        def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[object]:
            assert (symbol, start, end) == ("SH.000985", "2026-07-17", "2026-07-17")
            return [type("Bar", (), {"date": "2026-07-17", "close": 6123.45})()]

    assert trend_review.benchmark_fact(Quote(), "CN", "2026-07-17") == {
        "date": "2026-07-17",
        "close": "6123.45",
        "source_id": "CSI_ALL_SHARE_PRICE",
        "futu_symbol": "SH.000985",
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
    if broker_fact != "active":
        assert any(event.get("status") == "uncertain" for event in events)


def write_review_history(
    root: Path,
    *,
    completed_trades: int,
    days: int,
    missing_actual_index: int | None = None,
) -> None:
    daily = root / "trend_review/daily/CN"
    daily.mkdir(parents=True)
    start = date(2026, 5, 1)
    snapshot = {
        "strategy_id": "trend_animals_warm_to_hot/CN/v1",
        "strategy_name": "A 股短线右侧趋势",
        "strategy_version": "v1",
        "market": "CN",
        "parameter_rows": [],
        "parameters": {},
    }
    for index in range(days):
        trading_date = (start + timedelta(days=index)).isoformat()
        orders: list[dict[str, object]] = []
        if index < completed_trades:
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
        payload: dict[str, object] = {
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
                "source_id": "CSI_ALL_SHARE_PRICE",
                "futu_symbol": "SH.000985",
            },
        }
        if index == missing_actual_index:
            payload.pop("actual_equity")
        (daily / f"{trading_date}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-04-30,4.0\n", encoding="utf-8")


def test_projection_closes_non_overlapping_batch_at_thirtieth_trade(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=31, days=45)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["batch"]["completed_trade_count"] == 30
    assert projection["batch"]["batch_number"] == 1
    assert Path(projection["batch_path"]).exists()
    assert set(projection["metrics"]) == {
        "period_net_return",
        "market_excess_return",
        "max_drawdown",
        "calmar",
        "sharpe",
    }
    assert all(
        set(values) == {"discipline", "actual", "benchmark"}
        for values in projection["metrics"].values()
    )
    assert projection["metrics"]["market_excess_return"]["benchmark"] == {
        "value": "0",
        "reason": None,
    }


def test_projection_batch_starts_at_earliest_selected_entry(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)
    daily = tmp_path / "trend_review/daily/CN"
    first_path, _, third_path = sorted(daily.glob("*.json"))[:3]
    first = json.loads(first_path.read_text(encoding="utf-8"))
    delayed_sell = first["orders"].pop()
    first_path.write_text(json.dumps(first), encoding="utf-8")
    third = json.loads(third_path.read_text(encoding="utf-8"))
    third["orders"].append(delayed_sell)
    third_path.write_text(json.dumps(third), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["batch"]["start_date"] == "2026-05-01"


def test_projection_marks_missing_actual_curve_as_data_insufficient(
    tmp_path: Path,
) -> None:
    write_review_history(
        tmp_path,
        completed_trades=30,
        days=40,
        missing_actual_index=10,
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["metrics"]["sharpe"]["actual"] == {
        "value": None,
        "reason": "实际执行日终净值缺失",
    }


def test_projection_waits_for_thirty_trades(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=29, days=40)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["batch"]["completed_trade_count"] == 29
    assert projection["batch_path"] is None
    assert projection["metrics"]["calmar"]["discipline"]["value"] is None


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
            "parameter_rows": [],
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


def test_projection_counts_partial_exit_once_and_keeps_entry_version(
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
    batch = json.loads(Path(projection["batch_path"]).read_text(encoding="utf-8"))
    final_trade = batch["completed_trades"][-1]

    assert final_trade["quantity"] == "100"
    assert final_trade["strategy_snapshot"]["strategy_version"] == "v1"
