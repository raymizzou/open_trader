from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from open_trader import prediction_arbitrage_acceptance as acceptance
from open_trader.polymarket_trading import PredictConfig, TradingConfig


CONFIG = TradingConfig(
    signer_address="0x1111111111111111111111111111111111111111",
    wallet_address="0x2222222222222222222222222222222222222222",
    predict=PredictConfig(wallet_address="0x3333333333333333333333333333333333333333"),
)


@dataclass(frozen=True)
class Market:
    market_id: str = "predict-market"
    yes_token_id: str = "predict-yes"
    minimum_order_size: Decimal = Decimal("1")


class PredictSource:
    async def list_open_markets(self) -> tuple[Market, ...]:
        return (Market(),)

    async def get_order_book(self, market_id: str) -> object:
        assert market_id == "predict-market"
        return object()

    async def stream_books(self, market_ids: list[str]):
        assert market_ids == ["predict-market"]
        yield object()


class Builder:
    def get_approval_steps(self, scope: object) -> tuple[object, ...]:
        return (scope,)

    def check_approvals(self, steps: tuple[object, ...]) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(satisfied=True),)

    def balance_of(self, asset: str) -> Decimal:
        assert asset == "USDT"
        return Decimal("10")


class PredictClient:
    _builder = Builder()

    def _authenticate(self) -> str:
        return "jwt-sentinel"

    def no_submit_buy_preflight(
        self, market_id: str, token_id: str, quantity_wei: int
    ) -> SimpleNamespace:
        assert (market_id, token_id, quantity_wei) == (
            "predict-market",
            "predict-yes",
            10**18,
        )
        return SimpleNamespace(accepted=True, status="preflight", error_code="none")


class PolymarketClient:
    def preflight_report(self) -> dict[str, object]:
        return {
            "result": "PASS",
            "account_reads": "pass",
            "fok_pair_signed_not_submitted": "pass",
            "posted": False,
        }


def readiness_report(tmp_path: Path, **overrides: object):
    config_path = tmp_path / "prediction.json"
    config_path.write_text("{}", encoding="utf-8")
    factories: dict[str, object] = {
        "load_config": lambda _: CONFIG,
        "predict_source_factory": lambda _config, *, urlopen_fn: PredictSource(),
        "predict_client_factory": lambda _config, *, urlopen_fn: PredictClient(),
        "polymarket_client_factory": lambda _config: PolymarketClient(),
    }
    factories.update(overrides)
    return acceptance.run_live_readiness(config_path, **factories)


def test_readiness_report_distinguishes_all_no_submit_facts(tmp_path: Path) -> None:
    """A missing sub-check must not collapse into the old generic live PASS."""

    report = readiness_report(tmp_path)

    assert report.status == "PASS"
    assert report.predict_market.status == "PASS"
    assert report.predict_account.status == "PASS"
    assert report.predict_preflight.status == "PASS"
    assert report.polymarket.status == "PASS"
    assert report.safety.status == "PASS"
    assert report.as_dict() == {
        "predict": {
            "market_book_rest_ws": {"status": "PASS", "detail": "Predict REST and WebSocket market/book ready"},
            "account_jwt_balance_allowance": {"status": "PASS", "detail": "Predict JWT, balance, and allowance ready"},
            "signed_not_submitted_preflight": {"status": "PASS", "detail": "Predict order signed but not submitted"},
        },
        "polymarket": {
            "source_account_preflight": {"status": "PASS", "detail": "Polymarket source, account, and no-submit preflight ready"},
        },
        "safety": {
            "zero_mutation_calls": 0,
            "zero_live_notifications": 0,
            "status": "PASS",
            "detail": "zero mutation calls; zero live notifications",
        },
        "status": "PASS",
    }


def test_missing_configuration_is_blocked_not_fixture_pass(tmp_path: Path) -> None:
    report = acceptance.run_live_readiness(tmp_path / "missing.json")

    assert report.status == "BLOCKED"
    assert report.predict_market.status == "BLOCKED"
    assert report.predict_account.status == "BLOCKED"
    assert report.predict_preflight.status == "BLOCKED"
    assert report.polymarket.status == "BLOCKED"


def test_auth_read_failure_is_fail_and_redacts_secret_text(tmp_path: Path) -> None:
    class FailingPredictClient(PredictClient):
        def _authenticate(self) -> str:
            raise RuntimeError("jwt-sentinel private-key-sentinel")

    report = readiness_report(
        tmp_path,
        predict_client_factory=lambda _config, *, urlopen_fn: FailingPredictClient(),
    )
    rendered = str(report.as_dict())

    assert report.status == "FAIL"
    assert report.predict_account.status == "FAIL"
    assert report.predict_account.detail == "FAIL: predict account read failed"
    assert "sentinel" not in rendered


def test_read_only_transport_rejects_order_endpoint_and_counts_no_delivery() -> None:
    transport = acceptance.ReadOnlyTransport(lambda request, **kwargs: object())

    with pytest.raises(RuntimeError, match="mutation prohibited"):
        transport(Request("https://api.predict.fun/v1/orders", method="POST"), timeout=1)

    assert transport.mutation_calls == 1
    assert transport.live_notifications == 0
