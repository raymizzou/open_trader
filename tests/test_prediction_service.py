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
from open_trader.polymarket_lp import PolymarketLPService
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


METADATA_TTL = (
    polymarket_trading_module.LP_METADATA_CACHE_TTL_SECONDS
    + polymarket_trading_module.LP_METADATA_CACHE_JITTER_SECONDS
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

    with _server(runtime) as base:
        status, first = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        status_after_failure, stale = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert reward_transport.completed.wait(timeout=2)

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
    assert stale["orders"] == first["orders"]
    assert stale["positions"] == first["positions"]
    assert sdk.open_order_reads == 2
    assert sdk.scoring_reads == ["manual-order"]
    assert len(reward_transport.calls) == 4
    assert reward_transport.calls[0][0] == "/rewards/user/percentages"
    assert sdk.order_writes == 0
    assert sdk.cancellations == 0
    assert public_market.closed is True


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
    assert row["scoring_status"] is True
    assert payload["non_lp_row_count"] == 0

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
    assert first_scan["funnel"]["catalog_read"] == 1
    assert first_scan["funnel"]["base_pass"] == 1
    assert first_scan["funnel"]["volatility_pass"] == 1
    assert first_scan["funnel"]["selected"] == 1
    assert first_scan["funnel"]["risk"] == {
        "passed": 1,
        "rejected": 0,
        "unknown": 0,
    }
    assert first_scan["funnel"]["risk_directions"] == {
        "passed": 1,
        "rejected": 0,
        "unknown": 0,
    }
    assert first_scan["recommendations"][0]["state"] == "eligible"
    assert first_scan["recommendations"][0]["directions"]["YES"]["state"] == "eligible"
    assert exchange.catalog_reads == 1
    assert exchange.metadata_reads == 2
    assert exchange.book_reads == 1
    assert exchange.history_reads == 1
    assert exchange.account_reads == 2

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
    first_dashboard = execution.lp_dashboard()
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
    assert first_dashboard["funnel"]["catalog_read"] == 1
    assert first_dashboard["funnel"]["base_pass"] == 1
    assert first_dashboard["funnel"]["volatility_pass"] == 1
    assert first_dashboard["funnel"]["selected"] == 1
    assert first_dashboard["funnel"]["risk"] == {
        "passed": 1,
        "rejected": 0,
        "unknown": 0,
    }
    assert first_dashboard["selected_market_ids"] == ["market-A"]
    assert first_dashboard["funnel"]["selected"] == 1
    assert first_dashboard["checked_at"] == "2026-09-17T01:00:00.000000Z"
    assert [row["order_id"] for row in first_dashboard["lp_orders_today"]] == [
        "warm-lp-order"
    ]
    assert first_dashboard["non_lp_row_count"] == 0
    assert first_dashboard["recommendations"][0]["reference_share_percentage"] == Decimal("5")
    assert first_dashboard["recommendations"][0]["reference_daily_reward_usd"] == Decimal("5")
    first_account_checked_at = "2026-09-17T01:00:00.000000Z"

    now[0] = first_now + timedelta(seconds=10)
    account_failure[0] = True
    second_prepared = lp.refresh_price_history()
    assert second_prepared["state"] == "known"
    second_scan = lp.refresh_candidates(force=True)
    assert second_scan["state"] == "incomplete"
    assert second_scan["complete"] is False
    assert second_scan["stale"] is False
    assert second_scan["selected_market_ids"] == ["market-B", "market-C"]
    assert second_scan["checked_at"] == "2026-09-17T01:00:10.000000Z"
    assert second_scan["funnel"]["catalog_read"] == 2
    assert second_scan["funnel"]["base_pass"] == 2
    assert second_scan["funnel"]["volatility_pass"] == 2
    assert second_scan["funnel"]["selected"] == 2
    assert second_scan["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert second_scan["funnel"]["risk_directions"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert second_scan["recommendations"] == []
    assert [row["market_id"] for row in second_scan["selected_results"]] == [
        "market-B",
        "market-C",
    ]
    assert all(row["state"] == "unknown" for row in second_scan["selected_results"])
    assert all(
        row["directions"]["YES"]["state"] == "unknown"
        for row in second_scan["selected_results"]
    )
    assert exchange.catalog_reads == 2
    assert exchange.metadata_reads == 4
    assert exchange.book_reads == 2
    assert exchange.history_reads == 2
    assert exchange.account_reads == 5

    counters_before_stale_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    stale_dashboard = execution.lp_dashboard()
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
    assert stale_dashboard["candidate_state"] == "incomplete"
    assert stale_dashboard["complete"] is False
    assert stale_dashboard["candidate_stale"] is False
    assert stale_dashboard["candidate_checked_at"] == "2026-09-17T01:00:10.000000Z"
    assert stale_dashboard["selected_market_ids"] == ["market-B", "market-C"]
    assert stale_dashboard["funnel"]["catalog_read"] == 2
    assert stale_dashboard["funnel"]["base_pass"] == 2
    assert stale_dashboard["funnel"]["volatility_pass"] == 2
    assert stale_dashboard["funnel"]["selected"] == 2
    assert stale_dashboard["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert stale_dashboard["recommendations"] == []
    assert [row["market_id"] for row in stale_dashboard["selected_results"]] == [
        "market-B",
        "market-C",
    ]
    assert [row["order_id"] for row in stale_dashboard["lp_orders_today"]] == [
        "warm-lp-order"
    ]
    assert stale_dashboard["non_lp_row_count"] == 0
    assert [
        row["daily_pool_usd"] for row in stale_dashboard["selected_results"]
    ] == ["90", "80"]
    assert [
        row["reference_share_percentage"]
        for row in stale_dashboard["selected_results"]
    ] == [Decimal("5"), Decimal("5")]
    assert [
        row["reference_daily_reward_usd"]
        for row in stale_dashboard["selected_results"]
    ] == [Decimal("4.5"), Decimal("4")]
    assert all(
        row["directions"]["YES"]["state"] == "unknown"
        for row in stale_dashboard["selected_results"]
    )

    counters_before_repeated_dashboard = {
        "catalog": exchange.catalog_reads,
        "metadata": exchange.metadata_reads,
        "books": exchange.book_reads,
        "history": exchange.history_reads,
        "account": exchange.account_reads,
    }
    repeated_stale_dashboard = execution.lp_dashboard()
    assert exchange.catalog_reads == counters_before_repeated_dashboard["catalog"]
    assert exchange.metadata_reads == counters_before_repeated_dashboard["metadata"]
    assert exchange.book_reads == counters_before_repeated_dashboard["books"]
    assert exchange.history_reads == counters_before_repeated_dashboard["history"]
    assert exchange.account_reads == counters_before_repeated_dashboard["account"] + 1
    assert repeated_stale_dashboard["state"] == "stale"
    assert repeated_stale_dashboard["stale"] is True
    assert repeated_stale_dashboard["checked_at"] == first_account_checked_at
    assert repeated_stale_dashboard["candidate_state"] == "incomplete"
    assert repeated_stale_dashboard["complete"] is False
    assert repeated_stale_dashboard["candidate_stale"] is False
    assert repeated_stale_dashboard["candidate_checked_at"] == "2026-09-17T01:00:10.000000Z"
    assert repeated_stale_dashboard["selected_market_ids"] == ["market-B", "market-C"]
    assert repeated_stale_dashboard["funnel"]["catalog_read"] == 2
    assert repeated_stale_dashboard["funnel"]["base_pass"] == 2
    assert repeated_stale_dashboard["funnel"]["volatility_pass"] == 2
    assert repeated_stale_dashboard["funnel"]["selected"] == 2
    assert repeated_stale_dashboard["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert repeated_stale_dashboard["recommendations"] == []
    assert [
        row["market_id"] for row in repeated_stale_dashboard["selected_results"]
    ] == ["market-B", "market-C"]
    assert [
        row["order_id"] for row in repeated_stale_dashboard["lp_orders_today"]
    ] == ["warm-lp-order"]
    assert repeated_stale_dashboard["non_lp_row_count"] == 0
    assert [
        row["daily_pool_usd"]
        for row in repeated_stale_dashboard["selected_results"]
    ] == ["90", "80"]
    assert [
        row["reference_share_percentage"]
        for row in repeated_stale_dashboard["selected_results"]
    ] == [Decimal("5"), Decimal("5")]
    assert [
        row["reference_daily_reward_usd"]
        for row in repeated_stale_dashboard["selected_results"]
    ] == [Decimal("4.5"), Decimal("4")]
    assert all(
        row["directions"]["YES"]["state"] == "unknown"
        for row in repeated_stale_dashboard["selected_results"]
    )

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
    cold_dashboard = cold_execution.lp_dashboard()
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
    assert cold_dashboard["candidate_state"] == "incomplete"
    assert cold_dashboard["complete"] is False
    assert cold_dashboard["candidate_stale"] is False
    assert cold_dashboard["candidate_checked_at"] == "2026-09-17T01:00:10.000000Z"
    assert cold_dashboard["selected_market_ids"] == ["market-B", "market-C"]
    assert cold_dashboard["funnel"]["catalog_read"] == 2
    assert cold_dashboard["funnel"]["base_pass"] == 2
    assert cold_dashboard["funnel"]["volatility_pass"] == 2
    assert cold_dashboard["funnel"]["selected"] == 2
    assert cold_dashboard["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert cold_dashboard["recommendations"] == []
    assert [row["market_id"] for row in cold_dashboard["selected_results"]] == [
        "market-B",
        "market-C",
    ]
    assert [
        row["daily_pool_usd"] for row in cold_dashboard["selected_results"]
    ] == ["90", "80"]
    assert all(
        row["directions"]["YES"]["state"] == "unknown"
        for row in cold_dashboard["selected_results"]
    )



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
        dashboard = service.lp_dashboard()
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
        with _server(runtime) as base:
            with ThreadPoolExecutor(max_workers=1) as clients:
                future = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/lp/dashboard",
                    timeout=5,
                )
                assert entered.wait(timeout=2)
                status, payload = future.result(timeout=0.5)
    finally:
        release.set()

    assert status == 200
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

    try:
        with _server(runtime) as base:
            with ThreadPoolExecutor(max_workers=2) as clients:
                first_future = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/lp/dashboard",
                    timeout=5,
                )
                assert entered.wait(timeout=2)
                second_future = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/lp/dashboard",
                    timeout=5,
                )
                first_status, first = first_future.result(timeout=2)
                second_status, second = second_future.result(timeout=2)
            assert first_status == second_status == 200
            assert first["market_rewards"]["condition-1"]["state"] == "unknown"
            assert second["market_rewards"]["condition-1"]["state"] == "unknown"
            release.set()

            updated: dict[str, object] | None = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status, candidate = _response(
                    base + "/api/prediction-arbitrage/lp/dashboard",
                    timeout=5,
                )
                if (
                    status == 200
                    and candidate["market_rewards"]["condition-1"]["state"]
                    == "known"
                ):
                    updated = candidate
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
        service.lp_dashboard()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            first = service.lp_dashboard()
            if first["market_rewards"]["condition-1"]["state"] == "known":
                break
            time.sleep(0.01)
        assert calls == 1
        clock[0] = 61.0
        with ThreadPoolExecutor(max_workers=1) as clients:
            future = clients.submit(service.lp_dashboard)
            assert entered.wait(timeout=2)
            expired = future.result(timeout=0.5)
    finally:
        release.set()

    reward = expired["market_rewards"]["condition-1"]
    assert reward["state"] == "known"
    assert reward["stale"] is True
    assert reward["market_amount"] == Decimal("0.80")


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
        service.lp_dashboard()
        assert entered.wait(timeout=2)
        service.lp_dashboard()
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
    service._lp_dashboard_lock = HandoffLock(service)  # type: ignore[assignment]

    try:
        service.lp_dashboard()
        assert gap_open.wait(timeout=2)
        service.lp_dashboard()
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
        service.lp_dashboard()
        assert old_entered.wait(timeout=2)
        current = service.lp_dashboard()
        assert current["market_rewards"]["condition-1"]["reward_date"] == "2026-09-16"
        old_release.set()
        assert new_entered.wait(timeout=2)
        stale = service.lp_dashboard()
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
    service.lp_dashboard = lambda: {"state": "stale", "stale": True}  # type: ignore[method-assign]

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
    assert service.lp_dashboard()["lp_observations"][condition_id]["add_room"]["available"] is True

    account_id = service._lp_account_id()
    assert isinstance(account_id, str)
    saved_initial = store.lp_observations(account_id)[condition_id]
    state["orders"] = [order(Decimal("400"))]
    notification_calls_before_get = notifier.calls
    changed_dashboard = service.lp_dashboard()
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
    price_dashboard = service.lp_dashboard()
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
    aged_dashboard = service.lp_dashboard()
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
    assert service.lp_dashboard()["lp_observations"][condition_id]["add_room"]["available"] is False

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
    assert service.lp_dashboard()["state"] == "ready"
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
    assert voice_texts[-1] == posted[-1]["content"]["text"]

    retry = service.refresh_lp_observations()["observations"][condition_id]
    assert len(posted) == 1
    assert len(voice_texts) == 2
    assert retry["risk_alerts"]["YES"]["channels"]["xiaoai"]["success"] is True
    assert voice_texts[-1] == posted[-1]["content"]["text"]

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
            status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")
            assert status == 200
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
                recommendation = next(
                    row
                    for row in payload["recommendations"]
                    if row["condition_id"] == "condition-rec"
                )
                assert recommendation["reference_share_percentage"] == "5"
                assert recommendation["reference_daily_reward_usd"] == "4.95"
                no_pool = next(
                    row
                    for row in payload["selected_results"]
                    if row["condition_id"] == "condition-no-pool"
                )
                assert no_pool["reference_share_percentage"] == "5"
                assert no_pool["reference_daily_reward_usd"] is None

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
            status, dashboard = _response(base + "/api/prediction-arbitrage/lp/dashboard")
            assert status == 200
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


def test_lp_share_watch_alerts_only_on_confirmed_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    controlled_now = [base]
    elapsed_seconds = [0.0]

    class Account:
        wallet_address = "wallet-a"

        def __init__(self, *, include_second_market: bool = True) -> None:
            self.config = SimpleNamespace(wallet_address=self.wallet_address)
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
                },
                {
                    "order_id": "a-2",
                    "condition_id": "condition-a",
                    "market_title": "Market A",
                    "token_id": "a-no",
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.45"),
                    "original_size": Decimal("10"),
                    "size_matched": Decimal("0"),
                },
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
            self.reward_maker = self.wallet_address
            self.reward_source_override: datetime | None = None
            self.reward_reads = 0
            self.order_reads = 0
            self.stop_event_on_share_read: threading.Event | None = None

        def lp_open_orders_snapshot(self) -> dict[str, object]:
            self.order_reads += 1
            controlled_now[0] += timedelta(milliseconds=1)
            return {
                "authenticated": True,
                "open_orders": tuple(self.open_orders),
                "open_orders_complete": True,
                "checked_at": controlled_now[0],
            }

        def lp_reward_percentages(self) -> dict[str, object]:
            self.reward_reads += 1
            if self.reward_failure:
                raise RuntimeError("reward share read unavailable")
            stop_event = self.stop_event_on_share_read
            self.stop_event_on_share_read = None
            checked_at = (
                self.reward_source_override
                or self.reward_checked_at
                or controlled_now[0]
            )
            if stop_event is not None:
                stop_event.set()
            return {
                "state": "known",
                "scope": "account",
                "maker_address": self.reward_maker,
                "percentages": {
                    "condition-a": self.reward_value,
                    "condition-b": self.reward_value,
                },
                "checked_at": checked_at,
            }

    class RecordingXiaoai(XiaoaiSSHNotifier):
        def __init__(
            self,
            *,
            fail: bool = False,
            suppress: bool = False,
            respect_quiet_hours: bool = False,
            advance_after_success_seconds: float = 0.0,
        ) -> None:
            self.calls: list[tuple[str, str]] = []
            self.successes: list[tuple[str, str]] = []
            self.fail = fail
            self.suppress = suppress
            self.respect_quiet_hours = respect_quiet_hours
            self.advance_after_success_seconds = advance_after_success_seconds

        def notify(self, title: str, message: str) -> None:
            self.calls.append((title, message))
            if self.suppress or (
                self.respect_quiet_hours
                and not xiaoai_voice_allowed(controlled_now[0])
            ):
                raise XiaoaiVoiceSuppressed("quiet hours")
            if self.fail:
                raise NotificationError("delivery failed")
            if self.advance_after_success_seconds:
                controlled_now[0] += timedelta(
                    seconds=self.advance_after_success_seconds
                )
                self.advance_after_success_seconds = 0.0
            self.successes.append((title, message))

    def make_service(
        root: Path, account: Account, notifier: RecordingXiaoai
    ) -> PredictionExecutionService:
        service, _trading, _store, _monitor = execution_fixture(root)
        service._trading = account
        service._notifier = notifier
        service._clock = lambda: elapsed_seconds[0]
        return service

    def set_time(seconds: float) -> None:
        elapsed_seconds[0] = seconds
        controlled_now[0] = base + timedelta(seconds=seconds)

    def set_absolute_time(moment: datetime) -> None:
        elapsed_seconds[0] = (moment - base).total_seconds()
        controlled_now[0] = moment

    def refresh(
        service: PredictionExecutionService,
        account: Account,
        seconds: float,
        value: Decimal = Decimal("8.5"),
        *,
        checked_at: datetime | None = None,
        failure: bool = False,
        maker: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        set_time(seconds)
        account.reward_value = value
        account.reward_checked_at = checked_at
        account.reward_failure = failure
        account.reward_maker = maker or account.wallet_address
        return service.refresh_lp_share_watch(stop_event=stop_event)

    def refresh_at(
        service: PredictionExecutionService,
        account: Account,
        moment: datetime,
        value: Decimal = Decimal("8.5"),
        *,
        checked_at: datetime | None = None,
        failure: bool = False,
        maker: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        set_absolute_time(moment)
        account.reward_value = value
        account.reward_checked_at = checked_at
        account.reward_failure = failure
        account.reward_maker = maker or account.wallet_address
        return service.refresh_lp_share_watch(stop_event=stop_event)

    account = Account()
    notifier = RecordingXiaoai()
    service = make_service(tmp_path, account, notifier)
    recovery_account = Account(include_second_market=False)
    recovery_notifier = RecordingXiaoai()
    recovery_service = make_service(
        tmp_path / "recovery", recovery_account, recovery_notifier
    )
    gap_account = Account(include_second_market=False)
    gap_notifier = RecordingXiaoai()
    gap_service = make_service(tmp_path / "gap", gap_account, gap_notifier)
    spent_account = Account(include_second_market=False)
    spent_notifier = RecordingXiaoai()
    spent_service = make_service(tmp_path / "spent", spent_account, spent_notifier)
    pending_account = Account(include_second_market=False)
    pending_notifier = RecordingXiaoai(fail=True)
    pending_service = make_service(
        tmp_path / "pending", pending_account, pending_notifier
    )
    recovered_account = Account(include_second_market=False)
    recovered_notifier = RecordingXiaoai(suppress=True)
    recovered_service = make_service(
        tmp_path / "recovered", recovered_account, recovered_notifier
    )
    cached_account = Account(include_second_market=False)
    cached_notifier = RecordingXiaoai()
    cached_service = make_service(
        tmp_path / "cached", cached_account, cached_notifier
    )
    restart_account = Account(include_second_market=False)
    restart_notifier = RecordingXiaoai()
    restart_service = make_service(
        tmp_path / "restart", restart_account, restart_notifier
    )
    pending_restart_account = Account(include_second_market=False)
    pending_restart_notifier = RecordingXiaoai(fail=True)
    pending_restart_service = make_service(
        tmp_path / "pending-restart",
        pending_restart_account,
        pending_restart_notifier,
    )
    cancelled_account = Account(include_second_market=False)
    cancelled_notifier = RecordingXiaoai()
    cancelled_service = make_service(
        tmp_path / "cancelled", cancelled_account, cancelled_notifier
    )
    queued_account = Account()
    queued_notifier = RecordingXiaoai()
    queued_service = make_service(
        tmp_path / "queued", queued_account, queued_notifier
    )
    delayed_account = Account(include_second_market=False)
    delayed_notifier = RecordingXiaoai()
    delayed_service = make_service(
        tmp_path / "delayed", delayed_account, delayed_notifier
    )
    quiet_account = Account(include_second_market=False)
    quiet_notifier = RecordingXiaoai(respect_quiet_hours=True)
    quiet_service = make_service(tmp_path / "quiet", quiet_account, quiet_notifier)
    quiet_recovery_account = Account(include_second_market=False)
    quiet_recovery_notifier = RecordingXiaoai(respect_quiet_hours=True)
    quiet_recovery_service = make_service(
        tmp_path / "quiet-recovery",
        quiet_recovery_account,
        quiet_recovery_notifier,
    )
    sent_quiet_account = Account(include_second_market=False)
    sent_quiet_notifier = RecordingXiaoai(respect_quiet_hours=True)
    sent_quiet_service = make_service(
        tmp_path / "sent-quiet", sent_quiet_account, sent_quiet_notifier
    )
    monkeypatch.setattr(
        prediction_execution_module, "_utc_now", lambda: controlled_now[0]
    )

    assert service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    assert "condition-b" not in service.lp_share_watch_state()

    # Independent fresh over-limit samples must span a complete minute before
    # the first successful delivery.
    for seconds in (0, 10, 20, 30, 40, 50, 59.999):
        refresh(service, account, seconds)
    assert notifier.calls == []
    refresh(service, account, 60)
    assert len(notifier.successes) == 1
    assert notifier.successes[0][0] == "LP 份额预警"
    assert "Market A" in notifier.successes[0][1]
    assert "8.5" in notifier.successes[0][1]
    assert "5%–8%" in notifier.successes[0][1]
    assert "8%" in notifier.successes[0][1]
    assert "condition-b" not in service.lp_share_watch_state() or (
        service.lp_share_watch_state()["condition-b"].get("notification_pending") is not True
    )

    # A stop raised by the external percentage read cancels an otherwise
    # eligible refresh before it can send.
    assert cancelled_service.set_lp_share_watch("condition-a", enabled=True)[
        "enabled"
    ] is True
    for seconds in (0, 10, 20, 30, 40, 50):
        refresh(cancelled_service, cancelled_account, seconds)
    cancel_event = threading.Event()
    cancelled_account.stop_event_on_share_read = cancel_event
    cancelled_result = refresh(
        cancelled_service,
        cancelled_account,
        60,
        stop_event=cancel_event,
    )
    assert cancelled_result["state"] == "cancelled"
    assert cancel_event.is_set()
    assert cancelled_notifier.calls == []
    assert cancelled_notifier.successes == []

    # With two eligible markets, a successful first voice that consumes more
    # than the freshness window must prevent the queued second voice.  The
    # second event is cleared so it can only restart from a fresh sample.
    assert queued_service.set_lp_share_watch("condition-a", enabled=True)[
        "enabled"
    ] is True
    assert queued_service.set_lp_share_watch("condition-b", enabled=True)[
        "enabled"
    ] is True
    for seconds in (0, 10, 20, 30, 40, 50):
        refresh(queued_service, queued_account, seconds)
    queued_before_delivery = queued_service.lp_share_watch_state()
    assert all(
        queued_before_delivery[condition_id].get("breach_started_at") is not None
        for condition_id in ("condition-a", "condition-b")
    )
    queued_notifier.advance_after_success_seconds = 31.0
    refresh(queued_service, queued_account, 60)
    assert len(queued_notifier.calls) == 1
    assert len(queued_notifier.successes) == 1
    queued_after_delivery = queued_service.lp_share_watch_state()
    assert queued_after_delivery["condition-b"].get("notification_pending") is False
    assert queued_after_delivery["condition-b"].get("breach_started_at") is None

    # A continuously fresh breach does not repeat. Returning to 8% re-arms a
    # later episode, which first succeeds at t=680.
    for seconds in range(70, 601, 10):
        refresh(service, account, seconds)
    assert len(notifier.successes) == 1
    refresh(service, account, 610, Decimal("8"))
    for seconds in range(620, 681, 10):
        refresh(service, account, seconds)
    assert len(notifier.successes) == 2

    # UNKNOWN after a successful event does not re-arm that spent episode.
    refresh(service, account, 690, failure=True)
    refresh(service, account, 700)
    assert len(notifier.successes) == 2

    # A complete fresh no-order snapshot pauses the selected market. A new
    # order id resumes it, and manual OFF remains authoritative.
    account.open_orders = []
    refresh(service, account, 705)
    assert service.lp_share_watch_state()["condition-a"].get("paused") is True
    account.open_orders = [
        {
            "order_id": "a-new",
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
    refresh(service, account, 711, Decimal("6"))
    refresh(service, account, 721)
    assert service.lp_share_watch_state()["condition-a"].get("paused") is False
    assert service.set_lp_share_watch("condition-a", enabled=False)["enabled"] is False
    refresh(service, account, 800)
    assert len(notifier.successes) == 2

    # An unfinished breach is not carried through a service restart. Three
    # fresh over-limit readings before restart leave only ten seconds of
    # source continuity at t=730; t=790 is the first eligible delivery.
    assert restart_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (700, 710, 720):
        refresh(restart_service, restart_account, seconds)
    restarted = PredictionExecutionService(
        store=restart_service._store,
        monitor=restart_service._monitor,
        trading=restart_account,
        notifier=restart_notifier,
        lock_path=tmp_path / "restart-after-samples.lock",
    )
    restarted._clock = lambda: elapsed_seconds[0]
    assert restarted.lp_share_watch_state()["condition-a"]["enabled"] is True
    for seconds in (730, 740, 750, 760, 770, 780, 789.999):
        refresh(restarted, restart_account, seconds)
    assert restart_notifier.successes == []
    refresh(restarted, restart_account, 790)
    assert len(restart_notifier.successes) == 1

    # A repeated source sample remains readable while fresh, but cannot be
    # counted as the new observation that makes a minute eligible.
    assert cached_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40):
        refresh(cached_service, cached_account, seconds)
    cached_account.reward_source_override = base + timedelta(seconds=50)
    refresh(cached_service, cached_account, 50)
    refresh(cached_service, cached_account, 60)
    assert cached_notifier.successes == []
    cached_account.reward_source_override = None
    refresh(cached_service, cached_account, 70)
    assert len(cached_notifier.successes) == 1

    # A source stamped at t=59 but received at wall t=60 has only 59 seconds
    # of confirmed source continuity. The next fresh t=70 source is the first
    # one that can complete the minute.
    assert delayed_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40, 50):
        refresh(delayed_service, delayed_account, seconds)
    delayed_account.reward_source_override = base + timedelta(seconds=59)
    refresh(delayed_service, delayed_account, 60)
    assert delayed_notifier.successes == []
    delayed_account.reward_source_override = None
    refresh(delayed_service, delayed_account, 70)
    assert len(delayed_notifier.successes) == 1

    # A recovery at t=30 starts a new continuous window at t=40; t=90 and
    # t=99.999 are still too early, and t=100 is the first eligible delivery.
    assert recovery_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20):
        refresh(recovery_service, recovery_account, seconds)
    refresh(recovery_service, recovery_account, 30, Decimal("8"))
    for seconds in (40, 50, 60, 70, 80, 90, 99.999):
        refresh(recovery_service, recovery_account, seconds)
    assert recovery_notifier.successes == []
    refresh(recovery_service, recovery_account, 100)
    assert len(recovery_notifier.successes) == 1, recovery_service.lp_share_watch_state()

    # A failed delivery leaves an unsent event pending within one service,
    # but a restarted service must establish a fresh minute instead of
    # delivering immediately after a ten-second source gap.
    assert pending_restart_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40, 50, 60):
        refresh(pending_restart_service, pending_restart_account, seconds)
    assert pending_restart_notifier.calls
    assert pending_restart_notifier.successes == []
    pending_restart = PredictionExecutionService(
        store=pending_restart_service._store,
        monitor=pending_restart_service._monitor,
        trading=pending_restart_account,
        notifier=pending_restart_notifier,
        lock_path=tmp_path / "pending-restart-after-failure.lock",
    )
    pending_restart._clock = lambda: elapsed_seconds[0]
    pending_restart_notifier.fail = False
    refresh(pending_restart, pending_restart_account, 70)
    assert pending_restart_notifier.successes == []
    for seconds in (80, 90, 100, 110, 120, 129.999):
        refresh(pending_restart, pending_restart_account, seconds)
    assert pending_restart_notifier.successes == []
    refresh(pending_restart, pending_restart_account, 130)
    assert len(pending_restart_notifier.successes) == 1

    # An unseen gap after t=20 expires the old evidence; the fresh t=60
    # sample starts a new window and t=120 is the first eligible delivery.
    assert gap_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20):
        refresh(gap_service, gap_account, seconds)
    refresh(gap_service, gap_account, 60)
    for seconds in (70, 80, 90, 100, 110):
        refresh(gap_service, gap_account, seconds)
    assert gap_notifier.successes == []
    refresh(gap_service, gap_account, 120)
    assert len(gap_notifier.successes) == 1

    # A restart and UNKNOWN result after a successful episode do not create a
    # second delivery without a confirmed <=8% recovery.
    assert spent_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40, 50, 60):
        refresh(spent_service, spent_account, seconds)
    assert len(spent_notifier.successes) == 1
    spent_restarted = PredictionExecutionService(
        store=spent_service._store,
        monitor=spent_service._monitor,
        trading=spent_account,
        notifier=spent_notifier,
        lock_path=tmp_path / "spent-restarted.lock",
    )
    spent_restarted._clock = lambda: elapsed_seconds[0]
    refresh(spent_restarted, spent_account, 70, failure=True)
    for seconds in (80, 90, 100, 110, 120, 130):
        refresh(spent_restarted, spent_account, seconds)
    assert len(spent_notifier.successes) == 1

    # Failed and suppressed attempts remain pending and retry through the
    # public refresh seam while the same fresh breach continues. Recovery
    # clears a pending event before delivery, so that old event is not replayed.
    assert pending_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40, 50, 60):
        refresh(pending_service, pending_account, seconds)
    assert len(pending_notifier.calls) == 1
    assert pending_notifier.successes == []
    pending_notifier.fail = False
    refresh(pending_service, pending_account, 70)
    assert len(pending_notifier.successes) == 1
    refresh(pending_service, pending_account, 80)
    assert len(pending_notifier.successes) == 1

    assert recovered_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in (0, 10, 20, 30, 40, 50, 60):
        refresh(recovered_service, recovered_account, seconds)
    assert len(recovered_notifier.calls) == 1
    recovered_account.reward_value = Decimal("8")
    recovered_notifier.suppress = False
    refresh(recovered_service, recovered_account, 70, Decimal("8"))
    for seconds in (80, 90, 100, 110, 120, 130, 140):
        refresh(recovered_service, recovered_account, seconds)
    assert len(recovered_notifier.successes) == 1

    # A pending over-limit event at 07:59 is suppressed by the existing
    # Xiaoai quiet-hour boundary. Fresh readings at 08:00 release that same
    # event exactly once, with later readings remaining deduplicated.
    assert quiet_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in range(43080, 43191, 10):
        refresh(quiet_service, quiet_account, seconds)
    assert quiet_notifier.successes == []
    assert quiet_notifier.calls
    refresh(quiet_service, quiet_account, 43200)
    assert len(quiet_notifier.successes) == 1
    for seconds in (43210, 43220):
        refresh(quiet_service, quiet_account, seconds)
    assert len(quiet_notifier.successes) == 1

    # Recovering at 07:59:30 clears the suppressed pending event before the
    # quiet period ends, so 08:00 cannot replay it.
    assert quiet_recovery_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in range(43080, 43141, 10):
        refresh(quiet_recovery_service, quiet_recovery_account, seconds)
    refresh(quiet_recovery_service, quiet_recovery_account, 43170, Decimal("8"))
    for seconds in (43180, 43190, 43200):
        refresh(quiet_recovery_service, quiet_recovery_account, seconds, Decimal("8"))
    assert quiet_recovery_notifier.successes == []

    # A successful event before quiet hours remains spent across the overnight
    # gap and the next 08:00 boundary, even though all later readings exceed
    # the threshold.
    assert sent_quiet_service.set_lp_share_watch("condition-a", enabled=True)["enabled"] is True
    for seconds in range(10680, 10741, 10):
        refresh(sent_quiet_service, sent_quiet_account, seconds)
    assert len(sent_quiet_notifier.successes) == 1
    refresh(sent_quiet_service, sent_quiet_account, 10800)
    for seconds in range(43140, 43201, 10):
        refresh(sent_quiet_service, sent_quiet_account, seconds)
    assert len(sent_quiet_notifier.successes) == 1


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
        status, payload = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200
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

    with _server(restart_runtime, session_token="restart-session", csrf_token="restart-csrf") as base:
        request = Request(
            base + "/api/prediction-arbitrage/lp/share-watch",
            data=json.dumps(
                {"condition_id": restart_condition, "enabled": True}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": "ot_prediction_session=restart-session",
                "Origin": base,
                "X-CSRF-Token": "restart-csrf",
            },
            method="POST",
        )
        status, payload = _response(request)
        assert status == 200, payload
        assert payload["enabled"] is True

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
    assert scanned["candidates"] == []
    assert [row["market_id"] for row in scanned["recommendations"]] == [
        "market-1"
    ]
    assert len(public_state["catalog_sources"]) == 2
    assert public_state["selected_reward_requests"] == [
        (condition_id, False),
        (condition_id, True),
    ]
    assert len(public_state["metadata_conditions"]) == 2
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
        assert dashboard["complete"] is True
        assert candidate_rows == []
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
    assert failed_scan["stale"] is True
    assert failed_scan["checked_at"] == previous_checked_at
    assert failed_scan["last_success_at"] == previous_scan["last_success_at"]
    assert failed_scan["candidates"] == previous_candidates
    assert public_state["public_creates"] == public_state["public_closes"]


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
            if sponsored:
                return []
            market = self._market_specs().get(condition_id)
            if market is None:
                return []
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
            ]

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            requested = set(condition_ids)  # type: ignore[arg-type]
            markets_by_id = self._market_specs()
            return [
                {
                    "id": market["market_id"],
                    "condition_id": condition_id,
                    "question": f"Will {condition_id} happen?",
                    "slug": market["slug"],
                    "state": {"accepting_orders": True},
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


def test_lp_empty_preparation_clears_previous_selected_results(tmp_path: Path) -> None:
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
    assert first["selected_results"]
    first_account_reads = exchange.account_reads
    first_book_reads = exchange.book_reads

    clock["now"] += timedelta(seconds=1)
    service.refresh_price_history()
    empty = service.refresh_candidates(force=True)

    assert empty["state"] == "ready"
    assert empty["complete"] is True
    assert empty["selected_results"] == []
    assert empty["recommendations"] == []
    assert empty["selected_market_ids"] == []
    assert empty["funnel"]["catalog_read"] == 0
    assert empty["funnel"]["base_pass"] == 0
    assert empty["funnel"]["volatility_pass"] == 0
    assert empty["funnel"]["selected"] == 0
    assert empty["funnel"]["risk"] == {"passed": 0, "rejected": 0, "unknown": 0}
    assert empty["funnel"]["risk_directions"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 0,
    }
    assert exchange.account_reads == first_account_reads
    assert exchange.book_reads == first_book_reads

    persisted = store.lp_screening_snapshot()
    assert isinstance(persisted, Mapping)
    assert persisted["selected_results"] == []
    reopened = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: clock["now"]
    )
    reopened_snapshot = reopened.candidate_snapshot()
    assert reopened_snapshot["selected_results"] == []
    assert reopened_snapshot["recommendations"] == []
    assert reopened_snapshot["selected_market_ids"] == []


def test_lp_selected_event_end_confirmation_uses_fresh_metadata(tmp_path: Path) -> None:
    condition_id = "condition-event-fresh"
    token_id = "event-fresh-token"
    preparation_at = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
    event_observation_at = preparation_at + timedelta(minutes=1)
    clock = {"now": preparation_at}

    class Exchange:
        def __init__(self) -> None:
            self.metadata_reads = 0
            self.prepared_ended = False
            self.selected_ended = False
            self.selected_metadata_checked_at: datetime | None = None

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
                        "id": "event-fresh-reward",
                        "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                        "start_date": "2026-01-01",
                        "end_date": "2026-12-31",
                        "rate_per_day": Decimal("2"),
                    }
                ],
                "sponsored_reward_configs": [],
            }

        def _metadata(self) -> dict[str, object]:
            ended = self.prepared_ended if self.metadata_reads == 1 else self.selected_ended
            checked_at = (
                preparation_at if self.metadata_reads == 1 else clock["now"]
            )
            if self.metadata_reads > 1 and self.selected_metadata_checked_at is not None:
                checked_at = self.selected_metadata_checked_at
            return {
                "condition_id": condition_id,
                "market_id": "market-event-fresh",
                "metadata_checked_at": checked_at,
                "accepting_orders": True,
                "tick_size": Decimal("0.01"),
                "minimum_order_size": Decimal("1"),
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "fees_enabled": False,
                "event_id": "event-fresh",
                "game_id": "game-fresh",
                "game_start_time": preparation_at - timedelta(hours=2),
                "event_ended": ended,
                "event_finished_at": None,
                "outcomes": {"yes": {"label": "YES", "token_id": token_id}},
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock["now"],
                "markets": (self._reward(),),
            }

        def lp_market_metadata(
            self, condition_ids: object, *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            self.metadata_reads += 1
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
            return {
                token: {
                    "condition_id": condition_id,
                    "token_id": token,
                    "received_at": clock["now"],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": Decimal("0.49"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                }
                for token in tuple(token_ids)  # type: ignore[arg-type]
            }

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: clock["now"])
    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"

    exchange.selected_ended = True
    clock["now"] = event_observation_at
    ended = service.refresh_candidates(force=True)
    ended_row = ended["selected_results"][0]
    ended_direction = ended_row["directions"]["YES"]
    assert ended_direction["state"] == "rejected"
    assert ended_direction["reason_codes"] == ["event_recovery_pending"]
    saved = store.lp_screening_snapshot()
    assert isinstance(saved, Mapping)
    confirmation = saved["event_end_confirmations"][condition_id]
    assert isinstance(confirmation, Mapping)
    assert confirmation["confirmed_end_at"] == event_observation_at.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")

    clock["now"] = event_observation_at + timedelta(seconds=60)
    next_scan = service.refresh_candidates(force=True)
    next_confirmation = next_scan["event_end_confirmations"][condition_id]
    assert isinstance(next_confirmation, Mapping)
    assert next_confirmation["confirmed_end_at"] == confirmation["confirmed_end_at"]

    clock["now"] = event_observation_at + timedelta(hours=1)
    recovered = service.refresh_candidates(force=True)
    recovered_direction = recovered["selected_results"][0]["directions"]["YES"]
    assert recovered_direction["state"] == "eligible"
    recovered_confirmation = recovered["event_end_confirmations"][condition_id]
    assert isinstance(recovered_confirmation, Mapping)
    assert recovered_confirmation["confirmed_end_at"] == confirmation["confirmed_end_at"]

    cached_exchange = Exchange()
    cached_exchange.prepared_ended = True
    cached_store = PredictionArbitrageStore(tmp_path / "cached-ended")
    cached_service = PolymarketLPService(
        cached_store, cached_exchange, clock=lambda: clock["now"]
    )
    clock["now"] = preparation_at
    cached_preparation = cached_service.refresh_price_history()
    assert cached_preparation["state"] == "known"
    clock["now"] = preparation_at + timedelta(seconds=1)
    cached_service.refresh_candidates(force=True)
    cached_snapshot = cached_store.lp_screening_snapshot()
    assert isinstance(cached_snapshot, Mapping)
    assert condition_id not in cached_snapshot["event_end_confirmations"]

    future_exchange = Exchange()
    future_store = PredictionArbitrageStore(tmp_path / "future-ended")
    future_service = PolymarketLPService(
        future_store, future_exchange, clock=lambda: clock["now"]
    )
    clock["now"] = preparation_at
    future_preparation = future_service.refresh_price_history()
    assert future_preparation["state"] == "known"
    future_exchange.selected_ended = True
    future_exchange.selected_metadata_checked_at = event_observation_at + timedelta(
        minutes=5
    )
    clock["now"] = event_observation_at
    future_service.refresh_candidates(force=True)
    future_snapshot = future_store.lp_screening_snapshot()
    assert isinstance(future_snapshot, Mapping)
    assert condition_id not in future_snapshot["event_end_confirmations"]


def test_lp_recommendations_deduct_active_n_leg_cash_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "condition-lp"
    market = _lp_test_market(
        condition_id, yes_token="lp-yes", no_token="lp-no"
    )
    clock, state, store, _trading, service = _lp_adapter_service_fixture(
        tmp_path, monkeypatch, markets=[market]
    )

    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"

    def yes_direction(snapshot: Mapping[str, object]) -> Mapping[str, object]:
        rows = snapshot["recommendations"]
        if not isinstance(rows, list) or len(rows) != 1:
            rows = snapshot["selected_results"]
        assert isinstance(rows, list) and len(rows) == 1
        directions = rows[0]["directions"]
        assert isinstance(directions, Mapping)
        result = directions["YES"]
        assert isinstance(result, Mapping)
        return result

    without_n_leg = service.refresh_candidates(force=True)
    initial_yes = yes_direction(without_n_leg)
    assert initial_yes["state"] == "eligible"
    initial_guidance = initial_yes["guidance"]
    assert isinstance(initial_guidance, Mapping)
    assert Decimal(str(initial_guidance["price"])) == Decimal("0.50")
    assert Decimal(str(initial_guidance["quantity"])) == Decimal("90")
    assert Decimal(str(initial_guidance["required_capital"])) == Decimal("45.00")

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
    blocked_yes = yes_direction(blocked)
    assert blocked_yes["state"] == "rejected"
    assert blocked_yes["eligible"] is False
    assert blocked_yes["reason_codes"] == ["balance_insufficient"]
    assert blocked_yes["guidance"] is None

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

    clock["now"] += timedelta(seconds=METADATA_TTL + 1)
    reseed_history()
    market["reward_min_size"] = Decimal("72")
    exact_fit = service.refresh_candidates(force=True)
    exact_fit_yes = yes_direction(exact_fit)
    assert exact_fit_yes["state"] == "eligible"
    exact_fit_guidance = exact_fit_yes["guidance"]
    assert isinstance(exact_fit_guidance, Mapping)
    assert Decimal(str(exact_fit_guidance["quantity"])) == Decimal("72")
    assert Decimal(str(exact_fit_guidance["required_capital"])) == Decimal("36.00")

    acknowledged = store.n_leg_acknowledge_incident(
        batch_id,
        acknowledgement={"actor": "test", "reconciliation": "fresh_clean"},
    )
    assert acknowledged["state"] == "INCIDENT_ACKNOWLEDGED"
    assert store.n_leg_control()["active_batch_id"] is None
    assert store.n_leg_control()["total_unsettled_capital_units"] == 20_000_000

    clock["now"] += timedelta(seconds=METADATA_TTL + 1)
    reseed_history()
    market["reward_min_size"] = Decimal("90")
    after_acknowledgement = service.refresh_candidates(force=True)
    after_ack_yes = yes_direction(after_acknowledgement)
    assert after_ack_yes["state"] == "eligible"
    after_ack_guidance = after_ack_yes["guidance"]
    assert isinstance(after_ack_guidance, Mapping)
    assert Decimal(str(after_ack_guidance["required_capital"])) == Decimal("45.00")

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
    unknown_yes = yes_direction(unknown)
    assert unknown_yes["eligible"] is False
    assert "account_facts_unknown" in unknown_yes["reason_codes"]
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


def test_lp_recommendations_retain_expired_guidance_without_renewing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition_id = "condition-main"
    missing_condition_id = "condition-metadata-missing"
    yes_token = "yes-token"
    no_token = "no-token"
    clock = {"now": datetime(2026, 9, 15, 12, 0, tzinfo=UTC)}
    account_state: dict[str, object] = {
        "orders": [],
        "positions": [],
        "slow_orders": False,
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
            return SimpleNamespace(
                balance=1_000_000_000,
                allowances={"standard-exchange": 1_000_000_000},
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            if account_state["slow_orders"] is True:
                clock["now"] += timedelta(seconds=11)
            return list(account_state["orders"])  # type: ignore[arg-type]

        def list_account_trades(self, **_kwargs: object) -> list[object]:
            return []

        def list_positions(self, **_kwargs: object) -> list[object]:
            return list(account_state["positions"])  # type: ignore[arg-type]

    def reward_row(
        target: str, *, end_date: str = "2026-12-31"
    ) -> dict[str, object]:
        return {
            "condition_id": target,
            "rewards_min_size": Decimal("20"),
            "rewards_max_spread": Decimal("10"),
            "rewards_config": [
                {
                    "id": f"config-{target}",
                    "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                    "start_date": "2026-01-01",
                    "end_date": end_date,
                    "rate_per_day": Decimal("2"),
                }
            ],
        }

    def market_row(state: dict[str, object]) -> dict[str, object]:
        return {
            "id": "market-main",
            "condition_id": condition_id,
            "question": "Will the scheduled event happen?",
            "slug": "scheduled-event-market",
            "state": {"accepting_orders": True},
            "outcomes": {
                "yes": {"label": "Yes", "token_id": yes_token},
                "no": {"label": "No", "token_id": no_token},
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
            "events": [{"id": "event-main", "slug": "scheduled-event"}],
            "sports": {
                "game_id": "game-main",
                "game_start_time": datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
            },
            "prices": {"one_day_price_change": Decimal("0.02")},
        }

    class PublicSDK:
        def __init__(self, state: dict[str, object]) -> None:
            self.state = state

        def list_current_rewards(self, *, sponsored: bool) -> object:
            entered = self.state.get("catalog_entered")
            release = self.state.get("catalog_release")
            if isinstance(entered, threading.Event) and isinstance(release, threading.Event):
                entered.set()
                assert release.wait(timeout=5)
            if self.state.get("catalog_failure") is True:
                def failed_page() -> Iterator[object]:
                    yield reward_row(condition_id)
                    raise RuntimeError("synthetic reward page failure")

                return failed_page()
            if sponsored:
                return []
            end_date = str(self.state.get("reward_end_date") or "2026-12-31")
            return [
                reward_row(condition_id, end_date=end_date),
                reward_row(missing_condition_id, end_date=end_date),
            ]

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool
        ) -> list[object]:
            entered = self.state.get("catalog_entered")
            release = self.state.get("catalog_release")
            if isinstance(entered, threading.Event) and isinstance(
                release, threading.Event
            ):
                entered.set()
                assert release.wait(timeout=5)
            if sponsored:
                return []
            if self.state.get("catalog_failure") is True:
                raise RuntimeError("synthetic reward page failure")
            end_date = str(self.state.get("reward_end_date") or "2026-12-31")
            return [reward_row(condition_id, end_date=end_date)]

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            conditions = tuple(condition_ids)  # type: ignore[arg-type]
            return [market_row(self.state)] if condition_id in conditions else []

        def get_event(self, *, id: str) -> object:
            assert id == "event-main"
            ended = self.state.get("event_ended", False)
            finished_at = self.state.get("event_finished_at")
            if ended is True and finished_at is None:
                clock["now"] += timedelta(seconds=5)
            return {
                "id": "event-main",
                "slug": "scheduled-event",
                "state": {"ended": ended},
                "schedule": {
                    "start_time": datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
                    "finished_at": finished_at,
                },
            }

        def get_order_books(self, *, token_ids: object) -> list[object]:
            books: list[object] = []
            for token in tuple(token_ids):  # type: ignore[arg-type]
                if token == yes_token:
                    bid = self.state["yes_bid"]
                    ask = self.state["yes_ask"]
                elif token == no_token:
                    bid = self.state["no_bid"]
                    ask = self.state["no_ask"]
                else:
                    continue
                books.append(
                    {
                        "condition_id": condition_id,
                        "asset_id": token,
                        "timestamp": clock["now"] - timedelta(minutes=10),
                        "bids": [
                            {"price": bid, "size": Decimal("1")},
                            {"price": Decimal(str(bid)) - Decimal("0.01"), "size": Decimal("100")},
                        ],
                        "asks": [{"price": ask, "size": Decimal("100")}],
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

    def market_state(
        *,
        yes_bid: str = "0.49",
        yes_ask: str = "0.51",
        no_bid: str = "0.48",
        no_ask: str = "0.52",
        catalog_failure: bool = False,
        event_ended: bool = False,
        **extra: object,
    ) -> dict[str, object]:
        return {
            "yes_bid": Decimal(yes_bid),
            "yes_ask": Decimal(yes_ask),
            "no_bid": Decimal(no_bid),
            "no_ask": Decimal(no_ask),
            "catalog_failure": catalog_failure,
            "event_ended": event_ended,
            "event_finished_at": None,
            **extra,
        }

    def make_trading(state: dict[str, object]) -> PolymarketTradingClient:
        return PolymarketTradingClient(
            TradingConfig("signer", "wallet"),
            client=AccountSDK(),
            urlopen_fn=open_history,
            public_client_factory=lambda: PublicSDK(state),
        )

    def parsed_stamp(value: object) -> datetime:
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(UTC)

    def record_history(
        store: PredictionArbitrageStore,
        at: datetime,
        *,
        yes_bid: str = "0.49",
        yes_ask: str = "0.51",
        no_bid: str = "0.48",
        no_ask: str = "0.52",
    ) -> None:
        del yes_bid, yes_ask, no_bid, no_ask
        window_start = at - timedelta(hours=24)
        history_samples = [
            {"t": int(window_start.timestamp()), "p": Decimal("0.500")},
            {"t": int(at.timestamp()), "p": Decimal("0.505")},
        ]
        history_summary = {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "window_start": window_start,
            "window_end": at,
            "sample_count": len(history_samples),
            "checked_at": at,
            "valid_until": at + timedelta(hours=2),
        }
        store.lp_save_price_history_batch(
            {
                "condition_id": condition_id,
                "token_id": token,
                "samples": [dict(sample) for sample in history_samples],
                "summary": dict(history_summary),
            }
            for token in (yes_token, no_token)
        )

    state = market_state()
    trading = make_trading(state)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, trading, clock=lambda: clock["now"])
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert first["complete"] is False
    assert first["missing_metadata_condition_ids"] == [missing_condition_id]
    first_rows = first["recommendations"]
    assert isinstance(first_rows, list) and len(first_rows) == 1
    first_row = first_rows[0]
    assert isinstance(first_row, dict)
    assert first_row["condition_id"] == condition_id
    first_directions = first_row["directions"]
    assert isinstance(first_directions, dict)
    first_yes = first_directions["YES"]
    assert isinstance(first_yes, dict) and first_yes["state"] == "eligible"
    first_yes_screening = first_yes["screening"]
    assert isinstance(first_yes_screening, dict)
    assert first_yes_screening["state"] == "known"
    assert Decimal(str(first_yes_screening["amplitude"])) == Decimal("0.005")
    assert first_yes_screening["sample_count"] == 2
    assert parsed_stamp(first_yes_screening["window_start"]) == clock["now"] - timedelta(hours=24)
    assert parsed_stamp(first_yes_screening["window_end"]) == clock["now"]
    old_guidance = first_yes["guidance"]
    assert isinstance(old_guidance, dict)
    assert Decimal(str(old_guidance["price"])) == Decimal("0.49")
    old_checked_at = parsed_stamp(old_guidance["checked_at"])
    old_quantity = old_guidance["quantity"]
    no_guidance = first_directions["NO"]["guidance"]
    assert isinstance(no_guidance, dict)
    old_no_price = Decimal(str(no_guidance["price"]))
    old_no_quantity = no_guidance["quantity"]
    old_no_checked_at = parsed_stamp(no_guidance["checked_at"])

    split_expiry_snapshot = store.lp_screening_snapshot()
    assert isinstance(split_expiry_snapshot, dict)
    split_expiry_rows = split_expiry_snapshot["selected_results"]
    assert isinstance(split_expiry_rows, list) and len(split_expiry_rows) == 1
    split_expiry_directions = split_expiry_rows[0]["directions"]
    assert isinstance(split_expiry_directions, dict)
    split_expiry_directions["YES"]["guidance"]["expires_at"] = "2026-09-15T12:00:10Z"
    split_expiry_directions["NO"]["guidance"]["expires_at"] = "2026-09-15T12:01:00Z"
    store.lp_save_screening_snapshot(split_expiry_snapshot)
    lp = PolymarketLPService(store, trading, clock=lambda: clock["now"])
    assert lp.refresh_price_history()["state"] == "known"

    execution = PredictionExecutionService(
        store=store,
        monitor=_Monitor(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "prediction_arbitrage" / "execution.lock",
        lp=lp,
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
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, _server_instance):
        status, first_dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert status == 200
        first_dashboard_rows = first_dashboard["recommendations"]
        assert isinstance(first_dashboard_rows, list) and len(first_dashboard_rows) == 1
        assert first_dashboard["candidate_stale"] is False
        current_directions = first_dashboard_rows[0]["directions"]
        assert isinstance(current_directions, dict)
        assert current_directions["YES"]["eligible"] is True
        assert current_directions["NO"]["eligible"] is True
        current_yes_screening = current_directions["YES"]["screening"]
        assert isinstance(current_yes_screening, dict)
        assert Decimal(str(current_yes_screening["amplitude"])) == Decimal("0.005")
        assert current_yes_screening["sample_count"] == 2
        assert parsed_stamp(current_yes_screening["window_start"]) == clock["now"] - timedelta(hours=24)
        assert parsed_stamp(current_yes_screening["window_end"]) == clock["now"]

        clock["now"] = datetime(2026, 9, 15, 12, 0, 11, tzinfo=UTC)
        first_expiry_status, first_expiry_dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert first_expiry_status == 200
        assert first_expiry_dashboard["candidate_stale"] is False
        first_expiry_row = first_expiry_dashboard["recommendations"][0]
        assert isinstance(first_expiry_row, dict) and first_expiry_row["state"] == "eligible"
        first_expiry_directions = first_expiry_row["directions"]
        assert isinstance(first_expiry_directions, dict)
        assert first_expiry_directions["YES"]["state"] == "expired"
        assert first_expiry_directions["YES"]["eligible"] is False
        assert first_expiry_directions["NO"]["state"] == "eligible"
        assert first_expiry_directions["NO"]["eligible"] is True
        assert Decimal(str(first_expiry_directions["YES"]["guidance"]["price"])) == Decimal("0.49")
        assert Decimal(str(first_expiry_directions["YES"]["guidance"]["quantity"])) == Decimal(str(old_quantity))
        assert parsed_stamp(first_expiry_directions["YES"]["guidance"]["checked_at"]) == old_checked_at
        assert Decimal(str(first_expiry_directions["NO"]["guidance"]["price"])) == old_no_price
        assert Decimal(str(first_expiry_directions["NO"]["guidance"]["quantity"])) == Decimal(str(old_no_quantity))
        assert parsed_stamp(first_expiry_directions["NO"]["guidance"]["checked_at"]) == old_no_checked_at

        clock["now"] = datetime(2026, 9, 15, 12, 1, 0, tzinfo=UTC)
        second_expiry_status, second_expiry_dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert second_expiry_status == 200
        assert second_expiry_dashboard["recommendations"] == []
        second_expiry_row = second_expiry_dashboard["selected_results"][0]
        assert isinstance(second_expiry_row, dict) and second_expiry_row["state"] == "expired"
        second_expiry_directions = second_expiry_row["directions"]
        assert isinstance(second_expiry_directions, dict)
        assert second_expiry_directions["NO"]["state"] == "expired"
        assert second_expiry_directions["NO"]["eligible"] is False
        assert Decimal(str(second_expiry_directions["NO"]["guidance"]["price"])) == old_no_price
        assert Decimal(str(second_expiry_directions["NO"]["guidance"]["quantity"])) == Decimal(str(old_no_quantity))
        assert parsed_stamp(second_expiry_directions["NO"]["guidance"]["checked_at"]) == old_no_checked_at

        clock["now"] = old_checked_at + timedelta(seconds=61)
        expired_status, expired_dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert expired_status == 200
        assert expired_dashboard["recommendations"] == []
        expired_rows = expired_dashboard["selected_results"]
        assert isinstance(expired_rows, list) and len(expired_rows) == 1
        expired_row = expired_rows[0]
        assert isinstance(expired_row, dict) and expired_row["state"] == "expired"
        expired_directions = expired_row["directions"]
        assert isinstance(expired_directions, dict)
        expired_yes = expired_directions["YES"]
        assert isinstance(expired_yes, dict)
        assert expired_yes["state"] == "expired"
        assert expired_yes["eligible"] is False
        assert Decimal(str(expired_yes["guidance"]["price"])) == Decimal("0.49")
        assert Decimal(str(expired_yes["guidance"]["quantity"])) == Decimal(str(old_quantity))
        assert parsed_stamp(expired_yes["guidance"]["checked_at"]) == old_checked_at

        confirmation_at = datetime(2026, 9, 15, 14, 5, tzinfo=UTC)
        confirmed_end_at = confirmation_at
        clock["now"] = confirmation_at
        state["event_ended"] = True
        record_history(store, confirmation_at)
        ended = lp.refresh_candidates(force=True)
        ended_yes = ended["selected_results"][0]["directions"]["YES"]
        assert ended_yes["state"] == "rejected"
        assert ended_yes["eligible"] is False
        assert ended_yes["reason_codes"] == ["event_recovery_pending"]
        assert ended_yes["guidance"] is None

        saved = store.lp_screening_snapshot()
        assert isinstance(saved, dict)
        confirmations = saved["event_end_confirmations"]
        assert isinstance(confirmations, dict)
        confirmed = confirmations[condition_id]
        assert isinstance(confirmed, dict)
        first_confirmation_time = confirmed["confirmed_end_at"]
        assert parsed_stamp(first_confirmation_time) == confirmed_end_at

        reopened_store = PredictionArbitrageStore(tmp_path)
        reopened_lp = PolymarketLPService(
            reopened_store, trading, clock=lambda: clock["now"]
        )
        clock["now"] = confirmation_at + timedelta(seconds=10)
        record_history(reopened_store, clock["now"])
        assert reopened_lp.refresh_price_history()["state"] == "known"
        refreshed = reopened_lp.refresh_candidates(force=True)
        refreshed_yes = refreshed["selected_results"][0]["directions"]["YES"]
        assert refreshed_yes["state"] == "rejected"
        assert refreshed_yes["guidance"] is None
        reopened_confirmation = reopened_store.lp_screening_snapshot()
        assert isinstance(reopened_confirmation, dict)
        reopened_confirmations = reopened_confirmation["event_end_confirmations"]
        assert isinstance(reopened_confirmations, dict)
        assert parsed_stamp(reopened_confirmations[condition_id]["confirmed_end_at"]) == confirmed_end_at

        state["catalog_failure"] = True
        clock["now"] = confirmation_at + timedelta(seconds=61)
        failed_preparation = reopened_lp.refresh_price_history()
        assert failed_preparation["state"] == "unknown"
        failed = reopened_lp.refresh_candidates(force=True)
        assert failed["complete"] is False
        failed_yes = failed["selected_results"][0]["directions"]["YES"]
        assert failed_yes["state"] == "unknown"
        assert failed_yes["eligible"] is False
        assert failed_yes["reason_codes"] == ["reward_data_unknown"]
        assert failed_yes["guidance"] is None
        reopened_execution = PredictionExecutionService(
            store=reopened_store,
            monitor=_Monitor(),
            trading=trading,
            notifier=NullNotifier(),
            lock_path=tmp_path / "prediction_arbitrage" / "reopened-execution.lock",
            lp=reopened_lp,
        )
        reopened_runtime = SimpleNamespace(
            mode="production",
            state="RUNNING",
            production_owner=True,
            store=reopened_store,
            monitor=_Monitor(),
            execution=reopened_execution,
            cross_venue_monitor=None,
        )
        with _running_server(
            reopened_runtime,
            session_token="session-token",
            csrf_token="csrf-token",
            runtime_metadata={"git_sha": "abc123"},
        ) as (reopened_base, _reopened_server_instance):
            failed_status, failed_dashboard = _response(
                reopened_base + "/api/prediction-arbitrage/lp/dashboard"
            )
            assert failed_status == 200
            failed_dashboard_yes = failed_dashboard["selected_results"][0]["directions"]["YES"]
            assert failed_dashboard_yes["eligible"] is False
            assert failed_dashboard_yes["guidance"] is None

        state["catalog_failure"] = False
        state["yes_bid"] = Decimal("0.48")
        state["yes_ask"] = Decimal("0.52")
        state["no_bid"] = Decimal("0.47")
        state["no_ask"] = Decimal("0.53")
        recovered_at = confirmed_end_at + timedelta(hours=1, seconds=1)
        clock["now"] = recovered_at
        record_history(
            reopened_store,
            recovered_at,
            yes_bid="0.48",
            yes_ask="0.52",
            no_bid="0.47",
            no_ask="0.53",
        )
        recovered = reopened_lp.refresh_candidates(force=True)
        recovered_yes = recovered["recommendations"][0]["directions"]["YES"]
        assert recovered_yes["state"] == "eligible"
        assert Decimal(str(recovered_yes["guidance"]["price"])) == Decimal("0.48")
        assert parsed_stamp(recovered_yes["guidance"]["checked_at"]) > old_checked_at
        latest_saved = reopened_store.lp_screening_snapshot()
        assert isinstance(latest_saved, dict)
        latest_confirmations = latest_saved["event_end_confirmations"]
        assert isinstance(latest_confirmations, dict)
        assert parsed_stamp(latest_confirmations[condition_id]["confirmed_end_at"]) == confirmed_end_at

        old_state = market_state(yes_bid="0.48", yes_ask="0.52", no_bid="0.47", no_ask="0.53", event_ended=True)
        old_store = PredictionArbitrageStore(tmp_path)
        old_lp = PolymarketLPService(
            old_store,
            make_trading(old_state),
            clock=lambda: clock["now"],
        )
        record_history(
            old_store,
            clock["now"],
            yes_bid="0.48",
            yes_ask="0.52",
            no_bid="0.47",
            no_ask="0.53",
        )
        old_preparation = old_lp.refresh_price_history()
        assert old_preparation["state"] == "known"
        state["catalog_entered"] = threading.Event()
        state["catalog_release"] = threading.Event()
        old_state["catalog_entered"] = state["catalog_entered"]
        old_state["catalog_release"] = state["catalog_release"]
        old_result: dict[str, object] = {}
        old_error: list[BaseException] = []

        def run_old_scan() -> None:
            try:
                old_result.update(old_lp.refresh_candidates(force=True))
            except BaseException as exc:
                old_error.append(exc)

        clock["now"] = recovered_at + timedelta(seconds=1)
        record_history(
            reopened_store,
            clock["now"],
            yes_bid="0.48",
            yes_ask="0.52",
            no_bid="0.47",
            no_ask="0.53",
        )
        old_thread = threading.Thread(target=run_old_scan, daemon=True)
        old_thread.start()
        entered = state["catalog_entered"]
        release = state["catalog_release"]
        assert isinstance(entered, threading.Event) and entered.wait(timeout=5)

        newer_state = market_state(yes_bid="0.47", yes_ask="0.53", no_bid="0.46", no_ask="0.54", event_ended=True)
        clock["now"] = recovered_at + timedelta(seconds=2)
        record_history(
            reopened_store,
            clock["now"],
            yes_bid="0.47",
            yes_ask="0.53",
            no_bid="0.46",
            no_ask="0.54",
        )
        newer_trading = make_trading(newer_state)
        newer_lp = PolymarketLPService(
            PredictionArbitrageStore(tmp_path), newer_trading, clock=lambda: clock["now"]
        )
        assert newer_lp.refresh_price_history()["state"] == "known"
        newer = newer_lp.refresh_candidates(force=True)
        newer_yes = newer["recommendations"][0]["directions"]["YES"]
        assert Decimal(str(newer_yes["guidance"]["price"])) == Decimal("0.47")
        assert isinstance(release, threading.Event)
        release.set()
        old_thread.join(timeout=5)
        assert not old_thread.is_alive()
        assert not old_error
        after_old = PredictionArbitrageStore(tmp_path).lp_screening_snapshot()
        assert isinstance(after_old, dict)
        saved_rows = after_old["recommendations"]
        assert isinstance(saved_rows, list) and len(saved_rows) == 1
        saved_yes = saved_rows[0]["directions"]["YES"]
        assert Decimal(str(saved_yes["guidance"]["price"])) == Decimal("0.47")

    account_state["slow_orders"] = True
    account_read_started = clock["now"]
    stale_account = newer_trading.lp_account_snapshot()
    assert stale_account["checked_at"] == account_read_started
    assert (clock["now"] - stale_account["checked_at"]).total_seconds() == 11
    account_state["slow_orders"] = False
    account_state["orders"] = [
        {"side": "BUY", "status": "LIVE"},
        {
            "id": "missing-size-order",
            "asset_id": "other-token",
            "condition_id": "other-condition",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.47"),
        },
    ]
    account_state["positions"] = [{}]
    incomplete_account = newer_trading.lp_account_snapshot()
    assert len(incomplete_account["open_orders"]) == 1
    assert incomplete_account["open_orders_complete"] is False
    assert incomplete_account["positions_complete"] is False
    record_history(
        reopened_store,
        clock["now"],
        yes_bid="0.47",
        yes_ask="0.53",
        no_bid="0.46",
        no_ask="0.54",
    )
    incomplete_scan = newer_lp.refresh_candidates(force=True)
    incomplete_yes = incomplete_scan["selected_results"][0]["directions"]["YES"]
    assert incomplete_yes["state"] != "eligible"
    assert incomplete_yes["eligible"] is False
    assert "account_facts_unknown" in incomplete_yes["reason_codes"]

    midnight_read_at = datetime(2026, 9, 15, 23, 59, 30, tzinfo=UTC)
    reward_deadline = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    state["event_finished_at"] = confirmed_end_at
    state["reward_end_date"] = "2026-09-15"
    account_state["orders"] = []
    account_state["positions"] = []
    clock["now"] = midnight_read_at
    record_history(
        reopened_store,
        midnight_read_at,
        yes_bid="0.48",
        yes_ask="0.52",
        no_bid="0.47",
        no_ask="0.53",
    )
    before_reward_expiry = reopened_lp.refresh_candidates(force=True)
    before_reward_expiry_yes = before_reward_expiry["recommendations"][0]["directions"]["YES"]
    assert before_reward_expiry_yes["state"] == "eligible"
    reward_guidance = before_reward_expiry_yes["guidance"]
    assert isinstance(reward_guidance, dict)
    assert Decimal(str(reward_guidance["price"])) == Decimal("0.48")
    assert parsed_stamp(reward_guidance["checked_at"]) == midnight_read_at
    assert parsed_stamp(reward_guidance["expires_at"]) == reward_deadline

    clock["now"] = reward_deadline + timedelta(seconds=1)
    with _running_server(
        reopened_runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (reward_expiry_base, _reward_expiry_server):
        expired_status, expired_dashboard = _response(
            reward_expiry_base + "/api/prediction-arbitrage/lp/dashboard"
        )
    assert expired_status == 200
    assert expired_dashboard["recommendations"] == []
    expired_reward_rows = expired_dashboard["selected_results"]
    assert isinstance(expired_reward_rows, list) and len(expired_reward_rows) == 1
    expired_reward_row = expired_reward_rows[0]
    assert isinstance(expired_reward_row, dict)
    assert expired_reward_row["state"] == "expired"
    expired_reward_yes = expired_reward_row["directions"]["YES"]
    assert isinstance(expired_reward_yes, dict)
    assert expired_reward_yes["eligible"] is False
    assert expired_reward_yes["state"] == "expired"
    assert Decimal(str(expired_reward_yes["guidance"]["price"])) == Decimal("0.48")
    assert Decimal(str(expired_reward_yes["guidance"]["quantity"])) == Decimal(
        str(reward_guidance["quantity"])
    )
    assert parsed_stamp(expired_reward_yes["guidance"]["checked_at"]) == midnight_read_at


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
            diagnostic_rows = dashboard["selected_results"]
            assert isinstance(diagnostic_rows, list) and len(diagnostic_rows) == 1
            saved_direction = diagnostic_rows[0]["directions"]["YES"]
            assert saved_direction["state"] == "unknown"
            assert saved_direction["eligible"] is False
            assert saved_direction["guidance"]["price"] == "0.49"
            assert saved_direction["guidance"]["quantity"] == "20"
            assert saved_direction["guidance"]["checked_at"] == now.isoformat().replace(
                "+00:00", "Z"
            )
            assert saved_direction["guidance"]["expires_at"] == (
                now + timedelta(seconds=45)
            ).isoformat().replace("+00:00", "Z")

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
        assert set(payload) == {"venues", "monitor_subscription", "csrf_token"}
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
                    **(
                        {
                            "event_id": "event-M01",
                            "event_ended": True,
                            "event_finished_at": initial_now - timedelta(hours=2),
                        }
                        if market_id == "M01"
                        else {}
                    ),
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
            expected = {f"token-{market_id}" for market_id in markets[:50]}
            if set(token_ids) != expected:
                raise AssertionError("risk books requested outside selected markets")
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
    snapshot = service.refresh_candidates(force=True)
    assert [row["market_id"] for row in snapshot["recommendations"]] == list(markets[:50])
    assert exchange.book_requests == [tuple(f"token-{market_id}" for market_id in markets[:50])]
    assert snapshot["funnel"]["catalog_read"] == 51
    assert snapshot["funnel"]["base_pass"] == 51
    assert snapshot["funnel"]["volatility_pass"] == 51
    assert snapshot["funnel"]["selected"] == 50
    assert snapshot["funnel"]["risk"] == {"passed": 50, "rejected": 0, "unknown": 0}
    conditions = snapshot["funnel"]["conditions"]
    assert conditions == {
        "read": {
            "来源": "奖励目录与市场资料",
            "完整性": "完整目录；部分结果可参与筛选；缺失资料=UNKNOWN",
        },
        "filter": {
            "奖励": "奖励启用且日奖池>0",
            "市场": "接受订单",
            "参与": "没有已知订单或持仓",
            "窗口": "24h",
            "粒度": "1m",
            "振幅": "不超过1¢",
            "刷新": "每小时",
            "有效期": "2h",
            "缺失": "UNKNOWN",
        },
        "risk": {
            "奖励与市场资料": "60s内",
            "盘口与账户": "10s内；订单与持仓资料完整",
            "事件": "开始前30分钟、进行中、结束后1h冷却；结束后筛选必须通过；缺失=UNKNOWN",
            "入场压力": "最小数量、奖励价带、资金预留、含费压力退出不超过10%",
            "排序": "日奖池降序，同额按市场ID升序",
            "上限": 50,
        },
    }
    assert any(
        reason["market_id"] == "M51" and reason["code"] == "shortlist_cap"
        for reason in snapshot["funnel"]["reasons"]["selected"]
    )
    assert all(row["state"] == "eligible" for row in snapshot["recommendations"])


def test_lp_selected_risk_uses_fresh_facts_after_slow_preparation(
    tmp_path: Path,
) -> None:
    prepared_at = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
    now = [prepared_at]
    markets = tuple(f"M{index:02d}" for index in range(1, 52))
    selected_conditions = tuple(f"condition-{market_id}" for market_id in markets[:50])

    class Exchange:
        def __init__(self) -> None:
            self.catalog_calls = 0
            self.full_metadata_calls = 0
            self.selected_metadata_calls: list[tuple[str, ...]] = []
            self.selected_reward_calls: list[tuple[tuple[str, ...], bool]] = []
            self.book_requests: list[tuple[str, ...]] = []

        @staticmethod
        def _market(market_id: str, checked_at: datetime) -> dict[str, object]:
            condition_id = f"condition-{market_id}"
            return {
                "market_id": market_id,
                "condition_id": condition_id,
                "market_title": market_id,
                "accepting_orders": True,
                "metadata_checked_at": checked_at,
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

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            if condition_ids is not None:
                selected = tuple(condition_ids)
                self.selected_reward_calls.extend(
                    (selected, sponsored) for sponsored in (False, True)
                )
                checked_at = now[0]
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": checked_at,
                    "markets": tuple(
                        {
                            "condition_id": condition_id,
                            "daily_pool_usd": Decimal(
                                700 - markets.index(condition_id.removeprefix("condition-"))
                            ),
                            "reward_active": True,
                            "native_reward_configs": [
                                {
                                    "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                                    "rate_per_day": Decimal("1"),
                                    "end_date": "2500-12-31",
                                }
                            ],
                        }
                        for condition_id in selected
                    ),
                }
            self.catalog_calls += 1
            return {
                "state": "known",
                "complete": True,
                "checked_at": prepared_at,
                "markets": tuple(
                    {
                        "condition_id": f"condition-{market_id}",
                        "daily_pool_usd": Decimal(700 - index),
                        "reward_active": True,
                    }
                    for index, market_id in enumerate(markets)
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            requested = tuple(condition_ids)
            if len(requested) == len(markets):
                self.full_metadata_calls += 1
            else:
                self.selected_metadata_calls.append(requested)
            checked_at = now[0]
            return {
                f"condition-{market_id}": self._market(market_id, checked_at)
                for market_id in markets
                if f"condition-{market_id}" in requested
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
                        {"t": start_ts, "p": "0.50"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
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
            self.book_requests.append(tuple(token_ids))
            assert set(token_ids) == {f"token-{market_id}" for market_id in markets[:50]}
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

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now[0])

    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"
    assert exchange.catalog_calls == 1
    assert exchange.full_metadata_calls == 1

    now[0] = prepared_at + timedelta(seconds=137)
    first = service.refresh_candidates(force=True)
    assert first["state"] == "ready"
    assert first["funnel"]["selected"] == 50
    assert first["funnel"]["risk"] == {"passed": 50, "rejected": 0, "unknown": 0}
    assert len(first["recommendations"]) == 50
    guidance = first["recommendations"][0]["directions"]["YES"]["guidance"]
    assert guidance["required_capital"] == "10.00"
    assert guidance["estimated_exit_loss"] == "0.20"
    assert guidance["estimated_exit_loss_ratio"] == "0.02"
    assert exchange.catalog_calls == 1
    assert exchange.selected_metadata_calls == [selected_conditions]
    assert exchange.selected_reward_calls == [
        (selected_conditions, False),
        (selected_conditions, True),
    ]
    assert exchange.book_requests == [
        tuple(f"token-{market_id}" for market_id in markets[:50])
    ]
    assert all(
        market_id not in exchange.selected_metadata_calls[0]
        for market_id in ("condition-M51",)
    )

    now[0] += timedelta(minutes=1)
    second = service.refresh_candidates(force=True)
    assert second["funnel"]["risk"] == {"passed": 50, "rejected": 0, "unknown": 0}
    assert exchange.catalog_calls == 1
    assert exchange.full_metadata_calls == 1
    assert exchange.selected_metadata_calls == [selected_conditions, selected_conditions]
    assert len(exchange.selected_reward_calls) == 4
    assert len(exchange.book_requests) == 2


def test_lp_dashboard_recommendations_match_current_risk_results(
    tmp_path: Path,
) -> None:
    checked_at = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
    now = [checked_at]
    account_available = [True]

    class Exchange:
        config = SimpleNamespace(wallet_address="wallet")

        def __init__(self, market_ids: tuple[str, ...], *, unknown_books: bool = False) -> None:
            self.market_ids = market_ids
            self.unknown_books = unknown_books

        @staticmethod
        def _market(market_id: str, observed_at: datetime) -> dict[str, object]:
            condition_id = f"condition-{market_id}"
            return {
                "market_id": market_id,
                "condition_id": condition_id,
                "market_title": f"Market {market_id}",
                "accepting_orders": True,
                "metadata_checked_at": observed_at,
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

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = (
                tuple(f"condition-{market_id}" for market_id in self.market_ids)
                if condition_ids is None
                else tuple(condition_ids)
            )
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": tuple(
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal(
                            100 - self.market_ids.index(
                                condition_id.removeprefix("condition-")
                            )
                        ),
                        "reward_active": True,
                        "native_reward_configs": [
                            {
                                "id": f"config-{condition_id}",
                                "asset_address": (
                                    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                                ),
                                "rate_per_day": Decimal("1"),
                                "start_date": "2026-01-01",
                                "end_date": "2500-12-31",
                            }
                        ],
                    }
                    for condition_id in requested
                ),
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                f"condition-{market_id}": self._market(market_id, now[0])
                for market_id in self.market_ids
                if f"condition-{market_id}" in condition_ids
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
                        {"t": start_ts, "p": "0.50"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            if not account_available[0]:
                raise RuntimeError("account unavailable")
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
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            if self.unknown_books:
                return {}
            books: dict[str, dict[str, object]] = {}
            for token_id in token_ids:
                market_id = token_id.removeprefix("token-")
                if market_id == "C":
                    continue
                bids = (
                    [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ]
                    if market_id == "A"
                    else [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.40"), "size": Decimal("20")},
                    ]
                )
                books[token_id] = {
                    "condition_id": f"condition-{market_id}",
                    "token_id": token_id,
                    "received_at": now[0],
                    "bids": bids,
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
            return books

    def save_history(store: PredictionArbitrageStore, market_id: str) -> None:
        store.lp_save_price_history(
            f"condition-{market_id}",
            f"token-{market_id}",
            [],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "window_start": checked_at - timedelta(hours=24),
                "window_end": checked_at,
                "sample_count": 2,
                "checked_at": checked_at,
                "valid_until": checked_at + timedelta(hours=2),
            },
        )

    store = PredictionArbitrageStore(tmp_path / "mixed")
    for market_id in ("A", "B", "C"):
        save_history(store, market_id)
    exchange = Exchange(("A", "B", "C"))
    lp = PolymarketLPService(store, exchange, clock=lambda: now[0])
    assert lp.refresh_price_history()["state"] == "known"
    first_scan = lp.refresh_candidates(force=True)
    assert first_scan["funnel"]["selected"] == 3
    assert first_scan["funnel"]["risk"] == {
        "passed": 1,
        "rejected": 1,
        "unknown": 1,
    }
    assert [row["market_id"] for row in first_scan["recommendations"]] == ["A"]
    assert {
        row["market_id"]: row["state"]
        for row in first_scan["selected_results"]
    } == {"A": "eligible", "B": "rejected", "C": "unknown"}
    first_checked_at = first_scan["checked_at"]
    first_expiry = first_scan["recommendations"][0]["directions"]["YES"]["guidance"]["expires_at"]

    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=exchange,
        notifier=NullNotifier(),
        lock_path=tmp_path / "mixed-execution.lock",
        lp=lp,
    )
    dashboard = execution.lp_dashboard()
    assert [row["market_id"] for row in dashboard["recommendations"]] == ["A"]
    assert dashboard["funnel"]["risk"] == {
        "passed": 1,
        "rejected": 1,
        "unknown": 1,
    }
    assert any(
        reason["market_id"] == "B"
        for reason in dashboard["funnel"]["reasons"]["risk"]
    )
    assert any(
        reason["market_id"] == "C"
        for reason in dashboard["funnel"]["reasons"]["risk"]
    )

    now[0] = checked_at + timedelta(seconds=61)
    expired = lp.candidate_snapshot()
    assert expired["checked_at"] == first_checked_at
    assert expired["recommendations"] == []
    assert expired["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 1,
        "unknown": 2,
    }
    assert any(
        reason["code"] == "guidance_expired"
        for reason in expired["funnel"]["reasons"]["risk"]
    )
    assert {
        row["market_id"]: row["state"]
        for row in expired["selected_results"]
    } == {"A": "expired", "B": "rejected", "C": "unknown"}
    assert first_expiry != expired["checked_at"]

    account_available[0] = False
    outage = execution.lp_dashboard()
    assert outage["recommendations"] == []
    assert outage["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 1,
        "unknown": 2,
    }

    unknown_store = PredictionArbitrageStore(tmp_path / "unknown")
    unknown_ids = tuple(f"U{index:02d}" for index in range(1, 44))
    for market_id in unknown_ids:
        save_history(unknown_store, market_id)
    unknown_exchange = Exchange(unknown_ids, unknown_books=True)
    unknown_lp = PolymarketLPService(
        unknown_store,
        unknown_exchange,
        clock=lambda: now[0],
    )
    assert unknown_lp.refresh_price_history()["state"] == "known"
    all_unknown = unknown_lp.refresh_candidates(force=True)
    assert all_unknown["funnel"]["selected"] == 43
    assert all_unknown["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 43,
    }
    assert all_unknown["recommendations"] == []
    assert len(all_unknown["selected_results"]) == 43
    assert all(row["state"] == "unknown" for row in all_unknown["selected_results"])


def test_lp_refresh_preserves_selection_after_risk_without_backfill(tmp_path: Path) -> None:
    now = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    markets = tuple(f"M{index:02d}" for index in range(1, 52))

    class Exchange:
        def __init__(self) -> None:
            self.book_requests: list[tuple[str, ...]] = []

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
                "checked_at": now,
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
                f"condition-{market_id}": {
                    "market_id": market_id,
                    "condition_id": f"condition-{market_id}",
                    "market_title": market_id,
                    "accepting_orders": True,
                    "metadata_checked_at": now,
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": f"token-{market_id}"},
                        **(
                            {
                                "no": {
                                    "label": "NO",
                                    "token_id": f"token-{market_id}-no",
                                }
                            }
                            if market_id in {"M01", "M02"}
                            else {}
                        ),
                    },
                }
                for market_id in markets
                if f"condition-{market_id}" in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            self.book_requests.append(tuple(token_ids))
            expected = {f"token-{market_id}" for market_id in markets[:50]}
            expected.add("token-M02-no")
            if set(token_ids) != expected:
                raise AssertionError(
                    f"unexpected tokens: {set(token_ids) ^ expected}"
                )
            assert "token-M01-no" not in token_ids
            result: dict[str, dict[str, object]] = {}
            for token_id in token_ids:
                market_id = token_id.removeprefix("token-").removesuffix("-no")
                ordinal = int(market_id.removeprefix("M"))
                if ordinal > 45:
                    continue
                exit_bid = Decimal("0.49") if ordinal <= 30 else Decimal("0.40")
                result[token_id] = {
                    "condition_id": f"condition-{market_id}",
                    "token_id": token_id,
                    "received_at": now,
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": exit_bid, "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
            return result

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
                        {
                            "t": end_ts,
                            "p": "0.511" if token_id == "token-M01-no" else "0.505",
                        },
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

    store = PredictionArbitrageStore(tmp_path)
    for market_id in markets:
        store.lp_save_price_history(
            f"condition-{market_id}",
            f"token-{market_id}",
            [],
            {
                "state": "known",
                "amplitude": Decimal("0.005"),
                "checked_at": now,
                "window_start": now - timedelta(hours=24),
                "window_end": now,
            },
        )
        if market_id == "M01":
            store.lp_save_price_history(
                f"condition-{market_id}",
                f"token-{market_id}-no",
                [],
                {
                    "state": "known",
                    "amplitude": Decimal("0.0101"),
                    "checked_at": now,
                    "window_start": now - timedelta(hours=24),
                    "window_end": now,
                },
            )
        if market_id == "M02":
            store.lp_save_price_history(
                f"condition-{market_id}",
                f"token-{market_id}-no",
                [],
                {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": now,
                    "window_start": now - timedelta(hours=24),
                    "window_end": now,
                },
            )
    exchange = Exchange()
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    assert service.refresh_price_history()["state"] == "known"
    snapshot = service.refresh_candidates(force=True)
    recommendations = snapshot["recommendations"]
    assert [row["market_id"] for row in recommendations] == list(markets[:30])
    assert snapshot["selected_market_ids"] == list(markets[:50])
    selected_results = snapshot["selected_results"]
    states = [
        direction["state"]
        for row in selected_results
        for direction in row["directions"].values()
    ]
    market_states = [row["state"] for row in selected_results]
    assert states.count("eligible") == 31
    assert sum(
        1
        for row in selected_results
        for outcome in row["directions"]
        if outcome == "NO" and row["market_id"] == "M02"
    ) == 1
    assert selected_results[1]["directions"]["NO"]["state"] == "eligible"
    assert states.count("rejected") == 15
    assert states.count("unknown") == 5
    assert market_states.count("eligible") == 30
    assert market_states.count("rejected") == 15
    assert market_states.count("unknown") == 5
    assert snapshot["funnel"]["risk"] == {"passed": 30, "rejected": 15, "unknown": 5}
    assert Decimal("0.20") / Decimal("10") == Decimal("0.02")
    assert Decimal("2") / Decimal("10") == Decimal("0.20")


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
    assert [row["market_id"] for row in first["recommendations"]] == ["M1", "M2"]
    first_last_success = first["last_success_at"]
    now[0] = first_now + timedelta(minutes=2)
    failed_preparation = service.refresh_price_history()
    assert failed_preparation["state"] == "unknown"
    stale = service.candidate_snapshot()
    assert stale["state"] == "ready"
    assert stale["stale"] is True
    assert stale["last_success_at"] == first_last_success
    assert stale["recommendations"] == []
    assert all(row["state"] == "expired" for row in stale["selected_results"])
    assert stale["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 2,
    }
    assert stale["selected_market_ids"] == ["M1", "M2"]
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
    assert [row["market_id"] for row in second_replacement["recommendations"]] == ["M3"]
    assert second_replacement["selected_market_ids"] == ["M3"]
    assert replacement_exchange.book_requests == [
        ("token-M1", "token-M2"),
        ("token-M3",),
    ]

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
    assert legacy_snapshot["funnel"] == {}

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
    assert partial_service.refresh_price_history()["state"] == "known"
    partial = partial_service.refresh_candidates(force=True)
    assert partial["state"] == "incomplete"
    assert partial["complete"] is False
    assert [row["market_id"] for row in partial["recommendations"]] == ["M4"]
    assert partial["funnel"]["catalog_read"] == 1
    assert partial["funnel"]["base_pass"] == 1
    assert partial["funnel"]["volatility_pass"] == 1
    assert partial["funnel"]["selected"] == 1
    assert partial["funnel"]["risk"] == {"passed": 1, "rejected": 0, "unknown": 0}

    partial_exchange.account_failure = True
    partial_failed = partial_service.refresh_candidates(force=True)
    assert partial_failed["state"] == "incomplete"
    assert partial_failed["complete"] is False
    assert partial_failed["recommendations"] == []
    assert [row["market_id"] for row in partial_failed["selected_results"]] == ["M4"]
    assert partial_failed["funnel"]["selected"] == 1
    assert partial_failed["funnel"]["risk"] == {"passed": 0, "rejected": 0, "unknown": 1}

    partial_exchange.account_failure = False
    partial_exchange.book_failure = True
    partial_books_failed = partial_service.refresh_candidates(force=True)
    assert partial_books_failed["state"] == "incomplete"
    assert partial_books_failed["funnel"]["catalog_read"] == 1
    assert partial_books_failed["funnel"]["base_pass"] == 1
    assert partial_books_failed["funnel"]["volatility_pass"] == 1
    assert partial_books_failed["funnel"]["selected"] == 1
    assert partial_books_failed["funnel"]["risk"] == {"passed": 0, "rejected": 0, "unknown": 1}
    assert partial_books_failed["recommendations"] == []
    assert [row["market_id"] for row in partial_books_failed["selected_results"]] == ["M4"]


def test_lp_candidate_snapshot_marks_minute_risk_expired_without_refreshing(
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
    assert first["funnel"]["selected"] == 1
    assert first["funnel"]["risk"] == {"passed": 1, "rejected": 0, "unknown": 0}
    first_checked_at = first["checked_at"]
    first_funnel = first["funnel"]
    first_calls = dict(exchange.calls)

    now[0] = first_now + timedelta(seconds=59)
    fresh = service.candidate_snapshot()
    assert fresh["stale"] is False
    assert fresh["checked_at"] == first_checked_at
    assert fresh["selected_market_ids"] == ["market-minute"]
    assert fresh["funnel"] == first_funnel
    assert fresh["recommendations"][0]["directions"]["YES"]["state"] == "eligible"
    assert exchange.calls == first_calls

    now[0] = first_now + timedelta(seconds=60)
    exactly_expired = service.candidate_snapshot()
    assert exactly_expired["stale"] is True
    assert exactly_expired["checked_at"] == first_checked_at
    assert exactly_expired["selected_market_ids"] == ["market-minute"]
    assert exactly_expired["funnel"]["selected"] == first_funnel["selected"]
    assert exactly_expired["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 1,
    }
    expired_direction = exactly_expired["selected_results"][0]["directions"]["YES"]
    assert expired_direction["state"] == "expired"
    assert expired_direction["eligible"] is False
    assert exactly_expired["recommendations"] == []
    assert exactly_expired["selected_results"][0]["directions"]["YES"]["state"] == "expired"
    assert exchange.calls == first_calls

    now[0] = first_now + timedelta(seconds=61)
    historical = service.candidate_snapshot()
    assert historical["stale"] is True
    assert historical["checked_at"] == first_checked_at
    assert historical["selected_market_ids"] == ["market-minute"]
    assert historical["funnel"]["selected"] == first_funnel["selected"]
    assert historical["funnel"]["risk"] == {
        "passed": 0,
        "rejected": 0,
        "unknown": 1,
    }
    assert historical["recommendations"] == []
    assert historical["selected_results"][0]["directions"]["YES"]["state"] == "expired"
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
    assert snapshot["recommendations"] == []
    funnel = snapshot["funnel"]
    assert funnel["catalog_read"] == 0
    assert funnel["base_pass"] == 0
    assert funnel["volatility_pass"] == 0
    assert funnel["selected"] == 0
    assert funnel["risk"] == {"passed": 0, "rejected": 0, "unknown": 0}
    assert funnel["conditions"] == {
        "read": {
            "来源": "奖励目录与市场资料",
            "完整性": "完整目录；部分结果可参与筛选；缺失资料=UNKNOWN",
        },
        "filter": {
            "奖励": "奖励启用且日奖池>0",
            "市场": "接受订单",
            "参与": "没有已知订单或持仓",
            "窗口": "24h",
            "粒度": "1m",
            "振幅": "不超过1¢",
            "刷新": "每小时",
            "有效期": "2h",
            "缺失": "UNKNOWN",
        },
        "risk": {
            "奖励与市场资料": "60s内",
            "盘口与账户": "10s内；订单与持仓资料完整",
            "事件": "开始前30分钟、进行中、结束后1h冷却；结束后筛选必须通过；缺失=UNKNOWN",
            "入场压力": "最小数量、奖励价带、资金预留、含费压力退出不超过10%",
            "排序": "日奖池降序，同额按市场ID升序",
            "上限": 50,
        },
    }
    assert funnel["reasons"] == {
        "catalog": [],
        "base": [],
        "volatility": [],
        "selected": [],
        "risk": [],
    }
    assert exchange.catalog_calls == 1
    assert exchange.unexpected_calls == []


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
                        {"t": int(first_now.timestamp()), "p": Decimal("0.500")},
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

    now[0] = first_now + timedelta(hours=1)
    second = service.refresh_price_history()
    assert second["state"] == "known"
    assert len(exchange.history_calls) == 2
    second_call = exchange.history_calls[1]
    assert second_call["start_ts"] == int(first_now.timestamp()) - 60
    samples = store.lp_price_history_samples("condition-M1", "token-M1")
    assert [Decimal(str(row["p"])) for row in samples] == [
        Decimal("0.500"),
        Decimal("0.500"),
        Decimal("0.505"),
    ]
    second_summary = store.lp_price_history_summary("condition-M1", "token-M1", now=now[0])
    assert second_summary is not None
    assert Decimal(str(second_summary["amplitude"])) == Decimal("0.005")
    assert str(second_summary["checked_at"]).startswith(
        now[0].isoformat().replace("+00:00", "")
    )

    exchange.fail_history = True
    now[0] = first_now + timedelta(hours=2)
    failed = service.refresh_price_history()
    assert failed["state"] == "unknown"
    assert len(exchange.history_calls) == 3
    preserved = store.lp_price_history_summary("condition-M1", "token-M1", now=now[0])
    assert preserved is not None
    assert str(preserved["checked_at"]).startswith(
        (first_now + timedelta(hours=1)).isoformat().replace("+00:00", "")
    )
    assert Decimal(str(preserved["amplitude"])) == Decimal("0.005")

    now[0] = first_now + timedelta(hours=2, minutes=59, seconds=59)
    before_candidates = len(exchange.history_calls)
    usable = service.refresh_candidates(force=True)
    assert len(exchange.history_calls) == before_candidates
    assert usable["funnel"]["volatility_pass"] == 1

    now[0] = first_now + timedelta(hours=3)
    expired = service.refresh_candidates(force=True)
    assert len(exchange.history_calls) == before_candidates
    assert expired["funnel"]["volatility_pass"] == 0


def test_lp_share_watch_selection_is_durable_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Account:
        def __init__(self, wallet: str) -> None:
            self.wallet_address = wallet
            self.config = SimpleNamespace(wallet_address=wallet)
            self.order_writes = 0
            self.fail_open_order_reads = False
            self.omit_open_orders_checked_at = False
            self.open_orders_checked_at: datetime | None = None
            self.open_orders_wallet: str | None = None
            self.block_account_snapshot = False
            self.account_snapshot_started = threading.Event()
            self.release_account_snapshot = threading.Event()
            self.fail_account_snapshot = False
            self.open_orders = [
                {
                    "order_id": "a-1",
                    "condition_id": "condition-a",
                    "token_id": "a-yes",
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.40"),
                    "original_size": Decimal("20"),
                    "size_matched": Decimal("5"),
                },
                {
                    "order_id": "a-2",
                    "condition_id": "condition-a",
                    "token_id": "a-no",
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
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.50"),
                    "original_size": Decimal("12"),
                    "size_matched": Decimal("2"),
                },
            ]

        def lp_open_orders_snapshot(self) -> dict[str, object]:
            if self.fail_open_order_reads:
                raise RuntimeError("account read unavailable")
            result: dict[str, object] = {
                "authenticated": True,
                "open_orders": tuple(self.open_orders),
                "open_orders_complete": True,
            }
            if not self.omit_open_orders_checked_at:
                result["checked_at"] = self.open_orders_checked_at or datetime.now(UTC)
            if self.open_orders_wallet is not None:
                result["wallet_address"] = self.open_orders_wallet
            return result

        def lp_account_snapshot(self) -> dict[str, object]:
            if self.block_account_snapshot:
                self.account_snapshot_started.set()
                self.release_account_snapshot.wait(timeout=5)
                if self.fail_account_snapshot:
                    raise RuntimeError("account snapshot failed")
            if self.fail_account_snapshot:
                raise RuntimeError("account snapshot failed")
            return {
                "authenticated": not self.fail_open_order_reads,
                "open_orders": tuple(self.open_orders),
                "open_orders_complete": not self.fail_open_order_reads,
                "positions": (),
                "positions_complete": not self.fail_open_order_reads,
                "checked_at": datetime.now(UTC),
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

    store = PredictionArbitrageStore(tmp_path / "data")
    account = Account("wallet-a")
    monitor = make_monitor(tmp_path)
    service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
    )
    service._breaker_open = False

    class Runtime:
        mode = "production"
        state = "RUNNING"
        production_owner = True
        execution = service

    Runtime.store = store

    with _server(
        Runtime(),
        session_token="session-token",
        csrf_token="csrf-token",
    ) as base:
        def post(payload: dict[str, object], *, csrf: str = "csrf-token") -> tuple[int, dict[str, object]]:
            request = Request(
                base + "/api/prediction-arbitrage/lp/share-watch",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Cookie": "ot_prediction_session=session-token",
                    "Origin": base,
                    "X-CSRF-Token": csrf,
                },
                method="POST",
            )
            return _response(request)

        status, payload = post({"condition_id": "condition-a", "enabled": True})
        assert status == 200, payload
        assert payload["enabled"] is True
        assert payload["condition_id"] == "condition-a"
        dashboard_status, dashboard_payload = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert dashboard_status == 200
        assert dashboard_payload["lp_share_watch_state"]["condition-a"]["enabled"] is True

        fixed_now = datetime.now(UTC).replace(microsecond=0)
        original_utc_now = prediction_execution_module._utc_now
        monkeypatch.setattr(prediction_execution_module, "_utc_now", lambda: fixed_now)
        account.open_orders_checked_at = fixed_now - timedelta(seconds=29.999)
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 200
        assert post({"condition_id": "condition-b", "enabled": False})[0] == 200

        account.open_orders_checked_at = fixed_now - timedelta(seconds=30)
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 400
        assert service.lp_share_watch_state()["condition-b"]["enabled"] is False

        account.omit_open_orders_checked_at = True
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 400
        assert service.lp_share_watch_state()["condition-b"]["enabled"] is False
        account.omit_open_orders_checked_at = False

        account.open_orders_checked_at = fixed_now + timedelta(seconds=1)
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 400
        assert service.lp_share_watch_state()["condition-b"]["enabled"] is False

        account.open_orders_checked_at = fixed_now - timedelta(seconds=1)
        account.open_orders_wallet = "wallet-other"
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 400
        assert service.lp_share_watch_state()["condition-b"]["enabled"] is False
        account.open_orders_wallet = None
        monkeypatch.setattr(prediction_execution_module, "_utc_now", original_utc_now)

        account.fail_account_snapshot = True
        off_status, off_payload = post({"condition_id": "condition-a", "enabled": False})
        assert off_status == 200, off_payload
        assert off_payload["enabled"] is False
        failed_dashboard_status, failed_dashboard = _response(
            base + "/api/prediction-arbitrage/lp/dashboard"
        )
        assert failed_dashboard_status == 200
        assert failed_dashboard["state"] == "stale"
        assert failed_dashboard["lp_share_watch_state"]["condition-a"]["enabled"] is False
        account.fail_account_snapshot = False
        assert post({"condition_id": "condition-a", "enabled": True})[0] == 200
        before_b = service.lp_share_watch_state()
        assert "condition-b" not in before_b or before_b["condition-b"].get("enabled") is not True
        account_id = service._lp_account_id()
        assert account_id is not None
        store.update_lp_observation(
            account_id,
            "condition-a",
            {
                "share_alert": {
                    "breach_started_at": "2026-09-17T00:00:00+00:00",
                    "notification_pending": True,
                }
            },
        )
        repeated_status, repeated = post({"condition_id": "condition-a", "enabled": True})
        assert repeated_status == 200
        assert repeated["share_alert"]["breach_started_at"] == "2026-09-17T00:00:00+00:00"
        assert repeated["share_alert"]["notification_pending"] is True
        assert post({"condition_id": "condition-b", "enabled": True})[0] == 200
        restarted_while_enabled = PredictionExecutionService(
            store=store,
            monitor=monitor,
            trading=Account("wallet-a"),
            notifier=NullNotifier(),
            lock_path=tmp_path / "execution-restarted-enabled.lock",
        )
        enabled_after_restart = restarted_while_enabled.lp_share_watch_state()
        assert enabled_after_restart["condition-a"]["enabled"] is True
        assert enabled_after_restart["condition-b"]["enabled"] is True
        assert post({"condition_id": "condition-b", "enabled": False})[0] == 200
        assert post({"condition_id": "condition-a", "enabled": "true"})[0] == 400
        assert post({"condition_id": "condition-a", "enabled": True}, csrf="")[0] == 403
        assert post({"condition_id": "condition-a", "enabled": True, "account": "other"})[0] == 400
        assert post({"condition_id": "", "enabled": True})[0] == 400
        assert post({"condition_id": "condition-unknown", "enabled": True})[0] == 400

        account.fail_open_order_reads = True
        disabled_status, disabled_payload = post({"condition_id": "condition-a", "enabled": False})
        assert disabled_status == 200, disabled_payload
        assert disabled_payload["enabled"] is False
        account.fail_open_order_reads = False
        account.open_orders = []
        assert post({"condition_id": "condition-a", "enabled": True})[0] == 400

        account.open_orders = [
            {
                "order_id": "a-3",
                "condition_id": "condition-a",
                "token_id": "a-yes",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.40"),
                "original_size": Decimal("20"),
                "size_matched": Decimal("5"),
            }
        ]
        store.save_lp_observation(
            account_id,
            "condition-a",
            {
                "state": "known",
                "risk_alerts": {"YES": {"active": True}},
                "share_alert": {"enabled": False},
            },
        )
        account.block_account_snapshot = True
        account.fail_account_snapshot = True
        refresh_result: dict[str, object] = {}

        def refresh() -> None:
            refresh_result.update(service.refresh_lp_observations())

        refresh_thread = threading.Thread(target=refresh)
        refresh_thread.start()
        assert account.account_snapshot_started.wait(timeout=5)
        selected_status, selected_payload = post({"condition_id": "condition-a", "enabled": True})
        assert selected_status == 200, selected_payload
        assert selected_payload["enabled"] is True
        account.release_account_snapshot.set()
        refresh_thread.join(timeout=5)
        assert not refresh_thread.is_alive()
        assert refresh_result["state"] == "unknown"
        interleaved = store.lp_observations(account_id)["condition-a"]
        assert interleaved["share_alert"]["enabled"] is True
        assert interleaved["risk_alerts"]["YES"]["active"] is True

    account.fail_open_order_reads = True
    disabled_during_read_failure = service.set_lp_share_watch("condition-a", enabled=False)
    assert disabled_during_read_failure["enabled"] is False
    account.fail_open_order_reads = False
    assert account.order_writes == 0

    restarted = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=Account("wallet-a"),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-restarted.lock",
    )
    assert restarted.lp_share_watch_state()["condition-a"]["enabled"] is False
    other = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=Account("wallet-b"),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-other.lock",
    )
    assert other.lp_share_watch_state() == {}

    class FallbackAccount:
        def __init__(self, checked_at: datetime) -> None:
            self.wallet_address = "wallet-fallback"
            self.config = SimpleNamespace(wallet_address=self.wallet_address)
            self.checked_at = checked_at
            self.snapshot_calls = 0
            self.order_writes = 0

        def lp_account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {
                "authenticated": True,
                "wallet_address": "wallet-other",
                "checked_at": self.checked_at,
                "open_orders": (
                    {
                        "order_id": "fallback-order",
                        "condition_id": "condition-fallback",
                        "token_id": "fallback-yes",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.40"),
                        "original_size": Decimal("20"),
                        "size_matched": Decimal("5"),
                    },
                ),
                "open_orders_complete": True,
                "positions": (),
                "positions_complete": True,
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

    fallback_now = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(prediction_execution_module, "_utc_now", lambda: fallback_now)
    fallback_account = FallbackAccount(fallback_now)
    assert not callable(getattr(fallback_account, "lp_open_orders_snapshot", None))
    fallback_service = PredictionExecutionService(
        store=store,
        monitor=monitor,
        trading=fallback_account,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution-fallback.lock",
    )
    fallback_service._breaker_open = False

    class FallbackRuntime:
        mode = "production"
        state = "RUNNING"
        production_owner = True
        execution = fallback_service

    FallbackRuntime.store = store

    with _server(
        FallbackRuntime(),
        session_token="fallback-session",
        csrf_token="fallback-csrf",
    ) as fallback_base:
        fallback_request = Request(
            fallback_base + "/api/prediction-arbitrage/lp/share-watch",
            data=json.dumps(
                {"condition_id": "condition-fallback", "enabled": True}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": "ot_prediction_session=fallback-session",
                "Origin": fallback_base,
                "X-CSRF-Token": "fallback-csrf",
            },
            method="POST",
        )
        fallback_status, fallback_payload = _response(fallback_request)

    assert fallback_status == 400, fallback_payload
    assert fallback_payload["reason"] == "account_facts_unknown"
    assert fallback_account.snapshot_calls == 1
    assert fallback_service.lp_share_watch_state() == {}
    assert fallback_account.order_writes == 0
