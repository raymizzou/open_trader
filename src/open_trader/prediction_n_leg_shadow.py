"""Asynchronous, no-submit N-leg comparison for legacy YES/NO episodes."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from threading import Condition, RLock

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    OracleBudget,
    OracleRequest,
    PROBLEM_SCHEMA_V1,
    REQUEST_SCHEMA_V1,
    QualificationConstraint,
    QualificationMetric,
    SearchMode,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    OBSERVATION_SCHEMA_V1,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_server import SolverServerOwner
from open_trader.prediction_solver_worker import WorkerRequest
from open_trader.prediction_solver_verified import (
    VerificationStatus,
    candidate_evidence_from_payload,
    verification_result_from_payload,
    verify,
)


ShadowSubmission = Callable[[dict[str, object]], Future[Mapping[str, object]]]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"legacy {name} is required") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"legacy {name} must be positive")
    return result


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"legacy {name} is required") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"legacy {name} must be nonnegative")
    return result


def _time(value: object, name: str) -> datetime:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    raise ValueError(f"legacy {name} is required")


def _cost_slices(cost: Decimal, units_per_dollar: int, lots: int) -> tuple[ExecutableCostSlice, ...]:
    units = int((cost * units_per_dollar).to_integral_value(rounding=ROUND_CEILING))
    base, extra = divmod(units, lots)
    result: list[ExecutableCostSlice] = []
    if extra:
        result.append(ExecutableCostSlice(1, extra, base + 1))
    if extra < lots:
        result.append(ExecutableCostSlice(extra + 1, lots, base))
    return tuple(result)


def _snapshot_value(value: object, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    raise ValueError(f"legacy {name} is not canonical")


def _economic_fingerprint(opportunity: Mapping[str, object], signal_id: str) -> str:
    """Hash only economic inputs so observation timestamps never change identity."""

    economic: dict[str, object] = {
        "signal_id": signal_id,
        "market_type": str(opportunity.get("market_type", "standard_binary")),
    }
    for field in (
        "opportunity_id", "market_id", "quantity", "yes_max_cost", "no_max_cost",
        "total_max_cost", "minimum_profit", "estimated_profit", "calculable_gas",
        "canonical_cutoff", "resolution_at",
    ):
        value = _snapshot_value(opportunity.get(field), field)
        if value is not None:
            economic[field] = value
    if "minimum_profit" not in economic and "estimated_profit" in economic:
        economic["minimum_profit"] = economic["estimated_profit"]
    if opportunity.get("market_type") == "cross_venue_yes_no":
        legs = opportunity.get("legs")
        if not isinstance(legs, (tuple, list)):
            raise ValueError("legacy cross-venue legs are required")
        economic["legs"] = [
            {
                field: _snapshot_value(leg.get(field), f"leg[{index}].{field}")
                for field in (
                    "exchange", "market_id", "condition_id", "outcome", "token_id",
                    "net_quantity", "max_cost", "settlement_at",
                )
            }
            for index, leg in enumerate(legs)
            if isinstance(leg, Mapping)
        ]
        rules = opportunity.get("rules_fingerprints")
        if isinstance(rules, Mapping):
            economic["rules_fingerprints"] = {
                str(key): _snapshot_value(value, f"rules_fingerprints.{key}")
                for key, value in sorted(rules.items())
            }
    return fingerprint(canonical_payload(economic))


def legacy_shadow_snapshot(
    opportunity: Mapping[str, object], signal_id: str
) -> dict[str, object]:
    """Freeze only real legacy qualifying inputs into a stable Shadow request."""

    if not isinstance(signal_id, str) or not signal_id:
        raise ValueError("legacy signal identity is required")
    market_type = str(opportunity.get("market_type", "standard_binary"))
    fields = (
        "opportunity_id", "market_id", "quantity", "yes_max_cost", "no_max_cost",
        "total_max_cost", "minimum_payout", "minimum_profit", "estimated_profit",
        "confirmed_at", "resolution_at", "calculable_gas", "canonical_cutoff",
    )
    snapshot: dict[str, object] = {
        "signal_id": signal_id,
        "market_type": market_type,
    }
    for field in fields:
        value = _snapshot_value(opportunity.get(field), field)
        if value is not None:
            snapshot[field] = value
    if "minimum_profit" not in snapshot and "estimated_profit" in snapshot:
        snapshot["minimum_profit"] = snapshot["estimated_profit"]
    if market_type == "cross_venue_yes_no":
        legs = opportunity.get("legs")
        if not isinstance(legs, (tuple, list)):
            raise ValueError("legacy cross-venue legs are required")
        canonical_legs: list[dict[str, str | None]] = []
        for index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                raise ValueError("legacy cross-venue leg is required")
            canonical_legs.append({
                field: _snapshot_value(leg.get(field), f"leg[{index}].{field}")
                for field in (
                    "exchange", "market_id", "condition_id", "outcome", "token_id",
                    "net_quantity", "max_cost", "book_timestamp", "settlement_at",
                )
            })
        snapshot["legs"] = canonical_legs
        rules = opportunity.get("rules_fingerprints")
        if isinstance(rules, Mapping):
            snapshot["rules_fingerprints"] = {
                str(key): _snapshot_value(value, f"rules_fingerprints.{key}")
                for key, value in sorted(rules.items())
            }
    snapshot["fingerprint"] = _economic_fingerprint(opportunity, signal_id)
    return snapshot


def _missing_settlement_diagnostic(
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    fingerprint = snapshot.get("fingerprint")
    return {
        "run_status": "SUCCESS",
        "decision": "UNKNOWN",
        "comparison": "NOT_EVALUATED",
        "fingerprint": str(fingerprint) if fingerprint else "",
        "result": {"order_ready": False},
        "differences": {
            **{
                name: {"status": "na", "reason": "缺少结算证据"}
                for name in (
                    "opportunity_exists", "direction", "worst_case",
                    "net_margin_1pct", "annualized_15pct", "capital_release_30d",
                    "order_ready", "rejection_reasons",
                )
            },
            "capital_release_at": {
                "legacy": "",
                "n_leg": "",
                "reason": "缺少结算证据",
            },
        },
    }


def legacy_shadow_request(
    snapshot: Mapping[str, object],
) -> WorkerRequest | Mapping[str, object]:
    """Adapt a fixed legacy YES/NO proof into the canonical generic worker request."""

    fingerprint = snapshot.get("fingerprint")
    market_id = snapshot.get("market_id", snapshot.get("opportunity_id"))
    if not isinstance(fingerprint, str) or not fingerprint or not isinstance(market_id, str) or not market_id:
        raise ValueError("legacy snapshot identity is required")
    try:
        _time(snapshot.get("resolution_at"), "resolution_at")
    except ValueError:
        return _missing_settlement_diagnostic(snapshot)
    quantity = _decimal(snapshot.get("quantity"), "quantity")
    scale = 10 ** max(0, -quantity.as_tuple().exponent)
    lots = int(quantity * scale)
    if lots < 1:
        raise ValueError("legacy quantity is too small")
    units_per_dollar = 100_000_000 * scale
    if snapshot.get("market_type") == "cross_venue_yes_no":
        legs = snapshot.get("legs")
        if not isinstance(legs, (list, tuple)) or len(legs) != 2 or not all(isinstance(leg, Mapping) for leg in legs):
            raise ValueError("legacy cross-venue legs are required")
        costs = tuple(_decimal(leg.get("max_cost"), "leg max_cost") for leg in legs)
        gas = _nonnegative_decimal(snapshot.get("calculable_gas", "0"), "calculable_gas")
        costs = (costs[0], costs[1] + gas)
        sides = tuple(ActionSide.BUY_YES if leg.get("outcome") == "YES" else ActionSide.BUY_NO for leg in legs)
    else:
        costs = (
            _decimal(snapshot.get("yes_max_cost"), "yes_max_cost"),
            _decimal(snapshot.get("no_max_cost"), "no_max_cost"),
        )
        sides = (ActionSide.BUY_YES, ActionSide.BUY_NO)
    as_of = _time(snapshot.get("confirmed_at"), "confirmed_at")
    release_at = _time(snapshot.get("resolution_at"), "resolution_at")
    observation = SettlementObservationKey(
        OBSERVATION_SCHEMA_V1, "legacy-yes-no", str(market_id), as_of, release_at,
        "UTC", "legacy-v1",
    )
    actions = tuple(
        CandidateAction(
            f"legacy-{index}", "legacy", "legacy", "legacy", str(market_id), observation,
            side, 1, scale, lots, lots, "USD", f"legacy-usd-{units_per_dollar}",
            "legacy-yes-no-v1", _cost_slices(cost, units_per_dollar, lots),
        )
        for index, (side, cost) in enumerate(zip(sides, costs, strict=True), start=1)
    )
    yes_action = next(action.action_id for action in actions if action.side is ActionSide.BUY_YES)
    no_action = next(action.action_id for action in actions if action.side is ActionSide.BUY_NO)
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1, f"legacy:{market_id}:{fingerprint}", as_of,
        f"legacy-usd-{units_per_dollar}", actions,
        (
            TerminalStateSet(
                str(market_id), observation, "legacy-v1",
                (
                    TerminalAtom(
                        "legacy-yes", TerminalKind.NORMAL_YES, "legacy-v1",
                        (ActionPayout(yes_action, 100_000_000), ActionPayout(no_action, 0)),
                        release_at,
                    ),
                    TerminalAtom(
                        "legacy-no", TerminalKind.NORMAL_NO, "legacy-v1",
                        (ActionPayout(yes_action, 0), ActionPayout(no_action, 100_000_000)),
                        release_at,
                    ),
                ),
            ),
        ),
        ConstraintModel((), ()),
        (
            QualificationConstraint(
                "legacy-positive-profit", "legacy-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL, 1, 1,
            ),
        ),
    )
    return WorkerRequest(
        f"shadow:{snapshot.get('signal_id', 'episode')}:{fingerprint}", "cp_sat",
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(4, 4, 4)),
        BenchmarkLimits(500, 1_000, 256 * 1024 * 1024, 4),
    )


class NLegShadowClient:
    """First caller of the shared generic solver server, with strict result decoding."""

    def __init__(self, solver_server: SolverServerOwner) -> None:
        self._solver_server = solver_server

    def submit(self, snapshot: dict[str, object]) -> Future[Mapping[str, object]]:
        result: Future[Mapping[str, object]] = Future()
        fingerprint = str(snapshot.get("fingerprint", ""))
        try:
            request = legacy_shadow_request(snapshot)
            if isinstance(request, Mapping):
                result.set_result(dict(request))
                return result
            worker_future = self._solver_server.submit(request)
        except Exception as exc:
            result.set_result(_failure(fingerprint, "CONVERSION_OR_DISPATCH", exc))
            return result

        def complete(worker: Future[object]) -> None:
            try:
                outcome = worker.result()
                response = getattr(outcome, "response", None)
                if not getattr(outcome, "cleanup_proven", False):
                    result.set_result(_failure(fingerprint, "CLEANUP_UNPROVEN", None))
                    return
                if getattr(outcome, "status", None) != "OK" or response is None or response.evidence is None:
                    result.set_result(_failure(fingerprint, str(getattr(outcome, "termination", "SOLVER_FAILURE")), None))
                    return
                evidence = candidate_evidence_from_payload(response.evidence)
                verification = verification_result_from_payload(
                    verify(canonical_payload(evidence)),
                    source=evidence,
                )
                result.set_result(_comparison(snapshot, fingerprint, verification))
            except Exception as exc:
                result.set_result(_failure(fingerprint, "PROTOCOL_OR_VERIFIER", exc))

        worker_future.add_done_callback(complete)
        return result


def _failure(fingerprint: str, reason: str, error: BaseException | None) -> dict[str, object]:
    return {
        "run_status": "FAILURE",
        "decision": "UNKNOWN",
        "comparison": "FAILURE",
        "fingerprint": fingerprint,
        "reason": reason if error is None else f"{reason}: {type(error).__name__}",
    }


def _comparison(snapshot: Mapping[str, object], fingerprint: str, verification: object) -> dict[str, object]:
    status = getattr(verification, "status", None)
    decision = status.value if isinstance(status, VerificationStatus) else "UNKNOWN"
    if status is not VerificationStatus.QUALIFIED_VERIFIED:
        reason = (
            "缺少已验证解"
            if status is None
            else f"N_LEG 状态 {decision}"
        )
        return {
            "run_status": "SUCCESS",
            "decision": decision,
            "comparison": "DIFFERENCE",
            "fingerprint": fingerprint,
            "result": {"order_ready": False},
            "differences": {
                **{
                    name: {"status": "na", "reason": reason}
                    for name in (
                        "opportunity_exists", "direction", "worst_case",
                        "net_margin_1pct", "annualized_15pct", "capital_release_30d",
                        "order_ready", "rejection_reasons",
                    )
                },
                "decision": ["QUALIFIED_VERIFIED", decision],
            },
        }
    solution = getattr(verification, "solution", None)
    proof = getattr(solution, "payout_proof", None)
    if proof is None:
        return _failure(fingerprint, "VERIFIER_MISSING_SOLUTION", None)
    scale = 10 ** max(0, -_decimal(snapshot.get("quantity"), "quantity").as_tuple().exponent)
    units_per_dollar = Decimal(100_000_000 * scale)
    if not all(
        isinstance(getattr(proof, field, None), int)
        for field in ("cost_upper_bound_units", "guaranteed_profit_units")
    ):
        return _failure(fingerprint, "VERIFIER_INVALID_PROFIT", None)
    legacy_cost_value = snapshot.get("total_max_cost")
    if legacy_cost_value is None:
        legacy_cost_value = (
            _decimal(snapshot.get("yes_max_cost"), "yes_max_cost")
            + _decimal(snapshot.get("no_max_cost"), "no_max_cost")
        )
    legacy_cost = _decimal(legacy_cost_value, "total_max_cost")
    legacy_profit = _decimal(snapshot.get("minimum_profit"), "minimum_profit")
    n_leg_cost = Decimal(proof.cost_upper_bound_units) / units_per_dollar
    n_leg_profit = Decimal(proof.guaranteed_profit_units) / units_per_dollar
    differences: dict[str, object] = {}
    _add_decimal_difference(differences, "maximum_cost", legacy_cost, n_leg_cost)
    _add_decimal_difference(differences, "minimum_profit", legacy_profit, n_leg_profit)
    expected_lots = int(_decimal(snapshot.get("quantity"), "quantity") * scale)
    quantities = getattr(solution, "quantities", ())
    actual_lots = tuple(getattr(item, "quantity_lots", None) for item in quantities)
    if not actual_lots or any(lots != expected_lots for lots in actual_lots):
        differences["quantity"] = {
            "legacy": str(expected_lots),
            "n_leg": ",".join(str(lots) for lots in actual_lots),
            "absolute": str(max((abs(expected_lots - lots) for lots in actual_lots if isinstance(lots, int)), default=expected_lots)),
        }
    expected_release = _time(snapshot.get("resolution_at"), "resolution_at")
    actual_release = getattr(proof, "conservative_capital_release_at", None)
    if not isinstance(actual_release, datetime) or actual_release.astimezone(UTC) != expected_release:
        differences["capital_release_at"] = {
            "legacy": _timestamp(expected_release),
            "n_leg": _timestamp(actual_release) if isinstance(actual_release, datetime) else "",
        }
    differences["opportunity_exists"] = {
        "legacy": "是", "n_leg": "是", "status": "consistent",
    }
    expected_sides = _legacy_direction(snapshot)
    sides_by_action = {
        f"legacy-{index}": side for index, side in enumerate(expected_sides, start=1)
    }
    actual_direction = tuple(
        sides_by_action.get(getattr(item, "action_id", ""), "")
        for item in getattr(solution, "quantities", ())
    )
    differences["direction"] = {
        "legacy": ",".join(expected_sides),
        "n_leg": ",".join(str(item) for item in actual_direction),
        "status": (
            "consistent"
            if len(actual_direction) == len(expected_sides)
            and all(actual == expected for actual, expected in zip(actual_direction, expected_sides, strict=True))
            else "difference"
        ),
    }
    worst = _scenario_label(getattr(proof, "worst_scenario", None))
    differences["worst_case"] = (
        {"legacy": "旧路径未提供", "n_leg": worst, "status": "na"}
        if worst is not None
        else {"status": "na", "reason": "缺少最坏状态证据"}
    )
    _add_gate_difference(differences, "net_margin_1pct", _net_margin(legacy_profit, legacy_cost), _net_margin(n_leg_profit, n_leg_cost))
    _add_gate_difference(
        differences,
        "annualized_15pct",
        _annualized_gate(snapshot, legacy_profit, legacy_cost, expected_release),
        _annualized_gate(snapshot, n_leg_profit, n_leg_cost, actual_release),
    )
    _add_gate_difference(
        differences,
        "capital_release_30d",
        _release_within_30d(snapshot, expected_release),
        _release_within_30d(snapshot, actual_release),
    )
    differences["order_ready"] = {"legacy": "是", "n_leg": "是", "status": "consistent"}
    rejections = getattr(proof, "rejection_counts", ())
    differences["rejection_reasons"] = (
        {"legacy": "无", "n_leg": "无", "status": "consistent"}
        if not rejections
        else {
            "legacy": "无",
            "n_leg": ",".join(f"{count}×{name}" for name, count in rejections),
            "status": "difference",
        }
    )
    comparison = (
        "CONSISTENT"
        if all(
            not isinstance(value, Mapping)
            or value.get("status") in ("consistent", "na")
            for value in differences.values()
        )
        else "DIFFERENCE"
    )
    return {
        "run_status": "SUCCESS",
        "decision": decision,
        "comparison": comparison,
        "fingerprint": fingerprint,
        "result": {
            "order_ready": True,
            "quantity": format(_decimal(snapshot.get("quantity"), "quantity"), "f"),
            "maximum_cost": format(n_leg_cost, "f"),
            "minimum_profit": format(n_leg_profit, "f"),
            "capital_release_at": _timestamp(actual_release) if isinstance(actual_release, datetime) else None,
        },
        **({"differences": differences} if differences else {}),
    }


def _legacy_direction(snapshot: Mapping[str, object]) -> tuple[str, ...]:
    if snapshot.get("market_type") == "cross_venue_yes_no":
        legs = snapshot.get("legs")
        if not isinstance(legs, (tuple, list)) or len(legs) != 2:
            return ()
        # Expected per-leg sides in canonical leg order; action ids are assigned
        # by leg order, so comparing ids would only prove leg count.
        return tuple(
            "YES" if leg.get("outcome") == "YES" else "NO"
            for leg in legs
            if isinstance(leg, Mapping)
        )
    return ("YES", "NO")


def _scenario_label(scenario: object) -> str | None:
    atoms = getattr(scenario, "atoms", None)
    if not isinstance(atoms, (tuple, list)) or not atoms:
        return None
    return ",".join(str(getattr(atom, "atom_id", "")) for atom in atoms if atom is not None)


def _net_margin(profit: Decimal, cost: Decimal) -> bool | None:
    if cost <= 0:
        return None
    return profit / cost >= Decimal("0.01")


def _annualized_gate(
    snapshot: Mapping[str, object],
    profit: Decimal,
    cost: Decimal,
    release_at: object,
) -> bool | None:
    try:
        start = _time(snapshot.get("confirmed_at"), "confirmed_at")
        release = _time(release_at, "release_at")
    except ValueError:
        return None
    seconds = (release.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
    if cost <= 0 or seconds <= 0:
        return None
    return profit / cost * Decimal(365) * Decimal(86400) / Decimal(seconds) >= Decimal("0.15")


def _release_within_30d(snapshot: Mapping[str, object], release_at: object) -> bool | None:
    try:
        start = _time(snapshot.get("confirmed_at"), "confirmed_at")
        release = _time(release_at, "release_at")
    except ValueError:
        return None
    return (release.astimezone(UTC) - start.astimezone(UTC)).total_seconds() <= 30 * 86400


def _add_gate_difference(
    differences: dict[str, object],
    name: str,
    legacy: bool | None,
    n_leg: bool | None,
) -> None:
    if legacy is None or n_leg is None:
        differences[name] = {
            "status": "na",
            "reason": "缺少结算证据" if legacy is None and n_leg is None else "无法计算",
        }
        return
    differences[name] = {
        "legacy": "达标" if legacy else "未达标",
        "n_leg": "达标" if n_leg else "未达标",
        "status": "consistent" if legacy == n_leg else "difference",
    }


def _add_decimal_difference(
    differences: dict[str, object], name: str, legacy: Decimal, n_leg: Decimal
) -> None:
    if legacy == n_leg:
        return
    differences[name] = {
        "legacy": format(legacy, "f"),
        "n_leg": format(n_leg, "f"),
        "absolute": format(abs(legacy - n_leg), "f"),
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class NLegShadowScheduler:
    """Keep one running comparison per Episode and retain only its latest input."""

    def __init__(
        self,
        store: PredictionArbitrageStore,
        *,
        submit_snapshot: ShadowSubmission,
    ) -> None:
        self._store = store
        self._submit_snapshot = submit_snapshot
        self._lock = RLock()
        self._idle = Condition(self._lock)
        self._episodes: dict[str, dict[str, object]] = {}
        self._closed = False

    def schedule(self, signal_id: str, snapshot: Mapping[str, object]) -> str:
        """Queue a snapshot without waiting for conversion, solver, or verifier work."""

        fingerprint = snapshot.get("fingerprint")
        if not isinstance(signal_id, str) or not signal_id or not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("shadow schedule requires signal_id and snapshot fingerprint")
        frozen = copy.deepcopy(dict(snapshot))
        with self._lock:
            if self._closed:
                return "closed"
            state = self._episodes.setdefault(signal_id, {"running": False, "queued": None})
            summary = self._summary(signal_id)
            if (
                summary.get("latest_fingerprint") == fingerprint
                and isinstance(summary.get("latest_result"), Mapping)
            ):
                return "deduped"
            if state.get("latest_fingerprint") == fingerprint:
                return "deduped"
            queued = state.get("queued")
            if isinstance(queued, Mapping) and queued.get("fingerprint") == fingerprint:
                return "deduped"
            state["latest_fingerprint"] = fingerprint
            self._store_summary(signal_id, summary, latest_fingerprint=fingerprint)
            if state["running"]:
                state["queued"] = frozen
                return "scheduled"
            state["running"] = True
            self._submit(signal_id, frozen)
            return "scheduled"

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._idle:
            return self._idle.wait_for(
                lambda: not any(state.get("running") for state in self._episodes.values()),
                timeout=timeout,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for state in self._episodes.values():
                state["queued"] = None
                future = state.get("future")
                if isinstance(future, Future):
                    future.cancel()

    def _submit(self, signal_id: str, snapshot: dict[str, object]) -> None:
        try:
            future = self._submit_snapshot(snapshot)
        except Exception as exc:
            future = Future()
            future.set_exception(exc)
        if not isinstance(future, Future):
            failure: Future[Mapping[str, object]] = Future()
            failure.set_exception(ValueError("shadow submission must return Future"))
            future = failure
        self._episodes[signal_id]["future"] = future
        future.add_done_callback(
            lambda completed: self._completed(signal_id, snapshot, completed)
        )

    def _completed(
        self,
        signal_id: str,
        snapshot: Mapping[str, object],
        completed: Future[Mapping[str, object]],
    ) -> None:
        fingerprint = str(snapshot["fingerprint"])
        with self._idle:
            if self._closed:
                state = self._episodes.get(signal_id)
                if state is not None:
                    state["running"] = False
                self._idle.notify_all()
                return
        try:
            result = dict(completed.result())
        except Exception as exc:
            result = {
                "run_status": "FAILURE",
                "decision": "UNKNOWN",
                "comparison": "FAILURE",
                "fingerprint": fingerprint,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        result["fingerprint"] = fingerprint
        with self._idle:
            state = self._episodes[signal_id]
            summary = self._summary(signal_id)
            latest = state.get("latest_fingerprint") == fingerprint
            self._store_summary(
                signal_id,
                summary,
                result=result,
                apply_latest=latest,
            )
            queued = state.get("queued")
            state["queued"] = None
            if isinstance(queued, Mapping) and not self._closed:
                self._submit(signal_id, dict(queued))
            else:
                state["running"] = False
                self._idle.notify_all()

    def _summary(self, signal_id: str) -> dict[str, object]:
        signal = self._store.signal(signal_id) or {}
        current = signal.get("n_leg_shadow")
        return dict(current) if isinstance(current, Mapping) else {}

    def _store_summary(
        self,
        signal_id: str,
        summary: dict[str, object],
        *,
        latest_fingerprint: str | None = None,
        result: Mapping[str, object] | None = None,
        apply_latest: bool = False,
    ) -> None:
        now = _now()
        for field in ("run_count", "qualified_count", "difference_count", "failure_count"):
            summary.setdefault(field, 0)
        summary.setdefault("current_differences", {})
        summary.setdefault("max_differences", {})
        if latest_fingerprint is not None:
            summary["latest_fingerprint"] = latest_fingerprint
            summary.setdefault("status", "PENDING")
        if result is not None:
            summary["first_run_at"] = summary.get("first_run_at") or now
            summary["last_run_at"] = now
            summary["run_count"] = int(summary.get("run_count", 0)) + 1
            decision = result.get("decision")
            comparison = result.get("comparison")
            if decision == "QUALIFIED_VERIFIED":
                summary["qualified_count"] = int(summary.get("qualified_count", 0)) + 1
            if comparison == "DIFFERENCE":
                summary["difference_count"] = int(summary.get("difference_count", 0)) + 1
            if comparison == "FAILURE":
                summary["failure_count"] = int(summary.get("failure_count", 0)) + 1
            if apply_latest:
                summary["latest_result"] = dict(result)
                summary["status"] = str(result.get("comparison", "FAILURE"))
                differences = result.get("differences")
                summary["current_differences"] = (
                    copy.deepcopy(dict(differences)) if isinstance(differences, Mapping) else {}
                )
                summary["max_differences"] = _max_differences(
                    summary.get("max_differences"),
                    summary["current_differences"],
                )
        self._store.update_signal(signal_id, {"n_leg_shadow": summary})


def _max_differences(existing: object, current: Mapping[str, object]) -> dict[str, object]:
    """Keep the largest independently reported difference per economic key."""

    maximum = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
    for key, value in current.items():
        if not isinstance(value, Mapping):
            continue
        absolute = value.get("absolute")
        try:
            amount = abs(Decimal(str(absolute)))
        except Exception:
            continue
        prior = maximum.get(key)
        try:
            prior_amount = abs(Decimal(str(prior.get("absolute")))) if isinstance(prior, Mapping) else Decimal("-1")
        except Exception:
            prior_amount = Decimal("-1")
        if amount >= prior_amount:
            maximum[key] = copy.deepcopy(dict(value))
    return maximum
