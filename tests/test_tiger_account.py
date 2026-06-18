from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

import pytest

from open_trader.models import AssetClass, Market
from open_trader.tiger_account import (
    TigerAccount,
    TigerAccountConfig,
    TigerAccountError,
    TigerAccountClient,
    TigerAccountSnapshot,
    map_snapshot_to_portfolio_inputs,
    load_tiger_account_config,
    mask_account_id,
)


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
    assert "failed to initialize Tiger TradeClient: factory failed" in str(exc_info.value)
    assert len(calls) == 1


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
