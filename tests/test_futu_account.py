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
    map_snapshot_to_portfolio_inputs,
    sync_futu_portfolio,
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


def test_sync_futu_portfolio_replaces_old_futu_rows_and_preserves_other_brokers(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.MSFT",
                "stock_name": "Microsoft",
                "qty": "2",
                "cost_price": "300",
                "nominal_price": "410",
                "market_val": "820",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    symbols = {row["symbol"] for row in rows}
    assert "OLD" not in symbols
    assert {"AAPL", "MSFT", "USD_CASH"} <= symbols
    msft = next(row for row in rows if row["symbol"] == "MSFT")
    assert msft["brokers"] == "futu"
    assert msft["market_value_hkd"] == "6396.00"
    assert msft["portfolio_weight_hkd"] == "73.21%"
    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert aapl["brokers"] == "tiger"
    assert aapl["portfolio_weight_hkd"] == "17.86%"
    assert result.latest_path == tmp_path / "data/latest/portfolio.csv"
    assert result.snapshot_path == (
        tmp_path / "data/runs/2026-06-18/futu_account_snapshot.json"
    )
    assert result.portfolio_path == tmp_path / "data/runs/2026-06-18/portfolio.csv"
    assert result.report_path == tmp_path / "reports/futu_account/2026-06-18.md"
    assert read_portfolio(result.latest_path)[0]["symbol"] == "OLD"
    assert result.updated_latest is False

    snapshot_payload = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot_payload["accounts"][0]["acc_id"] == 111
    report = result.report_path.read_text(encoding="utf-8")
    assert "富途账户同步" in report
    assert "真实账户：1" in report
    assert "未更新 latest" in report


def test_sync_futu_portfolio_builds_live_only_portfolio_without_existing_portfolio(
    tmp_path: Path,
) -> None:
    missing_portfolio_path = tmp_path / "data/latest/portfolio.csv"
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.DRAM",
                "stock_name": "Roundhill Memory ETF",
                "qty": "300",
                "cost_price": "70",
                "nominal_price": "79",
                "market_val": "23700",
                "pl_val": "2700",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=missing_portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-25",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "DRAM"
    assert rows[0]["total_quantity"] == "300"
    assert rows[0]["fx_to_hkd"] == "7.85"
    assert missing_portfolio_path.exists() is False


def test_sync_futu_portfolio_deduplicates_unknown_zero_position_against_tiger_stock(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [hk_tiger_stock_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "HK.01688",
                "stock_name": "领益智造",
                "qty": "0",
                "cost_price": "0",
                "nominal_price": "9.71",
                "market_val": "0",
                "cost_value": "0",
                "pl_val": "-277.2",
                "currency": "HKD",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-29",
        update_latest=True,
    )

    rows = read_portfolio(result.portfolio_path)
    matching = [
        row for row in rows if row["market"] == "HK" and row["symbol"] == "01688"
    ]
    assert len(matching) == 1
    row = matching[0]
    assert row["asset_class"] == "stock"
    assert row["total_quantity"] == "2640"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"
    assert result.updated_latest is True
    latest_rows = read_portfolio(result.latest_path)
    assert (
        len(
            [
                row
                for row in latest_rows
                if row["market"] == "HK" and row["symbol"] == "01688"
            ]
        )
        == 1
    )


def test_sync_futu_portfolio_rebuilds_mixed_symbols_from_statement_details(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    mixed_vixy_row = {
        **old_futu_row(),
        "asset_class": "etf",
        "symbol": "VIXY",
        "name": "VIXY",
        "total_quantity": "200",
        "market_value": "4842.8",
        "market_value_hkd": "38015.98",
        "brokers": "futu;tiger",
        "accounts": "futu_main;tiger_main",
        "fx_to_hkd": "7.85",
    }
    write_portfolio(portfolio_path, [mixed_vixy_row])
    write_csv(
        tmp_path / "data/runs/2026-05/extracted_positions.csv",
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "futu_main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "165",
                "cost_price": "",
                "last_price": "24.41",
                "market_value": "4027.65",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-05-tiger",
                "broker": "tiger",
                "account_alias": "tiger_main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "35",
                "cost_price": "28.7408571",
                "last_price": "23.29",
                "market_value": "815.15",
                "cost_value": "1005.9299985",
                "unrealized_pnl": "-190.78",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.VIXY",
                "stock_name": "VIXY",
                "qty": "100",
                "cost_price": "42.616",
                "nominal_price": "21.925",
                "market_val": "2192.5",
                "pl_val": "-2069.07",
                "currency": "USD",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["brokers"] == "futu;tiger"
    assert rows[0]["total_quantity"] == "135"
    assert rows[0]["market_value"] == "3007.65"
    assert read_portfolio(portfolio_path)[0]["total_quantity"] == "200"
    with (tmp_path / "data/runs/2026-06-19/extracted_positions.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        detail_rows = list(csv.DictReader(handle))
    assert [
        {
            "broker": row["broker"],
            "symbol": row["symbol"],
            "quantity": row["quantity"],
        }
        for row in detail_rows
    ] == [
        {"broker": "tiger", "symbol": "VIXY", "quantity": "35"},
        {"broker": "futu", "symbol": "VIXY", "quantity": "100"},
    ]


def test_sync_futu_portfolio_cash_count_uses_expanded_currency_balances(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [tiger_row()])
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
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    assert result.cash_count == 2
    rows = read_portfolio(result.portfolio_path)
    assert {"HKD_CASH", "USD_CASH"} <= {row["symbol"] for row in rows}
    report = result.report_path.read_text(encoding="utf-8")
    assert "现金币种：2" in report


def test_sync_futu_portfolio_reconciles_unmapped_futu_total_assets(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "total_assets": "1000",
                "cash": "-100",
                "hk_cash": "-100",
                "hk_avl_withdrawal_cash": "0",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "HK.00700",
                "stock_name": "Tencent",
                "qty": "1",
                "cost_price": "200",
                "nominal_price": "200",
                "market_val": "200",
                "pl_val": "0",
                "currency": "HKD",
                "stock_type": "STOCK",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    adjustment = next(row for row in rows if row["symbol"] == "FUTU_UNMAPPED_ASSETS")
    assert adjustment["market"] == "CASH"
    assert adjustment["asset_class"] == "cash"
    assert adjustment["name"] == "富途未明细账户资产"
    assert adjustment["market_value_hkd"] == "900.00"
    assert adjustment["brokers"] == "futu"
    assert adjustment["accounts"] == "futu_111"
    total_hkd = sum(Decimal(row["market_value_hkd"]) for row in rows)
    assert total_hkd == Decimal("2560.00")


def test_sync_futu_portfolio_updates_latest_only_when_requested(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=True,
    )

    rows = read_portfolio(result.latest_path)
    assert {row["symbol"] for row in rows} == {"AAPL", "USD_CASH"}
    assert result.updated_latest is True


def test_sync_futu_portfolio_promotes_latest_through_atomic_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    calls: list[tuple[Path, set[str]]] = []

    def spy_write_latest(path: Path, rows: list[dict[str, str]]) -> None:
        rows_list = list(rows)
        calls.append((path, {row["symbol"] for row in rows_list}))
        write_portfolio(path, rows_list)

    monkeypatch.setattr(
        futu_account_module,
        "_write_latest_portfolio_atomic",
        spy_write_latest,
        raising=False,
    )
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=True,
    )

    assert calls == [(result.latest_path, {"AAPL", "USD_CASH"})]
    assert result.updated_latest is True


def test_sync_futu_portfolio_clears_stale_preserved_overweight_flag(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    stale_overweight = {**tiger_row(), "risk_flag": "overweight"}
    write_portfolio(portfolio_path, [old_futu_row(), stale_overweight])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "2000",
                "available_cash": "2000",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert aapl["portfolio_weight_hkd"] == "9.09%"
    assert aapl["risk_flag"] == "normal"


def test_sync_futu_portfolio_blocks_latest_when_required_fields_are_malformed(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [old_futu_row(), tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
            }
        ],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "US.BROKEN",
                "stock_name": "Broken",
                "qty": "bad",
                "currency": "USD",
                "stock_type": "STOCK",
            }
        ],
    )

    with pytest.raises(FutuAccountError) as exc_info:
        sync_futu_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-18",
            update_latest=True,
        )

    assert exc_info.value.error_type == "blocking_data_error"
    assert read_portfolio(portfolio_path)[0]["symbol"] == "OLD"
    run_dir = tmp_path / "data/runs/2026-06-18"
    snapshot_path = run_dir / "futu_account_snapshot.json"
    merged_portfolio_path = run_dir / "portfolio.csv"
    report_path = tmp_path / "reports/futu_account/2026-06-18.md"
    assert snapshot_path.exists()
    assert merged_portfolio_path.exists()
    assert report_path.exists()
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot_payload["position_records"][0]["qty"] == "bad"
    report = report_path.read_text(encoding="utf-8")
    assert "富途账户同步" in report
    assert "数据检查：需要复核" in report
    assert "未更新 latest" in report


@pytest.mark.parametrize("market_value_hkd", ["", "not-a-number"])
def test_sync_futu_portfolio_marks_all_rows_data_check_when_preserved_hkd_value_is_invalid(
    tmp_path: Path,
    market_value_hkd: str,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    malformed_tiger = {**tiger_row(), "market_value_hkd": market_value_hkd}
    write_portfolio(portfolio_path, [malformed_tiger])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "USD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=False,
    )

    rows = read_portfolio(result.portfolio_path)
    assert {row["portfolio_weight_hkd"] for row in rows} == {""}
    assert {row["risk_flag"] for row in rows} == {"data_check"}


def test_sync_futu_portfolio_preserves_malformed_cash_value_from_valid_hkd_value(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    malformed_cash = {**usd_cash_row(), "market_value": "bad"}
    write_portfolio(portfolio_path, [malformed_cash])
    snapshot = client_snapshot_from_records(
        cash_records=[
            {
                "_account_alias": "futu_111",
                "currency": "HKD",
                "cash": "100",
                "available_cash": "90",
            }
        ],
        position_records=[],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-18",
        update_latest=True,
    )

    rows = read_portfolio(result.portfolio_path)
    usd_cash = next(row for row in rows if row["symbol"] == "USD_CASH")
    assert usd_cash["market_value_hkd"] == "7800.00"
    assert usd_cash["market_value_hkd"] != "0.00"
    assert {row["portfolio_weight_hkd"] for row in rows} == {""}
    assert {row["risk_flag"] for row in rows} == {"data_check"}
    assert result.updated_latest is True
    latest_usd_cash = next(
        row for row in read_portfolio(result.latest_path) if row["symbol"] == "USD_CASH"
    )
    assert latest_usd_cash["market_value_hkd"] == "7800.00"


def test_sync_futu_portfolio_blocks_mixed_futu_broker_rows(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    mixed_row = {**old_futu_row(), "brokers": "futu;tiger"}
    write_portfolio(portfolio_path, [mixed_row, tiger_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[],
    )

    with pytest.raises(FutuAccountError) as exc_info:
        sync_futu_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-18",
            update_latest=True,
        )

    assert exc_info.value.error_type == "mixed_futu_broker_row"
    assert read_portfolio(portfolio_path)[0]["brokers"] == "futu;tiger"
