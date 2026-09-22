from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
import shlex
from pathlib import Path
import signal
import socket
import ssl
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator, Mapping
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import open_trader
import open_trader.prediction_arbitrage_execution as prediction_execution_module
import open_trader.prediction_service as prediction_service
import open_trader.polymarket_trading as polymarket_trading_module
from open_trader.llm_providers import PROVIDER_IDS, resolve_provider
from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    NotificationError,
    NullNotifier,
    XiaoaiVoiceSuppressed,
    XiaoaiSSHNotifier,
    xiaoai_voice_allowed,
)
from open_trader.polymarket_lp import (
    PolymarketLPService,
    _candidate_source_expired,
    _lp_funnel_conditions,
)
from open_trader.polymarket_trading import PredictConfig, PolymarketTradingClient, TradingConfig
from open_trader.predict_source import PredictSource
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_n_leg import fingerprint
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
from tests.test_polymarket_monitor import make_monitor
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


def test_prediction_service_entry_logging_carries_iso_timestamps() -> None:
    import logging
    import re

    from open_trader import prediction_service

    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    root.handlers[:] = []
    try:
        prediction_service._configure_entry_logging()
        stream_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
            and handler.formatter is not None
        ]
        assert stream_handlers
        record = logging.LogRecord(
            "open_trader.prediction_service",
            logging.INFO,
            __file__,
            1,
            "entry probe",
            None,
            None,
        )
        rendered = stream_handlers[-1].formatter.format(record)
        stamp = rendered.split(" ", 1)[0]
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}", stamp
        ), rendered
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)


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
            self.completed = threading.Event()

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
                result: object = [reward]
            elif params.get("sponsored") is False:
                result = {
                    "data": [{**reward, "condition_id": "condition-1"}],
                    "next_cursor": "LTE=",
                }
            else:
                result = {"data": [], "next_cursor": "LTE="}
            if len(self.calls) == 3:
                self.completed.set()
            return result

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

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
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

    # Issue #146: the HTTP route only serves the background snapshot, so the
    # test drives the snapshot pipeline the way the runtime thread does and
    # applies the same route projection. The first payload is built before
    # the reward worker is scheduled (deterministically pre-completion); the
    # second account read fails and degrades to the stale cache.
    first = prediction_service._lp_projection_safe_value(
        service.refresh_lp_dashboard_snapshot()
    )
    assert reward_transport.completed.wait(timeout=2)
    # Issue #146 D3: the dashboard refresh shares the trading client's
    # account TTL cache; expire it so this refresh genuinely re-attempts the
    # external read (which fails) and the stale-degradation path is kept.
    trading._lp_account_shared_cache = None
    stale = prediction_service._lp_projection_safe_value(
        service.refresh_lp_dashboard_snapshot()
    )
    assert first["stale"] is False
    first_order = first["orders"][0]
    assert first_order["management"] == "manual_read_only"
    assert first_order["filled_quantity"] == "5"
    assert first_order["quantity"] == "20"
    assert first_order["market_title"] == "Will it happen?"
    assert first_order["scoring_status"] == "true"
    assert first_order["scoring_last_success_at"] == first_order["scoring_checked_at"]
    assert datetime.fromisoformat(
        str(first_order["scoring_checked_at"]).replace("Z", "+00:00")
    ).tzinfo is not None
    assert "market_amount_raw" not in first_order
    assert first["positions"][0]["size"] == "5"
    assert first["positions"][0]["market_title"] == "Will it happen?"
    assert first["lp_observations"] == {}
    market_reward = first["market_rewards"]["condition-1"]
    assert market_reward["state"] == "unknown"
    assert market_reward["usd_state"] == "unknown"
    assert market_reward["market_amount"] is None
    assert market_reward["stale"] is True
    assert market_reward["market_amount_raw"] is None
    assert market_reward["market_asset"] is None
    assert market_reward["paid"] is False
    assert "account_amount" not in market_reward
    assert market_reward["checked_at"] is None
    assert stale["stale"] is True
    assert stale["checked_at"] == first["checked_at"]
    # Issue #140: the stale cache downgrades scoring to unknown while keeping
    # the first build's checked_at as the last confirmed success time.
    assert stale["orders"] == [
        {
            **first_order,
            "scoring_status": "unknown",
            "scoring_last_success_at": first_order["scoring_checked_at"],
        }
    ]
    assert stale["positions"] == first["positions"]
    assert sdk.open_order_reads == 2
    assert sdk.scoring_reads == ["manual-order"]
    assert len(reward_transport.calls) == 4
    assert reward_transport.calls[0][0] == "/rewards/user/percentages"
    assert sdk.order_writes == 0
    assert sdk.cancellations == 0
    assert public_market.closed is True


def test_lp_dashboard_scoring_status_preserves_official_false(tmp_path: Path) -> None:
    """S1-a: 官方计分以字符串词表输出，false 不得退化为 unknown 或原始布尔。"""

    state: dict[str, object] = {"scoring": False}

    class ScoringTrading:
        config = SimpleNamespace(wallet_address="0x" + "3" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "manual-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                        "market_title": "Scoring market",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            del order_id
            return bool(state["scoring"])

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": market,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=ScoringTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )
    runtime = _Runtime()
    runtime.store = service._store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    with _server(runtime) as base:
        first = service.refresh_lp_dashboard_snapshot()
        status_false, first = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        state["scoring"] = True
        second = service.refresh_lp_dashboard_snapshot()
        status_true, second = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )

    assert status_false == status_true == 200
    false_order = first["orders"][0]
    assert false_order["scoring_status"] == "false"
    assert datetime.fromisoformat(
        str(false_order["scoring_checked_at"]).replace("Z", "+00:00")
    ).tzinfo is not None
    assert false_order["scoring_last_success_at"] == false_order["scoring_checked_at"]
    true_order = second["orders"][0]
    assert true_order["scoring_status"] == "true"
    assert true_order["scoring_last_success_at"] == true_order["scoring_checked_at"]


def test_lp_dashboard_scoring_failure_keeps_last_success(tmp_path: Path) -> None:
    """S1-b: 计分查询失败 → unknown + 本次尝试时间，最后成功时间保留首次成功值。"""

    state: dict[str, object] = {"fail": False}

    class ScoringTrading:
        config = SimpleNamespace(wallet_address="0x" + "3" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "manual-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                        "market_title": "Scoring market",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            del order_id
            if state["fail"] is True:
                raise RuntimeError("scoring read unavailable")
            return True

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": market,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=ScoringTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    first_order = first["orders"][0]
    assert first_order["scoring_status"] == "true"
    first_success_at = first_order["scoring_last_success_at"]
    assert isinstance(first_success_at, str) and first_success_at
    assert first_order["scoring_checked_at"] == first_success_at

    state["fail"] = True
    # Join the worker scheduled by the first refresh so the second refresh
    # owns the pipeline lock and re-reads scoring live.
    reward_worker = service._lp_reward_refresh_thread
    if reward_worker is not None:
        reward_worker.join(timeout=2)
    second = service.refresh_lp_dashboard_snapshot()
    second_order = second["orders"][0]
    assert second_order["scoring_status"] == "unknown"
    assert second_order["scoring_checked_at"] != first_order["scoring_checked_at"]
    assert datetime.fromisoformat(
        str(second_order["scoring_checked_at"]).replace("Z", "+00:00")
    ).tzinfo is not None
    assert second_order["scoring_last_success_at"] == first_success_at


def test_lp_dashboard_stale_cache_downgrades_scoring_unknown(tmp_path: Path) -> None:
    """S1-c: stale 缓存服务把订单计分降级为 unknown，最后成功保留首次查询时间。"""

    state: dict[str, object] = {"fail_account": False}

    class ScoringTrading:
        config = SimpleNamespace(wallet_address="0x" + "3" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            if state["fail_account"] is True:
                raise RuntimeError("account read unavailable")
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "manual-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                        "market_title": "Scoring market",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            del order_id
            return True

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": market,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=ScoringTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    first_order = first["orders"][0]
    assert first_order["scoring_status"] == "true"
    first_checked_at = first_order["scoring_checked_at"]
    assert isinstance(first_checked_at, str) and first_checked_at

    state["fail_account"] = True
    # Issue #146: the page read no longer blocks on the reward worker; join
    # it so the next snapshot refresh owns the pipeline lock.
    worker = service._lp_reward_refresh_thread
    if worker is not None:
        worker.join(timeout=2)
    stale = service.refresh_lp_dashboard_snapshot()
    assert stale["state"] == "stale"
    assert stale["stale"] is True
    stale_order = stale["orders"][0]
    assert stale_order["scoring_status"] == "unknown"
    assert stale_order["scoring_checked_at"] == first_checked_at
    assert stale_order["scoring_last_success_at"] == first_checked_at
    # 缓存本体不得被就地改写。
    assert first["orders"][0]["scoring_status"] == "true"


def test_lp_dashboard_stale_cache_downgrades_today_orders_scoring(
    tmp_path: Path,
) -> None:
    """stale 缓存服务同样降级当天 LP 委托行的计分状态与最后成功时间。"""

    state: dict[str, object] = {"fail_account": False}

    class ScoringTrading:
        config = SimpleNamespace(wallet_address="0x" + "3" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            if state["fail_account"] is True:
                raise RuntimeError("account read unavailable")
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "manual-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                        "market_title": "Scoring market",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            del order_id
            return True

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": market,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=ScoringTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"], "manual order must appear in today table"
    first_row = first["lp_orders_today"][0]
    assert first_row["scoring_status"] == "true"
    first_checked_at = first_row["scoring_checked_at"]
    assert isinstance(first_checked_at, str) and first_checked_at

    state["fail_account"] = True
    # Issue #146: the page read no longer blocks on the reward worker; join
    # it so the next snapshot refresh owns the pipeline lock.
    worker = service._lp_reward_refresh_thread
    if worker is not None:
        worker.join(timeout=2)
    stale = service.refresh_lp_dashboard_snapshot()
    assert stale["state"] == "stale"
    stale_row = stale["lp_orders_today"][0]
    assert stale_row["scoring_status"] == "unknown"
    assert stale_row["scoring_last_success_at"] == first_checked_at
    # 缓存本体不得被就地改写。
    assert first["lp_orders_today"][0]["scoring_status"] == "true"


_LP_REWARD_USDC_ASSET = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def _lp_reward_market_row(
    condition_id: str,
    *,
    earning_percentage: str,
    rate_per_day: str = "1.2",
) -> dict[str, object]:
    """官方 /rewards/user/markets 行；日期窗口动态覆盖真实今天（防日历炸弹）。"""

    return {
        "condition_id": condition_id,
        "earning_percentage": earning_percentage,
        "rewards_config": [
            {
                "id": f"config-{condition_id}",
                "asset_address": _LP_REWARD_USDC_ASSET,
                "start_date": "2020-01-01",
                "end_date": "2030-01-01",
                "rate_per_day": rate_per_day,
            }
        ],
    }


def _lp_reward_order(
    order_id: str,
    *,
    remaining: Decimal,
    matched: Decimal,
    price: Decimal = Decimal("0.50"),
) -> dict[str, object]:
    return {
        "id": order_id,
        "market": "condition-1",
        "asset_id": "yes-token",
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": price,
        "original_size": remaining + matched,
        "size_matched": matched,
        "remaining_size": remaining,
    }


class _LPDashboardFakeLP:
    def candidate_snapshot(self) -> dict[str, object]:
        return {
            "state": "known",
            "complete": False,
            "candidates": [],
            "recommendations": [],
            "market_rewards": {},
        }

    def status(self, _session_id: str | None = None) -> dict[str, object]:
        return {"state": "none"}


def _lp_reward_dashboard_service(
    tmp_path: Path, state: dict[str, object]
) -> PredictionExecutionService:
    """S1-d..i 共用夹具：真实 PolymarketTradingClient 走假 SDK 传输层。"""

    class RewardTransport:
        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            if path == "/rewards/user/percentages":
                return {}
            if path == "/rewards/user/total":
                return []
            if path == "/rewards/user/markets":
                if state.get("market_missing") is True:
                    return {"data": [], "next_cursor": "LTE="}
                if params.get("sponsored") is not True:
                    return {
                        "data": [dict(row) for row in state["reward_markets"]],
                        "next_cursor": "LTE=",
                    }
                return {"data": [], "next_cursor": "LTE="}
            return {"data": [], "next_cursor": "LTE="}

    class AccountSDK:
        def __init__(self) -> None:
            self.scoring_reads: list[str] = []
            self._ctx = SimpleNamespace(
                secure_clob=RewardTransport(), wallet_type=None
            )
            self.environment = SimpleNamespace(
                standard_exchange="standard-exchange"
            )

        def get_balance_allowance(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                balance=30_000_000,
                allowances={"standard-exchange": 30_000_000},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            if state.get("fail_orders") is True:
                raise RuntimeError("account read unavailable")
            return [dict(row) for row in state["orders"]]

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return [dict(row) for row in state["positions"]]

        def get_order_scoring(self, *, order_id: str) -> bool:
            self.scoring_reads.append(order_id)
            behavior = state["scoring"].get(order_id, True)
            if behavior == "raise":
                raise RuntimeError("scoring read unavailable")
            return behavior is True

    class PublicMarketSDK:
        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            return [
                {
                    "id": f"market-{condition}",
                    "condition_id": str(condition),
                    "question": f"Will {condition} happen?",
                    "slug": f"will-{condition}-happen",
                }
                for condition in tuple(condition_ids)  # type: ignore[arg-type]
            ]

        def get_order_books(self, *, token_ids: object) -> list[object]:
            del token_ids
            return []

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool | None = None
        ) -> tuple[dict[str, object], ...]:
            # Mirror the authenticated row's pool once per condition: the
            # combined (sponsored=true) read equals the base (native) read,
            # so the deduped hourly figure keeps the single-pool value.
            configs = []
            for row in state.get("reward_markets", ()):  # type: ignore[union-attr]
                if row.get("condition_id") != condition_id:
                    continue
                for config in row.get("rewards_config", ()):
                    configs.append(
                        {
                            "id": (
                                "pool-"
                                + ("combined-" if sponsored else "native-")
                                + str(config.get("id"))
                            ),
                            "asset_address": config.get("asset_address"),
                            "start_date": config.get("start_date"),
                            "end_date": config.get("end_date"),
                            "rate_per_day": config.get("rate_per_day"),
                        }
                    )
            if not configs:
                return ()
            return ({"condition_id": condition_id, "rewards_config": configs},)

        def close(self) -> None:
            pass

    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        client=AccountSDK(),
        public_client_factory=lambda: PublicMarketSDK(),
    )
    return PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=_LPDashboardFakeLP(),
    )


def _lp_reward_dashboard_state(
    *, earning_percentage: str
) -> dict[str, object]:
    return {
        "orders": [
            _lp_reward_order(
                "manual-order", remaining=Decimal("40"), matched=Decimal("0")
            )
        ],
        "positions": [],
        "reward_markets": [
            _lp_reward_market_row(
                "condition-1", earning_percentage=earning_percentage
            )
        ],
        "scoring": {},
        "market_missing": False,
    }


def test_lp_dashboard_explicit_zero_reward_keeps_zero_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-d: earning_percentage 0 → 显式零奖励；占资 $20，率与小时奖励均为 0 而非 None。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="0")
    service = _lp_reward_dashboard_service(tmp_path, state)
    runtime = _Runtime()
    runtime.store = service._store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    refreshed = service.refresh_lp_observations()
    assert refreshed["state"] == "ready"
    # Issue #146: the route serves the background snapshot; run one
    # more snapshot pass so the served payload carries the stored
    # observations.
    service.refresh_lp_dashboard_snapshot()
    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")

    assert status == 200
    observation = payload["lp_observations"]["condition-1"]
    assert observation["current_hourly_reward_usd"] == "0"
    assert observation["current_yield_pct_per_hour"] == "0"
    assert observation["state"] == "known"


def test_lp_dashboard_pool_read_failure_degrades_to_unknown_by_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam 5 (#138 round 2): the deduped rates read the official pools via
    the public selected catalog; a failed pool read degrades only that
    market's observation to UNKNOWN with the reason carried through — the
    authenticated payload's overlapping rate can never stand in for it."""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="100")
    service = _lp_reward_dashboard_service(tmp_path, state)
    # Break only the public pool reader; the authenticated payload stays
    # healthy, so without dedup the old sum would still answer $0.05/h.
    original_factory = service._trading._public_client_factory

    real_public = original_factory()

    class FailingPools:
        # Only the selected reward pool reader fails; every other public
        # read (metadata, books, ...) still delegates to the real fake SDK.
        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool | None = None
        ):
            raise RuntimeError("selected reward pool unavailable")

        def __getattr__(self, name: str):
            return getattr(real_public, name)

    def failing_factory():
        return FailingPools()

    service._trading._public_client_factory = failing_factory

    refreshed = service.refresh_lp_observations()
    observation = refreshed["observations"]["condition-1"]
    assert observation["state"] == "unknown"
    assert observation["reason"] == "reward_read_failed"
    assert observation["current_hourly_reward_usd"] is None
    assert observation["current_yield_pct_per_hour"] is None

    service._trading._public_client_factory = original_factory
    recovered = service.refresh_lp_observations()
    recovered_observation = recovered["observations"]["condition-1"]
    assert recovered_observation["state"] == "known"
    assert Decimal(str(recovered_observation["current_hourly_reward_usd"])) == Decimal("0.05")


def test_lp_dashboard_missing_reward_market_stays_unknown_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-e: 奖励读取成功但 markets 缺该市场 → reward_market_missing，不按 0 计。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="100")
    state["market_missing"] = True
    service = _lp_reward_dashboard_service(tmp_path, state)

    refreshed = service.refresh_lp_observations()
    assert refreshed["state"] == "ready"
    observation = refreshed["observations"]["condition-1"]
    assert observation["reason"] == "reward_market_missing"
    assert observation["current_yield_pct_per_hour"] is None
    assert observation["current_hourly_reward_usd"] is None


def test_lp_dashboard_zero_capital_keeps_rate_unknown_and_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-f: 全成交无占资 → 率为 None 不按 0 计，占资 0，stage flat。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="100")
    state["orders"] = [
        _lp_reward_order(
            "manual-order", remaining=Decimal("0"), matched=Decimal("40")
        )
    ]
    service = _lp_reward_dashboard_service(tmp_path, state)

    refreshed = service.refresh_lp_observations()
    assert refreshed["state"] == "ready"
    observation = refreshed["observations"]["condition-1"]
    assert observation["current_yield_pct_per_hour"] is None
    assert observation["occupied_capital_usd"] == "0"
    assert observation["stage"] == "flat"


def test_lp_dashboard_yield_worked_example_hourly_and_capital(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-g: 1.2×100/100/24=0.05/小时；剩余 40 × 0.50=$20 → 0.05/20×100=0.25%／小时。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="100")
    service = _lp_reward_dashboard_service(tmp_path, state)
    runtime = _Runtime()
    runtime.store = service._store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    refreshed = service.refresh_lp_observations()
    assert refreshed["state"] == "ready"
    # Issue #146: the route serves the background snapshot; run one
    # more snapshot pass so the served payload carries the stored
    # observations.
    service.refresh_lp_dashboard_snapshot()
    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")

    assert status == 200
    observation = payload["lp_observations"]["condition-1"]
    assert observation["current_hourly_reward_usd"] == "0.05"
    assert observation["occupied_capital_usd"] == "20.00"
    assert observation["current_yield_pct_per_hour"] == "0.25"


def test_lp_dashboard_dual_orders_share_one_market_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-h: 同市场双订单奖励单份、分母合计；连续构建不累加；观察仅一条。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="100")
    state["orders"] = [
        _lp_reward_order(
            "manual-a", remaining=Decimal("20"), matched=Decimal("0")
        ),
        _lp_reward_order(
            "manual-b", remaining=Decimal("20"), matched=Decimal("0")
        ),
    ]
    service = _lp_reward_dashboard_service(tmp_path, state)
    runtime = _Runtime()
    runtime.store = service._store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    first_refresh = service.refresh_lp_observations()
    assert first_refresh["state"] == "ready"
    # Issue #146: the route serves the background snapshot; run one more
    # snapshot pass so the served payload carries the stored observations.
    service.refresh_lp_dashboard_snapshot()
    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200
        observation = payload["lp_observations"]["condition-1"]
        assert observation["current_hourly_reward_usd"] == "0.05"
        assert observation["current_yield_pct_per_hour"] == "0.25"
        assert sorted(
            str(row["order_id"]) for row in payload["lp_orders_today"]
        ) == ["manual-a", "manual-b"]
        assert list(payload["lp_observations"]) == ["condition-1"]

        service.refresh_lp_observations()
        status_second, payload_second = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )

    assert status_second == 200
    second_observation = payload_second["lp_observations"]["condition-1"]
    assert second_observation["current_hourly_reward_usd"] == "0.05"
    assert second_observation["current_yield_pct_per_hour"] == "0.25"


def test_lp_dashboard_qualification_three_states_with_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-i: qualified 三态与推导依据——orders_scoring / orders_not_scoring / 未知。"""

    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )
    state = _lp_reward_dashboard_state(earning_percentage="0")
    service = _lp_reward_dashboard_service(tmp_path, state)

    def _expire_shared_account_cache() -> None:
        # Issue #146 D3: observation passes share the trading client's
        # account TTL cache; expire it so each pass genuinely re-reads the
        # mutated account facts instead of the 10-second cached snapshot.
        trading = service._trading
        assert isinstance(trading, PolymarketTradingClient)
        trading._lp_account_shared_cache = None

    # (1) 全部买单计分 false（份额非正）→ 明确未确认。
    state["scoring"] = {"manual-order": False}
    first = service.refresh_lp_observations()
    assert first["state"] == "ready"
    first_observation = first["observations"]["condition-1"]
    assert first_observation["qualified"] is False
    assert first_observation["qualification_basis"] == "orders_not_scoring"

    # (2) 一 false 一查询失败 → 混合未知。
    state["orders"] = [
        _lp_reward_order("manual-a", remaining=Decimal("20"), matched=Decimal("0")),
        _lp_reward_order("manual-b", remaining=Decimal("20"), matched=Decimal("0")),
    ]
    state["scoring"] = {"manual-a": False, "manual-b": "raise"}
    _expire_shared_account_cache()
    second = service.refresh_lp_observations()
    assert second["state"] == "ready"
    second_observation = second["observations"]["condition-1"]
    assert second_observation["qualified"] is None
    assert second_observation["qualification_basis"] is None

    # (3) 全部 true → 明确已确认。
    state["scoring"] = {}
    _expire_shared_account_cache()
    third = service.refresh_lp_observations()
    assert third["state"] == "ready"
    third_observation = third["observations"]["condition-1"]
    assert third_observation["qualified"] is True
    assert third_observation["qualification_basis"] == "orders_scoring"


def test_lp_account_trades_reads_markets_and_flags_incomplete_reads() -> None:
    """The LP trades adapter reads per market, normalizes rows, flags gaps."""

    calls: list[dict[str, object]] = []

    class TradesSDK:
        def list_account_trades(self, **kwargs: object) -> list[object]:
            calls.append(dict(kwargs))
            market = str(kwargs.get("market") or "")
            if market == "condition-a":
                return [
                    {
                        "id": "trade-1",
                        "condition_id": "condition-a",
                        "asset_id": "yes-token",
                        "taker_order_id": "",
                        "side": "BUY",
                        "trader_side": "MAKER",
                        "price": "0.44",
                        "size": "30",
                        "status": "TRADE_STATUS_MATCHED",
                        "matched_at": "2026-09-16T02:00:00Z",
                        "maker_orders": [
                            {
                                "order_id": "maker-1",
                                "asset_id": "yes-token",
                                "maker_address": "wallet",
                                "owner": "wallet",
                                "side": "BUY",
                                "price": "0.44",
                                "matched_amount": "30",
                            }
                        ],
                    },
                    # Unparsable row must flag the read as incomplete.
                    {"id": "bad-trade"},
                ]
            if market == "condition-b":
                raise RuntimeError("trades unavailable")
            return []

    client = PolymarketTradingClient(
        TradingConfig("signer", "wallet"), client=TradesSDK()
    )
    result = client.lp_account_trades(("condition-a", "condition-b", "  "))

    # One call per requested market; blank ids are skipped.
    assert calls == [{"market": "condition-a"}, {"market": "condition-b"}]
    assert result["state"] == "unknown"
    assert result["complete"] is False
    checked_at = result["checked_at"]
    assert isinstance(checked_at, datetime) and checked_at.tzinfo is not None
    trades = result["trades"]
    # The failed market is absent; the readable market keeps its parsed rows.
    assert set(trades) == {"condition-a"}
    normalized = trades["condition-a"][0]
    assert normalized["trade_id"] == "trade-1"
    assert normalized["condition_id"] == "condition-a"
    assert normalized["token_id"] == "yes-token"
    assert normalized["trader_side"] == "MAKER"
    assert normalized["status"] == "MATCHED"
    assert normalized["price"] == Decimal("0.44")
    assert normalized["size"] == Decimal("30")
    assert normalized["maker_orders"][0]["order_id"] == "maker-1"
    assert normalized["maker_orders"][0]["matched_amount"] == Decimal("30")
    assert datetime.fromisoformat(
        str(normalized["matched_at"]).replace("Z", "+00:00")
    ) == datetime(2026, 9, 16, 2, 0, tzinfo=UTC)

    empty = client.lp_account_trades(())
    assert empty["state"] == "unknown"
    assert empty["complete"] is False
    assert empty["trades"] == {}
    assert calls == [{"market": "condition-a"}, {"market": "condition-b"}]


def test_lp_today_orders_trades_keep_only_self_maker_orders() -> None:
    """R1: 一笔 taker 扫单吃我方 10 份与两个外来 maker（40/50 份）时，
    适配器只保留我方钱包的 maker 行；归属不明的行也不得计入我方成交。"""

    class SweepSDK:
        def list_account_trades(self, **kwargs: object) -> list[object]:
            assert kwargs.get("market") == "condition-sweep"
            return [
                {
                    "id": "sweep-1",
                    "condition_id": "condition-sweep",
                    "asset_id": "yes-token",
                    "taker_order_id": "counterparty-taker-order",
                    "side": "SELL",
                    "trader_side": "MAKER",
                    "price": "0.50",
                    "size": "107",
                    "status": "CONFIRMED",
                    "matched_at": "2026-09-16T02:00:00Z",
                    "maker_orders": [
                        {
                            "order_id": "maker-foreign-a",
                            "asset_id": "yes-token",
                            "maker_address": "0x" + "a" * 40,
                            "owner": "0x" + "a" * 40,
                            "side": "BUY",
                            "price": "0.50",
                            "matched_amount": "40",
                        },
                        {
                            "order_id": "maker-ours",
                            "asset_id": "yes-token",
                            "maker_address": "WALLET",
                            "owner": "0x" + "b" * 40,
                            "side": "BUY",
                            "price": "0.50",
                            "matched_amount": "10",
                        },
                        {
                            "order_id": "maker-foreign-b",
                            "asset_id": "yes-token",
                            "maker_address": "0x" + "c" * 40,
                            "owner": "0x" + "c" * 40,
                            "side": "BUY",
                            "price": "0.50",
                            "matched_amount": "50",
                        },
                        {
                            "order_id": "maker-unknown-attribution",
                            "asset_id": "yes-token",
                            "side": "BUY",
                            "price": "0.50",
                            "matched_amount": "7",
                        },
                    ],
                }
            ]

    client = PolymarketTradingClient(
        TradingConfig("signer", "wallet"), client=SweepSDK()
    )
    result = client.lp_account_trades(("condition-sweep",))

    assert result["complete"] is True
    (trade,) = result["trades"]["condition-sweep"]
    assert [str(maker["order_id"]) for maker in trade["maker_orders"]] == [
        "maker-ours"
    ]
    assert str(trade["maker_orders"][0]["maker_address"]) == "WALLET"
    assert trade["maker_orders"][0]["matched_amount"] == Decimal("10")


def test_lp_dashboard_http_projection_keeps_today_orders(tmp_path: Path) -> None:
    """AC7: lp_orders_today 与 non_lp_row_count 经投影后保留，Decimal 序列化为字符串。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "scoring-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("80"),
                        "size_matched": Decimal("40"),
                        "remaining_size": Decimal("40"),
                        "market_title": "Projected LP market",
                        "market_url": "https://polymarket.com/event/projected",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    # Issue #146: the route serves the background snapshot, so run the
    # snapshot pipeline (as the runtime thread would) before serving.
    service.refresh_lp_dashboard_snapshot()
    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")

    assert status == 200
    today = payload["lp_orders_today"]
    assert [str(row["order_id"]) for row in today] == ["scoring-order"]
    row = today[0]
    assert row["quantity"] == "80"
    assert row["filled_quantity"] == "40"
    assert row["remaining_quantity"] == "40"
    assert row["price"] == "0.50"
    assert row["state"] == "open"
    assert row["scoring_status"] == "true"
    assert payload["non_lp_row_count"] == 0


def test_lp_dashboard_http_rows_include_session_id(tmp_path: Path) -> None:
    """Issue 165: lp_orders_today 行的 session_id 键经 HTTP 投影透传。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "scoring-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("80"),
                        "size_matched": Decimal("40"),
                        "remaining_size": Decimal("40"),
                        "market_title": "Projected LP market",
                        "market_url": "https://polymarket.com/event/projected",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {
                "state": "entry_open",
                "session_id": "lp-http-session",
                "condition_id": "condition-1",
                "token_id": "yes-token",
                "entry_order_id": "scoring-order",
                "owned_order_ids": ["scoring-order"],
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = object()
    runtime.execution = service

    service.refresh_lp_dashboard_snapshot()
    with _server(runtime) as base:
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")

    assert status == 200
    today = payload["lp_orders_today"]
    assert [str(row["order_id"]) for row in today] == ["scoring-order"]
    assert all("session_id" in row for row in today)
    assert today[0]["session_id"] == "lp-http-session"

def test_lp_dashboard_account_outage_keeps_newer_public_funnel(tmp_path: Path) -> None:
    first_now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    now = [first_now]
    account_failure = [False]

    class Exchange:
        config = SimpleNamespace(wallet_address="wallet")

        def __init__(self) -> None:
            self.catalog_reads = 0
            self.metadata_reads = 0
            self.book_reads = 0
            self.history_reads = 0
            self.account_reads = 0

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is None:
                self.catalog_reads += 1
                market_specs = (
                    (("A", Decimal("100")),)
                    if self.catalog_reads == 1
                    else (("B", Decimal("90")), ("C", Decimal("80")))
                )
            else:
                selected = {
                    condition_id.removeprefix("condition-")
                    for condition_id in condition_ids
                }
                market_specs = tuple(
                    (market_id, daily_pool)
                    for market_id, daily_pool in (
                        ("A", Decimal("100")),
                        ("B", Decimal("90")),
                        ("C", Decimal("80")),
                    )
                    if market_id in selected
                )
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": tuple(
                    {
                        "condition_id": f"condition-{market_id}",
                        "daily_pool_usd": daily_pool,
                        "reward_active": True,
                    }
                    for market_id, daily_pool in market_specs
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.metadata_reads += 1
            metadata: dict[str, dict[str, object]] = {}
            for market_id in ("A", "B", "C"):
                condition_id = f"condition-{market_id}"
                if condition_id not in condition_ids:
                    continue
                metadata[condition_id] = {
                    "market_id": f"market-{market_id}",
                    "condition_id": condition_id,
                    "market_title": f"Market {market_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "fees_checked_at": now[0],
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{market_id}",
                        }
                    },
                }
            return metadata

        def lp_account_snapshot(self) -> dict[str, object]:
            self.account_reads += 1
            if account_failure[0]:
                raise RuntimeError("account unavailable")
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [
                    {
                        "order_id": "warm-lp-order",
                        "condition_id": "condition-WARM",
                        "token_id": "token-WARM",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("0"),
                        "remaining_size": Decimal("20"),
                    }
                ],
                "positions": [],
                "checked_at": now[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            assert order_id == "warm-lp-order"
            return True

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.book_reads += 1
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": now[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.history_reads += 1
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

    def save_history(store: PredictionArbitrageStore, market_id: str, at: datetime) -> None:
        store.lp_save_price_history(
            f"condition-{market_id}",
            f"token-{market_id}",
            [
                {"t": int((at - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
                {"t": int(at.timestamp()), "p": Decimal("0.505")},
            ],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "window_start": at - timedelta(hours=24),
                "window_end": at,
                "sample_count": 2,
                "checked_at": at,
                "valid_until": at + timedelta(hours=2),
            },
        )

    store = PredictionArbitrageStore(tmp_path)
    save_history(store, "A", first_now)
    save_history(store, "B", first_now + timedelta(seconds=10))
    save_history(store, "C", first_now + timedelta(seconds=10))
    exchange = Exchange()
    lp = PolymarketLPService(store, exchange, clock=lambda: now[0])
    first_prepared = lp.refresh_price_history()
    assert first_prepared["state"] == "known"
    first_scan = lp.refresh_candidates(force=True)
    assert first_scan["state"] == "ready"
    assert first_scan["complete"] is True
    assert first_scan["selected_market_ids"] == ["market-A"]
    assert first_scan["checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert first_scan["funnel"]["read"] == 1
    assert first_scan["funnel"]["base"] == 1
    assert first_scan["funnel"]["sort"] == 1
    assert first_scan["funnel"]["trial"] == 1
    assert "risk" not in first_scan["funnel"]
    assert [row["market_id"] for row in first_scan["candidates"]] == ["market-A"]
    # Issue #143: the sole passer is the merged rank one and is the current
    # recommendation.
    assert [row["market_id"] for row in first_scan["recommendations"]] == [
        "market-A"
    ]
    assert exchange.catalog_reads == 1
    assert exchange.metadata_reads == 1
    assert exchange.book_reads == 1
    assert exchange.history_reads == 0
    assert exchange.account_reads == 1

    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=exchange,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )
    counters_before_first_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    # Issue #146: pipeline runs now happen only in the snapshot refresh.
    first_dashboard = execution.refresh_lp_dashboard_snapshot()
    assert exchange.catalog_reads == counters_before_first_dashboard["catalog"]
    assert exchange.metadata_reads == counters_before_first_dashboard["metadata"]
    assert exchange.book_reads == counters_before_first_dashboard["books"]
    assert exchange.history_reads == counters_before_first_dashboard["history"]
    assert exchange.account_reads == counters_before_first_dashboard["account"] + 1
    assert first_dashboard["state"] == "ready"
    assert first_dashboard["candidate_state"] == "ready"
    assert first_dashboard["complete"] is True
    assert first_dashboard["candidate_stale"] is False
    assert first_dashboard["candidate_checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert first_dashboard["funnel"]["read"] == 1
    assert first_dashboard["funnel"]["base"] == 1
    assert first_dashboard["funnel"]["sort"] == 1
    assert first_dashboard["funnel"]["trial"] == 1
    assert first_dashboard["selected_market_ids"] == ["market-A"]
    assert first_dashboard["checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert [row["order_id"] for row in first_dashboard["lp_orders_today"]] == [
        "warm-lp-order"
    ]
    assert first_dashboard["non_lp_row_count"] == 0
    assert [row["market_id"] for row in first_dashboard["candidates"]] == ["market-A"]
    assert [row["market_id"] for row in first_dashboard["recommendations"]] == [
        "market-A"
    ]
    first_account_checked_at = "2026-09-17T01:00:00.000000Z"

    # Issue #143 decision 5 + #157: once the cached account receipt ages
    # past the shared-fact window the account gate ends the batch before
    # any book read — zero consumed markets — and the still-valid pool row
    # stays published.
    now[0] = first_now + timedelta(seconds=65)
    account_failure[0] = True
    second_prepared = lp.refresh_price_history()
    assert second_prepared["state"] == "known"
    second_scan = lp.refresh_candidates(force=True)
    assert second_scan["state"] == "ready"
    # The catalog itself stays complete; only the account read failed.
    assert second_scan["complete"] is True
    assert second_scan["selected_market_ids"] == ["market-A"]
    assert second_scan["checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert second_scan["funnel"]["stop_reason"] == "account_unavailable"
    assert second_scan["funnel"]["checked"] == 1
    assert second_scan["funnel"]["passed"] == 1
    assert second_scan["funnel"]["batches"] == 1
    assert [row["market_id"] for row in second_scan["recommendations"]] == [
        "market-A"
    ]
    assert [row["market_id"] for row in second_scan["candidates"]] == ["market-A"]
    assert exchange.catalog_reads == 2
    assert exchange.metadata_reads == 2
    # Issue #157: the account gate consumed no book read; the extra account
    # read is the gate's targeted re-check (4 = queue build + first-scan
    # renewal + gate re-check + dashboard refresh).
    assert exchange.book_reads == 1
    assert exchange.history_reads == 0
    assert exchange.account_reads == 4

    counters_before_stale_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    stale_dashboard = execution.refresh_lp_dashboard_snapshot()
    assert exchange.catalog_reads == counters_before_stale_dashboard["catalog"]
    assert exchange.metadata_reads == counters_before_stale_dashboard["metadata"]
    assert exchange.book_reads == counters_before_stale_dashboard["books"]
    assert exchange.history_reads == counters_before_stale_dashboard["history"]
    assert exchange.account_reads == counters_before_stale_dashboard["account"] + 1
    assert stale_dashboard["state"] == "stale"
    assert stale_dashboard["stale"] is True
    assert stale_dashboard["checked_at"] == first_account_checked_at
    assert stale_dashboard["open_orders_complete"] is True
    assert stale_dashboard["positions_complete"] is True
    # Issue #157: the account outage degrades the dashboard's account
    # snapshot (state/stale above) but NOT the candidate pool — the stored
    # row is still valid, so candidate_state stays ready.
    assert stale_dashboard["candidate_state"] == "ready"
    # The catalog stays complete and the pool row is valid, so the
    # candidate-side stale flags stay False under the rolling contract.
    assert stale_dashboard["complete"] is True
    assert stale_dashboard["candidate_stale"] is False
    assert stale_dashboard["candidate_checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert stale_dashboard["selected_market_ids"] == ["market-A"]
    assert stale_dashboard["funnel"]["stop_reason"] == "account_unavailable"
    assert stale_dashboard["funnel"]["read"] >= 1
    assert stale_dashboard["funnel"]["base"] >= 1
    assert stale_dashboard["funnel"]["sort"] >= 1
    # Issue #157: the still-valid pool row stays the current recommendation
    # through the account outage (the outage degrades the account snapshot,
    # not the candidate pool).
    assert stale_dashboard["funnel"]["trial"] == 1
    assert [row["market_id"] for row in stale_dashboard["recommendations"]] == [
        "market-A",
    ]
    assert [row["market_id"] for row in stale_dashboard["candidates"]] == [
        "market-A",
    ]
    assert [row["order_id"] for row in stale_dashboard["lp_orders_today"]] == [
        "warm-lp-order"
    ]
    assert stale_dashboard["non_lp_row_count"] == 0
    assert [row["daily_pool_usd"] for row in stale_dashboard["candidates"]] == [
        "100",
    ]

    counters_before_repeated_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    repeated_stale_dashboard = execution.refresh_lp_dashboard_snapshot()
    assert exchange.catalog_reads == counters_before_repeated_dashboard["catalog"]
    assert exchange.metadata_reads == counters_before_repeated_dashboard["metadata"]
    assert exchange.book_reads == counters_before_repeated_dashboard["books"]
    assert exchange.history_reads == counters_before_repeated_dashboard["history"]
    assert exchange.account_reads == counters_before_repeated_dashboard["account"] + 1
    assert repeated_stale_dashboard["state"] == "stale"
    assert repeated_stale_dashboard["stale"] is True
    assert repeated_stale_dashboard["checked_at"] == first_account_checked_at
    # Issue #157: the repeated account outage also leaves the candidate pool
    # untouched (candidate_state stays ready; the pool row is still valid).
    assert repeated_stale_dashboard["candidate_state"] == "ready"
    # Issue #157: the candidate-side flags follow the pool — complete
    # (catalog) stays true, candidate_stale stays false, and the still-valid
    # row keeps the recommendation.
    assert repeated_stale_dashboard["complete"] is True
    assert repeated_stale_dashboard["candidate_stale"] is False
    assert repeated_stale_dashboard["candidate_checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert repeated_stale_dashboard["selected_market_ids"] == ["market-A"]
    assert repeated_stale_dashboard["funnel"]["stop_reason"] == "account_unavailable"
    assert [
        row["market_id"] for row in repeated_stale_dashboard["recommendations"]
    ] == ["market-A"]
    assert [
        row["market_id"] for row in repeated_stale_dashboard["candidates"]
    ] == ["market-A"]
    assert [
        row["order_id"] for row in repeated_stale_dashboard["lp_orders_today"]
    ] == ["warm-lp-order"]
    assert repeated_stale_dashboard["non_lp_row_count"] == 0

    cold_execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=exchange,
        notifier=NullNotifier(),
        lock_path=tmp_path / "cold-execution.lock",
        lp=lp,
    )
    counters_before_cold_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    cold_dashboard = cold_execution.refresh_lp_dashboard_snapshot()
    assert exchange.catalog_reads == counters_before_cold_dashboard["catalog"]
    assert exchange.metadata_reads == counters_before_cold_dashboard["metadata"]
    assert exchange.book_reads == counters_before_cold_dashboard["books"]
    assert exchange.history_reads == counters_before_cold_dashboard["history"]
    assert exchange.account_reads == counters_before_cold_dashboard["account"] + 1
    assert cold_dashboard["state"] == "unknown"
    assert cold_dashboard["stale"] is True
    assert cold_dashboard["authenticated"] is False
    assert cold_dashboard["orders"] == []
    assert cold_dashboard["positions"] == []
    assert cold_dashboard["lp_orders_today"] == []
    assert cold_dashboard["non_lp_row_count"] is None
    assert cold_dashboard["checked_at"] is None
    assert cold_dashboard["open_orders_complete"] is False
    assert cold_dashboard["positions_complete"] is False
    # Issue #157: the pool row stays valid on a cold dashboard too — the
    # candidate side is neither stale nor degraded.
    assert cold_dashboard["candidate_state"] == "ready"
    assert cold_dashboard["complete"] is True
    assert cold_dashboard["candidate_stale"] is False
    assert cold_dashboard["candidate_checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert cold_dashboard["selected_market_ids"] == ["market-A"]
    assert [row["market_id"] for row in cold_dashboard["recommendations"]] == [
        "market-A",
    ]
    assert [row["market_id"] for row in cold_dashboard["candidates"]] == [
        "market-A",
    ]


def test_lp_dashboard_normalizes_candidate_reward_without_freshness_proof(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Account:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "manual-order",
                        "condition_id": "condition-1",
                        "asset_id": "yes-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "manual-order"
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            entered.set()
            assert release.wait(timeout=5)
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "ready",
                "complete": True,
                "candidates": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.80"),
                    }
                },
            }

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )

    try:
        # Issue #146: the pipeline only runs inside the snapshot refresh.
        dashboard = service.refresh_lp_dashboard_snapshot()
        reward = dashboard["market_rewards"]["condition-1"]
        assert reward["stale"] is True
        assert reward["reason"] == "reward_candidate_stale"
        assert reward["market_amount"] == Decimal("0.80")
        assert entered.wait(timeout=2)
    finally:
        release.set()


def test_lp_dashboard_does_not_wait_for_market_reward_refresh(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Account:
        order_writes = 0
        cancellations = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "manual-order",
                        "condition_id": "condition-1",
                        "asset_id": "yes-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "manual-order"
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            entered.set()
            assert release.wait(timeout=5)
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "ready",
                "complete": True,
                "candidates": [{"condition_id": "condition-1"}],
                "recommendations": [],
            }

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    account = Account()
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = service

    try:
        # Issue #146: the snapshot refresh schedules the reward worker and
        # returns unknown rewards without waiting for it.
        with ThreadPoolExecutor(max_workers=1) as clients:
            future = clients.submit(service.refresh_lp_dashboard_snapshot)
            assert entered.wait(timeout=2)
            payload = future.result(timeout=5)
    finally:
        release.set()

    assert payload["orders"][0]["condition_id"] == "condition-1"
    reward = payload["market_rewards"]["condition-1"]
    assert reward["state"] == "unknown"
    assert reward["stale"] is True
    assert account.order_writes == 0
    assert account.cancellations == 0


def test_lp_dashboard_coalesces_reward_refresh_and_publishes_completed_cache(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class Account:
        order_writes = 0
        cancellations = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "manual-order",
                        "condition_id": "condition-1",
                        "asset_id": "yes-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "manual-order"
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    account = Account()
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = service

    # Issue #146: concurrent snapshot refreshes coalesce; the second caller
    # receives the current cache instead of starting a second pipeline.
    try:
        with ThreadPoolExecutor(max_workers=2) as clients:
            first_future = clients.submit(service.refresh_lp_dashboard_snapshot)
            assert entered.wait(timeout=2)
            second_future = clients.submit(service.refresh_lp_dashboard_snapshot)
            first = first_future.result(timeout=2)
            second = second_future.result(timeout=2)
        assert first["market_rewards"]["condition-1"]["state"] == "unknown"
        assert second["market_rewards"]["condition-1"]["state"] == "unknown"
        release.set()

        updated: dict[str, object] | None = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            candidate = service.lp_dashboard()
            if candidate["market_rewards"]["condition-1"]["state"] == "known":
                updated = prediction_service._lp_projection_safe_value(candidate)
                break
            time.sleep(0.01)
    finally:
        release.set()

    assert updated is not None
    reward = updated["market_rewards"]["condition-1"]
    assert reward["market_amount"] == "0.80"
    assert isinstance(reward["checked_at"], str)
    assert calls == 1
    assert account.order_writes == 0
    assert account.cancellations == 0


def test_lp_dashboard_expires_reward_cache_without_waiting_for_refresh(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    clock = [0.0]
    calls = 0

    class Account:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "manual-order",
                        "condition_id": "condition-1",
                        "asset_id": "yes-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "manual-order"
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                assert release.wait(timeout=5)
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    service._clock = lambda: clock[0]  # type: ignore[method-assign]

    try:
        # Issue #146: pipeline runs belong to the snapshot refresh; the page
        # read polls the cache until the background worker publishes.
        service.refresh_lp_dashboard_snapshot()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            first = service.lp_dashboard()
            if first["market_rewards"]["condition-1"]["state"] == "known":
                break
            time.sleep(0.01)
        assert calls == 1
        clock[0] = 61.0
        with ThreadPoolExecutor(max_workers=1) as clients:
            future = clients.submit(service.refresh_lp_dashboard_snapshot)
            assert entered.wait(timeout=2)
            expired = future.result(timeout=5)
    finally:
        release.set()

    reward = expired["market_rewards"]["condition-1"]
    assert reward["state"] == "known"
    assert reward["stale"] is True
    assert reward["market_amount"] == Decimal("0.80")


def test_lp_dashboard_covers_today_table_system_markets_in_reward_cache(
    tmp_path: Path,
) -> None:
    """A1: 委托表当天全部市场（含系统托管单）进入奖励缓存覆盖面。

    第一次 _lp_reward_cache_snapshot 只覆盖手动单；系统托管行的市场此前
    从不进入 market_rewards，也不进刷新队列。改动后第二次覆盖调用把当天
    委托表里剩余条件并入，后台批量读一次即让载荷携带 known 奖励。
    """

    class Account:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "system-order-1",
                        "condition_id": "condition-1",
                        "token_id": "sys-yes",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "system-order-1"
            return True

        def lp_reward_snapshots(
            self, reward_date: str, condition_ids: object
        ) -> dict[str, object]:
            return {
                str(condition_id): {
                    "state": "known",
                    "reward_date": reward_date,
                    "condition_id": str(condition_id),
                    "market_amount": "3.42",
                    "account_amount": "5.00",
                }
                for condition_id in condition_ids  # type: ignore[union-attr]
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    # 登记系统组会话：entry_order_id 归属后该行 management=system_managed
    # （非手动），A1 的覆盖面前提成立。
    store.lp_create_session(
        "session-1",
        "idempotency-1",
        state="entry_open",
        payload={
            "condition_id": "condition-1",
            "token_id": "sys-yes",
            "entry_order_id": "system-order-1",
            "market_title": "System market",
            "outcome": "YES",
        },
    )

    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"], "system order must appear in today table"
    assert first["lp_orders_today"][0]["management"] == "system_managed"
    # Issue #146: join the scheduled reward worker so the batch read lands
    # before reading the served cache.
    worker = service._lp_reward_refresh_thread
    if worker is not None:
        worker.join(timeout=2)
    payload = service.lp_dashboard()
    reward = payload["market_rewards"]["condition-1"]
    assert reward["state"] == "known"
    assert reward["market_amount"] == "3.42"


def test_lp_reward_cache_throttles_today_table_refetch_within_60s(
    tmp_path: Path,
) -> None:
    """A2: 委托表全部市场的奖励批量读受 60 秒节流——窗口内不重读，过期后才第二次读。"""

    calls = 0
    clock = [0.0]

    class Account:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "system-order-1",
                        "condition_id": "condition-1",
                        "token_id": "sys-yes",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return True

        def lp_reward_snapshots(
            self, reward_date: str, condition_ids: object
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                str(condition_id): {
                    "state": "known",
                    "reward_date": reward_date,
                    "condition_id": str(condition_id),
                    "market_amount": "3.42",
                }
                for condition_id in condition_ids  # type: ignore[union-attr]
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    service._clock = lambda: clock[0]  # type: ignore[method-assign]
    store.lp_create_session(
        "session-1",
        "idempotency-1",
        state="entry_open",
        payload={
            "condition_id": "condition-1",
            "token_id": "sys-yes",
            "entry_order_id": "system-order-1",
            "market_title": "System market",
            "outcome": "YES",
        },
    )

    def join_worker() -> None:
        worker = service._lp_reward_refresh_thread
        if worker is not None:
            worker.join(timeout=2)

    def served_reward() -> dict[str, object]:
        return service.lp_dashboard()["market_rewards"]["condition-1"]

    # 首次快照：未知占位进刷新队列，worker 批量读一次后发布 known。
    service.refresh_lp_dashboard_snapshot()
    join_worker()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if served_reward().get("state") == "known":
            break
        time.sleep(0.01)
    assert served_reward()["state"] == "known"
    assert calls == 1

    # 60 秒内的第二次快照：缓存命中（stale=False），不再触发批量读。
    service.refresh_lp_dashboard_snapshot()
    join_worker()
    assert calls == 1

    # 推进时钟越过 60 秒：缓存过期 → 第二次批量读。
    clock[0] = 61.0
    service.refresh_lp_dashboard_snapshot()
    join_worker()
    assert calls == 2


def test_lp_reward_cache_read_failure_keeps_last_amount_as_unknown(
    tmp_path: Path,
) -> None:
    """A3: 读失败 → 条目转未知、旧 market_amount 保留、不抛异常、有 reason。"""

    state: dict[str, object] = {"fail": False}
    clock = [0.0]

    class Account:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": "system-order-1",
                        "condition_id": "condition-1",
                        "token_id": "sys-yes",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return True

        def lp_reward_snapshots(
            self, reward_date: str, condition_ids: object
        ) -> dict[str, object]:
            if state["fail"] is True:
                raise RuntimeError("reward read unavailable")
            return {
                str(condition_id): {
                    "state": "known",
                    "reward_date": reward_date,
                    "condition_id": str(condition_id),
                    "market_amount": "3.42",
                }
                for condition_id in condition_ids  # type: ignore[union-attr]
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    service._clock = lambda: clock[0]  # type: ignore[method-assign]
    store.lp_create_session(
        "session-1",
        "idempotency-1",
        state="entry_open",
        payload={
            "condition_id": "condition-1",
            "token_id": "sys-yes",
            "entry_order_id": "system-order-1",
            "market_title": "System market",
            "outcome": "YES",
        },
    )

    def join_worker() -> None:
        worker = service._lp_reward_refresh_thread
        if worker is not None:
            worker.join(timeout=2)

    # 先成功一次：known 且 market_amount 落地。
    service.refresh_lp_dashboard_snapshot()
    join_worker()
    reward = service.lp_dashboard()["market_rewards"]["condition-1"]
    assert reward["state"] == "known"
    assert reward["market_amount"] == "3.42"

    # 再让批量读抛异常：pipeline 与 worker 都不得向外抛。
    state["fail"] = True
    clock[0] = 61.0
    service.refresh_lp_dashboard_snapshot()
    join_worker()
    failed = service.lp_dashboard()["market_rewards"]["condition-1"]
    assert failed["state"] == "unknown"
    # 旧值保留：绝不显示 $0。
    assert failed["market_amount"] == "3.42"
    assert failed.get("reason")


def test_lp_dashboard_drains_new_conditions_through_single_reward_worker(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    b_done = threading.Event()
    condition_reads: list[str] = []

    class Account:
        reads = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            self.reads += 1
            conditions = ("condition-a",) if self.reads == 1 else (
                "condition-a",
                "condition-b",
            )
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": f"manual-{condition}",
                        "condition_id": condition,
                        "asset_id": f"yes-{condition}",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                    for condition in conditions
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id.startswith("manual-")
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            condition_reads.append(condition_id)
            if condition_id == "condition-a" and len(condition_reads) == 1:
                entered.set()
                assert release.wait(timeout=5)
            if condition_id == "condition-b":
                b_done.set()
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )

    try:
        # Issue #146: the pipeline runs inside the snapshot refresh; the
        # second refresh picks up the newly listed condition.
        service.refresh_lp_dashboard_snapshot()
        assert entered.wait(timeout=2)
        service.refresh_lp_dashboard_snapshot()
        release.set()
        assert b_done.wait(timeout=2)
    finally:
        release.set()

    assert condition_reads == ["condition-a", "condition-b"]


def test_lp_dashboard_restarts_reward_worker_after_empty_queue_handoff(
    tmp_path: Path,
) -> None:
    gap_open = threading.Event()
    allow_exit = threading.Event()
    b_done = threading.Event()
    condition_reads: list[str] = []

    class HandoffLock:
        def __init__(self, service: PredictionExecutionService) -> None:
            self._lock = service._lp_dashboard_lock
            self._worker_lock_entries = 0
            self._armed = False
            self._pause_after_release = False

        def __enter__(self) -> "HandoffLock":
            self._lock.acquire()
            caller = sys._getframe(1).f_code.co_name
            if caller == "_refresh_lp_rewards":
                self._worker_lock_entries += 1
            self._pause_after_release = (
                not self._armed
                and caller == "_refresh_lp_rewards"
                and self._worker_lock_entries == 3
            )
            if self._pause_after_release:
                self._armed = True
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self._lock.release()
            if self._pause_after_release:
                gap_open.set()
                assert allow_exit.wait(timeout=5)
            return False

    class Account:
        reads = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            self.reads += 1
            condition_id = "condition-a" if self.reads == 1 else "condition-b"
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 12, tzinfo=UTC),
                "open_orders": [
                    {
                        "id": f"manual-{condition_id}",
                        "condition_id": condition_id,
                        "asset_id": f"yes-{condition_id}",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id.startswith("manual-")
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            condition_reads.append(condition_id)
            if condition_id == "condition-b":
                b_done.set()
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.80"),
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )
    class RefreshCapableHandoffLock(HandoffLock):
        """Issue #146: the snapshot refresh acquires the lock directly."""

        def acquire(self, blocking: bool = True) -> bool:
            return self._lock.acquire(blocking=blocking)

        def release(self) -> None:
            self._lock.release()

    service._lp_dashboard_lock = RefreshCapableHandoffLock(service)  # type: ignore[assignment]

    try:
        service.refresh_lp_dashboard_snapshot()
        assert gap_open.wait(timeout=2)
        service.refresh_lp_dashboard_snapshot()
        allow_exit.set()
        assert b_done.wait(timeout=2)
    finally:
        allow_exit.set()

    assert condition_reads == ["condition-a", "condition-b"]


def test_lp_dashboard_keeps_old_date_refresh_out_of_new_date_cache(
    tmp_path: Path,
) -> None:
    old_entered = threading.Event()
    old_release = threading.Event()
    new_entered = threading.Event()
    new_release = threading.Event()
    reward_reads = 0

    class Account:
        reads = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            self.reads += 1
            if self.reads == 1:
                checked_at = datetime(2026, 9, 15, 23, 59, tzinfo=UTC)
            elif self.reads == 2:
                checked_at = datetime(2026, 9, 16, 0, 1, tzinfo=UTC)
            else:
                raise RuntimeError("account read unavailable")
            return {
                "authenticated": True,
                "checked_at": checked_at,
                "open_orders": [
                    {
                        "id": "manual-order",
                        "condition_id": "condition-1",
                        "asset_id": "yes-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "original_size": Decimal("2"),
                        "size_matched": Decimal("0"),
                    }
                ],
                "positions": [],
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            assert order_id == "manual-order"
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            nonlocal reward_reads
            reward_reads += 1
            if reward_reads == 1:
                old_entered.set()
                assert old_release.wait(timeout=5)
                amount = Decimal("0.10")
            else:
                new_entered.set()
                assert new_release.wait(timeout=5)
                amount = Decimal("0.20")
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": amount,
            }

    class LP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {"state": "ready", "complete": True, "candidates": []}

        def status(self) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Account(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=LP(),
    )

    try:
        service.refresh_lp_dashboard_snapshot()
        assert old_entered.wait(timeout=2)
        # Issue #146: this second pipeline run belongs to the snapshot
        # refresh (the old route re-ran the pipeline on every request).
        current = service.refresh_lp_dashboard_snapshot()
        assert current["market_rewards"]["condition-1"]["reward_date"] == "2026-09-16"
        old_release.set()
        assert new_entered.wait(timeout=2)
        stale = service.refresh_lp_dashboard_snapshot()
        assert stale["stale"] is True
        reward = stale["market_rewards"]["condition-1"]
        assert reward["reward_date"] == "2026-09-16"
        assert reward["market_amount"] is None
        new_release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and reward_reads < 2:
            time.sleep(0.01)
        assert reward_reads == 2
    finally:
        old_release.set()
        new_release.set()

    published: dict[str, object] | None = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        candidate = service.lp_dashboard()
        reward = candidate["market_rewards"]["condition-1"]
        if reward["market_amount"] == Decimal("0.20"):
            published = candidate
            break
        time.sleep(0.01)
    assert published is not None
    reward = published["market_rewards"]["condition-1"]
    assert reward["reward_date"] == "2026-09-16"


def test_lp_reward_and_stale_observation_publications_preserve_each_other(
    tmp_path: Path,
) -> None:
    condition_id = "condition-1"
    coordination_ready = threading.Event()
    allow_reward = threading.Event()
    reward_done = threading.Event()
    coordination_mode: list[str] = []

    class CoordinatedCache(dict[str, object]):
        def __init__(self, values: Mapping[str, object]) -> None:
            super().__init__(values)

        def keys(self) -> Iterator[str]:
            if threading.current_thread().name == "mark-stale":
                coordination_mode.append("cache_read")
                coordination_ready.set()
                if not reward_done.wait(timeout=5):
                    raise AssertionError("reward publication did not complete")
            return super().keys()

    class CoordinatedLock:
        def __init__(self, lock: threading.RLock) -> None:
            self._lock = lock

        def __enter__(self) -> "CoordinatedLock":
            caller = sys._getframe(1).f_code.co_name
            if caller == "mark_stale":
                coordination_mode.append("lock_before_cache_read")
                coordination_ready.set()
                if not allow_reward.wait(timeout=5):
                    raise AssertionError("reward publication was not released")
                if not reward_done.wait(timeout=5):
                    raise AssertionError("reward publication did not complete")
            self._lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> bool:
            self._lock.release()
            return False

    class Trading:
        config = SimpleNamespace(wallet_address="0x" + "1" * 40)

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            assert reward_date == "2026-09-16"
            assert market == condition_id
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": market,
                "market_amount": Decimal("0.80"),
            }

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=Trading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
    )
    account_id = service._lp_account_id()
    assert account_id is not None
    service._store.save_lp_observation(
        account_id,
        condition_id,
        {"state": "ready", "stale": False, "trial_baseline": None},
    )
    service._lp_dashboard_cache = CoordinatedCache(
        {
            "state": "ready",
            "stale": False,
            "market_rewards": {
                condition_id: {
                    "state": "unknown",
                    "reward_date": "2026-09-16",
                    "condition_id": condition_id,
                    "market_amount": None,
                }
            },
            "lp_observations": {},
        }
    )
    service._lp_dashboard_lock = CoordinatedLock(service._lp_dashboard_lock)  # type: ignore[assignment]
    # Issue #146: refresh_lp_observations drives the snapshot refresh, not
    # the page cache read, so the stale-account stub moves with it.
    service.refresh_lp_dashboard_snapshot = lambda: {"state": "stale", "stale": True}  # type: ignore[method-assign]

    def refresh_stale() -> None:
        threading.current_thread().name = "mark-stale"
        service.refresh_lp_observations()

    def refresh_reward() -> None:
        threading.current_thread().name = "reward-worker"
        try:
            service._refresh_lp_reward_batch(
                "2026-09-16", (condition_id,), "2026-09-16T00:00:00.000000Z"
            )
        finally:
            reward_done.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        stale_future = workers.submit(refresh_stale)
        assert coordination_ready.wait(timeout=2)
        reward_future = workers.submit(refresh_reward)
        allow_reward.set()
        stale_future.result(timeout=5)
        reward_future.result(timeout=5)

    assert coordination_mode in (["cache_read"], ["lock_before_cache_read"])

    cache = service._lp_dashboard_cache
    assert isinstance(cache, Mapping)
    assert cache["state"] == "stale"
    assert cache["stale"] is True
    reward = cache["market_rewards"][condition_id]  # type: ignore[index]
    assert reward["state"] == "known"
    assert reward["market_amount"] == Decimal("0.80")
    observation = cache["lp_observations"][condition_id]  # type: ignore[index]
    assert observation["state"] == "unknown"
    assert observation["stale"] is True


def test_lp_observations_preserve_trial_after_manual_add(tmp_path: Path) -> None:
    condition_id = "condition-1"
    token_id = "yes-token"

    def order(order_id: str, quantity: Decimal, price: Decimal, filled: Decimal = Decimal("0"), side: str = "BUY") -> dict[str, object]:
        return {
            "id": order_id,
            "order_id": order_id,
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
            "side": side,
            "status": "LIVE",
            "price": price,
            "original_size": quantity,
            "size_matched": filled,
            "remaining_size": quantity - filled,
            "reward_min_size": Decimal("40"),
            "fees_enabled": False,
            "market_title": "Will it happen?",
        }

    state: dict[str, object] = {
        "orders": [order("trial-order", Decimal("40"), Decimal("0.50"))],
        "positions": [],
        "hourly_reward": Decimal("0.05"),
        "reward_available": True,
    }

    class LPObservationTrading:
        config = SimpleNamespace(wallet_address="0x" + "1" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": tuple(dict(row) for row in state["orders"]),
                "positions": tuple(dict(row) for row in state["positions"]),
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(self, reward_date: str, market: str) -> dict[str, object]:
            return {"state": "unknown", "reward_date": reward_date, "condition_id": market}

        def lp_reward_rates(self) -> dict[str, object]:
            if state["reward_available"] is not True:
                return {
                    "state": "unknown",
                    "complete": False,
                    "checked_at": datetime.now(UTC),
                    "markets": {},
                }
            hourly = state["hourly_reward"]
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "markets": {
                    condition_id: {
                        "state": "known",
                        "hourly_reward_usd": hourly,
                        "currency": "USD",
                        "checked_at": datetime.now(UTC),
                        "sources": ("native",),
                        "native": {
                            "state": "known",
                            "earning_percentage": Decimal("1"),
                            "daily_pool_usd": Decimal("1.2"),
                            "hourly_reward_usd": hourly,
                            "currency": "USD",
                        },
                    }
                },
            }

        def lp_order_books(self, token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
            return {
                token: {
                    "condition_id": condition_id,
                    "token_id": token,
                    "received_at": datetime.now(UTC),
                    "bids": [
                        {"price": Decimal("0.51"), "size": Decimal("20")},
                        {"price": Decimal("0.50"), "size": Decimal("1000")},
                        {"price": Decimal("0.46"), "size": Decimal("1000")},
                    ],
                    "asks": [],
                }
                for token in token_ids
            }

    service, _trading, store, monitor = execution_fixture(tmp_path)
    trading = LPObservationTrading()
    service._trading = trading

    state["reward_available"] = False
    delayed_trial = service.refresh_lp_observations()["observations"][condition_id]
    assert delayed_trial["stage"] == "trial_unknown"
    assert delayed_trial["current_yield_pct_per_hour"] is None
    assert delayed_trial["trial_reference"] is None

    state["reward_available"] = True
    trial = service.refresh_lp_observations()
    trial_market = trial["observations"][condition_id]
    assert Decimal(str(trial_market["current_yield_pct_per_hour"])) == Decimal("0.25")
    assert trial_market["trial_baseline"] is None

    state["orders"] = [
        order("trial-order", Decimal("40"), Decimal("0.50")),
        order("add-order", Decimal("80"), Decimal("0.50")),
    ]
    state["hourly_reward"] = Decimal("0.108")
    added = service.refresh_lp_observations()
    added_market = added["observations"][condition_id]
    baseline = added_market["trial_baseline"]
    assert isinstance(baseline, Mapping)
    assert Decimal(str(baseline["yield_pct_per_hour"])) == Decimal("0.25")
    assert baseline["quantity"] == "40"
    assert baseline["occupied_capital_usd"] == "20.00"
    assert Decimal(str(added_market["current_yield_pct_per_hour"])) == Decimal("0.18")
    assert added_market["exposure_quantity"] == "120"
    assert added_market["occupied_capital_usd"] == "60.00"

    state["orders"] = [
        order("trial-order", Decimal("40"), Decimal("0.50")),
        order("add-order", Decimal("80"), Decimal("0.49"), Decimal("10")),
        order("second-add", Decimal("30"), Decimal("0.50")),
        order("sell-order", Decimal("10"), Decimal("0.51"), side="SELL"),
    ]
    state["positions"] = [
        {
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
            "size": Decimal("10"),
            "average_price": Decimal("0.49"),
            "reward_min_size": Decimal("40"),
            "fees_enabled": False,
        }
    ]
    service.refresh_lp_observations()
    restarted_store = PredictionArbitrageStore(tmp_path / "data")
    restarted = PredictionExecutionService(
        store=restarted_store,
        monitor=monitor,
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
    )
    after_restart = restarted.refresh_lp_observations()["observations"][condition_id]
    assert after_restart["exposure_quantity"] == "150"
    assert after_restart["occupied_capital_usd"] == "74.20"
    assert Decimal(str(after_restart["trial_baseline"]["yield_pct_per_hour"])) == Decimal("0.25")

    enlarged_store = PredictionArbitrageStore(tmp_path / "enlarged-data")
    enlarged = PredictionExecutionService(
        store=enlarged_store,
        monitor=monitor,
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "enlarged-execution.lock",
    )
    state["orders"] = [order("already-large", Decimal("120"), Decimal("0.50"))]
    state["positions"] = []
    first_seen_large = enlarged.refresh_lp_observations()["observations"][condition_id]
    assert first_seen_large["trial_baseline"] is None
    assert first_seen_large["reason"] == "trial_baseline_unrecorded"

    state["orders"] = []
    state["hourly_reward"] = Decimal("0.05")
    flat = restarted.refresh_lp_observations()["observations"][condition_id]
    assert flat["stage"] == "flat"
    assert flat["trial_baseline"] is None
    state["orders"] = [order("new-trial", Decimal("40"), Decimal("0.50"))]
    restarted.refresh_lp_observations()
    state["orders"] = [
        order("new-trial", Decimal("40"), Decimal("0.50")),
        order("new-add", Decimal("80"), Decimal("0.50")),
    ]
    state["hourly_reward"] = Decimal("0.108")
    new_cycle = restarted.refresh_lp_observations()["observations"][condition_id]
    assert Decimal(str(new_cycle["trial_baseline"]["yield_pct_per_hour"])) == Decimal("0.25")


def test_lp_add_room_requires_current_aligned_reward_and_risk(tmp_path: Path) -> None:
    condition_id = "condition-1"
    token_id = "yes-token"
    state: dict[str, object] = {
        "market_id": condition_id,
        "orders": [],
        "positions": [],
        "hourly_reward": Decimal("0.10"),
        "rate_state": "known",
        "rate_age": 0,
        "account_age": 0,
        "account_complete": True,
        "account_error": False,
        "currency": "USD",
        "book_price": Decimal("0.46"),
        "order_price": Decimal("0.50"),
        "fees_enabled": False,
    }

    def order(quantity: Decimal) -> dict[str, object]:
        return {
            "id": "manual-order",
            "order_id": "manual-order",
            "condition_id": str(state["market_id"]),
            "token_id": token_id,
            "outcome": "YES",
            "side": "BUY",
            "status": "LIVE",
            "price": state["order_price"],
            "original_size": quantity,
            "size_matched": Decimal("0"),
            "remaining_size": quantity,
            "reward_min_size": Decimal("40"),
            "fees_enabled": state["fees_enabled"],
            "market_title": "Will it happen?",
        }

    class ReadOnlyTrading:
        config = SimpleNamespace(wallet_address="0x" + "2" * 40)

        def __init__(self) -> None:
            self.order_writes = 0
            self.cancellations = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            if state["account_error"] is True:
                raise RuntimeError("account_snapshot_failed")
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC) - timedelta(seconds=int(state["account_age"])),
                "open_orders": tuple(dict(row) for row in state["orders"]),
                "positions": tuple(dict(row) for row in state["positions"]),
                "open_orders_complete": state["account_complete"],
                "positions_complete": state["account_complete"],
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(self, reward_date: str, market: str) -> dict[str, object]:
            return {"state": "unknown", "reward_date": reward_date, "condition_id": market}

        def lp_reward_rates(self) -> dict[str, object]:
            hourly = state["hourly_reward"]
            checked_at = datetime.now(UTC) - timedelta(seconds=int(state["rate_age"]))
            if state["rate_state"] != "known" or state["market_id"] != condition_id:
                return {"state": "unknown", "complete": False, "checked_at": checked_at, "markets": {}}
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "markets": {
                        condition_id: {
                            "state": "known" if hourly is not None else "unknown",
                            "hourly_reward_usd": hourly,
                            "currency": state["currency"] if hourly is not None else None,
                        "checked_at": checked_at,
                        "sources": ("native",),
                        "native": {
                                "state": "known" if hourly is not None else "unknown",
                                "earning_percentage": Decimal("0") if hourly == 0 else Decimal("1"),
                                "hourly_reward_usd": hourly,
                                "currency": state["currency"] if hourly is not None else None,
                        },
                    }
                },
            }

        def lp_order_books(self, token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
            quantity = sum(
                (row["remaining_size"] for row in state["orders"] if row["side"] == "BUY"),
                Decimal("0"),
            )
            return {
                token: {
                    "condition_id": str(state["market_id"]),
                    "token_id": token,
                    "received_at": datetime.now(UTC),
                    "bids": [
                        {"price": Decimal("0.51"), "size": Decimal("20")},
                        {"price": Decimal("0.50"), "size": quantity},
                        {"price": state["book_price"], "size": Decimal("1000")},
                    ],
                    "asks": [],
                }
                for token in token_ids
            }

        def create_limit_order(self, **_kwargs: object) -> None:
            self.order_writes += 1

        def post_order(self, _order: object) -> None:
            self.order_writes += 1

        def cancel_orders(self, **_kwargs: object) -> None:
            self.cancellations += 1

    class CountingNotifier(FeishuWebhookNotifier):
        channel = "feishu"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, _title: str, _message: str) -> None:
            self.calls += 1

    service, _trading, store, monitor = execution_fixture(tmp_path)
    trading = ReadOnlyTrading()
    service._trading = trading
    notifier = CountingNotifier()
    service._notifier = notifier  # type: ignore[assignment]
    state["orders"] = [order(Decimal("200"))]

    initial = service.refresh_lp_observations()["observations"][condition_id]
    assert Decimal(str(initial["current_yield_pct_per_hour"])) == Decimal("0.1")
    assert initial["risk_state"] == "known"
    assert initial["risk_warning"] is False
    assert initial["add_room"]["available"] is True
    # Issue #146: the served payload reflects a fresh snapshot pass.
    assert service.refresh_lp_dashboard_snapshot()["lp_observations"][condition_id][
        "add_room"
    ]["available"] is True

    account_id = service._lp_account_id()
    assert isinstance(account_id, str)
    saved_initial = store.lp_observations(account_id)[condition_id]
    state["orders"] = [order(Decimal("400"))]
    notification_calls_before_get = notifier.calls
    changed_dashboard = service.refresh_lp_dashboard_snapshot()
    changed_observation = changed_dashboard["lp_observations"][condition_id]
    assert changed_dashboard["state"] == "ready"
    assert changed_dashboard["orders"][0]["quantity"] == Decimal("400")
    assert changed_observation["state"] == "unknown"
    assert changed_observation["reason"] == "exposure_changed"
    assert changed_observation["current_hourly_reward_usd"] is None
    assert changed_observation["occupied_capital_usd"] is None
    assert changed_observation["exposure_quantity"] is None
    assert changed_observation["risk_state"] == "unknown"
    assert changed_observation["risk_warning"] is None
    assert changed_observation["risk_directions"] == []
    assert changed_observation["add_room"] == {
        "available": False,
        "reason": "exposure_changed",
    }
    assert notifier.calls == notification_calls_before_get
    assert store.lp_observations(account_id)[condition_id] == saved_initial

    aligned = service.refresh_lp_observations()["observations"][condition_id]
    assert aligned["state"] == "known"
    assert aligned["exposure_quantity"] == "400"
    assert aligned["occupied_capital_usd"] == "200.00"

    state["order_price"] = Decimal("0.49")
    state["orders"] = [order(Decimal("400"))]
    price_dashboard = service.refresh_lp_dashboard_snapshot()
    price_observation = price_dashboard["lp_observations"][condition_id]
    assert price_dashboard["state"] == "ready"
    assert price_observation["reason"] == "exposure_changed"
    assert price_observation["occupied_capital_usd"] is None
    assert price_observation["risk_state"] == "unknown"
    assert store.lp_observations(account_id)[condition_id] == aligned

    price_aligned = service.refresh_lp_observations()["observations"][condition_id]
    assert price_aligned["state"] == "known"
    assert price_aligned["exposure_quantity"] == "400"
    assert price_aligned["occupied_capital_usd"] == "196.00"

    stored_aligned = store.lp_observations(account_id)[condition_id]
    aged_observation = {
        **stored_aligned,
        "checked_at": datetime.now(UTC) - timedelta(seconds=121),
    }
    store.save_lp_observation(account_id, condition_id, aged_observation)
    stored_before_aged_get = store.lp_observations(account_id)[condition_id]
    aged_dashboard = service.refresh_lp_dashboard_snapshot()
    aged_projection = aged_dashboard["lp_observations"][condition_id]
    assert aged_dashboard["state"] == "ready"
    assert aged_projection["reason"] == "observation_stale"
    assert aged_projection["occupied_capital_usd"] is None
    assert aged_projection["risk_state"] == "unknown"
    assert store.lp_observations(account_id)[condition_id] == stored_before_aged_get

    state["order_price"] = Decimal("0.50")
    state["orders"] = [order(Decimal("200"))]
    state["hourly_reward"] = Decimal("0.099")
    low_yield = service.refresh_lp_observations()["observations"][condition_id]
    assert low_yield["add_room"] == {"available": False, "reason": "yield_below_threshold"}
    state["hourly_reward"] = Decimal("0.20")
    state["book_price"] = Decimal("0.45")
    high_risk = service.refresh_lp_observations()["observations"][condition_id]
    assert high_risk["risk_warning"] is True
    assert high_risk["add_room"] == {"available": False, "reason": "risk_warning"}
    risk_alerts_before_account_failure = high_risk["risk_alerts"]

    state["account_complete"] = False
    incomplete = service.refresh_lp_observations()["observations"][condition_id]
    assert incomplete["state"] == "unknown"
    assert incomplete["current_hourly_reward_usd"] is None
    assert incomplete["occupied_capital_usd"] is None
    assert incomplete["exposure_quantity"] is None
    assert incomplete["risk_state"] == "unknown"
    assert incomplete["risk_warning"] is None
    assert incomplete["risk_directions"] == []
    assert incomplete["risk_alerts"] == risk_alerts_before_account_failure

    state["account_complete"] = True
    state["account_error"] = True
    failed_account = service.refresh_lp_observations()["observations"][condition_id]
    assert failed_account["state"] == "unknown"
    assert failed_account["occupied_capital_usd"] is None
    assert failed_account["risk_state"] == "unknown"
    assert failed_account["risk_warning"] is None
    assert failed_account["risk_directions"] == []
    assert failed_account["risk_alerts"] == risk_alerts_before_account_failure
    state["account_error"] = False

    state["hourly_reward"] = Decimal("0")
    state["book_price"] = Decimal("0.46")
    zero_reward = service.refresh_lp_observations()["observations"][condition_id]
    assert Decimal(str(zero_reward["current_yield_pct_per_hour"])) == Decimal("0")
    state["rate_state"] = "unknown"
    state["orders"] = [order(Decimal("400"))]
    unknown_reward = service.refresh_lp_observations()["observations"][condition_id]
    assert unknown_reward["current_yield_pct_per_hour"] is None
    assert unknown_reward["exposure_quantity"] == "400"
    assert unknown_reward["add_room"]["available"] is False

    state["rate_state"] = "known"
    state["hourly_reward"] = Decimal("0.20")
    state["currency"] = "EUR"
    unsupported_currency = service.refresh_lp_observations()["observations"][condition_id]
    assert unsupported_currency["current_hourly_reward_usd"] is None
    assert unsupported_currency["current_yield_pct_per_hour"] is None
    assert unsupported_currency["add_room"]["available"] is False
    state["currency"] = "USD"

    state["account_age"] = 121
    stale_account = service.refresh_lp_observations()["observations"][condition_id]
    assert stale_account["stale"] is True
    assert stale_account["current_hourly_reward_usd"] is None
    assert stale_account["occupied_capital_usd"] is None
    assert stale_account["exposure_quantity"] is None
    assert stale_account["risk_state"] == "unknown"
    assert stale_account["risk_warning"] is None
    assert stale_account["risk_directions"] == []
    assert stale_account["add_room"]["available"] is False
    assert service.refresh_lp_dashboard_snapshot()["lp_observations"][condition_id]["add_room"]["available"] is False

    state["account_age"] = 0
    state["account_complete"] = False
    incomplete = service.refresh_lp_observations()["observations"][condition_id]
    assert incomplete["add_room"]["available"] is False
    state["account_complete"] = True
    state["rate_age"] = 121
    expired_reward = service.refresh_lp_observations()["observations"][condition_id]
    assert expired_reward["current_yield_pct_per_hour"] is None
    assert expired_reward["add_room"]["available"] is False

    state["rate_age"] = 0
    state["market_id"] = "condition-2"
    state["orders"] = [order(Decimal("200"))]
    mismatched_market = service.refresh_lp_observations()["observations"]["condition-2"]
    assert mismatched_market["current_yield_pct_per_hour"] is None
    assert mismatched_market["add_room"]["available"] is False

    state["market_id"] = condition_id
    state["orders"] = []
    zero_capital = service.refresh_lp_observations()["observations"][condition_id]
    assert zero_capital["stage"] == "flat"
    assert zero_capital["current_yield_pct_per_hour"] is None
    assert zero_capital["add_room"]["available"] is False

    state["fees_enabled"] = None
    state["orders"] = [order(Decimal("200"))]
    unknown_fee = service.refresh_lp_observations()["observations"][condition_id]
    assert unknown_fee["risk_state"] == "unknown"
    assert unknown_fee["add_room"]["available"] is False
    notification_calls_before_get = notifier.calls
    assert service.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert notifier.calls == notification_calls_before_get
    assert trading.order_writes == 0
    assert trading.cancellations == 0


def test_lp_risk_alerts_deduplicate_per_channel_and_rearm(tmp_path: Path) -> None:
    condition_id = "condition-1"
    token_id = "yes-token"
    state: dict[str, object] = {
        "book_price": Decimal("0.46"),
        "book_unknown": False,
        "voice_time": datetime.fromisoformat("2026-07-15T08:00:00+08:00"),
        "voice_failures": 1,
        "order_writes": 0,
        "cancellations": 0,
    }
    order = {
        "order_id": "manual-order",
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "original_size": Decimal("100"),
        "size_matched": Decimal("0"),
        "remaining_size": Decimal("100"),
        "reward_min_size": Decimal("40"),
        "fees_enabled": False,
        "market_title": "Will it happen?",
    }

    class ReadOnlyTrading:
        config = SimpleNamespace(wallet_address="0x" + "3" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [dict(order)],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(self, reward_date: str, market: str) -> dict[str, object]:
            return {"state": "unknown", "reward_date": reward_date, "condition_id": market}

        def lp_reward_rates(self) -> dict[str, object]:
            checked_at = datetime.now(UTC)
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "markets": {
                    condition_id: {
                        "state": "known",
                        "hourly_reward_usd": Decimal("0.20"),
                        "currency": "USD",
                        "checked_at": checked_at,
                        "sources": ("native",),
                        "native": {
                            "state": "known",
                            "earning_percentage": Decimal("1"),
                            "hourly_reward_usd": Decimal("0.20"),
                            "currency": "USD",
                        },
                    }
                },
            }

        def lp_order_books(self, token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
            if state["book_unknown"] is True:
                return {}
            return {
                token: {
                    "condition_id": condition_id,
                    "token_id": token,
                    "received_at": datetime.now(UTC),
                    "bids": [
                        {"price": Decimal("0.51"), "size": Decimal("20")},
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": state["book_price"], "size": Decimal("1000")},
                    ],
                    "asks": [],
                }
                for token in token_ids
            }

        def create_limit_order(self, **_kwargs: object) -> None:
            state["order_writes"] = int(state["order_writes"]) + 1

        def post_order(self, _order: object) -> None:
            state["order_writes"] = int(state["order_writes"]) + 1

        def cancel_orders(self, **_kwargs: object) -> None:
            state["cancellations"] = int(state["cancellations"]) + 1

    posted: list[dict[str, object]] = []
    voice_texts: list[str] = []

    def fake_post(
        _url: str,
        payload: dict[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        posted.append(payload)
        return {"code": 0}

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        voice_texts.append(shlex.split(command[-1])[1])
        if int(state["voice_failures"]) > 0:
            state["voice_failures"] = int(state["voice_failures"]) - 1
            return subprocess.CompletedProcess(command, 255)
        return subprocess.CompletedProcess(command, 0)

    notifier = CompositeNotifier(
        [
            FeishuWebhookNotifier(
                webhook_url="https://feishu.invalid/hook", post_json=fake_post
            ),
            XiaoaiSSHNotifier(
                host="speaker.local",
                ssh_key=tmp_path / "unused-key",
                run_command=fake_run,
                lock_path=tmp_path / "lp-risk-voice.lock",
                now_fn=lambda: state["voice_time"],
            ),
        ]
    )
    service, _trading, _store, monitor = execution_fixture(tmp_path)
    trading = ReadOnlyTrading()
    service._trading = trading
    service._notifier = notifier
    observation = service.refresh_lp_observations()["observations"][condition_id]
    assert observation["risk_warning"] is False
    assert posted == []
    assert voice_texts == []

    state["book_price"] = Decimal("0.44")
    triggered = service.refresh_lp_observations()["observations"][condition_id]
    assert triggered["risk_warning"] is True
    assert len(posted) == 1
    assert len(voice_texts) == 1
    alert = triggered["risk_alerts"]["YES"]
    assert alert["active"] is True
    assert alert["channels"]["feishu"]["success"] is True
    assert alert["channels"]["xiaoai"]["success"] is False
    assert "LP 风险警告" in posted[-1]["content"]["text"]
    assert voice_texts[-1] == "LP 风险警告，1 个标的，请立即查看飞书。"
    assert "标的：Will it happen?（condition-1）" in posted[-1]["content"]["text"]

    retry = service.refresh_lp_observations()["observations"][condition_id]
    assert len(posted) == 1
    assert len(voice_texts) == 2
    assert retry["risk_alerts"]["YES"]["channels"]["xiaoai"]["success"] is True
    assert voice_texts[-1] == "LP 风险警告，1 个标的，请立即查看飞书。"

    restarted = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=monitor,
        trading=trading,
        notifier=notifier,
        lock_path=tmp_path / "restarted-execution.lock",
    )
    restarted.refresh_lp_observations()
    state["book_unknown"] = True
    unknown = restarted.refresh_lp_observations()["observations"][condition_id]
    assert unknown["risk_state"] == "unknown"
    assert unknown["risk_alerts"]["YES"]["active"] is True
    assert len(posted) == 1
    assert len(voice_texts) == 2

    state["book_unknown"] = False
    state["book_price"] = Decimal("0.46")
    recovered = restarted.refresh_lp_observations()["observations"][condition_id]
    assert recovered["risk_warning"] is False
    assert recovered["risk_alerts"]["YES"]["active"] is False
    assert len(posted) == 1
    assert len(voice_texts) == 2

    # 语音冷却 5 分钟：重触发前推进注入时钟，验证恢复后再播报而非被限流抑制
    state["voice_time"] = datetime.fromisoformat("2026-07-15T08:10:00+08:00")
    state["book_price"] = Decimal("0.44")
    retriggered = restarted.refresh_lp_observations()["observations"][condition_id]
    assert len(posted) == 2
    assert len(voice_texts) == 3
    assert retriggered["risk_alerts"]["YES"]["channels"]["feishu"]["success"] is True
    assert retriggered["risk_alerts"]["YES"]["channels"]["xiaoai"]["success"] is True

    state["book_price"] = Decimal("0.46")
    restarted.refresh_lp_observations()
    state["voice_time"] = datetime.fromisoformat("2026-07-15T23:00:00+08:00")
    state["book_price"] = Decimal("0.44")
    night = restarted.refresh_lp_observations()["observations"][condition_id]
    assert len(posted) == 3
    assert len(voice_texts) == 3
    assert night["risk_alerts"]["YES"]["channels"]["xiaoai"]["suppressed"] is True

    state["voice_time"] = datetime.fromisoformat("2026-07-16T08:00:00+08:00")
    restarted.refresh_lp_observations()
    assert len(posted) == 3
    assert len(voice_texts) == 4

    state["voice_time"] = datetime.fromisoformat("2026-07-16T23:00:00+08:00")
    state["book_price"] = Decimal("0.46")
    restarted.refresh_lp_observations()
    state["voice_time"] = datetime.fromisoformat("2026-07-17T23:00:00+08:00")
    state["book_price"] = Decimal("0.44")
    second_night = restarted.refresh_lp_observations()["observations"][condition_id]
    assert len(posted) == 4
    assert second_night["risk_alerts"]["YES"]["channels"]["xiaoai"]["suppressed"] is True
    state["voice_time"] = datetime.fromisoformat("2026-07-18T08:00:00+08:00")
    state["book_price"] = Decimal("0.46")
    restarted.refresh_lp_observations()
    assert len(voice_texts) == 4
    assert state["order_writes"] == 0
    assert state["cancellations"] == 0

def test_state_refreshes_signal_metrics_on_demand_but_lp_dashboard_does_not(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(tmp_path)
    summary_calls = 0

    def summary() -> dict[str, object]:
        nonlocal summary_calls
        summary_calls += 1
        return {
            "signals_24h": 2,
            "annualized_yields": {"7d": ["0.10"], "30d": ["0.10"]},
        }

    def forbidden_history(_window: str) -> list[dict[str, object]]:
        raise AssertionError("state metrics loaded the full signal history")

    monitor._store.signal_metric_summary = summary  # type: ignore[method-assign]
    monitor._store.signal_history = forbidden_history  # type: ignore[method-assign]

    class Execution(_Execution):
        _breaker_open = False

        def lp_dashboard(self) -> dict[str, object]:
            return {
                "state": "ready",
                "orders": [],
                "positions": [],
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

    runtime = _Runtime()
    runtime.store = monitor._store  # type: ignore[assignment]
    runtime.monitor = monitor
    runtime.execution = Execution()

    with _server(runtime) as base:
        lp_status, lp_payload = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        state_status, first_state = _response(
            base + "/api/prediction-arbitrage/state"
        )
        second_status, second_state = _response(
            base + "/api/prediction-arbitrage/state"
        )

    assert lp_status == state_status == second_status == 200
    assert lp_payload["state"] == "ready"
    assert summary_calls == 1
    assert first_state["signals_24h"] == second_state["signals_24h"] == 2
    assert (
        first_state["relation_discovery"]["annualized_distribution"]
        == second_state["relation_discovery"]["annualized_distribution"]
    )


def test_lp_dashboard_reward_share_target_status_is_market_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_base = datetime.now(UTC)
    controlled_now = [source_base]

    sequence = [
        Decimal("4.8"),
        Decimal("5"),
        Decimal("5.956665"),
        Decimal("8"),
        Decimal("8.000001"),
        Decimal("8.5"),
        Decimal("8.5"),
        Decimal("8.5"),
        Decimal("8.5"),
    ]

    class Account:
        wallet_address = "wallet"

        def __init__(self) -> None:
            self.config = SimpleNamespace(wallet_address=self.wallet_address)
            self.share_reads = 0
            self.order_writes = 0
            self.cancellations = 0
            self.percentages = sequence[0]
            self.share_result: dict[str, object] | None = None
            self.share_failure = False

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "open_orders": (
                    {
                        "order_id": "a-1",
                        "condition_id": "condition-a",
                        "token_id": "a-yes",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                    },
                    {
                        "order_id": "a-2",
                        "condition_id": "condition-a",
                        "token_id": "a-no",
                        "outcome": "NO",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.45"),
                        "original_size": Decimal("10"),
                        "size_matched": Decimal("0"),
                    },
                    {
                        "order_id": "b-1",
                        "condition_id": "condition-b",
                        "token_id": "b-yes",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.40"),
                        "original_size": Decimal("4"),
                        "size_matched": Decimal("1"),
                    },
                    {
                        "order_id": "managed-1",
                        "condition_id": "condition-managed",
                        "token_id": "managed-yes",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.55"),
                        "original_size": Decimal("8"),
                        "size_matched": Decimal("2"),
                    },
                ),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return order_id == "a-1"

        def lp_reward_percentages(self) -> dict[str, object]:
            if self.share_failure:
                raise RuntimeError("reward share read unavailable")
            if self.share_result is not None:
                self.share_reads += 1
                result = dict(self.share_result)
                age_seconds = result.pop("_age_seconds", None)
                if isinstance(age_seconds, (int, float)):
                    result["checked_at"] = controlled_now[0] - timedelta(
                        seconds=age_seconds
                    )
                return result
            value = sequence[min(self.share_reads, len(sequence) - 1)]
            self.share_reads += 1
            self.percentages = value
            return {
                "state": "known",
                "scope": "account",
                "maker_address": "wallet",
                "percentages": {
                    "condition-a": value,
                    "condition-b": Decimal("5"),
                    "condition-managed": Decimal("5"),
                },
                "checked_at": controlled_now[0]
                - timedelta(microseconds=self.share_reads),
            }

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

        def post_order(self, _order: object) -> object:
            self.order_writes += 1
            return object()

        def cancel_orders(self, **_kwargs: object) -> object:
            self.cancellations += 1
            return object()

    service, _trading, store, monitor = execution_fixture(tmp_path)
    monkeypatch.setattr(
        prediction_execution_module, "_utc_now", lambda: controlled_now[0]
    )
    account = Account()
    service._trading = account
    checked_at = controlled_now[0].isoformat().replace("+00:00", "Z")
    guide_checked_at = controlled_now[0]
    guide_expires_at = guide_checked_at + timedelta(hours=1)
    valid_recommendation = {
        "condition_id": "condition-rec",
        "market_id": "market-rec",
        "market_title": "Reference market",
        "daily_pool_usd": Decimal("99"),
        "state": "eligible",
        "directions": {
            "YES": {
                "condition_id": "condition-rec",
                "market_id": "market-rec",
                "token_id": "rec-yes",
                "outcome": "YES",
                "state": "eligible",
                "eligible": True,
                "reason_codes": [],
                "guidance": {
                    "condition_id": "condition-rec",
                    "market_id": "market-rec",
                    "token_id": "rec-yes",
                    "outcome": "YES",
                    "price": Decimal("0.50"),
                    "quantity": Decimal("20"),
                    "required_capital": Decimal("10"),
                    "estimated_exit_loss": Decimal("0.20"),
                    "estimated_exit_loss_ratio": Decimal("0.02"),
                    "checked_at": guide_checked_at,
                    "expires_at": guide_expires_at,
                },
            }
        },
    }
    unknown_recommendation = {
        "condition_id": "condition-no-pool",
        "market_id": "market-no-pool",
        "daily_pool_usd": None,
        "state": "unknown",
        "directions": {
            "YES": {
                "condition_id": "condition-no-pool",
                "market_id": "market-no-pool",
                "token_id": "no-pool-yes",
                "outcome": "YES",
                "state": "unknown",
                "eligible": False,
                "reason_codes": ["reward_pool_unknown"],
                "guidance": None,
            }
        },
    }
    store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "scanning": False,
            "candidates": [],
            "recommendations": [valid_recommendation],
            "selected_results": [valid_recommendation, unknown_recommendation],
            "checked_at": checked_at,
            "last_success_at": checked_at,
            "last_attempt_at": checked_at,
            "candidate_rows_fresh": True,
            "catalog_complete": True,
            "missing_metadata_condition_ids": [],
            "missing_book_token_ids": [],
            "funnel": {},
            "selected_market_ids": [],
        }
    )
    store.lp_create_session(
        "managed-session",
        "managed-idempotency",
        state="entry_open",
        payload={
            "condition_id": "condition-managed",
            "token_id": "managed-yes",
            "entry_order_id": "managed-1",
            "market_title": "Managed market",
            "outcome": "YES",
        },
    )
    service._lp = PolymarketLPService(
        store, object(), clock=lambda: controlled_now[0]
    )
    account_id = service._lp_account_id()
    assert account_id is not None
    store.update_lp_observation(
        account_id,
        "condition-a",
        {"share_alert": {"enabled": True, "market_title": "Market A"}},
    )
    clock = [0.0]
    service._clock = lambda: clock[0]
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor
    runtime.execution = service

    expected = [
        ("4.8", "space", "0.2", "normal", None),
        ("5", "target", "0", "normal", "0.2"),
        ("5.956665", "target", "0", "normal", "0.956665"),
        ("8", "target", "0", "normal", "2.043335"),
        ("8.000001", "excess", "0.000001", "warning", "0.000001"),
        ("8.5", "excess", "0.5", "warning", "0.499999"),
        ("8.5", "excess", "0.5", "warning", "0.499999"),
        ("8.5", "excess", "0.5", "warning", "0.0"),
        ("8.5", "excess", "0.5", "warning", "0.0"),
    ]
    previous_checked_at: str | None = None
    previous_delta: str | None = None
    with _server(runtime) as base:
        for index, (
            expected_value,
            expected_target,
            expected_target_delta,
            expected_severity,
            expected_delta,
        ) in enumerate(expected):
            clock[0] += 5 if index in {0, 6} else 61
            controlled_now[0] = source_base + timedelta(seconds=clock[0])
            # Issue #146: run the snapshot pipeline, then serve it the way
            # the route does. Join the reward worker first so the refresh
            # owns the pipeline lock and cannot coalesce into the cache.
            worker = service._lp_reward_refresh_thread
            if worker is not None:
                worker.join(timeout=2)
            payload = prediction_service._lp_projection_safe_value(
                service.refresh_lp_dashboard_snapshot()
            )
            assert "condition-a" in payload["reward_shares"], payload
            share = payload["reward_shares"]["condition-a"]
            assert share["percentage"] == expected_value
            assert share["target_status"] == expected_target
            assert share["target_delta_percentage_points"] == expected_target_delta
            assert share["severity"] == expected_severity
            assert share["state"] == "known"
            assert share["delta_percentage_points"] == expected_delta
            if index == 4:
                previous_checked_at = share["checked_at"]
                previous_delta = share["delta_percentage_points"]
            if index == 5:
                assert share["checked_at"] != previous_checked_at
                previous_checked_at = share["checked_at"]
                previous_delta = share["delta_percentage_points"]
            if index == 6:
                assert share["checked_at"] == previous_checked_at
                assert share["delta_percentage_points"] == previous_delta
                assert account.share_reads == 6
            if index == 0:
                assert all(
                    row["condition_id"] != "condition-rec"
                    for row in payload["recommendations"]
                )
                # Issue #157: the unknown-qualification row is not stored in
                # the pool, so selected_results carries no condition-rec row
                # for diagnostics; the market-scoped share projection is
                # covered by the share-read accounting below.
                assert all(
                    row["condition_id"] != "condition-rec"
                    for row in payload["selected_results"]
                )
                # (The old diagnostic rows for condition-rec /
                # condition-no-pool are gone: unknown qualifications no
                # longer persist pool rows. The market-scoped reference
                # share projection is exercised by the target-status and
                # boundary checks below, which read market_rewards /
                # reward_shares for the same conditions.)

        boundary_service = PredictionExecutionService(
            store=store,
            monitor=monitor,
            trading=account,
            notifier=NullNotifier(),
            lock_path=tmp_path / "share-boundary.lock",
        )
        boundary_service._lp = PolymarketLPService(
            store, object(), clock=lambda: datetime.now(UTC)
        )
        boundary_service._clock = lambda: clock[0]
        runtime.execution = boundary_service

        def read_share() -> dict[str, object]:
            # Issue #146: drive the current execution service's snapshot
            # pipeline (the route only serves its published cache).
            execution = runtime.execution
            assert execution is not None
            worker = getattr(execution, "_lp_reward_refresh_thread", None)
            if worker is not None:
                worker.join(timeout=2)
            dashboard = prediction_service._lp_projection_safe_value(
                execution.refresh_lp_dashboard_snapshot()
            )
            return dashboard["reward_shares"]["condition-a"]

        last_known = read_share()
        assert last_known["state"] == "known"

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "another-wallet",
            "percentages": {"condition-a": Decimal("6")},
            "checked_at": controlled_now[0],
        }
        clock[0] += 61
        wrong_maker = read_share()
        assert wrong_maker["state"] == "unknown"
        assert wrong_maker["target_status"] == "unknown"
        assert wrong_maker["percentage"] == last_known["percentage"]

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "wallet",
            "percentages": {"condition-a": Decimal("-0.1")},
            "checked_at": controlled_now[0],
        }
        clock[0] += 61
        invalid_percentage = read_share()
        assert invalid_percentage["state"] == "unknown"
        assert invalid_percentage["target_status"] == "unknown"

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "wallet",
            "percentages": {"condition-a": Decimal("6")},
            "checked_at": controlled_now[0] + timedelta(seconds=1),
        }
        clock[0] += 61
        future = read_share()
        assert future["state"] == "unknown"
        assert future["target_status"] == "unknown"

        fresh_service = PredictionExecutionService(
            store=store,
            monitor=monitor,
            trading=account,
            notifier=NullNotifier(),
            lock_path=tmp_path / "share-fresh-boundary.lock",
        )
        fresh_service._lp = PolymarketLPService(
            store, object(), clock=lambda: controlled_now[0]
        )
        fresh_service._clock = lambda: clock[0]
        runtime.execution = fresh_service

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "wallet",
            "percentages": {"condition-a": Decimal("6")},
            "_age_seconds": 29.999,
        }
        clock[0] += 61
        fresh_boundary = read_share()
        assert fresh_boundary["state"] == "known", fresh_boundary
        assert fresh_boundary["target_status"] == "target"

        exact_service = PredictionExecutionService(
            store=store,
            monitor=monitor,
            trading=account,
            notifier=NullNotifier(),
            lock_path=tmp_path / "share-exact-boundary.lock",
        )
        exact_service._lp = PolymarketLPService(
            store, object(), clock=lambda: controlled_now[0]
        )
        exact_service._clock = lambda: clock[0]
        runtime.execution = exact_service

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "wallet",
            "percentages": {"condition-a": Decimal("6")},
            "_age_seconds": 30.0,
        }
        clock[0] += 61
        stale_boundary = read_share()
        assert stale_boundary["state"] == "unknown"
        assert stale_boundary["target_status"] == "unknown"

        account.share_result = {
            "state": "known",
            "scope": "account",
            "maker_address": "wallet",
            "percentages": {"condition-b": Decimal("6")},
            "checked_at": controlled_now[0],
        }
        clock[0] += 61
        missing_condition = read_share()
        assert missing_condition["state"] == "unknown"
        assert missing_condition["target_status"] == "unknown"

        account.share_result = None
        account.share_failure = True
        clock[0] += 61
        failed_read = read_share()
        assert failed_read["state"] == "unknown"
        assert failed_read["target_status"] == "unknown"

    assert len(payload["reward_shares"]) == 3
    assert payload["reward_shares"]["condition-managed"]["percentage"] == "5"
    assert payload["reward_shares"]["condition-b"]["target_status"] == "target"
    assert payload["lp_observations"]["condition-a"]["share_alert"]["enabled"] is True
    assert "condition-b" not in payload["lp_observations"] or (
        payload["lp_observations"]["condition-b"].get("share_alert", {}).get("enabled") is not True
    )
    assert next(
        row for row in payload["orders"] if row["condition_id"] == "condition-managed"
    )["management"] == "system_managed"
    assert payload["orders"][0]["remaining_quantity"] == "15"
    assert payload["orders"][1]["remaining_quantity"] == "10"
    assert payload["orders"][2]["remaining_quantity"] == "3"
    assert payload["recommendations"] == []
    assert account.order_writes == 0
    assert account.cancellations == 0


class _ShareWatchAccount:
    """Fake trading account for the share-watch full-enablement tests."""

    wallet_address = "wallet-share-watch"

    def __init__(
        self,
        *,
        include_second_market: bool = True,
        clock: list[datetime] | None = None,
    ) -> None:
        self.config = SimpleNamespace(wallet_address=self.wallet_address)
        self._clock = clock if clock is not None else [_share_watch_base()]
        self.open_orders: list[dict[str, object]] = [
            {
                "order_id": "a-1",
                "condition_id": "condition-a",
                "market_title": "Market A",
                "token_id": "a-yes",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.40"),
                "original_size": Decimal("20"),
                "size_matched": Decimal("5"),
            }
        ]
        if include_second_market:
            self.open_orders.append(
                {
                    "order_id": "b-1",
                    "condition_id": "condition-b",
                    "market_title": "Market B",
                    "token_id": "b-yes",
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.50"),
                    "original_size": Decimal("12"),
                    "size_matched": Decimal("2"),
                }
            )
        self.reward_value = Decimal("8.5")
        self.reward_checked_at: datetime | None = None
        self.reward_failure = False

    def lp_open_orders_snapshot(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "open_orders": tuple(self.open_orders),
            "open_orders_complete": True,
            "checked_at": self._clock[0],
        }

    def lp_reward_percentages(self) -> dict[str, object]:
        if self.reward_failure:
            raise RuntimeError("reward share read unavailable")
        percentages: dict[str, object] = {"condition-a": self.reward_value}
        if any(
            str(order.get("condition_id") or "") == "condition-b"
            for order in self.open_orders
        ):
            percentages["condition-b"] = self.reward_value
        return {
            "state": "known",
            "scope": "account",
            "maker_address": self.wallet_address,
            "percentages": percentages,
            "checked_at": self.reward_checked_at or self._clock[0],
        }


class _ShareWatchNotifier(XiaoaiSSHNotifier):
    def __init__(
        self,
        *,
        fail: bool = False,
        respect_quiet_hours: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.successes: list[tuple[str, str]] = []
        self.fail = fail
        self.respect_quiet_hours = respect_quiet_hours
        self._now = now or (lambda: datetime.now(UTC))

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))
        if self.respect_quiet_hours and not xiaoai_voice_allowed(self._now()):
            raise XiaoaiVoiceSuppressed("quiet hours")
        if self.fail:
            raise NotificationError("delivery failed")
        self.successes.append((title, message))


def _share_watch_base() -> datetime:
    return datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _share_watch_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    include_second_market: bool = True,
) -> tuple[
    PredictionExecutionService,
    _ShareWatchAccount,
    _ShareWatchNotifier,
    list[datetime],
    datetime,
]:
    base = _share_watch_base()
    clock: list[datetime] = [base]
    account = _ShareWatchAccount(
        include_second_market=include_second_market, clock=clock
    )
    notifier = _ShareWatchNotifier()
    service, _trading, _store, _monitor = execution_fixture(tmp_path / name)
    service._trading = account
    service._notifier = notifier
    service._clock = lambda: (clock[0] - base).total_seconds()
    monkeypatch.setattr(prediction_execution_module, "_utc_now", lambda: clock[0])
    return service, account, notifier, clock, base


def _share_watch_refresh(
    service: PredictionExecutionService,
    account: _ShareWatchAccount,
    clock: list[datetime],
    base: datetime,
):
    def refresh(
        seconds: float,
        value: str,
        *,
        checked_at: datetime | None = None,
    ) -> dict[str, object]:
        clock[0] = base + timedelta(seconds=seconds)
        account.reward_value = Decimal(value)
        account.reward_checked_at = (
            checked_at if checked_at is not None else clock[0]
        )
        return service.refresh_lp_share_watch()

    return refresh


def _share_watch_breach_episode(refresh, *, value_second: str = "8.6") -> None:
    # Independent fresh over-limit samples must span a complete minute
    # before the voice fires; the 50s -> 70s step stays inside the 30s
    # source-freshness window, so continuity from t0 is preserved.
    for seconds in (0, 10, 20, 30, 40, 50):
        refresh(seconds, "8.5")
    refresh(70, value_second)


def test_lp_share_watch_alerts_without_selection_for_every_active_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="full-enable"
    )
    refresh = _share_watch_refresh(service, account, clock, base)
    t0_text = base.isoformat(timespec="microseconds").replace("+00:00", "Z")

    # No persisted selection exists; both markets hold unfinished orders, so
    # both are monitored and each fires exactly one independent episode.
    _share_watch_breach_episode(refresh)
    assert len(notifier.successes) == 2
    assert all(title == "LP 份额预警" for title, _message in notifier.successes)
    by_market = {
        "Market A" if "Market A" in message else "Market B": message
        for _title, message in notifier.successes
    }
    assert set(by_market) == {"Market A", "Market B"}
    state = service.lp_share_watch_state()
    for condition_id in ("condition-a", "condition-b"):
        alert = state[condition_id]
        assert alert["breach_started_at"] == t0_text
        assert alert["notification_sent"] is True
        assert alert["notification_pending"] is False
    assert "8.6" in by_market["Market A"]
    assert "Market B" in by_market["Market B"]

    # A third over-limit sample in the same episode must not repeat.
    refresh(130, "8.7")
    assert len(notifier.successes) == 2


def test_lp_share_watch_recovery_resets_breach_and_rearms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="recovery", include_second_market=False
    )
    refresh = _share_watch_refresh(service, account, clock, base)

    _share_watch_breach_episode(refresh)
    assert len(notifier.successes) == 1

    # Falling back to the target band clears the breach and the sent flag so
    # a later episode can fire again, without a duplicate right now.
    refresh(80, "6.0")
    state = service.lp_share_watch_state()["condition-a"]
    assert state["breach_started_at"] is None
    assert state["notification_sent"] is False
    assert state["notification_pending"] is False
    assert len(notifier.successes) == 1

    # The re-armed watch needs a complete fresh minute again: the second
    # episode over 8% starts at t=90 and first succeeds at t=150, proving
    # notification_sent was really reset above.
    for seconds in (90, 100, 110, 120, 130, 140):
        refresh(seconds, "8.5")
    assert len(notifier.successes) == 1
    refresh(150, "8.6")
    assert len(notifier.successes) == 2
    state = service.lp_share_watch_state()["condition-a"]
    assert state["notification_sent"] is True
    assert state["notification_pending"] is False


def test_lp_share_watch_stops_and_clears_breach_when_orders_vanish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="orders-vanish", include_second_market=False
    )
    refresh = _share_watch_refresh(service, account, clock, base)

    _share_watch_breach_episode(refresh)
    assert len(notifier.successes) == 1

    # With no unfinished order left the market leaves the monitored set; the
    # breach state is cleared but the last observed share is retained.
    account.open_orders = []
    result = refresh(80, "8.6")
    assert result["state"] == "ready"
    state = service.lp_share_watch_state()["condition-a"]
    assert state["breach_started_at"] is None
    assert state["notification_pending"] is False
    assert state["last_share_percentage"] == "8.6"
    assert len(notifier.successes) == 1


def test_lp_share_watch_stale_source_keeps_old_share_without_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="stale-source", include_second_market=False
    )
    refresh = _share_watch_refresh(service, account, clock, base)
    t70_text = (base + timedelta(seconds=70)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")

    _share_watch_breach_episode(refresh)
    assert len(notifier.successes) == 1

    # A percentage result older than the freshness window must not feed the
    # watch: keep the previous share value, drop the breach, stay quiet.
    refresh(100, "8.9", checked_at=base + timedelta(seconds=70))
    state = service.lp_share_watch_state()["condition-a"]
    assert state["breach_started_at"] is None
    assert state["notification_pending"] is False
    assert state["last_share_percentage"] == "8.6"
    assert state["last_share_checked_at"] == t70_text
    assert len(notifier.successes) == 1


def test_lp_share_watch_restart_discards_unfinished_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="restart-reset", include_second_market=False
    )
    refresh = _share_watch_refresh(service, account, clock, base)

    # An unfinished breach (window still open, nothing delivered yet) exists
    # when the service process ends.
    for seconds in (0, 10, 20, 30, 40, 50):
        refresh(seconds, "8.5")
    assert (
        service.lp_share_watch_state()["condition-a"]["breach_started_at"]
        is not None
    )
    assert notifier.successes == []

    restarted = PredictionExecutionService(
        store=service._store,
        monitor=service._monitor,
        trading=account,
        notifier=notifier,
        lock_path=tmp_path / "restart-reset-after-samples.lock",
    )
    restarted._clock = lambda: (clock[0] - base).total_seconds()
    refresh_restarted = _share_watch_refresh(restarted, account, clock, base)

    # The new instance must re-establish the source-timestamp window from its
    # own first fresh sample instead of inheriting the old one.
    for seconds in (60, 70, 80, 90, 100, 110):
        refresh_restarted(seconds, "8.5")
    assert notifier.successes == []
    refresh_restarted(120, "8.6")
    assert len(notifier.successes) == 1


def test_lp_share_watch_quiet_hours_hold_pending_until_morning_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, _default_notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="quiet-hours", include_second_market=False
    )
    notifier = _ShareWatchNotifier(respect_quiet_hours=True, now=lambda: clock[0])
    service._notifier = notifier
    refresh = _share_watch_refresh(service, account, clock, base)

    # Beijing 07:58–07:59:50 samples complete a minute inside the 23:00-08:00
    # quiet window: attempts are suppressed and the event stays pending.
    for seconds in range(43080, 43191, 10):
        refresh(seconds, "8.5")
    assert notifier.calls
    assert notifier.successes == []
    state = service.lp_share_watch_state()["condition-a"]
    assert state["notification_pending"] is True

    # Beijing 08:00:00 releases the held event exactly once; later samples of
    # the same episode stay deduplicated.
    refresh(43200, "8.6")
    assert len(notifier.successes) == 1
    for seconds in (43210, 43220):
        refresh(seconds, "8.6")
    assert len(notifier.successes) == 1


def test_lp_share_watch_failed_delivery_stays_pending_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, _default_notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="pending-retry", include_second_market=False
    )
    notifier = _ShareWatchNotifier(fail=True)
    service._notifier = notifier
    refresh = _share_watch_refresh(service, account, clock, base)

    # The confirmed minute fires into a failing channel: the event stays
    # pending instead of being dropped or marked sent.
    _share_watch_breach_episode(refresh)
    assert len(notifier.calls) == 1
    assert notifier.successes == []
    state = service.lp_share_watch_state()["condition-a"]
    assert state["notification_pending"] is True
    assert state["notification_sent"] is not True

    # A later fresh sample retries the same pending event and succeeds
    # exactly once; the spent episode does not repeat.
    notifier.fail = False
    refresh(80, "8.6")
    assert len(notifier.successes) == 1
    refresh(90, "8.7")
    assert len(notifier.successes) == 1


def test_lp_share_watch_recovery_clears_pending_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, _default_notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="pending-recovery", include_second_market=False
    )
    notifier = _ShareWatchNotifier(fail=True)
    service._notifier = notifier
    refresh = _share_watch_refresh(service, account, clock, base)

    # A confirmed minute fails to deliver and stays pending.
    _share_watch_breach_episode(refresh)
    assert notifier.successes == []
    state = service.lp_share_watch_state()["condition-a"]
    assert state["notification_pending"] is True

    # Returning to the target band clears the pending event before it is ever
    # delivered; the stale event is not replayed on later samples.
    refresh(80, "6.0")
    state = service.lp_share_watch_state()["condition-a"]
    assert state["notification_pending"] is False
    assert state["notification_sent"] is False
    notifier.fail = False
    refresh(90, "8.5")
    refresh(100, "8.5")
    assert notifier.successes == []

    # A brand-new episode still needs its own complete fresh minute.
    for seconds in (110, 120, 130, 140):
        refresh(seconds, "8.5")
    assert notifier.successes == []
    refresh(150, "8.6")
    assert len(notifier.successes) == 1


def test_lp_share_watch_repeated_or_older_source_never_completes_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, account, notifier, clock, base = _share_watch_fixture(
        tmp_path, monkeypatch, name="source-dedupe", include_second_market=False
    )
    refresh = _share_watch_refresh(service, account, clock, base)

    # Fresh samples open a window at source t0.
    for seconds in (0, 10, 20, 30, 40):
        refresh(seconds, "8.5")

    # Repeating the same checked_at (wall 50/60, source t=40) and replaying
    # an older stamp (wall 70, source t=20) must never accumulate toward the
    # minute: the breach evidence is discarded instead.
    refresh(50, "8.5", checked_at=base + timedelta(seconds=40))
    refresh(60, "8.5", checked_at=base + timedelta(seconds=40))
    refresh(70, "8.5", checked_at=base + timedelta(seconds=20))
    assert notifier.successes == []
    state = service.lp_share_watch_state()["condition-a"]
    assert state["breach_started_at"] is None
    assert state["notification_pending"] is False

    # A fresh advanced source restarts the window; a complete minute is
    # required again before the alert fires.
    for seconds in (80, 90, 100, 110, 120, 130):
        refresh(seconds, "8.5")
    assert notifier.successes == []
    refresh(140, "8.6")
    assert len(notifier.successes) == 1



def test_lp_dashboard_share_failures_preserve_unknown_without_order_writes(
    tmp_path: Path,
) -> None:
    events = (
        "initial",
        "repeat",
        "older_replay",
        "error",
        "replay_after_failure",
        "missing",
        "wrong_maker",
        "missing_identity",
        "old",
        "future",
        "fresh_8",
        "fresh_5",
    )

    class Account:
        wallet_address = "wallet"

        def __init__(self) -> None:
            self.share_reads = 0
            self.first_checked_at: datetime | None = None
            self.order_writes = 0
            self.cancellations = 0
            self.resizes = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "wallet_address": self.wallet_address,
                "open_orders": (
                    {
                        "order_id": "c-1",
                        "condition_id": "condition-c",
                        "token_id": "c-yes",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("12"),
                        "size_matched": Decimal("7"),
                    },
                ),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return order_id == "c-1"

        def lp_reward_percentages(self) -> dict[str, object]:
            event = events[min(self.share_reads, len(events) - 1)]
            self.share_reads += 1
            if event == "error":
                raise RuntimeError("transport failure")
            if event == "missing":
                checked_at = datetime.now(UTC)
                percentages: Mapping[str, object] = {}
                maker_address = self.wallet_address
            elif event == "wrong_maker":
                checked_at = datetime.now(UTC)
                percentages = {"condition-c": Decimal("8")}
                maker_address = "other-wallet"
            elif event == "old":
                checked_at = datetime.now(UTC) - timedelta(seconds=181)
                percentages = {"condition-c": Decimal("8")}
                maker_address = self.wallet_address
            elif event == "future":
                checked_at = datetime.now(UTC) + timedelta(seconds=5)
                percentages = {"condition-c": Decimal("8")}
                maker_address = self.wallet_address
            elif event == "repeat":
                assert self.first_checked_at is not None
                checked_at = self.first_checked_at
                percentages = {"condition-c": Decimal("8")}
                maker_address = self.wallet_address
            elif event == "replay_after_failure":
                assert self.first_checked_at is not None
                checked_at = self.first_checked_at
                percentages = {"condition-c": Decimal("8")}
                maker_address = self.wallet_address
            elif event == "older_replay":
                assert self.first_checked_at is not None
                checked_at = self.first_checked_at - timedelta(seconds=1)
                percentages = {"condition-c": Decimal("5")}
                maker_address = self.wallet_address
            elif event == "missing_identity":
                checked_at = datetime.now(UTC)
                percentages = {"condition-c": Decimal("8")}
                maker_address = "wallet"
            else:
                checked_at = datetime.now(UTC)
                value = Decimal("5") if event == "fresh_5" else Decimal("8")
                percentages = {"condition-c": value}
                maker_address = self.wallet_address
                if event == "initial":
                    self.first_checked_at = checked_at
            return {
                "state": "known",
                "scope": "account",
                "maker_address": maker_address,
                "percentages": percentages,
                "checked_at": checked_at,
            }

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

        def post_order(self, _order: object) -> object:
            self.order_writes += 1
            return object()

        def cancel_orders(self, **_kwargs: object) -> object:
            self.cancellations += 1
            return object()

        def resize_order(self, **_kwargs: object) -> object:
            self.resizes += 1
            return object()

    class Notifier:
        def __init__(self) -> None:
            self.calls = 0

        def notify(self, **_kwargs: object) -> None:
            self.calls += 1

    service, _trading, store, monitor = execution_fixture(tmp_path)
    account = Account()
    notifier = Notifier()
    service._trading = account
    service._notifier = notifier
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "scanning": False,
            "candidates": [],
            "recommendations": [],
            "checked_at": checked_at,
            "last_success_at": checked_at,
            "last_attempt_at": checked_at,
            "candidate_rows_fresh": True,
            "catalog_complete": True,
            "missing_metadata_condition_ids": [],
            "missing_book_token_ids": [],
        }
    )
    store.lp_create_session(
        "session-c",
        "idempotency-c",
        state="entry_open",
        payload={
            "condition_id": "condition-c",
            "token_id": "c-yes",
            "entry_order_id": "c-1",
            "market_title": "Protected market",
            "outcome": "YES",
        },
    )
    service._lp = PolymarketLPService(
        store, object(), clock=lambda: datetime.now(UTC)
    )
    clock = [0.0]
    service._clock = lambda: clock[0]
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor
    runtime.execution = service

    def read_dashboard(base: str) -> dict[str, object]:
        # Issue #146: the route serves the background snapshot, so run the
        # snapshot pipeline (joining the reward worker to own the pipeline
        # lock) and serve the result the way the route does.
        worker = getattr(service, "_lp_reward_refresh_thread", None)
        if worker is not None:
            worker.join(timeout=2)
        payload = prediction_service._lp_projection_safe_value(
            service.refresh_lp_dashboard_snapshot()
        )
        assert payload["stale"] is False
        order = payload["orders"][0]
        assert order["remaining_quantity"] == "5"
        assert order["management"] == "system_managed"
        assert payload["lp_session"]["state"] == "entry_open"
        return payload

    with _server(runtime) as base:
        clock[0] += 61
        first = read_dashboard(base)
        first_share = first["reward_shares"]["condition-c"]
        assert first_share["state"] == "known"
        assert first_share["percentage"] == "8"
        assert first_share["target_status"] == "target"
        assert first_share["target_delta_percentage_points"] == "0"
        assert first_share["historical"] is False
        assert first_share["delta_percentage_points"] is None
        first_checked_at = first_share["checked_at"]

        clock[0] += 61
        repeated = read_dashboard(base)["reward_shares"]["condition-c"]
        assert repeated["state"] == "known"
        assert repeated["percentage"] == "8"
        assert repeated["target_status"] == "target"
        assert repeated["target_delta_percentage_points"] == "0"
        assert repeated["checked_at"] == first_checked_at
        assert repeated["delta_percentage_points"] is None

        clock[0] += 61
        older_replay = read_dashboard(base)["reward_shares"]["condition-c"]
        assert older_replay["state"] == "unknown"
        assert older_replay["percentage"] == "8"
        assert older_replay["historical"] is True
        assert older_replay["severity"] == "unknown"
        assert older_replay["checked_at"] == first_checked_at
        assert older_replay["delta_percentage_points"] is None
        assert older_replay["reason"] == "reward_share_replay_older"

        clock[0] += 61
        failed = read_dashboard(base)["reward_shares"]["condition-c"]
        assert failed["state"] == "unknown"
        assert failed["percentage"] == "8"
        assert failed["historical"] is True
        assert failed["severity"] == "unknown"
        assert failed["delta_percentage_points"] is None
        assert failed["checked_at"] == first_checked_at
        assert failed["reason"] == "reward_share_unknown"

        clock[0] += 61
        replay_after_failure = read_dashboard(base)["reward_shares"]["condition-c"]
        assert replay_after_failure["state"] == "unknown"
        assert replay_after_failure["percentage"] == "8"
        assert replay_after_failure["historical"] is True
        assert replay_after_failure["severity"] == "unknown"
        assert replay_after_failure["delta_percentage_points"] is None
        assert replay_after_failure["checked_at"] == first_checked_at
        assert replay_after_failure["reason"] == "reward_share_replay_same"

        clock[0] += 61
        missing = read_dashboard(base)["reward_shares"]["condition-c"]
        assert missing["state"] == "unknown"
        assert missing["percentage"] == "8"
        assert missing["historical"] is True
        assert missing["reason"] == "reward_share_unknown"

        clock[0] += 61
        wrong_maker = read_dashboard(base)["reward_shares"]["condition-c"]
        assert wrong_maker["state"] == "unknown"
        assert wrong_maker["percentage"] == "8"
        assert wrong_maker["historical"] is True
        assert wrong_maker["reason"] == "reward_share_unknown"

        account.wallet_address = None  # type: ignore[assignment]
        clock[0] += 61
        missing_identity = read_dashboard(base)["reward_shares"]["condition-c"]
        assert missing_identity["state"] == "unknown"
        assert missing_identity["percentage"] == "8"
        assert missing_identity["historical"] is True
        assert missing_identity["reason"] == "reward_share_unknown"
        account.wallet_address = "wallet"

        clock[0] += 61
        stale = read_dashboard(base)["reward_shares"]["condition-c"]
        assert stale["state"] == "unknown"
        assert stale["percentage"] == "8"
        assert stale["historical"] is True
        assert stale["reason"] == "reward_share_stale"
        assert stale["checked_at"] == first_checked_at

        clock[0] += 61
        future = read_dashboard(base)["reward_shares"]["condition-c"]
        assert future["state"] == "unknown"
        assert future["percentage"] == "8"
        assert future["historical"] is True
        assert future["reason"] == "reward_share_stale"
        assert future["checked_at"] == first_checked_at

        clock[0] += 61
        recovered_warning = read_dashboard(base)["reward_shares"]["condition-c"]
        assert recovered_warning["state"] == "known"
        assert recovered_warning["percentage"] == "8"
        assert recovered_warning["target_status"] == "target"
        assert recovered_warning["target_delta_percentage_points"] == "0"
        assert recovered_warning["historical"] is False
        assert recovered_warning["delta_percentage_points"] == "0"
        assert recovered_warning["checked_at"] != first_checked_at

        clock[0] += 61
        recovered_normal = read_dashboard(base)["reward_shares"]["condition-c"]
        assert recovered_normal["state"] == "known"
        assert recovered_normal["percentage"] == "5"
        assert recovered_normal["target_status"] == "target"
        assert recovered_normal["target_delta_percentage_points"] == "0"
        assert recovered_normal["delta_percentage_points"] == "-3"

    assert account.share_reads == len(events)
    assert account.order_writes == 0
    assert account.cancellations == 0
    assert account.resizes == 0
    assert notifier.calls == 0

    source_checked_at = datetime.now(UTC).replace(microsecond=0)
    source_checked_text = source_checked_at.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    restart_condition = "condition-restart"

    class RestartAccount:
        def __init__(self, wallet: str, *, fail_percentage: bool = False) -> None:
            self.wallet_address = wallet
            self.config = SimpleNamespace(wallet_address=wallet)
            self.fail_percentage = fail_percentage
            self.share_reads = 0
            self.order_writes = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "wallet_address": self.wallet_address,
                "open_orders": (
                    {
                        "order_id": "restart-order",
                        "condition_id": restart_condition,
                        "token_id": "restart-yes",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("10"),
                        "size_matched": Decimal("0"),
                    },
                ),
                "positions": (),
                "checked_at": source_checked_at,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return order_id == "restart-order"

        def lp_reward_percentages(self) -> dict[str, object]:
            self.share_reads += 1
            if self.fail_percentage:
                raise RuntimeError("percentage read unavailable")
            return {
                "state": "known",
                "scope": "account",
                "maker_address": self.wallet_address,
                "percentages": {restart_condition: Decimal("8.5")},
                "checked_at": source_checked_at,
            }

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

        def post_order(self, _order: object) -> object:
            self.order_writes += 1
            return object()

        def cancel_orders(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

    restart_account = RestartAccount("wallet-restart-a")
    restart_service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=restart_account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-restart-a.lock",
        lp=PolymarketLPService(store, restart_account),
    )
    restart_service._breaker_open = False
    restart_runtime = _Runtime()
    restart_runtime.mode = "production"
    restart_runtime.state = "RUNNING"
    restart_runtime.production_owner = True
    restart_runtime.store = store  # type: ignore[assignment]
    restart_runtime.monitor = monitor
    restart_runtime.execution = restart_service

    # The monitored set is derived from unfinished open orders; the restart
    # condition holds a live order, so a plain refresh persists its share.
    refreshed = restart_service.refresh_lp_share_watch()
    assert refreshed["state"] == "ready"
    restart_account_id = restart_service._lp_account_id()
    assert restart_account_id is not None
    saved_restart = store.lp_observations(restart_account_id)[restart_condition]
    assert saved_restart["share_alert"]["last_share_percentage"] == "8.5"
    assert saved_restart["share_alert"]["last_share_checked_at"] == source_checked_text

    restarted_account = RestartAccount("wallet-restart-a", fail_percentage=True)
    restarted_service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=restarted_account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-restarted-a.lock",
        lp=PolymarketLPService(store, restarted_account),
    )
    restarted_runtime = _Runtime()
    restarted_runtime.store = store  # type: ignore[assignment]
    restarted_runtime.monitor = monitor
    restarted_runtime.execution = restarted_service

    with _server(restarted_runtime) as base:
        # Issue #146: run one snapshot pass so the payload carries the
        # restart condition (see the matching fix on runtime_b below).
        restarted_service.refresh_lp_dashboard_snapshot()
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200
        share = payload["reward_shares"][restart_condition]
        assert share["state"] == "unknown"
        assert share["historical"] is True
        assert share["percentage"] == "8.5"
        assert share["checked_at"] == source_checked_text
        assert share["last_success_at"] == source_checked_text
        assert share["target_status"] == "unknown"
        assert share["stale"] is True

    account_b = RestartAccount("wallet-restart-b", fail_percentage=True)
    service_b = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=account_b,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-restart-b.lock",
        lp=PolymarketLPService(store, account_b),
    )
    runtime_b = _Runtime()
    runtime_b.store = store  # type: ignore[assignment]
    runtime_b.monitor = monitor
    runtime_b.execution = service_b
    with _server(runtime_b) as base:
        # Issue #146: the route serves the background snapshot; run one
        # snapshot pass so the payload carries the restart condition.
        service_b.refresh_lp_dashboard_snapshot()
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200
        share_b = payload["reward_shares"][restart_condition]
        assert share_b["state"] == "unknown"
        assert share_b["historical"] is False
        assert share_b["percentage"] is None
        assert share_b["checked_at"] is None


def test_lp_reward_share_survives_background_reward_refresh(tmp_path: Path) -> None:
    condition_id = "condition-background"
    token_id = "background-yes"
    source_checked_at = datetime.now(UTC).replace(microsecond=0)
    source_checked_text = source_checked_at.isoformat().replace("+00:00", "Z")
    reward_started = threading.Event()
    reward_release = threading.Event()
    counts = {"percentage": 0, "market": 0}
    counts_lock = threading.Lock()

    class Account:
        wallet_address = "wallet"
        config = SimpleNamespace(wallet_address="wallet")

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "wallet_address": self.wallet_address,
                "checked_at": datetime.now(UTC),
                "open_orders": (
                    {
                        "order_id": "background-order",
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.40"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                    },
                ),
                "positions": (
                    {
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "outcome": "YES",
                        "size": Decimal("5"),
                    },
                ),
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, *, order_id: str) -> bool:
            return order_id == "background-order"

        def lp_reward_percentages(self) -> dict[str, object]:
            with counts_lock:
                counts["percentage"] += 1
            return {
                "state": "known",
                "scope": "account",
                "maker_address": self.wallet_address,
                "percentages": {condition_id: Decimal("7.5")},
                "checked_at": source_checked_at,
            }

        def lp_reward_snapshot(
            self,
            reward_date: str,
            market: str,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            assert market == condition_id
            with counts_lock:
                counts["market"] += 1
            reward_started.set()
            if stop_event is not None and stop_event.is_set():
                return {
                    "state": "unknown",
                    "reward_date": reward_date,
                    "condition_id": market,
                }
            if not reward_release.wait(timeout=30):
                raise AssertionError("held reward read was not released")
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": market,
                "market_amount": Decimal("0.80"),
                "market_amount_raw": Decimal("0.80"),
                "market_asset": "USDC.e",
            }

        def lp_reward_snapshots(
            self,
            reward_date: str,
            condition_ids: object,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            return {
                str(market): self.lp_reward_snapshot(
                    reward_date, str(market), stop_event=stop_event
                )
                for market in condition_ids  # type: ignore[union-attr]
            }

        def create_limit_order(self, **_kwargs: object) -> object:
            self.order_writes += 1
            return object()

        def post_order(self, _order: object) -> object:
            self.order_writes += 1
            return object()

        def cancel_orders(self, **_kwargs: object) -> object:
            self.cancellations += 1
            return object()

        def resize_order(self, **_kwargs: object) -> object:
            self.resizes += 1
            return object()

        order_writes = 0
        cancellations = 0
        resizes = 0

    class CounterNotifier(NullNotifier):
        def __init__(self) -> None:
            self.calls = 0

        def notify(self, _title: str, _message: str) -> None:
            self.calls += 1

    service, _trading, store, monitor = execution_fixture(tmp_path)
    account = Account()
    notifier = CounterNotifier()
    service._trading = account
    service._notifier = notifier
    store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "scanning": False,
            "candidates": [],
            "recommendations": [],
            "market_rewards": {},
            "checked_at": source_checked_text,
            "last_success_at": source_checked_text,
            "last_attempt_at": source_checked_text,
            "candidate_rows_fresh": True,
            "catalog_complete": True,
            "missing_metadata_condition_ids": [],
            "missing_book_token_ids": [],
        }
    )
    service._lp = PolymarketLPService(store, account)
    service._clock = lambda: 0.0
    runtime = _Runtime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor
    runtime.execution = service

    def read_dashboard(base: str) -> dict[str, object]:
        # Issue #146: the route serves the background snapshot; run the
        # snapshot pipeline first (this is what schedules the reward worker).
        service.refresh_lp_dashboard_snapshot()
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard", timeout=5)
        assert status == 200
        return payload

    def assert_projection(payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        assert set(payload["reward_shares"]) == {condition_id}
        share = payload["reward_shares"][condition_id]
        assert share["state"] == "known"
        assert share["percentage"] == "7.5"
        assert share["reference_share_percentage"] == "5"
        assert share["target_status"] == "target"
        assert share["target_delta_percentage_points"] == "0"
        assert share["delta_percentage_points"] is None
        assert share["stale"] is False
        assert share["historical"] is False
        order = payload["orders"][0]
        assert order["condition_id"] == condition_id
        assert order["remaining_quantity"] == "15"
        position = payload["positions"][0]
        assert position["condition_id"] == condition_id
        return share, order

    with _server(runtime) as base:
        with ThreadPoolExecutor(max_workers=4) as clients:
            first_future = clients.submit(read_dashboard, base)
            try:
                assert reward_started.wait(timeout=5), "market reward refresh did not start"
                try:
                    first = first_future.result(timeout=2)
                except TimeoutError as exc:
                    raise AssertionError(
                        "LP dashboard HTTP read blocked on market reward refresh"
                    ) from exc
                first_share, _first_order = assert_projection(first)
                first_reward = first["market_rewards"][condition_id]
                assert first_reward["state"] == "unknown"
                assert first_reward["stale"] is True

                repeated_future = clients.submit(read_dashboard, base)
                try:
                    repeated = repeated_future.result(timeout=2)
                except TimeoutError as exc:
                    raise AssertionError(
                        "repeated LP dashboard HTTP read blocked on market reward refresh"
                    ) from exc
                repeated_share, _repeated_order = assert_projection(repeated)
                assert repeated_share["checked_at"] == first_share["checked_at"]
                assert repeated_share["percentage"] == first_share["percentage"]
                assert repeated_share["delta_percentage_points"] == first_share["delta_percentage_points"]
                with counts_lock:
                    assert counts["percentage"] == 1
                    assert counts["market"] == 1
            finally:
                reward_release.set()

            deadline = time.monotonic() + 5
            while True:
                final = read_dashboard(base)
                final_reward = final["market_rewards"].get(condition_id)
                if (
                    isinstance(final_reward, Mapping)
                    and final_reward.get("state") == "known"
                    and final_reward.get("market_amount") == "0.80"
                ):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("background market reward refresh did not publish 0.80")
                time.sleep(0.05)
            final_share, final_order = assert_projection(final)
            assert final_share["checked_at"] == first_share["checked_at"]
            assert final_share["delta_percentage_points"] == first_share["delta_percentage_points"]
            assert final_order["remaining_quantity"] == "15"

    with counts_lock:
        assert counts["percentage"] == 1
        assert counts["market"] == 1
    assert account.order_writes == 0
    assert account.cancellations == 0
    assert account.resizes == 0
    assert notifier.calls == 0

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
        "selected_reward_requests": [],
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

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool
        ) -> list[object]:
            requests = public_state["selected_reward_requests"]
            assert isinstance(requests, list)
            requests.append((condition_id, sponsored))
            if public_state["catalog_fail"] is True:
                raise RuntimeError("synthetic reward catalog failure")
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

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
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

    class HistoryResponse:
        def __init__(self, payload: Mapping[str, object]) -> None:
            self.payload = payload

        def __enter__(self) -> "HistoryResponse":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def open_history(request: object, **_kwargs: object) -> HistoryResponse:
        assert isinstance(request, Request)
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        token_ids = tuple(body["markets"])
        start_ts = int(body["start_ts"])
        end_ts = int(body["end_ts"])
        return HistoryResponse(
            {
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                }
            }
        )

    sdk = AccountSDK()
    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        client=sdk,
        urlopen_fn=open_history,
        public_client_factory=PublicMarketSDK,
    )
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, trading, clock=lambda: clock_state["now"])
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"
    AdapterDateTime.calls = 0
    trading.expire_lp_metadata_cache()
    scanned = lp.refresh_candidates(force=True)
    assert scanned["state"] == "ready"
    assert scanned["complete"] is True
    # Issue #143 repair 2: receipts cached with the 200-second offset (the
    # prepared reward catalog and the first account read) are renewed once,
    # targeted at the scanned condition, and the market is judged live and
    # published; the preview path below still rechecks fresh facts directly.
    assert scanned["funnel"]["checked"] == 1
    assert scanned["funnel"]["passed"] == 1
    assert scanned["funnel"]["unknown"] == 0
    assert [row["condition_id"] for row in scanned["candidates"]] == [condition_id]
    assert len(scanned["recommendations"]) == 1
    assert public_state["selected_reward_requests"] == [
        (condition_id, False),
        (condition_id, True),
    ]
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
        # Issue #146: the route serves the background snapshot, so run the
        # snapshot pipeline before serving.
        execution.refresh_lp_dashboard_snapshot()
        dashboard_status, dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert dashboard_status == 200
        candidate_rows = dashboard["candidates"]
        assert isinstance(candidate_rows, list)
        assert dashboard["complete"] is True
        # Issue #143 repair 2: the scan renewed the stale account receipt and
        # published its live-qualified passer; the preview below still
        # rechecks fresh facts before confirming.
        assert [row["condition_id"] for row in candidate_rows] == [condition_id]
        assert dashboard["candidate_stale"] is False
        preview_status, preview = candidate_preview(base)
        assert preview_status == 200
        assert preview["state"] == "previewed"
        assert preview["request"]["price"] == "0.51"
        assert preview["request"]["quantity"] == "20"
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
        trading.expire_lp_metadata_cache()
        zero_score_status, zero_score = confirm_preview(
            base, score_preview["preview_id"]
        )
        assert zero_score_status == 200
        assert zero_score["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []

        public_state["max_spread"] = Decimal("10")
        trading.expire_lp_metadata_cache()
        size_preview_status, size_preview = candidate_preview(base)
        assert size_preview_status == 200
        assert size_preview["request"]["quantity"] == "20"
        public_state["reward_min_size"] = Decimal("19")
        trading.expire_lp_metadata_cache()
        stale_size_status, stale_size = confirm_preview(base, size_preview["preview_id"])
        assert stale_size_status == 200
        assert stale_size["state"] == "rejected"
        assert sdk.limit_orders == []
        assert sdk.posts == []
        resized_status, resized = candidate_preview(base)
        assert resized_status == 200
        assert resized["request"]["quantity"] == "19"
        public_state["reward_min_size"] = Decimal("20")
        trading.expire_lp_metadata_cache()

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
        trading.expire_lp_metadata_cache()
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
    failed_preparation = lp.refresh_price_history()
    assert failed_preparation["state"] == "unknown"
    failed_scan = lp.candidate_snapshot()
    # Issue #157: no 60s/whole-snapshot staleness — but the estimate aged
    # past its own five-minute validity, so the pool is honestly empty.
    assert failed_scan["stale"] is False
    assert failed_scan["checked_at"] == previous_checked_at
    assert failed_scan["last_success_at"] == previous_scan["last_success_at"]
    assert failed_scan["candidates"] == []
    assert public_state["public_creates"] == public_state["public_closes"]


def test_lp_trial_reads_share_current_refresh_and_ignore_late_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "condition-concurrent"
    market = _lp_test_market(
        condition_id,
        yes_token="concurrent-yes",
        no_token="concurrent-no",
        reward_min_size=Decimal("90"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    assert service.refresh_price_history()["state"] == "known"
    first = service.refresh_candidates(force=True)
    assert first["recommendations"]
    initial_reads = len(state["metadata_requests"])

    entered = threading.Event()
    release = threading.Event()
    original_books = trading.lp_order_books
    book_calls: list[tuple[str, ...]] = []

    def blocked_books(token_ids: object, *, stop_event: object = None):
        del stop_event
        batch = tuple(token_ids)  # type: ignore[arg-type]
        book_calls.append(batch)
        entered.set()
        assert release.wait(timeout=5)
        return original_books(batch)

    trading.lp_order_books = blocked_books  # type: ignore[method-assign]
    clock["now"] += timedelta(seconds=61)
    maintenance = threading.Thread(
        target=service.refresh_candidate_recommendations,
        name="issue142-maintenance",
        daemon=True,
    )
    maintenance.start()
    assert entered.wait(timeout=5)

    execution = PredictionExecutionService(
        store=store,
        monitor=_Monitor(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
        lp=service,
    )
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
    )
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "issue142-test"},
    ) as (base, _server_instance):
        # Issue #146: the route serves the background snapshot, so publish
        # one before the concurrent page polls.
        execution.refresh_lp_dashboard_snapshot()
        with ThreadPoolExecutor(max_workers=3) as clients:
            responses = list(
                clients.map(
                    lambda _index: _response(
                        base + "/api/prediction-arbitrage/lp/dashboard",
                        timeout=2,
                    ),
                    range(3),
                )
            )
        assert all(status == 200 for status, _payload in responses)
        assert all(payload["selected_results"] for _status, payload in responses)
    release.set()
    maintenance.join(timeout=5)
    assert not maintenance.is_alive()
    assert len(book_calls) == 1
    assert state["trade_writes"] == []
    # Maintenance refreshes the expired metadata once before its selected
    # book read; concurrent dashboard reads must not trigger another read.
    assert len(state["metadata_requests"]) == initial_reads + 1

    # Exercise the cross-service stale-writer boundary directly: an older
    # maintenance response is held after its source read, then a newer normal
    # candidate round publishes a changed direction before the old response is
    # released.
    trading.lp_order_books = original_books  # type: ignore[method-assign]
    late_store = PredictionArbitrageStore(tmp_path / "late-publication")
    late_initial = PolymarketLPService(
        late_store, trading, clock=lambda: clock["now"]
    )
    assert late_initial.refresh_price_history()["state"] == "known"
    # Issue #146: drop the trading-level shared account cache so the scan
    # reads an account stamped on the test's clock timeline.
    trading._lp_account_shared_cache = None
    late_scan = late_initial.refresh_candidates(force=True)
    assert late_scan["recommendations"], (
        late_scan.get("funnel"), late_scan.get("state"),
        late_initial._candidate_qualification_facts,
    )

    old_entered = threading.Event()
    old_release = threading.Event()
    old_payloads: list[dict[str, object]] = []

    class DelayedExchange:
        def __getattr__(self, name: str) -> object:
            return getattr(trading, name)

        def lp_order_books(self, token_ids: object, *, stop_event: object = None):
            del stop_event
            batch = tuple(token_ids)  # type: ignore[arg-type]
            payload = original_books(batch)
            old_payloads.append(dict(payload))
            old_entered.set()
            assert old_release.wait(timeout=5)
            return payload

    # Keep the in-memory qualification facts from the completed normal round;
    # a new service instance would correctly have no maintenance scope yet.
    old_service = late_initial
    old_service.exchange = DelayedExchange()  # type: ignore[assignment]
    clock["now"] += timedelta(seconds=61)
    old_result: list[dict[str, object]] = []

    def run_old_maintenance() -> None:
        old_result.append(old_service.refresh_candidate_recommendations())

    old_thread = threading.Thread(
        target=run_old_maintenance,
        name="issue142-old-publication",
        daemon=True,
    )
    old_thread.start()
    assert old_entered.wait(timeout=5)
    assert old_payloads

    state["markets"][condition_id]["yes_bid"] = Decimal("0.45")  # type: ignore[index]
    clock["now"] += timedelta(seconds=1)
    newer_service = PolymarketLPService(
        late_store, trading, clock=lambda: clock["now"]
    )
    assert newer_service.refresh_price_history()["state"] == "known"
    trading._lp_account_shared_cache = None
    newer = newer_service.refresh_candidates(force=True)
    assert newer["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    newer_checked_at = newer["checked_at"]

    old_release.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    assert len(old_result) == 1
    assert old_result[0]["checked_at"] == newer_checked_at
    # Issue #146: the stale maintenance publication loses to the newer scan
    # round (in-process generation guard, persistence arbiter across
    # service instances). The response carries the newer round's head row;
    # its recommendation is recomputed against this service's expired local
    # facts and is therefore empty instead of falsely live.
    assert old_result[0]["candidates"][0]["selected_direction"]["outcome"] == "NO"
    # Issue #157: the late maintenance re-estimated the stored row and its
    # judgment is the newest one this instance holds, so the recommendation
    # is kept; the durable pool on the shared store follows the save
    # arbiter (newest publish wins).
    assert old_result[0]["recommendations"][0]["condition_id"] == condition_id
    assert old_service.candidate_snapshot()["checked_at"] == newer_checked_at
    assert state["trade_writes"] == []


def test_lp_maintenance_refreshes_at_thirty_second_source_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A1: maintenance fires on the oldest source age (30s lead),
    well before the 60-second source expiry."""
    condition_id = "condition-lead"
    market = _lp_test_market(
        condition_id,
        yes_token="lead-yes",
        no_token="lead-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    # Tie the trading client's shared-snapshot TTL clock to the injected
    # service clock so TTL expiry follows the scenario timeline.
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    scanned = service.refresh_candidates(force=True)
    assert scanned["recommendations"]
    t0 = clock["now"]

    account_reads: list[datetime] = []
    original_account = trading.lp_account_snapshot

    def counting_account() -> dict[str, object]:
        account_reads.append(clock["now"])
        return original_account()  # type: ignore[no-any-return]

    trading.lp_account_snapshot = counting_account  # type: ignore[method-assign]
    original_books = trading.lp_order_books
    book_reads: list[tuple[str, ...]] = []

    def counting_books(token_ids: object, *, stop_event: object = None) -> object:
        del stop_event
        batch = tuple(token_ids)  # type: ignore[arg-type]
        book_reads.append(batch)
        return original_books(batch)

    trading.lp_order_books = counting_books  # type: ignore[method-assign]
    metadata_before = len(state["metadata_requests"])
    reward_before = len(state["selected_reward_requests"])

    # At 29 seconds of source age the maintenance call is a pure snapshot
    # read: zero external reads, published rows unchanged.
    clock["now"] = t0 + timedelta(seconds=29)
    untouched = service.refresh_candidate_recommendations()
    assert untouched["recommendations"] == scanned["recommendations"]
    assert account_reads == []
    assert book_reads == []
    assert len(state["metadata_requests"]) == metadata_before
    assert len(state["selected_reward_requests"]) == reward_before

    # One second past the lead boundary the due sources are re-read and the
    # refreshed facts carry the newer receipt times.
    clock["now"] = t0 + timedelta(seconds=31)
    lead_refreshed = service.refresh_candidate_recommendations()
    assert lead_refreshed["recommendations"]
    assert account_reads == [t0 + timedelta(seconds=31)]
    assert book_reads and set(book_reads[-1]) == {"lead-yes", "lead-no"}
    assert len(state["metadata_requests"]) == metadata_before + 1
    assert len(state["selected_reward_requests"]) >= reward_before + 1
    refreshed_head = lead_refreshed["recommendations"][0]
    assert refreshed_head["realtime_checked_at"] == (
        (t0 + timedelta(seconds=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def test_candidate_source_expiry_boundary_stays_at_sixty_seconds() -> None:
    """Issue #146 A1: the 60-second source expiry itself is unchanged."""

    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert (
        _candidate_source_expired(now - timedelta(seconds=59), now) is False
    )
    assert _candidate_source_expired(now - timedelta(seconds=61), now) is True


def test_lp_maintenance_backoff_schedule_and_wait_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A4: maintenance failures back off 60/120/300 seconds by
    attempt-finish time and every scheduler wait clamps to [1.0, 300.0]."""
    condition_id = "condition-backoff"
    market = _lp_test_market(
        condition_id,
        yes_token="backoff-yes",
        no_token="backoff-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_candidates(force=True)["recommendations"]
    t0 = clock["now"]

    state["metadata_error"] = True
    clock["now"] = t0 + timedelta(seconds=31)
    failed = service.refresh_candidate_recommendations()
    # Issue #157: the failed re-estimate keeps the stored row.
    assert failed["recommendations"][0]["condition_id"] == condition_id
    assert failed["candidates"][0]["refresh_failed"] is True
    assert failed["candidates"][0]["condition_id"] == condition_id
    assert failed["maintenance_consecutive_failures"] == 1
    first_failure_at = clock["now"]
    assert service.candidate_maintenance_wait_seconds() == pytest.approx(
        60.0, abs=1e-6
    )

    # Inside the 60-second backoff window the maintenance call is a pure
    # snapshot read: no external retries.
    metadata_reads = len(state["metadata_requests"])
    clock["now"] = first_failure_at + timedelta(seconds=59)
    retried_too_soon = service.refresh_candidate_recommendations()
    assert retried_too_soon["maintenance_consecutive_failures"] == 1
    assert len(state["metadata_requests"]) == metadata_reads
    assert service.candidate_maintenance_wait_seconds() == pytest.approx(
        1.0, abs=1e-6
    )

    # Backoff elapsed: the retry runs, fails again, and the wait doubles.
    clock["now"] = first_failure_at + timedelta(seconds=61)
    second_failure = service.refresh_candidate_recommendations()
    assert second_failure["maintenance_consecutive_failures"] == 2
    assert service.candidate_maintenance_wait_seconds() == pytest.approx(
        120.0, abs=1e-6
    )
    clock["now"] = clock["now"] + timedelta(seconds=121)
    third_failure = service.refresh_candidate_recommendations()
    assert third_failure["maintenance_consecutive_failures"] == 3
    assert service.candidate_maintenance_wait_seconds() == pytest.approx(
        300.0, abs=1e-6
    )
    # Three or more consecutive failures hold at 300 seconds.
    assert service.candidate_maintenance_wait_seconds() == pytest.approx(
        300.0, abs=1e-6
    )

    # Issue #157: past the +300s row expiry the stored estimate ages out,
    # so the maintenance ladder holds (nothing left to maintain) until the
    # exploration repopulates the pool.
    clock["now"] = clock["now"] + timedelta(seconds=301)
    fourth = service.refresh_candidate_recommendations()
    assert fourth["maintenance_consecutive_failures"] == 3
    # Nothing left to maintain: the scheduler reports None.
    assert service.candidate_maintenance_wait_seconds() is None

    # Any success resets the backoff to the 30-second lead cadence.  The
    # exploration repopulates the pool first (issue #157), then the
    # maintenance refresh on the republished row clears the counter.
    state["metadata_error"] = False
    clock["now"] = clock["now"] + timedelta(seconds=901)
    republished = service.refresh_candidates(force=True)
    assert republished["recommendations"]
    # Past the 30-second source lead the maintenance fires again.
    clock["now"] = clock["now"] + timedelta(seconds=31)
    recovered = service.refresh_candidate_recommendations()
    assert recovered["recommendations"]
    assert recovered["maintenance_consecutive_failures"] == 0
    assert recovered["maintenance_next_attempt_at"] is None
    recovered_wait = service.candidate_maintenance_wait_seconds()
    assert recovered_wait is not None and 29.0 <= recovered_wait <= 30.0

    # Degenerate source stamps clamp the wait to the 1.0-second floor.
    facts = service._candidate_qualification_facts[condition_id]
    facts["account"]["checked_at"] = "not-a-timestamp"
    assert service.candidate_maintenance_wait_seconds() == 1.0
    facts["account"]["checked_at"] = clock["now"] + timedelta(hours=1)
    assert service.candidate_maintenance_wait_seconds() == 1.0


def test_lp_same_kind_refreshes_do_not_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A2: concurrent same-kind refreshes never read twice."""
    condition_id = "condition-overlap"
    market = _lp_test_market(
        condition_id,
        yes_token="overlap-yes",
        no_token="overlap-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_candidates(force=True)["recommendations"]

    entered = threading.Event()
    release = threading.Event()
    original_books = trading.lp_order_books
    book_calls: list[tuple[str, ...]] = []

    def blocked_books(token_ids: object, *, stop_event: object = None) -> object:
        del stop_event
        batch = tuple(token_ids)  # type: ignore[arg-type]
        book_calls.append(batch)
        entered.set()
        assert release.wait(timeout=5)
        return original_books(batch)

    trading.lp_order_books = blocked_books  # type: ignore[method-assign]
    clock["now"] = clock["now"] + timedelta(seconds=31)
    maintenance = threading.Thread(
        target=service.refresh_candidate_recommendations,
        name="issue146-maintenance-in-flight",
        daemon=True,
    )
    maintenance.start()
    assert entered.wait(timeout=5)
    started = time.monotonic()
    second = service.refresh_candidate_recommendations()
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert second["scanning"] is True
    assert len(book_calls) == 1
    release.set()
    maintenance.join(timeout=5)
    assert not maintenance.is_alive()
    assert len(book_calls) == 1

    # The scan behaves the same way: one in-flight scan, second call returns
    # the snapshot immediately.  The competition read is scan-only, so
    # blocking it parks the scan without touching maintenance reads.
    book_calls.clear()
    entered = threading.Event()
    release = threading.Event()

    def blocked_competitiveness(
        *, stop_event: object = None, previous: object = None, start_cursor=None
    ) -> dict[str, object]:
        del stop_event, previous, start_cursor
        entered.set()
        assert release.wait(timeout=5)
        return {
            "state": "unknown",
            "complete": False,
            "competitiveness": {},
            "not_updated": [],
        }

    trading.lp_market_competitiveness = blocked_competitiveness  # type: ignore[method-assign]
    clock["now"] = clock["now"] + timedelta(seconds=31)
    scan = threading.Thread(
        target=service.refresh_candidates,
        kwargs={"force": True},
        name="issue146-scan-in-flight",
        daemon=True,
    )
    scan.start()
    assert entered.wait(timeout=5)
    started = time.monotonic()
    again = service.refresh_candidates()
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert again["scanning"] is True
    release.set()
    scan.join(timeout=5)
    assert not scan.is_alive()


def test_lp_scan_does_not_block_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A3: a long in-flight scan must not stop the maintenance
    loop from reading and publishing the maintained head."""
    condition_id = "condition-independent"
    market = _lp_test_market(
        condition_id,
        yes_token="independent-yes",
        no_token="independent-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_candidates(force=True)["recommendations"]

    entered = threading.Event()
    release = threading.Event()

    def blocked_competitiveness(
        *, stop_event: object = None, previous: object = None, start_cursor=None
    ) -> dict[str, object]:
        del stop_event, previous, start_cursor
        entered.set()
        assert release.wait(timeout=5)
        return {
            "state": "unknown",
            "complete": False,
            "competitiveness": {},
            "not_updated": [],
        }

    trading.lp_market_competitiveness = blocked_competitiveness  # type: ignore[method-assign]

    # Issue #157: the competition read left the scan path entirely — its
    # dedicated cache-refresh thread is the only caller that blocks there.
    competition = threading.Thread(
        target=service.refresh_competition_cache,
        name="issue146-long-scan",
        daemon=True,
    )
    competition.start()
    assert entered.wait(timeout=5)

    metadata_reads = len(state["metadata_requests"])
    clock["now"] = clock["now"] + timedelta(seconds=31)
    maintained = service.refresh_candidate_recommendations()
    assert maintained["scanning"] is False, {  # type: ignore[unreachable]
        "state": maintained.get("state"),
        "checked_at": str(maintained.get("checked_at")),
        "recommendations": maintained.get("recommendations"),
        "metadata_reads": len(state["metadata_requests"]),
        "book_reads": len(state["selected_reward_requests"]),
    }
    assert maintained["recommendations"]
    # Issue #157: the +31s maintenance genuinely re-qualified the stored row
    # (refresh_failed cleared) — external reads happened; it was not gated
    # into a pure cached snapshot.  The exact per-reader request counts live
    # behind the trading client's metadata TTL cache, so they are not
    # asserted here.
    assert maintained["candidates"][0]["refresh_failed"] is False
    assert maintained["maintenance_consecutive_failures"] == 0
    # The exploration batch also rolls while the competition read hangs.
    explored = service.refresh_candidates(force=True)
    assert explored["candidate_valid_count"] >= 1
    assert explored["funnel"]["batches"] >= 2
    release.set()
    competition.join(timeout=5)
    assert not competition.is_alive()
    final = service.candidate_snapshot()
    assert final["state"] == "ready"
    assert final["complete"] is True
    assert final["recommendations"]


def test_lp_generation_guard_keeps_newer_scan_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A5/D9: a stale maintenance result is discarded instead of
    overwriting a head published by a newer scan, and the discard is not
    counted as a data failure."""
    condition_id = "condition-generation"
    market = _lp_test_market(
        condition_id,
        yes_token="generation-yes",
        no_token="generation-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    first = service.refresh_candidates(force=True)
    assert first["recommendations"]
    t0 = clock["now"]

    entered = threading.Event()
    release = threading.Event()
    original_reward = trading.lp_reward_catalog

    def blocked_selected_reward(
        *, condition_ids: object = None, stop_event: object = None
    ) -> dict[str, object]:
        if condition_ids is None:
            return original_reward()  # type: ignore[no-any-return]
        del condition_ids, stop_event
        entered.set()
        assert release.wait(timeout=5)
        return {
            "state": "unknown",
            "complete": False,
            "checked_at": clock["now"],
            "markets": (),
        }

    trading.lp_reward_catalog = blocked_selected_reward  # type: ignore[method-assign]
    clock["now"] = t0 + timedelta(seconds=31)
    maintenance = threading.Thread(
        target=service.refresh_candidate_recommendations,
        name="issue146-stale-maintenance",
        daemon=True,
    )
    maintenance.start()
    assert entered.wait(timeout=5)

    # While the stale maintenance is parked in its reward read, a newer full
    # scan publishes a fresh head (new generation).
    state["markets"][condition_id]["yes_bid"] = Decimal("0.45")  # type: ignore[index]
    newer_scan = service.refresh_candidates(force=True)
    assert newer_scan["recommendations"]
    newer_checked_at = newer_scan["checked_at"]

    release.set()
    maintenance.join(timeout=5)
    assert not maintenance.is_alive()
    final = service.candidate_snapshot()
    assert final["recommendations"]
    assert final["checked_at"] == newer_checked_at
    # Issue #157: the generation guard is retired.  Both the newer scan and
    # the +31s maintenance failed their (blocked) reward reads, so the
    # stored row keeps its values marked refresh_failed and the maintenance
    # failure counter stands at one.
    assert final["candidates"][0]["refresh_failed"] is True
    assert final["maintenance_consecutive_failures"] == 1
    trading.lp_reward_catalog = original_reward  # type: ignore[method-assign]
    metadata_reads = len(state["metadata_requests"])
    clock["now"] = t0 + timedelta(seconds=62)
    suppressed = service.refresh_candidate_recommendations()
    # The +62s attempt lands inside the 60-second backoff window: a pure
    # cached snapshot with zero external reads.
    assert len(state["metadata_requests"]) == metadata_reads
    assert suppressed["candidates"][0]["refresh_failed"] is True
    # The scheduler wait stays inside the [1, 300] band whichever branch
    # (backoff remainder or 30-second lead) governs after the interleaving.
    wait = service.candidate_maintenance_wait_seconds()
    assert wait is None or 1.0 <= wait <= 300.0


def test_lp_dashboard_reads_come_from_background_snapshot(
    tmp_path: Path,
) -> None:
    """Issue #146 A6: the page endpoint reads only the background snapshot;
    external account reads happen solely inside the snapshot refresh. Issue
    #146 D3: the refresh reads the account through the trading client's
    shared TTL reader and falls back to the direct read when it is absent."""
    state: dict[str, object] = {"now": datetime(2026, 9, 20, 12, 0, tzinfo=UTC)}
    counters: dict[str, int] = {"account_reads": 0, "shared_reads": 0}

    class CountingTrading:
        config = SimpleNamespace(wallet_address="0x" + "4" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            counters["account_reads"] += 1
            return {
                "authenticated": True,
                "checked_at": state["now"],
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_account_snapshot_shared(
            self, max_age_seconds: float = 10.0
        ) -> dict[str, object]:
            del max_age_seconds
            counters["shared_reads"] += 1
            return self.lp_account_snapshot()

        def lp_reward_snapshot(
            self, reward_date: str, market: str
        ) -> dict[str, object]:
            del reward_date, market
            return {"state": "unknown"}

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "ready",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "selected_results": [],
                "funnel": {},
                "selected_market_ids": [],
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=CountingTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    # Before the first snapshot refresh the page endpoint serves the pending
    # payload without any external read.
    pending = service.lp_dashboard()
    assert pending["state"] == "snapshot_pending"
    assert counters["account_reads"] == 0

    first = service.refresh_lp_dashboard_snapshot()
    assert counters["account_reads"] == 1
    assert counters["shared_reads"] == 1
    checked_times = {str(first.get("checked_at"))}
    for _ in range(5):
        cached = service.lp_dashboard()
        assert cached["state"] == "ready"
        assert str(cached["checked_at"]) in checked_times
    assert counters["account_reads"] == 1

    # The next snapshot refresh performs exactly one more external read.
    state["now"] = state["now"] + timedelta(seconds=10)
    second = service.refresh_lp_dashboard_snapshot()
    assert counters["account_reads"] == 2
    assert counters["shared_reads"] == 2
    assert str(second["checked_at"]) not in checked_times

    # The pending payload carries every key a ready payload carries.
    assert set(first.keys()) <= set(pending.keys()) | {
        "candidate_valid_count", "candidate_pending_count",
        "candidate_failed_recent_count",
    }

    # Issue #146 D3: without the shared reader the refresh falls back to the
    # duck-typed direct read and still succeeds.
    del CountingTrading.lp_account_snapshot_shared
    state["now"] = state["now"] + timedelta(seconds=10)
    third = service.refresh_lp_dashboard_snapshot()
    assert third["state"] == "ready"
    assert counters["account_reads"] == 3
    assert counters["shared_reads"] == 2
    assert str(third["checked_at"]) not in checked_times | {
        str(second.get("checked_at"))
    }


def test_lp_maintenance_publishes_diagnostics_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A9: the snapshot carries per-read timings, the next
    allowed attempt, and the consecutive failure count."""
    condition_id = "condition-diag"
    market = _lp_test_market(
        condition_id,
        yes_token="diag-yes",
        no_token="diag-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    monkeypatch.setattr(
        polymarket_trading_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"].timestamp()),  # type: ignore[attr-defined]
    )
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_candidates(force=True)["recommendations"]
    t0 = clock["now"]

    state["metadata_error"] = True
    clock["now"] = t0 + timedelta(seconds=31)
    failed = service.refresh_candidate_recommendations()
    failed_diagnostics = failed["maintenance_diagnostics"]
    assert set(failed_diagnostics) == {"started_at", "finished_at", "read_seconds"}
    started = datetime.fromisoformat(
        failed_diagnostics["started_at"].replace("Z", "+00:00")
    )
    finished = datetime.fromisoformat(
        failed_diagnostics["finished_at"].replace("Z", "+00:00")
    )
    assert finished >= started
    assert set(failed_diagnostics["read_seconds"]) == {
        "account",
        "metadata",
        "reward",
        "books",
    }
    assert all(
        isinstance(value, float) and value >= 0.0
        for value in failed_diagnostics["read_seconds"].values()
    )
    assert failed["maintenance_consecutive_failures"] == 1
    next_attempt = datetime.fromisoformat(
        failed["maintenance_next_attempt_at"].replace("Z", "+00:00")
    )
    assert next_attempt == finished + timedelta(seconds=60)

    state["metadata_error"] = False
    clock["now"] = finished + timedelta(seconds=61)
    recovered = service.refresh_candidate_recommendations()
    assert recovered["recommendations"]
    assert recovered["maintenance_consecutive_failures"] == 0
    assert recovered["maintenance_next_attempt_at"] is None
    recovered_diagnostics = recovered["maintenance_diagnostics"]
    assert set(recovered_diagnostics["read_seconds"]) == {
        "account",
        "metadata",
        "reward",
        "books",
    }
    recovered_finished = datetime.fromisoformat(
        recovered_diagnostics["finished_at"].replace("Z", "+00:00")
    )
    assert recovered_finished >= t0 + timedelta(seconds=92)


def test_lp_trial_preview_uses_same_qualification_and_selected_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "condition-preview"
    token_id = "preview-yes"
    market = _lp_test_market(
        condition_id,
        yes_token=token_id,
        no_token="preview-no",
        reward_min_size=Decimal("20"),
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )
    assert service.refresh_price_history()["state"] == "known"
    scanned = service.refresh_candidates(force=True)
    assert scanned["recommendations"]

    execution = PredictionExecutionService(
        store=store,
        monitor=_Monitor(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
        lp=service,
    )
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
    )
    request = json.dumps(
        {
            "market_id": market["market_id"],
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
        }
    ).encode("utf-8")
    phase = {"name": "initial"}
    advance_before_book = {"value": True}
    original_books = trading.lp_order_books
    preview_book_batches: list[tuple[str, ...]] = []

    def staged_books(token_ids: object, *, stop_event: object = None):
        del stop_event
        batch = tuple(token_ids)  # type: ignore[arg-type]
        preview_book_batches.append(batch)
        if advance_before_book["value"]:
            clock["now"] += timedelta(seconds=1)
            advance_before_book["value"] = False
        books = original_books(batch)
        if token_id not in books:
            return books
        if phase["name"] == "initial":
            books[token_id] = {
                **books[token_id],
                "bids": [
                    {"price": Decimal("0.45"), "size": Decimal("1")},
                    {"price": Decimal("0.44"), "size": Decimal("100")},
                ],
                "asks": [{"price": Decimal("0.47"), "size": Decimal("100")}],
            }
        elif phase["name"] == "stress":
            books[token_id] = {
                **books[token_id],
                "bids": [
                    {"price": Decimal("0.45"), "size": Decimal("1")},
                    {"price": Decimal("0.40"), "size": Decimal("100")},
                ],
            }
        elif phase["name"] == "changed":
            books[token_id] = {
                **books[token_id],
                "bids": [
                    {"price": Decimal("0.46"), "size": Decimal("1")},
                    {"price": Decimal("0.45"), "size": Decimal("100")},
                ],
            }
        return books

    trading.lp_order_books = staged_books  # type: ignore[method-assign]
    initial_metadata_reads = len(state["metadata_requests"])
    initial_reward_reads = len(state["selected_reward_requests"])

    def post_preview(base: str) -> tuple[int, dict[str, object]]:
        return _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/candidates/preview",
                data=request,
            )
        )

    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "issue142-test"},
    ) as (base, _server_instance):
        status, previewed = post_preview(base)
        assert status == 200
        assert previewed["state"] == "previewed", previewed
        assert previewed["request"]["price"] == "0.45"
        assert previewed["request"]["quantity"] == "20"
        assert Decimal(previewed["request"]["price"]) * Decimal(
            previewed["request"]["quantity"]
        ) == Decimal("9.00")

        phase["name"] = "stress"
        status, rejected_stress = post_preview(base)
        assert status == 200
        assert rejected_stress == {
            "state": "rejected",
            "reason": "stress_loss_exceeded",
        }

        original_metadata = trading.lp_market_metadata_fresh

        def unknown_fee_metadata(
            condition_ids: object, *, stop_event: object = None
        ):
            del stop_event
            metadata = original_metadata(tuple(condition_ids))  # type: ignore[arg-type]
            return {
                condition: {
                    **value,
                    "fees_enabled": None,
                    "fee": None,
                    "taker_fee_rate": None,
                }
                for condition, value in metadata.items()
            }

        trading.lp_market_metadata_fresh = unknown_fee_metadata  # type: ignore[method-assign]
        status, rejected_fee = post_preview(base)
        assert status == 200
        assert rejected_fee == {
            "state": "rejected",
            "reason": "exit_fee_unknown",
        }
        trading.lp_market_metadata_fresh = original_metadata  # type: ignore[method-assign]

        phase["name"] = "changed"
        status, changed = post_preview(base)
        assert status == 200
        assert changed["state"] == "previewed"
        assert changed["request"]["price"] == "0.46"
        assert changed["request"]["quantity"] == "20"
        assert Decimal(changed["request"]["price"]) * Decimal(
            changed["request"]["quantity"]
        ) == Decimal("9.20")

    # Issue #143: each preview re-check reads both token books of the
    # selected market in one call.
    assert preview_book_batches == [(token_id, "preview-no")] * 4
    metadata_reads = state["metadata_requests"][initial_metadata_reads:]
    assert metadata_reads == [(condition_id,)] * 4
    reward_reads = state["selected_reward_requests"][initial_reward_reads:]
    assert len(reward_reads) == 8
    assert set(reward_reads) == {(condition_id, False), (condition_id, True)}
    assert state["trade_writes"] == []


def _lp_test_market(
    condition_id: str,
    *,
    yes_token: str,
    no_token: str,
    reward_min_size: Decimal = Decimal("90"),
) -> dict[str, object]:
    return {
        "condition_id": condition_id,
        "market_id": f"market-{condition_id}",
        "slug": f"{condition_id}-market",
        "yes_token": yes_token,
        "no_token": no_token,
        "reward_min_size": reward_min_size,
        "yes_bid": Decimal("0.50"),
        "yes_ask": Decimal("0.52"),
        "no_bid": Decimal("0.50"),
        "no_ask": Decimal("0.52"),
    }


def _lp_adapter_service_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    markets: list[dict[str, object]],
    balance_units: int = 50_000_000,
) -> tuple[
    dict[str, datetime],
    dict[str, object],
    PredictionArbitrageStore,
    PolymarketTradingClient,
    PolymarketLPService,
]:
    clock = {"now": datetime(2026, 9, 16, 12, 0, tzinfo=UTC)}
    state: dict[str, object] = {
        "markets": {str(row["condition_id"]): row for row in markets},
        "catalog_condition_ids": tuple(str(row["condition_id"]) for row in markets),
        "balance_units": balance_units,
        "account_error": False,
        "open_orders": [],
        "positions": [],
        "trade_writes": [],
        "selected_reward_requests": [],
        "metadata_requests": [],
        "market_accepting": True,
        "metadata_error": False,
        "selected_reward_invalid": set(),
        "selected_reward_timeout": set(),
        "selected_native_end_date": "2026-12-31",
        "selected_combined_end_date": "2026-12-31",
    }

    class AdapterDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            moment = clock["now"]
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)  # type: ignore[arg-type]

    monkeypatch.setattr(polymarket_trading_module, "datetime", AdapterDateTime)

    class AccountSDK:
        environment = SimpleNamespace(standard_exchange="standard-exchange")

        def get_balance_allowance(self, **_kwargs: object) -> object:
            if state["account_error"] is True:
                raise RuntimeError("injected private account read failure")
            balance = state["balance_units"]
            return SimpleNamespace(
                balance=balance,
                allowances={"standard-exchange": balance},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            if state["account_error"] is True:
                raise RuntimeError("injected private account read failure")
            return list(state["open_orders"])  # type: ignore[arg-type]

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            if state["account_error"] is True:
                raise RuntimeError("injected private account read failure")
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            if state["account_error"] is True:
                raise RuntimeError("injected private account read failure")
            return list(state["positions"])  # type: ignore[arg-type]

        def create_order(self, **_kwargs: object) -> object:
            state["trade_writes"].append("create_order")  # type: ignore[union-attr]
            raise AssertionError("read-only LP guidance must not sign orders")

        def post_order(self, *_args: object, **_kwargs: object) -> object:
            state["trade_writes"].append("post_order")  # type: ignore[union-attr]
            raise AssertionError("read-only LP guidance must not submit orders")

        def cancel(self, *_args: object, **_kwargs: object) -> object:
            state["trade_writes"].append("cancel")  # type: ignore[union-attr]
            raise AssertionError("read-only LP guidance must not cancel orders")

    class PublicSDK:
        def _market_specs(self) -> dict[str, dict[str, object]]:
            return state["markets"]  # type: ignore[return-value]

        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            if sponsored:
                return []
            markets_by_id = self._market_specs()
            return [
                {
                    "condition_id": condition_id,
                    "rewards_min_size": market["reward_min_size"],
                    "rewards_max_spread": Decimal("10"),
                    "rewards_config": [
                        {
                            "id": f"reward-{condition_id}",
                            "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                            "start_date": "2026-01-01",
                            "end_date": "2026-12-31",
                            "rate_per_day": Decimal("2"),
                        }
                    ],
                }
                for condition_id in state["catalog_condition_ids"]  # type: ignore[union-attr]
                if (market := markets_by_id.get(str(condition_id))) is not None
            ]

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool
        ) -> list[object]:
            requests = state["selected_reward_requests"]
            assert isinstance(requests, list)
            requests.append((condition_id, sponsored))
            timeout_requests = state["selected_reward_timeout"]
            assert isinstance(timeout_requests, set)
            if (condition_id, sponsored) in timeout_requests:
                raise TimeoutError("injected selected reward read failure")
            invalid_requests = state["selected_reward_invalid"]
            assert isinstance(invalid_requests, set)
            if (condition_id, sponsored) in invalid_requests:
                return [
                    {
                        "condition_id": condition_id,
                        "rewards_min_size": market["reward_min_size"]
                        if (market := self._market_specs().get(condition_id))
                        is not None
                        else None,
                        "rewards_max_spread": Decimal("10"),
                        "rewards_config": [
                            {
                                "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                                "start_date": "2026-01-01",
                                "end_date": "2026-12-31",
                                "rate_per_day": "invalid",
                            }
                        ],
                    }
                ]
            market = self._market_specs().get(condition_id)
            if market is None:
                return []
            end_date = (
                state["selected_combined_end_date"]
                if sponsored
                else state["selected_native_end_date"]
            )
            return [
                {
                    "condition_id": condition_id,
                    "rewards_min_size": market["reward_min_size"],
                    "rewards_max_spread": Decimal("10"),
                    "rewards_config": [
                        {
                            "id": f"reward-{condition_id}",
                            "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                            "start_date": "2026-01-01",
                            "end_date": end_date,
                            "rate_per_day": Decimal("2"),
                        }
                    ],
                }
            ]

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            requested_ids = tuple(condition_ids)  # type: ignore[arg-type]
            metadata_requests = state["metadata_requests"]
            assert isinstance(metadata_requests, list)
            metadata_requests.append(requested_ids)
            if state["metadata_error"] is True:
                raise TimeoutError("injected metadata read failure")
            requested = set(requested_ids)
            markets_by_id = self._market_specs()
            return [
                {
                    "id": market["market_id"],
                    "condition_id": condition_id,
                    "question": f"Will {condition_id} happen?",
                    "slug": market["slug"],
                    "state": {
                        "accepting_orders": state["market_accepting"] is True,
                    },
                    "outcomes": {
                        "yes": {"label": "Yes", "token_id": market["yes_token"]},
                        "no": {"label": "No", "token_id": market["no_token"]},
                    },
                    "trading": {
                        "minimum_order_size": Decimal("1"),
                        "minimum_tick_size": Decimal("0.01"),
                        "fees_enabled": False,
                    },
                    "rewards": {
                        "rewards_min_size": market["reward_min_size"],
                        "rewards_max_spread": Decimal("10"),
                    },
                    "events": [],
                }
                for condition_id, market in markets_by_id.items()
                if condition_id in requested
            ]

        def get_order_books(self, *, token_ids: object) -> list[object]:
            requested = set(token_ids)  # type: ignore[arg-type]
            books: list[object] = []
            for condition_id, market in self._market_specs().items():
                for outcome in ("yes", "no"):
                    token_id = market[f"{outcome}_token"]
                    if token_id not in requested:
                        continue
                    books.append(
                        {
                            "condition_id": condition_id,
                            "asset_id": token_id,
                            "timestamp": clock["now"] - timedelta(minutes=10),
                            "bids": [
                                {"price": market[f"{outcome}_bid"], "size": Decimal("1")},
                                {"price": Decimal("0.49"), "size": Decimal("100")},
                            ],
                            "asks": [
                                {"price": market[f"{outcome}_ask"], "size": Decimal("100")}
                            ],
                            "min_order_size": Decimal("1"),
                            "tick_size": Decimal("0.01"),
                        }
                    )
            return books

        def close(self) -> None:
            return None

    class HistoryResponse:
        def __init__(self, payload: Mapping[str, object]) -> None:
            self.payload = payload

        def __enter__(self) -> "HistoryResponse":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def open_history(request: object, **_kwargs: object) -> HistoryResponse:
        assert isinstance(request, Request)
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        token_ids = tuple(body["markets"])
        start_ts = int(body["start_ts"])
        end_ts = int(body["end_ts"])
        return HistoryResponse(
            {
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                }
            }
        )

    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        AccountSDK(),
        urlopen_fn=open_history,
        public_client_factory=PublicSDK,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, trading, clock=lambda: clock["now"])
    return clock, state, store, trading, service


def test_lp_returned_history_errors_obey_preparation_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markets = [
        _lp_test_market(
            f"condition-scale-{index:02d}",
            yes_token=f"scale-{index:02d}-yes",
            no_token=f"scale-{index:02d}-no",
        )
        for index in range(41)
    ]
    clock, _state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=markets
    )
    started_at = clock["now"]
    requests: list[tuple[str, ...]] = []
    lock = threading.Lock()
    active = 0
    max_active = 0
    all_started = threading.Event()

    def release_active() -> None:
        nonlocal active
        with lock:
            active -= 1

    class HistoryResponse:
        def __init__(self, payload: Mapping[str, object], release: object) -> None:
            self.payload = payload
            self.release = release

        def __enter__(self) -> "HistoryResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            if callable(self.release):
                self.release()

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def open_history(request: object, **_kwargs: object) -> HistoryResponse:
        nonlocal active, max_active
        assert isinstance(request, Request)
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        batch = tuple(body["markets"])
        with lock:
            requests.append(batch)
            active += 1
            max_active = max(max_active, active)
            if len(requests) == 4:
                all_started.set()
        assert all_started.wait(timeout=5)
        if "scale-00-yes" in batch:
            release_active()
            raise TimeoutError("synthetic history outage")
        start_ts = int(body["start_ts"])
        end_ts = int(body["end_ts"])
        return HistoryResponse(
            {
                "history": {
                    token_id: [
                        {
                            "t": stamp,
                            "p": "0.500" if offset % 2 == 0 else "0.505",
                        }
                        for offset, stamp in enumerate(
                            range(start_ts, end_ts + 60, 60)
                        )
                    ]
                    for token_id in batch
                }
            },
            release_active,
        )

    setattr(trading, "_urlopen_fn", open_history)
    first = service.refresh_price_history()
    assert first["state"] == "partial"
    assert first["preparation_outcome"] == "failure"
    first_preparation = first["preparation"]
    assert isinstance(first_preparation, Mapping)
    assert first_preparation["state"] == "partial"
    assert first_preparation["paused"] is False
    assert first_preparation["attempt"] == 0
    assert first_preparation["failure_count"] == 0
    assert first_preparation["next_retry_at"] == (
        (started_at + timedelta(seconds=300))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert first["target_count"] == 82
    assert first["updated_count"] == 62
    assert first["unknown_count"] == 20
    assert first["request_count"] == 5
    failed_condition_ids = {
        f"condition-scale-{index:02d}" for index in range(10)
    }
    failed_direction_ids = {
        f"scale-{index:02d}-{side}"
        for index in range(10)
        for side in ("yes", "no")
    }
    preparation_items = store.lp_preparation_items()
    assert {item["condition_id"] for item in preparation_items} == failed_condition_ids
    assert all(item["state"] == "waiting_retry" for item in preparation_items)
    assert all(item["retry_used"] is False for item in preparation_items)
    assert all(item["failure_count"] == 1 for item in preparation_items)
    assert all(
        item["failed_at"]
        == started_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for item in preparation_items
    )
    assert all(
        item["next_retry_at"]
        == (started_at + timedelta(seconds=300))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        for item in preparation_items
    )
    with lock:
        assert len(requests) == 5
        assert sum(len(batch) for batch in requests) == 82
        assert max_active == 4
        assert all(len(batch) <= 20 for batch in requests)
        assert "scale-40-yes" in {token for batch in requests for token in batch}
        assert active == 0

    retained = store.lp_price_history_summary(
        "condition-scale-10", "scale-10-yes", now=started_at
    )
    assert retained is not None
    assert retained["state"] == "known"
    tail_summary = store.lp_price_history_summary(
        "condition-scale-40", "scale-40-yes", now=started_at
    )
    assert tail_summary is not None
    assert tail_summary["state"] == "known"

    clock["now"] = started_at + timedelta(seconds=299)
    before_retry = service.refresh_price_history()
    assert before_retry["preparation_outcome"] == "waiting_retry"
    assert len(requests) == 6
    assert requests[-1] == ("scale-00-yes", "scale-00-no")

    clock["now"] = started_at + timedelta(seconds=300)
    second = service.refresh_price_history()
    assert second["state"] == "partial"
    assert second["preparation_outcome"] == "failure"
    second_preparation = second["preparation"]
    assert isinstance(second_preparation, Mapping)
    assert second_preparation["state"] == "partial"
    assert second_preparation["paused"] is False
    assert second_preparation["attempt"] == 0
    assert second_preparation["failure_count"] == 0
    assert len(requests) == 7
    assert set(requests[-1]) == failed_direction_ids
    assert len(requests[-1]) == 20
    all_direction_ids = {
        f"scale-{index:02d}-{side}"
        for index in range(41)
        for side in ("yes", "no")
    }
    assert all(
        sum(direction in batch for batch in requests)
        == (
            3
            if direction in {"scale-00-yes", "scale-00-no"}
            else 2
            if direction in failed_direction_ids
            else 1
        )
        for direction in all_direction_ids
    )
    waiting_items = store.lp_preparation_items()
    assert {item["condition_id"] for item in waiting_items} == failed_condition_ids
    assert all(item["state"] == "waiting_retry" for item in waiting_items)
    assert all(item["paused"] is False for item in waiting_items)
    assert all(item["retry_used"] is False for item in waiting_items)
    assert all(item["failure_count"] == 2 for item in waiting_items)
    assert all(
        item["next_retry_at"]
        == (started_at + timedelta(seconds=900))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        for item in waiting_items
    )
    assert second.get("alert_pending") is True

    clock["now"] = started_at + timedelta(hours=1)
    paused = service.refresh_price_history()
    assert paused["preparation"]["state"] == "partial"
    assert paused["preparation"]["paused"] is False
    assert paused.get("alert_pending") is not True
    assert len(requests) == 8
    rebuilt = PolymarketLPService(store, trading, clock=lambda: clock["now"])
    rebuilt_paused = rebuilt.refresh_price_history()
    assert rebuilt_paused["preparation"]["state"] == "partial"
    assert rebuilt_paused["preparation"]["paused"] is False
    assert rebuilt_paused.get("alert_pending") is not True
    assert len(requests) == 8

    short_clock, _short_state, _short_store, short_trading, short_service = (
        _lp_adapter_service_fixture(
            tmp_path / "short", monkeypatch, markets=markets
        )
    )
    short_requests: list[tuple[str, ...]] = []

    def open_short_history(request: object, **_kwargs: object) -> HistoryResponse:
        assert isinstance(request, Request)
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        batch = tuple(body["markets"])
        short_requests.append(batch)
        return HistoryResponse(
            {"history": {token_id: [] for token_id in batch}}, lambda: None
        )

    setattr(short_trading, "_urlopen_fn", open_short_history)
    short_result = short_service.refresh_price_history()
    assert short_result["state"] == "partial"
    assert short_result["preparation_outcome"] == "waiting_retry"
    assert short_result["unknown_count"] == 82
    assert short_result["request_count"] == 5
    short_preparation = short_result["preparation"]
    assert isinstance(short_preparation, Mapping)
    assert short_preparation["state"] == "partial"
    assert short_preparation["paused"] is False
    assert short_preparation["failure_count"] == 0
    assert short_preparation["next_retry_at"] == (
        (short_clock["now"] + timedelta(seconds=300))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert len(short_requests) == 5
    assert sum(len(batch) for batch in short_requests) == 82
    short_items = _short_store.lp_preparation_items()
    assert len(short_items) == 41
    assert all(item["state"] == "waiting_retry" for item in short_items)
    assert all(item["paused"] is False for item in short_items)


def test_lp_empty_preparation_keeps_pool_rows_until_expiry(tmp_path: Path) -> None:
    condition_id = "condition-empty-after"
    yes_token = "empty-after-yes"
    no_token = "empty-after-no"
    clock = {"now": datetime(2026, 9, 17, 2, 0, tzinfo=UTC)}

    class Exchange:
        def __init__(self) -> None:
            self.global_catalog_reads = 0
            self.account_reads = 0
            self.book_reads = 0

        def _reward(self) -> dict[str, object]:
            return {
                "condition_id": condition_id,
                "checked_at": clock["now"],
                "reward_checked_at": clock["now"],
                "reward_active": True,
                "daily_pool_usd": Decimal("100"),
                "rewards_min_size": Decimal("20"),
                "rewards_max_spread": Decimal("10"),
                "native_reward_configs": [
                    {
                        "id": "empty-after-reward",
                        "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                        "start_date": "2026-01-01",
                        "end_date": "2026-12-31",
                        "rate_per_day": Decimal("2"),
                    }
                ],
                "sponsored_reward_configs": [],
            }

        def _metadata(self) -> dict[str, object]:
            return {
                "condition_id": condition_id,
                "market_id": "market-empty-after",
                "metadata_checked_at": clock["now"],
                "fees_checked_at": clock["now"],
                "accepting_orders": True,
                "tick_size": Decimal("0.01"),
                "minimum_order_size": Decimal("1"),
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "fees_enabled": False,
                "outcomes": {
                    "yes": {"label": "YES", "token_id": yes_token},
                    "no": {"label": "NO", "token_id": no_token},
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                markets = () if self.global_catalog_reads >= 2 else (self._reward(),)
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": clock["now"],
                    "markets": markets,
                }
            self.global_catalog_reads += 1
            markets = (self._reward(),) if self.global_catalog_reads == 1 else ()
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock["now"],
                "markets": markets,
            }

        def lp_market_metadata(
            self, condition_ids: object, *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            requested = tuple(condition_ids)  # type: ignore[arg-type]
            return {condition_id: self._metadata()} if condition_id in requested else {}

        def lp_price_history(
            self,
            token_ids: object,
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.50")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in tuple(token_ids)  # type: ignore[arg-type]
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            self.account_reads += 1
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "checked_at": clock["now"],
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: object, *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.book_reads += 1
            return {
                token_id: {
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "received_at": clock["now"],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                }
                for token_id in tuple(token_ids)  # type: ignore[arg-type]
            }

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: clock["now"])

    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"
    first = service.refresh_candidates(force=True)
    assert first["candidates"]
    first_account_reads = exchange.account_reads
    first_book_reads = exchange.book_reads

    clock["now"] += timedelta(seconds=1)
    service.refresh_price_history()
    empty = service.refresh_candidates(force=True)

    assert empty["state"] == "ready"
    assert empty["complete"] is True
    # Issue #157: an emptied catalog no longer wipes the pool — the
    # one-second-old estimate stays valid for its full five-minute window —
    # while the funnel honestly reflects the emptied base funnel.
    assert len(empty["candidates"]) == 1
    assert len(empty["recommendations"]) == 1
    assert empty["funnel"]["read"] == 0
    assert empty["funnel"]["base"] == 0
    assert empty["funnel"]["sort"] == 0
    assert empty["funnel"]["trial"] == 1
    assert "risk" not in empty["funnel"]
    assert exchange.account_reads == first_account_reads
    assert exchange.book_reads == first_book_reads

    persisted = store.lp_screening_snapshot()
    assert isinstance(persisted, Mapping)
    assert len(persisted["pool"]) == 1
    reopened = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: clock["now"]
    )
    reopened_snapshot = reopened.candidate_snapshot()
    assert len(reopened_snapshot["candidates"]) == 1
    assert len(reopened_snapshot["recommendations"]) == 1
    assert reopened_snapshot["selected_market_ids"] == ["market-empty-after"]

    # Past the row's own expiry the table empties by the clock alone.
    clock["now"] += timedelta(seconds=300)
    aged_out = reopened.candidate_snapshot()
    assert aged_out["candidates"] == []
    assert aged_out["recommendations"] == []
    assert aged_out["selected_market_ids"] == []


def test_lp_recommendations_deduct_active_n_leg_cash_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "condition-lp"
    market = _lp_test_market(
        condition_id, yes_token="lp-yes", no_token="lp-no"
    )
    clock, state, store, trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market], balance_units=50_000_000
    )

    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"

    # Trial capital is reward_min_size 90 × latest_midpoint 0.505 = 45.45,
    # within the 50 USDC budget before any n-leg reservation.
    without_n_leg = service.refresh_candidates(force=True)
    assert [row["market_id"] for row in without_n_leg["candidates"]] == [
        "market-condition-lp"
    ]
    assert without_n_leg["funnel"]["excluded"]["over_available"] == 0

    batch_id = "lp-reservation-mixed"
    reservation_key = ("polymarket", "catalog-v2", "usd-micro")

    def receipt(
        receipt_id: str,
        client_order_id: str,
        *,
        quantity: int,
        filled: int,
        cost: int,
        state_name: str,
        sequence: int,
        venue_order_id: str,
    ) -> dict[str, object]:
        stamp = "2026-09-16T12:00:00.000000Z"
        return {
            "schema_version": "open_trader.prediction_n_leg.order_receipt.v1",
            "receipt_id": receipt_id,
            "execution_batch_id": batch_id,
            "client_order_id": client_order_id,
            "venue_id": reservation_key[0],
            "account_id": reservation_key[1],
            "venue_order_id": venue_order_id,
            "submitted_quantity": quantity,
            "cumulative_filled_quantity": filled,
            "cumulative_cost_units": cost,
            "cumulative_fee_units": 0,
            "state": state_name,
            "sequence": sequence,
            "rest_confirmed": False,
            "observed_at": stamp,
            "venue_timestamp": stamp,
            "rest_observation_version": None,
        }

    filled_receipt = receipt(
        "receipt-filled",
        f"{batch_id}:filled",
        quantity=6_000_000,
        filled=6_000_000,
        cost=6_000_000,
        state_name="FILLED",
        sequence=2,
        venue_order_id="venue-filled",
    )
    rejected_receipt = receipt(
        "receipt-rejected",
        f"{batch_id}:rejected",
        quantity=14_000_000,
        filled=0,
        cost=0,
        state_name="REJECTED",
        sequence=1,
        venue_order_id="venue-rejected",
    )
    prior_open_receipt = receipt(
        "receipt-filled-prior",
        f"{batch_id}:filled",
        quantity=6_000_000,
        filled=0,
        cost=0,
        state_name="OPEN",
        sequence=1,
        venue_order_id="venue-filled",
    )
    conflicting_filled_receipt = receipt(
        "receipt-filled-conflict",
        f"{batch_id}:filled",
        quantity=6_000_000,
        filled=6_000_000,
        cost=6_000_000,
        state_name="FILLED",
        sequence=1,
        venue_order_id="venue-filled",
    )
    historical_conflict = {
        "transition_id": fingerprint(
            {
                "reason": "SAME_SEQUENCE_CONFLICT",
                "old": prior_open_receipt,
                "new": conflicting_filled_receipt,
            }
        ),
        "reason": "SAME_SEQUENCE_CONFLICT",
        "old": prior_open_receipt,
        "new": conflicting_filled_receipt,
    }
    store.n_leg_create_batch(
        {
            "execution_batch_id": batch_id,
            "opportunity_episode_id": "episode-lp-reservation",
            "episode_lineage_id": "lineage-lp-reservation",
            "mode": "MANUAL",
            "state": "INCIDENT",
            "entry_fingerprint": "entry-lp-reservation",
            "total_unsettled_capital_units": 20_000_000,
            "reservation_units": 20_000_000,
            "reservations": [
                {
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "settlement_asset_id": reservation_key[2],
                    "original_units": 20_000_000,
                    "remaining_units": 14_000_000,
                    "holding_units": 6_000_000,
                }
            ],
            "legs": [
                {
                    "action_id": "filled",
                    "client_order_id": f"{batch_id}:filled",
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "asset_id": "unrelated-filled-token",
                    "settlement_asset_id": reservation_key[2],
                    "side": "BUY",
                    "submitted_quantity": 6_000_000,
                    "max_cost_units": 6_000_000,
                    "max_fee_units": 0,
                    "reservation_key": reservation_key,
                    "receipt": filled_receipt,
                },
                {
                    "action_id": "rejected",
                    "client_order_id": f"{batch_id}:rejected",
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "asset_id": "unrelated-rejected-token",
                    "settlement_asset_id": reservation_key[2],
                    "side": "BUY",
                    "submitted_quantity": 14_000_000,
                    "max_cost_units": 14_000_000,
                    "max_fee_units": 0,
                    "reservation_key": reservation_key,
                    "receipt": rejected_receipt,
                },
            ],
            "receipts": {
                "receipt-filled": filled_receipt,
                "receipt-rejected": rejected_receipt,
                "receipt-filled-prior": prior_open_receipt,
                "receipt-filled-conflict": conflicting_filled_receipt,
            },
            "confirmed_holdings": [
                {
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "asset_id": "unrelated-filled-token",
                    "quantity": 6_000_000,
                }
            ],
            "receipt_conflicts": [historical_conflict],
            "unresolved_conflicts": [],
            "capital_exposure_unknown": False,
            "incident": {"reason": "MIXED_TERMINAL_FILL"},
        }
    )

    blocked = service.refresh_candidates(force=True)
    # The active n-leg reservation reduces the budget to 30 USDC, below the
    # 45.45 trial capital, so the market is hard-excluded from the queues.
    # Issue #157: the still-valid pool estimate remains displayed until its
    # own expiry.
    assert blocked["funnel"]["excluded"]["over_available"] == 1
    assert blocked["candidate_pending_count"] == 0
    assert blocked["funnel"]["queue_total"] == 0

    def reseed_history() -> None:
        window_start = clock["now"] - timedelta(hours=24)
        history_samples = [
            {"t": int(window_start.timestamp()), "p": Decimal("0.500")},
            {"t": int(clock["now"].timestamp()), "p": Decimal("0.505")},
        ]
        store.lp_save_price_history_batch(
            {
                "condition_id": condition_id,
                "token_id": token_id,
                "samples": [dict(sample) for sample in history_samples],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "window_start": window_start,
                    "window_end": clock["now"],
                    "sample_count": len(history_samples),
                    "checked_at": clock["now"],
                    "valid_until": clock["now"] + timedelta(hours=2),
                },
            }
            for token_id in ("lp-yes", "lp-no")
        )

    trading.expire_lp_metadata_cache()
    reseed_history()
    market["reward_min_size"] = Decimal("56")
    republished = service.refresh_price_history()
    assert republished["state"] == "known"
    exact_fit = service.refresh_candidates(force=True)
    # 56 × 0.505 = 28.28 fits the 30 USDC reserved budget again.
    assert [row["min_quantity"] for row in exact_fit["candidates"]] == ["56"]
    assert exact_fit["funnel"]["excluded"]["over_available"] == 0

    acknowledged = store.n_leg_acknowledge_incident(
        batch_id,
        acknowledgement={"actor": "test", "reconciliation": "fresh_clean"},
    )
    assert acknowledged["state"] == "INCIDENT_ACKNOWLEDGED"
    assert store.n_leg_control()["active_batch_id"] is None
    assert store.n_leg_control()["total_unsettled_capital_units"] == 20_000_000

    trading.expire_lp_metadata_cache()
    reseed_history()
    market["reward_min_size"] = Decimal("90")
    republished = service.refresh_price_history()
    assert republished["state"] == "known"
    after_acknowledgement = service.refresh_candidates(force=True)
    # Acknowledgement releases the reservation, so the full 45.45 fits again.
    assert [row["market_id"] for row in after_acknowledgement["candidates"]] == [
        "market-condition-lp"
    ]
    assert after_acknowledgement["funnel"]["excluded"]["over_available"] == 0

    unknown_batch_id = "lp-reservation-unknown"
    unknown_client_id = f"{unknown_batch_id}:open"
    unknown_receipt = {
        "schema_version": "open_trader.prediction_n_leg.order_receipt.v1",
        "receipt_id": "receipt-open",
        "execution_batch_id": unknown_batch_id,
        "client_order_id": unknown_client_id,
        "venue_id": reservation_key[0],
        "account_id": reservation_key[1],
        "venue_order_id": None,
        "submitted_quantity": 20_000_000,
        "cumulative_filled_quantity": 0,
        "cumulative_cost_units": 0,
        "cumulative_fee_units": 0,
        "state": "OPEN",
        "sequence": 1,
        "rest_confirmed": False,
        "observed_at": "2026-09-16T12:00:00.000000Z",
        "venue_timestamp": "2026-09-16T12:00:00.000000Z",
        "rest_observation_version": None,
    }
    store.n_leg_create_batch(
        {
            "execution_batch_id": unknown_batch_id,
            "opportunity_episode_id": "episode-lp-reservation-unknown",
            "episode_lineage_id": "lineage-lp-reservation-unknown",
            "mode": "MANUAL",
            "state": "ACTIVE",
            "entry_fingerprint": "entry-lp-reservation-unknown",
            "total_unsettled_capital_units": 20_000_000,
            "reservations": [
                {
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "settlement_asset_id": reservation_key[2],
                    "original_units": 20_000_000,
                    "remaining_units": 20_000_000,
                    "holding_units": 0,
                }
            ],
            "legs": [
                {
                    "action_id": "open",
                    "client_order_id": unknown_client_id,
                    "venue_id": reservation_key[0],
                    "account_id": reservation_key[1],
                    "asset_id": "unrelated-live-token",
                    "settlement_asset_id": reservation_key[2],
                    "side": "BUY",
                    "submitted_quantity": 20_000_000,
                    "max_cost_units": 20_000_000,
                    "max_fee_units": 0,
                    "reservation_key": reservation_key,
                    "receipt": unknown_receipt,
                }
            ],
            "receipts": {"receipt-open": unknown_receipt},
        }
    )
    state["open_orders"] = [
        {
            "id": "unmapped-live-buy",
            "asset_id": "unrelated-live-token",
            "market": "unrelated-market",
            "condition_id": "unrelated-condition",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.50"),
            "original_size": Decimal("20"),
            "size_matched": Decimal("0"),
        }
    ]
    unknown = service.refresh_candidates(force=True)
    # An unmapped open buy makes the reserved budget unknown: the funnel
    # fails open (no over-available exclusion), and the candidate's own
    # affordability becomes unknown.  Issue #157: the failed re-estimate
    # keeps the stored row marked refresh_failed.
    assert unknown["candidates"][0]["refresh_failed"] is True
    assert unknown["funnel"]["unknown"] == 1
    assert unknown["funnel"]["excluded"]["over_available"] == 0
    assert unknown["funnel"]["budget"] == {"available_capital": None}
    assert state["trade_writes"] == []


def test_lp_public_book_sampling_survives_account_read_failure(tmp_path: Path) -> None:
    first_now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    second_now = first_now + timedelta(seconds=5)
    clock = {"now": first_now}
    markets = {
        "condition-a": {"market_id": "market-a", "yes_token": "a-yes", "no_token": "a-no"},
        "condition-b": {"market_id": "market-b", "yes_token": "b-yes", "no_token": "b-no"},
    }

    class Exchange:
        def __init__(self) -> None:
            self.catalog_reads = 0
            self.history_calls: list[dict[str, object]] = []
            self.trade_writes: list[str] = []

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            self.catalog_reads += 1
            condition_id = "condition-a" if self.catalog_reads == 1 else "condition-b"
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock["now"],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: {
                    "condition_id": condition_id,
                    "market_id": markets[condition_id]["market_id"],
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": markets[condition_id]["yes_token"]},
                        "no": {"label": "NO", "token_id": markets[condition_id]["no_token"]},
                    },
                }
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.history_calls.append(
                {
                    "token_ids": token_ids,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                }
            )
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            raise AssertionError("public history refresh must not require private account facts")

        def create_limit_order(self, **_kwargs: object) -> object:
            self.trade_writes.append("create")
            raise AssertionError("history refresh must not sign orders")

        def post_order(self, _order: object) -> object:
            self.trade_writes.append("post")
            raise AssertionError("history refresh must not post orders")

        def cancel_orders(self, **_kwargs: object) -> object:
            self.trade_writes.append("cancel")
            raise AssertionError("history refresh must not cancel orders")

    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: clock["now"])

    first = service.refresh_price_history()
    assert first["state"] == "known"
    assert first["updated_count"] == 2
    assert exchange.history_calls[0]["token_ids"] == ("a-yes", "a-no")
    first_summary = store.lp_price_history_summary(
        "condition-a", "a-yes", now=first_now
    )
    assert first_summary is not None
    assert Decimal(str(first_summary["amplitude"])) == Decimal("0.005")
    assert str(first_summary["checked_at"]).startswith(first_now.isoformat().replace("+00:00", ""))
    assert str(first_summary["window_end"]).startswith(first_now.isoformat().replace("+00:00", ""))

    clock["now"] = second_now
    second = service.refresh_price_history()
    assert second["state"] == "known"
    assert second["updated_count"] == 2
    assert exchange.history_calls[1]["token_ids"] == ("b-yes", "b-no")
    second_summary = store.lp_price_history_summary(
        "condition-b", "b-yes", now=second_now
    )
    assert second_summary is not None
    assert Decimal(str(second_summary["amplitude"])) == Decimal("0.005")
    assert str(second_summary["checked_at"]).startswith(second_now.isoformat().replace("+00:00", ""))
    retained = store.lp_price_history_summary("condition-a", "a-yes", now=second_now)
    assert retained is not None
    assert str(retained["checked_at"]).startswith(first_now.isoformat().replace("+00:00", ""))
    assert str(retained["window_end"]).startswith(first_now.isoformat().replace("+00:00", ""))
    assert store.lp_book_samples(
        "condition-a", "a-yes", since=first_now - timedelta(hours=1), until=second_now
    ) == []
    assert store.lp_book_samples(
        "condition-b", "b-yes", since=first_now - timedelta(hours=1), until=second_now
    ) == []
    assert exchange.trade_writes == []


def test_lp_refresh_queues_work_without_trading_or_waiting_for_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from functools import partial

    import open_trader.prediction_runtime as runtime_module
    import open_trader.polymarket_monitor as polymarket_monitor_module
    from open_trader.prediction_runtime import (
        PredictionRuntime,
        _UnavailableCrossVenueMonitor,
    )

    class RefreshProbe:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.first_catalog_entered = threading.Event()
            self.release_first_catalog = threading.Event()
            self.sponsored_catalog_finished = threading.Event()
            self.unexpected_catalog = threading.Event()
            self.native_catalog_calls = 0
            self.sponsored_catalog_calls = 0
            self.catalog_active = 0
            self.max_catalog_active = 0
            self.writes: list[str] = []

    probe = RefreshProbe()

    class RewardTransport:
        def get_json(self, _path: str, *, params: Mapping[str, object]) -> object:
            if params.get("sponsored") is True:
                return []
            return {"data": [], "next_cursor": "LTE="}

    class AccountSDK:
        def __init__(self) -> None:
            self.signer = "0x1111111111111111111111111111111111111111"
            self.wallet = "0x2222222222222222222222222222222222222222"
            self._ctx = SimpleNamespace(
                secure_clob=RewardTransport(), wallet_type="EOA"
            )
            self.environment = SimpleNamespace(standard_exchange="standard-exchange")

        def get_balance_allowance(self, **_kwargs: object) -> dict[str, object]:
            return {
                "balance": 100_000_000,
                "allowances": {"standard-exchange": 100_000_000},
            }

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            return []

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return []

        def is_gasless_ready(self) -> bool:
            return True

        def merge_positions(self, **_kwargs: object) -> None:
            probe.writes.append("merge")

        def create_limit_order(self, **_kwargs: object) -> None:
            probe.writes.append("sign_limit")

        def create_market_order(self, **_kwargs: object) -> None:
            probe.writes.append("sign_market")

        def post_order(self, *_args: object, **_kwargs: object) -> None:
            probe.writes.append("post_order")

        def post_orders(self, *_args: object, **_kwargs: object) -> None:
            probe.writes.append("post_orders")

        def cancel_orders(self, *_args: object, **_kwargs: object) -> None:
            probe.writes.append("cancel")

    class PublicSDK:
        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            with probe.lock:
                probe.catalog_active += 1
                probe.max_catalog_active = max(
                    probe.max_catalog_active, probe.catalog_active
                )
                if sponsored:
                    probe.sponsored_catalog_calls += 1
                    call = probe.sponsored_catalog_calls
                else:
                    probe.native_catalog_calls += 1
                    call = probe.native_catalog_calls
            try:
                if not sponsored and call == 1:
                    probe.first_catalog_entered.set()
                    assert probe.release_first_catalog.wait(timeout=5)
                elif not sponsored and call >= 2:
                    probe.unexpected_catalog.set()
                if sponsored:
                    probe.sponsored_catalog_finished.set()
                return []
            finally:
                with probe.lock:
                    probe.catalog_active -= 1

        def close(self) -> None:
            pass

    class IdlePublicClient:
        def list_events(self, **_kwargs: object) -> list[object]:
            return []

        def close(self) -> None:
            pass

    class MacOSNotifier:
        pass

    class FeishuNotifier:
        pass

    class TestNotifier:
        def __init__(self) -> None:
            self._notifiers = (MacOSNotifier(), FeishuNotifier())

    public_sdk = PublicSDK()
    account_sdk = AccountSDK()
    secrets = {
        "signing-private-key": "test-private-key",
        "builder-key": "test-builder-key",
        "builder-secret": "test-builder-secret",
        "builder-passphrase": "test-builder-passphrase",
    }
    monkeypatch.setattr(
        polymarket_trading_module,
        "load_keychain_secret",
        lambda account, **_kwargs: secrets[account],
    )
    monkeypatch.setattr(
        polymarket_trading_module.SecureClient,
        "create",
        classmethod(lambda _cls, **_kwargs: account_sdk),
    )
    monkeypatch.setattr(
        polymarket_trading_module, "PublicClient", lambda: public_sdk
    )

    class GeoBlockResponse:
        def __enter__(self) -> GeoBlockResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"blocked": false}'

    monkeypatch.setattr(
        polymarket_trading_module,
        "urlopen",
        lambda *_args, **_kwargs: GeoBlockResponse(),
    )
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketMonitor",
        partial(
            polymarket_monitor_module.PolymarketMonitor,
            public_client_factory=IdlePublicClient,
        ),
    )

    now = datetime.now(UTC)
    previous_attempt = now - timedelta(minutes=5)
    stamp = previous_attempt.isoformat().replace("+00:00", "Z")
    saved_store = PredictionArbitrageStore(tmp_path)
    saved_store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "candidate_rows_fresh": True,
            "catalog_complete": True,
            "funnel": {
                "catalog_read": 1,
                "base_pass": 1,
                "volatility_pass": 1,
                "selected": 1,
                "risk": {"passed": 1, "rejected": 0, "unknown": 0},
            },
            "selected_market_ids": ["market-old"],
            "candidates": [],
            "recommendations": [
                {
                    "condition_id": "condition-old",
                    "market_id": "market-old",
                    "state": "eligible",
                    "market_title": "Previously screened market",
                    "market_url": "https://polymarket.com/event/old-market",
                    "directions": {
                        "YES": {
                            "condition_id": "condition-old",
                            "market_id": "market-old",
                            "token_id": "token-old-yes",
                            "outcome": "YES",
                            "state": "eligible",
                            "eligible": True,
                            "reason_codes": [],
                            "guidance": {
                                "condition_id": "condition-old",
                                "market_id": "market-old",
                                "token_id": "token-old-yes",
                                "outcome": "YES",
                                "price": Decimal("0.49"),
                                "quantity": Decimal("20"),
                                "required_capital": Decimal("9.80"),
                                "estimated_exit_loss": Decimal("0.20"),
                                "estimated_exit_loss_ratio": Decimal("0.020408163265306122"),
                                "checked_at": now,
                                "expires_at": now + timedelta(seconds=45),
                            },
                        }
                    },
                }
            ],
            "selected_results": [
                {
                    "condition_id": "condition-old",
                    "market_id": "market-old",
                    "state": "eligible",
                    "market_title": "Previously screened market",
                    "market_url": "https://polymarket.com/event/old-market",
                    "directions": {
                        "YES": {
                            "condition_id": "condition-old",
                            "market_id": "market-old",
                            "token_id": "token-old-yes",
                            "outcome": "YES",
                            "state": "eligible",
                            "eligible": True,
                            "reason_codes": [],
                            "guidance": {
                                "condition_id": "condition-old",
                                "market_id": "market-old",
                                "token_id": "token-old-yes",
                                "outcome": "YES",
                                "price": Decimal("0.49"),
                                "quantity": Decimal("20"),
                                "required_capital": Decimal("9.80"),
                                "estimated_exit_loss": Decimal("0.20"),
                                "estimated_exit_loss_ratio": Decimal("0.020408163265306122"),
                                "checked_at": now,
                                "expires_at": now + timedelta(seconds=45),
                            },
                        }
                    },
                }
            ],
            "checked_at": now,
            "last_success_at": now,
            "last_attempt_at": previous_attempt,
            "scan_started_at": stamp,
            "missing_metadata_condition_ids": [],
            "missing_book_token_ids": [],
            "event_end_confirmations": {},
        }
    )
    prediction_config_path = tmp_path / "prediction.json"
    prediction_config_path.write_text(
        json.dumps(
            {
                "signer_address": "0x1111111111111111111111111111111111111111",
                "wallet_address": "0x2222222222222222222222222222222222222222",
            }
        ),
        encoding="utf-8",
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=prediction_config_path,
        dashboard_url="http://127.0.0.1:8766/",
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
        notifier=TestNotifier(),
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.monitor is not None
        runtime.monitor.stop()
        assert probe.first_catalog_entered.wait(timeout=2)
        assert runtime.lp is not None
        deadline = time.monotonic() + 2
        preparation_snapshot = runtime.lp.candidate_snapshot()
        while (
            preparation_snapshot.get("retention_reason")
            != "catalog_preparation_pending"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            preparation_snapshot = runtime.lp.candidate_snapshot()
        assert preparation_snapshot.get("retention_reason") == (
            "catalog_preparation_pending"
        )
        preparation_attempt = preparation_snapshot.get("last_attempt_at")
        assert runtime.store is not None
        assert runtime.execution is not None
        assert runtime.execution.set_validation_mode(
            "observe_only", audit={"reason": "read-only refresh fixture"}
        ) == {"state": "ok", "mode": "observe_only"}
        assert runtime.store.get_validation_mode() == "observe_only"
        preview_id = runtime.store.create_preview(
            {
                "market_type": "unrelated_test_execution",
                "intent": {"condition_id": "condition-unrelated"},
            },
            expires_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        )
        created_execution = runtime.store.consume_preview_and_create_execution(
            preview_id, "n12-unrelated-active-execution"
        )
        active_execution = runtime.store.active_execution()
        assert created_execution["state"] == "validating"
        assert active_execution is not None
        assert active_execution["execution_id"] == created_execution["execution_id"]
        assert active_execution["state"] == "validating"

        with _running_server(
            runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (base, _server_instance):
            path = "/api/prediction-arbitrage/lp/candidates/refresh"
            started_at = time.monotonic()
            status, queued = _response(
                _production_request(base, path, b"{}"), timeout=2
            )
            assert status == 202
            assert queued == {"state": "queued"}
            assert time.monotonic() - started_at < 1

            deadline = time.monotonic() + 2
            preparation_snapshot = runtime.lp.candidate_snapshot()
            while (
                (
                    preparation_snapshot.get("retention_reason")
                    != "catalog_preparation_pending"
                    or preparation_snapshot.get("last_attempt_at")
                    == preparation_attempt
                    or preparation_snapshot.get("scanning") is True
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                preparation_snapshot = runtime.lp.candidate_snapshot()
            assert preparation_snapshot.get("retention_reason") == (
                "catalog_preparation_pending"
            )
            assert preparation_snapshot.get("last_attempt_at") != preparation_attempt
            assert preparation_snapshot.get("scanning") is False

            dashboard_status, dashboard = _response(
                base + "/api/prediction-arbitrage/lp/dashboard", timeout=2
            )
            assert dashboard_status == 200
            saved_rows = dashboard["recommendations"]
            assert saved_rows == []
            # Issue #157: the pool stores only successful estimates — an
            # unknown qualification (YES unknown here) no longer persists a
            # diagnostic row in the published table; it surfaces through the
            # funnel's trial reasons instead.
            diagnostic_rows = dashboard["selected_results"]
            assert isinstance(diagnostic_rows, list) and len(diagnostic_rows) == 0
            # The YES direction carried guidance in the stored facts; the
            # unknown verdict surfaced through the funnel only, with no
            # diagnostic table row.
            assert dashboard["funnel"]["reasons"]["trial"] == []

            for _ in range(2):
                duplicate_status, duplicate = _response(
                    _production_request(base, path, b"{}"), timeout=2
                )
                assert duplicate_status == 202
                assert duplicate == {"state": "queued"}

            unauthenticated = Request(
                base + path,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": base,
                    "X-CSRF-Token": "csrf-token",
                },
                method="POST",
            )
            unauthenticated_status, _ = _response(unauthenticated, timeout=2)
            assert unauthenticated_status == 403
            invalid_status, _ = _response(
                _production_request(base, path, b'{"force":true}'), timeout=2
            )
            assert invalid_status == 400

            assert probe.native_catalog_calls == 1
            assert probe.sponsored_catalog_calls == 0
            assert probe.catalog_active == 1
            assert probe.max_catalog_active == 1
            assert probe.writes == []

            probe.release_first_catalog.set()
            assert probe.sponsored_catalog_finished.wait(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                snapshot = runtime.lp.candidate_snapshot()
                if (
                    probe.native_catalog_calls == 1
                    and probe.sponsored_catalog_calls == 1
                    and snapshot.get("state") == "ready"
                    and snapshot.get("complete") is True
                    and snapshot.get("scanning") is False
                    and snapshot.get("recommendations") == []
                    and snapshot.get("selected_results") == []
                ):
                    break
                time.sleep(0.01)
            assert snapshot.get("state") == "ready"
            assert snapshot.get("complete") is True
            assert snapshot.get("scanning") is False
            assert snapshot.get("recommendations") == []
            assert snapshot.get("selected_results") == []
            assert probe.native_catalog_calls == 1
            assert probe.sponsored_catalog_calls == 1
            assert probe.catalog_active == 0
            assert probe.max_catalog_active == 1
            assert not probe.unexpected_catalog.wait(timeout=0.1)
            assert probe.writes == []
    finally:
        probe.release_first_catalog.set()
        runtime.stop()


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


def test_paused_n_leg_routes_reject_before_business_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    external_calls: list[tuple[str, object]] = []

    class ExternalTrading:
        config = SimpleNamespace(wallet_address="0xwallet")

        def __init__(self) -> None:
            self.order = {
                "order_id": "manual-order",
                "market_id": "manual-market",
                "condition_id": "manual-condition",
                "token_id": "manual-token",
                "market_title": "Manual order fact",
                "outcome": "NO",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.36"),
                "original_size": Decimal("100"),
                "size_matched": Decimal("0"),
                "remaining_size": Decimal("100"),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "checked_at": datetime.now(UTC),
                "relayer_ready": True,
                "merge_ready": True,
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0xwallet",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": ("manual-order",),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            external_calls.append(("lp_account_snapshot", None))
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": (dict(self.order),),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return False

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            external_calls.append(("lp_reward_catalog", None))
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "markets": (),
            }

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str, **_kwargs: object
        ) -> dict[str, object]:
            external_calls.append(("lp_reward_snapshot", (reward_date, condition_id)))
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0"),
                "account_amount": Decimal("0"),
            }

        def close(self) -> None:
            external_calls.append(("trading_close", None))

        def __getattr__(self, name: str) -> object:
            if name in {
                "cancel_orders",
                "lp_post_order",
                "post_order",
                "submit_protected_sell",
            }:

                def forbidden(*_args: object, **_kwargs: object) -> None:
                    external_calls.append(("order_mutation", name))
                    raise AssertionError(f"paused HTTP path attempted {name}")

                return forbidden
            raise AttributeError(name)

    class ExternalNotifier:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def send(self, *_args: object, **_kwargs: object) -> bool:
            external_calls.append(("notification", self.channel))
            return True

    config = SimpleNamespace(
        signer_address="0xsigner", wallet_address="0xwallet", predict=None
    )
    trading = ExternalTrading()
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "lp-session",
        "lp-idempotency",
        state="complete",
        payload={
            "market_id": "manual-market",
            "condition_id": "manual-condition",
            "token_id": "manual-token",
            "outcome": "NO",
            "price": "0.36",
            "quantity": "100",
            "review_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        },
    )
    monkeypatch.setenv("OPEN_TRADER_NLEG_PAUSED", "1")
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(
            from_keychain=lambda _config: (_ for _ in ()).throw(
                AssertionError("paused HTTP server must not construct Predict client")
            )
        ),
    )
    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(
            _notifiers=(ExternalNotifier("macos"), ExternalNotifier("feishu"))
        ),
    )
    runtime.start()
    try:
        with _running_server(
            runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (base, _server_instance):
            health_status, health = _response(base + "/healthz")
            venues_status, venues = _response(
                base + "/api/prediction-arbitrage/venues"
            )

            get_paths = (
                "/api/prediction-arbitrage/state",
                "/api/prediction-arbitrage/history?kind=signals",
                "/api/prediction-arbitrage/n-leg/mode",
                "/api/prediction-arbitrage/n-leg/report",
                "/api/prediction-arbitrage/relations",
                "/api/prediction-arbitrage/relations/version-1",
                "/api/prediction-arbitrage/llm-provider",
            )
            get_results = [_response(base + path) for path in get_paths]
            post_paths = (
                "/api/prediction-arbitrage/mode",
                "/api/prediction-arbitrage/preview",
                "/api/prediction-arbitrage/n-leg/mode",
                "/api/prediction-arbitrage/n-leg/orders/confirm",
                "/api/prediction-arbitrage/relations/change-set",
                "/api/prediction-arbitrage/llm-provider",
            )
            post_results = [
                _response(_production_request(base, path, data=b"{}"))
                for path in post_paths
            ]
            # Issue #146: the route serves the background snapshot and
            # coalesces while the observation refresh owns the pipeline, so
            # retry like the page's 5-second poller would.
            lp_payload: dict[str, object] = {}
            lp_deadline = time.monotonic() + 5
            while time.monotonic() < lp_deadline:
                runtime.execution.refresh_lp_dashboard_snapshot()
                lp_status, lp_payload = _response(
                    base + "/api/prediction-arbitrage/lp/dashboard"
                )
                if lp_payload.get("state") != "snapshot_pending":
                    break
                time.sleep(0.05)
            session_status, session_payload = _response(
                base + "/api/prediction-arbitrage/lp/sessions/current"
            )
            report_status, report_payload = _response(
                base + "/api/prediction-arbitrage/lp/reports/2026-09-17"
            )
            lp_post_status, lp_post_payload = _response(
                _production_request(
                    base,
                    "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b"{}",
                )
            )
            unauthorized_lp_status = _status(
                Request(
                    base + "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            )
    finally:
        runtime.stop()

    assert health_status == 200
    assert health["n_leg"] == {"status": "paused", "code": "N_LEG_PAUSED"}
    assert venues_status == 200
    assert venues["n_leg"] == {"status": "paused", "code": "N_LEG_PAUSED"}
    assert [status for status, _payload in get_results] == [409] * len(get_paths)
    assert all(
        payload["error"] == "N_LEG_PAUSED"
        for _status_code, payload in get_results
    )
    assert [status for status, _payload in post_results] == [409] * len(post_paths)
    assert all(
        payload["error"] == "N_LEG_PAUSED"
        for _status_code, payload in post_results
    )
    assert lp_status == 200
    assert lp_payload["orders"], sorted(lp_payload) and lp_payload.get("state")
    assert lp_payload["orders"][0]["order_id"] == "manual-order"
    assert lp_payload["orders"][0]["management"] == "manual_read_only"
    assert session_status == 200
    assert session_payload["state"] == "complete"
    assert report_status == 404
    assert report_payload == {"error": "LP report not found"}
    assert lp_post_status == 202
    assert lp_post_payload == {"state": "queued"}
    assert unauthorized_lp_status == 403
    assert not [
        call
        for call in external_calls
        if call[0] in {"order_mutation", "notification"}
    ]


def test_paused_n_leg_requests_do_not_hold_lp_or_health_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    n_leg_entered = threading.Event()
    release_n_leg = threading.Event()
    external_calls: list[str] = []

    class ExternalTrading:
        config = SimpleNamespace(wallet_address="0xwallet")

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "checked_at": datetime.now(UTC),
                "relayer_ready": True,
                "merge_ready": True,
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0xwallet",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": (),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            external_calls.append("lp_account_snapshot")
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": (),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            external_calls.append("lp_reward_catalog")
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "markets": (),
            }

        def close(self) -> None:
            external_calls.append("trading_close")

    class ExternalNotifier:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def send(self, *_args: object, **_kwargs: object) -> bool:
            external_calls.append(f"notification:{self.channel}")
            return True

    def blocked_predict_client(_config: object) -> object:
        n_leg_entered.set()
        release_n_leg.wait(timeout=5)
        raise AssertionError("paused concurrent HTTP path entered N_LEG external boundary")

    config = SimpleNamespace(
        signer_address="0xsigner", wallet_address="0xwallet", predict=None
    )
    trading = ExternalTrading()
    monkeypatch.setenv("OPEN_TRADER_NLEG_PAUSED", "1")
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=blocked_predict_client),
    )
    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(
            _notifiers=(ExternalNotifier("macos"), ExternalNotifier("feishu"))
        ),
    )
    runtime.start()
    try:
        with _running_server(
            runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (base, _server_instance):
            paths = (
                "/api/prediction-arbitrage/state",
                "/api/prediction-arbitrage/history?kind=signals",
                "/api/prediction-arbitrage/n-leg/report",
                "/api/prediction-arbitrage/lp/dashboard",
                "/healthz",
            )
            barrier = threading.Barrier(len(paths))

            def fetch(path: str) -> tuple[str, int, dict[str, object]]:
                barrier.wait(timeout=5)
                status, payload = _response(base + path, timeout=5)
                return path, status, payload

            with ThreadPoolExecutor(max_workers=len(paths)) as clients:
                results = list(clients.map(fetch, paths))

            by_path = {path: (status, payload) for path, status, payload in results}
            assert [by_path[path][0] for path in paths[:3]] == [409, 409, 409]
            assert by_path["/api/prediction-arbitrage/lp/dashboard"][0] == 200
            assert by_path["/healthz"][0] == 200
            # Issue #146: the route serves the background snapshot; poll like
            # the page does until the first snapshot is published.
            dashboard_payload = by_path["/api/prediction-arbitrage/lp/dashboard"][1]
            dashboard_deadline = time.monotonic() + 5
            while (
                dashboard_payload.get("state") != "ready"
                and time.monotonic() < dashboard_deadline
            ):
                runtime.execution.refresh_lp_dashboard_snapshot()
                _dash_status, dashboard_payload = _response(
                    base + "/api/prediction-arbitrage/lp/dashboard", timeout=5
                )
                time.sleep(0.05)
            assert dashboard_payload["state"] == "ready"
            assert by_path["/healthz"][1]["n_leg"] == {
                "status": "paused",
                "code": "N_LEG_PAUSED",
            }
            assert n_leg_entered.is_set() is False
            assert release_n_leg.is_set() is False
    finally:
        release_n_leg.set()
        runtime.stop()

    assert "lp_account_snapshot" in external_calls
    assert not any(item.startswith("notification:") for item in external_calls)


def test_paused_state_and_lp_remain_responsive_during_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paused N-Leg HTTP reads stay available while LP preparation waits upstream."""

    import open_trader.prediction_runtime as runtime_module

    upstream_started = threading.Event()
    release_upstream = threading.Event()

    class ExternalTrading:
        config = SimpleNamespace(
            signer_address="0x" + "1" * 40,
            wallet_address="0x" + "2" * 40,
            predict=None,
        )

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "checked_at": datetime.now(UTC),
                "relayer_ready": True,
                "merge_ready": True,
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": self.config.wallet_address,
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": (),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": (),
                "positions": (),
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            upstream_started.set()
            while not release_upstream.wait(timeout=0.01):
                if stop_event is not None and stop_event.is_set():
                    return {
                        "state": "cancelled",
                        "complete": False,
                        "checked_at": datetime.now(UTC),
                        "markets": (),
                    }
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "markets": (),
            }

        def lp_market_metadata(
            self,
            _condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {}

        def lp_price_history(
            self,
            _token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            return {"state": "known", "history": {}}

        def lp_order_books(
            self,
            _token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {}

        def close(self) -> None:
            pass

    class ExternalNotifier:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def notify(self, *_args: object, **_kwargs: object) -> None:
            pass

    config = ExternalTrading.config
    trading = ExternalTrading()
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(
            _notifiers=(ExternalNotifier("macos"), ExternalNotifier("feishu"))
        ),
        cross_venue_monitor=runtime_module._UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
    )
    runtime.start()
    try:
        assert upstream_started.wait(timeout=2)
        with _running_server(
            runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (base, _server_instance):
            started_at = time.monotonic()
            state_status, state = _response(
                base + "/api/prediction-arbitrage/state", timeout=2
            )
            health_status, health = _response(base + "/healthz", timeout=2)
            dashboard_status, dashboard = _response(
                base + "/api/prediction-arbitrage/lp/dashboard", timeout=2
            )
            assert time.monotonic() - started_at < 1
            assert release_upstream.is_set() is False

        assert state_status == 409
        assert state == {"error": "N_LEG_PAUSED", "error_code": "N_LEG_PAUSED"}
        assert health_status == 200
        assert health["n_leg"] == {
            "status": "paused",
            "code": "N_LEG_PAUSED",
        }
        assert dashboard_status == 200
        # Issue #146 D2: the page endpoint stays fast while preparation is
        # still in flight. It serves the pending payload until the background
        # snapshot thread publishes, and the published snapshot afterwards.
        assert dashboard["state"] in {"snapshot_pending", "ready", "stale"}
        assert dashboard["recommendations"] == []
        assert dashboard["selected_results"] == []
    finally:
        release_upstream.set()
        runtime.stop()


def test_shadow_paused_n_leg_posts_report_pause_before_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    monkeypatch.setenv("OPEN_TRADER_NLEG_PAUSED", "1")
    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="shadow",
    )
    runtime.start()
    try:
        with _server(runtime) as base:
            n_leg_paths = (
                "/api/prediction-arbitrage/preview",
                "/api/prediction-arbitrage/n-leg/orders/confirm",
                "/api/prediction-arbitrage/relations/change-set",
            )
            n_leg_results = [
                _response(
                    Request(
                        base + path,
                        data=b"{",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                )
                for path in n_leg_paths
            ]
            lp_status, lp_payload = _response(
                Request(
                    base + "/api/prediction-arbitrage/lp/sessions/start",
                    data=b"{",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            )
    finally:
        runtime.stop()

    assert n_leg_results == [
        (409, {"error": "N_LEG_PAUSED", "error_code": "N_LEG_PAUSED"})
        for _path in n_leg_paths
    ]
    assert lp_status == 403
    assert lp_payload == {
        "code": "shadow_read_only",
        "message": "Shadow Prediction Service is read-only",
    }


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
    ("account_age_seconds", "expected_usdt"),
    ((0, "25"), (61, None)),
    ids=("current-predict-account", "expired-predict-account"),
)
def test_venues_endpoint_serves_cached_cards_and_bootstraps_lp_auth(
    tmp_path: Path, account_age_seconds: int, expected_usdt: str | None
) -> None:
    now = datetime.now(UTC)
    poly_wallet = "0x1111222233334444555566667777888899990000"
    predict_wallet = "0x2222333344445555666677778888999900001111"

    def unexpected_external_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cached venue summary must not call external adapters")

    monitor = make_monitor(
        tmp_path / "monitor",
        trading=SimpleNamespace(readiness_snapshot=unexpected_external_call),
        clock=lambda: now,
    )
    with monitor._lock:
        monitor._universe_at = now
        monitor._heartbeat_at = now
        monitor._stream_handle = object()
        monitor._readiness = {
            "status": "ready",
            "wallet_address": poly_wallet,
            "p_usd_balance": "50",
            "p_usd_allowance": "50",
            "checked_at": now,
        }
        monitor._cross_venue_tokens = {"poly-token"}
        monitor._n_leg_tokens = {"n-leg-token"}

    execution, _trading, store, _execution_monitor = execution_fixture(tmp_path / "execution")
    execution._breaker_open = False
    execution._cross_breaker_open = False
    account_cache = {
        "wallet_address": predict_wallet,
        "predict_account": predict_wallet,
        "available_usdt": "25",
        "allowance": "0",
        "scope_ready": True,
        "gas_ready": True,
        "allowance_breaker": False,
        "minimum_top_up_bnb": "0",
        "required_bnb": "0",
        "bnb_balance": "0.1",
        "reserved_usdt": "0",
        "unsettled_usdt": "0",
        "open_orders": [],
        "positions": [],
        "checked_at": now - timedelta(seconds=account_age_seconds),
    }
    with execution._predict_snapshot_lock:
        execution._predict_snapshot_cache = account_cache

    predict_source = PredictSource(
        PredictConfig(wallet_address=predict_wallet),
        key_loader=unexpected_external_call,
        urlopen_fn=unexpected_external_call,
        websocket_connect=unexpected_external_call,
        now_fn=lambda: now,
    )
    predict_source._rest_status = "ready"
    predict_source._ws_status = "ready"
    predict_source._last_success = {"rest": now, "ws": now}
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=monitor,
        execution=execution,
        cross_venue_monitor=SimpleNamespace(_predict=predict_source),
    )

    with _production_server(runtime) as (base, _runtime):
        status, payload, venue_headers = _response_with_headers(
            Request(base + "/api/prediction-arbitrage/venues", method="GET")
        )
        assert status == 200
        assert set(payload) == {
            "venues",
            "monitor_subscription",
            "csrf_token",
            "n_leg",
        }
        assert payload["n_leg"] == {"status": "running", "code": "N_LEG_RUNNING"}
        assert payload["csrf_token"] == "csrf-token"
        venues = payload["venues"]
        assert isinstance(venues, list) and len(venues) == 2
        assert venues[0]["venue"] == "polymarket"
        assert venues[0]["rest"] == venues[0]["ws"] == "ready"
        assert venues[0]["wallet"] == "0x1111…0000"
        assert venues[0]["balance"] == {"asset": "pUSD", "value": "50"}
        assert venues[1]["venue"] == "predict.fun"
        assert venues[1]["rest"] == venues[1]["ws"] == "ready"
        assert venues[1]["wallet"] == "0x2222…1111"
        assert venues[1]["balance"] == {"asset": "USDT", "value": expected_usdt}
        if expected_usdt is None:
            assert "account" not in venues[1]
        else:
            assert venues[1]["account"]["available_usdt"] == "25"
        assert payload["monitor_subscription"] == {
            "cross_venue_token_count": 2,
            "n_leg_cross_venue_token_count": 1,
        }
        for omitted in ("events", "opportunities", "observation", "histories", "orders"):
            assert omitted not in payload

        assert venue_headers["Set-Cookie"] == (
            "ot_prediction_session=session-token; SameSite=Strict; HttpOnly; Path=/"
        )
        session_cookie = venue_headers["Set-Cookie"].split(";", 1)[0]
        valid_headers = {
            "Content-Type": "application/json",
            "Origin": base,
            "Cookie": session_cookie,
            "X-CSRF-Token": str(payload["csrf_token"]),
        }
        valid_status, valid_body = _response(
            Request(
                base + "/api/prediction-arbitrage/lp/candidates/preview",
                data=b"{}",
                headers=valid_headers,
                method="POST",
            )
        )
        assert valid_status == 400
        assert valid_body["error_type"] == "ValueError"

        missing_token_headers = {
            "Content-Type": "application/json",
            "Origin": base,
            "Cookie": session_cookie,
        }
        missing_token_status = _status(
            Request(
                base + "/api/prediction-arbitrage/lp/candidates/preview",
                data=b"{}",
                headers=missing_token_headers,
                method="POST",
            )
        )
        assert missing_token_status == 403

    with _server(_Runtime()) as shadow_base:
        shadow_status, shadow_payload, shadow_headers = _response_with_headers(
            Request(shadow_base + "/api/prediction-arbitrage/venues", method="GET")
        )
    assert shadow_status == 200
    assert shadow_payload["csrf_token"] == ""
    assert "Set-Cookie" not in shadow_headers


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


def test_lp_augment_routes_preserve_guard_idempotency_and_schema(
    tmp_path: Path,
) -> None:
    """Issue 158 加量路由契约 + #167 改写——鉴权、严格 schema、幂等重试、busy 仍 200；
    两段式预检缺省组价（入场单在挂）按 D1-c 拒 price_level_active，
    成功/幂等/熔断路径移到单发 /lp/augment 带 price。"""

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
                    "balance": Decimal("1000"),
                    "allowance": Decimal("1000"),
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
            return {
                **signed,
                "order_id": f"lp-order-{len(self.posts)}",
                "status": "LIVE",
            }

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
    with _running_server(
        production_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/preview",
                data=json.dumps(request).encode("utf-8"),
            )
        )
        assert preview_status == 200
        entry_status, entry = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": preview["preview_id"],
                        "idempotency_key": "lp-aug-api-entry",
                    }
                ).encode("utf-8"),
            )
        )
        assert entry_status == 200
        assert entry["state"] == "entry_open"
        session_id = str(entry["session_id"])
        augment_path = f"/api/prediction-arbitrage/lp/sessions/{session_id}/augment"

        # 严格 schema：恰好 {session_id, quantity}。
        bad_preview_status, _bad_preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment/preview",
                data=json.dumps(
                    {"session_id": session_id, "quantity": "90", "price": "0.30"}
                ).encode("utf-8"),
            )
        )
        assert bad_preview_status == 400

        # #167：两段式预检 schema 严格 {session_id, quantity}，缺省=组价；
        # 入场单仍在挂 → price_level_active（同价加量退役）。
        augment_preview_status, augment_preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment/preview",
                data=json.dumps(
                    {"session_id": session_id, "quantity": "90"}
                ).encode("utf-8"),
            )
        )
        assert augment_preview_status == 200
        assert augment_preview == {"state": "rejected", "reason": "price_level_active"}
        assert len(exchange.posts) == 1

        # 成功：单发路由带 price=0.28（新价位，不高于顶档）。
        single_body = json.dumps(
            {
                "session_id": session_id,
                "quantity": "90",
                "idempotency_key": "lp-aug-api-1",
                "price": "0.28",
            }
        ).encode("utf-8")
        first_status, first = _response(
            _production_request(base, "/api/prediction-arbitrage/lp/augment", data=single_body)
        )
        assert first_status == 200
        assert first["state"] == "entry_open"
        assert first["augment_order_id"] == "lp-order-2"
        assert Decimal(str(exchange.posts[1]["price"])) == Decimal("0.28")
        assert len(exchange.posts) == 2

        # 幂等重试：同 key 同 body → 返既有结果，无第二张追加单。
        retry_status, retry = _response(
            _production_request(base, "/api/prediction-arbitrage/lp/augment", data=single_body)
        )
        assert retry_status == 200
        assert retry["augment_order_id"] == "lp-order-2"
        assert len(exchange.posts) == 2

        # 严格 schema：恰好 {preview_id, idempotency_key}。
        extra_status, _extra = _response(
            _production_request(
                base,
                augment_path,
                data=json.dumps(
                    {
                        "preview_id": "x",
                        "idempotency_key": "lp-aug-api-2",
                        "extra": True,
                    }
                ).encode("utf-8"),
            )
        )
        assert extra_status == 400
        missing_status, _missing = _response(
            _production_request(
                base,
                augment_path,
                data=b'{"preview_id":"x"}',
            )
        )
        assert missing_status == 400

        # busy 分支保持 HTTP 200：既有活动会话时新开 lp/sessions 的响应
        # 仍是 200，由 body 内 state 分支渲染文案（定案 10）。
        busy_preview_status, busy_preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/preview",
                data=json.dumps(request).encode("utf-8"),
            )
        )
        assert busy_preview_status == 200
        busy_status, busy = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": busy_preview["preview_id"],
                        "idempotency_key": "lp-aug-api-busy",
                    }
                ).encode("utf-8"),
            )
        )
        assert busy_status == 200
        assert busy["state"] == "busy"
        assert busy["reason"] == "lp_session_market_active"
        assert len(exchange.posts) == 2

        # 熔断开启 → 单发加量被锁，不下单。
        execution._breaker_open = True
        locked_status, locked = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "90",
                        "idempotency_key": "lp-aug-api-3",
                        "price": "0.28",
                    }
                ).encode("utf-8"),
            )
        )
        assert locked_status == 200
        assert locked == {"state": "locked", "reason": "circuit_breaker_open"}
        assert len(exchange.posts) == 2

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
        shadow_preview_status, _shadow_preview = _response(
            _production_request(
                shadow_base,
                "/api/prediction-arbitrage/lp/augment/preview",
                data=json.dumps(
                    {"session_id": session_id, "quantity": "90"}
                ).encode("utf-8"),
            )
        )
        shadow_augment_status, _shadow_augment = _response(
            _production_request(
                shadow_base,
                f"/api/prediction-arbitrage/lp/sessions/{session_id}/augment",
                data=b'{"preview_id":"x","idempotency_key":"k"}',
            )
        )

    assert shadow_preview_status == shadow_augment_status == 403
    assert len(exchange.posts) == 2


def test_lp167_h_augment_route_accepts_price_and_rejects_bad_prices(
    tmp_path: Path,
) -> None:
    """H（issue 167）：/lp/augment 可选 price——新价位成功、高于顶档拒、同价拒；
    严格 schema 保持（多字段 400）；/lp/orders 契约不变。"""

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    condition_id = "0x" + "c" * 64
    token_id = "0x" + "1" * 64

    class Exchange:
        def __init__(self) -> None:
            self.posts: list[dict[str, object]] = []
            self.snapshot = {
                "account": {
                    "authenticated": True,
                    "balance": Decimal("1000"),
                    "allowance": Decimal("1000"),
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
            return {
                **signed,
                "order_id": f"lp-order-{len(self.posts)}",
                "status": "LIVE",
            }

        def cancel_order(self, order_id: str) -> dict[str, object]:
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
    with _running_server(
        production_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/preview",
                data=json.dumps(request).encode("utf-8"),
            )
        )
        assert preview_status == 200
        entry_status, entry = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/sessions",
                data=json.dumps(
                    {
                        "preview_id": preview["preview_id"],
                        "idempotency_key": "lp167-h-entry",
                    }
                ).encode("utf-8"),
            )
        )
        assert entry_status == 200
        assert entry["state"] == "entry_open"
        session_id = str(entry["session_id"])

        # 新价位追加成功：恰一单 0.28。
        ok_status, ok = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "5",
                        "idempotency_key": "lp167-h-aug-1",
                        "price": "0.28",
                    }
                ).encode("utf-8"),
            )
        )
        assert ok_status == 200
        assert ok["state"] == "entry_open"
        assert ok["augment_order_id"] == "lp-order-2"
        assert Decimal(str(exchange.posts[1]["price"])) == Decimal("0.28")
        assert Decimal(str(exchange.posts[1]["quantity"])) == Decimal("5")

        # 高于同快照顶档买一（0.30）→ price_above_best_bid，无新单。
        above_status, above = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "5",
                        "idempotency_key": "lp167-h-aug-2",
                        "price": "0.50",
                    }
                ).encode("utf-8"),
            )
        )
        assert above_status == 200
        assert above == {
            "state": "rejected",
            "reason": "price_above_best_bid",
        }

        # 组价 0.30（入场单仍在挂）→ price_level_active。
        same_status, same = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "5",
                        "idempotency_key": "lp167-h-aug-3",
                        "price": "0.30",
                    }
                ).encode("utf-8"),
            )
        )
        assert same_status == 200
        assert same == {"state": "rejected", "reason": "price_level_active"}

        # 严格 schema：多余字段 400、缺键 400。
        extra_status, _extra = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "5",
                        "idempotency_key": "lp167-h-aug-4",
                        "extra": True,
                    }
                ).encode("utf-8"),
            )
        )
        assert extra_status == 400
        missing_status, _missing = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=b'{"session_id":"x","quantity":"5"}',
            )
        )
        assert missing_status == 400
        assert len(exchange.posts) == 2

        # /lp/orders 契约不变：多余字段仍 400、无下单。
        orders_extra_status, _orders_extra = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps({**request, "idempotency_key": "lp167-h-orders", "extra": True}).encode("utf-8"),
            )
        )
        assert orders_extra_status == 400
        assert len(exchange.posts) == 2


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
    expected_condition_id = condition_id
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

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool
        ) -> list[object]:
            assert condition_id == expected_condition_id
            del sponsored
            return self.list_current_rewards(sponsored=False)

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

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            assert page_size == 100
            assert tuple(condition_ids) == (condition_id,)  # type: ignore[arg-type]
            return [self.get_market(id="market-1")]

        def get_order_book(self, *, token_id: str) -> object:
            assert token_id in ("0x" + "1" * 64, "no-token")
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

        def get_order_books(self, *, token_ids: object) -> list[object]:
            # Issue #143: a candidate preview reads both token books of the
            # selected market in one call.
            assert tuple(token_ids) == (token_id, "no-token")  # type: ignore[arg-type]
            return [self.get_order_book(token_id=str(t)) for t in token_ids]

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


def test_lp_refresh_reads_risk_books_only_for_selected_markets(tmp_path: Path) -> None:
    initial_now = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    now = [initial_now]
    markets = tuple(f"M{index:02d}" for index in range(1, 52))

    class Exchange:
        def __init__(self) -> None:
            self.book_requests: list[tuple[str, ...]] = []
            self.account_calls = 0

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            requested = tuple(condition_ids) if condition_ids is not None else tuple(
                f"condition-{market_id}" for market_id in markets
            )
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": tuple(
                    {
                        "condition_id": f"condition-{market_id}",
                        "daily_pool_usd": Decimal(700 - index),
                        "reward_active": True,
                    }
                    for index, market_id in enumerate(markets)
                    if f"condition-{market_id}" in requested
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            return {
                condition_id: {
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "market_title": market_id,
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "fees_checked_at": now[0],
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": f"token-{market_id}"}
                    },
                }
                for market_id, condition_id in (
                    (market_id, f"condition-{market_id}") for market_id in markets
                )
                if condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            self.account_calls += 1
            now[0] = initial_now + timedelta(seconds=self.account_calls)
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            now[0] = max(now[0], initial_now) + timedelta(seconds=1)
            self.book_requests.append(tuple(token_ids))
            # Issue 143 + #138 round 2: books are read per ≤10-market
            # batch; any other request set (extra tokens or out-of-batch
            # tokens) must fail.
            batch_index = len(self.book_requests) - 1
            expected = tuple(
                f"token-{market_id}"
                for market_id in markets[batch_index * 10:(batch_index + 1) * 10]
            )
            if tuple(token_ids) != expected:
                raise AssertionError("books requested outside trial candidates")
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": now[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path)
    for market_id in markets:
        store.lp_save_price_history(
            f"condition-{market_id}",
            f"token-{market_id}",
            [],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "checked_at": initial_now,
                "window_start": initial_now - timedelta(hours=24),
                "window_end": initial_now,
            },
        )
    service = PolymarketLPService(
        store,
        exchange,
        clock=lambda: now[0],
    )
    assert service.refresh_price_history()["state"] == "known"
    # Issue #143 + #157: books are read per ≤10-market batch in one call,
    # and each exploration call rolls exactly one batch — five calls cover
    # the first fifty queued markets.
    for start in range(0, 50, 10):
        snapshot = service.refresh_candidates(force=True)
    assert [row["market_id"] for row in snapshot["candidates"]] == list(
        markets[:10]
    )
    assert exchange.book_requests == [
        tuple(f"token-{market_id}" for market_id in markets[start:start + 10])
        for start in range(0, 50, 10)
    ]
    candidate_rows = snapshot["candidates"]
    assert len(candidate_rows) == 10
    assert candidate_rows[0]["market_id"] == markets[0]
    assert all(row["verification"] == "verified" for row in candidate_rows)
    assert all("realtime_capital" in row for row in candidate_rows)
    assert snapshot["funnel"]["read"] == 51
    assert snapshot["funnel"]["base"] == 51
    assert snapshot["funnel"]["sort"] == 51
    # Issue #157: trial equals the whole valid pool (50 passer rows), the
    # published table caps at the top ten.
    assert snapshot["funnel"]["trial"] == 50
    assert snapshot["funnel"]["checked"] == 50
    assert snapshot["funnel"]["passed"] == 50
    assert snapshot["funnel"]["rejected"] == 0
    assert snapshot["funnel"]["unknown"] == 0
    assert snapshot["funnel"]["unchecked"] == 1
    assert snapshot["funnel"]["batches"] == 5
    # The seeded summaries carry no latest_midpoint, so every market queues
    # as backup (no reference price) — each batch still reads one backup.
    assert snapshot["funnel"]["backup_read"] == 50
    assert snapshot["funnel"]["stop_reason"] is None
    assert snapshot["funnel"]["competition_known"] == 0
    assert snapshot["funnel"]["competition_unknown"] == 51
    assert "risk" not in snapshot["funnel"]
    assert snapshot["funnel"]["conditions"] == _lp_funnel_conditions()


def test_lp_refresh_keeps_stale_batches_out_of_current_selection(tmp_path: Path) -> None:
    first_now = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    now = [first_now]

    class Exchange:
        def __init__(self) -> None:
            self.catalog_calls = 0
            self.book_requests: list[tuple[str, ...]] = []

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                requested = set(condition_ids)  # type: ignore[arg-type]
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": now[0],
                    "markets": tuple(
                        {
                            "condition_id": f"condition-M{index}",
                            "daily_pool_usd": Decimal(100 - index),
                            "reward_active": True,
                        }
                        for index in (1, 2)
                        if f"condition-M{index}" in requested
                    ),
                }
            self.catalog_calls += 1
            if self.catalog_calls > 1:
                raise RuntimeError("temporary catalog outage")
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": tuple(
                    {
                        "condition_id": f"condition-M{index}",
                        "daily_pool_usd": Decimal(100 - index),
                        "reward_active": True,
                    }
                    for index in (1, 2)
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            return {
                f"condition-M{index}": {
                    "market_id": f"M{index}",
                    "condition_id": f"condition-M{index}",
                    "accepting_orders": True,
                    "metadata_checked_at": first_now,
                    "fees_checked_at": first_now,
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {"yes": {"label": "YES", "token_id": f"token-M{index}"}},
                }
                for index in (1, 2)
                if f"condition-M{index}" in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            self.book_requests.append(tuple(token_ids))
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": first_now,
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
            }

    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_save_price_history(
            f"condition-M{index}",
            f"token-M{index}",
            [],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "checked_at": first_now,
                "window_start": first_now - timedelta(hours=24),
                "window_end": first_now,
            },
        )
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now[0])
    assert service.refresh_price_history()["state"] == "known"
    first = service.refresh_candidates(force=True)
    assert first["state"] == "ready"
    assert [row["market_id"] for row in first["candidates"]] == ["M1", "M2"]
    first_last_success = first["last_success_at"]
    now[0] = first_now + timedelta(minutes=2)
    failed_preparation = service.refresh_price_history()
    assert failed_preparation["state"] == "unknown"
    stale = service.candidate_snapshot()
    assert stale["state"] == "ready"
    # Issue #157: the pool rows stay valid and current — a preparation
    # outage neither degrades them nor clears the table.
    assert stale["stale"] is False
    assert stale["last_success_at"] == first_last_success
    assert [row["market_id"] for row in stale["recommendations"]] == ["M1"]
    assert stale["selected_market_ids"] == ["M1", "M2"]
    # Issue 143: both queued markets are read in one batch call.
    assert exchange.book_requests == [("token-M1", "token-M2")]

    # A completed batch replaces the previous selection instead of carrying
    # old markets forward when the catalog changes.
    class ReplacementExchange(Exchange):
        def __init__(self) -> None:
            super().__init__()
            self.catalog_calls = 0

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                requested = set(condition_ids)  # type: ignore[arg-type]
                selected = ("M1", "M2", "M3")
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": now[0],
                    "markets": tuple(
                        {
                            "condition_id": f"condition-{market_id}",
                            "daily_pool_usd": Decimal(100 - int(market_id[1:])),
                            "reward_active": True,
                        }
                        for market_id in selected
                        if f"condition-{market_id}" in requested
                    ),
                }
            self.catalog_calls += 1
            selected = ("M1", "M2") if self.catalog_calls == 1 else ("M3",)
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": tuple(
                    {
                        "condition_id": f"condition-{market_id}",
                        "daily_pool_usd": Decimal(100 - int(market_id[1:])),
                        "reward_active": True,
                    }
                    for market_id in selected
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            return {
                f"condition-{market_id}": {
                    "market_id": market_id,
                    "condition_id": f"condition-{market_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "fees_checked_at": now[0],
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": f"token-{market_id}"}
                    },
                }
                for market_id in ("M1", "M2", "M3")
                if f"condition-{market_id}" in condition_ids
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            self.book_requests.append(tuple(token_ids))
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": now[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

    replacement_store = PredictionArbitrageStore(tmp_path / "replacement")
    for index in (1, 2, 3):
        replacement_store.lp_save_price_history(
            f"condition-M{index}",
            f"token-M{index}",
            [],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "checked_at": first_now,
                "window_start": first_now - timedelta(hours=24),
                "window_end": first_now,
            },
        )
    replacement_exchange = ReplacementExchange()
    replacement_service = PolymarketLPService(
        replacement_store, replacement_exchange, clock=lambda: now[0]
    )
    assert replacement_service.refresh_price_history()["state"] == "known"
    first_replacement = replacement_service.refresh_candidates(force=True)
    assert first_replacement["selected_market_ids"] == ["M1", "M2"]
    now[0] = first_now + timedelta(minutes=3)
    assert replacement_service.refresh_price_history()["state"] == "known"
    second_replacement = replacement_service.refresh_candidates(force=True)
    # Issue #157 rotation: the second batch takes the never-tried M3 plus
    # the oldest-tried tail, and the pool keeps the earlier valid estimates.
    assert [row["market_id"] for row in second_replacement["candidates"]] == [
        "M1", "M2", "M3",
    ]
    assert second_replacement["selected_market_ids"] == ["M1", "M2", "M3"]
    assert len(replacement_exchange.book_requests) == 2
    assert set(replacement_exchange.book_requests[0]) == {"token-M1", "token-M2"}
    # At +3min the catalog offers only M3: the exploration batch reads it
    # alone, while the still-valid M1/M2 pool rows remain displayed.
    assert set(replacement_exchange.book_requests[1]) == {"token-M3"}

    # A legacy snapshot has no funnel contract and cannot be restored as a
    # successful result from the new flow.
    legacy_store = PredictionArbitrageStore(tmp_path / "legacy")
    legacy_store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "scanning": False,
            "candidates": [{"market_id": "legacy-market"}],
            "recommendations": [{"market_id": "legacy-market", "state": "eligible"}],
            "checked_at": first_now,
            "last_success_at": first_now,
            "last_attempt_at": first_now,
            "candidate_rows_fresh": True,
            "scan_started_at": first_now.isoformat(),
        }
    )
    legacy_service = PolymarketLPService(
        legacy_store, replacement_exchange, clock=lambda: now[0]
    )
    legacy_snapshot = legacy_service.candidate_snapshot()
    assert legacy_snapshot["state"] in {"unknown", "incomplete"}
    assert legacy_snapshot["complete"] is False
    assert legacy_snapshot["candidate_rows_fresh"] is False
    assert legacy_snapshot["selected_market_ids"] == []
    # Issue #157: the continuous funnel is always fully populated.
    assert legacy_snapshot["funnel"]["checked"] == 0
    assert legacy_snapshot["funnel"]["reasons"] == {
        "read": [], "base": [], "sort": [], "trial": [],
    }

    class PartialExchange(ReplacementExchange):
        def __init__(self) -> None:
            super().__init__()
            self.account_failure = False
            self.book_failure = False

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                requested = set(condition_ids)  # type: ignore[arg-type]
                if "condition-M4" not in requested:
                    return {
                        "state": "known",
                        "complete": True,
                        "checked_at": now[0],
                        "markets": (),
                    }
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": now[0],
                    "markets": (
                        {
                            "condition_id": "condition-M4",
                            "daily_pool_usd": Decimal("96"),
                            "reward_active": True,
                        },
                    ),
                }
            return {
                "state": "known",
                "complete": False,
                "checked_at": now[0],
                "markets": (
                    {
                        "condition_id": "condition-M4",
                        "daily_pool_usd": Decimal("96"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            if "condition-M4" not in condition_ids:
                return {}
            return {
                "condition-M4": {
                    "market_id": "M4",
                    "condition_id": "condition-M4",
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "fees_checked_at": now[0],
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "token-M4"}
                    },
                }
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            if self.account_failure:
                raise RuntimeError("temporary account outage")
            return super().lp_account_snapshot()

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            if self.book_failure:
                raise RuntimeError("temporary books outage")
            return super().lp_order_books(token_ids, stop_event=stop_event)

    partial_store = PredictionArbitrageStore(tmp_path / "partial")
    partial_store.lp_save_price_history(
        "condition-M4",
        "token-M4",
        [],
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": first_now,
            "window_start": first_now - timedelta(hours=24),
            "window_end": first_now,
        },
    )
    partial_exchange = PartialExchange()
    partial_service = PolymarketLPService(
        partial_store, partial_exchange, clock=lambda: now[0]
    )
    partial_preparation = partial_service.refresh_price_history()
    assert partial_preparation["state"] == "known"
    assert partial_preparation["preparation_outcome"] == "failure"
    partial_preparation_state = partial_preparation["preparation"]
    assert isinstance(partial_preparation_state, Mapping)
    assert partial_preparation_state["state"] == "waiting_retry"
    assert partial_preparation_state["failure_count"] == 1
    assert partial_preparation_state["next_retry_at"] == (
        (now[0] + timedelta(seconds=300))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    partial = partial_service.refresh_candidates(force=True)
    assert partial["state"] == "incomplete"
    assert partial["complete"] is False
    assert [row["market_id"] for row in partial["candidates"]] == ["M4"]
    assert partial["funnel"]["read"] == 1
    assert partial["funnel"]["base"] == 1
    assert partial["funnel"]["sort"] == 1
    assert partial["funnel"]["trial"] == 1
    assert "risk" not in partial["funnel"]

    partial_exchange.account_failure = True
    partial_failed = partial_service.refresh_candidates(force=True)
    # Issue #143 decision 5: an account outage ends the batch before any
    # book read.  Issue #157: the still-valid pool row stays published and
    # the incomplete catalog keeps the snapshot marked incomplete.
    assert partial_failed["state"] == "incomplete"
    assert partial_failed["complete"] is False
    assert [row["market_id"] for row in partial_failed["recommendations"]] == ["M4"]
    assert [row["market_id"] for row in partial_failed["candidates"]] == ["M4"]
    # Issue #157 + #143 decision 5 rationale: the account gate fires only
    # when the cached account receipt is missing/expired.  While the cached
    # receipt is inside its 60-second window the batch legitimately trusts
    # it (#143 decision 5: don't consume batch reads on an unusable account —
    # a usable cached account is not unusable), so no stop note is written
    # on this call.
    assert partial_failed["funnel"]["stop_reason"] is None
    assert partial_failed["funnel"]["checked"] == 2
    assert partial_failed["funnel"]["trial"] == 1

    partial_exchange.account_failure = False
    partial_exchange.book_failure = True
    partial_books_failed = partial_service.refresh_candidates(force=True)
    # Books outage: the market cannot be live-qualified, so it is accounted
    # unknown with a book reason and stays unpublished (issue #143).
    assert partial_books_failed["state"] == "incomplete"
    assert partial_books_failed["funnel"]["read"] == 1
    assert partial_books_failed["funnel"]["base"] == 1
    assert partial_books_failed["funnel"]["sort"] == 1
    # Issue #157: the failed re-estimate keeps the stored row (marked
    # refresh_failed); the trial stage reflects the whole valid pool and the
    # counters are continuous across batches.
    assert partial_books_failed["funnel"]["trial"] == 1
    assert partial_books_failed["funnel"]["checked"] == 3
    assert partial_books_failed["funnel"]["unknown"] == 1
    assert partial_books_failed["candidates"][0]["refresh_failed"] is True
    assert partial_books_failed["recommendations"][0]["market_id"] == "M4"
    unknown_codes = [
        row["code"] for row in partial_books_failed["funnel"]["reasons"]["trial"]
    ]
    assert unknown_codes.count("book_unknown") == 1


def test_lp_candidate_refresh_api_shares_round_and_preview_counts_separately(
    tmp_path: Path,
) -> None:
    """S8: the refresh endpoint only wakes the shared round; a preview reads
    only the selected market's two tokens, separate from scan counting, and
    never swaps the confirmed request identity for the recommendation."""
    from tests.test_polymarket_lp import (
        _LPBatchQueryExchange,
        _seed_stale_backup_summaries,
    )

    class PreviewExchange(_LPBatchQueryExchange):
        def lp_market_metadata_fresh(self, condition_ids, *, stop_event=None):
            return self.lp_market_metadata(condition_ids, stop_event=stop_event)

    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    pools = {"P1": Decimal(400), "P2": Decimal(300)}
    exchange = PreviewExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] in {"known", "partial"}
    scan = lp.refresh_candidates(force=True)
    reads_after_scan = len(exchange.book_token_reads)
    scan_checked = scan["funnel"]["checked"]

    class Execution:
        dashboard_reads = 0

        def lp_dashboard(self) -> dict[str, object]:
            Execution.dashboard_reads += 1
            return {
                "state": "ready",
                "stale": False,
                "complete": True,
                "candidates": scan["candidates"],
                "recommendations": scan["recommendations"],
                "selected_results": scan["selected_results"],
                "funnel": scan["funnel"],
                "orders": [],
                "positions": [],
            }

        def lp_candidate_preview(
            self, payload: Mapping[str, object]
        ) -> dict[str, object]:
            return lp.preview_candidate(payload)

    class Runtime:
        mode = "production"
        state = "RUNNING"
        production_owner = True

        def __init__(self) -> None:
            self.execution = Execution()
            self.refresh_requests = 0

        def queue_lp_candidate_refresh(self) -> bool:
            self.refresh_requests += 1
            return True

    runtime = Runtime()
    refresh_path = "/api/prediction-arbitrage/lp/candidates/refresh"
    preview_path = "/api/prediction-arbitrage/lp/candidates/preview"
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        status, queued = _response(_production_request(base, refresh_path, b"{}"))
        assert status == 202
        assert queued == {"state": "queued"}
        assert runtime.refresh_requests == 1

        # Page reads are served from the cached projection: repeated dashboards
        # never amplify into external candidate reads.
        for _ in range(3):
            dashboard_status, _dashboard = _response(
                base + "/api/prediction-arbitrage/lp/dashboard"
            )
            assert dashboard_status == 200
        assert Execution.dashboard_reads == 3
        assert len(exchange.book_token_reads) == reads_after_scan

        # A preview re-checks only the requested market (both of its token
        # books in one read) and keeps the confirmed identity even though the
        # scan's current recommendation points elsewhere.
        preview_payload = {
            "market_id": "market-P2",
            "condition_id": "condition-P2",
            "token_id": "token-condition-P2-no",
            "outcome": "NO",
        }
        status, previewed = _response(
            _production_request(
                base, preview_path, json.dumps(preview_payload).encode("utf-8")
            )
        )
        assert status == 200
        assert previewed["state"] == "previewed"
        request = previewed["request"]
        assert request["condition_id"] == "condition-P2"
        assert request["token_id"] == "token-condition-P2-no"
        assert request["outcome"] == "NO"
        new_reads = exchange.book_token_reads[reads_after_scan:]
        assert len(new_reads) == 1
        assert set(new_reads[0]) == {
            "token-condition-P2-yes",
            "token-condition-P2-no",
        }
        # Preview reads never touch the scan's progress accounting.
        current_snapshot = lp.candidate_snapshot()
        assert current_snapshot["funnel"]["checked"] == scan_checked
        assert runtime.refresh_requests == 1


def test_lp_candidate_snapshot_reads_are_pure_and_never_degrade(
    tmp_path: Path,
) -> None:
    first_now = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
    now = [first_now]

    class Exchange:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {
                "catalog": 0,
                "metadata": 0,
                "history": 0,
                "account": 0,
                "books": 0,
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.calls["catalog"] += 1
            if condition_ids is not None and "condition-minute" not in set(condition_ids):  # type: ignore[arg-type]
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": now[0],
                    "markets": (),
                }
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": (
                    {
                        "condition_id": "condition-minute",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.calls["metadata"] += 1
            if "condition-minute" not in condition_ids:
                return {}
            return {
                "condition-minute": {
                    "market_id": "market-minute",
                    "condition_id": "condition-minute",
                    "market_title": "Minute expiry market",
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "fees_checked_at": now[0],
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": "token-minute",
                        }
                    },
                }
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            self.calls["history"] += 1
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": Decimal("0.50")},
                        {"t": end_ts, "p": Decimal("0.505")},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            self.calls["account"] += 1
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": first_now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.calls["books"] += 1
            return {
                token_id: {
                    "condition_id": "condition-minute",
                    "token_id": token_id,
                    "received_at": first_now,
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_price_history(
        "condition-minute",
        "token-minute",
        [],
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": first_now,
            "window_start": first_now - timedelta(hours=24),
            "window_end": first_now,
            "valid_until": first_now + timedelta(hours=2),
        },
    )
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now[0])

    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"
    first = service.refresh_candidates(force=True)
    assert first["state"] == "ready"
    assert first["complete"] is True
    assert first["selected_market_ids"] == ["market-minute"]
    assert first["funnel"]["trial"] == 1
    assert "risk" not in first["funnel"]
    first_checked_at = first["checked_at"]
    first_funnel = first["funnel"]
    first_calls = dict(exchange.calls)

    now[0] = first_now + timedelta(seconds=59)
    fresh = service.candidate_snapshot()
    assert fresh["stale"] is False
    assert fresh["checked_at"] == first_checked_at
    assert fresh["selected_market_ids"] == ["market-minute"]
    assert fresh["funnel"] == first_funnel
    assert len(fresh["recommendations"]) == 1
    assert fresh["recommendations"][0]["state"] == "eligible"
    assert exchange.calls == first_calls

    # Issue #157: the 60-second whole-snapshot staleness is retired.  Reads
    # at and past the old boundary stay pure — no degrade, no refresh — and
    # the row remains the current recommendation until its own expiry.
    for offset in (60, 61):
        now[0] = first_now + timedelta(seconds=offset)
        historical = service.candidate_snapshot()
        assert historical["stale"] is False
        assert historical["checked_at"] == first_checked_at
        assert historical["selected_market_ids"] == ["market-minute"]
        assert historical["funnel"] == first_funnel
        assert [row["market_id"] for row in historical["candidates"]] == [
            "market-minute"
        ]
        assert len(historical["recommendations"]) == 1
        assert exchange.calls == first_calls


def test_lp_refresh_confirmed_empty_catalog_preserves_funnel_rules(
    tmp_path: Path,
) -> None:
    first_now = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)

    class EmptyExchange:
        def __init__(self) -> None:
            self.catalog_calls = 0
            self.unexpected_calls: list[str] = []

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.catalog_calls += 1
            return {
                "state": "known",
                "complete": True,
                "checked_at": first_now,
                "markets": (),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            if not condition_ids:
                return {}
            self.unexpected_calls.append("metadata")
            raise AssertionError("empty catalog must not read market metadata")

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del token_ids, start_ts, end_ts, fidelity, stop_event
            return {"state": "unknown", "history": {}}

        def lp_account_snapshot(self) -> dict[str, object]:
            self.unexpected_calls.append("account")
            raise AssertionError("empty catalog must not read account facts")

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del token_ids, stop_event
            self.unexpected_calls.append("books")
            raise AssertionError("empty catalog must not read order books")

    store = PredictionArbitrageStore(tmp_path)
    exchange = EmptyExchange()
    service = PolymarketLPService(store, exchange, clock=lambda: first_now)

    prepared = service.refresh_price_history()
    assert prepared["target_count"] == 0
    snapshot = service.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    assert snapshot["complete"] is True
    assert snapshot["selected_market_ids"] == []
    assert snapshot["selected_results"] == []
    assert snapshot["candidates"] == []
    assert snapshot["recommendations"] == []
    funnel = snapshot["funnel"]
    assert funnel["read"] == 0
    assert funnel["base"] == 0
    assert funnel["sort"] == 0
    assert funnel["trial"] == 0
    assert "risk" not in funnel
    assert funnel["conditions"] == _lp_funnel_conditions()
    assert funnel["reasons"] == {"read": [], "base": [], "sort": [], "trial": []}
    assert exchange.catalog_calls == 1
    assert exchange.unexpected_calls == []


def test_lp_history_uses_complete_minute_window(tmp_path: Path) -> None:
    now = datetime(2026, 9, 18, 12, 34, 16, tzinfo=UTC)
    window_end = datetime(2026, 9, 18, 12, 34, tzinfo=UTC)
    window_end_ts = int(window_end.timestamp())
    window_start_ts = window_end_ts - 86400
    full_rows = [
        {
            "t": window_start_ts + 12 + index * 60,
            "p": "0.40" if index == 0 else "0.60",
        }
        for index in range(1440)
    ]
    short_rows = [
        {"t": window_end_ts - 3600 + 12, "p": "0.50"},
        {"t": window_end_ts - 3540 + 12, "p": "0.55"},
    ]

    class Exchange:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": (
                    {
                        "condition_id": "condition-full",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                    {
                        "condition_id": "condition-short",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            tokens = {
                "condition-full": "token-full",
                "condition-short": "token-short",
            }
            return {
                condition_id: {
                    "accepting_orders": True,
                    "outcomes": {"yes": {"token_id": tokens[condition_id]}},
                }
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.history_calls.append(
                {
                    "token_ids": token_ids,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                }
            )
            return {
                "state": "known",
                "history": {
                    "token-full": full_rows,
                    "token-short": short_rows,
                },
            }

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    prepared = service.refresh_price_history()

    assert prepared["state"] == "partial"
    assert prepared["updated_count"] == 1
    assert prepared["unknown_count"] == 1
    assert exchange.history_calls == [
        {
            "token_ids": ("token-full", "token-short"),
            "start_ts": window_start_ts,
            "end_ts": window_end_ts,
            "fidelity": 1,
        }
    ]
    full_summary = store.lp_price_history_summary(
        "condition-full", "token-full", now=now
    )
    assert full_summary is not None
    assert full_summary["state"] == "known"
    assert full_summary["window_start"] == "2026-09-17T12:34:00.000000Z"
    assert full_summary["window_end"] == "2026-09-18T12:34:00.000000Z"
    assert full_summary["checked_at"] == "2026-09-18T12:34:16.000000Z"
    assert full_summary["sample_count"] == 1440
    short_summary = store.lp_price_history_summary(
        "condition-short", "token-short", now=now
    )
    assert short_summary is not None
    assert short_summary["state"] == "unknown"
    assert short_summary.get("checked_at") is None


def test_lp_history_preserves_upstream_error_codes(tmp_path: Path) -> None:
    now = datetime(2026, 9, 18, 12, 34, 16, tzinfo=UTC)
    window_end_ts = int(datetime(2026, 9, 18, 12, 34, tzinfo=UTC).timestamp())
    window_start_ts = window_end_ts - 86400

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": (
                    {
                        "condition_id": "condition-good",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                    {
                        "condition_id": "condition-failed",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            tokens = {
                "condition-good": "token-good",
                "condition-failed": "token-failed",
            }
            return {
                condition_id: {
                    "accepting_orders": True,
                    "outcomes": {"yes": {"token_id": tokens[condition_id]}},
                }
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del token_ids, fidelity, stop_event
            return {
                "state": "partial",
                "history": {
                    "token-good": [
                        {"t": start_ts + 12, "p": "0.40"},
                        {"t": end_ts - 48, "p": "0.60"},
                    ]
                },
                "errors": {"token-failed": "TimeoutError"},
            }

    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, Exchange(), clock=lambda: now)

    prepared = service.refresh_price_history()

    assert prepared["state"] == "partial"
    assert prepared["updated_count"] == 1
    assert prepared["unknown_count"] == 1
    assert prepared["errors"] == {"token-failed": "TimeoutError"}
    good_summary = store.lp_price_history_summary(
        "condition-good", "token-good", now=now
    )
    assert good_summary is not None
    assert good_summary["state"] == "known"
    failed_summary = store.lp_price_history_summary(
        "condition-failed", "token-failed", now=now
    )
    assert failed_summary is not None
    assert failed_summary["state"] == "unknown"
    assert failed_summary.get("checked_at") is None
    assert failed_summary["last_error"] == "TimeoutError"


def test_lp_history_progress_does_not_block_ready_candidates(tmp_path: Path) -> None:
    """Published catalog facts let cached directions finish while history waits."""

    now = datetime(2026, 9, 18, 12, 34, 16, tzinfo=UTC)
    window_start = now.replace(second=0, microsecond=0) - timedelta(hours=24)
    history_started = threading.Event()
    release_history = threading.Event()

    def reward(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "daily_pool_usd": Decimal("100"),
            "reward_active": True,
            "rewards_min_size": Decimal("20"),
            "rewards_max_spread": Decimal("10"),
            "checked_at": now,
        }

    def market(condition_id: str, token_id: str) -> dict[str, object]:
        return {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "accepting_orders": True,
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "minimum_order_size": Decimal("1"),
            "tick_size": Decimal("0.01"),
            "fees_enabled": False,
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
            "metadata_checked_at": now,
            "fees_checked_at": now,
            "outcomes": {"yes": {"label": "YES", "token_id": token_id}},
        }

    class Exchange:
        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            ids = condition_ids or ("condition-ready", "condition-waiting")
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": tuple(reward(condition_id) for condition_id in ids),
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            token_by_condition = {
                "condition-ready": "token-ready",
                "condition-waiting": "token-waiting",
            }
            return {
                condition_id: market(condition_id, token_by_condition[condition_id])
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_started.set()
            assert release_history.wait(timeout=5)
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.500"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("1000"),
                "allowance": Decimal("1000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                token_id: {
                    "condition_id": (
                        "condition-ready"
                        if token_id == "token-ready"
                        else "condition-waiting"
                    ),
                    "token_id": token_id,
                    "received_at": now,
                    # Exit-liquidity rule: the stress exit needs depth beyond
                    # the top bid level.
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                }
                for token_id in token_ids
            }

    store = PredictionArbitrageStore(tmp_path)
    checked_at = now - timedelta(minutes=5)
    valid_until = now + timedelta(hours=1)
    store.lp_save_price_history(
        "condition-ready",
        "token-ready",
        [],
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": checked_at,
            "valid_until": valid_until,
            "window_start": window_start,
            "window_end": window_start + timedelta(hours=24),
            "sample_count": 1440,
        },
    )
    service = PolymarketLPService(store, Exchange(), clock=lambda: now)
    refresh_result: dict[str, object] = {}

    def refresh() -> None:
        refresh_result.update(service.refresh_price_history())

    worker = threading.Thread(target=refresh)
    worker.start()
    assert history_started.wait(timeout=2)
    preparation = service.preparation_snapshot()
    assert preparation["state"] == "preparing"
    assert preparation["completed_count"] == 1
    assert preparation["total_count"] == 2

    snapshot = service.refresh_candidates(force=True)
    ready = next(
        row for row in snapshot["candidates"] if row["condition_id"] == "condition-ready"
    )
    assert ready["market_id"] == "market-condition-ready"
    assert any(
        row["condition_id"] == "condition-waiting"
        and row["code"] == "history_summary_unknown"
        for row in snapshot["funnel"]["reasons"]["base"]
    )
    retained = store.lp_price_history_summary(
        "condition-ready", "token-ready", now=now
    )
    assert retained is not None
    assert retained["checked_at"] == checked_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert retained["valid_until"] == valid_until.isoformat(timespec="microseconds").replace("+00:00", "Z")

    release_history.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert refresh_result["preparation_outcome"] == "success"


def test_lp_dashboard_reports_preparation_and_manual_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authenticated LP dashboard exposes durable preparation state."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
    catalog_calls: list[datetime] = []
    catalog_started = threading.Event()
    catalog_release = threading.Event()
    retry_waiting = threading.Event()
    retry_release = threading.Event()
    paused_waiting = threading.Event()
    ready_waiting = threading.Event()
    recovered = threading.Event()
    notifications: list[tuple[str, str]] = []
    runtime_holder: list[object] = []

    class Trading:
        fail_catalog = True
        catalog_error = "TimeoutError"

        def attach_metadata_cache(self, _store: object) -> None:
            pass

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": clock[0],
                    "markets": (),
                }
            catalog_calls.append(clock[0])
            if self.fail_catalog:
                if len(catalog_calls) == 1:
                    catalog_started.set()
                    assert catalog_release.wait(timeout=5)
                clock[0] += timedelta(seconds=75)
                if self.catalog_error == "certificate_error":
                    raise ssl.SSLCertVerificationError("catalog certificate invalid")
                raise TimeoutError("catalog body unavailable")
            recovered.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": (),
            }

        def lp_market_metadata(
            self, _condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {}

        def lp_price_history(
            self,
            _token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            return {"state": "known", "history": {}}

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": clock[0],
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, _token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {}

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x" + "1" * 40,
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": clock[0],
            }

        def close(self) -> None:
            pass

    class Monitor:
        def __init__(self, **_: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_auto_eat_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))

    class Execution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

    trading = Trading()
    config = SimpleNamespace(
        signer_address="0x" + "1" * 40,
        wallet_address="0x" + "2" * 40,
        predict=None,
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", Monitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", Execution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        if seconds in {60, 300}:
            runtime = runtime_holder[0] if runtime_holder else None
            lp = getattr(runtime, "lp", None)
            preparation_snapshot = getattr(lp, "preparation_snapshot", None)
            preparation = (
                preparation_snapshot() if callable(preparation_snapshot) else {}
            )
            if (
                trading.fail_catalog
                and trading.catalog_error == "certificate_error"
                and isinstance(preparation, Mapping)
                and preparation.get("state") == "paused"
            ):
                paused_waiting.set()
                while not stop_event.is_set():
                    wake = getattr(runtime, "_history_wakeup_event", None)
                    if wake is not None and wake.wait(timeout=0.01):
                        wake.clear()
                        return False
                return True
            retry_waiting.set()
            while not retry_release.wait(timeout=0.01):
                if stop_event.is_set():
                    return True
            retry_release.clear()
            clock[0] += timedelta(seconds=seconds)
            return False
        if seconds >= 3600:
            if trading.fail_catalog:
                paused_waiting.set()
            else:
                ready_waiting.set()
            while not stop_event.is_set():
                runtime = runtime_holder[0] if runtime_holder else None
                wake = getattr(runtime, "_history_wakeup_event", None)
                if wake is not None and wake.wait(timeout=0.01):
                    wake.clear()
                    return False
            return True
        raise AssertionError(f"unexpected history wait: {seconds}")

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(_notifiers=(Feishu(),)),
        cross_venue_monitor=runtime_module._UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime_holder.append(runtime)
    runtime.start()
    try:
        assert runtime.lp is not None
        assert catalog_started.wait(timeout=2)
        assert runtime.lp.preparation_snapshot()["state"] == "preparing"
        assert catalog_calls == [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
        with _running_server(
            runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (base, _server_instance):
            # Issue #146: refresh the background snapshot before serving.
            runtime.execution.refresh_lp_dashboard_snapshot()
            initial_status, initial = _response(
                base + "/api/prediction-arbitrage/lp/dashboard"
            )
            assert initial_status == 200
            assert initial["preparation"]["state"] == "preparing"
            assert initial["preparation"]["stage"] == "catalog"
            assert initial["recommendations"] == []
            assert initial["selected_results"] == []

            catalog_release.set()
            assert retry_waiting.wait(timeout=2)
            # Issue #146: refresh the background snapshot before serving.
            runtime.execution.refresh_lp_dashboard_snapshot()
            waiting_status, waiting = _response(
                base + "/api/prediction-arbitrage/lp/dashboard"
            )
            assert waiting_status == 200
            assert waiting["preparation"]["state"] == "waiting_retry"
            assert waiting["preparation"]["next_retry_at"] == "2026-09-18T12:06:15.000000Z"

            ordinary_status, ordinary = _response(
                _production_request(
                    base,
                    "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b"{}",
                )
            )
            assert ordinary_status == 202
            assert ordinary == {"state": "queued"}
            assert runtime.lp.preparation_snapshot()["state"] == "waiting_retry"

            invalid_type_status, _ = _response(
                _production_request(
                    base,
                    "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b'{"manual_recovery":"true"}',
                )
            )
            assert invalid_type_status == 400
            missing_csrf_headers = {
                "Content-Type": "application/json",
                "Cookie": "ot_prediction_session=session-token",
                "Origin": base,
            }
            missing_csrf_status = _status(
                Request(
                    base + "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b'{"manual_recovery":true}',
                    headers=missing_csrf_headers,
                    method="POST",
                )
            )
            assert missing_csrf_status == 403

            def release_retry_probe() -> None:
                retry_waiting.clear()
                retry_release.set()
                assert retry_waiting.wait(timeout=2)

            for _ in range(4):
                release_retry_probe()

            trading.catalog_error = "certificate_error"
            retry_waiting.clear()
            retry_release.set()
            assert paused_waiting.wait(timeout=2)
            # Issue #146: refresh the background snapshot before serving.
            pause_deadline = time.monotonic() + 2
            while True:
                runtime.execution.refresh_lp_dashboard_snapshot()
                paused_status, paused = _response(
                    base + "/api/prediction-arbitrage/lp/dashboard"
                )
                if (
                    paused_status == 200
                    and paused["preparation"]["state"] == "paused"
                    and len(notifications) == 1
                ):
                    break
                if time.monotonic() >= pause_deadline:
                    raise AssertionError("paused LP dashboard state was not published")
                time.sleep(0.01)
            assert paused_status == 200
            assert paused["preparation"]["state"] == "paused"
            assert paused["preparation"]["failure_count"] == 2
            assert paused["preparation"]["last_error"] == "SSLCertVerificationError"
            assert len(notifications) == 1
            expected_recovery_at = clock[0]
            assert expected_recovery_at == datetime(
                2026, 9, 18, 12, 7, 30, tzinfo=UTC
            )
            state_status, _ = _response(
                base + "/api/prediction-arbitrage/state"
            )
            assert state_status == 409

            trading.fail_catalog = False
            recovery_status, recovery = _response(
                _production_request(
                    base,
                    "/api/prediction-arbitrage/lp/candidates/refresh",
                    data=b'{"manual_recovery":true}',
                )
            )
            assert recovery_status == 200
            assert recovery["paused"] is False
            assert recovered.wait(timeout=2)
            assert ready_waiting.wait(timeout=2)
            # Issue #146: refresh the background snapshot before serving.
            runtime.execution.refresh_lp_dashboard_snapshot()
            ready_status, ready = _response(
                base + "/api/prediction-arbitrage/lp/dashboard"
            )
            assert ready_status == 200
            assert ready["preparation"]["state"] == "ready"
            assert ready["preparation"]["stage"] == "complete"
            assert ready["preparation"]["last_success_at"] == (
                expected_recovery_at.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                )
            )
            assert ready["preparation"]["total_count"] == 0
            assert ready["recommendations"] == []
            assert ready["selected_results"] == []
    finally:
        catalog_release.set()
        retry_release.set()
        if runtime.state not in {"NEW", "STOPPED", "FAILED"}:
            runtime.stop()
    assert runtime.state == "STOPPED"


def test_lp_price_history_updates_incrementally_and_expires(tmp_path: Path) -> None:
    first_now = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    now = [first_now]

    class Exchange:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []
            self.fail_history = False

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": (
                    {
                        "condition_id": "condition-M1",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            if "condition-M1" not in condition_ids:
                return {}
            return {
                "condition-M1": {
                    "market_id": "M1",
                    "condition_id": "condition-M1",
                    "accepting_orders": True,
                    "metadata_checked_at": now[0],
                    "outcomes": {"yes": {"label": "YES", "token_id": "token-M1"}},
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                }
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            self.history_calls.append(
                {
                    "token_ids": token_ids,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                }
            )
            if self.fail_history:
                raise RuntimeError("temporary history outage")
            if len(self.history_calls) == 1:
                return {
                    "state": "known",
                    "history": {
                        "token-M1": [
                            {"t": int((first_now - timedelta(hours=24)).timestamp()), "p": Decimal("0.80")},
                            {"t": int((first_now - timedelta(hours=23)).timestamp()), "p": Decimal("0.500")},
                            {"t": int(first_now.timestamp()), "p": Decimal("0.500")},
                        ]
                    },
                }
            return {
                "state": "known",
                "history": {
                    "token-M1": [
                        {"t": start_ts, "p": Decimal("0.500")},
                        {"t": int(now[0].timestamp()), "p": Decimal("0.505")},
                    ]
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                "token-M1": {
                    "condition_id": "condition-M1",
                    "token_id": "token-M1",
                    "received_at": now[0],
                    "bids": [{"price": Decimal("0.50"), "size": Decimal("20")}],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for _ in token_ids
            }

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now[0])

    first = service.refresh_price_history()
    assert first["state"] == "known"
    assert len(exchange.history_calls) == 1
    first_call = exchange.history_calls[0]
    assert first_call["start_ts"] == int((first_now - timedelta(hours=24)).timestamp())
    first_summary = store.lp_price_history_summary("condition-M1", "token-M1", now=first_now)
    assert first_summary is not None
    assert Decimal(str(first_summary["amplitude"])) == Decimal("0.300")

    now[0] = first_now + timedelta(hours=25)
    second = service.refresh_price_history()
    assert second["state"] == "known"
    assert len(exchange.history_calls) == 2
    second_call = exchange.history_calls[1]
    assert second_call["start_ts"] == int(
        (first_now + timedelta(hours=1)).timestamp()
    )
    samples = store.lp_price_history_samples("condition-M1", "token-M1")
    assert [Decimal(str(row["p"])) for row in samples] == [
        Decimal("0.500"),
        Decimal("0.505"),
    ]
    second_summary = store.lp_price_history_summary("condition-M1", "token-M1", now=now[0])
    assert second_summary is not None
    assert Decimal(str(second_summary["amplitude"])) == Decimal("0.005")
    assert str(second_summary["checked_at"]).startswith(
        now[0].isoformat().replace("+00:00", "")
    )

    now[0] = first_now + timedelta(hours=48, minutes=59, seconds=59)
    before_candidates = len(exchange.history_calls)
    usable = service.refresh_candidates(force=True)
    assert len(exchange.history_calls) == before_candidates
    assert usable["funnel"]["base"] == 1

    exchange.fail_history = True
    now[0] = first_now + timedelta(hours=49)
    failed = service.refresh_price_history()
    # f78562c6 classifies by usable coverage: the only identity failed, so
    # nothing is usable and the read is "unknown" (not "partial").
    assert failed["state"] == "unknown"
    assert len(exchange.history_calls) == 3
    preserved = store.lp_price_history_summary("condition-M1", "token-M1", now=now[0])
    assert preserved is not None
    assert str(preserved["checked_at"]).startswith(
        (first_now + timedelta(hours=25)).isoformat().replace("+00:00", "")
    )
    assert Decimal(str(preserved["amplitude"])) == Decimal("0.005")

    history_after_failure = len(exchange.history_calls)
    expired = service.refresh_candidates(force=True)
    assert len(exchange.history_calls) == history_after_failure
    assert expired["funnel"]["base"] == 0


def test_lp_share_watch_route_is_retired_and_posts_404() -> None:
    with _production_server() as (base, runtime):
        status, payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/share-watch",
                data=b'{"condition_id":"condition-a","enabled":true}',
            )
        )

    assert status == 404
    assert payload == {"error": "not found"}
    assert runtime.execution.calls == []


def test_lp_observations_attach_both_side_book_shares(tmp_path: Path) -> None:
    condition_id = "condition-book-share"

    def order(
        order_id: str,
        token_id: str,
        outcome: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> dict[str, object]:
        return {
            "id": order_id,
            "order_id": order_id,
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": outcome,
            "side": side,
            "status": "LIVE",
            "price": price,
            "original_size": quantity,
            "size_matched": Decimal("0"),
            "remaining_size": quantity,
            "reward_min_size": Decimal("40"),
            "fees_enabled": False,
            "market_title": "Book share market",
        }

    state_orders = [
        order(
            "share-buy", "token-share", "YES", "BUY", Decimal("1000"), Decimal("0.50")
        ),
        order(
            "share-sell", "token-share", "YES", "SELL", Decimal("100"), Decimal("0.55")
        ),
        order(
            "bare-buy", "token-nobook", "NO", "BUY", Decimal("20"), Decimal("0.40")
        ),
    ]

    class BookShareTrading:
        config = SimpleNamespace(wallet_address="0x" + "1" * 40)

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": tuple(state_orders),
                "positions": (),
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_rates(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "markets": {
                    condition_id: {
                        "state": "known",
                        "hourly_reward_usd": Decimal("0.05"),
                        "currency": "USD",
                        "checked_at": datetime.now(UTC),
                        "sources": ("native",),
                        "native": {
                            "state": "known",
                            "earning_percentage": Decimal("1"),
                            "daily_pool_usd": Decimal("1.2"),
                            "hourly_reward_usd": Decimal("0.05"),
                            "currency": "USD",
                        },
                    }
                },
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...]
        ) -> dict[str, dict[str, object]]:
            books: dict[str, dict[str, object]] = {}
            for token in token_ids:
                if token == "token-share":
                    books[token] = {
                        "condition_id": condition_id,
                        "token_id": token,
                        "received_at": datetime.now(UTC),
                        "bids": [
                            {"price": Decimal("0.50"), "size": Decimal("1000")},
                            {"price": Decimal("0.45"), "size": Decimal("7050")},
                        ],
                        "asks": [
                            {"price": Decimal("0.55"), "size": Decimal("4200")},
                        ],
                    }
            return books

    service, _trading, _store, _monitor = execution_fixture(tmp_path)
    service._trading = BookShareTrading()

    market = service.refresh_lp_observations()["observations"][condition_id]
    directions = {row["outcome"]: row for row in market["risk_directions"]}
    # bids 1000 + 7050 = 8050 with own buy 1000; asks 4200 with own sell 100.
    assert directions["YES"]["book_shares"]["BUY"] == {
        "own_side_quantity": "1000",
        "side_total_quantity": "8050",
        "book_share_pct": "12.42",
    }
    assert directions["YES"]["book_shares"]["SELL"] == {
        "own_side_quantity": "100",
        "side_total_quantity": "4200",
        "book_share_pct": "2.38",
    }
    # A missing order book keeps every field None instead of faking zero.
    assert directions["NO"]["book_shares"]["BUY"] == {
        "own_side_quantity": None,
        "side_total_quantity": None,
        "book_share_pct": None,
    }
    assert directions["NO"]["book_shares"]["SELL"] == {
        "own_side_quantity": None,
        "side_total_quantity": None,
        "book_share_pct": None,
    }


class _LpCancelExecution(_ProductionExecution):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls: list[dict[str, object]] = []
        self.cancel_result: dict[str, object] = {
            "requested": 1,
            "canceled": ["o1"],
            "not_canceled": {},
            "skipped": [],
        }

    def lp_cancel_orders(
        self, payload: Mapping[str, object]
    ) -> dict[str, object]:
        self.cancel_calls.append(dict(payload))
        return self.cancel_result


def _lp_cancel_runtime() -> tuple[_ProductionRuntime, _LpCancelExecution]:
    runtime = _ProductionRuntime()
    execution = _LpCancelExecution()
    runtime.execution = execution  # type: ignore[assignment]
    return runtime, execution


LP_CANCEL_PATH = "/api/prediction-arbitrage/lp/orders/cancel"


def test_lp_manual_cancel_endpoint_dispatches_each_selector() -> None:
    """A1: order_ids/condition_id/scope 三种合法请求各自分发,
    fake execution 的返回原样作为 200 响应体,调用参数逐字记录。"""

    runtime, execution = _lp_cancel_runtime()
    bodies = (
        {"order_ids": ["o1"], "confirm": True},
        {"condition_id": "0xc1", "confirm": True},
        {"scope": "all", "confirm": True},
    )
    with _production_server(runtime) as (base, _runtime):
        results = [
            _response(
                _production_request(
                    base, LP_CANCEL_PATH, data=json.dumps(body).encode()
                )
            )
            for body in bodies
        ]

    assert results == [(200, execution.cancel_result) for _body in bodies]
    assert execution.cancel_calls == [dict(body) for body in bodies]


@pytest.mark.parametrize(
    "body",
    (
        {"order_ids": ["o1"], "condition_id": "0xc1", "confirm": True},
        {"scope": "all", "order_ids": ["o1"], "confirm": True},
        {"confirm": True},
        {"order_ids": ["o1"]},
        {"order_ids": ["o1"], "confirm": False},
        {"order_ids": [], "confirm": True},
        {"order_ids": ["o1"], "confirm": True, "unexpected": "key"},
    ),
)
def test_lp_manual_cancel_endpoint_rejects_invalid_requests(
    body: dict[str, object],
) -> None:
    """A2: 选择器互斥/必填、confirm 必须为 true、order_ids 非空、
    未知键——全部 400 且不分发。"""

    runtime, execution = _lp_cancel_runtime()
    with _production_server(runtime) as (base, _runtime):
        status, payload = _response(
            _production_request(
                base, LP_CANCEL_PATH, data=json.dumps(body).encode()
            )
        )

    assert status == 400
    assert payload["status"] == "error"
    assert execution.cancel_calls == []


def test_lp_manual_cancel_endpoint_is_read_only_in_shadow_mode() -> None:
    """A3: shadow 模式 → 403 只读拒绝,镜像既有 shadow mutation 用例语义。"""

    with _server(_Runtime()) as base:
        status, payload = _response(
            Request(
                base + LP_CANCEL_PATH,
                data=json.dumps({"order_ids": ["o1"], "confirm": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    assert status == 403
    assert payload == {
        "code": "shadow_read_only",
        "message": "Shadow Prediction Service is read-only",
    }


def test_lp_dashboard_payload_projects_rolling_pool_fields(tmp_path: Path) -> None:
    """Issue #157 B1: the LP dashboard payload carries the rolling-pool
    status counts and every candidate row keeps its updated_at, expires_at,
    and refresh_failed fields through the projection untouched."""

    pool_row = {
        "market_id": "market-pool-1",
        "condition_id": "condition-pool-1",
        "market_title": "Pool market",
        "market_url": "https://polymarket.com/event/pool-1",
        "state": "eligible",
        "selected_direction": {
            "outcome": "YES",
            "state": "eligible",
            "eligible": True,
            "price": "0.34",
            "quantity": "20",
            "required_capital": "6.80",
            "checked_at": "2026-09-20T17:00:00.000000Z",
        },
        "directions": {
            "YES": {"state": "eligible", "eligible": True},
        },
        "estimate_state": "known",
        "estimate_updated": True,
        "estimated_yield_raw": "0.5",
        "estimated_yield_pct_per_hour": "0.500000",
        "updated_at": "2026-09-20T17:00:00.000000Z",
        "expires_at": "2026-09-20T17:05:00.000000Z",
        "refresh_failed": False,
    }

    class PoolLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "ready",
                "complete": True,
                "scanning": False,
                "stale": False,
                "candidates": [pool_row],
                "recommendations": [pool_row],
                "selected_results": [pool_row],
                "checked_at": "2026-09-20T17:00:00.000000Z",
                "last_success_at": "2026-09-20T17:00:00.000000Z",
                "last_attempt_at": "2026-09-20T17:00:00.000000Z",
                "candidate_valid_count": 3,
                "candidate_pending_count": 12,
                "candidate_failed_recent_count": 1,
                "missing_metadata_condition_ids": [],
                "missing_book_token_ids": [],
                "catalog_complete": True,
                "funnel": {
                    "read": 20,
                    "base": 18,
                    "sort": 15,
                    "trial": 3,
                    "reasons": {"read": [], "base": [], "sort": [], "trial": []},
                },
                "selected_market_ids": ["market-pool-1"],
                "preparation": {"state": "known"},
            }

        def preparation_snapshot(self) -> dict[str, object]:
            return {"state": "known"}

    class SnapshotTrading:
        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("1000"),
                "allowance": Decimal("1000"),
                "open_orders": [],
                "positions": [],
                "checked_at": "2026-09-20T17:00:00.000000Z",
                "open_orders_complete": True,
                "positions_complete": True,
            }

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=SnapshotTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=PoolLP(),
    )

    payload = service.refresh_lp_dashboard_snapshot()

    assert payload["candidate_valid_count"] == 3
    assert payload["candidate_pending_count"] == 12
    assert payload["candidate_failed_recent_count"] == 1
    row = payload["candidates"][0]
    assert row["updated_at"] == "2026-09-20T17:00:00.000000Z"
    assert row["expires_at"] == "2026-09-20T17:05:00.000000Z"
    assert row["refresh_failed"] is False
    assert payload["recommendations"][0]["updated_at"] == (
        "2026-09-20T17:00:00.000000Z"
    )


def test_lp_first_seen_protection_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S: 网页手动单出现 → 首见登记 → 盘口变化 → 触发 → 撤单 →
    /lp/orders/today 投影含 summary / baseline_source=first_observation / anchor。"""

    class RewardTransport:
        def get_json(self, path: str, *, params: dict[str, object]) -> object:
            del path, params
            return {"data": [], "next_cursor": "LTE="}

    reward_transport = RewardTransport()
    monkeypatch.setattr(
        polymarket_trading_module, "signature_type_for", lambda _wallet_type: 0
    )

    class AccountSDK:
        def __init__(self) -> None:
            self.order_rows: list[dict[str, object]] = []
            self.cancellation_calls: list[tuple[str, ...]] = []
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
            return list(self.order_rows)

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return []

        def get_order_scoring(self, *, order_id: str) -> bool:
            return True

        def cancel_orders(self, **kwargs: object) -> object:
            order_ids = tuple(kwargs.get("order_ids") or ())
            self.cancellation_calls.append(order_ids)
            return {"canceled": list(order_ids), "not_canceled": {}}

    class PublicMarketSDK:
        def __init__(self) -> None:
            self.level_total = "10000"

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            del condition_ids, page_size
            return []

        def get_order_books(self, *, token_ids: object) -> list[object]:
            return [
                {
                    "condition_id": "condition-1",
                    "token_id": str(token_ids[0]),
                    "timestamp": "2026-09-21T12:00:00Z",
                    "hash": "book-hash-s",
                    "bids": [{"price": "0.50", "size": self.level_total}],
                    "asks": [{"price": "0.52", "size": "100"}],
                }
                for token_id in tuple(token_ids)
            ]

        def close(self) -> None:
            pass

    sdk = AccountSDK()
    public_market = PublicMarketSDK()
    service, _trading, store, _monitor = execution_fixture(tmp_path)
    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        client=sdk,
        public_client_factory=lambda: public_market,
    )
    service._trading = trading
    service._lp = PolymarketLPService(store, trading)
    # The scripted account rounds must not race scheduled background
    # refresh threads for the dashboard lock.
    service._schedule_lp_reward_refresh = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service._schedule_lp_orders_today_refresh = lambda *args, **kwargs: None  # type: ignore[method-assign]

    notes: list[tuple[str, str, str]] = []
    service._lp.set_protection_notifier(
        lambda title, message, xiaoai_text: notes.append(
            (title, message, xiaoai_text)
        )
    )

    manual_buy: dict[str, object] = {
        "id": "m-web-1",
        "market": "condition-1",
        "asset_id": "yes-token",
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "original_size": Decimal("2000"),
        "size_matched": Decimal("0"),
    }

    # 第 1 轮：账户里还没有该单 → 只建底。
    service.refresh_lp_dashboard_snapshot()
    assert store.lp_active_first_seen_episodes() == []

    # 第 2 轮：网页手动 BUY 出现 → 首见登记（基线 10000 − 2000 = 8000）。
    sdk.order_rows = [manual_buy]
    trading._lp_account_shared_cache = None  # expire the shared account TTL
    service.refresh_lp_dashboard_snapshot()
    episodes = store.lp_active_first_seen_episodes()
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["baseline_source"] == "first_observation"
    assert Decimal(str(episode["baseline_front"])) == Decimal("8000")
    assert episode["anchor_order_ids"] == ["m-web-1"]

    # 盘口变化（同价位总量降到 4,000）→ 一秒 tick 评估触发 → 撤单。
    public_market.level_total = "4000"
    service._lp.tick()
    episode = store.lp_first_seen_episode(str(episode["episode_id"]))
    assert episode is not None
    assert episode["state"] == "canceling"
    assert sdk.cancellation_calls == [("m-web-1",)]

    # /lp/orders/today 投影：锚行带 summary / baseline_source / anchor。
    trading._lp_account_shared_cache = None
    payload = prediction_service._lp_projection_safe_value(
        service.refresh_lp_dashboard_snapshot()
    )
    rows = {
        str(row["order_id"]): row for row in payload["lp_orders_today"]
    }
    projection = rows["m-web-1"]
    assert projection["anchor"] is True
    summary = projection["queue_protection"]
    assert summary["state"] == "canceling"
    assert summary["baseline_source"] == "first_observation"
    assert Decimal(str(summary["anchor_price"])) == Decimal("0.50")
    assert Decimal(str(summary["level_total"])) == Decimal("4000")

    # 回执收敛：订单从所有读取路径消失 → canceled + 一次性首见通知。
    sdk.order_rows = []
    service._lp.tick()
    episode = store.lp_first_seen_episode(str(episode["episode_id"]))
    assert episode is not None
    assert episode["state"] == "canceled"
    assert len(notes) == 1
    assert "首见基线" in notes[0][0]


# ---- Issue 163: LP 单次确认提交路由（/lp/orders、/lp/augment） ----


def _lp163_route_fixture(tmp_path: Path, now: datetime):
    """Production-mode runtime wired to a counting LP exchange (issue 163)."""

    condition_id = "0x" + "c" * 64
    token_id = "0x" + "1" * 64

    class Exchange:
        def __init__(self) -> None:
            self.posts: list[dict[str, object]] = []
            self.snapshot_calls = 0
            self.best_bid = Decimal("0.29")
            self.snapshot = {
                "account": {
                    "authenticated": True,
                    "balance": Decimal("1000"),
                    "allowance": Decimal("1000"),
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
                    "bids": [{"price": self.best_bid, "size": Decimal("100")}],
                },
                "trades": [],
                "orders": [],
                "orders_terminal": True,
            }

        def lp_snapshot(self, _request: Mapping[str, object]) -> dict[str, object]:
            self.snapshot_calls += 1
            return self.snapshot

        def create_limit_order(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        def post_order(self, signed: dict[str, object]) -> dict[str, object]:
            self.posts.append(dict(signed))
            return {
                **signed,
                "order_id": f"lp-order-{len(self.posts)}",
                "status": "LIVE",
            }

        def cancel_order(self, order_id: str) -> dict[str, object]:
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
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=_Monitor(),
        execution=execution,
        cross_venue_monitor=None,
    )

    def orders_body(**overrides: object) -> dict[str, object]:
        body = {
            "market_id": "market-1",
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": "YES",
            "price": "0.29",
            "quantity": "20",
            "review_at": (now + timedelta(minutes=10)).isoformat(),
            "idempotency_key": "lp163-h",
            "candidate_policy": "best_bid_minimum",
            "estimated_target_quantity": "21",
        }
        body.update(overrides)
        return body

    return runtime, execution, exchange, store, lp, orders_body


def test_lp163_orders_route_auth_precedes(tmp_path: Path) -> None:
    """H1: 无 cookie/CSRF → 403，先于 body 解析（坏 schema 的 body 也不得触发 400）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    runtime, _execution, exchange, _store, _lp, orders_body = _lp163_route_fixture(
        tmp_path, now
    )
    with _server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        status, payload = _response(
            Request(
                base + "/api/prediction-arbitrage/lp/orders",
                data=json.dumps({"price": "not-even-a-schema-hit"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
    assert status == 403
    assert exchange.posts == []
    assert exchange.snapshot_calls == 0


def test_lp163_orders_route_schema_strict(tmp_path: Path) -> None:
    """H2: 严格 schema——缺 idempotency_key/多键/价格非十进制各自 400，零挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    runtime, _execution, exchange, _store, _lp, orders_body = _lp163_route_fixture(
        tmp_path, now
    )
    with _server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        missing = dict(orders_body())
        missing.pop("idempotency_key")
        missing_status, _missing_payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(missing).encode("utf-8"),
            )
        )
        extra_status, _extra_payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps({**orders_body(), "preview_id": "pv-x"}).encode(
                    "utf-8"
                ),
            )
        )
        bad_price_status, _bad_price_payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(price="abc")).encode("utf-8"),
            )
        )
    assert missing_status == 400
    assert extra_status == 400
    assert bad_price_status == 400
    assert exchange.posts == []
    assert exchange.snapshot_calls == 0


def test_lp163_orders_route_semantic_states_http_200(tmp_path: Path) -> None:
    """H3: 熔断→locked、活动会话→busy、买一漂移→rejected/best_bid_changed，全部 HTTP 200。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    runtime, execution, exchange, _store, _lp, orders_body = _lp163_route_fixture(
        tmp_path, now
    )
    with _server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        execution._breaker_open = True
        locked_status, locked = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h3-locked")).encode(
                    "utf-8"
                ),
            )
        )
        assert locked_status == 200
        assert locked == {"state": "locked", "reason": "circuit_breaker_open"}
        assert exchange.posts == []
        assert exchange.snapshot_calls == 0

        execution._breaker_open = False
        exchange.best_bid = Decimal("0.30")
        exchange.snapshot["book"]["bids"] = [
            {"price": Decimal("0.30"), "size": Decimal("100")}
        ]
        drifted_status, drifted = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h3-drift")).encode(
                    "utf-8"
                ),
            )
        )
        assert drifted_status == 200
        assert drifted == {"state": "rejected", "reason": "best_bid_changed"}
        assert exchange.posts == []
        assert exchange.snapshot_calls == 1

        exchange.best_bid = Decimal("0.29")
        exchange.snapshot["book"]["bids"] = [
            {"price": Decimal("0.29"), "size": Decimal("100")}
        ]
        ok_status, ok = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h3-ok")).encode(
                    "utf-8"
                ),
            )
        )
        assert ok_status == 200
        assert ok["state"] == "entry_open"

        busy_status, busy = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h3-busy")).encode(
                    "utf-8"
                ),
            )
        )
        assert busy_status == 200
        assert busy["state"] == "busy"
        assert busy["reason"] == "lp_session_market_active"
        assert len(exchange.posts) == 1


def test_lp163_orders_route_happy_path(tmp_path: Path) -> None:
    """H4: 全鉴权一次提交 → 200、entry_open、session_id 与 order id 齐备。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    runtime, _execution, exchange, store, _lp, orders_body = _lp163_route_fixture(
        tmp_path, now
    )
    with _server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        status, payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h4")).encode(
                    "utf-8"
                ),
            )
        )
    assert status == 200
    assert payload["state"] == "entry_open"
    assert str(payload["session_id"]).strip()
    assert str(payload["entry_order_id"]).strip()
    assert len(exchange.posts) == 1
    posted = exchange.posts[0]
    assert posted["post_only"] is True
    stored = store.lp_session_by_idempotency("lp163-h4")
    assert stored is not None
    assert Decimal(str(stored["estimated_target_quantity"])) == Decimal("21")


def test_lp163_augment_route_contract(tmp_path: Path) -> None:
    """H5: /lp/augment——缺字段 400、成功 200+augment_order_id、受阻组 200+真实原因、鉴权先行。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    runtime, _execution, exchange, store, lp, orders_body = _lp163_route_fixture(
        tmp_path, now
    )
    with _server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        start_status, started = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/orders",
                data=json.dumps(orders_body(idempotency_key="lp163-h5-entry")).encode(
                    "utf-8"
                ),
            )
        )
        assert start_status == 200
        assert started["state"] == "entry_open"
        session_id = str(started["session_id"])

        unauth_status, _unauth = _response(
            Request(
                base + "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {"session_id": session_id, "quantity": "20", "idempotency_key": "x"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        assert unauth_status == 403

        missing_status, _missing = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps({"session_id": session_id, "quantity": "20"}).encode(
                    "utf-8"
                ),
            )
        )
        assert missing_status == 400

        exchange.snapshot["account"]["open_orders"] = [
            {
                "order_id": str(started["entry_order_id"]),
                "token_id": "0x" + "1" * 64,
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.29"),
                "original_size": Decimal("20"),
                "size_matched": Decimal("0"),
            }
        ]
        # #167 改写：同价补量（缺省组价 0.29，入场单仍在挂）→ price_level_active。
        same_status, same = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "20",
                        "idempotency_key": "lp163-h5-aug-same-price",
                    }
                ).encode("utf-8"),
            )
        )
        assert same_status == 200
        assert same == {"state": "rejected", "reason": "price_level_active"}
        assert len(exchange.posts) == 1

        # 新价位 0.28（不高于顶档买一 0.29）→ 成功恰一单。
        aug_status, augmented = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "20",
                        "idempotency_key": "lp163-h5-aug",
                        "price": "0.28",
                    }
                ).encode("utf-8"),
            )
        )
        assert aug_status == 200
        assert augmented["state"] == "entry_open"
        assert str(augmented["augment_order_id"]) == "lp-order-2"
        assert Decimal(str(exchange.posts[1]["price"])) == Decimal("0.28")
        assert len(exchange.posts) == 2

        store.lp_update_session(session_id, state="complete")
        blocked_status, blocked = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/lp/augment",
                data=json.dumps(
                    {
                        "session_id": session_id,
                        "quantity": "20",
                        "idempotency_key": "lp163-h5-blocked",
                    }
                ).encode("utf-8"),
            )
        )
        assert blocked_status == 200
        assert blocked == {"state": "rejected", "reason": "session_not_active"}
        assert len(exchange.posts) == 2
