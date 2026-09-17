from __future__ import annotations

import json
import threading
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from polymarket import PRODUCTION, SecureClient

import open_trader.cli as cli
import open_trader.polymarket_trading as polymarket_trading
from open_trader.polymarket_trading import (
    KEYCHAIN_SERVICE,
    PREDICT_API_KEY_ACCOUNT,
    PREDICT_KEYCHAIN_SERVICE,
    PREDICT_PRIVATE_KEY_ACCOUNT,
    PolymarketTradingClient,
    PredictConfig,
    ThresholdHedgeSubmission,
    ThresholdLegResult,
    TradingConfig,
    load_keychain_secret,
    load_predict_api_key,
    load_predict_private_key,
    load_trading_config,
    store_keychain_secret,
    store_predict_api_key,
)
from open_trader.prediction_arbitrage import (
    PairIntent,
    ThresholdHedgeIntent,
    ThresholdHedgeLeg,
)
from open_trader.predict_cross_venue import CrossVenueLeg


SIGNER = "0x1111111111111111111111111111111111111111"
WALLET = "0x2222222222222222222222222222222222222222"
PREDICT_WALLET = "0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435"


def intent() -> PairIntent:
    return PairIntent(
        event_id="event-1",
        market_id="market-1",
        condition_id="condition-1",
        yes_token_id="yes-token",
        no_token_id="no-token",
        quantity=Decimal("20.00"),
        yes_max_price=Decimal("0.45"),
        no_max_price=Decimal("0.48"),
        yes_max_cost=Decimal("9.00"),
        no_max_cost=Decimal("9.60"),
        total_max_cost=Decimal("18.60"),
        minimum_profit=Decimal("1.40"),
        net_edge=Decimal("0.07"),
    )


def threshold_intent() -> ThresholdHedgeIntent:
    return ThresholdHedgeIntent(
        relation_id="relation-1",
        event_id="event-threshold",
        relation="B_IMPLIES_A",
        leg_a=ThresholdHedgeLeg(
            label="A",
            condition_id="condition-a",
            market_id="market-a",
            outcome="YES",
            token_id="a-token",
            quantity=Decimal("10"),
            max_price=Decimal("0.10"),
            max_cost=Decimal("1.00"),
            tick_size=Decimal("0.01"),
        ),
        leg_b=ThresholdHedgeLeg(
            label="B",
            condition_id="condition-b",
            market_id="market-b",
            outcome="NO",
            token_id="b-token",
            quantity=Decimal("10"),
            max_price=Decimal("0.11"),
            max_cost=Decimal("1.10"),
            tick_size=Decimal("0.01"),
        ),
        quantity=Decimal("10"),
        maximum_fee=Decimal("0.02"),
        total_max_cost=Decimal("2.12"),
        minimum_payout=Decimal("10"),
        minimum_profit=Decimal("7.88"),
        net_edge=Decimal("0.788"),
    )


def cross_polymarket_leg() -> CrossVenueLeg:
    return CrossVenueLeg(
        exchange="polymarket",
        market_id="market-cross",
        condition_id="condition-cross",
        outcome="NO",
        token_id="cross-no-token",
        settlement_asset="pUSD",
        requested_quantity=Decimal("5"),
        net_quantity=Decimal("5"),
        max_price=Decimal("0.48"),
        max_cost=Decimal("2.40"),
        maximum_fee=Decimal("0.05"),
        fee_asset="pUSD",
        book_timestamp=datetime.now(UTC),
        settlement_at=None,
    )


def test_keychain_write_never_places_secret_in_process_arguments() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        return CompletedProcess(args, 0, "", "")

    store_keychain_secret("signing-private-key", "secret-sentinel", run=run)

    assert calls[0][0] == [
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-a",
        "signing-private-key",
        "-s",
        KEYCHAIN_SERVICE,
        "-w",
    ]
    assert all("secret-sentinel" not in item for item in calls[0][0])
    assert calls[0][1] == "secret-sentinel\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Keychain")
def test_keychain_write_round_trips_through_real_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = f"com.open-trader.test.{uuid4()}"
    account = "signing-private-key"
    secret = f"secret-{uuid4()}"
    monkeypatch.setattr(polymarket_trading, "KEYCHAIN_SERVICE", service)

    try:
        store_keychain_secret(account, secret)
        stored = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.rstrip("\r\n")
        assert stored == secret
    finally:
        subprocess.run(
            [
                "/usr/bin/security",
                "delete-generic-password",
                "-a",
                account,
                "-s",
                service,
            ],
            text=True,
            capture_output=True,
            check=False,
        )


def test_keychain_read_captures_stdout_without_exposing_secret() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, "secret-sentinel\n", "")

    assert load_keychain_secret("builder-key", run=run) == "secret-sentinel"
    assert calls[0][0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "builder-key",
        "-s",
        KEYCHAIN_SERVICE,
        "-w",
    ]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True
    assert calls[0][1]["check"] is True


@pytest.mark.parametrize(
    "payload",
    (
        {"signer_address": "1111111111111111111111111111111111111111", "wallet_address": WALLET},
        {"signer_address": "0x1", "wallet_address": WALLET},
        {"signer_address": "0xGG11111111111111111111111111111111111111", "wallet_address": WALLET},
        {"signer_address": SIGNER, "wallet_address": "0x2222"},
    ),
)
def test_config_rejects_noncanonical_addresses(tmp_path: Path, payload: dict[str, str]) -> None:
    path = tmp_path / "trading.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_trading_config(path)


def test_config_accepts_canonical_addresses(tmp_path: Path) -> None:
    path = tmp_path / "trading.json"
    path.write_text(
        json.dumps({"signer_address": SIGNER, "wallet_address": WALLET}),
        encoding="utf-8",
    )

    assert load_trading_config(path) == TradingConfig(SIGNER, WALLET)


def test_trading_config_accepts_only_the_optional_mainnet_predict_wallet(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trading.json"
    path.write_text(
        json.dumps(
            {
                "signer_address": SIGNER,
                "wallet_address": WALLET,
                "predict": {
                    "wallet_address": PREDICT_WALLET,
                    "environment": "mainnet",
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_trading_config(path) == TradingConfig(
        SIGNER,
        WALLET,
        PredictConfig(wallet_address=PREDICT_WALLET),
    )

    for predict in (
        None,
        {"wallet_address": PREDICT_WALLET, "environment": "testnet"},
        {"wallet_address": PREDICT_WALLET, "environment": "mainnet", "api_key": "no"},
        {"wallet_address": "0x1", "environment": "mainnet"},
    ):
        path.write_text(
            json.dumps(
                {"signer_address": SIGNER, "wallet_address": WALLET, "predict": predict}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load_trading_config(path)


def test_predict_api_key_stays_in_its_own_keychain_service() -> None:
    calls: list[tuple[list[str], object]] = []

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        return CompletedProcess(args, 0, "", "")

    store_predict_api_key("predict-key-sentinel", run=run)

    assert calls == [
        (
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-a",
                PREDICT_API_KEY_ACCOUNT,
                "-s",
                PREDICT_KEYCHAIN_SERVICE,
                "-w",
            ],
            "predict-key-sentinel\n",
        )
    ]
    assert all("predict-key-sentinel" not in item for item in calls[0][0])


def test_load_predict_api_key_strips_value_and_redacts_failures() -> None:
    assert load_predict_api_key(
        run=lambda args, **kwargs: CompletedProcess(args, 0, " key-sentinel\r\n", "")
    ) == " key-sentinel"

    def unavailable(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        raise CalledProcessError(1, args, stderr="key-sentinel")

    with pytest.raises(Exception) as exc_info:
        load_predict_api_key(run=unavailable)
    assert "key-sentinel" not in str(exc_info.value)


def test_load_predict_private_key_uses_predict_keychain_and_redacts_failures() -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append(args)
        return CompletedProcess(args, 0, "private-sentinel\n", "")

    assert load_predict_private_key(run=run) == "private-sentinel"
    assert calls == [[
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        PREDICT_PRIVATE_KEY_ACCOUNT,
        "-s",
        PREDICT_KEYCHAIN_SERVICE,
        "-w",
    ]]
    assert all("private-sentinel" not in arg for arg in calls[0])

    def unavailable(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        raise CalledProcessError(1, args, stderr="private-sentinel")

    with pytest.raises(Exception) as exc_info:
        load_predict_private_key(run=unavailable)
    assert "private-sentinel" not in str(exc_info.value)


def test_predict_setup_preserves_polymarket_config_and_hides_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"signer_address": SIGNER, "wallet_address": WALLET}), encoding="utf-8"
    )
    stored: list[str] = []
    monkeypatch.setattr(cli, "getpass", lambda prompt: "predict-key-sentinel")
    monkeypatch.setattr(cli, "store_predict_api_key", lambda key: stored.append(key))

    assert cli.main(
        [
            "prediction-arb",
            "predict",
            "setup",
            "--config",
            str(config_path),
            "--wallet-address",
            PREDICT_WALLET,
        ]
    ) == 0

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "signer_address": SIGNER,
        "wallet_address": WALLET,
        "predict": {"wallet_address": PREDICT_WALLET, "environment": "mainnet"},
    }
    assert stored == ["predict-key-sentinel"]
    assert "predict-key-sentinel" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["prediction-arb", "predict", "setup", "--api-key", "no"])


def test_from_keychain_uses_official_factory_without_redacted_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def load(account: str, **kwargs: object) -> str:
        return {
            "signing-private-key": "private-sentinel",
            "builder-key": "builder-key-sentinel",
            "builder-secret": "builder-secret-sentinel",
            "builder-passphrase": "builder-passphrase-sentinel",
        }[account]

    def factory(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr("open_trader.polymarket_trading.load_keychain_secret", load)
    adapter = PolymarketTradingClient.from_keychain(
        TradingConfig(SIGNER, WALLET), client_factory=factory
    )

    assert isinstance(adapter, PolymarketTradingClient)
    assert captured["private_key"] == "private-sentinel"
    assert captured["wallet"] == WALLET
    assert "private-sentinel" not in repr(captured["api_key"])
    assert "builder-secret-sentinel" not in repr(captured["api_key"])


@pytest.mark.parametrize("identity", (False,))
def test_from_keychain_rejects_missing_client_identity(
    monkeypatch: pytest.MonkeyPatch, identity: bool
) -> None:
    secrets = {
        "signing-private-key": "private-sentinel",
        "builder-key": "builder-key-sentinel",
        "builder-secret": "builder-secret-sentinel",
        "builder-passphrase": "builder-passphrase-sentinel",
    }

    monkeypatch.setattr(
        "open_trader.polymarket_trading.load_keychain_secret",
        lambda account, **kwargs: secrets[account],
    )

    with pytest.raises(Exception) as exc_info:
        PolymarketTradingClient.from_keychain(
            TradingConfig(SIGNER, WALLET),
            client_factory=lambda **kwargs: FakeClient(identity=identity),
        )
    assert getattr(exc_info.value, "error_code", None) == "auth"


def test_from_keychain_rejects_mismatched_client_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "signing-private-key": "private-sentinel",
        "builder-key": "builder-key-sentinel",
        "builder-secret": "builder-secret-sentinel",
        "builder-passphrase": "builder-passphrase-sentinel",
    }
    monkeypatch.setattr(
        "open_trader.polymarket_trading.load_keychain_secret",
        lambda account, **kwargs: secrets[account],
    )

    def factory(**kwargs: object) -> FakeClient:
        client = FakeClient()
        client.signer = "0x4444444444444444444444444444444444444444"
        return client

    with pytest.raises(Exception) as exc_info:
        PolymarketTradingClient.from_keychain(
            TradingConfig(SIGNER, WALLET), client_factory=factory
        )
    assert getattr(exc_info.value, "error_code", None) == "auth"


@dataclass
class FakeSignedOrder:
    token_id: str
    taker_amount: int
    maker_amount: int = 20_000_000
    order_type: str = "FOK"
    side: str = "BUY"
    signature: str = "signature-sentinel"


class FakeClient:
    def __init__(
        self,
        *,
        taker_scale: int = 1_000_000,
        forced_quantity: Decimal | None = None,
        identity: bool = True,
        gasless_ready: bool = True,
        buy_fee: Decimal = Decimal("0"),
        buy_fee_tokens: set[str] | None = None,
    ) -> None:
        self.taker_scale = taker_scale
        if identity:
            self.signer = SIGNER
            self.wallet = WALLET
        self.environment = SimpleNamespace(
            standard_exchange="0x3333333333333333333333333333333333333333"
        )
        self.create_calls: list[dict[str, object]] = []
        self.post_calls: list[tuple[object, ...]] = []
        self.read_calls: list[str] = []
        self.cancel_calls: list[tuple[str, ...]] = []
        self.merge_calls: list[dict[str, object]] = []
        self.post_error: Exception | None = None
        self.bad_taker = False
        self.forced_quantity = forced_quantity
        self.gasless_ready = gasless_ready
        self.gasless_calls = 0
        self.buy_fee = buy_fee
        self.buy_fee_tokens = buy_fee_tokens
        self.trade_rows: list[object] = [SimpleNamespace(id="trade")]
        self.position_rows: list[object] = [
            {"condition_id": "condition-1", "size": "20"}
        ]
        self.merge_wait_value: object = SimpleNamespace(
            transaction_hash="0xmerge-hash", transaction_id="merge-transaction"
        )

    def create_market_order(self, **kwargs: object) -> FakeSignedOrder:
        self.create_calls.append(kwargs)
        if kwargs["side"] == "SELL":
            shares = kwargs["shares"]
            assert isinstance(shares, Decimal)
            quantity = shares
            return FakeSignedOrder(
                token_id=str(kwargs["token_id"]),
                taker_amount=int(quantity * kwargs["min_price"] * self.taker_scale),
                maker_amount=int(quantity * self.taker_scale),
                side="SELL",
            )
        amount = kwargs["amount"]
        assert isinstance(amount, Decimal)
        max_spend = kwargs.get("max_spend")
        if (
            self.buy_fee > 0
            and isinstance(max_spend, Decimal)
            and max_spend < amount + self.buy_fee
            and (
                self.buy_fee_tokens is None
                or str(kwargs["token_id"]) in self.buy_fee_tokens
            )
        ):
            if max_spend > self.buy_fee:
                amount = (max_spend - self.buy_fee).quantize(
                    Decimal("0.01"), rounding=ROUND_FLOOR
                )
            else:
                amount = Decimal("0")
        if self.forced_quantity is not None:
            quantity = self.forced_quantity
        else:
            quantity = amount / kwargs["max_price"]
            if self.bad_taker:
                quantity -= Decimal("1")
        return FakeSignedOrder(
            token_id=str(kwargs["token_id"]),
            taker_amount=int(quantity * self.taker_scale),
            maker_amount=int(amount * self.taker_scale),
        )

    def post_orders(self, orders: tuple[FakeSignedOrder, ...]) -> tuple[object, ...]:
        self.post_calls.append(orders)
        if self.post_error is not None:
            raise self.post_error
        return (
            SimpleNamespace(
                ok=True,
                status="matched",
                order_id="yes-order",
                taking_amount=Decimal("20"),
                trade_ids=("yes-trade",),
            ),
            SimpleNamespace(
                ok=False,
                code="fok_not_filled",
                message="safe message",
            ),
        )

    def get_balance_allowance(self, **kwargs: object) -> object:
        self.read_calls.append("balance")
        return SimpleNamespace(
            balance=20_000_000,
            allowances={
                self.environment.standard_exchange: 18_600_000,
                "0x9999999999999999999999999999999999999999": 999_000_000,
            },
        )

    def list_open_orders(self, **kwargs: object) -> list[object]:
        self.read_calls.append("orders")
        return [SimpleNamespace(id="open-order")]

    def list_account_trades(self, **kwargs: object) -> list[object]:
        self.read_calls.append("trades")
        return list(self.trade_rows)

    def list_positions(self, **kwargs: object) -> list[object]:
        self.read_calls.append("positions")
        return list(self.position_rows)

    def cancel_orders(self, **kwargs: object) -> object:
        order_ids = tuple(kwargs["order_ids"])
        self.cancel_calls.append(order_ids)
        return SimpleNamespace(canceled=order_ids)

    def merge_positions(self, **kwargs: object) -> object:
        self.merge_calls.append(kwargs)
        return SimpleNamespace(wait=lambda: self.merge_wait_value)

    def is_gasless_ready(self) -> bool:
        self.gasless_calls += 1
        return self.gasless_ready


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def make_adapter(fake: FakeClient | None = None) -> tuple[PolymarketTradingClient, FakeClient]:
    fake = fake or FakeClient()
    return PolymarketTradingClient(TradingConfig(SIGNER, WALLET), client=fake), fake


def test_lp_price_history_reader_batches_and_preserves_missing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = make_adapter()
    tokens = [f"token-{index:02d}" for index in range(21)] + ["token-00"]
    start_ts = 1_700_000_000
    end_ts = start_ts + 60
    requests: list[dict[str, object]] = []

    def open_batch(request: object, **_: object) -> FakeResponse:
        body = json.loads(getattr(request, "data").decode("utf-8"))
        requests.append(body)
        markets = body["markets"]
        assert body["start_ts"] == start_ts
        assert body["end_ts"] == end_ts
        assert body["fidelity"] == 1
        if "token-20" in markets:
            raise OSError("history endpoint unavailable")
        history: dict[str, object] = {}
        for token in markets:
            if token == "token-02":
                continue
            if token == "token-01":
                history[token] = [
                    {"t": start_ts, "p": "0.50"},
                    {"t": end_ts, "p": "1.20"},
                ]
                continue
            history[token] = [
                {"t": start_ts, "p": "0"},
                {"t": end_ts, "p": "0.005"},
            ]
        return FakeResponse({"history": history})

    monkeypatch.setattr("open_trader.polymarket_trading.urlopen", open_batch)
    result = adapter.lp_price_history(
        tokens, start_ts=start_ts, end_ts=end_ts, fidelity=1
    )

    assert len(requests) == 2
    assert sorted(len(body["markets"]) for body in requests) == [1, 20]
    assert len({token for body in requests for token in body["markets"]}) == 21
    assert all("token_ids" not in body for body in requests)
    assert result["request_count"] == 2
    assert result["history"]["token-00"][0]["p"] == Decimal("0")
    assert "token-01" in result["unknown_token_ids"]
    assert "token-02" in result["unknown_token_ids"]
    assert "token-20" in result["unknown_token_ids"]
    assert "token-01" not in result["history"]
    assert result["state"] == "partial"


def test_lp_catalog_reads_all_reward_pages_without_double_counting() -> None:
    native_asset = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    sponsored_asset = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

    def config(asset: str, amount: str, end_date: str = "2500-12-31") -> dict[str, object]:
        return {
            "id": 0,
            "asset_address": asset,
            "start_date": "2024-03-01",
            "end_date": end_date,
            "rate_per_day": amount,
        }

    native_page_1 = {
        "condition_id": "condition-a",
        "rewards_config": [
            config(native_asset, "80"),
            config(native_asset, "9", end_date="2024-12-31"),
        ],
        "native_daily_rate": "80",
        "sponsored_daily_rate": "5",
        "total_daily_rate": "85",
    }
    native_page_2 = {
        "condition_id": "condition-b",
        "rewards_config": [config(native_asset, "20")],
        "native_daily_rate": "20",
        "sponsored_daily_rate": "0",
        "total_daily_rate": "20",
    }
    sponsored_page = {
        "condition_id": "condition-a",
        "rewards_config": [config(sponsored_asset, "5")],
        "native_daily_rate": "80",
        "sponsored_daily_rate": "5",
        "total_daily_rate": "85",
    }

    class PagedRewards:
        def __init__(
            self,
            pages: tuple[tuple[dict[str, object], ...], ...],
            *,
            fail_page: int | None = None,
        ) -> None:
            self.pages = pages
            self.fail_page = fail_page

        def iter_items(self):
            for index, page in enumerate(self.pages):
                if index == self.fail_page:
                    raise RuntimeError("second reward page unavailable")
                yield from page

    class PublicRewardsClient:
        def __init__(self, *, fail_native_page: int | None = None) -> None:
            self.calls: list[bool] = []
            self.fail_native_page = fail_native_page

        def list_current_rewards(self, *, sponsored: bool = False) -> PagedRewards:
            self.calls.append(sponsored)
            if sponsored:
                return PagedRewards(((sponsored_page,),))
            return PagedRewards(
                ((native_page_1,), (native_page_2,)),
                fail_page=self.fail_native_page,
            )

    public = PublicRewardsClient()
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=FakeClient(),
        public_client_factory=lambda: public,
    )

    catalog = adapter.lp_reward_catalog()

    assert catalog["state"] == "known"
    assert catalog["daily_pool_usd"] == Decimal("105")
    assert public.calls == [False, True]

    incomplete_public = PublicRewardsClient(fail_native_page=1)
    incomplete_adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=FakeClient(),
        public_client_factory=lambda: incomplete_public,
    )
    incomplete = incomplete_adapter.lp_reward_catalog()

    assert incomplete["state"] == "unknown"


def test_lp_reward_snapshot_preserves_identity_assets_and_scope() -> None:
    class RewardTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.payloads: dict[tuple[str, object, object], object] = {}
            self.stop_after_path: str | None = None
            self.stop_event: threading.Event | None = None

        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            self.calls.append((path, dict(params)))
            payload = self.payloads[(path, params.get("sponsored"), params.get("next_cursor"))]
            if self.stop_after_path == path and self.stop_event is not None:
                self.stop_event.set()
            return payload

    class RewardClient(FakeClient):
        def __init__(self, transport: RewardTransport) -> None:
            super().__init__()
            self._ctx = SimpleNamespace(wallet_type="EOA", secure_clob=transport)

    date = "2026-09-14"
    condition_id = "condition-target"
    native_asset = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    sponsored_asset = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    transport = RewardTransport()
    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": native_asset,
            "maker_address": WALLET,
            "earnings": "0.60",
            "asset_rate": "1",
        },
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": sponsored_asset,
            "maker_address": WALLET,
            "earnings": "0.50",
            "asset_rate": "1",
        },
    ]
    transport.payloads[('/rewards/user', False, None)] = {
        "data": [
            {
                "date": f"{date}T00:00:00Z",
                "condition_id": condition_id,
                "asset_address": native_asset,
                "maker_address": WALLET,
                "earnings": "0.50",
                "asset_rate": "1",
            },
            {
                "date": f"{date}T00:00:00Z",
                "condition_id": "condition-other",
                "asset_address": native_asset,
                "maker_address": WALLET,
                "earnings": "9.00",
                "asset_rate": "1",
            },
        ],
        "next_cursor": "page-2",
    }
    transport.payloads[('/rewards/user', False, "page-2")] = {
        "data": [],
        "next_cursor": "LTE=",
    }
    transport.payloads[('/rewards/user', True, None)] = {
        "data": [
            {
                "date": f"{date}T00:00:00Z",
                "condition_id": condition_id,
                "asset_address": sponsored_asset,
                "maker_address": WALLET,
                "earnings": "0.30",
                "asset_rate": "1",
            }
        ],
        "next_cursor": "LTE=",
    }

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), RewardClient(transport)
    )

    snapshot = adapter.lp_reward_snapshot(date, condition_id)

    assert snapshot["state"] == "known"
    assert snapshot["account_amount"] == Decimal("1.10")
    assert snapshot["market_amount"] == Decimal("0.80")
    assert [params["sponsored"] for path, params in transport.calls if path == "/rewards/user/total"] == [True]
    assert {params["sponsored"] for path, params in transport.calls if path == "/rewards/user"} == {False, True}
    assert all(params["maker_address"] == WALLET for _, params in transport.calls)

    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": "0xdeadbeef",
            "maker_address": WALLET,
            "earnings": "1",
            "asset_rate": "1",
        }
    ]
    assert adapter.lp_reward_snapshot(date, condition_id)["state"] == "unknown"

    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": native_asset,
            "maker_address": WALLET,
            "earnings": "1",
            "asset_rate": "1.25",
        }
    ]
    assert adapter.lp_reward_snapshot(date, condition_id)["state"] == "unknown"

    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": "2026-09-13T00:00:00Z",
            "asset_address": native_asset,
            "maker_address": WALLET,
            "earnings": "1",
            "asset_rate": "1",
        }
    ]
    transport.payloads[('/rewards/user', False, None)] = {
        "data": [],
        "next_cursor": "page-2",
    }
    assert adapter.lp_reward_snapshot(date, condition_id)["state"] == "unknown"

    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": native_asset,
            "maker_address": "0x3333333333333333333333333333333333333333",
            "earnings": "1",
            "asset_rate": "1",
        }
    ]
    transport.calls.clear()
    assert adapter.lp_reward_snapshot(date, condition_id)["state"] == "unknown"
    assert [path for path, _ in transport.calls] == ["/rewards/user/total"]

    transport.payloads[('/rewards/user/total', True, None)] = [
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": native_asset,
            "maker_address": WALLET,
            "earnings": "0.60",
            "asset_rate": "1",
        },
        {
            "date": f"{date}T00:00:00Z",
            "asset_address": sponsored_asset,
            "maker_address": WALLET,
            "earnings": "0.50",
            "asset_rate": "1",
        },
    ]
    transport.payloads[('/rewards/user', False, "page-2")] = {
        "data": [],
        "next_cursor": None,
    }
    transport.calls.clear()
    assert adapter.lp_reward_snapshot(date, condition_id)["state"] == "unknown"
    assert [
        params.get("next_cursor")
        for path, params in transport.calls
        if path == "/rewards/user" and params["sponsored"] is False
    ] == [None, "page-2"]

    transport.payloads[('/rewards/user', False, "page-2")] = {
        "data": [],
        "next_cursor": "LTE=",
    }
    cancel_event = threading.Event()
    transport.stop_after_path = "/rewards/user/total"
    transport.stop_event = cancel_event
    transport.calls.clear()
    cancelled = adapter.lp_reward_snapshot(
        date, condition_id, stop_event=cancel_event
    )
    assert cancelled["state"] == "unknown"
    assert cancelled["reason"] == "cancelled"
    assert [path for path, _ in transport.calls] == ["/rewards/user/total"]


def test_lp_reward_snapshots_reads_account_total_once_for_multiple_conditions() -> None:
    date = "2026-09-14"
    native_asset = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    sponsored_asset = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    calls: list[tuple[str, dict[str, object]]] = []

    def row(
        *, condition_id: str | None, asset: str, earnings: str
    ) -> dict[str, object]:
        result = {
            "date": f"{date}T00:00:00Z",
            "asset_address": asset,
            "maker_address": WALLET,
            "earnings": earnings,
            "asset_rate": "1",
        }
        if condition_id is not None:
            result["condition_id"] = condition_id
        return result

    class RewardTransport:
        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            calls.append((path, dict(params)))
            if path == "/rewards/user/total":
                return [
                    row(condition_id=None, asset=native_asset, earnings="0.60"),
                    row(condition_id=None, asset=sponsored_asset, earnings="0.50"),
                ]
            if params["sponsored"] is False:
                return {
                    "data": [
                        row(
                            condition_id="condition-one",
                            asset=native_asset,
                            earnings="0.50",
                        ),
                        row(
                            condition_id="condition-two",
                            asset=native_asset,
                            earnings="0.20",
                        ),
                    ],
                    "next_cursor": "LTE=",
                }
            return {
                "data": [
                    row(
                        condition_id="condition-one",
                        asset=sponsored_asset,
                        earnings="0.30",
                    ),
                    row(
                        condition_id="condition-two",
                        asset="0xdeadbeef",
                        earnings="0.30",
                    ),
                ],
                "next_cursor": "LTE=",
            }

    class RewardClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._ctx = SimpleNamespace(
                wallet_type="EOA", secure_clob=RewardTransport()
            )

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), RewardClient()
    )

    snapshots = adapter.lp_reward_snapshots(
        date, ("condition-one", "condition-two")
    )

    assert snapshots["condition-one"]["state"] == "known"
    assert snapshots["condition-one"]["account_amount"] == Decimal("1.10")
    assert snapshots["condition-one"]["market_amount"] == Decimal("0.80")
    assert snapshots["condition-two"]["state"] == "unknown"
    assert sum(path == "/rewards/user/total" for path, _ in calls) == 1
    market_calls = [params for path, params in calls if path == "/rewards/user"]
    assert {params["sponsored"] for params in market_calls} == {False, True}
    assert all(params["maker_address"] == WALLET for _, params in calls)


def test_lp_reward_rates_use_current_scoped_percentages() -> None:
    today = datetime.now(UTC).date()
    date_text = today.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    native_asset = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    sponsored_asset = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    condition_id = "condition-live-share"
    native_config = {
        "id": "native-config",
        "asset_address": native_asset,
        "rate_per_day": "120",
        "start_date": date_text,
        "end_date": tomorrow,
    }
    sponsored_config = {
        "id": "sponsored-config",
        "asset_address": sponsored_asset,
        "rate_per_day": "48",
        "start_date": date_text,
        "end_date": tomorrow,
    }

    class RewardTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.pages: dict[tuple[bool, str, object], object] = {
                (False, "orders", None): {
                    "data": [
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [native_config],
                        },
                        {
                            "condition_id": "condition-order-only",
                            "earning_percentage": "1",
                            "rewards_config": [
                                {
                                    **native_config,
                                    "id": "order-only-config",
                                    "rate_per_day": "24",
                                }
                            ],
                        },
                        {
                            "condition_id": "condition-other",
                            "earning_percentage": "98",
                            "rewards_config": [
                                {
                                    **native_config,
                                    "id": "other-config",
                                    "rate_per_day": "999",
                                }
                            ],
                        },
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [
                                {
                                    **native_config,
                                    "id": "expired-config",
                                    "rate_per_day": "1000",
                                    "start_date": (today - timedelta(days=2)).isoformat(),
                                    "end_date": yesterday,
                                }
                            ],
                        },
                    ],
                    "next_cursor": "native-orders-page-2",
                },
                (False, "orders", "native-orders-page-2"): {
                    "data": [
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [native_config],
                        }
                    ],
                    "next_cursor": "LTE=",
                },
                (False, "positions", None): {
                    "data": [
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [native_config],
                        },
                        {
                            "condition_id": "condition-position-only",
                            "earning_percentage": "1",
                            "rewards_config": [
                                {
                                    **native_config,
                                    "id": "position-only-config",
                                    "rate_per_day": "24",
                                }
                            ],
                        },
                    ],
                    "next_cursor": "LTE=",
                },
                (True, "orders", None): {
                    "data": [
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [sponsored_config],
                        }
                    ],
                    "next_cursor": "LTE=",
                },
                (True, "positions", None): {
                    "data": [
                        {
                            "condition_id": condition_id,
                            "earning_percentage": "1",
                            "rewards_config": [sponsored_config],
                        }
                    ],
                    "next_cursor": "LTE=",
                },
            }

        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            self.calls.append((path, dict(params)))
            assert path == "/rewards/user/markets"
            scope = (
                "orders"
                if params.get("only_open_orders") is True
                and params.get("only_open_positions") is False
                else "positions"
                if params.get("only_open_orders") is False
                and params.get("only_open_positions") is True
                else "invalid"
            )
            assert scope != "invalid"
            return self.pages[
                (bool(params["sponsored"]), scope, params.get("next_cursor"))
            ]

    transport = RewardTransport()

    class RewardClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._ctx = SimpleNamespace(
                wallet_type="EOA", secure_clob=transport
            )

    adapter = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), RewardClient())

    rates = adapter.lp_reward_rates()

    assert rates["state"] == "known"
    assert rates["complete"] is True
    assert [
        (
            params["sponsored"],
            params["only_open_orders"],
            params["only_open_positions"],
            params.get("next_cursor"),
        )
        for path, params in transport.calls
        if path == "/rewards/user/markets"
    ] == [
        (False, True, False, None),
        (False, True, False, "native-orders-page-2"),
        (False, False, True, None),
        (True, True, False, None),
        (True, False, True, None),
    ]
    target = rates["markets"][condition_id]
    assert target["state"] == "known"
    assert target["hourly_reward_usd"] == Decimal("0.07")
    assert target["currency"] == "USD"
    assert target["checked_at"].tzinfo is not None
    assert target["sources"] == ("native", "sponsored")
    assert target["native"]["earning_percentage"] == Decimal("1")
    assert target["sponsored"]["earning_percentage"] == Decimal("1")
    assert rates["markets"]["condition-order-only"]["hourly_reward_usd"] == Decimal("0.01")
    assert rates["markets"]["condition-position-only"]["hourly_reward_usd"] == Decimal("0.01")

    target_rows = [
        row
        for (_sponsored, _scope, _cursor), page in transport.pages.items()
        if isinstance(page, dict)
        for row in page["data"]
        if row["condition_id"] == condition_id
    ]
    for row in target_rows:
        row["earning_percentage"] = "0"
    zero_share = adapter.lp_reward_rates()["markets"][condition_id]
    assert zero_share["state"] == "known"
    assert zero_share["hourly_reward_usd"] == Decimal("0")

    del target_rows[0]["earning_percentage"]
    missing_share = adapter.lp_reward_rates()["markets"][condition_id]
    assert missing_share["state"] == "unknown"
    assert missing_share["hourly_reward_usd"] is None

    target_rows[0]["earning_percentage"] = "NaN"
    nonfinite_share = adapter.lp_reward_rates()["markets"][condition_id]
    assert nonfinite_share["state"] == "unknown"

    for row in target_rows:
        row["earning_percentage"] = "1"
    native_config["asset_address"] = "0xdeadbeef"
    unknown_currency = adapter.lp_reward_rates()["markets"][condition_id]
    assert unknown_currency["state"] == "unknown"
    assert unknown_currency["hourly_reward_usd"] is None


def test_lp_rewards_preserve_raw_accrual_when_usd_value_is_unknown() -> None:
    date = "2026-09-14"
    condition_id = "condition-raw-reward"
    usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    class RewardTransport:
        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            row = {
                "date": f"{date}T00:00:00Z",
                "asset_address": usdc_e,
                "maker_address": WALLET,
                "condition_id": condition_id,
                "earnings": "0.25",
                "asset_rate": "0.9999",
            }
            if path == "/rewards/user/total":
                return [{key: value for key, value in row.items() if key != "condition_id"}]
            return {
                "data": [row] if params["sponsored"] is False else [],
                "next_cursor": "LTE=",
            }

    class RewardClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._ctx = SimpleNamespace(
                wallet_type="EOA", secure_clob=RewardTransport()
            )

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), RewardClient()
    )

    snapshot = adapter.lp_reward_snapshot(date, condition_id)

    assert snapshot["state"] == "unknown"
    assert snapshot.get("market_amount") is None
    assert snapshot["market_amount_raw"] == Decimal("0.25")
    assert snapshot["market_asset"] == "USDC.e"
    assert snapshot["usd_state"] == "unknown"
    assert snapshot["paid"] is False
    assert snapshot.get("paid_rewards") is None


def test_lp_reward_percentages_reads_official_market_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RewardTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.payload: object = {
                "condition-1": "5",
                "condition-2": Decimal("7.5"),
            }

        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            self.calls.append((path, dict(params)))
            if isinstance(self.payload, BaseException):
                raise self.payload
            return self.payload

    transport = RewardTransport()
    monkeypatch.setattr(polymarket_trading, "signature_type_for", lambda _: 2)

    class SDK:
        _ctx = SimpleNamespace(secure_clob=transport, wallet_type="proxy")

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), client=SDK()
    )

    known = adapter.lp_reward_percentages()
    assert known["state"] == "known"
    assert known["percentages"] == {
        "condition-1": Decimal("5"),
        "condition-2": Decimal("7.5"),
    }
    assert isinstance(known["checked_at"], datetime)
    assert known["maker_address"] == WALLET
    assert known["scope"] == "account"
    assert transport.calls == [
        (
            "/rewards/user/percentages",
            {"signature_type": 2, "maker_address": WALLET},
        )
    ]

    for payload in (
        RuntimeError("transport secret should stay redacted"),
        [],
        {"condition-1": True},
        {"condition-1": "NaN"},
        {"condition-1": "Infinity"},
        {"condition-1": "-0.01"},
        {"condition-1": "100.01"},
    ):
        transport.payload = payload
        unknown = adapter.lp_reward_percentages()
        assert unknown["state"] == "unknown"
        assert unknown["percentages"] == {}
        assert unknown["scope"] == "account"
        assert "transport secret" not in repr(unknown)

    for value in ("0", "100"):
        transport.payload = {"condition-1": value}
        boundary = adapter.lp_reward_percentages()
        assert boundary["state"] == "known"
        assert boundary["percentages"] == {"condition-1": Decimal(value)}

    transport.payload = {}
    empty = adapter.lp_reward_percentages()
    assert empty["state"] == "known"
    assert empty["percentages"] == {}


def make_probe_intent(
    *, quantity: Decimal, yes_price: Decimal = Decimal("0.45"), no_price: Decimal = Decimal("0.48")
) -> PairIntent:
    yes_cost = (quantity * yes_price).quantize(Decimal("0.01"))
    no_cost = (quantity * no_price).quantize(Decimal("0.01"))
    return PairIntent(
        event_id="event-probe",
        market_id="market-probe",
        condition_id="condition-probe",
        yes_token_id="yes-token",
        no_token_id="no-token",
        quantity=quantity,
        yes_max_price=yes_price,
        no_max_price=no_price,
        yes_max_cost=yes_cost,
        no_max_cost=no_cost,
        total_max_cost=yes_cost + no_cost,
        minimum_profit=Decimal("1.00"),
        net_edge=Decimal("0.01"),
    )


@pytest.mark.parametrize("payload", ({"blocked": True}, {"blocked": "false"}, [], None))
def test_geoblock_fails_closed_for_blocked_or_malformed_responses(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    adapter, _ = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    assert adapter.geoblock_allowed() is False


def test_geoblock_timeout_and_error_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = make_adapter()

    def fail(*args: object, **kwargs: object) -> object:
        raise TimeoutError("secret-sentinel")

    monkeypatch.setattr("open_trader.polymarket_trading.urlopen", fail)
    assert adapter.geoblock_allowed() is False


def test_geoblock_sends_an_explicit_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = make_adapter()

    def reject_default_urllib(request: object, **kwargs: object) -> FakeResponse:
        get_header = getattr(request, "get_header", None)
        user_agent = get_header("User-agent") if callable(get_header) else None
        if not isinstance(user_agent, str) or user_agent.startswith("Python-urllib"):
            raise PermissionError("default urllib user agent rejected")
        return FakeResponse({"blocked": False})

    monkeypatch.setattr("open_trader.polymarket_trading.urlopen", reject_default_urllib)
    assert adapter.geoblock_allowed() is True


def test_no_submit_identity_missing_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter(FakeClient(identity=False))
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_preflight(intent())

    assert summary["result"] == "BLOCKED"
    assert summary["error_code"] == "auth"
    assert fake.create_calls == []
    assert fake.post_calls == []


def test_no_submit_preflight_signs_exact_costs_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_preflight(intent())

    assert summary["result"] == "PASS"
    assert summary["posted"] is False
    assert summary["equal_requested_shares"] == "pass"
    assert len(fake.create_calls) == 2
    assert fake.post_calls == []
    assert fake.create_calls == [
        {
            "token_id": "yes-token",
            "side": "BUY",
            "amount": Decimal("9.00"),
            "max_spend": Decimal("9.00"),
            "max_price": Decimal("0.45"),
            "order_type": "FOK",
        },
        {
            "token_id": "no-token",
            "side": "BUY",
            "amount": Decimal("9.60"),
            "max_spend": Decimal("9.60"),
            "max_price": Decimal("0.48"),
            "order_type": "FOK",
        },
    ]
    assert "signature-sentinel" not in repr(summary)
    assert "yes-token" not in repr(summary)


def test_no_submit_preflight_mismatched_signed_shares_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.bad_taker = True
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_preflight(intent())

    assert summary["result"] == "BLOCKED"
    assert summary["error_code"] == "order_amount_mismatch"
    assert fake.post_calls == []


@pytest.mark.parametrize(
    ("tick_size", "price"),
    (
        (Decimal("0.1"), Decimal("0.5")),
        (Decimal("0.01"), Decimal("0.45")),
        (Decimal("0.005"), Decimal("0.45")),
        (Decimal("0.0025"), Decimal("0.45")),
        (Decimal("0.001"), Decimal("0.451")),
        (Decimal("0.0001"), Decimal("0.4501")),
    ),
)
def test_no_submit_preflight_checks_task1_rounding_for_every_supported_tick(
    monkeypatch: pytest.MonkeyPatch, tick_size: Decimal, price: Decimal
) -> None:
    from open_trader.prediction_arbitrage import protected_buy_quantity

    spend = Decimal("1.00")
    quantity = protected_buy_quantity(
        spend=spend, price=price, tick_size=tick_size
    )
    assert quantity is not None
    pair = make_probe_intent(quantity=quantity, yes_price=price, no_price=price)
    adapter, fake = make_adapter(FakeClient(forced_quantity=quantity))
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_preflight(pair, tick_size=tick_size)

    assert summary["result"] == "PASS"
    assert fake.post_calls == []


def test_submit_pair_requires_current_successful_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    blocked = adapter.submit_pair_once(intent())
    assert blocked.yes.error_code == "preflight_required"
    assert fake.post_calls == []

    assert adapter.no_submit_preflight(intent())["result"] == "PASS"
    result = adapter.submit_pair_once(intent())
    assert len(fake.post_calls) == 1
    assert result.yes.accepted is True


def test_high_cost_or_malformed_intent_is_rejected_before_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )
    too_expensive = replace(
        intent(), yes_max_cost=Decimal("20.01"), total_max_cost=Decimal("29.61")
    )

    summary = adapter.no_submit_preflight(too_expensive)
    assert summary["result"] == "BLOCKED"
    assert summary["error_code"] == "invalid"
    assert fake.create_calls == []
    assert fake.post_calls == []


def test_submit_pair_posts_two_signed_orders_once_and_preserves_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_preflight(intent())["result"] == "PASS"
    result = adapter.submit_pair_once(intent())

    assert len(fake.post_calls) == 1
    assert len(fake.post_calls[0]) == 2
    assert result.yes.accepted is True
    assert result.yes.order_id == "yes-order"
    assert result.yes.trade_ids == ("yes-trade",)
    assert result.no.accepted is False
    assert result.no.error_code == "fok_not_filled"


def test_submit_pair_post_exception_is_ambiguous_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.post_error = RuntimeError("signature-sentinel")
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_preflight(intent())["result"] == "PASS"
    result = adapter.submit_pair_once(intent())

    assert len(fake.post_calls) == 1
    assert result.yes.status == "ambiguous"
    assert result.no.status == "ambiguous"
    assert result.yes.error_code == "ambiguous"
    assert "signature-sentinel" not in repr(result)


def test_threshold_preflight_signs_independent_conditions_without_merge_or_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_threshold_preflight(threshold_intent())

    assert summary["result"] == "PASS"
    assert summary["posted"] is False
    assert summary["equal_requested_shares"] == "pass"
    assert len(fake.create_calls) == 2
    assert fake.post_calls == []
    assert fake.merge_calls == []
    assert fake.create_calls == [
        {
            "token_id": "a-token",
            "side": "BUY",
            "amount": Decimal("1.00"),
            "max_spend": Decimal("1.03"),
            "max_price": Decimal("0.10"),
            "order_type": "FOK",
        },
        {
            "token_id": "b-token",
            "side": "BUY",
            "amount": Decimal("1.10"),
            "max_spend": Decimal("1.13"),
            "max_price": Decimal("0.11"),
            "order_type": "FOK",
        },
    ]


def test_threshold_preflight_includes_fee_budget_so_shares_are_not_shrunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter(FakeClient(buy_fee=Decimal("0.01")))
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_threshold_preflight(threshold_intent())

    assert summary["result"] == "PASS"
    assert summary["fok_pair_signed_not_submitted"] == "pass"
    assert summary["equal_requested_shares"] == "pass"
    assert fake.post_calls == []
    assert [call["max_spend"] for call in fake.create_calls] == [
        Decimal("1.03"),
        Decimal("1.13"),
    ]


def test_threshold_preflight_keeps_boundary_when_one_leg_uses_whole_fee_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter(
        FakeClient(buy_fee=Decimal("0.02"), buy_fee_tokens={"b-token"})
    )
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    summary = adapter.no_submit_threshold_preflight(threshold_intent())

    assert summary["result"] == "PASS"
    assert summary["equal_requested_shares"] == "pass"
    assert fake.post_calls == []


def test_threshold_submit_requires_preflight_and_posts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )
    blocked = adapter.submit_threshold_hedge_once(threshold_intent())
    assert blocked.leg_a.error_code == "preflight_required"
    assert fake.post_calls == []

    assert adapter.no_submit_threshold_preflight(threshold_intent())["result"] == "PASS"
    result = adapter.submit_threshold_hedge_once(threshold_intent())
    assert len(fake.post_calls) == 1
    assert len(fake.post_calls[0]) == 2
    assert result.leg_a.accepted is True
    assert result.leg_b.accepted is False
    assert result.leg_a.condition_id == "condition-a"
    assert result.leg_b.condition_id == "condition-b"
    assert fake.merge_calls == []


def test_threshold_post_exception_is_ambiguous_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.post_error = RuntimeError("signature-sentinel")
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_threshold_preflight(threshold_intent())["result"] == "PASS"
    result = adapter.submit_threshold_hedge_once(threshold_intent())

    assert len(fake.post_calls) == 1
    assert result.leg_a.status == "ambiguous"
    assert result.leg_b.status == "ambiguous"
    assert "signature-sentinel" not in repr(result)


def test_threshold_post_exception_records_last_submit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.post_error = RuntimeError("signature-sentinel")
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_threshold_preflight(threshold_intent())["result"] == "PASS"
    result = adapter.submit_threshold_hedge_once(threshold_intent())

    assert result.leg_a.status == "ambiguous"
    assert result.leg_b.status == "ambiguous"
    detail = adapter.last_submit_error()
    assert detail is not None
    assert detail["error_type"] == "RuntimeError"
    assert "signature-sentinel" in detail["message"]
    assert detail["error_code"] in polymarket_trading._SAFE_ERROR_CODES


def test_pair_post_exception_records_last_submit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.post_error = RuntimeError("signature-sentinel")
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_preflight(intent())["result"] == "PASS"
    result = adapter.submit_pair_once(intent())

    assert result.yes.status == "ambiguous"
    assert result.no.status == "ambiguous"
    detail = adapter.last_submit_error()
    assert detail is not None
    assert detail["error_type"] == "RuntimeError"
    assert "signature-sentinel" in detail["message"]
    assert detail["error_code"] in polymarket_trading._SAFE_ERROR_CODES


def test_last_submit_error_resets_on_next_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    assert adapter.no_submit_threshold_preflight(threshold_intent())["result"] == "PASS"
    assert adapter.last_submit_error() is None

    fake.post_error = RuntimeError("signature-sentinel")
    adapter.submit_threshold_hedge_once(threshold_intent())
    assert adapter.last_submit_error() is not None

    fake.post_error = None
    assert adapter.no_submit_threshold_preflight(threshold_intent())["result"] == "PASS"
    result = adapter.submit_threshold_hedge_once(threshold_intent())
    assert result.leg_a.accepted is True
    assert adapter.last_submit_error() is None


def test_cross_leg_preflight_signs_once_then_posts_one_order_with_leg_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )
    def post_one(orders: tuple[FakeSignedOrder, ...]) -> tuple[object, ...]:
        fake.post_calls.append(orders)
        return (
            SimpleNamespace(
                ok=True,
                status="matched",
                order_id="cross-order",
                taking_amount=Decimal("5"),
                trade_ids=("cross-trade",),
            ),
        )

    fake.post_orders = post_one  # type: ignore[method-assign]
    leg = cross_polymarket_leg()

    assert adapter.no_submit_cross_leg_preflight(leg)["result"] == "PASS"
    assert fake.post_calls == []

    result = adapter.submit_cross_leg_once(leg)

    assert len(fake.post_calls) == 1
    assert len(fake.post_calls[0]) == 1
    assert (
        result.label,
        result.outcome,
        result.condition_id,
        result.token_id,
        result.order_id,
        result.trade_ids,
        result.filled_quantity,
    ) == (
        "polymarket",
        "NO",
        "condition-cross",
        "cross-no-token",
        "cross-order",
        ("cross-trade",),
        Decimal("5"),
    )


def test_cross_leg_transport_failure_is_ambiguous_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient()
    fake.post_error = RuntimeError("signature-sentinel")
    adapter, _ = make_adapter(fake)
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )
    leg = cross_polymarket_leg()

    assert adapter.no_submit_cross_leg_preflight(leg)["result"] == "PASS"
    result = adapter.submit_cross_leg_once(leg)

    assert len(fake.post_calls) == 1
    assert result.status == "ambiguous"
    assert result.error_code == "ambiguous"
    assert "signature-sentinel" not in repr(result)


def test_cross_leg_reconciliation_uses_order_trade_and_position_proof() -> None:
    adapter, fake = make_adapter()
    leg = cross_polymarket_leg()
    since = datetime.now(UTC) - timedelta(seconds=1)
    fake.trade_rows = [
        SimpleNamespace(
            id="cross-trade", condition_id="condition-cross", token_id="cross-no-token",
            taker_order_id="cross-order", size=Decimal("5"), status="CONFIRMED",
            side="BUY", trader_side="TAKER", price=Decimal("0.48"),
            fee_rate_bps=Decimal("200"),
            matched_at=datetime.now(UTC),
        )
    ]
    fake.position_rows = [
        {"condition_id": "condition-cross", "token_id": "cross-no-token", "size": "5"}
    ]
    result = ThresholdLegResult(
        "polymarket", "NO", "condition-cross", "cross-no-token", True,
        "filled", "cross-order", Decimal("5"), ("cross-trade",), "none",
    )

    reconciled = adapter.reconcile_cross_leg(leg, result, since=since)

    assert reconciled["verified"] is True
    assert reconciled["position_quantity"] == Decimal("5")
    assert reconciled["filled_quantity"] == Decimal("5")
    assert reconciled["actual_fee"] == Decimal("0.024960")
    assert reconciled["execution_proof"]["fee"] == Decimal("0.024960")
    assert reconciled["execution_proof"]["matched_refs"] == {
        "token_id": "cross-no-token",
        "order_ids": ["cross-order"],
        "trade_ids": ["cross-trade"],
    }


def test_cross_leg_reconciliation_does_not_invent_actual_fee_without_trade_fee_evidence() -> None:
    adapter, fake = make_adapter()
    leg = cross_polymarket_leg()
    since = datetime.now(UTC) - timedelta(seconds=1)
    fake.trade_rows = [
        SimpleNamespace(
            id="cross-trade", condition_id="condition-cross", token_id="cross-no-token",
            taker_order_id="cross-order", size=Decimal("5"), status="CONFIRMED",
            side="BUY", price=Decimal("0.48"), fee_rate_bps=Decimal("200"),
            matched_at=datetime.now(UTC),
        )
    ]
    fake.position_rows = [
        {"condition_id": "condition-cross", "token_id": "cross-no-token", "size": "5"}
    ]
    result = ThresholdLegResult(
        "polymarket", "NO", "condition-cross", "cross-no-token", True,
        "filled", "cross-order", Decimal("5"), ("cross-trade",), "none",
    )

    reconciled = adapter.reconcile_cross_leg(leg, result, since=since)

    assert reconciled["verified"] is True
    assert "actual_fee" not in reconciled
    assert "fee" not in reconciled["execution_proof"]


def test_cross_leg_reconciliation_carries_venue_minimum_order_size() -> None:
    adapter, fake = make_adapter()
    leg = SimpleNamespace(
        exchange="polymarket", market_id="market-cross", condition_id="condition-cross",
        outcome="NO", token_id="cross-no-token", settlement_asset="pUSD",
        requested_quantity=Decimal("5"), net_quantity=Decimal("5"),
        max_price=Decimal("0.48"), max_cost=Decimal("2.40"),
        maximum_fee=Decimal("0.05"), fee_asset="pUSD",
        book_timestamp=datetime.now(UTC), settlement_at=None,
        minimum_order_size=Decimal("1"),
    )
    since = datetime.now(UTC) - timedelta(seconds=1)
    fake.trade_rows = [
        SimpleNamespace(
            id="cross-trade", condition_id="condition-cross", token_id="cross-no-token",
            taker_order_id="cross-order", size=Decimal("5"), status="CONFIRMED",
            side="BUY", matched_at=datetime.now(UTC),
        )
    ]
    fake.position_rows = [
        {"condition_id": "condition-cross", "token_id": "cross-no-token", "size": "5"}
    ]
    result = ThresholdLegResult(
        "polymarket", "NO", "condition-cross", "cross-no-token", True,
        "filled", "cross-order", Decimal("5"), ("cross-trade",), "none",
    )

    reconciled = adapter.reconcile_cross_leg(leg, result, since=since)

    assert reconciled.get("minimum_order_size") == Decimal("1")


def test_threshold_reconcile_keeps_condition_and_token_refs_separate() -> None:
    adapter, fake = make_adapter()
    since = datetime.now(UTC) - timedelta(seconds=1)
    matched_at = datetime.now(UTC)
    fake.trade_rows = [
        SimpleNamespace(
            id="a-trade", condition_id="condition-a", token_id="a-token",
            taker_order_id="a-order", size=Decimal("10"), status="CONFIRMED",
            side="BUY", matched_at=matched_at,
        ),
        SimpleNamespace(
            id="b-trade", condition_id="condition-b", token_id="b-token",
            taker_order_id="b-order", size=Decimal("10"), status="CONFIRMED",
            side="BUY", matched_at=matched_at,
        ),
    ]
    fake.position_rows = [
        {"condition_id": "condition-a", "token_id": "a-token", "size": "10"},
        {"condition_id": "condition-b", "token_id": "b-token", "size": "10"},
    ]
    value = adapter.reconcile_threshold_hedge(
        intent=threshold_intent(),
        since=since,
        leg_a=ThresholdLegResult("A", "YES", "condition-a", "a-token", True, "filled", "a-order", Decimal("10"), ("a-trade",), "none"),
        leg_b=ThresholdLegResult("B", "NO", "condition-b", "b-token", True, "filled", "b-order", Decimal("10"), ("b-trade",), "none"),
    )

    assert value["status"] == "ok"
    assert value["execution_proof"]["verified"] is True
    assert value["execution_proof"]["condition_ids"] == {"A": "condition-a", "B": "condition-b"}
    assert value["execution_proof"]["matched_refs"]["B"]["token_id"] == "b-token"


def test_account_reads_cover_balance_orders_trades_and_positions() -> None:
    adapter, fake = make_adapter()

    snapshot = adapter.account_snapshot()

    assert fake.read_calls == ["balance", "orders", "trades", "positions"]
    assert snapshot.wallet_address == WALLET
    assert snapshot.p_usd_balance == Decimal("20")
    assert snapshot.p_usd_allowance == Decimal("18.6")
    assert snapshot.open_order_ids == ("open-order",)
    assert snapshot.positions == ({"condition_id": "condition-1", "size": "20"},)


def test_account_snapshot_uses_only_standard_exchange_allowance() -> None:
    adapter, _ = make_adapter()

    snapshot = adapter.account_snapshot()

    assert snapshot.p_usd_allowance == Decimal("18.6")


def test_reconcile_returns_execution_scoped_verified_trade_proof() -> None:
    adapter, fake = make_adapter()
    matched_at = datetime.now(UTC)
    fake.trade_rows = [
        SimpleNamespace(
            id="yes-trade",
            condition_id="condition-1",
            token_id="yes-token",
            taker_order_id="yes-order",
            size=Decimal("10"),
            status="CONFIRMED",
            side="BUY",
            matched_at=matched_at,
        ),
        SimpleNamespace(
            id="no-trade",
            condition_id="condition-1",
            token_id="no-token",
            taker_order_id="no-order",
            size=Decimal("10"),
            status="CONFIRMED",
            side="BUY",
            matched_at=matched_at,
        ),
    ]
    fake.position_rows = [
        {
            "condition_id": "condition-1",
            "token_id": "yes-token",
            "size": Decimal("10"),
            "updated_at": matched_at,
        },
        {
            "condition_id": "condition-1",
            "token_id": "no-token",
            "size": Decimal("10"),
            "updated_at": matched_at,
        },
    ]

    result = adapter.reconcile(
        condition_id="condition-1",
        since=matched_at - timedelta(seconds=1),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_order_id="yes-order",
        no_order_id="no-order",
        yes_trade_ids=("yes-trade",),
        no_trade_ids=("no-trade",),
    )

    assert result["status"] == "ok"
    assert result["yes_quantity"] == Decimal("10")
    assert result["no_quantity"] == Decimal("10")
    proof = result["execution_proof"]
    assert isinstance(proof, dict)
    assert proof["verified"] is True
    assert proof["venue"] == "polymarket"
    assert proof["positions_verified"] is True
    assert proof["matched_refs"]["YES"]["trade_ids"] == ["yes-trade"]
    assert proof["matched_refs"]["NO"]["trade_ids"] == ["no-trade"]


def test_reconcile_count_only_or_unmatched_positions_never_proves_fills() -> None:
    adapter, fake = make_adapter()
    fake.trade_rows = [SimpleNamespace(id="unrelated", size=Decimal("20"), status="CONFIRMED")]

    result = adapter.reconcile(
        condition_id="condition-1",
        since=datetime.now(UTC) - timedelta(seconds=1),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_order_id="yes-order",
        no_order_id="no-order",
        yes_trade_ids=("yes-trade",),
        no_trade_ids=("no-trade",),
    )

    assert result["status"] in {"blocked", "ambiguous"}
    assert "yes_quantity" not in result
    assert "no_quantity" not in result
    proof = result["execution_proof"]
    assert isinstance(proof, dict)
    assert proof["verified"] is False


def test_reconcile_requires_confirmed_trades_and_current_positions() -> None:
    adapter, fake = make_adapter()
    matched_at = datetime.now(UTC)
    fake.trade_rows = [
        SimpleNamespace(
            id="yes-trade",
            condition_id="condition-1",
            token_id="yes-token",
            taker_order_id="yes-order",
            size=Decimal("10"),
            status="MATCHED",
            side="BUY",
            matched_at=matched_at,
        ),
        SimpleNamespace(
            id="no-trade",
            condition_id="condition-1",
            token_id="no-token",
            taker_order_id="no-order",
            size=Decimal("10"),
            status="CONFIRMED",
            side="BUY",
            matched_at=matched_at,
        ),
    ]
    fake.position_rows = [
        {"condition_id": "condition-1", "token_id": "yes-token", "size": Decimal("10")}
    ]

    result = adapter.reconcile(
        condition_id="condition-1",
        since=matched_at - timedelta(seconds=1),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_order_id="yes-order",
        no_order_id="no-order",
        yes_trade_ids=("yes-trade",),
        no_trade_ids=("no-trade",),
    )

    assert result["status"] in {"blocked", "ambiguous"}
    assert result["execution_proof"]["verified"] is False
    assert result["execution_proof"]["positions_verified"] is False


def test_reconcile_exposes_verified_partial_fill_without_authorizing_merge() -> None:
    adapter, fake = make_adapter()
    matched_at = datetime.now(UTC)
    fake.trade_rows = [
        SimpleNamespace(
            id="yes-trade",
            condition_id="condition-1",
            token_id="yes-token",
            taker_order_id="yes-order",
            size=Decimal("10"),
            status="CONFIRMED",
            side="BUY",
            matched_at=matched_at,
        )
    ]
    fake.position_rows = [
        {
            "condition_id": "condition-1",
            "token_id": "yes-token",
            "size": Decimal("10"),
            "updated_at": matched_at,
        }
    ]

    result = adapter.reconcile(
        condition_id="condition-1",
        since=matched_at - timedelta(seconds=1),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_order_id="yes-order",
        no_order_id="no-order",
        yes_trade_ids=("yes-trade",),
        no_trade_ids=("no-trade",),
    )

    assert result["status"] == "partial"
    assert result["yes_quantity"] == Decimal("10")
    assert result["no_quantity"] == Decimal("0")
    proof = result["execution_proof"]
    assert proof["partial_verified"] is True
    assert proof["verified"] is False


def test_reconcile_existing_positions_without_matching_trades_stays_unverified() -> None:
    adapter, fake = make_adapter()
    now = datetime.now(UTC)
    fake.trade_rows = []
    fake.position_rows = [
        {"condition_id": "condition-1", "token_id": "yes-token", "size": Decimal("10")},
        {"condition_id": "condition-1", "token_id": "no-token", "size": Decimal("10")},
    ]

    result = adapter.reconcile(
        condition_id="condition-1",
        since=now - timedelta(seconds=1),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_order_id="yes-order",
        no_order_id="no-order",
        yes_trade_ids=("yes-trade",),
        no_trade_ids=("no-trade",),
    )

    assert result["status"] in {"blocked", "ambiguous"}
    assert result["execution_proof"]["verified"] is False


def test_secure_client_readiness_does_not_trust_deprecated_gasless_flag() -> None:
    client = object.__new__(SecureClient)
    client._ended = False
    client._ctx_inner = SimpleNamespace(
        environment=PRODUCTION,
        wallet_type="EOA",
        wallet=WALLET,
        secure_clob=SimpleNamespace(
            get_json=lambda *args, **kwargs: {
                "balance": 20_000_000,
                "allowances": {PRODUCTION.standard_exchange: 18_600_000},
            }
        ),
        relayer=SimpleNamespace(get_json=lambda *args, **kwargs: {"address": WALLET, "nonce": "1"}),
    )
    adapter = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), client=client)

    result = adapter.readiness_snapshot()

    assert result["relayer_ready"] is False
    assert result["merge_ready"] is False


def test_secure_client_readiness_uses_authenticated_relayer_probe() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Relayer:
        def get_json(self, path: str, *, params: dict[str, object]) -> dict[str, object]:
            calls.append((path, params))
            return {"address": "0x3333333333333333333333333333333333333333", "nonce": "1"}

    client = object.__new__(SecureClient)
    client._ended = False
    client._ctx_inner = SimpleNamespace(
        environment=PRODUCTION,
        wallet_type="POLY_PROXY",
        wallet=WALLET,
        signer=SimpleNamespace(address=SIGNER),
        secure_clob=SimpleNamespace(
            get_json=lambda *args, **kwargs: {
                "balance": 20_000_000,
                "allowances": {PRODUCTION.standard_exchange: 18_600_000},
            }
        ),
        relayer=Relayer(),
    )
    adapter = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), client=client)

    result = adapter.readiness_snapshot()

    assert result["relayer_ready"] is True
    assert result["merge_ready"] is True
    assert calls == [
        (
            "/relay-payload",
            {"address": SIGNER, "type": "PROXY"},
        )
    ]


def test_readiness_snapshot_requires_fresh_gasless_and_merge_capabilities() -> None:
    adapter, fake = make_adapter()

    result = adapter.readiness_snapshot()

    assert isinstance(result["checked_at"], datetime)
    assert result["relayer_ready"] is True
    assert result["merge_ready"] is True
    assert result["relayer"] == "ready"
    assert result["merge"] == "ready"
    assert fake.gasless_calls == 1

    fake.gasless_ready = False
    blocked = adapter.readiness_snapshot()
    assert blocked["relayer_ready"] is False
    assert blocked["merge_ready"] is False


def test_readiness_snapshot_includes_wallet_and_fresh_collateral_only() -> None:
    adapter, fake = make_adapter()

    result = adapter.readiness_snapshot()

    assert result["wallet"] == "ready"
    assert result["wallet_address"] == WALLET
    assert result["p_usd_balance"] == Decimal("20")
    assert result["p_usd_allowance"] == Decimal("18.6")
    assert fake.read_calls == ["balance"]


def test_cancel_and_merge_use_official_methods_once() -> None:
    adapter, fake = make_adapter()

    assert adapter.cancel_orders(("one", "two")) == ("one", "two")
    merged = adapter.merge_once(condition_id="condition-1", quantity=Decimal("20"))

    assert fake.cancel_calls == [("one", "two")]
    assert fake.merge_calls == [{"condition_id": "condition-1", "amount": 20_000_000}]
    assert merged["status"] == "confirmed"
    assert merged["confirmed"] is True
    assert merged["transaction_hash"] == "0xmerge-hash"
    assert merged["transaction_id"] == "merge-transaction"


def test_merge_without_transaction_reference_is_not_confirmed() -> None:
    adapter, fake = make_adapter()
    fake.merge_wait_value = SimpleNamespace(transaction_hash="", transaction_id="merge-transaction")

    result = adapter.merge_once(condition_id="condition-1", quantity=Decimal("20"))

    assert result["status"] in {"blocked", "ambiguous"}
    assert result.get("confirmed") is not True


def test_remediation_supports_sell_unwind_with_single_post() -> None:
    adapter, fake = make_adapter()

    result = adapter.submit_remediation_once(
        {
            "leg": "YES",
            "side": "SELL",
            "token_id": "yes-token",
            "shares": Decimal("2"),
            "quantity": Decimal("2"),
            "min_price": Decimal("0.40"),
        }
    )

    assert result.leg == "YES"
    assert len(fake.post_calls) == 1
    assert fake.create_calls[-1] == {
        "token_id": "yes-token",
        "side": "SELL",
        "shares": Decimal("2"),
        "min_price": Decimal("0.40"),
        "order_type": "FOK",
    }


@pytest.mark.parametrize(
    "order",
    (
        {"leg": "MAYBE", "side": "BUY"},
        {"leg": "YES", "side": "BUY", "token_id": "yes-token", "amount": Decimal("0"), "max_price": Decimal("0.4")},
        {"leg": "YES", "side": "SELL", "token_id": "yes-token", "shares": Decimal("0"), "min_price": Decimal("0.4")},
    ),
)
def test_remediation_rejects_invalid_leg_or_zero_order_without_post(
    order: dict[str, object],
) -> None:
    adapter, fake = make_adapter()

    result = adapter.submit_remediation_once(order)

    assert result.status == "blocked"
    assert result.error_code == "invalid"
    assert fake.create_calls == []
    assert fake.post_calls == []


class FakePublicClient:
    def __init__(self) -> None:
        self.markets = [
            SimpleNamespace(
                id="empty-book",
                condition_id="condition-empty",
                state=SimpleNamespace(
                    active=True,
                    closed=False,
                    archived=False,
                    accepting_orders=True,
                    enable_order_book=True,
                    neg_risk=False,
                ),
                outcomes=SimpleNamespace(
                    yes=SimpleNamespace(token_id="empty-yes"),
                    no=SimpleNamespace(token_id="empty-no"),
                ),
                metrics=SimpleNamespace(volume_24hr=Decimal("500")),
                trading=SimpleNamespace(
                    minimum_order_size=Decimal("1"),
                    minimum_tick_size=Decimal("0.01"),
                    fees_enabled=False,
                ),
            ),
            SimpleNamespace(
                id="eligible",
                condition_id="condition-eligible",
                state=SimpleNamespace(
                    active=True,
                    closed=False,
                    archived=False,
                    accepting_orders=True,
                    enable_order_book=True,
                    neg_risk=False,
                ),
                outcomes=SimpleNamespace(
                    yes=SimpleNamespace(token_id="yes-token"),
                    no=SimpleNamespace(token_id="no-token"),
                ),
                metrics=SimpleNamespace(volume_24hr=Decimal("100")),
                trading=SimpleNamespace(
                    minimum_order_size=Decimal("1"),
                    minimum_tick_size=Decimal("0.01"),
                    fees_enabled=False,
                ),
            ),
            SimpleNamespace(
                id="neg-risk",
                condition_id="condition-neg-risk",
                state=SimpleNamespace(
                    active=True,
                    closed=False,
                    archived=False,
                    accepting_orders=True,
                    enable_order_book=True,
                    neg_risk=True,
                ),
                outcomes=SimpleNamespace(
                    yes=SimpleNamespace(token_id="neg-yes"),
                    no=SimpleNamespace(token_id="neg-no"),
                ),
                metrics=SimpleNamespace(volume_24hr=Decimal("1000")),
                trading=SimpleNamespace(
                    minimum_order_size=Decimal("1"),
                    minimum_tick_size=Decimal("0.01"),
                    fees_enabled=False,
                ),
            ),
        ]

    def list_markets(self, **kwargs: object) -> object:
        return SimpleNamespace(
            first_page=lambda: SimpleNamespace(items=tuple(self.markets))
        )

    def get_order_book(self, *, token_id: str) -> object:
        if token_id == "empty-yes":
            return SimpleNamespace(
                asks=(),
                min_order_size=Decimal("1"),
                tick_size=Decimal("0.01"),
            )
        return SimpleNamespace(
            asks=(SimpleNamespace(price=Decimal("0.45" if token_id == "yes-token" else "0.48"), size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
        )


class RemediationClock(datetime):
    """``datetime`` stand-in whose ``now`` serves one frozen real instant.

    Remediation collaborators stamp order books at request time and the
    production freshness gate re-reads the clock afterwards; a host wall-clock
    rollback between the two reads turns ``age < 0`` into a false staleness
    rejection of a fresh book.  Subclassing keeps the production module's
    ``isinstance`` checks and ``fromtimestamp`` / ``fromisoformat`` working
    unchanged, while ``now`` serves one frozen real ``datetime`` instance so
    every timestamp on the tested path is identical and independent of the
    host clock.
    """

    frozen_instant: datetime
    now_calls = 0

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        cls.now_calls += 1
        return cls._from_moment(cls.frozen_instant)

    @classmethod
    def _from_moment(cls, moment: datetime) -> "RemediationClock":
        return cls(
            moment.year,
            moment.month,
            moment.day,
            moment.hour,
            moment.minute,
            moment.second,
            moment.microsecond,
            tzinfo=moment.tzinfo,
        )


def freeze_remediation_clock(monkeypatch: pytest.MonkeyPatch) -> type[RemediationClock]:
    """Route the remediation path's clock reads through one frozen instant.

    Patches both the production module and this test module so the ``since``
    computation, the fake collaborators' request-time stamps, and
    ``open_trader.polymarket_trading`` all observe the same clock.
    """

    clock = RemediationClock
    clock.frozen_instant = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)
    clock.now_calls = 0
    monkeypatch.setattr(polymarket_trading, "datetime", clock)
    monkeypatch.setattr(sys.modules[__name__], "datetime", clock)
    return clock


class FakeRemediationPublicClient(FakePublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        price = Decimal("0.12" if token_id == "no-token" else "0.15")
        return SimpleNamespace(
            asks=(SimpleNamespace(price=price, size=Decimal("100")),),
            bids=(SimpleNamespace(price=price, size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            timestamp=datetime.now(UTC),
        )


class CrossRemediationPublicClient:
    def get_order_book(self, *, token_id: str) -> object:
        assert token_id == "cross-no-token"
        return SimpleNamespace(
            asks=(SimpleNamespace(price=Decimal("0.18"), size=Decimal("5")),),
            bids=(SimpleNamespace(price=Decimal("0.82"), size=Decimal("5")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            timestamp=datetime.now(UTC),
        )


class StaleRemediationPublicClient(FakeRemediationPublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        book = super().get_order_book(token_id=token_id)
        book.timestamp = datetime.now(UTC) - timedelta(seconds=11)  # type: ignore[attr-defined]
        return book


class MissingRemediationTimestampPublicClient(FakeRemediationPublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        book = super().get_order_book(token_id=token_id)
        delattr(book, "timestamp")
        return book


class MalformedRemediationTimestampPublicClient(FakeRemediationPublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        book = super().get_order_book(token_id=token_id)
        book.timestamp = "not-a-timestamp"  # type: ignore[attr-defined]
        return book


class FutureRemediationTimestampPublicClient(FakeRemediationPublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        book = super().get_order_book(token_id=token_id)
        book.timestamp = datetime.now(UTC) + timedelta(seconds=1)  # type: ignore[attr-defined]
        return book


class NumericRemediationTimestampPublicClient(FakeRemediationPublicClient):
    def get_order_book(self, *, token_id: str) -> object:
        book = super().get_order_book(token_id=token_id)
        book.timestamp = str(int(datetime.now(UTC).timestamp() * 1000))  # type: ignore[attr-defined]
        return book


def test_preflight_report_discovers_standard_fee_free_probe_without_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter(FakeClient())
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=fake,
        public_client_factory=FakePublicClient,
    )
    report = adapter.preflight_report()

    assert report["signer_match"] == "yes"
    assert report["wallet_match"] == "yes"
    assert report["geoblock"] == "allowed"
    assert report["account_reads"] == "pass"
    assert report["fok_pair_signed_not_submitted"] == "pass"
    assert report["equal_requested_shares"] == "pass"
    assert report["merge_capability"] == "present_not_invoked"
    assert report["relayer_readiness"] == "pass"
    assert report["secret_scan"] == "pass"
    assert report["result"] == "PASS"
    assert fake.post_calls == []


def test_remediation_options_are_fresh_bounded_and_exact_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = freeze_remediation_clock(monkeypatch)
    adapter, fake = make_adapter()
    fake.position_rows = [
        {"condition_id": "condition-1", "token_id": "yes-token", "size": Decimal("10")}
    ]
    fake.list_open_orders = lambda **kwargs: []  # type: ignore[method-assign]
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=fake,
        public_client_factory=FakeRemediationPublicClient,
    )

    result = adapter.remediation_options(
        condition_id="condition-1",
        yes_token_id="yes-token",
        no_token_id="no-token",
        filled_leg="YES",
        filled_quantity=Decimal("10"),
        since=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result["fresh"] is True
    assert clock.now_calls > 0
    complete = result["complete"]
    assert complete["leg"] == "NO"
    assert complete["side"] == "BUY"
    assert complete["quantity"] == Decimal("10")
    assert complete["amount"] == complete["max_spend"]
    assert complete["loss"] <= Decimal("2")
    assert fake.post_calls == []


def test_cross_remediation_options_bind_a_fresh_book_to_the_exact_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = freeze_remediation_clock(monkeypatch)
    adapter, fake = make_adapter()
    fake.position_rows = [
        {"condition_id": "condition-cross", "token_id": "cross-no-token", "size": Decimal("5")}
    ]
    fake.list_open_orders = lambda **kwargs: []  # type: ignore[method-assign]
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET), client=fake,
        public_client_factory=CrossRemediationPublicClient,
    )
    leg = cross_polymarket_leg()

    buy = adapter.cross_remediation_option(
        venue="polymarket", market_id=leg.market_id, condition_id=leg.condition_id,
        token_id=leg.token_id, outcome=leg.outcome, side="BUY",
        quantity=leg.net_quantity, maximum_fee=leg.maximum_fee,
    )
    sell = adapter.cross_remediation_option(
        venue="polymarket", market_id=leg.market_id, condition_id=leg.condition_id,
        token_id=leg.token_id, outcome=leg.outcome, side="SELL",
        quantity=leg.net_quantity, maximum_fee=leg.maximum_fee,
    )

    assert buy["fresh"] is True
    assert buy["option"]["max_spend"] == Decimal("0.95")
    assert sell["fresh"] is True
    assert sell["option"]["min_price"] == Decimal("0.82")
    assert clock.now_calls > 0
    assert fake.post_calls == []


@pytest.mark.parametrize(
    "public_factory",
    [
        StaleRemediationPublicClient,
        MissingRemediationTimestampPublicClient,
        MalformedRemediationTimestampPublicClient,
        FutureRemediationTimestampPublicClient,
    ],
)
def test_remediation_options_reject_stale_or_invalid_book_timestamps(
    public_factory: type[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = freeze_remediation_clock(monkeypatch)
    adapter, fake = make_adapter()
    fake.position_rows = [
        {"condition_id": "condition-1", "token_id": "yes-token", "size": Decimal("10")}
    ]
    fake.list_open_orders = lambda **kwargs: []  # type: ignore[method-assign]
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=fake,
        public_client_factory=public_factory,
    )

    result = adapter.remediation_options(
        condition_id="condition-1",
        yes_token_id="yes-token",
        no_token_id="no-token",
        filled_leg="YES",
        filled_quantity=Decimal("10"),
        since=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result == {"fresh": False}
    assert clock.now_calls > 0
    assert fake.post_calls == []


def test_remediation_options_accepts_fresh_numeric_string_book_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = freeze_remediation_clock(monkeypatch)
    adapter, fake = make_adapter()
    fake.position_rows = [
        {"condition_id": "condition-1", "token_id": "yes-token", "size": Decimal("10")}
    ]
    fake.list_open_orders = lambda **kwargs: []  # type: ignore[method-assign]
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=fake,
        public_client_factory=NumericRemediationTimestampPublicClient,
    )

    result = adapter.remediation_options(
        condition_id="condition-1",
        yes_token_id="yes-token",
        no_token_id="no-token",
        filled_leg="YES",
        filled_quantity=Decimal("10"),
        since=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result["fresh"] is True
    assert clock.now_calls > 0
    assert isinstance(result["checked_at"], datetime)
    assert result["checked_at"] == clock.frozen_instant
    assert (datetime.now(UTC) - result["checked_at"]).total_seconds() < 10
    assert fake.post_calls == []


def test_preflight_report_uses_explicit_readiness_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, fake = make_adapter(FakeClient(gasless_ready=False))
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

    report = adapter.preflight_report()

    assert report["merge_capability"] == "present_not_invoked"
    assert report["relayer_readiness"] == "fail"
    assert report["result"] == "BLOCKED"
    assert fake.gasless_calls == 1
    assert fake.post_calls == []


def test_cli_preflight_prints_safe_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"signer_address": SIGNER, "wallet_address": WALLET}), encoding="utf-8")
    report = {
        "sdk_version": "0.2.0",
        "signer_match": "yes",
        "wallet_match": "yes",
        "geoblock": "allowed",
        "account_reads": "pass",
        "fok_pair_signed_not_submitted": "pass",
        "equal_requested_shares": "pass",
        "merge_capability": "present_not_invoked",
        "relayer_readiness": "pass",
        "secret_scan": "pass",
        "result": "PASS",
    }

    class FakeAdapter:
        def preflight_report(self) -> dict[str, object]:
            return report

    monkeypatch.setattr(cli.PolymarketTradingClient, "from_keychain", lambda config: FakeAdapter())
    monkeypatch.setattr(cli, "load_trading_config", lambda path: TradingConfig(SIGNER, WALLET))

    assert cli.main(["prediction-arb", "preflight", "--config", str(config), "--no-submit"]) == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert output == [f"{key}: {value}" for key, value in report.items()]


def test_prediction_wallet_help_has_no_secret_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["prediction-arb", "wallet", "setup", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out.lower()
    assert "--private-key" not in output
    assert "--secret" not in output
    assert "--signer-address" in output


def test_wallet_setup_writes_only_addresses_and_uses_keychain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.json"
    secrets = iter(("secret-sentinel", "builder-key", "builder-secret", "passphrase"))
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "getpass", lambda prompt: next(secrets))
    monkeypatch.setattr(cli, "store_keychain_secret", lambda account, secret: stored.append((account, secret)))

    assert cli.main(
        [
            "prediction-arb",
            "wallet",
            "setup",
            "--config",
            str(config_path),
            "--signer-address",
            SIGNER,
            "--wallet-address",
            WALLET,
        ]
    ) == 0

    assert config_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "signer_address": SIGNER,
        "wallet_address": WALLET,
    }
    assert stored == list(zip(("signing-private-key", "builder-key", "builder-secret", "builder-passphrase"), ("secret-sentinel", "builder-key", "builder-secret", "passphrase"), strict=True))
    assert "secret-sentinel" not in capsys.readouterr().out


def test_preflight_requires_explicit_no_submit(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["prediction-arb", "preflight"]) == 2
    assert "--no-submit" in capsys.readouterr().err


def test_lp_books_batch_receipts_preserve_unchanged_source_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = datetime.now(UTC)
    source_time = (base_time - timedelta(minutes=10)).isoformat()

    class ReceiptClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            moment = base_time + timedelta(seconds=cls.calls)
            cls.calls += 1
            return moment

    class BatchPublicClient:
        def __init__(self, mode: str = "complete") -> None:
            self.mode = mode
            self.batches: list[tuple[str, ...]] = []

        def get_order_books(self, *, token_ids: tuple[str, ...]) -> list[object]:
            self.batches.append(tuple(token_ids))
            rows = []
            for index, token_id in enumerate(token_ids):
                if self.mode == "missing" and index == 0:
                    continue
                returned_token = (
                    "unrequested-token"
                    if self.mode == "wrong_identity" and index == 0
                    else token_id
                )
                rows.append(
                    {
                        "condition_id": f"condition-{token_id}",
                        "token_id": returned_token,
                        "timestamp": source_time,
                        "bids": [{"price": Decimal("0.40"), "size": Decimal("10")}],
                        "asks": [{"price": Decimal("0.60"), "size": Decimal("11")}],
                    }
                )
            return rows

        def close(self) -> None:
            return None

    tokens = tuple(f"token-{index:03d}" for index in range(201))
    public = BatchPublicClient()
    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=object(),
        public_client_factory=lambda: public,
    )
    monkeypatch.setattr(polymarket_trading, "datetime", ReceiptClock)
    books = adapter.lp_order_books(tokens)

    assert set(books) == set(tokens)
    assert len(public.batches) == 3
    assert all(0 < len(batch) <= 100 for batch in public.batches)
    for token_id, book in books.items():
        batch_index = int(token_id.removeprefix("token-")) // 100
        assert book["token_id"] == token_id
        assert book["condition_id"] == f"condition-{token_id}"
        assert book["source_timestamp"] == source_time
        assert book["received_at"] == base_time + timedelta(seconds=batch_index)

    missing_public = BatchPublicClient("missing")
    missing_adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=object(),
        public_client_factory=lambda: missing_public,
    )
    missing_books = missing_adapter.lp_order_books(tokens[:3])
    assert "token-000" not in missing_books

    wrong_public = BatchPublicClient("wrong_identity")
    wrong_adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=object(),
        public_client_factory=lambda: wrong_public,
    )
    wrong_books = wrong_adapter.lp_order_books(tokens[:3])
    assert "token-000" not in wrong_books
    assert "unrequested-token" not in wrong_books


def test_lp_metadata_preserves_event_evidence_and_market_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections import Counter
    from http.client import InvalidURL

    from polymarket.models.gamma.event import Event
    from polymarket.models.gamma.market import Market

    start_time = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    finished_at = datetime(2026, 9, 15, 18, 4, 12, tzinfo=UTC)
    event_id = "event-42"
    event_slug = "world-cup"
    expected_market_url = f"https://polymarket.com/event/{event_slug}/final-winner"
    condition_event = "0x" + "1" * 64
    condition_event_other = "0x" + "2" * 64
    condition_game = "0x" + "3" * 64
    condition_mismatch = "0x" + "4" * 64
    condition_dated = "0x" + "5" * 64

    def parsed_market(
        market_id: str,
        condition_id: str,
        slug: str,
        *,
        linked_event: bool,
        numeric_event_id: int | None = None,
        event_reference_id: str | None = None,
    ) -> Market:
        event_reference = (
            {
                "id": event_reference_id or str(numeric_event_id),
                "slug": f"bulk-event-{numeric_event_id}",
            }
            if numeric_event_id is not None
            else {"id": event_id, "slug": event_slug, "title": "World Cup"}
        )
        return Market.parse_response(
            {
                "id": market_id,
                "conditionId": condition_id,
                "slug": slug,
                "question": f"Will {slug} resolve Yes?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.50", "0.50"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "endDate": "2026-09-16T00:00:00Z",
                "gameStartTime": (
                    "2026-09-15T16:00:00Z" if linked_event else None
                ),
                "oneDayPriceChange": "0.12",
                "events": [event_reference] if linked_event else [],
            }
        )

    linked_markets = (
        parsed_market(
            "market-event", condition_event, "final-winner", linked_event=True
        ),
        parsed_market(
            "market-event-other",
            condition_event_other,
            "top-scorer",
            linked_event=True,
        ),
    )
    game_market = Market.parse_response(
        {
            "id": "market-game",
            "conditionId": condition_game,
            "slug": "game-winner",
            "question": "Will the home team win?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["game-yes", "game-no"]',
            "gameId": "game-7",
            "gameStartTime": "2026-09-15T16:00:00Z",
            "events": [],
        }
    )
    mismatch_market = Market.parse_response(
        {
            "id": "market-mismatch",
            "conditionId": condition_mismatch,
            "slug": "mismatch-market",
            "question": "Will this market resolve Yes?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["mismatch-yes", "mismatch-no"]',
            "endDate": "2026-09-16T00:00:00Z",
            "events": [{"id": "event-mismatch", "slug": "wrong-event"}],
        }
    )
    dated_market = parsed_market(
        "market-dated", condition_dated, "dated-market", linked_event=False
    )
    event_markets = (*linked_markets, game_market, mismatch_market, dated_market)
    event_record = Event.parse_response(
        {
            "id": event_id,
            "slug": event_slug,
            "title": "World Cup",
            "startTime": "2026-09-15T16:00:00Z",
            "ended": True,
            "finishedTimestamp": "2026-09-15T18:04:12Z",
            "markets": [],
        }
    )
    leading_zero_mismatch_record = Event.parse_response(
        {
            "id": "1001",
            "slug": "bulk-event-1001",
            "title": "Bulk event 1001",
            "startTime": "2026-09-15T16:00:00Z",
            "ended": True,
            "closed": True,
            "finishedTimestamp": "2026-09-15T18:04:12Z",
            "markets": [],
        }
    )

    bulk_conditions: list[str] = []
    bulk_market_records: list[Market] = []
    bulk_event_ids: list[int] = []
    bulk_event_records: dict[int, Event] = {}
    for index in range(196):
        condition_id = f"0x{index + 6:064x}"
        if index == 0:
            numeric_event_id = 1000
        elif index in (1, 3):
            numeric_event_id = 1001
        elif index in (2, 4):
            numeric_event_id = 1002
        else:
            numeric_event_id = 1000 + index - 2
        bulk_conditions.append(condition_id)
        bulk_event_ids.append(numeric_event_id)
        bulk_market_records.append(
            parsed_market(
                f"market-bulk-{index}",
                condition_id,
                f"bulk-market-{index}",
                linked_event=True,
                numeric_event_id=numeric_event_id,
                event_reference_id=(
                    f"0{numeric_event_id}" if index == 3 else None
                ),
            )
        )
        if numeric_event_id not in bulk_event_records:
            is_closed = numeric_event_id in (1001, 1003)
            bulk_event_records[numeric_event_id] = Event.parse_response(
                {
                    "id": (
                        "01000"
                        if numeric_event_id == 1000
                        else " 1003 "
                        if numeric_event_id == 1003
                        else str(numeric_event_id)
                    ),
                    "slug": f"bulk-event-{numeric_event_id}",
                    "title": f"Bulk event {numeric_event_id}",
                    "startTime": "2026-09-15T16:00:00Z",
                    "ended": is_closed,
                    "closed": is_closed,
                    "finishedTimestamp": (
                        "2026-09-15T18:04:12Z" if is_closed else None
                    ),
                    "markets": [],
                }
            )

    all_markets = (*event_markets, *bulk_market_records)
    market_by_condition = {
        str(market.condition_id): market for market in all_markets
    }
    query_lock = threading.Lock()
    clients: list[MetadataPublicClient] = []
    market_queries: list[tuple[tuple[str, ...], int | None]] = []
    event_queries: list[tuple[tuple[int, ...], bool, int | None]] = []
    get_event_calls: list[str] = []
    paginators: list[object] = []

    metadata_read_started_at = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    later_event_read_at = datetime(2026, 9, 15, 19, 0, tzinfo=UTC)

    class PagedRows:
        def __init__(self, rows: tuple[object, ...]) -> None:
            self.rows = rows
            self.complete = False
            with query_lock:
                paginators.append(self)

        def iter_items(self):
            try:
                for offset in range(0, len(self.rows), 37):
                    yield from self.rows[offset : offset + 37]
            finally:
                self.complete = True

    class MetadataPublicClient:
        def __init__(self) -> None:
            self.closed = False
            with query_lock:
                clients.append(self)

        def list_markets(
            self,
            *,
            condition_ids: tuple[str, ...],
            page_size: int | None = None,
        ) -> PagedRows:
            assert self.closed is False
            requested_ids = tuple(condition_ids)
            with query_lock:
                market_queries.append((requested_ids, page_size))
            if len(requested_ids) > 100:
                raise InvalidURL("URL component 'query' too long")
            assert page_size == 100
            return PagedRows(
                tuple(
                    market_by_condition[condition_id]
                    for condition_id in requested_ids
                    if condition_id in market_by_condition
                )
            )

        def list_events(
            self,
            *,
            ids: tuple[int, ...],
            closed: bool,
            page_size: int | None = None,
        ) -> PagedRows:
            assert self.closed is False
            requested_ids = tuple(ids)
            with query_lock:
                event_queries.append((requested_ids, closed, page_size))
            if len(requested_ids) > 100:
                raise InvalidURL("URL component 'query' too long")
            assert page_size == 100
            assert len(requested_ids) == len(set(requested_ids))
            return PagedRows(
                tuple(
                    bulk_event_records[event_id]
                    for event_id in requested_ids
                    if event_id in bulk_event_records
                    and (event_id == 1001) is closed
                )
            )

        def get_event(self, *, id: str) -> Event:
            assert self.closed is False
            with query_lock:
                get_event_calls.append(id)
            return (
                leading_zero_mismatch_record
                if id == "01001"
                else event_record
            )

        def close(self) -> None:
            self.closed = True

    class MetadataClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            value = (
                metadata_read_started_at
                if not market_queries and not event_queries and not get_event_calls
                else later_event_read_at
            )
            return value if tz is None else value.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(polymarket_trading, "datetime", MetadataClock)

    adapter = PolymarketTradingClient(
        TradingConfig(SIGNER, WALLET),
        client=object(),
        public_client_factory=MetadataPublicClient,
    )

    requested_conditions = (
        condition_event,
        condition_event_other,
        condition_game,
        condition_mismatch,
        condition_dated,
        *bulk_conditions,
    )
    metadata = adapter.lp_market_metadata(
        requested_conditions
    )

    assert len(metadata) == 201
    assert set(metadata) == set(requested_conditions)
    direct_event_call_counts = Counter(get_event_calls)
    assert direct_event_call_counts[event_id] == 1
    assert direct_event_call_counts["event-mismatch"] == 1
    assert direct_event_call_counts["01001"] <= 1
    assert set(direct_event_call_counts) <= {
        event_id,
        "event-mismatch",
        "01001",
    }
    assert market_queries
    assert all(0 < len(ids) <= 100 for ids, _page_size in market_queries)
    assert all(page_size == 100 for _ids, page_size in market_queries)
    queried_market_ids = [
        condition_id for ids, _page_size in market_queries for condition_id in ids
    ]
    assert len(queried_market_ids) == 201
    assert set(queried_market_ids) == set(requested_conditions)
    assert event_queries
    assert all(0 < len(ids) <= 100 for ids, _closed, _page_size in event_queries)
    assert all(page_size == 100 for _ids, _closed, page_size in event_queries)
    assert {closed for _ids, closed, _page_size in event_queries} == {False, True}
    assert len(event_queries) <= 4
    queried_event_ids_by_mode = {
        closed: [
            event_id
            for ids, query_closed, _page_size in event_queries
            if query_closed is closed
            for event_id in ids
        ]
        for closed in (False, True)
    }
    assert all(
        len(ids) == len(set(ids)) for ids in queried_event_ids_by_mode.values()
    )
    assert set(bulk_event_ids) <= {
        event_id
        for ids in queried_event_ids_by_mode.values()
        for event_id in ids
    }
    assert all(getattr(paginator, "complete") for paginator in paginators)
    assert clients
    assert all(client.closed is True for client in clients)
    assert all(
        row["metadata_checked_at"] == metadata_read_started_at
        for row in metadata.values()
    )

    observed = metadata[condition_event]
    assert observed["metadata_checked_at"] == metadata_read_started_at
    assert observed["market_url"] == expected_market_url
    assert observed["event_id"] == event_id
    assert observed["game_start_time"] == start_time
    assert observed["event_start_time"] == start_time
    assert observed["event_ended"] is True
    assert observed["event_finished_at"] == finished_at
    assert observed["price_change_24h"] == Decimal("0.12")
    assert (
        observed["price_change_24h_source"]
        == "polymarket.prices.one_day_price_change"
    )

    repeated = metadata[condition_event_other]
    assert repeated["metadata_checked_at"] == metadata_read_started_at
    assert repeated["event_id"] == event_id
    assert repeated["market_url"] == (
        "https://polymarket.com/event/world-cup/top-scorer"
    )
    assert repeated["event_start_time"] == start_time
    assert repeated["price_change_24h"] == Decimal("0.12")

    game = metadata[condition_game]
    assert game["event_id"] is None
    assert game["game_id"] == "game-7"
    assert game["game_start_time"] == start_time

    mismatched = metadata[condition_mismatch]
    assert mismatched["event_id"] == "event-mismatch"
    assert mismatched["event_start_time"] is None
    assert mismatched["event_ended"] is None
    assert mismatched["event_finished_at"] is None

    dated = metadata[condition_dated]
    assert dated.get("event_id") is None
    assert dated.get("event_start_time") is None
    assert dated.get("game_start_time") is None

    closed_event = metadata[bulk_conditions[1]]
    assert closed_event["event_id"] == "1001"
    assert closed_event["event_ended"] is True
    assert closed_event["event_finished_at"] == finished_at

    shared_bulk_event = metadata[bulk_conditions[2]]
    assert shared_bulk_event["event_id"] == "1002"
    assert shared_bulk_event["event_start_time"] == start_time
    assert metadata[bulk_conditions[4]]["event_id"] == "1002"
    mismatched_bulk_event = metadata[bulk_conditions[0]]
    assert mismatched_bulk_event["event_id"] == "1000"
    assert mismatched_bulk_event["event_start_time"] is None
    assert mismatched_bulk_event["event_ended"] is None
    assert mismatched_bulk_event["event_finished_at"] is None
    leading_zero_mismatch = metadata[bulk_conditions[3]]
    assert leading_zero_mismatch["event_id"] == "01001"
    assert leading_zero_mismatch["event_start_time"] is None
    assert leading_zero_mismatch["event_ended"] is None
    assert leading_zero_mismatch["event_finished_at"] is None
    padded_event = metadata[bulk_conditions[5]]
    assert padded_event["event_id"] == "1003"
    assert padded_event["event_start_time"] is None
    assert padded_event["event_ended"] is None
    assert padded_event["event_finished_at"] is None
    assert str(bulk_event_records[1003].id) == " 1003 "

    stopped_before_clients = len(clients)
    stopped_before_queries = len(market_queries) + len(event_queries)
    stopped = threading.Event()
    stopped.set()
    assert adapter.lp_market_metadata(requested_conditions, stop_event=stopped) == {}
    assert len(clients) == stopped_before_clients
    assert len(market_queries) + len(event_queries) == stopped_before_queries
