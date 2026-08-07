"""Validation-phase auto-eat policy: pure gates plus store-backed counters."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Mapping

from .prediction_arbitrage import ThresholdHedgeIntent
from .prediction_arbitrage_store import PredictionArbitrageStore


MIN_BALANCE_FLOOR = Decimal("10.00")
MARKET_COOLDOWN_SECONDS = 300.0
DAILY_ORDER_LIMIT = 5
DAILY_COST_LIMIT = Decimal("25.00")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def should_eat(
    *,
    store: PredictionArbitrageStore,
    signal: Mapping[str, object],
    intent: ThresholdHedgeIntent,
    balance: Decimal,
    now: datetime,
) -> tuple[bool, str]:
    if store.get_validation_mode() != "auto":
        return False, "mode_not_auto"
    signal_id = str(signal.get("signal_id") or "")
    market_id = str(signal.get("market_id") or "")
    if not signal_id or not market_id:
        return False, "signal_unavailable"
    if store.auto_eat_attempt_exists(signal_id, "submitted"):
        return False, "episode_duplicate"
    last = store.last_submitted_auto_eat(market_id)
    if last is not None:
        last_time = _parse(last)
        if last_time is not None and (now - last_time).total_seconds() < MARKET_COOLDOWN_SECONDS:
            return False, "cooldown"
    if balance < MIN_BALANCE_FLOOR:
        return False, "insufficient_balance"
    stats = store.auto_eat_stats(now=now)
    if int(stats["today_submitted"]) >= DAILY_ORDER_LIMIT:
        return False, "daily_cap"
    if Decimal(str(stats["today_cost"])) + intent.total_max_cost > DAILY_COST_LIMIT:
        return False, "daily_cost_cap"
    return True, ""
