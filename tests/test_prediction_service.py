from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import open_trader
import open_trader.prediction_service as prediction_service
import open_trader.polymarket_trading as polymarket_trading_module
from open_trader.llm_providers import PROVIDER_IDS, resolve_provider
from open_trader.notifications import NullNotifier
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_read_model import (
    _prediction_relation_safe_value,
    _prediction_safe_value,
    prediction_history_payload,
    prediction_state_payload,
)
from open_trader.prediction_service import create_prediction_server
from tests.test_prediction_arbitrage_execution import (
    _cross_service,
    execution_fixture,
    threshold_execution_fixture,
    wait_until_terminal,
)
from tests.test_prediction_read_model import (
    _CrossVenueMonitor,
    _Execution,
    _Monitor,
    _Store,
)


FROZEN_PREDICTION_MUTATION_PATHS = (
    "/api/prediction-arbitrage/preview",
    "/api/prediction-arbitrage/executions",
    "/api/prediction-arbitrage/mode",
    "/api/prediction-arbitrage/circuit-breaker/reset",
    "/api/prediction-arbitrage/predict-allowance/cleanup",
    "/api/prediction-arbitrage/cross-auto/pause",
)


class _Runtime:
    def __init__(self, *, state: str = "RUNNING", violation: dict[str, object] | None = None) -> None:
        self.state = state
        self.store = _Store()
        self.monitor = _Monitor()
        self.execution = _Execution()
        self.cross_venue_monitor = _CrossVenueMonitor()
        self.shadow_evidence = {
            "mode": "shadow",
            "guard_attempts": [] if violation is None else [violation],
            "first_violation": violation,
            "codex": {
                "relation": {"calls": 1, "successes": 1},
                "cross_venue": {"calls": 2, "successes": 1},
            },
        }
        self.polls = 0

    def poll_shadow_failure(self) -> dict[str, object] | None:
        self.polls += 1
        return self.shadow_evidence["first_violation"]  # type: ignore[return-value]


@contextmanager
def _running_server(runtime: object, **kwargs: object) -> Iterator[tuple[str, object]]:
    server = create_prediction_server(runtime=runtime, port=0, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _server(runtime: object, **kwargs: object) -> Iterator[str]:
    with _running_server(runtime, **kwargs) as (base, _server_instance):
        yield base


def _response(
    request: str | Request, *, timeout: float = 60
) -> tuple[int, dict[str, object]]:
    status, payload, _headers = _response_with_headers(request, timeout=timeout)
    return status, payload


def _response_with_headers(
    request: str | Request, *, timeout: float = 60
) -> tuple[int, dict[str, object], Mapping[str, str]]:
    try:
        with urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                dict(response.headers.items()),
            )
    except HTTPError as error:
        return (
            error.code,
            json.loads(error.read().decode("utf-8")),
            dict(error.headers.items()),
        )


def _socket_fd_count() -> int:
    count = 0
    for name in os.listdir("/dev/fd"):
        try:
            count += stat.S_ISSOCK(os.fstat(int(name)).st_mode)
        except (OSError, ValueError):
            continue
    return count


def test_socket_fd_count_does_not_leak_non_socket_descriptors() -> None:
    read_fd, write_fd = os.pipe()
    try:
        before = len(os.listdir("/dev/fd"))
        for _ in range(3):
            _socket_fd_count()
        assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _handler_thread_ids() -> set[int | None]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if "process_request_thread" in thread.name
    }


class _ProductionExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, Mapping[str, object]]] = []
        self.mode_result: dict[str, object] = {"state": "ok", "mode": "manual"}
        self.error: Exception | None = None

    def preview(self, opportunity_id: str) -> dict[str, object]:
        self.calls.append(("preview", opportunity_id, {}))
        return {
            "state": "previewed",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
        }

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        self.calls.append(
            (
                "confirm",
                {"preview_id": preview_id, "idempotency_key": idempotency_key},
                {},
            )
        )
        return {
            "state": "validating",
            "execution_id": "execution-1",
            "preview_id": preview_id,
            "idempotency_key": idempotency_key,
        }

    def set_validation_mode(
        self, mode: str, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("mode", mode, audit))
        if self.error is not None:
            raise self.error
        return self.mode_result

    def reset_breaker(
        self, incident_id: str, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("reset", incident_id, audit))
        return {"state": "ready", "incident_id": incident_id}

    def cleanup_predict_allowance(
        self, *, confirm: bool, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("cleanup", confirm, audit))
        return {"state": "ready"}

    def pause_cross_auto(
        self, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("pause", True, audit))
        return {"state": "ready"}

    def cross_auto_status(self) -> dict[str, object]:
        return {}


class _ProductionStore(_Store):
    def safety_policy(self) -> dict[str, object]:
        return {"fingerprint": "policy-1"}


class _ProductionRuntime:
    mode = "production"

    def __init__(self, *, state: str = "RUNNING", owner: bool = True) -> None:
        self.state = state
        self.production_owner = owner
        self.store = _ProductionStore()
        self.monitor = _Monitor()
        self.execution = _ProductionExecution()
        self.cross_venue_monitor = _CrossVenueMonitor()


def _production_request(
    base: str,
    path: str,
    data: bytes = b'{"mode":"manual"}',
    *,
    headers: Mapping[str, str] | None = None,
) -> Request:
    request_headers = {
        "Content-Type": "application/json",
        "Cookie": "ot_prediction_session=session-token",
        "Origin": base,
        "X-CSRF-Token": "csrf-token",
    }
    request_headers.update(headers or {})
    return Request(
        base + path,
        data=data,
        headers=request_headers,
        method="POST",
    )


@contextmanager
def _production_server(
    runtime: _ProductionRuntime | None = None,
) -> Iterator[tuple[str, _ProductionRuntime]]:
    current = runtime or _ProductionRuntime()
    with _server(
        current,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        yield base, current


def test_global_http_capacity_rejects_overflow_and_releases_read_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class TrackingReadStore:
        def __init__(self) -> None:
            self.path = tmp_path / "tracked_reads.sqlite3"
            with sqlite3.connect(self.path) as connection:
                connection.execute("CREATE TABLE reads (value INTEGER)")
            self.lock = threading.Lock()
            self.live_contexts = 0

        @contextmanager
        def read_context(self) -> Iterator[None]:
            connection = sqlite3.connect(self.path)
            with self.lock:
                self.live_contexts += 1
            try:
                yield
            finally:
                connection.close()
                with self.lock:
                    self.live_contexts -= 1

    tracking_store = TrackingReadStore()
    counts = {"active": 0, "max_active": 0, "attempts": 0}
    counts_lock = threading.Lock()
    entered = threading.Event()
    overflow_attempted = threading.Event()
    release = threading.Event()

    def blocked_state_payload(**_kwargs: object) -> dict[str, object]:
        with tracking_store.read_context():
            with counts_lock:
                counts["active"] += 1
                counts["max_active"] = max(counts["max_active"], counts["active"])
                if counts["active"] == 8:
                    entered.set()
            try:
                assert release.wait(timeout=60)
                return {"state": "blocked"}
            finally:
                with counts_lock:
                    counts["active"] -= 1

    def overflow_request(base: str) -> tuple[int, dict[str, object], Mapping[str, str]]:
        with counts_lock:
            counts["attempts"] += 1
            if counts["attempts"] == 40:
                overflow_attempted.set()
        return _response_with_headers(base + "/api/prediction-arbitrage/state", timeout=60)

    monkeypatch.setattr(prediction_service, "prediction_state_payload", blocked_state_payload)
    with _running_server(_Runtime()) as (base, server):
        baseline_handler_ids = _handler_thread_ids()
        baseline_socket_fds = _socket_fd_count()
        try:
            with ThreadPoolExecutor(max_workers=48) as clients:
                leader_timeout = 60
                leaders = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/state",
                        timeout=leader_timeout,
                    )
                    for _ in range(8)
                ]
                assert entered.wait(timeout=30)
                overflow = [clients.submit(overflow_request, base) for _ in range(40)]
                assert overflow_attempted.wait(timeout=30)

                assert counts["max_active"] == 8
                assert server.http_load_snapshot()["active"] == 8  # type: ignore[attr-defined]
                assert len(_handler_thread_ids() - baseline_handler_ids) == 8
                overflow_results = [future.result(timeout=60) for future in overflow]
                overflow_statuses = [result[0] for result in overflow_results]
                overflow_payloads = [result[1] for result in overflow_results]
                overflow_retry_after = [result[2]["Retry-After"] for result in overflow_results]
                overflow_connections = [result[2]["Connection"] for result in overflow_results]
                assert overflow_statuses == [503] * 40
                assert overflow_payloads == [{"error": "prediction service busy"}] * 40
                assert overflow_retry_after == ["1"] * 40
                assert overflow_connections == ["close"] * 40
                assert server.http_load_snapshot()["overload_rejections"] == 40  # type: ignore[attr-defined]

                release.set()
                leader_results = [
                    future.result(timeout=leader_timeout) for future in leaders
                ]
                assert [result[0] for result in leader_results] == [200] * 8
                deadline = time.monotonic() + 5
                while server.http_load_snapshot()["active"] != 0 and time.monotonic() < deadline:  # type: ignore[attr-defined]
                    time.sleep(0.01)
                assert server.http_load_snapshot()["active"] == 0  # type: ignore[attr-defined]
                while (
                    _handler_thread_ids() - baseline_handler_ids
                    or _socket_fd_count() > baseline_socket_fds
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert _handler_thread_ids() == baseline_handler_ids
                assert _socket_fd_count() == baseline_socket_fds
                assert tracking_store.live_contexts == 0
        finally:
            release.set()


def test_mixed_http_capacity_shares_slots_and_exposes_health_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ProductionRuntime()
    counts = {"state": 0, "auth": 0, "body": 0, "preview": 0}
    counts_lock = threading.Lock()
    state_entered = threading.Event()
    preview_entered = threading.Event()
    preview_reentered = threading.Event()
    release_state = threading.Event()
    release_preview = threading.Event()
    release_replacement = threading.Event()

    def blocked_state_payload(**_kwargs: object) -> dict[str, object]:
        with counts_lock:
            counts["state"] += 1
            if counts["state"] == 4:
                state_entered.set()
        assert release_state.wait(timeout=60)
        return {"state": "blocked"}

    def blocked_preview(opportunity_id: str) -> dict[str, object]:
        with counts_lock:
            counts["preview"] += 1
            if counts["preview"] == 4:
                preview_entered.set()
            if counts["preview"] == 5:
                preview_reentered.set()
            release = release_preview if counts["preview"] <= 4 else release_replacement
        assert release.wait(timeout=60)
        return {
            "state": "previewed",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
        }

    monkeypatch.setattr(prediction_service, "prediction_state_payload", blocked_state_payload)
    monkeypatch.setattr(runtime.execution, "preview", blocked_preview)
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, server):
        handler_class = server.RequestHandlerClass  # type: ignore[attr-defined]
        original_auth = handler_class._require_production_auth
        original_read = handler_class._read_json_body

        def traced_auth(handler: object) -> None:
            with counts_lock:
                counts["auth"] += 1
            original_auth(handler)

        def traced_read(handler: object) -> dict[str, object]:
            with counts_lock:
                counts["body"] += 1
            return original_read(handler)

        monkeypatch.setattr(handler_class, "_require_production_auth", traced_auth)
        monkeypatch.setattr(handler_class, "_read_json_body", traced_read)
        try:
            with ThreadPoolExecutor(max_workers=10) as clients:
                state_calls = [
                    clients.submit(_response, base + "/api/prediction-arbitrage/state")
                    for _ in range(4)
                ]
                assert state_entered.wait(timeout=15)

                preview_calls = [
                    clients.submit(
                        _response,
                        _production_request(
                            base,
                            "/api/prediction-arbitrage/preview",
                            data=b'{"opportunity_id":"opp-1"}',
                        ),
                    )
                    for _ in range(4)
                ]
                assert preview_entered.wait(timeout=15)

                assert server.http_load_snapshot()["active"] == 8  # type: ignore[attr-defined]

                for status, payload, headers in (
                    _response_with_headers(
                        base + "/api/prediction-arbitrage/state", timeout=15
                    ),
                    _response_with_headers(
                        _production_request(
                            base,
                            "/api/prediction-arbitrage/preview",
                            data=b"",
                        ),
                        timeout=15,
                    ),
                ):
                    assert status == 503
                    assert payload == {"error": "prediction service busy"}
                    assert headers["Retry-After"] == "1"
                    assert headers["Connection"] == "close"
                assert counts == {"state": 4, "auth": 4, "body": 4, "preview": 4}
                assert server.http_load_snapshot()["overload_rejections"] == 2  # type: ignore[attr-defined]

                release_preview.set()
                deadline = time.monotonic() + 5
                while server.http_load_snapshot()["active"] != 4 and time.monotonic() < deadline:  # type: ignore[attr-defined]
                    time.sleep(0.01)
                assert server.http_load_snapshot()["active"] == 4  # type: ignore[attr-defined]
                replacement = clients.submit(
                    _response,
                    _production_request(
                        base,
                        "/api/prediction-arbitrage/preview",
                        data=b'{"opportunity_id":"opp-1"}',
                    ),
                )
                assert preview_reentered.wait(timeout=15)

                release_state.set()
                release_replacement.set()
                assert [future.result(timeout=60)[0] for future in state_calls] == [200] * 4
                assert [future.result(timeout=60)[0] for future in preview_calls] == [200] * 4
                assert replacement.result(timeout=60)[0] == 200
                deadline = time.monotonic() + 5
                while server.http_load_snapshot()["active"] != 0 and time.monotonic() < deadline:  # type: ignore[attr-defined]
                    time.sleep(0.01)
                assert server.http_load_snapshot()["active"] == 0  # type: ignore[attr-defined]

                health_status, health = _response(base + "/healthz")
                assert health_status == 200
                assert health["http_load"] == {
                    "limit": 8,
                    "active": 1,
                    "overload_rejections": 2,
                    "history_cache_hits": 0,
                    "history_cache_misses": 0,
                }
        finally:
            release_state.set()
            release_preview.set()
            release_replacement.set()


def test_shadow_health_has_the_read_only_identity() -> None:
    with _server(_Runtime()) as base:
        status, payload = _response(base + "/healthz")

    assert status == 200
    assert payload["schema_version"] == "open_trader.prediction_service.health.v1"
    assert payload["module"] == "prediction_service"
    assert payload["status"] == "running"
    assert payload["mode"] == "shadow"
    assert payload["production_owner"] is False
    assert payload["mutations"] == "prohibited"
    assert payload["runtime_state"] == "RUNNING"
    assert payload["codex"] == {
        "relation": {"calls": 1, "successes": 1},
        "cross_venue": {"calls": 2, "successes": 1},
    }
    assert payload["first_violation"] is None
    assert payload["guard_attempts"] == []
    assert isinstance(payload["pid"], int)
    assert isinstance(payload["started_at"], str)
    assert payload["source_state"] in {"clean", "dirty"}
    # code_root 必须来自被 import 的 open_trader 包实际路径,而非 cwd。
    assert payload["code_root"] == str(Path(open_trader.__file__).resolve().parent.parent)
    assert "release_schema_version" not in payload
    assert "reader_generation" not in payload
    assert "contract_generation" not in payload


def test_shadow_state_and_history_use_the_shared_read_model() -> None:
    runtime = _Runtime()
    expected_state = prediction_state_payload(
        store=runtime.store,
        monitor=runtime.monitor,
        execution=runtime.execution,
        csrf_token="",
        cross_venue_monitor=runtime.cross_venue_monitor,
        n_leg_metrics={},
    )
    expected_history = prediction_history_payload(
        runtime.store,
        kind="signals",
        limit=1,
        offset=0,
        monitor=runtime.monitor,
        execution=runtime.execution,
        cross_venue_monitor=runtime.cross_venue_monitor,
    )
    with _server(runtime) as base:
        state_status, state = _response(base + "/api/prediction-arbitrage/state")
        history_status, history = _response(
            base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0"
        )

    assert state_status == history_status == 200
    assert state == expected_state
    assert history == expected_history


def test_lp_state_exposes_reward_threshold_without_paid_profit(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    class RewardExchange:
        def __init__(self) -> None:
            self.reads = 0

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            self.reads += 1
            assert reward_date == "2026-09-14"
            assert condition_id == "condition-1"
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
                "account_amount": Decimal("1.10"),
            }

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "service-reward-session",
        "service-reward-idempotency",
        state="complete",
        payload={
            "condition_id": "condition-1",
            "reward_date": "2026-09-14",
            "paid_rewards": Decimal("0"),
            "trade_pnl": Decimal("0.10"),
            "total_pnl": None,
        },
    )
    exchange = RewardExchange()
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    observed = lp.refresh_rewards()
    assert observed["reward_observation"]["status"] == "met"
    assert exchange.reads == 1

    class RewardExecution(_Execution):
        def __init__(self) -> None:
            self.status_calls = 0
            self.write_calls = 0

        def lp_status(self) -> dict[str, object]:
            self.status_calls += 1
            return lp.status()

        def lp_preview(self, _request: object) -> dict[str, object]:
            self.write_calls += 1
            raise AssertionError("state read must not submit an LP order")

    runtime = _Runtime()
    execution = RewardExecution()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = execution

    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/state")

    assert status == 200
    observation = payload["lp_session"]["reward_observation"]
    assert observation["status"] == "met"
    assert observation["threshold_status"] == "met"
    assert observation["reward_date"] == "2026-09-14"
    assert observation["market_amount"] == "0.80"
    assert observation["account_amount"] == "1.10"
    assert observation["gap"] == "0"
    assert observation["checked_at"] == "2026-09-14T12:00:00.000000Z"
    assert observation["paid"] is False
    assert payload["lp_session"]["paid_rewards"] == "0"
    assert payload["lp_session"]["trade_pnl"] == "0.10"
    assert "total_pnl" not in payload["lp_session"]
    assert execution.status_calls == 1
    assert execution.write_calls == 0


def test_lp_dashboard_shows_manual_orders_without_managing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RewardTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            self.calls.append((path, dict(params)))
            reward = {
                "date": f"{params['date']}T00:00:00Z",
                "maker_address": "wallet",
                "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                "earnings": "0.25",
                "asset_rate": "0.9999",
            }
            if path == "/rewards/user/total":
                return [reward]
            if params.get("sponsored") is False:
                return {
                    "data": [{**reward, "condition_id": "condition-1"}],
                    "next_cursor": "LTE=",
                }
            return {"data": [], "next_cursor": "LTE="}

    reward_transport = RewardTransport()
    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )

    class AccountSDK:
        def __init__(self) -> None:
            self.open_order_reads = 0
            self.order_writes = 0
            self.cancellations = 0
            self.scoring_reads: list[str] = []
            self._ctx = SimpleNamespace(
                secure_clob=reward_transport, wallet_type=None
            )
            self.environment = SimpleNamespace(standard_exchange="standard-exchange")

        def get_balance_allowance(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                balance=30_000_000,
                allowances={"standard-exchange": 30_000_000},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            self.open_order_reads += 1
            if self.open_order_reads > 1:
                raise RuntimeError("account read unavailable")
            return [
                {
                    "id": "manual-order",
                    "market": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.50"),
                    "original_size": Decimal("20"),
                    "size_matched": Decimal("5"),
                }
            ]

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return [
                {
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "size": Decimal("5"),
                }
            ]

        def get_order_scoring(self, *, order_id: str) -> bool:
            self.scoring_reads.append(order_id)
            return True

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

        def post_order(self, _order: object) -> object:
            self.order_writes += 1
            return object()

        def cancel_orders(self, **_kwargs: object) -> object:
            self.cancellations += 1
            return object()

    class PublicMarketSDK:
        def __init__(self) -> None:
            self.closed = False

        def list_markets(self, *, condition_ids: object) -> list[object]:
            assert tuple(condition_ids) == ("condition-1",)  # type: ignore[arg-type]
            return [
                {
                    "id": "market-1",
                    "condition_id": "condition-1",
                    "question": "Will it happen?",
                    "slug": "will-it-happen",
                }
            ]

        def close(self) -> None:
            self.closed = True

    sdk = AccountSDK()
    public_market = PublicMarketSDK()
    service, trading, store, monitor = execution_fixture(tmp_path)
    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        client=sdk,
        public_client_factory=lambda: public_market,
    )
    service._trading = trading
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor
    runtime.execution = service

    with _server(runtime) as base:
        status, first = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        status_after_failure, stale = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )

    assert status == status_after_failure == 200
    assert first["stale"] is False
    first_order = first["orders"][0]
    assert first_order["management"] == "manual_read_only"
    assert first_order["filled_quantity"] == "5"
    assert first_order["quantity"] == "20"
    assert first_order["market_title"] == "Will it happen?"
    assert first_order["scoring_status"] is True
    assert datetime.fromisoformat(
        str(first_order["scoring_checked_at"]).replace("Z", "+00:00")
    ).tzinfo is not None
    assert "market_amount_raw" not in first_order
    assert first["positions"][0]["size"] == "5"
    assert first["positions"][0]["market_title"] == "Will it happen?"
    market_reward = first["market_rewards"]["condition-1"]
    assert market_reward["state"] == "unknown"
    assert market_reward["usd_state"] == "unknown"
    assert market_reward["market_amount"] is None
    assert market_reward["market_amount_raw"] == "0.25"
    assert market_reward["market_asset"] == "USDC.e"
    assert market_reward["paid"] is False
    assert "account_amount" not in market_reward
    assert datetime.fromisoformat(
        str(market_reward["checked_at"]).replace("Z", "+00:00")
    ).tzinfo is not None
    assert stale["stale"] is True
    assert stale["checked_at"] == first["checked_at"]
    assert stale["orders"] == first["orders"]
    assert stale["positions"] == first["positions"]
    assert sdk.open_order_reads == 2
    assert sdk.scoring_reads == ["manual-order"]
    assert len(reward_transport.calls) == 3
    assert sdk.order_writes == 0
    assert sdk.cancellations == 0
    assert public_market.closed is True


def test_lp_candidate_preview_rechecks_best_bid_before_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "0x" + "c" * 64
    token_id = "0x" + "1" * 64
    public_state: dict[str, object] = {
        "bids": [
            {"price": Decimal("0.51"), "size": Decimal("1")},
            {"price": Decimal("0.50"), "size": Decimal("100")},
        ],
        "max_spread": Decimal("10"),
        "reward_min_size": Decimal("20"),
        "book_reads": 0,
        "public_creates": 0,
        "public_closes": 0,
        "catalog_sources": [],
        "metadata_conditions": [],
        "book_batches": [],
        "catalog_fail": False,
        "omit_no_book": False,
    }
    clock_state = {"now": datetime.now(UTC)}

    class AdapterDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            cls.calls += 1
            moment = clock_state["now"]
            if cls.calls == 2:
                moment -= timedelta(seconds=200)
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)  # type: ignore[arg-type]

    monkeypatch.setattr(polymarket_trading_module, "datetime", AdapterDateTime)

    class AccountSDK:
        def __init__(self) -> None:
            self.environment = SimpleNamespace(standard_exchange="standard-exchange")
            self.balance_units = 1_000_000_000
            self.balance_reads = 0
            self.order_reads = 0
            self.trade_reads = 0
            self.position_reads = 0
            self.open_orders: list[object] = []
            self.limit_orders: list[dict[str, object]] = []
            self.posts: list[dict[str, object]] = []

        def get_balance_allowance(self, **_kwargs: object) -> object:
            self.balance_reads += 1
            return SimpleNamespace(
                balance=self.balance_units,
                allowances={"standard-exchange": self.balance_units},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            self.order_reads += 1
            return list(self.open_orders)

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            self.trade_reads += 1
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            self.position_reads += 1
            return []

        def create_limit_order(self, **kwargs: object) -> dict[str, object]:
            self.limit_orders.append(dict(kwargs))
            return {
                "post_only": kwargs.get("post_only"),
                "order_type": "GTD",
                "token_id": kwargs.get("token_id"),
                "price": kwargs.get("price"),
                "size": kwargs.get("size"),
                "side": kwargs.get("side"),
                "expiration": kwargs.get("expiration"),
            }

        def post_order(self, signed_order: object) -> object:
            assert isinstance(signed_order, dict)
            self.posts.append(signed_order)
            return {**signed_order, "order_id": "lp-order-1", "status": "LIVE"}

    class PublicMarketSDK:
        def _market(self) -> dict[str, object]:
            return {
                "id": "market-1",
                "condition_id": condition_id,
                "question": "Will it happen?",
                "slug": "will-it-happen",
                "state": {"accepting_orders": True},
                "outcomes": {
                    "yes": {"label": "Yes", "token_id": token_id},
                    "no": {"label": "No", "token_id": "no-token"},
                },
                "trading": {
                    "minimum_order_size": Decimal("1"),
                    "minimum_tick_size": Decimal("0.01"),
                    "fees_enabled": False,
                },
                "rewards": {
                    "rewards_min_size": public_state["reward_min_size"],
                    "rewards_max_spread": public_state["max_spread"],
                },
            }

        def __init__(self) -> None:
            public_state["public_creates"] = int(public_state["public_creates"]) + 1

        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            cast_sources = public_state["catalog_sources"]
            assert isinstance(cast_sources, list)
            cast_sources.append(sponsored)
            if public_state["catalog_fail"] is True:
                raise RuntimeError("synthetic reward catalog failure")
            if sponsored:
                return []
            return [
                {
                    "condition_id": condition_id,
                    "rewards_min_size": public_state["reward_min_size"],
                    "rewards_max_spread": public_state["max_spread"],
                    "rewards_config": [
                        {
                            "id": "native-config-1",
                            "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                            "start_date": "2026-01-01",
                            "end_date": "2026-12-31",
                            "rate_per_day": Decimal("2"),
                        }
                    ],
                }
            ]

        def list_markets(self, *, condition_ids: object) -> list[object]:
            cast_conditions = public_state["metadata_conditions"]
            assert isinstance(cast_conditions, list)
            conditions = tuple(condition_ids)  # type: ignore[arg-type]
            cast_conditions.append(conditions)
            return [self._market()] if condition_id in conditions else []

        def get_order_books(self, *, token_ids: object) -> list[object]:
            cast_batches = public_state["book_batches"]
            assert isinstance(cast_batches, list)
            tokens = tuple(token_ids)  # type: ignore[arg-type]
            cast_batches.append(tokens)
            books: list[object] = []
            for current_token in tokens:
                if (
                    current_token != token_id
                    and public_state["omit_no_book"] is True
                ):
                    continue
                if current_token == token_id:
                    bids = list(public_state["bids"])
                    asks = [{"price": Decimal("0.53"), "size": Decimal("100")}]
                else:
                    bids = [{"price": Decimal("0.49"), "size": Decimal("100")}]
                    asks = [{"price": Decimal("0.51"), "size": Decimal("100")}]
                books.append(
                    {
                        "condition_id": condition_id,
                        "asset_id": current_token,
                        "timestamp": datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
                        "bids": bids,
                        "asks": asks,
                        "min_order_size": Decimal("1"),
                        "tick_size": Decimal("0.01"),
                    }
                )
            return books

        def get_market(self, *, id: str) -> object:
            assert id == "market-1"
            return self._market()

        def get_order_book(self, *, token_id: str) -> object:
            assert token_id == "0x" + "1" * 64
            public_state["book_reads"] = int(public_state["book_reads"]) + 1
            return {
                "market": condition_id,
                "asset_id": token_id,
                "timestamp": datetime.now(UTC),
                "bids": list(public_state["bids"]),  # type: ignore[arg-type]
                "asks": [
                    {"price": Decimal("0.53"), "size": Decimal("100")}
                ],
                "min_order_size": Decimal("1"),
                "tick_size": Decimal("0.01"),
            }

        def close(self) -> None:
            public_state["public_closes"] = int(public_state["public_closes"]) + 1

    sdk = AccountSDK()
    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        client=sdk,
        public_client_factory=PublicMarketSDK,
    )
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, trading, clock=lambda: clock_state["now"])
    AdapterDateTime.calls = 0
    scanned = lp.refresh_candidates(force=True)
    assert scanned["state"] == "ready"
    assert scanned["complete"] is True
    assert len(scanned["candidates"]) == 2
    assert sdk.balance_reads == sdk.order_reads == sdk.trade_reads == sdk.position_reads == 1
    assert len(public_state["catalog_sources"]) == 2
    assert len(public_state["metadata_conditions"]) == 1
    assert len(public_state["book_batches"]) == 1
    public_state["omit_no_book"] = True
    clock_state["now"] += timedelta(seconds=1)
    partial_scan = lp.refresh_candidates(force=True)
    assert partial_scan["state"] == "incomplete"
    assert partial_scan["complete"] is False
    assert partial_scan["stale"] is False
    assert len(partial_scan["candidates"]) == 1
    assert partial_scan["candidates"][0]["outcome"] == "YES"
    execution = PredictionExecutionService(
        store=store,
        monitor=_Monitor(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
        lp=lp,
    )
    execution._breaker_open = False
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
    )
    candidate = json.dumps(
        {
            "market_id": "market-1",
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
        }
    ).encode("utf-8")

    def candidate_preview(base: str) -> tuple[int, dict[str, object]]:
        return _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/candidates/preview",
                data=candidate,
            )
        )

    def confirm_preview(
        base: str, preview_id: object
    ) -> tuple[int, dict[str, object]]:
        return _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": preview_id,
                        "idempotency_key": "lp-candidate-preview",
                    }
                ).encode("utf-8"),
            )
        )

    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        dashboard_status, dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert dashboard_status == 200
        candidate_rows = dashboard["candidates"]
        assert isinstance(candidate_rows, list)
        assert dashboard["complete"] is False
        assert dashboard["candidate_stale"] is False
        assert any(
            row.get("market_id") == "market-1"
            and row.get("outcome") == "YES"
            and row.get("price") == "0.51"
            and row.get("quantity") == "20"
            for row in candidate_rows
            if isinstance(row, dict)
        )
        preview_status, preview = candidate_preview(base)
        assert preview_status == 200
        assert preview["state"] == "previewed"
        assert preview["request"]["price"] == "0.51"
        assert preview["request"]["quantity"] == "20"
        public_state["omit_no_book"] = False
        clock_state["now"] += timedelta(seconds=1)
        complete_scan = lp.refresh_candidates(force=True)
        assert complete_scan["complete"] is True
        assert complete_scan["stale"] is False
        assert len(complete_scan["candidates"]) == 2
        public_state["bids"] = [
            {"price": Decimal("0.52"), "size": Decimal("1")},
            {"price": Decimal("0.50"), "size": Decimal("100")},
        ]
        stale_price_status, stale_price = confirm_preview(
            base, preview["preview_id"]
        )
        assert stale_price_status == 200
        assert stale_price["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []

        public_state["bids"] = [
            {"price": Decimal("0.51"), "size": Decimal("1")},
            {"price": Decimal("0.50"), "size": Decimal("100")},
        ]
        funds_preview_status, funds_preview = candidate_preview(base)
        assert funds_preview_status == 200
        assert funds_preview["request"]["price"] == "0.51"
        assert funds_preview["request"]["quantity"] == "20"
        sdk.balance_units = 10_190_000
        low_funds_status, low_funds = confirm_preview(
            base, funds_preview["preview_id"]
        )
        assert low_funds_status == 200
        assert low_funds["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []

        sdk.balance_units = 1_000_000_000
        score_preview_status, score_preview = candidate_preview(base)
        assert score_preview_status == 200
        assert score_preview["request"]["price"] == "0.51"
        assert score_preview["request"]["quantity"] == "20"
        public_state["max_spread"] = Decimal("0.5")
        zero_score_status, zero_score = confirm_preview(
            base, score_preview["preview_id"]
        )
        assert zero_score_status == 200
        assert zero_score["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []

        public_state["max_spread"] = Decimal("10")
        size_preview_status, size_preview = candidate_preview(base)
        assert size_preview_status == 200
        assert size_preview["request"]["quantity"] == "20"
        public_state["reward_min_size"] = Decimal("19")
        stale_size_status, stale_size = confirm_preview(base, size_preview["preview_id"])
        assert stale_size_status == 200
        assert stale_size["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []
        resized_status, resized = candidate_preview(base)
        assert resized_status == 200
        assert resized["request"]["quantity"] == "19"
        public_state["reward_min_size"] = Decimal("20")

        commitment_preview_status, commitment_preview = candidate_preview(base)
        assert commitment_preview_status == 200
        assert commitment_preview["request"]["quantity"] == "20"
        sdk.balance_units = 30_000_000
        sdk.open_orders = [
            {
                "id": "other-market-buy",
                "market": "other-condition",
                "asset_id": "other-token",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.50"),
                "original_size": Decimal("40"),
                "size_matched": Decimal("0"),
            }
        ]
        committed_status, committed = confirm_preview(
            base, commitment_preview["preview_id"]
        )
        assert committed_status == 200
        assert committed["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []
        sdk.open_orders = []
        sdk.balance_units = 1_000_000_000

        public_state["max_spread"] = Decimal("10")
        refreshed_status, refreshed = candidate_preview(base)
        assert refreshed_status == 200
        assert refreshed["state"] == "previewed"
        assert refreshed["request"]["price"] == "0.51"
        assert refreshed["request"]["quantity"] == "20"
        start_body = json.dumps(
            {
                "preview_id": refreshed["preview_id"],
                "idempotency_key": "lp-candidate-preview",
            }
        ).encode("utf-8")
        start_status, started = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/sessions", data=start_body
            )
        )
        repeated_status, repeated = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/sessions", data=start_body
            )
        )

    assert start_status == repeated_status == 200
    assert started["state"] == repeated["state"] == "entry_open"
    assert started["session_id"] == repeated["session_id"]
    assert len(sdk.limit_orders) == 1
    assert {
        key: sdk.limit_orders[0][key]
        for key in ("token_id", "price", "size", "side", "post_only")
    } == {
        "token_id": token_id,
        "price": Decimal("0.51"),
        "size": Decimal("20"),
        "side": "BUY",
        "post_only": True,
    }
    assert sdk.posts[0]["price"] == Decimal("0.51")
    assert sdk.posts[0]["size"] == Decimal("20")
    assert sdk.posts[0]["side"] == "BUY"
    assert sdk.posts[0]["post_only"] is True
    assert sdk.posts[0]["order_type"] == "GTD"
    assert len(sdk.posts) == 1
    previous_scan = lp.candidate_snapshot()
    previous_checked_at = previous_scan["checked_at"]
    previous_candidates = previous_scan["candidates"]
    public_state["catalog_fail"] = True
    clock_state["now"] += timedelta(seconds=301)
    failed_scan = lp.refresh_candidates(force=True)
    assert failed_scan["stale"] is True
    assert failed_scan["checked_at"] == previous_checked_at
    assert failed_scan["last_success_at"] == previous_scan["last_success_at"]
    assert failed_scan["candidates"] == previous_candidates
    assert public_state["public_creates"] == public_state["public_closes"]


def test_history_single_flight_reuses_identical_inflight_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def controlled_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        leader_entered.set()
        assert release_leader.wait(timeout=5)
        return {"call": call}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", controlled_history)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=8) as clients:
                requests = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0",
                    )
                    for _ in range(8)
                ]
                assert leader_entered.wait(timeout=5)
                for _ in range(100):
                    if server.http_load_snapshot()["history_cache_hits"] == 7:  # type: ignore[attr-defined]
                        break
                    time.sleep(0.01)
                assert calls == 1
                load = server.http_load_snapshot()  # type: ignore[attr-defined]
                assert load["history_cache_misses"] == 1
                assert load["history_cache_hits"] == 7

                release_leader.set()
                assert [future.result(timeout=5) for future in requests] == [
                    (200, {"call": 1})
                ] * 8
        finally:
            release_leader.set()


def test_history_cache_ttl_expires_and_keeps_pagination_keys_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    clock = [0.0]

    def controlled_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", controlled_history)
    monkeypatch.setattr(prediction_service.time, "monotonic", lambda: clock[0])
    with _running_server(_Runtime()) as (base, server):
        def get_history(query: str) -> tuple[int, dict[str, object]]:
            return _response(base + "/api/prediction-arbitrage/history?" + query)

        first_payload = (200, {"call": 1})
        assert get_history("kind=signals&limit=1&offset=0") == first_payload
        assert calls == 1

        clock[0] = 0.999
        assert get_history("kind=signals&limit=1&offset=0") == first_payload
        assert calls == 1

        clock[0] = 1.001
        second_payload = (200, {"call": 2})
        assert get_history("kind=signals&limit=1&offset=0") == second_payload
        assert calls == 2

        assert get_history("kind=executions&limit=1&offset=0") == (200, {"call": 3})
        assert get_history("kind=executions&limit=1&offset=1") == (200, {"call": 4})
        assert calls == 4
        assert server.http_load_snapshot()["history_cache_misses"] == 4  # type: ignore[attr-defined]


def test_history_flight_failure_wakes_followers_and_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def failing_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            leader_entered.set()
            assert release_leader.wait(timeout=5)
            raise sqlite3.OperationalError("database is locked")
        return {"call": call}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", failing_history)
    query = "kind=signals&limit=1&offset=0"
    key = ("signals", 1, 0)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=8) as clients:
                requests = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/history?" + query,
                    )
                    for _ in range(8)
                ]
                assert leader_entered.wait(timeout=5)
                for _ in range(100):
                    if server.http_load_snapshot()["history_cache_hits"] == 7:  # type: ignore[attr-defined]
                        break
                    time.sleep(0.01)
                release_leader.set()

                assert [future.result(timeout=5) for future in requests] == [
                    (503, {"error": "prediction history unavailable"})
                ] * 8
            assert calls == 1
            assert key not in server._history_cache  # type: ignore[attr-defined]
            assert _response(base + "/api/prediction-arbitrage/history?" + query) == (
                200,
                {"call": 2},
            )
            assert calls == 2
        finally:
            release_leader.set()


def test_history_wait_timeout_keeps_the_leader_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def blocked_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        leader_entered.set()
        assert release_leader.wait(timeout=5)
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", blocked_history)
    monkeypatch.setattr(prediction_service, "_HISTORY_WAIT_SECONDS", 0.05)
    query = "kind=signals&limit=1&offset=0"
    key = ("signals", 1, 0)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=2) as clients:
                leader = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/history?" + query,
                )
                assert leader_entered.wait(timeout=5)
                follower = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/history?" + query,
                )
                assert follower.result(timeout=1) == (
                    503,
                    {"error": "prediction history unavailable"},
                )
                assert calls == 1
                assert key in server._history_flights  # type: ignore[attr-defined]
                deadline = time.monotonic() + 5
                active = server.http_load_snapshot()["active"]  # type: ignore[attr-defined]
                while active != 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    active = server.http_load_snapshot()["active"]  # type: ignore[attr-defined]
                assert active == 1

                release_leader.set()
                assert leader.result(timeout=5) == (200, {"call": 1})
        finally:
            release_leader.set()


def test_history_cache_never_serves_expired_payload_after_recompute_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    clock = [0.0]

    def stale_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", stale_history)
    monkeypatch.setattr(prediction_service.time, "monotonic", lambda: clock[0])
    with _running_server(_Runtime()) as (base, _server_instance):
        path = base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0"
        assert _response(path) == (200, {"call": 1})
        clock[0] = 1.001
        assert _response(path) == (
            503,
            {"error": "prediction history unavailable"},
        )
        assert calls == 2


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("kind=signals&limit=0", "limit must be positive"),
        ("kind=unknown&limit=1", "kind must be signals, executions, or incidents"),
        ("kind=unknown&limit=0", "limit must be positive"),
    ),
)
def test_shadow_history_rejects_invalid_query(query: str, expected: str) -> None:
    with _running_server(_Runtime()) as (base, server):
        status, payload = _response(
            base + "/api/prediction-arbitrage/history?" + query
        )

    assert status == 400
    assert payload == {"error": expected}
    assert server.http_load_snapshot()["history_cache_hits"] == 0  # type: ignore[attr-defined]
    assert server.http_load_snapshot()["history_cache_misses"] == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", FROZEN_PREDICTION_MUTATION_PATHS)
def test_shadow_rejects_every_mutation_before_dispatch(path: str) -> None:
    runtime = _Runtime()
    with _server(runtime) as base:
        request = Request(
            base + path,
            data=b'{"unexpected":"payload"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status, payload = _response(request)

    assert status == 403
    assert payload == {
        "code": "shadow_read_only",
        "message": "Shadow Prediction Service is read-only",
    }


def test_shadow_health_and_reads_fail_closed_when_runtime_is_not_running() -> None:
    with _server(_Runtime(state="FAILED")) as base:
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(base + "/api/prediction-arbitrage/state")

    assert health_status == state_status == 503
    assert health["status"] == "unavailable"
    assert state == {"error": "shadow runtime is unavailable"}


def test_shadow_health_and_reads_fail_closed_after_a_violation() -> None:
    violation = {"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}
    with _server(_Runtime(violation=violation)) as base:
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(base + "/api/prediction-arbitrage/state")

    assert health_status == state_status == 503
    assert health["first_violation"] == violation
    assert health["guard_attempts"] == [violation]
    assert state == {"error": "shadow runtime is unavailable"}


def test_shadow_service_rejects_non_loopback_before_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_prediction_server(runtime=_Runtime(), host="0.0.0.0", port=0)


@pytest.mark.parametrize("method", ("HEAD", "PUT", "DELETE", "OPTIONS"))
def test_unsupported_http_methods_return_not_found(method: str) -> None:
    with _server(_Runtime()) as base:
        status = _status(Request(base + "/unsupported", method=method))

    assert status == 404


def _status(request: Request) -> int:
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code


def test_shadow_mutations_do_not_read_body_or_dispatch_downstream() -> None:
    runtime = _Runtime()
    probes = []

    class Probe:
        def __init__(self) -> None:
            self.calls: list[str] = []
            probes.append(self)

        def __getattr__(self, name: str) -> object:
            self.calls.append(name)
            raise AssertionError(f"unexpected downstream access: {name}")

    runtime.store = Probe()
    runtime.monitor = Probe()
    runtime.execution = Probe()
    runtime.cross_venue_monitor = Probe()
    runtime.session = Probe()
    runtime.csrf = Probe()
    with _server(runtime) as base:
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/preview HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    "Content-Length: 999999\r\n"
                    "Content-Type: application/json\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"403" in response.split(b"\r\n", 1)[0]
    assert all(probe.calls == [] for probe in probes)


@pytest.mark.parametrize(
    ("header", "value"),
    (
        ("Host", "evil.example"),
        ("Origin", "https://evil.example"),
        ("Cookie", "ot_prediction_session=wrong"),
        ("X-CSRF-Token", "wrong"),
    ),
)
def test_production_mutation_rejects_invalid_request_identity_before_dispatch(
    header: str, value: str
) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                headers={header: value},
            )
        )

    assert status == 403
    assert runtime.execution.calls == []


def test_production_auth_precedes_body_limits_and_route_dispatch() -> None:
    with _production_server() as (base, runtime):
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/preview HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    "Origin: https://evil.example\r\n"
                    "Content-Length: 9999999\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"403" in response.split(b"\r\n", 1)[0]
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    "body",
    (
        b"{}",
        b'{"mode":"manual","extra":true}',
        b'[{"mode":"manual"}]',
        b"not-json",
    ),
)
def test_production_control_rejects_invalid_json_schema(body: bytes) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(
                base, "/api/prediction-arbitrage/mode", data=body
            )
        )

    assert status == 400
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    (
        (
            "/api/prediction-arbitrage/preview",
            b'{"opportunity_id":"opp-1","unexpected":true}',
        ),
        (
            "/api/prediction-arbitrage/executions",
            b'{"preview_id":"preview-1"}',
        ),
    ),
)
def test_production_execution_mutations_reject_invalid_schema(
    path: str, body: bytes
) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(base, path, data=body)
        )

    assert status == 400
    assert runtime.execution.calls == []


def test_production_control_rejects_body_over_one_mib_before_reading() -> None:
    with _production_server() as (base, runtime):
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/mode HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    f"Origin: {base}\r\n"
                    "Cookie: ot_prediction_session=session-token\r\n"
                    "X-CSRF-Token: csrf-token\r\n"
                    "Content-Length: 1048577\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"413" in response.split(b"\r\n", 1)[0]
    assert runtime.execution.calls == []


def test_production_exposes_prediction_preview_and_confirmation() -> None:
    with _production_server() as (base, runtime):
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"opp-1"}',
            )
        )
        execution_status, execution = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/executions",
                data=b'{"preview_id":"preview-1","idempotency_key":"key-1"}',
            )
        )

    assert preview_status == execution_status == 200
    assert preview["preview_id"] == execution["preview_id"] == "preview-1"
    assert execution["idempotency_key"] == "key-1"
    assert runtime.execution.calls == [
        ("preview", "opp-1", {}),
        (
            "confirm",
            {"preview_id": "preview-1", "idempotency_key": "key-1"},
            {},
        ),
    ]


def test_lp_routes_preserve_guard_and_idempotency(tmp_path: Path) -> None:
    """The HTTP seam keeps LP writes guarded and exposes durable risk reads."""

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    condition_id = "0x" + "c" * 64
    token_id = "0x" + "1" * 64

    class Exchange:
        def __init__(self) -> None:
            self.posts: list[dict[str, object]] = []
            self.cancels: list[str] = []
            self.snapshot = {
                "account": {
                    "authenticated": True,
                    "balance": Decimal("100"),
                    "allowance": Decimal("100"),
                    "positions": [],
                    "open_orders": [],
                },
                "market": {
                    "market_id": "market-1",
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome": "YES",
                    "accepting_orders": True,
                    "exchange_type": "CLOB",
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("1"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "reward_min_size": Decimal("1"),
                    "reward_max_spread": Decimal("0.10"),
                },
                "book": {
                    "timestamp": now,
                    "received_at": now,
                    "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
                    "bids": [{"price": Decimal("0.29"), "size": Decimal("100")}],
                },
                "trades": [],
                "orders": [],
                "orders_terminal": True,
            }

        def lp_snapshot(self, _request: Mapping[str, object]) -> dict[str, object]:
            return self.snapshot

        def create_limit_order(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        def post_order(self, signed: dict[str, object]) -> dict[str, object]:
            self.posts.append(dict(signed))
            return {**signed, "order_id": "lp-order-1", "status": "LIVE"}

        def cancel_order(self, order_id: str) -> dict[str, object]:
            self.cancels.append(order_id)
            return {"status": "CANCELED", "order_id": order_id}

    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    execution = PredictionExecutionService(
        store=store,
        monitor=_Monitor(),
        trading=exchange,
        notifier=NullNotifier(),
        lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
        lp=lp,
    )
    execution._breaker_open = False

    production_runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
    )

    request = {
        "market_id": "market-1",
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": "YES",
        "question": "Will it happen?",
        "price": "0.30",
        "quantity": "10",
        "review_at": (now + timedelta(minutes=10)).isoformat(),
    }
    body = json.dumps(request).encode("utf-8")

    with _running_server(
        production_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        current_status, current = _response(
            base + "/api/prediction-arbitrage/lp/sessions/current"
        )
        preview_status, preview = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/preview", data=body
            )
        )
        assert preview_status == 200
        assert preview["state"] == "previewed"
        assert exchange.posts == []
        start_body = json.dumps(
            {
                "preview_id": preview["preview_id"],
                "idempotency_key": "lp-api-1",
            }
        ).encode("utf-8")
        first_status, first = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/sessions", data=start_body
            )
        )
        repeat_status, repeat = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/sessions", data=start_body
            )
        )
        current_after_start_status, current_after_start = _response(
            base + "/api/prediction-arbitrage/lp/sessions/current"
        )
        stop_path = (
            "/api/prediction-arbitrage/lp/sessions/"
            f"{first['session_id']}/stop"
        )
        stop_status, stopped = _response(
            _production_request(base, stop_path, data=b"{}")
        )
        repeat_stop_status, repeat_stopped = _response(
            _production_request(base, stop_path, data=b"{}")
        )

        assert current_status == 200
        assert current["state"] == "none"
        assert "residual_quantity" not in current
        assert first_status == repeat_status == 200
        assert first["state"] == repeat["state"] == "entry_open"
        assert first["session_id"] == repeat["session_id"]
        assert len(exchange.posts) == 1
        assert current_after_start_status == 200
        assert current_after_start["state"] == "entry_open"
        assert stop_status == repeat_stop_status == 200
        assert stopped["state"] == repeat_stopped["state"] == "review"
        assert exchange.cancels == ["lp-order-1"]
        assert stopped["session_id"] == first["session_id"]

        # A breaker opened after preview prevents consuming it or writing an
        # order, while the already persisted session remains readable.
        execution._breaker_open = True
        locked_preview_status, locked_preview = _response(
            _production_request(
                base, "/api/prediction-arbitrage/lp/preview", data=body
            )
        )
        locked_start_status, locked_start = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": locked_preview["preview_id"],
                        "idempotency_key": "lp-api-2",
                    }
                ).encode("utf-8"),
            )
        )
        assert locked_preview_status == 200
        assert locked_start_status == 200
        assert locked_start == {
            "state": "locked",
            "reason": "circuit_breaker_open",
        }
        assert len(exchange.posts) == 1

        store.lp_update_session(
            str(first["session_id"]),
            patch={
                "buy_filled_quantity": Decimal("5"),
                "residual_quantity": Decimal("5"),
                "residual_exit_value": Decimal("1.20"),
                "position_reconciled": True,
                "scoring_status": "unknown",
                "scoring_checked_at": now - timedelta(seconds=20),
            },
        )

    shadow_runtime = SimpleNamespace(
        mode="shadow",
        state="RUNNING",
        production_owner=False,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
        shadow_evidence={
            "mode": "shadow",
            "guard_attempts": [],
            "first_violation": None,
            "codex": {"relation": {"calls": 0, "successes": 0}},
        },
    )

    with _server(shadow_runtime) as shadow_base:
        shadow_current_status, shadow_current = _response(
            shadow_base + "/api/prediction-arbitrage/lp/sessions/current"
        )
        shadow_state_status, shadow_state = _response(
            shadow_base + "/api/prediction-arbitrage/state"
        )
        shadow_post_status, _shadow_post = _response(
            _production_request(
                shadow_base, "/api/prediction-arbitrage/lp/preview", data=body
            )
        )

    assert shadow_current_status == shadow_state_status == 200
    assert shadow_current["state"] == "review"
    assert shadow_current["residual_quantity"] == "5"
    assert shadow_current["scoring_status"] == "unknown"
    assert shadow_state["lp_session"]["residual_quantity"] == "5"
    assert shadow_state["lp_session"]["state"] == "review"
    assert shadow_post_status == 403
    assert len(exchange.posts) == 1


def test_production_http_confirmation_preserves_execution_idempotency(
    tmp_path: Path,
) -> None:
    execution_service, trading, store, monitor = execution_fixture(
        tmp_path, result="both_rejected"
    )
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]
    runtime.cross_venue_monitor = None  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        rejection_status, rejection = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"missing"}',
            )
        )
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"opp-1"}',
            )
        )
        request = json.dumps(
            {
                "preview_id": preview["preview_id"],
                "idempotency_key": "same-request",
            }
        ).encode("utf-8")
        first_status, first = _response(
            _production_request(
                base, "/api/prediction-arbitrage/executions", data=request
            )
        )
        second_status, second = _response(
            _production_request(
                base, "/api/prediction-arbitrage/executions", data=request
            )
        )

    final = wait_until_terminal(execution_service, str(first["execution_id"]))
    assert rejection_status == preview_status == first_status == second_status == 200
    assert rejection == {"state": "rejected", "reason": "opportunity_unavailable"}
    assert preview["total_max_cost"] == "8.00"
    assert preview["minimum_profit"] == "2.00"
    assert preview["wallet_address"] == "0x1111…1111"
    assert "intent" not in preview
    assert second["execution_id"] == first["execution_id"]
    assert final["state"] == "both_rejected"
    assert trading.batch_calls == 1


def test_production_http_previews_llm_relationship_economics(tmp_path: Path) -> None:
    execution_service, _trading, store, _monitor = threshold_execution_fixture(
        tmp_path
    )
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"threshold-opp-1"}',
            )
        )

    assert status == 200
    assert preview["state"] == "previewed"
    assert preview["intent_type"] == "threshold_hedge"
    assert preview["total_max_cost"] == "2.12"
    assert preview["minimum_profit"] == "7.88"
    assert preview["llm_status"] == "approved"


def test_production_http_previews_cross_venue_economics(tmp_path: Path) -> None:
    execution_service, store, _trading, _cross, _predict = _cross_service(tmp_path)
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=(
                    b'{"opportunity_id":'
                    b'"cross:public-pair:PREDICT_YES_POLYMARKET_NO"}'
                ),
            )
        )

    assert status == 200
    assert preview["state"] == "previewed"
    assert preview["market_type"] == "cross_venue_yes_no"
    assert preview["maximum_total_cost"] == "4.80"
    assert preview["minimum_profit"] == "0.20"
    assert [leg["exchange"] for leg in preview["buy_legs"]] == [
        "predict.fun",
        "polymarket",
    ]


def test_production_rejects_unknown_prediction_mutation() -> None:
    with _production_server() as (base, runtime):
        status, payload = _response(
            _production_request(base, "/api/prediction-arbitrage/unknown")
        )

    assert status == 404
    assert payload == {"error": "not found"}
    assert runtime.execution.calls == []


def test_production_control_maps_conflict_and_storage_failure() -> None:
    runtime = _ProductionRuntime()
    with _production_server(runtime) as (base, _runtime):
        runtime.execution.mode_result = {
            "state": "busy",
            "reason": "control_in_progress",
        }
        busy_status, _busy = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )
        runtime.execution.error = sqlite3.OperationalError("database is locked")
        unavailable_status, _unavailable = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )

    assert busy_status == 409
    assert unavailable_status == 503
    assert runtime.execution.calls[0][2] == {
        "actor": "local_operator",
        "git_sha": "abc123",
        "safety_fingerprint": "policy-1",
    }


class _ProviderSnapshotValidator:
    """The runtime validator's snapshot contract, backed by a real store."""

    def __init__(self, store: PredictionArbitrageStore) -> None:
        self._store = store

    def provider_snapshot(self) -> dict[str, object]:
        default = resolve_provider(
            os.environ.get("OPEN_TRADER_PREDICTION_LLM_PROVIDER")
        )
        return {
            "selected": self._store.get_llm_provider(default=default),
            "models": {
                "codex": "gpt-test",
                "deepseek": "deepseek-test",
                "zhipu": "glm-5",
            },
            "default": default,
            "configured": {"codex": True, "deepseek": False, "zhipu": False},
        }


class _ProviderMonitor(_Monitor):
    def __init__(self, validator: _ProviderSnapshotValidator) -> None:
        self._relation_validator = validator


class _ProviderRuntime(_ProductionRuntime):
    def __init__(self, store: PredictionArbitrageStore) -> None:
        super().__init__()
        self.store = store
        self.monitor = _ProviderMonitor(_ProviderSnapshotValidator(store))


@contextmanager
def _provider_server(
    runtime: _ProviderRuntime,
) -> Iterator[tuple[str, _ProviderRuntime, list[BaseException]]]:
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, server):
        handler_errors: list[BaseException] = []

        def _record_handler_error(request: object, client_address: object) -> None:
            handler_errors.append(sys.exc_info()[1])  # type: ignore[arg-type]

        server.handle_error = _record_handler_error  # type: ignore[method-assign]
        yield base, runtime, handler_errors


def test_llm_provider_get_reports_selection_and_provider_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPEN_TRADER_PREDICTION_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    runtime = _ProviderRuntime(PredictionArbitrageStore(tmp_path / "data"))
    with _provider_server(runtime) as (base, _runtime, handler_errors):
        status, payload = _response(base + "/api/prediction-arbitrage/llm-provider")

    assert status == 200
    assert payload["schema_version"] == (
        "open_trader.prediction_service.llm_provider.v1"
    )
    assert payload["selected"] == "deepseek"
    assert payload["default"] == "deepseek"
    providers = payload["providers"]
    assert isinstance(providers, list)
    assert [item["provider"] for item in providers] == list(PROVIDER_IDS)
    for item in providers:
        assert set(item) == {
            "provider",
            "model",
            "credentials_configured",
            "usage_24h",
        }
    by_provider = {item["provider"]: item for item in providers}
    assert by_provider["zhipu"]["model"] == "glm-5"
    assert by_provider["codex"]["credentials_configured"] is True
    assert by_provider["deepseek"]["credentials_configured"] is False
    assert by_provider["zhipu"]["usage_24h"] == {}
    assert handler_errors == []


def test_llm_provider_get_reports_fallback_key_and_usage_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPEN_TRADER_PREDICTION_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPEN_TRADER_PREDICTION_LLM_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    store = PredictionArbitrageStore(tmp_path / "data")
    store.record_llm_call(
        status="success",
        usage={"provider": "zhipu", "input_tokens": 7, "output_tokens": 3},
    )
    runtime = _ProviderRuntime(store)
    with _provider_server(runtime) as (base, _runtime, handler_errors):
        status, payload = _response(base + "/api/prediction-arbitrage/llm-provider")

    assert status == 200
    assert "fallback" in payload
    assert payload["fallback"] == ""
    by_provider = {item["provider"]: item for item in payload["providers"]}
    assert by_provider["zhipu"]["usage_24h"] == store.llm_usage_24h_by_provider()["zhipu"]
    assert handler_errors == []


def test_llm_provider_post_switches_engine_without_handler_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env default codex + empty selection table: the first click on zhipu
    # must durably win (no swallowed no-op) and the handler thread must
    # survive the response (no fall-through past the branch).
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_TRADER_PREDICTION_LLM_PROVIDER", "codex")
    db = PredictionArbitrageStore(tmp_path / "data")
    runtime = _ProviderRuntime(db)
    with _provider_server(runtime) as (base, _runtime, handler_errors):
        before_status, before = _response(
            base + "/api/prediction-arbitrage/llm-provider"
        )
        switch_status, switched = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/llm-provider",
                data=b'{"provider":"zhipu"}',
            )
        )
        after_status, after = _response(
            base + "/api/prediction-arbitrage/llm-provider"
        )
        invalid_status, _invalid = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/llm-provider",
                data=b'{"provider":"grok"}',
            )
        )

    assert before_status == after_status == 200
    assert before["selected"] == "codex"
    assert switch_status == 200
    assert switched["selected"] == "zhipu"
    assert after["selected"] == "zhipu"
    assert db.get_llm_provider(default="codex") == "zhipu"
    assert invalid_status == 400
    assert handler_errors == []


def test_llm_provider_post_is_read_only_in_shadow_mode() -> None:
    with _server(_Runtime()) as base:
        status, payload = _response(
            Request(
                base + "/api/prediction-arbitrage/llm-provider",
                data=b'{"provider":"zhipu"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    assert status == 403
    assert payload == prediction_service._READ_ONLY_ERROR


def test_production_reads_and_controls_fail_closed_after_owner_loss() -> None:
    runtime = _ProductionRuntime()
    with _production_server(runtime) as (base, _runtime):
        runtime.production_owner = False
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(
            base + "/api/prediction-arbitrage/state"
        )
        mutation_status, mutation = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )

    assert health_status == state_status == mutation_status == 503
    assert health["production_owner"] is False
    assert state == mutation == {"error": "production runtime is unavailable"}
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    ("state", "owner"),
    (("NOT_READY", True), ("FAILED", True), ("RUNNING", False)),
)
def test_production_server_refuses_to_bind_without_running_owner(
    state: str, owner: bool
) -> None:
    with pytest.raises(RuntimeError, match="not ready"):
        create_prediction_server(
            runtime=_ProductionRuntime(state=state, owner=owner),  # type: ignore[arg-type]
            port=0,
        )


def test_owner_loop_keeps_failed_shadow_listener_for_observability(tmp_path: Path) -> None:
    trigger = tmp_path / "violate"
    stopped = tmp_path / "stopped"
    violation = {"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = f'''\
from pathlib import Path
import open_trader.prediction_service as service

trigger = Path({str(trigger)!r})
stopped = Path({str(stopped)!r})
violation = {{"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}}

class FakeRuntime:
    def __init__(self, **_kwargs):
        self.state = "NEW"
        self.shadow_evidence = {{"mode": "shadow", "first_violation": None, "codex": {{}}}}
    def start(self):
        self.state = "RUNNING"
    def poll_shadow_failure(self):
        if trigger.exists():
            self.shadow_evidence["first_violation"] = violation
            return violation
        return None
    def stop(self):
        self.state = "STOPPED"
        stopped.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=Path({str(tmp_path)!r}),
    prediction_config_path=Path({str(tmp_path / "prediction.json")!r}),
    port={port},
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("shadow service did not bind")
        trigger.write_text("violate", encoding="utf-8")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status, payload = _response(f"http://127.0.0.1:{port}/healthz")
                if status == 503:
                    assert payload["first_violation"] == violation
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("failed Shadow health was not observable")
        status, payload = _response(f"http://127.0.0.1:{port}/api/prediction-arbitrage/state")
        assert status == 503
        assert payload == {"error": "shadow runtime is unavailable"}
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert stopped.read_text(encoding="utf-8") == "stopped"


def test_signal_handler_is_installed_before_runtime_start(tmp_path: Path) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    script = f'''\
from pathlib import Path
import os
import signal
import open_trader.prediction_service as service

started = Path({str(started)!r})
stopped = Path({str(stopped)!r})

class FakeRuntime:
    def __init__(self, **_kwargs):
        self.state = "NEW"
        self.shadow_evidence = {{"mode": "shadow", "first_violation": None, "codex": {{}}}}
    def start(self):
        started.write_text("started", encoding="utf-8")
        os.kill(os.getpid(), signal.SIGTERM)
        self.state = "RUNNING"
    def poll_shadow_failure(self):
        return None
    def stop(self):
        self.state = "STOPPED"
        stopped.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=Path({str(tmp_path)!r}),
    prediction_config_path=Path({str(tmp_path / "prediction.json")!r}),
    port=0,
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert process.wait(timeout=5) == 0
    assert started.read_text(encoding="utf-8") == "started"
    assert stopped.read_text(encoding="utf-8") == "stopped"


@pytest.mark.parametrize("mode", ("shadow", "production"))
def test_sigterm_stops_prediction_runtime_and_releases_its_lock(
    tmp_path: Path, mode: str
) -> None:
    lock_path = tmp_path / "prediction_arbitrage" / "runtime.lock"
    marker = tmp_path / "stopped"
    release_manifest = (
        Path(__file__).resolve().parents[1] / "ops" / "prediction-service-release.json"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = f'''\
from pathlib import Path
from open_trader.prediction_runtime import _RuntimeOwnershipLock
import open_trader.prediction_service as service

data_dir = Path({str(tmp_path)!r})
marker = Path({str(marker)!r})

class FakeRuntime:
    def __init__(self, **kwargs):
        self.state = "NEW"
        self.mode = kwargs["mode"]
        self.production_owner = False
        self.shadow_evidence = {{"mode": self.mode, "first_violation": None, "codex": {{}}}}
        self._lock = _RuntimeOwnershipLock(data_dir / "prediction_arbitrage" / "runtime.lock")
    def start(self):
        self._lock.acquire()
        self.state = "RUNNING"
        self.production_owner = self.mode == "production"
    def poll_shadow_failure(self):
        return None
    def stop(self):
        self.state = "STOPPED"
        self._lock.release()
        marker.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=data_dir,
    prediction_config_path=data_dir / "prediction.json",
    port={port},
    mode={mode!r},
    release_manifest_path=Path({str(release_manifest)!r}),
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("shadow service did not bind")
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert marker.read_text(encoding="utf-8") == "stopped"
    lock = __import__("open_trader.prediction_runtime", fromlist=["_RuntimeOwnershipLock"])._RuntimeOwnershipLock(lock_path)
    lock.acquire()
    lock.release()


@pytest.mark.parametrize(
    ("state", "owner"),
    (("NOT_READY", True), ("FAILED", True), ("RUNNING", False)),
)
def test_production_owner_must_be_ready_before_server_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    owner: bool,
) -> None:
    import open_trader.prediction_service as service

    instances = []

    class FakeRuntime:
        def __init__(self, **_kwargs: object) -> None:
            self.state = "NEW"
            self.production_owner = False
            instances.append(self)

        def start(self) -> None:
            self.state = state
            self.production_owner = owner

        def stop(self) -> None:
            self.state = "STOPPED"

    def unexpected_bind(**_kwargs: object) -> object:
        raise AssertionError("server must not bind")

    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    monkeypatch.setattr(service, "create_prediction_server", unexpected_bind)

    with pytest.raises(RuntimeError, match="not ready"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
            release_manifest_path=Path(__file__).resolve().parents[1]
            / "ops"
            / "prediction-service-release.json",
        )

    assert len(instances) == 1
    assert instances[0].state == "STOPPED"


def test_production_service_requires_release_manifest_before_runtime_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    monkeypatch.setattr(
        service,
        "PredictionRuntime",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runtime constructed")),
    )
    with pytest.raises(ValueError, match="release manifest is required"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
        )


def test_cli_passes_production_config_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.prediction_service as service

    captured: dict[str, object] = {}

    def serve(**kwargs: object) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(service, "serve_prediction_service", serve)

    result = service.main(
        [
            "--mode",
            "production",
            "--data-dir",
            "/tmp/data",
            "--config",
            "/tmp/prediction.json",
            "--release-manifest",
            "/tmp/release.json",
            "--notifier-config",
            "/tmp/daily_premarket.env",
        ]
    )

    assert result == 7
    assert captured["release_manifest_path"] == Path("/tmp/release.json")
    assert captured["notifier_config_path"] == Path("/tmp/daily_premarket.env")


def test_production_service_injects_notifier_from_notifier_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    instances = []
    notifier = object()
    notifier_config = tmp_path / "daily_premarket.env"
    loaded_config = object()
    loaded_paths: list[tuple[Path, bool]] = []

    class FakeRuntime:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.state = "NEW"
            self.production_owner = False
            instances.append(self)

        def start(self) -> None:
            self.state = "RUNNING"
            self.production_owner = True

        def stop(self) -> None:
            self.state = "STOPPED"

    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    def load_config(path: Path, *, dry_run: bool) -> object:
        loaded_paths.append((path, dry_run))
        return loaded_config

    def build_configured_notifier(config: object) -> object:
        assert config is loaded_config
        return notifier

    monkeypatch.setattr(service, "load_env_config", load_config, raising=False)
    monkeypatch.setattr(
        service, "build_notifier", build_configured_notifier, raising=False
    )
    monkeypatch.setattr(
        service, "create_prediction_server",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )

    with pytest.raises(OSError, match="bind failed"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            notifier_config_path=notifier_config,
            port=0,
            mode="production",
            release_manifest_path=Path(__file__).resolve().parents[1]
            / "ops"
            / "prediction-service-release.json",
        )

    assert instances[0].kwargs["notifier"] is notifier
    assert loaded_paths == [(notifier_config, False)]


def test_production_bind_failure_stops_runtime_and_uses_one_metadata_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    metadata = {
        "pid": 123,
        "cwd": "/tmp/accepted",
        "git_sha": "abc123",
        "started_at": "2026-08-11T00:00:00+08:00",
    }
    instances = []

    class FakeRuntime:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.state = "NEW"
            self.production_owner = False
            instances.append(self)

        def start(self) -> None:
            self.state = "RUNNING"
            self.production_owner = True

        def stop(self) -> None:
            self.state = "STOPPED"

    def fail_bind(**kwargs: object) -> object:
        assert kwargs["runtime"] is instances[0]
        assert kwargs["runtime_metadata"] == {
            **metadata,
            "release_schema_version": "open_trader.prediction_service.release.v1",
            "reader_generation": manifest_payload["reader_generation"],
            "contract_generation": manifest_payload["contract_generation"],
        }
        raise OSError("bind failed")

    manifest_payload = {
        "schema_version": "open_trader.prediction_service.release.v1",
        "reader_generation": 7,
        "contract_generation": 11,
    }
    manifest_path = tmp_path / "prediction-service-release.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    monkeypatch.setattr(service, "create_prediction_server", fail_bind)
    monkeypatch.setattr(service, "_runtime_metadata", lambda: metadata)
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    with pytest.raises(OSError, match="bind failed"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
            release_manifest_path=manifest_path,
        )

    assert instances[0].kwargs["git_sha"] == "abc123"
    assert instances[0].kwargs["reader_generation"] == manifest_payload["reader_generation"]
    assert instances[0].state == "STOPPED"
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == previous


class TestPredictionSafeValueConfiguredNotStripped:
    """Regression: the 'configured' key in llm_provider snapshot must survive
    _prediction_safe_value / _prediction_relation_safe_value, unlike the old
    'credentials' key which contained 'credential' and was silently dropped."""

    def test_safe_value_preserves_configured_key(self) -> None:
        payload = {
            "llm_provider": {
                "selected": "codex",
                "models": {"codex": "gpt-5.6-sol", "zhipu": "glm-5"},
                "default": "codex",
                "configured": {"codex": True, "zhipu": False},
            }
        }
        result = _prediction_safe_value(payload)
        assert isinstance(result, dict)
        lp = result["llm_provider"]
        assert isinstance(lp, dict)
        assert "configured" in lp
        assert lp["configured"] == {"codex": True, "zhipu": False}

    def test_relation_safe_value_preserves_configured_key(self) -> None:
        payload = {
            "llm_provider": {
                "selected": "zhipu",
                "models": {"codex": "gpt-5.6-sol", "zhipu": "glm-5"},
                "default": "zhipu",
                "configured": {"codex": True, "zhipu": True},
            }
        }
        result = _prediction_relation_safe_value(payload)
        assert isinstance(result, dict)
        lp = result["llm_provider"]
        assert isinstance(lp, dict)
        assert "configured" in lp
        assert lp["configured"] == {"codex": True, "zhipu": True}


def test_lp_daily_report_is_fixed_at_review_date_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Daily reports retain cutoff attribution and survive a process restart."""

    class Exchange:
        def __init__(self) -> None:
            self.order_creations = 0
            self.posts = 0
            self.cancellations = 0

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_creations += 1
            return {}

        def post_order(self, _signed_order: object) -> object:
            self.posts += 1
            return {}

        def cancel_order(self, _order_id: str) -> object:
            self.cancellations += 1
            return {}

    current_time = [datetime(2026, 9, 15, 0, 3, tzinfo=UTC)]
    store = PredictionArbitrageStore(tmp_path)
    exchanges: list[Exchange] = []
    session_id = "lp-daily-report-session"
    session_payload: dict[str, object] = {
        "market_id": "market-1",
        "condition_id": "condition-1",
        "token_id": "token-1",
        "market_title": "Report cutoff market",
        "outcome": "YES",
        "price": Decimal("0.50"),
        "quantity": Decimal("20"),
        "review_at": datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
        "buy_filled_quantity": Decimal("20"),
        "buy_cost": Decimal("10"),
        "buy_fees": Decimal("0.02"),
        "sold_quantity": Decimal("12"),
        "sold_revenue": Decimal("6.40"),
        "sell_fees": Decimal("0.015"),
        "residual_quantity": Decimal("8"),
        "residual_exit_value": Decimal("3.84"),
        "projected_exit_fee": Decimal("0.015"),
        "account_checked_at": "2026-09-15T00:03:00Z",
        "book_checked_at": "2026-09-15T00:03:00Z",
        "position_reconciled": True,
        "orders_terminal": True,
        "reward_observation": {
            "status": "met",
            "market_amount": Decimal("1.50"),
            "market_asset": "USDC.e",
            "paid": False,
        },
        "trade_events": [
            {
                "trade_id": "buy-1",
                "matched_at": datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
                "status": "CONFIRMED",
                "side": "BUY",
                "quantity": Decimal("20"),
                "price": Decimal("0.50"),
                "fee": Decimal("0.02"),
            },
            {
                "trade_id": "sell-1",
                "matched_at": datetime(2026, 9, 14, 23, 45, tzinfo=UTC),
                "status": "CONFIRMED",
                "side": "SELL",
                "quantity": Decimal("8"),
                "price": Decimal("0.55"),
                "fee": Decimal("0.01"),
            },
            {
                # This fill was observed after the 08:00 report boundary and
                # must be assigned to the next report, even if the first
                # report is generated a few minutes late.
                "trade_id": "sell-2",
                "matched_at": datetime(2026, 9, 15, 0, 1, tzinfo=UTC),
                "status": "CONFIRMED",
                "side": "SELL",
                "quantity": Decimal("4"),
                "price": Decimal("0.50"),
                "fee": Decimal("0.005"),
            },
        ],
        "verified_paid_reward_events": [
            {
                "payment_id": "paid-1",
                "paid_at": datetime(2026, 9, 14, 23, 0, tzinfo=UTC),
                "usd_amount": Decimal("0.20"),
                "verified": True,
            }
        ],
    }
    store.lp_create_session(
        session_id,
        "lp-report-idempotency",
        state="review",
        payload=session_payload,
    )

    def make_runtime(
        target_store: PredictionArbitrageStore,
    ) -> tuple[object, PolymarketLPService]:
        exchange = Exchange()
        exchanges.append(exchange)
        lp = PolymarketLPService(
            target_store,
            exchange,
            clock=lambda: current_time[0],
        )
        execution = PredictionExecutionService(
            store=target_store,
            monitor=_Monitor(),
            trading=exchange,
            notifier=NullNotifier(),
            lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
            lp=lp,
        )
        runtime = SimpleNamespace(
            mode="shadow",
            state="RUNNING",
            shadow_evidence={"mode": "shadow", "first_violation": None},
            store=target_store,
            monitor=_Monitor(),
            execution=execution,
            cross_venue_monitor=None,
        )
        return runtime, lp

    runtime, lp = make_runtime(store)
    generate = getattr(lp, "generate_due_report", None)
    if callable(generate):
        generate()
    with _server(runtime) as base:
        first_status, first = _response(
            base + "/api/prediction-arbitrage/lp/reports/2026-09-15"
        )
    assert first_status == 200
    first_session = first["sessions"][0]
    assert first["report_date"] == "2026-09-15"
    assert first["period_start"] == "2026-09-14T00:00:00Z"
    assert first["period_end"] == "2026-09-15T00:00:00Z"
    assert first["generated_at"] == "2026-09-15T00:03:00Z"
    assert first_session["session_id"] == session_id
    assert Decimal(str(first_session["realized_trade_pnl"])) == Decimal("0.382")
    assert Decimal(str(first_session["paid_rewards"])) == Decimal("0.20")
    assert Decimal(str(first_session["realized_net_pnl"])) == Decimal("0.582")
    assert first_session["residual_quantity_at_period_end"] == "12"
    assert first_session["residual_exit_value_at_period_end"] is None
    assert first_session["residual_exit_estimate_status"] == "unknown"

    # Reopening the same SQLite file cannot create or rewrite the report for
    # the date that was already observed.
    restarted_store = PredictionArbitrageStore(tmp_path)
    restarted_runtime, restarted_lp = make_runtime(restarted_store)
    restarted_generate = getattr(restarted_lp, "generate_due_report", None)
    if callable(restarted_generate):
        restarted_generate()
    with _server(restarted_runtime) as base:
        repeated_status, repeated = _response(
            base + "/api/prediction-arbitrage/lp/reports/2026-09-15"
        )
    assert repeated_status == 200
    assert repeated == first

    # A later fill and verified payment belong to the following 08:00 window;
    # the immutable prior report continues to describe the earlier interval.
    restarted_store.lp_update_session(
        session_id,
        patch={
            "sold_quantity": Decimal("20"),
            "sold_revenue": Decimal("10.56"),
            "sell_fees": Decimal("0.025"),
            "residual_quantity": Decimal("0"),
            "residual_exit_value": Decimal("0"),
            "projected_exit_fee": Decimal("0"),
            "account_checked_at": "2026-09-16T00:03:00Z",
            "book_checked_at": "2026-09-16T00:03:00Z",
            "trade_events": [
                *session_payload["trade_events"],
                {
                    "trade_id": "sell-3",
                    "matched_at": datetime(2026, 9, 15, 22, 0, tzinfo=UTC),
                    "status": "CONFIRMED",
                    "side": "SELL",
                    "quantity": Decimal("8"),
                    "price": Decimal("0.52"),
                    "fee": Decimal("0.01"),
                },
            ],
            "verified_paid_reward_events": [
                *session_payload["verified_paid_reward_events"],
                {
                    "payment_id": "paid-2",
                    "paid_at": datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
                    "usd_amount": Decimal("0.05"),
                    "verified": True,
                },
            ],
        },
    )
    current_time[0] = datetime(2026, 9, 16, 0, 3, tzinfo=UTC)
    next_runtime, next_lp = make_runtime(restarted_store)
    next_generate = getattr(next_lp, "generate_due_report", None)
    if callable(next_generate):
        next_generate()
    with _server(next_runtime) as base:
        next_status, next_report = _response(
            base + "/api/prediction-arbitrage/lp/reports/2026-09-16"
        )
        prior_status, prior_again = _response(
            base + "/api/prediction-arbitrage/lp/reports/2026-09-15"
        )
    assert next_status == prior_status == 200
    next_session = next_report["sessions"][0]
    assert next_report["period_start"] == "2026-09-15T00:00:00Z"
    assert next_report["period_end"] == "2026-09-16T00:00:00Z"
    assert next_report["generated_at"] == "2026-09-16T00:03:00Z"
    assert next_session["session_id"] == session_id
    assert Decimal(str(next_session["realized_trade_pnl"])) == Decimal("0.133")
    assert Decimal(str(next_session["paid_rewards"])) == Decimal("0.05")
    assert Decimal(str(next_session["realized_net_pnl"])) == Decimal("0.183")
    assert next_session["residual_quantity_at_period_end"] == "0"
    assert prior_again == first
    assert all(
        (exchange.order_creations, exchange.posts, exchange.cancellations) == (0, 0, 0)
        for exchange in exchanges
    )


def test_lp_candidate_review_time_is_next_beijing_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidate review deadlines stay fixed through restart and short GTD windows reject."""

    now = [datetime(2026, 9, 15, 1, 0, tzinfo=UTC)]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FrozenDateTime:
            current = now[0]
            return cls.fromtimestamp(
                current.timestamp(), tz=tz if tz is not None else UTC
            )

    monkeypatch.setattr(polymarket_trading_module, "datetime", FrozenDateTime)
    condition_id = "0x" + "c" * 64
    token_id = "0x" + "1" * 64

    class AccountSDK:
        def __init__(self) -> None:
            self.environment = SimpleNamespace(standard_exchange="standard-exchange")
            self.balance_units = 1_000_000_000
            self.limit_orders: list[dict[str, object]] = []
            self.posts: list[dict[str, object]] = []

        def get_balance_allowance(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                balance=self.balance_units,
                allowances={"standard-exchange": self.balance_units},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            return []

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return []

        def create_limit_order(self, **kwargs: object) -> dict[str, object]:
            self.limit_orders.append(dict(kwargs))
            return {**kwargs, "order_type": "GTD"}

        def post_order(self, signed_order: object) -> object:
            assert isinstance(signed_order, dict)
            self.posts.append(signed_order)
            return {**signed_order, "order_id": "lp-review-time-order", "status": "LIVE"}

    class PublicMarketSDK:
        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            if sponsored:
                return []
            return [
                {
                    "condition_id": condition_id,
                    "rewards_min_size": Decimal("20"),
                    "rewards_max_spread": Decimal("10"),
                    "rewards_config": [
                        {
                            "id": "native-config-review-time",
                            "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                            "start_date": "2026-01-01",
                            "end_date": "2026-12-31",
                            "rate_per_day": Decimal("2"),
                        }
                    ],
                }
            ]

        def get_market(self, *, id: str) -> object:
            assert id == "market-1"
            return {
                "id": "market-1",
                "condition_id": condition_id,
                "state": {"accepting_orders": True},
                "outcomes": {
                    "yes": {"label": "Yes", "token_id": token_id},
                    "no": {"label": "No", "token_id": "no-token"},
                },
                "trading": {
                    "minimum_order_size": Decimal("1"),
                    "minimum_tick_size": Decimal("0.01"),
                    "fees_enabled": False,
                },
                "rewards": {
                    "rewards_min_size": Decimal("20"),
                    "rewards_max_spread": Decimal("10"),
                },
            }

        def get_order_book(self, *, token_id: str) -> object:
            assert token_id == "0x" + "1" * 64
            return {
                "market": condition_id,
                "asset_id": token_id,
                "timestamp": FrozenDateTime.now(UTC),
                "bids": [
                    {"price": Decimal("0.51"), "size": Decimal("20")},
                    {"price": Decimal("0.50"), "size": Decimal("100")},
                ],
                "asks": [{"price": Decimal("0.53"), "size": Decimal("100")}],
                "min_order_size": Decimal("1"),
                "tick_size": Decimal("0.01"),
            }

        def close(self) -> None:
            return None

    def make_runtime(
        target_store: PredictionArbitrageStore, sdk: AccountSDK
    ) -> tuple[object, PolymarketLPService]:
        trading = PolymarketTradingClient(
            TradingConfig("signer", "wallet"),
            client=sdk,
            public_client_factory=PublicMarketSDK,
        )
        lp = PolymarketLPService(
            target_store, trading, clock=lambda: now[0]
        )
        execution = PredictionExecutionService(
            store=target_store,
            monitor=_Monitor(),
            trading=trading,
            notifier=NullNotifier(),
            lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
            lp=lp,
        )
        execution._breaker_open = False
        runtime = SimpleNamespace(
            mode="production",
            state="RUNNING",
            production_owner=True,
            store=target_store,
            monitor=_Monitor(),
            execution=execution,
            cross_venue_monitor=None,
        )
        return runtime, lp

    candidate = json.dumps(
        {
            "market_id": "market-1",
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
        }
    ).encode("utf-8")

    def post_candidate(base: str) -> tuple[int, dict[str, object]]:
        return _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/candidates/preview",
                data=candidate,
            )
        )

    def parse_time(value: object) -> datetime:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(UTC)

    store = PredictionArbitrageStore(tmp_path / "next-review")
    sdk = AccountSDK()
    runtime, _lp = make_runtime(store, sdk)
    expected_review = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        preview_status, preview = post_candidate(base)
        assert preview_status == 200
        assert preview["state"] == "previewed"
        assert parse_time(preview["request"]["review_at"]) == expected_review  # type: ignore[index]
        start_status, started = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": preview["preview_id"],
                        "idempotency_key": "lp-review-time-next-day",
                    }
                ).encode("utf-8"),
            )
        )
    assert start_status == 200
    assert started["state"] == "entry_open", started
    assert len(sdk.posts) == len(sdk.limit_orders) == 1
    expiration = int(sdk.limit_orders[0]["expiration"])
    assert datetime.fromtimestamp(expiration, UTC) == expected_review + timedelta(seconds=60)
    saved_session = store.lp_session(str(started["session_id"]))
    assert saved_session is not None
    assert parse_time(saved_session["review_at"]) == expected_review

    # A freshly constructed service after the deadline reads the stored
    # cutoff unchanged; it does not roll the opening into another day.
    now[0] = datetime(2026, 9, 16, 0, 2, tzinfo=UTC)
    restarted_runtime, _restarted_lp = make_runtime(store, AccountSDK())
    with _running_server(
        restarted_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        resumed_status, resumed = _response(
            base + "/api/prediction-arbitrage/lp/sessions/current"
        )
    assert resumed_status == 200
    assert resumed["session_id"] == started["session_id"]
    assert parse_time(resumed["review_at"]) == expected_review

    # At 07:58 Beijing there are only two minutes before the fixed boundary;
    # the existing three-minute SDK minimum must reject the preview.
    now[0] = datetime(2026, 9, 15, 23, 58, tzinfo=UTC)
    short_window_store = PredictionArbitrageStore(tmp_path / "short-window")
    short_window_sdk = AccountSDK()
    short_runtime, _short_lp = make_runtime(short_window_store, short_window_sdk)
    with _running_server(
        short_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        short_status, short_preview = post_candidate(base)
    assert short_status == 200
    assert short_preview["state"] == "rejected"
    assert short_preview["reason"] == "review_at_too_soon"
    assert short_window_sdk.limit_orders == []
    assert short_window_sdk.posts == []
