from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from open_trader.trend_simulate_positions import (
    TrendSimulatePositionService,
    _action_events,
)


REPORT_DIRS = {
    "futu": "trend_us_futu",
    "phillips": "trend_hk_phillips",
    "eastmoney": "trend_a_share",
}


def _report_hash(report: dict[str, Any]) -> str:
    body = (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _frozen_report(
    *,
    execution_date: str = "2026-07-20",
    market: str = "US",
    broker: str = "futu",
    symbol: str = "TRV",
    version: str = "v1",
) -> dict[str, Any]:
    return {
        "execution_date": execution_date,
        "metadata": {"market": market, "broker": broker},
        "strategy_snapshot": {"strategy_version": version},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": symbol}],
        },
    }


def _write_report(
    root: Path,
    *,
    broker: str,
    artifact: str,
    payload: dict[str, Any],
) -> None:
    path = root / "reports" / REPORT_DIRS[broker] / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_action_event(
    root: Path,
    *,
    market: str = "US",
    symbol: str = "TRV",
    side: str = "buy",
    status: str = "filled",
    report_sha256: str | None = "",
    strategy_version: str | None = "v1",
    filled_qty: str = "1",
    execution_date: str = "2026-07-20",
    recorded_at: str = "2026-07-20T10:00:00-04:00",
    reason: str | None = None,
) -> None:
    action_key = hashlib.sha256(
        f"{market}:{execution_date}:{strategy_version}:{symbol}:{side}".encode()
    ).hexdigest()
    path = (
        root
        / "data"
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
        / f"{recorded_at.replace(':', '-')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "market": market,
        "date": execution_date,
        "symbol": symbol,
        "side": side,
        "status": status,
        "filled_qty": filled_qty,
        "recorded_at": recorded_at,
    }
    if strategy_version is not None:
        payload["strategy_version"] = strategy_version
    if report_sha256 is not None:
        payload["report_sha256"] = report_sha256
    if reason is not None:
        payload["reason"] = reason
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _position(code: str = "US.TRV") -> dict[str, str]:
    return {
        "code": code,
        "stock_name": "旅行者保险",
        "qty": "9",
        "cost_price": "368.98",
        "nominal_price": "371.20",
        "market_val": "3340.80",
        "pl_ratio": "0.60",
    }


def test_action_events_sort_aware_timestamps_by_actual_instant(
    tmp_path: Path,
) -> None:
    _write_action_event(
        tmp_path,
        status="missed",
        recorded_at="2026-07-21T09:01:01+08:00",
    )
    _write_action_event(
        tmp_path,
        status="filled",
        recorded_at="2026-07-21T07:36:30-04:00",
    )

    events = _action_events(tmp_path / "data", "US")

    assert [event[3]["status"] for event in events] == ["missed", "filled"]


class FakeClient:
    def __init__(self, positions: list[dict[str, str]]) -> None:
        self.positions = positions
        self.closed = False

    def account_snapshot(self) -> dict[str, Any]:
        return {
            "acc_id": 102,
            "net_value": "10000",
            "cash": "6659.20",
            "positions": self.positions,
        }

    def close(self) -> None:
        self.closed = True


class FakeClientFactory:
    def __init__(self, positions: list[dict[str, str]]) -> None:
        self.positions = positions
        self.calls: list[dict[str, Any]] = []
        self.clients: list[FakeClient] = []

    def __call__(
        self,
        *,
        host: str,
        port: int,
        simulate_acc_id: int,
        trd_market: str,
    ) -> FakeClient:
        del host, port
        self.calls.append(
            {"market": trd_market, "simulate_acc_id": simulate_acc_id}
        )
        client = FakeClient(self.positions)
        self.clients.append(client)
        return client


class FailingClientFactory:
    def __init__(self, message: str) -> None:
        self.message = message

    def __call__(self, **_: Any) -> FakeClient:
        raise RuntimeError(self.message)


def _service(
    root: Path,
    clients: Any,
    *,
    account_ids: dict[str, int] | None = None,
    now: Any = None,
    refresh_seconds: int = 30,
) -> TrendSimulatePositionService:
    return TrendSimulatePositionService(
        host="127.0.0.1",
        port=11111,
        account_ids=(
            {"eastmoney": 101, "futu": 102, "phillips": 103}
            if account_ids is None
            else account_ids
        ),
        fx_to_hkd={
            "HKD": Decimal("1"),
            "USD": Decimal("7.8"),
            "CNY": Decimal("1.08"),
        },
        data_dir=root / "data",
        reports_dir=root / "reports",
        client_factory=clients,
        now=now or (lambda: datetime.fromisoformat("2026-07-18T12:34:56+08:00")),
        refresh_seconds=refresh_seconds,
    )


def test_simulated_positions_route_account_and_link_exact_filled_report(
    tmp_path: Path,
) -> None:
    report = _frozen_report()
    _write_report(
        tmp_path, broker="futu", artifact="2026-07-17.json", payload=report
    )
    _write_action_event(tmp_path, report_sha256=_report_hash(report))
    clients = FakeClientFactory(positions=[_position()])

    payload = _service(tmp_path, clients).load("futu")

    assert clients.calls == [{"market": "US", "simulate_acc_id": 102}]
    assert clients.clients[0].closed is True
    assert payload["available"] is True
    assert payload["synced_at"] == "2026-07-18T12:34:56+08:00"
    assert payload["positions"][0] == {
        "broker": "futu",
        "market": "US",
        "symbol": "TRV",
        "name": "旅行者保险",
        "currency": "USD",
        "quantity": "9",
        "cost_price": "368.98",
        "last_price": "371.20",
        "market_value": "3340.80",
        "cost_value": "3320.82",
        "market_value_hkd": "26058.24",
        "current_valuation": {
            "price": "371.20",
            "price_kind": "account_snapshot",
            "price_as_of": "2026-07-18T12:34:56+08:00",
            "market_value_usd": "3340.80",
            "market_value_hkd": "26058.24",
        },
        "account_weight": "33.41%",
        "portfolio_weight": "33.41%",
        "unrealized_pnl_pct": "0.60%",
        "attribution_status": "linked",
        "report": {
            "artifact": "2026-07-17.json",
            "execution_date": "2026-07-20",
            "strategy_version": "v1",
            "report_sha256": _report_hash(report),
        },
    }


def test_simulated_position_publishes_complete_current_valuation(tmp_path: Path) -> None:
    clients = FakeClientFactory(positions=[_position()])

    payload = _service(tmp_path, clients).load("futu")

    assert payload["positions"][0]["current_valuation"] == {
        "price": "371.20",
        "price_kind": "account_snapshot",
        "price_as_of": "2026-07-18T12:34:56+08:00",
        "market_value_usd": "3340.80",
        "market_value_hkd": "26058.24",
    }


@pytest.mark.parametrize(
    ("broker", "market", "account_id"),
    [("futu", "US", 102), ("phillips", "HK", 103), ("eastmoney", "CN", 101)],
)
def test_simulated_positions_route_each_broker_account(
    tmp_path: Path, broker: str, market: str, account_id: int
) -> None:
    clients = FakeClientFactory(positions=[])

    payload = _service(tmp_path, clients).load(broker)

    assert clients.calls == [{"market": market, "simulate_acc_id": account_id}]
    assert payload["broker"] == broker
    assert payload["market"] == market


def test_simulated_positions_reuse_fresh_cached_snapshot(tmp_path: Path) -> None:
    clients = FakeClientFactory(positions=[_position()])
    service = _service(tmp_path, clients)

    first = service.load("futu")
    second = service.load("futu")

    assert first == second
    assert len(clients.calls) == 1


def test_simulated_positions_return_stale_cache_while_refreshing(
    tmp_path: Path,
) -> None:
    current = [datetime.fromisoformat("2026-07-18T12:34:56+08:00")]
    entered = threading.Event()
    release = threading.Event()

    class ChangingFactory(FakeClientFactory):
        def __call__(self, **kwargs: Any) -> FakeClient:
            call_number = len(self.calls) + 1
            self.positions = [
                _position("US.TRV" if call_number == 1 else "US.NEW")
            ]
            if call_number == 2:
                entered.set()
                assert release.wait(timeout=2)
            return super().__call__(**kwargs)

    clients = ChangingFactory(positions=[])
    service = _service(
        tmp_path,
        clients,
        now=lambda: current[0],
        refresh_seconds=30,
    )
    first = service.load("futu")
    current[0] = datetime.fromisoformat("2026-07-18T12:35:27+08:00")

    stale = service.load("futu")

    assert entered.wait(timeout=1)
    assert stale == first
    assert len(clients.calls) == 1
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        refreshed = service.load("futu")
        if refreshed["positions"][0]["symbol"] == "NEW":
            break
        time.sleep(0.01)
    else:
        pytest.fail("background simulate-position refresh did not publish")
    assert len(clients.calls) == 2


def test_simulated_positions_reject_unsupported_broker(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="^unsupported trend simulate broker: tiger$"
    ):
        _service(tmp_path, FakeClientFactory([])).load("tiger")


def test_simulated_positions_keep_unlinked_position_visible(tmp_path: Path) -> None:
    payload = _service(
        tmp_path, FakeClientFactory(positions=[_position("US.OLD")])
    ).load("futu")

    assert payload["positions"][0]["attribution_status"] == "unlinked"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_fail_closed_on_conflicting_reports(
    tmp_path: Path,
) -> None:
    first = _frozen_report(version="v1")
    second = _frozen_report(version="v2")
    _write_report(tmp_path, broker="futu", artifact="first.json", payload=first)
    _write_report(tmp_path, broker="futu", artifact="second.json", payload=second)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(first),
        recorded_at="2026-07-20T10:00:00-04:00",
    )
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(second),
        strategy_version="v2",
        recorded_at="2026-07-20T10:01:00-04:00",
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "conflict"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_conflict_takes_precedence_over_unattributable_buy(
    tmp_path: Path,
) -> None:
    first = _frozen_report(version="v1")
    second = _frozen_report(version="v2")
    _write_report(tmp_path, broker="futu", artifact="first.json", payload=first)
    _write_report(tmp_path, broker="futu", artifact="second.json", payload=second)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(first),
        recorded_at="2026-07-20T10:00:00-04:00",
    )
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(second),
        strategy_version="v2",
        recorded_at="2026-07-20T10:01:00-04:00",
    )
    _write_action_event(
        tmp_path,
        report_sha256=None,
        strategy_version="v3",
        recorded_at="2026-07-20T10:02:00-04:00",
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "conflict"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_replay_sell_then_new_partial_buy(tmp_path: Path) -> None:
    first = _frozen_report(version="v1")
    second = _frozen_report(version="v2")
    _write_report(tmp_path, broker="futu", artifact="first.json", payload=first)
    _write_report(tmp_path, broker="futu", artifact="second.json", payload=second)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(first),
        recorded_at="2026-07-20T10:00:00-04:00",
    )
    _write_action_event(
        tmp_path,
        side="sell",
        report_sha256=_report_hash(first),
        recorded_at="2026-07-20T10:01:00-04:00",
    )
    _write_action_event(
        tmp_path,
        status="partially_filled",
        report_sha256=_report_hash(second),
        strategy_version="v2",
        filled_qty="0.5",
        recorded_at="2026-07-20T10:02:00-04:00",
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "linked"
    assert payload["positions"][0]["report"]["artifact"] == "second.json"


@pytest.mark.parametrize(
    ("status", "reason", "expected_status"),
    [
        ("incomplete", "position_zero_confirmed", "unlinked"),
        ("incomplete", None, "linked"),
        ("failed", "position_zero_confirmed", "linked"),
        ("submitted", "position_zero_confirmed", "linked"),
        ("missed", "position_zero_confirmed", "linked"),
    ],
)
def test_simulated_positions_clear_only_terminal_incomplete_sell(
    tmp_path: Path, status: str, reason: str | None, expected_status: str,
) -> None:
    report = _frozen_report()
    _write_report(tmp_path, broker="futu", artifact="report.json", payload=report)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(report),
        recorded_at="2026-07-20T10:00:00-04:00",
    )
    _write_action_event(
        tmp_path,
        side="sell",
        status=status,
        filled_qty="40",
        report_sha256=_report_hash(report),
        recorded_at="2026-07-20T10:01:00-04:00",
        reason=reason,
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == expected_status


def test_simulated_positions_do_not_link_zero_quantity_buy(tmp_path: Path) -> None:
    report = _frozen_report()
    _write_report(tmp_path, broker="futu", artifact="report.json", payload=report)
    _write_action_event(
        tmp_path, report_sha256=_report_hash(report), filled_qty="0"
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "unlinked"


@pytest.mark.parametrize("unattributable_hash", [None, "not-a-sha256"])
def test_simulated_positions_fail_closed_after_unattributable_positive_buy(
    tmp_path: Path, unattributable_hash: str | None
) -> None:
    report = _frozen_report()
    _write_report(tmp_path, broker="futu", artifact="report.json", payload=report)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(report),
        recorded_at="2026-07-20T10:00:00-04:00",
    )
    _write_action_event(
        tmp_path,
        report_sha256=unattributable_hash,
        recorded_at="2026-07-20T10:01:00-04:00",
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "unlinked"
    assert payload["positions"][0]["report"] is None


@pytest.mark.parametrize("strategy_version", [None, "v2"])
def test_simulated_positions_never_link_report_by_hash_without_matching_version(
    tmp_path: Path, strategy_version: str | None,
) -> None:
    report = _frozen_report(version="v1")
    _write_report(tmp_path, broker="futu", artifact="report.json", payload=report)
    _write_action_event(
        tmp_path,
        report_sha256=_report_hash(report),
        strategy_version=strategy_version,
    )

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "unlinked"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_require_report_hash_and_identity_match(
    tmp_path: Path,
) -> None:
    report = _frozen_report(market="HK", broker="phillips")
    _write_report(tmp_path, broker="futu", artifact="wrong.json", payload=report)
    _write_action_event(tmp_path, report_sha256=_report_hash(report))

    payload = _service(tmp_path, FakeClientFactory([_position()])).load("futu")

    assert payload["positions"][0]["attribution_status"] == "unlinked"
    assert payload["positions"][0]["report"] is None


def test_simulated_positions_skip_non_positive_positions(tmp_path: Path) -> None:
    zero = {**_position("US.ZERO"), "qty": "0"}
    negative = {**_position("US.SHORT"), "qty": "-1"}

    payload = _service(
        tmp_path, FakeClientFactory([zero, negative, _position()])
    ).load("futu")

    assert [row["symbol"] for row in payload["positions"]] == ["TRV"]


def test_simulated_positions_accept_beijing_exchange_position(tmp_path: Path) -> None:
    payload = _service(
        tmp_path, FakeClientFactory([_position("BJ.920000")])
    ).load("eastmoney")

    assert payload["available"] is True
    assert payload["positions"][0]["symbol"] == "920000"


def test_simulated_positions_return_unavailable_instead_of_fallback(
    tmp_path: Path,
) -> None:
    payload = _service(tmp_path, FailingClientFactory("OpenD unavailable")).load(
        "futu"
    )

    assert payload == {
        "available": False,
        "broker": "futu",
        "market": "US",
        "synced_at": "",
        "positions": [],
        "error": "OpenD unavailable",
    }


def test_simulated_positions_return_unavailable_for_missing_account(
    tmp_path: Path,
) -> None:
    clients = FakeClientFactory([])

    payload = _service(tmp_path, clients, account_ids={}).load("futu")

    assert clients.calls == []
    assert payload["available"] is False
    assert payload["error"] == "模拟账户未登记"
