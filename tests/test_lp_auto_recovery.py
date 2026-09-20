from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import Message
from io import BytesIO
import json
import multiprocessing
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import httpx
import pytest
from polymarket import PRODUCTION, PublicClient

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.notifications import FeishuWebhookNotifier


def _hold_preparation_lock_then_exit(path: str, ready: object) -> None:
    import fcntl

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()  # type: ignore[attr-defined]
        time.sleep(0.2)


def test_transient_outage_recovers_after_more_than_two_failures(tmp_path) -> None:
    """Transient catalog failures retain a durable, bounded recovery schedule."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    catalog_calls: list[datetime] = []

    class Exchange:
        def lp_preparation_probe(
            self, *, stop_event: object = None
        ) -> dict[str, object]:
            """The test boundary explicitly reports an unsupported cheap probe."""

            del stop_event
            return {"state": "unsupported", "complete": False}

        def lp_reward_catalog(
            self, *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            catalog_calls.append(clock[0])
            if len(catalog_calls) <= 5:
                raise TimeoutError("upstream catalog temporarily unavailable")
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": "condition-recovered",
                        "daily_pool_usd": Decimal("120"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                condition_id: {
                    "market_id": "market-recovered",
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": "token-recovered",
                        }
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
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
    retry_delays = (300, 600, 1200, 1800, 1800)

    for failure_index, delay in enumerate(retry_delays):
        failed = service.refresh_price_history()
        assert failed["preparation_outcome"] == "failure"
        preparation = service.preparation_snapshot()
        assert preparation["state"] == "waiting_retry"
        assert preparation["paused"] is False
        expected_retry_at = clock[0] + timedelta(seconds=delay)
        assert preparation["next_retry_at"] == expected_retry_at.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        persisted = store.lp_preparation()
        assert persisted is not None
        assert persisted["next_retry_at"] == preparation["next_retry_at"]

        candidate = service.candidate_snapshot()
        assert candidate["state"] == "unknown"
        assert candidate["stale"] is True

        clock[0] = expected_retry_at - timedelta(seconds=1)
        waiting = service.refresh_price_history()
        assert waiting["preparation_outcome"] == "waiting_retry"
        assert len(catalog_calls) == failure_index + 1

        clock[0] = expected_retry_at

    recovered = service.refresh_price_history()
    assert recovered["preparation_outcome"] == "success"
    assert recovered["state"] == "known"
    assert len(catalog_calls) == 6
    preparation = service.preparation_snapshot()
    assert preparation["state"] == "ready"
    assert preparation["paused"] is False
    assert preparation["failure_count"] == 0
    assert preparation["last_success_at"] == clock[0].isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    persisted = store.lp_preparation()
    assert persisted is not None
    assert persisted["state"] == "ready"
    assert persisted["last_success_at"] == preparation["last_success_at"]

    # A valid preparation result does not itself publish candidate eligibility;
    # the stale/unknown candidate projection remains safe until a scan validates it.
    candidate = service.candidate_snapshot()
    assert candidate["state"] == "unknown"
    assert candidate["stale"] is True


@pytest.mark.parametrize(
    ("retry_after_header", "expected_delay"),
    [
        ("120", 120),
        ("90000", 90000),
        ("Sat, 20 Sep 2026 05:25:00 GMT", 1440),
    ],
)
def test_dependency_probe_is_bounded_and_respects_retry_after(
    tmp_path, retry_after_header: str, expected_delay: int
) -> None:
    """A bounded dependency probe can wake one recovery pass without a scan loop."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    probe_requests: list[tuple[datetime, dict[str, str]]] = []
    case_store = tmp_path / f"retry-after-{expected_delay}"
    case_store.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rewards/markets/current"
        params = dict(request.url.params.multi_items())
        probe_requests.append((clock[0], params))
        # A successful first page advertises another page. The preparation
        # probe must not follow it, because doing so would turn a cheap health
        # check into the complete catalog walk.
        if params.get("next_cursor") == "probe-sentinel":
            raise AssertionError("bounded preparation probe traversed another page")
        if len(probe_requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after_header},
                json={"error": "temporarily rate limited"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"data": [], "next_cursor": "probe-sentinel"},
            request=request,
        )

    def public_factory() -> PublicClient:
        public = PublicClient(PRODUCTION)
        public._ctx.clob._client = httpx.Client(  # type: ignore[attr-defined]
            base_url=PRODUCTION.clob_url,
            transport=httpx.MockTransport(handler),
        )
        return public

    trading = PolymarketTradingClient(
        TradingConfig(
            "0x" + "1" * 40,
            "0x" + "2" * 40,
        ),
        client=object(),
        public_client_factory=public_factory,
    )

    class Exchange:
        def __init__(self) -> None:
            self.full_catalog_calls = 0

        def lp_reward_catalog(
            self, *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            self.full_catalog_calls += 1
            return {
                "state": "unknown",
                "complete": False,
                "checked_at": clock[0],
                "markets": (),
                "error_type": "TimeoutError",
            }

        def lp_preparation_probe(
            self, *, stop_event: object = None
        ) -> dict[str, object]:
            return trading.lp_preparation_probe(stop_event=stop_event)

    exchange = Exchange()
    service = PolymarketLPService(
        PredictionArbitrageStore(case_store), exchange, clock=lambda: clock[0]
    )

    initial = service.refresh_price_history()
    assert initial["preparation_outcome"] == "failure"
    assert exchange.full_catalog_calls == 1
    assert probe_requests == []

    clock[0] += timedelta(seconds=59)
    before_probe = service.refresh_price_history()
    assert before_probe["preparation_outcome"] == "waiting_retry"
    assert probe_requests == []
    assert exchange.full_catalog_calls == 1

    clock[0] += timedelta(seconds=1)
    rate_limited = service.refresh_price_history()
    assert rate_limited["preparation_outcome"] == "waiting_retry"
    assert len(probe_requests) == 1
    assert exchange.full_catalog_calls == 1
    assert rate_limited["preparation"]["next_probe_at"] == (
        clock[0] + timedelta(seconds=expected_delay)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert rate_limited["preparation"]["retry_after_seconds"] == expected_delay

    clock[0] += timedelta(seconds=expected_delay - 1)
    before_retry_after = service.refresh_price_history()
    assert before_retry_after["preparation_outcome"] == "waiting_retry"
    assert len(probe_requests) == 1
    assert exchange.full_catalog_calls == 1

    clock[0] += timedelta(seconds=1)
    recovered_probe = service.refresh_price_history()
    assert recovered_probe["preparation_outcome"] == "failure"
    assert len(probe_requests) == 2
    assert exchange.full_catalog_calls == 2

    clock[0] += timedelta(seconds=60)
    positive_probe_only = service.refresh_price_history()
    assert positive_probe_only["preparation_outcome"] == "waiting_retry"
    assert len(probe_requests) == 3
    # A healthy probe does not grant a second full read on every 60-second tick
    # while the full catalog remains unavailable.
    assert exchange.full_catalog_calls == 2


@pytest.mark.parametrize(
    ("label", "catalog_error", "expected_paused", "expected_chain", "status"),
    [
        ("timeout", {"error_type": "TimeoutError"}, False, (), None),
        ("http503", {"error_type": "UnexpectedResponseError", "status": 503}, False, (), 503),
        ("http429", {"error_type": "RateLimitError", "status": 429}, False, (), 429),
        ("connection_reset", {"error_type": "ConnectionResetError"}, False, (), None),
        (
            "wrapped_certificate",
            {
                "error_type": "TransportError",
                "error_chain": ["TransportError", "SSLCertVerificationError"],
            },
            True,
            ("TransportError", "SSLCertVerificationError"),
            None,
        ),
        ("http401", {"error_type": "RequestRejectedError", "status": 401}, True, (), 401),
        ("http403", {"error_type": "RequestRejectedError", "status": 403}, True, (), 403),
        ("bad_config", {"error_type": "ConfigurationError"}, True, (), None),
    ],
)
def test_error_classification_preserves_operator_blockers(
    tmp_path,
    label: str,
    catalog_error: dict[str, object],
    expected_paused: bool,
    expected_chain: tuple[str, ...],
    status: int | None,
) -> None:
    """Classify safe transport facts without exposing exception details."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "unknown",
                "complete": False,
                "checked_at": clock[0],
                "markets": (),
                **catalog_error,
                "secret_url": "https://private.invalid/?token=secret-token",
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del condition_ids, stop_event
            return {}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            return {"state": "unknown", "history": {}, "unknown_token_ids": token_ids}

    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / label), Exchange(), clock=lambda: clock[0]
    )
    result = service.refresh_price_history()
    preparation = result["preparation"]
    assert result["preparation_outcome"] == "failure"
    assert preparation["paused"] is expected_paused
    assert (preparation["next_retry_at"] is None) is expected_paused
    if expected_chain:
        assert tuple(preparation["last_error_chain"]) == expected_chain
    if status is not None:
        assert preparation["last_error_status"] == status
    assert "secret-token" not in repr(preparation)

    if label in {"http503", "wrapped_certificate"}:
        def boundary_handler(request: httpx.Request) -> httpx.Response:
            if label == "http503":
                return httpx.Response(
                    503,
                    headers={"Retry-After": "90000"},
                    json={"error": "secret boundary text"},
                    request=request,
                )
            connection_error = httpx.ConnectError(
                "https://private.invalid/?token=secret-token", request=request
            )
            connection_error.__cause__ = ssl.SSLCertVerificationError(
                "certificate secret-token"
            )
            raise connection_error

        def boundary_public_factory() -> PublicClient:
            public = PublicClient(PRODUCTION)
            public._ctx.clob._client = httpx.Client(  # type: ignore[attr-defined]
                base_url=PRODUCTION.clob_url,
                transport=httpx.MockTransport(boundary_handler),
            )
            return public

        boundary_client = PolymarketTradingClient(
            TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
            client=object(),
            public_client_factory=boundary_public_factory,
        )
        boundary = boundary_client.lp_preparation_probe()
        assert "secret-token" not in repr(boundary)
        if label == "http503":
            assert boundary["status"] == 503
            assert boundary["retry_after_seconds"] == 90000
        else:
            assert boundary["error_chain"] == (
                "TransportError",
                "ConnectError",
                "SSLCertVerificationError",
            )

    if label == "timeout":
        class MarketInvalidExchange:
            def lp_reward_catalog(
                self, *, stop_event: object = None
            ) -> dict[str, object]:
                del stop_event
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": clock[0],
                    "markets": [
                        {
                            "condition_id": "market-invalid",
                            "daily_pool_usd": Decimal("20"),
                            "reward_active": True,
                        }
                    ],
                }

            def lp_market_metadata_batch(
                self,
                condition_ids: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, object]:
                del stop_event
                return {
                    "state": "partial",
                    "markets": {},
                    "confirmed_absent_ids": (),
                    "failed_ids": {
                        condition_id: "ValueError" for condition_id in condition_ids
                    },
                    "deferred_ids": (),
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
                del start_ts, end_ts, fidelity, stop_event
                return {"state": "unknown", "history": {}, "unknown_token_ids": token_ids}

        invalid_store = PredictionArbitrageStore(tmp_path / "market-invalid")
        invalid_service = PolymarketLPService(
            invalid_store, MarketInvalidExchange(), clock=lambda: clock[0]
        )
        invalid_result = invalid_service.refresh_price_history()
        assert invalid_result["preparation_outcome"] in {"failure", "waiting_retry"}
        invalid_preparation = invalid_service.preparation_snapshot()
        assert invalid_preparation["paused"] is False
        assert invalid_preparation["state"] == "partial"
        invalid_items = invalid_store.lp_preparation_items()
        assert invalid_items[0]["condition_id"] == "market-invalid"
        assert invalid_items[0]["state"] == "waiting_retry"


def test_metadata_probe_captures_retry_after_from_gamma_transport() -> None:
    """Metadata probe facts come from the Gamma client that made the request."""

    requests: list[tuple[str, str]] = []

    def clob_handler(request: httpx.Request) -> httpx.Response:
        requests.append(("clob", request.url.path))
        return httpx.Response(200, json={"data": []}, request=request)

    def gamma_handler(request: httpx.Request) -> httpx.Response:
        requests.append(("gamma", request.url.path))
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            json={"error": "rate limited"},
            request=request,
        )

    def public_factory() -> PublicClient:
        public = PublicClient(PRODUCTION)
        public._ctx.clob._client = httpx.Client(  # type: ignore[attr-defined]
            base_url=PRODUCTION.clob_url,
            transport=httpx.MockTransport(clob_handler),
        )
        public._ctx.gamma._client = httpx.Client(  # type: ignore[attr-defined]
            base_url=PRODUCTION.gamma_url,
            transport=httpx.MockTransport(gamma_handler),
        )
        return public

    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        public_client_factory=public_factory,
    )

    probe = adapter.lp_preparation_probe(
        stage="metadata", condition_ids=("condition-gamma",)
    )

    assert requests == [("gamma", "/markets/keyset")]
    assert probe["status"] == 429
    assert probe["retry_after_seconds"] == 120


def test_history_probe_uses_cached_tokens_and_preserves_http_retry_facts() -> None:
    """History probes map condition ids through cached metadata before reading CLOB."""

    condition_id = "condition-history"
    token_ids = ("token-history-yes", "token-history-no")
    requested_markets: list[tuple[str, ...]] = []
    metadata = {
        "condition_id": condition_id,
        "accepting_orders": True,
        "outcomes": {
            "yes": {"token_id": token_ids[0]},
            "no": {"token_id": token_ids[1]},
        },
    }

    class MetadataCache:
        def lp_metadata_cache_entries(self, *, now: datetime) -> dict[str, object]:
            return {condition_id: (now.timestamp() + 3600, metadata)}

    def forbidden_public_factory() -> object:
        raise AssertionError("history probe must use cached token metadata")

    def open_history(request: object, **_: object) -> object:
        body = json.loads(getattr(request, "data").decode("utf-8"))
        requested_markets.append(tuple(body["markets"]))
        assert tuple(body["markets"]) == token_ids
        headers = Message()
        headers["Retry-After"] = "90000"
        raise HTTPError(
            "https://clob.polymarket.com/batch-prices-history",
            429,
            "rate limited",
            headers,
            BytesIO(b'{"error":"rate limited"}'),
        )

    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        urlopen_fn=open_history,
        public_client_factory=forbidden_public_factory,
        metadata_cache=MetadataCache(),
    )

    probe = adapter.lp_preparation_probe(
        stage="history", condition_ids=(condition_id,)
    )
    result = adapter.lp_price_history(
        token_ids, start_ts=1_700_000_000, end_ts=1_700_000_060, fidelity=1
    )

    assert requested_markets == [token_ids, token_ids]
    assert probe["state"] == "unknown"
    assert result["status"] == 429
    assert result["retry_after_seconds"] == 90000


def test_history_read_preserves_wrapped_certificate_chain() -> None:
    """The urllib history boundary keeps a safe wrapped certificate chain."""

    def open_history(_request: object, **_: object) -> object:
        wrapped = URLError("https://private.invalid/?token=secret-token")
        wrapped.__cause__ = ssl.SSLCertVerificationError("certificate secret-token")
        raise wrapped

    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        urlopen_fn=open_history,
    )

    result = adapter.lp_price_history(
        ("token-history",), start_ts=1_700_000_000, end_ts=1_700_000_060, fidelity=1
    )

    assert result["error_chain"] == ("URLError", "SSLCertVerificationError")
    assert "secret-token" not in repr(result)


def test_market_retries_are_isolated_and_prioritized(tmp_path) -> None:
    """A failed market is probed in isolation while priority inputs lead recovery."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    metadata_requests: list[tuple[str, ...]] = []
    history_requests: list[tuple[str, ...]] = []
    probe_stages: list[str | None] = []
    history_healthy = [False]
    condition_ids = ("background-z", "background-y", "watched-b", "position-a")

    def market(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "daily_pool_usd": Decimal("20"),
            "reward_active": True,
        }

    def metadata(condition_id: str) -> dict[str, object]:
        return {
            "market_id": f"market-{condition_id}",
            "condition_id": condition_id,
            "accepting_orders": True,
            "outcomes": {
                "yes": {
                    "label": "YES",
                    "token_id": f"token-{condition_id}",
                }
            },
        }

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                # Deliberately put background markets first and priority IDs
                # last; the public metadata boundary should still receive A/B
                # first from cached/session priority state.
                "markets": [market(condition_id) for condition_id in condition_ids],
            }

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event
            probe_stages.append(stage)
            assert condition_ids == ("position-a",)
            return {
                "state": "healthy" if history_healthy[0] else "unknown",
                "complete": history_healthy[0],
                "checked_at": clock[0],
                "stage": stage,
                "condition_ids": condition_ids,
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_requests.append(tuple(requested))
            return {
                "state": "known",
                "markets": {
                    condition_id: metadata(condition_id)
                    for condition_id in requested
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
                "deferred_ids": (),
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
            history_requests.append(tuple(token_ids))
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                    if token_id != "token-position-a" or history_healthy[0]
                },
                "unknown_token_ids": [
                    token_id
                    for token_id in token_ids
                    if token_id == "token-position-a" and not history_healthy[0]
                ],
                "errors": {
                    "token-position-a": "HistoryUnavailable"
                }
                if not history_healthy[0]
                else {},
            }

    store = PredictionArbitrageStore(tmp_path)
    # Existing screening state marks B as watched.  The position marker on A
    # is a cached account/session fact; preparation must consume it without a
    # new account request.
    store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "scan_started_at": "2026-09-20T04:00:00Z",
            "selected_results": [
                {"condition_id": "watched-b"},
                {"condition_id": "position-a", "position_size": "1"},
            ],
            "candidates": [],
            "recommendations": [],
            "funnel": {},
            "selected_market_ids": [],
        }
    )
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] in {"failure", "success"}
    assert metadata_requests[0][:2] == ("position-a", "watched-b")
    assert history_requests == [("token-position-a", "token-watched-b", "token-background-z", "token-background-y")]
    items = store.lp_preparation_items()
    assert [item["condition_id"] for item in items] == ["position-a"]
    assert first["preparation"]["state"] == "partial"

    # The rewards endpoint remains healthy, but the failed history dependency
    # is probed on its own every bounded interval; no full catalog/metadata or
    # history request is made while that dependency is still unavailable.
    clock[0] += timedelta(seconds=60)
    waiting = service.refresh_price_history()
    assert waiting["preparation_outcome"] == "waiting_retry"
    assert probe_stages == ["history"]
    assert len(metadata_requests) == 1
    assert len(history_requests) == 1

    history_healthy[0] = True
    clock[0] += timedelta(seconds=60)
    recovered = service.refresh_price_history()
    assert recovered["preparation_outcome"] in {"success", "waiting_retry"}
    assert probe_stages[-1] == "history"
    assert len(metadata_requests) == 2
    assert history_requests[-1] == ("token-position-a",)
    assert history_requests.count(("token-position-a",)) == 1
    assert all("token-watched-b" not in batch for batch in history_requests[1:])
    assert all("token-background-z" not in batch for batch in history_requests[1:])
    assert store.lp_preparation_items() == []


def test_restart_and_old_results_preserve_recovery_fences(tmp_path) -> None:
    """Restart keeps deadlines, claims are exclusive, and old generations lose."""

    clock = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)
    store_path = tmp_path / "restart-fences"
    store = PredictionArbitrageStore(store_path)
    store.lp_record_preparation_failure(
        "market-fenced",
        generation=1,
        stage="history",
        error="TimeoutError",
        failed_at=clock,
        token_id="token-fenced",
    )
    deadline = clock + timedelta(seconds=300)
    store.lp_save_preparation(
        {
            "state": "partial",
            "stage": "history",
            "generation": 1,
            "paused": False,
            "next_retry_at": deadline.isoformat().replace("+00:00", "Z"),
        }
    )

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {"state": "unknown", "complete": False, "markets": ()}

    restarted = PredictionArbitrageStore(store_path)
    service = PolymarketLPService(restarted, Exchange(), clock=lambda: clock)
    assert service.preparation_snapshot()["next_retry_at"] == deadline.isoformat().replace(
        "+00:00", "Z"
    )
    assert restarted.lp_claim_preparation_retries(
        now=clock + timedelta(seconds=299), condition_ids=("market-fenced",)
    ) == []

    claimed = restarted.lp_claim_preparation_retries(
        now=deadline, condition_ids=("market-fenced",)
    )
    assert len(claimed) == 1
    assert claimed[0]["state"] == "retrying"
    # A second live worker cannot claim the same retry lease.
    assert restarted.lp_claim_preparation_retries(
        now=deadline, condition_ids=("market-fenced",)
    ) == []

    # The restarted owner is gone.  Its exclusive claim is requeued for one
    # bounded retry instead of becoming a permanent manual pause.
    assert restarted.lp_normalize_interrupted_preparation_items() == 1
    requeued = restarted.lp_preparation_items()[0]
    assert requeued["state"] == "waiting_retry"
    assert requeued["paused"] is False
    requeued_at = datetime.fromisoformat(
        str(requeued["next_retry_at"]).replace("Z", "+00:00")
    )
    assert restarted.lp_claim_preparation_retries(
        now=requeued_at, condition_ids=("market-fenced",)
    )

    # A newer recovered generation fences an old worker's cache publication.
    restarted.lp_clear_preparation_items(("market-fenced",), generation=2)
    newer = {
        "state": "known",
        "checked_at": deadline,
        "valid_until": deadline + timedelta(hours=24),
        "preparation_generation": 2,
    }
    older = {**newer, "state": "unknown", "preparation_generation": 1}
    restarted.lp_save_price_history(
        "market-fenced", "token-fenced", [{"t": 1, "p": "0.40"}], newer
    )
    assert (
        restarted.lp_save_price_history_batch(
            [
                {
                    "condition_id": "market-fenced",
                    "token_id": "token-fenced",
                    "samples": [{"t": 2, "p": "0.41"}],
                    "summary": older,
                }
            ],
            generation=1,
        )
        == 0
    )
    persisted = restarted.lp_price_history_summary(
        "market-fenced", "token-fenced", now=deadline
    )
    assert persisted is not None
    assert persisted["preparation_generation"] == 2
    assert persisted["state"] == "known"


def test_legacy_pauses_migrate_once_without_fabricating_success(tmp_path) -> None:
    """Legacy global pauses become bounded market retries with TLS caution."""

    base_time = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)
    legacy_errors = (
        "globalTransportError",
        "IncompleteRead",
        "retry_interrupted",
        "genericSSLError",
        "SSLCertVerificationError",
    )

    for legacy_error in legacy_errors:
        current = [base_time + timedelta(hours=1)]
        store_path = tmp_path / legacy_error
        store = PredictionArbitrageStore(store_path)
        store.lp_save_price_history(
            "legacy-market",
            "legacy-token",
            [],
            {
                "state": "unknown",
                "checked_at": None,
                "last_attempt_at": base_time,
                "last_error": legacy_error,
            },
        )
        store.lp_save_price_history(
            "cached-market",
            "cached-token",
            [{"t": int(base_time.timestamp()), "p": "0.50"}],
            {
                "state": "known",
                "checked_at": base_time,
                "valid_until": base_time + timedelta(hours=24),
                "preparation_generation": 3,
            },
        )
        store.lp_save_preparation(
            {
                "state": "paused",
                "stage": "history",
                "generation": 3,
                "attempt": 2,
                "failure_count": 2,
                "paused": True,
                "alert_attempted": True,
                "alert_state": "sent",
                "last_attempt_at": base_time,
                "last_failure_at": base_time,
                "last_progress_at": base_time,
                "last_success_at": None,
                "next_retry_at": None,
                "last_error": legacy_error,
            }
        )
        history_calls: list[tuple[str, ...]] = []

        class Exchange:
            def lp_preparation_probe(
                self,
                *,
                stop_event: object = None,
                stage: str | None = None,
                condition_ids: tuple[str, ...] = (),
            ) -> dict[str, object]:
                del stop_event
                if legacy_error == "genericSSLError":
                    assert stage == "history"
                    assert condition_ids == ("legacy-market",)
                    return {"state": "healthy", "complete": True}
                return {"state": "unknown", "complete": False}

            def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
                del stop_event
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": current[0],
                    "markets": [
                        {
                            "condition_id": "legacy-market",
                            "daily_pool_usd": Decimal("20"),
                            "reward_active": True,
                        }
                    ],
                }

            def lp_market_metadata_batch(
                self,
                condition_ids: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, object]:
                del stop_event
                return {
                    "state": "known",
                    "markets": {
                        condition_id: {
                            "condition_id": condition_id,
                            "accepting_orders": True,
                            "outcomes": {
                                "yes": {"token_id": "legacy-token"}
                            },
                        }
                        for condition_id in condition_ids
                    },
                    "failed_ids": {},
                    "confirmed_absent_ids": (),
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
                del start_ts, end_ts, fidelity, stop_event
                history_calls.append(tuple(token_ids))
                return {
                    "state": "unknown",
                    "history": {},
                    "errors": {token_id: "history_missing" for token_id in token_ids},
                }

        first_service = PolymarketLPService(
            store, Exchange(), clock=lambda: current[0]
        )
        first = first_service.refresh_price_history()
        items = store.lp_preparation_items()
        assert len(items) == 1
        item = items[0]
        if legacy_error == "SSLCertVerificationError":
            assert item["paused"] is True
            assert item["state"] == "paused"
            assert history_calls == []
        else:
            assert item["paused"] is False
            assert item["state"] == "waiting_retry"
            assert item["next_retry_at"] is not None
            if legacy_error == "genericSSLError":
                # The normal TLS-verified read was attempted but did not
                # validate business history, so migration cannot fabricate a
                # known summary or clear the item.
                assert history_calls == [("legacy-token",)]
                assert first["preparation"]["state"] == "partial"

        deadline = item["next_retry_at"]
        reopened = PolymarketLPService(
            PredictionArbitrageStore(store_path), Exchange(), clock=lambda: current[0]
        )
        reopened.refresh_price_history()
        again = reopened.store.lp_preparation_items()
        assert len(again) == 1
        assert again[0]["next_retry_at"] == deadline
        assert reopened.store.lp_price_history_summary(
            "cached-market", "cached-token", now=current[0]
        )["state"] == "known"


def test_fault_notification_waits_five_minutes_and_retries_delivery(tmp_path) -> None:
    """Fault notices wait 300s, retry delivery, and deduplicate one episode."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    notifications: list[str] = []

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            raise TimeoutError("temporary catalog outage")

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event, stage, condition_ids
            return {"state": "unknown", "complete": False}

    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "incident"),
        Exchange(),
        clock=lambda: clock[0],
    )
    delivery_results = [
        {"code": 1, "msg": "temporary Feishu outage"},
        {"code": 0},
    ]
    delivered_titles: list[str] = []

    def post_json(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        del url, timeout
        delivered_titles.append(str(payload["content"]["text"]).split("\n", 1)[0])  # type: ignore[index]
        return delivery_results.pop(0)

    execution = PredictionExecutionService(
        store=service.store,
        monitor=object(),
        trading=object(),
        notifier=FeishuWebhookNotifier(
            webhook_url="https://feishu.invalid/webhook",
            post_json=post_json,
        ),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    initial = service.refresh_price_history()
    assert initial.get("alert_pending") is not True
    assert initial["preparation"]["fault_alert_state"] is None

    clock[0] += timedelta(seconds=299)
    before_deadline = service.refresh_price_history()
    assert before_deadline["preparation_outcome"] == "waiting_retry"
    assert before_deadline.get("alert_pending") is not True
    assert notifications == []

    clock[0] += timedelta(seconds=1)
    due = service.refresh_price_history()
    assert due.get("alert_pending") is True
    assert due["preparation"]["fault_alert_state"] == "claimed"
    failure_delivery = execution.notify_lp_preparation_failure(due["preparation"])
    assert failure_delivery["state"] == "failed"
    notifications.append("incident-1")
    failed_delivery = service.finish_preparation_alert(
        generation=int(due["preparation"]["generation"]), success=False
    )
    assert failed_delivery is not None
    assert failed_delivery["fault_alert_state"] == "failed"
    retry_deadline = datetime.fromisoformat(
        str(failed_delivery["fault_alert_next_at"]).replace("Z", "+00:00")
    )

    clock[0] = retry_deadline - timedelta(seconds=1)
    retry_wait = service.refresh_price_history()
    assert retry_wait.get("alert_pending") is not True
    clock[0] = retry_deadline
    retry_due = service.refresh_price_history()
    assert retry_due.get("alert_pending") is True
    assert len(notifications) == 1
    successful_delivery = execution.notify_lp_preparation_failure(
        retry_due["preparation"]
    )
    assert successful_delivery["state"] == "sent"
    assert len(delivered_titles) == 2
    delivered = service.finish_preparation_alert(
        generation=int(retry_due["preparation"]["generation"]), success=True
    )
    assert delivered is not None
    assert delivered["fault_alert_state"] == "sent"

    clock[0] += timedelta(seconds=60)
    after_delivery = service.refresh_price_history()
    assert after_delivery.get("alert_pending") is not True
    assert len(notifications) == 1

    short = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "short"),
        Exchange(),
        clock=lambda: clock[0],
    )
    short_result = short.refresh_price_history()
    assert short_result.get("alert_pending") is not True


def test_recovery_notification_requires_validated_business_recovery(tmp_path) -> None:
    """Only a complete affected-scope read after an acknowledged fault recovers."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    mode = ["failed"]
    probe_state = ["unknown"]
    probe_complete = [False]
    condition_ids = ("market-c", "market-d")
    history_requests: list[tuple[str, ...]] = []

    def catalog() -> dict[str, object]:
        return {
            "state": "known",
            "complete": True,
            "checked_at": clock[0],
            "markets": [
                {
                    "condition_id": condition_id,
                    "daily_pool_usd": Decimal("20"),
                    "reward_active": True,
                }
                for condition_id in condition_ids
            ],
        }

    def metadata(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "accepting_orders": True,
            "outcomes": {"yes": {"token_id": f"token-{condition_id}"}},
        }

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return catalog()

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event
            assert stage == "history"
            assert set(condition_ids) <= set(condition_ids_from_external)
            return {
                "state": probe_state[0],
                "complete": probe_complete[0],
                "checked_at": clock[0],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {condition_id: metadata(condition_id) for condition_id in requested},
                "failed_ids": {},
                "confirmed_absent_ids": (),
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
            history_requests.append(tuple(token_ids))
            if mode[0] == "failed":
                return {
                    "state": "unknown",
                    "history": {},
                    "errors": {token_id: "HistoryUnavailable" for token_id in token_ids},
                }
            if mode[0] == "stale":
                return {
                    "state": "known",
                    "history": {
                        "token-market-c": [
                            {"t": end_ts - 120, "p": "0.40"},
                            {"t": end_ts, "p": "0.41"},
                        ]
                    },
                    "errors": {"token-market-d": "HistoryUnavailable"},
                }
            if mode[0] == "mixed":
                return {
                    "state": "known",
                    "history": {
                        "token-market-c": [
                            {"t": start_ts, "p": "0.40"},
                            {"t": end_ts, "p": "0.41"},
                        ]
                    },
                    "errors": {"token-market-d": "HistoryUnavailable"},
                }
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

    condition_ids_from_external = condition_ids
    store = PredictionArbitrageStore(tmp_path / "cohort")
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
    first = service.refresh_price_history()
    assert first["preparation"]["state"] == "partial"
    assert first.get("recovery_alert_pending") is not True

    # Probe reachability with an incomplete business response is insufficient.
    clock[0] += timedelta(seconds=60)
    probe_state[0] = "healthy"
    probe_complete[0] = False
    incomplete = service.refresh_price_history()
    assert incomplete.get("recovery_alert_pending") is not True
    assert service.candidate_snapshot()["state"] == "unknown"

    # Acknowledge the delayed fault incident once it becomes due.
    clock[0] = datetime(2026, 9, 20, 5, 5, tzinfo=UTC)
    fault_due = service.refresh_price_history()
    assert fault_due.get("alert_pending") is True
    assert service.finish_preparation_alert(
        generation=int(fault_due["preparation"]["generation"]), success=True
    )["fault_alert_state"] == "sent"

    # A healthy probe followed by stale and mixed history cannot announce a
    # whole-scope recovery; C may progress while D remains failed.
    mode[0] = "stale"
    probe_complete[0] = True
    clock[0] += timedelta(seconds=60)
    stale = service.refresh_price_history()
    assert stale.get("recovery_alert_pending") is not True
    assert {item["condition_id"] for item in store.lp_preparation_items()} == set(condition_ids)

    probe_state[0] = "unknown"
    clock[0] += timedelta(seconds=60)
    assert service.refresh_price_history().get("recovery_alert_pending") is not True
    mode[0] = "mixed"
    probe_state[0] = "healthy"
    clock[0] += timedelta(seconds=60)
    mixed = service.refresh_price_history()
    assert mixed.get("recovery_alert_pending") is not True
    # The bounded probe rotates one affected market per tick.  Record that the
    # mixed tick really read D, then advance C to its own persisted retry
    # deadline instead of assuming the next healthy tick reads it.
    assert history_requests[-1] == ("token-market-d",)
    mixed_items = {
        str(item["condition_id"]): item for item in store.lp_preparation_items()
    }
    c_retry_at = datetime.fromisoformat(
        str(mixed_items["market-c"]["next_retry_at"]).replace("Z", "+00:00")
    )
    d_retry_at = datetime.fromisoformat(
        str(mixed_items["market-d"]["next_retry_at"]).replace("Z", "+00:00")
    )
    assert c_retry_at > clock[0]
    assert d_retry_at >= c_retry_at
    clock[0] = c_retry_at
    mixed_c_recovered = service.refresh_price_history()
    assert mixed_c_recovered.get("recovery_alert_pending") is not True
    assert mixed_c_recovered["updated_count"] == 1
    assert history_requests[-1] == ("token-market-c",)
    mixed_items = {
        str(item["condition_id"]): item for item in store.lp_preparation_items()
    }
    assert set(mixed_items) == {"market-d"}
    assert mixed_items["market-d"]["state"] == "waiting_retry"

    # Once the remaining affected market reaches its own dependency deadline,
    # a complete read makes exactly one recovery notice claimable.
    mode[0] = "healthy"
    mixed_items = {
        str(item["condition_id"]): item for item in store.lp_preparation_items()
    }
    d_retry_at = datetime.fromisoformat(
        str(mixed_items["market-d"]["next_retry_at"]).replace("Z", "+00:00")
    )
    assert d_retry_at >= c_retry_at
    clock[0] = d_retry_at
    recovered = service.refresh_price_history()
    assert history_requests[-1] == ("token-market-d",)
    assert recovered.get("recovery_alert_pending") is True
    failed_recovery = service.finish_preparation_recovery(
        generation=int(recovered["preparation"]["generation"]), success=False
    )
    assert failed_recovery["recovery_alert_state"] == "failed"
    recovery_retry_at = datetime.fromisoformat(
        str(failed_recovery["recovery_alert_next_at"]).replace("Z", "+00:00")
    )
    clock[0] = recovery_retry_at - timedelta(seconds=1)
    assert service.refresh_price_history().get("recovery_alert_pending") is not True
    clock[0] = recovery_retry_at
    recovery_retry = service.refresh_price_history()
    assert recovery_retry.get("recovery_alert_pending") is True
    assert service.finish_preparation_recovery(
        generation=int(recovery_retry["preparation"]["generation"]), success=True
    )["recovery_alert_state"] == "sent"
    clock[0] += timedelta(seconds=60)
    assert service.refresh_price_history().get("recovery_alert_pending") is not True

    # A subsequent outage starts a new incident episode after validated
    # recovery; expire the normal success cache so the scheduled read observes
    # the new dependency failure rather than reusing the old samples.
    clock[0] += timedelta(hours=25)
    mode[0] = "failed"
    new_outage = service.refresh_price_history()
    assert new_outage.get("alert_pending") is not True
    clock[0] += timedelta(seconds=300)
    new_incident = service.refresh_price_history()
    assert new_incident.get("alert_pending") is True

    # If the original incident was never acknowledged, a complete read cannot
    # create an orphan recovery notice.
    orphan_mode = ["failed"]
    orphan_clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]

    class OrphanExchange(Exchange):
        def lp_price_history(self, token_ids: tuple[str, ...], **kwargs: object) -> dict[str, object]:
            del kwargs
            if orphan_mode[0] == "failed":
                return {
                    "state": "unknown",
                    "history": {},
                    "errors": {token_id: "HistoryUnavailable" for token_id in token_ids},
                }
            start_ts = int((orphan_clock[0] - timedelta(hours=24)).timestamp())
            end_ts = int(orphan_clock[0].timestamp())
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

    orphan_store = PredictionArbitrageStore(tmp_path / "orphan")
    orphan = PolymarketLPService(
        orphan_store, OrphanExchange(), clock=lambda: orphan_clock[0]
    )
    orphan.refresh_price_history()
    orphan_mode[0] = "healthy"
    orphan_clock[0] += timedelta(seconds=60)
    orphan_result = orphan.refresh_price_history()
    assert orphan_result.get("recovery_alert_pending") is not True


def test_runtime_wakes_probe_deadline_and_retries_failed_feishu_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime schedules probes, isolates a partial cohort, and retries notices."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    mode = ["failed"]
    history_wait_calls: list[float] = []
    notification_times: list[datetime] = []
    notification_titles: list[str] = []
    risk_ticks: list[datetime] = []
    probe_calls: list[tuple[datetime, str | None]] = []
    invalid_wait = threading.Event()
    history_done = threading.Event()
    risk_started = threading.Event()
    partial_observed = threading.Event()
    delivery_done = threading.Event()
    delivery_results = iter(
        (
            {"code": 1, "msg": "temporary Feishu outage"},
            {"code": 0},
            {"code": 1, "msg": "temporary Feishu outage"},
            {"code": 0},
        )
    )

    class FakeTrading:
        def attach_metadata_cache(self, _store: object) -> None:
            pass

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            if mode[0] == "failed":
                raise TimeoutError("temporary catalog outage")
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                    for condition_id in (
                        "condition-recovered",
                        "condition-partial",
                    )
                ],
            }

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event, condition_ids
            probe_calls.append((clock[0], stage))
            if mode[0] == "partial":
                return {"state": "partial", "complete": False}
            return (
                {"state": "healthy", "complete": True}
                if mode[0] == "healthy"
                else {"state": "unknown", "complete": False}
            )

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "condition_id": condition_id,
                "market_id": f"market-{condition_id}",
                "accepting_orders": True,
                "outcomes": {"yes": {"token_id": f"token-{condition_id}"}},
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in condition_ids
            }

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: self._market(condition_id)
                    for condition_id in condition_ids
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
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
            if mode[0] == "failed":
                return {
                    "state": "unknown",
                    "history": {},
                    "errors": {token_id: "HistoryUnavailable" for token_id in token_ids},
                }
            if mode[0] == "partial":
                first_token = token_ids[0]
                partial_observed.set()
                mode[0] = "healthy"
                return {
                    "state": "known",
                    "history": {
                        first_token: [
                            {"t": start_ts, "p": "0.40"},
                            {"t": end_ts, "p": "0.41"},
                        ]
                    },
                    "errors": {
                        token_id: "HistoryUnavailable"
                        for token_id in token_ids[1:]
                    },
                }
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": [],
                "positions": [],
                "checked_at": clock[0],
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
            return {
                token_id: {
                    "condition_id": "condition-risk",
                    "token_id": token_id,
                    "received_at": clock[0],
                    "bids": [],
                    "asks": [],
                }
                for token_id in token_ids
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {"relayer_ready": True, "merge_ready": True, "checked_at": clock[0]}

        def close(self) -> None:
            pass

    class FakeMonitor:
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

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def lp_tick(self) -> dict[str, object]:
            risk_ticks.append(clock[0])
            risk_started.set()
            # This read stands in for the public risk reconciliation boundary;
            # the test proves it continues while history waits on its own clock.
            trading.lp_account_snapshot()
            trading.lp_order_books(("token-risk",))
            return {"state": "none"}

        def refresh_lp_observations(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

        def refresh_lp_share_watch(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

        def refresh_lp_dashboard_snapshot(self) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()

    def post_json(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        del url, timeout
        notification_times.append(clock[0])
        notification_titles.append(
            str(payload["content"]["text"]).split("\n", 1)[0]  # type: ignore[index]
        )
        result = next(delivery_results)
        if len(notification_times) == 2:
            mode[0] = "partial"
        if len(notification_times) == 4:
            delivery_done.set()
        return result

    notifier = FeishuWebhookNotifier(
        webhook_url="https://feishu.invalid/webhook", post_json=post_json
    )
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=None,
        ),
    )
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
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.005)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.1)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        if seconds >= 3600 and delivery_done.is_set():
            history_done.set()
            return True
        if not (seconds == pytest.approx(60) or seconds == pytest.approx(300)):
            invalid_wait.set()
            history_done.set()
            return True
        clock[0] += timedelta(seconds=float(seconds))
        threading.Event().wait(0.01)
        if delivery_done.is_set():
            history_done.set()
            return True
        if stop_event.is_set():
            history_done.set()
            return True
        return False

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path / "runtime",
        prediction_config_path=tmp_path / "runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=notifier,
        cross_venue_monitor=runtime_module._UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    started = False
    try:
        runtime.start()
        started = True
        assert history_done.wait(timeout=3)
        assert not invalid_wait.is_set(), history_wait_calls[-5:]
        assert history_wait_calls
        retry_index = history_wait_calls.index(300.0)
        assert retry_index > 0
        assert all(seconds == pytest.approx(60) for seconds in history_wait_calls[:retry_index])
        assert history_wait_calls[retry_index] == pytest.approx(300)
        assert history_wait_calls[-1] == pytest.approx(3600)
        assert risk_started.wait(timeout=2)
        assert len(risk_ticks) >= 2
        assert partial_observed.is_set()
        assert len(notification_times) == 4
        assert notification_times[1] - notification_times[0] == timedelta(minutes=5)
        assert notification_times[2] - notification_times[1] >= timedelta(seconds=60)
        assert notification_times[3] - notification_times[2] == timedelta(minutes=5)
        assert notification_titles[0].startswith("⚠️ LP 准备自动恢复中")
        assert notification_titles[1].startswith("⚠️ LP 准备自动恢复中")
        assert notification_titles[2].startswith("✅ LP 准备恢复")
        assert notification_titles[3].startswith("✅ LP 准备恢复")
        assert probe_calls
        assert delivery_done.is_set()
    finally:
        if started and runtime.state not in {"STOPPED", "FAILED"}:
            runtime.stop()
        assert runtime.state == "STOPPED"


def test_runtime_suppresses_short_fault_notification(
    tmp_path: Path,
) -> None:
    """A transient failure that recovers on the 60s probe emits no notice."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    mode = ["failed"]
    waits: list[float] = []
    notifications: list[dict[str, object]] = []
    stopped = threading.Event()

    class Exchange:
        def attach_metadata_cache(self, _store: object) -> None:
            pass

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            if mode[0] == "failed":
                raise TimeoutError("temporary catalog outage")
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": "short-condition",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event, stage, condition_ids
            return (
                {"state": "healthy", "complete": True}
                if mode[0] == "healthy"
                else {"state": "unknown", "complete": False}
            )

        @staticmethod
        def _market() -> dict[str, object]:
            return {
                "condition_id": "short-condition",
                "market_id": "short-market",
                "accepting_orders": True,
                "outcomes": {"yes": {"token_id": "short-token"}},
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {condition_id: self._market() for condition_id in condition_ids}

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: self._market() for condition_id in condition_ids
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
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
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path / "short-runtime")
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])

    def post_json(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        del url, timeout
        notifications.append(payload)
        return {"code": 0}

    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=exchange,
        notifier=FeishuWebhookNotifier(
            webhook_url="https://feishu.invalid/webhook",
            post_json=post_json,
        ),
        lock_path=tmp_path / "short-runtime.lock",
        lp=service,
    )
    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 1:
            mode[0] = "healthy"
        clock[0] += timedelta(seconds=seconds)
        if seconds >= 3600:
            stopped.set()
            return True
        if stop_event.is_set():
            stopped.set()
            return True
        return False

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path / "runtime",
        prediction_config_path=tmp_path / "runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime.lp = service
    runtime.execution = execution
    runtime._start_history_monitor()
    try:
        assert stopped.wait(timeout=3)
        assert waits[:1] == [pytest.approx(60)]
        assert waits[-1] == pytest.approx(3600)
        assert notifications == [], (
            waits,
            clock[0],
            service.preparation_snapshot(),
        )
        assert service.preparation_snapshot()["state"] == "ready"
    finally:
        runtime._history_stop_event.set()
        runtime._history_wakeup_event.set()
        thread = runtime._history_thread
        if thread is not None:
            thread.join(timeout=2)


def test_live_preparation_owner_cannot_be_reclaimed(tmp_path: Path) -> None:
    """A live preparation owner fences a second service until it exits."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    data_dir = tmp_path / "shared-preparation"
    first_read_started = threading.Event()
    release_first_read = threading.Event()
    first_read_finished = threading.Event()
    catalog_calls = [0]
    duplicate_reads = [0]

    def market() -> dict[str, object]:
        return {
            "condition_id": "condition-live-owner",
            "daily_pool_usd": Decimal("100"),
            "reward_active": True,
        }

    class ExchangeA:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            catalog_calls[0] += 1
            first_read_started.set()
            assert release_first_read.wait(timeout=3)
            first_read_finished.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [market()],
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {
                condition_id: {
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {"yes": {"token_id": "token-live-owner"}},
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
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
            }

    class ExchangeB(ExchangeA):
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            duplicate_reads[0] += 1
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [market()],
            }

    store_a = PredictionArbitrageStore(data_dir)
    store_b = PredictionArbitrageStore(data_dir)
    service_a = PolymarketLPService(
        store_a, ExchangeA(), clock=lambda: clock[0]
    )
    service_b = PolymarketLPService(
        store_b, ExchangeB(), clock=lambda: clock[0]
    )
    first_result: list[dict[str, object]] = []

    first_thread = threading.Thread(
        target=lambda: first_result.append(service_a.refresh_price_history())
    )
    first_thread.start()
    assert first_read_started.wait(timeout=3)

    second_result = service_b.refresh_price_history()
    assert second_result["preparation_outcome"] == "busy"
    assert duplicate_reads[0] == 0
    assert not first_read_finished.is_set()

    release_first_read.set()
    first_thread.join(timeout=3)
    assert first_result and first_result[0]["preparation_outcome"] == "success"
    assert catalog_calls[0] == 1

    failed_at = clock[0]
    store_a.lp_record_preparation_failure(
        "condition-stale-owner",
        generation=1,
        stage="history",
        error="TimeoutError",
        failed_at=failed_at,
        token_id="token-stale-owner",
    )
    claimed = store_a.lp_claim_preparation_retries(
        now=failed_at + timedelta(seconds=300),
        condition_ids=("condition-stale-owner",),
    )
    assert claimed and claimed[0]["state"] == "retrying"

    lock_path = data_dir / "prediction_arbitrage" / "lp-preparation.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    owner = context.Process(
        target=_hold_preparation_lock_then_exit,
        args=(str(lock_path), ready),
    )
    owner.start()
    try:
        assert ready.wait(3)
        assert store_b.lp_normalize_interrupted_preparation_items() == 0
    finally:
        owner.join(3)
        assert owner.exitcode == 0

    assert store_b.lp_normalize_interrupted_preparation_items() == 1
    assert store_b.lp_preparation_items()[0]["state"] == "waiting_retry"


def test_history_probe_has_constant_http_budget_and_rotates(tmp_path: Path) -> None:
    """Due history probes inspect one failed market and rotate the cohort."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    condition_ids = tuple(f"condition-{index:03d}" for index in range(81))
    token_ids = {
        condition_id: (f"{condition_id}-yes", f"{condition_id}-no")
        for condition_id in condition_ids
    }
    requests: list[tuple[str, ...]] = []

    def open_history(request: object, **_: object) -> object:
        payload = json.loads(getattr(request, "data").decode("utf-8"))
        requests.append(tuple(payload["markets"]))
        headers = Message()
        headers["Retry-After"] = "60"
        raise HTTPError(
            "https://clob.polymarket.com/batch-prices-history",
            429,
            "rate limited",
            headers,
            BytesIO(b'{"error":"rate limited"}'),
        )

    store = PredictionArbitrageStore(tmp_path / "rotating-history-probe")
    store.lp_metadata_cache_store_entries(
        {
            condition_id: (
                (clock[0] + timedelta(hours=24)).timestamp(),
                {
                    "condition_id": condition_id,
                    "accepting_orders": True,
                    "outcomes": {
                        "yes": {"token_id": token_ids[condition_id][0]},
                        "no": {"token_id": token_ids[condition_id][1]},
                    },
                },
            )
            for condition_id in condition_ids
        }
    )
    for condition_id in condition_ids:
        store.lp_record_preparation_failure(
            condition_id,
            generation=1,
            stage="history",
            error="TimeoutError",
            failed_at=clock[0],
            token_id=token_ids[condition_id][0],
        )
    store.lp_save_preparation(
        {
            "state": "partial",
            "stage": "history",
            "generation": 1,
            "paused": False,
            "next_retry_at": (clock[0] + timedelta(seconds=300)).isoformat(),
            "next_probe_at": clock[0].isoformat(),
        }
    )

    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        urlopen_fn=open_history,
        metadata_cache=store,
    )
    service = PolymarketLPService(store, adapter, clock=lambda: clock[0])

    first = service.refresh_price_history()
    assert first["preparation_outcome"] == "waiting_retry"
    assert len(requests) == 1
    assert len(requests[0]) == 2
    assert set(requests[0]) == set(token_ids[condition_ids[0]])

    clock[0] += timedelta(seconds=60)
    second = service.refresh_price_history()
    assert second["preparation_outcome"] == "waiting_retry"
    assert len(requests) == 2
    assert len(requests[1]) == 2
    assert set(requests[1]) == set(token_ids[condition_ids[1]])
    assert requests[0] != requests[1]


def test_full_read_retry_after_blocks_probe_and_data_retry(tmp_path: Path) -> None:
    """Full catalog/history 429s carry server pacing into both schedulers."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    catalog_requests: list[httpx.Request] = []

    def catalog_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rewards/markets/current"
        catalog_requests.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "90000"},
            json={"error": "temporarily rate limited"},
            request=request,
        )

    def catalog_public_factory() -> PublicClient:
        public = PublicClient(PRODUCTION)
        public._ctx.clob._client = httpx.Client(  # type: ignore[attr-defined]
            base_url=PRODUCTION.clob_url,
            transport=httpx.MockTransport(catalog_handler),
        )
        return public

    catalog_adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        public_client_factory=catalog_public_factory,
    )
    catalog_service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "catalog"),
        catalog_adapter,
        clock=lambda: clock[0],
    )

    catalog_failure = catalog_service.refresh_price_history()
    assert catalog_failure["preparation_outcome"] == "failure"
    assert len(catalog_requests) == 1  # native read fails before sponsored
    catalog_preparation = catalog_service.preparation_snapshot()
    catalog_probe_deadline = datetime.fromisoformat(
        str(catalog_preparation["next_probe_at"]).replace("Z", "+00:00")
    )
    catalog_retry_deadline = datetime.fromisoformat(
        str(catalog_preparation["next_retry_at"]).replace("Z", "+00:00")
    )
    server_deadline = clock[0] + timedelta(seconds=90000)
    assert catalog_probe_deadline >= server_deadline
    assert catalog_retry_deadline >= server_deadline

    clock[0] += timedelta(seconds=60)
    assert catalog_service.refresh_price_history()["preparation_outcome"] == "waiting_retry"
    clock[0] += timedelta(seconds=240)
    assert catalog_service.refresh_price_history()["preparation_outcome"] == "waiting_retry"
    assert len(catalog_requests) == 1

    clock[0] = server_deadline
    catalog_service.refresh_price_history()
    assert len(catalog_requests) > 2

    history_requests: list[object] = []

    def open_history(request: object, **_: object) -> object:
        history_requests.append(request)
        headers = Message()
        headers["Retry-After"] = "90000"
        raise HTTPError(
            "https://clob.polymarket.com/batch-prices-history",
            429,
            "rate limited",
            headers,
            BytesIO(b'{"error":"rate limited"}'),
        )

    history_adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        urlopen_fn=open_history,
    )
    probe_calls: list[tuple[str | None, tuple[str, ...]]] = []

    class HistoryExchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": "history-market",
                        "daily_pool_usd": Decimal("20"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: {
                        "condition_id": condition_id,
                        "accepting_orders": True,
                        "outcomes": {"yes": {"token_id": "history-token"}},
                    }
                    for condition_id in condition_ids
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
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
            return history_adapter.lp_price_history(
                token_ids,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
                stop_event=stop_event,
            )

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event
            probe_calls.append((stage, condition_ids))
            return {"state": "unknown", "complete": False}

    history_store = PredictionArbitrageStore(tmp_path / "history")
    history_service = PolymarketLPService(
        history_store, HistoryExchange(), clock=lambda: clock[0]
    )
    history_failure = history_service.refresh_price_history()
    assert history_failure["preparation"]["state"] == "partial"
    assert len(history_requests) == 1
    history_preparation = history_service.preparation_snapshot()
    history_probe_deadline = datetime.fromisoformat(
        str(history_preparation["next_probe_at"]).replace("Z", "+00:00")
    )
    history_item = history_store.lp_preparation_items()[0]
    history_retry_deadline = datetime.fromisoformat(
        str(history_item["next_retry_at"]).replace("Z", "+00:00")
    )
    history_server_deadline = clock[0] + timedelta(seconds=90000)
    assert history_probe_deadline >= history_server_deadline
    assert history_retry_deadline >= history_server_deadline

    clock[0] += timedelta(seconds=60)
    assert history_service.refresh_price_history()["preparation_outcome"] == "waiting_retry"
    clock[0] += timedelta(seconds=240)
    assert history_service.refresh_price_history()["preparation_outcome"] == "waiting_retry"
    assert len(history_requests) == 1
    assert probe_calls == []


def test_metadata_full_read_preserves_operator_and_retry_facts(tmp_path: Path) -> None:
    """Gamma full reads retain manual blockers and paced automatic failures."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    condition_id = "condition-metadata-facts"
    cases = (
        ("unauthorized", 401, False),
        ("forbidden", 403, False),
        ("rate_limited", 429, False),
        ("wrapped_certificate", None, True),
    )

    for label, status, wrapped_certificate in cases:
        requests: list[httpx.Request] = []

        def gamma_handler(
            request: httpx.Request,
            *,
            _status: int | None = status,
            _wrapped_certificate: bool = wrapped_certificate,
        ) -> httpx.Response:
            requests.append(request)
            if _wrapped_certificate:
                cause = ssl.SSLCertVerificationError("certificate sentinel")
                error = httpx.ConnectError("transport sentinel", request=request)
                error.__cause__ = cause
                raise error
            headers = {"Retry-After": "90000"} if _status == 429 else {}
            return httpx.Response(
                _status or 500,
                headers=headers,
                json={"error": "metadata read failed"},
                request=request,
            )

        def public_factory() -> PublicClient:
            public = PublicClient(PRODUCTION)
            public._ctx.gamma._client = httpx.Client(  # type: ignore[attr-defined]
                base_url=PRODUCTION.gamma_url,
                transport=httpx.MockTransport(gamma_handler),
            )
            return public

        adapter = PolymarketTradingClient(
            TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
            client=object(),
            public_client_factory=public_factory,
        )
        batch = adapter.lp_market_metadata_batch((condition_id,))
        assert requests and requests[0].url.path == "/markets/keyset"
        assert str(batch["failed_ids"][condition_id]).startswith("market_read_")  # type: ignore[index]
        facts = batch["failure_facts"][condition_id]  # type: ignore[index]
        assert isinstance(facts, dict)
        assert "message" not in facts
        if wrapped_certificate:
            assert "SSLCertVerificationError" in facts["error_chain"]
        else:
            assert facts["status"] == status
        if status == 429:
            assert facts["retry_after_seconds"] == 90000

        class Exchange:
            def lp_reward_catalog(
                self, *, stop_event: object = None
            ) -> dict[str, object]:
                del stop_event
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": clock[0],
                    "markets": [
                        {
                            "condition_id": condition_id,
                            "daily_pool_usd": Decimal("20"),
                            "reward_active": True,
                        }
                    ],
                }

            def lp_market_metadata_batch(
                self,
                condition_ids: tuple[str, ...],
                *,
                stop_event: object = None,
            ) -> dict[str, object]:
                del condition_ids, stop_event
                return batch

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
                            {"t": start_ts, "p": "0.40"},
                            {"t": end_ts, "p": "0.41"},
                        ]
                        for token_id in token_ids
                    },
                    "errors": {},
                }

        store = PredictionArbitrageStore(tmp_path / label)
        service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
        service.refresh_price_history()
        item = store.lp_preparation_items()[0]
        if status == 429:
            assert item["paused"] is False
            retry_at = datetime.fromisoformat(
                str(item["next_retry_at"]).replace("Z", "+00:00")
            )
            assert retry_at >= clock[0] + timedelta(seconds=90000)
        else:
            assert item["paused"] is True


def test_preparation_prioritizes_persisted_active_session(tmp_path: Path) -> None:
    """An active persisted LP session leads preparation without account reads."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    condition_ids = ("background-market", "watched-market", "held-market")
    metadata_requests: list[tuple[str, ...]] = []

    def market(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "daily_pool_usd": Decimal("20"),
            "reward_active": True,
        }

    def metadata(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "accepting_orders": True,
            "outcomes": {"yes": {"token_id": f"token-{condition_id}"}},
        }

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [market(condition_id) for condition_id in condition_ids],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_requests.append(tuple(requested))
            return {
                "state": "known",
                "markets": {
                    condition_id: metadata(condition_id) for condition_id in requested
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
                "deferred_ids": (),
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
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "held-session",
        "held-idempotency",
        state="entry_open",
        payload={"condition_id": "held-market"},
    )
    store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "selected_results": [{"condition_id": "watched-market"}],
            "recommendations": [],
            "candidates": [],
            "funnel": {},
            "selected_market_ids": [],
        }
    )

    # Construct after persisting the session and screening projection so the
    # order must come from durable public facts, not in-memory candidate state.
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
    result = service.refresh_price_history()

    assert result["preparation_outcome"] == "success"
    assert metadata_requests == [
        ("held-market", "watched-market", "background-market")
    ]


def test_invalid_market_history_is_isolated_and_rechecked(tmp_path: Path) -> None:
    """Malformed upstream history retries one market without pausing the cohort."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    markets = ("bad-history-market", "healthy-history-market")

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("20"),
                        "reward_active": True,
                    }
                    for condition_id in markets
                ],
            }

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: {
                        "condition_id": condition_id,
                        "accepting_orders": True,
                        "outcomes": {
                            "yes": {"token_id": f"token-{condition_id}"}
                        },
                    }
                    for condition_id in condition_ids
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
                "deferred_ids": (),
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
            history: dict[str, object] = {}
            errors: dict[str, str] = {}
            for token_id in token_ids:
                if token_id == "token-bad-history-market":
                    history[token_id] = [
                        {"t": start_ts, "p": "malformed-upstream-price"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                else:
                    history[token_id] = [
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
            return {
                "state": "partial" if "token-bad-history-market" in token_ids else "known",
                "history": history,
                "errors": errors,
            }

    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
    result = service.refresh_price_history()

    assert result["preparation"]["state"] == "partial"
    items = store.lp_preparation_items()
    assert len(items) == 1
    bad_item = items[0]
    assert bad_item["condition_id"] == "bad-history-market"
    assert bad_item["state"] == "waiting_retry"
    assert bad_item["paused"] is False
    assert bad_item["next_retry_at"] is not None
    assert store.lp_price_history_summary(
        "healthy-history-market",
        "token-healthy-history-market",
        now=clock[0],
    )["state"] == "known"


def test_failed_incident_delivery_survives_business_recovery(tmp_path: Path) -> None:
    """A failed incident retries after recovery, then permits one recovery notice."""

    from open_trader.prediction_runtime import PredictionRuntime

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    mode = ["failed"]
    notification_times: list[datetime] = []
    notification_titles: list[str] = []
    history_wait_calls: list[float] = []
    history_done = threading.Event()
    invalid_wait = threading.Event()

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            if mode[0] == "failed":
                raise TimeoutError("temporary catalog outage")
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": "recovered-market",
                        "daily_pool_usd": Decimal("20"),
                        "reward_active": True,
                    }
                ],
            }

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event, stage, condition_ids
            return (
                {"state": "healthy", "complete": True}
                if mode[0] == "healthy"
                else {"state": "unknown", "complete": False}
            )

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: {
                        "condition_id": condition_id,
                        "accepting_orders": True,
                        "outcomes": {
                            "yes": {"token_id": "token-recovered"}
                        },
                    }
                    for condition_id in condition_ids
                },
                "failed_ids": {},
                "confirmed_absent_ids": (),
                "deferred_ids": (),
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
                        {"t": start_ts, "p": "0.40"},
                        {"t": end_ts, "p": "0.41"},
                    ]
                    for token_id in token_ids
                },
                "errors": {},
            }

    store = PredictionArbitrageStore(tmp_path / "runtime")
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])

    def post_json(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        del url, timeout
        notification_times.append(clock[0])
        notification_titles.append(
            str(payload["content"]["text"]).split("\n", 1)[0]  # type: ignore[index]
        )
        if len(notification_times) == 1:
            # The business dependency recovers after the first incident send
            # fails, before the failed incident's t+600 retry deadline.
            mode[0] = "healthy"
            return {"code": 1, "msg": "temporary Feishu outage"}
        if len(notification_times) == 2:
            return {"code": 0}
        if len(notification_times) == 3:
            history_done.set()
            return {"code": 0}
        raise AssertionError("duplicate LP notification")

    notifier = FeishuWebhookNotifier(
        webhook_url="https://feishu.invalid/webhook", post_json=post_json
    )
    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=object(),
        notifier=notifier,
        lock_path=tmp_path / "runtime" / "execution.lock",
        lp=service,
    )

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        if seconds >= 3600 and history_done.is_set():
            return True
        if not (
            seconds == pytest.approx(60)
            or seconds == pytest.approx(240)
            or seconds == pytest.approx(300)
        ):
            invalid_wait.set()
            return True
        clock[0] += timedelta(seconds=float(seconds))
        threading.Event().wait(0.005)
        return stop_event.is_set()

    runtime = PredictionRuntime(
        data_dir=tmp_path / "runtime",
        prediction_config_path=tmp_path / "runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=notifier,
        cross_venue_monitor=object(),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime.lp = service
    runtime.execution = execution
    runtime._start_history_monitor()
    assert history_done.wait(timeout=3)
    runtime._history_stop_event.set()
    assert runtime._history_thread is not None
    runtime._history_thread.join(timeout=3)

    assert not invalid_wait.is_set(), (
        history_wait_calls,
        notification_times,
        service.preparation_snapshot(),
    )
    assert notification_times == [
        datetime(2026, 9, 20, 5, 5, tzinfo=UTC),
        datetime(2026, 9, 20, 5, 10, tzinfo=UTC),
        datetime(2026, 9, 20, 5, 11, tzinfo=UTC),
    ]
    assert notification_titles[0].startswith("⚠️ LP 准备自动恢复中")
    assert notification_titles[1].startswith("⚠️ LP 准备自动恢复中")
    assert notification_titles[2].startswith("✅ LP 准备恢复")
    assert mode[0] == "healthy"
    assert service.preparation_snapshot()["recovery_alert_state"] == "sent"


def test_recovery_delivery_retry_does_not_read_business_dependencies(
    tmp_path: Path,
) -> None:
    """A persisted recovery retry wakes Feishu delivery without a business read."""

    from open_trader.prediction_runtime import PredictionRuntime

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    dependency_calls: list[str] = []
    notification_times: list[datetime] = []
    history_wait_calls: list[float] = []
    recovery_done = threading.Event()
    invalid_wait = threading.Event()

    class Exchange:
        def _unexpected(self, name: str) -> None:
            dependency_calls.append(name)
            raise AssertionError(f"business dependency read during recovery retry: {name}")

        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            self._unexpected("catalog")
            return {}

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event, stage, condition_ids
            self._unexpected("probe")
            return {}

        def lp_market_metadata_batch(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            self._unexpected("metadata")
            return {}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del token_ids, start_ts, end_ts, fidelity, stop_event
            self._unexpected("history")
            return {}

    store = PredictionArbitrageStore(tmp_path / "runtime")
    service = PolymarketLPService(store, Exchange(), clock=lambda: clock[0])
    store.lp_save_preparation(
        {
            "generation": 1,
            "state": "ready",
            "stage": "complete",
            "paused": False,
            "attempt": 0,
            "failure_count": 0,
            "completed_count": 1,
            "total_count": 1,
            "last_success_at": clock[0] - timedelta(minutes=1),
            "fault_started_at": clock[0] - timedelta(minutes=10),
            "fault_alert_state": "sent",
            "fault_alert_attempts": 1,
            "fault_alert_next_at": None,
            "recovery_alert_state": "failed",
            "recovery_alert_attempts": 1,
            "recovery_alert_next_at": clock[0],
            "recovery_alert_claimed_at": None,
            "recovery_alert_sent_at": None,
            "next_retry_at": None,
            "next_probe_at": None,
            "last_error": None,
            "last_failure_at": None,
        }
    )

    def post_json(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        del url, payload, timeout
        notification_times.append(clock[0])
        if len(notification_times) == 1:
            return {"code": 1, "msg": "temporary Feishu outage"}
        recovery_done.set()
        return {"code": 0}

    notifier = FeishuWebhookNotifier(
        webhook_url="https://feishu.invalid/webhook", post_json=post_json
    )
    execution = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=object(),
        notifier=notifier,
        lock_path=tmp_path / "runtime" / "execution.lock",
        lp=service,
    )

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        if seconds >= 3600 and recovery_done.is_set():
            return True
        if seconds != pytest.approx(600):
            invalid_wait.set()
            return True
        clock[0] += timedelta(seconds=float(seconds))
        threading.Event().wait(0.005)
        return stop_event.is_set()

    runtime = PredictionRuntime(
        data_dir=tmp_path / "runtime",
        prediction_config_path=tmp_path / "runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=notifier,
        cross_venue_monitor=object(),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime.lp = service
    runtime.execution = execution
    # A notification-only retry must not wake the candidate scan loop.  Clear
    # the independently observable wakeup before starting the history worker so
    # the assertion covers this run rather than runtime construction.
    runtime._lp_candidate_refresh_requested.clear()
    runtime._start_history_monitor()
    assert recovery_done.wait(timeout=3), (
        history_wait_calls,
        notification_times,
        dependency_calls,
        service.preparation_snapshot(),
    )
    runtime._history_stop_event.set()
    assert runtime._history_thread is not None
    runtime._history_thread.join(timeout=3)

    assert not invalid_wait.is_set(), history_wait_calls
    assert dependency_calls == []
    assert not runtime._lp_candidate_refresh_requested.is_set()
    assert notification_times == [
        datetime(2026, 9, 20, 5, 0, tzinfo=UTC),
        datetime(2026, 9, 20, 5, 10, tzinfo=UTC),
    ]
    assert service.preparation_snapshot()["recovery_alert_state"] == "sent"


def test_rotating_probes_keep_market_recovery_transitions_independent(
    tmp_path: Path,
) -> None:
    """Each rotating market keeps its own cheap-probe recovery transition."""

    clock = [datetime(2026, 9, 20, 5, 0, tzinfo=UTC)]
    condition_ids = ("condition-a", "condition-b")
    probe_plan: dict[str, list[str]] = {
        "condition-a": ["unknown", "healthy", "healthy"],
        "condition-b": ["healthy", "unknown"],
    }
    probe_calls: list[tuple[str, tuple[str, ...]]] = []
    history_requests: list[tuple[str, ...]] = []

    def catalog() -> dict[str, object]:
        return {
            "state": "known",
            "complete": True,
            "checked_at": clock[0],
            "markets": [
                {
                    "condition_id": condition_id,
                    "daily_pool_usd": Decimal("20"),
                    "reward_active": True,
                }
                for condition_id in condition_ids
            ],
        }

    def metadata(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "accepting_orders": True,
            "outcomes": {"yes": {"token_id": f"token-{condition_id}"}},
        }

    class Exchange:
        def lp_reward_catalog(self, *, stop_event: object = None) -> dict[str, object]:
            del stop_event
            return catalog()

        def lp_preparation_probe(
            self,
            *,
            stop_event: object = None,
            stage: str | None = None,
            condition_ids: tuple[str, ...] = (),
        ) -> dict[str, object]:
            del stop_event
            assert stage == "history"
            assert len(condition_ids) == 1
            condition_id = condition_ids[0]
            probe_calls.append((condition_id, condition_ids))
            state = probe_plan[condition_id].pop(0)
            return {
                "state": state,
                "complete": state == "healthy",
                "checked_at": clock[0],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "markets": {
                    condition_id: metadata(condition_id)
                    for condition_id in requested
                },
                "failed_ids": {},
                "failure_facts": {},
                "confirmed_absent_ids": (),
                "deferred_ids": (),
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
            del start_ts, end_ts, fidelity, stop_event
            history_requests.append(tuple(token_ids))
            return {
                "state": "unknown",
                "history": {},
                "errors": {token_id: "HistoryUnavailable" for token_id in token_ids},
            }

    exchange = Exchange()
    store = PredictionArbitrageStore(tmp_path / "rotating-probes")
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])

    initial = service.refresh_price_history()
    assert initial["preparation_outcome"] == "failure"
    assert history_requests == [("token-condition-a", "token-condition-b")]

    # A's unknown probe establishes its own unhealthy state.
    clock[0] += timedelta(seconds=60)
    service.refresh_price_history()
    assert probe_calls[-1] == ("condition-a", ("condition-a",))
    assert len(history_requests) == 1

    # B becomes healthy and is allowed one early invalid business read.
    clock[0] += timedelta(seconds=60)
    service.refresh_price_history()
    assert probe_calls[-1] == ("condition-b", ("condition-b",))
    assert history_requests[-1] == ("token-condition-b",)

    # The probe state survives reconstruction; A's own unknown -> healthy
    # transition is still allowed to wake exactly one early read.
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    clock[0] += timedelta(seconds=60)
    service.refresh_price_history()
    assert probe_calls[-1] == ("condition-a", ("condition-a",))
    assert history_requests[-1] == ("token-condition-a",)
    assert len(history_requests) == 3

    # B goes unhealthy, then A remains healthy.  A's invalid business read is
    # still in backoff, so the later A probe must not trigger another read.
    clock[0] += timedelta(seconds=60)
    service.refresh_price_history()
    assert probe_calls[-1] == ("condition-b", ("condition-b",))
    assert len(history_requests) == 3

    clock[0] += timedelta(seconds=60)
    service.refresh_price_history()
    assert probe_calls[-1] == ("condition-a", ("condition-a",))
    assert len(history_requests) == 3


def test_concurrent_metadata_batches_keep_response_facts_isolated() -> None:
    """Concurrent Gamma batches retain each response's status and pacing facts."""

    condition_ids = tuple(f"condition-{index:03d}" for index in range(101))
    requests: list[tuple[str, ...]] = []
    batch_b_hook_seen = threading.Event()

    class DelayedUnauthorizedBody(httpx.SyncByteStream):
        def __iter__(self):
            assert batch_b_hook_seen.wait(timeout=3)
            yield b'{"error":"unauthorized"}'

    def gamma_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets/keyset"
        requested = tuple(request.url.params.get_list("condition_ids"))
        requests.append(requested)
        if len(requested) == 100:
            return httpx.Response(
                401,
                headers={"content-type": "application/json"},
                stream=DelayedUnauthorizedBody(),
                request=request,
            )
        assert len(requested) == 1
        return httpx.Response(
            429,
            headers={
                "content-type": "application/json",
                "Retry-After": "90000",
            },
            json={"error": "temporarily rate limited"},
            request=request,
        )

    def mark_response(response: httpx.Response) -> None:
        if response.status_code == 429:
            batch_b_hook_seen.set()

    def public_factory() -> PublicClient:
        public = PublicClient(PRODUCTION)
        public._ctx.gamma._client = httpx.Client(  # type: ignore[attr-defined]
            base_url=PRODUCTION.gamma_url,
            transport=httpx.MockTransport(gamma_handler),
            event_hooks={"response": [mark_response]},
        )
        return public

    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        client=object(),
        public_client_factory=public_factory,
    )
    result = adapter.lp_market_metadata_batch(condition_ids)

    assert sorted(map(len, requests)) == [1, 100]
    assert set(result["failed_ids"]) == set(condition_ids)
    failure_facts = result["failure_facts"]
    assert isinstance(failure_facts, dict)
    first_batch = condition_ids[:100]
    last_batch = condition_ids[100:]
    assert all(failure_facts[condition_id]["status"] == 401 for condition_id in first_batch)
    assert all(
        "retry_after_seconds" not in failure_facts[condition_id]
        for condition_id in first_batch
    )
    assert failure_facts[last_batch[0]]["status"] == 429
    assert failure_facts[last_batch[0]]["retry_after_seconds"] == 90000
    assert all(
        result["failed_ids"][condition_id] == "market_read_RequestRejectedError"
        for condition_id in first_batch
    )
    assert result["failed_ids"][last_batch[0]] == "market_read_RateLimitError"
    assert "unauthorized" not in repr(result)
