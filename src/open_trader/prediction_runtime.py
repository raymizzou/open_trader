from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import sys
import threading
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Literal

from .notifications import NullNotifier
from .polymarket_monitor import PolymarketMonitor
from .polymarket_relation_discovery import (
    LlmRelationValidator,
    discover_threshold_relation_catalog,
)
from .polymarket_trading import PolymarketTradingClient, load_trading_config
from .predict_cross_venue import (
    LlmCrossVenueEquivalenceValidator,
    POLYMARKET_CHAIN_ID,
    PREDICT_CHAIN_ID,
    PredictCrossVenueMonitor,
)
from .predict_source import PredictSource
from .predict_trading import PredictTradingClient
from .prediction_arbitrage import (
    MAX_CROSS_UNSETTLED_PRINCIPAL,
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_THRESHOLD_ANNUALIZED_YIELD,
)
from .prediction_arbitrage_execution import (
    BOOK_FRESHNESS_SECONDS,
    PredictionExecutionService,
)
from .prediction_arbitrage_store import (
    _CROSS_AUTO_DAILY_PRINCIPAL_CAP,
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)
from .prediction_live_resolver import PredictionLiveResolver
from .prediction_monitor_selection import MonitorSelectionStore
from .prediction_monitor_selection_driver import PredictionMonitorSelectionDriver
from .prediction_predict_snapshot_refresher import PredictAccountSnapshotRefresher
from .prediction_read_only import (
    PolymarketReadOnlyGuard,
    PredictReadOnlyGuard,
    guard_polymarket_client,
    guard_predict_client,
)
from .prediction_solver_server import SolverServerOwner
from .prediction_n_leg_shadow import (
    NLegShadowClient,
    NLegShadowScheduler,
    legacy_shadow_snapshot,
)
from .prediction_title_translation import LlmTitleTranslator
from .relation_catalog import RelationCatalog

logger = logging.getLogger(__name__)
_CROSS_VENUE_START_TIMEOUT = 5
_DEFAULT_HOLDING_RECONCILER = object()

# Keep the old spelling available for the existing Dashboard test seam.
discover_threshold_relations = discover_threshold_relation_catalog


class PredictionRuntimeOwnershipError(RuntimeError):
    """The Prediction data directory already has a live Runtime owner."""


class PredictionRuntimeCompatibilityError(RuntimeError):
    pass


class _RuntimeOwnershipLock:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("prediction runtime ownership is already acquired")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise PredictionRuntimeOwnershipError(
                f"prediction runtime ownership is unavailable: {self._path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @property
    def held(self) -> bool:
        return self._handle is not None


class _UnavailableCrossVenueMonitor:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "degraded",
            "mode": "observe_only",
            "reason": self._reason,
            "funnel": {},
            "events": [],
            "opportunities": [],
        }


class _CrossVenueRuntime:
    def __init__(self, monitor: PredictCrossVenueMonitor) -> None:
        self._monitor = monitor
        self._predict = getattr(monitor, "_predict", None)
        self._started = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_error: BaseException | None = None
        self._stop_error: BaseException | None = None

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("cross-venue runtime is already started")

        async def run() -> None:
            self._loop = asyncio.get_running_loop()
            try:
                try:
                    result = self._monitor.start()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    self._start_error = exc
                finally:
                    self._started.set()
                if self._start_error is not None:
                    return
                await asyncio.to_thread(self._stop_requested.wait)
                try:
                    result = self._monitor.stop()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    self._stop_error = exc
            finally:
                self._loop = None

        self._thread = threading.Thread(
            target=lambda: asyncio.run(run()),
            name="predict-cross-venue-monitor",
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(timeout=_CROSS_VENUE_START_TIMEOUT):
            self._stop_requested.set()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
            if self.thread_alive:
                raise RuntimeError("cross-venue monitor did not start or stop")
            raise RuntimeError("cross-venue monitor did not start")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise RuntimeError("cross-venue monitor failed to start") from error

    def snapshot(self) -> dict[str, object]:
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
            or self._thread is threading.current_thread()
        ):
            return self._monitor.snapshot()
        coroutine = self._snapshot_on_loop()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return {}
        try:
            return future.result(timeout=1)
        except Exception:
            return {}

    async def _snapshot_on_loop(self) -> dict[str, object]:
        return self._monitor.snapshot()

    def refresh_opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
            or self._thread is threading.current_thread()
        ):
            return None
        coroutine = self._refresh_on_loop(opportunity_id)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return None
        try:
            return future.result(timeout=15)
        except Exception:
            return None

    async def _refresh_on_loop(
        self, opportunity_id: str
    ) -> dict[str, object] | None:
        refresh = getattr(self._monitor, "refresh_opportunity", None)
        if not callable(refresh):
            return None
        value = refresh(opportunity_id)
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else None

    def stop(self) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        if self.thread_alive:
            raise RuntimeError("cross-venue monitor thread did not stop")
        if self._stop_error is not None:
            error = self._stop_error
            raise RuntimeError("cross-venue monitor failed to stop") from error


def _cross_venue_gamma_lookup(
    condition_ids: tuple[str, ...], *, closed: bool
) -> tuple[object, ...]:
    from polymarket import PublicClient

    client = PublicClient()
    try:
        paginator = client.list_markets(condition_ids=condition_ids, closed=closed)
        iter_items = getattr(paginator, "iter_items", None)
        return tuple(iter_items()) if callable(iter_items) else tuple(paginator)
    finally:
        client.close()


def _build_cross_venue_monitor(
    *,
    trading_config: object,
    prediction_monitor: PolymarketMonitor,
    store: PredictionArbitrageStore,
    execution: PredictionExecutionService,
    predict_trading: object | None = None,
    max_llm_calls: int | None = None,
    holding_reconciler: Callable[[], object] | None | object = _DEFAULT_HOLDING_RECONCILER,
    shadow_observer: Callable[[Mapping[str, object], str], object] | None = None,
) -> PredictCrossVenueMonitor | _UnavailableCrossVenueMonitor:
    predict_config = getattr(trading_config, "predict", None)
    if predict_config is None:
        return _UnavailableCrossVenueMonitor("predict_not_configured")
    if predict_trading is None:
        return _UnavailableCrossVenueMonitor("predict_construction_failed")
    if holding_reconciler is _DEFAULT_HOLDING_RECONCILER:
        holding_reconciler = getattr(execution, "reconcile_cross_holdings_once", None)
    account_identities: dict[str, dict[str, str]] = {}
    if getattr(trading_config, "wallet_address", ""):
        account_identities["polymarket"] = {
            "account_id": str(trading_config.wallet_address),
            "chain_id": POLYMARKET_CHAIN_ID,
        }
    if predict_config is not None and getattr(predict_config, "wallet_address", ""):
        account_identities["predict.fun"] = {
            "account_id": str(predict_config.wallet_address),
            "chain_id": PREDICT_CHAIN_ID,
        }
    try:
        return PredictCrossVenueMonitor(
            predict_source=PredictSource(predict_config),
            polymarket_monitor=prediction_monitor,
            validator=LlmCrossVenueEquivalenceValidator(
                store,
                max_llm_calls=max_llm_calls,
            ),
            gamma_lookup=_cross_venue_gamma_lookup,
            predict_quote_fn=getattr(predict_trading, "quote_market_buy", None),
            store=store,
            ready_observer=execution.notify_ready_opportunity,
            shadow_observer=shadow_observer,
            holding_reconciler=holding_reconciler,
            account_identities=account_identities,
        )
    except Exception:
        return _UnavailableCrossVenueMonitor("predict_construction_failed")


def _prediction_safety_policy(trading_config: object) -> dict[str, object]:
    predict = getattr(trading_config, "predict", None)
    return {
        "policy_version": "prediction-controls-v1",
        "identity": {
            "signer_address": str(getattr(trading_config, "signer_address", "")),
            "wallet_address": str(getattr(trading_config, "wallet_address", "")),
            "predict_wallet_address": str(getattr(predict, "wallet_address", "")),
            "predict_environment": str(getattr(predict, "environment", "")),
        },
        "limits": {
            "book_freshness_seconds": format(BOOK_FRESHNESS_SECONDS, "f"),
            "cross_auto_daily_principal_cap": format(
                _CROSS_AUTO_DAILY_PRINCIPAL_CAP, "f"
            ),
            "max_cross_unsettled_principal": format(
                MAX_CROSS_UNSETTLED_PRINCIPAL, "f"
            ),
            "max_emergency_loss": format(MAX_EMERGENCY_LOSS, "f"),
            "max_normal_cost": format(MAX_NORMAL_COST, "f"),
            "max_wallet_balance": format(MAX_WALLET_BALANCE, "f"),
            "min_estimated_profit": format(MIN_ESTIMATED_PROFIT, "f"),
            "min_threshold_annualized_yield": format(
                MIN_THRESHOLD_ANNUALIZED_YIELD, "f"
            ),
        },
    }


class PredictionRuntime:
    def __init__(
        self,
        *,
        data_dir: Path,
        prediction_config_path: Path,
        dashboard_url: str,
        notifier: object | None = None,
        cross_venue_monitor: object | None = None,
        mode: Literal["production", "shadow"] = "production",
        git_sha: str = "",
        reader_generation: int | None = None,
        solver_server_factory: Callable[[], SolverServerOwner] | None = None,
        enable_n_leg_background: bool = True,
    ) -> None:
        if mode not in {"production", "shadow"}:
            raise ValueError("prediction runtime mode must be production or shadow")
        if reader_generation is not None and (
            type(reader_generation) is not int or reader_generation < 1
        ):
            raise ValueError("prediction reader generation must be a positive integer")
        self._data_dir = Path(data_dir)
        self._prediction_config_path = Path(prediction_config_path)
        self._dashboard_url = str(dashboard_url)
        self._mode = mode
        self._git_sha = str(git_sha)
        self._reader_generation = reader_generation
        self._enable_n_leg_background = bool(enable_n_leg_background)
        self._solver_server_factory = solver_server_factory or (
            lambda: SolverServerOwner(
                [sys.executable, "-m", "open_trader.prediction_solver_worker", "--backend", "cp_sat"]
            )
        )
        self._owner_thread_id = threading.get_ident()
        self._notifier = NullNotifier() if mode == "shadow" else notifier or NullNotifier()
        self._injected_cross_venue_monitor = cross_venue_monitor
        self._owner = _RuntimeOwnershipLock(
            self._data_dir / "prediction_arbitrage" / "runtime.lock"
        )
        self._state = "NEW"
        self._prediction_trading: object | None = None
        self._predict_trading: object | None = None
        self._cross_runtime: _CrossVenueRuntime | None = None
        self.store: PredictionArbitrageStore | None = None
        self.monitor: PolymarketMonitor | None = None
        self.cross_venue_monitor: object | None = None
        self.execution: PredictionExecutionService | None = None
        self.relation_catalog: RelationCatalog | None = None
        self.solver_server: SolverServerOwner | None = None
        self.live_resolver: PredictionLiveResolver | None = None
        self.monitor_selection_driver: PredictionMonitorSelectionDriver | None = None
        self.predict_snapshot_refresher: PredictAccountSnapshotRefresher | None = None
        self.n_leg_shadow: NLegShadowScheduler | None = None
        self._shadow_guards: ExitStack | None = None
        self._shadow_failure_lock = threading.Lock()
        self._shadow_failure_event = threading.Event()
        self._shadow_failure: dict[str, object] | None = None
        self._shadow_attempts: list[dict[str, object]] = []
        self._relation_validator: object | None = None
        self._cross_validator: object | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def mode(self) -> Literal["production", "shadow"]:
        return self._mode

    @property
    def production_owner(self) -> bool:
        return self._mode == "production" and self._owner.held

    @property
    def shadow_evidence(self) -> dict[str, object]:
        def counters(validator: object | None) -> dict[str, int]:
            return {
                "calls": int(getattr(validator, "codex_calls", 0)),
                "successes": int(getattr(validator, "codex_successes", 0)),
            }

        with self._shadow_failure_lock:
            first = None if self._shadow_failure is None else dict(self._shadow_failure)
            attempts = [dict(attempt) for attempt in self._shadow_attempts]
        return {
            "mode": self._mode,
            "guard_attempts": attempts,
            "first_violation": first,
            "codex": {
                "relation": counters(self._relation_validator),
                "cross_venue": counters(self._cross_validator),
            },
        }

    def _record_shadow_violation(self, attempt: dict[str, object]) -> None:
        sanitized = {
            "venue": str(attempt.get("venue", "")),
            "kind": str(attempt.get("kind", "")),
            "method": str(attempt.get("method", "")),
            "call_chain": [
                str(frame) for frame in attempt.get("call_chain", [])
            ][:12],
        }
        with self._shadow_failure_lock:
            self._shadow_attempts.append(sanitized)
            if self._shadow_failure is None:
                self._shadow_failure = sanitized
                self._shadow_failure_event.set()

    def poll_shadow_failure(self) -> dict[str, object] | None:
        if (
            self._mode != "shadow"
            or threading.get_ident() != self._owner_thread_id
            or not self._shadow_failure_event.is_set()
        ):
            return None
        with self._shadow_failure_lock:
            failure = None if self._shadow_failure is None else dict(self._shadow_failure)
        if failure is not None and self._state not in {"STOPPED", "NEW"}:
            self.stop()
        return failure

    def start(self) -> None:
        if self._state != "NEW":
            raise RuntimeError(f"prediction runtime cannot start from {self._state}")
        self._owner_thread_id = threading.get_ident()
        self._state = "STARTING"
        if self._mode == "shadow":
            self._start_shadow()
            return
        try:
            self._owner.acquire()
            if self._reader_generation is not None:
                minimum_reader_generation = read_minimum_reader_generation(
                    self._data_dir
                )
                if self._reader_generation < minimum_reader_generation:
                    raise PredictionRuntimeCompatibilityError(
                        f"prediction reader generation {self._reader_generation} "
                        f"is below required {minimum_reader_generation}"
                    )
            self.solver_server = self._solver_server_factory()
            self.store = PredictionArbitrageStore(self._data_dir)
            self.relation_catalog = RelationCatalog(self._data_dir)
            trading_config = load_trading_config(self._prediction_config_path)
            apply_safety_policy = getattr(self.store, "apply_safety_policy", None)
            if callable(apply_safety_policy):
                apply_safety_policy(
                    _prediction_safety_policy(trading_config),
                    git_sha=self._git_sha,
                )
            self._prediction_trading = PolymarketTradingClient.from_keychain(
                trading_config
            )
            try:
                self._predict_trading = PredictTradingClient.from_keychain(
                    trading_config
                )
            except Exception:
                self._predict_trading = None
            relation_validator = LlmRelationValidator(self.store)
            title_translator = LlmTitleTranslator(self.store)
            self.monitor = PolymarketMonitor(
                store=self.store,
                trading=self._prediction_trading,
                relation_discovery=discover_threshold_relation_catalog,
                relation_validator=relation_validator,
                title_translator=title_translator,
                relation_catalog=self.relation_catalog,
            )
            self.execution = PredictionExecutionService(
                store=self.store,
                monitor=self.monitor,
                trading=self._prediction_trading,
                notifier=self._notifier,
                lock_path=self._data_dir
                / "prediction_arbitrage"
                / "execution.lock",
                dashboard_url=self._dashboard_url,
                predict_trading=self._predict_trading,
            )
            self.monitor.set_ready_observer(
                self.execution.notify_ready_opportunity
            )
            self.monitor.set_observation_observer(
                self.execution.notify_observation
            )
            self.monitor.set_auto_eat_observer(
                self.execution.auto_eat_threshold
            )
            self.monitor.set_failure_observer(
                self.execution.notify_monitor_failure
            )
            shadow_observer = self._configure_n_leg_shadow()
            cross_monitor = self._injected_cross_venue_monitor
            if cross_monitor is None:
                cross_monitor = _build_cross_venue_monitor(
                    trading_config=trading_config,
                    prediction_monitor=self.monitor,
                    store=self.store,
                    execution=self.execution,
                    predict_trading=self._predict_trading,
                    holding_reconciler=getattr(
                        self.execution, "reconcile_cross_holdings_once", None
                    ),
                    shadow_observer=shadow_observer,
                )
            if not isinstance(cross_monitor, _UnavailableCrossVenueMonitor):
                self._cross_runtime = _CrossVenueRuntime(cross_monitor)
            self.cross_venue_monitor = self._cross_runtime or cross_monitor
            set_cross_venue_monitor = getattr(
                self.execution, "set_cross_venue_monitor", None
            )
            if callable(set_cross_venue_monitor):
                set_cross_venue_monitor(
                    self._cross_runtime or self.cross_venue_monitor
                )
        except Exception:
            self._state = "FAILED"
            self._cleanup_resources()
            raise

        try:
            reconcile = self.execution.reconcile_startup()
            if isinstance(reconcile, Mapping) and reconcile.get("state") == "locked":
                self._state = "NOT_READY"
                logger.warning(
                    "prediction_runtime_state state=NOT_READY pid=%s data_dir=%s reason=%s",
                    os.getpid(),
                    self._data_dir,
                    reconcile.get("reason", "reconcile_locked"),
                )
                return
        except Exception:
            self._state = "NOT_READY"
            logger.exception(
                "prediction_runtime_state state=NOT_READY pid=%s data_dir=%s",
                os.getpid(),
                self._data_dir,
            )
            return

        try:
            self.monitor.start()
            if self._cross_runtime is not None:
                try:
                    self._cross_runtime.start()
                except Exception:
                    self._cross_runtime.stop()
                    self._cross_runtime = None
                    self.cross_venue_monitor = _UnavailableCrossVenueMonitor(
                        "predict_runtime_failed"
                    )
                    set_cross_venue_monitor = getattr(
                        self.execution, "set_cross_venue_monitor", None
                    )
                    if callable(set_cross_venue_monitor):
                        set_cross_venue_monitor(self.cross_venue_monitor)
            if self._predict_trading is not None and callable(
                getattr(self.execution, "_refresh_predict_account_snapshot", None)
            ):
                # #93: keep the predict snapshot cache warm off the HTTP threads.
                self.predict_snapshot_refresher = PredictAccountSnapshotRefresher(
                    execution=self.execution
                )
                self.predict_snapshot_refresher.start()
            if self._enable_n_leg_background:
                selection_store = MonitorSelectionStore(self._data_dir)
                selection_lock = threading.RLock()
                self.live_resolver = PredictionLiveResolver(
                    data_dir=self._data_dir,
                    relation_catalog=self.relation_catalog,
                    monitor=self.monitor,
                    solver_server=self.solver_server,
                    selection_store=selection_store,
                    selection_lock=selection_lock,
                    store=self.store,
                    execution=self.execution,
                )
                self.live_resolver.start()
                self.monitor_selection_driver = PredictionMonitorSelectionDriver(
                    relation_catalog=self.relation_catalog,
                    selection_store=selection_store,
                    selection_lock=selection_lock,
                    idle_check=self.live_resolver.is_idle,
                )
                self.monitor_selection_driver.start()
            self._state = "RUNNING"
            logger.info(
                "prediction_runtime_state state=RUNNING pid=%s data_dir=%s",
                os.getpid(),
                self._data_dir,
            )
        except Exception:
            self._state = "FAILED"
            self._cleanup_resources()
            raise

    def _start_shadow(self) -> None:
        try:
            self._owner.acquire()
            self.solver_server = self._solver_server_factory()
            self.store = PredictionArbitrageStore(self._data_dir)
            trading_config = load_trading_config(self._prediction_config_path)
            self._prediction_trading = PolymarketTradingClient.from_keychain(
                trading_config
            )
            try:
                self._predict_trading = PredictTradingClient.from_keychain(trading_config)
            except Exception:
                self._predict_trading = None
            self._relation_validator = LlmRelationValidator(
                self.store,
                max_llm_calls=3,
            )
            self.monitor = PolymarketMonitor(
                store=self.store,
                trading=self._prediction_trading,
                relation_discovery=discover_threshold_relation_catalog,
                relation_validator=self._relation_validator,
                title_translator=LlmTitleTranslator(self.store),
            )
            self.execution = PredictionExecutionService(
                store=self.store,
                monitor=self.monitor,
                trading=self._prediction_trading,
                notifier=NullNotifier(),
                lock_path=self._data_dir / "prediction_arbitrage" / "execution.lock",
                dashboard_url=self._dashboard_url,
                predict_trading=self._predict_trading,
            )
            self.monitor.set_ready_observer(self.execution.notify_ready_opportunity)
            self.monitor.set_observation_observer(self.execution.notify_observation)
            self.monitor.set_failure_observer(self.execution.notify_monitor_failure)
            shadow_observer = self._configure_n_leg_shadow()
            cross_monitor = self._injected_cross_venue_monitor
            if cross_monitor is None:
                cross_monitor = _build_cross_venue_monitor(
                    trading_config=trading_config,
                    prediction_monitor=self.monitor,
                    store=self.store,
                    execution=self.execution,
                    predict_trading=self._predict_trading,
                    max_llm_calls=3,
                    holding_reconciler=None,
                    shadow_observer=shadow_observer,
                )
            if not isinstance(cross_monitor, _UnavailableCrossVenueMonitor):
                self._cross_runtime = _CrossVenueRuntime(cross_monitor)
                self._cross_validator = getattr(cross_monitor, "_validator", None)
            self.cross_venue_monitor = self._cross_runtime or cross_monitor
            set_cross_venue_monitor = getattr(self.execution, "set_cross_venue_monitor", None)
            if callable(set_cross_venue_monitor):
                set_cross_venue_monitor(self._cross_runtime or self.cross_venue_monitor)

            self._shadow_guards = ExitStack()
            self._shadow_guards.enter_context(
                guard_polymarket_client(
                    self._prediction_trading,
                    PolymarketReadOnlyGuard(self._record_shadow_violation),
                )
            )
            if self._predict_trading is not None:
                self._shadow_guards.enter_context(
                    guard_predict_client(
                        self._predict_trading,
                        PredictReadOnlyGuard(self._record_shadow_violation),
                    )
                )
        except Exception:
            self._state = "FAILED"
            self._cleanup_resources()
            raise

        try:
            self.monitor.start()
            if self._cross_runtime is not None:
                self._cross_runtime.start()
            if self._predict_trading is not None and callable(
                getattr(self.execution, "_refresh_predict_account_snapshot", None)
            ):
                # #93: keep the predict snapshot cache warm off the HTTP threads.
                self.predict_snapshot_refresher = PredictAccountSnapshotRefresher(
                    execution=self.execution
                )
                self.predict_snapshot_refresher.start()
            self._state = "RUNNING"
            logger.info(
                "prediction_runtime_state state=RUNNING mode=shadow pid=%s data_dir=%s",
                os.getpid(), self._data_dir,
            )
        except Exception:
            self._state = "FAILED"
            self._cleanup_resources()
            raise

    def _configure_n_leg_shadow(self) -> Callable[[Mapping[str, object], str], object]:
        if self.store is None or self.solver_server is None or self.monitor is None:
            raise RuntimeError("prediction Shadow requires the owned store, monitor, and solver server")
        scheduler = NLegShadowScheduler(
            self.store,
            submit_snapshot=NLegShadowClient(self.solver_server).submit,
        )
        self.n_leg_shadow = scheduler

        def observe(opportunity: Mapping[str, object], signal_id: str) -> str:
            return scheduler.schedule(signal_id, legacy_shadow_snapshot(opportunity, signal_id))

        set_shadow_observer = getattr(self.monitor, "set_shadow_observer", None)
        if callable(set_shadow_observer):
            set_shadow_observer(observe)
        return observe

    def stop(self) -> None:
        if self._state == "STOPPED":
            return
        if self._state == "NEW":
            self._state = "STOPPED"
            return
        self._state = "STOPPING"
        errors = self._cleanup_resources()
        if errors:
            self._state = "STOPPING"
            details = "; ".join(
                f"{type(error).__name__}: {error}" for error in errors
            )
            logger.error(
                "prediction_runtime_state state=STOPPING pid=%s data_dir=%s cleanup_errors=%s",
                os.getpid(),
                self._data_dir,
                details,
            )
            raise RuntimeError(
                f"prediction runtime cleanup failed: {details}"
            ) from errors[0]
        self._state = "STOPPED"

    def n_leg_solutions(self) -> list[dict[str, object]]:
        resolver = self.live_resolver
        return [] if resolver is None else resolver.solutions()

    def n_leg_metrics(self) -> dict[str, object]:
        driver = self.monitor_selection_driver
        if driver is None:
            return {}
        status = driver.status()
        return {
            "selection_pending": int(status.get("selection_pending", 0)),
            "selection_failures_consecutive": int(
                status.get("selection_failures_consecutive", 0)
            ),
        }

    def _cleanup_resources(self) -> list[BaseException]:
        errors: list[BaseException] = []
        uncertain_thread = False
        if self.monitor_selection_driver is not None:
            try:
                self.monitor_selection_driver.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.monitor_selection_driver = None
        if self.live_resolver is not None:
            try:
                self.live_resolver.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.live_resolver = None
        if self.predict_snapshot_refresher is not None:
            try:
                self.predict_snapshot_refresher.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.predict_snapshot_refresher = None
        if self._cross_runtime is not None:
            try:
                self._cross_runtime.stop()
            except BaseException as exc:
                errors.append(exc)
                uncertain_thread = True
            finally:
                if not self._cross_runtime.thread_alive:
                    self._cross_runtime = None
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except BaseException as exc:
                errors.append(exc)
                uncertain_thread = True
            else:
                monitor_thread = getattr(self.monitor, "_thread", None)
                if monitor_thread is not None and monitor_thread.is_alive():
                    errors.append(RuntimeError("prediction monitor thread did not stop"))
                    uncertain_thread = True
                else:
                    self.monitor = None
        if not uncertain_thread and self._shadow_guards is not None:
            try:
                self._shadow_guards.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._shadow_guards = None
        for resource in (
            ("n_leg_shadow", self.n_leg_shadow),
            ("solver_server", self.solver_server),
            ("execution", self.execution),
            ("_prediction_trading", self._prediction_trading),
            ("_predict_trading", self._predict_trading),
            ("store", self.store),
        ):
            name, value = resource
            close = getattr(value, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    setattr(self, name, None)
        if not uncertain_thread:
            self._owner.release()
        return errors


__all__ = [
    "PredictionRuntime",
    "PredictionRuntimeCompatibilityError",
    "PredictionRuntimeOwnershipError",
    "_CrossVenueRuntime",
    "_UnavailableCrossVenueMonitor",
    "_build_cross_venue_monitor",
    "_cross_venue_gamma_lookup",
]
