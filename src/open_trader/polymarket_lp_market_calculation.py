"""Value-only calculations; results carry no execution or publication authority."""
from dataclasses import dataclass, field
from decimal import Decimal
from .polymarket_lp_market_reader import decimal, freeze
from .polymarket_lp_risk import minimum_order_estimate


@dataclass(frozen=True)
class CalculationPolicy:
    budget: Decimal | None
    target_count: int = 0
    selection_wallet: Decimal | None = None
    selection_capacity: Decimal | None = None
    buy_price_level: int = 1
    config_version: int = 0
    trading_config_version: int = 0
    api_retry_interval_seconds: int = 60
    order_check_interval_seconds: int = 10
    round_interval_seconds: int = 30


@dataclass(frozen=True)
class CalculationResult:
    buy_reserved: Decimal | None
    inventory_cost: Decimal | None
    new_capacity: Decimal | None
    yields: object = field(default_factory=lambda: freeze({}))
    targets: tuple[str, ...] = ()
    target_tokens: tuple[str, ...] = ()
    candidate_yields: object = field(default_factory=lambda: freeze({}))
    policy_version: int = 0


def calculate_current(frame, policy, as_of) -> CalculationResult:
    balance = frame.sources.get('balance')
    funds = tuple(map(decimal, balance.value)) if balance and balance.complete and not balance.error and balance.value else (None, None)
    reserved, inventory = frame.buy_reserved, frame.inventory_cost
    available = None
    account = frame.sources.get('current_account')
    if (policy.budget is not None and all(v is not None for v in (*funds, reserved, inventory))
            and (account is None or account.complete and not account.error)):
        available = max(Decimal(0), min(policy.budget-inventory-reserved, min(funds)-reserved))
    directions = frame.sources.get('directions')
    estimates = {}
    for row in frame.current_buys:
        estimate = None
        direction = directions.value.get(row['order_id']) if directions and directions.complete else None
        if direction:
            try:
                estimate = minimum_order_estimate(direction, {'price': row['price'],
                    'quantity': row['remaining']}, as_of, resting_quantity=row['remaining'])
            except (ValueError, TypeError, KeyError):
                pass
        estimates[row['order_id']] = (estimate.get('yield_pct_per_hour')
            if estimate and estimate.get('state') == 'known' else None)
    candidates = frame.sources.get('candidates')
    ranked, candidate_yields = [], {}
    if candidates and candidates.complete and not candidates.error:
        for row in candidates.value or ():
            try:
                quantity, price = decimal(row['quantity']), decimal(row['price'])
                estimate = minimum_order_estimate(row['direction'], {'price': price, 'quantity': quantity},
                    row.get('evaluation_at', as_of), resting_quantity=quantity if row.get('existing') else Decimal(0))
                rate = estimate.get('yield_pct_per_hour')
                candidate_yields[row['token_id']] = rate if estimate.get('state') == 'known' else None
                if estimate.get('state') == 'known' and rate is not None:
                    ranked.append((row, rate, quantity * price))
            except (KeyError, TypeError, ValueError):
                continue
    ranked.sort(key=lambda x: (-x[1], not x[0].get('existing', False),
        x[0]['condition_id'], x[0]['token_id'], x[0].get('order_id', '')))
    targets, tokens = [], []
    remaining = available if policy.selection_wallet is None else policy.selection_wallet
    capacity = available if policy.selection_capacity is None else policy.selection_capacity
    unique = {}
    for item in ranked:
        row = item[0]
        key = ('order', row.get('order_id')) if row.get('existing') else ('market', row['condition_id'])
        unique.setdefault(key, item)
    for row, rate, cost in unique.values():
        if len(targets) >= policy.target_count or remaining is None or capacity is None:
            break
        limit = capacity if row.get('existing') else min(remaining, capacity)
        if cost <= limit:
            targets.append(row['condition_id'])
            tokens.append(str(row.get('order_id') or row['token_id']))
            remaining = max(Decimal(0), remaining - cost)
            capacity -= cost
    return CalculationResult(reserved, inventory, available, freeze(estimates), tuple(targets), tuple(tokens), freeze(candidate_yields), policy.config_version)
