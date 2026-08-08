from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import open_trader.trend_api_stats as trend_api_stats_module

from open_trader.tiger_account import TigerAccountConfig, TigerAccountError
from open_trader.trend_api_stats import (
    FutuSimulateFillClient,
    TigerActualFillClient,
    _attribute_actual_fills,
    _futu_order_attributions,
    _merge_synced_fills,
    _tiger_order_fee,
    _tiger_transaction_record,
    build_trend_api_stats_payload,
    load_trend_api_stats,
    run_trend_statistics_cycle,
    sync_trend_api_stats,
    trend_statistics_cutoff_at,
    trend_statistics_cycle_path,
    write_trend_api_stats,
)
from open_trader.trend_review import _report_hash, _write_action_event
from open_trader.trend_kelly import load_trend_kelly_rounds


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


class FakeFutuContext:
    def __init__(self, **_: object) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return 0, FakeDataFrame([{
            "acc_id": 101,
            "acc_index": 0,
            "trd_env": "SIMULATE",
            "acc_status": "ACTIVE",
        }])

    def history_order_list_query(self, **kwargs: object) -> tuple[int, FakeDataFrame]:
        self.calls.append(("orders", dict(kwargs)))
        row = {
            "order_id": "o1", "code": "US.AAA", "trd_side": "BUY",
            "currency": "USD", "create_time": "2026-01-01 10:00:00",
            "updated_time": "2026-01-01 10:01:00",
            "dealt_qty": "2", "dealt_avg_price": "10",
        }
        return 0, FakeDataFrame([row, dict(row)])

    def order_list_query(self, **kwargs: object) -> tuple[int, FakeDataFrame]:
        self.calls.append(("active_orders", dict(kwargs)))
        return 0, FakeDataFrame([])

    def history_deal_list_query(self, **kwargs: object) -> tuple[int, FakeDataFrame]:
        raise AssertionError("Futu SIMULATE does not support deal history")

    def close(self) -> None:
        pass


def test_futu_simulate_adapter_deduplicates_orders_and_fills_without_trusting_zero_fee() -> None:
    context = FakeFutuContext()
    client = FutuSimulateFillClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=101,
        trd_market="US",
        context_factory=lambda **_: context,
        connectivity_checker=lambda *_: True,
    )

    result = client.fetch_fills(
        start="2026-01-01",
        end="2026-01-02",
        attributions_by_order={"o1": {
            "strategy_id": "trend_animals_warm_to_hot/US/v2",
            "strategy_version": "v2",
            "report_sha256": "a" * 64,
            "normal_cost_rate": "0.001",
            "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
        }},
    )

    assert result["orders_seen"] == 1
    assert result["fills"] == [{
        "fill_id": "futu-sim-order:o1:aggregate",
        "broker_fill_id": None,
        "execution_granularity": "order_aggregate",
        "order_id": "o1",
        "source": "simulation",
        "source_id": "simulation:futu:101",
        "broker": "futu",
        "account_id": "101",
        "market": "US",
        "symbol": "AAA",
        "currency": "USD",
        "side": "buy",
        "quantity": "2",
        "price": "10",
        "fee": "0",
        "costs_complete": False,
        "broker_fee_used": False,
        "filled_at": "2026-01-01T10:01:00-05:00",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
        "report_sha256": "a" * 64,
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
        "attribution_status": "attributed",
        "exclusion_reason": "",
    }]
    assert result["account_id"] == "101"
    assert result["source_id"] == "simulation:futu:101"
    assert [name for name, _ in context.calls] == ["active_orders", "orders"]


def test_rotation_action_events_flow_through_futu_attribution_and_kelly_rounds(
    tmp_path: Path,
) -> None:
    class RotationContext(FakeFutuContext):
        def history_order_list_query(self, **kwargs: object) -> tuple[int, FakeDataFrame]:
            self.calls.append(("orders", dict(kwargs)))
            return 0, FakeDataFrame([
                {
                    "order_id": "SIM-BUY", "code": "SH.WEAK", "trd_side": "BUY",
                    "currency": "CNY", "create_time": "2026-07-19 10:00:00",
                    "updated_time": "2026-07-19 10:01:00", "dealt_qty": "100",
                    "dealt_avg_price": "10",
                },
                {
                    "order_id": "SIM-SELL", "code": "SH.WEAK", "trd_side": "SELL",
                    "currency": "CNY", "create_time": "2026-07-20 10:00:00",
                    "updated_time": "2026-07-20 10:01:00", "dealt_qty": "100",
                    "dealt_avg_price": "12",
                },
            ])

    data_dir = tmp_path / "data"
    report_v10_sha = "a" * 64
    report_v11_sha = "b" * 64
    strategy_id = "trend_animals_warm_to_hot/CN/v10"
    strategy_v11_id = "trend_animals_warm_to_hot/CN/v11"
    common = {
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
    }
    _write_action_event(
        data_dir=data_dir,
        market="CN",
        execution_date="2026-07-19",
        action_key="rotation-opening",
        payload={
            "market": "CN", "date": "2026-07-19", "strategy_id": strategy_id,
            "strategy_version": "v10", "report_sha256": report_v10_sha,
            "symbol": "WEAK", "futu_code": "SH.WEAK", "side": "buy",
            "status": "filled", "order_ids": ["SIM-BUY"],
        },
        recorded_at="2026-07-19T10:01:00+08:00",
    )
    _write_action_event(
        data_dir=data_dir,
        market="CN",
        execution_date="2026-07-20",
        action_key="rotation-closing",
        payload={
            "market": "CN", "date": "2026-07-20", "strategy_id": strategy_v11_id,
            "strategy_version": "v11", "report_sha256": report_v11_sha,
            "symbol": "WEAK", "futu_code": "SH.WEAK", "side": "sell",
            "status": "filled", "order_ids": ["SIM-SELL"],
        },
        recorded_at="2026-07-20T10:01:00+08:00",
    )
    facts = [
        {
            "report_sha256": report_v10_sha, "strategy_id": strategy_id,
            "strategy_version": "v10", **common,
        },
        {
            "report_sha256": report_v11_sha, "strategy_id": strategy_v11_id,
            "strategy_version": "v11", **common,
        },
    ]
    from open_trader.trend_api_stats import _futu_order_attributions

    attributions = _futu_order_attributions(data_dir, facts)
    assert attributions["CN"]["SIM-BUY"]["strategy_version"] == "v10"
    assert attributions["CN"]["SIM-SELL"]["strategy_version"] == "v11"

    context = RotationContext()
    client = FutuSimulateFillClient(
        host="127.0.0.1", port=11111, simulate_acc_id=101, trd_market="CN",
        context_factory=lambda **_: context, connectivity_checker=lambda *_: True,
    )
    fills = client.fetch_fills(
        start="2026-07-19", end="2026-07-20",
        attributions_by_order=attributions["CN"],
    )["fills"]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[
            {"market": "CN", "strategy_id": strategy_id, "strategy_version": "v10"},
            {"market": "CN", "strategy_id": strategy_v11_id, "strategy_version": "v11"},
        ],
        generated_at="2026-07-21T00:00:00+00:00",
        statistics_cutoff_at="2026-07-21T00:00:00+00:00",
    )
    write_trend_api_stats(data_dir, payload)

    rounds = load_trend_kelly_rounds(data_dir)
    assert len(rounds) == 1
    assert rounds[0].opening_strategy_version == "v10"
    assert rounds[0].kelly_eligible is True


def test_futu_cn_order_aggregate_accepts_exchange_prefixed_symbol() -> None:
    context = FakeFutuContext()
    original = context.history_order_list_query

    def cn_orders(**kwargs: object) -> tuple[int, FakeDataFrame]:
        _, frame = original(**kwargs)
        frame.rows[0]["code"] = "SZ.300244"
        frame.rows[1]["code"] = "SZ.300244"
        frame.rows[0]["currency"] = "CNH"
        frame.rows[1]["currency"] = "CNH"
        return 0, frame

    context.history_order_list_query = cn_orders  # type: ignore[method-assign]
    client = FutuSimulateFillClient(
        host="127.0.0.1", port=11111, simulate_acc_id=101, trd_market="CN",
        context_factory=lambda **_: context, connectivity_checker=lambda *_: True,
    )

    result = client.fetch_fills(
        start="2026-01-01", end="2026-01-02", attributions_by_order={},
    )

    assert result["fills"][0]["market"] == "CN"
    assert result["fills"][0]["symbol"] == "300244"
    assert result["fills"][0]["currency"] == "CNH"


def test_futu_order_aggregate_snapshot_advances_monotonically_across_syncs() -> None:
    earlier = normalized_fill(
        "futu-sim-order:7:aggregate", "7", source="simulation", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    ) | {
        "quantity": "1",
        "broker_fill_id": None,
        "execution_granularity": "order_aggregate",
    }
    later = {
        **earlier,
        "quantity": "2",
        "price": "10.5",
        "filled_at": "2026-01-01T10:01:00-05:00",
    }

    assert _merge_synced_fills([earlier], [later]) == [later]
    with pytest.raises(ValueError, match="aggregate snapshot regressed"):
        _merge_synced_fills([later], [earlier])

    actual = normalized_fill(
        "actual-1", "9", source="actual", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    )
    with pytest.raises(ValueError, match="conflicting duplicate fill"):
        _merge_synced_fills([actual], [{**actual, "price": "11"}])


@pytest.mark.parametrize("incoming_status", ["outside_strategy", "ambiguous"])
def test_futu_order_aggregate_cannot_erase_existing_attribution(
    incoming_status: str,
) -> None:
    attributed = normalized_fill(
        "futu-sim-order:7:aggregate", "7", source="simulation", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    ) | {
        "broker_fill_id": None,
        "execution_granularity": "order_aggregate",
        "attribution_status": "attributed",
        "exclusion_reason": "",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
        "report_sha256": "a" * 64,
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
    }
    degraded = {
        **attributed,
        "filled_at": "2026-01-01T10:01:00-05:00",
        "attribution_status": incoming_status,
        "exclusion_reason": "temporary_attribution_gap",
        "strategy_id": "",
        "strategy_version": "",
        "report_sha256": "",
        "normal_cost_rate": "",
        "normal_cost_model": "",
    }

    with pytest.raises(ValueError, match="aggregate snapshot attribution changed"):
        _merge_synced_fills([attributed], [degraded])


def test_futu_order_aggregate_allows_attribution_enrichment_only_with_stable_execution() -> None:
    outside = normalized_fill(
        "futu-sim-order:7:aggregate", "7", source="simulation", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    ) | {
        "broker_fill_id": None,
        "execution_granularity": "order_aggregate",
    }
    attributed = {
        **outside,
        "filled_at": "2026-01-01T10:01:00-05:00",
        "attribution_status": "attributed",
        "exclusion_reason": "",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
        "report_sha256": "a" * 64,
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
    }

    assert _merge_synced_fills([outside], [attributed]) == [attributed]
    with pytest.raises(ValueError, match="aggregate snapshot attribution changed"):
        _merge_synced_fills(
            [attributed],
            [{**attributed, "strategy_version": "v3"}],
        )


def test_futu_order_aggregate_rejects_changed_price_when_quantity_is_unchanged() -> None:
    existing = normalized_fill(
        "futu-sim-order:7:aggregate", "7", source="simulation", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    ) | {
        "broker_fill_id": None,
        "execution_granularity": "order_aggregate",
    }
    later = {**existing, "filled_at": "2026-01-01T10:01:00-05:00"}

    assert _merge_synced_fills([existing], [later]) == [later]
    with pytest.raises(ValueError, match="aggregate average price changed"):
        _merge_synced_fills([existing], [{**later, "price": "11"}])


class FakeTigerTransactionsPage:
    def __init__(self, result: list[object], next_page_token: str | None) -> None:
        self.result = result
        self.next_page_token = next_page_token


class FakeTigerTradeClient:
    def __init__(self) -> None:
        contract = SimpleNamespace(symbol="AAA", market="US", currency="USD")
        first = SimpleNamespace(
            id="t1", order_id=7, account="U1", contract=contract, action="BUY",
            filled_quantity="1", filled_price="10", filled_amount="10",
            transacted_at="2026-01-01T10:00:00-05:00",
        )
        self.pages = {
            "": FakeTigerTransactionsPage([first, first], "next"),
            "next": FakeTigerTransactionsPage([
                SimpleNamespace(
                    id="t2", order_id=7, account="U1", contract=contract,
                    action="BUY", filled_quantity="2", filled_price="10",
                    filled_amount="20",
                    transacted_at="2026-01-01T10:01:00-05:00",
                )
            ], None),
        }
        self.transaction_tokens: list[str] = []
        self.order_page_tokens: list[str] = []
        self.order_calls: list[dict[str, object]] = []

    def get_orders(self, **kwargs: object) -> FakeTigerTransactionsPage:
        assert kwargs["sec_type"] == "STK"
        assert kwargs["market"] == "US"
        token = str(kwargs.get("page_token") or "")
        self.order_page_tokens.append(token)
        return FakeTigerTransactionsPage([
            SimpleNamespace(id=7, order_id=17, filled="3"),
        ], None)

    def get_transactions(self, **kwargs: object) -> FakeTigerTransactionsPage:
        assert kwargs["order_id"] == 7
        token = str(kwargs.get("page_token") or "")
        self.transaction_tokens.append(token)
        return self.pages[token]

    def get_order(self, **kwargs: object) -> object:
        self.order_calls.append(dict(kwargs))
        return SimpleNamespace(commission="3", charges=[], gst="0")

    def close(self) -> None:
        pass


def test_tiger_actual_adapter_pages_deduplicates_partial_fills_and_allocates_exact_order_cost() -> None:
    trade_client = FakeTigerTradeClient()
    config = TigerAccountConfig(
        tiger_id="T1", account="U1", private_key_path=None, private_key="key",
        secret_key=None, token=None, sandbox=False, config_dir=Path("/tmp"),
    )
    client = TigerActualFillClient(
        config=config,
        trade_client_factory=lambda _: trade_client,
    )

    result = client.fetch_fills(
        start="2026-01-01",
        end="2026-01-02",
        attributions_by_order={"7": {
            "strategy_id": "trend_animals_warm_to_hot/US/v2",
            "strategy_version": "v2",
            "report_sha256": "b" * 64,
        }},
    )

    assert trade_client.order_page_tokens == [""]
    assert trade_client.transaction_tokens == ["", "next"]
    assert trade_client.order_calls == [{
        "account": "U1", "id": 7, "show_charges": True,
    }]
    assert result["orders_seen"] == 1
    assert [fill["fill_id"] for fill in result["fills"]] == ["t1", "t2"]
    assert [fill["fee"] for fill in result["fills"]] == ["1", "2"]
    assert all(fill["costs_complete"] is True for fill in result["fills"])
    assert all(fill["source"] == "actual" for fill in result["fills"])


def test_tiger_total_cost_uses_commission_aggregate_plus_gst_without_adding_charge_breakdown_twice() -> None:
    order = SimpleNamespace(
        commission="3",
        gst="0.3",
        charges=[SimpleNamespace(total="1"), SimpleNamespace(total="2")],
    )
    breakdown_only = SimpleNamespace(
        commission=None,
        gst="0.3",
        charges=[SimpleNamespace(total="1"), SimpleNamespace(total="2")],
    )

    assert _tiger_order_fee(order) == Decimal("3.3")
    assert _tiger_order_fee(breakdown_only) == Decimal("3.3")


def test_tiger_naive_transaction_timestamp_is_interpreted_in_market_timezone() -> None:
    transaction = SimpleNamespace(
        id="t1", order_id=7, action="BUY", filled_quantity="1",
        filled_price="10", transacted_at="2026-01-01 10:00:00",
        contract=SimpleNamespace(symbol="AAA", market="US", currency="USD"),
    )

    assert _tiger_transaction_record(transaction)["filled_at"] == (
        "2026-01-01T10:00:00-05:00"
    )


def test_tiger_adapter_reports_api_failures_through_account_boundary() -> None:
    config = TigerAccountConfig(
        tiger_id="T1", account="U1", private_key_path=None, private_key="key",
        secret_key=None, token=None, sandbox=False, config_dir=Path("/tmp"),
    )
    trade_client = SimpleNamespace(
        get_orders=lambda **_: (_ for _ in ()).throw(RuntimeError("rate limit")),
        close=lambda: None,
    )
    client = TigerActualFillClient(
        config=config, trade_client_factory=lambda _: trade_client,
    )

    with pytest.raises(TigerAccountError, match="failed to query Tiger orders"):
        client.fetch_fills(
            start="2026-01-01", end="2026-01-02", attributions_by_order={},
        )


def test_actual_attribution_is_ambiguous_for_two_frozen_report_revisions() -> None:
    actual = normalized_fill(
        "t1", "7", source="actual", side="buy",
        filled_at="2026-01-01T10:00:00-05:00", price="10",
    )
    fact = {
        "market": "US",
        "execution_date": "2026-01-01",
        "strategy_id": "trend_animals_warm_to_hot/US/v2",
        "strategy_version": "v2",
        "formal_actions": [{"action": "BUY", "symbol": "AAA"}],
    }

    attributed = _attribute_actual_fills([
        actual,
    ], [
        {**fact, "report_sha256": "a" * 64},
        {**fact, "report_sha256": "b" * 64},
    ])

    assert attributed[0]["attribution_status"] == "ambiguous"
    assert attributed[0]["exclusion_reason"] == "multiple_opening_strategy_matches"


class FakeSyncClient:
    def __init__(self, fills: list[dict[str, object]]) -> None:
        self.fills = fills
        self.attributions: dict[str, dict[str, object]] = {}

    def fetch_fills(
        self, *, start: str, end: str,
        attributions_by_order: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        assert (start, end) == ("2026-01-01", "2026-01-03")
        self.attributions = {key: dict(value) for key, value in attributions_by_order.items()}
        normalized = []
        for fill in self.fills:
            fact = self.attributions.get(str(fill["order_id"]), {})
            normalized.append({**fill, **fact})
        return {"orders_seen": len({fill["order_id"] for fill in normalized}), "fills": normalized}


def normalized_fill(
    fill_id: str, order_id: str, *, source: str, side: str,
    filled_at: str, price: str,
) -> dict[str, object]:
    broker = "futu" if source == "simulation" else "tiger"
    account = "101" if source == "simulation" else "U1"
    return {
        "fill_id": fill_id,
        "order_id": order_id,
        "source": source,
        "source_id": f"{source}:{broker}:{account}",
        "broker": broker,
        "account_id": account,
        "market": "US",
        "symbol": "AAA",
        "currency": "USD",
        "side": side,
        "quantity": "1",
        "price": price,
        "fee": "0" if source == "simulation" else "0.1",
        "costs_complete": source == "actual",
        "filled_at": filled_at,
        "strategy_id": "",
        "strategy_version": "",
        "attribution_status": "outside_strategy",
        "exclusion_reason": "order_not_linked_to_frozen_strategy",
    }


def test_partial_actual_sell_is_attributed_and_round_closes_only_after_final_sell() -> None:
    strategy_id = "trend_animals_warm_to_hot/US/v4"
    frozen_reports = [
        {
            "market": "US",
            "execution_date": "2026-01-01",
            "strategy_id": strategy_id,
            "strategy_version": "v4",
            "report_sha256": "a" * 64,
            "formal_actions": [{"action": "BUY", "symbol": "AAA"}],
        },
        {
            "market": "US",
            "execution_date": "2026-01-02",
            "strategy_id": strategy_id,
            "strategy_version": "v4",
            "report_sha256": "b" * 64,
            "formal_actions": [{"action": "SELL_PARTIAL", "symbol": "AAA"}],
        },
        {
            "market": "US",
            "execution_date": "2026-01-03",
            "strategy_id": strategy_id,
            "strategy_version": "v4",
            "report_sha256": "c" * 64,
            "formal_actions": [{"action": "SELL_ALL", "symbol": "AAA"}],
        },
    ]
    actual_fills = [
        {
            **normalized_fill(
                "actual-buy", "actual-buy", source="actual", side="buy",
                filled_at="2026-01-01T10:00:00-05:00", price="10",
            ),
            "quantity": "10",
            "fee": "1",
        },
        {
            **normalized_fill(
                "actual-partial", "actual-partial", source="actual", side="sell",
                filled_at="2026-01-02T10:00:00-05:00", price="12",
            ),
            "quantity": "3",
            "fee": "0.3",
        },
        {
            **normalized_fill(
                "actual-final", "actual-final", source="actual", side="sell",
                filled_at="2026-01-03T10:00:00-05:00", price="11",
            ),
            "quantity": "7",
            "fee": "0.5",
        },
    ]
    strategy_versions = [{
        "market": "US", "strategy_id": strategy_id, "strategy_version": "v4",
    }]

    partial = _attribute_actual_fills(actual_fills[:2], frozen_reports)

    assert partial[-1]["strategy_id"] == strategy_id
    assert partial[-1]["strategy_version"] == "v4"
    assert partial[-1]["attribution_status"] == "attributed"
    partial_payload = build_trend_api_stats_payload(
        partial,
        strategy_versions=strategy_versions,
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T23:59:59+00:00",
    )
    assert partial_payload["rounds"] == []

    closed_payload = build_trend_api_stats_payload(
        _attribute_actual_fills(actual_fills, frozen_reports),
        strategy_versions=strategy_versions,
        generated_at="2026-01-04T00:00:00+00:00",
        statistics_cutoff_at="2026-01-03T23:59:59+00:00",
    )

    assert len(closed_payload["rounds"]) == 1
    assert closed_payload["rounds"][0]["fill_ids"] == [
        "actual-buy", "actual-partial", "actual-final",
    ]
    assert closed_payload["rounds"][0]["buy_quantity"] == "10"
    assert closed_payload["rounds"][0]["sell_quantity"] == "10"
    assert closed_payload["rounds"][0]["fees"] == "1.8"


def test_sync_uses_frozen_attribution_merges_idempotently_and_writes_canonical_artifact(
    tmp_path: Path,
) -> None:
    report = {
        "execution_date": "2026-01-01",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/US/v2",
            "strategy_version": "v2",
            "parameters": {
                "normal_cost_rate": "0.001",
                "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
            },
        },
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "AAA"}],
        },
    }
    report_path = tmp_path / "reports/trend_us_tiger/2026-01-01.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    (report_path.parent / "2025-12-31.json").write_text(json.dumps({
        "execution_date": "2025-12-31",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_snapshot": {"parameters": {}},
        "strategy_judgments": {"formal_actions": []},
    }), encoding="utf-8")
    report_sha = _report_hash(report)
    action_key = hashlib.sha256(b"US:2026-01-01:v2:AAA:buy").hexdigest()
    event_path = (
        tmp_path / "data/trend_review/ledgers/US/actions/2026-01-01"
        / action_key / "event.json"
    )
    event_path.parent.mkdir(parents=True)
    event_path.write_text(json.dumps({
        "market": "US", "date": "2026-01-01", "symbol": "AAA",
        "side": "buy", "status": "filled", "filled_qty": "1",
        "strategy_version": "v2", "report_sha256": report_sha,
        "order_ids": ["sim-buy"],
        "recorded_at": "2026-01-01T10:05:00-05:00",
    }), encoding="utf-8")
    simulation = FakeSyncClient([
        normalized_fill("sim-b", "sim-buy", source="simulation", side="buy", filled_at="2026-01-01T10:01:00-05:00", price="10"),
        normalized_fill("sim-s", "sim-sell", source="simulation", side="sell", filled_at="2026-01-02T10:01:00-05:00", price="12"),
    ])
    actual = FakeSyncClient([
        normalized_fill("actual-b", "actual-buy", source="actual", side="buy", filled_at="2026-01-01T10:02:00-05:00", price="10"),
        normalized_fill("actual-s", "actual-sell", source="actual", side="sell", filled_at="2026-01-02T10:02:00-05:00", price="11"),
    ])

    sync_args = {
        "data_dir": tmp_path / "data",
        "reports_dir": tmp_path / "reports",
        "futu_clients": {"US": simulation},
        "tiger_client": actual,
        "start": "2026-01-01",
        "end": "2026-01-03",
        "generated_at": "2026-01-03T12:00:00+00:00",
        "statistics_cutoff_at": "2026-01-03T12:00:00+00:00",
    }
    payload = sync_trend_api_stats(**sync_args)
    payload["sources"].append({
        "source": "actual",
        "source_id": "actual:eastmoney:eastmoney_main",
        "broker": "eastmoney",
        "account_id": "eastmoney_main",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-01-02T12:00:00+00:00",
        "status": "available",
    })
    write_trend_api_stats(tmp_path / "data", payload)

    payload = sync_trend_api_stats(**sync_args)

    assert simulation.attributions["sim-buy"]["report_sha256"] == report_sha
    assert len(payload["fills"]) == 4
    assert len(payload["rounds"]) == 2
    assert all(round_["attribution_status"] == "attributed" for round_ in payload["rounds"])
    assert any(source["broker"] == "eastmoney" for source in payload["sources"])
    assert load_trend_api_stats(tmp_path / "data") == payload
    assert (tmp_path / "data/latest/trend_api_stats.json").is_file()


class RecordingCycleClient:
    def __init__(self, *, account_id: str = "101", fail_once: bool = False) -> None:
        self.account_id = account_id
        self.fail_once = fail_once
        self.calls: list[dict[str, object]] = []

    def fetch_fills(
        self,
        *,
        start: str,
        end: str,
        attributions_by_order: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        self.calls.append({
            "start": start,
            "end": end,
            "attributions_by_order": dict(attributions_by_order),
        })
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("broker unavailable")
        return {"account_id": self.account_id, "orders_seen": 0, "fills": []}


def cycle_kwargs(
    tmp_path: Path,
    *,
    market: str,
    futu_client: RecordingCycleClient,
    tiger_client: RecordingCycleClient | None = None,
) -> dict[str, object]:
    return {
        "data_dir": tmp_path / "data",
        "reports_dir": tmp_path / "reports",
        "market": market,
        "as_of_date": "2026-08-08",
        "generated_at": "2026-08-09T08:00:00+08:00",
        "process_git_sha": "abc1234",
        "futu_client": futu_client,
        "tiger_client": tiger_client,
    }


def test_completed_cycle_is_not_recomputed_without_explicit_force(tmp_path: Path) -> None:
    client = RecordingCycleClient()
    kwargs = cycle_kwargs(tmp_path, market="CN", futu_client=client)

    first = run_trend_statistics_cycle(**kwargs)
    repeated = run_trend_statistics_cycle(**(kwargs | {"process_git_sha": "def5678"}))

    assert first["status"] == "completed"
    assert repeated["status"] == "already_completed"
    assert len(client.calls) == 1
    assert trend_statistics_cycle_path(
        tmp_path / "data", "CN", "2026-08-08"
    ).is_file()
    assert not (tmp_path / "data/trend_api_stats/missed").exists()


def test_force_requires_actor_and_reason(tmp_path: Path) -> None:
    kwargs = cycle_kwargs(tmp_path, market="CN", futu_client=RecordingCycleClient())
    run_trend_statistics_cycle(**kwargs)

    with pytest.raises(ValueError, match="force requires actor and reason"):
        run_trend_statistics_cycle(**kwargs, force=True)


def test_failed_cycle_retries_and_downtime_uses_one_range_request(tmp_path: Path) -> None:
    client = RecordingCycleClient(fail_once=True)
    kwargs = cycle_kwargs(
        tmp_path,
        market="US",
        futu_client=client,
        tiger_client=RecordingCycleClient(account_id="U1"),
    )

    assert run_trend_statistics_cycle(**kwargs)["status"] == "failed"
    assert run_trend_statistics_cycle(**kwargs)["status"] == "completed"
    assert len(client.calls) == 2
    assert client.calls[-1]["end"] == kwargs["as_of_date"]


def test_cycle_cutoffs_are_market_local_closes() -> None:
    assert trend_statistics_cutoff_at("CN", "2026-08-08") == "2026-08-08T15:00:00+08:00"
    assert trend_statistics_cutoff_at("HK", "2026-08-08") == "2026-08-08T16:00:00+08:00"
    assert trend_statistics_cutoff_at("US", "2026-08-08") == "2026-08-08T16:00:00-04:00"


def test_market_cycle_publications_are_order_independent(tmp_path: Path) -> None:
    us_kwargs = cycle_kwargs(
        tmp_path,
        market="US",
        futu_client=RecordingCycleClient(account_id="us-sim"),
        tiger_client=RecordingCycleClient(account_id="U1"),
    )
    assert run_trend_statistics_cycle(**us_kwargs)["status"] == "completed"

    cn_kwargs = cycle_kwargs(
        tmp_path,
        market="CN",
        futu_client=RecordingCycleClient(account_id="cn-sim"),
    )
    cn_result = run_trend_statistics_cycle(**cn_kwargs)

    assert cn_result["status"] == "completed"
    assert cn_result["statistics_cutoff_at"] == "2026-08-08T15:00:00+08:00"
    artifact = load_trend_api_stats(tmp_path / "data")
    assert artifact["statistics_cutoff_at"] == "2026-08-08T16:00:00-04:00"
    assert {source["market"] for source in artifact["sources"]} == {"CN", "US"}


def test_range_validation_failure_persists_retryable_cycle_state(tmp_path: Path) -> None:
    report_path = tmp_path / "reports/trend_a_share/2026-08-08.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("not json", encoding="utf-8")
    kwargs = cycle_kwargs(
        tmp_path,
        market="CN",
        futu_client=RecordingCycleClient(account_id="cn-sim"),
    )

    result = run_trend_statistics_cycle(**kwargs)

    assert result["status"] == "failed"
    state = json.loads(
        trend_statistics_cycle_path(tmp_path / "data", "CN", "2026-08-08").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "failed"
    assert "frozen trend report is unreadable" in state["reason"]


def test_partial_actual_artifact_catches_up_missing_simulation_from_reports(
    tmp_path: Path,
) -> None:
    report = {
        "execution_date": "2026-01-01",
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v1",
            "strategy_version": "v1",
            "parameters": {},
        },
        "strategy_judgments": {"formal_actions": []},
    }
    report_path = tmp_path / "reports/trend_a_share/2026-01-01.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    artifact = build_trend_api_stats_payload(
        [],
        strategy_versions=[],
        generated_at="2026-07-01T16:00:00+08:00",
        statistics_cutoff_at="2026-07-01T15:00:00+08:00",
    )
    artifact["sources"] = [{
        "source": "actual",
        "source_id": "actual:eastmoney:eastmoney_main",
        "broker": "eastmoney",
        "account_id": "eastmoney_main",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-07-01T15:00:00+08:00",
        "status": "available",
    }]
    write_trend_api_stats(tmp_path / "data", artifact)
    client = RecordingCycleClient(account_id="cn-sim")

    result = run_trend_statistics_cycle(
        **cycle_kwargs(tmp_path, market="CN", futu_client=client)
    )

    assert result["status"] == "completed"
    assert len(client.calls) == 1
    assert client.calls[0]["start"] == "2026-01-01"
    assert client.calls[0]["end"] == "2026-08-08"


def test_cycle_hashes_current_locked_snapshot_without_comparing_stale_sync_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = build_trend_api_stats_payload(
        [],
        strategy_versions=[],
        generated_at="2026-08-09T08:00:00+08:00",
        statistics_cutoff_at="2026-08-08T15:00:00+08:00",
    )
    artifact["sources"] = [{
        "source": "simulation",
        "source_id": "simulation:futu:cn-sim",
        "broker": "futu",
        "account_id": "cn-sim",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-08-08T15:00:00+08:00",
        "status": "available",
    }]
    write_trend_api_stats(tmp_path / "data", artifact)
    monkeypatch.setattr(
        trend_api_stats_module,
        "sync_trend_api_stats",
        lambda **_: {"stale": "concurrent writer result"},
    )

    result = run_trend_statistics_cycle(
        **cycle_kwargs(
            tmp_path, market="CN", futu_client=RecordingCycleClient(account_id="cn-sim")
        )
    )

    assert result["status"] == "completed"
    expected_sha = hashlib.sha256(
        (tmp_path / "data/latest/trend_api_stats.json").read_bytes()
    ).hexdigest()
    assert result["artifact_sha256"] == expected_sha
