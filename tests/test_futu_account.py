from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_account import (
    FutuAccountClient,
    FutuAccountError,
    map_snapshot_to_portfolio_inputs,
)
from open_trader.models import AssetClass, Market


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
                        "card_num": "12345678",
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
