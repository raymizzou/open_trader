"""Read-only projections for the Polymarket LP panel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .polymarket_lp_risk import (
    _account_after_reservations,
    _event_window_check,
    _has_market_order,
    evaluate_lp_entry,
)
from .polymarket_lp import (
    LP_CANDIDATE_REFRESH_SECONDS,
    STOP_LOSS,
    _LP_PRICE_HISTORY_WINDOW,
    PolymarketLPService,
    _decimal,
    _freshness,
    _iso,
    _maybe_decimal,
    _timestamp,
)

_BEIJING = ZoneInfo("Asia/Shanghai")
LP_DAILY_AMPLITUDE_LIMIT = Decimal("0.01")
LP_SHORTLIST_LIMIT = 50
LP_TRIAL_CANDIDATE_LIMIT = 10
LP_COMPETITION_MAX_AGE = timedelta(hours=1)
LP_REFERENCE_PRICE_MAX_AGE = timedelta(hours=1)


def _lp_history_summary(direction: Mapping[str, object]) -> Mapping[str, object] | None:
    value = direction.get("history_summary")
    if isinstance(value, Mapping):
        return value
    market = direction.get("market")
    if isinstance(market, Mapping):
        value = market.get("history_summary")
        if isinstance(value, Mapping):
            return value
    return None


def _lp_summary_amplitude(summary: Mapping[str, object]) -> Decimal | None:
    return _maybe_decimal(summary.get("amplitude"))


def _lp_shortlist_rows(
    direction_facts: object,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Build every market passing the light rules before the fixed cap."""

    if not isinstance(direction_facts, (list, tuple)):
        return []
    checked_at = now.astimezone(UTC) if isinstance(now, datetime) and now.tzinfo else None
    markets: dict[str, dict[str, object]] = {}
    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if not isinstance(market, Mapping):
            market = direction
        condition_id = str(market.get("condition_id") or "").strip()
        market_id = str(market.get("market_id") or condition_id).strip()
        if not condition_id or not market_id:
            continue
        if direction.get("reward_active") is not True:
            continue
        pool = _maybe_decimal(direction.get("daily_pool_usd", market.get("daily_pool_usd")))
        if pool is None or pool <= 0 or market.get("accepting_orders") is not True:
            continue
        if any(direction.get(key) is True or market.get(key) is True for key in ("participating", "already_participating", "known_participation")):
            continue
        summary = _lp_history_summary(direction)
        if summary is None or str(summary.get("state") or "").lower() not in {"known", "ready", "eligible"}:
            continue
        amplitude = _lp_summary_amplitude(summary)
        if amplitude is None or amplitude < 0 or amplitude > LP_DAILY_AMPLITUDE_LIMIT:
            continue
        if checked_at is not None:
            checked_value = summary.get("checked_at", summary.get("updated_at"))
            try:
                checked_summary_at = _timestamp(checked_value, name="history_checked_at")
            except ValueError:
                continue
            age = (checked_at - checked_summary_at).total_seconds()
            if age < 0 or age >= _LP_PRICE_HISTORY_WINDOW.total_seconds():
                continue
            valid_until = summary.get("valid_until")
            if valid_until is not None:
                try:
                    if checked_at >= _timestamp(valid_until, name="history_valid_until"):
                        continue
                except ValueError:
                    continue
        row = markets.setdefault(
            condition_id,
            {
                "market_id": market_id,
                "condition_id": condition_id,
                "market_title": market.get("market_title"),
                "market_url": market.get("market_url"),
                "daily_pool_usd": pool,
                "state": "eligible",
                "directions": [],
            },
        )
        directions = row["directions"]
        if isinstance(directions, list):
            directions.append(
                {
                    "outcome": str(market.get("outcome") or direction.get("outcome") or "").upper(),
                    "token_id": market.get("token_id", direction.get("token_id")),
                    "history_summary": dict(summary),
                }
            )
        if pool > _maybe_decimal(row.get("daily_pool_usd")):
            row["daily_pool_usd"] = pool
    rows = list(markets.values())
    rows.sort(key=lambda row: (-_maybe_decimal(row.get("daily_pool_usd")) or Decimal("0"), str(row.get("market_id") or row.get("condition_id") or "")))
    return rows


def lp_shortlist(
    direction_facts: object,
    *,
    now: datetime | None = None,
    limit: int = LP_SHORTLIST_LIMIT,
) -> list[dict[str, object]]:
    """Select reward markets from prepared facts before any risk reads."""

    if type(limit) is not int or limit < 1:
        return []
    return _lp_shortlist_rows(direction_facts, now=now)[:LP_SHORTLIST_LIMIT]


def _next_review_at(now: datetime) -> datetime:
    local_now = now.astimezone(_BEIJING)
    review_at = datetime.combine(local_now.date(), time(8), tzinfo=_BEIJING)
    if local_now >= review_at:
        review_at += timedelta(days=1)
    return review_at.astimezone(UTC)


def screen_lp_direction(
    direction: object,
    *,
    history: object,
    now: datetime,
) -> dict[str, object]:
    """Screen one reward direction using only market facts and BBO history."""

    result: dict[str, object] = {
        "state": "unknown",
        "reason_codes": [],
        "stability_range": None,
        "stability_min_midpoint": None,
        "stability_max_midpoint": None,
        "stability_sample_count": 0,
        "competition_state": "unknown",
        "competition_quantity": None,
        "price_change_24h": None,
        "price_change_24h_source": None,
    }

    def unknown(reason: str) -> dict[str, object]:
        result["reason_codes"] = [reason]
        return result

    if not isinstance(now, datetime) or now.tzinfo is None:
        return unknown("screen_time_unknown")
    checked_at = now.astimezone(UTC)
    if not isinstance(direction, Mapping):
        return unknown("market_facts_unknown")
    market = direction.get("market")
    book = direction.get("book")
    if not isinstance(market, Mapping):
        return unknown("market_facts_unknown")
    price_change_24h = _maybe_decimal(market.get("price_change_24h"))
    price_change_24h_source = market.get("price_change_24h_source")
    if price_change_24h is not None:
        result["price_change_24h"] = price_change_24h
        result["price_change_24h_source"] = (
            price_change_24h_source
            if isinstance(price_change_24h_source, str)
            and price_change_24h_source.strip()
            else None
        )

    condition_id = str(market.get("condition_id") or "").strip()
    token_id = str(market.get("token_id") or "").strip()
    if not condition_id or not token_id:
        return unknown("market_identity_unknown")
    if direction.get("reward_active") is False:
        result["state"] = "rejected"
        result["reason_codes"] = ["reward_inactive"]
        return result
    if direction.get("reward_active") is not True:
        return unknown("reward_status_unknown")
    pool = _maybe_decimal(direction.get("daily_pool_usd"))
    if pool is None:
        return unknown("reward_pool_unknown")
    if pool <= 0:
        result["state"] = "rejected"
        result["reason_codes"] = ["reward_pool_empty"]
        return result
    if market.get("accepting_orders") is False:
        result["state"] = "rejected"
        result["reason_codes"] = ["market_not_accepting_orders"]
        return result
    if market.get("accepting_orders") is not True:
        return unknown("market_status_unknown")

    reward_stamp = direction.get("reward_checked_at")
    try:
        reward_age = (checked_at - _timestamp(reward_stamp, name="reward_checked_at")).total_seconds()
    except ValueError:
        return unknown("reward_freshness_unknown")
    if reward_age < 0 or reward_age > 60:
        return unknown("reward_data_stale")

    history_rows = history if isinstance(history, (list, tuple)) else None
    if history_rows is None:
        return unknown("stability_history_unknown")

    def levels_for(value: Mapping[str, object]) -> tuple[
        list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]
    ] | None:
        compact_names = (
            "best_bid_price",
            "best_bid_size",
            "best_ask_price",
            "best_ask_size",
        )
        if any(name in value for name in compact_names):
            bid_price, bid_size, ask_price, ask_size = (
                _maybe_decimal(value.get(name)) for name in compact_names
            )
            if (
                bid_price is None
                or bid_size is None
                or ask_price is None
                or ask_size is None
                or not (0 < bid_price <= 1)
                or not (0 < ask_price <= 1)
                or bid_size <= 0
                or ask_size <= 0
            ):
                return None
            return ([(bid_price, bid_size)], [(ask_price, ask_size)])
        try:
            return (
                PolymarketLPService._levels(value.get("bids"), "bids"),
                PolymarketLPService._levels(value.get("asks"), "asks"),
            )
        except (TypeError, ValueError):
            return None

    window_start = checked_at - timedelta(hours=1)
    earliest_anchor = window_start - timedelta(seconds=10)
    samples: list[tuple[datetime, Decimal]] = []
    for row in history_rows:
        if not isinstance(row, Mapping):
            continue
        if (
            row.get("condition_id") != condition_id
            or row.get("token_id") != token_id
        ):
            continue
        try:
            received_at = _timestamp(row.get("received_at"), name="received_at")
        except ValueError:
            continue
        if received_at < earliest_anchor or received_at > checked_at:
            continue
        levels = levels_for(row)
        if levels is None:
            continue
        bids, asks = levels
        if not bids or not asks:
            continue
        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        if best_bid >= best_ask:
            continue
        samples.append((received_at, (best_bid + best_ask) / Decimal("2")))

    samples.sort(key=lambda sample: sample[0])
    anchors = [sample for sample in samples if sample[0] < window_start]
    window_samples = [sample for sample in samples if sample[0] >= window_start]
    covered = bool(window_samples) and (
        window_samples[0][0] == window_start or bool(anchors)
    )
    if anchors:
        latest_anchor = anchors[-1]
        covered = covered and (window_start - latest_anchor[0]).total_seconds() <= 10
        coverage_samples = [latest_anchor, *window_samples]
    else:
        coverage_samples = window_samples
    covered = covered and (
        (checked_at - coverage_samples[-1][0]).total_seconds() <= 10
    ) if coverage_samples else False
    covered = covered and all(
        (current[0] - previous[0]).total_seconds() <= 10
        for previous, current in zip(coverage_samples, coverage_samples[1:])
    )
    if not covered:
        return unknown("stability_history_incomplete")

    midpoints = [sample[1] for sample in coverage_samples]
    minimum_midpoint = min(midpoints)
    maximum_midpoint = max(midpoints)
    stability_range = maximum_midpoint - minimum_midpoint
    result.update(
        {
            "stability_range": stability_range,
            "stability_min_midpoint": minimum_midpoint,
            "stability_max_midpoint": maximum_midpoint,
            "stability_sample_count": len(coverage_samples),
        }
    )

    competition_state = "unknown"
    competition_quantity: Decimal | None = None
    spread = _maybe_decimal(market.get("reward_max_spread"))
    if isinstance(book, Mapping) and spread is not None and spread > 0:
        book_condition = book.get("condition_id", book.get("market"))
        book_token = book.get("token_id", book.get("asset_id"))
        try:
            book_age = (
                checked_at - _timestamp(book.get("received_at"), name="received_at")
            ).total_seconds()
            book_levels = levels_for(book)
        except ValueError:
            book_age = -1
            book_levels = None
        if (
            (book_condition is None or book_condition == condition_id)
            and (book_token is None or book_token == token_id)
            and 0 <= book_age <= 10
            and book_levels is not None
            and book_levels[0]
            and book_levels[1]
        ):
            bids, asks = book_levels
            best_bid = max(price for price, _ in bids)
            best_ask = min(price for price, _ in asks)
            if best_bid < best_ask:
                midpoint = (best_bid + best_ask) / Decimal("2")
                competition_quantity = sum(
                    (
                        size
                        for price, size in (*bids, *asks)
                        if abs(price - midpoint) < spread
                    ),
                    Decimal("0"),
                )
                competition_state = "known"

    result["competition_state"] = competition_state
    result["competition_quantity"] = competition_quantity
    if stability_range > Decimal("0.01"):
        result["state"] = "rejected"
        result["reason_codes"] = ["stability_range_exceeded"]
    else:
        result["state"] = "eligible"
    return result


def lp_recommendation_rows(
    direction_facts: object,
    *,
    histories: Mapping[tuple[str, str], object],
    account: Mapping[str, object],
    now: datetime,
    reservations: object = (),
) -> list[dict[str, object]]:
    """Return actionable market rows with independently screened outcomes."""

    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or not isinstance(direction_facts, (list, tuple))
        or not isinstance(histories, Mapping)
        or not isinstance(account, Mapping)
    ):
        return []
    checked_at = now.astimezone(UTC)
    market_competition: dict[str, dict[str, Decimal | None]] = {}
    recommendations: dict[str, dict[str, object]] = {}

    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if not isinstance(market, Mapping):
            continue
        condition_id = str(market.get("condition_id") or "").strip()
        token_id = str(market.get("token_id") or "").strip()
        outcome = str(market.get("outcome") or "").strip().upper()
        if not condition_id or not token_id or outcome not in {"YES", "NO"}:
            continue

        screening = screen_lp_direction(
            direction,
            history=histories.get((condition_id, token_id)),
            now=checked_at,
        )
        competition = market_competition.setdefault(condition_id, {})
        quantity = _maybe_decimal(screening.get("competition_quantity"))
        competition[outcome] = (
            quantity if screening.get("competition_state") == "known" else None
        )
        if screening.get("state") != "eligible":
            continue

        entry = evaluate_lp_entry(
            {**dict(direction), "screening": screening},
            account=account,
            now=checked_at,
            reservations=reservations,
        )
        guidance = entry.get("guidance")
        pool = _maybe_decimal(direction.get("daily_pool_usd"))
        if entry.get("state") != "eligible" or not isinstance(guidance, Mapping) or pool is None:
            continue

        row = recommendations.get(condition_id)
        if row is None:
            row = {
                "market_id": market.get("market_id"),
                "condition_id": condition_id,
                "market_title": market.get("market_title"),
                "market_url": market.get("market_url"),
                "daily_pool_usd": pool,
                "state": "eligible",
                "competition_state": "unknown",
                "competition_quantity": None,
                "directions": {},
            }
            recommendations[condition_id] = row
        directions = row["directions"]
        if isinstance(directions, dict):
            directions[outcome] = {
                "token_id": token_id,
                "state": entry.get("state"),
                "reason_codes": list(entry.get("reason_codes", ())),
                "screening": screening,
                "guidance": dict(guidance),
            }

    for condition_id, row in recommendations.items():
        outcomes = market_competition.get(condition_id, {})
        yes_quantity = outcomes.get("YES")
        no_quantity = outcomes.get("NO")
        if yes_quantity is not None and no_quantity is not None:
            total = yes_quantity + no_quantity
            row["competition_state"] = "known"
            row["competition_quantity"] = total
        directions = row["directions"]
        if isinstance(directions, dict):
            for recommendation in directions.values():
                if not isinstance(recommendation, dict):
                    continue
                screening = recommendation.get("screening")
                if isinstance(screening, Mapping):
                    recommendation["screening"] = {
                        **dict(screening),
                        "competition_state": row["competition_state"],
                        "competition_quantity": row["competition_quantity"],
                    }

    rows = list(recommendations.values())
    rows.sort(
        key=lambda row: (
            -_decimal(row["daily_pool_usd"], "daily_pool_usd"),
            row.get("competition_state") != "known",
            _maybe_decimal(row.get("competition_quantity")) or Decimal("0"),
            str(row.get("condition_id") or ""),
        )
    )
    return rows


def lp_candidate_rows(
    direction_facts: object,
    *,
    account: Mapping[str, object],
    now: datetime,
    reservations: object = (),
) -> list[dict[str, object]]:
    """Return currently qualifying minimum-size BUY opportunities."""

    if not isinstance(now, datetime) or now.tzinfo is None:
        return []
    checked_at = now.astimezone(UTC)
    if not isinstance(account, Mapping) or not isinstance(direction_facts, (list, tuple)):
        return []
    try:
        _freshness(account.get("checked_at"), checked_at, "account_freshness")
    except ValueError:
        return []
    available_account = _account_after_reservations(account, reservations)
    if available_account is None:
        return []

    review_at = _next_review_at(checked_at)
    rows: list[dict[str, object]] = []
    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        book = direction.get("book")
        if not isinstance(market, Mapping) or not isinstance(book, Mapping):
            continue
        if direction.get("reward_active") is not True:
            continue
        pool = _maybe_decimal(direction.get("daily_pool_usd"))
        if pool is None or pool <= 0:
            continue
        reward_checked_at = direction.get("reward_checked_at")
        if reward_checked_at is None:
            continue
        try:
            reward_stamp = _timestamp(reward_checked_at, name="reward_checked_at")
            age = Decimal(str((checked_at - reward_stamp).total_seconds()))
            if age < 0 or age > LP_CANDIDATE_REFRESH_SECONDS:
                continue
            stamp = book.get("received_at")
            if stamp is None:
                continue
            _freshness(stamp, checked_at, "book_freshness")

            bids = PolymarketLPService._levels(book.get("bids"), "bids")
            if not bids:
                continue
            price = max(level_price for level_price, _ in bids)
            minimum = _decimal(market.get("minimum_order_size"), "minimum_order_size")
            reward_minimum = _decimal(market.get("reward_min_size"), "reward_min_size")
            quantity = max(minimum, reward_minimum)
            identity = {
                key: market.get(key)
                for key in ("market_id", "condition_id", "token_id", "outcome")
            }
            request = {
                **identity,
                "price": price,
                "quantity": quantity,
                "review_at": review_at,
            }
            if _has_market_order(account, market):
                continue
            snapshot = {"market": market, "book": book, "account": available_account}
            facts = PolymarketLPService._validate_snapshot(
                request, snapshot, now=checked_at
            )
            if abs(price - facts["midpoint"]) >= facts["reward_max_spread"]:
                continue
            maker_fee = _maybe_decimal(market.get("fee"))
            if market.get("fees_enabled") is False:
                maker_fee = Decimal("0")
            if maker_fee is None or maker_fee != 0:
                continue
            required_capital = price * quantity
            exit_value = PolymarketLPService._executable_bid_value(snapshot, quantity)
            exit_fee = PolymarketLPService._projected_taker_fee(snapshot, quantity)
            if exit_value is None or exit_fee is None:
                continue
            estimated_exit_loss = max(
                Decimal("0"), required_capital - exit_value + exit_fee
            )
            if estimated_exit_loss >= STOP_LOSS:
                continue
            rows.append(
                {
                    **identity,
                    "market_title": market.get("market_title"),
                    "market_url": market.get("market_url"),
                    "daily_pool_usd": pool,
                    "price": price,
                    "quantity": quantity,
                    "required_capital": required_capital,
                    "estimated_exit_loss": estimated_exit_loss,
                    "checked_at": _iso(checked_at),
                    "review_at": _iso(review_at),
                    "preflight": facts,
                }
            )
        except (ValueError, TypeError, ArithmeticError):
            continue

    rows.sort(
        key=lambda row: (
            -_decimal(row["daily_pool_usd"], "daily_pool_usd"),
            str(row.get("market_id") or ""),
            str(row.get("outcome") or ""),
        )
    )
    return rows


def _lp_competition_entry(entry: object) -> dict[str, object]:
    """Parse one competition cache entry into value, checked_at, and updated."""

    if isinstance(entry, Mapping):
        value = _maybe_decimal(entry.get("value"))
        checked = entry.get("checked_at")
        updated = entry.get("updated")
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        value = _maybe_decimal(entry[0])
        checked = entry[1]
        updated = None
    else:
        value, checked, updated = None, None, None
    checked_at: datetime | None = None
    if isinstance(checked, datetime) and checked.tzinfo is not None:
        checked_at = checked.astimezone(UTC)
    elif isinstance(checked, str):
        try:
            checked_at = _timestamp(checked, name="competition_checked_at")
        except ValueError:
            checked_at = None
    return {
        "value": value,
        "checked_at": checked_at,
        "updated": updated if isinstance(updated, bool) else None,
    }


def _lp_event_window_rejections(
    direction_facts: list[dict[str, object]], *, now: datetime
) -> dict[str, str]:
    """Return condition_id to rejection code for blocking event windows."""

    rejections: dict[str, str] = {}
    for direction in direction_facts:
        market = direction.get("market")
        if not isinstance(market, Mapping):
            continue
        condition_id = str(market.get("condition_id") or "").strip()
        if not condition_id or condition_id in rejections:
            continue
        state, reason, _, _ = _event_window_check(direction, market, now)
        if state == "rejected" and reason:
            rejections[condition_id] = reason
    return rejections


def _lp_reference_price_state(
    summary: object,
    *,
    now: datetime,
) -> tuple[str, datetime | None]:
    """Classify the summary price freshness: known, stale, or missing."""

    if not isinstance(summary, Mapping):
        return "missing", None
    checked_value = summary.get("checked_at", summary.get("updated_at"))
    try:
        checked_at = _timestamp(checked_value, name="reference_price_checked_at")
    except ValueError:
        return "missing", None
    age = (now - checked_at).total_seconds()
    if age >= LP_REFERENCE_PRICE_MAX_AGE.total_seconds():
        return "stale", checked_at
    return "known", checked_at


def _lp_trial_direction_row(
    direction: Mapping[str, object],
    *,
    daily_pool_usd: Decimal,
    now: datetime,
) -> dict[str, object] | None:
    """Build one trial-direction candidate with the legalized minimum size."""

    market = direction.get("market")
    if not isinstance(market, Mapping):
        return None
    minimum = _maybe_decimal(market.get("minimum_order_size"))
    reward_minimum = _maybe_decimal(market.get("reward_min_size"))
    if minimum is None or reward_minimum is None or minimum <= 0 or reward_minimum <= 0:
        return None
    quantity = max(minimum, reward_minimum)
    quantity = (quantity / Decimal("0.01")).to_integral_value(
        rounding=ROUND_CEILING
    ) * Decimal("0.01")
    summary = direction.get("history_summary")
    price_state, price_checked_at = _lp_reference_price_state(summary, now=now)
    reference_price: Decimal | None = None
    if price_state == "known" and isinstance(summary, Mapping):
        reference_price = _maybe_decimal(summary.get("latest_midpoint"))
        if reference_price is not None and not Decimal("0") < reference_price <= Decimal("1"):
            reference_price = None
    if price_state == "known" and reference_price is None:
        price_state = "missing"
    summary_fields = (
        "amplitude",
        "sample_count",
        "window_start",
        "window_end",
        "valid_until",
    )
    evidence = (
        {
            key: summary.get(key)
            for key in summary_fields
            if summary.get(key) is not None
        }
        if isinstance(summary, Mapping)
        else {}
    )
    return {
        "market_id": market.get("market_id"),
        "condition_id": str(market.get("condition_id") or "").strip(),
        "market_title": market.get("market_title"),
        "market_url": market.get("market_url"),
        "token_id": market.get("token_id"),
        "outcome": str(market.get("outcome") or "").strip().upper(),
        "daily_pool_usd": daily_pool_usd,
        "min_quantity": quantity,
        "minimum_order_size": minimum,
        "reward_min_size": reward_minimum,
        "reference_price_state": price_state,
        "reference_price_checked_at": (
            _iso(price_checked_at) if price_checked_at is not None else None
        ),
        "reference_price": reference_price,
        "reference_capital": (
            quantity * reference_price if reference_price is not None else None
        ),
        "summary": evidence,
    }


def _lp_base_rejection_code(
    direction: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> str | None:
    """Return the first light-rule code that would drop this direction."""

    market = direction.get("market")
    if not isinstance(market, Mapping):
        return "market_facts_unknown"
    reward_active = direction.get("reward_active")
    if reward_active is None:
        return "reward_status_unknown"
    if reward_active is False:
        return "reward_inactive"
    pool = _maybe_decimal(direction.get("daily_pool_usd"))
    if pool is None:
        return "reward_pool_unknown"
    if pool <= 0:
        return "reward_pool_empty"
    if market.get("accepting_orders") is not True:
        return (
            "market_not_accepting_orders"
            if market.get("accepting_orders") is False
            else "market_status_unknown"
        )
    if any(
        direction.get(key) is True or market.get(key) is True
        for key in (
            "participating",
            "already_participating",
            "known_participation",
        )
    ):
        return "market_already_participating"
    summary = _lp_history_summary(direction)
    if summary is None or str(summary.get("state") or "").lower() not in {
        "known",
        "ready",
        "eligible",
    }:
        return "history_summary_unknown"
    amplitude = _lp_summary_amplitude(summary)
    if amplitude is None:
        return "history_amplitude_unknown"
    if amplitude < 0 or amplitude > LP_DAILY_AMPLITUDE_LIMIT:
        return "history_amplitude_exceeded"
    checked_value = summary.get("checked_at", summary.get("updated_at"))
    try:
        checked_summary_at = _timestamp(checked_value, name="history_checked_at")
    except ValueError:
        return "history_time_unknown"
    if now is not None:
        # Mirror the shortlist gates: an over-age or past-valid_until summary
        # is dropped there, so the base stage must report why.
        age = (now - checked_summary_at).total_seconds()
        if age < 0 or age >= _LP_PRICE_HISTORY_WINDOW.total_seconds():
            return "history_summary_expired"
        valid_until = summary.get("valid_until")
        if valid_until is not None:
            try:
                if now >= _timestamp(valid_until, name="history_valid_until"):
                    return "history_summary_expired"
            except ValueError:
                return "history_summary_expired"
    return None


def lp_trial_candidates(
    direction_facts: object,
    *,
    competition: object,
    account_budget_facts: object,
    now: datetime,
) -> dict[str, object]:
    """Project the at most ten trial candidates shown on the LP dashboard.

    Pipeline: cheap base facts (including event windows), one direction
    representative per market, the hard over-available exclusion, a normal
    queue (known reference price, ranked by the assumed hourly upper bound
    daily pool ÷ (24 × reference capital)) and a backup queue (unknown
    reference price, by daily pool), then the query batch: the first nine
    normal rows plus backup fill up to ten.  Only the batch head is verified
    against the live book by the service.  Explicit zero competition means
    nobody competes and is excluded as a danger signal; unread or stale
    competition stays unknown, never fills in as 0, and only breaks ties
    after the assumed upper bound.
    """

    result: dict[str, object] = {
        "rows": [],
        "funnel": {
            "read": 0,
            "base": 0,
            "sort": 0,
            "trial": 0,
            "competition_known": 0,
            "competition_unknown": 0,
            "excluded": {"competition_empty": 0, "over_available": 0},
            "gap_reason": None,
            "reasons": {"read": [], "base": [], "sort": [], "trial": []},
        },
        "compared_range": {"compared": 0, "total": 0, "pending": 0},
        "budget": {"available_capital": None},
    }
    if not isinstance(now, datetime) or now.tzinfo is None:
        return result
    checked_at = now.astimezone(UTC)
    if not isinstance(direction_facts, (list, tuple)):
        return result
    budget = (
        account_budget_facts
        if isinstance(account_budget_facts, Mapping)
        else {}
    )
    available_capital = _maybe_decimal(budget.get("available_capital"))
    competition_map: dict[str, object] = {
        str(key): value
        for key, value in competition.items()
        if isinstance(key, str)
    } if isinstance(competition, Mapping) else {}

    directions_by_condition: dict[str, list[dict[str, object]]] = {}
    read_conditions: set[str] = set()
    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if not isinstance(market, Mapping):
            continue
        condition_id = str(market.get("condition_id") or "").strip()
        if not condition_id:
            continue
        read_conditions.add(condition_id)
        directions_by_condition.setdefault(condition_id, []).append(dict(direction))

    funnel_reasons: dict[str, list[dict[str, object]]] = {
        "read": [],
        "base": [],
        "sort": [],
        "trial": [],
    }

    def add_reason(stage: str, row: Mapping[str, object], code: str) -> None:
        market_id = str(row.get("market_id") or row.get("condition_id") or "")
        funnel_reasons[stage].append(
            {
                "market_id": market_id,
                "condition_id": str(row.get("condition_id") or ""),
                "code": code,
            }
        )

    base_rows = _lp_shortlist_rows(direction_facts, now=checked_at)
    window_rejections = _lp_event_window_rejections(
        [row for row in direction_facts if isinstance(row, Mapping)],
        now=checked_at,
    )
    base_candidates: list[dict[str, object]] = []
    base_condition_ids: set[str] = set()
    for row in base_rows:
        condition_id = str(row.get("condition_id") or "")
        if condition_id in window_rejections:
            add_reason("base", row, window_rejections[condition_id])
            continue
        pool = _maybe_decimal(row.get("daily_pool_usd"))
        if pool is None:
            continue
        parsed: list[dict[str, object]] = []
        rules_known = True
        for direction in directions_by_condition.get(condition_id, ()):
            candidate = _lp_trial_direction_row(
                direction, daily_pool_usd=pool, now=checked_at
            )
            if candidate is None:
                rules_known = False
                continue
            parsed.append(candidate)
        if not parsed:
            if not rules_known:
                add_reason("base", row, "market_rules_unknown")
            continue
        # Direction representative: among base-passing directions prefer the
        # ones with a known reference price and the lowest reference capital;
        # with no known price anywhere the identity falls back to the first
        # direction by outcome label then token_id, and the market can only
        # enter the backup queue.
        known = [
            candidate
            for candidate in parsed
            if candidate.get("reference_capital") is not None
        ]
        if known:
            best = min(
                known,
                key=lambda candidate: (
                    candidate.get("reference_capital"),
                    str(candidate.get("outcome") or ""),
                    str(candidate.get("token_id") or ""),
                ),
            )
        else:
            best = min(
                parsed,
                key=lambda candidate: (
                    str(candidate.get("outcome") or ""),
                    str(candidate.get("token_id") or ""),
                ),
            )
        best["queue"] = (
            "backup" if best.get("reference_capital") is None else "normal"
        )
        base_candidates.append(best)
        base_condition_ids.add(condition_id)
    for condition_id in sorted(read_conditions - base_condition_ids):
        if condition_id in window_rejections:
            continue
        for direction in directions_by_condition.get(condition_id, ()):
            code = _lp_base_rejection_code(direction, now=checked_at)
            if code is None:
                continue
            market = direction.get("market")
            add_reason(
                "base",
                {
                    "market_id": (
                        market.get("market_id")
                        if isinstance(market, Mapping)
                        else condition_id
                    ),
                    "condition_id": condition_id,
                },
                code,
            )
            break

    entries = {
        condition_id: _lp_competition_entry(competition_map.get(condition_id))
        for condition_id in read_conditions
    }
    ranked: list[dict[str, object]] = []
    empty_competition = 0
    for candidate in base_candidates:
        condition_id = str(candidate.get("condition_id") or "")
        entry = entries.get(condition_id, {
            "value": None, "checked_at": None, "updated": None,
        })
        value = entry["value"]
        entry_checked = entry["checked_at"]
        stale = (
            entry_checked is None
            or entry_checked > checked_at
            or (checked_at - entry_checked).total_seconds() >= LP_COMPETITION_MAX_AGE.total_seconds()
        )
        known = value is not None and not stale
        if known and value == 0:
            # Explicit zero means nobody competes on the official market:
            # a danger signal, so the market is dropped, never shown as 0.
            empty_competition += 1
            add_reason("sort", candidate, "competition_empty")
            continue
        candidate["competition"] = {
            "value": value if known else None,
            "raw_value": value,
            "checked_at": _iso(entry_checked) if entry_checked is not None else None,
            "state": "known" if known else "unknown",
            "stale": stale and value is not None,
            "updated": entry["updated"],
        }
        ranked.append(candidate)

    def upper_bound_of(candidate: Mapping[str, object]) -> tuple[int, Decimal]:
        capital = candidate.get("reference_capital")
        pool = candidate.get("daily_pool_usd")
        if (
            isinstance(capital, Decimal)
            and capital > 0
            and isinstance(pool, Decimal)
        ):
            upper = (
                pool / (Decimal("24") * capital) * Decimal("100")
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            return (0, -upper)
        return (1, Decimal("0"))

    def sort_key(
        candidate: Mapping[str, object],
    ) -> tuple[int, Decimal, int, Decimal, int, Decimal, str]:
        competition_row = candidate.get("competition")
        assert isinstance(competition_row, Mapping)
        if competition_row.get("state") == "known":
            value = competition_row.get("value")
            competition_key = (0, value if isinstance(value, Decimal) else Decimal("0"))
        else:
            competition_key = (1, Decimal("0"))
        capital = candidate.get("reference_capital")
        if isinstance(capital, Decimal) and capital > 0:
            capital_key = (0, capital)
        else:
            capital_key = (1, Decimal("0"))
        return (
            *upper_bound_of(candidate),
            *competition_key,
            *capital_key,
            str(candidate.get("market_id") or candidate.get("condition_id") or ""),
        )

    def backup_sort_key(candidate: Mapping[str, object]) -> tuple[Decimal, str]:
        pool = candidate.get("daily_pool_usd")
        return (
            -(pool if isinstance(pool, Decimal) else Decimal("0")),
            str(candidate.get("market_id") or candidate.get("condition_id") or ""),
        )

    normal_queue = [row for row in ranked if row.get("queue") == "normal"]
    backup_queue = [row for row in ranked if row.get("queue") != "normal"]
    normal_queue.sort(key=sort_key)
    backup_queue.sort(key=backup_sort_key)
    ranked = normal_queue + backup_queue
    for candidate in ranked:
        competition_row = candidate.get("competition")
        assert isinstance(competition_row, Mapping)
        capital = candidate.get("reference_capital")
        pool = candidate.get("daily_pool_usd")
        competition_value = competition_row.get("value")
        upper_bound: Decimal | None = None
        if (
            isinstance(capital, Decimal)
            and capital > 0
            and isinstance(pool, Decimal)
        ):
            upper_bound = (
                pool / (Decimal("24") * capital) * Decimal("100")
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        candidate["query_rate_upper_bound"] = upper_bound
        candidate["reason"] = [
            (
                f"竞争 {competition_value}（粗排参考）"
                if competition_row.get("state") == "known"
                else "竞争未知（不填 0；指标并列时排已知之后）"
            ),
            (
                "假设每小时收益上限 "
                f"{upper_bound}%/小时（参考价格不变且取得全部奖池时的乐观上限，"
                "仅决定查询顺序）"
                if upper_bound is not None
                else "假设每小时收益上限 未知（参考价缺失或过期；仅决定查询顺序）"
            ),
            "无已知订单或持仓",
        ]
    excluded_counts = {"competition_empty": 0, "over_available": 0}

    def over_available(candidate: Mapping[str, object]) -> bool:
        capital = candidate.get("reference_capital")
        if (
            available_capital is not None
            and isinstance(capital, Decimal)
            and capital > available_capital
        ):
            excluded_counts["over_available"] += 1
            add_reason("trial", candidate, "capital_over_available")
            return True
        return False

    # The known-capital hard exclusion lands before the batch is formed:
    # an excluded market enters no queue and never consumes a batch slot.
    # The sort-stage count stays the pre-exclusion queue total.
    sort_count = len(normal_queue) + len(backup_queue)
    kept_normal = [
        candidate for candidate in normal_queue if not over_available(candidate)
    ]
    kept_backup = [
        candidate for candidate in backup_queue if not over_available(candidate)
    ]
    batch: list[dict[str, object]] = list(
        kept_normal[: LP_TRIAL_CANDIDATE_LIMIT - 1]
    )
    for candidate in kept_backup:
        if len(batch) >= LP_TRIAL_CANDIDATE_LIMIT:
            break
        batch.append(candidate)
    for candidate in kept_normal[LP_TRIAL_CANDIDATE_LIMIT - 1:]:
        if len(batch) >= LP_TRIAL_CANDIDATE_LIMIT:
            break
        batch.append(candidate)
    seen_conditions: set[str] = set()
    selected: list[dict[str, object]] = []
    for candidate in batch:
        condition_id = str(candidate.get("condition_id") or "")
        if condition_id in seen_conditions:
            continue
        seen_conditions.add(condition_id)
        candidate["verification"] = "pending"
        candidate["reason"] = list(candidate.get("reason") or [])
        if available_capital is None:
            candidate["reason"].append("可用资金未知（不做超可用排除）")
        else:
            candidate["reason"].append("占资 ≤ 可用")
        selected.append(dict(candidate))
    compared = sum(
        1 for entry in entries.values() if entry["value"] is not None
    )
    funnel: dict[str, object] = {
        "read": len(read_conditions),
        "base": len(base_candidates),
        "sort": sort_count,
        "trial": len(selected),
        "competition_known": sum(
            1
            for candidate in ranked
            if isinstance(candidate.get("competition"), Mapping)
            and candidate["competition"].get("state") == "known"
        ),
        "competition_unknown": sum(
            1
            for candidate in ranked
            if isinstance(candidate.get("competition"), Mapping)
            and candidate["competition"].get("state") != "known"
        ),
        "excluded": {
            "competition_empty": empty_competition,
            "over_available": excluded_counts["over_available"],
        },
        "normal_queue_count": len(kept_normal),
        "backup_queue_count": len(kept_backup),
        "reference_price_unknown": len(kept_backup),
        "gap_reason": (
            None
            if len(selected) >= LP_TRIAL_CANDIDATE_LIMIT
            else f"合格候选不足 {LP_TRIAL_CANDIDATE_LIMIT} 个（本轮 {len(selected)} 个）"
        ),
        "reasons": funnel_reasons,
    }
    funnel["budget"] = {"available_capital": available_capital}
    result["rows"] = selected
    result["funnel"] = funnel
    result["compared_range"] = {
        "compared": compared,
        "total": len(read_conditions),
        "pending": len(read_conditions) - compared,
    }
    result["budget"] = {"available_capital": available_capital}
    return result


def lp_report_totals(
    opening: Mapping[str, object], *, paid_rewards: Decimal | None = None
) -> dict[str, object]:
    """Project realized trade P&L separately from marked open inventory."""

    buy_quantity = _maybe_decimal(opening.get("buy_filled_quantity"))
    buy_cost = _maybe_decimal(opening.get("buy_cost"))
    buy_fees = _maybe_decimal(opening.get("buy_fees"))
    sold_quantity = _maybe_decimal(opening.get("sold_quantity"))
    sold_revenue = _maybe_decimal(opening.get("sold_revenue"))
    sell_fees = _maybe_decimal(opening.get("sell_fees"))
    residual_quantity = _maybe_decimal(opening.get("residual_quantity"))
    residual_exit_value = _maybe_decimal(opening.get("residual_exit_value"))
    projected_exit_fee = _maybe_decimal(opening.get("projected_exit_fee"))
    verified_paid_rewards = _maybe_decimal(paid_rewards)

    def nonnegative(value: Decimal | None) -> Decimal | None:
        return value if value is not None and value >= 0 else None

    buy_quantity = nonnegative(buy_quantity)
    buy_cost = nonnegative(buy_cost)
    buy_fees = nonnegative(buy_fees)
    sold_quantity = nonnegative(sold_quantity)
    sold_revenue = nonnegative(sold_revenue)
    sell_fees = nonnegative(sell_fees)
    residual_quantity = nonnegative(residual_quantity)
    residual_exit_value = nonnegative(residual_exit_value)
    projected_exit_fee = nonnegative(projected_exit_fee)
    verified_paid_rewards = nonnegative(verified_paid_rewards)

    realized_trade_pnl: Decimal | None = None
    if (
        sold_quantity == 0
        and sold_revenue == 0
        and sell_fees == 0
    ):
        realized_trade_pnl = Decimal("0")
    elif (
        buy_quantity is not None
        and buy_quantity > 0
        and buy_cost is not None
        and buy_fees is not None
        and sold_quantity is not None
        and sold_quantity <= buy_quantity
        and sold_revenue is not None
        and sell_fees is not None
    ):
        sold_fraction = sold_quantity / buy_quantity
        allocated_buy_cost = buy_cost * sold_fraction
        allocated_buy_fees = buy_fees * sold_fraction
        realized_trade_pnl = (
            sold_revenue - allocated_buy_cost - allocated_buy_fees - sell_fees
        )

    residual_cost: Decimal | None = None
    if (
        buy_quantity is not None
        and buy_cost is not None
        and buy_fees is not None
        and residual_quantity is not None
        and (buy_quantity > 0 or residual_quantity == 0)
    ):
        residual_cost = (
            Decimal("0")
            if buy_quantity == 0
            else (buy_cost + buy_fees) * residual_quantity / buy_quantity
        )

    residual_exit_net_value = (
        residual_exit_value - projected_exit_fee
        if residual_exit_value is not None and projected_exit_fee is not None
        else None
    )
    residual_pnl = (
        residual_exit_net_value - residual_cost
        if residual_exit_net_value is not None and residual_cost is not None
        else None
    )
    realized_net_pnl = (
        realized_trade_pnl + verified_paid_rewards
        if realized_trade_pnl is not None and verified_paid_rewards is not None
        else None
    )
    return {
        "realized_trade_pnl": realized_trade_pnl,
        "residual_cost": residual_cost,
        "residual_exit_net_value": residual_exit_net_value,
        "residual_pnl": residual_pnl,
        "paid_rewards": verified_paid_rewards,
        "realized_net_pnl": realized_net_pnl,
    }
