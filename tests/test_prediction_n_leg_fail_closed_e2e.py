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
1,000,000 micro-units. Fee/tick stay hardwired 0 in the live chain (ponytail
in ``prediction_live_resolver._snapshot_for``); fee rate 0 is a KNOWN value, so
it never blocks qualification — the fee-unknown fail-closed case below covers
the cost-missing half of that pairing.
"""

from __future__ import annotations

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
) -> dict[str, object]:
    """Wrap a market payload into the resolver-shaped solution entry."""
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
    """Fee/cost unknown fails closed: the live chain hardwires the taker fee
    at a KNOWN 0 (``prediction_live_resolver._snapshot_for`` builds every leg
    book with ``taker_fee_bps=Decimal("0")`` and ``build_solve_request`` keeps
    fee_ppm=0), so a fee number always exists there and the fail-closed half
    of the fee pair is a missing worst-case cost: with ``bounded_cost_units``
    absent the projection cannot know the worst-case cost, every presentable
    number would be invented, and the qualification must be UNKNOWN — never
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
) -> dict[str, object]:
    """One COMPLETE EXACTLY_ONE discovery payload over ``contract_ids``.
    ``prices`` document the intended book per leg (the live books are seeded
    separately); the compiled problem is the binary settlement view locked by
    the oracle corpus — the solvable N-leg shape. ``relation_type`` selects
    the identity prefix (EXACTLY_ONE groups vs NATIVE_COMPLEMENT pairs); the
    compiled constraint model stays EXACTLY_ONE, exactly like the mechanical
    complement codec's own compiled model."""
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
) -> tuple[PredictionLiveResolver, list[str]]:
    """Seed the #77 selection exactly like the no-submit validation harness:
    one component per compiled relation group with its structural
    fingerprints, then the resolver over the real catalog object."""
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
    observe-only (src/open_trader/prediction_n_leg_read_model.py). The chain
    runs on the hardwired KNOWN fee 0 (see module docstring), which qualifies. """
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
    result = catalog.ingest_mechanical_relation(group_relation(4))
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
    result = catalog.ingest_mechanical_relation(complement_relation())
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
    16,000,000; margin 0.2; annualized 3.65; release 20d; the chain's
    hardwired KNOWN taker fee 0 — see A4 — does not block). Even so the row
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
