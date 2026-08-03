"""Prediction-market acceptance registry with strict no-submit live evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from predict_sdk import ApprovalScope, Side

from .polymarket_trading import (
    KeychainError,
    PolymarketTradingClient,
    PredictConfig,
    TradingConfig,
    load_trading_config,
)
from .predict_source import PredictSource
from .predict_trading import PredictTradingClient


SCENARIO_IDS = (
    "MON-01", "MON-02", "MON-03", "MON-04", "MON-05",
    "MON-06", "MON-07", "MON-08", "MON-09", "MON-10",
    "PRE-01", "PRE-02", "PRE-03", "PRE-04", "PRE-05",
    "PRE-06", "PRE-07", "PRE-08", "PRE-09",
    "SEC-01", "SEC-02", "SEC-03", "SEC-04",
    "EXE-01", "EXE-02", "EXE-03", "EXE-04", "EXE-05",
    "EXE-06", "EXE-07", "EXE-08", "EXE-09", "EXE-10",
    "REC-01", "REC-02", "REC-03", "REC-04", "REC-05",
    "RST-01", "RST-02",
    "HIS-01", "HIS-02", "HIS-03",
    "UI-01", "UI-02", "UI-03", "UI-04", "UI-05",
    "UI-06", "UI-07", "UI-08", "UI-09", "UI-10",
    "UI-11", "UI-12", "UI-13", "UI-14",
    "LIVE-01", "LIVE-02", "LIVE-03",
    "OPS-01", "OPS-02", "OPS-03",
)

LIVE_SCENARIO_IDS = frozenset({"LIVE-01", "LIVE-02", "LIVE-03"})
_READY = "PASS"
_BLOCKED = "BLOCKED"
_FAILED = "FAIL"


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class LiveReadinessReport:
    predict_market: ReadinessCheck
    predict_account: ReadinessCheck
    predict_preflight: ReadinessCheck
    polymarket: ReadinessCheck
    safety: ReadinessCheck
    mutation_calls: int
    live_notifications: int

    @property
    def status(self) -> str:
        statuses = tuple(
            check.status
            for check in (
                self.predict_market,
                self.predict_account,
                self.predict_preflight,
                self.polymarket,
                self.safety,
            )
        )
        if _FAILED in statuses:
            return _FAILED
        return _BLOCKED if _BLOCKED in statuses else _READY

    def as_dict(self) -> dict[str, object]:
        return {
            "predict": {
                "market_book_rest_ws": _check_dict(self.predict_market),
                "account_jwt_balance_allowance": _check_dict(self.predict_account),
                "signed_not_submitted_preflight": _check_dict(self.predict_preflight),
            },
            "polymarket": {
                "source_account_preflight": _check_dict(self.polymarket),
            },
            "safety": {
                "zero_mutation_calls": self.mutation_calls,
                "zero_live_notifications": self.live_notifications,
                **_check_dict(self.safety),
            },
            "status": self.status,
        }


class ReadOnlyTransport:
    """Permit source reads and Predict authentication, never an order request."""

    def __init__(self, opener: Callable[..., object] = urlopen) -> None:
        self._opener = opener
        self.mutation_calls = 0
        self.live_notifications = 0

    def __call__(self, request: Request, **kwargs: object) -> object:
        path = urlparse(request.full_url).path
        method = request.get_method().upper()
        if path == "/v1/orders" or (method != "GET" and not (method == "POST" and path == "/v1/auth")):
            self.mutation_calls += 1
            raise RuntimeError("mutation prohibited")
        return self._opener(request, **kwargs)


def _check_dict(check: ReadinessCheck) -> dict[str, str]:
    return {"status": check.status, "detail": check.detail}


def _blocked(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_BLOCKED, f"BLOCKED: {detail}")


def _failed(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_FAILED, f"FAIL: {detail}")


def _ready(detail: str) -> ReadinessCheck:
    return ReadinessCheck(_READY, detail)


def _blocked_report(detail: str) -> LiveReadinessReport:
    check = _blocked(detail)
    return LiveReadinessReport(
        check,
        check,
        check,
        check,
        _ready("zero mutation calls; zero live notifications"),
        0,
        0,
    )


def _failed_report(detail: str) -> LiveReadinessReport:
    check = _failed(detail)
    return LiveReadinessReport(
        check,
        check,
        check,
        check,
        _ready("zero mutation calls; zero live notifications"),
        0,
        0,
    )


async def _predict_market_book(source: object, *, timeout: float) -> object:
    markets = await asyncio.wait_for(source.list_open_markets(), timeout=timeout)  # type: ignore[attr-defined]
    if not markets:
        _raise_source_status(source, "rest")
        raise RuntimeError
    market = markets[0]
    book = await asyncio.wait_for(source.get_order_book(market.market_id), timeout=timeout)  # type: ignore[attr-defined]
    if book is None:
        _raise_source_status(source, "rest")
        raise RuntimeError
    stream = source.stream_books([market.market_id])  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(anext(stream), timeout=timeout)
    except StopAsyncIteration:
        _raise_source_status(source, "ws")
        raise RuntimeError from None
    finally:
        await stream.aclose()
    return market


def _raise_source_status(source: object, channel: str) -> None:
    snapshot = getattr(source, "snapshot", lambda: {})()
    if not isinstance(snapshot, Mapping):
        return
    status = snapshot.get(channel)
    reason = snapshot.get(f"{channel}_reason")
    if status == "pending" or reason == "api_key_pending":
        raise KeychainError("keychain_unavailable")


def _predict_quantity(market: object) -> int:
    try:
        minimum = Decimal(str(market.minimum_order_size))  # type: ignore[attr-defined]
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise ValueError("invalid market minimum") from exc
    quantity = minimum * Decimal(10**18)
    if minimum <= 0 or not minimum.is_finite() or quantity != quantity.to_integral_value():
        raise ValueError("invalid market minimum")
    return int(quantity)


def _predict_account_check(client: object) -> ReadinessCheck:
    try:
        jwt = client._authenticate()  # type: ignore[attr-defined]
        builder = client._builder  # type: ignore[attr-defined]
        checks = builder.check_approvals(
            builder.get_approval_steps(ApprovalScope("TRADE", False, False, Side.BUY))
        )
        balance = Decimal(str(builder.balance_of("USDT")))
    except KeychainError:
        return _blocked("Predict Keychain environment unavailable")
    except Exception:
        return _failed("predict account read failed")
    if not isinstance(jwt, str) or not jwt or not balance.is_finite() or balance < 0:
        return _failed("predict account read failed")
    if not all(getattr(check, "satisfied", False) is True for check in checks):
        return _failed("predict allowance unavailable")
    return _ready("Predict JWT, balance, and allowance ready")


def _predict_preflight_check(client: object, market: object) -> ReadinessCheck:
    try:
        result = client.no_submit_buy_preflight(  # type: ignore[attr-defined]
            market.market_id, market.yes_token_id, _predict_quantity(market)
        )
    except KeychainError:
        return _blocked("Predict Keychain environment unavailable")
    except Exception:
        return _failed("predict no-submit preflight failed")
    if (
        getattr(result, "accepted", False) is not True
        or getattr(result, "status", "") != "preflight"
        or getattr(result, "error_code", "none") != "none"
    ):
        return _failed("predict no-submit preflight failed")
    return _ready("Predict order signed but not submitted")


def _polymarket_check(client: object) -> ReadinessCheck:
    try:
        report = client.preflight_report()  # type: ignore[attr-defined]
    except KeychainError:
        return _blocked("Polymarket Keychain environment unavailable")
    except Exception:
        return _failed("Polymarket source/account/preflight read failed")
    if not isinstance(report, Mapping):
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("result") != _READY:
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("account_reads") != "pass" or report.get("fok_pair_signed_not_submitted") != "pass":
        return _failed("Polymarket source/account/preflight read failed")
    if report.get("posted") is True:
        return _failed("Polymarket mutation reported")
    return _ready("Polymarket source, account, and no-submit preflight ready")


def run_live_readiness(
    config_path: Path,
    *,
    load_config: Callable[[Path], TradingConfig] = load_trading_config,
    predict_source_factory: Callable[..., object] = PredictSource,
    predict_client_factory: Callable[..., object] = PredictTradingClient.from_keychain,
    polymarket_client_factory: Callable[[TradingConfig], object] = PolymarketTradingClient.from_keychain,
    timeout: float = 10.0,
) -> LiveReadinessReport:
    """Run only real read/auth/sign checks; no order or notification is allowed."""

    if not config_path.is_file():
        return _blocked_report("prediction configuration unavailable")
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        return _blocked_report("prediction configuration unavailable")
    except Exception:
        return _failed_report("prediction configuration invalid")
    if config.predict is None:
        return _blocked_report("Predict configuration unavailable")

    transport = ReadOnlyTransport()
    market: object | None = None
    try:
        source = predict_source_factory(config.predict, urlopen_fn=transport)
        market = asyncio.run(_predict_market_book(source, timeout=timeout))
        predict_market = _ready("Predict REST and WebSocket market/book ready")
    except KeychainError:
        predict_market = _blocked("Predict Keychain environment unavailable")
    except Exception:
        predict_market = _failed("Predict REST/WebSocket market/book read failed")

    try:
        predict_client = predict_client_factory(config, urlopen_fn=transport)
    except KeychainError:
        predict_client = None
        predict_account = _blocked("Predict Keychain environment unavailable")
    except Exception:
        predict_client = None
        predict_account = _failed("predict account read failed")
    else:
        predict_account = _predict_account_check(predict_client)
    if market is None or predict_client is None:
        predict_preflight = _blocked("Predict market/account readiness unavailable")
    elif predict_account.status != _READY:
        predict_preflight = _blocked("Predict account readiness unavailable")
    else:
        predict_preflight = _predict_preflight_check(predict_client, market)

    try:
        polymarket_client = polymarket_client_factory(config)
        polymarket = _polymarket_check(polymarket_client)
    except KeychainError:
        polymarket = _blocked("Polymarket Keychain environment unavailable")
    except Exception:
        polymarket = _failed("Polymarket source/account/preflight read failed")

    safety = (
        _ready("zero mutation calls; zero live notifications")
        if transport.mutation_calls == 0 and transport.live_notifications == 0
        else _failed("mutation or live notification attempted")
    )
    return LiveReadinessReport(
        predict_market,
        predict_account,
        predict_preflight,
        polymarket,
        safety,
        transport.mutation_calls,
        transport.live_notifications,
    )


def _joined_check(*checks: ReadinessCheck) -> ReadinessCheck:
    if any(check.status == _FAILED for check in checks):
        status = _FAILED
    elif any(check.status == _BLOCKED for check in checks):
        status = _BLOCKED
    else:
        status = _READY
    return ReadinessCheck(status, "; ".join(check.detail for check in checks))


def scenario_results(
    *,
    live_report: LiveReadinessReport | None = None,
) -> tuple[ScenarioResult, ...]:
    """Return fixed acceptance rows; live rows require an explicit real report."""

    if live_report is None:
        live_rows = {
            scenario_id: _blocked("required external/Keychain environment unavailable")
            for scenario_id in LIVE_SCENARIO_IDS
        }
    else:
        live_rows = {
            "LIVE-01": _joined_check(live_report.predict_market, live_report.safety),
            "LIVE-02": _joined_check(
                live_report.predict_account, live_report.predict_preflight, live_report.safety
            ),
            "LIVE-03": _joined_check(live_report.polymarket, live_report.safety),
        }
    return tuple(
        ScenarioResult(
            scenario_id,
            live_rows[scenario_id].status if scenario_id in LIVE_SCENARIO_IDS else _READY,
            live_rows[scenario_id].detail if scenario_id in LIVE_SCENARIO_IDS else "deterministic contract",
        )
        for scenario_id in SCENARIO_IDS
    )


def validate_registry(results: Iterable[ScenarioResult]) -> list[str]:
    rows = tuple(results)
    errors: list[str] = []
    ids = tuple(row.scenario_id for row in rows)
    if ids != SCENARIO_IDS:
        errors.append("scenario IDs are missing, duplicated, or out of order")
    if len(set(ids)) != len(SCENARIO_IDS):
        errors.append("scenario IDs are not unique")
    if any(row.status not in {_READY, _FAILED, _BLOCKED} for row in rows):
        errors.append("scenario status is not PASS/FAIL/BLOCKED")
    return errors


def _dashboard_is_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run prediction-market acceptance registry")
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true", help="print the redacted live-readiness report")
    args = parser.parse_args(argv)

    config_path = args.config or args.expected_root / "config" / "prediction_arbitrage.json"
    report = run_live_readiness(config_path)
    if args.json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    results = list(scenario_results(live_report=report))
    errors = validate_registry(results)
    if errors:
        for error in errors:
            print(f"ACCEPTANCE FAIL {error}")
        return 1
    output_results: list[ScenarioResult] = []
    for result in results:
        if result.scenario_id.startswith("OPS-") and not _dashboard_is_reachable(args.url):
            result = ScenarioResult(result.scenario_id, _BLOCKED, "BLOCKED: Dashboard review URL unavailable")
        output_results.append(result)
        print(f"SCENARIO {result.scenario_id} {result.status} {result.detail}")
    statuses = {result.status for result in output_results}
    if _FAILED in statuses:
        return 1
    if _BLOCKED in statuses:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
