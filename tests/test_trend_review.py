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
        "effective_from": trend_review.TREND_V1_EFFECTIVE_FROM[market],
        "process_version": "test-sha",
        "parameter_rows": [
            {"group": "rules", "name": "position limit", "value": "10"}
        ],
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
    path = tmp_path / "trend_review/facts/actual_fills/CN" / f"{digest}.json"
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


def test_reimport_keeps_sequence_bearing_fill_body_byte_for_byte(
    tmp_path: Path,
) -> None:
    fill = actual_fill(
        "existing-fill", "600001", "BUY", "100", "2026-07-16",
        source_sequence=4,
    )
    payload = {
        "schema_version": "open_trader.trend_review.fill.v1",
        **asdict(fill),
    }
    identity = trend_review._actual_fill_identity(payload)
    digest = trend_review.hashlib.sha256(
        trend_review._canonical_json_bytes({"identity": identity})
    ).hexdigest()
    path = tmp_path / "trend_review/facts/actual_fills/CN" / f"{digest}.json"
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
    assert loaded[0]["source_sequence"] == 4


def test_tiger_reimport_keeps_legacy_fill_body_byte_for_byte(tmp_path: Path) -> None:
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
    for series, ready in (
        ("discipline", discipline_ready),
        ("actual", actual_ready),
    ):
        if ready:
            assert Decimal(
                projection["metrics"]["market_excess_return"][series]["value"]
            ) == Decimal(
                projection["metrics"]["period_net_return"][series]["value"]
            ) - Decimal(
                projection["metrics"]["period_net_return"]["benchmark"]["value"]
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


@pytest.mark.parametrize("reverse", [False, True])
def test_completed_cycles_rejects_conflicting_payloads_for_one_identity(
    reverse: bool,
) -> None:
    buy = asdict(actual_fill("fill-1", "600001", "BUY", "100", "2026-07-16"))
    sell = asdict(actual_fill("fill-1", "600001", "SELL", "100", "2026-07-16"))

    with pytest.raises(ValueError, match="conflicting actual fill identity"):
        trend_review._completed_cycles([sell, buy] if reverse else [buy, sell])


def test_legacy_single_date_completeness_never_fabricates_samples(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)
    trend_review.freeze_actual_fill_batch(
        tmp_path,
        {"broker": "eastmoney"},
        [],
        "2026-08-24",
    )

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"] == {
        "discipline": 0,
        "actual": 0,
        "required": 30,
    }


def test_missing_actual_fill_source_stops_common_cutoff(tmp_path: Path) -> None:
    write_review_history(tmp_path, completed_trades=30, days=40)

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["common_cutoff"] is None


def test_projection_without_cutoff_keeps_latest_complete_discipline_snapshot(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=0, days=2)
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
        "trend_animals_warm_to_hot/CN/v1"
    )
    assert projection["strategy_snapshot"]["process_version"] == "latest-sha"
    assert projection["strategy_snapshot"]["parameters"] == {}
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


def test_common_cutoff_stops_before_gap_between_fill_coverage_intervals(
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

    assert projection["common_cutoff"] == "2026-07-16"


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
        "discipline": 30,
        "actual": 30,
        "required": 30,
    }


def test_projection_rejects_mixed_parameters_within_one_strategy_version(
    tmp_path: Path,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count=0, actual_count=0)
    path = tmp_path / "trend_review/facts/discipline/CN/2026-07-17.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"]["parameters"] = {"position_limit": 99}
    path.write_text(json.dumps(fact), encoding="utf-8")

    with pytest.raises(ValueError, match="strategy snapshot identity changed"):
        trend_review.build_trend_review_projection(tmp_path, "CN")


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
        coverage_start="2026-07-16",
    )

    with pytest.raises(ValueError, match="sell fill exceeds actual position"):
        trend_review.build_trend_review_projection(tmp_path, "CN")
