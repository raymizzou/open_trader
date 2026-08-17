"""Issue #87: monitor-selection driver state machine, retention, and idle gate."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.prediction_live_resolver import (
    PredictionLiveResolver,
    _OutcomeTrackingServer,
)
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_monitor_selection_driver import (
    PredictionMonitorSelectionDriver,
)
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)


AS_OF = datetime(2026, 8, 16, tzinfo=UTC)


class FakeCatalog:
    def __init__(
        self,
        rows: dict[str, object],
        *,
        generation: int = 1,
        fingerprint: str = "fp-1",
    ) -> None:
        self.rows = rows
        self.generation = generation
        self.fingerprint = fingerprint

    def current_generation(self) -> dict[str, object]:
        return dict(self.rows)

    def generation_meta(self) -> dict[str, object]:
        return {"generation": self.generation, "fingerprint": self.fingerprint}


def driver(
    tmp_path: Path,
    catalog: FakeCatalog,
    *,
    idle: bool = True,
    poll_interval: float = 0.01,
) -> PredictionMonitorSelectionDriver:
    return PredictionMonitorSelectionDriver(
        relation_catalog=catalog,
        selection_store=MonitorSelectionStore(tmp_path),
        idle_check=lambda: idle,
        poll_interval=poll_interval,
    )


def observation(contract_id: str) -> SettlementObservationKey:
    return SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        f"oracle-{contract_id}",
        f"indicator-{contract_id}",
        AS_OF,
        AS_OF + timedelta(hours=1),
        "UTC",
        "v1",
    )


def raw_action(action_id: str, contract_id: str, side: ActionSide) -> CandidateAction:
    return CandidateAction(
        action_id=action_id,
        venue_id="polymarket",
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id=contract_id,
        settlement_observation_key=observation(contract_id),
        side=side,
        lot_step_units=1,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=2,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(ExecutableCostSlice(1, 2, 1),),
    )


def raw_problem() -> ArbitrageProblem:
    yes = raw_action("a-yes", "contract-a", ActionSide.BUY_YES)
    no = raw_action("a-no", "contract-a", ActionSide.BUY_NO)
    states = (
        TerminalStateSet(
            "contract-a",
            observation("contract-a"),
            "v1",
            (
                TerminalAtom(
                    "contract-a:yes",
                    TerminalKind.NORMAL_YES,
                    "v1",
                    (ActionPayout("a-yes", 1), ActionPayout("a-no", 0)),
                    AS_OF,
                ),
                TerminalAtom(
                    "contract-a:no",
                    TerminalKind.NORMAL_NO,
                    "v1",
                    (ActionPayout("a-yes", 0), ActionPayout("a-no", 1)),
                    AS_OF,
                ),
            ),
        ),
    )
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "live-test",
        AS_OF,
        "usd-cents",
        (yes, no),
        states,
        ConstraintModel((), ()),
        (),
    )


def row(identity: str, compiled: ArbitrageProblem) -> dict[str, object]:
    return {
        "identity": identity,
        "version_id": f"v-{identity}",
        "fingerprint": f"fp-{identity}",
        "activation": "ACTIVE",
        "relation_type": "IMPLIES",
        "endpoints": [],
        "model": {
            "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
            "payouts": {},
            "capital_release": "2026-08-31T00:00:00Z",
            "problem": canonical_payload(compiled),
        },
    }


def selected_component(
    component_id: str,
    *,
    relation_fingerprint: str = "r",
    terminal_fingerprint: str = "t",
) -> SelectedComponent:
    return SelectedComponent(
        component_id=component_id,
        contract_ids=("contract-a",),
        constraint_ids=(),
        action_ids=("a-yes", "a-no"),
        admission_score=20_000,
        portfolio=(),
        relation_fingerprint=relation_fingerprint,
        terminal_fingerprint=terminal_fingerprint,
        portfolio_fingerprint="p",
        status="ACTIVE",
    )


def test_startup_queues_current_generation_when_busy(tmp_path: Path) -> None:
    instance = driver(
        tmp_path,
        FakeCatalog({}, generation=7, fingerprint="fp-7"),
        idle=False,
    )
    instance._tick()
    assert instance.status() == {
        "selection_pending": 1,
        "selection_failures_consecutive": 0,
        "selection_applied_generation": None,
    }


def test_idle_gate_skips_processing_until_idle(tmp_path: Path) -> None:
    catalog = FakeCatalog({}, generation=7, fingerprint="fp-7")
    idle = [False]
    instance = PredictionMonitorSelectionDriver(
        relation_catalog=catalog,
        selection_store=MonitorSelectionStore(tmp_path),
        idle_check=lambda: idle[0],
    )
    instance._tick()
    assert instance.status()["selection_pending"] == 1
    idle[0] = True
    instance._tick()
    assert instance.status() == {
        "selection_pending": 0,
        "selection_failures_consecutive": 0,
        "selection_applied_generation": 7,
    }


def test_failure_keeps_pending_and_blocks_immediate_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = driver(tmp_path, FakeCatalog({}, generation=3, fingerprint="fp-3"))
    monkeypatch.setattr(
        instance,
        "_run_pass",
        lambda: (_ for _ in ()).throw(RuntimeError("pass failed")),
    )
    instance._tick()
    assert instance.status() == {
        "selection_pending": 1,
        "selection_failures_consecutive": 1,
        "selection_applied_generation": None,
    }
    instance._tick()
    assert instance.status()["selection_failures_consecutive"] == 1
    instance._next_attempt_at = 0.0
    instance._tick()
    assert instance.status()["selection_failures_consecutive"] == 2


def test_pending_queue_is_distinct_and_bounded(tmp_path: Path) -> None:
    catalog = FakeCatalog({}, generation=0, fingerprint="fp-0")
    instance = driver(tmp_path, catalog, idle=False)
    for index in range(40):
        catalog.fingerprint = f"fp-{index}"
        instance._tick()
    assert instance.status()["selection_pending"] == 32
    assert instance._pending[0] == (0, "fp-8")
    assert instance._pending[-1] == (0, "fp-39")


def test_retain_keeps_matching_component_and_drops_stale(tmp_path: Path) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    problem, components = relation_generation_problem(rows)
    component = components[0]
    raw = problem_for_component(problem, component)
    relation_fp = fingerprint({"constraint_model": raw.constraint_model})
    terminal_fp = fingerprint({"terminal_state_sets": raw.terminal_state_sets})
    instance = driver(tmp_path, FakeCatalog(rows))
    kept = selected_component(
        component.component_id,
        relation_fingerprint=relation_fp,
        terminal_fingerprint=terminal_fp,
    )
    stale = selected_component(
        component.component_id,
        relation_fingerprint="stale-r",
        terminal_fingerprint=terminal_fp,
    )
    missing = selected_component("component:missing")
    retained = instance._retain(
        {component.component_id: kept, "stale": stale, "missing": missing},
        problem,
        {component.component_id: component},
    )
    assert retained == {component.component_id: kept}


def test_pass_discovers_selects_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {"r:a": row("r:a", raw_problem())}
    catalog = FakeCatalog(rows, generation=5, fingerprint="fp-5")
    store = MonitorSelectionStore(tmp_path)
    instance = PredictionMonitorSelectionDriver(
        relation_catalog=catalog,
        selection_store=store,
        idle_check=lambda: True,
    )
    problem, components = relation_generation_problem(rows)
    component = components[0]
    selected = selected_component(component.component_id)
    captured: dict[str, object] = {}

    def fake_discovery(*args: object, **kwargs: object) -> tuple[()]:
        captured["problem"] = args[0]
        captured["components"] = args[1]
        captured.update(kwargs)
        return ()

    monkeypatch.setattr(
        "open_trader.prediction_monitor_selection_driver.run_discovery",
        fake_discovery,
    )
    monkeypatch.setattr(
        "open_trader.prediction_monitor_selection_driver.select_monitor_components",
        lambda candidates, current, **kwargs: {component.component_id: selected},
    )
    instance._tick()
    assert captured["problem"] == problem
    assert captured["components"] == components
    assert captured["generation"] == 5
    assert captured["code_version"] == "issue-87"
    assert captured["max_components"] == 10
    assert store.load()[1] == {component.component_id: selected}
    assert instance.status() == {
        "selection_pending": 0,
        "selection_failures_consecutive": 0,
        "selection_applied_generation": 5,
    }


def test_is_idle_tracks_pending_solves() -> None:
    class FakeSolver:
        def submit(self, request: object) -> Future[object]:
            return Future()

    resolver = object.__new__(PredictionLiveResolver)
    resolver._tracking = _OutcomeTrackingServer(FakeSolver())
    assert resolver.is_idle() is True
    future = resolver._tracking.submit(SimpleNamespace(request_id="r1"))
    assert resolver.is_idle() is False
    future.set_result(None)
    assert resolver.is_idle() is True


def test_start_stop_is_idempotent(tmp_path: Path) -> None:
    instance = driver(
        tmp_path,
        FakeCatalog({}, generation=1, fingerprint="fp-1"),
        idle=False,
    )
    instance.start()
    thread = instance._thread
    assert thread is not None
    instance.start()
    assert instance._thread is thread
    instance.stop()
    instance.stop()
    assert instance._thread is None
    assert not thread.is_alive()
