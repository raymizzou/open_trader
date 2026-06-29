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
    TigerPortfolioSyncResult,
    sync_tiger_portfolio,
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


class FakePrimeAssetUsefulCashTradeClient(FakeTradeClient):
    def get_prime_assets(self, **kwargs: object) -> FakePrimeAssetsWithUsefulZeroCash:
        self.prime_asset_calls.append(kwargs)
        return FakePrimeAssetsWithUsefulZeroCash()


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
    assert client.trade_client.position_calls == [{"account": "123456789"}]
    assert client.trade_client.prime_asset_calls == [{"account": "123456789"}]


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


def test_map_snapshot_to_portfolio_inputs_maps_positions_and_cash() -> None:
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "100.25",
                "available_balance": "88.50",
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


def test_sync_tiger_portfolio_replaces_tiger_only_rows_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    qqq_row = {
        **base_portfolio_row(
            symbol="QQQ",
            name="Invesco QQQ",
            analysis_symbol="QQQ",
            accounts="phillips",
            brokers="futu",
        ),
    }
    write_portfolio(portfolio_path, [base_portfolio_row(), qqq_row])

    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "88.50",
                "available_balance": "88.50",
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

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=False,
    )

    assert result.account_count == 1
    assert result.position_count == 1
    assert result.cash_count == 1
    assert result.snapshot_path == (
        tmp_path / "data/runs/2026-06-19/tiger_account_snapshot.json"
    )
    assert result.portfolio_path == (
        tmp_path / "data/runs/2026-06-19/portfolio.csv"
    )
    assert result.report_path == tmp_path / "reports/tiger_account/2026-06-19.md"
    assert result.updated_latest is False
    symbols = {row["symbol"] for row in read_portfolio(result.portfolio_path)}
    assert "OLD" not in symbols
    assert {"MSFT", "QQQ", "USD_CASH"} <= symbols
    snapshot_text = result.snapshot_path.read_text(encoding="utf-8")
    assert "*****6789" in snapshot_text
    assert "123456789" not in snapshot_text
    report = result.report_path.read_text(encoding="utf-8")
    assert "# 老虎账户同步" in report


def test_sync_tiger_portfolio_builds_live_only_portfolio_without_existing_portfolio(
    tmp_path: Path,
) -> None:
    missing_portfolio_path = tmp_path / "data/latest/portfolio.csv"
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "DRAM",
                "name": "Roundhill Memory ETF",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "300",
                "average_cost": "70",
                "market_price": "79",
                "market_value": "23700",
                "unrealized_pnl": "2700",
            }
        ],
    )

    result = sync_tiger_portfolio(
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


def test_sync_tiger_portfolio_reconciles_prime_account_total_assets(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [base_portfolio_row(fx_to_hkd="7.85")])

    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "100.25",
                "available_balance": "88.50",
                "source": "get_prime_assets",
            },
            {
                "record_type": "account_total",
                "account_alias": "tiger_6789",
                "currency": "USD",
                "account_total": "1200",
                "source": "get_prime_assets",
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
                "unrealized_pnl": "220",
            }
        ],
    )

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=False,
    )

    rows = {row["symbol"]: row for row in read_portfolio(result.portfolio_path)}
    adjustment = rows["TIGER_UNMAPPED_ASSETS"]
    assert adjustment["market"] == "CASH"
    assert adjustment["asset_class"] == "cash"
    assert adjustment["currency"] == "HKD"
    assert adjustment["market_value_hkd"] == "2196.04"
    assert adjustment["brokers"] == "tiger"
    assert (
        adjustment["notes"]
        == "Tiger account_total reconciliation for locked funds or fund assets not returned as positions"
    )


def test_sync_tiger_portfolio_masks_numeric_account_values_in_snapshot_artifact(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [base_portfolio_row()])

    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account": 123456789,
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "88.50",
                "available_balance": "88.50",
                "source": "get_prime_assets",
            }
        ],
        position_records=[
            {
                "account": 123456789,
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

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=False,
    )

    snapshot_payload = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot_payload["cash_records"][0]["account"] == "*****6789"
    assert snapshot_payload["position_records"][0]["account"] == "*****6789"
    assert "123456789" not in result.snapshot_path.read_text(encoding="utf-8")


def test_sync_tiger_portfolio_updates_latest_when_requested(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [base_portfolio_row()])

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
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

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=True,
    )

    latest_rows = read_portfolio(result.latest_path)
    assert {row["symbol"] for row in latest_rows} == {"MSFT"}
    assert result.updated_latest is True


def test_sync_tiger_portfolio_deduplicates_stock_against_preserved_futu_unknown(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [])
    run_dir = tmp_path / "data/runs/2026-06-29"
    write_csv(
        run_dir / "extracted_positions.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "market",
            "asset_class",
            "symbol",
            "name",
            "currency",
            "quantity",
            "cost_price",
            "last_price",
            "market_value",
            "cost_value",
            "unrealized_pnl",
            "confidence",
            "notes",
        ],
        [futu_hk_unknown_detail_row()],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "currency",
            "cash_balance",
            "available_balance",
            "confidence",
            "notes",
        ],
        [],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    result = sync_tiger_portfolio(
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


def test_sync_tiger_portfolio_detail_path_blocks_phillips_tiger_collision(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [])
    run_dir = tmp_path / "data/runs/2026-06-29"
    write_csv(
        run_dir / "extracted_positions.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "market",
            "asset_class",
            "symbol",
            "name",
            "currency",
            "quantity",
            "cost_price",
            "last_price",
            "market_value",
            "cost_value",
            "unrealized_pnl",
            "confidence",
            "notes",
        ],
        [
            {
                "statement_id": "2026-05-phillips",
                "broker": "phillips",
                "account_alias": "phillips_main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "01688",
                "name": "领益智造",
                "currency": "HKD",
                "quantity": "360",
                "cost_price": "10.18",
                "last_price": "9.71",
                "market_value": "3495.6",
                "cost_value": "3664.8",
                "unrealized_pnl": "-169.2",
                "confidence": "high",
                "notes": "Phillips statement position",
            }
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "currency",
            "cash_balance",
            "available_balance",
            "confidence",
            "notes",
        ],
        [],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    with pytest.raises(TigerAccountError) as exc_info:
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-29",
            update_latest=True,
        )

    assert exc_info.value.error_type == "mixed_tiger_broker_row"
    assert "01688" in str(exc_info.value)


def test_sync_tiger_portfolio_no_detail_fallback_deduplicates_preserved_futu_row(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            base_portfolio_row(
                market="HK",
                asset_class="unknown",
                symbol="01688",
                name="领益智造",
                currency="HKD",
                total_quantity="0",
                avg_cost_price="0",
                last_price="9.71",
                market_value="0",
                cost_value="0",
                unrealized_pnl="-277.2",
                unrealized_pnl_pct="",
                fx_to_hkd="1",
                market_value_hkd="0.00",
                cost_value_hkd="0.00",
                portfolio_weight_hkd="0.00%",
                brokers="futu",
                accounts="futu_111",
                ai_eligible="false",
                analysis_symbol="",
                risk_flag="normal",
                notes="Futu live account position",
            )
        ],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    result = sync_tiger_portfolio(
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


def test_sync_tiger_portfolio_no_detail_accepts_canonical_mixed_tiger_row(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            base_portfolio_row(
                sort_group="1",
                market="HK",
                asset_class="stock",
                symbol="01688",
                name="领益智造",
                currency="HKD",
                total_quantity="2640",
                avg_cost_price="10.18",
                last_price="9.71",
                market_value="25634.4",
                cost_value="26875.2",
                unrealized_pnl="-1518.0",
                unrealized_pnl_pct="-5.65%",
                fx_to_hkd="1",
                market_value_hkd="25634.40",
                cost_value_hkd="26875.20",
                portfolio_weight_hkd="100.00%",
                brokers="futu;tiger",
                accounts="futu_111;tiger_5683",
                ai_eligible="true",
                analysis_symbol="01688",
                risk_flag="overweight",
                notes="Futu live account position; Tiger live account position",
            )
        ],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    result = sync_tiger_portfolio(
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
    assert row["total_quantity"] == "2640"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"


@pytest.mark.parametrize(
    ("brokers", "accounts"),
    [
        ("phillips;tiger", "phillips_main;tiger_5683"),
        ("futu;phillips;tiger", "futu_111;phillips_main;tiger_5683"),
    ],
)
def test_sync_tiger_portfolio_no_detail_blocks_unsupported_mixed_tiger_rows(
    tmp_path: Path,
    brokers: str,
    accounts: str,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            base_portfolio_row(
                sort_group="1",
                market="HK",
                asset_class="stock",
                symbol="01688",
                name="领益智造",
                currency="HKD",
                total_quantity="3000",
                avg_cost_price="10.18",
                last_price="9.71",
                market_value="29130",
                cost_value="30540",
                unrealized_pnl="-1410",
                unrealized_pnl_pct="-4.62%",
                fx_to_hkd="1",
                market_value_hkd="29130.00",
                cost_value_hkd="30540.00",
                portfolio_weight_hkd="100.00%",
                brokers=brokers,
                accounts=accounts,
                ai_eligible="true",
                analysis_symbol="01688",
                risk_flag="overweight",
                notes="Imported mixed broker row",
            )
        ],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    with pytest.raises(TigerAccountError) as exc_info:
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-29",
            update_latest=True,
        )

    assert exc_info.value.error_type == "mixed_tiger_broker_row"
    assert "01688" in str(exc_info.value)


def test_sync_tiger_portfolio_replaces_tiger_details_when_latest_has_mixed_live_rows(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            base_portfolio_row(
                symbol="VIXY",
                total_quantity="135",
                brokers="futu;tiger",
                accounts="futu_live;tiger_old",
                notes="Futu live account position",
            )
        ],
    )
    write_csv(
        tmp_path / "data/runs/2026-06-19/extracted_positions.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "market",
            "asset_class",
            "symbol",
            "name",
            "currency",
            "quantity",
            "cost_price",
            "last_price",
            "market_value",
            "cost_value",
            "unrealized_pnl",
            "confidence",
            "notes",
        ],
        [
            {
                "statement_id": "2026-06-19-futu-live",
                "broker": "futu",
                "account_alias": "futu_live",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "100",
                "cost_price": "42.00",
                "last_price": "22.00",
                "market_value": "2200",
                "cost_value": "4200",
                "unrealized_pnl": "-2000",
                "confidence": "high",
                "notes": "Futu live account position",
            },
            {
                "statement_id": "2026-05-tiger",
                "broker": "tiger",
                "account_alias": "tiger_old",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "35",
                "cost_price": "42.00",
                "last_price": "22.00",
                "market_value": "770",
                "cost_value": "1470",
                "unrealized_pnl": "-700",
                "confidence": "high",
                "notes": "Old Tiger statement position",
            },
            {
                "statement_id": "2026-05-phillips",
                "broker": "phillips",
                "account_alias": "phillips_main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "02476",
                "name": "SHENGHONG",
                "currency": "HKD",
                "quantity": "400",
                "cost_price": "",
                "last_price": "400",
                "market_value": "160000",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    write_csv(
        tmp_path / "data/runs/2026-06-19/extracted_cash.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "currency",
            "cash_balance",
            "available_balance",
            "confidence",
            "notes",
        ],
        [
            {
                "statement_id": "2026-06-19-futu-live",
                "broker": "futu",
                "account_alias": "futu_live",
                "currency": "USD",
                "cash_balance": "1000",
                "available_balance": "1000",
                "confidence": "high",
                "notes": "Futu live account cash",
            },
            {
                "statement_id": "2026-05-tiger",
                "broker": "tiger",
                "account_alias": "tiger_old",
                "currency": "USD",
                "cash_balance": "500",
                "available_balance": "500",
                "confidence": "high",
                "notes": "Old Tiger statement cash",
            },
        ],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[
            {
                "account_alias": "tiger_6789",
                "currency": "USD",
                "cash_balance": "88.50",
                "available_balance": "88.50",
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

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=True,
    )

    latest_rows = read_portfolio(result.latest_path)
    vixy = next(row for row in latest_rows if row["symbol"] == "VIXY")
    assert vixy["total_quantity"] == "100"
    assert vixy["brokers"] == "futu"
    assert "tiger" not in vixy["accounts"]
    assert "MSFT" in {row["symbol"] for row in latest_rows}
    usd_cash = next(row for row in latest_rows if row["symbol"] == "USD_CASH")
    assert usd_cash["brokers"] == "futu;tiger"
    assert Decimal(usd_cash["market_value"]) == Decimal("1088.50")


def test_sync_tiger_portfolio_blocks_mixed_tiger_broker_rows(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    mixed_row = {**base_portfolio_row(symbol="MSFT"), "brokers": "futu;tiger"}
    write_portfolio(portfolio_path, [mixed_row])

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[],
    )

    with pytest.raises(TigerAccountError) as exc_info:
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=True,
        )

    assert exc_info.value.error_type == "mixed_tiger_broker_row"
    assert "MSFT" in str(exc_info.value)


def test_sync_tiger_portfolio_blocks_latest_update_on_data_errors(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [base_portfolio_row(symbol="QQQ")])

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "BROKEN",
                "name": "Broken",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "bad",
                "market_value": "invalid",
                "average_cost": "300",
                "market_price": "410",
                "unrealized_pnl": "220",
            }
        ],
    )
    run_dir = tmp_path / "data/runs/2026-06-19"

    with pytest.raises(TigerAccountError) as exc_info:
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=True,
        )

    assert exc_info.value.error_type == "blocking_data_error"
    assert exc_info.value.sync_result == TigerPortfolioSyncResult(
        run_date="2026-06-19",
        account_count=1,
        position_count=0,
        cash_count=0,
        merged_row_count=0,
        snapshot_path=run_dir / "tiger_account_snapshot.json",
        portfolio_path=run_dir / "portfolio.csv",
        report_path=tmp_path / "reports/tiger_account/2026-06-19.md",
        latest_path=tmp_path / "data/latest/portfolio.csv",
        updated_latest=False,
    )
    assert read_portfolio(portfolio_path)[0]["symbol"] == "QQQ"
    assert run_dir.joinpath("tiger_account_snapshot.json").exists()
    assert run_dir.joinpath("portfolio.csv").exists()
    assert (tmp_path / "reports/tiger_account/2026-06-19.md").exists()


def test_sync_tiger_portfolio_raises_blocking_error_after_dated_artifacts_without_latest_update(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [base_portfolio_row(symbol="QQQ")])

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "BROKEN",
                "name": "Broken",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "bad",
                "market_value": "invalid",
                "average_cost": "300",
                "market_price": "410",
                "unrealized_pnl": "220",
            }
        ],
    )
    run_dir = tmp_path / "data/runs/2026-06-19"

    with pytest.raises(TigerAccountError) as exc_info:
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=False,
        )

    assert exc_info.value.error_type == "blocking_data_error"
    assert read_portfolio(portfolio_path)[0]["symbol"] == "QQQ"
    assert run_dir.joinpath("tiger_account_snapshot.json").exists()
    assert run_dir.joinpath("portfolio.csv").exists()
    report_path = tmp_path / "reports/tiger_account/2026-06-19.md"
    assert report_path.exists()
    assert "未更新 latest" in report_path.read_text(encoding="utf-8")


def test_sync_tiger_portfolio_does_not_report_updated_latest_if_latest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                **base_portfolio_row(
                    symbol="QQQ",
                    name="Invesco QQQ",
                    analysis_symbol="QQQ",
                    accounts="phillips",
                    brokers="futu",
                )
            }
        ],
    )

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    report_path = tmp_path / "reports/tiger_account/2026-06-19.md"

    def fail_write_latest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("latest write failed")

    monkeypatch.setattr(
        tiger_account_module,
        "_write_latest_portfolio_atomic",
        fail_write_latest,
    )

    with pytest.raises(RuntimeError, match="latest write failed"):
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=True,
        )

    assert report_path.exists()
    assert "未更新 latest" in report_path.read_text(encoding="utf-8")
    assert "已更新 latest" not in report_path.read_text(encoding="utf-8")


def test_sync_tiger_portfolio_restores_latest_if_final_report_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    base_rows = [
        {
            **base_portfolio_row(
                symbol="QQQ",
                name="Invesco QQQ",
                analysis_symbol="QQQ",
                accounts="phillips",
                brokers="futu",
            )
        }
    ]
    write_portfolio(portfolio_path, base_rows)

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    report_path = tmp_path / "reports/tiger_account/2026-06-19.md"
    original_write_text = tiger_account_module._write_text_file_atomic

    def fail_on_final_report(
        path: Path,
        text: str,
        **kwargs: object,
    ) -> None:
        if path == report_path and "已更新 latest" in text:
            raise RuntimeError("final report write failed")
        original_write_text(path, text, **kwargs)

    monkeypatch.setattr(
        tiger_account_module,
        "_write_text_file_atomic",
        fail_on_final_report,
    )

    with pytest.raises(RuntimeError, match="final report write failed"):
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=True,
        )

    latest_rows = read_portfolio(portfolio_path)
    assert len(latest_rows) == 1
    assert latest_rows[0]["symbol"] == "QQQ"
    assert latest_rows[0]["name"] == "Invesco QQQ"
    assert report_path.exists()
    assert "未更新 latest" in report_path.read_text(encoding="utf-8")
    assert "已更新 latest" not in report_path.read_text(encoding="utf-8")


def test_sync_tiger_portfolio_restores_latest_using_rename_on_final_report_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    base_rows = [
        {
            **base_portfolio_row(
                symbol="QQQ",
                name="Invesco QQQ",
                analysis_symbol="QQQ",
                accounts="phillips",
                brokers="futu",
            )
        }
    ]
    write_portfolio(portfolio_path, base_rows)

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "220",
            }
        ],
    )

    report_path = tmp_path / "reports/tiger_account/2026-06-19.md"
    original_write_text = tiger_account_module._write_text_file_atomic

    def fail_on_final_report(
        path: Path,
        text: str,
        **kwargs: object,
    ) -> None:
        if path == report_path and "已更新 latest" in text:
            raise RuntimeError("final report write failed")
        original_write_text(path, text, **kwargs)

    def fail_if_bytes_restore(
        source_path: Path,
        destination_path: Path,
    ) -> None:
        raise AssertionError(
            "_write_bytes_to_path_atomic should not be used for final rollback"
        )

    monkeypatch.setattr(
        tiger_account_module,
        "_write_text_file_atomic",
        fail_on_final_report,
    )
    monkeypatch.setattr(
        tiger_account_module,
        "_write_bytes_to_path_atomic",
        fail_if_bytes_restore,
    )

    with pytest.raises(RuntimeError, match="final report write failed"):
        sync_tiger_portfolio(
            snapshot=snapshot,
            portfolio_path=portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            run_date="2026-06-19",
            update_latest=True,
        )

    latest_rows = read_portfolio(portfolio_path)
    assert len(latest_rows) == 1
    assert latest_rows[0]["symbol"] == "QQQ"
    assert latest_rows[0]["name"] == "Invesco QQQ"
    assert report_path.exists()
    assert "未更新 latest" in report_path.read_text(encoding="utf-8")
    assert "已更新 latest" not in report_path.read_text(encoding="utf-8")


def test_atomic_temp_path_includes_process_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path("/tmp/tiger-portfolio.csv")
    tokens = ["a" * 32, "b" * 32]

    class _Token:
        def __init__(self, value: str) -> None:
            self.hex = value

    def fake_uuid4() -> _Token:
        return _Token(tokens.pop(0))

    monkeypatch.setattr(tiger_account_module.uuid, "uuid4", fake_uuid4)
    first = tiger_account_module._atomic_temp_path(path)
    second = tiger_account_module._atomic_temp_path(path)

    assert first.name != second.name
    assert first.name.startswith(f".{path.name}.")
    assert first.name.endswith(".tmp")
    assert second.name.startswith(f".{path.name}.")
    assert second.name.endswith(".tmp")


def test_sync_tiger_portfolio_uses_safe_sort_group_parsing(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                **base_portfolio_row(symbol="QQQ", sort_group="bad"),
                "brokers": "futu",
            }
        ],
    )

    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_6789",
                "symbol": "MSFT",
                "name": "Microsoft",
                "sec_type": "STK",
                "currency": "USD",
                "market": "US",
                "position_qty": "1",
                "average_cost": "300",
                "market_price": "410",
                "market_value": "820",
                "unrealized_pnl": "520",
            }
        ],
    )

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-19",
        update_latest=False,
    )

    symbols = {row["symbol"] for row in read_portfolio(result.portfolio_path)}
    assert {"MSFT", "QQQ"} <= symbols
