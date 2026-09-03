"""Issue #64 Slice 3: preflight of one frozen N-leg solution against fresh books.

Pure function: no store, no runtime, no I/O. The head driver (Slice 5) calls
it between FIFO dequeue and atomic admission; it recomputes, from the FRESH
books only, the cost of the FROZEN quantities with the same
``cost_slices_from_book`` accounting the frozen solution was solved with
(#117 taker fee included via the books' ``taker_fee_bps``), then re-checks
freshness, skew, sequences, depth, fees, qualification and the frozen price
bounds.

Price-bound policy (approved ruling 7): a fresh cost equal to or better than
the frozen executed cost passes; a worse price still passes as
``PRICE_WITHIN_BOUNDS`` while it stays inside the proven cost upper bound
AND every qualification check still holds; anything beyond the bound is
``PRICE_BEYOND_BOUND``.

Known limitation, documented on purpose: the Polymarket "sequence" is a
synthetic locally-generated value, not a venue-owned monotonic book counter.
Presence and monotonicity are enforced as far as the frozen payload carries
a sequence baseline; a gap-free venue guarantee does NOT exist, so this
check is a sanity gate, not proof of total ordering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from open_trader.prediction_live_resolver import USD_UNITS_PER_DOLLAR
from open_trader.prediction_market_solution import cost_slices_from_book
from open_trader.prediction_n_leg import problem_from_payload
from open_trader.prediction_snapshot_scheduler import ComponentSnapshot

QUOTE_STALE = "QUOTE_STALE"
CROSS_LEG_SKEW = "CROSS_LEG_SKEW"
SEQUENCE_MISSING = "SEQUENCE_MISSING"
#: Review round 2 (ruling 7): the fresh sequence is behind the frozen baseline.
SEQUENCE_REGRESSED = "SEQUENCE_REGRESSED"
DEPTH_EXHAUSTED = "DEPTH_EXHAUSTED"
FEE_UNKNOWN = "FEE_UNKNOWN"
PRICE_BEYOND_BOUND = "PRICE_BEYOND_BOUND"
PASS = "PASS"
PRICE_WITHIN_BOUNDS = "PRICE_WITHIN_BOUNDS"

#: Defaults mirror the versioned safety config (ruling 7); the config values
#: win whenever the operator wrote them.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 10
DEFAULT_MAX_CROSS_LEG_SKEW_SECONDS = 5

MICROSECONDS_PER_SECOND = 1_000_000


def _config_int(config: Mapping[str, object], key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _frozen_quantities(frozen: Mapping[str, object]) -> dict[str, int]:
    for block in ("execution", "market"):
        payload = frozen.get(block)
        if not isinstance(payload, Mapping):
            continue
        rows = payload.get("quantities")
        if isinstance(rows, (list, tuple)):
            quantities = {
                str(row.get("action_id") or ""): int(row.get("quantity_lots") or 0)
                for row in rows
                if isinstance(row, Mapping)
            }
            if quantities:
                return quantities
    return {}


def _leg_cost(leg, action, quantity_lots: int, units_per_dollar: int) -> tuple[int, bool]:
    """Recompute one leg's cost for the frozen quantity; the boolean reports
    whether the fresh book reaches the required depth."""
    covered = 0
    total = 0
    for slab in cost_slices_from_book(
        action,
        leg.book,
        price_units_per_quote_unit=units_per_dollar,
    ):
        lots = min(quantity_lots, slab.last_lot) - max(1, slab.first_lot) + 1
        if lots > 0:
            total += lots * slab.incremental_cost_upper_bound_units
            covered += lots
        if covered >= quantity_lots:
            break
    return total, covered >= quantity_lots


def preflight(
    frozen: Mapping[str, object],
    fresh_books: ComponentSnapshot,
    safety_config: Mapping[str, object],
    qualification_policy: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    """Re-verify the frozen solution against the fresh books (ruling 7/8).

    Returns ``{"ok", "reason", "checks", "fresh_cost_units",
    "net_profit_units"}``; ``checks`` is the ordered check list with
    ``{key, passed, value, threshold}`` rows for audit/display.
    """
    market = frozen.get("market") if isinstance(frozen.get("market"), Mapping) else {}
    execution = (
        frozen.get("execution") if isinstance(frozen.get("execution"), Mapping) else {}
    )
    checks: list[dict[str, object]] = []

    def _record(key: str, passed: bool, value: object, threshold: object) -> bool:
        checks.append(
            {"key": key, "passed": passed, "value": value, "threshold": threshold}
        )
        return passed

    legs = {leg.leg_id: leg for leg in fresh_books.legs}
    quantities = _frozen_quantities(frozen)
    max_age = _config_int(
        safety_config, "max_quote_age_seconds", DEFAULT_MAX_QUOTE_AGE_SECONDS
    )
    max_skew = _config_int(
        safety_config,
        "max_cross_leg_skew_seconds",
        DEFAULT_MAX_CROSS_LEG_SKEW_SECONDS,
    )

    # 1. Per-leg freshness (single-leg quote age).
    ages: dict[str, float] = {}
    stale = False
    for action_id, leg in sorted(legs.items()):
        age = None
        if leg.received_at is not None:
            age = (now - leg.received_at).total_seconds()
            ages[action_id] = age
        if age is None or age < 0 or age > max_age:
            stale = True
            _record("quote_age", False, action_id, max_age)
    if stale:
        return _fail(checks, QUOTE_STALE, ages, None)

    # 2. Cross-leg exchange_time skew.
    exchange_times = [
        leg.exchange_time for leg in legs.values() if leg.exchange_time is not None
    ]
    skew = (
        (max(exchange_times) - min(exchange_times)).total_seconds()
        if len(exchange_times) == len(legs) and legs
        else None
    )
    if not _record("cross_leg_skew", skew is not None and skew <= max_skew, skew, max_skew):
        return _fail(checks, CROSS_LEG_SKEW, skew, max_skew)

    # 3. Sequences: present everywhere and not behind the frozen baseline.
    #    Polymarket sequences are synthetic (see module docstring): this is a
    #    sanity gate, never proof of a venue-owned total order. Review round
    #    2: a fresh sequence behind the frozen baseline is SEQUENCE_REGRESSED
    #    (distinct from a missing/invalid sequence).
    frozen_sequences = (
        frozen.get("sequences")
        if isinstance(frozen.get("sequences"), Mapping)
        else {}
    )
    sequence_values: dict[str, int] = {}
    missing = False
    regressed = False
    for action_id, leg in sorted(legs.items()):
        if type(leg.sequence) is not int or leg.sequence < 0:
            missing = True
            _record("sequence", False, action_id, "present+monotonic")
            continue
        baseline = frozen_sequences.get(action_id)
        if type(baseline) is int and leg.sequence < baseline:
            regressed = True
            _record("sequence", False, leg.sequence, baseline)
            continue
        sequence_values[action_id] = leg.sequence
    if missing:
        return _fail(checks, SEQUENCE_MISSING, sequence_values, None)
    if regressed:
        return _fail(checks, SEQUENCE_REGRESSED, sequence_values, None)

    # 4. Depth coverage + fresh cost of the frozen quantities, at the frozen
    # solution's own unit scale (component_usd_units_per_dollar, live = 1M).
    units_per_dollar = (
        _int_or_zero(market.get("component_usd_units_per_dollar"))
        or USD_UNITS_PER_DOLLAR
    )
    problem = None
    problem_payload = market.get("problem")
    if isinstance(problem_payload, Mapping):
        try:
            problem = problem_from_payload(problem_payload)
        except (TypeError, ValueError):
            problem = None
    actions = {action.action_id: action for action in problem.actions} if problem is not None else {}
    fresh_cost = 0
    deep = bool(quantities) and set(quantities) == set(legs)
    for action_id, quantity_lots in sorted(quantities.items()):
        leg = legs.get(action_id)
        action = actions.get(action_id)
        if leg is None or action is None or quantity_lots <= 0:
            deep = False
            _record("depth", False, action_id, quantity_lots)
            continue
        cost, covered = _leg_cost(leg, action, quantity_lots, units_per_dollar)
        if not covered:
            deep = False
            _record("depth", False, action_id, quantity_lots)
        fresh_cost += cost
    if not deep:
        return _fail(checks, DEPTH_EXHAUSTED, fresh_cost, sum(quantities.values()))
    _record("depth", True, fresh_cost, "frozen quantities covered")

    # 5. Fees remain modelable: every leg carries a finite non-negative bps.
    fee_known = all(
        getattr(leg.book, "taker_fee_bps", None) is not None for leg in legs.values()
    )
    if not _record("fee_known", fee_known, fresh_cost, "modeled"):
        return _fail(checks, FEE_UNKNOWN, fresh_cost, None)

    payout = _int_or_zero(market.get("bounded_payout_units"))
    frozen_executed = _int_or_zero(
        execution.get("capital_use_units", market.get("bounded_cost_units"))
    )
    proven_bound = _int_or_zero(market.get("bounded_cost_units"))
    net_profit = payout - fresh_cost

    # 6. Qualification re-check (ruling 7): the four policy gates recomputed
    #    with the fresh cost against the frozen payout/release facts.
    qualification_ok, qualification_value = _qualification_check(
        qualification_policy, net_profit, payout, market, now
    )
    if not _record(
        "qualification", qualification_ok, net_profit, qualification_value
    ):
        return _fail(checks, "NOT_QUALIFIED", fresh_cost, net_profit)

    per_trade_cap = _config_int(safety_config, "max_per_trade_cost_units", 0)
    cap_ok = per_trade_cap <= 0 or fresh_cost <= per_trade_cap
    if not _record("per_trade_cap", cap_ok, fresh_cost, per_trade_cap or None):
        return _fail(checks, "PER_TRADE_CAP_EXCEEDED", fresh_cost, per_trade_cap)

    # 7. Price bounds (ruling 7): equal-or-better passes; worse but inside
    #    the proven bound (and qualification-feasible) passes as
    #    PRICE_WITHIN_BOUNDS; beyond the bound fails closed.
    if fresh_cost <= frozen_executed:
        _record("price_bounds", True, fresh_cost, proven_bound)
        return {
            "ok": True,
            "reason": PASS,
            "checks": checks,
            "fresh_cost_units": fresh_cost,
            "net_profit_units": net_profit,
        }
    within_bound = fresh_cost <= proven_bound and net_profit >= 0
    _record("price_bounds", within_bound, fresh_cost, proven_bound)
    if within_bound:
        return {
            "ok": True,
            "reason": PRICE_WITHIN_BOUNDS,
            "checks": checks,
            "fresh_cost_units": fresh_cost,
            "net_profit_units": net_profit,
        }
    return _fail(checks, PRICE_BEYOND_BOUND, fresh_cost, proven_bound)


def _fail(
    checks: list[dict[str, object]],
    reason: str,
    fresh_cost: int | None,
    net_profit: int | None,
) -> dict[str, object]:
    return {
        "ok": False,
        "reason": reason,
        "checks": checks,
        "fresh_cost_units": fresh_cost,
        "net_profit_units": net_profit,
    }


def _int_or_zero(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _decimal_or_none(value: object):
    try:
        from decimal import Decimal

        return Decimal(str(value))
    except Exception:
        return None


def _qualification_check(
    policy: Mapping[str, object],
    net_profit_units: int,
    payout_units: int,
    market: Mapping[str, object],
    now: datetime,
) -> tuple[bool, str]:
    """The four approved policy gates (min_profit / net_margin /
    annualized_return / capital_release) on the fresh-cost economics."""
    from datetime import timedelta

    units_per_dollar = USD_UNITS_PER_DOLLAR
    min_profit = _decimal_or_none(
        policy.get("min_profit_usd") if isinstance(policy, Mapping) else None
    )
    if min_profit is None:
        return False, "min_profit"
    if net_profit_units < int(min_profit * units_per_dollar):
        return False, "min_profit"
    min_margin = _decimal_or_none(
        policy.get("min_net_margin") if isinstance(policy, Mapping) else None
    )
    if min_margin is None or payout_units <= 0:
        return False, "net_margin"
    margin = DecimalMargin(net_profit_units, payout_units)
    if margin < min_margin:
        return False, "net_margin"
    # capital release days from the frozen conservative release instant.
    release_at = _parse_release_at(market.get("capital_release_at"))
    if release_at is None or release_at <= now:
        return False, "capital_release"
    elapsed = (release_at - now) // timedelta(microseconds=1)
    days = max(1, -(-elapsed // (24 * 3600 * MICROSECONDS_PER_SECOND)))
    max_days = policy.get("max_capital_release_days") if isinstance(policy, Mapping) else None
    if type(max_days) is not int or days > max_days:
        return False, "capital_release"
    min_annualized = _decimal_or_none(
        policy.get("min_annualized_return") if isinstance(policy, Mapping) else None
    )
    if min_annualized is None:
        return False, "annualized_return"
    annualized = margin * 365 / days
    if annualized < min_annualized:
        return False, "annualized_return"
    return True, f"margin={format(margin, 'f')},days={days}"


def DecimalMargin(numerator: int, denominator: int):
    from decimal import Decimal

    return Decimal(numerator) / Decimal(denominator)


def _parse_release_at(value: object):
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
