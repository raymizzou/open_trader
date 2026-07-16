from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.trend_review as trend_review
from open_trader.a_share_trend import trend_strategy_snapshot


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
            "candidate_pool_ids": [622466, 697199],
            "generated_at": "2026-07-16T17:00:00+08:00",
            "metadata": {"market": "CN", "broker": "eastmoney"},
        },
    }

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt["process_version"] == "newsha"
    assert rebuilt["strategy_snapshot"]["process_version"] == "newsha"
    assert rebuilt["account"]["net_value"] == "100000"


class FakeTrendSimClient:
    def __init__(
        self,
        *,
        nav: str = "100000",
        positions: list[dict[str, object]] | None = None,
    ) -> None:
        self.nav = nav
        self.positions = positions or []
        self.requests: list[dict[str, object]] = []

    def account_snapshot(self) -> dict[str, object]:
        return {
            "acc_id": 101,
            "net_value": self.nav,
            "positions": self.positions,
        }

    def place_order(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {
            "futu_order_id": f"SIM-{len(self.requests)}",
            "status": "submitted",
        }


def cn_buy_report(*, weight: str = "0.04") -> dict[str, object]:
    return {
        "account": {"net_value": "735164.41"},
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
                    "symbol": "600001",
                    "target_weight": weight,
                    "lot_size": 100,
                }
            ]
        },
    }


def test_open_uses_sim_nav_current_price_and_frozen_lot(tmp_path: Path) -> None:
    client = FakeTrendSimClient()

    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        prices={"600001": Decimal("10")},
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
    )
    repeated = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=cn_buy_report(),
        client=client,
        prices={"600001": Decimal("9")},
        market="CN",
        execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
    )

    assert client.requests[0]["qty"] == "400"
    assert client.requests[0]["order_type"] == "MARKET"
    assert result["submitted_count"] == 1
    assert repeated["submitted_count"] == 0
    assert len(client.requests) == 1


def test_first_open_requires_empty_dedicated_simulate_account(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(
        positions=[{"code": "SH.600001", "qty": "100"}]
    )

    with pytest.raises(
        trend_review.TrendReviewAccountStateError,
        match="simulate account must start with zero positions",
    ):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path,
            report=cn_buy_report(),
            client=client,
            prices={"600001": Decimal("10")},
            market="CN",
            execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
        )


def test_close_uses_authoritative_simulate_account_nav(tmp_path: Path) -> None:
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
    assert payload["actual_equity"] == "735164.41"


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
            "remark": "trend-review:CN:event-1",
        }
    ]
    assert first["submitted_count"] == 1
    assert repeated["submitted_count"] == 0


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
