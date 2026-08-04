from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import time
from urllib.error import URLError
from urllib.request import Request

import pytest

from open_trader import prediction_arbitrage_acceptance as acceptance
from open_trader.polymarket_trading import (
    PolymarketTradingError,
    PredictConfig,
    TradingConfig,
)


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

    def __init__(self) -> None:
        self.approval_fact_calls: list[tuple[str, int]] = []
        self.account_snapshot_calls = 0

    def _account_facts(self) -> dict[str, object]:
        return {
            "predict_account": CONFIG.predict.wallet_address if CONFIG.predict else "",
            "gas_signer": CONFIG.signer_address,
            "available_usdt": "10",
            "allowance": "0",
            "scope_ready": True,
            "approval_scope": {"operation": "TRADE", "side": "BUY"},
            "bnb_balance": "0.002",
            "required_bnb": "0.001",
            "minimum_top_up_bnb": "0",
        }

    def approval_facts(self, market_id: str, exact_debit_wei: int = 0) -> dict[str, object]:
        self.approval_fact_calls.append((market_id, exact_debit_wei))
        return self._account_facts()

    def account_snapshot(self) -> dict[str, object]:
        self.account_snapshot_calls += 1
        return {
            "wallet_address": CONFIG.predict.wallet_address if CONFIG.predict else "",
            **self._account_facts(),
            "gas_ready": True,
            "allowance_breaker": False,
            "open_orders": (),
            "positions": (),
        }

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
    def __init__(self) -> None:
        self._client = SimpleNamespace()

    def preflight_report(self) -> dict[str, object]:
        return {
            "result": "PASS",
            "account_reads": "pass",
            "fok_pair_signed_not_submitted": "pass",
            "posted": False,
        }


def write_browser_handoff(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "browser-handoff.json"
    payload: dict[str, object] = {
        "schema_version": 2,
        "source": "playwright",
        "playwright_status": "passed",
        "browser_project": "chromium",
        "run_nonce": "test-nonce",
        "fixture_url": "http://127.0.0.1:18766",
        "fixture_health_url": "http://127.0.0.1:18766/healthz",
        "review_url": "http://127.0.0.1:8766",
        "health_url": "http://127.0.0.1:8766/healthz",
        "candidate_commit": "test-commit",
        "created_at": 1000.0,
        "expires_at": 1060.0,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_browser_nonce(tmp_path: Path, nonce: str = "test-nonce") -> Path:
    path = tmp_path / "browser-nonce"
    path.write_text(nonce, encoding="utf-8")
    return path


def readiness_report(tmp_path: Path, **overrides: object):
    config_path = tmp_path / "prediction.json"
    config_path.write_text("{}", encoding="utf-8")
    factories: dict[str, object] = {
        "load_config": lambda _: CONFIG,
        "predict_source_factory": lambda _config, *, urlopen_fn: PredictSource(),
        "predict_client_factory": lambda _config, *, urlopen_fn: PredictClient(),
        "polymarket_client_factory": lambda _config: PolymarketClient(),
        "browser_now_fn": lambda: 1001.0,
        "browser_commit_fn": lambda _root: "test-commit",
        "browser_health_fn": lambda _url: True,
    }
    if "browser_handoff_path" not in overrides:
        factories["browser_handoff_path"] = write_browser_handoff(tmp_path)
    if "browser_nonce_path" not in overrides:
        factories["browser_nonce_path"] = write_browser_nonce(tmp_path)
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
            "account_jwt_balance_allowance": {
                "status": "PASS",
                "detail": "Predict JWT, balance, allowance, and signer gas ready",
                "gas_signer": CONFIG.signer_address,
                "bnb_balance": "0.002",
                "required_bnb": "0.001",
                "minimum_top_up_bnb": "0",
            },
            "signed_not_submitted_preflight": {"status": "PASS", "detail": "Predict order signed but not submitted"},
        },
        "polymarket": {
            "source_account_preflight": {"status": "PASS", "detail": "Polymarket source, account, and no-submit preflight ready"},
        },
        "browser": {
            "status": "PASS",
            "detail": "Playwright browser handoff and dashboard health ready",
        },
        "safety": {
            "zero_mutation_calls": 0,
            "zero_live_notifications": 0,
            "status": "PASS",
            "detail": "zero mutation calls; zero live notifications",
        },
        "status": "PASS",
    }


def test_successful_empty_predict_market_scan_is_pass_without_signed_preflight(
    tmp_path: Path,
) -> None:
    class SnapshotOnlyPredictClient(PredictClient):
        def approval_facts(self, market_id: str, exact_debit_wei: int = 0) -> dict[str, object]:
            raise AssertionError((market_id, exact_debit_wei))

    client = SnapshotOnlyPredictClient()

    class EmptyPredictSource(PredictSource):
        async def list_open_markets(self) -> tuple[Market, ...]:
            return ()

    report = readiness_report(
        tmp_path,
        predict_source_factory=lambda _config, *, urlopen_fn: EmptyPredictSource(),
        predict_client_factory=lambda _config, *, urlopen_fn: client,
    )

    assert report.status == "PASS"
    assert report.predict_market.status == "PASS"
    assert report.predict_preflight.status == "NOT_APPLICABLE"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0
    assert client.account_snapshot_calls == 1


def test_predict_account_readiness_uses_exact_approval_facts_not_satisfied_flag(
    tmp_path: Path,
) -> None:
    class LegacyUnsatisfiedBuilder(Builder):
        def check_approvals(self, steps: tuple[object, ...]) -> tuple[SimpleNamespace, ...]:
            del steps
            return (SimpleNamespace(satisfied=False),)

    client = PredictClient()
    client._builder = LegacyUnsatisfiedBuilder()  # type: ignore[method-assign]

    report = readiness_report(
        tmp_path,
        predict_client_factory=lambda _config, *, urlopen_fn: client,
    )

    assert report.status == "PASS"
    assert report.predict_account.status == "PASS"
    assert report.as_dict()["predict"]["account_jwt_balance_allowance"] == {
        "status": "PASS",
        "detail": "Predict JWT, balance, allowance, and signer gas ready",
        "gas_signer": CONFIG.signer_address,
        "bnb_balance": "0.002",
        "required_bnb": "0.001",
        "minimum_top_up_bnb": "0",
    }
    assert report.mutation_calls == 0
    assert report.live_notifications == 0
    assert client.approval_fact_calls == [("predict-market", 0)]
    assert client.account_snapshot_calls == 0


@pytest.mark.parametrize("empty_scan", (False, True))
@pytest.mark.parametrize(
    "overrides",
    (
        {"gas_signer": ""},
        {"bnb_balance": "bad"},
        {"required_bnb": None},
        {"minimum_top_up_bnb": "bad"},
    ),
)
def test_predict_account_readiness_fails_closed_on_missing_or_malformed_gas_facts(
    tmp_path: Path, empty_scan: bool, overrides: dict[str, object]
) -> None:
    class GasFactsPredictClient(PredictClient):
        def _account_facts(self) -> dict[str, object]:
            facts = super()._account_facts()
            facts.update(overrides)
            return facts

    class EmptyPredictSource(PredictSource):
        async def list_open_markets(self) -> tuple[Market, ...]:
            return ()

    report = readiness_report(
        tmp_path,
        predict_source_factory=(
            (lambda _config, *, urlopen_fn: EmptyPredictSource())
            if empty_scan
            else (lambda _config, *, urlopen_fn: PredictSource())
        ),
        predict_client_factory=lambda _config, *, urlopen_fn: GasFactsPredictClient(),
    )

    assert report.status == "FAIL"
    assert report.predict_account.status == "FAIL"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0


@pytest.mark.parametrize(
    "overrides",
    (
        {"predict_account": ""},
        {"wallet_address": ""},
        {"wallet_address": "0x4444444444444444444444444444444444444444"},
    ),
)
def test_empty_predict_account_snapshot_fails_closed_on_missing_or_mismatched_identity(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    class IdentityPredictClient(PredictClient):
        def _account_facts(self) -> dict[str, object]:
            facts = super()._account_facts()
            facts.update(overrides)
            return facts

    class EmptyPredictSource(PredictSource):
        async def list_open_markets(self) -> tuple[Market, ...]:
            return ()

    report = readiness_report(
        tmp_path,
        predict_source_factory=lambda _config, *, urlopen_fn: EmptyPredictSource(),
        predict_client_factory=lambda _config, *, urlopen_fn: IdentityPredictClient(),
    )

    assert report.status == "FAIL"
    assert report.predict_account.status == "FAIL"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0


@pytest.mark.parametrize("empty_scan", (False, True))
def test_predict_account_readiness_fails_closed_on_insufficient_signer_bnb(
    tmp_path: Path, empty_scan: bool
) -> None:
    class LowGasPredictClient(PredictClient):
        def _account_facts(self) -> dict[str, object]:
            facts = super()._account_facts()
            facts["minimum_top_up_bnb"] = "0.001"
            return facts

    class EmptyPredictSource(PredictSource):
        async def list_open_markets(self) -> tuple[Market, ...]:
            return ()

    report = readiness_report(
        tmp_path,
        predict_source_factory=(
            (lambda _config, *, urlopen_fn: EmptyPredictSource())
            if empty_scan
            else (lambda _config, *, urlopen_fn: PredictSource())
        ),
        predict_client_factory=lambda _config, *, urlopen_fn: LowGasPredictClient(),
    )

    assert report.status == "FAIL"
    assert report.predict_account.status == "FAIL"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0


def test_missing_configuration_is_blocked_not_fixture_pass(tmp_path: Path) -> None:
    report = acceptance.run_live_readiness(tmp_path / "missing.json")

    assert report.status == "BLOCKED"
    assert report.predict_market.status == "BLOCKED"
    assert report.predict_account.status == "BLOCKED"
    assert report.predict_preflight.status == "BLOCKED"
    assert report.polymarket.status == "BLOCKED"
    assert report.browser.status == "BLOCKED"


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


@pytest.mark.parametrize(
    "path",
    (
        "/v1/approvals",
        "/v1/cleanup",
        "/v1/orders",
        "/v1/transfers",
        "/v1/redemptions",
    ),
)
def test_read_only_transport_rejects_mutation_endpoint_and_counts_no_delivery(
    path: str,
) -> None:
    transport = acceptance.ReadOnlyTransport(lambda request, **kwargs: object())

    with pytest.raises(RuntimeError, match="mutation prohibited"):
        transport(Request(f"https://api.predict.fun{path}", method="POST"), timeout=1)

    assert transport.mutation_calls == 1
    assert transport.live_notifications == 0


@pytest.mark.parametrize(
    "action",
    (
        "set_approval",
        "cleanup_allowance",
        "post_order",
        "transfer_usdt",
        "redeem_positions",
        "send_notification",
    ),
)
def test_polymarket_mutation_or_notification_attempt_fails_closed(
    tmp_path: Path, action: str
) -> None:
    class MutatingSdk:
        def __getattr__(self, name: str):
            if name != action:
                raise AttributeError(name)

            def mutate(*args: object, **kwargs: object) -> None:
                del args, kwargs

            return mutate

    class MutatingPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = MutatingSdk()

        def preflight_report(self) -> dict[str, object]:
            getattr(self._client, action)()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: MutatingPolymarket(),
    )

    assert report.status == "FAIL"
    assert report.polymarket.status == "FAIL"
    assert report.safety.status == "FAIL"
    assert report.live_notifications == (1 if action == "send_notification" else 0)
    assert report.mutation_calls == (0 if action == "send_notification" else 1)


@pytest.mark.parametrize(
    "action",
    (
        "set_approval",
        "cleanup_allowance",
        "post_order",
        "transfer_usdt",
        "redeem_positions",
        "send_notification",
    ),
)
def test_polymarket_guard_blocks_mutation_through_raw_nested_sdk_internals(
    tmp_path: Path, action: str
) -> None:
    class NestedSdk:
        def __init__(self) -> None:
            def mutate(*args: object, **kwargs: object) -> None:
                del args, kwargs

            self._ctx_inner = SimpleNamespace(**{action: mutate})

        def __getattr__(self, name: str):
            if name != action:
                raise AttributeError(name)

            def mutate(*args: object, **kwargs: object) -> None:
                del args, kwargs

            return mutate

    class NestedMutatingPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = NestedSdk()

        def preflight_report(self) -> dict[str, object]:
            getattr(self._client._ctx_inner, action)()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: NestedMutatingPolymarket(),
    )

    assert report.status == "FAIL"
    assert report.safety.status == "FAIL"
    assert report.live_notifications == (1 if action == "send_notification" else 0)
    assert report.mutation_calls == (0 if action == "send_notification" else 1)


def test_polymarket_guard_blocks_read_method_using_raw_nested_transport(
    tmp_path: Path,
) -> None:
    class Transport:
        def post_json(self, payload: object) -> object:
            del payload
            return {"posted": True}

        def get(self) -> object:
            return {"ok": True}

    class Context:
        def __init__(self) -> None:
            self.transport = Transport()

    class AdapterSdk:
        def __init__(self) -> None:
            self._ctx = Context()

        def read_account(self) -> object:
            return self._ctx.transport.post_json({"probe": True})

    class NestedTransportPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = AdapterSdk()

        def preflight_report(self) -> dict[str, object]:
            self._client.read_account()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: NestedTransportPolymarket(),
    )

    assert report.status == "FAIL"
    assert report.polymarket.status == "FAIL"
    assert report.safety.status == "FAIL"
    assert report.mutation_calls == 1
    assert report.live_notifications == 0


def test_polymarket_guard_preserves_nested_network_send_read_method(tmp_path: Path) -> None:
    class Transport:
        def __init__(self) -> None:
            self.send_calls = 0

        def send(self) -> object:
            self.send_calls += 1
            return {"ok": True}

    transport = Transport()

    class Context:
        def __init__(self) -> None:
            self.transport = transport

    class AdapterSdk:
        def __init__(self) -> None:
            self._ctx = Context()

        def read_account(self) -> object:
            return self._ctx.transport.send()

    class ReadOnlyNestedTransportPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = AdapterSdk()

        def preflight_report(self) -> dict[str, object]:
            self._client.read_account()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: ReadOnlyNestedTransportPolymarket(),
    )

    assert report.status == "PASS"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0
    assert transport.send_calls == 1


def test_polymarket_guard_preserves_nested_send_internal_iterator(
    tmp_path: Path,
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.responses = iter(({"ok": True},))

        def send(self) -> object:
            return next(self.responses)

    transport = Transport()

    class Context:
        def __init__(self) -> None:
            self.transport = transport

    class AdapterSdk:
        def __init__(self) -> None:
            self._ctx = Context()

        def read_account(self) -> object:
            return self._ctx.transport.send()

    class ReadOnlyNestedTransportPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = AdapterSdk()

        def preflight_report(self) -> dict[str, object]:
            self._client.read_account()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: ReadOnlyNestedTransportPolymarket(),
    )

    assert report.status == "PASS"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0


def test_polymarket_guard_preserves_nested_local_signing_method(
    tmp_path: Path,
) -> None:
    class AdapterSdk:
        def __init__(self) -> None:
            self.orders = iter(({"order_type": "FOK"},))

        def create_market_order(self, **_: object) -> object:
            return next(self.orders)

    class LocalSigningPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = AdapterSdk()

        def preflight_report(self) -> dict[str, object]:
            order = self._client.create_market_order(side="BUY")
            assert order["order_type"] == "FOK"
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: LocalSigningPolymarket(),
    )

    assert report.status == "PASS"
    assert report.mutation_calls == 0
    assert report.live_notifications == 0


def test_polymarket_guard_blocks_notifier_send_but_not_network_send(
    tmp_path: Path,
) -> None:
    class Notifier:
        def send(self) -> None:
            return None

    class NotifyingPolymarket(PolymarketClient):
        def __init__(self) -> None:
            self._client = SimpleNamespace()
            self.notifier = Notifier()

        def preflight_report(self) -> dict[str, object]:
            self.notifier.send()
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
                "posted": False,
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: NotifyingPolymarket(),
    )

    assert report.status == "FAIL"
    assert report.safety.status == "FAIL"
    assert report.live_notifications == 1
    assert report.mutation_calls == 0


def test_polymarket_report_without_posted_attestation_fails_closed(tmp_path: Path) -> None:
    class MissingPosted(PolymarketClient):
        def preflight_report(self) -> dict[str, object]:
            return {
                "result": "PASS",
                "account_reads": "pass",
                "fok_pair_signed_not_submitted": "pass",
            }

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: MissingPosted(),
    )

    assert report.status == "FAIL"
    assert report.polymarket.detail == "FAIL: Polymarket preflight did not attest no mutation"


def test_network_unavailability_is_blocked_but_invalid_polymarket_result_fails(
    tmp_path: Path,
) -> None:
    def unavailable_source(_config: object, *, urlopen_fn: object) -> object:
        del urlopen_fn
        raise URLError("network-sentinel")

    report = readiness_report(tmp_path, predict_source_factory=unavailable_source)
    assert report.status == "BLOCKED"
    assert report.predict_market.status == "BLOCKED"

    def unavailable_polymarket(_config: object) -> object:
        raise URLError("polymarket-network-sentinel")

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=unavailable_polymarket,
    )
    assert report.status == "BLOCKED"
    assert report.polymarket.status == "BLOCKED"

    class InvalidPolymarket(PolymarketClient):
        def preflight_report(self) -> dict[str, object]:
            return {"result": "BLOCKED"}

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=lambda _config: InvalidPolymarket(),
    )
    assert report.status == "FAIL"
    assert report.polymarket.status == "FAIL"


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("network", "BLOCKED"),
        ("timeout", "BLOCKED"),
        ("unavailable", "BLOCKED"),
        ("auth", "FAIL"),
        ("invalid", "FAIL"),
    ],
)
def test_polymarket_setup_error_preserves_blocked_vs_fail_classification(
    tmp_path: Path, error_code: str, expected_status: str
) -> None:
    def failing_polymarket(_config: object) -> object:
        raise PolymarketTradingError(error_code)

    report = readiness_report(
        tmp_path,
        polymarket_client_factory=failing_polymarket,
    )

    assert report.polymarket.status == expected_status
    assert report.status == expected_status
    assert "sentinel" not in report.polymarket.detail


def test_browser_handoff_and_health_are_required_for_live_pass(tmp_path: Path) -> None:
    report = readiness_report(
        tmp_path,
        browser_handoff_path=tmp_path / "missing-browser-handoff.json",
    )
    assert report.status == "BLOCKED"
    assert report.browser.status == "BLOCKED"

    report = readiness_report(
        tmp_path,
        browser_handoff_path=write_browser_handoff(tmp_path),
        browser_health_fn=lambda _url: False,
    )
    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: browser/dashboard health unavailable"


def test_browser_handoff_binds_fresh_url_and_candidate(tmp_path: Path) -> None:
    report = readiness_report(tmp_path)
    assert report.browser.status == "PASS"

    report = readiness_report(
        tmp_path,
        browser_handoff_path=write_browser_handoff(tmp_path, candidate_commit="other-commit"),
    )
    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: Playwright browser handoff candidate mismatch"

    report = readiness_report(
        tmp_path,
        browser_handoff_path=write_browser_handoff(tmp_path, expires_at=999.0),
    )
    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: Playwright browser handoff expired"


def test_browser_handoff_requires_fixture_binding(tmp_path: Path) -> None:
    report = readiness_report(
        tmp_path,
        browser_handoff_path=write_browser_handoff(
            tmp_path,
            fixture_url="http://127.0.0.1:8766",
            fixture_health_url="http://127.0.0.1:8766/healthz",
        ),
    )

    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: Playwright browser fixture binding unavailable"


def test_browser_handoff_nonce_is_missing_wrong_or_replayed_blocked(tmp_path: Path) -> None:
    handoff_path = write_browser_handoff(tmp_path, run_nonce="handoff-nonce")
    nonce_path = write_browser_nonce(tmp_path, "server-nonce")

    report = readiness_report(
        tmp_path,
        browser_handoff_path=handoff_path,
        browser_nonce_path=nonce_path,
    )
    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: Playwright browser nonce mismatch"
    assert not nonce_path.exists()

    handoff_path = write_browser_handoff(tmp_path, run_nonce="server-nonce")
    nonce_path = write_browser_nonce(tmp_path, "server-nonce")
    report = readiness_report(
        tmp_path,
        browser_handoff_path=handoff_path,
        browser_nonce_path=nonce_path,
    )
    assert report.status == "PASS"
    assert not nonce_path.exists()

    report = readiness_report(
        tmp_path,
        browser_handoff_path=handoff_path,
        browser_nonce_path=nonce_path,
    )
    assert report.status == "BLOCKED"
    assert report.browser.detail == "BLOCKED: Playwright browser nonce unavailable"


def test_direct_runner_with_arbitrary_handoff_without_pipeline_nonce_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = time.time()
    handoff_path = write_browser_handoff(
        tmp_path,
        created_at=now,
        expires_at=now + 60,
    )
    monkeypatch.setattr(acceptance, "_git_commit", lambda _root: "test-commit")
    monkeypatch.setattr(acceptance, "_dashboard_is_reachable", lambda _url: False)

    result = acceptance.main(
        [
            "--json",
            "--expected-root",
            str(tmp_path),
            "--config",
            str(tmp_path / "missing.json"),
            "--browser-handoff",
            str(handoff_path),
        ]
    )

    assert result == 2
    assert "BLOCKED: Playwright browser nonce unavailable" in capsys.readouterr().out


def test_make_acceptance_passes_playwright_handoff_before_live_registry() -> None:
    makefile = Path(__file__).parents[1].joinpath("Makefile").read_text(encoding="utf-8")
    playwright = makefile.index("npm exec playwright test")
    registry = makefile.index("prediction_arbitrage_acceptance")

    assert playwright < registry
    assert "--browser-ready" not in makefile
    assert "PREDICTION_ACCEPTANCE_BROWSER_HANDOFF" in makefile[:registry]
    assert "PREDICTION_ACCEPTANCE_BROWSER_NONCE" in makefile[:registry]
    assert "PREDICTION_ACCEPTANCE_REVIEW_URL" in makefile[:registry]
    assert "PREDICTION_ACCEPTANCE_BROWSER_NONCE_FILE" in makefile
    assert "--browser-handoff" in makefile[registry:]
