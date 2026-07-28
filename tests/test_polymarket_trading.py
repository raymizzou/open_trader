from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
from polymarket import SecureClient

import open_trader.cli as cli
from open_trader.polymarket_trading import (
    KEYCHAIN_SERVICE,
    PolymarketTradingClient,
    TradingConfig,
    load_keychain_secret,
    load_trading_config,
    store_keychain_secret,
)
from open_trader.prediction_arbitrage import PairIntent


SIGNER = "0x1111111111111111111111111111111111111111"
WALLET = "0x2222222222222222222222222222222222222222"


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
        wallet_type="EOA",
        wallet=WALLET,
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
        wallet_type="POLY_PROXY",
        wallet=WALLET,
        signer=SimpleNamespace(address=SIGNER),
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


def test_remediation_options_are_fresh_bounded_and_exact_quantity() -> None:
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
    complete = result["complete"]
    assert complete["leg"] == "NO"
    assert complete["side"] == "BUY"
    assert complete["quantity"] == Decimal("10")
    assert complete["amount"] == complete["max_spend"]
    assert complete["loss"] <= Decimal("2")
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
) -> None:
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
    assert fake.post_calls == []


def test_remediation_options_accepts_fresh_numeric_string_book_timestamps() -> None:
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
    assert isinstance(result["checked_at"], datetime)
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
