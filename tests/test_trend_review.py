from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader import market_trend
import open_trader.trend_review as trend_review
from open_trader.a_share_trend import (
    AccountSnapshot,
    CandidateInput,
    _report_payload,
    build_report,
    trend_strategy_snapshot,
)
from open_trader.models import Market, TradeFill


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
            "candidate_pool_ids": [622466, 697199],
            "generated_at": "2026-07-16T17:00:00+08:00",
            "metadata": {"market": "CN", "broker": "eastmoney"},
        },
    }

    rebuilt = trend_review.rebuild_trend_report_from_evidence(evidence)

    assert rebuilt["process_version"] == "newsha"
    assert rebuilt["strategy_snapshot"]["process_version"] == "newsha"
    assert rebuilt["account"]["net_value"] == "100000"


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
        positions: list[dict[str, object]] | None = None,
        fail_orders: int = 0,
        accepted_before_failure: bool = False,
    ) -> None:
        self.nav = nav
        self.positions = positions or []
        self.requests: list[dict[str, object]] = []
        self.orders: list[dict[str, object]] = []
        self.fail_orders = fail_orders
        self.accepted_before_failure = accepted_before_failure

    def account_snapshot(self) -> dict[str, object]:
        return {
            "acc_id": 101,
            "net_value": self.nav,
            "positions": self.positions,
        }

    def place_order(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        if self.fail_orders:
            self.fail_orders -= 1
            if self.accepted_before_failure:
                self.orders.append(dict(request))
            raise RuntimeError("place order failed")
        self.orders.append(dict(request))
        return {
            "futu_order_id": f"SIM-{len(self.requests)}",
            "status": "submitted",
        }

    def list_orders(self) -> dict[str, object]:
        return {"orders": self.orders}


def cn_buy_report(
    *, weight: str = "0.04", symbol: str = "600001"
) -> dict[str, object]:
    return {
        "account": {
            "net_value": "735164.41",
            "fresh": True,
            "source_date": "2026-07-17",
            "positions": [],
        },
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


def test_open_retries_intent_when_failed_order_is_absent_at_broker(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "prices": {"600001": Decimal("10")},
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
    }

    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    result = trend_review.execute_trend_review_open(**arguments)

    assert result["status"] == "submitted"
    assert result["submitted_count"] == 1
    assert len(client.requests) == 2


def test_open_reconciles_accepted_order_after_response_failure(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1, accepted_before_failure=True)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "prices": {"600001": Decimal("10")},
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
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

    assert result["status"] == "unchanged"
    assert len(client.requests) == 1
    assert list(tmp_path.glob("trend_review/ledgers/CN/open/*/*-result.json"))


def test_newer_revision_cannot_reconcile_to_older_response_failure(
    tmp_path: Path,
) -> None:
    client = FakeTrendSimClient(fail_orders=1, accepted_before_failure=True)
    first = {
        "data_dir": tmp_path,
        "report": cn_buy_report(symbol="600001"),
        "client": client,
        "prices": {"600001": Decimal("10")},
        "market": "CN",
        "execution_date": "2026-07-17",
        "now": "2026-07-17T09:31:00+08:00",
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**first)

    client.fail_orders = 1
    client.accepted_before_failure = False
    revised = {
        **first,
        "report": cn_buy_report(symbol="600002"),
        "prices": {"600002": Decimal("20")},
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**revised)
    result = trend_review.execute_trend_review_open(**revised)

    assert result["submitted_count"] == 1
    assert len(client.requests) == 3
    assert client.requests[0]["remark"] != client.requests[1]["remark"]
    assert client.requests[-1] | {
        "futu_code": "SH.600002",
        "side": "buy",
        "qty": "200",
    } == client.requests[-1]
    assert len(client.requests[-1]["remark"].encode("utf-8")) <= 64


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
            "remark": "trend-review:CN:event-1",
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

    assert result["status"] == "submitted"
    assert result["submitted_count"] == 1
    assert len(client.requests) == 2


def strategy_snapshot(market: str = "CN", version: str = "v1") -> dict[str, object]:
    return {
        "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
        "strategy_name": "trend",
        "strategy_version": version,
        "market": market,
        "effective_from": "2026-07-14",
        "process_version": "test-sha",
        "parameter_rows": [],
        "parameters": {},
    }


def actual_fill(
    source_id: str,
    symbol: str,
    side: str,
    quantity: str,
    executed_at: str,
    *,
    account_alias: str = "eastmoney_main",
    price: str = "10",
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
    )


def write_separate_review_facts(
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
                "source_id": "CSI_ALL_SHARE_PRICE",
                "futu_symbol": "SH.000985",
            },
        )
        if index < actual_count:
            symbol = f"{700000 + index:06d}"
            fills.extend(
                [
                    actual_fill(f"buy-{index}", symbol, "BUY", "100", trading_date),
                    actual_fill(f"sell-{index}", symbol, "SELL", "100", trading_date),
                ]
            )
    trend_review.freeze_actual_fill_batch(
        root,
        {"market": "CN", "source": "test"},
        fills,
        complete_through or (start + timedelta(days=39)).isoformat(),
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
                "source_id": "CSI_ALL_SHARE_PRICE",
                "futu_symbol": "SH.000985",
            },
        }
        (daily / f"{trading_date}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    rates = root / "rates/DGS3MO.csv"
    rates.parent.mkdir(parents=True)
    rates.write_text("DATE,DGS3MO\n2026-07-15,4.0\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("discipline_count", "actual_count", "discipline_ready", "actual_ready"),
    [(29, 30, False, True), (30, 29, True, False), (31, 31, True, True)],
)
def test_projection_unlocks_series_independently_and_never_batches(
    tmp_path: Path,
    discipline_count: int,
    actual_count: int,
    discipline_ready: bool,
    actual_ready: bool,
) -> None:
    write_separate_review_facts(
        tmp_path,
        discipline_count=discipline_count,
        actual_count=actual_count,
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": discipline_count,
        "actual": actual_count,
        "required": 30,
    }
    assert (
        projection["metrics"]["calmar"]["discipline"]["value"] is not None
    ) is discipline_ready
    assert (
        projection["metrics"]["calmar"]["actual"]["value"] is not None
    ) is actual_ready
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
        set(values) == {"discipline", "actual", "benchmark"}
        for values in projection["metrics"].values()
    )
    assert projection["metrics"]["market_excess_return"]["benchmark"] == {
        "value": "0",
        "reason": None,
    }


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


@pytest.mark.parametrize("reverse", [False, True])
def test_completed_cycles_rejects_conflicting_payloads_for_one_identity(
    reverse: bool,
) -> None:
    buy = asdict(actual_fill("fill-1", "600001", "BUY", "100", "2026-07-16"))
    sell = asdict(actual_fill("fill-1", "600001", "SELL", "100", "2026-07-16"))

    with pytest.raises(ValueError, match="conflicting actual fill identity"):
        trend_review._completed_cycles([sell, buy] if reverse else [buy, sell])


def test_old_daily_actual_equity_never_fabricates_actual_samples(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [],
        "2026-08-24",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": 30,
        "actual": 0,
        "required": 30,
    }


def test_missing_actual_fill_source_stops_common_cutoff(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None


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


def test_common_cutoff_respects_equity_continuity_and_fill_completeness(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    missing = (
        tmp_path
        / "trend_review/facts/actual_equity/CN/2026-07-18.json"
    )
    missing.unlink()

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] == "2026-07-17"
    assert projection["interval"] == {
        "start": "2026-07-16",
        "end": "2026-07-17",
    }


def test_common_cutoff_uses_available_benchmark_calendar() -> None:
    assert trend_review._common_cutoff(
        "2026-07-16",
        {"2026-07-16", "2026-07-17", "2026-07-20"},
        {"2026-07-16", "2026-07-17"},
        {"2026-07-16", "2026-07-17", "2026-07-20"},
    ) == "2026-07-17"


def test_projection_does_not_mix_strategy_versions(tmp_path: Path) -> None:
    write_separate_review_facts(tmp_path, discipline_count=31, actual_count=31)
    path = tmp_path / "trend_review/facts/discipline/CN/2026-08-15.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = strategy_snapshot(version="v2")
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["strategy_snapshot"]["strategy_version"] == "v1"
    assert projection["strategy_snapshot"]["effective_from"] == "2026-07-16"
    assert projection["sample_counts"]["discipline"] == 30


def test_projection_snapshot_ignores_facts_after_common_cutoff(
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
            "parameters": {"future": True},
        }
        path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] == "2026-07-17"
    assert projection["strategy_snapshot"]["process_version"] == "test-sha"
    assert projection["strategy_snapshot"]["parameters"] == {}
    assert projection["strategy_snapshot"]["strategy_version"] == "v1"


def test_late_opening_fact_never_backfills_an_earlier_sell(tmp_path: Path) -> None:
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
    )

    with pytest.raises(ValueError, match="sell fill exceeds actual position"):
        trend_review.build_trend_review_projection(tmp_path, "CN")
