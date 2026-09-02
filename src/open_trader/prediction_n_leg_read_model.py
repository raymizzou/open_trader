"""Issue #85: read-model projection of N_LEG Market/Execution solutions.

The projection is pure and stateless: it consumes the serialized #84
``MarketSolution`` / ``ExecutionSolution`` payloads (``canonical_payload``),
the merged n_leg scope contract + readiness entry, and the current unsettled
capital, and emits the dashboard-facing market/execution fields.  ORDER_READY
is bound to the execution solution fingerprint and the scope capability; it
never reuses the legacy YES/NO balance judgment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from open_trader.prediction_market_solution import EXECUTABLE_REASON
from open_trader.prediction_n_leg import canonical_payload, fingerprint
from open_trader.prediction_n_leg_mode import DEFAULT_QUALIFICATION_POLICY


_NLEG_UNITS_PER_DOLLAR = Decimal("1000000")
_NLEG_UNITS_PER_DOLLAR_INT = 1_000_000

PARTIAL_FILL_PROOF_REQUIRED = "PARTIAL_FILL_PROOF_REQUIRED"
PARTIAL_FILL_UNSAFE = "PARTIAL_FILL_UNSAFE"
SCOPE_OBSERVE_ONLY = "SCOPE_OBSERVE_ONLY"
EXECUTION_FINGERPRINT_MISMATCH = "EXECUTION_FINGERPRINT_MISMATCH"
UNSETTLED_CAP_EXCEEDED = "UNSETTLED_CAP_EXCEEDED"

PARTIAL_FILL_SAFE = "PARTIAL_FILL_SAFE"

_READY_CAPABILITIES = ("MANUAL_CANARY", "AUTO_ELIGIBLE")

# #104 qualification statuses / funding statuses / blocked reasons.
QUALIFIED_VERIFIED = "QUALIFIED_VERIFIED"
NOT_QUALIFIED = "NOT_QUALIFIED"
QUALIFICATION_UNKNOWN = "UNKNOWN"

FUNDING_SUFFICIENT = "SUFFICIENT"
FUNDING_INSUFFICIENT = "INSUFFICIENT"
FUNDING_UNKNOWN = "UNKNOWN"

INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
INSUFFICIENT_ALLOWANCE = "INSUFFICIENT_ALLOWANCE"

BLOCKED_NOT_QUALIFIED = "NOT_QUALIFIED"
BLOCKED_QUALIFICATION_UNKNOWN = "QUALIFICATION_UNKNOWN"
BLOCKED_FUNDING_UNKNOWN = "FUNDING_UNKNOWN"

# Issue #112: per-component fee states reported by the live resolver on each
# solution entry's "fee" block. Anything that is not a proven fee-free market
# fails closed: a missing/shapeless block reads as fee_unknown forever.
FEE_STATE_FREE = "fee_free"
FEE_STATE_CHARGING = "fee_charging"
FEE_STATE_UNKNOWN = "fee_unknown"

FEE_CHARGING_UNMODELED = "FEE_CHARGING_UNMODELED"
FEE_UNKNOWN = "FEE_UNKNOWN"

OPTIMAL = "OPTIMAL"
QUALIFIED_FEASIBLE = "QUALIFIED_FEASIBLE"


def _fee_state(fee: Mapping[str, object] | None) -> str:
    """Fee state of one solution entry; unknown unless proven fee-free."""
    if not isinstance(fee, Mapping):
        return FEE_STATE_UNKNOWN
    status = str(fee.get("status") or "")
    if status in (FEE_STATE_FREE, FEE_STATE_CHARGING, FEE_STATE_UNKNOWN):
        return status
    return FEE_STATE_UNKNOWN


def would_submit_predicate(
    execution_reason: object | None, qualification_status: object | None
) -> bool:
    """Shared #104/#106 would-submit semantics: an execution solution exists
    (a non-None reason carrier) whose reason is EXECUTABLE and whose
    qualification status is QUALIFIED_VERIFIED. Read-time funding gates stay
    out of it (they only shape the funding/executable blocks)."""
    return (
        execution_reason is not None
        and str(execution_reason) == EXECUTABLE_REASON
        and str(qualification_status) == QUALIFIED_VERIFIED
    )

_MICROSECONDS_PER_DAY = 86_400_000_000


def _units_to_dollars(value: object) -> str | None:
    try:
        units = int(value or 0)
    except (TypeError, ValueError):
        return None
    return format(Decimal(units) / _NLEG_UNITS_PER_DOLLAR, "f")


def _units_to_dollars_strict(value: object) -> str | None:
    """Dollar string for present integer units; ``None`` when units are absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        units = int(value)
    except (TypeError, ValueError):
        return None
    return format(Decimal(units) / _NLEG_UNITS_PER_DOLLAR, "f")


def _units_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _integer_ratio(value: object) -> tuple[int, int] | None:
    """Parse a decimal string policy threshold into an exact integer ratio."""
    parsed = _decimal_or_none(value)
    if parsed is None or parsed < 0:
        return None
    return parsed.as_integer_ratio()


def _parse_release_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _normalized_policy(qualification_policy: Mapping[str, object] | None) -> dict[str, object]:
    """Snapshot the policy in force, defaulting missing/invalid thresholds."""
    source = qualification_policy
    if isinstance(source, Mapping) and isinstance(source.get("policy"), Mapping):
        source = source["policy"]
    policy = dict(DEFAULT_QUALIFICATION_POLICY)
    if not isinstance(source, Mapping):
        return policy
    for key in DEFAULT_QUALIFICATION_POLICY:
        value = source.get(key)
        if key == "max_capital_release_days":
            if type(value) is int and value >= 1:
                policy[key] = value
        elif isinstance(value, str):
            parsed = _decimal_or_none(value)
            if parsed is not None and parsed >= 0:
                policy[key] = value
    return policy


def _qualification_projection(
    market: Mapping[str, object],
    *,
    now: datetime | None,
    policy: Mapping[str, object],
    fee_state: str = FEE_STATE_UNKNOWN,
) -> dict[str, object]:
    """#104 qualification profile: fixed-point checks, worst case, optimality.

    Issue #112 adds the fifth ``fee_status`` check: only a proven fee-free
    component passes; a charging or unknown fee state stays undecidable
    (``passed=None``), which keeps the whole qualification UNKNOWN.
    """
    profit_units = _units_or_none(market.get("guaranteed_profit_units"))
    payout_units = _units_or_none(market.get("bounded_payout_units"))
    cost_units = _units_or_none(market.get("bounded_cost_units"))
    release_at = _parse_release_at(market.get("capital_release_at"))

    min_profit_passed: bool | None = None
    profit_ratio = _integer_ratio(policy.get("min_profit_usd"))
    if profit_units is not None and profit_ratio is not None:
        threshold_num, threshold_den = profit_ratio
        min_profit_passed = (
            profit_units * threshold_den >= threshold_num * _NLEG_UNITS_PER_DOLLAR_INT
        )

    net_margin: str | None = None
    net_margin_passed: bool | None = None
    margin_ratio = _integer_ratio(policy.get("min_net_margin"))
    if (
        profit_units is not None
        and payout_units is not None
        and margin_ratio is not None
    ):
        # net margin = guaranteed profit / bounded payout; ratio gate by
        # exact integer cross-multiplication (equality passes).
        margin_num, margin_den = margin_ratio
        net_margin_passed = profit_units * margin_den >= payout_units * margin_num
        if payout_units > 0:
            net_margin = format(
                Decimal(profit_units) / Decimal(payout_units), "f"
            )

    annualized_return: str | None = None
    annualized_passed: bool | None = None
    annualized_ratio = _integer_ratio(policy.get("min_annualized_return"))

    capital_release_days: int | None = None
    capital_release_passed: bool | None = None
    if now is not None and release_at is not None:
        release_in_future = release_at > now
        if release_in_future:
            elapsed = (release_at - now) // timedelta(microseconds=1)
            # 24h ceiling with a 1-day floor; strict past fails the check.
            capital_release_days = max(1, -(-elapsed // _MICROSECONDS_PER_DAY))
        max_release_days = policy.get("max_capital_release_days")
        if type(max_release_days) is int:
            # Equality passes; capital_release_at must be strictly future.
            capital_release_passed = (
                release_in_future and capital_release_days <= max_release_days
            )

    if (
        profit_units is not None
        and payout_units is not None
        and capital_release_days is not None
        and annualized_ratio is not None
    ):
        # annualized = net margin x 365 / capital release days, gated by
        # exact integer cross-multiplication (equality passes).
        annualized_num, annualized_den = annualized_ratio
        annualized_passed = (
            profit_units * 365 * annualized_den
            >= payout_units * capital_release_days * annualized_num
        )
        if payout_units > 0:
            try:
                annualized_return = format(
                    Decimal(profit_units)
                    * Decimal(365)
                    / (Decimal(payout_units) * capital_release_days),
                    "f",
                )
            except InvalidOperation:
                annualized_return = None

    # Fee check (#112): only a proven fee-free component passes; charging or
    # unknown stays undecidable (None), which keeps the qualification UNKNOWN.
    fee_passed: bool | None = True if fee_state == FEE_STATE_FREE else None
    passed_values = (
        min_profit_passed,
        net_margin_passed,
        annualized_passed,
        capital_release_passed,
        fee_passed,
    )
    if any(passed is None for passed in passed_values) or cost_units is None:
        # Missing bounded cost leaves worst-case cost unknowable; qualification
        # stays UNKNOWN even though the margin ratios do not consume it.
        status = QUALIFICATION_UNKNOWN
    elif any(passed is False for passed in passed_values):
        status = NOT_QUALIFIED
    else:
        status = QUALIFIED_VERIFIED
    checks = [
        {
            "key": "min_profit",
            "label": "Min profit",
            "passed": min_profit_passed,
            "value": _units_to_dollars_strict(profit_units),
            "threshold": policy.get("min_profit_usd"),
        },
        {
            "key": "net_margin",
            "label": "Net margin",
            "passed": net_margin_passed,
            "value": net_margin,
            "threshold": policy.get("min_net_margin"),
        },
        {
            "key": "annualized_return",
            "label": "Annualized return",
            "passed": annualized_passed,
            "value": annualized_return,
            "threshold": policy.get("min_annualized_return"),
        },
        {
            "key": "capital_release",
            "label": "Capital release",
            "passed": capital_release_passed,
            "value": capital_release_days,
            "threshold": policy.get("max_capital_release_days"),
        },
        {
            "key": "fee_status",
            "label": "Fee status",
            "passed": fee_passed,
            "value": fee_state,
            "threshold": FEE_STATE_FREE,
        },
    ]
    return {
        "policy": dict(policy),
        "status": status,
        "checks": checks,
        "net_margin": net_margin,
        "annualized_return": annualized_return,
        "capital_release_days": capital_release_days,
        "worst_case": {
            "minimum_payout": _units_to_dollars_strict(payout_units),
            "maximum_cost": _units_to_dollars_strict(cost_units),
            "guaranteed_profit": _units_to_dollars_strict(profit_units),
        },
        "optimality": (
            OPTIMAL if market.get("global_search_closed") else QUALIFIED_FEASIBLE
        ),
    }


def _quantity_rows(quantities: object) -> list[dict[str, object]]:
    if not isinstance(quantities, (list, tuple)):
        return []
    rows: list[dict[str, object]] = []
    for quantity in quantities:
        if not isinstance(quantity, Mapping):
            continue
        rows.append(
            {
                "action_id": str(quantity.get("action_id") or ""),
                "quantity_lots": int(quantity.get("quantity_lots") or 0),
                "max_price": None,
                "max_cost": None,
                "venue": None,
                "outcome": None,
                "settlement_asset": None,
            }
        )
    return rows


def _merge_leg_display(
    rows: Sequence[Mapping[str, object]],
    legs: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    """Overlay optional per-leg book display facts (no invented prices)."""
    if not legs:
        return [dict(row) for row in rows]
    by_key: dict[str, Mapping[str, object]] = {}
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        key = str(leg.get("leg_id") or leg.get("action_id") or "")
        if key:
            by_key.setdefault(key, leg)
    merged: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        display = by_key.get(str(item.get("action_id") or ""))
        if display is not None:
            for field in ("max_price", "max_cost", "venue", "outcome", "settlement_asset"):
                if display.get(field) not in (None, ""):
                    item[field] = display.get(field)
        merged.append(item)
    return merged


def _funding_projection(
    market_legs: Sequence[Mapping[str, object]],
    balance_snapshot: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, object]:
    """#104 per-venue balance/allowance diagnostics over projected legs."""
    required_by_venue: dict[str, Decimal] = {}
    unknown = not market_legs
    for leg in market_legs:
        venue = leg.get("venue")
        cost = leg.get("max_cost")
        if venue in (None, "") or cost in (None, ""):
            unknown = True
            continue
        required = _decimal_or_none(cost)
        if required is None:
            unknown = True
            continue
        venue_key = str(venue)
        required_by_venue[venue_key] = (
            required_by_venue.get(venue_key, Decimal("0")) + required
        )
    venues: dict[str, dict[str, object]] = {}
    any_insufficient = False
    for venue_key, required in required_by_venue.items():
        entry = (
            balance_snapshot.get(venue_key)
            if isinstance(balance_snapshot, Mapping)
            else None
        )
        available: Decimal | None = None
        allowance: Decimal | None = None
        balance_ok: bool | None = None
        allowance_ok: bool | None = None
        if isinstance(entry, Mapping):
            available = _decimal_or_none(entry.get("available"))
            allowance = _decimal_or_none(entry.get("allowance"))
            if available is not None:
                balance_ok = required <= available
            if allowance is not None:
                allowance_ok = required <= allowance
        if available is None or allowance is None:
            # A venue with unknown balance or allowance is UNKNOWN: neither a
            # pass nor a failure.
            unknown = True
        reasons: list[str] = []
        if balance_ok is False:
            reasons.append(INSUFFICIENT_BALANCE)
        if allowance_ok is False:
            reasons.append(INSUFFICIENT_ALLOWANCE)
        if reasons:
            any_insufficient = True
        venues[venue_key] = {
            "required": format(required, "f"),
            "available": None if available is None else format(available, "f"),
            "allowance": None if allowance is None else format(allowance, "f"),
            "balance_ok": balance_ok,
            "allowance_ok": allowance_ok,
            "reasons": reasons,
        }
    if unknown:
        status = FUNDING_UNKNOWN
    elif any_insufficient:
        status = FUNDING_INSUFFICIENT
    else:
        status = FUNDING_SUFFICIENT
    return {"status": status, "venues": venues}


def _first_insufficient_reason(funding: Mapping[str, object]) -> str | None:
    """First triggering venue's reason; balance outranks allowance."""
    venues = funding.get("venues")
    if not isinstance(venues, Mapping):
        return None
    for row in venues.values():
        if isinstance(row, Mapping):
            reasons = row.get("reasons")
            if isinstance(reasons, list) and reasons:
                return str(reasons[0])
    return None


def project_n_leg_solution(
    *,
    market: Mapping[str, object] | None,
    execution: Mapping[str, object] | None,
    scope: Mapping[str, object] | None,
    component_id: str | None = None,
    max_total_unsettled_capital_units: int,
    total_unsettled_capital_units: int = 0,
    legs: Sequence[Mapping[str, object]] | None = None,
    partial_fill_proof: Mapping[str, object] | None = None,
    now: datetime | None = None,
    qualification_policy: Mapping[str, object] | None = None,
    balance_snapshot: Mapping[str, Mapping[str, object]] | None = None,
    fee: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Project one solution into dashboard market/execution fields.

    ``scope`` is the merged n_leg entry: ``capability`` from the mode contract
    plus ``order_ready``/``reason``/``action`` from ``n_leg_order_readiness``.
    ``partial_fill_proof`` is the optional #74 proof record payload; when
    absent the projection falls back to the ``partial_fill_proof`` status
    string carried by the execution payload.  ``fee`` is the #112 fee block
    carried by the resolver's solution entry; a missing block (or one without
    a usable status) is a permanent fee_unknown.  Returns ``None`` when no
    MarketSolution exists.
    """
    if not isinstance(market, Mapping):
        return None
    fee_state = _fee_state(fee)
    scope = dict(scope) if isinstance(scope, Mapping) else {}
    capability = str(scope.get("capability") or SCOPE_OBSERVE_ONLY)
    scope_ready = bool(scope.get("order_ready"))
    scope_reason = str(scope.get("reason") or "")
    scope_blocked = capability not in _READY_CAPABILITIES

    quantities = market.get("quantities")
    market_legs = _merge_leg_display(_quantity_rows(quantities), legs)
    market_fields = {
        "minimum_profit": _units_to_dollars(market.get("guaranteed_profit_units")),
        "maximum_cost": _units_to_dollars(market.get("bounded_cost_units")),
        "capital_release_at": market.get("capital_release_at"),
        "structure_fingerprint": market.get("structure_fingerprint"),
        "quote_fingerprint": market.get("quote_fingerprint"),
        "verification_fingerprint": market.get("verification_fingerprint"),
        "global_search_closed": market.get("global_search_closed"),
        "legs": market_legs,
    }
    qualification = _qualification_projection(
        market,
        now=now,
        policy=_normalized_policy(qualification_policy),
        fee_state=fee_state,
    )
    funding = _funding_projection(market_legs, balance_snapshot)
    main_list = qualification["status"] == QUALIFIED_VERIFIED
    if not main_list:
        executable: bool | None = False
        blocked_reason: str | None = (
            BLOCKED_NOT_QUALIFIED
            if qualification["status"] == NOT_QUALIFIED
            else BLOCKED_QUALIFICATION_UNKNOWN
        )
    elif funding["status"] == FUNDING_SUFFICIENT:
        executable = True
        blocked_reason = None
    elif funding["status"] == FUNDING_INSUFFICIENT:
        executable = False
        blocked_reason = _first_insufficient_reason(funding)
    else:
        # Funding UNKNOWN leaves executability undecidable, not blocked.
        executable = None
        blocked_reason = BLOCKED_FUNDING_UNKNOWN

    execution_payload = (
        dict(execution) if isinstance(execution, Mapping) else None
    )
    execution_solution_fingerprint = (
        fingerprint(canonical_payload(execution_payload))
        if execution_payload is not None
        else None
    )
    # #104: would_submit applies the same qualification policy as the future
    # real execution path; read-time funding gates stay out of it (they only
    # shape the funding/executable blocks).
    would_submit = would_submit_predicate(
        execution_payload.get("reason") if execution_payload is not None else None,
        qualification["status"],
    )
    projected_total_units = int(execution_payload.get("capital_use_units") or 0) if execution_payload is not None else 0
    projected_with_unsettled = projected_total_units + int(total_unsettled_capital_units or 0)

    # #74 three-state proof status: explicit record payload wins, otherwise
    # the status string carried by the execution solution.
    proof_payload = (
        dict(partial_fill_proof)
        if isinstance(partial_fill_proof, Mapping)
        else None
    )
    proof_status = (
        str(proof_payload.get("status") or "")
        if proof_payload is not None
        else (
            str(execution_payload.get("partial_fill_proof") or "")
            if execution_payload is not None
            else ""
        )
    )

    order_ready = False
    reason = ""
    if fee_state != FEE_STATE_FREE:
        # Issue #112: the fee veto is the first gate of the chain -- a
        # charging or fee-unknown component never unlocks ordering, ahead of
        # even the scope capability block.
        reason = (
            FEE_CHARGING_UNMODELED
            if fee_state == FEE_STATE_CHARGING
            else FEE_UNKNOWN
        )
    elif scope_blocked:
        reason = SCOPE_OBSERVE_ONLY
    elif execution_payload is None:
        reason = PARTIAL_FILL_PROOF_REQUIRED
    elif str(execution_payload.get("reason") or "") not in (EXECUTABLE_REASON, ""):
        reason = str(execution_payload.get("reason") or PARTIAL_FILL_PROOF_REQUIRED)
    elif proof_status == PARTIAL_FILL_UNSAFE:
        reason = PARTIAL_FILL_UNSAFE
    elif proof_status != PARTIAL_FILL_SAFE:
        # Missing, unknown-semantics, timeout or unclosed proofs never unlock.
        reason = PARTIAL_FILL_PROOF_REQUIRED
    elif execution_payload.get("market_solution_fingerprint") != fingerprint(
        canonical_payload(market)
    ):
        reason = EXECUTION_FINGERPRINT_MISMATCH
    elif (
        int(max_total_unsettled_capital_units or 0) > 0
        and projected_with_unsettled > int(max_total_unsettled_capital_units)
    ):
        reason = UNSETTLED_CAP_EXCEEDED
    elif scope_ready:
        order_ready = True
        reason = scope_reason or "MANUAL_CANARY"
    else:
        reason = scope_reason or SCOPE_OBSERVE_ONLY

    execution_fields: dict[str, object] = {
        "would_submit": would_submit,
        "order_ready": order_ready,
        "reason": reason,
        "execution_solution_fingerprint": execution_solution_fingerprint,
        "projected_total_units": projected_total_units,
        "total_unsettled_capital_units": int(total_unsettled_capital_units or 0),
        "max_total_unsettled_capital_units": int(max_total_unsettled_capital_units or 0),
        # #74 proof state: three-state status plus the closed upper bound and
        # cap for display, and a counterexample reference when UNSAFE.
        "partial_fill_proof": proof_status,
        "partial_fill_upper_bound_units": (
            int(proof_payload["solver_upper_bound"])
            if proof_payload is not None
            and proof_payload.get("solver_upper_bound") is not None
            else None
        ),
        "partial_fill_cap_units": (
            int(proof_payload["max_partial_fill_loss"])
            if proof_payload is not None
            and proof_payload.get("max_partial_fill_loss") is not None
            else None
        ),
        "partial_fill_proof_fingerprint": (
            str(proof_payload.get("fingerprint") or "")
            if proof_payload is not None
            else None
        ),
        "legs": (
            _merge_leg_display(_quantity_rows(execution_payload.get("quantities")), legs)
            if execution_payload is not None
            else []
        ),
    }
    return {
        "component_id": str(
            component_id if component_id is not None else (market.get("component_id") or "")
        ),
        "market": market_fields,
        "execution": execution_fields,
        "qualification": qualification,
        "funding": funding,
        "main_list": main_list,
        "executable": executable,
        "blocked_reason": blocked_reason,
    }
