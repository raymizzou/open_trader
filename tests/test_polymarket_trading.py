from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

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


@dataclass
class FakeSignedOrder:
    token_id: str
    taker_amount: int
    order_type: str = "FOK"
    side: str = "BUY"
    signature: str = "signature-sentinel"


class FakeClient:
    def __init__(self, *, taker_scale: int = 1_000_000) -> None:
        self.taker_scale = taker_scale
        self.create_calls: list[dict[str, object]] = []
        self.post_calls: list[tuple[object, ...]] = []
        self.read_calls: list[str] = []
        self.cancel_calls: list[tuple[str, ...]] = []
        self.merge_calls: list[dict[str, object]] = []
        self.post_error: Exception | None = None
        self.bad_taker = False

    def create_market_order(self, **kwargs: object) -> FakeSignedOrder:
        self.create_calls.append(kwargs)
        amount = kwargs["amount"]
        assert isinstance(amount, Decimal)
        quantity = Decimal("20") if not self.bad_taker else Decimal("19")
        return FakeSignedOrder(
            token_id=str(kwargs["token_id"]),
            taker_amount=int(quantity * self.taker_scale),
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
        return SimpleNamespace(balance=20_000_000, allowances={"spender": 18_600_000})

    def list_open_orders(self, **kwargs: object) -> list[object]:
        self.read_calls.append("orders")
        return [SimpleNamespace(id="open-order")]

    def list_account_trades(self, **kwargs: object) -> list[object]:
        self.read_calls.append("trades")
        return [SimpleNamespace(id="trade")]

    def list_positions(self, **kwargs: object) -> list[object]:
        self.read_calls.append("positions")
        return [{"condition_id": "condition-1", "size": "20"}]

    def cancel_orders(self, **kwargs: object) -> object:
        order_ids = tuple(kwargs["order_ids"])
        self.cancel_calls.append(order_ids)
        return SimpleNamespace(canceled=order_ids)

    def merge_positions(self, **kwargs: object) -> object:
        self.merge_calls.append(kwargs)
        return SimpleNamespace(wait=lambda: "confirmed")


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


def test_submit_pair_posts_two_signed_orders_once_and_preserves_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake = make_adapter()
    monkeypatch.setattr(
        "open_trader.polymarket_trading.urlopen",
        lambda *args, **kwargs: FakeResponse({"blocked": False}),
    )

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


def test_cancel_and_merge_use_official_methods_once() -> None:
    adapter, fake = make_adapter()

    assert adapter.cancel_orders(("one", "two")) == ("one", "two")
    merged = adapter.merge_once(condition_id="condition-1", quantity=Decimal("20"))

    assert fake.cancel_calls == [("one", "two")]
    assert fake.merge_calls == [{"condition_id": "condition-1", "amount": 20_000_000}]
    assert merged["status"] == "confirmed"


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
