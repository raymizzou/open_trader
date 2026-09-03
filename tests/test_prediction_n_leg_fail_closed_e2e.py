"""Issue #107: fail-closed end-to-end lock on the real HTTP prediction service.

Every N_LEG fail-closed safety semantic is asserted through the production
assembly — ``create_prediction_server`` → ``GET /api/prediction-arbitrage/state``
— and never through a projection function directly, so a future regression
shows up exactly where an operator would see it. Two live assemblies are
exercised:

- Assembly (2) (seeded rows): an HTTP runtime whose ``n_leg_solutions()`` seam
  carries serialized #84 solutions, mirroring the production wiring
  (``PredictionRuntime.n_leg_solutions`` → ``PredictionLiveResolver.solutions``).
- Assembly (1) (live chain): a real v2 relation catalog, real in-process CP-SAT
  solving through the #52 ``PredictionLiveResolver`` (books injected through the
  monitor seam), adapted onto the same HTTP runtime — the multi-N proof that no
  leg count is special-cased (N=2/3/4/5, one builder, one chain).

Expected values are hand-computed from the locked defaults in
``prediction_n_leg_mode.DEFAULT_QUALIFICATION_POLICY`` (min profit $1.00,
min net margin 0.01, min annualized return 0.15, max capital release 30 days)
and the exact cross-multiplication gates in
``prediction_n_leg_read_model._qualification_projection``. 1 USD =
1,000,000 micro-units. Since #117 the live chain models the catalog fee
facts into the leg books and cost slices: the fixtures below state
``fees_enabled=False`` (proven fee-free, 0 bps) unless a case says
otherwise, a charging market is priced into the slices and decided on
post-fee numbers, and a market whose rate is missing/unparseable/conflicting
is unmodelable and its whole component is skipped before it can present.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from concurrent.futures import Future
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_live_resolver import PredictionLiveResolver
from open_trader.prediction_market_solution import (
    AccountView,
    MarketSolution,
)
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    OracleBudget,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_solver import (
    BenchmarkLimits,
    solve_with_constraint_generation,
)
from open_trader.prediction_solver_backends import CpSatBackend
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse
from open_trader.relation_catalog import RelationCatalog
from open_trader.relation_catalog_v2 import GROUP_BUDGET
from open_trader.polymarket_relation_discovery import (
    discover_mechanical_relation_catalog,
)
from test_mechanical_relations import (
    complement_relation,
    group_relation,
    mechanical_event,
    mechanical_market,
)
from test_prediction_legacy_retirement import (
    _ContractExecution,
    _FakeRuntime,
    _RetiredStateMonitor,
    _RetiredStateStore,
    _get_state,
    _serve,
)


UNITS_PER_DOLLAR = 1_000_000

#: Issue #112: the proven fee-free block the real resolver now attaches to
#: every solution entry; seeded fixtures use it to keep the non-fee cases
#: (and their locked values) byte-identical.
_FEE_FREE_BLOCK = {
    "status": "fee_free",
    "charging_contracts": [],
    "unknown_contracts": [],
}


# --------------------------------------------------------------------------
# Assembly (2) harness: production-shaped HTTP runtime with seeded solutions.
# --------------------------------------------------------------------------


class _BalanceMonitor:
    """Healthy monitor whose polymarket readiness carries explicit balances."""

    def __init__(self, *, p_usd_balance: str, p_usd_allowance: str) -> None:
        self._p_usd_balance = p_usd_balance
        self._p_usd_allowance = p_usd_allowance

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "health": {"status": "healthy", "degraded_reasons": []},
            "readiness": {
                "status": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": self._p_usd_balance,
                "p_usd_allowance": self._p_usd_allowance,
            },
            "heartbeat_at": "2026-09-01T00:00:00Z",
            "stale": False,
            "events": [],
            "opportunities": [],
        }


class _SeededHttpRuntime(_FakeRuntime):
    """Fence-2 production runtime whose N_LEG pipeline seam is seeded."""

    def __init__(
        self,
        solutions: list[dict[str, object]],
        *,
        monitor: object | None = None,
        execution: object | None = None,
        catalog: object | None = None,
    ) -> None:
        super().__init__(legacy_retired=True)
        self.store = _RetiredStateStore()
        self.monitor = monitor if monitor is not None else _RetiredStateMonitor()
        self.execution = execution if execution is not None else _ContractExecution()
        if catalog is not None:
            self.relation_catalog = catalog
        self.n_leg_solutions = lambda: list(solutions)  # noqa: E731


def _seeded_market(
    *,
    component_id: str = "component:cond-a:cond-b",
    contract_ids: tuple[str, ...] = ("cond-a", "cond-b"),
    quantity_lots: int = 20,
    profit_units: int,
    cost_units: int | None,
    payout_units: int,
    release_at: datetime | None,
    global_search_closed: bool = True,
) -> dict[str, object]:
    """One serialized #84 MarketSolution payload with hand-settable numbers."""
    market = canonical_payload(
        MarketSolution(
            component_id=component_id,
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=tuple(
                ActionQuantity(f"polymarket:{contract_id}", quantity_lots)
                for contract_id in contract_ids
            ),
            guaranteed_profit_units=profit_units,
            bounded_cost_units=cost_units,
            bounded_payout_units=payout_units,
            capital_release_at=release_at,
            global_search_closed=global_search_closed,
            verification_fingerprint="sha256:verify",
        )
    )
    return market


def _seeded_solution(
    market: dict[str, object],
    *,
    scope_id: str | None = None,
    legs: list[dict[str, object]] | None = None,
    execution_reason: str = "EXECUTABLE",
    capital_use_units: int | None = None,
    partial_fill_proof: str = "PARTIAL_FILL_SAFE",
    fee: dict[str, object] | None = _FEE_FREE_BLOCK,
) -> dict[str, object]:
    """Wrap a market payload into the resolver-shaped solution entry.

    Issue #112: the resolver always attaches a ``fee`` block; the default
    here is the proven fee-free state, and ``fee=None`` seeds the legacy
    shape without any fee block (the fail-closed default case)."""
    execution = {
        "market_solution_fingerprint": fingerprint(canonical_payload(market)),
        "quantities": market["quantities"],
        "capital_use_units": (
            market["bounded_cost_units"]
            if capital_use_units is None
            else capital_use_units
        ),
        "reason": execution_reason,
        "order_ready": False,
        "partial_fill_proof": partial_fill_proof,
    }
    entry: dict[str, object] = {
        "component_id": market["component_id"],
        "market": market,
        "execution": execution,
    }
    if scope_id is not None:
        entry["scope_id"] = scope_id
    if legs is not None:
        entry["legs"] = legs
    if fee is not None:
        entry["fee"] = dict(fee)
    return entry


def _qualification_checks(row: dict[str, object]) -> dict[str, dict[str, object]]:
    return {check["key"]: check for check in row["qualification"]["checks"]}


def _opportunity_row(state: dict[str, object], component_id: str) -> dict[str, object]:
    matches = [
        row
        for row in state["opportunities"]
        if row["component_id"] == component_id
    ]
    assert len(matches) == 1, state["opportunities"]
    return matches[0]


# --------------------------------------------------------------------------
# B1: the $1.00 minimum-profit gate, at and one micro-unit below the line.
# --------------------------------------------------------------------------


def test_b1_min_profit_exactly_one_dollar_passes_and_a_unit_below_fails(
    tmp_path: Path,
) -> None:
    """Hand math (units; 1 USD = 1,000,000 units; release = now+20d so the
    remaining gates are comfortable and only min_profit can flip):

    pass  profit=1,000,000: 1,000,000 * 1 >= 1.00 * 1,000,000 (equality passes)
          margin = 1,000,000/50,000,000 = 0.02 >= 0.01;
          annualized = 0.02 * 365/20 = 0.365 >= 0.15 -> QUALIFIED_VERIFIED
    fail  profit=999,999:   999,999 * 1 < 1.00 * 1,000,000 -> min_profit False,
          margin 0.01999998 and annualized 0.36499982 still pass -> NOT_QUALIFIED
    """
    release = datetime.now(UTC) + timedelta(days=20)
    passing = _seeded_solution(
        _seeded_market(
            component_id="component:b1-pass",
            contract_ids=("b1-pass-a", "b1-pass-b"),
            profit_units=1_000_000,
            cost_units=49_000_000,
            payout_units=50_000_000,
            release_at=release,
        )
    )
    failing = _seeded_solution(
        _seeded_market(
            component_id="component:b1-fail",
            contract_ids=("b1-fail-a", "b1-fail-b"),
            profit_units=999_999,
            cost_units=49_000_000,
            payout_units=50_000_000,
            release_at=release,
        )
    )
    with _serve(_SeededHttpRuntime([passing, failing])) as base:
        status, state = _get_state(base)

    assert status == 200
    passing_row = _opportunity_row(state, "component:b1-pass")
    checks = _qualification_checks(passing_row)
    assert checks["min_profit"]["passed"] is True
    assert checks["min_profit"]["value"] == "1"
    assert checks["min_profit"]["threshold"] == "1.00"
    assert passing_row["qualification"]["status"] == "QUALIFIED_VERIFIED"

    failing_row = _opportunity_row(state, "component:b1-fail")
    checks = _qualification_checks(failing_row)
    assert checks["min_profit"]["passed"] is False
    assert checks["min_profit"]["value"] == "0.999999"
    for key in ("net_margin", "annualized_return", "capital_release"):
        assert checks[key]["passed"] is True, key
    assert failing_row["qualification"]["status"] == "NOT_QUALIFIED"


# --------------------------------------------------------------------------
# B2: the 1% net-margin gate, at and one unit above the worst-payout line.
# --------------------------------------------------------------------------


def test_b2_net_margin_exactly_one_percent_passes_and_a_unit_above_fails(
    tmp_path: Path,
) -> None:
    """Hand math (release = now+20d keeps annualized comfortable):

    pass  profit=1,000,000, payout=100,000,000:
          margin gate profit*100 >= payout*1 -> 100,000,000 >= 100,000,000
          (equality passes); margin displays as "0.01";
          annualized = 0.01 * 365/20 = 0.1825 >= 0.15 -> QUALIFIED_VERIFIED
    fail  payout=100,000,001: 100,000,000 < 100,000,001 -> only net_margin
          flips False (min_profit 1.00, annualized 0.18249…, release all pass)
    """
    release = datetime.now(UTC) + timedelta(days=20)
    passing = _seeded_solution(
        _seeded_market(
            component_id="component:b2-pass",
            contract_ids=("b2-pass-a", "b2-pass-b"),
            profit_units=1_000_000,
            cost_units=99_000_000,
            payout_units=100_000_000,
            release_at=release,
        )
    )
    failing = _seeded_solution(
        _seeded_market(
            component_id="component:b2-fail",
            contract_ids=("b2-fail-a", "b2-fail-b"),
            profit_units=1_000_000,
            cost_units=99_000_001,
            payout_units=100_000_001,
            release_at=release,
        )
    )
    with _serve(_SeededHttpRuntime([passing, failing])) as base:
        status, state = _get_state(base)

    assert status == 200
    passing_row = _opportunity_row(state, "component:b2-pass")
    checks = _qualification_checks(passing_row)
    assert checks["net_margin"]["passed"] is True
    assert passing_row["qualification"]["net_margin"] == "0.01"
    assert checks["net_margin"]["threshold"] == "0.01"
    assert passing_row["qualification"]["status"] == "QUALIFIED_VERIFIED"

    failing_row = _opportunity_row(state, "component:b2-fail")
    checks = _qualification_checks(failing_row)
    assert checks["net_margin"]["passed"] is False
    for key in ("min_profit", "annualized_return", "capital_release"):
        assert checks[key]["passed"] is True, key
    assert failing_row["qualification"]["status"] == "NOT_QUALIFIED"


# --------------------------------------------------------------------------
# B3: the 15% annualized-return gate at the exact cross-multiplication
# equality (profit * 365 * 20 == payout * days * 3 for policy 0.15 = 3/20).
# --------------------------------------------------------------------------


def test_b3_annualized_exactly_fifteen_percent_passes_and_a_unit_above_fails(
    tmp_path: Path,
) -> None:
    """Hand math (profit=1,125,000, payout=91,250,000, release=now+30d taken
    at test construction, so at GET time the elapsed time is strictly below
    30 days and ceil yields exactly 30 — the epsilon can only shrink the day
    count, never flip the gate):

    margin = 1,125,000/91,250,000 = 0.012328… >= 0.01 (passes)
    annualized equality: LHS = 1,125,000 * 365 * 20 = 8,212,500,000
                         RHS = 91,250,000 * 30 * 3 = 8,212,500,000 -> passes,
    displayed annualized = 410,625,000 / 2,737,500,000 = "0.15"
    fail  payout=91,250,001: RHS = 8,212,500,090 > LHS -> only the annualized
          gate flips False (profit $1.125, margin, 30-day release all pass)
    """
    release = datetime.now(UTC) + timedelta(days=30)
    passing = _seeded_solution(
        _seeded_market(
            component_id="component:b3-pass",
            contract_ids=("b3-pass-a", "b3-pass-b"),
            profit_units=1_125_000,
            cost_units=90_125_000,
            payout_units=91_250_000,
            release_at=release,
        )
    )
    failing = _seeded_solution(
        _seeded_market(
            component_id="component:b3-fail",
            contract_ids=("b3-fail-a", "b3-fail-b"),
            profit_units=1_125_000,
            cost_units=90_125_001,
            payout_units=91_250_001,
            release_at=release,
        )
    )
    with _serve(_SeededHttpRuntime([passing, failing])) as base:
        status, state = _get_state(base)

    assert status == 200
    passing_row = _opportunity_row(state, "component:b3-pass")
    checks = _qualification_checks(passing_row)
    assert checks["annualized_return"]["passed"] is True
    assert passing_row["qualification"]["annualized_return"] == "0.15"
    assert checks["annualized_return"]["threshold"] == "0.15"
    assert checks["capital_release"]["passed"] is True
    assert passing_row["qualification"]["capital_release_days"] == 30
    assert passing_row["qualification"]["status"] == "QUALIFIED_VERIFIED"

    failing_row = _opportunity_row(state, "component:b3-fail")
    checks = _qualification_checks(failing_row)
    assert checks["annualized_return"]["passed"] is False
    for key in ("min_profit", "net_margin", "capital_release"):
        assert checks[key]["passed"] is True, key
    assert failing_row["qualification"]["status"] == "NOT_QUALIFIED"


# --------------------------------------------------------------------------
# B4: the 30-day capital-release window at exactly 30 days and at 30d+6min
# (ceil -> 31 days), which must fail with capital_release_days = 31.
# --------------------------------------------------------------------------


def test_b4_capital_release_exactly_thirty_days_passes_and_thirty_one_fails(
    tmp_path: Path,
) -> None:
    """Hand math (profit=15,000,000, payout=100,000,000 keeps every other gate
    comfortable for both variants; +6min dwarfs any test-execution epsilon):

    days = ceil(elapsed/24h), floor 1, release must be strictly future.
    now+30d   -> days=30 <= 30 -> capital_release passes
    now+30d+6min -> days=31 > 30 -> fails with capital_release_days == 31;
    annualized = 0.15 * 365/days = 1.825 (30d) / 1.766… (31d) >= 0.15 either way
    """
    passing = _seeded_solution(
        _seeded_market(
            component_id="component:b4-pass",
            contract_ids=("b4-pass-a", "b4-pass-b"),
            profit_units=15_000_000,
            cost_units=85_000_000,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    failing = _seeded_solution(
        _seeded_market(
            component_id="component:b4-fail",
            contract_ids=("b4-fail-a", "b4-fail-b"),
            profit_units=15_000_000,
            cost_units=85_000_000,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=30, minutes=6),
        )
    )
    with _serve(_SeededHttpRuntime([passing, failing])) as base:
        status, state = _get_state(base)

    assert status == 200
    passing_row = _opportunity_row(state, "component:b4-pass")
    checks = _qualification_checks(passing_row)
    assert checks["capital_release"]["passed"] is True
    assert passing_row["qualification"]["capital_release_days"] == 30
    assert passing_row["qualification"]["status"] == "QUALIFIED_VERIFIED"

    failing_row = _opportunity_row(state, "component:b4-fail")
    checks = _qualification_checks(failing_row)
    assert checks["capital_release"]["passed"] is False
    assert failing_row["qualification"]["capital_release_days"] == 31
    for key in ("min_profit", "net_margin", "annualized_return"):
        assert checks[key]["passed"] is True, key
    assert failing_row["qualification"]["status"] == "NOT_QUALIFIED"


# --------------------------------------------------------------------------
# A4/A5: unknown inputs fail closed to UNKNOWN; a beyond-window release fails
# closed to NOT_QUALIFIED.
# --------------------------------------------------------------------------


def test_a4_missing_bounded_cost_qualification_is_unknown(tmp_path: Path) -> None:
    """Fee/cost unknown fails closed: since #117 the live chain models the
    catalog fee facts (``prediction_live_resolver._snapshot_for`` stamps each
    leg book with the contract's fee fact -- 0 bps when proven fee-free, the
    rate in bps priced into the cost slices when charging -- and a market
    whose rate is missing/unparseable/conflicting is unmodelable, skipping
    the whole component before it could present), so a presented live row
    always has a known fee. The fail-closed half shown here is therefore a
    missing worst-case cost: with ``bounded_cost_units`` absent the
    projection cannot know the worst-case cost, every presentable number
    would be invented, and the qualification must be UNKNOWN — never
    silently qualified. All ratio gates stay decidable here (profit $2,
    margin 0.02, annualized 0.365, release 20d), so only the missing cost
    forces the UNKNOWN.
    """
    solution = _seeded_solution(
        _seeded_market(
            component_id="component:a4-cost-unknown",
            contract_ids=("a4-a", "a4-b"),
            profit_units=2_000_000,
            cost_units=None,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=20),
        )
    )
    with _serve(_SeededHttpRuntime([solution])) as base:
        status, state = _get_state(base)

    assert status == 200
    row = _opportunity_row(state, "component:a4-cost-unknown")
    checks = _qualification_checks(row)
    for key in ("min_profit", "net_margin", "annualized_return", "capital_release"):
        assert checks[key]["passed"] is True, key
    assert row["qualification"]["status"] == "UNKNOWN"
    assert row["qualification"]["worst_case"]["maximum_cost"] is None
    assert row["order_ready"] is False


def test_a5_unknown_and_beyond_window_capital_release_fail_closed(
    tmp_path: Path,
) -> None:
    """Hand math (profit=15,000,000, payout=100,000,000: margin 0.15 and
    annualized 2.7375 (20d) / 1.766… (31d) stay comfortable, so only the
    release gate decides):

    unknown    capital_release_at=None -> the release check is undecidable
               (passed=None) -> status UNKNOWN, days None.
    beyond     release=now+30d+6min -> ceil -> 31 days > 30 -> NOT_QUALIFIED
               with capital_release_passed=False and capital_release_days=31.
    """
    unknown = _seeded_solution(
        _seeded_market(
            component_id="component:a5-release-unknown",
            contract_ids=("a5-u-a", "a5-u-b"),
            profit_units=15_000_000,
            cost_units=85_000_000,
            payout_units=100_000_000,
            release_at=None,
        )
    )
    beyond = _seeded_solution(
        _seeded_market(
            component_id="component:a5-release-beyond",
            contract_ids=("a5-b-a", "a5-b-b"),
            profit_units=15_000_000,
            cost_units=85_000_000,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=30, minutes=6),
        )
    )
    with _serve(_SeededHttpRuntime([unknown, beyond])) as base:
        status, state = _get_state(base)

    assert status == 200
    unknown_row = _opportunity_row(state, "component:a5-release-unknown")
    unknown_checks = _qualification_checks(unknown_row)
    assert unknown_checks["capital_release"]["passed"] is None
    assert unknown_checks["capital_release"]["value"] is None
    assert unknown_row["qualification"]["status"] == "UNKNOWN"
    assert unknown_row["order_ready"] is False

    beyond_row = _opportunity_row(state, "component:a5-release-beyond")
    beyond_checks = _qualification_checks(beyond_row)
    assert beyond_checks["capital_release"]["passed"] is False
    assert beyond_row["qualification"]["capital_release_days"] == 31
    assert beyond_row["qualification"]["status"] == "NOT_QUALIFIED"
    assert beyond_row["order_ready"] is False


# --------------------------------------------------------------------------
# A2b/A3b (seeded layer): the stale-monitor and verification-UNKNOWN
# fail-closed presentations through the seeded HTTP assembly.
# --------------------------------------------------------------------------


class _StaleBalanceMonitor(_BalanceMonitor):
    """The healthy ``_BalanceMonitor`` shape with the top-level monitor
    staleness flag set (the ``"stale": True`` payload the read model
    consumes at ``prediction_read_model.py`` ``snapshot_stale``)."""

    def snapshot(self) -> dict[str, object]:
        payload = super().snapshot()
        payload["stale"] = True
        return payload


def test_a2b_stale_monitor_seed_shows_unknown_summary_and_never_order_ready(
    tmp_path: Path,
) -> None:
    """Stale monitor fails the dashboard closed (assembly 2, HTTP layer): the
    read model turns the top-level ``"stale": True`` monitor flag into an
    ``UNKNOWN`` opportunity-qualification summary
    (``prediction_read_model.py`` ``snapshot_stale`` → status "UNKNOWN"; the
    unit mirror is ``test_prediction_read_model.py::
    test_opportunity_qualification_summary_is_unknown_when_stale``). Here the
    seeded row itself is fully qualified with comfortable funding (hand math:
    profit $2.00, payout $100.00, cost $95.00, release 20d — margin 0.02,
    annualized 0.365), so the summary flip can only come from the stale
    layer: the summary must read UNKNOWN (never a no-arbitrage signal) while
    the row stays presented and never order-ready."""
    solution = _seeded_solution(
        _seeded_market(
            component_id="component:a2b-stale",
            contract_ids=("a2b-a", "a2b-b"),
            profit_units=2_000_000,
            cost_units=95_000_000,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=20),
        )
    )
    monitor = _StaleBalanceMonitor(p_usd_balance="100.00", p_usd_allowance="100.00")
    with _serve(_SeededHttpRuntime([solution], monitor=monitor)) as base:
        status, state = _get_state(base)

    assert status == 200
    summary = state["opportunity_qualification"]
    assert summary["status"] == "UNKNOWN"
    assert summary["no_arbitrage"] is False
    row = _opportunity_row(state, "component:a2b-stale")
    assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"
    assert row["order_ready"] is False


def test_a3b_seeded_solver_verifier_unknown_presents_unknown_and_never_order_ready(
    tmp_path: Path,
) -> None:
    """A solver/verifier-UNKNOWN verification fails closed at the row level
    (assembly 2, HTTP layer). ``resolution_from_verification`` /
    ``resolve_market_solution`` (``prediction_market_solution.py:350-414``)
    map a solver or verifier failure to ``ComponentResolution(UNKNOWN, None,
    "SOLVER_OR_VERIFIER_UNKNOWN")`` — no ``MarketSolution`` exists, so there
    are no verified economics to present. The seeded canonical payload is the
    honest HTTP presentation of that state: the verified economics fields
    (profit/cost/payout) are absent so no number is invented, every structure
    fact the monitor path knows stays complete (quantities, fingerprints,
    20-day capital release, closed global search), and the execution slot
    carries the exact production unknown reason. Seeded into the
    MANUAL_CANARY scope that would otherwise allow ordering, the row must
    present qualification UNKNOWN with the undecidable checks reporting no
    value, a worst case with no invented bounds, ``would_submit`` False, and
    ``order_ready`` False — while staying on the presented list (contrast the
    live-chain A3 timeout, whose row is withdrawn entirely)."""
    solution = _seeded_solution(
        _seeded_market(
            component_id="component:a3b-verify-unknown",
            contract_ids=("a3b-a", "a3b-b"),
            profit_units=None,
            cost_units=None,
            payout_units=None,
            release_at=datetime.now(UTC) + timedelta(days=20),
        ),
        scope_id="s1",
        execution_reason="SOLVER_OR_VERIFIER_UNKNOWN",
    )
    with _serve(_SeededHttpRuntime([solution])) as base:
        status, state = _get_state(base)

    assert status == 200
    row = _opportunity_row(state, "component:a3b-verify-unknown")
    checks = _qualification_checks(row)
    for key in ("min_profit", "net_margin", "annualized_return"):
        assert checks[key]["passed"] is None, key
        assert checks[key]["value"] is None, key
    assert checks["capital_release"]["passed"] is True
    assert row["qualification"]["status"] == "UNKNOWN"
    assert row["qualification"]["worst_case"] == {
        "minimum_payout": None,
        "maximum_cost": None,
        "guaranteed_profit": None,
    }
    projection = row["n_leg_solution"]
    assert projection["main_list"] is False
    assert projection["blocked_reason"] == "QUALIFICATION_UNKNOWN"
    assert projection["execution"]["would_submit"] is False
    assert row["order_ready"] is False
    assert row["reason"] == "SOLVER_OR_VERIFIER_UNKNOWN"


# --------------------------------------------------------------------------
# A6 (seeded layer): insufficient balance keeps the row on the main list but
# never order-ready, with the exact per-venue INSUFFICIENT_BALANCE reason.
# --------------------------------------------------------------------------


def test_a6_seeded_low_balance_stays_on_main_list_and_never_order_ready(
    tmp_path: Path,
) -> None:
    """Hand math (profit=2,000,000, payout=100,000,000, release=now+20d:
    margin 0.02, annualized 0.365, release 20d — qualification fully passes,
    so only the funding layer can act):

    required (polymarket) = 47.50 + 47.50 = 95.00; available = 0.01 ->
    balance_ok=False, allowance_ok=True (100.00) -> reasons exactly
    ["INSUFFICIENT_BALANCE"]. The execution solution carries the production
    low-balance reason INSUFFICIENT_FUNDS (``execution_solution_from_market``
    reports it whenever capital_use exceeds available/allowance), so the row
    stays on the main list (QUALIFIED_VERIFIED, main_list=True, funding
    INSUFFICIENT) while order_ready stays False and executable is False.
    """
    market = _seeded_market(
        component_id="component:a6-low-balance",
        contract_ids=("a6-a", "a6-b"),
        profit_units=2_000_000,
        cost_units=95_000_000,
        payout_units=100_000_000,
        release_at=datetime.now(UTC) + timedelta(days=20),
    )
    legs = [
        {
            "action_id": "polymarket:a6-a",
            "venue": "polymarket",
            "max_cost": "47.50",
        },
        {
            "action_id": "polymarket:a6-b",
            "venue": "polymarket",
            "max_cost": "47.50",
        },
    ]
    solution = _seeded_solution(
        market,
        scope_id="s1",
        legs=legs,
        execution_reason="INSUFFICIENT_FUNDS",
    )
    monitor = _BalanceMonitor(p_usd_balance="0.01", p_usd_allowance="100.00")
    with _serve(_SeededHttpRuntime([solution], monitor=monitor)) as base:
        status, state = _get_state(base)

    assert status == 200
    row = _opportunity_row(state, "component:a6-low-balance")
    assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"
    projection = row["n_leg_solution"]
    assert projection["main_list"] is True
    funding = projection["funding"]
    assert funding["status"] == "INSUFFICIENT"
    polymarket = funding["venues"]["polymarket"]
    assert polymarket["required"] == "95.00"
    assert polymarket["available"] == "0.01"
    assert polymarket["balance_ok"] is False
    assert polymarket["allowance_ok"] is True
    assert polymarket["reasons"] == ["INSUFFICIENT_BALANCE"]
    assert projection["executable"] is False
    assert projection["blocked_reason"] == "INSUFFICIENT_BALANCE"
    assert row["order_ready"] is False
    assert row["reason"] == "INSUFFICIENT_FUNDS"


# --------------------------------------------------------------------------
# Assembly (1): the live chain — real v2 catalog, real in-process CP-SAT
# solving, real #52 resolver — adapted onto the production HTTP runtime.
# --------------------------------------------------------------------------

#: Test budget/limits covering N=2..5 binary EXACTLY_ONE groups (raw joint
#: states 2^N <= 32) with room for the constraint-generation rounds; injected
#: through the resolver's #71 budget/limits seams. The production LIVE_BUDGET
#: and LIVE_LIMITS stay untouched.
LIVE_TEST_BUDGET = OracleBudget(
    max_quantity_vectors=64, max_joint_states=64, max_support_rechecks=2
)
LIVE_TEST_LIMITS = BenchmarkLimits(
    soft_time_limit_ms=5_000,
    hard_time_limit_ms=10_000,
    memory_limit_bytes=1 << 30,
    max_constraint_generation_rounds=8,
)
#: Quantity cap per leg (also the compiled max_quantity_lots); the book depth
#: below stays well above it so the solver's per-leg optimum is exactly this.
LIVE_TEST_QUANTITY_CAP = 20
LIVE_TEST_BOOK_DEPTH = 200


class _LiveBooksMonitor:
    """Read-only book seam keyed to the resolver's 30s ``SNAPSHOT_FRESHNESS``
    harness window; production ``PolymarketMonitor.cross_venue_books`` omits
    books older than the stricter 10s ``BOOK_FRESHNESS_SECONDS``, so a stale
    quote is a fortiori omitted there and can never form a resolver snapshot."""

    def __init__(self, books: dict[str, ThresholdOrderBook]) -> None:
        self.books = dict(books)

    def _fresh(self) -> dict[str, ThresholdOrderBook]:
        now = datetime.now(UTC)
        return {
            token: book
            for token, book in self.books.items()
            if 0 <= (now - book.confirmed_at).total_seconds() <= 30
        }

    def cross_venue_books(
        self, token_ids: tuple[str, ...]
    ) -> dict[str, ThresholdOrderBook]:
        fresh = self._fresh()
        return {
            token: book for token, book in fresh.items() if token in set(token_ids)
        }

    def cross_venue_book_meta(self, token_id: str) -> dict[str, object]:
        book = self.books.get(token_id)
        if book is None:
            return {"received_at": None, "exchange_time": None, "sequence": None}
        return {
            "received_at": book.confirmed_at,
            "exchange_time": book.confirmed_at,
            "sequence": int(book.confirmed_at.timestamp() * 1000),
        }


class _RealSolverServer:
    """In-process solver server running the real #50 solve seam per request
    (the validation-harness pattern). ``timeout_next`` makes the solve entry
    raise TimeoutError instead, the production worker-timeout shape."""

    def __init__(self) -> None:
        self.submit_calls = 0
        self.requests: list[object] = []
        self.timeout_next = False

    def submit(self, request: object) -> Future[WorkerOutcome]:
        self.submit_calls += 1
        self.requests.append(request)
        future: Future[WorkerOutcome] = Future()
        if self.timeout_next:
            future.set_exception(TimeoutError("solver timed out"))
            return future
        evidence = solve_with_constraint_generation(
            request.request, CpSatBackend(), request.limits
        )
        future.set_result(
            WorkerOutcome(
                request.request_id,
                "OK",
                "COMPLETED",
                1,
                1,
                0,
                False,
                True,
                WorkerResponse(
                    "p",
                    "cp_sat",
                    request.request_id,
                    "OK",
                    canonical_payload(evidence),
                    {},
                    (),
                ),
                "9.15.6755",
            )
        )
        return future


class _LiveStore:
    """Store seam with caps loose enough that qualification — not the
    unsettled-capital or partial-fill cap — decides the row."""

    def n_leg_control(self) -> dict[str, object]:
        return {"total_unsettled_capital_units": 0, "qualification_policy_version": 1}

    def n_leg_safety_config_latest(self) -> dict[str, object]:
        return {
            "version": 1,
            "config": {
                "max_total_unsettled_capital_units": 60_000_000,
                "max_partial_fill_loss_units": 60_000_000,
                "max_auto_repair_loss_units": 0,
            },
        }


class _LiveExecution:
    """Account seam: balances can never gate the live chain here."""

    def n_leg_account_view(self) -> AccountView:
        return AccountView(10**18, 10**18, 0)


class _LiveHttpRuntime(_FakeRuntime):
    """Production runtime shape whose N_LEG seams are a real live resolver
    and the real v2 catalog (so relation_review reflects catalog changes)."""

    def __init__(
        self,
        *,
        resolver: PredictionLiveResolver,
        catalog: RelationCatalog,
        monitor: _LiveBooksMonitor,
        execution: object | None = None,
    ) -> None:
        super().__init__(legacy_retired=True)
        self.store = _RetiredStateStore()
        self.monitor = _RetiredStateMonitor()
        self.execution = execution if execution is not None else _ContractExecution()
        self.relation_catalog = catalog
        self.n_leg_solutions = resolver.solutions
        self.n_leg_episodes = resolver.n_leg_episodes


def _canonical_timestamp(value: datetime) -> str:
    """RFC3339 UTC 'Z' timestamp, the canonical payload encoding."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _exactly_one_problem_payload(
    contract_ids: list[str],
    *,
    release_at: datetime,
    quantity_cap: int = LIVE_TEST_QUANTITY_CAP,
) -> dict[str, object]:
    """The compiled binary-settlement EXACTLY_ONE problem over ``contract_ids``
    — the oracle-corpus ``exactly-one-n3``/``quantity-selection-n4`` shape,
    parametrized over N and serialized through ``canonical_payload``. Each
    contract carries the two real settlement atoms NORMAL_YES/NORMAL_NO (a YES
    holder is paid one dollar per lot when its own contract resolves YES), so
    EXACTLY_ONE guarantees exactly one YES per group; every action is BUY_YES
    on ``polymarket:{contract_id}`` with the test quantity cap."""
    as_of = datetime.now(UTC)
    actions: list[CandidateAction] = []
    states: list[TerminalStateSet] = []
    for contract_id in contract_ids:
        key = SettlementObservationKey(
            OBSERVATION_SCHEMA_V1,
            "oracle-nleg107",
            "indicator-nleg107",
            as_of,
            as_of,
            "UTC",
            f"rules-nleg107-{contract_id}",
        )
        action_id = f"polymarket:{contract_id}"
        actions.append(
            CandidateAction(
                action_id,
                venue_id="polymarket",
                account_id="catalog-v2",
                chain_id="polymarket",
                market_contract_id=contract_id,
                settlement_observation_key=key,
                side=ActionSide.BUY_YES,
                lot_step_units=1,
                quantity_scale=1,
                min_quantity_lots=1,
                max_quantity_lots=quantity_cap,
                settlement_asset_id="USD",
                valuation_unit_id="USD",
                asset_valuation_rule_id="usd-1:1-v1",
                cost_slices=(ExecutableCostSlice(1, quantity_cap, 0),),
            )
        )
        states.append(
            TerminalStateSet(
                contract_id,
                key,
                f"rules-nleg107-{contract_id}",
                (
                    TerminalAtom(
                        f"{contract_id}:NORMAL_YES",
                        TerminalKind.NORMAL_YES,
                        f"rules-nleg107-{contract_id}",
                        (ActionPayout(action_id, 1),),
                        release_at,
                    ),
                    TerminalAtom(
                        f"{contract_id}:NORMAL_NO",
                        TerminalKind.NORMAL_NO,
                        f"rules-nleg107-{contract_id}",
                        (ActionPayout(action_id, 0),),
                        release_at,
                    ),
                ),
            )
        )
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "nleg107",
        as_of,
        "USD",
        tuple(actions),
        tuple(states),
        ConstraintModel(
            (
                RelationConstraint(
                    f"exactly-one:{':'.join(contract_ids)}",
                    RelationKind.EXACTLY_ONE,
                    tuple(contract_ids),
                    "rules-nleg107",
                ),
            ),
            (),
        ),
        (),
    )
    return canonical_payload(problem)


def _exactly_one_payload(
    contract_ids: list[str],
    prices: list[str],
    *,
    release_at: datetime,
    quantity_cap: int = LIVE_TEST_QUANTITY_CAP,
    relation_type: str = "EXACTLY_ONE",
    fee_fields: list[dict[str, object] | None] | None = None,
) -> dict[str, object]:
    """One COMPLETE EXACTLY_ONE discovery payload over ``contract_ids``.
    ``prices`` document the intended book per leg (the live books are seeded
    separately); the compiled problem is the binary settlement view locked by
    the oracle corpus — the solvable N-leg shape. ``relation_type`` selects
    the identity prefix (EXACTLY_ONE groups vs NATIVE_COMPLEMENT pairs); the
    compiled constraint model stays EXACTLY_ONE, exactly like the mechanical
    complement codec's own compiled model. ``fee_fields`` carries the #112
    per-market fee facts: by default every market is the proven fee-free
    ``{"fees_enabled": False}``; a per-index dict is merged into that
    market's entry, and a per-index ``None`` omits the fee keys entirely
    (the fee-unknown case)."""
    return {
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-08-15T02:32:00Z",
        "relation_type": relation_type,
        "semantics": {
            "statement": "exactly one resolves YES",
            "direction": "A_TO_B",
        },
        "source_evidence": [
            {"source": "Polymarket rules", "quote": "resolves YES if..."}
        ],
        "model": {
            "completeness": "COMPLETE",
            "terminal_states": ["NORMAL_YES", "NORMAL_NO"],
            "payouts": {
                contract_id: {"NORMAL_YES": 1, "NORMAL_NO": 0}
                for contract_id in contract_ids
            },
            "capital_release": _canonical_timestamp(release_at),
            "problem": _exactly_one_problem_payload(
                contract_ids, release_at=release_at, quantity_cap=quantity_cap
            ),
        },
        "markets": [
            {
                "venue": "Polymarket",
                "contract_id": contract_id,
                "title": f"NLeg107 market {index}",
                "market_date": "2026-08-15T00:00:00Z",
                "expires_at": "2026-12-31T17:00:00Z",
                "event_identity_basis": "event-nleg107",
                "settlement_observation_key": "nleg107",
                "settlement_rules": "official index",
                "cancellation_rules": "void refunds",
                **(
                    {"fees_enabled": False}
                    if fee_fields is None
                    else (fee_fields[index] or {})
                ),
            }
            for index, contract_id in enumerate(contract_ids)
        ],
    }


def _live_book(
    contract_id: str,
    price: str,
    *,
    confirmed_at: datetime | None = None,
    depth: int = LIVE_TEST_BOOK_DEPTH,
) -> ThresholdOrderBook:
    level = (BookLevel(Decimal(price), Decimal(str(depth))),)
    return ThresholdOrderBook(
        contract_id, level, level, confirmed_at or datetime.now(UTC)
    )


def _activate_relation(
    tmp_path: Path, payload: dict[str, object]
) -> tuple[RelationCatalog, str]:
    catalog = RelationCatalog(tmp_path / "catalog")
    entry = catalog.ingest(payload)
    approved = catalog.approve(
        entry["version_id"],
        {"version_id": entry["version_id"]},
        actor="op",
        git_sha="sha",
    )
    assert approved["activation"] == "ACTIVE", approved
    return catalog, str(entry["identity"])


def _build_live_resolver(
    tmp_path: Path,
    catalog: RelationCatalog,
    monitor: _LiveBooksMonitor,
    server: _RealSolverServer,
    *,
    execution: object | None = None,
    portfolio: tuple[ActionQuantity, ...] = (),
) -> tuple[PredictionLiveResolver, list[str]]:
    """Seed the #77 selection exactly like the no-submit validation harness:
    one component per compiled relation group with its structural
    fingerprints, then the resolver over the real catalog object. The
    optional ``portfolio`` seeds the selected quantities (the #117 frozen
    fee units are computed over them at request-build time)."""
    rows = catalog.current_generation()
    problem, components = relation_generation_problem(rows)
    assert problem is not None and components
    selection_store = MonitorSelectionStore(tmp_path)
    selection: dict[str, SelectedComponent] = {}
    for component in components:
        sub = problem_for_component(problem, component)
        selection[component.component_id] = SelectedComponent(
            component_id=component.component_id,
            contract_ids=component.contract_ids,
            constraint_ids=component.constraint_ids,
            action_ids=component.action_ids,
            admission_score=0,
            portfolio=portfolio,
            relation_fingerprint=fingerprint(
                {"constraint_model": sub.constraint_model}
            ),
            terminal_fingerprint=fingerprint(
                {"terminal_state_sets": sub.terminal_state_sets}
            ),
            portfolio_fingerprint=fingerprint({"quantities": ()}),
            status="ACTIVE",
        )
    selection_store.save(selection)
    resolver = PredictionLiveResolver(
        data_dir=tmp_path,
        relation_catalog=catalog,
        monitor=monitor,
        solver_server=server,
        selection_store=selection_store,
        store=_LiveStore(),
        execution=execution if execution is not None else _LiveExecution(),
        poll_interval=0.01,
        budget=LIVE_TEST_BUDGET,
        limits=LIVE_TEST_LIMITS,
    )
    return resolver, [component.component_id for component in components]


def _drive_live_state(
    tmp_path: Path,
    catalog: RelationCatalog,
    monitor: _LiveBooksMonitor,
    server: _RealSolverServer,
    *,
    execution: object | None = None,
) -> tuple[int, dict[str, object], PredictionLiveResolver]:
    resolver, _ = _build_live_resolver(tmp_path, catalog, monitor, server)
    resolver._tick()
    resolver._tick()
    runtime = _LiveHttpRuntime(
        resolver=resolver,
        catalog=catalog,
        monitor=monitor,
        execution=execution,
    )
    try:
        with _serve(runtime) as base:
            status, state = _get_state(base)
    except BaseException:
        resolver.stop()
        raise
    return status, state, resolver


# --------------------------------------------------------------------------
# C1/C4/C5: one builder, one live chain, N=2/3/4/5 — no leg-count hardcoding.
# --------------------------------------------------------------------------


def _positive_legs(row: dict[str, object]) -> list[dict[str, object]]:
    return [
        leg
        for leg in row["n_leg_solution"]["market"]["legs"]
        if leg["quantity_lots"] > 0
    ]


@pytest.mark.parametrize(
    "n,prices,profit_units,cost_units,payout_units,net_margin,annualized",
    [
        # Hand math per N: every leg buys the 20-lot cap at its ask; exactly
        # one contract resolves YES, so the guaranteed payout is 20 lots x $1.
        # profit = 20 x (1 - sum(asks)) x $1, cost = 20 x sum(asks) x $1.
        # N=2: asks 0.40+0.40=0.80 -> profit 4,000,000; margin 0.2;
        #      annualized 0.2*365/20 = 3.65
        (
            2,
            ("0.40", "0.40"),
            4_000_000,
            16_000_000,
            20_000_000,
            "0.2",
            "3.65",
        ),
        # N=3: asks 0.32+0.30+0.28=0.90 -> profit 2,000,000; margin 0.1;
        #      annualized 0.1*365/20 = 1.825
        (
            3,
            ("0.32", "0.30", "0.28"),
            2_000_000,
            18_000_000,
            20_000_000,
            "0.1",
            "1.825",
        ),
        # N=4: asks 0.24+0.22+0.21+0.21=0.88 -> profit 2,400,000; margin 0.12;
        #      annualized 0.12*365/20 = 2.19
        (
            4,
            ("0.24", "0.22", "0.21", "0.21"),
            2_400_000,
            17_600_000,
            20_000_000,
            "0.12",
            "2.19",
        ),
        # N=5: asks 0.20+0.19+0.18+0.17+0.16=0.90 -> profit 2,000,000;
        #      margin 0.1; annualized 1.825
        (
            5,
            ("0.20", "0.19", "0.18", "0.17", "0.16"),
            2_000_000,
            18_000_000,
            20_000_000,
            "0.1",
            "1.825",
        ),
    ],
)
def test_c1_live_chain_solves_every_n_without_leg_hardcoding(
    tmp_path: Path,
    n: int,
    prices: tuple[str, ...],
    profit_units: int,
    cost_units: int,
    payout_units: int,
    net_margin: str,
    annualized: str,
) -> None:
    """One synthetic EXACTLY_ONE builder driven through the SAME live chain at
    N=2/3/4/5 (C5: the parametrization itself is the no-hardcoding proof —
    nothing in the chain knows the leg count). Per N the HTTP row must show:
    legs == N with exactly N positive-quantity legs (C4: the group activates
    inside GROUP_BUDGET=7), the hand-computed profit/cost/payout, net margin,
    annualized return, 20 capital-release days, QUALIFIED_VERIFIED — and the
    fail-closed presentation: order_ready stays False with SCOPE_OBSERVE_ONLY
    because live solutions carry no scope_id, so the capability falls back to
    observe-only (src/open_trader/prediction_n_leg_read_model.py). The
    chain's markets are all proven fee-free (see module docstring), which
    qualifies. """
    contract_ids = [f"c{index}" for index in range(n)]
    release = datetime.now(UTC) + timedelta(days=20)
    payload = _exactly_one_payload(contract_ids, list(prices), release_at=release)
    catalog, identity = _activate_relation(tmp_path, payload)
    assert identity.count("|") == n  # one endpoint per leg, one group
    # C4: the group activated inside the seven-contract budget.
    assert n <= GROUP_BUDGET
    problem, components = relation_generation_problem(
        catalog.current_generation()
    )
    assert len(components) == 1
    assert len(components[0].contract_ids) == n

    monitor = _LiveBooksMonitor(
        {
            contract_id: _live_book(contract_id, price)
            for contract_id, price in zip(contract_ids, prices)
        }
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run", catalog, monitor, server
    )
    try:
        assert status == 200
        assert server.submit_calls >= 1
        component_id = components[0].component_id
        row = _opportunity_row(state, component_id)
        assert row["leg_count"] == n
        assert len(row["n_leg_solution"]["market"]["legs"]) == n
        positive = _positive_legs(row)
        assert len(positive) == n
        assert all(leg["quantity_lots"] == LIVE_TEST_QUANTITY_CAP for leg in positive)

        market = row["n_leg_solution"]["market"]
        assert market["minimum_profit"] == format(
            Decimal(profit_units) / UNITS_PER_DOLLAR, "f"
        )
        # exact solver economics, straight from the resolver's market payload
        raw = resolver.solutions()[0]["market"]
        assert raw["guaranteed_profit_units"] == profit_units
        assert raw["bounded_cost_units"] == cost_units
        assert raw["bounded_payout_units"] == payout_units
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        assert qualification["net_margin"] == net_margin
        assert qualification["annualized_return"] == annualized
        assert qualification["capital_release_days"] == 20
        assert all(
            check["passed"] is True for check in qualification["checks"]
        )
        # fail-closed presentation of a live row: no scope_id -> capability
        # fallback -> observe-only, never order-ready
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


# --------------------------------------------------------------------------
# C2/C3: the real mechanical codecs (VENUE_METADATA negRisk group N=4 and
# YES/NO native complement N=2) through the same live chain.
#
# The codecs' stored five-kind terminal models (NORMAL_YES/NORMAL_NO plus
# VOID/REFUND/SPLIT) are deliberately void-conservative and locked by their own
# tests: VOID/REFUND/SPLIT scenarios pay zero and are allowed, so no portfolio
# over such a group can carry a positive GUARANTEED payout and the group can
# never surface a qualified, orderable row. That fail-closed outcome is locked
# first. The issue's hand numbers ($0.96 x 125 for the negRisk group) are then
# locked on the binary settlement view of the SAME relation identity — the
# oracle-corpus exactly-one shape — proving the chain itself has no N bias.
# --------------------------------------------------------------------------


def _codec_identity(tmp_path: Path, row: object) -> str:
    catalog = RelationCatalog(tmp_path / "codec-identity")
    return str(catalog.ingest_mechanical_relation(row)["identity"])


def _fee_free_group_relation(n: int = 4) -> object:
    """The real negRisk codec fixture pinned to proven fee-free markets.

    Since #117 a component whose contracts carry no modelable fee fact is
    skipped whole before the solve; pinning the fixture to
    ``fees_enabled=False`` keeps this test's void-conservative semantics
    (the dispatch must actually happen) instead of skipping it."""
    relation = group_relation(n)
    return replace(
        relation,
        markets=tuple(
            replace(market, fees_enabled=False) for market in relation.markets
        ),
    )


def _fee_free_complement_relation() -> object:
    """The real complement codec fixture pinned to proven fee-free (see
    ``_fee_free_group_relation``)."""
    relation = complement_relation()
    return replace(relation, market=replace(relation.market, fees_enabled=False))


def _assert_no_qualified_row(state: dict[str, object]) -> None:
    for row in state["opportunities"]:
        assert row["qualification"]["status"] != "QUALIFIED_VERIFIED"
        assert row["order_ready"] is False
        assert row["n_leg_solution"]["main_list"] is False


def test_c2_negrisk_n4_real_codec_group_never_surfaces_a_qualified_row(
    tmp_path: Path,
) -> None:
    """The real NegRisk group codec (4 markets, five-kind terminal model)
    activated in the v2 catalog and dispatched into the live chain: the oracle
    cannot bound the group (5^4 raw joint states exceed the state budget) and
    even a full enumeration could not prove a positive guaranteed payout
    (void/refund/split scenarios pay zero), so /state must not present any
    qualified or orderable row for the group."""
    catalog = RelationCatalog(tmp_path / "catalog")
    result = catalog.ingest_mechanical_relation(_fee_free_group_relation(4))
    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="op",
        git_sha="sha",
    )
    assert approved["activation"] == "ACTIVE"
    contracts = [f"condition-{index}" for index in range(4)]
    monitor = _LiveBooksMonitor(
        {contract: _live_book(contract, "0.24") for contract in contracts}
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run", catalog, monitor, server
    )
    try:
        assert status == 200
        assert server.submit_calls >= 1
        assert state["opportunities"] == []
        # no qualified projection is presented at all
        assert "n_leg_solutions" not in state
    finally:
        resolver.stop()


def test_c2_negrisk_n4_issue_numbers_on_the_binary_settlement_view(
    tmp_path: Path,
) -> None:
    """Hand math for the issue's numbers on the negRisk group's binary
    settlement view (four BUY_YES legs, exactly one resolves YES, 125 lots per
    leg, asks 4 x $0.24 = $0.96 per unit):

    cost   = 125 x 0.96 x $1 = $120.00 -> 120,000,000 units
    payout = 125 x 1    x $1 = $125.00 -> 125,000,000 units
    profit = 125 - 120 = $5.00         ->   5,000,000 units
    net margin = 5/125 = 0.04 (>= 0.01); release = end_date = now+20d -> 20
    annualized = 0.04 * 365/20 = 0.73 (>= 0.15) -> QUALIFIED_VERIFIED

    The relation identity equals the real codec's identity (same endpoints,
    same relation type), and the presented row is again order-ready False /
    SCOPE_OBSERVE_ONLY. The known taker fee 0 does not block qualification.
    """
    end_date = datetime.now(UTC) + timedelta(days=20)
    discovery = discover_mechanical_relation_catalog(
        [
            mechanical_event(
                *(
                    mechanical_market(
                        f"m{index}",
                        yes_token=f"yes-m{index}",
                        no_token=f"no-m{index}",
                        end_date=_canonical_timestamp(end_date),
                    )
                    for index in range(4)
                ),
                event_id="event-negrisk-107",
                neg_risk=True,
            )
        ]
    )
    assert len(discovery.groups) == 1
    (group,) = discovery.groups
    contracts = [market.condition_id for market in group.markets]

    payload = _exactly_one_payload(
        contracts, ["0.24"] * 4, release_at=end_date, quantity_cap=125
    )
    catalog, identity = _activate_relation(tmp_path, payload)
    assert identity == _codec_identity(tmp_path, group)

    monitor = _LiveBooksMonitor(
        {
            contract: _live_book(contract, "0.24", depth=125)
            for contract in contracts
        }
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run", catalog, monitor, server
    )
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(contracts))
        raw = resolver.solutions()[0]["market"]
        assert raw["guaranteed_profit_units"] == 5_000_000
        assert raw["bounded_payout_units"] == 125_000_000
        assert raw["bounded_cost_units"] == 120_000_000
        assert row["leg_count"] == 4
        assert len(_positive_legs(row)) == 4
        assert all(leg["quantity_lots"] == 125 for leg in _positive_legs(row))
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        assert qualification["net_margin"] == "0.04"
        assert qualification["annualized_return"] == "0.73"
        assert qualification["capital_release_days"] == 20
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


def test_c3_complement_n2_real_codec_pair_never_surfaces_a_qualified_row(
    tmp_path: Path,
) -> None:
    """The real YES/NO native complement codec (two token endpoints,
    five-kind terminal model) through the live chain at fresh, profitable-looking
    books: because void/refund/split scenarios pay zero, no portfolio over the
    pair carries a positive guaranteed payout, so whatever the admission master
    closes stays unqualified — /state must not present a qualified or orderable
    row for the pair."""
    catalog = RelationCatalog(tmp_path / "catalog")
    result = catalog.ingest_mechanical_relation(_fee_free_complement_relation())
    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="op",
        git_sha="sha",
    )
    assert approved["activation"] == "ACTIVE"
    monitor = _LiveBooksMonitor(
        {"yes-1": _live_book("yes-1", "0.48"), "no-1": _live_book("no-1", "0.49")}
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run", catalog, monitor, server
    )
    try:
        assert status == 200
        assert server.submit_calls >= 1
        _assert_no_qualified_row(state)
    finally:
        resolver.stop()


def test_c3_complement_n2_hand_math_on_the_binary_settlement_view(
    tmp_path: Path,
) -> None:
    """Hand math for the complement pair's binary settlement view (two token
    legs, exactly one resolves YES, 200 lots per leg, asks 0.48 + 0.49 = 0.97):

    cost   = 200 x 0.97 = $194.00 -> 194,000,000 units
    payout = 200 x 1    = $200.00 -> 200,000,000 units
    profit = 200 - 194 = $6.00    ->   6,000,000 units
    net margin = 6/200 = 0.03; release = now+20d -> 20 days
    annualized = 0.03 * 365/20 = 0.5475 -> QUALIFIED_VERIFIED

    The relation identity equals the real codec's NATIVE_COMPLEMENT identity,
    and the row stays order-ready False / SCOPE_OBSERVE_ONLY.
    """
    release = datetime.now(UTC) + timedelta(days=20)
    payload = _exactly_one_payload(
        ["yes-1", "no-1"],
        ["0.48", "0.49"],
        release_at=release,
        quantity_cap=200,
        relation_type="NATIVE_COMPLEMENT",
    )
    catalog, identity = _activate_relation(tmp_path, payload)
    assert identity == _codec_identity(tmp_path, complement_relation())

    monitor = _LiveBooksMonitor(
        {
            "yes-1": _live_book("yes-1", "0.48", depth=200),
            "no-1": _live_book("no-1", "0.49", depth=200),
        }
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run", catalog, monitor, server
    )
    try:
        assert status == 200
        row = _opportunity_row(state, "component:no-1:yes-1")
        raw = resolver.solutions()[0]["market"]
        assert raw["guaranteed_profit_units"] == 6_000_000
        assert raw["bounded_payout_units"] == 200_000_000
        assert raw["bounded_cost_units"] == 194_000_000
        assert row["leg_count"] == 2
        assert len(_positive_legs(row)) == 2
        assert all(leg["quantity_lots"] == 200 for leg in _positive_legs(row))
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        assert qualification["net_margin"] == "0.03"
        assert qualification["annualized_return"] == "0.5475"
        assert qualification["capital_release_days"] == 20
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


# --------------------------------------------------------------------------
# A7: a fully-qualified live row is still never order-ready (OBSERVE_ONLY
# capability fallback), with sufficient funding and the known fee 0.
# --------------------------------------------------------------------------


def test_a7_fully_qualified_live_row_stays_observe_only(tmp_path: Path) -> None:
    """Every qualification gate passes for this N=2 live row (asks
    0.40+0.40, 20 lots per leg: profit 4,000,000 / payout 20,000,000 / cost
    16,000,000; margin 0.2; annualized 3.65; release 20d; the chain's proven
    fee-free markets — see A4 — do not block). Even so the row
    must stay order_ready=False with reason SCOPE_OBSERVE_ONLY: live
    solutions carry no scope_id, so the projection's capability falls back
    to observe-only; would_submit stays a pure qualification+execution
    statement (True) and never unlocks ordering. The funding block honestly
    reports UNKNOWN: live solutions carry no per-leg venue/max_cost display
    facts, so the projection invents no per-venue requirement (executable
    stays undecidable)."""
    contract_ids = ["a7-a", "a7-b"]
    payload = _exactly_one_payload(
        contract_ids, ["0.40", "0.40"], release_at=datetime.now(UTC) + timedelta(days=20)
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {c: _live_book(c, "0.40") for c in contract_ids}
    )
    server = _RealSolverServer()
    status, state, resolver = _drive_live_state(
        tmp_path / "run",
        catalog,
        monitor,
        server,
        execution=_BalanceExecution(),
    )
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(contract_ids))
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        assert all(check["passed"] is True for check in qualification["checks"])
        projection = row["n_leg_solution"]
        assert projection["funding"]["status"] == "UNKNOWN"
        assert projection["executable"] is None
        assert projection["execution"]["would_submit"] is True
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


class _BalanceExecution:
    """Account seam with an explicit balance (default: well funded)."""

    def __init__(self, available_units: int = 10**18) -> None:
        self._available_units = available_units

    def n_leg_account_view(self) -> AccountView:
        return AccountView(self._available_units, self._available_units, 0)


# --------------------------------------------------------------------------
# A6 (live layer): a low account balance fails the execution solution closed.
# --------------------------------------------------------------------------


def test_a6_live_low_balance_execution_fails_closed_and_stays_observe_only(
    tmp_path: Path,
) -> None:
    """Low account balance (available $1.00 < required $16.00 capital use):
    ``execution_solution_from_market`` reports INSUFFICIENT_FUNDS on the
    execution solution (asserted on the resolver's public solutions seam —
    the exact payload the HTTP runtime consumes), while at /state the row
    keeps its qualification (market-side facts are unchanged) and stays
    order_ready=False / SCOPE_OBSERVE_ONLY. The funding block itself reports
    FUNDING_UNKNOWN: live solutions carry no per-leg venue/max_cost display
    facts, so the projection invents no per-venue requirement."""
    contract_ids = ["a6l-a", "a6l-b"]
    payload = _exactly_one_payload(
        contract_ids, ["0.40", "0.40"], release_at=datetime.now(UTC) + timedelta(days=20)
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {c: _live_book(c, "0.40") for c in contract_ids}
    )
    server = _RealSolverServer()
    resolver, components = _build_live_resolver(
        tmp_path / "run",
        catalog,
        monitor,
        server,
        execution=_BalanceExecution(available_units=1_000_000),
    )
    resolver._tick()
    resolver._tick()
    raw = resolver.solutions()
    assert len(raw) == 1
    assert raw[0]["execution"]["reason"] == "INSUFFICIENT_FUNDS"

    runtime = _LiveHttpRuntime(
        resolver=resolver,
        catalog=catalog,
        monitor=monitor,
        execution=_BalanceExecution(available_units=1_000_000),
    )
    try:
        with _serve(runtime) as base:
            status, state = _get_state(base)
        assert status == 200
        row = _opportunity_row(state, components[0])
        assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"
        projection = row["n_leg_solution"]
        assert projection["funding"]["status"] == "UNKNOWN"
        assert projection["executable"] is None
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


# --------------------------------------------------------------------------
# A1: a source change revokes the running relation and the live row leaves
# /state on the resolver's next generation cycle.
# --------------------------------------------------------------------------


def test_a1_rule_change_withdraws_the_live_row(tmp_path: Path) -> None:
    """Rule change through the public v2 source-change path: v1 is ACTIVE with
    its row on /state; a v2 of the same identity is ingested, approved (the
    activation gate blocks it while v1 is live) and published with
    ``RelationCatalog.replace(reason="rules_changed")``. The generation
    fingerprint moves, the resolver's next cycle generation-prunes the stale
    selection, and the row leaves /state. The superseded version keeps its
    SUPERSEDED activation in the catalog history (review vocabulary) and the
    replacement counts as ACTIVATED."""
    release_v1 = datetime.now(UTC) + timedelta(days=20)
    payload_v1 = _exactly_one_payload(["a1-a", "a1-b"], ["0.40", "0.40"], release_at=release_v1)
    catalog, identity = _activate_relation(tmp_path, payload_v1)
    generation_v1 = catalog.current_generation()
    version_v1 = str(generation_v1[identity]["version_id"])

    monitor = _LiveBooksMonitor(
        {"a1-a": _live_book("a1-a", "0.40"), "a1-b": _live_book("a1-b", "0.40")}
    )
    server = _RealSolverServer()
    resolver, components = _build_live_resolver(
        tmp_path / "run", catalog, monitor, server
    )
    component_id = components[0]
    runtime = _LiveHttpRuntime(
        resolver=resolver, catalog=catalog, monitor=monitor
    )
    try:
        with _serve(runtime) as base:
            resolver._tick()
            resolver._tick()
            status, state = _get_state(base)
            assert status == 200
            before = _opportunity_row(state, component_id)
            assert before["qualification"]["status"] == "QUALIFIED_VERIFIED"

            # the source change: same identity, changed model (new release)
            payload_v2 = _exactly_one_payload(
                ["a1-a", "a1-b"],
                ["0.40", "0.40"],
                release_at=datetime.now(UTC) + timedelta(days=19),
            )
            entry_v2 = catalog.ingest(payload_v2)
            catalog.approve(
                entry_v2["version_id"],
                {"version_id": entry_v2["version_id"]},
                actor="op",
                git_sha="sha",
            )
            replaced = catalog.replace(
                {"version_id": version_v1},
                {"version_id": entry_v2["version_id"]},
                reason="rules_changed",
                actor="op",
                git_sha="sha",
            )
            assert replaced["activated_version_id"] == entry_v2["version_id"]

            # the resolver's next cycle prunes the stale generation
            resolver._tick()
            resolver._tick()
            status, state = _get_state(base)
            assert status == 200
            assert state["opportunities"] == []
            assert "n_leg_solutions" not in state

            # catalog evidence: the old version is superseded history, the
            # replacement is the active relation
            history_ids = {
                str(row["version_id"]) for row in catalog.list("history")
            }
            assert version_v1 in history_ids
            review = state["relation_review"]["counts"]
            assert review["ACTIVATED"] == 1
    finally:
        resolver.stop()


def test_a1_relation_review_presents_source_changed_reapproval(
    tmp_path: Path,
) -> None:
    """The #95 review vocabulary maps the superseded version (APPROVED with
    activation SUPERSEDED — the historical #60-cutover shape; public flows
    revoke-and-replace instead) onto SOURCE_CHANGED_REAPPROVAL, and /state
    presents that count. The record is written through the same store-level
    seam the locked #95 tests use (``_force_record``)."""
    from test_relation_catalog import _force_record

    payload = _exactly_one_payload(
        ["a1r-a", "a1r-b"],
        ["0.40", "0.40"],
        release_at=datetime.now(UTC) + timedelta(days=20),
    )
    catalog, identity = _activate_relation(tmp_path, payload)
    version_id = str(catalog.current_generation()[identity]["version_id"])
    _force_record(
        catalog, version_id, status="APPROVED", activation_status="SUPERSEDED"
    )

    with _serve(_SeededHttpRuntime([], catalog=catalog)) as base:
        status, state = _get_state(base)

    assert status == 200
    review = state["relation_review"]
    assert review["counts"]["SOURCE_CHANGED_REAPPROVAL"] == 1
    assert review["counts"]["ACTIVATED"] == 0


# --------------------------------------------------------------------------
# A2: stale quotes never enter the live chain (freshness 30s).
# --------------------------------------------------------------------------


def test_a2_stale_books_are_never_dispatched(tmp_path: Path) -> None:
    """The harness window is the resolver's SNAPSHOT_FRESHNESS = 30s, while
    production cross_venue_books ignores old books under the stricter
    BOOK_FRESHNESS_SECONDS = 10s (a 31s-old book is a fortiori ignored): a
    book re-confirmed 31 seconds ago at a NEW price would be a new economic
    snapshot if it were fresh, but the stale quote is omitted, the resolver
    forms no snapshot, and no second solve is ever dispatched — no
    qualification evidence is derived from stale books, and nothing
    orderable is presented (the persisted row keeps order_ready=False)."""
    contract_ids = ["a2-a", "a2-b"]
    payload = _exactly_one_payload(
        contract_ids, ["0.40", "0.40"], release_at=datetime.now(UTC) + timedelta(days=20)
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {c: _live_book(c, "0.40") for c in contract_ids}
    )
    server = _RealSolverServer()
    resolver, components = _build_live_resolver(
        tmp_path / "run", catalog, monitor, server
    )
    component_id = components[0]
    runtime = _LiveHttpRuntime(
        resolver=resolver, catalog=catalog, monitor=monitor
    )
    try:
        with _serve(runtime) as base:
            resolver._tick()
            resolver._tick()
            status, state = _get_state(base)
            assert status == 200
            row = _opportunity_row(state, component_id)
            assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"
            assert row["order_ready"] is False
            dispatches_after_fresh = server.submit_calls
            assert dispatches_after_fresh >= 1

            # a repriced book whose confirmation is 31s stale: omitted by the
            # production freshness contract, never dispatched
            stale_at = datetime.now(UTC) - timedelta(seconds=31)
            monitor.books = {
                "a2-a": _live_book("a2-a", "0.10", confirmed_at=stale_at),
                "a2-b": _live_book("a2-b", "0.10", confirmed_at=stale_at),
            }
            resolver._tick()
            resolver._tick()
            assert server.submit_calls == dispatches_after_fresh
    finally:
        resolver.stop()


# --------------------------------------------------------------------------
# A3: a solver timeout withdraws the presented row instead of leaving stale
# qualified economics on the board.
# --------------------------------------------------------------------------


def test_a3_solver_timeout_withdraws_the_presented_row(tmp_path: Path) -> None:
    """The production worker maps a solver timeout to a non-OK outcome
    (``WorkerOutcome`` UNKNOWN / raised TimeoutError). After a fresh solve
    presented a qualified row, a repriced book re-dispatches and the solve
    times out: the resolver drops the stored solution, so the next /state
    carries no row at all — an unproven quote never leaves the previous
    verified economics presented as if still orderable."""
    contract_ids = ["a3-a", "a3-b"]
    payload = _exactly_one_payload(
        contract_ids, ["0.40", "0.40"], release_at=datetime.now(UTC) + timedelta(days=20)
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {c: _live_book(c, "0.40") for c in contract_ids}
    )
    server = _RealSolverServer()
    resolver, components = _build_live_resolver(
        tmp_path / "run", catalog, monitor, server
    )
    component_id = components[0]
    runtime = _LiveHttpRuntime(
        resolver=resolver, catalog=catalog, monitor=monitor
    )
    try:
        with _serve(runtime) as base:
            resolver._tick()
            resolver._tick()
            status, state = _get_state(base)
            assert status == 200
            row = _opportunity_row(state, component_id)
            assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"

            # reprice (fresh timestamps, new economic fingerprint) and time out
            server.timeout_next = True
            monitor.books = {
                "a3-a": _live_book("a3-a", "0.35"),
                "a3-b": _live_book("a3-b", "0.35"),
            }
            resolver._tick()
            resolver._tick()
            status, state = _get_state(base)
            assert status == 200
            assert state["opportunities"] == []
            assert "n_leg_solutions" not in state
    finally:
        resolver.stop()


# --------------------------------------------------------------------------
# Issue #112→#117 (S5): the fee acceptance cases, locked through the
# production assembly (create_prediction_server → GET /state). Case A proves
# the fee-free path keeps the #107 locked values byte-identical (with the
# #117 block fields); B locks a charging market solved on post-fee numbers
# (modeled block), C locks the unmodelable-rate whole-component skip, and D
# locks the missing-fee-block fail-closed presentation.
# --------------------------------------------------------------------------


def _fee_live_state(
    tmp_path: Path,
    contract_ids: list[str],
    prices: list[str],
    *,
    fee_fields: list[dict[str, object] | None] | None = None,
) -> tuple[int, dict[str, object], PredictionLiveResolver]:
    release = datetime.now(UTC) + timedelta(days=20)
    payload = _exactly_one_payload(
        contract_ids,
        prices,
        release_at=release,
        fee_fields=fee_fields,
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {contract: _live_book(contract, price) for contract, price in zip(contract_ids, prices)}
    )
    server = _RealSolverServer()
    return _drive_live_state(tmp_path / "run", catalog, monitor, server)


def test_fee_a_all_markets_fee_free_keep_the_issue_107_locked_values(
    tmp_path: Path,
) -> None:
    """Case A (unchanged): every market fees_enabled=False. Hand math is the
    #107 C1 N=2 baseline (asks 0.40+0.40, 20 lots per leg, exactly one YES):
    profit 4,000,000 / cost 16,000,000 / payout 20,000,000; margin 0.2;
    annualized 0.2*365/20 = 3.65; release 20d -> QUALIFIED_VERIFIED, every
    qualification check passes (fee_status included), and the #107 fail-closed
    presentation (order_ready False / SCOPE_OBSERVE_ONLY) is byte-identical."""
    contract_ids = ["fee-a", "fee-b"]
    status, state, resolver = _fee_live_state(tmp_path, contract_ids, ["0.40", "0.40"])
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(contract_ids))
        raw = resolver.solutions()[0]
        assert raw["fee"]["status"] == "fee_free"
        assert raw["fee"] == {
            "status": "fee_free",
            "charging_contracts": [],
            "unknown_contracts": [],
            "modeled": True,
            "taker_fee_rate_bps": 0,
            "taker_fee_units": 0,
        }
        assert raw["market"]["guaranteed_profit_units"] == 4_000_000
        assert raw["market"]["bounded_cost_units"] == 16_000_000
        assert raw["market"]["bounded_payout_units"] == 20_000_000
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        assert qualification["net_margin"] == "0.2"
        assert qualification["annualized_return"] == "3.65"
        assert qualification["capital_release_days"] == 20
        checks = _qualification_checks(row)
        assert checks["fee_status"]["passed"] is True
        assert checks["fee_status"]["value"] == "fee_free"
        assert checks["fee_status"]["threshold"] == "fee_free"
        assert all(check["passed"] is True for check in qualification["checks"])
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


def test_fee_b_single_charging_market_solves_on_post_fee_numbers(
    tmp_path: Path,
) -> None:
    """Case B (#117 flip of the #112 charging-unknown case): exactly one
    market flipped to fees_enabled=True + fee_rate "0.05" is modelable now,
    so the component solves with the fee priced into that leg's slices and
    the block freezes {fee_charging, modeled=True, 500 bps}. Hand math (asks
    0.40+0.40, 20 lots/leg, N=2, only leg fee-a charging: per-lot fee
    0.05 x 400,000 x 600,000 / 1e6 = 12,000 units, 20 lots -> 240,000):
    profit 4,000,000 - 240,000 = 3,760,000; margin 0.188; annualized 3.431
    -> QUALIFIED_VERIFIED with the fee check itself passing (True,
    value fee_charging) and order-ready blocked only by the observe-only
    capability fallback."""
    contract_ids = ["fee-a", "fee-b"]
    status, state, resolver = _fee_live_state(
        tmp_path,
        contract_ids,
        ["0.40", "0.40"],
        fee_fields=[
            {"fees_enabled": True, "fee_rate": "0.05"},
            {"fees_enabled": False},
        ],
    )
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(sorted(contract_ids)))
        raw = resolver.solutions()[0]
        assert raw["fee"]["status"] == "fee_charging"
        assert raw["fee"]["charging_contracts"] == ["fee-a"]
        assert raw["fee"]["unknown_contracts"] == []
        assert raw["fee"]["modeled"] is True
        assert raw["fee"]["taker_fee_rate_bps"] == 500
        assert raw["market"]["guaranteed_profit_units"] == 3_760_000
        checks = _qualification_checks(row)
        assert checks["fee_status"]["passed"] is True
        assert checks["fee_status"]["value"] == "fee_charging"
        assert checks["fee_status"]["threshold"] == "fee_free"
        assert row["qualification"]["status"] == "QUALIFIED_VERIFIED"
        projection = row["n_leg_solution"]
        assert projection["execution"]["would_submit"] is True
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


def test_fee_c_unmodelable_rate_skips_the_component_entirely(
    tmp_path: Path,
) -> None:
    """Case C (#117 flip of the #112 fee-unknown case): fees_enabled missing
    on exactly one market is unmodelable -- there is no fee number to price,
    so the whole component is skipped like a book miss: no solve, no
    solutions() entry, and no row on /state at all (stricter than the #112
    presented-fee_unknown row, which required blocking an economics that no
    longer exists)."""
    contract_ids = ["fee-a", "fee-b"]
    status, state, resolver = _fee_live_state(
        tmp_path,
        contract_ids,
        ["0.40", "0.40"],
        fee_fields=[None, {"fees_enabled": False}],
    )
    try:
        assert status == 200
        assert state["opportunities"] == []
        assert "n_leg_solutions" not in state
        assert resolver.solutions() == []
    finally:
        resolver.stop()


def test_fee_d_seeded_row_without_fee_block_fails_closed(tmp_path: Path) -> None:
    """Case D (missing fee block, fail-closed by default): a seeded solution
    row that carries no "fee" block at all — the shape any pre-#112 producer
    would emit — must present UNKNOWN with FEE_UNKNOWN and never order-ready,
    even though every other qualification number is comfortable (profit $2.00,
    cost $95.00, payout $100.00, release 20d: margin 0.02, annualized 0.365)."""
    solution = _seeded_solution(
        _seeded_market(
            component_id="component:fee-d",
            contract_ids=("fee-d-a", "fee-d-b"),
            profit_units=2_000_000,
            cost_units=95_000_000,
            payout_units=100_000_000,
            release_at=datetime.now(UTC) + timedelta(days=20),
        ),
        fee=None,
    )
    assert "fee" not in solution
    with _serve(_SeededHttpRuntime([solution])) as base:
        status, state = _get_state(base)

    assert status == 200
    row = _opportunity_row(state, "component:fee-d")
    assert row["qualification"]["status"] == "UNKNOWN"
    checks = _qualification_checks(row)
    assert checks["fee_status"]["passed"] is None
    projection = row["n_leg_solution"]
    assert projection["execution"]["would_submit"] is False
    assert projection["execution"]["order_ready"] is False
    assert row["order_ready"] is False
    assert row["reason"] == "FEE_UNKNOWN"


# --------------------------------------------------------------------------
# Issue #117 (S6): the full-chain taker-fee economics on a complementary
# pair (EXACTLY_ONE over two markets == the YES/NO complement shape), 100
# shares per leg, catalog rate 0.04 on both markets, 1 share/lot and
# PU=1e6 -- every hand number below is an exact integer (per-lot fees
# 9,984 at $0.48 and 9,996 at $0.49 divide cleanly, no carry drift).
# --------------------------------------------------------------------------

SEAM_D_QUANTITY = 100


def _seam_d_live_state(
    tmp_path: Path,
    contract_ids: list[str],
    prices: list[str],
) -> tuple[int, dict[str, object], PredictionLiveResolver]:
    """Seam D harness: the real live chain over a two-market EXACTLY_ONE
    group with catalog rate 0.04 on both markets and books at the given
    prices; the #77 selection carries the 100-lot portfolio so the frozen
    fee units describe the same opportunity the solver picks (the cap)."""
    release = datetime.now(UTC) + timedelta(days=20)
    payload = _exactly_one_payload(
        contract_ids,
        prices,
        release_at=release,
        quantity_cap=SEAM_D_QUANTITY,
        fee_fields=[
            {"fees_enabled": True, "fee_rate": "0.04"},
            {"fees_enabled": True, "fee_rate": "0.04"},
        ],
    )
    catalog, _ = _activate_relation(tmp_path, payload)
    monitor = _LiveBooksMonitor(
        {
            contract: _live_book(contract, price, depth=SEAM_D_QUANTITY)
            for contract, price in zip(contract_ids, prices)
        }
    )
    server = _RealSolverServer()
    portfolio = tuple(
        ActionQuantity(f"polymarket:{contract}", SEAM_D_QUANTITY)
        for contract in contract_ids
    )
    resolver, _components = _build_live_resolver(
        tmp_path / "run",
        catalog,
        monitor,
        server,
        portfolio=portfolio,
    )
    resolver._tick()
    resolver._tick()
    runtime = _LiveHttpRuntime(resolver=resolver, catalog=catalog, monitor=monitor)
    try:
        with _serve(runtime) as base:
            status, state = _get_state(base)
    except BaseException:
        resolver.stop()
        raise
    return status, state, resolver


def test_d1_complement_pair_at_forty_eight_cents_qualifies_post_fee(
    tmp_path: Path,
) -> None:
    """D1 (#117): both legs ask $0.48, both markets charging 0.04, 100
    shares per leg at 1 share/lot, PU=1e6. Hand math (all exact integers):

    payout = 100 x $1                      = $100.00  (100,000,000 units)
    cost   = 2 x 100 x $0.48               = $96.00   (96,000,000 units)
    fee    = 2 x 100 x 0.04 x 0.48 x 0.52  = $1.9968  (1,996,800 units;
             per lot per leg 0.04 x 480,000 x 520,000 / 1e6 = 9,984, exact)
    profit = 100,000,000 - 96,000,000 - 1,996,800 = 2,003,200 ($2.0032);
    margin 0.020032, annualized 0.365584, release 20d, min profit $2.0032
    -> QUALIFIED_VERIFIED on post-fee numbers, with the frozen fee block
    charging / modeled / 400 bps / 1,996,800 units. Under #112 semantics
    this charging group was qualification UNKNOWN; #117 decides it."""
    contract_ids = ["d1-a", "d1-b"]
    status, state, resolver = _seam_d_live_state(tmp_path, contract_ids, ["0.48", "0.48"])
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(sorted(contract_ids)))
        raw = resolver.solutions()[0]
        assert raw["market"]["guaranteed_profit_units"] == 2_003_200
        assert raw["market"]["bounded_cost_units"] == 97_996_800
        assert raw["market"]["bounded_payout_units"] == 100_000_000
        assert raw["fee"] == {
            "status": "fee_charging",
            "charging_contracts": sorted(contract_ids),
            "unknown_contracts": [],
            "modeled": True,
            "taker_fee_rate_bps": 400,
            "taker_fee_units": 1_996_800,
        }
        qualification = row["qualification"]
        assert qualification["status"] == "QUALIFIED_VERIFIED"
        checks = _qualification_checks(row)
        assert checks["fee_status"]["passed"] is True
        assert checks["fee_status"]["value"] == "fee_charging"
        assert checks["min_profit"]["passed"] is True
        assert row["order_ready"] is False
        assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    finally:
        resolver.stop()


def test_d2_complement_pair_at_forty_nine_cents_not_qualified_post_fee(
    tmp_path: Path,
) -> None:
    """D2 (#117), adjacent to D1 (AC#3 contrast): both legs ask $0.49, same
    catalog rate 0.04. Hand math (exact integers):

    fee    = 2 x 100 x 0.04 x 0.49 x 0.51 = $1.9992 (1,999,200 units;
             per lot per leg 0.04 x 490,000 x 510,000 / 1e6 = 9,996, exact)
    cost   = 2 x 100 x $0.49              = $98.00  (98,000,000 units)
    profit = 100,000,000 - 98,000,000 - 1,999,200 = 800 ($0.0008)

    The fee check itself passes (charging+modeled) and the post-fee
    min_profit check fails -> NOT_QUALIFIED, never UNKNOWN. Under #112
    semantics this group was UNKNOWN (fee unmodeled); the "before" half of
    that contrast is locked by the flipped Seam C read-model cases."""
    contract_ids = ["d2-a", "d2-b"]
    status, state, resolver = _seam_d_live_state(tmp_path, contract_ids, ["0.49", "0.49"])
    try:
        assert status == 200
        row = _opportunity_row(state, "component:" + ":".join(sorted(contract_ids)))
        raw = resolver.solutions()[0]
        assert raw["market"]["guaranteed_profit_units"] == 800
        assert raw["market"]["bounded_cost_units"] == 99_999_200
        assert raw["market"]["bounded_payout_units"] == 100_000_000
        assert raw["fee"]["status"] == "fee_charging"
        assert raw["fee"]["modeled"] is True
        assert raw["fee"]["taker_fee_rate_bps"] == 400
        assert raw["fee"]["taker_fee_units"] == 1_999_200
        checks = _qualification_checks(row)
        assert checks["fee_status"]["passed"] is True
        assert checks["min_profit"]["passed"] is False
        assert row["qualification"]["status"] == "NOT_QUALIFIED"
        assert row["order_ready"] is False
    finally:
        resolver.stop()


# ---------------------------------------------------------------------------
# Issue #64: the manual-confirm full chain (confirm -> preflight -> admit ->
# submit -> receipts) and its fail-closed gate matrix, driven with fakes
# through the same seams the production runtime wires (queue driver, fake
# trading client, real store).
# ---------------------------------------------------------------------------


def _issue64_full_chain(tmp_path: Path, *, outcome: dict[str, object]):
    from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        _caps_store,
        _e2e_driver,
        _enqueued_e2e_store,
        _FakeTrading,
        _solution_entry,
        _confirm,
    )

    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading([outcome, {"state": "FILLED", "error_code": None, "cost_units": 400}])
    driver = _e2e_driver(store, trading, base_source, recon=True)
    return store, row, base_source, trading, driver


def test_issue64_full_chain_confirm_admit_submit_complete(tmp_path: Path) -> None:
    store, row, _base, trading, driver = _issue64_full_chain(
        tmp_path,
        outcome={"state": "FILLED", "error_code": None, "cost_units": 400},
    )
    summary = driver.tick(now=_E2E_AS_OF)
    assert "abandoned" not in summary, summary
    assert len(trading.calls) == 2
    batch = store.n_leg_batch(summary["submitted"])
    assert str(batch["state"]).startswith("RECONCILED")
    stored = next(
        r for r in store.n_leg_requests() if r["request_id"] == row["request_id"]
    )
    assert stored["state"] == "SUBMITTED"


def test_issue64_gate_matrix_fails_closed_before_any_submit(tmp_path: Path) -> None:
    from open_trader.prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        NLegConfirmRejected,
    )
    from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        _confirm,
        _solution_entry,
    )

    # OBSERVE_ONLY scope (fresh store, no caps): hard reject, no row.
    from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        _store as _fresh_store,
    )

    observe_store = _fresh_store(tmp_path / "observe")
    with pytest.raises(NLegConfirmRejected) as observe:
        _confirm(observe_store, [_solution_entry()], idempotency_key="m1")
    assert observe.value.reason == "SCOPE_OBSERVE_ONLY"

    # Caps not configured with a ready scope: rejected, no row.
    from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        _store as _fresh_store2,
    )
    from open_trader.prediction_n_leg_mode import (  # type: ignore[import-not-found]
        ensure_same_event_same_venue_scope,
        n_leg_upsert_scope,
    )

    nocaps = _fresh_store2(tmp_path / "nocaps")
    ensure_same_event_same_venue_scope(nocaps)
    n_leg_upsert_scope(
        nocaps,
        scope_id="SAME_EVENT_SAME_VENUE",
        capability="MANUAL_CANARY",
        members={"relation_type": "complement", "same_event": True, "same_venue": True, "venues": ["polymarket"]},
        base_scope_version=1,
    )
    with pytest.raises(NLegConfirmRejected) as caps:
        _confirm(nocaps, [_solution_entry()], idempotency_key="m2")
    assert caps.value.reason == "CAPS_NOT_CONFIGURED"

    # Queue rules: duplicate component + full queue (reuses B5 coverage at
    # the chain level) and incident stop-the-world (E2) are enforced upstream
    # of any submission; here prove a driver tick with an empty gate-free
    # queue is a no-op on a caps-configured store with no rows.
    from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
        _caps_store,
        _e2e_driver,
        _FakeTrading,
    )

    empty = _caps_store(tmp_path / "empty")
    trading = _FakeTrading([])
    driver = _e2e_driver(empty, trading, _solution_entry and None or trading)
    assert driver.tick(now=_E2E_AS_OF) == {"skipped": "QUEUE_EMPTY"}
    assert trading.calls == []


from test_prediction_n_leg_execution import AS_OF as _E2E_AS_OF  # noqa: E402  (fixture clock)


# --------------------------------------------------------------------------
# Issue #64 repair round 1: the REAL production chain. A real
# PredictionLiveResolver (real v2 catalog, real CP-SAT solve, real #74
# prover, real store) produces the opportunity; the resolver-retained frozen
# #51 ExecutionSolutionSource carries confirm -> queue-head admission; a
# fake trading client submits; receipts fold through the durable reducer;
# the batch completes. No fixture solution entries, no injected source
# factory material: the payloads crossing every seam are the resolver's own.
# This chain is also the live evidence that the projection's order_ready
# gate chain HOLDS for real resolver payloads (the prior round's
# EXECUTION_FINGERPRINT_MISMATCH "known boundary" was a misreport).
# --------------------------------------------------------------------------


class _HealthyAccountExecution:
    """Resolver account seam with healthy Predict balances."""

    def n_leg_account_view(self):
        from open_trader.prediction_market_solution import AccountView

        return AccountView(500_000_000, 500_000_000, 0)


def _issue64_real_chain(tmp_path: Path):
    """The real live resolver over a two-leg EXACTLY_ONE group, wired to a
    real caps-configured store (MANUAL_CANARY scope, ruling-5 caps write with
    loss caps that honestly bound the ~$8 worst one-leg partial fill of this
    chain so the real #74 prover closes PARTIAL_FILL_SAFE)."""
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_monitor_selection import (
        MonitorSelectionStore,
        SelectedComponent,
        problem_for_component,
        relation_generation_problem,
    )
    from open_trader.prediction_n_leg import fingerprint
    from open_trader.prediction_n_leg_mode import (
        ensure_same_event_same_venue_scope,
        n_leg_update_safety_config,
        n_leg_upsert_scope,
    )

    contract_ids = ["real-a", "real-b"]
    release = datetime.now(UTC) + timedelta(days=20)
    payload = _exactly_one_payload(contract_ids, ["0.40", "0.40"], release_at=release)
    catalog, _ = _activate_relation(tmp_path / "catalog", payload)

    store = PredictionArbitrageStore(tmp_path / "data")
    ensure_same_event_same_venue_scope(store)
    n_leg_upsert_scope(
        store,
        scope_id="SAME_EVENT_SAME_VENUE",
        capability="MANUAL_CANARY",
        members={
            "relation_type": "complement",
            "same_event": True,
            "same_venue": True,
            "venues": ["polymarket"],
        },
        base_scope_version=1,
    )
    n_leg_update_safety_config(
        store,
        config={
            "episode_rearm_gap_seconds": 300,
            "max_per_trade_cost_units": 50_000_000,
            "max_total_unsettled_capital_units": 200_000_000,
            "max_partial_fill_loss_units": 100_000_000,
            "max_auto_repair_loss_units": 100_000_000,
        },
        base_version=1,
    )

    problem, components = relation_generation_problem(catalog.current_generation())
    component = components[0]
    sub = problem_for_component(problem, component)
    selection_store = MonitorSelectionStore(tmp_path / "selection")
    selection_store.save(
        {
            component.component_id: SelectedComponent(
                component_id=component.component_id,
                contract_ids=component.contract_ids,
                constraint_ids=component.constraint_ids,
                action_ids=component.action_ids,
                admission_score=0,
                portfolio=(),
                relation_fingerprint=fingerprint(
                    {"constraint_model": sub.constraint_model}
                ),
                terminal_fingerprint=fingerprint(
                    {"terminal_state_sets": sub.terminal_state_sets}
                ),
                portfolio_fingerprint=fingerprint({"quantities": ()}),
                status="ACTIVE",
            )
        }
    )
    monitor = _LiveBooksMonitor(
        {c: _live_book(c, "0.40") for c in contract_ids}
    )
    resolver = PredictionLiveResolver(
        data_dir=tmp_path / "resolver",
        relation_catalog=catalog,
        monitor=monitor,
        solver_server=_RealSolverServer(),
        selection_store=selection_store,
        store=store,
        execution=_HealthyAccountExecution(),
        poll_interval=0.01,
        budget=LIVE_TEST_BUDGET,
        limits=LIVE_TEST_LIMITS,
    )
    resolver._tick()
    resolver._tick()
    return store, resolver, monitor, component.component_id


def test_issue64_real_resolver_chain_confirm_admit_submit_complete(
    tmp_path: Path,
) -> None:
    store, resolver, monitor, component_id = _issue64_real_chain(tmp_path)
    try:
        from open_trader.prediction_executable_cost import (
            execution_solution_from_payload,
        )
        from open_trader.prediction_n_leg_confirm import confirm_enqueue
        from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver
        from open_trader.prediction_n_leg_execution import (
            ConfirmedHolding,
            ReconciliationContext,
            SettlementCashFlow,
        )
        from open_trader.prediction_n_leg_read_model import (
            project_n_leg_solution,
        )
        from test_prediction_n_leg_confirm import (  # type: ignore[import-not-found]
            _FakeTrading,
        )

        entries = resolver.solutions()
        assert [str(e["component_id"]) for e in entries] == [component_id]
        entry = entries[0]
        # The real light payload: verified, executable, really proven SAFE.
        assert entry["execution"]["reason"] == "EXECUTABLE"
        assert entry["execution"]["partial_fill_proof"] == "PARTIAL_FILL_SAFE"

        # Goal 2 evidence: the projection's order_ready gate chain HOLDS for
        # the real resolver payload -- no EXECUTION_FINGERPRINT_MISMATCH.
        projection = project_n_leg_solution(
            market=entry["market"],
            execution=entry["execution"],
            scope={
                "capability": "MANUAL_CANARY",
                "order_ready": True,
                "reason": "MANUAL_CANARY",
                "action": "manual_confirm",
            },
            component_id=component_id,
            max_total_unsettled_capital_units=200_000_000,
            total_unsettled_capital_units=0,
            now=datetime.now(UTC),
            fee=entry["fee"],
        )
        assert projection is not None
        assert projection["execution"]["order_ready"] is True
        assert projection["execution"]["reason"] != "EXECUTION_FINGERPRINT_MISMATCH"

        # The retained frozen #51 source exists and re-decodes faithfully.
        material = resolver.driver_execution_source(component_id)
        assert material is not None, "resolver retained no execution source"
        assert material["partial_fill_proof"]["status"] == "PARTIAL_FILL_SAFE"
        source = material["source"]
        market = source.decode_market()
        execution = execution_solution_from_payload(
            dict(source.execution_solution_payload),
            market_solution=market,
            account_snapshot=source.account_snapshot,
            now=source.now,
        )
        assert execution.fingerprint == material["execution"]["fingerprint"]
        # Hand math for the heavy admission economics: tick 1 makes each
        # protected price 400,001; 20 lots x 2 legs -> 16,000,040 units.
        assert execution.capital_use_units == 16_000_040

        displayed = fingerprint(canonical_payload(entry["execution"]))
        result = confirm_enqueue(
            store,
            entries,
            component_id=component_id,
            displayed_fingerprint=displayed,
            idempotency_key="real-chain-1",
            now=datetime.now(UTC),
            partial_fill_proof=material["partial_fill_proof"],
            execution_source={
                "market": material["market"],
                "execution": material["execution"],
            },
        )
        assert result["state"] == "PENDING"
        # Audit block semantics (locked by B2): bound = whole-payload
        # fingerprint of the frozen heavy execution; the admission identity
        # (the solution's own fingerprint field) is re-checked on the batch.
        assert result["bound_fingerprint"] == fingerprint(
            canonical_payload(material["execution"])
        )

        def source_factory(frozen):
            material = resolver.driver_execution_source(
                str(frozen["component_id"])
            )
            if material is None:
                raise ValueError("N_LEG_SOURCE_UNAVAILABLE")
            return material["source"]

        def recon_factory(batch_id):
            batch = store.n_leg_batch(batch_id)
            now = datetime.now(UTC)
            account = replace(source.account_snapshot, captured_at=now)
            flows, holdings = [], []
            for leg in batch["legs"]:
                receipt = leg["receipt"]
                flows.append(
                    SettlementCashFlow(
                        leg["client_order_id"],
                        receipt.get("venue_order_id"),
                        leg["venue_id"],
                        leg["account_id"],
                        leg["settlement_asset_id"],
                        int(receipt["cumulative_cost_units"]),
                        int(receipt["cumulative_fee_units"]),
                        now,
                        now,
                        (
                            receipt.get("rest_observation_version")
                            if receipt.get("rest_confirmed")
                            else receipt.get("sequence")
                        ),
                        bool(receipt["rest_confirmed"]),
                    )
                )
                if int(receipt["cumulative_filled_quantity"]) > 0:
                    holdings.append(
                        ConfirmedHolding(
                            leg["venue_id"],
                            leg["account_id"],
                            leg["asset_id"],
                            int(receipt["cumulative_filled_quantity"]),
                            now,
                            now,
                        )
                    )
            return ReconciliationContext(
                f"{batch_id}:v1",
                account,
                tuple(holdings),
                tuple(flows),
                now,
                now,
                now,
            )

        trading = _FakeTrading(
            [
                {"state": "FILLED", "error_code": None, "cost_units": 8_000_000},
                {"state": "FILLED", "error_code": None, "cost_units": 8_000_000},
            ]
        )
        driver = NLegOrderQueueDriver(
            store,
            books_provider=lambda cid: resolver.driver_books_snapshot(cid),
            source_factory=source_factory,
            trading=trading,
            reconciliation_context_factory=recon_factory,
        )
        # A fresh book refresh before the driver tick mirrors production.
        monitor.books = {
            c: _live_book(c, "0.40") for c in ("real-a", "real-b")
        }
        summary = driver.tick(now=datetime.now(UTC))

        assert "abandoned" not in summary, summary
        assert len(trading.calls) == 2
        batch = store.n_leg_batch(summary["submitted"])
        assert str(batch["state"]).startswith("RECONCILED")
        # The admitted batch carries exactly the frozen heavy solution.
        assert (
            batch["execution_solution_fingerprint"]
            == material["execution"]["fingerprint"]
        )
        rows = store.n_leg_requests()
        assert [r["state"] for r in rows] == ["SUBMITTED"]
        assert all(r["abandon_reason"] is None for r in rows)
        control = store.n_leg_control()
        assert control["active_batch_id"] is None
        # The ledger keeps the conservative heavy bound (8,000,020/leg).
        assert control["total_unsettled_capital_units"] == 16_000_040
    finally:
        resolver.stop()
