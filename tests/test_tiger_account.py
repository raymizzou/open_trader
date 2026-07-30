from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from decimal import Decimal

import pytest
from open_trader import tiger_account as tiger_account_module

from open_trader.models import AssetClass, Market
from open_trader.tiger_account import (
    TigerAccount,
    TigerAccountConfig,
    TigerAccountError,
    TigerAccountClient,
    TigerAccountSnapshot,
    build_tiger_account_candidate,
    map_snapshot_to_portfolio_inputs,
    load_tiger_account_config,
    mask_account_id,
)
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_portfolio(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def base_portfolio_row(**overrides: object) -> dict[str, str]:
    row: dict[str, str] = {
        "sort_group": "2",
        "market": "US",
        "asset_class": "stock",
        "symbol": "OLD",
        "name": "Old Tiger",
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
        "fx_to_hkd": "7.80",
        "market_value_hkd": "7.80",
        "cost_value_hkd": "7.80",
        "portfolio_weight_hkd": "0.01%",
        "brokers": "tiger",
        "accounts": "old",
        "ai_eligible": "true",
        "analysis_symbol": "OLD",
        "risk_flag": "normal",
        "confidence": "high",
        "notes": "",
    }
    row.update({key: str(value) for key, value in overrides.items()})
    return row


def futu_hk_unknown_detail_row() -> dict[str, str]:
    return {
        "statement_id": "2026-06-29-futu-live",
        "broker": "futu",
        "account_alias": "futu_111",
        "market": "HK",
        "asset_class": "unknown",
        "symbol": "01688",
        "name": "领益智造",
        "currency": "HKD",
        "quantity": "0",
        "cost_price": "0",
        "last_price": "9.71",
        "market_value": "0",
        "cost_value": "0",
        "unrealized_pnl": "-277.2",
        "confidence": "high",
        "notes": "Futu live account position",
    }


def test_mask_account_id_masks_short_and_long_values() -> None:
    assert mask_account_id("123456789") == "*****6789"
    assert mask_account_id("DU575569") == "***5569"
    assert mask_account_id("123") == "***"
    assert mask_account_id("") == ""


def test_load_config_prefers_cli_account_and_environment_private_key_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "tiger.pem"
    key_path.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TIGEROPEN_TIGER_ID", "tiger-123")
    monkeypatch.setenv("TIGEROPEN_ACCOUNT", "env-account")
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("TIGEROPEN_SECRET_KEY", "secret-value")
    monkeypatch.setenv("TIGEROPEN_TOKEN", "token-value")

    config = load_tiger_account_config(
        config_dir=tmp_path / "missing-config-dir",
        account="cli-account",
        sandbox=True,
    )

    assert config == TigerAccountConfig(
        tiger_id="tiger-123",
        account="cli-account",
        private_key_path=key_path,
        private_key=None,
        secret_key="secret-value",
        token="token-value",
        sandbox=True,
        config_dir=tmp_path / "missing-config-dir",
    )


def test_load_config_reads_official_properties_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".tigeropen"
    config_dir.mkdir()
    properties_path = config_dir / "tiger_openapi_config.properties"
    properties_path.write_text(
        "\n".join(
            [
                "tiger_id=file-tiger-id",
                "account=file-account",
                "private_key_pk1=-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----",
            ]
        ),
        encoding="utf-8",
    )

    config = load_tiger_account_config(
        config_dir=config_dir,
        account=None,
        sandbox=False,
    )

    assert config.tiger_id == "file-tiger-id"
    assert config.account == "file-account"
    assert config.private_key == (
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    )
    assert config.private_key_path is None
    assert config.config_dir == config_dir


def test_load_config_environment_private_key_overrides_properties_private_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / ".tigeropen"
    config_dir.mkdir()
    config_dir.joinpath("tiger_openapi_config.properties").write_text(
        "\n".join(
            [
                "tiger_id=file-tiger-id",
                "account=file-account",
                "private_key_pk1=file-pk1-key",
                "private_key=file-private-key",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY", "env-private-key")

    config = load_tiger_account_config(
        config_dir=config_dir,
        account=None,
        sandbox=False,
    )

    assert config.private_key == "env-private-key"
    assert config.private_key_path is None


def test_load_config_prefers_private_key_path_over_raw_private_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "tiger.pem"
    key_path.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nfile-key\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TIGEROPEN_TIGER_ID", "tiger-123")
    monkeypatch.setenv("TIGEROPEN_ACCOUNT", "env-account")
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY", "inline-key")

    config = load_tiger_account_config(config_dir=tmp_path, account=None, sandbox=False)

    assert config.private_key is None
    assert config.private_key_path == key_path


def test_load_config_requires_identity_and_private_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIGEROPEN_TIGER_ID", raising=False)
    monkeypatch.delenv("TIGEROPEN_ACCOUNT", raising=False)
    monkeypatch.delenv("TIGEROPEN_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("TIGEROPEN_PRIVATE_KEY", raising=False)

    with pytest.raises(TigerAccountError) as exc_info:
        load_tiger_account_config(config_dir=tmp_path, account=None, sandbox=False)

    assert exc_info.value.error_type == "config_missing"
    assert "Tiger OpenAPI configuration is incomplete" in str(exc_info.value)


def test_load_config_rejects_missing_private_key_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIGEROPEN_TIGER_ID", "tiger-123")
    monkeypatch.setenv("TIGEROPEN_ACCOUNT", "env-account")
    missing_path = tmp_path / "missing-key.pem"
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY_PATH", str(missing_path))

    with pytest.raises(TigerAccountError) as exc_info:
        load_tiger_account_config(config_dir=tmp_path, account=None, sandbox=False)

    assert exc_info.value.error_type == "config_invalid"
    assert "Tiger OpenAPI private key path is invalid" in str(exc_info.value)
    assert str(missing_path) in str(exc_info.value)


def test_tiger_account_config_repr_hides_sensitive_values() -> None:
    config = TigerAccountConfig(
        tiger_id="tiger-123",
        account="123456789",
        private_key_path=Path("unused"),
        private_key="private-key-value",
        secret_key="secret-key-value",
        token="token-value",
        sandbox=True,
        config_dir=Path("unused-config"),
    )

    dumped = repr(config)

    assert "private-key-value" not in dumped
    assert "secret-key-value" not in dumped
    assert "token-value" not in dumped
    assert "private_key=" not in dumped
    assert "secret_key=" not in dumped
    assert "token=" not in dumped


class FakeContract:
    def __init__(
        self,
        *,
        symbol: str,
        sec_type: str = "STK",
        currency: str = "USD",
        market: str = "US",
        name: str = "Microsoft",
    ) -> None:
        self.symbol = symbol
        self.sec_type = sec_type
        self.currency = currency
        self.market = market
        self.name = name


class FakePosition:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakeCurrencyAsset:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakeSegment:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakePrimeAssets:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency_assets={
                    "USD": FakeCurrencyAsset(
                        currency="USD",
                        cash_balance="100.25",
                        cash_available_for_trade="88.50",
                        gross_position_value="820",
                    ),
                    "HKD": FakeCurrencyAsset(
                        currency="HKD",
                        cash_balance="0",
                        cash_available_for_trade="0",
                        gross_position_value="0",
                    ),
                },
            )
        }


class FakePrimeAssetsWithUsefulZeroCash:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency_assets={
                    "USD": FakeCurrencyAsset(
                        currency="USD",
                        cash_balance="100.25",
                        cash_available_for_trade="88.50",
                        gross_position_value="820",
                    ),
                    "HKD": FakeCurrencyAsset(
                        currency="HKD",
                        cash_balance="0",
                        cash_available_for_trade="0",
                        gross_position_value="100.00",
                    ),
                    "CNY": FakeCurrencyAsset(
                        currency="CNY",
                        cash_balance="0",
                        cash_available_for_trade="0",
                        gross_position_value="0",
                    ),
                },
            )
        }


class FakePrimeAssetsWithMetrics:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency="USD",
                cash_balance="-3980.76",
                cash_available_for_trade="62320.21",
                equity_with_loan="91322.91",
                net_liquidation="29548.11",
                currency_assets={
                    "USD": FakeCurrencyAsset(
                        currency="USD",
                        cash_balance="-37207.24",
                        cash_available_for_trade="-37207.24",
                        gross_position_value="0",
                        forex_rate="1",
                    ),
                    "HKD": FakeCurrencyAsset(
                        currency="HKD",
                        cash_balance="260484.1",
                        cash_available_for_trade="260484.1",
                        gross_position_value="0",
                        forex_rate="0.1275578",
                    ),
                },
            )
        }


class FakePrimeAssetsWithCommodityCash:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency_assets={
                    "USD": FakeCurrencyAsset(
                        currency="USD",
                        cash_balance="100.25",
                        cash_available_for_trade="88.50",
                        gross_position_value="820",
                    ),
                },
            ),
            "C": FakeSegment(
                category="C",
                currency_assets={
                    "USD": FakeCurrencyAsset(
                        currency="USD",
                        cash_balance="9999.99",
                        cash_available_for_trade="9999.99",
                        gross_position_value="0",
                    ),
                },
            ),
        }


class FakePrimeAssetsWithWithdrawalOnly:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency_assets={
                    "JPY": FakeCurrencyAsset(
                        currency="JPY",
                        cash_balance="0",
                        gross_position_value="0",
                        cash_available_for_withdrawal="77.75",
                    ),
                },
            )
        }


class FakeTradeClient:
    def __init__(self, client_config: object) -> None:
        self.client_config = client_config
        self.position_calls: list[dict[str, object]] = []
        self.prime_asset_calls: list[dict[str, object]] = []

    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return [
            type(
                "Profile",
                (),
                {
                    "account": "123456789",
                    "accountType": "STANDARD",
                    "capability": "RegTMargin",
                    "status": "Funded",
                },
            )(),
            type(
                "Profile",
                (),
                {
                    "account": "20190000000000000",
                    "accountType": "PAPER",
                    "capability": "Cash",
                    "status": "Closed",
                },
            )(),
        ]

    def get_positions(self, **kwargs: object) -> list[FakePosition]:
        self.position_calls.append(kwargs)
        if kwargs.get("sec_type") == "FUND":
            return []
        return [
            FakePosition(
                account="123456789",
                contract=FakeContract(symbol="MSFT"),
                position_qty="2",
                average_cost="300",
                market_price="410",
                market_value="820",
                unrealized_pnl="220",
            )
        ]

    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssets:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssets()


class PagedTransactions(FakeTradeClient):
    def __init__(self, client_config: object) -> None:
        super().__init__(client_config)
        self.transaction_calls: list[dict[str, object]] = []
        self.order_calls: list[dict[str, object]] = []

    @staticmethod
    def _transaction(source_id: str, order_id: int) -> object:
        return type(
            "Transaction",
            (),
            {
                "id": source_id,
                "order_id": order_id,
                "action": "BUY",
                "filled_quantity": "1",
                "filled_price": "123.45",
                "transacted_at": 1784246400000,
                "contract": FakeContract(
                    symbol="MSFT", currency="USD", market="US"
                ),
            },
        )()

    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        if kwargs["page_token"] is None:
            return type(
                "TransactionsResponse",
                (),
                {
                    "result": [self._transaction("9001-1", 9001)],
                    "next_page_token": "next",
                },
            )()
        return type(
            "TransactionsResponse",
            (),
            {
                "result": [self._transaction("9002-1", 9002)],
                "next_page_token": None,
            },
        )()

    def get_order(self, **kwargs: object) -> object:
        self.order_calls.append(kwargs)
        return type("Order", (), {"commission": "1.25", "charges": []})()


class ListTransactions(PagedTransactions):
    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        return [self._transaction("9001-1", 9001)]


class MultipleFillsOneOrder(ListTransactions):
    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        return [
            self._transaction("9001-1", 9001),
            self._transaction("9001-2", 9001),
        ]


class SdkPagedTransactions(PagedTransactions):
    def _page(self) -> list[object]:
        return [self._transaction(f"{index}-1", index) for index in range(100)]

    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        if kwargs["page_token"] is None:
            return self._page()
        if kwargs["page_token"] == "":
            return type(
                "TransactionsResponse",
                (),
                {"result": self._page(), "next_page_token": "next"},
            )()
        return type(
            "TransactionsResponse",
            (),
            {
                "result": [self._transaction("100-1", 100)],
                "next_page_token": None,
            },
        )()


class HkTransactions(ListTransactions):
    @staticmethod
    def _transaction(source_id: str, order_id: int) -> object:
        transaction = PagedTransactions._transaction(source_id, order_id)
        transaction.contract = FakeContract(
            symbol="00700", currency="HKD", market="HK"
        )
        return transaction


class SymbolRequiredTransactions(PagedTransactions):
    def __init__(self, client_config: object) -> None:
        super().__init__(client_config)
        self.filled_order_calls: list[dict[str, object]] = []

    def get_filled_orders(self, **kwargs: object) -> list[object]:
        self.filled_order_calls.append(kwargs)
        return [type("Order", (), {"id": 9001})()]

    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        if "order_id" not in kwargs:
            raise RuntimeError("biz param error(field 'symbol' cannot be empty)")
        return [self._transaction("9001-1", 9001)]


class EmptyScopedTransactions(SymbolRequiredTransactions):
    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        if "order_id" not in kwargs:
            raise RuntimeError("biz param error(field 'symbol' cannot be empty)")
        return []


class HundredFilledOrders(SymbolRequiredTransactions):
    def get_filled_orders(self, **kwargs: object) -> list[object]:
        self.filled_order_calls.append(kwargs)
        return [type("Order", (), {"id": index})() for index in range(100)]


class PagedScopedTransactions(SymbolRequiredTransactions):
    def get_transactions(self, **kwargs: object) -> object:
        self.transaction_calls.append(kwargs)
        if "order_id" not in kwargs:
            raise RuntimeError("biz param error(field 'symbol' cannot be empty)")
        if kwargs.get("page_token") is None:
            return [
                self._transaction(f"9001-{index}", 9001)
                for index in range(100)
            ]
        return type(
            "TransactionsResponse",
            (),
            {
                "result": [self._transaction("9001-100", 9001)],
                "next_page_token": None,
            },
        )()


class IncompletePagedScopedTransactions(PagedScopedTransactions):
    def get_transactions(self, **kwargs: object) -> object:
        if "order_id" not in kwargs:
            return super().get_transactions(**kwargs)
        self.transaction_calls.append(kwargs)
        if kwargs.get("page_token") is None:
            return [
                self._transaction(f"9001-{index}", 9001)
                for index in range(100)
            ]
        return None


class FakeStockAndFundTradeClient(FakeTradeClient):
    def get_positions(self, **kwargs: object) -> list[FakePosition]:
        self.position_calls.append(kwargs)
        if kwargs.get("sec_type") == "FUND":
            return [
                FakePosition(
                    account="123456789",
                    contract=FakeContract(
                        symbol="HK0000951506.HKD",
                        sec_type="FUND",
                        currency="HKD",
                        market="HK",
                        name="华泰港元货币市场基金A",
                    ),
                    position_qty="437187.6069",
                    average_cost="1.10",
                    market_price="1.1032",
                    market_value="482305.3679",
                    unrealized_pnl="0",
                )
            ]
        return [
            FakePosition(
                account="123456789",
                contract=FakeContract(symbol="MSFT"),
                position_qty="2",
                average_cost="300",
                market_price="410",
                market_value="820",
                unrealized_pnl="220",
            )
        ]


class FakePrimeAssetUsefulCashTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithUsefulZeroCash:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithUsefulZeroCash()


class FakePrimeAssetMetricsTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithMetrics:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithMetrics()


class FakePrimeAssetCommodityCashTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithCommodityCash:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithCommodityCash()


class FakePrimeAssetWithdrawalOnlyTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithWithdrawalOnly:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithWithdrawalOnly()


class FakePrimeAssetsWithTradeBlankAndWithdrawalValue:
    def __init__(self) -> None:
        self.account = "123456789"
        self.segments = {
            "S": FakeSegment(
                category="S",
                currency_assets={
                    "JPY": FakeCurrencyAsset(
                        currency="JPY",
                        cash_balance="88",
                        cash_available_for_trade="",
                        cash_available_for_withdrawal="44.44",
                        gross_position_value="0",
                    ),
                },
            )
        }


class FakePrimeAssetTradeBlankTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithTradeBlankAndWithdrawalValue:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithTradeBlankAndWithdrawalValue()


class FakeEmptyPrimeAssetsTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> object:
        self.prime_asset_calls.append(kwargs)
        return type("EmptyPrimeAssets", (), {"segments": {}})()


class FakeCompleteZeroPrimeAssetsTradeClient(FakeTradeClient):
    def get_positions(self, **kwargs: object) -> list[FakePosition]:
        self.position_calls.append(kwargs)
        return []

    def get_prime_assets(self, **kwargs: object) -> object:
        self.prime_asset_calls.append(kwargs)
        return type(
            "CompleteZeroPrimeAssets",
            (),
            {"segments": {"S": FakeSegment(category="S", currency_assets={})}},
        )()


class FakeGlobalTradeClient(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return [
            type(
                "Profile",
                (),
                {
                    "account": "U575569",
                    "accountType": "GLOBAL",
                    "capability": "Cash",
                    "status": "Funded",
                },
            )()
        ]

    def get_assets(self, **kwargs: object) -> list[object]:
        return [
            type(
                "PortfolioAccount",
                (),
                {
                    "account": "U575569",
                    "market_values": {
                        "USD": type(
                            "MarketValue",
                            (),
                            {
                                "currency": "USD",
                                "cash_balance": "55.50",
                                "cash_available_for_trade": "33.33",
                                "net_liquidation": "900.00",
                            },
                        )()
                    },
                },
            )()
        ]


class FakeOpenStatusTradeClient(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return [
            type(
                "Profile",
                (),
                {
                    "account": "123456789",
                    "accountType": "STANDARD",
                    "capability": "RegTMargin",
                    "status": "Open",
                },
            )()
        ]


class FakeGlobalTradeClientBlankTradeField(FakeGlobalTradeClient):
    def get_assets(self, **kwargs: object) -> list[object]:
        return [
            type(
                "PortfolioAccount",
                (),
                {
                    "account": "U575569",
                    "market_values": {
                        "USD": type(
                            "MarketValue",
                            (),
                            {
                                "currency": "USD",
                                "cash_balance": "55.50",
                                "cash_available_for_trade": "",
                                "cash_available_for_withdrawal": "33.33",
                                "net_liquidation": "900.00",
                            },
                        )()
                    },
                },
            )()
        ]


class FakeGlobalTradeClientZeroCashAndPositiveNetLiquidation(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return [
            type(
                "Profile",
                (),
                {
                    "account": "U575569",
                    "accountType": "GLOBAL",
                    "capability": "Cash",
                    "status": "FUNDED",
                },
            )()
        ]

    def get_assets(self, **kwargs: object) -> list[object]:
        return [
            type(
                "PortfolioAccount",
                (),
                {
                    "account": "U575569",
                    "market_values": {
                        "USD": type(
                            "MarketValue",
                            (),
                            {
                                "currency": "USD",
                                "cash_balance": "0",
                                "cash_available_for_trade": "",
                                "cash_available_for_withdrawal": "",
                                "net_liquidation": "900.00",
                            },
                        )()
                    },
                },
            )()
        ]


class FakeEmptyTradeClient(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return []


class FakeAccountQueryFailedTradeClient(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        raise RuntimeError("Tiger account query failed: secret=SECRET-AAA-123456789")


class FakePositionQueryFailedTradeClient(FakeTradeClient):
    def get_positions(self, **kwargs: object) -> list[object]:
        raise RuntimeError("Tiger position query failed: secret=SECRET-POS-123456789")


class FakeGetAssetsQueryFailedTradeClient(FakeTradeClient):
    def get_managed_accounts(self, account: str | None = None) -> list[object]:
        return [
            type(
                "Profile",
                (),
                {
                    "account": "U575569",
                    "accountType": "GLOBAL",
                    "capability": "Cash",
                    "status": "Funded",
                },
            )()
        ]

    def get_prime_assets(self, **kwargs: object) -> list[object]:
        raise RuntimeError("Tiger asset query failed: secret=SECRET-ASSET-123456789")

    def get_assets(self, **kwargs: object) -> list[object]:
        raise RuntimeError("Tiger asset query failed: secret=SECRET-ASSET-123456789")

    def get_positions(self, **kwargs: object) -> list[object]:
        return []


def tiger_config(account: str = "123456789") -> TigerAccountConfig:
    return TigerAccountConfig(
        tiger_id="tiger-123",
        account=account,
        private_key_path=None,
        private_key="-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        secret_key=None,
        token=None,
        sandbox=False,
        config_dir=Path("unused"),
    )


def test_tiger_account_client_fetches_standard_account_snapshot() -> None:
    client = TigerAccountClient(config=tiger_config(), trade_client_factory=FakeTradeClient)

    snapshot = client.fetch_snapshot()

    assert snapshot.accounts == [
        TigerAccount(
            account="123456789",
            account_alias="tiger_6789",
            account_type="STANDARD",
            capability="REGTMARGIN",
            status="FUNDED",
            asset_method="get_prime_assets",
        )
    ]
    assert len(snapshot.position_records) == 1
    assert snapshot.position_records[0]["account_alias"] == "tiger_6789"
    assert snapshot.position_records[0]["symbol"] == "MSFT"
    assert len(snapshot.cash_records) == 1
    assert snapshot.cash_records[0]["currency"] == "USD"
    assert snapshot.cash_records[0]["cash_balance"] == "100.25"
    assert client.trade_client.position_calls == [
        {"account": "123456789", "sec_type": "STK"},
        {"account": "123456789", "sec_type": "FUND"},
    ]
    assert client.trade_client.prime_asset_calls == [{"account": "123456789"}]


def test_tiger_fetch_transactions_paginates_and_normalizes() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=PagedTransactions,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert [fill.source_id for fill in fills] == ["9001-1", "9002-1"]
    assert all(fill.market is Market.US for fill in fills)
    assert all(fill.fees == Decimal("1.25") for fill in fills)
    assert client.trade_client.transaction_calls == [
        {
            "account": "123456789",
            "since_date": "20260717",
            "to_date": "20260717",
            "limit": 100,
            "page_token": None,
        },
        {
            "account": "123456789",
            "since_date": "20260717",
            "to_date": "20260717",
            "limit": 100,
            "page_token": "next",
        },
    ]
    assert client.trade_client.order_calls == [
        {"order_id": 9001, "show_charges": True},
        {"order_id": 9002, "show_charges": True},
    ]


def test_tiger_fetch_transactions_accepts_sdk_list_response() -> None:
    fills = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=ListTransactions,
    ).fetch_actual_fills("2026-07-17", "2026-07-17")

    assert [fill.source_id for fill in fills] == ["9001-1"]


def test_tiger_fetch_transactions_falls_back_to_filled_orders_when_symbol_is_required() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=SymbolRequiredTransactions,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert [fill.source_id for fill in fills] == ["9001-1"]
    assert client.trade_client.filled_order_calls == [
        {
            "account": "123456789",
            "start_time": "2026-07-17 00:00:00",
            "end_time": "2026-07-18 00:00:00",
            "limit": 100,
        }
    ]
    assert client.trade_client.transaction_calls[-1] == {
        "account": "123456789",
        "order_id": 9001,
        "limit": 100,
        "page_token": None,
    }


def test_tiger_fallback_rejects_filled_order_without_transactions() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=EmptyScopedTransactions,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert exc_info.value.error_type == "transaction_query_failed"


def test_tiger_fallback_rejects_daily_filled_order_limit() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=HundredFilledOrders,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert exc_info.value.error_type == "transaction_query_failed"


def test_tiger_fallback_paginates_all_transactions_for_each_order() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=PagedScopedTransactions,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert len(fills) == 101
    assert len({fill.source_id for fill in fills}) == 101
    assert client.trade_client.transaction_calls[-2:] == [
        {
            "account": "123456789",
            "order_id": 9001,
            "limit": 100,
            "page_token": None,
        },
        {
            "account": "123456789",
            "order_id": 9001,
            "limit": 100,
            "page_token": "",
        },
    ]


def test_tiger_fallback_rejects_unprovable_order_transaction_continuation() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=IncompletePagedScopedTransactions,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert exc_info.value.error_type == "transaction_query_failed"


def test_tiger_fallback_queries_filled_orders_one_calendar_day_at_a_time() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=SymbolRequiredTransactions,
    )

    client.fetch_actual_fills("2026-07-17", "2026-07-18")

    assert client.trade_client.filled_order_calls == [
        {
            "account": "123456789",
            "start_time": "2026-07-17 00:00:00",
            "end_time": "2026-07-18 00:00:00",
            "limit": 100,
        },
        {
            "account": "123456789",
            "start_time": "2026-07-18 00:00:00",
            "end_time": "2026-07-19 00:00:00",
            "limit": 100,
        },
    ]


def test_tiger_multiple_fills_do_not_guess_order_fee_allocation() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=MultipleFillsOneOrder,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert [fill.fees for fill in fills] == [None, None]
    assert client.trade_client.order_calls == [
        {"order_id": 9001, "show_charges": True}
    ]


def test_tiger_sdk_list_response_at_limit_continues_with_response_pagination() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=SdkPagedTransactions,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert len(fills) == 101
    assert len({fill.source_id for fill in fills}) == 101
    assert [call["page_token"] for call in client.trade_client.transaction_calls] == [
        None,
        "",
        "next",
    ]


def test_tiger_actual_fills_ignore_non_us_transactions() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=HkTransactions,
    )

    fills = client.fetch_actual_fills("2026-07-17", "2026-07-17")

    assert fills == []
    assert client.trade_client.order_calls == []


def test_tiger_account_client_fetches_stock_and_fund_positions() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakeStockAndFundTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert client.trade_client.position_calls == [
        {"account": "123456789", "sec_type": "STK"},
        {"account": "123456789", "sec_type": "FUND"},
    ]
    assert [row["symbol"] for row in snapshot.position_records] == [
        "MSFT",
        "HK0000951506.HKD",
    ]


def test_tiger_account_client_accepts_open_status_case_insensitively() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakeOpenStatusTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.accounts == [
        TigerAccount(
            account="123456789",
            account_alias="tiger_6789",
            account_type="STANDARD",
            capability="REGTMARGIN",
            status="OPEN",
            asset_method="get_prime_assets",
        )
    ]


def test_tiger_account_client_uses_get_assets_for_global_account() -> None:
    client = TigerAccountClient(
        config=tiger_config(account="U575569"),
        trade_client_factory=FakeGlobalTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.accounts[0].account_type == "GLOBAL"
    assert snapshot.accounts[0].asset_method == "get_assets"
    assert snapshot.cash_records == [
        {
            "account": "U575569",
            "account_alias": "tiger_5569",
            "currency": "USD",
            "cash_balance": "55.50",
            "available_balance": "33.33",
            "gross_position_value": "900.00",
            "source": "get_assets",
        }
    ]


def test_tiger_account_client_uses_withdrawal_when_global_trade_field_blank() -> None:
    client = TigerAccountClient(
        config=tiger_config(account="U575569"),
        trade_client_factory=FakeGlobalTradeClientBlankTradeField,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.cash_records == [
        {
            "account": "U575569",
            "account_alias": "tiger_5569",
            "currency": "USD",
            "cash_balance": "55.50",
            "available_balance": "33.33",
            "gross_position_value": "900.00",
            "source": "get_assets",
        }
    ]


def test_tiger_account_client_global_zero_cash_row_keeps_net_liquidation_as_gross_position_value() -> None:
    client = TigerAccountClient(
        config=tiger_config(account="U575569"),
        trade_client_factory=FakeGlobalTradeClientZeroCashAndPositiveNetLiquidation,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.cash_records == [
        {
            "account": "U575569",
            "account_alias": "tiger_5569",
            "currency": "USD",
            "cash_balance": "0",
            "available_balance": None,
            "gross_position_value": "900.00",
            "source": "get_assets",
        }
    ]

    _, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert len(cash_balances) == 1
    assert cash_balances[0].cash_balance == Decimal("0")
    assert cash_balances[0].available_balance is None
    assert blocking_errors == []


def test_tiger_account_client_reports_no_matching_accounts() -> None:
    client = TigerAccountClient(config=tiger_config(), trade_client_factory=FakeEmptyTradeClient)

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "no_matching_accounts"
    assert "no active Tiger accounts matched" in str(exc_info.value)
    assert "*****6789" in str(exc_info.value)
    assert "123456789" not in str(exc_info.value)


def test_tiger_account_client_masks_raw_text_in_account_query_errors() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakeAccountQueryFailedTradeClient,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "account_query_failed"
    assert "failed to query Tiger managed accounts" in str(exc_info.value)
    assert "SECRET-AAA-123456789" not in str(exc_info.value)
    assert "123456789" not in str(exc_info.value)


def test_tiger_account_client_supports_positional_factory() -> None:
    calls: list[dict[str, object]] = []

    def positional_factory(config: object) -> object:
        calls.append({"config_account": getattr(config, "account", "")})
        return FakeTradeClient(config)

    client = TigerAccountClient(config=tiger_config(), trade_client_factory=positional_factory)

    snapshot = client.fetch_snapshot()

    assert snapshot.accounts[0].account == "123456789"
    assert calls == [{"config_account": "123456789"}]


def test_tiger_account_client_supports_keyword_factory() -> None:
    calls: list[dict[str, object]] = []

    def keyword_factory(*, client_config: object) -> object:
        calls.append({"config_account": getattr(client_config, "account", "")})
        return FakeTradeClient(client_config)

    client = TigerAccountClient(config=tiger_config(), trade_client_factory=keyword_factory)
    snapshot = client.fetch_snapshot()

    assert snapshot.accounts[0].account == "123456789"
    assert calls == [{"config_account": "123456789"}]


def test_tiger_account_client_does_not_retry_positional_factory_on_type_error() -> None:
    calls: list[dict[str, object]] = []

    def broken_factory(config: object) -> object:
        calls.append({"config_account": getattr(config, "account", "")})
        raise TypeError("factory failed")

    with pytest.raises(TigerAccountError) as exc_info:
        TigerAccountClient(config=tiger_config(), trade_client_factory=broken_factory)

    assert exc_info.value.error_type == "config_invalid"
    assert "failed to initialize Tiger TradeClient" in str(exc_info.value)
    assert len(calls) == 1


def test_tiger_account_client_hides_raw_error_text_from_trade_client_initialization() -> None:
    secret_text = "secret=SECRET-INIT-123456789"

    def secret_factory(_: object) -> None:
        raise RuntimeError(
            f"cannot initialize trade client because {secret_text} and account=123456789"
        )

    with pytest.raises(TigerAccountError) as exc_info:
        TigerAccountClient(config=tiger_config(), trade_client_factory=secret_factory)

    assert exc_info.value.error_type == "config_invalid"
    assert "failed to initialize Tiger TradeClient" in str(exc_info.value)
    assert secret_text not in str(exc_info.value)
    assert "123456789" not in str(exc_info.value)


def test_tiger_account_client_keeps_zero_cash_row_if_other_balance_is_positive() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePrimeAssetUsefulCashTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert len(snapshot.cash_records) == 2
    currencies = {record["currency"] for record in snapshot.cash_records}
    assert currencies == {"USD", "HKD"}
    for record in snapshot.cash_records:
        if record["currency"] == "HKD":
            assert record["cash_balance"] == "0"
            assert record["gross_position_value"] == "100.00"


def test_tiger_account_client_captures_prime_cash_metrics_and_live_fx() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePrimeAssetMetricsTradeClient,
    )

    snapshot = client.fetch_snapshot()

    total = next(
        row for row in snapshot.cash_records if row.get("record_type") == "account_total"
    )
    assert total["cash_balance"] == "-3980.76"
    assert total["cash_available_for_trade"] == "62320.21"
    assert Decimal(str(total["fx_to_hkd"])) == (
        Decimal("1") / Decimal("0.1275578")
    )
    cash_by_currency = {
        str(row["currency"]): row
        for row in snapshot.cash_records
        if row.get("record_type") != "account_total"
    }
    assert Decimal(str(cash_by_currency["USD"]["fx_to_hkd"])) == (
        Decimal("1") / Decimal("0.1275578")
    )
    assert cash_by_currency["HKD"]["fx_to_hkd"] == "1"


def test_tiger_account_client_prime_asset_ignores_commodity_segment_cash() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePrimeAssetCommodityCashTradeClient,
    )

    snapshot = client.fetch_snapshot()
    _, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert snapshot.cash_records == [
        {
            "account": "123456789",
            "account_alias": "tiger_6789",
            "currency": "USD",
            "cash_balance": "100.25",
            "available_balance": "88.50",
            "gross_position_value": "820",
            "source": "get_prime_assets",
        }
    ]
    assert len(cash_balances) == 1
    assert cash_balances[0].cash_balance == Decimal("100.25")


def test_tiger_account_client_masks_raw_text_in_position_query_errors() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePositionQueryFailedTradeClient,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "position_query_failed"
    assert "failed to query Tiger account positions" in str(exc_info.value)
    assert "SECRET-POS-123456789" not in str(exc_info.value)
    assert "123456789" not in str(exc_info.value)


def test_tiger_account_client_masks_raw_text_in_asset_query_errors() -> None:
    client = TigerAccountClient(
        config=tiger_config(account="U575569"),
        trade_client_factory=FakeGetAssetsQueryFailedTradeClient,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "asset_query_failed"
    assert "failed to query Tiger assets" in str(exc_info.value)
    assert "SECRET-ASSET-123456789" not in str(exc_info.value)
    assert "U575569" not in str(exc_info.value)


def test_tiger_account_client_keeps_withdrawal_only_prime_asset_row() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePrimeAssetWithdrawalOnlyTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.cash_records == [
        {
            "account": "123456789",
            "account_alias": "tiger_6789",
            "currency": "JPY",
            "cash_balance": "0",
            "available_balance": "77.75",
            "gross_position_value": "0",
            "source": "get_prime_assets",
        }
    ]


def test_tiger_account_client_prime_asset_falls_back_to_withdrawal_when_trade_is_blank() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakePrimeAssetTradeBlankTradeClient,
    )

    snapshot = client.fetch_snapshot()

    assert snapshot.cash_records == [
        {
            "account": "123456789",
            "account_alias": "tiger_6789",
            "currency": "JPY",
            "cash_balance": "88",
            "available_balance": "44.44",
            "gross_position_value": "0",
            "source": "get_prime_assets",
        }
    ]


def test_tiger_account_client_rejects_empty_prime_asset_response() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakeEmptyPrimeAssetsTradeClient,
    )

    with pytest.raises(TigerAccountError) as exc_info:
        client.fetch_snapshot()

    assert exc_info.value.error_type == "asset_query_failed"
    assert "123456789" not in str(exc_info.value)


def test_tiger_account_client_accepts_explicit_complete_zero_assets() -> None:
    client = TigerAccountClient(
        config=tiger_config(),
        trade_client_factory=FakeCompleteZeroPrimeAssetsTradeClient,
    )

    snapshot = client.fetch_snapshot()
    candidate = build_tiger_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
    )

    assert snapshot.cash_records == []
    assert candidate.summary["position_count"] == 0
    assert candidate.summary["cash_count"] == 0


def test_default_factory_reports_tigeropen_missing_when_sdk_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in (
        "tigeropen.trade.trade_client",
        "tigeropen.trade",
        "tigeropen",
    ):
        monkeypatch.setitem(sys.modules, module_name, None)

    with pytest.raises(TigerAccountError) as exc_info:
        TigerAccountClient(config=tiger_config())

    assert exc_info.value.error_type == "tigeropen_missing"


def tiger_snapshot_from_records(
    *,
    cash_records: list[dict[str, object]],
    position_records: list[dict[str, object]],
) -> TigerAccountSnapshot:
    return TigerAccountSnapshot(
        accounts=[
            TigerAccount(
                account="123456789",
                account_alias="tiger_6789",
                account_type="STANDARD",
                capability="RegTMargin",
                status="FUNDED",
                asset_method="get_prime_assets",
            )
        ],
        cash_records=cash_records,
        position_records=position_records,
    )


def test_build_tiger_account_candidate_normalizes_reconciliation_and_live_fx() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "100",
                "available_balance": "80",
                "fx_to_hkd": "7.84",
            },
            {
                "record_type": "account_total",
                "account_alias": "tiger_6789",
                "currency": "USD",
                "account_total": "1200",
                "fx_to_hkd": "7.84",
            },
        ],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "2",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
            }
        ],
    )

    candidate = build_tiger_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
    )

    assert [position.symbol for position in candidate.positions] == [
        "MSFT",
        "TIGER_UNMAPPED_ASSETS",
    ]
    assert candidate.summary == {
        "account_count": 1,
        "position_count": 2,
        "cash_count": 1,
        "account_aliases": ["tiger_6789"],
    }
    assert candidate.fx_rates == (
        {"account_alias": "tiger_6789", "currency": "USD", "rate_to_hkd": "7.84"},
    )
    assert all("account" not in item for item in candidate.fx_rates)
    assert "123456789" not in repr(candidate)


def test_build_tiger_account_candidate_complete_zero_positions_is_valid() -> None:
    candidate = build_tiger_account_candidate(
        tiger_snapshot_from_records(cash_records=[], position_records=[]),
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
    )

    assert candidate.positions == ()
    assert candidate.summary["position_count"] == 0


def test_build_tiger_account_candidate_rejects_malformed_or_duplicate_identity() -> None:
    malformed = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[{"account_alias": "tiger_6789", "symbol": "MSFT"}],
    )
    duplicate = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "0",
                "available_balance": "0",
                "fx_to_hkd": "7.84",
            }
        ],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "market_value": "11",
            },
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "market_value": "11",
            },
        ],
    )

    for snapshot, error_type in ((malformed, "blocking_data_error"), (duplicate, "duplicate_identity")):
        with pytest.raises(TigerAccountError) as exc_info:
            build_tiger_account_candidate(
                snapshot,
                run_date="2026-07-30",
                data_as_of="2026-07-30T11:56:54+08:00",
            )

        assert exc_info.value.error_type == error_type


def test_build_tiger_account_candidate_rejects_malformed_account_total() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "record_type": "account_total",
                "account_alias": "tiger_6789",
                "currency": "USD",
                "account_total": "bad",
                "fx_to_hkd": "7.84",
            }
        ],
        position_records=[],
    )

    with pytest.raises(TigerAccountError) as exc_info:
        build_tiger_account_candidate(
            snapshot,
            run_date="2026-07-30",
            data_as_of="2026-07-30T11:56:54+08:00",
        )

    assert exc_info.value.error_type == "blocking_data_error"


def test_build_tiger_account_candidate_normalizes_raw_record_account_alias() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "123456789",
                "currency": "USD",
                "cash_balance": "10",
                "available_balance": "10",
                "fx_to_hkd": "7.84",
            },
            {
                "record_type": "account_total",
                "account_alias": "123456789",
                "currency": "USD",
                "account_total": "100",
                "fx_to_hkd": "7.84",
            },
        ],
        position_records=[],
    )

    candidate = build_tiger_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
    )

    assert {position.account_alias for position in candidate.positions} == {"tiger_6789"}
    assert {cash.account_alias for cash in candidate.cash} == {"tiger_6789"}
    assert candidate.fx_rates[0]["account_alias"] == "tiger_6789"
    assert candidate.summary["account_aliases"] == ["tiger_6789"]
    assert "123456789" not in repr(candidate)


def test_build_tiger_account_candidate_normalizes_self_consistent_raw_account_alias() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account": "123456789",
                "account_alias": "123456789",
                "currency": "USD",
                "cash_balance": "10",
                "available_balance": "10",
                "fx_to_hkd": "7.84",
            },
            {
                "record_type": "account_total",
                "account": "123456789",
                "account_alias": "123456789",
                "currency": "USD",
                "account_total": "100",
                "fx_to_hkd": "7.84",
            },
        ],
        position_records=[
            {
                "account": "123456789",
                "account_alias": "123456789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "market_value": "11",
            }
        ],
    )
    snapshot = TigerAccountSnapshot(
        accounts=[
            TigerAccount(
                account="123456789",
                account_alias="123456789",
                account_type="STANDARD",
                capability="RegTMargin",
                status="FUNDED",
                asset_method="get_prime_assets",
            )
        ],
        cash_records=snapshot.cash_records,
        position_records=snapshot.position_records,
    )

    candidate = build_tiger_account_candidate(
        snapshot,
        run_date="2026-07-30",
        data_as_of="2026-07-30T11:56:54+08:00",
    )

    assert {position.account_alias for position in candidate.positions} == {"tiger_6789"}
    assert {cash.account_alias for cash in candidate.cash} == {"tiger_6789"}
    assert candidate.fx_rates[0]["account_alias"] == "tiger_6789"
    assert candidate.summary["account_aliases"] == ["tiger_6789"]
    assert "123456789" not in repr(candidate)


def test_map_snapshot_to_portfolio_inputs_maps_positions_and_cash() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "100.25",
                "available_balance": "88.50",
                "fx_to_hkd": "7.85",
                "source": "get_prime_assets",
            }
        ],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "2",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    position = positions[0]
    assert position.statement_id == "2026-06-19-tiger-live"
    assert position.broker == "tiger"
    assert position.account_alias == "tiger_6789"
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
    assert "Tiger live account" in position.notes

    assert len(cash_balances) == 1
    cash = cash_balances[0]
    assert cash.statement_id == "2026-06-19-tiger-live"
    assert cash.broker == "tiger"
    assert cash.account_alias == "tiger_6789"
    assert cash.currency == "USD"
    assert cash.cash_balance == Decimal("100.25")
    assert cash.available_balance == Decimal("88.50")
    assert cash.confidence == "high"


@pytest.mark.parametrize(
    ("name", "expected_asset_class"),
    [
        ("华泰港元货币市场基金A", AssetClass.MONEY_MARKET_FUND),
        ("环球股票基金", AssetClass.FUND),
        ("Global Equity ETF", AssetClass.FUND),
    ],
)
def test_map_snapshot_classifies_tiger_funds(
    name: str,
    expected_asset_class: AssetClass,
) -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "HK0000951506.HKD",
                "name": name,
                "sec_type": "FUND",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "437187.6069",
                "average_cost": "1.10",
                "market_price": "1.1032",
                "market_value": "482305.3679",
                "unrealized_pnl": "0",
            }
        ],
    )

    positions, _, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-07-16",
    )

    assert blocking_errors == []
    assert positions[0].asset_class == expected_asset_class


def test_map_snapshot_to_portfolio_inputs_defaults_hk_currency_from_symbol() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "00700.HK",
                "name": "Tencent",
                "sec_type": "STK",
                "currency": "",
                "position_qty": "100",
                "average_cost": "300",
                "market_price": "310",
                "market_value": "31000",
                "unrealized_pnl": "1000",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-29",
    )

    assert blocking_errors == []
    assert cash_balances == []
    assert len(positions) == 1
    assert positions[0].market == Market.HK
    assert positions[0].currency == "HKD"


@pytest.mark.parametrize(
    (
        "position_records",
        "expected_blocking_errors",
    ),
    [
        (
            [
                {
                    "account_alias": "tiger_6789",
                    "symbol": "MSFT",
                    "sec_type": "STK",
                    "currency": "USD",
                    "market": "US",
                    "position_qty": "bad",
                    "average_cost": "300",
                    "market_price": "410",
                    "market_value": "820",
                    "unrealized_pnl": "220",
                }
            ],
            ["position MSFT has invalid required field position_qty='bad'"],
        ),
        (
            [
                {
                    "account_alias": "tiger_6789",
                    "symbol": "MSFT",
                    "sec_type": "STK",
                    "currency": "USD",
                    "market": "US",
                    "position_qty": "bad",
                    "average_cost": "300",
                    "market_price": "410",
                    "market_value": "bad",
                    "unrealized_pnl": "220",
                }
            ],
            [
                "position MSFT has invalid required field position_qty='bad'",
                "position MSFT has invalid required field market_value='bad'",
            ],
        ),
    ],
)
def test_map_snapshot_skips_malformed_required_position_rows_and_records_blocking_errors(
    position_records: list[dict[str, object]],
    expected_blocking_errors: list[str],
) -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=position_records,
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert cash_balances == []
    assert positions == []
    assert blocking_errors == expected_blocking_errors


def test_map_snapshot_handles_non_scalar_required_decimal_values_as_blocking_errors() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": [],
                "available_balance": "88.50",
                "source": "get_prime_assets",
            }
        ],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": [],
                "average_cost": "300",
                "market_price": "410",
                "market_value": [],
                "unrealized_pnl": "220",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert cash_balances == []
    assert blocking_errors == [
        "position MSFT has invalid required field position_qty=[]",
        "position MSFT has invalid required field market_value=[]",
        "cash USD has invalid required field cash_balance=[]",
    ]


def test_map_snapshot_handles_non_scalar_optional_decimal_values_as_missing() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "10",
                "available_balance": [],
                "gross_position_value": [],
                "source": "get_prime_assets",
            }
        ],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "2",
                "average_cost": [],
                "market_price": [],
                "market_value": "820",
                "unrealized_pnl": [],
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    assert positions[0].cost_price is None
    assert positions[0].cost_value is None
    assert positions[0].last_price is None
    assert positions[0].unrealized_pnl is None
    assert len(cash_balances) == 1
    assert cash_balances[0].available_balance is None


def test_map_snapshot_recomputes_identity_from_fallback_symbol_fields() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "code": "msft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "2",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    positions, _, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    assert positions[0].symbol == "MSFT"
    assert positions[0].confidence == "high"


def test_map_snapshot_infers_us_market_from_currency_when_market_is_missing() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "sec_type": "STK",
                "currency": "USD",
                "position_qty": "2",
                "market_value": "820",
            }
        ],
    )

    positions, _, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    assert positions[0].market == Market.US


@pytest.mark.parametrize(
    "record",
    [
        {"symbol": "00700", "currency": "HKD"},
        {"symbol": "00700.HK", "currency": ""},
        {"symbol": "HK.00700", "currency": ""},
    ],
)
def test_map_snapshot_infers_hk_market_when_market_is_missing(
    record: dict[str, object],
) -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "sec_type": "STK",
                "position_qty": "100",
                "market_value": "32000",
                **record,
            }
        ],
    )

    positions, _, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert blocking_errors == []
    assert len(positions) == 1
    assert positions[0].market == Market.HK


def test_map_snapshot_skips_malformed_cash_rows_and_reports_error() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "bad",
                "available_balance": "88.50",
                "source": "get_prime_assets",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert cash_balances == []
    assert blocking_errors == ["cash USD has invalid required field cash_balance='bad'"]


def test_map_snapshot_preserves_negative_prime_asset_cash_balance() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "-12.50",
                "available_balance": "0",
                "source": "get_prime_assets",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert len(cash_balances) == 1
    assert cash_balances[0].cash_balance == Decimal("-12.50")
    assert blocking_errors == []


def test_map_snapshot_skips_zero_cash_records() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "HKD",
                "cash_balance": "0",
                "available_balance": "0",
                "source": "get_prime_assets",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert cash_balances == []
    assert blocking_errors == []


def test_map_snapshot_keeps_zero_cash_record_with_gross_position_value() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "0",
                "available_balance": "0",
                "gross_position_value": "100.00",
                "source": "get_prime_assets",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert len(cash_balances) == 1
    cash = cash_balances[0]
    assert cash.statement_id == "2026-06-19-tiger-live"
    assert cash.broker == "tiger"
    assert cash.account_alias == "tiger_6789"
    assert cash.currency == "USD"
    assert cash.cash_balance == Decimal("0")
    assert cash.available_balance == Decimal("0")
    assert cash.confidence == "high"
    assert "Tiger live account cash" in cash.notes
    assert blocking_errors == []


def test_map_snapshot_keeps_zero_cash_record_with_negative_gross_position_value() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "0",
                "available_balance": "0",
                "gross_position_value": "-900.00",
                "source": "get_prime_assets",
            }
        ],
        position_records=[],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert len(cash_balances) == 1
    assert cash_balances[0].cash_balance == Decimal("0")
    assert blocking_errors == []


def test_map_snapshot_skips_position_row_when_identity_is_missing() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "2",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    positions, cash_balances, blocking_errors = map_snapshot_to_portfolio_inputs(
        snapshot,
        run_date="2026-06-19",
    )

    assert positions == []
    assert cash_balances == []
    assert blocking_errors == ["position has invalid required field symbol=None"]
