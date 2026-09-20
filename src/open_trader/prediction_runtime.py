from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from .notifications import NullNotifier
from .daily_premarket import send_notification_with_results
from .polymarket_monitor import PolymarketMonitor
from .polymarket_lp import (
    _LP_CANDIDATE_BATCH_MIN_INTERVAL_SECONDS,
    PolymarketLPService,
)
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
    N_LEG_READER_GENERATION,
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)
from .relation_auto_confirm import (
    RelationAutoConfirmRunner,
    load_auto_confirm_policy_file,
    run_relation_lifecycle,
)
from .prediction_live_resolver import PredictionLiveResolver
from .prediction_observation_monitor import PredictionObservationMonitor
from .prediction_monitor_selection import MonitorSelectionStore
from .prediction_monitor_selection_driver import PredictionMonitorSelectionDriver
from .prediction_n_leg_episodes import EpisodeStore, EpisodeTracker
from .prediction_n_leg_mode import ensure_same_event_same_venue_scope
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
_LP_TICK_SECONDS = 1.0
_LP_REWARD_SECONDS = 60.0
_LP_SHARE_WATCH_SECONDS = 10.0
_LP_HISTORY_SECONDS = 3600.0
_LP_BOOK_SAMPLE_SECONDS = 5.0
# Issue #146 D1: the LP dashboard snapshot refreshes on a fixed background
# cadence; the page's 5-second polling only reads the published snapshot.
_LP_DASHBOARD_SNAPSHOT_SECONDS = 10.0
# One in-flight request may consume the installed SDK's bounded connect/read/
# write/pool phases (5/10/10/2 seconds); this is a fixed cleanup grace, not a
# whole multi-request scan deadline.
_LP_REWARD_STOP_GRACE_SECONDS = 30.0
_LP_BOOK_SAMPLE_STOP_GRACE_SECONDS = 30.0
_N_LEG_PAUSED_ENV = "OPEN_TRADER_NLEG_PAUSED"

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
        n_leg_paused: bool | None = None,
        history_clock: Callable[[], datetime] | None = None,
        history_wait: Callable[[threading.Event, float], bool] | None = None,
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
        # Issue #60: the data dir seeds the reader fence at 1 and only the
        # startup probe (release-manifest runtimes) can observe a higher
        # fence; before start() the runtime is at fence-1 semantics.
        self._minimum_reader_generation = 1
        self._enable_n_leg_background = bool(enable_n_leg_background)
        self._history_clock = history_clock or (lambda: datetime.now(UTC))
        self._history_clock_injected = history_clock is not None
        self._history_waiter = history_wait
        if n_leg_paused is None:
            n_leg_paused = self._parse_n_leg_paused(os.environ.get(_N_LEG_PAUSED_ENV))
        elif type(n_leg_paused) is not bool:
            raise ValueError("n_leg_paused must be a boolean")
        self._n_leg_paused = n_leg_paused
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
        self.lp: PolymarketLPService | None = None
        self.observation_monitor: PredictionObservationMonitor | None = None
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
        self._lp_stop_event = threading.Event()
        self._lp_thread: threading.Thread | None = None
        self._book_sample_stop_event = threading.Event()
        self._book_sampler_thread: threading.Thread | None = None
        self._history_stop_event = threading.Event()
        self._history_wakeup_event = threading.Event()
        self._history_initial_done = threading.Event()
        self._history_thread: threading.Thread | None = None
        self._reward_stop_event = threading.Event()
        self._lp_share_stop_event = threading.Event()
        self._lp_candidate_refresh_requested = threading.Event()
        # Issue #146: wakes the maintenance monitor when a scan round
        # publishes so its wait re-anchors on the fresh snapshot.
        self._candidate_maintenance_wakeup = threading.Event()
        self._candidate_scan_thread: threading.Thread | None = None
        self._candidate_maintenance_thread: threading.Thread | None = None
        self._lp_dashboard_thread: threading.Thread | None = None
        self._reward_thread: threading.Thread | None = None
        self._lp_share_thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def mode(self) -> Literal["production", "shadow"]:
        return self._mode

    @property
    def legacy_retired(self) -> bool:
        """Issue #60: legacy strategy surface is retired at the N_LEG fence."""

        return self._minimum_reader_generation >= N_LEG_READER_GENERATION

    @property
    def production_owner(self) -> bool:
        return self._mode == "production" and self._owner.held

    @staticmethod
    def _parse_n_leg_paused(value: str | None) -> bool:
        if value in (None, "", "0"):
            return False
        if value == "1":
            return True
        raise ValueError(f"{_N_LEG_PAUSED_ENV} must be 0 or 1")

    @property
    def n_leg_paused(self) -> bool:
        return self._n_leg_paused

    def recover_lp_preparation(self) -> dict[str, object]:
        """Explicitly re-arm a paused LP preparation task and wake its worker."""

        lp = self.lp
        recover = getattr(lp, "recover_preparation", None) if lp is not None else None
        if not callable(recover):
            return {"state": "unknown", "reason": "lp_unavailable"}
        result = recover()
        self._history_wakeup_event.set()
        self._lp_candidate_refresh_requested.set()
        return result if isinstance(result, Mapping) else {"state": "unknown"}

    def queue_lp_candidate_refresh(self) -> bool:
        """Wake the owned read-only LP candidate refresh worker."""

        thread = self._candidate_scan_thread
        if (
            self._mode != "production"
            or self._state != "RUNNING"
            or thread is None
            or not thread.is_alive()
        ):
            return False
        self._lp_candidate_refresh_requested.set()
        return True

    @property
    def shadow_evidence(self) -> dict[str, object]:
        def counters(validator: object | None) -> dict[str, int]:
            # The validators expose llm_calls/llm_successes; the previous
            # codex_* names never existed, so healthz always reported zeros.
            return {
                "calls": int(getattr(validator, "llm_calls", 0)),
                "successes": int(getattr(validator, "llm_successes", 0)),
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

    def _wire_relation_lifecycle(self) -> None:
        """Hook the #96 governance pass onto the monitor's full-scan boundary.

        The policy lives next to the prediction config; a missing file means
        the feature is off and no runner is exposed (the HTTP round endpoint
        then reports unavailability instead of pretending to run zero tiers).
        A file that parses to at least one tier entry always instantiates the
        runner — even when zero tiers are enabled or every tier failed its own
        validation — so round reports surface ``configuration_errors`` and an
        operator typo can never silently disable auto-confirm. The catalog
        object is owned by this process only — governance writes go through
        it, never around it.
        """
        policy_path = self._prediction_config_path.parent / "relation_auto_confirm.json"
        policy = load_auto_confirm_policy_file(policy_path)
        if not policy.tiers:
            return
        assert self.relation_catalog is not None
        assert self.monitor is not None
        git_sha = self._git_sha
        runner = RelationAutoConfirmRunner(
            self.relation_catalog,
            policy=policy,
            notifier=self._notifier,
        )
        self.relation_auto_confirm_runner = runner

        def observe() -> dict[str, object]:
            return run_relation_lifecycle(
                self.relation_catalog,
                runner,
                actor_expire="lifecycle:expire",
                git_sha=git_sha,
            )

        self.monitor.set_relation_lifecycle_observer(observe)

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
                # Issue #60: remember the fence once, under the owner lock;
                # this single read drives both the compatibility check and
                # legacy retirement.
                self._minimum_reader_generation = minimum_reader_generation
                if self._reader_generation < minimum_reader_generation:
                    raise PredictionRuntimeCompatibilityError(
                        f"prediction reader generation {self._reader_generation} "
                        f"is below required {minimum_reader_generation}"
                    )
            if not self._n_leg_paused:
                self.solver_server = self._solver_server_factory()
            self.store = PredictionArbitrageStore(self._data_dir)
            # #104: idempotent startup seed; failures are logged inside and
            # never block startup.
            if not self._n_leg_paused and ensure_same_event_same_venue_scope(self.store):
                logger.info(
                    "prediction_n_leg_scope_seed scope=SAME_EVENT_SAME_VENUE pid=%s",
                    os.getpid(),
                )
            if not self._n_leg_paused:
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
            # Issue #137: persist the LP metadata TTL cache across restarts.
            # Attached duck-typed (not via from_keychain) so the many test
            # doubles that replace the client keep working unchanged.
            attach_metadata_cache = getattr(
                self._prediction_trading, "attach_metadata_cache", None
            )
            if callable(attach_metadata_cache):
                attach_metadata_cache(self.store)
            self.lp = PolymarketLPService(
                self.store,
                self._prediction_trading,
                owner_lock=self._owner,
            )
            if self._history_clock_injected:
                # Keep the production constructor seam compatible with the
                # existing test doubles while allowing the history scheduler
                # and LP reader to share one injected boundary clock.
                setattr(self.lp, "clock", self._history_clock)
            if not self._n_leg_paused:
                try:
                    self._predict_trading = PredictTradingClient.from_keychain(
                        trading_config
                    )
                except Exception:
                    self._predict_trading = None
            relation_validator = (
                LlmRelationValidator(self.store) if not self._n_leg_paused else None
            )
            title_translator = (
                LlmTitleTranslator(self.store) if not self._n_leg_paused else None
            )
            self.monitor = PolymarketMonitor(
                store=self.store,
                trading=self._prediction_trading,
                relation_discovery=(
                    discover_threshold_relation_catalog
                    if not self._n_leg_paused
                    else None
                ),
                relation_validator=relation_validator,
                title_translator=title_translator,
                relation_catalog=self.relation_catalog,
            )
            if not self._n_leg_paused:
                self.observation_monitor = PredictionObservationMonitor(
                    catalog=self.relation_catalog,
                    store=self.store,
                    monitor=self.monitor,
                )
                self._wire_relation_lifecycle()
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
                legacy_retired=self.legacy_retired,
                lp=self.lp,
            )
            setattr(self.execution, "_n_leg_paused", self._n_leg_paused)
            set_mutation_guard = getattr(self.lp, "set_mutation_guard", None)
            lp_mutation_allowed = getattr(self.execution, "lp_mutation_allowed", None)
            if callable(set_mutation_guard) and callable(lp_mutation_allowed):
                set_mutation_guard(lp_mutation_allowed)
            # Issue 152: wire the LP queue-protection notifier to the
            # execution notification capability (feishu via the execution
            # delivery path, xiaoai as the second hop in the same callback).
            set_protection_notifier = getattr(self.lp, "set_protection_notifier", None)
            if callable(set_protection_notifier):
                def _deliver_lp_protection_notification(
                    title: str, message: str, xiaoai_text: str
                ) -> None:
                    deliver = getattr(
                        self.execution, "_deliver_feishu_notification", None
                    )
                    if callable(deliver):
                        try:
                            deliver(title, message)
                        except Exception:
                            logger.warning(
                                "lp_protection_feishu_delivery_failed",
                                exc_info=True,
                            )
                    notifier = getattr(self, "_notifier", None)
                    if notifier is None:
                        return
                    try:
                        send_notification_with_results(
                            notifier, title, xiaoai_text, channels={"xiaoai"}
                        )
                    except Exception:
                        logger.warning(
                            "lp_protection_xiaoai_delivery_failed", exc_info=True
                        )

                set_protection_notifier(_deliver_lp_protection_notification)
            if not self._n_leg_paused and not self.legacy_retired:
                # Issue #109: legacy ready/observation alerts retire with the
                # legacy engine at the N_LEG fence; the monitor keeps both
                # channels silent while no observer is set.
                self.monitor.set_ready_observer(
                    self.execution.notify_ready_opportunity
                )
                self.monitor.set_observation_observer(
                    self.execution.notify_observation
                )
                # Issue #60: the legacy auto-eat path must never arm once the
                # reader fence has reached the N_LEG generation.
                self.monitor.set_auto_eat_observer(
                    self.execution.auto_eat_threshold
                )
            if not self._n_leg_paused:
                self.monitor.set_failure_observer(
                    self.execution.notify_monitor_failure
                )
            shadow_observer = (
                self._configure_n_leg_shadow() if not self._n_leg_paused else None
            )
            cross_monitor = (
                self._injected_cross_venue_monitor
                if not self._n_leg_paused
                else _UnavailableCrossVenueMonitor("n_leg_paused")
            )
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
            if not self._n_leg_paused:
                self.monitor.start()
            if self.observation_monitor is not None and not self._n_leg_paused:
                self.observation_monitor.start()
            if self._cross_runtime is not None and not self._n_leg_paused:
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
            if not self._n_leg_paused and self._predict_trading is not None and callable(
                getattr(self.execution, "_refresh_predict_account_snapshot", None)
            ):
                # #93: keep the predict snapshot cache warm off the HTTP threads.
                self.predict_snapshot_refresher = PredictAccountSnapshotRefresher(
                    execution=self.execution
                )
                self.predict_snapshot_refresher.start()
            if self._enable_n_leg_background and not self._n_leg_paused:
                selection_store = MonitorSelectionStore(self._data_dir)
                selection_lock = threading.RLock()
                # #106: the episode store owns the shared SQLite tables; the
                # tracker resumes open episodes across restarts.
                episode_store = EpisodeStore(self._data_dir)
                episode_tracker = EpisodeTracker(store=episode_store)
                episode_tracker.load_open()
                self.live_resolver = PredictionLiveResolver(
                    data_dir=self._data_dir,
                    relation_catalog=self.relation_catalog,
                    monitor=self.monitor,
                    solver_server=self.solver_server,
                    selection_store=selection_store,
                    selection_lock=selection_lock,
                    store=self.store,
                    execution=self.execution,
                    episode_tracker=episode_tracker,
                )
                self.live_resolver.start()
                self.monitor_selection_driver = PredictionMonitorSelectionDriver(
                    relation_catalog=self.relation_catalog,
                    selection_store=selection_store,
                    selection_lock=selection_lock,
                    idle_check=self.live_resolver.is_idle,
                )
                self.monitor_selection_driver.start()
                # Issue #64: the manual-confirm FIFO queue driver. It is a
                # no-op until a confirmed queue row exists; the source
                # factory fails closed until the resolver retains the frozen
                # solve inputs needed for faithful re-decode.
                from .prediction_n_leg_driver import (
                    NLegOrderQueueDriver,
                    trading_reconciliation_context_factory,
                )

                _queue_resolver = self.live_resolver

                def _queue_books_provider(component_id: str):
                    accessor = getattr(
                        _queue_resolver, "driver_books_snapshot", None
                    )
                    return (
                        accessor(component_id) if callable(accessor) else None
                    )

                def _n_leg_source_factory(frozen):
                    # Issue #64: the resolver retains each frozen solution's
                    # heavy #51 source material; the factory hands it to the
                    # admission unchanged. N_LEG_SOURCE_UNAVAILABLE remains
                    # only as the defensive fail-closed for missing material
                    # (component rotated out / process restarted), never the
                    # normal path.
                    accessor = getattr(
                        _queue_resolver, "driver_execution_source", None
                    )
                    material = (
                        accessor(str(frozen.get("component_id") or ""))
                        if callable(accessor)
                        else None
                    )
                    if material is None:
                        raise ValueError("N_LEG_SOURCE_UNAVAILABLE")
                    return material["source"]

                # Review round 2 (issue #64 P1): production reconciliation —
                # without a factory an all-filled batch could never complete
                # and the queue wedged silently on the first successful order.
                # The factory builds the ReconciliationContext from fresh
                # venue reads (the trading client's account snapshot).
                _trading_client = getattr(self, "_prediction_trading", None)
                reconciliation_factory = (
                    trading_reconciliation_context_factory(
                        self.store, _trading_client
                    )
                    if _trading_client is not None
                    else None
                )
                self.n_leg_order_queue_driver = NLegOrderQueueDriver(
                    self.store,
                    books_provider=_queue_books_provider,
                    source_factory=_n_leg_source_factory,
                    trading=_trading_client,
                    reconciliation_context_factory=reconciliation_factory,
                )
                self.n_leg_order_queue_driver.start()
            self._start_lp_monitor()
            self._start_history_monitor()
            self._start_candidate_scan_monitor()
            self._start_candidate_maintenance_monitor()
            self._start_lp_dashboard_monitor()
            self._start_reward_monitor()
            self._start_lp_share_watch()
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

    def _start_lp_monitor(self) -> None:
        """Keep one active LP session reconciled by the owned runtime."""

        if self.lp is None or self.execution is None or self._lp_thread is not None:
            return
        self._lp_stop_event.clear()

        def run() -> None:
            while not self._lp_stop_event.wait(_LP_TICK_SECONDS):
                execution = self.execution
                if execution is None:
                    return
                lp_tick = getattr(execution, "lp_tick", None)
                if not callable(lp_tick):
                    return
                try:
                    status = lp_tick()
                except Exception:
                    # The durable session remains active and visible; the
                    # next iteration retries through the same reconciliation
                    # and idempotency path.
                    logger.exception("prediction_lp_tick_failed")
                    continue
                generate_report = getattr(self.lp, "generate_due_report", None)
                state = str(status.get("state") or "") if isinstance(status, Mapping) else ""
                cancellation_pending = (
                    state == "needs_attention"
                    and isinstance(status, Mapping)
                    and status.get("review_status") == "awaiting_reconciliation"
                    and str(status.get("reconciliation") or "").startswith("deadline_cancel_")
                )
                reconciliation_ready = state == "none" or (
                    isinstance(status, Mapping)
                    and (
                        state not in {"busy", "needs_attention", "error", "failed"}
                        or cancellation_pending
                    )
                    and status.get("account_checked_at") is not None
                    and status.get("book_checked_at") is not None
                )
                if callable(generate_report) and reconciliation_ready:
                    try:
                        generate_report()
                    except Exception:
                        logger.exception("prediction_lp_daily_report_failed")

        self._lp_thread = threading.Thread(
            target=run,
            name="prediction-lp-monitor",
            daemon=True,
        )
        self._lp_thread.start()

    def _start_reward_monitor(self) -> None:
        """Refresh platform LP earnings without sharing the risk-loop thread."""

        if self.lp is None or self._reward_thread is not None:
            return
        self._reward_stop_event.clear()

        def run() -> None:
            while not self._reward_stop_event.is_set():
                lp = self.lp
                if lp is None:
                    return
                refresh_rewards = getattr(lp, "refresh_rewards", None)
                if not callable(refresh_rewards):
                    return
                try:
                    refresh_rewards(stop_event=self._reward_stop_event)
                except Exception:
                    # Earnings are read-only and advisory; a failed refresh
                    # is recorded by the service without touching LP risk.
                    logger.exception("prediction_lp_reward_refresh_failed")
                if self._reward_stop_event.is_set():
                    return
                execution = self.execution
                refresh_observations = getattr(
                    execution, "refresh_lp_observations", None
                )
                if callable(refresh_observations):
                    try:
                        refresh_observations(stop_event=self._reward_stop_event)
                    except Exception:
                        logger.exception("prediction_lp_observation_refresh_failed")
                if self._reward_stop_event.is_set():
                    return
                if self._reward_stop_event.wait(_LP_REWARD_SECONDS):
                    return

        self._reward_thread = threading.Thread(
            target=run,
            name="prediction-lp-reward-monitor",
            daemon=True,
        )
        self._reward_thread.start()

    def _start_lp_share_watch(self) -> None:
        """Refresh selected LP share watches on an independent short loop."""

        if self.execution is None or self._lp_share_thread is not None:
            return
        self._lp_share_stop_event.clear()

        def run() -> None:
            while not self._lp_share_stop_event.is_set():
                execution = self.execution
                if execution is None:
                    return
                refresh_watch = getattr(execution, "refresh_lp_share_watch", None)
                if not callable(refresh_watch):
                    return
                try:
                    refresh_watch(stop_event=self._lp_share_stop_event)
                except Exception:
                    logger.exception("prediction_lp_share_watch_refresh_failed")
                if self._lp_share_stop_event.wait(_LP_SHARE_WATCH_SECONDS):
                    return

        self._lp_share_thread = threading.Thread(
            target=run,
            name="prediction-lp-share-watch",
            daemon=True,
        )
        self._lp_share_thread.start()

    def _start_candidate_scan_monitor(self) -> None:
        """Explore the candidate queue continuously on its own thread (#157).

        Each wake processes one rolling exploration batch; the wait is at
        least the two-second batch floor and never leaves the [1, 300]
        scheduler band. A manual page refresh wakes the loop for the next
        batch immediately.
        """

        if self.lp is None or self._candidate_scan_thread is not None:
            return
        self._reward_stop_event.clear()
        self._lp_candidate_refresh_requested.clear()

        def run() -> None:
            while not self._reward_stop_event.is_set():
                lp = self.lp
                if lp is None:
                    return
                refresh_candidates = getattr(lp, "refresh_candidates", None)
                if not callable(refresh_candidates):
                    return
                scan_result: Mapping[str, object] | None = None
                try:
                    scan_result = refresh_candidates(
                        stop_event=self._reward_stop_event,
                        force=True,
                    )
                except Exception:
                    logger.exception("prediction_lp_candidate_refresh_failed")
                if self._reward_stop_event.is_set():
                    return
                # A fresh publication re-anchors the maintenance monitor on
                # the newly refreshed pool rows.
                self._candidate_maintenance_wakeup.set()
                wait_seconds = float(_LP_CANDIDATE_BATCH_MIN_INTERVAL_SECONDS)
                if isinstance(scan_result, Mapping):
                    next_wait = scan_result.get("next_batch_wait_seconds")
                    if isinstance(next_wait, (int, float)):
                        wait_seconds = max(
                            float(next_wait),
                            float(_LP_CANDIDATE_BATCH_MIN_INTERVAL_SECONDS),
                        )
                wait_seconds = min(max(wait_seconds, 1.0), 300.0)
                refresh_requested = self._lp_candidate_refresh_requested.wait(
                    wait_seconds
                )
                if self._reward_stop_event.is_set():
                    return
                if refresh_requested:
                    self._lp_candidate_refresh_requested.clear()

        self._candidate_scan_thread = threading.Thread(
            target=run,
            name="prediction-lp-candidate-scan-monitor",
            daemon=True,
        )
        self._candidate_scan_thread.start()

    def _start_candidate_maintenance_monitor(self) -> None:
        """Maintain the published recommendation head on its own thread.

        The wait comes from ``candidate_maintenance_wait_seconds`` (the
        30-second source lead or the failure backoff, clamped to
        [1.0, 300.0]); a scan publication wakes the loop early.
        """

        if self.lp is None or self._candidate_maintenance_thread is not None:
            return
        self._candidate_maintenance_wakeup.clear()

        def run() -> None:
            while not self._reward_stop_event.is_set():
                lp = self.lp
                if lp is None:
                    return
                refresh_recommendations = getattr(
                    lp, "refresh_candidate_recommendations", None
                )
                if callable(refresh_recommendations):
                    try:
                        refresh_recommendations(
                            stop_event=self._reward_stop_event
                        )
                    except Exception:
                        logger.exception(
                            "prediction_lp_candidate_recommendation_refresh_failed"
                        )
                if self._reward_stop_event.is_set():
                    return
                wait_seconds = 30.0
                next_deadline = getattr(
                    lp, "candidate_maintenance_wait_seconds", None
                )
                if callable(next_deadline):
                    try:
                        candidate_wait = next_deadline()
                    except Exception:
                        candidate_wait = None
                        logger.exception(
                            "prediction_lp_candidate_deadline_failed"
                        )
                    if isinstance(candidate_wait, (int, float)):
                        wait_seconds = min(
                            max(float(candidate_wait), 1.0), 300.0
                        )
                if self._candidate_maintenance_wakeup.wait(wait_seconds):
                    self._candidate_maintenance_wakeup.clear()

        self._candidate_maintenance_thread = threading.Thread(
            target=run,
            name="prediction-lp-candidate-maintenance-monitor",
            daemon=True,
        )
        self._candidate_maintenance_thread.start()

    def _start_lp_dashboard_monitor(self) -> None:
        """Publish the LP dashboard snapshot the page endpoint serves."""

        if self.execution is None or self._lp_dashboard_thread is not None:
            return

        def run() -> None:
            execution = self.execution
            if execution is None:
                return
            refresh_snapshot = getattr(
                execution, "refresh_lp_dashboard_snapshot", None
            )
            if not callable(refresh_snapshot):
                return
            while not self._reward_stop_event.is_set():
                try:
                    refresh_snapshot()
                except Exception:
                    logger.exception("prediction_lp_dashboard_refresh_failed")
                if self._reward_stop_event.wait(_LP_DASHBOARD_SNAPSHOT_SECONDS):
                    return

        self._lp_dashboard_thread = threading.Thread(
            target=run,
            name="prediction-lp-dashboard-snapshot-monitor",
            daemon=True,
        )
        self._lp_dashboard_thread.start()

    def _start_history_monitor(self) -> None:
        """Refresh the bounded LP price-history cache hourly."""

        if self.lp is None or self._history_thread is not None:
            return
        self._history_stop_event.clear()
        self._history_initial_done.clear()
        self._history_wakeup_event.clear()

        def wait_for_history(seconds: float) -> bool:
            if self._history_waiter is not None:
                return bool(self._history_waiter(self._history_stop_event, seconds))
            deadline = time.monotonic() + max(0.0, seconds)
            while not self._history_stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if self._history_wakeup_event.wait(min(remaining, 0.5)):
                    self._history_wakeup_event.clear()
                    return False
            return True

        def preparation_alert(result: Mapping[str, object]) -> None:
            fault_pending = result.get("alert_pending") is True
            recovery_pending = result.get("recovery_alert_pending") is True
            if not fault_pending and not recovery_pending:
                return
            lp = self.lp
            execution = self.execution
            if lp is None or execution is None:
                return
            preparation = result.get("preparation")
            if not isinstance(preparation, Mapping):
                return
            generation = preparation.get("generation")
            if type(generation) is not int:
                return
            if fault_pending:
                notifier = getattr(execution, "notify_lp_preparation_failure", None)
                success = False
                if callable(notifier):
                    try:
                        value = notifier(preparation)
                        success = isinstance(value, Mapping) and value.get("state") == "sent"
                    except Exception:
                        logger.exception("prediction_lp_preparation_notification_failed")
                finish = getattr(lp, "finish_preparation_alert", None)
                if callable(finish):
                    try:
                        finish(generation=generation, success=success)
                    except Exception:
                        logger.exception("prediction_lp_preparation_notification_state_failed")
            if recovery_pending:
                notifier = getattr(execution, "notify_lp_preparation_recovery", None)
                success = False
                if callable(notifier):
                    try:
                        value = notifier(preparation)
                        success = isinstance(value, Mapping) and value.get("state") == "sent"
                    except Exception:
                        logger.exception("prediction_lp_preparation_recovery_notification_failed")
                finish = getattr(lp, "finish_preparation_recovery", None)
                if callable(finish):
                    try:
                        finish(generation=generation, success=success)
                    except Exception:
                        logger.exception("prediction_lp_preparation_recovery_state_failed")

        def retry_delay(result: Mapping[str, object]) -> float:
            preparation = result.get("preparation")
            if not isinstance(preparation, Mapping):
                return _LP_HISTORY_SECONDS
            now = self._history_clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)

            def parse_deadline(value: object) -> datetime | None:
                if isinstance(value, datetime):
                    due = value
                elif isinstance(value, str):
                    text = value[:-1] + "+00:00" if value.endswith("Z") else value
                    try:
                        due = datetime.fromisoformat(text)
                    except ValueError:
                        return None
                else:
                    return None
                if due.tzinfo is None:
                    due = due.replace(tzinfo=UTC)
                return due.astimezone(UTC)

            deadlines: list[datetime] = []
            for key in ("next_retry_at", "next_probe_at"):
                due = parse_deadline(preparation.get(key))
                if due is not None:
                    deadlines.append(due)
            # A failed or not-yet-delivered incident has its own durable
            # deadline. Include it so delivery retries do not wait behind a
            # later full preparation retry; a sent/claimed episode has no
            # independent wake-up requirement.
            if preparation.get("fault_alert_state") not in {"sent", "claimed"}:
                due = parse_deadline(preparation.get("fault_alert_next_at"))
                if due is not None:
                    deadlines.append(due)
            if preparation.get("recovery_alert_state") == "failed":
                due = parse_deadline(preparation.get("recovery_alert_next_at"))
                if due is not None:
                    deadlines.append(due)
            if not deadlines:
                return _LP_HISTORY_SECONDS
            seconds = min(
                (due - now.astimezone(UTC)).total_seconds() for due in deadlines
            )
            # A persisted deadline can already be due after a process pause;
            # keep the loop bounded without spinning at zero seconds.
            return max(1.0, seconds)

        def run() -> None:
            try:
                while not self._history_stop_event.is_set():
                    lp = self.lp
                    if lp is None:
                        return
                    refresh_history = getattr(lp, "refresh_price_history", None)
                    if not callable(refresh_history):
                        return
                    result: object = None
                    claim_recovery = getattr(
                        lp, "claim_due_preparation_recovery_alert", None
                    )
                    if callable(claim_recovery):
                        try:
                            result = claim_recovery()
                        except Exception:
                            result = None
                            logger.exception(
                                "prediction_lp_recovery_claim_failed"
                            )
                    business_refresh_completed = False
                    if not isinstance(result, Mapping):
                        try:
                            result = refresh_history(
                                stop_event=self._history_stop_event
                            )
                            business_refresh_completed = True
                        except Exception:
                            result = None
                            logger.exception("prediction_lp_history_refresh_failed")
                    self._history_initial_done.set()
                    if business_refresh_completed:
                        self._lp_candidate_refresh_requested.set()
                    if isinstance(result, Mapping):
                        preparation_alert(result)
                        # Delivery state is persisted by the alert finisher;
                        # use that post-delivery snapshot when selecting the
                        # next wake-up instead of the claimed pre-send copy.
                        refreshed_preparation = getattr(
                            lp, "preparation_snapshot", None
                        )
                        if (
                            callable(refreshed_preparation)
                            and (
                                result.get("alert_pending") is True
                                or result.get("recovery_alert_pending") is True
                            )
                        ):
                            try:
                                current_preparation = refreshed_preparation()
                            except Exception:
                                current_preparation = None
                            if isinstance(current_preparation, Mapping):
                                result = {
                                    **result,
                                    "preparation": current_preparation,
                                }
                        outcome = str(result.get("preparation_outcome") or "")
                        preparation = result.get("preparation")
                        preparation_state = (
                            str(preparation.get("state") or "")
                            if isinstance(preparation, Mapping)
                            else ""
                        )
                        if outcome == "waiting_retry" or (
                            outcome == "failure"
                            and preparation_state in {"waiting_retry", "partial"}
                        ):
                            wait_seconds = retry_delay(result)
                        elif outcome == "paused":
                            wait_seconds = _LP_HISTORY_SECONDS
                        else:
                            # A successful data read can still leave a
                            # failed recovery notification due. Reuse the
                            # same deadline selector so delivery retries do
                            # not sleep behind the hourly refresh interval.
                            wait_seconds = retry_delay(result)
                    else:
                        wait_seconds = _LP_HISTORY_SECONDS
                    if wait_for_history(wait_seconds):
                        return
            finally:
                self._history_initial_done.set()

        self._history_thread = threading.Thread(
            target=run,
            name="prediction-lp-history-monitor",
            daemon=True,
        )
        self._history_thread.start()

    def _start_book_sampler(self) -> None:
        """Sample the published LP observation set on its own bounded loop."""

        if self.lp is None or self._book_sampler_thread is not None:
            return
        self._book_sample_stop_event.clear()

        def run() -> None:
            while not self._book_sample_stop_event.is_set():
                started = time.monotonic()
                lp = self.lp
                if lp is None:
                    return
                sample_books = getattr(lp, "sample_candidate_books", None)
                if not callable(sample_books):
                    return
                try:
                    sample_books(stop_event=self._book_sample_stop_event)
                except Exception:
                    logger.exception("prediction_lp_book_sample_failed")
                remaining = max(
                    0.0,
                    _LP_BOOK_SAMPLE_SECONDS - (time.monotonic() - started),
                )
                if self._book_sample_stop_event.wait(remaining):
                    return

        self._book_sampler_thread = threading.Thread(
            target=run,
            name="prediction-lp-book-sampler",
            daemon=True,
        )
        self._book_sampler_thread.start()

    def _start_shadow(self) -> None:
        try:
            self._owner.acquire()
            if self._n_leg_paused:
                self.store = PredictionArbitrageStore(self._data_dir)
                self._state = "RUNNING"
                logger.info(
                    "prediction_runtime_state state=RUNNING mode=shadow n_leg_paused=true pid=%s data_dir=%s",
                    os.getpid(),
                    self._data_dir,
                )
                return
            self.solver_server = self._solver_server_factory()
            self.store = PredictionArbitrageStore(self._data_dir)
            trading_config = load_trading_config(self._prediction_config_path)
            self._prediction_trading = PolymarketTradingClient.from_keychain(
                trading_config
            )
            # Issue #137: shadow mode keeps the same warm LP metadata cache.
            attach_metadata_cache = getattr(
                self._prediction_trading, "attach_metadata_cache", None
            )
            if callable(attach_metadata_cache):
                attach_metadata_cache(self.store)
            # Keep the durable LP read model available in shadow mode.  The
            # service rejects every LP POST before dispatch, and shadow never
            # starts the LP monitor, so this collaborator is read-only in
            # practice while persisted residual risk remains visible after a
            # mode switch or restart.
            self.lp = PolymarketLPService(
                self.store,
                self._prediction_trading,
                owner_lock=self._owner,
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
            self.observation_monitor = PredictionObservationMonitor(
                catalog={},
                store=self.store,
                monitor=self.monitor,
            )
            self.execution = PredictionExecutionService(
                store=self.store,
                monitor=self.monitor,
                trading=self._prediction_trading,
                notifier=NullNotifier(),
                lock_path=self._data_dir / "prediction_arbitrage" / "execution.lock",
                dashboard_url=self._dashboard_url,
                predict_trading=self._predict_trading,
                lp=self.lp,
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
            if self.observation_monitor is not None:
                self.observation_monitor.start()
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

    def observation_snapshot(self) -> dict[str, object] | None:
        monitor = self.observation_monitor
        if monitor is None:
            return None
        return monitor.snapshot()

    def n_leg_execution_source(
        self, component_id: str
    ) -> dict[str, object] | None:
        """Issue #64: the resolver's retained admission-grade execution
        material for one component (heavy #51 source + payloads + bound
        proof), or None when unavailable — the confirm endpoint's freeze
        source."""
        resolver = self.live_resolver
        accessor = getattr(resolver, "driver_execution_source", None)
        if not callable(accessor):
            return None
        try:
            return accessor(component_id)
        except Exception:
            logger.exception(
                "n_leg_execution_source build failed component=%s",
                component_id,
            )
            return None

    def n_leg_episodes(self) -> dict[str, dict[str, object]]:
        resolver = self.live_resolver
        return {} if resolver is None else resolver.n_leg_episodes()

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
        self._reward_stop_event.set()
        self._lp_share_stop_event.set()
        self._history_stop_event.set()
        self._history_wakeup_event.set()
        self._history_initial_done.set()
        self._lp_candidate_refresh_requested.set()
        self._candidate_maintenance_wakeup.set()
        self._lp_stop_event.set()
        self._book_sample_stop_event.set()
        history_thread = self._history_thread
        if history_thread is not None:
            history_thread.join(timeout=_LP_REWARD_STOP_GRACE_SECONDS)
            if history_thread.is_alive():
                errors.append(RuntimeError("prediction LP history monitor thread did not stop"))
                uncertain_thread = True
                # The reader may still be using the shared LP, trading, and
                # store collaborators. Preserve ownership until a later stop
                # call can join it after the external read returns.
            else:
                self._history_thread = None
        for attr, label in (
            ("_candidate_scan_thread", "candidate scan monitor"),
            ("_candidate_maintenance_thread", "candidate maintenance monitor"),
            ("_lp_dashboard_thread", "LP dashboard snapshot monitor"),
        ):
            thread = getattr(self, attr)
            if thread is not None:
                thread.join(timeout=_LP_REWARD_STOP_GRACE_SECONDS)
                if thread.is_alive():
                    errors.append(
                        RuntimeError(
                            f"prediction LP {label} thread did not stop"
                        )
                    )
                    # Keep the live LP/trading/store collaborators and owner
                    # while the reader may still use them; a later stop can
                    # retry cleanup.
                    uncertain_thread = True
                else:
                    setattr(self, attr, None)
        reward_thread = self._reward_thread
        if reward_thread is not None:
            # Cooperative cancellation leaves at most one bounded SDK request
            # in flight. Wait the fixed per-request grace, rather than
            # treating one HTTP read timeout as a whole-scan wall bound.
            reward_thread.join(timeout=_LP_REWARD_STOP_GRACE_SECONDS)
            if reward_thread.is_alive():
                errors.append(RuntimeError("prediction LP reward monitor thread did not stop"))
                # Keep the live LP/trading/store collaborators and owner while
                # the reader may still use them; a later stop can retry cleanup.
                uncertain_thread = True
            else:
                self._reward_thread = None
        lp_share_thread = self._lp_share_thread
        if lp_share_thread is not None:
            lp_share_thread.join(timeout=_LP_REWARD_STOP_GRACE_SECONDS)
            if lp_share_thread.is_alive():
                errors.append(RuntimeError("prediction LP share watch thread did not stop"))
                uncertain_thread = True
            else:
                self._lp_share_thread = None
        lp_thread = self._lp_thread
        if lp_thread is not None:
            lp_thread.join(timeout=5)
            if lp_thread.is_alive():
                errors.append(RuntimeError("prediction LP monitor thread did not stop"))
                uncertain_thread = True
            else:
                self._lp_thread = None
        book_sampler_thread = self._book_sampler_thread
        if book_sampler_thread is not None:
            book_sampler_thread.join(timeout=_LP_BOOK_SAMPLE_STOP_GRACE_SECONDS)
            if book_sampler_thread.is_alive():
                errors.append(RuntimeError("book sampler thread did not stop"))
                # It may still hold a client response or write a sample. Keep
                # the shared LP, trading, store and runtime owner until a later
                # stop call can join it after the read returns.
                return errors
            self._book_sampler_thread = None
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
        driver = getattr(self, "n_leg_order_queue_driver", None)
        if driver is not None:
            try:
                driver.stop()
            except Exception:
                pass
            self.n_leg_order_queue_driver = None
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
        if self.observation_monitor is not None:
            try:
                self.observation_monitor.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.observation_monitor = None
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
        for attr in (
            "_candidate_scan_thread",
            "_candidate_maintenance_thread",
            "_lp_dashboard_thread",
        ):
            thread = getattr(self, attr)
            if thread is not None and thread.is_alive():
                return errors
            setattr(self, attr, None)
        if reward_thread is not None:
            if reward_thread.is_alive():
                return errors
            self._reward_thread = None
        if lp_share_thread is not None:
            if lp_share_thread.is_alive():
                return errors
            self._lp_share_thread = None
        if history_thread is not None:
            if history_thread.is_alive():
                return errors
            self._history_thread = None
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
            ("lp", self.lp),
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
