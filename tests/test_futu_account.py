from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader import futu_account as futu_account_module
from open_trader.futu_account import (
    FutuAccountClient,
    FutuAccountError,
    build_futu_account_candidate,
    map_snapshot_to_portfolio_inputs,
)
from open_trader.models import AssetClass, Market
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeSecTradeContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.accinfo_calls: list[dict[str, object]] = []
        self.position_calls: list[dict[str, object]] = []

    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 111,
                        "acc_index": 0,
                        "trd_env": "REAL",
                        "acc_type": "CASH",
                        "acc_status": "ACTIVE",
                        "card_num": "12345678",
                    },
                    {
                        "acc_id": 333,
                        "acc_index": 2,
                        "trd_env": "REAL",
                        "acc_type": "MARGIN",
                        "acc_status": "DISABLED",
                        "card_num": "87654321",
                    },
                    {
                        "acc_id": 222,
                        "acc_index": 1,
                        "trd_env": "SIMULATE",
                        "acc_type": "SECURITY",
                        "card_num": "SIM",
                    },
                ]
            ),
        )

    def accinfo_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        currency: str,
        asset_category: str,
    ) -> tuple[int, FakeDataFrame]:
        self.accinfo_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "currency": currency,
                "asset_category": asset_category,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "currency": "USD",
                        "cash": "100.25",
                        "available_cash": "88.50",
                        "total_assets": "1500",
                    }
                ]
            ),
        )

    def position_list_query(
        self,
        *,
        trd_env: str,
        acc_id: int,
        acc_index: int,
        refresh_cache: bool,
        position_market: str,
        asset_category: str,
        currency: str,
    ) -> tuple[int, FakeDataFrame]:
        self.position_calls.append(
            {
                "trd_env": trd_env,
                "acc_id": acc_id,
                "acc_index": acc_index,
                "refresh_cache": refresh_cache,
                "position_market": position_market,
                "asset_category": asset_category,
                "currency": currency,
            }
        )
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "code": "US.MSFT",
                        "stock_name": "Microsoft",
                        "qty": "2",
                        "cost_price": "300",
                        "nominal_price": "410",
                        "market_val": "820",
                        "pl_val": "220",
                        "currency": "USD",
                        "stock_type": "STOCK",
                    }
                ]
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeNoRealAccountContext(FakeSecTradeContext):
    def get_acc_list(self) -> tuple[int, FakeDataFrame]:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 222,
                        "acc_index": 1,
                        "trd_env": "SIMULATE",
                        "acc_type": "SECURITY",
                    }
                ]
            ),
        )


class FakeFailingAccountContext(FakeSecTradeContext):
    def get_acc_list(self) -> tuple[int, str]:
        return -1, "account query failed"


def test_futu_account_client_fetches_only_real_accounts() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )

    snapshot = client.fetch_snapshot()

    assert len(snapshot.accounts) == 1
    assert snapshot.accounts[0].acc_id == 111
    assert snapshot.accounts[0].acc_index == 0
    assert snapshot.accounts[0].trd_env == "REAL"
    assert snapshot.accounts[0].account_alias == "futu_111"
    assert snapshot.cash_records[0]["cash"] == "100.25"
    assert snapshot.position_records[0]["code"] == "US.MSFT"
    assert client.context.accinfo_calls == [
        {
            "trd_env": "REAL",
            "acc_id": 111,
            "acc_index": 0,
            "refresh_cache": True,
            "currency": "HKD",
            "asset_category": "N/A",
        }
    ]
    assert client.context.position_calls == [
        {
            "trd_env": "REAL",
            "acc_id": 111,
            "acc_index": 0,
            "refresh_cache": True,
            "position_market": "N/A",
            "asset_category": "N/A",
            "currency": "USD",
        }
    ]


def test_futu_account_client_fails_fast_when_opend_unreachable() -> None:
    called = False

    def context_factory(*, host: str, port: int) -> FakeSecTradeContext:
        nonlocal called
        called = True
        return FakeSecTradeContext(host=host, port=port)

    with pytest.raises(FutuAccountError) as exc_info:
        FutuAccountClient(
            host="127.0.0.1",
            port=11111,
            context_factory=context_factory,
            connectivity_checker=lambda host, port: False,
        )

    assert exc_info.value.error_type == "opend_unreachable"
    assert "Futu OpenD is not reachable" in str(exc_info.value)
    assert called is False


def test_futu_account_client_reports_no_real_accounts() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeNoRealAccountContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "no_real_accounts"
    assert "no REAL Futu securities accounts found" in str(exc_info.value)


def test_futu_account_client_classifies_account_query_failure() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeFailingAccountContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "account_query_failed"
    assert "account query failed" in str(exc_info.value)


def test_futu_account_client_close_closes_context() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )

    client.close()

    assert client.context.closed is True


def test_map_snapshot_to_portfolio_inputs_maps_positions_and_cash() -> None:
    client = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeSecTradeContext,
        connectivity_checker=lambda host, port: True,
    )
    snapshot = client.fetch_snapshot()

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    position = positions[0]
    assert position.statement_id == "2026-06-18-futu-live"
    assert position.broker == "futu"
    assert position.account_alias == "futu_111"
    assert position.market == Market.US
    assert position.asset_class == AssetClass.STOCK
    assert position.symbol == "MSFT"
    assert position.name == "Microsoft"
    assert position.currency == "USD"
    assert position.quantity == Decimal("2")
    assert position.cost_price == Decimal("300")
    assert position.last_price == Decimal("410")
    assert position.market_value == Decimal("820")
    assert position.cost_value == Decimal("600")
    assert position.unrealized_pnl == Decimal("220")
    assert position.confidence == "high"
    assert "Futu live account" in position.notes

    assert len(cash_balances) == 1
    cash = cash_balances[0]
    assert cash.statement_id == "2026-06-18-futu-live"
    assert cash.broker == "futu"
    assert cash.account_alias == "futu_111"
    assert cash.currency == "USD"
    assert cash.cash_balance == Decimal("100.25")
    assert cash.available_balance == Decimal("88.50")
    assert cash.confidence == "high"


def test_map_snapshot_expands_futu_accinfo_per_currency_cash() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "-114156.26",
                "hk_cash": "-125409.59",
                "hk_avl_withdrawal_cash": "-125409.59",
                "us_cash": "1435.8",
                "us_avl_withdrawal_cash": "1400.50",
                "cn_cash": "0",
                "cn_avl_withdrawal_cash": "0",
                "au_cash": "N/A",
                "au_avl_withdrawal_cash": "N/A",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert positions == []
    assert blocking_errors == []
    assert [cash.currency for cash in cash_balances] == ["HKD", "USD"]
    cash_by_currency = {cash.currency: cash for cash in cash_balances}
    assert cash_by_currency["HKD"].cash_balance == Decimal("-125409.59")
    assert cash_by_currency["USD"].cash_balance == Decimal("1435.8")
    assert cash_by_currency["USD"].available_balance == Decimal("1400.50")


def test_map_snapshot_uses_per_currency_net_cash_power_for_buying_power() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "58.8",
                "hk_cash": "58.8",
                "hk_avl_withdrawal_cash": "0",
                "hkd_net_cash_power": "455581.60",
                "us_cash": "0",
                "us_avl_withdrawal_cash": "0",
                "usd_net_cash_power": "50564.72",
            }
        ],
        position_records=[],
    )

    _, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-07-16",
    )

    assert blocking_errors == []
    cash_by_currency = {cash.currency: cash for cash in cash_balances}
    assert cash_by_currency["HKD"].cash_balance == Decimal("58.8")
    assert cash_by_currency["HKD"].available_balance == Decimal("455581.60")
    assert cash_by_currency["USD"].cash_balance == Decimal("0")
    assert cash_by_currency["USD"].available_balance == Decimal("50564.72")


def test_map_snapshot_preserves_simple_fake_cash_record_compatibility() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100.25",
                "available_cash": "88.50",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert positions == []
    assert blocking_errors == []
    assert len(cash_balances) == 1
    assert cash_balances[0].currency == "USD"
    assert cash_balances[0].cash_balance == Decimal("100.25")
    assert cash_balances[0].available_balance == Decimal("88.50")


def test_map_snapshot_skips_empty_na_cash_record() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_222",
                "currency": "N/A",
                "cash": "0",
                "available_funds": "N/A",
                "hk_cash": "0",
                "hk_avl_withdrawal_cash": "0",
                "us_cash": "N/A",
                "us_avl_withdrawal_cash": "N/A",
                "cn_cash": "N/A",
                "cn_avl_withdrawal_cash": "N/A",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert positions == []
    assert cash_balances == []
    assert blocking_errors == []


def test_map_snapshot_marks_malformed_required_position_fields_low_confidence() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "100",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.BROKEN",
                "stock_name": "Broken",
                "qty": "not-a-number",
                "market_val": "100",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert cash_balances[0].cash_balance == Decimal("100")
    assert len(positions) == 1
    assert positions[0].symbol == "BROKEN"
    assert positions[0].quantity == Decimal("0")
    assert positions[0].market_value is None
    assert positions[0].confidence == "low"
    assert blocking_errors == [
        "position US.BROKEN has invalid required field qty='not-a-number'"
    ]


def test_map_snapshot_blocks_invalid_cost_basis() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.COST",
                "stock_name": "Cost Broken",
                "qty": "3",
                "market_val": "120",
                "cost_price": "not-a-number",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert cash_balances == []
    assert len(positions) == 1
    assert positions[0].symbol == "COST"
    assert positions[0].market_value == Decimal("120")
    assert positions[0].cost_value is None
    assert positions[0].confidence == "low"
    assert blocking_errors == [
        "position US.COST has invalid required field cost_value=None"
    ]


def test_map_snapshot_blocks_invalid_market_value() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.BADVAL",
                "stock_name": "Bad Value",
                "qty": "3",
                "market_val": "not-a-number",
                "cost_value": "90",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert cash_balances == []
    assert len(positions) == 1
    assert positions[0].symbol == "BADVAL"
    assert positions[0].market_value is None
    assert positions[0].cost_value == Decimal("90")
    assert positions[0].confidence == "low"
    assert blocking_errors == [
        "position US.BADVAL has invalid required field market_val='not-a-number'"
    ]


@pytest.mark.parametrize("record", [{"code": " "}, {"stock_name": "No Code"}])
def test_map_snapshot_blocks_blank_or_missing_code(record: dict[str, object]) -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "stock_name": "No Code",
                "qty": "3",
                "market_val": "120",
                "cost_value": "90",
                "currency": "USD",
                "stock_type": "STOCK",
                **record,
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert cash_balances == []
    assert len(positions) == 1
    assert positions[0].symbol == ""
    assert positions[0].confidence == "low"
    assert blocking_errors == [
        f"position has invalid required field code={record.get('code')!r}"
    ]


def test_map_snapshot_accepts_empty_positions() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "5000",
                "available_cash": "4500",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-18",
    )

    assert positions == []
    assert cash_balances[0].symbol == "HKD_CASH"
    assert blocking_errors == []


def client_snapshot_from_records(
    *,
    cash_records: list[dict[str, object]],
    position_records: list[dict[str, object]],
) -> object:
    from open_trader.futu_account import FutuAccount, FutuAccountSnapshot

    return FutuAccountSnapshot(
        accounts=[
            FutuAccount(
                acc_id=111,
                acc_index=0,
                trd_env="REAL",
                acc_type="CASH",
                account_alias="futu_111",
            )
        ],
        cash_records=cash_records,
        position_records=position_records,
    )


def test_build_futu_account_candidate_normalizes_complete_snapshot() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100.25",
                "available_cash": "88.50",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": f"US.TEST{index}",
                "stock_name": f"Test {index}",
                "stock_type": "STOCK",
                "currency": "USD",
                "qty": "1",
                "cost_price": "10",
                "nominal_price": "11",
                "market_val": "11",
            }
            for index in range(14)
        ],
    )

    candidate = build_futu_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
        fallback_fx_to_hkd={"USD": Decimal("7.8123")},
    )

    assert len(candidate.positions) == 14
    assert candidate.summary == {
        "account_count": 1,
        "position_count": 14,
        "cash_count": 1,
        "account_aliases": ["futu_***"],
    }
    assert candidate.data_as_of == "2026-07-30T11:56:54+08:00"
    assert candidate.positions[0].asset_class is AssetClass.STOCK
    assert candidate.cash[0].currency == "USD"
    assert candidate.fx_rates == (
        {"account_alias": "futu_***", "currency": "USD", "rate_to_hkd": "7.8123"},
    )


def test_build_futu_account_candidate_complete_zero_positions_is_valid() -> None:
    candidate = build_futu_account_candidate(
        client_snapshot_from_records(cash_records=[], position_records=[]),
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
        fallback_fx_to_hkd={},
    )

    assert candidate.positions == ()
    assert candidate.summary["position_count"] == 0


def test_build_futu_account_candidate_rejects_missing_real_account() -> None:
    snapshot = client_snapshot_from_records(cash_records=[], position_records=[])
    snapshot = snapshot.__class__(
        accounts=[],
        cash_records=snapshot.cash_records,
        position_records=snapshot.position_records,
    )

    with pytest.raises(FutuAccountError) as exc_info:
        build_futu_account_candidate(
            snapshot,
            run_date="2026-07-30",
            data_as_of="2026-07-30T11:56:54+08:00",
            fallback_fx_to_hkd={},
        )

    assert exc_info.value.error_type == "no_real_accounts"


def test_build_futu_account_candidate_rejects_malformed_or_duplicate_identity() -> None:
    malformed = client_snapshot_from_records(
        cash_records=[],
        position_records=[{"_account_alias": "futu_111", "code": "US.MSFT"}],
    )
    duplicate = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.MSFT",
                "qty": "1",
                "market_val": "11",
                "cost_price": "10",
            },
            {
                "_account_alias": "futu_111",
                "code": "US.MSFT",
                "qty": "1",
                "market_val": "11",
                "cost_price": "10",
            },
        ],
    )

    for snapshot, error_type in ((malformed, "blocking_data_error"), (duplicate, "duplicate_identity")):
        with pytest.raises(FutuAccountError) as exc_info:
            build_futu_account_candidate(
                snapshot,
                run_date="2026-07-30",
                data_as_of="2026-07-30T11:56:54+08:00",
                fallback_fx_to_hkd={"USD": Decimal("7.8123")},
            )

        assert exc_info.value.error_type == error_type


def test_build_futu_account_candidate_rejects_malformed_total_assets() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "0",
                "total_assets": "bad",
            }
        ],
        position_records=[],
    )

    with pytest.raises(FutuAccountError) as exc_info:
        build_futu_account_candidate(
            snapshot,
            run_date="2026-07-30",
            data_as_of="2026-07-30T11:56:54+08:00",
            fallback_fx_to_hkd={},
        )

    assert exc_info.value.error_type == "blocking_data_error"


def test_build_futu_account_candidate_rejects_raw_record_account_alias() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "123456789",
                "currency": "HKD",
                "cash": "10",
                "total_assets": "100",
            }
        ],
        position_records=[],
    )

    with pytest.raises(FutuAccountError) as exc_info:
        build_futu_account_candidate(
            snapshot,
            run_date="2026-07-30",
            data_as_of="2026-07-30T11:56:54+08:00",
            fallback_fx_to_hkd={},
        )

    assert exc_info.value.error_type == "account_query_failed"
    assert "123456789" not in str(exc_info.value)


def test_build_futu_account_candidate_normalizes_self_consistent_raw_account_alias() -> None:
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_acc_id": "123456789",
                "_account_alias": "123456789",
                "currency": "USD",
                "cash": "10",
            }
        ],
        position_records=[
            {
                "_acc_id": "123456789",
                "_account_alias": "123456789",
                "code": "US.MSFT",
                "qty": "1",
                "cost_price": "10",
                "market_val": "11",
            }
        ],
    )
    snapshot = snapshot.__class__(
        accounts=[
            futu_account_module.FutuAccount(
                acc_id=123456789,
                acc_index=0,
                trd_env="REAL",
                acc_type="CASH",
                account_alias="123456789",
            )
        ],
        cash_records=snapshot.cash_records,
        position_records=snapshot.position_records,
    )

    candidate = build_futu_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
        fallback_fx_to_hkd={"USD": Decimal("7.8123")},
    )

    assert {position.account_alias for position in candidate.positions} == {
        "futu_*****6789"
    }
    assert {cash.account_alias for cash in candidate.cash} == {"futu_*****6789"}
    assert candidate.fx_rates[0]["account_alias"] == "futu_*****6789"
    assert candidate.summary["account_aliases"] == ["futu_*****6789"]
    assert "123456789" not in repr(candidate)


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def read_portfolio(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def old_futu_row() -> dict[str, str]:
    return {
        "sort_group": "2",
        "market": "US",
        "asset_class": "stock",
        "symbol": "OLD",
        "name": "Old Futu",
        "currency": "USD",
        "total_quantity": "1",
        "avg_cost_price": "1.00",
        "last_price": "1.00",
        "market_value": "1",
        "cost_value": "1",
        "unrealized_pnl": "0.00",
        "unrealized_pnl_pct": "0.00%",
        "fx_source": "external_month_end_static",
        "fx_date": "2026-06-30",
        "fx_to_hkd": "7.8",
        "market_value_hkd": "7.80",
        "cost_value_hkd": "7.80",
        "portfolio_weight_hkd": "0.01%",
        "brokers": "futu",
        "accounts": "old",
        "ai_eligible": "true",
        "analysis_symbol": "OLD",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }


def tiger_row() -> dict[str, str]:
    return {
        "sort_group": "2",
        "market": "US",
        "asset_class": "stock",
        "symbol": "AAPL",
        "name": "Apple",
        "currency": "USD",
        "total_quantity": "1",
        "avg_cost_price": "100.00",
        "last_price": "200.00",
        "market_value": "200",
        "cost_value": "100",
        "unrealized_pnl": "100.00",
        "unrealized_pnl_pct": "100.00%",
        "fx_source": "external_month_end_static",
        "fx_date": "2026-06-30",
        "fx_to_hkd": "7.8",
        "market_value_hkd": "1560.00",
        "cost_value_hkd": "780.00",
        "portfolio_weight_hkd": "100.00%",
        "brokers": "tiger",
        "accounts": "tiger_main",
        "ai_eligible": "true",
        "analysis_symbol": "AAPL",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }


def hk_tiger_stock_row() -> dict[str, str]:
    return {
        **tiger_row(),
        "sort_group": "1",
        "market": "HK",
        "asset_class": "stock",
        "symbol": "01688",
        "name": "领益智造",
        "currency": "HKD",
        "total_quantity": "2640",
        "avg_cost_price": "10.18",
        "last_price": "9.71",
        "market_value": "25634.4",
        "cost_value": "26875.2",
        "unrealized_pnl": "-1240.80",
        "unrealized_pnl_pct": "-4.62%",
        "fx_to_hkd": "1",
        "market_value_hkd": "25634.40",
        "cost_value_hkd": "26875.20",
        "portfolio_weight_hkd": "100.00%",
        "brokers": "tiger",
        "accounts": "tiger_5683",
        "analysis_symbol": "01688",
        "notes": "Tiger live account position",
    }


def usd_cash_row() -> dict[str, str]:
    return {
        "sort_group": "6",
        "market": "CASH",
        "asset_class": "cash",
        "symbol": "USD_CASH",
        "name": "USD Cash",
        "currency": "USD",
        "total_quantity": "1",
        "avg_cost_price": "",
        "last_price": "",
        "market_value": "1000",
        "cost_value": "",
        "unrealized_pnl": "",
        "unrealized_pnl_pct": "",
        "fx_source": "external_month_end_static",
        "fx_date": "2026-06-30",
        "fx_to_hkd": "7.8",
        "market_value_hkd": "7800.00",
        "cost_value_hkd": "",
        "portfolio_weight_hkd": "100.00%",
        "brokers": "tiger",
        "accounts": "tiger_main",
        "ai_eligible": "false",
        "analysis_symbol": "",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }


def tiger_unmapped_assets_row() -> dict[str, str]:
    return {
        **usd_cash_row(),
        "symbol": "TIGER_UNMAPPED_ASSETS",
        "name": "Tiger unmapped assets",
        "currency": "HKD",
        "market_value": "900",
        "cost_value": "900",
        "fx_to_hkd": "1",
        "market_value_hkd": "900.00",
        "cost_value_hkd": "900.00",
        "brokers": "tiger",
        "accounts": "tiger_main",
        "notes": "Tiger account_total reconciliation for locked funds or fund assets not returned as positions",
    }
